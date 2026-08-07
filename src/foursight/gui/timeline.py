"""Time ↔ segment, driven by ``SegmentStore.duration``. **No Qt.**

`sim/timing.py` gives every segment a duration, so a cumulative sum turns the store into a timeline and a
`searchsorted` turns a scrub position into a segment. One pass to build, O(log N) to query.

Two things here are not arithmetic.

**A program with unknown durations is not a program that takes zero seconds.** `timing` records 0.0 and
*counts* it when there is no usable feed rate, precisely so a timeline can say how much of itself is
missing. A scrubber that presented a partial total as the cycle time would be inventing a number, so
`Timeline` carries `unknown` and the readout says so.

**Zero-duration segments are not individually addressable by time, and that is inherent.** A dwell, a
stationary block, or a rapid at an unknown rate occupies a single instant, so several segments can share
one cumulative time and no scrub position distinguishes them. `searchsorted(..., side="left")` returns the
*earliest* segment at that instant, which keeps every position in range and monotonic. Reaching a specific
zero-length move needs the editor or a click, not the scrubber — which is why all three exist.
"""

from dataclasses import dataclass

import numpy as np

from foursight.sim.segments import SegmentStore


@dataclass(frozen=True, slots=True)
class Timeline:
    """Cumulative time across a store, and the queries a scrubber needs."""

    cumulative: np.ndarray  # (N,) seconds at the *end* of each segment
    unknown: int  # segments with no usable rate, counted by `sim/timing`

    @property
    def total(self) -> float:
        return float(self.cumulative[-1]) if self.cumulative.size else 0.0

    @property
    def segments(self) -> int:
        return int(self.cumulative.size)

    @property
    def is_complete(self) -> bool:
        """False when any segment had no usable feed rate, so the total is short."""
        return self.unknown == 0

    def index_at(self, seconds: float) -> int | None:
        """The segment in progress at ``seconds``, or None for an empty timeline.

        Clamped at both ends: scrubbing to 0 selects the first segment and scrubbing past the total
        selects the last, rather than returning None for a position the slider can legitimately reach.
        """
        if self.segments == 0:
            return None
        index = int(np.searchsorted(self.cumulative, max(0.0, seconds), side="left"))
        return min(index, self.segments - 1)

    def time_at(self, index: int) -> float:
        """Cumulative seconds at the end of ``index``."""
        if not 0 <= index < self.segments:
            return 0.0
        return float(self.cumulative[index])


def build_timeline(store: SegmentStore, unknown: int = 0) -> Timeline:
    """Cumulative durations for ``store``.

    ``unknown`` comes from `Simulation.unknown_durations` rather than being inferred from zeros: a zero
    duration means "does not move" *or* "we could not tell", and only the simulator knows which.
    """
    if len(store) == 0:
        return Timeline(cumulative=np.zeros(0, dtype=np.float64), unknown=unknown)
    return Timeline(cumulative=np.cumsum(store.duration, dtype=np.float64), unknown=unknown)


def format_scrub_time(seconds: float) -> str:
    """``"4:12.3"`` — minutes and seconds with a tenth, for a moving readout.

    Deliberately a different format from `session._duration`, which produces `"1h 05m"` for a *cycle
    time* to be compared against a job sheet. A scrubber needs sub-second resolution and a monotonically
    readable position; a cycle time needs a coarse, comparable figure. Two formats because there are two
    questions, not because one was forgotten.
    """
    seconds = max(0.0, seconds)
    minutes, remainder = divmod(seconds, 60.0)
    if minutes >= 60:
        hours, minutes = divmod(int(minutes), 60)
        return f"{hours}:{minutes:02d}:{remainder:04.1f}"
    return f"{int(minutes)}:{remainder:04.1f}"


def describe_position(timeline: Timeline, index: int | None, line_no: int | None) -> str:
    """The scrubber's readout: where in time, where in the file, and whether the total is trustworthy."""
    if timeline.segments == 0:
        return "No geometry"
    if index is None:
        return f"0:00.0 / {format_scrub_time(timeline.total)}"

    position = format_scrub_time(timeline.time_at(index))
    total = format_scrub_time(timeline.total)
    text = f"{position} / {total}"
    if line_no is not None:
        text += f"   line {line_no}"
    if not timeline.is_complete:
        # Never present a partial total as the cycle time. `timing` counts these deliberately.
        text += f"   ({timeline.unknown:,} segments without a feed rate — total is short)"
    return text
