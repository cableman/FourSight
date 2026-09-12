# FourSight

A 4-axis G-code previewer and verifier. It draws the toolpath, carves the blank, checks the program
against a machine profile, and offers fixes — with one governing rule:

> **Never render a confidently wrong toolpath.** A previewer that refuses to draw is recoverable; one that
> draws the wrong path is worse than no previewer.

![The main window: code, toolpath, legend, transport and diagnostics](docs/images/overview.png)

That screenshot is the rule in action. The program contains a `G81` drill cycle, which FourSight does not
interpret — so the span is **not drawn at all**, there is a gap in the toolpath where the holes would be,
a banner says the picture is incomplete, and the diagnostics panel reports it as `unsupported` rather than
as a warning. The alternative — a straight line through the hole positions — would have looked entirely
plausible and been wrong.

## Why 4-axis matters

In machine coordinates, wrapping a feature around a cylinder is three straight lines. In part coordinates
it is what the tool actually cuts:

![The same wrap in part coordinates](docs/images/part-coordinates.png)

Simultaneous XYZ+A moves are transformed **per interpolation step**, not from their endpoints — endpoint-only
transformation collapses a helix into a straight chord, and it is the single most likely source of silently
wrong output. Measured: a four-turn wrap lands on its cylinder to within 3.55e-15 mm.

## Install

```bash
python3 -m venv .venv                    # any Python 3.11 or newer
.venv/bin/pip install -e ".[gui]"        # viewer; omit [gui] for the CLI only
```

**Python 3.11 or newer** — `tomllib` is stdlib from 3.11. Check what you got before going further, because
a distribution's `python3` is often older than you assume:

```bash
.venv/bin/python --version
```

If that reports 3.10, delete `.venv` and create it with an explicit interpreter
(`/usr/bin/python3.12 -m venv .venv`, or whatever your system calls it).

## Use

```bash
foursight-gui part.nc                    # open the viewer
foursight check part.nc                  # verify, exit 1 if errors were found
foursight parse part.nc                  # dump the parsed blocks
```

Both `check` and `parse` take `--profile`, `--dialect`, `--arc-centre` and `--block-delete`; `foursight-gui`
takes all four as well, and its file argument is optional. `check` adds `--no-simulate`, which tests block
endpoints only — faster on a very large file, but blind to an arc that bulges past a travel limit mid-sweep.
`parse` adds `--modal`, which dumps each block's resolved modal snapshot alongside it.
Block delete (`/`) **executes** by default, matching the common control-panel default; `--block-delete`
skips those blocks instead.

`foursight check` is built for a pipeline: diagnostics go to stdout in compiler format
(`file:line: severity [rule] message`), tool problems go to stderr, and the exit code is **0** clean,
**1** errors found, **2** could not run. `unsupported` and `warning` do not fail a run — failing on
`unsupported` would push users toward suppressing the check entirely, which is worse than reporting a span
that was not interpreted.

The CLI needs no Qt. That is enforced by a CI job that installs without the `[gui]` extra.

## Three tiers, and why they are not degrees of the same thing

| Tier | Means | What the viewer does |
|---|---|---|
| `error` | Malformed, or would break the machine | Reported; geometry drawn where it can be |
| `unsupported` | Well-formed, recognized, **affects motion**, not interpreted in v1 | The span is **not drawn**, or drawn and explicitly marked — never drawn as if understood |
| `warning` | Suspicious, or unrecognized but inert | Drawn normally |

An unrecognized code that never touches position is a warning. One that changes how later motion is
interpreted is `unsupported`. Under an active `G81`, a block containing only `X10 Y10` is a drill cycle,
not a linear move — and that difference is the whole reason the middle tier exists.

There are **29 rules**: 6 structural, 17 process, 6 geometry. `PLAN.md` § Verifier Rules lists every one.

## Reading the picture

The viewport distinguishes everything it draws by **colour alone**, and not by preference: pyqtgraph's
`GLLinePlotItem` has no dash or stipple parameter, and `glLineWidth` is inert on core OpenGL profiles. So
the **legend is the second channel** — it supplies the names, and it is on by default, because rapid-red
against feed-green is exactly the distinction that collapses for the commonest colour blindness. It reads
its rows off the batches that were handed to GL rather than restating the palette, and it lists only what
is actually on screen: a program with no cutter-compensated span gets no `unverified` row, because the row
*appearing* is the information.

| | Shortcut | |
|---|---|---|
| Fit to program | `Ctrl+0` | |
| Play / pause | `Ctrl+Space` | not bare Space — the editor holds the focus and Space must still type a space |
| Legend | `Ctrl+L` | on by default |
| Part coordinates | `Ctrl+P` | the path as it lies on the part (table mount) or the tool tip (head mount) |
| Solid view | `Ctrl+D` | |
| Toolpath | `Ctrl+T` | independent of the solid, deliberately |
| Machine profile… | `Ctrl+M` | |

Left-drag orbits, the wheel zooms, and panning is on **Shift+left-drag, right-drag and middle-drag** — a
middle button is what a trackpad does not have. Every pan works in the camera plane rather than pyqtgraph's
`view-upright`, which foreshortens the vertical to about half at the default elevation and reads as the view
resisting the hand.

**Clicking a segment jumps to its line, and clicking a line highlights its segments.** One pixel separates a
click from a camera move, in both directions: below the threshold the camera does not move at all (`GLViewWidget`
orbits a degree per pixel, so a two-pixel tremor would make the release miss what the user aimed at), and above
it the release picks nothing, or every orbit would select whatever it left under the cursor.

**Putting the cursor on a line parks the play head at the start of that line's first move**, so selecting a
block and pressing `Ctrl+Space` runs it. Playback advances 1×, 10×, 100× or 1000× program-seconds per
wall-second and keeps reporting program time; the multiplier survives the reload that every applied fix causes.
A line with no geometry leaves the position alone and the status bar says why — "no motion", or "not drawn" for
a suppressed span — rather than starting somewhere the user did not click.

### The solid view

`View → Solid view` (Ctrl+D) carves the `[stock]` blank with the `[tool]` cutter and shades the result, so the
question "what does this program leave behind" gets an answer without a material-removal simulator.

![A wrapped helical groove, carved into the blank](docs/images/solid-view.png)

It is a **display** heightfield and nothing in `verify/` reads it or `[tool]`. That narrowing is deliberate
rather than temporary: every rule judges the programmed centreline, and giving the cutter a width would
silently change what several of them mean. The model also states its own limits, on the picture rather than in
a document nobody has open — it **cannot represent an undercut** (one height per cell), it carves with **one
cutter** (there is no tool table), and it **does not carve untrusted spans**, so a cutter-compensated span
leaves *more* material than reality, which is the recoverable direction. Each of those reaches the legend's
solid row when it applies.

The stock shape decides the frame, not the user. A box is a top-down Z map in machine coordinates and is
**refused** once a table-mounted program moves A; a cylinder is a radial map in part coordinates, which is
stationary under rotation. So switching the solid on flips `Part coordinates` to match, and changing the frame
by hand under a live solid switches the solid off. Drawing the carve in the other frame is not a rougher
picture — it is a picture of somewhere the part is not.

The toolpath and the solid are **independent** toggles. Lines over a solid is the useful combination for asking
*which move left that mark*, and lines alone is what the application has always been.

## Fixes refuse when the intent is ambiguous

![Reviewing a fix before applying it](docs/images/diff-preview.png)

There are eight fixes. Every one is previewed as a unified diff and applied only on confirmation, and
applied to the **editor buffer** — the file on disk is never written until you save. Applying one fix re-runs the entire pipeline (parse →
simulate → verify), because a fix shifts every line number after it and a second fix aimed at "line 42"
would otherwise land somewhere else.

Two fixes decline on purpose:

- **Arc-centre recomputation** refuses beyond 10× tolerance. Within a small mismatch the centre is a
  rounding artefact and the endpoints are the intent; past that, three inconsistent numbers describe no arc
  and choosing which to keep is a guess.
- **IJK→R conversion** refuses on a full circle. R gives a radius and its sign picks the minor or major arc,
  but coincident endpoints make every R the same degenerate case.

N-word stripping is available but marked destructive and never the default: operators restart mid-cut on
N-numbers and some dialects use them as jump targets, so removing them can break a program in ways **the
diff does not show**.

## Machine profiles

A TOML file describes the machine — travel limits, rapid rates, tolerances, rotary mount and centreline.
The rule throughout is that **absence means unknown, never a fabricated value**: a limit that is not
configured disables its check rather than defaulting to something plausible, and a head-mount profile with
no `pivot_to_tip` refuses to show part coordinates rather than inventing a tool length.

```bash
foursight check part.nc --profile my-mill.toml
```

See `src/foursight/profiles/default_4axis.toml`, which documents every field inline, and
`profiles/rotary.toml`, which is a real wrapped-rotary machine written up with the reasoning for each value.

### Editing the profile in the app

`File → Machine profile…` (Ctrl+M) opens a form over the loaded profile. It is non-modal, and **Apply
changes the profile in memory and re-checks the open program immediately** — so a limit can be tried
against a real file and the diagnostics list responds, rather than requiring a file edit and a restart.

Nothing is written to disk until `Save as…`, which is also the only way to keep changes to the profile
that ships inside the package. Three details worth knowing:

- Optional fields have a **set** checkbox. Clearing it writes *nothing* rather than a zero, because an
  unset limit disables its check and a zero one does not.
- Saving keeps the file's comments, and its layout: one key's value is replaced in place. A field you
  switch off is commented out rather than deleted, so your number and its explanation are still there
  when you switch it back on.
- Values you type are written verbatim — `0.005` stays `0.005`.

### Stock, and rapids that would hit it

Two optional settings answer the commonest complaint about a previewer — *it doesn't tell me the machine is
going to hit the stock*.

```toml
[limits]
max_plunge_feed = 300.0     # mm/min; a straight-down G1 faster than this is reported

[stock]                     # a box, in MACHINE coordinates — for prismatic work
min = [0.0, 0.0, -20.0]
max = [100.0, 80.0, 0.0]
```

For 4-axis work, describe the blank as a **cylinder** instead:

```toml
[stock]
shape = "cylinder"
diameter = 50.0
length = 200.0
axis_min = 0.0              # machine coordinate of the end nearer the axis minimum
```

Its axis is not stated here — it is `[kinematics].rotary_axis` through `centerline_offset`. That is the
point rather than a shortcut: a cylinder concentric with the rotary axis is unchanged by any A rotation,
so the check stays exact at every angle. A **box** cannot manage that, because it stops describing stock
that turns with the part, so a program that moves A gets one diagnostic saying the envelope cannot be
judged instead of per-rapid findings. An off-axis cylinder would have the same problem, which is why it
cannot be expressed.

Both are unset by default, because neither has an honest generic value: a sane plunge rate belongs to the
tool and the material, and no shipped profile knows what is clamped to your table today. `[stock]` also
feeds the solid view, together with `[tool]` — which is the cutter the carve uses and nothing else, read by
no verifier rule.

What they catch and what they do not is worth being precise about:

- **Plunging** is reported only for blocks that move **Z alone**. A ramp or a helical entry at the
  contouring feed is correct practice, not a defect, so neither is reported however fast it is.
- **`[stock]` is an envelope, not a material-removal model.** It knows where the solid started, never
  what is left of it — so a rapid repositioning at depth inside a pocket an earlier pass already cleared
  is reported too. That is why the finding is a **warning**: it is worth a look, not a claim that the
  program is broken. A cutting move through uncut material is not caught at all; that needs the removal
  simulation which is still future work.
- A **retract is never reported**, or the rule would flag the `G0 Z25` at the end of every single pass.
  Out of a box that means straight up; out of a cylinder it means radially away from the axis — so a
  tool working the underside of a bar can retract *downward* without being flagged, and a `+Z` move
  from below the centreline is flagged, because that one goes through the material.
- When the **part rotates** and the blank is a **box**, a solid fixed in machine coordinates no longer
  describes where the stock is. FourSight says so in one diagnostic and checks nothing further, rather
  than reporting collisions it cannot stand behind. Declare a **cylinder** and the check holds.

### Dialect

LinuxCNC is normative, and its documented deviations from Fanuc are listed in `PLAN.md` § Dialect
Divergences — `G4 P` in seconds rather than milliseconds, `%`/`Oxxxx` framing consumed silently, `;`
comments accepted.

Mach3 is selectable, because two of its behaviours are **controller configuration** rather than G-code —
nothing in the file can tell us which way they are set, and guessing wrong at the arc-centre mode draws
every unqualified arc in the program in the wrong place with no diagnostic at all.

```toml
[dialect]
name = "mach3"
arc_centre = "absolute"   # Mach3's Config -> General "IJ Mode" radio button
```

```bash
foursight check part.nc --dialect mach3 --arc-centre absolute
```

Precedence is `--dialect`/`--arc-centre` > the profile's `[dialect]` > `linuxcnc`. Selecting a
dialect adds no G-codes and removes none; it changes only what `PLAN.md` § Dialect Divergences lists.
Mach3-specific codes FourSight cannot interpret — `G68`/`G69` rotation, `G51`/`G50` scaling,
`G16`/`G15` polar, `M98`/`M99` subprogram calls — are reported as `unsupported` and **not drawn**,
rather than being assumed inert and drawn as if absent.

## Two rules judge the control, not the program

Both were found the expensive way — on a real Mach3 machine, on the same wrapped-rotary job, by looking at
the workpiece. Both report a hazard that is **not in the G-code**, so both are permanently warnings: the
rule says the corner is there, the machine decides what to do with it. Their remedies do not overlap, which
is the whole reason they are two rules.

- **`process.rotary-rapid-before-plunge`** — a `G0` whose duration is set by the rotary axis, with a plunge
  on the next moving block. The commanded path is safe: Z is at clearance throughout, every limit holds, and
  the viewport draws it correctly. Under constant-velocity blending (Mach3 CV, LinuxCNC `G64`) the control
  rounds the corner and starts the descent before the rotation finishes, cutting a groove around the part
  that appears nowhere in the program. A dwell or `G61` fixes it. "Rotary-dominated" is a ratio of **times**
  from each axis's `max_rapid`, never of degrees — a degree threshold fires on a fast A axis and stays silent
  on a slow one, which is backwards.
- **`process.rotary-rapid-short-rotates`** — a `G0` past a half turn on a control set to take the shorter way
  round (Mach3's "Ang Short Rot on G0"). It stops a **full turn** from the commanded angle and reports that as
  its position, so the next absolute block makes the turn up — and if it is cutting, it cuts a complete circle
  around the part. No dwell, M-code or `G61` helps: the axis is physically in the wrong place. Declare it with
  `[axes.a].short_rotate`, which requires `wrap` on the same axis and is refused otherwise; undeclared, the
  rule is off, because the setting appears nowhere in the file.

`G61` was applied to the real job, removed the groove *inside* each profile, and left the one *between*
profiles exactly as it was — which is what these being one rule would have cost.

## Performance

Measured on an Intel Iris Xe / Mesa 25.1.5 baseline:

| | | |
|---|---|---|
| Parse | 94k lines/sec | floor of 50k asserted in CI |
| Simulate | 32k blocks/sec | cost is per *block*, not per segment |
| 100k-line file to first frame drawn | **4.80 s** | against a 5 s target — 4% margin |
| Orbit, 500k segments | 241 fps | worst frame 10.7 ms, against 30 fps |
| Geometry + GL buffers, 500k segments | 50.5 MB | 38.5 MB store + 12.0 MB GL |
| Click → segment, 500k segments | 27 ms | projection cached per camera change |
| Solid carve | 61 ms / 0.95 s | 200-pass raster / 40k cutting segments |

That 4% margin is thin and it is honest: the number was 4.46 s before the editor pane existed, and the
editor's `setPlainText` costs 1.23 s on a 100k-line file. The next feature touching the load path will break
it. `PLAN.md` § Timing fast path records what was already reclaimed and what levers remain.

The carve is two passes and the erosion is the algorithm: rasterise tool-axis positions (cost ∝ path length),
then erode once by the cutter's bottom (cost ∝ grid size, independent of program size). A per-segment sweep
would be 450M cell updates at 500k segments — the wrong algorithm, not a slow one. Grid resolution is derived
rather than fixed, because the kernel grows with the square of the tool radius *in cells*, and a 90 mm face
mill on a fixed grid builds a 125,000-offset kernel and hangs.

Loading runs off the GUI thread and is cancellable; a cancelled load leaves the previous toolpath on
screen rather than a partial one, because half a program is not a program.

## Development

```bash
.venv/bin/pip install -e ".[dev,gui]"
.venv/bin/pytest -q                                        # 1820, ~38 s
.venv/bin/ruff check --fix . && .venv/bin/ruff format .
.venv/bin/python scripts/build.py                          # PyInstaller one-dir bundle
DISPLAY=:0 .venv/bin/python scripts/screenshots.py         # regenerate the images above
```

The suite once segfaulted in one process at `test_editor.py::test_loading_a_program_shows_the_parsed_text`
and had to be run in two parts. It no longer reproduces — twelve one-process runs across two revisions,
two Qt platforms, `PYTHONMALLOC=malloc` and forced per-test collection are all green — so the split is
gone. The hazard behind it is real and is tracked as `OPEN.md` § 4: a worker `QThread` the window has
forgotten is destroyed while still running, which aborts the process.

`PLAN.md` is the design record and owns every architectural decision, with the reasoning for each.
`TASKS.md` is the execution log. When the two disagree, PLAN.md wins.

Testing leans on three things beyond ordinary unit tests: **mutation testing** (a test that passes when the
code is broken is not a test), **golden fingerprints** of simulator output that name the field that moved
rather than only that a hash changed, and **closed-form geometry checks** for kinematics — compared against
independently computed expectations rather than recorded output, because a golden will happily lock in a
mirrored wrap.

## Status

M0–M15 complete. **`OPEN.md` lists everything still owed** — one gate, four defects and five owed items,
each with its evidence and what closing it takes. The gaps that are deliberate design decisions rather
than debt, all reasoned out in `PLAN.md`:

- **No tool table**, so `G43`/`G44` tool length is not modelled. The path's shape is correct and its Z datum
  is shifted; the program is drawn and flagged `unsupported`, because refusing every program that uses G43
  would make the previewer useless. The solid view inherits it: one cutter carves the whole program, and the
  legend says so.
- **No material-removal model in the verifier.** `[stock]` is an envelope and the solid view is a display
  heightfield; neither tells a rule what is left of the blank, so a cutting move through uncut material is
  not reported. Rotary step sizing likewise uses the path's own radius as a proxy for the part's.
- **No cutter-compensation geometry.** The programmed centreline is drawn and marked unverified, and it is
  not carved.

Two of `OPEN.md`'s items are worth knowing before you rely on a build: **Windows bundles have never been
launched on a clean VM** (they are built in CI, and `--windowed` has never been exercised), and **applying a
machine profile while `Part coordinates` is on** raises out of the toggle handler — switch it off first.
