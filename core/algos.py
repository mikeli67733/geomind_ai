# -*- coding: utf-8 -*-
"""
Pure algorithm cores for GeoMind AI.

These functions contain no file / GDAL / QGIS I/O and depend only on
numpy, which keeps them unit-testable in complete isolation. The I/O
orchestrators in ``tools.raster_ops`` call into these cores, so the
same math is shared by the UI tool widgets and the LLM skill dispatcher.
"""
from typing import Optional, Sequence

import numpy as np

__all__ = [
    "compute_spectral_index",
    "convolve3x3",
    "kmeans_labels",
    "compute_area_statistics",
]


# ===========================================================================
# Spectral indices
# ===========================================================================

def compute_spectral_index(
    b1: np.ndarray,
    b2: np.ndarray,
    b3: Optional[np.ndarray] = None,
    index_type: str = "ndvi",
) -> np.ndarray:
    """Compute a spectral index array from band arrays (float32 output).

    Supported indices: ndvi, gndvi, savi, evi, fvc, ndwi, mndwi,
    ndbi, ndmi, nbr (two-band) and evi / bsi (three-band).

    Zero-denominator pixels are mapped to NaN, mirroring the original
    in-place behavior of the raster pipeline.
    """
    b1 = b1.astype(np.float32)
    b2 = b2.astype(np.float32)
    idx_type = index_type.lower()

    if idx_type == "savi":
        l_factor = 0.5
        denom = b1 + b2 + l_factor
        denom[denom == 0] = np.nan
        return ((b1 - b2) / denom) * (1.0 + l_factor)

    if idx_type == "evi":
        if b3 is None:
            raise ValueError("EVI 需要第三波段 (b3)")
        b3 = b3.astype(np.float32)
        denom = b1 + 6.0 * b2 - 7.5 * b3 + 1.0
        denom[denom == 0] = np.nan
        return 2.5 * (b1 - b2) / denom

    if idx_type == "fvc":
        denom = b1 + b2
        denom[denom == 0] = np.nan
        ndvi = (b1 - b2) / denom
        return np.clip((ndvi - 0.05) / (0.70 - 0.05 + 1e-6), 0.0, 1.0)

    if idx_type == "bsi":
        if b3 is None:
            raise ValueError("BSI 需要第三波段 (b3)")
        b3 = b3.astype(np.float32)
        num = (b1 + b2) - b3
        den = (b1 + b2) + b3
        den[den == 0] = np.nan
        return num / den

    # ndvi, gndvi, ndwi, mndwi, ndbi, ndmi, nbr
    denom = b1 + b2
    denom[denom == 0] = np.nan
    return (b1 - b2) / denom


# ===========================================================================
# Spatial filters
# ===========================================================================

def convolve3x3(arr: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Apply a 3x3 convolution on the valid region (border stays zero).

    The kernel is applied with the same orientation as the original
    nested-loop implementation; inputs are promoted to float32.
    """
    arr = arr.astype(np.float32)
    out = np.zeros_like(arr)
    for r in range(1, arr.shape[0] - 1):
        for c in range(1, arr.shape[1] - 1):
            out[r, c] = np.sum(arr[r - 1 : r + 2, c - 1 : c + 2] * kernel)
    return out


# ===========================================================================
# K-Means clustering
# ===========================================================================

def kmeans_labels(
    X: np.ndarray,
    k: int = 5,
    max_iters: int = 15,
    seed: int = 42,
) -> np.ndarray:
    """Run K-Means on row-vector samples; returns 0-based label per row.

    Matches the legacy pipeline: deterministic seed, fallback keeps a
    stale center when a cluster becomes empty, convergence atol=1e-2.
    """
    np.random.seed(seed)
    indices = np.random.choice(X.shape[0], k, replace=False)
    centers = X[indices]
    labels = np.zeros(X.shape[0], dtype=int)

    for _ in range(max_iters):
        dists = np.linalg.norm(X[:, None, :] - centers[None, :, :], axis=-1)
        labels = np.argmin(dists, axis=-1)
        new_centers = np.array([
            X[labels == j].mean(axis=0) if np.any(labels == j) else centers[j]
            for j in range(k)
        ])
        if np.allclose(centers, new_centers, atol=1e-2):
            break
        centers = new_centers
    return labels


# ===========================================================================
# Area statistics
# ===========================================================================

def compute_area_statistics(arr: np.ndarray, pixel_area: float) -> list:
    """Per-class pixel count and area from a classified array.

    Returns a list of dicts:
    [{class_id, pixels, area_m2, area_mu, percent}].
    Pixels with value <= 0 (nodata) are excluded, mirroring the
    original raster pipeline.
    """
    unique, counts = np.unique(arr[arr > 0], return_counts=True)
    total_pixels = int(np.sum(counts))

    results = []
    for val, count in zip(unique, counts):
        area_m2 = float(count * pixel_area)
        results.append({
            "class_id": int(val),
            "pixels": int(count),
            "area_m2": area_m2,
            "area_mu": area_m2 / 666.6667,
            "percent": (count / total_pixels) * 100.0 if total_pixels > 0 else 0.0,
        })
    return results
