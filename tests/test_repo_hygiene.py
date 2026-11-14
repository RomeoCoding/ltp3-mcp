"""P8: the repository must hold no ADI content and no personal paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SELF = "tests/test_repo_hygiene.py"


def _tracked() -> list[str]:
    try:
        r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return r.stdout.splitlines()


def test_no_adi_files_tracked() -> None:
    bad = [f for f in _tracked() if f.endswith(".ltp3t") or f.startswith("samples/")]
    assert bad == []


def test_no_ltp3_project_files_tracked() -> None:
    # Save-As copies of an ADI template count as ADI content; tests build trees as text
    assert [f for f in _tracked() if f.endswith(".ltp3")] == []


def test_no_personal_paths_outside_license() -> None:
    hits = []
    for f in _tracked():
        if f in ("LICENSE", _SELF):
            continue
        text = (ROOT / f).read_bytes().decode("utf-8", "replace")
        if "Romeo Mattar" in text or "ROMEOM~1" in text:
            hits.append(f)
    assert hits == []


def test_template_headers_not_in_tracked_files(template_paths: list[Path]) -> None:
    from ltp3.catalog import catalog_item

    needles = set()
    for p in template_paths:
        item = catalog_item(p)
        for s in (item.solution, item.description):
            if len(s) >= 25:  # short strings collide with ordinary words
                needles.add(s)
    blob = ""
    for f in _tracked():
        if f.startswith("tests/") or f.endswith(".lock"):
            continue
        blob += (ROOT / f).read_bytes().decode("utf-8", "replace")
    assert [n for n in needles if n in blob] == []
