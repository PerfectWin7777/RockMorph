# 04 — Feuille de route

Objectif : que RockMorph couvre **tout le workflow d'un géomorphologue** (du MNT à la figure publiée),
de façon automatique et reproductible, et que l'outil puisse être publié dans une revue.

> Ce document est un plan, pas des changements. Aucune étape ne sera faite sans ton accord explicite.

---

## Phase 0 — Fiabiliser les résultats (avant toute publication)

Priorité absolue : un relecteur qui retrouverait l'un de ces bugs disqualifierait l'outil.

| # | Action | Réf. audit |
|---|---|---|
| 0.1 | Aligner χ, Z, ksn sur le même ordre ; corriger la droite d'équilibre du chi-plot | C1 |
| 0.2 | Exports CSV/GPKG cohérents (même ordre, géométrie orientée source → embouchure, interpolation en mètres) | C2, C3 |
| 0.3 | Corriger le crash du mode régression et le recalcul du curseur θ | C4, C5 |
| 0.4 | Remplacer `np.trapz` | C6 |
| 0.5 | Knickpoints : recherche gloutonne globale en O(n) + critère d'arrêt (BIC) | C7 |
| 0.6 | SL par tronçons (équidistance verticale) | C8 |
| 0.7 | Statistiques axiales de la rose (doublement d'angle), test de Rayleigh | C9 |
| 0.8 | Seuils Smf conformes à El Hamdouni et al. (2008), paramétrables ; NoData ≠ 0 | C10 |
| 0.9 | **Tests unitaires** sur cas analytiques (rivière à ksn fixé, cône, bassin circulaire, rose à azimut connu) | — |

## Phase 1 — Rendre l'extension installable partout

| # | Action | Réf. |
|---|---|---|
| 1.1 | Import protégé de QWebEngine ; pas de vue web pour les panneaux qui n'en ont pas besoin | M1, doc 03 |
| 1.2 | Chemins de modules relatifs dans le registre | M3 |
| 1.3 | `qgis.PyQt` partout (préparation QGIS 4) | M2 |
| 1.4 | `QgsTask` + annulation + `QgsFeedback` ; plus d'accès aux couches depuis un QThread | M4 |
| 1.5 | `icon.png`, `LICENSE` (GPL-2.0-or-later conseillée, comme QGIS), `README`, `changelog`, `about` exact | M6 |
| 1.6 | Identifiants GRASS à jour + alternative sans GRASS | M5 |
| 1.7 | Graphiques 2D en matplotlib | doc 03 |

## Phase 2 — Intégrer les ~60 indices (étape 4, avec ton second dépôt)

### Architecture proposée

```
core/indices/                ← bibliothèque PURE (NumPy, sans QGIS ni Qt) → testable, réutilisable, citable
    __init__.py              ← registre INDICES = {id: IndexSpec}
    spec.py                  ← dataclass IndexSpec: id, nom, famille, formule (LaTeX), unités,
                               entrées requises, référence bibliographique, classes Iat éventuelles
    basin_shape.py           ← Re, Rc, Ff, Kc, Bs, …
    relief.py                ← Rh, Melton, HI, moments hypsométriques, …
    network.py               ← Rb, Rl, Dd, Fs, Hack, …
    tectonic.py              ← Af, T, Vf, Smf, SL, Iat, …
    profiles.py              ← χ, ksn, NCP, knickpoints (déplacés depuis tools/fluvial)
tests/indices/               ← une valeur de référence publiée (ou analytique) par indice
processing/provider.py       ← QgsProcessingProvider : un algorithme par indice + un algorithme « Tous les indices »
tools/indices/panel.py       ← panneau « Morphometric Indices » : choisir les bassins, cocher les indices, tableau + carte
```

Principes :
1. **Séparer l'extraction géométrique (QGIS) du calcul (NumPy).** QGIS fournit aires, périmètres,
   longueurs et altitudes ; les fonctions d'indices ne reçoivent que des nombres et des tableaux.
   On peut alors les tester sans QGIS, les réutiliser en script et les publier.
2. **Chaque indice porte sa référence et ses classes.** Le rapport, l'infobulle de l'interface et la
   section Méthodes de l'article sortent de la même source.
3. **Ne rien dupliquer.** Les indices déjà présents (HI, SL, ksn, χ, Smf, NCP…) sont déplacés dans
   `core/indices` et appelés par les outils existants.
4. **Iat automatique** : SL, Af, HI, Vf, Bs, Smf → classes 1–3 → Iat = S/n → classes 1–4, avec carte et
   tableau (El Hamdouni et al. 2008 ; Pérez-Peña et al. 2010).

Démarche quand tu fourniras le dépôt :
1. Inventaire des fonctions → remplir les colonnes « Ton dépôt » de [05_catalogue_indices.md](05_catalogue_indices.md).
2. Comparer chaque formule avec la référence publiée et avec l'éventuelle implémentation existante dans RockMorph.
3. Écrire le test **avant** de porter la fonction, puis porter.
4. Brancher ensuite le provider Processing et le panneau.

## Phase 3 — Workflow automatique de bout en bout

« Projet RockMorph » = un fichier JSON de paramètres + un algorithme Processing maître :

```
MNT ─► conditionnement (comblement/brèche) ─► FDR/FAC ─► réseau + ordres (Strahler/Shreve/Horton)
    ─► bassins (seuil ou exutoires) ─► rivières principales
    ─► tous les indices (bassin, relief, réseau, tectonique, profils)
    ─► classes + Iat ─► couches GPKG stylées
    ─► mises en page QgsPrintLayout (carte Iat, carte ksn/χ, carte SL, swaths, hypsométrie)
    ─► export figures (PDF/SVG/PNG 300–600 dpi) + tableaux (CSV/XLSX/LaTeX) + rapport HTML/PDF
    ─► fichier de reproductibilité (versions QGIS/GDAL/NumPy/RockMorph, tous les paramètres)
```

C'est ce qui remplace la chaîne ArcGIS + Excel + Illustrator : les calculs dans les moteurs, les cartes
dans `QgsPrintLayout`, les graphiques en matplotlib avec un style de revue, et tout le paramétrage dans un fichier.

## Phase 4 — L'article

### Positionnement

Outils comparables à citer et à battre :
- **CalHypso** (Pérez-Peña et al., 2009, *Computers & Geosciences*) : courbes et moments hypsométriques (ArcGIS).
- **SwathProfiler et NProfiler** (Pérez-Peña et al., 2017, *Computers & Geosciences*) : swaths et profils normalisés (ArcGIS).
- **SLiX** et **Knickpoint Finder** (Queiroz et al., 2015) : SL et knickzones.
- **TopoToolbox** (Schwanghart & Scherler, 2014, MATLAB), **LSDTopoTools** (Mudd et al.), **TAK** (Forte & Whipple, 2019), **ChiProfiler** (Gallen & Wegmann, 2017).

Argument central de RockMorph : **un seul environnement libre (QGIS), un seul workflow automatique, de
l'acquisition à la figure**. Ses ajouts originaux (MFSD, digitaliseur, 3D/STL, suite de visualisation)
ne se trouvent pas tous dans les outils ci-dessus.

### Ce qu'il faudra démontrer

1. **Validité** : sur un même MNT et les mêmes bassins, des résultats identiques (aux arrondis près) à
   CalHypso, SwathProfiler/NProfiler et TopoToolbox pour HI, swath, NCP, χ et ksn. Tableaux et graphes
   de comparaison.
2. **Gain de temps** : protocole chronométré, workflow manuel (ArcGIS + Excel + Illustrator) contre RockMorph,
   sur un cas d'étude réel. « Quatre fois plus rapide » doit être **mesuré**, avec la méthode décrite.
3. **Reproductibilité** : code ouvert (GitHub), version archivée avec DOI (Zenodo), jeu de données de test,
   fichier de paramètres de l'étude.
4. **Cas d'étude** : une région tectoniquement active, avec une carte Iat, idéalement comparée à une étude déjà publiée.

### Revues possibles

- *Computers & Geosciences* : la revue des CalHypso et SwathProfiler, donc le choix le plus naturel.
- *Environmental Modelling & Software*, *SoftwareX*, *Journal of Open Source Software* (court, centré sur le logiciel).
- *Geomorphology* ou *Earth Surface Dynamics* : plutôt pour l'article d'application (cas d'étude Iat), qui citera l'article logiciel.

Stratégie possible : un article logiciel (C&G ou SoftwareX), puis un article d'application régional (Geomorphology).
