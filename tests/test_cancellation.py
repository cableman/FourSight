"""Cancellable simulation (T2.9). **No Qt** — this is the half that matters and it is Qt-free.

`simulate` takes `cancelled() -> bool`, which a `threading.Event.is_set` satisfies exactly, so the
threading lives entirely in `gui/background.py` and the behaviour worth asserting lives here.

The load-bearing decision is that **a cancelled run raises rather than returning a partial
`Simulation`**. A half-stepped program is a truncated toolpath, and handing one back invites a caller
to draw it as though the program ended there — the confidently-wrong picture the plan exists to
prevent. There is no honest way to render "the first 40% of this program", so the caller gets nothing
and keeps whatever it had.
"""

import threading

import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.fileio.loader import load_text
from foursight.gui.session import open_loaded
from foursight.machine.profile import load_profile
from foursight.parser.resolver import parse
from foursight.sim.simulator import SimulationCancelled, simulate


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def program(blocks: int) -> list:
    """A program of `blocks` motion blocks, long enough to span several progress intervals."""
    body = "".join(f"G1 X{index % 50} Y{index % 30} F600\n" for index in range(blocks))
    return parse(f"G21 G90 G94\n{body}").commands


# --------------------------------------------------------------------------- cancelling


def test_cancelling_raises_rather_than_returning_a_partial_simulation(profile) -> None:
    """The decision this whole design rests on. A truncated store must not be returnable."""
    with pytest.raises(SimulationCancelled):
        simulate(program(9000), profile, cancelled=lambda: True, progress_interval=100)


def test_the_cancellation_message_says_how_far_it_got(profile) -> None:
    """Useful in a log, and it proves cancellation happened mid-run rather than before it started."""
    with pytest.raises(SimulationCancelled, match=r"cancelled after 100 of 9,00[01] blocks"):
        simulate(program(9000), profile, cancelled=lambda: True, progress_interval=100)


def test_a_cancel_partway_through_stops_early(profile) -> None:
    """Cancelling must actually shorten the work, not merely discard the result at the end."""
    seen = []

    def stop_after_two_ticks(done: int, total: int) -> None:
        seen.append(done)

    flag = threading.Event()

    def cancelled() -> bool:
        if len(seen) >= 2:
            flag.set()
        return flag.is_set()

    with pytest.raises(SimulationCancelled):
        simulate(
            program(9000),
            profile,
            progress=stop_after_two_ticks,
            cancelled=cancelled,
            progress_interval=100,
        )
    assert len(seen) < 30, f"kept working after cancellation: {len(seen)} progress ticks"


def test_not_cancelling_completes_normally(profile) -> None:
    """The control case: a `cancelled` that never fires must change nothing."""
    with_check = simulate(program(500), profile, cancelled=lambda: False)
    without = simulate(program(500), profile)
    assert len(with_check.store) == len(without.store) > 0


def test_no_cancel_callback_at_all_still_works(profile) -> None:
    """`cancelled=None` is the CLI's path and must not be a special case that rots."""
    assert len(simulate(program(200), profile).store) > 0


def test_a_cancel_arriving_in_the_final_partial_interval_is_honoured(profile) -> None:
    """Otherwise a cancel in the last few blocks reports a *completed* simulation.

    The loop only polls on the interval, so 250 blocks at an interval of 100 leaves 50 unpolled. Without
    the check after the loop, a cancel during those 50 would be silently ignored.
    """
    commands = program(250)
    fired = threading.Event()

    def cancelled() -> bool:
        return fired.is_set()

    def progress(done: int, total: int) -> None:
        if done >= 200:
            fired.set()

    with pytest.raises(SimulationCancelled):
        simulate(commands, profile, progress=progress, cancelled=cancelled, progress_interval=100)


# --------------------------------------------------------------------------- progress


def test_progress_reports_monotonically_up_to_the_total(profile) -> None:
    reports = []
    simulate(
        program(1000), profile, progress=lambda d, t: reports.append((d, t)), progress_interval=100
    )
    dones = [done for done, _ in reports]
    assert dones == sorted(dones)
    assert all(total == len(program(1000)) for _, total in reports)
    assert dones[-1] == reports[-1][1], "the final report must reach the total"


def test_progress_always_reaches_the_total_even_when_it_is_not_a_multiple(profile) -> None:
    """A bar that stops at 90% on most files is worse than no bar."""
    reports = []
    commands = program(1050)
    simulate(commands, profile, progress=lambda d, t: reports.append(d), progress_interval=100)
    assert reports[-1] == len(commands)


def test_progress_is_not_called_per_command(profile) -> None:
    """The interval exists so the callback cost stays irrelevant; per-command would dominate."""
    calls = []
    simulate(program(2000), profile, progress=lambda d, t: calls.append(d), progress_interval=500)
    assert len(calls) <= 6, f"{len(calls)} progress calls for 2000 blocks at interval 500"


# --------------------------------------------------------------------------- through the session


def test_cancellation_propagates_through_open_loaded(profile) -> None:
    """The GUI enters via `session`, so the exception must survive that layer unchanged."""
    loaded = load_text(fixture_text("baseline_4axis.nc").encode("utf-8"))
    with pytest.raises(SimulationCancelled):
        open_loaded(loaded, profile, cancelled=lambda: True)


def test_a_cancel_during_parsing_does_not_wait_for_the_simulation(profile) -> None:
    """Parsing is ~a quarter of the wall clock and has no progress seam of its own.

    Without the check between the two phases, a user who cancels during a long parse would still sit
    through the entire simulation before anything happened.
    """
    loaded = load_text(fixture_text("baseline_4axis.nc").encode("utf-8"))
    simulate_calls = []

    with pytest.raises(SimulationCancelled, match="after parsing"):
        open_loaded(
            loaded,
            profile,
            cancelled=lambda: True,
            progress=lambda d, t: simulate_calls.append(d),
        )
    assert simulate_calls == [], "simulation started despite a cancel during parsing"


def test_progress_propagates_through_open_loaded(profile) -> None:
    loaded = load_text(fixture_text("arc_helical.nc").encode("utf-8"))
    reports = []
    open_loaded(
        loaded,
        profile,
        progress=lambda d, t: reports.append(d),
    )
    assert reports, "no progress reported through the session layer"
