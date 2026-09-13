"""A Mach3 profile `.xml`, read as **edits to a FourSight profile document**.

Every setting `[dialect]` and `[axes.a].short_rotate` carry is a setting only the controller knows, and
before this module the way to fill them in was to open the machine's Mach3 XML in a text editor and read
a tag. That is how `profiles/rotary.toml` was written, and its `short_rotate` has since gone stale
against the file it quotes (OPEN.md § 11). Reading the file is what this module does instead.

Five decisions, all of them refusals (PLAN.md § Importing a Mach3 profile):

- **The result is a list of `Edit`, never a generated file.** M8's surgery keeps the target profile's
  comments, and the form it feeds validates before anything takes effect.
- **`[machine].units` is never written**, and every length is converted into the units the document
  already declares. Writing `units = "inch"` would silently reinterpret every key the import did *not*
  touch — 25.4x, with no diff to look at.
- **Rotary values never scale.** `[axes.a]` is degrees end to end, and Mach3 agrees: `<AAngular>1` says
  the axis is angular, so its `Steps`/`Vel` are per degree on any native-units setting.
- **Nothing is guessed.** `[kinematics]`, `[stock]`, `[tool]`, `[offsets]`, `[safety]` and `[tolerance]`
  are not in the file, and a guessed `rotary_axis` draws a confidently wrong toolpath. They come back as
  notes saying what was not imported and why, rather than as silence.
- **`<Rot360>` is not `wrap`.** Mach3's "Rot 360" rolls the *DRO* over; `wrap` claims the axis turns
  continuously, so `min`/`max` do not bound it. A table does the second with the first switched off.

The three unit conventions are documented rather than inferred, in Mach3's own *V3.x Macro Programmers
Reference*: `GetSetupUnits()` is `0 = mm, 1 = inch` and does not move with G20/G21; `VelocitiesX…C` is
motor-tuning velocity in units/**second**; `GetIJMode()` is `0 = absolute, 1 = incremental`.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from foursight.machine.profile import INCH_TO_MM, ProfileError
from foursight.machine.profile_doc import Edit, ProfileDocument, render

#: One `<Tag>value</Tag>` pair. **Not an XML parser, deliberately**: a real profile carries raw bytes
#: inside `<LastUser>`, where `xml.etree` raises `not well-formed (invalid token)` and gives up on the
#: whole file. This also keeps `xml.etree` out of the codebase, which ruff's `S` set flags (S314).
_TAG = re.compile(r"<([A-Za-z_][\w.\-]*)>([^<]*)</\1>")

#: C0 controls, minus tab/newline/carriage return. Mach3 stores fixed-width binary blobs in some text
#: fields, and they are what makes the file invalid XML.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_PREFERENCES = re.compile(r"<Preferences>(.*)</Preferences>", re.DOTALL)

#: Mach3 indexes axes X, Y, Z, A, B, C as 0-5. FourSight v1 reads the first four; B and C exist in the
#: file but have no profile section to import into.
_AXES: tuple[tuple[int, str], ...] = ((0, "x"), (1, "y"), (2, "z"), (3, "a"))
_ROTARY_INDEX = 3

#: Seconds to minutes. `<Vel0>` is units/second; `[axes.*].max_rapid` is per minute.
_PER_MINUTE = 60.0

#: Mach3's spindle pulley table is `<SPEED1>`…`<SPEED25>` (maximum) and `<LSPEED*>` (minimum). The
#: ceiling `[limits].max_spindle_rpm` wants is the machine's, so it is the highest of them rather than
#: the pulley `<PULLEY>` selects today — which also sidesteps whether that index is 0- or 1-based.
_PULLEYS = 25

#: What a machine plausibly is, once every length is in mm. Used only to catch a **mis-set `<Units>`**,
#: which is the commonest Mach3 setup error there is: tuning steps-per-unit in inches without switching
#: native units leaves the flag saying mm while the operator means inch.
#:
#: Two things this cannot be built out of. `Steps x Vel` is a step rate in Hz, **identical under both
#: readings** — it looks like a units check and is not. And the two directions are not equally
#: detectable: a metric machine read as inch is 25.4x too big and unmistakable, while an inch machine
#: read as mm shrinks into numbers that stay superficially plausible. The floor below catches most of
#: that direction (a mill with under 50 mm of travel on its longest axis is not a mill), but the
#: reliable remedy is the selector with the values on screen in the units chosen: an operator knows
#: their own machine's rapid rate at a glance.
_MAX_RAPID_MM_MIN = 60_000.0
_MAX_TRAVEL_MM = 20_000.0
_MIN_TRAVEL_MM = 50.0


@dataclass(frozen=True, slots=True)
class Mach3Profile:
    """The tags of one Mach3 profile, flat. ``name`` is `<Profile>`; ``path`` is where it came from."""

    name: str
    values: dict[str, str]
    path: Path | None = None

    def text(self, tag: str) -> str | None:
        value = self.values.get(tag)
        return value or None

    def number(self, tag: str) -> float | None:
        """A tag as a float. Mach3 writes `85.`, `4.e+003` and `88.8889`; all three parse."""
        raw = self.text(tag)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def flag(self, tag: str) -> bool | None:
        value = self.number(tag)
        return None if value is None else value != 0.0


@dataclass(frozen=True, slots=True)
class Row:
    """One mapped setting, as the review table shows it.

    ``edit`` is ``None`` for a row that **writes nothing**: a conflict the file states and a profile
    cannot, kept visible because the user opened the dialog to see exactly this.
    """

    target: str
    source: str
    shown: str
    edit: Edit | None = None
    ticked: bool = True
    caveat: str = ""

    @property
    def writable(self) -> bool:
        return self.edit is not None


@dataclass(frozen=True, slots=True)
class Note:
    """Something deliberately not imported, and why. Shown, never silently omitted."""

    subject: str
    reason: str


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """What an import would do: rows to tick, notes to read, and any warning about the units."""

    units: str  # the Mach3 profile's native units, as read or as overridden
    declared_units: str  # the target document's, which the import never changes
    rows: tuple[Row, ...]
    notes: tuple[Note, ...]
    warnings: tuple[str, ...] = ()

    def edits(self, rows: "tuple[Row, ...] | list[Row] | None" = None) -> list[Edit]:
        """The edits for ``rows``, defaulting to the rows that are writable **and ticked**.

        The default is what the dialog opens with, so a test driving `edits()` drives what an operator
        who pressed OK without reading would get — which is the case worth pinning.
        """
        chosen = list(self.rows if rows is None else rows)
        if rows is None:
            chosen = [row for row in chosen if row.ticked]
        return [row.edit for row in chosen if row.edit is not None]


# --------------------------------------------------------------------------- reading


def read(path: "str | Path") -> Mach3Profile:
    """Read a Mach3 profile. ``OSError`` propagates; a file that is not one raises `ProfileError`."""
    resolved = Path(path)
    data = resolved.read_bytes()
    try:
        return read_text(_decode(data), path=resolved)
    except ProfileError as error:
        raise ProfileError(f"{resolved}: {error}") from error


def read_text(text: str, *, path: Path | None = None) -> Mach3Profile:
    """Scan the `<Preferences>` block. A duplicated tag takes its **last** value, as Mach3 rewrites it."""
    block = _PREFERENCES.search(text)
    scope = block.group(1) if block else text
    values = {
        match.group(1): match.group(2).strip() for match in _TAG.finditer(_CONTROL.sub("", scope))
    }
    name = values.get("Profile", "").strip()
    if not name or "Vel0" not in values:
        raise ProfileError(
            "not a Mach3 profile — no <Profile> and <Vel0> inside a <Preferences> block. A truncated "
            "or partly written file looks exactly like this."
        )
    return Mach3Profile(name=name, values=values, path=path)


def _decode(data: bytes) -> str:
    """Mach3 writes cp1252. UTF-8 is tried first so a hand-edited file survives its own accents."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def native_units(mach3: Mach3Profile) -> str:
    """`<Units>`: ``0`` is mm, ``1`` is inch — `GetSetupUnits()`, invariant under G20/G21."""
    return "inch" if mach3.flag("Units") else "mm"


# --------------------------------------------------------------------------- planning


def plan_import(
    document: ProfileDocument, mach3: Mach3Profile, units: str | None = None
) -> ImportPlan:
    """Everything the import would change, as reviewable rows plus the refusals as notes."""
    native = units or native_units(mach3)
    declared = document.declared_units
    scale = _scale(native, declared)
    rows: list[Row] = list(_identity_rows(mach3))
    notes: list[Note] = []
    for index, axis in _AXES:
        rows.extend(_axis_rows(document, mach3, index, axis, scale, notes))
    rows.extend(_limit_rows(mach3, scale, declared))
    notes.extend(_conditional_notes(mach3))
    notes.extend(_NOT_SUPPLIED)
    return ImportPlan(
        units=native,
        declared_units=declared,
        rows=tuple(rows),
        notes=tuple(notes),
        warnings=tuple(_unit_warnings(mach3, native)),
    )


def _scale(native: str, declared: str) -> float:
    """Lengths, from the Mach3 profile's units into the document's. Never the other way round."""
    if native == declared:
        return 1.0
    return INCH_TO_MM if native == "inch" else 1.0 / INCH_TO_MM


def _identity_rows(mach3: Mach3Profile) -> list[Row]:
    """Who the controller is, and the two settings only it knows."""
    rows = [
        Row(
            target="[machine].name",
            source=f"<Profile>{mach3.name}",
            shown=mach3.name,
            edit=Edit("machine", "name", mach3.name),
        ),
        Row(
            target="[dialect].name",
            source="the file is a Mach3 profile",
            shown="mach3",
            edit=Edit("dialect", "name", "mach3"),
        ),
    ]
    ij = mach3.flag("IJMode")
    if ij is not None:
        centre = "incremental" if ij else "absolute"
        rows.append(
            Row(
                target="[dialect].arc_centre",
                source=f"<IJMode>{mach3.text('IJMode')}",
                shown=centre,
                edit=Edit("dialect", "arc_centre", centre),
                caveat="Mach3's Config → General I/J Mode radio: 0 is absolute, 1 incremental.",
            )
        )
    dwell = mach3.flag("DwellinMilli")
    if dwell is not None:
        units = "milliseconds" if dwell else "seconds"
        rows.append(
            Row(
                target="[dialect].dwell_units",
                source=f"<DwellinMilli>{mach3.text('DwellinMilli')}",
                shown=units,
                edit=Edit("dialect", "dwell_units", units),
                caveat='Mach3\'s "G04 Dwell param in Milliseconds" checkbox.',
            )
        )
    return rows


def _axis_rows(
    document: ProfileDocument,
    mach3: Mach3Profile,
    index: int,
    axis: str,
    scale: float,
    notes: list[Note],
) -> list[Row]:
    """One axis's rate and travel, or a note saying why the axis was left alone."""
    section = f"axes.{axis}"
    rotary = index == _ROTARY_INDEX
    refusal = _axis_refusal(mach3, index, axis, rotary)
    if refusal is not None:
        notes.append(refusal)
        return []

    axis_scale = 1.0 if rotary else scale
    rows: list[Row] = []
    velocity = mach3.number(f"Vel{index}")
    if velocity is not None:
        rate = velocity * _PER_MINUTE * axis_scale
        rows.append(
            Row(
                target=f"[{section}].max_rapid",
                source=f"<Vel{index}>{mach3.text(f'Vel{index}')} per second",
                shown=_shown(rate, "deg/min" if rotary else f"{document.declared_units}/min"),
                edit=Edit(section, "max_rapid", rate),
            )
        )
    rows.extend(_travel_rows(document, mach3, index, section, axis_scale, rotary))
    if rotary:
        rows.extend(_short_rotate_rows(document, mach3, section, notes))
    return rows


def _axis_refusal(mach3: Mach3Profile, index: int, axis: str, rotary: bool) -> Note | None:
    """Why this axis contributes nothing. An inactive motor is a note, never a section deletion."""
    if mach3.flag(f"Motor{index}Active") is False:
        return Note(
            f"[axes.{axis}]",
            f"<Motor{index}Active>0 — the axis is switched off in this Mach3 profile. The section is "
            "left exactly as it is: removing it would change every check on that axis from enforced "
            "to unknown, which is a decision about the FourSight profile and not a fact in the XML.",
        )
    mapped = mach3.number(f"AxisToMotor{index}")
    if mapped is not None and int(mapped) != index:
        return Note(
            f"[axes.{axis}]",
            f"<AxisToMotor{index}>{int(mapped)} — this axis is driven by another motor's tuning, and "
            "which numbers then belong to it is not something either sample profile exercises. Set "
            "the rate and travel by hand rather than from a guess.",
        )
    if rotary and mach3.flag("AAngular") is False:
        return Note(
            f"[axes.{axis}]",
            "<AAngular>0 — A is configured as a linear axis in Mach3, so its travel and rate are "
            "lengths rather than degrees and cannot be read into a rotary section.",
        )
    return None


def _travel_rows(
    document: ProfileDocument,
    mach3: Mach3Profile,
    index: int,
    section: str,
    axis_scale: float,
    rotary: bool,
) -> list[Row]:
    """`min`/`max`, ticked only where Mach3 is actually enforcing them.

    Soft limits switched off are numbers nothing has had to be true for years — this machine's two
    profiles disagree about X travel by 750 mm — and `geometry.axis-travel-exceeded` reported against a
    stale envelope is confidently wrong about a correct program.
    """
    enforced = bool(mach3.flag("SoftLimit")) and (not rotary or bool(mach3.flag("ROTSOFT")))
    caveat = (
        ""
        if enforced
        else (
            "Mach3 is not enforcing soft limits here (<SoftLimit>0"
            + (", <ROTSOFT>0" if rotary else "")
            + "), so these bounds may never have been checked. Tick them only if they are real."
        )
    )
    unit = "deg" if rotary else document.declared_units
    rows = []
    for key, tag in (("min", f"M{index}Min"), ("max", f"M{index}Max")):
        value = mach3.number(tag)
        if value is None:
            continue
        travel = value * axis_scale
        rows.append(
            Row(
                target=f"[{section}].{key}",
                source=f"<{tag}>{mach3.text(tag)}",
                shown=_shown(travel, unit),
                edit=Edit(section, key, travel),
                ticked=enforced,
                caveat=caveat,
            )
        )
    return rows


def _short_rotate_rows(
    document: ProfileDocument, mach3: Mach3Profile, section: str, notes: list[Note]
) -> list[Row]:
    """`short_rotate`, and the one thing Mach3 can say that a profile cannot.

    The loader refuses `short_rotate` without `wrap`, and it is right to (T14.1): landing a full turn
    from the commanded angle is only the same place if the axis wraps. Mach3 has the two as independent
    checkboxes, so `<ShortRot>1` with a non-wrapping axis is a conflict — shown, and never reconciled by
    writing a `wrap` the file does not state.
    """
    short = mach3.flag("ShortRot")
    if short is None:
        return []
    wraps = bool(document.value(section, "wrap", False))
    source = f"<ShortRot>{mach3.text('ShortRot')}"
    if short and not wraps:
        notes.append(
            Note(
                f"[{section}].short_rotate",
                f"{source} says the control short-rotates rapids, but [{section}].wrap is false and "
                "the loader refuses the pair: stopping a full turn out is only the same place if the "
                "axis wraps. Mach3's <Rot360> is a DRO rollover and does not settle it. Set wrap by "
                "hand if the table turns continuously, then import again.",
            )
        )
        return [
            Row(
                target=f"[{section}].short_rotate",
                source=source,
                shown="conflict — nothing written",
                edit=None,
                ticked=False,
                caveat=f"Does the A axis turn continuously? [{section}].wrap currently says no.",
            )
        ]
    if not short and not wraps:
        return []
    return [
        Row(
            target=f"[{section}].short_rotate",
            source=source,
            shown="true" if short else "false",
            edit=Edit(section, "short_rotate", bool(short)),
        )
    ]


def _limit_rows(mach3: Mach3Profile, scale: float, declared: str) -> list[Row]:
    """`[limits]`: one real Mach3 setting, and one derivation that is offered rather than written."""
    rows = []
    speeds = [
        value
        for index in range(1, _PULLEYS + 1)
        if (value := mach3.number(f"SPEED{index}")) is not None
    ]
    if speeds:
        top = max(speeds)
        rows.append(
            Row(
                target="[limits].max_spindle_rpm",
                source=f"<SPEED1…SPEED{_PULLEYS}> pulley table, highest {render(top)}",
                shown=_shown(top, "rpm"),
                edit=Edit("limits", "max_spindle_rpm", top),
                caveat="The ceiling is the machine's, so it is the highest pulley rather than the "
                "one selected today.",
            )
        )
    velocities = [
        value
        for index in range(_ROTARY_INDEX)
        if (value := mach3.number(f"Vel{index}")) is not None
    ]
    if velocities:
        feed = min(velocities) * _PER_MINUTE * scale
        rows.append(
            Row(
                target="[limits].max_feed",
                source=f"<Vel0…{_ROTARY_INDEX - 1}> slowest linear axis",
                shown=_shown(feed, f"{declared}/min"),
                edit=Edit("limits", "max_feed", feed),
                ticked=False,
                caveat="Mach3 has no maximum-feed setting. This is a derivation: the highest feed the "
                "slowest linear axis can hold in any direction. Offered, not imported.",
            )
        )
    return rows


def _shown(value: float, unit: str) -> str:
    return f"{render(value)} {unit}".strip()


# --------------------------------------------------------------------------- what is not in the file


#: The sections a Mach3 XML has nothing to say about. Listed for the user every time, because a blank
#: where a setting should be is indistinguishable from a setting that was imported as blank —
#: `[kinematics]` most of all, where a guess draws a confidently wrong toolpath.
_NOT_SUPPLIED: tuple[Note, ...] = (
    Note(
        "[kinematics].rotary_mount",
        "table or head is a physical arrangement. The file has motors, not geometry.",
    ),
    Note(
        "[kinematics].rotary_axis",
        "nothing states which linear axis A turns about. <AParallel> and <AtoX/AtoY/AtoZ> are "
        "toolpath-display settings: profiles/rotary.toml wraps A about Y, from the post's header, "
        "while <AParallel>1 sits in the same XML.",
    ),
    Note(
        "[kinematics].centerline_offset",
        "where the axis passes through follows from how the job is fixtured and zeroed, and changes "
        "between setups on one machine.",
    ),
    Note("[kinematics].pivot_to_tip", "head-mount geometry, absent from the file."),
    Note(
        "[stock]",
        "today's blank, not a machine setting. <StockSize>, <RadiusA> and <MinPerPass> are display "
        "and feedrate values, not an envelope.",
    ),
    Note("[tool]", "Mach3's tool table is toolz.dat, not this file."),
    Note(
        "[offsets]",
        "G54-G59 live in Mach3's fixture .dat. Importing zeros would turn 'assumes zero offset' "
        "warnings into hard errors.",
    ),
    Note(
        "[safety].min_clearance_z",
        "a job decision. <m_SafeZ> is Mach3's own SafeZ move height, which is a different threshold "
        "and is not a check.",
    ),
    Note(
        "[tolerance]",
        "FourSight's tessellation, not a machine setting. <CVDegrees>/<LookAhead> describe the "
        "control's blending, which nothing here reads.",
    ),
    Note(
        "[axes.*].home",
        "<GHomeWay0…5> is a homing direction, not the machine position G28 returns to. An unset "
        "home means a G28 is not drawn, rather than drawn to a guessed point.",
    ),
    Note(
        "[axes.*] acceleration",
        "<Acc0…5> has nowhere to go: FourSight has no acceleration model, and a number in a field "
        "nothing reads makes a profile look more configured than it is.",
    ),
)


def _conditional_notes(mach3: Mach3Profile) -> list[Note]:
    """Notes about this particular file — the two tags that look like they map and do not."""
    notes = []
    rollover = mach3.flag("Rot360")
    if rollover is not None:
        notes.append(
            Note(
                "[axes.a].wrap",
                f"<Rot360>{mach3.text('Rot360')} is Mach3's DRO rollover — whether the displayed "
                "angle folds back to 0-360. FourSight's wrap claims the axis turns continuously, so "
                "min/max do not bound it. A table does the second with the first switched off, so the "
                "tag is reported and never written.",
            )
        )
    radius = mach3.number("RadiusA")
    if radius:
        notes.append(
            Note(
                "[stock].diameter",
                f"<RadiusA>{mach3.text('RadiusA')} is the Rotational Diameter family, used when "
                "making blended feedrate calculations (Mach3Mill 1.84 § 6.2.12). It is not the stock "
                "radius, and importing it as one would put a plausible wrong number into both the "
                "solid view and geometry.rapid-into-stock.",
            )
        )
    return notes


def _unit_warnings(mach3: Mach3Profile, native: str) -> list[str]:
    """Catch a mis-set `<Units>` by asking whether the result is a machine.

    Only quantities that *carry* units can discriminate. A step rate cannot: `Steps x Vel` is steps per
    second under either reading, which is exactly why it looks like a units check and is not.
    """
    to_mm = INCH_TO_MM if native == "inch" else 1.0
    other = "mm" if native == "inch" else "inch"
    warnings = []
    rapids = [
        value * _PER_MINUTE * to_mm
        for index in range(_ROTARY_INDEX)
        if (value := mach3.number(f"Vel{index}")) is not None
    ]
    if rapids and max(rapids) > _MAX_RAPID_MM_MIN:
        warnings.append(
            f"Read as {native}, the fastest linear rapid here is {render(max(rapids))} mm/min — "
            f"above anything a mill does. Check Mach3's Config → Select Native Units, or switch the "
            f"selector to {other}."
        )
    travels = [
        (top - bottom) * to_mm
        for index in range(_ROTARY_INDEX)
        if (top := mach3.number(f"M{index}Max")) is not None
        and (bottom := mach3.number(f"M{index}Min")) is not None
    ]
    span = max(travels, default=None)
    if span is not None and (span > _MAX_TRAVEL_MM or 0.0 < span < _MIN_TRAVEL_MM):
        warnings.append(
            f"Read as {native}, the longest axis travel here is {render(span)} mm. Check Mach3's "
            f"Config → Select Native Units, or switch the selector to {other}."
        )
    return warnings
