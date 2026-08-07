"""Fix transforms. Each returns new text, or refuses with a reason.

**The refusals are the point.** PLAN.md § Fix Engine names two explicitly, and both exist because past a
certain threshold a "fix" stops correcting the program and starts inventing it:

- **Arc-centre recomputation refuses beyond 10× tolerance.** Within a small mismatch the intent is
  obvious — the centre is a rounding artefact and the endpoints are what the programmer meant. Past that,
  three mutually inconsistent numbers describe no arc, and picking two of them to keep is a guess about
  which the programmer got right. A wrong guess produces geometry that looks entirely plausible.
- **IJK→R refuses on a full circle.** R-format cannot express one: R is the radius and the sign
  distinguishes the minor from the major arc, but a full circle has coincident endpoints, so every R
  describes the same degenerate case. There is no correct output.

**N-word stripping is destructive and off by default.** Operators restart a program mid-cut on an N-number,
and some dialects use them as jump targets. Removing them can break a program in ways the diff does not
show — the diff shows the lines changed, not that a `GOTO N120` no longer has a target.

Tolerances come from the profile, shared with the checks that reported the problem, so a fix and a
diagnostic can never disagree about what counts as a mismatch.
"""

import math
import re

from foursight.fix.engine import Fix, FixContext, FixResult, refuse, register_fix, succeeded
from foursight.machine.state import walk
from foursight.parser.resolver import parse
from foursight.verify.checks.geometry import arc_geometry

#: How far past `tolerance.arc_radius_mismatch` a fix will still act. PLAN.md § Fix Engine.
ARC_REFUSAL_FACTOR = 10.0

_ARC_MOTIONS = ("2", "3")
#: Which plane maps to which offset letters. G18 is (Z, X), matching `sim/interpolate.PLANES`.
_PLANE_OFFSETS = {"17": ("I", "J"), "18": ("K", "I"), "19": ("J", "K")}
_SAFETY_PREAMBLE = ("G90", "G21", "G17")


# --------------------------------------------------------------------------- line editing helpers


def _lines(text: str) -> list[str]:
    """Lines without their endings, plus the ending to rejoin with."""
    return text.splitlines()


def _newline(text: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if ending in text:
            return ending
    return "\n"


def _rejoin(lines: list[str], text: str) -> str:
    """Rejoin, preserving the original ending and whether the file ended with one."""
    ending = _newline(text)
    joined = ending.join(lines)
    return joined + ending if text.endswith(("\n", "\r")) or not text else joined


def _replace_line(text: str, line_no: int, replacement: str) -> str:
    lines = _lines(text)
    if not 1 <= line_no <= len(lines):
        raise IndexError(f"line {line_no} is outside a {len(lines)}-line program")
    lines[line_no - 1] = replacement
    return _rejoin(lines, text)


def _format_number(value: float) -> str:
    """``12.5`` not ``12.500000``, and ``-0`` never.

    Trailing zeros matter here: a fix that rewrote `I5` as `I5.000000` would produce a diff full of noise
    and make the real change hard to see.
    """
    if value == 0.0:
        return "0"
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "-") else "0"


def _set_word(block: str, letter: str, value: float) -> str:
    """Replace or append ``letter`` in one block, preserving everything else on the line."""
    pattern = re.compile(rf"(?<![A-Za-z]){letter}[ \t]*[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.IGNORECASE)
    replacement = f"{letter}{_format_number(value)}"
    if pattern.search(block):
        return pattern.sub(replacement, block, count=1)
    return f"{block} {replacement}" if block.strip() else replacement


def _remove_word(block: str, letter: str) -> str:
    pattern = re.compile(
        rf"[ \t]*(?<![A-Za-z]){letter}[ \t]*[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.IGNORECASE
    )
    return pattern.sub("", block, count=1)


def _command_on_line(context: FixContext, line_no: int):
    """The parsed command for ``line_no``, with the machine state before it. None if it has no motion."""
    result = parse(context.text, block_delete=context.block_delete)
    for command, before, _ in walk(result.commands):
        if command.ref.line_no == line_no:
            return command, before
    return None


# --------------------------------------------------------------------------- geometry fixes (T5.2)


def recompute_arc_centre(context: FixContext) -> FixResult:
    """Move an IJK centre onto the perpendicular bisector of the two endpoints.

    Both endpoints are **preserved** — they are where the tool must actually go, and the centre is the
    over-specified value. The new centre is the point on the bisector closest to the stated one, which
    keeps the arc's direction and sweep as close to the programmed intent as the geometry allows.

    Refuses beyond `ARC_REFUSAL_FACTOR` × tolerance: past that the three numbers describe no arc and
    choosing which to keep is a guess.
    """
    fix_id = "fix.recompute-arc-centre"
    if context.line is None:
        return refuse(fix_id, "this fix needs the line of the arc to correct")
    found = _command_on_line(context, context.line)
    if found is None:
        return refuse(fix_id, f"line {context.line} does not contain a motion block")
    command, before = found

    arc = arc_geometry(command, before)
    if arc is None:
        return refuse(fix_id, f"line {context.line} is not an IJK arc")

    tolerance = context.profile.tolerance.arc_radius_mismatch
    mismatch = abs(arc.radius_start - arc.radius_end)
    limit = tolerance * ARC_REFUSAL_FACTOR
    if mismatch > limit:
        return refuse(
            fix_id,
            f"radius mismatch is {mismatch:.4f} mm, more than {ARC_REFUSAL_FACTOR:g}x the "
            f"{tolerance:g} mm tolerance. Past that the endpoints and the centre describe no single "
            "arc, and correcting one of them would invent geometry rather than recover the intent — "
            "check the arc by hand.",
        )
    if mismatch <= tolerance:
        return FixResult(fix_id=fix_id, text=context.text, diff="", note="already within tolerance")

    centre = _closest_point_on_bisector(arc.start, arc.end, arc.centre)
    if centre is None:
        return refuse(
            fix_id, "the arc's endpoints are coincident, so it has no perpendicular bisector"
        )

    offsets = _PLANE_OFFSETS.get(command.modal_snapshot.plane)
    if offsets is None:
        return refuse(fix_id, f"unsupported plane G{command.modal_snapshot.plane}")
    first_letter, second_letter = offsets

    absolute = command.modal_snapshot.arc_distance == "90.1"
    first = centre[0] if absolute else centre[0] - arc.start[0]
    second = centre[1] if absolute else centre[1] - arc.start[1]

    block = _lines(context.text)[context.line - 1]
    block = _set_word(block, first_letter, first)
    block = _set_word(block, second_letter, second)
    return succeeded(
        fix_id,
        context,
        _replace_line(context.text, context.line, block),
        note=f"centre moved {mismatch / 2:.4f} mm; both endpoints preserved",
    )


def _closest_point_on_bisector(start, end, stated) -> tuple[float, float] | None:
    """The point on the perpendicular bisector of start–end nearest to ``stated``."""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length_squared = dx * dx + dy * dy
    if length_squared == 0.0:
        return None
    mid = ((sx + ex) / 2.0, (sy + ey) / 2.0)
    # The bisector runs through `mid` along the perpendicular (-dy, dx); project `stated` onto it.
    px, py = -dy, dx
    t = ((stated[0] - mid[0]) * px + (stated[1] - mid[1]) * py) / length_squared
    return (mid[0] + t * px, mid[1] + t * py)


def arc_r_to_ijk(context: FixContext) -> FixResult:
    """Rewrite an R-format arc as IJK, which is unambiguous.

    R is ambiguous by construction: two arcs of the same radius join any two points, and the sign only
    distinguishes them. IJK names the centre outright, which is why the verifier prefers it.
    """
    fix_id = "fix.arc-r-to-ijk"
    if context.line is None:
        return refuse(fix_id, "this fix needs the line of the arc to convert")
    found = _command_on_line(context, context.line)
    if found is None:
        return refuse(fix_id, f"line {context.line} does not contain a motion block")
    command, before = found
    if command.motion not in _ARC_MOTIONS:
        return refuse(fix_id, f"line {context.line} is not an arc")
    if "R" not in command.words:
        return refuse(fix_id, f"line {context.line} is not an R-format arc")

    offsets = _PLANE_OFFSETS.get(command.modal_snapshot.plane)
    if offsets is None:
        return refuse(fix_id, f"unsupported plane G{command.modal_snapshot.plane}")

    centre = _centre_from_r(command, before, offsets)
    if centre is None:
        return refuse(
            fix_id,
            "the endpoints are more than 2R apart, so no arc of that radius reaches them — the R "
            "value or an endpoint is wrong, and picking one to change would be a guess",
        )
    start, first_letter, second_letter = centre[1], offsets[0], offsets[1]
    absolute = command.modal_snapshot.arc_distance == "90.1"
    first = centre[0][0] if absolute else centre[0][0] - start[0]
    second = centre[0][1] if absolute else centre[0][1] - start[1]

    block = _lines(context.text)[context.line - 1]
    block = _remove_word(block, "R")
    block = _set_word(block, first_letter, first)
    block = _set_word(block, second_letter, second)
    return succeeded(fix_id, context, _replace_line(context.text, context.line, block))


def _centre_from_r(command, before, offsets):
    """The centre implied by an R arc, honouring the sign convention, plus the start point."""
    from foursight.verify.checks.geometry import _PLANE_AXES, _end_in_plane, _start_in_plane

    plane = _PLANE_AXES.get(command.modal_snapshot.plane)
    if plane is None:
        return None
    first, second = plane[0], plane[1]
    start = _start_in_plane(before, (first, second))
    end = _end_in_plane(before, command, (first, second))
    if start is None or end is None:
        return None

    radius = float(command.words["R"])
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if chord == 0.0 or abs(radius) < chord / 2.0:
        return None

    height = math.sqrt(max(0.0, radius * radius - (chord / 2.0) ** 2))
    mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    # Perpendicular direction; which side depends on the sweep direction and the sign of R. A negative R
    # selects the arc greater than 180 degrees, which is the centre on the other side of the chord.
    ux, uy = -dy / chord, dx / chord
    clockwise = command.motion == "2"
    sign = -1.0 if clockwise else 1.0
    if radius < 0:
        sign = -sign
    centre = (mid[0] + sign * height * ux, mid[1] + sign * height * uy)
    return centre, start


def arc_ijk_to_r(context: FixContext) -> FixResult:
    """Rewrite an IJK arc as R. **Refuses on a full circle**, which R cannot express.

    The sign convention is honoured: a sweep greater than 180° needs a negative R, and getting that wrong
    silently substitutes the complementary arc — same endpoints, same radius, the tool going the long way
    round instead of the short one.
    """
    fix_id = "fix.arc-ijk-to-r"
    if context.line is None:
        return refuse(fix_id, "this fix needs the line of the arc to convert")
    found = _command_on_line(context, context.line)
    if found is None:
        return refuse(fix_id, f"line {context.line} does not contain a motion block")
    command, before = found
    arc = arc_geometry(command, before)
    if arc is None:
        return refuse(fix_id, f"line {context.line} is not an IJK arc")

    if math.isclose(arc.start[0], arc.end[0], abs_tol=1e-9) and math.isclose(
        arc.start[1], arc.end[1], abs_tol=1e-9
    ):
        return refuse(
            fix_id,
            "this is a full circle, which R-format cannot express: R gives the radius and its sign "
            "picks the minor or major arc, but with coincident endpoints every R describes the same "
            "degenerate case. Keep the IJK form.",
        )

    sweep = _sweep_degrees(arc, clockwise=command.motion == "2")
    radius = math.hypot(arc.start[0] - arc.centre[0], arc.start[1] - arc.centre[1])
    signed = -radius if sweep > 180.0 else radius

    offsets = _PLANE_OFFSETS.get(command.modal_snapshot.plane)
    block = _lines(context.text)[context.line - 1]
    if offsets is not None:
        block = _remove_word(block, offsets[0])
        block = _remove_word(block, offsets[1])
    block = _set_word(block, "R", signed)
    return succeeded(
        fix_id,
        context,
        _replace_line(context.text, context.line, block),
        note=f"sweep {sweep:.1f}deg, so R is {'negative' if signed < 0 else 'positive'}",
    )


def _sweep_degrees(arc, *, clockwise: bool) -> float:
    """The swept angle in degrees, 0 to 360, in the arc's own direction."""
    start = math.atan2(arc.start[1] - arc.centre[1], arc.start[0] - arc.centre[0])
    end = math.atan2(arc.end[1] - arc.centre[1], arc.end[0] - arc.centre[0])
    delta = end - start
    if clockwise:
        delta = -delta
    while delta <= 0:
        delta += 2 * math.pi
    return math.degrees(delta)


# --------------------------------------------------------------------------- text fixes (T5.3)


def add_safety_preamble(context: FixContext) -> FixResult:
    """Insert `G90 G21 G17` and a safe-Z retract at the top, if not already established.

    Only the codes genuinely missing are added. A program that already declares G21 does not need it
    restated, and a diff that rewrites lines it did not have to change makes the real edit harder to see.
    """
    fix_id = "fix.add-safety-preamble"
    result = parse(context.text, block_delete=context.block_delete)
    present = {code for command in result.commands for code in command.gcodes}
    missing = [code for code in _SAFETY_PREAMBLE if code.lstrip("G") not in present]
    if not missing:
        return FixResult(
            fix_id=fix_id, text=context.text, diff="", note="the preamble is already established"
        )

    clearance = context.profile.safety.min_clearance_z
    lines = _lines(context.text)
    insert_at = _first_non_comment(lines)
    block = " ".join(missing)
    inserted = [f"{block} (added by FourSight)"]
    if clearance is not None:
        inserted.append(f"G0 Z{_format_number(clearance)} (safe retract)")
    lines[insert_at:insert_at] = inserted
    note = f"added {block}" + (
        "" if clearance is None else f" and a Z{_format_number(clearance)} retract"
    )
    return succeeded(fix_id, context, _rejoin(lines, context.text), note=note)


def _first_non_comment(lines: list[str]) -> int:
    """Where a preamble should go: after leading comments and framing, before the first real block."""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped == "%" or stripped.startswith(("(", ";")):
            continue
        if stripped.upper().startswith("O"):
            continue  # a Fanuc program number belongs above the preamble
        return index
    return len(lines)


def inject_feed_rate(context: FixContext) -> FixResult:
    """Add an F word to the first cutting move that has no feed rate in force.

    **The value is supplied by the user**, never chosen here. PLAN.md is explicit that this fix is
    prompted: a feed rate is a machining decision about tool, material and depth of cut, and inventing one
    would put a number in the program that nobody chose and the machine would obey.
    """
    fix_id = "fix.inject-feed-rate"
    try:
        feed = float(context.parameter)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return refuse(fix_id, "a feed rate is needed, in the program's own units per minute")
    if feed <= 0:
        return refuse(fix_id, f"a feed rate must be positive; got {feed:g}")

    result = parse(context.text, block_delete=context.block_delete)
    for command in result.commands:
        if command.motion in ("1", "2", "3") and command.modal_snapshot.feed is None:
            block = _set_word(_lines(context.text)[command.ref.line_no - 1], "F", feed)
            return succeeded(
                fix_id,
                context,
                _replace_line(context.text, command.ref.line_no, block),
                note=f"F{_format_number(feed)} added at line {command.ref.line_no}",
            )
    return FixResult(
        fix_id=fix_id, text=context.text, diff="", note="every cutting move already has a feed rate"
    )


def append_program_end(context: FixContext) -> FixResult:
    """Append `M30` if the program does not end."""
    fix_id = "fix.append-program-end"
    result = parse(context.text, block_delete=context.block_delete)
    if any(code in ("2", "30") for command in result.commands for code in command.mcodes):
        return FixResult(fix_id=fix_id, text=context.text, diff="", note="the program already ends")
    lines = _lines(context.text)
    # Above trailing framing, not after it: `%` closes a Fanuc program and M30 belongs inside.
    insert_at = len(lines)
    while insert_at > 0 and lines[insert_at - 1].strip() in ("", "%"):
        insert_at -= 1
    lines[insert_at:insert_at] = ["M30 (added by FourSight)"]
    return succeeded(fix_id, context, _rejoin(lines, context.text), note="M30 appended")


def normalize_whitespace(context: FixContext) -> FixResult:
    """Upper-case address letters and collapse runs of spaces. **Comments are left alone.**

    Text inside a comment is the programmer's prose, and normalizing it would rewrite words a human wrote
    for other humans — a change nobody asked for, arriving inside one they did.
    """
    fix_id = "fix.normalize-whitespace"
    changed = [_normalize_block(line) for line in _lines(context.text)]
    new_text = _rejoin(changed, context.text)
    if new_text == context.text:
        return FixResult(fix_id=fix_id, text=context.text, diff="", note="already normalized")
    return succeeded(fix_id, context, new_text)


_COMMENT_SPLIT = re.compile(r"(\([^)]*\)?|;.*)")


def _normalize_block(line: str) -> str:
    parts = _COMMENT_SPLIT.split(line)
    out = []
    for index, part in enumerate(parts):
        if index % 2 == 1:  # the captured comment
            out.append(part)
        else:
            out.append(re.sub(r"[ \t]+", " ", part.upper()))
    return "".join(out).strip()


def strip_line_numbers(context: FixContext) -> FixResult:
    """Remove N-words. **Destructive, and off by default.**

    PLAN.md § Fix Engine: operators restart a program mid-cut on an N-number, and some dialects use them
    as jump targets. Removing them can break a program in ways the **diff does not show** — the diff shows
    the lines that changed, not that a `GOTO N120` no longer has a target. So this is never applied
    silently, and the note says what was removed.
    """
    fix_id = "fix.strip-line-numbers"
    pattern = re.compile(r"^([ \t]*/?[ \t]*)N[ \t]*\d+(?:\.\d*)?[ \t]*", re.IGNORECASE)
    stripped = 0
    lines = []
    for line in _lines(context.text):
        new_line, count = pattern.subn(r"\1", line)
        stripped += count
        lines.append(new_line)
    if stripped == 0:
        return FixResult(fix_id=fix_id, text=context.text, diff="", note="no N-words to remove")
    return succeeded(
        fix_id,
        context,
        _rejoin(lines, context.text),
        note=(
            f"removed {stripped} N-word(s). If any are used as jump targets, or an operator restarts "
            "on them, this will change how the program runs."
        ),
    )


# --------------------------------------------------------------------------- registry


register_fix(
    Fix(
        fix_id="fix.recompute-arc-centre",
        title="Recompute arc centre",
        description=(
            "Move IJK onto the perpendicular bisector of the endpoints, keeping both endpoints. "
            f"Refuses beyond {ARC_REFUSAL_FACTOR:g}x tolerance."
        ),
        transform=recompute_arc_centre,
    )
)
register_fix(
    Fix(
        fix_id="fix.arc-r-to-ijk",
        title="Convert R arc to IJK",
        description="Replace an ambiguous R with an explicit centre.",
        transform=arc_r_to_ijk,
    )
)
register_fix(
    Fix(
        fix_id="fix.arc-ijk-to-r",
        title="Convert IJK arc to R",
        description="Replace an explicit centre with a signed radius. Refuses on full circles.",
        transform=arc_ijk_to_r,
    )
)
register_fix(
    Fix(
        fix_id="fix.add-safety-preamble",
        title="Add safety preamble",
        description="Insert the missing ones of G90, G21, G17 and a safe-Z retract at the top.",
        transform=add_safety_preamble,
    )
)
register_fix(
    Fix(
        fix_id="fix.inject-feed-rate",
        title="Add a feed rate",
        description="Add an F word to the first cutting move that has none.",
        transform=inject_feed_rate,
        needs_parameter=True,
        parameter_prompt="feed rate in units per minute",
    )
)
register_fix(
    Fix(
        fix_id="fix.append-program-end",
        title="Append M30",
        description="End a program that does not end.",
        transform=append_program_end,
    )
)
register_fix(
    Fix(
        fix_id="fix.normalize-whitespace",
        title="Normalize whitespace and case",
        description="Upper-case addresses and collapse spaces, leaving comments untouched.",
        transform=normalize_whitespace,
    )
)
register_fix(
    Fix(
        fix_id="fix.strip-line-numbers",
        title="Strip N-word line numbers",
        description=(
            "Remove N-numbers. Destructive: operators restart on them and some dialects use them as "
            "jump targets."
        ),
        transform=strip_line_numbers,
        destructive=True,
    )
)
