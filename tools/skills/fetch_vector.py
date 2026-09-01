# -*- coding: utf-8 -*-
"""OpenStreetMap Overpass vector data acquisition skill."""

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

from .common import _get_target_bbox, _HTTP


logger = get_logger("tools.skills.fetch_vector")
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]


def skill_fetch_osm_vector_data(
        place_name: str = "当前视口",
        feature_type: str = "all",
        custom_tag: str = ""
) -> str:
    """
    通过 Overpass API 获取 OpenStreetMap 真实矢量 JSON 数据，并在 QGIS 中自动生成矢量图层。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name)
        min_lon, min_lat, max_lon, max_lat = bbox

        tag_filter = ""
        if custom_tag:
            tag_filter = f"[{custom_tag}]"
        elif feature_type == "building":
            tag_filter = '["building"]'
        elif feature_type == "highway":
            tag_filter = '["highway"]'
        elif feature_type == "water":
            tag_filter = '["natural"~"water|wetland"]'
        elif feature_type == "amenity":
            tag_filter = '["amenity"]'
        elif feature_type == "landuse":
            tag_filter = '["landuse"]'

        overpass_query = f"""
        [out:json][timeout:25];
        (
          node{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
          way{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
          relation{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
        );
        out body;
        >;
        out skel qt;
        """

        data = None
        # 并行探测多个 Overpass 服务器，取最先成功者；避免单个服务器
        # 20s 超时拖慢整个流程（并发后最坏约等于单次 6s 超时）。
        import concurrent.futures

        def _probe(server):
            try:
                resp = _HTTP.post(
                    server, data={"data": overpass_query}, timeout=6)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                return None
            return None

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(OVERPASS_SERVERS))
        try:
            futures = {executor.submit(_probe, s): s for s in OVERPASS_SERVERS}
            for fut in concurrent.futures.as_completed(futures):
                result = fut.result()
                if result and "elements" in result:
                    data = result
                    for other in futures:
                        other.cancel()
                    break
        finally:
            executor.shutdown(wait=False)

        if not data or "elements" not in data:
            return f"{located_msg}❌ 获取 OSM 矢量 JSON 数据失败：Overpass 接口连接超时，请缩小视口后重试。"

        elements = data.get("elements", [])
        if not elements:
            return f"{located_msg}ℹ️ 当前区域内未检索到符合条件的 OSM 矢量要素。"

        nodes_dict = {}
        for elem in elements:
            if elem.get("type") == "node" and "lon" in elem and "lat" in elem:
                nodes_dict[elem["id"]] = QgsPointXY(elem["lon"], elem["lat"])

        point_features, line_features, poly_features = [], [], []

        for elem in elements:
            tags = elem.get("tags", {})
            if not tags:
                continue

            elem_id = str(elem.get("id"))
            name = tags.get("name") or tags.get("name:en") or tags.get("name:zh") or "未命名"
            elem_type = tags.get("building") or tags.get("highway") or tags.get("amenity") or tags.get(
                "natural") or tags.get("landuse") or "osm_feature"
            tags_json_str = json.dumps(tags, ensure_ascii=False)

            if elem.get("type") == "node":
                if elem["id"] in nodes_dict:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPointXY(nodes_dict[elem["id"]]))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    point_features.append(feat)

            elif elem.get("type") == "way":
                node_ids = elem.get("nodes", [])
                pts = [nodes_dict[nid] for nid in node_ids if nid in nodes_dict]
                if len(pts) < 2:
                    continue

                is_polygon = (len(pts) >= 4 and pts[0] == pts[-1] and (
                        "building" in tags or "landuse" in tags or "natural" in tags or "area" in tags
                ))

                if is_polygon:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPolygonXY([pts]))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    poly_features.append(feat)
                else:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    line_features.append(feat)

        time_tag = datetime.now().strftime("%H%M%S")
        loaded_layers = []

        def create_layer(geom_type, name, features):
            vlayer = QgsVectorLayer(f"{geom_type}?crs=EPSG:4326", name, "memory")
            prov = vlayer.dataProvider()
            prov.addAttributes([
                QgsField("osm_id", QVariant.String),
                QgsField("name", QVariant.String),
                QgsField("type", QVariant.String),
                QgsField("tags", QVariant.String),
            ])
            vlayer.updateFields()
            prov.addFeatures(features)
            vlayer.updateExtents()
            QgsProject.instance().addMapLayer(vlayer)
            return vlayer.name()

        if poly_features:
            lyr = create_layer("Polygon", f"OSM_面要素_{feature_type}_{time_tag}", poly_features)
            loaded_layers.append(f"`{lyr}` ({len(poly_features)} 个面)")
        if line_features:
            lyr = create_layer("LineString", f"OSM_线要素_{feature_type}_{time_tag}", line_features)
            loaded_layers.append(f"`{lyr}` ({len(line_features)} 条线)")
        if point_features:
            lyr = create_layer("Point", f"OSM_点要素_{feature_type}_{time_tag}", point_features)
            loaded_layers.append(f"`{lyr}` ({len(point_features)} 个点)")

        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        if not loaded_layers:
            return f"{located_msg}⚠️ 获取到了 {len(elements)} 个 OSM 拓扑节点，但未构建出有效几何要素。"

        return (
                f"{located_msg}🎉 **已成功获取 OpenStreetMap 真实 JSON 矢量数据并加载至工程**：\n"
                + "\n".join([f"- {l}" for l in loaded_layers])
                + "\n💡 *图层属性表中包含原始 OSM 标签属性 (name, type, tags)，可直接参与空间分析或编辑。*"
        )

    except Exception as e:
        return f"获取 OSM 矢量数据异常: {e}"
