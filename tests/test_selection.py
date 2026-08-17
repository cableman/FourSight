"""Line → segment selection (T3.2). No Qt — the logic lives in `gui/selection.py`.

`SegmentStore.line` makes the mask trivial. What is worth testing is **what an empty result means**,
because three different situations produce one, and telling them apart is the difference between a
useful readout and a misleading one:

- a comment or M-code line has no motion — nothing to draw, nothing wrong;
- a canned-cycle line *does* command motion that the simulator refused to draw;
- a line past the end of the program is a bug or a stale diagnostic.

Reporting the second as "no motion" would tell a user their drill cycle does nothing. PLAN.md's
governing principle applies to the selection readout, not only to the viewport.
"""

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.selection import empty_selection, select_line
from foursight.machine.profile import load_profile
from foursight.sim.simulator import simulate_text


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def simulation(text: str, profile):
    sim, _ = simulate_text(text, profile)
    return sim


def fixture_simulation(name: str, profile):
    return simulation(fixture_text(name), profile)


PROGRAM = "G21 G90 G94 G54\nG0 Z5\nG1 X10 Y10 F600\n(a comment)\nG1 X20\nM30\n"


# --------------------------------------------------------------------------- the mask


def test_a_motion_line_selects_its_own_segments(profile) -> None:
    sim = simulation(PROGRAM, profile)
    selection = select_line(sim, 3)  # G1 X10 Y10
    assert selection.has_geometry
    assert set(sim.store.line[selection.mask]) == {3}


def test_the_mask_is_one_entry_per_segment(profile) -> None:
    """A wrong-length mask would either raise in the renderer or highlight arbitrary segments."""
    sim = simulation(PROGRAM, profile)
    assert select_line(sim, 3).mask.shape == (len(sim.store),)
    assert select_line(sim, 3).mask.dtype == np.bool_


def test_the_count_matches_the_mask(profile) -> None:
    sim = simulation(PROGRAM, profile)
    selection = select_line(sim, 3)
    assert selection.count == int(selection.mask.sum()) > 0


def test_selections_partition_the_store(profile) -> None:
    """Every segment belongs to exactly one line, so the per-line counts must sum to the total.

    This is the property that makes editor↔viewport sync trustworthy: no segment is unreachable from
    the editor, and none is claimed by two lines.
    """
    sim = fixture_simulation("baseline_4axis.nc", profile)
    total = sum(
        select_line(sim, line_no).count for line_no in range(1, int(sim.store.line.max()) + 1)
    )
    assert total == len(sim.store)


def test_an_arc_line_selects_all_of_its_tessellated_segments(profile) -> None:
    """One source line can be hundreds of segments; the highlight must cover the whole arc."""
    sim = fixture_simulation("arc_full_circle_ijk.nc", profile)
    line_no = int(np.bincount(sim.store.line).argmax())
    assert select_line(sim, line_no).count > 10


# --------------------------------------------------------------------------- where playback starts


def test_the_first_segment_is_the_earliest_one_the_line_produced(profile) -> None:
    """T15.1 parks the play head at the start of this segment, so it has to be the line's *first*
    move — the store is built in program order, so the first set bit is it."""
    sim = fixture_simulation("arc_full_circle_ijk.nc", profile)
    line_no = int(np.bincount(sim.store.line).argmax())
    selection = select_line(sim, line_no)

    assert selection.first_segment == int(np.flatnonzero(selection.mask)[0])
    assert int(sim.store.line[selection.first_segment]) == line_no


def test_a_line_with_no_motion_has_no_first_segment(profile) -> None:
    """None rather than 0: segment 0 is the start of the *program*, and a comment line must not send
    the play head there."""
    sim = simulation(PROGRAM, profile)
    assert select_line(sim, 4).first_segment is None  # (a comment)
    assert empty_selection(sim).first_segment is None


# --------------------------------------------------------------------------- why there is nothing


def test_a_comment_line_reports_no_motion(profile) -> None:
    selection = select_line(simulation(PROGRAM, profile), 4)  # (a comment)
    assert not selection.has_geometry
    assert selection.suppressed is None
    assert selection.describe() == "Line 4: no motion"


def test_a_suppressed_line_says_it_is_not_drawn_rather_than_that_nothing_happens(profile) -> None:
    """The distinction that matters. A canned cycle commands motion that is deliberately not drawn.

    "no motion" here would be a lie about the program: the user would conclude their drill cycle is
    inert, when the truth is that FourSight declined to interpret it.
    """
    sim = fixture_simulation("canned_cycle_span.nc", profile)
    span = sim.suppressed[0]
    selection = select_line(sim, span.first_line)
    assert not selection.has_geometry
    assert selection.suppressed is not None
    assert "not drawn" in selection.describe()
    assert "no motion" not in selection.describe()


def test_the_suppression_reason_reaches_the_readout(profile) -> None:
    """A bare "not drawn" invites the question the reason already answers."""
    sim = fixture_simulation("canned_cycle_span.nc", profile)
    described = select_line(sim, sim.suppressed[0].first_line).describe()
    assert "canned cycle" in described


def test_every_line_of_a_suppressed_span_is_covered(profile) -> None:
    """Not just its first line — clicking anywhere in the span must explain itself."""
    sim = fixture_simulation("canned_cycle_span.nc", profile)
    span = sim.suppressed[0]
    for line_no in range(span.first_line, span.last_line + 1):
        assert select_line(sim, line_no).suppressed is not None, f"line {line_no}"


def test_a_line_past_the_end_selects_nothing_without_failing(profile) -> None:
    selection = select_line(simulation(PROGRAM, profile), 9999)
    assert not selection.has_geometry
    assert selection.count == 0


# --------------------------------------------------------------------------- unverified geometry


def test_an_unverified_line_is_selected_and_flagged(profile) -> None:
    """Cutter comp *is* drawn, so it highlights — but the readout must not imply it is trustworthy."""
    sim = fixture_simulation("cutter_comp_span.nc", profile)
    span = sim.unverified[0]
    selection = select_line(sim, span.first_line)
    assert selection.unverified is not None
    if selection.has_geometry:
        assert "unverified" in selection.describe()


def test_an_unverified_line_is_not_reported_as_suppressed(profile) -> None:
    """Drawn-but-untrusted and not-drawn are different tiers and must not collapse into one."""
    sim = fixture_simulation("cutter_comp_span.nc", profile)
    selection = select_line(sim, sim.unverified[0].first_line)
    assert selection.suppressed is None
    assert "not drawn" not in selection.describe()


# --------------------------------------------------------------------------- wording


def test_one_segment_is_singular(profile) -> None:
    """ "1 segments" in a status bar is the kind of thing that makes a tool feel unfinished."""
    sim = simulation("G21 G90 G94\nG1 X10 F600\n", profile)
    selection = select_line(sim, 2)
    assert selection.count == 1
    assert selection.describe() == "Line 2: 1 segment"


def test_many_segments_are_plural_and_grouped(profile) -> None:
    sim = fixture_simulation("arc_helical.nc", profile)
    line_no = int(np.bincount(sim.store.line).argmax())
    described = select_line(sim, line_no).describe()
    assert "segments" in described


def test_the_description_names_the_line(profile) -> None:
    """The readout has to be locatable in the file."""
    assert select_line(simulation(PROGRAM, profile), 3).describe().startswith("Line 3:")


# --------------------------------------------------------------------------- the empty selection


def test_an_empty_selection_has_a_correctly_shaped_mask(profile) -> None:
    """Returning a zero-length array would make the renderer raise on a shape mismatch."""
    sim = simulation(PROGRAM, profile)
    selection = empty_selection(sim)
    assert selection.mask.shape == (len(sim.store),)
    assert not selection.mask.any()
    assert not selection.has_geometry


def test_an_empty_program_selects_nothing_without_failing(profile) -> None:
    sim = simulation("(comment only)\n", profile)
    assert select_line(sim, 1).count == 0
    assert empty_selection(sim).mask.shape == (0,)
