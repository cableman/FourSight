"""Session tests (T2.7). No Qt — `gui/session.py` is the Qt-free half of the shell.

What is actually under test is **disclosure**. The simulator already refuses to draw what it cannot
interpret, but PLAN.md's promise — *"a previewer that refuses to draw is recoverable; one that draws
the wrong path is worse than no previewer"* — is only kept if the refusal reaches the user. In M2 this
summary is the entire channel for that, so the distinction between *missing* geometry and *untrusted*
geometry is asserted here rather than left to the window's layout code.
"""

from pathlib import Path

import pytest

from conftest import DEFAULT_PROFILE_PATH, FIXTURES, fixture_text
from foursight.fileio.loader import load_text
from foursight.gui.session import ProgramSummary, _duration, open_loaded, open_program
from foursight.machine.profile import load_profile


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def opened(text: str, profile, **kwargs):
    return open_loaded(load_text(text.encode("utf-8")), profile, **kwargs)


def summary_of(text: str, profile, **kwargs) -> ProgramSummary:
    return opened(text, profile, **kwargs).summary


CLEAN = "G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\nG1 X20\nM30\n"


# --------------------------------------------------------------------------- the pipeline


def test_opening_a_file_parses_and_simulates_it(profile) -> None:
    program = open_program(FIXTURES / "baseline_4axis.nc", profile)
    assert program.commands
    assert len(program.simulation.store) > 0
    assert program.path is not None and program.path.name == "baseline_4axis.nc"


def test_a_missing_file_raises_rather_than_returning_an_empty_program(profile) -> None:
    """The window needs to distinguish "failed" from "loaded, and it was empty".

    Returning an empty program would leave the window showing a successful load of nothing.
    """
    with pytest.raises(OSError):
        open_program(Path("no-such-file.nc"), profile)


def test_block_delete_is_passed_through(profile) -> None:
    """The toggle must reach the parser, or the drawn path is not the one the control would run."""
    text = "G21 G90 G94\nG1 X10 F600\n/G1 X20\n"
    assert (
        summary_of(text, profile).segments > summary_of(text, profile, block_delete=True).segments
    )


# --------------------------------------------------------------------------- missing vs untrusted


def test_suppressed_geometry_makes_the_program_incomplete(profile) -> None:
    """A canned cycle is not drawn, so the picture is missing part of the program."""
    summary = summary_of(fixture_text("canned_cycle_span.nc"), profile)
    assert summary.suppressed
    assert summary.incomplete is True


def test_unverified_geometry_does_not_make_the_program_incomplete(profile) -> None:
    """The distinction that stops the warning becoming noise.

    A cutter-comp span *is* drawn, and distinctly styled. Treating it as incomplete would raise a
    "this toolpath is incomplete" banner on a large share of real programs, which is how a warning
    stops being read at all.
    """
    summary = summary_of(fixture_text("cutter_comp_span.nc"), profile)
    assert summary.unverified
    assert not summary.suppressed
    assert summary.incomplete is False


def test_a_clean_program_warns_about_nothing(profile) -> None:
    """The control case: if this ever produces warnings, every other assertion here is worthless."""
    summary = summary_of(CLEAN, profile)
    assert summary.warnings() == []
    assert summary.incomplete is False


# --------------------------------------------------------------------------- the warnings


def test_the_suppression_warning_says_the_toolpath_is_incomplete(profile) -> None:
    """Wording matters here: the user must learn the *picture* is wrong, not just that a code was odd."""
    warnings = summary_of(fixture_text("canned_cycle_span.nc"), profile).warnings()
    assert any("incomplete" in message for message in warnings)
    assert any("Not drawn" in message for message in warnings)


def test_the_unverified_warning_carries_the_span_reason(profile) -> None:
    """Restating the reason here made the line right about cutter comp and wrong about G33."""
    summary = summary_of(fixture_text("cutter_comp_span.nc"), profile)
    warning = next(m for m in summary.warnings() if "unverified" in m)
    assert summary.unverified[0].reason in warning
    assert "not the compensated path" in warning


def test_the_unverified_warning_does_not_claim_a_thread_is_in_the_wrong_place(profile) -> None:
    """G33's path is exact; only its clock is unmodelled, and the banner must not say otherwise."""
    summary = summary_of(fixture_text("spindle_sync_g33.nc"), profile)
    warning = next(m for m in summary.warnings() if "unverified" in m)
    assert "centerline" not in warning and "centreline" not in warning
    assert "time estimate" in warning


def test_missing_geometry_is_reported_before_merely_untrusted_geometry(profile) -> None:
    """Order is the only prioritization a single banner line has."""
    text = (
        "G21 G90 G94 G54\nG0 Z5\nG41 D1\nG1 X10 Y10 F600\nG40\n"
        "G81 Z-5 R2 F100\nX10 Y10\nX20 Y20\nG80\nM30\n"
    )
    summary = summary_of(text, profile)
    assert summary.suppressed and summary.unverified, "the fixture must exercise both tiers"
    warnings = summary.warnings()
    assert "Not drawn" in warnings[0]
    assert "unverified" in warnings[1]


def test_parse_errors_are_reported(profile) -> None:
    warnings = summary_of("G21 G90 G94\nG1 X10 F600\nX(unclosed\n", profile).warnings()
    assert any("could not be parsed" in message for message in warnings)


def test_a_latin1_fallback_is_disclosed(profile) -> None:
    """Silently mangling a comment is minor; silently *not saying so* is the part worth a warning."""
    loaded = load_text(b"G21 G90 G94\n( caf\xe9 )\nG1 X10 F600\n")
    assert loaded.used_fallback
    warnings = open_loaded(loaded, profile).summary.warnings()
    assert any("Non-ASCII" in message for message in warnings)


def test_missing_feed_rates_are_disclosed_as_a_short_estimate(profile) -> None:
    """A time estimate quietly missing half the program is worse than no estimate."""
    summary = summary_of("G21 G90 G94\nG1 X10\nG1 X20\n", profile)
    assert summary.unknown_durations > 0
    assert any("time estimate is short" in message for message in summary.warnings())


def test_simulator_notes_reach_the_warnings(profile) -> None:
    """G43 tool length is not modelled, and the note explaining that must not be dropped here."""
    summary = summary_of("G21 G90 G94\nG43 H1\nG1 X10 Z-5 F600\n", profile)
    assert summary.notes
    assert any("tool length" in message.lower() for message in summary.warnings())


# --------------------------------------------------------------------------- the headline


def test_the_headline_reports_blocks_segments_and_time(profile) -> None:
    headline = summary_of(CLEAN, profile).headline
    assert "blocks" in headline and "segments" in headline and "est." in headline


def test_a_program_with_no_geometry_says_so_rather_than_showing_zeros(profile) -> None:
    """ "0 segments · 0.0s" reads like a successful load of an empty machine program."""
    assert "no drawable geometry" in summary_of("(comment only)\n", profile).headline


def test_a_program_with_no_timing_says_so_instead_of_claiming_zero_seconds(profile) -> None:
    assert "no timing" in summary_of("G21 G90 G94\nG1 X10\n", profile).headline


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (9.26, "9.3s"),  # not 9.25: that is a rounding tie, and ties are not what this tests
        (59.9, "59.9s"),
        (61.0, "1m 01s"),
        (599.0, "9m 59s"),
        (3600.0, "1h 00m"),
        (3900.0, "1h 05m"),
    ],
)
def test_durations_are_formatted_for_comparison_with_a_job_sheet(
    seconds: float, expected: str
) -> None:
    """A machinist compares cycle time to a job sheet, so "3900.0s" is the wrong unit to show."""
    assert _duration(seconds) == expected


# --------------------------------------------------------------------------- line ranges


def test_span_line_numbers_are_named_so_the_user_can_find_them(profile) -> None:
    """A warning that cannot be located in the file is barely actionable."""
    warnings = summary_of(fixture_text("canned_cycle_span.nc"), profile).warnings()
    assert "9-12" in warnings[0], warnings[0]


def test_many_spans_are_truncated_rather_than_filling_the_banner(profile) -> None:
    """A program with fifty canned cycles must not produce a fifty-range warning string."""
    body = "".join(f"G81 Z-5 R2 F100\nX{index} Y{index}\nG80\n" for index in range(20))
    summary = summary_of(f"G21 G90 G94 G54\nG0 Z5\n{body}M30\n", profile)
    assert len(summary.suppressed) > 3
    assert "and" in summary.warnings()[0] and "more" in summary.warnings()[0]
