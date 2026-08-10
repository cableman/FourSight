"""Profile-document tests (T8.1): surgical TOML editing, and the schema that drives the form.

Three properties carry the whole design, and each has a test that fails loudly if it stops holding:

- **Comments survive an edit.** The shipped profile's inline comments are its documentation, and a
  form that regenerated TOML would delete them one save at a time.
- **Unset is not zero.** Switching a field off must write *nothing*, not `0`, because absence is what
  disables a check.
- **The as-written value is what a form sees.** `MachineProfile` has already converted to mm, so
  reading values from it would show 2540 to someone who typed 100 in an inch profile.
"""

import pytest

from foursight.machine.profile import ProfileError, default_profile_path, load_profile
from foursight.machine.profile_doc import UNSET, Edit, ProfileDocument, render
from foursight.machine.profile_schema import GROUPS, Kind, fields, unit_label

SIMPLE = """# a machine
[machine]
name = "Test mill"
units = "mm"

[limits]
max_feed = 3000.0        # mm/min
max_spindle_rpm = 24000.0
# mm/min above which a plunge is reported. Unset because it depends on the tool.
# max_plunge_feed = 300.0

[safety]
min_clearance_z = 5.0 # rapids below this → warning
"""


def doc(text: str = SIMPLE) -> ProfileDocument:
    return ProfileDocument.from_text(text)


def line_of(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, f"{needle!r} matched {len(matches)} lines"
    return matches[0]


# --------------------------------------------------------------------------- reading


def test_values_are_read_as_written_not_as_converted() -> None:
    """The trap this module exists for: an inch profile's 100.0 must not read as 2540.0."""
    inch = doc('[machine]\nunits = "inch"\n[limits]\nmax_feed = 100.0\n')
    assert inch.value("limits", "max_feed") == 100.0
    assert inch.profile().limits.max_feed == pytest.approx(2540.0)
    assert inch.declared_units == "inch"


def test_a_commented_out_key_reads_as_absent() -> None:
    """What makes a form's "set" checkbox agree with the loader's "absence means unknown"."""
    assert doc().value("limits", "max_plunge_feed") is None
    assert doc().value("limits", "max_feed") == 3000.0


def test_a_nested_section_is_addressable() -> None:
    nested = doc('[machine]\nunits = "mm"\n[axes.x]\nmax = 400.0\n')
    assert nested.value("axes.x", "max") == 400.0
    assert nested.has_section("axes.x")
    assert not nested.has_section("axes.y")


def test_malformed_toml_is_refused_like_the_loader_refuses_it() -> None:
    with pytest.raises(ProfileError, match="invalid TOML"):
        doc("[machine\nunits = 'mm'\n")


# --------------------------------------------------------------------------- setting a value


def test_setting_a_value_keeps_its_trailing_comment() -> None:
    """`# mm/min` is the file telling the next reader what the number means."""
    edited = doc().apply([Edit("limits", "max_feed", 1500.0)])
    assert line_of(edited.text, "max_feed").strip() == "max_feed = 1500.0        # mm/min"


def test_setting_a_value_leaves_every_other_line_byte_identical() -> None:
    edited = doc().apply([Edit("safety", "min_clearance_z", 10.0)])
    before, after = SIMPLE.splitlines(), edited.text.splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1
    assert "min_clearance_z" in after[differing[0]]
    assert "rapids below this" in after[differing[0]], "the explanation survives"


def test_switching_on_a_commented_out_key_edits_it_in_place() -> None:
    """Not appended as a second copy: the commented line is where the explanation lives."""
    edited = doc().apply([Edit("limits", "max_plunge_feed", 250.0)])
    assert edited.value("limits", "max_plunge_feed") == 250.0
    assert edited.text.count("max_plunge_feed") == 1
    assert "# mm/min above which a plunge is reported." in edited.text


def test_a_key_the_section_lacks_is_inserted_into_it() -> None:
    edited = doc().apply([Edit("safety", "retract_before_toolchange", False)])
    assert edited.value("safety", "retract_before_toolchange") is False
    assert edited.profile().safety.retract_before_toolchange is False


def test_a_key_is_inserted_after_the_sections_last_content_line() -> None:
    """Not at the section's very end, or a blank separator line would be pushed down every time."""
    edited = doc().apply([Edit("limits", "rotary_wrap_warn", 720.0)])
    lines = edited.text.splitlines()
    index = next(i for i, line in enumerate(lines) if "rotary_wrap_warn" in line)
    assert "max_plunge_feed" in lines[index - 1]
    assert lines[index + 1] == "", "the blank line before [safety] stays put"


def test_a_section_the_file_never_had_is_appended() -> None:
    edited = doc().apply([Edit("stock", "min", (0.0, 0.0, -20.0))])
    assert "[stock]" in edited.text
    assert edited.value("stock", "min") == [0.0, 0.0, -20.0]
    assert edited.text.startswith("# a machine"), "nothing above is disturbed"


def test_an_edit_round_trips_through_the_loader() -> None:
    edited = doc().apply(
        [
            Edit("stock", "min", (0.0, 0.0, -20.0)),
            Edit("stock", "max", (100.0, 80.0, 0.0)),
        ]
    )
    stock = edited.profile().stock
    assert stock is not None
    assert stock.min == (0.0, 0.0, -20.0) and stock.max == (100.0, 80.0, 0.0)


# --------------------------------------------------------------------------- switching off


def test_unsetting_comments_the_line_out_rather_than_deleting_it() -> None:
    """The line is the only place the file explains the field. Deleting it loses that for good."""
    edited = doc().apply([Edit("limits", "max_feed", UNSET)])
    assert edited.value("limits", "max_feed") is None
    assert edited.profile().limits.max_feed is None
    assert "# max_feed = 3000.0        # mm/min" in edited.text


def test_unsetting_never_writes_zero() -> None:
    """PLAN.md's governing profile rule, as a test: `max_feed = 0` is not `max_feed` absent."""
    edited = doc().apply([Edit("limits", "max_feed", UNSET)])
    assert "max_feed = 0" not in edited.text


def test_unsetting_an_already_absent_key_changes_nothing() -> None:
    assert doc().apply([Edit("limits", "max_plunge_feed", UNSET)]).text == SIMPLE


def test_a_value_switched_off_and_on_again_comes_back() -> None:
    off = doc().apply([Edit("limits", "max_feed", UNSET)])
    on = off.apply([Edit("limits", "max_feed", 3000.0)])
    assert on.value("limits", "max_feed") == 3000.0
    assert "# mm/min" in line_of(on.text, "max_feed")


# --------------------------------------------------------------------------- whole-section toggle

WITH_STOCK = """[machine]
units = "mm"

[stock]
min = [0.0, 0.0, -20.0]
max = [100.0, 80.0, 0.0]
"""


def test_switching_a_section_off_takes_its_header_with_it() -> None:
    """Leaving `[stock]` behind with both keys commented out is the present-but-empty section the
    loader refuses — so a section switched off key-by-key would refuse to load at all."""
    edited = doc(WITH_STOCK).apply([Edit("stock", None, UNSET)])
    assert "# [stock]" in edited.text
    assert edited.profile().stock is None, "and it must still load"


def test_switching_a_section_back_on_reactivates_the_block_in_place() -> None:
    off = doc(WITH_STOCK).apply([Edit("stock", None, UNSET)])
    on = off.apply([Edit("stock", None, True)])
    assert on.profile().stock is not None
    assert on.text.count("[stock]") == 1, "not appended a second time"
    assert on.value("stock", "min") == [0.0, 0.0, -20.0], "the numbers came back"


def test_setting_a_key_inside_a_commented_out_section_reactivates_it() -> None:
    off = doc(WITH_STOCK).apply([Edit("stock", None, UNSET)])
    on = off.apply([Edit("stock", "max", (50.0, 50.0, 0.0))])
    stock = on.profile().stock
    assert stock is not None and stock.max == (50.0, 50.0, 0.0)


# --------------------------------------------------------------------------- line endings, quoting


def test_crlf_endings_survive_an_edit() -> None:
    """Windows checks these files out with CRLF; rewriting one line's ending would mix them."""
    edited = doc(SIMPLE.replace("\n", "\r\n")).apply([Edit("limits", "max_feed", 1.0)])
    assert "\r\n" in edited.text
    assert "\n" not in edited.text.replace("\r\n", "")


def test_a_hash_inside_a_quoted_name_is_not_a_comment() -> None:
    original = '[machine]\nname = "Mill #3"\nunits = "mm"\n'
    edited = doc(original).apply([Edit("machine", "name", "Mill #4")])
    assert edited.value("machine", "name") == "Mill #4"
    assert edited.profile().name == "Mill #4"


def test_a_quote_in_a_name_is_escaped() -> None:
    edited = doc().apply([Edit("machine", "name", 'the "big" mill')])
    assert edited.value("machine", "name") == 'the "big" mill'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "true"),
        (False, "false"),
        (3000.0, "3000.0"),
        (0.005, "0.005"),
        (0.0, "0.0"),
        (-20.5, "-20.5"),
        (360, "360.0"),
        ((1.0, 2.0, 3.0), "[1.0, 2.0, 3.0]"),
    ],
)
def test_rendering(value, expected: str) -> None:
    assert render(value) == expected


def test_a_tolerance_does_not_acquire_floating_point_noise() -> None:
    """`0.005` must not come back as `0.005000000000000001` after a round trip through the form."""
    edited = doc().apply([Edit("tolerance", "arc_radius_mismatch", 0.005)])
    assert "arc_radius_mismatch = 0.005" in edited.text


# --------------------------------------------------------------------------- the shipped profile


def test_the_shipped_profile_is_editable_without_loss() -> None:
    """The real target: every comment in the shipped profile survives an edit to one field."""
    text = default_profile_path().read_text(encoding="utf-8")
    edited = doc(text).apply([Edit("limits", "max_plunge_feed", 300.0)])
    assert edited.profile().limits.max_plunge_feed == 300.0
    comments_before = [line for line in text.splitlines() if line.strip().startswith("#")]
    comments_after = [line for line in edited.text.splitlines() if line.strip().startswith("#")]
    # One comment fewer: the commented-out `max_plunge_feed` example became the live setting.
    assert len(comments_after) == len(comments_before) - 1
    assert edited.profile().unknown_keys == ()


def test_switching_stock_on_in_the_shipped_profile_produces_a_loadable_profile() -> None:
    text = default_profile_path().read_text(encoding="utf-8")
    edited = doc(text).apply([Edit("stock", None, True)])
    stock = edited.profile().stock
    assert stock is not None, "the shipped profile's commented-out example is a working one"
    assert stock.min == (0.0, 0.0, -20.0) and stock.max == (100.0, 80.0, 0.0)


def test_the_shipped_profile_is_unchanged_by_a_no_op_edit_list() -> None:
    text = default_profile_path().read_text(encoding="utf-8")
    assert doc(text).apply([]).text == text


# --------------------------------------------------------------------------- the schema


def test_every_editable_field_names_a_key_the_loader_accepts() -> None:
    """A field the loader would report as unknown is a field that silently disables a check."""
    from foursight.machine.profile import _AXIS_KEYS, _KEYS

    for field in fields():
        if field.section.startswith("axes."):
            assert field.key in _AXIS_KEYS, f"{field.section}.{field.key}"
        elif field.section == "offsets":
            assert field.key.startswith("g")
        else:
            assert field.key in _KEYS[field.section], f"{field.section}.{field.key}"


def test_every_loader_key_is_editable() -> None:
    """The direction that actually rots: a key added to the loader and forgotten in the form is an
    uneditable setting, and a missing field looks exactly like a field that does not exist."""
    from foursight.machine.profile import _KEYS

    editable = {(f.section, f.key) for f in fields()}
    for section, keys in _KEYS.items():
        for key in keys:
            assert (section, key) in editable, f"[{section}].{key} has no form field"


def test_every_axis_key_is_editable_on_every_axis() -> None:
    from foursight.machine.profile import _AXIS_KEYS

    editable = {(f.section, f.key) for f in fields()}
    for axis in ("x", "y", "z", "a"):
        for key in _AXIS_KEYS:
            if key == "type":
                continue  # implied by the axis: the form has no reason to let A become linear
            if key == "wrap" and axis != "a":
                continue  # a linear axis does not wrap
            assert (f"axes.{axis}", key) in editable, f"axes.{axis}.{key}"


def test_group_sections_are_unique_and_ordered_as_shown() -> None:
    sections = [group.section for group in GROUPS]
    assert len(sections) == len(set(sections))


def test_required_fields_that_have_no_loader_default_are_only_in_toggled_groups() -> None:
    """A required field with no default must not be reachable in a section that can be absent, or the
    form could produce a section the loader refuses. `[stock]`'s two bounds are exactly that case,
    which is why that group is a toggle."""
    for group in GROUPS:
        for field in group.fields:
            if not field.optional and field.default is None:
                assert group.toggle, f"{field.section}.{field.key}"


@pytest.mark.parametrize(
    ("kind", "units", "expected"),
    [
        (Kind.LENGTH, "mm", "mm"),
        (Kind.LENGTH, "inch", "in"),
        (Kind.RATE, "mm", "mm/min"),
        (Kind.RATE, "inch", "in/min"),
        (Kind.ANGLE, "inch", "deg"),
        (Kind.ANGLE_RATE, "inch", "deg/min"),
        (Kind.COUNT, "inch", ""),
    ],
)
def test_unit_labels(kind: Kind, units: str, expected: str) -> None:
    assert unit_label(kind, units) == expected


def test_rotary_units_never_follow_the_files_units() -> None:
    """The mm/degrees split at the form boundary: an inch profile's A limits are still degrees."""
    for kind in (Kind.ANGLE, Kind.ANGLE_RATE):
        assert unit_label(kind, "inch") == unit_label(kind, "mm")


def test_the_rotary_axis_group_uses_angular_kinds() -> None:
    rotary = next(group for group in GROUPS if group.section == "axes.a")
    kinds = {f.key: f.kind for f in rotary.fields}
    assert kinds["min"] is Kind.ANGLE and kinds["max"] is Kind.ANGLE
    assert kinds["max_rapid"] is Kind.ANGLE_RATE
    linear = next(group for group in GROUPS if group.section == "axes.x")
    assert {f.kind for f in linear.fields} & {Kind.ANGLE, Kind.ANGLE_RATE} == set()


def test_the_shipped_profile_populates_the_schema() -> None:
    """Every field the form will show either has a value in the shipped profile or is legitimately
    unset there — nothing raises while reading it, which is what the dialog does on open."""
    document = doc(default_profile_path().read_text(encoding="utf-8"))
    profile = load_profile(default_profile_path())
    assert document.declared_units == profile.declared_units
    for field in fields():
        document.value(field.section, field.key)  # must not raise


# ------------------------------------------------------- CLI overrides, both implementations


OVERRIDE_BASE = """[machine]
units = "mm"

[dialect]
name = "%s"
"""


@pytest.mark.parametrize("start", ["linuxcnc", "mach3"])
@pytest.mark.parametrize("dialect", [None, "linuxcnc", "mach3"])
@pytest.mark.parametrize("arc_centre", [None, "incremental", "absolute"])
def test_the_document_override_agrees_with_the_profile_one(
    start: str, dialect: str | None, arc_centre: str | None
) -> None:
    """`apply_dialect_override` duplicates `with_dialect`/`with_arc_centre`'s precedence rules.

    The duplication is deliberate — the GUI needs the override as *text* so its profile editor cannot
    disagree with what is in force — and it is guarded the way `sim/timing.py`'s two implementations
    are: drive both over every combination and demand the same answer, including the same refusals.
    """
    from foursight.machine.profile import load_profile_text, with_arc_centre, with_dialect
    from foursight.machine.profile_doc import apply_dialect_override

    text = OVERRIDE_BASE % start
    profile_error: str | None = None
    try:
        expected = with_arc_centre(with_dialect(load_profile_text(text), dialect), arc_centre)
    except ProfileError as error:
        expected, profile_error = None, str(error)

    document_error: str | None = None
    try:
        actual = apply_dialect_override(doc(text), dialect, arc_centre).profile()
    except ProfileError as error:
        actual, document_error = None, str(error)

    assert (profile_error is None) == (document_error is None), (
        f"one path refused and the other did not: {profile_error!r} vs {document_error!r}"
    )
    if profile_error is not None:
        return
    assert actual.dialect == expected.dialect
    assert actual.parser_dialect == expected.parser_dialect


def test_an_override_to_a_different_dialect_clears_the_previous_controller_settings() -> None:
    """Settings tuned for one controller describe nothing about another."""
    from foursight.machine.profile_doc import apply_dialect_override

    mach3 = doc(
        '[machine]\nunits = "mm"\n[dialect]\nname = "mach3"\n'
        'arc_centre = "absolute"\ndwell_units = "milliseconds"\n'
    )
    switched = apply_dialect_override(mach3, "linuxcnc", None)
    # Under LinuxCNC these keys are refused outright, so clearing them is what makes the result load.
    assert switched.profile().dialect.name == "linuxcnc"
    assert switched.value("dialect", "arc_centre") is None


def test_an_override_to_the_same_dialect_keeps_its_settings() -> None:
    from foursight.machine.profile_doc import apply_dialect_override

    text = '[machine]\nunits = "mm"\n[dialect]\nname = "mach3"\narc_centre = "absolute"\n'
    unchanged = apply_dialect_override(doc(text), "mach3", None)
    assert unchanged.text == text
    assert unchanged.profile().dialect.arc_centre == "absolute"


def test_no_override_leaves_the_text_byte_identical() -> None:
    from foursight.machine.profile_doc import apply_dialect_override

    assert apply_dialect_override(doc(), None, None).text == SIMPLE


def test_an_unknown_override_dialect_is_refused() -> None:
    from foursight.machine.profile_doc import apply_dialect_override

    with pytest.raises(ProfileError, match="unknown dialect"):
        apply_dialect_override(doc(), "fanuc", None)


def test_the_profile_keeps_the_path_it_was_built_from(tmp_path) -> None:
    """A rebuilt profile that forgot its path would send the editor back to the shipped default."""
    target = tmp_path / "mill.toml"
    assert doc().profile(path=target).path == target
    assert doc().profile().path is None
