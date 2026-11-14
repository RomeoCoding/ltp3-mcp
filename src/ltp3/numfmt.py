"""Number formatting and parsing that matches the .NET double text the app writes.

The app runs on .NET Framework 4 (CLR 4.0.30319), where `Double.ToString()` keeps 15 significant
digits (not the shortest round-trip form of .NET Core 3.0+).

Examples: 5.0 -> "5", 1e-05 -> "1E-05", 5.5e-05 -> "5.5E-05", 0.0001 -> "0.0001".
"""

from __future__ import annotations

import math
import re
from decimal import Decimal

from .errors import BAD_VALUE, Ltp3Error

# .NET switches to scientific notation below 1E-05 and from 1E+15 upwards
_SCI_LOW = -5
_SCI_HIGH = 15

_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def fmt(value: float) -> str:
    """Format like .NET Framework 4 `double.ToString()`: 15 significant digits, trailing zeros
    stripped, `E+XX`/`E-XX` exponents."""
    # bool is an int subclass; True would silently become "True"/1, which is never a number here
    if isinstance(value, bool):
        raise Ltp3Error(BAD_VALUE, f"not a number: {value!r}")
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"  # .NET Framework prints negative zero as "0"
    sign = "-" if value < 0 else ""
    # 15 significant digits (1 + 14 after the point), correctly rounded: what .NET Framework 4 keeps
    _, digit_tuple, exp = Decimal(f"{abs(value):.14e}").as_tuple()
    assert isinstance(exp, int)  # finite values only
    all_digits = "".join(map(str, digit_tuple))
    sci_exp = len(all_digits) + exp - 1
    digits = all_digits.rstrip("0")
    if _SCI_LOW < sci_exp < _SCI_HIGH:
        if sci_exp >= 0:
            whole = digits[: sci_exp + 1].ljust(sci_exp + 1, "0")
            frac = digits[sci_exp + 1 :]
            return f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}"
        return f"{sign}0.{'0' * (-sci_exp - 1)}{digits}"
    mantissa = digits[0] + (f".{digits[1:]}" if len(digits) > 1 else "")
    return f"{sign}{mantissa}E{'-' if sci_exp < 0 else '+'}{abs(sci_exp):02d}"


def parse(text: str) -> float:
    """Parse a stored number (integer, decimal or `1E-05`-style); raise BAD_VALUE otherwise."""
    if text == "NaN":
        return math.nan
    if text in {"Infinity", "-Infinity"}:
        return math.inf if text[0] != "-" else -math.inf
    if _NUMBER.fullmatch(text) is None:
        raise Ltp3Error(BAD_VALUE, f"not a number: {text!r}")
    return float(text)
