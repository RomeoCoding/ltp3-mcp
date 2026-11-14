"""P6: template catalogue (reads in place, never copies)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ltp3.catalog import CatalogItem, catalog_item, list_templates, vendor_counts
from ltp3.errors import Ltp3Error

CRLF = "\r\n"


def _write(path: Path, header: dict[str, str]) -> None:
    lines = ["<Vers> 3.3.4"] + [f"<{k}> {v}" for k, v in header.items()]
    lines += ["<Tabs> 1, #FFFFFFFF, Main", "<Summ> 0, 0, 12, #FF000000, #FFFFFFFF"]
    path.write_bytes("".join(x + CRLF for x in lines).encode())


@pytest.fixture
def small_dir(tmp_path: Path) -> Path:
    _write(
        tmp_path / "a.ltp3t",
        {
            "Grop": "FPGA",
            "Vend": "Xilinx",
            "Dvce": "Artix-7",
            "SolN": "Basys board",
            "Desc": "Digilent",
            "PPtN": "LTM4644",
        },
    )
    _write(
        tmp_path / "b.ltp3t",
        {
            "Grop": "Industrial",
            "Vend": "ADI",
            "Dvce": "AD4000",
            "SolN": "ADC supply",
            "Desc": "precision",
            "PPtN": "LT3042",
        },
    )
    _write(
        tmp_path / "c.ltp3t",
        {
            "Grop": "Industrial",
            "Vend": "ADI",
            "Dvce": "ADG1304",
            "SolN": "Mux",
            "Desc": "x",
            "PPtN": "LT3042",
        },
    )
    (tmp_path / "ignored.txt").write_text("not a template")
    return tmp_path


def test_synthetic_listing_sorted(small_dir: Path) -> None:
    items = list_templates(directory=small_dir)
    assert [i.file for i in items] == ["a.ltp3t", "b.ltp3t", "c.ltp3t"]
    assert items[0].vendor == "Xilinx" and items[0].device == "Artix-7"
    assert items[0].tabs == ["Main"]


def test_synthetic_filters(small_dir: Path) -> None:
    assert [i.file for i in list_templates(vendor="adi", directory=small_dir)] == [
        "b.ltp3t",
        "c.ltp3t",
    ]
    assert [i.file for i in list_templates(group="FPGA", directory=small_dir)] == ["a.ltp3t"]
    # every whitespace-separated term must match somewhere (AND), case-insensitively
    assert [i.file for i in list_templates(text="lt3042 PRECISION", directory=small_dir)] == [
        "b.ltp3t"
    ]
    assert list_templates(text="nothing-matches-this", directory=small_dir) == []


def test_vendor_counts_synthetic(small_dir: Path) -> None:
    assert vendor_counts(directory=small_dir) == {"ADI": 2, "Xilinx": 1}


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(Ltp3Error) as exc:
        list_templates(directory=tmp_path / "nope")
    assert exc.value.code == "NOT_FOUND"


def test_item_to_dict_has_stable_keys(small_dir: Path) -> None:
    d = catalog_item(small_dir / "a.ltp3t").to_dict()
    assert list(d) == [
        "file", "path", "name", "group", "vendor", "device", "solution", "description",
        "input_rails", "output_rails", "part_numbers", "date", "format_version", "tabs",
    ]  # fmt: skip
    assert d["tabs"] == ["Main"]


def test_listing_does_not_modify_or_copy(small_dir: Path) -> None:
    def digest() -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in small_dir.iterdir()}

    before = digest()
    list_templates(directory=small_dir)
    assert before == digest()


# ---------------------------------------------------------------- corpus


def test_corpus_261_items(template_dir: Path) -> None:
    items = list_templates()
    assert len(items) == 261
    assert all(isinstance(i, CatalogItem) for i in items)
    assert all(Path(i.path).parent == template_dir for i in items)  # read in place


def test_corpus_vendor_counts(template_dir: Path) -> None:
    counts = vendor_counts()
    assert counts["ADI"] == 237
    assert counts["Xilinx"] == 7
    assert sum(counts.values()) == 261
    assert len(list_templates(vendor="ADI")) == 237
    assert len(list_templates(vendor="Xilinx")) == 7


def test_corpus_free_text(template_dir: Path) -> None:
    hits = list_templates(text="artix")
    assert any("Artix-7" in i.file for i in hits)
    assert len(hits) < 261


def test_repo_contains_no_templates() -> None:
    root = Path(__file__).resolve().parent.parent
    skip = {".venv", ".git", "samples"}
    stray = [p for p in root.rglob("*.ltp3t") if not skip.intersection(p.relative_to(root).parts)]
    assert stray == []
