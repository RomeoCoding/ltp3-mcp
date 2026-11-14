"""Nets, drivers, consumers and traversal over a Tab (PLAN.md 2.2).

All identity here is by object (`is` / dict keyed on views) or by list index, never by comparing
lines, because line dataclasses compare by value and many lines are textually identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import CYCLE, Ltp3Error
from .model import Part, Tab, Terminal, Wire

Partition = frozenset[frozenset[int]]


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, a: int) -> int:
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # keep the smaller index as root so component order follows file order
            self.parent[max(ra, rb)] = min(ra, rb)


def _wire_ids(tab: Tab) -> dict[int, list[int]]:
    """Wire id -> indices. A list because one corpus tab repeats wire ids."""
    out: dict[int, list[int]] = {}
    for i, w in enumerate(tab.wires):
        out.setdefault(w.id, []).append(i)
    return out


def _link_edges(
    tab: Tab, ids: dict[int, list[int]]
) -> tuple[list[tuple[int, int]], list[tuple[str, str]]]:
    edges: list[tuple[int, int]] = []
    missing: list[tuple[str, str]] = []
    for i, w in enumerate(tab.wires):
        for target in (*w.start_links, *w.end_links):
            hits = ids.get(target)
            if not hits:
                missing.append(("link", f"wire {w.id} lists unknown wire {target}"))
                continue
            edges.extend((i, j) for j in hits)
    return edges, missing


def _partition(uf: _UnionFind, tab: Tab) -> Partition:
    # Grouped by wire *id*, not index: a corpus tab holds two identical wires with one id, which
    # the links cannot tell apart, so by-index partitions would disagree there for no real reason.
    groups: dict[int, set[int]] = {}
    for i, w in enumerate(tab.wires):
        groups.setdefault(uf.find(i), set()).add(w.id)
    return frozenset(frozenset(g) for g in groups.values())


def link_components(tab: Tab) -> list[list[int]]:
    """Wire indices grouped by SWires/EWires links, in file order (first wire of each group first).

    Index-based, so a repeated wire id cannot merge two groups the links keep apart."""
    uf = _UnionFind(len(tab.wires))
    for a, b in _link_edges(tab, _wire_ids(tab))[0]:
        uf.union(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(len(tab.wires)):
        groups.setdefault(uf.find(i), []).append(i)
    return [groups[root] for root in sorted(groups)]


def netid_partition(tab: Tab) -> Partition:
    """Wire ids grouped by shared netId."""
    uf = _UnionFind(len(tab.wires))
    first: dict[int, int] = {}
    for i, w in enumerate(tab.wires):
        uf.union(first.setdefault(w.net_id, i), i)
    return _partition(uf, tab)


def link_partition(tab: Tab) -> Partition:
    """Wire ids grouped by SWires/EWires junction links."""
    uf = _UnionFind(len(tab.wires))
    for a, b in _link_edges(tab, _wire_ids(tab))[0]:
        uf.union(a, b)
    return _partition(uf, tab)


@dataclass(eq=False)
class Net:
    index: int
    wires: list[Wire]
    terminals: list[Terminal]
    net_ids: tuple[int, ...]
    labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def refs(self) -> list[str]:
        return [t.ref for t in self.terminals]


class TabGraph:
    def __init__(self, tab: Tab, merge_net_labels: bool = True) -> None:
        self.tab = tab
        self.merge_net_labels = merge_net_labels
        self.warnings: list[str] = []
        self.dangling: list[tuple[str, str]] = []
        self.nets: list[Net] = []
        self._net_of: dict[Terminal, Net] = {}
        self._build()
        self._edges = self._part_edges()

    # ------------------------------------------------------------ construction
    def _resolve(self, w: Wire, end: tuple[int, str, int] | None) -> Terminal | None:
        if end is None:
            return None
        pid, kind, num = end
        for p in self.tab.parts:
            if p.id == pid:
                for t in p.terminals:
                    if t.kind == kind and t.n == num:
                        return t
        self.dangling.append(("terminal", f"wire {w.id} end {pid}:{kind}:{num} does not exist"))
        return None

    def _build(self) -> None:
        tab = self.tab
        n = len(tab.wires)
        ids = _wire_ids(tab)
        edges, missing = _link_edges(tab, ids)
        self.dangling.extend(missing)
        uf = _UnionFind(n)
        first: dict[int, int] = {}
        for i, w in enumerate(tab.wires):
            uf.union(first.setdefault(w.net_id, i), i)
        link_uf = _UnionFind(n)
        for a, b in edges:
            uf.union(a, b)
            link_uf.union(a, b)
        # Disagreement is a warning, not an error; the union (uf) is used (PLAN 2.2).
        if netid_partition(tab) != _partition(link_uf, tab):
            self.warnings.append("NET_PARTITION_MISMATCH")

        wire_terms: list[list[Terminal]] = []
        for w in tab.wires:
            ends = [self._resolve(w, w.start), self._resolve(w, w.end)]
            wire_terms.append([t for t in ends if t is not None])

        if self.merge_net_labels:
            # Same-named Net parts are one electrical net (assumption A-NETLABEL, hypothesis).
            by_name: dict[str, int] = {}
            for i, terms in enumerate(wire_terms):
                for t in terms:
                    if t.part.type == "Net" and t.part.name:
                        uf.union(by_name.setdefault(t.part.name, i), i)

        order = {t: k for k, t in enumerate(tab.all_terminals())}
        comps: dict[int, list[int]] = {}
        for i in range(n):
            comps.setdefault(uf.find(i), []).append(i)
        for root in sorted(comps):
            members = comps[root]
            seen: dict[Terminal, None] = {}
            for i in members:
                for t in wire_terms[i]:
                    seen[t] = None
            ordered = sorted(seen, key=lambda t: order[t])
            labels: dict[str, None] = {}
            for t in ordered:
                if t.part.type == "Net" and t.part.name:
                    labels[t.part.name] = None
            net = Net(
                index=len(self.nets),
                wires=[tab.wires[i] for i in members],
                terminals=ordered,
                net_ids=tuple(sorted({tab.wires[i].net_id for i in members})),
                labels=tuple(labels),
            )
            self.nets.append(net)
            for t in ordered:
                self._net_of.setdefault(t, net)

    def _part_edges(self) -> dict[Part, list[Part]]:
        edges: dict[Part, list[Part]] = {p: [] for p in self.tab.parts}
        for net in self.nets:
            for d in self.drivers(net):
                for c in self.consumers(net):
                    if c.part not in edges[d.part]:
                        edges[d.part].append(c.part)
        return edges

    # ------------------------------------------------------------ queries
    def net_of(self, terminal: Terminal) -> Net | None:
        return self._net_of.get(terminal)

    def drivers(self, net: Net) -> list[Terminal]:
        return [t for t in net.terminals if t.kind == "Output"]

    def consumers(self, net: Net) -> list[Terminal]:
        return [t for t in net.terminals if t.kind == "Input"]

    def passives(self, net: Net) -> list[Terminal]:
        return [t for t in net.terminals if t.kind == "Aux"]

    def unconnected_terminals(self) -> list[Terminal]:
        return [t for t in self.tab.all_terminals() if t not in self._net_of]

    def downstream(self, part: Part) -> list[Part]:
        """Parts whose inputs sit on nets that `part` drives, in file order."""
        reach = self._edges[part]
        return [p for p in self.tab.parts if p in reach]

    def upstream(self, part: Part) -> list[Part]:
        return [p for p in self.tab.parts if part in self._edges[p]]

    def topo_order(self) -> list[Part]:
        """Kahn's algorithm; ties break in file order so the result is deterministic."""
        parts = self.tab.parts
        indeg = {p: 0 for p in parts}
        for p in parts:
            for q in self._edges[p]:
                indeg[q] += 1
        out: list[Part] = []
        ready = [p for p in parts if indeg[p] == 0]
        while ready:
            p = ready.pop(0)
            out.append(p)
            for q in self._edges[p]:
                indeg[q] -= 1
                if indeg[q] == 0:
                    ready.append(q)
            ready.sort(key=parts.index)
        if len(out) != len(parts):
            stuck = [p.id for p in parts if indeg[p] > 0]
            raise Ltp3Error(CYCLE, f"power tree has a loop through parts {stuck}")
        return out


def build_graph(tab: Tab, merge_net_labels: bool = True) -> TabGraph:
    return TabGraph(tab, merge_net_labels)


def nets(tab: Tab, merge_net_labels: bool = True) -> list[Net]:
    return build_graph(tab, merge_net_labels).nets
