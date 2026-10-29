# 01 — Audit complet de RockMorph

> Audit en lecture seule. Les références `fichier:ligne` renvoient au commit `76b17b9`.
> Certaines affirmations ont été vérifiées numériquement hors de QGIS, avec NumPy et des stubs
> pour `qgis`/`PyQt5`. C'est précisé à chaque fois (« **vérifié** »). Les points notés
> « **à vérifier** » n'ont pas pu être testés sans QGIS.

---

## 1. Vue d'ensemble

| Élément | Valeur |
|---|---|
| Type | Extension QGIS (Python), `qgisMinimumVersion=3.36`, `experimental=True` |
| Taille | ~19 000 lignes Python + ~5 000 lignes HTML/JS (hors bibliothèques) |
| Dépendances Python | NumPy, GDAL/OGR, SciPy (obligatoire pour le digitaliseur et le SMF automatique, optionnel ailleurs), PyQt5 + **QtWebEngine**, GRASS via Processing (FAC automatique, HydroFlow) |
| Dépendances JS (embarquées) | plotly.js 2.27.0 (3,6 Mo), three.js r128 (2021), TrackballControls, qwebchannel |
| Tests | aucun |
| Documentation utilisateur | aucune (pas de README, pas de LICENSE, pas d'aide en ligne) |
| Historique | 50 commits visibles (clone superficiel), du 03/07/2026 au 06/07/2026 |

### 1.1 Architecture

```
plugin.py ──► ui/main_dock.py (RockMorphDock)
                 │  registre : base/registry.py (TOOL_REGISTRY, chargement paresseux)
                 ▼
          tools/<outil>/panel.py  (hérite de base/base_panel.BasePanel)
                 │   ├─ QWebEngineView + QWebChannel (web/<outil>.html, Plotly ou three.js)
                 │   └─ ComputeWorker (QThread) ──► tools/<outil>/engine.py (hérite de BaseEngine)
                 ▼
          core/   raster.py (RasterReader), sampling.py (SwathSampler), hydro.py (rivière principale,
                  échantillonnage), watershed.py (D8, CSR, BFS), digitizer.py (k-means, OBIA),
                  exporter.py (PNG/JPG/SVG/PDF/CSV/JSON/GPKG), utils.py (lissage)
          widgets/ composants Qt réutilisables (couleurs, styles de lignes, export, sélecteur de sortie)
```

Le principe est sain. Les moteurs ne contiennent aucune logique d'interface et renvoient des
`dict` sérialisables. Les panneaux lisent l'UI, lancent le moteur dans un thread et envoient le
résultat à la vue web. C'est le bon découpage pour ajouter ensuite un fournisseur Processing, des
tests unitaires et l'intégration de nouveaux indices.

### 1.2 Inventaire des outils

| Famille | Outil | Fichiers | Ce qu'il calcule | État |
|---|---|---|---|---|
| Structural | Rose Diagram | `tools/rose/` | Diagramme en rose (count/length/frequency), stats circulaires, entropie, anisotropie | ⚠️ stats circulaires fausses en mode axial (C9) |
| Structural | Geological Digitizer | `tools/digitizer/`, `core/digitizer.py` | Vectorisation d'une carte géologique scannée (k-means/OBIA par couleur, polygonisation) | ✅ fonctionnel, code mort présent |
| Fluvial | Fluvial Toolbox | `tools/fluvial/`, `core/hydro.py` | Rivière principale, SL, SLk, χ, ksn (continu + segments), θ local, knickpoints, profil d'équilibre de Hack | ❌ **bugs critiques** (C1–C5, C7, C8) |
| Fluvial | Mountain Front Sinuosity | `tools/smf/` | Smf (Lf/Lr, ligne de base PCA ou extrémités), détection automatique des fronts (MFSD) | ⚠️ seuils de classes non standard (C10) |
| Fluvial | Normalized Channel Profile | `tools/ncp/` | Profil normalisé, MaxC, dL, concavité % (équivalent de NProfiler) | ✅ correct, `np.trapz` (C6) |
| Fluvial | HydroFlow | `tools/hydroflow/` | Comblement des dépressions, FDR/FAC/bassins (GRASS), réseau, Strahler/Shreve/Horton | ⚠️ performance, ids GRASS (M5, M13) |
| Topographie | Swath Profile | `tools/swath/`, `core/sampling.py` | Min/max/moyenne/Q1/Q3, relief local, intégrale hypsométrique transverse | ✅ correct, plantage du lissage si NaN (M12) |
| Topographie | Hypsometric Curve | `tools/hypsometry/` | Courbe hypsométrique et HI par bassin, regroupement | ✅ correct, sans les moments statistiques (voir 05) |
| Topographie | Watershed Subdivision | `tools/watershed/`, `core/watershed.py` | Sous-bassins hiérarchiques à partir de FDR/FAC (D8 ESRI/GRASS/SAGA) | ⚠️ exutoire utilisateur non respecté (M8) |
| Topographie | Terrain Derivatives | `tools/terrainderivatives/` | Pente, exposition, ombrage (GDAL), TPI, TRI, openness +/−, SVF, sky illumination, SVF anisotrope, local dominance | ✅ correct, TPI/TRI mono-échelle |
| Visualisation | 3D Explorer | `tools/explorer3d/`, `web/js/explorer3d_render.js` | MNT 3D WebGL, drapage de vecteurs, ombrages GLSL, export PNG/PDF/HTML/STL | ✅ riche, dépend fortement de QWebEngine (voir 03) |

---

## 2. Points forts

1. **Architecture modulaire et extensible.** Le contrat `BaseEngine.compute(**kwargs) -> dict`, les hooks
   de `BasePanel` et le `TOOL_REGISTRY` avec chargement paresseux (`ui/main_dock.py:switch_to_tool`)
   rendent l'ajout d'un outil simple et n'affectent pas le temps de démarrage de QGIS.
2. **Le calcul ne bloque pas l'interface.** `ComputeWorker` (QThread) injecte un `progress_callback`
   dans les moteurs, qui ne connaissent pas Qt. Les erreurs sont attrapées et QGIS ne plante pas.
3. **Bonne rigueur géospatiale.** Reprojection systématique entre couches (`QgsCoordinateTransform`),
   longueurs géodésiques avec `QgsDistanceArea` sur l'ellipsoïde, réparation des géométries invalides
   (`makeValid`), détection des doublons, gestion du NoData.
4. **NumPy vectorisé dans plusieurs parties critiques.** D8 → indice aval (`core/watershed.py:build_downstream`),
   index amont CSR par `argsort` (`build_upstream_csr`), k-means sans tableau intermédiaire N×K×3
   (`core/digitizer.py`), solveurs d'horizon par décalage de tableaux (`terrainderivatives`).
5. **Export soigné.** Choix du DPI, SVG vectoriel (Plotly → SVG → QSvgRenderer/cairosvg), PDF, CSV avec
   séparateur adapté à la locale pour Excel (`;` en France), GeoPackage multicouche.
6. **Interactivité graphique ↔ carte.** Survol du profil → marqueur sur la carte (swath, fluvial), clic → sélection.
7. **Originalité scientifique.** Plusieurs fonctions ne se trouvent pas dans les outils publiés équivalents :
   - MFSD : détection automatique des fronts montagneux par squelettisation (`tools/smf/engine.py`) ;
   - digitaliseur de cartes géologiques par segmentation couleur ;
   - suite de visualisation du relief proche de RVT (SVF, SVF anisotrope, openness, local dominance, sky illumination) en NumPy pur ;
   - explorateur 3D avec export STL (impression 3D) et HTML autonome.
8. **Docstrings très détaillées**, avec les références bibliographiques (Hack 1973, Perron & Royden 2013,
   Kirby & Whipple 2001, Yokoyama 2002, Zakšek 2011…). C'est une bonne base pour la section Méthodes d'un article.
   Attention : plusieurs docstrings **ne décrivent plus le code** (voir §5).

---

## 3. Bugs critiques (faussent des résultats scientifiques)

### C1 — χ (chi) désaligné par rapport à l'altitude → chi-plot inversé, knickpoints et ksn faux — **vérifié**

- `tools/fluvial/engine.py:1046` : `_compute_chi` renvoie `chi_rev`, ordonné **embouchure → source**
  (le commentaire « Flip back to source→mouth order » n'est pas appliqué).
- `elev`, `dist`, `area` et `slope` sont ordonnés **source → embouchure** (bascule forcée dans `_compute_metrics`).
- `chi[i]` et `elev[i]` ne désignent donc **pas le même point**. Ensuite :
  - `_detect_knickpoints(chi, elev, …)` ajuste des droites sur des paires (χ, Z) qui ne correspondent pas
    → les knickpoints sont placés au mauvais endroit ;
  - `_compute_ksn_segments(chi, elev, …)` calcule Δz/Δχ sur des paires fausses → ksn par segment faux ;
  - `engine.py:689-692` inverse `chi` et `elev`, mais les deux tableaux n'avaient pas le même ordre au départ :
    le désalignement reste ;
  - `web/fluvial.html:518-521` trace la « droite d'équilibre » avec `z_mouth + ksn·(χmax − χ)`. Elle descend
    quand χ augmente. C'est la trace d'une compensation visuelle du bug : un vrai chi-plot monte (Z = Z_embouchure + ksn·χ).
- **Vérification numérique** : rivière synthétique à l'équilibre (θ = 0,45, ksn = 100, 400 points).
  La régression Z~χ sur les paires produites par le code donne une **pente de −82,9**. Avec des paires alignées, on obtient **+100,0**.
- **Correctif proposé (non appliqué)** : `return chi_rev[::-1]` dans `_compute_chi`, renvoyer tous les tableaux
  dans le même ordre (source → embouchure), supprimer la double inversion l.689-698 et corriger la droite
  d'équilibre JS en `z = z_mouth + ksn·χ`.

### C2 — Exports CSV et GeoPackage de l'outil fluvial : colonnes désalignées

- `tools/fluvial/panel.py:1104-1108` (CSV) et `:1309-1314` (GPKG « profile_points ») assemblent ligne par ligne
  `distances_m`, `elevations`, `sl`, `slk` (source → embouchure) avec `chi` et `ksn_profile` (ordre inversé, voir C1).
  Chaque ligne exportée mélange donc deux points différents.
- **Conséquence** : un tableau ou une carte faits à partir de ces exports contiennent des valeurs de χ et de ksn
  rattachées à de mauvaises positions.

### C3 — Géométrie de la rivière principale orientée embouchure → source, distances mesurées depuis la source

- `core/hydro.py:302-327` : le tracé part de l'exutoire et remonte (`final_points.extend(pts[::-1])`). La géométrie
  renvoyée commence donc à l'**embouchure**, alors que la docstring de `sample_river_profile` la dit orientée source → embouchure.
- Les exports GPKG (`tools/fluvial/panel.py:1262` `curveSubstring`, `:1287` et `:1310` `interpolate`) positionnent
  les points avec des distances **mesurées depuis la source**. Résultat : les segments ksn, les knickpoints et les
  points de profil sont placés **en miroir** le long de la rivière.
- De plus, `interpolate()` et `curveSubstring()` travaillent en **unités du SCR**. En SCR géographique, la distance
  en mètres est interprétée en degrés.
- **Correctif proposé** : inverser `final_points` avant de construire la géométrie, et en SCR géographique,
  interpoler dans un SCR métrique.

### C4 — Mode ksn « regression » : plantage dès le 2ᵉ segment — **vérifié**

- `tools/fluvial/engine.py:2120` : `slope = coeffs[0]` écrase le **tableau** `slope` passé en argument.
  À l'itération suivante, `slope[i0:i1]` lève `IndexError: invalid index to scalar variable`.
- L'exception est attrapée par bassin (`engine.py:417`). Le bassin est alors **silencieusement écarté**
  (« skipped ») dès qu'il a au moins un knickpoint.
- **Correctif** : renommer la variable locale (`reg_slope`).

### C5 — Curseur θ_ref : le recalcul « en direct » ne fonctionne pas

- `tools/fluvial/panel.py:647` appelle `_compute_ksn_loglog_V3(..., window_size=None)`.
- `tools/fluvial/engine.py:1419` évalue `if n > window_size or not window_size`. En Python 3, `n > None`
  lève `TypeError` avant d'arriver au `or`. Le slot Qt échoue et rien n'est mis à jour.
- Même une fois réparé, le recalcul stockerait `chi` et `ksn_profile` dans un ordre différent du calcul initial,
  remplacerait les NaN du ksn par **0** (`nan_to_num`, l.649), ce qui fausse les courbes et la moyenne,
  et ne mettrait pas à jour la valeur de χ des knickpoints.

### C6 — `np.trapz` supprimé dans NumPy 2.x récent — **vérifié**

- Utilisé dans `tools/fluvial/engine.py:1040` (χ), `tools/ncp/engine.py:234` (concavité) et
  `tools/hypsometry/engine.py:187` (HI).
- `np.trapz` a été déprécié dans NumPy 2.0 au profit de `np.trapezoid`. Il est **absent** de NumPy 2.5.3
  (testé). Les trois outils planteront sur les prochaines distributions QGIS qui embarqueront un NumPy récent.
- **Correctif** : `trapezoid = getattr(np, "trapezoid", None) or np.trapz`, défini une fois dans `core/utils.py`.

### C7 — Détection des knickpoints : nombre incontrôlé, mauvais tri, coût en O(n²) — **vérifié**

- `tools/fluvial/engine.py:1734-1737` : la récursion passe `remaining - 1` aux **deux** branches.
  Avec `n_knick = 3`, on obtient jusqu'à 2³ − 1 = 7 ruptures (7 obtenues sur un profil test).
- `engine.py:1626` : `results[:n_knick]` garde les ruptures **les plus proches de la source** (tri par position),
  pas les plus fortes. Le knickpoint principal peut donc être éliminé.
- Coût : pour chaque position candidate, deux `np.polyfit`. **Mesuré : 0,4 s (1 000 pts), 2,3 s (3 000 pts),
  6,6 s (6 000 pts) par rivière.** Avec l'échantillonnage à 0,3 pixel (C8), une rivière de 50 km sur un MNT
  à 30 m donne environ 5 500 points, soit plusieurs minutes pour 50 bassins.
- Le nom « Bai-Perron » est abusif : c'est une segmentation binaire gloutonne, sans critère de significativité.
- **Correctif proposé** : recherche gloutonne globale (à chaque étape, la coupure qui réduit le plus le RSS
  parmi tous les segments), RSS de toutes les coupures calculé en O(n) par sommes cumulées
  (`Σx, Σy, Σx², Σxy, Σy²`), et arrêt sur un critère (BIC ou gain minimal de RSS).

### C8 — Indice SL : échantillonnage à 0,3 pixel, pixels dupliqués → SL en dents de scie

- `core/hydro.py:785` : `snap_m = taille_pixel × 0,3`. Chaque pixel est échantillonné environ 3 fois,
  et le filtre anti-doublon est **commenté** (`hydro.py:822-823`).
- Conséquences : des suites de dz = 0 puis des sauts, une pente centrée bruitée (planchers à 1e-8),
  et un **SL et un ksn continus très bruités**. Le ksn continu est en plus tronqué à 1 500 (`engine.py:1451`).
- Le SL de Hack (1973) est défini sur des **tronçons** (Δh/Δl entre deux courbes de niveau ou sur une longueur
  fixe, au point milieu du tronçon), pas pixel par pixel. Les outils publiés (Pérez-Peña et al. ;
  SLiX, Queiroz et al. 2015) utilisent une équidistance verticale ou un pas régulier.
- Détails secondaires : `sl[0]` vaut toujours 0 (distance 0) ; la docstring parle de « distance depuis
  l'embouchure » alors que le code utilise, à juste titre, la distance depuis la source.
- **Correctif proposé** : SL par tronçons à équidistance verticale paramétrable (ex. 10 m), au point milieu,
  plus un mode « pas régulier » ; supprimer les doublons de pixels.

### C9 — Rose diagram : statistiques circulaires fausses pour des données axiales

- `tools/rose/engine.py:65-66` : en mode symétrie axiale (activé par défaut), chaque azimut est dupliqué à +180°.
  Le vecteur moyen (`:214-218`) tend alors vers **0** : `R ≈ 0` et l'azimut moyen n'a plus de sens.
- En mode demi-rose (`% 180`), la moyenne arithmétique des sinus/cosinus est biaisée
  (5° et 175° donnent une moyenne de 90° au lieu de 0°).
- **Correctif** : pour des données axiales (linéaments, failles), utiliser la **méthode du doublement d'angle**
  (Mardia & Jupp 2000) : moyenne de 2θ, puis division par 2.
- Autres points : azimuts calculés en unités de carte (nord de la grille, pas le nord géographique ; faux en SCR
  géographique) ; la densification ne fait que dupliquer des poids ; le filtre de rectitude utilise `geom.length()`
  en unités du SCR.

### C10 — Smf : classes non conformes à la littérature Iat, NoData mis à 0 m

- `tools/smf/engine.py:167-170` : les classes sont < 1,4 / 1,4–1,8 / > 1,8. La méthodologie Iat
  (El Hamdouni et al., 2008, reprise par Pérez-Peña et al., 2010) utilise **classe 1 : Smf < 1,1 ;
  classe 2 : 1,1–1,5 ; classe 3 : > 1,5**. Pour un article comparable, il faut ces seuils (ou des seuils
  paramétrables, avec la source citée).
- `smf/engine.py:176` : une altitude NoData est remplacée par **0,0 m**, ce qui crée de faux creux dans le profil.
  Il faut garder `NaN`/`null`.

---

## 4. Problèmes majeurs (plateforme, stabilité, robustesse)

### M1 — QWebEngine importé sans protection dans tous les panneaux

`base/base_panel.py:7` importe `PyQt5.QtWebEngineWidgets` au chargement du module, et `:192` crée une
`QWebEngineView` pour **chaque** panneau, y compris ceux qui n'affichent rien (Terrain Derivatives,
HydroFlow, Watershed, Digitizer). Si PyQtWebEngine est absent, ce qui est fréquent sous Windows,
**aucun outil ne se charge**. Chaque vue démarre aussi un processus Chromium (plusieurs dizaines de Mo).
Analyse complète dans [03_qwebengine.md](03_qwebengine.md).

### M2 — Imports `PyQt5` directs au lieu de `qgis.PyQt`

Tous les modules importent `PyQt5.*` directement. QGIS fournit `qgis.PyQt` justement pour rester
compatible avec la branche **QGIS 4 / Qt6**. Toutes les énumérations Qt non qualifiées
(`Qt.RightDockWidgetArea`, `QDialog.Accepted`, `exec_()`…) devront aussi être migrées.

### M3 — Chemins de modules absolus dans le registre

`base/registry.py:30` (et suivantes) : `"module_path": "rockmorph.tools.rose.panel"`.
`importlib.import_module` n'aboutit que si le dossier du plugin s'appelle **exactement** `rockmorph`.
Le dépôt s'appelle `RockMorph`, et un ZIP GitHub donne `RockMorph-main` : dans ces cas, tous les outils
échouent au chargement (le message « Could not load tool » s'affiche dans la barre de messages).
**Correctif** : chemins relatifs, `importlib.import_module(".tools.rose.panel", package=<paquet racine>)`.

### M4 — Couches QGIS manipulées depuis un QThread, pas d'annulation

`ComputeWorker` (QThread) reçoit des `QgsVectorLayer`/`QgsRasterLayer` et les moteurs appellent
`getFeatures()`, `dataProvider().sample()` et `processing.run()` depuis ce thread. La documentation QGIS
indique que les couches ne sont **pas thread-safe**. Cela peut provoquer des plantages aléatoires,
surtout si l'utilisateur modifie le projet pendant le calcul. La bonne pratique est d'utiliser `QgsTask`
(barre de tâches QGIS, annulation via `isCanceled()`) avec `QgsVectorLayerFeatureSource` ou les chemins
des sources, plus un `QgsFeedback`. Aujourd'hui, **aucun calcul n'est annulable**.

### M5 — Dépendance à GRASS et détails des appels

- `"grass7:r.watershed"`, `"grass7:r.stream.extract"`, `"grass7:r.fill.dir"` : depuis QGIS 3.36, le
  fournisseur GRASS utilise l'identifiant `grass:` (**à vérifier** : `grass7:` existe-t-il encore comme alias
  dans la version ciblée ?). GRASS est absent de certaines installations (QGIS sans GRASS, certaines
  distributions macOS/Linux). Il faut alors un message clair et une alternative (fourniture d'une FAC par
  l'utilisateur, WhiteboxTools…).
- `"-a": True` est commenté « absolute accumulation in m² » (`fluvial/engine.py:2551`, `hydroflow/engine.py:94`).
  Dans `r.watershed`, `-a` signifie « utiliser une accumulation positive même pour les sous-estimations
  probables ». La sortie reste un **nombre de cellules**. Le code multiplie ensuite par la surface du pixel,
  ce qui est correct ; c'est le commentaire qui est faux.
- **À vérifier** : la sortie `drainage` de `r.watershed` contient des valeurs **négatives** sur les bords
  (écoulement hors région). `detect_encoding` (`core/watershed.py:142-174`) lèverait alors « Unrecognised D8
  encoding ». L'enchaînement HydroFlow (FDR) → Watershed Subdivision risque de casser.
- HydroFlow comble les dépressions avant `r.watershed`, alors que `r.watershed` les gère déjà (algorithme
  A* de moindre coût). Le comblement préalable est inutile et modifie le MNT.

### M6 — Métadonnées et empaquetage incomplets

- `metadata.txt` référence `icon=icon.png`, mais **le fichier n'existe pas**.
- `about=` annonce « depth-to-water », qui n'est **implémenté nulle part**.
- Pas de `LICENSE` (le dépôt officiel QGIS exige une licence compatible GPL), pas de `README`,
  pas de `changelog`, pas de dossier `i18n/`.
- `plugin.py:unload` ne retire pas le menu « RockMorph » ajouté dans *Extensions*
  (`RockMorphDock.unload()` n'est jamais appelé). Après un rechargement, le menu pointe vers un dock détruit.
- Le commentaire de `ui/main_dock.py` annonce SMF comme outil par défaut au démarrage, mais le code ouvre
  `terrainderivatives`.

### M7 — `RasterReader` : mémoire et types

- `core/raster.py` charge **tout le raster** en float32 en mémoire, à chaque instanciation. Exemples :
  `sample_river_native_pixels` relit le MNT complet **pour chaque bassin** (`core/hydro.py:757`) ;
  chaque solveur de `terrainderivatives` relit le MNT, puis `_save_numpy_to_gtiff` le relit encore
  rien que pour le géoréférencement. Il faut lire une fois, mettre en cache, et lire par fenêtre
  (`ReadAsArray(xoff, yoff, w, h)`) quand c'est possible.
- La table `gdal_to_numpy` (`core/raster.py:189`) ignore Int8, Int64 et UInt64 (types GDAL 12–14).
  Le repli sur float32 avec une taille d'octets incorrecte fait échouer le `reshape`.
- `layer.source()` n'est pas toujours un chemin GDAL ouvrable (raster en GeoPackage avec options,
  fournisseurs non GDAL).

### M8 — Watershed Subdivision : écarts entre la docstring et le code

- Le pipeline annoncé (étape 5 « Delineate parent basin via BFS ») n'existe pas : les confluences sont
  cherchées sur **tout le raster**, pas seulement en amont de l'exutoire fourni par l'utilisateur.
  Cet exutoire est ajouté **en fin de liste** (`tools/watershed/engine.py:243`), ce qui casse le tri par
  surface décroissante que suppose `propagate_labels_bfs`.
- `pixel_area` en SCR géographique utilise la latitude du **bord supérieur** et non du centre
  (`core/watershed.py:930`).
- `next_node in path` sur une liste (`core/watershed.py:594`) est en O(n²) sur les longs troncs.
- La docstring annonce un BFS en « ~0,02 s » sur 2000×2000. **Mesuré : 3,8 s** (2000×2000, réseau convergent).
  C'est acceptable, mais la documentation doit correspondre aux faits.
- `mask_to_polygon_numpy` ne suit qu'**un seul anneau**. Un masque en plusieurs parties perd des parties
  sans erreur (le repli GDAL ne s'active que si le résultat vaut `None`).

### M9 — Extraction de la rivière principale (`MainRiverExtractor`)

- La tolérance d'accrochage `snap_dist_m` est comparée à `QgsPointXY.distance()` en **unités du SCR**
  (`core/hydro.py:270`). En SCR géographique, « 2 m » devient 2 degrés.
- Graphe de connectivité construit en O(n²) par bassin.
- L'orientation de chaque tronçon dépend de l'altitude de ses extrémités. En plaine, ou si une extrémité tombe
  sur du NoData (−9999), elle peut s'inverser.
- La rivière principale est la plus **longue** chaîne amont. C'est la définition de Hack, mais elle peut
  différer du chenal de plus grande aire drainée : à documenter ou rendre configurable.

### M10 — Knickpoints jamais affichés sur la carte

`tools/fluvial/panel.py:1020-1029` : `_get_river_geom` renvoie toujours `None` (« Future: … »).
Les marqueurs de knickpoints sur la carte ne s'affichent donc jamais. Par ailleurs, la transformation
de coordonnées utilise le SCR des **bassins** alors que la géométrie est dans le SCR du **réseau**.

### M11 — Explorateur 3D

- MNT décimé par **pas fixe** jusqu'à 400 px de côté (`tools/explorer3d/engine.py:prepare_dem`), sans moyenne :
  du repliement (aliasing) apparaît sur les reliefs fins.
- **À vérifier** : en SCR géographique, l'origine en Y du MNT part du haut (`j·pixel`) alors que celle des
  vecteurs part du bas (`pt.y − y_min`). Il y a un risque de décalage vertical des vecteurs drapés.
- Toutes les entités vectorielles sont typées `"line"`, même les points et polygones.

### M12 — Swath : le lissage plante s'il y a une station vide

`tools/swath/engine.py:164` : `np.array(liste_avec_None, dtype=np.float32)` lève `TypeError` dès qu'une
station est entièrement NoData. L'exception est convertie en « Swath sampling failed ».

### M13 — HydroFlow : ordre de Strahler en O(E²) et récursion

- `tools/hydroflow/engine.py:311` : une boucle `while` reparcourt toutes les arêtes à chaque passe.
  Sur des dizaines de milliers de tronçons, c'est très lent. Un tri topologique (Kahn) est en O(E).
- `_trace_horton_upstream` est récursif : sur un très long tronc, on peut atteindre la limite de récursion Python.
- Les identifiants de nœuds sont des coordonnées arrondies à 3 décimales. En SCR géographique, cela fait
  ~100 m de tolérance, ce qui peut fusionner des nœuds distincts.

---

## 5. Problèmes mineurs et qualité du code

| # | Constat | Où |
|---|---|---|
| m1 | `tr(f"...")` × 76 : une chaîne interpolée ne peut pas être traduite (Qt Linguist ne l'extrait pas, la recherche à l'exécution échoue) | partout |
| m2 | Artefacts de génération « [1.13.2] » / « [2] » dans des chaînes **visibles par l'utilisateur** et dans le registre (×33) | `hydroflow`, `terrainderivatives`, `explorer3d`, `registry.py` |
| m3 | 59 `except Exception`, 4 `except:` nus, 20 `print()` (au lieu de `QgsMessageLog`) | partout |
| m4 | Code mort : `computeSSSS` (`tools/digitizer/engine.py:103`), `_compute_ksn_loglog_V1/V2`, `rgb_to_hsv_array` défini deux fois (`core/digitizer.py:461` et `:576`), grands blocs commentés | |
| m5 | Docstrings en contradiction avec le code : orientation du χ, BFS « 0,02 s », `-a` « m² », « 10 % de la longueur » pour la fenêtre, « dist from mouth » pour le SL | |
| m6 | MathJax référencé dans `web/fluvial.html:306`, mais `web/js/tex-mml-chtml.js` est dans `.gitignore` : les formules ne s'affichent pas | |
| m7 | `core/utils.py` affiche un `print` à l'import | |
| m8 | Aucun paramètre mémorisé entre sessions (`QgsSettings` absent) | |
| m9 | Mélange de commentaires français et anglais, style hétérogène, pas de linter ni de formatage automatique | |
| m10 | `MIN_OUTLET_SPACING_PX` défini deux fois (`core/watershed.py:95` et `:248`) | |
| m11 | three.js r128 (2021) et plotly 2.27 embarqués, sans indication de licences tierces dans le dépôt | `web/js/` |
| m12 | Pixel géographique : facteur de conversion appliqué seulement en X (cos φ). Les directions N-S des solveurs d'horizon sont donc légèrement faussées | `terrainderivatives/engine.py:_get_pixel_size_meters` |

---

## 6. Validité scientifique par outil

| Outil | Formules | Verdict | À ajouter pour un article |
|---|---|---|---|
| Hypsométrie | HI = ∫ courbe normalisée (Strahler 1952) | ✅ correct | Moments statistiques (asymétrie, aplatissement, asymétrie et aplatissement de densité ; Harlin 1978), comme CalHypso (Pérez-Peña et al. 2009) ; HI rapide par E/R (Pike & Wilson 1971) ; classes Iat (HI > 0,5 / 0,4–0,5 / < 0,4) |
| NCP | x et y normalisés, diagonale y = 1 − x, MaxC, dL, Ca % | ✅ conforme à NProfiler (Pérez-Peña et al. 2017) | Option de normalisation par (Z_source, Z_embouchure) plutôt que min/max ; export des profils normalisés de plusieurs rivières sur un même graphique |
| Swath | stats par transversale, relief, THi = (moy − min)/(max − min) | ✅ conforme à SwathProfiler (Pérez-Peña et al. 2017) | Échantillonnage par interpolation bilinéaire (option), largeur exacte en SCR géographique, swath courbe |
| SL / SLk | SL = (Δh/Δl)·L, SLk = SL/K, K = ΔH/ln L | ⚠️ formule juste, échantillonnage inadapté (C8) | SL par tronçons (équidistance), anomalies SL/K (Seeber & Gornitz 1983), carte des anomalies, classes Iat |
| χ | χ = ∫(A₀/A)^θ dx (Perron & Royden 2013) | ❌ intégrale juste, ordre faux (C1) | Carte de χ sur tout le réseau (comparaison des lignes de partage des eaux, Willett et al. 2014) |
| ksn | ksn = S·A^θref ; segments Δz/Δχ ; régression log S–log A | ❌ (C1, C4) | ksn sur fenêtre glissante de χ (Mudd et al. 2014 ; Gallen & Wegmann 2017), incertitudes |
| Knickpoints | segmentation binaire du chi-plot | ❌ (C1, C7) | Critère statistique (BIC) ; comparaison avec Knickpoint Finder (Queiroz et al. 2015) |
| Profil d'équilibre | Z = AH − (ΔH / Δlog L)·(log L − log Lmin) | ✅ forme logarithmique de Hack | Mentionner la convention L depuis la source ; écart profil–équilibre (aire, max) |
| Smf | Lf/Lr, ligne de base extrémités ou PCA | ⚠️ seuils (C10) | Vf (largeur de fond de vallée), indispensable pour l'Iat |
| Rose | binning correct | ⚠️ stats (C9) | Test de Rayleigh, statistiques axiales, nord géographique |
| Terrain | GDAL + solveurs NumPy (Yokoyama 2002, Zakšek 2011) | ✅ | TPI multi-échelle (Weiss 2001), courbures, rugosité VRM (Sappington 2007), TWI |
| Watershed | D8 + BFS amont | ✅ algorithme correct | Respect de l'exutoire utilisateur (M8) |
| HydroFlow | Strahler, Shreve, Horton | ✅ logique correcte | Rb, Rl, Dd, Fs (voir 05) |

---

## 7. Ce qui manque pour « tout le workflow d'un géomorphologue »

1. **Fournisseur Processing** (`QgsProcessingProvider`). Sans lui, pas de modeleur graphique, pas de traitement
   par lots, pas d'automatisation, pas de reproductibilité scriptable. C'est **la** brique de l'automatisation.
2. **Indices de bassin manquants** : Af, T, Vf, Bs, Re, Rc, Ff, Kc, Dd, Fs, Rb, Melton, Rh… et **l'agrégation Iat**
   avec sa carte de classes (voir 05).
3. **Mise en page cartographique automatique** (`QgsPrintLayout`) : cartes d'indices, légendes et échelles prêtes
   à publier. C'est le « Illustrator » du workflow.
4. **Rapport et tables** : export d'un tableau récapitulatif par bassin (CSV/XLSX/LaTeX) avec tous les indices et
   leurs classes, plus un fichier de paramètres JSON (versions, θref, seuils) pour la reproductibilité.
5. **Tests unitaires** sur données synthétiques avec valeurs analytiques connues (rivière à ksn fixé, bassin
   circulaire, cône hypsométrique…), et un jeu de données réel de référence pour comparer avec CalHypso,
   SwathProfiler/NProfiler et TopoToolbox.
6. **Documentation utilisateur** (README, guide par outil, captures) et **i18n** (fr/en/es).
7. **Annulation** et suivi des calculs dans le gestionnaire de tâches QGIS (`QgsTask`).

---

## 8. Annexe — méthode de vérification

Les vérifications « **vérifié** » ont été faites hors QGIS, avec Python 3 et NumPy 2.5.3, en chargeant les
modules du dépôt avec des stubs pour `qgis.core` et `PyQt5.QtCore`. Les scripts sont restés hors du dépôt.

- **C1** : rivière synthétique source → embouchure, 400 points, A = 10⁴ + 2(d+50)^1,7 m², Z = 200 + 100·χ (θ = 0,45).
  `FluvialEngine._compute_chi` renvoie χ[0] = 0 pour le point source (Z = 2 460 m). La pente de la régression
  Z~χ avec l'appariement du code vaut −82,9 (attendu : +100).
- **C4** : `_compute_ksn_segments(..., knickpoints=[{idx:130},{idx:260}], "regression")` → `IndexError`.
- **C6** : `hasattr(numpy, "trapz")` → `False` sous NumPy 2.5.3.
- **C7** : `_recursive_breakpoints(..., remaining=3)` → 7 ruptures ; temps de 0,41 s, 2,26 s et 6,58 s pour 1 000, 3 000 et 6 000 points.
- **M8** : `propagate_labels_bfs` sur un réseau convergent : 0,77 s (1000²), 3,76 s (2000²).
