"""Rotary transforms: table mount (part rotates) and head mount (tool swings).

Table::

    p_part = R_axis(-A) @ (p_tool - centerline) + centerline

Head — the tool *tip translates* as the head swings; it is not simply the machine XYZ::

    p_tip = p_pivot + R_axis(A) @ (0, 0, -pivot_to_tip)

Populates ``SegmentStore.lin_part`` and **never mutates ``lin``**, which must stay in machine
coordinates for verification. Simultaneous XYZ+A moves are transformed per interpolation step;
endpoint-only transformation draws helical and wrapped paths as straight chords.

Ruff ``N806`` is ignored in this module because ``R`` for a rotation matrix is correct.

Implemented in T4.1–T4.3.
"""
