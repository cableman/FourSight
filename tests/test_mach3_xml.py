"""Reading a Mach3 profile XML into profile edits (T17.1-T17.3, T17.5).

The fixtures under `tests/fixtures/mach3/` are trimmed from two real profiles for one machine — the
tags the importer reads, a sample of the thousands it does not, and `rotary.xml`'s raw bytes inside
`<LastUser>`, which are what make the real file invalid XML.

`profiles/rotary.toml` is **not** an oracle here, and these tests deliberately never compare against
it: it is a hand re-interpretation of this machine for a wrapped-rotary job (`rotary_axis = "y"`, no
`[axes.x]`, rates chosen by hand), so only `[dialect]` and the spindle ceiling would ever agree.
"""

from pathlib import Path

import pytest

from foursight.machine.mach3_xml import (
    ImportPlan,
    Row,
    native_units,
    plan_import,
    read,
    read_text,
)
from foursight.machine.profile import INCH_TO_MM, ProfileError, default_profile_path
from foursight.machine.profile_doc import ProfileDocument

FIXTURES = Path(__file__).parent / "fixtures" / "mach3"


@pytest.fixture
def shipped() -> ProfileDocument:
    return ProfileDocument.from_text(default_profile_path().read_text(encoding="utf-8"))


def mach3(name: str):
    return read(FIXTURES / f"{name}.xml")


def plan(document: ProfileDocument, name: str, units: str | None = None) -> ImportPlan:
    return plan_import(document, mach3(name), units)


def row(imported: ImportPlan, target: str) -> Row:
    return next(row for row in imported.rows if row.target == target)


def targets(imported: ImportPlan) -> set[str]:
    return {row.target for row in imported.rows}


def applied(document: ProfileDocument, imported: ImportPlan):
    return document.apply(imported.edits()).profile()


# --------------------------------------------------------------------------- the reader


def test_a_file_with_raw_bytes_in_a_tag_still_reads() -> None:
    """The real profile is not well-formed XML: `xml.etree` gives up on the whole file at `<LastUser>`."""
    raw = (FIXTURES / "rotary.xml").read_bytes()
    assert b"\x18" in raw, "the fixture must keep the control byte that breaks an XML parser"

    profile = mach3("rotary")

    assert profile.name == "Rotary"
    assert profile.number("Vel0") == 85.0


def test_a_truncated_file_is_refused_and_the_message_names_it() -> None:
    with pytest.raises(ProfileError) as error:
        mach3("truncated")

    assert "truncated.xml" in str(error.value)


def test_a_repeated_tag_takes_its_last_value() -> None:
    """Mach3 rewrites the file rather than editing it, so the later value is the current one."""
    profile = read_text(
        "<Preferences><Profile>a</Profile><Vel0>1.</Vel0><Vel0>2.</Vel0></Preferences>"
    )

    assert profile.number("Vel0") == 2.0


def test_tags_outside_the_preferences_block_are_not_read() -> None:
    """Other sections repeat tag names; reading them all would let a window position beat a setting."""
    profile = read_text(
        "<Preferences><Profile>a</Profile><Vel0>1.</Vel0></Preferences><Control><Vel0>9.</Vel0></Control>"
    )

    assert profile.number("Vel0") == 1.0


def test_mach3_numbers_in_every_shape_it_writes() -> None:
    profile = mach3("rotary")

    assert profile.number("Vel0") == 85.0  # '85.'
    assert profile.number("Steps3") == pytest.approx(88.8889)
    assert profile.flag("IJMode") is True
    assert profile.flag("SoftLimit") is False
    assert profile.number("NoSuchTag") is None


# --------------------------------------------------------------------------- what gets imported


def test_the_settings_only_the_controller_knows(shipped) -> None:
    """The case for the milestone: three keys no G-code program can state."""
    profile = applied(shipped, plan(shipped, "rotary"))

    assert profile.dialect.name == "mach3"
    assert profile.dialect.arc_centre == "incremental"  # <IJMode>1
    assert profile.dialect.dwell_units == "milliseconds"  # <DwellinMilli>1


def test_rapid_rates_are_per_second_in_the_file_and_per_minute_in_the_profile(shipped) -> None:
    profile = applied(shipped, plan(shipped, "rotary"))

    assert profile.axes["X"].max_rapid == pytest.approx(5100.0)  # <Vel0>85.
    assert profile.axes["Y"].max_rapid == pytest.approx(4999.8)  # <Vel1>83.33
    assert profile.axes["Z"].max_rapid == pytest.approx(4000.2)  # <Vel2>66.67
    assert profile.axes["A"].max_rapid == pytest.approx(3000.0)  # <Vel3>50., degrees


def test_the_spindle_ceiling_is_the_highest_pulley_not_the_selected_one(shipped) -> None:
    profile = applied(shipped, plan(shipped, "rotary"))

    assert profile.limits.max_spindle_rpm == 25000.0


def test_the_import_leaves_every_section_it_does_not_map_byte_identical(shipped) -> None:
    updated = shipped.apply(plan(shipped, "rotary").edits())

    before = _sections(shipped.text)
    after = _sections(updated.text)
    changed = {name for name in before if before[name] != after.get(name)}
    assert changed == {"machine", "dialect", "limits", "axes.x", "axes.y", "axes.z", "axes.a"}
    assert before.keys() == after.keys()


def test_the_comments_survive_the_import(shipped) -> None:
    """M8's whole point: the shipped profile documents its own format in comments."""
    updated = shipped.apply(plan(shipped, "rotary").edits())

    assert "# units the values in THIS file are expressed in" in updated.text
    assert (
        updated.text.count("#") == shipped.text.count("#") - 2
    )  # arc_centre and dwell_units switch on


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections.setdefault(current, [])
        sections[current].append(line)
    return {name: "\n".join(body) for name, body in sections.items()}


# --------------------------------------------------------------------------- what does not


def test_soft_limits_are_offered_unticked_when_mach3_is_not_enforcing_them(shipped) -> None:
    """`<SoftLimit>0`: numbers nothing has had to be true for years. This machine's two profiles
    disagree about X travel by 750 mm, which is what a stale envelope looks like."""
    imported = plan(shipped, "rotary")

    assert row(imported, "[axes.x].min").ticked is False
    assert row(imported, "[axes.x].max").ticked is False
    assert "<SoftLimit>0" in row(imported, "[axes.x].min").caveat

    profile = applied(shipped, imported)
    assert profile.axes["X"].max == 400.0  # the document's own value, untouched


def test_soft_limits_are_ticked_when_mach3_enforces_them(shipped) -> None:
    imported = plan(shipped, "inch")

    assert row(imported, "[axes.x].min").ticked is True
    assert row(imported, "[axes.a].min").ticked is True  # <ROTSOFT>1


def test_rotary_soft_limits_need_rotsoft_of_their_own(shipped) -> None:
    """`<SoftLimit>` alone does not make the rotary bounds real; Mach3 gates them separately."""
    text = (FIXTURES / "inch.xml").read_text().replace("<ROTSOFT>1<", "<ROTSOFT>0<")
    imported = plan_import(shipped, read_text(text))

    assert row(imported, "[axes.x].min").ticked is True
    assert row(imported, "[axes.a].min").ticked is False


def test_an_inactive_motor_is_a_note_and_the_section_is_left_alone(shipped) -> None:
    """Removing `[axes.a]` would change every rotary check from enforced to unknown."""
    imported = plan(shipped, "mill3axis")

    assert not any(target.startswith("[axes.a]") for target in targets(imported))
    assert any("<Motor3Active>0" in note.reason for note in imported.notes)

    profile = applied(shipped, imported)
    assert profile.axes["A"].max_rapid == 3600.0  # the shipped default, untouched


def test_a_linear_a_axis_is_refused_rather_than_read_as_degrees(shipped) -> None:
    text = (
        (FIXTURES / "rotary.xml")
        .read_bytes()
        .decode("cp1252")
        .replace("<AAngular>1<", "<AAngular>0<")
    )
    imported = plan_import(shipped, read_text(text))

    assert not any(target.startswith("[axes.a]") for target in targets(imported))
    assert any("<AAngular>0" in note.reason for note in imported.notes)


def test_an_axis_driven_by_another_motor_is_refused_rather_than_guessed(shipped) -> None:
    text = (FIXTURES / "inch.xml").read_text().replace("<AxisToMotor1>1<", "<AxisToMotor1>4<")
    imported = plan_import(shipped, read_text(text))

    assert not any(target.startswith("[axes.y]") for target in targets(imported))
    assert any("<AxisToMotor1>4" in note.reason for note in imported.notes)


def test_the_derived_feed_ceiling_is_offered_and_not_imported(shipped) -> None:
    """Mach3 has no maximum-feed setting, so the candidate is a derivation and stays unticked."""
    imported = plan(shipped, "rotary")

    assert row(imported, "[limits].max_feed").ticked is False
    assert applied(shipped, imported).limits.max_feed == 3000.0  # the document's own value


def test_the_sections_the_file_cannot_describe_come_back_as_notes(shipped) -> None:
    imported = plan(shipped, "rotary")

    subjects = {note.subject for note in imported.notes}
    assert {
        "[kinematics].rotary_mount",
        "[kinematics].rotary_axis",
        "[kinematics].centerline_offset",
        "[kinematics].pivot_to_tip",
        "[stock]",
        "[tool]",
        "[offsets]",
        "[safety].min_clearance_z",
        "[tolerance]",
    } <= subjects
    assert not any(
        target.startswith(
            ("[kinematics]", "[stock]", "[tool]", "[offsets]", "[safety]", "[tolerance]")
        )
        for target in targets(imported)
    )


def test_the_machine_units_are_never_written(shipped) -> None:
    """Writing `units = "inch"` would reinterpret every key the import did not touch, 25.4x, silently."""
    imported = plan(shipped, "inch")

    assert "[machine].units" not in targets(imported)
    assert 'units = "mm"' in shipped.apply(imported.edits()).text


# --------------------------------------------------------------------------- units


def test_an_inch_profile_is_converted_into_the_documents_units(shipped) -> None:
    profile = applied(shipped, plan(shipped, "inch"))

    assert native_units(mach3("inch")) == "inch"
    assert profile.axes["X"].max_rapid == pytest.approx(6.0 * 60 * INCH_TO_MM)
    assert profile.axes["X"].max == pytest.approx(48.0 * INCH_TO_MM)
    # Rotary is degrees end to end under either setting: 25.4 x an angle is not an angle.
    assert profile.axes["A"].max_rapid == pytest.approx(3000.0)
    assert profile.axes["A"].max == pytest.approx(360.0)


def test_an_inch_document_takes_the_metric_profile_the_other_way(shipped) -> None:
    inch_document = ProfileDocument.from_text(
        shipped.text.replace('units = "mm"', 'units = "inch"')
    )

    imported = plan_import(inch_document, mach3("rotary"))
    written = inch_document.apply(imported.edits())

    assert row(imported, "[axes.x].max_rapid").shown == "200.7874016 inch/min"
    # The profile reports mm whatever the file says, so the round trip lands back on 5100.
    assert written.profile().axes["X"].max_rapid == pytest.approx(5100.0)


def test_a_units_override_is_obeyed(shipped) -> None:
    imported = plan(shipped, "rotary", units="inch")

    assert imported.units == "inch"
    assert row(imported, "[axes.x].max_rapid").shown.startswith("129540")


def test_a_misset_units_flag_is_flagged_rather_than_scaled(shipped) -> None:
    """Tuning steps-per-unit in inches without switching native units is the commonest Mach3 error."""
    assert plan(shipped, "misset_units").warnings
    assert not plan(shipped, "inch").warnings
    assert not plan(shipped, "rotary").warnings


def test_reading_a_metric_machine_as_inch_is_caught(shipped) -> None:
    warnings = plan(shipped, "rotary", units="inch").warnings

    assert any("rapid" in warning for warning in warnings)


# --------------------------------------------------------------------------- short rotation


def test_short_rotate_is_imported_where_the_axis_wraps(shipped) -> None:
    imported = plan(shipped, "shortrot")

    assert row(imported, "[axes.a].short_rotate").edit.value is True
    assert applied(shipped, imported).axes["A"].short_rotate is True


def test_short_rotate_against_a_non_wrapping_axis_is_a_conflict_that_writes_nothing(
    shipped,
) -> None:
    """Mach3 has short-rotate and rollover as independent checkboxes; a profile cannot say that.

    The loader is right to refuse the pair (T14.1), so the importer shows the conflict instead of
    manufacturing the `wrap = true` that would make it loadable.
    """
    document = ProfileDocument.from_text(shipped.text.replace("wrap = true", "wrap = false"))
    imported = plan_import(document, mach3("shortrot"))

    conflict = row(imported, "[axes.a].short_rotate")
    assert conflict.writable is False
    assert conflict.ticked is False
    assert any("wrap" in note.reason for note in imported.notes)
    assert document.apply(imported.edits()).profile().axes["A"].short_rotate is False


def test_no_import_can_produce_an_edit_set_the_loader_refuses(shipped) -> None:
    """Every fixture, against both a wrapping and a non-wrapping target."""
    non_wrapping = ProfileDocument.from_text(shipped.text.replace("wrap = true", "wrap = false"))
    for name in ("rotary", "mill3axis", "shortrot", "inch", "misset_units"):
        for document in (shipped, non_wrapping):
            imported = plan_import(document, mach3(name))
            document.apply(imported.edits()).profile()  # raises ProfileError if it disagrees
            document.apply(imported.edits(imported.rows)).profile()  # every row, ticked or not


def test_rot360_is_reported_and_never_mapped_onto_wrap(shipped) -> None:
    """Mach3's rollover is a DRO setting; `wrap` claims the axis turns continuously."""
    imported = plan(shipped, "rotary")

    assert "[axes.a].wrap" not in targets(imported)
    note = next(note for note in imported.notes if note.subject == "[axes.a].wrap")
    assert "<Rot360>0" in note.reason


def test_the_rotary_radius_is_reported_and_never_mapped_onto_stock(shipped) -> None:
    """`<RadiusA>` is a feed-compensation radius, and a plausible wrong stock is worse than none."""
    note = next(
        note for note in plan(shipped, "rotary").notes if note.subject == "[stock].diameter"
    )

    assert "<RadiusA>63." in note.reason
