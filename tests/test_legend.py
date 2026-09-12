"""Legend tests (T11.1/T11.2). The model half needs no Qt; the overlay half runs offscreen.

Two properties carry the weight, and neither is about layout.

**The legend names what is on screen, and only that.** A row for a colour the program does not contain
would train the reader to ignore the key, and the unverified row *appearing* is itself the information —
it means part of this picture cannot be trusted, with the span's own reason saying which part.

**The legend does not restate the palette; it reads it off the batches.** Every label and every colour
comes from the `Batch` that was uploaded to GL. A second table here would be a second copy of the
styling decision, and the failure mode of the two disagreeing is a key that confidently mislabels the
picture — which is worse than no key at all. The tests below compare against `batching`'s own constants
rather than against literals for exactly that reason.
"""

import os

import numpy as np
import pytest

from conftest import DEFAULT_PROFILE_PATH, fixture_text
from foursight.gui.batching import TRUSTED_COLORS, UNTRUSTED_COLORS, build_batches
from foursight.gui.legend import (
    HIGHLIGHT_COLOR,
    MARKER_COLOR,
    MARKER_LABEL,
    SELECTION_LABEL,
    SOLID_COLOR,
    SOLID_LABEL,
    Swatch,
    hex_color,
    legend_entries,
)
from foursight.machine.profile import load_profile
from foursight.sim.segments import Kind, SegmentBuilder, SegmentStore
from foursight.sim.simulator import simulate_text


@pytest.fixture(scope="module")
def profile():
    return load_profile(DEFAULT_PROFILE_PATH)


def store_of(*kinds: Kind) -> SegmentStore:
    """A store with one segment of each given kind."""
    builder = SegmentBuilder()
    for index, kind in enumerate(kinds, start=1):
        points = np.array([[float(index - 1), 0.0, 0.0], [float(index), 0.0, 0.0]])
        builder.add_polyline(points, kind, index)
    return builder.finalize()


def labels(entries) -> list[str]:
    return [entry.label for entry in entries]


# --------------------------------------------------------------------------- the model


def test_nothing_on_screen_has_no_legend() -> None:
    """An empty key over an empty viewport says nothing and reads as a rendering failure."""
    assert legend_entries([]) == ()


def test_each_drawn_batch_gets_a_row() -> None:
    batches = build_batches(store_of(Kind.RAPID, Kind.FEED))
    assert labels(legend_entries(batches)) == ["Rapid", "Feed"]


def test_a_program_without_unverified_geometry_has_no_unverified_row() -> None:
    """The row appearing is the information. Listing it always would train the reader past it."""
    batches = build_batches(store_of(Kind.RAPID, Kind.FEED))
    assert not any("unverified" in label for label in labels(legend_entries(batches)))


def test_an_unverified_span_names_itself(profile) -> None:
    sim, _ = simulate_text(fixture_text("cutter_comp_span.nc"), profile)
    assert sim.unverified_mask().any(), "the fixture no longer produces an unverified span"
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    assert "Feed (unverified)" in labels(legend_entries(batches))


def test_the_labels_are_the_batches_own_labels(profile) -> None:
    """Not a parallel table. A rename in `build_batches` must reach the legend, not diverge from it."""
    sim, _ = simulate_text(fixture_text("cutter_comp_span.nc"), profile)
    batches = build_batches(sim.store, untrusted=sim.unverified_mask())
    expected = [batch.label[:1].upper() + batch.label[1:] for batch in batches]
    assert labels(legend_entries(batches)) == expected


def test_the_colours_are_the_batches_own_colours() -> None:
    """A legend swatch that is merely *similar* to the line it names is a legend that can be wrong."""
    batches = build_batches(store_of(Kind.RAPID, Kind.FEED))
    entries = legend_entries(batches)
    assert [entry.color for entry in entries] == [batch.color for batch in batches]
    assert entries[0].color == TRUSTED_COLORS[Kind.RAPID]
    assert entries[1].color == TRUSTED_COLORS[Kind.FEED]


def test_the_two_unverified_tiers_are_not_the_same_colour() -> None:
    """PLAN.md once said "both amber". They are two distinct ambers, and the key shows both."""
    assert UNTRUSTED_COLORS[Kind.RAPID] != UNTRUSTED_COLORS[Kind.FEED]
    store = store_of(Kind.RAPID, Kind.FEED)
    batches = build_batches(store, untrusted=np.ones(len(store), dtype=bool))
    colours = [entry.color for entry in legend_entries(batches)]
    assert len(set(colours)) == len(colours), "two rows share a swatch colour"


def test_view_state_rows_appear_only_when_active() -> None:
    batches = build_batches(store_of(Kind.FEED))
    assert labels(legend_entries(batches)) == ["Feed"]
    assert labels(legend_entries(batches, highlighted=True)) == ["Feed", SELECTION_LABEL]
    assert labels(legend_entries(batches, marker=True)) == ["Feed", MARKER_LABEL]


def test_geometry_comes_before_view_state() -> None:
    """The first rows describe the program, the last describe what the viewer is doing to it."""
    batches = build_batches(store_of(Kind.RAPID, Kind.FEED))
    entries = legend_entries(batches, highlighted=True, marker=True)
    assert labels(entries) == ["Rapid", "Feed", SELECTION_LABEL, MARKER_LABEL]
    assert entries[-2].color == HIGHLIGHT_COLOR
    assert entries[-1].color == MARKER_COLOR


def test_the_tool_position_is_a_point_and_everything_else_is_a_line() -> None:
    """A swatch that does not resemble what it names has to be decoded twice."""
    batches = build_batches(store_of(Kind.FEED))
    entries = legend_entries(batches, highlighted=True, marker=True)
    assert [entry.swatch for entry in entries] == [Swatch.LINE, Swatch.LINE, Swatch.POINT]


def test_view_state_can_be_named_with_no_geometry_at_all() -> None:
    """A marker with no batches is unusual but reachable, and must not produce an unlabelled dot."""
    assert labels(legend_entries([], marker=True)) == [MARKER_LABEL]


def test_hex_conversion_covers_every_colour_in_the_palette() -> None:
    assert hex_color(TRUSTED_COLORS[Kind.RAPID]) == "#e64033"
    assert hex_color(TRUSTED_COLORS[Kind.FEED]) == "#33d959"
    assert hex_color(UNTRUSTED_COLORS[Kind.RAPID]) == "#d9731a"
    assert hex_color(UNTRUSTED_COLORS[Kind.FEED]) == "#ffbf26"
    assert hex_color(HIGHLIGHT_COLOR) == "#59f2ff"
    assert hex_color(MARKER_COLOR) == "#ffffff"


def test_hex_conversion_drops_alpha_rather_than_encoding_it() -> None:
    """A swatch carrying an alpha the GL item does not have would look paler than the line it names."""
    assert hex_color((1.0, 0.0, 0.0, 0.25)) == hex_color((1.0, 0.0, 0.0, 1.0))


# --------------------------------------------------------------------------- the overlay

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the [gui] extra is not installed")
pytest.importorskip("pyqtgraph", reason="the [gui] extra is not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from foursight.gui.viewport3d import ToolpathViewport  # noqa: E402

GRID_ITEMS = 1


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def viewport(qt_app):
    try:
        widget = ToolpathViewport()
    except Exception as error:  # pragma: no cover - environment-dependent
        pytest.skip(f"cannot construct a GL widget on this platform: {error}")
    widget.resize(640, 480)
    return widget


def simulation(text: str, profile):
    sim, _ = simulate_text(text, profile)
    return sim


def shown(widget) -> bool:
    """Whether the widget is showing *itself*.

    `isVisible()` is useless here: it is False whenever an ancestor is unshown, and the fixture never
    calls `show()` on the viewport. `isHidden()` reports the widget's own explicit state, which is the
    thing under test.
    """
    return not widget.isHidden()


def test_the_legend_is_hidden_until_there_is_something_to_name(viewport) -> None:
    assert viewport.legend_visible is True, "the key is on by default"
    assert shown(viewport.legend) is False


def test_loading_a_program_names_its_colours(viewport, profile) -> None:
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    text = viewport.legend.text()
    for batch in viewport.batches:
        assert hex_color(batch.color) in text
        assert batch.label[:1].upper() + batch.label[1:] in text


def test_the_legend_forgets_the_previous_programs_rows(viewport, profile) -> None:
    """The failure this guards: a key still describing the file you just closed."""
    viewport.set_simulation(simulation(fixture_text("cutter_comp_span.nc"), profile))
    assert "unverified" in viewport.legend.text()

    viewport.set_simulation(simulation("G21 G94 G90\nG1 X10 Y10 F600\n", profile))
    assert "unverified" not in viewport.legend.text()


def test_selecting_and_deselecting_adds_and_removes_its_row(viewport, profile) -> None:
    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    assert SELECTION_LABEL not in viewport.legend.text()

    viewport.set_highlight(sim.store, sim.store.line == sim.store.line[0])
    assert SELECTION_LABEL in viewport.legend.text()

    viewport.clear_highlight()
    assert SELECTION_LABEL not in viewport.legend.text()


def test_the_tool_position_row_follows_the_marker(viewport, profile) -> None:
    from foursight.gui.timeline import build_timeline

    sim = simulation(fixture_text("baseline_4axis.nc"), profile)
    viewport.set_simulation(sim)
    assert MARKER_LABEL not in viewport.legend.text()

    viewport.set_marker(sim.store, build_timeline(sim.store), 0.0)
    assert MARKER_LABEL in viewport.legend.text()

    viewport.clear_marker()
    assert MARKER_LABEL not in viewport.legend.text()


def test_turning_the_legend_off_hides_it_without_forgetting_the_program(viewport, profile) -> None:
    viewport.set_simulation(simulation(fixture_text("baseline_4axis.nc"), profile))
    assert shown(viewport.legend) is True

    viewport.set_legend_visible(False)
    assert shown(viewport.legend) is False

    viewport.set_legend_visible(True)
    assert shown(viewport.legend) is True
    assert "Feed" in viewport.legend.text()


def test_turning_it_on_with_no_program_leaves_it_hidden(viewport) -> None:
    viewport.set_legend_visible(False)
    viewport.set_legend_visible(True)
    assert shown(viewport.legend) is False


def test_the_legend_is_a_widget_and_not_a_scene_item(viewport, profile) -> None:
    """It must stay out of `viewport.items`, or the item-count regression tests stop meaning anything
    and it starts counting against the PLAN.md buffer budget."""
    viewport.set_simulation(simulation(fixture_text("cutter_comp_span.nc"), profile))
    assert viewport.legend.parent() is viewport
    assert len(viewport.items) == GRID_ITEMS + len(viewport.batches)
    assert viewport.legend not in viewport.items


# ------------------------------------------------------------------- the carved solid row (M12)


def test_no_solid_row_when_there_is_no_solid():
    """`None` means no solid; an empty sequence means a solid with nothing to qualify."""
    entries = legend_entries(build_batches(store_of(Kind.FEED)), solid=None)
    assert [entry.label for entry in entries] == ["Feed"]


def test_a_clean_carve_still_gets_a_row():
    """The commonest case has no caveats, and it must not therefore vanish from the key."""
    entries = legend_entries(build_batches(store_of(Kind.FEED)), solid=())
    assert entries[-1].label == SOLID_LABEL
    assert entries[-1].note == ""


def test_the_carve_notes_reach_the_row():
    entries = legend_entries(
        [], solid=("carved with one tool", "3 unverified segments were not cut")
    )
    assert entries[0].note == "carved with one tool; 3 unverified segments were not cut"


def test_the_solid_uses_a_swatch_of_its_own():
    """A line swatch would describe it as one more kind of move; it is the only thing with area."""
    entries = legend_entries([], solid=())
    assert entries[0].swatch is Swatch.SOLID
    assert entries[0].color == SOLID_COLOR


def test_the_solid_row_precedes_the_view_state_rows():
    """Geometry first, then what the viewer is doing to it. The solid is geometry."""
    entries = legend_entries([], solid=(), highlighted=True, marker=True)
    assert [entry.label for entry in entries] == [SOLID_LABEL, SELECTION_LABEL, MARKER_LABEL]


def test_the_solid_colour_is_not_a_toolpath_colour():
    """A shared colour would make the surface read as a motion type."""
    assert SOLID_COLOR not in set(TRUSTED_COLORS.values()) | set(UNTRUSTED_COLORS.values())
