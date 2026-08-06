"""Position tracking for verification (minimal; T2.2 extends this for simulation).

Scope is deliberately narrow: **endpoint positions only**, no interpolation. That is enough for the
M1 verifier, which checks block endpoints; T2.8 re-runs the travel-limit check over interpolated
points, because an arc can bulge past a limit mid-sweep without either endpoint doing so.

Built here rather than duplicated inside two check modules, and rather than deferred to T2.2, for
the same reason PLAN.md moved profile loading into M1: the verifier's limit checks cannot run
without it.

Two distinctions this module exists to keep straight:

- **Programmed vs machine coordinates.** Programmed coordinates are in the active work coordinate
  system; machine coordinates are those plus the work offset. `walk` tracks *programmed* values,
  and `machine_value` converts, reporting whether the offset was actually known. An unknown offset
  weakens the claim rather than being silently treated as zero.
- **Unknown vs zero.** At program start the machine could be anywhere, so every axis starts `None`.
  A check that cannot establish a position must not judge it.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

from foursight.machine.profile import MachineProfile
from foursight.parser.model import AXIS_LETTERS, Command

# G53 makes a block's coordinates machine-absolute for that block only.
_MACHINE_COORDS = "53"


@dataclass(slots=True, frozen=True)
class Position:
    """Programmed position, one field per axis. ``None`` means "not yet established"."""

    x: float | None = None
    y: float | None = None
    z: float | None = None
    a: float | None = None

    def get(self, letter: str) -> float | None:
        return getattr(self, letter.lower(), None)


_FIELD_OF = {"X": "x", "Y": "y", "Z": "z", "A": "a"}


def walk(commands: Sequence[Command]) -> Iterator[tuple[Command, Position, Position]]:
    """Yield ``(command, before, after)`` for each command, honouring G90/G91.

    Under G91 an axis whose current value is unknown stays unknown: an increment from nowhere is
    still nowhere.
    """
    current = Position()
    for command in commands:
        after = _advance(current, command)
        yield command, current, after
        current = after


def _advance(current: Position, command: Command) -> Position:
    moved = {
        _FIELD_OF[letter]: value
        for letter, value in command.words.items()
        if letter in AXIS_LETTERS
    }
    if not moved:
        return current
    incremental = command.modal_snapshot.distance == "91" and _MACHINE_COORDS not in command.gcodes
    if not incremental:
        return replace(current, **moved)

    updated = {}
    for field, delta in moved.items():
        base = getattr(current, field)
        updated[field] = None if base is None else base + delta
    return replace(current, **updated)


def is_machine_absolute(command: Command) -> bool:
    """True when this block's coordinates are already machine coordinates (G53)."""
    return _MACHINE_COORDS in command.gcodes


def machine_value(
    programmed: float | None, letter: str, command: Command, profile: MachineProfile
) -> tuple[float | None, bool]:
    """Convert a programmed coordinate to machine coordinates.

    Returns ``(value, offset_known)``. When the active work offset was never configured the
    programmed value is returned with ``offset_known=False`` — the caller then weakens its claim
    rather than asserting a machine position it cannot know. Work offsets live in the controller,
    not the G-code file, so "unknown" is the normal case, not an error.
    """
    if programmed is None:
        return None, True
    if is_machine_absolute(command):
        return programmed, True
    offset = profile.offset(command.modal_snapshot.offset)
    if offset is None:
        return programmed, False
    return programmed + getattr(offset, letter.lower()), True


# =============================================================================================
# MachineState (T2.2) — the stepper the simulator drives.
#
# The walker above answers "where does this block end?" for the verifier. The simulator needs more:
# machine coordinates rather than programmed ones, the moves a block performs rather than just its
# endpoint, dwell time for the timeline, and an honest signal when a construct cannot be drawn.
#
# Two constructs PLAN.md lists as interpreted turn out not to be computable from the data available,
# and each is handled differently on purpose:
#
# - **G28/G30 reference return.** The reference point is machine-specific and appears nowhere in the
#   G-code. Without `[axes.*].home` in the profile the target is unknown, so the move is reported as
#   undrawable rather than drawn to a guessed point. It is one rapid; suppressing it is proportionate.
# - **G43/G44 tool length.** There is no tool table (PLAN.md: tool changes are "position tracking
#   only, no geometry in v1"), so the H offset's length is unknown. Here suppression would be the
#   wrong trade: G43 appears in nearly every real program, and refusing to draw them all would make
#   the previewer useless. The offset shifts the Z datum uniformly without changing the path's
#   *shape*, so the path is drawn and `tool_length_unmodelled` records that Z is relative to the
#   spindle rather than the tool tip.
# =============================================================================================

_REFERENCE_RETURN = frozenset({"28", "30"})
_DWELL = "4"
_TOOL_LENGTH_ON = frozenset({"43", "44"})
_TOOL_LENGTH_OFF = "49"
_RAPID_MOTIONS = frozenset({"0"})
_FEED_MOTIONS = frozenset({"1", "2", "3"})


@dataclass(slots=True, frozen=True)
class Move:
    """One straight traverse in **machine** coordinates.

    An arc block still yields a single ``Move`` describing its endpoints; turning that into a
    tessellated polyline is ``sim/interpolate.py``'s job (T2.3), and it reads the arc words from the
    command itself.
    """

    start: Position
    end: Position
    rapid: bool
    offset_known: bool = True  # False when the work offset was assumed to be zero


@dataclass(slots=True, frozen=True)
class Step:
    """Everything one block does.

    ``moves`` is usually one entry, empty for a block that only sets modes, and **two** for a
    ``G28 X0 Y0``, which traverses to the intermediate point and then to the reference point.
    """

    command: Command
    moves: tuple[Move, ...] = ()
    dwell: float = 0.0  # seconds; G4 P is seconds in LinuxCNC
    undrawable: str | None = None  # why this block's geometry cannot be produced
    tool_length_unmodelled: bool = False

    @property
    def moved(self) -> bool:
        return bool(self.moves)


class MachineState:
    """Live position and modal context while stepping through a program.

    Holds *both* coordinate frames: ``programmed`` is what the G-code says, ``machine`` is that plus
    the active work offset. `SegmentStore.lin` is machine coordinates, so the simulator needs the
    latter — but arc geometry and incremental moves are computed in the former, and conflating them
    is how a work offset ends up applied twice.
    """

    __slots__ = ("_profile", "active_h", "programmed")

    def __init__(self, profile: MachineProfile) -> None:
        self._profile = profile
        self.programmed = Position()
        self.active_h: int | None = None

    @property
    def profile(self) -> MachineProfile:
        return self._profile

    def machine_position(self, command: Command) -> tuple[Position, bool]:
        """Current position in machine coordinates, and whether the offset was known."""
        return self._to_machine(self.programmed, command)

    def apply(self, command: Command) -> Step:
        """Advance the state by one block and describe what it did."""
        self._track_tool_length(command)

        if _DWELL in command.gcodes:
            return Step(command=command, dwell=self._dwell_seconds(command))
        if any(code in _REFERENCE_RETURN for code in command.gcodes):
            return self._reference_return(command)
        return self._ordinary_move(command)

    # ---------------------------------------------------------------- individual constructs

    def _ordinary_move(self, command: Command) -> Step:
        before = self.programmed
        after = _advance(before, command)
        self.programmed = after
        if after == before or command.motion is None:
            return Step(command=command, tool_length_unmodelled=self.active_h is not None)

        start, start_known = self._to_machine(before, command)
        end, end_known = self._to_machine(after, command)
        return Step(
            command=command,
            moves=(
                Move(
                    start=start,
                    end=end,
                    rapid=command.motion in _RAPID_MOTIONS,
                    offset_known=start_known and end_known,
                ),
            ),
            tool_length_unmodelled=self.active_h is not None,
        )

    def _reference_return(self, command: Command) -> Step:
        """G28/G30: optionally traverse to an intermediate point, then to the reference point.

        Both legs are rapids. If any axis the machine would return along has no configured `home`,
        nothing is drawn: a reference move to a guessed target is exactly the confidently-wrong
        output the plan forbids.
        """
        before = self.programmed
        intermediate = _advance(before, command)
        home = self._home_position()
        if home is None:
            # Position is genuinely unknown afterwards, so do not pretend to track it.
            self.programmed = Position()
            return Step(
                command=command,
                undrawable=(
                    "G28/G30 reference return: no [axes.*].home in the profile, so the reference "
                    "point is unknown and the move is not drawn"
                ),
            )

        legs: list[Move] = []
        via, via_known = self._to_machine(intermediate, command)
        start, start_known = self._to_machine(before, command)
        if intermediate != before:
            legs.append(
                Move(start=start, end=via, rapid=True, offset_known=start_known and via_known)
            )
            start, start_known = via, via_known
        # The reference point is a machine position: no work offset applies to it.
        legs.append(Move(start=start, end=home, rapid=True, offset_known=start_known))
        self.programmed = self._home_as_programmed(home, command)
        return Step(
            command=command,
            moves=tuple(legs),
            tool_length_unmodelled=self.active_h is not None,
        )

    def _dwell_seconds(self, command: Command) -> float:
        """G4 P in **seconds** (LinuxCNC). Fanuc uses milliseconds; the divergence is documented.

        The ms/s confusion warning (P > 60) is the verifier's call, not this module's — here the
        value is simply carried through to the timeline.
        """
        return float(command.words.get("P", 0.0))

    def _track_tool_length(self, command: Command) -> None:
        for code in command.gcodes:
            if code == _TOOL_LENGTH_OFF:
                self.active_h = None
            elif code in _TOOL_LENGTH_ON:
                h_word = command.words.get("H")
                if h_word is not None:
                    self.active_h = int(h_word)
                elif self.active_h is None:
                    # G43 with no H and none active: LinuxCNC uses the current tool's offset, which
                    # we cannot know either. Record that an offset is in force with an unknown value.
                    self.active_h = 0

    # ---------------------------------------------------------------- coordinate frames

    def _to_machine(self, position: Position, command: Command) -> tuple[Position, bool]:
        values: dict[str, float | None] = {}
        known = True
        for letter, field in _FIELD_OF.items():
            value, offset_known = machine_value(
                position.get(letter), letter, command, self._profile
            )
            values[field] = value
            known = known and offset_known
        return Position(**values), known

    def _home_position(self) -> Position | None:
        """The reference point in machine coordinates, or None when any axis lacks a `home`."""
        values: dict[str, float] = {}
        for letter, field in _FIELD_OF.items():
            axis = self._profile.axes.get(letter)
            if axis is None or axis.home is None:
                return None
            values[field] = axis.home
        return Position(**values)

    def _home_as_programmed(self, home: Position, command: Command) -> Position:
        """Convert the reference point back to programmed coordinates for continued tracking."""
        offset = self._profile.offset(command.modal_snapshot.offset)
        if offset is None:
            return home
        return Position(
            x=None if home.x is None else home.x - offset.x,
            y=None if home.y is None else home.y - offset.y,
            z=None if home.z is None else home.z - offset.z,
            a=None if home.a is None else home.a - offset.a,
        )
