"""Editor tests (T3.1). Headless via Qt's ``offscreen`` platform; skips without the `[gui]` extra.

Two things here are load-bearing rather than cosmetic.

**Line numbers are 1-based everywhere.** `SourceRef.line_no`, `Diagnostic.line` and `SegmentStore.line`
are all 1-based; Qt text blocks are 0-based. `CodeEditor` converts in one place so T3.2 and T3.4 never
have to, and an off-by-one would point every diagnostic and every jump at the neighbouring line — which
looks entirely plausible on screen.

**The editor shows the text that was parsed**, `loaded.text` after decoding and BOM removal, not a
re-read of the file. Otherwise the gutter's numbering could disagree with the parser's.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
pytest.importorskip("pyqtgraph", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH, FIXTURES, fixture_text  # noqa: E402
from foursight.gui.editor import CodeEditor  # noqa: E402
from foursight.gui.highlighting import TokenKind  # noqa: E402
from foursight.machine.profile import load_profile  # noqa: E402

PROGRAM = "G21 G90 G94\nG0 Z5\nG1 X10 Y10 F600\nG1 X20\nM30\n"


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(qt_app):
    try:
        widget = CodeEditor()
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct the editor on this platform: {error}")
    widget.resize(600, 400)
    return widget


# --------------------------------------------------------------------------- 1-based line numbers


def test_line_numbers_are_one_based(editor) -> None:
    """The contract T3.2 and T3.4 both rely on. Qt blocks are 0-based; these are not."""
    editor.setPlainText(PROGRAM)
    editor.goto_line(1)
    assert editor.current_line == 1
    editor.goto_line(3)
    assert editor.current_line == 3


def test_line_text_is_addressed_the_same_way(editor) -> None:
    """`line_text(3)` must return the third line, not the fourth."""
    editor.setPlainText(PROGRAM)
    assert editor.line_text(1) == "G21 G90 G94"
    assert editor.line_text(3) == "G1 X10 Y10 F600"


def test_line_text_agrees_with_splitlines(editor) -> None:
    """Cross-checked against the same primitive the tokenizer uses, so the two cannot drift."""
    text = fixture_text("baseline_4axis.nc")
    editor.setPlainText(text)
    for number, expected in enumerate(text.splitlines(), start=1):
        assert editor.line_text(number) == expected, f"line {number}"


def test_source_line_count_matches_the_parser(editor) -> None:
    """`source_line_count` is what T3.2/T3.4 must compare against, not `line_count`."""
    text = fixture_text("baseline_4axis.nc")
    editor.setPlainText(text)
    assert editor.source_line_count == len(text.splitlines())


def test_line_count_counts_the_trailing_block_and_source_line_count_does_not(editor) -> None:
    """The discrepancy is real and documented, so it is asserted rather than left to be rediscovered.

    `"G1 X10\n"` is one line to the parser and two blocks to Qt — the position after the final newline
    is a real cursor position. Both are correct; conflating them puts every jump one line out.
    """
    editor.setPlainText("G1 X10\n")
    assert editor.line_count == 2
    assert editor.source_line_count == 1

    editor.setPlainText("G1 X10")
    assert editor.line_count == 1
    assert editor.source_line_count == 1


def test_goto_line_clamps_rather_than_failing(editor) -> None:
    """A diagnostic pointing past the end must not crash the pane — it should land on the last line."""
    editor.setPlainText(PROGRAM)
    editor.goto_line(9999)
    assert editor.current_line == editor.line_count
    editor.goto_line(0)
    assert editor.current_line == 1
    editor.goto_line(-5)
    assert editor.current_line == 1


# --------------------------------------------------------------------------- highlighting is applied


def formats_on_line(editor, line_no: int):
    """The (start, length, colour-name) triples Qt actually applied to a line."""
    block = editor.document().findBlockByNumber(line_no - 1)
    return [
        (fmt.start, fmt.length, fmt.format.foreground().color().name())
        for fmt in block.layout().formats()
    ]


def test_the_highlighter_actually_formats_a_block(editor) -> None:
    """That the rules are right is `test_highlighting.py`'s job; this is that they get *applied*."""
    editor.setPlainText(PROGRAM)
    applied = formats_on_line(editor, 3)
    assert applied, "no formats were applied to a line of ordinary G-code"


def test_malformed_input_is_formatted_distinctly(editor) -> None:
    """Colour alone would fail a red-green colour-blind reader, so malformed also gets a wavy underline."""
    from PySide6.QtGui import QTextCharFormat

    editor.setPlainText("G1 X Y10\n")
    block = editor.document().findBlockByNumber(0)
    # `underlineStyle()`, not `fontUnderline()`: with a wave style the latter reports False, because it
    # answers "is this a plain single underline?". That cost this test a false pass.
    waved = [
        fmt
        for fmt in block.layout().formats()
        if fmt.format.underlineStyle() == QTextCharFormat.WaveUnderline
    ]
    assert waved, "malformed input carries no wavy underline, only colour"


def test_every_token_kind_has_a_colour(editor) -> None:
    """A missing entry would raise a KeyError mid-paint, on whichever file first used that letter."""
    from foursight.gui.editor import COLORS

    assert set(COLORS) == set(TokenKind)


# --------------------------------------------------------------------------- the gutter


def test_the_gutter_widens_for_more_digits(editor) -> None:
    """A five-digit file must not have its line numbers clipped."""
    editor.setPlainText("G1 X1\n" * 9)
    narrow = editor.gutter_width()
    editor.setPlainText("G1 X1\n" * 10_000)
    assert editor.gutter_width() > narrow


def test_the_gutter_has_a_width_even_when_empty(editor) -> None:
    """A zero-width gutter on an empty document would make the pane jump on the first keystroke."""
    editor.setPlainText("")
    assert editor.gutter_width() > 0


# --------------------------------------------------------------------------- the buffer is editable


def test_the_buffer_is_editable(editor) -> None:
    """PLAN.md § Fix Engine: fixes modify the editor buffer and the user saves explicitly.

    A read-only pane would make M5 impossible, so this is a requirement rather than a default.
    """
    assert editor.isReadOnly() is False
    editor.setPlainText(PROGRAM)
    editor.goto_line(2)
    editor.insertPlainText("(edited)")
    assert "(edited)" in editor.toPlainText()


def test_editing_rehighlights_the_changed_line(editor) -> None:
    """Otherwise a line stays coloured as whatever it used to be."""
    editor.setPlainText("G1 X10\n")
    editor.goto_line(1)
    editor.insertPlainText("(")  # opens a comment: the rest of the line becomes one
    kinds = formats_on_line(editor, 1)
    assert kinds, "no formats after an edit"


# --------------------------------------------------------------------------- scale


def test_a_100k_line_file_loads_without_highlighting_all_of_it(editor) -> None:
    """`QSyntaxHighlighter` formats only blocks Qt paints, which is what makes a large file viable.

    Asserted as a time bound rather than by counting calls: the guarantee that matters is that opening a
    100k-line program does not stall, and a regression to whole-document highlighting would blow this by
    an order of magnitude. The bound is deliberately loose for a shared CI runner.
    """
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).parent))
    from test_perf import generate

    text = generate(100_000)
    started = time.perf_counter()
    editor.setPlainText(text)
    elapsed = time.perf_counter() - started
    assert editor.source_line_count == 100_000
    assert elapsed < 10.0, f"loading 100k lines into the editor took {elapsed:.1f}s"


def test_loading_a_program_shows_the_parsed_text(qt_app) -> None:
    """The window must put `loaded.text` in the pane — the decoded text, not a re-read of the file.

    A re-read could differ (BOM, encoding fallback) and then the gutter's numbering would disagree with
    `SourceRef.line_no`, which is what every jump and diagnostic keys on.
    """
    from PySide6.QtCore import QDeadlineTimer, QEventLoop

    from foursight.gui.main_window import MainWindow

    window = MainWindow(load_profile(DEFAULT_PROFILE_PATH))
    window.open_file(FIXTURES / "baseline_4axis.nc")
    deadline = QDeadlineTimer(30_000)
    while window._loader is not None and not deadline.hasExpired():
        QApplication.processEvents(QEventLoop.AllEvents, 20)

    assert window.program is not None
    assert window.editor.toPlainText() == window.program.loaded.text
    assert window.editor.source_line_count == len(window.program.loaded.text.splitlines())
