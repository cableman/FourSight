"""Which segments belong to a source line, and what to say when none do. **No Qt.**

`SegmentStore.line` exists precisely for this: every segment records the line it came from, so
line → segments is one comparison over a numpy column — 0.8 ms at 500k segments including the vertex
gather, which is why this can follow the cursor rather than waiting for a click.

The part worth testing is not the mask. It is **what an empty result means**, because there are three
reasons a line highlights nothing and they are not interchangeable:

- **No motion.** A comment, an M-code, a modal-only block. Nothing to draw, nothing wrong.
- **Suppressed.** The line *does* command motion and the simulator refused to draw it — an
  uninterpreted canned cycle, a lost position. Reporting this as "no motion" would tell the user their
  drill cycle does nothing, which is worse than saying nothing at all. PLAN.md's governing principle
  applies to the selection readout as much as to the viewport.
- **Out of range.** A line number past the end of the program, which only arises from a stale
  diagnostic or a bug, and should not be silently indistinguishable from a comment.

So `select_line` returns *why*, not just *what*.
"""

from dataclasses import dataclass

import numpy as np

from foursight.sim.simulator import Simulation, Span


@dataclass(frozen=True, slots=True)
class LineSelection:
    """The segments a source line produced, and how to describe the result."""

    line_no: int
    mask: np.ndarray  # (N,) bool over the store
    count: int
    suppressed: Span | None  # the span that stopped this line being drawn, if any
    unverified: Span | None  # drawn, but not where the tool actually goes

    @property
    def has_geometry(self) -> bool:
        return self.count > 0

    def describe(self) -> str:
        """A status-bar line. Says *why* there is nothing, when there is nothing."""
        if self.suppressed is not None and not self.has_geometry:
            return f"Line {self.line_no}: not drawn — {self.suppressed.reason}"
        if not self.has_geometry:
            return f"Line {self.line_no}: no motion"
        segments = "segment" if self.count == 1 else "segments"
        if self.unverified is not None:
            return (
                f"Line {self.line_no}: {self.count:,} {segments} — unverified, "
                "shown as the programmed centreline"
            )
        return f"Line {self.line_no}: {self.count:,} {segments}"


def select_line(simulation: Simulation, line_no: int) -> LineSelection:
    """The segments produced by 1-based ``line_no``, with the reason when there are none.

    ``line_no`` is a *source* line number, the same one `SourceRef.line_no`, `Diagnostic.line` and
    `SegmentStore.line` use. `CodeEditor.source_line_count` exists so callers can bound it correctly on
    newline-terminated text without an off-by-one.
    """
    store = simulation.store
    mask = store.line == line_no
    count = int(mask.sum())
    return LineSelection(
        line_no=line_no,
        mask=mask,
        count=count,
        suppressed=_span_covering(simulation.suppressed, line_no),
        unverified=_span_covering(simulation.unverified, line_no),
    )


def empty_selection(simulation: Simulation) -> LineSelection:
    """A selection of nothing, for when the cursor is nowhere meaningful.

    Returns a correctly-shaped all-False mask rather than an empty array, so callers can use it without
    a special case and a shape mismatch cannot reach the renderer.
    """
    return LineSelection(
        line_no=0,
        mask=np.zeros(len(simulation.store), dtype=bool),
        count=0,
        suppressed=None,
        unverified=None,
    )


def _span_covering(spans: tuple[Span, ...], line_no: int) -> Span | None:
    for span in spans:
        if span.contains(line_no):
            return span
    return None
