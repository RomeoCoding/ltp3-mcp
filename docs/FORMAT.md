# LTpowerPlanner III file format (`.ltp3` / `.ltp3t`) — draft spec v0.1

Status: DRAFT. Derived only from observing data files (261 factory templates, versions 3.0.3–3.5.4,
and two files saved by app v3.6.8). No program binary was inspected. Every statement is tagged
**[C]** confirmed on the whole corpus, **[S]** seen in samples only, or **[H]** hypothesis.
Counts are corpus totals (261 files, 2,433,847 bytes, 444 tabs).

## 1. Encoding and lines

- **[C]** UTF-8. 106 files contain non-ASCII (`°`, `μ`, `±`, `–`, `√` …); all decode as strict UTF-8.
- **[C]** One file starts with a UTF-8 BOM (`Power Tree for LT1210.ltp3t`). Preserve it.
- **[C]** Line terminator is CRLF; every file ends with CRLF. Bare LF never occurs.
- **[C]** A lone CR (not followed by LF) is **data**, not a terminator: 10 occurrences in 7 files,
  always the last character of a `Comments` value (`...Amplifier\r\r\n`). Split on `\r\n` only.
- **[C]** Trailing whitespace is significant: `<Note> `, `<Desn> `, `\tTitle   : ` (159×), part
  names and `PartNumb` values with trailing or leading padding (`PartNumb: LTC3877     `).
- **[C]** Exactly two line shapes exist (79,556 property lines, 25,460 tag lines, nothing else):
  - tag line, column 0: `<Tag>` + optional ` args`. The space is absent when args are absent
    (`<DS05>`) and absent in `<DS11>0.272727272727273`; it is present with empty args in `<Note> `.
  - property line: TAB + key padded with spaces to 8 chars + `:` + space + value
    (`\tVout    : 5`). All keys are ≤ 8 chars. Value may be empty (`Title`), may contain `:` and `,`.
- **[C]** No key repeats inside one block.

## 2. File structure

```
header block      <Vers> then, in templates only, 11 more tags
tab*              <Tabs> <Summ> <DSnn>* (<BPrt> <Term>+)* <Symb>* <Wire>*
```

- **[C]** Order inside a tab is always: Tabs, Summ, optional DS tags, all parts (each followed by its
  terminals), all symbols, all wires. The last line of every file is a wire `Points` line.
- **[C]** Tabs per file: 1 (143 files), 2 (74), 3 (28), 4 (14), 6 (1), 7 (1).

### 2.1 Header

| Tag | Content | Notes |
|---|---|---|
| `Vers` | `3.0.3` (22), `3.2.3` (17), `3.3.4` (206), `3.4.1` (2), `3.5.3` (13), `3.5.4` (1) | **[S]** app writes `3.6.8` |
| `Grop` `Vend` `Dvce` `SolN` `Desc` | group, vendor, device, solution name, description | template only |
| `Note` `Desn` | empty in 261 / 260 files (trailing space kept) | template only |
| `IRal` | input rails, e.g. `5V, 12V` | template only |
| `ORal` | output rails `V/I` per load terminal, e.g. `3.287504V/5.4mA` | template only; see §6 |
| `PPtN` | part numbers | template only |
| `Date` | `7/24/2020 12:00:00 AM` | template only |

**[S]** A project saved by the app (`.ltp3`) contains only `<Vers>` in the header: the other 11 tags
are dropped. Relative to its source template the saved Artix-7 file differs only by: those 11 lines
removed, `<Vers> 3.6.8`, and `\tZIndex  : -1` inserted as the first property of each of the 6
**Wire** blocks. (Not Symb blocks: the diff lines fall inside `<Wire>` blocks.) Saving twice is
byte-identical.

### 2.2 `<Tabs> id, #AARRGGBB, name`

- **[C]** `name` is the remainder after the second `, ` and may contain commas (16 tabs), e.g.
  `±15V VS, (from 5V/12Vsource)`. Parse with max-split 2.
- **[C]** `id` is unique within a file but not ordered and not 1-based (`6` then `5`). Opaque.

### 2.3 `<Summ> x, y, fontSize, #color, #fill`

- **[H]** Geometry and style of the summary box. In the Artix-7 file `x, y, fontSize` equal the
  `<Symb> Rectangle, 0, 410, 555` position and its `Thicknss : 18`. No solver results are stored in
  any file.

### 2.4 `<DSnn>`

- **[C]** Tags `DS02 DS03 DS04 DS05 DS06 DS07 DS11 DS17`, between `Summ` and the first part, in 12
  tabs of 9 files. Args are empty except `DS11`, which carries a float with no separating space.
- Meaning unknown. Preserve verbatim; never create.

## 3. Parts

### 3.1 `<BPrt> Type, id, x, y, name`

- **[C]** `name` is the remainder after the fourth `, ` and may contain commas (77 parts), e.g.
  `Power, 1, 65, 745, 5V, 12V, 24V Up to 40V`. Parse with max-split 4.
- **[C]** `x, y` are integers on a 5-unit grid (top-left corner). `Width`/`Height` are multiples of 10.
- **[C]** Part ids are unique within a tab (444/444) but collide across tabs (113 files). The id
  space is per tab. Parts are not sorted by id in 254 tabs: file order is not id order.

| Type | Count | Terminals (file order) | Part keys (count) |
|---|---|---|---|
| `Load` | 1371 | `Input`×1..7 (1357 have 1) | VisType 1193, FontSize 863, Comments 65, ComAsLbl 50, FontColr 3 |
| `LDO` | 752 | `Input`×1, `Output`×1 (6 have 2) | VisType 721, PartNumb 719, Comments 4, ComAsLbl 4, Ignd 4, FontSize 1 |
| `Converter` | 635 | `Input`×1, `Output`×1..8 | VisType 599, PartNumb 503, ConvType 228, Topology 72, SizeArea 54, FontSize 13, FontColr 8, SizeY 8, SizeX 7, Comments 4, ComAsLbl 4, Frequenc 3, SizeZ 3, CadPath 2 |
| `Power` | 470 | `Output`×1 | VisType 372, ResType 12, Comments 1, FontColr 1 |
| `Capacitor` | 432 | `Aux`×1 | Comments 2, FontColr 1 |
| `Resistor` | 200 | `Input`×1, `Output`×1 | Resstnce 185, ResType 162, FontSize 66, VisType 6, PLossMax 4 |
| `Net` | 44 | `Aux`×1 | — |

Every part has `Color`, `Width`, `Height`. Canonical part-key order (no conflicts in 3904 parts):
`Color, Width, Height, PartNumb|Resstnce, Frequenc|Ignd|PLossMax, ResType|SizeX, SizeY, SizeZ,
SizeArea, Topology, ConvType, CadPath, VisType, Comments, FontSize, FontColr, ComAsLbl`
(`a|b` = never seen together, relative order unknown).

Enumerations (meaning unknown, **[H]** display or catalogue hints with no effect on power maths):
`VisType` 1 (1489), 2 (1385), 0, 3, 4, 5; `ConvType` 1 (198), 2 (30); `Topology` 1 (71), 2 (1);
`ResType` 2 (64), 5 (51), 11 (32), 1 (12), 3, 4, 6, 7, 8, 10 (5 co-occurs with the name
`Ferrite Bead`, 1 with `Hot Swap`/`INFET`, 2 with `filter`); `ComAsLbl` always `True` when present.

### 3.2 `<Term> Kind, n`

- **[C]** Always 2 args; `Kind` ∈ `Input`, `Output`, `Aux`; `n` runs 1..k contiguously per kind;
  inputs precede outputs. A Term belongs to the nearest preceding BPrt.

| Owner.kind | Count | Keys (count) |
|---|---|---|
| Power.Output | 470 | VoutMin 378, VoutMax 333, IoutMax 199, Vout 184 (47 have no keys; 112 have only VoutMin+VoutMax) |
| Converter.Input | 635 | VinMax 401, VinMin 363 |
| Converter.Output | 1004 | Vout 816, IoutMax 629, VoutMax 499, VoutMin 453, NomEff 387, Dropout 51, IoutMin 51, PeakEff 50, MatrixA 50, pLossSet 6, EffType 1 |
| LDO.Input | 752 | VinMin 642, VinMax 529 |
| LDO.Output | 758 | Vout 738, IoutMin 706, IoutMax 695, VoutMin 674, VoutMax 658, Dropout 212 |
| Load.Input | 1396 | Ipeak 1358, Inom 1031, InomU 905, IpeakU 902, LoadType 712, Title 165 |
| Resistor.Input / .Output, Capacitor.Aux, Net.Aux | 200 / 200 / 432 / 44 | none |

Canonical terminal-key order (no conflicts in 5891 terminals):
- Input of Converter/LDO: `VinMin, VinMax`
- Output: `Vout, VoutMin, VoutMax, Dropout, IoutMin, IoutMax, NomEff|pLossSet, PeakEff|MatrixA, EffType`
- Load input: `Inom, InomU, Ipeak, IpeakU, LoadType, Title`

Efficiency data on the 1004 converter outputs: `NomEff` only 338, `NomEff`+`PeakEff` 49, `PeakEff`
only 1, `MatrixA` only 44, `MatrixA`+`pLossSet` 6, **none 566**. `MatrixA` is three comma-separated
floats (`0.4893703488, 1.5916185285, 0.3553039864`; 44 of 50 are this exact triple). `NomEff` and
`PeakEff` are percentages (80, 91, 89.5).

Units: `InomU`/`IpeakU` are always `-3` when present. **[H]** they are a power-of-ten exponent applied
to the adjacent value (absent = 0 = amperes). Supported by `ORal`: `Inom : 5.4`, `InomU : -3`
prints as `5.4mA`. Loads: 1017 terminals have both Ipeak and Inom (peak > nom 724, equal 264,
peak < nom 29), 341 only Ipeak, 14 only Inom, 24 neither.

Negative rails are stored with negative voltage **and** negative current (`-5V/-1A`): 335 negative
`Vout`, 178 negative `Ipeak`.

## 4. Symbols — `<Symb> Type, id, x, y, a,b, c,d`

| Type | Count | Args | Keys |
|---|---|---|---|
| `Rectangle` | 4204 | `a,b` = width,height; 3713 are all-zero placeholders with id 0 | Thicknss, Color, Color2, Dash (`5 2`), ZIndex (`-2`, 8×) |
| `Label` | 2953 | `a,b` and `c,d` always `0,0` | Thicknss (= font size), Color, Comments (= text), Color2 |
| `Line` | 151 | `c,d` = dx,dy vector; `a,b` unknown | Color, Thicknss, Dash, Startcap |
| `SummaryReport` | 56 | all zero | Color, Color2, Thicknss |

- **[C]** Symbol ids repeat within a tab (311 tabs): copies keep their id. They are not unique keys.
- Canonical key order: `Color|ZIndex, Color2, Thicknss, Comments|Dash|Startcap`.
- **[C]** Text values encode a line break as the literal 4-character token `*^!#` (6417 occurrences,
  245 files). Values `Comments : 1.0V CORE` are otherwise raw text.

## 5. Wires — `<Wire> wireId, netId`

Keys in canonical order: `ZIndex, WireClr, STermPID, STermTyp, STermNum, ETermPID, ETermTyp,
ETermNum, SWires, EWires, Points`. `ZIndex : -1` appears only in files of version ≥ 3.5.3 (775) and
in everything the 3.6.8 app saves. `WireClr` is `#AARRGGBB` (35×).

- **[C]** `Points : (x,y)(x,y)…` — 1 to 5 points (2: 3381, 3: 734, 4: 134, 5: 3, 1: 1). Every segment
  is axis-aligned (5263/5263). The first point is the S end, the last point the E end.
- **[C]** `S…`/`E…` name the part id, terminal kind and terminal number at that end. Direction is not
  canonical: S is an `Output` 1538×, an `Input` 1354×, an `Aux` 391×.
- **[C]** `SWires`/`EWires` list the ids of the **other wires meeting at that end** (a junction).
  Junctions are cliques: every wire at a junction lists all the others, and all share the exact
  junction coordinate (5938/5938 links). Junction degree 2 (680), 3 (2457), 4 (108), 5 (5). The
  list is not sorted (783 unsorted).
- **[C]** Every wire end has a terminal, a link list, or both; never neither. "Both" occurs only on
  `Aux` terminals (680 ends): a capacitor or net label sits on the junction.
- **[C]** An `Input` or `Output` terminal carries at most one wire (5252 terminals). Only `Aux`
  terminals carry two (340). Fan-out is therefore always built from junction wires.
- **[C]** `netId` equals the link-connected component in 444/444 tabs (2188 components = 2188
  distinct net ids). It is the authoritative net key; the link lists are redundant with it.
- **[C]** Anomalies that a reader must tolerate: duplicate wire ids in one tab
  (`Power Tree for ADGM1304.ltp3t`, ids 114 and 124); wire id equal to a part id (2 tabs); net id
  equal to a wire id (2 tabs) or part id (3 tabs). No dangling references: every `…TermPID` names an
  existing part and terminal, every linked wire id exists.
- **[H]** New ids: all objects in a tab draw from one counter (Artix-7 ids 1, 2, 9, 10, 11, 16, 18…
  interleave parts, wires, nets and labels). Allocate `max(every id in the tab) + 1`.

Net statistics before merging `Net` labels: 1 driver (`Output`) 2109 nets, 0 drivers 18, 2 drivers 40,
≥3 drivers 21. Multi-driver nets are Converter+Converter (59) or Power+Power (2). 17 of the 18
undriven nets contain a `Net` part. **[H]** `Net` parts with the same name in one tab are one
electrical net (8 names such as `12V`, `1.5V` sit on 2–5 separate wire nets of one tab).
299 terminals are unconnected (133 converter outputs, 73 capacitors, 42 load inputs …): legal.

### 5.1 Terminal anchor geometry (needed to place new wires)

For a part at `(x, y)` with `Width w`, `Height h` and `n` terminals of that kind, terminal `k`:

- x: `Input` → `x` (left edge); `Output` → `x + w` (right edge); `Aux` → `x + w/2`.
  Holds for 5925 of 5936 wire ends.
- y: `y + (h − 60·n)/2 + 30 + 60·(k − 1)` (60-unit pitch, centred). Holds for 100 % of Power, LDO
  input, Resistor, Capacitor, Converter input; 99 % of Load inputs and LDO outputs; 48 % of Converter outputs.
  Another 43 % of Converter outputs follow even spacing `y + h·(2k − 1)/(2n)`; 9 % follow neither.
- `Net` parts anchor at top or bottom edge (`dy` = 0 or `h`).
- The two rules coincide when `h = 60·n`. New parts must use that height.

## 6. `ORal` header as a weak oracle

`ORal` lists `voltage/current` for each load terminal in file order. A prototype that takes
voltage = driver `Vout` minus series-resistor drop `R·ΣI`, and current = `Inom` (else `Ipeak`) in
display units, reproduces the header exactly in 151 of 261 templates. Example: AD4000,
`3.287504V/5.4mA` = 3.3 V − 2.2 Ω × (5.4 mA + 0.28 mA). The header is stale or malformed in others
(`5.5AmA`, peak instead of nominal in old files), so it is supporting evidence, not ground truth.

## 7. Value forms

| Form | Keys | Notes |
|---|---|---|
| integer | ids, coordinates, Width, Height, Thicknss, FontSize, VisType, enums, `…U` | |
| decimal | Vout, I…, Eff, Resstnce, Dropout, Size… | no trailing zeros, no leading-dot form; scientific as `1E-05`, `5.5E-05`, `2E-08` (.NET style, capital E, two-digit exponent) |
| colour | Color, Color2, FontColr, WireClr | `#AARRGGBB`, upper-case hex |
| bool | LoadType (always `False`), ComAsLbl (`True`), EffType (`True`, 1×) | |
| text | PartNumb, Comments, Title, CadPath, Dash | raw; `*^!#` = newline |
| list | SWires, EWires (`18, 19`), MatrixA | `, `-separated |
| points | Points | `(x,y)` concatenated, no separator |

## 8. Open questions

DS tags; `Summ` args; Symb id semantics; `Line` `a,b`; VisType/ConvType/Topology/ResType meaning;
`LoadType`; `MatrixA` model; `EffType`; `pLossSet`; `Ignd` unit; whether the app tolerates files
whose key order, ids or anchors differ from what it would write itself.
