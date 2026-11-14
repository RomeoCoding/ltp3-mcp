"""Terminal anchors, part placement and orthogonal wire routing (PLAN.md 2.4, FORMAT.md 5.1).

Pure geometry over the typed views: nothing here edits a Document.
"""

from __future__ import annotations

import itertools
import math
import re
from typing import Literal

from .model import Part, Tab, Terminal

GRID = 5
PITCH = 60  # terminal pitch; part Height = 60 * terminals keeps both observed y rules equal

# Corpus modes (PLAN.md 2.4).
DEFAULT_WIDTH: dict[str, int] = {
    "Power": 110,
    "Converter": 180,
    "LDO": 180,
    "Load": 140,
    "Resistor": 80,
    "Capacitor": 100,
}
# Artix-7 colours for the three types the sample shows; everything else is transparent white.
DEFAULT_COLOR: dict[str, str] = {
    "Power": "#00FFFFFF",
    "Converter": "#FFAFEEEE",
    "Load": "#FFFFB6C1",
    "LDO": "#00FFFFFF",
    "Resistor": "#00FFFFFF",
    "Capacitor": "#00FFFFFF",
}

_COLUMN_GAP = 100  # room for a trunk, a junction and a corner between two columns
_ROW_GAP = 40  # PLAN.md 2.4

Point = tuple[int, int]
_POINT = re.compile(r"\((-?\d+),(-?\d+)\)")


def snap(value: float) -> int:
    """Round to the 5-unit grid (half rounds up, so the result does not depend on parity)."""
    return GRID * math.floor(value / GRID + 0.5)


def part_height(n_inputs: int, n_outputs: int) -> int:
    return PITCH * max(n_inputs, n_outputs, 1)


def _size(part: Part) -> tuple[int, int]:
    return int(part.props.get("Width", "0")), int(part.props.get("Height", "0"))


def anchor(part: Part, kind: str, n: int) -> Point:
    """Where terminal `kind:n` of `part` meets a wire. Holds for 5925 of 5936 stored x values and
    for every y of a part whose Height is 60 x (terminals of that kind)."""
    w, h = _size(part)
    count = sum(1 for t in part.terminals if t.kind == kind)
    # Input on the left edge, Output on the right edge, Aux on the centre line (FORMAT.md 5.1)
    x = part.x if kind == "Input" else part.x + w if kind == "Output" else part.x + w // 2
    y = part.y + (h - PITCH * count) // 2 + PITCH // 2 + PITCH * (n - 1)
    return x, y


def terminal_anchor(terminal: Terminal) -> Point:
    return anchor(terminal.part, terminal.kind, terminal.n)


def collapse(points: list[Point]) -> list[Point]:
    """Drop repeated points and middle points that lie on a straight axis-aligned run."""
    out: list[Point] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            del out[i]
        else:
            i += 1
    return out


def route(
    a: Point, b: Point, first: Literal["vertical", "horizontal"] | None = None
) -> list[Point]:
    """An orthogonal path of 2 to 4 points from `a` to `b`.

    `first` forces the direction of the first segment (two-bend-free L shape); by default the path
    goes horizontal, vertical at a grid-snapped mid x, horizontal.
    """
    if first == "vertical":
        return collapse([a, (a[0], b[1]), b])
    if first == "horizontal":
        return collapse([a, (b[0], a[1]), b])
    mid = snap((a[0] + b[0]) / 2)
    return collapse([a, (mid, a[1]), (mid, b[1]), b])


def format_points(points: list[Point]) -> str:
    return "".join(f"({x},{y})" for x, y in points)


def parse_points(text: str) -> list[Point]:
    return [(int(x), int(y)) for x, y in _POINT.findall(text)]


def place(
    tab: Tab,
    width: int,
    height: int,
    near: Part | None = None,
    x: int | None = None,
    y: int | None = None,
) -> Point:
    """Top-left corner for a new part. An explicit `x`/`y` is only snapped; otherwise the part goes
    to the first free column right of `near` (or at the left edge), below parts already there."""
    if x is not None and y is not None:
        return snap(x), snap(y)
    parts = [(p.x, p.y, *_size(p)) for p in tab.parts]
    rects = list(parts)
    # wire segments are obstacles too, so a new part does not sit on top of an existing wire
    for w in tab.wires:
        for (ax, ay), (bx, by) in itertools.pairwise(w.points):
            rects.append(
                (
                    min(ax, bx) - GRID,
                    min(ay, by) - GRID,
                    abs(ax - bx) + 2 * GRID,
                    abs(ay - by) + 2 * GRID,
                )
            )
    if near is not None:
        nw, _ = _size(near)
        px, py = snap(near.x + nw + _COLUMN_GAP), near.y
    else:
        px, py = snap(min((r[0] for r in parts), default=GRID)), GRID
    if x is not None:
        px = snap(x)
    if y is not None:
        py = snap(y)
    while True:
        # vertical clearance is _ROW_GAP; horizontal overlap is plain rectangle overlap
        hit = [
            r
            for r in rects
            if px < r[0] + r[2]
            and r[0] < px + width
            and py < r[1] + r[3] + _ROW_GAP
            and r[1] < py + height + _ROW_GAP
        ]
        if not hit:
            return px, py
        py = GRID * math.ceil(
            (max(r[1] + r[3] for r in hit) + _ROW_GAP) / GRID
        )  # ceil: always advances
