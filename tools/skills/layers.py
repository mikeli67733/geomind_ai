# -*- coding: utf-8 -*-
"""Layer introspection skills: raster physical profiling and project-wide layer report."""
from typing import Dict, Any

from osgeo import gdal

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.utils import iface

from ...core.logger import get_logger


logger = get_logger("tools.skills.layers")


def get_layer_by_name(layer_name: str, layer_type: str = "raster"):
    """按名称查找图层并进行类型校验。"""
    layers = QgsProject.instance().mapLayersByName(layer_name)
    if not layers:
        raise ValueError(f"找不到图层: '{layer_name}'")
    layer = layers[0]
    if layer_type == "raster" and not isinstance(layer, QgsRasterLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是栅格图层")
    if layer_type == "vector" and not isinstance(layer, QgsVectorLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是矢量图层")
    return layer


def _inspect_raster_profile(layer: QgsRasterLayer) -> Dict[str, Any]:
    """探测栅格影像的物理分辨率与波段数，推断适用的算法路径。"""
    source = layer.source().lower()
    provider = layer.providerType().lower()
    name = layer.name().lower()

    is_online_tile = provider in ("wms", "wmts", "xyz") or "type=xyz" in source
    is_sentinel = "sentinel" in name or "s2_" in source or "sentinel-2" in source

    band_count = layer.bandCount()

    res_x = 999.0
    res_y = 999.0
    if not is_online_tile:
        try:
            ds = gdal.Open(layer.source())
            if ds:
                gt = ds.GetGeoTransform()
                res_x = abs(gt[1])
                res_y = abs(gt[5])
                if layer.crs().isGeographic():
                    res_x *= 111320.0
                    res_y *= 111320.0
                ds = None
        except Exception:
            pass

    if is_online_tile:
        data_type = "在线XYZ/WMTS瓦片 (仅RGB目视，无物理反射率)"
        suggested_route = "【标准地物首选】skill_ai_extract_feature；特殊地物降级使用 SAM3 或 底图参考；【严禁计算物理光谱指数】"
    elif is_sentinel or (band_count >= 4 and 8.0 <= res_x <= 35.0):
        data_type = f"哨兵/中分辨率多光谱卫星 ({res_x:.1f}m 分辨率, {band_count}波段)"
        suggested_route = "【标准地物首选】skill_ai_extract_feature；特殊地物降级使用 SAM3；支持物理光谱指数计算"
    elif res_x < 0.35:
        data_type = f"无人机超高分辨率正射影像 ({res_x*100:.1f}cm 厘米级, {band_count}波段)"
        suggested_route = "【强制首选 SAM3 提示词模型】skill_ai_sam3_extract（地物模型易失效）"
    elif res_x <= 3.5:
        data_type = f"高分辨率卫星影像 ({res_x:.2f}m 分辨率, {band_count}波段)"
        suggested_route = "【标准地物首选】skill_ai_extract_feature；特殊地物降级使用 SAM3"
    else:
        data_type = f"通用高程/栅格数据 ({res_x:.1f}m 分辨率, {band_count}波段)"
        suggested_route = "支持 DEM 地形分析、聚类或滤波算子"

    return {
        "data_type": data_type,
        "band_count": band_count,
        "resolution_m": round(res_x, 3),
        "is_online_tile": is_online_tile,
        "suggested_route": suggested_route,
    }


def get_active_layers() -> str:
    """获取当前工程全部图层的物理属性画像与算法调度建议。"""
    layers = QgsProject.instance().mapLayers().values()
    if not layers:
        return (
            "当前 QGIS 工程中无任何图层。\n"
            "- 若需大范围卫星数据，请调用 `skill_fetch_sentinel2_imagery` 在线拉取哨兵影像；\n"
            "- 若需高程数据，请调用 `skill_fetch_dem_data` 在线拉取 DEM；\n"
            "- 若需超高精解译，请提示用户加载本地无人机影像。"
        )

    info = ["🗺️ **当前 QGIS 工程活动图层画像与调度路由**："]
    for l in layers:
        if isinstance(l, QgsRasterLayer):
            profile = _inspect_raster_profile(l)
            info.append(
                f"- **[栅格图层] `{l.name()}`**\n"
                f"  - 数据类别: `{profile['data_type']}`\n"
                f"  - 推荐处理链: {profile['suggested_route']}"
            )
        elif isinstance(l, QgsVectorLayer):
            geom_type = ["点", "线", "面", "无几何"][min(l.geometryType(), 3)]
            info.append(f"- **[矢量图层] `{l.name()}`**: 类型={geom_type}，要素数={l.featureCount()}个")
    return "\n".join(info)
