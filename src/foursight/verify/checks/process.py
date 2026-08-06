"""Process checks (T1.8): feed, spindle, offsets, tool changes, coolant, program framing.

PLAN.md § Verifier Rules, the Process group. Two conventions run through the module:

- **A missing limit disables its check.** `MachineProfile` represents an unconfigured limit as
  `None`, and none of these rules invents a bound — a confident diagnostic about a machine we know
  nothing about is worse than no diagnostic.
- **Most of these report once, at the first offending line.** "No feed rate ever set" is one fact
  about the program, not one per subsequent block.

Messages render values in the program's *declared* units: "F exceeds 3000 mm/min" against an inch
program is not actionable.
"""

from collections.abc import Iterable

from foursight.machine.state import machine_value, walk
from foursight.verify.report import Diagnostic, Severity, format_feed, format_length
from foursight.verify.rules import Program, Rule, register_rule

CUTTING_MOTIONS = frozenset({"1", "2", "3"})
SPINDLE_ON_CODES = frozenset({"3", "4"})
COOLANT_ON_CODES = frozenset({"7", "8"})
COOLANT_OFF = "9"
PROGRAM_END_CODES = frozenset({"2", "30"})
TOOL_CHANGE = "6"
UNITS_CODES = frozenset({"20", "21"})


def _last_line(program: Program) -> int:
    return program.commands[-1].ref.line_no if program.commands else 1


@register_rule
class UnitsNotSet(Rule):
    """A program that states neither G20 nor G21 relies on the control's power-on default."""

    rule_id = "process.units-not-set"
    description = "Units never explicitly set (G20/G21 missing)"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if any(code in UNITS_CODES for command in program.commands for code in command.gcodes):
            return
        yield Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=program.commands[0].ref.line_no if program.commands else 1,
            message="units never set: no G20 or G21, so the control's default is assumed (mm)",
        )


@register_rule
class NoWorkOffset(Rule):
    """Motion before any G54–G59.

    Without a work offset the travel-limit check cannot run in machine coordinates, which is why
    this is worth saying even though the geometry still renders.
    """

    rule_id = "process.no-work-offset"
    description = "No work offset selected before motion"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command in program.commands:
            if command.motion is None or not command.words:
                continue
            if command.modal_snapshot.offset is None:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    line=command.ref.line_no,
                    message=(
                        "motion before any work offset (G54-G59) is selected; "
                        "travel limits cannot be checked in machine coordinates"
                    ),
                )
            return


@register_rule
class NoFeedRate(Rule):
    """A cutting move with no F ever set. The control would refuse, or move at its last feed."""

    rule_id = "process.no-feed-rate"
    description = "Cutting move with no feed rate ever set"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command in program.commands:
            if command.motion in CUTTING_MOTIONS and command.words:
                if command.modal_snapshot.feed is None:
                    yield Diagnostic(
                        rule_id=self.rule_id,
                        severity=Severity.ERROR,
                        line=command.ref.line_no,
                        message=f"G{command.motion} cutting move with no feed rate ever set",
                    )
                return


@register_rule
class FeedTooHigh(Rule):
    """F beyond the machine's maximum. Reported per offending F word, since each is a distinct
    programming decision the user has to change."""

    rule_id = "process.feed-too-high"
    description = "Feed rate exceeds limits.max_feed"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        limit = program.profile.limits.max_feed
        if limit is None:
            return
        for command in program.commands:
            feed = command.words.get("F")
            if feed is not None and feed > limit:
                units = command.modal_snapshot.units
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=(
                        f"feed {format_feed(feed, units)} exceeds the machine maximum "
                        f"{format_feed(limit, units)}"
                    ),
                )


@register_rule
class SpindleTooHigh(Rule):
    rule_id = "process.spindle-too-high"
    description = "Spindle speed exceeds limits.max_spindle_rpm"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        limit = program.profile.limits.max_spindle_rpm
        if limit is None:
            return
        for command in program.commands:
            speed = command.words.get("S")
            if speed is not None and speed > limit:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=f"spindle speed S{speed:g} exceeds the machine maximum S{limit:g}",
                )


@register_rule
class InverseTimeWithoutFeed(Rule):
    """G93 requires an F on every cutting block: under inverse time, F means 1/minutes for *this*
    move, so a carried-over value is meaningless."""

    rule_id = "process.g93-without-feed"
    description = "G93 inverse-time active with no F on a cutting block"
    severity = Severity.ERROR

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command in program.commands:
            if (
                command.modal_snapshot.feed_mode == "93"
                and command.motion in CUTTING_MOTIONS
                and command.words
                and "F" not in command.words
            ):
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.ERROR,
                    line=command.ref.line_no,
                    message=(
                        "G93 inverse time is active but this cutting block has no F; "
                        "under G93 the F value applies to one move only"
                    ),
                )


@register_rule
class CutBeforeSpindle(Rule):
    rule_id = "process.cut-before-spindle"
    description = "Cutting move before spindle start (M3/M4)"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if not program.profile.safety.require_spindle_before_cut:
            return
        for command in program.commands:
            if command.motion in CUTTING_MOTIONS and command.words:
                if command.modal_snapshot.spindle_on is None:
                    yield Diagnostic(
                        rule_id=self.rule_id,
                        severity=Severity.WARNING,
                        line=command.ref.line_no,
                        message="cutting move before the spindle is started (M3/M4)",
                    )
                return


@register_rule
class NoProgramEnd(Rule):
    rule_id = "process.no-program-end"
    description = "Program lacks M2/M30"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if any(code in PROGRAM_END_CODES for c in program.commands for code in c.mcodes):
            return
        yield Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=_last_line(program),
            message="program does not end with M2 or M30",
        )


@register_rule
class IncrementalAtEnd(Rule):
    """G91 still active at program end: the next program run would start in incremental mode."""

    rule_id = "process.incremental-at-end"
    description = "G91 active at program end"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if not program.commands:
            return
        if program.commands[-1].modal_snapshot.distance == "91":
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=_last_line(program),
                message="G91 incremental mode is still active at program end",
            )


@register_rule
class CoolantWithoutSpindle(Rule):
    """Coolant running with the spindle stopped.

    Coolant state is tracked locally rather than added to `ModalState`: nothing else needs it yet,
    and a field there would have to be threaded through the resolver for one check.
    """

    rule_id = "process.coolant-without-spindle"
    description = "Coolant on with spindle off"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        coolant_on = False
        for command in program.commands:
            for code in command.mcodes:
                if code in COOLANT_ON_CODES:
                    coolant_on = True
                elif code == COOLANT_OFF:
                    coolant_on = False
            if coolant_on and command.modal_snapshot.spindle_on is None:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    line=command.ref.line_no,
                    message="coolant is on while the spindle is stopped",
                )
                return


@register_rule
class ToolChangeWithoutTool(Rule):
    rule_id = "process.toolchange-without-tool"
    description = "M6 with no tool number ever set"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        for command in program.commands:
            if TOOL_CHANGE in command.mcodes and command.modal_snapshot.tool is None:
                yield Diagnostic(
                    rule_id=self.rule_id,
                    severity=Severity.WARNING,
                    line=command.ref.line_no,
                    message="M6 tool change with no T number ever set",
                )


@register_rule
class ToolChangeWithoutRetract(Rule):
    """M6 without a prior retract to safe Z.

    An **unknown** Z counts as a violation, not as a pass: if we cannot establish that the tool was
    clear, we cannot claim the change is safe. That is also what makes this fire at program start,
    where the machine could be anywhere.
    """

    rule_id = "process.toolchange-without-retract"
    description = "M6 without prior retract to safe Z"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        safety = program.profile.safety
        clearance = safety.min_clearance_z
        if not safety.retract_before_toolchange or clearance is None:
            return
        for command, before, _ in walk(program.commands):
            if TOOL_CHANGE not in command.mcodes:
                continue
            z, offset_known = machine_value(before.z, "Z", command, program.profile)
            if z is not None and z >= clearance:
                continue
            units = command.modal_snapshot.units
            where = "position unknown" if z is None else f"Z is {format_length(z, units)}"
            caveat = "" if offset_known else " (assumes zero work offset)"
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=command.ref.line_no,
                message=(
                    f"M6 tool change without a prior retract to "
                    f"{format_length(clearance, units)}: {where}{caveat}"
                ),
            )


@register_rule
class RapidBelowClearance(Rule):
    """A rapid at or below the safe-Z threshold.

    Checked in **machine** coordinates, consistent with PLAN.md's rule that verification happens in
    machine coords. When the active work offset is unknown the programmed value is used and the
    message says so — there is no tier below `warning` to downgrade to, so the caveat carries the
    uncertainty instead.
    """

    rule_id = "process.rapid-below-clearance"
    description = "Rapid below safety.min_clearance_z"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        clearance = program.profile.safety.min_clearance_z
        if clearance is None:
            return
        for command, _, after in walk(program.commands):
            if command.motion != "0" or not command.words:
                continue
            z, offset_known = machine_value(after.z, "Z", command, program.profile)
            if z is None or z >= clearance:
                continue
            units = command.modal_snapshot.units
            caveat = "" if offset_known else " (assumes zero work offset)"
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=command.ref.line_no,
                message=(
                    f"rapid at Z {format_length(z, units)} is below the safe clearance "
                    f"{format_length(clearance, units)}{caveat}"
                ),
            )
