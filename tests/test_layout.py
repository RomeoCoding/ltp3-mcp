"""P4: anchors, placement and orthogonal routing."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from ltp3 import layout
from ltp3.model import Part, Tree


def _tree(path: Path) -> Tree:
    return Tree.from_bytes(path.read_bytes())


def test_snap_to_grid() -> None:
    assert layout.snap(0) == 0
    assert layout.snap(7) == 5
    assert layout.snap(8) == 10
    assert layout.snap(-3) == -5
    assert layout.snap(12.4) == 10


def test_anchor_matches_sample(sample_path: Path) -> None:
    tab = _tree(sample_path).tabs[0]
    # coordinates read off the stored wires of the Artix-7 sample
    assert layout.anchor(tab.part(1), "Output", 1) == (295, 295)
    assert layout.anchor(tab.part(2), "Input", 1) == (420, 295)
    assert layout.anchor(tab.part(2), "Output", 1) == (560, 265)
    assert layout.anchor(tab.part(2), "Output", 2) == (560, 325)
    assert layout.anchor(tab.part(16), "Output", 1) == (560, 460)
    assert layout.anchor(tab.part(11), "Input", 3) == (760, 385)
    t = tab.part(11).terminal("Input", 2)
    assert layout.terminal_anchor(t) == (760, 325)


def test_aux_anchor_is_centre_x() -> None:
    tree = Tree.from_bytes(
        b"<Vers> 3.6.8\r\n<Tabs> 1, #FFFFFACD, T\r\n<Summ> 0, 0, 18, #FF0000FF, #00FFFFFF\r\n"
        b"<BPrt> Capacitor, 3, 100, 200, C\r\n\tColor   : #00FFFFFF\r\n\tWidth   : 100\r\n"
        b"\tHeight  : 60\r\n<Term> Aux, 1\r\n"
    )
    assert layout.anchor(tree.tabs[0].part(3), "Aux", 1) == (150, 230)


def _anchor_stats(paths: list[Path]) -> tuple[int, int, int, int]:
    """(ends, x matches, y checked, y mismatches) over every wire end that has a terminal."""
    ends = xok = ychecked = ybad = 0
    for p in paths:
        for tab in _tree(p).tabs:
            by_id = {pt.id: pt for pt in tab.parts}
            for w in tab.wires:
                pts = w.points
                for end, point in ((w.start, pts[0] if pts else None), (w.end, pts[-1])):
                    if end is None or point is None or end[0] not in by_id:
                        continue
                    part = by_id[end[0]]
                    try:
                        t = part.terminal(end[1], end[2])
                    except Exception:
                        continue
                    ends += 1
                    ax, ay = layout.terminal_anchor(t)
                    xok += ax == point[0]
                    if _tight(part, end[1]) and part.type != "Net":
                        ychecked += 1
                        ybad += ay != point[1]
    return ends, xok, ychecked, ybad


def _tight(part: Part, kind: str) -> bool:
    n = sum(1 for t in part.terminals if t.kind == kind)
    return int(part.props["Height"]) == 60 * n


@pytest.mark.corpus
def test_anchor_reproduces_corpus(template_paths: list[Path]) -> None:
    ends, xok, ychecked, ybad = _anchor_stats(template_paths)
    assert ends >= 5936 - 5  # FORMAT.md 5: 5936 wire ends carry a terminal
    assert xok >= 5925  # FORMAT.md 5.1
    assert ychecked > 1000
    assert ybad == 0, "y rule must hold for every part whose Height == 60 * n"


def test_route_is_orthogonal_and_ends_match() -> None:
    cases = [((0, 0), (100, 0)), ((0, 0), (100, 60)), ((100, 60), (0, 0)), ((50, 50), (50, 120))]
    for a, b in cases:
        pts = layout.route(a, b)
        assert pts[0] == a and pts[-1] == b
        assert 2 <= len(pts) <= 4
        assert all(p[0] == q[0] or p[1] == q[1] for p, q in itertools.pairwise(pts))
        assert all(p != q for p, q in itertools.pairwise(pts))


def test_route_mid_x_on_grid() -> None:
    pts = layout.route((295, 295), (420, 460))
    assert len(pts) == 4
    assert pts[1][0] % 5 == 0 and pts[1][0] == pts[2][0]


def test_route_forced_directions() -> None:
    v = layout.route((345, 295), (420, 460), first="vertical")
    assert v == [(345, 295), (345, 460), (420, 460)]
    h = layout.route((345, 295), (420, 460), first="horizontal")
    assert h == [(345, 295), (420, 295), (420, 460)]


def test_collapse_removes_duplicates_and_collinear_points() -> None:
    pts = [(0, 0), (0, 0), (5, 0), (10, 0), (10, 5)]
    assert layout.collapse(pts) == [(0, 0), (10, 0), (10, 5)]
    assert layout.collapse([(3, 4)]) == [(3, 4)]


def test_points_round_trip() -> None:
    pts = [(560, 460), (660, 460), (660, 385), (760, 385)]
    assert layout.parse_points(layout.format_points(pts)) == pts
    assert layout.format_points(pts) == "(560,460)(660,460)(660,385)(760,385)"


def test_part_height_and_defaults() -> None:
    assert layout.part_height(1, 2) == 120
    assert layout.part_height(0, 0) == 60
    assert layout.part_height(3, 1) == 180
    assert layout.DEFAULT_WIDTH["Converter"] == 180
    assert layout.DEFAULT_WIDTH["Power"] == 110
    assert layout.DEFAULT_WIDTH["Load"] == 140


def test_place_is_free_and_gridded(sample_path: Path) -> None:
    tab = _tree(sample_path).tabs[0]
    near = tab.part(16)
    x, y = layout.place(tab, 140, 60, near=near)
    assert x % 5 == 0 and y % 5 == 0
    assert x >= near.x + int(near.props["Width"])  # to the right of the upstream part
    for p in tab.parts:
        px, py = p.x, p.y
        pw, ph = int(p.props["Width"]), int(p.props["Height"])
        overlap = x < px + pw and px < x + 140 and y < py + ph and py < y + 60
        assert not overlap
    # a second request for the same spot cannot return the same place once it is occupied
    x2, y2 = layout.place(tab, 140, 60)
    assert (x2 % 5, y2 % 5) == (0, 0)


def test_place_explicit_position_is_snapped(sample_path: Path) -> None:
    tab = _tree(sample_path).tabs[0]
    assert layout.place(tab, 100, 60, x=903, y=1002) == (905, 1000)
