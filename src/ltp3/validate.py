"""Structural and electrical checks (PLAN.md P5).

Errors mean the file would be malformed or ambiguous and block saving. Warnings are design-level
findings (the factory templates produce plenty and are all legal), so they never block.
Nothing here edits a document.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from itertools import pairwise
from typing import Literal, TypeVar

from . import numfmt
from .errors import CYCLE, VALIDATION_FAILED, Ltp3Error
from .graph import TabGraph
from .lossless import Document, PropLine, RawLine
from .model import Part, Symbol, Tab, Terminal, Tree, Wire

Severity = Literal["error", "warning"]
T = TypeVar("T")

# ----------------------------------------------------------------------------- vocabulary
_HEADER_TAGS = frozenset(
    {"Vers", "Grop", "Vend", "Dvce", "SolN", "Desc", "Note", "Desn", "IRal", "ORal", "PPtN", "Date"}
)
_STRUCT_TAGS = frozenset({"Tabs", "Summ", "BPrt", "Term", "Symb", "Wire"})
_DS_TAG = re.compile(r"DS\d+")


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Known keys per block tag (FORMAT.md sections 3-5); anything else is a warning, never dropped.
_KEYS: dict[str, frozenset[str]] = {
    "BPrt": _words(
        "Color Width Height PartNumb Resstnce Frequenc Ignd PLossMax ResType SizeX SizeY SizeZ "
        "SizeArea Topology ConvType CadPath VisType Comments FontSize FontColr ComAsLbl"
    ),
    "Term": _words(
        "Vout VoutMin VoutMax IoutMin IoutMax VinMin VinMax Dropout NomEff PeakEff MatrixA "
        "pLossSet EffType Ipeak Inom IpeakU InomU LoadType Title"
    ),
    "Symb": _words("Thicknss Color Color2 Dash ZIndex Comments Startcap"),
    "Wire": _words(
        "ZIndex WireClr STermPID STermTyp STermNum ETermPID ETermTyp ETermNum SWires EWires Points"
    ),
}
# Keys whose value must be one .NET-style number; text, colours, booleans and lists are excluded.
_NUMERIC = _words(
    "Width Height Resstnce Frequenc Ignd PLossMax ResType SizeX SizeY SizeZ SizeArea Topology "
    "ConvType VisType FontSize Vout VoutMin VoutMax IoutMin IoutMax VinMin VinMax Dropout NomEff "
    "PeakEff pLossSet Ipeak Inom IpeakU InomU Thicknss ZIndex"
)
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]{0,7}")  # FORMAT.md 1: all corpus keys are <= 8 chars
_POINTS = re.compile(r"(?:\(-?\d+,-?\d+\))*")
_EPS = 1e-9  # relative slack so 3.3 vs 3.3000000000000003 never warns


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    message: str
    tab: int | None = None


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def counts(self) -> Counter[str]:
        return Counter(f"{i.severity}:{i.code}" for i in self.issues)

    def raise_if_errors(self) -> None:
        errs = self.errors
        if errs:
            shown = "; ".join(f"{i.code}: {i.message}" for i in errs[:5])
            more = f" (+{len(errs) - 5} more)" if len(errs) > 5 else ""
            raise Ltp3Error(
                VALIDATION_FAILED,
                f"{len(errs)} validation error(s): {shown}{more}",
                "Fix the errors, or save a copy of the original unedited file.",
            )


def validate(subject: Tree | Document) -> Report:
    """Check a Tree (or a bare Document) and return every issue found."""
    issues: list[Issue] = []
    if isinstance(subject, Document):
        try:
            tree = Tree(subject)
        except Ltp3Error as exc:
            # the model cannot even be built (e.g. <Term> before any <BPrt>): report, don't raise
            return Report([Issue("STRUCTURE", "error", exc.message)])
    else:
        tree = subject
    doc = tree.document
    try:
        doc.check()  # reused from lossless: catches CR/LF smuggled into tags or values
    except Ltp3Error as exc:
        issues.append(Issue("LINEBREAK", "error", exc.message))
    issues.extend(_unknown_tags(doc))
    for index, tab in enumerate(tree.tabs):
        issues.extend(_TabChecker(tab, index).run())
    return Report(issues)


def _unknown_tags(doc: Document) -> list[Issue]:
    out = [
        Issue("UNKNOWN_TAG", "warning", f"line is neither tag nor property: {line.text!r}")
        for line in doc.lines
        if isinstance(line, RawLine)
    ]
    for block in doc.blocks():
        tag = block.head.tag
        if tag not in _HEADER_TAGS | _STRUCT_TAGS and not _DS_TAG.fullmatch(tag):
            out.append(Issue("UNKNOWN_TAG", "warning", f"unknown tag <{tag}>"))
    return out


def _num(props: Callable[[str], str | None], key: str) -> float | None:
    """A numeric property, or None when absent or unparseable (the latter is reported elsewhere)."""
    raw = props(key)
    if raw is None:
        return None
    try:
        return numfmt.parse(raw)
    except Ltp3Error:
        return None


def _outside(value: float, lo: float | None, hi: float | None) -> bool:
    slack = _EPS * max(1.0, abs(value))
    return (lo is not None and value < lo - slack) or (hi is not None and value > hi + slack)


class _TabChecker:
    def __init__(self, tab: Tab, index: int) -> None:
        self.tab = tab
        self.index = index
        self.out: list[Issue] = []
        self.broken = False  # an argument could not be read: skip checks that need it

    def err(self, code: str, message: str) -> None:
        self.out.append(Issue(code, "error", message, self.index))

    def warn(self, code: str, message: str) -> None:
        self.out.append(Issue(code, "warning", message, self.index))

    def safe(self, fn: Callable[[], T], what: str) -> T | None:
        try:
            return fn()
        except Ltp3Error as exc:
            self.broken = True
            self.err("BAD_ARGS", f"{what}: {exc.message}")
            return None

    # ------------------------------------------------------------------ driver
    def run(self) -> list[Issue]:
        self._tab_args()
        self._order()
        self._properties()
        self._read_args()
        if self.broken:
            return self.out  # later checks would only repeat the same failure as exceptions
        self._part_ids()
        wire_index = self._wire_ids()
        self._wire_ends(wire_index)
        self._geometry()
        self._junctions(wire_index)
        self._terminal_use()
        self._electrical()
        return self.out

    # ------------------------------------------------------------------ structure
    def _tab_args(self) -> None:
        self.safe(lambda: (self.tab.id, self.tab.color, self.tab.name), "tab header")

    def _order(self) -> None:
        late = False
        for block in self.tab.blocks[1:]:
            tag = block.head.tag
            if tag in {"Symb", "Wire"}:
                late = True
            elif tag in {"BPrt", "Term"} and late:
                # the app reads parts before symbols and wires; a later part could be lost
                self.err("TAB_ORDER", f"<{tag}> appears after a symbol or wire")
                return

    def _properties(self) -> None:
        for block in self.tab.blocks[1:]:
            tag = block.head.tag
            known = _KEYS.get(tag)
            seen_keys: set[str] = set()
            for line in block.body:
                if not isinstance(line, PropLine):
                    continue
                if not _KEY.fullmatch(line.key):
                    self.err(
                        "BAD_KEY", f"<{tag}> key {line.key!r} is not [A-Za-z][A-Za-z0-9]{{0,7}}"
                    )
                if line.key in seen_keys:
                    # FORMAT.md 1: no key repeats inside a block; the app's reader would take one
                    self.err("DUP_KEY", f"<{tag}> repeats key {line.key!r}")
                seen_keys.add(line.key)
                if known is not None and line.key not in known:
                    self.warn("UNKNOWN_KEY", f"<{tag}> has unknown key {line.key!r}")
                if tag in _KEYS and line.key in _NUMERIC:
                    self._number(tag, line)
                elif tag == "Term" and line.key == "MatrixA":
                    for piece in line.value.split(", "):
                        self._number(tag, PropLine("MatrixA", " ", piece, ""))
                elif tag == "Wire" and line.key == "Points" and not _POINTS.fullmatch(line.value):
                    self.err("BAD_POINTS", f"unreadable Points value {line.value!r}")

    def _number(self, tag: str, line: PropLine) -> None:
        try:
            numfmt.parse(line.value)
        except Ltp3Error:
            self.err("BAD_NUMBER", f"<{tag}> {line.key} is not a number: {line.value!r}")

    def _read_args(self) -> None:
        """Force every view argument once so a bad one is reported instead of raised later."""

        def part(p: Part) -> object:
            return (p.type, p.id, p.x, p.y, p.name)

        def terminal(t: Terminal) -> object:
            return (t.kind, t.n)

        def symbol(s: Symbol) -> object:
            return (s.type, s.id)

        def wire(w: Wire) -> object:
            return (w.id, w.net_id, w.start, w.end, w.start_links, w.end_links)

        for p in self.tab.parts:
            self.safe(partial(part, p), "part header")
            for t in p.terminals:
                self.safe(partial(terminal, t), "terminal header")
        for s in self.tab.symbols:
            self.safe(partial(symbol, s), "symbol header")
        for w in self.tab.wires:
            self.safe(partial(wire, w), "wire")

    def _part_ids(self) -> None:
        for pid, n in Counter(p.id for p in self.tab.parts).items():
            if n > 1:
                self.err("DUP_PART_ID", f"{n} parts share id {pid}")

    def _wire_ids(self) -> dict[int, list[Wire]]:
        index: dict[int, list[Wire]] = {}
        for w in self.tab.wires:
            index.setdefault(w.id, []).append(w)
        for wid, ws in index.items():
            if len(ws) > 1:
                # real in the corpus (one template); links cannot tell the copies apart
                self.warn("DUP_WIRE_ID", f"{len(ws)} wires share id {wid}")
        return index

    def _resolve(self, pid: int, kind: str, num: int) -> Terminal | None:
        for p in self.tab.parts:
            if p.id == pid:
                for t in p.terminals:
                    if t.kind == kind and t.n == num:
                        return t
        return None

    def _wire_ends(self, wire_index: dict[int, list[Wire]]) -> None:
        for w in self.tab.wires:
            for side, links in (("S", w.start_links), ("E", w.end_links)):
                keys = [f"{side}TermPID", f"{side}TermTyp", f"{side}TermNum"]
                present = [k for k in keys if k in w.props]
                if 0 < len(present) < 3:
                    self.err(
                        "DANGLING_TERMINAL", f"wire {w.id} {side} end names a partial terminal"
                    )
                elif len(present) == 3:
                    end = w.start if side == "S" else w.end
                    assert end is not None  # all three keys exist, so the end was parsed
                    if self._resolve(*end) is None:
                        self.err(
                            "DANGLING_TERMINAL",
                            f"wire {w.id} {side} end {end[0]}:{end[1]}:{end[2]} does not exist",
                        )
                elif not links:
                    self.err("WIRE_END_EMPTY", f"wire {w.id} {side} end has no terminal or links")
                for target in links:
                    if target not in wire_index:
                        self.err("DANGLING_WIRE", f"wire {w.id} lists unknown wire {target}")

    def _geometry(self) -> None:
        for w in self.tab.wires:
            pts = w.points
            if any(a[0] != b[0] and a[1] != b[1] for a, b in pairwise(pts)):
                self.err("DIAGONAL", f"wire {w.id} has a diagonal segment")

    def _junctions(self, wire_index: dict[int, list[Wire]]) -> None:
        seen: set[tuple[frozenset[int], str]] = set()

        def report(group: frozenset[int], code: str, message: str) -> None:
            if (group, code) not in seen:  # one report per junction, not one per member
                seen.add((group, code))
                self.err(code, message)

        for w in self.tab.wires:
            for side, links in (("S", w.start_links), ("E", w.end_links)):
                if not links:
                    continue
                group = frozenset({w.id, *links})
                pts = w.points
                here = (pts[0] if side == "S" else pts[-1]) if pts else None
                for member in links:
                    hits = wire_index.get(member, [])
                    # (hit, sides where it lists w and agrees on the whole group); a duplicated id
                    # has several hits, and one agreeing copy is enough
                    agreeing = [
                        (h, k)
                        for h in hits
                        for k, ls in (("S", h.start_links), ("E", h.end_links))
                        if w.id in ls and set(ls) | {h.id} == group
                    ]
                    if hits and not agreeing:
                        report(
                            group,
                            "NON_CLIQUE_JUNCTION",
                            f"junction of wires {sorted(group)} is not a clique "
                            f"(wire {member} disagrees with wire {w.id})",
                        )
                    elif (
                        hits
                        and here is not None
                        and not any(
                            h.points and (h.points[0] if k == "S" else h.points[-1]) == here
                            for h, k in agreeing
                        )
                    ):
                        report(
                            group,
                            "JUNCTION_COORD",
                            f"wires {w.id} and {member} meet at different coordinates",
                        )

    def _terminal_use(self) -> None:
        uses: Counter[tuple[int, str, int]] = Counter()
        ids = Counter(w.id for w in self.tab.wires)
        for w in self.tab.wires:
            if ids[w.id] > 1:
                continue  # duplicated ids are ambiguous copies (DUP_WIRE_ID warns); not counted
            for end in (w.start, w.end):
                if end is not None and end[1] in {"Input", "Output"}:
                    uses[end] += 1
        for (pid, kind, num), n in uses.items():
            if n > 1:
                self.err(
                    "TERMINAL_MULTI_WIRE",
                    f"terminal {pid}:{kind}:{num} carries {n} wires; only Aux may carry two",
                )

    # ------------------------------------------------------------------ electrical
    def _electrical(self) -> None:
        graph = TabGraph(self.tab)
        if graph.warnings:
            # an error since QA2 #1: a netId shared by link-disconnected wires (or a link joining
            # two netIds) makes the graph read a different circuit than the GUI draws
            self.err("NET_MISMATCH", "netId grouping and SWires/EWires links disagree")
        for t in graph.unconnected_terminals():
            self.warn("UNCONNECTED_TERMINAL", f"terminal {t.ref} has no wire")
        for net in graph.nets:
            drivers, consumers = graph.drivers(net), graph.consumers(net)
            if len(drivers) > 1:
                self.warn("MULTI_DRIVER", f"net drives from {[t.ref for t in drivers]}")
            if consumers and not drivers:
                self.warn(
                    "UNDRIVEN_CONSUMER", f"inputs {[t.ref for t in consumers]} have no driver"
                )
            self._vin(net.terminals, drivers)
            if len(drivers) == 1:
                self._load_current(drivers[0], consumers)
        for p in self.tab.parts:
            for t in p.terminals:
                if t.kind == "Output":
                    self._vout(t)
        try:
            graph.topo_order()
        except Ltp3Error as exc:
            if exc.code == CYCLE:
                self.warn("CYCLE", exc.message)

    def _vout(self, t: Terminal) -> None:
        get = t.props.get
        v = _num(get, "Vout")
        lo, hi = _num(get, "VoutMin"), _num(get, "VoutMax")
        if v is not None and lo is not None and hi is not None:
            lo, hi = min(lo, hi), max(lo, hi)  # negative rails may store the bounds either way
            if _outside(v, lo, hi):
                self.warn("VOUT_RANGE", f"{t.ref} Vout {v:g} outside {lo:g}..{hi:g}")

    def _vin(self, terminals: list[Terminal], drivers: list[Terminal]) -> None:
        for t in terminals:
            if t.kind != "Input" or t.part.type not in {"Converter", "LDO"}:
                continue
            lo, hi = _num(t.props.get, "VinMin"), _num(t.props.get, "VinMax")
            if lo is not None and hi is not None:
                lo, hi = min(lo, hi), max(lo, hi)
            for d in drivers:
                v = _num(d.props.get, "Vout")
                if v is not None and _outside(v, lo, hi):
                    self.warn("VIN_RANGE", f"{t.ref} fed {v:g} V by {d.ref}, limits {lo}..{hi}")

    def _load_current(self, driver: Terminal, consumers: list[Terminal]) -> None:
        limit = _num(driver.props.get, "IoutMax")
        if limit is None:
            return
        total = 0.0
        for c in consumers:
            if c.part.type != "Load":
                continue  # a converter's input current needs the solver (P3)
            # peak first: IoutMax is a capability limit, so the worst case is the fair check
            for key, unit in (("Ipeak", "IpeakU"), ("Inom", "InomU")):
                amps = _num(c.props.get, key)
                if amps is not None:
                    total += amps * 10 ** (_num(c.props.get, unit) or 0)  # unit is a 10^n exponent
                    break
        if _outside(total, None, limit):
            self.warn("IOUT_OVER", f"{driver.ref} supplies {total:g} A but IoutMax is {limit:g}")


__all__ = ["Issue", "Report", "validate"]
