"""Bytes <-> Document. Every byte of the input lives in exactly one raw piece, so `serialize` is
exact. Nothing here interprets values; edits replace individual pieces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import BAD_VALUE, UNPARSEABLE_ARGS, Ltp3Error

_BOM = b"\xef\xbb\xbf"

# Tag names are alphanumeric (BPrt, DS11); args are whatever follows, verbatim.
_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9]*)>( ?)(.*)", re.DOTALL)
# Key is padded with spaces (8 wide in practice); the width is kept as found, not enforced.
_PROP = re.compile(r"\t([A-Za-z0-9_]+ *):( ?)(.*)", re.DOTALL)

# Piece counts after splitting args on ", ". BPrt and Tabs end in a free-text name that may
# contain ", ", so the split is capped and the last piece takes the remainder (FORMAT.md 2.2, 3.1).
_ARG_PIECES = {"Tabs": 3, "Summ": 5, "BPrt": 5, "Term": 2, "Wire": 2, "Symb": 6}
_FREE_TEXT_LAST = {"Tabs", "BPrt"}


# eq=False on the line classes: equal-valued lines in different parts must stay distinct objects
@dataclass(slots=True, eq=False)
class TagLine:
    """`<Tag>` + `sep` + `args`. `sep` is kept apart because `<DS11>0.27` has none while
    `<Note> ` has one with empty args."""

    tag: str
    sep: str
    args: str
    eol: str

    def serialize(self) -> str:
        return f"<{self.tag}>{self.sep}{self.args}{self.eol}"


@dataclass(slots=True, eq=False)
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

    @classmethod
    def new(cls, key: str, value: str, eol: str) -> PropLine:
        """A fresh line; the key is padded to 8 columns as the corpus does (`\tVout    : 5`)."""
        return cls(key.ljust(8), " ", value, eol)


@dataclass(slots=True, eq=False)
class RawLine:
    """Anything that is neither grammar; kept so future file versions stay lossless."""

    text: str
    eol: str

    def serialize(self) -> str:
        return f"{self.text}{self.eol}"


Line = TagLine | PropLine | RawLine


@dataclass(slots=True, eq=False)
class Block:
    """A tag line and the lines after it: `doc.lines[start:end]`, `start` being the tag line.

    The indices are valid until the next `insert_after`/`remove`; call `blocks()` again after.
    """

    doc: Document
    start: int
    end: int

    @property
    def head(self) -> TagLine:
        line = self.doc.lines[self.start]
        assert isinstance(line, TagLine)
        return line

    @property
    def body(self) -> list[Line]:
        """The live line objects after the tag (the list itself is a fresh slice)."""
        return self.doc.lines[self.start + 1 : self.end]

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
        starts = [i for i, line in enumerate(self.lines) if isinstance(line, TagLine)]
        ends = [*starts[1:], len(self.lines)]
        return [Block(self, s, e) for s, e in zip(starts, ends, strict=True)]

    def _index(self, line: Line) -> int:
        # identity scan: list.index() would use ==, and the explicit `is` keeps that guaranteed
        for i, candidate in enumerate(self.lines):
            if candidate is line:
                return i
        raise ValueError("line is not part of this document")

    def insert_after(self, anchor: Line, new: Line) -> None:
        """Insert `new` right after `anchor` (matched by identity)."""
        self.lines.insert(self._index(anchor) + 1, new)

    def remove(self, line: Line) -> None:
        """Remove `line` (matched by identity, so an identical line elsewhere stays)."""
        del self.lines[self._index(line)]

    def check(self) -> None:
        """Assert the document survives a write: re-parsing `serialize()` must give the same
        number of lines with the same types. Catches values or args holding a line break, which
        would turn into extra lines. Raises `Ltp3Error(BAD_VALUE)`."""
        again = parse(serialize(self)).lines
        if len(again) != len(self.lines) or any(
            type(x) is not type(y) for x, y in zip(again, self.lines, strict=True)
        ):
            raise Ltp3Error(
                BAD_VALUE,
                f"document does not re-parse to the same lines ({len(self.lines)} lines became "
                f"{len(again)}); a value or argument probably contains a line break",
                "Remove CR/LF characters from the edited text.",
            )

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
    text = "".join(line.serialize() for line in doc.lines)
    try:
        body = text.encode(doc.encoding, errors="surrogateescape")
    except UnicodeEncodeError as exc:
        # the file's own code page cannot hold the edit; refuse rather than corrupt or crash
        raise Ltp3Error(
            BAD_VALUE,
            f"character {text[exc.start]!r} cannot be stored in this file's {doc.encoding} "
            "encoding",
            "Use only characters the document's encoding supports.",
        ) from exc
    return _BOM + body if doc.bom else body


def split_args(tag: str, args: str) -> list[str]:
    """Split a known tag's args into fields; raise UNPARSEABLE_ARGS if the shape is off.

    Joining the result with ", " always reproduces `args`; callers may rely on that.
    """
    want = _ARG_PIECES.get(tag)
    if want is None:
        raise Ltp3Error(UNPARSEABLE_ARGS, f"no argument grammar for tag <{tag}>")
    pieces = args.split(", ", want - 1) if tag in _FREE_TEXT_LAST else args.split(", ")
    # only the count can be wrong: ", ".join(split) == args by construction, so no round-trip check
    if len(pieces) != want:
        raise Ltp3Error(
            UNPARSEABLE_ARGS,
            f"<{tag}> expects {want} fields, got {len(pieces)} in {args!r}",
            "The line is left untouched; edit it as raw text.",
        )
    return pieces
