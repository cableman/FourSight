"""Line → words (letter + number).

One compiled regex per line, not per word — the ~20 µs/line budget does not survive otherwise.
Handles comments ``( )`` and ``;``, block delete ``/``, N-numbers, leading/trailing ``%``, and
Fanuc ``Oxxxx`` program numbers (consumed silently, never flagged).

Creates each ``SourceRef`` once per line, shared by every ``Command`` and segment derived from it.

Implemented in T1.2.
"""
