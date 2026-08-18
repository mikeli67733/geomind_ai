# -*- coding: utf-8 -*-
"""
llm_skills.py
涵盖所有本地免费遥感工具、AI 地物提取、SAM3 目标检测、双期变化检测。
"""
import os
import tempfile
import numpy as np
from datetime import datetime
from osgeo import gdal, ogr, osr

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsApplication
)


# =========================================================================
# 辅助函数
# =========================================================================
def get_layer_by_name(layer_name: str, layer_type="raster"):
    layers = QgsProject.instance().mapLayersByName(layer_name)
    if not layers:
        raise ValueError(f"找不到图层: '{layer_name}'")
    layer = layers[0]
    if layer_type == "raster" and not isinstance(layer, QgsRasterLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是栅格图层")
    if layer_type == "vector" and not isinstance(layer, QgsVectorLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是矢量图层")
    return layer


def get_active_layers() -> str:
    layers = QgsProject.instance().mapLayers().values()
    if not layers:
        return "当前 QGIS 工程中无图层。"
    info = []
    for l in layers:
        l_type = "栅格" if isinstance(l, QgsRasterLayer) else "矢量"
        info.append(f"{l.name()} ({l_type})")
    return f"当前活动图层有: {', '.join(info)}"


# =========================================================================
# 一、10 大免费本地遥感全能工具箱
# =========================================================================

def skill_calc_spectral_index(layer_name: str, index_type: str, b1_idx: int, b2_idx: int, b3_idx: int = 1) -> str:
    """1. 光谱指数计算 (NDVI, GNDVI, EVI, NDWI 等)"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        b1 = ds.GetRasterBand(b1_idx).ReadAsArray().astype(np.float32)
        b2 = ds.GetRasterBand(b2_idx).ReadAsArray().astype(np.float32)
        idx_type = index_type.lower()

        if idx_type == "savi":
            L = 0.5
            denom = b1 + b2 + L
            denom[denom == 0] = np.nan
            index_arr = ((b1 - b2) / denom) * (1.0 + L)
        elif idx_type == "evi":
            b3 = ds.GetRasterBand(b3_idx).ReadAsArray().astype(np.float32)
            denom = b1 + 6.0 * b2 - 7.5 * b3 + 1.0
            denom[denom == 0] = np.nan
            index_arr = 2.5 * (b1 - b2) / denom
        elif idx_type == "fvc":
            denom = b1 + b2
            denom[denom == 0] = np.nan
            ndvi = (b1 - b2) / denom
            index_arr = np.clip((ndvi - 0.05) / (0.70 - 0.05 + 1e-6), 0.0, 1.0)
        elif idx_type == "bsi":
            b3 = ds.GetRasterBand(b3_idx).ReadAsArray().astype(np.float32)
            num = (b1 + b2) - (b3 + 0)
            den = (b1 + b2) + (b3 + 0)
            den[den == 0] = np.nan
            index_arr = num / den
        else:  # ndvi, gndvi, ndwi, mndwi, ndbi, ndmi, nbr
            denom = b1 + b2
            denom[denom == 0] = np.nan
            index_arr = (b1 - b2) / denom

        out_file = os.path.join(tempfile.gettempdir(), f"{idx_type}_{datetime.now().strftime('%H%M%S')}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).SetNoDataValue(-9999)
        out_ds.GetRasterBand(1).WriteArray(np.nan_to_num(index_arr, nan=-9999))
        out_ds = None

        res_layer = QgsRasterLayer(out_file, f"{idx_type.upper()}_LLM结果")
        QgsProject.instance().addMapLayer(res_layer)
        return f"光谱指数 {idx_type} 计算成功并已加载。"
    except Exception as e:
        return f"计算失败: {e}"


def skill_run_pca(layer_name: str, n_comp: int = 3) -> str:
    """2. PCA 主成分分析"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
        h, w = bands[0].shape
        X = np.stack(bands, axis=-1).reshape(-1, len(bands))
        mean = np.mean(X, axis=0)
        u, s, vt = np.linalg.svd(X - mean, full_matrices=False)
        pcs = np.dot(X - mean, vt.T[:, :n_comp])

        out_file = os.path.join(tempfile.gettempdir(), f"PCA_{n_comp}B_{datetime.now().strftime('%H%M%S')}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, w, h, n_comp, gdal.GDT_Float32)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        for i in range(n_comp):
            out_ds.GetRasterBand(i + 1).WriteArray(pcs[:, i].reshape(h, w))
        out_ds = None

        QgsProject.instance().addMapLayer(QgsRasterLayer(out_file, f"PCA主成分_{n_comp}B_LLM"))
        return f"成功执行 PCA，提取了 {n_comp} 个主成分图层。"
    except Exception as e:
        return f"PCA 分析失败: {e}"


def skill_dem_analysis(layer_name: str, analysis_type: str, z_factor: float = 1.0) -> str:
    """3. DEM 地形全要素分析"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        out_file = os.path.join(tempfile.gettempdir(), f"{analysis_type}_{datetime.now().strftime('%H%M%S')}.tif")
        options = gdal.DEMProcessingOptions(zFactor=z_factor)
        gdal.DEMProcessing(out_file, layer.source(), analysis_type, options=options)

        QgsProject.instance().addMapLayer(QgsRasterLayer(out_file, f"{analysis_type}_LLM地形"))
        return f"地形分析 [{analysis_type}] 完成并已加载。"
    except Exception as e:
        return f"地形分析失败: {e}"


def skill_spatial_filter(layer_name: str, filter_type: str, band_idx: int = 1) -> str:
    """4. 空间滤波与边缘提取"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        arr = ds.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)

        if filter_type == "sobel":
            kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
            ky = np.array([[1, 2, 1], [0, 0, 0], [-1, -2, -1]], dtype=np.float32)
            # 为保持简单，Python 层面的纯卷积计算(近似处理边界)
            gx = np.zeros_like(arr);
            gy = np.zeros_like(arr)
            for r in range(1, arr.shape[0] - 1):
                for c in range(1, arr.shape[1] - 1):
                    sub = arr[r - 1:r + 2, c - 1:c + 2]
                    gx[r, c] = np.sum(sub * kx);
                    gy[r, c] = np.sum(sub * ky)
            out_arr = np.sqrt(gx ** 2 + gy ** 2)
        elif filter_type == "gaussian":
            k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32) / 16.0
            out_arr = np.zeros_like(arr)
            for r in range(1, arr.shape[0] - 1):
                for c in range(1, arr.shape[1] - 1):
                    out_arr[r, c] = np.sum(arr[r - 1:r + 2, c - 1:c + 2] * k)
        else:  # laplacian
            k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
            out_arr = np.zeros_like(arr)
            for r in range(1, arr.shape[0] - 1):
                for c in range(1, arr.shape[1] - 1):
                    out_arr[r, c] = np.sum(arr[r - 1:r + 2, c - 1:c + 2] * k)

        out_file = os.path.join(tempfile.gettempdir(), f"Filter_{filter_type}_{datetime.now().strftime('%H%M%S')}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(out_arr)
        out_ds = None

        QgsProject.instance().addMapLayer(QgsRasterLayer(out_file, f"滤波_{filter_type}_LLM"))
        return f"空间滤波 [{filter_type}] 完成！"
    except Exception as e:
        return f"空间滤波失败: {e}"


def skill_area_statistics(layer_name: str) -> str:
    """5. 分类面积统计 (返回报表文本)"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        arr = ds.GetRasterBand(1).ReadAsArray()
        gt = ds.GetGeoTransform()
        pixel_area = abs(gt[1] * gt[5])

        unique, counts = np.unique(arr[arr > 0], return_counts=True)
        report = [f"图层 {layer_name} 面积统计结果："]
        for val, count in zip(unique, counts):
            area_m2 = count * pixel_area
            report.append(f" - 类别 {val}: {count} 个像元，约 {area_m2:,.2f} 平方米 ({area_m2 / 666.6667:,.2f} 亩)")
        return "\n".join(report)
    except Exception as e:
        return f"面积统计失败: {e}"


def skill_vector_smooth(layer_name: str, tolerance: float = 1.0, iterations: int = 2) -> str:
    """6. 矢量化简与平滑"""
    try:
        layer = get_layer_by_name(layer_name, "vector")
        from qgis import processing
        res_simp = processing.run("native:simplifygeometries",
                                  {'INPUT': layer, 'METHOD': 0, 'TOLERANCE': tolerance, 'OUTPUT': 'memory:'})
        res_smooth = processing.run("native:smoothgeometry",
                                    {'INPUT': res_simp['OUTPUT'], 'ITERATIONS': iterations, 'OFFSET': 0.25,
                                     'OUTPUT': 'memory:'})
        out_layer = res_smooth['OUTPUT']
        out_layer.setName(f"{layer.name()}_平滑_LLM")
        QgsProject.instance().addMapLayer(out_layer)
        return "矢量边界化简与平滑处理完成，已生成新图层。"
    except Exception as e:
        return f"矢量平滑失败: {e}"


def skill_kmeans_cluster(layer_name: str, k: int = 5, max_iters: int = 15) -> str:
    """7. K-Means 智能无监督聚类"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
        h, w = bands[0].shape
        X = np.stack(bands, axis=-1).reshape(-1, len(bands))

        np.random.seed(42)
        indices = np.random.choice(X.shape[0], k, replace=False)
        centers = X[indices]
        for _ in range(max_iters):
            dists = np.linalg.norm(X[:, None, :] - centers[None, :, :], axis=-1)
            labels = np.argmin(dists, axis=-1)
            new_centers = np.array(
                [X[labels == j].mean(axis=0) if np.any(labels == j) else centers[j] for j in range(k)])
            if np.allclose(centers, new_centers, atol=1e-2): break
            centers = new_centers

        cluster_map = labels.reshape(h, w).astype(np.uint8) + 1
        out_file = os.path.join(tempfile.gettempdir(), f"KMeans_{datetime.now().strftime('%H%M%S')}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, w, h, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(cluster_map)
        out_ds = None

        QgsProject.instance().addMapLayer(QgsRasterLayer(out_file, f"KMeans聚类_K{k}_LLM"))
        return f"K-Means (K={k}) 聚类执行成功。"
    except Exception as e:
        return f"K-Means 失败: {e}"


def skill_raster_diff(layer_t1: str, layer_t2: str, threshold: float = 30.0, polygonize: bool = True) -> str:
    """8. 双期像元差分变化检测"""
    try:
        l1 = get_layer_by_name(layer_t1, "raster")
        l2 = get_layer_by_name(layer_t2, "raster")
        ds1 = gdal.Open(l1.source());
        ds2 = gdal.Open(l2.source())
        arr1 = ds1.GetRasterBand(1).ReadAsArray().astype(np.float32)
        arr2 = ds2.GetRasterBand(1).ReadAsArray().astype(np.float32)

        diff = np.abs(arr2 - arr1)
        change_mask = np.where(diff >= threshold, 1, 0).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"diff_{time_str}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, ds1.RasterXSize, ds1.RasterYSize, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds1.GetGeoTransform())
        out_ds.SetProjection(ds1.GetProjection())
        band_out = out_ds.GetRasterBand(1)
        band_out.WriteArray(change_mask)
        band_out.FlushCache()

        if polygonize:
            shp_driver = ogr.GetDriverByName("ESRI Shapefile")
            out_shp = os.path.join(tempfile.gettempdir(), f"diff_poly_{time_str}.shp")
            srs = osr.SpatialReference()
            srs.ImportFromWkt(ds1.GetProjection())
            shp_ds = shp_driver.CreateDataSource(out_shp)
            shp_layer = shp_ds.CreateLayer("change", srs, ogr.wkbPolygon)
            shp_layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
            gdal.Polygonize(band_out, band_out, shp_layer, 0, [], callback=None)
            shp_ds = None
            QgsProject.instance().addMapLayer(QgsVectorLayer(out_shp, f"像元差分矢量图斑_LLM", "ogr"))

        out_ds = None
        QgsProject.instance().addMapLayer(QgsRasterLayer(out_tif, f"像元差分掩膜_LLM"))
        return "双期影像像元级差分检测成功，已生成变化区域图层。"
    except Exception as e:
        return f"差分检测失败: {e}"


def skill_image_enhance(layer_name: str, r: int = 4, g: int = 3, b: int = 2) -> str:
    """9. 假彩色合成与画质增强"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        bands_data = []
        for b_idx in [r, g, b]:
            arr = ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
            p2, p98 = np.percentile(arr[arr > 0], (2, 98))
            arr = np.clip((arr - p2) / (p98 - p2 + 1e-5) * 255.0, 0, 255)
            bands_data.append(arr.astype(np.uint8))

        out_file = os.path.join(tempfile.gettempdir(), f"enhance_{datetime.now().strftime('%H%M%S')}.tif")
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 3, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        for i, b_arr in enumerate(bands_data):
            out_ds.GetRasterBand(i + 1).WriteArray(b_arr)
        out_ds = None

        QgsProject.instance().addMapLayer(QgsRasterLayer(out_file, f"增强影像_LLM"))
        return f"基于波段 {r}-{g}-{b} 的假彩色画质增强已完成。"
    except Exception as e:
        return f"增强失败: {e}"


def skill_raster_polygonize(layer_name: str, sieve_size: int = 4) -> str:
    """10. 栅格掩膜转矢量与去噪点"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        src_band = ds.GetRasterBand(1)
        if sieve_size > 0:
            gdal.SieveFilter(src_band, None, src_band, sieve_size, 4)

        out_shp = os.path.join(tempfile.gettempdir(), f"polygon_{datetime.now().strftime('%H%M%S')}.shp")
        driver = ogr.GetDriverByName("ESRI Shapefile")
        srs = osr.SpatialReference()
        srs.ImportFromWkt(ds.GetProjection())
        shp_ds = driver.CreateDataSource(out_shp)
        shp_layer = shp_ds.CreateLayer("polygonized", srs, ogr.wkbPolygon)
        shp_layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))

        gdal.Polygonize(src_band, src_band, shp_layer, 0, [], callback=None)
        shp_ds = None

        QgsProject.instance().addMapLayer(QgsVectorLayer(out_shp, f"栅格转矢量_LLM", "ogr"))
        return "栅格已成功转换为矢量多边形图斑（并滤除孤立碎斑）。"
    except Exception as e:
        return f"矢量化失败: {e}"


# =========================================================================
# 二、三大核心 AI 云端解译派发器
# =========================================================================

def skill_ai_extract_feature(layer_name: str, feature_type: str, server_url: str, token: str,
                              machine_id: str, extent=None, extent_crs=None):
    from .interpret_task import InterpretTask
    from .constants import find_class_ids_by_keywords, get_model_key_by_mode

    layer = get_layer_by_name(layer_name, "raster")
    target_class_id = find_class_ids_by_keywords([feature_type], fallback_id="5")
    real_model_key = get_model_key_by_mode("landuse", fallback_key="LANDUSE")

    task_extent = extent if extent is not None else layer.extent()
    task_extent_crs = extent_crs if extent_crs is not None else layer.crs()

    task = InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class=target_class_id, prompt="", output_format="mask",
        server_url=server_url, machine_id=machine_id, token=token
    )
    return task


def skill_ai_sam3_extract(layer_name: str, prompt: str, output_format: str, server_url: str, token: str,
                          machine_id: str, extent=None, extent_crs=None):
    from .interpret_task import InterpretTask
    from .constants import get_model_key_by_mode

    layer = get_layer_by_name(layer_name, "raster")
    real_model_key = get_model_key_by_mode("sam3", fallback_key="SAM3_MODEL")

    task_extent = extent if extent is not None else layer.extent()
    task_extent_crs = extent_crs if extent_crs is not None else layer.crs()

    task = InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt=prompt, output_format=output_format,
        server_url=server_url, machine_id=machine_id, token=token
    )
    return task


def skill_ai_change_detection(layer_t1: str, layer_t2: str, server_url: str, token: str, machine_id: str,
                              extent=None, extent_crs=None):
    from .interpret_task import InterpretTask
    from .constants import get_model_key_by_mode

    l1 = get_layer_by_name(layer_t1, "raster")
    l2 = get_layer_by_name(layer_t2, "raster")
    real_model_key = get_model_key_by_mode("change_detection", fallback_key="CHANGE_DETECTION")

    task_extent = extent if extent is not None else l1.extent()
    task_extent_crs = extent_crs if extent_crs is not None else l1.crs()

    task = InterpretTask(
        raster_layer=l1, raster_layer_after=l2, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt="", output_format="mask",
        server_url=server_url, machine_id=machine_id, token=token
    )
    return task