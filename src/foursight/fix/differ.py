"""Unified diff generation and application.

Generation is for **review**: PLAN.md § Fix Engine requires a fix to be reviewable before it is applied,
and a unified diff is what a machinist can actually read against their own file.

Application exists too, and not only for symmetry. A fix computes new text directly, so the engine never
*needs* to apply a diff — but being able to means the diff shown to the user can be checked to reproduce
exactly the text that would be written. A preview that does not match what gets applied is worse than no
preview, and `apply_unified_diff` is how that is tested rather than asserted.

**Line endings survive.** `LoadedFile.newline` records what the file used, and a fix that silently
rewrote a CRLF program as LF would be an unrequested change hiding inside a requested one — invisible in
the diff, since a unified diff does not show line terminators. `split_keeping_ends` and `join_with`
handle that; the editor buffer is always LF because Qt normalizes it, so the conversion happens at the
boundary.
"""

import difflib
from dataclasses import dataclass


class DiffError(Exception):
    """A diff could not be applied because it does not match the text it is being applied to."""


def unified_diff(before: str, after: str, *, path: str = "program.nc", context: int = 3) -> str:
    """A unified diff from ``before`` to ``after``, or ``""`` when they are identical.

    An empty string for "no change" is deliberate and load-bearing: the engine uses it to tell a fix that
    did nothing from one that did something, and a fix reporting success while changing nothing would send
    the user round the re-parse cycle for no reason.
    """
    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    return "".join(_ensure_newline(line) for line in lines)


def _ensure_newline(line: str) -> str:
    """A diff line always ends in a newline, so a file with no trailing newline still renders correctly."""
    return line if line.endswith("\n") else line + "\n"


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``@@`` block: where it applies in the original, and the lines it replaces it with."""

    start: int  # 0-based index into the original lines
    removed: tuple[str, ...]
    added: tuple[str, ...]


def parse_unified_diff(diff: str) -> list[Hunk]:
    """The hunks of a unified diff, in order.

    Only what `unified_diff` produces is supported — no renames, no binary, no multi-file diffs. This is
    a round-trip checker for our own output, not a general patch implementation, and pretending otherwise
    would invite it to be used as one.
    """
    hunks: list[Hunk] = []
    lines = diff.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@"):
            index += 1
            continue
        start = _parse_hunk_header(line)
        index += 1
        removed: list[str] = []
        added: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            body = lines[index]
            if body.startswith("+++") or body.startswith("---"):
                index += 1
                continue
            if body.startswith("+"):
                added.append(body[1:])
            elif body.startswith("-"):
                removed.append(body[1:])
            elif body.startswith(" "):
                removed.append(body[1:])
                added.append(body[1:])
            elif body.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                break
            index += 1
        hunks.append(Hunk(start=start, removed=tuple(removed), added=tuple(added)))
    return hunks


def _parse_hunk_header(header: str) -> int:
    """The 0-based original start line from ``@@ -a,b +c,d @@``."""
    try:
        old = header.split()[1]  # '-a,b'
        first = old[1:].split(",")[0]
        return max(0, int(first) - 1)
    except (IndexError, ValueError) as error:
        raise DiffError(f"malformed hunk header: {header!r}") from error


def apply_unified_diff(before: str, diff: str) -> str:
    """Apply ``diff`` to ``before``, or raise `DiffError` if the context does not match.

    Refusing on a context mismatch is the point. PLAN.md's one-fix contract exists because a fix
    invalidates every line number, so a diff applied to an already-changed buffer would land in the wrong
    place — and silently, since the surrounding lines often still look plausible.
    """
    if not diff:
        return before
    original = before.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    for hunk in parse_unified_diff(diff):
        if hunk.start < cursor:
            raise DiffError(f"hunks are not in order: {hunk.start} follows {cursor}")
        result.extend(original[cursor : hunk.start])
        expected = [
            line.rstrip("\r\n") for line in original[hunk.start : hunk.start + len(hunk.removed)]
        ]
        if expected != list(hunk.removed):
            raise DiffError(
                f"diff does not apply at line {hunk.start + 1}: expected {hunk.removed!r}, "
                f"found {tuple(expected)!r}"
            )
        newline = _newline_of(original, hunk.start)
        result.extend(line + newline for line in hunk.added)
        cursor = hunk.start + len(hunk.removed)
    result.extend(original[cursor:])
    return "".join(result)


def _newline_of(lines: list[str], index: int) -> str:
    """The line ending in use at ``index``, so applied lines match the file they join."""
    for line in lines[index:]:
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                return ending
    return "\n"


def split_keeping_ends(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def restore_newlines(text: str, newline: str) -> str:
    """Rewrite ``text``'s LF endings as ``newline``.

    The editor buffer is always LF because `QPlainTextEdit` normalizes it, so saving a program that
    arrived as CRLF needs this or the fix silently converts the whole file. That change would not appear
    in the diff at all — a unified diff shows no line terminators — which is exactly why it is handled
    here rather than left to whoever writes the file.
    """
    # Normalize to LF first, unconditionally, then apply the target. An early return for "\n" would make
    # a CRLF-to-LF request a silent no-op — surprising for a function named "restore", and the sort of
    # quiet non-action that only shows up as a mangled file much later.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline in ("", "\n"):
        return normalized
    return normalized.replace("\n", newline)
