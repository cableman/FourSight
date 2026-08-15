"""What the colours in the viewport mean. **No Qt.**

The viewport distinguishes everything it draws by **colour alone**, and not by choice: `GLLinePlotItem`
has no dash or stipple parameter, and pyqtgraph skips `glLineWidth` entirely on core forward-compatible
profiles, so anything encoded in thickness vanishes there with no error. PLAN.md § Batching Layer states
it plainly — *"colour carries every distinction; line width carries none."*

That is a problem this codebase already refuses to accept anywhere else. `diagnostics_panel` gives each
severity its own colour **and** its own symbol, and `editor` underlines malformed input as well as
colouring it, both for the reason stated in their docstrings: colour alone collapses the distinction for
a colour-blind reader. The viewport cannot add a second visual channel to the geometry — so the legend
is that channel. It supplies the **names**. Red rapids against green feeds is precisely the worst case
for the commonest form of colour blindness, which is why the legend is shown by default rather than
hidden behind a preference.

Two rules keep it honest, and both are about it agreeing with what is actually on screen:

**The legend never restates a colour or a name; it reads them off the thing being drawn.** Rows come
from `Batch.label` and `Batch.color` — the very values `build_batches` gave the GL item. A second table
of names and swatches here would be a second copy of the styling decision, and the two disagreeing is a
legend that confidently mislabels the picture. `HIGHLIGHT_COLOR` and `MARKER_COLOR` live in *this*
module and `viewport3d` imports them, for the same reason `batching` owns the batch colours.

**It lists what is on screen, not what could be.** A program with no cutter-compensated span has no
unverified batch and gets no unverified row. Rows for colours that are not present would train the
reader to ignore the legend, and the amber row appearing is itself information.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from foursight.gui.batching import Batch, Color

#: The selection highlight. Bright and cool, so it cannot be mistaken for a rapid (red), a feed (green)
#: or an unverified span (amber) — the highlight is a *view* state, not a property of the toolpath.
HIGHLIGHT_COLOR: Color = (0.35, 0.95, 1.0, 1.0)
#: The playback tool position. White, so it is the brightest thing on screen and reads as neither a
#: motion type nor the selection.
MARKER_COLOR: Color = (1.0, 1.0, 1.0, 1.0)

#: Wording fixed here rather than at each call site, so the two view-state rows cannot drift apart from
#: the phrasing the status bar already uses for the same things.
SELECTION_LABEL = "Selected line"
MARKER_LABEL = "Tool position"


class Swatch(StrEnum):
    """How an entry is drawn, so its swatch can look like the thing it names."""

    LINE = "line"
    POINT = "point"  # the playback marker really is a dot, and a line swatch would misdescribe it


@dataclass(frozen=True, slots=True)
class LegendEntry:
    """One row: a colour actually on screen, and what it means."""

    label: str
    color: Color
    swatch: Swatch


def legend_entries(
    batches: Sequence[Batch], *, highlighted: bool = False, marker: bool = False
) -> tuple[LegendEntry, ...]:
    """The rows for what is currently drawn, or ``()`` when nothing is.

    Geometry first, in the order `build_batches` produced it — trusted rapid, trusted feed, then the
    unverified tiers — followed by view state. That grouping is the point: the first rows describe the
    *program*, the last describe what the viewer is doing to it, and a reader who has understood that
    split can ignore half the legend.
    """
    entries = [LegendEntry(_display(batch.label), batch.color, Swatch.LINE) for batch in batches]
    if highlighted:
        entries.append(LegendEntry(SELECTION_LABEL, HIGHLIGHT_COLOR, Swatch.LINE))
    if marker:
        entries.append(LegendEntry(MARKER_LABEL, MARKER_COLOR, Swatch.POINT))
    return tuple(entries)


def _display(label: str) -> str:
    """``"feed (unverified)"`` → ``"Feed (unverified)"``.

    Presentation only. The words themselves stay `build_batches`' — capitalising here is what lets the
    legend reuse them instead of keeping a parallel table that a rename could leave behind.
    """
    return label[:1].upper() + label[1:]


def hex_color(color: Color) -> str:
    """``(0.90, 0.25, 0.20, 1.0)`` → ``"#e64033"``.

    Alpha is dropped: every colour in the palette is opaque, and a widget stylesheet that carried an
    alpha the GL item does not have would show a swatch paler than the line it names.
    """
    red, green, blue = (min(255, max(0, round(channel * 255))) for channel in color[:3])
    return f"#{red:02x}{green:02x}{blue:02x}"
