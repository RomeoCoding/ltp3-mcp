"""Mutations on a Document, as the smallest possible line edits (PLAN.md 2.4).

Views from `model.py` hold block indices, which go stale after any insert or remove. So every
operation re-reads the tree it needs (`Editor._fresh`), takes the live line objects it will touch,
and edits those through the identity-based helpers of `lossless.py`. Line classes compare by
identity; nothing here looks a line up with `==`, `.index()` or `.remove()` on a list.

Every public operation is atomic: it ends with `Document.check()`, and on any error the document
is restored to its bytes from before the call.
"""

from __future__ import annotations

import difflib
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from . import layout
from .errors import (
    BAD_VALUE,
    CYCLE,
    TERMINAL_IN_USE,
    UNKNOWN_KEY,
    UNKNOWN_TAB,
    Ltp3Error,
)
from .graph import build_graph
from .layout import Point
from .lossless import Block, Document, PropLine, TagLine, parse, serialize, split_args
from .model import Part, Tab, Terminal, Tree, Wire
from .numfmt import fmt
from .numfmt import parse as parse_number

T = TypeVar("T")

NEWLINE_TOKEN = "*^!#"  # FORMAT.md 4: how the file encodes a line break inside text values

# ---------------------------------------------------------------- canonical key orders (FORMAT.md)
PART_ORDER: tuple[str, ...] = (
    "Color", "Width", "Height", "PartNumb", "Resstnce", "Frequenc", "Ignd", "PLossMax",
    "ResType", "SizeX", "SizeY", "SizeZ", "SizeArea", "Topology", "ConvType", "CadPath",
    "VisType", "Comments", "FontSize", "FontColr", "ComAsLbl",
)  # fmt: skip
WIRE_ORDER: tuple[str, ...] = (
    "ZIndex", "WireClr", "STermPID", "STermTyp", "STermNum", "ETermPID", "ETermTyp", "ETermNum",
    "SWires", "EWires", "Points",
)  # fmt: skip
_IN_LIMITS: tuple[str, ...] = ("VinMin", "VinMax")
_OUT_FULL: tuple[str, ...] = (
    "Vout", "VoutMin", "VoutMax", "Dropout", "IoutMin", "IoutMax", "NomEff", "pLossSet",
    "PeakEff", "MatrixA", "EffType",
)  # fmt: skip
# pLossSet shares NomEff's slot and MatrixA shares PeakEff's (FORMAT.md 3.2), which is why they
# sit in this order: MatrixA + pLossSet co-occur in 6 corpus terminals.
TERMINAL_ORDER: dict[tuple[str, str], tuple[str, ...]] = {
    ("Converter", "Input"): _IN_LIMITS,
    ("LDO", "Input"): _IN_LIMITS,
    ("Converter", "Output"): _OUT_FULL,
    ("LDO", "Output"): ("Vout", "VoutMin", "VoutMax", "Dropout", "IoutMin", "IoutMax"),
    ("Power", "Output"): ("Vout", "VoutMin", "VoutMax", "IoutMax"),
    ("Load", "Input"): ("Inom", "InomU", "Ipeak", "IpeakU", "LoadType", "Title"),
}
_BASE_PART = ("Color", "Width", "Height")
PART_KEYS: dict[str, tuple[str, ...]] = {
    "Load": (*_BASE_PART, "VisType", "FontSize", "Comments", "ComAsLbl", "FontColr"),
    "LDO": (*_BASE_PART, "VisType", "PartNumb", "Comments", "ComAsLbl", "Ignd", "FontSize"),
    "Converter": (
        *_BASE_PART, "VisType", "PartNumb", "ConvType", "Topology", "SizeArea", "FontSize",
        "FontColr", "SizeY", "SizeX", "Comments", "ComAsLbl", "Frequenc", "SizeZ", "CadPath",
    ),
    "Power": (*_BASE_PART, "VisType", "ResType", "Comments", "FontColr"),
    "Capacitor": (*_BASE_PART, "Comments", "FontColr"),
    "Resistor": (*_BASE_PART, "Resstnce", "ResType", "FontSize", "VisType", "PLossMax"),
    "Net": _BASE_PART,
}  # fmt: skip

_INT_KEYS = frozenset(
    {"Width", "Height", "VisType", "ConvType", "Topology", "ResType", "FontSize", "InomU", "IpeakU"}
)
_DECIMAL_KEYS = frozenset(
    {
        "Resstnce", "Frequenc", "Ignd", "PLossMax", "SizeX", "SizeY", "SizeZ", "SizeArea",
        "Vout", "VoutMin", "VoutMax", "IoutMax", "IoutMin", "Dropout", "NomEff", "PeakEff",
        "pLossSet", "VinMin", "VinMax", "Inom", "Ipeak",
    }
)  # fmt: skip
_COLOR_KEYS = frozenset({"Color", "FontColr", "WireClr"})
_BOOL_KEYS = frozenset({"ComAsLbl", "LoadType", "EffType"})
_NEWLINE_KEYS = frozenset({"Comments", "Title"})  # the only keys whose text may hold a line break
_COLOR = re.compile(r"#[0-9A-Fa-f]{8}")


def keys_in_canonical_order(owner: str, kind: str | None, keys: Sequence[str]) -> bool:
    """True when the known keys of a block appear in the canonical order (unknown keys are
    ignored). `owner` is a part type or `"Wire"`; `kind` the terminal kind or None for the part."""
    order = _order_for(owner, kind)
    ranks = [order.index(k) for k in keys if k in order]
    return ranks == sorted(ranks)


def _order_for(owner: str, kind: str | None) -> tuple[str, ...]:
    if owner == "Wire":
        return WIRE_ORDER
    if kind is None:
        return PART_ORDER
    return TERMINAL_ORDER.get((owner, kind), ())


# ---------------------------------------------------------------- results


@dataclass(slots=True)
class ConnectResult:
    net_id: int
    wires_added: list[int]
    wires_modified: list[int]


@dataclass(slots=True)
class RemoveResult:
    removed_wires: list[int] = field(default_factory=list)
    merged_junctions: int = 0


@dataclass(slots=True)
class MoveResult:
    rerouted_wires: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def changed_lines(before: bytes, after: bytes) -> dict[str, int]:
    """Line-level diff of two serialized files: `{added, removed, modified}`."""
    a, b = before.split(b"\n"), after.split(b"\n")
    out = {"added": 0, "removed": 0, "modified": 0}
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "insert":
            out["added"] += j2 - j1
        elif op == "delete":
            out["removed"] += i2 - i1
        elif op == "replace":
            m = min(i2 - i1, j2 - j1)
            out["modified"] += m
            out["added"] += (j2 - j1) - m
            out["removed"] += (i2 - i1) - m
    return out


# ---------------------------------------------------------------- value handling


def _bad(message: str, hint: str = "") -> Ltp3Error:
    return Ltp3Error(BAD_VALUE, message, hint)


def _as_number(key: str, value: object) -> float:
    if isinstance(value, bool):
        raise _bad(f"{key}: expected a number, got {value!r}")
    if isinstance(value, int | float):
        n = float(value)
    elif isinstance(value, str):
        n = parse_number(value.strip())  # raises BAD_VALUE for anything that is not a number
    else:
        raise _bad(f"{key}: expected a number, got {value!r}")
    if not math.isfinite(n):
        raise _bad(f"{key}: {value!r} is not finite")
    return n


def normalise_value(key: str, value: object) -> str:
    """The text to store for `key`, or raise BAD_VALUE. Numbers use the .NET format, text keeps
    `*^!#` for line breaks, and CR/LF are refused everywhere else."""
    if isinstance(value, bool):
        if key in _BOOL_KEYS:
            return "True" if value else "False"
        raise _bad(f"{key}: a boolean is not valid here")
    if key in _INT_KEYS:
        n = _as_number(key, value)
        if n != int(n):
            raise _bad(f"{key}: expected an integer, got {value!r}")
        return str(int(n))
    if key in _DECIMAL_KEYS:
        return fmt(_as_number(key, value))
    if key in _COLOR_KEYS:
        if not isinstance(value, str) or not _COLOR.fullmatch(value):
            raise _bad(f"{key}: expected #AARRGGBB, got {value!r}")
        return value.upper()
    if key in _BOOL_KEYS:
        if value not in ("True", "False"):
            raise _bad(f"{key}: expected True or False, got {value!r}")
        return str(value)
    if key == "MatrixA":
        if not isinstance(value, str):
            raise _bad("MatrixA: expected three comma-separated numbers")
        parts = [fmt(_as_number(key, p.strip())) for p in value.split(",")]
        if len(parts) != 3:
            raise _bad("MatrixA: expected three comma-separated numbers")
        return ", ".join(parts)
    text = fmt(value) if isinstance(value, int | float) else value
    if not isinstance(text, str):
        raise _bad(f"{key}: unsupported value {value!r}")
    if "\r" in text:
        raise _bad(f"value for {key!r} contains CR", "Remove CR/LF characters.")
    if "\n" in text:
        if key not in _NEWLINE_KEYS:
            raise _bad(f"value for {key!r} contains LF", "Only Comments and Title may span lines.")
        text = text.replace("\n", NEWLINE_TOKEN)
    return text


def _same_value(old: str, new: str) -> bool:
    if old == new:
        return True
    try:
        return parse_number(old) == parse_number(new)  # "3.30" and 3.3 are the same setting
    except Ltp3Error:
        return False


def _check_text(what: str, text: str) -> None:
    if "\r" in text or "\n" in text:
        raise _bad(f"{what} contains CR or LF")


# ---------------------------------------------------------------- line-level helpers


def _dominant_eol(doc: Document) -> str:
    return next((line.eol for line in doc.lines if line.eol), "\r\n")


def _insert_after(doc: Document, anchor: TagLine | PropLine, new: list[TagLine | PropLine]) -> None:
    """Insert `new` after `anchor`, in order. If `anchor` was the unterminated last line, it gains
    a terminator and the last new line takes over the missing one."""
    eol = anchor.eol or _dominant_eol(doc)
    last_open = anchor.eol == ""
    if last_open:
        anchor.eol = eol
    cur = anchor
    for line in new:
        line.eol = eol
        doc.insert_after(cur, line)
        cur = line
    if last_open and new:
        new[-1].eol = ""


def _rank(order: tuple[str, ...], key: str) -> int:
    return order.index(key) if key in order else len(order)  # unknown keys sort last


def _block_of(doc: Document, head: TagLine) -> Block:
    """A Block for the tag line `head` with indices valid right now (found by identity)."""
    start = next(i for i, line in enumerate(doc.lines) if line is head)
    end = start + 1
    while end < len(doc.lines) and not isinstance(doc.lines[end], TagLine):
        end += 1
    return Block(doc, start, end)


def _put(doc: Document, head: TagLine, key: str, value: str, order: tuple[str, ...]) -> None:
    """Set `key` in the block of `head`; a new line goes right after the last line that must
    precede it. Views hold block indices that earlier edits shift, so callers pass the tag line
    they captured before editing and the block is located again here."""
    block = _block_of(doc, head)
    line = block.find(key)
    if line is not None:
        line.value = value
        return
    rank = _rank(order, key)
    anchor: TagLine | PropLine = block.head
    for ln in block.body:
        if isinstance(ln, PropLine) and _rank(order, ln.key) <= rank:
            anchor = ln
    _insert_after(doc, anchor, [PropLine.new(key, value, "")])


def _drop(doc: Document, head: TagLine, key: str) -> bool:
    block = _block_of(doc, head)
    line = block.find(key)
    if line is None:
        return False
    doc.remove(line)
    return True


def _remove_block(doc: Document, head: TagLine) -> None:
    block = _block_of(doc, head)
    # copy first: the slice is a fresh list of live lines, removal shifts the document's indices
    for line in [block.head, *block.body]:
        doc.remove(line)


def _other(side: str) -> str:
    return "E" if side == "S" else "S"


# ---------------------------------------------------------------- wire accessors


def _links(w: Wire, side: str) -> list[int]:
    return w.start_links if side == "S" else w.end_links


def _term(w: Wire, side: str) -> tuple[int, str, int] | None:
    return w.start if side == "S" else w.end


def _end_point(w: Wire, side: str) -> Point:
    pts = w.points
    return pts[0] if side == "S" else pts[-1]


def _set_links(doc: Document, head: TagLine, side: str, ids: list[int]) -> None:
    key = f"{side}Wires"
    if ids:
        _put(doc, head, key, ", ".join(map(str, ids)), WIRE_ORDER)
    else:
        _drop(doc, head, key)


def _set_term(doc: Document, head: TagLine, side: str, term: tuple[int, str, int] | None) -> None:
    keys = (f"{side}TermPID", f"{side}TermTyp", f"{side}TermNum")
    if term is None:
        for k in keys:
            _drop(doc, head, k)
        return
    for k, v in zip(keys, (str(term[0]), term[1], str(term[2])), strict=True):
        _put(doc, head, k, v, WIRE_ORDER)


def _set_points(doc: Document, head: TagLine, points: list[Point]) -> None:
    _put(doc, head, "Points", layout.format_points(points), WIRE_ORDER)


def _partners(tab: Tab, w: Wire, side: str) -> list[tuple[Wire, str]]:
    """The wires that meet `w` at its `side` end: those listed there that list `w` back at the same
    coordinate. Falls back to any end that lists `w` so a slightly malformed file still resolves."""
    point = _end_point(w, side)
    out: list[tuple[Wire, str]] = []
    for lid in _links(w, side):
        strict: list[tuple[Wire, str]] = []
        loose: list[tuple[Wire, str]] = []
        for o in tab.wires:
            if o.id != lid or o.block.head is w.block.head:
                continue
            for os in ("S", "E"):
                if w.id in _links(o, os):
                    (strict if _end_point(o, os) == point else loose).append((o, os))
        pick = strict or loose
        if pick:
            out.append(pick[0])
    return out


def _wires_at(tab: Tab, pid: int, kind: str | None = None, n: int | None = None) -> list[Wire]:
    """Wires with an end on part `pid` (optionally on one terminal)."""
    out: list[Wire] = []
    for w in tab.wires:
        for end in (w.start, w.end):
            if end is not None and end[0] == pid and (kind is None or end[1:] == (kind, n)):
                out.append(w)
                break
    return out


def _driver_side(w: Wire, term: Terminal) -> str:
    ref = (term.part.id, term.kind, term.n)
    return "S" if w.start == ref else "E"


def _next_id(tab: Tab) -> int:
    """All objects of a tab draw ids from one counter (FORMAT.md 5, hypothesis)."""
    ids = [0]
    ids += [p.id for p in tab.parts]
    ids += [w.id for w in tab.wires]
    ids += [w.net_id for w in tab.wires]
    ids += [s.id for s in tab.symbols]
    return max(ids) + 1


def _version(doc: Document) -> tuple[int, ...]:
    for line in doc.lines:
        if isinstance(line, TagLine) and line.tag == "Vers":
            try:
                return tuple(int(p) for p in line.args.strip().split("."))
            except ValueError:
                return ()
    return ()


def _wants_zindex(doc: Document) -> bool:
    """FORMAT.md 5: ZIndex appears from 3.5.3 and in everything the 3.6.8 app saves."""
    if any(w.block.find("ZIndex") is not None for t in Tree(doc).tabs for w in t.wires):
        return True
    return _version(doc) >= (3, 5, 3)


# ---------------------------------------------------------------- the editor


class Editor:
    """Edits one Document. `tree` is rebuilt after every mutation, so read it after the call."""

    def __init__(self, document: Document) -> None:
        self.doc = document
        self._tree: Tree | None = None

    @classmethod
    def from_bytes(cls, data: bytes) -> Editor:
        return cls(parse(data))

    def serialize(self) -> bytes:
        return serialize(self.doc)

    @property
    def tree(self) -> Tree:
        if self._tree is None:
            self._tree = Tree(self.doc)
        return self._tree

    def _fresh(self) -> Tree:
        self._tree = None
        return self.tree

    def _tab(self, ti: int) -> Tab:
        return self._fresh().tabs[ti]

    def _tab_index(self, tab: int | str) -> int:
        tree = self.tree
        if isinstance(tab, int):
            tree.tab(tab)  # range check, raises UNKNOWN_TAB
            return tab
        found = tree.tab(tab)
        for i, t in enumerate(tree.tabs):
            if t is found:
                return i
        raise Ltp3Error(UNKNOWN_TAB, f"no tab named {tab!r}")

    def _run(self, fn: Callable[[], T]) -> T:
        """Run one operation atomically: check the document afterwards, restore on any error."""
        snapshot = serialize(self.doc)
        try:
            result = fn()
            self.doc.check()
        except BaseException:
            self.doc.lines[:] = parse(snapshot).lines
            self._tree = None
            raise
        self._tree = None
        return result

    # ------------------------------------------------------------ properties
    def set_property(
        self,
        target: str,
        key: str,
        value: str | float | None,
        tab: int | str = 0,
        allow_unknown_key: bool = False,
    ) -> tuple[str | None, str | None]:
        """Set, add (`value` given) or remove (`value=None`) one property of a part or terminal.
        Returns `(old, new)` as stored text. Setting an equal value changes nothing."""
        ti = self._tab_index(tab)

        def op() -> tuple[str | None, str | None]:
            t = self._tab(ti)
            term: Terminal | None = None
            bits = target.rsplit(":", 2)
            if len(bits) == 3 and bits[1] in ("Input", "Output", "Aux") and bits[2].isdigit():
                term = t.terminal(target)
                part, block = term.part, term.block
                order = TERMINAL_ORDER.get((part.type, term.kind), ())
                known = order
            else:
                part = t.part(target)
                block = part.block
                order = PART_ORDER
                known = PART_KEYS.get(part.type, _BASE_PART)
            existing = block.find(key)
            old = existing.value if existing is not None else None
            if value is None:
                if existing is not None:
                    self.doc.remove(existing)
                return old, None
            new = normalise_value(key, value)
            if existing is None and key not in known and not allow_unknown_key:
                raise Ltp3Error(
                    UNKNOWN_KEY,
                    f"{key!r} is not a known property of {part.type}"
                    + (f" {term.kind} terminals" if term else ""),
                    "Pass allow_unknown_key=true to write it anyway.",
                )
            if old is not None and _same_value(old, new):
                return old, old
            _put(self.doc, block.head, key, new, order)
            return old, new

        return self._run(op)

    def rename_part(self, part: int | str, name: str, tab: int | str = 0) -> tuple[str, str]:
        ti = self._tab_index(tab)

        def op() -> tuple[str, str]:
            p = self._tab(ti).part(part)
            old = p.name
            _check_text("part name", name)
            if name != old:
                p.name = name
            return old, name

        return self._run(op)

    # ------------------------------------------------------------ add_part
    def add_part(
        self,
        type: str,
        name: str,
        tab: int | str = 0,
        *,
        part_number: str | None = None,
        outputs: Sequence[Mapping[str, float]] | None = None,
        inputs: Sequence[Mapping[str, float]] | None = None,
        resistance: float | None = None,
        near: int | str | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> Part:
        ti = self._tab_index(tab)
        pid = self._run(
            lambda: self._add_part(
                ti, type, name, part_number, outputs, inputs, resistance, near, x, y
            )
        )
        return self._tab(ti).part(pid)

    def _add_part(
        self,
        ti: int,
        ptype: str,
        name: str,
        part_number: str | None,
        outputs: Sequence[Mapping[str, float]] | None,
        inputs: Sequence[Mapping[str, float]] | None,
        resistance: float | None,
        near: int | str | None,
        x: int | None,
        y: int | None,
    ) -> int:
        if ptype not in layout.DEFAULT_WIDTH:  # also rejects Net: not creatable in v1
            raise _bad(
                f"cannot create a part of type {ptype!r}",
                "Use Power, Converter, LDO, Load, Resistor or Capacitor.",
            )
        _check_text("part name", name)
        if part_number is not None:
            _check_text("part number", part_number)
            if ptype not in ("Converter", "LDO"):
                raise _bad(f"{ptype} parts have no part number")
        if resistance is not None and ptype != "Resistor":
            raise _bad("resistance only applies to a Resistor")
        if outputs is not None and ptype not in ("Power", "Converter", "LDO"):
            raise _bad(f"{ptype} parts have no configurable outputs")
        if inputs is not None and ptype != "Load":
            raise _bad(f"{ptype} parts have no configurable inputs")

        out_keys = {"Power": {"vout", "iout_max"}, "LDO": {"vout", "iout_max"}}.get(
            ptype, {"vout", "nom_eff", "iout_max"}
        )
        out_map = {"vout": "Vout", "nom_eff": "NomEff", "iout_max": "IoutMax"}
        term_specs: list[tuple[str, int, dict[str, str]]] = []
        n_in = n_out = 0
        if ptype in ("Converter", "LDO"):
            term_specs.append(("Input", 1, {}))
            n_in = 1
        if ptype in ("Power", "Converter", "LDO"):
            outs = outputs if outputs else [{}]
            for i, spec in enumerate(outs, 1):
                extra = set(spec) - out_keys
                if extra:
                    raise _bad(f"unknown output field(s) {sorted(extra)} for {ptype}")
                term_specs.append(
                    (
                        "Output",
                        i,
                        {out_map[k]: normalise_value(out_map[k], v) for k, v in spec.items()},
                    )
                )
            n_out = len(outs)
        if ptype == "Load":
            ins = inputs if inputs else [{}]
            for i, spec in enumerate(ins, 1):
                extra = set(spec) - {"ipeak", "inom", "unit_exp"}
                if extra:
                    raise _bad(f"unknown input field(s) {sorted(extra)} for Load")
                vals: dict[str, str] = {}
                exp = spec.get("unit_exp", 0)
                for field_name, key in (("inom", "Inom"), ("ipeak", "Ipeak")):
                    if field_name in spec:
                        vals[key] = normalise_value(key, spec[field_name])
                        if exp:
                            vals[f"{key}U"] = normalise_value(f"{key}U", exp)
                term_specs.append(("Input", i, vals))
            n_in = len(ins)
        if ptype == "Resistor":
            term_specs += [("Input", 1, {}), ("Output", 1, {})]
            n_in = n_out = 1
        if ptype == "Capacitor":
            term_specs.append(("Aux", 1, {}))

        tab = self._tab(ti)
        near_part = tab.part(near) if near is not None else None
        width = layout.DEFAULT_WIDTH[ptype]
        height = layout.part_height(n_in, n_out)
        px, py = layout.place(tab, width, height, near=near_part, x=x, y=y)
        pid = _next_id(tab)

        props: list[tuple[str, str]] = [
            ("Color", layout.DEFAULT_COLOR[ptype]),
            ("Width", str(width)),
            ("Height", str(height)),
        ]
        if part_number is not None:
            props.append(("PartNumb", part_number))
        if resistance is not None:
            props.append(("Resstnce", normalise_value("Resstnce", resistance)))
        # first so that each key lands in canonical order (they are already listed in it)
        lines: list[TagLine | PropLine] = [
            TagLine("BPrt", " ", f"{ptype}, {pid}, {px}, {py}, {name}", "")
        ]
        lines += [
            PropLine.new(k, v, "")
            for k, v in sorted(props, key=lambda kv: _rank(PART_ORDER, kv[0]))
        ]
        for kind, n, vals in term_specs:
            lines.append(TagLine("Term", " ", f"{kind}, {n}", ""))
            order = TERMINAL_ORDER.get((ptype, kind), ())
            lines += [
                PropLine.new(k, v, "")
                for k, v in sorted(vals.items(), key=lambda kv: _rank(order, kv[0]))
            ]

        # parts go after the last Term of the last part, before the first Symb/Wire (FORMAT.md 2)
        last: Block = tab.blocks[0]
        for b in tab.blocks:
            if b.head.tag in ("Symb", "Wire"):
                break
            last = b
        anchor = self.doc.lines[last.end - 1]
        assert isinstance(anchor, TagLine | PropLine)
        _insert_after(self.doc, anchor, lines)
        return pid

    # ------------------------------------------------------------ wires
    def _append_wire(
        self,
        ti: int,
        wid: int,
        net_id: int,
        points: list[Point],
        s: tuple[int, str, int] | list[int] | None,
        e: tuple[int, str, int] | list[int] | None,
        zindex: str | None,
        color: str | None,
    ) -> None:
        """Append a wire at the end of the tab (wires come last, FORMAT.md 2). An end is either a
        terminal tuple or a list of wire ids (a junction)."""
        props: list[tuple[str, str]] = []
        if zindex is not None:
            props.append(("ZIndex", zindex))
        if color is not None:
            props.append(("WireClr", color))
        for side, end in (("S", s), ("E", e)):
            if isinstance(end, tuple):
                props += [
                    (f"{side}TermPID", str(end[0])),
                    (f"{side}TermTyp", end[1]),
                    (f"{side}TermNum", str(end[2])),
                ]
        for side, end in (("S", s), ("E", e)):
            if isinstance(end, list) and end:
                props.append((f"{side}Wires", ", ".join(map(str, end))))
        props.append(("Points", layout.format_points(points)))
        props.sort(key=lambda kv: _rank(WIRE_ORDER, kv[0]))
        lines: list[TagLine | PropLine] = [TagLine("Wire", " ", f"{wid}, {net_id}", "")]
        lines += [PropLine.new(k, v, "") for k, v in props]
        tab = self._tab(ti)
        anchor = self.doc.lines[tab.blocks[-1].end - 1]
        assert isinstance(anchor, TagLine | PropLine)
        _insert_after(self.doc, anchor, lines)

    def connect(self, source: str, sink: str, tab: int | str = 0) -> ConnectResult:
        """Wire an Output terminal to an unwired Input terminal."""
        ti = self._tab_index(tab)
        return self._run(lambda: self._connect(ti, source, sink))

    def _connect(self, ti: int, source: str, sink: str) -> ConnectResult:
        tab = self._tab(ti)
        src, dst = tab.terminal(source), tab.terminal(sink)
        if src.kind != "Output" or dst.kind != "Input":
            raise _bad(
                f"connect needs an Output source and an Input sink, got {src.kind} -> {dst.kind}",
                "Fan-out is made by connecting several Inputs to the same Output.",
            )
        if _wires_at(tab, dst.part.id, dst.kind, dst.n):
            raise Ltp3Error(
                TERMINAL_IN_USE,
                f"{dst.ref} already carries a wire",
                "Disconnect it first; an Input terminal takes one wire.",
            )
        graph = build_graph(tab)
        reach: set[int] = {dst.part.id}
        todo = [dst.part]
        while todo:
            for nxt in graph.downstream(todo.pop()):
                if nxt.id not in reach:
                    reach.add(nxt.id)
                    todo.append(nxt)
        if src.part.id in reach:
            raise Ltp3Error(
                CYCLE, f"{src.ref} -> {dst.ref} would close a loop through part {src.part.id}"
            )

        zindex = "-1" if _wants_zindex(self.doc) else None
        target = layout.terminal_anchor(dst)
        existing = _wires_at(tab, src.part.id, src.kind, src.n)
        base = _next_id(tab)

        if not existing:
            wid = base + 1  # the net id is allocated first, as the app does (wire 28 / net 27)
            pts = layout.route(layout.terminal_anchor(src), target)
            self._append_wire(
                ti, wid, base, pts, (src.part.id, "Output", src.n), (dst.part.id, "Input", dst.n),
                zindex, None,
            )  # fmt: skip
            return ConnectResult(base, [wid], [])

        w = existing[0]
        d = _driver_side(w, src)
        far = _other(d)
        color = w.block.find("WireClr")
        clr = color.value if color is not None else None
        zi = w.block.find("ZIndex")
        zindex = zi.value if zi is not None else zindex
        net_id = w.net_id

        if _links(w, far):
            # the far end already is a junction: join its clique instead of adding a second one
            members = _partners(tab, w, far)
            j = _end_point(w, far)
            first: Literal["horizontal", "vertical"] = (
                "horizontal" if self._vertical_at(w, far) else "vertical"
            )
            pts = layout.route(j, target, first=first)
            ids = [w.id, *[m.id for m, _ in members]]
            self._append_wire(
                ti, base, net_id, pts, ids, (dst.part.id, "Input", dst.n), zindex, clr
            )
            tab = self._tab(ti)
            by_head = {x.block.head: x for x in tab.wires}
            updates = []
            for m, ms in [(w, far), *members]:
                mw = by_head[m.block.head]
                updates.append((mw.block.head, ms, [*_links(mw, ms), base]))
            for head, ms, ids_ in updates:  # heads captured above: each edit shifts later indices
                _set_links(self.doc, head, ms, ids_)
            return ConnectResult(net_id, [base], ids)

        # split the driver's wire on its first segment: it becomes the trunk, the old far end moves
        # to a new branch, and the new sink hangs on a second new branch
        pts = w.points
        pd = pts if d == "S" else pts[::-1]  # points oriented from the driver
        p0 = pd[0]
        if len(pd) > 1:
            p1 = pd[1]
            length = abs(p1[0] - p0[0]) + abs(p1[1] - p0[1])
            off = min(max(layout.snap(length / 2), layout.GRID), length)
            dx = (p1[0] > p0[0]) - (p1[0] < p0[0])
            dy = (p1[1] > p0[1]) - (p1[1] < p0[1])
            j = (p0[0] + dx * off, p0[1] + dy * off)
            horizontal = p0[1] == p1[1]
        else:
            j, horizontal = p0, True
        trunk = layout.collapse([p0, j])
        rest = layout.collapse([j, *pd[1:]])
        b1, b2 = base, base + 1
        far_term = _term(w, far)
        branch2 = layout.route(j, target, first="vertical" if horizontal else "horizontal")

        # trunk (the old wire): keeps the driver end, loses the far terminal, gains the junction
        wh = w.block.head
        _set_term(self.doc, wh, far, None)
        _set_links(self.doc, wh, far, [b1, b2])
        _set_points(self.doc, wh, trunk if d == "S" else trunk[::-1])
        if d == "S":  # the old far end was E: branch 1 starts at the junction
            self._append_wire(ti, b1, net_id, rest, [w.id, b2], far_term, zindex, clr)
        else:  # the old far end was S: branch 1 ends at the junction
            self._append_wire(ti, b1, net_id, rest[::-1], far_term, [w.id, b2], zindex, clr)
        self._append_wire(
            ti, b2, net_id, branch2, [w.id, b1], (dst.part.id, "Input", dst.n), zindex, clr
        )
        return ConnectResult(net_id, [b1, b2], [w.id])

    @staticmethod
    def _vertical_at(w: Wire, side: str) -> bool:
        pts = w.points
        if len(pts) < 2:
            return False
        a, b = (pts[-2], pts[-1]) if side == "E" else (pts[1], pts[0])
        return a[0] == b[0] and a[1] != b[1]

    # ------------------------------------------------------------ removal
    def disconnect(self, terminal: str, tab: int | str = 0) -> RemoveResult:
        ti = self._tab_index(tab)

        def op() -> RemoveResult:
            t = self._tab(ti)
            term = t.terminal(terminal)
            heads = [w.block.head for w in _wires_at(t, term.part.id, term.kind, term.n)]
            return self._delete_wires(ti, heads)

        return self._run(op)

    def remove_part(self, part: int | str, tab: int | str = 0) -> RemoveResult:
        ti = self._tab_index(tab)

        def op() -> RemoveResult:
            t = self._tab(ti)
            p = t.part(part)
            heads = [w.block.head for w in _wires_at(t, p.id)]
            result = self._delete_wires(ti, heads)
            t = self._tab(ti)
            p = t.part(part)
            heads_ = [term.block.head for term in p.terminals] + [p.block.head]
            for h in heads_:
                _remove_block(self.doc, h)
            return result

        return self._run(op)

    def _find_wire(self, ti: int, head: TagLine) -> Wire | None:
        for w in self._tab(ti).wires:
            if w.block.head is head:
                return w
        return None

    def _delete_wires(self, ti: int, heads: list[TagLine]) -> RemoveResult:
        result = RemoveResult()
        spots: list[tuple[TagLine, str]] = []
        for head in heads:
            spots += self._delete_one(ti, head, result)
        # normalise only after every deletion, so a merge cannot absorb a wire still to be deleted
        while spots:
            head, side = spots.pop(0)
            spots += self._normalise(ti, head, side, result)
        result.removed_wires.sort()
        return result

    def _delete_one(
        self, ti: int, head: TagLine, result: RemoveResult
    ) -> list[tuple[TagLine, str]]:
        w = self._find_wire(ti, head)
        if w is None:
            return []
        tab = self._tab(ti)
        spots: list[tuple[TagLine, str]] = []
        edits: list[tuple[TagLine, str, list[int]]] = []
        for side in ("S", "E"):
            for pw, ps in _partners(tab, w, side):
                edits.append((pw.block.head, ps, [i for i in _links(pw, ps) if i != w.id]))
                spots.append((pw.block.head, ps))
        for h, ps, ids_ in edits:
            _set_links(self.doc, h, ps, ids_)
        result.removed_wires.append(w.id)
        _remove_block(self.doc, head)
        return spots

    def _normalise(
        self, ti: int, head: TagLine, side: str, result: RemoveResult
    ) -> list[tuple[TagLine, str]]:
        """Repair the junction `head`/`side` belongs to after a member left it."""
        w = self._find_wire(ti, head)
        if w is None:
            return []
        links = _links(w, side)
        has_terminal = _term(w, side) is not None
        if len(links) >= 2 or has_terminal:
            return []
        if not links:  # a lone wire end hanging in the air: the wire serves no purpose
            return self._delete_one(ti, head, result)
        tab = self._tab(ti)
        partners = _partners(tab, w, side)
        if len(partners) != 1 or _term(partners[0][0], partners[0][1]) is not None:
            return []
        self._merge(ti, w, side, partners[0][0], partners[0][1])
        result.merged_junctions += 1
        return []

    def _merge(self, ti: int, w1: Wire, s1: str, w2: Wire, s2: str) -> None:
        """Join two wires that meet at a junction of exactly these two into one wire. The earlier
        one in the file keeps its id; the other's far end moves onto it."""
        tab = self._tab(ti)
        order = {x.block.head: i for i, x in enumerate(tab.wires)}
        if order[w2.block.head] < order[w1.block.head]:
            w1, s1, w2, s2 = w2, s2, w1, s1
        # w1 survives. Its junction side s1 takes over w2's far end (side f2)
        f2 = _other(s2)
        pa = w1.points if s1 == "E" else w1.points[::-1]  # far end of w1 -> junction
        pb = w2.points if s2 == "S" else w2.points[::-1]  # junction -> far end of w2
        merged = layout.collapse([*pa, *pb])
        far_term = _term(w2, f2)
        far_links = _links(w2, f2)
        old_id = w2.id
        # others listing w2 at its far junction must now list w1
        h1, h2 = w1.block.head, w2.block.head
        relink = [
            (pw.block.head, ps, [w1.id if i == old_id else i for i in _links(pw, ps)])
            for pw, ps in _partners(tab, w2, f2)
        ]
        for h, ps, ids_ in relink:
            _set_links(self.doc, h, ps, ids_)
        _set_links(self.doc, h1, s1, [])
        _set_term(self.doc, h1, s1, far_term)
        _set_links(self.doc, h1, s1, far_links)
        _set_points(self.doc, h1, merged if s1 == "E" else merged[::-1])
        _remove_block(self.doc, h2)

    # ------------------------------------------------------------ move
    def move_part(self, part: int | str, x: int, y: int, tab: int | str = 0) -> MoveResult:
        """Move a part (snapped to the 5-unit grid), carrying the wire ends attached to it."""
        ti = self._tab_index(tab)
        return self._run(lambda: self._move_part(ti, part, x, y))

    def _move_part(self, ti: int, ref: int | str, x: int, y: int) -> MoveResult:
        result = MoveResult()
        t = self._tab(ti)
        p = t.part(ref)
        sx, sy = layout.snap(x), layout.snap(y)
        if (sx, sy) != (x, y):
            result.warnings.append(f"position ({x}, {y}) snapped to the grid: ({sx}, {sy})")
        dx, dy = sx - p.x, sy - p.y
        if dx == 0 and dy == 0:
            return result
        pid = p.id
        wire_heads = [w.block.head for w in _wires_at(t, pid)]
        fields = split_args("BPrt", p.block.head.args)
        fields[2], fields[3] = str(sx), str(sy)
        p.block.head.args = ", ".join(fields)

        def shift(pt: Point) -> Point:
            return pt[0] + dx, pt[1] + dy

        for head in wire_heads:
            w = self._find_wire(ti, head)
            if w is None:
                continue
            pts = w.points
            on = {
                "S": w.start is not None and w.start[0] == pid,
                "E": w.end is not None and w.end[0] == pid,
            }
            if on["S"] and on["E"]:
                new = [shift(pt) for pt in pts]
            else:
                a, b = pts[0], pts[-1]
                a2 = shift(a) if on["S"] else a
                b2 = shift(b) if on["E"] else b
                new = layout.route(a2, b2)
            _set_points(self.doc, w.block.head, new)
            result.rerouted_wires.append(w.id)
            # an Aux terminal can sit on a junction: the partners' ends must follow it
            tab = self._tab(ti)
            w = next(x_ for x_ in tab.wires if x_.block.head is head)
            for side in ("S", "E"):
                if on[side] and _links(w, side):
                    follow = []
                    for pw, ps in _partners(tab, w, side):
                        q = pw.points
                        moved = shift(q[0] if ps == "S" else q[-1])
                        q2 = layout.route(moved, q[-1]) if ps == "S" else layout.route(q[0], moved)
                        follow.append((pw.block.head, q2, pw.id))
                    for h, q2, qid in follow:
                        _set_points(self.doc, h, q2)
                        result.rerouted_wires.append(qid)
        result.rerouted_wires = sorted(set(result.rerouted_wires))
        return result
