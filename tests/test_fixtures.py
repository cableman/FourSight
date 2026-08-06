"""Fixture corpus tests (T1.10).

Guards the corpus itself: every fixture parses, the baseline really is clean, and each mutation is
genuinely a *single* edit. The per-mutation diagnostic assertions arrive as T1.7–T1.9 land their
rules; until then each skips, naming the rule it is waiting for, so nothing passes by omission.
"""

from pathlib import Path

import pytest

from conftest import (
    BASELINE,
    FIXTURES,
    MUTATIONS,
    Mutation,
    apply_mutation,
    diagnose,
    diagnostic_keys,
    fixture_text,
    registered_rule_ids,
)
from foursight.fileio.loader import load
from foursight.machine.profile import MachineProfile
from foursight.parser.resolver import parse

ALL_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.nc"))
TARGETED = [name for name in ALL_FIXTURES if name != BASELINE]


def test_the_corpus_is_not_empty() -> None:
    """A glob that silently matches nothing would make every test below vacuous."""
    assert len(ALL_FIXTURES) >= 10, ALL_FIXTURES
    assert BASELINE in ALL_FIXTURES


# --------------------------------------------------------------------------- every fixture parses


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_loads_from_disk(name: str) -> None:
    """Through the real loader, so encoding and line endings are exercised too."""
    loaded = load(FIXTURES / name)
    assert loaded.text
    assert not loaded.used_fallback, f"{name} is not valid UTF-8"


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_parses_without_raising(name: str) -> None:
    """Malformed G-code is reported, never raised — including in deliberately broken fixtures."""
    result = parse(fixture_text(name))
    assert result.commands, f"{name} produced no commands at all"


@pytest.mark.parametrize("name", TARGETED)
def test_targeted_fixtures_have_no_parse_errors(name: str) -> None:
    """The targeted fixtures exercise *semantics*, so they must be lexically clean.

    A stray typo here would show up later as a mystery diagnostic attributed to the construct under
    test rather than to the typo.
    """
    result = parse(fixture_text(name))
    assert [error.message for error in result.errors] == []


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_fixture_traces_every_command_to_a_source_line(name: str) -> None:
    text = fixture_text(name)
    lines = text.splitlines()
    for command in parse(text).commands:
        assert 1 <= command.ref.line_no <= len(lines)
        assert text[command.ref.start : command.ref.end] == lines[command.ref.line_no - 1]


# --------------------------------------------------------------------------- the baseline is clean


def test_baseline_parses_without_errors(baseline_text: str) -> None:
    assert [error.message for error in parse(baseline_text).errors] == []


def test_baseline_has_the_framing_a_clean_program_needs(baseline_text: str) -> None:
    """Asserted explicitly: if the baseline stops being clean, every mutation diff becomes noise."""
    commands = parse(baseline_text).commands
    gcodes = {code for command in commands for code in command.gcodes}
    mcodes = {code for command in commands for code in command.mcodes}
    assert {"21", "90", "17", "54"} <= gcodes, "units, distance, plane and work offset must be set"
    assert "30" in mcodes, "program must end with M30"
    assert "3" in mcodes and "5" in mcodes, "spindle must start and stop"
    assert "6" in mcodes, "must contain a tool change"
    final = commands[-1].modal_snapshot
    assert final.distance == "90", "G91 must not be active at program end"
    assert final.units == "mm"


def test_baseline_exercises_all_four_axes(baseline_text: str) -> None:
    """A 4-axis baseline that never moves A would leave the rotary path untested."""
    letters = {letter for command in parse(baseline_text).commands for letter in command.words}
    assert {"X", "Y", "Z", "A"} <= letters


def test_baseline_diagnostic_set_is_recorded(baseline_diagnostics) -> None:
    """The baseline's own diagnostics are the subtrahend for every mutation test.

    It need not be empty — it is a *known* set. Today no rules are registered, so it is empty; when
    T1.7–T1.9 land, whatever remains here is by definition accepted-as-clean and this test documents
    it rather than asserting zero.
    """
    keys = diagnostic_keys(baseline_diagnostics)
    assert keys == set(), f"baseline is no longer clean: {sorted(keys)}"


# --------------------------------------------------------------------------- mutation discipline


def test_mutation_names_are_unique() -> None:
    names = [mutation.name for mutation in MUTATIONS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_every_mutation_changes_exactly_one_line(mutation: Mutation, baseline_text: str) -> None:
    """The property the whole strategy rests on, enforced rather than trusted.

    Sixteen hand-maintained near-copies of the baseline would drift; deriving them and checking the
    diff cannot.
    """
    mutated = apply_mutation(baseline_text, mutation)
    before = baseline_text.splitlines()
    after = mutated.splitlines()
    assert mutated != baseline_text, "mutation had no effect"
    assert abs(len(before) - len(after)) <= 1

    differing = [
        index
        for index in range(max(len(before), len(after)))
        if (before[index] if index < len(before) else None)
        != (after[index] if index < len(after) else None)
    ]
    if mutation.replace_with is None:
        # A deletion shifts every following line, so compare as multisets instead of by position.
        removed = [line for line in before if line not in after]
        assert len(removed) == 1, f"deletion removed {len(removed)} lines"
    else:
        assert len(differing) == 1, f"changed {len(differing)} lines, expected 1"


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_every_mutation_still_parses(mutation: Mutation, baseline_text: str) -> None:
    """Even a deliberately broken fixture must not take the parser down."""
    mutated = apply_mutation(baseline_text, mutation)
    assert parse(mutated).commands


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_mutation_adds_its_expected_diagnostic(
    mutation: Mutation,
    baseline_text: str,
    baseline_diagnostics,
    default_profile: MachineProfile,
) -> None:
    """Assert on the diagnostic *added* relative to the baseline, per PLAN.md.

    Skips while the rule does not yet exist, naming it — so an unimplemented check is visible as
    pending rather than passing silently.
    """
    if mutation.expect_rule not in registered_rule_ids():
        pytest.skip(f"awaiting rule {mutation.expect_rule!r} ({mutation.why})")

    mutated = apply_mutation(baseline_text, mutation)
    added = diagnostic_keys(diagnose(mutated, default_profile)) - diagnostic_keys(
        baseline_diagnostics
    )
    assert mutation.expect_rule in {rule_id for rule_id, _ in added}, (
        f"{mutation.name}: expected {mutation.expect_rule!r} among added diagnostics, "
        f"got {sorted(added)}"
    )


def test_pending_rules_are_visible() -> None:
    """Reports which expectations are still unimplemented, so progress is not invisible.

    Always passes; it exists for its output. When it prints nothing, T1.7-T1.9 are complete with
    respect to this corpus.
    """
    pending = sorted(
        {m.expect_rule for m in MUTATIONS if m.expect_rule not in registered_rule_ids()}
    )
    print(f"\n{len(pending)} rule(s) still unimplemented:")
    for rule_id in pending:
        print(f"  - {rule_id}")


# --------------------------------------------------------------------------- targeted constructs


def test_inch_fixture_declares_inch_units() -> None:
    commands = parse(fixture_text("inch_program.nc")).commands
    assert commands[0].modal_snapshot.units == "inch"
    # And the geometry really was converted: X0.5 in becomes 12.7 mm.
    moves = [c for c in commands if "X" in c.words]
    assert moves[0].words["X"] == pytest.approx(0.5 * 25.4)


def test_inch_fixture_leaves_rotary_in_degrees() -> None:
    commands = parse(fixture_text("inch_program.nc")).commands
    rotary = [c for c in commands if "A" in c.words]
    assert rotary and rotary[0].words["A"] == 90.0


def test_block_delete_fixture_has_deleted_blocks_that_execute_by_default() -> None:
    """Block delete defaults to OFF, meaning deleted blocks run (PLAN.md § Dialect Divergences)."""
    text = fixture_text("block_delete.nc")
    with_deleted = parse(text, block_delete=False).commands
    without = parse(text, block_delete=True).commands
    assert len(with_deleted) - len(without) == 2, "fixture must contain exactly two deleted blocks"


def test_framing_fixture_consumes_percent_and_o_number_silently() -> None:
    result = parse(fixture_text("framing_fanuc.nc"))
    assert result.errors == []
    assert all("O" not in command.words for command in result.commands)


def test_full_circle_fixture_really_is_a_full_circle() -> None:
    """Start == end is what makes it inexpressible in R-format, so the fixture must have it."""
    commands = parse(fixture_text("arc_full_circle_ijk.nc")).commands
    arcs = [c for c in commands if c.motion in {"2", "3"} and "I" in c.words]
    assert arcs, "no IJK arc found"
    arc = arcs[0]
    assert (arc.words["X"], arc.words["Y"]) == (50.0, 20.0)


def test_r_format_fixture_has_both_signs() -> None:
    """Positive R selects the arc <= 180 degrees, negative the arc > 180."""
    radii = [
        c.words["R"] for c in parse(fixture_text("arc_r_format.nc")).commands if "R" in c.words
    ]
    assert any(r > 0 for r in radii)
    assert any(r < 0 for r in radii)


def test_g18_fixture_selects_the_xz_plane_and_uses_i_k() -> None:
    """G18 maps IJK as I,K — using J here would be the classic mistake."""
    commands = parse(fixture_text("arc_g18_direction.nc")).commands
    arcs = [c for c in commands if c.motion in {"2", "3"}]
    assert arcs
    assert all(c.modal_snapshot.plane == "18" for c in arcs)
    for arc in arcs:
        assert "K" in arc.words and "J" not in arc.words


def test_helical_fixture_moves_the_plane_normal_axis_during_an_arc() -> None:
    commands = parse(fixture_text("arc_helical.nc")).commands
    helical = [c for c in commands if c.motion in {"2", "3"} and "Z" in c.words]
    assert helical, "no arc with plane-normal motion"
    assert any("A" in c.words for c in helical), "no simultaneous rotary move"


def test_canned_cycle_fixture_has_bare_axis_blocks_inside_the_span() -> None:
    """These are the blocks that must NOT be drawn as straight lines."""
    commands = parse(fixture_text("canned_cycle_span.nc")).commands
    inside = [c for c in commands if c.motion == "81" and not c.gcodes]
    assert len(inside) == 3, "fixture must have bare X/Y blocks under an active G81"
    assert any("80" in c.gcodes for c in commands), "span must be closed by G80"


def test_cutter_comp_fixture_has_an_active_span() -> None:
    commands = parse(fixture_text("cutter_comp_span.nc")).commands
    active = [c for c in commands if c.modal_snapshot.cutter_comp == "41"]
    assert active, "no block with compensation active"
    assert commands[-1].modal_snapshot.cutter_comp is None, "span must be cancelled by G40"


def test_fixtures_are_committed_as_files_not_generated() -> None:
    """The targeted fixtures are real .nc files a human can open in the app."""
    for name in TARGETED:
        assert (FIXTURES / name).is_file()
        assert Path(FIXTURES / name).suffix == ".nc"
