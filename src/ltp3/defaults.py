"""GUI default values of every part type.

LTpowerPlanner omits a property whose value equals the app's default, so a missing key means
"default", not "unknown" (docs/ORACLES.md, "Key finding"). Source for every number below:
docs/ORACLES.md, section "GUI defaults" (fresh parts, LTpowerPlanner III v3.6.8, 2026-11-14),
plus oracles O2 (NomEff 90, Power Vout 12) and O4 (a fresh Resistor does write Resstnce 0).
Older template versions (3.0.3-3.5.4) may use other defaults; docs/deviations/P9.md reports it.

Keys are `(part type, scope, key)`. Scope `Part` is the part's own property, `Input`/`Output`
a terminal property. The values are file text, so they parse like any value read from a file.
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULTS: Mapping[tuple[str, str, str], str] = {
    # Power: ORACLES "Power" row. Typ is the voltage used unless a Min/Max radio says otherwise;
    # no corpus key carries that radio, so Typ is all the solver can use.
    ("Power", "Output", "VoutMin"): "2",
    ("Power", "Output", "Vout"): "12",
    ("Power", "Output", "VoutMax"): "15",
    ("Power", "Output", "IoutMax"): "15",
    # Converter: ORACLES "Converter" row. NomEff is used unless the efficiency radio selects peak.
    ("Converter", "Input", "VinMin"): "4.5",
    ("Converter", "Input", "VinMax"): "20",
    ("Converter", "Output", "Vout"): "2",
    ("Converter", "Output", "IoutMax"): "15",
    ("Converter", "Output", "VoutMin"): "0.6",
    ("Converter", "Output", "VoutMax"): "15",
    ("Converter", "Output", "NomEff"): "90",
    ("Converter", "Output", "PeakEff"): "95",
    # LDO: ORACLES "LDO" row. The GUI label is Vdropout, the file key is Dropout.
    ("LDO", "Input", "VinMin"): "0.8",
    ("LDO", "Input", "VinMax"): "20",
    ("LDO", "Part", "Ignd"): "0",
    ("LDO", "Output", "Vout"): "1",
    ("LDO", "Output", "IoutMax"): "5",
    ("LDO", "Output", "IoutMin"): "0.1",
    ("LDO", "Output", "VoutMin"): "0.6",
    ("LDO", "Output", "VoutMax"): "15",
    ("LDO", "Output", "Dropout"): "0.2",
    # Resistor: ORACLES "Resistor" row. A missing Resstnce is 0 ohm, not "blocked".
    ("Resistor", "Part", "Resstnce"): "0",
    ("Resistor", "Part", "PLossMax"): "2",
    ("Resistor", "Input", "VinMin"): "0.1",
    ("Resistor", "Input", "VinMax"): "500",
    # Load: ORACLES "Load" row. The unit exponent of a fresh load is 0 (amperes).
    ("Load", "Input", "Inom"): "10",
    ("Load", "Input", "Ipeak"): "15",
    ("Load", "Input", "InomU"): "0",
    ("Load", "Input", "IpeakU"): "0",
}


def default_for(part_type: str, scope: str, key: str) -> str | None:
    """The GUI default as file text, or None when the key has no scalar default (MatrixA...)."""
    return DEFAULTS.get((part_type, scope, key))


def lookup(
    props: Mapping[str, str], part_type: str, scope: str, key: str
) -> tuple[str | None, bool]:
    """`(text, defaulted)`: the file's value if present and non-blank, else the GUI default."""
    raw = props.get(key)
    if raw is not None and raw.strip():
        return raw, False
    dflt = default_for(part_type, scope, key)
    return (dflt, True) if dflt is not None else (None, False)
