"""Fixture corpus and the baseline-plus-mutation machinery (T1.10).

PLAN.md § Testing Strategy: *"'Each broken file triggers exactly one diagnostic' is brittle in
practice — a file missing G21 also trips 'no work offset' and 'lacks M30'. Instead keep one
known-clean baseline file, derive each broken fixture by a single mutation, and assert on the
**newly added** diagnostic relative to the baseline's diagnostic set."*

Two things make that trustworthy here:

- **Mutations are derived, not stored.** Each broken fixture is the baseline plus one declarative
  edit, so the single-mutation property cannot rot the way sixteen hand-maintained near-copies
  would. `test_every_mutation_changes_exactly_one_line` enforces it mechanically.
- **Expectations are declared up front.** Each mutation names the `rule_id` it should add. Until
  that rule exists (T1.7–T1.9) its test skips with a message naming the missing rule, so the corpus
  never silently passes for want of a check.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from foursight.machine.profile import MachineProfile, default_profile_path, load_profile
from foursight.parser.resolver import parse
from foursight.verify.report import Diagnostic
from foursight.verify.rules import Program, registered_rules, verify

FIXTURES = Path(__file__).parent / "fixtures"
# Resolved through the package, not the repo layout: the shipped profile lives inside
# `foursight/profiles/` so that an installed app and a PyInstaller bundle can both find it.
DEFAULT_PROFILE_PATH = default_profile_path()
BASELINE = "baseline_4axis.nc"


@dataclass(frozen=True, slots=True)
class Mutation:
    """One edit to the baseline, and the diagnostic it is expected to add.

    ``find`` must match exactly one line of the baseline. ``replace_with`` of ``None`` deletes that
    line; otherwise it substitutes it. Either way the result differs from the baseline by exactly
    one line, which is the property the whole strategy rests on.
    """

    name: str
    find: str  # a unique substring of the single line to change
    replace_with: str | None  # None deletes the line
    expect_rule: str  # rule_id the mutation should add
    why: str


# Ordered by the PLAN.md § Verifier Rules checklist they exercise. `expect_rule` ids are the
# contract T1.7-T1.9 must satisfy; a rule that never registers shows up as a skip, not a pass.
MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "no_units", "N10 G21 G90 G17 G94 G54", "N10 G90 G17 G94 G54",
        "process.units-not-set", "W: units never explicitly set (G20/G21 missing)",
    ),
    Mutation(
        "no_work_offset", "N10 G21 G90 G17 G94 G54", "N10 G21 G90 G17 G94",
        "process.no-work-offset", "W: no work offset selected before motion",
    ),
    Mutation(
        "no_feed", "N80 G1 Z-1.0 F300", "N80 G1 Z-1.0",
        "process.no-feed-rate", "E: cutting move with no feed rate ever set",
    ),
    Mutation(
        "feed_exceeds_limit", "N90 G1 X50.0 Y10.0 F600", "N90 G1 X50.0 Y10.0 F9000",
        "process.feed-too-high", "E: feed rate exceeds limits.max_feed (3000)",
    ),
    Mutation(
        "spindle_exceeds_limit", "N50 S8000 M3", "N50 S30000 M3",
        "process.spindle-too-high", "E: spindle S exceeds limits.max_spindle_rpm (24000)",
    ),
    Mutation(
        "inverse_time_without_feed",
        "N10 G21 G90 G17 G94 G54",
        "N10 G21 G90 G17 G93 G54",
        "process.g93-without-feed",
        "E: G93 inverse-time active with no F on a cutting block",
    ),
    Mutation(
        "cut_before_spindle", "N50 S8000 M3", None,
        "process.cut-before-spindle", "W: cutting move before spindle start",
    ),
    Mutation(
        "no_program_end", "N190 M30", None,
        "process.no-program-end", "W: program lacks M2/M30",
    ),
    Mutation(
        "incremental_at_end", "N180 G0 X0.0 Y0.0", "N180 G91 G0 X0.0 Y0.0",
        "process.incremental-at-end", "W: G91 active at program end",
    ),
    Mutation(
        "coolant_without_spindle", "N160 M9", "N160 M8",
        "process.coolant-without-spindle", "W: coolant on with spindle off",
    ),
    Mutation(
        "toolchange_without_tool", "N40 T1 M6", "N40 M6",
        "process.toolchange-without-tool", "W: M6 with no tool number ever set",
    ),
    Mutation(
        "toolchange_without_retract", "N30 G0 Z25.0", None,
        "process.toolchange-without-retract", "W: M6 without prior retract to safe Z",
    ),
    Mutation(
        "rapid_below_clearance", "N30 G0 Z25.0", "N30 G0 Z1.0",
        "process.rapid-below-clearance", "W: rapid below safety.min_clearance_z (5.0)",
    ),
    Mutation(
        "axis_travel_exceeded", "N90 G1 X50.0 Y10.0 F600", "N90 G1 X500.0 Y10.0 F600",
        "geometry.axis-travel-exceeded", "E: axis travel limit exceeded (X max 400)",
    ),
    Mutation(
        "arc_radius_mismatch", "N100 G2 X40.0 Y20.0 I0.0 J10.0", "N100 G2 X40.0 Y20.0 I0.0 J12.0",
        "geometry.arc-radius-mismatch", "E: arc radius mismatch beyond tolerance",
    ),
    Mutation(
        "arc_r_coincident",
        "N100 G2 X40.0 Y20.0 I0.0 J10.0",
        "N100 G2 X50.0 Y10.0 R10.0",
        "geometry.arc-r-invalid",
        "E: R-format arc with coincident endpoints is undefined",
    ),
    Mutation(
        "rotary_wrap_warning", "N130 G1 A90.0 F1800", "N130 G1 A900.0 F1800",
        "geometry.rotary-wrap", "W: rotary move exceeds limits.rotary_wrap_warn (360)",
    ),
    Mutation(
        "modal_group_conflict", "N120 G1 X10.0 Y30.0", "N120 G1 G2 X10.0 Y30.0",
        "structural.modal-group-conflict", "E: two G-codes from the same modal group in one block",
    ),
    Mutation(
        "malformed_word", "N120 G1 X10.0 Y30.0", "N120 G1 X Y30.0",
        "structural.syntax-error", "E: malformed word",
    ),
    Mutation(
        "unknown_inert_code", "N120 G1 X10.0 Y30.0", "N120 G12 G1 X10.0 Y30.0",
        "structural.unknown-code", "W: unknown/unsupported INERT G/M code",
    ),
    Mutation(
        "canned_cycle", "N120 G1 X10.0 Y30.0", "N120 G81 Z-5.0 R2.0",
        "structural.unsupported-motion", "U: canned cycle affects motion, not interpreted in v1",
    ),
    Mutation(
        "cutter_compensation", "N120 G1 X10.0 Y30.0", "N120 G41 D1",
        "structural.unsupported-motion", "U: cutter comp active; displayed path is the centerline",
    ),
    Mutation(
        "coordinate_rotation", "N120 G1 X10.0 Y30.0", "N120 G68 X0.0 Y0.0 R45.0",
        "structural.unsupported-motion", "U: G68 rotation; the drawn path would be the unrotated one",
    ),
    Mutation(
        "coordinate_scaling", "N120 G1 X10.0 Y30.0", "N120 G51 X0.0 Y0.0 P2.0",
        "structural.unsupported-motion", "U: G51 scaling; the drawn path would be unscaled",
    ),
    Mutation(
        "polar_coordinates", "N120 G1 X10.0 Y30.0", "N120 G16",
        "structural.unsupported-motion", "U: G16 polar; X and Y are a radius and an angle",
    ),
    Mutation(
        "subprogram_call", "N120 G1 X10.0 Y30.0", "N120 M98 P1000",
        "structural.unsupported-motion", "U: M98 is not expanded; position is lost from here on",
    ),
)  # fmt: skip


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def apply_mutation(baseline: str, mutation: Mutation) -> str:
    """Derive a broken fixture. Refuses unless `find` matches exactly one line."""
    lines = baseline.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if mutation.find in line]
    if len(hits) != 1:
        raise AssertionError(
            f"mutation {mutation.name!r}: {mutation.find!r} matched {len(hits)} lines, expected 1"
        )
    index = hits[0]
    if mutation.replace_with is None:
        del lines[index]
    else:
        ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = mutation.replace_with + ending
    return "".join(lines)


def diagnose(text: str, profile: MachineProfile) -> list[Diagnostic]:
    """Verify a program, refusing to return a result in which a rule crashed.

    `verify()` deliberately converts a raising rule into one `internal.rule-failed` diagnostic so a
    single broken rule cannot suppress the others. That robustness also *masks crashes from tests*:
    a rule that blew up on a None looked like a rule that correctly stayed silent, because the
    assertions only checked that a specific rule_id was absent. Every fixture and check test routes
    through here, so this one assertion closes that hole everywhere at once.
    """
    # Parsed under the profile's own dialect, exactly as `cli.run_check` and `gui.session` do.
    # Passing the profile to `verify` while parsing under the default would split the one thing the
    # dialect design insists cannot be split, and would quietly hide every parse-layer divergence
    # from every test that uses a non-default profile.
    result = parse(text, dialect=profile.parser_dialect)
    program = Program(commands=result.commands, profile=profile, parse_errors=result.errors)
    diagnostics = verify(program)
    crashed = [d.message for d in diagnostics if d.rule_id == "internal.rule-failed"]
    assert not crashed, f"a rule raised while checking this program: {crashed}"
    return diagnostics


def diagnostic_keys(diagnostics: list[Diagnostic]) -> set[tuple[str, int]]:
    """Identity for set-diffing: rule id and line, never the message text.

    Asserting on messages would make every wording change a test failure, and would say nothing
    about *which* rule fired.
    """
    return {(diagnostic.rule_id, diagnostic.line) for diagnostic in diagnostics}


def registered_rule_ids() -> set[str]:
    return {rule.rule_id for rule in registered_rules()}


@pytest.fixture(scope="session")
def default_profile() -> MachineProfile:
    return load_profile(DEFAULT_PROFILE_PATH)


@pytest.fixture(scope="session")
def baseline_text() -> str:
    return fixture_text(BASELINE)


@pytest.fixture(scope="session")
def baseline_diagnostics(baseline_text: str, default_profile: MachineProfile) -> list[Diagnostic]:
    return diagnose(baseline_text, default_profile)


# --------------------------------------------------------------------------- perf reporting

# T1.12's DoD requires the measured rate to be *logged*. pytest swallows stdout for passing tests,
# so measurements are collected here and printed in the terminal summary, where they show up in a
# normal `pytest -q` run and therefore in CI logs — without anyone having to remember `-s`.
PERF_MEASUREMENTS: list[str] = []


def record_measurement(line: str) -> None:
    PERF_MEASUREMENTS.append(line)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not PERF_MEASUREMENTS:
        return
    terminalreporter.write_sep("-", "performance")
    for line in PERF_MEASUREMENTS:
        terminalreporter.write_line(line)
