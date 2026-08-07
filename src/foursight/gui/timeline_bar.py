"""The timeline scrubber widget. Thin: `timeline.py` holds the time↔segment logic and imports no Qt.

Scrubbing **moves the editor cursor**, which highlights the line through the T3.2 path. That is the point
of it — watching the code scroll past as the tool advances is how a timeline earns its place in an editor
rather than in a player. It also means the scrubber needs no highlight machinery of its own.

The slider works in **integer thousandths of the total**, not seconds: `QSlider` is integral, and a
seconds-valued slider would quantize a 40-hour program to one-second steps and a two-second program to
two positions. Thousandths give the same resolution to both.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QWidget

from foursight.gui.timeline import Timeline, build_timeline, describe_position

#: Slider granularity. Position is `round(fraction * TICKS)`, so resolution is relative to the program
#: rather than absolute — a 2-second program and a 40-hour one both get 1000 steps.
TICKS = 1000


class TimelineBar(QWidget):
    """A scrubber over a program's cumulative duration."""

    #: The segment index at the scrub position. The window turns this into a line and a highlight.
    scrubbed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.timeline: Timeline | None = None

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, TICKS)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_value_changed)

        self.readout = QLabel("No geometry")
        self.readout.setMinimumWidth(320)
        self.readout.setStyleSheet("color: #a0a0a0; padding-right: 8px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.addWidget(QLabel("Time"))
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.readout)

    # ------------------------------------------------------------------ content

    def set_simulation(self, simulation) -> None:
        """Rebuild the timeline for a newly loaded program and reset to the start."""
        self.timeline = build_timeline(simulation.store, simulation.unknown_durations)
        usable = self.timeline.segments > 0 and self.timeline.total > 0.0
        # Disabled when there is no *time* to scrub through, not merely no geometry: a program whose every
        # feed rate is unknown has segments but a zero total, and a slider that moves without changing
        # anything is worse than one that plainly cannot be moved.
        self.slider.setEnabled(usable)
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._update_readout(self.timeline.index_at(0.0), None)

    def clear(self) -> None:
        self.timeline = None
        self.slider.setEnabled(False)
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.readout.setText("No geometry")

    # ------------------------------------------------------------------ scrubbing

    @property
    def seconds(self) -> float:
        """The scrub position in seconds."""
        if self.timeline is None:
            return 0.0
        return self.slider.value() / TICKS * self.timeline.total

    def index(self) -> int | None:
        return None if self.timeline is None else self.timeline.index_at(self.seconds)

    def _on_value_changed(self, _value: int) -> None:
        index = self.index()
        if index is not None:
            self.scrubbed.emit(index)

    def show_line(self, line_no: int | None) -> None:
        """Called back by the window once it has resolved the segment to a source line."""
        self._update_readout(self.index(), line_no)

    def _update_readout(self, index: int | None, line_no: int | None) -> None:
        if self.timeline is None:
            self.readout.setText("No geometry")
            return
        self.readout.setText(describe_position(self.timeline, index, line_no))
