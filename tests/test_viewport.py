"""Viewport tests (T2.6). Runs headless via Qt's ``offscreen`` platform; skips without the `[gui]` extra.

PLAN.md says the GUI is verified by a manual script per milestone, and it is — but "thin" does not
mean "unchecked", and one class of bug here is invisible to the eye in a way a test catches instantly:
**the viewport drawing nothing, or drawing the previous program.**

That is not hypothetical. The first version of `_rebuild_items` called `clear_toolpath`, which reset
`self.batches` to `[]` before the loop that reads it, so no GL items were ever created and every
program rendered as an empty scene. No exception, no warning — just an empty viewport that looks like
a program with no geometry. `test_one_gl_item_is_created_per_batch` is that bug's regression test.

The `offscreen` platform cannot create a GL context, so nothing here asserts on pixels — `paintGL`
never runs. What it *can* do is construct the widget, add items, and read back the scene graph and
camera, which covers the batch-to-item wiring and the camera fit. Pixels are the manual script's job
(T2.12), and the fps measurement is the T0.7 spike's.
"""

import os

import numpy as np
import pytest

# Must be set before any QApplication exists, so it goes at import time rather than in a fixture.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
pytest.importorskip("pyqtgraph", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH, fixture_text  # noqa: E402
from foursight.gui.playback import marker_point  # noqa: E402
from foursight.gui.timeline import build_timeline  # noqa: E402
from foursight.gui.viewport3d import MARKER_COLOR, ToolpathViewport  # noqa: E402
from foursight.machine.profile import load_profile  # noqa: E402
from foursight.sim.segments import SegmentStore  # noqa: E402
from foursight.sim.simulator import simulate_text  # noqa: E402

GRID_ITEMS = 1  # the floor grid, which every scene keeps


@pytest.fixture(scope="session")
def qt_app():
    """One ``QApplication`` for the session; Qt permits no more."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def viewport(qt_app):
    """A fresh viewport, or a skip on a platform that cannot construct one.

    Deliberately tolerant: a Qt platform plugin that refuses even offscreen widget construction is a
    CI-environment problem, and turning it into a red build would say nothing about this code. A
    failure *after* construction is a real failure and is not caught.
    """
    try:
        widget = ToolpathViewport()
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct a GL widget on this platform: {error}")
    widget.resize(640, 480)
    return widget


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def simulation(text: str, profile):
    sim, _ = simulate_text(text, profile)
    return sim


# --------------------------------------------------------------------------- batch → item wiring


def test_one_gl_item_is_created_per_batch(viewport, profile) -> None:
    """The regression test for an empty viewport. See the module docstring.

    Asserted against `viewport.items` — pyqtgraph's own scene list — rather than the private
    bookkeeping list, because the scene list is what actually gets drawn.
    """
    sim = simulation(fixture_text("cutter_comp_span.nc"), profile)
    viewport.set_simulation(sim)
    assert len(viewport.batches) == 3, "rapid, feed, and feed (unverified)"
    assert len(viewport.items) == GRID_ITEMS + len(viewport.batches)


def test_all_the_geometry_reaches_the_scene(viewport, profile) -> None:
    """Segment counts must survive the trip into GL items, not just into batches."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    drawn = sum(batch.segments for batch in viewport.batches)
    assert drawn == len(sim.store)
    uploaded = sum(
        item.pos.shape[0] for item in viewport.items if getattr(item, "pos", None) is not None
    )
    assert uploaded == 2 * len(sim.store), "vertices uploaded do not match the simulated geometry"


def test_reloading_a_smaller_program_leaves_no_stale_items(viewport, profile) -> None:
    """The other half of the same failure: geometry from the *previous* file still on screen.

    A program with three batches followed by one with a single batch must end with one, or the viewer
    shows a toolpath that is not the file they opened.
    """
    viewport.set_simulation(simulation(fixture_text("cutter_comp_span.nc"), profile))
    assert len(viewport.items) == GRID_ITEMS + 3
    viewport.set_simulation(simulation("G21 G94 G90\nG1 X10 Y10 F600\n", profile))
    assert len(viewport.batches) == 1
    assert len(viewport.items) == GRID_ITEMS + 1


def test_clearing_keeps_the_grid_and_drops_the_toolpath(viewport, profile) -> None:
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    viewport.clear_toolpath()
    assert viewport.batches == []
    assert len(viewport.items) == GRID_ITEMS


def test_an_empty_program_draws_nothing_without_failing(viewport, profile) -> None:
    """A fully suppressed program must leave an empty scene, not an empty GL buffer or a crash."""
    viewport.set_simulation(simulation("(comment only)\n", profile))
    assert viewport.batches == []
    assert len(viewport.items) == GRID_ITEMS


def test_a_bare_store_can_be_drawn_without_span_information(viewport) -> None:
    """`set_store` is the lower-level path; it must not require a `Simulation`."""
    viewport.set_store(SegmentStore.empty())
    assert viewport.batches == []


# --------------------------------------------------------------------------- the untrusted tier


def test_the_untrusted_mask_reaches_the_viewport(viewport, profile) -> None:
    """`set_simulation` must pass the mask through, or comp spans draw as ordinary feeds.

    The mask is asserted non-empty first: if the fixture stopped producing a drawn-but-unverified
    span, this test would otherwise pass while checking nothing.
    """
    sim = simulation(fixture_text("cutter_comp_span.nc"), profile)
    assert sim.unverified_mask().any(), "the fixture no longer produces an unverified span"
    viewport.set_simulation(sim)
    untrusted = [batch for batch in viewport.batches if not batch.trusted]
    assert len(untrusted) == 1
    # Compared against the trusted batch of the **same kind**. Comparing against just any trusted
    # batch is what let this test pass while the tier was collapsed: the untrusted span is a feed, the
    # first trusted batch is a rapid, so green != red held even when untrusted geometry was being
    # styled exactly like trusted geometry.
    same_kind = [
        batch for batch in viewport.batches if batch.trusted and batch.kind == untrusted[0].kind
    ]
    assert same_kind, (
        "the fixture no longer has a trusted batch of the same kind to compare against"
    )
    assert untrusted[0].color != same_kind[0].color


def test_every_item_draws_in_line_mode_at_width_one(viewport, profile) -> None:
    """Colour carries all meaning: pyqtgraph skips `glLineWidth` on core forward-compatible profiles,
    so anything encoded in thickness would silently vanish there."""
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    for item in viewport.items:
        if getattr(item, "pos", None) is None:
            continue
        assert item.mode == "lines"
        assert item.width == 1.0


# --------------------------------------------------------------------------- the camera


def test_the_camera_is_fitted_to_the_geometry(viewport, profile) -> None:
    """A camera left at its default distance frames a small program as a dot and a large one not at all."""
    small = simulation("G21 G94 G90\nG1 X5 Y5 F600\n", profile)
    large = simulation("G21 G94 G90\nG1 X400 Y400 F600\n", profile)
    viewport.set_simulation(small)
    near = viewport.opts["distance"]
    viewport.set_simulation(large)
    assert viewport.opts["distance"] > near, "the camera distance did not follow the program size"


def test_the_camera_centres_on_the_geometry_not_the_origin(viewport, profile) -> None:
    """A program milled far from the origin would otherwise sit off-screen."""
    sim = simulation("G21 G94 G90\nG0 X300 Y300\nG1 X310 Y310 F600\n", profile)
    viewport.set_simulation(sim)
    centre = viewport.opts["center"]
    assert centre.x() > 100.0 and centre.y() > 100.0


def test_a_degenerate_program_does_not_collapse_the_camera(viewport, profile) -> None:
    """A single-axis move has zero extent in Y and Z; a naive fit would divide the distance to zero."""
    sim = simulation("G21 G94 G90\nG1 X10 F600\n", profile)
    viewport.set_simulation(sim)
    assert viewport.opts["distance"] > 0.0


def test_fitting_an_empty_store_falls_back_rather_than_dividing_by_zero(viewport) -> None:
    viewport.fit_to(SegmentStore.empty())
    assert viewport.opts["distance"] > 0.0


# --------------------------------------------------------------------------- invariants


def test_drawing_does_not_mutate_the_store(viewport, profile) -> None:
    """`lin` is machine coordinates and the verifier reads it. The renderer is a consumer only."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    before = sim.store.lin.copy()
    viewport.set_simulation(sim)
    assert np.array_equal(sim.store.lin, before)


def test_the_batch_count_never_exceeds_the_plan_budget(viewport, profile) -> None:
    """PLAN.md § Performance: ≤ 10 buffers, never one draw call per move."""
    for name in ("baseline_4axis.nc", "arc_helical.nc", "cutter_comp_span.nc"):
        viewport.set_simulation(simulation(fixture_text(name), profile))
        assert len(viewport.items) - GRID_ITEMS <= 10


# --------------------------------------------------------------------------- selection highlight (T3.2)


def test_a_highlight_adds_exactly_one_item(viewport, profile) -> None:
    """One long-lived item updated via `setData`, not a per-selection rebuild.

    Unlike the toolpath batches, the highlight's existence never depends on the data, so there is no
    stale-item risk — and it follows the text cursor, so it updates far more often than a load does.
    """
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    before = len(viewport.items)
    mask = sim.store.line == sim.store.line[0]
    viewport.set_highlight(sim.store, mask)
    assert len(viewport.items) == before + 1
    # A second, different selection must reuse the same item.
    viewport.set_highlight(sim.store, sim.store.line == sim.store.line[-1])
    assert len(viewport.items) == before + 1


def test_the_highlight_uploads_two_vertices_per_selected_segment(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    mask = sim.store.line == sim.store.line[0]
    viewport.set_highlight(sim.store, mask)
    assert viewport.highlighted_segments == int(mask.sum())
    assert viewport._highlight.pos.shape[0] == 2 * int(mask.sum())


def test_an_empty_mask_hides_the_highlight_rather_than_uploading_nothing(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.set_highlight(sim.store, sim.store.line == sim.store.line[0])
    viewport.set_highlight(sim.store, np.zeros(len(sim.store), dtype=bool))
    assert viewport.highlighted_segments == 0
    assert viewport._highlight.visible() is False


def test_the_highlight_colour_differs_from_every_batch_colour(viewport, profile) -> None:
    """The highlight is view state, not a property of the toolpath, so it must not read as a motion type."""
    from foursight.gui.viewport3d import HIGHLIGHT_COLOR

    sim = simulation(fixture_text("cutter_comp_span.nc"), profile)
    viewport.set_simulation(sim)
    assert HIGHLIGHT_COLOR not in {batch.color for batch in viewport.batches}


def test_loading_a_new_program_clears_the_highlight(viewport, profile) -> None:
    """A mask indexes the *previous* store, so keeping it would highlight arbitrary new segments."""
    first = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(first)
    viewport.set_highlight(first.store, first.store.line == first.store.line[0])
    assert viewport.highlighted_segments > 0

    viewport.set_simulation(simulation("G21 G94 G90\nG1 X5 F600\n", profile))
    assert viewport.highlighted_segments == 0


def test_a_wrong_length_highlight_mask_is_refused(viewport, profile) -> None:
    """Numpy would broadcast a short mask and highlight the wrong segments."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    with pytest.raises(ValueError, match="one per segment"):
        viewport.set_highlight(sim.store, np.ones(3, dtype=bool))


def test_highlighting_does_not_mutate_the_store(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    before = sim.store.lin.copy()
    viewport.set_highlight(sim.store, sim.store.line == sim.store.line[0])
    assert np.array_equal(sim.store.lin, before)


def test_the_highlight_stays_inside_the_plan_buffer_budget(viewport, profile) -> None:
    """PLAN allows <= 10 buffers. Four batches, one highlight and one playback marker is six."""
    sim = simulation(fixture_text("cutter_comp_span.nc"), profile)
    viewport.set_simulation(sim)
    viewport.set_highlight(sim.store, sim.store.line == sim.store.line[0])
    viewport.set_marker(sim.store, build_timeline(sim.store), 0.0)
    assert len(viewport.items) - GRID_ITEMS <= 10


# --------------------------------------------------------------------------- picking (T3.3)


def test_the_projection_is_cached_between_picks(viewport, profile) -> None:
    """27 ms warm versus 94 ms cold at 500k, so reusing it is the difference between usable and not."""
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    first = viewport.projection()
    assert first is not None
    assert viewport.projection() is first


def test_moving_the_camera_invalidates_the_cached_projection(viewport, profile) -> None:
    """The failure the matrix-keyed cache exists to prevent, exercised through the real widget.

    A pick against a pre-orbit projection returns a confidently wrong segment with nothing in the picture
    to suggest it. This asserts the *widget* notices, not just that `matches()` can tell.
    """
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    before = viewport.projection()
    viewport.setCameraPosition(azimuth=viewport.opts["azimuth"] + 45)
    after = viewport.projection()
    assert after is not before, "the projection survived a camera move"
    assert not np.array_equal(before.mvp, after.mvp)


def test_loading_a_new_program_invalidates_the_cached_projection(viewport, profile) -> None:
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    first = viewport.projection()
    viewport.set_simulation(simulation(fixture_text("arc_helical.nc"), profile))
    second = viewport.projection()
    assert second is not first
    assert second.segments != first.segments


def test_there_is_no_projection_without_geometry(viewport) -> None:
    assert viewport.projection() is None
    assert viewport.pick_at(10.0, 10.0) is None


def test_picking_a_visible_segment_returns_a_valid_index(viewport, profile) -> None:
    """Sweeps the viewport rather than guessing one position, since the camera framing is not fixed here."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    hits = [
        viewport.pick_at(float(x), float(y))
        for x in range(0, viewport.width(), 17)
        for y in range(0, viewport.height(), 19)
    ]
    found = [index for index in hits if index is not None]
    assert found, "nothing was pickable anywhere in the viewport"
    assert all(0 <= index < len(sim.store) for index in found)


def test_the_pick_matrix_is_a_four_by_four(viewport, profile) -> None:
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    assert viewport.pick_matrix().shape == (4, 4)


def test_the_highlight_is_drawn_in_the_coordinates_on_screen(viewport, profile) -> None:
    """Asserted on the uploaded **vertices**, not the segment count.

    A count-only check passes even when the highlight is drawn in machine coordinates while part
    coordinates are displayed — a highlight floating away from the toolpath it belongs to. Mutation
    testing found exactly that hole: forcing `store.lin` here changed no test result.
    """
    from foursight.machine.kinematics import apply_display_transform

    sim = simulation("G21 G90 G94\nG0 Y25 Z0\nG1 X40 A180 F600\n", profile)
    store = sim.store
    apply_display_transform(store, profile.kinematics)
    mask = store.line == 3
    assert mask.any(), "the wrapping move produced no segments"

    viewport.set_simulation(sim, use_part_coordinates=True)
    viewport.set_highlight(store, mask)
    uploaded = np.asarray(viewport._highlight.pos, dtype=np.float64)

    expected_part = store.lin_part[mask].reshape(-1, 3)
    expected_machine = store.lin[mask].reshape(-1, 3)
    assert not np.allclose(expected_part, expected_machine), (
        "this program does not distinguish the two frames, so the test proves nothing"
    )
    assert np.allclose(uploaded, expected_part, atol=1e-3)


def test_the_highlight_follows_a_switch_back_to_machine_coordinates(viewport, profile) -> None:
    """Both directions: switching off must redraw in machine coordinates, not leave the wrapped path."""
    from foursight.machine.kinematics import apply_display_transform

    sim = simulation("G21 G90 G94\nG0 Y25 Z0\nG1 X40 A180 F600\n", profile)
    store = sim.store
    apply_display_transform(store, profile.kinematics)
    mask = store.line == 3

    viewport.set_simulation(sim, use_part_coordinates=True)
    viewport.set_highlight(store, mask)
    viewport.set_simulation(sim, use_part_coordinates=False)
    viewport.set_highlight(store, mask)
    uploaded = np.asarray(viewport._highlight.pos, dtype=np.float64)
    assert np.allclose(uploaded, store.lin[mask].reshape(-1, 3), atol=1e-3)


def test_highlighting_part_coordinates_without_a_transform_is_refused(viewport, profile) -> None:
    """Falling back to `lin` would draw the selection in a frame the toolpath is not in."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.part_coordinates = True  # as if a toggle had been applied without transforming
    with pytest.raises(ValueError, match="no display transform"):
        viewport.set_highlight(sim.store, sim.store.line == sim.store.line[0])


# --------------------------------------------------------------------------- playback marker (T10.3)


def test_a_marker_adds_exactly_one_item_and_reuses_it(viewport, profile) -> None:
    """One long-lived item, like the highlight: it moves once a frame, so rebuilding is not an option."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    timeline = build_timeline(sim.store)
    before = len(viewport.items)

    viewport.set_marker(sim.store, timeline, 0.0)
    assert len(viewport.items) == before + 1
    first = viewport._marker

    viewport.set_marker(sim.store, timeline, timeline.total / 2.0)
    assert len(viewport.items) == before + 1
    assert viewport._marker is first, "the marker was rebuilt rather than moved"


def test_clearing_a_marker_that_was_never_drawn_adds_nothing(viewport, profile) -> None:
    """`set_store` clears on every load, and the scene must stay grid-plus-batches until playback
    actually starts — which is what the item-count regression tests above count on."""
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.clear_marker()
    assert viewport._marker is None
    assert len(viewport.items) == GRID_ITEMS + len(viewport.batches)


def test_the_marker_is_drawn_where_the_interpolation_says(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    timeline = build_timeline(sim.store)
    seconds = timeline.total / 3.0

    viewport.set_marker(sim.store, timeline, seconds)
    uploaded = np.asarray(viewport._marker.pos, dtype=np.float64)
    assert uploaded.shape == (1, 3), "the marker is one point, not a polyline"
    assert np.allclose(uploaded[0], marker_point(sim.store, timeline, seconds), atol=1e-3)


def test_an_empty_program_hides_the_marker_rather_than_uploading_nothing(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.set_marker(sim.store, build_timeline(sim.store), 0.0)

    empty = SegmentStore.empty()
    viewport.set_marker(empty, build_timeline(empty), 0.0)
    assert viewport._marker.visible() is False


def test_loading_a_new_program_hides_the_marker(viewport, profile) -> None:
    """A playback position indexes the *previous* program's timeline, so it cannot survive a load."""
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_marker(sim.store, build_timeline(sim.store), 1.0)
    assert viewport._marker.visible() is True

    viewport.set_simulation(simulation("G21 G94 G90\nG1 X10 Y10 F600\n", profile))
    assert viewport._marker.visible() is False


def test_the_marker_is_drawn_in_the_coordinates_on_screen(viewport, profile) -> None:
    """A marker in machine coordinates over a part-coordinate toolpath floats beside the path."""
    from foursight.machine.kinematics import apply_display_transform

    sim = simulation("G21 G90 G94\nG0 Y25 Z0\nG1 X40 A180 F600\n", profile)
    store = sim.store
    apply_display_transform(store, profile.kinematics)
    timeline = build_timeline(store)
    seconds = timeline.total / 2.0

    viewport.set_simulation(sim, use_part_coordinates=True)
    viewport.set_marker(store, timeline, seconds)
    uploaded = np.asarray(viewport._marker.pos, dtype=np.float64)[0]

    expected_part = marker_point(store, timeline, seconds, part_coordinates=True)
    expected_machine = marker_point(store, timeline, seconds)
    assert not np.allclose(expected_part, expected_machine), (
        "this program does not distinguish the two frames, so the test proves nothing"
    )
    assert np.allclose(uploaded, expected_part, atol=1e-3)


def test_marking_part_coordinates_without_a_transform_is_refused(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.part_coordinates = True  # as if a toggle had been applied without transforming
    with pytest.raises(ValueError, match="no display transform"):
        viewport.set_marker(sim.store, build_timeline(sim.store), 0.0)


def test_the_marker_is_not_hidden_behind_the_toolpath(viewport, profile) -> None:
    """`additive` is the GL mode that turns the depth test *off* — `translucent` leaves it on. The
    tool is often down inside the work, and a marker that disappears into the stock reads as the
    program having finished rather than as a marker being occluded."""
    from OpenGL import GL

    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    viewport.set_marker(sim.store, build_timeline(sim.store), 0.0)

    options = viewport._marker.__dict__["_GLGraphicsItem__glOpts"]
    assert options[GL.GL_DEPTH_TEST] is False
    assert viewport._marker.depthValue() > 1, "the marker must sort above the selection highlight"


def test_the_marker_colour_differs_from_every_batch_colour_and_the_highlight(
    viewport, profile
) -> None:
    """It is view state, not a motion type — it must not read as a rapid, a feed or a selection."""
    from foursight.gui.viewport3d import HIGHLIGHT_COLOR

    sim = simulation(fixture_text("cutter_comp_span.nc"), profile)
    viewport.set_simulation(sim)
    for batch in viewport.batches:
        assert batch.color != MARKER_COLOR
    assert HIGHLIGHT_COLOR != MARKER_COLOR
