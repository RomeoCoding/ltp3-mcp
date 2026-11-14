"""In-memory registry of open trees, with an undo stack (PLAN.md 2.5 rules 1 and 5).

Nothing here touches the disk except `open` (read). Writing goes through `safeio.save` only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from .editor import Editor, changed_lines
from .errors import (
    BAD_PATH,
    BAD_VALUE,
    NOTHING_TO_UNDO,
    PARSE_ERROR,
    READ_ONLY,
    UNKNOWN_TAB,
    UNKNOWN_TREE,
    Ltp3Error,
)
from .model import Tab, Tree
from .safeio import Loaded, load, sha256_hex

T = TypeVar("T")

# Header tags of an empty project, as the app writes them (structure only, no template content).
_SKELETON = (
    "<Vers> 3.6.8\r\n<Tabs> 1, #FFFFFACD, {name}\r\n<Summ> 410, 555, 18, #FF0000FF, #00FFFFFF\r\n"
    "<DS05>\r\n"
)


@dataclass
class Session:
    tree_id: str
    editor: Editor
    path: Path | None
    opened_bytes: bytes  # baseline for dirty/preview/changed_lines
    opened_sha256: str
    read_only: bool
    is_template: bool
    undo_stack: list[bytes] = field(default_factory=list)

    @property
    def tree(self) -> Tree:
        return self.editor.tree

    def current_bytes(self) -> bytes:
        return self.editor.serialize()

    @property
    def dirty(self) -> bool:
        return self.current_bytes() != self.opened_bytes

    def tab_index(self, tab: int | str | None) -> int:
        """Resolve a tab argument to an index. A digit string that names no tab is an index
        (MCP clients often send numbers as text)."""
        tree = self.tree
        if tab is None:
            tab = 0
        if isinstance(tab, int):
            tree.tab(tab)  # range check -> UNKNOWN_TAB
            return tab
        for i, t in enumerate(tree.tabs):
            if t.name == tab:
                return i
        if tab.strip().isdigit():
            return self.tab_index(int(tab))
        raise Ltp3Error(UNKNOWN_TAB, f"no tab named {tab!r}")

    def tab(self, tab: int | str | None) -> Tab:
        return self.tree.tabs[self.tab_index(tab)]

    def edit(self, fn: Callable[[Editor], T]) -> tuple[T, dict[str, int]]:
        """Run one mutation. The editor restores itself on failure, so a failed edit pushes no
        undo snapshot; a successful one pushes the bytes from before it."""
        self.require_writable()
        before = self.current_bytes()
        result = fn(self.editor)
        after = self.current_bytes()
        if after != before:
            self.undo_stack.append(before)
        return result, changed_lines(before, after)

    def require_writable(self) -> None:
        if self.read_only:
            raise Ltp3Error(
                READ_ONLY,
                f"tree {self.tree_id} was opened read-only",
                "Open it again with read_only=false, or work on a copy via save_tree.",
            )

    def undo(self) -> int:
        self.require_writable()
        if not self.undo_stack:
            raise Ltp3Error(NOTHING_TO_UNDO, "no edits to undo in this tree")
        # a fresh Editor: model views from before the restore must not be reused
        self.editor = Editor.from_bytes(self.undo_stack.pop())
        return len(self.undo_stack)

    def mark_saved(self, path: Path, data: bytes, sha256: str) -> None:
        """After a successful save the file just written is the baseline."""
        self.path, self.opened_bytes, self.opened_sha256 = path, data, sha256
        self.is_template = False


class Registry:
    def __init__(self) -> None:
        self.lock = threading.RLock()  # tools run in worker threads; one tree edit at a time
        self._sessions: dict[str, Session] = {}
        self._counter = 0

    def _add(self, **kw: object) -> Session:
        self._counter += 1
        s = Session(tree_id=f"t{self._counter}", **kw)  # type: ignore[arg-type]
        self._sessions[s.tree_id] = s
        return s

    def get(self, tree_id: str) -> Session:
        try:
            return self._sessions[tree_id]
        except KeyError:
            raise Ltp3Error(
                UNKNOWN_TREE,
                f"no open tree {tree_id!r}",
                "Call open_tree or new_tree first and use the tree_id it returns.",
            ) from None

    def open(self, path: str, read_only: bool) -> Session:
        p = Path(path)
        try:
            if p.is_dir():
                raise Ltp3Error(BAD_PATH, f"{p} is a directory, not a file")
            loaded: Loaded = load(p)
        except ValueError as exc:  # e.g. an empty file
            raise Ltp3Error(PARSE_ERROR, f"{p} is not a readable power-tree file: {exc}") from exc
        except OSError as exc:  # PermissionError and friends
            raise Ltp3Error(BAD_PATH, f"cannot read {p}: {exc}") from exc
        for tab in loaded.tree.tabs:  # surfaces UNPARSEABLE_ARGS at open, not at first use
            _ = (tab.id, tab.name)
        return self._add(
            editor=Editor(loaded.tree.document),
            path=p,
            opened_bytes=loaded.data,
            opened_sha256=loaded.sha256,
            read_only=read_only,
            is_template=p.suffix.lower() == ".ltp3t",
        )

    def new(self, tab_name: str) -> Session:
        data = _SKELETON.format(name=tab_name).encode("cp1252")
        tree = Tree.from_bytes(data)
        if len(tree.tabs) != 1 or tree.tabs[0].name != tab_name:
            # a comma or line break in the name would change the tag's shape
            raise Ltp3Error(BAD_VALUE, f"unusable tab name {tab_name!r}")
        return self._add(
            editor=Editor(tree.document),
            path=None,
            opened_bytes=data,
            opened_sha256=sha256_hex(data),
            read_only=False,
            is_template=False,
        )

    def close(self, tree_id: str) -> None:
        self.get(tree_id)
        del self._sessions[tree_id]
