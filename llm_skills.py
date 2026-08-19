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
import requests
import time

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsApplication, QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField, QgsProject
)
from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY,
    QgsProject, QgsField, QgsCoordinateTransform, QgsCoordinateReferenceSystem
)
from qgis.utils import iface
from qgis.PyQt.QtCore import QVariant
import json
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
import json
import time
import requests
from typing import Optional
from qgis.core import (
    QgsVectorLayer,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform
)
from qgis.PyQt.QtCore import QVariant


def skill_geocode_address(address_text: str, lon: Optional[float] = None, lat: Optional[float] = None) -> str:
    """
    地理编码与地图定位工具（支持国内外地址）：
    - 国内地址：只需传入 address_text，自动使用天地图高精度解析。
    - 国外地址/天地图无数据时：请大语言模型基于自身常识知识，在调用时一并传入估算的 WGS84 经度(lon)和纬度(lat)。

    :param address_text: 地点/地址名称（如 "北京市海淀区中关村" 或 "巴黎埃菲尔铁塔"）
    :param lon: [可选] 经度(WGS84)，国外地址请模型直接提供
    :param lat: [可选] 纬度(WGS84)，国外地址请模型直接提供
    :return: 执行状态与定位结果
    """
    tk = "7ba1ada42adefb5df42e4a1364b321c4"
    source_type = "天地图"

    try:
        if iface is None:
            return "错误：获取不到QGIS iface对象，无法操作地图画布"

        # -------------------------------------------------------------
        # 1. 坐标获取逻辑（国外由LLM传入 / 国内调用天地图）
        # -------------------------------------------------------------
        if lon is not None and lat is not None:
            # 模式 A：大模型直接提供了经纬度（适用于国外地点或已知坐标）
            lon = float(lon)
            lat = float(lat)
            source_type = "大模型地理常识估算/国外定位"
        else:
            # 模式 B：国内地址，调用天地图 API
            if not tk or len(tk) < 10:
                return "地图tk密钥无效，请检查配置"

            url = "https://api.tianditu.gov.cn/geocoder"
            ds_data = json.dumps({"keyWord": address_text}, ensure_ascii=False)
            params = {
                "ds": ds_data,
                "tk": tk
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tianditu.gov.cn/"
            }

            time.sleep(0.3)
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            js = resp.json()

            if js.get("status") != "0":
                return f"地图解析失败: {js.get('msg', '未知错误')}。若是国外地址，请直接传入 lon 和 lat 经纬度调用。"

            location = js.get("location")
            if not location:
                return (
                    f"地图未匹配到国内结果：'{address_text}'（可能为国外地点或生僻地名）。\n"
                    f"请大语言模型根据自身知识库评估该地点的 WGS84 经度(lon)和纬度(lat)，重新调用此函数。"
                )

            lon = float(location["lon"])
            lat = float(location["lat"])

        # -------------------------------------------------------------
        # 2. 生成 QGIS 内存图层并打点
        # -------------------------------------------------------------
        layer_name = f"定位_{address_text[:10]}"
        vlayer = QgsVectorLayer("Point?crs=EPSG:4326", layer_name, "memory")
        prov = vlayer.dataProvider()
        prov.addAttributes([
            QgsField("address", QVariant.String),
            QgsField("lon", QVariant.Double),
            QgsField("lat", QVariant.Double),
            QgsField("source", QVariant.String)
        ])
        vlayer.updateFields()

        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        feat.setAttributes([address_text, lon, lat, source_type])
        prov.addFeature(feat)
        vlayer.updateExtents()
        QgsProject.instance().addMapLayer(vlayer)

        # -------------------------------------------------------------
        # 3. 画布坐标系转换并跳转定位
        # -------------------------------------------------------------
        canvas = iface.mapCanvas()
        point_4326 = QgsPointXY(lon, lat)
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        dest_crs = canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        canvas_point = transform.transform(point_4326)

        canvas.setCenter(canvas_point)
        canvas.zoomScale(50000)
        canvas.refresh()

        return f"地址定位完成：{address_text} (经度={lon:.6f}, 纬度={lat:.6f})，画布已跳转。"

    except Exception as e:
        return f"地址解析/定位失败: {str(e)}"

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


# ====== llm_skills.py 追加 QGIS 工具箱向量检索与执行逻辑 ======
from qgis import processing
from qgis.core import (
    QgsApplication, QgsProject, QgsMapLayer, QgsVectorLayer, QgsRasterLayer,
    QgsProcessingParameterDefinition
)
from .qgis_vector_indexer import QgisToolVectorIndexer


def qgis_search_tools(query: str, top_k: int = 5) -> str:
    """语义搜索 QGIS 算子"""
    indexer = QgisToolVectorIndexer()
    results = indexer.search(query, top_k=top_k)
    if not results:
        return f"未找到与 '{query}' 相关的 QGIS 算子。"

    lines = [f"🔍 为您检索到最匹配的 {len(results)} 个 QGIS 算子："]
    for r in results:
        lines.append(f"- **ID**: `{r['id']}` | **名称**: {r['name']} ({r['group']}) | 相似度: {r['score']:.2f}")
        lines.append(f"  *描述*: {r['description']}")
    lines.append("\n👉 您可以调用 `qgis_get_tool_params(algorithm_id)` 获取入参详情，随后调用 `qgis_run_algorithm` 执行。")
    return "\n".join(lines)


def qgis_get_tool_params(algorithm_id: str) -> str:
    """获取指定算法的参数 Schema"""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if not alg:
        return f"错误：未找到算子 `{algorithm_id}`"

    param_info = [f"🛠️ **算法 `{algorithm_id}` ({alg.displayName()}) 参数列表**:"]
    for p in alg.parameterDefinitions():
        req = "必填" if not (p.flags() & QgsProcessingParameterDefinition.FlagOptional) else "选填"
        param_info.append(f"- **{p.name()}** ({p.type()}, {req}): {p.description()} (默认值: {p.defaultValue()})")

    return "\n".join(param_info)


def _looks_like_layer_param(alg, param_name: str) -> bool:
    """粗略判断某个参数是否是图层类输入，用于提前拦截"传了图层名但找不到"的情况"""
    try:
        p = alg.parameterDefinition(param_name)
        if p is None:
            return False
        return p.type() in ("source", "layer", "raster", "vector", "multilayer")
    except Exception:
        return param_name.upper() in ("INPUT", "SOURCE", "LAYER", "LAYER_T1", "LAYER_T2")


def qgis_run_algorithm(algorithm_id: str, parameters: dict) -> str:
    """使用 pyqgis 真正运行算法，用显式 context 避免输出图层被提前销毁，并做严格结果校验"""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    try:
        registry = QgsApplication.processingRegistry()
        # 用 createAlgorithmById 生成一个独立实例，而不是复用注册表里的共享单例，
        # 避免多次调用之间互相污染状态
        alg = registry.createAlgorithmById(algorithm_id)
        if not alg:
            return f"❌ 错误：未找到算法 `{algorithm_id}`"

        # 1. 解析参数中的图层（LLM 传入图层名时自动转换为 QgsMapLayer 对象）
        resolved_params = {}
        missing_layer_names = []
        for k, v in parameters.items():
            if isinstance(v, str):
                layers = QgsProject.instance().mapLayersByName(v)
                if layers:
                    resolved_params[k] = layers[0]
                elif _looks_like_layer_param(alg, k):
                    # 参数名明显是图层输入，但按名字找不到对应图层，直接拦截报错
                    missing_layer_names.append((k, v))
                    resolved_params[k] = v
                else:
                    resolved_params[k] = v
            else:
                resolved_params[k] = v

        if missing_layer_names:
            detail = ", ".join(f"{k}='{v}'" for k, v in missing_layer_names)
            return f"❌ 参数解析失败：找不到图层 {detail}，请先调用 get_active_layers 确认准确图层名后重试。"

        # 2. 为所有未指定的输出参数赋默认值
        for out in alg.outputDefinitions():
            if out.name() not in resolved_params:
                resolved_params[out.name()] = "memory:"

        # 3. 用显式、存活到函数结束的 context 执行算法（关键修复点）
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()

        # 注意：QgsProcessingAlgorithm.run() 返回的元组顺序是 (results, ok)，
        # 不是 (ok, results)！先结果字典，后布尔值，顺序不能写反。
        outputs, ok = alg.run(resolved_params, context, feedback)

        if not ok:
            return f"❌ 算法 `{alg.displayName()}` ({algorithm_id}) 执行失败：{feedback.textLog() or '无详细日志'}"

        # 4. 捕获产出图层并加载到项目
        loaded_layers = []

        # 4a. 官方标准做法：遍历 context.layersToLoadOnCompletion()。
        #     这是 processing.runAndLoadResults() 内部真正依赖的机制，
        #     它明确记录了"这次运行产生了哪些应当被加载进工程的图层"，
        #     比自己去猜 outputs 里字符串该怎么解析要可靠得多。
        layers_to_load = dict(context.layersToLoadOnCompletion())
        for layer_id, details in layers_to_load.items():
            layer = context.temporaryLayerStore().mapLayer(layer_id)
            if layer is None:
                layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None and layer.isValid():
                # 取出所有权，避免 context 销毁时图层被一并删除
                context.temporaryLayerStore().removeMapLayer(layer_id)
                display_name = details.name if getattr(details, "name", None) else layer.name()
                layer.setName(display_name)
                QgsProject.instance().addMapLayer(layer)
                loaded_layers.append(layer.name())

        # 4b. 兜底：极少数算法不走 layersToLoadOnCompletion，
        #     再尝试直接从 outputs 字符串/对象里恢复图层
        if not loaded_layers:
            for out_name, out_val in outputs.items():
                layer = None
                if isinstance(out_val, QgsMapLayer):
                    layer = out_val
                elif isinstance(out_val, str) and out_val:
                    layer = context.temporaryLayerStore().mapLayer(out_val)
                    if layer is None and os.path.exists(out_val):
                        lower = out_val.lower()
                        if lower.endswith(('.tif', '.tiff', '.img')):
                            cand = QgsRasterLayer(out_val, f"{alg.displayName()}_结果")
                            layer = cand if cand.isValid() else None
                        elif lower.endswith(('.shp', '.gpkg', '.geojson')):
                            cand = QgsVectorLayer(out_val, f"{alg.displayName()}_结果", "ogr")
                            layer = cand if cand.isValid() else None

                if layer is not None and layer.isValid():
                    context.temporaryLayerStore().takeMapLayer(layer)
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layers.append(layer.name())

        # 5. 严格校验：没捕获到任何有效图层，就不能说"执行成功"，避免 LLM 编造完成报告
        if not loaded_layers:
            return (
                f"⚠️ 算法 `{alg.displayName()}` ({algorithm_id}) 已调用完成，"
                f"但未捕获到任何有效的输出图层（可能是空结果、参数不匹配或图层被丢弃）。\n"
                f"原始输出: {json.dumps({k: str(v) for k, v in outputs.items()}, ensure_ascii=False)}\n"
                f"请不要向用户报告任务已成功完成，应如实说明结果未确认，并建议检查参数或重试。"
            )

        return f"✅ 算法 `{alg.displayName()}` ({algorithm_id}) 执行成功，已加载图层: {', '.join(loaded_layers)}"

    except Exception as e:
        return f"❌ 执行算子 `{algorithm_id}` 失败: {str(e)}"