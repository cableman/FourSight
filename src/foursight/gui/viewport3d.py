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

from foursight.gui.batching import Batch, bounds, build_batches
from foursight.sim.segments import SegmentStore
from foursight.sim.simulator import Simulation

# Every batch draws at width 1.0. Not a stylistic choice: see the module docstring.
LINE_WIDTH = 1.0
# Fallback camera distance for an empty or degenerate program, in mm.
DEFAULT_DISTANCE_MM = 200.0
# The view is fitted to this multiple of the geometry's extent, so the path is not flush to the edges.
FIT_MARGIN = 1.6


class ToolpathViewport(GLViewWidget):
    """Draws a ``Simulation`` as a small number of batched line items.

    Orbit, pan and zoom come from ``GLViewWidget`` itself: left-drag orbits, middle-drag pans, wheel
    zooms. T2.7 wires this into a window; picking is T3.0 and deliberately absent, because with 500k
    segments in four buffers Qt item picking is unavailable and the strategy is still undecided.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[gl.GLLinePlotItem] = []
        self._grid: gl.GLGridItem | None = None
        self.batches: list[Batch] = []
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
