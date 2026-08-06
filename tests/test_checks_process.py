"""Process check tests (T1.8).

Beyond "does each rule fire", two things get particular attention:

- **A missing limit must disable its check**, not default to something plausible. A confident
  diagnostic about a machine we know nothing about is worse than none.
- **Clearance is judged in machine coordinates**, so a configured work offset changes the answer.
  That is the decision PLAN.md's "verification needs machine coords" forces, and it is easy to get
  wrong in a way no simple test would notice.
"""

import pytest

from conftest import DEFAULT_PROFILE_PATH, diagnose, fixture_text
from foursight.machine.profile import load_profile, load_profile_text
from foursight.verify.report import Severity

PREAMBLE = "G21 G90 G17 G94 G54\nG0 Z25.0\nT1 M6\nS8000 M3\n"
POSTAMBLE = "G0 Z25.0\nM5\nM30\n"


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def check(text: str, profile):
    return diagnose(text, profile)


def of(diagnostics, rule_id: str):
    return [d for d in diagnostics if d.rule_id == rule_id]


def only(diagnostics, rule_id: str):
    """The diagnostics for one rule, asserting the rule fired at all."""
    found = of(diagnostics, rule_id)
    assert found, f"{rule_id} did not fire; got {sorted({d.rule_id for d in diagnostics})}"
    return found


def program(body: str) -> str:
    return PREAMBLE + body + POSTAMBLE


# --------------------------------------------------------------------------- each rule fires


def test_units_not_set(profile) -> None:
    found = only(check("G90 G17 G54\nG1 X1 F100\nM30\n", profile), "process.units-not-set")
    assert found[0].severity is Severity.WARNING
    assert len(found) == 1, "one fact about the program, not one per block"


def test_no_work_offset(profile) -> None:
    found = only(check("G21 G90\nS8000 M3\nG1 X1 F100\nM30\n", profile), "process.no-work-offset")
    assert len(found) == 1, "reported at the first motion only"


def test_no_feed_rate_is_an_error(profile) -> None:
    found = only(check(program("G1 X10\nG1 X20\n"), profile), "process.no-feed-rate")
    assert found[0].severity is Severity.ERROR
    assert len(found) == 1, "one report, at the first cutting move"


def test_feed_too_high_reports_each_offending_f(profile) -> None:
    """Each F is a separate programming decision the user has to change."""
    found = only(check(program("G1 X10 F9000\nG1 X20 F8000\n"), profile), "process.feed-too-high")
    assert len(found) == 2
    assert all(d.severity is Severity.ERROR for d in found)
    assert "3000 mm/min" in found[0].message


def test_spindle_too_high(profile) -> None:
    body = "S30000\nG1 X10 F100\n"
    found = only(check("G21 G90 G54\nM3\n" + body + "M30\n", profile), "process.spindle-too-high")
    assert found[0].severity is Severity.ERROR


def test_g93_without_feed_on_a_cutting_block(profile) -> None:
    """Under inverse time, F applies to one move only, so a carried value is meaningless."""
    found = only(check(program("G93\nG1 X10 F100\nG1 X20\n"), profile), "process.g93-without-feed")
    assert found[0].severity is Severity.ERROR
    assert len(found) == 1, "only the block missing F"


def test_cut_before_spindle(profile) -> None:
    found = only(check("G21 G90 G54\nG1 X10 F100\nM30\n", profile), "process.cut-before-spindle")
    assert len(found) == 1


def test_no_program_end(profile) -> None:
    found = only(check("G21 G90 G54\nS8000 M3\nG1 X1 F100\n", profile), "process.no-program-end")
    assert found[0].line == 3, "reported at the last line, where the missing M30 should be"


@pytest.mark.parametrize("ender", ["M2", "M30"])
def test_either_program_end_code_satisfies_the_rule(ender: str, profile) -> None:
    body = f"G21 G90 G54\nS8000 M3\nG1 X1 F100\n{ender}\n"
    assert not of(check(body, profile), "process.no-program-end")


def test_incremental_at_end(profile) -> None:
    found = only(check(program("G1 X10 F100\nG91\n"), profile), "process.incremental-at-end")
    assert found[0].severity is Severity.WARNING


def test_coolant_without_spindle(profile) -> None:
    """M9 before M5 is correct order; the reverse leaves coolant running on a stopped spindle."""
    body = "G21 G90 G54\nS8000 M3\nM8\nG1 X1 F100\nM5\nG1 X2\nM9\nM30\n"
    found = only(check(body, profile), "process.coolant-without-spindle")
    assert len(found) == 1


def test_coolant_switched_off_before_the_spindle_is_fine(profile) -> None:
    body = "G21 G90 G54\nS8000 M3\nM8\nG1 X1 F100\nM9\nM5\nM30\n"
    assert not of(check(body, profile), "process.coolant-without-spindle")


def test_toolchange_without_tool(profile) -> None:
    body = "G21 G90 G54\nG0 Z25.0\nM6\nS8000 M3\nG1 X1 F100\nM30\n"
    found = only(check(body, profile), "process.toolchange-without-tool")
    assert len(found) == 1


def test_toolchange_with_a_t_in_the_same_block_is_fine(profile) -> None:
    body = "G21 G90 G54\nG0 Z25.0\nT1 M6\nS8000 M3\nG1 X1 F100\nM30\n"
    assert not of(check(body, profile), "process.toolchange-without-tool")


def test_toolchange_without_retract(profile) -> None:
    body = "G21 G90 G54\nG0 Z25.0\nG1 Z-1.0 F100\nT1 M6\nS8000 M3\nM30\n"
    found = only(check(body, profile), "process.toolchange-without-retract")
    assert "Z is -1 mm" in found[0].message


def test_toolchange_with_unknown_z_is_a_violation_not_a_pass(profile) -> None:
    """If we cannot establish the tool was clear, we cannot claim the change is safe."""
    body = "G21 G90 G54\nT1 M6\nS8000 M3\nG1 X1 F100\nM30\n"
    found = only(check(body, profile), "process.toolchange-without-retract")
    assert "position unknown" in found[0].message


def test_rapid_below_clearance(profile) -> None:
    body = "G21 G90 G54\nS8000 M3\nG0 Z1.0\nG1 X1 F100\nM30\n"
    found = only(check(body, profile), "process.rapid-below-clearance")
    assert found[0].severity is Severity.WARNING
    assert "1 mm" in found[0].message


def test_rapid_with_unknown_z_is_not_judged(profile) -> None:
    """No position, no claim — the opposite of the tool-change rule, and deliberately so.

    A rapid whose Z we never established might be perfectly safe; a tool change we cannot prove is
    clear is not.
    """
    body = "G21 G90 G54\nS8000 M3\nG0 X10 Y10\nG1 X1 F100\nM30\n"
    assert not of(check(body, profile), "process.rapid-below-clearance")


# ------------------------------------------------------- machine coordinates and unknown offsets


WITH_Z_OFFSET = """
[machine]
units = "mm"
[safety]
min_clearance_z = 5.0
[offsets]
g54 = [0.0, 0.0, 10.0, 0.0]
"""

NO_OFFSETS = """
[machine]
units = "mm"
[safety]
min_clearance_z = 5.0
"""


def test_clearance_is_judged_in_machine_coordinates() -> None:
    """A programmed Z of 1.0 with a +10 work offset is machine Z 11 — clear, not a violation.

    Judging the programmed value instead would report a violation that does not exist.
    """
    body = "G21 G90 G54\nS8000 M3\nG0 Z1.0\nG1 X1 F100\nM30\n"
    with_offset = load_profile_text(WITH_Z_OFFSET)
    assert not of(diagnose(body, with_offset), "process.rapid-below-clearance")


def test_the_same_program_violates_when_the_offset_is_zero() -> None:
    """Control: the only difference from the test above is the work offset."""
    body = "G21 G90 G54\nS8000 M3\nG0 Z1.0\nG1 X1 F100\nM30\n"
    zeroed = load_profile_text(WITH_Z_OFFSET.replace("10.0, 0.0]", "0.0, 0.0]"))
    assert of(diagnose(body, zeroed), "process.rapid-below-clearance")


def test_unknown_offset_says_it_assumed_zero() -> None:
    """There is no tier below `warning`, so the uncertainty goes in the message instead."""
    body = "G21 G90 G54\nS8000 M3\nG0 Z1.0\nG1 X1 F100\nM30\n"
    found = only(diagnose(body, load_profile_text(NO_OFFSETS)), "process.rapid-below-clearance")
    assert "assumes zero work offset" in found[0].message


def test_known_offset_adds_no_caveat() -> None:
    body = "G21 G90 G54\nS8000 M3\nG0 Z-20.0\nG1 X1 F100\nM30\n"
    found = only(diagnose(body, load_profile_text(WITH_Z_OFFSET)), "process.rapid-below-clearance")
    assert "assumes zero" not in found[0].message


def test_g53_coordinates_are_already_machine_absolute() -> None:
    """Adding the work offset to a G53 block would double-count it.

    Z2.0 is the discriminating value: under G53 the machine Z really is 2 and must warn, while
    wrongly adding the +10 offset would give 12 and stay silent. A larger Z would clear the
    threshold either way and prove nothing — the first version of this test used Z20 and could not
    tell the two apart.
    """
    body = "G21 G90 G54\nS8000 M3\nG53 G0 Z2.0\nG1 X1 F100\nM30\n"
    found = only(diagnose(body, load_profile_text(WITH_Z_OFFSET)), "process.rapid-below-clearance")
    assert "2 mm" in found[0].message


def test_the_same_z_without_g53_gets_the_offset_and_is_clear() -> None:
    """The paired control: identical Z, no G53, so the +10 offset applies and 12 mm is clear."""
    body = "G21 G90 G54\nS8000 M3\nG0 Z2.0\nG1 X1 F100\nM30\n"
    assert not of(diagnose(body, load_profile_text(WITH_Z_OFFSET)), "process.rapid-below-clearance")


# --------------------------------------------------------------------------- absent limits


MINIMAL = '[machine]\nunits = "mm"\n'


@pytest.mark.parametrize(
    ("rule_id", "body"),
    [
        ("process.feed-too-high", "G1 X1 F999999\n"),
        ("process.spindle-too-high", "S999999 M3\nG1 X1 F100\n"),
        ("process.rapid-below-clearance", "G0 Z-500.0\n"),
        ("process.toolchange-without-retract", "T1 M6\n"),
    ],
)
def test_an_unconfigured_limit_disables_its_check(rule_id: str, body: str) -> None:
    """`MachineProfile` reports an unset limit as None; no rule may invent a bound."""
    bare = load_profile_text(MINIMAL)
    assert not of(diagnose("G21 G90 G54\n" + body + "M30\n", bare), rule_id)


def test_require_spindle_before_cut_can_be_switched_off() -> None:
    body = "G21 G90 G54\nG1 X1 F100\nM30\n"
    off = load_profile_text(MINIMAL + "\n[safety]\nrequire_spindle_before_cut = false\n")
    assert not of(diagnose(body, off), "process.cut-before-spindle")


def test_retract_before_toolchange_can_be_switched_off() -> None:
    body = "G21 G90 G54\nT1 M6\nS8000 M3\nG1 X1 F100\nM30\n"
    off = load_profile_text(
        MINIMAL + "\n[safety]\nmin_clearance_z = 5.0\nretract_before_toolchange = false\n"
    )
    assert not of(diagnose(body, off), "process.toolchange-without-retract")


# --------------------------------------------------------------------------- units in messages


def test_messages_use_the_programs_declared_units() -> None:
    """ "F exceeds 3000 mm/min" against an inch program is not actionable."""
    inch = load_profile_text(MINIMAL + "\n[limits]\nmax_feed = 3000.0\n")
    body = "G20 G90 G54\nS8000 M3\nG1 X1 F200\nM30\n"
    found = only(diagnose(body, inch), "process.feed-too-high")
    assert "in/min" in found[0].message
    assert "mm/min" not in found[0].message


def test_clearance_message_uses_inches_for_an_inch_program() -> None:
    inch = load_profile_text(MINIMAL + "\n[safety]\nmin_clearance_z = 5.0\n")
    body = "G20 G90 G54\nS8000 M3\nG0 Z0.01\nG1 X1 F10\nM30\n"
    found = only(diagnose(body, inch), "process.rapid-below-clearance")
    assert " in" in found[0].message


# --------------------------------------------------------------------------- the baseline


def test_the_clean_baseline_reports_nothing(profile, baseline_text) -> None:
    """Every process rule must pass on the baseline, or every mutation diff becomes noise."""
    assert check(baseline_text, profile) == []


@pytest.mark.parametrize(
    "name",
    [
        "arc_g18_direction.nc",
        "arc_helical.nc",
        "arc_full_circle_ijk.nc",
        "arc_r_format.nc",
        "inch_program.nc",
        "block_delete.nc",
        "framing_fanuc.nc",
    ],
)
def test_targeted_fixtures_report_no_process_problems(name: str, profile) -> None:
    found = [d for d in check(fixture_text(name), profile) if d.rule_id.startswith("process.")]
    assert found == [], [d.message for d in found]
