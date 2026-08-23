# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**M0–M15 are complete.** `src/`, `tests/` and `pyproject.toml` all exist; the suite is **1771 tests**
(1727 passed + 1 skipped without `test_dialect.py`, 43 in it).
CI ran green on Ubuntu and Windows for py3.11 and py3.12 through M5; **the M6 matrix has not been run
and will fail as configured**, because the job invokes `pytest -q` in one process — see below. The two open items are **T0.8/T0.9** — launching the
PyInstaller bundle on a clean Windows VM, which needs a VM — and `--windowed` has never been exercised.

**The full suite currently cannot be run in one process.** `pytest -q` segfaults at
`test_editor.py::test_loading_a_program_shows_the_parsed_text`; the main thread garbage-collects
while a background `ProgramLoader` QThread is mid-parse, and PySide6 destroys Qt objects under it.
Every test passes — run `pytest --ignore=tests/test_dialect.py` (1728, ~40 s) and `pytest
tests/test_dialect.py` (43) and both are green. `tests/test_dialect.py` is only the *trigger*: it
contains no Qt and no threads and merely shifts when a large collection lands. See `TASKS.md`
§ M6 for the full evidence and what has already been ruled out. **Run the suite in those two parts
until it is fixed**, and do not read a green `--ignore` run as a green suite.

**Open defect, found while regenerating the README screenshots: applying a machine profile while
`Part coordinates` is on raises out of `_on_part_coordinates_toggled`.** Sequence: open a program,
`Ctrl+P`, then `File → Machine profile…` → Apply a profile whose kinematics change the segment count.
`_reload_from_buffer` clears the part-coordinates toggle before the timeline has caught up with the
new store, and `playback.marker_point` refuses the mismatch it is given —
`ValueError: timeline has 504 segments, store has 465 — they describe different programs`. The guard
is right; the ordering is not. It surfaces on stderr and Qt swallows it, so the user sees only a marker
that stopped updating. No test covers the sequence, and `scripts/screenshots.py` steers around it.

`scripts/screenshots.py` regenerates `docs/images/` for the README (`DISPLAY=:0
.venv/bin/python scripts/screenshots.py`). Refresh it after any visible UI change — a README picturing
a version of the tool that no longer exists is confidently wrong about what the user will see. It
drives **one** `MainWindow` throughout and swaps profiles through `_on_profile_applied`: a second GL
context in the same process leaves the first window's line items undrawable, so a toolpath silently
goes missing from the image with nothing but `Error while drawing item` on stderr.

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

# Dialect overrides; both subcommands accept --profile, --dialect and --arc-centre.
.venv/bin/foursight check file.nc --dialect mach3 --arc-centre absolute
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
- **`sim/`** — interpolates lines and arcs (G2/G3 in both IJK and R form) plus rotary blending into a `SegmentStore`; `timing.py` derives per-segment durations for the timeline; `solid.py` carves the `[stock]` blank into a display heightfield for the M12 solid view.
- **`verify/`** — `Rule` base class with a registry; one module per check category under `checks/`; emits `Diagnostic(severity, line, message, fix_ids)`.
- **`fix/`** — each fix is a transform returning a text diff. Fixes never write the original file; they modify the editor buffer and the user saves explicitly.
- **`gui/`** — thin Qt/PySide6 layer so logic stays testable. Includes `picking.py`, because batched rendering rules out Qt item picking, and `playback.py`, which owns the animation clock and the tool-marker interpolation so the transport widget stays checkable by eye, and `legend.py`, which turns what is on screen into named colour rows, and `solid_mesh.py`, which turns a carved `SolidField` into closed, outward-facing triangles. None of them import Qt.
- **`fileio/`** — deliberately not named `io/`, which shadows the stdlib module.

### Invariants

These are the ones that are easy to violate silently. `PLAN.md` has the reasoning.

- **Segments are stored columnar, never as per-object dataclasses.** `SegmentStore` holds parallel numpy arrays (`lin`, `rot`, `kind`, `line`, `duration`). Per-object segments blow the memory budget ~6× and force a repack before every GL upload.
- **Rotary lives in its own column, out of the position vector.** Never take a norm across linear (mm) and rotary (degrees) components — the result is meaningless, and it's exactly what you'd write for a timeline duration. Combine them in `sim/timing.py` as `max(linear_time, rotary_time)`.
- **`lin` is always machine coordinates.** Verification needs machine coords; table-mount display needs part coords. The display transform writes `lin_part` and never mutates `lin`.
- **Every segment traces back to a source line** via `line[i]`. Editor↔viewport sync, diagnostics, and fixes all depend on this.
- **Every module outside `gui/` must import without Qt installed.**
- **The dialect is resolved once, at the CLI/GUI boundary, into the effective `MachineProfile`.**
  `parse()` takes a `parser.dialect.Dialect` because `parser/` cannot import `machine/`; everything
  downstream derives it from the profile it already carries, via `MachineProfile.parser_dialect`.
  Never store a second copy on `Program`, `FixContext` or `ModalState` — a copy can disagree with the
  profile, and the disagreement surfaces as arithmetically wrong I/J written into the user's file by
  `fix.recompute-arc-centre`, in a diff that looks entirely plausible.
- **`COORD_TRANSFORM_MODES` in `parser/model.py` is the single source for the refused coordinate
  transforms** (G68/G69, G51/G50, G16/G15), and `SUBPROGRAM_MCODES` likewise for M98/M99. `sim`
  decides not to draw them and `verify` reports them from the same table, for exactly the reason
  `CANNED_CYCLE_CODES` lives there: `sim` cannot import `verify`, so a second copy would be a second
  copy of *the refusal decision*, and the two halves disagreeing is how a confidently wrong path gets
  drawn. `CoordTransformMode.field` is three names at once — the `_GROUPS` key, the `ModalState`
  field, and the attribute both layers read — and a rename that misses one makes every span vanish
  silently.
- **The profile editor edits the profile's *text*, never a `MachineProfile`.** Two things break if that
  is reversed. `MachineProfile` values are already mm, so a form populated from one shows 2540 for an
  inch profile's `max_feed = 100.0` and converts it again on write — 25.4× per round trip, silent. And
  regenerating TOML from a parsed profile deletes every comment in `default_4axis.toml`, which is where
  the format is documented. `machine/profile_doc.py` does surgical per-key edits; switching a key off
  comments it out rather than deleting it. Also: **an unset field must write nothing, never `0`**, and a
  section whose keys are mandatory together (`[stock]`) toggles as a unit including its header, or the
  loader refuses the present-but-empty table it would leave behind.
- **`profile_doc.apply_dialect_override` duplicates `with_dialect`/`with_arc_centre`** — the second
  deliberate duplication after `sim/timing.py`'s two paths, because the GUI needs the CLI override as
  *text* or the editor would show the wrong dialect and revert it on the next edit.
  `test_profile_doc.py::test_the_document_override_agrees_with_the_profile_one` drives both over every
  combination and demands identical results **and identical refusals**. Do not weaken it.
- **`[stock]` is an envelope, not a material-removal model**, and `geometry.rapid-into-stock` must stay
  a **warning** because of it. It says where the solid started, never what is left of it, so a rapid
  inside it is legitimate whenever an earlier pass cleared that material — which is ordinary pocketing
  output. Promoting the rule to `error` on the grounds that "a collision would break the machine" would
  make `foursight check` exit 1 on correct programs.
- **A cylinder is rotation-invariant; a box is not.** That asymmetry is the whole reason `[stock]` has
  two shapes, and it decides several things that look arbitrary otherwise. A box under
  `rotary_mount = "table"` is **refused** with one diagnostic once the program moves A, because it stops
  describing stock that turns with the part. A cylinder concentric with the rotary axis maps onto itself
  under every rotation, so it is checked at every angle — which is why its axis comes from
  `[kinematics]` and **cannot** be restated in `[stock]`: an off-axis cylinder loses the invariance and
  would be silently wrong the moment the part turned.
- **Withdrawal is exempt from the stock check, and "upward" is the wrong test for a cylinder.** Every cut
  ends with a retract from inside the material, so without an exemption the rule fires on every pass —
  do not "tidy away" either form. For a box it is a strictly vertical climb. For a cylinder it is
  *radially outward with no axial motion*: a tool working the underside of a bar retracts in **−Z**, and
  a vertical rule would both report that and exempt a `+Z` move from below the centreline, which drives
  through the middle of the stock. Monotonically outward, not merely ending further out — radial distance
  along a line is convex.
- **Every pyqtgraph GL item defaults to `glOptions="additive"`, and additive blending *adds overlapping
  colours together*.** This shipped as a real defect from M2 to M11: `_rebuild_items` never set
  `glOptions`, so a red rapid `(0.90, 0.25, 0.20)` crossing a green feed `(0.20, 0.85, 0.35)` rendered as
  `#ffff8c` — a yellow in no palette, naming nothing. On a wrapped-rotary program most of the screen was
  that colour. It breaks *"colour carries every distinction"* in the renderer rather than in the batching,
  where no data-model test can see it, and it was found only when a legend made it obvious that a colour
  on screen had no name. **Toolpath batches use `BATCH_GL_OPTIONS`: blending off.** Assert GL state
  directly (`tests/test_viewport.py::test_toolpath_batches_never_blend`); the batching can be perfect
  while the picture is wrong.
- **Depth testing is off for the toolpath, and the overlays no longer *rely* on that.** A disabled test
  writes no depth, so the batches leave the depth buffer empty and the selection highlight and tool
  marker draw on top of the very segments they coincide with instead of z-fighting them. Until M12 the
  highlight used `translucent`, which *enables* the depth test (pyqtgraph's docs: *translucent — enables
  depth testing*; *additive — disables* it) and got away with it only because nothing wrote depth. **The
  solid view writes depth**, so that assumption died: a selection inside the material would have been
  swallowed by the solid it cuts, silently. The highlight now states `OVERLAY_GL_OPTIONS` — depth test
  off, blending off — explicitly, and depends on nothing else in the scene. The marker was always safe:
  `additive` disables the test. Turning depth testing **on** for the toolpath is still not a local
  change; it would make selections flicker or vanish, with no error.
- **The solid view is a *display* heightfield, and `verify/` must never read it or `[tool]`.** M12
  amended PLAN.md § Non-Goals rather than overrunning it: dexel removal *for verification* is still out.
  Every rule judges the programmed centreline, and giving the cutter a width would silently change what
  several of them mean — `geometry.axis-travel-exceeded` against a tool edge rather than the spindle
  centre is a different check, not a better one. A diagnostic derived from the carve would also be
  confidently wrong in exactly the cases that matter, because the model **cannot represent an undercut**
  (one number per cell), carves with **one cutter** (there is no tool table), and **does not carve
  untrusted spans** (a cutter-compensated span is the centreline, not where the tool goes — so the solid
  shows *more* material than reality, the recoverable direction). Each of those is a `SolidField.notes`
  entry that reaches the legend's solid row. A shaded solid looks far more authoritative than a line
  does; the narrowing has to stay visible on the picture, not live in a document nobody has open.
- **The stock shape decides the solid's frame, not the user.** A box is a top-down Z map in machine
  coordinates and is **refused** once a table-mounted program moves A, exactly as
  `geometry.rapid-into-stock` refuses it. A cylinder is a radial map over (angle, axial) in **part**
  coordinates, which is stationary under rotation for the M9 invariance reason. So `Ctrl+D` flips
  `Ctrl+P` to match, and changing the frame by hand under a live solid switches the solid off. Drawing a
  carve in the other frame is not a rougher picture — it is a picture of somewhere the part is not.
- **The solid is pre-lit in world space, and the floor grid hides while it is up.** Both were found by
  launching the application and looking, and no offscreen test can see either. pyqtgraph's `shaded`
  program lights from **eye** space and clamps `dot < 0` to zero, so a face turned *toward* the camera
  renders at ambient — and that face is the machined surface, which came out as the darkest thing on
  screen. `solid_mesh.shade` bakes a **world**-space light into vertex colours and the item uses
  `shader=None`; a named shader would re-light them. It uses a **wrap-around** term rather than clamped
  Lambert, because clamping gives every away-facing wall the same value and a pocket then reads only as
  an outline. Separately, `GLGridItem` is a plane at **Z = 0** and a stock top at Z = 0 is the ordinary
  convention, so the grid lies in the blank's top face — drawn over every pocket floor and z-fighting
  every uncut one. `_show_grid(False)` while a solid is on screen. The depth buffer is real (24-bit) and
  works; this is geometry, not a missing depth test.
- **The carve is two passes, and the erosion is the algorithm.** `height = min over path points within r`
  *is* a grayscale erosion of the tool-axis height map by the cutter's bottom, so `sim/solid.py`
  rasterises axis positions (cost ∝ path length) and erodes once (cost ∝ grid size, independent of
  program size). Rewriting it as a per-segment sweep is `O(segments × kernel)` — 450M cell updates at
  500k segments — and is the wrong algorithm, not a slow one. Grid resolution is **derived**, as the
  coarser of `extent / TARGET_CELLS` and `radius / MAX_RADIUS_CELLS`, because the kernel grows with the
  square of the radius *in cells*; fixing the cell size lets a large cutter build a 125,000-offset kernel
  and hang. Both the carve and the mesh **wrap the angular seam**; either one left open leaves a defect
  exactly one tool radius wide at exactly one angle, which reads as a feature of the user's program.
- **`default_4axis.toml`'s switchable blocks must stay bare.** `profile_doc._set_section_active`
  uncomments a section's header and every `key = value` line **down to the next header**, so a prose line
  shaped like an assignment sitting inside a toggle section's span is uncommented into a file that is not
  valid TOML. This is not hypothetical — `[tool]`'s documentation said `shape = "flat"   reaches …` and
  broke five tests. Explanations go above both blocks; the blocks themselves are header plus keys.
- **The legend reads its labels and colours off the `Batch` objects, and never restates the palette.**
  `build_batches` already emits `"feed (unverified)"` and the exact RGBA handed to GL; `legend.py`
  capitalises the label for display and owns no second table. That is also why `HIGHLIGHT_COLOR` and
  `MARKER_COLOR` live in `legend.py` with `viewport3d` importing them. A hard-coded row is a second copy
  of the styling decision, and the two disagreeing produces a key that confidently mislabels the picture —
  worse than no key. For the same reason it lists only what is drawn: no unverified row for a program
  with no unverified span, because the row *appearing* is the information.
- **One pixel threshold separates a click from a camera move, and it works in both directions.** Picking
  is bound to mouse *release*, and an orbit or a pan ends in a release too — so above `CLICK_SLOP_PX` the
  release must pick **nothing**, or every orbit scrolls the editor to whatever segment the camera move
  left under the cursor. Below it the camera must not move **at all**: `GLViewWidget` orbits a *degree per
  pixel*, so a two-pixel tremor while clicking swings the view far enough that the release misses the
  segment the user aimed at, and clicking reads as intermittently broken. The travel inside the slop is
  deferred rather than discarded (`mousePos` stays at the press point) or the geometry trails the cursor
  by up to that much for the rest of the drag. Panning is on Shift+left-drag, right-drag and middle-drag,
  all three through one implementation in the camera plane — pyqtgraph's `view-upright` foreshortens the
  vertical to about half at the default elevation, and a drag that moves the part less than the hand reads
  as the view resisting.
- **The playback position is a float that the slider *displays*, never the other way round.** `TimelineBar`
  works in integer thousandths of the total — right for dropping a handle, useless for animating: one tick
  of an hour-long program is 3.6 seconds. `Playback.seconds` is authoritative; the slider is written under
  `blockSignals`. And `advance` takes the wall step as an **argument** rather than reading a clock, which is
  what lets every playback test run frame by frame without sleeping.
- **Editor→transport and transport→editor are a loop, and `MainWindow._without_playback_seek` is what keeps
  it open.** The cursor sets the play head (M15) and the play head moves the cursor (T3.5), so every cursor
  move the *user* did not make must be wrapped: the scrubber's `goto_line`, and `setPlainText` on load and
  after a fix. Ungated, playback moves the cursor ~30 times a second and each move seeks back to the
  *start* of that line, so the player stalls inside the first long move — and a dropped scrub handle snaps
  to a line boundary. A click in the viewport or on a diagnostic is deliberately **not** wrapped; both mean
  "start here". The seek also carries the segment index it intends rather than letting the widget re-derive
  it, because `index_at(start_of(i))` is **`i - 1`** — `cumulative` holds *end* times, so the boundary
  belongs to the move that just finished — which is why `seek_to_segment` emits `advanced` and never
  `scrubbed`.
- **Exactly two rules judge the control rather than the program, both must stay warnings, and their
  remedies do not overlap.** They fire on the *same blocks* of a wrapped-rotary post's profile resets, so
  conflating them sends the user to apply the wrong fix and watch half the gouge survive. This is not
  hypothetical: `G61` was applied to a real job, removed the groove inside each profile, and left the one
  between profiles exactly as it was, because that one was never a blending fault.
  `process.rotary-rapid-before-plunge` (M13) is a **timing** fault — a dwell or `G61` fixes it.
  `process.rotary-rapid-short-rotates` (M14) leaves the axis **physically in the wrong place**, where no
  dwell, M-code or `G61` helps at all. If you add a look-ahead-flush exemption to one, do not copy it to
  the other: `_next_moving_block` stops at an M-code or `G4` and `_next_rotary_block` deliberately does
  not, and `test_a_dwell_does_not_suppress_this_rule` is the paired assertion that keeps them apart.
- **M14's rule tracks the *physical* rotary position, never the modelled one.** Under `short_rotate` a
  `G0` past a half turn stops a full turn from the commanded angle and reports that as its position, so
  the block that cuts is typically `G1 A0.000` when the program already has A at 0 — **no programmed
  rotation at all**, and a rule keyed on a change in `after.a` reports the gouge nowhere. Keying off
  `"A" in command.words` is what makes it visible. The drift also propagates: a rapid whose programmed
  travel is 90° is a 270° move from a machine already a turn out, so the position is carried block to
  block rather than recomputed. `[axes.a].short_rotate` **requires `wrap`** and is refused otherwise —
  landing a full turn away is only the same place if the axis wraps — and it lives on the axis rather than
  in `[dialect]` because it is a per-machine checkbox, not a property of the language, and `parser/` must
  never learn about it. An exact half turn is reported as a **tie** rather than assumed harmless: both
  directions are 180° and land a full turn apart, and a two-pass wrapped program resetting A0 from A-180
  is that case.
- `process.rotary-rapid-before-plunge` (M13) reports a `G0` whose duration is set by the rotary axis
  followed straight away by a plunge. The commanded path is *safe* — Z is at clearance for the whole
  rapid, every limit holds, the viewport draws it correctly — and a machine still cut a groove around
  the bar, because constant-velocity blending (Mach3 CV, `G64`) starts the descent before the rotation
  finishes. Nothing else in `verify/` can see that, which is why the exception exists; whether a control
  blends is not in the G-code, which is why it can never be an error and why it has no fix. Its
  "rotary-dominated" test is a ratio of **times** from each axis's `max_rapid`, never of degrees: a
  degree threshold fires on a fast A axis and stays silent on a slow one, which is backwards, and
  comparing degrees to millimetres is the cross-unit expression the rotary column exists to prevent.
  `_descends_alone` is shared with `plunge-feed-too-high` so the two cannot drift apart about what a
  plunge is.
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
- **unsupported** — well-formed, recognized, and **affects motion**, but not interpreted by v1 (cutter compensation, canned cycles G80–G89, coordinate transforms G68/G51/G16, subprogram calls M98/M99). The affected span is marked or suppressed, never drawn as if understood. Under an active G81, a block containing only `X10 Y10` is a drill cycle, not a linear move.
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
- LinuxCNC is the **default and normative** dialect. Mach3 is selectable via `[dialect].name` or
  `--dialect`; both are documented deviations in `PLAN.md` § Dialect Divergences.
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
