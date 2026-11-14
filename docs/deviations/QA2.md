# QA2 deviations (findings against P3-P6 and P9a)

Tests are in `tests/test_qa2.py`; they were committed failing before the fixes.

1. Finding 12 and 14 of the QA list were not assigned to this round; nothing was done for them.
2. `FILE_LOCKED` (finding 8) is not in PLAN section 4. `test_scaffold` pins `errors.ALL_CODES` to
   that list, so the code lives in `errors.EXTRA_CODES` and `ALL_CODES` is unchanged. A
   `PermissionError` on `os.replace` or on reading an existing file maps to it; other `OSError`s
   still give `BAD_PATH`.
3. Finding 9: `lossless.parse` stays total (it never raises; an empty document now yields no
   blocks instead of a `ValueError` from `blocks()`). The `PARSE_ERROR` for an empty or tagless
   file is raised by `Tree.__init__`, so `Tree.from_bytes`, `Tree.load` and `safeio.load` all
   report it. A directory gives `NOT_FOUND`.
4. Finding 1: the group that keeps the old netId is the one holding the first wire in file order;
   split-off groups get `max(all ids in the tab) + 1`, `+2`, ... . Two new validation errors
   beyond the plan: `NET_MISMATCH` (was a warning), `DUP_KEY` and `BAD_KEY` (finding 5). The
   whole corpus still validates with 0 errors.
5. Finding 3: an `Aux` terminal is a tap. On a junction the wire end only loses its terminal and
   the junction collapses through the existing `_normalise`; between exactly two bare wires they are
   merged into one (`_merge`, now routing across a gap if the two ends differ). A bare `Aux` stub
   (one wire, no links) is still deleted with the part. Input/Output wires are deleted as before:
   removing a series part (Resistor, Converter) disconnects its load, which is the intent.
6. Finding 5: the `Width` and `Height` guard is wider than the QA text. `Height` is refused on any
   wired part, `Width` on a part with a wire on an `Output` or `Aux` terminal (their anchors move
   with it). Refusing was chosen over re-anchoring because the routes cannot be verified in the GUI.
   Unit exponents accept {0, -3, -6}; the corpus only contains -3 (FORMAT.md 3.2), so 0 and -6 are
   unconfirmed hypotheses (H6). `normalise_value` hosts the range checks, so `add_part` gets them too.
7. Finding 13: `connect` accepts an unwired `Aux` sink (a capacitor on a rail), reusing the Input
   routing. The Aux anchor comes from FORMAT.md 5.1 and the stub-wire shape (one wire with an
   `Aux` end, 19 corpus cases) is the corpus one, but the saved file has not been opened in
   LTpowerPlanner. Treat it as experimental until a GUI round trip (G-test) passes. Net labels
   remain uncreatable.
8. Finding 11: only the forced `current_mode="nominal"` falls back to `Ipeak`. A load whose
   `LoadType : False` radio selects nominal but has no `Inom` still reads the 10 A GUI default
   (omitted key = default, O2/O4).
9. Finding 4: the default of an omitted `Resstnce` changes from 0 to 0.001. `Editor.add_part("Resistor")`
   always writes `Resstnce` (0 when unspecified). Corpus: 382 of 444 tabs complete, unchanged.
10. Pre-existing and left alone: `tests/test_server.py` fails `ruff check` (import order, I001). It
    belongs to the P7 agent on main and was not touched here.
11. Part-removal sweep (not in the suite, 95 s): `remove_part` on all 3904 parts of the 261
    templates returns a document with 0 validation errors every time. The suite's sweep covers the
    403 wired Capacitor/Net parts and also asserts driver->consumer connectivity is unchanged.
