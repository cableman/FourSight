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
