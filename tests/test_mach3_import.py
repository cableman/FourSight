"""The Mach3 import, through the form (T17.4). Headless via Qt's ``offscreen`` platform.

The mapping is tested without Qt in `test_mach3_xml.py`. These are about the three things the *dialog*
is responsible for: that an import arrives as pending edits rather than as an applied profile, that
Apply then produces what the mapping promised, and that a row nobody ticked writes nothing.
"""

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import DEFAULT_PROFILE_PATH  # noqa: E402
from foursight.machine.mach3_xml import plan_import, read  # noqa: E402
from foursight.machine.profile_doc import ProfileDocument  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "mach3"


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


@pytest.fixture
def review(qt_app, shipped):
    from foursight.gui.mach3_import import Mach3ImportDialog

    return Mach3ImportDialog(read(FIXTURES / "rotary.xml"), shipped)


def row(dialog, section: str, key: str):
    group = next(g for g in dialog._groups if g.group.section == section)
    return group.rows[key]


def table_row(review, target: str) -> int:
    return next(
        index
        for index in range(review.table.rowCount())
        if review.table.item(index, 0).text() == target
    )


# --------------------------------------------------------------------------- the review table


def test_the_table_shows_every_mapped_row_with_its_source(review) -> None:
    index = table_row(review, "[axes.x].max_rapid")

    assert review.table.item(index, 1).text().startswith("<Vel0>85.")
    assert review.table.item(index, 2).text() == "5100.0 mm/min"
    assert review.table.item(index, 0).checkState() == Qt.Checked


def test_a_row_mach3_is_not_enforcing_starts_unticked(review) -> None:
    index = table_row(review, "[axes.x].min")

    assert review.table.item(index, 0).checkState() == Qt.Unchecked
    assert "[axes.x].min" not in {edit.section + "." + edit.key for edit in review.edits()}


def test_unticking_a_row_removes_its_edit(review) -> None:
    index = table_row(review, "[dialect].dwell_units")
    review.table.item(index, 0).setCheckState(Qt.Unchecked)

    assert not any(edit.key == "dwell_units" for edit in review.edits())


def test_switching_the_units_redraws_the_values(review) -> None:
    review.units.setCurrentText("inch")

    index = table_row(review, "[axes.x].max_rapid")
    assert review.table.item(index, 2).text().startswith("129540")
    assert review.plan.warnings  # 129 m/min is not a machine


def test_the_notes_name_what_was_not_imported(review) -> None:
    text = review.notes.toPlainText()

    assert "[kinematics].rotary_axis" in text
    assert "[stock]" in text


def test_the_notes_keep_the_tag_names_they_are_about(review) -> None:
    """Found by looking, invisible to the plan: `QTextBrowser` reads `<Rot360>` as an unknown HTML
    element and renders the sentence with its subject missing. Every note quotes a tag, so this is the
    whole list going quietly wrong, not one string."""
    text = review.notes.toPlainText()

    assert "<Rot360>0" in text
    assert "<AParallel>" in text
    assert "<RadiusA>63." in text


def test_no_note_leaks_markdown_at_the_user(review) -> None:
    """The same look found `toolpath-*display*` rendered with its asterisks. Stripping emphasis by hand
    across a dozen strings is exactly the job that gets under-done with nothing to flag it.

    An asterisk against a word, rather than a matched pair: that catches a half-stripped `*display`
    too, and still lets `[axes.*]` through, which is a glob the notes legitimately write.
    """
    emphasis = re.compile(r"\*\w")
    caveats = [review.table.item(index, 3).text() for index in range(review.table.rowCount())]

    assert not emphasis.search(review.notes.toPlainText())
    assert not [text for text in caveats if emphasis.search(text)]


# --------------------------------------------------------------------------- into the form


def test_an_import_lands_as_pending_edits_and_applies_as_one(dialog, review) -> None:
    dialog.propose(review.edits())

    assert dialog.apply_button.isEnabled()
    assert dialog.document.value("dialect", "name") == "linuxcnc"  # nothing applied yet

    assert dialog.apply() is True
    profile = dialog.document.profile()
    assert profile.dialect.name == "mach3"
    assert profile.dialect.dwell_units == "milliseconds"
    assert profile.dialect.arc_centre == "incremental"
    assert profile.axes["X"].max_rapid == pytest.approx(5100.0)
    assert profile.limits.max_spindle_rpm == 25000.0
    assert profile.name == "Rotary"


def test_a_gated_field_still_arrives_because_its_gate_arrives_with_it(dialog, review) -> None:
    """`arc_centre` is refused under LinuxCNC, and the row that makes it legal is in the same import."""
    assert row(dialog, "dialect", "arc_centre")._gated is False

    dialog.propose(review.edits())

    assert row(dialog, "dialect", "arc_centre")._gated is True
    assert any(edit.key == "arc_centre" for edit in dialog.edits())


def test_an_import_changes_only_the_rows_it_mapped(dialog, review) -> None:
    before = dialog.document.text
    dialog.propose(review.edits())
    dialog.apply()

    pairs = zip(before.splitlines(), dialog.document.text.splitlines(), strict=True)
    changed = {new.lstrip("# ").split("=")[0].strip() for was, new in pairs if was != new}
    assert changed == {"name", "arc_centre", "dwell_units", "max_spindle_rpm", "max_rapid"}


def test_revert_discards_an_unapplied_import(dialog, review) -> None:
    before = dialog.document.text
    dialog.propose(review.edits())

    dialog.revert()

    assert not dialog.edits()
    assert dialog.document.text == before
    assert row(dialog, "dialect", "name").current_or_none() == "linuxcnc"


def test_an_import_nobody_ticked_changes_nothing(dialog, review) -> None:
    for index in range(review.table.rowCount()):
        review.table.item(index, 0).setCheckState(Qt.Unchecked)

    assert dialog.propose(review.edits()) is False
    assert not dialog.edits()


def test_every_mapped_target_has_a_field_in_the_form(dialog, shipped) -> None:
    """A target with no row would be silently dropped, leaving a profile that looks imported."""
    for name in ("rotary", "mill3axis", "shortrot", "inch", "misset_units"):
        plan = plan_import(shipped, read(FIXTURES / f"{name}.xml"))
        for candidate in plan.rows:
            if candidate.writable:
                assert dialog._row(candidate.edit.section, candidate.edit.key) is not None, (
                    candidate.target
                )
