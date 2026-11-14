"""Template catalogue. Reads the factory templates in place (read-only); nothing is copied."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .errors import NOT_FOUND, Ltp3Error
from .model import Tree
from .paths import template_dir

# Header tags the catalogue exposes, as (field, tag). Templates carry all of them; a saved
# project carries only Vers, so every lookup defaults to "".
_FIELDS = (
    ("group", "Grop"),
    ("vendor", "Vend"),
    ("device", "Dvce"),
    ("solution", "SolN"),
    ("description", "Desc"),
    ("input_rails", "IRal"),
    ("output_rails", "ORal"),
    ("part_numbers", "PPtN"),
    ("date", "Date"),
    ("format_version", "Vers"),
)


@dataclass(frozen=True, slots=True)
class CatalogItem:
    file: str
    path: str
    name: str
    group: str
    vendor: str
    device: str
    solution: str
    description: str
    input_rails: str
    output_rails: str
    part_numbers: str
    date: str
    format_version: str
    tabs: list[str]

    def to_dict(self) -> dict[str, object]:
        # Explicit order = the documented, stable key order of the catalogue JSON.
        return {
            "file": self.file,
            "path": self.path,
            "name": self.name,
            "group": self.group,
            "vendor": self.vendor,
            "device": self.device,
            "solution": self.solution,
            "description": self.description,
            "input_rails": self.input_rails,
            "output_rails": self.output_rails,
            "part_numbers": self.part_numbers,
            "date": self.date,
            "format_version": self.format_version,
            "tabs": list(self.tabs),
        }

    def _haystack(self) -> str:
        bits = [self.file, self.group, self.vendor, self.device, self.solution, self.description]
        bits += [self.input_rails, self.output_rails, self.part_numbers, *self.tabs]
        return "\n".join(bits).lower()


def catalog_item(path: str | Path) -> CatalogItem:
    p = Path(path)
    tree = Tree.load(p)
    header = {b.head.tag: b.head.args.strip() for b in tree.header}
    values = {field: header.get(tag, "") for field, tag in _FIELDS}
    return CatalogItem(
        file=p.name,
        path=str(p),
        name=p.stem,
        tabs=[t.name for t in tree.tabs],
        **values,
    )


def _dir(directory: str | Path | None) -> Path:
    d = Path(directory) if directory is not None else template_dir()
    if not d.is_dir():
        raise Ltp3Error(NOT_FOUND, f"template directory not found: {d}", "Set LTP3_TEMPLATE_DIR.")
    return d


def _all(directory: str | Path | None) -> list[CatalogItem]:
    d = _dir(directory)
    # Case-insensitive then exact name: stable order that does not depend on the filesystem.
    files = sorted(d.glob("*.ltp3t"), key=lambda p: (p.name.lower(), p.name))
    return [catalog_item(p) for p in files]


def list_templates(
    vendor: str | None = None,
    group: str | None = None,
    text: str | None = None,
    directory: str | Path | None = None,
) -> list[CatalogItem]:
    """Templates sorted by file name. `vendor`/`group` match exactly (case-insensitive); every
    whitespace-separated term of `text` must occur in some field (AND)."""
    items = _all(directory)
    if vendor is not None:
        items = [i for i in items if i.vendor.lower() == vendor.strip().lower()]
    if group is not None:
        items = [i for i in items if i.group.lower() == group.strip().lower()]
    if text:
        terms = text.lower().split()
        items = [i for i in items if all(t in i._haystack() for t in terms)]
    return items


def vendor_counts(directory: str | Path | None = None) -> dict[str, int]:
    counts = Counter(i.vendor for i in _all(directory))
    # Count descending, then name, so the output order is deterministic.
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
