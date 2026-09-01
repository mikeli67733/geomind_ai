# -*- coding: utf-8 -*-
"""Real-time web search (Bing/Baidu dual channel) and webpage text extraction."""

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


logger = get_logger("tools.skills.web")

#: 模块级共享会话（连接池复用），搜索/抓取需浏览器 UA 故不共用 common._HTTP
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
})


def skill_web_search(query: str, max_results: int = 5) -> str:
    """通用实时联网搜索工具（国内直连免Key免费版：Bing中国 + 百度双通道）。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }

    results = []

    # 通道 1：Bing 中国
    try:
        encoded_query = urllib.parse.quote(query)
        bing_url = f"https://cn.bing.com/search?q={encoded_query}&ensearch=0"

        resp = _HTTP.get(bing_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            raw_html = resp.text
            blocks = re.findall(r'<li class="b_algo"(.*?)</li>', raw_html, re.DOTALL)

            for b in blocks[:max_results]:
                t_m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', b, re.DOTALL)
                s_m = re.search(r'<div class="b_caption"><p[^>]*>(.*?)</p>', b, re.DOTALL) or re.search(
                    r'<p[^>]*>(.*?)</p>', b, re.DOTALL)

                if t_m:
                    link = t_m.group(1)
                    title = re.sub(r'<[^>]+>', '', t_m.group(2)).strip()
                    snippet = re.sub(r'<[^>]+>', '', s_m.group(1)).strip() if s_m else "无摘要"

                    title = unescape(title)
                    snippet = unescape(snippet)

                    if title and link.startswith("http"):
                        results.append(f"📌 **[{title}]({link})**\n   {snippet}")

            if results:
                return f"🔍 **Bing 搜索结果 (`{query}`)**：\n\n" + "\n\n".join(results)
    except Exception as e:
        logger.warning(f"Bing search failed, falling back to Baidu: {e}")

    # 通道 2：百度搜索
    try:
        baidu_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"
        resp = _HTTP.get(baidu_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            raw_html = resp.text
            blocks = re.findall(r'<div class="[a-z0-9-_]*\s*c-container[^"]*"(.*?)</div>\s*</div>', raw_html, re.DOTALL)

            for b in blocks[:max_results]:
                t_m = re.search(r'<h3[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h3>', b, re.DOTALL)
                s_m = re.search(r'<span class="content-right_[^"]*"[^>]*>(.*?)</span>', b, re.DOTALL) or re.search(
                    r'<div class="c-abstract"[^>]*>(.*?)</div>', b, re.DOTALL)

                if t_m:
                    link = t_m.group(1)
                    title = re.sub(r'<[^>]+>', '', t_m.group(2)).strip()
                    snippet = re.sub(r'<[^>]+>', '', s_m.group(1)).strip() if s_m else "无摘要"

                    title = unescape(title)
                    snippet = unescape(snippet)

                    if title:
                        results.append(f"📌 **[{title}]({link})**\n   {snippet}")

            if results:
                return f"🔍 **百度搜索结果 (`{query}`)**：\n\n" + "\n\n".join(results)
    except Exception as e_baidu:
        logger.warning(f"Baidu search fallback failed: {e_baidu}")

    return "❌ 联网搜索失败：当前网络未能连接到 Bing/百度搜索服务，请稍后重试。"


def skill_fetch_webpage_content(url: str, max_chars: int = 2500) -> str:
    """抓取并提取指定网页的正文文本内容（自动清洗 HTML 标签与冗余脚本）。"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = _HTTP.get(url, headers=headers, timeout=12)
        resp.encoding = resp.apparent_encoding or "utf-8"

        if resp.status_code != 200:
            return f"❌ 抓取网页失败，HTTP 状态码: {resp.status_code}"

        html = resp.text
        html = re.sub(r'<(script|style|head|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        text = unescape(text)

        if not text:
            return "⚠️ 网页抓取成功，但未提取到有效文本内容（可能是纯 JS 渲染页面）。"

        preview = text[:max_chars]
        truncated_msg = f"\n\n*(正文已截断，前 {max_chars} 字符)*" if len(text) > max_chars else ""
        return f"📄 **网页正文提取自**：`{url}`\n\n{preview}{truncated_msg}"

    except Exception as e:
        return f"抓取网页异常: {e}"
