# 05 — Catalogue des indices géomorphologiques

Bilan de ce que calculent **RockMorph** et **GSV-Studio** (ton dépôt de ~60 indices, analysé dans
[06_gsv_studio_inventaire.md](06_gsv_studio_inventaire.md)), comparé à la liste de référence des indices
utilisés dans les études de tectonique active et de morphométrie.

Légende : ✅ implémenté et correct · ⚠️ implémenté mais à corriger · ❌ absent ou inutilisable · — sans objet.
Les renvois Cx/Mx désignent l'audit RockMorph (01), les renvois §/Px l'inventaire GSV-Studio (06).
Entre crochets, l'identifiant de l'indice dans GSV-Studio.

> Les références et les seuils de classes sont donnés tels que couramment repris dans la littérature
> (notamment El Hamdouni et al., 2008). **Il faudra les vérifier dans les articles originaux avant publication.**

## A. Géométrie du bassin

| Indice | Formule | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|---|
| Aire A | — | — | ✅ (hypsométrie, watershed) | ⚠️ [BasinArea] valeur en m², libellée km² |
| Périmètre P | — | — | ❌ | ⚠️ [BasinPerimeter] valeur en m, libellée km |
| Longueur du bassin Lb | distance exutoire → point le plus éloigné | — | ❌ | ⚠️ grand côté du rectangle minimal orienté (approximation) |
| Rapport d'allongement Re | 2√(A/π) / Lb | Schumm 1956 | ❌ | ✅ [Re] |
| Coefficient de circularité Rc | 4πA / P² | Miller 1953 | ❌ | ✅ [Rc] |
| Facteur de forme Ff | A / Lb² | Horton 1932 | ❌ | ✅ [Rf] |
| Indice de compacité Kc | 0,28 P / √A | Gravelius 1914 | ❌ | ✅ [Compactness] |
| Forme du bassin Bs | Bl / Bw | Ramírez-Herrera 1998 | ❌ | ✅ [Bs] |
| Rapport de lemniscate k | Lb² / 4A | Chorley et al. 1957 | ❌ | ✅ [Lemniscate] |

## B. Relief et hypsométrie

| Indice | Formule | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|---|
| Relief du bassin H | Zmax − Zmin | — | ✅ (hypsométrie) | ✅ [TR] |
| Rapport de relief Rh | H / Lb | Schumm 1956 | ❌ | ✅ [Rra] |
| Rugosité de Melton | H / √A | Melton 1965 | ❌ | ❌ |
| Nombre de rugosité Rn | H · Dd | Strahler 1958 | ❌ | ✅ [Hd] (P9 : 0 sans réseau) |
| Intégrale hypsométrique HI | ∫ courbe normalisée | Strahler 1952 | ✅ (`np.trapz`, C6) | — |
| HI par rapport élévation-relief | (Zmoy − Zmin)/(Zmax − Zmin) | Pike & Wilson 1971 | ❌ | ✅ [HI] (paramètre de variante ignoré) |
| Moments hypsométriques (asymétrie, aplatissement, asymétrie et aplatissement de densité) | — | Harlin 1978 ; CalHypso (Pérez-Peña et al. 2009) | ❌ | ⚠️ [AltitudeSkewness/Kurtosis] moments des altitudes, pas de la courbe |
| Pente moyenne du bassin | moyenne des pentes | — | ❌ (pente raster seulement) | ⚠️ [SlopeMeanDegree/Percent] algorithme imposé |
| Relief local (fenêtre) | max − min dans un rayon | — | ⚠️ seulement le long des swaths | ❌ |

## C. Réseau hydrographique

| Indice | Formule | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|---|
| Ordre de Strahler | — | Strahler 1952 | ✅ (HydroFlow) | ⚠️ [StreamOrderMax] ordre du raster PySheds (P4) |
| Magnitude de Shreve | — | Shreve 1966 | ✅ (HydroFlow) | ❌ |
| Ordre de Horton | — | Horton 1945 | ✅ (HydroFlow) | ❌ |
| Rapport de bifurcation Rb | Nu / Nu+1 | Horton 1945 ; Schumm 1956 | ❌ | ⚠️ [RB] compte des segments, pas des cours d'eau |
| Rapport des longueurs Rl | Lu / Lu−1 | Horton 1945 | ❌ | ⚠️ [RL] idem |
| Densité de drainage Dd | ΣL / A | Horton 1932 | ❌ | ✅ [Dd] (P9) |
| Fréquence des cours d'eau Fs | ΣN / A | Horton 1932 | ❌ | ✅ [Fs] (P9) |
| Rapport de texture T | ΣN / P | Horton 1945 | ❌ | ✅ [Dt] (P9) |
| Constante d'entretien C | 1 / Dd | Schumm 1956 | ❌ | ✅ [C] |
| Longueur d'écoulement Lo | 1 / (2 Dd) | Horton 1945 | ❌ | ✅ [Lg] |
| Loi de Hack | L = c·A^h | Hack 1957 | ❌ | ❌ [Lar] coefficient en miles appliqué en km, longueur totale au lieu du chenal principal |
| Sinuosité du chenal S | longueur chenal / longueur vallée | Schumm 1963 | ❌ | ⚠️ [HSI] correct comme sinuosité simple, mal nommé « Mueller » ; chenal tronqué (P1) |

## D. Tectonique active (Iat et indices associés)

| Indice | Formule | Référence | Classes Iat | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|---|---|
| Gradient-longueur SL | (Δh/Δl)·L | Hack 1973 | 1 : ≥ 500 ; 2 : 300–500 ; 3 : < 300 | ⚠️ (C8) | ⚠️ [SL] bonne méthode par tronçons, chenal tronqué (P1), classes fausses (§5) |
| SLk / anomalies SL/K | SL / K, K = ΔH / ln L | Seeber & Gornitz 1983 | — | ⚠️ (C8) | ❌ |
| Facteur d'asymétrie Af | 100 · Ar / At | Hare & Gardner 1985 | \|Af − 50\| : 1 : ≥ 15 ; 2 : 7–15 ; 3 : < 7 | ❌ | ✅ [AF] (classification Balla 2023 à appliquer sur \|AF − 50\|) |
| Symétrie topographique transverse T | Da / Dd | Cox 1994 | — | ❌ | ⚠️ [TTSF] méthode par défaut non Cox, pas d'azimut |
| Rapport largeur/hauteur de fond de vallée Vf | 2 Vfw / [(Eld − Esc) + (Erd − Esc)] | Bull & McFadden 1977 | 1 : < 0,5 ; 2 : 0,5–1 ; 3 : ≥ 1 | ❌ | ⚠️ [VF] paramètres utilisateur ignorés (clé « Vf ») |
| Sinuosité du front montagneux Smf | Lmf / Ls | Bull & McFadden 1977 | 1 : < 1,1 ; 2 : 1,1–1,5 ; 3 : ≥ 1,5 | ⚠️ (C10) | ❌ |
| Forme du bassin Bs | Bl / Bw | Ramírez-Herrera 1998 | 1 : ≥ 4 ; 2 : 3–4 ; 3 : ≤ 3 | ❌ | ✅ [Bs] |
| HI (voir B) | | | 1 : ≥ 0,5 ; 2 : 0,4–0,5 ; 3 : < 0,4 | ✅ | ✅ [HI] |
| **Iat** | S / n (moyenne des classes) | El Hamdouni et al. 2008 | 1 : 1–1,5 ; 2 : 1,5–2 ; 3 : 2–2,5 ; 4 : ≥ 2,5 | ❌ | ✅ (Studio, générique ; voir §5 et §6) |

## E. Profils en long

| Indice | Formule | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|---|
| Profil longitudinal | Z(L) | — | ✅ | — (interne, non exporté) |
| Profil d'équilibre logarithmique | Z = a − b·ln L | Hack 1973 | ✅ | ❌ |
| Profil normalisé : Ca, MaxC, dL | — | Demoulin 1998 ; NProfiler (Pérez-Peña et al. 2017) | ✅ (`np.trapz`, C6) | ❌ |
| χ (chi) | ∫(A₀/A)^θ dx | Perron & Royden 2013 | ⚠️ (C1) | ⚠️ [Chi] statistique par défaut toujours 0 ; `k_chi` utile |
| ksn continu | S·A^θref | Wobus et al. 2006 | ⚠️ (C1, C8) | ⚠️ [Ksn] agrégé par bassin, plats forcés à S = 1e-5 |
| ksn par segment | Δz/Δχ ou régression | Kirby & Whipple 2001 ; Wobus et al. 2006 | ⚠️ (C1, C4) | ❌ |
| Concavité θ (régression pente-aire) | log S = log ks − θ log A | Flint 1974 | ⚠️ (bruité, C8) | ❌ |
| Knickpoints | segmentation du chi-plot | — | ⚠️ (C1, C7) | ❌ |
| Carte de χ du réseau (migration des lignes de partage des eaux) | — | Willett et al. 2014 | ❌ | ❌ |

## F. Dérivées de terrain

| Indice | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|
| Pente, exposition | — | ✅ (GDAL) | — (pente moyenne par bassin seulement) |
| Ombrage (standard, multidirectionnel, combiné, Igor) | — | ✅ (GDAL) | — |
| Courbures (profil, plan, totale) | Zevenbergen & Thorne 1987 | ❌ | ❌ (listées dans `TerrainAttribute`, non implémentées) |
| TPI | Weiss 2001 | ⚠️ 3×3 seulement (pas multi-échelle) | ❌ |
| TRI | Riley et al. 1999 | ✅ (GDAL) | ❌ |
| Rugosité VRM | Sappington et al. 2007 | ❌ | ❌ |
| Openness + / − | Yokoyama et al. 2002 | ✅ | ❌ |
| Sky View Factor (+ anisotrope) | Zakšek et al. 2011 | ✅ | ❌ |
| Sky illumination, local dominance | Kokalj & Somrak 2019 (RVT) | ✅ | ❌ |
| TWI | Beven & Kirkby 1979 | ❌ | ❌ [TWI] MNT utilisé à la place de la pente (§3.5) |
| SPI, facteur LS | Moore et al. 1991 | ❌ | ❌ [SPI] même bug ; LS absent |

## G. Swath et orientation

| Indice | Référence | RockMorph | Ton dépôt (GSV) |
|---|---|---|---|
| Swath min/max/moyenne/Q1/Q3 | Pérez-Peña et al. 2017 | ✅ | ❌ |
| Relief local du swath | — | ✅ | ❌ |
| Intégrale hypsométrique transverse (THi) | Pérez-Peña et al. 2017 | ✅ | ❌ |
| Rose diagram (count/length/frequency) | — | ✅ | ❌ |
| Statistiques circulaires axiales, test de Rayleigh | Mardia & Jupp 2000 | ⚠️ (C9) / ❌ | ❌ |

## H. Indices présents uniquement dans GSV-Studio

| ID GSV | Indice | Verdict | Remarque |
|---|---|---|---|
| TotalStreamLength, TotalNumSegments | longueur totale, nombre de segments | ⚠️ | unités / découpage |
| RA | rapport des surfaces (Schumm) | ⚠️ | aires locales au lieu des aires totales |
| Di | intensité de drainage | ⚠️ | Fs × Dd ou Fs / Dd ? (à vérifier, Faniran 1968) |
| Rfn | ratio de finesse (Melton) | ⚠️ | Lb au lieu de la longueur du chenal |
| LengthArea | La (Schumm) | ⚠️ | unités mélangées |
| TSI | sinuosité topographique (Mueller) | ⚠️ | formule différente de Mueller |
| AltitudeMin/Max/Mean/MedianHm | statistiques d'altitude | ✅ | médiane approchée |
| TD | dissection absolue (Hmoy − Hmin) | ✅ | |
| TRD | relief relatif au sommet | ❌ | code ≠ définition |
| Rr | relief relatif (H·100/P) | ✅ | |
| U | indice de Sinha-Roy | ⚠️ | formule et classes à vérifier |
| MASC, HG | pente axiale moyenne du chenal | ✅ | doublons (HG = MASC × 100), chenal tronqué |
| De, E, Ebulk | taux de dénudation, d'érosion, volume érodé | ⚠️ / ❌ | sources et unités à confirmer ; Ebulk non défini correctement |
| STI | transport de sédiments | ❌ | même bug que TWI/SPI |
| — | modeleur composite, AHP, ACP-PC1, K-means, CAH, DBSCAN, t-SNE, UMAP, Isolation Forest, LOF | ✅ | couche d'analyse multicritère (§6 de 06) |

---

## Bilan chiffré (sections A à G, 63 indices de référence)

| Statut | RockMorph seul | GSV-Studio seul | Meilleur des deux (sans correction) |
|---|---|---|---|
| ✅ correct | 20 | 18 | **36** |
| ⚠️ à corriger | 11 | 14 | 20 |
| ❌ absent ou inutilisable | 32 | 31 | 7 |

GSV-Studio apporte aussi les lignes de la section H, ainsi que l'Iat, l'AHP, le modeleur composite et le clustering.

**Conclusion** : à eux deux, les dépôts couvrent l'essentiel d'un article de type Iat. Une fois les 20 ⚠️
corrigés, il ne restera que **7 indices à créer** : rugosité de Melton, loi de Hack correcte, carte de χ du
réseau, courbures, rugosité VRM, TWI, et SPI/LS.
