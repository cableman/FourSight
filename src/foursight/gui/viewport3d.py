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
from foursight.gui.solid_mesh import build_mesh
from foursight.gui.timeline import Timeline
from foursight.sim.segments import SegmentStore
from foursight.sim.simulator import Simulation
from foursight.sim.solid import SolidField

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
# The selection highlight and, since M12, the tool marker's peer: opaque, and **never depth-tested**.
#
# This used to be `setGLOptions("translucent")`, which was safe only for as long as nothing in the scene
# wrote depth — pyqtgraph's `translucent` *enables* the depth test, and the batches above leave the
# buffer empty, so the highlight landed on top by default. The solid view broke that assumption: a mesh
# writes depth, and a selected segment inside the material would then be occluded by the very solid it
# cuts. The symptom is a selection that silently vanishes, which is exactly what a selection must never
# do. Stating the state explicitly makes the highlight independent of what else is drawn.
#
# `HIGHLIGHT_COLOR` is opaque, so `translucent` was buying nothing else; behaviour without a solid on
# screen is unchanged.
OVERLAY_GL_OPTIONS = {
    GL.GL_DEPTH_TEST: False,
    GL.GL_BLEND: False,
    GL.GL_CULL_FACE: False,
}
# The carved solid. Depth testing **on**, which is the opposite of everything else here and is the point:
# a heightfield is a surface with a front and a back, and without a depth test the far wall of a pocket
# draws over the near one and the shading reads as noise.
#
# Face culling stays off. The mesh carries explicit outward normals, so lighting does not depend on
# winding, and a closed solid seen from inside a deep pocket is more useful than one that disappears.
SOLID_GL_OPTIONS = {
    GL.GL_DEPTH_TEST: True,
    GL.GL_BLEND: False,
    GL.GL_CULL_FACE: False,
}
# Drawn before everything else, so the lines and overlays — none of which test depth — land on top of it
# rather than being sorted against it.
SOLID_DEPTH_VALUE = -1
# Marker diameter in *pixels*, via `pxMode`. Not millimetres: a world-sized marker vanishes when the
# camera pulls back to fit a large part and swamps the toolpath when it zooms in.
MARKER_SIZE_PX = 12.0
# The legend sits over the toolpath, so it is translucent and dark: it must be readable against the
# near-black background without hiding the geometry it is there to explain.
LEGEND_STYLE = (
    "background: rgba(20, 20, 20, 190); color: #d8d8d8; border-radius: 4px; padding: 6px 8px;"
)
LEGEND_MARGIN_PX = 10
# The legend cannot name the *reason* — it has only the batches, and one program's unverified spans
# may be cutter comp while another's are spindle-synchronized motion, which are wrong about different
# things. So the tooltip says where the reason lives rather than guessing at it; before M16 it
# asserted "the programmed centreline", which is exactly right for G41 and false for G33.
LEGEND_TOOLTIP = (
    "Unverified spans are drawn, but part of what they show cannot be trusted; "
    "the diagnostics list says what."
)
# How far the cursor may travel between press and release and still count as a *click*, in pixels
# (Manhattan distance, which is what Qt's own drag-start heuristic uses). Above it the gesture was a
# camera move and must not also select a segment — see `mouseReleaseEvent`.
CLICK_SLOP_PX = 4.0
# Held with the left button, this pans instead of orbiting. Shift rather than Ctrl: pyqtgraph already
# binds Ctrl+left to a pan in the *camera* plane, which slides the floor grid out of the picture on a
# tilted view, and Ctrl is the modifier every menu shortcut in `main_window` uses.
PAN_MODIFIER = Qt.ShiftModifier
# Every pan gesture pans in the plane of the camera, and every gesture uses the same frame — including
# middle-drag, which pyqtgraph itself does in `view-upright`.
#
# `view` is the one that tracks the cursor exactly. Measured offscreen at elevation 30°, a 20 px drag
# moves a world point 20 px on screen under `view`, and 20 px horizontally but only 10.3 px vertically
# under `view-upright`, which pans along the machine's XY plane and so foreshortens with the tilt. A
# drag that moves the part less than the hand is read as the view fighting back, and it gets worse the
# flatter the view. Cursor-locked is also what "drag" means everywhere else, so it is worth the one
# behaviour change to middle-drag rather than leaving two pans that feel different.
#
# Ctrl+drag is deliberately *not* rerouted: pyqtgraph binds Ctrl+left and Ctrl+middle to pans of its
# own, and those keep working exactly as its documentation describes them.
PAN_FRAME = "view"
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


#: Swatch glyph per kind. A legend whose marks do not resemble what they name has to be decoded twice,
#: so the tool position reads as a dot, geometry as lines, and the solid as a filled block.
_SWATCH_GLYPHS = {
    Swatch.POINT: "&#9679;",
    Swatch.SOLID: "&#9608;&#9608;",
    Swatch.LINE: "&#9473;&#9473;",
}


def _legend_row(entry: LegendEntry) -> str:
    """One row of the key: a swatch in the entry's own colour, then its name and any caveat.

    A note is dimmed and set below its row rather than beside it. It is a sentence, not a label, and
    putting it inline would push the swatch column out of alignment for every other row — but it must
    stay *in* the legend, because a caveat the user has to find elsewhere is one they will not read.
    """
    glyph = _SWATCH_GLYPHS.get(entry.swatch, _SWATCH_GLYPHS[Swatch.LINE])
    row = (
        f'<span style="color: {hex_color(entry.color)}">{glyph}</span>&nbsp;&nbsp;'
        f"{html.escape(entry.label)}"
    )
    if entry.note:
        row += (
            f'<br><span style="color: #a0a0a0; font-size: 90%">'
            f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{html.escape(entry.note)}</span>"
        )
    return row


class ToolpathViewport(GLViewWidget):
    """Draws a ``Simulation`` as a small number of batched line items.

    The camera is ``GLViewWidget``'s, with the **pan gestures widened**. Out of the box pyqtgraph pans
    only on middle-drag or Ctrl+left-drag, and a middle button is exactly what a laptop trackpad — the
    machine a lot of this gets used on — does not have, so zooming in left the user unable to reach the
    part they had zoomed towards. `PAN_MODIFIER`+left-drag and right-drag now pan as well, and
    middle-drag is rerouted through the same code so all three agree; left-drag still orbits and the
    wheel still zooms.

    Clicking emits `segment_picked` with a segment index (T3.3), using the CPU screen-space strategy
    decided in T3.0 — Qt item picking is unavailable with 500k segments in a handful of buffers.
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
        self._solid: gl.GLMeshItem | None = None
        #: The carved field currently on screen, or None. Held so the legend can read its caveats and
        #: `set_store` can drop a solid that belonged to the previous program.
        self.solid_field: SolidField | None = None
        #: Whether the user wants the lines. Independent of the solid: either, both or neither may be on.
        self.toolpath_visible = True
        self.batches: list[Batch] = []
        self.highlighted_segments = 0
        self._store: SegmentStore | None = None
        self._projection: ScreenProjection | None = None
        #: Where the current mouse gesture started, or None between gestures. A click is a press and a
        #: release in nearly the same place; anything further apart moved the camera instead.
        self._press_pos = None
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
        # And for the solid, which is the strongest case of the three: a carved surface from the previous
        # program looks exactly as authoritative as one from this program, and nothing about the picture
        # would say which it is. It is also frame-bound — a cylinder carve is part coordinates — so it
        # cannot survive a switch that changes what is drawn.
        self.clear_solid()
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
            # A rebuild must not un-hide the toolpath. Loading a second program while the lines are
            # switched off would otherwise bring them back with the menu still saying they are hidden.
            item.setVisible(self.toolpath_visible)
            self.addItem(item)
            self._items.append(item)

    def set_toolpath_visible(self, visible: bool) -> None:
        """Show or hide the line batches, leaving everything else alone.

        A visibility flag rather than a rebuild: the batches are unchanged, and dropping the GL items
        would mean re-uploading several megabytes every time the user toggled. Picking deliberately keeps
        working against hidden geometry — the selection highlight ignores depth and stays visible over
        the solid, so clicking a feature on the carved surface still finds the line that cut it, which is
        most of the reason to have the solid and the editor side by side.
        """
        self.toolpath_visible = visible
        for item in self._items:
            item.setVisible(visible)
        # The legend names what is on screen, so hiding the lines has to take their rows with it.
        self._refresh_legend()

    # ------------------------------------------------------------------ carved solid (T12.5)

    def set_solid(self, field: SolidField) -> None:
        """Draw ``field`` as a shaded solid, replacing any solid already on screen.

        Rebuilt wholesale rather than updated through `setData`, for `_rebuild_items`' reason: the vertex
        *count* changes with the grid, and pyqtgraph's mesh item caches derived arrays keyed on the mesh
        data it was given. A partially updated mesh would draw a surface that is half of one program and
        half of another, and that is not a shape anything would flag.

        A field whose frame does not match what the viewport is displaying is refused rather than drawn:
        a cylinder carve lives in part coordinates, and putting it on screen beside a machine-coordinate
        toolpath would place the part somewhere the path never goes.
        """
        if field.part_coordinates != self.part_coordinates:
            raise ValueError(
                f"the solid was carved in {'part' if field.part_coordinates else 'machine'} coordinates "
                f"but the viewport is showing "
                f"{'part' if self.part_coordinates else 'machine'} coordinates"
            )
        mesh = build_mesh(field)
        if mesh is None:
            self.clear_solid()
            return

        self._remove_solid()
        self._solid = gl.GLMeshItem(
            vertexes=mesh.vertices,
            faces=mesh.faces,
            vertexColors=mesh.colors,
            # `shader=None` because the lighting is already in the colours. pyqtgraph's `shaded` lights
            # from eye space and leaves any face turned toward the camera at ambient — which is the
            # machined surface, the one face this whole view exists to show. See `solid_mesh`.
            shader=None,
            # Nothing downstream reads normals now, and computing them here would be `MeshData` doing in
            # Python what `solid_mesh` already did in numpy.
            computeNormals=False,
            smooth=True,
            glOptions=SOLID_GL_OPTIONS,
        )
        self._solid.setDepthValue(SOLID_DEPTH_VALUE)
        self.addItem(self._solid)
        self.solid_field = field
        self._show_grid(False)
        self._refresh_legend()

    def clear_solid(self) -> None:
        """Remove the solid, if there is one. Safe to call when there is not."""
        self._remove_solid()
        self.solid_field = None
        self._show_grid(True)
        self._refresh_legend()

    def _show_grid(self, visible: bool) -> None:
        """The floor grid is hidden whenever a solid is on screen.

        Not a style preference. The grid is a **plane at Z = 0**, and a stock top at Z = 0 is the ordinary
        convention — `bamse-rotary.toml` says so in as many words — so the grid lies exactly in the
        blank's top face and slices through the part everywhere the program cut below it. Inside a pocket
        the grid is genuinely in front of the machined floor and is drawn over it; on an uncut face the two
        are coincident and z-fight. Either way it lands on top of the one surface this view exists to show.

        A wireframe path floating in space needs a ground reference. A solid *is* one — it has a visible
        underside and a silhouette — so nothing is lost by taking the grid away while it is up.
        """
        if self._grid is not None:
            self._grid.setVisible(visible)

    def _remove_solid(self) -> None:
        if self._solid is not None:
            self.removeItem(self._solid)
            self._solid = None

    # ------------------------------------------------------------------ selection highlight

    def set_highlight(self, store: SegmentStore, mask: np.ndarray | None) -> None:
        """Draw ``mask``'s segments on top of everything, or clear the highlight when it is empty.

        Kept as **one long-lived item updated with `setData`**, unlike the toolpath batches which are
        rebuilt wholesale. The reasoning that made rebuilding right there does not apply here: there is
        exactly one highlight item and its existence never depends on the data, so no stale item can
        survive a change. It also follows the text cursor, so it updates far more often than a load does.

        It lands on top because it does not test depth at all — `OVERLAY_GL_OPTIONS`. Until M12 that was
        instead a consequence of nothing else writing depth: the batches disable the test, a disabled
        test writes nothing, so `"translucent"` here tested against an empty buffer and always won. The
        solid view ended that, because a mesh does write depth, and a selection inside the material would
        have been swallowed by the solid it cuts. A selected segment buried behind other geometry must
        stay visible, or the user reads it as "this line draws nothing" — the opposite of what a
        selection is for — and the failure would appear as nothing at all rather than as an error.
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
                pos=vertices,
                color=HIGHLIGHT_COLOR,
                width=LINE_WIDTH,
                mode="lines",
                antialias=False,
                # Depth testing off explicitly, rather than borrowing an empty depth buffer — see
                # `OVERLAY_GL_OPTIONS`. With a solid on screen the buffer is no longer empty.
                glOptions=OVERLAY_GL_OPTIONS,
            )
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
            # Hidden lines get no rows. The legend names what is *on screen*, and a key listing colours
            # the user has just switched off is the same failure as one listing colours a program does
            # not contain — it teaches the reader that the legend is not to be trusted.
            self.batches if self.toolpath_visible else (),
            highlighted=self.highlighted_segments > 0,
            marker=marker_shown,
            solid=None if self.solid_field is None else self.solid_field.notes,
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

    def mousePressEvent(self, event) -> None:
        """Remember where the gesture started, so release can tell a click from a drag."""
        super().mousePressEvent(event)
        self._press_pos = event.position()

    def mouseMoveEvent(self, event) -> None:
        """Pan on the widened gestures; leave orbit, and pyqtgraph's own pans, to the base class.

        The delta is taken and `mousePos` advanced here rather than after delegating, because
        `GLViewWidget.mouseMoveEvent` consumes `mousePos` for the gesture it handles — letting it run
        as well would apply the same drag twice.
        """
        position = event.position()
        if self._within_click_slop(position):
            return
        if not self._is_pan_drag(event):
            super().mouseMoveEvent(event)
            return
        # A drag can begin without this widget having seen the press (a grab handed over mid-gesture),
        # in which case the first delta is unknowable rather than zero.
        previous = getattr(self, "mousePos", position)
        self.mousePos = position
        delta = position - previous
        self.pan(delta.x(), delta.y(), 0, relative=PAN_FRAME)

    def _within_click_slop(self, position) -> bool:
        """Whether the gesture so far is still small enough to be a click.

        Inside the slop the camera does not move **at all**. `GLViewWidget` orbits a *degree per pixel*,
        so without this a two-pixel tremor while clicking swings the view several degrees — and then the
        segment the user aimed at is no longer under the cursor when the release picks, which reads as
        clicking simply not working.

        `mousePos` is deliberately left at the press position while this returns True, so the delta is
        not thrown away: the first move that escapes the slop applies the whole travel since the press
        and the drag stays locked to the cursor from where it started.
        """
        press = self._press_pos
        return press is not None and (position - press).manhattanLength() <= CLICK_SLOP_PX

    @staticmethod
    def _is_pan_drag(event) -> bool:
        """Whether this drag is one of the pan gestures handled here.

        Middle-drag is included even though `GLViewWidget` already pans on it, so that every pan uses
        one frame — see `PAN_FRAME`. Anything with Ctrl held is excluded and falls through to
        pyqtgraph's own Ctrl+left and Ctrl+middle pans.
        """
        if event.modifiers() & Qt.ControlModifier:
            return False
        buttons = event.buttons()
        if buttons & (Qt.RightButton | Qt.MiddleButton):
            return True
        return bool(buttons & Qt.LeftButton) and bool(event.modifiers() & PAN_MODIFIER)

    def mouseReleaseEvent(self, event) -> None:
        """A left click *without a drag* picks a segment.

        Bound to release rather than press because left-drag orbits: picking on press would fire on
        every orbit and jump the editor around while the user is just looking at the part. Release
        alone is not enough, though — an orbit or a Shift-pan also ends in a left release, and picking
        there scrolls the editor to whatever segment the camera move happened to leave under the
        cursor. So the release must land within `CLICK_SLOP_PX` of the press to count as a click.
        """
        press, self._press_pos = self._press_pos, None
        super().mouseReleaseEvent(event)
        if event.button() != Qt.LeftButton or press is None:
            return
        if (event.position() - press).manhattanLength() > CLICK_SLOP_PX:
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
