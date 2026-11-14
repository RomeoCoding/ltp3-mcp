"""P4: editor. Every mutation must keep Document.check() green and re-parse to the same model."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ltp3 import errors
from ltp3.editor import Editor, changed_lines
from ltp3.errors import Ltp3Error
from ltp3.graph import build_graph
from ltp3.lossless import parse, serialize
from ltp3.model import Tab, Tree
from ltp3.numfmt import fmt
from ltp3.paths import template_dir


def _corpus_names() -> list[str]:
    d = template_dir()
    return sorted(p.name for p in d.glob("*.ltp3t")) if d.is_dir() else []


# ------------------------------------------------------------------ shared invariants


def assert_invariants(data: bytes, baseline: bytes | None = None) -> Tree:
    """FORMAT.md section 5 invariants on every tab of `data`; returns the re-parsed tree."""
    doc = parse(data)
    doc.check()
    tree = Tree(doc)
    for tab in tree.tabs:
        _tab_invariants(tab, _crowded(baseline))
    return tree


def _tab_invariants(tab: Tab, crowded: frozenset[tuple[int, str, int]] = frozenset()) -> None:
    pids = [p.id for p in tab.parts]
    assert len(pids) == len(set(pids)), "duplicate part ids"
    by_id: dict[int, list[int]] = {}
    for i, w in enumerate(tab.wires):
        by_id.setdefault(w.id, []).append(i)
    per_terminal: dict[tuple[int, str, int], int] = {}
    for w in tab.wires:
        pts = w.points
        assert pts, f"wire {w.id} has no points"
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in itertools.pairwise(pts)), (
            f"wire {w.id} has a diagonal segment"
        )
        for end, links, point in (
            (w.start, w.start_links, pts[0]),
            (w.end, w.end_links, pts[-1]),
        ):
            assert end is not None or links, f"wire {w.id} has a dangling end"
            if end is not None:
                assert end[0] in pids
                per_terminal[end] = per_terminal.get(end, 0) + 1
            for other_id in links:
                partners = [tab.wires[j] for j in by_id[other_id]]
                ok = False
                for o in partners:
                    opts = o.points
                    for olinks, opoint in ((o.start_links, opts[0]), (o.end_links, opts[-1])):
                        if w.id in olinks and opoint == point:
                            ok = True
                assert ok, f"wire {w.id} lists {other_id} but not reciprocally at {point}"
    for end, count in per_terminal.items():
        if end[1] in ("Input", "Output") and end not in crowded:
            assert count == 1, f"terminal {end} carries {count} wires"


def _subsequence(small: Sequence[str], big: Sequence[str]) -> bool:
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


def _lines(data: bytes) -> list[str]:
    return data.decode().split("\r\n")


@pytest.fixture
def sample(sample_path: Path) -> bytes:
    return sample_path.read_bytes()


# ------------------------------------------------------------------ minimal diff


def test_set_existing_value_changes_one_line(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    old, new = ed.set_property("1:Output:1", "Vout", 3.3)
    assert (old, new) == ("5", "3.3")
    after = ed.serialize()
    assert changed_lines(sample, after) == {"added": 0, "removed": 0, "modified": 1}
    assert_invariants(after)


def test_set_equal_number_is_noop(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("1:Output:1", "Vout", "5.000")
    ed.set_property("1:Output:1", "Vout", 5)
    assert ed.serialize() == sample
    assert changed_lines(sample, ed.serialize()) == {"added": 0, "removed": 0, "modified": 0}


def test_numbers_are_formatted_dotnet_style(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("1:Output:1", "Vout", 0.00001)
    assert ed.tree.tabs[0].terminal("1:Output:1").props["Vout"] == "1E-05"
    ed.set_property("1:Output:1", "Vout", 0.1 + 0.2)
    assert ed.tree.tabs[0].terminal("1:Output:1").props["Vout"] == "0.3"


def test_add_key_inserts_one_line_at_canonical_position(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("2:Output:1", "IoutMax", 3)
    after = ed.serialize()
    assert changed_lines(sample, after) == {"added": 1, "removed": 0, "modified": 0}
    keys = list(ed.tree.tabs[0].terminal("2:Output:1").props)
    assert keys == [
        "Vout",
        "IoutMax",
        "NomEff",
    ]  # Vout, VoutMin, VoutMax, Dropout, IoutMin, IoutMax, NomEff
    ed.set_property("2:Output:1", "VoutMin", 0.9)
    assert list(ed.tree.tabs[0].terminal("2:Output:1").props) == [
        "Vout",
        "VoutMin",
        "IoutMax",
        "NomEff",
    ]


def test_add_key_to_keyless_terminal_and_part(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("2:Input:1", "VinMax", 6)
    ed.set_property("2:Input:1", "VinMin", 2.5)
    assert list(ed.tree.tabs[0].terminal("2:Input:1").props) == ["VinMin", "VinMax"]
    ed.set_property("2", "VisType", 1)
    ed.set_property("2", "Comments", "line one\nline two")
    props = ed.tree.tabs[0].part(2).props
    assert list(props) == ["Color", "Width", "Height", "PartNumb", "VisType", "Comments"]
    assert props["Comments"] == "line one*^!#line two"  # newline token of FORMAT.md 4
    assert_invariants(ed.serialize())


def test_remove_key_removes_one_line(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    old, new = ed.set_property("2:Output:1", "NomEff", None)
    assert (old, new) == ("80", None)
    assert changed_lines(sample, ed.serialize()) == {"added": 0, "removed": 1, "modified": 0}
    # removing an absent key is a no-op
    assert ed.set_property("2:Output:1", "NomEff", None) == (None, None)


def test_identical_lines_in_different_blocks_stay_independent(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    # parts 2 and 16 both carry "\tColor   : #FFAFEEEE": editing one must not touch the other
    ed.set_property("2", "Color", "#FF112233")
    tab = ed.tree.tabs[0]
    assert tab.part(2).props["Color"] == "#FF112233"
    assert tab.part(16).props["Color"] == "#FFAFEEEE"


def test_unknown_key_rejected_unless_allowed(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property("2", "Bogus", 1)
    assert exc.value.code == errors.UNKNOWN_KEY
    ed.set_property("2", "Bogus", 1, allow_unknown_key=True)
    assert ed.tree.tabs[0].part(2).props["Bogus"] == "1"
    assert_invariants(ed.serialize())


@pytest.mark.parametrize("bad", ["a\r\nb", "a\rb", "a\nb"])
def test_crlf_in_values_rejected(sample: bytes, bad: str) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property("2", "PartNumb", bad)  # LF is legal only in Comments/Title (as *^!#)
    assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


def test_non_numeric_value_for_numeric_key_rejected(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    for key, value in (("Vout", "five"), ("Vout", "1,5"), ("NomEff", "")):
        with pytest.raises(Ltp3Error) as exc:
            ed.set_property("2:Output:1", key, value)
        assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


def test_rename_part(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    assert ed.rename_part("2", "BUCK, 1V/3.3V") == ("LTC3633 BUCK", "BUCK, 1V/3.3V")
    assert ed.tree.tabs[0].part(2).name == "BUCK, 1V/3.3V"  # commas are legal in names
    assert changed_lines(sample, ed.serialize()) == {"added": 0, "removed": 0, "modified": 1}
    with pytest.raises(Ltp3Error) as exc:
        ed.rename_part("2", "bad\nname")
    assert exc.value.code == errors.BAD_VALUE
    assert_invariants(ed.serialize())


@settings(max_examples=40, deadline=None)
@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_property_edit_reparses_to_same_value(sample_path: Path, value: float) -> None:
    ed = Editor.from_bytes(sample_path.read_bytes())
    ed.set_property("1:Output:1", "Vout", value)
    again = Tree.from_bytes(ed.serialize())
    assert again.tabs[0].terminal("1:Output:1").props["Vout"] == fmt(value)


# ------------------------------------------------------------------ add_part / connect


def _add_load(ed: Editor, name: str = "NEW LOAD", near: str | None = "16") -> int:
    part = ed.add_part("Load", name, inputs=[{"ipeak": 1.0}], near=near)
    return part.id


def test_add_part_writes_minimal_block(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    part = ed.add_part("Load", "NEW LOAD", inputs=[{"ipeak": 1.5, "inom": 1.0, "unit_exp": -3}])
    assert part.id == 33  # max id in the tab is 32 (wire 32; net 31; symbols 24..26)
    after = ed.serialize()
    tree = assert_invariants(after)
    p = tree.tabs[0].part(part.id)
    assert p.type == "Load"
    assert list(p.props) == ["Color", "Width", "Height"]
    assert p.props["Color"] == "#FFFFB6C1"
    assert p.props["Height"] == "60"
    assert p.x % 5 == 0 and p.y % 5 == 0
    t = p.terminal("Input", 1)
    assert dict(t.props) == {"Inom": "1", "InomU": "-3", "Ipeak": "1.5", "IpeakU": "-3"}
    # the part sits after the last existing part and before the first symbol
    lines = _lines(after)
    first_symb = next(i for i, ln in enumerate(lines) if ln.startswith("<Symb>"))
    new_bprt = next(i for i, ln in enumerate(lines) if ln.startswith(f"<BPrt> Load, {part.id},"))
    assert new_bprt < first_symb
    assert changed_lines(sample, after)["removed"] == 0
    assert changed_lines(sample, after)["modified"] == 0


def test_add_part_types_and_heights(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    conv = ed.add_part(
        "Converter",
        "NEW BUCK",
        part_number="LTC9999",
        outputs=[{"vout": 1.2, "nom_eff": 90, "iout_max": 2}, {"vout": 3.3}],
    )
    ldo = ed.add_part("LDO", "NEW LDO", part_number="LT1234", outputs=[{"vout": 1.8}])
    pwr = ed.add_part("Power", "12V", outputs=[{"vout": 12}])
    res = ed.add_part("Resistor", "R1", resistance=2.2)
    cap = ed.add_part("Capacitor", "C1")
    tree = assert_invariants(ed.serialize())
    tab = tree.tabs[0]
    c = tab.part(conv.id)
    assert c.props["Height"] == "120" and c.props["Width"] == "180"
    assert c.props["Color"] == "#FFAFEEEE" and c.props["PartNumb"] == "LTC9999"
    assert [(t.kind, t.n) for t in c.terminals] == [("Input", 1), ("Output", 1), ("Output", 2)]
    assert dict(c.terminal("Output", 1).props) == {"Vout": "1.2", "IoutMax": "2", "NomEff": "90"}
    assert tab.part(ldo.id).props["Width"] == "180"
    assert tab.part(pwr.id).props["Color"] == "#00FFFFFF"
    assert tab.part(res.id).props["Resstnce"] == "2.2"
    assert [t.kind for t in tab.part(cap.id).terminals] == ["Aux"]
    ids = [p.id for p in tab.parts]
    assert len(ids) == len(set(ids))
    rects = [(p.x, p.y, int(p.props["Width"]), int(p.props["Height"])) for p in tab.parts]
    for i, a in enumerate(rects):  # no two parts overlap
        for b in rects[i + 1 :]:
            assert not (
                a[0] < b[0] + b[2]
                and b[0] < a[0] + a[2]
                and a[1] < b[1] + b[3]
                and b[1] < a[1] + a[3]
            )


def test_add_part_rejects_bad_input(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    for kwargs in (
        {"type": "Net", "name": "x"},
        {"type": "Gizmo", "name": "x"},
        {"type": "Load", "name": "a\nb"},
        {"type": "Load", "name": "ok", "inputs": [{"bogus": 1}]},
    ):
        with pytest.raises(Ltp3Error) as exc:
            ed.add_part(**kwargs)  # type: ignore[arg-type]
        assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


def test_connect_to_wired_output_splits_wire(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    load_id = _add_load(ed)
    mid = ed.serialize()
    res = ed.connect("16:Output:1", f"{load_id}:Input:1")
    after = ed.serialize()
    tree = assert_invariants(after)
    tab = tree.tabs[0]
    assert len(res.wires_added) == 2 and res.wires_modified == [32]
    trunk = next(w for w in tab.wires if w.id == 32)
    assert trunk.start == (16, "Output", 1) and trunk.end is None
    assert sorted(trunk.end_links) == sorted(res.wires_added)
    branches = [w for w in tab.wires if w.id in res.wires_added]
    junction = trunk.points[-1]
    assert len(trunk.points) == 2
    ends = set()
    for b in branches:
        assert b.start is None and b.points[0] == junction
        assert sorted(b.start_links) == sorted([32, *[x for x in res.wires_added if x != b.id]])
        assert b.net_id == trunk.net_id == res.net_id == 31
        assert b.end is not None
        ends.add(b.end)
    assert ends == {(11, "Input", 3), (load_id, "Input", 1)}
    assert [p for p in branches[0].props][:1] == ["ZIndex"]  # the sample already uses ZIndex
    g = build_graph(tab)
    net = g.net_of(tab.terminal("16:Output:1"))
    assert net is not None
    assert {t.ref for t in g.consumers(net)} == {"11:Input:3", f"{load_id}:Input:1"}
    # everything except the split wire's own lines is untouched (and still in order)
    old = _lines(sample)
    split_start = old.index("<Wire> 32, 31")
    assert _subsequence(old[:split_start], _lines(after))
    assert _subsequence(_lines(mid)[:split_start], _lines(after))


def test_remove_part_undoes_connect_exactly(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    load_id = _add_load(ed)
    ed.connect("16:Output:1", f"{load_id}:Input:1")
    res = ed.remove_part(load_id)
    assert res.merged_junctions == 1
    assert ed.serialize() == sample  # byte-identical, hence graph-identical
    g0 = build_graph(Tree.from_bytes(sample).tabs[0])
    g1 = build_graph(ed.tree.tabs[0])
    assert [n.refs for n in g0.nets] == [n.refs for n in g1.nets]


def test_connect_extends_existing_junction(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    load_id = _add_load(ed, near="2")
    res = ed.connect("1:Output:1", f"{load_id}:Input:1")
    tree = assert_invariants(ed.serialize())
    tab = tree.tabs[0]
    assert len(res.wires_added) == 1 and sorted(res.wires_modified) == [10, 18, 19]
    new = next(w for w in tab.wires if w.id == res.wires_added[0])
    assert sorted(new.start_links) == [10, 18, 19]
    for wid in (10, 18, 19):
        w = next(x for x in tab.wires if x.id == wid)
        assert new.id in (w.end_links if wid == 10 else w.start_links)
    assert new.points[0] == (345, 295)
    assert {w.net_id for w in tab.wires if w.id in (10, 18, 19, new.id)} == {9}
    # removing the new load restores the original bytes
    ed.remove_part(load_id)
    assert ed.serialize() == sample


def test_connect_unwired_output_makes_single_wire(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    src = ed.add_part("Converter", "BUCK B", outputs=[{"vout": 1}])
    load_id = _add_load(ed, near=str(src.id))
    res = ed.connect(f"{src.id}:Output:1", f"{load_id}:Input:1")
    tree = assert_invariants(ed.serialize())
    tab = tree.tabs[0]
    assert len(res.wires_added) == 1 and res.wires_modified == []
    w = next(x for x in tab.wires if x.id == res.wires_added[0])
    assert w.start == (src.id, "Output", 1) and w.end == (load_id, "Input", 1)
    assert w.net_id == res.net_id and w.net_id not in {x.id for x in tab.parts}
    assert w.start_links == [] and w.end_links == []


def test_remove_part_in_the_middle_merges_junction(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    # removing Converter 16 deletes its input branch (18) and its output wire (32); the junction of
    # 10/18/19 is left with two wires and collapses into one wire from 5VIN to converter 2
    res = ed.remove_part(16)
    assert sorted(res.removed_wires) == [18, 32]
    assert res.merged_junctions == 1
    tree = assert_invariants(ed.serialize())
    tab = tree.tabs[0]
    assert [w.id for w in tab.wires] == [10, 28, 30]
    w10 = tab.wires[0]
    assert w10.start == (1, "Output", 1) and w10.end == (2, "Input", 1)
    assert w10.points == [(295, 295), (420, 295)]
    assert w10.start_links == [] and w10.end_links == []
    assert [p.id for p in tab.parts] == [1, 2, 11]
    assert Tree.from_bytes(ed.serialize()).tabs[0].terminal("11:Input:3")  # FPGA still intact


def test_disconnect_branch_and_driver(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    res = ed.disconnect("2:Input:1")  # a junction branch
    assert res.removed_wires == [19] and res.merged_junctions == 1
    tab = assert_invariants(ed.serialize()).tabs[0]
    w10 = next(w for w in tab.wires if w.id == 10)
    assert w10.end == (16, "Input", 1) and w10.points[0] == (295, 295)
    assert w10.points[-1] == (420, 460)
    ed.disconnect("11:Input:1")  # a plain two-terminal wire
    tab = assert_invariants(ed.serialize()).tabs[0]
    assert 28 not in [w.id for w in tab.wires]
    g = build_graph(tab)
    assert tab.terminal("11:Input:1") in g.unconnected_terminals()


def test_move_part_reroutes_attached_wires(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    res = ed.move_part("11", 800, 300)
    assert sorted(res.rerouted_wires) == [28, 30, 32]
    tab = assert_invariants(ed.serialize()).tabs[0]
    fpga = tab.part(11)
    assert (fpga.x, fpga.y) == (800, 300)
    w28 = next(w for w in tab.wires if w.id == 28)
    assert w28.points[0] == (560, 265)  # untouched end stays where it was
    assert w28.points[-1] == (800, 335)  # (760, 265) moved by (+40, +70)
    res = ed.move_part("11", 803, 302)
    assert any("snap" in w.lower() for w in res.warnings)
    assert (ed.tree.tabs[0].part(11).x, ed.tree.tabs[0].part(11).y) == (805, 300)


def test_move_part_keeps_junction_coordinates(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.move_part("2", 500, 300)
    tab = assert_invariants(ed.serialize()).tabs[0]
    w19 = next(w for w in tab.wires if w.id == 19)
    assert w19.points[0] == (345, 295)  # the junction does not move


# ------------------------------------------------------------------ rejections


def test_connect_rejections(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    a = ed.add_part("Converter", "A", outputs=[{"vout": 1}])
    b = ed.add_part("Converter", "B", outputs=[{"vout": 1}])
    base = ed.serialize()

    def expect(code: str, src: str, dst: str) -> None:
        with pytest.raises(Ltp3Error) as exc:
            ed.connect(src, dst)
        assert exc.value.code == code
        assert ed.serialize() == base, "a rejected connect must not change the document"

    expect(errors.BAD_VALUE, f"{a.id}:Output:1", f"{b.id}:Output:1")  # Output -> Output
    expect(errors.BAD_VALUE, f"{a.id}:Input:1", f"{b.id}:Input:1")  # Input -> Input
    expect(errors.TERMINAL_IN_USE, f"{a.id}:Output:1", "11:Input:1")  # second wire on an Input
    expect(errors.UNKNOWN_TERMINAL, f"{a.id}:Output:9", f"{b.id}:Input:1")
    ed.connect(f"{a.id}:Output:1", f"{b.id}:Input:1")
    base = ed.serialize()
    expect(errors.CYCLE, f"{b.id}:Output:1", f"{a.id}:Input:1")
    expect(errors.CYCLE, f"{a.id}:Output:1", f"{a.id}:Input:1")


def test_read_only_types_of_failure_leave_document_valid(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error):
        ed.set_property("99", "Vout", 1)
    with pytest.raises(Ltp3Error):
        ed.remove_part("99")
    assert ed.serialize() == sample


def test_edits_in_second_tab_leave_first_untouched() -> None:
    d = template_dir()
    multi = (
        next(
            (p for p in sorted(d.glob("*.ltp3t")) if len(Tree.from_bytes(p.read_bytes()).tabs) > 1),
            None,
        )
        if d.is_dir()
        else None
    )
    if multi is None:
        pytest.skip("no multi-tab template")
    data = multi.read_bytes()
    ed = Editor.from_bytes(data)
    part = ed.add_part("Load", "TAB2 LOAD", tab=1)
    assert ed.tree.tabs[1].part(part.id).name == "TAB2 LOAD"
    first_before = Tree.from_bytes(data).tabs[0]
    first_after = ed.tree.tabs[0]
    assert len(first_before.parts) == len(first_after.parts)
    assert_invariants(ed.serialize())


# ------------------------------------------------------------------ key order vs corpus


@pytest.mark.corpus
def test_canonical_key_orders_match_corpus(template_paths: list[Path]) -> None:
    from ltp3 import editor

    bad: list[str] = []
    for p in template_paths:
        for tab in Tree.from_bytes(p.read_bytes()).tabs:
            for part in tab.parts:
                if not editor.keys_in_canonical_order(part.type, None, list(part.props)):
                    bad.append(f"{p.name} part {part.id}")
                for t in part.terminals:
                    if not editor.keys_in_canonical_order(part.type, t.kind, list(t.props)):
                        bad.append(f"{p.name} {t.ref}")
            for w in tab.wires:
                if not editor.keys_in_canonical_order("Wire", None, list(w.props)):
                    bad.append(f"{p.name} wire {w.id}")
    assert not bad, bad[:5]


# ------------------------------------------------------------------ corpus fuzz


@pytest.mark.corpus
@pytest.mark.parametrize("name", _corpus_names())
def test_fuzz_add_load_and_connect(name: str) -> None:
    data = (template_dir() / name).read_bytes()
    ed = Editor.from_bytes(data)
    tab0 = ed.tree.tabs[0]
    outputs = [t for p in tab0.parts for t in p.terminals if t.kind == "Output"]
    if not outputs:
        pytest.skip("first tab has no Output terminal")
    src = outputs[0]
    n_parts = len(tab0.parts)
    load = ed.add_part("Load", "FUZZ LOAD", inputs=[{"ipeak": 0.5}], near=str(src.part.id))
    # a part id can appear twice in one corpus tab only if it collides across tabs; here ids are
    # per tab, so the new id must not clash
    assert load.id not in [p.id for p in tab0.parts]
    ed.connect(src.ref, f"{load.id}:Input:1")
    after = ed.serialize()
    tree = assert_invariants(after, data)
    tab = tree.tabs[0]
    assert len(tab.parts) == n_parts + 1
    g = build_graph(tab)
    new_in = tab.terminal(f"{load.id}:Input:1")
    net = g.net_of(new_in)
    assert net is not None
    assert src.ref in {t.ref for t in g.drivers(net)}
    assert any(p.id == load.id for p in g.downstream(tab.terminal(src.ref).part))
    # the other tabs are byte-for-byte unchanged
    assert [b.head.args for t in tree.tabs[1:] for b in t.blocks] == [
        b.head.args for t in Tree.from_bytes(data).tabs[1:] for b in t.blocks
    ]
    # a second sink on the same output joins the junction made by the first
    load2 = ed.add_part("Load", "FUZZ LOAD 2", inputs=[{"ipeak": 0.1}], near=str(src.part.id))
    ed.connect(src.ref, f"{load2.id}:Input:1")
    assert_invariants(ed.serialize(), data)
    ed.remove_part(load2.id)
    assert_invariants(ed.serialize(), data)
    # and undoing the edit restores the original bytes
    ed.remove_part(load.id)
    assert ed.serialize() == serialize(parse(data))


def _crowded(baseline: bytes | None) -> frozenset[tuple[int, str, int]]:
    """Input/Output terminals that already carry two wires in the unedited file (the corpus has
    one such tab, with byte-identical duplicate wires); the edit must not add to them."""
    if baseline is None:
        return frozenset()
    out: set[tuple[int, str, int]] = set()
    for tab in Tree.from_bytes(baseline).tabs:
        seen: dict[tuple[int, str, int], int] = {}
        for w in tab.wires:
            for end in (w.start, w.end):
                if end is not None:
                    seen[end] = seen.get(end, 0) + 1
        out |= {e for e, c in seen.items() if c > 1}
    return frozenset(out)
