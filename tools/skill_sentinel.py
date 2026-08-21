# -*- coding: utf-8 -*-
"""
Sentinel-2 STAC Search and Stream Loader for QGIS.
"""
import requests
from datetime import datetime, timedelta
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsCoordinateTransform, 
    QgsCoordinateReferenceSystem, QgsRectangle
)

STAC_API_URL = "https://earth-search.aws.element84.com/v1/search"


def search_and_load_sentinel2(
    extent_bbox: list, 
    date_start: str = None, 
    date_end: str = None, 
    max_cloud_cover: int = 15,
    auto_load_first: bool = True
) -> str:
    """
    检索指定时空范围的 Sentinel-2 L2A 影像，并可自动将最佳影像流式加载到 QGIS。
    
    :param extent_bbox: [min_x, min_y, max_x, max_y] (必须为 EPSG:4326 经纬度)
    :param date_start: 起始日期 'YYYY-MM-DD'，默认为 14 天前
    :param date_end: 结束日期 'YYYY-MM-DD'，默认为今天
    :param max_cloud_cover: 最大云量百分比 (0-100)
    :param auto_load_first: 是否自动将云量最少的一期加载到地图
    """
    today = datetime.now()
    if not date_end:
        date_end = today.strftime("%Y-%m-%d")
    if not date_start:
        date_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")

    # 构造 STAC 检索请求体
    payload = {
        "collections": ["sentinel-2-l2a"],
        "bbox": extent_bbox,
        "datetime": f"{date_start}T00:00:00Z/{date_end}T23:59:59Z",
        "query": {
            "eo:cloud_cover": {"lt": max_cloud_cover}
        },
        "limit": 5,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}]  # 按云量升序
    }

    try:
        resp = requests.post(STAC_API_URL, json=payload, timeout=12)
        if resp.status_code != 200:
            return f"STAC 检索服务异常 (HTTP {resp.status_code})"
        
        data = resp.json()
        features = data.get("features", [])
        if not features:
            return f"在 {date_start} 至 {date_end} 期间未检索到云量 < {max_cloud_cover}% 的 Sentinel-2 影像，建议放宽日期或云量限制。"

        result_lines = [f"🛰️ 成功检索到 {len(features)} 景符合条件的 Sentinel-2 影像："]
        
        loaded_layer_name = ""
        for i, item in enumerate(features):
            props = item.get("properties", {})
            acq_time = props.get("datetime", "")[:10]
            cloud = props.get("eo:cloud_cover", 0.0)
            item_id = item.get("id", "")
            
            # 获取 10米分辨率真彩色合成波段 (Visual / TCI)
            assets = item.get("assets", {})
            visual_asset = assets.get("visual") or assets.get("overview")
            visual_url = visual_asset.get("href") if visual_asset else None
            
            result_lines.append(f"{i+1}. 拍摄日期: `{acq_time}` | 云量: `{cloud:.1f}%` | 景号: `{item_id}`")

            # 自动加载第一景（云量最少）到 QGIS
            if i == 0 and auto_load_first and visual_url:
                # 使用 GDAL 的 /vsicurl/ 协议实现无需下载的 COG 在线秒级流式渲染
                gdal_url = f"/vsicurl/{visual_url}"
                layer_name = f"Sentinel2_{acq_time}_云量{cloud:.1f}%"
                
                layer = QgsRasterLayer(gdal_url, layer_name, "gdal")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layer_name = layer_name
        
        if loaded_layer_name:
            result_lines.append(f"\n🎉 **已自动为您流式加载最优影像**：`{loaded_layer_name}`（无需等待下载，已直接上屏）。")

        return "\n".join(result_lines)

    except Exception as e:
        return f"检索 Sentinel-2 影像时发生异常: {e}"