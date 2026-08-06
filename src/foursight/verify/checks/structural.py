"""Structural checks (T1.7): syntax, modal conflicts, unknown codes, unsupported motion.

This module is where PLAN.md's governing principle is enforced. The taxonomy line it draws is not
cosmetic:

- An unrecognized code that **never touches position** is a ``warning``; the path still renders.
- A recognized code that **changes how motion is interpreted** but is not implemented in v1 is
  ``unsupported``. Under an active G81 a block of bare ``X10 Y10`` is a drill cycle, and drawing a
  straight line through the hole positions is exactly the confidently-wrong output we refuse.

Spans, not blocks. A canned cycle or a compensation region gets **one** diagnostic covering its
extent, because twenty holes should not produce twenty identical complaints. The span boundaries
come from the resolver's already-computed `Command.motion` and `ModalState.cutter_comp` rather than
being re-derived here.
"""

from collections.abc import Iterable, Iterator, Sequence

from foursight.parser.model import CANNED_CYCLE_CODES, Command, ParseErrorKind
from foursight.verify.report import Diagnostic, Severity
from foursight.verify.rules import Program, Rule, register_rule

# Codes v1 interprets. Anything here produces no diagnostic of its own.
INTERPRETED_GCODES = frozenset({
    "0", "1", "2", "3", "4",
    "17", "18", "19",
    "20", "21",
    "28", "28.1", "30", "30.1",
    "40",                                    # cutter comp CANCEL is interpreted; 41/42 are not
    "43", "44", "49",
    "53",
    "54", "55", "56", "57", "58", "59", "59.1", "59.2", "59.3",
    "61", "61.1", "64",                      # path-control modes: see note below
    "80",                                    # canned-cycle CANCEL
    "90", "91", "90.1", "91.1",
    "93", "94", "95",
    "98", "99",                              # canned-cycle return mode; only meaningful with 8x
})  # fmt: skip

# G61/G61.1/G64 are accepted silently rather than warned about. They select exact-stop versus
# blending, which changes how the machine *corners* but not the programmed centreline we render, so
# there is nothing about the toolpath to warn on — and G64 appears in nearly every LinuxCNC program,
# so warning would be pure noise. G98/G99 are likewise inert without a canned cycle, and a canned
# cycle already reports itself.

INTERPRETED_MCODES = frozenset({"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "30"})

# Recognized, motion-affecting, and NOT interpreted in v1 → `unsupported`, never `warning`.
# Borrowed from the parse layer so `sim` and `verify` cannot disagree about what a cycle is.
CANNED_CYCLES = CANNED_CYCLE_CODES
CUTTER_COMP = frozenset({"41", "42"})
# Per-occurrence rather than span-based: these are one-shot or rarely repeated.
UNSUPPORTED_ONE_SHOT: dict[str, str] = {
    "10": "G10 sets tool/offset table values, which v1 does not model",
    "33": "G33 spindle-synchronized motion is not interpreted in v1",
    "38.2": "G38.2 probing motion is not interpreted in v1",
    "38.3": "G38.3 probing motion is not interpreted in v1",
    "38.4": "G38.4 probing motion is not interpreted in v1",
    "38.5": "G38.5 probing motion is not interpreted in v1",
    "92": "G92 coordinate-system offset is not interpreted in v1",
    "92.1": "G92.1 clears coordinate-system offsets, which v1 does not model",
    "92.2": "G92.2 suspends coordinate-system offsets, which v1 does not model",
    "92.3": "G92.3 restores coordinate-system offsets, which v1 does not model",
}

# ParseError kinds that are genuine malformed input, as opposed to a recognized-but-unsupported
# construct. Split out so the mapping lives in one place rather than in message-text matching.
_SYNTAX_KINDS = frozenset({
    ParseErrorKind.MALFORMED_WORD,
    ParseErrorKind.UNTERMINATED_COMMENT,
    ParseErrorKind.DUPLICATE_WORD,
})  # fmt: skip


@register_rule
class SyntaxErrors(Rule):
    """Malformed words, stray characters, unterminated comments, repeated addresses."""

    rule_id = "structural.syntax-error"
    description = "Malformed word or unparseable text"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind in _SYNTAX_KINDS:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class ModalGroupConflicts(Rule):
    """Two codes from one modal group in a single block: the machine cannot be in both states."""

    rule_id = "structural.modal-group-conflict"
    description = "Two codes from the same modal group in one block"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind is ParseErrorKind.MODAL_GROUP_CONFLICT:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class UnsupportedOwords(Rule):
    """LinuxCNC O-word flow control.

    ``unsupported`` rather than ``error``: the construct is well-formed, and it decides *which*
    motion executes — so the path we would draw is not the path that runs.
    """

    rule_id = "structural.unsupported-oword"
    description = "O-word flow control is not interpreted in v1"
    severity = Severity.UNSUPPORTED

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for error in program.parse_errors:
            if error.kind is ParseErrorKind.UNSUPPORTED_OWORD:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.UNSUPPORTED,
                    line=error.line,
                    message=error.message,
                    offset=error.offset,
                )


@register_rule
class UnknownCodes(Rule):
    """Codes v1 neither interprets nor recognizes as motion-affecting.

    A ``warning``, and the path still renders: per PLAN.md's taxonomy an unrecognized code that
    never touches position stays a warning. The motion-affecting cases are enumerated in the rules
    below rather than guessed at here.
    """

    rule_id = "structural.unknown-code"
    description = "Unknown or unsupported inert G/M code"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        # Report each distinct code once per line, but a code repeated across a 100k-line file
        # should not yield 100k warnings either — so report the first occurrence of each code.
        seen: set[str] = set()
        for command in program.commands:
            for code in command.gcodes:
                if code in INTERPRETED_GCODES or code in _all_unsupported():
                    continue
                if f"G{code}" not in seen:
                    seen.add(f"G{code}")
                    yield self._unknown(command, "G", code)
            for code in command.mcodes:
                if code in INTERPRETED_MCODES:
                    continue
                if f"M{code}" not in seen:
                    seen.add(f"M{code}")
                    yield self._unknown(command, "M", code)

    def _unknown(self, command: Command, letter: str, code: str) -> Diagnostic:
        return Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=command.ref.line_no,
            message=(
                f"{letter}{code} is not recognized; it is assumed inert and the path is drawn "
                f"as if it were absent"
            ),
            offset=command.ref.start,
        )


@register_rule
class UnsupportedMotionCodes(Rule):
    """Recognized, motion-affecting, not interpreted in v1 — reported per span.

    The two span cases are the ones PLAN.md singles out as the most likely source of a wrong
    picture:

    - **Canned cycles (G73/G76/G81–G89).** Under an active cycle, a block carrying only axis words
      is a full drill cycle, not a linear move. The span runs from the cycle code to G80 or the end
      of the program.
    - **Cutter compensation (G41/G42).** While comp is active the real path is offset by the tool
      radius; v1 draws the programmed centreline and says so. The span runs to G40 or program end.
    """

    rule_id = "structural.unsupported-motion"
    description = "Motion-affecting code not interpreted in v1"
    severity = Severity.UNSUPPORTED

    def check(self, program: Program) -> Iterable[Diagnostic]:
        commands = program.commands
        yield from self._cycle_spans(commands)
        yield from self._comp_spans(commands)
        yield from self._one_shots(commands)

    def _cycle_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        for start, end in _spans(commands, lambda c: (c.motion or "") in CANNED_CYCLES):
            cycle = commands[start].motion
            closed = end + 1 < len(commands) and "80" in commands[end + 1].gcodes
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=commands[start].ref.line_no,
                message=(
                    f"G{cycle} canned cycle active from line {commands[start].ref.line_no} to "
                    f"{commands[end].ref.line_no}"
                    f"{'' if closed else ' (never cancelled by G80)'}"
                    "; motion in this span is not interpreted and is not drawn"
                ),
                offset=commands[start].ref.start,
            )

    def _comp_spans(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        active = _spans(commands, lambda c: c.modal_snapshot.cutter_comp is not None)
        for start, end in active:
            code = commands[start].modal_snapshot.cutter_comp
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.UNSUPPORTED,
                line=commands[start].ref.line_no,
                message=(
                    f"G{code} cutter compensation active from line {commands[start].ref.line_no} "
                    f"to {commands[end].ref.line_no}; the displayed path is the programmed "
                    f"centerline, not the compensated path"
                ),
                offset=commands[start].ref.start,
            )

    def _one_shots(self, commands: Sequence[Command]) -> Iterator[Diagnostic]:
        for command in commands:
            for code in command.gcodes:
                reason = UNSUPPORTED_ONE_SHOT.get(code)
                if reason is not None:
                    yield Diagnostic(
                        rule_id=self.rule_id,
                        severity=Severity.UNSUPPORTED,
                        line=command.ref.line_no,
                        message=reason,
                        offset=command.ref.start,
                    )


def _all_unsupported() -> frozenset[str]:
    """Codes handled by `UnsupportedMotionCodes`, so `UnknownCodes` stays quiet about them."""
    return CANNED_CYCLES | CUTTER_COMP | frozenset(UNSUPPORTED_ONE_SHOT)


def _spans(commands: Sequence[Command], active) -> list[tuple[int, int]]:
    """Maximal runs of consecutive commands satisfying `active`, as inclusive index pairs.

    One diagnostic per run rather than per block: a 20-hole drilling cycle is one thing the user
    needs to know about, not twenty.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, command in enumerate(commands):
        if active(command):
            if start is None:
                start = index
        elif start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(commands) - 1))
    return spans
