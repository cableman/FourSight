"""The diagnostics list: click a finding, jump to its line.

PLAN.md's three tiers have to be **visually distinct here**, because this is the only place the user sees
them side by side and they mean genuinely different things:

- **error** — malformed, or would break the machine.
- **unsupported** — well-formed, recognized, affects motion, and *not interpreted*. The span is not drawn.
  This is the tier a naive panel would render as a warning, which would be wrong: a warning says "look at
  this", while unsupported says "part of the picture is missing".
- **warning** — suspicious, or unrecognized but inert. Drawn normally.

So each tier gets its own colour *and* its own symbol. Colour alone would collapse the distinction for a
colour-blind reader, and this is exactly the distinction that must not collapse.

**Sorted worst-first, then by line.** `report.SEVERITY_RANK` already exists for this and is imported
rather than re-stated — a second ordering that drifted from the CLI's would mean the same program reads
differently in the two front ends.

**Large results are truncated, and say so.** A 100k-line program with no feed rates produces 155,958
diagnostics; a table with that many rows is unusable and building it stalls the window. The cap is
explicit in the header rather than silent, because "showing everything" and "showing the first 2000" look
identical otherwise.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from foursight.verify.report import SEVERITY_RANK, Diagnostic, Severity

#: Colour and symbol per tier. The symbol matters as much as the colour: `unsupported` must not read as a
#: warning, and a red-green colour-blind reader would otherwise see error and warning as the same thing.
TIER_STYLE: dict[Severity, tuple[str, str]] = {
    Severity.ERROR: ("#e05252", "✖"),
    Severity.UNSUPPORTED: ("#c9986a", "◌"),
    Severity.WARNING: ("#b5a642", "▲"),
}

#: Rows beyond this are not built. See the module docstring: silent truncation is the thing to avoid.
MAX_ROWS = 2000

COLUMNS = ("", "Line", "Rule", "Message")


class DiagnosticsPanel(QWidget):
    """A sortable list of findings. Selecting one emits `line_activated`."""

    #: The source line of the selected diagnostic, 1-based, matching `SourceRef.line_no`.
    line_activated = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.diagnostics: tuple[Diagnostic, ...] = ()

        self.header = QLabel("No file loaded")
        self.header.setStyleSheet("padding: 4px 8px; color: #a0a0a0;")

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)  # lets Qt skip per-row measurement on a long list
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.setColumnWidth(0, 24)
        self.tree.setColumnWidth(1, 64)
        self.tree.setColumnWidth(2, 200)

        # Single click, not double: a diagnostics list is for walking, and requiring a double click on
        # every finding makes reviewing thirty of them thirty times more work than it should be.
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemActivated.connect(lambda item, _column: self._emit_for(item))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.tree, stretch=1)

    # ------------------------------------------------------------------ content

    def set_diagnostics(self, diagnostics: tuple[Diagnostic, ...]) -> None:
        """Replace the list. Sorted worst-first, then by line, and truncated with a visible count."""
        self.diagnostics = tuple(diagnostics)
        self.tree.clear()

        ordered = sort_diagnostics(self.diagnostics)
        for diagnostic in ordered[:MAX_ROWS]:
            self.tree.addTopLevelItem(_row(diagnostic))
        self.header.setText(summarize_counts(self.diagnostics, shown=min(len(ordered), MAX_ROWS)))

    def clear(self) -> None:
        self.diagnostics = ()
        self.tree.clear()
        self.header.setText("No file loaded")

    def set_pending(self) -> None:
        """Shown while stage two runs, so an empty list does not read as "no problems found"."""
        self.tree.clear()
        self.diagnostics = ()
        self.header.setText("Checking…")

    # ------------------------------------------------------------------ selection

    def _on_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if items:
            self._emit_for(items[0])

    def _emit_for(self, item: QTreeWidgetItem) -> None:
        line_no = item.data(1, Qt.UserRole)
        if isinstance(line_no, int) and line_no > 0:
            self.line_activated.emit(line_no)


def _row(diagnostic: Diagnostic) -> QTreeWidgetItem:
    colour, symbol = TIER_STYLE[Severity(diagnostic.severity)]
    item = QTreeWidgetItem([symbol, str(diagnostic.line), diagnostic.rule_id, diagnostic.message])
    item.setData(1, Qt.UserRole, int(diagnostic.line))
    item.setForeground(0, QColor(colour))
    item.setForeground(2, QColor(colour))
    item.setToolTip(3, f"{diagnostic.severity}: {diagnostic.message}")
    return item


def sort_diagnostics(diagnostics: tuple[Diagnostic, ...]) -> list[Diagnostic]:
    """Worst first, then by line, then by rule for a stable order.

    `SEVERITY_RANK` is imported from `verify.report` rather than restated: a second ordering that drifted
    from the CLI's would make the same program read differently in the two front ends.
    """
    return sorted(
        diagnostics,
        key=lambda d: (SEVERITY_RANK[Severity(d.severity)], d.line, d.rule_id),
    )


def summarize_counts(diagnostics: tuple[Diagnostic, ...], *, shown: int) -> str:
    """The header line: counts per tier, and what was left out if anything was."""
    if not diagnostics:
        return "No problems found"
    counts = dict.fromkeys(SEVERITY_RANK, 0)
    for diagnostic in diagnostics:
        counts[Severity(diagnostic.severity)] += 1
    parts = [
        f"{counts[tier]} {tier.value}"
        for tier in sorted(counts, key=lambda t: SEVERITY_RANK[t])
        if counts[tier]
    ]
    text = "  ·  ".join(parts)
    if shown < len(diagnostics):
        text += f"   (showing the first {shown:,} of {len(diagnostics):,})"
    return text
