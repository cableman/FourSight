"""Performance regression tests (T1.12 parse; T2.11 segment budget and memory).

PLAN.md § Performance Requirements sets **parse ≥ 50k lines/sec**, and says plainly why this file
exists: *"Hard numbers in the plan need a test or they decay."* Nothing else in the suite would
notice a change that halved the parse rate.

Two deliberate choices about how to measure without being flaky:

- **Assert the floor, log the actual.** The threshold is the plan's requirement, not the local
  measurement. Asserting anything near the number this machine happens to reach would fail on a
  slower CI runner for no good reason.
- **Best of several runs.** A shared runner will occasionally deschedule the process mid-measurement;
  taking the minimum elapsed time removes that noise without inflating the result, since no amount of
  scheduling luck makes the code faster than it is.

If a CI runner genuinely cannot reach the floor, override `FOURSIGHT_PERF_MIN_RATE` rather than
deleting the test — a wrong threshold is worth arguing about, an absent one is not.

**T2.11 — what measuring the segment budget actually revealed.** PLAN.md says "the 100k-line and
500k-segment targets are different axes." They are, and the difficulty runs opposite to the
intuition: simulating a program to **500k segments takes 0.18 s** (2.7M segments/sec) while
**39k segments derived from 50k CAM lines takes 1.76 s** (22k segments/sec). A 120× spread on the
same code. The cost is **per motion block**, roughly 50 µs of fixed numpy overhead each, and a block
that tessellates into hundreds of segments amortizes it away. So the segment budget is comfortable
and the *line* budget is the binding constraint — which is what `test_the_m2_gate_budget...` records.
"""

import os
import time

import numpy as np
import pytest

from conftest import FIXTURES, fixture_text, record_measurement
from foursight.machine.profile import default_profile_path, load_profile
from foursight.parser.resolver import parse
from foursight.parser.tokenizer import tokenize
from foursight.sim.simulator import simulate
from foursight.verify.rules import Program, verify

# PLAN.md § Performance Requirements. Overridable for a runner that cannot reach it, but not
# silently: the default is the plan's number.
MIN_PARSE_RATE = float(os.environ.get("FOURSIGHT_PERF_MIN_RATE", "50000"))

REPEATS = 3
LARGE = 50_000

# --- T2.11 budgets -------------------------------------------------------------------------------
TARGET_SEGMENTS = 500_000
# PLAN.md § Performance Requirements, restated after T0.7 measured a 233 MB fixed Python+Qt+Mesa
# baseline: the original "≤ 250 MB resident" cannot be met by any data model, so the budget binds on
# what the data model controls. This is the **store half** of it — measured 38.5 MB. The GL half is
# the float32 upload copy (12.0 MB at 500k), asserted in `test_batching.py`, and PLAN's *combined*
# budget is 55 MB. Keeping the two apart is deliberate: this module must not need the [gui] extra.
MAX_GEOMETRY_MB_PER_500K = 50.0
# Blocks, not lines and not segments, because that is the unit the cost actually scales with. This
# floor is what stops the simulate cost decaying; the gate *seconds* below are deliberately not
# asserted tightly, since a wall-clock threshold that holds locally and not on a shared Windows
# runner is a flaky test rather than a useful one. (The parse floor already sits at only 14% margin
# on Windows CI — one such threshold is enough.)
MIN_SIMULATE_BLOCKS_PER_SEC = float(os.environ.get("FOURSIGHT_PERF_MIN_BLOCK_RATE", "5000"))
# Tessellation is bulk work, so it runs two orders of magnitude faster per segment than blocks do per
# block. Measured 2.5M/sec; the floor sits well below that because its job is to catch a per-segment
# Python loop creeping in, not to track how fast the machine is.
MIN_TESSELLATION_SEGMENTS_PER_SEC = float(
    os.environ.get("FOURSIGHT_PERF_MIN_TESSELLATION_RATE", "400000")
)
# Purely a runaway guard: an order of magnitude above the ~5 s measured locally, so it fires only for
# a catastrophic regression and never for a slow runner. T2.13 owns the real gate on D5 hardware.
MAX_GATE_SECONDS = float(os.environ.get("FOURSIGHT_PERF_MAX_GATE_SECONDS", "60.0"))
GATE_LINES = 100_000
# How much per-item cost may grow between a small and a 10x larger input before the growth counts as
# non-linear. These tests exist to catch *quadratic* behaviour, which at 10x input would show as ~10x
# per-item growth — so the bound has to leave room for everything that legitimately gets worse with
# size (allocator pressure, GC, cache misses) without swallowing the pathology.
#
# It was 2.0, and ubuntu-py3.11 measured **2.07** on a shared runner where this machine gives 1.27-1.35.
# That threshold also contradicted these tests' own docstrings, which say they are looking for an
# order-of-magnitude change: 2.0 is not one. Third time a threshold calibrated here turned out to be a
# coin flip on CI hardware, so this one is deliberately generous and overridable.
MAX_NONLINEARITY_RATIO = float(os.environ.get("FOURSIGHT_PERF_MAX_NONLINEARITY", "4.0"))

# A realistic mix rather than one repeated line: motion with and without a feed, arcs, rapids,
# rotary moves, an M-code block and a comment. A file of identical lines would flatter the modal
# resolver, which is at its fastest when nothing changes.
_PATTERN = (
    "N{n} G1 X{x:.4f} Y{y:.4f} Z{z:.4f} F1200",
    "N{n} G1 X{x:.4f} Y{y:.4f}",
    "N{n} G2 X{x:.4f} Y{y:.4f} I0.5 J-0.5",
    "N{n} G0 Z5.0",
    "N{n} G1X{x:.4f}Y{y:.4f}A{a:.3f}",
    "N{n} M8",
    "( pass {n} )",
)


def generate(lines: int) -> str:
    """A deterministic synthetic program of `lines` lines."""
    out = []
    for index in range(lines):
        template = _PATTERN[index % len(_PATTERN)]
        out.append(
            template.format(n=index, x=index * 0.01, y=index * 0.02, z=index * 0.003, a=index * 0.1)
        )
    return "\n".join(out) + "\n"


def fastest(call, repeats: int = REPEATS) -> float:
    """Shortest elapsed time over `repeats` runs, after one warmup."""
    call()
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        best = min(best, time.perf_counter() - started)
    return best


def rate(lines: int, seconds: float) -> float:
    return lines / seconds


@pytest.fixture(scope="module")
def large_text() -> str:
    """Generated once for the module: building it five times cost more than the measurements did."""
    return generate(LARGE)


# --------------------------------------------------------------------------- the plan's target


def test_parse_rate_meets_the_plan(large_text: str) -> None:
    """The whole parse — tokenize plus resolve — at or above 50k lines/sec."""
    text = large_text
    elapsed = fastest(lambda: parse(text))
    measured = rate(LARGE, elapsed)
    record_measurement(
        f"  parse (tokenize+resolve): {measured:>9,.0f} lines/sec "
        f"({elapsed / LARGE * 1e6:5.2f} us/line, floor {MIN_PARSE_RATE:,.0f})"
    )
    assert measured >= MIN_PARSE_RATE, (
        f"parse rate {measured:,.0f} lines/sec is below the {MIN_PARSE_RATE:,.0f} floor "
        f"PLAN.md requires ({elapsed / LARGE * 1e6:.2f} us/line)"
    )


def test_tokenize_rate_is_recorded(large_text: str) -> None:
    """Informational: the tokenizer's share of the budget, so a regression can be localized.

    Asserted only against the same floor — a tokenizer slower than the whole-parse target could not
    possibly leave room for the resolver.
    """
    text = large_text
    elapsed = fastest(lambda: tokenize(text))
    measured = rate(LARGE, elapsed)
    record_measurement(
        f"  tokenize only:            {measured:>9,.0f} lines/sec "
        f"({elapsed / LARGE * 1e6:5.2f} us/line)"
    )
    assert measured >= MIN_PARSE_RATE


# --------------------------------------------------------------------------- shape of the cost


def test_parse_time_is_linear_in_file_size() -> None:
    """Guards against accidental quadratic behaviour, which a rate test alone would not catch.

    A parser that rescans or copies accumulated state per line still looks fast on a small file. The
    per-line cost at 50k lines must stay within `MAX_NONLINEARITY_RATIO` of the cost at 5k — a wide
    allowance on purpose, since this is looking for quadratic behaviour, not jitter. Quadratic at 10x
    input would show as roughly 10x per-line growth; a shared CI runner legitimately reaches 2x.
    """
    small, large = 5_000, LARGE
    per_line_small = fastest(lambda: parse(generate(small))) / small
    per_line_large = fastest(lambda: parse(generate(large))) / large
    ratio = per_line_large / per_line_small
    record_measurement(
        f"  per-line cost {large:,} vs {small:,}: {ratio:.2f}x "
        f"({per_line_small * 1e6:.2f} -> {per_line_large * 1e6:.2f} us/line)"
    )
    assert ratio < MAX_NONLINEARITY_RATIO, (
        f"per-line parse cost grew {ratio:.2f}x from {small:,} to {large:,} lines "
        f"(bound {MAX_NONLINEARITY_RATIO:.1f}x), which suggests the parse is not linear in file size"
    )


def test_modal_state_sharing_holds_at_scale(large_text: str) -> None:
    """The parse-rate target rests on copy-on-write `ModalState`; this is what keeps it honest.

    A regression here would not fail the rate test on a fast machine, but it would allocate one
    frozen dataclass per line and quietly eat the budget on a slower one.
    """
    result = parse(large_text)
    distinct = len({id(command.modal_snapshot) for command in result.commands})
    record_measurement(
        f"  distinct ModalState objects: {distinct} for {len(result.commands):,} commands"
    )
    assert distinct <= 8, (
        f"{distinct} distinct ModalState objects for {len(result.commands):,} commands: "
        "copy-on-write sharing has regressed"
    )


# --------------------------------------------------------------------------- verification cost


def test_verify_cost_is_recorded(large_text: str) -> None:
    """Informational only — PLAN.md sets no verification-rate target, so none is invented here.

    Recorded because 23 rules each walking the command list is the obvious place for a future
    slowdown, and a number in the log is what makes that visible when it happens.
    """
    result = parse(large_text)
    profile = load_profile(default_profile_path())
    program = Program(commands=result.commands, profile=profile, parse_errors=result.errors)
    elapsed = fastest(lambda: verify(program), repeats=2)
    record_measurement(
        f"  verify ({len(result.commands):,} commands): {elapsed * 1e3:.1f} ms "
        f"({elapsed / len(result.commands) * 1e6:.2f} us/command, no target set)"
    )
    assert elapsed > 0.0


# =================================================================== T2.11 segment budget + memory


def rotary_program(blocks: int) -> str:
    """A tessellation-heavy program: each block sweeps A a full turn, becoming ~158 segments.

    This is how PLAN.md's 500k-segment target is actually reached — *not* by a 500k-line file. One
    `G1 X.. A360` block produces hundreds of segments, so a few thousand lines suffice.
    """
    body = "".join(f"G1 X{index % 50} A{(index + 1) * 360} F600\n" for index in range(blocks))
    return "G21 G94 G90\n" + body


def simulate_text_timed(text: str, profile):
    """Parse then simulate, returning the simulation and both elapsed times."""
    started = time.perf_counter()
    commands = parse(text).commands
    parse_seconds = time.perf_counter() - started
    started = time.perf_counter()
    sim = simulate(commands, profile)
    return sim, parse_seconds, time.perf_counter() - started, commands


def peak_rss_mb() -> float | None:
    """Peak resident set size, or None where the platform will not tell us.

    `resource` is Unix-only and Windows is in the CI matrix, so this is informational and never
    asserted on. PLAN.md § Performance Requirements records total resident rather than capping it,
    precisely because the fixed component is platform- and driver-dependent.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@pytest.fixture(scope="module")
def profile():
    return load_profile(default_profile_path())


# --------------------------------------------------------------------------- the memory budget


def test_a_500k_segment_program_stays_within_the_geometry_budget(profile) -> None:
    """The budget PLAN.md actually sets, measured through the real pipeline rather than a built store.

    `test_segments.py` already checks a `SegmentBuilder` filled directly. This is the different and
    stronger claim: that *simulating a program* to the 500k target costs no more than that, so no
    per-block bookkeeping or duplicated column has crept in between the parser and the store.
    """
    sim, _, _, _ = simulate_text_timed(rotary_program(3165), profile)
    count = len(sim.store)
    assert count >= TARGET_SEGMENTS, f"the generator produced only {count:,} segments"

    megabytes = sim.store.nbytes() / 1e6
    per_500k = megabytes / count * TARGET_SEGMENTS
    resident = peak_rss_mb()
    record_measurement(
        f"  geometry at {count:,} simulated segments: {megabytes:.1f} MB "
        f"({sim.store.nbytes() / count:.0f} B/segment, {per_500k:.1f} MB per 500k, "
        f"budget {MAX_GEOMETRY_MB_PER_500K:.0f} MB)"
        + (f"; peak RSS {resident:.0f} MB" if resident else "")
    )
    assert per_500k < MAX_GEOMETRY_MB_PER_500K, (
        f"{per_500k:.1f} MB of geometry per 500k segments exceeds the "
        f"{MAX_GEOMETRY_MB_PER_500K:.0f} MB budget in PLAN.md § Performance Requirements"
    )


def test_geometry_cost_per_segment_does_not_grow_with_program_size(profile) -> None:
    """Bytes per segment must be a constant, or the 500k figure cannot be extrapolated from.

    What this catches is a **size-dependent** cost: an extra column populated only for large programs
    is the realistic shape, and it fails here at 125 B/segment against 77 while every small-program
    number stays correct.

    What it explicitly does *not* catch — checked, not assumed — is untrimmed capacity. `len(store)`
    is derived from the arrays, so a store padded to the next power of two reports both the padding as
    segments and an unchanged 77 B/segment. That failure is real (phantom segments at the origin would
    be *drawn*), but it is caught by the goldens and by `test_segments.py`, not by any size check.
    """
    small, _, _, _ = simulate_text_timed(rotary_program(300), profile)
    large, _, _, _ = simulate_text_timed(rotary_program(3000), profile)
    small_cost = small.store.nbytes() / len(small.store)
    large_cost = large.store.nbytes() / len(large.store)
    record_measurement(
        f"  bytes/segment: {small_cost:.1f} at {len(small.store):,} -> "
        f"{large_cost:.1f} at {len(large.store):,}"
    )
    assert large_cost == pytest.approx(small_cost, rel=0.05), (
        "per-segment memory cost grew with program size; the store is retaining something per block"
    )


def test_the_store_holds_exactly_the_columns_plan_specifies(profile) -> None:
    """The memory budget is only meaningful if it is measuring all of the geometry.

    `nbytes()` summing a subset of the arrays would report a comfortable figure for a store that had
    quietly grown a seventh column.
    """
    sim, _, _, _ = simulate_text_timed(rotary_program(50), profile)
    store = sim.store
    counted = sum(
        array.nbytes for array in (store.lin, store.rot, store.kind, store.line, store.duration)
    )
    assert store.nbytes() == counted, (
        f"nbytes() reports {store.nbytes():,} but the five columns total {counted:,}: "
        "a column has been added without being budgeted for"
    )
    assert store.lin.dtype == np.float64 and store.rot.dtype == np.float64


# --------------------------------------------------------------------------- the segment budget


def test_segment_count_is_driven_by_tessellation_not_line_count(profile) -> None:
    """PLAN.md § Performance: "the 100k-line and 500k-segment targets are different axes."

    One rotary block outweighs a hundred ordinary ones. A change that made segment count track line
    count would mean tessellation had stopped adapting to the chord tolerance.
    """
    one_block, _, _, _ = simulate_text_timed("G21 G94 G90\nG1 X10 A360 F600\n", profile)
    hundred_lines, _, _, _ = simulate_text_timed(
        "G21 G94 G90\n" + "".join(f"G1 X{i} Y{i} F600\n" for i in range(1, 101)), profile
    )
    record_measurement(
        f"  1 rotary block -> {len(one_block.store)} segments; "
        f"100 linear lines -> {len(hundred_lines.store)} segments"
    )
    assert len(one_block.store) > len(hundred_lines.store), (
        "a single full-turn rotary block should tessellate into more segments than 100 straight "
        "moves produce; segment count appears to track line count now"
    )


def test_tessellation_respects_the_chord_tolerance_budget(profile) -> None:
    """Segment count must fall as the tolerance loosens, or tolerance is not driving tessellation.

    A fixed step count would satisfy every geometric test in the suite while ignoring the profile.
    """
    from dataclasses import replace

    coarse = replace(profile, tolerance=replace(profile.tolerance, rotary_chord=0.1))
    fine, _, _, _ = simulate_text_timed("G21 G94 G90\nG1 X10 A360 F600\n", profile)
    loose, _, _, _ = simulate_text_timed("G21 G94 G90\nG1 X10 A360 F600\n", coarse)
    record_measurement(
        f"  rotary_chord 0.01 -> {len(fine.store)} segments; 0.1 -> {len(loose.store)} segments"
    )
    assert len(loose.store) < len(fine.store), (
        "a 10x looser chord tolerance produced no fewer segments; tessellation is not adaptive"
    )


def test_per_fixture_segment_counts_are_recorded(profile) -> None:
    """The per-fixture table T2.11 asks for. Exact counts are pinned by the goldens, not here.

    Recorded rather than asserted because duplicating `tests/golden/segments.json` would mean two
    places to update for every intended tessellation change — and the golden is the better of the
    two, since it fails with a diff.
    """
    counts = {}
    for path in sorted(FIXTURES.glob("*.nc")):
        sim, _, _, _ = simulate_text_timed(fixture_text(path.name), profile)
        counts[path.name] = len(sim.store)
    record_measurement(
        f"  fixture segment counts ({len(counts)} files, total {sum(counts.values())}):"
    )
    for name, count in counts.items():
        record_measurement(f"      {count:>6,}  {name}")
    # Meaningful because a fixture can legitimately be *suppressed* down to very few segments but
    # never to none: every fixture in the corpus contains drawable motion. Zero would mean the
    # pipeline had stopped producing geometry for that file while this table still looked plausible.
    empty = [name for name, count in counts.items() if count == 0]
    assert not empty, f"these fixtures produced no geometry at all: {empty}"


# --------------------------------------------------------------------------- the cost of simulating


def test_simulate_rate_meets_the_block_floor(profile) -> None:
    """The floor that keeps the simulate cost from decaying, expressed per *block*.

    Per block and not per segment or per line, because that is what the measurements show the cost
    scales with — see the module docstring.
    """
    text = generate(LARGE)
    commands = parse(text).commands
    elapsed = fastest(lambda: simulate(commands, profile), repeats=1)
    blocks_per_sec = len(commands) / elapsed
    record_measurement(
        f"  simulate: {blocks_per_sec:>9,.0f} blocks/sec "
        f"({elapsed / len(commands) * 1e6:5.1f} us/block, floor {MIN_SIMULATE_BLOCKS_PER_SEC:,.0f})"
    )
    assert blocks_per_sec >= MIN_SIMULATE_BLOCKS_PER_SEC, (
        f"simulate rate {blocks_per_sec:,.0f} blocks/sec is below the "
        f"{MIN_SIMULATE_BLOCKS_PER_SEC:,.0f} floor"
    )


def test_tessellation_sustains_its_segment_throughput(profile) -> None:
    """Tessellation must stay vectorized: segments are produced in bulk, never one at a time.

    Deliberately phrased as a floor on segment throughput rather than as a *ratio* against the
    line-heavy program. The ratio is the T2.11 finding and is recorded below, but asserting on it
    would make this test fail the day someone fixes the per-block overhead — a test that punishes an
    improvement is worse than no test. What genuinely must not regress is this: a `SegmentBuilder`
    appending per segment, or an `_arc` looping in Python, would drop this number through the floor.
    """
    dense, _, dense_seconds, dense_commands = simulate_text_timed(rotary_program(3165), profile)
    sparse, _, sparse_seconds, sparse_commands = simulate_text_timed(generate(LARGE), profile)
    dense_rate = len(dense.store) / dense_seconds
    sparse_rate = len(sparse.store) / sparse_seconds
    record_measurement(
        f"  {dense_rate:>11,.0f} segments/sec over {len(dense_commands):,} dense blocks vs "
        f"{sparse_rate:>9,.0f} over {len(sparse_commands):,} sparse blocks "
        f"({dense_rate / sparse_rate:.0f}x -- the cost is per block, not per segment)"
    )
    assert dense_rate >= MIN_TESSELLATION_SEGMENTS_PER_SEC, (
        f"tessellation produced only {dense_rate:,.0f} segments/sec, below the "
        f"{MIN_TESSELLATION_SEGMENTS_PER_SEC:,.0f} floor; something in the interpolation or the "
        "builder is no longer working in bulk"
    )


def test_simulate_time_is_linear_in_block_count(profile) -> None:
    """Guards accidental quadratic behaviour, which the rate test alone would not catch."""
    small, large = 5_000, LARGE
    small_commands = parse(generate(small)).commands
    large_commands = parse(generate(large)).commands
    per_block_small = fastest(lambda: simulate(small_commands, profile), repeats=1) / small
    per_block_large = fastest(lambda: simulate(large_commands, profile), repeats=1) / large
    ratio = per_block_large / per_block_small
    record_measurement(
        f"  per-block cost {large:,} vs {small:,}: {ratio:.2f}x "
        f"({per_block_small * 1e6:.1f} -> {per_block_large * 1e6:.1f} us/block)"
    )
    assert ratio < MAX_NONLINEARITY_RATIO, (
        f"per-block simulate cost grew {ratio:.2f}x from {small:,} to {large:,} lines "
        f"(bound {MAX_NONLINEARITY_RATIO:.1f}x), which suggests the simulation is not linear in "
        "block count"
    )


def test_the_m2_gate_budget_is_recorded(profile) -> None:
    """T2.13's gate is "a 100k-line file parses and renders within 5 s". This is its input half.

    Recorded, with only a runaway ceiling asserted: the gate itself is a D5-hardware measurement and
    a wall-clock assertion here would be flaky on a shared runner. The point of measuring it now is
    that the gate's budget is spent *before* any rendering happens, which is worth knowing while
    there is still time to act on it.
    """
    sim, parse_seconds, sim_seconds, commands = simulate_text_timed(generate(GATE_LINES), profile)
    total = parse_seconds + sim_seconds
    resident = peak_rss_mb()
    record_measurement(
        f"  M2 gate input: {GATE_LINES:,} lines -> {len(commands):,} blocks, "
        f"{len(sim.store):,} segments in {total:.2f} s "
        f"(parse {parse_seconds:.2f} s + simulate {sim_seconds:.2f} s); "
        f"geometry {sim.store.nbytes() / 1e6:.1f} MB"
        + (f", peak RSS {resident:.0f} MB" if resident else "")
    )
    assert total < MAX_GATE_SECONDS, (
        f"{GATE_LINES:,} lines took {total:.1f} s to parse and simulate, past the "
        f"{MAX_GATE_SECONDS:.0f} s runaway guard"
    )


def test_the_generator_produces_the_requested_size(large_text: str) -> None:
    """A generator quietly producing 100 lines would make every measurement above meaningless."""
    text = large_text
    assert len(text.splitlines()) == LARGE
    result = parse(text)
    assert not result.errors, [error.message for error in result.errors[:3]]
    # Six of every seven lines carry motion; the seventh is a comment.
    assert len(result.commands) == LARGE - LARGE // len(_PATTERN)
