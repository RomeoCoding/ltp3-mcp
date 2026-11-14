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
    # entries are often pasted with quotes or padding (`"C:\my dir" ; D:\x`); strip both
    cleaned = (p.strip().strip("\"'").strip() for p in raw.split(os.pathsep))
    roots = [Path(p).resolve() for p in cleaned if p]
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
    # ProgramW6432 is the 64-bit Program Files seen from a 32-bit process; the C:\ literals cover
    # an unset or spoofed environment, and ProgramData/Windows hold machine-wide shared state.
    for var in (
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "ProgramData",
        "SystemRoot",
        "windir",
    ):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value).resolve())
    roots.extend(Path(lit).resolve() for lit in (r"C:\Windows", r"C:\ProgramData"))
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
    raw = os.fspath(path)
    # UNC and extended/device prefixes dodge the string-based install-tree check and hit the network
    if raw.startswith(("\\\\", "//")):
        raise Ltp3Error(
            BAD_PATH,
            f"UNC and device paths are not accepted: {raw!r}",
            "Use a local drive path such as C:/Users/you/out.ltp3.",
        )
    # ':' after the drive letter marks an NTFS alternate data stream (`a.ltp3t:x.ltp3`)
    candidate = Path(raw)
    names = candidate.parts[1:] if candidate.anchor else candidate.parts
    if any(":" in part for part in names):
        raise Ltp3Error(
            BAD_PATH,
            f"':' is not allowed in a file or folder name (NTFS stream syntax): {raw!r}",
            "Remove the ':' from the name.",
        )
    target = Path(raw).resolve()
    if str(target).startswith("\\\\"):  # a mapped drive can resolve to a UNC path
        raise Ltp3Error(BAD_PATH, f"UNC and device paths are not accepted: {str(target)!r}")
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
