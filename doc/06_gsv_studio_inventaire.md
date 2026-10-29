# 06 — GSV-Studio : inventaire de la logique géomorphologique

> **Prise de notes en lecture seule** (étape 4, préparation). Aucun code n'a été modifié, ni dans
> GSV-Studio ni dans RockMorph, et rien n'a encore été porté.
> Dépôt : `PerfectWin7777/GSV-Studio`, commit `f501590` (« Avancée en ToolCalling… »), 78 fichiers, ~49 000 lignes Python.
> Les références `fichier:ligne` renvoient à ce commit.

---

## 1. Où se trouve la logique géomorphologique

Sur les ~49 000 lignes, la logique de calcul d'indices tient dans **quatre fichiers**. Le reste est
de l'interface (PyQt5), de la gestion de données (catalogue, validation) et l'assistant IA
(LangChain, ChromaDB, fournisseurs LLM), **sans rapport avec le calcul**.

| Fichier | Lignes | Rôle | À porter ? |
|---|---|---|---|
| `expert/data_base/indices_definition.py` | 2 728 | **Registre `INDICE_DEFS`** : 55 indices avec nom, description, tags, auteur, DOI/URL, fonction de calcul, entrées requises, paramètres configurables, plusieurs classifications sourcées | **Oui** : c'est exactement la structure `IndexSpec` proposée en 04 (déjà écrite !) |
| `expert/workers/geo_index_worker.py` | 6 254 | **Toutes les fonctions de calcul** (`calculate_*`), table `CALCULATION_FUNCTIONS`, et le worker `GSVGeoIndexWorker` (reprojection, prétraitement PySheds, métriques de base par bassin, chenal principal, réseau, classification) | **Oui** : fonctions de calcul ; le pipeline est à réécrire avec l'API QGIS |
| `studios/geomorpho_index/studio.py` | 6 868 | UI du Studio + **post-traitements** : calcul **Iat** (`_performIatCalculation`, l.5249), **modeleur d'indices composites** (`_calculateAndApplyModel`, l.4695 : moyenne, moyenne pondérée, min, max, moyenne des classes type Iat, ACP-PC1) | Oui pour la logique Iat et modeleur, non pour l'UI |
| `studios/geomorpho_index/dialogs.py` | 5 705 | Dialogues, dont **AHP** (matrice de Saaty, poids, ratio de cohérence, l.3900-3990, analyse de sensibilité) et Iat (configuration des scores de classes) | Oui pour le calcul AHP |
| `expert/calculators/statistical_analyzer.py` | 702 | K-means, CAH + dendrogramme, DBSCAN, ACP + éboulis, t-SNE, UMAP, Isolation Forest, LOF, scores PC1 normalisés | Oui (option, dépend de scikit-learn) |
| `gsv_resources/knowledge_base/*.md` | 57 | Fiches HI, AHP, validation (pour l'assistant IA) | Utile pour l'aide en ligne |

Fichiers **sans logique géomorphologique** (non concernés) : `core/dispatcher.py`, `core/gatekeeper.py`
(catalogue et validation des couches), `core/geo_utils.py` (utilitaires : facteur Z, résolution métrique,
ombrage coloré ; l'énumération `TerrainAttribute` liste courbures, géomorphons, TWI… mais **aucun calcul
n'est implémenté dans ce dépôt**), `expert/calculators/raster_processing.py` (filtres d'image : moyenne,
Gauss, médiane, bilatéral, Lee, Frost, Sobel, Canny, Gabor, CLAHE…), `expert/intelligence/*` (IA),
`gui/*`, `tests/*` (aucun test sur les indices).

---

## 2. Architecture du calcul (ce qu'il faut comprendre avant de porter)

```
INDICE_DEFS (registre)                       CALCULATION_FUNCTIONS (geo_index_worker.py:3224)
  id → fonction_calcul, inputs_directs_requis,     id → calculate_xxx(inputs: dict) -> float
       parametres_calcul_specifiques, classifications
                     │
GSVGeoIndexWorker.run()  (geo_index_worker.py:4526)
  0. hash des fichiers d'entrée → invalidation des caches
  1. lecture bassins (GeoPandas) ; DEM : si non projeté en mètres → reprojection UTM auto (gdal.Warp bilinéaire)
  2. prétraitement global PySheds (l.3380) : fill_pits → fill_depressions → resolve_flats → flowdir D8
     → accumulation → ordre de Strahler raster (seuil en px / m² / km²) → pente globale (np.gradient)
  3. réseau vecteur utilisateur : reprojection, explode, **ordre Strahler échantillonné sur le raster PySheds**
  4. pour chaque bassin : métriques de base (l.3806)
       aire, périmètre, Lb et largeur par **rectangle minimal orienté (MRR)**, axe médian MRR, **axe ACP**,
       altitudes min/max/moyenne/médiane (histogramme 100 classes), TR, TD,
       chenal principal (si un indice le demande), métriques réseau (N, L totale, ordres, longueurs par ordre),
       aires de sous-bassins par ordre (pour RA)
     puis chaque indice demandé : paramètres utilisateur injectés dans `inputs`, appel de la fonction, arrondi
  5. DataFrame final + colonne `<id>_Classe` via la classification par défaut
Studio (après le calcul) : Iat, modèles composites, AHP, ACP, clustering, graphiques d'exploration
```

Points d'architecture **à reprendre tels quels** dans RockMorph :
- le registre déclaratif (métadonnées, références, paramètres typés pour générer l'UI, classifications multiples) ;
- la signature uniforme `f(inputs: dict) -> float` ;
- le calcul des métriques de base **une seule fois par bassin**, partagées par tous les indices ;
- l'Iat générique (n'importe quels indices, n'importe quelles classifications, table classe → score) et le modeleur composite.

Points d'architecture **à ne pas reprendre** :
- caches disque `joblib.Memory` sur des fonctions qui reçoivent des tableaux NumPy (hachage coûteux à chaque appel) ;
- graphiques de débogage déclenchés par des **identifiants de bassin codés en dur** (21, 61, 2), écrits sur disque
  à côté du MNT (`geo_index_worker.py:891`, `:1142`, `:1585`, `:1812`, `:2061`, `:2368`, `:2616`) ;
- lecture du MNT complet **à chaque profil** (`dem_reader.read(1)`, `:2520` pour Vf, `:2686` pour χ).

---

## 3. Inventaire des 55 indices

Légende : ✅ correct · ⚠️ à corriger ou préciser · ❌ faux ou inutilisable en l'état.
« Base » = la valeur vient directement des métriques de base.

### 3.1 Réseau hydrographique

| ID | Nom | Formule implémentée | Verdict | Remarques |
|---|---|---|---|---|
| StreamOrderMax | Ordre max (Strahler) | max des ordres | ⚠️ | Ordres **imposés par le raster PySheds** (`:4681`), le champ d'ordre de l'utilisateur est ignoré |
| TotalStreamLength | Longueur totale | ΣL (base) | ⚠️ | Libellé « km », valeur renvoyée en **m** (`:3275`) |
| TotalNumSegments | Nombre de segments | N entités | ⚠️ | Dépend du découpage du vecteur (segments ≠ cours d'eau de Horton) |
| RB | Rapport de bifurcation | moyenne de Nu/Nu+1 | ⚠️ | Compte des **segments** et non des cours d'eau d'ordre u (un cours d'eau d'ordre 2 coupé en 3 segments compte 3) → RB biaisé |
| RL | Rapport des longueurs | moyenne de L̄u/L̄u−1 | ⚠️ | Même problème de segments |
| RA | Rapport des surfaces | moyenne de Āu/Āu−1 | ⚠️ | Utilise des aires **locales** (aire totale − affluents, `:4291`) alors que Schumm (1956) utilise l'aire drainée totale des bassins d'ordre u |
| Dd | Densité de drainage | L/A (km/km²) | ✅ | Vaut **0** (et non NaN) si aucun réseau n'est fourni (`:4100`) |
| Fs | Fréquence | N/A (km⁻²) | ✅ | idem |
| Dt | Texture | N/P (km⁻¹) | ✅ | idem |
| Di | Intensité de drainage | Fs × Dd | ⚠️ | Faniran (1968) est généralement cité avec **Di = Fs/Dd** (à vérifier dans la source) |
| Lg | Longueur d'écoulement | 1/(2Dd) | ✅ | |
| C | Constante d'entretien | 1/Dd | ✅ | |
| Lar | Relation longueur-surface | Lobs / (1,4·A^0,6) | ❌ | 1,4·A^0,6 est la loi de Hack en **miles/miles²** appliquée ici en km/km² ; elle porte sur la longueur du **chenal principal**, pas sur la longueur totale du réseau |
| LengthArea | La (Schumm) | Lg / A | ⚠️ | Mélange d'unités (km et km²), sens physique faible |
| HSI | Sinuosité « hydraulique » (Mueller) | Lc / Lv | ⚠️ | C'est la sinuosité simple du chenal ; le HSI de Mueller (1968) est (CI − VI)/(CI − 1) |
| TSI | Sinuosité « topographique » (Mueller) | Lv / Lb | ⚠️ | Idem : le TSI de Mueller vaut (VI − 1)/(CI − 1) |

### 3.2 Forme du bassin

| ID | Formule | Verdict | Remarques |
|---|---|---|---|
| BasinArea | A | ⚠️ | Libellé km², valeur en **m²** (`:3277`) |
| BasinPerimeter | P | ⚠️ | Libellé km, valeur en **m** (`:3278`) |
| Rf | A / Lb² | ✅ | Lb = grand côté du MRR (approximation ; la définition classique est l'exutoire → point le plus éloigné de la ligne de partage) |
| Re | 2√(A/π) / Lb | ✅ | idem Lb |
| Rc | 4πA / P² | ✅ | Sensible à la résolution du polygone (P augmente avec un contour « en escalier ») |
| Lemniscate | Lb² / 4A | ✅ | |
| Compactness | P / (2√(πA)) | ✅ | Gravelius |
| Rfn | Lb / P | ⚠️ | Melton (1957) utilise la longueur du **chenal** sur le périmètre |
| Bs | Lb / Lw (MRR), plancher 1 | ✅ | Définition de Ramírez-Herrera (1998) ; le registre cite Cannon (1977), source à vérifier |

### 3.3 Relief et hypsométrie

| ID | Formule | Verdict | Remarques |
|---|---|---|---|
| AltitudeMin / Max / Mean | stats DEM découpé | ✅ | |
| AltitudeMedianHm | médiane via histogramme à 100 classes | ⚠️ | Approximation ; `np.median` est exact et simple |
| TR | Hmax − Hmin | ✅ | |
| TD | Hmoy − Hmin | ✅ | |
| TRD | **TR × TD / 10⁶** (`:812`) | ❌ | Le registre définit **TRD = Hmax − Hmoy** (Walcott & Summerfield 2008) : code et définition ne correspondent pas |
| Rra | TR / Lb | ✅ | Rapport de relief de Schumm |
| Rr | TR × 100 / P | ✅ | Relief relatif (Melton 1957 ; le registre cite Pike & Wilson 1971) |
| Hd | TR(km) × Dd | ✅ | Nombre de rugosité de Strahler |
| HI | (Hmoy − Hmin)/(Hmax − Hmin) | ✅ | Méthode élévation-relief (Pike & Wilson 1971), équivalente à l'intégrale. Le paramètre `hi_formula_variant` (variante médiane) est **ignoré** par la fonction |
| U | Hm(km) + (1 − HI) | ⚠️ | Formule de Sinha-Roy (2002) à vérifier dans la source ; paramètre `u_formula_variant` ignoré ; la classification « El Hamdouni 2008 » de U ne correspond à rien dans cet article (il n'utilise pas U) |
| AltitudeSkewness / Kurtosis | asymétrie et aplatissement (Pearson) des **altitudes** | ⚠️ | Utile, mais ce ne sont **pas** les moments de la courbe hypsométrique de Harlin (1978) / CalHypso, qui portent sur la courbe normalisée et sa densité |
| SlopeMeanDegree / Percent | arctan(moyenne de tan β) par `np.gradient` | ⚠️ | Le paramètre `slope_algorithm` (Horn, Zevenbergen…) est **ignoré** ; arctan(moyenne(tan)) ≠ moyenne des pentes en degrés |

### 3.4 Tectonique active et profils

| ID | Méthode | Verdict | Remarques |
|---|---|---|---|
| **SL** | profil échantillonné le long du chenal ; **tronçons de longueur fixe** (défaut 100 m dans le registre) ; SL = (ΔH/ΔL)·L au **milieu** du tronçon, L depuis la source ; agrégation moyenne, médiane, max ou P90 | ✅ **méthode** / ⚠️ données | C'est la bonne approche de Hack, qui corrige le défaut C8 de RockMorph. Mais le chenal est tronqué (voir P1) et la classification « El Hamdouni 2008 » est fausse (voir §5) |
| **AF** | coupe d'un grand rectangle par le chenal prolongé (`shapely.split`), identification rive droite/gauche par décalage parallèle, AF = 100·Ar/At | ✅ | Algorithme robuste et original. Le prolongement rectiligne du chenal au-delà de la source reste une approximation (la ligne de partage n'est pas suivie) |
| **TTSF (T de Cox)** | axe ACP du bassin comme ligne médiane ; Da = distance chenal → axe ; Dd = demi-largeur ; T = moyenne de Da/Dd sur N sections | ⚠️ | Méthode par défaut « Max Axe-to-Boundary » ; la définition de Cox correspond à l'option « Side of Channel Displacement ». La **direction** du basculement (azimut), essentielle chez Cox, n'est pas sortie. Axe ACP rectiligne ≠ ligne médiane sinueuse |
| **VF** | N profils transversaux (interpolation bilinéaire, lissage gaussien), Vfw par seuil de pente ou détection du pied de versant, Eld/Erd par percentile ou détection d'épaulement, Vf = Vfw / Ēh, agrégation (médiane par défaut) | ⚠️ | Méthode riche et automatique, **mais les paramètres de l'utilisateur sont ignorés** : `INDICE_DEFS.get("Vf")` alors que la clé est `"VF"` (`:2402`). Toutes les valeurs par défaut internes s'appliquent toujours. Largeur de profil fixe (500 m) : les crêtes Eld/Erd sont souvent hors profil |
| **Ksn** | S sur une fenêtre de 250 m vers l'aval, A en amont, ksn = S·A^θ, agrégation | ⚠️ | Les tronçons plats ou en contre-pente sont forcés à S = 1e-5 (`:2260`) au lieu d'être exclus ; fenêtres très chevauchantes |
| **Chi** | χ = Σ(A₀/A)^θ·Δx de l'exutoire vers l'amont, A au point amont | ⚠️ | Statistique par défaut « Valeur à l'exutoire » = **toujours 0** (χ commence à 0 à l'exutoire). La seule sortie utile est `k_chi` (pente Z~χ = ksn quand A₀ = 1). Branche « source → exutoire » : bloc `else` mal indenté, la « valeur à la source » renvoie NaN |
| MASC | (Hmax − Hmin)_chenal / Lc | ✅ | |
| HG | MASC × 100 | ✅ | Doublon de MASC |
| De | 28·e^(0,00774·S) | ⚠️ | Unité de S non tranchée (le code utilise des degrés) ; relation empirique régionale |
| Ebulk | TR × De | ❌ | Brocklehurst & Whipple (2002) définissent un **volume** (enveloppe des crêtes − topographie actuelle) ; TR × taux n'a pas de sens dimensionnel |
| E | 1,4·10⁻⁶·TR^1,8 | ⚠️ | Relation empirique ; le relief « local » devrait être mesuré dans une fenêtre, pas comme le relief total du bassin |

### 3.5 Indices « pixel »

| ID | Formule | Verdict | Remarques |
|---|---|---|---|
| SPI | A·tan β | ❌ | **Le raster découpé comme « pente » est le MNT** (`rasterio.mask.mask(dataset=dem_reader…)`, `:3129`) : les altitudes en mètres sont interprétées comme des radians. Le raster de pente calculé n'est jamais utilisé |
| TWI | ln(A / tan β) | ❌ | Même bug ; de plus, il faut l'aire **spécifique** a = A / largeur de contour |
| STI | (As/22,13)^0,6·(sin β/0,0896)^1,3 | ❌ | Même bug (formule correcte sinon) |

---

## 4. Problèmes transverses (qui faussent plusieurs indices à la fois)

| # | Problème | Où | Indices touchés |
|---|---|---|---|
| P1 | **Chenal principal tronqué** : remontée limitée à **1 000 pixels** (`:3728`), soit 10 km sur un MNT à 10 m et 30 km à 30 m ; arrêt quand l'accumulation tombe sous ~½·max(P85, 10 % du max) (`:3716`), donc la tête de chenal n'est jamais atteinte | `_trace_main_channel` | SL, Ksn, Chi, Vf, T, AF, HSI, TSI, MASC, HG |
| P2 | Remontée « gloutonne » vers le voisin de plus forte accumulation **sans vérifier qu'il s'écoule vers la cellule courante** (la direction de flux n'est pas utilisée) : le tracé peut sauter sur un chenal parallèle | `_find_upstream_cell` | idem P1 |
| P3 | Coordonnées du chenal au **coin** du pixel et non au centre (`transform * (col, row)`, `:3738`) : décalage d'un demi-pixel | `_trace_main_channel` | idem P1 |
| P4 | Ordre de Strahler du réseau utilisateur **remplacé** par celui échantillonné sur le raster PySheds (mode des pixels, seuil d'accumulation global) | `run`, `:4681` | StreamOrderMax, RB, RL, RA |
| P5 | Paramètres utilisateur ignorés : Vf (clé `"Vf"`), `hi_formula_variant`, `u_formula_variant`, `slope_algorithm` | divers | VF, HI, U, Slope* |
| P6 | Bassins en **MultiPolygon** : toutes les fonctions testent `isinstance(…, Polygon)`, donc NaN silencieux | partout | tous |
| P7 | Le cache PySheds ne sert jamais : le hash calculé dans `run` (`:4643`) diffère de celui stocké (`:3470`), donc le prétraitement est refait à chaque lancement | `run` | performance |
| P8 | Lecture complète du MNT à chaque profil Vf et à chaque calcul de χ | `:2520`, `:2686` | performance |
| P9 | Réseau absent → Dd, Fs, Dt valent 0 au lieu de NaN | `:4100` | Dd, Fs, Dt, Di, C, Lg, Hd |
| P10 | Aucune unité explicite dans les sorties ; libellés km/km² sur des valeurs en m/m² | `:3275-3278` | BasinArea, BasinPerimeter, TotalStreamLength |

---

## 5. Classifications à corriger

Le système de classifications multiples, avec auteur et référence, est une **excellente idée** à garder.
Plusieurs règles sont cependant fausses ou mal appliquées :

| Indice | Classification | Problème |
|---|---|---|
| SL | `SL_ElHamdouni_2008_Activite` : <100 / 100–300 / >300 | El Hamdouni et al. (2008) : **classe 1 ≥ 500 ; 2 : 300–500 ; 3 < 300**. Seuils et libellés à revoir |
| AF | `AF_Balla_2023` : <7 / 7–15 / >15 | Ces seuils portent sur **\|AF − 50\|**, pas sur AF brut (0–100). Appliqués tels quels, presque tous les bassins sont « forte activité » |
| HI | `Balla_Ateba_2023_HI` | Règles **non triées** (contraire à la convention du fichier) ; HI > 0,4 libellé « concave/vieux », en contradiction avec la fiche `indice_hypsometrique.md` (HI élevé = convexe/jeune) |
| TTSF | `T_Cox_1994_TiltInterpretation` | Règles **qui se chevauchent** (−∞–0,1 puis 0–0,2) |
| Ksn | `Ksn_RelativeInterpretation` | Libellé « > 500 » sur une classe ≥ 300 ; seuils absolus de ksn qui dépendent de θ et de A₀ |
| U | `U_ElHamdouni_2008_Classes` | Classification attribuée à un article qui n'utilise pas U |
| Iat | (Studio) | `pd.cut(right=False)` n'applique pas la convention « dernière borne incluse » du registre ; un bassin auquel il manque des indices reçoit quand même un Iat, sans signaler le nombre d'indices utilisés |

Référence Iat à respecter (El Hamdouni et al., 2008 ; **à vérifier dans l'article original avant publication**) :
SL : 1 ≥ 500, 2 : 300–500, 3 < 300 · Af : \|Af − 50\| 1 ≥ 15, 2 : 7–15, 3 < 7 · HI : 1 ≥ 0,5, 2 : 0,4–0,5, 3 < 0,4 ·
Vf : 1 < 0,5, 2 : 0,5–1, 3 ≥ 1 · Bs : 1 ≥ 4, 2 : 3–4, 3 < 3 · Smf : 1 < 1,1, 2 : 1,1–1,5, 3 ≥ 1,5 ·
Iat = S/n : 1 (très forte) 1–1,5, 2 (forte) 1,5–2, 3 (modérée) 2–2,5, 4 (faible) ≥ 2,5.

**Smf n'existe pas dans GSV-Studio** ; il existe dans RockMorph (outil SMF). Les deux dépôts se complètent pour l'Iat complet.

---

## 6. Couche d'analyse multicritère (Studio)

| Fonction | Où | Méthode | Verdict |
|---|---|---|---|
| Iat | `studio.py:5249` | pour chaque indice choisi : classification → table classe → score, puis moyenne des scores (NaN ignorés), puis classification finale | ✅ générique et réutilisable (corriger les points du §5) |
| Modeleur d'indices | `studio.py:4695` | normalisation min-max, puis moyenne, moyenne pondérée, min, max, moyenne des classes (type Iat) ou PC1 | ⚠️ pas d'inversion de sens pour les indices où « petit = actif » (Vf, Smf) ; PC1 de signe arbitraire (déjà noté dans le code) |
| AHP | `dialogs.py:3900-3990` | poids par moyenne des lignes de la matrice normalisée (approximation du vecteur propre), λmax exact par `eigvals`, CI, RI de Saaty (n = 3–16), CR ≤ 0,10, curseurs de Saaty, sensibilité | ✅ correct ; utiliser le vrai vecteur propre pour la cohérence avec λmax |
| Statistiques et apprentissage | `statistical_analyzer.py` | K-means, CAH, DBSCAN, ACP, t-SNE, UMAP, Isolation Forest, LOF | ✅ utile pour l'article (regroupement des bassins) ; dépend de scikit-learn, qui n'est pas fourni par QGIS |

---

## 7. Dépendances : ce qui pose problème pour un portage dans QGIS

| Bibliothèque GSV | Usage | Disponible dans QGIS (Windows OSGeo4W) ? | Remplacement dans RockMorph |
|---|---|---|---|
| NumPy, SciPy | partout | ✅ | — |
| GDAL/OGR | reprojection | ✅ | — |
| **pysheds** | conditionnement, D8, accumulation, Strahler, bassins | ❌ | GRASS (`r.watershed`, `r.stream.*`), `native:filldepressions`, ou le D8/CSR NumPy déjà présent dans `core/watershed.py` de RockMorph |
| **rasterio** | lecture, découpe, échantillonnage | ❌ en standard | `RasterReader` (GDAL) de RockMorph + `gdal.RasterizeLayer` (déjà utilisé par l'hypsométrie) |
| **GeoPandas / Shapely** | bassins, réseau, AF, T, MRR | Shapely souvent présent, GeoPandas non | `QgsGeometry` : `orientedMinimumBoundingBox()` (MRR), `splitGeometry`, `offsetCurve`, `intersection`, `interpolate`, `QgsSpatialIndex` |
| pandas | DataFrames | ✅ en général | listes et dict, ou pandas si présent |
| scikit-learn | ACP de l'axe, ACP-PC1, clustering | ❌ | ACP de l'axe en NumPy (`np.linalg.eigh`, déjà fait dans le SMF de RockMorph) ; clustering en option si sklearn est installé |
| networkx | graphe RA | ❌ | dict d'adjacence (comme `MainRiverExtractor`) |
| joblib, matplotlib (débogage), PyQt5 | cache, graphiques, signaux | — | supprimer ; `QgsTask` pour le calcul |

**Conséquence** : les **formules** (fonctions courtes du §3.1-3.3) se portent presque telles quelles.
Les **algorithmes géométriques** (AF, T, Vf, SL, Ksn, χ, chenal principal, RA) doivent être réécrits avec
l'API QGIS/GDAL. La logique reste la même ; seules les bibliothèques changent.

---

## 8. Croisement GSV-Studio × RockMorph

| Thème | RockMorph | GSV-Studio | Après fusion |
|---|---|---|---|
| Rivière principale | `MainRiverExtractor` sur réseau vecteur (graphe, longueur maximale) | traçage raster par accumulation (tronqué, P1-P3) | **garder RockMorph** (corrigé C3) ; option raster D8 propre |
| SL | pixel par pixel (défaut C8) | tronçons de longueur fixe, L au milieu | **méthode GSV** sur le chenal RockMorph |
| ksn, χ | profils complets, chi-plot, segments, knickpoints (bugs C1-C7) | une valeur agrégée par bassin | **RockMorph corrigé** pour les profils + **agrégats GSV** pour la table |
| HI | intégrale de la courbe complète | E/R | les deux (identiques en théorie, utile pour vérifier) |
| Moments hypsométriques | — | asymétrie et aplatissement des altitudes | ajouter les moments de Harlin sur la courbe |
| AF, T, Vf, Bs, U | — | ✅ / ⚠️ | **apport GSV** |
| Smf | ✅ (corriger les seuils C10) | — | **apport RockMorph** |
| Forme (Re, Rc, Rf, Kc, k, Rfn) | — | ✅ | **apport GSV** |
| Réseau (Dd, Fs, Dt, Rb, Rl, Ra, C, Lg, Hd) | Strahler, Shreve, Horton (HydroFlow) | ✅ / ⚠️ | ordres **RockMorph** + rapports **GSV** recalculés par cours d'eau (et non par segment) |
| Relief (TR, TD, TRD, Rh, Rr) | relief dans les swaths | ✅ (TRD faux) | **apport GSV** |
| Érosion (De, E, Ebulk, MASC, HG) | — | ⚠️ / ❌ | à discuter : garder MASC/HG, revoir les relations empiriques et leurs sources, retirer Ebulk tant qu'il n'est pas défini correctement |
| TWI, SPI, STI | — | ❌ (bug du §3.5) | à refaire dans `terrainderivatives` (raster) + statistiques par bassin |
| Iat, AHP, modeleur, ACP, clustering | — | ✅ | **apport GSV** : c'est la couche « article » |
| Swath, NCP, rose, SVF/openness, 3D, digitaliseur, sous-bassins | ✅ | — | RockMorph |

Avec les deux dépôts, le catalogue passe de 20 indices corrects (RockMorph seul) à une couverture quasi
complète des indices utilisés dans les articles de tectonique active. Le détail est dans
[05_catalogue_indices.md](05_catalogue_indices.md).

---

## 9. Plan de portage proposé (rien n'est fait ; à valider par toi)

1. **Bibliothèque pure** `core/indices/` (NumPy, sans QGIS) : registre repris de `INDICE_DEFS` (mêmes clés,
   métadonnées, classifications **corrigées**), fonctions simples des §3.1-3.3 portées avec leurs corrections
   (unités, TRD, Di, Lar, HSI/TSI, paramètres réellement utilisés).
2. **Métriques de base par bassin** avec l'API QGIS : aire et périmètre géodésiques, MRR
   (`orientedMinimumBoundingBox`), axe ACP NumPy, statistiques d'altitude via `RasterReader` + rasterisation,
   réseau découpé et ordres de cours d'eau (et non de segments).
3. **Chenal principal** : réutiliser `MainRiverExtractor` (corrigé), ou un traçage D8 qui suit la direction de flux, sans limite de longueur.
4. **Indices géométriques** réécrits en `QgsGeometry` : AF (même algorithme), T (option Cox par défaut + azimut de basculement), Vf (paramètres réellement lus), SL par tronçons, ksn et χ agrégés.
5. **Iat, AHP, modeleur** portés comme module de post-traitement sur la table de résultats.
6. **Tests** : une valeur analytique ou publiée par indice (bassin circulaire : Rc = 1 ; rectangle : Re, Rf, Bs connus ; AF = 50 pour un bassin symétrique…).

### Questions pour toi avant de commencer

1. **Ebulk, De, E** : as-tu les articles sources (et les unités exactes) ? Sinon, je propose de les garder hors du calcul Iat et marqués « expérimentaux ».
2. **Di** : quelle définition veux-tu (Fs × Dd ou Fs / Dd) ? Je recommande de suivre Faniran (1968) après vérification.
3. **Classifications Balla Ateba (2023), Nsangou (2020), Mvondo Ondoa (2016)** : ce sont des travaux camerounais que tu connais. Peux-tu confirmer les seuils exacts (et sur quelle grandeur, par exemple \|AF − 50\|) ?
4. **scikit-learn** (clustering, UMAP) : fonctionnalité optionnelle activée seulement s'il est installé, ou à laisser de côté ?
