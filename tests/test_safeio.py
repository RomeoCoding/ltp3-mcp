"""P5: safe I/O. Every save runs inside tmp_path, which is the only allowed root."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from conftest import corpus_names
from ltp3 import errors, safeio
from ltp3.errors import Ltp3Error
from ltp3.lossless import Document, parse
from ltp3.model import Tree
from ltp3.safeio import load, save
from test_validate import CONV, LOAD, POWER, W_CL, W_PC, doc


@pytest.fixture(autouse=True)
def allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))
    return tmp_path


def _bytes() -> bytes:
    return doc(POWER, CONV, LOAD, W_PC, W_CL).document.serialize()


def _src(tmp_path: Path) -> Path:
    p = tmp_path / "in.ltp3"
    p.write_bytes(_bytes())
    return p


def test_load_records_bytes_and_hash(tmp_path: Path) -> None:
    p = _src(tmp_path)
    loaded = load(p)
    assert loaded.data == p.read_bytes()
    assert loaded.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    assert len(loaded.tree.tabs) == 1
    assert not loaded.read_only


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as exc:
        load(tmp_path / "nope.ltp3")
    assert exc.value.code == errors.NOT_FOUND


def test_templates_load_read_only(tmp_path: Path) -> None:
    t = tmp_path / "tpl.ltp3t"
    t.write_bytes(_bytes())
    assert load(t).read_only


def test_save_new_path_is_byte_identical_when_unedited(tmp_path: Path) -> None:
    loaded = load(_src(tmp_path))
    res = save(loaded.tree, tmp_path / "out.ltp3")
    assert (tmp_path / "out.ltp3").read_bytes() == loaded.data
    assert res.path == (tmp_path / "out.ltp3").resolve()
    assert res.backup is None
    assert res.sha256 == loaded.sha256


def test_one_edit_changes_exactly_one_line(tmp_path: Path) -> None:
    loaded = load(_src(tmp_path))
    loaded.tree.tabs[0].part(2).terminal("Output", 1).props["Vout"] = "3.3"
    save(loaded.tree, tmp_path / "out.ltp3")
    old = loaded.data.split(b"\r\n")
    new = (tmp_path / "out.ltp3").read_bytes().split(b"\r\n")
    assert len(old) == len(new)
    assert [(a, b) for a, b in zip(old, new, strict=True) if a != b] == [
        (b"\tVout    : 5", b"\tVout    : 3.3")
    ]


def test_bom_and_lone_cr_survive(tmp_path: Path) -> None:
    raw = b"\xef\xbb\xbf" + _bytes().replace(
        b"\tVout    : 12", b"\tComments: a\r\r\n\tVout    : 12"
    )
    src = tmp_path / "bom.ltp3"
    src.write_bytes(raw)
    save(load(src).tree, tmp_path / "out.ltp3")
    assert (tmp_path / "out.ltp3").read_bytes() == raw


def test_file_exists_refused_by_default(tmp_path: Path) -> None:
    src = _src(tmp_path)
    before = src.read_bytes()
    with pytest.raises(Ltp3Error) as exc:
        save(load(src).tree, src)
    assert exc.value.code == errors.FILE_EXISTS
    assert src.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))


def test_overwrite_creates_backup_with_old_bytes(tmp_path: Path) -> None:
    src = _src(tmp_path)
    loaded = load(src)
    loaded.tree.tabs[0].part(2).terminal("Output", 1).props["Vout"] = "3.3"
    res = save(loaded.tree, src, overwrite=True, opened_sha256=loaded.sha256)
    assert res.backup is not None
    assert res.backup.read_bytes() == loaded.data
    assert ".bak-" in res.backup.name
    assert b"Vout    : 3.3" in src.read_bytes()


def test_two_overwrites_in_one_second_keep_both_backups(tmp_path: Path) -> None:
    src = _src(tmp_path)
    loaded = load(src)
    a = save(loaded.tree, src, overwrite=True)
    b = save(loaded.tree, src, overwrite=True)
    assert a.backup is not None and b.backup is not None
    assert a.backup != b.backup
    assert a.backup.exists() and b.backup.exists()


def test_stale_file_refused(tmp_path: Path) -> None:
    src = _src(tmp_path)
    loaded = load(src)
    src.write_bytes(src.read_bytes() + b"<Zzzz> external edit\r\n")  # the GUI saved meanwhile
    with pytest.raises(Ltp3Error) as exc:
        save(loaded.tree, src, overwrite=True, opened_sha256=loaded.sha256)
    assert exc.value.code == errors.STALE_FILE
    assert b"external edit" in src.read_bytes()


def test_validation_errors_block_saving(tmp_path: Path) -> None:
    loaded = load(_src(tmp_path))
    etermnum = loaded.tree.tabs[0].wires[0].block.find("ETermNum")
    assert etermnum is not None
    etermnum.value = "9"
    with pytest.raises(Ltp3Error) as exc:
        save(loaded.tree, tmp_path / "out.ltp3")
    assert exc.value.code == errors.VALIDATION_FAILED
    assert not (tmp_path / "out.ltp3").exists()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_validation_warnings_do_not_block(tmp_path: Path) -> None:
    tree = doc(POWER, CONV, LOAD, W_PC)  # converter output and load input unconnected
    save(tree, tmp_path / "out.ltp3")
    assert (tmp_path / "out.ltp3").exists()


def test_linebreak_in_value_blocks_saving(tmp_path: Path) -> None:
    tree = doc(POWER, CONV, LOAD, W_PC, W_CL)
    tree.tabs[0].part(2).block.head.args += "\nrogue"
    with pytest.raises(Ltp3Error) as exc:
        save(tree, tmp_path / "out.ltp3")
    assert exc.value.code == errors.VALIDATION_FAILED
    assert not (tmp_path / "out.ltp3").exists()


def test_refuses_ltp3t_target(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as exc:
        save(load(_src(tmp_path)).tree, tmp_path / "out.ltp3t")
    assert exc.value.code == errors.BAD_PATH


@pytest.mark.skipif(sys.platform != "win32", reason="Program Files is a Windows install tree")
def test_refuses_program_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", pf)  # even if allow-listed, the install tree wins
    with pytest.raises(Ltp3Error) as exc:
        save(load(_src(tmp_path)).tree, Path(pf) / "ltp3-test" / "x.ltp3")
    assert exc.value.code == errors.PATH_NOT_ALLOWED


def test_refuses_outside_allowed(tmp_path: Path) -> None:
    other = tmp_path.parent / "elsewhere" / "x.ltp3"
    with pytest.raises(Ltp3Error) as exc:
        save(load(_src(tmp_path)).tree, other)
    assert exc.value.code == errors.PATH_NOT_ALLOWED


def test_missing_directory_is_not_created(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as exc:
        save(load(_src(tmp_path)).tree, tmp_path / "sub" / "x.ltp3")
    assert exc.value.code == errors.BAD_PATH
    assert not (tmp_path / "sub").exists()


class _Crash(BaseException):
    """Stands in for a process kill: not an Exception, so no handler may swallow it."""


def test_crash_between_temp_write_and_replace_leaves_target_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path)
    before = src.read_bytes()
    loaded = load(src)
    loaded.tree.tabs[0].part(2).terminal("Output", 1).props["Vout"] = "3.3"
    seen: list[Path] = []

    def boom(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> None:
        seen.append(Path(a))
        assert Path(a).exists()  # the temp file was fully written before the replace
        raise _Crash

    monkeypatch.setattr(safeio.os, "replace", boom)
    with pytest.raises(_Crash):
        save(loaded.tree, src, overwrite=True)
    assert src.read_bytes() == before
    assert seen[0].parent == src.parent  # same directory, or os.replace is not atomic
    assert not seen[0].exists()  # temp is cleaned up on the way out


def test_replace_failure_maps_to_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path)
    before = src.read_bytes()

    def boom(a: object, b: object) -> None:
        raise PermissionError("file is open in the GUI")

    monkeypatch.setattr(safeio.os, "replace", boom)
    with pytest.raises(Ltp3Error):
        save(load(src).tree, src, overwrite=True)
    assert src.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp-*"))


def test_reparse_mismatch_blocks_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a serializer bug must never reach disk: corrupt serialize() and expect a refusal
    loaded = load(_src(tmp_path))
    real = Document.serialize
    monkeypatch.setattr(Document, "serialize", lambda self: real(self).replace(b"Vout", b"Vot"))
    with pytest.raises(Ltp3Error):
        save(loaded.tree, tmp_path / "out.ltp3")
    assert not (tmp_path / "out.ltp3").exists()
    assert not list(tmp_path.glob("*.tmp-*"))


def test_save_accepts_document(tmp_path: Path) -> None:
    save(parse(_bytes()), tmp_path / "out.ltp3")
    assert (tmp_path / "out.ltp3").read_bytes() == _bytes()


@pytest.mark.corpus
@pytest.mark.parametrize("name", corpus_names()[::13])
def test_corpus_template_saves_byte_identical(
    name: str, template_dir: Path, tmp_path: Path
) -> None:
    loaded = load(template_dir / name)
    res = save(loaded.tree, tmp_path / "copy.ltp3")
    assert res.path.read_bytes() == (template_dir / name).read_bytes()
    assert Tree.load(res.path).tabs[0].name == loaded.tree.tabs[0].name
