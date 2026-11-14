"""Graph -> JSON export with a stable, versioned schema.

Schema "ltp3-export" version 1 (additive changes keep the version; any rename, removal or type
change bumps `schema_version`). All numbers that come from the file stay strings: the file format
stores text and the export does not reinterpret it.

    {
      "schema": "ltp3-export", "schema_version": 1, "include_layout": bool,
      "header": {Tag: raw args, ...},                  # file header tags (Vers, Vend, ...)
      "tabs": [{
        "index": int, "id": int, "name": str,
        "color": str,                                  # layout only
        "parts": [{                                    # sorted by id (stable)
          "id": int, "type": str, "name": str,
          "x": int, "y": int,                          # layout only
          "props": {key: str},                         # layout keys only with include_layout
          "results": {metric: float},                  # only when a results mapping is given
          "terminals": [{"ref": "id:Kind:n", "kind": str, "n": int,
                         "props": {key: str}, "net": int | null}]
        }],
        "nets": [{"id": int, "terminals": [ref, ...], "labels": [str, ...]}],
        "wires": [...], "symbols": [...]               # layout only
      }]
    }

Net ids are positions in the list, ordered by their sorted terminal refs, so they are stable for
identical electrical content. Output is `json.dumps(sort_keys=True, indent=2)` plus a newline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .graph import build_graph
from .model import Part, Terminal, Tree

SCHEMA_NAME = "ltp3-export"
SCHEMA_VERSION = 1

# Solved numbers are not computed here: P7 passes the solver's output in this shape,
# {(tab_index, part_id): {metric_name: value}}, so export/compare never import the solver.
Results = Mapping[tuple[int, int], Mapping[str, float]]

# Presentation-only keys. VisType/ComAsLbl are display hints per FORMAT.md section 3.1 [H].
PART_LAYOUT_KEYS = frozenset(
    {"Color", "Width", "Height", "FontSize", "FontColr", "VisType", "ComAsLbl"}
)
# SWires/EWires are redundant with the net ids and the terminal ends, so they are not exported.
WIRE_LAYOUT_KEYS = frozenset({"ZIndex", "WireClr", "Points"})

_KIND_ORDER = {"Input": 0, "Output": 1, "Aux": 2}


def terminal_sort_key(t: Terminal) -> tuple[int, int, int, str]:
    return (t.part.id, _KIND_ORDER.get(t.kind, 9), t.n, t.kind)


def sorted_net_terminals(terminals: list[Terminal]) -> list[Terminal]:
    return sorted(terminals, key=terminal_sort_key)


def _terminal(t: Terminal, net_index: int | None) -> dict[str, Any]:
    return {
        "ref": t.ref,
        "kind": t.kind,
        "n": t.n,
        "props": dict(t.props),
        "net": net_index,
    }


def _part(part: Part, layout: bool, nets: dict[Terminal, int]) -> dict[str, Any]:
    props = {k: v for k, v in part.props.items() if layout or k not in PART_LAYOUT_KEYS}
    out: dict[str, Any] = {
        "id": part.id,
        "type": part.type,
        "name": part.name,
        "props": props,
        "terminals": [_terminal(t, nets.get(t)) for t in part.terminals],
    }
    if layout:
        out["x"] = part.x
        out["y"] = part.y
    return out


def export_tree(
    tree: Tree, include_layout: bool = False, results: Results | None = None
) -> dict[str, Any]:
    tabs: list[dict[str, Any]] = []
    for index, tab in enumerate(tree.tabs):
        graph = build_graph(tab)
        ordered = sorted(
            (n for n in graph.nets if n.terminals),
            key=lambda n: [terminal_sort_key(t) for t in sorted_net_terminals(n.terminals)],
        )
        net_index: dict[Terminal, int] = {}
        nets_out: list[dict[str, Any]] = []
        for i, net in enumerate(ordered):
            members = sorted_net_terminals(net.terminals)
            for t in members:
                net_index[t] = i
            nets_out.append(
                {"id": i, "terminals": [t.ref for t in members], "labels": sorted(net.labels)}
            )
        # Stable sort: equal ids (never within a tab per FORMAT.md) would keep file order.
        parts = sorted(tab.parts, key=lambda p: p.id)
        parts_out = [_part(p, include_layout, net_index) for p in parts]
        if results is not None:
            for p_out in parts_out:
                got = results.get((index, p_out["id"]))
                if got is not None:
                    p_out["results"] = {k: float(v) for k, v in got.items()}
        tab_out: dict[str, Any] = {
            "index": index,
            "id": tab.id,
            "name": tab.name,
            "parts": parts_out,
            "nets": nets_out,
        }
        if include_layout:
            tab_out["color"] = tab.color
            tab_out["wires"] = [
                {
                    "id": w.id,
                    "net_id": w.net_id,
                    "points": [list(pt) for pt in w.points],
                    "props": {k: v for k, v in w.props.items() if k in WIRE_LAYOUT_KEYS},
                }
                for w in tab.wires
            ]
            tab_out["symbols"] = [
                {"type": s.type, "id": s.id, "args": s.block.head.args, "props": dict(s.props)}
                for s in tab.symbols
            ]
        tabs.append(tab_out)
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "include_layout": include_layout,
        "header": {b.head.tag: b.head.args for b in tree.header},
        "tabs": tabs,
    }


def to_json(tree: Tree, include_layout: bool = False, results: Results | None = None) -> str:
    doc = export_tree(tree, include_layout=include_layout, results=results)
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
