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
- [ ] **D4 — Picking strategy: GPU colour-pick vs CPU KD-tree.** Decided in T3.0, informed by D1.
      Do not defer past M3 planning; it is not a one-liner at 500k segments in ≤ 10 batches.
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

- [ ] **T1.4 — `fileio/loader.py` (minimum viable)**
      Read a file to text + line offsets. Encoding detection with latin-1 fallback, BOM, CRLF.
      Hardening is M5 (T5.4); M1 needs correct offsets so `SourceRef` is trustworthy.
      **DoD:** tests for UTF-8, UTF-8-BOM, latin-1, CRLF, and a file with no trailing newline —
      each yielding correct 1-based line numbers and offsets.
      Blocked by: T1.1

- [ ] **T1.5 — `machine/profile.py`** *(moved into M1: the verifier cannot run without it)*
      `MachineProfile` from TOML via `tomllib`, matching PLAN.md § *Machine Profile* exactly:
      `[machine]`, `[limits]`, `[tolerance]`, `[axes.*]`, `[offsets]`, `[kinematics]`, `[safety]`.
      Dataclasses, not dicts. Convert profile values to mm on load using `machine.units`.
      **Refuse to load** a `rotary_mount = "head"` profile without `pivot_to_tip`.
      Unset work offsets are represented distinctly from zero — they downgrade limit errors to
      warnings (T1.8), so `None` and `0.0` must not collapse.
      **DoD:** `profiles/default_4axis.toml` loads; tests cover head-mount-without-pivot refusal,
      unset-vs-zero offsets, inch-profile conversion, and unknown-key handling.
      Blocked by: T1.1
      Files: `src/foursight/machine/profile.py`, `profiles/default_4axis.toml`

- [ ] **T1.6 — `verify/report.py` + `verify/rules.py`**
      `Diagnostic(severity, line, message, fix_ids)` — `fix_ids` is a **list**. Severity is the
      three-tier enum: `error` | `unsupported` | `warning`, per PLAN.md § *Diagnostic severity
      taxonomy*. `Rule` base class + registry so checks self-register.
      Diagnostic messages render positions **in the program's declared units**.
      **DoD:** registry test (registering, listing, no duplicate rule ids); a unit-formatting test
      asserting an inch program reports inches.
      Blocked by: T1.3, T1.5

- [ ] **T1.7 — Structural checks** — `verify/checks/structural.py`
      E: syntax/malformed word. E: two codes from one modal group in a block.
      W: unknown/unsupported **inert** G/M code. U: unsupported **motion-affecting** code —
      G40/G41/G42 cutter comp, G80–G89 canned cycles.
      The taxonomy line is the deliverable: an unrecognized code that never touches position is a
      warning; one that changes how subsequent motion is interpreted is `unsupported`, never a
      warning. Canned cycles emit **one diagnostic per cycle span** (open at G8x, close at G80),
      not one per block.
      **DoD:** fixtures for each; a test asserting a canned-cycle span produces exactly one
      `unsupported`, and that a block of bare `X10 Y10` under G81 is *not* classified as a
      linear move.
      Blocked by: T1.6

- [ ] **T1.8 — Process checks** — `verify/checks/process.py`
      All of PLAN.md's Process list: E for cutting move with no feed ever set; G93 active with no
      F on a cutting block; F > `limits.max_feed`; S > `limits.max_spindle_rpm`. W for units never
      set, no work offset before motion, cut before M3/M4, M6 without prior safe-Z retract (when
      `safety.retract_before_toolchange`), M6 with no tool ever set, coolant on with spindle off,
      rapid below `min_clearance_z`, G91 active at program end, no M2/M30.
      **DoD:** one broken fixture per check, each a **single mutation** of the clean baseline
      (T1.10), asserting on the diagnostic *added* relative to the baseline's set.
      Blocked by: T1.6

- [ ] **T1.9 — Sim-free geometry checks** — `verify/checks/geometry.py`
      What is checkable without interpolation: arc radius mismatch beyond
      `tolerance.arc_radius_mismatch`; R-format arc with coincident endpoints (error);
      rotary move > `rotary_wrap_warn` degrees in one block; endpoint-only axis-limit check.
      Tolerance comes from the profile, **one source only** — the check and the fix (T5.2) must
      not each hard-code it. Interpolated-point limit checking arrives with the simulator (T2.8).
      **DoD:** fixtures per check; a test asserting the check and the arc fix read the same
      tolerance value.
      Blocked by: T1.6

- [ ] **T1.10 — Fixture corpus: clean baseline + single-mutation siblings**
      One known-clean 4-axis `.nc` baseline. Each broken fixture is that baseline with **exactly
      one** mutation. Test helper asserts on the *newly added* diagnostic set — never
      "exactly one diagnostic per file" (a file missing G21 also trips "no work offset" and
      "lacks M30"). Plus targeted fixtures for: G18 arc direction, helical arc, full-circle IJK,
      R-format >180°, canned-cycle span, cutter-comp span, inch program, block delete, `%` framing,
      `Oxxxx` header.
      **DoD:** every fixture parses without exception; the baseline yields a known, asserted
      diagnostic set that all mutation tests diff against.
      Blocked by: T1.3
      Files: `tests/fixtures/*.nc`, `tests/conftest.py`

- [ ] **T1.11 — CLI: `foursight parse` / `foursight check`**
      `parse` dumps commands; `check` runs the verifier and reports `severity line message`.
      `--profile` (default `profiles/default_4axis.toml`) and `--block-delete` (default **off**,
      meaning deleted blocks execute, matching the common control-panel default). Exit non-zero
      when errors are present. **No Qt import anywhere on this path.**
      **DoD:** both subcommands run against fixtures; a test invokes the CLI in a Qt-free
      environment.
      Blocked by: T1.7, T1.8, T1.9

- [ ] **T1.12 — `tests/test_perf.py`: parse rate**
      Assert **≥ 50k lines/sec** on a generated large fixture, with enough headroom that CI
      variance does not flake it (assert the floor, log the actual).
      **DoD:** test passes on the T0.9 baseline hardware and in CI; the measured rate is logged.
      Blocked by: T1.3

- [ ] **T1.13 — Milestone gate**
      **DoD:** all fixtures parse; each broken fixture produces exactly its expected *added*
      diagnostic; `foursight check` works end to end; parse rate asserted; CI green.
      Blocked by: T1.10, T1.11, T1.12

---

## M2 — Simulation + viewer

Built on the **4-axis-shaped data model from day one**, kinematics transform as identity until M4.
Re-granulate after D1 is resolved — a `QOpenGLWidget` fallback materially changes T2.5–T2.7.

- [ ] **T2.1 — `sim/segments.py`: `SegmentStore`**
      Columnar parallel numpy arrays exactly as specified: `lin (N,2,3) f64`, `rot (N,2) f64`,
      `kind (N,) uint8`, `line (N,) int32`, `duration (N,) f64`, `lin_part (N,2,3) | None`.
      Never per-object segments. `rot` stays **out** of the position vector. `kind` is motion type
      only (RAPID | FEED) — arcs are already lines after interpolation.
      Amortized growth (chunked append + finalize), since segment count is unknown up front.
      **DoD:** `lin.reshape(-1, 3)` is a zero-copy contiguous view; a test asserts memory for
      N = 500k is within the ~38 MB expectation and that every segment has a nonzero `line[i]`.
      Blocked by: T1.13

- [ ] **T2.2 — `machine/state.py`: `MachineState`**
      Live position + modal groups during simulation. G53 non-modal machine coords, G28/G30
      reference return, G43/G44 with H and G49, G54–G59 offsets, G4 dwell (seconds; warn if
      P > 60 as likely ms/s confusion).
      Blocked by: T2.1

- [ ] **T2.3 — `sim/interpolate.py`: lines and arcs**
      G0/G1 lines. G2/G3 in **both IJK and R** form. R sign convention: positive selects ≤ 180°,
      negative selects > 180°. Full circles are IJK-only (start == end); R-format with coincident
      endpoints is an **error**, not a guess. Plane-dependent IJK mapping G17→I,J / G18→I,K /
      G19→J,K, with G18's counterintuitive direction convention covered by its own fixture.
      Helical: plane-normal axis interpolates linearly across the sweep; an A-word may move
      simultaneously. **Adaptive chord-height tessellation** from `tolerance.arc_chord` —
      never a fixed step count. Interpolation is **per-step from the start**, not endpoint-only,
      so M4 is a transform and not a rewrite.
      **DoD:** `tests/test_arcs.py` with hypothesis properties — interpolated points equidistant
      from center within 1e-6, chord deviation never exceeds `tolerance.arc_chord`, R↔IJK
      round-trips where expressible; plus the G18-direction fixture.
      Blocked by: T2.2

- [ ] **T2.4 — `sim/timing.py`**
      Per-segment duration. Linear and rotary travel measured **separately** and combined as
      `max(linear_time, rotary_time)`. **Never** `np.linalg.norm` across linear (mm) and rotary
      (deg) components — that is the exact expression the columnar split exists to prevent.
      Honour G93 inverse-time / G94 units-per-min / G95 units-per-rev, and per-axis `max_rapid`.
      **DoD:** unit tests per feed mode; a test asserting a pure-A move gets a duration from the
      rotary rate alone, and a mixed move takes the max, not the norm.
      Blocked by: T2.3

- [ ] **T2.5 — `sim/simulator.py`**
      Steps `Command`s → `SegmentStore`. Suppresses geometry across `unsupported` spans (canned
      cycles) and marks cutter-comp spans as unverified rather than drawing them as understood.
      `lin` is written in **machine coordinates, always**.
      Blocked by: T2.4

- [ ] **T2.6 — Batched GL viewport** — `gui/viewport3d.py`
      Pre-batch into ≤ 10 buffers grouped by `kind`; never one draw call per move. Rapids red,
      feeds green (colour-only unless D3 says otherwise). Do not rely on `glLineWidth > 1.0`.
      Blocked by: T2.5, D1

- [ ] **T2.7 — Qt shell** — `gui/app.py`, `gui/main_window.py`
      Open file, view toolpath, orbit/pan/zoom.
      Blocked by: T2.6

- [ ] **T2.8 — Interpolated-point limit checking**
      Re-run the axis-travel check over interpolated points, not just block endpoints — an arc can
      bulge past a limit mid-sweep. Downgrade to warning when the active work offset is unknown.
      Rotary limit enforced when `axes.a.wrap = false`.
      Blocked by: T2.5

- [ ] **T2.9 — Simulation off the GUI thread**
      QThread with progress reporting; cancellable.
      Blocked by: T2.5

- [ ] **T2.10 — `tests/test_golden.py`**
      Hash `SegmentStore` geometry (rounded to tolerance) per fixture. The highest-value regression
      net for a geometry engine: refactors that silently move the toolpath are otherwise invisible.
      Blocked by: T2.5

- [ ] **T2.11 — Extend `test_perf.py`: segment budget + memory**
      Per-fixture segment counts, and ≤ 250 MB resident for a 500k-segment program including
      coordinate arrays and GL buffers.
      Blocked by: T2.10

- [ ] **T2.12 — Manual GUI test script**
      Written steps a human follows to verify the viewport (keep the GUI thin so everything else
      stays unit-testable).
      Blocked by: T2.7
      Files: `docs/manual_tests/m2.md`

- [ ] **T2.13 — Milestone gate**
      **DoD:** a 100k-line file parses and renders within 5 s and sustains ≥ 30 fps while orbiting,
      on the D5 baseline hardware. Numbers recorded.
      Blocked by: T2.11, T2.12

---

## M3 — Editor sync + diagnostics UI

- [ ] **T3.0 — Decide the picking strategy** *(resolves D4)*
      GPU colour-picking to an offscreen target vs a CPU KD-tree over segment midpoints. Qt item
      picking is unavailable at 500k segments in ≤ 10 batches. Record the choice and its cost in
      `PLAN.md` **before** T3.3.
      Blocked by: T2.13

- [ ] **T3.1 — Code pane + syntax highlighting** — `gui/editor.py`
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
