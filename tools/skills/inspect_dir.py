# -*- coding: utf-8 -*-
"""技能：纯后台读取与扫描本地工作目录中的栅格和矢量成果（绝不加载到 QGIS 画布）。"""
import os
import glob
from datetime import datetime
from qgis.core import QgsApplication


def skill_scan_work_directory(
    folder_path: str = "",
    file_type: str = "all",
    detail_level: str = "summary"
) -> str:
    """
    【本地文件技能】纯后台静默扫描本地工作文件夹，检索遥感成果文件（TIFF、VRT、SHP等），
    返回文件清单、大小、生成时间与波段/要素信息。绝不向当前 QGIS 地图画布加载任何图层。

    :param folder_path: 目标工作文件夹路径。留空则默认使用插件的 monitor 监控目录。
    :param file_type: 筛选文件类型，可选: 'all'（全部）, 'raster'（仅栅格）, 'vector'（仅矢量）。
    :param detail_level: 详略度: 'summary'（基础清单与大小）, 'deep'（深入读取栅格长宽/波段/要素数）。
    :return: 纯文本/Markdown 格式的检索结果清单。
    """
    try:
        # 1. 路径兜底
        if not folder_path or not folder_path.strip():
            folder_path = os.path.join(QgsApplication.qgisSettingsDirPath(), "geomind_ai", "monitor")

        folder_path = os.path.abspath(folder_path.strip())
        if not os.path.exists(folder_path):
            return f"❌ 目标文件夹不存在：`{folder_path}`"

        # 2. 匹配对应成果后缀
        extensions = []
        ft = (file_type or "all").lower()
        if ft in ("all", "raster"):
            extensions.extend(["*.tif", "*.tiff", "*.vrt"])
        if ft in ("all", "vector"):
            extensions.extend(["*.shp", "*.geojson", "*.gpkg"])

        matched_files = []
        for ext in extensions:
            matched_files.extend(glob.glob(os.path.join(folder_path, "**", ext), recursive=True))

        # 过滤掉分波段缓存目录中的碎片文件（如 raw/ 目录）
        clean_files = [f for f in matched_files if os.sep + "raw" + os.sep not in f]
        if not clean_files:
            clean_files = matched_files

        if not clean_files:
            return f"📁 **工作文件夹**：`{folder_path}`\n\n该目录下目前未检索到任何符合条件的栅格或矢量成果文件。"

        # 按最后修改时间倒序排列（最新生成的文件排在前面）
        clean_files.sort(key=lambda x: os.path.getmtime(x), reverse=True)

        lines = [
            f"📁 **工作目录文件检索报告**",
            f"- 路径：`{folder_path}`",
            f"- 检索到有效成果：**{len(clean_files)}** 个文件\n",
            "| 文件名 | 类型 | 大小 | 生成/修改时间 | 详情/属性 |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ]

        from osgeo import gdal, ogr

        for fpath in clean_files:
            fname = os.path.basename(fpath)
            fext = os.path.splitext(fname)[1].lower()
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            size_desc = f"{size_mb:.2f} MB" if size_mb >= 0.01 else "< 10 KB"
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M:%S")

            extra_info = "—"
            if detail_level == "deep":
                try:
                    if fext in ('.tif', '.tiff', '.vrt'):
                        ds = gdal.Open(fpath, gdal.GA_ReadOnly)
                        if ds:
                            extra_info = f"{ds.RasterXSize}×{ds.RasterYSize}, {ds.RasterCount}波段"
                            ds = None
                    elif fext in ('.shp', '.geojson', '.gpkg'):
                        ds = ogr.Open(fpath, 0)
                        if ds:
                            lyr = ds.GetLayer(0)
                            extra_info = f"{lyr.GetFeatureCount()} 个要素"
                            ds = None
                except Exception:
                    pass

            ftype_desc = "栅格 (GeoTIFF)" if fext in ('.tif', '.tiff') else (
                "虚拟栅格 (VRT)" if fext == '.vrt' else "矢量 (Shapefile)"
            )

            lines.append(f"| `{fname}` | {ftype_desc} | {size_desc} | {mtime} | {extra_info} |")

        return "\n".join(lines)

    except Exception as exc:
        return f"扫描工作文件夹异常: {exc}"