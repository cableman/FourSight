"""Per-segment durations for the timeline.

Linear and rotary travel are measured **separately** and combined as
``max(linear_time, rotary_time)``. Never ``np.linalg.norm`` across linear (mm) and rotary (deg)
components — that expression is meaningless, and it is exactly what one would otherwise write
for a duration. The columnar split in ``segments.py`` exists to make this mistake hard.

Honours G93 inverse-time, G94 units/min, G95 units/rev, and per-axis ``max_rapid``.

Implemented in T2.4.
"""
