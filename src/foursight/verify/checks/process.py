"""Process checks (T1.8): feed, spindle, offsets, tool changes, coolant, program framing.

PLAN.md § Verifier Rules, the Process group. Two conventions run through the module:

- **A missing limit disables its check.** `MachineProfile` represents an unconfigured limit as
  `None`, and none of these rules invents a bound — a confident diagnostic about a machine we know
  nothing about is worse than no diagnostic.
- **Most of these report once, at the first offending line.** "No feed rate ever set" is one fact
  about the program, not one per subsequent block.

Messages render values in the program's *declared* units: "F exceeds 3000 mm/min" against an inch
program is not actionable.

Two rules here break the module's usual frame by reporting a hazard the **commanded** geometry does not
contain. Everything else in `verify/` judges the program; these two judge what the machine will make of
it, which is why their messages name a control setting rather than a line to rewrite.

- `process.rotary-rapid-before-plunge` — a rotary-dominated rapid followed straight away by a plunge is
  safe as written and is drawn correctly, but a control blending the two blocks (Mach3 CV, `G64`) starts
  the descent before the rotation finishes.
- `process.rotary-rapid-short-rotates` — a rapid over a half turn on a control that takes the short way
  round stops a full turn from the commanded angle, and the next cutting block makes up the difference.

They look alike and their remedies do not overlap at all. The first is a *timing* fault and a dwell or
`G61` fixes it; the second leaves the axis **physically in the wrong place**, where no dwell helps. Both
fire on the same blocks of a wrapped-rotary post's profile resets, so a reader who conflates them will
apply the wrong fix and see half the gouge remain.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from foursight.machine.profile import MachineProfile
from foursight.machine.state import (
    Position,
    machine_value,
    offsets_rewritten_from,
    offsets_stale_at,
    walk,
)
from foursight.parser.dialect import DwellUnits
from foursight.parser.model import AXIS_LETTERS, Command
from foursight.verify.report import (
    Diagnostic,
    Severity,
    format_angle,
    format_feed,
    format_length,
)
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
        rewritten_from = offsets_rewritten_from(program.commands)
        for command, before, _ in walk(program.commands):
            if TOOL_CHANGE not in command.mcodes:
                continue
            z, offset_known = machine_value(before.z, "Z", command, program.profile)
            offset_known = offset_known and not offsets_stale_at(
                command.ref.line_no, rewritten_from
            )
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
        rewritten_from = offsets_rewritten_from(program.commands)
        for command, _, after in walk(program.commands):
            if command.motion != "0" or not command.words:
                continue
            z, offset_known = machine_value(after.z, "Z", command, program.profile)
            offset_known = offset_known and not offsets_stale_at(
                command.ref.line_no, rewritten_from
            )
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


#: Feed-rate mode in which `F` is a length per minute, and so comparable to a mm/min limit. Under G93
#: `F` is inverse time in 1/minutes and under G95 it is mm/rev; neither converts without inventing a
#: spindle speed or a block length, so the plunge check stays silent there rather than guessing.
UNITS_PER_MINUTE = "94"
LINEAR_MOTION = "1"


@register_rule
class PlungeFeedTooHigh(Rule):
    """A straight-down G1 faster than `limits.max_plunge_feed`.

    This is the "the machine bangs into the stock" complaint that needs no stock model: entering
    material along Z at the contouring feed is a common post-processor misconfiguration, and it sounds
    exactly like a collision.

    **A plunge moves Z alone.** A block that also moves X or Y is a *ramp*, and ramping in at the
    contouring feed is correct practice rather than a defect — reporting it would make the rule fire on
    most well-written programs, which is how a user learns to ignore a diagnostic. G2/G3 are excluded
    for the same reason: an arc with Z motion is a helical entry, the recommended way into material. A
    moves are excluded too; a coordinated XYZ+A move is not a plunge.

    Motion is judged from **positions, not words**, so a post that restates the unchanged `X10 Y20` on
    its plunge block is still recognized as plunging. An axis whose position was never established is
    treated as motion, so the rule declines to judge rather than guessing — matching this module's
    rule that no position means no claim.

    A **warning**: a rigid machine plunging a centre-cutting drill into aluminium can legitimately
    exceed any figure a profile would name.
    """

    rule_id = "process.plunge-feed-too-high"
    description = "Straight-down G1 plunge above limits.max_plunge_feed"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        limit = program.profile.limits.max_plunge_feed
        if limit is None:
            return
        # Grouped by feed rate, in first-occurrence order. One misconfigured plunge rate is one fact
        # about the program; a drilling job with 200 holes would otherwise bury every other finding.
        offenders: dict[float, list[Command]] = {}
        for command, before, after in walk(program.commands):
            feed = _plunge_feed(command, before, after)
            if feed is not None and feed > limit:
                offenders.setdefault(feed, []).append(command)

        for feed, commands in offenders.items():
            first = commands[0]
            units = first.modal_snapshot.units
            others = "" if len(commands) == 1 else f" ({len(commands) - 1} more like it)"
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=first.ref.line_no,
                message=(
                    f"straight-down plunge at {format_feed(feed, units)} exceeds the plunge limit "
                    f"{format_feed(limit, units)}{others}"
                ),
                offset=first.ref.start,
            )


def _plunge_feed(command: Command, before: Position, after: Position) -> float | None:
    """The active feed rate if this block plunges straight down, else None.

    "Straight down" is all four conditions together: G1, a strictly decreasing Z, and X, Y and A all
    holding station.
    """
    if command.motion != LINEAR_MOTION or not command.words:
        return None
    modal = command.modal_snapshot
    if modal.feed_mode != UNITS_PER_MINUTE or modal.feed is None:
        return None
    if not _descends_alone(before, after):
        return None
    # The *active* feed, not this block's own F word: a plunge usually inherits the rate set earlier.
    return float(modal.feed)


def _descends_alone(before: Position, after: Position) -> bool:
    """True when Z strictly decreases and X, Y and A all hold station.

    Shared with `process.rotary-rapid-before-plunge`, which needs the same notion of "a plunge" for a
    different reason. One definition, because the two rules disagreeing about what a plunge is would
    make one of them quietly stop firing on the blocks the other reports.
    """
    if before.z is None or after.z is None or after.z >= before.z:
        return False
    return all(before.get(letter) == after.get(letter) for letter in ("X", "Y", "A"))


#: G4 dwell, and the threshold above which a P value looks like milliseconds rather than seconds.
#: Not a hard rule — a tool-cooling dwell can legitimately exceed a minute, which is why this is a
#: warning and why nothing is rescaled on the strength of it.
DWELL = "4"
DWELL_SECONDS_SUSPECT = 60.0


@register_rule
class DwellUnitsSuspect(Rule):
    """G4 P far larger than any plausible pause: the post probably emitted milliseconds.

    **Owed since M1**, promised by PLAN.md § Dialect Divergences and never implemented. LinuxCNC and
    Mach3 both specify G4 P in seconds; Fanuc uses milliseconds, and posts configured for a Fanuc
    control emit them anyway. `G4 P5000` is then either a 5-second pause or an 83-minute one, and
    the two are not distinguishable from the program.

    A warning, and nothing is rescaled: inferring the units from the magnitude of P would turn a
    legitimate 90-second tool-cooling dwell into 0.09 s, which is the confidently-wrong output the
    plan forbids. The way to *fix* it is `[dialect].dwell_units`, and the message says so.

    Reported once, at the first offending block, matching this module's convention: one
    misconfigured post is one fact about the program, not one per dwell.
    """

    rule_id = "process.dwell-units-suspect"
    description = "G4 P over 60 s, which is more likely a millisecond value"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        if program.profile.dialect.dwell_units == DwellUnits.MILLISECONDS:
            # The profile has already answered the question; P5000 really is 5 seconds.
            return
        offenders = [
            command
            for command in program.commands
            if DWELL in command.gcodes and command.words.get("P", 0.0) > DWELL_SECONDS_SUSPECT
        ]
        if not offenders:
            return
        first = offenders[0]
        seconds = first.words["P"]
        others = "" if len(offenders) == 1 else f" ({len(offenders) - 1} more like it.)"
        yield Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=first.ref.line_no,
            message=(
                f"G4 P{seconds:g} is a dwell of {seconds:g} s ({seconds / 60:.0f} min): G4 P is "
                f"seconds under this dialect, and a value this large usually means the post emitted "
                f'milliseconds. Set [dialect].dwell_units = "milliseconds" if that is intended.'
                f"{others}"
            ),
            offset=first.ref.start,
        )


#: Rapid motion, and the axes a coordinated rapid times separately. A rapid ignores `F` and runs each
#: axis at its own `max_rapid`, so the block takes as long as its slowest participant needs — the same
#: rule `sim/timing._rapid_seconds` applies, restated here for one block at a time rather than borrowed,
#: because this rule needs the *per-axis* times side by side and that function returns only the maximum.
RAPID_MOTION = "0"
ROTARY_AXIS = "A"
LINEAR_AXES = ("X", "Y", "Z")
SECONDS_PER_MINUTE = 60.0


@dataclass(slots=True, frozen=True)
class _RotaryRapid:
    """A rapid whose duration is set by the rotary axis rather than by any linear one."""

    travel: float  # degrees
    rotary_seconds: float
    linear_seconds: float


@register_rule
class RotaryRapidBeforePlunge(Rule):
    """A rotary-dominated rapid immediately followed by a plunge: the corner a CV control rounds off.

    Wrapped-rotary posts reposition between passes with a single `G0` that unwinds A by hundreds of
    degrees while the linear axes travel a comparatively short distance, then plunge on the very next
    block. The *programmed* path is safe — Z stays clear for the whole rapid, which is why every
    geometric rule here passes it and why the viewport draws it correctly. The machine is where it goes
    wrong: under constant-velocity blending (Mach3 CV, LinuxCNC `G64`) the control rounds the corner
    between the two blocks and starts the descent before the rotation has finished, cutting a
    circumferential groove that ends where the plunge finally lands.

    **This is a warning about the control, not about the file**, and it is the one rule in this module
    that reports a hazard the commanded geometry does not contain. It earns its place because the
    failure is invisible everywhere else: no travel limit is exceeded, no rapid is below clearance, and
    the preview is right. The first evidence is a gouge in the workpiece.

    **"Rotary-dominated" is measured in seconds, not degrees.** A coordinated rapid takes as long as its
    slowest axis, so the question is whether A is that axis — 488° at 3600 deg/min is 8.1 s against
    110 mm at 5000 mm/min, which is 1.3 s, and only the ratio of *times* says the rapid is essentially a
    pure rotation. Comparing degrees against millimetres would be the meaningless cross-unit norm the
    rotary column exists to prevent, and a degree threshold would fire on a fast rotary axis and stay
    silent on a slow one, which is backwards.

    **An intervening M-code or `G4` means no finding.** Both flush a control's look-ahead, and a dwell
    between the two blocks is precisely the remedy the message recommends — so a program that already
    has one is not at risk and must not be told that it is.

    **Every occurrence is reported**, unlike most of this module. The usual "report once, at the first
    offending line" applies to facts about the *program* — one missing G21, one misconfigured plunge
    rate — where the second finding tells the user nothing new. Here each finding is a different place
    on the workpiece, at a different angle and a different Y, and the user has to go and look at each
    one. Summarising them into a count and hiding the rest is how the second gouge goes unfound.
    """

    rule_id = "process.rotary-rapid-before-plunge"
    description = "Rotary-dominated rapid immediately before a plunge (CV blending gouges)"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        profile = program.profile
        clearance = profile.safety.min_clearance_z
        if clearance is None:
            # No clearance plane configured, so there is no way to say the plunge entered material.
            return
        steps = list(walk(program.commands))
        for index, (command, before, after) in enumerate(steps):
            rapid = _rotary_dominated_rapid(command, before, after, profile)
            if rapid is None:
                continue
            following = _next_moving_block(steps, index + 1)
            if following is None:
                continue
            plunge, plunge_before, plunge_after = following
            depth = _plunge_depth(plunge, plunge_before, plunge_after, clearance, profile)
            if depth is None:
                continue
            units = plunge.modal_snapshot.units
            yield Diagnostic(
                rule_id=self.rule_id,
                severity=Severity.WARNING,
                line=command.ref.line_no,
                message=(
                    f"rapid turns A {format_angle(rapid.travel)}, which takes "
                    f"{rapid.rotary_seconds:.1f} s at axes.a.max_rapid against "
                    f"{rapid.linear_seconds:.1f} s of linear travel, and line {plunge.ref.line_no} "
                    f"plunges to Z {format_length(depth, units)} immediately after. A control "
                    f"blending the corner (Mach3 CV, or G64) starts the plunge before the rotation "
                    f"finishes and cuts a groove around the part: use exact stop (G61) for this "
                    f"program, or put a dwell between the two blocks"
                ),
                offset=command.ref.start,
            )


def _rotary_dominated_rapid(
    command: Command, before: Position, after: Position, profile: MachineProfile
) -> _RotaryRapid | None:
    """This rapid's rotary travel and timing, when A is the axis that sets its duration.

    An unconfigured `axes.a.max_rapid` disables the rule rather than defaulting a rate, matching this
    module's convention: a missing limit is not a bound to invent.
    """
    if command.motion != RAPID_MOTION or not command.words:
        return None
    if before.a is None or after.a is None:
        return None
    travel = abs(after.a - before.a)
    rotary_seconds = _axis_seconds(travel, profile, ROTARY_AXIS)
    if rotary_seconds is None or rotary_seconds <= 0.0:
        return None
    linear_seconds = 0.0
    for letter in LINEAR_AXES:
        start, end = before.get(letter), after.get(letter)
        if start is None or end is None:
            continue
        seconds = _axis_seconds(abs(end - start), profile, letter)
        if seconds is not None:
            linear_seconds = max(linear_seconds, seconds)
    if rotary_seconds <= linear_seconds:
        return None
    return _RotaryRapid(travel=travel, rotary_seconds=rotary_seconds, linear_seconds=linear_seconds)


def _axis_seconds(distance: float, profile: MachineProfile, letter: str) -> float | None:
    """How long one axis needs for `distance` at its rapid rate. `None` when the rate is unknown."""
    axis = profile.axes.get(letter)
    if axis is None or axis.max_rapid is None or axis.max_rapid <= 0.0:
        return None
    return distance / axis.max_rapid * SECONDS_PER_MINUTE


def _next_moving_block(
    steps: list[tuple[Command, Position, Position]], start: int
) -> tuple[Command, Position, Position] | None:
    """The next block that actually moves, or `None` if something between it and `start` would not blend.

    An M-code or a `G4` dwell flushes the control's look-ahead, so the two motions cannot be run
    together and there is nothing to report.
    """
    for command, before, after in steps[start:]:
        if command.mcodes or DWELL in command.gcodes:
            return None
        if AXIS_LETTERS & set(command.words):
            return command, before, after
    return None


def _plunge_depth(
    command: Command,
    before: Position,
    after: Position,
    clearance: float,
    profile: MachineProfile,
) -> float | None:
    """The machine Z this block plunges to, when it plunges below `clearance`, else `None`.

    A `G0` descent is excluded: that is `process.rapid-below-clearance`'s finding, and a rule that
    reported it again would double every one of them.
    """
    if command.motion != LINEAR_MOTION or not command.words:
        return None
    if not _descends_alone(before, after):
        return None
    z, _ = machine_value(after.z, "Z", command, profile)
    if z is None or z >= clearance:
        return None
    return z


#: Half and whole turns. A `G0` commanding a short-rotating axis further than `HALF_TURN` is where the
#: control's choice stops agreeing with the commanded angle. `_ANGLE_EPSILON` only guards the exact
#: half-turn comparison against decimal noise in the parsed word; it is not a tolerance to tune.
HALF_TURN = 180.0
FULL_TURN = 360.0
_ANGLE_EPSILON = 1e-9


@register_rule
class RotaryRapidShortRotates(Rule):
    """A `G0` over a half turn on a control that takes the short way round: it lands somewhere else.

    Mach3's "Ang Short Rot on G0" (`<ShortRot>1<`) and its equivalents make a rapid reach the commanded
    angle by the shorter of the two directions. Below a half turn that is the same place the program
    asked for. Above it, the control goes the other way and **stops a full turn from the commanded
    angle** — and reports that as its position. The next block that commands an absolute angle for the
    axis is not short-rotated, so it makes up the whole turn; if that block is cutting, it cuts a
    complete circle around the part.

    Observed on a Vectric wrapped-rotary program whose passes sit at A0, A-90, A-180 and A-270. Every
    rapid *within* a pass turns exactly 90 deg, so the control's choice agrees and nothing drifts. The
    profile-reset rapid — `G00 A0.000` from A-270, a 270 deg move — is the only one over a half turn: it
    stopped at A-360, and the following `G1 A0.000 Z-12.000` then unwound a full revolution while
    descending, cutting a ring around the blank at the pass start.

    **This is the second rule that judges the control rather than the program**, and it is the reason
    `axes.a.short_rotate` exists: the setting appears nowhere in the G-code, so only the profile can
    say the control does this. It differs from its sibling
    `process.rotary-rapid-before-plunge` in the one way that matters for the fix — **nothing flushes
    it.** A dwell or an M-code between the blocks defeats CV blending and does absolutely nothing here,
    because the axis is *physically in the wrong place*, not merely early. `G61` does not help either.
    The remedies are to clear the setting, or to keep the axis monotonic so no rapid exceeds a half turn.

    **It stays a warning.** It fires three times on ordinary, correct Vectric post output, so promoting
    it to `error` would make `foursight check` exit 1 on programs the user cannot reasonably be asked to
    hand-edit — the `geometry.rapid-into-stock` argument exactly. It is also the *control* that is
    misconfigured here, not the file.

    **The last finding has no following block, and it is not the least important one.** A program that
    ends on a short-rotated rapid leaves the axis a full turn from the angle the DRO shows, so the *next*
    run starts out of phase and gouges near its beginning instead of its middle. Dropping that case for
    lack of a consequence to name would hide the one occurrence that repeats.

    **An exact half turn is a tie**, resolved by a rule inside the control that is not in the G-code —
    both directions are 180 deg and they land a full turn apart. A two-pass wrapped program resetting
    `A0` from `A-180` is exactly that, so it is reported rather than assumed safe, and the message says
    the landing may be either.
    """

    rule_id = "process.rotary-rapid-short-rotates"
    description = "Rapid over a half turn where the control short-rotates: lands a full turn out"
    severity = Severity.WARNING

    def check(self, program: Program) -> Iterable[Diagnostic]:
        profile = program.profile
        axis = profile.axes.get(ROTARY_AXIS)
        if axis is None or not axis.short_rotate:
            # Not declared, so not assumed. A control that honours absolute angles has no such hazard,
            # and inventing the setting would report gouges on machines that cannot produce them.
            return
        steps = list(walk(program.commands))
        machine: float | None = None  # where the axis physically is, which is not always after.a
        for index, (command, before, after) in enumerate(steps):
            if machine is None:
                machine = before.a
            if ROTARY_AXIS not in command.words or after.a is None:
                continue
            target = after.a
            if command.motion != RAPID_MOTION or machine is None:
                machine = target  # anything but a rapid honours the commanded angle exactly
                continue
            reached, tie = _short_rotation(target, machine)
            if reached != target or tie:
                yield self._finding(command, steps, index, machine, target, reached, tie, profile)
            machine = reached

    def _finding(
        self,
        command: Command,
        steps: list[tuple[Command, Position, Position]],
        index: int,
        machine: float,
        target: float,
        reached: float,
        tie: bool,
        profile: MachineProfile,
    ) -> Diagnostic:
        travel = abs(target - machine)
        cause = (
            f"rapid commands A {format_angle(target)} from {format_angle(machine)}, a "
            f"{format_angle(travel)} move"
        )
        if tie:
            cause += (
                "; exactly a half turn, so which way axes.a.short_rotate sends the control is its own "
                f"tie-break and it may stop at {format_angle(reached)} instead of the commanded angle"
            )
        else:
            cause += (
                f"; axes.a.short_rotate says the control takes the short way round, so it stops at "
                f"{format_angle(reached)} instead"
            )
        return Diagnostic(
            rule_id=self.rule_id,
            severity=Severity.WARNING,
            line=command.ref.line_no,
            message=(
                f"{cause}, {_unwind_clause(steps, index + 1, reached, profile)}. A dwell or G61 does "
                f"not help: the axis is in the wrong place, not merely early. Clear "
                f"axes.a.short_rotate if the control honours absolute angles, or keep A monotonic so "
                f"no rapid exceeds a half turn"
            ),
            offset=command.ref.start,
        )


def _short_rotation(target: float, machine: float) -> tuple[float, bool]:
    """Where a control taking the short way round stops, and whether the two directions tie.

    `round` breaks a tie toward even, which is not any control's rule, so the tie is reported
    separately and named as the landing the program does *not* expect — the recoverable direction,
    since a warning about a turn that does not happen costs less than silence about one that does.
    """
    delta = target - machine
    tie = abs(abs(delta) - HALF_TURN) < _ANGLE_EPSILON
    turns = (1 if delta > 0 else -1) if tie else round(delta / FULL_TURN)
    return target - FULL_TURN * turns, tie


def _unwind_clause(
    steps: list[tuple[Command, Position, Position]],
    start: int,
    reached: float,
    profile: MachineProfile,
) -> str:
    """What the next block commanding the axis will therefore do.

    Only one block ahead is examined. A chain of rapids each short-rotating in turn needs no deeper
    look, because the loop in `check` carries the physical position forward and every link reports
    itself; going deeper here would only duplicate those findings inside each other's messages.
    """
    following = _next_rotary_block(steps, start)
    if following is None:
        return (
            "and nothing after it commands A again, so the program ends a full turn from the angle "
            "it reports and the next run starts out of phase"
        )
    command, _, after = following
    line, target = command.ref.line_no, after.a
    if target is None:
        return f"and line {line} commands A from a position that is not established"
    if command.motion == RAPID_MOTION:
        return (
            f"and line {line} is another rapid, which short-rotates from there in turn rather than "
            f"making the turn up"
        )
    travel = abs(target - reached)
    depth = _cutting_depth(command, after, profile)
    if depth is not None:
        units = command.modal_snapshot.units
        return (
            f"and line {line} then commands A {format_angle(target)} at feed with Z at "
            f"{format_length(depth, units)}, below clearance: the axis makes up "
            f"{format_angle(travel)} while in the material, cutting a full circle around the part"
        )
    if command.motion in CUTTING_MOTIONS:
        return (
            f"and line {line} then commands A {format_angle(target)} at feed, making up "
            f"{format_angle(travel)} at cutting speed"
        )
    return (
        f"and line {line} then commands A {format_angle(target)}, making up {format_angle(travel)}"
    )


def _next_rotary_block(
    steps: list[tuple[Command, Position, Position]], start: int
) -> tuple[Command, Position, Position] | None:
    """The next block that commands the rotary axis.

    Deliberately *not* stopped by an M-code or a `G4`, unlike `_next_moving_block`. Those flush a
    control's look-ahead, which is what makes them a remedy for CV blending; a short-rotated axis is
    standing in the wrong place and will still be standing there after any dwell.
    """
    for command, before, after in steps[start:]:
        if ROTARY_AXIS in command.words:
            return command, before, after
    return None


def _cutting_depth(command: Command, after: Position, profile: MachineProfile) -> float | None:
    """This block's machine Z, when it cuts at or below clearance. `None` when it does not, or unknown.

    An unset `[safety].min_clearance_z` yields `None` rather than a guessed plane: the drift is
    reported either way, and only the wording that claims the tool is in material depends on knowing
    where material starts.
    """
    clearance = profile.safety.min_clearance_z
    if clearance is None or command.motion not in CUTTING_MOTIONS:
        return None
    z, _ = machine_value(after.z, "Z", command, profile)
    if z is None or z >= clearance:
        return None
    return z
