"""P6: JSON export of the electrical graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from ltp3.export import SCHEMA_VERSION, export_tree, to_json
from ltp3.model import Tree

CRLF = "\r\n"

# Schema defined here on purpose: the test, not the implementation, is the contract.
_PROPS = {"type": "object", "additionalProperties": {"type": "string"}}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema", "schema_version", "include_layout", "header", "tabs"],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": "ltp3-export"},
        "schema_version": {"const": 1},
        "include_layout": {"type": "boolean"},
        "header": _PROPS,
        "tabs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "id", "name", "parts", "nets"],
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer"},
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "color": {"type": "string"},
                    "symbols": {"type": "array"},
                    "wires": {"type": "array"},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "type", "name", "props", "terminals"],
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "integer"},
                                "type": {"type": "string"},
                                "name": {"type": "string"},
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "props": _PROPS,
                                "results": {
                                    "type": "object",
                                    "additionalProperties": {"type": "number"},
                                },
                                "terminals": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["ref", "kind", "n", "props", "net"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "ref": {"type": "string"},
                                            "kind": {"enum": ["Input", "Output", "Aux"]},
                                            "n": {"type": "integer"},
                                            "props": _PROPS,
                                            "net": {"type": ["integer", "null"]},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "nets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["id", "terminals", "labels"],
                            "additionalProperties": False,
                            "properties": {
                                "id": {"type": "integer"},
                                "terminals": {"type": "array", "items": {"type": "string"}},
                                "labels": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        },
    },
}

_DOC = [
    "<Vers> 3.3.4",
    "<Vend> ADI",
    "<Tabs> 1, #FFFFFFFF, Main",
    "<Summ> 0, 0, 12, #FF000000, #FFFFFFFF",
    "<BPrt> Power, 2, 100, 100, 5V, 12V",
    "\tColor   : #FF000000",
    "\tWidth   : 60",
    "\tHeight  : 60",
    "<Term> Output, 1",
    "\tVout    : 5",
    "<BPrt> Load, 1, 300, 100, Load, A",
    "\tColor   : #FF000000",
    "\tWidth   : 60",
    "\tHeight  : 60",
    "<Term> Input, 1",
    "\tIpeak   : 1",
    "<Wire> 3, 7",
    "\tSTermPID: 2",
    "\tSTermTyp: Output",
    "\tSTermNum: 1",
    "\tETermPID: 1",
    "\tETermTyp: Input",
    "\tETermNum: 1",
    "\tPoints  : (160,130)(300,130)",
]


def _tree() -> Tree:
    return Tree.from_bytes("".join(x + CRLF for x in _DOC).encode())


def _validate(doc: dict[str, Any]) -> None:
    jsonschema.validate(doc, SCHEMA)


def test_schema_and_content() -> None:
    doc = export_tree(_tree())
    _validate(doc)
    assert doc["schema_version"] == SCHEMA_VERSION == 1
    tab = doc["tabs"][0]
    assert [p["id"] for p in tab["parts"]] == [1, 2]  # sorted by id
    load = tab["parts"][0]
    assert load["name"] == "Load, A"  # comma inside a name survives
    assert load["terminals"][0]["props"] == {"Ipeak": "1"}
    assert tab["nets"] == [{"id": 0, "terminals": ["1:Input:1", "2:Output:1"], "labels": []}]
    assert load["terminals"][0]["net"] == 0


def test_no_layout_by_default() -> None:
    doc = export_tree(_tree())
    tab = doc["tabs"][0]
    assert "wires" not in tab and "symbols" not in tab and "color" not in tab
    for p in tab["parts"]:
        assert "x" not in p and "y" not in p
        assert not {"Color", "Width", "Height"} & set(p["props"])
    assert doc["include_layout"] is False


def test_layout_on_request() -> None:
    doc = export_tree(_tree(), include_layout=True)
    _validate(doc)
    p = doc["tabs"][0]["parts"][0]
    assert (p["x"], p["y"]) == (300, 100)
    assert p["props"]["Width"] == "60"
    assert doc["tabs"][0]["wires"][0]["points"] == [[160, 130], [300, 130]]


def test_deterministic_text() -> None:
    a = to_json(_tree())
    b = to_json(_tree())
    assert a == b
    assert json.loads(a) == export_tree(_tree())
    assert a.endswith("\n")


def test_results_mapping_attached() -> None:
    results = {(0, 1): {"pin_w": 5.0, "pout_w": 0.0}}
    doc = export_tree(_tree(), results=results)
    _validate(doc)
    parts = {p["id"]: p for p in doc["tabs"][0]["parts"]}
    assert parts[1]["results"] == {"pin_w": 5.0, "pout_w": 0.0}
    assert "results" not in parts[2]


def test_corpus_exports_validate_and_are_deterministic(template_paths: list[Path]) -> None:
    # Every 10th template keeps the run short; the schema check is per-file independent.
    for p in template_paths[::10]:
        tree = Tree.load(p)
        _validate(export_tree(tree))
        _validate(export_tree(tree, include_layout=True))
        assert to_json(tree) == to_json(Tree.load(p)), p.name


def test_whole_corpus_exports(template_paths: list[Path]) -> None:
    n = sum(len(export_tree(Tree.load(p))["tabs"]) for p in template_paths)
    assert n == 444
