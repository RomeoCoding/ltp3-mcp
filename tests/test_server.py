"""P7: every tool is exercised through a real MCP client session (in-memory transport), plus one
stdio subprocess smoke test. Writes only ever go to tmp_path (the single allowed root)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client

from ltp3.server import build_server

TOOLS = [
    "list_templates", "open_tree", "new_tree", "close_tree", "describe_tree", "list_parts",
    "get_part", "list_nets", "solve", "validate", "set_property", "rename_part", "add_part",
    "remove_part", "connect", "disconnect", "move_part", "undo", "preview_changes",
    "compare_trees", "export_json", "save_tree",
]  # fmt: skip
A7 = {"pin_w": 16.67, "pout_w": 14.7, "ploss_w": 1.97, "efficiency_pct": 88.16}


@pytest.fixture(autouse=True)
def allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))
    return tmp_path


@dataclass
class Res:
    is_error: bool
    data: dict[str, Any]
    text: str

    @property
    def code(self) -> str:
        return str(self.data.get("code"))


class Tools:
    def __init__(self, client: Client) -> None:
        self.client = client

    async def __call__(self, tool: str, /, **args: Any) -> Res:
        r = await self.client.call_tool(tool, args)
        text = "".join(getattr(c, "text", "") for c in r.content)
        if r.is_error:
            return Res(True, json.loads(text), text)
        return Res(False, r.structured_content or {}, text)

    async def ok(self, tool: str, /, **args: Any) -> dict[str, Any]:
        r = await self(tool, **args)
        assert not r.is_error, r.text
        return r.data


def run(scenario: Callable[[Tools], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with Client(build_server()) as client:
            return await scenario(Tools(client))

    return asyncio.run(main())


@pytest.fixture
def sample(sample_path: Path, tmp_path: Path) -> Path:
    """A private copy, so STALE_FILE and overwrite tests never touch samples/."""
    p = tmp_path / "artix7.ltp3"
    p.write_bytes(sample_path.read_bytes())
    return p


# ------------------------------------------------------------------ registry


def test_tool_names_are_exactly_the_plan_list() -> None:
    async def go(t: Tools) -> list[Any]:
        return list((await t.client.list_tools()).tools)

    tools = run(go)
    assert sorted(x.name for x in tools) == sorted(TOOLS)
    for tool in tools:
        assert len(tool.description or "") > 60, tool.name
        schema = tool.input_schema
        assert schema["type"] == "object"
        for prop, spec in schema.get("properties", {}).items():
            assert any(k in spec for k in ("type", "anyOf", "$ref", "enum")), (tool.name, prop)
    by = {x.name: x.description or "" for x in tools}
    # LLM-facing text must carry units and the assumptions caveat
    assert "watts" in by["solve"].lower() and "amperes" in by["solve"].lower()
    assert "assumption" in by["solve"].lower()
    assert "volts" in by["add_part"].lower()
    assert "overwrite" in by["save_tree"].lower()


# ------------------------------------------------------------------ catalogue and open


def test_list_templates(template_dir: Path) -> None:
    async def go(t: Tools) -> None:
        r = await t.ok("list_templates", query="artix", limit=3)
        assert r["total"] >= 1 and len(r["items"]) <= 3
        item = r["items"][0]
        for k in ("name", "path", "version", "group", "vendor", "tabs", "input_rails"):
            assert k in item
        assert (await t.ok("list_templates", vendor="Xilinx"))["total"] == 7

    run(go)


def test_list_templates_missing_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LTP3_TEMPLATE_DIR", str(tmp_path / "nope"))

    async def go(t: Tools) -> None:
        r = await t("list_templates")
        assert r.is_error and r.code == "NOT_FOUND"
        assert "LTP3_TEMPLATE_DIR" in r.data["message"] + r.data["hint"]

    run(go)


def test_open_tree_and_errors(sample: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.ltp3"
    bad.write_bytes(b"<Vers> 3.6.8\r\n<Tabs> 1, #FFFFFACD, T\r\n<Term> Input, 1\r\n")

    async def go(t: Tools) -> None:
        r = await t.ok("open_tree", path=str(sample))
        assert r["tree_id"] and r["is_template"] is False and len(r["sha256"]) == 64
        assert r["version"] == "3.6.8"
        assert r["tabs"][0]["parts"] == 4
        assert (await t("open_tree", path=str(tmp_path / "none.ltp3"))).code == "NOT_FOUND"
        assert (await t("open_tree", path=str(tmp_path))).code == "BAD_PATH"
        assert (await t("open_tree", path=str(bad))).code == "PARSE_ERROR"
        assert (await t("describe_tree", tree_id="nope")).code == "UNKNOWN_TREE"

    run(go)


def test_open_template_is_editable_but_not_saveable(template_paths: list[Path]) -> None:
    tpl = template_paths[0]

    async def go(t: Tools) -> None:
        r = await t.ok("open_tree", path=str(tpl))
        assert r["is_template"] is True
        tid = r["tree_id"]
        d = await t.ok("describe_tree", tree_id=tid)
        assert d["dirty"] is False
        # saving back over the template path is refused by the path policy
        s = await t("save_tree", tree_id=tid, path=str(tpl), overwrite=True)
        assert s.is_error and s.code in ("BAD_PATH", "PATH_NOT_ALLOWED")

    run(go)


def test_new_tree_skeleton_save_and_reopen(tmp_path: Path) -> None:
    async def go(t: Tools) -> None:
        r = await t.ok("new_tree", tab_name="Scratch")
        assert "EXPERIMENTAL_SKELETON" in r["warnings"]
        tid = r["tree_id"]
        assert r["tabs"][0]["name"] == "Scratch"
        await t.ok("add_part", tree_id=tid, type="Power", name="VIN", outputs=[{"vout": 5}])
        assert (await t.ok("validate", tree_id=tid))["ok"] is True
        out = tmp_path / "fresh.ltp3"
        await t.ok("save_tree", tree_id=tid, path=str(out))
        again = await t.ok("open_tree", path=str(out))
        assert again["tabs"][0]["parts"] == 1

    run(go)


def test_close_tree(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        await t.ok("close_tree", tree_id=tid)  # clean tree closes
        assert (await t("close_tree", tree_id=tid)).code == "UNKNOWN_TREE"
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        await t.ok("rename_part", tree_id=tid, part_id=2, name="X")
        guard = await t("close_tree", tree_id=tid)
        assert guard.code == "VALIDATION_FAILED" and "unsaved" in guard.data["message"].lower()
        await t.ok("close_tree", tree_id=tid, discard=True)

    run(go)


# ------------------------------------------------------------------ read tools


def test_read_tools(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        d = await t.ok("describe_tree", tree_id=tid)
        assert d["tabs"][0]["counts_by_type"]["Converter"] == 2
        assert d["header"]["Vers"].strip() == "3.6.8"
        parts = (await t.ok("list_parts", tree_id=tid, type="Converter"))["parts"]
        assert [p["id"] for p in parts] == [2, 16]
        term = next(x for x in parts[0]["terminals"] if x["ref"] == "2:Input:1")
        assert "1:Output:1" in term["connected_to"] and term["net"] is not None
        got = await t.ok("get_part", tree_id=tid, part_name="FPGA")
        assert got["part"]["id"] == 11 and 2 in got["upstream"] and got["downstream"] == []
        nets = (await t.ok("list_nets", tree_id=tid))["nets"]
        assert any(n["voltage"] == 5.0 and n["drivers"] == ["1:Output:1"] for n in nets)
        assert (await t("get_part", tree_id=tid, part_id=999)).code == "UNKNOWN_PART"
        assert (await t("list_parts", tree_id=tid, tab=7)).code == "UNKNOWN_TAB"
        assert (await t("list_parts", tree_id=tid, tab="New Tab 1")).data["parts"]
        await t.ok("rename_part", tree_id=tid, part_id=16, name="LTC3633 BUCK")
        amb = await t("get_part", tree_id=tid, part_name="LTC3633 BUCK")
        assert amb.code == "AMBIGUOUS_PART" and "2" in amb.data["hint"]

    run(go)


def test_solve_artix7_oracle(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        r = await t.ok("solve", tree_id=tid)
        assert r["complete"] is True
        for k, v in A7.items():
            assert r["display"][k] == pytest.approx(v), k
        assert [a["code"] for a in r["assumptions"]] == ["A-CURRENT-PEAK-ONLY"]
        nom = await t.ok("solve", tree_id=tid, current_mode="nominal")
        assert nom["current_mode"] == "nominal"
        assert (await t("solve", tree_id=tid, current_mode="bogus")).is_error

    run(go)


def test_validate_reports_errors(sample: Path, tmp_path: Path) -> None:
    broken = tmp_path / "broken.ltp3"
    broken.write_bytes(sample.read_bytes().replace(b"<BPrt> Load, 11,", b"<BPrt> Load, 2,"))

    async def go(t: Tools) -> None:
        good = await t.ok(
            "validate", tree_id=(await t.ok("open_tree", path=str(sample)))["tree_id"]
        )
        assert good["ok"] is True and good["errors"] == []
        bad_id = (await t.ok("open_tree", path=str(broken)))["tree_id"]
        bad = await t.ok("validate", tree_id=bad_id)
        assert bad["ok"] is False and any(e["code"] == "DUP_PART_ID" for e in bad["errors"])
        s = await t("save_tree", tree_id=bad_id, path=str(tmp_path / "x.ltp3"))
        assert s.code == "VALIDATION_FAILED"
        assert not (tmp_path / "x.ltp3").exists()

    run(go)


# ------------------------------------------------------------------ edit tools


def test_set_property_rename_undo_preview(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        r = await t.ok("set_property", tree_id=tid, terminal="1:Output:1", key="Vout", value=3.3)
        assert r["old"] == "5" and r["new"] == "3.3" and r["ok"] is True
        assert r["changed_lines"] == {"added": 0, "removed": 0, "modified": 1}
        p = await t.ok("preview_changes", tree_id=tid)
        assert p["dirty"] is True and "+\tVout    : 3.3" in p["diff"].replace("\r", "")
        assert p["stats"]["modified"] == 1
        rn = await t.ok("rename_part", tree_id=tid, part_id=11, name="Zynq")
        assert rn["old"] == "FPGA" and rn["new"] == "Zynq"
        assert (await t.ok("undo", tree_id=tid))["remaining"] == 1
        assert (await t.ok("undo", tree_id=tid))["remaining"] == 0
        assert (await t.ok("preview_changes", tree_id=tid))["dirty"] is False
        assert (await t("undo", tree_id=tid)).code == "NOTHING_TO_UNDO"
        gone = await t.ok(
            "set_property", tree_id=tid, terminal="1:Output:1", key="Vout", value=None
        )
        assert gone["new"] is None
        assert (await t("set_property", tree_id=tid, part_id=1, key="Nope", value=1)).code == (
            "UNKNOWN_KEY"
        )
        bad = await t("set_property", tree_id=tid, part_id=2, key="PartNumb", value="a\nb")
        assert bad.code == "BAD_VALUE"
        # a failed edit leaves nothing on the undo stack
        assert (await t.ok("undo", tree_id=tid))["remaining"] == 0

    run(go)


def test_read_only_tree_refuses_edits(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample), read_only=True))["tree_id"]
        for name, args in (
            ("rename_part", {"part_id": 2, "name": "z"}),
            ("set_property", {"part_id": 2, "key": "Color", "value": "#FF000000"}),
            ("add_part", {"type": "Load", "name": "L"}),
            ("remove_part", {"part_id": 11}),
            ("connect", {"source": "1:Output:1", "sink": "2:Input:1"}),
            ("disconnect", {"terminal": "2:Input:1"}),
            ("move_part", {"part_id": 2, "x": 100, "y": 100}),
            ("undo", {}),
        ):
            assert (await t(name, tree_id=tid, **args)).code == "READ_ONLY", name
        assert (await t.ok("solve", tree_id=tid))["complete"] is True

    run(go)


def test_structure_edits(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        ldo = (
            await t.ok(
                "add_part", tree_id=tid, type="LDO", name="LDO A", part_number="LT3045",
                outputs=[{"vout": 1.8}], near_id=16,
            )
        )["part"]  # fmt: skip
        assert ldo["type"] == "LDO" and ldo["terminals"][1]["ref"].endswith(":Output:1")
        load = (
            await t.ok(
                "add_part", tree_id=tid, type="Load", name="Sensor",
                inputs=[{"ipeak": 500, "unit_exp": -3}],
            )
        )["part"]  # fmt: skip
        c = await t.ok("connect", tree_id=tid, source="16:Output:1", sink=f"{ldo['id']}:Input:1")
        assert c["wires_added"] and c["ok"] is True and c["changed_lines"]["added"] > 0
        await t.ok(
            "connect", tree_id=tid, source=f"{ldo['id']}:Output:1", sink=f"{load['id']}:Input:1"
        )
        again = await t("connect", tree_id=tid, source="2:Output:1", sink=f"{ldo['id']}:Input:1")
        assert again.code == "TERMINAL_IN_USE"
        assert (await t("connect", tree_id=tid, source="2:Input:1", sink="11:Input:1")).is_error
        assert (await t("connect", tree_id=tid, source="99:Output:1", sink="11:Input:1")).code == (
            "UNKNOWN_TERMINAL"
        )
        mv = await t.ok("move_part", tree_id=tid, part_id=load["id"], x=1203, y=700)
        assert "rerouted_wires" in mv and mv["warnings"]  # 1203 is off the 5-unit grid
        dis = await t.ok("disconnect", tree_id=tid, terminal=f"{load['id']}:Input:1")
        assert dis["removed_wires"]
        rem = await t.ok("remove_part", tree_id=tid, part_id=load["id"])
        assert "removed_wires" in rem and "merged_junctions" in rem
        assert (await t("remove_part", tree_id=tid, part_id=load["id"])).code == "UNKNOWN_PART"
        bad = await t("add_part", tree_id=tid, type="Net", name="n")
        assert bad.code == "BAD_VALUE"
        assert (await t.ok("validate", tree_id=tid))["ok"] is True

    run(go)


def test_connect_cycle(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        a = (await t.ok("add_part", tree_id=tid, type="LDO", name="A"))["part"]["id"]
        b = (await t.ok("add_part", tree_id=tid, type="LDO", name="B"))["part"]["id"]
        await t.ok("connect", tree_id=tid, source=f"{a}:Output:1", sink=f"{b}:Input:1")
        r = await t("connect", tree_id=tid, source=f"{b}:Output:1", sink=f"{a}:Input:1")
        assert r.code == "CYCLE"

    run(go)


# ------------------------------------------------------------------ compare and export


def test_compare_and_export(sample: Path) -> None:
    async def go(t: Tools) -> None:
        a = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        b = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        same = await t.ok("compare_trees", a=a, b=b)
        assert same["count"] == 0
        await t.ok("set_property", tree_id=b, terminal="2:Output:1", key="NomEff", value=85)
        diff = await t.ok("compare_trees", a=a, b=b)
        assert any(c["key"] == "NomEff" for c in diff["changes"])
        kinds = {c["kind"] for c in diff["changes"]}
        assert "result" in kinds  # include_solve defaults to true
        assert diff["solve_delta"]["pin_w"] < 0  # higher efficiency draws less
        no_solve = await t.ok("compare_trees", a=a, b=b, include_solve=False)
        assert "result" not in {c["kind"] for c in no_solve["changes"]}
        assert (await t("compare_trees", a=a, b="zzz")).code == "UNKNOWN_TREE"
        ex = await t.ok("export_json", tree_id=a)
        assert ex["schema"] == "ltp3-export" and ex["tabs"][0]["parts"]
        assert any("results" in p for p in ex["tabs"][0]["parts"])
        assert (await t("export_json", tree_id=a, tab=3)).code == "UNKNOWN_TAB"

    run(go)


# ------------------------------------------------------------------ save


def test_save_policy(sample: Path, tmp_path: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        await t.ok("rename_part", tree_id=tid, part_id=11, name="Zynq")
        # default target is a NEW path next to the opened file; the opened file is untouched
        before = sample.read_bytes()
        r = await t.ok("save_tree", tree_id=tid)
        assert Path(r["path"]) != sample and Path(r["path"]).exists()
        assert sample.read_bytes() == before
        assert r["backup_path"] is None and r["validation"]["ok"] is True
        assert r["changed_lines"]["modified"] == 1 and len(r["sha256"]) == 64
        # saved: the tree is clean again
        assert (await t.ok("preview_changes", tree_id=tid))["dirty"] is False
        dup = await t("save_tree", tree_id=tid, path=r["path"])
        assert dup.code == "FILE_EXISTS"
        assert not list(tmp_path.glob("*.tmp-*"))
        assert (await t("save_tree", tree_id=tid, path=str(tmp_path / "x.txt"))).code == "BAD_PATH"
        outside = tmp_path.parent / "elsewhere.ltp3"
        assert (await t("save_tree", tree_id=tid, path=str(outside))).code == "PATH_NOT_ALLOWED"
        assert not outside.exists()
        adi = r"C:\Program Files (x86)\LTC\LTpowerCAD2\out.ltp3"
        assert (await t("save_tree", tree_id=tid, path=adi)).code == "PATH_NOT_ALLOWED"
        # overwrite the opened file: explicit flag, backup kept
        await t.ok("rename_part", tree_id=tid, part_id=11, name="Zynq2")
        ow = await t.ok("save_tree", tree_id=tid, path=str(sample), overwrite=True)
        assert ow["backup_path"] and Path(ow["backup_path"]).read_bytes() == before
        assert b"Zynq2" in sample.read_bytes()

    run(go)


def test_save_stale_file(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        await t.ok("rename_part", tree_id=tid, part_id=11, name="Mine")
        sample.write_bytes(sample.read_bytes() + b"\r\n")  # the GUI saved in between
        r = await t("save_tree", tree_id=tid, path=str(sample), overwrite=True)
        assert r.code == "STALE_FILE"

    run(go)


# ------------------------------------------------------------------ errors never leak


def test_errors_are_structured_and_never_a_traceback(
    sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ltp3.server as server

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(server, "build_graph", boom)

    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        r = await t("solve", tree_id=tid)
        assert r.is_error and r.code == "INTERNAL_ERROR"
        assert "Traceback" not in r.text and "secret internal detail" not in r.text
        # argument validation errors are structured too
        v = await t("solve")  # missing tree_id
        assert v.is_error and v.code == "BAD_ARGUMENTS" and "tree_id" in v.data["message"]
        u = await t("no_such_tool")
        assert u.is_error and u.code == "UNKNOWN_TOOL"

    run(go)


def test_digit_names_are_names_not_ids(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        await t.ok("rename_part", tree_id=tid, part_id=11, name="2")
        assert (await t.ok("get_part", tree_id=tid, part_name="2"))["part"]["id"] == 11
        assert (await t.ok("get_part", tree_id=tid, part_id=2))["part"]["type"] == "Converter"
        both = await t("get_part", tree_id=tid, part_id=2, part_name="2")
        assert both.code == "BAD_VALUE"
        assert (await t("get_part", tree_id=tid)).code == "BAD_VALUE"

    run(go)


def test_open_unreadable_inputs_are_structured(tmp_path: Path) -> None:
    empty = tmp_path / "empty.ltp3"
    empty.write_bytes(b"")

    async def go(t: Tools) -> None:
        r = await t("open_tree", path=str(empty))
        # an empty file either opens as a tree with no tabs or fails with a code, never a crash
        assert r.is_error is False or r.code in ("PARSE_ERROR", "BAD_PATH")
        if not r.is_error:
            assert r.data["tabs"] == []

    run(go)


def test_assertion_errors_become_structured(sample: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ltp3.editor import Editor

    def bad(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("internal invariant")

    monkeypatch.setattr(Editor, "remove_part", bad)

    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        r = await t("remove_part", tree_id=tid, part_id=11)
        assert r.code == "INTERNAL_ERROR" and "invariant" not in r.text

    run(go)


def test_second_overwrite_is_not_stale(sample: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        for n in ("A", "B"):
            await t.ok("rename_part", tree_id=tid, part_id=11, name=n)
            await t.ok("save_tree", tree_id=tid, path=str(sample), overwrite=True)
        assert b"<BPrt> Load, 11," in sample.read_bytes() and b", B" in sample.read_bytes()

    run(go)


# ------------------------------------------------------------------ end to end


def test_end_to_end_artix7(sample: Path, tmp_path: Path) -> None:
    async def go(t: Tools) -> None:
        tid = (await t.ok("open_tree", path=str(sample)))["tree_id"]
        base = await t.ok("solve", tree_id=tid)
        for k, v in A7.items():
            assert base["display"][k] == pytest.approx(v)

        ldo = (
            await t.ok("add_part", tree_id=tid, type="LDO", name="LDO 1V8", outputs=[{"vout": 1.8}])
        )["part"]["id"]
        load = (
            await t.ok("add_part", tree_id=tid, type="Load", name="Sensor", inputs=[{"ipeak": 0.5}])
        )["part"]["id"]
        await t.ok("connect", tree_id=tid, source="16:Output:1", sink=f"{ldo}:Input:1")
        await t.ok("connect", tree_id=tid, source=f"{ldo}:Output:1", sink=f"{load}:Input:1")
        v = await t.ok("validate", tree_id=tid)
        assert v["ok"] is True
        out = tmp_path / "artix7_plus_ldo.ltp3"
        s = await t.ok("save_tree", tree_id=tid, path=str(out))
        assert Path(s["path"]) == out.resolve()

        edited = await t.ok("solve", tree_id=tid)
        again_id = (await t.ok("open_tree", path=str(out)))["tree_id"]
        again = await t.ok("solve", tree_id=again_id)
        assert again["complete"] is True
        assert again["totals"] == edited["totals"] and again["display"] == edited["display"]
        # the new 0.5 A load at 1.8 V adds exactly 0.9 W of output power
        assert again["totals"]["pout_w"] - base["totals"]["pout_w"] == pytest.approx(0.9)
        assert again["totals"]["pin_w"] > base["totals"]["pin_w"] + 0.9
        assert len(again["loads"]) == len(base["loads"]) + 1

    run(go)


# ------------------------------------------------------------------ process level


def test_entry_point_help() -> None:
    p = subprocess.run(
        [sys.executable, "-m", "ltp3.server", "--help"], capture_output=True, text=True, timeout=60
    )
    assert p.returncode == 0 and "ltp3-mcp" in p.stdout


def test_stdio_smoke() -> None:
    """initialize + tools/list over real JSON-RPC on a subprocess's stdin/stdout."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "ltp3.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv() -> dict[str, Any]:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line, "server closed stdout"
        return json.loads(line)  # type: ignore[no-any-return]

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        })  # fmt: skip
        init = recv()
        assert init["id"] == 1 and init["result"]["serverInfo"]["name"] == "ltp3-mcp"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listing = recv()
        assert sorted(x["name"] for x in listing["result"]["tools"]) == sorted(TOOLS)
    finally:
        proc.terminate()
        proc.wait(timeout=30)
