"""Structural check tests (T1.7).

The severity *tier* is asserted as carefully as the fact that a diagnostic fires. A canned cycle
downgraded from `unsupported` to `warning` would still show up in the report while quietly licensing
the renderer to draw a straight line through the hole positions — the exact failure PLAN.md's
governing principle exists to prevent.
"""

import pytest

from conftest import DEFAULT_PROFILE_PATH, diagnose, fixture_text
from foursight.machine.profile import load_profile, load_profile_text
from foursight.parser.model import (
    COORD_TRANSFORM_CANCELS,
    COORD_TRANSFORM_CODES,
    COORD_TRANSFORM_MODES,
    SUBPROGRAM_MCODES,
)
from foursight.verify.checks.structural import (
    CANNED_CYCLES,
    CUTTER_COMP,
    INTERPRETED_GCODES,
    INTERPRETED_MCODES,
    UNSUPPORTED_MCODES,
    UNSUPPORTED_ONE_SHOT,
    _all_unsupported,
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
# Needed only by the tests that assert *zero* diagnostics: without it the process checks correctly
# report a missing program end, which would swamp the structural assertion being made.
POSTAMBLE = "M5\nM30\n"


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
    assert check(PREAMBLE + f"{code}\nG1 X10 F100\n" + POSTAMBLE, profile) == []


def test_cancel_codes_do_not_report_themselves(profile) -> None:
    """G40 and G80 end an unsupported span; they are not unsupported constructs."""
    assert check(PREAMBLE + "G40\nG80\nG1 X10 F100\n" + POSTAMBLE, profile) == []


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
    """A code in both tables would report and not report depending on rule order.

    Asserted against `_all_unsupported()` itself, not a union rebuilt here. Re-listing the tables in
    the test is the same drift that let the M-branch of `UnknownCodes` go years without an
    unsupported table at all: the mirror agreed with itself while the code did something else.
    """
    assert INTERPRETED_GCODES.isdisjoint(_all_unsupported())
    assert _all_unsupported() >= CANNED_CYCLES | CUTTER_COMP | COORD_TRANSFORM_CODES
    assert set(UNSUPPORTED_ONE_SHOT) <= _all_unsupported()


def test_no_mcode_is_both_interpreted_and_unsupported() -> None:
    assert INTERPRETED_MCODES.isdisjoint(UNSUPPORTED_MCODES)


def test_the_unsupported_mcode_table_matches_the_parse_layer() -> None:
    """`sim` reads SUBPROGRAM_MCODES and `verify` reads UNSUPPORTED_MCODES; they must agree.

    They cannot be one table — one carries a message and the other does not — so this is the pin.
    """
    assert set(UNSUPPORTED_MCODES) == set(SUBPROGRAM_MCODES)


def test_the_cancel_codes_are_interpreted_and_their_activations_are_not() -> None:
    assert {"40", "80"} <= INTERPRETED_GCODES
    assert COORD_TRANSFORM_CANCELS <= INTERPRETED_GCODES
    assert not ({"41", "42"} & INTERPRETED_GCODES)
    assert not (CANNED_CYCLES & INTERPRETED_GCODES)
    assert INTERPRETED_GCODES.isdisjoint(COORD_TRANSFORM_CODES)


@pytest.mark.parametrize("code", sorted(_all_unsupported()))
def test_no_unsupported_gcode_is_also_called_unknown(code: str, profile) -> None:
    """Closes the class of bug rather than the instance.

    `_all_unsupported()` is the only thing keeping `UnknownCodes` quiet about codes the unsupported
    rule owns. A fifth table added later and not unioned in there fails here the moment it exists,
    instead of quietly producing both a warning and an unsupported diagnostic for the same code —
    with the warning saying the code is "assumed inert", which would contradict the other.
    """
    found = check(PREAMBLE + f"G{code}\n", profile)
    assert not of(found, "structural.unknown-code")


@pytest.mark.parametrize("code", sorted(UNSUPPORTED_MCODES))
def test_no_unsupported_mcode_is_also_called_unknown(code: str, profile) -> None:
    found = check(PREAMBLE + f"M{code} P1000\n", profile)
    assert not of(found, "structural.unknown-code")


# --------------------------------------------------------------------------- coordinate transforms


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_coordinate_transform_is_unsupported_not_a_warning(mode, profile) -> None:
    """A warning would license the renderer to draw the untransformed path as if it were the truth."""
    found = of(
        check(PREAMBLE + f"G{mode.activate}\nG1 X10 F100\nG{mode.cancel}\n", profile),
        "structural.unsupported-motion",
    )
    assert len(found) == 1
    assert found[0].severity is Severity.UNSUPPORTED
    assert found[0].severity is not Severity.WARNING


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_transform_span_names_its_extent(mode, profile) -> None:
    body = f"G{mode.activate}\nG1 X10 F100\nG1 X20\nG{mode.cancel}\n"
    found = of(check(PREAMBLE + body, profile), "structural.unsupported-motion")
    assert "from line 3 to 5" in found[0].message


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_an_uncancelled_transform_span_says_so(mode, profile) -> None:
    found = of(
        check(PREAMBLE + f"G{mode.activate}\nG1 X10 F100\n", profile),
        "structural.unsupported-motion",
    )
    assert f"never cancelled by G{mode.cancel}" in found[0].message


def test_two_rotation_spans_separated_by_a_cancel_are_two_diagnostics(profile) -> None:
    body = "G68\nG1 X10 F100\nG69\nG1 X20\nG68\nG1 X30\nG69\n"
    assert len(of(check(PREAMBLE + body, profile), "structural.unsupported-motion")) == 2


def test_simultaneous_transforms_report_separately(profile) -> None:
    """Two transforms at once are two things wrong; collapsing them leaves the user guessing."""
    body = "G68\nG51 P2\nG1 X10 F100\nG50\nG69\n"
    found = of(check(PREAMBLE + body, profile), "structural.unsupported-motion")
    assert len(found) == 2
    assert {"rotation" in d.message for d in found} == {True, False}


def test_the_polar_message_says_x_and_y_are_not_cartesian(profile) -> None:
    """The least arguable of the three: under G16 the drawn curve is a *different* curve."""
    found = of(check(PREAMBLE + "G16\nG1 X50 Y30 F100\n", profile), "structural.unsupported-motion")
    assert "radius and an angle" in found[0].message


def test_g50_alone_is_silent(profile) -> None:
    """G50 is ambiguous — lathe max-spindle-speed, or the scaling cancel.

    Read as the cancel, because this is a mill previewer. If that guess is wrong the cost is
    silence; giving it a diagnostic of its own would print a confidently wrong message instead.
    """
    assert check(PREAMBLE + "G50\nG1 X10 F100\n" + POSTAMBLE, profile) == []


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_transform_and_its_cancel_in_one_block_is_a_modal_conflict(mode, profile) -> None:
    """The machine cannot be in both states; Fanuc puts each pair in one modal group."""
    found = check(PREAMBLE + f"G{mode.activate} G{mode.cancel}\n", profile)
    assert of(found, "structural.modal-group-conflict")


# --------------------------------------------------------------------------- subprograms


@pytest.mark.parametrize("code", sorted(UNSUPPORTED_MCODES))
def test_a_subprogram_code_is_unsupported_not_a_warning(code: str, profile) -> None:
    found = of(check(PREAMBLE + f"M{code} P1000\n", profile), "structural.unsupported-motion")
    assert len(found) == 1
    assert found[0].severity is Severity.UNSUPPORTED
    assert found[0].severity is not Severity.WARNING


def test_the_subprogram_message_says_the_position_is_lost(profile) -> None:
    found = of(check(PREAMBLE + "M98 P1000\n", profile), "structural.unsupported-motion")
    assert "lost" in found[0].message


def test_every_subprogram_call_is_reported(profile) -> None:
    """Each call is a distinct place the picture breaks, unlike a once-per-program modal setting."""
    body = "M98 P1000\nG0 X1 Y1 Z1\nM98 P1001\n"
    assert len(of(check(PREAMBLE + body, profile), "structural.unsupported-motion")) == 2


def test_g98_and_g99_are_not_confused_with_m98_and_m99(profile) -> None:
    """The tables are keyed on bare digits, and the two live in different lists on one Command.

    G98/G99 are the canned-cycle return modes: interpreted, inert without a cycle, and silent.
    M98/M99 cost us the machine position entirely. Confusing them is the likeliest bug here.
    """
    assert check(PREAMBLE + "G98\nG99\nG1 X10 F100\n" + POSTAMBLE, profile) == []
    assert of(check(PREAMBLE + "M98 P1000\n", profile), "structural.unsupported-motion")


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


# --------------------------------------------------------------------------- tool length (owed from M1)


def test_g43_is_reported_as_unsupported_not_a_warning(profile) -> None:
    """The taxonomy's whole point. G43 affects how subsequent motion is interpreted, and v1 does not
    interpret it — so it is `unsupported`, and a warning would say "look at this" about something that
    changes what Z *means*."""
    diagnostics = diagnose("G21 G90 G94\nG43 H1\nG1 X10 Z-5 F600\n", profile)
    found = [d for d in diagnostics if d.rule_id == "structural.tool-length-not-modelled"]
    assert len(found) == 1
    assert found[0].severity == "unsupported"


def test_the_tool_length_message_says_what_the_drawn_z_means(profile) -> None:
    """The path's shape is right and its datum is shifted; saying only "unsupported" would not help."""
    diagnostics = diagnose("G21 G90 G94\nG44 H2\nG1 Z-5 F600\n", profile)
    message = next(
        d.message for d in diagnostics if d.rule_id == "structural.tool-length-not-modelled"
    )
    assert "spindle position, not the tool tip" in message
    assert "shape is correct" in message


def test_one_diagnostic_per_activation_not_per_affected_line(profile) -> None:
    """A program cutting 40,000 lines under one G43 has one thing wrong with it, not 40,000."""
    body = "".join(f"G1 X{n} F600\n" for n in range(40))
    diagnostics = diagnose(f"G21 G90 G94\nG43 H1\n{body}", profile)
    found = [d for d in diagnostics if d.rule_id == "structural.tool-length-not-modelled"]
    assert len(found) == 1
    assert found[0].line == 2


def test_g49_alone_reports_nothing(profile) -> None:
    """Cancelling an offset needs no diagnostic — there is no unmodelled offset in force."""
    diagnostics = diagnose("G21 G90 G94\nG49\nG1 X10 F600\n", profile)
    assert not [d for d in diagnostics if d.rule_id == "structural.tool-length-not-modelled"]


def test_a_program_without_tool_length_reports_nothing(profile) -> None:
    diagnostics = diagnose("G21 G90 G94\nG1 X10 F600\n", profile)
    assert not [d for d in diagnostics if d.rule_id == "structural.tool-length-not-modelled"]


# --------------------------------------------------------------------------- dialect-gated codes


def test_g71_is_unknown_under_linuxcnc_and_interpreted_under_mach3() -> None:
    """The dialect decides whether a code is ours to interpret.

    Deliberately not interpreted globally: in Fanuc, G71 is a turning roughing cycle — very much
    motion-affecting — so reading it as "millimetres" is only safe once the user has named Mach3.
    """
    linuxcnc = load_profile(DEFAULT_PROFILE_PATH)
    mach3 = load_profile_text('[machine]\nunits = "mm"\n[dialect]\nname = "mach3"\n')
    program = PREAMBLE + "G71\nG1 X10 F100\n" + POSTAMBLE

    assert of(check(program, linuxcnc), "structural.unknown-code")
    assert not of(check(program, mach3), "structural.unknown-code")


def test_the_mach3_safe_start_line_produces_no_diagnostics() -> None:
    """The end-to-end shape of the fix: real posted output must check clean.

    `G00 G21 G17 G90 G40 G49 G80` followed by `G71` is exactly what Vectric's Mach2/3 post emits,
    and under LinuxCNC it produced a hard error plus a warning — so `foursight check` exited 1 on a
    correct program.
    """
    mach3 = load_profile_text('[machine]\nunits = "mm"\n[dialect]\nname = "mach3"\n')
    posted = "G00G21G17G90G40G49G80\nG71G91.1\nG54\nS8000 M3\nG1 X10 F100\nM5\nM30\n"
    assert check(posted, mach3) == []
