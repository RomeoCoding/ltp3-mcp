"""P2: typed views over the lossless Document."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.lossless import parse
from ltp3.model import Tree

CRLF = "\r\n"


def _doc_text(*lines: str) -> bytes:
    return "".join(line + CRLF for line in lines).encode()


def _lines(data: bytes) -> list[bytes]:
    return data.split(b"\r\n")


# ---------------------------------------------------------------- corpus census


def test_census_parts_and_terminals(template_paths: list[Path]) -> None:
    parts: Counter[str] = Counter()
    terms: Counter[str] = Counter()
    comma_parts = comma_tabs = n_tabs = 0
    for p in template_paths:
        tree = Tree.from_bytes(p.read_bytes())
        for tab in tree.tabs:
            n_tabs += 1
            comma_tabs += "," in tab.name
            ids = [part.id for part in tab.parts]
            assert len(ids) == len(set(ids)), f"{p.name}: duplicate part ids"
            for part in tab.parts:
                parts[part.type] += 1
                comma_parts += "," in part.name
                for t in part.terminals:
                    terms[f"{part.type}.{t.kind}"] += 1
    assert n_tabs == 444
    assert dict(parts) == {
        "Load": 1371, "LDO": 752, "Converter": 635, "Power": 470,
        "Capacitor": 432, "Resistor": 200, "Net": 44,
    }  # fmt: skip
    assert comma_parts == 77
    assert comma_tabs == 16
    assert terms["Power.Output"] == 470
    assert terms["Converter.Input"] == 635
    assert terms["Converter.Output"] == 1004
    assert terms["LDO.Input"] == 752
    assert terms["LDO.Output"] == 758
    assert terms["Load.Input"] == 1396


def test_comma_names_rejoin_exactly(template_paths: list[Path]) -> None:
    # Re-joining the parsed fields must reproduce each BPrt/Tabs argument string.
    for p in template_paths:
        tree = Tree.from_bytes(p.read_bytes())
        for tab in tree.tabs:
            assert tab.block.head.args == f"{tab.id}, {tab.color}, {tab.name}"
            for part in tab.parts:
                head = part.block.head.args
                assert head == f"{part.type}, {part.id}, {part.x}, {part.y}, {part.name}"


# ---------------------------------------------------------------- Artix-7 sample


@pytest.fixture
def artix(sample_path: Path) -> Tree:
    return Tree.from_bytes(sample_path.read_bytes())


def test_artix_structure(artix: Tree) -> None:
    assert len(artix.tabs) == 1
    tab = artix.tabs[0]
    assert tab.name == "New Tab 1"
    assert [(p.type, p.id) for p in tab.parts] == [
        ("Power", 1), ("Converter", 2), ("Load", 11), ("Converter", 16)
    ]  # fmt: skip
    assert len(tab.wires) == 6
    assert len(tab.symbols) == 10
    w = tab.wires[1]
    assert (w.id, w.net_id) == (18, 9)
    assert w.start is None
    assert w.end == (16, "Input", 1)
    assert w.start_links == [10, 19]
    assert w.end_links == []
    assert w.points == [(345, 295), (345, 460), (420, 460)]
    conv = tab.part(2)
    assert [t.ref for t in conv.terminals] == ["2:Input:1", "2:Output:1", "2:Output:2"]
    assert conv.terminals[2].props["NomEff"] == "91"
    assert tab.symbols[0].type == "Label"


def test_addressing(artix: Tree) -> None:
    assert artix.tab(0) is artix.tab("New Tab 1")
    tab = artix.tabs[0]
    assert tab.part("FPGA").id == 11
    assert tab.part("11") is tab.part(11)
    assert tab.terminal("2:Output:2").part is tab.part(2)
    assert tab.terminal("FPGA:Input:3").ref == "11:Input:3"
    for call, code in [
        (lambda: artix.tab(5), errors.UNKNOWN_TAB),
        (lambda: artix.tab("nope"), errors.UNKNOWN_TAB),
        (lambda: tab.part(99), errors.UNKNOWN_PART),
        (lambda: tab.part("nope"), errors.UNKNOWN_PART),
        (lambda: tab.terminal("2:Output:9"), errors.UNKNOWN_TERMINAL),
        (lambda: tab.terminal("garbage"), errors.UNKNOWN_TERMINAL),
    ]:
        with pytest.raises(Ltp3Error) as ei:
            call()
        assert ei.value.code == code


def test_ambiguous_name() -> None:
    data = _doc_text(
        "<Vers> 3.6.8",
        "<Tabs> 1, #FFFFFACD, T",
        "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF",
        "<BPrt> Load, 1, 0, 0, X",
        "<Term> Input, 1",
        "<BPrt> Load, 2, 0, 100, X",
        "<Term> Input, 1",
    )
    tab = Tree.from_bytes(data).tabs[0]
    with pytest.raises(Ltp3Error) as ei:
        tab.part("X")
    assert ei.value.code == errors.AMBIGUOUS_PART
    assert tab.part(2).name == "X"


def test_commas_in_names() -> None:
    data = _doc_text(
        "<Vers> 3.6.8",
        "<Tabs> 1, #FFFFFACD, ±15V VS, (from 5V/12Vsource)",
        "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF",
        "<BPrt> Power, 1, 65, 745, 5V, 12V, 24V Up to 40V",
        "<Term> Output, 1",
    )
    tab = Tree.from_bytes(data).tabs[0]
    assert tab.name == "±15V VS, (from 5V/12Vsource)"
    assert tab.parts[0].name == "5V, 12V, 24V Up to 40V"
    assert tab.parts[0].x == 65


def test_views_are_live(artix: Tree, sample_path: Path) -> None:
    before = _lines(sample_path.read_bytes())
    term = artix.tabs[0].terminal("1:Output:1")
    term.props["Vout"] = "4.5"
    after = _lines(artix.document.serialize())
    diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert diff == [(b"\tVout    : 5", b"\tVout    : 4.5")]
    # a fresh view over the same document sees the edit
    fresh = Tree.from_document(artix.document)
    assert fresh.tabs[0].terminal("1:Output:1").props["Vout"] == "4.5"


def test_part_setters_are_one_line(artix: Tree, sample_path: Path) -> None:
    before = _lines(sample_path.read_bytes())
    part = artix.tabs[0].part(2)
    part.name = "LTC3633, renamed"
    after = _lines(artix.document.serialize())
    changed = [b for a, b in zip(before, after, strict=True) if a != b]
    assert changed == [b"<BPrt> Converter, 2, 420, 230, LTC3633, renamed"]
    assert Tree.from_document(artix.document).tabs[0].part(2).name == "LTC3633, renamed"


def test_props_mapping_and_errors(artix: Tree) -> None:
    part = artix.tabs[0].part(2)
    assert list(part.props) == ["Color", "Width", "Height", "PartNumb"]  # unknown keys never hidden
    assert part.props["PartNumb"] == "LTC3633"
    assert "Width" in part.props and "Nope" not in part.props
    with pytest.raises(Ltp3Error) as ei:
        part.props["Nope"] = "1"
    assert ei.value.code == errors.UNKNOWN_KEY
    with pytest.raises(Ltp3Error) as ei:
        part.props["Width"] = "1\r\n2"
    assert ei.value.code == errors.BAD_VALUE


def test_unparseable_args_refused() -> None:
    data = _doc_text(
        "<Vers> 3.6.8",
        "<Tabs> 1, #FFFFFACD, T",
        "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF",
        "<BPrt> Load, 1, 0",
    )
    with pytest.raises(Ltp3Error) as ei:
        _ = Tree.from_bytes(data).tabs[0].parts[0].name
    assert ei.value.code == errors.UNPARSEABLE_ARGS


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as ei:
        Tree.load(tmp_path / "absent.ltp3")
    assert ei.value.code == errors.NOT_FOUND


def test_samples_load(sample_paths: list[Path]) -> None:
    for p in sample_paths:
        tree = Tree.load(p)
        assert tree.document.serialize() == p.read_bytes()
        assert parse(p.read_bytes()).serialize() == p.read_bytes()
