"""MCP server for LTpowerPlanner III power trees (PLAN.md section 4).

Trees live in memory under a `tree_id`; only `save_tree` writes to disk. Every failure reaches the
client as a tool error whose text is JSON `{"code", "message", "hint"}`, never a stack trace.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import json
import logging
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, ParamSpec

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from . import __version__, catalog, paths, safeio
from .compare import compare_trees as diff_trees
from .editor import changed_lines
from .errors import (
    AMBIGUOUS_PART,
    BAD_VALUE,
    UNKNOWN_PART,
    VALIDATION_FAILED,
    Ltp3Error,
)
from .export import Results, export_tree
from .graph import TabGraph, build_graph
from .model import Part, Tab, Tree
from .numfmt import parse as parse_number
from .session import Registry, Session
from .solver import CurrentMode, SolveOptions, SolveResult
from .solver import solve as run_solve
from .validate import Report

log = logging.getLogger("ltp3")
P = ParamSpec("P")

TabRef = str | int | None


# ---------------------------------------------------------------------------- error mapping


def _error_result(code: str, message: str, hint: str = "") -> CallToolResult:
    body = json.dumps({"code": code, "message": message, "hint": hint})
    return CallToolResult(content=[TextContent(type="text", text=body)], is_error=True)


def _guarded(reg: Registry) -> Callable[[Callable[P, dict[str, Any]]], Callable[P, dict[str, Any]]]:
    """Wrap a tool: serialise access to the registry and turn every exception into a structured
    error. Internal errors are logged to stderr (stdout carries the protocol) and reported
    without detail, so no path or stack trace reaches the model."""

    def deco(fn: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
            try:
                with reg.lock:
                    return fn(*args, **kwargs)
            except Ltp3Error as exc:
                return _error_result(exc.code, exc.message, exc.hint)  # type: ignore[return-value]
            except Exception:
                log.exception("tool %s crashed", fn.__name__)
                return _error_result(  # type: ignore[return-value]
                    "INTERNAL_ERROR",
                    f"{fn.__name__} failed unexpectedly; the tree is unchanged or was restored",
                    "Run validate, and report this with the steps that led to it.",
                )

        return wrapper

    return deco


class _Server(MCPServer[None]):
    """Adds structured errors for failures that happen before a tool body runs."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                fields = sorted({".".join(str(p) for p in e["loc"]) for e in cause.errors()})
                return _error_result(
                    "BAD_ARGUMENTS",
                    f"invalid arguments for {name}: {', '.join(fields)}",
                    "Check the tool's input schema: names, types and allowed values.",
                )
            if "Unknown tool" in str(exc):
                return _error_result("UNKNOWN_TOOL", f"no tool named {name!r}")
            log.error("tool %s: %s", name, exc)
            return _error_result("INTERNAL_ERROR", f"{name} failed unexpectedly")


# ---------------------------------------------------------------------------- serialisers


def _parts_or_tabs(tree: Tree) -> list[dict[str, Any]]:
    return [
        {"index": i, "id": t.id, "name": t.name, "parts": len(t.parts), "wires": len(t.wires)}
        for i, t in enumerate(tree.tabs)
    ]


def _version(tree: Tree) -> str:
    for b in tree.header:
        if b.head.tag == "Vers":
            return b.head.args.strip()
    return ""


def _pick(tab: Tab, part_id: int | None, part_name: str | None) -> int:
    """Resolve a part to its id. Ids and names are separate arguments because a name made only
    of digits would otherwise be read as an id by the model lookup."""
    if (part_id is None) == (part_name is None):
        raise Ltp3Error(BAD_VALUE, "give exactly one of part_id and part_name")
    if part_id is not None:
        return part_id
    hits = [p.id for p in tab.parts if p.name == part_name]
    if not hits:
        raise Ltp3Error(UNKNOWN_PART, f"no part named {part_name!r} in tab {tab.name!r}")
    if len(hits) > 1:
        raise Ltp3Error(
            AMBIGUOUS_PART,
            f"{len(hits)} parts are named {part_name!r}",
            "Address the part by part_id: " + ", ".join(map(str, hits)),
        )
    return hits[0]


def _tree_info(s: Session, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "tree_id": s.tree_id,
        "path": str(s.path) if s.path else None,
        "version": _version(s.tree),
        "is_template": s.is_template,
        "read_only": s.read_only,
        "sha256": s.opened_sha256,
        "tabs": _parts_or_tabs(s.tree),
        "warnings": warnings or [],
    }


def _part_dict(part: Part, graph: TabGraph) -> dict[str, Any]:
    terms: list[dict[str, Any]] = []
    for t in part.terminals:
        net = graph.net_of(t)
        terms.append(
            {
                "ref": t.ref,
                "kind": t.kind,
                "n": t.n,
                "properties": dict(t.props),
                "net": net.index if net else None,
                "connected_to": [o.ref for o in net.terminals if o is not t] if net else [],
            }
        )
    return {
        "id": part.id,
        "type": part.type,
        "name": part.name,
        "part_number": part.props.get("PartNumb"),
        "x": part.x,
        "y": part.y,
        "properties": dict(part.props),
        "terminals": terms,
    }


def _issues(report: Report) -> dict[str, Any]:
    def one(i: Any) -> dict[str, Any]:
        return {"code": i.code, "message": i.message, "tab": i.tab}

    return {
        "ok": report.ok,
        "errors": [one(i) for i in report.errors],
        "warnings": [one(i) for i in report.warnings],
    }


def _solve_tab(s: Session, ti: int, mode: CurrentMode) -> SolveResult:
    graph = build_graph(s.tree.tabs[ti])
    return run_solve(graph, SolveOptions(current_mode=mode))


def _results(tree: Tree, mode: CurrentMode = "auto") -> Results:
    """Solved numbers keyed `(tab_index, part_id)` for export/compare. A tab that cannot be
    solved (CYCLE) or a part with no number simply has no entry."""
    out: dict[tuple[int, int], dict[str, float]] = {}
    for ti, tab in enumerate(tree.tabs):
        try:
            res = run_solve(build_graph(tab), SolveOptions(current_mode=mode))
        except Ltp3Error:
            continue
        for p in res.parts:
            metrics = {
                k: v
                for k, v in (("pin_w", p.pin_w), ("pout_w", p.pout_w), ("ploss_w", p.ploss_w))
                if v is not None
            }
            if metrics:
                out[(ti, p.id)] = metrics
    return out


def _totals(tree: Tree, tab_index: int | None) -> dict[str, float]:
    """Sum of complete tabs' totals (or one tab), for compare's solve_delta."""
    sums = {"pin_w": 0.0, "pout_w": 0.0, "ploss_w": 0.0}
    for ti, tab in enumerate(tree.tabs):
        if tab_index is not None and ti != tab_index:
            continue
        try:
            res = run_solve(build_graph(tab))
        except Ltp3Error:
            continue
        sums["pin_w"] += res.totals.pin_w
        sums["pout_w"] += res.totals.pout_w
        sums["ploss_w"] += res.totals.ploss_w or 0.0
    return sums


def _edit_reply(
    extra: dict[str, Any], lines: dict[str, int], warnings: list[str] | None = None
) -> dict[str, Any]:
    return {"ok": True, **extra, "changed_lines": lines, "warnings": warnings or []}


# ---------------------------------------------------------------------------- the server


def build_server() -> MCPServer[None]:
    reg = Registry()
    server = _Server(
        "ltp3-mcp",
        version=__version__,
        instructions=(
            "Read, analyse and edit LTpowerPlanner III power trees (.ltp3 projects, .ltp3t "
            "templates). Open a file with open_tree (or start with list_templates), inspect it "
            "with list_parts/list_nets, compute power with solve, edit the in-memory tree, then "
            "validate and save_tree to a NEW path. Units: volts, amperes, watts, percent. "
            "Nothing is written to disk until save_tree."
        ),
    )
    guard = _guarded(reg)

    def tool(**kw: Any) -> Callable[[Callable[P, dict[str, Any]]], Callable[P, dict[str, Any]]]:
        def deco(fn: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
            wrapped = guard(fn)
            server.tool(**kw)(wrapped)
            return wrapped

        return deco

    # ------------------------------------------------------------------ catalogue / open / close

    @tool()
    def list_templates(
        query: str | None = None,
        vendor: str | None = None,
        group: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List the factory power-tree templates (.ltp3t) installed with LTpowerPlanner III,
        read in place and never copied. Use this to find a starting point for a design.
        `query` is a free-text AND search over file name, device, description, rail voltages
        and part numbers; `vendor` and `group` match exactly (case-insensitive). `limit` caps
        the returned items (default 50) but `total` counts all matches. input_rails and
        output_rails are summaries such as '12V' / '3.3V/2A' (volts / amperes). Pass an item's
        `path` to open_tree. Set LTP3_TEMPLATE_DIR if the templates are installed elsewhere."""
        items = catalog.list_templates(vendor=vendor, group=group, text=query)
        shown = [{**i.to_dict(), "version": i.format_version} for i in items[: max(limit, 0)]]
        return {"template_dir": str(paths.template_dir()), "total": len(items), "items": shown}

    @tool()
    def open_tree(path: str, read_only: bool = False) -> dict[str, Any]:
        """Open a .ltp3 project or .ltp3t template into memory and return a `tree_id` used by
        every other tool. The file on disk is never modified by editing; only save_tree writes.
        Templates may be edited in memory but can never be saved over their own path. With
        read_only=true all editing tools fail with READ_ONLY. Returns the file format version,
        the SHA-256 of the bytes read (used to detect that the GUI changed the file later), and
        the tabs (each tab is one independent power tree) with part and wire counts."""
        s = reg.open(path, read_only)
        return _tree_info(s)

    @tool()
    def new_tree(tab_name: str = "New Tab 1") -> dict[str, Any]:
        """Create an empty in-memory project with one tab. EXPERIMENTAL: the skeleton has not
        yet been confirmed to open in the GUI, so the result carries the warning
        EXPERIMENTAL_SKELETON. Add parts with add_part, wire them with connect, then save_tree
        to a new .ltp3 path. Prefer open_tree on a known-good file when you can."""
        return _tree_info(reg.new(tab_name), ["EXPERIMENTAL_SKELETON"])

    @tool()
    def close_tree(tree_id: str, discard: bool = False) -> dict[str, Any]:
        """Forget an open tree and free its memory. Refuses (VALIDATION_FAILED, 'unsaved
        changes') when the tree has edits that were not saved, unless discard=true."""
        s = reg.get(tree_id)
        if s.dirty and not discard:
            raise Ltp3Error(
                VALIDATION_FAILED,
                f"tree {tree_id} has unsaved changes",
                "Call save_tree first, or pass discard=true to drop them.",
            )
        reg.close(tree_id)
        return {"ok": True}

    # ------------------------------------------------------------------ read tools

    @tool()
    def describe_tree(tree_id: str, tab: TabRef = None) -> dict[str, Any]:
        """Summarise an open tree: header tags of the file, and for each tab the number of
        parts by type (Power, Converter, LDO, Load, Resistor, Capacitor, Net), the number of
        nets and the terminals that are not wired to anything. `dirty` is true when there are
        unsaved edits. Omit `tab` for all tabs; otherwise give a 0-based index or the tab name."""
        s = reg.get(tree_id)
        indices = range(len(s.tree.tabs)) if tab is None else [s.tab_index(tab)]
        tabs = []
        for i in indices:
            t = s.tree.tabs[i]
            g = build_graph(t)
            tabs.append(
                {
                    "index": i,
                    "name": t.name,
                    "counts_by_type": dict(Counter(p.type for p in t.parts)),
                    "nets": len(g.nets),
                    "unconnected_terminals": [x.ref for x in g.unconnected_terminals()],
                }
            )
        return {
            "header": {b.head.tag: b.head.args.strip() for b in s.tree.header},
            "tabs": tabs,
            "dirty": s.dirty,
        }

    @tool()
    def list_parts(tree_id: str, tab: TabRef = None, type: str | None = None) -> dict[str, Any]:
        """List the parts of one tab (default: the first) with their properties and terminals.
        Terminal refs look like '2:Output:1' (part id, Input|Output|Aux, number). For each
        terminal, `net` is a net index and `connected_to` the other terminals on that net.
        Property values are raw file text: volts (Vout), amperes (Ipeak, Inom, IoutMax), percent
        (NomEff), ohms (Resstnce). Filter with `type`, e.g. 'Converter' or 'Load'."""
        s = reg.get(tree_id)
        t = s.tab(tab)
        g = build_graph(t)
        return {"parts": [_part_dict(p, g) for p in t.parts if type is None or p.type == type]}

    @tool()
    def get_part(
        tree_id: str, part_id: int | None = None, part_name: str | None = None, tab: TabRef = None
    ) -> dict[str, Any]:
        """Return one part (give `part_id`, or `part_name` when the name is unique) in the
        list_parts format, plus `upstream` and `downstream`: the ids of parts that feed it and
        parts it feeds. An
        ambiguous name fails with AMBIGUOUS_PART and the hint lists the candidate ids."""
        s = reg.get(tree_id)
        t = s.tab(tab)
        g = build_graph(t)
        p = t.part(_pick(t, part_id, part_name))
        return {
            "part": _part_dict(p, g),
            "upstream": [x.id for x in g.upstream(p)],
            "downstream": [x.id for x in g.downstream(p)],
        }

    @tool()
    def list_nets(tree_id: str, tab: TabRef = None) -> dict[str, Any]:
        """List the electrical nets (rails) of a tab. Each net has its `drivers` (Output
        terminals), `consumers` (Input terminals), `passives` (Aux terminals, e.g. capacitors),
        the wire ids, the labels of Net parts on it, and `voltage` in volts when a driver states
        Vout (null otherwise; use solve for computed voltages)."""
        s = reg.get(tree_id)
        g = build_graph(s.tab(tab))
        nets = []
        for n in g.nets:
            drivers = g.drivers(n)
            volts: float | None = None
            for d in drivers:
                v = d.props.get("Vout")
                if v is not None:
                    try:
                        volts = parse_number(v)
                    except Ltp3Error:
                        volts = None
                    break
            nets.append(
                {
                    "net_id": n.index,
                    "voltage": volts,
                    "drivers": [x.ref for x in drivers],
                    "consumers": [x.ref for x in g.consumers(n)],
                    "passives": [x.ref for x in g.passives(n)],
                    "wire_ids": [w.id for w in n.wires],
                    "labels": list(n.labels),
                }
            )
        return {"nets": nets}

    @tool()
    def solve(
        tree_id: str,
        tab: TabRef = None,
        current_mode: Literal["auto", "peak", "nominal"] = "auto",
    ) -> dict[str, Any]:
        """Compute the power budget of one tab the way the app's Calculate does. Returns totals
        in watts (pin_w, pout_w, ploss_w) and percent (efficiency_pct) at full precision, and
        `display` rounded to 2 decimals as the app shows them, plus per-part voltages (volts),
        currents (amperes), power (watts) and each load's v/i/p. `current_mode` chooses the
        load current: 'auto' follows each load's own radio (peak unless it selects nominal),
        'peak' or 'nominal' force one. Always read `assumptions` (rules that are computed but
        not fully confirmed, and keys that were read as GUI defaults) and `blocked`: when
        `complete` is false some parts are unsupported and totals are partial. It never raises
        for unsupported constructs, only CYCLE for a loop."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)
        out = _solve_tab(s, ti, current_mode).to_dict()
        return {"tab": ti, "tab_name": s.tree.tabs[ti].name, **out}

    @tool()
    def validate(tree_id: str) -> dict[str, Any]:
        """Check the whole tree before saving: file structure, wire/terminal consistency,
        numeric values and electrical sanity (e.g. an output voltage outside a converter's
        range). `errors` block save_tree; `warnings` (such as a load current above IoutMax)
        do not. Each issue has a code, message and tab index."""
        s = reg.get(tree_id)
        from .validate import validate as run_validate

        return _issues(run_validate(s.tree))

    # ------------------------------------------------------------------ edit tools

    @tool()
    def set_property(
        tree_id: str,
        key: str,
        value: str | float | None,
        part_id: int | None = None,
        part_name: str | None = None,
        terminal: str | None = None,
        tab: TabRef = None,
        allow_unknown_key: bool = False,
    ) -> dict[str, Any]:
        """Set, add or remove one property line of a part or terminal. The target is a part
        (`part_id`, or a unique `part_name`) or a terminal (`terminal`, e.g. '2:Output:1');
        give one. Units follow the key: Vout/VoutMin/VoutMax in
        volts, Ipeak/Inom/IoutMax in amperes (scaled by IpeakU/InomU, a power-of-ten exponent),
        NomEff in percent, Resstnce in ohms. `value=null` removes the line, which restores the
        app's default for that key. Keys never seen for that part type fail with UNKNOWN_KEY
        unless allow_unknown_key=true. Returns the stored old and new text."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)
        if terminal is not None:
            if part_id is not None or part_name is not None:
                raise Ltp3Error(BAD_VALUE, "give a terminal or a part, not both")
            target = terminal
        else:
            target = str(_pick(s.tree.tabs[ti], part_id, part_name))
        (old, new), lines = s.edit(
            lambda e: e.set_property(target, key, value, ti, allow_unknown_key)
        )
        return _edit_reply({"old": old, "new": new}, lines)

    @tool()
    def rename_part(
        tree_id: str,
        name: str,
        part_id: int | None = None,
        part_name: str | None = None,
        tab: TabRef = None,
    ) -> dict[str, Any]:
        """Change the display name of a part (`part_id`, or a unique current `part_name`) to
        `name`. Names need not be unique, but use distinct names if you plan to address parts
        by name."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)
        (old, new), lines = s.edit(
            lambda e: e.rename_part(_pick(s.tree.tabs[ti], part_id, part_name), name, ti)
        )
        return _edit_reply({"old": old, "new": new}, lines)

    @tool()
    def add_part(
        tree_id: str,
        # str, not a Literal: an unsupported type (Net) must give BAD_VALUE, not a schema error
        type: str,
        name: str,
        tab: TabRef = None,
        part_number: str | None = None,
        outputs: list[dict[str, float]] | None = None,
        inputs: list[dict[str, float]] | None = None,
        resistance: float | None = None,
        near_id: int | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> dict[str, Any]:
        """Add a part with default geometry and no wires; connect it afterwards. Types: Power
        (a source), Converter (buck/boost; Input 1 plus one Output per entry in `outputs`), LDO,
        Load, Resistor, Capacitor. `outputs` entries (Power, Converter, LDO): {vout} in volts,
        {nom_eff} in percent (Converter only), {iout_max} in amperes. `inputs` entries (Load):
        {ipeak} and/or {inom} in amperes, {unit_exp} a power-of-ten exponent for the stored
        unit (-3 means the values are milliamps). `resistance` in ohms (Resistor).
        `part_number` only for Converter or LDO. Position: `near_id` (another part's id),
        or explicit x/y (snapped to a 5-unit grid); otherwise a free spot. Returns the new part
        with its
        terminal refs. Nets (labels) cannot be created."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)

        def op(e: Any) -> int:
            return int(e.add_part(
                type, name, ti, part_number=part_number, outputs=outputs, inputs=inputs,
                resistance=resistance, near=near_id, x=x, y=y,
            ).id)  # fmt: skip

        pid, lines = s.edit(op)
        t = s.tree.tabs[ti]
        return _edit_reply({"part": _part_dict(t.part(pid), build_graph(t))}, lines)

    @tool()
    def remove_part(
        tree_id: str, part_id: int | None = None, part_name: str | None = None, tab: TabRef = None
    ) -> dict[str, Any]:
        """Delete a part (`part_id`, or a unique `part_name`) and every wire attached to it;
        junctions left with a single wire are merged or removed. Returns the ids of removed
        wires and the number of merged junctions."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)
        pid = _pick(s.tree.tabs[ti], part_id, part_name)
        r, lines = s.edit(lambda e: e.remove_part(pid, ti))
        return _edit_reply(
            {"removed_wires": r.removed_wires, "merged_junctions": r.merged_junctions}, lines
        )

    @tool()
    def connect(tree_id: str, source: str, sink: str, tab: TabRef = None) -> dict[str, Any]:
        """Wire an Output terminal ('<part>:Output:<n>') to an unwired Input terminal
        ('<part>:Input:<n>'). An output may feed several inputs: connecting to an output that is
        already wired branches the existing wire. Fails with TERMINAL_IN_USE if the sink already
        has a wire, CYCLE if the connection would close a loop, UNKNOWN_TERMINAL for a bad
        reference, BAD_VALUE if the directions are wrong."""
        s = reg.get(tree_id)
        r, lines = s.edit(lambda e: e.connect(source, sink, s.tab_index(tab)))
        return _edit_reply(
            {"net_id": r.net_id, "wires_added": r.wires_added, "wires_modified": r.wires_modified},
            lines,
        )

    @tool()
    def disconnect(tree_id: str, terminal: str, tab: TabRef = None) -> dict[str, Any]:
        """Remove every wire attached to one terminal ('<part>:<Input|Output|Aux>:<n>'). The
        rest of the net stays connected where it can."""
        s = reg.get(tree_id)
        r, lines = s.edit(lambda e: e.disconnect(terminal, s.tab_index(tab)))
        return _edit_reply(
            {"removed_wires": r.removed_wires, "merged_junctions": r.merged_junctions}, lines
        )

    @tool()
    def move_part(
        tree_id: str,
        x: int,
        y: int,
        part_id: int | None = None,
        part_name: str | None = None,
        tab: TabRef = None,
    ) -> dict[str, Any]:
        """Move a part (`part_id` or unique `part_name`) on the schematic canvas to (x, y),
        dragging attached wire ends along. This changes layout only, never the electrical
        design. Coordinates snap to a 5-unit grid and
        a warning reports any snapping."""
        s = reg.get(tree_id)
        ti = s.tab_index(tab)
        pid = _pick(s.tree.tabs[ti], part_id, part_name)
        r, lines = s.edit(lambda e: e.move_part(pid, x, y, ti))
        return _edit_reply({"rerouted_wires": r.rerouted_wires}, lines, r.warnings)

    @tool()
    def undo(tree_id: str) -> dict[str, Any]:
        """Revert the last successful edit of this tree. Returns how many edits can still be
        undone. Fails with NOTHING_TO_UNDO when the stack is empty."""
        s = reg.get(tree_id)
        return {"ok": True, "remaining": s.undo()}

    @tool()
    def preview_changes(tree_id: str, max_lines: int = 200) -> dict[str, Any]:
        """Show a unified diff of the in-memory tree against the file as opened (or as last
        saved), so you can see exactly what save_tree would change. `stats` counts added,
        removed and modified lines; `dirty` is false when nothing differs. The diff is cut after
        `max_lines` lines."""
        s = reg.get(tree_id)
        now = s.current_bytes()
        enc = s.editor.doc.encoding
        a = s.opened_bytes.decode(enc, errors="replace").splitlines()
        b = now.decode(enc, errors="replace").splitlines()
        diff = list(difflib.unified_diff(a, b, "opened", "current", lineterm="", n=2))
        if len(diff) > max_lines:
            diff = [*diff[:max_lines], f"... ({len(diff) - max_lines} more lines)"]
        return {
            "dirty": now != s.opened_bytes,
            "diff": "\n".join(diff),
            "stats": changed_lines(s.opened_bytes, now),
        }

    # ------------------------------------------------------------------ compare / export

    @tool()
    def compare_trees(
        a: str,
        b: str,
        tab_a: TabRef = None,
        tab_b: TabRef = None,
        include_layout: bool = False,
        include_solve: bool = True,
    ) -> dict[str, Any]:
        """Structural diff of two open trees (a = before, b = after): parts added, removed or
        changed (by id), tabs added or removed, and connections compared as nets. Layout
        (positions, colours) is hidden unless include_layout=true. With include_solve=true,
        per-part solved numbers (watts) are compared too (kind 'result') and `solve_delta` gives
        b minus a in watts for pin_w, pout_w and ploss_w. Give tab_a/tab_b to restrict the
        comparison to one tab of each."""
        sa, sb = reg.get(a), reg.get(b)
        ia = None if tab_a is None else sa.tab_index(tab_a)
        ib = None if tab_b is None else sb.tab_index(tab_b)
        diff = diff_trees(
            sa.tree,
            sb.tree,
            include_layout=include_layout,
            results_a=_results(sa.tree) if include_solve else None,
            results_b=_results(sb.tree) if include_solve else None,
        )
        out = diff.to_dict()
        if ia is not None or ib is not None:
            names = {sa.tree.tabs[ia].name if ia is not None else None}
            names.add(sb.tree.tabs[ib].name if ib is not None else None)
            names.discard(None)
            out["changes"] = [c for c in out["changes"] if c["tab"] in names]
            out["count"] = len(out["changes"])
        if include_solve:
            ta, tb = _totals(sa.tree, ia), _totals(sb.tree, ib)
            out["solve_delta"] = {k: tb[k] - ta[k] for k in ta}
        return out

    @tool()
    def export_json(tree_id: str, tab: TabRef = None) -> dict[str, Any]:
        """Export the tree as plain JSON (schema 'ltp3-export'): header, and per tab the parts
        with their properties and terminals, and the nets as lists of terminal refs. Parts
        carry a `results` object with solved pin_w/pout_w/ploss_w (watts) when available. Omit
        `tab` for all tabs. Layout is excluded; use it to feed other tools or reports."""
        s = reg.get(tree_id)
        doc = export_tree(s.tree, include_layout=False, results=_results(s.tree))
        if tab is not None:
            ti = s.tab_index(tab)
            doc["tabs"] = [t for t in doc["tabs"] if t["index"] == ti]
        return doc

    # ------------------------------------------------------------------ save

    @tool()
    def save_tree(tree_id: str, path: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Validate and write the tree to a .ltp3 file. Refuses (VALIDATION_FAILED) when
        validation finds errors and writes nothing. By default it writes a NEW file: `path`, or
        '<opened name>_edited.ltp3' beside the opened file (in the working directory for
        templates and new trees). An existing target fails with FILE_EXISTS unless
        overwrite=true, which first keeps a '.bak-<timestamp>' copy; overwriting the file this
        tree was opened from also fails with STALE_FILE if it changed on disk since it was
        opened. Writes are atomic, restricted to the allowed directories (LTP3_ALLOWED_DIRS,
        default: the server's working directory), refused inside the installed program
        folders, and only .ltp3 is accepted. Returns the path, size, SHA-256, backup path and
        the validation report."""
        s = reg.get(tree_id)
        target = Path(path) if path else _default_target(s)
        same = s.path is not None and _same_file(target, s.path)
        before = s.opened_bytes
        res = safeio.save(
            # fresh Tree from the edited bytes: editor views can be stale after inserts
            Tree.from_bytes(s.current_bytes()),
            target,
            overwrite=overwrite,
            # the hash guard only makes sense for the file this tree was read from
            opened_sha256=s.opened_sha256 if same else None,
        )
        data = res.path.read_bytes()
        lines = changed_lines(before, data)
        s.mark_saved(res.path, data, res.sha256)
        return {
            "ok": True,
            "path": str(res.path),
            "bytes": res.size,
            "sha256": res.sha256,
            "backup_path": str(res.backup) if res.backup else None,
            "validation": _issues(res.report),
            "changed_lines": lines,
        }

    return server


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _default_target(s: Session) -> Path:
    """A path that does not exist yet: '<stem>_edited[_n].ltp3' beside the opened project file,
    else in the working directory (templates live in the read-only install tree)."""
    if s.path is not None and not s.is_template:
        folder, stem = s.path.parent, s.path.stem
    else:
        folder, stem = Path.cwd(), (s.path.stem if s.path else "new_tree")
    if stem.endswith("_edited"):
        stem = stem[: -len("_edited")]
    candidate = folder / f"{stem}_edited.ltp3"
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem}_edited_{n}.ltp3"
        n += 1
    return candidate


# ---------------------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ltp3-mcp",
        description="MCP server (stdio) for LTpowerPlanner III power trees. Started by an MCP "
        "client; see docs/CLIENTS.md. Environment: LTP3_TEMPLATE_DIR (template folder), "
        "LTP3_ALLOWED_DIRS (folders save_tree may write to; default: working directory).",
    )
    parser.add_argument("--version", action="version", version=f"ltp3-mcp {__version__}")
    parser.parse_args(argv)
    # stdout is the protocol channel: send all logging to stderr
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
