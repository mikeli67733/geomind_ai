# -*- coding: utf-8 -*-
"""
一键清理缓存、自动生成 LICENSE 并打包符合 QGIS 官方规范的 ZIP 插件包
（已完美支持绝对路径与 Windows/Linux 跨平台路径转换）
运行方法: python clean_pack.py
"""
import os
import shutil
import zipfile

# 你的插件绝对路径或相对路径
PLUGIN_DIR_PATH = r"C:\Users\Administrator\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\geomind_ai"
OUTPUT_ZIP_PATH = r"C:\Users\Administrator\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\geomind_ai.zip"

# 标准 GPL v2 许可证文本
GPL_LICENSE_TEXT = """GNU GENERAL PUBLIC LICENSE
Version 2, June 1991

Copyright (C) 2026 GeoMind AI Team

Everyone is permitted to copy and distribute verbatim copies
of this license document, but changing it is not allowed.

Preamble

The licenses for most software are designed to take away your
freedom to share and change it. By contrast, the GNU General Public
License is intended to guarantee your freedom to share and change free
software--to make sure the software is free for all its users.

This General Public License applies to most of the Free Software
Foundation's software and to any other program whose authors commit to
using it.
"""


def auto_create_license(plugin_path):
    """自动生成 QGIS 官方要求的 LICENSE 文件"""
    license_path = os.path.join(plugin_path, "LICENSE")
    if not os.path.exists(license_path):
        with open(license_path, "w", encoding="utf-8") as f:
            f.write(GPL_LICENSE_TEXT)
        print("📄 已自动为您生成标准的 LICENSE 许可证文件！")


def clean_and_zip():
    plugin_path = os.path.abspath(PLUGIN_DIR_PATH)
    parent_dir = os.path.dirname(plugin_path)

    if not os.path.exists(plugin_path):
        print(f"❌ 找不到文件夹: {plugin_path}，请检查路径是否正确")
        return

    # 1. 自动补充缺失的 LICENSE 文件
    auto_create_license(plugin_path)

    # 2. 彻底递归清理 __pycache__ 文件夹和 .pyc 文件
    for root, dirs, files in os.walk(plugin_path, topdown=False):
        for d in dirs:
            if d == "__pycache__":
                pycache_path = os.path.join(root, d)
                shutil.rmtree(pycache_path)
                print(f"🧹 已清理缓存目录: {pycache_path}")
        for f in files:
            if f.endswith(".pyc") or f.endswith(".pyo"):
                file_path = os.path.join(root, f)
                os.remove(file_path)

    # 3. 打包为 ZIP（关键修正：强制把路径中的 \ 替换为 Linux 标准的 /）
    zip_output_path = os.path.abspath(OUTPUT_ZIP_PATH)
    if os.path.exists(zip_output_path):
        os.remove(zip_output_path)

    with zipfile.ZipFile(zip_output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(plugin_path):
            for file in files:
                if file.startswith(".") or file.endswith(".pyc"):
                    continue
                file_path = os.path.join(root, file)
                # 计算相对父目录的路径，并强制将反斜杠 \ 转换为正斜杠 /
                rel_path = os.path.relpath(file_path, parent_dir)
                arcname = rel_path.replace('\\', '/')
                zipf.write(file_path, arcname)

    print(f"\n✅ 重新打包成功！高兼容性压缩包已生成: {zip_output_path}")


if __name__ == "__main__":
    clean_and_zip()