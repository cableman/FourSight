"""Golden simulator-output tests (T2.10).

PLAN.md § Testing Strategy calls this "the highest-value regression net for a geometry engine —
refactors that silently move the toolpath are otherwise invisible." Nothing else in the suite would
notice an arc shifting by a millimetre, a rapid becoming a feed, or a segment losing its source line.

**A bare hash is a poor golden**: it tells you something changed and nothing about what. So each
fixture's fingerprint pairs an exact `geometry_sha256` with readable fields — segment count, per-axis
bounds, duration, rapid/feed split, spans. A failure names the field that moved, and the hash catches
the subtle changes the summary numbers would miss.

Geometry is **quantized before hashing**, to 1 µm and 0.001°. That is 10× finer than the 0.01 mm
chord tolerance, so a real change in the toolpath cannot hide inside it, and about ten orders of
magnitude coarser than float64 noise, so a different libm's `cos` cannot break the build. Windows CI
exercises exactly that.

Run `FOURSIGHT_UPDATE_GOLDEN=1 pytest tests/test_golden.py` to re-record after an *intended* change —
and read the diff before committing it.
"""

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, FIXTURES, fixture_text
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind
from foursight.sim.simulator import Simulation, simulate_text

GOLDEN_PATH = Path(__file__).parent / "golden" / "segments.json"
UPDATE = os.environ.get("FOURSIGHT_UPDATE_GOLDEN") == "1"

# Quantization grids: fine enough that a real geometry change cannot hide, coarse enough that
# platform float noise cannot trip the hash.
LINEAR_GRID_MM = 1e-3
ROTARY_GRID_DEG = 1e-3

ALL_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.nc"))


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def geometry_hash(sim: Simulation) -> str:
    """SHA-256 over the quantized geometry, plus the exact integer columns.

    `kind` and `line` go in unquantized: a rapid reclassified as a feed, or a segment attributed to
    the wrong source line, are regressions every bit as real as a moved coordinate.
    """
    store = sim.store
    digest = hashlib.sha256()
    digest.update(np.round(store.lin / LINEAR_GRID_MM).astype(np.int64).tobytes())
    digest.update(np.round(store.rot / ROTARY_GRID_DEG).astype(np.int64).tobytes())
    digest.update(store.kind.tobytes())
    digest.update(store.line.astype(np.int64).tobytes())
    return digest.hexdigest()[:32]


def fingerprint(sim: Simulation) -> dict:
    """A record of the simulation that a human can read and a machine can compare exactly."""
    store = sim.store
    bounds: dict[str, list[float]] = {}
    if len(store):
        for index, axis in enumerate("XYZ"):
            values = store.lin[:, :, index]
            bounds[axis] = [round(float(values.min()), 3), round(float(values.max()), 3)]
        bounds["A"] = [round(float(store.rot.min()), 3), round(float(store.rot.max()), 3)]
    return {
        "segments": len(store),
        "rapid": int(store.mask(Kind.RAPID).sum()),
        "feed": int(store.mask(Kind.FEED).sum()),
        "bounds_mm_deg": bounds,
        "duration_s": round(sim.duration, 3),
        "unknown_durations": sim.unknown_durations,
        "spans": [
            [span.first_line, span.last_line, "unverified" if span.drawn else "suppressed"]
            for span in sim.spans
        ],
        "notes": len(sim.notes),
        "geometry_sha256": geometry_hash(sim),
    }


def load_golden() -> dict:
    if not GOLDEN_PATH.is_file():
        return {}
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def describe_drift(name: str, expected: dict, actual: dict) -> str:
    """Name the fields that moved, so a failure is diagnosable without a debugger."""
    lines = [f"golden mismatch for {name}:"]
    for key in sorted(set(expected) | set(actual)):
        before, after = expected.get(key, "<absent>"), actual.get(key, "<absent>")
        if before != after:
            lines.append(f"    {key}: {before!r} -> {after!r}")
    lines.append(
        "  If this change is intended, re-record with "
        "FOURSIGHT_UPDATE_GOLDEN=1 pytest tests/test_golden.py -- and read the diff first."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- the golden comparison


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_simulator_output_matches_the_golden(name: str, profile) -> None:
    """The regression net: any change to the toolpath for a committed fixture fails here."""
    if UPDATE:
        pytest.skip("re-recording goldens")
    golden = load_golden()
    assert name in golden, (
        f"no golden recorded for {name}; run FOURSIGHT_UPDATE_GOLDEN=1 pytest tests/test_golden.py"
    )
    sim, _ = simulate_text(fixture_text(name), profile)
    actual = fingerprint(sim)
    assert actual == golden[name], describe_drift(name, golden[name], actual)


def test_the_golden_file_covers_every_fixture() -> None:
    """A fixture added without a golden would be silently unprotected."""
    if UPDATE:
        pytest.skip("re-recording goldens")
    golden = load_golden()
    assert set(golden) == set(ALL_FIXTURES), (
        f"golden/fixture mismatch: missing {sorted(set(ALL_FIXTURES) - set(golden))}, "
        f"stale {sorted(set(golden) - set(ALL_FIXTURES))}"
    )


def test_record_goldens(profile) -> None:
    """Writes the golden file. Runs only under FOURSIGHT_UPDATE_GOLDEN=1."""
    if not UPDATE:
        pytest.skip("set FOURSIGHT_UPDATE_GOLDEN=1 to re-record")
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    recorded = {}
    for name in ALL_FIXTURES:
        sim, _ = simulate_text(fixture_text(name), profile)
        recorded[name] = fingerprint(sim)
    GOLDEN_PATH.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- the net itself works


def test_the_fingerprint_is_stable_across_runs(profile) -> None:
    """A fingerprint that varied run to run would make every golden test flaky."""
    first, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    second, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    assert fingerprint(first) == fingerprint(second)


def test_a_moved_toolpath_changes_the_hash(profile) -> None:
    """The property the net depends on: shifting geometry by more than the grid must be detected."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    before = geometry_hash(sim)
    sim.store.lin[0, 0, 0] += 0.01  # 10x the quantization grid
    assert geometry_hash(sim) != before


def test_noise_below_the_quantization_grid_does_not_change_the_hash(profile) -> None:
    """Why quantization exists: a different libm's `cos` must not fail the build.

    Windows CI runs a different C library from the dev machine, so this is not hypothetical.
    """
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    before = geometry_hash(sim)
    sim.store.lin += 1e-9
    assert geometry_hash(sim) == before


def test_the_grid_is_finer_than_the_chord_tolerance(profile) -> None:
    """A change large enough to matter geometrically must never hide inside the grid."""
    assert profile.tolerance.arc_chord >= LINEAR_GRID_MM * 10


def test_reclassifying_a_rapid_as_a_feed_changes_the_hash(profile) -> None:
    """`kind` is hashed unquantized: motion type is part of the output, not a presentation detail."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    before = geometry_hash(sim)
    rapid = np.flatnonzero(sim.store.kind == Kind.RAPID)
    sim.store.kind[rapid[0]] = Kind.FEED
    assert geometry_hash(sim) != before


def test_losing_a_source_line_changes_the_hash(profile) -> None:
    """Editor sync and fixes both key on `line[i]`, so a wrong attribution is a real regression."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    before = geometry_hash(sim)
    sim.store.line[0] += 1
    assert geometry_hash(sim) != before


def test_the_fingerprint_records_spans_not_just_geometry(profile) -> None:
    """A canned cycle silently becoming drawn would change no coordinate that exists yet."""
    suppressed, _ = simulate_text(fixture_text("canned_cycle_span.nc"), profile)
    assert fingerprint(suppressed)["spans"] == [[9, 12, "suppressed"]]
    unverified, _ = simulate_text(fixture_text("cutter_comp_span.nc"), profile)
    assert fingerprint(unverified)["spans"] == [[9, 11, "unverified"]]


def test_bounds_are_recorded_per_axis_with_rotary_separate(profile) -> None:
    """Rotary in its own field: a bounding box mixing mm and degrees would be meaningless."""
    sim, _ = simulate_text(fixture_text("baseline_4axis.nc"), profile)
    bounds = fingerprint(sim)["bounds_mm_deg"]
    assert set(bounds) == {"X", "Y", "Z", "A"}
    assert bounds["A"] == [0.0, 180.0], "the baseline sweeps A from 0 to 180"


def test_an_empty_simulation_fingerprints_without_bounds(profile) -> None:
    sim, _ = simulate_text("(comment only)\n", profile)
    record = fingerprint(sim)
    assert record["segments"] == 0
    assert record["bounds_mm_deg"] == {}
