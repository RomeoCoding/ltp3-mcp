"""Typed views over a lossless Document. Views hold references to blocks and parse on demand;
setters edit the underlying lines in place, so the Document stays the only copy of file data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from .errors import (
    AMBIGUOUS_PART,
    BAD_VALUE,
    FILE_LOCKED,
    NOT_FOUND,
    PARSE_ERROR,
    UNKNOWN_KEY,
    UNKNOWN_PART,
    UNKNOWN_TAB,
    UNKNOWN_TERMINAL,
    Ltp3Error,
)
from .lossless import Block, Document, parse, split_args

_POINT = re.compile(r"\((-?\d+),(-?\d+)\)")


class Props(Mapping[str, str]):
    """Read/write view of a block's property lines, in file order. Unknown keys are included."""

    def __init__(self, block: Block) -> None:
        self._block = block

    def __getitem__(self, key: str) -> str:
        line = self._block.find(key)
        if line is None:
            raise KeyError(key)
        return line.value

    def __iter__(self) -> Iterator[str]:
        return (line.key for line in self._block.props)

    def __len__(self) -> int:
        return len(self._block.props)

    def __setitem__(self, key: str, value: str) -> None:
        if "\r" in value or "\n" in value:
            raise Ltp3Error(BAD_VALUE, f"value for {key!r} contains CR or LF")
        line = self._block.find(key)
        if line is None:
            # Inserting needs the canonical key order and access to Document.lines: P4's editor.
            raise Ltp3Error(
                UNKNOWN_KEY, f"{key!r} is not present", "Adding keys is the editor's job."
            )
        line.value = value


def _int(text: str, what: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise Ltp3Error(PARSE_ERROR, f"{what}: expected an integer, got {text!r}") from None


class _View:
    """Base: a view of one tag block."""

    __slots__ = ("block",)

    def __init__(self, block: Block) -> None:
        self.block = block

    @property
    def props(self) -> Props:
        return Props(self.block)

    def _fields(self) -> list[str]:
        return split_args(self.block.head.tag, self.block.head.args)

    def _set_field(self, index: int, value: str) -> None:
        fields = self._fields()
        fields[index] = value
        self.block.head.args = ", ".join(fields)


class Terminal(_View):
    __slots__ = ("part",)

    def __init__(self, block: Block, part: Part) -> None:
        super().__init__(block)
        self.part = part

    @property
    def kind(self) -> str:
        return self._fields()[0]

    @property
    def n(self) -> int:
        return _int(self._fields()[1], "terminal number")

    @property
    def ref(self) -> str:
        return f"{self.part.id}:{self.kind}:{self.n}"

    def __repr__(self) -> str:
        return f"Terminal({self.ref})"


class Part(_View):
    __slots__ = ("terminals",)

    def __init__(self, block: Block) -> None:
        super().__init__(block)
        self.terminals: list[Terminal] = []

    @property
    def type(self) -> str:
        return self._fields()[0]

    @property
    def id(self) -> int:
        return _int(self._fields()[1], "part id")

    @property
    def x(self) -> int:
        return _int(self._fields()[2], "part x")

    @property
    def y(self) -> int:
        return _int(self._fields()[3], "part y")

    @property
    def name(self) -> str:
        return self._fields()[4]

    @name.setter
    def name(self, value: str) -> None:
        if "\r" in value or "\n" in value:
            raise Ltp3Error(BAD_VALUE, "part name contains CR or LF")
        self._set_field(4, value)

    def terminal(self, kind: str, n: int) -> Terminal:
        for t in self.terminals:
            if t.kind == kind and t.n == n:
                return t
        raise Ltp3Error(UNKNOWN_TERMINAL, f"part {self.id} has no terminal {kind}:{n}")

    def __repr__(self) -> str:
        return f"Part({self.type} {self.id})"


class Wire(_View):
    @property
    def id(self) -> int:
        return _int(self._fields()[0], "wire id")

    @property
    def net_id(self) -> int:
        return _int(self._fields()[1], "net id")

    def _end(self, side: str) -> tuple[int, str, int] | None:
        pid = self.props.get(f"{side}TermPID")
        typ = self.props.get(f"{side}TermTyp")
        num = self.props.get(f"{side}TermNum")
        if pid is None or typ is None or num is None:
            return None
        return _int(pid, "wire terminal part"), typ, _int(num, "wire terminal number")

    @property
    def start(self) -> tuple[int, str, int] | None:
        return self._end("S")

    @property
    def end(self) -> tuple[int, str, int] | None:
        return self._end("E")

    def _links(self, key: str) -> list[int]:
        raw = self.props.get(key)
        if raw is None or not raw.strip():
            return []
        return [_int(p.strip(), key) for p in raw.split(",")]

    @property
    def start_links(self) -> list[int]:
        return self._links("SWires")

    @property
    def end_links(self) -> list[int]:
        return self._links("EWires")

    @property
    def points(self) -> list[tuple[int, int]]:
        raw = self.props.get("Points", "")
        return [(int(a), int(b)) for a, b in _POINT.findall(raw)]


class Symbol(_View):
    @property
    def type(self) -> str:
        return self._fields()[0]

    @property
    def id(self) -> int:
        return _int(self._fields()[1], "symbol id")


class Tab:
    def __init__(self, blocks: list[Block]) -> None:
        self.block = blocks[0]
        self.blocks = blocks
        self.parts: list[Part] = []
        self.symbols: list[Symbol] = []
        self.wires: list[Wire] = []
        for b in blocks[1:]:
            tag = b.head.tag
            if tag == "BPrt":
                self.parts.append(Part(b))
            elif tag == "Term":
                if not self.parts:
                    raise Ltp3Error(PARSE_ERROR, "<Term> before any <BPrt>")
                part = self.parts[-1]
                part.terminals.append(Terminal(b, part))  # a Term belongs to the preceding BPrt
            elif tag == "Symb":
                self.symbols.append(Symbol(b))
            elif tag == "Wire":
                self.wires.append(Wire(b))
            # Summ, DSnn and unknown tags stay reachable via self.blocks and are never dropped

    def _fields(self) -> list[str]:
        return split_args("Tabs", self.block.head.args)

    @property
    def id(self) -> int:
        return _int(self._fields()[0], "tab id")

    @property
    def color(self) -> str:
        return self._fields()[1]

    @property
    def name(self) -> str:
        return self._fields()[2]

    def part(self, ref: int | str) -> Part:
        """By id (int, or digit string) or by exact name when unambiguous."""
        if isinstance(ref, int) or ref.isdigit():
            pid = int(ref)
            hits = [p for p in self.parts if p.id == pid]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise Ltp3Error(AMBIGUOUS_PART, f"{len(hits)} parts have id {pid}")
            if isinstance(ref, int):
                raise Ltp3Error(UNKNOWN_PART, f"no part with id {pid} in tab {self.name!r}")
        hits = [p for p in self.parts if p.name == ref]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise Ltp3Error(UNKNOWN_PART, f"no part {ref!r} in tab {self.name!r}")
        raise Ltp3Error(
            AMBIGUOUS_PART,
            f"{len(hits)} parts are named {ref!r}",
            "Address the part by id: " + ", ".join(str(p.id) for p in hits),
        )

    def terminal(self, ref: str) -> Terminal:
        """`"<part>:<kind>:<n>"`. rsplit so a part name containing ':' still works."""
        bits = ref.rsplit(":", 2)
        if len(bits) != 3 or not bits[2].isdigit():
            raise Ltp3Error(UNKNOWN_TERMINAL, f"bad terminal reference {ref!r}", "Use 'id:Kind:n'.")
        try:
            part = self.part(bits[0])
        except Ltp3Error as exc:
            if exc.code == UNKNOWN_PART:
                raise Ltp3Error(UNKNOWN_TERMINAL, f"{ref!r}: {exc.message}") from None
            raise
        return part.terminal(bits[1], int(bits[2]))

    def all_terminals(self) -> list[Terminal]:
        return [t for p in self.parts for t in p.terminals]


class Tree:
    def __init__(self, document: Document) -> None:
        self.document = document
        if not document.blocks():
            # an empty or non-LTpowerPlanner file has no tag at all (lossless.parse stays total)
            raise Ltp3Error(PARSE_ERROR, "no <tags> found: not an LTpowerPlanner file")
        header, tab_blocks = document.split_tabs()
        self.header = header
        self.tabs = [Tab(b) for b in tab_blocks]

    @classmethod
    def from_document(cls, document: Document) -> Tree:
        return cls(document)

    @classmethod
    def from_bytes(cls, data: bytes) -> Tree:
        return cls(parse(data))

    @classmethod
    def load(cls, path: str | Path) -> Tree:
        p = Path(path)
        try:
            data = p.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            if p.is_dir():  # Windows raises PermissionError, not IsADirectoryError, for a folder
                raise Ltp3Error(NOT_FOUND, f"{p} is a directory, not a file") from None
            if p.exists():
                raise Ltp3Error(
                    FILE_LOCKED,
                    f"cannot read {p}",
                    "If it is open, close the file in LTpowerPlanner.",
                ) from None
            raise Ltp3Error(NOT_FOUND, f"no such file: {p}") from None
        return cls.from_bytes(data)

    def tab(self, ref: int | str) -> Tab:
        """By 0-based index or exact name."""
        if isinstance(ref, int):
            if 0 <= ref < len(self.tabs):
                return self.tabs[ref]
            raise Ltp3Error(UNKNOWN_TAB, f"tab index {ref} out of range (0..{len(self.tabs) - 1})")
        hits = [t for t in self.tabs if t.name == ref]
        if not hits:
            raise Ltp3Error(UNKNOWN_TAB, f"no tab named {ref!r}")
        return hits[0]
