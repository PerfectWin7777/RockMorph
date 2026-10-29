# Documentation d'audit — RockMorph

Audit du dépôt RockMorph réalisé le 29/10/2026 sur la branche `main` (commit `76b17b9`).
L'audit est en **lecture seule** : aucun fichier de code n'a été modifié. Les correctifs
proposés sont décrits dans les documents, mais pas appliqués.

| Document | Contenu |
|---|---|
| [01_audit_complet.md](01_audit_complet.md) | État des lieux : architecture, inventaire des outils, points forts, bugs (critiques, majeurs, mineurs) avec fichier et ligne, validité scientifique par outil, manques |
| [02_rust.md](02_rust.md) | Faut-il ajouter du Rust ? Mesures de performance, goulots d'étranglement, recommandation |
| [03_qwebengine.md](03_qwebengine.md) | Peut-on se passer de QWebEngine ? Risques sous Windows et Qt6, alternatives, plan de migration |
| [04_feuille_de_route.md](04_feuille_de_route.md) | Priorités, plan d'intégration des ~60 indices, workflow automatique, stratégie pour l'article |
| [05_catalogue_indices.md](05_catalogue_indices.md) | Catalogue des indices géomorphologiques : RockMorph et GSV-Studio côte à côte, ce qui est correct, à corriger ou manquant |
| [06_gsv_studio_inventaire.md](06_gsv_studio_inventaire.md) | Étape 4 (prise de notes) : où se trouve la logique d'indices dans GSV-Studio, les 55 indices un par un, bugs, classifications, dépendances, croisement avec RockMorph, plan de portage et questions |

## Résumé en 10 lignes

1. L'architecture est bonne : moteurs de calcul séparés de l'UI (`BaseEngine`), panneaux (`BasePanel`), registre avec chargement à la demande, export centralisé. C'est une base solide pour un outil publiable.
2. Le périmètre couvre déjà 12 outils, dont plusieurs vont plus loin que les outils publiés (détection automatique des fronts montagneux, digitaliseur de cartes géologiques, suite de visualisation de terrain, explorateur 3D).
3. **Il y a des bugs scientifiques critiques dans l'outil fluvial.** Le χ (chi) est désaligné par rapport à l'altitude, ce qui fausse le chi-plot, les knickpoints, le ksn par segment et les exports CSV/GPKG. Vérifié numériquement : sur une rivière synthétique avec ksn = 100, la pente du chi-plot produit par le code vaut −83.
4. D'autres défauts touchent directement les résultats : statistiques de la rose fausses en mode axial, seuils Smf différents de ceux d'El Hamdouni et al. (2008), SL très bruité à cause d'un échantillonnage à 0,3 pixel, crash du mode « régression » du ksn.
5. `np.trapz` n'existe plus dans les NumPy 2.x récents. Le χ, le NCP et l'hypsométrie planteront donc sur les futures versions de QGIS.
6. Plateforme : QWebEngine est importé sans protection dans **tous** les panneaux. Sans PyQtWebEngine (cas fréquent sous Windows), aucun outil ne se charge, même ceux qui n'affichent pas de graphique.
7. Le registre importe `rockmorph.tools...` en chemin absolu. Si le dossier du plugin ne s'appelle pas exactement `rockmorph` (par exemple `RockMorph-main` après un téléchargement ZIP depuis GitHub), aucun outil ne se charge.
8. Rust : **inutile pour l'instant**. Les lenteurs mesurées viennent d'algorithmes en O(n²) et de boucles Python, qui se corrigent en NumPy. Voir 02.
9. QWebEngine : **on peut s'en passer pour tous les graphiques 2D** avec matplotlib, présent dans QGIS sur toutes les plateformes et meilleur pour les figures de publication. Pour la 3D, il faut le garder en option, avec un repli. Voir 03.
10. Il manque l'essentiel pour un article de type Pérez-Peña : Af, T, Vf, Bs, moments hypsométriques, l'agrégation Iat et un fournisseur Processing pour automatiser le workflow. C'est l'objet de l'étape 4 (ton dépôt de ~60 indices). Voir 04 et 05.
