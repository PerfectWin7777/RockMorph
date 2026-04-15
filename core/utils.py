# core/utils.py


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