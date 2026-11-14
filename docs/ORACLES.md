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
