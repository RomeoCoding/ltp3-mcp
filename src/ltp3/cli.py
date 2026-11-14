"""The `ltp3` command line: a thin wrapper over the library, usable in CI.

Exit codes: 0 success; 1 the check found something (validation errors, a difference, an
incomplete solve with --strict, a round-trip mismatch); 2 usage or I/O error (unreadable file,
unknown tab, existing output file).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, catalog
from .compare import compare_trees
from .errors import FILE_EXISTS, UNKNOWN_TAB, Ltp3Error
from .export import to_json
from .graph import build_graph
from .lossless import parse, serialize
from .model import Tree
from .solver import SolveOptions, SolveResult
from .solver import solve as run_solve
from .validate import Report
from .validate import validate as run_validate


def _out(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _dump(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True)


def _tab_indices(tree: Tree, ref: str | None) -> list[int]:
    if ref is None:
        return list(range(len(tree.tabs)))
    if ref.lstrip("-").isdigit():
        i = int(ref)
        if 0 <= i < len(tree.tabs):
            return [i]
        raise Ltp3Error(UNKNOWN_TAB, f"tab index {i} out of range (0..{len(tree.tabs) - 1})")
    for i, t in enumerate(tree.tabs):
        if t.name == ref:
            return [i]
    raise Ltp3Error(UNKNOWN_TAB, f"no tab named {ref!r}")


# ------------------------------------------------------------------------------- commands


def _fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def _solve_text(name: str, index: int, res: SolveResult) -> str:
    d = res.display
    lines = [
        f"Tab {index}: {name}   ({'complete' if res.complete else 'INCOMPLETE'}, "
        f"current mode {res.current_mode})",
        f"  Pin {_fmt(d.pin_w)} W   Pout {_fmt(d.pout_w)} W   Ploss {_fmt(d.ploss_w)} W   "
        f"Eff {_fmt(d.efficiency_pct)} %",
    ]
    for p in res.parts:
        if p.pin_w is None and p.ploss_w is None:
            continue
        lines.append(
            f"  {p.id:>4} {p.type:<10} {p.name:<28} Pin {_fmt(p.pin_w):>8}  "
            f"Ploss {_fmt(p.ploss_w):>8}  [{p.status}]"
        )
    for label, notes in (("assumed", res.assumptions), ("blocked", res.blocked)):
        lines += [f"  {label} {n.code}: {n.message}" for n in notes]
    lines += [f"  warning {w.code}: {w.message}" for w in res.warnings]
    return "\n".join(lines)


def cmd_solve(args: argparse.Namespace) -> int:
    tree = Tree.load(args.file)
    results: list[tuple[int, str, SolveResult]] = []
    for i in _tab_indices(tree, args.tab):
        res = run_solve(build_graph(tree.tabs[i]), SolveOptions(current_mode=args.mode))
        results.append((i, tree.tabs[i].name, res))
    if args.json:
        _out(_dump({"tabs": [{"tab": i, "tab_name": n, **r.to_dict()} for i, n, r in results]}))
    else:
        _out("\n\n".join(_solve_text(n, i, r) for i, n, r in results))
    return 1 if args.strict and not all(r.complete for _, _, r in results) else 0


def _report_dict(report: Report) -> dict[str, Any]:
    def one(i: Any) -> dict[str, Any]:
        return {"code": i.code, "message": i.message, "tab": i.tab}

    return {
        "ok": report.ok,
        "errors": [one(i) for i in report.errors],
        "warnings": [one(i) for i in report.warnings],
    }


def cmd_validate(args: argparse.Namespace) -> int:
    report = run_validate(Tree.load(args.file))
    failed = bool(report.errors) or (args.strict and bool(report.warnings))
    if args.json:
        _out(_dump(_report_dict(report)))
    else:
        for i in report.issues:
            where = "" if i.tab is None else f" [tab {i.tab}]"
            _out(f"{i.severity} {i.code}{where}: {i.message}")
        _out(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 1 if failed else 0


def cmd_diff(args: argparse.Namespace) -> int:
    diff = compare_trees(
        Tree.load(args.a),
        Tree.load(args.b),
        include_layout=args.layout,
        include_header=args.header,
    )
    if args.json:
        _out(_dump(diff.to_dict()))
    elif diff.is_empty:
        _out("no differences")
    else:
        for c in diff.changes:
            what = f"{c.kind} {c.tab}/{c.target}" + (f" {c.key}" if c.key else "")
            _out(f"{what}: {c.old!r} -> {c.new!r}" if c.key else f"{what}: {c.old or c.new}")
    return 0 if diff.is_empty else 1


def cmd_export(args: argparse.Namespace) -> int:
    text = to_json(Tree.load(args.file), include_layout=args.layout)
    if args.output is None:
        sys.stdout.write(text)
        return 0
    target = Path(args.output)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as fh:  # never overwrites
            fh.write(text)
    except FileExistsError:
        raise Ltp3Error(FILE_EXISTS, f"{target} already exists", "Pick a new name.") from None
    return 0


def cmd_roundtrip(args: argparse.Namespace) -> int:
    Tree.load(args.file)  # raises NOT_FOUND / FILE_LOCKED / PARSE_ERROR for unreadable input
    data = Path(args.file).read_bytes()
    ok = serialize(parse(data)) == data
    _out(f"{args.file}: {'byte-exact' if ok else 'MISMATCH'} ({len(data)} bytes)")
    return 0 if ok else 1


def cmd_templates(args: argparse.Namespace) -> int:
    items = catalog.list_templates(
        vendor=args.vendor, group=args.group, text=args.search, directory=args.dir
    )
    if args.json:
        _out(_dump([i.to_dict() for i in items]))
    else:
        for i in items:
            _out(f"{i.file}\t{i.vendor}\t{i.device}\t{len(i.tabs)} tab(s)")
        _out(f"{len(items)} template(s)")
    return 0


# ------------------------------------------------------------------------------- parser


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ltp3",
        description="Inspect, validate, solve and compare LTpowerPlanner III power trees. "
        "Exit codes: 0 ok; 1 findings (validation errors, differences, ...); 2 usage or I/O "
        "error. Not affiliated with Analog Devices.",
    )
    p.add_argument("--version", action="version", version=f"ltp3 {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    s = sub.add_parser("solve", help="compute the power budget (the app's Calculate)")
    s.add_argument("file")
    s.add_argument("--tab", help="tab index or name (default: every tab)")
    s.add_argument("--mode", choices=["auto", "peak", "nominal"], default="auto")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true", help="exit 1 if any tab is incomplete")
    s.set_defaults(func=cmd_solve)

    v = sub.add_parser("validate", help="check structure and electrical sanity")
    v.add_argument("file")
    v.add_argument("--json", action="store_true")
    v.add_argument("--strict", action="store_true", help="warnings also fail")
    v.set_defaults(func=cmd_validate)

    d = sub.add_parser("diff", help="semantic diff of two trees (exit 1 if they differ)")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--layout", action="store_true", help="include positions, sizes, colours")
    d.add_argument("--header", action="store_true", help="include file header tags")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_diff)

    e = sub.add_parser("export", help="neutral JSON export of a tree")
    e.add_argument("file")
    e.add_argument("--layout", action="store_true")
    e.add_argument("-o", "--output", help="write to a NEW file instead of stdout")
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("roundtrip", help="check that parse+serialize reproduces the file bytes")
    r.add_argument("file")
    r.set_defaults(func=cmd_roundtrip)

    t = sub.add_parser("templates", help="list factory templates (read in place)")
    t.add_argument("--vendor")
    t.add_argument("--group")
    t.add_argument("--search", help="every word must occur in some header field")
    t.add_argument("--dir", help="template folder (default: LTP3_TEMPLATE_DIR or the install)")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_templates)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code: int = args.func(args)
    except Ltp3Error as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        sys.stderr.write(f"error {exc.code}: {exc.message}{hint}\n")
        return 2
    return code


def main_entry() -> None:
    """Console-script entry point (a script's return value is not an exit code on all paths)."""
    sys.exit(main())
