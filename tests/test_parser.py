"""Parse-layer tests.

T1.1 covers the dataclasses in `parser/model.py`. Tokenizer and resolver behaviour arrive with
T1.2 and T1.3 and belong in this file too.

These tests are deliberately structural — field sets, annotations, frozen-ness, slots. The data
model is what the memory and parse-rate targets rest on, and every one of those properties is easy
to break silently in a refactor: dropping `slots=True` still passes every behavioural test.
"""

import dataclasses
from typing import get_type_hints

import pytest

from foursight.parser.model import (
    AXIS_LETTERS,
    COORD_TRANSFORM_MODES,
    LINEAR_LENGTH_LETTERS,
    ROTARY_LETTERS,
    WORD_LETTERS,
    Command,
    ModalState,
    ParseErrorKind,
    SourceRef,
)
from foursight.parser.resolver import _GROUP_OF, parse

# PLAN.md § Core Data Model — Parse layer. Asserted verbatim so a field rename or a dropped field
# fails here rather than surfacing as a mystery three milestones later.
EXPECTED_FIELDS = {
    SourceRef: ("line_no", "start", "end"),
    ModalState: (
        "units",
        "plane",
        "distance",
        "arc_distance",
        "feed_mode",
        "offset",
        "feed",
        "spindle_rpm",
        "spindle_on",
        "tool",
        "length_offset",
        "cutter_comp",
        "rotation",
        "scaling",
        "polar",
    ),
    Command: ("ref", "gcodes", "mcodes", "motion", "words", "modal_snapshot"),
}


def _ref() -> SourceRef:
    return SourceRef(line_no=1, start=0, end=10)


def _command() -> Command:
    return Command(
        ref=_ref(),
        gcodes=["90", "21"],
        mcodes=["3", "8"],
        motion="1",
        words={"X": 1.0, "A": 90.0, "F": 200.0},
        modal_snapshot=ModalState(),
    )


@pytest.mark.parametrize("cls", list(EXPECTED_FIELDS))
def test_field_names_match_plan(cls: type) -> None:
    names = tuple(field.name for field in dataclasses.fields(cls))
    assert names == EXPECTED_FIELDS[cls]


def test_source_ref_stores_offsets_not_text() -> None:
    """Holding the line's text here would duplicate the whole file (PLAN.md § Parse layer)."""
    names = {field.name for field in dataclasses.fields(SourceRef)}
    assert not names & {"raw", "text", "source", "line"}


def test_gcodes_are_strings_never_floats() -> None:
    """`G90.1` as a float invites `words['G'] == 90.1` equality bugs (PLAN.md § Parse layer)."""
    hints = get_type_hints(Command)
    assert hints["gcodes"] == list[str]
    assert hints["mcodes"] == list[str]
    assert hints["motion"] == str | None
    assert hints["words"] == dict[str, float]


def test_modal_state_annotations_match_plan() -> None:
    hints = get_type_hints(ModalState)
    assert hints["units"] is str
    assert hints["plane"] is str
    assert hints["distance"] is str
    assert hints["arc_distance"] is str
    assert hints["feed_mode"] is str
    assert hints["offset"] == str | None
    assert hints["feed"] == float | None
    assert hints["spindle_rpm"] == float | None
    assert hints["spindle_on"] == str | None
    assert hints["tool"] == int | None
    assert hints["length_offset"] == int | None
    assert hints["cutter_comp"] == str | None
    assert hints["rotation"] == str | None
    assert hints["scaling"] == str | None
    assert hints["polar"] == str | None


@pytest.mark.parametrize(
    ("instance", "field_name"),
    [(_ref(), "line_no"), (_ref(), "end"), (ModalState(), "feed_mode"), (ModalState(), "feed")],
)
def test_shared_types_are_frozen(instance: object, field_name: str) -> None:
    """Assign to a field the class actually has.

    Assigning an *unknown* name to a `frozen=True, slots=True` dataclass raises a confusing
    `TypeError: super(type, obj)...` from CPython's generated `__setattr__` rather than
    AttributeError or FrozenInstanceError — so a typo'd attribute name here would pass for
    entirely the wrong reason.
    """
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, 99)


@pytest.mark.parametrize("instance", [_ref(), ModalState()])
def test_shared_types_are_hashable(instance: object) -> None:
    assert hash(instance) == hash(instance)
    assert {instance: "usable as a key"}[instance] == "usable as a key"


@pytest.mark.parametrize("cls", list(EXPECTED_FIELDS))
def test_slots_took_effect(cls: type) -> None:
    """`slots=True` on every hot dataclass; the 50k lines/sec target does not survive otherwise.

    Dropping the decorator argument breaks no behaviour, so nothing but this test would notice.
    """
    assert hasattr(cls, "__slots__")
    instance = _command() if cls is Command else cls() if cls is ModalState else _ref()
    assert not hasattr(instance, "__dict__")


def test_modal_state_defaults_are_the_documented_dialect_defaults() -> None:
    """G91.1 is the arc-centre default and G90.1 is honoured (PLAN.md § Dialect Divergences)."""
    state = ModalState()
    assert (state.units, state.plane, state.distance) == ("mm", "17", "90")
    assert (state.arc_distance, state.feed_mode) == ("91.1", "94")
    # Everything genuinely "not yet established" starts as None, so the verifier can tell the
    # difference between "never set" and "set to zero".
    assert state.offset is None
    assert state.feed is None
    assert state.spindle_on is None
    assert state.tool is None
    assert state.cutter_comp is None


def test_modal_state_is_copy_on_write() -> None:
    """The sharing strategy: emit a new instance only when something changes."""
    original = ModalState()
    changed = dataclasses.replace(original, feed=200.0)
    assert original.feed is None, "replace() must not mutate the shared original"
    assert changed.feed == 200.0
    assert changed is not original
    # Equal states are interchangeable, which is what makes sharing safe.
    assert ModalState() == original
    assert hash(ModalState()) == hash(original)


def test_command_is_mutable_and_unhashable() -> None:
    """Unlike the frozen pair, a Command is produced per block and never shared or keyed."""
    command = _command()
    command.motion = "0"
    assert command.motion == "0"
    with pytest.raises(TypeError):
        hash(command)


def test_word_letters_exclude_g_and_m() -> None:
    """A block carries several G- and M-words, so they cannot live in a letter-keyed dict."""
    assert not WORD_LETTERS & set("GM")
    assert set("XYZAIJKRFSTPHDLQ") == WORD_LETTERS


def test_rotary_is_never_treated_as_a_length() -> None:
    """Scaling A by 25.4 on a G20 program would silently corrupt every rotary move.

    This is the parse-layer half of "never mix millimetres and degrees".
    """
    assert "A" in ROTARY_LETTERS
    assert "A" not in LINEAR_LENGTH_LETTERS
    assert not ROTARY_LETTERS & LINEAR_LENGTH_LETTERS
    assert set("XYZIJKR") == LINEAR_LENGTH_LETTERS
    assert ROTARY_LETTERS | LINEAR_LENGTH_LETTERS <= WORD_LETTERS
    assert set("XYZA") == AXIS_LETTERS


def test_feed_is_not_classified_as_a_length() -> None:
    """F scales under G94/G95 but not under G93 (inverse time), so no static table can own it."""
    assert "F" not in LINEAR_LENGTH_LETTERS
    assert "F" not in ROTARY_LETTERS
    assert "F" in WORD_LETTERS


# =========================================================================== T1.3 resolver


def _cmds(text: str, **kwargs: object):
    result = parse(text, **kwargs)
    return result.commands, result.errors


# --------------------------------------------------------------------------- canonicalization


@pytest.mark.parametrize(
    ("text", "expected"),
    [("G1", "1"), ("G01", "1"), ("G1.0", "1"), ("G001", "1"), ("G90.1", "90.1"), ("G0", "0")],
)
def test_gcode_canonicalization(text: str, expected: str) -> None:
    """`G01`, `G1` and `G1.0` are the same code; `G90.1` is a different one."""
    commands, _ = _cmds(text)
    assert commands[0].gcodes == [expected]


def test_canonical_codes_are_strings_not_floats() -> None:
    commands, _ = _cmds("G90.1 G21")
    assert all(isinstance(code, str) for code in commands[0].gcodes)
    assert commands[0].gcodes == ["90.1", "21"]


def test_multiple_g_and_m_words_in_one_block() -> None:
    """`G90 G21 G17 G54` is four G-words and `M3 M8` is two — a letter-keyed dict cannot hold them."""
    commands, errors = _cmds("G90 G21 G17 G54 M3 M8 S1000")
    assert commands[0].gcodes == ["90", "21", "17", "54"]
    assert commands[0].mcodes == ["3", "8"]
    assert commands[0].words == {"S": 1000.0}
    assert errors == []


# --------------------------------------------------------------------------- modal carry-over


def test_motion_carries_onto_bare_axis_blocks() -> None:
    commands, _ = _cmds("G1 X1 F100\nX2\nX3\n")
    assert [command.motion for command in commands] == ["1", "1", "1"]


def test_motion_mode_switches_and_then_carries() -> None:
    commands, _ = _cmds("G0 X1\nX2\nG1 X3\nX4\n")
    assert [command.motion for command in commands] == ["0", "0", "1", "1"]


def test_motion_is_none_before_any_motion_code() -> None:
    commands, _ = _cmds("G21 G90\nX10\n")
    assert commands[0].motion is None
    assert commands[1].motion is None


def test_canned_cycle_becomes_the_active_motion_mode() -> None:
    """Under G81 a bare `X10 Y10` is a drill cycle, not a linear move (PLAN.md § Unsupported)."""
    commands, _ = _cmds("G81 Z-5 R1 F100\nX10 Y10\nG80\nX20 Y20\n")
    assert [command.motion for command in commands] == ["81", "81", None, None]


def test_g80_cancels_motion_mode() -> None:
    commands, _ = _cmds("G1 X1\nG80\nX2\n")
    assert [command.motion for command in commands] == ["1", None, None]


def test_non_modal_codes_do_not_disturb_motion_mode() -> None:
    """`G53 G0 X0` must leave G0 active, and a G4 dwell must not cancel a contour's G1."""
    commands, _ = _cmds("G1 X1 F100\nG4 P1\nX2\n")
    assert [command.motion for command in commands] == ["1", "1", "1"]


# --------------------------------------------------------------------------- modal groups


def test_two_codes_from_one_modal_group_is_an_error() -> None:
    _, errors = _cmds("G1 G2 X10")
    assert len(errors) == 1
    assert "modal group 'motion'" in errors[0].message


def test_codes_from_different_groups_are_fine() -> None:
    _, errors = _cmds("G90 G21 G17 G94 G54 G1 X1 F10")
    assert errors == []


def test_conflicting_m_codes_are_an_error() -> None:
    _, errors = _cmds("M3 M4")
    assert len(errors) == 1
    assert "spindle" in errors[0].message


def test_m7_and_m8_conflict_because_linuxcnc_groups_them_together() -> None:
    """Mist plus flood is physically meaningful, but the normative dialect makes it a conflict."""
    _, errors = _cmds("M7 M8")
    assert len(errors) == 1
    assert "coolant" in errors[0].message


def test_repeated_address_word_is_an_error_not_silently_last_wins() -> None:
    commands, errors = _cmds("G1 X10 X20")
    assert len(errors) == 1
    assert "more than once" in errors[0].message
    assert commands[0].words["X"] == 10.0


# --------------------------------------------------------------------------- G20 inch conversion


def test_g20_converts_lengths_to_mm() -> None:
    commands, _ = _cmds("G20 G1 X1 Y2 Z0.5 F10")
    words = commands[0].words
    assert words["X"] == pytest.approx(25.4)
    assert words["Y"] == pytest.approx(50.8)
    assert words["Z"] == pytest.approx(12.7)


def test_g20_declared_units_are_recorded_for_diagnostics() -> None:
    """Geometry is mm internally, but "X exceeds 400 mm" against an inch program is unactionable."""
    commands, _ = _cmds("G20 G1 X1 F10")
    assert commands[0].modal_snapshot.units == "inch"


def test_g21_is_the_default_and_does_not_scale() -> None:
    commands, _ = _cmds("G21 G1 X1 Y2 F10")
    assert commands[0].words["X"] == 1.0
    assert commands[0].modal_snapshot.units == "mm"


def test_g20_does_not_scale_rotary_words() -> None:
    """Scaling A by 25.4 would silently corrupt every rotary move on an inch program."""
    commands, _ = _cmds("G20 G1 X1 A90 F10")
    assert commands[0].words["A"] == 90.0
    assert commands[0].words["X"] == pytest.approx(25.4)


def test_g20_scales_arc_offsets_and_radius() -> None:
    commands, _ = _cmds("G20 G2 X1 Y1 I0.5 J0.5\nG20 G2 X1 Y1 R2\n")
    assert commands[0].words["I"] == pytest.approx(12.7)
    assert commands[1].words["R"] == pytest.approx(50.8)


def test_g20_scales_feed_under_g94_but_not_under_g93() -> None:
    """Under G93 F is inverse time in 1/minutes; scaling it would corrupt every feed."""
    per_minute, _ = _cmds("G20 G94 G1 X1 F10")
    inverse_time, _ = _cmds("G20 G93 G1 X1 F10")
    assert per_minute[0].words["F"] == pytest.approx(254.0)
    assert inverse_time[0].words["F"] == 10.0


def test_g20_applies_to_the_block_that_declares_it() -> None:
    commands, _ = _cmds("G20 X1\nX1\n")
    assert commands[0].words["X"] == pytest.approx(25.4)
    assert commands[1].words["X"] == pytest.approx(25.4)


def test_units_switch_mid_program() -> None:
    commands, _ = _cmds("G20 X1\nG21 X1\n")
    assert commands[0].words["X"] == pytest.approx(25.4)
    assert commands[1].words["X"] == 1.0


# --------------------------------------------------------------------------- modal state sharing


def test_modal_state_instance_is_shared_across_unchanged_blocks() -> None:
    """Copy-on-write is what keeps the parse inside its per-line budget."""
    commands, _ = _cmds("G1 X1 F100\nX2\nX3\nX4\n")
    snapshots = [command.modal_snapshot for command in commands]
    assert all(snapshot is snapshots[0] for snapshot in snapshots[1:])


def test_modal_state_is_replaced_only_when_a_field_changes() -> None:
    commands, _ = _cmds("G1 X1 F100\nX2\nF200 X3\nX4\n")
    first, second, third, fourth = (command.modal_snapshot for command in commands)
    assert second is first
    assert third is not first
    assert fourth is third
    assert third.feed == 200.0


def test_restating_the_same_modal_code_does_not_allocate() -> None:
    commands, _ = _cmds("G21 G90 X1\nG21 G90 X2\n")
    assert commands[1].modal_snapshot is commands[0].modal_snapshot


# --------------------------------------------------------------------------- modal state fields


def test_spindle_and_tool_tracking() -> None:
    commands, _ = _cmds("T3 M6\nM3 S2000\nM5\n")
    assert commands[0].modal_snapshot.tool == 3
    assert commands[1].modal_snapshot.spindle_on == "3"
    assert commands[1].modal_snapshot.spindle_rpm == 2000.0
    assert commands[2].modal_snapshot.spindle_on is None


def test_work_offset_tracking() -> None:
    commands, _ = _cmds("X1\nG54 X2\nG55 X3\n")
    assert commands[0].modal_snapshot.offset is None
    assert commands[1].modal_snapshot.offset == "54"
    assert commands[2].modal_snapshot.offset == "55"


def test_cutter_comp_tracking_and_cancel() -> None:
    commands, _ = _cmds("G41 D1 X1\nG40 X2\n")
    assert commands[0].modal_snapshot.cutter_comp == "41"
    assert commands[1].modal_snapshot.cutter_comp is None


def test_tool_length_offset_tracking() -> None:
    commands, _ = _cmds("G43 H2 Z1\nG49 Z2\n")
    assert commands[0].modal_snapshot.length_offset == 2
    assert commands[1].modal_snapshot.length_offset is None


def test_g43_without_h_keeps_the_active_offset() -> None:
    """LinuxCNC falls back to the current tool's offset; clearing it would drop a real Z shift."""
    commands, _ = _cmds("G43 H5 Z1\nG43 Z2\n")
    assert commands[1].modal_snapshot.length_offset == 5


# --------------------------------------------------------------------------- plumbing


def test_tokenizer_errors_reach_the_parse_result() -> None:
    _, errors = _cmds("G1 X10\nX#\n")
    assert errors, "tokenizer errors must not be dropped by the resolver"


def test_blank_and_comment_lines_produce_no_commands() -> None:
    commands, errors = _cmds("(header)\n\n%\nG1 X1 F10\n")
    assert len(commands) == 1
    assert errors == []


def test_block_delete_off_by_default_executes_the_block() -> None:
    commands, _ = _cmds("/G1 X1 F10\n")
    assert len(commands) == 1


def test_block_delete_on_skips_the_block() -> None:
    commands, _ = _cmds("/G1 X1 F10\nG1 X2\n", block_delete=True)
    assert len(commands) == 1
    assert commands[0].words["X"] == 2.0


def test_every_command_traces_back_to_its_source_line() -> None:
    commands, _ = _cmds("G1 X1 F10\n\nX2\n(c)\nX3\n")
    assert [command.ref.line_no for command in commands] == [1, 3, 5]


def test_restated_identical_feed_does_not_allocate_a_new_state() -> None:
    """CAM output repeats F on every line; treating that as a change defeats copy-on-write.

    Regression: `feed` is applied after unit conversion rather than inside `_modal_changes`, so it
    originally bypassed the "only if it differs" filter and allocated one ModalState per line on the
    most common real-world input.
    """
    commands, _ = _cmds("G1 X1 F1200\nX2 F1200\nX3 F1200\n")
    snapshots = [command.modal_snapshot for command in commands]
    assert all(snapshot is snapshots[0] for snapshot in snapshots[1:])
    assert snapshots[0].feed == 1200.0


def test_changing_the_feed_does_allocate() -> None:
    commands, _ = _cmds("G1 X1 F1200\nX2 F600\n")
    assert commands[1].modal_snapshot is not commands[0].modal_snapshot
    assert commands[1].modal_snapshot.feed == 600.0


# --------------------------------------------------------------------------- coordinate transforms


def test_every_coordinate_transform_mode_matches_its_group_and_state_field() -> None:
    """`mode.field` is three names at once and they must stay identical.

    The resolver routes a code to a `ModalState` field via the modal-group *name*, so a rename that
    misses one spelling makes the field silently never update — and every transform span quietly
    vanishes, which is the failure this whole milestone exists to prevent.
    """
    for mode in COORD_TRANSFORM_MODES:
        assert _GROUP_OF[mode.activate] == mode.field
        assert _GROUP_OF[mode.cancel] == mode.field
        assert hasattr(ModalState(), mode.field)


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_transform_is_carried_across_bare_blocks_until_its_cancel(mode) -> None:
    text = f"G{mode.activate}\nX10\nX20\nG{mode.cancel}\nX30\n"
    states = [command.modal_snapshot for command in parse(text).commands]
    assert [getattr(state, mode.field) for state in states[:3]] == [mode.activate] * 3
    assert getattr(states[-1], mode.field) is None


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_transform_does_not_disturb_the_carried_motion_mode(mode) -> None:
    """`G68 G1 X10` must keep G1 active: the transform groups are not the motion group."""
    commands = parse(f"G1 X10 F100\nG{mode.activate}\nX20\n").commands
    assert [command.motion for command in commands] == ["1", "1", "1"]


@pytest.mark.parametrize("mode", COORD_TRANSFORM_MODES, ids=lambda m: m.field)
def test_a_transform_and_its_cancel_in_one_block_conflict(mode) -> None:
    result = parse(f"G{mode.activate} G{mode.cancel}\n")
    assert [error.kind for error in result.errors] == [ParseErrorKind.MODAL_GROUP_CONFLICT]


def test_the_transform_fields_do_not_defeat_copy_on_write() -> None:
    """Three more None fields must not make each block allocate a fresh ModalState."""
    commands = parse("G21 G90 G54\n" + "".join(f"G1 X{i} F100\n" for i in range(30))).commands
    assert len({id(command.modal_snapshot) for command in commands}) <= 2
