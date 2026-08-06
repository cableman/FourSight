"""File loading: encoding detection, BOM handling, and honest offsets.

Minimum viable for M1 (T1.4); hardened for large files in T5.4.

The one thing this module must not get wrong is **offsets**. Every `SourceRef` holds character
offsets into the text this module returns, and editor↔viewport sync, diagnostics and fixes all
depend on those being right. Two decisions follow from that:

- **The BOM is stripped**, because leaving it would make the first line start with an invisible
  ``\\ufeff`` that the tokenizer would correctly report as garbage. Offsets are therefore into the
  decoded, BOM-free text — not into the on-disk bytes.
- **Line endings are preserved, never normalized.** Rewriting CRLF to LF would silently shift every
  offset after line 1 relative to the text we hand the editor. The tokenizer already handles both.

Decoding is separated from I/O (`load_text` vs `load`) so the encoding rules are testable without
a filesystem.
"""

import codecs
from dataclasses import dataclass
from pathlib import Path

# latin-1 maps every possible byte, so it can never fail — it is the fallback of last resort rather
# than a guess. The CLI should say so when it is used: a mis-decoded comment is cosmetic, but
# silently claiming success on a non-UTF-8 file is not.
FALLBACK_ENCODING = "latin-1"

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8"),
    # UTF-16 before UTF-8 would be wrong; UTF-32's BOM starts with UTF-16-LE's, but G-code in
    # UTF-32 is not a real case and is deliberately not claimed as supported.
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


class FileLoadError(Exception):
    """The file cannot be treated as G-code text at all.

    Distinct from a `ParseError`: malformed *G-code* is reported and the rest of the file still
    parses, but a binary file has nothing to report about. Raising keeps us from emitting thousands
    of meaningless diagnostics for an STL someone opened by mistake.
    """


@dataclass(slots=True, frozen=True)
class LoadedFile:
    """Decoded text plus what had to be assumed to get it."""

    text: str
    encoding: str  # the encoding actually used, not the one guessed first
    had_bom: bool
    newline: str  # dominant line ending: '\n' | '\r\n' | '\r' | '' when there are none
    path: Path | None = None

    @property
    def used_fallback(self) -> bool:
        """True when the text was only decodable as latin-1, so non-ASCII may be wrong."""
        return self.encoding == FALLBACK_ENCODING


def load(path: str | Path) -> LoadedFile:
    """Read and decode a file. `OSError` propagates: a missing file is the caller's problem."""
    resolved = Path(path)
    return load_text(resolved.read_bytes(), path=resolved)


def load_text(data: bytes, *, path: Path | None = None) -> LoadedFile:
    """Decode bytes to text, detecting a BOM and falling back to latin-1."""
    encoding, had_bom, body = _strip_bom(data)
    if encoding is None:
        try:
            text = body.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = body.decode(FALLBACK_ENCODING)
            encoding = FALLBACK_ENCODING
    else:
        text = body.decode(encoding)

    # Checked on the decoded text, not the raw bytes: legitimate UTF-16 is full of NUL bytes.
    if "\x00" in text:
        where = f" in {path}" if path else ""
        raise FileLoadError(f"file appears to be binary (contains NUL characters){where}")

    return LoadedFile(
        text=text,
        encoding=encoding,
        had_bom=had_bom,
        newline=detect_newline(text),
        path=path,
    )


def _strip_bom(data: bytes) -> tuple[str | None, bool, bytes]:
    """Return (encoding or None, had_bom, remaining bytes)."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding, True, data[len(bom) :]
    return None, False, data


def detect_newline(text: str) -> str:
    """The file's dominant line ending, for round-tripping on save (M5).

    Counts rather than looks at the first occurrence, so a file with one stray ending is still
    reported by its convention.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf:
        return "\r\n"
    if lf >= cr and lf:
        return "\n"
    if cr:
        return "\r"
    return ""


def line_starts(text: str) -> list[int]:
    """Offset of the first character of each line, 0-based index → offset.

    A function rather than a `LoadedFile` field: the tokenizer already tracks offsets as it goes,
    so most callers never need this, and materializing ~100k ints on every load would cost several
    MB for nothing. Fixes (T5.x) and jump-to-line need it, and can ask.

    `line_starts(text)[n - 1]` is the offset of 1-based line `n`.

    Built from `splitlines(keepends=True)` — the same primitive the tokenizer walks — so the two
    agree **by construction**. A hand-rolled scan for '\\n' looked faster and was wrong: it missed
    lone-'\\r' line endings, which `splitlines` does split on, so a classic-Mac file reported one
    line here and three in the tokenizer.
    """
    starts: list[int] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(raw)
    return starts
