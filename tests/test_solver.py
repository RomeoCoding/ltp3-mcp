"""P3: the solver (PLAN.md section 6). Synthetic trees are built as text, then parsed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.graph import build_graph
from ltp3.model import Tab, Tree
from ltp3.solver import SolveOptions, SolveResult, format_oral, solve

CRLF = "\r\n"
ARTIX = "Artix-7 Digilent Basys 3 board power tree.ltp3t"

# (kind, n, props) per terminal
Term = tuple[str, int, dict[str, str]]


def part(
    ptype: str, pid: int, name: str, terms: list[Term], props: dict[str, str] | None = None
) -> list[str]:
    out = [f"<BPrt> {ptype}, {pid}, 0, 0, {name}"]
    out += [f"\t{k:<8}: {v}" for k, v in (props or {}).items()]
    for kind, n, tp in terms:
        out.append(f"<Term> {kind}, {n}")
        out += [f"\t{k:<8}: {v}" for k, v in tp.items()]
    return out


def source(pid: int, vout: str | None = "5", **extra: str) -> list[str]:
    tp = ({"Vout": vout} if vout is not None else {}) | extra
    return part("Power", pid, f"SRC{pid}", [("Output", 1, tp)])


def buck(pid: int, vout: str, eff: str | None = "80", **extra: str) -> list[str]:
    out = {"Vout": vout} | ({"NomEff": eff} if eff is not None else {}) | extra
    return part("Converter", pid, f"BUCK{pid}", [("Input", 1, {}), ("Output", 1, out)])


def ldo(pid: int, vout: str, props: dict[str, str] | None = None) -> list[str]:
    return part("LDO", pid, f"LDO{pid}", [("Input", 1, {}), ("Output", 1, {"Vout": vout})], props)


def resistor(pid: int, ohms: str | None) -> list[str]:
    props = {"Resstnce": ohms} if ohms is not None else None
    return part("Resistor", pid, f"R{pid}", [("Input", 1, {}), ("Output", 1, {})], props)


def load(pid: int, **tp: str) -> list[str]:
    return part("Load", pid, f"LOAD{pid}", [("Input", 1, tp)])


def netlabel(pid: int, name: str) -> list[str]:
    return part("Net", pid, name, [("Aux", 1, {})])


def build(parts: list[list[str]], links: list[tuple[str, str]]) -> Tab:
    """`links` are (from, to) terminal refs 'id:Kind:n'; one wire each. Wires sharing a terminal
    share a net id, so fan-out needs no junction wires."""
    lines = ["<Vers> 3.6.8", "<Tabs> 1, #FFFFFACD, T", "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF"]
    for p in parts:
        lines += p
    nets: dict[str, int] = {}
    for i, (a, b) in enumerate(links):
        net = nets.get(a) or nets.get(b) or 1000 + len(nets)
        nets[a] = nets[b] = net
        lines.append(f"<Wire> {100 + i}, {net}")
        for tag, ref in (("S", a), ("E", b)):
            pid, kind, n = ref.split(":")
            lines += [f"\t{tag}TermPID: {pid}", f"\t{tag}TermTyp: {kind}", f"\t{tag}TermNum: {n}"]
        lines.append("\tPoints  : (0,0)(5,0)")
    return Tree.from_bytes("".join(x + CRLF for x in lines).encode()).tabs[0]


def run(tab: Tab, **opts: Any) -> SolveResult:
    return solve(build_graph(tab), SolveOptions(**opts))


def codes(res: SolveResult) -> list[str]:
    return [a.code for a in res.assumptions]


def by_id(res: SolveResult, pid: int) -> Any:
    return next(p for p in res.parts if p.id == pid)


def chain_5v_buck_load(**load_tp: str) -> Tab:
    return build(
        [source(1, "5"), buck(2, "1", "80"), load(3, **load_tp)],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )


# ---------------------------------------------------------------- Artix-7 oracle (the app)


def test_artix_oracle(template_dir: Path) -> None:
    tab = Tree.load(template_dir / ARTIX).tabs[0]
    res = solve(build_graph(tab))
    pin = 3 / 0.8 + 9.9 / 0.91 + 1.8 / 0.88
    assert res.totals.pin_w == pytest.approx(pin, abs=1e-9)
    assert res.totals.pin_w == pytest.approx(16.674575, abs=1e-6)
    assert res.totals.pout_w == pytest.approx(14.7, abs=1e-9)
    assert res.totals.ploss_w == pytest.approx(pin - 14.7, abs=1e-9)
    assert res.totals.efficiency_pct == pytest.approx(14.7 / pin * 100, abs=1e-9)
    d = res.display
    assert (d.pin_w, d.pout_w, d.ploss_w, d.efficiency_pct) == (16.67, 14.7, 1.97, 88.16)
    assert res.complete is True
    assert codes(res) == ["A-CURRENT-PEAK-ONLY"]
    assert not res.blocked
    # the Artix tree has one converter with two outputs
    buck2 = by_id(res, 2)
    assert buck2.pin_w == pytest.approx(3 / 0.8 + 9.9 / 0.91)
    assert [o.n for o in buck2.outputs] == [1, 2]


def test_result_is_json_serialisable(template_dir: Path) -> None:
    tab = Tree.load(template_dir / ARTIX).tabs[0]
    d = solve(build_graph(tab)).to_dict()
    assert set(d) >= {"complete", "totals", "display", "sources", "parts", "loads", "assumptions"}
    assert json.loads(json.dumps(d)) == d


# ---------------------------------------------------------------- synthetic hypotheses


def test_h1_converter_efficiency_closed_form() -> None:
    res = run(chain_5v_buck_load(Ipeak="3"))
    assert res.complete
    assert res.totals.pin_w == pytest.approx(3.75)
    assert res.totals.pout_w == pytest.approx(3.0)
    assert res.totals.efficiency_pct == pytest.approx(80.0)
    assert codes(res) == ["A-CURRENT-PEAK-ONLY"]


def test_h5_current_modes() -> None:
    tab = chain_5v_buck_load(Ipeak="3", Inom="1")
    peak = run(tab, current_mode="peak")
    nom = run(tab, current_mode="nominal")
    auto = run(tab)
    assert (peak.totals.pin_w, peak.totals.pout_w) == (pytest.approx(3.75), pytest.approx(3.0))
    assert (nom.totals.pin_w, nom.totals.pout_w) == (pytest.approx(1.25), pytest.approx(1.0))
    assert auto.totals.pin_w == pytest.approx(3.75)  # radio absent = the GUI default, IPeak
    assert "A-CURRENT-MODE" not in codes(auto)
    assert "A-CURRENT-MODE" not in codes(peak)
    assert "A-CURRENT-PEAK-ONLY" not in codes(auto)


def test_h5_load_type_false_selects_nominal() -> None:
    # oracle O4: IPeak (default) writes no LoadType, INominal writes `LoadType : False`
    tab = chain_5v_buck_load(Ipeak="3", Inom="1", LoadType="False")
    auto = run(tab)
    assert auto.totals.pout_w == pytest.approx(1.0)
    assert "A-CURRENT-MODE" not in codes(auto)  # confirmed, so not flagged
    assert run(tab, current_mode="peak").totals.pout_w == pytest.approx(3.0)
    assert run(tab, current_mode="nominal").totals.pout_w == pytest.approx(1.0)


def test_oracle_o4_radio_files() -> None:
    fresh = _sample_tab("O4_radio.ltp3", 4)
    nominal = _sample_tab("O4_radio.ltp3", 5)
    pf = next(p for p in fresh.parts if p.type == "Load").terminals[0].props
    pn = next(p for p in nominal.parts if p.type == "Load").terminals[0].props
    assert "LoadType" not in pf and pn["LoadType"] == "False"


def test_load_current_defaults() -> None:
    # radio = IPeak, Ipeak omitted: the GUI default is 15 A
    res = run(chain_5v_buck_load(Inom="1"))
    assert res.complete
    assert res.totals.pout_w == pytest.approx(15.0)
    note = next(a for a in res.assumptions if a.code == "A-DEFAULT" and "Ipeak" in a.message)
    assert note.parts == [3]
    # radio = INominal, Inom omitted: 10 A
    res = run(chain_5v_buck_load(Ipeak="1", LoadType="False"))
    assert res.totals.pout_w == pytest.approx(10.0)
    assert any(a.code == "A-DEFAULT" and "Inom" in a.message for a in res.assumptions)


def test_h6_unit_exponent_milliamps_and_display_rounding() -> None:
    tab = chain_5v_buck_load(Ipeak="500", IpeakU="-3")
    up = run(tab)
    assert up.totals.pin_w == pytest.approx(0.625)
    assert up.totals.pout_w == pytest.approx(0.5)
    # 0.625 and 0.125 are exact in binary: half-up shows 0.63 / 0.13, half-even 0.62 / 0.12
    assert (up.display.pin_w, up.display.ploss_w) == (0.63, 0.13)
    ev = run(tab, rounding="half_even")
    assert (ev.display.pin_w, ev.display.ploss_w) == (0.62, 0.12)


def test_h7_ldo() -> None:
    tab = build(
        [source(1, "5"), ldo(2, "3.3"), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert (res.totals.pin_w, res.totals.pout_w) == (pytest.approx(5.0), pytest.approx(3.3))
    assert res.totals.ploss_w == pytest.approx(1.7)
    assert res.totals.efficiency_pct == pytest.approx(66.0)
    assert "A-LDO" in codes(res)
    assert by_id(res, 2).ploss_w == pytest.approx(1.7)


def test_h9_cascade() -> None:
    tab = build(
        [source(1, "12"), buck(2, "5", "90"), buck(3, "1", "80"), load(4, Ipeak="2")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1"), ("3:Output:1", "4:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(2 / 0.8 / 0.9)
    assert res.totals.pout_w == pytest.approx(2.0)
    assert res.display.pin_w == 2.78
    assert "A-CASCADE" in codes(res)
    assert by_id(res, 2).outputs[0].iout == pytest.approx(2.5 / 5)  # B loads A's output
    assert by_id(res, 3).iin == pytest.approx(2.5 / 5)


def test_h10_resistor_drop_and_power() -> None:
    tab = build(
        [source(1, "5"), resistor(2, "0.5"), load(3, Ipeak="2")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(10.0)
    assert res.totals.pout_w == pytest.approx(8.0)  # load sees 5 - 0.5*2 = 4 V
    assert res.totals.ploss_w == pytest.approx(2.0)
    assert res.loads[0].v == pytest.approx(4.0)
    assert "A-RESISTOR" in codes(res)


def test_h10_resistor_without_value_is_zero_ohm() -> None:
    tab = build(
        [source(1, "5"), resistor(2, None), load(3, Ipeak="2")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.ploss_w == pytest.approx(0.0)
    assert res.loads[0].v == pytest.approx(5.0)
    assert any(a.code == "A-DEFAULT" and "Resstnce" in a.message for a in res.assumptions)
    assert not any(b.code == "B-NO-RESISTANCE" for b in res.blocked)


def test_resistor_feeding_converter_converges() -> None:
    # Iin of the converter depends on the voltage after the drop, which depends on Iin
    tab = build(
        [source(1, "5"), resistor(2, "0.1"), buck(3, "1", "100"), load(4, Ipeak="2")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1"), ("3:Output:1", "4:Input:1")],
    )
    res = run(tab)
    i = by_id(res, 2).iin
    # P = 2 W drawn at V = 5 - 0.1 i  ->  i (5 - 0.1 i) = 2
    assert i * (5 - 0.1 * i) == pytest.approx(2.0, rel=1e-9)
    assert res.totals.pin_w == pytest.approx(5 * i)


def test_h11_missing_efficiency_is_the_default_90() -> None:
    tab = build(
        [source(1, "12"), buck(2, "5", None), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(5 / 0.9)
    note = next(a for a in res.assumptions if a.code == "A-DEFAULT" and "NomEff" in a.message)
    assert note.parts == [2]
    assert by_id(res, 2).status == "assumed"
    assert not any(b.code == "B-NO-EFFICIENCY" for b in res.blocked)


def test_peak_efficiency_with_eff_type_true() -> None:
    # hypothesis: EffType True is the non-default radio position, Eff Peak (default 95 %)
    for extra, eff in (({"EffType": "True"}, 0.95), ({"EffType": "True", "PeakEff": "80"}, 0.8)):
        tab = build(
            [source(1, "12"), buck(2, "5", "70", **extra), load(3, Ipeak="1")],
            [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
        )
        res = run(tab)
        assert res.complete
        assert res.totals.pin_w == pytest.approx(5 / eff)
        assert "A-EFF-RADIO" in codes(res)


def test_peak_eff_next_to_nom_eff_uses_nom_eff() -> None:
    # the radio decides, not the presence of PeakEff (ORACLES.md); no EffType means NomEff
    tab = build(
        [source(1, "12"), buck(2, "5", "70", PeakEff="99"), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(5 / 0.7)


def test_defaults_for_source_and_ldo_voltage() -> None:
    tab = build(
        [source(1, None), ldo(2, "3"), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(12.0)  # Vout omitted = 12 V (Typ)
    assert any(a.code == "A-DEFAULT" and "Vout" in a.message for a in res.assumptions)
    ldo_no_vout = part("LDO", 2, "L", [("Input", 1, {}), ("Output", 1, {})])
    tab = build(
        [source(1, "5"), ldo_no_vout, load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pout_w == pytest.approx(1.0)  # LDO Vout omitted = 1 V


@pytest.mark.parametrize(
    "extra",
    [{"MatrixA": "0.49, 1.59, 0.35"}, {"pLossSet": "1"}],
)
def test_h12_efficiency_models_blocked(extra: dict[str, str]) -> None:
    tab = build(
        [source(1, "12"), buck(2, "5", "90", **extra), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert not res.complete
    assert by_id(res, 2).status == "unsupported"
    assert any(b.code == "B-EFFICIENCY-MODEL" for b in res.blocked)


def test_h8_ldo_ground_current_blocked() -> None:
    tab = build(
        [source(1, "5"), ldo(2, "3.3", {"Ignd": "14"}), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert not res.complete
    assert by_id(res, 2).status == "unsupported"
    assert any(b.code == "B-LDO-GROUND-CURRENT" for b in res.blocked)


def test_h13_range_only_source() -> None:
    rng = source(1, None, VoutMin="9", VoutMax="24")
    under_ldo = build(
        [rng, ldo(2, "5"), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(under_ldo)
    # no radio key exists in the corpus, so the voltage is Typ = the omitted default, 12 V
    assert res.complete and res.totals.pin_w == pytest.approx(1 * 12)
    assert any(a.code == "A-DEFAULT" and "Vout" in a.message for a in res.assumptions)
    # a converter path does not need the source voltage: Pin = Pout / eff
    under_buck = build(
        [rng, buck(2, "5", "80"), load(3, Ipeak="1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    ok = run(under_buck)
    assert ok.complete
    assert ok.totals.pin_w == pytest.approx(6.25)


def test_h14_negative_rail() -> None:
    tab = build(
        [source(1, "5"), buck(2, "-5", "80"), load(3, Ipeak="-1")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pin_w == pytest.approx(6.25)
    assert res.totals.pout_w == pytest.approx(5.0)
    assert res.totals.efficiency_pct == pytest.approx(80.0)


def test_h15_net_labels_merge() -> None:
    parts = [
        source(1, "12"),
        netlabel(2, "VBUS"),
        netlabel(3, "VBUS"),
        buck(4, "1", "80"),
        load(5, Ipeak="3"),
    ]
    links = [("1:Output:1", "2:Aux:1"), ("3:Aux:1", "4:Input:1"), ("4:Output:1", "5:Input:1")]
    tab = build(parts, links)
    res = solve(build_graph(tab))
    assert res.complete and res.totals.pin_w == pytest.approx(3.75)
    assert "A-NETLABEL" in codes(res)


def test_h16_multiple_drivers_blocked() -> None:
    tab = build(
        [source(1, "5"), buck(2, "1", "90"), buck(3, "1", "80"), load(4, Ipeak="10")],
        [
            ("1:Output:1", "2:Input:1"),
            ("1:Output:1", "3:Input:1"),
            ("2:Output:1", "4:Input:1"),
            ("3:Output:1", "4:Input:1"),
        ],
    )
    res = run(tab)
    assert not res.complete
    assert any(b.code == "B-MULTI-DRIVER" for b in res.blocked)
    assert by_id(res, 2).status == by_id(res, 3).status == "unsupported"


def test_h20_unknown_load_type_blocked() -> None:
    res = run(chain_5v_buck_load(Ipeak="1", LoadType="Maybe"))
    assert not res.complete
    assert any(b.code == "B-LOAD-TYPE" for b in res.blocked)


def test_h19_limits_are_warnings_only() -> None:
    conv = part(
        "Converter",
        2,
        "B",
        [
            ("Input", 1, {"VinMax": "4"}),
            ("Output", 1, {"Vout": "1", "NomEff": "80", "IoutMax": "1"}),
        ],
    )
    tab = build(
        [source(1, "5"), conv, load(3, Ipeak="3")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab)
    assert res.complete
    assert {"W-VIN-RANGE", "W-IOUT-MAX"} <= {w.code for w in res.warnings}


def test_unconnected_load_draws_nothing() -> None:
    tab = build(
        [source(1, "5"), load(2, Ipeak="1"), load(3, Ipeak="1")], [("1:Output:1", "2:Input:1")]
    )
    res = run(tab)
    assert res.complete
    assert res.totals.pout_w == pytest.approx(5.0)
    assert any(w.code == "W-UNCONNECTED" for w in res.warnings)


def test_undriven_input_is_unsupported() -> None:
    tab = build([buck(1, "1", "80"), load(2, Ipeak="1")], [("1:Output:1", "2:Input:1")])
    res = run(tab)  # the converter input floats: its Pin cannot be charged to any source
    assert not res.complete
    assert by_id(res, 1).status == "unsupported"
    assert any(b.code == "B-UNDRIVEN" for b in res.blocked)
    assert res.loads[0].v == pytest.approx(1.0)  # the load side is still known


def test_empty_tab_and_cycle() -> None:
    empty = run(build([], []))
    assert empty.complete and empty.totals.pin_w == 0 and empty.totals.efficiency_pct is None
    loop = build(
        [buck(1, "1", "80"), buck(2, "1", "80")],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "1:Input:1")],
    )
    with pytest.raises(Ltp3Error) as exc:
        run(loop)
    assert exc.value.code == errors.CYCLE


def test_format_oral() -> None:
    tab = build(
        [
            source(1, "3.3"),
            resistor(2, "2.2"),
            load(3, Inom="5.4", InomU="-3", Ipeak="9", IpeakU="-3"),
        ],
        [("1:Output:1", "2:Input:1"), ("2:Output:1", "3:Input:1")],
    )
    res = run(tab, current_mode="nominal")
    assert format_oral(res) == ["3.28812V/5.4mA"]


# ---------------------------------------------------------------- oracles O1-O3 (the app)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _sample_tab(name: str, index: int) -> Tab:
    path = SAMPLES / name
    if not path.is_file():
        pytest.skip(f"{path} is absent")
    tabs = Tree.load(path).tabs
    if index >= len(tabs):
        pytest.skip(f"{name} has no tab {index + 1}")
    return tabs[index]


def _display(res: SolveResult) -> tuple[float, float, float | None, float | None]:
    d = res.display
    return (d.pin_w, d.pout_w, d.ploss_w, d.efficiency_pct)


def test_oracle_o1_ldo() -> None:
    res = solve(build_graph(_sample_tab("O2_ldo.ltp3", 1)))
    assert res.complete
    assert _display(res) == (5.0, 3.3, 1.7, 66.0)
    assert by_id(res, 3).ploss_w == pytest.approx(1.7)


def test_oracle_o2_cascade_with_omitted_defaults() -> None:
    # converter 2 (90 %) and the 12 V source have no NomEff / Vout line in the file
    res = solve(build_graph(_sample_tab("O2_ldo.ltp3", 2)))
    assert res.complete
    assert _display(res) == (2.78, 2.0, 0.78, 72.0)
    assert by_id(res, 3).ploss_w == pytest.approx(0.5, abs=5e-3)
    assert by_id(res, 2).ploss_w == pytest.approx(0.28, abs=5e-3)
    msgs = {a.message for a in res.assumptions if a.code == "A-DEFAULT"}
    assert any("NomEff" in m for m in msgs) and any("Vout" in m for m in msgs)


def test_oracle_o3_resistor() -> None:
    res = solve(build_graph(_sample_tab("O4_radio.ltp3", 3)))
    assert res.complete
    assert _display(res) == (10.0, 8.0, 2.0, 80.0)
    assert res.loads[0].v == pytest.approx(4.0)


# ---------------------------------------------------------------- corpus


def test_corpus_smoke_and_statistics(template_paths: list[Path]) -> None:
    complete = incomplete = cycles = n = 0
    reasons: dict[str, int] = {}
    for p in template_paths:
        for tab in Tree.from_bytes(p.read_bytes()).tabs:
            n += 1
            try:
                res = solve(build_graph(tab))
            except Ltp3Error as exc:
                assert exc.code == errors.CYCLE, p.name
                cycles += 1
                continue
            json.dumps(res.to_dict())
            if res.complete:
                complete += 1
            else:
                incomplete += 1
                for code in {b.code for b in res.blocked}:
                    reasons[code] = reasons.get(code, 0) + 1
    print(f"tabs={n} complete={complete} incomplete={incomplete} cycles={cycles} {reasons}")
    assert n == 444
    assert complete + incomplete + cycles == 444
    assert complete > 0


def test_oral_header_agreement(template_paths: list[Path]) -> None:
    agree = comparable = 0
    for p in template_paths:
        tree = Tree.from_bytes(p.read_bytes())
        header = next(
            (b.head.args for b in tree.header if b.head.tag == "ORal"),
            None,
        )
        if header is None:
            continue
        comparable += 1
        got: list[str] = []
        for tab in tree.tabs:
            try:
                res = solve(build_graph(tab), SolveOptions(current_mode="nominal"))
            except Ltp3Error:
                continue
            got += format_oral(res)
        if ", ".join(got) == header.strip():
            agree += 1
    print(f"ORal agreement: {agree}/{comparable}")
    assert agree >= 150
