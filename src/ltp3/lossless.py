"""Bytes <-> Document. Every byte of the input lives in exactly one raw piece, so `serialize` is
exact. Nothing here interprets values; edits replace individual pieces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import UNPARSEABLE_ARGS, Ltp3Error

_BOM = b"\xef\xbb\xbf"

# Tag names are alphanumeric (BPrt, DS11); args are whatever follows, verbatim.
_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9]*)>( ?)(.*)", re.DOTALL)
# Key is padded with spaces (8 wide in practice); the width is kept as found, not enforced.
_PROP = re.compile(r"\t([A-Za-z0-9_]+ *):( ?)(.*)", re.DOTALL)

# Piece counts after splitting args on ", ". BPrt and Tabs end in a free-text name that may
# contain ", ", so the split is capped and the last piece takes the remainder (FORMAT.md 2.2, 3.1).
_ARG_PIECES = {"Tabs": 3, "Summ": 5, "BPrt": 5, "Term": 2, "Wire": 2, "Symb": 6}
_FREE_TEXT_LAST = {"Tabs", "BPrt"}


@dataclass(slots=True)
class TagLine:
    """`<Tag>` + `sep` + `args`. `sep` is kept apart because `<DS11>0.27` has none while
    `<Note> ` has one with empty args."""

    tag: str
    sep: str
    args: str
    eol: str

    def serialize(self) -> str:
        return f"<{self.tag}>{self.sep}{self.args}{self.eol}"


@dataclass(slots=True)
class PropLine:
    """TAB + `key_raw` (padded) + `:` + `sep` + `value`."""

    key_raw: str
    sep: str
    value: str
    eol: str

    @property
    def key(self) -> str:
        return self.key_raw.rstrip(" ")

    def serialize(self) -> str:
        return f"\t{self.key_raw}:{self.sep}{self.value}{self.eol}"


@dataclass(slots=True)
class RawLine:
    """Anything that is neither grammar; kept so future file versions stay lossless."""

    text: str
    eol: str

    def serialize(self) -> str:
        return f"{self.text}{self.eol}"


Line = TagLine | PropLine | RawLine


@dataclass(slots=True)
class Block:
    """A tag line with the lines that follow it. `body` holds the live line objects."""

    head: TagLine
    body: list[Line] = field(default_factory=list)

    @property
    def props(self) -> list[PropLine]:
        return [line for line in self.body if isinstance(line, PropLine)]

    def find(self, key: str) -> PropLine | None:
        for line in self.body:
            if isinstance(line, PropLine) and line.key == key:
                return line
        return None


@dataclass(slots=True)
class Document:
    bom: bool
    encoding: str
    lines: list[Line]

    def serialize(self) -> bytes:
        return serialize(self)

    def blocks(self) -> list[Block]:
        """Group lines into blocks. Lines before the first tag belong to no block (they stay in
        `lines`, so nothing is lost)."""
        out: list[Block] = []
        for line in self.lines:
            if isinstance(line, TagLine):
                out.append(Block(line))
            elif out:
                out[-1].body.append(line)
        return out

    def split_tabs(self) -> tuple[list[Block], list[list[Block]]]:
        """Header blocks, then one block list per `<Tabs>` (from `<Tabs>` to the next one)."""
        header: list[Block] = []
        tabs: list[list[Block]] = []
        for block in self.blocks():
            if block.head.tag == "Tabs":
                tabs.append([block])
            elif tabs:
                tabs[-1].append(block)
            else:
                header.append(block)
        return header, tabs


def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # surrogateescape keeps bytes cp1252 leaves undefined (e.g. 0x81), so re-encoding is exact
        return data.decode("cp1252", errors="surrogateescape"), "cp1252"


def _classify(text: str, eol: str) -> Line:
    m = _TAG.fullmatch(text)
    if m:
        return TagLine(m.group(1), m.group(2), m.group(3), eol)
    m = _PROP.fullmatch(text)
    if m:
        return PropLine(m.group(1), m.group(2), m.group(3), eol)
    return RawLine(text, eol)


def parse(data: bytes) -> Document:
    """Parse bytes into a Document. Never fails: unknown shapes become `RawLine`."""
    bom = data.startswith(_BOM)
    text, encoding = _decode(data[len(_BOM) :] if bom else data)
    lines: list[Line] = []
    pos, n = 0, len(text)
    while pos < n:
        nl = text.find("\n", pos)
        if nl < 0:
            lines.append(_classify(text[pos:], ""))
            break
        # a lone CR is data (FORMAT.md 1); only CRLF or a bare LF ends a line
        if nl > pos and text[nl - 1] == "\r":
            lines.append(_classify(text[pos : nl - 1], "\r\n"))
        else:
            lines.append(_classify(text[pos:nl], "\n"))
        pos = nl + 1
    return Document(bom, encoding, lines)


def serialize(doc: Document) -> bytes:
    """Concatenate the raw pieces. No normalisation."""
    body = "".join(line.serialize() for line in doc.lines).encode(
        doc.encoding, errors="surrogateescape"
    )
    return _BOM + body if doc.bom else body


def split_args(tag: str, args: str) -> list[str]:
    """Split a known tag's args into fields; raise UNPARSEABLE_ARGS if the shape is off.

    Joining the result with ", " always reproduces `args`; callers may rely on that.
    """
    want = _ARG_PIECES.get(tag)
    if want is None:
        raise Ltp3Error(UNPARSEABLE_ARGS, f"no argument grammar for tag <{tag}>")
    pieces = args.split(", ", want - 1) if tag in _FREE_TEXT_LAST else args.split(", ")
    if len(pieces) != want or ", ".join(pieces) != args:
        raise Ltp3Error(
            UNPARSEABLE_ARGS,
            f"<{tag}> expects {want} fields, got {len(pieces)} in {args!r}",
            "The line is left untouched; edit it as raw text.",
        )
    return pieces
