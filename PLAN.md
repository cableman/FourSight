# FourSight — 4-Axis CNC G-code Previewer & Verifier

A cross-platform (Ubuntu + Windows) desktop application that parses, simulates, verifies, and fixes CNC G-code with up to 4-axis (XYZ + A rotary) visualization.

**Naming:** import package is `foursight`; PyPI distribution name is `foursight-cnc` (plain `foursight` is already taken on PyPI). Set `name = "foursight-cnc"` in pyproject.toml.

**Reference dialect:** LinuxCNC is the normative dialect. Fanuc-isms are supported as documented deviations (see Dialect Divergences). Where the two conflict and no explicit setting resolves it, LinuxCNC wins.

## Goals

- Load and parse G-code files (LinuxCNC dialect, Fanuc-compatible subset)
- Simulate and render toolpaths in 3D, including 4th-axis rotary motion
- Verify programs against a configurable machine profile and report errors/warnings with line numbers
- Offer safe, diff-based automatic fixes
- Run natively on Ubuntu and Windows from a single Python codebase

**Overriding principle: never render a confidently wrong toolpath.** A previewer that refuses to draw is recoverable; one that draws the wrong path is worse than no previewer at all. Any construct we cannot interpret correctly must degrade to a visible refusal, not a plausible-looking line.

## Non-Goals (v1)

- Material removal / stock simulation (voxel cutting) — deferred, see Future
- 5-axis kinematics
- Post-processor generation or CAM features
- Controller-specific macro languages (Fanuc Macro B, LinuxCNC O-words) beyond basic tolerance/skip
- Executing canned cycles (G81–G89) — v1 **detects and refuses** them, see Unsupported Motion Codes

## Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Environment | `venv` (`.venv/` at repo root) | **Always.** Never install into or run against system Python |
| GUI shell | PySide6 (Qt) | Behind the `[gui]` extra — see below |
| 3D rendering | pyqtgraph GLViewWidget (OpenGL) | Behind `[gui]`. Batch into ≤ 10 `GLLinePlotItem`s; see Rendering Constraints |
| OpenGL binding | PyOpenGL | Behind `[gui]`. Hard requirement of `pyqtgraph.opengl`, not optional |
| Code editor pane | QPlainTextEdit + custom highlighter | Line sync with 3D view |
| Parsing | Custom tokenizer/parser | Do NOT depend on pygcode; write our own for modal-state control |
| Math | numpy | Arc interpolation, rotary transforms |
| Config | TOML (tomllib read) | Machine profiles; add `tomli-w` only if we ship a profile editor |
| Testing | pytest + hypothesis | Parser and verifier must be heavily unit-tested; hypothesis for arc/kinematics properties |
| Lint/format | ruff | Single tool for linting and formatting; config in pyproject.toml |
| Packaging | PyInstaller (**one-dir**) | One-file re-extracts ~200 MB per launch and trips Windows AV heuristics |
| CI | GitHub Actions, Ubuntu + Windows matrix | From M0; the cross-platform claim is only as good as the matrix |

**Dependency extras.** Runtime deps are `numpy` only. The three Qt/GL packages live behind a
`[gui]` extra, and `[dev]` carries `pytest`, `hypothesis`, `ruff`, `pyinstaller`. This is what makes
"every module outside `gui/` imports without Qt" an *enforced* invariant rather than an intention:
CI runs a job that installs `.[dev]` alone and imports every headless package. M1 (parser, verifier,
`foursight check`) is fully usable without the extra. Full dev install is
`pip install -e ".[dev,gui]"`.

## Repository Layout

```
FourSight/
├── PLAN.md                  # this file
├── pyproject.toml
├── src/foursight/
│   ├── __init__.py
│   ├── cli.py               # headless CLI: `foursight parse` / `foursight check`; no Qt
│   ├── parser/
│   │   ├── tokenizer.py     # line → words (letter+number), comments, block-delete
│   │   ├── resolver.py      # words → Command objects, modal group resolution
│   │   └── model.py         # Command, Word, SourceRef, ModalState dataclasses
│   ├── machine/
│   │   ├── state.py         # MachineState: live position + modal groups during simulation
│   │   ├── profile.py       # MachineProfile loaded from TOML (limits, offsets, kinematics)
│   │   └── kinematics.py    # rotary table vs rotary head transforms
│   ├── sim/
│   │   ├── interpolate.py   # lines, arcs (G2/G3 IJK + R), rotary blending → segments
│   │   ├── segments.py      # SegmentStore: columnar segment storage (see Core Data Model)
│   │   ├── timing.py        # per-segment duration model for the timeline
│   │   └── simulator.py     # steps Commands → SegmentStore
│   ├── verify/
│   │   ├── rules.py         # Rule base class + registry
│   │   ├── checks/          # one module per check category
│   │   └── report.py        # Diagnostic (severity, line, message, fix_ids)
│   ├── fix/
│   │   ├── fixes.py         # Fix transforms, each returns a text diff
│   │   └── differ.py        # unified diff generation/application
│   ├── gui/
│   │   ├── app.py           # entry point
│   │   ├── main_window.py
│   │   ├── viewport3d.py    # GL view, camera, batched geometry
│   │   ├── picking.py       # segment ↔ screen hit-testing (see Picking)
│   │   ├── editor.py        # code pane, line highlighting
│   │   ├── timeline.py      # play/pause/scrub
│   │   └── diagnostics_panel.py
│   └── fileio/              # NOT `io/` — that shadows the stdlib module
│       └── loader.py        # file loading, encoding detection, large-file handling
├── profiles/
│   └── default_4axis.toml
├── tests/
│   ├── test_tokenizer.py
│   ├── test_parser.py
│   ├── test_arcs.py
│   ├── test_kinematics.py
│   ├── test_verifier.py
│   ├── test_fixes.py
│   ├── test_golden.py       # simulator output regression (see Testing Strategy)
│   ├── test_perf.py         # parse rate + segment budget regression
│   └── fixtures/            # sample .nc files, good and deliberately broken
└── scripts/
    └── build.py             # PyInstaller build for current OS
```

## Core Data Model

### Parse layer

```python
@dataclass(slots=True, frozen=True)
class SourceRef:
    line_no: int          # 1-based line in original file
    start: int            # byte/char offset into the source text
    end: int              # ...so we never store a per-line copy of `raw`

@dataclass(slots=True, frozen=True)
class ModalState:
    units: str            # always 'mm' internally; records the program's declared units
    plane: str            # 'G17' | 'G18' | 'G19'
    distance: str         # 'G90' | 'G91'
    arc_distance: str     # 'G90.1' | 'G91.1'
    feed_mode: str        # 'G93' | 'G94' | 'G95'
    offset: str | None    # 'G54'..'G59' or None if never set
    feed: float | None
    spindle_rpm: float | None
    spindle_on: str | None    # 'M3' | 'M4' | None
    tool: int | None
    length_offset: int | None # active H number under G43, None under G49
    cutter_comp: str | None   # 'G41' | 'G42' | None

@dataclass(slots=True)
class Command:
    ref: SourceRef
    gcodes: list[str]         # ['90', '21', '17', '54'] — a block carries several
    mcodes: list[str]         # ['3', '8'] — likewise
    motion: str | None        # resolved modal motion: '0'|'1'|'2'|'3'|None
    words: dict[str, float]   # axis/parameter letters only: {'X': 1.0, 'A': 90.0, 'F': 200.0}
    modal_snapshot: ModalState
```

Three things the earlier draft got wrong and this fixes:

- **A block carries multiple G- and M-words.** `G90 G21 G17 G54` is four G-words and `M3 M8` is two M-words; a single `dict[str, float]` keyed by letter can hold one of each. G- and M-codes live in their own lists, and `words` holds only the axis/parameter letters.
- **G-codes are strings, never floats.** `G90.1` as a float invites `words['G'] == 90.1` equality bugs, and we support G90.1/G91.1 explicitly. Canonicalize on parse: `G01`, `G1`, and `G1.0` all become `'1'`.
- **`SourceRef` stores offsets, not text.** Holding `raw` per line duplicates the entire file in memory and costs us the parse-rate target. `SourceRef` is created **once per line and shared** by every Command and Segment deriving from it.

`ModalState` is frozen and shared: the simulator holds one live `MachineState` and emits a new `ModalState` only when something actually changes, so consecutive commands usually share one instance.

**Implemented in T1.1, with three decisions the sketch above left open:**

- **`ModalState` fields carry the dialect defaults**, so a program that never states them still has a well-defined starting state: `units='mm'`, `plane='17'`, `distance='90'`, `arc_distance='91.1'` (per Dialect Divergences), `feed_mode='94'`. Everything genuinely not-yet-established — `offset`, `feed`, `spindle_on`, `tool`, `length_offset`, `cutter_comp` — starts `None`, so the verifier can distinguish "never set" from "set to zero".
- **`units` is never `None`**, unlike `offset`. A program with neither G20 nor G21 still has an effective unit, whereas "no work offset active yet" is a real modal state. "Units never explicitly set" is a property of the whole program rather than of a modal group, so the verifier detects it by looking for G20/G21 across the command stream, not via a sentinel.
- **`parser/model.py` owns the word-letter tables**, because the G20 inch conversion in the resolver depends on classifying letters correctly and getting it wrong is silent:
  - `WORD_LETTERS` — `X Y Z A I J K R F S T P H D L Q`; excludes G and M, which are lists of strings.
  - `AXIS_LETTERS` — `X Y Z A`; distinguishes a real motion block from a parameter-only one.
  - `LINEAR_LENGTH_LETTERS` — `X Y Z I J K R`; scaled by 25.4 on G20 input.
  - `ROTARY_LETTERS` — `A`; degrees, **never** scaled by a unit conversion.
  - `F` is in neither scaling set on purpose: it is a length rate under G94/G95 but 1/minutes under G93, so no static table can classify it and the resolver must decide per feed mode.

### Tokenizer layer (T1.2)

`tokenizer.py` turns one line into a `TokenizedLine`, and `model.py` gains three types for it:

```python
@dataclass(slots=True, frozen=True)
class Word:                       # one address word
    letter: str                   # always upper-cased
    value: float

@dataclass(slots=True, frozen=True)
class TokenError:                 # malformed input, REPORTED not raised
    offset: int                   # absolute offset into the source text
    text: str                     # the offending characters, as written
    message: str

@dataclass(slots=True)
class TokenizedLine:
    ref: SourceRef
    words: list[Word]
    block_delete: bool = False    # line began with '/'
    line_number: float | None = None      # N
    program_number: float | None = None    # Fanuc Oxxxx
    comments: list[str] | None = None
    errors: list[TokenError] | None = None
```

- **`TokenError` is deliberately not a `Diagnostic`.** The dependency direction forbids the parse layer from naming a `verify` type, so the tokenizer reports neutral facts and the verifier attaches severity (T1.7).
- **`comments` and `errors` are `None` when empty**, not `[]`. Both are empty on the overwhelming majority of lines, and a 100k-line file would otherwise allocate 200k throwaway lists against a ~20 µs/line budget. Read them as `line.comments or ()`.
- **N and O never appear in `words`.** An N-number labels the line; a bare `Oxxxx` is a Fanuc program number, consumed silently.
- **Comments are stripped before words are scanned**, matching LinuxCNC, which makes `X (why not) 10` a legal spelling of `X10`. They are blanked in place rather than deleted, so every later offset — and therefore every error position — stays correct. Tokenizing them inline instead produced two *false* errors on valid input.
- **LinuxCNC O-word flow control** (`O100 sub`, `o<name> while`, …) is detected and reported as a single unsupported construct. Without that, the letters of `sub` surfaced as three bogus "address has no value" errors, which would have hidden a construct that decides *which motion runs* — `unsupported`, never a warning.
- **Measured: 159k lines/sec (6.27 µs/line)** on the baseline machine for a realistic mix, or 31% of the 20 µs/line budget, leaving ~13.7 µs for the resolver.

### Segment store — columnar, not per-object

500k `Segment` dataclasses each holding two numpy arrays costs ~400 B apiece (≈48 B object + ~100 B `__dict__` + 2 × ~144 B for the tiny arrays) — over 200 MB before the GL buffers, and then it all has to be repacked into contiguous arrays anyway. Store columns:

```python
@dataclass(slots=True)
class SegmentStore:
    lin:      np.ndarray  # (N, 2, 3) float64 — XYZ mm, machine coords, [seg, start|end, axis]
    rot:      np.ndarray  # (N, 2)    float64 — A degrees, kept OUT of the position vector
    kind:     np.ndarray  # (N,)      uint8   — Kind.RAPID | Kind.FEED
    line:     np.ndarray  # (N,)      int32   — 1-based source line, for editor sync
    duration: np.ndarray  # (N,)      float64 — seconds, for the timeline
    lin_part: np.ndarray | None  # (N, 2, 3) display coords when rotary_mount = "table"
```

~38 MB at N = 500k, and `lin.reshape(-1, 3)` is a contiguous zero-copy view in exactly the layout `GLLinePlotItem(mode='lines')` wants. A `Segment` view class may exist for test readability, but it must never be the storage.

**Key invariant:** every segment traces back to a source line via `line[i]`. Editor sync, diagnostics, and fixes all depend on this.

**Rotary is a separate column on purpose.** With A packed into a position 4-vector, `np.linalg.norm(end - start)` mixes millimetres and degrees and returns a meaningless number — and that is precisely the expression you would write for timeline durations. Linear and rotary travel are measured separately and combined in `sim/timing.py`. When 5-axis lands, `rot` generalizes to `(N, 2, R)`.

**`kind` is motion type only.** The earlier `'rapid' | 'feed' | 'arc'` conflated motion type with geometry: an arc is always a cutting move, and after interpolation everything is a line segment anyway. Rendering groups by rapid vs feed. If the originating motion code is needed, it is recoverable via `line[i]`.

### Coordinate frames

`lin` is **always machine coordinates**. Verification (travel limits) requires machine coordinates; table-mount display requires part coordinates. The rotary transform depends on A at every interpolation step, so it is nonlinear along the path and *cannot* be expressed as a view matrix — it must be baked into vertex positions. The buffer builder therefore fills `lin_part` as a second array for display. Never transform `lin` in place; doing so silently destroys the ability to verify.

Segment count is driven by rotary and arc tessellation, not line count: one `G1 X100 A360` block can become 1000+ segments. The 100k-line and 500k-segment targets are different axes.

## Machine Profile (TOML)

```toml
[machine]
name = "Generic 4-axis mill"
units = "mm"                # units the values in THIS FILE are expressed in

[limits]
max_feed = 3000.0           # mm/min
max_spindle_rpm = 24000.0

[tolerance]
arc_radius_mismatch = 0.005 # mm — |r_start - r_end| above this is an error
arc_chord = 0.01            # mm — max chord deviation when tessellating arcs
rotary_chord = 0.01         # mm — same, measured at max path radius from centerline

[axes.x]
min = 0.0
max = 400.0
max_rapid = 5000.0          # mm/min

[axes.a]
type = "rotary"
wrap = true                 # 0-360 wraparound; when false, min/max below are enforced
min = -360.0
max = 360.0
max_rapid = 3600.0          # deg/min

# Work offsets are NOT in the G-code file — they live in the controller. Without them
# the travel-limit check cannot run in machine coordinates. Any offset left unset makes
# limit violations WARNINGS ("assumes zero offset") rather than errors.
[offsets]
g54 = [0.0, 0.0, 0.0, 0.0]
# g55 = ...

[kinematics]
rotary_mount = "table"      # "table" | "head"
rotary_axis = "x"           # A rotates about machine X
# Point the rotary axis passes through. For rotary_axis = "x" only the Y and Z
# components are meaningful; the X term cancels through the rotation.
centerline_offset = [0.0, 0.0, 50.0]
# Required when rotary_mount = "head": distance from the swing pivot to the tool tip.
# pivot_to_tip = 120.0

[safety]
min_clearance_z = 5.0       # rapids below this → warning
require_spindle_before_cut = true
retract_before_toolchange = true
```

## Supported G-code Subset (v1)

**Interpreted:**

- Motion: G0, G1, G2, G3 (IJK and R format, including helical), G4 dwell
- Coordinate: G53 (non-modal machine coords), G28/G30 (reference return)
- Plane: G17/G18/G19
- Units: G20/G21
- Distance: G90/G91, arc-center distance G90.1/G91.1
- Feed mode: G93 (inverse time) / G94 (units/min) / G95 (units/rev)
- Offsets: G54–G59
- Tool length: G43/G44 (with H), G49
- Spindle/coolant M-codes: M3/M4/M5, M7/M8/M9 — **multiple M-codes per block are legal**
- Program: M0/M1/M2/M30, N-numbers, comments `( )` and `;`, block delete `/`
- File framing: leading/trailing `%`, Fanuc `Oxxxx` program number — consumed silently, not flagged
- Words: X Y Z A I J K R F S T P H D L Q
- Tool change: T + M6 (position tracking only, no geometry in v1)

**Detected and refused (see below):** G40/G41/G42 cutter compensation, G80–G89 canned cycles.

### Diagnostic severity taxonomy

The old rule — "unknown codes → warning, never crash" — is right for inert codes and dangerous for motion-affecting ones. Three tiers:

| Tier | Meaning | Path rendering |
|---|---|---|
| **error** | Malformed word, or a construct that would break/crash the machine | Path suppressed from the offending block onward where geometry is affected |
| **unsupported** | Well-formed, recognized, **affects motion**, not interpreted by v1 | Affected span rendered in a distinct "unverified" style, or suppressed |
| **warning** | Well-formed but suspicious, or unrecognized and inert | Path rendered normally |

An unrecognized code that never touches position stays a warning. An unrecognized or unimplemented code that *changes how subsequent motion is interpreted* is **unsupported**, never a warning.

### Unsupported motion codes (v1)

These are common enough that silently mis-drawing them is the most likely way FourSight produces a wrong picture:

- **G40/G41/G42 cutter compensation** — while comp is active the real path is offset by the tool radius. v1 renders the programmed centerline and marks the span: "compensation active; displayed path is the programmed centerline."
- **G80–G89 canned cycles** — under an active G81, a block containing only `X10 Y10` is a full drill cycle, not a linear move. Drawing a straight line there is exactly the failure mode we refuse. v1 detects the cycle, suppresses motion geometry until G80, and emits one `unsupported` diagnostic per cycle span.

## Arc Semantics

The component most likely to be subtly wrong, so it gets its own section.

- **R-format sign:** positive R selects the arc ≤ 180°; negative R selects the arc > 180°.
- **Full circles** are expressible in IJK (start == end) but **not** in R-format. The IJK→R fix must refuse on full circles rather than emit garbage.
- **Degenerate R:** R-format with coincident start and end points is undefined — error.
- **Helical arcs:** G2/G3 with motion on the plane-normal axis is a helix (helical ramping is common). The normal-axis component interpolates linearly across the sweep. An A-word may also move simultaneously.
- **Plane-dependent IJK mapping:** G17 → I,J; G18 → I,K; G19 → J,K. G18's direction convention is counterintuitive (in the XZ plane, G2 appears counter-clockwise viewed from +Y) — this needs its own fixture test, not just a code comment.
- **Radius mismatch tolerance** comes from `tolerance.arc_radius_mismatch` in the profile. It has exactly one source; do not hard-code it at the two use sites (the check and the fix).
- **Tessellation is adaptive on chord height**, driven by `tolerance.arc_chord` — never a fixed step count. A 500 mm-radius arc and a 0.5 mm-radius arc need wildly different step counts, and this choice is what actually sets the segment budget.

## Verifier Rules (v1 checklist)

Severity per the taxonomy above.

**Structural**
- [ ] E: Syntax errors, malformed words
- [ ] E: Two G-codes from the same modal group in one block
- [ ] W: Unknown/unsupported *inert* G/M code
- [ ] U: Unsupported *motion-affecting* code (cutter comp, canned cycles)

**Geometry**
- [ ] E: Arc geometry invalid — radius mismatch beyond `tolerance.arc_radius_mismatch`
- [ ] E: R-format arc with coincident endpoints
- [ ] E: Axis travel limit exceeded — **checked on interpolated points, not just block endpoints**, since an arc can bulge past a limit mid-sweep. Downgraded to W when the active work offset is unknown.
- [ ] E: Rotary travel limit exceeded when `axes.a.wrap = false`
- [ ] W: Rotary move > `rotary_wrap_warn` degrees in one block (default 360; legitimate for multi-turn wrapping, so tunable)

**Process**
- [ ] E: Cutting move (G1/G2/G3) with no feed rate ever set
- [ ] E: G93 inverse-time active with no F on a cutting block (G93 requires F per block)
- [ ] E: Feed rate exceeds `limits.max_feed`
- [ ] E: Spindle S exceeds `limits.max_spindle_rpm`
- [ ] W: Units never explicitly set (G20/G21 missing)
- [ ] W: No work offset selected before motion
- [ ] W: Cutting move before spindle start (M3/M4)
- [ ] W: M6 tool change without prior retract to safe Z (when `safety.retract_before_toolchange`)
- [ ] W: M6 with no tool number ever set
- [ ] W: Coolant on with spindle off
- [ ] W: Rapid below `min_clearance_z`
- [ ] W: G91 active at program end
- [ ] W: Program lacks M2/M30

Diagnostics report positions **in the program's declared units**. A message reading "X exceeds 400 mm" against a program written in inches is not actionable.

## Automatic Fixes (each produces a reviewable diff)

- [ ] Add safety preamble (G90 G21 G17 + safe Z retract) — prompted
- [ ] Inject feed rate on first cutting move — prompted, user supplies value
- [ ] Recompute arc centers (IJK) to eliminate radius mismatch — moves the center to the point equidistant from both endpoints along the perpendicular bisector, preserving both endpoints. **Refuses when the mismatch exceeds 10× tolerance**, since at that point the intent is genuinely ambiguous and the "fix" would be inventing geometry.
- [ ] Convert G2/G3 R-format → IJK; IJK → R **refuses on full circles** (inexpressible) and honours the >180° sign convention
- [ ] Normalize formatting: whitespace, case
- [ ] Strip/renumber N-words — **off by default.** Operators use N-numbers to restart mid-program and some dialects use them as jump targets; renumbering is destructive in ways that are not visible in the diff.
- [ ] Append M30 if missing

Fixes never write to the original file; they modify the editor buffer and the user saves explicitly.

**Applying a fix invalidates every line number.** Inserting a preamble shifts all subsequent lines, invalidating every Diagnostic and every `SegmentStore.line` entry — and a second fix's diff will not apply cleanly to a buffer the first one changed. The contract is therefore: **apply exactly one fix → re-parse the whole buffer → re-verify → rebuild segments.** No batch application, no diff rebasing.

`Diagnostic.fix_ids` is a **list**: several fixes (formatting, preamble) are not tied to any diagnostic, and some diagnostics have more than one candidate fix.

## Rotary Kinematics (critical design decision)

**Table mount** (`rotary_mount = "table"`, part rotates): to display what is actually cut, transform tool positions into **part coordinates** by applying the inverse rotation about the rotary centerline:

```
p_part = R_axis(-A) @ (p_tool - centerline) + centerline
```

**Head mount** (`rotary_mount = "head"`, tool rotates): render in machine coordinates — but *not* by simply reusing the machine XYZ. When the head swings about its pivot, the tool **tip translates as well**, unless the tip sits exactly at the pivot. The tip path is:

```
p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)
```

with `p_pivot` the programmed machine position. `pivot_to_tip` is required in the profile for head-mount machines; refuse to load a head-mount profile without it.

**Interpolation step size.** Simultaneous XYZ+A moves must be interpolated in small parameter steps and transformed *per step* — endpoint-only transformation renders helical and wrapped toolpaths as straight chords. Step size is driven by `tolerance.rotary_chord`, evaluated at the maximum distance of the path from the centerline (we have no stock model, so path radius is the proxy for part radius). This directly sets the rotary segment budget.

## Milestones

### M0 — Skeleton and de-risking spikes

Cheap now, expensive later. Nothing here ships, but two of these spikes can invalidate Tech Stack choices, and we want to know that before M2 rather than during M5.

- `.venv/` at the repo root, `.gitignore`d; editable install (`pip install -e ".[dev]"`)
- `pyproject.toml`, package skeleton, ruff + pytest wired
- GitHub Actions matrix (Ubuntu + Windows) green on an empty test suite
- **Render spike — DONE, pyqtgraph survives.** `spikes/render_500k.py`. 376.9 fps median at 500k segments in 10 `GLLinePlotItem`s on Intel Iris Xe, against a 30 fps requirement. Full numbers in § Rendering Constraints. No raw `QOpenGLWidget` needed.
- **Packaging spike — LINUX PASSES, WINDOWS OUTSTANDING.** `spikes/gl_window.py`, bundled with `scripts/build.py --entry spikes/gl_window.py --name gl-spike`.
  - **Ubuntu/Pop!_OS 22.04: passes.** One-dir bundle is **410 files plus 30 symlinks, 265 MB**. PySide6, shiboken6, PyOpenGL and pyqtgraph all import; pyqtgraph's 87 package-data files (CET colormap CSVs and friends) survive PyInstaller's analysis; all nine Qt platform plugins including `libqxcb.so` are bundled; the window paints a real frame on `Mesa Intel(R) Iris(R) Xe Graphics`. Exit 0. **`HIDDEN_IMPORTS` in `scripts/build.py` is still empty — nothing needed adding.**
  - **Windows: not yet run.** This is the half that actually carries risk: DLL resolution, the VC++ runtime, and AV heuristics all differ, and none of the Linux result transfers. Copy `dist/gl-spike/` to a VM with no Python or dev tooling and run `gl-spike.exe` from a terminal.
  - The 265 MB measured size corroborates rejecting one-file packaging: a one-file build would re-extract that on every launch, matching the ~200 MB figure this plan already cited.
- **Done when:** both spikes have a measured answer recorded in this file. *(Render spike recorded; packaging spike passes on Linux, Windows outstanding.)*

### M1 — Parser core + verifier (no GUI)

- Tokenizer, parser, modal state machine
- **Machine profile loading** — moved here from M4; the verifier's limit checks cannot run without it, and `foursight check` is an M1 deliverable
- Verifier rules: all structural and process checks, plus geometry checks that need no simulation
- CLI: `foursight parse file.nc` dumps commands; `foursight check file.nc` runs the verifier
- **Done when:** all fixture files parse; each broken fixture produces exactly its expected diagnostic

### M2 — Simulation + viewer

Built on the **4-axis-shaped data model from day one**, with the kinematics transform as identity until M4. `SegmentStore` carries `rot` and `lin_part` from the start, and interpolation is per-step rather than endpoint-only. Retrofitting these into a genuinely 3-axis M2 would mean rewriting the simulator, the store, and the buffer builder — which is exactly the rework M4 used to imply.

- Simulator with line + arc interpolation, adaptive chord tessellation
- `sim/timing.py`: per-segment duration, combining linear and rotary as `max(linear_time, rotary_time)`
- Qt window, GL viewport, batched polyline rendering (rapids red, feeds green — see Rendering Constraints on dashes)
- Open file, view toolpath, orbit/pan/zoom camera
- Simulation off the GUI thread with progress reporting
- **Done when:** a 100k-line file parses and renders within 5 s, and sustains ≥ 30 fps while orbiting, on the M0 spike's baseline hardware — **Intel Iris Xe Graphics (ADL GT2), Mesa 25.1.5, Pop!_OS 22.04**. Measure with vsync off; with vsync on every result pins to ~60 fps and the gate cannot fail.

### M3 — Editor sync + diagnostics UI

- Code pane with syntax highlighting
- Click a line → highlight segments
- **Click a segment → jump to line.** This is not a one-liner: with 500k segments in ≤ 10 batched buffers, Qt item picking is unavailable. Implement GPU colour-picking to an offscreen target, or a CPU KD-tree over segment midpoints. Pick one in M3 planning and budget for it.
- Diagnostics panel listing verifier output, click → jump to line; unsupported spans visually distinct
- Timeline scrubber animating tool position, driven by `SegmentStore.duration`

### M4 — Rotary kinematics

With the data model already 4-axis-shaped, this milestone is the transform itself.

- `kinematics.py`: table and head transforms; populate `lin_part`
- Rotary-aware step sizing from `tolerance.rotary_chord`
- Fixture tests: known wrap toolpath (e.g. helix on cylinder) matches the analytic result
- **Done when:** a 4-axis wrapping program renders as the correct cylindrical path

### M5 — Fixer + packaging

- Fix engine with diff preview dialog, one-fix-then-re-parse contract
- `fileio/loader.py` large-file handling hardened (encoding detection, latin-1 fallback, BOM, CRLF)
- PyInstaller one-dir builds for Ubuntu and Windows, smoke-tested on both
- README with screenshots

## Performance Requirements

- **Parse ≥ 50k lines/sec** — ~20 µs per line in CPython. Reachable, but only with `slots=True` on every hot dataclass, shared copy-on-write `ModalState`, `SourceRef` holding offsets rather than string copies, and one compiled regex per line rather than per word.
- **Render 500k+ segments interactively** — pre-batch into ≤ 10 buffers grouped by kind; never one draw call per move.
- **Memory — target restated after measurement (T0.7).** The columnar store's predicted ~38 MB per 500k segments is **confirmed**: measured geometry cost is 40 MB at 500k, 76 MB at 1M, 157 MB at 2M — linear at ~39 MB per 500k. But the original "≤ 250 MB resident for a 500k-segment program, including coordinate arrays and GL buffers" is **not achievable, and never was**: the fixed Python + Qt + Mesa baseline is **233 MB** with only 1k segments on screen, before any real geometry exists. A total-RSS cap therefore measures the interpreter and GL driver, not our data model. The budget binds on what the data model actually controls: **geometry + GL buffers ≤ 50 MB per 500k segments** (measured 40 MB). Total resident is recorded rather than capped — 273 MB at 500k on the baseline machine — because the fixed component is platform- and driver-dependent. Per-object segments would blow the geometry budget ~6× and remain ruled out.
- Simulation runs off the GUI thread (QThread) with progress reporting for large files.

### Rendering Constraints

#### T0.7 render spike result — MEASURED, pyqtgraph holds

Baseline hardware (also the answer to "baseline hardware" for the M2 gate): **Intel Iris Xe
Graphics (ADL GT2), Mesa 25.1.5, Pop!_OS 22.04, Python 3.12.10, pyqtgraph 0.14.0**, 1280×800
window, 10 `GLLinePlotItem`s, `mode='lines'`, timed inside `paintGL` after `glFinish`, 20 warmup
frames discarded.

| Segments | fps median (vsync off) | Worst frame | Resident |
|---|---|---|---|
| 1k (baseline overhead) | 59.8 *(vsync-capped)* | — | 233 MB |
| **500k** | **376.9** | 5.6 ms | 273 MB |
| 1M | 275.5 | 5.7 ms | 309 MB |
| 2M | 188.6 | 9.1 ms | 390 MB |

**Verdict: pyqtgraph HOLDS, with ~12× margin at the 500k target** — 376.9 fps median against a
30 fps requirement, worst frame 5.6 ms. Even at 2M segments (4× target) it sustains 188 fps. The
raw `QOpenGLWidget` fallback is **not needed**; M2 builds on pyqtgraph.

With vsync on, every configuration from 1k to 500k reports ~60 fps, because that measures the
display refresh rate rather than the GPU. Always measure uncapped (`vblank_mode=0` on Mesa/GLX)
before drawing conclusions about headroom; the spike now prints a warning when it detects this.

#### Item and driver constraints

**Verified against pyqtgraph 0.14.0** by `spikes/render_500k.py --introspect-only` (T0.7). Two of
the three assumptions originally recorded here were written against an older pyqtgraph and were
wrong; re-run the spike's introspection on every pyqtgraph upgrade, because these answers are
version-specific.

- **CONFIRMED — `GLLinePlotItem` has no dash or line-stipple parameter.** `setData` accepts exactly
  `['pos', 'color', 'width', 'mode', 'antialias']` and raises on anything else. "Rapids dashed"
  therefore requires baking dashes into the geometry (roughly doubling rapid vertex count) or
  settling for a colour-only distinction. Default to colour-only.
- **CORRECTED — pyqtgraph 0.14.0 does *not* use the legacy fixed-function path.** `GLLinePlotItem`
  draws through a shader program with `glVertexAttribPointer` + `glDrawArrays`, holds **persistent
  VBOs** (`m_vbo_position`, `m_vbo_color`, `QOpenGLBuffer`), and re-uploads vertex data **only when
  a dirty flag is set** — not per frame. No `glBegin`, `glVertexPointer`, or `glEnableClientState`
  anywhere. This materially lowers the risk of needing a raw `QOpenGLWidget`: the modern path we
  would have dropped down to write ourselves is already what pyqtgraph does. It does **not** settle
  the framerate question, which still needs the measurement half of T0.7.
- **CONFIRMED and sharpened — do not depend on line thickness to carry meaning.** Beyond drivers
  being free to ignore `glLineWidth > 1.0`, pyqtgraph itself *deliberately skips the
  `glLineWidth` call entirely* on a core forward-compatible profile, because such contexts error
  on any width but 1.0. So on those contexts `width=` is silently inert.
- The "≤ 10 vertex buffers" requirement is in practice "≤ 10 `GLLinePlotItem`s"; each item owns its
  own VBOs.

## Dialect Divergences

Where LinuxCNC and Fanuc disagree, and what we do:

| Construct | LinuxCNC | Fanuc | FourSight |
|---|---|---|---|
| `G4 P` units | seconds | milliseconds | seconds; warn if P > 60 as a likely ms/s confusion |
| Arc center default | G91.1 (incremental) | always incremental | G91.1 default, G90.1 honoured |
| Comments | `( )` and `;` | `( )` | both accepted |
| Comment placement | stripped before parsing, so `X (c) 10` == `X10` | same | strip first (LinuxCNC) |
| O-words | flow control (`O100 sub`) | `Oxxxx` program number | bare `Oxxxx` consumed silently; flow control reported as one `unsupported` construct |
| Program framing | none required | `%` … `%`, `Oxxxx` | consumed silently, never flagged |

**Block delete (`/`)**: simulated **with block-delete OFF by default** (deleted blocks execute), matching the common control-panel default. Exposed as a toggle in the GUI and a `--block-delete` CLI flag; the verifier runs against the active mode.

## Testing Strategy

- **Parser/verifier:** pure functions, exhaustive pytest with fixture `.nc` files.
- **Broken fixtures: baseline plus one mutation.** "Each broken file triggers exactly one diagnostic" is brittle in practice — a file missing G21 also trips "no work offset" and "lacks M30." Instead keep one known-clean baseline file, derive each broken fixture by a single mutation, and assert on the *newly added* diagnostic relative to the baseline's diagnostic set.
- **Arc math:** hypothesis property tests — interpolated points equidistant from center within 1e-6; tessellation never exceeds `tolerance.arc_chord`; R↔IJK round-trips where expressible.
- **Kinematics:** compare transformed paths against closed-form expectations (helix on a cylinder).
- **Golden tests:** hash `SegmentStore` geometry (rounded to tolerance) per fixture. This is the highest-value regression net for a geometry engine — refactors that silently move the toolpath are otherwise invisible.
- **Performance regression:** assert the parse rate and the segment count per fixture in `test_perf.py`. Hard numbers in the plan need a test or they decay.
- **GUI:** manual test script per milestone; keep the GUI layer thin so logic stays testable.

## Conventions for Claude Code

- **Everything Python runs inside the project venv at `.venv/`.** Never invoke bare `python`, `pip`, `pytest`, or `ruff` — they may resolve to system Python. Either activate first, or call the venv binaries directly (`.venv/bin/python`, `.venv/bin/pytest`; `.venv\Scripts\` on Windows). If `.venv/` is missing, create it before running anything:

  Bootstrap with an **explicit 3.11+ interpreter** — bare `python3` is not guaranteed to be one (on the current dev machine it is 3.10). See CLAUDE.md for the interpreter path in use.

  ```bash
  <python3.11+> -m venv .venv
  .venv/bin/pip install -e ".[dev]"
  ```

- All geometry in numpy float64; machine units are mm internally (convert G20 inputs on parse). Diagnostics report in the program's declared units.
- **Never take a norm across linear and rotary components.** They are separate columns for this reason; combine them via `sim/timing.py`.
- `lin` is machine coordinates, always. Display transforms write `lin_part`; they never mutate `lin`.
- Every module in `src/foursight/` must be importable without Qt installed except `gui/`.
- Dependency direction is `parser → machine → sim → verify → fix → gui`. `parser/model.py` must not import from `machine/`.
- `slots=True` on every dataclass on a hot path (`SourceRef`, `ModalState`, `Command`).
- Run `pytest -q` after every change to parser, sim, verify, or fix modules.
- Run `ruff check --fix . && ruff format .` before finishing any task; code must pass both with zero errors.
- No new third-party dependencies without updating this file's Tech Stack table.
- Keep functions under ~50 lines; prefer dataclasses over dicts for structured data.

### Ruff configuration (pyproject.toml)

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests"]
# Ruff formats Python code blocks embedded in Markdown. This file's snippets use aligned
# comment columns deliberately, and `ruff format .` rewrites them — so Markdown is excluded.
extend-exclude = ["*.md"]

[tool.ruff.lint]
# "S" must be selected for the tests/* S101 ignore below to mean anything.
select = ["E", "F", "W", "I", "N", "UP", "B", "SIM", "NPY", "S"]
ignore = ["E501"]  # line length handled by formatter

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101"]                       # assert is the point
"scripts/*" = ["S603", "S607"]             # build.py shells out to PyInstaller
"src/foursight/machine/kinematics.py" = ["N806"]  # R for a rotation matrix is correct
```

## Future (post-v1)

- Stock/material removal simulation (dexel-based, likely C extension or OpenCAMLib)
- Tool library with geometry, collision checking against fixtures
- 5-axis (B/C) support — `rot` generalizes to `(N, 2, R)`
- Canned cycle expansion (G81–G89), cutter compensation, LinuxCNC O-word subset
