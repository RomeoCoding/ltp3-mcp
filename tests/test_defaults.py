"""P9a: the table of GUI default values (docs/ORACLES.md, "GUI defaults")."""

from __future__ import annotations

import pytest

from ltp3.defaults import DEFAULTS, default_for, lookup
from ltp3.numfmt import parse


@pytest.mark.parametrize(
    ("ptype", "scope", "key", "expected"),
    [
        ("Power", "Output", "Vout", 12),
        ("Power", "Output", "VoutMin", 2),
        ("Power", "Output", "VoutMax", 15),
        ("Converter", "Output", "NomEff", 90),
        ("Converter", "Output", "PeakEff", 95),
        ("Converter", "Output", "Vout", 2),
        ("Converter", "Input", "VinMin", 4.5),
        ("LDO", "Output", "Vout", 1),
        ("LDO", "Output", "Dropout", 0.2),
        ("LDO", "Part", "Ignd", 0),
        ("Resistor", "Part", "Resstnce", 0.001),
        ("Resistor", "Part", "PLossMax", 2),
        ("Load", "Input", "Ipeak", 15),
        ("Load", "Input", "Inom", 10),
    ],
)
def test_default_values(ptype: str, scope: str, key: str, expected: float) -> None:
    raw = default_for(ptype, scope, key)
    assert raw is not None
    assert parse(raw) == expected


def test_unknown_key_has_no_default() -> None:
    assert default_for("Converter", "Output", "MatrixA") is None  # a model, not a value
    assert default_for("Load", "Input", "Nonsense") is None


def test_every_default_parses() -> None:
    for raw in DEFAULTS.values():
        parse(raw)


def test_lookup_prefers_the_file_and_flags_defaults() -> None:
    assert lookup({"Vout": "5"}, "Power", "Output", "Vout") == ("5", False)
    assert lookup({}, "Power", "Output", "Vout") == ("12", True)
    assert lookup({}, "Converter", "Output", "MatrixA") == (None, False)
    assert lookup({"Vout": "  "}, "Power", "Output", "Vout") == ("12", True)
