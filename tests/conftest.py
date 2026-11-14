"""Shared fixtures. Corpus tests read ADI templates in place and skip when they are absent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Mirrors ltp3.paths.DEFAULT_TEMPLATE_DIR; duplicated so conftest works even if the library breaks.
_DEFAULT_TEMPLATE_DIR = (
    r"C:\Program Files (x86)\LTC\LTpowerCAD2\LTpowerPlanner III\PlannerDataBase\FactorySolutions"
)
_SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_SAMPLE_NAMES = ("artix7_original.ltp3", "artix7_resaved.ltp3")


def _template_dir() -> Path:
    return Path(os.environ.get("LTP3_TEMPLATE_DIR", _DEFAULT_TEMPLATE_DIR))


def corpus_names() -> list[str]:
    """Collection-time listing so parametrised ids carry the template name."""
    d = _template_dir()
    return sorted(p.name for p in d.glob("*.ltp3t")) if d.is_dir() else []


@pytest.fixture(scope="session")
def template_dir() -> Path:
    d = _template_dir()
    if not d.is_dir():
        pytest.skip(f"template directory not found: {d} (set LTP3_TEMPLATE_DIR)")
    if not any(d.glob("*.ltp3t")):
        pytest.skip(f"no .ltp3t files in {d}")
    return d


@pytest.fixture(scope="session")
def template_paths(template_dir: Path) -> list[Path]:
    found = sorted(template_dir.glob("*.ltp3t"))
    if not found:
        pytest.skip(f"no .ltp3t files in {template_dir}")
    return found


@pytest.fixture(scope="session")
def sample_path() -> Path:
    p = _SAMPLES / _SAMPLE_NAMES[0]
    if not p.is_file():
        pytest.skip(f"sample not found: {p}")
    return p


@pytest.fixture(scope="session")
def sample_paths() -> list[Path]:
    ps = [_SAMPLES / n for n in _SAMPLE_NAMES]
    if not all(p.is_file() for p in ps):
        pytest.skip(f"samples not found in {_SAMPLES}")
    return ps
