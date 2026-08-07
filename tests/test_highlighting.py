"""Highlighting tests (T3.1). No Qt — the rules live in `gui/highlighting.py`, which imports none.

The claim under test is **agreement with the parser**, not prettiness. `highlighting.py` reuses the
tokenizer's own `_COMMENT_RE` and `_TOKEN_RE` in the same order, so the editor cannot paint something as
a valid word that `foursight check` rejects, or as a comment that the parser reads as motion.

`test_highlighting_agrees_with_the_tokenizer_on_every_fixture` is the important one: it compares, line by
line across the whole fixture corpus, what the editor calls malformed against what the parser actually
reports as an error. A hand-written highlighting regex would drift from the parser over time and nothing
else in the suite would notice.
"""

import pytest

from conftest import FIXTURES, fixture_text
from foursight.gui.highlighting import Span, TokenKind, spans_for_line
from foursight.parser.tokenizer import tokenize_line


def kinds(text: str) -> list[tuple[str, TokenKind]]:
    """The rendered text of each span with its kind, which is what a reader would see."""
    return [(text[span.start : span.end], span.kind) for span in spans_for_line(text)]


def kind_of(text: str, fragment: str) -> TokenKind:
    for rendered, kind in kinds(text):
        if rendered == fragment:
            return kind
    raise AssertionError(f"{fragment!r} is not a span of {text!r}; got {kinds(text)}")


# --------------------------------------------------------------------------- agreement with the parser


@pytest.mark.parametrize("name", sorted(path.name for path in FIXTURES.glob("*.nc")))
def test_highlighting_agrees_with_the_tokenizer_on_every_fixture(name: str) -> None:
    """No fixture line may be painted as malformed, because none of them has a parse error.

    The corpus is clean by construction — T1.13 verified 0 parse errors across all ten — so any
    malformed span here means the highlighter has drifted from the parser and is crying wolf in the
    editor about code that runs.
    """
    for number, content in enumerate(fixture_text(name).splitlines(), start=1):
        bad = [span for span in spans_for_line(content) if span.kind is TokenKind.MALFORMED]
        assert not bad, f"{name}:{number} highlighted as malformed but parses cleanly: {content!r}"


@pytest.mark.parametrize(
    "content",
    [
        "G1 X Y10",  # a letter with no value
        "G1 X1.2.3",  # '1.2' then a stray '.3'
        "G1 $ X10",  # stray punctuation
    ],
)
def test_what_the_parser_rejects_is_marked_malformed(content: str) -> None:
    """The other direction: if the parser errors on a line, the editor must say so.

    Both halves are needed. Without this one the highlighter could mark nothing malformed ever and still
    pass the fixture agreement test above.
    """
    assert tokenize_line(content, 1).has_errors, "the test input must actually be a parse error"
    assert any(span.kind is TokenKind.MALFORMED for span in spans_for_line(content))


def test_a_clean_line_has_no_malformed_span() -> None:
    assert tokenize_line("N10 G1 X10.5 Y-3 F1200", 1).has_errors is False
    assert all(kind is not TokenKind.MALFORMED for _, kind in kinds("N10 G1 X10.5 Y-3 F1200"))


# --------------------------------------------------------------------------- comments


def test_a_parenthesised_comment_is_one_span() -> None:
    assert kind_of("G0 Z5 (retract)", "(retract)") is TokenKind.COMMENT


def test_a_semicolon_comment_runs_to_end_of_line() -> None:
    assert kind_of("G2 X10 I5 ; to the end", "; to the end") is TokenKind.COMMENT


def test_comments_do_not_nest() -> None:
    """`(a (b) c)` closes at the first `)`. A nested-comment assumption once cost 14 spurious errors."""
    text = "G1 X10 (from (50, 0) onward)"
    assert kind_of(text, "(from (50, 0)") is TokenKind.COMMENT
    assert any(kind is not TokenKind.COMMENT for _, kind in kinds(text)[1:])


def test_a_word_spanning_a_comment_keeps_the_comment_visible() -> None:
    """`X (c) 10` means X10 under LinuxCNC rules, and the comment must still look like a comment.

    The word genuinely covers the comment's characters, since whitespace between letter and value is
    legal and the comment is blanked to spaces. Painting the whole run as an axis word would hide the
    comment; dropping the word would imply `X` and `10` are unrelated.
    """
    text = "X (c) 10"
    rendered = kinds(text)
    assert (("(c)", TokenKind.COMMENT)) in rendered
    assert [kind for _, kind in rendered].count(TokenKind.AXIS) == 2, rendered


def test_spans_never_overlap() -> None:
    """Overlapping spans would let one format silently overwrite another."""
    for text in ["X (c) 10", "G1 X10 (a) Y20 ; b", "/N5 G0 Z5 (up)", "G1 X1.2.3 (c)"]:
        spans = spans_for_line(text)
        for first, second in zip(spans, spans[1:], strict=False):
            assert first.end <= second.start, f"overlap in {text!r}: {first} {second}"


# --------------------------------------------------------------------------- framing and block delete


def test_bare_framing_is_not_marked_malformed() -> None:
    """A `%` line is legal Fanuc framing that the tokenizer consumes silently.

    An earlier version marked it malformed, which is the editor contradicting the parser about a
    construct every Fanuc program starts with.
    """
    assert tokenize_line("%", 1).has_errors is False
    assert spans_for_line("%") == []


def test_a_blank_line_produces_no_spans() -> None:
    assert spans_for_line("") == []
    assert spans_for_line("   ") == []


def test_block_delete_is_its_own_kind() -> None:
    """`/` decides whether the line runs at all, so it must not read as ordinary punctuation."""
    assert kind_of("/G0 Z5", "/") is TokenKind.BLOCK_DELETE


def test_block_delete_after_leading_whitespace_is_still_recognised() -> None:
    assert kind_of("  /G0 Z5", "/") is TokenKind.BLOCK_DELETE


def test_a_slash_that_is_not_leading_is_not_block_delete() -> None:
    """Only a *leading* slash deletes the block; anywhere else it is a stray character."""
    assert kind_of("G0 Z5 /", "/") is TokenKind.MALFORMED


# --------------------------------------------------------------------------- letter roles


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ("N10", TokenKind.LINE_NUMBER),
        ("G1", TokenKind.GCODE),
        ("M8", TokenKind.MCODE),
        ("X1", TokenKind.AXIS),
        ("A90", TokenKind.AXIS),
        ("I5", TokenKind.ARC),
        ("R2", TokenKind.ARC),
        ("F600", TokenKind.PARAMETER),
        ("S8000", TokenKind.PARAMETER),
        ("T1", TokenKind.TOOL),
        ("H1", TokenKind.TOOL),
    ],
)
def test_letters_are_grouped_by_what_a_machinist_scans_for(
    fragment: str, expected: TokenKind
) -> None:
    assert kind_of("N10 G1 M8 X1 A90 I5 R2 F600 S8000 T1 H1", fragment) is expected


def test_an_n_number_is_not_coloured_as_an_axis() -> None:
    """N labels the line and addresses nothing — PLAN.md § Supported G-code Subset."""
    assert kind_of("N10 G1 X10", "N10") is not TokenKind.AXIS


def test_lowercase_is_recognised_the_same_as_uppercase() -> None:
    """`g1 x10` is legal and tokenizes identically, so it must highlight identically."""
    assert kind_of("g1 x10", "g1") is TokenKind.GCODE
    assert kind_of("g1 x10", "x10") is TokenKind.AXIS


def test_an_unknown_letter_is_a_word_not_an_error() -> None:
    """`Q` is well-formed and unrecognised. PLAN's tiers: unrecognised-but-inert is not an error."""
    assert kind_of("G1 Q5", "Q5") is TokenKind.OTHER_WORD


# --------------------------------------------------------------------------- shape


def test_spans_are_returned_in_source_order() -> None:
    spans = spans_for_line("N5 G1 X10 (c) F600")
    assert [span.start for span in spans] == sorted(span.start for span in spans)


def test_span_end_is_start_plus_length() -> None:
    span = Span(3, 4, TokenKind.AXIS)
    assert span.end == 7


def test_every_span_lies_inside_the_line() -> None:
    text = "N5 G1 X10 Y-3.5 (comment) F600 ; trailing"
    for span in spans_for_line(text):
        assert 0 <= span.start < len(text)
        assert span.end <= len(text)
