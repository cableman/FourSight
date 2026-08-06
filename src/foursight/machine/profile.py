"""``MachineProfile`` loaded from TOML via ``tomllib``.

The governing rule of this module: **absence means "unknown", never a fabricated value.** PLAN.md
states it for work offsets — an unset offset downgrades travel-limit violations from errors to
warnings — and the same reasoning applies to every limit. Inventing a `max_feed` of 3000 for a
profile that never said so would produce confident diagnostics about a machine we know nothing
about. So anything whose absence must *disable* a check is ``| None``, and only the tolerances
carry real defaults, because tessellation cannot proceed without a number.

Two things that must not be got wrong:

- **An unset work offset is not a zero work offset.** ``[offsets]`` with only ``g54`` present leaves
  g55–g59 unset; ``g54 = [0, 0, 0, 0]`` is *set to zero*. Collapsing the two would silently turn
  "assumes zero offset" warnings into hard errors.
- **A work offset mixes millimetres and degrees.** ``g54 = [x, y, z, a]`` is three lengths and one
  angle, so an inch profile scales the first three and never the fourth.

Values are converted to mm on load using ``[machine].units``; ``declared_units`` records what the
file said, and every stored number is already mm (or degrees, for rotary).
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from foursight.parser.model import ROTARY_LETTERS

INCH_TO_MM = 25.4

# Rotary axes beyond A are not v1, but naming them here means an unlabelled `[axes.b]` is treated
# as rotary rather than having its degrees scaled by 25.4 on an inch profile. Guessing "rotary" is
# recoverable; guessing "linear" corrupts the values.
_ROTARY_NAMES = frozenset(ROTARY_LETTERS | {"B", "C"})

_DEFAULT_ARC_RADIUS_MISMATCH = 0.005
_DEFAULT_ARC_CHORD = 0.01
_DEFAULT_ROTARY_CHORD = 0.01
_DEFAULT_ROTARY_WRAP_WARN = 360.0

_SECTIONS = frozenset({"machine", "limits", "tolerance", "axes", "offsets", "kinematics", "safety"})
_KEYS: dict[str, frozenset[str]] = {
    "machine": frozenset({"name", "units"}),
    "limits": frozenset({"max_feed", "max_spindle_rpm", "rotary_wrap_warn"}),
    "tolerance": frozenset({"arc_radius_mismatch", "arc_chord", "rotary_chord"}),
    "kinematics": frozenset({"rotary_mount", "rotary_axis", "centerline_offset", "pivot_to_tip"}),
    "safety": frozenset(
        {"min_clearance_z", "require_spindle_before_cut", "retract_before_toolchange"}
    ),
}
_AXIS_KEYS = frozenset({"type", "min", "max", "max_rapid", "wrap"})


class ProfileError(Exception):
    """The profile cannot be used as given.

    Raised, not reported: a profile that cannot describe the machine cannot verify a program
    against it, and continuing would mean checking against something invented.
    """


@dataclass(slots=True, frozen=True)
class AxisLimits:
    """Travel and rate limits for one axis. mm and mm/min, or degrees and deg/min if rotary."""

    name: str  # 'X', 'A', ... upper-cased to match Command.words keys
    is_rotary: bool
    min: float | None = None
    max: float | None = None
    max_rapid: float | None = None
    wrap: bool = False  # rotary only: when True, min/max are not enforced


@dataclass(slots=True, frozen=True)
class WorkOffset:
    """A work offset in machine coordinates. Named fields, because ``[2]`` invites index bugs.

    ``x``/``y``/``z`` are mm; ``a`` is **degrees** and is never unit-converted.
    """

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    a: float = 0.0


@dataclass(slots=True, frozen=True)
class Tolerances:
    """The only section with real defaults: tessellation needs a number to work with."""

    arc_radius_mismatch: float = _DEFAULT_ARC_RADIUS_MISMATCH
    arc_chord: float = _DEFAULT_ARC_CHORD
    rotary_chord: float = _DEFAULT_ROTARY_CHORD


@dataclass(slots=True, frozen=True)
class Limits:
    max_feed: float | None = None  # mm/min; None disables the feed check
    max_spindle_rpm: float | None = None  # None disables the spindle check
    rotary_wrap_warn: float = _DEFAULT_ROTARY_WRAP_WARN  # degrees in one block before warning


@dataclass(slots=True, frozen=True)
class Kinematics:
    rotary_mount: str = "table"  # 'table' | 'head'
    rotary_axis: str = "x"
    centerline_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    pivot_to_tip: float | None = None  # required for head mount


@dataclass(slots=True, frozen=True)
class Safety:
    min_clearance_z: float | None = None  # None disables the rapid-clearance warning
    require_spindle_before_cut: bool = True
    retract_before_toolchange: bool = True


@dataclass(slots=True, frozen=True)
class MachineProfile:
    """A machine description. Every length is mm; rotary values stay in degrees."""

    name: str = "unnamed"
    declared_units: str = "mm"  # what the FILE used; stored values are already converted
    limits: Limits = field(default_factory=Limits)
    tolerance: Tolerances = field(default_factory=Tolerances)
    axes: dict[str, AxisLimits] = field(default_factory=dict)  # keyed 'X', 'Y', 'Z', 'A'
    offsets: dict[str, WorkOffset] = field(default_factory=dict)  # keyed '54'..'59'
    kinematics: Kinematics = field(default_factory=Kinematics)
    safety: Safety = field(default_factory=Safety)
    # Reported rather than raised, so a newer profile still loads — but the CLI must surface these:
    # `max_fed = 3000` is a typo that would otherwise silently disable the feed check.
    unknown_keys: tuple[str, ...] = ()
    path: Path | None = None

    def offset(self, code: str | None) -> WorkOffset | None:
        """The offset for a `ModalState.offset` code ('54'), or None when it was never configured.

        None is the meaningful answer, not an error: it is what downgrades travel-limit violations
        to warnings.
        """
        return self.offsets.get(code) if code else None


def load_profile(path: str | Path) -> MachineProfile:
    """Read and parse a profile. ``OSError`` propagates for a missing file."""
    resolved = Path(path)
    with resolved.open("rb") as handle:
        data = tomllib.load(handle)
    return _build(data, path=resolved)


def load_profile_text(text: str, *, path: Path | None = None) -> MachineProfile:
    """Parse a profile from a TOML string, so the rules are testable without a filesystem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"invalid TOML: {exc}") from exc
    return _build(data, path=path)


def _build(data: dict, *, path: Path | None) -> MachineProfile:
    unknown: list[str] = []
    machine = _section(data, "machine", unknown)
    declared = str(machine.get("units", "mm")).lower()
    if declared not in {"mm", "inch"}:
        raise ProfileError(f"[machine].units must be 'mm' or 'inch', got {declared!r}")
    scale = INCH_TO_MM if declared == "inch" else 1.0

    for key in data:
        if key not in _SECTIONS:
            unknown.append(key)

    kinematics = _kinematics(_section(data, "kinematics", unknown), scale)
    profile = MachineProfile(
        name=str(machine.get("name", "unnamed")),
        declared_units=declared,
        limits=_limits(_section(data, "limits", unknown), scale),
        tolerance=_tolerances(_section(data, "tolerance", unknown), scale),
        axes=_axes(data.get("axes", {}), scale, unknown),
        offsets=_offsets(data.get("offsets", {}), scale, unknown),
        kinematics=kinematics,
        safety=_safety(_section(data, "safety", unknown), scale),
        unknown_keys=tuple(unknown),
        path=path,
    )
    _validate(profile)
    return profile


def _section(data: dict, name: str, unknown: list[str]) -> dict:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ProfileError(f"[{name}] must be a table")
    for key in section:
        if key not in _KEYS.get(name, frozenset()):
            unknown.append(f"{name}.{key}")
    return section


def _limits(section: dict, scale: float) -> Limits:
    return Limits(
        max_feed=_scaled(section.get("max_feed"), scale),
        max_spindle_rpm=_scaled(section.get("max_spindle_rpm"), 1.0),  # rpm is not a length
        rotary_wrap_warn=_or_default(
            section.get("rotary_wrap_warn"), 1.0, _DEFAULT_ROTARY_WRAP_WARN
        ),
    )


def _tolerances(section: dict, scale: float) -> Tolerances:
    return Tolerances(
        arc_radius_mismatch=_or_default(
            section.get("arc_radius_mismatch"), scale, _DEFAULT_ARC_RADIUS_MISMATCH
        ),
        arc_chord=_or_default(section.get("arc_chord"), scale, _DEFAULT_ARC_CHORD),
        # Despite the name, rotary_chord is a chord *height* in mm, measured at the path's maximum
        # radius from the centerline — so it does scale on an inch profile.
        rotary_chord=_or_default(section.get("rotary_chord"), scale, _DEFAULT_ROTARY_CHORD),
    )


def _axes(section: dict, scale: float, unknown: list[str]) -> dict[str, AxisLimits]:
    axes: dict[str, AxisLimits] = {}
    for raw_name, body in section.items():
        name = str(raw_name).upper()
        if not isinstance(body, dict):
            raise ProfileError(f"[axes.{raw_name}] must be a table")
        for key in body:
            if key not in _AXIS_KEYS:
                unknown.append(f"axes.{raw_name}.{key}")
        is_rotary = body.get("type") == "rotary" or name in _ROTARY_NAMES
        # A rotary axis is in degrees end to end: travel AND rate. Scaling either would corrupt it.
        axis_scale = 1.0 if is_rotary else scale
        axes[name] = AxisLimits(
            name=name,
            is_rotary=is_rotary,
            min=_scaled(body.get("min"), axis_scale),
            max=_scaled(body.get("max"), axis_scale),
            max_rapid=_scaled(body.get("max_rapid"), axis_scale),
            wrap=bool(body.get("wrap", False)),
        )
    return axes


def _offsets(section: dict, scale: float, unknown: list[str]) -> dict[str, WorkOffset]:
    offsets: dict[str, WorkOffset] = {}
    for raw_key, value in section.items():
        key = str(raw_key).lower()
        if not (key.startswith("g") and key[1:] in {"54", "55", "56", "57", "58", "59"}):
            unknown.append(f"offsets.{raw_key}")
            continue
        if not isinstance(value, list) or len(value) not in {3, 4}:
            raise ProfileError(f"[offsets].{raw_key} must be a list of 3 or 4 numbers")
        x, y, z = (_number(component) * scale for component in value[:3])
        # value[3] is A in degrees and is deliberately NOT scaled.
        a = _number(value[3]) if len(value) == 4 else 0.0
        offsets[key[1:]] = WorkOffset(x=x, y=y, z=z, a=a)
    return offsets


def _kinematics(section: dict, scale: float) -> Kinematics:
    centerline = section.get("centerline_offset", [0.0, 0.0, 0.0])
    if not isinstance(centerline, list) or len(centerline) != 3:
        raise ProfileError("[kinematics].centerline_offset must be a list of 3 numbers")
    return Kinematics(
        rotary_mount=str(section.get("rotary_mount", "table")).lower(),
        rotary_axis=str(section.get("rotary_axis", "x")).lower(),
        centerline_offset=tuple(_number(component) * scale for component in centerline),
        pivot_to_tip=_scaled(section.get("pivot_to_tip"), scale),
    )


def _safety(section: dict, scale: float) -> Safety:
    return Safety(
        min_clearance_z=_scaled(section.get("min_clearance_z"), scale),
        require_spindle_before_cut=bool(section.get("require_spindle_before_cut", True)),
        retract_before_toolchange=bool(section.get("retract_before_toolchange", True)),
    )


def _validate(profile: MachineProfile) -> None:
    mount = profile.kinematics.rotary_mount
    if mount not in {"table", "head"}:
        raise ProfileError(f"[kinematics].rotary_mount must be 'table' or 'head', got {mount!r}")
    if mount == "head" and profile.kinematics.pivot_to_tip is None:
        # The tool tip translates as the head swings, so without this distance the tip path is
        # simply unknowable. Refusing at load time beats rendering a confidently wrong toolpath.
        raise ProfileError(
            "[kinematics].pivot_to_tip is required when rotary_mount = 'head': the tool tip "
            "translates as the head swings, so the tip path cannot be computed without it"
        )
    if profile.kinematics.rotary_axis not in {"x", "y", "z"}:
        raise ProfileError(
            f"[kinematics].rotary_axis must be 'x', 'y' or 'z', got {profile.kinematics.rotary_axis!r}"
        )
    for axis in profile.axes.values():
        if axis.min is not None and axis.max is not None and axis.min > axis.max:
            raise ProfileError(f"[axes.{axis.name.lower()}] min {axis.min} exceeds max {axis.max}")


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"expected a number, got {value!r}")
    return float(value)


def _scaled(value: object, scale: float) -> float | None:
    return None if value is None else _number(value) * scale


def _or_default(value: object, scale: float, default: float) -> float:
    """Substitute the default only when the key is *absent*.

    `value or default` would be shorter and wrong: a legitimate 0.0 is falsy, so
    `rotary_wrap_warn = 0` ("warn on any rotary move at all") would silently become 360.
    """
    return default if value is None else _number(value) * scale
