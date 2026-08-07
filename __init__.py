# -*- coding: utf-8 -*-
"""
QGIS 插件入口文件
"""

def classFactory(iface):
    from .plugin_main import ImageInterpretPlugin
    return ImageInterpretPlugin(iface)