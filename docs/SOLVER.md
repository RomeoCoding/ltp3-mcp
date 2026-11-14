# Solver (`src/ltp3/solver.py`)

`solve(graph, SolveOptions(current_mode="auto"|"peak"|"nominal", rounding="half_up"|"half_even"))`
solves one tab (`graph = build_graph(tab)`). It raises only `CYCLE`. Everything else is reported:

- `assumptions` (`A-*`): the rule is computed but not confirmed (`A-DEFAULT` names each key that was
  read as its GUI default, see `src/ltp3/defaults.py`). Does not change `complete`.
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

## Omitted keys
LTpowerPlanner omits a value equal to the app default, so a missing key means "default". All values
go through `ltp3.defaults` (source: ORACLES.md "GUI defaults"); a defaulted value that enters a sum
is recorded as `A-DEFAULT` (one note per `Type.key`) and makes the part `assumed`. Limits
(`VinMin`, `IoutMax`, `Dropout`, `PLossMax`) are only checked when the file states them.
Radio buttons write only their non-default position (O4):

| Radio | Default (writes nothing) | Non-default |
|---|---|---|
| load current | `Ipeak` (15 A if omitted) | `LoadType : False` = `Inom` (10 A if omitted); CONFIRMED by O4 |
| converter efficiency | `NomEff` (90 if omitted) | `EffType : True` = `PeakEff` (95 if omitted); HYPOTHESIS, `A-EFF-RADIO` |
| power voltage | Typ = `Vout` (12 V if omitted) | Min/Max radio: no key exists in the corpus, so Typ is used |

`MatrixA` and `pLossSet` (non-manual efficiency models) stay blocked.

## Hypotheses

| ID | Rule | Status | Code | Test |
|---|---|---|---|---|
| H1 | converter output `Pin = Vout*Iout/(NomEff/100)` | CONFIRMED | | `test_artix_oracle`, `test_h1_converter_efficiency_closed_form` |
| H2 | `Pout = sum V*I` at loads, `Ploss = Pin - Pout`, 2 dp display | CONFIRMED | | `test_artix_oracle` |
| H3 | source is ideal | CONFIRMED | | `test_artix_oracle` |
| H4 | load current is `Ipeak` when `Inom` is absent | CONFIRMED, still listed | `A-CURRENT-PEAK-ONLY` | `test_artix_oracle` (assumptions are exactly this one) |
| H5 | load current is `Inom` iff `LoadType : False`, else `Ipeak` | CONFIRMED (O4) | | `test_h5_current_modes`, `test_h5_load_type_false_selects_nominal`, `test_oracle_o4_radio_files`, `test_load_current_defaults` |
| H6 | `IpeakU`/`InomU` are power-of-ten exponents | LIKELY; `IpeakU` flagged until a GUI oracle confirms it | `A-UNIT-EXP-PEAK` (whenever a non-zero `IpeakU` is applied) | `test_h6_unit_exponent_milliamps_and_display_rounding`, `test_format_oral` |
| H7 | LDO `Iin=Iout`, `Pin=Vin*Iin` | CONFIRMED (O1) | `A-LDO` (still emitted) | `test_h7_ldo`, `test_oracle_o1_ldo` |
| H8 | `Ignd` | BLOCKED when non-zero; an explicit `Ignd : 0` is the default and adds nothing | `B-LDO-GROUND-CURRENT` | `test_h8_ldo_ground_current_blocked`, `test_explicit_zero_ignd_does_not_block_ldo` |
| H9 | cascade `Iin = Pin/Vin_net` | CONFIRMED (O2) | `A-CASCADE` (still emitted) | `test_h9_cascade`, `test_oracle_o2_cascade_with_omitted_defaults` |
| H10 | resistor `Vout = Vin - R*I`, `Ploss = I^2 R`, load sees the drop | CONFIRMED (O3) | `A-RESISTOR` (still emitted); missing `Resstnce` = 1 mohm with `A-DEFAULT` (QA2 #4, evidence in ORACLES.md; a fresh v3.6.8 resistor writes `Resstnce : 0` explicitly) | `test_h10_*`, `test_oracle_o3_resistor`, `test_resistor_feeding_converter_converges`, `test_oral_header_agreement` |
| H11 | output without `NomEff` | RESOLVED by omitted defaults: NomEff = 90 (O2) | `A-DEFAULT` | `test_h11_missing_efficiency_is_the_default_90` |
| H12 | `MatrixA`, `pLossSet` block; `EffType : True` selects `PeakEff`; `PeakEff` alone is inert | BLOCKED / HYPOTHESIS | `B-EFFICIENCY-MODEL`, `A-EFF-RADIO` | `test_h12_efficiency_models_blocked`, `test_peak_efficiency_with_eff_type_true`, `test_peak_eff_next_to_nom_eff_uses_nom_eff` |
| H13 | source without `Vout` | RESOLVED: Typ = 12 V default (no Min/Max radio key exists) | `A-DEFAULT`; `B-NO-VOLTAGE` only for a driver whose voltage is unknown | `test_h13_range_only_source`, `test_defaults_for_source_and_ldo_voltage` |
| H14 | negative rails, `P = V*I` | LIKELY, not flagged | | `test_h14_negative_rail` |
| H15 | same-named `Net` parts are one net | LIKELY | `A-NETLABEL` (graph merge, only when it joins distinct net ids) | `test_h15_net_labels_merge` |
| H16 | several drivers on one net | BLOCKED | `B-MULTI-DRIVER` | `test_h16_multiple_drivers_blocked` |
| H17 | totals are per tab | LIKELY | | structural: `solve` takes one tab |
| H18 | capacitors and `Aux` carry no current | LIKELY | | `test_h15_net_labels_merge` (labels draw nothing) |
| H19 | limits are warnings only | LIKELY | `W-VIN-RANGE`, `W-IOUT-MAX`, `W-DROPOUT`, `W-PLOSS-MAX` | `test_h19_limits_are_warnings_only` |
| H20 | `LoadType` other than `False`/`True` | BLOCKED | `B-LOAD-TYPE` | `test_h20_unknown_load_type_blocked` |
| H21 | `ConvType`, `Topology`, `ResType`, `VisType` are inert | LIKELY | | not read by the solver |

Other blocked codes: `B-UNDRIVEN` (a part draws power from a net with no driver; test
`test_undriven_input_is_unsupported`), `B-BAD-VALUE` (a number does not parse, `NomEff <= 0`),
`B-NO-CONVERGENCE`. Other warnings: `W-UNCONNECTED` (load input not wired: it draws nothing),
`W-NO-CURRENT`, `W-UNKNOWN-PART`, `W-GRAPH` (graph warnings). `A-NOMINAL-FALLBACK`: forced `current_mode="nominal"` on a load with no `Inom` uses `Ipeak` (the 10 A GUI default would be invented; QA2 #11). The `LoadType : False` radio still reads the 10 A default. `A-CURRENT-FALLBACK`: no `Ipeak`,
`Inom` used.

Part status: `ok`, `assumed` (the part takes part in a flagged `A-*` rule other than
`A-CURRENT-PEAK-ONLY`), `unsupported`.

## Corpus (444 tabs, 261 templates, default options)

Before P9a: 65 tabs `complete`. After: **382 complete**, 62 not. Remaining blocking codes (tabs):
`B-EFFICIENCY-MODEL` 42 (`MatrixA`/`pLossSet`), `B-MULTI-DRIVER` 20, `B-LDO-GROUND-CURRENT` 1.
`B-NO-EFFICIENCY`, `B-NO-VOLTAGE` and `B-NO-RESISTANCE` no longer occur.
`<ORal>` header agreement (default options, i.e. the load radio decides): **176 of 261** templates,
against 164 for the old "nominal everywhere" rule; no template that agreed before disagrees now
(the 12 gains come from the radio and the omitted defaults). Of the 85 disagreements, 15 are
templates with a blocked tab and 70 have only complete tabs (`docs/deviations/P9.md`).

## Open (needs oracles, PLAN 6.2)

H6 and rounding (O5), H8 (O12), H15 (O8), H16 (O7). `EffType : True` = peak (one corpus use).
Min/Max power-voltage radio (no key found). `IpeakU` (A-UNIT-EXP-PEAK).
