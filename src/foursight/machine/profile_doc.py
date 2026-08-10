"""A machine profile held as **editable text**, so a form can change one field and keep the rest.

The GUI's profile editor (M8) is a structured form, but the thing it edits is this document rather than
a `MachineProfile`. Two reasons, and both are correctness rather than taste:

- **`MachineProfile` values are already converted to mm.** An inch profile writing `max_feed = 100.0`
  loads as 2540.0, so a form populated from the profile would show 2540 to someone who typed 100, and
  writing that back would convert it again — a 25.4× corruption per round trip, silent and cumulative.
  The as-written values are the only ones a form may display, and they live here.
- **The shipped profile's comments are its documentation.** `default_4axis.toml` explains every field
  inline, including which checks each one enables and why several are deliberately unset. Regenerating
  TOML from a parsed profile would delete all of it. So edits are *surgical*: one key's value is
  replaced in place, everything else in the file is untouched, and a key being switched off is
  commented out rather than deleted so its documentation survives to be switched back on.

The same reasoning the fix engine uses on G-code, applied to the profile: never rewrite the user's
file wholesale when a targeted edit will do.

`apply` returns a new document and never validates. Validation is `load_profile_text`, called by the
caller so that a `ProfileError` can be reported against the edit that caused it while the previously
loaded profile stays in force.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foursight.machine.profile import MachineProfile, ProfileError, load_profile_text
from foursight.parser.dialect import ARC_CENTRE_CODES, PRESETS, DialectName

#: Sentinel for "switch this key off". Distinct from `None`, which is a legitimate TOML-less value in
#: Python and would be ambiguous here — and the unset/zero distinction is exactly what this module
#: exists to keep straight (PLAN.md § Loading rules).
UNSET = object()


@dataclass(frozen=True, slots=True)
class Edit:
    """One field's new state.

    ``section`` is the TOML table path as written in the header without brackets: ``"limits"``,
    ``"axes.x"``. ``key`` is ``None`` only for a whole-section edit, which is how an optional section is
    switched off — see `ProfileDocument.apply`.
    """

    section: str
    key: str | None
    value: Any  # a Python value to render, or UNSET


# A key line, active or commented out. The leading `#` is captured so an existing commented-out
# default can be switched on in place, keeping the comment that explains it.
_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<hash>#\s*)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")
_HEADER = re.compile(r"^(?P<indent>\s*)(?P<hash>#\s*)?\[(?P<name>[^\]]+)\]\s*$")


@dataclass(frozen=True, slots=True)
class ProfileDocument:
    """The profile's text, plus the values exactly as the file writes them.

    ``raw`` is the unconverted `tomllib` result: ``raw["limits"]["max_feed"]`` is 100.0 for an inch
    profile that wrote 100.0, where `MachineProfile` would report 2540.0. A form must read from here.
    """

    text: str
    raw: dict

    @classmethod
    def from_text(cls, text: str) -> "ProfileDocument":
        """Parse the text. Raises `ProfileError` on malformed TOML, matching `load_profile_text`."""
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ProfileError(f"invalid TOML: {exc}") from exc
        return cls(text=text, raw=raw)

    @property
    def declared_units(self) -> str:
        """What the *file* says its numbers are in, for labelling a form. Never the internal mm."""
        units = str(self.raw.get("machine", {}).get("units", "mm")).lower()
        return units if units in {"mm", "inch"} else "mm"

    def value(self, section: str, key: str, default: Any = None) -> Any:
        """The as-written value of one key, or ``default`` when the file does not set it.

        A commented-out key is *not set*: it reads as absent here, which is what makes a form's
        "set" checkbox agree with the loader's "absence means unknown".
        """
        table: Any = self.raw
        for part in section.split("."):
            if not isinstance(table, dict):
                return default
            table = table.get(part)
            if table is None:
                return default
        if not isinstance(table, dict):
            return default
        return table.get(key, default)

    def has_section(self, section: str) -> bool:
        table: Any = self.raw
        for part in section.split("."):
            if not isinstance(table, dict) or part not in table:
                return False
            table = table[part]
        return isinstance(table, dict)

    def profile(self, path: "Path | None" = None) -> MachineProfile:
        """Validate and build. Raises `ProfileError`, which the caller reports rather than swallows.

        ``path`` is carried onto the profile so it still knows where it came from — the GUI re-reads it
        to repopulate the editor, and a rebuilt profile that forgot its path would send the editor back
        to the shipped default on the second open.
        """
        return load_profile_text(self.text, path=path)

    def apply(self, edits: "list[Edit] | tuple[Edit, ...]") -> "ProfileDocument":
        """Return a new document with each edit applied to the text in place.

        An `Edit` whose ``key`` is ``None`` addresses the whole section: ``UNSET`` comments out its
        header *and* every key under it, and anything else re-activates them. That indirection exists
        for `[stock]`, whose two bounds are mandatory together — commenting out only the keys would
        leave a present-but-empty section, which the loader refuses precisely because a half-specified
        box is a trap. Switching the section off has to take the header with it.
        """
        lines = self.text.splitlines(keepends=True)
        for edit in edits:
            if edit.key is None:
                lines = _set_section_active(lines, edit.section, active=edit.value is not UNSET)
            elif edit.value is UNSET:
                lines = _comment_out(lines, edit.section, edit.key)
            else:
                lines = _set_key(lines, edit.section, edit.key, render(edit.value))
        return ProfileDocument.from_text("".join(lines))


# --------------------------------------------------------------- CLI overrides, as document edits


def apply_dialect_override(
    document: ProfileDocument, name: str | None, arc_centre: str | None
) -> ProfileDocument:
    """Write the CLI's ``--dialect``/``--arc-centre`` into the document's own text.

    Without this the GUI's profile editor would show `[dialect].name = "linuxcnc"` while Mach3 was
    actually in force, and the first unrelated edit the user applied would silently revert the override
    — arcs then drawn with the wrong I/J convention and no diagnostic to say so. That is precisely the
    failure CLAUDE.md's "the dialect is resolved once, at the CLI/GUI boundary" invariant exists to
    prevent, so the boundary is where the override becomes text and there is no second copy downstream.

    **This duplicates `with_dialect`/`with_arc_centre`'s precedence rules**, which the headless CLI still
    uses on a `MachineProfile`. The duplication is deliberate and guarded the same way `sim/timing.py`'s
    two implementations are: `test_profile_doc.py::test_the_document_override_agrees_with_the_profile_one`
    drives both over every combination and demands the same result.
    """
    edits: list[Edit] = []
    current = str(document.value("dialect", "name", DialectName.LINUXCNC)).lower()
    effective = current if name is None else name
    if name is not None and name != current:
        if name not in PRESETS:
            raise ProfileError(f"unknown dialect {name!r}; expected one of {', '.join(PRESETS)}")
        # Settings tuned for one controller describe nothing about another, so they are cleared rather
        # than carried across — matching `with_dialect`, which resets them to defaults.
        edits += [
            Edit("dialect", "name", name),
            Edit("dialect", "arc_centre", UNSET),
            Edit("dialect", "dwell_units", UNSET),
        ]
    if arc_centre is not None:
        if arc_centre not in ARC_CENTRE_CODES:
            raise ProfileError(
                f"--arc-centre must be one of {', '.join(ARC_CENTRE_CODES)}, got {arc_centre!r}"
            )
        if effective == DialectName.LINUXCNC:
            raise ProfileError(
                "--arc-centre applies only where the arc centre is a controller setting; under "
                "'linuxcnc' the G-code decides it (G90.1/G91.1). Add --dialect mach3, or state "
                "G90.1/G91.1 in the program."
            )
        edits.append(Edit("dialect", "arc_centre", arc_centre))
    return document.apply(edits) if edits else document


# --------------------------------------------------------------------------- rendering


def render(value: Any) -> str:
    """A Python value as the TOML text for the right-hand side of a key.

    Floats keep a decimal point: `max_feed = 3000` is valid TOML and loads as an *integer*, which the
    profile loader accepts, but a file that reads `3000` where every neighbour reads `3000.0` invites
    the next reader to wonder whether the distinction means something.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render(item) for item in value) + "]"
    return _number(float(value))


def _number(value: float) -> str:
    """Enough precision to survive a round trip, without `0.005` becoming `0.005000000000000001`."""
    text = f"{value:.10g}"
    return text if ("." in text or "e" in text or "E" in text) else f"{text}.0"


# --------------------------------------------------------------------------- text surgery


@dataclass(frozen=True, slots=True)
class _Span:
    """Where a section's lines are: its header index, and the half-open range of its body."""

    header: int | None  # None when the section has no header in the file at all
    start: int  # first body line
    end: int  # one past the last body line
    active: bool  # whether the header itself is uncommented


def _find_section(lines: list[str], section: str) -> _Span:
    """Locate a section's header and body, whether or not the header is commented out.

    A commented-out header still has to be found, because switching `[stock]` back on means
    re-activating the block the user (or the shipped profile) left commented rather than appending a
    second copy of it further down the file.
    """
    header_index: int | None = None
    active = False
    for index, line in enumerate(lines):
        match = _HEADER.match(line)
        if match is None:
            continue
        if match.group("name").strip() == section:
            header_index = index
            active = match.group("hash") is None
            continue
        # The next header ends the body — but a *commented-out* header only ends an active section,
        # since inside a commented-out block it is part of that block's own text.
        if header_index is not None and (match.group("hash") is None or not active):
            return _Span(header=header_index, start=header_index + 1, end=index, active=active)
    if header_index is None:
        return _Span(header=None, start=len(lines), end=len(lines), active=False)
    return _Span(header=header_index, start=header_index + 1, end=len(lines), active=active)


def _find_key(lines: list[str], span: _Span, key: str) -> int | None:
    for index in range(span.start, min(span.end, len(lines))):
        match = _KEY_LINE.match(lines[index])
        if match is not None and match.group("key") == key:
            return index
    return None


def _set_key(lines: list[str], section: str, key: str, rendered: str) -> list[str]:
    """Set one key, re-activating and rewriting an existing line where there is one."""
    lines = list(lines)
    span = _find_section(lines, section)
    if span.header is None:
        return lines + _new_section(lines, section, key, rendered)
    if not span.active:
        lines = _set_section_active(lines, section, active=True)
        span = _find_section(lines, section)

    index = _find_key(lines, span, key)
    if index is None:
        return _insert_key(lines, span, key, rendered)
    lines[index] = _rewrite(lines[index], key, rendered)
    return lines


def _rewrite(line: str, key: str, rendered: str) -> str:
    """Replace a key line's value, keeping its indentation, trailing comment, column and ending.

    The trailing comment carries `# mm/min` and `# rapids below this → warning`; dropping it would
    strip the file's documentation one edit at a time. Its **column** is kept too, so the shipped
    profile's aligned comment blocks survive an edit that does not change the value's width — which is
    most edits, since a rate replaces a rate.

    Everything after the `=` is read from the regex match rather than scanned for, because on a
    *commented-out* line the leading `#` marks the whole line and is not a trailing comment. Treating
    it as one produced `max_plunge_feed = 250.0 # max_plunge_feed = 300.0`.
    """
    body, ending = _split_ending(line)
    match = _KEY_LINE.match(body)
    if (
        match is None
    ):  # pragma: no cover - only reached with a key that was just found by this regex
        return line
    indent = match.group("indent")
    at, comment = _trailing_comment(body[match.end() :])
    prefix = f"{indent}{key} = {rendered}"
    if not comment:
        return prefix + ending
    pad = " " * max(1, match.end() + at - len(prefix))
    return f"{prefix}{pad}{comment}{ending}"


def _comment_out(lines: list[str], section: str, key: str) -> list[str]:
    """Switch a key off by commenting it, never by deleting it.

    The line is the only place the file says what this field means and what a plausible value looks
    like. A user who switches `max_plunge_feed` off and later back on should find their number and its
    explanation still there.
    """
    lines = list(lines)
    span = _find_section(lines, section)
    if span.header is None or not span.active:
        return lines
    index = _find_key(lines, span, key)
    if index is None:
        return lines
    match = _KEY_LINE.match(lines[index])
    if match is not None and match.group("hash") is not None:
        return lines  # already off
    body, ending = _split_ending(lines[index])
    return _replaced(lines, index, f"{_indent(body)}# {body.lstrip()}{ending}")


def _set_section_active(lines: list[str], section: str, *, active: bool) -> list[str]:
    """Comment or uncomment a whole section — its header and every line of its body."""
    lines = list(lines)
    span = _find_section(lines, section)
    if span.header is None or span.active == active:
        return lines
    for index in range(span.header, min(span.end, len(lines))):
        body, ending = _split_ending(lines[index])
        stripped = body.strip()
        if not stripped:
            continue
        indent = _indent(body)
        if active:
            # Only a header or a `key = value` is uncommented. Prose inside a switched-off block —
            # a note about the fixture, or a second shape's keys listed for reference — is left alone,
            # because uncommenting it would produce a file that is not valid TOML at all. Commenting
            # *out* needs no such care: everything in the block goes.
            uncommentable = _HEADER.match(body) or _KEY_LINE.match(body)
            if stripped.startswith("#") and uncommentable:
                lines[index] = indent + stripped[1:].lstrip() + ending
        else:
            lines[index] = f"{indent}# {stripped}{ending}"
    return lines


def _new_section(lines: list[str], section: str, key: str, rendered: str) -> list[str]:
    """A section the file never mentioned, appended at the end."""
    lead = [] if not lines or lines[-1].endswith(("\n", "\r\n")) else ["\n"]
    return [*lead, "\n", f"[{section}]\n", f"{key} = {rendered}\n"]


def _insert_key(lines: list[str], span: _Span, key: str, rendered: str) -> list[str]:
    """Add a key the section does not have, after its last non-blank line.

    After the last *content* line rather than at the section's end, so a blank line separating this
    section from the next one stays where it is instead of being pushed down by every new key.
    """
    index = span.start
    for candidate in range(span.start, min(span.end, len(lines))):
        if lines[candidate].strip():
            index = candidate + 1
    return [*lines[:index], f"{key} = {rendered}\n", *lines[index:]]


def _replaced(lines: list[str], index: int, line: str) -> list[str]:
    updated = list(lines)
    updated[index] = line
    return updated


def _indent(body: str) -> str:
    return body[: len(body) - len(body.lstrip())]


def _split_ending(line: str) -> tuple[str, str]:
    """Separate a line from its newline, so an edit cannot change CRLF to LF.

    Windows checks these files out with CRLF, and rewriting one line's ending would leave a file with
    mixed endings that no diff explains (CLAUDE.md § Windows differs).
    """
    for ending in ("\r\n", "\n", "\r"):
        if line.endswith(ending):
            return line[: -len(ending)], ending
    return line, ""


def _trailing_comment(remainder: str) -> tuple[int, str]:
    """The `# …` in a key line's value part, as ``(offset, text)``, or ``(-1, "")``.

    A `#` inside a quoted string is not a comment: `name = "Mill #3"` has none.
    """
    in_string = False
    escaped = False
    for index, char in enumerate(remainder):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif char == "#" and not in_string:
            return index, remainder[index:].rstrip()
    return -1, ""
