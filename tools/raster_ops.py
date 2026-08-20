# -*- coding: utf-8 -*-
"""
Shared raster processing operations.

These functions are used by both the UI tool widgets and the LLM skill
dispatcher, eliminating code duplication.
"""
import os
import tempfile
from datetime import datetime
from typing import Tuple, Optional

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.core import QgsProject, QgsRasterLayer

from ..core.algos import (
    compute_spectral_index,
    convolve3x3,
    kmeans_labels,
    compute_area_statistics,
)
from ..core.logger import get_logger

logger = get_logger("tools.raster_ops")


def _timestamp() -> str:
    return datetime.now().strftime("%H%M%S")


def _save_raster(
    out_path: str,
    array: np.ndarray,
    geo_transform,
    projection: str,
    dtype=gdal.GDT_Float32,
    nodata: Optional[float] = None,
) -> str:
    """Write a numpy array to a GeoTIFF and return the file path."""
    driver = gdal.GetDriverByName("GTiff")
    bands = 1 if array.ndim == 2 else array.shape[2]
    out_ds = driver.Create(out_path, array.shape[1], array.shape[0], bands, dtype)
    out_ds.SetGeoTransform(geo_transform)
    out_ds.SetProjection(projection)

    if bands == 1:
        if nodata is not None:
            out_ds.GetRasterBand(1).SetNoDataValue(nodata)
        out_ds.GetRasterBand(1).WriteArray(array)
    else:
        for i in range(bands):
            out_ds.GetRasterBand(i + 1).WriteArray(array[:, :, i])

    out_ds = None
    return out_path


def _add_raster_layer(out_path: str, name: str) -> QgsRasterLayer:
    """Load a raster file into the current QGIS project."""
    layer = QgsRasterLayer(out_path, name)
    if layer.isValid():
        QgsProject.instance().addMapLayer(layer)
    return layer


# ===========================================================================
# 1. Spectral index calculation
# ===========================================================================

def calc_spectral_index(
    source_path: str,
    index_type: str,
    b1_idx: int,
    b2_idx: int,
    b3_idx: int = 1,
    threshold: Optional[float] = None,
) -> str:
    """
    Calculate a spectral index from a multi-band raster.

    Supported indices: ndvi, gndvi, savi, evi, fvc, ndwi, mndwi,
    ndbi, ndmi, nbr, bsi.
    """
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    b1 = ds.GetRasterBand(b1_idx).ReadAsArray()
    b2 = ds.GetRasterBand(b2_idx).ReadAsArray()
    b3 = None
    if index_type.lower() in ("evi", "bsi"):
        b3 = ds.GetRasterBand(b3_idx).ReadAsArray()

    idx_type = index_type.lower()
    index_arr = compute_spectral_index(b1, b2, b3, idx_type)

    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()

    out_file = os.path.join(tempfile.gettempdir(), f"{idx_type}_{_timestamp()}.tif")

    if threshold is not None:
        out_arr = np.where(index_arr >= threshold, 1, 0).astype(np.uint8)
        _save_raster(out_file, out_arr, gt, proj, dtype=gdal.GDT_Byte)
    else:
        out_arr = np.nan_to_num(index_arr, nan=-9999)
        _save_raster(out_file, out_arr, gt, proj, dtype=gdal.GDT_Float32, nodata=-9999)

    ds = None
    _add_raster_layer(out_file, f"{idx_type.upper()}_结果")
    return out_file


# ===========================================================================
# 2. PCA transform
# ===========================================================================

def run_pca(source_path: str, n_comp: int = 3) -> str:
    """Perform PCA (principal component analysis) on a multi-band raster."""
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
    h, w = bands[0].shape
    X = np.stack(bands, axis=-1).reshape(-1, len(bands))

    mean = np.mean(X, axis=0)
    X_centered = X - mean
    u, s, vt = np.linalg.svd(X_centered, full_matrices=False)
    pcs = np.dot(X_centered, vt.T[:, :n_comp])

    out_file = os.path.join(tempfile.gettempdir(), f"PCA_{n_comp}B_{_timestamp()}.tif")
    out_bands = np.zeros((h, w, n_comp), dtype=np.float32)
    for i in range(n_comp):
        out_bands[:, :, i] = pcs[:, i].reshape(h, w)

    _save_raster(out_file, out_bands, ds.GetGeoTransform(), ds.GetProjection(), dtype=gdal.GDT_Float32)
    ds = None
    _add_raster_layer(out_file, f"PCA主成分_{n_comp}B")
    return out_file


# ===========================================================================
# 3. DEM terrain analysis
# ===========================================================================

def dem_analysis(source_path: str, analysis_type: str, z_factor: float = 1.0) -> str:
    """Run GDAL DEM processing (hillshade, slope, aspect, TRI)."""
    out_file = os.path.join(tempfile.gettempdir(), f"{analysis_type}_{_timestamp()}.tif")
    options = gdal.DEMProcessingOptions(zFactor=z_factor)
    ds = gdal.DEMProcessing(out_file, source_path, analysis_type, options=options)
    ds = None

    _add_raster_layer(out_file, f"{analysis_type}_地形")
    return out_file


# ===========================================================================
# 4. Spatial filter (sobel, gaussian, laplacian)
# ===========================================================================

def spatial_filter(source_path: str, filter_type: str, band_idx: int = 1) -> str:
    """Apply a spatial convolution filter to a raster band."""
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    arr = ds.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)

    if filter_type == "sobel":
        kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
        ky = np.array([[1, 2, 1], [0, 0, 0], [-1, -2, -1]], dtype=np.float32)
        gx = convolve3x3(arr, kx)
        gy = convolve3x3(arr, ky)
        out_arr = np.sqrt(gx ** 2 + gy ** 2)
    elif filter_type == "gaussian":
        k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32) / 16.0
        out_arr = convolve3x3(arr, k)
    else:  # laplacian (sharpening)
        k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        out_arr = convolve3x3(arr, k)

    out_file = os.path.join(tempfile.gettempdir(), f"filter_{filter_type}_{_timestamp()}.tif")
    _save_raster(out_file, out_arr, ds.GetGeoTransform(), ds.GetProjection(), dtype=gdal.GDT_Float32)
    ds = None
    _add_raster_layer(out_file, f"滤波_{filter_type}")
    return out_file


# ===========================================================================
# 5. Area statistics
# ===========================================================================

def area_statistics(source_path: str) -> list:
    """
    Compute per-class pixel count and area from a classified raster.

    Returns a list of dicts: [{class_id, pixels, area_m2, area_mu, percent}].
    """
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    arr = ds.GetRasterBand(1).ReadAsArray()
    gt = ds.GetGeoTransform()
    pixel_area = abs(gt[1] * gt[5])
    ds = None

    return compute_area_statistics(arr, pixel_area)


# ===========================================================================
# 6. K-Means clustering
# ===========================================================================

def kmeans_cluster(source_path: str, k: int = 5, max_iters: int = 15) -> str:
    """Run K-Means unsupervised clustering on a multi-band raster."""
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
    h, w = bands[0].shape
    X = np.stack(bands, axis=-1).reshape(-1, len(bands))

    labels = kmeans_labels(X, k=k, max_iters=max_iters)
    cluster_map = labels.reshape(h, w).astype(np.uint8) + 1
    out_file = os.path.join(tempfile.gettempdir(), f"KMeans_K{k}_{_timestamp()}.tif")
    _save_raster(out_file, cluster_map, ds.GetGeoTransform(), ds.GetProjection(), dtype=gdal.GDT_Byte)
    ds = None
    _add_raster_layer(out_file, f"KMeans聚类_K{k}")
    return out_file


# ===========================================================================
# 7. Raster diff change detection
# ===========================================================================

def raster_diff(
    source_t1: str,
    source_t2: str,
    band_idx: int = 1,
    threshold: float = 30.0,
    polygonize: bool = True,
) -> Tuple[str, Optional[str]]:
    """
    Pixel-level difference detection between two raster images.

    Returns (raster_path, optional_vector_path).
    """
    ds1 = gdal.Open(source_t1)
    ds2 = gdal.Open(source_t2)
    if ds1 is None or ds2 is None:
        raise RuntimeError("Cannot open one or both raster files")

    arr1 = ds1.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)
    arr2 = ds2.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)

    if arr1.shape != arr2.shape:
        raise RuntimeError("两期分辨率或行列数不匹配")

    diff = np.abs(arr2 - arr1)
    change_mask = np.where(diff >= threshold, 1, 0).astype(np.uint8)

    time_str = _timestamp()
    out_tif = os.path.join(tempfile.gettempdir(), f"diff_{time_str}.tif")
    _save_raster(out_tif, change_mask, ds1.GetGeoTransform(), ds1.GetProjection(), dtype=gdal.GDT_Byte)

    out_shp = None
    if polygonize:
        out_shp = _polygonize_band(
            out_tif, ds1.GetProjection(), f"diff_poly_{time_str}.shp", "change"
        )

    ds1 = None
    ds2 = None
    _add_raster_layer(out_tif, f"像元变化掩膜")
    return out_tif, out_shp


# ===========================================================================
# 8. Image enhancement (false color composite)
# ===========================================================================

def image_enhance(source_path: str, r: int = 4, g: int = 3, b: int = 2, stretch: bool = True) -> str:
    """Create a false-color composite with optional contrast stretching."""
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    bands_data = []
    for b_idx in [r, g, b]:
        arr = ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
        if stretch:
            p2, p98 = np.percentile(arr[arr > 0], (2, 98))
            arr = np.clip((arr - p2) / (p98 - p2 + 1e-5) * 255.0, 0, 255)
        bands_data.append(arr.astype(np.uint8))

    h, w = bands_data[0].shape
    out_arr = np.stack(bands_data, axis=-1)
    out_file = os.path.join(tempfile.gettempdir(), f"composite_{_timestamp()}.tif")
    _save_raster(out_file, out_arr, ds.GetGeoTransform(), ds.GetProjection(), dtype=gdal.GDT_Byte)
    ds = None
    _add_raster_layer(out_file, "增强假彩色")
    return out_file


# ===========================================================================
# 9. Raster to vector polygonize
# ===========================================================================

def raster_polygonize(source_path: str, sieve_size: int = 4) -> str:
    """Convert a raster mask to vector polygons with optional sieve filtering."""
    ds = gdal.Open(source_path)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {source_path}")

    src_band = ds.GetRasterBand(1)
    if sieve_size > 0:
        gdal.SieveFilter(src_band, None, src_band, sieve_size, 4)

    out_shp = _polygonize_band(
        source_path, ds.GetProjection(), f"poly_vector_{_timestamp()}.shp", "polygonized"
    )
    ds = None

    from qgis.core import QgsVectorLayer
    vlayer = QgsVectorLayer(out_shp, "矢量多边形图斑", "ogr")
    if vlayer.isValid():
        QgsProject.instance().addMapLayer(vlayer)
    return out_shp


# ===========================================================================
# Internal helper: polygonize a raster band to Shapefile
# ===========================================================================

def _polygonize_band(raster_path: str, projection_wkt: str, shp_name: str, layer_name: str) -> str:
    """Polygonize a single-band raster to an ESRI Shapefile."""
    ds = gdal.Open(raster_path)
    src_band = ds.GetRasterBand(1)

    out_shp = os.path.join(tempfile.gettempdir(), shp_name)
    driver = ogr.GetDriverByName("ESRI Shapefile")
    srs = osr.SpatialReference()
    srs.ImportFromWkt(projection_wkt)
    shp_ds = driver.CreateDataSource(out_shp)
    shp_layer = shp_ds.CreateLayer(layer_name, srs, ogr.wkbPolygon)
    shp_layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))

    gdal.Polygonize(src_band, src_band, shp_layer, 0, [], callback=None)
    shp_ds = None
    ds = None
    return out_shp
