"""Diagnostics panel tests (T3.4). Headless via ``offscreen``; skips without the `[gui]` extra.

The requirement with teeth is that **`unsupported` reads differently from a warning**. They are not
degrees of the same thing: a warning says "look at this", while `unsupported` says "part of the picture
is missing". A panel that painted them alike would quietly undo the tier distinction the whole simulator
is built around — so each tier carries its own colour *and* its own symbol, since colour alone collapses
for a colour-blind reader.

Two supporting properties: the ordering is imported from `verify.report` rather than restated, so the
panel and the CLI cannot disagree about the same program; and a large result is truncated **visibly**,
because 155,958 diagnostics is a real number for a 100k-line program with no feed rates.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from foursight.gui.diagnostics_panel import (  # noqa: E402
    MAX_ROWS,
    TIER_STYLE,
    DiagnosticsPanel,
    sort_diagnostics,
    summarize_counts,
)
from foursight.verify.report import SEVERITY_RANK, Diagnostic, Severity  # noqa: E402


def diagnostic(severity: Severity, line: int, rule_id: str = "test.rule") -> Diagnostic:
    return Diagnostic(rule_id=rule_id, severity=severity, line=line, message=f"{rule_id} at {line}")


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qt_app):
    try:
        return DiagnosticsPanel()
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct the panel on this platform: {error}")


# --------------------------------------------------------------------------- the three tiers


def test_every_tier_has_a_style() -> None:
    """A missing entry would raise mid-populate, on whichever program first produced that tier."""
    assert set(TIER_STYLE) == set(Severity)


def test_unsupported_is_visually_distinct_from_warning() -> None:
    """The distinction that must not collapse. "Look at this" versus "part of the picture is missing"."""
    unsupported = TIER_STYLE[Severity.UNSUPPORTED]
    warning = TIER_STYLE[Severity.WARNING]
    assert unsupported[0] != warning[0], "same colour"
    assert unsupported[1] != warning[1], "same symbol"


def test_all_three_tiers_differ_in_both_colour_and_symbol() -> None:
    """Colour alone would collapse for a colour-blind reader; symbol alone is easy to overlook."""
    colours = [colour for colour, _ in TIER_STYLE.values()]
    symbols = [symbol for _, symbol in TIER_STYLE.values()]
    assert len(set(colours)) == 3
    assert len(set(symbols)) == 3


def test_a_row_carries_its_tier_symbol(panel) -> None:
    panel.set_diagnostics((diagnostic(Severity.UNSUPPORTED, 9),))
    item = panel.tree.topLevelItem(0)
    assert item.text(0) == TIER_STYLE[Severity.UNSUPPORTED][1]


# --------------------------------------------------------------------------- ordering


def test_worst_comes_first() -> None:
    mixed = (
        diagnostic(Severity.WARNING, 1),
        diagnostic(Severity.UNSUPPORTED, 2),
        diagnostic(Severity.ERROR, 3),
    )
    assert [d.severity for d in sort_diagnostics(mixed)] == [
        Severity.ERROR,
        Severity.UNSUPPORTED,
        Severity.WARNING,
    ]


def test_within_a_tier_the_order_is_by_line() -> None:
    """So a tier can be walked down the file rather than jumped around."""
    same_tier = (
        diagnostic(Severity.ERROR, 30),
        diagnostic(Severity.ERROR, 10),
        diagnostic(Severity.ERROR, 20),
    )
    assert [d.line for d in sort_diagnostics(same_tier)] == [10, 20, 30]


def test_the_order_uses_the_shared_rank_not_a_local_copy() -> None:
    """The panel and the CLI must not disagree about the same program.

    Asserted by deriving the expectation from `SEVERITY_RANK` itself, so a panel that hard-coded its own
    order would fail here rather than merely drift.
    """
    mixed = tuple(diagnostic(tier, 1) for tier in Severity)
    expected = sorted(Severity, key=lambda tier: SEVERITY_RANK[tier])
    assert [Severity(d.severity) for d in sort_diagnostics(mixed)] == expected


def test_the_order_is_stable_for_identical_line_and_tier() -> None:
    pair = (diagnostic(Severity.ERROR, 5, "b.rule"), diagnostic(Severity.ERROR, 5, "a.rule"))
    assert [d.rule_id for d in sort_diagnostics(pair)] == ["a.rule", "b.rule"]


# --------------------------------------------------------------------------- the header


def test_an_empty_result_says_no_problems_found() -> None:
    assert summarize_counts((), shown=0) == "No problems found"


def test_the_header_counts_each_tier() -> None:
    counts = summarize_counts(
        (
            diagnostic(Severity.ERROR, 1),
            diagnostic(Severity.ERROR, 2),
            diagnostic(Severity.WARNING, 3),
        ),
        shown=3,
    )
    assert "2 error" in counts
    assert "1 warning" in counts


def test_the_header_omits_tiers_with_no_findings() -> None:
    """ "0 unsupported" is noise that makes the real counts harder to read."""
    assert "unsupported" not in summarize_counts((diagnostic(Severity.ERROR, 1),), shown=1)


def test_truncation_is_stated_not_silent() -> None:
    """ "Showing everything" and "showing the first 2000" look identical without this."""
    many = tuple(diagnostic(Severity.WARNING, n) for n in range(1, 50))
    assert "showing the first" in summarize_counts(many, shown=10)
    assert "49" in summarize_counts(many, shown=10)


def test_no_truncation_notice_when_everything_is_shown() -> None:
    few = tuple(diagnostic(Severity.WARNING, n) for n in range(1, 4))
    assert "showing" not in summarize_counts(few, shown=3)


# --------------------------------------------------------------------------- populating


def test_rows_are_capped_but_the_count_is_not(panel) -> None:
    """A table with 155,958 rows is unusable and building it stalls the window."""
    many = tuple(diagnostic(Severity.WARNING, n) for n in range(1, MAX_ROWS + 500))
    panel.set_diagnostics(many)
    assert panel.tree.topLevelItemCount() == MAX_ROWS
    assert len(panel.diagnostics) == len(many)
    assert "showing the first" in panel.header.text()


def test_reloading_replaces_rather_than_appends(panel) -> None:
    panel.set_diagnostics((diagnostic(Severity.ERROR, 1), diagnostic(Severity.ERROR, 2)))
    panel.set_diagnostics((diagnostic(Severity.WARNING, 5),))
    assert panel.tree.topLevelItemCount() == 1


def test_pending_does_not_read_as_no_problems_found(panel) -> None:
    """An empty list while the check is still running would claim a clean bill of health we cannot give."""
    panel.set_pending()
    assert panel.tree.topLevelItemCount() == 0
    assert "No problems found" not in panel.header.text()
    assert "Checking" in panel.header.text()


def test_clearing_empties_the_panel(panel) -> None:
    panel.set_diagnostics((diagnostic(Severity.ERROR, 1),))
    panel.clear()
    assert panel.tree.topLevelItemCount() == 0
    assert panel.diagnostics == ()


# --------------------------------------------------------------------------- activation


def test_selecting_a_row_emits_its_line(panel) -> None:
    received = []
    panel.line_activated.connect(received.append)
    panel.set_diagnostics((diagnostic(Severity.ERROR, 42),))
    panel.tree.setCurrentItem(panel.tree.topLevelItem(0))
    assert received == [42]


def test_a_single_click_is_enough(panel) -> None:
    """Requiring a double click makes reviewing thirty findings thirty times more work."""
    received = []
    panel.line_activated.connect(received.append)
    panel.set_diagnostics((diagnostic(Severity.WARNING, 7), diagnostic(Severity.WARNING, 9)))
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    assert received[-1] == 9


def test_the_emitted_line_is_the_diagnostic_line_not_the_row_index(panel) -> None:
    """Off-by-one bait: the second row's diagnostic is on line 99, not line 2."""
    received = []
    panel.line_activated.connect(received.append)
    panel.set_diagnostics((diagnostic(Severity.ERROR, 5), diagnostic(Severity.ERROR, 99)))
    panel.tree.setCurrentItem(panel.tree.topLevelItem(1))
    assert received[-1] == 99


# --------------------------------------------------------------------------- diff preview (T5.5)


def test_a_destructive_fix_gets_a_banner_not_a_footnote(qt_app) -> None:
    """The diff cannot show what N-word stripping breaks, so the warning must be impossible to miss."""
    from PySide6.QtWidgets import QLabel

    from conftest import DEFAULT_PROFILE_PATH
    from foursight.fix.engine import FixContext, apply_fix, get_fix, load_builtin_fixes
    from foursight.gui.diff_dialog import DiffDialog
    from foursight.machine.profile import load_profile

    load_builtin_fixes()
    profile = load_profile(DEFAULT_PROFILE_PATH)
    result = apply_fix(
        "fix.strip-line-numbers", FixContext(text="N10 G1 X10 F600\n", profile=profile)
    )
    dialog = DiffDialog(get_fix("fix.strip-line-numbers"), result)
    labels = [label.text().lower() for label in dialog.findChildren(QLabel)]
    assert any("destructive" in text for text in labels)
    assert any("jump target" in text for text in labels), "the note must reach the dialog"


def test_a_non_destructive_fix_gets_no_banner(qt_app) -> None:
    """A warning on every fix stops being read."""
    from PySide6.QtWidgets import QLabel

    from conftest import DEFAULT_PROFILE_PATH
    from foursight.fix.engine import FixContext, apply_fix, get_fix, load_builtin_fixes
    from foursight.gui.diff_dialog import DiffDialog
    from foursight.machine.profile import load_profile

    load_builtin_fixes()
    result = apply_fix(
        "fix.append-program-end",
        FixContext(text="G1 X10 F600\n", profile=load_profile(DEFAULT_PROFILE_PATH)),
    )
    dialog = DiffDialog(get_fix("fix.append-program-end"), result)
    labels = [label.text().lower() for label in dialog.findChildren(QLabel)]
    assert not any("destructive" in text for text in labels)


def test_a_refusal_dialog_offers_nothing_to_apply(qt_app) -> None:
    """A greyed-out Apply would suggest the user could get it to work by trying again."""
    from PySide6.QtWidgets import QPushButton

    from conftest import DEFAULT_PROFILE_PATH
    from foursight.fix.engine import FixContext, apply_fix, get_fix, load_builtin_fixes
    from foursight.gui.diff_dialog import RefusalDialog
    from foursight.machine.profile import load_profile

    load_builtin_fixes()
    result = apply_fix(
        "fix.recompute-arc-centre",
        FixContext(
            text="G21 G17\nG0 X0 Y0\nG2 X20 Y0 I12 J0 F600\n",
            profile=load_profile(DEFAULT_PROFILE_PATH),
            line=3,
        ),
    )
    assert result.refused
    dialog = RefusalDialog(get_fix("fix.recompute-arc-centre"), result)
    assert not any(button.text() == "Apply" for button in dialog.findChildren(QPushButton)), (
        "a refusal must offer no Apply"
    )
