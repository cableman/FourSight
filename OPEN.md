# OPEN

Everything still owed, in one place. **This file is the only live record of each item** — `TASKS.md`
keeps the milestone history and points here rather than restating status, because two copies of "is this
done yet" is exactly the drift this codebase refuses everywhere else.

`PLAN.md` owns the design. `TASKS.md` owns the order the work was done in. **OPEN.md owns what is left.**

When an item closes: delete it here, and tick it where `TASKS.md` points at it. When a new one opens —
including anything found by launching the application and looking at it — add it here first.

Project status: **M0–M16 complete except T0.8/T0.9.**

| # | Item | Kind | Blocked by |
|---|---|---|---|
| [1](#1-the-windows-bundle-has-never-been-launched-t08--t09) | Windows bundle never launched | gate | a clean Windows VM |
| [2](#2-the-full-test-suite-cannot-run-in-one-process) | Full suite segfaults in one process | defect | diagnosis |
| [3](#3-applying-a-profile-under-part-coordinates-raises) | Profile Apply under Part coordinates raises | defect | — |
| [4](#4-processfeed-too-high-has-a-feed-mode-blind-spot) | `feed-too-high` blind to G93 | defect | — |
| [5](#5-stepdwell-reaches-no-consumer) | `Step.dwell` reaches no consumer | owed | — |
| [6](#6-the-plunge-check-is-still-z-only) | Plunge check is Z-only on rotary jobs | owed | a design answer |
| [7](#7-neither-m7-rule-has-a-fix) | Neither M7 rule has a fix | owed | — |
| [8](#8-the-profile-dialog-has-no-diff-preview) | Profile dialog has no diff preview | owed | — |
| [9](#9-axestype-is-not-editable-so-axesb-is-invisible) | `[axes.*].type` not editable | owed | 5-axis is post-v1 |

---

## Gate

### 1. The Windows bundle has never been launched (T0.8 / T0.9)

**The only thing standing between the project and its M0 gate**, and the half of the packaging spike where
the risk actually lives.

The Linux half is done and passing: 410 real files plus 30 symlinks, 265 MB, all four imports succeed,
pyqtgraph's 87 package-data files survive analysis, all nine Qt platform plugins including `libqxcb.so` are
present, a real frame paints on Iris Xe, exit 0. Launched from a neutral working directory so nothing could
resolve out of the source tree. **`HIDDEN_IMPORTS` in `scripts/build.py` is still empty — nothing needed
adding.**

None of that transfers. DLL resolution, the VC++ runtime and AV heuristics all differ on Windows.
`--windowed` has never been exercised on any platform either.

```bash
.venv/bin/python scripts/build.py --entry spikes/gl_window.py --name gl-spike
# copy dist/gl-spike/ to a VM with no Python or dev tooling, then from a TERMINAL:
gl-spike.exe        # 0 painted · 1 created but never painted · 2 an import failed
```

**Done when:** the Windows launch result and any `HIDDEN_IMPORTS` / binary / Qt-plugin-path fix are recorded
in `PLAN.md` and applied to `scripts/build.py`; D2 ticked; T0.9's gate closed.
*TASKS.md T0.8, T0.9.*

---

## Defects

### 2. The full test suite cannot run in one process

`pytest -q` dies with `Fatal Python error: Segmentation fault` at
`test_editor.py::test_loading_a_program_shows_the_parsed_text`, reproducibly (3/3). Run the suite in two
parts and every test passes:

```bash
.venv/bin/pytest -q --ignore=tests/test_dialect.py   # 1777, ~40 s
.venv/bin/pytest -q tests/test_dialect.py            # 43, <1 s
```

**The CI matrix still invokes `pytest -q -rs` in one process, so it will fail as configured.** The M6 matrix
has not been run since.

What the evidence says, so the next person does not rediscover it:

- Pristine HEAD is green 3/3, so this arrived with M6.
- `test_dialect.py` contains no Qt and no threads. It is the *trigger*, not the cause: it only shifts
  allocation timing.
- The faulthandler dump is unambiguous: the **main thread is `Garbage-collecting`** while a background
  `ProgramLoader` QThread is inside `tokenize`. PySide6 destroys Qt C++ objects during that collection
  while the worker is still executing Python.
- Forcing `gc.collect()` after every test gets far past the crash, which fits: the danger is one large
  accumulated collection landing at the wrong moment, not any single object.
- Running everything up to and including `test_editor.py` (417 tests) is green. The crash needs the *whole*
  suite to have been collected, i.e. every test module imported.

**Tried, and did not fix it** — both reverted rather than left in as a half-fix that reads like a solution:
closing the window in `test_editor.py`; giving `test_main_window.py`'s `window` fixture a teardown that
calls `close()` (which is what cancels and joins the loader).

**Not diagnosed:** *which* orphaned Qt object is unsafe to collect. Candidates are the unparented widgets
returned by the `editor` and `panel` fixtures, which are never deleted. The product itself looks careful
here — `MainWindow.closeEvent` cancels and waits for the loader precisely to avoid a QThread outliving its
parent widget, and the real application pumps a true event loop rather than `processEvents` in a tight loop
— so this reads as test-harness fragility rather than a shipping defect. **That should be confirmed, not
assumed.**

**Done when:** `pytest -q` is green in one process on Linux and Windows, and the CI matrix has run.
*TASKS.md § M6, OPEN.*

### 3. Applying a profile under Part coordinates raises

Found while regenerating the README screenshots — no test covers the sequence.

1. Open a program.
2. `Ctrl+P` (Part coordinates).
3. `File → Machine profile…` → **Apply** a profile whose kinematics change the segment count.

```
ValueError: timeline has 504 segments, store has 465 — they describe different programs
  gui/playback.py:143, from MainWindow._on_part_coordinates_toggled
```

`_reload_from_buffer` clears the part-coordinates toggle before the timeline has caught up with the new
store, and `marker_point` refuses the mismatch it is handed. **The guard is right; the ordering is not.**
Qt swallows the exception, so the user sees nothing but a marker that stopped updating.

`scripts/screenshots.py` steers around it deliberately, with a comment saying so.

**Done when:** the toggle is cleared with the timeline and store agreeing, and a test drives the three steps
above.

### 4. `process.feed-too-high` has a feed-mode blind spot

It compares a raw `F` word to `limits.max_feed` under **every** feed mode, so under G93 (inverse time) a
legitimate `F1000` — a 0.06 s block — is reported as *"feed 1000 mm/min exceeds 3000 mm/min"*. A wrong
message, though in the harmless direction.

T7.1 avoided this for the plunge rule, and `process.plunge-feed-too-high` documents the correct treatment
right next to it. Not fixed at the time because it changes existing behaviour and its own tests.

**Done when:** the rule reads the feed mode from the modal snapshot, and G93 blocks are either judged
correctly or skipped with that stated.
*TASKS.md § M7, owed.*

---

## Owed

### 5. `Step.dwell` reaches no consumer

The value is computed, converted and tested, and **the timeline does not include dwell time**. So M6's
dwell-units change is correct and currently unobservable outside tests, and playback skips dwells instantly.

Worth closing when the timeline is next touched.
*TASKS.md § M6, owed.*

### 6. The plunge check is still Z-only

On a rotary job the "plunge" into a bar is **radial**, and for a tool working the side of a blank that can
be a Y move rather than a Z one. `process.plunge-feed-too-high` would not see it.

The stock's axis is now known (M9), so the radial direction is available. But whether a *rate* limit should
be expressed radially is a real design question rather than an oversight — which is why this is recorded
rather than guessed at.

**Needs a decision before it needs code.**
*TASKS.md § M9, owed.*

### 7. Neither M7 rule has a fix

`process.plunge-feed-too-high` has an obvious one — rewrite the `F` on the plunge block — and it would be
**the first fix keyed to a rule whose diagnostic spans two lines**: the plunge, and wherever the inherited
`F` was set. Worth doing deliberately rather than as an afterthought.

`geometry.rapid-into-stock` has no fix and arguably should not: the remedy is a different toolpath, not a
different number.
*TASKS.md § M7, owed.*

### 8. The profile dialog has no diff preview

Every other change FourSight makes to a file is reviewable as a unified diff first (`DiffDialog`) — a
profile edit is not. `ProfileDocument` already holds both texts, so the diff is available; showing it before
`Save as…` would close the gap.
*TASKS.md § M8, owed.*

### 9. `[axes.*].type` is not editable, so `[axes.b]` is invisible

The schema deliberately omits `type`, since the form has no reason to let A become linear. The consequence
is that **a profile using `[axes.b]` cannot be edited in the GUI at all** — it simply does not appear.

5-axis is post-v1, so this is recorded rather than fixed. Note that
`test_every_loader_key_is_editable` covers the flat sections and **would not catch a new axis**.
*TASKS.md § M8, owed.*
