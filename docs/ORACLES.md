# Solver oracles

Results of the app's **Calculate** (LTpowerPlanner III v3.6.8), recorded by hand from the GUI.
These are measurements of the app's behaviour and contain no ADI file content. P9 turns them into tests.

| Id | Tree | Pin (W) | Pout (W) | Ploss (W) | Eff | Confirms |
|---|---|---|---|---|---|---|
| A7 | Artix-7 Basys 3 template (two bucks, NomEff 80/91/88 %, loads Ipeak 3/3/1 A) | 16.67 | 14.7 | 1.97 | 88.16 % | load I = Ipeak; Pin = Vout·I/NomEff per output; display rounds to 2 dp |
| O1 | 5 V source → LDO (Vout 3.3, Ignd 0, Vdropout 0.2) → load 1 A (Ipeak = Inom = 1) | 5 | 3.3 | 1.7 | 66 % | LDO: Iin = Iout (+Ignd), Pin = Vin·Iin; per-part labels show Loss1 = 1.7 W, Eff1 = 66 % |

Pending: O2 (cascaded bucks), O3 (resistor), O4 (Ipeak vs Inom), O6 (default efficiency), T1 (AD4000 template).
