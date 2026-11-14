"""Structural diff of two trees: parts, terminals, connections. Presentation is hidden by default.

Parts are matched by id within a matched tab; tabs by name, then by tab id. Connections are
compared as electrical nets (sets of terminal refs), not wire by wire, so re-routing a wire or
renumbering wire/net ids is not a change. `Change.old/new` are display strings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .export import (
    PART_LAYOUT_KEYS,
    WIRE_LAYOUT_KEYS,
    Results,
    sorted_net_terminals,
    terminal_sort_key,
)
from .graph import build_graph
from .model import Part, Tab, Tree


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    tab: str
    target: str
    key: str = ""
    old: str | None = None
    new: str | None = None
    layout: bool = False  # True for presentation-only changes (shown only on request)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tab": self.tab,
            "target": self.target,
            "key": self.key,
            "old": self.old,
            "new": self.new,
            "layout": self.layout,
        }


@dataclass(slots=True)
class TreeDiff:
    changes: list[Change] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changes

    def to_dict(self) -> dict[str, Any]:
        return {"count": len(self.changes), "changes": [c.to_dict() for c in self.changes]}


def _pair_tabs(a: Tree, b: Tree) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """(matched index pairs, unmatched a, unmatched b). Name first (k-th duplicate with k-th),
    then tab id for the leftovers, which catches a renamed tab."""
    pairs: list[tuple[int, int]] = []
    left_a = list(range(len(a.tabs)))
    left_b = list(range(len(b.tabs)))
    for attr in ("name", "id"):
        for ia in list(left_a):
            hit = next(
                (ib for ib in left_b if getattr(a.tabs[ia], attr) == getattr(b.tabs[ib], attr)),
                None,
            )
            if hit is not None:
                pairs.append((ia, hit))
                left_a.remove(ia)
                left_b.remove(hit)
    pairs.sort()
    return pairs, left_a, left_b


def _diff_props(
    out: list[Change],
    kind: str,
    tab: str,
    target: str,
    old: dict[str, str],
    new: dict[str, str],
    layout_keys: frozenset[str] | None,
    layout_kind: str,
    include_layout: bool,
) -> None:
    # Key order: a's keys in file order, then keys only in b, so output is deterministic.
    keys = list(old) + [k for k in new if k not in old]
    for k in keys:
        if old.get(k) == new.get(k):
            continue
        is_layout = layout_keys is not None and k in layout_keys
        if is_layout and not include_layout:
            continue
        out.append(
            Change(
                layout_kind if is_layout else kind,
                tab,
                target,
                k,
                old.get(k),
                new.get(k),
                is_layout,
            )
        )


def _part_scalars(p: Part) -> dict[str, str]:
    return {"x": str(p.x), "y": str(p.y)}


def _diff_part(out: list[Change], tab_name: str, pa: Part, pb: Part, include_layout: bool) -> None:
    target = str(pa.id)
    if pa.type != pb.type:
        out.append(Change("part_type", tab_name, target, "type", pa.type, pb.type))
    if pa.name != pb.name:
        out.append(Change("part_name", tab_name, target, "name", pa.name, pb.name))
    if include_layout:
        _diff_props(
            out, "", tab_name, target, _part_scalars(pa), _part_scalars(pb),
            frozenset({"x", "y"}), "part_layout", True,
        )  # fmt: skip
    _diff_props(
        out, "part_prop", tab_name, target, dict(pa.props), dict(pb.props),
        PART_LAYOUT_KEYS, "part_layout", include_layout,
    )  # fmt: skip
    ta = {(t.kind, t.n): t for t in pa.terminals}
    tb = {(t.kind, t.n): t for t in pb.terminals}
    for key in sorted(set(ta) | set(tb), key=lambda k: (k[0], k[1])):
        ref = f"{pa.id}:{key[0]}:{key[1]}"
        if key not in tb:
            out.append(Change("terminal_removed", tab_name, ref))
        elif key not in ta:
            out.append(Change("terminal_added", tab_name, ref))
        else:
            _diff_props(
                out, "terminal_prop", tab_name, ref, dict(ta[key].props), dict(tb[key].props),
                None, "", False,
            )  # fmt: skip


def _net_sets(tab: Tab) -> dict[frozenset[str], list[str]]:
    """Net -> its refs in canonical order. Only nets joining 2+ terminals are connections; a
    one-terminal net is a wire stub and would only add noise."""
    sets: dict[frozenset[str], list[str]] = {}
    for net in build_graph(tab).nets:
        if len(net.terminals) >= 2:
            refs = [t.ref for t in sorted_net_terminals(net.terminals)]
            sets[frozenset(refs)] = refs
    return sets


def _net_order(refs: list[str], tab: Tab) -> list[Any]:
    by_ref = {t.ref: t for p in tab.parts for t in p.terminals}
    return [terminal_sort_key(by_ref[r]) for r in refs if r in by_ref]


def _diff_nets(out: list[Change], name: str, ta: Tab, tb: Tab) -> None:
    na, nb = _net_sets(ta), _net_sets(tb)
    gone = sorted((s for s in na if s not in nb), key=lambda s: _net_order(na[s], ta))
    new = sorted((s for s in nb if s not in na), key=lambda s: _net_order(nb[s], tb))
    unpaired = list(new)
    for s in gone:
        # A net that kept some terminals is "changed", not removed + added: one edit, one change.
        best = max(unpaired, key=lambda c: len(c & s), default=None)
        if best is not None and best & s:
            unpaired.remove(best)
            out.append(
                Change("net_changed", name, na[s][0], "", " ".join(na[s]), " ".join(nb[best]))
            )
        else:
            out.append(Change("net_removed", name, na[s][0], "", " ".join(na[s]), None))
    for s in unpaired:
        out.append(Change("net_added", name, nb[s][0], "", None, " ".join(nb[s])))


def _keyed(items: list[Any], key: Any) -> dict[tuple[Any, int], Any]:
    """Key + occurrence index: wire and symbol ids may repeat inside a tab."""
    seen: dict[Any, int] = {}
    out: dict[tuple[Any, int], Any] = {}
    for it in items:
        k = key(it)
        n = seen.get(k, 0)
        seen[k] = n + 1
        out[(k, n)] = it
    return out


def _diff_layout_objects(out: list[Change], name: str, ta: Tab, tb: Tab) -> None:
    wa = _keyed(ta.wires, lambda w: w.id)
    wb = _keyed(tb.wires, lambda w: w.id)
    for k in wa:
        if k in wb:
            _diff_props(
                out, "", name, f"wire {k[0]}",
                {x: v for x, v in wa[k].props.items() if x in WIRE_LAYOUT_KEYS},
                {x: v for x, v in wb[k].props.items() if x in WIRE_LAYOUT_KEYS},
                WIRE_LAYOUT_KEYS, "wire_layout", True,
            )  # fmt: skip
    sa = _keyed(ta.symbols, lambda s: (s.type, s.id))
    sb = _keyed(tb.symbols, lambda s: (s.type, s.id))
    for k in sa:
        target = f"{k[0][0]} {k[0][1]}"
        if k not in sb:
            out.append(Change("symbol_removed", name, target, layout=True))
            continue
        if sa[k].block.head.args != sb[k].block.head.args:
            out.append(
                Change(
                    "symbol_layout", name, target, "args",
                    sa[k].block.head.args, sb[k].block.head.args, True,
                )
            )  # fmt: skip
        _diff_props(
            out, "", name, target, dict(sa[k].props), dict(sb[k].props),
            None, "symbol_layout", True,
        )  # fmt: skip
    for k in sb:
        if k not in sa:
            out.append(Change("symbol_added", name, f"{k[0][0]} {k[0][1]}", layout=True))


def _diff_results(
    out: list[Change],
    name: str,
    ia: int,
    ib: int,
    ra: Results,
    rb: Results,
    ids: list[int],
    tol: float,
) -> None:
    for pid in ids:
        ma, mb = ra.get((ia, pid), {}), rb.get((ib, pid), {})
        for metric in list(ma) + [m for m in mb if m not in ma]:
            va, vb = ma.get(metric), mb.get(metric)
            if va is not None and vb is not None and math.isclose(va, vb, rel_tol=tol, abs_tol=tol):
                continue
            out.append(
                Change(
                    "result", name, str(pid), metric,
                    None if va is None else repr(float(va)),
                    None if vb is None else repr(float(vb)),
                )
            )  # fmt: skip


def compare_trees(
    a: Tree,
    b: Tree,
    include_layout: bool = False,
    include_header: bool = False,
    results_a: Results | None = None,
    results_b: Results | None = None,
    result_tolerance: float = 1e-9,
) -> TreeDiff:
    """Diff `a` (before) against `b` (after).

    Hidden by default: layout (positions, sizes, colours, wire style, symbols) and the file header.
    Solved numbers are compared only when both `results_a` and `results_b` are given; they use the
    `export.Results` shape, which P7 fills from the solver (this module never imports it).
    """
    out: list[Change] = []
    if include_header:
        ha = {x.head.tag: x.head.args for x in a.header}
        hb = {x.head.tag: x.head.args for x in b.header}
        _diff_props(out, "header", "", "header", ha, hb, None, "", False)

    pairs, only_a, only_b = _pair_tabs(a, b)
    for ia in only_a:
        out.append(Change("tab_removed", a.tabs[ia].name, a.tabs[ia].name))
    for ib in only_b:
        out.append(Change("tab_added", b.tabs[ib].name, b.tabs[ib].name))

    for ia, ib in pairs:
        ta, tb = a.tabs[ia], b.tabs[ib]
        name = tb.name
        if ta.name != tb.name:
            out.append(Change("tab_name", name, name, "name", ta.name, tb.name))
        if include_layout and ta.color != tb.color:
            out.append(Change("tab_layout", name, name, "color", ta.color, tb.color, True))
        pa = {p.id: p for p in ta.parts}
        pb = {p.id: p for p in tb.parts}
        for pid in sorted(set(pa) | set(pb)):
            if pid not in pb:
                out.append(
                    Change("part_removed", name, str(pid), "", f"{pa[pid].type} {pa[pid].name}")
                )
            elif pid not in pa:
                out.append(
                    Change("part_added", name, str(pid), "", None, f"{pb[pid].type} {pb[pid].name}")
                )
            else:
                _diff_part(out, name, pa[pid], pb[pid], include_layout)
        _diff_nets(out, name, ta, tb)
        if include_layout:
            _diff_layout_objects(out, name, ta, tb)
        if results_a is not None and results_b is not None:
            _diff_results(
                out, name, ia, ib, results_a, results_b, sorted(set(pa) & set(pb)), result_tolerance
            )
    return TreeDiff(out)
