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

import html

import numpy as np
import pyqtgraph.opengl as gl
from OpenGL import GL
from pyqtgraph.opengl import GLViewWidget
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout

from foursight.gui.batching import Batch, bounds, build_batches
from foursight.gui.legend import (
    HIGHLIGHT_COLOR,
    MARKER_COLOR,
    LegendEntry,
    Swatch,
    hex_color,
    legend_entries,
)
from foursight.gui.picking import ScreenProjection, pick, project_store
from foursight.gui.playback import marker_point
from foursight.gui.timeline import Timeline
from foursight.sim.segments import SegmentStore
from foursight.sim.simulator import Simulation

# Every batch draws at width 1.0. Not a stylistic choice: see the module docstring.
LINE_WIDTH = 1.0
# Toolpath batches draw with **blending off**, which is not pyqtgraph's default and has to be said.
#
# `GLLinePlotItem` defaults to `glOptions="additive"`, and additive blending *adds overlapping colours
# together*. A red rapid (0.90, 0.25, 0.20) crossing a green feed (0.20, 0.85, 0.35) renders as
# (1.0, 1.0, 0.55) — `#ffff8c`, a yellow that is in no palette and means nothing at all. On a wrapped
# rotary program, where the path crosses itself constantly, most of the screen ends up that colour.
# That is precisely the invariant PLAN.md § Batching Layer states — "colour carries every distinction" —
# being broken by the renderer rather than by the batching, and it is unfalsifiable from the data model.
#
# Depth testing stays **off**, as it has been since M2. That is a separate question from blending: with
# it off the whole path is visible through itself, which is what a wireframe preview is for, and it is
# also what lets the selection highlight and the tool marker draw on top without z-fighting against the
# geometry they coincide with. Every pixel now shows exactly one palette colour — the last batch drawn
# there — rather than a sum of several.
BATCH_GL_OPTIONS = {
    GL.GL_DEPTH_TEST: False,
    GL.GL_BLEND: False,
    GL.GL_CULL_FACE: False,
}
# Marker diameter in *pixels*, via `pxMode`. Not millimetres: a world-sized marker vanishes when the
# camera pulls back to fit a large part and swamps the toolpath when it zooms in.
MARKER_SIZE_PX = 12.0
# The legend sits over the toolpath, so it is translucent and dark: it must be readable against the
# near-black background without hiding the geometry it is there to explain.
LEGEND_STYLE = (
    "background: rgba(20, 20, 20, 190); color: #d8d8d8; border-radius: 4px; padding: 6px 8px;"
)
LEGEND_MARGIN_PX = 10
# Reused verbatim from `session` and `selection`, which say this about the same geometry. The colour
# means nothing on its own — what makes an amber span actionable is knowing it is a centreline.
LEGEND_TOOLTIP = (
    "Unverified spans are drawn as the programmed centreline, which is not where the tool goes."
)
# Fallback camera distance for an empty or degenerate program, in mm.
DEFAULT_DISTANCE_MM = 200.0
# The view is fitted to this multiple of the geometry's extent, so the path is not flush to the edges.
FIT_MARGIN = 1.6


class LegendOverlay(QLabel):
    """The colour key, drawn over the toolpath.

    A single rich-text label rather than a grid of swatch widgets. There is no state and no layout to
    get wrong: one `setText` replaces the whole key, so the legend cannot end up half-updated with a
    row from the previous program still in it.

    It is a plain child widget rather than anything drawn into the GL scene, which is what keeps it out
    of the buffer budget and out of `viewport.items` — the item-count assertions that guard against
    stale geometry stay meaningful.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(LEGEND_STYLE)
        self.setToolTip(LEGEND_TOOLTIP)
        self.setTextFormat(Qt.RichText)
        self.hide()

    def set_entries(self, entries: tuple[LegendEntry, ...], *, visible: bool) -> None:
        """Show ``entries``, or hide entirely when there are none or the user turned the legend off."""
        self.setText("<br>".join(_legend_row(entry) for entry in entries))
        # Hidden when empty even if the user asked for a legend: an empty box over an empty viewport
        # says nothing, and a key with no entries reads as a rendering failure.
        self.setVisible(visible and bool(entries))


def _legend_row(entry: LegendEntry) -> str:
    """One row of the key: a swatch in the entry's own colour, then its name.

    The swatch glyph follows the swatch *kind*, so the tool position reads as a dot and the geometry
    as lines — a legend whose marks do not resemble what they name has to be decoded twice.
    """
    glyph = "&#9679;" if entry.swatch is Swatch.POINT else "&#9473;&#9473;"
    return (
        f'<span style="color: {hex_color(entry.color)}">{glyph}</span>&nbsp;&nbsp;'
        f"{html.escape(entry.label)}"
    )


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
        self._marker: gl.GLScatterPlotItem | None = None
        self.batches: list[Batch] = []
        self.highlighted_segments = 0
        self._store: SegmentStore | None = None
        self._projection: ScreenProjection | None = None
        #: Which coordinates are on screen. Owned here so batching, the highlight, picking and the camera
        #: cannot disagree — each reading `store.lin` independently is how three of them end up in machine
        #: coordinates while one is in part coordinates.
        self.part_coordinates = False
        #: Whether the user wants the key. Whether it is *shown* also depends on there being something
        #: to name, which `_refresh_legend` decides — the two are not the same question.
        self.legend_visible = True
        self.legend = LegendOverlay(self)
        # A layout on the GL widget parents the overlay and pins it to the corner across every resize,
        # which is cheaper and harder to get wrong than a `resizeEvent` override that has to recompute
        # a position. `GLViewWidget` has no layout of its own, so there is nothing to displace.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            LEGEND_MARGIN_PX, LEGEND_MARGIN_PX, LEGEND_MARGIN_PX, LEGEND_MARGIN_PX
        )
        layout.addWidget(self.legend, alignment=Qt.AlignTop | Qt.AlignLeft)
        layout.addStretch(1)
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
        self.part_coordinates = use_part_coordinates
        self.batches = build_batches(
            store, untrusted=untrusted, use_part_coordinates=use_part_coordinates
        )
        # A highlight indexes into the *previous* store, so it is meaningless the moment the geometry
        # changes — and a mask of the wrong length would either raise or, worse, silently highlight
        # arbitrary segments of the new program.
        self.clear_highlight()
        # Same argument for the playback marker: it is a position in the *previous* program's timeline.
        self.clear_marker()
        self._store = store
        self._projection = None
        self._rebuild_items()
        self.fit_to(store, use_part_coordinates=use_part_coordinates)
        self._refresh_legend()

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
                glOptions=BATCH_GL_OPTIONS,
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

        It lands on top because **no toolpath batch ever writes depth** — `BATCH_GL_OPTIONS` disables
        the depth test, and a disabled test writes nothing — so the depth buffer the highlight tests
        against is empty and it draws over whatever is already there. Stated here because it is a
        property of the *batches*, not of this item: turning depth testing on for the toolpath would
        make a selection z-fight against the very segments it duplicates, and the symptom would be a
        highlight that flickers or vanishes rather than an error. A selected segment buried behind
        other geometry must stay visible, or the user reads it as "this line draws nothing" — the
        opposite of what a selection is for.
        """
        vertices = self._highlight_vertices(store, mask, self.part_coordinates)
        self.highlighted_segments = 0 if vertices is None else vertices.shape[0] // 2

        if vertices is None:
            if self._highlight is not None:
                self._highlight.setVisible(False)
            self._refresh_legend()
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
        self._refresh_legend()

    def clear_highlight(self) -> None:
        self.set_highlight(SegmentStore.empty(), None)

    # ------------------------------------------------------------------ playback marker (T10.3)

    def set_marker(self, store: SegmentStore, timeline: Timeline, seconds: float) -> None:
        """Draw the tool position at ``seconds``, or hide the marker when there is nothing to mark.

        One long-lived item updated with `setData`, for the highlight's reasons: its existence never
        depends on the data, so no stale item can survive a change, and it moves once a *frame* during
        playback rather than once a load.

        Two departures from the highlight, both deliberate.

        It is a **point sprite** rather than a small cross of lines. `pxMode` sizes it in pixels
        through pyqtgraph's own vertex shader, which works on the core profile where `glLineWidth` is
        silently inert — the same constraint that makes colour carry all the meaning in `batching`.

        It keeps ``GLScatterPlotItem``'s default **additive** GL options, and that is the load-bearing
        detail: `additive` is the mode that turns the depth test *off*. `translucent` leaves it on
        (the highlight gets away with it only because it is drawn at the same depth as the geometry it
        duplicates). The marker sits on a path that may be deep inside the work, and a tool position
        that disappears behind the stock reads as the program having finished.
        """
        point = marker_point(store, timeline, seconds, part_coordinates=self.part_coordinates)
        if point is None:
            if self._marker is not None:
                self._marker.setVisible(False)
            self._refresh_legend()
            return

        vertices = point.reshape(1, 3).astype(np.float32, copy=False)
        if self._marker is None:
            self._marker = gl.GLScatterPlotItem(
                pos=vertices, color=MARKER_COLOR, size=MARKER_SIZE_PX, pxMode=True
            )
            self._marker.setDepthValue(2)  # drawn after the highlight's 1, so it stays on top
            self.addItem(self._marker)
        else:
            self._marker.setData(pos=vertices)
        self._marker.setVisible(True)
        self._refresh_legend()

    # ------------------------------------------------------------------ legend (T11.2)

    def set_legend_visible(self, visible: bool) -> None:
        """Show or hide the colour key."""
        self.legend_visible = visible
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        """Rebuild the key from what is currently on screen.

        Called explicitly from every method that changes what is drawn, rather than relying on
        `set_store` reaching it through `clear_highlight`. A refresh that happens only as a side effect
        of another call is one refactor away from silently not happening, and the symptom would be a
        legend describing the *previous* program — which is worse than no legend.
        """
        marker_shown = self._marker is not None and self._marker.visible()
        entries = legend_entries(
            self.batches, highlighted=self.highlighted_segments > 0, marker=marker_shown
        )
        self.legend.set_entries(entries, visible=self.legend_visible)

    def clear_marker(self) -> None:
        """Hide the marker, and **never create one**.

        A viewport that has never played must hold exactly the grid plus its batches: the item-count
        assertions that guard against stale geometry (`test_one_gl_item_is_created_per_batch` and the
        vertex-sum beside it) count everything in the scene, so a marker constructed eagerly here
        would turn those regression tests into a maintenance burden rather than a net.
        """
        if self._marker is not None:
            self._marker.setVisible(False)
        self._refresh_legend()

    @staticmethod
    def _highlight_vertices(
        store: SegmentStore, mask: np.ndarray | None, part_coordinates: bool
    ) -> np.ndarray | None:
        """``(2M, 3)`` float32 vertices for the selected segments, or None when there are none.

        Reads whichever array is on screen. Highlighting `lin` while part coordinates are displayed would
        draw the selection somewhere the toolpath is not — visibly wrong, but only if you happen to look.
        """
        if mask is None or len(store) == 0 or not mask.any():
            return None
        if mask.shape != (len(store),):
            raise ValueError(
                f"highlight mask has shape {mask.shape}, expected ({len(store)},) — one per segment"
            )
        source = store.lin
        if part_coordinates:
            if store.lin_part is None:
                raise ValueError(
                    "part coordinates are displayed but no display transform is attached"
                )
            source = store.lin_part
        return source[mask].astype(np.float32, copy=False).reshape(-1, 3)

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
            mvp, width, height, len(self._store), self.part_coordinates
        ):
            return self._projection
        self._projection = project_store(
            self._store, mvp, width, height, part_coordinates=self.part_coordinates
        )
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
