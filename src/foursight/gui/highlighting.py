"""What to colour on a line of G-code, as character spans. **No Qt.**

Same split as `batching.py` and `session.py`: the rules are testable without a widget, and
`editor.py` is a thin `QSyntaxHighlighter` that turns these spans into formats.

**The rules are the parser's own, not a second opinion.** This module imports `_COMMENT_RE` and
`_TOKEN_RE` from `parser/tokenizer.py` and applies them in the same order the tokenizer does —
comments first, then words over the comment-blanked text. That is the whole point: a hand-written
highlighting regex would drift from the parser, and the failure is nasty in a specific way. The editor
would show a construct in confident "this is a valid word" colour while the parser rejected it, or
paint something as a comment that the parser reads as motion. `X (c) 10` is exactly that trap — under
LinuxCNC rules it means `X10`, and a highlighter that treated the comment as a separator would imply
otherwise.

Reusing the tokenizer also makes malformed input visible for free. `_TOKEN_RE` already classifies a
letter with no value (`X` alone) and stray characters separately, so the editor can mark precisely what
`foursight check` will complain about, with no duplicated notion of "valid".

The private-name import is deliberate and worth the coupling: the alternative is two definitions of
what a word looks like, and only one of them decides what the machine does.
"""

from dataclasses import dataclass
from enum import Enum, auto

from foursight.parser.tokenizer import _COMMENT_RE, _FRAMING, _TOKEN_RE

# Which letters mean what. Split finer than the parser needs, because the reason to colour them
# differently is that a machinist scans for different things: geometry, feeds, and tooling.
AXIS_LETTERS = frozenset("XYZABCUVW")
ARC_LETTERS = frozenset("IJKR")
PARAMETER_LETTERS = frozenset("FS")
TOOL_LETTERS = frozenset("TDH")


class TokenKind(Enum):
    """A span's role. `editor.py` maps these to colours and nothing else."""

    COMMENT = auto()
    BLOCK_DELETE = auto()
    LINE_NUMBER = auto()  # N — labels the line, addresses nothing
    GCODE = auto()
    MCODE = auto()
    AXIS = auto()
    ARC = auto()
    PARAMETER = auto()  # F, S
    TOOL = auto()  # T, D, H
    OTHER_WORD = auto()
    MALFORMED = auto()  # what the parser will reject


@dataclass(frozen=True, slots=True)
class Span:
    """``length`` characters from ``start``, to be drawn as ``kind``."""

    start: int
    length: int
    kind: TokenKind

    @property
    def end(self) -> int:
        return self.start + self.length


def spans_for_line(content: str) -> list[Span]:
    """Classify one source line into non-overlapping spans, left to right.

    ``content`` is one line without its ending. Returns spans in start order; characters not covered
    by any span (whitespace, the framing `%`) are simply left unstyled.
    """
    stripped = content.strip()
    if not stripped or stripped == _FRAMING:
        # Blank, or program framing. The tokenizer returns silently for both — no words, no errors — so
        # marking `%` malformed here would be the editor contradicting the parser about a construct
        # that is perfectly legal Fanuc framing.
        return []

    comments: list[Span] = []
    blanked = _blank_comments(content, comments)
    words: list[Span] = []
    _scan_words(content, blanked, words)
    # A word may legitimately *span* a comment, because whitespace between letter and value is legal and
    # the comment has been blanked to spaces: `X (c) 10` is one word meaning X10. The parser is right,
    # but the comment still has to look like a comment, so word spans are clipped around them.
    spans = comments + [piece for word in words for piece in _clip(word, comments)]
    spans.sort(key=lambda span: span.start)
    return spans


def _clip(word: Span, comments: list[Span]) -> list[Span]:
    """`word` with any overlapping comment ranges cut out of it."""
    pieces = [word]
    for comment in comments:
        remaining: list[Span] = []
        for piece in pieces:
            if comment.end <= piece.start or comment.start >= piece.end:
                remaining.append(piece)
                continue
            if piece.start < comment.start:
                remaining.append(Span(piece.start, comment.start - piece.start, piece.kind))
            if comment.end < piece.end:
                remaining.append(Span(comment.end, piece.end - comment.end, piece.kind))
        pieces = remaining
    return pieces


def _blank_comments(content: str, spans: list[Span]) -> str:
    """Record comment spans and return the line with them replaced by spaces.

    Blanking in place rather than removing is what keeps every later offset equal to the offset in the
    original line — the same trick `tokenizer._strip_comments` uses, and the reason the two agree.
    """
    if "(" not in content and ";" not in content:
        return content  # the common case; skip the regex entirely
    pieces = list(content)
    for match in _COMMENT_RE.finditer(content):
        start, end = match.span()
        spans.append(Span(start, end - start, TokenKind.COMMENT))
        for index in range(start, end):
            pieces[index] = " "
    return "".join(pieces)


def _scan_words(content: str, blanked: str, spans: list[Span]) -> None:
    """Words, N-numbers and malformed input, over the comment-free text."""
    slash = _block_delete_index(content)
    if slash >= 0:
        # Just the slash, not the whitespace before it: highlighting the indentation as well reads as a
        # selection artefact rather than as a marked character.
        spans.append(Span(slash, 1, TokenKind.BLOCK_DELETE))

    for match in _TOKEN_RE.finditer(blanked, slash + 1):
        if match.lastgroup == "space":
            continue
        start, end = match.span()
        if match.group("word"):
            spans.append(Span(start, end - start, _kind_for(match.group("letter"))))
        else:
            # `naked` (a letter with no value) and `bad` (a stray character) are both what the parser
            # reports as errors, so both are shown as malformed rather than as plausible words.
            spans.append(Span(start, end - start, TokenKind.MALFORMED))


def _block_delete_index(content: str) -> int:
    """Index of a leading ``/``, or -1. Leading whitespace before it is legal.

    Returns -1 rather than 0 so that "no block delete" and "a slash at column 0" stay distinguishable;
    callers scan from ``result + 1``, which is 0 in the absent case.
    """
    for index, character in enumerate(content):
        if character == "/":
            return index
        if character not in " \t":
            return -1
    return -1


def _kind_for(letter: str) -> TokenKind:
    upper = letter.upper()
    if upper == "N":
        return TokenKind.LINE_NUMBER
    if upper == "G":
        return TokenKind.GCODE
    if upper == "M":
        return TokenKind.MCODE
    if upper in AXIS_LETTERS:
        return TokenKind.AXIS
    if upper in ARC_LETTERS:
        return TokenKind.ARC
    if upper in PARAMETER_LETTERS:
        return TokenKind.PARAMETER
    if upper in TOOL_LETTERS:
        return TokenKind.TOOL
    return TokenKind.OTHER_WORD
