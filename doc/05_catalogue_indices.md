# 05 — Catalogue des indices géomorphologiques

Bilan de ce que RockMorph calcule **aujourd'hui**, avec la liste de référence des indices utilisés dans les
études de tectonique active et de morphométrie. La colonne **Ton dépôt** sera remplie à l'étape 4, quand
tu fourniras le dépôt des ~60 indices.

Légende : ✅ implémenté et correct · ⚠️ implémenté mais à corriger (voir 01) · ❌ absent · — sans objet

> Les références et les seuils de classes sont donnés tels que couramment repris dans la littérature
> (notamment El Hamdouni et al., 2008). **Il faudra les vérifier dans les articles originaux avant publication.**

## A. Géométrie du bassin

| Indice | Formule | Référence | RockMorph | Ton dépôt |
|---|---|---|---|---|
| Aire A | — | — | ✅ (hypsométrie, watershed) | |
| Périmètre P | — | — | ❌ | |
| Longueur du bassin Lb | distance exutoire → point le plus éloigné | — | ❌ | |
| Rapport d'allongement Re | 2√(A/π) / Lb | Schumm 1956 | ❌ | |
| Coefficient de circularité Rc | 4πA / P² | Miller 1953 | ❌ | |
| Facteur de forme Ff | A / Lb² | Horton 1932 | ❌ | |
| Indice de compacité Kc | 0,28 P / √A | Gravelius 1914 | ❌ | |
| Forme du bassin Bs | Bl / Bw | Ramírez-Herrera 1998 | ❌ | |
| Rapport de lemniscate k | Lb² / 4A | Chorley et al. 1957 | ❌ | |

## B. Relief et hypsométrie

| Indice | Formule | Référence | RockMorph | Ton dépôt |
|---|---|---|---|---|
| Relief du bassin H | Zmax − Zmin | — | ✅ (hypsométrie) | |
| Rapport de relief Rh | H / Lb | Schumm 1956 | ❌ | |
| Rugosité de Melton | H / √A | Melton 1965 | ❌ | |
| Nombre de rugosité Rn | H · Dd | Strahler 1958 | ❌ | |
| Intégrale hypsométrique HI | ∫ courbe normalisée | Strahler 1952 | ✅ (`np.trapz`, C6) | |
| HI par rapport élévation-relief | (Zmoy − Zmin)/(Zmax − Zmin) | Pike & Wilson 1971 | ❌ | |
| Moments hypsométriques (asymétrie, aplatissement, asymétrie et aplatissement de densité) | — | Harlin 1978 ; CalHypso (Pérez-Peña et al. 2009) | ❌ | |
| Pente moyenne du bassin | moyenne des pentes | — | ❌ (pente raster seulement) | |
| Relief local (fenêtre) | max − min dans un rayon | — | ⚠️ seulement le long des swaths | |

## C. Réseau hydrographique

| Indice | Formule | Référence | RockMorph | Ton dépôt |
|---|---|---|---|---|
| Ordre de Strahler | — | Strahler 1952 | ✅ (HydroFlow) | |
| Magnitude de Shreve | — | Shreve 1966 | ✅ (HydroFlow) | |
| Ordre de Horton | — | Horton 1945 | ✅ (HydroFlow) | |
| Rapport de bifurcation Rb | Nu / Nu+1 | Horton 1945 ; Schumm 1956 | ❌ | |
| Rapport des longueurs Rl | Lu / Lu−1 | Horton 1945 | ❌ | |
| Densité de drainage Dd | ΣL / A | Horton 1932 | ❌ | |
| Fréquence des cours d'eau Fs | ΣN / A | Horton 1932 | ❌ | |
| Rapport de texture T | ΣN / P | Horton 1945 | ❌ | |
| Constante d'entretien C | 1 / Dd | Schumm 1956 | ❌ | |
| Longueur d'écoulement Lo | 1 / (2 Dd) | Horton 1945 | ❌ | |
| Loi de Hack | L = c·A^h | Hack 1957 | ❌ | |
| Sinuosité du chenal S | longueur chenal / longueur vallée | Schumm 1963 | ❌ | |

## D. Tectonique active (Iat et indices associés)

| Indice | Formule | Référence | Classes Iat | RockMorph | Ton dépôt |
|---|---|---|---|---|---|
| Gradient-longueur SL | (Δh/Δl)·L | Hack 1973 | 1 : ≥ 500 ; 2 : 300–500 ; 3 : < 300 | ⚠️ (C8) | |
| SLk / anomalies SL/K | SL / K, K = ΔH / ln L | Seeber & Gornitz 1983 | — | ⚠️ (C8) | |
| Facteur d'asymétrie Af | 100 · Ar / At | Hare & Gardner 1985 | \|Af − 50\| : 1 : ≥ 15 ; 2 : 7–15 ; 3 : < 7 | ❌ | |
| Symétrie topographique transverse T | Da / Dd | Cox 1994 | — | ❌ | |
| Rapport largeur/hauteur de fond de vallée Vf | 2 Vfw / [(Eld − Esc) + (Erd − Esc)] | Bull & McFadden 1977 | 1 : < 0,5 ; 2 : 0,5–1 ; 3 : ≥ 1 | ❌ | |
| Sinuosité du front montagneux Smf | Lmf / Ls | Bull & McFadden 1977 | 1 : < 1,1 ; 2 : 1,1–1,5 ; 3 : ≥ 1,5 | ⚠️ (C10) | |
| Forme du bassin Bs | Bl / Bw | Ramírez-Herrera 1998 | 1 : ≥ 4 ; 2 : 3–4 ; 3 : ≤ 3 | ❌ | |
| HI (voir B) | | | 1 : ≥ 0,5 ; 2 : 0,4–0,5 ; 3 : < 0,4 | ✅ | |
| **Iat** | S / n (moyenne des classes) | El Hamdouni et al. 2008 | 1 : 1–1,5 ; 2 : 1,5–2 ; 3 : 2–2,5 ; 4 : ≥ 2,5 | ❌ | |

## E. Profils en long

| Indice | Formule | Référence | RockMorph | Ton dépôt |
|---|---|---|---|---|
| Profil longitudinal | Z(L) | — | ✅ | |
| Profil d'équilibre logarithmique | Z = a − b·ln L | Hack 1973 | ✅ | |
| Profil normalisé : Ca, MaxC, dL | — | Demoulin 1998 ; NProfiler (Pérez-Peña et al. 2017) | ✅ (`np.trapz`, C6) | |
| χ (chi) | ∫(A₀/A)^θ dx | Perron & Royden 2013 | ⚠️ (C1) | |
| ksn continu | S·A^θref | Wobus et al. 2006 | ⚠️ (C1, C8) | |
| ksn par segment | Δz/Δχ ou régression | Kirby & Whipple 2001 ; Wobus et al. 2006 | ⚠️ (C1, C4) | |
| Concavité θ (régression pente-aire) | log S = log ks − θ log A | Flint 1974 | ⚠️ (bruité, C8) | |
| Knickpoints | segmentation du chi-plot | — | ⚠️ (C1, C7) | |
| Carte de χ du réseau (migration des lignes de partage des eaux) | — | Willett et al. 2014 | ❌ | |

## F. Dérivées de terrain

| Indice | Référence | RockMorph | Ton dépôt |
|---|---|---|---|
| Pente, exposition | — | ✅ (GDAL) | |
| Ombrage (standard, multidirectionnel, combiné, Igor) | — | ✅ (GDAL) | |
| Courbures (profil, plan, totale) | Zevenbergen & Thorne 1987 | ❌ | |
| TPI | Weiss 2001 | ⚠️ 3×3 seulement (pas multi-échelle) | |
| TRI | Riley et al. 1999 | ✅ (GDAL) | |
| Rugosité VRM | Sappington et al. 2007 | ❌ | |
| Openness + / − | Yokoyama et al. 2002 | ✅ | |
| Sky View Factor (+ anisotrope) | Zakšek et al. 2011 | ✅ | |
| Sky illumination, local dominance | Kokalj & Somrak 2019 (RVT) | ✅ | |
| TWI | Beven & Kirkby 1979 | ❌ | |
| SPI, facteur LS | Moore et al. 1991 | ❌ | |

## G. Swath et orientation

| Indice | Référence | RockMorph | Ton dépôt |
|---|---|---|---|
| Swath min/max/moyenne/Q1/Q3 | Pérez-Peña et al. 2017 | ✅ | |
| Relief local du swath | — | ✅ | |
| Intégrale hypsométrique transverse (THi) | Pérez-Peña et al. 2017 | ✅ | |
| Rose diagram (count/length/frequency) | — | ✅ | |
| Statistiques circulaires axiales, test de Rayleigh | Mardia & Jupp 2000 | ⚠️ (C9) / ❌ | |

---

## Bilan chiffré (état actuel)

| Statut | Nombre (sur les 63 indices listés) |
|---|---|
| ✅ implémenté et correct | 20 |
| ⚠️ implémenté, à corriger ou compléter | 11 |
| ❌ absent | 32 |

Les quatre manques qui empêchent aujourd'hui un article de type Iat sont **Af, Vf, Bs et l'agrégation Iat**,
plus SL et Smf à corriger. La géométrie de bassin (Re, Rc, Ff, Kc) et le réseau (Rb, Dd, Fs) devraient
venir rapidement avec ton dépôt.
