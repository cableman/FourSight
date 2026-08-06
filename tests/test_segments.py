"""SegmentStore tests (T2.1).

The store is what the memory budget and the GL upload path both rest on, and every property that
matters here is invisible to behavioural testing: a store that works perfectly while costing 6× the
memory, or whose reshape silently copies, passes any test that only checks coordinates.

So the assertions are deliberately structural — dtypes, contiguity, `shares_memory`, byte counts —
and the mm/degrees separation is checked at the API level, not just in the data.
"""

import numpy as np
import pytest

from conftest import record_measurement
from foursight.sim.segments import DEFAULT_CHUNK, Kind, SegmentBuilder, SegmentStore

TARGET_SEGMENTS = 500_000
# PLAN.md § Segment store predicts ~38 MB at N = 500k. The bound is generous enough not to fail on a
# harmless dtype-neutral change, tight enough that per-object storage (~200 MB+) cannot pass.
MAX_MB_AT_TARGET = 45.0


def line_points(count: int) -> np.ndarray:
    """`count` collinear points, so `count - 1` segments."""
    points = np.zeros((count, 3), dtype=np.float64)
    points[:, 0] = np.arange(count, dtype=np.float64)
    return points


def build(points: np.ndarray, kind: Kind = Kind.FEED, line: int = 1, **kwargs) -> SegmentStore:
    builder = SegmentBuilder()
    builder.add_polyline(points, kind, line, **kwargs)
    return builder.finalize()


# --------------------------------------------------------------------------- shape and dtype


def test_columns_have_the_shapes_and_dtypes_plan_specifies() -> None:
    store = build(line_points(4))
    assert store.lin.shape == (3, 2, 3) and store.lin.dtype == np.float64
    assert store.rot.shape == (3, 2) and store.rot.dtype == np.float64
    assert store.kind.shape == (3,) and store.kind.dtype == np.uint8
    assert store.line.shape == (3,) and store.line.dtype == np.int32
    assert store.duration.shape == (3,) and store.duration.dtype == np.float64
    assert store.lin_part is None


def test_segment_endpoints_are_consecutive_points() -> None:
    store = build(line_points(4))
    assert store.lin[0, 0].tolist() == [0.0, 0.0, 0.0]
    assert store.lin[0, 1].tolist() == [1.0, 0.0, 0.0]
    assert store.lin[2, 1].tolist() == [3.0, 0.0, 0.0]


def test_validate_accepts_a_well_formed_store() -> None:
    build(line_points(10)).validate()


def test_an_empty_store_needs_no_special_casing() -> None:
    empty = SegmentStore.empty()
    assert len(empty) == 0
    empty.validate()
    assert empty.vertices.shape == (0, 3)
    assert SegmentBuilder().finalize().validate() is None


# --------------------------------------------------------------------------- zero-copy upload


def test_vertices_is_a_zero_copy_view() -> None:
    """The entire reason for the (N, 2, 3) shape: `GLLinePlotItem(mode='lines')` wants (2N, 3).

    A copy here would double the resident cost of every upload and silently blow the budget.
    """
    store = build(line_points(1000))
    vertices = store.vertices
    assert vertices.shape == (1998, 3)
    assert np.shares_memory(vertices, store.lin)
    assert vertices.base is store.lin


def test_vertices_reflects_lin_because_it_is_a_view() -> None:
    store = build(line_points(3))
    store.lin[0, 0, 0] = 99.0
    assert store.vertices[0, 0] == 99.0


def test_lin_is_c_contiguous_after_finalize() -> None:
    """Non-contiguous data cannot be uploaded without a repack, whatever its shape says."""
    store = build(line_points(DEFAULT_CHUNK * 2 + 5))
    assert store.lin.flags["C_CONTIGUOUS"]


def test_validate_rejects_non_contiguous_lin() -> None:
    store = build(line_points(10))
    sliced = SegmentStore(
        lin=store.lin[::2],
        rot=store.rot[::2],
        kind=store.kind[::2],
        line=store.line[::2],
        duration=store.duration[::2],
    )
    with pytest.raises(ValueError, match="C-contiguous"):
        sliced.validate()


# --------------------------------------------------------------------------- rot stays separate


def test_rotation_is_its_own_column_not_a_fourth_position_component() -> None:
    """`np.linalg.norm(end - start)` over a 4-vector mixes mm and degrees and returns nonsense.

    The columns are separate so that expression cannot be written by accident.
    """
    store = build(line_points(3), rotations=np.array([0.0, 90.0, 180.0]))
    assert store.lin.shape[-1] == 3, "lin must stay 3-wide: no rotary component"
    assert store.rot.tolist() == [[0.0, 90.0], [90.0, 180.0]]


def test_there_is_no_accessor_returning_a_four_vector() -> None:
    """Guards the invariant at the API surface, where a helper would otherwise be tempting."""
    store = build(line_points(3))
    for name in dir(store):
        if name.startswith("_"):
            continue
        value = getattr(store, name)
        if isinstance(value, np.ndarray) and value.ndim and value.shape[-1] == 4:
            pytest.fail(f"{name} exposes a 4-wide vector, mixing millimetres and degrees")


def test_rotations_are_a_separate_argument_not_a_points_column() -> None:
    """Passing (M, 4) points must fail rather than be silently interpreted."""
    builder = SegmentBuilder()
    with pytest.raises(ValueError, match=r"\(M, 3\)"):
        builder.add_polyline(np.zeros((4, 4)), Kind.FEED, line=1)


def test_rotations_length_is_checked_against_the_points() -> None:
    builder = SegmentBuilder()
    with pytest.raises(ValueError, match="rotations must be"):
        builder.add_polyline(line_points(4), Kind.FEED, 1, rotations=np.array([0.0, 1.0]))


def test_rotations_default_to_zero_for_a_three_axis_move() -> None:
    assert build(line_points(3)).rot.tolist() == [[0.0, 0.0], [0.0, 0.0]]


# --------------------------------------------------------------------------- traceability


def test_every_segment_records_its_source_line() -> None:
    builder = SegmentBuilder()
    builder.add_polyline(line_points(3), Kind.FEED, line=12)
    builder.add_polyline(line_points(4), Kind.RAPID, line=40)
    store = builder.finalize()
    assert store.line.tolist() == [12, 12, 40, 40, 40]


def test_a_zero_line_number_is_refused() -> None:
    """Editor sync, diagnostics and fixes all key on `line[i]`; an untraceable segment breaks them."""
    builder = SegmentBuilder()
    with pytest.raises(ValueError, match="1-based"):
        builder.add_polyline(line_points(3), Kind.FEED, line=0)


def test_validate_rejects_a_store_with_an_untraceable_segment() -> None:
    store = build(line_points(3))
    store.line[1] = 0
    with pytest.raises(ValueError, match="source line"):
        store.validate()


# --------------------------------------------------------------------------- kind


def test_kind_is_motion_type_only() -> None:
    """RAPID or FEED. An arc is a cutting move and, after interpolation, a line — so no arc kind."""
    assert [member.name for member in Kind] == ["RAPID", "FEED"]
    builder = SegmentBuilder()
    builder.add_polyline(line_points(2), Kind.RAPID, line=1)
    builder.add_polyline(line_points(2), Kind.FEED, line=2)
    store = builder.finalize()
    assert store.kind.tolist() == [int(Kind.RAPID), int(Kind.FEED)]


def test_mask_groups_by_kind_for_gl_batching() -> None:
    builder = SegmentBuilder()
    builder.add_polyline(line_points(3), Kind.RAPID, line=1)
    builder.add_polyline(line_points(4), Kind.FEED, line=2)
    store = builder.finalize()
    assert store.mask(Kind.RAPID).sum() == 2
    assert store.mask(Kind.FEED).sum() == 3


# --------------------------------------------------------------------------- lin_part


def test_part_coordinates_are_a_second_array_and_never_replace_lin() -> None:
    """Transforming `lin` in place would silently destroy the ability to verify travel limits."""
    store = build(line_points(4))
    machine = store.lin.copy()
    store.set_part_coordinates(store.lin + 5.0)
    assert np.array_equal(store.lin, machine), "lin must be untouched"
    assert np.array_equal(store.lin_part, machine + 5.0)


def test_part_coordinates_must_not_alias_lin() -> None:
    store = build(line_points(4))
    with pytest.raises(ValueError, match="separate array"):
        store.set_part_coordinates(store.lin)


def test_part_coordinates_shape_is_checked() -> None:
    store = build(line_points(4))
    with pytest.raises(ValueError, match="shape"):
        store.set_part_coordinates(np.zeros((2, 2, 3)))


def test_part_vertices_is_also_a_zero_copy_view() -> None:
    store = build(line_points(100))
    store.set_part_coordinates(store.lin + 1.0)
    assert np.shares_memory(store.part_vertices, store.lin_part)


def test_part_vertices_is_none_before_any_transform() -> None:
    assert build(line_points(4)).part_vertices is None


# --------------------------------------------------------------------------- chunked growth


@pytest.mark.parametrize("count", [1, 2, 7, 64, 65, 128, 129, 1000])
def test_chunk_boundaries_are_handled_for_any_size(count: int) -> None:
    """A polyline spanning several chunks must produce exactly the same segments as one that fits."""
    builder = SegmentBuilder(chunk_size=64)
    points = line_points(count + 1)
    builder.add_polyline(points, Kind.FEED, line=3)
    store = builder.finalize()
    assert len(store) == count
    assert store.lin[:, 0, 0].tolist() == list(range(count))
    assert store.lin[:, 1, 0].tolist() == list(range(1, count + 1))
    store.validate()


def test_many_small_appends_match_one_large_append() -> None:
    small = SegmentBuilder(chunk_size=8)
    for index in range(50):
        small.add_segment((float(index), 0, 0), (index + 1.0, 0, 0), Kind.FEED, line=1)
    large = SegmentBuilder()
    large.add_polyline(line_points(51), Kind.FEED, line=1)
    assert np.array_equal(small.finalize().lin, large.finalize().lin)


def test_builder_length_tracks_appends() -> None:
    builder = SegmentBuilder(chunk_size=4)
    assert len(builder) == 0
    builder.add_polyline(line_points(10), Kind.FEED, line=1)
    assert len(builder) == 9


def test_a_single_point_adds_nothing() -> None:
    """One point is not a segment; silently adding a zero-length one would corrupt the timeline."""
    builder = SegmentBuilder()
    assert builder.add_polyline(line_points(1), Kind.FEED, line=1) == 0
    assert len(builder.finalize()) == 0


def test_durations_are_optional_and_default_to_zero() -> None:
    """`sim/timing.py` fills this column later; zeros are the honest placeholder."""
    assert build(line_points(4)).duration.tolist() == [0.0, 0.0, 0.0]


def test_durations_length_is_checked_per_segment_not_per_point() -> None:
    builder = SegmentBuilder()
    with pytest.raises(ValueError, match="one per segment"):
        builder.add_polyline(line_points(4), Kind.FEED, 1, durations=np.zeros(4))


def test_durations_are_stored_when_given() -> None:
    store = build(line_points(4), durations=np.array([0.1, 0.2, 0.3]))
    assert store.duration.tolist() == [0.1, 0.2, 0.3]


# --------------------------------------------------------------------------- the memory budget


def test_memory_at_the_target_segment_count() -> None:
    """PLAN.md § Segment store: ~38 MB at N = 500k, versus 200 MB+ for per-object segments.

    This is the assertion that makes the columnar design a requirement rather than a preference.
    """
    builder = SegmentBuilder()
    builder.add_polyline(line_points(TARGET_SEGMENTS + 1), Kind.FEED, line=1)
    store = builder.finalize()
    assert len(store) == TARGET_SEGMENTS

    megabytes = store.nbytes() / 1e6
    record_measurement(
        f"  SegmentStore at {TARGET_SEGMENTS:,} segments: {megabytes:.1f} MB "
        f"({store.nbytes() / TARGET_SEGMENTS:.0f} bytes/segment, PLAN predicts ~38 MB)"
    )
    assert megabytes < MAX_MB_AT_TARGET, (
        f"{megabytes:.1f} MB exceeds the {MAX_MB_AT_TARGET} MB bound; the columnar layout has "
        "regressed towards per-object storage"
    )


def test_part_coordinates_cost_is_accounted_for() -> None:
    """The table-mount display array is a second (N, 2, 3) float64 — 24 MB at 500k, not free."""
    store = build(line_points(1001))
    before = store.nbytes()
    store.set_part_coordinates(store.lin + 1.0)
    assert store.nbytes() == before + store.lin.nbytes
