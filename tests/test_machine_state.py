"""MachineState tests (T2.2).

The stepper's job is to hand the simulator machine coordinates and to be honest when it cannot. Two
constructs PLAN.md lists as interpreted are not actually computable from available data, and they are
resolved *differently* — so both resolutions are asserted here rather than left to the reader:

- **G28/G30** has no reference point without `[axes.*].home`, so the move is not drawn.
- **G43/G44** has no tool table, so the path *is* drawn and the unmodelled Z datum is flagged.

The difference is proportionality: a G28 is one rapid, while G43 appears in nearly every real
program and suppressing all of them would make the previewer useless.
"""

import dataclasses

import pytest

from foursight.machine.profile import load_profile_text
from foursight.machine.state import MachineState, Position
from foursight.parser.resolver import parse

WITH_HOME = """
[machine]
units = "mm"
[axes.x]
min = 0.0
max = 400.0
home = 0.0
[axes.y]
max = 300.0
home = 0.0
[axes.z]
max = 100.0
home = 100.0
[axes.a]
type = "rotary"
wrap = true
home = 0.0
"""

WITH_OFFSET = WITH_HOME + "\n[offsets]\ng54 = [10.0, 20.0, 30.0, 0.0]\n"
NO_HOME = '[machine]\nunits = "mm"\n[offsets]\ng54 = [0.0, 0.0, 0.0, 0.0]\n'
NO_OFFSETS = '[machine]\nunits = "mm"\n'


def steps(program: str, profile_text: str = WITH_HOME):
    state = MachineState(load_profile_text(profile_text))
    return [state.apply(command) for command in parse(program).commands], state


def moves(program: str, profile_text: str = WITH_HOME):
    collected, _ = steps(program, profile_text)
    return [move for step in collected for move in step.moves]


# --------------------------------------------------------------------------- coordinate frames


def test_moves_are_reported_in_machine_coordinates() -> None:
    """`SegmentStore.lin` is machine coordinates, so the stepper must already have applied the offset."""
    move = moves("G21 G90 G54\nG1 X100 Y50 Z-5 F100\n", WITH_OFFSET)[0]
    assert (move.end.x, move.end.y, move.end.z) == (110.0, 70.0, 25.0)
    assert move.offset_known is True


def test_programmed_and_machine_frames_are_both_tracked() -> None:
    """Conflating them is how a work offset gets applied twice."""
    collected, state = steps("G21 G90 G54\nG1 X100 F100\n", WITH_OFFSET)
    assert state.programmed.x == 100.0
    assert collected[-1].moves[0].end.x == 110.0


def test_an_unknown_work_offset_is_flagged_not_assumed() -> None:
    move = moves("G21 G90 G54\nG1 X100 F100\n", NO_OFFSETS)[0]
    assert move.end.x == 100.0
    assert move.offset_known is False


def test_g53_coordinates_are_already_machine_absolute() -> None:
    """Adding the work offset to a G53 block would double-count it."""
    move = moves("G21 G90 G54\nG53 G0 X100\n", WITH_OFFSET)[0]
    assert move.end.x == 100.0


def test_incremental_moves_accumulate_in_the_programmed_frame() -> None:
    collected, state = steps("G21 G90 G54\nG1 X10 F100\nG91\nX5\nX5\n", WITH_OFFSET)
    assert state.programmed.x == 20.0
    assert collected[-1].moves[0].end.x == 30.0  # 20 programmed + 10 offset


# --------------------------------------------------------------------------- motion kinds


def test_rapids_and_feeds_are_distinguished() -> None:
    collected = moves("G21 G90 G54\nG0 X10\nG1 X20 F100\nG2 X30 Y5 I5 J0\n")
    assert [move.rapid for move in collected] == [True, False, False]


def test_an_arc_yields_one_move_describing_its_endpoints() -> None:
    """Tessellation is T2.3's job; the stepper reports where the arc starts and ends."""
    collected = moves("G21 G90 G54\nG1 X40 Y20 F100\nG2 X60 Y20 I10 J0\n")
    assert len(collected) == 2
    assert (collected[1].start.x, collected[1].end.x) == (40.0, 60.0)


def test_a_mode_only_block_produces_no_move() -> None:
    collected, _ = steps("G21 G90 G54\nG17 G94\nM8\n")
    assert all(not step.moved for step in collected)


def test_a_block_that_repeats_the_current_position_produces_no_move() -> None:
    """A zero-length segment would add a spurious entry to the timeline."""
    collected, _ = steps("G21 G90 G54\nG1 X10 F100\nX10\n")
    assert sum(len(step.moves) for step in collected) == 1


# --------------------------------------------------------------------------- G4 dwell


def test_dwell_is_reported_in_seconds() -> None:
    """LinuxCNC treats G4 P as seconds; Fanuc uses milliseconds (a documented divergence)."""
    collected, _ = steps("G21 G90 G54\nG4 P2.5\n")
    assert collected[-1].dwell == 2.5
    assert not collected[-1].moved


def test_a_large_dwell_is_carried_through_not_judged() -> None:
    """P > 60 is a likely ms/s confusion, but flagging it is the verifier's call, not the stepper's."""
    collected, _ = steps("G21 G90 G54\nG4 P5000\n")
    assert collected[-1].dwell == 5000.0
    assert collected[-1].undrawable is None


def test_a_dwell_without_p_is_zero_seconds() -> None:
    collected, _ = steps("G21 G90 G54\nG4\n")
    assert collected[-1].dwell == 0.0


# --------------------------------------------------------------------------- G28/G30


def test_reference_return_traverses_to_the_configured_home() -> None:
    collected = moves("G21 G90 G54\nG1 X100 Y50 F100\nG28\n")
    assert len(collected) == 2
    home = collected[-1].end
    assert (home.x, home.y, home.z) == (0.0, 0.0, 100.0)
    assert collected[-1].rapid is True


def test_reference_return_via_an_intermediate_point_is_two_rapids() -> None:
    """`G28 X0 Y0` traverses to the intermediate point first, then to the reference point."""
    collected = moves("G21 G90 G54\nG1 X100 Y50 Z-5 F100\nG28 X50 Y25\n")
    assert len(collected) == 3
    assert (collected[1].end.x, collected[1].end.y) == (50.0, 25.0)
    assert (collected[2].end.x, collected[2].end.z) == (0.0, 100.0)
    assert collected[1].rapid and collected[2].rapid


@pytest.mark.parametrize("code", ["G28", "G30"])
def test_reference_return_without_a_configured_home_is_not_drawn(code: str) -> None:
    """A reference move to a guessed target is exactly the confidently-wrong output PLAN forbids."""
    collected, _ = steps(f"G21 G90 G54\nG1 X10 F100\n{code}\n", NO_HOME)
    final = collected[-1]
    assert final.moves == ()
    assert final.undrawable is not None
    assert "home" in final.undrawable


def test_position_after_an_undrawable_reference_return_is_unknown() -> None:
    """Claiming to still know the position would make every later move wrong too."""
    collected, state = steps("G21 G90 G54\nG1 X10 Y20 F100\nG28\n", NO_HOME)
    assert state.programmed == Position()


def test_position_after_a_drawable_reference_return_is_the_home_point() -> None:
    _, state = steps("G21 G90 G54\nG1 X100 F100\nG28\n", WITH_OFFSET)
    # Home is a machine position; converting back through the +10 X offset gives programmed -10.
    assert state.programmed.x == -10.0


# --------------------------------------------------------------------------- G43/G44/G49


def test_tool_length_is_tracked_and_flagged_rather_than_suppressed() -> None:
    """G43 appears in nearly every real program; refusing to draw them all would be useless.

    The offset shifts the Z datum uniformly without changing the path's shape, so the path is drawn
    and the unmodelled datum is recorded instead.
    """
    collected, state = steps("G21 G90 G54\nG43 H2 Z10\nG1 X10 F100\n")
    assert state.active_h == 2
    assert collected[-1].tool_length_unmodelled is True
    assert collected[-1].moved, "the path must still be drawn"


def test_g49_cancels_the_tool_length_offset() -> None:
    collected, state = steps("G21 G90 G54\nG43 H2 Z10\nG49\nG1 X10 F100\n")
    assert state.active_h is None
    assert collected[-1].tool_length_unmodelled is False


def test_g44_is_tracked_like_g43() -> None:
    _, state = steps("G21 G90 G54\nG44 H3 Z10\n")
    assert state.active_h == 3


def test_g43_without_an_h_records_that_an_unknown_offset_is_active() -> None:
    """LinuxCNC falls back to the current tool's offset, which we cannot know either."""
    _, state = steps("G21 G90 G54\nG43 Z10\n")
    assert state.active_h == 0


def test_g43_without_an_h_keeps_a_previously_set_one() -> None:
    _, state = steps("G21 G90 G54\nG43 H5 Z10\nG43 Z20\n")
    assert state.active_h == 5


def test_no_tool_length_means_no_flag() -> None:
    collected, _ = steps("G21 G90 G54\nG1 X10 F100\n")
    assert collected[-1].tool_length_unmodelled is False


# --------------------------------------------------------------------------- rotary


def test_rotary_is_carried_in_its_own_field_not_the_position_vector() -> None:
    move = moves("G21 G90 G54\nG1 X10 A90 F100\n")[0]
    assert move.end.a == 90.0
    assert move.end.x == 10.0
    # Position exposes four *named* axes rather than an indexable vector, so there is nothing to
    # take a norm over: `math.dist(start, end)` would not even typecheck against it.
    assert [f.name for f in dataclasses.fields(Position)] == ["x", "y", "z", "a"]
    with pytest.raises(TypeError):
        len(move.end)  # type: ignore[arg-type]


def test_a_rotary_only_move_still_counts_as_motion() -> None:
    """`G1 A90` moves the machine even though no linear axis changes."""
    collected = moves("G21 G90 G54\nG1 A90 F1800\n")
    assert len(collected) == 1
    assert collected[0].end.a == 90.0


def test_the_rotary_work_offset_applies_to_a_but_is_not_scaled() -> None:
    rotated = WITH_HOME + "\n[offsets]\ng54 = [0.0, 0.0, 0.0, 15.0]\n"
    move = moves("G21 G90 G54\nG1 A90 F1800\n", rotated)[0]
    assert move.end.a == 105.0


# --------------------------------------------------------------------------- fixtures


def test_the_baseline_fixture_steps_without_anything_undrawable(baseline_text: str) -> None:
    collected, _ = steps(baseline_text)
    undrawable = [step.undrawable for step in collected if step.undrawable]
    assert undrawable == []
    assert sum(len(step.moves) for step in collected) > 0


def test_every_move_from_the_baseline_has_a_known_offset(baseline_text: str) -> None:
    """The default fixture profile sets g54, so nothing should be guessing."""
    for move in moves(baseline_text, WITH_OFFSET):
        assert move.offset_known is True


# --------------------------------------------------------------------------- M98/M99 subprograms

MILLISECOND_DWELL = """
[machine]
units = "mm"
[dialect]
name = "mach3"
dwell_units = "milliseconds"
"""


@pytest.mark.parametrize("code", ["98", "99"])
def test_a_subprogram_call_loses_the_position_and_is_undrawable(code: str) -> None:
    """We do not expand subprograms, so afterwards we genuinely do not know where the machine is.

    Drawing the next block would draw a straight line from wherever the main program left off to
    wherever the subprogram happened to end — a fabricated move at full confidence.
    """
    collected, state = steps(f"G21 G90 G54\nG0 X10 Y10 Z5\nM{code} P1000\n")
    assert collected[-1].undrawable is not None
    assert not collected[-1].moved
    assert state.position_lost
    assert state.programmed == Position()


def test_a_subprogram_call_outranks_everything_else_in_its_block() -> None:
    """Nothing else about the block can be honoured once we stop knowing what ran."""
    collected, _ = steps("G21 G90 G54\nG0 X10 Y10 Z5\nG1 X20 M98 P1000 F100\n")
    assert collected[-1].undrawable is not None
    assert not collected[-1].moved


def test_g98_and_g99_do_not_lose_the_position() -> None:
    """They are canned-cycle return modes, not subprogram calls. Same digits, different letter."""
    collected, state = steps("G21 G90 G54\nG98\nG99\nG1 X10 F100\n")
    assert not state.position_lost
    assert all(step.undrawable is None for step in collected)


def test_a_full_absolute_restatement_re_establishes_the_position_after_m98() -> None:
    """A partial restatement is not enough: `_advance` leaves the unmentioned axes unknown."""
    collected, _ = steps("G21 G90 G54\nG0 X10 Y10 Z5\nM98 P1000\nG0 X1 Y2 Z3\nG1 X4 F100\n")
    assert collected[-1].moved
    assert collected[-1].moves[0].start.x == 1.0


# --------------------------------------------------------------------------- dialect dwell units


def test_a_milliseconds_dialect_converts_the_dwell_to_seconds() -> None:
    """The units come from the profile, never from the magnitude of P."""
    collected, _ = steps("G21 G90 G54\nG4 P2500\n", MILLISECOND_DWELL)
    assert collected[-1].dwell == 2.5


def test_the_default_dialect_leaves_the_dwell_in_seconds() -> None:
    collected, _ = steps("G21 G90 G54\nG4 P2500\n")
    assert collected[-1].dwell == 2500.0
