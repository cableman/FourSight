"""Timing tests (T2.4).

The invariant under test is `max(linear_time, rotary_time)`, never a norm across millimetres and
degrees. That needs a *discriminating* case: for 100 mm + 3600° at F600 the correct answer is 60 s,
a norm gives 360.1 s — and an early bug that treated F as the rotary rate gave **360 s**, close
enough to the norm's answer to be mistaken for one. So the numbers here are chosen so the three
possible answers are far apart.
"""

import numpy as np
import pytest

from foursight.machine.profile import load_profile_text
from foursight.parser.resolver import parse
from foursight.sim.timing import Rates, block_durations, rates_for

PROFILE_TEXT = """
[machine]
units = "mm"
[limits]
max_feed = 3000.0
[axes.x]
max_rapid = 5000.0
[axes.y]
max_rapid = 5000.0
[axes.z]
max_rapid = 3000.0
[axes.a]
type = "rotary"
max_rapid = 3600.0
"""


@pytest.fixture(scope="module")
def profile():
    return load_profile_text(PROFILE_TEXT)


def timing(program: str, lin, rot, profile):
    """Time one block. `lin` is (n, 2, 3) mm and `rot` is (n, 2) degrees."""
    command = parse(program).commands[-1]
    rapid = command.motion == "0"
    rates = rates_for(command, profile, rapid=rapid)
    return block_durations(
        np.array(lin, dtype=np.float64),
        np.array(rot, dtype=np.float64),
        rates,
        profile,
        rapid=rapid,
    )


def straight(distance: float):
    return [[[0.0, 0.0, 0.0], [distance, 0.0, 0.0]]]


def still():
    return [[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]]


# --------------------------------------------------------------------------- the core invariant


def test_a_mixed_move_takes_the_max_not_the_norm(profile) -> None:
    """100 mm needs 10 s at F600; 3600° needs 60 s at the 3600 deg/min axis limit.

    The correct answer is 60 s. A norm across mm and degrees gives 360.1 s. Treating F as the rotary
    rate gives 360 s — which is why this case uses numbers that separate all three.
    """
    result = timing("G21 G94\nG1 X100 A3600 F600\n", straight(100.0), [[0.0, 3600.0]], profile)
    assert result.total == pytest.approx(60.0)

    norm_answer = float(np.hypot(100.0, 3600.0)) / 600.0 * 60.0
    assert abs(result.total - norm_answer) > 100.0, "must not be the mm/degree norm"


def test_a_pure_rotary_move_is_timed_from_the_rotary_rate_alone(profile) -> None:
    """No linear travel at all, so any linear-derived duration would be zero."""
    result = timing("G21 G94\nG1 A90 F1800\n", still(), [[0.0, 90.0]], profile)
    assert result.total == pytest.approx(3.0)
    assert result.unknown == 0


def test_a_pure_linear_move_ignores_the_rotary_axis(profile) -> None:
    result = timing("G21 G94\nG1 X100 F600\n", straight(100.0), [[0.0, 0.0]], profile)
    assert result.total == pytest.approx(10.0)


def test_whichever_component_is_slower_sets_the_duration(profile) -> None:
    """Both directions of the max, so a `min` would fail one of them."""
    linear_slower = timing("G21 G94\nG1 X100 A10 F60\n", straight(100.0), [[0.0, 10.0]], profile)
    rotary_slower = timing("G21 G94\nG1 X1 A3600 F600\n", straight(1.0), [[0.0, 3600.0]], profile)
    assert linear_slower.total == pytest.approx(100.0)  # 100 mm at 60 mm/min
    assert rotary_slower.total == pytest.approx(60.0)  # 3600 deg at 3600 deg/min


def test_f_is_degrees_per_minute_only_when_nothing_linear_moves(profile) -> None:
    """The distinction the early bug missed.

    Rotary-only: F is deg/min. Mixed: F governs the linear path and A is capped by its own maximum.
    """
    alone = timing("G21 G94\nG1 A3600 F600\n", still(), [[0.0, 3600.0]], profile)
    mixed = timing("G21 G94\nG1 X100 A3600 F600\n", straight(100.0), [[0.0, 3600.0]], profile)
    assert alone.total == pytest.approx(360.0)  # 3600 deg at F600 deg/min
    assert mixed.total == pytest.approx(60.0)  # 3600 deg at the 3600 deg/min axis limit


# --------------------------------------------------------------------------- feed modes


def test_g94_is_units_per_minute(profile) -> None:
    assert timing(
        "G21 G94\nG1 X50 F300\n", straight(50.0), [[0, 0]], profile
    ).total == pytest.approx(10.0)


def test_g95_multiplies_feed_by_spindle_speed(profile) -> None:
    """0.1 mm/rev at 6000 rpm is 600 mm/min."""
    result = timing("G21 G95\nS6000 M3\nG1 X100 F0.1\n", straight(100.0), [[0, 0]], profile)
    assert result.total == pytest.approx(10.0)


def test_g95_without_a_spindle_speed_is_unknown_not_guessed(profile) -> None:
    """F is mm/rev and the spindle is stopped, so there is genuinely no feed rate to use."""
    result = timing("G21 G95\nG1 X100 F0.1\n", straight(100.0), [[0, 0]], profile)
    assert result.total == 0.0
    assert result.unknown == 1


def test_g93_is_a_block_time_not_a_rate(profile) -> None:
    """F2 under inverse time means the block takes half a minute, however far it travels."""
    short = timing("G21 G93\nG1 X1 F2\n", straight(1.0), [[0, 0]], profile)
    long = timing("G21 G93\nG1 X1000 F2\n", straight(1000.0), [[0, 0]], profile)
    assert short.total == pytest.approx(30.0)
    assert long.total == pytest.approx(30.0), "inverse time is distance-independent"


def test_g93_shares_its_block_time_along_the_path(profile) -> None:
    """Weighted by length, so the speed stays constant.

    Splitting evenly per segment would make the tool appear to slow down through the finely
    tessellated parts of an arc.
    """
    lin = [[[0.0, 0, 0], [1.0, 0, 0]], [[1.0, 0, 0], [4.0, 0, 0]]]
    result = timing("G21 G93\nG1 X4 F2\n", lin, [[0, 0], [0, 0]], profile)
    assert result.total == pytest.approx(30.0)
    assert result.durations.tolist() == pytest.approx([7.5, 22.5])  # 1 : 3 by length


def test_g93_with_no_travel_still_consumes_its_time(profile) -> None:
    result = timing("G21 G93\nG1 X0 F2\n", still(), [[0, 0]], profile)
    assert result.total == pytest.approx(30.0)


# --------------------------------------------------------------------------- rapids


def test_a_rapid_ignores_the_feed_rate(profile) -> None:
    """G0 moves at the machine's rapid rate whatever F happens to be set to."""
    fast = timing("G21 G94\nG0 X100\n", straight(100.0), [[0, 0]], profile)
    assert fast.total == pytest.approx(100.0 / 5000.0 * 60.0)


def test_a_coordinated_rapid_takes_as_long_as_its_slowest_axis(profile) -> None:
    """X at 5000 mm/min covers 100 mm in 1.2 s; Z at 3000 covers 100 mm in 2.0 s."""
    lin = [[[0.0, 0, 0], [100.0, 0, -100.0]]]
    result = timing("G21\nG0 X100 Z-100\n", lin, [[0, 0]], profile)
    assert result.total == pytest.approx(2.0)


def test_a_rapid_with_no_axis_rate_configured_is_unknown() -> None:
    bare = load_profile_text('[machine]\nunits = "mm"\n')
    result = timing("G21\nG0 X100\n", straight(100.0), [[0, 0]], bare)
    assert result.total == 0.0
    assert result.unknown == 1


def test_a_rotary_rapid_uses_the_rotary_axis_rate(profile) -> None:
    result = timing("G21\nG0 A3600\n", still(), [[0.0, 3600.0]], profile)
    assert result.total == pytest.approx(60.0)


# --------------------------------------------------------------------------- limits


def test_feed_is_clamped_to_the_machine_maximum(profile) -> None:
    """The control would clamp, so using the programmed value would under-report the time."""
    result = timing("G21 G94\nG1 X100 F9000\n", straight(100.0), [[0, 0]], profile)
    assert result.total == pytest.approx(2.0)  # 100 mm at the 3000 mm/min cap, not at 9000


def test_an_unconfigured_max_feed_does_not_clamp() -> None:
    bare = load_profile_text('[machine]\nunits = "mm"\n')
    result = timing("G21 G94\nG1 X100 F6000\n", straight(100.0), [[0, 0]], bare)
    assert result.total == pytest.approx(1.0)


def test_a_rotary_only_feed_is_capped_by_the_axis_maximum(profile) -> None:
    """F9000 deg/min is beyond the 3600 deg/min axis, so 3600° takes a minute, not 24 s."""
    result = timing("G21 G94\nG1 A3600 F9000\n", still(), [[0.0, 3600.0]], profile)
    assert result.total == pytest.approx(60.0)


# --------------------------------------------------------------------------- unknowns


def test_a_move_with_no_feed_rate_is_counted_as_unknown(profile) -> None:
    """0.0 rather than invented — and countable, so a timeline can say how much it is missing."""
    result = timing("G21 G94\nG1 X100\n", straight(100.0), [[0, 0]], profile)
    assert result.total == 0.0
    assert result.unknown == 1


def test_a_stationary_segment_is_not_counted_as_unknown(profile) -> None:
    """Zero duration because it does not move, which is different from zero because we cannot tell."""
    result = timing("G21 G94\nG1 X0 F600\n", still(), [[0, 0]], profile)
    assert result.total == 0.0
    assert result.unknown == 0


def test_zero_feed_is_treated_as_unknown_rather_than_infinite_time(profile) -> None:
    result = timing("G21 G94\nG1 X100 F0\n", straight(100.0), [[0, 0]], profile)
    assert result.unknown == 1
    assert np.isfinite(result.durations).all()


# --------------------------------------------------------------------------- shape


def test_durations_are_one_per_segment(profile) -> None:
    lin = [[[0.0, 0, 0], [1.0, 0, 0]], [[1.0, 0, 0], [2.0, 0, 0]], [[2.0, 0, 0], [3.0, 0, 0]]]
    result = timing("G21 G94\nG1 X3 F600\n", lin, [[0, 0]] * 3, profile)
    assert result.durations.shape == (3,)
    assert result.durations.dtype == np.float64


def test_an_empty_block_yields_no_durations(profile) -> None:
    result = block_durations(np.empty((0, 2, 3)), np.empty((0, 2)), Rates(), profile, rapid=False)
    assert result.durations.shape == (0,)
    assert result.total == 0.0


def test_durations_are_never_negative(profile) -> None:
    """A reversed rotary move is still time spent."""
    result = timing("G21 G94\nG1 A-90 F1800\n", still(), [[0.0, -90.0]], profile)
    assert result.total == pytest.approx(3.0)
    assert (result.durations >= 0.0).all()


# --------------------------------------------------------------------------- rates


def test_rates_distinguish_unknown_from_zero(profile) -> None:
    """A missing feed and a feed of zero are different programs; only one is describable."""
    no_feed = rates_for(parse("G21 G94\nG1 X1\n").commands[-1], profile, rapid=False)
    assert no_feed.linear_mm_per_min is None


def test_a_rapid_reports_no_linear_rate_because_it_is_per_axis(profile) -> None:
    rapid = rates_for(parse("G21\nG0 X1\n").commands[-1], profile, rapid=True)
    assert rapid.linear_mm_per_min is None
    assert rapid.rotary_max_deg_per_min == 3600.0


def test_inverse_time_is_flagged_on_the_rates(profile) -> None:
    rates = rates_for(parse("G21 G93\nG1 X1 F2\n").commands[-1], profile, rapid=False)
    assert rates.inverse_time is True
    assert rates.block_seconds == pytest.approx(30.0)
    assert (
        rates_for(parse("G21 G94\nG1 X1 F2\n").commands[-1], profile, rapid=False).inverse_time
        is False
    )


# --------------------------------------------------------------------------- the single-segment fast path


def test_the_fast_path_agrees_with_the_vector_path(profile) -> None:
    """**This test is why a second implementation is acceptable.**

    91% of motion blocks produce one segment, so `_durations_from_deltas` takes a scalar shortcut for
    ``n == 1``. Two implementations of the same rules is normally a drift hazard, and here the drift would be
    especially nasty: a fast path that disagreed would give a wrong time estimate *only for ordinary
    programs*, which is the worst possible distribution for a bug.

    So the equivalence is proved rather than assumed: both are driven over every rate configuration and a
    spread of randomized geometry on **identical input**, and must agree bit-for-bit.

    The first version of this test padded a stationary second segment to force the vector path, which is
    unsound — under inverse time the padding changes the weight denominator, so the two were compared on
    different questions and disagreed for the wrong reason. `_vector_durations` is named and called directly
    instead.
    """
    import numpy as np

    from foursight.sim.timing import _single_segment, _vector_durations

    rng = np.random.default_rng(20260807)
    programs = [
        "G21 G94\nG1 X1 F600\n",  # ordinary feed
        "G21 G94\nG1 X1\n",  # no feed rate: unknown
        "G21 G94\nG1 X1 F0\n",  # zero feed: unknown
        "G21 G93\nG1 X1 F2\n",  # inverse time
        "G21 G95\nS6000 M3\nG1 X1 F0.1\n",  # units per rev
        "G21 G95\nG1 X1 F0.1\n",  # units per rev, spindle stopped
        "G21\nG0 X1\n",  # rapid
        "G21 G94\nG1 X1 F9000\n",  # feed above the machine maximum
    ]
    checked = 0
    for program in programs:
        command = parse(program).commands[-1]
        rapid = command.motion == "0"
        rates = rates_for(command, profile, rapid=rapid)
        for _ in range(40):
            linear = rng.normal(size=(1, 3)) * rng.choice([0.0, 0.001, 1.0, 250.0])
            rotary = np.array([rng.normal() * rng.choice([0.0, 0.5, 90.0, 3600.0])])

            fast = _single_segment(linear, rotary, rates, profile, rapid=rapid)
            slow = _vector_durations(linear, rotary, rates, profile, rapid=rapid)

            assert fast.durations[0] == slow.durations[0], (
                f"fast path diverged for {program!r}: {fast.durations[0]!r} != {slow.durations[0]!r}"
            )
            assert fast.unknown == slow.unknown, f"unknown count diverged for {program!r}"
            checked += 1
    assert checked == len(programs) * 40


def test_the_fast_path_is_actually_taken(profile) -> None:
    """A fast path nothing reaches is dead weight, and the equivalence test above would still pass."""
    import numpy as np

    from foursight.sim import timing

    calls = []
    original = timing._single_segment
    try:
        timing._single_segment = lambda *a, **k: calls.append(1) or original(*a, **k)
        timing.block_durations(
            np.zeros((1, 2, 3)),
            np.zeros((1, 2)),
            Rates(linear_mm_per_min=600.0),
            profile,
            rapid=False,
        )
    finally:
        timing._single_segment = original
    assert calls, "a one-segment block did not reach the fast path"


def test_a_multi_segment_block_uses_the_vector_path(profile) -> None:
    """The boundary: two segments must not take the scalar shortcut."""
    import numpy as np

    from foursight.sim import timing

    calls = []
    original = timing._single_segment
    try:
        timing._single_segment = lambda *a, **k: calls.append(1) or original(*a, **k)
        timing.block_durations(
            np.zeros((2, 2, 3)),
            np.zeros((2, 2)),
            Rates(linear_mm_per_min=600.0),
            profile,
            rapid=False,
        )
    finally:
        timing._single_segment = original
    assert not calls
