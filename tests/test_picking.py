"""Picking tests (T3.3). No Qt — `gui/picking.py` takes a matrix, not a widget.

Two properties carry the weight.

**The right segment, not the nearest midpoint.** That is what D4's KD-tree proposal got wrong and what
T3.0 measured: clicking 1 px from the far end of a long rapid must select *that rapid*, not whatever
happens to sit near its middle. Several tests here use geometry where the two answers differ, because a
test on geometry where they agree proves nothing.

**A stale cache is impossible, not merely unlikely.** `ScreenProjection` records the matrix it was built
from, so `matches()` is exact. This is the failure mode worth engineering against: a projection left over
from before an orbit returns a confidently wrong segment, and nothing in the picture hints at it.
"""

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.picking import PICK_RADIUS_PX, pick, project_store
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind, SegmentBuilder
from foursight.sim.simulator import simulate_text

WIDTH, HEIGHT = 1280, 800


def orthographic(scale: float = 8.0, offset=(0.0, 0.0)) -> np.ndarray:
    """A simple axis-aligned matrix, so expected pixel positions are computable by hand.

    Deliberately not a perspective camera: the arithmetic of *this* matrix is not what is under test, and
    a hand-checkable one makes the assertions about picking rather than about projection.
    """
    mvp = np.eye(4)
    mvp[0, 0] = scale / (WIDTH / 2)
    mvp[1, 1] = scale / (HEIGHT / 2)
    mvp[0, 3] = offset[0]
    mvp[1, 3] = offset[1]
    return mvp


def store_of(*polylines) -> "SegmentBuilder":
    builder = SegmentBuilder()
    for line_no, points in enumerate(polylines, start=1):
        builder.add_polyline(np.array(points, dtype=np.float64), Kind.FEED, line_no)
    return builder.finalize()


def projected(store, mvp=None):
    return project_store(store, mvp if mvp is not None else orthographic(), WIDTH, HEIGHT)


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


# --------------------------------------------------------------------------- the segment, not a midpoint


def test_clicking_the_end_of_a_long_segment_selects_that_segment() -> None:
    """The case that rules out a midpoint KD-tree, and the reason D4 was reopened in T3.0.

    A long segment on source line 1 and a short one on line 2 placed near the long one's far end. The
    nearest *midpoint* is the short segment's; the nearest *segment* is the long one.
    """
    store = store_of(
        [[-40.0, 0, 0], [40.0, 0, 0]],  # line 1, long
        [[35.0, 6, 0], [39.0, 6, 0]],  # line 2, short, near the long one's far end
    )
    projection = projected(store)
    cursor = tuple(projection.b[0] + np.array([0.0, 1.0]))  # 1 px from the long segment's end

    index = pick(projection, cursor)
    assert index is not None
    assert store.line[index] == 1, "picked the nearby short segment instead of the one clicked"

    midpoints = (projection.a + projection.b) / 2
    nearest_midpoint = int(np.argmin(np.linalg.norm(midpoints - np.asarray(cursor), axis=1)))
    assert store.line[nearest_midpoint] == 2, "the test geometry no longer distinguishes the two"


def test_clicking_the_middle_of_a_segment_also_works() -> None:
    store = store_of([[-40.0, 0, 0], [40.0, 0, 0]])
    projection = projected(store)
    index = pick(projection, tuple((projection.a[0] + projection.b[0]) / 2))
    assert index == 0


def test_a_click_beyond_a_segment_end_does_not_project_past_it() -> None:
    """Without the clamp on `t`, the infinite *line* would be picked from far off its end."""
    store = store_of([[0.0, 0, 0], [10.0, 0, 0]])
    projection = projected(store)
    beyond = tuple(projection.b[0] + np.array([200.0, 0.0]))
    assert pick(projection, beyond) is None


def test_a_click_in_empty_space_picks_nothing() -> None:
    """A miss must return None, so the caller can leave the selection alone rather than clearing it."""
    store = store_of([[-40.0, 0, 0], [40.0, 0, 0]])
    projection = projected(store)
    assert pick(projection, (10.0, 10.0)) is None


def test_the_pick_radius_is_respected() -> None:
    store = store_of([[-40.0, 0, 0], [40.0, 0, 0]])
    projection = projected(store)
    on_line = (projection.a[0] + projection.b[0]) / 2
    assert pick(projection, tuple(on_line + np.array([0.0, PICK_RADIUS_PX - 1]))) == 0
    assert pick(projection, tuple(on_line + np.array([0.0, PICK_RADIUS_PX + 2]))) is None


def test_a_zero_length_segment_is_pickable_at_its_point() -> None:
    """A dwell or a stationary block has coincident endpoints; the clamp must not divide by zero."""
    store = store_of([[5.0, 5.0, 0.0], [5.0, 5.0, 0.0]])
    projection = projected(store)
    assert pick(projection, tuple(projection.a[0])) == 0


# --------------------------------------------------------------------------- depth breaks ties only


def test_the_front_most_of_two_overlapping_segments_wins() -> None:
    """Two segments at the same screen position, different depths. The nearer one is picked."""
    store = store_of(
        [[-20.0, 0.0, -50.0], [20.0, 0.0, -50.0]],
        [[-20.0, 0.0, 50.0], [20.0, 0.0, 50.0]],
    )
    projection = projected(store)
    index = pick(projection, tuple((projection.a[0] + projection.b[0]) / 2))
    assert index is not None
    assert projection.depth[index] == projection.depth.min()


def test_segments_behind_the_camera_are_never_picked() -> None:
    """Their projected coordinates are a reflection through the origin — confident nonsense.

    Uses a perspective matrix, because an orthographic one has no behind-the-camera case to speak of.
    """
    store = store_of([[-20.0, 0, 0], [20.0, 0, 0]])
    perspective = np.array(
        [[1.7, 0, 0, 0], [0, 2.4, 0, 0], [0, 0, -1.002, -2.002], [0, 0, -1, 0]], dtype=np.float64
    )
    # Camera at the origin looking down -z, geometry at +z, i.e. behind it.
    behind = perspective.copy()
    behind[2, 3] += 100.0
    projection = project_store(store, behind, WIDTH, HEIGHT)
    if projection.behind.all():
        assert pick(projection, (WIDTH / 2, HEIGHT / 2)) is None
    else:
        pytest.skip("the constructed matrix did not put the geometry behind the camera")


# --------------------------------------------------------------------------- the cache cannot go stale


def test_the_projection_matches_the_matrix_it_was_built_from() -> None:
    mvp = orthographic()
    projection = projected(store_of([[0.0, 0, 0], [10.0, 0, 0]]), mvp)
    assert projection.matches(mvp, WIDTH, HEIGHT, 1)


def test_a_moved_camera_invalidates_the_projection() -> None:
    """The failure this design exists to prevent: a pick against a pre-orbit projection.

    It returns a confidently wrong segment and nothing in the picture hints at it, which is why the cache
    is keyed on the matrix rather than on a dirty flag someone has to remember to set.
    """
    store = store_of([[0.0, 0, 0], [10.0, 0, 0]])
    projection = projected(store, orthographic())
    assert not projection.matches(orthographic(offset=(0.3, 0.0)), WIDTH, HEIGHT, 1)
    assert not projection.matches(orthographic(scale=16.0), WIDTH, HEIGHT, 1)


def test_a_resized_viewport_invalidates_the_projection() -> None:
    """Pixel coordinates depend on the viewport, so the same matrix at a new size is a different answer."""
    projection = projected(store_of([[0.0, 0, 0], [10.0, 0, 0]]))
    assert not projection.matches(orthographic(), WIDTH + 1, HEIGHT, 1)
    assert not projection.matches(orthographic(), WIDTH, HEIGHT // 2, 1)


def test_a_different_program_invalidates_the_projection() -> None:
    """Same camera, new geometry. The arrays are indexed by segment and would be the wrong length."""
    projection = projected(store_of([[0.0, 0, 0], [10.0, 0, 0]]))
    assert not projection.matches(orthographic(), WIDTH, HEIGHT, 999)


def test_the_projection_holds_its_own_copy_of_the_matrix() -> None:
    """The renderer rebuilds its matrix in place, so a reference would compare equal to itself forever."""
    mvp = orthographic()
    projection = projected(store_of([[0.0, 0, 0], [10.0, 0, 0]]), mvp)
    mvp[0, 3] = 99.0
    assert not projection.matches(mvp, WIDTH, HEIGHT, 1)


# --------------------------------------------------------------------------- shape and scale


def test_an_empty_store_picks_nothing() -> None:
    from foursight.sim.segments import SegmentStore

    projection = project_store(SegmentStore.empty(), orthographic(), WIDTH, HEIGHT)
    assert pick(projection, (0.0, 0.0)) is None


def test_the_projection_has_one_entry_per_segment(profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    projection = projected(sim.store)
    assert projection.a.shape == (len(sim.store), 2)
    assert projection.b.shape == (len(sim.store), 2)
    assert projection.depth.shape == (len(sim.store),)
    assert projection.behind.shape == (len(sim.store),)


def test_picking_never_returns_an_out_of_range_index(profile) -> None:
    """The returned index is used to look up `store.line`, so it must be a valid segment."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    projection = projected(sim.store)
    for x in range(0, WIDTH, 137):
        for y in range(0, HEIGHT, 149):
            index = pick(projection, (float(x), float(y)))
            assert index is None or 0 <= index < len(sim.store)


def test_projecting_does_not_mutate_the_store(profile) -> None:
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    before = sim.store.lin.copy()
    projected(sim.store)
    assert np.array_equal(sim.store.lin, before)
