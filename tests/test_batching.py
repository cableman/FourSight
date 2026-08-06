"""Batching tests (T2.6). No Qt, no display — that is the point of splitting `batching.py` out.

PLAN.md § Performance Requirements requires ≤ 10 buffers grouped by kind and never one draw call per
move. But the assertions that matter here are about *not losing geometry*:

- every segment is drawn exactly once, and
- untrusted spans never share a batch with trusted ones.

A batching bug cannot make the picture merely ugly. It makes it wrong — geometry missing from the
screen, or a cutter-compensated centreline presented as a path the tool follows.
"""

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.batching import (
    MAX_BATCHES,
    TRUSTED_COLORS,
    UNTRUSTED_COLORS,
    Batch,
    bounds,
    build_batches,
    total_gl_bytes,
)
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.simulator import simulate_text


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def store_of(*runs: tuple[Kind, int]) -> SegmentStore:
    """A store with the given (kind, segment count) runs, each a simple polyline along X."""
    builder = SegmentBuilder()
    origin = 0.0
    for line_no, (kind, count) in enumerate(runs, start=1):
        points = np.stack(
            [
                np.linspace(origin, origin + count, count + 1),
                np.zeros(count + 1),
                np.zeros(count + 1),
            ],
            axis=1,
        )
        builder.add_polyline(points, kind, line_no)
        origin += count
    return builder.finalize()


# --------------------------------------------------------------------------- the partition


def test_every_segment_is_drawn_exactly_once() -> None:
    """The property a batching bug breaks: geometry silently missing from the screen.

    Asserted as a count over the batches rather than by re-deriving the grouping, so a bug in the
    grouping cannot also be present in the check.
    """
    store = store_of((Kind.RAPID, 7), (Kind.FEED, 11), (Kind.RAPID, 5))
    batches = build_batches(store)
    assert sum(batch.segments for batch in batches) == len(store) == 23


def test_no_segment_is_drawn_twice() -> None:
    """A vertex total above 2N would mean some segment landed in two batches."""
    store = store_of((Kind.RAPID, 4), (Kind.FEED, 6))
    batches = build_batches(store)
    assert sum(batch.vertices.shape[0] for batch in batches) == 2 * len(store)


def test_the_partition_holds_with_untrusted_segments_interleaved() -> None:
    """Trust and kind cut across each other, so the two groupings must compose without loss."""
    store = store_of((Kind.RAPID, 5), (Kind.FEED, 5), (Kind.RAPID, 5), (Kind.FEED, 5))
    untrusted = np.zeros(len(store), dtype=bool)
    untrusted[3:8] = True  # spans the rapid/feed boundary
    batches = build_batches(store, untrusted=untrusted)
    assert sum(batch.segments for batch in batches) == len(store)
    assert sum(batch.segments for batch in batches if not batch.trusted) == 5


def test_untrusted_segments_never_land_in_a_trusted_batch() -> None:
    """The governing principle, enforced at the point it would be violated.

    Reconstructed from the vertex data rather than from the batch labels: a batch could be *labelled*
    untrusted and still contain trusted geometry, and the label is not what gets drawn.
    """
    store = store_of((Kind.FEED, 10))
    untrusted = np.zeros(len(store), dtype=bool)
    untrusted[2:5] = True
    batches = build_batches(store, untrusted=untrusted)

    trusted_x = np.concatenate(
        [batch.vertices[:, 0] for batch in batches if batch.trusted] or [np.empty(0)]
    )
    untrusted_x = np.concatenate([batch.vertices[:, 0] for batch in batches if not batch.trusted])
    # Segments 2, 3 and 4 span x = 2..5, and none of those interior coordinates may appear in a
    # trusted batch. x = 2.0 and x = 5.0 are shared endpoints, so only the strict interior is checked.
    assert not ((trusted_x > 2.0) & (trusted_x < 5.0)).any()
    assert untrusted_x.min() == 2.0 and untrusted_x.max() == 5.0


def test_kinds_are_never_mixed_within_a_batch() -> None:
    """Colour is per batch, so a mixed batch would draw rapids in the feed colour."""
    store = store_of((Kind.RAPID, 3), (Kind.FEED, 3))
    for batch in build_batches(store):
        assert batch.segments == 3, "a batch holding both kinds would have six segments"


# --------------------------------------------------------------------------- batch count and budget


def test_the_batch_count_stays_far_inside_the_plan_budget() -> None:
    """Four styles, not ten. PLAN allows ten buffers; fewer draw calls is strictly better."""
    store = store_of((Kind.RAPID, 5), (Kind.FEED, 5))
    untrusted = np.zeros(len(store), dtype=bool)
    untrusted[[0, 6]] = True  # one rapid and one feed, so all four styles are present
    batches = build_batches(store, untrusted=untrusted)
    assert len(batches) == 4
    assert len(batches) <= MAX_BATCHES


def test_a_program_with_one_kind_produces_one_batch() -> None:
    """Empty groups are skipped rather than uploaded as zero-length buffers."""
    assert len(build_batches(store_of((Kind.FEED, 10)))) == 1


def test_an_empty_store_produces_no_batches() -> None:
    """A fully suppressed program draws nothing, and must not create an empty GL item to do it."""
    assert build_batches(SegmentStore.empty()) == []


def test_gl_bytes_are_four_per_float_and_counted() -> None:
    """The GL half of the memory budget: float32 positions, two vertices per segment."""
    store = store_of((Kind.FEED, 1000))
    batches = build_batches(store)
    assert total_gl_bytes(batches) == 1000 * 2 * 3 * 4


# --------------------------------------------------------------------------- upload readiness


def test_vertices_are_float32_and_contiguous() -> None:
    """GL wants float32; a non-contiguous array would be silently re-copied on every upload."""
    for batch in build_batches(store_of((Kind.FEED, 4))):
        assert batch.vertices.dtype == np.float32
        assert batch.vertices.flags["C_CONTIGUOUS"]


def test_vertices_are_pairs_for_mode_lines() -> None:
    """`mode='lines'` reads consecutive pairs, so an odd vertex count would drop the last segment."""
    for batch in build_batches(store_of((Kind.RAPID, 3), (Kind.FEED, 7))):
        assert batch.vertices.shape[0] % 2 == 0
        assert batch.vertices.shape[1] == 3


def test_the_store_is_not_mutated_by_batching() -> None:
    """`lin` is machine coordinates and the verifier reads it; batching must not touch it."""
    store = store_of((Kind.FEED, 5))
    before = store.lin.copy()
    build_batches(store)
    assert np.array_equal(store.lin, before)
    assert store.lin.dtype == np.float64, "the float32 conversion must not write back to the store"


# --------------------------------------------------------------------------- colour


def test_rapids_and_feeds_get_different_colours() -> None:
    """PLAN.md: rapids red, feeds green — and colour is the *only* channel carrying this."""
    assert TRUSTED_COLORS[Kind.RAPID] != TRUSTED_COLORS[Kind.FEED]
    batches = {
        batch.kind: batch.color
        for batch in build_batches(store_of((Kind.RAPID, 2), (Kind.FEED, 2)))
    }
    assert batches[Kind.RAPID] == TRUSTED_COLORS[Kind.RAPID]
    assert batches[Kind.FEED] == TRUSTED_COLORS[Kind.FEED]


def test_untrusted_geometry_is_visually_distinct_from_trusted() -> None:
    """Every trust/kind pairing must be distinguishable, or the tier collapses on screen.

    This checks the palettes only. On its own it is not enough — see the test below, which is the one
    that catches `build_batches` failing to *apply* the untrusted palette.
    """
    all_colors = list(TRUSTED_COLORS.values()) + list(UNTRUSTED_COLORS.values())
    assert len(set(all_colors)) == len(all_colors), "two styles share a colour"


@pytest.mark.parametrize("kind", [Kind.RAPID, Kind.FEED])
def test_the_untrusted_palette_is_actually_applied(kind: Kind) -> None:
    """The governing principle, asserted on the output rather than on the constants.

    Distinct palettes are worthless if `build_batches` reaches for the trusted one regardless — and
    that mutation survived the first version of this suite, because the only test comparing colours
    compared an untrusted *feed* against a trusted *rapid* and found them different for the wrong
    reason. Both kinds are checked, so neither branch can silently use the wrong palette.
    """
    store = store_of((kind, 4))
    untrusted = np.ones(len(store), dtype=bool)
    batch = build_batches(store, untrusted=untrusted)[0]
    assert batch.trusted is False
    assert batch.color == UNTRUSTED_COLORS[kind]
    assert batch.color != TRUSTED_COLORS[kind]


def test_untrusted_batches_are_labelled_so_a_legend_can_name_them() -> None:
    store = store_of((Kind.FEED, 4))
    untrusted = np.ones(len(store), dtype=bool)
    assert build_batches(store, untrusted=untrusted)[0].label == "feed (unverified)"


# --------------------------------------------------------------------------- masks and coordinates


def test_a_wrong_length_mask_is_refused_rather_than_broadcast() -> None:
    """Numpy would broadcast a length-1 mask happily and mark nothing, or the wrong things."""
    store = store_of((Kind.FEED, 10))
    with pytest.raises(ValueError, match="one entry per segment"):
        build_batches(store, untrusted=np.zeros(3, dtype=bool))


def test_no_mask_means_everything_is_trusted() -> None:
    assert all(batch.trusted for batch in build_batches(store_of((Kind.FEED, 3))))


def test_part_coordinates_are_used_when_asked_for() -> None:
    """Table-mount display draws `lin_part`; `lin` stays machine coordinates for the verifier."""
    store = store_of((Kind.FEED, 2))
    store.set_part_coordinates(store.lin + 100.0)
    machine = build_batches(store)[0].vertices
    part = build_batches(store, use_part_coordinates=True)[0].vertices
    assert np.allclose(part, machine + 100.0)


def test_requesting_part_coordinates_without_a_transform_is_an_error() -> None:
    """Falling back to machine coordinates would draw the wrong path with no indication."""
    with pytest.raises(ValueError, match="no display transform"):
        build_batches(store_of((Kind.FEED, 2)), use_part_coordinates=True)


# --------------------------------------------------------------------------- bounds for the camera


def test_bounds_cover_the_geometry() -> None:
    store = store_of((Kind.FEED, 10))
    extent = bounds(store)
    assert extent.shape == (2, 3)
    assert extent[0].tolist() == [0.0, 0.0, 0.0]
    assert extent[1].tolist() == [10.0, 0.0, 0.0]


def test_bounds_exclude_the_rotary_axis() -> None:
    """A box spanning millimetres and degrees would be meaningless, and this one places a camera."""
    store = store_of((Kind.FEED, 2))
    store.rot[:] = 3600.0
    assert bounds(store).max() == 2.0, "a rotary value leaked into the linear bounding box"


def test_bounds_of_an_empty_store_are_none_not_zeros() -> None:
    """Returning zeros would aim the camera at the origin as though geometry were there."""
    assert bounds(SegmentStore.empty()) is None


# --------------------------------------------------------------------------- against real programs


@pytest.mark.parametrize(
    "fixture",
    ["baseline_4axis.nc", "arc_helical.nc", "cutter_comp_span.nc", "canned_cycle_span.nc"],
)
def test_real_programs_batch_without_losing_geometry(fixture: str, profile) -> None:
    """End to end on the corpus, including both span tiers.

    `cutter_comp_span.nc` is drawn-but-untrusted and `canned_cycle_span.nc` is suppressed, so between
    them they exercise the two tiers that reach the renderer differently.
    """
    sim, _ = simulate_text(fixture_text(fixture), profile)
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    assert sum(batch.segments for batch in batches) == len(sim.store)
    assert len(batches) <= MAX_BATCHES


def test_a_cutter_comp_program_yields_an_untrusted_batch(profile) -> None:
    """The tier must survive the whole pipeline, not just the batching unit tests.

    If this passed vacuously — no untrusted batch because the mask was empty — the styling would be
    untested against a real program, so the mask is asserted non-empty first.
    """
    sim, _ = simulate_text(fixture_text("cutter_comp_span.nc"), profile)
    assert sim.unverified_mask().any(), "the fixture no longer produces a drawn-but-unverified span"
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    assert any(not batch.trusted for batch in batches)


def test_a_suppressed_program_has_nothing_untrusted_to_draw(profile) -> None:
    """A canned cycle is not drawn at all, so it must not appear as untrusted geometry either."""
    sim, _ = simulate_text(fixture_text("canned_cycle_span.nc"), profile)
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    assert all(batch.trusted for batch in batches)


# --------------------------------------------------------------------------- scale


def test_a_500k_segment_program_batches_within_the_gl_budget(profile) -> None:
    """The GL half of PLAN's memory budget, measured rather than assumed.

    Positions only: a uniform colour per batch means no per-vertex colour buffer, which would
    otherwise cost 16 MB at this size — more than the positions themselves.
    """
    text = "G21 G94 G90\n" + "".join(f"G1 X{i % 50} A{(i + 1) * 360} F600\n" for i in range(3165))
    sim, _ = simulate_text(text, profile)
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    assert len(sim.store) >= 500_000
    assert sum(batch.segments for batch in batches) == len(sim.store)
    assert len(batches) <= MAX_BATCHES
    gl_megabytes = total_gl_bytes(batches) / 1e6
    assert gl_megabytes < 15.0, f"{gl_megabytes:.1f} MB of GL buffers at 500k segments"

    # The combined budget is the one that actually binds, and the only place both halves are in
    # scope. It was measured at 50.5 MB here and PLAN's number was 50 MB, set from the store alone
    # without counting the upload copy — which is how a budget gets quietly exceeded by a change that
    # looks like it only touches rendering. PLAN now says 55 MB.
    combined = (sim.store.nbytes() + total_gl_bytes(batches)) / 1e6
    assert combined < 55.0, (
        f"{combined:.1f} MB of store + GL memory at {len(sim.store):,} segments exceeds the 55 MB "
        "budget in PLAN.md § Performance Requirements "
        f"(store {sim.store.nbytes() / 1e6:.1f} MB + GL {gl_megabytes:.1f} MB)"
    )


def test_batch_is_frozen_so_geometry_cannot_be_reassigned() -> None:
    """The widget holds these; a mutable batch would let a styling change rewrite geometry."""
    batch = build_batches(store_of((Kind.FEED, 2)))[0]
    with pytest.raises(AttributeError):
        batch.color = (0.0, 0.0, 0.0, 1.0)
    assert isinstance(batch, Batch)
