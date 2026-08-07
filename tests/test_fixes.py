"""Fix tests (T5.0–T5.3, T5.6). No Qt.

TASKS.md: *"including the refusal cases, which are the point."* They are, and this file is weighted
accordingly. A fix that acts when it should decline produces a program that looks corrected and machines
something else — the exact failure the whole tool exists to prevent, arriving through the feature meant to
help.

The two refusals PLAN.md names:

- **Arc-centre recomputation beyond 10× tolerance.** Within a small mismatch the centre is a rounding
  artefact and the endpoints are the intent. Past that, three inconsistent numbers describe no arc, and
  choosing which to keep is a guess about which the programmer got right.
- **IJK→R on a full circle.** R cannot express one: the sign picks minor or major arc, but coincident
  endpoints make every R the same degenerate case.

Both are tested at the boundary as well as well past it, because a refusal that fires everywhere is as
useless as one that never fires.
"""

import pytest

from conftest import DEFAULT_PROFILE_PATH
from foursight.fix.differ import (
    DiffError,
    apply_unified_diff,
    restore_newlines,
    unified_diff,
)
from foursight.fix.engine import (
    FixContext,
    FixHistory,
    apply_fix,
    load_builtin_fixes,
)
from foursight.fix.fixes import ARC_REFUSAL_FACTOR
from foursight.machine.profile import load_profile, load_profile_text
from foursight.parser.resolver import parse
from foursight.verify.rules import Program, load_builtin_checks, verify


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def run(fix_id: str, text: str, profile, line: int | None = None, parameter=None):
    return apply_fix(fix_id, FixContext(text=text, profile=profile, line=line, parameter=parameter))


def changed_lines(result) -> list[str]:
    return [
        line
        for line in result.diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


ARC_PROGRAM = "G21 G90 G94 G17\nG0 X0 Y0\nG2 X20 Y0 I{i} J0 F600\n"


# --------------------------------------------------------------------------- refusals: arc centre


def test_arc_centre_refuses_beyond_ten_times_tolerance(profile) -> None:
    """The refusal PLAN.md names. Past this the three numbers describe no arc at all."""
    result = run("fix.recompute-arc-centre", ARC_PROGRAM.format(i=12), profile, line=3)
    assert result.refused
    assert "invent geometry" in result.refusal
    assert result.text is None, "a refusal must not hand back text"


def test_the_refusal_says_how_far_out_it_is_and_what_to_do(profile) -> None:
    """A refusal the user cannot act on is only marginally better than a wrong fix."""
    result = run("fix.recompute-arc-centre", ARC_PROGRAM.format(i=12), profile, line=3)
    assert "4.0000 mm" in result.refusal
    assert "check the arc by hand" in result.refusal


def test_arc_centre_acts_just_inside_the_threshold(profile) -> None:
    """A refusal that fires everywhere is as useless as one that never fires.

    Chosen from the tolerance rather than hard-coded, so tightening the profile cannot make this test
    silently meaningless.
    """
    tolerance = profile.tolerance.arc_radius_mismatch
    # A centre offset of d gives a radius mismatch of 2d for this geometry, so stay under 10x tolerance.
    offset = tolerance * ARC_REFUSAL_FACTOR * 0.4
    result = run("fix.recompute-arc-centre", ARC_PROGRAM.format(i=10 + offset), profile, line=3)
    assert result.applied, result.refusal
    assert "endpoints preserved" in result.note


def test_arc_centre_leaves_an_already_good_arc_alone(profile) -> None:
    """Within tolerance there is nothing to correct, and rewriting the line would be noise in the diff."""
    result = run("fix.recompute-arc-centre", ARC_PROGRAM.format(i=10), profile, line=3)
    assert result.changed_nothing
    assert "within tolerance" in result.note


def test_arc_centre_preserves_both_endpoints(profile) -> None:
    """The endpoints are where the tool must go; the centre is the over-specified value."""
    text = ARC_PROGRAM.format(i=10.01)
    result = run("fix.recompute-arc-centre", text, profile, line=3)
    assert result.applied
    fixed = parse(result.text).commands[-1]
    assert fixed.words["X"] == 20.0
    assert fixed.words["Y"] == 0.0


def test_the_corrected_arc_no_longer_trips_the_check(profile) -> None:
    """End to end: the fix must satisfy the rule that reported the problem, not merely change the text."""
    load_builtin_checks()
    text = ARC_PROGRAM.format(i=10.02)
    before = parse(text)
    assert any(
        d.rule_id == "geometry.arc-radius-mismatch"
        for d in verify(
            Program(commands=before.commands, profile=profile, parse_errors=before.errors)
        )
    )

    result = run("fix.recompute-arc-centre", text, profile, line=3)
    assert result.applied
    after = parse(result.text)
    assert not any(
        d.rule_id == "geometry.arc-radius-mismatch"
        for d in verify(
            Program(commands=after.commands, profile=profile, parse_errors=after.errors)
        )
    )


# --------------------------------------------------------------------------- refusals: IJK to R


def test_ijk_to_r_refuses_on_a_full_circle(profile) -> None:
    """R-format cannot express a full circle; there is no correct output to produce."""
    result = run(
        "fix.arc-ijk-to-r", "G21 G90 G94 G17\nG0 X10 Y0\nG2 X10 Y0 I-10 J0 F600\n", profile, line=3
    )
    assert result.refused
    assert "full circle" in result.refusal
    assert "Keep the IJK form" in result.refusal


def test_ijk_to_r_uses_a_positive_radius_for_a_minor_arc(profile) -> None:
    result = run(
        "fix.arc-ijk-to-r", "G21 G90 G94 G17\nG0 X0 Y0\nG2 X10 Y10 I10 J0 F600\n", profile, line=3
    )
    assert result.applied
    assert parse(result.text).commands[-1].words["R"] == 10.0


def test_ijk_to_r_uses_a_negative_radius_beyond_180_degrees(profile) -> None:
    """The sign convention. Getting it wrong substitutes the complementary arc: same endpoints, same
    radius, the tool going the long way round instead of the short one."""
    result = run(
        "fix.arc-ijk-to-r", "G21 G90 G94 G17\nG0 X0 Y0\nG3 X10 Y10 I10 J0 F600\n", profile, line=3
    )
    assert result.applied
    assert parse(result.text).commands[-1].words["R"] == -10.0
    assert "270" in result.note


def test_ijk_to_r_removes_the_offset_words(profile) -> None:
    """Leaving I and J alongside R would be over-specified and dialect-dependent."""
    result = run(
        "fix.arc-ijk-to-r", "G21 G90 G94 G17\nG0 X0 Y0\nG2 X10 Y10 I10 J0 F600\n", profile, line=3
    )
    words = parse(result.text).commands[-1].words
    assert "I" not in words and "J" not in words


# --------------------------------------------------------------------------- R to IJK


def test_r_to_ijk_produces_the_centre_that_reproduces_the_arc(profile) -> None:
    """Checked against hand geometry: start (0,0), end (10,10), R10 clockwise puts the centre at (10,0)."""
    result = run(
        "fix.arc-r-to-ijk", "G21 G90 G94 G17\nG0 X0 Y0\nG2 X10 Y10 R10 F600\n", profile, line=3
    )
    assert result.applied
    words = parse(result.text).commands[-1].words
    assert words["I"] == pytest.approx(10.0, abs=1e-6)
    assert words["J"] == pytest.approx(0.0, abs=1e-6)
    assert "R" not in words


def test_r_to_ijk_round_trips_through_ijk_to_r(profile) -> None:
    """The two conversions must agree, or one of them is wrong about the sign convention."""
    text = "G21 G90 G94 G17\nG0 X0 Y0\nG2 X10 Y10 R10 F600\n"
    to_ijk = run("fix.arc-r-to-ijk", text, profile, line=3)
    assert to_ijk.applied
    back = run("fix.arc-ijk-to-r", to_ijk.text, profile, line=3)
    assert back.applied
    assert parse(back.text).commands[-1].words["R"] == pytest.approx(10.0, abs=1e-6)


def test_r_to_ijk_refuses_when_no_arc_of_that_radius_reaches_the_endpoints(profile) -> None:
    """The endpoints are more than 2R apart, so the R or an endpoint is wrong — and which is a guess."""
    result = run(
        "fix.arc-r-to-ijk", "G21 G90 G94 G17\nG0 X0 Y0\nG2 X100 Y0 R5 F600\n", profile, line=3
    )
    assert result.refused
    assert "2R" in result.refusal or "reaches them" in result.refusal


# --------------------------------------------------------------------------- text fixes


def test_the_preamble_adds_only_what_is_missing(profile) -> None:
    """A diff that rewrites lines it did not need to makes the real change harder to see."""
    result = run("fix.add-safety-preamble", "G21\nG1 X10 F600\n", profile)
    assert result.applied
    added = [line for line in changed_lines(result) if line.startswith("+")]
    assert any("G90" in line for line in added)
    assert not any("G21" in line for line in added), "G21 was already established"


def test_the_preamble_is_skipped_when_already_established(profile) -> None:
    result = run("fix.add-safety-preamble", "G90 G21 G17\nG1 X10 F600\n", profile)
    assert result.changed_nothing


def test_the_preamble_goes_below_framing_and_the_program_number(profile) -> None:
    """`%` and `Oxxxx` belong above the preamble; inserting above them would break Fanuc framing."""
    result = run("fix.add-safety-preamble", "%\nO1000\nG1 X10 F600\n", profile)
    assert result.applied
    lines = result.text.splitlines()
    assert lines[0] == "%"
    assert lines[1].startswith("O1000")
    assert "G90" in lines[2]


def test_a_feed_rate_must_be_supplied_not_invented(profile) -> None:
    """PLAN.md requires this fix to be prompted: a feed rate is a machining decision.

    Inventing one would put a number in the program that nobody chose and the machine would obey.
    """
    result = run("fix.inject-feed-rate", "G21 G90 G94\nG1 X10\n", profile, parameter=None)
    assert result.refused
    assert "feed rate" in result.refusal


def test_a_supplied_feed_rate_lands_on_the_first_cutting_move(profile) -> None:
    result = run(
        "fix.inject-feed-rate", "G21 G90 G94\nG0 Z5\nG1 X10\nG1 X20\n", profile, parameter=450.0
    )
    assert result.applied
    assert parse(result.text).commands[2].words["F"] == 450.0
    assert "line 3" in result.note


def test_a_non_positive_feed_rate_is_refused(profile) -> None:
    assert run("fix.inject-feed-rate", "G21 G90 G94\nG1 X10\n", profile, parameter=0.0).refused
    assert run("fix.inject-feed-rate", "G21 G90 G94\nG1 X10\n", profile, parameter=-5.0).refused


def test_m30_is_appended_above_trailing_framing(profile) -> None:
    """`%` closes a Fanuc program, so M30 belongs inside it."""
    result = run("fix.append-program-end", "%\nO1000\nG1 X10 F600\n%\n", profile)
    assert result.applied
    lines = result.text.splitlines()
    assert lines[-1] == "%"
    assert "M30" in lines[-2]


def test_m30_is_not_appended_twice(profile) -> None:
    assert run("fix.append-program-end", "G1 X10 F600\nM30\n", profile).changed_nothing
    assert run("fix.append-program-end", "G1 X10 F600\nM2\n", profile).changed_nothing


def test_normalizing_leaves_comment_text_alone(profile) -> None:
    """Comment prose was written by a human for humans; rewriting it is a change nobody asked for."""
    result = run("fix.normalize-whitespace", "g1   x10 (Rough Pass - see Setup Sheet)\n", profile)
    assert result.applied
    assert "(Rough Pass - see Setup Sheet)" in result.text
    assert "G1 X10" in result.text


def test_normalizing_is_idempotent(profile) -> None:
    once = run("fix.normalize-whitespace", "g1   x10  f600\n", profile)
    assert run("fix.normalize-whitespace", once.text, profile).changed_nothing


def test_stripping_line_numbers_is_marked_destructive() -> None:
    """PLAN.md: off by default, because the diff does not show that a `GOTO N120` lost its target."""
    fixes = load_builtin_fixes()
    assert fixes["fix.strip-line-numbers"].destructive is True
    assert not any(
        fix.destructive for fix_id, fix in fixes.items() if fix_id != "fix.strip-line-numbers"
    ), "only N-word stripping should be marked destructive"


def test_stripping_line_numbers_warns_about_jump_targets(profile) -> None:
    result = run("fix.strip-line-numbers", "N10 G1 X10 F600\nN20 M30\n", profile)
    assert result.applied
    assert "jump targets" in result.note


def test_stripping_preserves_block_delete(profile) -> None:
    """The `/` decides whether the line runs; removing it with the N-word would change the program."""
    result = run("fix.strip-line-numbers", "/N10 G1 X10 F600\n", profile)
    assert result.applied
    assert result.text.startswith("/G1")


# --------------------------------------------------------------------------- the engine contract


def test_an_unknown_fix_refuses_rather_than_raising(profile) -> None:
    """Fix ids arrive from diagnostics and UI actions, so a stale one is a runtime condition."""
    result = run("fix.does-not-exist", "G1 X10\n", profile)
    assert result.refused
    assert "no such fix" in result.refusal


def test_a_fix_that_raises_does_not_lose_the_buffer(profile, monkeypatch) -> None:
    """A transform raising is our bug. The user's program must survive it, with a readable message."""
    from foursight.fix import engine

    def exploding(_context):
        raise RuntimeError("transform blew up")

    fixes = load_builtin_fixes()
    monkeypatch.setitem(
        engine._REGISTRY,
        "fix.append-program-end",
        type(fixes["fix.append-program-end"])(
            fix_id="fix.append-program-end",
            title="t",
            description="d",
            transform=exploding,
        ),
    )
    result = run("fix.append-program-end", "G1 X10\n", profile)
    assert result.refused
    assert "could not be applied" in result.refusal
    assert result.text is None


def test_every_registered_fix_has_a_title_and_description() -> None:
    """These are shown in the UI; an empty one is a blank menu entry."""
    for fix_id, fix in load_builtin_fixes().items():
        assert fix.title.strip(), fix_id
        assert fix.description.strip(), fix_id


def test_every_fix_id_promised_by_a_diagnostic_exists() -> None:
    """A diagnostic offering a fix that is not registered is a dead button.

    This is the check that keeps the verifier and the fix engine honest about each other.
    """
    load_builtin_checks()
    registered = set(load_builtin_fixes())
    promised = set()
    profile = load_profile_text('[machine]\nunits = "mm"\n')
    programs = [
        "G21 G90 G94 G17\nG0 X0 Y0\nG2 X20 Y0 I12 J0 F600\n",
        "G21 G90 G94 G17\nG0 X0 Y0\nG2 X100 Y0 R5 F600\n",
    ]
    for text in programs:
        result = parse(text)
        for diagnostic in verify(
            Program(commands=result.commands, profile=profile, parse_errors=result.errors)
        ):
            promised.update(diagnostic.fix_ids)
    assert promised, "no diagnostic offered a fix, so this test proves nothing"
    assert promised <= registered, (
        f"diagnostics promise unregistered fixes: {promised - registered}"
    )


def test_a_parameterized_fix_declares_that_it_needs_one() -> None:
    """The GUI has to know *before* invoking that it must prompt."""
    fixes = load_builtin_fixes()
    assert fixes["fix.inject-feed-rate"].needs_parameter is True
    assert fixes["fix.inject-feed-rate"].parameter_prompt.strip()
    assert fixes["fix.append-program-end"].needs_parameter is False


# --------------------------------------------------------------------------- the differ (T5.0)


def test_a_diff_of_identical_text_is_empty() -> None:
    """The engine distinguishes "changed nothing" from "did something" by this."""
    assert unified_diff("G1 X10\n", "G1 X10\n") == ""


def test_a_diff_round_trips_through_application() -> None:
    """The preview must reproduce exactly the text that would be written, or it is misleading."""
    before = "G21 G90\nG1 X10 F600\nG1 X20\nM30\n"
    after = "G21 G90\nG1 X10 F600\nG1 X25\nM30\n"
    assert apply_unified_diff(before, unified_diff(before, after)) == after


def test_applying_a_diff_to_changed_text_is_refused() -> None:
    """The one-fix contract's backstop: a diff applied to an already-changed buffer lands wrong.

    And it lands *plausibly* wrong, because the surrounding lines usually still look right — which is why
    this raises rather than doing its best.
    """
    before = "G1 X10\nG1 X20\nG1 X30\n"
    diff = unified_diff(before, "G1 X10\nG1 X25\nG1 X30\n")
    with pytest.raises(DiffError, match="does not apply"):
        apply_unified_diff("G1 X99\nG1 X20\nG1 X30\n", diff)


def test_a_diff_survives_a_file_with_no_trailing_newline() -> None:
    before, after = "G1 X10", "G1 X20"
    assert apply_unified_diff(before, unified_diff(before, after)).rstrip("\n") == after


def test_line_endings_can_be_restored() -> None:
    """The editor buffer is LF because Qt normalizes it; saving a CRLF program needs this.

    A silent LF conversion would be an unrequested change hiding inside a requested one, and it would not
    appear in the diff at all — a unified diff shows no line terminators.
    """
    assert restore_newlines("a\nb\n", "\r\n") == "a\r\nb\r\n"
    assert restore_newlines("a\nb\n", "\n") == "a\nb\n"
    assert restore_newlines("a\r\nb\r\n", "\n") == "a\nb\n"


# --------------------------------------------------------------------------- history / undo


def test_history_stores_snapshots_not_diffs() -> None:
    """Reversing a diff is exactly the rebasing the one-fix contract forbids."""
    history = FixHistory()
    history.record("fix.a", "before a")
    history.record("fix.b", "before b")
    assert history.depth == 2
    assert history.undo() == ("fix.b", "before b")
    assert history.undo() == ("fix.a", "before a")
    assert history.undo() is None


def test_history_clears() -> None:
    history = FixHistory()
    history.record("fix.a", "x")
    history.clear()
    assert history.depth == 0
