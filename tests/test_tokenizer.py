"""Tokenizer tests (T1.2).

Every construct PLAN.md § Supported G-code Subset lists as tokenizer-level, plus malformed input,
which must come back as ``ParseError`` records rather than exceptions.
"""

import pytest

from foursight.parser.model import TokenizedLine
from foursight.parser.tokenizer import tokenize, tokenize_line


def words(line: TokenizedLine) -> list[tuple[str, float]]:
    return [(word.letter, word.value) for word in line.words]


def messages(line: TokenizedLine) -> list[str]:
    return [error.message for error in line.errors or ()]


def tok(text: str) -> TokenizedLine:
    return tokenize_line(text, line_no=1)


# --------------------------------------------------------------------------- words


def test_simple_block() -> None:
    line = tok("G1 X10 Y-2.5 F200")
    assert words(line) == [("G", 1.0), ("X", 10.0), ("Y", -2.5), ("F", 200.0)]
    assert not line.has_errors


def test_words_need_no_separating_whitespace() -> None:
    """`X1.0Y2.0` is legal and common in machine-generated output."""
    assert words(tok("G1X1.0Y2.0Z3")) == [("G", 1.0), ("X", 1.0), ("Y", 2.0), ("Z", 3.0)]


def test_whitespace_between_letter_and_value_is_legal() -> None:
    assert words(tok("X 10   Y\t20")) == [("X", 10.0), ("Y", 20.0)]


def test_case_insensitive_and_normalized_to_upper() -> None:
    assert words(tok("g1 x10 f200")) == [("G", 1.0), ("X", 10.0), ("F", 200.0)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("X1", 1.0),
        ("X1.", 1.0),
        ("X1.0", 1.0),
        ("X.5", 0.5),
        ("X-.5", -0.5),
        ("X+2", 2.0),
        ("X-0.001", -0.001),
        ("X0", 0.0),
    ],
)
def test_number_formats(text: str, expected: float) -> None:
    """Decimal-less, trailing-dot, leading-dot and signed forms all appear in real programs."""
    line = tok(text)
    assert not line.has_errors, messages(line)
    assert line.words[0].value == expected


def test_gcodes_keep_their_decimal_form_as_a_value() -> None:
    """G90.1 must survive tokenization; canonicalization to '90.1' is the resolver's job (T1.3)."""
    assert words(tok("G90.1")) == [("G", 90.1)]


# --------------------------------------------------------------------------- comments


def test_parenthesised_comment_is_captured_and_removed() -> None:
    line = tok("G1 X10 (rough pass) Y20")
    assert words(line) == [("G", 1.0), ("X", 10.0), ("Y", 20.0)]
    assert line.comments == ["rough pass"]
    assert not line.has_errors


def test_semicolon_comment_runs_to_end_of_line() -> None:
    line = tok("G1 X10 ; everything here is a comment (even this")
    assert words(line) == [("G", 1.0), ("X", 10.0)]
    assert line.comments == ["everything here is a comment (even this"]
    assert not line.has_errors


def test_unterminated_comment_is_an_error_but_still_captured() -> None:
    line = tok("G1 X10 (oops")
    assert words(line) == [("G", 1.0), ("X", 10.0)]
    assert line.comments == ["oops"]
    assert "unterminated comment" in messages(line)[0]


def test_comment_between_letter_and_value_is_stripped_before_parsing() -> None:
    """LinuxCNC removes comments *before* interpreting words, so this legally means `X10`.

    Tokenizing comments inline instead would report two false errors on valid input. Comments are
    blanked in place rather than deleted, so offsets after them stay correct.
    """
    line = tok("G1 X (why not) 10 Y20")
    assert words(line) == [("G", 1.0), ("X", 10.0), ("Y", 20.0)]
    assert line.comments == ["why not"]
    assert not line.has_errors


def test_oword_flow_control_is_one_clear_unsupported_error() -> None:
    """Not three bogus "address has no value" errors for the letters of `sub`.

    O-word flow control decides *which* motion runs, so T1.7 must classify this as `unsupported`
    rather than a warning — a misleading malformed-word diagnosis would hide that.
    """
    line = tok("O100 sub")
    assert line.words == []
    assert len(line.errors) == 1
    assert "O-word 'sub'" in line.errors[0].message
    assert line.program_number is None, "a subroutine number is not a program number"


@pytest.mark.parametrize(
    "text", ["O100 sub", "o100 endsub", "O<myname> sub", "o<n> while", "O10 if", "O10 call"]
)
def test_oword_flow_control_forms(text: str) -> None:
    line = tok(text)
    assert line.has_errors
    assert "flow control" in line.errors[0].message


def test_comment_only_line() -> None:
    line = tok("(setup: 6mm flat endmill)")
    assert line.words == []
    assert line.comments == ["setup: 6mm flat endmill"]
    assert not line.has_errors


# --------------------------------------------------------------------------- framing


def test_block_delete_flag_is_recorded_not_applied() -> None:
    """Block delete is OFF by default, so the words must still be tokenized (PLAN.md § Dialect)."""
    line = tok("/G1 X10")
    assert line.block_delete is True
    assert words(line) == [("G", 1.0), ("X", 10.0)]


def test_block_delete_may_follow_leading_whitespace() -> None:
    assert tok("  /G1 X10").block_delete is True


def test_slash_that_is_not_leading_is_not_block_delete() -> None:
    line = tok("G1 X10 / Y20")
    assert line.block_delete is False
    assert "/" in (line.errors or [None])[0].text


def test_n_number_is_pulled_out_of_words() -> None:
    line = tok("N120 G1 X10")
    assert line.line_number == 120.0
    assert words(line) == [("G", 1.0), ("X", 10.0)]
    assert "N" not in [word.letter for word in line.words]


def test_fanuc_program_number_is_consumed_silently() -> None:
    """`Oxxxx` is framing, never flagged (PLAN.md § Dialect Divergences)."""
    line = tok("O1234")
    assert line.program_number == 1234.0
    assert line.words == []
    assert not line.has_errors


def test_percent_framing_line_is_silent() -> None:
    for text in ("%", "  %  "):
        line = tok(text)
        assert line.words == []
        assert not line.has_errors
        assert line.comments is None


def test_blank_line_is_silent() -> None:
    for text in ("", "   ", "\t"):
        line = tok(text)
        assert line.words == []
        assert not line.has_errors


# --------------------------------------------------------------------------- malformed input


@pytest.mark.parametrize("text", ["X", "XY", "X1.2.3", "*", "G1 X#", "X1 Y", "?!"])
def test_malformed_input_reports_errors_and_never_raises(text: str) -> None:
    """One bad word must not cost us the rest of the file."""
    line = tok(text)
    assert line.has_errors, f"{text!r} should have produced an error"


def test_address_without_a_value_says_so() -> None:
    line = tok("X")
    assert messages(line) == ["address 'X' has no value"]


def test_two_addresses_without_values_report_separately() -> None:
    line = tok("XY")
    assert messages(line) == ["address 'X' has no value", "address 'Y' has no value"]


def test_double_decimal_keeps_the_valid_prefix_and_flags_the_rest() -> None:
    """`X1.2.3` is genuinely ambiguous; reporting `.3` is more useful than discarding the line."""
    line = tok("X1.2.3")
    assert words(line) == [("X", 1.2)]
    assert line.errors is not None
    assert line.errors[0].text == ".3"


def test_adjacent_stray_characters_merge_into_one_error() -> None:
    line = tok("G1 @@@ X10")
    assert words(line) == [("G", 1.0), ("X", 10.0)]
    assert len(line.errors or []) == 1
    assert line.errors[0].text == "@@@"


def test_stray_runs_separated_by_space_report_separately() -> None:
    line = tok("G1 @@ ## X10")
    assert [error.text for error in line.errors or ()] == ["@@", "##"]


def test_good_words_survive_alongside_a_bad_one() -> None:
    line = tok("G1 X10 $ Y20")
    assert words(line) == [("G", 1.0), ("X", 10.0), ("Y", 20.0)]
    assert len(line.errors or []) == 1


# --------------------------------------------------------------------------- source refs


def test_source_ref_is_created_once_per_line_and_shared() -> None:
    lines = tokenize("G1 X1\nG1 X2\n")
    assert lines[0].ref is not lines[1].ref
    assert (lines[0].ref.line_no, lines[1].ref.line_no) == (1, 2)


def test_offsets_are_absolute_into_the_document() -> None:
    text = "G21\nG1 X10\nM30\n"
    lines = tokenize(text)
    for line in lines:
        assert text[line.ref.start : line.ref.end] == text.splitlines()[line.ref.line_no - 1]


def test_crlf_offsets_account_for_both_characters() -> None:
    """A byte-counting editor and our SourceRef must agree, or editor sync drifts."""
    text = "G21\r\nG1 X10\r\n"
    lines = tokenize(text)
    assert lines[0].ref.start == 0
    assert lines[0].ref.end == 3  # 'G21', excluding the line ending
    assert lines[1].ref.start == 5  # after 'G21\r\n'
    assert text[lines[1].ref.start : lines[1].ref.end] == "G1 X10"


def test_error_offsets_are_absolute_too() -> None:
    """`X#` is two distinct problems, reported in positional order.

    The address has no value *and* the '#' is unrecognized. Both are worth saying: collapsing them
    would leave the user guessing which half of `X#` we objected to.
    """
    text = "G21\nG1 X#\n"
    lines = tokenize(text)
    assert [error.text for error in lines[1].errors] == ["X", "#"]
    for error in lines[1].errors:
        assert text[error.offset] == error.text, f"offset wrong for {error.text!r}"


def test_tokenize_returns_one_entry_per_line_including_blanks() -> None:
    assert len(tokenize("G21\n\nM30\n")) == 3


def test_final_line_without_newline_is_tokenized() -> None:
    lines = tokenize("G21\nM30")
    assert len(lines) == 2
    assert words(lines[1]) == [("M", 30.0)]
