# 07 — Refactoring and index-integration journal

This journal records every decision taken while porting the GSV-Studio
indices, IAT, AHP, composite modeller and machine learning into RockMorph,
and while fixing the bugs of the audit ([01_audit_complet.md](01_audit_complet.md)).
Each decision has an identifier (D-A… architecture, D-S… science, D-U… user
interface) so that it can be **accepted or rejected one by one**. When the
scientific literature and GSV-Studio disagree, both are given, together with
the choice made "for now" and how to switch.

Branch: `ccr-c052972c-gpr3iz`. All values quoted below were produced by the
code on this branch (QGIS 3.44.7, Python 3.12, NumPy 2.x).

---

## 1. What was added

| Tool (menu) | Files | Purpose |
|---|---|---|
| Morphometry & Active Tectonics › **Morphometric Indices** | `tools/morphometry/`, `core/basin_context_builder.py`, `core/indices/` | 60 indices for the user's own basins, with published classifications, tables, maps and charts |
| Synthesis & Statistics › **Relative Active Tectonics (Iat)** | `tools/synthesis/iat_panel.py` | El Hamdouni et al. (2008) Iat from classified indices |
| Synthesis & Statistics › **AHP Weighting** | `tools/synthesis/ahp_panel.py` | Saaty pairwise matrix, weights, CR |
| Synthesis & Statistics › **Composite Index** | `tools/synthesis/composite_panel.py` | GSV-Studio "index modeler", with directions |
| Synthesis & Statistics › **Statistics & Machine Learning** | `tools/synthesis/statistics_panel.py`, `core/indices/ml.py` | PCA, k-means, hierarchical, DBSCAN, t-SNE/UMAP, Isolation Forest, LOF |
| (shared) | `widgets/results_window.py`, `web/morphometry.html`, `web/js/morphometry.js`, `widgets/index_widgets.py` | Results window (table, charts, profiles, diagnostics), catalogue widgets |

Unit tests: `tests/indices/` (21 tests, `python -m pytest tests/indices`).

---

## 2. Architecture decisions

**D-A1 — A pure index library (`core/indices`).** Every index is an
`IndexSpec`: name, family, formula, unit, authors, DOI, data requirements,
parameters, published classifications and a pure function
`compute(context, params)`. The UI (catalogue tree, info card, parameter
widgets, classification editor) is generated from these specs, so adding an
index means adding one spec. The library imports only NumPy/SciPy and is
tested without QGIS.

**D-A2 — One `BasinContext` per basin.** `core/basin_context_builder.py`
extracts once what the selected indices need (polygon metrics, DEM values,
flow routing, trunk stream, network, AF split, T sections, Vf profiles), and
only what they need (`requires` flags): polygon-only indices run instantly.

**D-A3 — Flow routing inside RockMorph (NumPy), per basin.** Priority-Flood
filling (Barnes et al., 2014), D8, accumulation in topological order, trunk
stream = largest-accumulation donor up to the divide. No FAC raster and no
GRASS are required for the indices. GSV-Studio truncated the channel at
1000 cells (bug P1 of [06](06_gsv_studio_inventaire.md)); there is no limit
here.

**D-A4 — Thread safety.** QGIS layers are read on the main thread
(`collect_request`), the computation runs in a `QgsTask` (QGIS progress bar,
Cancel button), and the output layers are built back on the main thread.
The synthesis tools work on attribute tables and run on the main thread
(fast).

**D-A5 — Work CRS.** The DEM CRS when it is projected in metres; otherwise
the WGS 84 / UTM zone of the basins, with bilinear reprojection of the DEM
(same rule as GSV-Studio). Tested with a geographic DEM: SL 228 m vs 222 m
with the UTM DEM (resampling difference only).

**D-A6 — No imposed study area.** Each tool asks for its own layers (DEM,
basins, optional network; or any vector layer with numeric fields for the
synthesis tools). Outputs are **new memory layers**: the user's data are
never modified.

**D-A7 — Run is blocked until inputs are complete.** Every tool shows a
plain-language checklist ("Choose a DEM", "Tick at least one index",
"SL: the edited classification is invalid", "CR > 0.10"…). The Run button
stays disabled until the list is empty. Problems specific to one basin (DEM
coverage < 95 %, no channel above the threshold, AF split impossible…) never
block the run: they appear in the *Diagnostics* tab and in a `warnings`
field.

**D-A8 — Qt WebEngine stays the default renderer** (your decision).
`base/webengine.py` is the only place that imports it; when it is missing,
panels show a message instead of charts and everything else works. The
future installer can offer WebEngine as a checkbox.

**D-A9 — Large results window.** The dock is ~420 px wide; tables of 60
columns and correlation matrices need room. Results open in a non-modal
window (can go to a second screen). Clicking a table row selects and zooms
the basin on the map.

---

## 3. Scientific decisions

Format: *GSV-Studio* · *literature* · **choice now** · how to switch.

**D-S1 — SL reach length.** GSV-Studio: 100 m (registry) but 1000 m in the
code. Literature: reaches of fixed length or between contours (Hack, 1973);
recent Iat studies use ~200 m on 30 m DEMs. **200 m**, editable per run
(*Reach length ΔL*). The fluvial tool uses the same 200 m (D-S14).

**D-S2 — SL classes.** GSV-Studio: 100 / 300 under the name "El Hamdouni et
al. (2008)". El Hamdouni et al. (2008): **300 / 500** (class 1 ≥ 500).
**300 / 500 by default**; GSV-Studio's values are selectable as
"GSV-Studio legacy (100 / 300)" to reproduce older results; Seeber &
Gornitz (1983) also available.

**D-S3 — AF classes.** El Hamdouni et al. (2008) classify |AF − 50|
(< 7, 7–15, ≥ 15). GSV-Studio applied the Balla Ateba (2023) thresholds to
AF itself. **|AF − 50|** for both schemes. AF is measured by splitting the
basin polygon along the trunk stream extended beyond its ends (stopping at
the first exit from the basin, otherwise the line could cross itself); if
GEOS cannot split, the right-bank share is counted on DEM cells.

**D-S4 — HI.** Default: elevation–relief ratio (Pike & Wilson, 1971),
classes 0.4 / 0.5 (El Hamdouni et al., 2008). Options: GSV-Studio median
variant, curve integral (Strahler, 1952).

**D-S5 — Vf.** GSV-Studio's cross-profile algorithm is kept (flat-floor
slope threshold, adaptive shoulder for the divides, 10 profiles from 5 % to
95 % of the channel, 500 m wide, median). Two additions, both documented in
the code: (a) the valley bottom is re-centred on the lowest point within
one cell of the D8 channel (D8 runs through cell centres); (b) the floor
width is never below one DEM cell. Without them most mountain profiles gave
Vf = 0 exactly (a floor narrower than the resolution cannot be measured).
Known limit: in wide alluvial valleys a 500 m profile does not reach the
valley sides, giving Vf ≫ 1 (e.g. 19.1 for basin 2120703460); widen
*Profile width* there. Classes 0.5 / 1.0 (El Hamdouni et al., 2008).

**D-S6 — Bs.** Bl and Bw are the sides of the minimum oriented bounding
rectangle. GSV-Studio cited Cannon (1977); the usual citation is
Ramírez-Herrera (1998). Classes 3 / 4 (El Hamdouni et al., 2008).

**D-S7 — T (transverse symmetry).** Midline = principal axis of the basin
cells (GSV-Studio used the polygon vertices, which over-weights densely
digitised parts). Dd measured towards the channel side (Cox, 1994); GSV's
"maximum half-width" is an option. GSV-Studio's T classes overlapped
(−∞–0.1 then 0–0.2): merged into three classes (0.2 / 0.4).

**D-S8 — Horton–Strahler.** Counts are **streams** (consecutive links of the
same order), not links. RB, RL, RA: mean of successive ratios (GSV-Studio)
by default, Horton-law regression as an option. Network: from the DEM with
a channel-initiation area of 1 km² (GSV-Studio default), or from a vector
layer; with a vector layer whose orders start above 1 (e.g. HydroRIVERS
`ORD_STRA`), a warning says the network is truncated.

**D-S9 — Drainage intensity Di.** GSV-Studio: Fs × Dd. Faniran (1968):
Fs / Dd. **Fs × Dd (GSV-Studio) by default** as you asked; Faniran's
definition is an option.

**D-S10 — TRD and Lar.** GSV-Studio's TRD used TR × TD / 10⁶ while its
registry described Hmax − Hmean; Lar mixed units (bug P5). Defaults follow
the definitions (TRD = Hmax − Hmean; Lar = Hack's law in miles); GSV
formulas remain selectable as "legacy".

**D-S11 — TWI, SPI, STI.** Computed with Horn slope and the specific
catchment area a = A / cell width. GSV-Studio used the DEM elevation instead
of the slope (bug P3).

**D-S12 — Erosion indices (De, Ebulk, E) and U, LengthArea, TSI.** Sources
or units are not settled; GSV-Studio's formulas are kept, flagged ⚗
"experimental" in the catalogue and excluded from the tests of §6.

**D-S13 — Iat.** Mean of the class scores of the chosen components,
classes 1.5 / 2.0 / 2.5 (El Hamdouni et al., 2008). A basin with missing
components gets the mean of the available ones (as in the paper);
*Minimum components* can require all. `Iat_n` records how many were used.
Fields named like an index are scored with its default classification;
`*_score` fields are used as scores.

**D-S14 — AHP.** Weights = principal eigenvector (Saaty). GSV-Studio's
column normalisation is an option. RI for n ≤ 10 from Saaty; n = 11–16
from GSV-Studio (published tables differ in the third decimal). Run blocked
when CR > 0.10 unless the user ticks "Accept CR > 0.10".

**D-S15 — Composite index.** Criteria rescaled 0–1 (min–max over the
processed basins). New: a direction per criterion ("↓ low" for Vf, Smf and
class scores) so that 1 always means more active. Methods: mean, weighted
mean, min, max, PC1 (oriented by the sum of loadings, with loadings and
explained variance reported). Results can be split in 4 classes
(quantiles by default, class 1 = highest).

**D-S16 — Statistics & ML.** Fields standardised (z-scores). PCA by SVD
(identical to scikit-learn). k-means: scikit-learn (`random_state=42`, as
GSV-Studio) or SciPy `kmeans2` when scikit-learn is missing. Cluster labels
start at 1. DBSCAN, t-SNE, UMAP, Isolation Forest and LOF need scikit-learn
/ umap-learn; they are listed but disabled with the reason when missing.
Every clustering is also drawn in the PC1–PC2 plane.

**D-S17 — Smf (existing tool).** Classes 1.1 / 1.5 (Bull & McFadden, 1977;
El Hamdouni et al., 2008) instead of 1.4 / 1.8 (no source). NoData
elevations stay missing instead of 0 m.

**D-S18 — Fluvial tool (existing).** See §4 (C1–C8).

---

## 4. Audit fixes applied

| Id | Problem | Fix | Commit |
|---|---|---|---|
| C1 | χ reversed relative to elevation (Z~χ slope −82.9 on a synthetic river) | χ from `chi_coordinate`, all arrays source → outlet; real rivers now give positive Z~χ slopes; chi-plot line z = z_out + ksn·χ | 1e79642 |
| C2 | CSV/GPKG rows mixed two points | consequence of C1: one order for all arrays | 1e79642 |
| C3 | river geometry mirrored; distances in CRS units | geometry source → outlet; metres converted to CRS units before `interpolate`/`curveSubstring` | 1e79642 |
| C4 | "regression" k_sn crashed on the 2nd segment | local variable renamed | 1e79642 |
| C5 | θ slider raised TypeError; NaN → 0 | engine window reused; NaN kept; knickpoint χ updated | 1e79642 |
| C6 | `np.trapz` removed from NumPy 2.x | `trapezoid` alias (`core/utils.py`) | 3d27e15 |
| C7 | knickpoints: up to 2ⁿ−1, nearest to source, O(n²) | global greedy segmentation, O(n) per step, BIC stop, ranked (test: 6000 points < 1 s) | 1e79642 |
| C8 | SL from pixels sampled 3× | one sample per cell; SL by 200 m reaches mapped on the points | 1e79642 |
| C9 | rose R ≈ 0, wrong axial mean | angle doubling (Mardia & Jupp, 2000) on measured segments | 3d27e15 |
| C10 | Smf thresholds, NoData = 0 m | D-S17 | 3d27e15 |
| M1 | no WebEngine → no tool loads | `base/webengine.py` fallbacks | 3d27e15 |
| M2 | `PyQt5` imports | `qgis.PyQt` everywhere (Qt6-ready) | 3d27e15 |
| M3 | absolute registry paths | relative paths, works as `RockMorph-main` | 4e4cae2 |
| M5 | `grass7:` ids, `native:filldepressions` missing, `-a` comments | `core/processing_ids.py`, `native:fillsinkswangliu`, comments | 1e79642 |
| M6 | missing icon, wrong "about", menu left after unload | icon, metadata 0.2.0 with changelog, unload removes the menu | af6920b |
| M12 | swath crashed on empty stations | gap-aware smoothing | 3d27e15 |

Found during the work (not in the audit):

* **`web/js/plotly.min.js` was double-encoded** (UTF-8 saved through
  windows-1252): negative tick labels showed "â'0.5", marker and arrow
  symbols were broken in *all* tools. Replaced by the official 2.27.0 build
  (identical except five bytes lost in the corruption).
* GSV-Studio's `BasinArea` was labelled km² but returned m².

Not done yet (still as in the audit): M4 for the **older** tools (they still
read layers from a `QThread`), M7 (raster read entirely), M8 (watershed
subdivision), M9 (main-river snapping in CRS units), M10 (knickpoints not
drawn on the map), M11 (3D explorer), M13 (HydroFlow Strahler in O(E²)).

---

## 5. Interface decisions

**D-U1 — Four numbered steps in every panel** (1 Data · 2 Indices/Fields ·
3 Settings · 4 Run), each with tooltips that explain the science and cite
the source.

**D-U2 — Presets.** "Iat – El Hamdouni et al. (2008)", Horton–Strahler,
basin shape, relief & hypsometry, active tectonics, channel profile,
hydrological terrain. The Iat tool ticks SL, AF, HI, VF and Bs by itself
when the layer has them.

**D-U3 — Editable thresholds.** Published classifications can be edited in
a table; gaps, overlaps and non-numeric bounds are reported and block the
run. A "Reset" button restores the published values.

**D-U4 — Charts.** One y-axis per plot (the profile view stacks elevation
and SL instead of a dual axis); class colours are an ordinal blue ramp
(class 1 darkest) shared by the map and the viewer; categorical colours in
a fixed order; light and dark themes; PNG (3×) and SVG export.

---

## 6. Tests on real data

**Data** (downloaded for this test, not in the repository):
Copernicus DEM GLO-30 tiles N36/N37 W004 (Sierra Nevada and Granada basin,
Spain — the region of El Hamdouni et al., 2008, and Pérez-Peña et al.,
2010), reprojected to ETRS89 / UTM 30N at 30 m; HydroBASINS level 12
(15 basins, 28–242 km²); HydroRIVERS (199 reaches).

**Results.**

* All 54 non-experimental indices computed for all 15 basins, with both
  the DEM-derived and the HydroRIVERS network; ~1 s per basin with every
  index selected (17 s in total), 11 s for the 5 Iat indices.
* Geographic DEM (EPSG:4326) path: same values within resampling
  differences (24 s, reprojection included).
* Fluvial tool on the same basins: 8 s for 15 rivers with GRASS flow
  accumulation; χ, knickpoints and segment k_sn consistent (§4).
* Synthesis tools on the indices layer: Iat, AHP (CR 0.098), composite PC1
  (56.1 % of variance), PCA, k-means, hierarchical clustering.
* Every tool of the plugin (16) loads in headless QGIS, including from a
  folder named `RockMorph-main`.

Iat obtained (default classifications, El Hamdouni et al., 2008):

| Basin (HYBAS_ID) | Area km² | SL (m) | AF (%) | HI | Vf | Bs | Scores SL/AF/HI/Vf/Bs | Iat | Class |
|---|---|---|---|---|---|---|---|---|---|
| 2120709100 | 121.7 | 222 | 23.2 | 0.336 | 0.98 | 2.19 | 3/1/3/2/3 | 2.40 | 3 |
| 2120709110 | 130.2 | 269 | 72.6 | 0.293 | 1.99 | 1.77 | 3/1/3/3/3 | 2.60 | 4 |
| 2121255500 | 146.4 | 379 | 24.0 | 0.277 | 0.56 | 3.19 | 2/1/3/2/2 | 2.00 | 3 |
| 2120708680 | 237.0 | 712 | 49.1 | 0.376 | 0.49 | 2.33 | 1/3/3/1/3 | 2.20 | 3 |
| 2120708640 | 242.2 | 1041 | 76.8 | 0.557 | 0.24 | 1.50 | 1/1/1/1/3 | 1.40 | 1 |
| 2121255100 | 175.3 | 759 | 76.5 | 0.244 | 0.29 | 2.58 | 1/1/3/1/3 | 1.80 | 2 |
| 2120703460 | 149.0 | 204 | 40.6 | 0.165 | 19.10 | 1.71 | 3/2/3/3/3 | 2.80 | 4 |
| 2120703520 | 148.6 | 241 | 49.3 | 0.321 | 1.86 | 2.32 | 3/3/3/3/3 | 3.00 | 4 |
| 2121255140 | 136.4 | 165 | 52.3 | 0.332 | 0.96 | 1.32 | 3/3/3/2/3 | 2.80 | 4 |
| 2120704060 | 27.8 | 37 | 62.0 | 0.236 | 16.58 | 2.85 | 3/2/3/3/3 | 2.80 | 4 |
| 2120704140 | 127.0 | 670 | 30.7 | 0.261 | 0.63 | 2.41 | 1/1/3/2/3 | 2.00 | 3 |
| 2120704430 | 129.3 | 685 | 27.6 | 0.286 | 0.40 | 2.88 | 1/1/3/1/3 | 1.80 | 2 |
| 2120704320 | 104.0 | 320 | 45.2 | 0.431 | 0.56 | 1.93 | 2/3/2/2/3 | 2.40 | 3 |
| 2120704330 | 190.0 | 564 | 56.4 | 0.439 | 0.30 | 1.46 | 1/3/2/1/3 | 2.00 | 3 |
| 2120704240 | 137.7 | 334 | 40.0 | 0.422 | 0.56 | 1.86 | 2/2/2/2/3 | 2.20 | 3 |

Reading: one basin is in class 1 (2120708640: SL 1041 m, HI 0.56, Vf 0.24),
two in class 2, seven in class 3 and five in class 4. These are test values
on downloaded data, not a validation: El Hamdouni et al. (2008) used other
basin limits and another DEM, so only a study with your own basins can be
compared with them.

Observations that matter for interpretation:

* The trunk-stream profile of 2120709100 has a 3 km flat stretch at 475 m:
  a reservoir surface in the Copernicus DSM, filled by the routing. SL is 0
  there. Use a hydrologically conditioned DEM where reservoirs exist.
* HydroBASINS level-12 units are often inter-basins (a river enters from
  upstream), so Horton ratios computed on them describe partial networks.
* Slope–area k_sn means (~40–65) exceed χ–segment k_sn (~3–20): the pixel
  slope–area estimate is biased upward by DEM noise; χ-based values are the
  robust ones (Perron & Royden, 2013).

---

## 7. Points to decide

1. D-S1/D-S2: SL reach 200 m and 300/500 classes by default (vs GSV 100/300).
2. D-S9: Di = Fs × Dd (GSV-Studio) or Fs / Dd (Faniran).
3. D-S5: the two Vf safeguards (re-centring, one-cell minimum floor).
4. D-S13: Iat with missing components (mean of available vs complete set).
5. D-S12: keep or remove the experimental erosion indices.

---

## Appendix — index catalogue as implemented

Generated from `core/indices/catalog.py` (⚗ = experimental).

### Basin geometry

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| BasinArea | Basin area A | km² | — | — | Computed on the polygon reprojected to the metric work CRS. GSV-Studio labelled this value km² but returned m². |
| BasinPerimeter | Basin perimeter P | km | — | — | P grows with the resolution of a raster-derived outline (stair-steps); compare basins digitised the same way. |
| BasinLength | Basin length Lb | km | — | — | Lb is the long side of the minimum-area oriented bounding rectangle (as in GSV-Studio); Bw is its short side. |
| Rf | Form factor Rf | — | — | Shape classes | — |
| Re | Elongation ratio Re | — | — | Schumm (1956) | — |
| Rc | Circularity ratio Rc | — | — | Miller (1953) | — |
| Lemniscate | Lemniscate ratio k | — | — | Chorley et al. (1957) | — |
| Compactness | Compactness coefficient Kc (Gravelius) | — | — | Gravelius (1914) | — |
| Rfn | Fineness ratio Rfn | — | length=Basin length (GSV-Studio) | Melton (1957) | — |
| Bs | Basin shape index Bs | — | — | El Hamdouni et al. (2008) – Iat | Bl and Bw are the sides of the minimum oriented bounding rectangle. GSV-Studio cited Cannon (1977) for the definition; the usual citation is Ramírez-Herrera (1998). |

### Relief & hypsometry

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| AltitudeMin | Minimum elevation Hmin | m | — | — | — |
| AltitudeMax | Maximum elevation Hmax | m | — | — | — |
| AltitudeMean | Mean elevation Hmean | m | — | — | — |
| AltitudeMedianHm | Median elevation Hm | m | — | — | Exact median; GSV-Studio interpolated it from a 100-bin histogram. |
| TR | Basin relief TR | m | — | — | — |
| TD | Absolute dissection TD | m | — | — | — |
| TRD | Summit relief TRD | m | formula=Hmax − Hmean (definition) | — | — |
| Rra | Relief ratio Rh | — | — | Schumm (1956); Strahler (1958) | — |
| Rr | Relative relief Rr | % | — | Mvondo Ondoa et al. (2016) | GSV-Studio attributed Rr to Pike & Wilson (1971); the TR·100/P form is Melton's (1957). |
| MRN | Melton ruggedness number | — | — | — | — |
| HI | Hypsometric integral HI | — | formula=Elevation-relief ratio (Pike & Wilson 1971) | El Hamdouni et al. (2008) – Iat | — |
| U ⚗ | Relative uplift index U | — | — | Nsangou et al. (2020) | GSV-Studio also offered a 'U El Hamdouni 2008' classification, but El Hamdouni et al. (2008) do not use U; it was dropped. |
| AltitudeSkewness | Elevation skewness | — | — | Statistical convention | Moments of the elevations, not the hypsometric-curve moments of Harlin (1978). |
| AltitudeKurtosis | Elevation kurtosis | — | — | Statistical convention | — |
| SlopeMeanDegree | Mean slope (degrees) | ° | averaging=Mean of slope angles | General slope classes | — |
| SlopeMeanPercent | Mean gradient (%) | % | — | General slope classes | — |

### Drainage network

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| StreamOrderMax | Highest stream order | — | — | — | — |
| TotalStreamLength | Total stream length ΣL | km | — | — | GSV-Studio labelled this value km but returned m. |
| NumStreams | Number of streams ΣNu | — | — | — | Streams are counted as Strahler defines them (a run of constant order), not as vector segments. Used by Fs, Dt and Rb. |
| TotalNumSegments | Number of links | — | — | — | Kept for comparison with GSV-Studio, which used segments where Horton's laws need streams. |
| RB | Bifurcation ratio Rb | — | method=Mean of successive ratios | Strahler (1964) | — |
| RL | Length ratio Rl | — | method=Mean of successive ratios | Horton (1945) | — |
| RA | Area ratio Ra | — | method=Mean of successive ratios | Schumm (1956) | Needs drainage areas: available with the DEM-derived network, or with a vector network when the DEM routing is also computed. |
| Dd | Drainage density Dd | km/km² | — | General classes | Dd depends on the channel-initiation threshold (DEM network) or on the mapping scale (vector network); keep it constant across a study. |
| Fs | Stream frequency Fs | km⁻² | — | General classes | — |
| Dt | Drainage texture Dt | km⁻¹ | — | Smith (1950), per km | — |
| Di | Drainage intensity Di | — | formula=Fs × Dd (GSV-Studio) | Faniran (1968) | — |
| Lg | Length of overland flow Lg | km | — | Horton (1945) | — |
| C | Constant of channel maintenance C | km²/km | — | Schumm (1956) | — |
| Lar | Hack's law ratio | — | formula=Main stream / Hack's law (miles) | Ratio reading | — |
| LengthArea ⚗ | Length-area texture La | km⁻¹ | — | — | Kept from GSV-Studio for continuity; its physical meaning is weak. |
| Hd | Ruggedness number Hd | — | — | Strahler (1958) | — |

### Main channel

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| HSI | Channel sinuosity | — | — | Schumm (1963) | GSV-Studio named this ratio Mueller's hydraulic sinuosity index; Mueller's HSI needs a separate valley length, so the plain sinuosity is reported. |
| TSI ⚗ | Valley / basin length ratio | — | — | — | Mueller's topographic sinuosity index is (VI − 1)/(CI − 1); this ratio is the GSV-Studio quantity and is kept for continuity. |
| MASC | Mean axial slope of the channel | m/m | — | — | — |
| HG | Hydraulic gradient | % | — | — | — |
| Ksn | Normalised steepness ksn | m^(2θ) | theta_ref=0.45, reach_length_m=250.0, statistic=Mean | Relative reading (θref = 0.45) | — |
| Chi | Chi (χ) of the trunk stream | m | theta_ref=0.45, reference_area_m2=1.0, statistic=Chi at source | — | — |

### Active tectonics

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| SL | Stream length-gradient index SL | m | reach_length_m=200.0, statistic=Mean | El Hamdouni et al. (2008) – Iat | — |
| SLK | Normalised SL (SL/K) | — | reach_length_m=200.0, statistic=Mean | Seeber & Gornitz (1983) | — |
| AF | Asymmetry factor AF | % | side=Right bank (looking downstream) | El Hamdouni et al. (2008) – Iat | The basin is split by the trunk stream extended straight beyond its ends. |
| TTSF | Transverse topographic symmetry T | — | sections=10, dd_method=Channel side (Cox 1994) | Cox (1994) | The midline is the principal axis of the basin area (PCA of its cells). |
| TTSF_Azimuth | Direction of channel shift (T vector) | ° | sections=10, dd_method=Channel side (Cox 1994) | — | — |
| VF | Valley floor width-to-height ratio Vf | — | profiles=10, start_fraction=0.05, end_fraction=0.95, profile_width_m=500.0, sampling_m=0.0, smoothing_sigma=1.5, floor_method=Flat-floor slope threshold, max_flat_slope_deg=2.0, basal_slope_threshold_deg=3.0, consistency_points=2, min_height_diff_m=0.5, min_half_width_m=1.0, divide_method=Adaptive shoulder detection, percentile=90.0, shoulder_min_rise_m=5.0, shoulder_slope_factor=0.3, shoulder_min_slope_deg=1.0, statistic=Median | El Hamdouni et al. (2008) – Iat | GSV-Studio read its Vf parameters under the key 'Vf' while the registry key is 'VF', so user settings were ignored; they are honoured here. |

### Hydrological terrain

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| TWI | Topographic wetness index TWI | — | statistic=Mean | — | a = upslope area per unit contour width; tan β floored at 0.001. |
| SPI | Stream power index SPI | m | statistic=Mean | — | — |
| STI | Sediment transport index STI | — | statistic=Mean | — | — |

### Erosion (experimental)

| Key | Index | Unit | Defaults | Default classification | Implementation notes |
|---|---|---|---|---|---|
| De ⚗ | Denudation rate D | — | — | — | Unit of S and of D to be confirmed in the source (doc/07). |
| Ebulk ⚗ | Bulk erosion Ebulk | — | — | — | Not the volume of Brocklehurst & Whipple (2002); kept for comparison only. |
| E ⚗ | Erosion rate E | — | — | — | The relation uses local relief; TR is used as in GSV-Studio. |
