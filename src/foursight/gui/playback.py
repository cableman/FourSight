"""Playback: wall clock → program time, and program time → a point on the toolpath. **No Qt.**

`timeline.py` answers "which segment is in progress at *t*". That is everything a scrubber needs,
because a scrubber only ever moves when the user moves it. A *player* needs two more things, and both
of them are the kind of arithmetic that is wrong in ways nobody notices, so they live here rather than
in the widget.

**The position is a float, and the slider is not.** `TimelineBar` works in integer thousandths of the
total — the right choice for a scrubber, and unusable for an animation: on a one-hour program a tick is
3.6 seconds, so a marker driven from the slider would jump rather than move. The clock owns the
position; the slider becomes a *view* of it.

**Wall time is not program time.** A 40-hour job watched at 1× is not a preview, and a two-second one is
over before the eye finds it, so playback advances `speed` program-seconds per wall-second and the
readout keeps saying program time. `advance` takes the elapsed wall time as an argument instead of
reading a clock itself: the caller measures it (`QElapsedTimer`, which is monotonic), and the tests
drive playback frame by frame without sleeping.

**A program with no usable feed rate cannot be played, and says so by refusing.** `sim/timing` records
0.0 for a move it cannot time, so such a program has geometry and a zero total. `play()` on one is a
no-op — the same judgement that already disables the scrub slider. The alternative is a play button
that lights up and does nothing, which reads as a broken player rather than an untimed program.
"""

from dataclasses import dataclass

import numpy as np

from foursight.gui.timeline import Timeline
from foursight.sim.segments import SegmentStore

#: Program seconds per wall second. Powers of ten because the useful span is enormous — a facing pass
#: and an overnight job want different orders of magnitude, not different fractions of one.
SPEEDS = (1.0, 10.0, 100.0, 1000.0)


@dataclass(slots=True)
class Playback:
    """The transport state: where we are, how fast, and whether we are moving.

    Mutable, unlike `Timeline`, because it *is* the moving part. It holds no geometry and no Qt — the
    widget owns the timer, this owns the arithmetic.
    """

    total: float
    seconds: float = 0.0
    speed: float = 1.0
    playing: bool = False

    @property
    def playable(self) -> bool:
        """False when there is no time to play through. See the module docstring."""
        return self.total > 0.0

    @property
    def at_end(self) -> bool:
        return self.seconds >= self.total

    def play(self) -> None:
        """Start playing, rewinding first if we are sitting at the end.

        Without the rewind, pressing play on a finished program does nothing visible and looks broken.
        """
        if not self.playable:
            return
        if self.at_end:
            self.seconds = 0.0
        self.playing = True

    def pause(self) -> None:
        self.playing = False

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def reset(self) -> None:
        """Rewind to the start and stop. Distinct from `pause`, which leaves the position alone."""
        self.seconds = 0.0
        self.playing = False

    def seek(self, seconds: float) -> None:
        """Jump to ``seconds``, clamped to the program.

        Deliberately does **not** touch `playing`: seeking during playback is a seek, not a stop, and
        making the user press play again after every drag is the difference between a player and a
        toy.
        """
        self.seconds = min(max(0.0, seconds), max(0.0, self.total))

    def set_speed(self, speed: float) -> None:
        """Change the multiplier. Takes effect on the next `advance`, so the position never jumps."""
        if speed <= 0.0:
            raise ValueError(f"playback speed must be positive, got {speed}")
        self.speed = speed

    def advance(self, dt_wall: float) -> float:
        """Advance by ``dt_wall`` wall-seconds and return the new position in program seconds.

        Pauses itself on reaching the end rather than sitting there consuming frames, so the widget can
        restore the play icon by asking `playing` instead of comparing floats.

        A negative ``dt_wall`` is treated as zero. `QElapsedTimer` is monotonic so it should never
        produce one, but the failure mode if it ever did — the tool walking backwards through a
        program that only moves forwards — is exactly the kind of confidently wrong output the plan
        forbids, and clamping costs nothing.
        """
        if not self.playing or not self.playable:
            return self.seconds
        self.seconds = min(self.total, self.seconds + max(0.0, dt_wall) * self.speed)
        if self.at_end:
            self.playing = False
        return self.seconds


def marker_point(
    store: SegmentStore,
    timeline: Timeline,
    seconds: float,
    *,
    part_coordinates: bool = False,
) -> np.ndarray | None:
    """Where the tool is at ``seconds``: a ``(3,)`` float64 point, or None for an empty program.

    Interpolated *within* the segment in progress, not snapped to its endpoints. Snapping is what makes
    a marker crawl in jerks on a coarse program, and the interpolation is free: a segment is already a
    straight chord by the time it reaches the store — `sim/interpolate` tessellated the arcs and the
    rotary blending — so a point along it is within `tolerance.arc_chord` of the real path by
    construction. It is emphatically **not** free to interpolate anywhere else: interpolating in `lin`
    and then rotating by the segment's start angle would cut the corner of a wrapped path, which is the
    endpoint-only mistake PLAN.md warns about, one level down. Reading the *displayed* array avoids the
    question entirely.

    Reads whichever array is on screen, and **raises** rather than falling back when part coordinates
    are asked for without a transform — the same contract as `viewport3d._highlight_vertices`. A marker
    drawn in machine coordinates over a part-coordinate toolpath would float somewhere near the path
    and look like a rendering artefact rather than a lie about where the tool is.

    Refuses a ``timeline`` and ``store`` of different lengths for the same reason the highlight refuses
    a wrong-length mask: the timeline of the *previous* program indexes this one perfectly plausibly,
    and the marker would ride a position that was never in the file.
    """
    if timeline.segments != len(store):
        raise ValueError(
            f"timeline has {timeline.segments} segments, store has {len(store)} — "
            "they describe different programs"
        )

    index = timeline.index_at(seconds)
    if index is None or len(store) == 0:
        return None

    source = store.lin
    if part_coordinates:
        if store.lin_part is None:
            raise ValueError("part coordinates are displayed but no display transform is attached")
        source = store.lin_part

    duration = float(store.duration[index])
    if duration <= 0.0:
        # An instant has no inside. Several zero-duration segments can share one cumulative time, so
        # there is nothing to interpolate along and nothing to choose between them: take the start.
        fraction = 0.0
    else:
        start = timeline.time_at(index - 1)  # time_at(-1) is 0.0, so index 0 needs no special case
        fraction = min(1.0, max(0.0, (seconds - start) / duration))

    begin = source[index, 0]
    return begin + fraction * (source[index, 1] - begin)
