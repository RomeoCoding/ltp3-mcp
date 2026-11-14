"""P6: structural diff of two trees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ltp3.compare import compare_trees
from ltp3.model import Tree

CRLF = "\r\n"

_BASE = [
    "<Vers> 3.3.4",
    "<Tabs> 1, #FFFFFFFF, Main",
    "<Summ> 0, 0, 12, #FF000000, #FFFFFFFF",
    "<BPrt> Power, 2, 100, 100, 5V",
    "\tColor   : #FF000000",
    "\tWidth   : 60",
    "\tHeight  : 60",
    "<Term> Output, 1",
    "\tVout    : 5",
    "<BPrt> Load, 1, 300, 100, Load",
    "\tColor   : #FF000000",
    "\tWidth   : 60",
    "\tHeight  : 60",
    "<Term> Input, 1",
    "\tIpeak   : 1",
    "<Wire> 3, 7",
    "\tSTermPID: 2",
    "\tSTermTyp: Output",
    "\tSTermNum: 1",
    "\tETermPID: 1",
    "\tETermTyp: Input",
    "\tETermNum: 1",
    "\tPoints  : (160,130)(300,130)",
]


def _tree(lines: list[str] | None = None) -> Tree:
    return Tree.from_bytes("".join(x + CRLF for x in (lines or _BASE)).encode())


def _replace(old: str, new: str) -> list[str]:
    assert old in _BASE
    return [new if x == old else x for x in _BASE]


def test_identical_trees_have_no_changes() -> None:
    d = compare_trees(_tree(), _tree())
    assert d.changes == [] and d.is_empty


def test_one_property_edit_is_exactly_one_change() -> None:
    d = compare_trees(_tree(), _tree(_replace("\tIpeak   : 1", "\tIpeak   : 2")))
    assert len(d.changes) == 1
    c = d.changes[0]
    assert (c.kind, c.tab, c.target, c.key, c.old, c.new) == (
        "terminal_prop", "Main", "1:Input:1", "Ipeak", "1", "2",
    )  # fmt: skip
    assert not d.is_empty


def test_layout_hidden_unless_requested() -> None:
    moved = _replace("<BPrt> Load, 1, 300, 100, Load", "<BPrt> Load, 1, 400, 100, Load")
    # second Width line (the Load's) widened
    widths = [i for i, x in enumerate(moved) if x == "\tWidth   : 60"]
    moved[widths[1]] = "\tWidth   : 80"
    other = _tree(moved)
    assert compare_trees(_tree(), other).changes == []
    shown = compare_trees(_tree(), other, include_layout=True)
    keys = {(c.kind, c.key) for c in shown.changes}
    assert ("part_layout", "x") in keys and ("part_layout", "Width") in keys
    assert all(c.layout for c in shown.changes)


def test_part_renamed_and_added_removed() -> None:
    renamed = _replace("<BPrt> Power, 2, 100, 100, 5V", "<BPrt> Power, 2, 100, 100, 3V3")
    d = compare_trees(_tree(), _tree(renamed))
    assert [(c.kind, c.key, c.old, c.new) for c in d.changes] == [
        ("part_name", "name", "5V", "3V3")
    ]

    extra = [
        *_BASE[:15],
        "<BPrt> Load, 9, 500, 100, Extra",
        "\tColor   : #FF000000",
        "\tWidth   : 60",
        "\tHeight  : 60",
        "<Term> Input, 1",
        *_BASE[15:],
    ]
    d2 = compare_trees(_tree(), _tree(extra))
    assert [(c.kind, c.target) for c in d2.changes] == [("part_added", "9")]
    d3 = compare_trees(_tree(extra), _tree())
    assert [(c.kind, c.target) for c in d3.changes] == [("part_removed", "9")]


def test_connection_change_reported_without_wire_noise() -> None:
    no_wire = _BASE[:15]
    d = compare_trees(_tree(), _tree(no_wire))
    assert [c.kind for c in d.changes] == ["net_removed"]
    assert d.changes[0].old == "1:Input:1 2:Output:1"
    assert [c.kind for c in compare_trees(_tree(no_wire), _tree()).changes] == ["net_added"]


def test_wire_style_is_layout() -> None:
    styled = [*_BASE[:16], "\tZIndex  : -1", *_BASE[16:]]
    assert compare_trees(_tree(), _tree(styled)).changes == []
    shown = compare_trees(_tree(), _tree(styled), include_layout=True)
    assert [(c.kind, c.key) for c in shown.changes] == [("wire_layout", "ZIndex")]


def test_header_hidden_unless_requested() -> None:
    other = [_BASE[0].replace("3.3.4", "3.6.8"), *_BASE[1:]]
    assert compare_trees(_tree(), _tree(other)).changes == []
    shown = compare_trees(_tree(), _tree(other), include_header=True)
    assert [(c.kind, c.key, c.old, c.new) for c in shown.changes] == [
        ("header", "Vers", "3.3.4", "3.6.8")
    ]


def test_tab_added() -> None:
    two = [*_BASE, "<Tabs> 2, #FFFFFFFF, Second", "<Summ> 0, 0, 12, #FF000000, #FFFFFFFF"]
    d = compare_trees(_tree(), _tree(two))
    assert [(c.kind, c.target) for c in d.changes] == [("tab_added", "Second")]


def test_results_delta_optional_interface() -> None:
    a = {(0, 2): {"pout_w": 5.0, "ploss_w": 1.0}}
    b = {(0, 2): {"pout_w": 5.0, "ploss_w": 1.5}}
    assert compare_trees(_tree(), _tree()).changes == []  # no results given -> no result diff
    d = compare_trees(_tree(), _tree(), results_a=a, results_b=b)
    assert [(c.kind, c.target, c.key, c.old, c.new) for c in d.changes] == [
        ("result", "2", "ploss_w", "1.0", "1.5")
    ]
    assert compare_trees(_tree(), _tree(), results_a=a, results_b=a).changes == []


def test_to_dict_is_json_ready() -> None:
    d = compare_trees(_tree(), _tree(_replace("\tIpeak   : 1", "\tIpeak   : 2")))
    out = json.loads(json.dumps(d.to_dict()))
    assert out["count"] == 1 and out["changes"][0]["key"] == "Ipeak"


# ---------------------------------------------------------------- Artix-7 template vs saved sample


@pytest.fixture(scope="module")
def artix_pair(template_dir: Path) -> tuple[Tree, Tree]:
    # Walk up so a git worktree finds the main checkout's gitignored samples/ too.
    sample = next(
        (
            p / "samples" / "artix7_original.ltp3"
            for p in Path(__file__).resolve().parents
            if (p / "samples" / "artix7_original.ltp3").is_file()
        ),
        None,
    )
    if sample is None:
        pytest.skip("samples/artix7_original.ltp3 not found")
    tmpl = template_dir / "Artix-7 Digilent Basys 3 board power tree.ltp3t"
    return Tree.load(tmpl), Tree.load(sample)


def test_artix_template_vs_sample_no_part_or_connection_change(
    artix_pair: tuple[Tree, Tree],
) -> None:
    assert compare_trees(*artix_pair).changes == []


def test_artix_header_diff_available_on_request(artix_pair: tuple[Tree, Tree]) -> None:
    d = compare_trees(*artix_pair, include_header=True)
    assert {c.kind for c in d.changes} == {"header"}
    assert "Vers" in {c.key for c in d.changes}


def test_artix_layout_diff_is_only_wire_zindex(artix_pair: tuple[Tree, Tree]) -> None:
    d = compare_trees(*artix_pair, include_layout=True)
    assert {(c.kind, c.key) for c in d.changes} == {("wire_layout", "ZIndex")}
    assert len(d.changes) == 6


def test_artix_one_property_edit(artix_pair: tuple[Tree, Tree]) -> None:
    tmpl, sample = artix_pair
    edited = Tree.from_bytes(sample.document.serialize())
    term = next(t for p in edited.tabs[0].parts for t in p.terminals if "Ipeak" in t.props)
    old = term.props["Ipeak"]
    term.props["Ipeak"] = "123"
    d = compare_trees(tmpl, edited)
    assert len(d.changes) == 1
    assert (d.changes[0].key, d.changes[0].old, d.changes[0].new) == ("Ipeak", old, "123")
