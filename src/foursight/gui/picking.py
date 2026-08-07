"""Click → segment. **No Qt.** The strategy and its cost were decided by measurement in T3.0.

D4 is resolved in favour of **CPU screen-space distance to the segment**, not the GPU colour-pick or the
midpoint KD-tree it originally proposed. PLAN.md § Picking Strategy has the full reasoning; the short
version is that midpoints answer the wrong question (clicking 1 px from a long rapid's end returns a
segment 28.7 px away) and colour-picking costs a per-vertex colour buffer plus a render pass per click,
while fighting the one-colour-per-batch design that keeps GL memory at 12 MB.

**The projection is cached, and the cache is keyed on the matrix itself.** That is the load-bearing
decision here. A projection left stale after an orbit returns the *wrong segment* while the picture gives
no hint anything is wrong — there is nothing to notice. Rather than trying to hook every camera mutation
(`setCameraPosition`, mouse drag, wheel, a resize, a programmatic `opts` poke), `ScreenProjection` records
the exact `mvp` and viewport it was built from and `matches()` compares them. If the matrix is unchanged
the projection is valid *by definition*; if anything moved the cache misses and reprojects. Staleness
becomes impossible rather than merely unlikely.

Measured on the baseline machine: 27.3 ms per click at 500k segments with the cache warm, 93.9 ms cold.
"""

from dataclasses import dataclass

import numpy as np

from foursight.sim.segments import SegmentStore

#: How near the cursor has to be, in pixels. Generous enough to hit a 1-pixel line without a steady hand.
PICK_RADIUS_PX = 6.0


@dataclass(frozen=True, slots=True)
class ScreenProjection:
    """Every segment endpoint in screen space, plus what it was projected with.

    ``behind`` marks segments entirely behind the camera, which must never be pickable: their projected
    coordinates are a meaningless reflection through the origin and would otherwise produce confident
    hits on geometry that is not on screen at all.
    """

    a: np.ndarray  # (N, 2) first endpoint, pixels
    b: np.ndarray  # (N, 2) second endpoint, pixels
    depth: np.ndarray  # (N,) mean NDC depth, for breaking ties front-to-back
    behind: np.ndarray  # (N,) bool
    mvp: np.ndarray  # the 4x4 this was built from
    width: int
    height: int
    segments: int

    def matches(self, mvp: np.ndarray, width: int, height: int, segments: int) -> bool:
        """Whether this projection is still valid for the given camera, viewport and store size.

        The store size is part of the key because a *different program with the same camera* must miss:
        the arrays are indexed by segment and would otherwise be silently the wrong length.
        """
        return (
            self.width == width
            and self.height == height
            and self.segments == segments
            and np.array_equal(self.mvp, mvp)
        )


def project_store(
    store: SegmentStore, mvp: np.ndarray, width: int, height: int
) -> ScreenProjection:
    """Project every segment endpoint to pixels. The expensive half — 47.7 ms at 500k segments."""
    points = store.lin.reshape(-1, 3)
    homogeneous = np.empty((points.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = points
    homogeneous[:, 3] = 1.0
    clip = homogeneous @ mvp.T

    w = clip[:, 3]
    # Guard the division rather than letting it produce inf/nan: a vertex exactly on the camera plane is
    # rare but a nan would propagate into every distance comparison and make `argmin` meaningless.
    safe = np.where(np.abs(w) < 1e-12, 1e-12, w)
    ndc = clip[:, :3] / safe[:, None]

    screen = np.empty((points.shape[0], 2))
    screen[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * width
    screen[:, 1] = (
        1.0 - (ndc[:, 1] * 0.5 + 0.5)
    ) * height  # y grows downward in widget coordinates

    return ScreenProjection(
        a=screen[0::2],
        b=screen[1::2],
        depth=(ndc[0::2, 2] + ndc[1::2, 2]) * 0.5,
        behind=(w[0::2] <= 0) & (w[1::2] <= 0),
        mvp=mvp.copy(),  # copied: the caller's matrix is rebuilt in place on every camera change
        width=width,
        height=height,
        segments=len(store),
    )


def pick(
    projection: ScreenProjection, cursor: tuple[float, float], radius: float = PICK_RADIUS_PX
) -> int | None:
    """The segment nearest ``cursor``, or None if nothing is within ``radius``. The cheap half.

    Distance is to the **segment**, not to its midpoint — the clamp on ``t`` is what makes clicking the
    far end of a long rapid select that rapid rather than whatever happens to sit near its middle.

    Among candidates inside the radius, the **front-most** wins. Depth is used only to break ties, never
    to exclude: in a wireframe view there are no surfaces, so wanting the line behind another line is
    ordinary, and strict occlusion would make it unreachable.
    """
    if projection.segments == 0:
        return None
    point = np.asarray(cursor, dtype=np.float64)

    ab = projection.b - projection.a
    ap = point - projection.a
    length_squared = np.einsum("ij,ij->i", ab, ab)
    usable = length_squared > 0
    # A zero-length segment (a dwell, a stationary block) clamps to t = 0, i.e. distance to its point.
    t = np.where(usable, np.einsum("ij,ij->i", ap, ab) / np.where(usable, length_squared, 1.0), 0.0)
    np.clip(t, 0.0, 1.0, out=t)

    distance = np.linalg.norm(projection.a + t[:, None] * ab - point, axis=1)
    distance[projection.behind] = np.inf

    within = np.flatnonzero(distance <= radius)
    if within.size == 0:
        return None
    return int(within[np.argmin(projection.depth[within])])
