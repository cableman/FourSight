"""GL view, camera, and batched geometry.

Pre-batch into ≤ 10 buffers grouped by ``kind``; never one draw call per move. Rapids red, feeds
green — colour-only unless the M0 render spike shows the extra rapid vertices for baked dashes
are free, because ``GLLinePlotItem`` has no dash or stipple parameter. Do not rely on
``glLineWidth > 1.0`` to carry meaning; not all drivers honour it.

Whether this stays on pyqtgraph or drops to a raw ``QOpenGLWidget`` with our own shaders and VBOs
is decided by the T0.7 spike (decision D1 in TASKS.md).

Implemented in T2.6.
"""
