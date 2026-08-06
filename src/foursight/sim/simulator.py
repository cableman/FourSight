"""Steps ``Command`` objects → ``SegmentStore``.

Writes ``lin`` in machine coordinates, always. Suppresses motion geometry across ``unsupported``
spans (canned cycles G80–G89) and marks cutter-compensation spans as unverified rather than
drawing them as if understood: never render a confidently wrong toolpath.

Implemented in T2.5; runs off the GUI thread from T2.9.
"""
