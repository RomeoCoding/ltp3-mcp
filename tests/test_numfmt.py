from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from ltp3 import errors
from ltp3.errors import Ltp3Error
from ltp3.lossless import PropLine, parse
from ltp3.numfmt import fmt
from ltp3.numfmt import parse as parse_number

# keys whose values are free text or enumerations, not .NET-formatted doubles
_TEXT_KEYS = {"PartNumb", "Comments", "Title", "CadPath", "Dash"}
_NUMERIC = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (5.0, "5"),
        (3.3, "3.3"),
        (1e-05, "1E-05"),
        (5.5e-05, "5.5E-05"),
        (0.0001, "0.0001"),
        (2e-08, "2E-08"),
        (0.272727272727273, "0.272727272727273"),
        (-5.0, "-5"),
        (-0.0, "0"),
        (0.0, "0"),
        (100.0, "100"),
        (123456789012345.0, "123456789012345"),
        (1e15, "1E+15"),
        (1.5e20, "1.5E+20"),
        (1e-100, "1E-100"),
        (0.5, "0.5"),
        (-3.287504, "-3.287504"),
        (89.5, "89.5"),
        (0.4893703488, "0.4893703488"),
    ],
)
def test_fmt_known(value: float, text: str) -> None:
    assert fmt(value) == text


def test_fmt_int_and_special() -> None:
    assert fmt(7) == "7"
    assert fmt(-12) == "-12"
    assert fmt(math.nan) == "NaN"
    assert fmt(math.inf) == "Infinity"
    assert fmt(-math.inf) == "-Infinity"


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("5", 5.0),
        ("-5", -5.0),
        ("3.3", 3.3),
        ("1E-05", 1e-05),
        ("5.5E-05", 5.5e-05),
        ("1E+15", 1e15),
        ("1e-5", 1e-05),
        (".5", 0.5),
        ("+2", 2.0),
        ("0.272727272727273", 0.272727272727273),
    ],
)
def test_parse_accepts(text: str, value: float) -> None:
    assert parse_number(text) == value


def test_parse_special() -> None:
    assert math.isnan(parse_number("NaN"))
    assert parse_number("Infinity") == math.inf
    assert parse_number("-Infinity") == -math.inf


@pytest.mark.parametrize("text", ["", " 5", "5 ", "abc", "1,5", "0x10", "1e", "--1", "5V", "#FF"])
def test_parse_rejects(text: str) -> None:
    with pytest.raises(Ltp3Error) as ei:
        parse_number(text)
    assert ei.value.code == errors.BAD_VALUE


@pytest.mark.parametrize("value", [0.1, 0.2 + 0.1, 1 / 3, 2.5e-07, 12345.678, 1e-05, 9.99e14])
def test_fmt_parse_roundtrip_float(value: float) -> None:
    # .NET Framework 4 keeps 15 significant digits, so the round trip is exact only to ~1e-15
    assert math.isclose(parse_number(fmt(value)), value, rel_tol=1e-14)
    assert fmt(parse_number(fmt(value))) == fmt(value)  # text is a fixed point


def test_numeric_values_in_templates_survive_fmt_parse(template_paths: list[Path]) -> None:
    total = 0
    exceptions: list[str] = []
    for p in template_paths:
        for line in parse(p.read_bytes()).lines:
            if not isinstance(line, PropLine) or line.key in _TEXT_KEYS:
                continue
            if _NUMERIC.fullmatch(line.value) is None:
                continue
            total += 1
            if fmt(parse_number(line.value)) != line.value:
                exceptions.append(f"{p.name}: {line.key}={line.value!r}")
    assert total > 1000
    ratio = 1 - len(exceptions) / total
    shown = "\n".join(exceptions[:40])
    assert ratio >= 0.999, f"{len(exceptions)}/{total} numeric values do not round-trip:\n{shown}"
    if exceptions:
        print(f"numfmt exceptions ({len(exceptions)}/{total}):\n{shown}")


# Vectors measured in PowerShell on .NET Framework 4 (CLR 4.0.30319): 15 significant digits.
@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.1 + 0.2, "0.3"),
        (1 / 3, "0.333333333333333"),
        (3.3 - 2.2 * 0.00568, "3.287504"),
        (1.2345678901234568e17, "1.23456789012346E+17"),
        (1e-7, "1E-07"),
        (3.30, "3.3"),
        (-(0.1 + 0.2), "-0.3"),
        (2 / 3, "0.666666666666667"),
        (999999999999999.9, "1E+15"),  # rounds up into the scientific range
        (0.99999999999999999, "1"),
    ],
)
def test_fmt_dotnet_framework_15_digits(value: float, text: str) -> None:
    assert fmt(value) == text


def test_fmt_negative_zero_is_plain_zero() -> None:
    assert fmt(-0.0) == "0"


@pytest.mark.parametrize("value", [True, False])
def test_fmt_rejects_bool(value: bool) -> None:
    with pytest.raises(Ltp3Error) as ei:
        fmt(value)
    assert ei.value.code == errors.BAD_VALUE
