"""Template directory, write allow-list and the path policy of PLAN.md section 2.5 rule 4."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import BAD_PATH, PATH_NOT_ALLOWED, Ltp3Error

# Default install location of the factory templates; override with LTP3_TEMPLATE_DIR.
DEFAULT_TEMPLATE_DIR = (
    r"C:\Program Files (x86)\LTC\LTpowerCAD2\LTpowerPlanner III\PlannerDataBase\FactorySolutions"
)
ENV_TEMPLATE_DIR = "LTP3_TEMPLATE_DIR"
ENV_ALLOWED_DIRS = "LTP3_ALLOWED_DIRS"


def template_dir() -> Path:
    """Directory holding the `.ltp3t` factory templates (read in place, never copied)."""
    return Path(os.environ.get(ENV_TEMPLATE_DIR) or DEFAULT_TEMPLATE_DIR)


def allowed_roots() -> list[Path]:
    """Directories the server may write under; default is the working directory only."""
    raw = os.environ.get(ENV_ALLOWED_DIRS, "")
    roots = [Path(p).resolve() for p in raw.split(os.pathsep) if p.strip()]
    return roots or [Path.cwd().resolve()]


def _adi_roots() -> list[Path]:
    """Install trees that must never be written to, even if listed as allowed."""
    roots: list[Path] = [template_dir().resolve()]
    # Walk up from the template dir to the vendor root ("LTC") so sibling folders are covered too.
    for parent in template_dir().resolve().parents:
        if parent.name.lower() == "ltc":
            roots.append(parent)
            break
    # Windows only: refuse anything under Program Files, where the install lives
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value).resolve())
    return roots


def _is_within(path: Path, root: Path) -> bool:
    # normcase: Windows paths compare case-insensitively; parents compare components, not prefixes
    p = Path(os.path.normcase(path))
    r = Path(os.path.normcase(root))
    return p == r or r in p.parents


def check_writable(path: str | os.PathLike[str]) -> Path:
    """Return the resolved target if writing is allowed, else raise `Ltp3Error`.

    Rules: only `.ltp3` (templates are read-only), never inside the ADI install tree, and only
    inside `allowed_roots()`.
    """
    target = Path(path).resolve()
    if target.suffix.lower() != ".ltp3":
        raise Ltp3Error(
            BAD_PATH,
            f"only .ltp3 files can be written, got {target.name!r}",
            "Templates (.ltp3t) are read-only; save to a new .ltp3 path.",
        )
    for adi in _adi_roots():
        if _is_within(target, adi):
            raise Ltp3Error(
                PATH_NOT_ALLOWED,
                f"refusing to write inside the installed program tree: {adi}",
                "Choose a path outside Program Files / the LTpowerPlanner install.",
            )
    if not any(_is_within(target, root) for root in allowed_roots()):
        raise Ltp3Error(
            PATH_NOT_ALLOWED,
            f"{target} is outside the allowed directories",
            f"Set {ENV_ALLOWED_DIRS} (os.pathsep-separated) to permit it.",
        )
    return target
