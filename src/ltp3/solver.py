"""Power-tree solver (PLAN.md section 2.3 and 6, docs/SOLVER.md).

`solve(graph, options)` never guesses silently: every rule that is not CONFIRMED either appears
in `result.assumptions` (computed, flagged) or in `result.blocked` (not computed, the part gets
`status="unsupported"` and `complete` is False). It never raises for an unsupported construct;
only a loop in the tree raises `CYCLE` (from the graph).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

from .errors import Ltp3Error
from .graph import Net, TabGraph
from .model import Part, Terminal
from .numfmt import fmt
from .numfmt import parse as parse_number

CurrentMode = Literal["auto", "peak", "nominal"]
Rounding = Literal["half_up", "half_even"]

_ACTIVE = ("Power", "Converter", "LDO", "Load", "Resistor")
_PASSIVE = ("Capacitor", "Net")  # H18: carry no current
_EFFICIENCY_MODEL_KEYS = ("MatrixA", "PeakEff", "pLossSet", "EffType")  # H12
_UNIT_PREFIX = {-3: "m", -6: "u", -9: "n", 3: "k"}
_MAX_ITER = 200  # resistors make Iin depend on the drop it causes; the fixed point converges fast
_TOL = 1e-15


@dataclass(frozen=True)
class SolveOptions:
    current_mode: CurrentMode = "auto"
    # Open question O5 (PLAN 6.2): does the app round half up or half to even? Display only.
    rounding: Rounding = "half_up"


@dataclass
class Totals:
    pin_w: float
    pout_w: float
    ploss_w: float | None
    efficiency_pct: float | None


@dataclass
class Note:
    """An assumption, a blocked rule or a warning, with the part ids it concerns."""

    code: str
    message: str
    parts: list[int] = field(default_factory=list)


Assumption = Note
Blocked = Note


@dataclass
class Warn:
    code: str
    message: str
    part: int | None = None


@dataclass
class OutputResult:
    n: int
    vout: float | None
    iout: float | None
    pout_w: float | None
    pin_w: float | None
    eff_pct: float | None


@dataclass
class PartResult:
    id: int
    name: str
    type: str
    status: str  # ok | assumed | unsupported
    reason: str = ""
    vin: float | None = None
    iin: float | None = None
    pin_w: float | None = None
    pout_w: float | None = None
    ploss_w: float | None = None
    outputs: list[OutputResult] = field(default_factory=list)


@dataclass
class LoadResult:
    terminal: str
    part: int
    v: float | None
    i: float | None  # amperes
    p_w: float | None
    i_value: float | None  # as stored, before the unit exponent
    unit_exp: int


@dataclass
class SourceResult:
    id: int
    name: str
    vout: float | None
    iout: float | None
    pin_w: float | None


@dataclass
class SolveResult:
    complete: bool
    totals: Totals
    display: Totals
    sources: list[SourceResult]
    parts: list[PartResult]
    loads: list[LoadResult]
    assumptions: list[Assumption]
    blocked: list[Blocked]
    warnings: list[Warn]
    current_mode: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------------
# helpers


def _scale(value: float, exp: int) -> float:
    # integer powers of ten are exact, so 500 mA is exactly 0.5 (500 * 0.001 would not be)
    return value * float(10**exp) if exp >= 0 else value / float(10 ** (-exp))


def _round2(x: float | None, mode: Rounding) -> float | None:
    if x is None or not math.isfinite(x):
        return x
    # round the 15-digit text the app would show, not the binary value, so 0.625 is a true half
    d = Decimal(fmt(x)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP if mode == "half_up" else ROUND_HALF_EVEN
    )
    return float(d) + 0.0  # + 0.0 turns -0.0 into 0.0


def _product(v: float | None, i: float | None) -> float | None:
    if i == 0:
        return 0.0  # nothing is drawn, so the voltage does not matter
    if v is None or i is None:
        return None
    return v * i


class _Notes:
    """Collects notes keyed by code, merging part ids."""

    def __init__(self) -> None:
        self.items: dict[str, Note] = {}
        self.warnings: list[Warn] = []

    def add(self, code: str, message: str, part: int | None = None) -> None:
        note = self.items.setdefault(code, Note(code, message))
        if part is not None and part not in note.parts:
            note.parts.append(part)

    def warn(self, code: str, message: str, part: int | None = None) -> None:
        self.warnings.append(Warn(code, message, part))

    def sorted(self) -> list[Note]:
        return [self.items[k] for k in sorted(self.items)]


class _Acc:
    """Per-net demand sums. `None` poisons a sum: one unknown draw makes the total unknown."""

    def __init__(self) -> None:
        self.i: dict[int, float | None] = {}
        self.p: dict[int, float | None] = {}

    def add(self, net: int, i: float | None, p: float | None) -> None:
        for store, val in ((self.i, i), (self.p, p)):
            cur = store.get(net, 0.0)
            store[net] = None if cur is None or val is None else cur + val

    def current(self, net: Net | None) -> float | None:
        return 0.0 if net is None else self.i.get(net.index, 0.0)

    def power(self, net: Net | None) -> float | None:
        return 0.0 if net is None else self.p.get(net.index, 0.0)


@dataclass
class _Out:
    term: Terminal
    n: int
    vout: float | None
    nom_eff: float | None
    iout_max: float | None
    dropout: float | None


@dataclass
class _Spec:
    part: Part
    blocked: str = ""  # code of the static block, "" if computable
    outs: list[_Out] = field(default_factory=list)
    inp: Terminal | None = None
    vin_min: float | None = None
    vin_max: float | None = None
    resistance: float | None = None
    ploss_max: float | None = None


@dataclass
class _LoadSpec:
    part: Part
    term: Terminal
    amps: float
    value: float | None
    exp: int
    blocked: str = ""


class _Solver:
    def __init__(self, graph: TabGraph, options: SolveOptions) -> None:
        self.g = graph
        self.opt = options
        self.order = graph.topo_order()  # raises CYCLE
        self.static = _Notes()
        self.specs: dict[Part, _Spec] = {}
        self.loads: list[_LoadSpec] = []
        self.unsupported_parts: dict[Part, str] = {}
        self._prepare()

    # ---------------------------------------------------------------- parsing
    def _num(self, part: Part, t: Terminal | Part, key: str) -> float | None:
        raw = t.props.get(key)
        if raw is None or not raw.strip():
            return None
        try:
            return parse_number(raw.strip())
        except Ltp3Error:
            self._block(part, "B-BAD-VALUE", f"{key} is not a number ({raw!r})")
            return None

    def _int(self, part: Part, t: Terminal, key: str) -> int:
        raw = t.props.get(key)
        if raw is None or not raw.strip():
            return 0
        try:
            return int(raw.strip())
        except ValueError:
            self._block(part, "B-BAD-VALUE", f"{key} is not an integer ({raw!r})")
            return 0

    def _block(self, part: Part, code: str, message: str) -> None:
        self.static.add(code, message, part.id)
        self.unsupported_parts.setdefault(part, code)

    # ---------------------------------------------------------------- static analysis
    def _prepare(self) -> None:
        for w in self.g.warnings:
            self.static.warn("W-GRAPH", w)
        for kind, msg in self.g.dangling:
            self.static.warn("W-GRAPH", f"{kind}: {msg}")
        for net in self.g.nets:
            if net.labels and len(net.net_ids) > 1:
                for t in net.terminals:
                    if t.part.type == "Net":
                        self.static.add(
                            "A-NETLABEL",
                            "Net labels with one name are one electrical net (H15, unconfirmed)",
                            t.part.id,
                        )
            drivers = self.g.drivers(net)
            if len(drivers) > 1:
                for d in drivers:
                    self._block(
                        d.part,
                        "B-MULTI-DRIVER",
                        "several outputs drive one net; the current split is unknown (H16)",
                    )
        for part in self.g.tab.parts:
            if part.type in _PASSIVE:
                continue
            if part.type not in _ACTIVE:
                self.static.warn("W-UNKNOWN-PART", f"unknown part type {part.type!r}", part.id)
                continue
            if part.type == "Load":
                self._prepare_load(part)
            else:
                self.specs[part] = self._prepare_part(part)
        for part, code in self.unsupported_parts.items():
            if part in self.specs:
                self.specs[part].blocked = code

    def _prepare_part(self, part: Part) -> _Spec:
        spec = _Spec(part)
        ins = [t for t in part.terminals if t.kind == "Input"]
        spec.inp = ins[0] if ins else None
        if spec.inp is not None:
            spec.vin_min = self._num(part, spec.inp, "VinMin")
            spec.vin_max = self._num(part, spec.inp, "VinMax")
        for t in part.terminals:
            if t.kind != "Output":
                continue
            out = _Out(
                t,
                t.n,
                self._num(part, t, "Vout"),
                self._num(part, t, "NomEff"),
                self._num(part, t, "IoutMax"),
                self._num(part, t, "Dropout"),
            )
            spec.outs.append(out)
            if part.type == "Converter":
                self._check_efficiency(part, t, out)
            if part.type == "LDO" and out.vout is None:
                self._block(part, "B-NO-VOLTAGE", "LDO output has no Vout")
        if part.type == "LDO" and part.props.get("Ignd") is not None:
            self._block(
                part, "B-LDO-GROUND-CURRENT", "Ignd (ground current) has no known unit (H8)"
            )
        if part.type == "LDO" and part not in self.unsupported_parts:
            self.static.add("A-LDO", "LDO: Iin = Iout, Pin = Vin*Iin (H7, unconfirmed)", part.id)
        if part.type == "Resistor":
            spec.ploss_max = self._num(part, part, "PLossMax")
            raw = part.props.get("Resstnce")
            if raw is None or not raw.strip():
                self._block(part, "B-NO-RESISTANCE", "resistor has no Resstnce")
            else:
                spec.resistance = self._num(part, part, "Resstnce")
            if part not in self.unsupported_parts:
                self.static.add(
                    "A-RESISTOR", "Resistor: Vout = Vin - R*I, Ploss = I^2*R (H10)", part.id
                )
        if part.type == "Converter" and part not in self.unsupported_parts:
            self._check_cascade(part, spec)
        return spec

    def _check_efficiency(self, part: Part, t: Terminal, out: _Out) -> None:
        if any(t.props.get(k) is not None for k in _EFFICIENCY_MODEL_KEYS):
            self._block(
                part,
                "B-EFFICIENCY-MODEL",
                "MatrixA/PeakEff/pLossSet/EffType select an unknown efficiency model (H12)",
            )
            return
        net = self.g.net_of(t)
        loaded = net is not None and bool(self.g.consumers(net))
        if out.nom_eff is None:
            # an output nobody draws from needs no efficiency; a loaded one does (H11)
            if loaded:
                self._block(part, "B-NO-EFFICIENCY", "converter output has no NomEff (H11)")
        elif out.nom_eff <= 0:
            self._block(part, "B-BAD-VALUE", "NomEff must be positive")

    def _check_cascade(self, part: Part, spec: _Spec) -> None:
        net = self.g.net_of(spec.inp) if spec.inp is not None else None
        if net is None:
            return
        if any(d.part.type != "Power" for d in self.g.drivers(net)):
            self.static.add(
                "A-CASCADE",
                "A converter fed by another stage draws Pin/Vin from it (H9, unconfirmed)",
                part.id,
            )

    def _prepare_load(self, part: Part) -> None:
        for t in part.terminals:
            if t.kind != "Input":
                continue
            net = self.g.net_of(t)
            ipk = self._num(part, t, "Ipeak")
            inom = self._num(part, t, "Inom")
            kpk = self._int(part, t, "IpeakU")
            knom = self._int(part, t, "InomU")
            pk = None if ipk is None else _scale(ipk, kpk)
            nom = None if inom is None else _scale(inom, knom)
            mode = self.opt.current_mode
            use_nom = (mode == "nominal" and nom is not None) or (pk is None and nom is not None)
            if use_nom:
                value, exp, amps = inom, knom, nom
            else:
                value, exp, amps = ipk, kpk, pk
            spec = _LoadSpec(part, t, amps or 0.0, value, exp)
            lt = (t.props.get("LoadType") or "").strip().lower()
            if lt not in ("", "false"):
                spec.blocked = "B-LOAD-TYPE"
                self._block(part, "B-LOAD-TYPE", f"LoadType {lt!r} changes the load model (H20)")
            self.loads.append(spec)
            if net is None:
                self.static.warn("W-UNCONNECTED", "load input is not connected", part.id)
                continue
            if amps is None:
                self.static.warn("W-NO-CURRENT", "load has neither Ipeak nor Inom", part.id)
                continue
            if mode == "auto" and pk is not None and nom is not None and pk != nom:
                self.static.add(
                    "A-CURRENT-MODE",
                    "Load has Ipeak and Inom; auto uses Ipeak, the app's choice is unknown (H5)",
                    part.id,
                )
            if pk is not None and nom is None and not use_nom:
                self.static.add(
                    "A-CURRENT-PEAK-ONLY",
                    "No Inom, so the load current is Ipeak (confirmed on the Artix-7 oracle)",
                    part.id,
                )
            if pk is None and nom is not None:
                self.static.add(
                    "A-CURRENT-FALLBACK",
                    "No Ipeak, so the load current is Inom (unconfirmed)",
                    part.id,
                )

    # ---------------------------------------------------------------- numeric passes
    def _net_voltage(self, drv: dict[int, list[float | None]], net: Net | None) -> float | None:
        if net is None:
            return None
        vs = drv.get(net.index, [])
        if not vs or vs[0] is None:
            return None
        if any(v is None or abs(v - vs[0]) > 1e-12 for v in vs):
            return None
        return vs[0]

    def _forward(self, rcur: dict[Part, float]) -> dict[int, list[float | None]]:
        drv: dict[int, list[float | None]] = {}
        for p in self.order:
            spec = self.specs.get(p)
            if spec is None:
                continue
            for o in spec.outs:
                v = o.vout
                if p.type == "Resistor":
                    vin = self._net_voltage(drv, self.g.net_of(spec.inp)) if spec.inp else None
                    r = spec.resistance
                    v = None if vin is None or r is None else vin - r * rcur.get(p, 0.0)
                net = self.g.net_of(o.term)
                if net is not None:
                    drv.setdefault(net.index, []).append(v)
        return drv

    def _why_no_voltage(self, net: Net | None) -> tuple[str, str]:
        if net is None or not self.g.drivers(net):
            return "B-UNDRIVEN", "net has no driver (unconnected or label not matched)"
        if len(self.g.drivers(net)) > 1:
            return "B-MULTI-DRIVER", "several outputs drive one net (H16)"
        return "B-NO-VOLTAGE", "the driving output has no known voltage (H13)"

    def _backward(
        self, drv: dict[int, list[float | None]], notes: _Notes
    ) -> tuple[dict[Part, PartResult], dict[Terminal, LoadResult], dict[Part, float], _Acc]:
        g = self.g
        acc = _Acc()
        res: dict[Part, PartResult] = {}
        loads: dict[Terminal, LoadResult] = {}
        rcur: dict[Part, float] = {}
        by_part: dict[Part, list[_LoadSpec]] = {}
        for ls in self.loads:
            by_part.setdefault(ls.part, []).append(ls)

        def need_v(part: Part, net: Net | None, pr: PartResult) -> float | None:
            v = self._net_voltage(drv, net)
            if v is None:
                code, msg = self._why_no_voltage(net)
                notes.add(code, msg, part.id)
                pr.status, pr.reason = "unsupported", code
            return v

        for p in reversed(self.order):
            if p.type == "Load":
                self._load(p, by_part.get(p, []), drv, acc, loads, res, notes, need_v)
                continue
            spec = self.specs.get(p)
            if spec is None:
                continue
            pr = PartResult(p.id, p.name, p.type, "ok")
            res[p] = pr
            if spec.blocked:
                pr.status, pr.reason = "unsupported", spec.blocked
                for o in spec.outs:
                    pr.outputs.append(OutputResult(o.n, o.vout, None, None, None, None))
                continue
            in_net = g.net_of(spec.inp) if spec.inp is not None else None
            if p.type == "Power":
                self._power(spec, pr, acc)
            elif p.type == "Converter":
                self._converter(spec, pr, acc, drv, in_net, notes, need_v)
            elif p.type == "LDO":
                self._ldo(spec, pr, acc, drv, in_net, notes, need_v)
            elif p.type == "Resistor":
                self._resistor(spec, pr, acc, drv, in_net, notes, need_v, rcur)
        return res, loads, rcur, acc

    def _power(self, spec: _Spec, pr: PartResult, acc: _Acc) -> None:
        for o in spec.outs:
            net = self.g.net_of(o.term)
            i, pw = acc.current(net), acc.power(net)
            pr.outputs.append(OutputResult(o.n, o.vout, i, pw, pw, None))
        ps = [o.pin_w for o in pr.outputs]
        pr.pin_w = None if any(x is None for x in ps) else math.fsum(x for x in ps if x is not None)
        pr.pout_w = pr.pin_w
        pr.ploss_w = 0.0 if pr.pin_w is not None else None  # H3: the source is ideal
        if pr.pin_w is None:
            pr.status, pr.reason = "unsupported", "depends on an unknown draw"

    def _feed_input(
        self,
        spec: _Spec,
        pr: PartResult,
        acc: _Acc,
        drv: dict[int, list[float | None]],
        in_net: Net | None,
        notes: _Notes,
        p_in: float | None,
        i_in: float | None,
    ) -> float | None:
        """Charge the input demand to the input net; returns the input voltage."""
        vin = self._net_voltage(drv, in_net)
        pr.vin, pr.iin = vin, i_in
        undriven = in_net is None or not self.g.drivers(in_net)
        if undriven and (p_in != 0 or i_in != 0):
            notes.add("B-UNDRIVEN", "input net has no driver", spec.part.id)
            pr.status, pr.reason = "unsupported", "B-UNDRIVEN"
        if in_net is not None:
            acc.add(in_net.index, i_in, p_in)
        return vin

    def _converter(
        self,
        spec: _Spec,
        pr: PartResult,
        acc: _Acc,
        drv: dict[int, list[float | None]],
        in_net: Net | None,
        notes: _Notes,
        need_v: Any,
    ) -> None:
        pins: list[float | None] = []
        pouts: list[float | None] = []
        for o in spec.outs:
            net = self.g.net_of(o.term)
            iout = acc.current(net)
            pout: float | None
            pin: float | None
            if iout == 0:
                pout, pin = 0.0, 0.0
            elif o.vout is None or iout is None:
                if o.vout is None:
                    notes.add("B-NO-VOLTAGE", "converter output has no Vout", spec.part.id)
                    pr.status, pr.reason = "unsupported", "B-NO-VOLTAGE"
                pout = pin = None
            else:
                pout = o.vout * iout
                pin = pout / (o.nom_eff / 100) if o.nom_eff else None  # H1
            eff = None if not pout or not pin else pout / pin * 100
            pr.outputs.append(OutputResult(o.n, o.vout, iout, pout, pin, eff))
            if o.iout_max is not None and iout is not None and abs(iout) > abs(o.iout_max) + 1e-12:
                notes.warn("W-IOUT-MAX", f"output {o.n} current exceeds IoutMax", spec.part.id)
            pins.append(pin)
            pouts.append(pout)
        known = all(x is not None for x in pins + pouts)
        pr.pin_w = math.fsum(x for x in pins if x is not None) if known else None
        pr.pout_w = math.fsum(x for x in pouts if x is not None) if known else None
        pr.ploss_w = (
            pr.pin_w - pr.pout_w if pr.pin_w is not None and pr.pout_w is not None else None
        )
        vin = self._net_voltage(drv, in_net)
        if pr.pin_w is None:
            i_in = None
        elif pr.pin_w == 0:
            i_in = 0.0
        else:
            i_in = pr.pin_w / vin if vin else None  # H9: Iin = Pin / Vin
        self._feed_input(spec, pr, acc, drv, in_net, notes, pr.pin_w, i_in)
        self._vin_warn(spec, vin, notes)
        if pr.pin_w is None and pr.status == "ok":
            pr.status, pr.reason = "unsupported", "depends on an unknown draw"

    def _vin_warn(self, spec: _Spec, vin: float | None, notes: _Notes) -> None:
        if vin is None:
            return
        lo, hi = spec.vin_min, spec.vin_max
        if (lo is not None and vin < lo - 1e-12) or (hi is not None and vin > hi + 1e-12):
            notes.warn("W-VIN-RANGE", f"input {fmt(vin)} V is outside VinMin/VinMax", spec.part.id)

    def _ldo(
        self,
        spec: _Spec,
        pr: PartResult,
        acc: _Acc,
        drv: dict[int, list[float | None]],
        in_net: Net | None,
        notes: _Notes,
        need_v: Any,
    ) -> None:
        iouts: list[float | None] = []
        for o in spec.outs:
            net = self.g.net_of(o.term)
            iout = acc.current(net)
            iouts.append(iout)
            pout = _product(o.vout, iout)
            pr.outputs.append(OutputResult(o.n, o.vout, iout, pout, None, None))
            if o.iout_max is not None and iout is not None and abs(iout) > abs(o.iout_max) + 1e-12:
                notes.warn("W-IOUT-MAX", f"output {o.n} current exceeds IoutMax", spec.part.id)
        i_in = None if any(i is None for i in iouts) else math.fsum(i for i in iouts if i)
        vin = self._net_voltage(drv, in_net)
        if i_in != 0 and vin is None:
            need_v(spec.part, in_net, pr)
        p_in = _product(vin, i_in)  # H7
        known = p_in is not None and all(o.pout_w is not None for o in pr.outputs)
        for res in pr.outputs:
            res.pin_w = _product(vin, res.iout)
            if res.pin_w and res.pout_w is not None:
                res.eff_pct = res.pout_w / res.pin_w * 100
        pr.pin_w = p_in
        pr.pout_w = (
            math.fsum(o.pout_w for o in pr.outputs if o.pout_w is not None) if known else None
        )
        pr.ploss_w = (
            pr.pin_w - pr.pout_w if pr.pin_w is not None and pr.pout_w is not None else None
        )
        self._feed_input(spec, pr, acc, drv, in_net, notes, p_in, i_in)
        self._vin_warn(spec, vin, notes)
        for o in spec.outs:
            has_all = o.dropout is not None and vin is not None and o.vout is not None
            if has_all and abs(vin or 0) - abs(o.vout or 0) < (o.dropout or 0) - 1e-12:
                notes.warn("W-DROPOUT", f"headroom below Dropout on output {o.n}", spec.part.id)
        if pr.pin_w is None and pr.status == "ok":
            pr.status, pr.reason = "unsupported", "depends on an unknown draw"

    def _resistor(
        self,
        spec: _Spec,
        pr: PartResult,
        acc: _Acc,
        drv: dict[int, list[float | None]],
        in_net: Net | None,
        notes: _Notes,
        need_v: Any,
        rcur: dict[Part, float],
    ) -> None:
        out = spec.outs[0] if spec.outs else None
        net = self.g.net_of(out.term) if out is not None else None
        cur = acc.current(net)
        r = spec.resistance
        vin = self._net_voltage(drv, in_net)
        if cur is not None:
            rcur[spec.part] = cur
        if cur != 0 and vin is None:
            need_v(spec.part, in_net, pr)
        p_in = _product(vin, cur)
        ploss = None if cur is None or r is None else cur * cur * r
        if out is not None:
            vout = None if vin is None or r is None or cur is None else vin - r * cur
            pout = _product(vout, cur)
            pr.outputs.append(OutputResult(out.n, vout, cur, pout, p_in, None))
            pr.pout_w = pout
        pr.pin_w, pr.ploss_w = p_in, ploss
        self._feed_input(spec, pr, acc, drv, in_net, notes, p_in, cur)
        if spec.ploss_max is not None and ploss is not None and ploss > spec.ploss_max + 1e-12:
            notes.warn("W-PLOSS-MAX", "resistor dissipation exceeds PLossMax", spec.part.id)
        if p_in is None and pr.status == "ok":
            pr.status, pr.reason = "unsupported", "depends on an unknown draw"

    def _load(
        self,
        p: Part,
        specs: list[_LoadSpec],
        drv: dict[int, list[float | None]],
        acc: _Acc,
        loads: dict[Terminal, LoadResult],
        res: dict[Part, PartResult],
        notes: _Notes,
        need_v: Any,
    ) -> None:
        pr = PartResult(p.id, p.name, p.type, "ok")
        res[p] = pr
        pins: list[float | None] = []
        for ls in specs:
            net = self.g.net_of(ls.term)
            v = self._net_voltage(drv, net)
            if net is None:
                pw: float | None = 0.0  # an unconnected load draws nothing
                amps_in: float | None = 0.0
            elif ls.blocked:
                pw, amps_in = None, None
                pr.status, pr.reason = "unsupported", ls.blocked
            else:
                amps_in = ls.amps
                if ls.amps != 0 and v is None:
                    need_v(p, net, pr)
                pw = _product(v, ls.amps)
                acc.add(net.index, ls.amps, pw)  # H2, H4: Pout = V*I at the load
            loads[ls.term] = LoadResult(ls.term.ref, p.id, v, ls.amps, pw, ls.value, ls.exp)
            pins.append(pw)
            del amps_in
        known = all(x is not None for x in pins)
        pr.pin_w = math.fsum(x for x in pins if x is not None) if known else None
        pr.pout_w = pr.pin_w
        pr.ploss_w = 0.0 if known else None
        if not known and pr.status == "ok":
            pr.status, pr.reason = "unsupported", "depends on an unknown voltage"

    # ---------------------------------------------------------------- driver
    def run(self) -> SolveResult:
        rcur: dict[Part, float] = {}
        notes = _Notes()
        for _ in range(_MAX_ITER):
            notes = _Notes()
            drv = self._forward(rcur)
            res, loads, new, _ = self._backward(drv, notes)
            delta = max(
                (abs(new.get(p, 0.0) - rcur.get(p, 0.0)) for p in set(new) | set(rcur)), default=0.0
            )
            scale = max((abs(x) for x in new.values()), default=0.0) + 1.0
            rcur = new
            if delta <= _TOL * scale:
                break
        else:
            notes.add("B-NO-CONVERGENCE", "resistor drops did not settle", None)
        return self._result(res, loads, notes)

    def _result(
        self, res: dict[Part, PartResult], loads: dict[Terminal, LoadResult], notes: _Notes
    ) -> SolveResult:
        tab = self.g.tab
        assumed: set[int] = set()
        for code, note in self.static.items.items():
            if code.startswith("A-") and code != "A-CURRENT-PEAK-ONLY":
                assumed.update(note.parts)
        parts = [res[p] for p in tab.parts if p in res]
        for pr in parts:
            if pr.status == "ok" and pr.id in assumed:
                pr.status = "assumed"
        merged = _Notes()
        for src in (self.static, notes):
            for n in src.items.values():
                merged.add(n.code, n.message)
                for pid in n.parts:
                    merged.add(n.code, n.message, pid)
            merged.warnings.extend(src.warnings)
        blocked = [n for n in merged.sorted() if n.code.startswith("B-")]
        assumptions = [n for n in merged.sorted() if n.code.startswith("A-")]
        sources = [
            SourceResult(pr.id, pr.name, o.vout, o.iout, o.pin_w)
            for pr in parts
            if pr.type == "Power"
            for o in pr.outputs
        ]
        load_list = [loads[ls.term] for ls in self.loads if ls.term in loads]
        complete = not blocked and all(pr.status != "unsupported" for pr in parts)
        pins = [pr.pin_w for pr in parts if pr.type == "Power"]
        pin = math.fsum(x for x in pins if x is not None)
        pout = math.fsum(x.p_w for x in load_list if x.p_w is not None)
        ploss: float | None = pin - pout if complete else None
        eff = pout / pin * 100 if complete and pin else None
        totals = Totals(pin, pout, ploss, eff)
        mode = self.opt.rounding
        display = Totals(
            _round2(pin, mode) or 0.0,
            _round2(pout, mode) or 0.0,
            _round2(ploss, mode),
            _round2(eff, mode),
        )
        return SolveResult(
            complete,
            totals,
            display,
            sources,
            parts,
            load_list,
            assumptions,
            blocked,
            merged.warnings,
            self.opt.current_mode,
        )


def solve(graph: TabGraph, options: SolveOptions | None = None) -> SolveResult:
    """Solve one tab. Raises `CYCLE` on a loop; everything else is reported in the result."""
    return _Solver(graph, options or SolveOptions()).run()


def format_oral(result: SolveResult) -> list[str]:
    """Load rails as the template header `<ORal>` prints them: `3.287504V/5.4mA`.

    The current is shown in the unit the file stores (`IpeakU`/`InomU` = power-of-ten exponent).
    """
    out: list[str] = []
    for ld in result.loads:
        if ld.v is None or ld.i_value is None:
            continue
        out.append(f"{fmt(ld.v)}V/{fmt(ld.i_value)}{_UNIT_PREFIX.get(ld.unit_exp, '')}A")
    return out
