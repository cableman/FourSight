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
│   │   ├── session.py      # path -> commands -> geometry + what to disclose (NO Qt)
│   │   ├── background.py   # QThread that loads off the GUI thread; cancellable
│   │   ├── batching.py     # SegmentStore -> GL vertex batches (NO Qt; see Batching layer)
│   │   ├── viewport3d.py    # GL view and camera; thin, because batching.py holds the logic
│   │   ├── picking.py       # segment ↔ screen hit-testing (see Picking)
│   │   ├── editor.py        # code pane, line highlighting
│   │   ├── timeline.py      # play/pause/scrub
│   │   └── diagnostics_panel.py
│   ├── fileio/              # NOT `io/` — that shadows the stdlib module
│   │   └── loader.py        # file loading, encoding detection, large-file handling
│   └── profiles/            # package DATA, not the repo root: an installed app and a
│       └── default_4axis.toml   # PyInstaller bundle must both be able to find it
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
class ParseError:                 # malformed input, REPORTED not raised
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
    errors: list[ParseError] | None = None
```

- **`ParseError` is deliberately not a `Diagnostic`.** The dependency direction forbids the parse layer from naming a `verify` type, so the tokenizer reports neutral facts and the verifier attaches severity (T1.7).
- **`comments` and `errors` are `None` when empty**, not `[]`. Both are empty on the overwhelming majority of lines, and a 100k-line file would otherwise allocate 200k throwaway lists against a ~20 µs/line budget. Read them as `line.comments or ()`.
- **N and O never appear in `words`.** An N-number labels the line; a bare `Oxxxx` is a Fanuc program number, consumed silently.
- **Comments are stripped before words are scanned**, matching LinuxCNC, which makes `X (why not) 10` a legal spelling of `X10`. They are blanked in place rather than deleted, so every later offset — and therefore every error position — stays correct. Tokenizing them inline instead produced two *false* errors on valid input.
- **LinuxCNC O-word flow control** (`O100 sub`, `o<name> while`, …) is detected and reported as a single unsupported construct. Without that, the letters of `sub` surfaced as three bogus "address has no value" errors, which would have hidden a construct that decides *which motion runs* — `unsupported`, never a warning.
- **Measured: 159k lines/sec (6.27 µs/line)** on the baseline machine for a realistic mix, or 31% of the 20 µs/line budget, leaving ~13.7 µs for the resolver.

### Loader layer (T1.4)

`load(path)` / `load_text(bytes)` → `LoadedFile(text, encoding, had_bom, newline, path)`. Decoding
is split from I/O so the encoding rules are testable without a filesystem. Hardened for large files
in T5.4.

Everything here exists to make **offsets trustworthy**, since every `SourceRef` indexes into this
text and editor sync, diagnostics and fixes all anchor on it:

- **Offsets are character offsets into the decoded, BOM-free text** — not byte offsets into the
  file. For a UTF-16 file the two differ, and the editor holds the decoded text, so decoded wins.
- **The BOM is stripped.** Left in place, line 1 would begin with an invisible `﻿` that the
  tokenizer would correctly report as garbage.
- **Line endings are preserved, never normalized.** Rewriting CRLF to LF would shift every offset
  after line 1 relative to the text handed to the editor. LF, CRLF and lone CR all work.
- **Encoding order:** UTF-8 BOM, UTF-16 LE/BE BOM, then UTF-8 strict, then latin-1. latin-1 maps
  every byte so it cannot fail — `LoadedFile.used_fallback` flags it, because silently claiming
  success on a mis-decoded file is worse than saying so.
- **Binary input raises `FileLoadError`** rather than latin-1 decoding megabytes of garbage into
  thousands of meaningless diagnostics. The NUL-byte check runs on the *decoded* text, since
  legitimate UTF-16 is full of NUL bytes.
- **`line_starts(text)` is a function, not a `LoadedFile` field.** The tokenizer already tracks
  offsets as it goes, so materializing ~100k ints on every load would cost several MB for nothing;
  fixes and jump-to-line can ask when they need it. It is built from
  `splitlines(keepends=True)` — the same primitive the tokenizer walks — so the two agree **by
  construction** rather than by two implementations happening to match.

### Resolver layer (T1.3)

`resolve(lines)` → `ParseResult(commands, errors)`; `parse(text)` tokenizes and resolves in one
call and is the entry point the CLI uses. Errors from both stages land in one list, in source
order.

- **Modal groups** use LinuxCNC numbering, restricted to the v1 subset. Two codes from one group in
  a block is an error. **Group 1 includes the canned cycles G80–G89**: they are not *interpreted*,
  but they must be resolved as motion modes, because that is exactly what makes a following bare
  `X10 Y10` a drill cycle rather than a straight line.
- **M-code groups** follow LinuxCNC too, which puts M7/M8/M9 in one group — so `M7 M8` is reported
  as a conflict even though mist plus flood is physically meaningful. The normative dialect wins.
- **`ModalState` has no motion field**, so the resolver carries the motion mode itself and puts the
  resolved value on `Command.motion`. G80 sets it back to `None`; non-modal codes (G4, G53, G28, …)
  leave it untouched, so `G53 G0 X0` keeps G0 active and a mid-contour dwell does not cancel G1.
- **A repeated address word is an error, not last-one-wins.** `G1 X10 X20` would otherwise lose the
  conflict silently in a letter-keyed dict.
- **`G43`/`G44` with no H word keeps the active offset** rather than clearing it; LinuxCNC falls
  back to the current tool's offset, and clearing would silently drop a real Z shift.
- **Unrecognized codes are passed through untouched**, not dropped: they stay in `Command.gcodes`
  so the verifier can classify them (warning if inert, `unsupported` if motion-affecting).
- **Measured: 118.6k lines/sec (8.43 µs/line) for the full parse — 2.4× the 50k target.** The
  resolver adds 2.31 µs/line on top of the tokenizer. On a 43k-command file, **one** `ModalState`
  instance is shared by every command, which is the copy-on-write design working as intended.

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

### Simulator (T2.5)

`simulate(commands, profile)` → `Simulation(store, spans, notes, unknown_durations)`. Composes
`MachineState`, `interpolate`, `timing` and `SegmentBuilder`; `lin` is written in machine coordinates.

**Spans are reported as source-line ranges, not as a seventh `SegmentStore` column.** Every segment
already carries `line[i]`, so `unverified_mask()` is one comparison away and the store keeps exactly
the six columns this plan specifies.

Three tiers of honesty, and the differences between them are the point:

- **Canned cycles are not drawn.** The span is suppressed and reported.
- **Cutter comp *is* drawn**, as the programmed centreline, with the span marked `unverified` so the
  renderer can style it distinctly. Refusing would refuse a large share of real programs, and the
  centreline is genuinely what was programmed.
- **An uninterpretable arc is not drawn**, with the interpolator's reason attached.

**The machine is assumed to start at its reference position**, and this is deliberately *not* noted —
a note present on every program says nothing. The stricter rule (refuse until every axis is
established) was implemented and then rejected: the first move along each axis becomes undrawable, so
a program that never mentions Y renders **empty**. That loses real geometry to avoid a bounded,
conventional assumption about one approach move's origin, and the cut geometry is identical either
way.

That is distinct from a **lost** position. `MachineState.position_lost` marks the state after an
undrawable G28, and those moves *are* suppressed: there we had a position and no longer do, so
assuming one would fabricate the rest of the program.

`progress(done, total)` is the seam T2.9 drives from a QThread; the simulator knows nothing about Qt.

### Timing (T2.4)

`sim/timing.py`. `rates_for(command, profile, rapid=…)` resolves the rates in force for a block;
`block_durations(lin, rot, rates, profile, rapid=…)` returns one duration per segment plus a count of
those it could not determine.

- **`max(linear_time, rotary_time)`, never a norm.** Also the physically correct answer: the axes
  move together, so the move takes as long as its slowest participant. The `np.linalg.norm` in this
  module spans X, Y and Z only.
- **`F` means different things depending on the block**, and this is the subtle one. On a
  *rotary-only* move F is degrees/min. On a *mixed* XYZ+A move F governs the linear path and A merely
  keeps up, bounded only by its own `max_rapid`. Collapsing the two makes 100 mm + 3600° at F600 take
  360 s instead of 60 s — and 360 s is close enough to the 360.1 s a mm/degree norm gives that the
  error looks like a different bug entirely.
- **`limits.max_feed` is in mm/min and must never bound a degrees-per-minute rate.** The linear and
  rotary clamps are applied to the unclamped F independently; sharing them makes F9000 on a
  rotary-only move come out as 3000 deg/min. The mm/degrees conflation can appear in the *limits*,
  not just the geometry.
- **Rapids ignore F** and use per-axis `max_rapid`: a coordinated rapid takes as long as its slowest
  axis, which is why per-axis rates matter rather than one vector rate.
- **G93 is a block time, not a rate.** The block takes `1/F` minutes whatever the distance, shared
  along the path *by length* so the speed stays constant — an even per-segment split would make the
  tool appear to slow down through a finely tessellated arc.
- **G95 without a spindle speed is unknown, not guessed.** Unknown durations are `0.0` **and
  counted**, so a timeline can report how much of itself is missing rather than silently pretending
  such moves are instantaneous.
- **Feed is clamped to `max_feed`** because the control would clamp; using the programmed value would
  under-report the time for a program the verifier is already flagging.

### MachineState (T2.2)

`machine/state.py` gains `MachineState`, which steps a command list and returns a `Step` per block:
the `Move`s it performs **in machine coordinates**, its dwell time, and an honest reason when its
geometry cannot be produced. The endpoint-only `walk` the verifier uses is unchanged.

Both coordinate frames are held at once — `programmed` is what the G-code says, machine is that plus
the work offset. Arc geometry and incremental moves are computed in the former while `SegmentStore.lin`
needs the latter, and conflating them is how an offset gets applied twice.

**Two constructs this plan lists as "interpreted" turn out not to be computable, and they are
resolved differently on purpose:**

- **G28/G30 reference return.** The reference point is machine-specific and appears nowhere in the
  G-code, so `[axes.*].home` was added to the profile (optional, unset in the shipped default). With
  no home configured the move is **not drawn** and the position afterwards becomes unknown — a
  reference move to a guessed target is exactly the confidently-wrong output this plan forbids, and
  claiming to still know the position would corrupt every later move too. `G28 X0 Y0` is two rapids:
  to the intermediate point, then to the reference point.
- **G43/G44 tool length.** There is no tool table (tool changes are "position tracking only, no
  geometry in v1"), so the H offset's length is unknown. Here suppression would be the **wrong**
  trade: G43 appears in nearly every real program, and refusing to draw them all makes the previewer
  useless. The offset shifts the Z datum uniformly *without changing the path's shape*, so the path
  **is** drawn and `Step.tool_length_unmodelled` records that Z is relative to the spindle rather
  than the tool tip. **A verifier rule reporting this is still owed** — by the strict taxonomy it is
  motion-affecting and uninterpreted, i.e. `unsupported`.

G4 dwell is carried through in **seconds** (LinuxCNC); the `P > 60` ms/s-confusion warning stays the
verifier's call, not the stepper's.

### Segment store implementation (T2.1)

`SegmentStore` plus a `SegmentBuilder`. **Measured 38.5 MB at N = 500k (77 bytes/segment)**, against
this plan's ~38 MB prediction and the 200 MB+ a per-object layout would cost.

- **`vertices` is a genuine zero-copy view.** `lin.reshape(-1, 3)` returns a view whose `.base` *is*
  `lin`, asserted with `np.shares_memory`. Converting to float32 for upload is the renderer's job;
  doing it in the store would double the resident cost.
- **Growth is chunked, then concatenated once.** A doubling realloc would leave up to 2× the needed
  capacity resident, which the 250 MB budget cannot spare; the single concatenate at `finalize` is
  the price of exactly-sized final arrays.
- **`add_polyline` is the vectorized path** interpolation should use. Appending 500k segments one at
  a time through Python would cost more than the interpolation itself. `rotations` is a **separate
  argument**, never a fourth column of `points`, so `(M, 4)` input is rejected rather than
  interpreted — the mm/degrees split is enforced at the call site, not just in storage.
- **`finalize` refuses a store containing `line == 0`.** An untraceable segment silently breaks
  editor sync, diagnostics and fixes, so it cannot be constructed in the first place.
- `set_part_coordinates` validates shape and dtype and **rejects an array aliasing `lin`**;
  `validate()` additionally checks C-contiguity, since non-contiguous data cannot be uploaded
  without a repack whatever its shape claims.

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
rotary_wrap_warn = 360.0    # degrees of rotary travel in one block before warning

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

### Loading rules (T1.5)

`load_profile(path)` / `load_profile_text(text)` → `MachineProfile`, all frozen dataclasses. The
shipped profile is `src/foursight/profiles/default_4axis.toml`, resolved at runtime through
`default_profile_path()`.

**Absence means "unknown", never a fabricated value.** This plan already states it for work offsets;
the same reasoning covers every limit. An invented `max_feed` of 3000 would produce confident
diagnostics about a machine we know nothing about. So `max_feed`, `max_spindle_rpm`,
`min_clearance_z`, per-axis limits and each offset are `| None`, and their absence *disables* the
corresponding check rather than inventing a bound. **Tolerances are the one exception** and carry
real defaults, because tessellation cannot proceed without a number.

- **Unset ≠ zero.** `[offsets]` with only `g54` leaves g55–g59 unset; `g54 = [0,0,0,0]` is *set to
  zero*. Collapsing them would turn "assumes zero offset" warnings into hard errors.
- **Offsets are keyed `'54'`…`'59'`**, matching `ModalState.offset`, so lookup needs no conversion.
- **A work offset mixes units.** `g54 = [x, y, z, a]` is three lengths and one **angle**: an inch
  profile scales the first three and never the fourth. Same rule for `[axes.a]`, where travel *and*
  rate are degrees. An unlabelled `[axes.b]`/`[axes.c]` is assumed rotary, because guessing
  "rotary" is recoverable while guessing "linear" corrupts the values by 25.4×.
- **`rotary_chord` scales despite its name** — it is a chord *height* in mm, measured at the path's
  maximum radius from the centerline.
- **Head mount without `pivot_to_tip` is refused at load time.** The tip translates as the head
  swings, so its path is unknowable without that distance; refusing beats rendering a wrong path.
- **Unknown keys are reported, not raised** (`MachineProfile.unknown_keys`), so a profile written
  for a newer version still loads — but the CLI **must** surface them, because `max_fed = 3000` is a
  typo that would otherwise silently disable the feed check.
- **Zero is a real value.** `value or default` is wrong here: `rotary_wrap_warn = 0` legitimately
  means "warn on any rotary move" and must not become 360.

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

### Verifier infrastructure (T1.6)

`Diagnostic(rule_id, severity, line, message, fix_ids, offset)` in `verify/report.py`; `Rule`,
`Program`, the registry and `verify()` in `verify/rules.py`.

- **`Severity` is a `StrEnum`** so CLI output and string comparisons need no conversion.
- **`Diagnostic` is frozen and hashable**, because the fixture strategy diffs the *set* of
  diagnostics a mutated file produces against its baseline. `fix_ids` is a `tuple` rather than a
  `list` so the dataclass can stay frozen — the plurality this plan calls for is what matters.
- **`rule_id` is not in the original sketch but is required.** Tests assert on it instead of on
  message text, and any future suppression mechanism keys on it. `offset` is carried when known,
  since `ParseError` already has it and discarding held information costs precise highlighting.
- **Rules see the whole `Program`**, not one command at a time: "no work offset before motion",
  "lacks M30" and "G91 active at program end" cannot be answered from a single block.
- **`Program` has no program-wide `units` field.** A program may switch G20/G21 mid-file, so the
  units that matter for a message are those in force at the offending block.
- **`Program.parse_errors` is carried unconverted.** Turning a `ParseError` into a `Diagnostic`
  means choosing a severity, which is a check's judgment (T1.7), not the driver's.
- **A rule that raises becomes one diagnostic about itself** and the others still run; the verifier
  exists to list every problem it can find. Diagnostics yielded before the failure are kept.
- **Registration is by `@register_rule` at class-definition time**, and `load_builtin_checks()` uses
  `importlib.import_module` rather than a plain `import`. An import kept purely for its registration
  side effect looks unused: `ruff check --fix` deleted exactly that line as F401, leaving the
  registry permanently empty. **A verifier that finds nothing is indistinguishable from a clean
  program**, so that failure was silent. Two tests guard it — one that the package really is
  imported, one that every module in `checks/` appears in `checks/__init__.py`.
- **Duplicate or empty `rule_id` is refused**, since a silent collision makes one rule invisible.
- Positions render in the program's declared units via `format_length` / `format_feed`;
  `format_angle` deliberately takes **no** units argument, because rotary values are degrees in
  every unit mode and a parameter there could only be misused.

### Structural checks (T1.7)

`verify/checks/structural.py`. Five rules: `structural.syntax-error`, `.modal-group-conflict`,
`.unsupported-oword`, `.unknown-code`, `.unsupported-motion`.

- **`ParseError` carries a `kind`** (`ParseErrorKind`), set by whichever layer produced it. Without
  it the verifier would have to pattern-match message *text* to tell a malformed word from a
  modal-group conflict, so any wording change would silently re-route a diagnostic to the wrong
  rule and severity. `ParseError` also carries `line`, since a `Diagnostic` is reported by line.
- **Unsupported constructs are reported per *span*, not per block.** A twenty-hole canned cycle is
  one thing the user needs to know about. Span boundaries come from the resolver's existing
  `Command.motion` and `ModalState.cutter_comp` rather than being re-derived, so a bare `X10 Y10`
  inside a cycle is already known to belong to it. An uncancelled span runs to end of program and
  says so.
- **Three code tables, and no code may appear in two of them:** `INTERPRETED_GCODES` (silent),
  the unsupported sets (`CANNED_CYCLES`, `CUTTER_COMP`, `UNSUPPORTED_ONE_SHOT`), and everything
  else, which warns. The *cancel* codes G40 and G80 are interpreted; their activations are not.
- **G61/G61.1/G64 and G98/G99 are accepted silently.** They change cornering or canned-cycle return,
  not the programmed centreline we draw, so there is nothing about the toolpath to warn on — and
  G64 appears in nearly every LinuxCNC program, so warning would be pure noise.
- **`G10` and `G92`/`G92.x` are `unsupported`, not warnings.** They shift the coordinate system or
  tool table, which changes where subsequent motion actually goes.
- **An unknown code is reported once per code, not once per line**, so a 100k-line file with a stray
  `G12` on every line yields one warning rather than 100k.

### Process checks (T1.8)

`verify/checks/process.py`, 13 rules covering PLAN.md's Process group, plus the G93-without-F check
the checklist mandates. Position tracking lives in `machine/state.py` — a minimal, **endpoint-only**
walker built here for the same reason profile loading moved into M1: the verifier cannot run without
it. T2.2 extends it for simulation; T2.8 re-runs limit checks over interpolated points.

- **Clearance and retract are judged in machine coordinates**, per this plan's rule that
  verification happens in machine coords. `machine_value()` adds the active work offset and reports
  whether that offset was actually *known*. A `G53` block is already machine-absolute and must not
  have the offset added again.
- **When the work offset is unknown**, the programmed value is used and the message says
  "assumes zero work offset". For travel limits PLAN downgrades `error` → `warning`; these rules are
  already warnings, so there is no tier below to drop to and the caveat carries the uncertainty.
- **Unknown position is treated asymmetrically, deliberately.** A rapid whose Z was never
  established is *not* judged — no position, no claim. A tool change whose Z cannot be established
  *is* a violation: if we cannot show the tool was clear, we cannot call the change safe.
- **A missing limit disables its check.** No rule invents a bound.
- **Most rules report once**, at the first offending line: "no feed rate ever set" is one fact about
  the program. `feed-too-high` and `spindle-too-high` report per offending word, since each is a
  separate programming decision to change.
- Coolant state is tracked inside its rule rather than added to `ModalState`; nothing else needs it.

### Geometry checks (T1.9)

`verify/checks/geometry.py`, 5 rules. **Travel limits here are endpoint-only** — T2.8 re-runs the
same check over interpolated points, because an arc can bulge past a limit mid-sweep while both of
its endpoints sit comfortably inside it. Nothing in this module proves a program stays in bounds.

- **Arc radius comparison happens in programmed coordinates**, deliberately: a work offset is a
  uniform translation and cannot change a radius, so applying one would add rounding for nothing.
- **`tolerance.arc_radius_mismatch` has exactly one source**, the profile — asserted by a test that
  the same arc is accepted under a loose profile and rejected under a strict one, so the T5.2 fix
  cannot drift from the check.
- **`geometry.arc-r-invalid`** covers the two ways an R-format arc fails to describe an arc:
  coincident endpoints (which is why a full circle is IJK-only), and |R| below half the chord, where
  no such circle exists. A semicircle, `2R == chord`, is the limiting valid case.
- **Linear travel downgrades `error` → `warning` on an unknown work offset**, per this plan. **Rotary
  travel does not** — the downgrade is attached specifically to the linear bullet, and a non-zero
  rotary work offset is rare; the assumption is stated in the message instead.
- **Rotary and linear limits are checked by separate rules.** A rotary axis is skipped by the linear
  rule, or every rotary violation would be reported twice.
- **`geometry.rotary-wrap` measures the block's *delta*, not its target.** A350 → A400 is a 50°
  move. It assumes A started at 0 when the axis has no established position, and says so: refusing
  to judge would skip the very first block, often the largest move in a wrapping program.

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
- **Plane-dependent IJK mapping:** G17 → I,J; G18 → I,K; G19 → J,K. **Resolved in T2.3:** G18's convention is counterintuitive because the *frame* must be right-handed. "G2 decreases the angle" only holds when `first × second = +normal`, and `X × Z = -Y`, so G18's 2D frame is **(Z, X)**, not (X, Z) — an (X, Z) frame is left-handed and silently reverses G2 and G3. The offsets follow the frame, so G18's first offset is **K**. `sim/interpolate.PLANES` is the single source for this; `verify/checks/geometry.py` derives its (order-irrelevant) axis pair from it rather than keeping a second copy.
- **Radius mismatch tolerance** comes from `tolerance.arc_radius_mismatch` in the profile. It has exactly one source; do not hard-code it at the two use sites (the check and the fix).
- **Tessellation is adaptive on chord height**, driven by `tolerance.arc_chord` — never a fixed step count. **Measured (T2.3):** at 0.01 mm a full circle needs **16** segments at r = 0.5 mm and **497** at r = 500 mm, so a fixed count is wrong by ~30× at one end or the other. The sagitta relation `h = r(1 - cos(Δθ/2))` inverts to a maximum step of `2·arccos(1 - tol/r)`; a radius at or below the tolerance needs one step.
- **Endpoints are snapped to the commanded values.** Real CAM output rounds X/Y and I/J independently, so the commanded endpoint sits microns off the circle the centre defines; ending an arc on recomputed trigonometry instead would leave a visible gap before the next block. Note this is invisible on a *mathematically consistent* arc, where the trigonometry reproduces the endpoint bit-exactly — so it needs a deliberately inconsistent test case.

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
- [x] E: Axis travel limit exceeded — **checked on interpolated points, not just block endpoints** (T2.8), since an arc can bulge past a limit mid-sweep. Downgraded to W when the active work offset is unknown. `Program.segments` carries the `SegmentStore` when a simulation has been run; without one the check falls back to endpoints and says so. Measured: an arc with both endpoints at Y90 inside a Y100 limit reaches **Y110** mid-sweep — 66 offending interpolated points, invisible to the endpoint check. Aggregated to **one diagnostic per (line, axis)** at the worst value, so a long breach reports once rather than per point.
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

### CLI (T1.11)

`foursight parse FILE` and `foursight check FILE`, both headless — the path imports no Qt, enforced
by CI's `.[dev]`-only job and by a subprocess test.

- **Exit codes:** `0` ran and found no errors, `1` errors found, `2` could not run (unreadable file,
  unusable profile, bad usage). `FileLoadError` and `ProfileError` are relayed as one-line messages,
  never tracebacks.
- **`unsupported` and `warning` do not fail the run.** A program that legitimately contains canned
  cycles must still pass a build pipeline; failing it would push users toward suppressing the whole
  check, which is worse than reporting a span we did not interpret.
- **Diagnostics and the summary go to stdout**, as linters do; stderr carries problems with the
  tool's own inputs. Mixing them interleaves unpredictably the moment stdout is piped.
- Output is compiler-style — `file:line: severity [rule_id] message` — so editors can jump to it and
  grep can filter it.
- **`--profile` defaults to the bundled profile**, resolved via `importlib.resources` rather than a
  path relative to the repo, which exists only in a checkout.
- **Unrecognized profile keys are printed to stderr.** Loading tolerates them so a newer profile
  still works, but an unsurfaced `max_fed = 3000` silently disables the feed check.
- A latin-1 decoding fallback is reported too: a mis-decoded comment is cosmetic, but silently
  claiming success on a non-UTF-8 file is not.

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

- **Windows CI is ~1.9× slower than the dev machine, and that is the number that matters.**
  Measured on `windows-latest`: **57,009 lines/sec (py3.11)** and **58,747 (py3.12)** against the
  50k floor — the target is met on the slowest hardware we test, but with only **14% headroom**. A
  15% regression is therefore caught; runner variance beyond that will occasionally fail the build.
  If it does, the answer is to make the parse faster or to revise the target with evidence — **not**
  to lower `FOURSIGHT_PERF_MIN_RATE` until the test stops complaining.
- **Verification on Windows CI costs 1,086 ms** for the same 42,858 commands, against 530 ms
  locally. For a 100k-line file that is ~2.5 s of the M2 gate's 5 s budget, on the platform we
  ship to. This makes the redundant-walk optimization below materially more attractive than the
  local numbers alone suggest.
- **Parse ≥ 50k lines/sec — MEASURED 110k (9.0 µs/line), 2.2× the target**, on the baseline machine
  for a realistic 50k-line mix. Asserted by `tests/test_perf.py`, which asserts the *floor* and logs
  the actual, so a slower CI runner does not flake. `FOURSIGHT_PERF_MIN_RATE` overrides the floor for
  a runner that genuinely cannot reach it. The tokenizer's share is 6.9 µs/line; one `ModalState`
  instance is shared across all 42,858 commands; per-line cost grows only 1.3× from 5k to 50k lines,
  so the parse is linear rather than quadratic.
- **Verification is not covered by a target, and currently costs more than the parse: 530 ms for
  42,858 commands (12.4 µs/command).** Measured breakdown: **72% of that (380 ms) is seven rules
  each independently re-walking the command list** to rebuild positions — `arc-r-invalid`,
  `arc-radius-mismatch`, `axis-travel-exceeded`, `rotary-travel-exceeded`, `rotary-wrap`,
  `rapid-below-clearance`, `toolchange-without-retract`. Computing the walk once and sharing it
  through `Program` would cut verification to roughly 200 ms. Deliberately **not** done in M1: no
  requirement is being missed, and it changes the rule contract. Revisit if the M2 gate
  (100k lines parsed and rendered in 5 s) turns out tight, since verification would take ~1.1 s of
  that budget.
- **Parse ≥ 50k lines/sec** — ~20 µs per line in CPython. **Measured 95k on the baseline machine; 47–50k on GitHub's shared runners**, which is why CI sets `FOURSIGHT_PERF_MIN_RATE=30000` rather than PLAN lowering the requirement. All four matrix legs land on top of a 50k threshold — three failed and one passed by 0.4% — so on that hardware the floor is a coin flip rather than a regression test. The requirement is about hardware a machinist would use, not a 2-vCPU shared runner; the CI override still catches a genuine halving. Reachable, but only with `slots=True` on every hot dataclass, shared copy-on-write `ModalState`, `SourceRef` holding offsets rather than string copies, and one compiled regex per line rather than per word.
- **Render 500k+ segments interactively** — pre-batch into ≤ 10 buffers grouped by kind; never one draw call per move. **Met with ~8× margin using the production renderer (T2.6): 241 fps median at 500,070 segments, worst frame 10.7 ms against the 33.3 ms a 30 fps floor allows** — and in *one* draw call, since a program whose motion is all feed needs only one batch. Measured through `ToolpathViewport`, not the spike's own batching, on the baseline Intel Iris Xe with the host under load ~4.0; the T0.7 spike's higher 376.9 fps used ten artificially-split items on an idle machine.
- **Memory — target restated after measurement (T0.7).** The columnar store's predicted ~38 MB per 500k segments is **confirmed**: measured geometry cost is 40 MB at 500k, 76 MB at 1M, 157 MB at 2M — linear at ~39 MB per 500k. But the original "≤ 250 MB resident for a 500k-segment program, including coordinate arrays and GL buffers" is **not achievable, and never was**: the fixed Python + Qt + Mesa baseline is **233 MB** with only 1k segments on screen, before any real geometry exists. A total-RSS cap therefore measures the interpreter and GL driver, not our data model. The budget binds on what the data model actually controls: **geometry + GL buffers ≤ 55 MB per 500k segments**. Total resident is recorded rather than capped — 273 MB at 500k on the baseline machine — because the fixed component is platform- and driver-dependent. Per-object segments would blow the geometry budget ~6× and remain ruled out.

  **The 50 MB figure was corrected to 55 MB by the first end-to-end measurement (T2.6).** T0.7 and T2.11 both measured geometry *only* and agreed at 38.5 MB; neither counted the **float32 copy GL requires**, which adds **12.0 MB** at 500k (1M vertices × 3 × 4 B). The real total with the renderer attached is **50.5 MB — 38.5 MB float64 store + 12.0 MB GL positions** — which quietly exceeded a budget set from the geometry half alone. The copy is irreducible: the store is float64 because geometry precision demands it, and GL takes float32, so both live at once. Raising the number is the honest fix rather than pretending the upload is free.

  What keeps that 12.0 MB from being 28 MB: **a uniform colour per batch, never a per-vertex colour array.** A per-vertex RGBA buffer would cost 16 MB at 500k — more than the positions — for information that is constant across an entire batch by construction.
- **Simulation cost is per *block*, not per segment — measured (T2.11).** ~40 µs per motion block,
  essentially independent of how much geometry that block produces. The consequence inverts the
  intuition behind "the 100k-line and 500k-segment targets are different axes":

  | Program | Blocks | Segments | Simulate | Segments/sec |
  |---|---|---|---|---|
  | 3,166 rotary lines (full-turn A sweeps) | 3,166 | 500,070 | **0.18 s** | 2,725,813 |
  | 100k CAM-style lines | 85,715 | 81,900 | **3.46 s** | 22,750 |

  **The 500k-segment target is met with ~25× margin; the 100k-line target is the binding one.** Parse
  plus simulate for 100k lines is **4.56 s**, so the M2 gate's 5 s budget is spent before rendering
  begins. Geometry is a non-issue at that size (6.3 MB).

  The cost is fixed numpy overhead on tiny arrays, not geometry math: `cProfile` attributes 35% of
  simulate to `sim/timing.block_durations` via `_durations`, which performs two `np.stack` calls and
  roughly six array reductions **per block** — on arrays of length 1 for the ordinary single-segment
  `G1`. `np.stack` is called 71,428 times and `linspace` 42,857 times for a 50k-line file. A fast path
  for single-segment blocks should recover most of that 35%, taking 100k lines to roughly 3.4 s. **Not
  done here** — T2.11 is a measurement task and PLAN sets no simulate-rate target — but T2.13 will
  need it, or a gate stated against blocks rather than lines.
- **Geometry budget confirmed through the real pipeline (T2.11).** T0.7 measured a directly-filled
  `SegmentBuilder`; simulating an actual program to 500,070 segments also costs **38.5 MB, 77 B per
  segment**, against the 50 MB budget. So no per-block bookkeeping has crept in between parser and
  store. Peak RSS is recorded, never asserted — `resource` is Unix-only and Windows is in the matrix.
- **Simulation runs off the GUI thread — done (T2.9).** `open_file` returns in **0 ms** where it
  previously blocked ~4.6 s; measured on the baseline machine, the event loop regained control **513
  times** during a real 100k-line load. `simulate` takes `progress(done, total)` and
  `cancelled() -> bool`, both Qt-free — a `threading.Event.is_set` satisfies the latter exactly — so
  `gui/background.py` owns the threading and no Qt type crosses into `sim/`. Polling on the progress
  interval keeps the cancel check free: ~43 calls for a 100k-line program against ~40 µs per block.
  **A cancelled run raises `SimulationCancelled` rather than returning a partial `Simulation`.** A
  half-stepped program is a truncated toolpath, and returning one invites a caller to draw it as though
  the program ended there. There is no honest way to render "the first 40% of this program", so the
  window keeps whatever it had — the same rule as a failed open. Cancellation is also checked between
  parse and simulate, because parsing is ~a quarter of the wall clock and has no progress seam of its
  own, so a user who cancels during it should not then wait out the simulation.

### GUI Shell

`gui/session.py` is the Qt-free half of the shell, holding the pipeline (path → text → commands →
geometry) and — the part with actual judgement — **what the window has to disclose**. Same split as
`batching.py`, and for the same reason: 33 of T2.7's 49 tests need no Qt.

The governing principle reaches the user here or nowhere. The simulator already refuses to draw what
it cannot interpret, but a refusal nobody sees is barely better than a confident lie, so the summary
separates two things that are easy to conflate:

- **suppressed** — geometry is *missing*. `ProgramSummary.incomplete` is true and a **banner** appears
  above the viewport saying the toolpath is incomplete.
- **unverified** — geometry is drawn but untrustworthy (a cutter-comp centreline). `incomplete` stays
  false and the warning goes to the status bar instead. Raising the banner here would fire it on a
  large share of real programs, which is exactly how a warning stops being read.

Also disclosed: parse errors, a latin-1 decode fallback, segments with no usable feed rate (so a short
time estimate is never presented as complete), and simulator notes such as unmodelled G43 tool length.
Cycle time is formatted as `1h 05m` rather than seconds, because it gets compared to a job sheet.

**A failed open leaves the loaded program untouched and on screen**, with its own name still in the
title bar. Clearing the viewport would lose the user's program to a mistyped filename, and drawing
nothing under the new name would misrepresent what they are looking at.

`foursight-gui` is the entry point, and `app.py` **imports Qt inside `main`, not at module scope**: a
console script imports the module to find `main`, so a top-level Qt import turns a missing `[gui]`
extra into a `ModuleNotFoundError` traceback before any of our code runs. Importing late makes it one
sentence naming the fix, with exit code 3. An unusable `--profile` exits 2 rather than opening with a
silently substituted default, which would make every limit and rapid rate wrong.

Simulation still runs on the GUI thread, so a 100k-line file freezes the window for ~4.6 s. T2.9 moves
it off; the wait cursor is the interim signal that the application is working rather than hung.

### Batching Layer

`gui/batching.py` turns a `SegmentStore` into GL-ready vertex batches, and **contains no Qt**. The
split is the point: what can be *wrong* about rendering is which segments end up in which batch, and
that is testable without a display, a GL context, or the `[gui]` extra. `viewport3d.py` is then thin
enough for the manual script to cover — 32 of T2.6's 46 tests need no Qt at all.

- **One batch per (kind, trust) pair — four for a typical program**, not ten. The ≤ 10 budget is a
  ceiling; fewer draw calls is strictly better, and the T0.7 spike used ten only to prove ten was
  survivable. A 500k-segment program whose motion is all feed renders in **one** draw call.
- **Untrusted geometry gets its own batch, never a shared one.** A cutter-compensated span is drawn as
  the programmed centreline, which is *not where the tool goes*. Batching it in with ordinary feeds
  would present it as understood, so the tier is enforced in the partition rather than left to a
  styling pass that a later change could drop. Rapids red, feeds green, both amber when untrusted.
- **Colour carries every distinction; line width carries none.** pyqtgraph skips the `glLineWidth`
  call entirely on core forward-compatible profiles, so anything encoded in thickness silently
  vanishes there with no error. All batches draw at width 1.0.
- **A uniform colour per batch, never per-vertex RGBA** — that array would cost 16 MB at 500k, more
  than the positions, to carry a value that is constant across the batch by construction.
- **The store is never mutated.** `lin` stays machine coordinates because the verifier reads it; the
  float32 conversion GL needs produces a new array, and `use_part_coordinates` reads `lin_part`.
- **A wrong-length untrusted mask is refused, not broadcast.** Numpy would happily broadcast a
  length-1 mask and mark nothing, silently downgrading every unverified span to ordinary geometry.

Two failure modes here are invisible to the eye and so are tested explicitly: **the viewport drawing
nothing**, and **the viewport still drawing the previous program**. The first was a real bug in the
first version of `_rebuild_items` — it called `clear_toolpath`, which reset `batches` to `[]` before
the loop that reads it, so no GL items were ever created and every program rendered as an empty scene,
with no exception and no warning. Qt's `offscreen` platform cannot create a GL context but *can*
construct widgets and add items, which is enough to catch both.

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

#### M2 gate result — MEASURED, gate met (T2.13)

End to end through `MainWindow` on the same baseline hardware: open the file, wait for the background
load, force one `paintGL` with `glFinish`, then orbit 120 frames discarding 20 as warmup.

| Criterion | Measured | Margin |
|---|---|---|
| 100k-line file parses and renders ≤ 5 s | **4.46 s** to first frame drawn | 11% |
| ≥ 30 fps while orbiting (81,900 segments) | **316 fps**, worst frame 4.99 ms | 10.5× |
| ≥ 30 fps at the 500k-segment target | **241 fps**, worst frame 10.7 ms | 8× |

100,000 lines → 85,715 blocks → 81,900 segments in 2 batches; geometry 6.3 MB + 2.0 MB GL.

**Rendering is not the constraint — simulation is.** Of the 4.46 s, roughly 4.44 s is parse and
simulate and ~20 ms is drawing. The gate passes as written, so the `_durations` fast path sized above
is not required for M2; it remains the lever if slower hardware ever has to meet this number, because
11% is not much margin and none of it is in the renderer.

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
- **Golden tests (T2.10):** a fingerprint per fixture in `tests/golden/segments.json`, pairing an exact `geometry_sha256` with **readable** fields — segment count, per-axis bounds, duration, rapid/feed split, spans. A bare hash is a poor golden: it says something changed and nothing about what, so a failure here names the field that moved.
  Geometry is **quantized before hashing**, to 1 µm and 0.001° — 10× finer than the 0.01 mm chord tolerance, so a real change cannot hide inside it, and ~10 orders of magnitude coarser than float64 noise, so a different libm's `cos` cannot break the build. `kind` and `line` are hashed unquantized: a rapid reclassified as a feed, or a segment attributed to the wrong line, are regressions too.
  Verified to catch what it is for: tessellation one step coarser fails 6 fixtures (`segments: 128 → 125`); a silent 0.1 mm shift fails 10 (`X: [0.0, 50.0] → [0.0, 50.1]`).
  Re-record with `FOURSIGHT_UPDATE_GOLDEN=1 pytest tests/test_golden.py`, and read the diff first.
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
