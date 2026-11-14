# ltp3-mcp — implementation plan (v1)

Audience: Sonnet implementers (one per phase) and the architect who reviews them.
Companion: `docs/FORMAT.md` (format spec, data-derived). Read FORMAT.md before any phase.
Tags used below: **[C]** confirmed on the 261-file corpus, **[S]** confirmed on the saved samples,
**[H]** hypothesis that needs an oracle.

## 0. Environment (verified 2026-11-14)

| Item | Value |
|---|---|
| OS / shells | Windows 11, Git Bash + PowerShell 5.1 |
| Python | 3.14.6 (default `python`), CPython 3.12.13 (uv-managed) |
| uv | 0.12.1 |
| git | 2.55.0 (repo not initialised yet) |
| Corpus | 261 `*.ltp3t` in `C:\Program Files (x86)\LTC\LTpowerCAD2\LTpowerPlanner III\PlannerDataBase\FactorySolutions` |
| Samples | `samples\artix7_original.ltp3`, `samples\artix7_resaved.ltp3` (byte-identical, app v3.6.8) |

Hard rule for everyone: never open, decompile, `strings`-dump or otherwise inspect
`LTpowerPlannerIII.exe`, any DLL, `.config` or `.sdf` file. Work from `.ltp3`/`.ltp3t` data only.
Never copy a factory template into the repo, a test fixture, a docstring or a commit.

## 1. Scope

### In scope for v1
1. Lossless reader/writer for `.ltp3` and `.ltp3t`: `write(parse(b)) == b` for all 261 templates and
   both samples.
2. Semantic graph: tabs, parts, terminals, nets resolved through wire junctions and net ids.
3. Solver for the confirmed subset (ideal source, converters with `NomEff`, loads), with every
   unconfirmed rule behind a named, reported assumption.
4. Editor: set/unset properties, rename, add/remove parts, connect/disconnect with generated layout.
5. Validator, safe save (validate, atomic write, backup, no silent overwrite), structural compare.
6. MCP server over stdio with the tools of section 4; works with Claude Desktop/Code, Gemini CLI and
   OpenAI-compatible MCP clients.
7. `export_json`: the semantic graph as plain JSON (vendor-neutral stepping stone).

### Non-goals for v1
- `.sdf` part databases, part selection, LTpowerCAD `.ltpc` design files.
- Driving the GUI, launching the app, or any automation of ADI binaries.
- Pixel-faithful layout. Generated wires are legal and orthogonal, not pretty.
- Transient, thermal, sequencing or tolerance analysis.
- HTTP transport and multi-user hosting (stdio only).

### Recommendations on the two optional bridges
- **`.ptproj` export (bin2ai/powertree): defer to v1.1.** The schema belongs to another project and
  can move; the mapping is lossy (no layout, different efficiency model); and it adds a second format
  to keep correct before the first one is proven. `export_json` in v1 gives 90 % of the value and is
  the input a later `ptproj` exporter would consume.
- **LTspice bridge: defer to v2, probably a separate package.** A power tree holds budgets, not
  circuits: there is no netlist to hand to `spicelib` without inventing component values. It also
  drags in a heavy dependency and a second ADI tool. Revisit once the solver is oracle-confirmed.

## 2. Architecture

```
ltp3-mcp/
  pyproject.toml  uv.lock  .python-version  .gitignore  .gitattributes  LICENSE  README.md
  docs/           PLAN.md  FORMAT.md  SOLVER.md (P3)  TOOLS.md (P7)
  src/ltp3/
    __init__.py   __version__ only                                   (P0)
    errors.py     Ltp3Error + error codes                            (P0)
    paths.py      template dir, allowed roots, path policy            (P0)
    lossless.py   bytes <-> Document (blocks, props, raw pieces)      (P1)
    numfmt.py     .NET-style number formatting/parsing                (P1)
    model.py      typed views: Tab, Part, Terminal, Wire, Symbol      (P2)
    graph.py      nets, drivers, consumers, traversal                 (P2)
    solver.py     power solve + assumptions report                    (P3)
    layout.py     anchors, placement, orthogonal routing              (P4)
    editor.py     mutations on the Document through model views       (P4)
    validate.py   structural + electrical checks                      (P5)
    safeio.py     load, atomic save, backup, hash guard               (P5)
    compare.py    structural diff of two trees                        (P6)
    catalog.py    template listing/search (reads in place)            (P6)
    export.py     graph -> JSON                                       (P6)
    session.py    open-tree registry, undo stack                      (P7)
    server.py     MCP tools, error mapping, entry point               (P7)
    cli.py        `python -m ltp3` dev CLI                            (P8)
  tests/
    conftest.py   fixtures (P0)      oracles/  user-built .ltp3 + expected JSON (P9)
    test_<module>.py per module (owner = the module's phase)
```

### 2.1 Lossless layer (`lossless.py`)
The Document is the single source of truth. Nothing above it keeps a second copy of file data.

- `Document(bom: bool, encoding: str, lines: list[Line])`.
- `Line` is one of:
  - `TagLine(tag, sep, args, eol)` — `sep` is the literal text between `>` and args (`" "` or `""`),
    `args` the raw remainder. *Why raw:* `<DS11>0.27…` has no space and `<Note> ` has a space and
    no args; both must survive.
  - `PropLine(key_raw, sep, value, eol)` — `key_raw` is the 8-char padded key as found, `sep` the
    text after `:` (normally one space).
  - `RawLine(text, eol)` — anything matching neither grammar. None exist in the corpus; this keeps
    future versions lossless instead of failing.
- Split on `\r\n` only and store `eol` per line. *Why:* a lone `\r` is data in 7 files, and per-line
  `eol` keeps a file that was LF-mangled by git still round-trippable.
- Decode strict UTF-8; on failure fall back to `cp1252` and record it in `encoding`.
- `serialize()` concatenates raw pieces. No normalisation, ever. Edits replace individual pieces.
- A block view groups a `TagLine` with its following `PropLine`s. `Term` blocks attach to the
  preceding `BPrt`; everything from `<Tabs>` to the next `<Tabs>` is a tab.
- Split rules (from FORMAT.md): `BPrt` args max-split 4 on `", "`, `Tabs` max-split 2, because names
  contain commas. Re-joining the split pieces must reproduce `args` exactly, else the view refuses
  to edit that line and reports `UNPARSEABLE_ARGS`.

### 2.2 Model and graph (`model.py`, `graph.py`)
- Views are thin objects holding a reference to their block; getters parse on demand, setters edit
  the block in place. Unknown keys are reachable through `block.props` and are never dropped.
- Addressing: `tab` by 0-based index or exact name; `part` by id (unique per tab **[C]**) or exact
  name when unambiguous; terminal as `"<part>:<kind>:<n>"`, e.g. `"2:Output:1"`.
- Nets: union of (a) wires sharing `netId` and (b) wires linked through `SWires`/`EWires`. These
  agree in 444/444 tabs **[C]**; disagreement is a validation warning, and the union is used.
- `Net` parts with equal names in one tab are merged into one net only when `merge_net_labels=True`
  (default True, reported as assumption `A-NETLABEL` **[H]**).
- Graph API: `nets(tab)`, `net_of(terminal)`, `drivers(net)` (Output terminals), `consumers(net)`
  (Input terminals), `passives(net)` (Aux), `upstream(part)`, `downstream(part)`, `topo_order(tab)`
  (raises `CYCLE` on a loop).

### 2.3 Solver (`solver.py`)
Pure function `solve(tab_graph, options) -> SolveResult`. Section 6 lists every rule and its status.
Each rule that is not **CONFIRMED** emits an entry in `result.assumptions`; rules marked **BLOCKED**
are not computed at all: the affected part gets `status="unsupported"` and totals are returned with
`complete=False`. The solver never guesses silently.

### 2.4 Editor and layout (`editor.py`, `layout.py`)
- Every mutation is the smallest possible line edit. Changing one value changes one line; nothing
  else in the file moves. A value numerically equal to the stored one is a no-op (keeps `3.30` if a
  future file writes it that way).
- New property lines are inserted at the canonical position from FORMAT.md §3–§5 key orders.
- New ids: `max(all part, wire, net and symbol ids in the tab) + 1` **[H]**.
- New parts: inserted after the last `<Term>` of the last part in the tab (before the first `Symb`);
  new wires appended at the end of the tab. Order **[C]**: parts, symbols, wires.
- New part geometry: `Height = 60 × max(inputs, outputs, 1)` so both observed anchor rules coincide
  (FORMAT.md §5.1); default widths Power 110, Converter 180, LDO 180, Load 140, Resistor 80,
  Capacitor 100 (the corpus modes); position on the 5-unit grid, in the first free column to the
  right of the upstream part, below existing parts in that column with a 40-unit gap.
- New parts write only this key set: `Color, Width, Height[, PartNumb][, Resstnce]` plus terminal
  keys the caller supplied. Colours: Power `#00FFFFFF`, Converter `#FFAFEEEE`, Load `#FFFFB6C1`
  (Artix-7 values), others `#00FFFFFF`.
- Wires: orthogonal, 2–4 points, from the Output anchor to the Input anchor through a mid-x snapped
  to 5. Written with `ZIndex : -1` first when the document already has it on any wire or
  `<Vers>` ≥ 3.5.3, otherwise omitted.
- Fan-out: an Input/Output terminal carries at most one wire **[C]**. Connecting a second consumer
  to a wired output splits the existing wire at a junction point on its first segment: the trunk
  keeps the driver end and gets `EWires`; each branch gets `SWires` listing every other wire at the
  junction (clique **[C]**), all share the junction coordinate and the trunk's `netId`. Adding to an
  existing junction extends the clique on every member.
- `remove_part` removes the part, its terminals and its wires; a junction left with two wires is
  merged back into one wire, and a junction left with one keeps a dangling-free shape (the lone
  branch absorbs the trunk). Nets left without wires disappear.
- Text values: API uses `\n`; the file uses `*^!#` **[C]**. CR and LF are rejected in every other
  value. Names may contain commas; they may not contain CR/LF.

### 2.5 Safety model (the anti-corruption contract)
1. Trees live in memory under a `tree_id`. Tools never write to disk except `save_tree`.
2. `save_tree` pipeline: `validate` (errors block) → `serialize` → **re-parse the bytes and require
   graph equality with the in-memory tree** → write `<name>.tmp-<pid>` in the target directory →
   flush + `os.fsync` → if the target exists, copy it to `<name>.bak-YYYYMMDD-HHMMSS` →
   `os.replace(tmp, target)`. *Why same directory:* `os.replace` is atomic only within one volume.
3. Default target is a **new path**. Saving over the opened file needs `overwrite=true` **and** the
   on-disk SHA-256 must still equal the hash recorded at open (`STALE_FILE` otherwise: the GUI may
   have saved in between).
4. Writes are refused inside the ADI install tree and outside `LTP3_ALLOWED_DIRS` (default: the
   server's working directory). Only `.ltp3` may be written; `.ltp3t` is read-only.
5. Every mutating tool returns `changed_lines` (added/removed/modified counts) and pushes an undo
   snapshot (the serialized bytes; documents are ≤ 73 kB so this is cheap).
6. `preview_changes` returns a unified diff against the opened bytes, so a human or the model can
   see the blast radius before saving.

## 3. Dependencies (PyPI checked 2026-11-14; pin exactly)

| Package | Version | Role | Note |
|---|---|---|---|
| `mcp` | 2.3.0 | runtime | Official SDK. In 2.x the high-level server is `from mcp.server import MCPServer` (the 1.x name `FastMCP` is gone); `from mcp import Client` gives an in-process client for tests. Pulls pydantic ≥ 2.12, anyio, starlette, uvicorn, pywin32. |
| `pytest` | 9.2.0 | dev | |
| `pytest-cov` | 7.1.0 | dev | |
| `hypothesis` | 6.170.15 | dev | property tests for edit → serialize → parse |
| `ruff` | 0.17.5 | dev | lint + format |
| `mypy` | 2.4.1 | dev | `--strict` on `src/` |
| `hatchling` | 1.33.0 | build | |

- `requires-python = ">=3.10"` (the SDK floor). Develop on 3.12: `.python-version` = `3.12`.
  *Why not 3.14:* 3.12 is the conservative wheel target for the SDK's native dependencies.
- `uv.lock` is committed. Install with `uv sync --dev`; run everything through `uv run`.
- Core modules (`lossless` … `export`) import the standard library only. Only `session.py` and
  `server.py` may import `mcp`/`pydantic`. *Why:* the parser stays usable and testable without the
  SDK, and an SDK major bump touches two files. A test enforces it.
- The 2.x SDK is five months old. P7 starts with a 20-line spike test (register one tool, call it
  through `Client`) before any real tool is written; if an import in this plan is wrong, fix the plan
  first.

## 4. MCP tools

Conventions: all results are JSON objects (structured content). Errors are raised as `Ltp3Error`
and mapped by `server.py` to an MCP tool error whose text is JSON
`{"code": "...", "message": "...", "hint": "..."}`. Codes: `NOT_FOUND`, `BAD_PATH`,
`PATH_NOT_ALLOWED`, `PARSE_ERROR`, `UNKNOWN_TREE`, `UNKNOWN_TAB`, `UNKNOWN_PART`, `AMBIGUOUS_PART`,
`UNKNOWN_TERMINAL`, `BAD_VALUE`, `UNKNOWN_KEY`, `TERMINAL_IN_USE`, `WOULD_SHORT`, `CYCLE`,
`READ_ONLY`, `VALIDATION_FAILED`, `FILE_EXISTS`, `STALE_FILE`, `UNPARSEABLE_ARGS`, `NOTHING_TO_UNDO`.
`tab` defaults to the first tab. Every mutating tool fails with `READ_ONLY` on a read-only tree and
returns `{ok, changed_lines: {added, removed, modified}, warnings: [...]}` plus the fields listed.

| Tool | Arguments | Returns | Errors |
|---|---|---|---|
| `list_templates` | `query?: str`, `vendor?: str`, `group?: str`, `limit: int = 50` | `{template_dir, total, items: [{name, path, version, group, vendor, device, description, tabs, input_rails, output_rails}]}` | `NOT_FOUND` if the directory is absent (message says how to set `LTP3_TEMPLATE_DIR`) |
| `open_tree` | `path: str`, `read_only: bool = false` | `{tree_id, path, version, is_template, sha256, tabs: [{index, id, name, parts, wires}], warnings}` | `NOT_FOUND`, `BAD_PATH`, `PARSE_ERROR`. Templates always open read-only as a source: edits are allowed, saving back to the template path is not. |
| `new_tree` | `tab_name: str = "New Tab 1"` | as `open_tree` | experimental until GUI check G1 (section 6.3) passes; result carries `warnings: ["EXPERIMENTAL_SKELETON"]` |
| `close_tree` | `tree_id`, `discard: bool = false` | `{ok}` | `UNKNOWN_TREE`; `VALIDATION_FAILED` reused as "unsaved changes" guard unless `discard` |
| `describe_tree` | `tree_id`, `tab?` | `{header: {...}, tabs: [{index, name, counts_by_type, nets, unconnected_terminals}], dirty: bool}` | `UNKNOWN_TREE`, `UNKNOWN_TAB` |
| `list_parts` | `tree_id`, `tab?`, `type?` | `{parts: [{id, type, name, part_number, x, y, properties, terminals: [{ref, kind, n, properties, net, connected_to: [terminal refs]}]}]}` | `UNKNOWN_TREE`, `UNKNOWN_TAB` |
| `get_part` | `tree_id`, `part`, `tab?` | one part object as above plus `upstream`, `downstream` part ids | `UNKNOWN_PART`, `AMBIGUOUS_PART` (lists candidates) |
| `list_nets` | `tree_id`, `tab?` | `{nets: [{net_id, voltage?, drivers, consumers, passives, wire_ids, labels}]}` | as above |
| `solve` | `tree_id`, `tab?`, `current_mode: "auto"\|"peak"\|"nominal" = "auto"` | `{complete: bool, totals: {pin_w, pout_w, ploss_w, efficiency_pct} (full precision), display: {...} (2 dp, as the app shows), sources: [...], parts: [{id, name, type, status, vin, iin, pin_w, pout_w, ploss_w, outputs: [{n, vout, iout, pout_w, pin_w, eff_pct}]}], loads: [{terminal, v, i, p_w}], assumptions: [{code, message, parts}], warnings}` | `CYCLE`; unsupported constructs do not raise, they set `complete=false` |
| `validate` | `tree_id` | `{ok: bool, errors: [...], warnings: [{code, message, tab, part?}]}` | `UNKNOWN_TREE` |
| `set_property` | `tree_id`, `target: str` (part ref or terminal ref), `key: str`, `value: str\|number\|null`, `tab?`, `allow_unknown_key: bool = false` | `{old, new}` | `UNKNOWN_KEY` unless the key was seen for that owner type in FORMAT.md or the flag is set; `BAD_VALUE` (form mismatch, CR/LF); `null` removes the line |
| `rename_part` | `tree_id`, `part`, `name`, `tab?` | `{old, new}` | `BAD_VALUE` |
| `add_part` | `tree_id`, `type: Power\|Converter\|LDO\|Load\|Resistor\|Capacitor`, `name`, `tab?`, `part_number?`, `outputs?: [{vout, nom_eff?, iout_max?}]`, `inputs?: [{ipeak?, inom?, unit_exp?}]`, `resistance?`, `near?: part`, `x?`, `y?` | `{part: {...}}` | `BAD_VALUE`; `Net` parts are not creatable in v1 |
| `remove_part` | `tree_id`, `part`, `tab?` | `{removed_wires: [...], merged_junctions}` | `UNKNOWN_PART` |
| `connect` | `tree_id`, `source: terminal`, `sink: terminal`, `tab?` | `{net_id, wires_added, wires_modified}` | `TERMINAL_IN_USE` (sink already wired), `WOULD_SHORT` (joins two nets that both have drivers), `CYCLE`, `BAD_VALUE` (not Output→Input) |
| `disconnect` | `tree_id`, `terminal`, `tab?` | `{removed_wires, merged_junctions}` | `UNKNOWN_TERMINAL` |
| `move_part` | `tree_id`, `part`, `x`, `y`, `tab?` | `{rerouted_wires}` | `BAD_VALUE` (off-grid values are snapped, reported in `warnings`) |
| `undo` | `tree_id` | `{remaining}` | `NOTHING_TO_UNDO` |
| `preview_changes` | `tree_id`, `max_lines: int = 200` | `{dirty, diff: str, stats}` | `UNKNOWN_TREE` |
| `compare_trees` | `a: tree_id`, `b: tree_id`, `tab_a?`, `tab_b?`, `include_layout: bool = false`, `include_solve: bool = true` | `{parts_added, parts_removed, parts_changed: [{part, key, a, b}], connections_added, connections_removed, solve_delta?}` | `UNKNOWN_TREE`. Parts match by id, then by `(type, name)`. |
| `export_json` | `tree_id`, `tab?` | `{schema: "ltp3-graph/1", tabs: [...]}` | `UNKNOWN_TREE` |
| `save_tree` | `tree_id`, `path?: str`, `overwrite: bool = false` | `{path, bytes, sha256, backup_path?, validation: {...}, changed_lines}` | `VALIDATION_FAILED` (nothing written), `FILE_EXISTS` (target exists and `overwrite` is false), `STALE_FILE`, `PATH_NOT_ALLOWED`, `BAD_PATH` (not `.ltp3`) |

Tool descriptions must state units (volts, amperes, watts, percent) and that `solve` reports
assumptions. `docs/TOOLS.md` is generated from the registered schemas in P7.

## 5. Phases

One Sonnet implementer per phase. Test-first: commit failing tests, then the implementation. A phase
edits only the files it owns. Fixes needed in another phase's file are reported, not made.
`docs/FORMAT.md` and `docs/PLAN.md` are owned by the architect.

```
P0 -> P1 -> P2 -> { P3 | P4 | P5 | P6 } -> P7 -> P8        P9 starts when oracles arrive (needs P3)
```
P3, P4, P5, P6 run in parallel: they share no file and depend only on P1/P2 interfaces.

Shared fixtures (`tests/conftest.py`, P0): `template_dir` (env `LTP3_TEMPLATE_DIR`, default the path
in section 0; `pytest.skip` when absent), `template_paths` (sorted list), `sample_path`
(`samples/artix7_original.ltp3`, skip when absent), marker `corpus`. Corpus tests are parametrised
by file name so a failure names the template.

### P0 — Scaffold (sequential, small)
Owns: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.gitattributes`, `LICENSE`,
`README.md` (stub), `src/ltp3/__init__.py`, `src/ltp3/errors.py`, `src/ltp3/paths.py`,
`tests/conftest.py`, `tests/test_scaffold.py`, `.github/workflows/ci.yml`.
- `git init` on branch `main` happens here, after the user approves this plan.
- `.gitattributes`: `*.ltp3 -text` and `*.ltp3t -text`. *Why:* autocrlf would rewrite CRLF and break
  byte round-trip on checkout.
- `.gitignore`: `samples/`, `*.ltp3t`, `*.bak-*`, `.venv/`. *Why `samples/`:* the Artix-7 samples are
  Save-As copies of an ADI template and count as ADI content.
- `errors.py`: `Ltp3Error(code, message, hint="")` and the code constants of section 4.
- `paths.py`: `template_dir()`, `allowed_roots()` (`LTP3_ALLOWED_DIRS`, `os.pathsep`-separated),
  `check_writable(path)` implementing rule 4 of section 2.5.
- CI runs lint, types and the non-corpus tests on windows-latest and ubuntu-latest (corpus tests
  skip there by design).

Acceptance:
```
uv sync --dev                                   -> exit 0, creates uv.lock
uv run pytest -q                                -> all pass, 0 errors
uv run ruff check . && uv run ruff format --check .   -> clean
uv run mypy --strict src                        -> Success: no issues found
git check-attr text -- x.ltp3                   -> "text: unset"
git status --short samples                      -> empty (ignored)
```

### P1 — Lossless layer
Owns: `src/ltp3/lossless.py`, `src/ltp3/numfmt.py`, `tests/test_lossless.py`, `tests/test_numfmt.py`.
Tests first:
- `test_roundtrip_corpus[<name>]`: `serialize(parse(b)) == b` for each of the 261 templates.
- `test_roundtrip_samples`: both samples.
- `test_census`: totals equal FORMAT.md — tags `Symb 7364, Term 5891, Wire 4253, BPrt 3904, Tabs 444,
  Summ 444`; 79,556 property lines; 0 `RawLine`; 1 BOM file; 10 lone-CR values in 7 files.
- Synthetic cases written inline in the test (never copied from a template): BOM, lone CR inside a
  value, `<DS11>0.5` without space, `<Note> ` with trailing space, empty value with trailing space,
  LF-only file, unknown tag, unknown line shape → `RawLine`, value containing `:`.
- Hypothesis: random documents built from the grammar round-trip; editing one `PropLine.value`
  changes exactly that line in the output.
- `numfmt`: `fmt(5.0) == "5"`, `fmt(3.3) == "3.3"`, `fmt(1e-05) == "1E-05"`, `fmt(5.5e-05) ==
  "5.5E-05"`, `fmt(0.0001) == "0.0001"`; `parse` accepts every numeric value form in the corpus
  (test iterates the corpus and requires `fmt(parse(v)) == v` for ≥ 99.9 % of numeric values and
  lists the exceptions).

Acceptance:
```
uv run pytest tests/test_lossless.py -q -k corpus     -> 261 passed
uv run pytest tests/test_lossless.py tests/test_numfmt.py -q   -> all pass
uv run python -c "import sys, ltp3.lossless; assert 'mcp' not in sys.modules and 'pydantic' not in sys.modules"
```

### P2 — Model and graph
Owns: `src/ltp3/model.py`, `src/ltp3/graph.py`, `tests/test_model.py`, `tests/test_graph.py`.
Tests first:
- Census: parts by type `Load 1371, LDO 752, Converter 635, Power 470, Capacitor 432, Resistor 200,
  Net 44`; 77 part names and 16 tab names containing commas re-join exactly; part ids unique per tab.
- Nets: 2188 nets over the corpus with label merging off; driver histogram `0: 18, 1: 2109, 2: 40,
  ≥3: 21`; net-id partition equals link partition in 444/444 tabs; 299 unconnected terminals.
- Artix-7 sample: 4 parts; nets `{1:Output:1 → 2:Input:1, 16:Input:1}`, `{2:Output:1 → 11:Input:1}`,
  `{2:Output:2 → 11:Input:2}`, `{16:Output:1 → 11:Input:3}`; `topo_order` = Power, converters, Load.
- Tolerance: the tab with duplicate wire ids (`Power Tree for ADGM1304.ltp3t`) still builds nets.
- Views are live: setting `terminal.props["Vout"]` changes exactly one serialized line.

Acceptance: `uv run pytest tests/test_model.py tests/test_graph.py -q` → all pass (corpus tests not
skipped on the dev machine: the summary line shows 0 skipped).

### P3 — Solver (parallel)
Owns: `src/ltp3/solver.py`, `docs/SOLVER.md`, `tests/test_solver.py`.
Implements section 6 exactly, with the statuses given there. Tests first:
- Artix-7: `pin_w == 16.674575…`, `pout_w == 14.7`, `ploss_w == 1.974575…`,
  `efficiency_pct == 88.158…` (tolerance 1e-9 against the closed form
  `3/0.8 + 9.9/0.91 + 1.8/0.88`), display values `16.67 / 14.7 / 1.97 / 88.16`, `complete is True`,
  assumptions exactly `[A-CURRENT-PEAK-ONLY]`.
- Synthetic trees built with the lossless layer in the test: LDO, cascade, resistor, missing
  efficiency, multi-driver → each yields the documented assumption code or `status="unsupported"`
  and `complete is False`; none raises.
- `test_oral_header_agreement` (corpus, informative regression): with `current_mode="nominal"` and
  the load voltage/current rule of FORMAT.md §6, the formatted `V/I` list equals `<ORal>` in at least
  150 templates. The architect's prototype reaches 151.
- Corpus smoke: `solve` returns without exception on all 444 tabs; record how many are `complete`.

Acceptance: `uv run pytest tests/test_solver.py -q` → all pass; `docs/SOLVER.md` has one row per
hypothesis with status and the test that covers it.

### P4 — Editor and layout (parallel)
Owns: `src/ltp3/editor.py`, `src/ltp3/layout.py`, `tests/test_editor.py`, `tests/test_layout.py`.
Tests first:
- Minimal diff: `set_property` on an existing key changes 1 line; adding a key adds 1 line at the
  canonical position; removing removes 1; setting an equal number is a 0-line change.
- Anchors: `layout.anchor()` reproduces the stored wire end for ≥ 5925 of 5936 x-coordinates and for
  every part whose `Height == 60 × n` (FORMAT.md §5.1).
- `add_part` + `connect` on the Artix-7 sample: new Load on `16:Output:1` (already wired) → the old
  wire becomes a trunk, two branches exist, clique lists and coordinates are consistent, `netId`
  shared, graph shows both consumers, every pre-existing line except the split wire is unchanged.
- `remove_part` undoes that edit down to a graph equal to the original.
- Corpus fuzz (`-k fuzz`, 261 cases): on the first tab, add a Load and connect it to the first
  Output terminal found; then `serialize → parse → graph` shows the new edge, all wire segments are
  axis-aligned, junction cliques hold, ids are unique among parts.
- Rejections: CR/LF in a value, connecting Output→Output, a second wire on an Input.

Acceptance: `uv run pytest tests/test_editor.py tests/test_layout.py -q` → all pass, fuzz shows 261.

### P5 — Validation and safe I/O (parallel)
Owns: `src/ltp3/validate.py`, `src/ltp3/safeio.py`, `tests/test_validate.py`, `tests/test_safeio.py`.
- Errors (block saving): dangling terminal or wire reference, duplicate part id in a tab, wire end
  with neither terminal nor links, non-clique junction, junction coordinate mismatch, diagonal wire
  segment, Input/Output terminal with two wires, CR/LF in a tag line, unparseable numeric where the
  key is numeric, tab order violated (part after symbol/wire).
- Warnings: multi-driver net, undriven consumer, unconnected terminal, cycle, `Vout` outside
  `VoutMin..VoutMax`, upstream voltage outside `VinMin..VinMax`, load current above `IoutMax`,
  net-id/link disagreement, duplicate wire id, unknown tag or key.
- Tests first: every one of the 261 templates and the samples validates with **zero errors** (warning
  counts are recorded in the test output); one synthetic negative case per error code.
- `safeio`: each step of section 2.5 has a test, including a simulated crash between temp write and
  replace (target untouched), backup creation, `FILE_EXISTS`, `STALE_FILE`, `PATH_NOT_ALLOWED` for a
  path under `Program Files`, refusal of `.ltp3t`, and byte-exact output.

Acceptance: `uv run pytest tests/test_validate.py tests/test_safeio.py -q` → all pass.

### P6 — Compare, catalogue, export (parallel)
Owns: `src/ltp3/compare.py`, `src/ltp3/catalog.py`, `src/ltp3/export.py`, `tests/test_compare.py`,
`tests/test_catalog.py`, `tests/test_export.py`.
- `catalog`: 261 items; filters by vendor (`ADI` 237, `Xilinx` 7 …) and free text; no file copied.
- `compare`: template vs sample for Artix-7 reports no part or connection change; a one-property
  edit reports exactly that change; layout differences hidden unless `include_layout`.
- `export`: JSON is deterministic, validates against a schema defined in the test, and contains no
  layout unless requested.

Acceptance: `uv run pytest tests/test_compare.py tests/test_catalog.py tests/test_export.py -q`.

### P7 — MCP server
Owns: `src/ltp3/session.py`, `src/ltp3/server.py`, `docs/TOOLS.md`, `tests/test_session.py`,
`tests/test_server.py`. Depends on P3–P6.
- Step 1: SDK spike test (section 3). Step 2: tools of section 4, thin wrappers only.
- Tests through the in-process `Client`: registered tool names equal the section 4 list exactly;
  every tool has typed arguments and a description; an end-to-end script (open template → add LDO +
  Load → connect → validate → solve → save to `tmp_path` → reopen → compare) passes; each error code
  of section 4 is produced by at least one test; the server never writes outside `tmp_path`.
- Entry point `ltp3-mcp = "ltp3.server:main"` (stdio).

Acceptance:
```
uv run pytest tests/test_session.py tests/test_server.py -q    -> all pass
uv run ltp3-mcp --help                                          -> exit 0
uv run pytest -q                                                -> whole suite green, 0 skipped locally
```

### P8 — CLI, docs, release hygiene
Owns: `src/ltp3/cli.py`, `tests/test_cli.py`, `README.md` (full), `CONTRIBUTING.md`, `CHANGELOG.md`,
`examples/` (client configs for Claude Desktop, Claude Code, Gemini CLI, generic stdio).
- `python -m ltp3 roundtrip|validate|solve|dump <file>`.
- README per section 7. Coverage gate: `uv run pytest --cov=ltp3 --cov-fail-under=90`.
- Repository audit script in `tests/test_repo_hygiene.py`: no `.ltp3t`, no file under `samples/`
  tracked, no string from the header fields `SolN`/`Desc` of any template present in tracked files.

### P9 — Oracle integration (blocked on the user, section 6.2)
Owns: `tests/oracles/*`, `tests/test_oracles.py`, and edits to `solver.py`/`SOLVER.md` (P3 is finished
by then, so there is no overlap). Each oracle becomes `<name>.ltp3` + `<name>.expected.json`; each
hypothesis is promoted to CONFIRMED or replaced by what the oracle shows. A hypothesis is never
adjusted to fit one oracle if that breaks another: report the conflict instead.

## 6. Solver hypotheses and oracles

### 6.1 Rules and status

| ID | Rule | Status | Evidence |
|---|---|---|---|
| H1 | Converter, per output: `Pin = Vout·Iout / (NomEff/100)`; part `Pin` = sum over outputs | CONFIRMED | Artix-7 Calculate |
| H2 | Totals: `Pin` = sum at sources, `Pout` = Σ load `V·I`, `Ploss = Pin − Pout`, `Eff = Pout/Pin`; display rounds to 2 dp | CONFIRMED | Artix-7 (88.158 → 88.16) |
| H3 | Source is ideal (no loss, no droop) | CONFIRMED | Artix-7 |
| H4 | Load current = `Ipeak` when `Inom` is absent | CONFIRMED | Artix-7 |
| H5 | When both exist the app uses … `Ipeak`? `Inom`? a user switch? | OPEN — `current_mode="auto"` uses `Ipeak` and reports `A-CURRENT-MODE` | `ORal` headers use `Inom` (FORMAT.md §6), Calculate unknown |
| H6 | `IpeakU`/`InomU` are power-of-ten exponents; absent = amperes | LIKELY | `ORal` prints `mA` for `-3` |
| H7 | LDO: `Iin = Iout`, `Pin = Vin·Iin`, `Ploss = (Vin − Vout)·Iout`, `Vin` = upstream net voltage | OPEN — computed, reported `A-LDO` | none |
| H8 | `Ignd` adds to LDO input current (unit unknown, value `14`) | BLOCKED | 4 parts |
| H9 | Cascade: a converter's `Iin = Pin / Vin_net`; that current loads the upstream output | OPEN — computed, `A-CASCADE` | follows from H1 if power is conserved |
| H10 | Resistor: `Vout = Vin − R·I`, `Ploss = I²R`; loads downstream see the reduced voltage and `Pout` uses it | LIKELY for voltage (`ORal`: 3.287504 V = 3.3 − 2.2 Ω·5.68 mA); OPEN for power — `A-RESISTOR` | 29 templates match with resistors |
| H11 | Converter output with no `NomEff`, `PeakEff` or `MatrixA` (566 of 1004 outputs): default efficiency? | BLOCKED — part `unsupported` | none |
| H12 | `MatrixA` (three coefficients) defines loss or efficiency versus load; `pLossSet`, `EffType`, `PeakEff` select the model | BLOCKED | 50 outputs |
| H13 | Source with only `VoutMin`/`VoutMax` (112 sources) or no keys (47): which voltage feeds LDO maths? | BLOCKED for LDO/resistor paths below such a source; irrelevant for converter-only paths | none |
| H14 | Negative rails: `P = V·I` with both negative gives positive power | LIKELY | storage convention **[C]** |
| H15 | `Net` parts with the same name in a tab are one net | LIKELY — `A-NETLABEL` | 17 of 18 undriven nets contain a Net part |
| H16 | Nets with several drivers (61 nets; 59 are converter+converter): equal current sharing | BLOCKED — net `unsupported` | none |
| H17 | Totals are per tab | LIKELY — `<Summ>` is per tab | structure |
| H18 | Capacitors and `Aux` terminals carry no current | LIKELY | no electrical keys |
| H19 | `VinMin/VinMax/VoutMin/VoutMax/IoutMax/IoutMin/Dropout/PLossMax` are limits: warnings only | LIKELY | names |
| H20 | `LoadType : True` changes the load model | BLOCKED — never seen `True` | 712 × `False` |
| H21 | `ConvType`, `Topology`, `ResType`, `VisType` do not affect power maths | LIKELY | enumerations |

Blocked solver features until oracles arrive: LDO ground current (H8), converters without an
efficiency key (H11), `MatrixA`/`PeakEff` models (H12), range-only sources under LDOs (H13), parallel
drivers (H16), `LoadType` (H20). Features computed but flagged: H5, H7, H9, H10, H15. Corpus impact:
only converters with `NomEff` (387 outputs) are fully confirmed today, so most factory templates
will return `complete=false` until P9.

### 6.2 Oracle trees for the user
For each tree: build it in the GUI with generic names, press Calculate, record **Total Pin, Pout,
Ploss, Efficiency** and any per-block numbers the app shows, and save it as
`tests/oracles/<id>.ltp3`. These are the user's own designs and may be committed. Leave every field
not listed at its default. "Predicted" is what the current hypotheses give; a mismatch is the point.

| ID | Tree | Tests | Predicted Pin / Pout / Ploss / Eff |
|---|---|---|---|
| O1 | Source 5 V → LDO 3.3 V → Load Ipeak 1 A | H7 | 5.00 / 3.30 / 1.70 / 66.00 % |
| O2 | Source 12 V → Buck A 5 V, 90 % → Buck B 1 V, 80 % → Load Ipeak 2 A | H9 | 2.78 / 2.00 / 0.78 / 72.00 % |
| O3 | Source 5 V → Resistor 0.5 Ω → Load Ipeak 2 A | H10 | 10.00 / 8.00 / 2.00 / 80.00 % (if the load keeps 5 V: Pout 10.00) |
| O4 | Source 5 V → Buck 1 V, 80 % → Load Ipeak 3 A **and** Inom 1 A | H5 | peak: 3.75 / 3.00 / 0.75 / 80 %; nominal: 1.25 / 1.00 / 0.25 / 80 %. Note whether the GUI offers a peak/nominal switch and record both. |
| O5 | O4 with the load set to 500 mA peak (unit mA), no Inom | H6, rounding mode | 0.625 / 0.5 / 0.125 exactly: shows 0.63 and 0.13 if half rounds up, 0.62 and 0.12 if half-to-even |
| O6 | Source 12 V → Converter dropped from the palette, only Vout = 5 V set, efficiency untouched → Load Ipeak 1 A | H11 | unknown; the saved file also shows which keys the app writes by default |
| O7 | Source 5 V → two Bucks, both 1 V, one 90 % one 80 %, outputs joined → Load Ipeak 10 A | H16 | equal share: 11.81 / 10.00 / 1.81 / 84.71 % |
| O8 | Source 12 V → wire to a Net label `VBUS`; second Net label `VBUS` → Buck 1 V, 80 % → Load Ipeak 3 A | H15 | 3.75 / 3.00 / 0.75 / 80.00 % |
| O9 | Two tabs: tab 1 = the Artix-7 tree, tab 2 = O1 | H17 | tab 1: 16.67 / 14.70 / 1.97 / 88.16 %; tab 2 as O1 |
| O10 | Source 5 V → Converter −5 V, 80 % → Load −1 A (if the GUI accepts it) | H14 | 6.25 / 5.00 / 1.25 / 80.00 % |
| O11 | Source with only min 9 V / max 24 V (no nominal) → LDO 5 V → Load Ipeak 1 A | H13 | unknown |
| O12 | O1 with an LDO picked from the part database that shows a ground current | H8 | unknown |

Zero-build oracles (open a factory template, Calculate each tab, screenshot the summary; the file
itself is not committed, expected numbers are keyed by template file name):
T1 `Power Tree for AD4000` (MatrixA, resistor, LDO, mA units, several tabs) · T2 `Power Tree for
AD7685` (range-only source under an LDO) · T3 `Arria 10 GX FPGA Power Tree` (NomEff + PeakEff) ·
T4 `48V-14V Bidirectional Polyphase Buck Converter` (parallel drivers, resistors, PLossMax).

Priority if time is short: O1, O2, O3, O4, O6, T1.

### 6.3 GUI acceptance checks (after P4; the app is the only judge of writer hypotheses)
- G1: open a `new_tree` skeleton (`<Vers>`, one `<Tabs>`, one `<Summ>`) with two added parts.
- G2: open the Artix-7 sample after `add_part` + `connect` with a junction split; Calculate works.
- G3: re-save G2 from the GUI and send the file back: the diff against our output shows which of our
  choices (ids, key order, anchors, ZIndex, wire routing) the app rewrites.
Until G1–G3 pass, README labels structural editing as experimental; property edits are not affected.

## 7. Risks, licensing, README

| Risk | Mitigation |
|---|---|
| Format variants outside the corpus (app 3.6.8+ files, user files) | Lossless core keeps unknown tags, keys and line shapes as raw text; `RawLine` fallback; validator warns on unknown items instead of failing; G3 feeds real 3.6.8 output back into tests. |
| Editor writes something the GUI rejects | Minimal-diff edits; canonical key order from the corpus; `h = 60·n` geometry; G1–G3; structural editing flagged experimental until they pass. |
| Solver wrong outside the one confirmed data point | Assumption codes on every unconfirmed rule; blocked rules are not computed; `complete=false`; P9 promotes rules only on oracle evidence. |
| Silent corruption of a user's design | Section 2.5: in-memory edits, validate before save, re-parse check, atomic replace, timestamped backup, new path by default, hash guard, write allow-list. |
| License: ADI forbids reverse engineering the program | Clean-room rule in section 0, repeated in CONTRIBUTING; the spec cites data files only; no ADI binary, template, database or screenshot in the repo; hygiene test in P8. This is engineering practice, not legal advice: get the current EULA text and consider telling ADI about the project before publishing. |
| ADI ships its own MCP or retires LTpowerPlanner for the web Power Studio | Keep the core a dependency-free library with a neutral JSON export; the value that survives is the open reader and solver for existing `.ltp3` archives. |
| Trademark confusion | Name stays descriptive (`ltp3-mcp`), README disclaimer, no ADI logos. |
| `mcp` SDK 2.x API drift | Exact pin, two files import it, spike test first. |
| Git CRLF conversion breaks fixtures | `.gitattributes -text`; per-line `eol` in the model. |
| Tests need a proprietary install | Corpus tests skip cleanly; synthetic and user-built oracle fixtures cover CI. |

Licensing guidance:
- **MIT** for the code. It matches the MCP SDK and the nearest prior art, and the project has no
  patent surface that would justify Apache-2.0's extra terms. `LICENSE` holds the MIT text with the
  author's name.
- README must contain, near the top: "Not affiliated with, endorsed by or supported by Analog
  Devices, Inc. LTpowerPlanner, LTpowerCAD and LTspice are trademarks of Analog Devices. This project
  reads and writes files that you create with your own licensed copy; it contains no Analog Devices
  software, templates or part data."
- README sections: what it does, safety model (section 2.5 in five bullets), install
  (`uvx ltp3-mcp` / `uv tool install`), client configuration snippets, tool table, solver status
  table (confirmed vs assumed vs unsupported), `LTP3_TEMPLATE_DIR` and `LTP3_ALLOWED_DIRS`, how the
  format knowledge was obtained, how to contribute an oracle tree, limitations.
- CONTRIBUTING: the clean-room rule; never attach factory templates to issues; oracle files must be
  the contributor's own designs.

## 8. QA checklist (architect, per phase)

Process
- [ ] Tests were committed before the implementation (check commit order); no test was weakened later.
- [ ] Only files owned by the phase changed (`git diff --stat` against the phase start).
- [ ] The acceptance commands were re-run by the reviewer, not taken from the report.
- [ ] `ruff check`, `ruff format --check`, `mypy --strict src` clean; no `# type: ignore` or `noqa`
      without a reason on the same line.
- [ ] Dependencies unchanged or pinned exactly; `uv.lock` updated in the same commit.

Correctness
- [ ] Corpus tests ran here: the pytest summary shows 0 skipped and the expected parametrised counts
      (261 where stated).
- [ ] Census numbers in tests equal FORMAT.md; any discrepancy was reported, not "fixed" in the test.
- [ ] Round-trip still holds for 261 + 2 files after this phase (`pytest -k "roundtrip"`).
- [ ] Reviewer spot-check: pick three templates not named in any test, run the phase's main function
      by hand, and compare with a manual reading of the file.
- [ ] Error paths raise `Ltp3Error` with a section 4 code; no bare `except`, no swallowed exceptions.
- [ ] No behaviour depends on a hypothesis without an assumption code or an `unsupported` status.

Safety and licence
- [ ] No template text, name list or copied block in tracked files (`git grep` for three distinctive
      `SolN` strings); no file under `samples/` tracked.
- [ ] Nothing reads or references the app's executable, DLLs, `.config` or `.sdf` files.
- [ ] No write outside `tmp_path` in tests; no test modifies the template directory (compare the
      directory's file count and total size before and after: 261 files, 2,433,847 bytes).
- [ ] Core modules import neither `mcp` nor `pydantic`.

Quality
- [ ] Every non-obvious decision has a one-line *why* comment; no comment restates the code.
- [ ] Public functions are typed and documented with units where numbers are physical quantities.
- [ ] No hardcoded absolute path outside `paths.py` defaults; tests use fixtures.
- [ ] Minimal-diff property (from P4 on): a single-property edit on three random templates changes
      exactly one line.
- [ ] The implementer's report lists open questions and deviations from this plan; each one is either
      accepted into the plan or sent back.
