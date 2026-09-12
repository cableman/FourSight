"""Opening a program: path → text → commands → geometry, plus what the user must be told about it.

**No Qt.** Same split as `batching.py`, for the same reason: the judgement here is *what the window
has to disclose*, and that is worth testing without a display.

The governing principle lands on this module. PLAN.md: *"A previewer that refuses to draw is
recoverable; one that draws the wrong path is worse than no previewer."* The simulator already
refuses to draw what it cannot interpret — a canned cycle is suppressed rather than rendered as a
straight line through the hole positions — but a refusal the user never sees is barely better than a
confident lie. So a summary that reports a **suppressed** span is not a nicety; it is the other half
of that promise. `ProgramSummary.incomplete` is what the window uses to decide it must warn.

The three tiers reach the screen differently:

- **suppressed** — geometry absent. The picture is incomplete and the window says so prominently.
- **unverified** — geometry drawn but not trustworthy (a cutter-comp centreline). Styled distinctly by
  `batching.py`, and named here so the styling has a caption.
- **notes** — modelling caveats that affect the whole program, like unmodelled tool length offsets.

M3 replaces the summary line with a real diagnostics panel. Until then this is the entire channel
through which the viewer learns the drawing is not the whole story, which is why it is tested rather
than assembled inline in the window.
"""

from dataclasses import dataclass
from pathlib import Path

from foursight.fileio.loader import LoadedFile, load
from foursight.machine.profile import MachineProfile
from foursight.parser.model import Command, ParseError
from foursight.parser.resolver import parse
from foursight.sim.simulator import (
    CancelCheck,
    ProgressCallback,
    Simulation,
    SimulationCancelled,
    Span,
    simulate,
)
from foursight.verify.report import Diagnostic
from foursight.verify.rules import Program, verify


@dataclass(frozen=True, slots=True)
class ProgramSummary:
    """Everything the window needs to describe a loaded program honestly."""

    path: Path | None
    encoding: str
    used_encoding_fallback: bool
    parse_errors: int
    commands: int
    segments: int
    duration_s: float
    unknown_durations: int
    suppressed: tuple[Span, ...]
    unverified: tuple[Span, ...]
    notes: tuple[str, ...]

    @property
    def incomplete(self) -> bool:
        """True when geometry is **missing** from the drawing, so the window must warn.

        Deliberately not true for `unverified` spans: those *are* drawn, distinctly styled, and the
        picture is complete even though part of it cannot be trusted. Conflating the two would cry
        wolf on the many real programs that use cutter compensation.
        """
        return bool(self.suppressed)

    @property
    def headline(self) -> str:
        """One line for the status bar."""
        if self.segments == 0:
            return f"{self.commands:,} blocks, no drawable geometry"
        parts = [f"{self.commands:,} blocks", f"{self.segments:,} segments"]
        parts.append(f"{_duration(self.duration_s)} est." if self.duration_s > 0 else "no timing")
        return "  ·  ".join(parts)

    def warnings(self) -> list[str]:
        """What is wrong or unmodelled, most serious first, ready to display verbatim.

        Ordered so the message that changes what the user should *believe about the picture* comes
        before the ones that merely qualify it.
        """
        messages: list[str] = []
        if self.suppressed:
            lines = _line_ranges(self.suppressed)
            messages.append(
                f"Not drawn: {_count(len(self.suppressed), 'span')} at {lines}. "
                "This toolpath is incomplete."
            )
        if self.unverified:
            # The reason comes off the spans, never restated here. Until M16 this line claimed every
            # unverified span was "the programmed centreline, which is not where the tool goes" —
            # true of cutter compensation and flatly wrong about G33, where the centreline is exactly
            # where the tool goes and only the *timing* is unmodelled. A summary that confidently
            # mis-states which part of the picture to distrust is worse than a vaguer one.
            reasons = sorted({span.reason for span in self.unverified})
            detail = reasons[0] if len(reasons) == 1 else "see the diagnostics for why"
            messages.append(
                f"Drawn but unverified: {_count(len(self.unverified), 'span')} at "
                f"{_line_ranges(self.unverified)} — {detail}"
            )
        if self.parse_errors:
            messages.append(
                f"{_count(self.parse_errors, 'line')} could not be parsed and contributed no motion."
            )
        if self.unknown_durations:
            messages.append(
                f"{_count(self.unknown_durations, 'segment')} had no usable feed rate, "
                "so the time estimate is short."
            )
        if self.used_encoding_fallback:
            messages.append(
                f"Not valid UTF-8; decoded as {self.encoding}. Non-ASCII comments may be wrong."
            )
        messages.extend(f"{note[:1].upper()}{note[1:]}" for note in self.notes)
        return messages


@dataclass(frozen=True, slots=True)
class OpenedProgram:
    """A loaded program and everything derived from it."""

    loaded: LoadedFile
    commands: tuple[Command, ...]
    parse_errors: tuple[ParseError, ...]
    simulation: Simulation
    summary: ProgramSummary

    @property
    def path(self) -> Path | None:
        return self.loaded.path


def open_program(
    path: str | Path,
    profile: MachineProfile,
    *,
    block_delete: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
) -> OpenedProgram:
    """Read, parse and simulate a file.

    Exceptions propagate: `OSError` for an unreadable path, `FileLoadError` for something that is not
    G-code at all, and `SimulationCancelled` if ``cancelled`` fires. All three are the caller's to
    present, and swallowing any of them here would leave the window showing the *previous* program
    under a new filename.
    """
    loaded = load(path)
    return open_loaded(
        loaded, profile, block_delete=block_delete, progress=progress, cancelled=cancelled
    )


def open_loaded(
    loaded: LoadedFile,
    profile: MachineProfile,
    *,
    block_delete: bool = False,
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
) -> OpenedProgram:
    """The same, for text already in hand — an editor buffer in M3, and every test here.

    Parsing has no progress seam of its own and accounts for roughly a quarter of the wall clock on a
    large file (1.2 s of 4.6 s at 100k lines), so it is reported as a single step rather than pretended
    to be incremental. Cancellation is checked once after it, because a user who clicks Cancel during a
    long parse should not then wait out the whole simulation.
    """
    result = parse(loaded.text, block_delete=block_delete, dialect=profile.parser_dialect)
    if cancelled is not None and cancelled():
        raise SimulationCancelled("cancelled after parsing")
    simulation = simulate(result.commands, profile, progress=progress, cancelled=cancelled)
    return OpenedProgram(
        loaded=loaded,
        commands=tuple(result.commands),
        parse_errors=tuple(result.errors),
        simulation=simulation,
        summary=summarize(loaded, result.commands, result.errors, simulation),
    )


def verify_program(
    opened: "OpenedProgram", profile: MachineProfile, *, block_delete: bool = False
) -> tuple[Diagnostic, ...]:
    """Run the verifier over an already-loaded program.

    **Kept out of `open_program` deliberately.** Verification costs **5.93 s at 100k lines** with
    interpolated-point checking enabled — comparable to the whole parse-and-simulate pass — so doing it
    eagerly would nearly double the time before anything appears on screen. The toolpath is what the user
    opened the file to see; diagnostics can arrive a moment later, the way a linter fills in behind an
    editor. `background.ProgramLoader` runs this as a second stage after the geometry is already drawn.

    The segments are passed in, so travel limits are checked over *interpolated points* rather than block
    endpoints — an arc can bulge past a limit mid-sweep with both endpoints inside it (T2.8).
    """
    program = Program(
        commands=list(opened.commands),
        profile=profile,
        parse_errors=list(opened.parse_errors),
        segments=opened.simulation.store,
        block_delete=block_delete,
    )
    return tuple(verify(program))


def summarize(
    loaded: LoadedFile,
    commands: list[Command],
    parse_errors: list[ParseError],
    simulation: Simulation,
) -> ProgramSummary:
    return ProgramSummary(
        path=loaded.path,
        encoding=loaded.encoding,
        used_encoding_fallback=loaded.used_fallback,
        parse_errors=len(parse_errors),
        commands=len(commands),
        segments=len(simulation.store),
        duration_s=simulation.duration,
        unknown_durations=simulation.unknown_durations,
        suppressed=simulation.suppressed,
        unverified=simulation.unverified,
        notes=simulation.notes,
    )


def _line_ranges(spans: tuple[Span, ...], limit: int = 3) -> str:
    """``"lines 9-12, 20"``, truncated so a program with hundreds of spans stays readable."""
    shown = [
        f"{span.first_line}"
        if span.first_line == span.last_line
        else f"{span.first_line}-{span.last_line}"
        for span in spans[:limit]
    ]
    text = (
        "line " + ", ".join(shown)
        if len(shown) == 1 and "-" not in shown[0]
        else "lines " + ", ".join(shown)
    )
    remaining = len(spans) - len(shown)
    return f"{text} and {remaining} more" if remaining > 0 else text


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"


def _duration(seconds: float) -> str:
    """``"1h 04m"``, ``"4m 12s"``, ``"9.3s"`` — a cycle time a machinist can compare to a job sheet."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
