"""G-code parsing: text → ``Command`` objects.

Owns modal-group resolution, which is why this is a custom parser rather than pygcode.
Nothing here may import from ``foursight.machine``: the dependency direction is
parser → machine → sim → verify → fix → gui.
"""
