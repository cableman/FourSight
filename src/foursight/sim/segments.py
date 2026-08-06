"""``SegmentStore``: columnar segment storage — parallel numpy arrays.

Never per-object segment dataclasses: 500k of those cost over 200 MB before the GL buffers and
then have to be repacked into contiguous arrays anyway. ``lin.reshape(-1, 3)`` is a zero-copy
view in exactly the layout ``GLLinePlotItem(mode='lines')`` wants. A ``Segment`` view class may
exist for test readability, but must never be the storage.

Invariants:

- ``rot`` is its own column, kept out of the position vector, so no norm is ever taken across
  millimetres and degrees.
- ``lin`` is always machine coordinates; display transforms write ``lin_part``.
- ``kind`` is motion type only (RAPID | FEED) — after interpolation everything is a line
  segment, and an arc is always a cutting move.
- Every segment traces back to a source line via ``line[i]``; editor sync, diagnostics, and
  fixes all depend on it.

Implemented in T2.1.
"""
