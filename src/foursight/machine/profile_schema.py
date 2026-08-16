"""What the profile editor shows, as data rather than as widget code.

The GUI form is generated from this table, so adding a profile key means adding a row here and not
writing a widget. That matters more than it sounds: the alternative — a hand-built form — drifts from
the loader the first time a key is added and nobody notices, because a *missing* field in a settings
dialog looks exactly like a field that does not exist.
`tests/test_profile_doc.py` asserts this table and `machine/profile.py`'s key tables describe the
same set of keys, so the drift is a test failure rather than a silently uneditable field.

Three things the schema has to carry that a naive "label plus line edit" would lose:

- **Optional versus required.** PLAN.md's governing profile rule is that absence means *unknown* and
  disables a check, so `max_feed = 0` and no `max_feed` at all are different programs. An optional
  field therefore gets a "set" toggle, and clearing it writes nothing rather than writing zero.
- **Units are the file's, not the internal mm.** A field's unit label comes from `[machine].units`, and
  the value shown is the one the file wrote. Rotary fields are degrees in every unit mode and say so.
- **Some keys are meaningful only under some other setting.** `[dialect].arc_centre` and `dwell_units`
  are refused outright under LinuxCNC, and `pivot_to_tip` is required only for a head mount. A form
  that offered them unconditionally would let a user set something the loader then rejects.
"""

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """How a field is edited. The form maps each of these to exactly one widget."""

    TEXT = "text"
    CHOICE = "choice"
    BOOL = "bool"
    LENGTH = "length"  # scales with [machine].units
    RATE = "rate"  # a length per minute; scales too
    ANGLE = "angle"  # degrees, in every unit mode
    ANGLE_RATE = "angle_rate"  # degrees per minute
    COUNT = "count"  # a bare number: rpm, a scale factor
    VEC3 = "vec3"  # three lengths
    OFFSET = "offset"  # three lengths and one angle — the mixed-units case


#: Unit suffixes that do **not** depend on `[machine].units`.
FIXED_UNITS: dict[Kind, str] = {
    Kind.ANGLE: "deg",
    Kind.ANGLE_RATE: "deg/min",
}

#: Kinds whose numbers are lengths in the file's declared units.
SCALED: frozenset[Kind] = frozenset({Kind.LENGTH, Kind.RATE, Kind.VEC3, Kind.OFFSET})


@dataclass(frozen=True, slots=True)
class Field:
    """One editable key.

    ``requires`` gates the field on another field's value, as ``(section, key, allowed values)``. It is
    evaluated against the document being edited, so the form can grey out a key that would be refused
    rather than letting the user set it and meet a `ProfileError` on Apply.
    """

    section: str
    key: str
    label: str
    kind: Kind
    optional: bool = True
    choices: tuple[str, ...] = ()
    default: object = None  # what the loader uses when the key is absent; shown as a placeholder
    help: str = ""
    requires: tuple[str, str, tuple[str, ...]] | None = None


@dataclass(frozen=True, slots=True)
class Group:
    """A section of the form.

    ``toggle`` marks a section that is switched on and off as a unit rather than key by key — `[stock]`,
    whose keys are mandatory together, so leaving the header behind with all of them commented out
    would produce the present-but-empty section the loader refuses.
    """

    section: str
    title: str
    fields: tuple[Field, ...]
    help: str = ""
    toggle: bool = False


_MACH3_ONLY = ("dialect", "name", ("mach3",))


def _axis(name: str, title: str, *, rotary: bool = False) -> Group:
    """One `[axes.x]` group. Rotary travel and rates are degrees end to end, never scaled."""
    travel = Kind.ANGLE if rotary else Kind.LENGTH
    rate = Kind.ANGLE_RATE if rotary else Kind.RATE
    section = f"axes.{name}"
    fields = [
        Field(section, "min", "Minimum travel", travel),
        Field(section, "max", "Maximum travel", travel),
        Field(section, "max_rapid", "Rapid rate", rate),
        Field(
            section,
            "home",
            "G28 reference",
            travel,
            help=(
                "Where this axis goes under G28/G30. Unset means a G28 is not drawn at all, rather "
                "than drawn to a guessed point."
            ),
        ),
    ]
    if rotary:
        fields.insert(
            0,
            Field(
                section,
                "wrap",
                "Wraps continuously",
                Kind.BOOL,
                optional=False,
                default=False,
                help="When set, the travel limits above are not enforced.",
            ),
        )
    return Group(section=section, title=title, fields=tuple(fields))


MACHINE = Group(
    "machine",
    "Machine",
    (
        Field("machine", "name", "Name", Kind.TEXT, default="unnamed"),
        Field(
            "machine",
            "units",
            "Units of this file",
            Kind.CHOICE,
            optional=False,
            choices=("mm", "inch"),
            default="mm",
            help=(
                "The units the numbers in THIS profile are written in. Changing it reinterprets every "
                "length in the file — it does not convert them."
            ),
        ),
    ),
)

DIALECT = Group(
    "dialect",
    "Controller",
    (
        Field(
            "dialect",
            "name",
            "Dialect",
            Kind.CHOICE,
            optional=False,
            choices=("linuxcnc", "mach3"),
            default="linuxcnc",
        ),
        Field(
            "dialect",
            "arc_centre",
            "Arc I/J mode",
            Kind.CHOICE,
            choices=("incremental", "absolute"),
            default="incremental",
            requires=_MACH3_ONLY,
            help=(
                "Mach3's Config → General 'IJ Mode'. Under LinuxCNC the G-code decides "
                "(G90.1/G91.1), so the key is refused there rather than ignored."
            ),
        ),
        Field(
            "dialect",
            "dwell_units",
            "G4 P units",
            Kind.CHOICE,
            choices=("seconds", "milliseconds"),
            default="seconds",
            requires=_MACH3_ONLY,
            help="Some Mach3 posts emit G4 P in milliseconds. Never inferred from the value of P.",
        ),
    ),
)

LIMITS = Group(
    "limits",
    "Limits",
    (
        Field("limits", "max_feed", "Maximum feed", Kind.RATE),
        Field("limits", "max_spindle_rpm", "Maximum spindle", Kind.COUNT),
        Field(
            "limits",
            "max_plunge_feed",
            "Maximum plunge feed",
            Kind.RATE,
            help=(
                "A straight-down G1 faster than this is reported. Ramps and helical entries are "
                "never reported. A property of the tool and material rather than the machine, which "
                "is why the shipped profile leaves it unset."
            ),
        ),
        Field(
            "limits",
            "rotary_wrap_warn",
            "Rotary travel per block",
            Kind.ANGLE,
            optional=False,
            default=360.0,
            help="Warn above this much rotary travel in one block. 0 warns on any rotary move.",
        ),
    ),
    help="An unset limit disables its check rather than defaulting to something plausible.",
)

TOLERANCE = Group(
    "tolerance",
    "Tolerances",
    (
        Field(
            "tolerance",
            "arc_radius_mismatch",
            "Arc radius mismatch",
            Kind.LENGTH,
            optional=False,
            default=0.005,
        ),
        Field(
            "tolerance",
            "arc_chord",
            "Arc chord deviation",
            Kind.LENGTH,
            optional=False,
            default=0.01,
        ),
        Field(
            "tolerance",
            "rotary_chord",
            "Rotary chord deviation",
            Kind.LENGTH,
            optional=False,
            default=0.01,
            help="A chord height in length units despite the name, measured at maximum path radius.",
        ),
    ),
    help="The one group with real defaults: tessellation cannot proceed without a number.",
)

KINEMATICS = Group(
    "kinematics",
    "Kinematics",
    (
        Field(
            "kinematics",
            "rotary_mount",
            "Rotary mount",
            Kind.CHOICE,
            optional=False,
            choices=("table", "head"),
            default="table",
            help=(
                "'table' rotates the part, 'head' swings the tool. A rotating table also means a "
                "fixed [stock] box stops describing where the stock is once A moves."
            ),
        ),
        Field(
            "kinematics",
            "rotary_axis",
            "Rotates about",
            Kind.CHOICE,
            optional=False,
            choices=("x", "y", "z"),
            default="x",
        ),
        Field(
            "kinematics",
            "centerline_offset",
            "Centreline passes through",
            Kind.VEC3,
            optional=False,
            default=(0.0, 0.0, 0.0),
        ),
        Field(
            "kinematics",
            "pivot_to_tip",
            "Pivot to tool tip",
            Kind.LENGTH,
            requires=("kinematics", "rotary_mount", ("head",)),
            help="Required for a head mount: the tip translates as the head swings.",
        ),
    ),
)

SAFETY = Group(
    "safety",
    "Safety",
    (
        Field("safety", "min_clearance_z", "Safe clearance Z", Kind.LENGTH),
        Field(
            "safety",
            "require_spindle_before_cut",
            "Require spindle before cutting",
            Kind.BOOL,
            optional=False,
            default=True,
        ),
        Field(
            "safety",
            "retract_before_toolchange",
            "Require retract before M6",
            Kind.BOOL,
            optional=False,
            default=True,
        ),
    ),
)

_BOX_ONLY = ("stock", "shape", ("box",))
_CYLINDER_ONLY = ("stock", "shape", ("cylinder",))

STOCK = Group(
    "stock",
    "Stock",
    (
        Field(
            "stock",
            "shape",
            "Shape",
            Kind.CHOICE,
            optional=False,
            choices=("box", "cylinder"),
            default="box",
            help=(
                "A box for prismatic work. A cylinder for a blank on the rotary axis — and that is "
                "the one the check can still judge once A moves, because a cylinder concentric with "
                "the rotary axis is unchanged by any rotation."
            ),
        ),
        Field("stock", "min", "Minimum corner", Kind.VEC3, optional=False, requires=_BOX_ONLY),
        Field("stock", "max", "Maximum corner", Kind.VEC3, optional=False, requires=_BOX_ONLY),
        Field(
            "stock",
            "diameter",
            "Diameter",
            Kind.LENGTH,
            optional=False,
            requires=_CYLINDER_ONLY,
        ),
        Field("stock", "length", "Length", Kind.LENGTH, optional=False, requires=_CYLINDER_ONLY),
        Field(
            "stock",
            "axis_min",
            "Nearer end, along the axis",
            Kind.LENGTH,
            optional=False,
            requires=_CYLINDER_ONLY,
            help=(
                "Machine coordinate of the end of the blank nearer the rotary axis's minimum. The "
                "axis itself is not restated here — it is the kinematics' rotary axis and centreline, "
                "because only a concentric cylinder is rotation-invariant."
            ),
        ),
    ),
    toggle=True,
    help=(
        "Where the blank is, in MACHINE coordinates — the same frame as the axes and offsets above. "
        "Every key of the chosen shape is mandatory. This is an envelope, not a material-removal "
        "model: a rapid through material an earlier pass cleared is reported too, which is why the "
        "finding is a warning."
    ),
)

TOOL = Group(
    "tool",
    "Tool",
    (
        Field(
            "tool",
            "diameter",
            "Diameter",
            Kind.LENGTH,
            optional=False,
            help=(
                "The cutter the solid view carves with. There is no tool table — this is ONE tool for "
                "the whole program, so a program that changes tools is carved wrongly wherever the "
                "other tool cut, and the view says so."
            ),
        ),
        Field(
            "tool",
            "shape",
            "Shape",
            Kind.CHOICE,
            optional=False,
            choices=("flat", "ball"),
            default="flat",
            help=(
                "Only the cutter's bottom matters to a heightfield. A shape that is neither is refused "
                "rather than carved as flat, which would leave square corners the part will not have."
            ),
        ),
    ),
    toggle=True,
    help=(
        "Used only to draw the solid view. No verifier reads it: every rule judges the programmed "
        "centreline, and giving the cutter a width would quietly change what several of them mean."
    ),
)

OFFSETS = Group(
    "offsets",
    "Work offsets",
    tuple(
        Field("offsets", f"g{code}", f"G{code}", Kind.OFFSET)
        for code in ("54", "55", "56", "57", "58", "59")
    ),
    help=(
        "Machine coordinates of each work origin. These live in the controller, not the G-code, so "
        "an unset offset downgrades travel-limit violations to warnings. Set-to-zero is not unset."
    ),
)

#: The form's groups, in the order they are shown. Axes come after the machine-wide settings because
#: that is the order the shipped profile writes them in, and a form that disagrees with the file it
#: edits is one more thing to reconcile while reading a diff.
GROUPS: tuple[Group, ...] = (
    MACHINE,
    DIALECT,
    LIMITS,
    TOLERANCE,
    _axis("x", "X axis"),
    _axis("y", "Y axis"),
    _axis("z", "Z axis"),
    _axis("a", "A axis (rotary)", rotary=True),
    OFFSETS,
    KINEMATICS,
    SAFETY,
    STOCK,
    TOOL,
)


def fields() -> tuple[Field, ...]:
    return tuple(f for group in GROUPS for f in group.fields)


def unit_label(kind: Kind, declared_units: str) -> str:
    """The suffix shown beside a field. Rotary kinds ignore ``declared_units`` by construction."""
    if kind in FIXED_UNITS:
        return FIXED_UNITS[kind]
    if kind is Kind.RATE:
        return "in/min" if declared_units == "inch" else "mm/min"
    if kind in SCALED:
        return "in" if declared_units == "inch" else "mm"
    return ""
