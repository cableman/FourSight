"""GL view and camera. Kept thin: the batching that can be *wrong* lives in ``batching.py``.

D1 is resolved — T0.7 measured **376.9 fps median at 500k segments** in 10 ``GLLinePlotItem``s on the
baseline Intel Iris Xe, against a 30 fps requirement, so this builds on pyqtgraph and no raw
``QOpenGLWidget`` is needed. See PLAN.md § Rendering Constraints for the full numbers.

What this module must not do:

- **One draw call per move.** Geometry arrives pre-batched — four items for a typical program — and
  each `set_simulation` call rebuilds them wholesale rather than appending.
- **Encode meaning in line width.** pyqtgraph skips `glLineWidth` entirely on core
  forward-compatible profiles, so `width=` is inert there with no error. Colour carries everything.
- **Mutate the store.** The camera reads bounds; nothing here writes to `lin`.

Qt is imported at module scope, which is fine because this module lives in ``gui/`` and nothing
outside ``gui/`` imports it. ``gui.batching`` stays Qt-free so its tests need no display.
"""

import numpy as np
import pyqtgraph.opengl as gl
from pyqtgraph.opengl import GLViewWidget
from PySide6.QtCore import Qt, Signal

from foursight.gui.batching import Batch, bounds, build_batches
from foursight.gui.picking import ScreenProjection, pick, project_store
from foursight.sim.segments import SegmentStore
from foursight.sim.simulator import Simulation

# Every batch draws at width 1.0. Not a stylistic choice: see the module docstring.
LINE_WIDTH = 1.0
# The selection highlight. Bright and cool, so it cannot be mistaken for a rapid (red), a feed (green)
# or an unverified span (amber) — the highlight is a *view* state, not a property of the toolpath.
HIGHLIGHT_COLOR = (0.35, 0.95, 1.0, 1.0)
# Fallback camera distance for an empty or degenerate program, in mm.
DEFAULT_DISTANCE_MM = 200.0
# The view is fitted to this multiple of the geometry's extent, so the path is not flush to the edges.
FIT_MARGIN = 1.6


class ToolpathViewport(GLViewWidget):
    """Draws a ``Simulation`` as a small number of batched line items.

    Orbit, pan and zoom come from ``GLViewWidget`` itself: left-drag orbits, middle-drag pans, wheel
    zooms. Clicking emits `segment_picked` with a segment index (T3.3), using the CPU screen-space
    strategy decided in T3.0 — Qt item picking is unavailable with 500k segments in a handful of buffers.
    """

    #: Emitted with the index of the segment under a click. Not emitted when the click hits nothing,
    #: so a miss leaves the current selection alone rather than clearing it — a slightly-off click
    #: should not undo the selection the user was looking at.
    segment_picked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[gl.GLLinePlotItem] = []
        self._grid: gl.GLGridItem | None = None
        self._highlight: gl.GLLinePlotItem | None = None
        self.batches: list[Batch] = []
        self.highlighted_segments = 0
        self._store: SegmentStore | None = None
        self._projection: ScreenProjection | None = None
        self.setCameraPosition(distance=DEFAULT_DISTANCE_MM, elevation=30, azimuth=-60)
        self._add_grid()

    # ------------------------------------------------------------------ geometry

    def set_simulation(self, simulation: Simulation, *, use_part_coordinates: bool = False) -> None:
        """Replace the drawn geometry with ``simulation``'s, styling untrusted spans distinctly.

        The untrusted mask comes from the simulation rather than being recomputed here: a
        cutter-compensated span is drawn as the programmed centreline and must not look like a path
        the tool actually follows.
        """
        self.set_store(
            simulation.store,
            untrusted=simulation.unverified_mask(),
            use_part_coordinates=use_part_coordinates,
        )

    def set_store(
        self,
        store: SegmentStore,
        *,
        untrusted: np.ndarray | None = None,
        use_part_coordinates: bool = False,
    ) -> None:
        """Lower-level entry point: draw a bare store, with no span information."""
        self.batches = build_batches(
            store, untrusted=untrusted, use_part_coordinates=use_part_coordinates
        )
        # A highlight indexes into the *previous* store, so it is meaningless the moment the geometry
        # changes — and a mask of the wrong length would either raise or, worse, silently highlight
        # arbitrary segments of the new program.
        self.clear_highlight()
        self._store = store
        self._projection = None
        self._rebuild_items()
        self.fit_to(store, use_part_coordinates=use_part_coordinates)

    def clear_toolpath(self) -> None:
        """Remove the toolpath, keeping the grid and camera."""
        self._remove_items()
        self.batches = []

    def _remove_items(self) -> None:
        """Drop the GL items without forgetting ``batches``.

        Kept separate from `clear_toolpath` deliberately. Folding the two together is what broke this
        the first time: `_rebuild_items` called `clear_toolpath`, which reset `self.batches` to `[]`
        *before* the loop that reads it, so no items were ever created and every program rendered as
        an empty scene. Nothing about that failure looks like an error at runtime.
        """
        for item in self._items:
            self.removeItem(item)
        self._items.clear()

    def _rebuild_items(self) -> None:
        """One ``GLLinePlotItem`` per batch, rebuilt wholesale.

        Reusing items across loads by calling `setData` would be a reasonable optimization, but a
        stale item left behind when the batch count *shrinks* would keep drawing geometry from the
        previous program — the exact class of confidently-wrong output the plan forbids. A full
        rebuild costs one upload per load and cannot do that.
        """
        self._remove_items()
        for batch in self.batches:
            item = gl.GLLinePlotItem(
                pos=batch.vertices,
                color=batch.color,
                width=LINE_WIDTH,
                mode="lines",  # consecutive vertex pairs, so a batch need not be one polyline
                antialias=False,  # a per-frame cost that buys little on a dense path
            )
            self.addItem(item)
            self._items.append(item)

    # ------------------------------------------------------------------ selection highlight

    def set_highlight(self, store: SegmentStore, mask: np.ndarray | None) -> None:
        """Draw ``mask``'s segments on top of everything, or clear the highlight when it is empty.

        Kept as **one long-lived item updated with `setData`**, unlike the toolpath batches which are
        rebuilt wholesale. The reasoning that made rebuilding right there does not apply here: there is
        exactly one highlight item and its existence never depends on the data, so no stale item can
        survive a change. It also follows the text cursor, so it updates far more often than a load does.

        Drawn with the **depth test off**, deliberately. A selected segment buried behind other geometry
        would otherwise highlight invisibly, and the user would read that as "this line draws nothing" —
        the opposite of what a selection is for.
        """
        vertices = self._highlight_vertices(store, mask)
        self.highlighted_segments = 0 if vertices is None else vertices.shape[0] // 2

        if vertices is None:
            if self._highlight is not None:
                self._highlight.setVisible(False)
            return

        if self._highlight is None:
            self._highlight = gl.GLLinePlotItem(
                pos=vertices, color=HIGHLIGHT_COLOR, width=LINE_WIDTH, mode="lines", antialias=False
            )
            # `translucent` sorts without writing depth; combined with the disabled test the highlight
            # always lands on top of the geometry it belongs to.
            self._highlight.setGLOptions("translucent")
            self._highlight.setDepthValue(1)
            self.addItem(self._highlight)
        else:
            self._highlight.setData(pos=vertices)
        self._highlight.setVisible(True)

    def clear_highlight(self) -> None:
        self.set_highlight(SegmentStore.empty(), None)

    @staticmethod
    def _highlight_vertices(store: SegmentStore, mask: np.ndarray | None) -> np.ndarray | None:
        """``(2M, 3)`` float32 vertices for the selected segments, or None when there are none."""
        if mask is None or len(store) == 0 or not mask.any():
            return None
        if mask.shape != (len(store),):
            raise ValueError(
                f"highlight mask has shape {mask.shape}, expected ({len(store)},) — one per segment"
            )
        return store.lin[mask].astype(np.float32, copy=False).reshape(-1, 3)

    # ------------------------------------------------------------------ picking (T3.3)

    def mouseReleaseEvent(self, event) -> None:
        """A left click without a drag picks a segment.

        Distinguished from an orbit by comparing against the press position: `GLViewWidget` uses
        left-drag to orbit, so picking on *press* would fire on every orbit and jump the editor around
        while the user is just looking at the part.
        """
        super().mouseReleaseEvent(event)
        if event.button() != Qt.LeftButton:
            return
        index = self.pick_at(event.position().x(), event.position().y())
        if index is not None:
            self.segment_picked.emit(index)

    def pick_at(self, x: float, y: float) -> int | None:
        """The segment under widget coordinates ``(x, y)``, or None."""
        projection = self.projection()
        return None if projection is None else pick(projection, (x, y))

    def projection(self) -> ScreenProjection | None:
        """The cached screen projection, reprojecting only when the camera or geometry has changed.

        The cache is keyed on the **matrix itself** rather than on a dirty flag set by camera setters.
        A flag has to be maintained at every mutation site — `setCameraPosition`, mouse drag, wheel,
        resize, a direct `opts` poke — and a single missed one returns the wrong segment with nothing in
        the picture to suggest it. Comparing the matrix cannot miss.
        """
        if self._store is None or len(self._store) == 0:
            return None
        mvp = self.pick_matrix()
        width, height = self.width(), self.height()
        if self._projection is not None and self._projection.matches(
            mvp, width, height, len(self._store)
        ):
            return self._projection
        self._projection = project_store(self._store, mvp, width, height)
        return self._projection

    def pick_matrix(self) -> np.ndarray:
        """``projection @ view`` as a 4x4 numpy array, matching what the renderer used.

        pyqtgraph 0.14 takes `(region, viewport)` on `projectionMatrix()` — older versions took nothing —
        and `QMatrix4x4.data()` is column-major, hence the transposes. Both were pinned by the T3.0 spike.
        """
        viewport = (0, 0, self.width(), self.height())
        projection = np.array(self.projectionMatrix(viewport, viewport).data(), dtype=np.float64)
        view = np.array(self.viewMatrix().data(), dtype=np.float64)
        return projection.reshape(4, 4).T @ view.reshape(4, 4).T

    # ------------------------------------------------------------------ camera

    def fit_to(self, store: SegmentStore, *, use_part_coordinates: bool = False) -> None:
        """Point the camera at the geometry's centre, far enough out to see all of it."""
        extent = bounds(store, use_part_coordinates=use_part_coordinates)
        if extent is None:
            self.setCameraPosition(distance=DEFAULT_DISTANCE_MM)
            return
        centre = extent.mean(axis=0)
        span = float(np.max(extent[1] - extent[0]))
        # A single-point or single-axis program has zero span in some direction; fall back rather
        # than dividing the camera distance down to zero and rendering nothing.
        distance = max(span * FIT_MARGIN, DEFAULT_DISTANCE_MM * 0.1)
        self.setCameraPosition(pos=_vector(centre), distance=distance, elevation=30, azimuth=-60)

    def _add_grid(self) -> None:
        grid = gl.GLGridItem()
        grid.setSize(x=400, y=400)
        grid.setSpacing(x=10, y=10)
        self._grid = grid
        self.addItem(grid)


def _vector(point: np.ndarray):
    """A ``(3,)`` array as the Qt vector ``setCameraPosition`` wants."""
    from pyqtgraph import Vector

    return Vector(float(point[0]), float(point[1]), float(point[2]))
