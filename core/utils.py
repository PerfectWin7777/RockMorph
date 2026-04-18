# core/utils.py

"""
rockmorph/core/utils.py
General purpose geomorphometry utilities.
"""

import numpy as np  # type: ignore


# Pure NumPy smoothing function
def smooth_data(y, window_size):
    """
    Smooth the data using a Hanning window convolution.
    y: 1D array (elevations)
    window_size: int (must be > 2)

    Robust NumPy smoothing using a Hanning window.
    """
    # 1. Safety check: need at least some data
    n = len(y)
    if n < 3 or window_size < 3:
        return y
    
    # 2. Adaptive window size: 
    # The window must be odd and at most 1/3 of the data length 
    # to avoid destroying the profile shape or crashing.
    if window_size >= n:
        window_size = n // 2
    
    # Force window to be odd
    if window_size % 2 == 0:
        window_size -= 1
        
    if window_size < 3:
        return y

    # 3. Hanning Window
    window = np.hanning(window_size)
    window /= window.sum()
    
    # 4. Padding with 'edge' to preserve start/end elevations
    # This prevents the profile from dropping to zero at the mouth
    pad_size = window_size // 2
    y_padded = np.pad(y, (pad_size, pad_size), mode='edge')
    
    # 5. Convolution
    y_smooth = np.convolve(y_padded, window, mode='valid')
    
    # 6. Final length check (should match input y)
    if len(y_smooth) != n:
        # If mismatch, we interpolate back to original length
        return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(y_smooth)), y_smooth)
        
    return y_smooth





def reorient_profile_high_to_low(distances: list, profiles: dict) -> tuple:
    """
    Ensures that a profile sequence starts at the highest elevation.
    Used for longitudinal river profiles or scientific swath analysis.
    
    Args:
        distances: list of floats (x-axis)
        profiles: dict where values are lists/arrays of elevations (y-axis)
    
    Returns:
        tuple: (new_distances, new_profiles)
    """
    # Use 'mean' if available (for Swath), otherwise try 'elevations' (for NCP)
    ref_key = 'mean' if 'mean' in profiles else 'elevations'
    
    # Check if the start elevation is lower than the end elevation
    # We use valid (non-None) values for the check
    y_vals = [v for v in profiles[ref_key] if v is not None]
    
    if len(y_vals) > 1 and y_vals[0] < y_vals[-1]:
        # The profile is oriented Down-to-Up, we need to flip it
        total_dist = distances[-1]
        
        # 1. Reverse and recalculate distances from 0
        new_distances = [round(total_dist - d, 2) for d in reversed(distances)]
        
        # 2. Reverse all profile arrays
        new_profiles = {}
        for key, values in profiles.items():
            if values is not None and isinstance(values, list):
                new_profiles[key] = list(reversed(values))
            else:
                new_profiles[key] = values
        
        return new_distances, new_profiles
    
    return distances, profiles