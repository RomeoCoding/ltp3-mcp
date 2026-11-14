"""P5: validation. Synthetic negatives are written inline; the corpus is read in place."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from conftest import corpus_names
from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.lossless import parse
from ltp3.model import Tree
from ltp3.validate import Issue, Report, validate

CRLF = "\r\n"


def _text(*lines: str) -> bytes:
    return "".join(line + CRLF for line in lines).encode()


def part(
    kind: str, pid: int, terms: list[tuple[str, list[str]]], x: int = 0, name: str = "P"
) -> list[str]:
    out = [
        f"<BPrt> {kind}, {pid}, {x}, 0, {name}",
        "\tColor   : #00FFFFFF",
        "\tWidth   : 100",
        "\tHeight  : 60",
    ]
    counts: Counter[str] = Counter()
    for term, props in terms:
        counts[term] += 1  # numbering restarts per kind (FORMAT.md 3.2)
        out.append(f"<Term> {term}, {counts[term]}")
        out += [f"\t{k:<8}: {v}" for k, v in (p.split("=", 1) for p in props)]
    return out


def wire(
    wid: int,
    net: int,
    s: str | None,
    e: str | None,
    sw: str = "",
    ew: str = "",
    pts: str = "(0,0)(5,0)",
) -> list[str]:
    out = [f"<Wire> {wid}, {net}"]
    for tag, end in (("S", s), ("E", e)):
        if end:
            pid, typ, num = end.split(":")
            out += [f"\t{tag}TermPID: {pid}", f"\t{tag}TermTyp: {typ}", f"\t{tag}TermNum: {num}"]
    if sw:
        out.append(f"\tSWires  : {sw}")
    if ew:
        out.append(f"\tEWires  : {ew}")
    out.append(f"\tPoints  : {pts}")
    return out


POWER = part("Power", 1, [("Output", ["Vout=12", "VoutMin=11", "VoutMax=13"])])
CONV = part(
    "Converter",
    2,
    [
        ("Input", ["VinMin=4", "VinMax=20"]),
        ("Output", ["Vout=5", "VoutMin=4.5", "VoutMax=5.5", "IoutMax=2"]),
    ],
    x=200,
    name="C",
)
LOAD = part("Load", 3, [("Input", ["Ipeak=1", "Inom=0.5"])], x=400, name="L")
W_PC = wire(10, 10, "1:Output:1", "2:Input:1", pts="(100,30)(200,30)")
W_CL = wire(11, 11, "2:Output:1", "3:Input:1", pts="(300,30)(400,30)")


def tab(*chunks: list[str], name: str = "T", tab_id: int = 1) -> list[str]:
    head = [f"<Tabs> {tab_id}, #FFFFFACD, {name}", "<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF"]
    return head + [line for c in chunks for line in c]


def doc(*chunks: list[str], header: list[str] | None = None) -> Tree:
    lines = (header if header is not None else ["<Vers> 3.6.8"]) + tab(*chunks)
    return Tree.from_bytes(_text(*lines))


def good() -> Tree:
    return doc(POWER, CONV, LOAD, W_PC, W_CL)


def codes(report: Report, severity: str) -> set[str]:
    return {i.code for i in report.issues if i.severity == severity}


def test_good_tree_has_no_issues() -> None:
    report = validate(good())
    assert report.issues == []
    assert report.ok


def test_issue_and_report_shape() -> None:
    report = validate(doc(POWER, CONV, LOAD, wire(10, 10, "1:Output:1", "2:Input:9")))
    issue = report.errors[0]
    assert isinstance(issue, Issue)
    assert issue.severity == "error"
    assert issue.tab == 0
    assert not report.ok
    with pytest.raises(Ltp3Error) as exc:
        report.raise_if_errors()
    assert exc.value.code == errors.VALIDATION_FAILED
    validate(good()).raise_if_errors()


def test_validate_accepts_document() -> None:
    assert validate(parse(_text("<Vers> 3.6.8", *tab(POWER)))).ok


# ----------------------------------------------------------------------------- errors
ERROR_CASES: dict[str, Tree] = {
    "DANGLING_TERMINAL": doc(POWER, CONV, wire(10, 10, "1:Output:1", "2:Input:9")),
    "DANGLING_WIRE": doc(POWER, CONV, wire(10, 10, "1:Output:1", "2:Input:1", sw="99")),
    "DUP_PART_ID": doc(POWER, part("Load", 1, [("Input", [])], x=400)),
    "WIRE_END_EMPTY": doc(POWER, CONV, wire(10, 10, "1:Output:1", None)),
    "NON_CLIQUE_JUNCTION": doc(
        POWER,
        CONV,
        LOAD,
        wire(10, 10, "1:Output:1", None, ew="11, 12", pts="(100,30)(150,30)"),
        wire(11, 10, "2:Input:1", None, sw="10, 12", pts="(150,30)(200,30)"),
        # wire 12 forgets to list wire 11 at the junction
        wire(12, 10, "3:Input:1", None, sw="10", pts="(150,30)(400,30)"),
    ),
    "JUNCTION_COORD": doc(
        POWER,
        CONV,
        LOAD,
        wire(10, 10, "1:Output:1", None, ew="11, 12", pts="(100,30)(150,30)"),
        wire(11, 10, "2:Input:1", None, sw="10, 12", pts="(150,30)(200,30)"),
        wire(12, 10, "3:Input:1", None, sw="10, 11", pts="(155,30)(400,30)"),
    ),
    "DIAGONAL": doc(POWER, CONV, wire(10, 10, "1:Output:1", "2:Input:1", pts="(100,30)(200,40)")),
    "TERMINAL_MULTI_WIRE": doc(
        POWER,
        CONV,
        LOAD,
        W_PC,
        wire(12, 12, "1:Output:1", "3:Input:1", pts="(100,30)(400,30)"),
    ),
    "BAD_NUMBER": doc(part("Power", 1, [("Output", ["Vout=abc"])])),
    "TAB_ORDER": doc(POWER, ["<Symb> Label, 1, 0, 0, 0,0, 0,0", "\tComments: hi"], CONV),
    "BAD_POINTS": doc(POWER, CONV, wire(10, 10, "1:Output:1", "2:Input:1", pts="(100,30)(x,y)")),
    # a non-integer part id: the model refuses the number, validation must report it
    "BAD_ARGS": doc([line.replace("Power, 1,", "Power, one,") for line in POWER]),
    # QA2 #1: a netId that disagrees with the links is an error, not a warning
    "NET_MISMATCH": doc(
        POWER,
        CONV,
        LOAD,
        wire(10, 10, "1:Output:1", None, ew="11", pts="(100,30)(150,30)"),
        wire(11, 99, "2:Input:1", None, sw="10", pts="(150,30)(200,30)"),
    ),
}


@pytest.mark.parametrize("code", sorted(ERROR_CASES))
def test_error_code(code: str) -> None:
    report = validate(ERROR_CASES[code])
    assert code in codes(report, "error"), report.issues


def test_linebreak_in_tag_line() -> None:
    tree = good()
    tree.tabs[0].part(2).block.head.args += "\nrogue"  # bypass the setters on purpose
    assert "LINEBREAK" in codes(validate(tree), "error")


def test_structure_error_is_reported_not_raised() -> None:
    # a <Term> before any <BPrt> cannot be modelled; validate must report it, not raise
    data = _text("<Vers> 3.6.8", *tab(["<Term> Input, 1"]))
    assert "STRUCTURE" in codes(validate(parse(data)), "error")


def test_error_has_tab_index() -> None:
    bad = tab(POWER, CONV, wire(10, 10, "1:Output:1", "2:Input:9"), tab_id=2)
    report = validate(Tree.from_bytes(_text("<Vers> 3.6.8", *tab(POWER), *bad)))
    assert {i.tab for i in report.errors} == {1}


# --------------------------------------------------------------------------- warnings
WARN_CASES: dict[str, Tree] = {
    "MULTI_DRIVER": doc(
        POWER,
        part("Power", 4, [("Output", ["Vout=12"])]),
        part("Capacitor", 5, [("Aux", [])]),
        CONV,
        wire(10, 10, "1:Output:1", "2:Input:1"),
        wire(13, 10, "4:Output:1", "5:Aux:1"),
    ),
    "UNDRIVEN_CONSUMER": doc(
        LOAD, part("Capacitor", 5, [("Aux", [])]), wire(10, 10, "3:Input:1", "5:Aux:1")
    ),
    "UNCONNECTED_TERMINAL": doc(POWER, CONV, LOAD, W_PC),
    "CYCLE": doc(
        CONV,
        part("Converter", 4, [("Input", []), ("Output", ["Vout=5"])], x=300),
        wire(10, 10, "2:Output:1", "4:Input:1"),
        wire(11, 11, "4:Output:1", "2:Input:1"),
    ),
    "VOUT_RANGE": doc(part("Power", 1, [("Output", ["Vout=12", "VoutMin=8", "VoutMax=10"])])),
    "VIN_RANGE": doc(
        part("Power", 1, [("Output", ["Vout=12"])]),
        part("Converter", 2, [("Input", ["VinMin=4", "VinMax=10"])], x=200),
        wire(10, 10, "1:Output:1", "2:Input:1"),
    ),
    "IOUT_OVER": doc(
        part("Converter", 2, [("Input", []), ("Output", ["Vout=5", "IoutMax=1"])]),
        part("Load", 3, [("Input", ["Ipeak=1.5"])], x=400),
        wire(11, 11, "2:Output:1", "3:Input:1"),
    ),
    "DUP_WIRE_ID": doc(POWER, CONV, W_PC, wire(10, 10, "2:Output:1", None, pts="(1,1)(2,1)")),
    "UNKNOWN_TAG": doc(POWER, ["<Zzzz> 1, 2"]),
    "UNKNOWN_KEY": doc([*POWER, "\tBogus   : 1"]),
}


@pytest.mark.parametrize("code", sorted(WARN_CASES))
def test_warning_code(code: str) -> None:
    report = validate(WARN_CASES[code])
    assert code in codes(report, "warning"), report.issues


def test_scaled_current_units() -> None:
    # Ipeak 1500 with IpeakU -3 is 1.5 A: over a 1 A limit, within a 2 A one
    def build(limit: int) -> Tree:
        return doc(
            part("Converter", 2, [("Input", []), ("Output", [f"IoutMax={limit}"])]),
            part("Load", 3, [("Input", ["Ipeak=1500", "IpeakU=-3"])], x=400),
            wire(11, 11, "2:Output:1", "3:Input:1"),
        )

    assert "IOUT_OVER" in codes(validate(build(1)), "warning")
    assert "IOUT_OVER" not in codes(validate(build(2)), "warning")


def test_duplicate_wire_id_is_warning_not_error() -> None:
    report = validate(WARN_CASES["DUP_WIRE_ID"])
    assert "DUP_WIRE_ID" in codes(report, "warning")
    assert "DUP_WIRE_ID" not in codes(report, "error")


def test_known_header_and_ds_tags_are_not_unknown() -> None:
    header = ["<Vers> 3.3.4", "<Grop> g", "<Note> ", "<ORal> 1V/1A", "<Date> 7/24/2020 12:00:00 AM"]
    tree = doc(POWER, ["<DS11>0.272727272727273", "<DS05>"], header=header)
    assert "UNKNOWN_TAG" not in codes(validate(tree), "warning")


# ----------------------------------------------------------------------------- corpus
@pytest.mark.corpus
@pytest.mark.parametrize("name", corpus_names())
def test_corpus_validates_without_errors(name: str, template_dir: Path) -> None:
    report = validate(Tree.load(template_dir / name))
    assert report.errors == [], [(i.code, i.message) for i in report.errors[:5]]


@pytest.mark.corpus
def test_corpus_warning_histogram(
    template_paths: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    hist: Counter[str] = Counter()
    for p in template_paths:
        hist.update(f"{i.severity}:{i.code}" for i in validate(Tree.load(p)).issues)
    with capsys.disabled():
        print("\ncorpus validation histogram:", dict(sorted(hist.items())))
    assert not [k for k in hist if k.startswith("error:")]


def test_samples_validate_without_errors(sample_paths: list[Path]) -> None:
    for p in sample_paths:
        assert validate(Tree.load(p)).errors == []
