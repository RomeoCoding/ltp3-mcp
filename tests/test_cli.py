"""P8: the `ltp3` command line. Synthetic trees only; exit codes are the contract for CI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ltp3.cli import main

CRLF = "\r\n"
Capture = pytest.CaptureFixture[str]


def _tree(load_amps: str = "1", broken_wire: bool = False) -> bytes:
    lines = [
        "<Vers> 3.6.8",
        "<Tabs> 1, #FFFFFACD, T",
        "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF",
        "<BPrt> Power, 1, 0, 0, SRC",
        "<Term> Output, 1",
        "\tVout    : 5",
        "<BPrt> Converter, 2, 0, 0, BUCK",
        "<Term> Input, 1",
        "<Term> Output, 1",
        "\tVout    : 1",
        "\tNomEff  : 80",
        "<BPrt> Load, 3, 0, 0, LOAD",
        "<Term> Input, 1",
        f"\tIpeak   : {load_amps}",
    ]
    for i, (a, b) in enumerate([("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")]):
        lines.append(f"<Wire> {100 + i}, {1000 + i}")
        for tag, ref in (("S", a), ("E", b)):
            pid, kind, n = ref.split(":")
            if broken_wire and tag == "E" and i == 1:
                pid = "99"  # no such part
            lines += [f"\t{tag}TermPID: {pid}", f"\t{tag}TermTyp: {kind}", f"\t{tag}TermNum: {n}"]
        lines.append("\tPoints  : (0,0)(5,0)")
    return "".join(x + CRLF for x in lines).encode()


@pytest.fixture
def good(tmp_path: Path) -> Path:
    p = tmp_path / "good.ltp3"
    p.write_bytes(_tree())
    return p


@pytest.fixture
def bad(tmp_path: Path) -> Path:
    p = tmp_path / "bad.ltp3"
    p.write_bytes(_tree(broken_wire=True))
    return p


def test_solve_json(good: Path, capsys: Capture) -> None:
    assert main(["solve", str(good), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    t = doc["tabs"][0]
    assert t["tab"] == 0 and t["complete"] is True
    assert t["display"]["pin_w"] == 1.25 and t["display"]["efficiency_pct"] == 80.0


def test_solve_text_and_tab_selection(good: Path, capsys: Capture) -> None:
    assert main(["solve", str(good), "--tab", "0"]) == 0
    out = capsys.readouterr().out
    assert "Pin" in out and "1.25" in out and "80" in out
    assert main(["solve", str(good), "--tab", "T"]) == 0  # by name


def test_solve_unknown_tab_is_an_error(good: Path, capsys: Capture) -> None:
    assert main(["solve", str(good), "--tab", "7"]) == 2
    assert "UNKNOWN_TAB" in capsys.readouterr().err


def test_solve_mode_flag(good: Path, capsys: Capture) -> None:
    assert main(["solve", str(good), "--json", "--mode", "nominal"]) == 0
    assert json.loads(capsys.readouterr().out)["tabs"][0]["current_mode"] == "nominal"


def test_missing_file_exit_2(tmp_path: Path, capsys: Capture) -> None:
    assert main(["validate", str(tmp_path / "nope.ltp3")]) == 2
    assert "NOT_FOUND" in capsys.readouterr().err


def test_validate_ok_exit_0(good: Path, capsys: Capture) -> None:
    assert main(["validate", str(good)]) == 0
    assert "0 error" in capsys.readouterr().out


def test_validate_errors_exit_1(bad: Path, capsys: Capture) -> None:
    assert main(["validate", str(bad)]) == 1
    assert "error" in capsys.readouterr().out
    assert main(["validate", str(bad), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and doc["errors"]


def test_validate_strict_fails_on_warnings(tmp_path: Path) -> None:
    # an Ipeak above the converter's IoutMax is a warning, not an error
    p = tmp_path / "warn.ltp3"
    p.write_bytes(_tree().replace(b"\tNomEff  : 80", b"\tNomEff  : 80\r\n\tIoutMax : 0.5"))
    assert main(["validate", str(p)]) == 0
    assert main(["validate", str(p), "--strict"]) == 1


def test_diff_identical_and_different(good: Path, tmp_path: Path, capsys: Capture) -> None:
    other = tmp_path / "other.ltp3"
    other.write_bytes(_tree(load_amps="2"))
    assert main(["diff", str(good), str(good)]) == 0
    assert "no differences" in capsys.readouterr().out
    assert main(["diff", str(good), str(other)]) == 1
    assert "Ipeak" in capsys.readouterr().out
    assert main(["diff", str(good), str(other), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_export_stdout_and_file(good: Path, tmp_path: Path, capsys: Capture) -> None:
    assert main(["export", str(good)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema_version"] == 1 and len(doc["tabs"][0]["parts"]) == 3
    out = tmp_path / "x.json"
    assert main(["export", str(good), "-o", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == doc
    assert main(["export", str(good), "-o", str(out)]) == 2  # never overwrites


def test_roundtrip(good: Path, capsys: Capture) -> None:
    assert main(["roundtrip", str(good)]) == 0
    assert "byte-exact" in capsys.readouterr().out


def _templates(tmp_path: Path) -> Path:
    d = tmp_path / "tpl"
    d.mkdir()
    for name, vend in (("a", "Xilinx"), ("b", "Intel")):
        (d / f"{name}.ltp3t").write_bytes(
            f"<Vers> 3.3.4{CRLF}<Vend> {vend}{CRLF}<Dvce> dev {name}{CRLF}"
            f"<Tabs> 1, #FFFFFFFF, Main{CRLF}<Summ> 0, 0, 12, #FF000000, #FFFFFFFF{CRLF}".encode()
        )
    return d


def test_templates_filters(tmp_path: Path, capsys: Capture) -> None:
    d = _templates(tmp_path)
    assert main(["templates", "--dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "a.ltp3t" in out and "b.ltp3t" in out
    assert main(["templates", "--dir", str(d), "--vendor", "xilinx", "--json"]) == 0
    assert [i["file"] for i in json.loads(capsys.readouterr().out)] == ["a.ltp3t"]
    assert main(["templates", "--dir", str(d), "--search", "dev b"]) == 0
    assert "b.ltp3t" in capsys.readouterr().out


def test_templates_missing_dir_exit_2(tmp_path: Path) -> None:
    assert main(["templates", "--dir", str(tmp_path / "none")]) == 2


def test_module_entry_point(good: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "ltp3", "validate", str(good)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "-m", "ltp3", "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "solve" in r.stdout
