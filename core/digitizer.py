# core/digitizer.py

"""
core/digitizer.py

Pure mathematical and image processing algorithms for Geological Map Digitization.
Uses only NumPy and SciPy. Zero QGIS/GDAL dependencies here.
"""

import numpy as np # type: ignore
from scipy.ndimage import median_filter # type: ignore

def kmeans_image_segmentation(
    img_rgb: np.ndarray, 
    k: int, 
    max_iter: int = 50, 
    tol: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """
    Fast K-Means clustering for RGB image segmentation using pure NumPy.
    Replaces the need for heavy libraries like scikit-learn.
    
    Parameters
    ----------
    img_rgb  : np.ndarray  shape (rows, cols, 3) - The RGB image.
    k        : int         - Number of color clusters.
    max_iter : int         - Maximum number of iterations.
    tol      : float       - Tolerance for centroid movement to declare convergence.
    
    Returns
    -------
    labels    : np.ndarray  shape (rows, cols) - The cluster ID (0 to K-1) for each pixel.
    centroids : np.ndarray  shape (K, 3)       - The RGB values of the K centroids.
    """
    rows, cols, channels = img_rgb.shape
    
    # Flatten the image to a 2D array of pixels (N, 3)
    pixels = img_rgb.reshape(-1, channels).astype(np.float32)
    
    # 1. Initialization (Random selection of starting pixels)
    np.random.seed(42) # For reproducibility
    random_indices = np.random.choice(pixels.shape[0], k, replace=False)
    centroids = pixels[random_indices]
    
    labels = np.zeros(pixels.shape[0], dtype=np.int32)
    
    for i in range(max_iter):
        # 2. Assign pixels to the nearest centroid using Broadcasting
        distances = np.linalg.norm(pixels[:, None, :] - centroids[None, :, :], axis=2)
        new_labels = np.argmin(distances, axis=1)
        
        # 3. Update centroids (calculate the mean RGB of assigned pixels)
        new_centroids = np.zeros_like(centroids)
        for c in range(k):
            cluster_pixels = pixels[new_labels == c]
            if len(cluster_pixels) > 0:
                new_centroids[c] = cluster_pixels.mean(axis=0)
            else:
                # Reassign empty cluster to a random pixel to avoid dead clusters
                new_centroids[c] = pixels[np.random.randint(0, pixels.shape[0])]
                
        # 4. Check convergence
        shift = np.linalg.norm(new_centroids - centroids, axis=1).max()
        centroids = new_centroids
        labels = new_labels
        
        if shift < tol:
            break
            
    return labels.reshape(rows, cols), centroids.astype(np.uint8)


def clean_segmentation_noise(labels: np.ndarray, smooth_size: int = 5) -> np.ndarray:
    """
    Applies a median filter to the labeled image to remove 'salt and pepper' noise,
    scanned map artifacts, and thin boundary lines.
    
    Parameters
    ----------
    labels      : np.ndarray shape (rows, cols) - The raw k-means output.
    smooth_size : int - Kernel size (must be odd, e.g., 3, 5, 7).
    
    Returns
    -------
    cleaned_labels : np.ndarray
    """
    if smooth_size < 3:
        return labels
        
    # Ensure odd kernel size
    if smooth_size % 2 == 0:
        smooth_size += 1
        
    # Median filter perfectly respects categorical boundaries (no new weird labels created)
    return median_filter(labels, size=smooth_size)