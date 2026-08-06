"""``SegmentStore`` → a handful of GL-ready vertex batches. **No Qt, deliberately.**

PLAN.md § Performance Requirements: *"pre-batch into ≤ 10 buffers grouped by kind; never one draw
call per move."* This module is that pre-batching, and it is kept free of Qt so the part that can be
wrong — which segments end up in which batch — is unit-testable without a display or a GL context.
`viewport3d.py` is then thin enough to be checked by eye.

Three things here are load-bearing:

- **Every segment lands in exactly one batch.** A batching bug that drops a group silently removes
  toolpath from the screen, which is the failure mode PLAN.md cares most about. `test_batching.py`
  asserts the partition directly rather than trusting the grouping logic.
- **Untrusted geometry never shares a batch with trusted geometry.** A cutter-compensated span is
  drawn as the programmed centreline, which is *not where the tool goes*. Batching it in with ordinary
  feeds would present it as understood — "never render a confidently wrong toolpath" is a rendering
  requirement, and this is where it is enforced. One batch per (kind, trust) pair, so it cannot be
  lost by a later styling change.
- **Colour carries the distinction, never line width.** T0.7 confirmed pyqtgraph skips the
  `glLineWidth` call entirely on core forward-compatible profiles, so `width=` is silently inert
  there. Anything encoded in thickness would vanish on those drivers with no error.

Four batches is the natural result, well inside the budget of ten. Subdividing further would only add
draw calls; the spike used ten to prove the constraint was survivable, not because ten is desirable.
"""

from dataclasses import dataclass

import numpy as np

from foursight.sim.segments import Kind, SegmentStore

# PLAN.md § Performance Requirements. In practice "≤ 10 vertex buffers" means "≤ 10
# `GLLinePlotItem`s", since each item owns its own VBOs.
MAX_BATCHES = 10

Color = tuple[float, float, float, float]

# Rapids red, feeds green, per PLAN.md. Untrusted spans get their own hue rather than a lower alpha:
# translucency would depend on depth-sort order and read as a rendering artefact on a dense path,
# whereas amber reads as caution at any density.
TRUSTED_COLORS: dict[Kind, Color] = {
    Kind.RAPID: (0.90, 0.25, 0.20, 1.0),
    Kind.FEED: (0.20, 0.85, 0.35, 1.0),
}
UNTRUSTED_COLORS: dict[Kind, Color] = {
    Kind.RAPID: (0.85, 0.45, 0.10, 1.0),
    Kind.FEED: (1.00, 0.75, 0.15, 1.0),
}

# GL wants float32. `SegmentStore.vertices` is deliberately float64 — converting there would double
# the resident geometry cost — so the conversion happens here, once per batch, at upload time.
GL_DTYPE = np.float32


@dataclass(frozen=True, slots=True)
class Batch:
    """One draw call's worth of geometry, ready for ``GLLinePlotItem(mode='lines')``."""

    label: str
    vertices: np.ndarray  # (2M, 3) float32 — consecutive pairs form one line segment
    color: Color
    kind: Kind
    trusted: bool

    @property
    def segments(self) -> int:
        return self.vertices.shape[0] // 2

    @property
    def nbytes(self) -> int:
        """What this batch costs in GL-bound memory, for the T2.11 budget."""
        return int(self.vertices.nbytes)


def build_batches(
    store: SegmentStore,
    *,
    untrusted: np.ndarray | None = None,
    use_part_coordinates: bool = False,
) -> list[Batch]:
    """Group ``store`` into one batch per (kind, trust) pair.

    ``untrusted`` is the per-segment boolean mask from ``Simulation.unverified_mask()`` — spans that
    were drawn but must not be presented as the truth. Passing ``None`` treats everything as trusted,
    which is correct only for a program with no such spans.

    ``use_part_coordinates`` draws ``lin_part`` instead of ``lin``, for a table-mounted rotary where
    the part turns under the tool. ``lin`` stays machine coordinates always — that is what makes
    travel-limit verification possible — so the display transform is a separate array, never a
    mutation of the one the verifier reads.
    """
    count = len(store)
    if count == 0:
        return []

    source = _display_array(store, use_part_coordinates)
    mask = _validated_mask(untrusted, count)

    batches: list[Batch] = []
    for trusted in (True, False):
        trust_group = ~mask if trusted else mask
        if not trust_group.any():
            continue
        palette = TRUSTED_COLORS if trusted else UNTRUSTED_COLORS
        for kind, color in palette.items():
            selected = trust_group & (store.kind == kind)
            if not selected.any():
                continue
            batches.append(
                Batch(
                    label=f"{kind.name.lower()}{'' if trusted else ' (unverified)'}",
                    vertices=_vertices_for(source, selected),
                    color=color,
                    kind=kind,
                    trusted=trusted,
                )
            )

    if len(batches) > MAX_BATCHES:  # pragma: no cover - four styles cannot exceed ten
        raise AssertionError(f"{len(batches)} batches exceeds the {MAX_BATCHES} PLAN.md allows")
    return batches


def _display_array(store: SegmentStore, use_part_coordinates: bool) -> np.ndarray:
    """The ``(N, 2, 3)`` array to draw, machine or part."""
    if not use_part_coordinates:
        return store.lin
    if store.lin_part is None:
        raise ValueError(
            "part coordinates were requested but no display transform has been applied; "
            "call SegmentStore.set_part_coordinates() first"
        )
    return store.lin_part


def _validated_mask(untrusted: np.ndarray | None, count: int) -> np.ndarray:
    """A length-``count`` boolean mask, refusing a wrong-length one rather than broadcasting it.

    A mask of the wrong length would silently mark arbitrary segments as untrusted — or, worse,
    numpy-broadcast to mark none of them — so the shape is checked instead of assumed.
    """
    if untrusted is None:
        return np.zeros(count, dtype=bool)
    if untrusted.shape != (count,):
        raise ValueError(
            f"untrusted mask has shape {untrusted.shape}, expected ({count},) — one entry per segment"
        )
    return untrusted.astype(bool, copy=False)


def _vertices_for(source: np.ndarray, selected: np.ndarray) -> np.ndarray:
    """``(2M, 3)`` float32 vertex pairs for the selected segments.

    Selecting a subset necessarily copies — the zero-copy `(N, 2, 3) -> (2N, 3)` reshape only holds
    for the whole store — and the float32 conversion would copy regardless, so the two are done in
    one step. `reshape(-1, 3)` after a fancy-index is free, since the result is already contiguous.
    """
    return source[selected].astype(GL_DTYPE, copy=False).reshape(-1, 3)


def total_gl_bytes(batches: list[Batch]) -> int:
    """GL-bound bytes across all batches, for the memory budget in PLAN.md."""
    return sum(batch.nbytes for batch in batches)


def bounds(store: SegmentStore, *, use_part_coordinates: bool = False) -> np.ndarray | None:
    """``(2, 3)`` min/max of the drawn geometry, or None when there is none.

    Linear axes only. The rotary column is *not* included: a bounding box spanning millimetres and
    degrees would be meaningless, and this one is used to place a camera.
    """
    if len(store) == 0:
        return None
    source = _display_array(store, use_part_coordinates).reshape(-1, 3)
    return np.stack([source.min(axis=0), source.max(axis=0)])
