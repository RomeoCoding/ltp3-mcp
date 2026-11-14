"""Load and save with the anti-corruption pipeline of PLAN.md section 2.5.

`save` order: path policy, existence/staleness, validate, serialize, re-parse check, temp file in
the target directory, fsync, backup, `os.replace`. The target is only touched by the last step.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import (
    BAD_PATH,
    BAD_VALUE,
    FILE_EXISTS,
    FILE_LOCKED,
    NOT_FOUND,
    STALE_FILE,
    Ltp3Error,
)
from .lossless import Document, Line
from .model import Tree
from .paths import check_writable
from .validate import Report, validate


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Loaded:
    """A file as opened: the parsed tree plus the exact bytes and hash it came from."""

    path: Path
    data: bytes
    sha256: str
    tree: Tree

    @property
    def read_only(self) -> bool:
        # only .ltp3 may be written (paths.check_writable); templates are reference data
        return self.path.suffix.lower() != ".ltp3"


@dataclass
class SaveResult:
    path: Path
    backup: Path | None
    sha256: str
    size: int
    report: Report


def load(path: str | os.PathLike[str]) -> Loaded:
    """Read any readable file (templates included; only writing is policed)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        if p.is_dir():  # Windows raises PermissionError, not IsADirectoryError, for a folder
            raise Ltp3Error(NOT_FOUND, f"{p} is a directory, not a file") from None
        if p.exists():
            raise Ltp3Error(
                FILE_LOCKED, f"cannot read {p}", "If it is open, close the file in LTpowerPlanner."
            ) from None
        raise Ltp3Error(NOT_FOUND, f"no such file: {p}") from None
    return Loaded(p, data, sha256_hex(data), Tree.from_bytes(data))


def _signature(doc: Document) -> list[object]:
    """Everything a reader can observe, with no reference to object identity."""

    def one(line: Line) -> tuple[object, ...]:
        # type name first: a TagLine and a RawLine must never compare equal by accident
        return (type(line).__name__, line.serialize())

    return [doc.bom, doc.encoding, *map(one, doc.lines)]


def _check_roundtrip(tree: Tree, data: bytes) -> None:
    """Re-parse the bytes about to be written and demand the same document and the same tree."""
    again = Tree.from_bytes(data)
    same_doc = _signature(again.document) == _signature(tree.document)
    same_tree = len(again.tabs) == len(tree.tabs) and all(
        len(a.parts) == len(b.parts) and len(a.wires) == len(b.wires)
        for a, b in zip(again.tabs, tree.tabs, strict=True)
    )
    if not (same_doc and same_tree):
        raise Ltp3Error(
            BAD_VALUE,
            "the serialized bytes do not re-parse to the in-memory document; nothing was written",
            "This is a bug in the writer; keep the original file and report it.",
        )


def _backup_path(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f"{target.name}.bak-{stamp}")
    n = 1
    while candidate.exists():  # two saves in one second must not clobber the earlier backup
        candidate = target.with_name(f"{target.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


def save(
    subject: Tree | Document,
    target: str | os.PathLike[str],
    *,
    overwrite: bool = False,
    opened_sha256: str | None = None,
) -> SaveResult:
    """Validate and write `subject` to `target`.

    The default refuses an existing target (`FILE_EXISTS`). With `overwrite=True` the old file is
    copied to `<name>.bak-YYYYMMDD-HHMMSS` first. Pass `opened_sha256` (from `Loaded.sha256`) when
    overwriting the file that was opened, so an edit made by the GUI meanwhile gives `STALE_FILE`.
    """
    # Always rebuild: a caller's Tree caches block indices that go stale after any edit, and the
    # document is the only source of truth for what gets written (QA2 finding 6).
    document = subject.document if isinstance(subject, Tree) else subject
    tree = Tree.from_document(document)
    path = check_writable(target)
    if not path.parent.is_dir():
        # creating directories would let a typo write somewhere unintended
        raise Ltp3Error(BAD_PATH, f"directory does not exist: {path.parent}")
    exists = path.exists()
    if exists and path.is_dir():
        raise Ltp3Error(BAD_PATH, f"{path} is a directory")
    if exists and not overwrite:
        raise Ltp3Error(
            FILE_EXISTS,
            f"{path} already exists",
            "Choose a new path, or pass overwrite=true (a backup is kept).",
        )
    if exists and opened_sha256 is not None and sha256_hex(path.read_bytes()) != opened_sha256:
        raise Ltp3Error(
            STALE_FILE,
            f"{path} changed on disk since it was opened",
            "Reopen the file, or save to a new path.",
        )

    report = validate(tree)
    report.raise_if_errors()  # includes Document.check(): line breaks inside values
    data = tree.document.serialize()
    _check_roundtrip(tree, data)

    # same directory as the target: os.replace is atomic only within one volume
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    backup: Path | None = None
    try:
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())  # data must be on disk before the name flips
            if exists:
                backup = _backup_path(path)
                shutil.copyfile(path, backup)
            os.replace(tmp, path)
        except OSError as exc:
            if backup is not None:
                # the target was not replaced, so the backup would be an orphan copy of it
                backup.unlink(missing_ok=True)
            if isinstance(exc, PermissionError):
                # on Windows an open file (or a read-only one) refuses os.replace with this
                raise Ltp3Error(
                    FILE_LOCKED,
                    f"cannot write {path}: it is locked or read-only",
                    "If it is open, close the file in LTpowerPlanner, then retry.",
                ) from exc
            raise Ltp3Error(BAD_PATH, f"could not write {path}: {exc}") from exc
    finally:
        # also runs for BaseException (a simulated or real kill signal): never leave the temp behind
        if tmp.exists():
            tmp.unlink()
    if path.read_bytes() != data:
        raise Ltp3Error(BAD_VALUE, f"{path} does not hold the bytes that were written")
    return SaveResult(path, backup, sha256_hex(data), len(data), report)


__all__ = ["Loaded", "SaveResult", "load", "save", "sha256_hex"]
