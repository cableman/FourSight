"""SPIKE (T0.7): can pyqtgraph render 500k segments interactively?

Resolves decisions D1 and D3 in TASKS.md. Answers four questions with measurements rather than
impressions, and prints a Markdown block to paste into PLAN.md § Rendering Constraints:

1. What orbit framerate do 500k segments sustain in <= 10 ``GLLinePlotItem``s?
2. Does ``GLLinePlotItem`` accept a dash/stipple parameter?          (D3)
3. Does pyqtgraph's line item use the legacy fixed-function path?
4. What line widths does this driver actually support?

Not shipped: this lives outside ``src/`` on purpose and is never imported by the package.

Usage::

    .venv/bin/python spikes/render_500k.py                    # the real measurement
    .venv/bin/python spikes/render_500k.py --introspect-only  # no window; questions 2-3 only
    .venv/bin/python spikes/render_500k.py --segments 1000000 --batches 10

Needs a display. Under Wayland/X11 just run it; over SSH you need X forwarding. Do NOT run it
under ``xvfb`` and record the result as the answer — llvmpipe software rendering measures the CPU
rasteriser, not the integrated GPU the target is stated against.
"""

import argparse
import inspect
import json
import statistics
import sys
import time

import numpy as np

# Imported lazily inside main() so --introspect-only still works if a display is missing, and so
# an import failure is reported as a finding rather than a traceback.
WARMUP_FRAMES = 20


def synth_segments(count: int) -> tuple[np.ndarray, np.ndarray]:
    """Build a spatially spread toolpath in the real SegmentStore layout.

    Shape matters for the measurement: rasterisation cost depends on how much screen the geometry
    covers, so a degenerate straight line would flatter the result. Columnar ``(N, 2, 3)`` float64
    mirrors ``sim/segments.py`` so the reshape below is the one the real buffer builder does.
    """
    t = np.linspace(0.0, 240.0 * np.pi, count + 1)
    radius = 60.0 + 30.0 * np.sin(t / 37.0)
    points = np.empty((count + 1, 3), dtype=np.float64)
    points[:, 0] = radius * np.cos(t)
    points[:, 1] = radius * np.sin(t)
    points[:, 2] = np.linspace(0.0, 120.0, count + 1)

    lin = np.empty((count, 2, 3), dtype=np.float64)
    lin[:, 0, :] = points[:-1]
    lin[:, 1, :] = points[1:]

    kind = np.zeros(count, dtype=np.uint8)
    kind[::13] = 1  # ~8% rapids, roughly what a real program carries
    return lin, kind


def batch_slices(kind: np.ndarray, batches: int) -> list[tuple[int, np.ndarray]]:
    """Split segments into <= `batches` groups, partitioned by kind first.

    Grouping by kind is what the renderer must do anyway (rapids and feeds differ in colour), so
    the batch count is only ever subdivided *within* a kind. Returns (kind_value, indices) pairs.
    """
    groups: list[tuple[int, np.ndarray]] = []
    per_kind = max(1, batches // 2)
    for kind_value in (0, 1):
        indices = np.flatnonzero(kind == kind_value)
        if indices.size == 0:
            continue
        groups.extend((kind_value, chunk) for chunk in np.array_split(indices, per_kind))
    return groups


def resident_mb() -> float | None:
    """Resident memory, or None where it cannot be read (notably Windows)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def introspect_pyqtgraph() -> dict[str, object]:
    """Answer questions 2 and 3 without needing a display or a GL context.

    Worth running on every pyqtgraph upgrade: these answers are version-specific, and PLAN.md's
    original Rendering Constraints were written against an older pyqtgraph than 0.14.
    """
    import pyqtgraph
    import pyqtgraph.opengl as gl

    findings: dict[str, object] = {"pyqtgraph_version": pyqtgraph.__version__}

    # Question 2 (D3): probe empirically rather than trusting the docs. setData validates keyword
    # names, so an unsupported one raises — and the message enumerates what *is* allowed.
    item = gl.GLLinePlotItem()
    for keyword in ("dash", "stipple", "dashPattern"):
        try:
            item.setData(**{keyword: [4, 4]})
        # Bare `Exception` is intentional: pyqtgraph raises plain Exception here, and any
        # rejection at all is the finding we are after.
        except Exception as exc:
            findings[f"accepts_{keyword}"] = "no"
            findings.setdefault("setdata_rejection", f"{type(exc).__name__}: {exc}")
        else:
            findings[f"accepts_{keyword}"] = "YES"

    # Question 3: fixed-function immediate mode, or shaders with persistent VBOs? This decides how
    # much of the rendering work we would have to take over ourselves if D1 goes against us.
    try:
        paint_source = inspect.getsource(gl.GLLinePlotItem.paint)
    except OSError:
        findings["draw_path"] = "unknown (source unavailable)"
    else:
        legacy = [
            marker
            for marker in ("glVertexPointer", "glEnableClientState", "glBegin")
            if marker in paint_source
        ]
        modern = [
            marker
            for marker in ("glDrawArrays", "glVertexAttribPointer", "getShaderProgram")
            if marker in paint_source
        ]
        if legacy:
            findings["draw_path"] = f"legacy fixed-function ({', '.join(legacy)})"
        else:
            findings["draw_path"] = f"shaders ({', '.join(modern)})"
        # Re-uploading vertex data every frame would dominate the frame time at 500k segments;
        # a dirty-flag guard means uploads happen only when the geometry actually changes.
        findings["uploads_only_when_dirty"] = "dirty_bits" in paint_source
        findings["applies_gl_line_width"] = "glLineWidth" in paint_source
        findings["skips_line_width_on_core_profile"] = "core_forward_compatible" in paint_source

    findings["persistent_vbo_attrs"] = (
        sorted(name for name in vars(item) if "vbo" in name.lower()) or "none found"
    )
    return findings


def build_view(args: argparse.Namespace, lin: np.ndarray, kind: np.ndarray):
    """Create the GL widget and upload the geometry as <= args.batches line items."""
    import pyqtgraph.opengl as gl

    view = _bench_view_class()(target_frames=args.frames, orbit_step=args.orbit_step)
    view.setWindowTitle("FourSight T0.7 render spike")
    view.resize(args.width, args.height)
    view.setCameraPosition(distance=320)

    colours = {0: (0.2, 0.9, 0.3, 1.0), 1: (0.9, 0.2, 0.2, 1.0)}  # feed green, rapid red
    for kind_value, indices in batch_slices(kind, args.batches):
        # float32: GL wants it, and uploading float64 would silently convert every time.
        vertices = lin[indices].reshape(-1, 3).astype(np.float32)
        item = gl.GLLinePlotItem(
            pos=vertices,
            color=colours[kind_value],
            width=args.line_width,
            mode="lines",
            antialias=False,
        )
        view.addItem(item)
    return view


def _bench_view_class():
    """Define the timing widget lazily, since it subclasses an import-time-optional base."""
    import pyqtgraph.opengl as gl
    from OpenGL import GL
    from PySide6 import QtCore

    class BenchView(gl.GLViewWidget):
        def __init__(self, target_frames: int, orbit_step: float) -> None:
            super().__init__()
            self.target_frames = target_frames
            self.orbit_step = orbit_step
            self.frame_deltas: list[float] = []
            self.gl_info: dict[str, object] = {}
            self._last: float | None = None
            self._finished = False

        def paintGL(self, *args: object, **kwargs: object) -> None:  # noqa: N802 - Qt override
            super().paintGL(*args, **kwargs)
            if not self.gl_info:
                self.gl_info = self._read_gl_info()
            # Without glFinish the timing measures command submission, not rendering.
            GL.glFinish()
            now = time.perf_counter()
            if self._last is not None:
                self.frame_deltas.append(now - self._last)
            self._last = now
            if len(self.frame_deltas) >= self.target_frames + WARMUP_FRAMES:
                if not self._finished:
                    self._finished = True
                    QtCore.QTimer.singleShot(0, QtCore.QCoreApplication.quit)
                return
            self.orbit(self.orbit_step, 0)
            self.update()

        def _read_gl_info(self) -> dict[str, object]:
            def text(name: int) -> str:
                value = GL.glGetString(name)
                return value.decode("utf-8", "replace") if value else "?"

            aliased = GL.glGetFloatv(GL.GL_ALIASED_LINE_WIDTH_RANGE)
            smooth = GL.glGetFloatv(GL.GL_SMOOTH_LINE_WIDTH_RANGE)
            return {
                "renderer": text(GL.GL_RENDERER),
                "vendor": text(GL.GL_VENDOR),
                "gl_version": text(GL.GL_VERSION),
                "aliased_line_width_range": [float(aliased[0]), float(aliased[1])],
                "smooth_line_width_range": [float(smooth[0]), float(smooth[1])],
            }

    return BenchView


def summarise(deltas: list[float]) -> dict[str, float]:
    """Framerate stats. The worst frames matter more than the mean for "feels interactive"."""
    usable = deltas[WARMUP_FRAMES:] or deltas
    ordered = sorted(usable)
    p95_delta = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    return {
        "frames_measured": float(len(usable)),
        "fps_mean": 1.0 / statistics.fmean(usable),
        "fps_median": 1.0 / statistics.median(usable),
        "fps_p5_worst": 1.0 / p95_delta,
        "frame_ms_mean": 1000.0 * statistics.fmean(usable),
        "frame_ms_worst": 1000.0 * max(usable),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="T0.7 render spike: 500k segments in <=10 items.")
    parser.add_argument("--segments", type=int, default=500_000, help="segment count (PLAN: 500k)")
    parser.add_argument("--batches", type=int, default=10, help="max GLLinePlotItems (PLAN: <=10)")
    parser.add_argument("--frames", type=int, default=300, help="frames to time after warmup")
    parser.add_argument(
        "--orbit-step", type=float, default=0.5, help="degrees of azimuth per frame"
    )
    parser.add_argument("--line-width", type=float, default=1.0, help="GL line width to request")
    parser.add_argument("--width", type=int, default=1280, help="window width")
    parser.add_argument("--height", type=int, default=800, help="window height")
    parser.add_argument("--introspect-only", action="store_true", help="skip the window and timing")
    parser.add_argument("--json", action="store_true", help="emit raw JSON as well")
    return parser.parse_args(argv)


def report(args: argparse.Namespace, findings: dict[str, object]) -> None:
    """Print a block shaped for pasting into PLAN.md, then the verdict against the D1 threshold."""
    fps = findings.get("fps_median")
    measured = findings.get("items_uploaded", "n/a — introspection only")
    print("\n" + "=" * 78)
    print("### T0.7 render spike result\n")
    print(f"- pyqtgraph {findings.get('pyqtgraph_version')}")
    if fps is not None:
        print(f"- Segments: {args.segments:,} in {measured} GLLinePlotItems")
        print(f"- Vertices uploaded: {2 * args.segments:,} float32 (converted from float64)")
        print(f"- Window: {args.width}x{args.height}, line width requested {args.line_width}")
        for key in ("renderer", "vendor", "gl_version"):
            print(f"- {key}: {findings.get(key, 'n/a')}")
        print(
            f"- Orbit framerate: **{fps:.1f} fps median**, {findings['fps_mean']:.1f} mean, "
            f"{findings['fps_p5_worst']:.1f} at the 5th-percentile worst frame "
            f"({findings['frames_measured']:.0f} frames)"
        )
        print(f"- Worst frame: {findings['frame_ms_worst']:.1f} ms")
        print(f"- Resident memory: {findings.get('resident_mb') or 'unmeasured'} MB")
        print(
            f"- Driver line-width range: aliased {findings.get('aliased_line_width_range')}, "
            f"smooth {findings.get('smooth_line_width_range')}"
        )
    print(f"- Dash/stipple parameter: {findings.get('accepts_dash')}")
    print(f"  ({findings.get('setdata_rejection')})")
    print(f"- Draw path: {findings.get('draw_path')}")
    print(f"- Persistent VBO attributes: {findings.get('persistent_vbo_attrs')}")
    print(f"- Uploads vertex data only when dirty: {findings.get('uploads_only_when_dirty')}")
    print(
        f"- Applies glLineWidth: {findings.get('applies_gl_line_width')}; "
        f"skips it on a core forward-compatible profile: "
        f"{findings.get('skips_line_width_on_core_profile')}\n"
    )

    # A number pinned near the display refresh rate is measuring the compositor, not the GPU. It
    # still answers "does it clear 30 fps", but it says nothing about headroom, and reading it as
    # a capacity figure would badly understate what the hardware can do.
    if fps is not None and 55.0 <= fps <= 65.0:
        print(
            "NOTE: ~60 fps almost certainly means vsync-capped, not GPU-limited. This confirms the\n"
            "      30 fps threshold but hides all headroom. Re-run uncapped to measure capacity:\n"
            "        vblank_mode=0 .venv/bin/python spikes/render_500k.py   (Mesa/GLX)\n"
        )

    if fps is None:
        print("VERDICT: introspection only — no framerate measured, D1 stays open.")
    elif fps >= 30.0:
        print(f"VERDICT: pyqtgraph HOLDS at {fps:.1f} fps median (D1 threshold is 30).")
    else:
        print(
            f"VERDICT: pyqtgraph FAILS at {fps:.1f} fps median (< 30). M2 needs a raw "
            "QOpenGLWidget with our own shaders and VBOs; re-granulate T2.5-T2.7."
        )
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        findings = introspect_pyqtgraph()
    except ImportError as exc:
        print(f"cannot import pyqtgraph: {exc}", file=sys.stderr)
        print('install the GUI extra: .venv/bin/pip install -e ".[dev,gui]"', file=sys.stderr)
        return 2

    if not args.introspect_only:
        from PySide6 import QtWidgets

        print(f"generating {args.segments:,} segments...", flush=True)
        started = time.perf_counter()
        lin, kind = synth_segments(args.segments)
        print(f"generated in {time.perf_counter() - started:.2f}s", flush=True)

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        view = build_view(args, lin, kind)
        findings["items_uploaded"] = len(view.items)
        print(f"uploaded into {len(view.items)} items; orbiting...", flush=True)
        view.show()
        app.exec()

        if not view.frame_deltas:
            print("no frames were rendered — is a display available?", file=sys.stderr)
            return 1
        findings.update(summarise(view.frame_deltas))
        findings.update(view.gl_info)
        findings["resident_mb"] = resident_mb()

    report(args, findings)
    if args.json:
        print("\n" + json.dumps(findings, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
