# Solver oracles

Results of the app's **Calculate** (LTpowerPlanner III v3.6.8), recorded by hand from the GUI.
These are measurements of the app's behaviour and contain no ADI file content. P9 turns them into tests.

| Id | Tree | Pin (W) | Pout (W) | Ploss (W) | Eff | Confirms |
|---|---|---|---|---|---|---|
| A7 | Artix-7 Basys 3 template (two bucks, NomEff 80/91/88 %, loads Ipeak 3/3/1 A) | 16.67 | 14.7 | 1.97 | 88.16 % | load I = Ipeak; Pin = Vout·I/NomEff per output; display rounds to 2 dp |
| O1 | 5 V source → LDO (Vout 3.3, Ignd 0, Vdropout 0.2) → load 1 A (Ipeak = Inom = 1) | 5 | 3.3 | 1.7 | 66 % | LDO: Iin = Iout (+Ignd), Pin = Vin·Iin; per-part labels show Loss1 = 1.7 W, Eff1 = 66 % |

| O2 | 12 V → buck 5 V @ 90 % → buck 1 V @ 80 % → load 2 A | 2.78 | 2 | 0.78 | 72 % | cascade: downstream Pin is the upstream load (conv 3: 5 V 0.5 A, Loss 0.5 W; conv 2: 12 V 0.23 A, Loss 0.28 W) |
| O3 | 5 V → resistor 0.5 Ω → load 2 A | 10 | 8 | 2 | 80 % | resistor: Vout = Vin − R·I (load sees 4 V); Pout counts the load at the dropped voltage; resistor loss I²R counts in Ploss; label shows Eff = 80 % |

## Key finding: the app omits values equal to their defaults
In `samples/O2_ldo.ltp3`, tab 3 (O2), converter 2 was set to 90 % in the GUI but has **no `NomEff` line**, while converter 3 (80 %) has `NomEff : 80`. The 12 V source has **no `Vout` line**. The LDO in tab 2 has no `Vdropout`/`Ignd`/`VinMin` lines (GUI showed 0.2 V / 0 mA / 0.8 V).
So **a missing key means "the app's default", not "unknown"**. Known defaults so far: converter NomEff = 90 %, Power Vout = 12 V. This likely unblocks most `B-NO-EFFICIENCY` (238 tabs) and part of `B-NO-VOLTAGE` (161 tabs).
Default values seen in a fresh LDO panel: VinMin 0.8, VinMax 20, Vout 1, IoutMax 5, IoutMin 0.1, VoutMin 0.6, VoutMax 15, Vdropout 0.2, Ignd 0. A new Load showed 15 A.

Our solver on `samples/O1_ldo.ltp3` (tab 1 = A7, tab 2 = O1) reproduces both rows exactly.
The app trims trailing zeros in the summary (72 %, not 72.00 %).

Pending: O3 (resistor), O4 (Ipeak vs Inom), O6 (default efficiency), T1 (AD4000 template).

## GUI defaults (fresh parts, LTpowerPlanner III v3.6.8, screenshots 2026-11-14)
These are presumed to be the values used when a key is missing. That still needs confirming against older templates (v3.0.3-3.5.4), where defaults may differ.

| Part | Defaults |
|---|---|
| Power | VoutMin 2, **Vout (Typ) 12**, VoutMax 15 V; IoutMax 15 A. A radio button picks Min / **Typ** / Max as the voltage used |
| Converter | VinMin 4.5, VinMax 20 V; Frequency empty; Topology All; Converter Type All. Output 1: **Vout 2 V**, IoutMax 15 A, VoutMin 0.6, VoutMax 15 V; efficiency mode "Manually Enter Efficiency"; a radio button picks **Eff Nom 90 %** or Eff Peak 95 % |
| LDO | VinMin 0.8, VinMax 20 V; **Ignd 0 mA**. Output 1: **Vout 1 V**, IoutMax 5 A, IoutMin 0.1 A, VoutMin 0.6, VoutMax 15 V, **Vdropout 0.2 V** |
| Resistor | **Resistance 0 Ω**; VinMin 0.1, VinMax 500 V; PLossMax 2 W |
| Load | INominal 10 A, **IPeak 15 A**, unit A. A radio button picks INominal or **IPeak** as the current used (default IPeak) |

Implications:
- `PeakEff` being present does not mean the peak model is used; the efficiency radio decides. A missing `NomEff` means 90 %.
- Peak vs nominal load current (H5) is decided by the load's radio button. Its key is still unknown (candidate: `LoadType`, 712 occurrences). O4 = toggle the radio, save, diff.
- Power voltage depends on the radio. A source with only VoutMin/VoutMax has Typ = the 12 V default unless the radio picks Min or Max.
- A missing `Resstnce` means 0 Ω, not "blocked".

## GUI round-trip of editor output (P4), 2026-11-14
All three files in `samples/generated/` open in LTpowerPlanner 3.6.8 without errors, and Calculate runs. Our solver matches the app's summary on all 12 values:

| File | Pin | Pout | Ploss | Eff |
|---|---|---|---|---|
| artix7_dual_converter | 18.94 | 16.66 | 2.28 | 87.96 % |
| artix7_plus_ldo_load | 18.17 | 15.24 | 2.93 | 83.85 % |
| artix7_split_output | 17.19 | 15.15 | 2.04 | 88.15 % |

O4 (load current radio): with the radio on INominal the file has `LoadType: False`; a fresh load (radio on IPeak) has no LoadType line.
