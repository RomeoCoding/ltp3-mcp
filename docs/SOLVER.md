# Solver (`src/ltp3/solver.py`)

`solve(graph, SolveOptions(current_mode="auto"|"peak"|"nominal", rounding="half_up"|"half_even"))`
solves one tab (`graph = build_graph(tab)`). It raises only `CYCLE`. Everything else is reported:

- `assumptions` (`A-*`): the rule is computed but not confirmed. Does not change `complete`.
- `blocked` (`B-*`): the rule is not computed. The part is `status="unsupported"`, `complete=False`.
- `warnings` (`W-*`): limits and wiring oddities (H19). Never change a number.

## Model

Demand flows from loads to sources in reverse topological order. Each net accumulates a current
and a power demand; a converter draws `Pin` (so `Iin = Pin / Vnet`), an LDO draws `Iout`, a
resistor draws the current of its output net, a load draws `Ipeak`/`Inom`. A source's `Pin` is the
power demanded on its net (ideal source, H3). Voltages flow forward (resistors subtract `R*I`).
Because a resistor's drop depends on the current it carries, the two passes repeat until the
resistor currents stop changing (relative 1e-15, at most 200 passes). `Ploss = Pin - Pout` and
`Eff = Pout / Pin` are only reported when `complete`; otherwise `pin_w`/`pout_w` are partial sums
(not a power balance) and `ploss_w`, `efficiency_pct` are `None`. Per-load `v`/`i` stay available
even when other parts are blocked. `display` rounds the 15-digit text the app would print to 2 dp
(`half_up` by default; oracle O5 decides, see Open).

## Hypotheses

| ID | Rule | Status | Code | Test |
|---|---|---|---|---|
| H1 | converter output `Pin = Vout*Iout/(NomEff/100)` | CONFIRMED | | `test_artix_oracle`, `test_h1_converter_efficiency_closed_form` |
| H2 | `Pout = sum V*I` at loads, `Ploss = Pin - Pout`, 2 dp display | CONFIRMED | | `test_artix_oracle` |
| H3 | source is ideal | CONFIRMED | | `test_artix_oracle` |
| H4 | load current is `Ipeak` when `Inom` is absent | CONFIRMED, still listed | `A-CURRENT-PEAK-ONLY` | `test_artix_oracle` (assumptions are exactly this one) |
| H5 | peak vs nominal when both exist | OPEN, `auto` = peak | `A-CURRENT-MODE` (only when both exist and differ) | `test_h5_current_modes` |
| H6 | `IpeakU`/`InomU` are power-of-ten exponents | LIKELY, not flagged | | `test_h6_unit_exponent_milliamps_and_display_rounding`, `test_format_oral` |
| H7 | LDO `Iin=Iout`, `Pin=Vin*Iin` | OPEN | `A-LDO` | `test_h7_ldo` |
| H8 | `Ignd` | BLOCKED | `B-LDO-GROUND-CURRENT` | `test_h8_ldo_ground_current_blocked` |
| H9 | cascade `Iin = Pin/Vin_net` | OPEN | `A-CASCADE` (converter fed by a non-source) | `test_h9_cascade` |
| H10 | resistor `Vout = Vin - R*I`, `Ploss = I^2 R`, load sees the drop | LIKELY (voltage) / OPEN (power) | `A-RESISTOR`; `B-NO-RESISTANCE` if `Resstnce` is missing | `test_h10_*`, `test_resistor_feeding_converter_converges`, `test_oral_header_agreement` |
| H11 | output without any efficiency key | BLOCKED, only for outputs that have a consumer | `B-NO-EFFICIENCY` | `test_h11_missing_efficiency_is_unsupported` |
| H12 | `MatrixA`, `PeakEff`, `pLossSet`, `EffType` | BLOCKED (any of them on an output) | `B-EFFICIENCY-MODEL` | `test_h12_efficiency_models_blocked` |
| H13 | range-only or key-less source feeding LDO/resistor/load | BLOCKED where the voltage is needed | `B-NO-VOLTAGE` | `test_h13_range_only_source` |
| H14 | negative rails, `P = V*I` | LIKELY, not flagged | | `test_h14_negative_rail` |
| H15 | same-named `Net` parts are one net | LIKELY | `A-NETLABEL` (graph merge, only when it joins distinct net ids) | `test_h15_net_labels_merge` |
| H16 | several drivers on one net | BLOCKED | `B-MULTI-DRIVER` | `test_h16_multiple_drivers_blocked` |
| H17 | totals are per tab | LIKELY | | structural: `solve` takes one tab |
| H18 | capacitors and `Aux` carry no current | LIKELY | | `test_h15_net_labels_merge` (labels draw nothing) |
| H19 | limits are warnings only | LIKELY | `W-VIN-RANGE`, `W-IOUT-MAX`, `W-DROPOUT`, `W-PLOSS-MAX` | `test_h19_limits_are_warnings_only` |
| H20 | `LoadType` other than `False` | BLOCKED | `B-LOAD-TYPE` | `test_h20_load_type_true_blocked` |
| H21 | `ConvType`, `Topology`, `ResType`, `VisType` are inert | LIKELY | | not read by the solver |

Other blocked codes: `B-UNDRIVEN` (a part draws power from a net with no driver; test
`test_undriven_input_is_unsupported`), `B-BAD-VALUE` (a number does not parse, `NomEff <= 0`),
`B-NO-CONVERGENCE`. Other warnings: `W-UNCONNECTED` (load input not wired: it draws nothing),
`W-NO-CURRENT`, `W-UNKNOWN-PART`, `W-GRAPH` (graph warnings). `A-CURRENT-FALLBACK`: no `Ipeak`,
`Inom` used.

Part status: `ok`, `assumed` (the part takes part in a flagged `A-*` rule other than
`A-CURRENT-PEAK-ONLY`), `unsupported`.

## Corpus (444 tabs, 261 templates, default options)

65 tabs `complete`, 379 not. Tabs carrying each blocking code (a tab can carry several):
`B-NO-EFFICIENCY` 238, `B-NO-VOLTAGE` 161, `B-EFFICIENCY-MODEL` 61, `B-MULTI-DRIVER` 20,
`B-NO-RESISTANCE` 7, `B-LDO-GROUND-CURRENT` 1. `B-NO-VOLTAGE` is mostly LDOs below range-only
sources (H13). `current_mode="nominal"` reproduces the `<ORal>` header in 164 of 261 templates.

## Open (needs oracles, PLAN 6.2)

H5 (O4), H6 and rounding (O5), H7 (O1), H8 (O12), H9 (O2), H10 (O3), H11 (O6), H13 (O11),
H15 (O8), H16 (O7). A `PeakEff` next to a `NomEff` is blocked, see `docs/deviations/P3.md`.
