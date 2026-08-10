"""Dialect selection (M6).

The one thing a dialect changes in v1 is where `ModalState.arc_distance` *starts*, and that is the
most consequential possible thing to get wrong quietly: an arc read under the wrong I/J convention
is drawn in the wrong place, with the wrong radius, and no diagnostic fires. So the assertions here
run end to end — through `parse`, and through `simulate` where a wrong centre moves real geometry —
rather than stopping at the modal snapshot.
"""

import dataclasses

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH
from foursight.machine.profile import (
    DialectSettings,
    ProfileError,
    load_profile,
    load_profile_text,
    with_arc_centre,
    with_dialect,
)
from foursight.parser.dialect import (
    ARC_CENTRE_ABSOLUTE,
    ARC_CENTRE_INCREMENTAL,
    DIALECT_NAMES,
    LINUXCNC,
    MACH3,
    Dialect,
    DialectName,
    DwellUnits,
    preset,
)
from foursight.parser.model import ParseErrorKind
from foursight.parser.resolver import parse, resolve
from foursight.parser.tokenizer import tokenize
from foursight.sim.simulator import simulate_text

# An arc whose I/J read differently under each convention. Under G91.1 the centre is the start plus
# (I, J) = (20, 10); under G90.1 the centre IS (20, 10). Two different arcs from identical text.
ARC_PROGRAM = "G21 G90 G17 G94 G54\nG0 X10 Y10\nG2 X30 Y10 I20 J10 F300\n"

MACH3_ABSOLUTE_TOML = """
[machine]
name = "mach3 mill"
[dialect]
name = "mach3"
arc_centre = "absolute"
"""


@pytest.fixture(scope="module")
def default_profile_local():
    return load_profile(DEFAULT_PROFILE_PATH)


# --------------------------------------------------------------------------- the value itself


def test_the_default_dialect_is_linuxcnc() -> None:
    """LinuxCNC is normative (PLAN.md § Reference dialect), so it is what you get for free."""
    assert Dialect() == LINUXCNC
    assert LINUXCNC.name == DialectName.LINUXCNC
    assert LINUXCNC.arc_distance == ARC_CENTRE_INCREMENTAL
    assert LINUXCNC.dwell_units == DwellUnits.SECONDS


def test_the_mach3_presets_defaults_match_linuxcnc_but_its_code_table_does_not() -> None:
    """Mach3 ships with incremental I/J and seconds, exactly as LinuxCNC — that part is honest.

    What differs is the *code table*: G70/G71 are Mach3's inch/mm codes, and G80 does not conflict
    with a motion code. The controller-setting defaults matching is why `[dialect].arc_centre`
    exists at all; the code table is why the name alone is worth selecting.
    """
    assert MACH3.name == DialectName.MACH3
    assert (MACH3.arc_distance, MACH3.dwell_units) == (
        LINUXCNC.arc_distance,
        LINUXCNC.dwell_units,
    )
    assert MACH3.extra_gcodes == {"70", "71"}
    assert LINUXCNC.extra_gcodes == frozenset()
    assert MACH3.cycle_cancel_conflicts is False
    assert LINUXCNC.cycle_cancel_conflicts is True


def test_the_profile_carries_the_presets_code_table_through() -> None:
    """`as_parser_dialect` must build from the preset, not from a bare `Dialect`.

    Listing fields there would silently drop every one it forgot, so a Mach3 profile would parse
    under LinuxCNC's code table while still calling itself Mach3 — the failure would look like the
    dialect simply not working, with nothing to point at.
    """
    dialect = load_profile_text(MACH3_ABSOLUTE_TOML).parser_dialect
    assert dialect.arc_distance == ARC_CENTRE_ABSOLUTE, "the configured setting survives"
    assert dialect.extra_gcodes == MACH3.extra_gcodes, "and so does the preset's code table"
    assert dialect.cycle_cancel_conflicts is False


# --------------------------------------------------------------------------- Mach3 code table

SAFE_START = "G00 G21 G17 G90 G40 G49 G80\n"


def test_the_mach3_safe_start_line_is_not_an_error() -> None:
    """`G00 ... G80` is what nearly every post emits, and Mach3 accepts it.

    LinuxCNC puts G80 in modal group 1 with G0, so it is genuinely a conflict there — but reporting
    it as an *error* made `foursight check` exit 1 on ordinary, correct Mach3 output.
    """
    assert parse(SAFE_START, dialect=MACH3).errors == []


def test_the_safe_start_line_is_still_an_error_under_linuxcnc() -> None:
    """The normative dialect stays strict; that is the whole point of having one."""
    errors = parse(SAFE_START).errors
    assert [e.kind for e in errors] == [ParseErrorKind.MODAL_GROUP_CONFLICT]


@pytest.mark.parametrize("block", ["G0 G80", "G80 G0"])
def test_the_cancel_never_beats_a_real_motion_code_whatever_the_order(block: str) -> None:
    """Scanning in order and returning on the first of the two made the two spellings differ.

    Order-dependence in a modal resolution shows up as one wrong rapid in someone else's posted
    output, which is exactly the class of bug that is impossible to find from the symptom.
    """
    assert parse(f"{block} X10\n", dialect=MACH3).commands[0].motion == "0"
    assert parse(f"{block} X10\n").commands[0].motion == "0"


def test_g80_alone_still_cancels_the_motion_mode() -> None:
    """The exemption must not cost G80 its actual job."""
    commands = parse("G81 Z-5 R2 F100\nX10\nG80\nX20\n", dialect=MACH3).commands
    assert [c.motion for c in commands] == ["81", "81", None, None]


def test_two_real_motion_codes_still_conflict_under_mach3() -> None:
    """Only the G80 pair is exempt. `G1 G2` is genuinely ambiguous under every dialect."""
    errors = parse("G1 G2 X10\n", dialect=MACH3).errors
    assert [e.kind for e in errors] == [ParseErrorKind.MODAL_GROUP_CONFLICT]


def test_a_third_motion_code_beside_the_cancel_still_conflicts() -> None:
    """The exemption keeps the real motion code in view, so it cannot mask a following conflict."""
    errors = parse("G0 G80 G1 X10\n", dialect=MACH3).errors
    assert [e.kind for e in errors] == [ParseErrorKind.MODAL_GROUP_CONFLICT]


def test_mach3_reads_g71_as_millimetres_and_g70_as_inches() -> None:
    """Vectric's Mach2/3 post emits G71 for metric; under LinuxCNC it is an unknown code.

    G70 is the case a `code == "20"` test would get wrong: it would read every G70 as millimetres
    and leave an inch program's coordinates unscaled by 25.4.
    """
    metric = parse("G71\nG1 X10 F100\n", dialect=MACH3).commands[-1]
    assert metric.modal_snapshot.units == "mm"
    assert metric.words["X"] == pytest.approx(10.0)

    imperial = parse("G70\nG1 X10 F100\n", dialect=MACH3).commands[-1]
    assert imperial.modal_snapshot.units == "inch"
    assert imperial.words["X"] == pytest.approx(254.0), "inch input converts to mm at parse time"


def test_g20_and_g21_still_work_under_mach3() -> None:
    """A dialect adds spellings; it never removes one. Posts emit both, often in the same file."""
    assert parse("G20\n", dialect=MACH3).commands[0].modal_snapshot.units == "inch"
    assert parse("G21\n", dialect=MACH3).commands[0].modal_snapshot.units == "mm"


def test_g70_and_g71_conflict_with_g20_and_g21_under_mach3() -> None:
    """They are the same modal group, so stating two of them in one block is still ambiguous."""
    assert parse("G20 G71\n", dialect=MACH3).errors


def test_g70_and_g71_remain_unknown_under_linuxcnc() -> None:
    """LinuxCNC has no such codes, so it must still say so — and Fanuc's G71 is a turning cycle."""
    command = parse("G71\nG1 X10 F100\n").commands[0]
    assert command.modal_snapshot.units == "mm", "unrecognized, so it changes nothing"
    assert "71" in command.gcodes, "and it is still reported as written"


def test_the_dialect_is_frozen_and_hashable() -> None:
    assert {LINUXCNC, MACH3}
    with pytest.raises(dataclasses.FrozenInstanceError):
        LINUXCNC.arc_distance = ARC_CENTRE_ABSOLUTE  # type: ignore[misc]


@pytest.mark.parametrize("name", DIALECT_NAMES)
def test_every_named_dialect_has_a_preset(name: str) -> None:
    assert preset(name).name == name


def test_an_unknown_dialect_raises_rather_than_falling_back() -> None:
    """A typo that quietly selected LinuxCNC would draw a Mach3 program under the wrong rules."""
    with pytest.raises(ValueError, match="unknown dialect"):
        preset("fanuc")


# --------------------------------------------------------------------------- parse


def test_the_default_parse_is_incremental_arc_centre() -> None:
    """The regression floor: nothing about this change may move default behaviour."""
    result = parse(ARC_PROGRAM)
    assert all(c.modal_snapshot.arc_distance == ARC_CENTRE_INCREMENTAL for c in result.commands)


def test_an_absolute_dialect_starts_the_arc_centre_absolute() -> None:
    dialect = Dialect(name=DialectName.MACH3, arc_distance=ARC_CENTRE_ABSOLUTE)
    result = parse(ARC_PROGRAM, dialect=dialect)
    assert all(c.modal_snapshot.arc_distance == ARC_CENTRE_ABSOLUTE for c in result.commands)


def test_the_dialect_sets_the_start_not_a_lock() -> None:
    """A program that states its arc mode is unambiguous; no controller setting overrides it."""
    dialect = Dialect(name=DialectName.MACH3, arc_distance=ARC_CENTRE_ABSOLUTE)
    result = parse("G21 G90 G54\nG91.1\nG2 X30 Y10 I20 J10 F300\n", dialect=dialect)
    assert result.commands[0].modal_snapshot.arc_distance == ARC_CENTRE_ABSOLUTE
    assert result.commands[-1].modal_snapshot.arc_distance == ARC_CENTRE_INCREMENTAL


def test_parse_and_resolve_agree_about_the_dialect() -> None:
    dialect = Dialect(name=DialectName.MACH3, arc_distance=ARC_CENTRE_ABSOLUTE)
    through_parse = parse(ARC_PROGRAM, dialect=dialect)
    through_resolve = resolve(tokenize(ARC_PROGRAM), dialect=dialect)
    assert [c.modal_snapshot for c in through_parse.commands] == [
        c.modal_snapshot for c in through_resolve.commands
    ]


@pytest.mark.parametrize("arc_distance", [ARC_CENTRE_INCREMENTAL, ARC_CENTRE_ABSOLUTE])
def test_copy_on_write_survives_a_non_default_dialect(arc_distance: str) -> None:
    """Injecting at construction must not turn every block into a fresh ModalState.

    The parse-rate target depends on consecutive blocks sharing one frozen instance — T1.12 measures
    exactly one across 42,858 commands — and a dialect start that registered as a *change* on every
    line would defeat it silently, showing up only as a missed perf floor.
    """
    text = "G21 G90 G54\n" + "".join(f"G1 X{i} F100\n" for i in range(50))
    result = parse(text, dialect=Dialect(arc_distance=arc_distance))
    identities = {id(c.modal_snapshot) for c in result.commands}
    assert len(identities) <= 2, "a dialect start should not defeat copy-on-write"


def test_a_wrong_arc_centre_moves_real_geometry(default_profile_local) -> None:
    """The whole point: this is what a silently wrong dialect costs.

    Asserted on the simulated path rather than on the modal snapshot, because a snapshot assertion
    would still pass if `sim/interpolate` stopped honouring the field.
    """
    incremental, _ = simulate_text(ARC_PROGRAM, default_profile_local)
    absolute_profile = load_profile_text(MACH3_ABSOLUTE_TOML)
    absolute, _ = simulate_text(ARC_PROGRAM, absolute_profile)
    assert len(incremental.store) and len(absolute.store)
    assert not np.allclose(
        incremental.store.lin.max(axis=(0, 1)), absolute.store.lin.max(axis=(0, 1))
    ), "the two I/J conventions produced the same arc; the dialect is not reaching the simulator"


# --------------------------------------------------------------------------- the profile section


def test_a_profile_with_no_dialect_section_is_linuxcnc(default_profile_local) -> None:
    assert default_profile_local.dialect == DialectSettings()
    assert default_profile_local.parser_dialect == LINUXCNC


def test_the_shipped_profile_declares_its_dialect() -> None:
    """It is commented as documentation for the keys; it must still be the normative default."""
    assert load_profile(DEFAULT_PROFILE_PATH).dialect.name == DialectName.LINUXCNC


def test_an_absolute_mach3_profile_produces_an_absolute_parser_dialect() -> None:
    profile = load_profile_text(MACH3_ABSOLUTE_TOML)
    assert profile.dialect.name == DialectName.MACH3
    assert profile.parser_dialect.arc_distance == ARC_CENTRE_ABSOLUTE


def test_a_milliseconds_profile_reaches_the_parser_dialect() -> None:
    profile = load_profile_text('[dialect]\nname = "mach3"\ndwell_units = "milliseconds"\n')
    assert profile.parser_dialect.dwell_units == DwellUnits.MILLISECONDS


def test_an_unknown_dialect_name_refuses_to_load() -> None:
    with pytest.raises(ProfileError, match=r"\[dialect\].name"):
        load_profile_text('[dialect]\nname = "haas"\n')


@pytest.mark.parametrize("key,value", [("arc_centre", "absolute"), ("dwell_units", "milliseconds")])
def test_a_controller_setting_is_refused_under_linuxcnc(key: str, value: str) -> None:
    """Ignoring it would let a user believe they had configured something.

    Under LinuxCNC neither is a setting: G90.1/G91.1 decide the arc centre, and G4 P is seconds by
    specification.
    """
    with pytest.raises(ProfileError, match="controller setting"):
        load_profile_text(f'[dialect]\nname = "linuxcnc"\n{key} = "{value}"\n')


def test_a_controller_setting_set_to_its_default_is_still_refused() -> None:
    """Presence, not value: only the raw table can tell "absent" from "explicitly the default"."""
    with pytest.raises(ProfileError, match="controller setting"):
        load_profile_text('[dialect]\narc_centre = "incremental"\n')


@pytest.mark.parametrize(
    "body", ['name = "mach3"\narc_centre = "sideways"', 'name = "mach3"\ndwell_units = "years"']
)
def test_a_bad_controller_setting_value_refuses_to_load(body: str) -> None:
    with pytest.raises(ProfileError):
        load_profile_text(f"[dialect]\n{body}\n")


def test_the_dialect_section_is_a_known_section() -> None:
    """An unrecognized section lands in unknown_keys, which would make [dialect] a silent no-op."""
    assert load_profile_text('[dialect]\nname = "mach3"\n').unknown_keys == ()


def test_an_unknown_dialect_key_is_reported_not_silently_ignored() -> None:
    assert "dialect.arc_mode" in load_profile_text('[dialect]\narc_mode = "x"\n').unknown_keys


# --------------------------------------------------------------------------- the CLI overrides


def test_overriding_to_the_same_dialect_keeps_the_controller_settings() -> None:
    profile = load_profile_text(MACH3_ABSOLUTE_TOML)
    assert with_dialect(profile, "mach3") is profile
    assert with_dialect(profile, "mach3").dialect.arc_centre == "absolute"


def test_overriding_to_a_different_dialect_resets_the_controller_settings() -> None:
    """Settings tuned for one controller describe nothing about another."""
    profile = load_profile_text(MACH3_ABSOLUTE_TOML)
    overridden = with_dialect(profile, "linuxcnc")
    assert overridden.dialect == DialectSettings()
    assert overridden.parser_dialect.arc_distance == ARC_CENTRE_INCREMENTAL


def test_no_override_is_a_no_op(default_profile_local) -> None:
    assert with_dialect(default_profile_local, None) is default_profile_local
    assert with_arc_centre(default_profile_local, None) is default_profile_local


def test_the_arc_centre_override_moves_the_parser_dialect() -> None:
    profile = with_arc_centre(load_profile_text('[dialect]\nname = "mach3"\n'), "absolute")
    assert profile.parser_dialect.arc_distance == ARC_CENTRE_ABSOLUTE


def test_the_arc_centre_override_is_refused_under_linuxcnc(default_profile_local) -> None:
    """Silently accepting it would suggest we had overridden something the G-code decides."""
    with pytest.raises(ProfileError, match="controller setting"):
        with_arc_centre(default_profile_local, "absolute")


def test_the_overrides_leave_the_rest_of_the_profile_alone(default_profile_local) -> None:
    """`replace` on the wrong field would quietly drop the machine's limits."""
    overridden = with_dialect(default_profile_local, "mach3")
    assert overridden.limits == default_profile_local.limits
    assert overridden.axes == default_profile_local.axes
    assert overridden.kinematics == default_profile_local.kinematics
