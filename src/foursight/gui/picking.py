"""Segment ↔ screen hit-testing.

This module exists because batched rendering rules out Qt item picking: with 500k segments in
≤ 10 buffers there are no per-segment scene items to hit-test. The strategy is either GPU
colour-picking to an offscreen target or a CPU KD-tree over segment midpoints — chosen in T3.0
(decision D4 in TASKS.md), and not a one-liner either way.

Implemented in T3.3.
"""
