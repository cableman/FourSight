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
from foursight.gui.viewport3d import ToolpathViewport  # noqa: E402
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
