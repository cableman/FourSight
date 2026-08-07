# TASKS.md — FourSight implementation task list

Execution layer for `PLAN.md`. **`PLAN.md` owns the design; this file owns the order of work.**
Tasks reference PLAN.md sections rather than restating them — if a task and PLAN.md disagree,
PLAN.md wins and the task is wrong.

## How to use this file

- Work tasks in ID order within a milestone. Cross-milestone order is M0 → M5.
- `Blocked by` is hard: do not start a task whose blockers are open.
- A task is done only when its **DoD** (definition of done) holds, not when the code exists.
- `[ ]` open · `[x]` DoD met · `[~]` work delivered but the DoD is **blocked**, with the reason and
  what remains stated inline. A `[~]` is not done; nothing depending on it should be assumed safe.
- Every task ends with `.venv/bin/ruff check --fix . && .venv/bin/ruff format .` at zero errors,
  and `.venv/bin/pytest -q` green if it touched `parser/`, `sim/`, `verify/`, or `fix/`.
- When a task changes the design (new dependency, new module, changed data model),
  **update `PLAN.md` in the same commit.**
- M2–M5 are deliberately coarser. Two M0 spikes can invalidate Tech Stack choices;
  re-granulate M2+ once `Open decisions` below are resolved.

## Open decisions (blocking, resolved by M0)

Record the answers **in `PLAN.md`**, then tick here.

- [x] **D1 — Does pyqtgraph hold? → YES, with ~12× margin.** Measured by T0.7 on Intel Iris Xe
      (ADL GT2), Mesa 25.1.5: **376.9 fps median** at 500k segments in 10 `GLLinePlotItem`s,
      worst frame 5.6 ms, against a 30 fps requirement. Still 188 fps at 2M segments (4× target).
      **M2 builds on pyqtgraph; the raw `QOpenGLWidget` fallback is dropped**, so T2.5–T2.7 keep
      their original scope. Consistent with the introspection finding that 0.14.0 already draws via
      shaders with persistent VBOs and dirty-flag uploads. Recorded in PLAN.md § Rendering
      Constraints.
- [~] **D2 — Does a bundled GL app launch? → YES on Linux; Windows still open.** T0.8's bundle runs
      on Pop!_OS 22.04: all four imports fine, pyqtgraph's package data intact, a real frame painted
      on Iris Xe, exit 0, and **no `HIDDEN_IMPORTS` were needed**. That retires the "PyInstaller
      cannot bundle this stack at all" risk, which was the larger one. **It does not answer the
      question as posed**: Windows differs in DLL resolution, the VC++ runtime, and AV heuristics,
      and none of the Linux result transfers. Record: Windows version, failure mode (if any), and
      any hook/binary fix applied.
- [x] **D3 — Rapids: dashed or colour-only? → COLOUR-ONLY.** Settled by T0.7's introspection:
      `GLLinePlotItem.setData` accepts exactly `['pos', 'color', 'width', 'mode', 'antialias']`
      and raises on `dash`/`stipple`/`dashPattern`, so no stipple parameter exists at any driver.
      Dashes would have to be baked into geometry (~2× rapid vertices). Not worth it, and
      `width=` is not a fallback either — pyqtgraph skips `glLineWidth` entirely on core
      forward-compatible profiles. **Rapids are distinguished by colour.** Recorded in PLAN.md.
- [x] **D4 — Picking strategy → CPU screen-space distance to the segment, projection cached per
      camera change.** Neither option as framed. Measured in T3.0 (`spikes/picking.py`): **27.3 ms per
      click at 500k segments** warm, against 93.9 ms if it reprojects every time. Midpoints were
      rejected on *accuracy*, not speed — clicking 1 px from a long segment's end, the nearest midpoint
      belongs to a different line 28.7 px away. GPU colour-pick was rejected on cost: a per-vertex
      colour buffer (4 MB at 500k) plus a render pass and readback per click, and it fights the
      one-colour-per-batch design that keeps GL memory at 12 MB. Full reasoning in PLAN.md
      § Picking Strategy.
- [x] **D5 — Baseline hardware → Intel Iris Xe Graphics (ADL GT2), Mesa 25.1.5, Pop!_OS 22.04,
      Python 3.12.10.** The machine T0.7 was measured on; recorded in PLAN.md against both the M0
      spike result and the M2 gate. **Measure that gate with vsync off** — with vsync on, every
      configuration from 1k to 500k segments reports ~60 fps and the gate cannot fail.

---

## M0 — Skeleton and de-risking spikes

Nothing here ships. Two spikes can invalidate the Tech Stack; that is the point of doing them now.

- [x] **T0.1 — Create `.venv/` and `.gitignore`** — *done*
      `.venv/` runs **CPython 3.12.10** (uv-managed local install). Bare `python3` here is 3.10,
      below PLAN.md's floor, and `/usr/bin/python3.11` is `3.11.0rc1` — CLAUDE.md now documents the
      correct bootstrap interpreter and PLAN.md no longer implies bare `python3` is safe.
      `.gitignore` covers the venv, bytecode, `*.egg-info/`, pytest/ruff/hypothesis/coverage caches,
      PyInstaller `build/`+`dist/`+`*.spec`, editor dirs, OS cruft, and
      `.claude/settings.local.json`.
      **DoD met:** `.venv/` is ignored (`git check-ignore` confirms); `git status` shows only
      intended files. Re-verify "clean after a full test run" once a suite exists (T0.5).
      Files: `.gitignore`, `CLAUDE.md`, `PLAN.md`

- [x] **T0.2 — `pyproject.toml`** — *done*
      `foursight-cnc` / import package `foursight`, `requires-python = ">=3.11"`, src-layout,
      setuptools backend. Runtime dep `numpy` only; `[gui]` extra = PySide6 + pyqtgraph + PyOpenGL;
      `[dev]` extra = pytest + hypothesis + ruff + pyinstaller. Console script declared
      (`foursight.cli:main`, unimportable until T1.11). Ruff config per PLAN.md **plus
      `extend-exclude = ["*.md"]`** — ruff formats Python inside Markdown code fences and
      `ruff format .` was rewriting PLAN.md's aligned dataclass snippets; it also hit a
      permission error on `.claude/loop.md`. PLAN.md updated for both this and the `[gui]` extra.
      Installed: numpy 2.5.1, PySide6 6.11.1, pyqtgraph 0.14.0, PyOpenGL 3.1.10, pytest 9.1.1,
      hypothesis 6.165.2, ruff 0.16.1, pyinstaller 6.21.0.
      **DoD met:** `pip install -e ".[dev,gui]"` succeeds; `ruff check .` and `ruff format --check .`
      both exit 0; `import foursight` works; ruff config verified *live* by probe files —
      `I001`/`N806` fire in both trees and `S101` fires in `src/` but is suppressed in `tests/`.
      **DoD corrected:** the original "`pytest -q` exits 0 on an empty suite" is unachievable —
      pytest returns **5** (`EXIT_NOTESTSCOLLECTED`) when nothing is collected. Exit 0 is T0.5's gate.
      Also created ahead of their tasks, minimally: `src/foursight/__init__.py` (an editable
      install needs the package to exist) and `tests/` (`testpaths` needs the dir to exist).
      Blocked by: T0.1
      Files: `pyproject.toml`, `src/foursight/__init__.py`, `tests/.gitkeep`, `PLAN.md`

- [x] **T0.3 — Package skeleton** — *done*
      32 modules across `parser/`, `machine/`, `sim/`, `verify/` + `verify/checks/`, `fix/`,
      `gui/`, `fileio/`, plus dirs `profiles/`, `scripts/`, `tests/fixtures/`. `fileio`, never `io`.
      Stubs are docstring-only, each recording the invariant that module must not violate (why
      `rot` is a separate column, why `lin` is never mutated, why `picking.py` exists at all), so
      the constraint sits at the point of use rather than only in PLAN.md.
      **Added `cli.py`, which PLAN.md's Repository Layout omitted** — M1 mandates
      `foursight parse`/`foursight check` and the console script already points at
      `foursight.cli:main`. PLAN.md layout updated. Its `main()` raises `NotImplementedError`
      naming T1.11, so `foursight` fails with a clear message instead of a `ModuleNotFoundError`
      traceback.
      The `[gui]` extra design note was already carried out in T0.2 (PLAN.md Tech Stack updated).
      **DoD met:** the stated import check passes; `find src/` matches PLAN.md's layout; ruff
      clean over all 32 files. Verified beyond the DoD: all 23 headless modules import with
      **zero** Qt/GL modules in `sys.modules` afterwards — the first actual evidence for the
      Qt-free invariant, though CI enforcement still needs T0.6's `.[dev]`-only job.
      Blocked by: T0.2

- [x] **T0.4 — `scripts/build.py`** — *done*
      PyInstaller **one-dir** build; `--onedir` is hard-coded and not configurable, since one-file
      re-extracts ~200 MB per launch and trips Windows AV heuristics. Shells out to
      `sys.executable -m PyInstaller` so it always uses the venv. Keeps `.spec` and work files in
      `build/`, passes `--paths src` for the src-layout, and handles missing entry point / missing
      PyInstaller with a clear message and exit 2.
      Built more than a bare stub because T0.8 needs to drive it: `--entry` and `--name` let the
      spike bundle a different script, `--dry-run` prints the command, `--clean` clears the cache.
      **Console output is deliberately left on** — `--windowed` is opt-in for release, because
      T0.8's whole purpose is observing *how* a bundled GL app fails to start, and `--windowed`
      throws that output away. `HIDDEN_IMPORTS` is empty on purpose: guessing Qt/GL hook entries
      up front would mask the real failure T0.8 is meant to find.
      **DoD met:** `.venv/bin/python scripts/build.py` produces `dist/foursight/` (3 files,
      41.8 MB) and the bundled executable launches and exits 0 — so the PyInstaller bootloader
      works on Linux.
      **Known limitation, by design:** the entry point is `gui/app.py`, still a docstring-only
      stub, so **no Qt or GL was bundled** (verified: no PySide6/Qt/OpenGL files in `dist/`).
      This proves the build harness, not that Qt bundles. That is exactly T0.8's question.
      Blocked by: T0.3

- [x] **T0.5 — Smoke tests** — *done*
      `tests/test_smoke.py`, 28 tests: package installed; `fileio` present and no `foursight.io`;
      all 24 layout modules import (parametrized, so a dropped or renamed module fails by name);
      plus the two invariants that are checkable before any behaviour exists —
      **headless modules pull in no Qt**, and **`parser/model.py` imports nothing from `machine/`**.
      The Qt check runs in a **subprocess**: this venv has `[gui]` installed, and once `gui/`
      really imports Qt a same-process `sys.modules` check would quietly become test-order
      dependent. The `machine/` check is **static (AST)**, so it also catches function-local
      imports, which a `sys.modules` check would miss.
      **DoD met:** `.venv/bin/pytest -q` → 28 passed, **exit 0** (the corrected T0.2 gate).
      **Both invariant tests mutation-verified**, since a test that cannot fail is worthless:
      adding `from PySide6 import QtCore` to `sim/timing.py` failed the Qt test (reporting the
      leaked module list), and a *function-local* `from foursight.machine import profile` in
      `parser/model.py` failed the AST test. Both files then confirmed byte-identical to their
      pre-mutation state via `cmp`.
      Blocked by: T0.3

- [x] **T0.6 — GitHub Actions matrix (Ubuntu + Windows)** — *done; all 6 jobs green on
      `origin/main`*
      **DoD met (run 31112042171, and again on the T1.1 push):** `lint` ✓, `headless` ✓, and all
      four matrix legs ✓ — `ubuntu×3.11`, `ubuntu×3.12`, `windows×3.11`, `windows×3.12`, 28 tests
      each. The two things I could not check locally both hold: **Windows works**, so the
      cross-platform claim now has evidence rather than intent; and **3.11 works**, so
      `requires-python = ">=3.11"` and `target-version = "py311"` are validated rather than
      asserted (local dev is 3.12.10 only).
      The `headless` job behaves exactly as simulated: prints `Qt/GL absent, as required`, then
      **21 passed / 7 skipped**, then `foursight.cli imports clean without Qt`. The Qt-free
      invariant is now genuinely enforced by infrastructure, not just by a local test.
      **Follow-up applied:** runs annotated a Node 20 deprecation for `actions/checkout@v4` and
      `actions/setup-python@v5` (forced onto Node 24). Both bumped to `@v7`, the current major.
      `.github/workflows/ci.yml`, 6 jobs. Restructured slightly from the original single-job plan:
      **`lint`** (ubuntu/3.12: `ruff check --output-format=github`, `ruff format --check`, plus
      `scripts/build.py --dry-run`, since build.py has no unit tests); **`test`** (4 legs —
      ubuntu+windows × 3.11+3.12, `fail-fast: false` so one red leg cannot hide the others);
      **`headless`** (`.[dev]` only). Lint runs once rather than in every leg — faster feedback,
      same coverage.
      CI uses the runner's `setup-python` interpreter directly, not a `.venv`. The venv rule
      exists to protect a dev machine's system Python; a throwaway runner has nothing to protect,
      and `.venv/bin` vs `.venv\Scripts` across OSes is pure friction. Documented in the workflow
      so nobody "fixes" it.
      The `headless` job does three things, and the middle one guards the guard: assert Qt/GL is
      genuinely **absent** (if PySide6 ever arrives transitively the gui tests would stop skipping
      and the job would silently stop proving anything), run `pytest -q -rs`, then import
      `foursight.cli` and assert no Qt reached `sys.modules`.
      `tests/test_smoke.py` gained a `requires_qt` skipif so the 7 `gui/` module tests skip when
      the extra is absent instead of failing. Inert today (the stubs import Qt-free), load-bearing
      from T2.7.
      Also added a Linux-only apt step for `libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3`:
      PySide6 wheels need these to *import*, not merely to install. Unnecessary while `gui/` is
      stubs; correct now rather than debugged inside a matrix leg at T2.7.
      **Verified locally:** YAML parses (6 jobs, 4 legs); the `headless` job simulated end to end
      in a throwaway `[dev]`-only venv → **21 passed, 7 skipped**, Qt-absent assertion passes, CLI
      imports Qt-free. Mutation-tested: `import pyqtgraph` added to `verify/report.py` fails that
      job on **two** independent checks while the full matrix would have stayed green — which is
      precisely the gap this job closes. File restored byte-identical.
      Blocked by: T0.5
      Files: `.github/workflows/ci.yml`, `tests/test_smoke.py`

- [x] **T0.7 — SPIKE: render 500k segments** — *done; D1, D3 and D5 all resolved*
      `spikes/render_500k.py` is written and runnable. Synthesizes 500k segments in the real
      columnar `(N, 2, 3)` float64 layout along a spread-out helical path (a degenerate straight
      line would flatter the result), partitions by `kind` then subdivides to `--batches` items,
      uploads float32, orbits, and times frames **inside `paintGL` after `glFinish`** — without
      that, the numbers measure command submission rather than rendering. Discards 20 warmup
      frames and reports median/mean/worst-5% fps, worst frame ms, RSS, GL renderer/vendor/version,
      and the driver's line-width range, then prints a paste-ready Markdown block and a verdict
      against the 30 fps threshold.
      **DoD met — measured, verdict recorded, D1/D3/D5 all resolved.** Results on Intel Iris Xe
      (ADL GT2) / Mesa 25.1.5, uncapped: **376.9 fps median at 500k** (worst frame 5.6 ms),
      275.5 at 1M, 188.6 at 2M. Verdict: **pyqtgraph holds with ~12× margin; no `QOpenGLWidget`
      fallback.** Introspection additionally resolved D3 and corrected two wrong claims in
      PLAN.md § Rendering Constraints.
      **Two findings that changed PLAN.md beyond the fps verdict:**
      1. **Vsync makes this measurement lie.** With vsync on, 1k / 50k / 500k segments all report
         ~60 fps — the display refresh rate, not the GPU. The pass is real but the headroom is
         invisible. Measure with `vblank_mode=0`; the spike now warns when it detects a ~60 fps
         result, and PLAN.md's M2 gate says to measure uncapped or it cannot fail.
      2. **The ≤ 250 MB memory budget was never achievable.** Fixed Python + Qt + Mesa overhead is
         **233 MB** with 1k segments on screen. The columnar store's own cost is exactly as
         predicted — 40 MB per 500k, linear to 2M — so the data model is vindicated while the
         budget was measuring the interpreter. PLAN.md now binds the budget to geometry + GL
         buffers (≤ 50 MB per 500k) and records total RSS (273 MB at 500k) rather than capping it.
      Blocked by: — *(done)*
      Files: `spikes/render_500k.py`, `PLAN.md`

- [~] **T0.8 — SPIKE: PyInstaller one-dir on a clean Windows VM** *(D2 open; needs a VM)*
      `spikes/gl_window.py` is written. It imports PySide6, shiboken6, PyOpenGL **and pyqtgraph
      separately**, printing which one fails — a bare traceback is close to useless on a machine
      with no Python installed, and pyqtgraph is included deliberately because it ships shader
      source as package data, exactly what PyInstaller's analysis tends to drop. It reports
      `sys.frozen`, `_MEIPASS`, the Qt plugin path, and GL renderer/vendor/version, then exits with
      a code that distinguishes the outcomes: **0** a frame was painted, **1** window created but
      never painted (QPA plugin or GL driver), **2** an import failed. `--seconds` auto-closes so
      the check is scriptable; `--stay` keeps it open for a human.
      Build and test:
      `.venv/bin/python scripts/build.py --entry spikes/gl_window.py --name gl-spike`
      then copy `dist/gl-spike/` to a Windows VM with no Python or dev tooling and run
      `gl-spike.exe` **from a terminal** so the console output is visible.
      **Linux half: DONE and passing.** Bundle is 410 real files + 30 symlinks, 265 MB. All four imports succeed;
      pyqtgraph's 87 package-data files survive analysis; all nine Qt platform plugins including
      `libqxcb.so` are present; a real frame paints on Iris Xe; exit 0. Launched from a neutral
      working directory so nothing could resolve out of the source tree. **`HIDDEN_IMPORTS` stays
      empty — nothing needed adding.** The 265 MB size also corroborates rejecting one-file.
      **Windows half: OUTSTANDING, and it is where the risk actually lives.** DLL resolution, the
      VC++ runtime and AV heuristics all differ; nothing above transfers. Copy `dist/gl-spike/` to
      a VM with no Python or dev tooling, run `gl-spike.exe` **from a terminal**, and read the exit
      code: 0 painted, 1 created but never painted, 2 import failed.
      **DoD:** Windows launch result and any `HIDDEN_IMPORTS` / binary / Qt-plugin-path fix recorded
      in `PLAN.md` and applied to `scripts/build.py`; D2 ticked.
      Blocked by: a clean Windows VM
      Files: `spikes/gl_window.py`, `PLAN.md`

- [ ] **T0.9 — Milestone gate** — *one item left: T0.8 on Windows*
      **DoD:** both spikes have a *measured* answer in `PLAN.md`; CI green; D1–D3 and D5 resolved.
      Status: CI green on `origin/main` (6/6 jobs) ✓ · render spike measured and recorded ✓ ·
      D1 ✓ D3 ✓ D5 ✓ · packaging spike measured **on Linux only**, D2 half-open.
      **Remaining: run `dist/gl-spike/gl-spike.exe` on a clean Windows VM** (T0.8), record the
      result in `PLAN.md`, tick D2. Nothing else blocks the gate.
      Blocked by: T0.8

---

## M1 — Parser core + verifier (no GUI)

Headless end to end. This is where the parse-rate target is won or lost, and where the severity
taxonomy gets teeth.

- [x] **T1.1 — `parser/model.py`** — *done*
      `SourceRef`, `ModalState` (both `slots=True, frozen=True`), `Command` (`slots=True`) exactly
      as PLAN.md § *Parse layer* specifies. Imports nothing from the rest of the package.
      **Three decisions the PLAN sketch left open, now made and recorded there:**
      1. `ModalState` fields carry the dialect defaults (`mm`/`17`/`90`/`91.1`/`94`); everything
         not-yet-established starts `None`, so "never set" stays distinguishable from "set to zero".
      2. `units` is never `None` (unlike `offset`) — a program with neither G20 nor G21 still has an
         effective unit. "Units never set" is a program-level property, so the verifier finds it by
         scanning the command stream rather than reading a sentinel.
      3. This module owns the **word-letter tables**, because the resolver's G20 conversion depends
         on classifying letters and a mistake is silent: `WORD_LETTERS`, `AXIS_LETTERS`,
         `LINEAR_LENGTH_LETTERS` (scaled by 25.4), `ROTARY_LETTERS` (degrees, never scaled).
         **`F` is deliberately in neither scaling set** — a length rate under G94/G95 but 1/minutes
         under G93, so only the resolver can classify it per feed mode.
      **DoD met:** `tests/test_parser.py`, 21 tests — field sets asserted verbatim against PLAN.md,
      annotations checked (`gcodes`/`mcodes` are `list[str]`, not floats), frozen-ness, hashability,
      `slots` in effect, dialect defaults, copy-on-write via `replace`, `Command` mutable and
      unhashable. The `machine/` import rule is covered by the existing AST test in
      `tests/test_smoke.py`, which catches function-local imports too.
      **Mutation-verified** — dropping `slots=True`, classifying `A` as a length, and changing
      `gcodes` to `list[float]` each fail exactly one test. The first two break nothing
      behaviourally, so without these guards they would pass unnoticed.
      **Test bug found and fixed while writing it:** asserting frozen-ness by assigning an
      *unknown* attribute to a `frozen=True, slots=True` dataclass raises a confusing
      `TypeError: super(type, obj)...` from CPython's generated `__setattr__`, not
      `FrozenInstanceError` — so a typo'd field name would have passed for the wrong reason. The
      test now assigns only fields each class actually has.
      Blocked by: — *(T0.9's gate is still open on T0.6/T0.8, both of which are infrastructure-only
      and cannot affect the parse layer)*

- [x] **T1.2 — `parser/tokenizer.py`** — *done*
      Module-level compiled regexes, one `finditer` pass per line; nothing is compiled or matched
      per word. `model.py` gained `Word`, `ParseError` and `TokenizedLine` (the layout already
      promised `Word`); all recorded in PLAN.md § Tokenizer layer.
      **DoD met:** `tests/test_tokenizer.py`, 51 tests — every listed construct plus malformed
      input (`X`, `XY`, `X1.2.3`, stray punctuation) reported as `ParseError` records, never raised.
      `X1.2.3` keeps the valid `X1.2` and flags only `.3`; adjacent stray characters merge into one
      error rather than one per character.
      **Measured 159,476 lines/sec (6.27 µs/line)** for a realistic mix on the baseline machine —
      31% of the 20 µs/line budget, leaving ~13.7 µs for the resolver. T1.12 turns this into an
      assertion.
      **Two bugs found by probing edge cases rather than by the tests I first wrote:**
      1. **`X (why not) 10` was reported as two errors on valid input.** LinuxCNC strips comments
         *before* interpreting words, so that legally means `X10`. Comments are now removed in a
         prior pass and **blanked in place rather than deleted**, so every later offset — and hence
         every error position — stays correct. A fast `"(" not in line` membership test keeps the
         common case free; throughput actually *improved* after the change.
      2. **`O100 sub` reported "address 'S' has no value" three times**, one per letter of `sub`.
         O-word flow control is now detected and reported as a single unsupported construct. This
         mattered beyond tidiness: flow control decides *which motion runs*, so it must reach T1.7
         as `unsupported`, and a malformed-word diagnosis would have buried it.
      **Known gap, deliberate:** `ParseError` carries no severity — the parse layer cannot name a
      `verify` type without violating the dependency direction. T1.7 converts these to
      `Diagnostic`s, and is where the O-word error becomes `unsupported` rather than `error`.
      A second `N` on one line is silently ignored (first wins); flagging duplicates is the
      verifier's call, not the tokenizer's.
      Blocked by: T1.1
      Files: `src/foursight/parser/tokenizer.py`, `src/foursight/parser/model.py`,
      `tests/test_tokenizer.py`, `PLAN.md`

- [x] **T1.3 — `parser/resolver.py` — modal groups** — *done*
      `resolve(lines)` → `ParseResult(commands, errors)`, plus a `parse(text)` convenience the CLI
      will use in T1.11. All four required behaviours verified: canonicalization
      (`G01`/`G1`/`G1.0` → `'1'`, `G90.1` stays `'90.1'`, always strings), modal carry-over,
      same-group conflicts for both G and M codes, G20 conversion, and `ModalState` sharing.
      **DoD met:** 61 tests in `tests/test_parser.py` (40 new), 142 across the suite, ruff clean.
      **Measured 118,569 lines/sec (8.43 µs/line) for the full parse — 2.4× the 50k target**, the
      resolver adding only 2.31 µs/line. On a 43k-command file **one** `ModalState` is shared by
      every command.
      **A real perf bug, found by probing rather than by the tests I wrote first.** `feed` is
      applied after unit conversion, so it bypassed the "only if it differs" filter that `S`/`T` go
      through: restating an identical `F` allocated a fresh `ModalState` every line. CAM output
      repeats `F` on **every** line, so copy-on-write was defeated on the single most common input
      there is — 100 repeated-`F` lines produced 100 states instead of 1. Fixed, with a regression
      test that states why. My first sharing test only covered `F` stated once, which is the easy
      direction and passed throughout.
      **Two bugs caught by re-reading before testing:** `("6")` is a string rather than a tuple, so
      the M-code toolchange group was iterating characters; and `G43` with no `H` word cleared the
      active length offset instead of keeping it, which would silently drop a real Z shift.
      **Judgment calls recorded in PLAN.md:** canned cycles G80–G89 resolve *as motion modes* (that
      is what makes a following bare `X10 Y10` a drill cycle); `M7 M8` is a conflict because
      LinuxCNC groups them, even though mist+flood is physically meaningful; a repeated address word
      is an error rather than last-one-wins; unrecognized codes pass through untouched so the
      verifier can classify them.
      Also renamed `TokenError` → **`ParseError`**: it now carries resolver-level problems such as
      modal-group conflicts, and the old name would have been misleading. One commit old, no
      external consumers.
      Blocked by: T1.2
      Files: `src/foursight/parser/resolver.py`, `src/foursight/parser/model.py`,
      `tests/test_parser.py`, `PLAN.md`

- [x] **T1.4 — `fileio/loader.py` (minimum viable)** — *done*
      `load(path)` / `load_text(bytes)` → `LoadedFile(text, encoding, had_bom, newline, path)`,
      with decoding split from I/O so the encoding rules are testable without a filesystem.
      Recorded in PLAN.md § Loader layer.
      **DoD met:** 38 tests in `tests/test_loader.py`; 180 across the suite. Every DoD case —
      UTF-8, UTF-8 BOM, latin-1, CRLF, no trailing newline — is asserted **end to end through the
      tokenizer**: each `SourceRef` must slice its own line back out of the loaded text. Line
      numbers alone would not catch a BOM shifting everything by one.
      **A real bug found by checking a claim the module already made.** `detect_newline` handles
      lone `\r`, but `line_starts` only scanned for `\n` — so a classic-Mac file reported **one**
      line where the tokenizer saw **three**, at offsets `[0]` vs `[0, 4, 10]`. Two offset
      computations that must agree, disagreeing. The root cause was having two independent
      implementations of the same thing, so the fix removes the class of bug rather than the
      instance: `line_starts` now derives from `splitlines(keepends=True)`, the same primitive the
      tokenizer walks, making agreement hold **by construction**. Regression test covers LF, CRLF,
      lone CR, mixed, no-trailing-newline, empty and blank-only.
      **Mutation-verified:** leaving the BOM in, normalizing CRLF→LF, and dropping the binary check
      each fail tests (4, 2 and 1 respectively). File restored byte-identical.
      **Two decisions worth knowing:** offsets are *character* offsets into the decoded BOM-free
      text, not byte offsets into the file (they differ for UTF-16, and the editor holds decoded
      text); and binary input raises `FileLoadError` rather than latin-1 decoding an STL into
      thousands of meaningless diagnostics — the NUL check runs on decoded text because real UTF-16
      is full of NUL bytes.
      **Deliberately deferred to T5.4:** any size limit or streaming. A whole file is read into
      memory, which is fine at ~3 MB for 100k lines but not for a pathological input.
      Blocked by: T1.1
      Files: `src/foursight/fileio/loader.py`, `tests/test_loader.py`, `PLAN.md`

- [x] **T1.5 — `machine/profile.py`** — *done*
      `load_profile(path)` / `load_profile_text(text)` → `MachineProfile`, all frozen dataclasses,
      every section from PLAN.md implemented. `profiles/default_4axis.toml` written and loading with
      **zero unknown keys**. Recorded in PLAN.md § Loading rules.
      **DoD met:** 40 tests in `tests/test_profile.py`; 220 across the suite. All four required
      cases covered — head-mount-without-pivot refusal, unset-vs-zero offsets, inch conversion,
      unknown-key handling.
      **Mutation-verified, all five caught:** collapsing an unset offset to zero (4 failures),
      scaling rotary axes by 25.4, scaling the offset's A component, dropping the head-mount
      refusal, and using falsy-`or` for defaults.
      **A `PLAN.md` gap filled:** `rotary_wrap_warn` is referenced by the verifier checklist and
      described as "tunable", but appeared nowhere in the TOML sample — so it had no home. Added to
      `[limits]` in both the sample and the shipped profile.
      **The module's governing rule, now in PLAN.md:** *absence means unknown, never a fabricated
      value.* PLAN states it for work offsets; the same reasoning applies to every limit, so
      `max_feed`, `max_spindle_rpm`, `min_clearance_z`, per-axis limits and each offset are `| None`
      and their absence disables the check. Tolerances are the sole exception, since tessellation
      needs a number.
      **A bug caught before testing:** `_scaled(...) or DEFAULT` substitutes the default when the
      value is legitimately `0.0` — and `rotary_wrap_warn = 0` ("warn on any rotary move") is a
      reasonable setting that would silently have become 360. Replaced with an explicit `is None`
      check.
      **Design notes:** offsets are keyed `'54'`…`'59'` to match `ModalState.offset` so lookup needs
      no conversion; an unlabelled `[axes.b]`/`[axes.c]` is assumed **rotary**, because guessing
      rotary is recoverable while guessing linear corrupts values by 25.4×; `rotary_chord` scales on
      an inch profile despite its name, being a chord height in mm; and booleans are rejected as
      numbers, since `max_feed = true` would otherwise become 1.0 mm/min.
      **Note for T1.11:** the CLI must print `profile.unknown_keys`. Loading tolerates them so a
      newer profile still works, but an unsurfaced typo silently disables a check.
      Blocked by: T1.1
      Files: `src/foursight/machine/profile.py`, `profiles/default_4axis.toml`,
      `tests/test_profile.py`, `PLAN.md`

- [x] **T1.6 — `verify/report.py` + `verify/rules.py`** — *done*
      `Diagnostic`, three-tier `Severity` (`StrEnum`), unit-aware formatting, `Rule` base class,
      `Program`, the `@register_rule` registry and the `verify()` driver. Recorded in PLAN.md
      § Verifier infrastructure.
      **DoD met:** 35 tests in `tests/test_verify.py`; 255 across the suite. Registry covered
      (registering, ordered listing, duplicate ids refused, empty id refused, idempotent
      re-registration) and the inch-formatting requirement asserted — `format_length(400, "inch")`
      → `"15.748 in"`.
      **A silent failure mode found and fixed mid-task.** `load_builtin_checks()` originally held a
      plain `import foursight.verify.checks` for its registration side effect. `ruff check --fix`
      **deleted it as F401**, turning the function into a no-op and leaving the registry permanently
      empty — and *a verifier that finds nothing looks exactly like a clean program*, so nothing
      would have complained. Switched to `importlib.import_module`, which no linter can mistake for
      unused, and added two guards: one asserting the package really gets imported, one comparing
      the modules on disk in `checks/` against those named in `checks/__init__.py`.
      **Design additions beyond PLAN's sketch, all recorded there:** `rule_id` (tests assert on it
      rather than brittle message text; suppression will key on it); `offset` carried when known
      since `ParseError` already has it; `fix_ids` as a `tuple` so `Diagnostic` can be frozen and
      **hashable**, which is what lets T1.10 diff diagnostic *sets*.
      **Judgment calls:** rules see the whole `Program` because many checks are program-scoped;
      `Program` has no program-wide `units` field, since a program may switch G20/G21 mid-file;
      `parse_errors` are carried unconverted because choosing their severity is T1.7's judgment; a
      rule that raises becomes one diagnostic about itself while the rest still run, with
      already-yielded findings kept; and `format_angle` takes **no** units argument, because a
      parameter there could only be misused.
      **Also fixed:** `format_length(-0.0001, "mm")` rendered `-0 mm`, which reads like a real
      quantity. Values that round to zero now render `0`.
      **Mutation-verified, all four caught:** neutering `load_builtin_checks`, allowing duplicate
      rule ids, letting a broken rule propagate (2 tests), and adding a check module that
      `checks/__init__.py` does not import.
      Blocked by: T1.3, T1.5
      Files: `src/foursight/verify/report.py`, `src/foursight/verify/rules.py`,
      `src/foursight/verify/checks/__init__.py`, `tests/test_verify.py`, `PLAN.md`

      Blocked by: T1.3, T1.5

- [x] **T1.7 — Structural checks** — `verify/checks/structural.py` — *done*
      Five rules: `structural.syntax-error`, `.modal-group-conflict`, `.unsupported-oword`,
      `.unknown-code`, `.unsupported-motion`. Recorded in PLAN.md § Structural checks.
      **DoD met:** 38 tests in `tests/test_checks_structural.py`; 393 across the suite. The corpus's
      pending skips dropped **20 → 15**, so five declared mutations now assert for real
      (`malformed_word`, `modal_group_conflict`, `unknown_inert_code`, `canned_cycle`,
      `cutter_compensation`), and the clean baseline still reports **zero** diagnostics.
      **A design change to avoid brittleness:** `ParseError` gained a **`kind`**
      (`ParseErrorKind`), set by whichever layer produced it, plus `line`. Otherwise the verifier
      would have had to pattern-match message *text* to tell a malformed word from a modal-group
      conflict — so any wording change would silently re-route a diagnostic to the wrong rule and
      severity.
      **Spans, not blocks.** A canned cycle or comp region yields **one** diagnostic covering its
      extent — a twenty-hole cycle produced one, not twenty. Boundaries come from the resolver's
      existing `Command.motion` / `ModalState.cutter_comp`, so a bare `X10 Y10` inside a cycle is
      already known to belong to it. That is T1.3's carry-over paying off. An uncancelled span runs
      to program end and says so.
      **Judgment calls, all in PLAN.md:** G61/G61.1/G64 and G98/G99 are silent (they change
      cornering or canned-cycle return, not the centreline, and G64 is in nearly every LinuxCNC
      program — warning would be pure noise); `G10` and `G92`/`G92.x` are `unsupported` rather than
      warnings, since they move the coordinate system; cancel codes G40/G80 are interpreted while
      their activations are not; an unknown code reports once per code, not once per line.
      **Mutation-verified — and one mutation exposed a real test gap.** Downgrading a canned cycle
      from `unsupported` to `warning` failed 2 tests; marking cycles interpreted failed 2; making
      spans per-block failed 10. But **merging the syntax and modal-conflict kinds failed nothing**:
      the test asserting "a modal conflict is not a syntax error" used input with no syntax error, so
      the negative held trivially. Added the missing direction plus a test that no parse error
      surfaces under two rule ids; both now catch it.
      Blocked by: T1.6
      Files: `src/foursight/verify/checks/structural.py`,
      `src/foursight/verify/checks/__init__.py`, `src/foursight/parser/model.py`,
      `src/foursight/parser/tokenizer.py`, `src/foursight/parser/resolver.py`,
      `tests/test_checks_structural.py`, `PLAN.md`

- [x] **T1.8 — Process checks** — `verify/checks/process.py` — *done*
      13 rules: all of PLAN.md's Process group plus `process.g93-without-feed`, which the checklist
      mandates but the corpus had no mutation for — added one. Recorded in PLAN.md § Process checks.
      **DoD met:** 42 tests in `tests/test_checks_process.py`; 451 across the suite. Pending skips
      dropped **15 → 3** (only the geometry rules remain), and the clean baseline still reports
      **zero** diagnostics.
      **Built `machine/state.py` early** — a minimal *endpoint-only* position walker. Several rules
      need machine Z, and duplicating position tracking across `process.py` and `geometry.py` would
      be worse than building the small version T2.2 extends. Same reasoning that moved profile
      loading into M1.
      **The `min_clearance_z` ambiguity flagged in T1.10 is resolved:** judged in **machine
      coordinates**, consistent with PLAN's rule that verification happens there. `machine_value()`
      adds the active work offset and reports whether it was known; a `G53` block is already
      machine-absolute and must not have the offset applied twice. When the offset is unknown the
      message says "assumes zero work offset" — these rules are already warnings, so there is no
      tier below to downgrade to and the caveat carries the uncertainty instead.
      **Unknown position is treated asymmetrically, on purpose:** a rapid whose Z was never
      established is not judged (no position, no claim), while a tool change whose Z cannot be
      established *is* a violation (if we cannot show the tool was clear, we cannot call the change
      safe). Both directions are tested.
      **A fixture defect the new checks caught:** `arc_r_format.nc` rapided at Z-1, i.e. at cutting
      depth without retracting. The check was right; the fixture was not. Fixed.
      **Mutation-verified — and two survivors exposed real test weaknesses:**
      1. **A crashing rule was passing as a clean result.** Making the rapid check judge an unknown
         Z crashes it on `None`; `verify()` converts that into `internal.rule-failed` so one bad rule
         cannot suppress the others — and my test only asserted the *specific* rule id was absent, so
         the crash read as correct silence. `conftest.diagnose` now refuses to return any result
         containing `internal.rule-failed`, closing that hole for **every** fixture and check test at
         once. The robustness feature was masking bugs from the tests.
      2. **A test that could not distinguish the thing it named.** `test_g53_coordinates_are_already_
         machine_absolute` used Z20 with a +10 offset — 20 and 30 both clear a 5 mm threshold, so
         double-counting the offset was invisible. Rewritten at Z2, where the two answers differ,
         plus a paired control without G53.
      Blocked by: T1.6
      Files: `src/foursight/verify/checks/process.py`, `src/foursight/machine/state.py`,
      `src/foursight/verify/checks/__init__.py`, `tests/test_checks_process.py`,
      `tests/conftest.py`, `tests/fixtures/arc_r_format.nc`, `PLAN.md`

- [x] **T1.9 — Sim-free geometry checks** — `verify/checks/geometry.py` — *done*
      5 rules: `geometry.arc-radius-mismatch`, `.arc-r-invalid`, `.axis-travel-exceeded`,
      `.rotary-travel-exceeded`, `.rotary-wrap`. Recorded in PLAN.md § Geometry checks.
      **DoD met, and the verifier is complete:** 41 tests in `tests/test_checks_geometry.py`;
      **498 across the suite with zero skips** — every one of the corpus's 22 mutations now asserts a
      real diagnostic, and the clean baseline still reports nothing. 23 rules registered in total.
      **`tolerance.arc_radius_mismatch` has one source**, asserted rather than assumed: the same arc
      is accepted under a loose profile and rejected under a strict one, so the T5.2 fix cannot
      drift from the check. PLAN.md warns specifically against hard-coding it at both sites.
      **Arc tests check hand-computed values**, not whatever the code returns — G17/G18/G19 IJK
      mappings, G90.1 vs G91.1 centres, helical arcs measured in-plane only, and omitted axis words.
      **Added `geometry.arc-r-invalid` beyond the checklist's coincident-endpoint case:** |R| below
      half the chord describes no circle at all. Same class of "undefined, not imprecise", so it
      shares the rule. A semicircle (`2R == chord`) is the limiting valid case and is accepted.
      **A deliberate asymmetry, and a deviation worth knowing:** linear travel downgrades
      `error` → `warning` on an unknown work offset per PLAN, but rotary travel does **not** — PLAN
      attaches that downgrade to the linear bullet only, and a non-zero rotary work offset is rare.
      The assumption is stated in the message so the severity follows the plan while the uncertainty
      stays visible.
      **Three problems caught while writing it:** a dummy `Command` constructed just to read the
      start position (replaced by separate start/end helpers); a lambda built inside a loop; and
      rotary axes being checked by *both* travel rules, which would have reported every rotary
      violation twice.
      **Mutation-verified — 6 mutations, and the sixth exposed a non-discriminating test.**
      Swapping G18's K for J failed 2; treating every centre as absolute failed 15; hard-coding the
      tolerance failed 1; removing the offset downgrade failed 1; accepting coincident-endpoint R
      arcs failed 2. But making rotary wrap read the absolute target instead of the delta failed
      **nothing**: the test used A350 → A360, where neither reading exceeds the 360 threshold.
      Re-pointed at A350 → A400, where the delta (50) and the target (400) fall on opposite sides of
      it.
      Blocked by: T1.6
      Files: `src/foursight/verify/checks/geometry.py`,
      `src/foursight/verify/checks/__init__.py`, `tests/test_checks_geometry.py`,
      `tests/conftest.py`, `PLAN.md`

- [x] **T1.10 — Fixture corpus: clean baseline + single-mutation siblings** — *done (built early,
      ahead of T1.7–T1.9, because the corpus is an input to the checks rather than an output)*
      `tests/fixtures/baseline_4axis.nc` plus 9 targeted fixtures, and **20 mutations declared
      in `tests/conftest.py`** covering the whole PLAN.md § Verifier Rules checklist.
      **Mutations are derived, not stored.** Each broken fixture is the baseline plus one
      declarative edit, so the single-mutation property cannot rot the way 20 hand-maintained
      near-copies of the baseline would.
      `test_every_mutation_changes_exactly_one_line` **enforces** it rather than trusting it, and
      `apply_mutation` refuses unless its `find` string matches exactly one line.
      **Each mutation declares the `rule_id` it must add**, so T1.7–T1.9 have a written contract.
      Until a rule registers, its test **skips naming the missing rule** — an unimplemented check is
      visible as pending, never as passing. `test_pending_rules_are_visible` prints the outstanding
      list (currently 19 rules).
      **DoD met, with one part necessarily deferred:** all 10 fixtures load through the real loader
      and parse without raising; every command in every fixture slices its own source line back out;
      the baseline's diagnostic set is asserted (empty today, and the test reports any drift). The
      per-mutation *diagnostic* assertions cannot pass until their rules exist — that is the 20
      skips, and they convert to real assertions automatically as T1.7–T1.9 land.
      **A profile bug found by trying to write a genuinely clean program:** `default_4axis.toml` had
      `axes.z.max = 0.0` while `safety.min_clearance_z = 5.0` required rapids to stay above Z+5.
      **No program could satisfy both** — there was nowhere legal to retract to, so a clean baseline
      was impossible. Z max raised to 100.0 with the reasoning recorded in the profile.
      **A fixture bug caught by the corpus's own guard:** `arc_g18_direction.nc` had **nested
      parentheses** in a comment. G-code comments do not nest, so the comment ended at the first
      `)` and the remainder tokenized as code, yielding 14 spurious errors. Exactly the failure
      `test_targeted_fixtures_have_no_parse_errors` exists to prevent — a typo in a fixture would
      otherwise surface later as a mystery diagnostic blamed on the construct under test.
      Baseline geometry verified independently before building on it: both arcs are exact quarter
      circles, zero radius mismatch, and they chain endpoint-to-endpoint.
      **Note for T1.8:** `min_clearance_z` is ambiguous — the check reads machine Z, but a
      machinist thinks of clearance above the *part*. With `g54 = [0,0,0,0]` they coincide, which is
      why the baseline passes; decide explicitly when implementing the rule.
      Blocked by: T1.3
      Files: `tests/fixtures/*.nc` (10), `tests/conftest.py`, `tests/test_fixtures.py`,
      `profiles/default_4axis.toml`


- [x] **T1.11 — CLI: `foursight parse` / `foursight check`** — *done*
      Both subcommands, `--profile`, `--block-delete` (off by default), plus `--modal` for `parse`.
      Recorded in PLAN.md § CLI.
      **DoD met:** 43 tests in `tests/test_cli.py`; **541 across the suite**. Every fixture runs
      through both subcommands; the Qt-free requirement is checked in a **subprocess**, because this
      venv has `[gui]` installed and an in-process check would prove nothing about a `.[dev]`-only
      install. That guard was itself verified to fail when a leak is simulated.
      **A packaging bug found and fixed:** the default profile lived at the **repo root**, where an
      installed app or a PyInstaller bundle can never find it. Moved to
      `src/foursight/profiles/default_4axis.toml` as package data, resolved via
      `importlib.resources` in `default_profile_path()`. This would have surfaced at M5 as "the
      bundled app cannot find its own default profile"; PLAN.md's layout is updated.
      **Exit codes are the contract**, each asserted: `0` clean, `1` errors, `2` cannot run.
      **`unsupported` and `warning` deliberately do not fail the run** — a program that legitimately
      contains canned cycles must still pass a pipeline, since failing it would push users toward
      suppressing the whole check.
      **Diagnostics and summary go to stdout, tool problems to stderr.** The first version put the
      summary on stderr, which interleaved out of order as soon as stdout was piped; linters put
      both on stdout and that is what this now does.
      **The T1.5 note is honoured:** unrecognized profile keys print to stderr, so a `max_fed` typo
      cannot silently disable the feed check. Latin-1 fallback is reported too.
      **Mutation-verified, all six caught:** errors not failing the run (2 tests), `unsupported`
      failing it (3), swallowing profile typos (2), ignoring `--profile` (5), ignoring
      `--block-delete` (1), and letting exceptions escape as tracebacks (3).
      **A process failure worth recording:** after moving the profile I ran only the CLI tests, not
      the full suite, and left `conftest.py` pointing at the old path — 110 errors that the next full
      run surfaced. Moving a file that tests locate by path needs a full-suite run immediately, not
      at the end of the task.
      Blocked by: T1.7, T1.8, T1.9
      Files: `src/foursight/cli.py`, `src/foursight/machine/profile.py`, `pyproject.toml`,
      `src/foursight/profiles/default_4axis.toml`, `tests/test_cli.py`, `tests/conftest.py`,
      `tests/test_profile.py`, `PLAN.md`

- [x] **T1.12 — `tests/test_perf.py`: parse rate** — *done*
      **DoD met:** 6 tests; 547 across the suite. **Measured 110,551 lines/sec (9.05 µs/line) —
      2.2× the 50k floor** — and the measurement is *logged in normal output*, not only with `-s`:
      a `pytest_terminal_summary` hook prints every measurement, so the numbers appear in CI logs
      without anyone remembering a flag.
      **Asserts the floor, logs the actual**, per PLAN.md's guidance, so a slower CI runner does not
      flake. Best-of-3 after a warmup removes scheduling noise without inflating the result.
      `FOURSIGHT_PERF_MIN_RATE` overrides the threshold for a runner that genuinely cannot reach it —
      better to argue about a wrong threshold than to delete the test.
      **Three guards beyond the rate itself**, each catching something a rate test alone would miss:
      per-line cost from 5k → 50k lines stays within 1.3× (a rescanning parser looks fast on a small
      file); one `ModalState` is still shared across all 42,858 commands (a sharing regression would
      not fail the rate test on a fast machine but would eat the budget on a slow one); and the
      generator really produces the requested size, without which every measurement above is
      meaningless.
      **A measured finding recorded in PLAN.md rather than acted on:** verification costs **530 ms**
      for 42,858 commands — *more than the parse* — and **72% of it (380 ms) is seven rules each
      independently re-walking the command list** to rebuild positions. Sharing one walk through
      `Program` would cut it to roughly 200 ms. Not done here: PLAN sets no verification target, so
      nothing is being missed, and it would change the rule contract. Flagged because M2's gate
      (100k lines in 5 s) leaves verification ~1.1 s of that budget.
      Blocked by: T1.3
      Files: `tests/test_perf.py`, `tests/conftest.py`, `PLAN.md`

- [x] **T1.13 — Milestone gate** — *M1 COMPLETE; all five DoD items verified*
      **DoD 1 — all fixtures parse: PASS.** 10 fixtures, 0 failures, 0 parse errors in any of them.
      **DoD 2 — each broken fixture produces its expected added diagnostic: PASS.** All **22**
      mutations hit their declared `rule_id`, diffed against a baseline whose own diagnostic set is
      **empty**.
      Three mutations add a *second* diagnostic, and all three are legitimate cascades — which is
      precisely why PLAN.md rejects "exactly one diagnostic per file":
      • `cut_before_spindle` (removes `S8000 M3`) also trips `coolant-without-spindle`, because M8 is
      now on with the spindle stopped.
      • `rapid_below_clearance` (`G0 Z25` → `G0 Z1`) also trips `toolchange-without-retract`, because
      the M6 now happens below clearance.
      • `axis_travel_exceeded` (`X50` → `X500`) also trips `arc-radius-mismatch`, because the
      following arc now starts 450 mm away and its IJK centre no longer matches.
      Each is the *correct* consequence of the single mutation, and each would have broken a
      one-diagnostic-per-file assertion.
      **DoD 3 — `foursight check` end to end: PASS.** All 10 fixtures via the installed console
      script; 8 clean, 2 reporting their intended `unsupported` span; exit codes as specified.
      **DoD 4 — parse rate asserted: PASS.** 108,538 lines/sec (9.21 µs/line), 2.2× the 50k floor,
      asserted and logged by `tests/test_perf.py`.
      **DoD 5 — CI green: NOT YET CONFIRMED.** Run **31118036275** (T1.3) **failed**, and had gone
      unnoticed for nine commits. Cause was **transient GitHub infrastructure**, not a regression:
      `Failed to resolve action download info. Error: Service Unavailable` at *Set up job* on four of
      six jobs, while `windows-3.12` and `headless` ran to completion and **passed** — which is what
      shows the code was fine. Everything since is pushed (`eeb9520`) and run **31121…** is queued;
      the matrix has not yet exercised the verifier, the CLI or the perf test on **Windows** or
      **3.11**.
      **The one genuine risk in that run** is the perf floor on a shared runner. If a leg reports
      below 50k lines/sec, set `FOURSIGHT_PERF_MIN_RATE` in the workflow rather than deleting the
      assertion.
      **CI update — run 31123157517 (`a87ad82`): 4 jobs never started, 2 passed.** Cause again
      infrastructure, and a different one: *"The job was not acquired by Runner of type hosted even
      after multiple attempts"* — all four Ubuntu-hosted jobs sat 15 minutes without a runner. The
      T1.12 run was `cancelled`, which is correct: the workflow's `cancel-in-progress` superseded it.
      **Both Windows legs passed in under 2 minutes, and those were the ones that mattered** — the
      first time the matrix has exercised the verifier, the CLI and the perf test on Windows or 3.11.
      **Two real findings from that data, both acted on:**
      1. **The perf floor has only 14% headroom on Windows.** 57,009 lines/sec (py3.11) and 58,747
         (py3.12) against the 50k floor; Windows CI is ~1.9× slower than the dev machine. The target
         *is* met on the slowest hardware we test, and a 15% regression would be caught — but runner
         variance may occasionally fail the build. Recorded in PLAN.md, along with the rule that the
         answer is a faster parse or an evidence-based revision of the target, **not** lowering
         `FOURSIGHT_PERF_MIN_RATE` until it stops complaining.
      2. **A test was silently skipping on Windows.** `546 passed, 1 skipped` against 547/0 locally:
         `test_the_console_script_is_installed_and_runs` looked for `foursight` beside
         `sys.executable`, but Windows installs `foursight.exe` into `Scripts/`. So the console
         script went untested on the one platform where a shim is most likely to break. Fixed to
         check both names.
      Also recorded: **verification costs 1,086 ms on Windows CI** versus 530 ms locally — ~2.5 s of
      M2's 5 s budget for a 100k-line file, which makes the redundant-walk optimization more
      attractive than the local numbers suggested.
      **To close:** re-run the four Ubuntu jobs and confirm all 6 green.
      Blocked by: CI confirmation only
      **DoD 5 — CI green: PASS (2026-08-07, run 31165831549).** All six jobs green: `lint`,
      `headless (no Qt installed)`, and all four matrix legs — ubuntu and windows × py3.11 and py3.12.
      So the verifier, the CLI and the perf test are now exercised on Windows and on 3.11, which is what
      this item was waiting for. It took three fixes to get there, and **this entry predicted the first
      one exactly** — "if a leg reports below 50k lines/sec, set `FOURSIGHT_PERF_MIN_RATE` in the
      workflow rather than deleting the assertion" — which is what happened (47–50k on shared runners
      against a 50k floor) and what was done. The other two: the parse-linearity bound at 2.07x against
      `< 2.0`, and a console-script test that had been silently skipping on Windows because it looked for
      scripts in `Path(sys.executable).parent`. All three are recorded under M2's CI follow-up note.
      **Confirmed incidentally: the golden geometry hashes survive a different libm**, which was the one
      untested assumption behind T2.10's 1 µm / 0.001° quantization grid.
      Files: `TASKS.md`

### M1 summary

Parse layer, machine profile, verifier and headless CLI complete. **547 tests, 23 rules**, ruff
clean. Measured: parse **110k lines/sec** (2.2× target), one `ModalState` shared across 42,858
commands, verification **530 ms** for the same file — with 72% of that identified as redundant
position walking, recorded in PLAN.md but deliberately not optimized, since no target requires it.

Carried into M2:
- **Verification cost.** M2's gate (100k lines parsed and rendered in 5 s) would spend ~1.1 s
  verifying. The fix is measured and scoped; take it if the gate turns out tight.
- **`machine/state.py` is endpoint-only.** T2.2 extends it for simulation; **T2.8 must re-run the
  travel-limit check over interpolated points**, because an arc can bulge past a limit mid-sweep
  while both endpoints sit inside it. Nothing in M1 proves a program stays in bounds.
- **`min_clearance_z` is judged in machine coordinates**, with the work offset applied and an
  "assumes zero work offset" caveat when unknown.

---

## M2 — Simulation + viewer

Built on the **4-axis-shaped data model from day one**, kinematics transform as identity until M4.
Re-granulate after D1 is resolved — a `QOpenGLWidget` fallback materially changes T2.5–T2.7.

- [x] **T2.1 — `sim/segments.py`: `SegmentStore`** — *done*
      Columnar arrays exactly as PLAN specifies, plus a `SegmentBuilder`. Recorded in PLAN.md
      § Segment store implementation.
      **DoD met:** 39 tests in `tests/test_segments.py`; **586 across the suite**. `lin.reshape(-1, 3)`
      is a genuine zero-copy view (`.base is lin`, `np.shares_memory`), and **memory at N = 500k is
      38.5 MB (77 bytes/segment)** — against PLAN's ~38 MB and the 200 MB+ per-object storage would
      cost. The figure is printed in the terminal summary like the other measurements.
      **Chunked growth rather than a doubling realloc**, because doubling leaves up to 2× the needed
      capacity resident and the 250 MB budget cannot spare it. One concatenate at `finalize` buys
      exactly-sized arrays.
      **`add_polyline` is vectorized** — 500k per-segment Python calls would cost more than the
      interpolation that feeds them. `rotations` is a **separate argument**, so `(M, 4)` points are
      *rejected* rather than silently interpreted: the mm/degrees split is enforced at the call site,
      not only in storage. A test also scans the public API for any 4-wide array accessor, since a
      convenience helper returning one is the obvious way this invariant would erode.
      **`finalize` refuses `line == 0`.** An untraceable segment silently breaks editor sync,
      diagnostics and fixes, so it cannot be built at all.
      **Mutation-verified, all six caught:** returning a copy from `vertices` (1 test), widening `lin`
      to 4 (33), accepting a zero line number (1), storing `lin` as float32 (32 — note this *halves*
      memory, so the memory assertion alone would have welcomed it), having
      `set_part_coordinates` also write `lin` (1), and keeping whole chunks at finalize (30).
      One mutation initially printed nothing and looked like a survivor; the cause was a literal
      `\n` in a bash-quoted replacement making the file invalid Python. Re-run properly, it is
      caught — a broken mutation is not evidence of a robust test.
      Blocked by: T1.13
      Files: `src/foursight/sim/segments.py`, `tests/test_segments.py`, `PLAN.md`

- [x] **T2.2 — `machine/state.py`: `MachineState`** — *done*
      Steps a command list into a `Step` per block: the `Move`s it performs **in machine
      coordinates**, its dwell, and an honest reason when its geometry cannot be produced. The
      verifier's endpoint-only `walk` is untouched. Recorded in PLAN.md § MachineState.
      **DoD met:** 29 tests in `tests/test_machine_state.py`; **615 across the suite**. All five
      listed constructs covered — G53, G28/G30, G43/G44 with H and G49, G54–G59, G4 dwell.
      **Two constructs PLAN calls "interpreted" are not computable from available data, and I
      resolved them differently on purpose:**
      1. **G28/G30** — the reference point is machine-specific and appears nowhere in the G-code.
         Added optional **`[axes.*].home`** to the profile (unset in the shipped default). With no
         home configured the move is **not drawn** and the position afterwards becomes *unknown*;
         claiming to still know it would corrupt every later move. `G28 X0 Y0` is two rapids.
      2. **G43/G44** — no tool table exists, so the H length is unknown. Here suppression would be
         the **wrong** trade: G43 is in nearly every real program and refusing to draw them all makes
         the previewer useless. The offset shifts the Z datum uniformly *without changing the path's
         shape*, so the path **is** drawn and `Step.tool_length_unmodelled` records that Z is
         relative to the spindle rather than the tool tip.
      The asymmetry is proportionality: a G28 is one rapid, G43 is the whole program.
      **Owed follow-up, recorded in PLAN.md:** a verifier rule for the unmodelled tool length. By the
      strict taxonomy it is motion-affecting and uninterpreted — `unsupported` — but adding a rule
      was outside this task.
      **Mutation-verified, all five caught:** drawing G28 to machine zero instead of refusing (3
      tests), suppressing G43 paths instead of flagging them (1), reporting programmed instead of
      machine coordinates (5), keeping the position after an undrawable G28 (1), and clamping dwell
      instead of carrying it through (1).
      **Also fixed a tautological test of my own:** an assertion comparing a list comprehension to
      itself, which could never fail. Replaced with a real check that `Position` exposes four *named*
      axes and is not indexable, so `math.dist` cannot be applied across mm and degrees.
      Blocked by: T2.1
      Files: `src/foursight/machine/state.py`, `src/foursight/machine/profile.py`,
      `src/foursight/profiles/default_4axis.toml`, `tests/test_machine_state.py`, `PLAN.md`

- [x] **T2.3 — `sim/interpolate.py`: lines and arcs** — *done*
      G0/G1 lines, G2/G3 in IJK and R form, helical, adaptive chord tessellation, per-step rotary.
      Recorded in PLAN.md § Arc Semantics.
      **DoD met:** 32 tests in `tests/test_arcs.py`; **646 across the suite**. All three hypothesis
      properties PLAN names are asserted over generated inputs — points equidistant from the centre
      within 1e-6, chord sagitta never exceeding `arc_chord`, and R↔IJK agreement where both are
      expressible (minor arcs, deliberately excluding the full circle R cannot express).
      **The G18 trap is resolved by construction, not by convention.** "G2 decreases the angle" only
      holds in a *right-handed* frame, and `X × Z = -Y` — so G18's frame is **(Z, X)** with first
      offset **K**. An (X, Z) frame is left-handed and silently reverses G2/G3. A test asserts
      right-handedness for all three planes, which is the property rather than the instance.
      **Removed a duplicated direction-sensitive table:** `verify/checks/geometry.py` now derives its
      axis pair from `interpolate.PLANES` instead of keeping its own copy. Its checks only measure
      distances, where order is irrelevant, so borrowing costs nothing and removes the drift hazard.
      **R-format centre selection checks itself against the definition** rather than applying a
      hand-derived sign rule: both candidate centres are computed and the one whose sweep matches the
      R sign is chosen. Verified R=+15 gives the minor arc and R=-15 the major.
      **Rotary step sizing implemented here rather than deferred to T4.3**, using
      `tolerance.rotary_chord` at the path's distance from the centerline. PLAN shapes M2 around the
      4-axis model precisely so M4 is a transform and not a rewrite, and endpoint-only interpolation
      of a rotary move would draw a wrapped path as a chord. **T4.3 is therefore reduced to the
      transform itself.**
      **Mutation-verified, 7 mutations, and two needed better tests:**
      • The G18 (X,Z) frame fails 4 tests; a fixed step count fails 4; inverted R sign fails 3; a
      flat helix fails 1; a zero full-circle sweep fails 1; disabled rotary subdivision fails 2.
      • **Removing endpoint snapping failed nothing at first.** On a mathematically consistent arc
      the trigonometry reproduces the endpoint bit-exactly, so the test could not discriminate.
      Re-pointed at the case that actually matters — an endpoint rounded off its own circle, as CAM
      emits — plus a test that consecutive arcs leave no gap. Both now catch it.
      **One flaky failure observed and diagnosed:** a full-suite run under heavy load failed once and
      did not reproduce. Sampling five runs showed the parse rate swinging 92.6k–109.2k (18%) while
      the linearity ratio held at 1.24–1.31, so the parse-rate floor is the fragile assertion — which
      corroborates the 14% Windows CI margin already recorded. Not loosened: it is PLAN's
      requirement, and `FOURSIGHT_PERF_MIN_RATE` exists for a runner that cannot meet it.
      Blocked by: T2.2
      Files: `src/foursight/sim/interpolate.py`, `src/foursight/verify/checks/geometry.py`,
      `tests/test_arcs.py`, `tests/test_checks_geometry.py`, `PLAN.md`

- [x] **T2.4 — `sim/timing.py`** — *done*
      Per-segment durations combining linear and rotary as `max(linear_time, rotary_time)`, all four
      feed modes, per-axis rapid rates. Recorded in PLAN.md § Timing.
      **DoD met:** 27 tests in `tests/test_timing.py`; **674 across the suite**. Both DoD cases are
      asserted with numbers chosen so the wrong answers are far apart: a pure-A move is timed from the
      rotary rate alone (3 s), and a mixed move takes the max (60 s) where a norm gives 360.1 s.
      **Two real bugs found by my own expected values, both instances of the mm/degrees conflation:**
      1. **`F` treated as the rotary rate on a *mixed* move.** On a rotary-only move F is degrees/min,
         but on a mixed move F governs the linear path and A is bounded only by its own `max_rapid`.
         The bug made 100 mm + 3600° at F600 take **360 s instead of 60 s** — and 360 s is close
         enough to the norm's 360.1 s that it would have read as a norm bug rather than a rate bug.
         The test now uses values that separate all three possible answers.
      2. **`limits.max_feed` (documented mm/min) was clamping a degrees-per-minute rate.** F9000 on a
         rotary-only move came out at 3000 deg/min, taking 72 s instead of 60 s. The linear and rotary
         clamps now apply to the unclamped F independently. Worth noting the invariant can be violated
         in the *limits*, not only in the geometry.
      **G93 is a block time, not a rate** — shared along the path by length, so the speed stays
      constant; an even split would make the tool appear to slow through a finely tessellated arc.
      **Unknown rates are 0.0 and counted**, never invented, so a timeline can say how much of itself
      is missing. A stationary segment is distinguished from an undeterminable one.
      **Mutation-verified, all eight caught:** the forbidden norm (3 tests), `min` instead of `max`
      (14), ignoring rotary entirely (7), G93 as a rate (3), G93 split evenly (1), a vector rapid rate
      instead of per-axis (1), re-applying `max_feed` to the rotary rate (1), and inventing 1000 for
      an unknown rate (3).
      Blocked by: T2.3
      Files: `src/foursight/sim/timing.py`, `tests/test_timing.py`, `PLAN.md`

- [x] **T2.5 — `sim/simulator.py`** — *done*
      Steps commands into a `SegmentStore` in machine coordinates, composing T2.2–T2.4. Recorded in
      PLAN.md § Simulator.
      **DoD met:** 30 tests in `tests/test_simulator.py`; **704 across the suite**. All ten fixtures
      simulate and validate; the baseline yields 129 segments over 22.58 s with nothing suppressed.
      **A real bug found, and it is the quietest possible form of the failure this project exists to
      prevent.** `MachineState` treated "position unchanged" as "no motion" — but an arc whose
      endpoints coincide is a **full circle**, precisely the case IJK can express and R cannot. So
      **every full circle was silently dropped**: no error, no diagnostic, just a missing circle. Now
      drawn: 71 segments at exactly radius 10, closing on itself. Regression test added.
      **Spans are line ranges, not a seventh store column** — `line[i]` already exists for exactly
      this, so `unverified_mask()` is one comparison and PLAN's six columns stand.
      **A decision I reversed after implementing it, which is worth recording.** I first refused to
      draw any move whose start position was unestablished, on the grounds that drawing from a
      fabricated origin invents a line. That is true but the cure is worse: the first move along
      *each* axis is then undrawable, so a program that never mentions Y renders **empty**. Settled
      on assuming the machine starts at its reference — the universal convention — and *not* noting
      it, since a note on every program says nothing. The cut geometry is identical either way.
      **Kept distinct: an assumed start vs a lost position.** `position_lost` marks the state after an
      undrawable G28, and those moves are suppressed, because there we had a position and no longer
      do. Both directions are tested.
      Also moved `CANNED_CYCLE_CODES` into the parse layer: `sim` needs it and cannot import
      `verify` without inverting the dependency direction, so `structural.py` now borrows it.
      **Three of my own test bugs fixed along the way:** a progress callback bound to `list.append`
      (which takes one argument), an off-by-one source line after G80, and a profile fixture with a
      duplicate `[axes.x]` table, which TOML forbids.
      Blocked by: T2.4
      Files: `src/foursight/sim/simulator.py`, `src/foursight/machine/state.py`,
      `src/foursight/parser/model.py`, `src/foursight/verify/checks/structural.py`,
      `tests/test_simulator.py`, `PLAN.md`

- [x] **T2.6 — Batched GL viewport** — `gui/batching.py`, `gui/viewport3d.py` — *done*
      Split in two: `batching.py` holds the logic and **imports no Qt**; `viewport3d.py` is the thin
      widget. Recorded in PLAN.md § Batching Layer.
      **DoD met:** ≤ 10 buffers grouped by kind — in practice **4**, one per (kind, trust) pair, and a
      500k all-feed program renders in **one** draw call. Rapids red, feeds green, colour-only per D3.
      Width is 1.0 everywhere and nothing depends on it, because pyqtgraph skips `glLineWidth`
      entirely on core forward-compatible profiles.
      **46 tests; 797 across the suite. 32 of the 46 need no Qt**, which is the reason for the split:
      what can be *wrong* about rendering is which segments land in which batch, and that should not
      require a display to test. The `headless` CI job (no `[gui]` extra) runs those 32 and skips the
      rest.
      **Verified against a real GL context, with production code**: **241 fps median at 500,070
      segments**, worst frame 10.7 ms against the 33.3 ms a 30 fps floor allows — ~8x margin, measured
      through `ToolpathViewport` rather than the spike's own batching, with the host under load ~4.0.
      **Found a real bug in my own first version**, and it is the worst kind: `_rebuild_items` called
      `clear_toolpath`, which reset `self.batches` to `[]` *before* the loop that reads it, so **no GL
      items were ever created and every program rendered as an empty scene** — no exception, no
      warning, just a viewport that looks like a program with no geometry. Caught by the offscreen
      smoke run, which is why `test_viewport.py` now exists rather than leaving this to the manual
      script: Qt's `offscreen` platform cannot create a GL context but *can* construct widgets and add
      items, enough to assert one GL item per batch and no stale items after a reload.
      **Corrected a PLAN memory figure that my own T2.11 had set too low.** T0.7 and T2.11 both
      measured geometry *only* and agreed at 38.5 MB; neither counted the **float32 copy GL requires**,
      12.0 MB at 500k. The real end-to-end total is **50.5 MB against a 50 MB budget** — quietly
      exceeded. The copy is irreducible (float64 store for precision, float32 for GL), so PLAN now
      says **55 MB** and `test_batching.py` asserts the *combined* number, the only place both halves
      are in scope.
      **Mutation-verified, and one survivor was a hole in my own test:** styling untrusted spans
      exactly like trusted ones **passed all 44 tests**, because the only colour comparison pitted an
      untrusted *feed* against a trusted *rapid* — green != red held while the tier had collapsed.
      Now compared per kind, plus a parametrized test asserting `build_batches` applies the untrusted
      palette for both kinds. Caught: OR instead of AND in the partition (6 tests), no float32
      conversion (3), a wrong-length mask accepted (1), rotary leaking into the linear bounds (1).
      One mutant is *equivalent*, not missed: removing the outer empty-trust-group guard changes
      nothing, since the inner loop already skips empty groups — it is an optimization, not a check.
      **Not green at the moment, and not because of this change:** `test_parse_rate_meets_the_plan`
      and `test_tokenize_rate_is_recorded` fail under sustained host load (~4.0), measuring 35–49k
      lines/sec against the 50k floor where an idle machine gives 96–134k. Proof it is noise rather
      than regression: in one run `tokenize` measured *faster than the full parse that contains it*,
      which is impossible for correct code. Everything else passes — **780 in 5.1 s** with the perf
      module excluded. The threshold is PLAN's requirement and was left alone; `fastest()` already
      takes the best of three, which cannot help when the contention lasts the whole run.
      Blocked by: T2.5, D1 *(D1 resolved by T0.7 — pyqtgraph holds)*
      Files: `src/foursight/gui/batching.py`, `src/foursight/gui/viewport3d.py`,
      `tests/test_batching.py`, `tests/test_viewport.py`, `tests/test_perf.py`, `PLAN.md`

- [x] **T2.7 — Qt shell** — `gui/app.py`, `gui/main_window.py`, `gui/session.py` — *done*
      Split again: `session.py` holds the pipeline and the disclosure logic and **imports no Qt**;
      `main_window.py` is the widget. Recorded in PLAN.md § GUI Shell.
      **DoD met:** open a file (dialog, `Ctrl+O`, or `foursight-gui part.nc`), see the toolpath, orbit
      /pan/zoom via `GLViewWidget`, plus Reload (F5) and Fit (Ctrl+0). New `foursight-gui` entry point.
      **49 tests; 848 across the suite, fully green. 33 of the 49 need no Qt.**
      **The governing principle reaches the user here or nowhere**, so the summary separates two things
      that are easy to conflate. *Suppressed* geometry is **missing**, so a banner appears saying the
      toolpath is incomplete. *Unverified* geometry is drawn but untrustworthy, so `incomplete` stays
      false and the warning goes to the status bar — raising the banner there would fire it on a large
      share of real programs, which is exactly how a warning stops being read. Both directions are
      tested.
      Also disclosed: parse errors, latin-1 fallback, segments with no usable feed rate (a short time
      estimate is never presented as complete), and simulator notes such as unmodelled G43. Cycle time
      reads `1h 05m`, not seconds, because it gets compared against a job sheet.
      **A failed open leaves the loaded program untouched and on screen**, name still in the title bar.
      Clearing the viewport would lose the user's program to a mistyped filename.
      **Qt is imported inside `main`, not at module scope** — a console script imports the module to
      find `main`, so a top-level Qt import would turn a missing `[gui]` extra into a
      `ModuleNotFoundError` traceback before any of our code runs. Now one sentence and exit code 3,
      and a subprocess test asserts importing `foursight.gui.app` pulls in no Qt.
      **Found and fixed a pre-existing M1 bug, outside T2.7's scope but on its path:**
      `load_profile_text` wrapped `tomllib.TOMLDecodeError` into `ProfileError` and `load_profile` did
      **not** — and the path variant is the one `--profile` reaches. So a typo in a hand-edited profile
      crashed `foursight check` with a raw tomllib traceback, despite the CLI documenting exit code 2
      for an unusable profile. Fixed in `machine/profile.py` so both entry points benefit, with the
      error now naming the file; regression tests added at the profile layer and the CLI, not only
      where the GUI happened to find it.
      **Mutation-verified, all 7 caught:** nothing ever incomplete (banner never shows), unverified
      treated as incomplete (banner cries wolf), suppressed spans producing no warning text, banner
      text never set, open failures swallowed, the viewport never given geometry, and simulator notes
      dropped.
      **Two of my own slips:** a duration test reached through `__globals__` for a private function
      instead of importing it, and it expected `9.25 -> "9.3s"` when banker's rounding gives `"9.2s"` —
      a rounding tie tests nothing here, so the case is now 9.26. Also balanced the wait cursor: it was
      restored in both an `except` branch and a `finally`, popping Qt's cursor stack twice for one push.
      Simulation still runs on the GUI thread, so 100k lines freezes the window ~4.6 s — T2.9's job.
      Blocked by: T2.6
      Files: `src/foursight/gui/session.py`, `src/foursight/gui/main_window.py`,
      `src/foursight/gui/app.py`, `src/foursight/machine/profile.py`, `pyproject.toml`,
      `tests/test_session.py`, `tests/test_main_window.py`, `tests/test_gui_app.py`,
      `tests/test_profile.py`, `tests/test_cli.py`, `PLAN.md`

- [x] **T2.8 — Interpolated-point limit checking** — *done*
      `Program` gained an optional `segments: SegmentStore`. When present, `axis-travel-exceeded` and
      `rotary-travel-exceeded` check every interpolated point; without it they fall back to block
      endpoints, so `foursight check` still works headless. Recorded in PLAN.md § Verifier Rules.
      **DoD met:** 16 tests in `tests/test_checks_interpolated.py`; **720 across the suite**.
      **The case PLAN describes, now demonstrated:** an arc with both endpoints at Y90 inside a Y100
      limit reaches **Y110** mid-sweep. The endpoint check reports **nothing**; the interpolated check
      reports one error naming 110 mm. Every test here uses geometry where the two checks *disagree* —
      an arc violating at its endpoints would prove nothing about interpolation, so there is also a
      test asserting the endpoints really are inside the limit.
      **Aggregated to one diagnostic per (line, axis)** at the most extreme value: the demonstration
      arc has 66 offending points, and a 500k-segment program would otherwise emit thousands of
      identical diagnostics. Reporting the *worst* value rather than the first keeps it actionable.
      **`foursight check` now simulates by default**, with `--no-simulate` to opt out. Leaving the
      capability unwired would have meant shipping a check nothing calls; the flag preserves the fast
      path for very large files, and the help text says plainly what it gives up.
      **An empty store falls back to endpoints rather than skipping the check** — a program whose
      geometry was entirely suppressed must not read as having no violations.
      The `error → warning` downgrade on an unknown work offset carries over. Since `lin` is already
      machine coordinates, the check needs no offset arithmetic but still needs to know whether those
      coordinates rest on a configured offset, so that is resolved per source line: a program may mix
      a configured G54 with an unconfigured G55.
      **Mutation-verified, all six caught:** silently falling back to endpoints (5 tests), examining
      only one endpoint per segment (7), reporting the first offending point instead of the worst (2),
      dropping the downgrade (1), removing aggregation (8), and treating an empty store as nothing to
      check (1).
      **Two of my own errors along the way:** an arc swept the wrong way (G3 from 180° bulges *down*,
      so it never approached the limit I was testing), and `np`/`is_machine_absolute` used in
      annotations, which are evaluated at def-time and so broke the import rather than just linting.
      Blocked by: T2.5
      Files: `src/foursight/verify/checks/geometry.py`, `src/foursight/verify/rules.py`,
      `src/foursight/cli.py`, `tests/test_checks_interpolated.py`, `PLAN.md`

- [x] **T2.9 — Simulation off the GUI thread** — `gui/background.py` — *done*
      QThread with progress reporting and cancellation. Recorded in PLAN.md § Performance Requirements.
      **DoD met, measured:** `open_file` returns in **0 ms** where it previously blocked ~4.6 s, and on
      a real 100k-line load the event loop regained control **513 times** during the 4.7 s it took.
      Progress reports two stages — `Parsing` (indeterminate, since it has no measurable extent) and
      `Simulating` (block counts) — behind a status-bar bar and a Cancel button.
      **38 tests (12 Qt-free in `test_cancellation.py`, 26 in `test_main_window.py`); 872 across the
      suite.** The Qt-free half is the half that matters: `simulate` takes `progress` and
      `cancelled() -> bool`, which `threading.Event.is_set` satisfies exactly, so no Qt type crosses
      into `sim/`.
      **A cancelled run raises rather than returning a partial `Simulation`** — the decision the design
      rests on. A half-stepped program is a truncated toolpath, and handing one back invites drawing it
      as though the program ended there. Cancellation is also checked *between parse and simulate*,
      because parsing is ~a quarter of the wall clock with no progress seam, so cancelling during it
      must not still wait out the simulation.
      **A second Open supersedes the first**: the older loader is disconnected before being cancelled,
      so a late `loaded` cannot draw a file the user has moved on from. `closeEvent` stops a running
      load, since a QThread outliving its parent widget turns a clean exit into a crash.
      **`SimulationCancelled` is deliberately not named `...Error`** (ruff N818, with a noqa and the
      reason): cancellation is a normal outcome the user asked for, like `StopIteration`, and calling it
      an error pushes callers toward reporting a problem to someone who just pressed Cancel.
      **Found a real Qt bug in my own first version, via a vacuous assertion.** `_set_busy` used
      `setVisible(False)`, which **does not work on a status-bar permanent widget** — `QStatusBar`
      re-shows everything it manages on every reformat, and showing a message causes one. The progress
      bar and Cancel button would have stayed on screen for the rest of the session: a finished
      application looking permanently busy, with a Cancel button that did nothing. Now uses
      `addPermanentWidget`/`removeWidget`, Qt's documented way to hide one.
      The bug hid because my tests asserted `isVisible()`, which is False for **any** widget whose
      window was never shown — so they passed while checking nothing. Mutation testing exposed it
      (removing the hide changed no test result), and `isVisibleTo(window)` is the correct check, which
      the T2.7 banner tests already used. Six vacuous assertions replaced; a grep confirms none remain.
      **Mutation-verified, all 6 caught** after that fix: mid-run cancellation ignored, a cancel in the
      final partial interval ignored, a cancel during parsing waiting out the simulation, a cancelled
      load wiping the previous program, a superseded loader left connected, and the progress widgets
      never hidden.
      Blocked by: T2.5
      Files: `src/foursight/gui/background.py`, `src/foursight/gui/main_window.py`,
      `src/foursight/gui/session.py`, `src/foursight/sim/simulator.py`,
      `tests/test_cancellation.py`, `tests/test_main_window.py`, `PLAN.md`

- [x] **T2.10 — `tests/test_golden.py`** — *done*
      A fingerprint per fixture in `tests/golden/segments.json`. Recorded in PLAN.md § Testing
      Strategy.
      **DoD met:** 21 tests; **740 across the suite**. All 10 fixtures have goldens, and a test
      asserts the golden file and the fixture directory stay in step — a fixture added without a
      golden would be silently unprotected.
      **A bare hash is a poor golden**, so the fingerprint pairs `geometry_sha256` with readable
      fields (segments, per-axis bounds, duration, rapid/feed, spans) and a mismatch **names the field
      that moved**. Verified against injected regressions: tessellation one step coarser fails 6
      fixtures reporting `segments: 128 -> 125`; a silent 0.1 mm shift fails 10 reporting
      `X: [0.0, 50.0] -> [0.0, 50.1]`. That is exactly the "refactor silently moves the toolpath"
      failure PLAN says is otherwise invisible.
      **Quantized to 1 µm / 0.001° before hashing** — 10× finer than the chord tolerance so a real
      change cannot hide, ~10 orders of magnitude coarser than float64 noise so a different libm's
      `cos` cannot break the build. Both directions are tested: a 0.01 mm shift changes the hash, a
      1e-9 shift does not. Windows CI is the real exercise of that.
      **`kind` and `line` are hashed unquantized**, with tests: a rapid reclassified as a feed, or a
      segment attributed to the wrong source line, are regressions as real as a moved coordinate and
      would change no coordinate at all.
      **Spans are in the fingerprint too**, because a canned cycle silently becoming *drawn* would
      change no existing coordinate — it would add geometry that should not exist.
      Re-record with `FOURSIGHT_UPDATE_GOLDEN=1`; the failure message says so, and says to read the
      diff first.
      Blocked by: T2.5
      Files: `tests/test_golden.py`, `tests/golden/segments.json`, `PLAN.md`

- [x] **T2.11 — Extend `test_perf.py`: segment budget + memory** — *done*
      16 tests in `test_perf.py` (was 6); **750 across the suite**. Recorded in PLAN.md
      § Performance Requirements.
      **DoD met:** geometry measured **through the real pipeline** at 500,070 simulated segments —
      **38.5 MB, 77 B/segment**, against the 50 MB budget. T0.7 only measured a directly-filled
      builder; this is the stronger claim that nothing per-block crept in between parser and store.
      Per-fixture segment counts are recorded as a table (exact counts stay pinned by T2.10's
      goldens, which fail with a diff — duplicating them here would mean two places to update).
      **The measurement inverted the assumption.** PLAN says the 100k-line and 500k-segment targets
      are "different axes", and they are, but the difficulty runs the opposite way: 500k segments
      simulate in **0.18 s** (2.7M segments/sec) while 100k CAM lines take **3.46 s** (22.7k
      segments/sec). A 120x spread on the same code. **The cost is ~40 us per motion block**,
      near-independent of the geometry produced, so the segment target has ~25x margin and the
      *line* target is the binding one.
      **This puts T2.13's gate at risk, which is why it is worth knowing now:** parse + simulate for
      100k lines is **4.56 s** and the gate is "parses and renders within 5 s" — the whole budget is
      spent before rendering starts. `cProfile` localizes it: 35% of simulate is `_durations`, doing
      two `np.stack` and ~6 reductions **per block** on length-1 arrays (`np.stack` called 71,428
      times for a 50k-line file). A single-segment fast path should recover most of it. **Not done
      here** — this is a measurement task and PLAN sets no simulate target — see PLAN.md for sizing.
      **Two of my own tests were wrong and were fixed:**
      - The per-block/per-segment test originally *asserted the ratio*, which would have failed the
        day someone fixed the overhead. A test that punishes an improvement is worse than none, so it
        now asserts a floor on tessellation throughput (the real property: bulk, never per-segment)
        and merely records the ratio.
      - The fixture-count test ended in `assert all(count >= 0)`, a tautology. Now asserts no fixture
        produced *zero* geometry, which would mean the pipeline had broken for it while the table
        still looked plausible.
      **Mutation-verified**, including one that survived and taught something: dropping a column from
      `nbytes()` is caught; fixed-count tessellation is caught; a size-dependent extra column is
      caught (`77.0 -> 125.0 B/segment`). But **padding the store to a power of two survives every
      size check** — `len(store)` derives from the arrays, so a padded store reports the padding *as
      segments* at an unchanged 77 B/segment. Those phantom origin segments would be drawn; the
      goldens and `test_segments.py` catch it, no memory test can, and the docstring now says so
      rather than claiming otherwise.
      Wall-clock thresholds are deliberately loose: the gate seconds carry only a 60 s runaway guard,
      because the parse floor already sits at 14% margin on Windows CI and one flaky cross-platform
      time assertion is enough. The regression duty sits on the blocks/sec floor instead.
      Blocked by: T2.10
      Files: `tests/test_perf.py`, `PLAN.md`

- [x] **T2.12 — Manual GUI test script** — `docs/manual_tests/m2.md` — *done*
      Six sections, ten minutes. Scoped to what a human eye is the **only** instrument for: Qt's
      `offscreen` platform has no GL context, so `paintGL` never runs in CI and pixels are unverifiable
      automatically. Every step that could be automated has been, which is why 872 tests exist; the
      document says so and says such steps should leave it.
      Covers the things that look fine at a glance and are not: **arcs reading as curves rather than
      facets** (with the note that faceted geometry plus passing goldens means the goldens were
      re-recorded when they should not have been), **the banner clearing completely** on the next load,
      **no banner for cutter comp** (warning "incomplete" on a construct most real programs use is how
      a warning becomes worthless), **window responsiveness during a 100k-line load**, and **a cancel
      leaving the previous toolpath rather than a partial one**.
      Every factual claim in it was verified rather than written from memory — banner text naming lines
      9–12, cutter comp not flagged incomplete, the red/green/amber palette, and the 85,715-block count.
      Ends with what is *deliberately* absent in M2, so a tester does not file M3–M5 gaps as bugs.
      Blocked by: T2.7
      Files: `docs/manual_tests/m2.md`

- [x] **T2.13 — Milestone gate** — *M2 COMPLETE*
      **DoD met, on the D5 baseline hardware (Intel Iris Xe, Mesa 25.1.5), numbers recorded:**

      | Criterion | Measured | Margin |
      |---|---|---|
      | 100k-line file parses and renders ≤ 5 s | **4.46 s** to first frame drawn | 11% |
      | Sustains ≥ 30 fps while orbiting | **316 fps**, worst frame 4.99 ms | 10.5x |

      Measured end to end through `MainWindow` — open the file, wait for the background load, force a
      `paintGL` with `glFinish`, then orbit 120 frames and discard 20 as warmup. 100,000 lines →
      85,715 blocks → 81,900 segments in 2 batches; geometry 6.3 MB + 2.0 MB of GL buffers.
      **This settles the concern raised in T2.11 and carried through T2.9.** Parse + simulate is 4.56 s
      measured in isolation, so the gate looked likely to fail once rendering was added. It does not:
      rendering costs ~20 ms, and the gate passes as written. The `_durations` fast path sized in
      PLAN.md (35% of simulate, spent on `np.stack` over length-1 arrays) is **not needed for M2** and
      stays available as headroom — worth having if slower hardware ever has to meet this, since 11% is
      not much and the figure is dominated by simulation rather than rendering.
      Also recorded at the 500k-segment target: **241 fps**, worst frame 10.7 ms, geometry + GL
      38.5 + 12.0 MB against the 55 MB budget.
      **872 tests pass**, ruff clean, and the matrix went green on Linux and Windows for py3.11/3.12
      once the shared-runner parse floor was corrected.
      Blocked by: T2.11, T2.12
      Files: `PLAN.md`, `TASKS.md`

- **CI follow-up (after the first matrix run in eleven commits, 2026-08-07)** — *done*
      The queue finally drained and produced one real failure plus one hidden hole.
      **The parse floor is a coin flip on shared runners.** All four legs measured 47–50k lines/sec
      against the 50k floor: ubuntu-py3.11 passed, windows-py3.11 failed by **0.4%** (49,784),
      ubuntu-py3.12 48,521, windows-py3.12 47,101. This machine does 95k. CI now sets
      `FOURSIGHT_PERF_MIN_RATE=30000` — the remedy T1.12's docstring already specified — which keeps
      PLAN's 50k as the requirement for real hardware while still catching a halving. Lowering PLAN
      instead would let a 2-vCPU runner dictate a product requirement.
      **The console-script test had never run on Windows.** An earlier fix handled `foursight` vs
      `foursight.exe` but not the *directory*: it looked in `Path(sys.executable).parent`, which holds
      scripts in a Linux venv but on Windows sits one level *above* `Scripts\`. So it skipped on the
      one platform where a console-script shim is most likely to be what breaks. Now searches
      `sysconfig.get_path("scripts")` first, and — more importantly — **asserts instead of skipping**
      when a declared entry point has no executable, so the hiding place is gone. Verified by
      simulating the Windows layout: the test fails with the searched paths named. Also covers
      `foursight-gui`, which T2.7 added.
      **`-rs` added to the matrix pytest**, which is why that skip stayed invisible for eleven
      commits — only the headless job printed skip reasons.
      **Confirmed by the same run:** all 20 golden comparisons passed on `windows-latest` under a
      different libm, validating the 1 µm / 0.001° quantization grid against the exact risk it was
      chosen for. The `headless` job passed too, so the Qt-free split holds — 32 batching tests ran
      with no Qt installed while the viewport tests skipped cleanly.
      Files: `.github/workflows/ci.yml`, `tests/test_cli.py`, `PLAN.md`
      **A third perf threshold turned out to be a coin flip — found by the very run that fixed the first
      two.** With the parse floor corrected, 5 of 6 jobs went green and ubuntu-py3.11 failed
      `test_parse_time_is_linear_in_file_size` at **2.07x** against a `< 2.0` bound, where this machine
      measures 1.22–1.35x. That bound also contradicted the test's own docstring, which says it looks for
      an order-of-magnitude change: 2.0 is not one, and quadratic behaviour at 10x input would show as
      roughly 10x per-item growth. Both linearity tests (parse and simulate) now share one documented,
      overridable `MAX_NONLINEARITY_RATIO = 4.0` — still catching the pathology while leaving room for
      what legitimately worsens with size: allocator pressure, GC, cache misses.
      **Process note:** this entry was accidentally deleted while rewriting the T2.9 task, because it sat
      between T2.9 and T2.10 and the edit replaced that whole range. Restored from `c489c79` and moved to
      the end of M2, where a range-replace on a task cannot reach it.

---

## M3 — Editor sync + diagnostics UI

- [x] **T3.0 — Decide the picking strategy** *(resolves D4)* — *done*
      `spikes/picking.py`, run against real `GLViewWidget` matrices. Recorded in PLAN.md
      § Picking Strategy **before** T3.3, as the task required.
      **Chose a third option neither candidate covered: CPU screen-space distance to the segment, with
      the projection cached per camera change.** Per-click cost at the 500k target is **27.3 ms** (warm),
      versus 93.9 ms if it reprojects on every click; 0.45 ms at 10k, 5.79 ms at 100k. A click projects
      nothing — the camera does not move between the user stopping an orbit and clicking, which is what
      makes the cache sound. float32 was tried and does not help (48.0 vs 47.7 ms): the matmul dominates.
      **Midpoints were rejected on accuracy, not speed** — the flaw is baked into D4's phrasing. Clicking
      1 px from the end of a 100 mm rapid, the nearest *midpoint* belongs to a different segment 28.7 px
      away, so a KD-tree returns the wrong source line. Demonstrated in the spike rather than argued.
      Distance to the segment itself costs one extra clamp, and needs no scipy.
      **GPU colour-pick was rejected on cost and on fit** — a per-vertex colour buffer (4 MB at 500k as
      uint8) purely so clicking works, plus an extra render pass and framebuffer readback per click, and
      it contradicts the one-colour-per-batch choice that keeps GL memory at 12 MB rather than 28 MB.
      **Its exact occlusion is a drawback here, not an advantage:** a wireframe toolpath has no surfaces,
      and wanting the line *behind* another line is ordinary. "Nearest on screen, front-most among
      candidates within the pick radius" is the better semantic; depth only breaks ties.
      Recorded risk: **57 ms per click at 1M segments** is noticeable. That is 2x the target with 4x
      margin at 500k; the lever if it ever matters is a screen-space bounding-box prefilter per chunk.
      Also pinned a third pyqtgraph API that moved since PLAN was written: `projectionMatrix()` now takes
      `(region, viewport)`, and `QMatrix4x4.data()` is column-major.
      Blocked by: T2.13
      Files: `spikes/picking.py`, `PLAN.md`

- [x] **T3.1 — Code pane + syntax highlighting** — `gui/editor.py`, `gui/highlighting.py` — *done*
      Split as before: `highlighting.py` decides what to colour and **imports no Qt**; `editor.py` is a
      `QSyntaxHighlighter` plus a gutter. Recorded in PLAN.md § Editor.
      **56 tests (41 Qt-free in `test_highlighting.py`, 15 in `test_editor.py`).**
      **The rules are the parser's own, not a second opinion.** `highlighting.py` imports `_COMMENT_RE`,
      `_TOKEN_RE` and `_FRAMING` from the tokenizer and applies them in its order. A separate regex would
      drift, and the failure is specific: the editor showing a construct as a valid word that
      `foursight check` rejects. A parametrized test walks **every line of all ten fixtures** asserting
      nothing clean is marked malformed, and its converse asserts everything the parser errors on *is*.
      Reusing the tokenizer made malformed input free, marked with a **wavy underline as well as colour**
      — it is the one role meaning "this will not run", and colour alone fails a colour-blind reader.
      **Two of my own mistakes, both caught by tests I had written:**
      - `%` was marked malformed. The tokenizer consumes bare framing silently, so that was the editor
        contradicting the parser about a construct every Fanuc program starts with.
      - `X (c) 10` produced *overlapping* spans. The word legitimately covers the comment (it means X10),
        so word spans are now clipped around comments — neither hiding the comment nor splitting the word.
      **Two off-by-one traps, handled once so nothing downstream inherits them.** Qt blocks are 0-based
      and every line number in this codebase is 1-based. Worse, `line_count` is **one more** than the
      parser's count for newline-terminated text (`"G1 X10\n"` is 1 line and 2 blocks). Added
      `source_line_count` for comparing against anything the parser produced, with both asserted —
      T3.2/T3.4 must use it or every jump lands one line off, which looks plausible on screen.
      Also found that `setUnderlineStyle(WaveUnderline)` makes `fontUnderline()` report **False**, which
      had my underline test passing for the wrong reason.
      **Mutation-verified, all 7 caught:** malformed treated as valid, N-numbers mis-coloured, framing
      marked malformed, spans not clipped, block delete unrecognised, underline dropped, and
      `source_line_count` off by one.
      Replaced five scattered `# noqa: N802` with one scoped `per-file-ignores` entry for
      `src/foursight/gui/*`: Qt override names (`closeEvent`, `paintEvent`, `resizeEvent`, `sizeHint`,
      `highlightBlock`) are not ours to choose, and gui/ is the only package permitted to import Qt.
      **⚠ This task breaks the M2 gate, measured and recorded rather than absorbed.** `setPlainText` on
      100k lines costs **1.23 s**; with parse + simulate at 4.75 s the total is **5.97 s against the 5 s
      gate T2.13 certified at 4.46 s**. The remedy is the already-sized `_durations` fast path (~35% of
      simulate, ~1.66 s here) — the headroom T2.13 noted was in simulation, not rendering. Until it
      lands, a 100k-line file takes ~6 s to open, and 1.23 s of that is a **GUI-thread freeze**, since Qt
      widgets cannot be written from the T2.9 worker.
      Blocked by: T2.13
      Files: `src/foursight/gui/highlighting.py`, `src/foursight/gui/editor.py`,
      `src/foursight/gui/main_window.py`, `pyproject.toml`, `tests/test_highlighting.py`,
      `tests/test_editor.py`, `PLAN.md`

- [ ] **T3.2 — Click a line → highlight segments** (uses `SegmentStore.line`)
- [ ] **T3.3 — Click a segment → jump to line** — `gui/picking.py`, per D4. Budget real time here.
- [ ] **T3.4 — Diagnostics panel** — click → jump to line; `unsupported` spans visually distinct
      from warnings and errors.
- [ ] **T3.5 — Timeline scrubber** — `gui/timeline.py`, driven by `SegmentStore.duration`.
- [ ] **T3.6 — Manual GUI test script** — `docs/manual_tests/m3.md`

---

## M4 — Rotary kinematics

The data model is already 4-axis-shaped, so this milestone is the transform itself.

- [ ] **T4.1 — `machine/kinematics.py`: table mount**
      `p_part = R_axis(-A) @ (p_tool - centerline) + centerline`. Populate `lin_part`;
      **never mutate `lin`** — doing so silently destroys the ability to verify.
- [ ] **T4.2 — `machine/kinematics.py`: head mount**
      `p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)`. The tip **translates** as the head
      swings; it is not simply the machine XYZ.
- [ ] **T4.3 — Rotary-aware step sizing**
      From `tolerance.rotary_chord`, evaluated at the max distance of the path from the centerline
      (no stock model, so path radius is the proxy for part radius). Simultaneous XYZ+A moves are
      transformed **per step** — endpoint-only transformation draws helical/wrapped paths as
      straight chords, the single most likely source of silently wrong output.
- [ ] **T4.4 — `tests/test_kinematics.py`**
      Compare transformed paths against closed-form expectations (helix on a cylinder), not
      against previously-generated output.
- [ ] **T4.5 — Display toggle** — machine coords vs part coords in the viewport.
- [ ] **T4.6 — Milestone gate:** a 4-axis wrapping program renders as the correct cylindrical path.

---

## M5 — Fixer + packaging

- [ ] **T5.0 — `fix/differ.py`** — unified diff generation and application.
- [ ] **T5.1 — Fix engine + the one-fix contract**
      **Apply exactly one fix → re-parse the whole buffer → re-verify → rebuild segments.**
      No batch application, no diff rebasing: a fix invalidates every line number, so every
      `Diagnostic` and every `SegmentStore.line` entry is stale afterwards. Fixes never write the
      original file — they modify the editor buffer and the user saves explicitly.
- [ ] **T5.2 — Geometry fixes**
      Recompute arc centers (IJK) to the point equidistant from both endpoints along the
      perpendicular bisector, preserving both endpoints — **refuse when the mismatch exceeds
      10× tolerance** (past that, the intent is ambiguous and the "fix" invents geometry).
      R→IJK conversion; IJK→R **refuses on full circles** (inexpressible) and honours the >180°
      sign convention. Tolerance read from the profile, shared with T1.9's check.
- [ ] **T5.3 — Text fixes**
      Safety preamble (`G90 G21 G17` + safe-Z retract, prompted); inject feed rate on first cutting
      move (prompted, user supplies the value); normalize whitespace/case; append M30 if missing;
      strip/renumber N-words **off by default** (operators restart mid-program on N-numbers and
      some dialects use them as jump targets — destructive in ways the diff does not show).
- [ ] **T5.4 — Harden `fileio/loader.py`** — large files, encoding detection, latin-1 fallback,
      BOM, CRLF.
- [ ] **T5.5 — Diff preview dialog** — reviewable before application.
- [ ] **T5.6 — `tests/test_fixes.py`** — including the refusal cases, which are the point.
- [ ] **T5.7 — PyInstaller one-dir builds for Ubuntu and Windows**, smoke-tested on both,
      applying whatever T0.8 learned.
- [ ] **T5.8 — README with screenshots.**
- [ ] **T5.9 — Manual GUI test script** — `docs/manual_tests/m5.md`

---

## Standing invariants (any task may violate these silently)

Checklist to run against a diff before calling a task done. Reasoning lives in `PLAN.md`.

- [ ] Segments stored **columnar**, never per-object dataclasses.
- [ ] `rot` is its own column; **no norm across linear and rotary** components, anywhere.
- [ ] `lin` is machine coordinates, always; display transforms write `lin_part`.
- [ ] Every segment traces to a source line via `line[i]`.
- [ ] Every module outside `gui/` imports without Qt (enforced by the T0.6 Qt-free job).
- [ ] Geometry is numpy float64; internal units mm; G20 converted at parse; **diagnostics reported
      in the program's declared units**.
- [ ] G-codes are strings (`'90.1'`), never floats; multiple G/M words per block live in
      `Command.gcodes` / `Command.mcodes`.
- [ ] `slots=True` on every hot-path dataclass.
- [ ] Dependency direction `parser → machine → sim → verify → fix → gui`;
      `parser/model.py` imports nothing from `machine/`.
- [ ] Nothing motion-affecting is ever drawn as if understood — `unsupported`, not `warning`.
- [ ] No new third-party dependency without a `PLAN.md` Tech Stack update in the same commit.
