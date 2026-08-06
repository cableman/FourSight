"""File loader tests (T1.4).

The DoD case list — UTF-8, UTF-8 BOM, latin-1, CRLF, no trailing newline — is checked end to end
through the tokenizer, because "correct offsets" only means anything if the offsets the *parser*
produces still index back into the loaded text.
"""

import codecs
from pathlib import Path

import pytest

from foursight.fileio.loader import (
    FALLBACK_ENCODING,
    FileLoadError,
    LoadedFile,
    detect_newline,
    line_starts,
    load,
    load_text,
)
from foursight.parser.tokenizer import tokenize

PROGRAM = "G21 G90\nG1 X10 F100\nM30\n"


def check_offsets_round_trip(loaded: LoadedFile) -> None:
    """Every SourceRef must slice its own line back out of the loaded text.

    This is the assertion that actually matters: if it holds, editor sync, diagnostics and fixes all
    have a trustworthy anchor. Line numbers alone would not catch a BOM shifting everything by one.
    """
    expected = loaded.text.splitlines()
    for line in tokenize(loaded.text):
        sliced = loaded.text[line.ref.start : line.ref.end]
        assert sliced == expected[line.ref.line_no - 1], f"line {line.ref.line_no} offset wrong"


# --------------------------------------------------------------------------- encodings


def test_plain_utf8() -> None:
    loaded = load_text(PROGRAM.encode("utf-8"))
    assert loaded.text == PROGRAM
    assert loaded.encoding == "utf-8"
    assert loaded.had_bom is False
    assert not loaded.used_fallback
    check_offsets_round_trip(loaded)


def test_utf8_bom_is_stripped_so_offsets_are_not_shifted() -> None:
    """Left in place, the BOM would make line 1 start with an invisible unparseable character."""
    loaded = load_text(codecs.BOM_UTF8 + PROGRAM.encode("utf-8"))
    assert loaded.text == PROGRAM
    assert loaded.text[0] == "G", "BOM leaked into the text"
    assert loaded.had_bom is True
    assert loaded.encoding == "utf-8"
    check_offsets_round_trip(loaded)
    # And the tokenizer must see a clean first line, not a stray character.
    assert not tokenize(loaded.text)[0].has_errors


def test_utf8_with_non_ascii_comment() -> None:
    text = "(Größe: 6mm)\nG1 X1 F10\n"
    loaded = load_text(text.encode("utf-8"))
    assert loaded.encoding == "utf-8"
    assert tokenize(loaded.text)[0].comments == ["Größe: 6mm"]


def test_latin1_fallback_when_not_valid_utf8() -> None:
    """A latin-1 degree sign is invalid UTF-8; falling back beats refusing the file."""
    raw = "(temp 20\xb0C)\nG1 X1 F10\n".encode(FALLBACK_ENCODING)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # precondition: this really is not UTF-8
    loaded = load_text(raw)
    assert loaded.encoding == FALLBACK_ENCODING
    assert loaded.used_fallback is True
    assert "20\xb0C" in loaded.text
    check_offsets_round_trip(loaded)


def test_utf16_bom_is_detected() -> None:
    loaded = load_text(codecs.BOM_UTF16_LE + PROGRAM.encode("utf-16-le"))
    assert loaded.text == PROGRAM
    assert loaded.encoding == "utf-16-le"
    assert loaded.had_bom is True
    check_offsets_round_trip(loaded)


def test_utf16_big_endian_bom_is_detected() -> None:
    loaded = load_text(codecs.BOM_UTF16_BE + PROGRAM.encode("utf-16-be"))
    assert loaded.text == PROGRAM
    assert loaded.encoding == "utf-16-be"


def test_binary_input_is_refused_rather_than_decoded_as_garbage() -> None:
    """A mis-opened STL should not produce thousands of meaningless diagnostics."""
    with pytest.raises(FileLoadError, match="binary"):
        load_text(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")


def test_utf16_is_not_mistaken_for_binary() -> None:
    """UTF-16 text is full of NUL bytes; the check must run on the decoded text."""
    loaded = load_text(codecs.BOM_UTF16_LE + PROGRAM.encode("utf-16-le"))
    assert loaded.text == PROGRAM


# --------------------------------------------------------------------------- line endings


def test_crlf_is_preserved_not_normalized() -> None:
    """Rewriting CRLF to LF would shift every offset relative to the text we hand the editor."""
    loaded = load_text(PROGRAM.replace("\n", "\r\n").encode("utf-8"))
    assert "\r\n" in loaded.text
    assert loaded.newline == "\r\n"
    check_offsets_round_trip(loaded)


def test_crlf_offsets_match_the_tokenizer() -> None:
    loaded = load_text(b"G21\r\nG1 X10\r\n")
    lines = tokenize(loaded.text)
    assert lines[1].ref.start == 5  # after 'G21\r\n'
    assert loaded.text[lines[1].ref.start : lines[1].ref.end] == "G1 X10"


def test_no_trailing_newline() -> None:
    loaded = load_text(b"G21 G90\nM30")
    lines = tokenize(loaded.text)
    assert len(lines) == 2
    assert lines[1].ref.end == len(loaded.text)
    check_offsets_round_trip(loaded)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\nb\n", "\n"),
        ("a\r\nb\r\n", "\r\n"),
        ("a\rb\r", "\r"),
        ("no endings", ""),
        ("", ""),
        ("a\r\nb\r\nc\n", "\r\n"),  # mixed: reported by its dominant convention
    ],
)
def test_detect_newline(text: str, expected: str) -> None:
    assert detect_newline(text) == expected


# --------------------------------------------------------------------------- line offsets


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("a", [0]),
        ("a\n", [0]),
        ("a\nb", [0, 2]),
        ("a\nb\n", [0, 2]),
        ("a\n\nb\n", [0, 2, 3]),
        ("a\r\nb\r\n", [0, 3]),
    ],
)
def test_line_starts(text: str, expected: list[int]) -> None:
    assert line_starts(text) == expected


def test_line_starts_agrees_with_the_tokenizer() -> None:
    """Two independent offset computations must not drift apart."""
    text = "G21\n\nG1 X1 F10\n(c)\nM30\n"
    starts = line_starts(text)
    assert len(starts) == len(text.splitlines())
    for line in tokenize(text):
        assert starts[line.ref.line_no - 1] == line.ref.start


def test_line_starts_indexes_by_one_based_line_number() -> None:
    text = "aa\nbb\ncc\n"
    starts = line_starts(text)
    assert text[starts[3 - 1] :].startswith("cc")


# --------------------------------------------------------------------------- filesystem


def test_load_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "part.nc"
    path.write_bytes(PROGRAM.encode("utf-8"))
    loaded = load(path)
    assert loaded.text == PROGRAM
    assert loaded.path == path
    check_offsets_round_trip(loaded)


def test_load_accepts_a_string_path(tmp_path: Path) -> None:
    path = tmp_path / "part.nc"
    path.write_bytes(b"G21\n")
    assert load(str(path)).text == "G21\n"


def test_missing_file_raises_oserror(tmp_path: Path) -> None:
    """A missing file is the caller's problem, not something to report as a diagnostic."""
    with pytest.raises(OSError):
        load(tmp_path / "nope.nc")


def test_empty_file_is_valid_and_yields_nothing() -> None:
    loaded = load_text(b"")
    assert loaded.text == ""
    assert tokenize(loaded.text) == []
    assert line_starts(loaded.text) == []


@pytest.mark.parametrize(
    "text",
    [
        "G21\nG1 X1\nM30\n",
        "G21\r\nG1 X1\r\nM30\r\n",
        "G21\rG1 X1\rM30\r",  # classic Mac endings, which detect_newline also claims to handle
        "G21\r\nG1 X1\nM30\r",  # mixed
        "G21",
        "",
        "\n\n\n",
    ],
)
def test_line_starts_never_disagrees_with_the_tokenizer(text: str) -> None:
    """Regression: the two offset computations must agree for every line ending style.

    A hand-rolled scan for '\\n' missed lone-'\\r' endings, which `splitlines` does split on, so a
    classic-Mac file reported one line here and three in the tokenizer. `line_starts` now derives
    from the same primitive, so agreement holds by construction rather than by coincidence.
    """
    starts = line_starts(text)
    assert [line.ref.start for line in tokenize(text)] == starts
    assert len(starts) == len(text.splitlines())


def test_cr_only_file_tokenizes_with_correct_offsets() -> None:
    text = "G21\rG1 X1\rM30\r"
    assert detect_newline(text) == "\r"
    assert line_starts(text) == [0, 4, 10]
    for line in tokenize(text):
        assert text[line.ref.start : line.ref.end] == text.splitlines()[line.ref.line_no - 1]
