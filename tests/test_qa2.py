"""QA round 2: regressions for the findings against P3-P6 and P9a (docs/deviations/QA2.md).

Corpus tests read the ADI templates in place (conftest default path) and never copy them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ltp3 import errors
from ltp3.defaults import default_for
from ltp3.editor import Editor
from ltp3.errors import Ltp3Error
from ltp3.graph import build_graph, link_partition, netid_partition
from ltp3.lossless import parse
from ltp3.model import Tab, Tree
from ltp3.numfmt import parse as parse_number
from ltp3.safeio import load, save
from ltp3.solver import SolveOptions, solve
from ltp3.validate import validate

from test_solver import build, buck, chain_5v_buck_load, ldo, source
from test_solver import load as mk_load


@pytest.fixture(autouse=True)
def _allow_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTP3_ALLOWED_DIRS", str(tmp_path))


@pytest.fixture
def sample(sample_path: Path) -> bytes:
    return sample_path.read_bytes()


def _template(template_dir: Path, fragment: str) -> bytes:
    hits = sorted(template_dir.glob(f"*{fragment}*.ltp3t"))
    assert hits, fragment
    return hits[0].read_bytes()


def _find_part(data: bytes, ptype: str, pid: int) -> int:
    """Index of the tab holding part `pid` of type `ptype`."""
    for i, tab in enumerate(Tree.from_bytes(data).tabs):
        if any(p.id == pid and p.type == ptype for p in tab.parts):
            return i
    raise AssertionError(f"no {ptype} {pid}")


def _pairs(tab: Tab) -> set[tuple[str, str]]:
    """Driver -> consumer terminal pairs, label merging off so a removed Net label cannot count."""
    g = build_graph(tab, merge_net_labels=False)
    return {(d.ref, c.ref) for n in g.nets for d in g.drivers(n) for c in g.consumers(n)}


def _errors(data: bytes) -> list[str]:
    return [f"{i.code}: {i.message}" for i in validate(parse(data)).errors]


# ---------------------------------------------------------------- 1. stale netId after removal


def test_t1023_remove_label_keeps_netids_consistent(template_dir: Path) -> None:
    data = _template(template_dir, "T1023")
    ti = _find_part(data, "Net", 168)
    ed = Editor.from_bytes(data)
    ed.remove_part(168, tab=ti)
    tab = Tree.from_bytes(ed.serialize()).tabs[ti]
    # a netId must name exactly one link-connected group
    assert netid_partition(tab) == link_partition(tab)
    assert _errors(ed.serialize()) == []


def test_netid_link_mismatch_is_a_validation_error(sample: bytes) -> None:
    tree = Tree.from_bytes(sample)
    tab = tree.tabs[0]
    w = next(w for w in tab.wires if w.start_links)
    other = next(o for o in tab.wires if o.id == w.start_links[0])
    other.block.head.args = f"{other.id}, 987654"  # linked wires now disagree on the net id
    report = validate(tree)
    assert any(i.code == "NET_MISMATCH" for i in report.errors)


# ---------------------------------------------------------------- 2. bare AssertionError


def test_ad5674_remove_part_70(template_dir: Path) -> None:
    data = _template(template_dir, "AD5674")
    ed = Editor.from_bytes(data)
    ed.remove_part(70, tab=0)
    assert _errors(ed.serialize()) == []


@pytest.mark.corpus
def test_fuzz_remove_every_wired_capacitor_and_net(template_paths: list[Path]) -> None:
    """Every wired Capacitor/Net removal: success with 0 errors, consistent nets and unchanged
    driver->consumer connectivity; or a clean Ltp3Error. Never any other exception."""
    done = clean_errors = 0
    failures: list[str] = []
    for path in template_paths:
        data = path.read_bytes()
        tree = Tree.from_bytes(data)
        for ti, tab in enumerate(tree.tabs):
            for p in tab.parts:
                if p.type not in ("Capacitor", "Net"):
                    continue
                if not any(
                    e is not None and e[0] == p.id for w in tab.wires for e in (w.start, w.end)
                ):
                    continue
                before = _pairs(tab)
                ed = Editor.from_bytes(data)
                tag = f"{path.name} tab {ti} part {p.id}"
                try:
                    ed.remove_part(p.id, tab=ti)
                except Ltp3Error:
                    clean_errors += 1
                    continue
                except Exception as exc:  # noqa: BLE001 - the point of the test
                    failures.append(f"{tag}: {type(exc).__name__} {exc}")
                    continue
                out = ed.serialize()
                errs = _errors(out)
                after_tab = Tree.from_bytes(out).tabs[ti]
                if errs:
                    failures.append(f"{tag}: validate {errs[:2]}")
                elif netid_partition(after_tab) != link_partition(after_tab):
                    failures.append(f"{tag}: netId/link partitions differ")
                elif _pairs(after_tab) != before:
                    failures.append(f"{tag}: connectivity changed")
                done += 1
    print(f"remove sweep: {done} removed cleanly, {clean_errors} clean Ltp3Error")
    assert not failures, f"{len(failures)} failures, e.g. {failures[:5]}"
    assert done > 300


# ---------------------------------------------------------------- 3. in-line capacitor


def test_inline_capacitor_removal_keeps_load_connected(template_dir: Path) -> None:
    data = _template(template_dir, "Communication Power Tree Example 1")
    ti = next(
        i
        for i, t in enumerate(Tree.from_bytes(data).tabs)
        if any(p.type == "Capacitor" and p.name == "C1" for p in t.parts)
    )
    tab = Tree.from_bytes(data).tabs[ti]
    c1 = next(p for p in tab.parts if p.name == "C1")
    assert ("2:Output:1", "4:Input:1") in _pairs(tab)
    ed = Editor.from_bytes(data)
    ed.remove_part(c1.id, tab=ti)
    after = Tree.from_bytes(ed.serialize()).tabs[ti]
    assert ("2:Output:1", "4:Input:1") in _pairs(after)
    assert _errors(ed.serialize()) == []


# ---------------------------------------------------------------- 4. Resstnce


def test_omitted_resistance_is_one_milliohm() -> None:
    raw = default_for("Resistor", "Part", "Resstnce")
    assert raw is not None
    assert parse_number(raw) == 0.001


def test_resistor_default_reproduces_app_load_voltages(template_dir: Path) -> None:
    """ORal of two v3.0.3 templates: 1.188 V at 12 A and 0.99761 V at 2.39 A (R = 1 mohm)."""
    chain_file = template_dir / "Communication Power Chain Example .ltp3t"
    chain = Tree.from_bytes(chain_file.read_bytes()).tabs[0]
    res = solve(build_graph(chain))
    v = next(ld.v for ld in res.loads if ld.terminal == "52:Input:1")
    assert v == pytest.approx(1.188, abs=1e-9)
    pfga = Tree.from_bytes(_template(template_dir, "Generic PFGA")).tabs[0]
    res = solve(build_graph(pfga))
    v = next(ld.v for ld in res.loads if ld.terminal == "4:Input:1")
    assert v == pytest.approx(0.99761, abs=1e-9)


def test_add_resistor_always_writes_resstnce(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    r0 = ed.add_part("Resistor", "R0")
    r1 = ed.add_part("Resistor", "R1", resistance=0.5)
    tab = Tree.from_bytes(ed.serialize()).tabs[0]
    assert tab.part(r0.id).props["Resstnce"] == "0"  # v3.6.8 writes Resstnce: 0 explicitly
    assert tab.part(r1.id).props["Resstnce"] == "0.5"


# ---------------------------------------------------------------- 5. set_property hardening


@pytest.mark.parametrize("key", ["", "a b", "toolongkey", "1abc", "a:b", "ke\ty"])
def test_set_property_rejects_malformed_keys(sample: bytes, key: str) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property("11", key, 1, allow_unknown_key=True)
    assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


def test_set_property_strips_key_whitespace(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("11", " Comments ", "x")
    assert Tree.from_bytes(ed.serialize()).tabs[0].part("11").props["Comments"] == "x"


@pytest.mark.parametrize("key", ["Width", "Height", "Color"])
def test_set_property_protects_base_keys(sample: bytes, key: str) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property("11", key, None)
    assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        ("11", "Width", 0),
        ("11", "Width", -10),
        ("11", "Height", 0),
        ("16:Output:1", "NomEff", 0),
        ("16:Output:1", "NomEff", 101),
        ("16:Output:1", "NomEff", -5),
        ("16:Output:1", "PeakEff", 100.5),
        ("11:Input:1", "InomU", 2),
        ("11:Input:1", "IpeakU", -4),
    ],
)
def test_set_property_range_checks(sample: bytes, target: str, key: str, value: float) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property(target, key, value, allow_unknown_key=True)
    assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample


def test_set_property_accepts_valid_extremes(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    ed.set_property("16:Output:1", "NomEff", 100)
    for exp in (0, -3, -6):
        ed.set_property("11:Input:1", "IpeakU", exp)


def test_height_change_on_wired_part_rejected(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    tab = Tree.from_bytes(sample).tabs[0]
    wired = next(p for p in tab.parts if p.type == "Converter")
    with pytest.raises(Ltp3Error) as exc:
        ed.set_property(str(wired.id), "Height", 400)
    assert exc.value.code == errors.BAD_VALUE
    # an unwired part may change
    fresh = ed.add_part("Load", "L")
    ed.set_property(str(fresh.id), "Height", 200)


def test_duplicate_key_in_block_is_a_validation_error(sample: bytes) -> None:
    doc = parse(sample)
    tab = Tree(doc).tabs[0]
    block = tab.parts[0].block
    line = block.props[0]
    dup = type(line)(line.key_raw, line.sep, line.value, line.eol)
    doc.insert_after(line, dup)
    codes = [i.code for i in validate(doc).errors]
    assert "DUP_KEY" in codes


# ---------------------------------------------------------------- 6. stale tree in save


def test_save_validates_the_document_not_a_stale_tree(sample: bytes, tmp_path: Path) -> None:
    ed = Editor.from_bytes(sample)
    ed.add_part("Power", "P2")
    ed.add_part("Load", "L2")
    tree = Tree.from_document(ed.doc)  # built now ...
    src, dst = tree.tabs[0].parts[-2].id, tree.tabs[0].parts[-1].id
    ed.connect(f"{src}:Output:1", f"{dst}:Input:1")
    wire = Tree.from_document(ed.doc).tabs[0].wires[-1]
    wire.block.find("Points").value = "(0,0)(5,5)"  # ... then a diagonal wire appears
    with pytest.raises(Ltp3Error) as exc:
        save(tree, tmp_path / "out.ltp3")
    assert exc.value.code == errors.VALIDATION_FAILED
    assert not (tmp_path / "out.ltp3").exists()


# ---------------------------------------------------------------- 7. unit exponent on Ipeak


def test_ipeak_unit_exponent_is_flagged() -> None:
    tab = chain_5v_buck_load(Ipeak="500", IpeakU="-3")
    res = solve(build_graph(tab))
    assert res.loads[0].i == pytest.approx(0.5)
    assert any(a.code == "A-UNIT-EXP-PEAK" for a in res.assumptions)


def test_no_unit_exponent_no_flag() -> None:
    res = solve(build_graph(chain_5v_buck_load(Ipeak="1")))
    assert not any(a.code == "A-UNIT-EXP-PEAK" for a in res.assumptions)


# ---------------------------------------------------------------- 8. locked target


def test_locked_target_gives_file_locked_and_no_orphan_backup(
    sample: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "p.ltp3"
    save(parse(sample), target)
    real = os.replace

    def locked(src: object, dst: object) -> None:
        if str(dst) == str(target):
            raise PermissionError(13, "The process cannot access the file")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(Ltp3Error) as exc:
        save(parse(sample), target, overwrite=True)
    assert exc.value.code == errors.FILE_LOCKED
    assert "close the file in LTpowerPlanner" in exc.value.hint
    assert sorted(p.name for p in tmp_path.iterdir()) == ["p.ltp3"]  # no .bak, no .tmp


# ---------------------------------------------------------------- 9. unreadable inputs


def test_empty_and_tagless_files_are_parse_errors(tmp_path: Path) -> None:
    for name, data in (("empty.ltp3", b""), ("tagless.ltp3", b"hello\r\nworld\r\n")):
        p = tmp_path / name
        p.write_bytes(data)
        with pytest.raises(Ltp3Error) as exc:
            load(p)
        assert exc.value.code == errors.PARSE_ERROR


def test_directory_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as exc:
        load(tmp_path)
    assert exc.value.code == errors.NOT_FOUND


def test_parse_of_empty_bytes_does_not_crash() -> None:
    assert parse(b"").blocks() == []


# ---------------------------------------------------------------- 10/11. solver


def test_explicit_zero_ignd_does_not_block_ldo() -> None:
    tab = build(
        [source(1, "5"), ldo(2, "3.3", {"Ignd": "0"}), mk_load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = solve(build_graph(tab))
    assert res.complete
    assert not any(b.code == "B-LDO-GROUND-CURRENT" for b in res.blocked)


def test_nonzero_ignd_still_blocks_ldo() -> None:
    tab = build(
        [source(1, "5"), ldo(2, "3.3", {"Ignd": "14"}), mk_load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = solve(build_graph(tab))
    assert any(b.code == "B-LDO-GROUND-CURRENT" for b in res.blocked)


def test_forced_nominal_falls_back_to_ipeak_when_inom_missing() -> None:
    tab = build(
        [source(1, "5"), buck(2, "1", "80"), mk_load(3, Ipeak="3")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = solve(build_graph(tab), SolveOptions(current_mode="nominal"))
    assert res.loads[0].i == pytest.approx(3.0)  # not the invented 10 A default
    assert any(a.code == "A-NOMINAL-FALLBACK" for a in res.assumptions)


def test_forced_nominal_with_inom_is_unflagged() -> None:
    tab = build(
        [source(1, "5"), buck(2, "1", "80"), mk_load(3, Ipeak="3", Inom="2")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = solve(build_graph(tab), SolveOptions(current_mode="nominal"))
    assert res.loads[0].i == pytest.approx(2.0)
    assert not any(a.code == "A-NOMINAL-FALLBACK" for a in res.assumptions)


# ---------------------------------------------------------------- 13. capacitor wiring


def test_capacitor_on_a_rail(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    tab = Tree.from_bytes(sample).tabs[0]
    drv = next(p for p in tab.parts if p.type == "Converter")
    cap = ed.add_part("Capacitor", "C9")
    res = ed.connect(f"{drv.id}:Output:1", f"{cap.id}:Aux:1")
    out = ed.serialize()
    assert res.wires_added
    assert _errors(out) == []
    after = Tree.from_bytes(out).tabs[0]
    g = build_graph(after)
    assert any(t.ref == f"{cap.id}:Aux:1" for n in g.nets for t in g.passives(n))
    # the cap hangs on the rail without disturbing who drives whom
    assert _pairs(after) == _pairs(Tree.from_bytes(sample).tabs[0])
    # a second wire on the same Aux terminal is refused
    with pytest.raises(Ltp3Error) as exc:
        ed.connect(f"{drv.id}:Output:1", f"{cap.id}:Aux:1")
    assert exc.value.code == errors.TERMINAL_IN_USE


# ---------------------------------------------------------------- 15. unit_exp without current


def test_unit_exp_without_current_is_rejected(sample: bytes) -> None:
    ed = Editor.from_bytes(sample)
    with pytest.raises(Ltp3Error) as exc:
        ed.add_part("Load", "L", inputs=[{"unit_exp": -3}])
    assert exc.value.code == errors.BAD_VALUE
    assert ed.serialize() == sample
