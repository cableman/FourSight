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
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path

from foursight.parser.dialect import (
    ARC_CENTRE_CODES,
    PRESETS,
    Dialect,
    DialectName,
    DwellUnits,
    preset,
)
from foursight.parser.model import ROTARY_LETTERS

INCH_TO_MM = 25.4

DEFAULT_PROFILE_NAME = "default_4axis.toml"


def default_profile_path() -> Path:
    """Path to the profile shipped inside the package.

    Resolved through ``importlib.resources`` rather than relative to the source tree, because the
    only copy that exists at runtime is the packaged one — a path relative to the repository root
    works in a checkout and fails for both `pip install` and a PyInstaller bundle.
    """
    return Path(str(resources.files("foursight") / "profiles" / DEFAULT_PROFILE_NAME))


# Rotary axes beyond A are not v1, but naming them here means an unlabelled `[axes.b]` is treated
# as rotary rather than having its degrees scaled by 25.4 on an inch profile. Guessing "rotary" is
# recoverable; guessing "linear" corrupts the values.
_ROTARY_NAMES = frozenset(ROTARY_LETTERS | {"B", "C"})

_DEFAULT_ARC_RADIUS_MISMATCH = 0.005
_DEFAULT_ARC_CHORD = 0.01
_DEFAULT_ROTARY_CHORD = 0.01
_DEFAULT_ROTARY_WRAP_WARN = 360.0

_SECTIONS = frozenset(
    {
        "machine",
        "limits",
        "tolerance",
        "axes",
        "offsets",
        "kinematics",
        "safety",
        "stock",
        "dialect",
    }
)
_KEYS: dict[str, frozenset[str]] = {
    "machine": frozenset({"name", "units"}),
    "dialect": frozenset({"name", "arc_centre", "dwell_units"}),
    "limits": frozenset({"max_feed", "max_spindle_rpm", "max_plunge_feed", "rotary_wrap_warn"}),
    "stock": frozenset({"shape", "min", "max", "diameter", "length", "axis_min"}),
    "tolerance": frozenset({"arc_radius_mismatch", "arc_chord", "rotary_chord"}),
    "kinematics": frozenset({"rotary_mount", "rotary_axis", "centerline_offset", "pivot_to_tip"}),
    "safety": frozenset(
        {"min_clearance_z", "require_spindle_before_cut", "retract_before_toolchange"}
    ),
}
_AXIS_KEYS = frozenset({"type", "min", "max", "max_rapid", "wrap", "home"})


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
    # Machine position this axis returns to under G28. Absent means unknown, and a G28 move is
    # then not drawn at all rather than drawn to a guessed point — the reference point is
    # machine-specific and appears nowhere in the G-code.
    home: float | None = None


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
    # mm/min above which a straight-down G1 is suspicious. None by default and absent from the
    # shipped profile: a sane plunge rate is a property of the tool and the material, not of the
    # machine, so there is no generic number to invent here.
    max_plunge_feed: float | None = None
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


# Neither stock shape is a material-removal model. Both say where the solid *started*, never what is
# left of it, which is why the rule reading them can only warn (PLAN.md § Stock and plunge checks).
# Both are in **machine** coordinates, matching `[axes]` and `[offsets]` and the frame verification
# runs in. With `g54 = [0, 0, 0, 0]` the two frames coincide, which is the common case, but nothing
# here may depend on that.


@dataclass(slots=True, frozen=True)
class StockBox:
    """A rectangular blank, as an axis-aligned box. The 3-axis case."""

    min: tuple[float, float, float]
    max: tuple[float, float, float]


@dataclass(slots=True, frozen=True)
class StockCylinder:
    """A round blank on the rotary axis. The 4-axis case, and the reason it is worth a second shape.

    **Its axis is not stated here** — it is `[kinematics].rotary_axis` through
    `[kinematics].centerline_offset`, and that is deliberate rather than a shortcut. A cylinder centred
    on the rotary axis maps onto *itself* under any A rotation, so the interference check stays exact at
    every angle; an off-axis cylinder would not, and offering the option would invite a profile whose
    check is silently wrong the moment the part turns. Concentric is the only case that can be right,
    so it is the only case that can be expressed.

    ``axis_min`` is the machine coordinate, along the rotary axis, of the end of the blank nearer that
    axis's minimum — for `rotary_axis = "x"`, the smaller machine X of the two ends.
    """

    diameter: float
    length: float
    axis_min: float

    @property
    def radius(self) -> float:
        return self.diameter / 2.0

    @property
    def axis_max(self) -> float:
        return self.axis_min + self.length


#: Either stock shape. Kept as a union rather than one dataclass with a discriminator so that a rule
#: reading `stock.min` cannot compile against a cylinder.
StockEnvelope = StockBox | StockCylinder


@dataclass(slots=True, frozen=True)
class DialectSettings:
    """Which controller this profile describes, plus the settings a program cannot state.

    ``arc_centre`` and ``dwell_units`` are *controller configuration*: they appear nowhere in the
    G-code, so a file cannot tell us and we cannot infer them. They are only meaningful for a
    dialect that treats them as settings — under LinuxCNC the G-code decides (G90.1/G91.1) and G4 P
    is seconds by specification — so the loader refuses them there rather than ignoring them, which
    would let a user believe they had configured something.
    """

    name: str = DialectName.LINUXCNC
    arc_centre: str = "incremental"  # 'incremental' → G91.1, 'absolute' → G90.1
    dwell_units: str = DwellUnits.SECONDS

    def as_parser_dialect(self) -> Dialect:
        """The parse-layer value for these settings.

        Built by `replace`-ing the **preset**, never by constructing a bare `Dialect`: the preset
        carries the dialect's code tables (`unit_aliases`, `cycle_cancel_conflicts`) as well as its
        defaults, and listing fields here would silently drop every one this method forgot — so a
        Mach3 profile would parse with LinuxCNC's code table while calling itself Mach3.
        """
        return replace(
            preset(self.name),
            arc_distance=ARC_CENTRE_CODES[self.arc_centre],
            dwell_units=self.dwell_units,
        )


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
    # None means "no stock declared", which disables the interference check rather than assuming a
    # box. A guessed envelope would report confidently on a solid that is not on the table.
    stock: StockEnvelope | None = None
    dialect: DialectSettings = field(default_factory=DialectSettings)
    # Reported rather than raised, so a newer profile still loads — but the CLI must surface these:
    # `max_fed = 3000` is a typo that would otherwise silently disable the feed check.
    unknown_keys: tuple[str, ...] = ()
    path: Path | None = None

    @property
    def parser_dialect(self) -> Dialect:
        """The value to hand `parse(..., dialect=...)`.

        The one bridge from the machine layer to the parse layer, and the only place the two
        vocabularies meet. Derived rather than stored anywhere downstream: a `Program` or a
        `FixContext` that kept its own copy could disagree with the profile travelling beside it,
        and that disagreement surfaces as arithmetically wrong I/J in an applied fix.
        """
        return self.dialect.as_parser_dialect()

    def offset(self, code: str | None) -> WorkOffset | None:
        """The offset for a `ModalState.offset` code ('54'), or None when it was never configured.

        None is the meaningful answer, not an error: it is what downgrades travel-limit violations
        to warnings.
        """
        return self.offsets.get(code) if code else None


def load_profile(path: str | Path) -> MachineProfile:
    """Read and parse a profile. ``OSError`` propagates for a missing file.

    Malformed TOML becomes a `ProfileError`, exactly as in `load_profile_text`. This wrapping was
    missing here at first, and only the text variant had it — so a hand-edited profile with a typo
    reached `foursight check --profile` as a raw `tomllib.TOMLDecodeError` traceback, in spite of the
    CLI documenting exit code 2 for an unusable profile. The path variant is the one users actually
    reach, which is precisely why it needed it more.
    """
    resolved = Path(path)
    with resolved.open("rb") as handle:
        try:
            data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"{resolved}: invalid TOML: {exc}") from exc
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
        stock=_stock(_section(data, "stock", unknown), scale, present="stock" in data),
        dialect=_dialect(_section(data, "dialect", unknown)),
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
        max_plunge_feed=_scaled(section.get("max_plunge_feed"), scale),
        rotary_wrap_warn=_or_default(
            section.get("rotary_wrap_warn"), 1.0, _DEFAULT_ROTARY_WRAP_WARN
        ),
    )


#: The keys that belong to each `[stock].shape`. A key from the wrong list is refused rather than
#: ignored: `diameter` under a box would look configured and do nothing.
_BOX_KEYS = ("min", "max")
_CYLINDER_KEYS = ("diameter", "length", "axis_min")
_STOCK_SHAPES = ("box", "cylinder")


def _stock(section: dict, scale: float, *, present: bool) -> StockEnvelope | None:
    """Build `[stock]`, refusing anything half-specified or belonging to the other shape.

    Every key of the chosen shape is mandatory, and none is completed from the others. Defaulting an
    absent box bound to ±infinity would declare the whole machine envelope to be stock; defaulting it
    to zero would declare a solid nothing can intersect. Both leave the user believing they had
    configured a check that is in fact reporting on a different solid, so a partial section raises.

    ``shape`` may be omitted and is then **inferred from the keys present**, so a `[stock]` written
    before cylinders existed still means what it did. Stating it explicitly is checked against the keys
    rather than trusted, because the two disagreeing is how a cylinder gets read as a box.

    ``present`` distinguishes an absent section from an empty one, which `_section` cannot: no
    `[stock]` at all means "no stock declared" and disables the check, while an empty `[stock]` header
    is the same trap as a half-specified one — written deliberately, and silently doing nothing.
    """
    if not present:
        return None
    shape = _stock_shape(section)
    if shape == "cylinder":
        _refuse_foreign_keys(section, _BOX_KEYS, shape)
        return _stock_cylinder(_required(section, _CYLINDER_KEYS, shape), scale)
    _refuse_foreign_keys(section, _CYLINDER_KEYS, shape)
    return _stock_box(_required(section, _BOX_KEYS, shape), scale)


def _stock_shape(section: dict) -> str:
    declared = section.get("shape")
    if declared is None:
        return "cylinder" if any(key in section for key in _CYLINDER_KEYS) else "box"
    shape = str(declared).lower()
    if shape not in _STOCK_SHAPES:
        raise ProfileError(
            f"[stock].shape must be one of {', '.join(_STOCK_SHAPES)}, got {shape!r}"
        )
    return shape


def _refuse_foreign_keys(section: dict, foreign: tuple[str, ...], shape: str) -> None:
    present = [key for key in foreign if key in section]
    if present:
        raise ProfileError(
            f"[stock] has {', '.join(present)}, which {shape} stock does not use. A key from the "
            f"other shape looks configured and does nothing"
        )


def _required(section: dict, keys: tuple[str, ...], shape: str) -> dict:
    missing = [key for key in keys if key not in section]
    if missing:
        raise ProfileError(
            f"[stock] with shape '{shape}' needs {', '.join(keys)}; {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} absent. Completing one by a guess would make the "
            f"check report on a different solid"
        )
    return section


def _stock_box(section: dict, scale: float) -> StockBox:
    bounds = {}
    for key in _BOX_KEYS:
        value = section[key]
        if not isinstance(value, list) or len(value) != 3:
            raise ProfileError(f"[stock].{key} must be a list of 3 numbers [x, y, z]")
        bounds[key] = tuple(_number(component) * scale for component in value)
    for axis, name in enumerate("xyz"):
        if bounds["min"][axis] > bounds["max"][axis]:
            raise ProfileError(
                f"[stock] min {name} {bounds['min'][axis]} exceeds max {bounds['max'][axis]}"
            )
    return StockBox(min=bounds["min"], max=bounds["max"])


def _stock_cylinder(section: dict, scale: float) -> StockCylinder:
    """All three keys are lengths along or across the rotary axis, so all three scale.

    A zero or negative diameter or length is refused rather than clamped: it describes no solid, and a
    check against nothing would pass every program while looking configured.
    """
    values = {key: _number(section[key]) * scale for key in _CYLINDER_KEYS}
    for key in ("diameter", "length"):
        if values[key] <= 0.0:
            raise ProfileError(f"[stock].{key} must be greater than zero, got {values[key]:g}")
    return StockCylinder(
        diameter=values["diameter"], length=values["length"], axis_min=values["axis_min"]
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
            home=_scaled(body.get("home"), axis_scale),
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


#: `[dialect]` keys that describe a *controller setting* rather than the controller's identity.
_CONTROLLER_SETTINGS = ("arc_centre", "dwell_units")


def _dialect(section: dict) -> DialectSettings:
    """Build `[dialect]`, refusing a setting stated where it means nothing.

    The presence checks have to live here rather than in `_validate`: once the section is a
    `DialectSettings`, its defaults have erased the difference between "absent" and "explicitly set
    to the default", and only the raw table still knows.
    """
    name = str(section.get("name", DialectName.LINUXCNC)).lower()
    if name not in PRESETS:
        raise ProfileError(f"[dialect].name must be one of {', '.join(PRESETS)}, got {name!r}")
    if name == DialectName.LINUXCNC:
        for key in _CONTROLLER_SETTINGS:
            if key in section:
                raise ProfileError(
                    f"[dialect].{key} is meaningful only where it is a controller setting; under "
                    f"'linuxcnc' the G-code decides (G90.1/G91.1) and G4 P is seconds by "
                    f'specification. Remove the key, or set [dialect].name = "mach3".'
                )

    arc_centre = str(section.get("arc_centre", "incremental")).lower()
    if arc_centre not in ARC_CENTRE_CODES:
        raise ProfileError(
            f"[dialect].arc_centre must be one of {', '.join(ARC_CENTRE_CODES)}, got {arc_centre!r}"
        )
    dwell_units = str(section.get("dwell_units", DwellUnits.SECONDS)).lower()
    if dwell_units not in set(DwellUnits):
        raise ProfileError(
            f"[dialect].dwell_units must be 'seconds' or 'milliseconds', got {dwell_units!r}"
        )
    return DialectSettings(name=name, arc_centre=arc_centre, dwell_units=dwell_units)


def with_dialect(profile: MachineProfile, name: str | None) -> MachineProfile:
    """Apply a CLI dialect override, producing the *effective* profile.

    Resolved once, at the CLI/GUI boundary, so that every later stage reads the dialect off the
    profile it already carries and there is exactly one source of truth. Precedence is
    ``--dialect`` > ``[dialect].name`` > ``linuxcnc``.

    Overriding to the dialect the profile already names is a no-op, so the profile's controller
    settings survive. Overriding to a *different* dialect resets them, because settings tuned for
    one controller describe nothing about another — and under LinuxCNC they are not settings at all.
    """
    if name is None or name == profile.dialect.name:
        return profile
    if name not in PRESETS:
        raise ProfileError(f"unknown dialect {name!r}; expected one of {', '.join(PRESETS)}")
    return replace(profile, dialect=DialectSettings(name=name))


def with_arc_centre(profile: MachineProfile, arc_centre: str | None) -> MachineProfile:
    """Apply a CLI arc-centre override.

    Refused under a dialect where the arc centre is not a controller setting, for the same reason
    the loader refuses the key there: silently accepting it would let a user believe they had
    overridden something the G-code actually decides.
    """
    if arc_centre is None:
        return profile
    if arc_centre not in ARC_CENTRE_CODES:
        raise ProfileError(
            f"--arc-centre must be one of {', '.join(ARC_CENTRE_CODES)}, got {arc_centre!r}"
        )
    if profile.dialect.name == DialectName.LINUXCNC:
        raise ProfileError(
            "--arc-centre applies only where the arc centre is a controller setting; under "
            "'linuxcnc' the G-code decides it (G90.1/G91.1). Add --dialect mach3, or state "
            "G90.1/G91.1 in the program."
        )
    return replace(profile, dialect=replace(profile.dialect, arc_centre=arc_centre))


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
