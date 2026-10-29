# 03 — Peut-on se passer de QWebEngine ?

## Réponse courte

**Oui pour tous les graphiques 2D, et c'est même préférable. Pour la 3D, on le garde en option avec un repli.**

Ton inquiétude est fondée : aujourd'hui, QWebEngine est un **point de défaillance unique** pour toute
l'extension.

---

## 1. Comment RockMorph utilise QWebEngine aujourd'hui

- `base/base_panel.py:7` : `from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings`,
  exécuté au chargement de **chaque** panneau.
- `base/base_panel.py:192` : une `QWebEngineView` (donc un processus Chromium) est créée pour **chaque**
  panneau ouvert, y compris Terrain Derivatives, HydroFlow, Watershed et Digitizer, qui n'en ont pas besoin
  (ils renvoient `empty.html` ou `""` et surchargent `_load_html`).
- Le pont Python ↔ JS passe par `QWebChannel` (`web/js/bridge.js`) : export SVG, survol, clic.
- Graphiques 2D : Plotly (swath, rose, hypsométrie, NCP, fluvial, SMF). 3D : three.js r128 + shaders GLSL
  (`web/js/explorer3d_render.js`, 2 585 lignes).
- Réglages `LocalContentCanAccessRemoteUrls` et `AllowRunningInsecureContent` activés : surface d'attaque
  inutile pour du contenu local.

## 2. Pourquoi c'est un problème

1. **Windows.** Historiquement, les installeurs officiels de QGIS 3 pour Windows (OSGeo4W et installeur
   autonome) ne fournissent **pas** les liaisons Python PyQtWebEngine ; ils fournissent QtWebKit.
   *Test à faire sur une machine cible, dans la console Python de QGIS :*
   ```python
   from PyQt5.QtWebEngineWidgets import QWebEngineView
   ```
   Si cette ligne échoue, **aucun outil RockMorph ne se charge**, car l'import est fait par la classe de base.
   L'installation manuelle (`pip install PyQtWebEngine` dans le Python de QGIS) doit correspondre exactement
   à la version de Qt 5.15 d'OSGeo4W. C'est fragile et hors de portée de la plupart des géologues.
2. **QGIS 4 / Qt6.** Il faudra migrer vers `PyQt6.QtWebEngineWidgets`, dont la disponibilité dépendra encore
   de chaque distribution.
3. **Coût en ressources.** Une vue = un processus Chromium (plusieurs dizaines de Mo) ; avec 8 panneaux
   ouverts, la mémoire grimpe vite. Le WebGL dépend du pilote graphique, et sur les machines virtuelles
   ou les bureaux distants, il peut tomber en rendu logiciel ou en écran noir.
4. **Qualité des figures de publication.** Le chemin actuel (Plotly → SVG → `QSvgRenderer` ou `cairosvg`)
   fonctionne, mais `QSvgRenderer` ne gère qu'un sous-ensemble de SVG (SVG Tiny 1.2). Certains
   éléments Plotly (dégradés, `clipPath` complexes, MathJax) sont mal rendus à l'export PNG/PDF.

## 3. Alternatives évaluées

| Option | Disponible dans QGIS sans installation ? | 2D | 3D | Export publication | Interactivité | Verdict |
|---|---|---|---|---|---|---|
| **matplotlib** (`FigureCanvasQTAgg` intégré dans le dock) | Oui : fourni par OSGeo4W (Windows), le bundle macOS et les paquets Linux (**à confirmer** sur tes machines cibles avec `import matplotlib`) | ✅ excellent | ⚠️ `mplot3d` seulement, lent | ✅ **natif** : SVG/PDF/EPS vectoriels, PNG à n'importe quel DPI, polices et mise en page contrôlées, standard des revues | ✅ survol (`motion_notify_event`), clic (`pick_event`), zoom/pan (`NavigationToolbar2QT`) | **Recommandé pour toute la 2D** |
| Plotly dans le **navigateur système** (`QDesktopServices.openUrl` sur un HTML autonome) | Oui | ✅ | ✅ | via Plotly | ✅ mais hors QGIS, pas de lien carte ↔ graphe | Bon **repli** |
| QtWebKit (`qgis.PyQt.QtWebKitWidgets`) | Oui en QGIS 3 Windows, absent en Qt6 | ⚠️ JS ancien, Plotly 2.x non garanti | ❌ WebGL très limité | — | — | Non : voie sans avenir |
| **3D native QGIS** (`Qgs3DMapCanvas`, `Qgs3DMapSettings`, module `qgis._3d`) | Oui (QGIS 3.x, API Python partielle) | — | ✅ terrain, drapage, vecteurs extrudés, rendu GPU natif | captures d'écran | ✅ | **À prototyper** comme repli 3D |
| pyqtgraph / PyVista / VTK | Non garanti, installation manuelle | ✅ | ✅ | moyen | ✅ | Non : même problème de dépendance |
| Graphiques natifs QGIS (`QgsPlot`, profil d'élévation 3.26+) | Oui | ⚠️ limité, API instable | — | via mise en page | partielle | Pour des cas ciblés seulement |

## 4. Architecture recommandée

1. **Découpler l'affichage du panneau.** Introduire une interface `PlotBackend` (`render(data)`,
   `export(path, fmt, dpi)`, signaux `hovered(distance)` / `clicked(fid)`), avec deux implémentations :
   - `MatplotlibBackend` : **par défaut** pour Swath, Rose, Hypsométrie, NCP, Fluvial et SMF ;
   - `WebBackend` : l'actuel, Plotly + QWebChannel, gardé en **option** pour qui veut l'interactivité Plotly.
2. **Import protégé.** Ne plus importer QWebEngine dans `base_panel.py`. Le faire seulement dans `WebBackend`
   et dans l'explorateur 3D :
   ```python
   try:
       from qgis.PyQt.QtWebEngineWidgets import QWebEngineView
       HAS_WEBENGINE = True
   except ImportError:
       HAS_WEBENGINE = False
   ```
   Ne créer une vue web que si le panneau en a besoin.
3. **Explorateur 3D sans QWebEngine** : bouton « Ouvrir dans le navigateur ». L'export HTML autonome existe
   déjà (`tools/explorer3d/panel.py`, repli CDN). En second temps, un repli « Vue 3D QGIS » qui configure
   un `Qgs3DMapCanvas` avec le MNT et les couches drapées.
4. **Exports** : avec matplotlib, `savefig(path, dpi=…, format=…)` remplace toute la chaîne
   SVG → QSvgRenderer/cairosvg. `core/exporter.py` se simplifie et devient plus fiable.

## 5. Plan de migration par étapes (à faire plus tard, sur ton accord)

| Étape | Contenu | Risque |
|---|---|---|
| 1 | Import protégé de QWebEngine, aucune vue créée pour les panneaux sans HTML, message clair si la 3D est indisponible | faible, débloque Windows immédiatement |
| 2 | `PlotBackend` + `MatplotlibBackend` pour un premier outil simple (Hypsométrie ou Rose), comparaison visuelle avec Plotly | faible |
| 3 | Migration de Swath, NCP, SMF, puis Fluvial (le plus riche : 2 axes, segments, annotations) | moyen |
| 4 | Feuille de style matplotlib « RockMorph » (polices, tailles, palettes) pour des figures homogènes, prêtes pour la revue | faible |
| 5 | Plotly/QWebEngine en mode optionnel ; 3D : navigateur + prototype `Qgs3DMapCanvas` | moyen |
| 6 | Passage à `qgis.PyQt` partout (prérequis pour QGIS 4) | moyen, mécanique |

## 6. Bilan

| | QWebEngine (actuel) | matplotlib (proposé pour la 2D) |
|---|---|---|
| Installation Windows | ❌ souvent absente | ✅ fournie |
| Qt6 / QGIS 4 | ⚠️ à revalider | ✅ backend `QtAgg` compatible Qt5 et Qt6 |
| Mémoire | élevée (Chromium par vue) | faible |
| Figures de revue (SVG/PDF/EPS, DPI) | indirect, rendu SVG partiel | natif, standard de facto |
| Interactivité | très bonne | bonne (survol, clic, zoom) |
| Liaison carte ↔ graphe | via QWebChannel | signaux Qt directs, plus simple |
