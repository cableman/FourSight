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
    LINEAR_LENGTH_LETTERS,
    ROTARY_LETTERS,
    WORD_LETTERS,
    Command,
    ModalState,
    SourceRef,
)

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
