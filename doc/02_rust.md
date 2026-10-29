# 02 — Faut-il mettre du Rust dans RockMorph ?

## Réponse courte

**Non, pas maintenant.** Le dépôt ne contient aucun code Rust (aucun `Cargo.toml`, `.rs`, PyO3 ni maturin,
y compris dans l'historique visible). Les lenteurs mesurées ne viennent pas de Python lui-même mais
d'**algorithmes en O(n²)** et de **boucles Python évitables**. Elles se corrigent en NumPy/SciPy,
déjà disponibles dans QGIS, pour un gain de 10 à 1 000×, sans rien compiler ni distribuer.
Ajouter Rust aujourd'hui alourdirait fortement l'empaquetage et la publication, pour un bénéfice faible.

---

## 1. Où le temps est-il réellement passé ?

Mesures hors QGIS (Python 3, NumPy 2.5.3), sur les fonctions du dépôt, avec des données synthétiques.

| Fonction | Taille | Temps mesuré | Cause | Correctif sans Rust | Gain attendu |
|---|---|---|---|---|---|
| `FluvialEngine._recursive_breakpoints` (knickpoints) | 1 000 / 3 000 / 6 000 pts | 0,4 s / 2,3 s / **6,6 s par rivière** | 2 `np.polyfit` par position candidate → O(n²), répété à chaque niveau | RSS de toutes les coupures en O(n) par sommes cumulées (Σx, Σy, Σx², Σxy, Σy²) | ×100 à ×1 000 |
| `core.watershed.propagate_labels_bfs` | 1000² / 2000² pixels | 0,8 s / **3,8 s** | boucle `deque` Python sur chaque pixel | propagation par *pointer jumping* vectorisé (`labels[i] = labels[aval[i]]`, `aval = aval[aval]`), environ log₂(longueur max) itérations | ×5 à ×20 |
| `build_downstream` + `build_upstream_csr` | 2000² | 0,5 s + 0,3 s | déjà vectorisé | rien à faire | — |

Goulots identifiés à la lecture, non chronométrés car il faudrait QGIS :

| Endroit | Problème | Correctif sans Rust |
|---|---|---|
| `HydroFlowEngine._solve_topology` | boucle `while` qui reparcourt toutes les arêtes : O(E²) | tri topologique de Kahn, O(E) |
| `MainRiverExtractor._extract_basin` | graphe d'accrochage en O(n²) par bassin | index spatial (`QgsSpatialIndex` ou `scipy.spatial.cKDTree`) sur les extrémités |
| `sample_river_native_pixels` | relit **tout le MNT** pour chaque bassin, échantillonnage FAC point par point via `dataProvider().sample()` | lire le MNT et la FAC une seule fois, indexer les tableaux NumPy en bloc |
| `sample_river_profile`, `SwathSampler` | `sample_at` appelé point par point dans des boucles Python | conversion vectorisée coordonnées → (lignes, colonnes) puis `array[rows, cols]` ; `scipy.ndimage.map_coordinates` pour du bilinéaire |
| `mask_to_polygon_numpy`, `subbasins_from_labels` | parcours pixel par pixel, un masque plein raster par label | `gdal.Polygonize` sur le raster de labels en **un seul appel**, puis lecture des polygones par valeur |
| `terrainderivatives` (SVF, openness…) | déjà vectorisé, mais relit le MNT à chaque solveur et alloue un tableau par pas de rayon | lecture unique, mise en cache des angles d'horizon entre SVF, openness et sky illumination |

**Conclusion** : sur tous ces points, Rust ne serait plus rapide qu'en gardant le **même** mauvais
algorithme. Avec le bon algorithme, NumPy suffit largement aux tailles de MNT courantes en géomorphologie
(jusqu'à quelques dizaines de millions de pixels).

---

## 2. Ce que coûterait Rust (PyO3 + maturin)

1. **Compilation par plateforme.** Il faut des binaires pour Windows x64, macOS (arm64 et x86_64) et Linux,
   pour chaque version de Python embarquée par QGIS (3.9 à 3.12 selon les versions). L'ABI stable `abi3`
   réduit le nombre de builds, mais une chaîne CI dédiée reste nécessaire.
2. **Distribution.** Le dépôt officiel des extensions QGIS est très réticent aux binaires compilés dans
   un plugin (**politique exacte à vérifier** avant toute décision). L'alternative serait un paquet séparé
   sur PyPI, installé à part dans le Python de QGIS. C'est exactement le genre de friction d'installation
   sous Windows qu'on cherche à éviter (voir 03).
3. **Sécurité et confiance.** Des DLL non signées dans un plugin déclenchent des alertes d'antivirus et
   inquiètent les administrateurs des postes universitaires.
4. **Relecture scientifique.** Pour un article méthodologique, les relecteurs doivent pouvoir lire et refaire
   les calculs. Une implémentation NumPy lisible est un atout ; un module natif est une boîte noire de plus.
5. **Maintenance.** Il faudrait maintenir deux langages, deux chaînes de tests, et un repli Python de toute
   façon pour les plateformes sans binaire.

---

## 3. Si un jour il faut du code natif rapide : que privilégier

Dans l'ordre :

1. **Déléguer aux outils natifs déjà présents dans QGIS** : GDAL (`DEMProcessing`, `Polygonize`, `Warp`),
   GRASS (`r.watershed`, `r.stream.*`), SAGA. Ils sont compilés, testés et déjà installés.
2. **WhiteboxTools**, qui est **écrit en Rust** et possède un plugin QGIS. Pour l'hydrologie sur de très
   gros MNT (Priority-Flood, D-inf, ordres de cours d'eau), on profite de la vitesse de Rust **sans écrire
   ni distribuer une ligne de Rust**. RockMorph peut l'appeler via Processing quand il est installé.
3. **SciPy** (déjà requis) : `ndimage` (labels, distances, filtres), `spatial.cKDTree`, `sparse.csgraph`
   (composantes connexes, plus courts chemins sur graphes de réseau).
4. Seulement en dernier recours, un paquet Rust **optionnel** (`rockmorph-core` sur PyPI) avec repli
   automatique en NumPy : `try: import rockmorph_core` / `except ImportError: version NumPy`. Candidats
   réalistes : χ sur tout un réseau de grande taille, SVF à grand rayon sur des MNT LiDAR de plus de
   100 millions de pixels.

---

## 4. Recommandation

| Horizon | Action |
|---|---|
| Maintenant | Pas de Rust. Corriger les algorithmes ci-dessus (knickpoints O(n), BFS vectorisé, Kahn, lecture unique des rasters). |
| Avant l'article | Écrire des **benchmarks reproductibles** (même MNT, même machine, temps et mémoire) : ils justifient l'argument « plus rapide que le workflow manuel ». |
| Plus tard, si un benchmark le montre | Appeler WhiteboxTools en option, puis seulement envisager un module Rust optionnel avec repli NumPy. |
