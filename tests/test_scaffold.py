from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import ltp3
from ltp3 import errors, paths
from ltp3.errors import Ltp3Error

ROOT = Path(__file__).resolve().parent.parent

SECTION_4_CODES = [
    "NOT_FOUND", "BAD_PATH", "PATH_NOT_ALLOWED", "PARSE_ERROR", "UNKNOWN_TREE", "UNKNOWN_TAB",
    "UNKNOWN_PART", "AMBIGUOUS_PART", "UNKNOWN_TERMINAL", "BAD_VALUE", "UNKNOWN_KEY",
    "TERMINAL_IN_USE", "WOULD_SHORT", "CYCLE", "READ_ONLY", "VALIDATION_FAILED", "FILE_EXISTS",
    "STALE_FILE", "UNPARSEABLE_ARGS", "NOTHING_TO_UNDO",
]  # fmt: skip


def test_version() -> None:
    assert isinstance(ltp3.__version__, str) and ltp3.__version__


@pytest.mark.parametrize("code", SECTION_4_CODES)
def test_error_code_constants(code: str) -> None:
    assert getattr(errors, code) == code


def test_error_codes_are_exactly_section_4() -> None:
    assert sorted(errors.ALL_CODES) == sorted(SECTION_4_CODES)


def test_ltp3error_fields() -> None:
    e = Ltp3Error(errors.BAD_VALUE, "nope", hint="try again")
    assert (e.code, e.message, e.hint) == ("BAD_VALUE", "nope", "try again")
    assert "BAD_VALUE" in str(e) and "nope" in str(e)
    assert Ltp3Error(errors.CYCLE, "x").hint == ""


def test_ltp3error_to_dict() -> None:
    d = Ltp3Error(errors.CYCLE, "loop", "break it").to_dict()
    assert d == {"code": "CYCLE", "message": "loop", "hint": "break it"}


def test_template_dir_default_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LTP3_TEMPLATE_DIR", raising=False)
    assert paths.template_dir() == Path(paths.DEFAULT_TEMPLATE_DIR)
    monkeypatch.setenv("LTP3_TEMPLATE_DIR", str(tmp_path))
    assert paths.template_dir() == tmp_path


def test_allowed_roots_default_is_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LTP3_ALLOWED_DIRS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.allowed_roots() == [tmp_path.resolve()]


def test_allowed_roots_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", os.pathsep.join([str(a), "", str(b)]))
    assert paths.allowed_roots() == [a.resolve(), b.resolve()]


@pytest.fixture
def allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))
    return tmp_path


def test_check_writable_ok(allowed: Path) -> None:
    target = allowed / "out.ltp3"
    assert paths.check_writable(target) == target.resolve()
    assert paths.check_writable(str(allowed / "sub" / "x.LTP3")).name == "x.LTP3"


def test_check_writable_refuses_template_extension(allowed: Path) -> None:
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(allowed / "t.ltp3t")
    assert ei.value.code == errors.BAD_PATH


@pytest.mark.parametrize("name", ["x.txt", "x", "x.ltp3.bak"])
def test_check_writable_refuses_other_extensions(allowed: Path, name: str) -> None:
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(allowed / name)
    assert ei.value.code == errors.BAD_PATH


def test_check_writable_refuses_outside_allowed(
    allowed: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = tmp_path_factory.mktemp("elsewhere")
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(other / "x.ltp3")
    assert ei.value.code == errors.PATH_NOT_ALLOWED


def test_check_writable_refuses_dotdot_escape(allowed: Path) -> None:
    sub = allowed / "sub"
    sub.mkdir()
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(sub / ".." / ".." / "x.ltp3")
    assert ei.value.code == errors.PATH_NOT_ALLOWED


def test_check_writable_refuses_sibling_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # "proj" must not allow "proj-evil": compare path components, not string prefixes
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(root))
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(tmp_path / "proj-evil" / "x.ltp3")
    assert ei.value.code == errors.PATH_NOT_ALLOWED


def test_check_writable_refuses_adi_tree_even_if_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ltc = tmp_path / "LTC"
    tdir = ltc / "LTpowerCAD2" / "LTpowerPlanner III" / "PlannerDataBase" / "FactorySolutions"
    tdir.mkdir(parents=True)
    monkeypatch.setenv("LTP3_TEMPLATE_DIR", str(tdir))
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(tdir / "x.ltp3")
    assert ei.value.code == errors.PATH_NOT_ALLOWED
    with pytest.raises(Ltp3Error) as ei2:
        paths.check_writable(ltc / "elsewhere" / "x.ltp3")
    assert ei2.value.code == errors.PATH_NOT_ALLOWED


def test_check_writable_refuses_program_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pf = tmp_path / "Program Files (x86)"
    pf.mkdir()
    monkeypatch.setenv("ProgramFiles(x86)", str(pf))
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))
    with pytest.raises(Ltp3Error) as ei:
        paths.check_writable(pf / "Vendor" / "x.ltp3")
    assert ei.value.code == errors.PATH_NOT_ALLOWED


def test_core_imports_do_not_pull_sdk() -> None:
    code = (
        "import sys, ltp3, ltp3.errors, ltp3.paths;"
        "assert 'mcp' not in sys.modules and 'pydantic' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_gitattributes_keeps_bytes() -> None:
    text = (ROOT / ".gitattributes").read_text()
    assert "*.ltp3 -text" in text and "*.ltp3t -text" in text


def test_gitignore_covers_adi_content() -> None:
    lines = (ROOT / ".gitignore").read_text().splitlines()
    for entry in ("samples/", "*.ltp3t", "*.bak-*", ".venv/"):
        assert entry in lines


def test_pins_are_exact() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    for pkg in ("mcp", "pytest", "pytest-cov", "hypothesis", "ruff", "mypy", "hatchling"):
        assert re.search(rf'"{pkg}==\d+\.\d+\.\d+"', text), pkg


# ---------------------------------------------------------------- QA fixes: path policy


def test_allowed_roots_strips_whitespace_and_quotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", os.pathsep.join([f' "{a}" ', f"'{b}'", ' "" ']))
    assert paths.allowed_roots() == [a.resolve(), b.resolve()]


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS streams, UNC and drive roots")
class TestWindowsPolicy:
    @pytest.fixture(autouse=True)
    def _allow_all(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path) + os.pathsep + "C:\\")

    @pytest.mark.parametrize("name", ["a.ltp3t:x.ltp3", "a.ltp3:stream.ltp3", "a.ltp3:$DATA.ltp3"])
    def test_refuses_alternate_data_stream(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(Ltp3Error) as ei:
            paths.check_writable(str(tmp_path / name))
        assert ei.value.code == errors.BAD_PATH

    def test_refuses_stream_on_directory_component(self, tmp_path: Path) -> None:
        with pytest.raises(Ltp3Error) as ei:
            paths.check_writable(str(tmp_path / "d:s" / "x.ltp3"))
        assert ei.value.code == errors.BAD_PATH

    @pytest.mark.parametrize(
        "raw",
        [
            r"\server\share\x.ltp3",
            "//server/share/x.ltp3",
            r"\?\C:\Users\Public\x.ltp3",
            r"\.\C:\x.ltp3",
        ],
    )
    def test_refuses_unc_and_extended_prefix(self, raw: str) -> None:
        with pytest.raises(Ltp3Error) as ei:
            paths.check_writable(raw)
        assert ei.value.code == errors.BAD_PATH

    @pytest.mark.parametrize("target", [r"C:\Windows\Temp\x.ltp3", r"C:\ProgramData\x\y.ltp3"])
    def test_refuses_windows_and_programdata(
        self, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        for var in ("SystemRoot", "windir", "ProgramData"):
            monkeypatch.delenv(var, raising=False)  # the literal fallback must still deny
        with pytest.raises(Ltp3Error) as ei:
            paths.check_writable(target)
        assert ei.value.code == errors.PATH_NOT_ALLOWED

    def test_refuses_programw6432(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pf = tmp_path / "PF64"
        pf.mkdir()
        monkeypatch.setenv("ProgramW6432", str(pf))
        with pytest.raises(Ltp3Error) as ei:
            paths.check_writable(pf / "V" / "x.ltp3")
        assert ei.value.code == errors.PATH_NOT_ALLOWED
