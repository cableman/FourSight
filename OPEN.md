# OPEN

Everything still owed, in one place. **This file is the only live record of each item** — `TASKS.md`
keeps the milestone history and points here rather than restating status, because two copies of "is this
done yet" is exactly the drift this codebase refuses everywhere else.

`PLAN.md` owns the design. `TASKS.md` owns the order the work was done in. **OPEN.md owns what is left.**

When an item closes: delete it here, and tick it where `TASKS.md` points at it. When a new one opens —
including anything found by launching the application and looking at it — add it here first.

Project status: **M0–M17 complete except T0.8/T0.9.**

| # | Item | Kind | Blocked by |
|---|---|---|---|
| [1](#1-the-windows-bundle-has-never-been-launched-t08--t09) | Windows bundle never launched | gate | a clean Windows VM |
| [2](#2-applying-a-profile-under-part-coordinates-raises) | Profile Apply under Part coordinates raises | defect | — |
| [3](#3-processfeed-too-high-has-a-feed-mode-blind-spot) | `feed-too-high` blind to G93 | defect | — |
| [4](#4-a-forgotten-worker-thread-aborts-the-application-on-quit) | Forgotten worker aborts the app on quit | defect | — |
| [5](#5-stepdwell-reaches-no-consumer) | `Step.dwell` reaches no consumer | owed | — |
| [6](#6-the-plunge-check-is-still-z-only) | Plunge check is Z-only on rotary jobs | owed | a design answer |
| [7](#7-neither-m7-rule-has-a-fix) | Neither M7 rule has a fix | owed | — |
| [8](#8-the-profile-dialog-has-no-diff-preview) | Profile dialog has no diff preview | owed | — |
| [9](#9-axestype-is-not-editable-so-axesb-is-invisible) | `[axes.*].type` not editable | owed | 5-axis is post-v1 |
| [10](#10-there-is-no-way-to-save-the-edited-program) | No way to save the edited program | owed | — |
| [11](#11-profilesrotarytoml-asserts-a-controller-setting-the-controllers-own-file-denies) | `rotary.toml`'s `short_rotate` is contradicted by the machine's XML | defect | — |

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

### 2. Applying a profile under Part coordinates raises

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

### 3. `process.feed-too-high` has a feed-mode blind spot

It compares a raw `F` word to `limits.max_feed` under **every** feed mode, so under G93 (inverse time) a
legitimate `F1000` — a 0.06 s block — is reported as *"feed 1000 mm/min exceeds 3000 mm/min"*. A wrong
message, though in the harmless direction.

T7.1 avoided this for the plunge rule, and `process.plunge-feed-too-high` documents the correct treatment
right next to it. Not fixed at the time because it changes existing behaviour and its own tests.

**Done when:** the rule reads the feed mode from the modal snapshot, and G93 blocks are either judged
correctly or skipped with that stated.
*TASKS.md § M7, owed.*

### 4. A forgotten worker thread aborts the application on quit

Found while investigating the old § 2. **Reproduced 3/3, deterministically, in two seconds** — this is a
shipping crash, not a test-harness one:

1. Open a program large enough that the carve takes a moment.
2. `Ctrl+D` to switch the solid view on.
3. `Ctrl+D` again *while it is still carving*.
4. Quit.

```
QThread: Destroyed while thread '' is still running
Fatal Python error: Aborted            (SIGABRT, exit 134)
  File "src/foursight/sim/solid.py", line 431 in _sample_chunks
  File "src/foursight/gui/background.py", line 172 in run
```

`_on_solid_toggled(False)` sets `self._carver = None` while the thread is still running
(`main_window.py:807`) — that is what makes a superseded result droppable, since `_on_carved` compares
identity. But `closeEvent` (`:495-505`) cancels and waits **only** `self._carver`, so a forgotten carve is
never joined, and destroying the window destroys a running `QThread` child. `_start_carve` (`:838`,
*"replacing any carve already running"*) drops a live carver exactly the same way — one defect, two routes.

Nulling the reference is not what makes the result droppable: `_on_carved` already guards with
`not self.solid_action.isChecked()`. `SolidCarver` has no `cancel` by design (`background.py:144-146`), so
the remedy is to **wait**, which `closeEvent` already does — it simply cannot see a worker the window has
forgotten. The window needs to own *every* running worker until it finishes, not just the current one.

The same shape is reachable through the tests, which is the likely mechanism behind the old § 2: a
`MainWindow` left to be garbage-collected while its parented `ProgramLoader` is still parsing aborts with
the identical message, and that stack (`tokenize` under `background.py:91`) is exactly what the § 2 dump
recorded. The suite itself no longer crashes (12 one-process runs green across two revisions, `xcb` and
`offscreen`, `PYTHONMALLOC=malloc`, and forced per-test collection), so § 2 is closed — but this is the
hazard it was describing, and it is live.

Two tests build their own window in the test *body* rather than taking the fixture —
`test_editor.py:226` (`test_loading_a_program_shows_the_parsed_text`, the test the old § 2 named) and
`test_main_window.py:910` — and neither closes it. That is worth knowing before anyone reaches for fixture
teardown again: § 2 records `close()` on the `window` fixture as tried and reverted, and it *could not*
have worked, because `closeEvent` already cancels and waits `self._loader` (`:495-498`) and the windows
that leak are not the fixture's. A teardown assertion that no worker `QThread` is still running — tracked
by patching `QThread.start` into a `WeakSet` — catches a leak whatever built the window, which a fixture
`close()` cannot.

**Done when:** a worker started by the window is cancelled and joined before the window is destroyed,
whichever route dropped the reference, and a test drives the four steps above.

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

### 10. There is no way to save the edited program

Four places state the contract — `PLAN.md` § Fix Engine, `fix/engine.py:17`, `gui/editor.py:8` and
`README.md` § Fixes — and all four say the same thing: *"fixes modify the editor buffer and the user saves
explicitly."* The second half was never built. `main_window.py` contains no `save` at all: `File` is Open,
Reload, Machine profile…, Quit. The only `QFileDialog.getSaveFileName` in the codebase is the profile
dialog's (`profile_dialog.py:233`), which writes the *profile*.

So a fix can be previewed, applied and re-verified, and then the only thing the user can do with the result
is retype it. `tests/test_main_window.py:760` and `tests/test_editor.py:172` both quote the contract while
asserting only the buffer half of it, so nothing fails.

**Not a deferral.** `PLAN.md` § Non-Goals (`:21-38`) defers removal simulation, 5-axis, CAM, macro
languages, canned cycles, coordinate transforms and subprograms — and says nothing about saving. `:1037`
states the contract positively, in the same paragraph that lists the eight fixes. So this is owed work that
was never done, not scope that was declined.

**One thing to settle first:** `:1037` says *"fixes never write to the original file"*, which constrains the
**fix**, not the user — an explicit `Save` over the loaded path is still the user's act, and every editor
works that way. Read the other way it means `Save as…` only. Decide it before writing the menu, because the
two differ in exactly the case that matters: a fixed file the operator meant to keep beside the original.

`LoadedFile.newline` (`fileio/loader.py:60`) exists **for** this — it is the recorded line ending a save is
supposed to restore, and the Windows CRLF defect it was added for is only half-closed until something writes
it back. Editing the buffer by hand has the same problem; this is not specific to fixes.

**Done when:** `File → Save` and `Save as…` write the editor buffer using `LoadedFile.newline`, the window
title or status bar shows unsaved state, quitting with unsaved changes prompts, and a test round-trips a
CRLF fixture through a fix and asserts the bytes on disk.

---

## Defects (found later)

### 11. `profiles/rotary.toml` asserts a controller setting the controller's own file denies

Found while planning M17 (`TASKS.md` § M17, `PLAN.md` § Importing a Mach3 profile), by reading the
machine's Mach3 profiles instead of the comments quoting them.

`profiles/rotary.toml` sets `[axes.a].short_rotate = true` and justifies it in prose: *"Both Mach3
profiles for this machine have `<ShortRot>1<` … Verified in Rotary.xml and Mach3Mill.xml, not assumed."*
`TASKS.md` § M14 cites the same pair as `<ShortRot>1<`, `<Rot360>0<`.

The files today say otherwise:

| | `Rotary.xml` | `Mach3Mill.xml` |
|---|---|---|
| `<ShortRot>` | **0** | 1 |
| `<Rot360>` | **0** | 1 |

`Rotary.xml` is the profile a rotary job runs under, and it has the checkbox **off**. The likely reading
is that the operator cleared it — that was M14's recommended remedy, in the profile's own words: *"Clearing
the Mach3 checkbox is the better fix."* If so the fix worked and the profile was never updated, so
`process.rotary-rapid-short-rotates` now reports three findings on correct Vectric output describing a
hazard the machine no longer has. Warnings nobody can act on are how a rule gets ignored.

Two neighbours are **not** part of this, and saying so matters because they look identical from a distance.
`wrap = true` is not contradicted by `<Rot360>0`: Mach3's "Rot 360" rolls the **DRO** over, while
FourSight's `wrap` claims the axis *turns continuously, so min/max do not bound it*. A table can do the
second with the first switched off, which is this machine. And `dwell_units = "milliseconds"` rests on
`<DwellinMilli>1`, still `1` in both files — that one holds. Only `short_rotate` is stale.

**Done when:** `profiles/rotary.toml`'s `short_rotate` matches `Rotary.xml`, its comment cites what the
file actually says with the date it was read, and M14's citation in `TASKS.md` is corrected. Confirm with
the operator whether the checkbox was cleared before changing the value — if it was cleared *after* the
job that gouged, both readings were true in turn and the comment should say so.
