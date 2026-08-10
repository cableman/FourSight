"""Profile-editor tests (T8.2). Headless via Qt's ``offscreen`` platform.

The dialog is thin on purpose — `ProfileDocument` does the editing and `load_profile_text` the
validating — so these tests are about the four things the *form* is responsible for:

- an optional field cleared writes nothing, never zero;
- a field the loader would refuse under the current dialect cannot be set at all;
- an invalid value leaves the previously applied profile in force;
- Apply changes the profile in memory and nothing on disk.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH  # noqa: E402
from foursight.machine.profile import load_profile  # noqa: E402
from foursight.machine.profile_doc import ProfileDocument  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shipped() -> ProfileDocument:
    return ProfileDocument.from_text(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def dialog(qt_app, shipped):
    from foursight.gui.profile_dialog import ProfileDialog

    try:
        return ProfileDialog(shipped, DEFAULT_PROFILE_PATH)
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct the dialog on this platform: {error}")


def row(dialog, section: str, key: str):
    group = next(g for g in dialog._groups if g.group.section == section)
    return group.rows[key]


def group_of(dialog, section: str):
    return next(g for g in dialog._groups if g.group.section == section)


# --------------------------------------------------------------------------- opening


def test_the_form_shows_the_loaded_values(dialog) -> None:
    assert row(dialog, "limits", "max_feed").editors[0].text() == "3000.0"
    assert row(dialog, "safety", "min_clearance_z").editors[0].text() == "5.0"
    assert row(dialog, "kinematics", "rotary_mount").editors[0].currentText() == "table"
    assert row(dialog, "axes.a", "wrap").editors[0].isChecked() is True


def test_nothing_is_dirty_on_open(dialog) -> None:
    """Opening the editor must not itself constitute an edit, or Apply would always be live."""
    assert dialog.edits() == []
    assert dialog.apply_button.isEnabled() is False


def test_an_unset_field_shows_unchecked_with_its_example_value(dialog) -> None:
    """`max_plunge_feed` is commented out in the shipped profile: unset, but the example is shown."""
    plunge = row(dialog, "limits", "max_plunge_feed")
    assert plunge.is_set() is False
    assert plunge.editors[0].isEnabled() is False


def test_a_toggled_section_starts_off_when_the_file_has_it_commented_out(dialog) -> None:
    stock = group_of(dialog, "stock")
    assert stock.box.isChecked() is False
    assert stock.rows["min"].editors[0].isEnabled() is False


def test_the_heading_says_when_the_profile_is_the_packaged_one(dialog) -> None:
    """Apply works on it; Save would be writing into site-packages, so the dialog says so."""
    assert "packaged with FourSight" in dialog._heading.text()


def test_an_inch_profile_shows_as_written_values_and_inch_labels(qt_app) -> None:
    """The 25.4× trap, at the widget: a file writing 100.0 must not display 2540."""
    from foursight.gui.profile_dialog import ProfileDialog

    document = ProfileDocument.from_text('[machine]\nunits = "inch"\n[limits]\nmax_feed = 100.0\n')
    dialog = ProfileDialog(document)
    assert row(dialog, "limits", "max_feed").editors[0].text() == "100.0"
    assert row(dialog, "limits", "max_feed").unit.text() == "in/min"
    assert row(dialog, "axes.a", "max_rapid").unit.text() == "deg/min", "rotary stays degrees"


# --------------------------------------------------------------------------- editing


def test_changing_a_value_produces_one_edit(dialog) -> None:
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    edits = dialog.edits()
    assert len(edits) == 1
    assert (edits[0].section, edits[0].key, edits[0].value) == ("limits", "max_feed", 1500.0)


def test_retyping_the_same_value_is_not_an_edit(dialog) -> None:
    row(dialog, "limits", "max_feed").editors[0].setText("3000.0")
    assert dialog.edits() == []


def test_applying_updates_the_profile_and_emits_it(dialog) -> None:
    seen = []
    dialog.applied.connect(seen.append)
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    assert dialog.apply() is True
    assert len(seen) == 1
    assert seen[0].profile().limits.max_feed == 1500.0
    assert dialog.document.profile().limits.max_feed == 1500.0


def test_applying_leaves_the_file_on_disk_untouched(dialog) -> None:
    """The fix engine's contract, applied to the profile: nothing is written until Save as…."""
    before = DEFAULT_PROFILE_PATH.read_bytes()
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    assert dialog.apply() is True
    assert DEFAULT_PROFILE_PATH.read_bytes() == before


def test_applying_clears_the_dirty_state(dialog) -> None:
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    dialog.apply()
    assert dialog.edits() == []
    assert dialog.apply_button.isEnabled() is False


def test_switching_a_field_off_writes_nothing_rather_than_zero(dialog) -> None:
    """PLAN.md's governing profile rule, at the form boundary."""
    row(dialog, "limits", "max_feed").check.setChecked(False)
    assert dialog.apply() is True
    assert dialog.document.profile().limits.max_feed is None
    assert "max_feed = 0" not in dialog.document.text


def test_switching_a_field_on_uses_the_commented_out_example(dialog) -> None:
    plunge = row(dialog, "limits", "max_plunge_feed")
    plunge.check.setChecked(True)
    plunge.editors[0].setText("300.0")
    assert dialog.apply() is True
    assert dialog.document.profile().limits.max_plunge_feed == 300.0


def test_switching_a_section_on_reveals_the_values_its_comment_already_held(dialog) -> None:
    """Ticking Stock must not present two empty rows and a "required" error.

    The shipped profile's commented-out `[stock]` carries working example values, and reactivating the
    block is what brings them back — the comment-preserving design paying off.
    """
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    assert stock.rows["min"].editors[0].text() == "0.0"
    assert stock.rows["max"].editors[0].text() == "100.0"
    assert dialog.apply() is True
    box = dialog.document.profile().stock
    assert box is not None
    assert box.min == (0.0, 0.0, -20.0) and box.max == (100.0, 80.0, 0.0)


def test_switching_a_section_off_still_loads(dialog) -> None:
    """A section switched off key-by-key would leave the present-but-empty table the loader refuses."""
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    assert dialog.apply() is True
    stock.box.setChecked(False)
    assert dialog.apply() is True
    assert dialog.document.profile().stock is None


def test_reverting_discards_unapplied_edits(dialog) -> None:
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    dialog.revert()
    assert dialog.edits() == []
    assert row(dialog, "limits", "max_feed").editors[0].text() == "3000.0"


# --------------------------------------------------------------------------- refusing


def test_an_unparseable_number_is_reported_and_nothing_is_applied(dialog) -> None:
    row(dialog, "limits", "max_feed").editors[0].setText("fast")
    assert dialog.apply() is False
    # `isHidden`, not `isVisible`: the dialog itself is never shown in these tests, and a child of an
    # unshown parent reports `isVisible() is False` however many times it was shown itself.
    assert dialog._error.isHidden() is False
    assert "not a number" in dialog._error.text()
    assert dialog.document.profile().limits.max_feed == 3000.0, "the old value stands"


def test_a_profile_error_is_reported_against_the_edit_that_caused_it(dialog) -> None:
    """An axis whose minimum exceeds its maximum: valid TOML, refused by the loader."""
    row(dialog, "axes.x", "min").editors[0].setText("900.0")
    assert dialog.apply() is False
    assert "exceeds max" in dialog._error.text()
    assert dialog.document.profile().axes["X"].min == 0.0


def test_an_empty_required_field_says_which_one(dialog) -> None:
    row(dialog, "tolerance", "arc_chord").editors[0].setText("")
    assert dialog.apply() is False
    assert "Arc chord deviation" in dialog._error.text()


def test_a_stock_corner_names_the_component_that_is_wrong(dialog) -> None:
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    stock.rows["min"].editors[1].setText("oops")
    assert dialog.apply() is False
    assert "(Y)" in dialog._error.text()


# --------------------------------------------------------------------------- gated fields


def test_a_mach3_only_field_is_unreachable_under_linuxcnc(dialog) -> None:
    """Setting it would meet a `ProfileError` on Apply, so the form does not offer it."""
    arc_centre = row(dialog, "dialect", "arc_centre")
    assert arc_centre.check.isEnabled() is False
    assert arc_centre.editors[0].isEnabled() is False


def test_choosing_mach3_enables_its_settings_before_apply(dialog) -> None:
    """Read from the widgets, not the document, or the fields would stay grey until Apply."""
    row(dialog, "dialect", "name").editors[0].setCurrentText("mach3")
    assert row(dialog, "dialect", "arc_centre").check.isEnabled() is True


def test_a_gated_field_contributes_no_edit_even_if_it_holds_a_value(dialog) -> None:
    arc_centre = row(dialog, "dialect", "arc_centre")
    arc_centre.check.setChecked(True)
    arc_centre.editors[0].setCurrentText("absolute")
    assert dialog.edits() == [], "arc_centre is refused under linuxcnc, so it is not written"


def test_pivot_to_tip_is_offered_only_for_a_head_mount(dialog) -> None:
    pivot = row(dialog, "kinematics", "pivot_to_tip")
    assert pivot.check.isEnabled() is False
    row(dialog, "kinematics", "rotary_mount").editors[0].setCurrentText("head")
    dialog._on_field_changed()
    assert pivot.check.isEnabled() is True


def test_switching_to_a_head_mount_without_a_pivot_is_refused_with_its_reason(dialog) -> None:
    row(dialog, "kinematics", "rotary_mount").editors[0].setCurrentText("head")
    assert dialog.apply() is False
    assert "pivot_to_tip" in dialog._error.text()


# --------------------------------------------------------------------------- saving


def test_save_as_writes_the_document_and_adopts_the_path(dialog, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    target = tmp_path / "mill.toml"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    assert dialog.save_as() == target
    assert dialog.path == target
    saved = load_profile(target)
    assert saved.limits.max_feed == 1500.0
    assert "rapids below this" in target.read_text(encoding="utf-8"), "comments went with it"


def test_save_as_refuses_to_write_a_profile_that_would_not_load(
    dialog, tmp_path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    target = tmp_path / "broken.toml"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    row(dialog, "axes.x", "min").editors[0].setText("900.0")
    assert dialog.save_as() is None
    assert not target.exists(), "a file FourSight itself would refuse must not reach the disk"


def test_a_cancelled_save_writes_nothing(dialog, tmp_path, monkeypatch) -> None:
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    assert dialog.save_as() is None
    assert list(tmp_path.iterdir()) == []


def test_crlf_endings_survive_a_save(qt_app, tmp_path, monkeypatch) -> None:
    """Written with newline="" so a profile checked out on Windows does not come back mixed."""
    from PySide6.QtWidgets import QFileDialog

    from foursight.gui.profile_dialog import ProfileDialog

    source = DEFAULT_PROFILE_PATH.read_text(encoding="utf-8").replace("\n", "\r\n")
    dialog = ProfileDialog(ProfileDocument.from_text(source))
    target = tmp_path / "crlf.toml"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    row(dialog, "limits", "max_feed").editors[0].setText("1500.0")
    assert dialog.save_as() == target
    raw = target.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0


# --------------------------------------------------------------------------- every field is reachable


def test_every_schema_field_has_a_widget(dialog) -> None:
    """The form is generated, so this is really a test that generation covered the whole table."""
    from foursight.machine.profile_schema import fields

    for field in fields():
        assert row(dialog, field.section, field.key) is not None


def test_no_field_raises_while_being_read(dialog) -> None:
    """`current_or_none` runs on every keystroke through the gating pass; none of it may throw."""
    for group in dialog._groups:
        for candidate in group.rows.values():
            candidate.current_or_none()


def test_the_whole_shipped_profile_round_trips_through_the_form(dialog) -> None:
    """Open, apply nothing, and the document is byte-identical — the form invents no edits."""
    text_before = dialog.document.text
    dialog.revert()
    assert dialog.edits() == []
    assert dialog.document.text == text_before


def test_path_is_optional(qt_app, shipped) -> None:
    from foursight.gui.profile_dialog import ProfileDialog

    dialog = ProfileDialog(shipped, None)
    assert "no file" in dialog._heading.text()
    assert isinstance(dialog.path, (Path, type(None)))


# --------------------------------------------------------------------- cylindrical stock (M9)


def test_the_shape_selector_gates_the_two_sets_of_dimensions(dialog) -> None:
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    assert stock.rows["min"].check is None, "a box corner is required, not optional"
    assert stock.rows["diameter"].editors[0].isEnabled() is False, "cylinder keys are gated off"

    stock.rows["shape"].editors[0].setCurrentText("cylinder")
    dialog._on_field_changed()
    assert stock.rows["diameter"].editors[0].isEnabled() is True
    assert stock.rows["min"].editors[0].isEnabled() is False


def test_switching_to_a_cylinder_removes_the_box_keys(dialog) -> None:
    """The loader refuses a section carrying both shapes' keys, so the ones that no longer apply have
    to be *removed* rather than left behind — otherwise Apply fails for something the user never
    touched."""
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    assert dialog.apply() is True, dialog._error.text()

    stock.rows["shape"].editors[0].setCurrentText("cylinder")
    stock.rows["diameter"].editors[0].setText("50.0")
    stock.rows["length"].editors[0].setText("200.0")
    stock.rows["axis_min"].editors[0].setText("0.0")
    assert dialog.apply() is True, dialog._error.text()

    from foursight.machine.profile import StockCylinder

    stock_value = dialog.document.profile().stock
    assert isinstance(stock_value, StockCylinder)
    assert stock_value.diameter == 50.0 and stock_value.length == 200.0
    assert dialog.document.value("stock", "min") is None, "the box corners were removed"


def test_a_cylinder_can_be_configured_from_scratch(qt_app) -> None:
    from foursight.gui.profile_dialog import ProfileDialog
    from foursight.machine.profile import StockCylinder

    document = ProfileDocument.from_text('[machine]\nunits = "mm"\n')
    dialog = ProfileDialog(document)
    stock = group_of(dialog, "stock")
    stock.box.setChecked(True)
    stock.rows["shape"].editors[0].setCurrentText("cylinder")
    for key, value in (("diameter", "50.0"), ("length", "200.0"), ("axis_min", "-10.0")):
        stock.rows[key].editors[0].setText(value)
    assert dialog.apply() is True, dialog._error.text()
    built = dialog.document.profile().stock
    assert isinstance(built, StockCylinder)
    assert built.axis_min == -10.0 and built.axis_max == 190.0


def test_a_cylinder_dimension_shows_the_files_length_units(qt_app) -> None:
    from foursight.gui.profile_dialog import ProfileDialog

    document = ProfileDocument.from_text('[machine]\nunits = "inch"\n')
    dialog = ProfileDialog(document)
    assert row(dialog, "stock", "diameter").unit.text() == "in"


def test_switching_the_dialect_back_removes_the_setting_it_refuses(dialog) -> None:
    """The same removal rule, on the field that first needed it: a stale `arc_centre` left behind
    makes LinuxCNC refuse to load, for an edit the user never made."""
    row(dialog, "dialect", "name").editors[0].setCurrentText("mach3")
    dialog._on_field_changed()
    arc_centre = row(dialog, "dialect", "arc_centre")
    arc_centre.check.setChecked(True)
    arc_centre.editors[0].setCurrentText("absolute")
    assert dialog.apply() is True, dialog._error.text()
    assert dialog.document.profile().dialect.arc_centre == "absolute"

    row(dialog, "dialect", "name").editors[0].setCurrentText("linuxcnc")
    dialog._on_field_changed()
    assert dialog.apply() is True, dialog._error.text()
    assert dialog.document.value("dialect", "arc_centre") is None
