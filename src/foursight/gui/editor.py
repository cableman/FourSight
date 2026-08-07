"""The code pane: a G-code editor with a line-number gutter and syntax highlighting.

Thin on purpose. What to colour is decided by `highlighting.py`, which is Qt-free and shares the
parser's own regexes, so the editor cannot disagree with `foursight check` about what a word is. This
module only maps `TokenKind` to a `QTextCharFormat` and draws the gutter.

**Editable, not read-only.** PLAN.md § Fix Engine: *"Fixes never write the original file; they modify
the editor buffer and the user saves explicitly."* A read-only pane would make M5 impossible, so the
buffer is the editable thing from the start.

Highlighting is **per visible block**, which is what `QSyntaxHighlighter` does natively: Qt calls
`highlightBlock` only for blocks it needs to paint, so a 100k-line file costs nothing to open beyond
loading the text. That is why there is no attempt to highlight the whole document up front.

The gutter shows **source line numbers** — the same numbers `SourceRef.line_no`, `Diagnostic.line` and
`SegmentStore.line` use, all 1-based. Editor↔viewport sync in T3.2/T3.4 keys on them, so an off-by-one
here would misattribute every jump.
"""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from foursight.gui.highlighting import TokenKind, spans_for_line

# One colour per role. Chosen to survive a light or dark background: mid-tones rather than extremes,
# and malformed input is the only red, so it cannot be confused with anything else.
COLORS: dict[TokenKind, str] = {
    TokenKind.COMMENT: "#6a8759",
    TokenKind.BLOCK_DELETE: "#9876aa",
    TokenKind.LINE_NUMBER: "#808080",
    TokenKind.GCODE: "#4a9edd",
    TokenKind.MCODE: "#c9986a",
    TokenKind.AXIS: "#d8d8d8",
    TokenKind.ARC: "#8fbcbb",
    TokenKind.PARAMETER: "#b5a642",
    TokenKind.TOOL: "#c586c0",
    TokenKind.OTHER_WORD: "#a0a0a0",
    TokenKind.MALFORMED: "#e05252",
}
BOLD_KINDS = frozenset({TokenKind.GCODE, TokenKind.MCODE, TokenKind.BLOCK_DELETE})

GUTTER_MARGIN_PX = 12
CURRENT_LINE_COLOR = "#2a2d2e"


class GCodeHighlighter(QSyntaxHighlighter):
    """Applies `highlighting.spans_for_line` to each block Qt asks about."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._formats = {kind: _format(kind) for kind in TokenKind}

    def highlightBlock(self, text: str) -> None:
        for span in spans_for_line(text):
            self.setFormat(span.start, span.length, self._formats[span.kind])


def _format(kind: TokenKind) -> QTextCharFormat:
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(COLORS[kind]))
    if kind in BOLD_KINDS:
        fmt.setFontWeight(QFont.Bold)
    if kind is TokenKind.MALFORMED:
        # Colour alone is not enough for the one role that means "this will not run": a red-green
        # colour-blind reader would see malformed input as ordinary text.
        #
        # Only the style is set. `setFontUnderline(True)` alongside it is not merely redundant but
        # misleading: with a wave style, `fontUnderline()` reports **False**, since it answers "is this a
        # plain single underline?". Check `underlineStyle()` instead.
        fmt.setUnderlineStyle(QTextCharFormat.WaveUnderline)
    return fmt


class LineNumberGutter(QWidget):
    """The margin that draws line numbers. Owned by `CodeEditor`, never used alone."""

    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_gutter(event)


class CodeEditor(QPlainTextEdit):
    """A monospaced G-code pane with a line-number gutter.

    Exposes `line_count`, `current_line` and `goto_line` in **1-based source line numbers**, matching
    `SourceRef.line_no`, so callers never have to remember that Qt blocks are 0-based. That conversion
    living in one place is the point: T3.2 and T3.4 both sync on these numbers, and an off-by-one would
    quietly point every diagnostic and every jump at the neighbouring line.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabChangesFocus(True)

        self.highlighter = GCodeHighlighter(self.document())
        self.gutter = LineNumberGutter(self)

        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()

    # ------------------------------------------------------------------ line numbers (1-based)

    @property
    def line_count(self) -> int:
        """Qt blocks, which is **one more** than the parser sees for newline-terminated text.

        `"G1 X10\n"` is one line to `str.splitlines()` and two blocks to Qt: the position after the final
        newline is a real place to put a cursor. Both are right for their own purpose. Use
        `source_line_count` when comparing against anything the parser produced.
        """
        return self.blockCount()

    @property
    def source_line_count(self) -> int:
        """Lines as the *parser* counts them, matching `str.splitlines()` and `SourceRef.line_no`.

        This exists so T3.2 and T3.4 never have to reason about the trailing-block discrepancy above.
        Getting it wrong points a jump one line off, which looks entirely plausible on screen and is
        exactly the class of bug the 1-based conversion is centralized to prevent.
        """
        count = self.blockCount()
        if count > 1 and not self.document().findBlockByNumber(count - 1).text():
            return count - 1
        return count

    @property
    def current_line(self) -> int:
        return self.textCursor().blockNumber() + 1

    def goto_line(self, line_no: int) -> None:
        """Move the cursor to the start of 1-based ``line_no``, clamped to the document."""
        block = self.document().findBlockByNumber(max(0, min(line_no, self.blockCount()) - 1))
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()

    def line_text(self, line_no: int) -> str:
        return self.document().findBlockByNumber(line_no - 1).text()

    # ------------------------------------------------------------------ gutter

    def gutter_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return GUTTER_MARGIN_PX + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        area = self.contentsRect()
        self.gutter.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))

    def paint_gutter(self, event) -> None:
        """Draw numbers for the visible blocks only — a 100k-line file must not paint 100k labels."""
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor("#1e1e1e"))
        painter.setPen(QColor(COLORS[TokenKind.LINE_NUMBER]))

        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        height = self.blockBoundingRect(block).height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and top + height >= event.rect().top():
                painter.drawText(
                    0,
                    int(top),
                    self.gutter.width() - GUTTER_MARGIN_PX // 2,
                    int(height),
                    Qt.AlignRight | Qt.AlignVCenter,
                    str(block.blockNumber() + 1),
                )
            block = block.next()
            top += height
            height = self.blockBoundingRect(block).height()

    # ------------------------------------------------------------------ current line

    def _highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor(CURRENT_LINE_COLOR))
        selection.format.setProperty(QTextFormat.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])
