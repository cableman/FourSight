"""Carve the stock envelope into a heightfield, so the viewport can show the *end result*.

This is the material-removal preview PLAN.md § Non-Goals deferred, deliberately narrowed until it fits
inside the rule that governs everything here — **never render a confidently wrong toolpath**. A shaded
solid looks far more authoritative than a line does, so the narrowing has to be real and it has to be
visible to the user. What this model cannot do:

- **Undercuts.** A heightfield stores one number per cell. A T-slot cutter, a dovetail or a boring bar
  reaching under a lip renders as though the material above it were gone too.
- **More than one cutter.** There is no tool table in v1, so `[tool]` is one cutter for the whole
  program and a program that changes tools is carved wrongly wherever the other tool cut. `carve`
  is told which T numbers appeared and says so in `SolidField.notes`.
- **Untrusted geometry.** A cutter-compensated span is drawn as the programmed centreline, which is not
  where the tool goes; carving with it would be exactly the confidently-wrong output the plan forbids.
  Those spans do not cut, so the solid shows **more** material than reality — the recoverable direction —
  and says so.
- **Fixtures, clamps, or holding.** Not modelled at all.

Nothing in here is read by `verify/`, and that is a boundary rather than an oversight. Every rule judges
the programmed centreline; giving the cutter a width would change what several of them mean, and
`geometry.axis-travel-exceeded` measured against a tool edge rather than the spindle centre is a
different check, not a better one.

## Why the carve is two passes and not a swept volume

The direct approach — for each segment, touch every grid cell within the tool radius of it — is
``O(segments x kernel)``. At 500k segments and a 15-cell radius that is ~450M cell updates driven from a
500k-iteration Python loop, which is not a slow implementation of the right algorithm but the wrong
algorithm. For a flat end mill,

    height(x, y) = min over path points p with |(x, y) - p_xy| <= r of p_z

which is precisely a **grayscale erosion of the tool-axis height map by a disc of radius r**. So the
carve rasterises axis positions first (cost proportional to *path length*, not segment count) and erodes
once (cost proportional to *grid size*, independent of program size). A ball nose is the same erosion
against a non-flat structuring element — `bottom_offset` is that element, in one place so the two carve
paths cannot disagree about what a ball nose is.

## Why the grid resolution is derived rather than fixed

The erosion kernel grows with the square of the tool radius **in cells**, so a fixed cell size lets a
large cutter on a small part produce a kernel with tens of thousands of offsets and stall. The cell size
is therefore the coarser of "the part divided into `TARGET_CELLS`" and "the tool radius divided into
`MAX_RADIUS_CELLS`", which bounds the kernel at roughly 450 offsets no matter what is configured. A big
tool coarsens the picture instead of hanging the application.

## The two frames, and why a cylinder gets its own

`[stock]` has two shapes for a reason that carries straight over here: **a cylinder concentric with the
rotary axis is rotation-invariant and a box is not.**

- A **box** is carved as a top-down Z map in machine coordinates, and is refused outright once the
  program moves A under a table mount — a fixed box has stopped describing where the stock is, which is
  the same reason `geometry.rapid-into-stock` refuses it.
- A **cylinder** is carved as a radial map over (angle, axial) in **part** coordinates, which is exact
  under any rotation for the same invariance reason. It needs `SegmentStore.lin_part`, so it needs the
  display transform Ctrl+P already computes.

The cylinder carve treats the removal footprint as a disc of the tool radius on the *unrolled* surface.
That is exact where the tool axis is radial — the ordinary wrapped-machining case, tool over top dead
centre — and loosens as the contact point moves off it. Stated, not hidden.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from foursight.machine.kinematics import KinematicsError, axis_columns
from foursight.machine.profile import MachineProfile, StockBox, StockCylinder, Tool
from foursight.sim.segments import Kind, SegmentStore

#: Cells across the longer side of the stock, before the tool-radius bound below is applied.
TARGET_CELLS = 512
#: The tool radius may never exceed this many cells. This is what bounds the erosion kernel — the disc
#: holds about `pi * MAX_RADIUS_CELLS**2` offsets, ~450 here, and each one is a single vectorised pass
#: over the grid. Raising it sharpens a large cutter's edges and costs quadratically.
MAX_RADIUS_CELLS = 12
#: Angular cells are never fewer than this, however short the blank. Below it the seam of the wrap is
#: visibly faceted and reads as a modelling artefact rather than as resolution.
MIN_ANGULAR_CELLS = 180
#: Tool-axis positions are sampled at this fraction of a cell along every cutting move. Anything coarser
#: skips cells the axis genuinely crossed, which leaves ridges of uncut material along diagonal moves.
CELL_FRACTION = 0.5
#: Samples materialised at once. Purely a memory bound — the samples are scattered chunk by chunk, and
#: the step is never coarsened to fit, because coarsening is what produces the ridges above.
CHUNK_SAMPLES = 1 << 21


class SolidError(Exception):
    """The program or profile does not describe enough to carve a solid.

    Raised rather than defaulted, for the reason every refusal in this project is: a solid carved from a
    guessed cutter or a guessed blank is a confident picture of the wrong part, and it does not look like
    an error to anyone reading it.
    """


@dataclass(frozen=True, slots=True)
class SolidField:
    """A carved heightfield plus the frame that places it in space.

    ``height`` is ``(nu, nv)``. What it *means* depends on ``kind``, and so does the mapping back to
    world coordinates, which is why `points` lives here rather than in the renderer:

    - ``"box"``    — ``height[i, j]`` is Z, over a grid of X (``u``) and Y (``v``), machine coordinates.
    - ``"cylinder"`` — ``height[i, j]`` is the radius from the rotary centreline, over a grid of angle in
      degrees (``u``) and position along the rotary axis (``v``), **part** coordinates.

    ``ceiling`` is the uncut value and ``floor`` the deepest the stock goes, so ``height == ceiling``
    means "never touched" and is what makes `carved` answerable.
    """

    kind: str
    height: np.ndarray
    origin: tuple[float, float]  # world value of cell (0, 0); u then v
    cell: tuple[float, float]  # cell size in u and v, degrees for a cylinder's u
    floor: float
    ceiling: float
    wraps_u: bool = False
    #: Cylinder only: the axial column, the two radial columns, and the centreline they are measured from.
    axial: int = 2
    radial: tuple[int, int] = (0, 1)
    centerline: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: True when `height` is in part coordinates, so the viewport can refuse to draw it in the other frame.
    part_coordinates: bool = False
    #: Everything the picture cannot show about its own accuracy. Surfaced verbatim by the legend.
    notes: tuple[str, ...] = ()

    @property
    def carved(self) -> bool:
        """Whether anything was removed at all. A blank solid means the program cut nothing here."""
        return bool(np.any(self.height < self.ceiling))

    def points(self) -> np.ndarray:
        """``(nu, nv, 3)`` world coordinates of every grid vertex, in this field's frame."""
        nu, nv = self.height.shape
        u = self.origin[0] + np.arange(nu, dtype=np.float64) * self.cell[0]
        v = self.origin[1] + np.arange(nv, dtype=np.float64) * self.cell[1]
        out = np.empty((nu, nv, 3), dtype=np.float64)
        if self.kind == "box":
            out[..., 0] = u[:, None]
            out[..., 1] = v[None, :]
            out[..., 2] = self.height
            return out
        first, second = self.radial
        theta = np.radians(u)[:, None]
        out[..., self.axial] = v[None, :]
        out[..., first] = self.centerline[first] + self.height * np.cos(theta)
        out[..., second] = self.centerline[second] + self.height * np.sin(theta)
        return out


def bottom_offset(distance: np.ndarray, tool: Tool) -> np.ndarray:
    """How far **above the tool tip** the cutting edge sits, ``distance`` from the tool axis.

    The structuring element of the erosion, and the only place the cutter's shape is interpreted. A flat
    end mill reaches its tip depth everywhere inside the radius; a ball nose rises away from the tip as
    ``r - sqrt(r**2 - d**2)``.
    """
    d = np.asarray(distance, dtype=np.float64)
    if tool.shape == "ball":
        # Clamped before the root. A distance a hair over the radius from floating-point error would
        # otherwise be a NaN, and one NaN in a min-reduction silently poisons every cell it reaches.
        inside = np.clip(d, 0.0, tool.radius)
        return tool.radius - np.sqrt(tool.radius**2 - inside**2)
    return np.zeros_like(d)


def carve(
    store: SegmentStore,
    profile: MachineProfile,
    *,
    untrusted: np.ndarray | None = None,
    tool_numbers: Sequence[int] = (),
    target_cells: int = TARGET_CELLS,
) -> SolidField:
    """Carve ``profile``'s stock with ``profile``'s tool along ``store``'s cutting moves.

    ``untrusted`` is `Simulation.unverified_mask()`. Those segments do not cut — see the module
    docstring — and their presence becomes a note rather than a silent omission.

    ``tool_numbers`` are the distinct T words the program used. More than one is a note, because this
    model has exactly one cutter and cannot honour the others.
    """
    tool = profile.tool
    if tool is None:
        raise SolidError(
            "no [tool] in the machine profile: the solid view needs a cutter diameter, and there is no "
            "tool table to take one from. Carving with a guessed cutter would draw a confident picture "
            "of the wrong part"
        )
    if profile.stock is None:
        raise SolidError(
            "no [stock] in the machine profile: without a blank there is nothing to remove material "
            "from, and an invented envelope would show a part that was never on the table"
        )

    cutting = _cutting_mask(store, untrusted)
    notes = _notes(store, untrusted, tool_numbers, cutting)

    if isinstance(profile.stock, StockCylinder):
        return _carve_cylinder(store, profile, profile.stock, tool, cutting, target_cells, notes)
    return _carve_box(store, profile, profile.stock, tool, cutting, target_cells, notes)


# ------------------------------------------------------------------------------ what actually cuts


def _cutting_mask(store: SegmentStore, untrusted: np.ndarray | None) -> np.ndarray:
    """Feed moves that are trusted. Rapids never cut, and neither does geometry we did not interpret."""
    mask = store.mask(Kind.FEED)
    if untrusted is None:
        return mask
    if untrusted.shape != (len(store),):
        raise SolidError(
            f"untrusted mask has shape {untrusted.shape}, expected ({len(store)},) — one entry per "
            f"segment. A wrong-length mask would carve with spans we cannot vouch for"
        )
    return mask & ~untrusted.astype(bool, copy=False)


def _notes(
    store: SegmentStore,
    untrusted: np.ndarray | None,
    tool_numbers: Sequence[int],
    cutting: np.ndarray,
) -> tuple[str, ...]:
    """The caveats this particular carve carries, in the words the legend will show."""
    notes: list[str] = []
    distinct = sorted({int(number) for number in tool_numbers})
    if len(distinct) > 1:
        listed = ", ".join(f"T{number}" for number in distinct)
        notes.append(
            f"carved with one tool; the program uses {listed}, and the others are not modelled"
        )
    if untrusted is not None and bool(np.any(untrusted & store.mask(Kind.FEED))):
        removed = int(np.count_nonzero(untrusted & store.mask(Kind.FEED)))
        notes.append(
            f"{removed} unverified cutting segments were not carved, so more material is shown than "
            f"the program actually leaves"
        )
    if not bool(np.any(cutting)):
        notes.append("no trusted cutting moves, so nothing was removed")
    return tuple(notes)


# ---------------------------------------------------------------------------------- the box carve


def _carve_box(
    store: SegmentStore,
    profile: MachineProfile,
    stock: StockBox,
    tool: Tool,
    cutting: np.ndarray,
    target_cells: int,
    notes: tuple[str, ...],
) -> SolidField:
    """A top-down Z map in machine coordinates.

    Refused once a table-mounted program moves A, for the same reason `geometry.rapid-into-stock`
    refuses it: a box fixed in machine coordinates has stopped describing stock that turns with the
    part, so every cell of the result would be carved against the wrong solid.
    """
    if profile.kinematics.rotary_mount == "table" and _moves_rotary(store):
        raise SolidError(
            "a box [stock] cannot be carved once the program moves A on a table mount: the blank turns "
            "with the part, so a box fixed in machine coordinates no longer says where the material is. "
            "Describe the blank as a cylinder on the rotary axis, which is unchanged by any rotation"
        )

    low = np.asarray(stock.min, dtype=np.float64)
    high = np.asarray(stock.max, dtype=np.float64)
    span = high - low
    cell = _cell_size(float(max(span[0], span[1])), tool.radius, target_cells)
    pad = _pad_cells(tool.radius, cell)

    nu = int(np.ceil(span[0] / cell)) + 1
    nv = int(np.ceil(span[1] / cell)) + 1
    ceiling = float(high[2])

    grid = np.full((nu + 2 * pad, nv + 2 * pad), ceiling, dtype=np.float64)
    origin = (float(low[0]) - pad * cell, float(low[1]) - pad * cell)

    for chunk in _sample_chunks(store.lin, cutting, cell * CELL_FRACTION):
        iu = np.floor((chunk[:, 0] - origin[0]) / cell + 0.5).astype(np.int64)
        iv = np.floor((chunk[:, 1] - origin[1]) / cell + 0.5).astype(np.int64)
        _scatter_min(grid, iu, iv, chunk[:, 2], wrap_u=False)

    eroded = _erode(grid, tool, (cell, cell), pad, wrap_u=False)
    height = np.maximum(eroded[pad : pad + nu, pad : pad + nv], float(low[2]))
    return SolidField(
        kind="box",
        height=np.ascontiguousarray(height),
        origin=(float(low[0]), float(low[1])),
        cell=(cell, cell),
        floor=float(low[2]),
        ceiling=ceiling,
        notes=notes,
    )


# ----------------------------------------------------------------------------- the cylinder carve


def _carve_cylinder(
    store: SegmentStore,
    profile: MachineProfile,
    stock: StockCylinder,
    tool: Tool,
    cutting: np.ndarray,
    target_cells: int,
    notes: tuple[str, ...],
) -> SolidField:
    """A radial map over (angle, axial) in part coordinates.

    Part coordinates, not machine, and that is the whole design: a concentric cylinder maps onto itself
    under every rotation, so a map indexed by the *part's* angle is stationary while the blank turns. The
    same fact is what lets `geometry.rapid-into-stock` stay exact at every angle for this shape.
    """
    if store.lin_part is None:
        raise KinematicsError(
            "carving cylindrical stock needs part coordinates, which have not been computed for this "
            "program; apply the display transform first"
        )
    axial, radial = axis_columns(profile.kinematics.rotary_axis)
    centerline = tuple(float(value) for value in profile.kinematics.centerline_offset)

    length = stock.length
    cell_v = _cell_size(length, tool.radius, target_cells)
    # Angular cells sized so their arc length at the blank's surface matches the axial cell, which keeps
    # the erosion disc round on the unrolled surface — exactly at the surface, approximately below it.
    nu = max(MIN_ANGULAR_CELLS, int(np.ceil(360.0 / np.degrees(cell_v / stock.radius))))
    cell_u = 360.0 / nu
    pad = _pad_cells(tool.radius, cell_v)

    nv = int(np.ceil(length / cell_v)) + 1
    ceiling = stock.radius

    # Only v is padded. u is an angle and wraps, so it has no ends to run off.
    grid = np.full((nu, nv + 2 * pad), ceiling, dtype=np.float64)
    origin = (0.0, stock.axis_min - pad * cell_v)
    # Sampling density is set by the finer of the two cells, measured at the surface where the angular
    # cell is widest — undersampling near the axis is harmless, undersampling at the rim is a ridge.
    step = min(cell_v, np.radians(cell_u) * stock.radius) * CELL_FRACTION

    for chunk in _sample_chunks(store.lin_part, cutting, step):
        offset_first = chunk[:, radial[0]] - centerline[radial[0]]
        offset_second = chunk[:, radial[1]] - centerline[radial[1]]
        angle = np.degrees(np.arctan2(offset_second, offset_first)) % 360.0
        iu = np.floor(angle / cell_u + 0.5).astype(np.int64) % nu
        iv = np.floor((chunk[:, axial] - origin[1]) / cell_v + 0.5).astype(np.int64)
        _scatter_min(grid, iu, iv, np.hypot(offset_first, offset_second), wrap_u=True)

    eroded = _erode(grid, tool, (np.radians(cell_u) * stock.radius, cell_v), pad, wrap_u=True)
    height = np.maximum(eroded[:, pad : pad + nv], 0.0)
    return SolidField(
        kind="cylinder",
        height=np.ascontiguousarray(height),
        origin=(0.0, stock.axis_min),
        cell=(cell_u, cell_v),
        floor=0.0,
        ceiling=ceiling,
        wraps_u=True,
        axial=axial,
        radial=radial,
        centerline=centerline,
        part_coordinates=True,
        notes=notes,
    )


# ------------------------------------------------------------------------------------- the passes


def _cell_size(extent: float, radius: float, target_cells: int) -> float:
    """The coarser of "part over `target_cells`" and "radius over `MAX_RADIUS_CELLS`".

    Taking the coarser is what bounds the erosion kernel. A 50 mm face mill on a 60 mm part would
    otherwise want a 200-cell radius and a 125,000-offset kernel; here it coarsens the grid instead.
    """
    by_extent = extent / max(target_cells, 1)
    by_tool = radius / MAX_RADIUS_CELLS
    return max(by_extent, by_tool, 1e-6)


def _pad_cells(radius: float, cell: float) -> int:
    """How far outside the stock the axis map must reach.

    A facing pass that runs the cutter off the edge of the blank still removes material *inside* it. Drop
    those axis positions and the erosion has nothing to spread inward from, leaving a rim of uncut
    material that looks like a real feature.
    """
    return int(np.ceil(radius / cell)) + 1


def _moves_rotary(store: SegmentStore) -> bool:
    if len(store) == 0:
        return False
    return bool(np.any(store.rot != store.rot.flat[0]))


def _sample_chunks(source: np.ndarray, cutting: np.ndarray, step: float):
    """Yield ``(M, 3)`` chunks of tool-axis positions along the cutting segments.

    Sampled in **world space** and converted afterwards, never the other way round: a straight line in
    XYZ is not straight in (angle, axial), and interpolating in the grid's own frame would cut a curve
    where the tool went straight.

    Interpolating linearly between segment endpoints is the same thing the viewport draws, and it is
    accurate for the same reason — `sim.interpolate` already tessellated arcs and rotary moves to within
    the profile's chord tolerances, so a segment is a chord that is close enough by construction.
    """
    selected = np.flatnonzero(cutting)
    if selected.size == 0:
        return
    starts = source[selected, 0, :]
    ends = source[selected, 1, :]
    delta = ends - starts
    intervals = np.maximum(1, np.ceil(np.linalg.norm(delta, axis=1) / step)).astype(np.int64)
    per_segment = intervals + 1  # both endpoints, so consecutive segments overlap by one point

    # Chunk on a cumulative sample count rather than a fixed segment count: one G1 across a large part
    # can be worth more samples than ten thousand short ones, so a segment-count chunk bounds nothing.
    edges = _chunk_edges(per_segment, CHUNK_SAMPLES)
    for begin, end in zip(edges[:-1], edges[1:], strict=True):
        counts = per_segment[begin:end]
        index = np.repeat(np.arange(begin, end), counts)
        offsets = np.concatenate(([0], np.cumsum(counts)[:-1]))
        local = np.arange(int(counts.sum())) - np.repeat(offsets, counts)
        fraction = local / np.repeat(intervals[begin:end], counts)
        yield starts[index] + delta[index] * fraction[:, None]


def _chunk_edges(per_segment: np.ndarray, budget: int) -> list[int]:
    """Segment indices splitting ``per_segment`` into runs of at most ``budget`` samples."""
    total = np.cumsum(per_segment)
    edges = [0]
    while edges[-1] < per_segment.size:
        consumed = 0 if edges[-1] == 0 else int(total[edges[-1] - 1])
        # `searchsorted` finds where the running total crosses the budget; +1 guarantees progress even
        # when a single segment is worth more than a whole chunk on its own.
        nxt = int(np.searchsorted(total, consumed + budget, side="right"))
        edges.append(min(max(nxt, edges[-1] + 1), per_segment.size))
    return edges


def _scatter_min(
    grid: np.ndarray, iu: np.ndarray, iv: np.ndarray, values: np.ndarray, *, wrap_u: bool
) -> None:
    """Lower each addressed cell to ``values``, dropping anything outside the grid.

    `np.minimum.at` rather than fancy-index assignment: several samples land in the same cell on any
    real path, and plain assignment would keep whichever happened to be written last instead of the
    deepest — a difference that shows up as noise along every move that is not axis-aligned.
    """
    nu, nv = grid.shape
    inside = (iv >= 0) & (iv < nv)
    if not wrap_u:
        inside &= (iu >= 0) & (iu < nu)
    if not inside.all():
        iu, iv, values = iu[inside], iv[inside], values[inside]
    np.minimum.at(grid, (iu, iv), values)


def _erode(
    grid: np.ndarray, tool: Tool, cell_world: tuple[float, float], pad: int, *, wrap_u: bool
) -> np.ndarray:
    """Grayscale-erode the tool-axis map by the cutter's bottom.

    ``result[i, j] = min over disc offsets (du, dv) of grid[i+du, j+dv] + bottom_offset(distance)``.

    The offsets are a disc, so it is symmetric and the sign of the shift does not matter. Padding is
    ``+inf`` rather than the uncut ceiling so that a cell outside the array can never *raise* a result —
    the wrap case aside, where the grid genuinely continues.
    """
    nu, nv = grid.shape
    du_max = int(np.ceil(tool.radius / cell_world[0]))
    dv_max = int(np.ceil(tool.radius / cell_world[1]))
    if wrap_u:
        # A cutter wider than the blank's circumference would ask for a wrap deeper than the array is
        # long, which numpy would serve as a short slice rather than as an error. Half a turn reaches
        # every cell already, so clamping there loses nothing and cannot silently mis-index.
        du_max = min(du_max, nu // 2)
    if du_max == 0 and dv_max == 0:
        return grid.copy()

    padded = np.full((nu + 2 * du_max, nv + 2 * dv_max), np.inf, dtype=np.float64)
    padded[du_max : du_max + nu, dv_max : dv_max + nv] = grid
    if wrap_u:
        # The angular axis has no ends: cell 0 is adjacent to cell nu-1, and leaving +inf across the seam
        # would leave an uncut stripe there exactly one tool radius wide.
        padded[:du_max, dv_max : dv_max + nv] = grid[nu - du_max :, :]
        padded[du_max + nu :, dv_max : dv_max + nv] = grid[:du_max, :]

    out = np.full_like(grid, np.inf)
    for du, dv, lift in _disc_offsets(tool, cell_world, du_max, dv_max):
        window = padded[du_max + du : du_max + du + nu, dv_max + dv : dv_max + dv + nv]
        np.minimum(out, window + lift, out=out)
    # `pad` guarantees every in-stock cell had a full disc of real neighbours, so an infinity can only
    # survive in the padding ring the caller crops away. Falling back to `grid` keeps the array finite
    # regardless, since an inf reaching the mesh would render as nothing at all.
    return np.where(np.isfinite(out), out, grid)


def _disc_offsets(
    tool: Tool, cell_world: tuple[float, float], du_max: int, dv_max: int
) -> list[tuple[int, int, float]]:
    """Every ``(du, dv, lift)`` in the cutter's footprint, ``lift`` from `bottom_offset`."""
    du = np.arange(-du_max, du_max + 1)
    dv = np.arange(-dv_max, dv_max + 1)
    world_u = du[:, None] * cell_world[0]
    world_v = dv[None, :] * cell_world[1]
    distance = np.hypot(world_u, world_v)
    inside = distance <= tool.radius
    lift = bottom_offset(distance, tool)
    rows, columns = np.nonzero(inside)
    return [
        (int(du[row]), int(dv[column]), float(lift[row, column]))
        for row, column in zip(rows, columns, strict=True)
    ]
