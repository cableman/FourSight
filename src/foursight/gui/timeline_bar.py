"""The timeline scrubber and transport. Thin: `timeline.py` holds the time↔segment logic and
`playback.py` the clock, and neither imports Qt.

Scrubbing **moves the editor cursor**, which highlights the line through the T3.2 path. That is the point
of it — watching the code scroll past as the tool advances is how a timeline earns its place in an editor
rather than in a player. It also means the scrubber needs no highlight machinery of its own, and playback
inherits that for free by going through the same signal.

The slider works in **integer thousandths of the total**, not seconds: `QSlider` is integral, and a
seconds-valued slider would quantize a 40-hour program to one-second steps and a two-second program to
two positions. Thousandths give the same resolution to both.

The slider is a **view of the clock, not the position itself** (T10.4). Thousandths are ample for
dropping a handle somewhere and far too coarse to animate: one tick of an hour-long program is 3.6
seconds, so a marker driven from `slider.value()` would jump between stills. `Playback.seconds` is a
float and is authoritative; the slider follows it.

The timer lives here rather than in the window because the reset choreography already does. Every path
that reloads a program calls `set_simulation`, so stopping playback there means no caller can forget to
— and a `QTimer` parented to this widget needs nothing at shutdown.
"""

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QStyle,
    QToolButton,
    QWidget,
)

from foursight.gui.playback import SPEEDS, Playback
from foursight.gui.timeline import Timeline, build_timeline, describe_position

#: Slider granularity. Position is `round(fraction * TICKS)`, so resolution is relative to the program
#: rather than absolute — a 2-second program and a 40-hour one both get 1000 steps.
TICKS = 1000

#: Frame interval in milliseconds, ~30 fps — the rate PLAN.md requires of the viewport. The exact value
#: does not have to be honoured, because each frame *measures* the wall time it actually got rather than
#: assuming it got this; a slow frame advances further instead of playing in slow motion.
FRAME_MS = 33


class TimelineBar(QWidget):
    """A scrubber and transport over a program's cumulative duration."""

    #: The segment index at the current position. The window turns this into a line and a highlight.
    scrubbed = Signal(int)
    #: The position in program seconds, at full float resolution. The window turns this into a marker.
    #: Separate from `scrubbed` because a segment index cannot express where *inside* a move the tool is.
    advanced = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.timeline: Timeline | None = None
        self.playback: Playback | None = None

        self.play_button = QToolButton()
        self.play_button.setAutoRaise(True)
        self.play_button.setToolTip("Play / pause  (Ctrl+Space)")
        self.play_button.clicked.connect(self.toggle_playback)

        self.reset_button = QToolButton()
        self.reset_button.setAutoRaise(True)
        self.reset_button.setToolTip("Back to the start")
        self.reset_button.setIcon(self._icon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.reset_button.clicked.connect(self.reset_playback)

        self.speed_box = QComboBox()
        self.speed_box.setToolTip("Program seconds per second of playback")
        for speed in SPEEDS:
            self.speed_box.addItem(f"{speed:g}×", speed)
        self.speed_box.currentIndexChanged.connect(self._on_speed_changed)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, TICKS)
        self.slider.valueChanged.connect(self._on_value_changed)

        self.readout = QLabel("No geometry")
        self.readout.setMinimumWidth(320)
        self.readout.setStyleSheet("color: #a0a0a0; padding-right: 8px;")

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        # Monotonic, and *measured* rather than assumed: accumulating `FRAME_MS` per tick would drift
        # against the clock the readout claims to be showing.
        self._elapsed = QElapsedTimer()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.addWidget(self.play_button)
        layout.addWidget(self.reset_button)
        layout.addWidget(self.speed_box)
        layout.addWidget(QLabel("Time"))
        layout.addWidget(self.slider, stretch=1)
        layout.addWidget(self.readout)

        self._set_enabled(False)
        self._sync_transport()

    def _icon(self, pixmap: QStyle.StandardPixmap):
        """Transport icons from the platform style, so there are no assets to ship or theme."""
        return self.style().standardIcon(pixmap)

    # ------------------------------------------------------------------ content

    def set_simulation(self, simulation) -> None:
        """Rebuild the timeline for a newly loaded program and reset to the start."""
        self._timer.stop()
        self.timeline = build_timeline(simulation.store, simulation.unknown_durations)
        self.playback = Playback(total=self.timeline.total)
        # The speed selection survives the load on purpose. Every applied fix reloads the program, and
        # a multiplier that snapped back to 1× each time would make a 100× inspection unusable.
        self.playback.speed = self._selected_speed()

        usable = self.timeline.segments > 0 and self.timeline.total > 0.0
        # Disabled when there is no *time* to scrub through, not merely no geometry: a program whose every
        # feed rate is unknown has segments but a zero total, and a slider that moves without changing
        # anything is worse than one that plainly cannot be moved. The transport goes with it — a play
        # button that lights up and does nothing reads as a broken player rather than an untimed program.
        self._set_enabled(usable)
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._update_readout(self.timeline.index_at(0.0), None)
        self._sync_transport()

    def clear(self) -> None:
        self._timer.stop()
        self.timeline = None
        self.playback = None
        self._set_enabled(False)
        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.readout.setText("No geometry")
        self._sync_transport()

    def _set_enabled(self, usable: bool) -> None:
        for widget in (self.slider, self.play_button, self.reset_button, self.speed_box):
            widget.setEnabled(usable)

    # ------------------------------------------------------------------ transport (T10.4)

    def toggle_playback(self) -> None:
        """Start or stop playing. Public because the window binds it to a menu action."""
        if self.playback is None:
            return
        self.playback.toggle()
        if self.playback.playing:
            self._elapsed.restart()
            self._timer.start()
        else:
            self._timer.stop()
        self._sync_transport()

    def reset_playback(self) -> None:
        """Rewind to the start and stop.

        Unlike the reset inside `set_simulation`, this one **emits**: the user asked to go back to the
        beginning, and jumping the editor there is what they asked for.
        """
        if self.playback is None:
            return
        self.playback.reset()
        self._timer.stop()
        self._sync_transport()
        self._apply_position(0.0)

    def _tick(self, dt: float | None = None) -> None:
        """One frame. ``dt`` is the wall seconds elapsed; None measures it.

        Tests pass ``dt`` directly rather than sleeping, which is the whole reason `Playback.advance`
        takes it as an argument.
        """
        if self.playback is None:
            return
        if dt is None:
            dt = self._elapsed.restart() / 1000.0
        if self.slider.isSliderDown():
            # The handle is held: the user owns the position until they let go. The elapsed time is
            # consumed above and discarded, so releasing does not deliver it all in one jump.
            #
            # Returning early is not politeness, it is correctness at speed. A stationary held handle
            # emits no `valueChanged`, so nothing would seek the clock back — at 1000× the position
            # would run away underneath the user's fingers while `_apply_position` yanked the handle
            # after it.
            return
        seconds = self.playback.advance(dt)
        self._apply_position(seconds)
        if not self.playback.playing:  # `advance` pauses itself at the end
            self._timer.stop()
            self._sync_transport()

    def _sync_transport(self) -> None:
        playing = self.playback is not None and self.playback.playing
        pixmap = (
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.play_button.setIcon(self._icon(pixmap))

    def _selected_speed(self) -> float:
        speed = self.speed_box.currentData()
        return SPEEDS[0] if speed is None else float(speed)

    def _on_speed_changed(self, _index: int) -> None:
        if self.playback is not None:
            self.playback.set_speed(self._selected_speed())

    # ------------------------------------------------------------------ scrubbing

    @property
    def seconds(self) -> float:
        """The current position in seconds.

        Read from the clock rather than the slider: the slider is quantized to thousandths, which is
        fine for choosing a position and far too coarse for reporting one during playback.
        """
        return 0.0 if self.playback is None else self.playback.seconds

    def index(self) -> int | None:
        return None if self.timeline is None else self.timeline.index_at(self.seconds)

    def _slider_seconds(self) -> float:
        if self.timeline is None:
            return 0.0
        return self.slider.value() / TICKS * self.timeline.total

    def _on_value_changed(self, _value: int) -> None:
        """Only ever user-initiated: every programmatic move blocks signals first."""
        if self.playback is None:
            return
        # A drag during playback is a seek, not a stop — `seek` leaves `playing` alone.
        self.playback.seek(self._slider_seconds())
        self._emit_position()

    def _apply_position(self, seconds: float) -> None:
        """Move the handle to ``seconds`` without letting it look like the user did."""
        if self.timeline is not None and self.timeline.total > 0.0:
            self.slider.blockSignals(True)
            self.slider.setValue(round(seconds / self.timeline.total * TICKS))
            self.slider.blockSignals(False)
        self._emit_position()

    def _emit_position(self) -> None:
        index = self.index()
        self._update_readout(index, None)
        self.advanced.emit(self.seconds)
        if index is not None:
            self.scrubbed.emit(index)

    def show_line(self, line_no: int | None) -> None:
        """Called back by the window once it has resolved the segment to a source line."""
        self._update_readout(self.index(), line_no)

    def _update_readout(self, index: int | None, line_no: int | None) -> None:
        if self.timeline is None:
            self.readout.setText("No geometry")
            return
        # The clock's position, not the end of the segment it lands in: during playback the latter
        # would tick forward in jumps of whatever the current move happens to take.
        self.readout.setText(describe_position(self.timeline, index, line_no, seconds=self.seconds))
