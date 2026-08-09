# -*- coding: utf-8 -*-
"""
GeoMind AI - 后台任务调度模块
使用 QGIS 原生 QgsTask 替代 QThread，实现线程安全、状态栏进度显示与随时取消功能[cite: 1]。
"""

import os
import time
import traceback
from qgis.core import (
    QgsTask,
    QgsMessageLog,
    Qgis,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsProject
)


class GeoMindInferenceTask(QgsTask):
    """
    GeoMind AI 遥感推理与提取任务 (基于 QgsTask)[cite: 1]
    """
    def __init__(self, description, input_raster, output_path, params=None):
        """
        初始化任务
        :param description: 任务描述（将显示在 QGIS 右下角状态栏任务管理器中）
        :param input_raster: 输入的栅格图像路径[cite: 1]
        :param output_path: 输出结果保存路径 (如 GeoJSON, SHP, GeoTIFF)
        :param params: 算法与推理模型参数字典
        """
        super().__init__(description, QgsTask.CanCancel)
        self.input_raster = input_raster
        self.output_path = output_path
        self.params = params or {}
        
        # 结果与异常记录
        self.result_data = None
        self.exception_msg = ""

    def run(self):
        """
        【后台子线程运行】执行耗时的遥感图像切片、模型推理与矢量化等逻辑[cite: 1]。
        ⚠️ 注意：在此方法中绝不可直接操作 QgsMapCanvas、QMessageBox 或直接向项目添加图层！
        """
        QgsMessageLog.logMessage(f"开始执行 GeoMind AI 任务: {self.description()}", "GeoMind AI", Qgis.Info)
        
        try:
            # 1. 检查输入路径
            if not os.path.exists(self.input_raster):
                raise FileNotFoundError(f"未找到输入栅格文件: {self.input_raster}")
            
            # 2. 模拟切片推理过程（例如分 100 个瓦片/步骤处理）
            total_tiles = 100
            
            for i in range(total_tiles):
                # 【核心】检查用户是否在 QGIS 界面点击了取消按钮 [X]
                if self.isCanceled():
                    QgsMessageLog.logMessage("用户中断了 GeoMind AI 推理任务", "GeoMind AI", Qgis.Warning)
                    return False

                # -------------------------------------------------------------
                # 此处插入真实的遥感切片/AI 推理逻辑，如 ONNX/PyTorch/Rasterio 操作
                # -------------------------------------------------------------
                time.sleep(0.05)  # 模拟单个切片推理耗时

                # 更新 QGIS 状态栏进度条 (0 - 100)
                progress = int(((i + 1) / total_tiles) * 100)
                self.setProgress(progress)

            # 3. 推理完成，组装输出结果
            self.result_data = {
                "output_path": self.output_path,
                "tile_count": total_tiles,
                "status": "success"
            }
            
            return True

        except Exception as e:
            self.exception_msg = str(e)
            QgsMessageLog.logMessage(
                f"GeoMind AI 任务出错: {self.exception_msg}\n{traceback.format_exc()}", 
                "GeoMind AI", 
                Qgis.Critical
            )
            return False

    def finished(self, result):
        """
        【主 UI 线程运行】任务结束（成功、失败或被取消）后自动触发。
        可以在此方法中安全地向 QGIS 画布叠加图层或弹出提示[cite: 1]。
        
        :param result: run() 方法的返回值 (True/False)
        """
        if result and self.result_data:
            QgsMessageLog.logMessage(
                f"GeoMind AI 推理完成！输出文件: {self.result_data['output_path']}", 
                "GeoMind AI", 
                Qgis.Success
            )
            # 自动将生成的结果加载到 QGIS 图层画布中[cite: 1]
            self._add_result_to_canvas(self.result_data["output_path"])

        elif self.isCanceled():
            QgsMessageLog.logMessage("GeoMind AI 任务已成功取消", "GeoMind AI", Qgis.Info)

        else:
            QgsMessageLog.logMessage(
                f"GeoMind AI 任务未成功完成。错误信息: {self.exception_msg}", 
                "GeoMind AI", 
                Qgis.Critical
            )

    def cancel(self):
        """用户在 QGIS 界面点击取消时触发"""
        QgsMessageLog.logMessage("正在发送中断请求...", "GeoMind AI", Qgis.Info)
        super().cancel()

    def _add_result_to_canvas(self, layer_path):
        """
        辅助方法：安全的将结果叠加进 QGIS 图层树（主线程）
        """
        if not os.path.exists(layer_path):
            return

        layer_name = os.path.basename(layer_path).rsplit('.', 1)[0] + "_GeoMind_Result"
        
        # 判断是矢量还是栅格文件
        if layer_path.endswith(('.geojson', '.shp', '.gpkg')):
            vlayer = QgsVectorLayer(layer_path, layer_name, "ogr")
            if vlayer.isValid():
                QgsProject.instance().addMapLayer(vlayer)
        elif layer_path.endswith(('.tif', '.tiff', '.img')):
            rlayer = QgsRasterLayer(layer_path, layer_name)
            if rlayer.isValid():
                QgsProject.instance().addMapLayer(rlayer)
