# Deviations from PLAN.md

Each entry: what the plan said, what was done, why.

## P0

1. `requires-python = ">=3.11"` instead of `>=3.10`. The pinned `hypothesis==6.170.15` requires
   3.11, so `uv sync --dev` cannot resolve a 3.10 split. The MCP SDK itself would allow 3.10.
2. No `[project.scripts]` entry yet. `ltp3-mcp = "ltp3.server:main"` belongs to P7 (which owns
   `server.py`); a script pointing at a missing module would be a dangling entry point.
3. `docs/PLAN.md` and `docs/FORMAT.md` were committed unmodified in the first P0 commit so the repo
   carries its own spec. Phase commits are two: failing tests first, then implementation, to keep
   the test-before-implementation order visible (QA checklist, section 8).
4. `check_writable` raises `BAD_PATH` for every non-`.ltp3` target including `.ltp3t` (section 4
   lists `BAD_PATH` for "not .ltp3" and has no code for a read-only extension). It also refuses
   anything under `ProgramFiles` / `ProgramFiles(x86)` in addition to the ADI tree, because P5 needs
   "a path under Program Files" refused.
5. `tests/conftest.py` also provides a `sample_paths` fixture (both samples) because P1's
   `test_roundtrip_samples` needs both and P1 does not own `conftest.py`.
6. No git identity is configured on this machine; commits were made with
   `-c user.name=Claude -c user.email=noreply@anthropic.com`, not written to any git config.

## P1

1. Census: FORMAT.md section 1 gives 25,459 tag lines; the parser finds 25,460. The difference is
   `<Vers>` in the BOM file (`Power Tree for LT1210.ltp3t`), which a `^<` count misses because the
   BOM precedes the `<`. PLAN.md's census list does not include the total; `test_census` asserts
   25,460 with that explanation. All numbers PLAN.md does list match exactly.
2. The `corpus` marker is applied only to `test_roundtrip_corpus`. `-k corpus` also matches marker
   names, so marking `test_census` (or renaming tests to contain "corpus") would make the acceptance
   command select more than the 261 per-template tests.
3. `lossless.py` also exposes `Block`, `Document.blocks()`, `Document.split_tabs()` and
   `split_args()` (PLAN.md 2.1 describes the grouping and split rules but lists no names). Split
   arities used: Tabs 3, Summ 5, BPrt 5, Term 2, Wire 2, Symb 6 (`a,b` and `c,d` are single fields);
   all 25,460-line corpus tag lines of these types split cleanly. `Term` attachment to `BPrt` is left
   to P2's `model.py`.
4. `numfmt.parse` is imported as `parse_number` in tests only to avoid clashing with
   `lossless.parse`; the module function keeps the plan's name `parse`. `fmt` also accepts `int`.
5. Numeric round-trip check: of 46,783 non-text-key values matching a plain numeric pattern,
   0 fail `fmt(parse(v)) == v` (threshold was 99.9 %).
6. Non-UTF-8 input is decoded as `cp1252` with `errors="surrogateescape"` so the four byte values
   cp1252 leaves undefined still round-trip (plain `cp1252` would raise).

## P2

1. Graph API is a `TabGraph` class (`build_graph(tab, merge_net_labels=True)`) with methods `nets`,
   `net_of`, `drivers`, `consumers`, `passives`, `upstream`, `downstream`, `topo_order`, instead of
   free functions taking a tab, so P3's `solve(tab_graph, ...)` receives one object. A free
   `nets(tab)` is also exported. Extra: `unconnected_terminals()`, `dangling`, `warnings`.
2. `netid_partition` / `link_partition` group wire **ids**, not list positions. Tab "12Vin +3.3V
   External 80V" of `Power Tree for ADGM1304.ltp3t` holds two byte-identical wires with id 114; links
   cannot distinguish them, so by-position partitions differ in 1 of 444 tabs. By id, 444/444 agree
   as PLAN.md states. Net building itself unions by position, so both duplicates join the net.
3. `Props.__setitem__` only edits existing keys; a missing key raises `UNKNOWN_KEY`. Inserting a line
   needs `Document.lines` positions and canonical order (P4 editor); `Block.body` is a copy of the
   line list. No identity-based insert helper from lossless.py was needed in P2.
4. `topo_order` includes every part of the tab (isolated Net/Capacitor parts too), ties in file
   order. Cycle check runs on part-level edges from Output to Input terminals.
5. Test correction: the Artix-7 sample has 10 symbols (3 labels + 1 label + 1 rect + 1 label + 4
   rects), not 9 as first written in `test_model.py`; fixed before the implementation commit.
   PLAN.md gives no symbol count.
