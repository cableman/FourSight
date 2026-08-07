# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**M0–M5 are complete.** `src/`, `tests/` and `pyproject.toml` all exist; the suite is **1157 tests** and CI
is green on Ubuntu and Windows for py3.11 and py3.12. The two open items are **T0.8/T0.9** — launching the
PyInstaller bundle on a clean Windows VM, which needs a VM — and `--windowed` has never been exercised.

`PLAN.md` remains the single source of truth for the design: architecture, tech stack, G-code subset,
verifier rules, milestones, and the reasoning behind every decision including the ones that were reversed.

`TASKS.md` is the execution layer: ordered tasks with blockers and done-criteria, derived from `PLAN.md`'s milestones. **PLAN.md owns the design; TASKS.md owns the order of work.** If the two disagree, PLAN.md wins and the task is wrong. Its `Open decisions` section lists the questions that block later milestones — the M0 spikes exist to answer them.

Read `PLAN.md` before starting any task, then pick up work from `TASKS.md` in ID order. When a change alters the design (new dependency, new module, changed data model), update `PLAN.md` in the same change, and tick the task in `TASKS.md`.

## Commands

**All Python runs inside the project venv at `.venv/`.** Never invoke bare `python`, `pip`, `pytest`, or `ruff` — they may resolve to system Python. Call the venv binaries directly (shown below), or activate first with `source .venv/bin/activate`. On Windows the binaries live in `.venv\Scripts\`.

The venv runs **CPython 3.12.10**. `PLAN.md` requires 3.11+ (`tomllib` is stdlib only from 3.11), and bare `python3` on this machine is **3.10** — so never bootstrap with plain `python3`. The system's `/usr/bin/python3.11` is `3.11.0rc1`, a release candidate, and is **actively broken for this purpose**: `python3.11 -m venv` fails in `ensurepip`, so the venv is created without pip and nothing can be installed into it. Verified, not assumed. Do not reach for it.

If `.venv/` does not exist, create it before doing anything else, using an explicit 3.11+ interpreter. A uv-managed CPython 3.12.10 is already cached locally at `~/.local/share/uv/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12`:

```bash
~/.local/share/uv/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Verify with `.venv/bin/python --version` before proceeding; if it reports 3.10, delete `.venv/` and start again.

These are the commands `PLAN.md` mandates; they become runnable once `pyproject.toml` and the package exist.

```bash
.venv/bin/pytest -q                            # after every change to parser, sim, verify, or fix
.venv/bin/pytest tests/test_arcs.py::test_name # single test
.venv/bin/ruff check --fix . && .venv/bin/ruff format .   # before finishing any task; zero errors
.venv/bin/python scripts/build.py              # PyInstaller one-dir build for the current OS
```

Planned CLI (M1, headless — no Qt needed):

```bash
.venv/bin/foursight parse file.nc    # dump parsed commands
.venv/bin/foursight check file.nc    # run the verifier
```

## Architecture

The pipeline is strictly one-directional, and each stage is a separate package under `src/foursight/`:

```
text → parser/ → Command objects → sim/ → SegmentStore → gui/ viewport
                       ↓                        ↓
                    verify/ → Diagnostics → fix/ → unified diff
```

Dependency direction is `parser → machine → sim → verify → fix → gui`. `parser/model.py` must not import from `machine/`.

- **`parser/`** — custom tokenizer and parser (deliberately *not* pygcode, because modal-state control must be owned by us). Resolves modal groups so each `Command` carries a frozen `modal_snapshot`.
- **`machine/`** — `MachineState` (live position and modal groups during simulation), `MachineProfile` loaded from TOML, and `kinematics.py` for rotary transforms.
- **`sim/`** — interpolates lines and arcs (G2/G3 in both IJK and R form) plus rotary blending into a `SegmentStore`; `timing.py` derives per-segment durations for the timeline.
- **`verify/`** — `Rule` base class with a registry; one module per check category under `checks/`; emits `Diagnostic(severity, line, message, fix_ids)`.
- **`fix/`** — each fix is a transform returning a text diff. Fixes never write the original file; they modify the editor buffer and the user saves explicitly.
- **`gui/`** — thin Qt/PySide6 layer so logic stays testable. Includes `picking.py`, because batched rendering rules out Qt item picking.
- **`fileio/`** — deliberately not named `io/`, which shadows the stdlib module.

### Invariants

These are the ones that are easy to violate silently. `PLAN.md` has the reasoning.

- **Segments are stored columnar, never as per-object dataclasses.** `SegmentStore` holds parallel numpy arrays (`lin`, `rot`, `kind`, `line`, `duration`). Per-object segments blow the memory budget ~6× and force a repack before every GL upload.
- **Rotary lives in its own column, out of the position vector.** Never take a norm across linear (mm) and rotary (degrees) components — the result is meaningless, and it's exactly what you'd write for a timeline duration. Combine them in `sim/timing.py` as `max(linear_time, rotary_time)`.
- **`lin` is always machine coordinates.** Verification needs machine coords; table-mount display needs part coords. The display transform writes `lin_part` and never mutates `lin`.
- **Every segment traces back to a source line** via `line[i]`. Editor↔viewport sync, diagnostics, and fixes all depend on this.
- **Every module outside `gui/` must import without Qt installed.**
- **All geometry is numpy float64; internal units are always mm.** Convert G20 (inch) input at parse time. But report diagnostics in the program's declared units — "X exceeds 400 mm" against an inch program isn't actionable.
- **G-codes are strings (`'90.1'`), never floats.** A block carries multiple G- and M-words, so they live in `Command.gcodes` / `Command.mcodes` lists, not in the `words` dict.
- **`slots=True` on every hot-path dataclass** — the 50k lines/sec parse target doesn't survive otherwise.
- **`sim/timing.py` has two implementations of the timing rules**, and this is the one duplication in the
  codebase that is deliberate. `_single_segment` is a scalar path for blocks producing one segment (91% of
  them in a realistic program); `_vector_durations` is the general one. **A rule added to one must be added
  to the other.** What makes the duplication safe is
  `test_timing.py::test_the_fast_path_agrees_with_the_vector_path`, which drives both over every rate
  configuration and randomized geometry and demands bit-identical output — do not weaken or skip it. A fast
  path that silently disagreed would produce a wrong time estimate *only for ordinary programs*, which is
  the worst possible distribution for a bug.

### Never render a confidently wrong toolpath

The governing principle. A previewer that refuses to draw is recoverable; one that draws the wrong path is worse than no previewer.

Diagnostics have three tiers, and the distinction matters:

- **error** — malformed, or would break the machine.
- **unsupported** — well-formed, recognized, and **affects motion**, but not interpreted by v1 (cutter compensation, canned cycles G80–G89). The affected span is marked or suppressed, never drawn as if understood. Under an active G81, a block containing only `X10 Y10` is a drill cycle, not a linear move.
- **warning** — suspicious, or unrecognized but inert. Rendered normally.

An unrecognized code that never touches position is a warning. One that changes how subsequent motion is interpreted is `unsupported`, never a warning.

### Rotary kinematics

For `rotary_mount = "table"` (part rotates), transform tool positions into part coordinates:

```
p_part = R_axis(-A) @ (p_tool - centerline) + centerline
```

For `rotary_mount = "head"`, the tool **tip translates as the head swings** — it is not simply the machine XYZ:

```
p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)
```

Simultaneous XYZ+A moves **must** be interpolated in small parameter steps and transformed per step. Endpoint-only transformation renders helical/wrapped toolpaths as straight chords — the most likely source of silently wrong output. Step size comes from `tolerance.rotary_chord`, not a fixed count.

### Performance constraints

These shape the design, not just later optimization passes:

- Parse ≥ 50k lines/sec (~20 µs/line).
- Render 500k+ segments interactively: pre-batch into ≤ 10 GL buffers grouped by `kind`. Never one draw call per move.
- **Memory: geometry + GL buffers ≤ 55 MB per 500k segments** (measured 50.5 MB: 38.5 MB float64 store plus
  a 12.0 MB float32 GL copy). The original "≤ 250 MB resident" was **not achievable and never was** — the
  fixed Python + Qt + Mesa baseline alone is 233 MB with 1k segments on screen, so a total-RSS cap measures
  the interpreter and the GL driver rather than our data model. Total resident is recorded, never asserted.
- Simulation runs off the GUI thread (QThread) with progress reporting.

Segment count is driven by tessellation, not line count — one `G1 X100 A360` block can become 1000+ segments.

Two rendering facts, both settled by the T0.7 spike and re-verifiable with `spikes/render_500k.py
--introspect-only`:

- **`GLLinePlotItem` has no dash or stipple parameter** — confirmed. "Rapids dashed" would need dashes baked
  into geometry, so the distinction is **colour-only**, and nothing may depend on `glLineWidth`: pyqtgraph
  skips that call entirely on core forward-compatible profiles, where `width=` is silently inert.
- **pyqtgraph 0.14 does *not* use the legacy fixed-function path** — this file previously claimed it did,
  which was written against an older version and was wrong. It draws through a shader program with
  persistent VBOs and re-uploads only on a dirty flag. **D1 is resolved: pyqtgraph holds**, measured at
  376.9 fps for 500k segments against a 30 fps requirement, so no raw `QOpenGLWidget` is needed.

These answers are version-specific. Re-run the spike's introspection on every pyqtgraph upgrade.

## Windows differs, and it will catch you

The CI matrix has caught **four Windows-only defects**, three of them introduced by a change that looked
platform-neutral. Assume anything touching the filesystem or line endings behaves differently there:

- **`os.stat` and `open` describe a missing file differently.** `open` surfaces the CRT's
  "No such file or directory"; `os.stat` surfaces the Win32 "The system cannot find the file specified".
  A size check that stats unconditionally changes an error message on Windows alone.
- **Scripts do not live beside `python.exe`.** They are in `Scripts\`, one level below it, so
  `Path(sys.executable).parent` finds them on a Linux venv and not on Windows. Use
  `sysconfig.get_path("scripts")`.
- **Git checks files out with CRLF**, and `QPlainTextEdit.setPlainText` normalizes to LF — so a buffer is
  not byte-identical to the file it came from. `.gitattributes` pins the fixtures; `LoadedFile.newline`
  is what restores a file's endings on save.
- **Skips hide platform bugs.** A test that quietly skips on one platform is worse than one that fails, so
  the matrix job runs `pytest -rs` and prefers an assertion over a `pytest.skip`.

Shared runners are also roughly half the speed of this machine, which is why the perf floors are
overridable and set from `PLAN.md`'s requirement rather than from local measurement.

## Conventions

- Every Python invocation goes through `.venv/` — see Commands. This includes one-off checks and throwaway scripts, not just the mandated commands.
- Distribution name is `foursight-cnc` (plain `foursight` is taken on PyPI); the import package is `foursight`.
- LinuxCNC is the normative dialect; Fanuc-isms are documented deviations in `PLAN.md`.
- No new third-party dependencies without updating the Tech Stack table in `PLAN.md`.
- Prefer dataclasses over dicts for structured data; keep functions under ~50 lines.
- Applying a fix invalidates every line number. The contract is **one fix → full re-parse → re-verify → rebuild segments**. No batch application, no diff rebasing.
- Ruff config lives in `pyproject.toml` — line length 100, target py311, `select = ["E", "F", "W", "I", "N", "UP", "B", "SIM", "NPY", "S"]`.

## Testing

- Parser and verifier are pure functions and must be exhaustively unit-tested against fixture `.nc` files.
- Broken fixtures are **baseline plus one mutation**: keep one known-clean file, derive each broken fixture by a single change, and assert on the diagnostic added relative to the baseline's set. "Exactly one diagnostic per file" is brittle — a file missing G21 also trips "no work offset" and "lacks M30."
- Arc math: hypothesis property tests — interpolated points equidistant from center within 1e-6, tessellation within `tolerance.arc_chord`.
- Kinematics: compare transformed paths against closed-form expectations (helix on a cylinder).
- Golden tests hash `SegmentStore` geometry per fixture — the main net against refactors that silently move the toolpath.
- `test_perf.py` asserts parse rate and per-fixture segment counts; the plan's hard numbers decay without it.
- GUI is verified by a manual test script per milestone, so keep the GUI layer thin.
