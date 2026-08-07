"""Steps ``Command`` objects → ``SegmentStore``.

Composes the four pieces built before it: ``MachineState`` for position and machine coordinates,
``interpolate`` for geometry, ``timing`` for durations, and ``SegmentBuilder`` for storage. ``lin`` is
written in **machine coordinates, always** — that is what makes travel-limit verification possible.

The governing principle lands here, because this is the module that decides what gets drawn:

- **A canned cycle is not drawn at all.** Under an active G81 a block of bare ``X10 Y10`` is a full
  drill cycle; a straight line through the hole positions is exactly the confidently-wrong picture
  the plan refuses. The whole span is suppressed and reported.
- **Cutter compensation *is* drawn**, as the programmed centreline, and the span is marked
  ``unverified`` so the renderer can style it distinctly. Refusing to draw comp spans would refuse a
  large share of real programs, and the centreline is genuinely what was programmed — it simply is
  not where the tool went.
- **An arc we cannot interpret is not drawn either.** ``interpolate`` returning an error means the
  geometry was undefined, so the block is suppressed with the reason attached.

Spans are reported as source-line ranges rather than as an extra ``SegmentStore`` column: every
segment already carries ``line[i]``, so a mask is one ``np.isin`` away and the store keeps the exact
six columns PLAN.md specifies.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

from foursight.machine.profile import MachineProfile
from foursight.machine.state import MachineState, Move, Position, Step
from foursight.parser.model import CANNED_CYCLE_CODES, Command
from foursight.sim.interpolate import interpolate
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.timing import block_durations, rates_for

ProgressCallback = Callable[[int, int], None]
CancelCheck = Callable[[], bool]


class SimulationCancelled(Exception):  # noqa: N818 - not an error; see below
    """Raised when ``cancelled()`` returns True partway through ``simulate``.

    Deliberately *not* named ``...Error``, against ruff's N818. Cancellation is a normal outcome the
    user asked for, not a failure, and `except SimulationCancelled:` is what the catch site should
    read like — the same reasoning that gives the stdlib `StopIteration` and `GeneratorExit` no such
    suffix. Calling it an error would push callers toward reporting a problem to the user who just
    pressed Cancel.

    Raising rather than returning the partial ``Simulation`` is deliberate. A half-stepped program is
    a **truncated toolpath**, and handing one back invites a caller to draw it as though the program
    ended there — the confidently-wrong picture the plan exists to prevent. There is no honest way to
    render "the first 40% of this program"; the caller should keep showing whatever it had before.
    """


@dataclass(slots=True, frozen=True)
class Span:
    """A run of source lines the simulator could not fully honour.

    ``drawn`` distinguishes the two tiers that matter to the renderer: a suppressed span has no
    geometry at all, while an unverified span has geometry that must not be presented as the truth.
    """

    first_line: int
    last_line: int
    reason: str
    drawn: bool

    def contains(self, line: int) -> bool:
        return self.first_line <= line <= self.last_line


@dataclass(slots=True)
class Simulation:
    """The result of stepping a program."""

    store: SegmentStore
    spans: tuple[Span, ...] = ()
    notes: tuple[str, ...] = ()
    unknown_durations: int = 0

    @property
    def suppressed(self) -> tuple[Span, ...]:
        return tuple(span for span in self.spans if not span.drawn)

    @property
    def unverified(self) -> tuple[Span, ...]:
        return tuple(span for span in self.spans if span.drawn)

    def unverified_mask(self) -> np.ndarray:
        """Per-segment mask for the spans that were drawn but cannot be trusted.

        Derived from ``line[i]`` rather than stored, which is why the store needs no extra column.
        """
        mask = np.zeros(len(self.store), dtype=bool)
        for span in self.unverified:
            mask |= (self.store.line >= span.first_line) & (self.store.line <= span.last_line)
        return mask

    @property
    def duration(self) -> float:
        return float(self.store.duration.sum())


def simulate(
    commands: Sequence[Command],
    profile: MachineProfile,
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
    progress_interval: int = 2000,
) -> Simulation:
    """Step a command list into a ``SegmentStore``.

    ``progress`` is called as ``(done, total)`` every ``progress_interval`` commands, and ``cancelled``
    is polled on the same tick. Both exist so T2.9 can drive this from a QThread without the simulator
    knowing anything about Qt — a `threading.Event.is_set` satisfies ``cancelled`` exactly.

    Polling on the interval rather than per command is what keeps this free: at the default 2000 the
    check costs about 43 calls for a 100k-line program, against ~40 µs of work per block.

    A cancelled run raises `SimulationCancelled` and returns nothing. See that exception for why a
    partial `Simulation` would be the wrong thing to hand back.
    """
    state = MachineState(profile)
    # The machine is assumed to start at its reference position. This is the universal convention —
    # a control powers up knowing only where it is homed, and every previewer makes the same
    # assumption — and it is not noted, because a note present on every single program says nothing.
    #
    # The alternative was tried and is worse: refusing to draw until every axis is established means
    # the first move along each axis is undrawable, so a program that never mentions Y renders as
    # *empty*. That loses real geometry to avoid a bounded, conventional assumption about one
    # approach move's origin. The cut geometry is identical either way.
    #
    # This is deliberately NOT the same as a *lost* position: `MachineState` clears the position and
    # sets `position_lost` after an undrawable G28, and those moves are suppressed rather than
    # assumed, because there we had a position and no longer do.
    state.programmed = Position(0.0, 0.0, 0.0, 0.0)
    builder = SegmentBuilder()
    run = _Run(builder=builder, profile=profile)
    total = len(commands)

    for index, command in enumerate(commands):
        run.step(state.apply(command), command, position_lost=state.position_lost)
        if (index + 1) % progress_interval == 0:
            if cancelled is not None and cancelled():
                raise SimulationCancelled(f"cancelled after {index + 1:,} of {total:,} blocks")
            if progress is not None:
                progress(index + 1, total)
    # Checked once more at the end so a cancellation arriving during the final partial interval is
    # still honoured, rather than being reported as a completed simulation.
    if cancelled is not None and cancelled():
        raise SimulationCancelled(f"cancelled after {total:,} of {total:,} blocks")
    if progress is not None:
        progress(total, total)

    run.close_open_span(commands)
    store = builder.finalize()
    return Simulation(
        store=store,
        spans=tuple(run.spans),
        notes=tuple(sorted(run.notes)),
        unknown_durations=run.unknown_durations,
    )


@dataclass(slots=True)
class _Run:
    """Mutable state while stepping: the builder, the open span, and what has been noticed."""

    builder: SegmentBuilder
    profile: MachineProfile
    spans: list[Span] = field(default_factory=list)
    notes: set[str] = field(default_factory=set)
    unknown_durations: int = 0
    _open: Span | None = None

    def step(self, step: Step, command: Command, *, position_lost: bool = False) -> None:
        line = command.ref.line_no
        if step.tool_length_unmodelled:
            self.notes.add(
                "tool length offsets (G43/G44) are not modelled: Z is relative to the spindle, "
                "not the tool tip"
            )

        self._track_span(command, line)

        if self._suppressing():
            return
        if step.undrawable is not None:
            self._one_line_span(line, step.undrawable, drawn=False)
            return
        for move in step.moves:
            self._emit(move, command, line, position_lost=position_lost)

    # ------------------------------------------------------------------ spans

    def _track_span(self, command: Command, line: int) -> None:
        """Open, extend or close the span covering this block."""
        cycle = command.motion if command.motion in CANNED_CYCLE_CODES else None
        comp = command.modal_snapshot.cutter_comp

        if cycle is not None:
            self._extend(
                line,
                f"G{cycle} canned cycle is not interpreted in v1; this span is not drawn",
                drawn=False,
            )
            return
        if comp is not None:
            self._extend(
                line,
                f"G{comp} cutter compensation active; the drawn path is the programmed "
                "centerline, not the compensated path",
                drawn=True,
            )
            return
        self._close(line - 1)

    def _extend(self, line: int, reason: str, *, drawn: bool) -> None:
        if self._open is not None and self._open.reason == reason:
            self._open = Span(self._open.first_line, line, reason, drawn)
            return
        self._close(line - 1)
        self._open = Span(line, line, reason, drawn)

    def _close(self, last_line: int) -> None:
        if self._open is None:
            return
        self.spans.append(
            Span(
                self._open.first_line,
                max(self._open.first_line, last_line),
                self._open.reason,
                self._open.drawn,
            )
        )
        self._open = None

    def close_open_span(self, commands: Sequence[Command]) -> None:
        """A span running to the end of the program still needs recording."""
        if self._open is not None:
            self._close(commands[-1].ref.line_no if commands else self._open.last_line)

    def _one_line_span(self, line: int, reason: str, *, drawn: bool) -> None:
        self.spans.append(Span(line, line, reason, drawn))

    def _suppressing(self) -> bool:
        return self._open is not None and not self._open.drawn

    # ------------------------------------------------------------------ geometry

    def _emit(self, move: Move, command: Command, line: int, *, position_lost: bool) -> None:
        start = _xyz(move.start)
        end = _xyz(move.end)
        if start is None or end is None:
            if position_lost:
                # We had a position and lost it (an undrawable G28). Assuming one here would
                # fabricate the rest of the program's geometry.
                self._one_line_span(
                    line, "machine position was lost, so this move cannot be drawn", drawn=False
                )
                return
            # Unreachable in practice: `simulate` seeds the start position, so a None here means
            # the position was lost without `position_lost` being set. Refuse rather than guess.
            self._one_line_span(
                line,
                "machine position is not established, so this move cannot be drawn",
                drawn=False,
            )
            return

        rot_start = move.start.a or 0.0
        rot_end = move.end.a or 0.0
        path = interpolate(
            command, start, end, rot_start, rot_end, self.profile.tolerance, self.profile.kinematics
        )
        if not path.ok:
            self._one_line_span(line, path.error or "arc could not be interpolated", drawn=False)
            return
        if len(path.points) < 2:
            return

        durations = self._durations(path.points, path.rotations, command, rapid=move.rapid)
        self.builder.add_polyline(
            path.points,
            Kind.RAPID if move.rapid else Kind.FEED,
            line,
            rotations=path.rotations,
            durations=durations,
        )

    def _durations(
        self, points: np.ndarray, rotations: np.ndarray, command: Command, *, rapid: bool
    ) -> np.ndarray:
        """Time the block's segments, in the exact column layout `timing` expects."""
        lin = np.stack([points[:-1], points[1:]], axis=1)
        rot = np.stack([rotations[:-1], rotations[1:]], axis=1)
        rates = rates_for(command, self.profile, rapid=rapid)
        timing = block_durations(lin, rot, rates, self.profile, rapid=rapid)
        self.unknown_durations += timing.unknown
        return timing.durations


def _xyz(position) -> np.ndarray | None:
    """A position as ``(3,)`` XYZ, or None if any linear axis was never established."""
    if position.x is None or position.y is None or position.z is None:
        return None
    return np.array([position.x, position.y, position.z], dtype=np.float64)


def _zeroed(position) -> np.ndarray:
    """The same, treating an unestablished axis as sitting at the machine reference."""
    return np.array([position.x or 0.0, position.y or 0.0, position.z or 0.0], dtype=np.float64)


def simulate_text(
    text: str, profile: MachineProfile, *, block_delete: bool = False
) -> tuple[Simulation, Iterable[str]]:
    """Convenience for tests and the CLI: parse then simulate. Returns parse error messages too."""
    from foursight.parser.resolver import parse

    result = parse(text, block_delete=block_delete)
    return simulate(result.commands, profile), [error.message for error in result.errors]
