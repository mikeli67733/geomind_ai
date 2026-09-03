# -*- coding: utf-8 -*-
"""
7x24 监控任务引擎 —— 后台独立线程静默执行，不卡死主界面、不加载图层。
逻辑：
1. 从现在开始(watch)：首次立即搜索当前最新的第 1 景并处理。后续心跳探测若 ID 相同则直接跳过，零下载/零算力消耗；
2. 历史回补(backfill)：从指定日期向最新逐景推进，完成后自动暂停。
"""
import json
import os
import time
import uuid
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import (
    QgsApplication, QgsRectangle, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
)

from ..core.logger import get_logger
from ..utils.wechat_push import push_markdown
from ..core.llm_client import run_copilot_agent

logger = get_logger("core.monitor_engine")

STAC_AWS_SEARCH = "https://earth-search.aws.element84.com/v1/search"
STAC_MPC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

_HTTP = None


def _http():
    global _HTTP
    if _HTTP is None:
        import requests
        _HTTP = requests.Session()
        _HTTP.headers["User-Agent"] = "GeoMind-QGIS-Plugin/1.0"
    return _HTTP


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jobs_dir() -> str:
    base = os.path.join(QgsApplication.qgisSettingsDirPath(), "geomind_ai")
    path = os.path.join(base, "jobs")
    os.makedirs(path, exist_ok=True)
    return path


def default_config() -> dict:
    return {
        "id": "",
        "name": "",
        "source": "s2",            # s2=哨兵光学 | s1=哨兵雷达
        "pol": "vv",
        "extent": None,            # [xmin, ymin, xmax, ymax]
        "extent_crs": "EPSG:4326",
        "mode": "watch",           # watch=从现在开始 | backfill=历史回补
        "start_date": "",
        "prompt": "",
        "threshold": 0.0,
        "heartbeat_min": 30,
        "work_dir": "",
        "webhook": "",
        "created_at": "",
    }


def _default_state() -> dict:
    return {
        "status": "paused",
        "cursor_date": "",         # 最近处理的景日期
        "cursor_scene": "",        # 最近处理的景 ID（用于精准去重）
        "last_check_at": "",
        "last_result": "",
        "last_error": "",
        "last_file": "",           # 上一時相生成的文件路径
        "next_run_at": 0.0,
        "outputs": [],
    }


def _job_path(job_id: str) -> str:
    return os.path.join(jobs_dir(), f"{job_id}.json")


def load_job(job_id: str) -> Optional[dict]:
    try:
        with open(_job_path(job_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("id"):
            data.setdefault("state", _default_state())
            return data
    except Exception:
        return None
    return None


def save_job(job: dict) -> None:
    try:
        with open(_job_path(job["id"]), "w", encoding="utf-8") as fh:
            json.dump(job, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("保存任务失败 %s: %s", job.get("id"), exc)


def list_jobs() -> List[dict]:
    jobs = []
    try:
        for name in sorted(os.listdir(jobs_dir())):
            if name.endswith(".json"):
                job = load_job(name[:-5])
                if job:
                    jobs.append(job)
    except Exception:
        pass
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


class MonitorEngine(QObject):
    jobsChanged = pyqtSignal()
    stepFinished = pyqtSignal(str, str)
    logLine = pyqtSignal(str, str)   # (job_id, 日志行) 供运行日志窗口实时显示

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._logs: Dict[str, List[str]] = {}   # 每个任务最近 400 行运行日志
        self._timer = QTimer(self)
        self._timer.setInterval(5 * 1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _log(self, job_id: str, msg: str) -> None:
        """记录一行运行日志：写缓冲 + 发信号（后台线程调用安全，Qt 自动排队到主线程）。"""
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        buf = self._logs.setdefault(job_id, [])
        buf.append(line)
        if len(buf) > 400:
            del buf[: len(buf) - 400]
        self.logLine.emit(job_id, line)

    def get_logs(self, job_id: str) -> List[str]:
        return list(self._logs.get(job_id, []))

    def restore(self) -> int:
        restored = 0
        for job in list_jobs():
            if job["state"]["status"] == "running":
                job["state"]["next_run_at"] = time.time()
            restored += 1
        self.jobsChanged.emit()
        return restored

    def create_job(self, cfg: dict) -> str:
        cfg = dict(cfg)
        cfg["id"] = uuid.uuid4().hex[:12]
        cfg["created_at"] = _now_iso()
        cfg["state"] = _default_state()
        if cfg["mode"] == "watch":
            cfg["start_date"] = ""
            # 空 cursor_date/scene 让首次探测立即拉取最新一景
            cfg["state"]["cursor_date"] = ""
            cfg["state"]["cursor_scene"] = ""
        save_job(cfg)
        self.jobsChanged.emit()
        return cfg["id"]

    def delete_job(self, job_id: str) -> None:
        try:
            os.remove(_job_path(job_id))
        except Exception:
            pass
        self.jobsChanged.emit()

    def start_job(self, job_id: str) -> None:
        job = load_job(job_id)
        if not job:
            return
        st = job["state"]
        st["status"] = "running"
        st["next_run_at"] = time.time() + 1
        st["last_error"] = ""
        save_job(job)
        self.jobsChanged.emit()
        QTimer.singleShot(1000, self._tick)

    def pause_job(self, job_id: str) -> None:
        job = load_job(job_id)
        if not job:
            return
        job["state"]["status"] = "paused"
        save_job(job)
        self.jobsChanged.emit()

    def run_now(self, job_id: str) -> str:
        job = load_job(job_id)
        if not job:
            return "任务不存在"
        if self._busy:
            return "后台正在处理其他任务，请稍后"
        self._schedule_worker(job)
        return "⚡ 已触发立即执行，正在后台静默探测影像..."

    def _tick(self):
        if self._busy:
            return
        now = time.time()
        for job in list_jobs():
            st = job["state"]
            if st["status"] != "running" or st["next_run_at"] > now:
                continue
            self._schedule_worker(job)
            break

    def _schedule_worker(self, job: dict):
        """将耗时的下载、运算、网络请求推入后台子线程，保证 QGIS 毫无卡顿"""
        self._busy = True

        def _worker():
            try:
                self._run_one_step(job)
            except Exception as exc:
                logger.exception("后台任务运行异常 %s", job.get("id"))
                job["state"]["last_error"] = str(exc)
                try:
                    self._log(job.get("id", ""), f"❌ 运行异常: {exc}")
                except Exception:
                    pass
                job["state"]["next_run_at"] = time.time() + 60
            finally:
                save_job(job)
                self._busy = False
                # 回到主线程发信号
                QTimer.singleShot(0, self.jobsChanged.emit)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    # ---------------- 后台工作核心流程 ----------------

    def _run_one_step(self, job: dict) -> str:
        cfg, st = job, job["state"]
        heartbeat = max(1, int(cfg.get("heartbeat_min") or 30))
        st["last_check_at"] = _now_iso()
        self._log(cfg["id"], "▶ 开始执行检查（"
                  + ("持续监测" if cfg.get("mode") == "watch" else "历史回补")
                  + f"，心跳 {heartbeat} 分钟）")

        extent = cfg.get("extent")
        if not extent or len(extent) != 4:
            return self._finish_step(cfg, "监测区域无效，已跳过", heartbeat * 60)
        wgs = self._to_wgs84(extent, cfg.get("extent_crs") or "EPSG:4326")
        if not wgs:
            return self._finish_step(cfg, "坐标转换失败，已跳过", heartbeat * 60)

        cursor_date = st.get("cursor_date") or ""
        cursor_scene = st.get("cursor_scene") or ""
        is_watch = cfg.get("mode") == "watch"

        # 1. 探测影像
        try:
            scene_date, scene_id, item = self._probe_scene(
                cfg.get("source", "s2"), wgs, cfg.get("pol", "vv"),
                cursor_date=cursor_date, is_watch=is_watch
            )
        except Exception as exc:
            st["last_error"] = f"STAC 检索失败: {exc}"
            self._log(cfg["id"], f"⚠️ STAC 检索失败: {exc}（120 秒后重试）")
            return self._finish_step(cfg, "⚠️ STAC 接口暂时不可用，稍后重试", 120)

        # 2. 检查是否有新景（核心去重机制）
        if not scene_id or not scene_date:
            if not is_watch:
                return self._stop_backfill(cfg, st)
            self._log(cfg["id"], "🔎 未检索到新影像")
            return self._finish_step(cfg, f"暂无新影像（最新检查: {st['last_check_at']}）", heartbeat * 60)

        # 若获取到的影像 ID 和已处理的一致，直接跳过，绝不再下载
        if scene_id == cursor_scene:
            if not is_watch:
                return self._stop_backfill(cfg, st)
            msg = f"未发布新景（最新景 {scene_id} 已处理完毕，静默待命中）"
            self._log(cfg["id"], f"🔎 最新景 {scene_id} 已处理过，跳过")
            return self._finish_step(cfg, msg, heartbeat * 60)

        # 3. 发现全新景，后台静默下载并处理（不加载到 QGIS 地图）
        logger.info("后台拉取全新影像: %s (%s)", scene_id, scene_date)
        self._log(cfg["id"], f"⬇ 发现新景 {scene_id}（{scene_date}），开始静默下载…")
        try:
            layer_file = self._fetch_scene_silent(cfg, wgs, scene_date, scene_id, item)
        except Exception as exc:
            logger.exception("静默下载影像失败: %s", scene_id)
            st["last_error"] = str(exc)
            return self._finish_step(cfg, f"下载失败: {exc}（5分钟后重试）", 300)

        # 4. 驱动后台 Agent 进行计算研判；AI 未产出文件时用内置确定性分析兜底
        self._log(cfg["id"], f"📦 影像就绪: {layer_file}")
        work = self._ensure_workdir(cfg)
        before = self._snapshot_files(work)
        self._log(cfg["id"], "🤖 调用 AI 进行分析研判（多轮工具调用）…")
        summary = self._delegate_to_copilot_silent(cfg, scene_date, scene_id, layer_file, st.get("last_file"))
        self._log(cfg["id"], "✅ AI 研判完成")
        produced = [f for f in self._snapshot_files(work) if f not in before]
        if not produced:
            self._log(cfg["id"], "📁 AI 未生成结果文件，改用内置确定性分析兜底…")
            produced, fb_note = self._analyze_fallback(cfg, layer_file, work)
            if fb_note:
                summary = summary + "\n\n" + fb_note
        if produced:
            self._log(cfg["id"], "📁 已生成结果文件: "
                      + "、".join(os.path.basename(f) for f in produced))
        else:
            self._log(cfg["id"], "⚠️ 本轮未生成任何结果文件（请检查 AI 简报）")
        st["last_file"] = layer_file

        record = {
            "scene_date": scene_date,
            "scene_id": scene_id,
            "summary": summary,
            "file": layer_file,
            "pushed_at": "",
        }
        pushed_err = ""
        if cfg.get("webhook"):
            pushed_err = push_markdown(cfg["webhook"], summary) or ""
            record["pushed_at"] = _now_iso()
            if pushed_err:
                st["last_error"] = f"推送失败: {pushed_err}"

        st["outputs"].append(record)
        st["cursor_date"] = scene_date
        st["cursor_scene"] = scene_id  # 锁定此 ID，下次遇到直接跳过
        st["last_result"] = summary

        delay_sec = 10 if not is_watch else heartbeat * 60
        return self._finish_step(cfg, summary, delay_sec, keep_error=pushed_err)

    def _finish_step(self, cfg: dict, msg: str, delay_sec: int, keep_error: str = "") -> str:
        st = cfg["state"]
        st["last_result"] = msg
        st["next_run_at"] = time.time() + delay_sec
        if keep_error:
            st["last_error"] = keep_error
        return msg

    def _stop_backfill(self, cfg: dict, st: dict) -> str:
        done = f"✅ 历史回补完毕（已推进至 {st.get('cursor_date') or '最新'}），已自动停止"
        self._log(cfg.get("id", ""), done)
        st["status"] = "paused"
        st["next_run_at"] = 0
        st["last_result"] = done
        return done

    # ---------------- STAC 检索（区分从现在开始 vs 回补） ----------------

    def _probe_scene(self, source: str, wgs: List[float], pol: str,
                     cursor_date: str = "", is_watch: bool = True) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
        """
        - is_watch: 始终获取最新一景（sortby: desc）
        - backfill: 从游标日期往后顺序获取（sortby: asc）
        """
        now = datetime.now().strftime("%Y-%m-%d")
        if is_watch:
            dt_range = f"2023-01-01T00:00:00Z/{now}T23:59:59Z"
            sort_dir = "desc"
        else:
            low = cursor_date or "2020-01-01"
            try:
                low = (datetime.strptime(low, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            except Exception:
                low = "2020-01-01"
            if low > now:
                return None, None, None
            dt_range = f"{low}T00:00:00Z/{now}T23:59:59Z"
            sort_dir = "asc"

        wgs = [max(-180.0, min(180.0, wgs[0])),
               max(-90.0, min(90.0, wgs[1])),
               max(-180.0, min(180.0, wgs[2])),
               max(-90.0, min(90.0, wgs[3]))]

        if source == "s1":
            payload = {
                "collections": ["sentinel-1-grd"],
                "bbox": wgs,
                "datetime": dt_range,
                "sortby": [{"field": "properties.datetime", "direction": sort_dir}],
                "limit": 10,
            }
            resp = _http().post(STAC_MPC_SEARCH, json=payload, timeout=15)
        else:
            payload = {
                "collections": ["sentinel-2-l2a"],
                "bbox": wgs,
                "datetime": dt_range,
                "filter-lang": "cql2-json",
                "filter": {"op": "<", "args": [{"property": "eo:cloud_cover"}, 50]},
                "sortby": [{"field": "properties.datetime", "direction": sort_dir}],
                "limit": 10,
            }
            resp = _http().post(STAC_AWS_SEARCH, json=payload, timeout=15)

        if resp.status_code != 200:
            raise RuntimeError(f"STAC 返回异常 HTTP {resp.status_code}")

        features = resp.json().get("features", []) or []
        for feat in features:
            props = feat.get("properties", {})
            dt = (props.get("datetime") or "")[:10]
            fid = feat.get("id", "")
            if source == "s1":
                assets = feat.get("assets", {})
                if not any(k in assets for k in ("vv", "vh", "hh", "hv")):
                    continue
            if dt and fid:
                return dt, fid, feat
        return None, None, None

    # ---------------- 静默下载（纯文件级操作，不注册 Layer） ----------------

    def _fetch_scene_silent(self, cfg: dict, wgs: List[float], scene_date: str, scene_id: str, item: dict) -> str:
        from osgeo import gdal
        source = cfg.get("source", "s2")
        work = self._ensure_workdir(cfg)
        tag = f"{cfg.get('id')}_{scene_date}_{scene_id[:8]}"

        if source == "s1":
            assets = item.get("assets", {})
            pol = cfg.get("pol", "vv")
            href = None
            for key in (pol, "vv", "vh", "hh", "hv"):
                if assets.get(key) and assets[key].get("href"):
                    href = assets[key]["href"]
                    pol = key
                    break
            if not href:
                raise RuntimeError("未找到有效 S1 资产")
            signed = self._sign_mpc(href)
            res = self._target_res(wgs, 900)
            tif_path = os.path.join(work, f"{tag}_S1_{pol}.tif")
            if not os.path.exists(tif_path):
                self._warp_url(signed, tif_path, wgs, res)
            return tif_path

        # Sentinel-2: B02, B03, B04, B08 (构建轻量核心多波段)
        s2_roles = [
            ("B02", ["blue", "B02", "b02"]),
            ("B03", ["green", "B03", "b03"]),
            ("B04", ["red", "B04", "b04"]),
            ("B08", ["nir", "nir08", "B08", "b08"]),
        ]
        assets = item.get("assets", {})
        res = self._target_res(wgs, 1000)
        single_band_files = []

        for b_name, aliases in s2_roles:
            url = self._asset_href(assets, aliases)
            if not url:
                continue
            b_path = os.path.join(work, "raw", f"{tag}_{b_name}.tif")
            if not os.path.exists(b_path):
                self._warp_url(url, b_path, wgs, res)
            single_band_files.append(b_path)

        if not single_band_files:
            raise RuntimeError("未成功获取 Sentinel-2 波段")

        multiband_vrt = os.path.join(work, f"{tag}_S2_Composite.vrt")
        gdal.BuildVRT(multiband_vrt, single_band_files, separate=True)
        return multiband_vrt

    # ---------------- 驱动后台 Agent 研判 ----------------

    def _delegate_to_copilot_silent(self, cfg: dict, scene_date: str, scene_id: str,
                                    cur_file: str, prev_file: Optional[str]) -> str:
        prompt_goal = (cfg.get("prompt") or "").strip() or "全要素地物特征提取与变化检测"
        threshold = cfg.get("threshold", 0.0)

        sys_task = (
            f"【后台静默遥感监测任务】\n"
            f"- 任务: {cfg.get('name')}\n"
            f"- 影像时相: {scene_date} (ID: {scene_id})\n"
            f"- 影像文件路径: {cur_file}\n"
        )
        if prev_file:
            sys_task += f"- 对比基准（前一时相）文件: {prev_file}\n"
        work = self._ensure_workdir(cfg)
        sys_task += (
            f"- 监测要求: {prompt_goal}\n"
            f"- 阈值判定: {threshold}\n"
            f"- 输出目录: {work}\n\n"
            "约束：后台静默模式只允许做文件级处理（gdal/numpy/processing 输出到文件）。\n"
            "严禁调用任何 QGIS 图层/工程/画布接口（如 addMapLayer、iface、canvas、QgsProject 加载图层）。\n"
            "读取影像请使用 osgeo.gdal 直接打开给定文件路径；如需 NDVI/植被提取请基于文件波段计算并输出 GeoTIFF；\n"
            "若需转为矢量（shp），将结果保存到上述输出目录并给出完整文件路径。最后输出一份结构化监测简报（含生成的文件路径）。"
        )

        ai_summary, err = run_copilot_agent(sys_task, active_layers=[], max_turns=4)

        prefix = (
            f"📡 **[{cfg.get('name')}] 遥感监测通报**\n"
            f"- 影像日期: {scene_date}\n"
            f"- 景标识: `{scene_id}`\n"
            f"- 目标: {prompt_goal}\n\n"
        )
        if ai_summary:
            return prefix + f"🤖 **AI 分析结果**：\n{ai_summary}"
        return prefix + f"⚠️ AI 处理完毕（{err or '无额外说明'}）"

    # ---------------- 基础工具 ----------------

    # ---------------- 文件快照 / 确定性兜底分析 ----------------

    @staticmethod
    def _snapshot_files(work: str) -> set:
        """递归列出工作目录下的相对路径集合（用于判断 AI 是否产出新文件）。"""
        out = set()
        try:
            for root, _dirs, names in os.walk(work):
                for n in names:
                    out.add(os.path.relpath(os.path.join(root, n), work))
        except Exception:
            pass
        return out

    @staticmethod
    def _read_band(path: str, band: int = 1):
        """读取指定波段为 float32 数组；失败返回 None。"""
        import numpy as np
        from osgeo import gdal
        try:
            ds = gdal.Open(path)
            if ds is None:
                return None
            arr = ds.GetRasterBand(band).ReadAsArray()
            ds = None
            return np.asarray(arr, dtype="float32")
        except Exception:
            return None

    @staticmethod
    def _write_tif_like(arr, ref_path: str, out_path: str, nodata=None):
        """按参考影像的几何/投影把数组写成 GeoTIFF（dtype 由 arr 决定）。"""
        from osgeo import gdal
        ref = gdal.Open(ref_path)
        if ref is None:
            raise RuntimeError("无法读取参考影像几何: " + ref_path)
        rows, cols = arr.shape
        drv = gdal.GetDriverByName("GTiff")
        gdt = gdal.GDT_Byte if arr.dtype == "uint8" else gdal.GDT_Float32
        ds = drv.Create(out_path, cols, rows, 1, gdt,
                        options=["COMPRESS=DEFLATE", "TILED=YES"])
        ds.SetGeoTransform(ref.GetGeoTransform())
        ds.SetProjection(ref.GetProjection())
        band = ds.GetRasterBand(1)
        band.WriteArray(arr)
        if nodata is not None:
            band.SetNoDataValue(nodata)
        ds = None
        ref = None

    @staticmethod
    def _polygonize_mask(mask_path: str, shp_path: str) -> int:
        """把 0/1 掩膜中值为 1 的连通区矢量化；返回图斑数。"""
        from osgeo import gdal, ogr, osr
        try:
            src = gdal.Open(mask_path)
            if src is None:
                return 0
            drv = ogr.GetDriverByName("ESRI Shapefile")
            if os.path.exists(shp_path):
                drv.DeleteDataSource(shp_path)
            dst = drv.CreateDataSource(shp_path)
            srs = osr.SpatialReference()
            srs.ImportFromWkt(src.GetProjection())
            layer = dst.CreateLayer("target", srs=srs, geom_type=ogr.wkbPolygon)
            fd = ogr.FieldDefn("DN", ogr.OFTInteger)
            layer.CreateField(fd)
            gdal.Polygonize(src.GetRasterBand(1), None, layer, 0, [], callback=None)
            # 仅保留 DN==1（目标区）
            keep = 0
            layer.SetAttributeFilter("DN = 1")
            # 反向删除：先收集需删除的 FID
            del_fids = []
            layer.SetAttributeFilter(None)
            for feat in layer:
                if feat.GetField("DN") != 1:
                    del_fids.append(feat.GetFID())
            for fid in del_fids:
                layer.DeleteFeature(fid)
            keep = layer.GetFeatureCount()
            dst = None
            src = None
            if keep == 0:
                drv.DeleteDataSource(shp_path)
            return keep
        except Exception:
            return 0

    def _analyze_fallback(self, cfg: dict, layer_file: str, work: str):
        """AI 未产出文件时的内置确定性分析（保证工作目录有结果）。

        光学(S2 四波段复合: B02,B03,B04,B08) → NDVI 或 NDWI + 目标区掩膜，
        提示词含“转shp/矢量化”时再转 Shapefile；雷达 → 与上一景振幅差分。
        返回 (生成文件列表, 附加说明)。
        """
        import numpy as np
        prompt = (cfg.get("prompt") or "").lower()
        threshold = float(cfg.get("threshold") or 0.0)
        tag = os.path.splitext(os.path.basename(layer_file))[0]
        files = []
        notes = []

        if cfg.get("source", "s2") == "s1":
            prev = cfg["state"].get("last_file")
            cur = self._read_band(layer_file)
            if cur is None:
                return [], "⚠️ 兜底分析无法读取雷达影像"
            if not prev or not os.path.isfile(prev):
                notes.append("- 首景已就绪，作为基准；待下一景到达后进行差分检测")
                return files, "\n".join(notes)
            base = self._read_band(prev)
            if base is None or base.shape != cur.shape:
                notes.append("- 与上一景网格不一致，已更新基准")
                return files, "\n".join(notes)
            th = threshold if threshold else 0.15
            norm = np.abs((cur - base) / (np.abs(base) + 1e-6))
            change = norm >= th
            out_tif = os.path.join(work, f"{tag}_变化疑似区.tif")
            self._write_tif_like(change.astype("uint8"), layer_file, out_tif)
            files.append(out_tif)
            pct = 100.0 * change.sum() / max(1, change.size)
            notes.append(f"- 兜底分析（振幅差分）: 变化占比 {pct:.2f}%（阈值 {th}）")
            return files, "\n".join(notes)

        # 光学 NDVI / NDWI（复合波段序: 1=B02 2=B03 3=B04 4=B08）
        b_green = self._read_band(layer_file, 2)
        b_red = self._read_band(layer_file, 3)
        b_nir = self._read_band(layer_file, 4)
        if b_green is None or b_red is None or b_nir is None:
            return [], "⚠️ 兜底分析无法读取光学复合影像波段"
        if any(k in prompt for k in ("水体", "水域", "ndwi", "提取水")):
            idx_name = "NDWI"
            with np.errstate(divide="ignore", invalid="ignore"):
                arr = np.where(b_green + b_nir != 0,
                               (b_green - b_nir) / (b_green + b_nir), -9999.0)
        else:
            idx_name = "NDVI"
            with np.errstate(divide="ignore", invalid="ignore"):
                arr = np.where(b_red + b_nir != 0,
                               (b_nir - b_red) / (b_nir + b_red), -9999.0)
        arr = np.nan_to_num(arr, nan=-9999.0).astype("float32")
        th = threshold if threshold else (0.3 if idx_name == "NDVI" else 0.0)
        valid = arr > -9000.0
        hit = (arr >= th) & valid

        idx_tif = os.path.join(work, f"{tag}_{idx_name}.tif")
        mask_tif = os.path.join(work, f"{tag}_{idx_name}目标区.tif")
        self._write_tif_like(arr, layer_file, idx_tif, nodata=-9999)
        self._write_tif_like(hit.astype("uint8"), layer_file, mask_tif)
        files += [idx_tif, mask_tif]
        pct = 100.0 * hit.sum() / max(1, valid.sum())
        mean = float(arr[valid].mean()) if valid.any() else float("nan")
        notes.append(f"- 兜底分析 {idx_name}: 指数均值 {mean:.3f}，"
                     f"目标区(≥{th})占比 {pct:.1f}%")
        if any(k in prompt for k in ("转shp", "矢量化", "shapefile", "polygon", "shp")):
            shp_path = os.path.join(work, f"{tag}_{idx_name}目标区.shp")
            n = self._polygonize_mask(mask_tif, shp_path)
            if n:
                files.append(shp_path)
                notes.append(f"- 已矢量化 {n} 个图斑 → {os.path.basename(shp_path)}")
            else:
                notes.append("- 无 ≥阈值的连通图斑，未生成 SHP")
        return files, "\n".join(notes)

    def _ensure_workdir(self, cfg: dict) -> str:
        work = cfg.get("work_dir") or os.path.join(
            QgsApplication.qgisSettingsDirPath(), "geomind_ai", "monitor", cfg.get("id", "job")
        )
        os.makedirs(work, exist_ok=True)
        os.makedirs(os.path.join(work, "raw"), exist_ok=True)
        return work

    @staticmethod
    def _asset_href(assets: dict, aliases) -> Optional[str]:
        for key in aliases:
            a = assets.get(key)
            if a and a.get("href"):
                return a["href"]
        return None

    def _sign_mpc(self, href: str) -> str:
        try:
            resp = _http().get(MPC_SIGN_URL, params={"href": href}, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("href", href)
        except Exception:
            pass
        return href

    @staticmethod
    def _to_wgs84(extent, crs_authid: str) -> Optional[List[float]]:
        """将任意 CRS 的 extent [xmin, ymin, xmax, ymax] 安全转换为 WGS84 [min_lon, min_lat, max_lon, max_lat]"""
        try:
            if not extent or len(extent) != 4:
                return None

            xmin, ymin, xmax, ymax = [float(v) for v in extent]
            crs_authid = (crs_authid or "EPSG:4326").strip()

            # 如果已经是 4326 则直接规范化返回
            if crs_authid.upper() in ("EPSG:4326", "WGS84"):
                min_lon, max_lon = min(xmin, xmax), max(xmin, xmax)
                min_lat, max_lat = min(ymin, ymax), max(ymin, ymax)
                return [max(-180.0, min(180.0, min_lon)),
                        max(-90.0, min(90.0, min_lat)),
                        max(-180.0, min(180.0, max_lon)),
                        max(-90.0, min(90.0, max_lat))]

            # 优先采用 QGIS 原生转换（传入 QgsProject 上下文）
            try:
                from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsPointXY
                src = QgsCoordinateReferenceSystem(crs_authid)
                dst = QgsCoordinateReferenceSystem("EPSG:4326")
                if src.isValid() and dst.isValid():
                    ctx = QgsProject.instance().transformContext()
                    tr = QgsCoordinateTransform(src, dst, ctx)
                    # 转换 4 个角点避免跨投影外包框变形
                    pts = [
                        tr.transform(QgsPointXY(xmin, ymin)),
                        tr.transform(QgsPointXY(xmin, ymax)),
                        tr.transform(QgsPointXY(xmax, ymin)),
                        tr.transform(QgsPointXY(xmax, ymax)),
                    ]
                    lons = [p.x() for p in pts]
                    lats = [p.y() for p in pts]
                    return [
                        max(-180.0, min(180.0, min(lons))),
                        max(-90.0, min(90.0, min(lats))),
                        max(-180.0, min(180.0, max(lons))),
                        max(-90.0, min(90.0, max(lats)))
                    ]
            except Exception as qgis_err:
                logger.warning("QGIS 坐标转换异常，改用 GDAL/OSR 兜底: %s", qgis_err)

            # 兜底：使用 osgeo.osr 进行纯数据转换（后台线程完全安全）
            from osgeo import osr
            src_srs = osr.SpatialReference()
            if src_srs.SetFromUserInput(crs_authid) != 0:
                return None
            dst_srs = osr.SpatialReference()
            dst_srs.ImportFromEPSG(4326)
            # 确保采用传统长宽比/XY 顺序
            if hasattr(src_srs, "SetAxisMappingStrategy"):
                src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if hasattr(dst_srs, "SetAxisMappingStrategy"):
                dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

            ct = osr.CoordinateTransformation(src_srs, dst_srs)
            pts = [
                ct.TransformPoint(xmin, ymin),
                ct.TransformPoint(xmin, ymax),
                ct.TransformPoint(xmax, ymin),
                ct.TransformPoint(xmax, ymax),
            ]
            lons = [p[0] for p in pts]
            lats = [p[1] for p in pts]

            return [
                max(-180.0, min(180.0, min(lons))),
                max(-90.0, min(90.0, min(lats))),
                max(-180.0, min(180.0, max(lons))),
                max(-90.0, min(90.0, max(lats)))
            ]
        except Exception as exc:
            logger.exception("坐标转 WGS84 失败: %s", exc)
            return None

    @staticmethod
    def _target_res(wgs: List[float], max_side: int) -> float:
        span = max(wgs[2] - wgs[0], wgs[3] - wgs[1], 1e-6)
        return span / max_side

    def _warp_url(self, url: str, out_path: str, wgs: List[float], res: float) -> None:
        from osgeo import gdal
        gdal.Warp(
            out_path, f"/vsicurl/{url}",
            format="GTiff", outputBounds=wgs,
            outputBoundsSRS="EPSG:4326", dstSRS="EPSG:4326",
            xRes=res, yRes=res,
            resampleAlg="bilinear",
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "NUM_THREADS=ALL_CPUS"],
        )