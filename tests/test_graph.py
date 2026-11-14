"""P2: nets, drivers, consumers and traversal."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.graph import build_graph, link_partition, netid_partition
from ltp3.model import Terminal, Tree

CRLF = "\r\n"


def _text(*lines: str) -> bytes:
    return "".join(line + CRLF for line in lines).encode()


def _wire(
    wid: int, net: int, s: str | None, e: str | None, sw: str = "", ew: str = ""
) -> list[str]:
    out = [f"<Wire> {wid}, {net}"]
    for tag, end in (("S", s), ("E", e)):
        if end:
            pid, typ, num = end.split(":")
            out += [f"\t{tag}TermPID: {pid}", f"\t{tag}TermTyp: {typ}", f"\t{tag}TermNum: {num}"]
    if sw:
        out.append(f"\tSWires  : {sw}")
    if ew:
        out.append(f"\tEWires  : {ew}")
    out.append("\tPoints  : (0,0)(5,0)")
    return out


def _hdr() -> list[str]:
    return ["<Vers> 3.6.8", "<Tabs> 1, #FFFFFACD, T", "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF"]


def _refs(terms: Iterable[Terminal]) -> list[str]:
    return [t.ref for t in terms]


@pytest.fixture
def artix(sample_path: Path) -> Tree:
    return Tree.load(sample_path)


# ---------------------------------------------------------------- Artix-7


def test_artix_nets(artix: Tree) -> None:
    g = build_graph(artix.tabs[0])
    got = {(tuple(_refs(g.drivers(n))), tuple(sorted(_refs(g.consumers(n))))) for n in g.nets}
    assert got == {
        (("1:Output:1",), ("16:Input:1", "2:Input:1")),
        (("2:Output:1",), ("11:Input:1",)),
        (("2:Output:2",), ("11:Input:2",)),
        (("16:Output:1",), ("11:Input:3",)),
    }
    tab = artix.tabs[0]
    net = g.net_of(tab.terminal("11:Input:3"))
    assert net is not None and _refs(g.drivers(net)) == ["16:Output:1"]
    assert g.passives(net) == []
    assert g.net_of(tab.terminal("2:Input:1")) is g.net_of(tab.terminal("16:Input:1"))


def test_artix_traversal(artix: Tree) -> None:
    tab = artix.tabs[0]
    g = build_graph(tab)
    assert [p.id for p in g.topo_order()] == [1, 2, 16, 11]
    assert [p.id for p in g.downstream(tab.part(1))] == [2, 16]
    assert [p.id for p in g.upstream(tab.part(11))] == [2, 16]
    assert g.upstream(tab.part(1)) == []
    assert g.unconnected_terminals() == []
    assert g.dangling == []
    assert g.warnings == []


# ---------------------------------------------------------------- synthetic


def test_cycle_raises() -> None:
    data = _text(
        *_hdr(),
        "<BPrt> Converter, 1, 0, 0, A", "<Term> Input, 1", "<Term> Output, 1",
        "<BPrt> Converter, 2, 0, 0, B", "<Term> Input, 1", "<Term> Output, 1",
        *_wire(10, 5, "1:Output:1", "2:Input:1"),
        *_wire(11, 6, "2:Output:1", "1:Input:1"),
    )  # fmt: skip
    g = build_graph(Tree.from_bytes(data).tabs[0])
    with pytest.raises(Ltp3Error) as ei:
        g.topo_order()
    assert ei.value.code == errors.CYCLE


def _label_tree() -> Tree:
    # Power -> wire 10 -> label V; label V -> wire 11 -> Load: two wire nets, one electrical net
    data = _text(
        *_hdr(),
        "<BPrt> Power, 1, 0, 0, P", "<Term> Output, 1",
        "<BPrt> Net, 2, 0, 0, V", "<Term> Aux, 1",
        "<BPrt> Net, 3, 0, 0, V", "<Term> Aux, 1",
        "<BPrt> Load, 4, 0, 0, L", "<Term> Input, 1",
        *_wire(10, 5, "1:Output:1", "2:Aux:1"),
        *_wire(11, 6, "3:Aux:1", "4:Input:1"),
    )  # fmt: skip
    return Tree.from_bytes(data)


def test_net_label_merging() -> None:
    tab = _label_tree().tabs[0]
    off = build_graph(tab, merge_net_labels=False)
    on = build_graph(tab)  # default is True
    assert len(off.nets) == 2
    assert len(on.nets) == 1
    load_net = on.net_of(tab.terminal("4:Input:1"))
    assert load_net is not None
    assert _refs(on.drivers(load_net)) == ["1:Output:1"]
    assert _refs(on.passives(load_net)) == ["2:Aux:1", "3:Aux:1"]
    assert load_net.labels == ("V",)
    assert [p.id for p in on.downstream(tab.part(1))] == [4]
    assert off.downstream(tab.part(1)) == []


def test_dangling_and_disagreement_reported() -> None:
    data = _text(
        *_hdr(),
        "<BPrt> Power, 1, 0, 0, P", "<Term> Output, 1",
        "<BPrt> Load, 4, 0, 0, L", "<Term> Input, 1",
        *_wire(10, 5, "1:Output:1", "9:Input:1", ew="77"),
        *_wire(11, 6, None, "4:Input:1", sw="10"),
    )  # fmt: skip
    g = build_graph(Tree.from_bytes(data).tabs[0])
    assert {d[0] for d in g.dangling} == {"terminal", "link"}
    # netId says {10},{11}; links say {10,11}: warning, and the union is used
    assert len(g.nets) == 1
    assert "NET_PARTITION_MISMATCH" in g.warnings


# ---------------------------------------------------------------- corpus


def test_corpus_nets(template_paths: list[Path]) -> None:
    n_nets = unconnected = dangling = 0
    hist: Counter[str] = Counter()
    failed: list[str] = []
    for p in template_paths:
        tree = Tree.from_bytes(p.read_bytes())
        for tab in tree.tabs:
            try:
                g = build_graph(tab, merge_net_labels=False)
            except Exception as exc:  # collect, then fail with the list
                failed.append(f"{p.name}: {exc!r}")
                continue
            assert netid_partition(tab) == link_partition(tab), f"{p.name}: {tab.name}"
            assert "NET_PARTITION_MISMATCH" not in g.warnings
            n_nets += len(g.nets)
            unconnected += len(g.unconnected_terminals())
            dangling += len(g.dangling)
            for n in g.nets:
                d = len(g.drivers(n))
                hist["0" if d == 0 else "1" if d == 1 else "2" if d == 2 else ">=3"] += 1
    assert not failed, failed
    assert n_nets == 2188
    assert dict(hist) == {"0": 18, "1": 2109, "2": 40, ">=3": 21}
    assert unconnected == 299
    assert dangling == 0


def test_duplicate_wire_ids_tolerated(template_dir: Path) -> None:
    tree = Tree.load(template_dir / "Power Tree for ADGM1304.ltp3t")
    dup_tabs = 0
    for tab in tree.tabs:
        ids = [w.id for w in tab.wires]
        if len(ids) != len(set(ids)):
            dup_tabs += 1
            assert build_graph(tab).nets
    assert dup_tabs >= 1


def test_corpus_merged_and_topo(template_paths: list[Path]) -> None:
    for p in template_paths:
        for tab in Tree.from_bytes(p.read_bytes()).tabs:
            g = build_graph(tab)  # merge on: must never raise
            try:
                assert len(g.topo_order()) == len(tab.parts)
            except Ltp3Error as exc:
                assert exc.code == errors.CYCLE
