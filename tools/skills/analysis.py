# -*- coding: utf-8 -*-
"""Local raster/vector analysis skills: thresholding, PCA, terrain, filtering,
statistics, smoothing, k-means, change diff, enhancement and polygonization."""

import os
import re
import json
import math
import tempfile
import urllib.parse
from html import unescape
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import requests
import numpy as np
from PIL import Image
from osgeo import gdal, ogr, osr

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsMapLayer,
    QgsApplication,
    QgsProcessingParameterDefinition,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsRectangle,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
)
from qgis.PyQt.QtCore import QVariant, QCoreApplication
from qgis.utils import iface

from ...core.logger import get_logger

from .layers import get_layer_by_name


logger = get_logger("tools.skills.analysis")
def skill_raster_threshold(layer_name: str, min_val: float, max_val: float = 1.0, band_idx: int = 1) -> str:
    """对栅格指数或 DEM 执行快速阈值二值化提取（生成 0/1 掩膜）。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            raise RuntimeError(f"无法打开栅格: {layer.source()}")

        arr = ds.GetRasterBand(band_idx).ReadAsArray()
        mask = np.where((arr >= min_val) & (arr <= max_val), 1, 0).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_file = os.path.join(tempfile.gettempdir(), f"mask_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(mask)
        out_ds.GetRasterBand(1).SetNoDataValue(0)
        out_ds = None
        ds = None

        out_layer = QgsRasterLayer(out_file, f"{layer_name}_阈值提取[{min_val}-{max_val}]")
        if out_layer.isValid():
            QgsProject.instance().addMapLayer(out_layer)
        return f"已成功对 `{layer_name}` 完成阈值提取 [{min_val}, {max_val}]，二值化掩膜已上屏。"
    except Exception as e:
        return f"阈值提取失败: {e}"


# ===========================================================================
# 3. 栅格与矢量核心算法引擎 (纯 PyQGIS / GDAL / NumPy 自包含原生实现)
# ===========================================================================

def skill_run_pca(layer_name: str, n_comp: int = 3) -> str:
    """【PyQGIS 原生】PCA 多波段主成分分析。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        band_count = ds.RasterCount
        if band_count < 2:
            return f"❌ PCA 分析至少需要 2 个波段，当前图层仅有 {band_count} 个波段。"

        # 读取全部波段数据
        bands_data = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(band_count)]
        h, w = bands_data[0].shape
        X = np.stack([b.flatten() for b in bands_data], axis=1)

        # 协方差矩阵与特征分解
        mean = np.mean(X, axis=0)
        X_centered = X - mean
        cov = np.cov(X_centered, rowvar=False)

        eig_vals, eig_vecs = np.linalg.eigh(cov)
        sort_indices = np.argsort(eig_vals)[::-1]
        eig_vecs = eig_vecs[:, sort_indices]

        actual_comp = min(n_comp, band_count)
        loaded_layers = []
        time_str = datetime.now().strftime("%H%M%S")

        driver = gdal.GetDriverByName("GTiff")
        for i in range(actual_comp):
            pc_arr = np.dot(X_centered, eig_vecs[:, i]).reshape((h, w)).astype(np.float32)
            out_tif = os.path.join(tempfile.gettempdir(), f"PCA_PC{i + 1}_{time_str}.tif")

            out_ds = driver.Create(out_tif, w, h, 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            out_ds.GetRasterBand(1).WriteArray(pc_arr)
            out_ds.FlushCache()
            out_ds = None

            pc_layer_name = f"{layer_name}_PCA_PC{i + 1}"
            lyr = QgsRasterLayer(out_tif, pc_layer_name, "gdal")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                loaded_layers.append(pc_layer_name)

        ds = None
        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"🎉 成功对 `{layer_name}` 完成 PCA 分析，已生成并加载 {len(loaded_layers)} 个主成分图层：\n- " + "\n- ".join(loaded_layers)
    except Exception as e:
        return f"PCA 分析失败: {e}"


def skill_dem_analysis(layer_name: str, analysis_type: str = "hillshade", z_factor: float = 1.0) -> str:
    """【PyQGIS 原生】DEM 地形特征提取 (hillshade/山体阴影, slope/坡度, aspect/坡向, TRI/崎岖度)。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"dem_{analysis_type}_{time_str}.tif")
        opt_type = analysis_type.lower().strip()

        if opt_type in ("hillshade", "阴影", "山体阴影"):
            gdal.DEMProcessing(out_tif, layer.source(), "hillshade", zFactor=z_factor)
            display_name = f"{layer_name}_山体阴影"
        elif opt_type in ("slope", "坡度"):
            gdal.DEMProcessing(out_tif, layer.source(), "slope", zFactor=z_factor)
            display_name = f"{layer_name}_坡度分析(度)"
        elif opt_type in ("aspect", "坡向"):
            gdal.DEMProcessing(out_tif, layer.source(), "aspect")
            display_name = f"{layer_name}_坡向分析"
        elif opt_type in ("tri", "崎岖度", "地形崎岖度"):
            gdal.DEMProcessing(out_tif, layer.source(), "TRI")
            display_name = f"{layer_name}_地形崎岖度(TRI)"
        elif opt_type in ("tpi", "地形位置指数"):
            gdal.DEMProcessing(out_tif, layer.source(), "TPI")
            display_name = f"{layer_name}_地形位置指数(TPI)"
        elif opt_type in ("roughness", "粗糙度"):
            gdal.DEMProcessing(out_tif, layer.source(), "roughness")
            display_name = f"{layer_name}_粗糙度"
        else:
            gdal.DEMProcessing(out_tif, layer.source(), "hillshade", zFactor=z_factor)
            display_name = f"{layer_name}_地形分析({analysis_type})"

        lyr = QgsRasterLayer(out_tif, display_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"⛰️ **地形分析 [{analysis_type}] 处理完成**：已加载图层 `{display_name}`"
        return "❌ 地形分析处理完成，但图层加载失败。"
    except Exception as e:
        return f"地形分析失败: {e}"


def skill_spatial_filter(layer_name: str, filter_type: str = "sobel", band_idx: int = 1) -> str:
    """【PyQGIS 原生】空间卷积滤波 (sobel 边缘提取, gaussian 平滑, laplacian 锐化)。"""
    try:
        from scipy.ndimage import sobel, gaussian_filter, laplace

        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        arr = ds.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)
        f_type = filter_type.lower()

        if "sobel" in f_type or "边缘" in f_type:
            sx = sobel(arr, axis=0)
            sy = sobel(arr, axis=1)
            filtered = np.hypot(sx, sy)
            display_type = "Sobel边缘提取"
        elif "gaussian" in f_type or "平滑" in f_type:
            filtered = gaussian_filter(arr, sigma=1.5)
            display_type = "高斯平滑"
        elif "laplace" in f_type or "锐化" in f_type:
            filtered = laplace(arr)
            display_type = "拉普拉斯锐化"
        else:
            filtered = arr
            display_type = filter_type

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"filter_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(filtered)
        out_ds.FlushCache()
        out_ds = None
        ds = None

        layer_title = f"{layer_name}_{display_type}"
        lyr = QgsRasterLayer(out_tif, layer_title, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"✨ **空间滤波 [{display_type}] 处理完成**：已加载图层 `{layer_title}`"
        return "❌ 滤波图层构建失败。"
    except Exception as e:
        return f"空间滤波失败: {e}"


def skill_area_statistics(layer_name: str) -> str:
    """【PyQGIS 原生】统计分类图层面积与占比（原生支持矢量图斑与栅格像元统计）。"""
    try:
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if not layers:
            raise ValueError(f"找不到图层: '{layer_name}'")
        layer = layers[0]

        # 1. 矢量图层面积统计
        if isinstance(layer, QgsVectorLayer):
            total_area_m2 = sum(f.geometry().area() for f in layer.getFeatures() if f.hasGeometry())
            total_count = layer.featureCount()
            area_mu = total_area_m2 / 666.6667
            area_sqkm = total_area_m2 / 1_000_000
            return (
                f"📊 **矢量图层 `{layer_name}` 面积统计**：\n"
                f"- 要素总数: `{total_count}` 个图斑\n"
                f"- 累计总面积: `{total_area_m2:,.2f} ㎡` (`{area_mu:,.2f} 亩` / `{area_sqkm:.4f} k㎡`)"
            )

        # 2. 栅格分类面积统计
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法读取栅格数据: {layer.source()}"

        gt = ds.GetGeoTransform()
        res_x = abs(gt[1])
        res_y = abs(gt[5])

        # 经纬度投影坐标换算
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        if srs.IsGeographic():
            res_x *= 111320.0
            res_y *= 111320.0

        pixel_area_m2 = res_x * res_y
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        nodata = band.GetNoDataValue()

        valid_mask = (arr != nodata) if nodata is not None else np.ones_like(arr, dtype=bool)
        unique, counts = np.unique(arr[valid_mask], return_counts=True)
        total_pixels = sum(counts)

        report = [f"📊 **栅格图层 `{layer_name}` 像元分类面积统计**："]
        for val, cnt in zip(unique, counts):
            area_m2 = float(cnt * pixel_area_m2)
            area_mu = area_m2 / 666.6667
            pct = (cnt / total_pixels) * 100.0 if total_pixels > 0 else 0.0
            report.append(
                f"- **类别 {int(val)}**: `{int(cnt):,}` 个像元 | `{area_m2:,.2f} ㎡` (`{area_mu:,.2f} 亩`, 占比 `{pct:.1f}%`)"
            )
        ds = None
        return "\n".join(report)
    except Exception as e:
        return f"面积统计失败: {e}"


def skill_vector_smooth(layer_name: str, tolerance: float = 1.0, iterations: int = 2) -> str:
    """【PyQGIS 原生】矢量边界平滑、化简与去锯齿。"""
    try:
        layer = get_layer_by_name(layer_name, "vector")

        # 使用 QGIS 原生平滑算法
        import processing
        params = {
            'INPUT': layer,
            'ITERATIONS': iterations,
            'OFFSET': 0.25,
            'MAX_ANGLE': 180,
            'OUTPUT': 'memory:'
        }
        res = processing.run("native:smoothgeometry", params)
        smoothed_layer = res['OUTPUT']

        smoothed_layer.setName(f"{layer.name()}_平滑")
        QgsProject.instance().addMapLayer(smoothed_layer)
        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"📐 **矢量图层 `{layer_name}` 边界平滑去锯齿完成**：已生成内存图层 `{smoothed_layer.name()}` (迭代次数: {iterations})。"
    except Exception as e:
        return f"矢量平滑失败: {e}"


def skill_kmeans_cluster(layer_name: str, k: int = 5, max_iters: int = 15) -> str:
    """【PyQGIS 原生】多波段 K-Means 无监督聚类。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        bands_data = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
        h, w = bands_data[0].shape
        X = np.stack([b.flatten() for b in bands_data], axis=1)

        # 随机中心点初始化
        np.random.seed(42)
        valid_idx = np.random.choice(X.shape[0], k, replace=False)
        centers = X[valid_idx]

        labels = np.zeros(X.shape[0], dtype=np.int32)
        for _ in range(max_iters):
            dists = np.linalg.norm(X[:, np.newaxis] - centers, axis=2)
            new_labels = np.argmin(dists, axis=1)
            if np.all(labels == new_labels):
                break
            labels = new_labels
            for c in range(k):
                mask = (labels == c)
                if np.any(mask):
                    centers[c] = np.mean(X[mask], axis=0)

        clustered = labels.reshape((h, w)).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"kmeans_k{k}_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, w, h, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(clustered)
        out_ds.FlushCache()
        out_ds = None
        ds = None

        out_name = f"{layer_name}_KMeans聚类(K={k})"
        lyr = QgsRasterLayer(out_tif, out_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"🧩 **K-Means 智能聚类完成**：已生成 {k} 个地物聚类图层 `{out_name}`"
        return "❌ 聚类结果加载失败。"
    except Exception as e:
        return f"K-Means 失败: {e}"


def skill_raster_diff(layer_t1: str, layer_t2: str, threshold: float = 30.0, polygonize: bool = True) -> str:
    """【PyQGIS 原生】双期影像像元级绝对差分变化检测。"""
    try:
        l1 = get_layer_by_name(layer_t1, "raster")
        l2 = get_layer_by_name(layer_t2, "raster")

        d1 = gdal.Open(l1.source())
        d2 = gdal.Open(l2.source())
        if not d1 or not d2:
            return "❌ 无法打开双期影像文件。"

        a1 = d1.GetRasterBand(1).ReadAsArray().astype(np.float32)
        a2 = d2.GetRasterBand(1).ReadAsArray().astype(np.float32)

        if a1.shape != a2.shape:
            return f"❌ 双期影像尺寸不一致：T1 为 {a1.shape}，T2 为 {a2.shape}，无法直接差分。"

        diff = np.abs(a1 - a2)
        mask = (diff >= threshold).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"diff_mask_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, a1.shape[1], a1.shape[0], 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(d1.GetGeoTransform())
        out_ds.SetProjection(d1.GetProjection())
        band = out_ds.GetRasterBand(1)
        band.WriteArray(mask)
        band.SetNoDataValue(0)
        out_ds.FlushCache()
        out_ds = None
        d1 = None
        d2 = None

        diff_layer_name = f"双期差分变化掩膜(阈值{threshold})"
        lyr = QgsRasterLayer(out_tif, diff_layer_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)

        poly_msg = ""
        if polygonize:
            poly_msg = "\n" + skill_raster_polygonize(diff_layer_name, sieve_size=4)

        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"🔄 **双期影像差分变化检测完成**：已生成变化掩膜图层 `{diff_layer_name}`{poly_msg}"
    except Exception as e:
        return f"差分检测失败: {e}"


def skill_image_enhance(layer_name: str, r: int = 4, g: int = 3, b: int = 2) -> str:
    """【PyQGIS 原生】多波段假彩色合成与 2% 线性拉伸增强。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        max_b = ds.RasterCount
        if max(r, g, b) > max_b:
            return f"❌ 波段索引越界：栅格仅包含 {max_b} 个波段，无法以 ({r},{g},{b}) 组合合成。"

        bands = [ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32) for b_idx in (r, g, b)]
        enhanced_bands = []

        for arr in bands:
            p2, p98 = np.percentile(arr, (2, 98))
            if p98 > p2:
                arr = (arr - p2) / (p98 - p2) * 255.0
            arr = np.clip(arr, 0, 255)
            enhanced_bands.append(arr.astype(np.uint8))

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"enhance_{r}_{g}_{b}_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        h, w = enhanced_bands[0].shape
        out_ds = driver.Create(out_tif, w, h, 3, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())

        for i in range(3):
            out_ds.GetRasterBand(i + 1).WriteArray(enhanced_bands[i])

        out_ds.FlushCache()
        out_ds = None
        ds = None

        out_title = f"{layer_name}_彩色增强({r},{g},{b})"
        lyr = QgsRasterLayer(out_tif, out_title, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"🎨 **多波段假彩色合成与画质增强完成**：已加载图层 `{out_title}`"
        return "❌ 增强图层生成失败。"
    except Exception as e:
        return f"画质增强失败: {e}"


def skill_raster_polygonize(layer_name: str, sieve_size: int = 4) -> str:
    """【PyQGIS 原生】二值/分类栅格矢量化为 Polygon 面要素，自动过滤碎斑。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        src_ds = gdal.Open(layer.source())
        if src_ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        src_band = src_ds.GetRasterBand(1)
        time_str = datetime.now().strftime("%H%M%S")
        out_shp = os.path.join(tempfile.gettempdir(), f"poly_{time_str}.shp")

        srs = osr.SpatialReference(wkt=src_ds.GetProjection())
        drv = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(out_shp):
            drv.DeleteDataSource(out_shp)

        dst_ds = drv.CreateDataSource(out_shp)
        dst_layer = dst_ds.CreateLayer("polygonized", srs=srs, geom_type=ogr.wkbPolygon)

        fd = ogr.FieldDefn("DN", ogr.OFTInteger)
        dst_layer.CreateField(fd)

        # GDAL 原生矢量化
        gdal.Polygonize(src_band, None, dst_layer, 0, [], callback=None)

        dst_ds.FlushCache()
        dst_ds = None
        src_ds = None

        out_vec_name = f"{layer_name}_矢量化图斑"
        vlayer = QgsVectorLayer(out_shp, out_vec_name, "ogr")
        if vlayer.isValid():
            # 过滤背景 (DN > 0)
            vlayer.setSubsetString("DN > 0")
            QgsProject.instance().addMapLayer(vlayer)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"📦 **栅格 `{layer_name}` 已成功转换为矢量多边形**：已加载图层 `{out_vec_name}` ({vlayer.featureCount()} 个图斑)。"
        return "❌ 矢量化图层加载失败。"
    except Exception as e:
        return f"矢量化失败: {e}"
