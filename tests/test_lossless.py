"""P1: lossless layer. Synthetic inputs are written inline; corpus files are read in place."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.lossless import (
    Block,
    Document,
    PropLine,
    RawLine,
    TagLine,
    parse,
    serialize,
    split_args,
)
from ltp3.paths import template_dir

CRLF = "\r\n"
BOM = b"\xef\xbb\xbf"


def _corpus_names() -> list[str]:
    d = template_dir()
    return sorted(p.name for p in d.glob("*.ltp3t")) if d.is_dir() else []


# ---------------------------------------------------------------- corpus round-trip


@pytest.mark.corpus
@pytest.mark.parametrize("name", _corpus_names())
def test_roundtrip_corpus(name: str) -> None:
    raw = (template_dir() / name).read_bytes()
    assert serialize(parse(raw)) == raw


def test_roundtrip_samples(sample_paths: list[Path]) -> None:
    for p in sample_paths:
        raw = p.read_bytes()
        assert serialize(parse(raw)) == raw, p.name


def test_census(template_paths: list[Path]) -> None:
    assert len(template_paths) == 261
    tags: Counter[str] = Counter()
    n_props = n_raw = n_tag_lines = n_bom = lone_cr = lone_cr_files = 0
    for p in template_paths:
        doc = parse(p.read_bytes())
        n_bom += doc.bom
        file_cr = 0
        for line in doc.lines:
            if isinstance(line, TagLine):
                tags[line.tag] += 1
                n_tag_lines += 1
                file_cr += line.args.count("\r")
            elif isinstance(line, PropLine):
                n_props += 1
                file_cr += line.value.count("\r")
            else:
                n_raw += 1
        lone_cr += file_cr
        lone_cr_files += file_cr > 0
    expected = {"Symb": 7364, "Term": 5891, "Wire": 4253, "BPrt": 3904, "Tabs": 444, "Summ": 444}
    assert {k: tags[k] for k in expected} == expected
    # FORMAT.md says 25,459: that count missed the BOM file first <Vers> (BOM precedes the "<")
    assert n_tag_lines == 25_460
    assert n_props == 79_556
    assert n_raw == 0
    assert n_bom == 1
    assert (lone_cr, lone_cr_files) == (10, 7)


def test_known_tag_args_split_on_templates(template_paths: list[Path]) -> None:
    """Every Tabs/BPrt/Term/Wire/Symb/Summ line must split without UNPARSEABLE_ARGS."""
    for p in template_paths:
        for line in parse(p.read_bytes()).lines:
            if isinstance(line, TagLine) and line.tag in {
                "Tabs", "BPrt", "Term", "Wire", "Symb", "Summ",
            }:  # fmt: skip
                split_args(line.tag, line.args)


# ---------------------------------------------------------------- synthetic cases


def _rt(data: bytes) -> Document:
    doc = parse(data)
    assert serialize(doc) == data
    return doc


def test_bom_preserved() -> None:
    doc = _rt(BOM + b"<Vers> 3.3.4\r\n")
    assert doc.bom is True and doc.encoding == "utf-8"
    assert _rt(b"<Vers> 3.3.4\r\n").bom is False


def test_lone_cr_inside_value() -> None:
    doc = _rt(b"<Tabs> 1, #FF, T\r\n\tComments: Amplifier\r\r\n<Summ> 0, 0, 1, #A, #B\r\n")
    prop = doc.lines[1]
    assert isinstance(prop, PropLine)
    assert prop.value == "Amplifier\r" and prop.eol == CRLF


def test_dsnn_without_space() -> None:
    doc = _rt(b"<DS11>0.5\r\n<DS05>\r\n")
    a, b = doc.lines
    assert isinstance(a, TagLine) and (a.tag, a.sep, a.args) == ("DS11", "", "0.5")
    assert isinstance(b, TagLine) and (b.tag, b.sep, b.args) == ("DS05", "", "")


def test_note_with_trailing_space() -> None:
    doc = _rt(b"<Note> \r\n")
    (a,) = doc.lines
    assert isinstance(a, TagLine) and (a.tag, a.sep, a.args) == ("Note", " ", "")


def test_empty_value_with_trailing_space() -> None:
    doc = _rt(b"\tTitle   : \r\n")
    (a,) = doc.lines
    assert isinstance(a, PropLine)
    assert (a.key, a.key_raw, a.sep, a.value) == ("Title", "Title   ", " ", "")


def test_value_padding_and_colon_preserved() -> None:
    doc = _rt(b"\tPartNumb: LTC3877     \r\n\tComments: a: b, c\r\n\tVout    :5\r\n")
    vals = [(line.key, line.sep, line.value) for line in doc.lines if isinstance(line, PropLine)]
    assert vals == [
        ("PartNumb", " ", "LTC3877     "),
        ("Comments", " ", "a: b, c"),
        ("Vout", "", "5"),
    ]


def test_lf_only_file() -> None:
    doc = _rt(b"<Vers> 3.3.4\n<Tabs> 1, #FF, T\n\tKey     : v\n")
    assert [line.eol for line in doc.lines] == ["\n"] * 3


def test_mixed_eol_and_missing_final_eol() -> None:
    doc = _rt(b"<Vers> 1\r\n<Note> \n\tKey     : v")
    assert [line.eol for line in doc.lines] == [CRLF, "\n", ""]


def test_unknown_tag_is_kept() -> None:
    doc = _rt(b"<Zzzz> some, args\r\n\tOdd     : 1\r\n")
    a = doc.lines[0]
    assert isinstance(a, TagLine) and a.tag == "Zzzz" and a.args == "some, args"


@pytest.mark.parametrize(
    "text",
    [
        "garbage line",
        " leading space",
        "\t",
        "\tnocolon",
        "\ttoo long key name: 1",
        "<unterminated",
        "<> empty tag",
        "",
    ],
)
def test_unknown_shape_becomes_rawline(text: str) -> None:
    doc = _rt((text + CRLF).encode())
    (line,) = doc.lines
    assert isinstance(line, RawLine) and line.text == text


def test_empty_file_and_blank_lines() -> None:
    assert parse(b"").lines == []
    assert serialize(parse(b"")) == b""
    assert len(_rt(b"\r\n\r\n").lines) == 2


def test_cp1252_fallback() -> None:
    data = b"<Vers> 1\r\n\tComments: caf\xe9 \x81\r\n"
    doc = _rt(data)
    assert doc.encoding == "cp1252"
    assert "café" in doc.lines[1].serialize()


def test_utf8_non_ascii() -> None:
    data = "\tComments: 5°C ±15V\r\n".encode()
    doc = _rt(data)
    assert doc.encoding == "utf-8"


def test_edit_one_value_changes_one_line() -> None:
    data = b"<Vers> 1\r\n<Term> Output, 1\r\n\tVout    : 5\r\n\tVoutMax : 6\r\n"
    doc = parse(data)
    prop = doc.lines[2]
    assert isinstance(prop, PropLine)
    prop.value = "3.3"
    out = serialize(doc)
    assert out == b"<Vers> 1\r\n<Term> Output, 1\r\n\tVout    : 3.3\r\n\tVoutMax : 6\r\n"


# ---------------------------------------------------------------- blocks and arg splitting

_SAMPLE_DOC = (
    "<Vers> 3.3.4\r\n"
    "<Note> \r\n"
    "<Tabs> 6, #FFFFFFFF, ±15V VS, (from 5V/12Vsource)\r\n"
    "<Summ> 0, 410, 18, #FF000000, #00FFFFFF\r\n"
    "<BPrt> Power, 1, 65, 745, 5V, 12V, 24V Up to 40V\r\n"
    "\tColor   : #00FFFFFF\r\n"
    "\tWidth   : 110\r\n"
    "<Term> Output, 1\r\n"
    "\tVout    : 5\r\n"
    "<BPrt> Load, 2, 300, 745, Load\r\n"
    "<Term> Input, 1\r\n"
    "\tIpeak   : 1\r\n"
    "<Symb> Label, 3, 10, 20, 0,0, 0,0\r\n"
    "\tComments: 1.0V CORE\r\n"
    "<Wire> 9, 4\r\n"
    "\tPoints  : (1,2)(3,2)\r\n"
    "<Tabs> 5, #FFFFFFFF, Second\r\n"
    "<Summ> 0, 0, 18, #FF000000, #00FFFFFF\r\n"
)


def test_blocks_group_props_under_their_tag() -> None:
    doc = parse(_SAMPLE_DOC.encode())
    blocks = doc.blocks()
    assert [b.head.tag for b in blocks] == [
        "Vers", "Note", "Tabs", "Summ", "BPrt", "Term", "BPrt", "Term", "Symb", "Wire", "Tabs",
        "Summ",
    ]  # fmt: skip
    bprt = blocks[4]
    assert isinstance(bprt, Block)
    assert [p.key for p in bprt.props] == ["Color", "Width"]
    found = bprt.find("Width")
    assert found is not None and found.value == "110" and bprt.find("Nope") is None


def test_block_props_are_live_lines() -> None:
    doc = parse(_SAMPLE_DOC.encode())
    term = doc.blocks()[5]
    prop = term.find("Vout")
    assert prop is not None
    prop.value = "3.3"
    assert serialize(doc) == _SAMPLE_DOC.replace("Vout    : 5", "Vout    : 3.3").encode()


def test_split_tabs_header_and_tabs() -> None:
    doc = parse(_SAMPLE_DOC.encode())
    header, tabs = doc.split_tabs()
    assert [b.head.tag for b in header] == ["Vers", "Note"]
    assert len(tabs) == 2
    assert [b.head.tag for b in tabs[0]][:2] == ["Tabs", "Summ"]
    assert len(tabs[0]) == 8 and len(tabs[1]) == 2


def test_split_args_names_with_commas() -> None:
    assert split_args("BPrt", "Power, 1, 65, 745, 5V, 12V, 24V Up to 40V") == [
        "Power", "1", "65", "745", "5V, 12V, 24V Up to 40V",
    ]  # fmt: skip
    assert split_args("Tabs", "6, #FFFFFFFF, ±15V VS, (from 5V/12Vsource)") == [
        "6", "#FFFFFFFF", "±15V VS, (from 5V/12Vsource)",
    ]  # fmt: skip
    assert split_args("Term", "Output, 1") == ["Output", "1"]
    assert split_args("Wire", "9, 4") == ["9", "4"]
    assert split_args("Summ", "0, 410, 18, #FF000000, #00FFFFFF") == [
        "0", "410", "18", "#FF000000", "#00FFFFFF",
    ]  # fmt: skip
    assert split_args("Symb", "Label, 3, 10, 20, 0,0, 0,0") == [
        "Label", "3", "10", "20", "0,0", "0,0",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("tag", "args"),
    [("BPrt", "Power, 1, 65"), ("Tabs", "6"), ("Term", "Output"), ("Wire", ""), ("Zzzz", "a, b")],
)
def test_split_args_refuses_short_or_unknown(tag: str, args: str) -> None:
    with pytest.raises(Ltp3Error) as ei:
        split_args(tag, args)
    assert ei.value.code == errors.UNPARSEABLE_ARGS


# ---------------------------------------------------------------- hypothesis

_text = st.text(
    alphabet=st.characters(blacklist_characters="\r\n", blacklist_categories=("Cs",)),
    max_size=20,
)
_eol = st.sampled_from(["\r\n", "\n", "\r\n", "\r\n"])
_key = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_",
    min_size=1,
    max_size=8,
)
_tag = st.from_regex(r"[A-Za-z][A-Za-z0-9]{0,5}", fullmatch=True)
_value = st.one_of(_text, _text.map(lambda s: s + "\r"))  # lone CR as data


@st.composite
def _line_text(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["tag", "tagnosp", "prop", "propnosp", "raw"]))
    if kind == "tag":
        return f"<{draw(_tag)}> {draw(_text)}"
    if kind == "tagnosp":
        return f"<{draw(_tag)}>{draw(_text)}"
    if kind in {"prop", "propnosp"}:
        key = draw(_key)
        sep = " " if kind == "prop" else ""
        return f"\t{key:<8}:{sep}{draw(_value)}"
    return draw(_text)


@st.composite
def _documents(draw: st.DrawFn) -> bytes:
    n = draw(st.integers(0, 12))
    parts = [draw(_line_text()) + draw(_eol) for _ in range(n)]
    if parts and draw(st.booleans()):
        parts[-1] = parts[-1].rstrip("\r\n")  # no final terminator
    body = "".join(parts).encode("utf-8")
    return (BOM + body) if draw(st.booleans()) else body


@settings(max_examples=300, deadline=None)
@given(_documents())
def test_hypothesis_grammar_roundtrip(data: bytes) -> None:
    assert serialize(parse(data)) == data


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=200))
def test_hypothesis_arbitrary_bytes_roundtrip(data: bytes) -> None:
    assert serialize(parse(data)) == data


@settings(max_examples=200, deadline=None)
@given(_documents(), st.data())
def test_hypothesis_edit_changes_exactly_one_line(data: bytes, draw: st.DataObject) -> None:
    doc = parse(data)
    props = [i for i, line in enumerate(doc.lines) if isinstance(line, PropLine)]
    if not props:
        return
    idx = draw.draw(st.sampled_from(props))
    new_value = draw.draw(_text)
    before = [line.serialize() for line in doc.lines]
    line = doc.lines[idx]
    assert isinstance(line, PropLine)
    line.value = new_value
    after = [line.serialize() for line in doc.lines]
    assert [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b] in ([idx], [])
    assert serialize(doc) == (BOM if doc.bom else b"") + "".join(after).encode(doc.encoding)
    # the re-parse must see the same structure, with only that value different
    again = parse(serialize(doc))
    assert len(again.lines) == len(doc.lines)
    again_line = again.lines[idx]
    # "key:" + value with no separator and a leading space re-parses as sep " ": compare bytes
    assert isinstance(again_line, PropLine) and again_line.serialize() == line.serialize()
