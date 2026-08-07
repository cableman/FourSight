"""Per-segment durations for the timeline.

**Linear and rotary travel are measured separately and combined as ``max(linear_time,
rotary_time)``.** Never ``np.linalg.norm`` across linear (mm) and rotary (deg) components — that
expression is meaningless, and it is exactly what one would otherwise write for a duration. The
columnar split in ``segments.py`` exists to make the mistake hard; this module is where it would
have been made. `max` is also the physically correct answer for coordinated motion: the axes move
together and the move takes as long as its slowest participant.

Feed modes, per PLAN.md § Supported G-code Subset:

- **G94** units/min — ``F`` is mm/min (or deg/min for a rotary-only move), so time is distance ÷ F.
- **G95** units/rev — ``F`` is mm/rev, so the spindle speed sets the pace. With no spindle speed the
  duration is unknown rather than guessed.
- **G93** inverse time — ``F`` is *not* a rate. The block takes ``1/F`` minutes regardless of how far
  it travels, so the block's time is computed once and shared out along its length.

Rapids ignore ``F`` entirely and use per-axis ``max_rapid``: a coordinated rapid takes as long as its
slowest axis needs, which is why the per-axis rates matter rather than a single vector rate.

Where a rate is unknown the duration is **0.0 and counted**, not invented. `sim/timing` cannot fix a
program with no feed rate — the verifier already reports that as an error — but a timeline that
silently pretends such a move is instantaneous should at least be able to say how much of itself is
missing.
"""

import math
from dataclasses import dataclass

import numpy as np

from foursight.machine.profile import MachineProfile
from foursight.parser.model import Command

SECONDS_PER_MINUTE = 60.0
_LINEAR_AXES = ("X", "Y", "Z")
_ROTARY_AXIS = "A"
_INVERSE_TIME = "93"
_UNITS_PER_REV = "95"


@dataclass(slots=True, frozen=True)
class Rates:
    """The effective rates for one block, already clamped to what the machine can do.

    ``None`` means *unknown*, never zero: a missing feed rate and a feed rate of zero are different
    programs and only one of them is a mistake we can describe.
    """

    linear_mm_per_min: float | None = None
    # Two rotary rates, because F means different things depending on the block:
    #   * rotary-only move  -> F *is* degrees/min, so `rotary_feed` applies
    #   * mixed XYZ+A move  -> F governs the linear path and A merely keeps up, limited only by the
    #                          axis maximum, so `rotary_max` applies
    # Collapsing them into one rate makes a mixed move take as long as if F were deg/min: for
    # 100 mm + 3600 deg at F600 that is 360 s instead of 60 s — and 360 s is close enough to the
    # 360.1 s a mm/degree norm would give that the error reads like a different bug entirely.
    rotary_feed_deg_per_min: float | None = None
    rotary_max_deg_per_min: float | None = None
    block_seconds: float | None = None  # G93 only: the whole block takes this long

    @property
    def inverse_time(self) -> bool:
        return self.block_seconds is not None


@dataclass(slots=True, frozen=True)
class Timing:
    """Durations for one block's segments, and how many could not be determined."""

    durations: np.ndarray  # (n,) seconds
    unknown: int = 0

    @property
    def total(self) -> float:
        return float(self.durations.sum())


def rates_for(command: Command, profile: MachineProfile, *, rapid: bool) -> Rates:
    """Work out the rates in force for one block.

    A rapid ignores ``F``; a feed clamps ``F`` to ``limits.max_feed`` when configured, because that
    is what the control would do — using the programmed value would under-report the time for a
    program the verifier is already flagging as over-limit.
    """
    modal = command.modal_snapshot
    rotary_limit = _axis_rate(profile, _ROTARY_AXIS)

    if rapid:
        return Rates(
            linear_mm_per_min=None,  # rapids are per-axis; see _rapid_seconds
            rotary_feed_deg_per_min=rotary_limit,
            rotary_max_deg_per_min=rotary_limit,
        )

    feed = modal.feed
    if feed is None or feed <= 0.0:
        return Rates(rotary_max_deg_per_min=rotary_limit)

    if modal.feed_mode == _INVERSE_TIME:
        # F is 1/minutes for this block: the move takes 1/F minutes however far it goes.
        return Rates(
            block_seconds=SECONDS_PER_MINUTE / feed,
            rotary_feed_deg_per_min=rotary_limit,
            rotary_max_deg_per_min=rotary_limit,
        )

    if modal.feed_mode == _UNITS_PER_REV:
        rpm = modal.spindle_rpm
        if rpm is None or rpm <= 0.0:
            # F is mm/rev and the spindle is not turning, so the feed rate is genuinely unknown.
            return Rates(rotary_max_deg_per_min=rotary_limit)
        feed = feed * rpm

    # The two clamps come from different limits and must be applied to the *unclamped* F
    # independently. `limits.max_feed` is documented in mm/min, so bounding a degrees-per-minute
    # rate with it is the mm/degrees conflation showing up in the limits rather than the geometry:
    # F9000 on a rotary-only move would come out as 3000 deg/min and take 72 s instead of 60 s.
    linear_limit = profile.limits.max_feed
    return Rates(
        linear_mm_per_min=_capped(feed, linear_limit),
        rotary_feed_deg_per_min=_capped(feed, rotary_limit),
        rotary_max_deg_per_min=rotary_limit,
    )


def block_durations(
    lin: np.ndarray,
    rot: np.ndarray,
    rates: Rates,
    profile: MachineProfile,
    *,
    rapid: bool,
) -> Timing:
    """Durations for a block's segments. ``lin`` is ``(n, 2, 3)`` mm, ``rot`` is ``(n, 2)`` degrees.

    The segment-pair form. `polyline_durations` is the same computation over a polyline, and the two share
    `_durations_from_deltas` so there is one implementation of the timing rules rather than two that could
    drift.
    """
    if lin.shape[0] == 0:
        return Timing(durations=np.empty(0, dtype=np.float64))
    return _durations_from_deltas(
        lin[:, 1, :] - lin[:, 0, :], rot[:, 1] - rot[:, 0], rates, profile, rapid=rapid
    )


def polyline_durations(
    points: np.ndarray,
    rotations: np.ndarray,
    rates: Rates,
    profile: MachineProfile,
    *,
    rapid: bool,
) -> Timing:
    """The same, over an ``(n+1, 3)`` polyline and ``(n+1,)`` rotations — **without materializing pairs**.

    This exists for the simulator's hot path. Building the ``(n, 2, 3)`` and ``(n, 2)`` pair arrays cost two
    `np.stack` calls **per block**, 71,428 of them for a 50k-line file, purely so that the differences could
    be taken along an axis the caller already had. The deltas are exactly ``points[1:] - points[:-1]``, which
    is one subtraction on a slice and no allocation of the intermediate.
    """
    if points.shape[0] < 2:
        return Timing(durations=np.empty(0, dtype=np.float64))
    return _durations_from_deltas(
        points[1:] - points[:-1], rotations[1:] - rotations[:-1], rates, profile, rapid=rapid
    )


def _durations_from_deltas(
    linear_delta: np.ndarray,
    rotary_delta: np.ndarray,
    rates: Rates,
    profile: MachineProfile,
    *,
    rapid: bool,
) -> Timing:
    """The timing rules themselves, on per-segment deltas. The single implementation.

    ``linear_delta`` is ``(n, 3)`` mm and ``rotary_delta`` is ``(n,)`` degrees, signed. The **sign is
    discarded here and only here**: a reversed rotary move is still time spent.
    """
    if linear_delta.shape[0] == 1:
        return _single_segment(linear_delta, rotary_delta, rates, profile, rapid=rapid)
    return _vector_durations(linear_delta, rotary_delta, rates, profile, rapid=rapid)


def _vector_durations(
    linear_delta: np.ndarray,
    rotary_delta: np.ndarray,
    rates: Rates,
    profile: MachineProfile,
    *,
    rapid: bool,
) -> Timing:
    """The general path, for any number of segments.

    Named and reachable directly so the equivalence test can drive it on the *same* single-segment input the
    fast path gets. The first attempt at that test padded a stationary second segment instead, which is
    unsound: under inverse time the padding changes the weight denominator, so the two paths were being
    compared on different questions and disagreed for the wrong reason.
    """
    # The norm here spans X, Y and Z only. Including the rotary delta would mix millimetres with degrees.
    linear_distance = np.linalg.norm(linear_delta, axis=1)
    rotary_distance = np.abs(rotary_delta)

    if rates.inverse_time:
        return _inverse_time(rates, linear_distance, rotary_distance)

    linear_seconds = (
        _rapid_seconds(linear_delta, profile)
        if rapid
        else _rate_seconds(linear_distance, rates.linear_mm_per_min)
    )
    # Which rotary rate applies depends on whether anything linear is moving in this block.
    travels_linearly = bool(linear_distance.sum() > 0.0)
    rotary_rate = (
        rates.rotary_max_deg_per_min if travels_linearly else rates.rotary_feed_deg_per_min
    )
    rotary_seconds = _rate_seconds(rotary_distance, rotary_rate)

    # Coordinated motion: the move takes as long as its slowest component. NOT a norm.
    durations = np.maximum(linear_seconds, rotary_seconds)
    unknown = int(np.count_nonzero((durations == 0.0) & _moves(linear_distance, rotary_distance)))
    return Timing(durations=durations, unknown=unknown)


def _single_segment(
    linear_delta: np.ndarray,
    rotary_delta: np.ndarray,
    rates: Rates,
    profile: MachineProfile,
    *,
    rapid: bool,
) -> Timing:
    """The same rules in scalar arithmetic, for a block that produced exactly one segment.

    **Why this exists.** 91% of motion blocks in a realistic program are a single ``G1`` producing one
    segment, and the vector path spends a dozen numpy calls on length-1 arrays to time it — `norm`, `abs`,
    two `_rate_seconds`, `maximum`, `count_nonzero`, and the boolean mask feeding it. Each is a microsecond
    or two of call overhead against nanoseconds of arithmetic, and at 35,714 blocks that was 0.7 s of a
    3.2 s simulate.

    **Why a second implementation is acceptable here, when it usually is not.** The two paths are held
    identical by `test_timing.py::test_the_fast_path_agrees_with_the_vector_path`, which drives both over a
    grid of rate configurations and randomized geometry and asserts **bit-identical** output. A fast path
    that silently disagreed with the general one would produce a time estimate that is wrong only for
    ordinary programs, which is the worst possible distribution of a bug — so the equivalence is proved
    rather than assumed. Any new rule added above must be added here too, and the test will say so.

    Inverse time collapses cleanly: with one segment, ``weights / weights.sum() * total`` is ``total``
    whatever the weights are, and the zero-weight branch spreads the same total over one segment.
    """
    dx, dy, dz = float(linear_delta[0, 0]), float(linear_delta[0, 1]), float(linear_delta[0, 2])
    linear_distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    rotary_distance = abs(float(rotary_delta[0]))

    if rates.inverse_time:
        return Timing(durations=np.array([rates.block_seconds or 0.0], dtype=np.float64))

    if rapid:
        linear_seconds = 0.0
        for index, letter in enumerate(_LINEAR_AXES):
            rate = _axis_rate(profile, letter)
            if rate is None:
                continue
            linear_seconds = max(
                linear_seconds, abs(float(linear_delta[0, index])) / rate * SECONDS_PER_MINUTE
            )
    else:
        linear_seconds = _scalar_seconds(linear_distance, rates.linear_mm_per_min)

    # Which rotary rate applies depends on whether anything linear is moving in this block.
    rotary_rate = (
        rates.rotary_max_deg_per_min if linear_distance > 0.0 else rates.rotary_feed_deg_per_min
    )
    rotary_seconds = _scalar_seconds(rotary_distance, rotary_rate)

    # Coordinated motion: the move takes as long as its slowest component. NOT a norm.
    duration = max(linear_seconds, rotary_seconds)
    moves = linear_distance > 0.0 or rotary_distance > 0.0
    unknown = 1 if duration == 0.0 and moves else 0
    return Timing(durations=np.array([duration], dtype=np.float64), unknown=unknown)


def _scalar_seconds(distance: float, rate_per_minute: float | None) -> float:
    """`_rate_seconds` for one value. Same guard: an absent or non-positive rate is unknown, not instant."""
    if rate_per_minute is None or rate_per_minute <= 0.0:
        return 0.0
    return distance / rate_per_minute * SECONDS_PER_MINUTE


def _moves(linear_distance: np.ndarray, rotary_distance: np.ndarray) -> np.ndarray:
    """Segments that actually travel, so a zero duration on them means "unknown", not "stationary"."""
    return (linear_distance > 0.0) | (rotary_distance > 0.0)


def _rate_seconds(distance: np.ndarray, rate_per_minute: float | None) -> np.ndarray:
    if rate_per_minute is None or rate_per_minute <= 0.0:
        return np.zeros_like(distance)
    return distance / rate_per_minute * SECONDS_PER_MINUTE


def _rapid_seconds(linear_delta: np.ndarray, profile: MachineProfile) -> np.ndarray:
    """A coordinated rapid takes as long as its slowest axis, so each axis is timed separately."""
    seconds = np.zeros(linear_delta.shape[0], dtype=np.float64)
    for index, letter in enumerate(_LINEAR_AXES):
        rate = _axis_rate(profile, letter)
        if rate is None:
            continue
        seconds = np.maximum(seconds, np.abs(linear_delta[:, index]) / rate * SECONDS_PER_MINUTE)
    return seconds


def _inverse_time(rates: Rates, linear_distance: np.ndarray, rotary_distance: np.ndarray) -> Timing:
    """G93: the block has a fixed duration, shared along its length so the speed stays constant.

    Splitting evenly per segment instead would make the tool appear to slow down through the finely
    tessellated parts of an arc.
    """
    total = rates.block_seconds or 0.0
    weights = linear_distance if linear_distance.sum() > 0.0 else rotary_distance
    weight_sum = float(weights.sum())
    if weight_sum <= 0.0:
        # A block with a duration but no travel: spread it evenly rather than losing the time.
        even = np.full(linear_distance.shape[0], total / max(1, linear_distance.shape[0]))
        return Timing(durations=even)
    return Timing(durations=weights / weight_sum * total)


def _capped(feed: float, limit: float | None) -> float:
    """``feed`` bounded by ``limit`` when one is configured. An absent limit does not clamp."""
    if limit is None or limit <= 0.0:
        return feed
    return min(feed, limit)


def _axis_rate(profile: MachineProfile, letter: str) -> float | None:
    axis = profile.axes.get(letter)
    return None if axis is None else axis.max_rapid
