"""The diff preview: review a fix before it is applied.

PLAN.md § Fix Engine requires a fix to be reviewable before application, and a unified diff is what a
machinist can actually check against their own program. Nothing is applied until Apply is pressed.

Two things this dialog must not do:

- **Present a refusal as a failure.** Several fixes decline on purpose — arc-centre recomputation past 10×
  tolerance, IJK→R on a full circle — and the reason is the useful part. `show_refusal` gives it its own
  presentation with no Apply button, because there is nothing to apply and offering the button would
  invite the user to try again harder.
- **Show a diff that is not what would be written.** The preview is generated from the same text the
  engine returns, so the two cannot diverge. `differ.apply_unified_diff` round-trips it in the tests.

The note is shown alongside the diff, and for a destructive fix it is the whole point: N-word stripping
can break a program in ways the diff does not show, because the diff shows changed lines and not that a
`GOTO N120` no longer has a target.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from foursight.fix.engine import Fix, FixResult

#: Diff line colours. Added and removed must differ by more than shade, since a diff is read at a glance.
ADDED_COLOR = "#6a8759"
REMOVED_COLOR = "#e05252"
HEADER_COLOR = "#808080"

_DESTRUCTIVE_STYLE = "background: #7a2a12; color: #ffe9c9; padding: 6px 9px; font-weight: 600;"
_NOTE_STYLE = "color: #b5a642; padding: 4px 9px;"


class DiffDialog(QDialog):
    """Shows a fix's diff and asks for confirmation. Returns ``Accepted`` only if Apply is pressed."""

    def __init__(self, fix: Fix, result: FixResult, parent=None) -> None:
        super().__init__(parent)
        self.fix = fix
        self.result = result
        self.setWindowTitle(f"Apply: {fix.title}")
        self.resize(900, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{fix.title}</b><br>{fix.description}"))

        if fix.destructive:
            # A destructive fix gets a banner, not a footnote. The diff cannot show what it breaks.
            warning = QLabel(
                "This change is destructive in ways the diff does not show. Review it against how the "
                "program is actually run."
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(_DESTRUCTIVE_STYLE)
            layout.addWidget(warning)

        if result.note:
            note = QLabel(result.note)
            note.setWordWrap(True)
            note.setStyleSheet(_NOTE_STYLE)
            layout.addWidget(note)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setPlainText(result.diff or "(no change)")
        _colourize(self.view)
        layout.addWidget(self.view, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.AcceptRole)
        self.apply_button.setEnabled(result.applied)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _colourize(view: QPlainTextEdit) -> None:
    """Colour added and removed lines. Applied after the text is set, per block."""
    document = view.document()
    for index in range(document.blockCount()):
        block = document.findBlockByNumber(index)
        text = block.text()
        if text.startswith(("+++", "---", "@@")):
            colour = HEADER_COLOR
        elif text.startswith("+"):
            colour = ADDED_COLOR
        elif text.startswith("-"):
            colour = REMOVED_COLOR
        else:
            continue
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colour))
        if text.startswith("@@"):
            fmt.setFontWeight(QFont.Bold)
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.mergeCharFormat(fmt)


class RefusalDialog(QDialog):
    """Shows why a fix declined. **No Apply button** — there is nothing to apply.

    Separate from `DiffDialog` on purpose. A refusal is a result, not an error: "we could change this but
    the intent is ambiguous" is genuinely useful, and presenting it with a greyed-out Apply would suggest
    the user might get it to work by trying again.
    """

    def __init__(self, fix: Fix, result: FixResult, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Cannot apply: {fix.title}")
        self.resize(680, 260)

        layout = QVBoxLayout(self)
        heading = QLabel(f"<b>{fix.title}</b> was not applied.")
        layout.addWidget(heading)

        reason = QLabel(result.refusal or "no reason given")
        reason.setWordWrap(True)
        reason.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )  # copyable, so it can be pasted into a note
        layout.addWidget(reason, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.reject)
        layout.addWidget(buttons)
