"""Structural check tests (T1.7).

The severity *tier* is asserted as carefully as the fact that a diagnostic fires. A canned cycle
downgraded from `unsupported` to `warning` would still show up in the report while quietly licensing
the renderer to draw a straight line through the hole positions — the exact failure PLAN.md's
governing principle exists to prevent.
"""

import pytest

from conftest import DEFAULT_PROFILE_PATH, diagnose, fixture_text
from foursight.machine.profile import load_profile
from foursight.verify.checks.structural import (
    CANNED_CYCLES,
    CUTTER_COMP,
    INTERPRETED_GCODES,
    UNSUPPORTED_ONE_SHOT,
)
from foursight.verify.report import Severity


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def check(text: str, profile):
    return diagnose(text, profile)


def ids(diagnostics) -> list[str]:
    return [d.rule_id for d in diagnostics]


def of(diagnostics, rule_id: str):
    return [d for d in diagnostics if d.rule_id == rule_id]


PREAMBLE = "G21 G90 G17 G94 G54\nS8000 M3\n"


# --------------------------------------------------------------------------- taxonomy


def test_canned_cycle_is_unsupported_not_a_warning(profile) -> None:
    """`unsupported` means "affects motion and we did not interpret it" — the span is not drawn."""
    found = of(
        check(PREAMBLE + "G81 Z-5 R2 F100\nX10 Y10\nG80\n", profile),
        "structural.unsupported-motion",
    )
    assert len(found) == 1
    assert found[0].severity is Severity.UNSUPPORTED
    assert found[0].severity is not Severity.WARNING


def test_cutter_comp_is_unsupported_and_says_the_path_is_the_centerline(profile) -> None:
    found = of(
        check(PREAMBLE + "G41 D1\nG1 X10 F100\nG40\n", profile), "structural.unsupported-motion"
    )
    assert len(found) == 1
    assert found[0].severity is Severity.UNSUPPORTED
    assert "centerline" in found[0].message


def test_unknown_code_is_only_a_warning(profile) -> None:
    """An unrecognized code that never touches position stays a warning; the path still renders."""
    found = of(check(PREAMBLE + "G12 X1\n", profile), "structural.unknown-code")
    assert len(found) == 1
    assert found[0].severity is Severity.WARNING


def test_syntax_error_is_an_error(profile) -> None:
    found = of(check(PREAMBLE + "G1 X Y10\n", profile), "structural.syntax-error")
    assert found and all(d.severity is Severity.ERROR for d in found)


def test_modal_conflict_is_an_error_with_its_own_rule_id(profile) -> None:
    """Distinguished by ParseError.kind, not by matching message text."""
    found = check(PREAMBLE + "G1 G2 X10\n", profile)
    assert of(found, "structural.modal-group-conflict")
    assert of(found, "structural.modal-group-conflict")[0].severity is Severity.ERROR
    assert not of(found, "structural.syntax-error"), "a modal conflict is not a syntax error"


def test_a_syntax_error_is_not_also_reported_as_a_modal_conflict(profile) -> None:
    """The reverse direction of the kind split, which the other test could not catch.

    Its input has no syntax error, so "no modal conflict" held trivially there. A mutation making
    ModalGroupConflicts also accept syntax kinds went undetected until this test existed.
    """
    found = check(PREAMBLE + "G1 X Y10\n", profile)
    assert of(found, "structural.syntax-error")
    assert not of(found, "structural.modal-group-conflict")


def test_each_parse_error_kind_maps_to_exactly_one_rule(profile) -> None:
    """No parse error should surface twice under different rule ids."""
    program = PREAMBLE + "G1 G2 X10\nG1 X Y10\nG1 X1 X2 F100\nO100 sub\n"
    found = check(program, profile)
    from_parse = [
        d
        for d in found
        if d.rule_id
        in {
            "structural.syntax-error",
            "structural.modal-group-conflict",
            "structural.unsupported-oword",
        }
    ]
    assert len({(d.line, d.offset, d.message) for d in from_parse}) == len(from_parse)


def test_oword_flow_control_is_unsupported_not_an_error(profile) -> None:
    """Well-formed, but it decides which motion runs, so the drawn path may not be the real one."""
    found = of(check(PREAMBLE + "O100 sub\n", profile), "structural.unsupported-oword")
    assert len(found) == 1
    assert found[0].severity is Severity.UNSUPPORTED


def test_duplicate_address_is_a_syntax_error(profile) -> None:
    found = of(check(PREAMBLE + "G1 X10 X20 F100\n", profile), "structural.syntax-error")
    assert found and "more than once" in found[0].message


def test_unterminated_comment_is_a_syntax_error(profile) -> None:
    found = of(check(PREAMBLE + "G1 X10 F100 (oops\n", profile), "structural.syntax-error")
    assert found and "unterminated" in found[0].message


# --------------------------------------------------------------------------- spans


def test_a_multi_hole_cycle_yields_one_diagnostic_not_one_per_hole(profile) -> None:
    """Twenty holes are one thing the user needs to know, not twenty."""
    holes = "".join(f"X{n * 10} Y10\n" for n in range(1, 21))
    found = of(
        check(PREAMBLE + "G81 Z-5 R2 F100\n" + holes + "G80\n", profile),
        "structural.unsupported-motion",
    )
    assert len(found) == 1


def test_span_message_names_its_extent(profile) -> None:
    found = of(
        check(PREAMBLE + "G81 Z-5 R2 F100\nX10\nX20\nG80\nG1 X30 F100\n", profile),
        "structural.unsupported-motion",
    )
    assert len(found) == 1
    assert "from line 3 to 5" in found[0].message


def test_two_separate_cycles_yield_two_diagnostics(profile) -> None:
    program = PREAMBLE + "G81 Z-5 R2 F100\nX10\nG80\nG1 X5 F100\nG83 Z-9 R2 Q2\nX20\nG80\n"
    assert len(of(check(program, profile), "structural.unsupported-motion")) == 2


def test_an_uncancelled_cycle_span_runs_to_program_end_and_says_so(profile) -> None:
    """A missing G80 is worth stating: the span silently swallows the rest of the program."""
    found = of(
        check(PREAMBLE + "G81 Z-5 R2 F100\nX10\nX20\nM30\n", profile),
        "structural.unsupported-motion",
    )
    assert len(found) == 1
    assert "never cancelled by G80" in found[0].message


def test_a_cancelled_cycle_does_not_claim_it_was_uncancelled(profile) -> None:
    found = of(
        check(PREAMBLE + "G81 Z-5 R2 F100\nX10\nG80\n", profile), "structural.unsupported-motion"
    )
    assert "never cancelled" not in found[0].message


def test_bare_axis_blocks_inside_a_cycle_are_part_of_the_span(profile) -> None:
    """The resolver carries the cycle as the active motion mode; this is what that buys.

    Without it, `X10 Y10` after a G81 would look like an ordinary linear move.
    """
    from foursight.parser.resolver import parse

    commands = parse(PREAMBLE + "G81 Z-5 R2 F100\nX10 Y10\nG80\n").commands
    bare = [c for c in commands if not c.gcodes and not c.mcodes]
    assert bare and all(c.motion == "81" for c in bare)


def test_two_comp_spans_separated_by_g40(profile) -> None:
    program = PREAMBLE + "G41 D1\nG1 X10 F100\nG40\nG1 X20\nG42 D1\nG1 X30\nG40\n"
    found = of(check(program, profile), "structural.unsupported-motion")
    assert len(found) == 2
    assert "G41" in found[0].message
    assert "G42" in found[1].message


# --------------------------------------------------------------------------- what stays silent


def test_the_clean_baseline_produces_no_structural_diagnostics(profile, baseline_text) -> None:
    assert check(baseline_text, profile) == []


@pytest.mark.parametrize("code", ["G61", "G61.1", "G64", "G98", "G99"])
def test_path_control_and_return_modes_are_silent(code: str, profile) -> None:
    """They change cornering or canned-cycle return, not the centreline we draw.

    G64 appears in nearly every LinuxCNC program, so warning here would be pure noise.
    """
    assert check(PREAMBLE + f"{code}\nG1 X10 F100\n", profile) == []


def test_cancel_codes_do_not_report_themselves(profile) -> None:
    """G40 and G80 end an unsupported span; they are not unsupported constructs."""
    assert check(PREAMBLE + "G40\nG80\nG1 X10 F100\n", profile) == []


def test_codes_owned_by_the_unsupported_rule_are_not_also_called_unknown(profile) -> None:
    """Otherwise every canned cycle would produce both a warning and an unsupported diagnostic."""
    found = check(PREAMBLE + "G81 Z-5 R2 F100\nG80\n", profile)
    assert not of(found, "structural.unknown-code")


def test_unknown_code_is_reported_once_per_code_not_once_per_line(profile) -> None:
    """A 100k-line file with G12 on every line must not produce 100k warnings."""
    repeated = "".join("G12 X1\n" for _ in range(50))
    found = of(check(PREAMBLE + repeated, profile), "structural.unknown-code")
    assert len(found) == 1


def test_distinct_unknown_codes_are_reported_separately(profile) -> None:
    found = of(check(PREAMBLE + "G12 X1\nG13 X2\nM77\n", profile), "structural.unknown-code")
    assert len(found) == 3
    assert {"G12", "G13", "M77"} == {d.message.split()[0] for d in found}


# --------------------------------------------------------------------------- tables are coherent


def test_no_code_is_both_interpreted_and_unsupported() -> None:
    """A code in both tables would report and not report depending on rule order."""
    unsupported = CANNED_CYCLES | CUTTER_COMP | set(UNSUPPORTED_ONE_SHOT)
    assert not (INTERPRETED_GCODES & unsupported)


def test_the_cancel_codes_are_interpreted_and_their_activations_are_not() -> None:
    assert {"40", "80"} <= INTERPRETED_GCODES
    assert not ({"41", "42"} & INTERPRETED_GCODES)
    assert not (CANNED_CYCLES & INTERPRETED_GCODES)


# --------------------------------------------------------------------------- fixtures


def test_canned_cycle_fixture_reports_exactly_one_span(profile) -> None:
    found = check(fixture_text("canned_cycle_span.nc"), profile)
    spans = of(found, "structural.unsupported-motion")
    assert len(spans) == 1
    assert spans[0].severity is Severity.UNSUPPORTED
    assert found == spans, f"unexpected extra diagnostics: {ids(found)}"


def test_cutter_comp_fixture_reports_exactly_one_span(profile) -> None:
    found = check(fixture_text("cutter_comp_span.nc"), profile)
    assert len(of(found, "structural.unsupported-motion")) == 1


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
def test_structurally_clean_fixtures_report_nothing(name: str, profile) -> None:
    """These exercise geometry and framing, so a structural diagnostic here means a fixture bug."""
    assert check(fixture_text(name), profile) == []
