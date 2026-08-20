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

## 架构设计

v4.0 起按**六层包结构**组织；v5.0 进一步引入配置门面、统一 HTTP 传输层、页面注册表与纯算法核，职责更清晰、可测试、可扩展：

```
geomind_ai/
├── __init__.py              # QGIS classFactory 入口
├── plugin_main.py           # 插件入口：菜单/工具栏注册、Dock 生命周期与状态持久化
├── metadata.txt / plugins.xml / pyproject.toml
│
├── core/                    # 核心基础设施（无 QGIS 强依赖，可单测）
│   ├── config.py            # 配置门面：server_url 懒加载 + 统一配置源链
│   ├── algos.py             # 纯算法核（光谱指数/滤波/面积统计/K-Means，零 QGIS 依赖）
│   ├── logger.py            # 统一日志系统（get_logger / log_to_qgis）
│   ├── exceptions.py        # 领域异常体系（GeoMindError 基类 + 5 个派生异常）
│   ├── compat.py            # PyQt5/6、QGIS 3.16+ 版本兼容常量
│   └── constants.py         # 全局常量：API 路径、模型定义、套餐、QSettings 键
│
├── api/                     # 后端服务客户端（统一走 http_client 传输层）
│   ├── http_client.py       # 统一 HTTP 客户端：超时/指数退避重试/Token 注入/错误归一化
│   ├── auth_client.py       # 注册/登录/改密/用户信息
│   ├── task_client.py       # 解译任务提交/轮询/结果/取消
│   └── copilot_task.py      # Copilot SSE 流式对话（QgsTask 后台执行）
│
├── tasks/                   # QGIS 后台任务
│   └── interpret_task.py    # 云端解译任务（可取消、进度上报）
│
├── ui/                      # 界面层
│   ├── theme.py             # 统一 QSS 主题与色板
│   ├── dock_widget.py       # 主容器：导航栏 + QStackedWidget 页面栈 + 状态持久化
│   ├── copilot_widget.py    # AI 对话主页
│   ├── base_task_widget.py  # AI 任务基类 + 4 个解译专项页
│   ├── local_tools/         # 10 个本地工具页（每工具一模块）
│   │   ├── registry.py      # 页面注册表/工厂：新增工具只需登记一行
│   │   ├── base.py          # BaseLocalToolWidget 基类
│   │   └── spectral_index.py / pca.py / dem.py / filter.py / area_stats.py /
│   │       vector_smooth.py / kmeans.py / raster_diff.py / enhance.py / polygonize.py
│   ├── local_tool_widgets.py# v5.0 兼容转发层（旧导入路径可用）
│   ├── account_page.py      # 账号中心
│   ├── login_dialog.py      # 登录/注册/改密
│   └── plan_dialog.py       # 套餐与卡密兑换
│
├── tools/                   # 共享处理逻辑（UI 与 Copilot 技能共用，消除重复）
│   ├── raster_ops.py        # 栅格算法（调用 core.algos 纯算法核 + GDAL I/O）
│   ├── vector_ops.py        # 矢量化简平滑
│   └── skill_dispatcher.py  # Copilot 技能分发（统一 HTTP 层处理天地图请求）
│
├── utils/                   # 通用工具
│   ├── machine_id.py        # 机器码
│   ├── raster_clip.py       # 栅格裁剪
│   ├── extent_tool.py       # 地图范围框选工具
│   └── qgis_indexer.py      # QGIS 工具语义索引（单例）
│
└── tests/                   # pytest 单测（配置懒加载/纯算法核/HTTP 客户端 mock/异常体系）
```

### 设计原则
- **单一职责**：每个模块只做一件事；原 2383 行单体 `dockwidget.py` 已拆分为专业化 UI 模块，10 个本地工具页进一步按工具拆分
- **消除重复**：栅格算法抽为 `core/algos.py` 纯算法核，UI 与 Copilot 技能统一调用；网络请求统一收敛到 `api/http_client.py`
- **面向异常**：`TokenExpiredError` / `QuotaExhaustedError` / `TaskCancelledError` 等可捕获、可提示
- **启动零副作用**：配置懒加载，导入插件不发网络请求；首次访问 `settings.server_url` 才解析
- **可测试**：`core/algos.py`、`core/config.py`、`api/http_client.py` 均脱离 QGIS 可单测
- **安全**：第三方 API 密钥移入环境变量，不再硬编码
- **兼容**：PyQt5/PyQt6 枚举差异收敛于 `core/compat.py`

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

## 配置

### 环境变量
| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `GEOMIND_TIANDITU_TK` | 天地图地理编码 API Key | 空（功能自动禁用） |

### 服务器地址解析链
`QSettings`（运行期覆盖）→ `server_config.json`（本地覆盖）→ `REMOTE_CONFIG_URLS`（远程配置拉取）→ `FALLBACK_SERVER_URL`（`http://127.0.0.1:8000`）。统一由 `core/config.py` 的 `settings.server_url()` 懒加载解析（带缓存与后台刷新），**导入插件不会触发网络请求**。

### 内置 LLM 对话配置
`DEFAULT_LLM_BASE_URL` / `DEFAULT_LLM_MODEL` 位于 `core/constants.py`，运行期可在账号设置页覆盖。

---

## 开发指南

### 新增一个本地工具
1. 在 `core/algos.py`（纯算法）或 `tools/raster_ops.py` / `vector_ops.py`（GDAL I/O）中实现算法
2. 在 `ui/local_tools/` 下新建模块，继承 `BaseLocalToolWidget` 编写配置页
3. 在 `ui/local_tools/registry.py` 的 `LOCAL_TOOL_PAGES` 中登记一行 `(page_key, 标题, WidgetClass)`，Dock 导航自动生效
4. 如需 Copilot 自然语言触发，在 `tools/skill_dispatcher.py` 中登记技能

### 新增一个云端模型
1. 在 `core/constants.py` 的 `MODELS` 中追加模型元组 `(显示名, model_key, mode)`
2. 在 `ui/base_task_widget.py` 中继承 `BaseTaskWidget` 实现参数页
3. 在 `ui/dock_widget.py` 中注册页面

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

### 5.0（架构级升级）
- 配置系统懒加载：新增 `core/config.py` 门面，导入期不再发网络请求（统一配置源链 QSettings > server_config.json > 远程 > 兜底）
- 统一 HTTP 客户端：新增 `api/http_client.py`（指数退避重试/统一超时/Token 注入/错误归一化），auth/task/copilot/天地图全部迁移
- 10 大本地工具页拆分为 `ui/local_tools/` 独立模块 + 页面注册表工厂，消除 762 行超大类
- 生命周期增强：Dock 可见性/位置/当前页持久化，重启 QGIS 自动恢复
- 纯算法核抽取 `core/algos.py`（零 QGIS 依赖）+ pytest 测试体系 + pyproject.toml（ruff/pytest 配置）

### 4.0（架构重构）
- 按 `core/api/tasks/ui/tools/utils` 六层包结构拆分原单文件代码
- 新增统一日志系统、领域异常体系、PyQt5/6 兼容层
- 消除 9 类栅格算法与矢量算法的重复实现（共享 `tools/` 层）
- 移除硬编码的天地图 API 密钥，改为环境变量注入
- 界面文案专业化、统一 QSS 主题
- 清理残留的测试占位配置

### 3.6 及更早
- 历史版本，见 GitHub Releases。

---

## 许可证

[LICENSE](LICENSE) © Mike Li

> 注意：插件本体为开源代码；云端 AI 解译能力为商业服务，需注册账号并遵守对应服务条款。
