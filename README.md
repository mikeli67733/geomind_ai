# GeoMind AI

> 基于 AI 大模型的遥感影像智能解译 QGIS 插件
> AI Copilot 自然语言对话 · 10 大本地免费遥感/GIS 工具 · 8 大深度解译模型

GeoMind AI 将大模型驱动的自然语言交互与专业遥感解译能力整合进 QGIS，覆盖从"栅格预处理 → 智能解译 → 矢量化 → 统计制图"的完整工作流。插件同时提供**本地免费算法工具**（不依赖网络）与**云端 AI 解译服务**（需登录授权）。

---

## 功能特性

### 1. AI Copilot 自然语言对话
- 基于 SSE 流式输出的对话式操作终端
- 支持自然语言触发本地算法（光谱指数、DEM 分析、滤波、聚类等）与云端 AI 任务
- 展示思考过程 / 工具调用 / 最终结论的分层渲染
- 内置 QGIS 工具语义检索（向量索引 + 本地词袋兜底）

### 2. 10 大本地免费工具（离线可用）
| 工具 | 说明 |
| --- | --- |
| 全能光谱指数库 | NDVI/NDWI/NDBI 等指数一键计算 |
| PCA 主成分分析 | 多波段降维与特征提取 |
| DEM 地形全要素分析 | 坡度/坡向/山体阴影/等高线等 |
| 空间滤波与边缘提取 | 均值/高斯/中值/边缘增强 |
| 地物分类面积统计 | 分类结果面积/占比统计 |
| 矢量图斑化简平滑 | 拓扑保持的化简与平滑 |
| K-Means 智能聚类 | 无监督影像分割 |
| 双期像元差分检测 | 两期影像变化区域探测 |
| 假彩色画质增强 | 多波段组合与线性拉伸 |
| 栅格一键矢量化 | 栅格转面要素（含聚合/平滑） |

### 3. 8 大 AI 深度解译模型（云端）
土地利用全要素解译、建筑物/道路/水系/林草/农田五类专项提取、SAM3 交互提示解译、深度双期变化检测。

### 4. 账号与套餐体系
- 注册/登录/改密，Token 持久化与自动恢复登录
- 免费版（每日 20 次）/ 包月会员（99 元/30 天）/ 定制私有化部署
- 卡密兑换、账号配额实时刷新

---

## 安装

### 环境要求
- QGIS ≥ 3.16（推荐 3.28 LTR 及以上）
- Python 依赖：`requests`、`numpy`、GDAL（QGIS 自带）

### 方式一：插件目录安装
1. 将 `geomind_ai` 目录复制到 QGIS 插件目录：
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
2. 重启 QGIS，在「插件 → 管理并安装插件」中勾选启用

### 方式二：ZIP 安装
从 [Releases](https://github.com/mikeli67733/geomind_ai/releases) 下载 `geomind_ai.zip`，在 QGIS「管理并安装插件 → 从 ZIP 安装」中选择该文件。

---

## 快速使用

1. 点击工具栏「遥感影像智能解译助手」按钮打开 Dock 面板
2. **登录**：首次使用点击右上角「设置」→ 注册/登录（自动开通免费版每日 20 次配额）
3. **AI 对话**：在 Copilot 输入框用自然语言描述需求，如"对当前影像计算 NDVI"
4. **专项工具**：点击输入框左下「工具」菜单，或直接使用 Copilot 触发，进入对应工具页配置参数
5. **云端解译**：加载影像 → 框选范围 → 选择模型 → 运行，结果自动加载到地图

---

## 开发指南

### 新增一个本地工具
1. 在 `core/algos.py`（纯算法）或 `tools/raster_ops.py` / `vector_ops.py`（GDAL I/O）中实现算法
2. 在 `ui/local_tools/` 下新建模块，继承 `BaseLocalToolWidget` 编写配置页
3. 在 `ui/local_tools/registry.py` 的 `LOCAL_TOOL_PAGES` 中登记一行 `(page_key, 标题, WidgetClass)`，Dock 导航自动生效
4. 如需 Copilot 自然语言触发，在 `tools/skill_dispatcher.py` 中登记技能


### 运行测试（开发环境）
```bash
pip install -r requirements-dev.txt   # pytest / numpy / requests
python -m pytest tests/ -v
```

### 代码规范
- 所有日志统一走 `core.logger.get_logger(__name__)`
- 业务异常统一抛 `core.exceptions` 中的领域异常
- 网络请求一律经 `api.http_client`（禁止裸 `requests`）
- 配置读取统一走 `core.config.settings` 门面
- Qt 枚举一律使用 `core.compat` 中的兼容常量
- UI 样式复用 `ui.theme` 中的 QSS 常量

---

## 更新日志

### 3.6 及更早
- 历史版本，见 GitHub Releases。

---

## 许可证

[LICENSE](LICENSE) © Mike Li

> 注意：插件本体为开源代码；云端 AI 解译能力为商业服务，需注册账号并遵守对应服务条款。
