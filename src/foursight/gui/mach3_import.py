"""The Mach3 import review table: what would change, what would not, and why.

Three things this dialog is responsible for, all of them about *seeing* the mapping rather than doing
it — `machine/mach3_xml.py` owns the mapping and has no Qt (PLAN.md § Importing a Mach3 profile):

- **Nothing is applied from here.** Accepting writes the chosen rows into the profile form as pending
  edits, so the import lands on the same Apply that validates a hand edit. An import the loader would
  refuse cannot take effect, and Revert discards it like anything else.
- **The rows that write nothing are still shown.** A conflict the XML states and a profile cannot —
  `<ShortRot>1` against a non-wrapping axis — is exactly what the operator opened the dialog to find
  out, so it appears greyed with the question that settles it rather than being dropped.
- **The units selector redraws the values.** `<Units>` is documented (`0 = mm`, `1 = inch`) but a
  mis-set flag is the commonest Mach3 setup error, and the check for it cannot be watertight in both
  directions. An operator recognises their own machine's rapid rate at a glance, so the numbers are on
  screen in the units chosen and one click switches them.
"""

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
)

from foursight.machine.mach3_xml import ImportPlan, Mach3Profile, Row, plan_import
from foursight.machine.profile_doc import Edit, ProfileDocument

MACH3_FILTER = "Mach3 profile (*.xml);;All files (*)"

_MUTED = "color: #9a9a9a;"
_WARNING = "background: #6a4a12; color: #ffe9c9; padding: 6px 9px; font-weight: 600;"
_UNAVAILABLE = "#9a9a9a"

_COLUMNS = ("Profile key", "From Mach3", "Value", "Note")


class Mach3ImportDialog(QDialog):
    """Review one Mach3 profile's mapping onto the open document."""

    def __init__(self, mach3: Mach3Profile, document: ProfileDocument, parent=None) -> None:
        super().__init__(parent)
        self._mach3 = mach3
        self._document = document
        self.setWindowTitle("Import from Mach3")
        self.resize(860, 620)

        layout = QVBoxLayout(self)
        heading = QLabel(
            f"<b>{escape(mach3.name)}</b><br>{escape(str(mach3.path)) if mach3.path else ''}"
            f"<br>Values are written in this profile's units (<b>{document.declared_units}</b>), "
            "which the import never changes."
        )
        heading.setWordWrap(True)
        heading.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(heading)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("Mach3 native units:"))
        self.units = QComboBox()
        self.units.addItems(["mm", "inch"])
        picker.addWidget(self.units)
        note = QLabel("Mach3's Config → Select Native Units, read from &lt;Units&gt;.")
        note.setStyleSheet(_MUTED)
        picker.addWidget(note)
        picker.addStretch(1)
        layout.addLayout(picker)

        self._warning = QLabel()
        self._warning.setWordWrap(True)
        # A warning quotes <Units>, and a rich-text QLabel would swallow it exactly as the notes did.
        self._warning.setTextFormat(Qt.PlainText)
        self._warning.setStyleSheet(_WARNING)
        self._warning.hide()
        layout.addWidget(self._warning)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, stretch=3)

        layout.addWidget(QLabel("Not imported, and why:"))
        self.notes = QTextBrowser()
        layout.addWidget(self.notes, stretch=2)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.import_button = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._rows: list[Row] = []
        self.plan: ImportPlan = plan_import(document, mach3)
        self.units.setCurrentText(self.plan.units)
        self._show(self.plan)
        # Connected after the initial fill so setting the combo does not re-plan on the way up.
        self.units.currentTextChanged.connect(self._on_units_changed)

    # ------------------------------------------------------------------ state

    def _on_units_changed(self, units: str) -> None:
        """Re-plan from scratch: a different native unit changes the ticks, not only the numbers."""
        self.plan = plan_import(self._document, self._mach3, units)
        self._show(self.plan)

    def _show(self, plan: ImportPlan) -> None:
        self._rows = list(plan.rows)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self._fill(index, row)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._warning.setText("\n".join(plan.warnings))
        self._warning.setVisible(bool(plan.warnings))
        # Escaped, because every note names the tag it came from and `QTextBrowser` would otherwise
        # read `<Rot360>` as an unknown HTML element and show the sentence with its subject missing.
        self.notes.setHtml(
            "<ul>"
            + "".join(
                f"<li><b>{escape(note.subject)}</b> — {escape(note.reason)}</li>"
                for note in plan.notes
            )
            + "</ul>"
        )

    def _fill(self, index: int, row: Row) -> None:
        key = QTableWidgetItem(row.target)
        key.setFlags(
            Qt.ItemIsEnabled | (Qt.ItemIsUserCheckable if row.writable else Qt.NoItemFlags)
        )
        key.setCheckState(Qt.Checked if row.ticked and row.writable else Qt.Unchecked)
        cells = [key, QTableWidgetItem(row.source), QTableWidgetItem(row.shown)]
        cells.append(QTableWidgetItem(row.caveat))
        for column, cell in enumerate(cells):
            if column:
                cell.setFlags(Qt.ItemIsEnabled)
            if not row.writable:
                cell.setForeground(Qt.gray)
            if row.caveat:
                cell.setToolTip(row.caveat)
            self.table.setItem(index, column, cell)

    # ------------------------------------------------------------------ result

    def chosen(self) -> list[Row]:
        """The ticked rows, in table order."""
        return [
            row
            for index, row in enumerate(self._rows)
            if self.table.item(index, 0).checkState() == Qt.Checked
        ]

    def edits(self) -> list[Edit]:
        return self.plan.edits(self.chosen())
