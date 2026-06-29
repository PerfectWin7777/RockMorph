# core/digitizer.py

"""
core/digitizer.py

Pure mathematical and image processing algorithms for Geological Map Digitization.
Uses only NumPy and SciPy. Zero QGIS/GDAL dependencies here.

Performance notes
-----------------
K-Means distance computation
    Old : np.linalg.norm(pixels[:, None] - centroids[None], axis=2)
          Allocates an (N, K, 3) intermediate array — up to 384 MB on a
          2000×2000 image with K=8.  Repeated every iteration.
    New : ||x - c||² = ||x||² - 2<x,c> + ||c||²
          Computed with np.dot (BLAS DGEMM) — no large intermediate array,
          ~3-5x faster in practice.

K-Means centroid update
    Old : Python loop + boolean mask copy per cluster → O(K × N) copies.
    New : np.bincount + np.add.at → single pass, no copies.

Pixel subsampling
    Geological maps have large uniform color regions.  Finding centroids on
    a random 20-30% subsample gives nearly identical results to using the
    full image, while cutting K-Means time proportionally.  Full-image
    assignment is still done once at the end.
"""

import numpy as np # type: ignore
from scipy.ndimage import (    # type: ignore
median_filter, label, distance_transform_edt, binary_closing, 
gaussian_filter, uniform_filter,distance_transform_edt,
gaussian_gradient_magnitude
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def kmeans_image_segmentation(
    img_rgb:       np.ndarray,
    k:             int,
    max_iter:      int   = 30,
    tol:           float = 1.0,
    subsample_pct: int   = 30,
    random_seed:   int   = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Fast K-Means clustering for RGB image segmentation using pure NumPy.

    Optimisations vs previous version
    ----------------------------------
    1. Distance computation via the algebraic identity
           ||x - c||² = ||x||² - 2 x·cᵀ + ||c||²
       evaluated with np.dot (BLAS level-3).  No (N, K, 3) intermediate
       array — memory footprint drops from ~384 MB to ~32 MB for a
       2000×2000 image with K=8.

    2. Centroid update with np.bincount — replaces a Python loop that
       created K boolean mask copies per iteration.

    3. Pixel subsampling — centroids are found on a random subset
       (``subsample_pct`` % of pixels), then the final label assignment
       uses the full image.  Geological maps have large uniform regions so
       30 % gives centroids indistinguishable from 100 %.

    Parameters
    ----------
    img_rgb       : np.ndarray  shape (rows, cols, 3)
    k             : int   — number of color clusters
    max_iter      : int   — maximum iterations (default 30, was 50)
    tol           : float — centroid shift convergence threshold
    subsample_pct : int   — percentage of pixels used for centroid search
                            (1-100, default 30)
    random_seed   : int   — for reproducibility (default 42)

    Returns
    -------
    labels    : np.ndarray  shape (rows, cols)  dtype int32
    centroids : np.ndarray  shape (k, 3)        dtype uint8
    """
    rng = np.random.default_rng(random_seed)

    rows, cols, channels = img_rgb.shape
    pixels = img_rgb.reshape(-1, channels).astype(np.float32)
    n_pixels = pixels.shape[0]

    # ── Subsampling for centroid search ───────────────────────────────
    subsample_pct = max(1, min(100, subsample_pct))
    n_sample = max(k * 10, int(n_pixels * subsample_pct / 100))
    n_sample = min(n_sample, n_pixels)

    sample_idx = rng.choice(n_pixels, size=n_sample, replace=False)
    sample     = pixels[sample_idx]          # shape (n_sample, 3)

    # ── K-Means++ initialisation ──────────────────────────────────────
    # Much better starting centroids than pure random — converges in fewer
    # iterations, especially when colors are well-separated on the map.
    centroids = _kmeans_plus_plus_init(sample, k, rng)

    # ── Iterative centroid refinement on the subsample ────────────────
    sample_labels = np.zeros(n_sample, dtype=np.int32)

    for _ in range(max_iter):
        new_labels = _assign_labels(sample, centroids)

        new_centroids = _update_centroids(sample, new_labels, k, centroids)

        shift = float(np.abs(new_centroids - centroids).max())
        centroids     = new_centroids
        sample_labels = new_labels

        if shift < tol:
            break

    # ── Final assignment on the FULL image ───────────────────────────
    # Centroids are now stable — assign every pixel once.
    labels = _assign_labels(pixels, centroids)

    return labels.reshape(rows, cols), centroids.astype(np.uint8)


def clean_segmentation_noise(
    labels:      np.ndarray,
    smooth_size: int = 5,
) -> np.ndarray:
    """
    Applies a median filter to the label raster to remove salt-and-pepper
    noise, scanned map artefacts, and thin boundary lines.

    The median filter is ideal for categorical data because it never
    creates label values that were not present in the input.

    Parameters
    ----------
    labels      : np.ndarray  shape (rows, cols)
    smooth_size : int  — kernel size (must be odd; forced odd if even)

    Returns
    -------
    cleaned_labels : np.ndarray  shape (rows, cols)
    """
    if smooth_size < 3:
        return labels

    # Force odd kernel size
    if smooth_size % 2 == 0:
        smooth_size += 1

    return median_filter(labels, size=smooth_size)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _kmeans_plus_plus_init(
    pixels: np.ndarray,
    k:      int,
    rng:    np.random.Generator,
) -> np.ndarray:
    """
    K-Means++ centroid initialisation.

    Selects the first centroid uniformly at random, then each subsequent
    centroid is chosen with probability proportional to the squared distance
    to the nearest already-chosen centroid.

    This produces better initial centroids than pure random, reducing both
    the number of iterations needed and the risk of degenerate solutions
    (empty clusters on uniform-coloured regions).

    Complexity: O(K × N) — acceptable for K ≤ 50.
    """
    n = pixels.shape[0]
    first_idx = int(rng.integers(0, n))
    centroids = [pixels[first_idx]]

    for _ in range(1, k):
        # Squared distance from every pixel to its nearest centroid
        # Shape: (n, len(centroids)) — still manageable because len ≤ k
        c_arr = np.array(centroids, dtype=np.float32)   # (c, 3)
        dists = _sq_distances(pixels, c_arr)             # (n, c)
        min_dists = dists.min(axis=1).astype(np.float64) # (n,)
                   

        # Sample proportionally to min_dists (avoid zero-sum edge case)
        total = min_dists.sum()
        if total == 0.0:
            probs = np.ones(n, dtype=np.float64) / n
        else:
            probs = (min_dists / total).astype(np.float64)
            probs /= probs.sum()

        chosen = int(rng.choice(n, p=probs))
        centroids.append(pixels[chosen])

    return np.array(centroids, dtype=np.float32)


def _sq_distances(pixels: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    """
    Compute squared Euclidean distances between every pixel and every
    centroid using the algebraic identity:

        ||x - c||² = ||x||² - 2 x·cᵀ + ||c||²

    This replaces the naive broadcast subtraction which allocates an
    (N, K, 3) intermediate array.  np.dot calls BLAS DGEMM and is
    3-5x faster for large N.

    Parameters
    ----------
    pixels    : np.ndarray  (N, 3)  float32
    centroids : np.ndarray  (K, 3)  float32

    Returns
    -------
    sq_dists : np.ndarray  (N, K)  float32
    """
    # ||x||²  shape (N,)
    px_sq = (pixels * pixels).sum(axis=1)

    # ||c||²  shape (K,)
    c_sq = (centroids * centroids).sum(axis=1)

    # -2 x·cᵀ  shape (N, K)
    cross = np.dot(pixels, centroids.T)

    # Broadcast to (N, K)
    sq_dists = px_sq[:, None] - 2.0 * cross + c_sq[None, :]

    # Numerical safety — clamp small negatives to zero
    np.clip(sq_dists, 0.0, None, out=sq_dists)

    return sq_dists


def _assign_labels(pixels: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    """
    Assign each pixel to its nearest centroid.

    Uses _sq_distances internally — no large intermediate array.

    Returns
    -------
    labels : np.ndarray  (N,)  int32
    """
    sq_dists = _sq_distances(pixels, centroids)
    return np.argmin(sq_dists, axis=1).astype(np.int32)


def _update_centroids(
    pixels:    np.ndarray,
    labels:    np.ndarray,
    k:         int,
    prev_centroids: np.ndarray,
) -> np.ndarray:
    """
    Recompute centroids as the mean RGB of their assigned pixels.

    Uses np.bincount + vectorised summation — no Python loop over clusters,
    no boolean mask copies.

    Empty clusters (no pixels assigned) keep their previous centroid to
    avoid NaN propagation and degenerate solutions.

    Parameters
    ----------
    pixels         : (N, 3) float32
    labels         : (N,)   int32
    k              : int
    prev_centroids : (K, 3) float32  — fallback for empty clusters

    Returns
    -------
    new_centroids : np.ndarray  (K, 3)  float32
    """
    counts = np.bincount(labels, minlength=k).astype(np.float32)  # (K,)
    channels = pixels.shape[1]

    # Sum of values per cluster — one pass per channel
    sums = np.zeros((k, channels), dtype=np.float32)
    for ch in range(channels):
        sums[:, ch] = np.bincount(labels, weights=pixels[:, ch], minlength=k)

    # Avoid division by zero for empty clusters
    empty = (counts == 0)
    counts_safe = np.where(empty, 1.0, counts)

    new_centroids = sums / counts_safe[:, None]

    # Restore previous centroid for empty clusters
    new_centroids[empty] = prev_centroids[empty]

    return new_centroids


def preprocess_geological_map(img_rgb: np.ndarray, filter_size: int = 7) -> np.ndarray:
    """
    Élimine les lignes de grille, le texte, et fusionne les hachures AVANT la segmentation.
    Utilise un filtre médian sur chaque canal RGB.
    Le filtre médian a la propriété magique d'effacer les détails fins (lignes/textes)
    tout en gardant les frontières principales parfaitement nettes.
    """
    
    # On s'assure que la taille est impaire
    if filter_size % 2 == 0:
        filter_size += 1
        
    cleaned_rgb = np.zeros_like(img_rgb)
    
    # On applique le filtre médian sur chaque couleur (Rouge, Vert, Bleu)
    for i in range(3):
        cleaned_rgb[:, :, i] = median_filter(img_rgb[:, :, i], size=filter_size)
        
    return cleaned_rgb

# ---------------------------------------------------------------------------
# NOUVELLE APPROCHE : WATERSHED HYBRIDE (SPATIAL + COULEUR)
# ---------------------------------------------------------------------------

# def watershed_image_segmentation(
#     img_rgb: np.ndarray,
#     k: int,
#     line_threshold: float = 15.0,
#     closing_size: int = 3,
#     random_seed: int = 42
# ) -> tuple[np.ndarray, np.ndarray]:
#     """
#     Segmentation de Niveau 3 : Approche Morphologique (Spatial d'abord, Couleur ensuite).
    
#     1. Détecte les bordures (lignes noires) par filtrage local.
#     2. Identifie les "bassins" géologiques clos.
#     3. Étend les bassins pour absorber les bordures (Voronoi expansion).
#     4. Calcule la couleur moyenne de chaque bassin.
#     5. Applique K-Means uniquement sur les bassins pour les regrouper en `k` unités.
#     """
#     rows, cols, _ = img_rgb.shape
    
#     # 1. Convertir en niveaux de gris (Float32 pour éviter les underflows)
#     gray = img_rgb.mean(axis=2).astype(np.float32)

#     # 2. Détection des lignes sombres (Différence de Gaussiennes)
#     # On compare l'image floutée à l'image nette. Les lignes fines ressortent très fort.
#     blurred = gaussian_filter(gray, sigma=5)
#     dark_lines = (blurred - gray) > line_threshold

#     # 3. Fermer les trous dans les lignes (Morphological Closing)
#     if closing_size > 0:
#         kernel = np.ones((closing_size, closing_size), dtype=bool)
#         dark_lines = binary_closing(dark_lines, structure=kernel)

#     # 4. Identifier les bassins (les zones claires enfermées par les lignes)
#     basins = ~dark_lines
#     basin_labels, num_basins = label(basins)

#     # 5. Étendre les bassins pour "avaler" les lignes noires (Voronoi/Distance Transform)
#     # Cela garantit que les polygones finaux se touchent sans vide entre eux.
#     distances, indices = distance_transform_edt(dark_lines, return_indices=True)
#     full_labels = basin_labels[indices[0], indices[1]]

#     # 6. Calculer la couleur moyenne (RGB) de CHAQUE bassin
#     # np.bincount est ultra-rapide pour ça.
#     basin_colors = np.zeros((num_basins + 1, 3), dtype=np.float32)
#     counts = np.bincount(full_labels.ravel(), minlength=num_basins + 1)
    
#     for ch in range(3):
#         ch_sum = np.bincount(full_labels.ravel(), weights=img_rgb[:, :, ch].ravel(), minlength=num_basins + 1)
#         valid = counts > 0
#         basin_colors[valid, ch] = ch_sum[valid] / counts[valid]

#     # On ignore le label 0 (qui n'existe techniquement plus grâce à l'expansion, mais par sécurité)
#     valid_basin_colors = basin_colors[1:]

#     # 7. Regrouper les milliers de bassins en `k` unités géologiques avec notre K-Means
#     rng = np.random.default_rng(random_seed)
    
#     # K-Means++ init sur les couleurs des bassins
#     centroids = _kmeans_plus_plus_init(valid_basin_colors, k, rng)
    
#     for _ in range(30):
#         basin_clusters = _assign_labels(valid_basin_colors, centroids)
#         new_centroids = _update_centroids(valid_basin_colors, basin_clusters, k, centroids)
        
#         if np.allclose(centroids, new_centroids):
#             break
#         centroids = new_centroids

#     # Assigner chaque bassin à son cluster final
#     basin_clusters = _assign_labels(valid_basin_colors, centroids)

#     # 8. Reconstruire l'image finale
#     # On crée une table de correspondance : ID du Bassin -> ID du Cluster Géologique
#     lookup = np.zeros(num_basins + 1, dtype=np.int32)
#     lookup[1:] = basin_clusters
    
#     final_pixels_labels = lookup[full_labels]

#     return final_pixels_labels.reshape((rows, cols)), centroids.astype(np.uint8)




def compute_texture_channels(img_clean: np.ndarray, size_fine: int = 5, 
                             size_coarse: int = 25) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute TWO texture channels at different scales.
 
    Why two scales?
    ---------------
    - Fine scale  (size ~5)  : detects dense patterns (hatching ///, grid +)
                               and thin lines. High response on tightly-packed motifs.
    - Coarse scale (size ~25): detects sparse patterns (scattered dots ·)
                               whose period is 15-30 pixels. A small window
                               sees only one dot and cannot measure periodicity;
                               a large window sees many dots and gets high variance.
 
    Using both lets K-Means distinguish:
        plain red        → fine=low,  coarse=low
        red + dense ///  → fine=high, coarse=mid
        red + sparse ·   → fine=low,  coarse=high
        red + grid +     → fine=high, coarse=high
 
    Parameters
    ----------
    img_clean   : np.ndarray  shape (rows, cols, 3)  — pre-filtered RGB
    size_fine   : int  — small kernel for dense patterns  (default 5)
    size_coarse : int  — large kernel for sparse patterns (default 25)
 
    Returns
    -------
    tex_fine, tex_coarse : two np.ndarray  shape (rows, cols)  dtype uint8
    """
    # Work on grayscale — color info is in RGB channels already
    gray = img_clean.mean(axis=2).astype(np.float32)
 
    def local_variance(arr: np.ndarray, size: int) -> np.ndarray:
        # E[X²] - E[X]²  — numerically stable local variance
        mu  = uniform_filter(arr,       size=size, mode='reflect')
        mu2 = uniform_filter(arr * arr, size=size, mode='reflect')
        var = mu2 - mu * mu
        var[var < 0] = 0
        return np.sqrt(var)
 
    tex_fine   = local_variance(gray, size_fine)
    tex_coarse = local_variance(gray, size_coarse)
 
    # Normalise each to [0, 255]
    def norm255(arr: np.ndarray) -> np.ndarray:
        vmax = arr.max()
        if vmax < 1e-6:
            return np.zeros_like(arr, dtype=np.uint8)
        return (arr / vmax * 255.0).clip(0, 255).astype(np.uint8)
 
    return norm255(tex_fine), norm255(tex_coarse)



def rgb_to_hsv_array(img_rgb: np.ndarray) -> np.ndarray:
    """
    Convert an RGB image to HSV colorspace using pure NumPy.
 
    Why HSV instead of RGB for geological maps?
    --------------------------------------------
    RGB mixes luminance and chrominance together. Two formations that appear
    visually distinct (plain red vs dark red with dots) can have very similar
    RGB means because the dot pattern averages out. In HSV:
      - H (Hue)        : the pure color angle — red stays near 0°
      - S (Saturation) : color purity — dotted zones are more saturated
      - V (Value)      : brightness — dotted zones are darker
 
    K-Means in HSV space separates perceptually distinct colors that RGB
    would conflate, without needing any texture channel.
 
    Parameters
    ----------
    img_rgb : np.ndarray  shape (rows, cols, 3)  dtype uint8
 
    Returns
    -------
    img_hsv : np.ndarray  shape (rows, cols, 3)  dtype uint8
              H in [0, 179], S in [0, 255], V in [0, 255]  (OpenCV convention)
    """
    # Normalise to [0, 1]
    rgb = img_rgb.astype(np.float32) / 255.0
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
 
    vmax = np.maximum(np.maximum(r, g), b)
    vmin = np.minimum(np.minimum(r, g), b)
    diff = vmax - vmin
 
    # Value channel
    v = vmax
 
    # Saturation channel
    s = np.where(vmax > 0, diff / vmax, 0.0)
 
    # Hue channel
    h = np.zeros_like(r)
    mask_r = (vmax == r) & (diff > 0)
    mask_g = (vmax == g) & (diff > 0)
    mask_b = (vmax == b) & (diff > 0)
 
    h[mask_r] = (60.0 * ((g[mask_r] - b[mask_r]) / diff[mask_r])) % 360.0
    h[mask_g] = 60.0 * ((b[mask_g] - r[mask_g]) / diff[mask_g]) + 120.0
    h[mask_b] = 60.0 * ((r[mask_b] - g[mask_b]) / diff[mask_b]) + 240.0
 
    # Scale to uint8 — H: [0,360] → [0,179], S,V: [0,1] → [0,255]
    h_out = (h / 360.0 * 179.0).clip(0, 179).astype(np.uint8)
    s_out = (s * 255.0).clip(0, 255).astype(np.uint8)
    v_out = (v * 255.0).clip(0, 255).astype(np.uint8)
 
    return np.dstack((h_out, s_out, v_out))
 









































# core/digitizer.py

"""
core/digitizer.py

State-of-the-Art Geological Map Digitization Engine.
Paradigm: OBIA (Object-Based Image Analysis) using Superpixels and Hierarchical Clustering.
Pure NumPy / SciPy. Zero external QGIS/GDAL dependencies.
"""

import numpy as np # type: ignore
from scipy.ndimage import median_filter, uniform_filter, gaussian_filter # type: ignore
from scipy.cluster.hierarchy import linkage, fcluster # type: ignore

# ---------------------------------------------------------------------------
# 1. Feature Extraction (Base Colors & Texture)
# ---------------------------------------------------------------------------

def rgb_to_hsv_array(img_rgb: np.ndarray) -> np.ndarray:
    rgb  = img_rgb.astype(np.float32) / 255.0
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]

    vmax = np.maximum(np.maximum(r, g), b)
    vmin = np.minimum(np.minimum(r, g), b)
    diff = vmax - vmin

    v = vmax
    s = np.where(vmax > 0, diff / vmax, 0.0)

    h = np.zeros_like(r)
    mr, mg, mb = (vmax == r) & (diff > 0), (vmax == g) & (diff > 0), (vmax == b) & (diff > 0)
    
    h[mr] = (60.0 * ((g[mr] - b[mr]) / diff[mr])) % 360.0
    h[mg] =  60.0 * ((b[mg] - r[mg]) / diff[mg]) + 120.0
    h[mb] =  60.0 * ((r[mb] - g[mb]) / diff[mb]) + 240.0

    return np.dstack([
        (h / 360.0 * 179.0).clip(0, 179).astype(np.uint8),
        (s * 255.0).clip(0, 255).astype(np.uint8),
        (v * 255.0).clip(0, 255).astype(np.uint8),
    ])

def compute_base_and_texture(img_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Separates the map into a median-cleaned background and an ink texture map."""
    clean_rgb = np.zeros_like(img_rgb)
    for ch in range(3):
        clean_rgb[:, :, ch] = median_filter(img_rgb[:, :, ch], size=7)
        
    clean_hsv = rgb_to_hsv_array(clean_rgb)
    
    gray_orig = img_rgb.mean(axis=2).astype(np.float32)
    gray_clean = clean_rgb.mean(axis=2).astype(np.float32)
    
    # Isolate ink (hatches, pluses)
    ink = np.clip(gray_clean - gray_orig, 0, 255)
    
    # Smooth into a continuous energy field
    texture_map = gaussian_filter(ink, sigma=3.0)
    
    t_max = np.max(texture_map)
    if t_max > 0.1:
        texture_map = (texture_map / t_max) * 100.0
        
    return clean_rgb, clean_hsv, texture_map


# ---------------------------------------------------------------------------
# 2. SLIC Superpixels (Custom NumPy Implementation)
# ---------------------------------------------------------------------------

def generate_slic_superpixels(img_rgb: np.ndarray, n_segments: int = 3000, compactness: float = 20.0) -> np.ndarray:
    """
    Groups pixels into small, uniform polygons (superpixels).
    This replaces pixel-by-pixel analysis to eliminate noise and sparse hatch gaps.
    """
    H, W = img_rgb.shape[:2]
    step = max(2, int(np.sqrt(H * W / n_segments)))
    
    # Initialize grid centers
    y_grid = np.arange(step//2, H, step)
    x_grid = np.arange(step//2, W, step)
    cy, cx = np.meshgrid(y_grid, x_grid, indexing='ij')
    cy, cx = cy.ravel(), cx.ravel()
    K = len(cy)
    
    img_f = img_rgb.astype(np.float32)
    labels = np.zeros((H, W), dtype=np.int32)
    distances = np.full((H, W), np.inf, dtype=np.float32)
    
    yy, xx = np.mgrid[0:H, 0:W]
    colors = img_f[cy, cx].copy()
    
    # 3 iterations is usually enough for a stable over-segmentation
    for _ in range(3):
        distances.fill(np.inf)
        for k in range(K):
            y0, y1 = max(0, cy[k] - 2*step), min(H, cy[k] + 2*step)
            x0, x1 = max(0, cx[k] - 2*step), min(W, cx[k] + 2*step)
            
            if y1 <= y0 or x1 <= x0: continue
            
            win_img = img_f[y0:y1, x0:x1]
            win_y = yy[y0:y1, x0:x1]
            win_x = xx[y0:y1, x0:x1]
            
            # Color distance
            dc = np.sqrt(np.sum((win_img - colors[k])**2, axis=2))
            # Spatial distance
            ds = np.sqrt((win_y - cy[k])**2 + (win_x - cx[k])**2)
            
            # SLIC distance formula
            D = dc + (compactness / step) * ds
            
            mask = D < distances[y0:y1, x0:x1]
            distances[y0:y1, x0:x1][mask] = D[mask]
            labels[y0:y1, x0:x1][mask] = k
            
        # Update centroids
        for k in range(K):
            mask = (labels == k)
            if np.any(mask):
                cy[k] = int(yy[mask].mean())
                cx[k] = int(xx[mask].mean())
                colors[k] = img_f[cy[k], cx[k]]
                
    return labels


# ---------------------------------------------------------------------------
# 3. Superpixel Feature Aggregation
# ---------------------------------------------------------------------------

def extract_superpixel_features(
    labels: np.ndarray, clean_hsv: np.ndarray, texture_map: np.ndarray
) -> np.ndarray:
    """Calculates the average Color and Texture inside each superpixel."""
    num_superpixels = labels.max() + 1
    
    h = clean_hsv[:,:,0].ravel()
    s = clean_hsv[:,:,1].ravel()
    v = clean_hsv[:,:,2].ravel()
    t = texture_map.ravel()
    lbls = labels.ravel()
    
    counts = np.bincount(lbls, minlength=num_superpixels)
    valid = counts > 0
    
    mean_h = np.bincount(lbls, weights=h, minlength=num_superpixels)[valid] / counts[valid]
    mean_s = np.bincount(lbls, weights=s, minlength=num_superpixels)[valid] / counts[valid]
    mean_v = np.bincount(lbls, weights=v, minlength=num_superpixels)[valid] / counts[valid]
    mean_t = np.bincount(lbls, weights=t, minlength=num_superpixels)[valid] / counts[valid]
    
    return np.column_stack([mean_h, mean_s, mean_v, mean_t])


# ---------------------------------------------------------------------------
# 4. Agglomerative Clustering (The Brain)
# ---------------------------------------------------------------------------

def cluster_superpixels(features: np.ndarray, tolerance: float) -> np.ndarray:
    """
    Groups similar superpixels using Ward's Hierarchical Clustering.
    Automatically finds the optimal number of geological units based on the tolerance cut-off.
    """
    # 1. Transform features into a uniform Euclidean space
    # Hue is circular (0-179). Convert to Sin/Cos.
    H_rad = (features[:, 0] / 179.0) * 2 * np.pi
    H_sin = np.sin(H_rad) * 100.0  # Hue is heavily weighted
    H_cos = np.cos(H_rad) * 100.0
    
    S = features[:, 1] * 0.5       # Saturation/Value vary on scans, weight them less
    V = features[:, 2] * 0.5
    T = features[:, 3] * 1.5       # Texture is a strong separator
    
    X = np.column_stack([H_sin, H_cos, S, V, T])
    
    # 2. Build the connectivity tree (Agglomerative Clustering via SciPy)
    Z = linkage(X, method='ward')
    
    # 3. Cut the tree at the visual tolerance threshold
    # Higher tolerance = fewer clusters (more merging)
    cluster_ids = fcluster(Z, t=tolerance, criterion='distance')
    
    return cluster_ids


# ---------------------------------------------------------------------------
# Master Pipeline
# ---------------------------------------------------------------------------

def run_obia_digitization(img_rgb: np.ndarray, tolerance: float = 150.0) -> tuple[np.ndarray, list]:
    
    # 1. Base & Texture
    clean_rgb, clean_hsv, texture_map = compute_base_and_texture(img_rgb)
    
    # 2. Generate Superpixels (using the clean RGB to snap nicely to borders)
    superpixels = generate_slic_superpixels(clean_rgb, n_segments=3000, compactness=15.0)
    
    # 3. Extract Features per Superpixel
    features = extract_superpixel_features(superpixels, clean_hsv, texture_map)
    
    # 4. Cluster Superpixels
    cluster_ids = cluster_superpixels(features, tolerance)
    
    # 5. Reconstruct the image map
    # Create a mapping array. Note: fcluster returns IDs starting at 1.
    num_superpixels = superpixels.max() + 1
    mapping = np.zeros(num_superpixels, dtype=np.int32)
    
    # Map valid feature indices back to their original superpixel ID
    valid_mask = np.bincount(superpixels.ravel(), minlength=num_superpixels) > 0
    valid_indices = np.where(valid_mask)[0]
    
    for i, sp_id in enumerate(valid_indices):
        mapping[sp_id] = cluster_ids[i]
        
    final_label_map = mapping[superpixels]
    
    # 6. Smooth the superpixel edges for beautiful polygons
    final_label_map = median_filter(final_label_map, size=5)
    
    # 7. Generate UI Colors
    rgb_colors =[]
    unique_clusters = np.unique(cluster_ids)
    
    for cid in unique_clusters:
        # Find mean color of this cluster
        mask = (final_label_map == cid)
        c_rgb = clean_rgb[mask].mean(axis=0) if np.any(mask) else [128, 128, 128]
        rgb_colors.append((int(c_rgb[0]), int(c_rgb[1]), int(c_rgb[2])))
        
    return final_label_map, rgb_colors