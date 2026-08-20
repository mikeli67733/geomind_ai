# -*- coding: utf-8 -*-
"""
Central QSS (Qt Style Sheet) theme for GeoMind AI.

Modern light SaaS style: soft slate background, white cards, brand-blue
gradient accents, generous rounded corners and clear typography hierarchy.
All UI components import from here to ensure visual consistency.
"""

# -- Color palette ----------------------------------------------------------

COLOR_BG = "#f4f6fa"          # dock 整体背景（淡灰蓝）
COLOR_SURFACE = "#ffffff"     # 卡片 / 表面
COLOR_PRIMARY = "#2563eb"     # 品牌蓝
COLOR_PRIMARY_LIGHT = "#3b82f6"
COLOR_PRIMARY_HOVER = "#1d4ed8"
COLOR_PRIMARY_SOFT = "#eff6ff"  # 品牌蓝浅底（选中态/链接底）
COLOR_TEXT = "#0f172a"          # 主文字
COLOR_TEXT_SECONDARY = "#334155"
COLOR_TEXT_MUTED = "#64748b"
COLOR_TEXT_FAINT = "#94a3b8"
COLOR_BORDER = "#d7dee8"        # 控件边框
COLOR_BORDER_LIGHT = "#e5eaf1"  # 卡片边框
COLOR_BORDER_FAINT = "#eef2f7"  # 分割线
COLOR_DANGER = "#dc2626"
COLOR_DANGER_BG = "#fef2f2"
COLOR_DANGER_BORDER = "#fecaca"
COLOR_SUCCESS = "#15803d"
COLOR_SUCCESS_LIGHT = "#ecfdf5"
COLOR_INFO_BG = "#eff6ff"
COLOR_INFO_BORDER = "#bfdbfe"
COLOR_INFO_TEXT = "#1d4ed8"

# -- Full QSS for the main dock container -----------------------------------
# 所有工具页 / 账号页 / 设置页都挂在 dockContainer 下，自动继承本样式。

MAIN_DOCK_QSS = """
QWidget#dockContainer { background-color: #f4f6fa; }

/* ---- 卡片式分组框 ---- */
QGroupBox {
    font-weight: 600; font-size: 13px;
    border: 1px solid #e5eaf1;
    border-radius: 12px;
    margin-top: 12px;
    padding-top: 16px;
    background-color: #ffffff;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px; top: 2px;
    padding: 0 8px;
    color: #1e293b;
}

/* ---- 通用按钮 ---- */
QPushButton {
    background-color: #ffffff;
    border: 1px solid #d7dee8;
    border-radius: 8px;
    padding: 7px 14px;
    color: #334155;
    font-weight: 500;
}
QPushButton:hover { background-color: #f1f5f9; border-color: #94a3b8; color: #0f172a; }
QPushButton:pressed { background-color: #e2e8f0; }
QPushButton:disabled { background-color: #f8fafc; color: #b6c2d4; border-color: #e5eaf1; }

/* ---- 主行动按钮（品牌蓝渐变） ---- */
QPushButton#runBtn {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #3b82f6, stop:1 #2563eb);
    color: #ffffff; border: none;
    border-radius: 8px;
    font-size: 13px; font-weight: 600;
    padding: 8px 16px;
}
QPushButton#runBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #2563eb, stop:1 #1d4ed8);
}
QPushButton#runBtn:pressed { background-color: #1e40af; }
QPushButton#runBtn:disabled { background-color: #cbd5e1; color: #f1f5f9; }

/* ---- 取消 / 危险按钮 ---- */
QPushButton#cancelBtn {
    background-color: #fef2f2; color: #dc2626;
    border: 1px solid #fecaca;
}
QPushButton#cancelBtn:hover { background-color: #fee2e2; border-color: #fca5a5; }

/* ---- 图标型按钮（顶部导航） ---- */
QToolButton#iconBtn {
    border: none; background: transparent;
    font-size: 13px; padding: 6px 10px; border-radius: 8px;
    color: #475569; font-weight: 500;
}
QToolButton#iconBtn:hover { background-color: #e8edf5; color: #0f172a; }
QToolButton#iconBtn:pressed { background-color: #dbe3ee; }

/* ---- 输入控件 ---- */
QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox {
    border: 1px solid #d7dee8;
    border-radius: 8px;
    padding: 6px 9px;
    background-color: #ffffff;
    color: #1e293b;
    selection-background-color: #bfdbfe;
    selection-color: #0f172a;
}
QLineEdit:hover, QComboBox:hover, QTextEdit:hover,
QSpinBox:hover, QDoubleSpinBox:hover { border-color: #94a3b8; }
QLineEdit:focus, QComboBox:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #2563eb;
    background-color: #ffffff;
}
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #64748b;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #e5eaf1;
    border-radius: 10px;
    padding: 4px;
    outline: none;
    selection-background-color: #eff6ff;
    selection-color: #1d4ed8;
}

/* ---- 复选框 ---- */
QCheckBox { spacing: 7px; color: #334155; font-size: 12px; }
QCheckBox:hover { color: #0f172a; }
QCheckBox::indicator {
    width: 17px; height: 17px;
    border: 1px solid #cbd5e1; border-radius: 5px;
    background-color: #ffffff;
}
QCheckBox::indicator:hover { border-color: #2563eb; }
QCheckBox::indicator:checked {
    background-color: #2563eb; border-color: #2563eb;
}
QCheckBox::indicator:disabled { background-color: #f1f5f9; border-color: #e2e8f0; }

/* ---- 表格 ---- */
QTableWidget {
    border: 1px solid #e5eaf1; border-radius: 10px;
    background-color: #ffffff; gridline-color: #eef2f7;
    selection-background-color: #eff6ff; selection-color: #1e293b;
}
QHeaderView::section {
    background-color: #f8fafc;
    border: none;
    border-bottom: 1px solid #e5eaf1;
    padding: 7px 10px;
    font-weight: 600; color: #475569;
}
QTableWidget::item { padding: 4px 6px; }

/* ---- 进度条 ---- */
QProgressBar {
    border: none; background-color: #e8edf4;
    border-radius: 5px; height: 9px; text-align: center;
    font-size: 9px; color: #64748b;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #3b82f6, stop:1 #2563eb);
    border-radius: 5px;
}

/* ---- 范围 / 坐标标签 ---- */
QLabel#extentLabel {
    background-color: #f8fafc;
    border: 1px solid #e5eaf1;
    border-radius: 8px;
    padding: 8px 10px;
    font-family: Consolas, "Courier New", monospace;
    font-size: 11px; color: #475569;
}

/* ---- 通知横幅 ---- */
QLabel#noticeBanner {
    background-color: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 8px;
    padding: 8px 10px;
    color: #1d4ed8; font-size: 11px;
}

/* ---- 滚动条（全局） ---- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #c9d2de; border-radius: 5px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: #94a3b8; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: #c9d2de; border-radius: 5px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: #94a3b8; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ---- 工具提示 ---- */
QToolTip {
    background-color: #1e293b; color: #f1f5f9;
    border: none; border-radius: 6px;
    padding: 6px 10px; font-size: 11px;
}
"""

# -- Top navigation brand bar (dock_widget) ---------------------------------

TOP_BAR_QSS = """
QFrame#topBar {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                      stop:0 #ffffff, stop:1 #f0f4fb);
    border: 1px solid #e5eaf1;
    border-radius: 12px;
}
QLabel#brandLogo {
    background-color: #2563eb;
    color: #ffffff;
    border-radius: 9px;
    font-size: 14px;
}
QLabel#brandTitle {
    font-size: 14px; font-weight: 700; color: #0f172a;
    background: transparent;
}
QLabel#brandSubtitle {
    font-size: 10px; color: #94a3b8; background: transparent;
}
"""

# -- Button styles for Copilot chat -----------------------------------------

BTN_CLEAR_QSS = """
QPushButton {
    background-color: #ffffff; color: #475569;
    border: 1px solid #d7dee8; border-radius: 8px;
    padding: 5px 12px; font-size: 12px; font-weight: 500;
}
QPushButton:hover { background-color: #f1f5f9; color: #0f172a; border-color: #94a3b8; }
"""

BTN_STOP_QSS = """
QPushButton {
    background-color: #fef2f2; color: #dc2626;
    border: 1px solid #fecaca; border-radius: 8px;
    padding: 5px 12px; font-size: 12px; font-weight: 600;
}
QPushButton:hover { background-color: #fee2e2; border-color: #fca5a5; }
QPushButton:disabled { background-color: #f8fafc; color: #94a3b8; border-color: #e2e8f0; }
"""

BTN_KEY_QSS = """
QPushButton {
    background-color: #ffffff; color: #334155;
    border: 1px solid #d7dee8; border-radius: 8px;
    padding: 6px 12px; font-size: 12px; font-weight: 500;
}
QPushButton:hover { background-color: #f1f5f9; color: #0f172a; border-color: #94a3b8; }
"""

BTN_TOOLS_QSS = """
QPushButton {
    background-color: #eff6ff; color: #1d4ed8;
    border: 1px solid #bfdbfe; border-radius: 8px;
    padding: 6px 12px; font-size: 12px; font-weight: 600;
}
QPushButton:hover { background-color: #dbeafe; border-color: #93c5fd; }
QPushButton::menu-indicator { image: none; }
"""

BTN_SEND_QSS = """
QPushButton {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #3b82f6, stop:1 #2563eb);
    color: white; font-weight: 600;
    border-radius: 8px; padding: 6px 18px; font-size: 12px; border: none;
}
QPushButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                      stop:0 #2563eb, stop:1 #1d4ed8);
}
QPushButton:disabled { background-color: #cbd5e1; color: #f1f5f9; }
"""

CHAT_HISTORY_QSS = """
QTextEdit {
    background-color: #ffffff; font-size: 13px;
    border: 1px solid #e5eaf1; border-radius: 12px;
    padding: 14px;
    selection-background-color: #bfdbfe;
}
"""

INPUT_CARD_QSS = """
QFrame {
    background-color: #ffffff;
    border: 1px solid #d7dee8; border-radius: 12px;
}
"""

MENU_QSS = """
QMenu {
    background-color: #ffffff; border: 1px solid #e5eaf1;
    border-radius: 10px; padding: 6px; font-size: 12px;
}
QMenu::item {
    padding: 7px 20px 7px 14px; border-radius: 6px; color: #334155;
}
QMenu::item:selected { background-color: #eff6ff; color: #1d4ed8; }
QMenu::item:disabled { color: #cbd5e1; }
QMenu::separator { height: 1px; background: #eef2f7; margin: 5px 8px; }
QMenu::icon { padding-left: 6px; }
"""
