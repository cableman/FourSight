"""The machine-profile editor: a structured form over `ProfileDocument`.

Generated from `machine/profile_schema.GROUPS`, so a new profile key becomes a new row in that table
rather than new widget code here — and a key the loader gained but the schema forgot is a test failure
rather than a setting nobody can reach.

Four decisions that are not cosmetic:

- **Apply changes the profile in memory and nothing on disk.** The same contract the fix engine has for
  G-code: FourSight does not write the user's files, it re-runs and lets them save deliberately. So the
  window swaps its profile and re-runs parse → simulate → verify, and `Save as…` is a separate act.
  It also means a limit can be *tried* — the point of the exercise, since the alternative is editing a
  file and restarting the application.
- **Values are edited as text, not in spin boxes.** A `QDoubleSpinBox` has a fixed decimal count, and
  the profile mixes `3000.0` with `arc_radius_mismatch = 0.005` and an inch profile's `0.0002`. Any one
  setting rounds one of them. A line edit writes back exactly what was typed.
- **Optional fields have a "set" box, and clearing it writes nothing.** PLAN.md § Loading rules: absence
  means unknown and disables a check, so an unset field must not become `0`. The box is the only honest
  way for a form to express that difference, and it is why the schema records `optional` at all.
- **A `ProfileError` leaves the old profile in force.** The message appears in the dialog against the
  edit that caused it, nothing is re-run, and the user can keep editing. Half-applying a profile would
  leave the window verifying against a machine that does not exist.
"""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from foursight.gui.mach3_import import MACH3_FILTER, Mach3ImportDialog
from foursight.machine.mach3_xml import read as read_mach3
from foursight.machine.profile import ProfileError, default_profile_path
from foursight.machine.profile_doc import UNSET, Edit, ProfileDocument, render
from foursight.machine.profile_schema import GROUPS, Field, Group, Kind, unit_label

PROFILE_FILTER = "Machine profile (*.toml);;All files (*)"

_HELP_STYLE = "color: #9a9a9a;"
_ERROR_STYLE = "background: #7a2a12; color: #ffe9c9; padding: 6px 9px; font-weight: 600;"
_PATH_STYLE = "color: #9a9a9a;"

#: Numeric kinds, all edited as free text. `TEXT` is excluded because it is a name, and `CHOICE`/`BOOL`
#: have their own widgets.
_NUMERIC = frozenset(
    {Kind.LENGTH, Kind.RATE, Kind.ANGLE, Kind.ANGLE_RATE, Kind.COUNT, Kind.VEC3, Kind.OFFSET}
)

#: How many numbers each kind edits, and the label under each box.
_VECTOR_PARTS: dict[Kind, tuple[str, ...]] = {
    Kind.VEC3: ("X", "Y", "Z"),
    # The mixed-units case PLAN.md keeps warning about: three lengths and one angle in one list. The
    # A box is labelled in degrees so nobody types millimetres into it.
    Kind.OFFSET: ("X", "Y", "Z", "A"),
}


class FieldError(ValueError):
    """A value the form cannot turn into TOML. Reported against the field, not raised at the user."""


class ProfileDialog(QDialog):
    """View and edit the loaded machine profile.

    Emits `applied` with a validated `ProfileDocument` each time Apply succeeds. The dialog stays open,
    because trying a limit and looking at the diagnostics is the workflow.
    """

    applied = Signal(object)  # ProfileDocument

    def __init__(self, document: ProfileDocument, path: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self.document = document
        self.path = path
        self.setWindowTitle("Machine profile")
        self.resize(720, 760)

        layout = QVBoxLayout(self)
        self._heading = QLabel()
        self._heading.setWordWrap(True)
        self._heading.setStyleSheet(_PATH_STYLE)
        self._heading.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._heading)

        self._groups: list[_GroupEditor] = []
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        for group in GROUPS:
            editor = _GroupEditor(group, document)
            editor.changed.connect(self._on_field_changed)
            self._groups.append(editor)
            body_layout.addWidget(editor.box)
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        self._error = QLabel()
        self._error.setWordWrap(True)
        self._error.setStyleSheet(_ERROR_STYLE)
        self._error.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._error.hide()
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.revert_button = buttons.addButton("Revert", QDialogButtonBox.ResetRole)
        self.import_button = buttons.addButton("Import from Mach3…", QDialogButtonBox.ActionRole)
        self.save_button = buttons.addButton("Save as…", QDialogButtonBox.ActionRole)
        self.apply_button = buttons.addButton("Apply", QDialogButtonBox.ApplyRole)
        self.revert_button.clicked.connect(self.revert)
        self.import_button.clicked.connect(self.import_mach3)
        self.save_button.clicked.connect(self.save_as)
        self.apply_button.clicked.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_heading()
        self._on_field_changed()

    # ------------------------------------------------------------------ state

    def _refresh_heading(self) -> None:
        name = self.document.value("machine", "name", "unnamed")
        where = str(self.path) if self.path else "(no file — the profile shipped with FourSight)"
        note = ""
        if self.path is not None and self.path == default_profile_path():
            # Writing here would edit the installed package, and in a PyInstaller bundle the directory
            # may not even be writable. Apply still works: it never touches disk.
            note = "  ·  packaged with FourSight — use Save as… to keep changes"
        self._heading.setText(f"<b>{name}</b><br>{where}{note}")

    def edits(self) -> list[Edit]:
        """Every change the form represents, relative to the document it was opened with."""
        return [edit for group in self._groups for edit in group.edits()]

    def _on_field_changed(self) -> None:
        """Re-evaluate which fields are reachable, and whether there is anything to apply.

        A half-typed number must not throw out of a Qt slot, so an invalid field counts as a *change*
        and leaves Apply enabled — pressing it is how the user gets told what is wrong.
        """
        for group in self._groups:
            group.update_gating(self._pending_value)
        try:
            dirty = bool(self.edits())
        except FieldError:
            dirty = True
        self.apply_button.setEnabled(dirty)
        self.revert_button.setEnabled(dirty)

    def _pending_value(self, section: str, key: str):
        """The value another field currently holds, for `Field.requires`.

        Read from the widgets rather than from the document, so choosing Mach3 enables its two settings
        immediately instead of only after Apply.
        """
        for group in self._groups:
            if group.group.section == section:
                row = group.rows.get(key)
                if row is not None:
                    return row.current_or_none()
        return self.document.value(section, key)

    # ------------------------------------------------------------------ actions

    def apply(self) -> bool:
        """Validate the form, build the new document, and emit it. Returns whether it took.

        Nothing partial: either the whole edit set produces a profile the loader accepts, or the old
        document stays and the reason is shown.
        """
        self._error.hide()
        try:
            edits = self.edits()
        except FieldError as error:
            return self._fail(str(error))
        if not edits:
            return False
        try:
            updated = self.document.apply(edits)
            updated.profile()  # validates; the caller re-runs with it
        except ProfileError as error:
            return self._fail(str(error))

        self.document = updated
        self._reload_widgets()
        self._refresh_heading()
        self.applied.emit(updated)
        return True

    def _fail(self, message: str) -> bool:
        self._error.setText(message)
        self._error.show()
        return False

    def import_mach3(self) -> bool:
        """Read a Mach3 profile and offer its mapping as pending edits. Returns whether any landed.

        Deliberately not an apply: the rows arrive in the form, where the user can look at them, change
        them, and press the same Apply that validates a hand edit. An import FourSight would refuse
        therefore cannot take effect, and Revert discards it like anything else.
        """
        self._error.hide()
        suggested = str(self.path.parent if self.path else Path.home())
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Import a Mach3 profile", suggested, MACH3_FILTER
        )
        if not chosen:
            return False
        try:
            mach3 = read_mach3(Path(chosen))
        except (OSError, ProfileError) as error:
            return self._fail(str(error))
        review = Mach3ImportDialog(mach3, self.document, self)
        if review.exec() != QDialog.Accepted:
            return False
        return self.propose(review.edits())

    def propose(self, edits: list[Edit]) -> bool:
        """Show ``edits`` in the form as unapplied changes. Returns whether anything was placed.

        A target with no row is reported rather than dropped: silently losing part of an import is the
        one outcome worse than refusing it, because the profile then looks imported and is not.
        """
        placed, missing = 0, []
        for edit in edits:
            row = self._row(edit.section, edit.key)
            if row is None:
                missing.append(f"[{edit.section}].{edit.key}")
                continue
            row.show_value(edit.value)
            placed += 1
        self._on_field_changed()
        if missing:
            self._fail("the form has no field for " + ", ".join(missing))
        return placed > 0

    def _row(self, section: str, key: str | None) -> "_Row | None":
        if key is None:
            return None
        for group in self._groups:
            if group.group.section == section:
                return group.rows.get(key)
        return None

    def revert(self) -> None:
        """Discard unapplied edits, back to the last applied (or opened) document."""
        self._error.hide()
        self._reload_widgets()

    def _reload_widgets(self) -> None:
        for group in self._groups:
            group.reload(self.document)
        self._on_field_changed()

    def save_as(self) -> Path | None:
        """Write the profile — including unapplied edits, once they validate — to a chosen file.

        Applying first is deliberate: saving a file whose contents had never been validated would put a
        profile on disk that FourSight itself would refuse to load.
        """
        try:
            pending = bool(self.edits())
        except FieldError as error:
            self._fail(str(error))
            return None
        if pending and not self.apply():
            return None
        suggested = str(self.path.parent if self.path else Path.home())
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save machine profile", suggested, PROFILE_FILTER
        )
        if not chosen:
            return None
        target = Path(chosen)
        try:
            # newline="" so the document's own endings are written through unchanged: a profile checked
            # out on Windows with CRLF must not come back mixed (CLAUDE.md § Windows differs).
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(self.document.text)
        except OSError as error:
            QMessageBox.warning(self, "Cannot save profile", f"{target}\n\n{error}")
            return None
        self.path = target
        self._refresh_heading()
        return target


class _GroupEditor:
    """One section of the form. Not a QWidget itself — `box` is the widget."""

    def __init__(self, group: Group, document: ProfileDocument) -> None:
        self.group = group
        self.rows: dict[str, _Row] = {}
        self.changed = _Signalish()
        self._active = document.has_section(group.section)
        # The document a toggle reads from when it needs to reveal a commented-out block. Kept current
        # by `reload`, and set here so switching a section on before any Apply works too.
        self._scratch = document

        self.box = QGroupBox(group.title)
        if group.toggle:
            # A section switched on and off as a unit, because its keys are mandatory together.
            self.box.setCheckable(True)
            self.box.setChecked(self._active)
            self.box.toggled.connect(self._on_toggled)
        grid = QGridLayout(self.box)
        grid.setColumnStretch(2, 1)

        row_index = 0
        if group.help:
            note = QLabel(group.help)
            note.setWordWrap(True)
            note.setStyleSheet(_HELP_STYLE)
            grid.addWidget(note, row_index, 0, 1, 4)
            row_index += 1

        units = document.declared_units
        for field in group.fields:
            row = _Row(field, document, units)
            row.changed = self._emit_changed
            row.add_to(grid, row_index)
            self.rows[field.key] = row
            row_index += 1 + (1 if field.help else 0)

        if group.toggle:
            # A section that starts switched off starts with its rows greyed out, not merely unchecked.
            for row in self.rows.values():
                row.set_enabled(self._active)

    # `changed` is a tiny callable-list rather than a Qt signal because `_GroupEditor` is deliberately
    # not a QObject: it owns widgets but is not one, which keeps the form's logic out of the widget
    # tree and testable without a parent.
    def _emit_changed(self) -> None:
        self.changed.emit()

    def _on_toggled(self, checked: bool) -> None:
        """Switching a toggled section on reveals what its commented-out block already said.

        The shipped profile carries `[stock]` commented out *with working example values*, and those
        read as absent — so ticking the box would otherwise present two empty corner rows and a
        "required" error. Applying the activation to a scratch document brings the numbers back, which
        is the comment-preserving design paying off rather than a special case.
        """
        for row in self.rows.values():
            row.set_enabled(checked)
        if not checked:
            return
        try:
            revealed = self._scratch.apply([Edit(self.group.section, None, True)])
        except ProfileError:  # pragma: no cover - reactivating cannot produce invalid TOML
            return
        for key, row in self.rows.items():
            # Every key the revealed block states, not only the empty rows: the block may set a
            # discriminator such as `shape` whose widget already holds a default, and leaving that at
            # "box" while filling a cylinder's dimensions describes neither solid.
            stated = revealed.value(self.group.section, key)
            if stated is not None:
                row.show_value(stated)
        self._emit_changed()

    def reload(self, document: ProfileDocument) -> None:
        self._scratch = document
        self._active = document.has_section(self.group.section)
        if self.group.toggle:
            self.box.blockSignals(True)
            self.box.setChecked(self._active)
            self.box.blockSignals(False)
        for row in self.rows.values():
            row.reload(document)
            if self.group.toggle:
                row.set_enabled(self._active)

    def update_gating(self, lookup) -> None:
        for row in self.rows.values():
            row.update_gating(lookup)

    def edits(self) -> list[Edit]:
        section = self.group.section
        if not self.group.toggle:
            return [edit for row in self.rows.values() if (edit := row.edit()) is not None]

        checked = self.box.isChecked()
        if not checked:
            return [Edit(section, None, UNSET)] if self._active else []
        if self._active:
            return [edit for row in self.rows.values() if (edit := row.edit()) is not None]
        # Switching the section on: activate the block, then state every key that applies explicitly,
        # rather than relying on whatever the commented-out text happened to say. Rows belonging to the
        # shape *not* chosen contribute nothing, or the section would carry both shapes' keys.
        forced = (row.forced_edit() for row in self.rows.values())
        return [Edit(section, None, True), *(edit for edit in forced if edit is not None)]


class _Signalish:
    """The smallest thing that can stand in for a Qt signal on a non-QObject."""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self) -> None:
        for slot in self._slots:
            slot()


class _Row:
    """One field: an optional "set" box, a label, one or more editors, and a unit suffix."""

    def __init__(self, field: Field, document: ProfileDocument, units: str) -> None:
        self.field = field
        self.changed = lambda: None
        self._gated = True
        self._section_enabled = True

        self.check: QCheckBox | None = QCheckBox() if field.optional else None
        self.label = QLabel(field.label)
        self.unit = QLabel(unit_label(field.kind, units))
        self.unit.setStyleSheet(_HELP_STYLE)
        self.help = QLabel(field.help) if field.help else None
        if self.help is not None:
            self.help.setWordWrap(True)
            self.help.setStyleSheet(_HELP_STYLE)

        self.editors: list[QWidget] = list(_build_editors(field))
        self.holder = QWidget()
        holder_layout = QHBoxLayout(self.holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        parts = _VECTOR_PARTS.get(field.kind, ())
        for index, editor in enumerate(self.editors):
            if parts:
                caption = QLabel(parts[index])
                caption.setStyleSheet(_HELP_STYLE)
                holder_layout.addWidget(caption)
            holder_layout.addWidget(editor)
        if field.kind is Kind.OFFSET:
            # The fourth component is degrees whatever the file's units are, so the row carries two
            # suffixes rather than one that would be wrong for a quarter of it.
            degrees = QLabel("(A in deg)")
            degrees.setStyleSheet(_HELP_STYLE)
            holder_layout.addWidget(degrees)

        self.reload(document)
        for editor in self.editors:
            _on_edit(editor, self._touched)
        if self.check is not None:
            self.check.toggled.connect(self._on_check)

    # ------------------------------------------------------------------ layout

    def add_to(self, grid: QGridLayout, row: int) -> None:
        if self.check is not None:
            grid.addWidget(self.check, row, 0)
        grid.addWidget(self.label, row, 1)
        grid.addWidget(self.holder, row, 2)
        grid.addWidget(self.unit, row, 3)
        if self.help is not None:
            grid.addWidget(self.help, row + 1, 1, 1, 3)

    # ------------------------------------------------------------------ value

    def reload(self, document: ProfileDocument) -> None:
        self._original = document.value(self.field.section, self.field.key)
        self.show_value(self._original)

    def show_value(self, value) -> None:
        if self.check is not None:
            self.check.blockSignals(True)
            self.check.setChecked(value is not None)
            self.check.blockSignals(False)
        shown = value if value is not None else self.field.default
        _write(self.editors, self.field, shown)
        self._apply_enabled()

    def is_set(self) -> bool:
        return self.check is None or self.check.isChecked()

    def current_or_none(self):
        """The value as the widgets hold it, or None when unset or unparseable. Never raises.

        Used for `requires` gating, where a half-typed number must not throw while the user types.
        """
        if not self.is_set():
            return None
        try:
            return _read(self.editors, self.field)
        except FieldError:
            return None

    def edit(self) -> Edit | None:
        """This row's `Edit`, or None when nothing changed. Raises `FieldError` on a bad value."""
        if not self._gated:
            # A field the loader would refuse under the current settings is **removed**, not left
            # alone. Leaving it is how switching Stock from box to cylinder produced a section with
            # both shapes' keys, and switching the dialect to LinuxCNC left an `arc_centre` behind
            # that the loader refuses outright — in both cases an Apply that fails for a reason the
            # user did not touch.
            return self._unset_if_present()
        if not self.is_set():
            return self._unset_if_present()
        value = _read(self.editors, self.field)
        if self._original is not None and _same(value, self._original):
            return None
        return Edit(self.field.section, self.field.key, value)

    def forced_edit(self) -> Edit | None:
        """This row's `Edit`, stated even when unchanged. None when the row does not apply at all."""
        if not self._gated:
            return self._unset_if_present()
        if not self.is_set():
            return self._unset_if_present()
        return Edit(self.field.section, self.field.key, _read(self.editors, self.field))

    def _unset_if_present(self) -> Edit | None:
        if self._original is None:
            return None
        return Edit(self.field.section, self.field.key, UNSET)

    # ------------------------------------------------------------------ enabling

    def _touched(self, *_args) -> None:
        self.changed()

    def _on_check(self, _checked: bool) -> None:
        self._apply_enabled()
        self.changed()

    def set_enabled(self, enabled: bool) -> None:
        self._section_enabled = enabled
        self._apply_enabled()

    def update_gating(self, lookup) -> None:
        """Grey out a field whose `requires` is not met, so it cannot be set and then refused."""
        requires = self.field.requires
        self._gated = True
        if requires is not None:
            section, key, allowed = requires
            self._gated = str(lookup(section, key)) in allowed
        self._apply_enabled()

    def _apply_enabled(self) -> None:
        available = self._gated and self._section_enabled
        if self.check is not None:
            self.check.setEnabled(available)
        self.label.setEnabled(available)
        self.unit.setEnabled(available)
        editable = available and self.is_set()
        for editor in self.editors:
            editor.setEnabled(editable)


# --------------------------------------------------------------------------- widget plumbing


def _build_editors(field: Field) -> list[QWidget]:
    if field.kind is Kind.BOOL:
        return [QCheckBox()]
    if field.kind is Kind.CHOICE:
        combo = QComboBox()
        combo.addItems(field.choices)
        return [combo]
    count = len(_VECTOR_PARTS.get(field.kind, ("",)))
    editors: list[QWidget] = []
    for _ in range(count):
        line = QLineEdit()
        if field.kind in _NUMERIC:
            line.setMaximumWidth(110)
            line.setAlignment(Qt.AlignRight)
        editors.append(line)
    return editors


def _on_edit(editor: QWidget, slot) -> None:
    if isinstance(editor, QCheckBox):
        editor.toggled.connect(slot)
    elif isinstance(editor, QComboBox):
        editor.currentTextChanged.connect(slot)
    else:
        editor.textEdited.connect(slot)


def _write(editors: list[QWidget], field: Field, value) -> None:
    if field.kind is Kind.BOOL:
        editors[0].setChecked(bool(value))
        return
    if field.kind is Kind.CHOICE:
        text = str(value) if value is not None else (field.choices[0] if field.choices else "")
        index = editors[0].findText(text)
        editors[0].setCurrentIndex(max(0, index))
        return
    if field.kind is Kind.TEXT:
        editors[0].setText("" if value is None else str(value))
        return
    parts = list(value) if isinstance(value, (list, tuple)) else [value]
    for index, editor in enumerate(editors):
        component = parts[index] if index < len(parts) else None
        editor.setText("" if component is None else render(float(component)))


def _read(editors: list[QWidget], field: Field):
    if field.kind is Kind.BOOL:
        return editors[0].isChecked()
    if field.kind is Kind.CHOICE:
        return editors[0].currentText()
    if field.kind is Kind.TEXT:
        return editors[0].text()
    values = []
    for index, editor in enumerate(editors):
        text = editor.text().strip()
        if not text:
            raise FieldError(
                f"{field.label}: enter a number, or clear the box beside it to unset it"
            )
        try:
            values.append(float(text))
        except ValueError as error:
            where = f" ({_VECTOR_PARTS[field.kind][index]})" if field.kind in _VECTOR_PARTS else ""
            raise FieldError(f"{field.label}{where}: {text!r} is not a number") from error
    return values[0] if len(values) == 1 else values


def _same(new, original) -> bool:
    """Whether an edit is a no-op, comparing numbers numerically and lists element-wise."""
    if isinstance(new, list) and isinstance(original, (list, tuple)):
        return len(new) == len(original) and all(
            _same(a, b) for a, b in zip(new, original, strict=True)
        )
    if isinstance(new, bool) or isinstance(original, bool):
        return bool(new) is bool(original)
    if isinstance(new, (int, float)) and isinstance(original, (int, float)):
        return float(new) == float(original)
    return new == original
