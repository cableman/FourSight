"""Line → words (letter + number), with comments and framing constructs pulled out.

**One compiled regex, scanned once per line** — not one per word. The ~20 µs/line budget behind
"parse ≥ 50k lines/sec" does not survive per-word matching, and it certainly does not survive
compiling anything inside the loop. ``_TOKEN_RE`` is module-level and every line is a single
``finditer`` pass.

Malformed input is **reported, not raised**: a bad word must not cost us the rest of the file, and
the verifier's job is to list every problem at once. Errors come back as ``ParseError`` records on
the ``TokenizedLine``.

Creates each ``SourceRef`` once per line, shared by every ``Command`` and segment derived from it.

Handles per PLAN.md § Supported G-code Subset: comments ``( )`` and ``;``, block delete ``/``,
N-numbers, leading/trailing ``%``, Fanuc ``Oxxxx`` (consumed silently, never flagged),
case-insensitivity, and words with no separating whitespace (``X1.0Y2.0``).
"""

import re

from foursight.parser.model import ParseError, ParseErrorKind, SourceRef, TokenizedLine, Word

# A single alternation over the comment-free line, tried in order:
#
#   word   X1.0   — a letter with a value; whitespace between the two is legal
#   naked  X      — a letter with NO value. Worth its own group so the message can say exactly
#                   that, rather than lumping it in with stray punctuation.
#   space         — skipped
#   bad    .      — one stray character; consecutive ones merge into a single error
#
# The number pattern accepts '1', '1.', '1.0', '.5' and a leading sign. It deliberately does not
# accept '1.2.3': that matches as '1.2' and leaves '.3' to `bad`, which is the diagnosis we want.
_TOKEN_RE = re.compile(
    r"(?P<word>(?P<letter>[A-Za-z])[ \t]*(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)))"
    r"|(?P<naked>[A-Za-z])"
    r"|(?P<space>[ \t\r]+)"
    r"|(?P<bad>\S)"
)

# Comments are removed *before* words are scanned, because LinuxCNC strips them first: that makes
# `X (why not) 10` a legal way to write `X10`. Blanking them in place (rather than deleting) keeps
# every subsequent offset correct, so error positions still point into the original text.
_COMMENT_RE = re.compile(r"\([^)]*\)?|;[^\n]*")

# LinuxCNC O-word flow control. Out of scope for v1 (PLAN.md § Non-Goals), but it must not be
# reported as malformed: the letters of `sub` would otherwise surface as three bogus "address has
# no value" errors. Flow control decides *which motion runs*, so T1.7 classifies this as
# `unsupported` rather than a warning.
_OWORD_KEYWORDS = (
    "sub",
    "endsub",
    "call",
    "return",
    "if",
    "elseif",
    "else",
    "endif",
    "while",
    "endwhile",
    "do",
    "repeat",
    "endrepeat",
    "break",
    "continue",
)
_OWORD_RE = re.compile(
    r"^\s*O\s*(?:\d+|<[^>]*>)\s*(" + "|".join(_OWORD_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# A line that is nothing but '%' frames the program (Fanuc). Consumed silently, never flagged.
_FRAMING = "%"


def tokenize(text: str) -> list[TokenizedLine]:
    """Tokenize a whole document, preserving absolute offsets into ``text``.

    Offsets are computed from the *original* line lengths including their line endings, so a CRLF
    file yields the same ``SourceRef.start`` values a byte-counting editor would show.
    """
    lines: list[TokenizedLine] = []
    offset = 0
    for line_no, raw in enumerate(text.splitlines(keepends=True), start=1):
        content = raw.rstrip("\r\n")
        lines.append(tokenize_line(content, line_no, offset))
        offset += len(raw)
    return lines


def tokenize_line(content: str, line_no: int, line_start: int = 0) -> TokenizedLine:
    """Tokenize one line. ``content`` must already have its line ending stripped."""
    ref = SourceRef(line_no=line_no, start=line_start, end=line_start + len(content))

    stripped = content.strip()
    if not stripped or stripped == _FRAMING:
        # Blank, or program framing. Both are silent: no words, no errors.
        return TokenizedLine(ref=ref, words=[])

    scan_from, block_delete = _skip_block_delete(content)
    line = TokenizedLine(ref=ref, words=[], block_delete=block_delete)
    code = _strip_comments(line, ref, content)
    if _is_oword_flow_control(line, ref, code):
        return line
    return _scan(code, line, ref, scan_from)


def _strip_comments(line: TokenizedLine, ref: SourceRef, content: str) -> str:
    """Capture comments and blank them out, preserving length so offsets stay valid.

    Fast path first: most lines contain neither '(' nor ';', and a C-level membership test is far
    cheaper than a regex scan that will find nothing.
    """
    if "(" not in content and ";" not in content:
        return content

    pieces: list[str] = []
    last = 0
    for match in _COMMENT_RE.finditer(content):
        text = match.group()
        if text.startswith("("):
            if text.endswith(")"):
                _append(line, "comments", text[1:-1])
            else:
                _add_error(
                    line,
                    ref,
                    match.start(),
                    text,
                    "unterminated comment: missing ')'",
                    ParseErrorKind.UNTERMINATED_COMMENT,
                )
                _append(line, "comments", text[1:])
        else:
            _append(line, "comments", text[1:].strip())
        pieces.append(content[last : match.start()])
        pieces.append(" " * (match.end() - match.start()))
        last = match.end()
    pieces.append(content[last:])
    return "".join(pieces)


def _is_oword_flow_control(line: TokenizedLine, ref: SourceRef, code: str) -> bool:
    """Report an O-word subroutine/conditional as one clear unsupported construct."""
    match = _OWORD_RE.match(code)
    if match is None:
        return False
    keyword = match.group(1).lower()
    _add_error(
        line,
        ref,
        match.start(),
        match.group().strip(),
        f"LinuxCNC O-word '{keyword}' flow control is not interpreted in v1",
        ParseErrorKind.UNSUPPORTED_OWORD,
    )
    return True


def _skip_block_delete(content: str) -> tuple[int, bool]:
    """A leading '/' deletes the block. It must be the first non-whitespace character."""
    index = 0
    while index < len(content) and content[index] in " \t":
        index += 1
    if index < len(content) and content[index] == "/":
        return index + 1, True
    return 0, False


def _scan(code: str, line: TokenizedLine, ref: SourceRef, scan_from: int) -> TokenizedLine:
    """Single ``finditer`` pass over the comment-free line, classified by which group matched."""
    words: list[Word] = line.words
    pending_bad: list[re.Match[str]] = []

    for match in _TOKEN_RE.finditer(code, scan_from):
        if match.group("bad") is not None:
            # Hold stray characters so that '.3' reports once instead of twice.
            pending_bad.append(match)
            continue
        if pending_bad:
            _flush_bad(line, ref, pending_bad)

        if match.group("word") is not None:
            _add_word(line, words, match)
        elif match.group("naked") is not None:
            letter = match.group("naked").upper()
            _add_error(line, ref, match.start(), letter, f"address '{letter}' has no value")

    if pending_bad:
        _flush_bad(line, ref, pending_bad)
    return line


def _add_word(line: TokenizedLine, words: list[Word], match: re.Match[str]) -> None:
    """Route N and O out of ``words``; everything else is an address word."""
    letter = match.group("letter").upper()
    value = float(match.group("number"))
    if letter == "N":
        if line.line_number is None:
            line.line_number = value
        return
    if letter == "O":
        # Fanuc program number. Consumed silently — never flagged (PLAN.md § Supported subset).
        # NOTE: LinuxCNC O-word control flow ('O100 sub') is a different construct and is out of
        # scope for v1; its trailing keyword currently surfaces as a malformed-word error, which
        # T1.7 should reclassify as `unsupported` because flow control affects which motion runs.
        if line.program_number is None:
            line.program_number = value
        return
    words.append(Word(letter=letter, value=value))


def _flush_bad(line: TokenizedLine, ref: SourceRef, pending: list[re.Match[str]]) -> None:
    """Merge a run of adjacent stray characters into one error, then clear the run."""
    start = pending[0].start()
    text = "".join(match.group("bad") for match in pending)
    _add_error(line, ref, start, text, f"unrecognized characters {text!r}")
    pending.clear()


def _add_error(
    line: TokenizedLine,
    ref: SourceRef,
    at: int,
    text: str,
    message: str,
    kind: ParseErrorKind = ParseErrorKind.MALFORMED_WORD,
) -> None:
    _append(
        line,
        "errors",
        ParseError(line=ref.line_no, offset=ref.start + at, text=text, message=message, kind=kind),
    )


def _append(line: TokenizedLine, attribute: str, item: object) -> None:
    """Append to a list field that is ``None`` until first use, to avoid per-line allocations."""
    existing = getattr(line, attribute)
    if existing is None:
        setattr(line, attribute, [item])
    else:
        existing.append(item)
