"""
core/indices/catalog.py

The RockMorph catalogue of geomorphic indices.

This module declares every index (``IndexSpec``) and the presets offered in
the "Morphometric Indices" tool. It is the single source of truth for names,
formulas, units, references, parameters and classifications: the UI, the
result layer, the Iat/AHP tools and the decisions journal all read it.

Provenance
----------
The catalogue is a port of GSV-Studio's ``INDICE_DEFS`` registry (same keys,
so tables from both programs can be compared) with these systematic changes:

* labels are in English (the UI goes through Qt translation);
* classification rules use the shared convention of ``spec.Classification``
  and carry class numbers (``score``) where the scheme is an ordinal ranking;
* classifications used by the relative active tectonics index follow
  El Hamdouni et al. (2008), Geomorphology 96, 150-173,
  doi:10.1016/j.geomorph.2007.08.004 — values verified against later papers
  that reproduce that table (see doc/07);
* thresholds GSV-Studio attributed to a source that says otherwise are fixed
  and the change is written in the scheme ``description``.

Adding an index
---------------
1. Write the pure function in ``formulas.py``.
2. Declare its ``IndexSpec`` below, in the right family section.
3. Add a unit test with a basin whose answer is known (tests/indices).

Authors: RockMorph contributors / Tony
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from . import formulas as f
from .spec import Classification, IndexSpec, ParameterSpec, rules

INF = float("inf")

# DOIs used several times.
DOI_EL_HAMDOUNI_2008 = "doi:10.1016/j.geomorph.2007.08.004"
DOI_HORTON_1945 = "doi:10.1130/0016-7606(1945)56[275:EDOSAT]2.0.CO;2"
DOI_SCHUMM_1956 = "doi:10.1130/0016-7606(1956)67[597:EODSAS]2.0.CO;2"
DOI_STRAHLER_1952 = "doi:10.1130/0016-7606(1952)63[1117:HAAOET]2.0.CO;2"
DOI_HACK_1973 = "https://pubs.usgs.gov/journal/1973/vol1issue4/report.pdf"
DOI_COX_1994 = "doi:10.1130/0016-7606(1994)106<0571:AODBSA>2.3.CO;2"
DOI_BALLA_2023 = "doi:10.1007/s12517-023-11235-1"
DOI_NSANGOU_2020 = "doi:10.1016/j.jafrearsci.2020.103798"

# Family names, in catalogue order.
FAMILY_SHAPE = "Basin geometry"
FAMILY_RELIEF = "Relief & hypsometry"
FAMILY_NETWORK = "Drainage network"
FAMILY_CHANNEL = "Main channel"
FAMILY_TECTONIC = "Active tectonics"
FAMILY_TERRAIN = "Hydrological terrain"
FAMILY_EROSION = "Erosion (experimental)"
FAMILIES = (FAMILY_SHAPE, FAMILY_RELIEF, FAMILY_NETWORK, FAMILY_CHANNEL,
            FAMILY_TECTONIC, FAMILY_TERRAIN, FAMILY_EROSION)


# ---------------------------------------------------------------------------
# Shared parameter factories
# ---------------------------------------------------------------------------

def _statistic(default: str = "Mean") -> ParameterSpec:
    """Basin aggregation of local values (reaches, sections, pixels)."""
    return ParameterSpec(
        "statistic", "Basin statistic", "choice", default,
        help="How local values (reaches, sections, profiles or cells) are "
             "summarised into one value per basin.",
        choices=f.STATISTICS,
    )


def _horton_method() -> ParameterSpec:
    return ParameterSpec(
        "method", "Ratio estimate", "choice", "Mean of successive ratios",
        help="'Mean of successive ratios' averages the ratio between consecutive "
             "orders (GSV-Studio). 'Horton law regression' fits Horton's "
             "geometric law over all orders and is less sensitive to a single "
             "unusual order (Strahler, 1964).",
        choices=("Mean of successive ratios", "Horton law regression"),
    )


def _theta() -> ParameterSpec:
    return ParameterSpec(
        "theta_ref", "Reference concavity θref", "float", 0.45,
        help="Reference concavity index. 0.45 is the common value for bedrock "
             "rivers (Wobus et al., 2006; Kirby & Whipple, 2012). Keep it the "
             "same for all basins you compare.",
        minimum=0.1, maximum=1.0, step=0.05, decimals=2,
    )


# ---------------------------------------------------------------------------
# Shared classification schemes
# ---------------------------------------------------------------------------

def _el_hamdouni(key, label, rule_set, description="", transform=None) -> Classification:
    return Classification(
        key, label, "El Hamdouni et al. (2008)", DOI_EL_HAMDOUNI_2008,
        rule_set, description=description, transform=transform,
    )


SPECS: Tuple[IndexSpec, ...] = (

    # =======================================================================
    # Basin geometry
    # =======================================================================
    IndexSpec(
        "BasinArea", "Basin area A", FAMILY_SHAPE,
        "Planimetric area enclosed by the drainage divide.",
        "A", "km²", "—", "—", ("geometry",), f.basin_area,
        notes="Computed on the polygon reprojected to the metric work CRS. "
              "GSV-Studio labelled this value km² but returned m².",
    ),
    IndexSpec(
        "BasinPerimeter", "Basin perimeter P", FAMILY_SHAPE,
        "Length of the drainage divide.",
        "P", "km", "—", "—", ("geometry",), f.basin_perimeter,
        notes="P grows with the resolution of a raster-derived outline "
              "(stair-steps); compare basins digitised the same way.",
    ),
    IndexSpec(
        "BasinLength", "Basin length Lb", FAMILY_SHAPE,
        "Longest dimension of the basin, used by Rf, Re, k, Bs and Rh.",
        "Lb", "km", "—", "—", ("geometry",), f.basin_length,
        notes="Lb is the long side of the minimum-area oriented bounding "
              "rectangle (as in GSV-Studio); Bw is its short side.",
    ),
    IndexSpec(
        "Rf", "Form factor Rf", FAMILY_SHAPE,
        "Ratio of area to the square of basin length. Low values mean elongated basins.",
        "Rf = A / Lb²", "", "Horton (1932); Schumm (1956)",
        "doi:10.1029/TR013i001p00350", ("geometry",), f.form_factor,
        classifications=(
            Classification("Rf_Shape", "Shape classes", "after Horton (1932); Schumm (1956)",
                           DOI_SCHUMM_1956, rules(
                               (-INF, 0.25, "Very elongated", 1), (0.25, 0.5, "Elongated", 2),
                               (0.5, 0.75, "Moderately compact", 3), (0.75, INF, "Compact (circle ≈ 0.785)", 4))),
            Classification("Rf_Schumm_1956", "Schumm (1956)", "Schumm (1956)", DOI_SCHUMM_1956, rules(
                (-INF, 0.21, "Elongated", 1), (0.21, 0.27, "Moderate", 2), (0.27, INF, "Compact", 3))),
        ),
        default_classification="Rf_Shape",
    ),
    IndexSpec(
        "Re", "Elongation ratio Re", FAMILY_SHAPE,
        "Diameter of the circle of the same area divided by basin length. "
        "Values near 1 mean circular basins, low values elongated ones.",
        "Re = 2·√(A/π) / Lb", "", "Schumm (1956)", DOI_SCHUMM_1956,
        ("geometry",), f.elongation_ratio,
        classifications=(
            Classification("Re_Schumm_1956", "Schumm (1956)", "Schumm (1956)", DOI_SCHUMM_1956, rules(
                (-INF, 0.5, "Elongated", 1), (0.5, 0.7, "Oval", 2), (0.7, INF, "Circular", 3))),
            Classification("Re_Strahler_1964", "Strahler (1964)", "Strahler (1964)",
                           "Handbook of Applied Hydrology, section 4-II", rules(
                               (-INF, 0.6, "Elongated, high relief", 1), (0.6, 0.8, "Oval, moderate relief", 2),
                               (0.8, INF, "Circular, low relief", 3))),
        ),
        default_classification="Re_Schumm_1956",
    ),
    IndexSpec(
        "Rc", "Circularity ratio Rc", FAMILY_SHAPE,
        "Area divided by the area of a circle with the same perimeter (1 for a circle).",
        "Rc = 4πA / P²", "", "Miller (1953)", "doi:10.1086/626413",
        ("geometry",), f.circularity_ratio,
        classifications=(
            Classification("Rc_Miller_1953", "Miller (1953)", "Miller (1953)", "doi:10.1086/626413", rules(
                (-INF, 0.3, "Very elongated / irregular", 1), (0.3, 0.5, "Oval", 2),
                (0.5, 0.7, "Sub-circular", 3), (0.7, INF, "Circular", 4))),
            Classification("Rc_Anand_2019", "Anand et al. (2019)", "Anand et al. (2019)",
                           "reference to be checked (GSV-Studio)", rules(
                               (-INF, 0.35, "Elongated", 1), (0.35, 0.4, "Moderate", 2), (0.4, INF, "Circular", 3))),
        ),
        default_classification="Rc_Miller_1953",
    ),
    IndexSpec(
        "Lemniscate", "Lemniscate ratio k", FAMILY_SHAPE,
        "Compares the basin with a lemniscate (k = 1); larger values mean elongated basins.",
        "k = Lb² / 4A", "", "Chorley, Malm & Pogorzelski (1957)", "doi:10.2475/ajs.255.2.138",
        ("geometry",), f.lemniscate_ratio,
        classifications=(
            Classification("k_Chorley_1957", "Chorley et al. (1957)", "Chorley et al. (1957)",
                           "doi:10.2475/ajs.255.2.138", rules(
                               (-INF, 0.8, "Wider than a lemniscate", 1), (0.8, 1.2, "Close to a lemniscate", 2),
                               (1.2, INF, "More elongated", 3))),
        ),
        default_classification="k_Chorley_1957",
    ),
    IndexSpec(
        "Compactness", "Compactness coefficient Kc (Gravelius)", FAMILY_SHAPE,
        "Perimeter divided by the circumference of the circle of the same area (1 for a circle).",
        "Kc = P / (2·√(πA))", "", "Gravelius (1914)", "Grundriss der gesamten Gewässerkunde",
        ("geometry",), f.compactness_coefficient,
        classifications=(
            Classification("Kc_Gravelius", "Gravelius (1914)", "Gravelius (1914)", "—", rules(
                (1.0, 1.25, "Nearly circular", 1), (1.25, 1.5, "Oval", 2), (1.5, INF, "Elongated / irregular", 3))),
        ),
        default_classification="Kc_Gravelius",
    ),
    IndexSpec(
        "Rfn", "Fineness ratio Rfn", FAMILY_SHAPE,
        "Length over perimeter; high values mean elongated, simple outlines.",
        "Rfn = Lb / P  (Melton: Lc / P)", "", "Melton (1957)", "doi:10.1086/626481",
        ("geometry", "channel"), f.fineness_ratio,
        parameters=(ParameterSpec(
            "length", "Length used", "choice", "Basin length (GSV-Studio)",
            help="Melton (1957) uses the main channel length; GSV-Studio used the basin length.",
            choices=("Basin length (GSV-Studio)", "Main channel length (Melton 1957)")),),
        classifications=(
            Classification("Rfn_Melton_1957", "Melton (1957)", "Melton (1957)", "doi:10.1086/626481", rules(
                (-INF, 0.15, "Very compact / complex outline", 1), (0.15, 0.3, "Moderate", 2),
                (0.3, INF, "Very elongated", 3))),
        ),
        default_classification="Rfn_Melton_1957",
    ),
    IndexSpec(
        "Bs", "Basin shape index Bs", FAMILY_SHAPE,
        "Basin length over width. Elongated basins (high Bs) are typical of young, "
        "tectonically active mountain fronts.",
        "Bs = Bl / Bw", "", "Ramírez-Herrera (1998); Cannon (1976)",
        "doi:10.1002/(SICI)1096-9837(199804)23:4<317::AID-ESP845>3.0.CO;2-V",
        ("geometry",), f.basin_shape_index,
        classifications=(
            _el_hamdouni("Bs_ElHamdouni_2008", "El Hamdouni et al. (2008) – Iat", rules(
                (-INF, 3.0, "Class 3: circular (low activity)", 3),
                (3.0, 4.0, "Class 2: moderate", 2),
                (4.0, INF, "Class 1: elongated (high activity)", 1))),
            Classification("Bs_Nsangou_2020", "Nsangou et al. (2020)", "Nsangou et al. (2020)",
                           DOI_NSANGOU_2020, rules(
                               (-INF, 3.0, "Low tectonic activity", 3), (3.0, 4.0, "Moderate tectonic activity", 2),
                               (4.0, INF, "High tectonic activity", 1))),
        ),
        default_classification="Bs_ElHamdouni_2008",
        notes="Bl and Bw are the sides of the minimum oriented bounding rectangle. "
              "GSV-Studio cited Cannon (1977) for the definition; the usual citation is "
              "Ramírez-Herrera (1998).",
    ),

    # =======================================================================
    # Relief & hypsometry
    # =======================================================================
    IndexSpec("AltitudeMin", "Minimum elevation Hmin", FAMILY_RELIEF,
              "Lowest DEM elevation in the basin.", "Hmin", "m", "—", "—",
              ("elevation",), f.altitude_min),
    IndexSpec("AltitudeMax", "Maximum elevation Hmax", FAMILY_RELIEF,
              "Highest DEM elevation in the basin.", "Hmax", "m", "—", "—",
              ("elevation",), f.altitude_max),
    IndexSpec("AltitudeMean", "Mean elevation Hmean", FAMILY_RELIEF,
              "Mean DEM elevation in the basin.", "Hmean", "m", "—", "—",
              ("elevation",), f.altitude_mean),
    IndexSpec("AltitudeMedianHm", "Median elevation Hm", FAMILY_RELIEF,
              "Median DEM elevation in the basin.", "Hm", "m", "Strahler (1952)", DOI_STRAHLER_1952,
              ("elevation",), f.altitude_median,
              notes="Exact median; GSV-Studio interpolated it from a 100-bin histogram."),
    IndexSpec("TR", "Basin relief TR", FAMILY_RELIEF,
              "Total relief of the basin.", "TR = Hmax − Hmin", "m", "Schumm (1956)", DOI_SCHUMM_1956,
              ("elevation",), f.total_relief),
    IndexSpec("TD", "Absolute dissection TD", FAMILY_RELIEF,
              "Height of the mean surface above the local base level.",
              "TD = Hmean − Hmin", "m", "Walcott & Summerfield (2008)",
              "doi:10.1016/j.geomorph.2007.08.001", ("elevation",), f.topographic_dissection),
    IndexSpec("TRD", "Summit relief TRD", FAMILY_RELIEF,
              "Height of the summits above the mean surface.",
              "TRD = Hmax − Hmean", "m", "Walcott & Summerfield (2008)",
              "doi:10.1016/j.geomorph.2007.08.001", ("elevation",), f.summit_relief,
              parameters=(ParameterSpec(
                  "formula", "Formula", "choice", "Hmax − Hmean (definition)",
                  help="GSV-Studio's code computed TR × TD / 10⁶ although its own "
                       "definition is Hmax − Hmean.",
                  choices=("Hmax − Hmean (definition)", "TR × TD / 10⁶ (GSV-Studio legacy)")),)),
    IndexSpec(
        "Rra", "Relief ratio Rh", FAMILY_RELIEF,
        "Basin relief divided by basin length: overall steepness of the basin.",
        "Rh = TR / Lb", "", "Schumm (1956)", DOI_SCHUMM_1956,
        ("elevation", "geometry"), f.relief_ratio,
        classifications=(
            Classification("Rh_Schumm_Strahler", "Schumm (1956); Strahler (1958)", "Schumm (1956); Strahler (1958)",
                           "doi:10.1130/0016-7606(1958)69[279:DAATFE]2.0.CO;2", rules(
                               (-INF, 0.01, "Very low (plains)", 1), (0.01, 0.05, "Low (low hills)", 2),
                               (0.05, 0.1, "Moderate (high hills)", 3), (0.1, 0.2, "High (low mountains)", 4),
                               (0.2, INF, "Very high (high mountains)", 5))),
            Classification("Rh_Das_2018", "Das (2018)", "Das (2018)", "reference to be checked (GSV-Studio)", rules(
                (-INF, 0.02, "Low", 1), (0.02, 0.04, "Moderate", 2), (0.04, INF, "High", 3))),
        ),
        default_classification="Rh_Schumm_Strahler",
    ),
    IndexSpec(
        "Rr", "Relative relief Rr", FAMILY_RELIEF,
        "Basin relief as a percentage of the perimeter.",
        "Rr = TR × 100 / P", "%", "Melton (1957)", "doi:10.1086/626481",
        ("elevation", "geometry"), f.relative_relief,
        classifications=(
            Classification("Rr_Mvondo_2016", "Mvondo Ondoa et al. (2016)", "Mvondo Ondoa et al. (2016)",
                           "doi:10.4236/ojg.2016.61004 (to be checked)", rules(
                               (-INF, 0.5, "Low", 1), (0.5, 1.0, "Moderate", 2), (1.0, INF, "High", 3))),
        ),
        default_classification="Rr_Mvondo_2016",
        notes="GSV-Studio attributed Rr to Pike & Wilson (1971); the TR·100/P form is Melton's (1957).",
    ),
    IndexSpec(
        "MRN", "Melton ruggedness number", FAMILY_RELIEF,
        "Basin relief normalised by the square root of area; discriminates debris-flow "
        "from flood-dominated fans.",
        "MRN = TR / √A", "", "Melton (1965)", "doi:10.1086/627083",
        ("elevation", "geometry"), f.melton_ruggedness,
    ),
    IndexSpec(
        "HI", "Hypsometric integral HI", FAMILY_RELIEF,
        "Relative volume of the basin above its base. High values mean little-eroded, "
        "young or uplifting relief; low values mature, eroded relief.",
        "HI = (Hmean − Hmin) / (Hmax − Hmin)", "", "Strahler (1952); Pike & Wilson (1971)",
        DOI_STRAHLER_1952, ("elevation",), f.hypsometric_integral,
        parameters=(ParameterSpec(
            "formula", "Formula", "choice", "Elevation-relief ratio (Pike & Wilson 1971)",
            help="The elevation-relief ratio equals the curve integral (Pike & Wilson, 1971). "
                 "The median variant was offered by GSV-Studio. The curve integral is computed "
                 "from all DEM cells and lets you check the ratio.",
            choices=("Elevation-relief ratio (Pike & Wilson 1971)", "Median variant (GSV-Studio)",
                     "Curve integral (Strahler 1952)")),),
        classifications=(
            _el_hamdouni("HI_ElHamdouni_2008", "El Hamdouni et al. (2008) – Iat", rules(
                (-INF, 0.4, "Class 3: concave, mature (low activity)", 3),
                (0.4, 0.5, "Class 2: moderate", 2),
                (0.5, INF, "Class 1: convex, young (high activity)", 1))),
            Classification("HI_Strahler_1952", "Strahler (1952) erosion stages", "Strahler (1952)",
                           DOI_STRAHLER_1952, rules(
                               (-INF, 0.35, "Old (monadnock)", 3), (0.35, 0.6, "Mature (equilibrium)", 2),
                               (0.6, INF, "Young (inequilibrium)", 1))),
            Classification("HI_Balla_2023", "Balla Ateba et al. (2023)", "Balla Ateba et al. (2023)",
                           DOI_BALLA_2023, rules(
                               (-INF, 0.2, "Convex (young) – class 3", 3),
                               (0.2, 0.4, "S-shaped (mature) – class 2", 2),
                               (0.4, INF, "Concave (old) – class 1", 1)),
                           description="Thresholds and labels as entered in GSV-Studio; the labels "
                                       "contradict the usual reading (high HI = convex/young) and must "
                                       "be checked in the paper."),
        ),
        default_classification="HI_ElHamdouni_2008",
    ),
    IndexSpec(
        "U", "Relative uplift index U", FAMILY_RELIEF,
        "Combines median elevation and hypsometric integral to rank relative uplift.",
        "U = Hm(km) + (1 − HI)", "", "Sinha-Roy (2002)",
        "Sinha-Roy, S. (2002), Banas basin, Rajasthan (reference to be checked)",
        ("elevation",), f.uplift_index,
        classifications=(
            Classification("U_Nsangou_2020", "Nsangou et al. (2020)", "Nsangou et al. (2020)",
                           DOI_NSANGOU_2020, rules(
                               (-INF, 1.0, "Low activity", 3), (1.0, 1.1, "Moderate activity", 2),
                               (1.1, INF, "High activity", 1))),
        ),
        default_classification="U_Nsangou_2020",
        experimental=True,
        notes="GSV-Studio also offered a 'U El Hamdouni 2008' classification, but El Hamdouni "
              "et al. (2008) do not use U; it was dropped.",
    ),
    IndexSpec(
        "AltitudeSkewness", "Elevation skewness", FAMILY_RELIEF,
        "Skewness of the elevation distribution (positive: most of the basin is low).",
        "g1 (unbiased)", "", "—", "—", ("elevation",), f.altitude_skewness,
        classifications=(
            Classification("Skew_Standard", "Statistical convention", "—", "—", rules(
                (-INF, -0.5, "Strongly left-skewed"), (-0.5, -0.1, "Moderately left-skewed"),
                (-0.1, 0.1, "About symmetric"), (0.1, 0.5, "Moderately right-skewed"),
                (0.5, INF, "Strongly right-skewed"))),
        ),
        default_classification="Skew_Standard",
        notes="Moments of the elevations, not the hypsometric-curve moments of Harlin (1978).",
    ),
    IndexSpec(
        "AltitudeKurtosis", "Elevation kurtosis", FAMILY_RELIEF,
        "Pearson kurtosis of the elevation distribution (3 for a normal law).",
        "β2 (unbiased)", "", "—", "—", ("elevation",), f.altitude_kurtosis,
        classifications=(
            Classification("Kurt_Pearson", "Statistical convention", "—", "—", rules(
                (-INF, 2.5, "Platykurtic"), (2.5, 3.5, "Mesokurtic"), (3.5, INF, "Leptokurtic"))),
        ),
        default_classification="Kurt_Pearson",
    ),
    IndexSpec(
        "SlopeMeanDegree", "Mean slope (degrees)", FAMILY_RELIEF,
        "Mean of the slope angle of every basin cell (Horn's method, as in QGIS).",
        "mean(β)", "°", "Horn (1981)", "doi:10.1109/PROC.1981.11918",
        ("terrain",), f.mean_slope_degree,
        parameters=(ParameterSpec(
            "averaging", "Averaging", "choice", "Mean of slope angles",
            help="GSV-Studio returned atan(mean(tan β)), which is lower than the mean angle on "
                 "steep terrain.",
            choices=("Mean of slope angles", "atan(mean tan β) (GSV-Studio legacy)")),),
        classifications=(
            Classification("Slope_Degree", "General slope classes", "FAO-type land classes", "—", rules(
                (0.0, 2.0, "Flat"), (2.0, 5.0, "Gentle"), (5.0, 10.0, "Moderate"),
                (10.0, 15.0, "Moderately steep"), (15.0, 30.0, "Steep"), (30.0, INF, "Very steep"))),
        ),
        default_classification="Slope_Degree",
    ),
    IndexSpec(
        "SlopeMeanPercent", "Mean gradient (%)", FAMILY_RELIEF,
        "Mean of tan β × 100 over the basin.", "mean(tan β) × 100", "%", "Horn (1981)",
        "doi:10.1109/PROC.1981.11918", ("terrain",), f.mean_slope_percent,
        classifications=(
            Classification("Slope_Percent", "General slope classes", "FAO-type land classes", "—", rules(
                (0.0, 3.5, "Flat"), (3.5, 8.7, "Gentle"), (8.7, 17.6, "Moderate"),
                (17.6, 26.8, "Moderately steep"), (26.8, 57.7, "Steep"), (57.7, INF, "Very steep"))),
        ),
        default_classification="Slope_Percent",
    ),

    # =======================================================================
    # Drainage network
    # =======================================================================
    IndexSpec("StreamOrderMax", "Highest stream order", FAMILY_NETWORK,
              "Strahler order of the trunk stream at the outlet.", "max(u)", "",
              "Strahler (1952)", DOI_STRAHLER_1952, ("network",), f.stream_order_max),
    IndexSpec("TotalStreamLength", "Total stream length ΣL", FAMILY_NETWORK,
              "Sum of all channel lengths.", "ΣL", "km", "Horton (1945)", DOI_HORTON_1945,
              ("network",), f.total_stream_length,
              notes="GSV-Studio labelled this value km but returned m."),
    IndexSpec("NumStreams", "Number of streams ΣNu", FAMILY_NETWORK,
              "Number of Strahler streams of all orders.", "ΣNu", "",
              "Strahler (1952)", DOI_STRAHLER_1952, ("network",), f.stream_count,
              notes="Streams are counted as Strahler defines them (a run of constant order), "
                    "not as vector segments. Used by Fs, Dt and Rb."),
    IndexSpec("TotalNumSegments", "Number of links", FAMILY_NETWORK,
              "Number of channel links (segments between junctions or heads).", "N links", "",
              "Shreve (1966)", "doi:10.1086/627073", ("network",), f.link_count,
              notes="Kept for comparison with GSV-Studio, which used segments where Horton's "
                    "laws need streams."),
    IndexSpec(
        "RB", "Bifurcation ratio Rb", FAMILY_NETWORK,
        "Ratio of the number of streams of one order to the next. 3–5 is typical of "
        "undisturbed networks; high values suggest structural control.",
        "Rb = Nu / Nu+1", "", "Horton (1945); Strahler (1964)", DOI_HORTON_1945,
        ("network",), f.bifurcation_ratio, parameters=(_horton_method(),),
        classifications=(
            Classification("RB_Strahler_1964", "Strahler (1964)", "Strahler (1964)",
                           "Handbook of Applied Hydrology, section 4-II", rules(
                               (-INF, 2.0, "Very low"), (2.0, 3.0, "Low"),
                               (3.0, 5.0, "Normal (undisturbed network)"),
                               (5.0, INF, "High (structural control likely)"))),
            Classification("RB_Das_2018", "Das (2018)", "Das (2018)", "reference to be checked (GSV-Studio)", rules(
                (-INF, 1.7, "Class 1"), (1.7, 2.2, "Class 2"), (2.2, INF, "Class 3"))),
        ),
        default_classification="RB_Strahler_1964",
    ),
    IndexSpec(
        "RL", "Length ratio Rl", FAMILY_NETWORK,
        "Ratio of the mean stream length of one order to the previous order.",
        "Rl = L̄u / L̄u−1", "", "Horton (1945)", DOI_HORTON_1945,
        ("network",), f.length_ratio, parameters=(_horton_method(),),
        classifications=(
            Classification("RL_Horton", "Horton (1945)", "Horton (1945); Strahler (1964)", DOI_HORTON_1945, rules(
                (-INF, 1.5, "Relatively low"), (1.5, 3.5, "Typical"), (3.5, INF, "Relatively high"))),
        ),
        default_classification="RL_Horton",
    ),
    IndexSpec(
        "RA", "Area ratio Ra", FAMILY_NETWORK,
        "Ratio of the mean drainage area of basins of one order to the previous order.",
        "Ra = Āu / Āu−1", "", "Schumm (1956)", DOI_SCHUMM_1956,
        ("network",), f.area_ratio, parameters=(_horton_method(),),
        classifications=(
            Classification("RA_Schumm", "Schumm (1956)", "Schumm (1956); Strahler (1964)", DOI_SCHUMM_1956, rules(
                (-INF, 3.0, "Relatively low"), (3.0, 6.0, "Typical"), (6.0, INF, "Relatively high"))),
        ),
        default_classification="RA_Schumm",
        notes="Needs drainage areas: available with the DEM-derived network, or with a vector "
              "network when the DEM routing is also computed.",
    ),
    IndexSpec(
        "Dd", "Drainage density Dd", FAMILY_NETWORK,
        "Channel length per unit area: how finely the landscape is dissected.",
        "Dd = ΣL / A", "km/km²", "Horton (1932, 1945)", DOI_HORTON_1945,
        ("network", "geometry"), f.drainage_density,
        classifications=(
            Classification("Dd_General", "General classes", "after Horton (1945); Strahler (1964)", DOI_HORTON_1945,
                           rules((-INF, 2.0, "Low"), (2.0, 5.0, "Moderate"), (5.0, INF, "High"))),
            Classification("Dd_Shukla_2013", "Shukla et al. (2013)", "Shukla et al. (2013)",
                           "doi:10.1007/s12517-012-0680-y", rules(
                               (-INF, 0.4, "Low"), (0.4, 0.6, "Moderate"), (0.6, INF, "High"))),
        ),
        default_classification="Dd_General",
        notes="Dd depends on the channel-initiation threshold (DEM network) or on the "
              "mapping scale (vector network); keep it constant across a study.",
    ),
    IndexSpec(
        "Fs", "Stream frequency Fs", FAMILY_NETWORK,
        "Number of streams per unit area.", "Fs = ΣNu / A", "km⁻²", "Horton (1932)", DOI_HORTON_1945,
        ("network", "geometry"), f.stream_frequency,
        classifications=(
            Classification("Fs_General", "General classes", "after Horton (1945)", DOI_HORTON_1945, rules(
                (-INF, 0.5, "Very low"), (0.5, 2.0, "Low to moderate"), (2.0, 5.0, "High"), (5.0, INF, "Very high"))),
            Classification("Fs_Selvan_2011", "Selvan et al. (2011)", "Selvan et al. (2011)",
                           "doi:10.1007/s12517-010-0219-7 (to be checked)", rules(
                               (-INF, 3.0, "Low"), (3.0, 4.0, "Moderate"), (4.0, 5.0, "High"), (5.0, INF, "Very high"))),
        ),
        default_classification="Fs_General",
    ),
    IndexSpec(
        "Dt", "Drainage texture Dt", FAMILY_NETWORK,
        "Number of streams per unit length of perimeter.", "Dt = ΣNu / P", "km⁻¹",
        "Smith (1950); Horton (1945)", "doi:10.2475/ajs.248.9.655",
        ("network", "geometry"), f.drainage_texture,
        classifications=(
            Classification("Dt_Smith_1950", "Smith (1950), per km", "Smith (1950)", "doi:10.2475/ajs.248.9.655", rules(
                (-INF, 2.0, "Very coarse"), (2.0, 6.0, "Coarse"), (6.0, 9.0, "Moderate"), (9.0, INF, "Fine"))),
        ),
        default_classification="Dt_Smith_1950",
    ),
    IndexSpec(
        "Di", "Drainage intensity Di", FAMILY_NETWORK,
        "Combination of stream frequency and drainage density.",
        "Di = Fs × Dd  (Faniran: Fs / Dd)", "", "Faniran (1968)", "Faniran (1968), Nigerian Geographical Journal",
        ("network", "geometry"), f.drainage_intensity,
        parameters=(ParameterSpec(
            "formula", "Formula", "choice", "Fs × Dd (GSV-Studio)",
            help="GSV-Studio computes Fs × Dd. Faniran (1968) is usually cited with Di = Fs / Dd. "
                 "Both are offered until the source is checked (doc/07).",
            choices=("Fs × Dd (GSV-Studio)", "Fs / Dd (Faniran 1968)")),),
        classifications=(
            Classification("Di_Faniran_1968", "Faniran (1968)", "Faniran (1968)", "—", rules(
                (-INF, 0.25, "Low"), (0.25, 0.5, "Moderate"), (0.5, INF, "High")),
                description="Thresholds suit the Fs / Dd form."),
        ),
        default_classification="Di_Faniran_1968",
    ),
    IndexSpec(
        "Lg", "Length of overland flow Lg", FAMILY_NETWORK,
        "Mean distance water travels over the surface before reaching a channel.",
        "Lg = 1 / (2·Dd)", "km", "Horton (1945)", DOI_HORTON_1945,
        ("network", "geometry"), f.overland_flow_length,
        classifications=(
            Classification("Lg_General", "Horton (1945)", "Horton (1945)", DOI_HORTON_1945, rules(
                (-INF, 0.4, "Short (dense network)"), (0.4, 0.8, "Moderate"), (0.8, INF, "Long (sparse network)"))),
        ),
        default_classification="Lg_General",
    ),
    IndexSpec(
        "C", "Constant of channel maintenance C", FAMILY_NETWORK,
        "Area needed to sustain one unit length of channel.", "C = 1 / Dd", "km²/km",
        "Schumm (1956)", DOI_SCHUMM_1956, ("network", "geometry"), f.channel_maintenance,
        classifications=(
            Classification("C_Schumm_1956", "Schumm (1956)", "Schumm (1956)", DOI_SCHUMM_1956, rules(
                (-INF, 0.6, "Low"), (0.6, 1.2, "Moderate"), (1.2, INF, "High"))),
        ),
        default_classification="C_Schumm_1956",
    ),
    IndexSpec(
        "Lar", "Hack's law ratio", FAMILY_NETWORK,
        "Observed main-stream length divided by the length predicted by Hack's law.",
        "Lc / (1.4·A^0.6)  [miles]", "", "Hack (1957)", "doi:10.3133/pp294B",
        ("channel", "geometry", "network"), f.length_area_ratio,
        parameters=(ParameterSpec(
            "formula", "Formula", "choice", "Main stream / Hack's law (miles)",
            help="Hack's law holds in miles and square miles for the main stream. GSV-Studio "
                 "compared the total network length with 1.4·A^0.6 in km/km².",
            choices=("Main stream / Hack's law (miles)",
                     "Total network length / 1.4·A^0.6 km (GSV-Studio legacy)")),),
        classifications=(
            Classification("Lar_Ratio", "Ratio reading", "—", "—", rules(
                (-INF, 0.8, "Shorter than predicted"), (0.8, 1.2, "As predicted"), (1.2, INF, "Longer than predicted"))),
        ),
        default_classification="Lar_Ratio",
    ),
    IndexSpec(
        "LengthArea", "Length-area texture La", FAMILY_NETWORK,
        "Length of overland flow per unit area (GSV-Studio).", "La = Lg / A", "km⁻¹",
        "after Schumm (1956)", DOI_SCHUMM_1956, ("network", "geometry"), f.length_area_texture,
        experimental=True,
        notes="Kept from GSV-Studio for continuity; its physical meaning is weak.",
    ),
    IndexSpec(
        "Hd", "Ruggedness number Hd", FAMILY_NETWORK,
        "Product of relief and drainage density: high where steep slopes are densely dissected.",
        "Hd = TR(km) × Dd", "", "Strahler (1958)", "doi:10.1130/0016-7606(1958)69[279:DAATFE]2.0.CO;2",
        ("network", "geometry", "elevation"), f.ruggedness_number,
        classifications=(
            Classification("Hd_Strahler_1958", "Strahler (1958)", "Strahler (1958)",
                           "doi:10.1130/0016-7606(1958)69[279:DAATFE]2.0.CO;2", rules(
                               (-INF, 0.1, "Very low"), (0.1, 0.5, "Low"), (0.5, 1.0, "Moderate"),
                               (1.0, 4.0, "High"), (4.0, INF, "Very high (badlands)"))),
        ),
        default_classification="Hd_Strahler_1958",
    ),

    # =======================================================================
    # Main channel
    # =======================================================================
    IndexSpec(
        "HSI", "Channel sinuosity", FAMILY_CHANNEL,
        "Trunk-stream length over the straight distance between its ends.",
        "S = Lc / Lv", "", "Schumm (1963); Mueller (1968)", "doi:10.1111/j.1467-8306.1968.tb00650.x",
        ("channel",), f.channel_sinuosity,
        classifications=(
            Classification("S_Schumm_1963", "Schumm (1963)", "Schumm (1963)",
                           "doi:10.1130/0016-7606(1963)74[1089:ASICOP]2.0.CO;2", rules(
                               (1.0, 1.06, "Straight"), (1.06, 1.3, "Transitional"),
                               (1.3, 2.0, "Regular meanders"), (2.0, INF, "Tortuous"))),
            Classification("S_Leopold_1964", "Leopold, Wolman & Miller (1964)", "Leopold et al. (1964)",
                           "Fluvial Processes in Geomorphology, p. 281", rules(
                               (1.0, 1.05, "Almost straight"), (1.05, 1.5, "Sinuous"), (1.5, INF, "Meandering"))),
        ),
        default_classification="S_Schumm_1963",
        notes="GSV-Studio named this ratio Mueller's hydraulic sinuosity index; Mueller's HSI "
              "needs a separate valley length, so the plain sinuosity is reported.",
    ),
    IndexSpec(
        "TSI", "Valley / basin length ratio", FAMILY_CHANNEL,
        "Straight trunk-valley length over basin length (GSV-Studio 'TSI').",
        "Lv / Lb", "", "after Mueller (1968)", "doi:10.1111/j.1467-8306.1968.tb00650.x",
        ("channel", "geometry"), f.valley_basin_ratio, experimental=True,
        notes="Mueller's topographic sinuosity index is (VI − 1)/(CI − 1); this ratio is the "
              "GSV-Studio quantity and is kept for continuity.",
    ),
    IndexSpec("MASC", "Mean axial slope of the channel", FAMILY_CHANNEL,
              "Elevation drop of the trunk stream divided by its length.",
              "MASC = (Hmax − Hmin)ch / Lc", "m/m", "Huyghe et al. (2004)", "doi:10.1130/G20000.1",
              ("channel",), f.mean_axial_slope),
    IndexSpec("HG", "Hydraulic gradient", FAMILY_CHANNEL,
              "Mean channel gradient in percent (MASC × 100).", "HG = MASC × 100", "%",
              "Grohmann (2004)", "doi:10.1016/j.cageo.2004.08.002", ("channel",), f.hydraulic_gradient),
    IndexSpec(
        "Ksn", "Normalised steepness ksn", FAMILY_CHANNEL,
        "Channel slope normalised for drainage area: rock uplift / erodibility proxy.",
        "ksn = S · A^θref", "m^(2θ)", "Wobus et al. (2006); Kirby & Whipple (2012)",
        "doi:10.1016/j.jsg.2012.07.009", ("channel",), f.normalized_steepness,
        parameters=(_theta(),
                    ParameterSpec("reach_length_m", "Reach length", "float", 250.0,
                                  help="Length over which the channel slope is measured (GSV-Studio: 250 m).",
                                  minimum=30.0, maximum=5000.0, step=50.0, decimals=0, unit="m"),
                    _statistic("Mean")),
        classifications=(
            Classification("Ksn_Relative", "Relative reading (θref = 0.45)", "after Kirby & Whipple (2012)",
                           "doi:10.1016/j.jsg.2012.07.009", rules(
                               (-INF, 50, "Low"), (50, 150, "Moderate"), (150, 300, "High"), (300, INF, "Very high")),
                           description="Absolute ksn depends on θref and A0; compare basins of one study."),
        ),
        default_classification="Ksn_Relative",
    ),
    IndexSpec(
        "Chi", "Chi (χ) of the trunk stream", FAMILY_CHANNEL,
        "Integral transform of the profile that linearises steady-state rivers.",
        "χ = ∫ (A0/A)^θref dx", "m", "Perron & Royden (2013)", "doi:10.1002/esp.3302",
        ("channel",), f.chi_index,
        parameters=(_theta(),
                    ParameterSpec("reference_area_m2", "Reference area A0", "float", 1.0,
                                  help="A0 = 1 m² makes the Z–χ slope equal to ksn.",
                                  minimum=1.0, maximum=1e8, step=1.0, decimals=0, unit="m²"),
                    ParameterSpec("statistic", "Reported value", "choice", "Chi at source",
                                  help="GSV-Studio's default (χ at the outlet) is always 0 by construction.",
                                  choices=("Chi at source", "Mean", "Median", "Z-χ slope (k_χ)"))),
    ),

    # =======================================================================
    # Active tectonics
    # =======================================================================
    IndexSpec(
        "SL", "Stream length-gradient index SL", FAMILY_TECTONIC,
        "Channel gradient times distance from the divide: stream power anomalies from "
        "uplift or resistant rocks.",
        "SL = (ΔH/ΔL) · L", "m", "Hack (1973)", DOI_HACK_1973,
        ("channel",), f.stream_length_gradient,
        parameters=(ParameterSpec("reach_length_m", "Reach length ΔL", "float", 200.0,
                                  help="Length of the reaches along the trunk stream. GSV-Studio: 100 m "
                                       "(registry) / 1000 m (code); 200 m follows recent Iat studies "
                                       "(e.g. Tarı & Tüysüz, 2016). Use at least 3–5 DEM cells.",
                                  minimum=30.0, maximum=5000.0, step=50.0, decimals=0, unit="m"),
                    _statistic("Mean")),
        classifications=(
            _el_hamdouni("SL_ElHamdouni_2008", "El Hamdouni et al. (2008) – Iat", rules(
                (-INF, 300.0, "Class 3: SL < 300 (low activity)", 3),
                (300.0, 500.0, "Class 2: 300–500", 2),
                (500.0, INF, "Class 1: SL ≥ 500 (high activity)", 1)),
                description="GSV-Studio used 100/300 thresholds under this name; El Hamdouni et al. "
                            "(2008) use 300/500."),
            Classification("SL_Seeber_Gornitz_1983", "Seeber & Gornitz (1983)", "Seeber & Gornitz (1983)",
                           "doi:10.1016/0040-1951(83)90201-9", rules(
                               (-INF, 200.0, "Low", 3), (200.0, 500.0, "Moderate", 2), (500.0, INF, "High", 1))),
            # Kept so that results produced with GSV-Studio can be reproduced
            # exactly; not the values of the paper it cited (see doc/07, D-S2).
            Classification("SL_GSV_legacy", "GSV-Studio legacy (100 / 300)", "GSV-Studio",
                           "GSV-Studio registry", rules(
                               (-INF, 100.0, "Class 3: SL < 100", 3), (100.0, 300.0, "Class 2: 100–300", 2),
                               (300.0, INF, "Class 1: SL ≥ 300", 1)),
                           description="Thresholds used by GSV-Studio under the El Hamdouni et al. (2008) "
                                       "name; the paper uses 300 / 500."),
        ),
        default_classification="SL_ElHamdouni_2008",
    ),
    IndexSpec(
        "SLK", "Normalised SL (SL/K)", FAMILY_TECTONIC,
        "SL divided by the gradient of a graded river with the same drop and length; "
        "values above 2 are anomalies.",
        "SL/K, K = (Hsrc − Hout) / ln(Lc)", "", "Seeber & Gornitz (1983)",
        "doi:10.1016/0040-1951(83)90201-9", ("channel",), f.normalized_sl,
        parameters=(ParameterSpec("reach_length_m", "Reach length ΔL", "float", 200.0,
                                  help="Same reach length as SL.", minimum=30.0, maximum=5000.0,
                                  step=50.0, decimals=0, unit="m"),
                    _statistic("Mean")),
        classifications=(
            Classification("SLK_Seeber_Gornitz", "Seeber & Gornitz (1983)", "Seeber & Gornitz (1983)",
                           "doi:10.1016/0040-1951(83)90201-9", rules(
                               (-INF, 2.0, "Normal"), (2.0, 10.0, "Anomalous"), (10.0, INF, "Strongly anomalous"))),
        ),
        default_classification="SLK_Seeber_Gornitz",
    ),
    IndexSpec(
        "AF", "Asymmetry factor AF", FAMILY_TECTONIC,
        "Share of the basin on the right of the trunk stream (looking downstream). 50 for a "
        "symmetric basin; departures indicate tilting.",
        "AF = 100 · Ar / At", "%", "Hare & Gardner (1985)", "doi:10.1016/0169-555X(85)90021-7",
        ("asymmetry", "geometry"), f.asymmetry_factor,
        parameters=(ParameterSpec(
            "side", "Bank measured", "choice", "Right bank (looking downstream)",
            help="Hare & Gardner (1985) measure the right bank looking downstream.",
            choices=("Right bank (looking downstream)", "Left bank (looking downstream)")),),
        classifications=(
            _el_hamdouni("AF_ElHamdouni_2008", "El Hamdouni et al. (2008) – Iat", rules(
                (-INF, 7.0, "Class 3: |AF−50| < 7 (symmetric)", 3),
                (7.0, 15.0, "Class 2: 7–15", 2),
                (15.0, INF, "Class 1: |AF−50| ≥ 15 (asymmetric)", 1)),
                transform="abs_minus_50",
                description="Classified on |AF − 50| as in El Hamdouni et al. (2008)."),
            Classification("AF_Hare_Gardner_1985", "Hare & Gardner (1985) – direction", "Hare & Gardner (1985)",
                           "doi:10.1016/0169-555X(85)90021-7", rules(
                               (-INF, 40.0, "Strong, left bank dominant"), (40.0, 48.0, "Moderate, left bank dominant"),
                               (48.0, 52.0, "Symmetric"), (52.0, 60.0, "Moderate, right bank dominant"),
                               (60.0, INF, "Strong, right bank dominant"))),
            Classification("AF_Balla_2023", "Balla Ateba et al. (2023)", "Balla Ateba et al. (2023)", DOI_BALLA_2023,
                           rules((-INF, 7.0, "Low activity", 3), (7.0, 15.0, "Moderate activity", 2),
                                 (15.0, INF, "High activity", 1)),
                           transform="abs_minus_50",
                           description="GSV-Studio applied these thresholds to AF itself; they are "
                                       "meant for |AF − 50|."),
        ),
        default_classification="AF_ElHamdouni_2008",
        notes="The basin is split by the trunk stream extended straight beyond its ends.",
    ),
    IndexSpec(
        "TTSF", "Transverse topographic symmetry T", FAMILY_TECTONIC,
        "Offset of the trunk stream from the basin midline relative to the half-width "
        "(0 symmetric, 1 at the divide).",
        "T = Da / Dd", "", "Cox (1994)", DOI_COX_1994, ("sections",), f.transverse_symmetry,
        parameters=(
            ParameterSpec("sections", "Number of sections", "int", 10,
                          help="Sections evenly spaced between 5% and 95% of the channel length.",
                          minimum=3, maximum=100, step=1, decimals=0),
            ParameterSpec("dd_method", "Half-width Dd", "choice", "Channel side (Cox 1994)",
                          help="Cox (1994) measures Dd towards the side where the channel lies. "
                               "GSV-Studio defaulted to the larger half-width.",
                          choices=("Channel side (Cox 1994)", "Maximum half-width (GSV-Studio)",
                                   "Minimum half-width", "Mean half-width")),
        ),
        classifications=(
            Classification("T_Cox_1994", "Cox (1994)", "Cox (1994)", DOI_COX_1994, rules(
                (0.0, 0.2, "Nearly symmetric", 3), (0.2, 0.4, "Moderately asymmetric", 2),
                (0.4, INF, "Strongly asymmetric", 1)),
                description="GSV-Studio's rules overlapped (−∞–0.1 then 0–0.2); merged into three classes."),
        ),
        default_classification="T_Cox_1994",
        notes="The midline is the principal axis of the basin area (PCA of its cells).",
    ),
    IndexSpec(
        "TTSF_Azimuth", "Direction of channel shift (T vector)", FAMILY_TECTONIC,
        "Mean azimuth of the displacement of the trunk stream from the midline, i.e. the "
        "direction of ground tilting suggested by Cox (1994).",
        "atan2(Σ T·sin α, Σ T·cos α)", "°", "Cox (1994)", DOI_COX_1994,
        ("sections",), f.transverse_symmetry_azimuth,
        parameters=(
            ParameterSpec("sections", "Number of sections", "int", 10,
                          help="Same sections as T.", minimum=3, maximum=100, step=1, decimals=0),
            ParameterSpec("dd_method", "Half-width Dd", "choice", "Channel side (Cox 1994)",
                          help="Same choice as T.",
                          choices=("Channel side (Cox 1994)", "Maximum half-width (GSV-Studio)",
                                   "Minimum half-width", "Mean half-width")),
        ),
    ),
    IndexSpec(
        "VF", "Valley floor width-to-height ratio Vf", FAMILY_TECTONIC,
        "Width of the valley floor over the mean height of the valley sides. V-shaped "
        "valleys (Vf < 0.5) indicate active incision.",
        "Vf = 2·Vfw / [(Eld − Esc) + (Erd − Esc)]", "", "Bull & McFadden (1977)",
        "doi:10.4324/9780429299230-5", ("valley",), f.valley_floor_ratio,
        parameters=(
            ParameterSpec("profiles", "Number of profiles", "int", 10,
                          help="Cross-profiles evenly spaced along the trunk stream.", minimum=1,
                          maximum=100, step=1, decimals=0),
            ParameterSpec("start_fraction", "First profile at", "float", 0.05,
                          help="Position of the first profile as a fraction of the channel length "
                               "from the source. El Hamdouni et al. (2008) measure Vf near the "
                               "mountain front, i.e. close to the outlet: use e.g. 0.8–0.95.",
                          minimum=0.0, maximum=1.0, step=0.05, decimals=2),
            ParameterSpec("end_fraction", "Last profile at", "float", 0.95,
                          help="Position of the last profile (fraction from the source).",
                          minimum=0.0, maximum=1.0, step=0.05, decimals=2),
            ParameterSpec("profile_width_m", "Profile width", "float", 500.0,
                          help="Total width of each cross-profile. It must reach the valley "
                               "divides on both sides (GSV-Studio default: 500 m).",
                          minimum=50.0, maximum=20000.0, step=50.0, decimals=0, unit="m"),
            ParameterSpec("sampling_m", "Sampling step", "float", 0.0,
                          help="Distance between samples along a profile; 0 = half a DEM cell.",
                          minimum=0.0, maximum=500.0, step=1.0, decimals=1, unit="m"),
            ParameterSpec("smoothing_sigma", "Smoothing σ", "float", 1.5,
                          help="Gaussian smoothing of the profile, in samples.",
                          minimum=0.0, maximum=20.0, step=0.5, decimals=1),
            ParameterSpec("floor_method", "Valley-floor detection", "choice", "Flat-floor slope threshold",
                          help="How the edges of the valley floor are found (both from GSV-Studio).",
                          choices=("Flat-floor slope threshold", "Foot-of-slope search")),
            ParameterSpec("max_flat_slope_deg", "Max floor slope", "float", 2.0,
                          help="Slopes below this angle belong to the valley floor.",
                          minimum=0.1, maximum=30.0, step=0.5, decimals=1, unit="°"),
            ParameterSpec("basal_slope_threshold_deg", "Foot-of-slope threshold", "float", 3.0,
                          help="Minimum slope of the valley side (foot-of-slope search).",
                          minimum=0.1, maximum=45.0, step=0.5, decimals=1, unit="°"),
            ParameterSpec("consistency_points", "Consistent steep points", "int", 2,
                          help="Number of consecutive steep samples that end the valley floor.",
                          minimum=1, maximum=20, step=1, decimals=0),
            ParameterSpec("min_height_diff_m", "Min rise at floor edge", "float", 0.5,
                          help="Minimum height above the channel where the floor may end.",
                          minimum=0.0, maximum=100.0, step=0.5, decimals=1, unit="m"),
            ParameterSpec("min_half_width_m", "Min half floor width", "float", 1.0,
                          help="Smallest half-width accepted for the valley floor.",
                          minimum=0.0, maximum=1000.0, step=1.0, decimals=1, unit="m"),
            ParameterSpec("divide_method", "Divide elevation (Eld, Erd)", "choice",
                          "Adaptive shoulder detection",
                          help="How the valley-side divide elevations are picked (GSV-Studio default: "
                               "adaptive shoulder detection).",
                          choices=("Adaptive shoulder detection", "Percentile of side elevations")),
            ParameterSpec("percentile", "Side percentile", "float", 90.0,
                          help="Percentile used by the percentile method.", minimum=50.0, maximum=100.0,
                          step=1.0, decimals=0),
            ParameterSpec("shoulder_min_rise_m", "Shoulder min rise", "float", 5.0,
                          help="Minimum height above the channel for a shoulder.", minimum=0.0,
                          maximum=1000.0, step=1.0, decimals=1, unit="m"),
            ParameterSpec("shoulder_slope_factor", "Shoulder slope factor", "float", 0.3,
                          help="Fraction of the side slope variability defining a gentle shoulder.",
                          minimum=0.0, maximum=5.0, step=0.05, decimals=2),
            ParameterSpec("shoulder_min_slope_deg", "Shoulder min slope", "float", 1.0,
                          help="Lower bound of the shoulder slope threshold.", minimum=0.0,
                          maximum=30.0, step=0.5, decimals=1, unit="°"),
            _statistic("Median"),
        ),
        classifications=(
            _el_hamdouni("VF_ElHamdouni_2008", "El Hamdouni et al. (2008) – Iat", rules(
                (-INF, 0.5, "Class 1: V-shaped (Vf < 0.5)", 1),
                (0.5, 1.0, "Class 2: 0.5–1", 2),
                (1.0, INF, "Class 3: U-shaped (Vf ≥ 1)", 3))),
            Classification("VF_Bull_1977", "Bull & McFadden (1977)", "Bull & McFadden (1977)",
                           "doi:10.4324/9780429299230-5", rules(
                               (-INF, 0.5, "V-shaped valley", 1), (0.5, 1.0, "V to U transition", 2),
                               (1.0, INF, "U-shaped valley", 3))),
        ),
        default_classification="VF_ElHamdouni_2008",
        notes="GSV-Studio read its Vf parameters under the key 'Vf' while the registry key is "
              "'VF', so user settings were ignored; they are honoured here.",
    ),

    # =======================================================================
    # Hydrological terrain
    # =======================================================================
    IndexSpec("TWI", "Topographic wetness index TWI", FAMILY_TERRAIN,
              "Tendency of each cell to accumulate water; basin statistic.",
              "TWI = ln(a / tan β)", "", "Beven & Kirkby (1979)", "doi:10.1080/02626667909491834",
              ("terrain",), f.topographic_wetness, parameters=(_statistic("Mean"),),
              notes="a = upslope area per unit contour width; tan β floored at 0.001."),
    IndexSpec("SPI", "Stream power index SPI", FAMILY_TERRAIN,
              "Erosive power of overland flow; basin statistic.", "SPI = a · tan β", "m",
              "Moore et al. (1991)", "doi:10.1002/hyp.3360050103",
              ("terrain",), f.stream_power, parameters=(_statistic("Mean"),)),
    IndexSpec("STI", "Sediment transport index STI", FAMILY_TERRAIN,
              "Transport capacity of overland flow (LS factor form); basin statistic.",
              "STI = (a/22.13)^0.6 · (sin β / 0.0896)^1.3", "", "Moore & Burch (1986)",
              "doi:10.2136/sssaj1986.03615995005000050042x",
              ("terrain",), f.sediment_transport, parameters=(_statistic("Mean"),)),

    # =======================================================================
    # Erosion (experimental, kept from GSV-Studio)
    # =======================================================================
    IndexSpec("De", "Denudation rate D", FAMILY_EROSION,
              "Empirical denudation rate from mean basin slope (GSV-Studio).",
              "D = 28 · e^(0.00774·S)", "", "Reinhardt et al. (2007), via GSV-Studio",
              "doi:10.1029/2006TC001954", ("terrain",), f.denudation_rate, experimental=True,
              notes="Unit of S and of D to be confirmed in the source (doc/07)."),
    IndexSpec("Ebulk", "Bulk erosion Ebulk", FAMILY_EROSION,
              "GSV-Studio product of relief and denudation rate.", "Ebulk = TR × D", "",
              "Brocklehurst & Whipple (2002), via GSV-Studio", "doi:10.1016/S0169-555X(01)00069-1",
              ("terrain", "elevation"), f.bulk_erosion, experimental=True,
              notes="Not the volume of Brocklehurst & Whipple (2002); kept for comparison only."),
    IndexSpec("E", "Erosion rate E", FAMILY_EROSION,
              "Empirical erosion rate from basin relief (GSV-Studio).", "E = 1.4·10⁻⁶ · TR^1.8", "",
              "Portenga & Bierman (2011); Champagnac et al. (2014), via GSV-Studio",
              "doi:10.1130/B30559.1", ("elevation",), f.erosion_rate, experimental=True,
              notes="The relation uses local relief; TR is used as in GSV-Studio."),
)


INDEX_BY_KEY: Dict[str, IndexSpec] = {spec.key: spec for spec in SPECS}


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

PRESETS: Dict[str, Tuple[str, ...]] = {
    "Iat – El Hamdouni et al. (2008)": ("SL", "AF", "HI", "VF", "Bs"),
    "Horton–Strahler morphometry": ("StreamOrderMax", "NumStreams", "TotalStreamLength", "RB", "RL",
                                    "RA", "Dd", "Fs", "Dt", "Lg", "C"),
    "Basin shape": ("BasinArea", "BasinPerimeter", "BasinLength", "Rf", "Re", "Rc",
                    "Lemniscate", "Compactness", "Bs"),
    "Relief & hypsometry": ("AltitudeMin", "AltitudeMax", "AltitudeMean", "AltitudeMedianHm", "TR",
                            "TD", "TRD", "Rra", "Rr", "MRN", "HI", "SlopeMeanDegree"),
    "Active tectonics": ("SL", "SLK", "AF", "TTSF", "TTSF_Azimuth", "VF", "Bs", "HI", "Ksn"),
    "Channel profile": ("HSI", "MASC", "Ksn", "Chi", "SL", "SLK"),
    "Hydrological terrain": ("TWI", "SPI", "STI"),
}


def families() -> List[Tuple[str, List[IndexSpec]]]:
    """Catalogue grouped by family, in display order."""
    return [(family, [s for s in SPECS if s.family == family]) for family in FAMILIES]


def get(key: str) -> IndexSpec:
    """Return the spec of ``key`` (KeyError when unknown)."""
    return INDEX_BY_KEY[key]
