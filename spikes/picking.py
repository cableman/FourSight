"""T3.0 spike — how do we turn a click into a segment at 500k segments? (resolves D4)

D4 names two candidates: **GPU colour-picking to an offscreen target** and a **CPU KD-tree over
segment midpoints**. This spike measures a third that the framing misses: **CPU screen-space distance,
fully vectorized in numpy**.

The three, and what each actually costs:

1. **GPU colour-pick.** Render every segment in a unique colour to an offscreen buffer, read the pixel
   under the cursor. Exact and occlusion-correct. But `GLLinePlotItem` takes either one colour per item
   or one per *vertex*, and our batches deliberately use the former — a per-vertex pick-colour buffer is
   4 MB at 500k as uint8 and 16 MB as float32, on top of the 12 MB of positions, purely so clicking
   works. Plus one extra full render pass and a framebuffer readback per click.
2. **CPU KD-tree over midpoints.** Cheap to query, but *midpoints are the wrong feature*: a 100 mm
   rapid's midpoint is 50 mm from where the user clicked on its end. Measured below.
3. **CPU screen-space distance to the segment itself.** Project all 2N vertices with the same
   projection × view matrices the renderer used, then take the minimum 2D point-to-*segment* distance.
   No new dependency, no extra GPU memory, and it answers what the user is actually asking: "which line
   is nearest my cursor on screen?"

Run:  DISPLAY=:0 .venv/bin/python spikes/picking.py
      .venv/bin/python spikes/picking.py --no-gl     # synthetic camera; timings still valid
"""

import argparse
import sys
import time

import numpy as np

sys.path.insert(0, "src")

from foursight.sim.segments import Kind, SegmentBuilder  # noqa: E402

PICK_RADIUS_PX = 6.0


def build_store(segments: int):
    """A store of roughly `segments` segments, as a few long helical polylines."""
    builder = SegmentBuilder()
    per_line = 2000
    lines = max(1, segments // per_line)
    for line_no in range(lines):
        t = np.linspace(0, 4 * np.pi, per_line + 1)
        points = np.stack(
            [50 + 40 * np.cos(t), 50 + 40 * np.sin(t), line_no * 0.05 + t * 0.5], axis=1
        )
        builder.add_polyline(points, Kind.FEED, line_no + 1)
    return builder.finalize()


def matrices(width: int, height: int, use_gl: bool) -> np.ndarray:
    """``projection @ view`` as a 4x4, from a real GLViewWidget when one is available."""
    if not use_gl:
        f = 1.0 / np.tan(np.radians(30))
        aspect = width / height
        near, far = 1.0, 1000.0
        proj = np.array(
            [
                [f / aspect, 0, 0, 0],
                [0, f, 0, 0],
                [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                [0, 0, -1, 0],
            ]
        )
        view = np.eye(4)
        view[:3, 3] = [-50, -50, -300]
        return proj @ view

    from pyqtgraph.opengl import GLViewWidget
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    widget = GLViewWidget()
    widget.resize(width, height)
    widget.setCameraPosition(distance=250, elevation=30, azimuth=-60)
    # pyqtgraph 0.14 takes (region, viewport) — both (x, y, w, h) — rather than the no-arg form older
    # versions had. Worth pinning in a comment: T0.7 already found two PLAN assumptions written against
    # an older pyqtgraph, and this is a third API that moved.
    viewport = (0, 0, width, height)
    proj = np.array(widget.projectionMatrix(viewport, viewport).data(), dtype=np.float64)
    view = np.array(widget.viewMatrix().data(), dtype=np.float64)
    # QMatrix4x4.data() is column-major, so reshape then transpose to get row-major maths.
    return proj.reshape(4, 4).T @ view.reshape(4, 4).T


def project(points: np.ndarray, mvp: np.ndarray, width: int, height: int):
    """(M, 3) world -> (M, 2) pixels, plus clip w and NDC depth so callers can discard what is behind."""
    homogeneous = np.empty((points.shape[0], 4), dtype=np.float64)
    homogeneous[:, :3] = points
    homogeneous[:, 3] = 1.0
    clip = homogeneous @ mvp.T
    w = clip[:, 3]
    safe = np.where(np.abs(w) < 1e-12, 1e-12, w)
    ndc = clip[:, :3] / safe[:, None]
    screen = np.empty((points.shape[0], 2))
    screen[:, 0] = (ndc[:, 0] * 0.5 + 0.5) * width
    screen[:, 1] = (1.0 - (ndc[:, 1] * 0.5 + 0.5)) * height
    return screen, w, ndc[:, 2]


def pick_screen_space(store, mvp, width, height, cursor):
    """Nearest segment to `cursor`, projecting from scratch. The cold path."""
    return pick_projected(project(store.lin.reshape(-1, 3), mvp, width, height), cursor)


def pick_projected(projected, cursor):
    """The warm path: screen coordinates already cached from the last camera change.

    This is the real per-click cost. The camera does not move between the user stopping an orbit and
    clicking, so the projection is computed once per camera settle rather than once per click.
    """
    screen, w, depth = projected
    a, b = screen[0::2], screen[1::2]
    behind = (w[0::2] <= 0) & (w[1::2] <= 0)

    ab = b - a
    ap = cursor - a
    denom = np.einsum("ij,ij->i", ab, ab)
    usable = denom > 0
    t = np.where(usable, np.einsum("ij,ij->i", ap, ab) / np.where(usable, denom, 1.0), 0.0)
    np.clip(t, 0.0, 1.0, out=t)
    distance = np.linalg.norm(a + t[:, None] * ab - cursor, axis=1)
    distance[behind] = np.inf

    within = np.flatnonzero(distance <= PICK_RADIUS_PX)
    if within.size == 0:
        index = int(np.argmin(distance))
        return (index, float(distance[index])) if np.isfinite(distance[index]) else None
    # Front-most among the candidates: occlusion handling without a GPU pass.
    nearest = within[np.argmin((depth[0::2] + depth[1::2])[within])]
    return int(nearest), float(distance[nearest])


def pick_midpoint(store, mvp, width, height, cursor):
    """What a KD-tree over midpoints returns: the nearest *midpoint*, not the nearest segment."""
    screen, w, _ = project(store.lin.mean(axis=1), mvp, width, height)
    distance = np.linalg.norm(screen - cursor, axis=1)
    distance[w <= 0] = np.inf
    index = int(np.argmin(distance))
    return index, float(distance[index])


def time_pick(fn, *args, repeats: int = 5) -> float:
    fn(*args)
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        fn(*args)
        best = min(best, time.perf_counter() - started)
    return best * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description="T3.0 picking spike")
    parser.add_argument(
        "--no-gl", action="store_true", help="synthetic camera; timings still valid"
    )
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    args = parser.parse_args()

    width, height = args.width, args.height
    mvp = matrices(width, height, use_gl=not args.no_gl)
    source = "a synthetic camera" if args.no_gl else "a real GLViewWidget"
    print(
        f"viewport {width}x{height}, pick radius {PICK_RADIUS_PX:.0f} px, matrices from {source}\n"
    )

    print("=== Cost per click: screen-space distance to the segment (candidate 3) ===")
    print(
        "  cold = project every vertex then measure; warm = projection cached from the last camera"
    )
    print(
        "  change, which is the real per-click cost since the camera does not move while clicking.\n"
    )
    cursor = np.array([width / 2, height / 2])
    for count in (10_000, 100_000, 500_000, 1_000_000):
        store = build_store(count)
        cold = time_pick(pick_screen_space, store, mvp, width, height, cursor)
        projected = project(store.lin.reshape(-1, 3), mvp, width, height)
        warm = time_pick(pick_projected, projected, cursor)
        verdict = "interactive" if warm < 50 else "sluggish"
        print(
            f"  {len(store):>9,} segments: cold {cold:7.2f} ms   warm {warm:6.2f} ms   ({verdict})"
        )

    print("\n=== Accuracy: midpoints vs the segment itself (candidate 2 vs 3) ===")
    builder = SegmentBuilder()
    builder.add_polyline(np.array([[0.0, 0, 0], [100.0, 0, 0]]), Kind.RAPID, 10)
    builder.add_polyline(np.array([[95.0, 8, 0], [99.0, 8, 0]]), Kind.FEED, 20)
    store = builder.finalize()
    screen, _, _ = project(store.lin.reshape(-1, 3), mvp, width, height)
    cursor = screen[1] + np.array([0.0, 1.0])  # one pixel from the long segment's far end

    seg_index, seg_distance = pick_screen_space(store, mvp, width, height, cursor)
    mid_index, mid_distance = pick_midpoint(store, mvp, width, height, cursor)
    print("  clicked 1 px from the end of a long segment (source line 10):")
    print(f"    distance to segment -> line {store.line[seg_index]} at {seg_distance:.1f} px")
    print(f"    midpoint only       -> line {store.line[mid_index]} at {mid_distance:.1f} px")
    print(
        "    "
        + (
            "MIDPOINTS PICK THE WRONG LINE"
            if store.line[mid_index] != 10
            else "both agree (widen the case)"
        )
    )

    print("\n=== Memory a GPU colour-pick would add (candidate 1) ===")
    for count in (100_000, 500_000):
        vertices = count * 2
        print(
            f"  {count:>7,} segments: pick-colour buffer {vertices * 4 / 1e6:5.1f} MB as uint8, "
            f"{vertices * 4 * 4 / 1e6:5.1f} MB as float32   "
            f"(positions are {vertices * 3 * 4 / 1e6:.1f} MB)"
        )
    print("  ...plus one extra full render pass and a framebuffer readback per click.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
