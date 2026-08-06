"""Performance regression tests (T1.12).

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

Segment counts and the memory budget belong to T2.11, once `SegmentStore` exists.
"""

import os
import time

import pytest

from conftest import record_measurement
from foursight.machine.profile import default_profile_path, load_profile
from foursight.parser.resolver import parse
from foursight.parser.tokenizer import tokenize
from foursight.verify.rules import Program, verify

# PLAN.md § Performance Requirements. Overridable for a runner that cannot reach it, but not
# silently: the default is the plan's number.
MIN_PARSE_RATE = float(os.environ.get("FOURSIGHT_PERF_MIN_RATE", "50000"))

REPEATS = 3
LARGE = 50_000

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
    per-line cost at 50k lines must stay close to the cost at 5k; a wide allowance is deliberate,
    since this is looking for an order-of-magnitude change, not jitter.
    """
    small, large = 5_000, LARGE
    per_line_small = fastest(lambda: parse(generate(small))) / small
    per_line_large = fastest(lambda: parse(generate(large))) / large
    ratio = per_line_large / per_line_small
    record_measurement(
        f"  per-line cost {large:,} vs {small:,}: {ratio:.2f}x "
        f"({per_line_small * 1e6:.2f} -> {per_line_large * 1e6:.2f} us/line)"
    )
    assert ratio < 2.0, (
        f"per-line parse cost grew {ratio:.2f}x from {small:,} to {large:,} lines, "
        "which suggests the parse is not linear in file size"
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


def test_the_generator_produces_the_requested_size(large_text: str) -> None:
    """A generator quietly producing 100 lines would make every measurement above meaningless."""
    text = large_text
    assert len(text.splitlines()) == LARGE
    result = parse(text)
    assert not result.errors, [error.message for error in result.errors[:3]]
    # Six of every seven lines carry motion; the seventh is a comment.
    assert len(result.commands) == LARGE - LARGE // len(_PATTERN)
