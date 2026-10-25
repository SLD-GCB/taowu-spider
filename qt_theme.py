"""
qt_theme.py — Taowu Spider PySide6 theme system.
Ported from baxiadb (taowuDB) config_gui/utils/theme.py.
Tailwind-inspired Slate+Blue palette with QPalette + QSS.
"""

from __future__ import annotations

import sys

from PySide6.QtGui import QPalette, QColor, QFont
from PySide6.QtCore import Qt

# ═══════════════════════════════════════════════════════════════
# 组件弹窗统一窗口标志 —— 标题栏右上角三颗按钮
# ═══════════════════════════════════════════════════════════════
# 全项目的组件弹窗统一用这一份：最小化 / 最大化 / 关闭（外加标题栏系统菜单）。
# 在此之前只有 ManualInputDialog 带 WindowMinMaxButtonsHint，其余弹窗标题栏只有一颗关闭。
#
# **放这儿不放 config.py**：config.py 是**不 import PySide6** 的纯常量中枢
# （回归脚本、打包脚本、CLI 都会 import 它），往里塞 Qt 类型会把 Qt 依赖带进非 GUI 路径。
# qt_theme 则相反 —— 每个弹窗模块**已经**在 import 它，落这儿是零新增依赖。
#
# 注意 `WindowSystemMenuHint`：Qt 对没有显式设过 flags 的 QDialog 是**默认带上**它
# （系统菜单＝右键标题栏那一栏）。一旦显式 setWindowFlags 就会整个替换掉默认值，
# 所以这里必须显式写上，否则那批弹窗会**丢掉系统菜单** —— 那是本次改动之外的行为变化。
#
# 另：这些弹窗**一律以非模态打开**（见本文件 `open_tool_dialog`）。早先它们是 `.exec()` 应用模态，
# 导致"最小化子窗后主窗仍点不动"，已改。
WIN_FLAGS_DIALOG = (
    Qt.WindowType.Dialog |
    Qt.WindowType.WindowTitleHint |
    Qt.WindowType.WindowSystemMenuHint |
    Qt.WindowType.WindowCloseButtonHint |
    Qt.WindowType.WindowMinMaxButtonsHint
)


def _own_taskbar_entry(dlg):
    """让弹窗在 Windows 任务栏上有**自己那一颗按钮**。

    为什么非有不可（真机实测 + 社区定论，不是推测）：

        这些弹窗构造函数都把主窗当 parent 传进来，于是它们全是主窗的 **owned window**
        —— 实测 `GW_OWNER` = 主窗句柄，而 `WS_EX_APPWINDOW` = False。
        Windows **不给 owned window 任务栏按钮**；按下最小化键时，系统手里没有
        "属于这个弹窗的按钮"可以缩进去，就只能把弹窗缩成**屏幕左下角的一条残留标题条**，
        并且这事按"整个窗口组"处理，**会牵连主窗**。
        这不是 Qt 的缺陷，是 Windows 的设计（社区原话：*"It's intentional, as your dialog
        has no taskbar-entry of its own. It's per design on Windows, and not a fault of Qt."*）。

    给它 `WS_EX_APPWINDOW` 之后它自成一颗任务栏按钮，最小化就只是它自己的事。

    **保留 owner 关系不动**：弹窗仍然永远在主窗之上、模态判定不受影响，只是多一颗任务栏按钮。
    （另一条被验证过的路是"去掉父窗"，但那会连 Qt 自带的"居中于父窗"一起丢掉，代价更大。）

    Qt 不认识这个扩展样式，所以它不会被后续的 `setWindowFlags()` / `show()` 冲掉；
    但它要求原生窗口已存在，故先用 `winId()` 把句柄逼出来。
    非 Windows、离屏平台、取不到句柄时静默跳过 —— 这一层只为任务栏归属，不该让弹窗开不出来。
    """
    if sys.platform != "win32":
        return
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.platformName() != "windows":
            return
        import ctypes
        from ctypes import wintypes

        hwnd = int(dlg.winId())
        if not hwnd:
            return

        # 用独立的 WinDLL 实例，避免给全局共享的 windll.user32 设 argtypes 影响别处
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowLongW.restype = ctypes.c_long
        u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        u32.SetWindowLongW.restype = ctypes.c_long

        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        ex = u32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        if not (ex & WS_EX_APPWINDOW):
            u32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                               ex | WS_EX_APPWINDOW)
    except Exception:
        pass


def apply_dialog_window_flags(dlg):
    """组件弹窗 `__init__` 里统一调这一句：三颗按钮 + 自己的任务栏按钮。

    替换掉原先直接写 `self.setWindowFlags(WIN_FLAGS_DIALOG)` —— 光有标志还不够，
    在 Windows 上还必须补上任务栏归属，否则最小化会牵连主窗（见 `_own_taskbar_entry`）。
    """
    dlg.setWindowFlags(WIN_FLAGS_DIALOG)
    _own_taskbar_entry(dlg)


# ═══════════════════════════════════════════════════════════════
# 组件弹窗一律以**非模态**打开
# ═══════════════════════════════════════════════════════════════
_OPEN_TOOL_DIALOGS = {}          # parent -> {弹窗类: 实例}


def open_tool_dialog(parent, dlg, on_finished=None):
    """打开一个组件弹窗，**不挡住主窗**。所有原本写 `dlg.exec()` 的地方改调它。

    ※ 为什么必须非模态（真机实测，不是推测）：

        原先这些弹窗一律 `.exec()`＝**应用模态**。Qt 挡主窗的方式是**直接在 Win32 层
        `EnableWindow(主窗, FALSE)`** —— 真机量到弹窗开着时主窗 `IsWindowEnabled` ＝ False。

        实测还堵死了两条"看起来更省事"的退路：

          · 弹窗**最小化之后**主窗 `Enabled` 仍然是 False —— 所以"最小化子窗去主窗点别的功能"
            在模态下根本做不到，这正是用户报的现象；
          · 光改 `setWindowModality(NonModal)` + `setModal(False)` **解不开** ——
            真机量过，只要那个 `exec()` 循环还在，主窗就是禁用的。

        所以只能不用 `exec()`。改成 `show()` 之后主窗全程可用，最小化与否都不影响。

    ※ 非模态顺带带来的两件事，都在这一处收掉（散到调用点必漏）：

        · **保活**：非模态没有阻塞循环托着，没人持引用就可能被回收 → 挂在 `parent` 上。
        · **去重**：非模态能连点开出好几个 → 同一 parent 下**同类弹窗只留一个**，
          已开着就抬起来，不另开。

    `on_finished(code)`：弹窗关闭时回调，用来接原先写在 `dlg.exec()` 后面的收尾动作
    （例：`if dlg.exec() == Accepted: self._refresh_table_list()`）。

    ※ 改完真机复现过用户那个场景（真 `MainWindow` + 真 `ManualInputDialog`，
       `WM_SYSCOMMAND/SC_MINIMIZE` 真最小化，`IsIconic` 确认已最小化）：
       最小化后主窗 `qtEnabled=True`/`IsWindowEnabled=1`，并且**能再开出第二个弹窗**。
       改之前这两项是 False —— 模态确实堵死。
       注意：这里验的是"主窗收得到点击"，不是真拿鼠标点了标题栏那一颗按钮；
       连点去重那条也没做真机手测。
    """
    key = type(dlg)
    book = _OPEN_TOOL_DIALOGS.setdefault(parent, {})
    old = book.get(key)
    if old is not None and old is not dlg:
        try:
            if old.isVisible():
                old.raise_()
                old.activateWindow()
                dlg.deleteLater()          # 本次这个不开了
                return old
        except Exception:
            pass
    book[key] = dlg

    def _finished(code=0):
        if book.get(key) is dlg:
            book.pop(key, None)
        if on_finished is not None:
            try:
                on_finished(code)
            except Exception:
                pass

    sig = getattr(dlg, "finished", None)
    if sig is not None:
        sig.connect(_finished)

    dlg.setModal(False)
    dlg.setWindowModality(Qt.WindowModality.NonModal)
    dlg.show()
    return dlg

# ═══════════════════════════════════════════════════════════════
# Color palette — baxiadb Slate+Blue design system
# ═══════════════════════════════════════════════════════════════

LIGHT_THEME = {
    "primary":       "#2563EB",
    "primary_light": "#60A5FA",
    "primary_dark":  "#1D4ED8",
    "success":       "#16A34A",
    "warning":       "#D97706",
    "danger":        "#DC2626",
    "info":          "#0284C7",
    "purple":        "#7C3AED",
    "pink":          "#DB2777",
    "background":    "#F8FAFC",
    "surface":       "#FFFFFF",
    "text":          "#1E293B",
    "text_secondary":"#64748B",
    "border":        "#E2E8F0",
    "hover":         "#F1F5F9",
    "active":        "#DBEAFE",
    "muted":         "#64748B",
    "dark":          "#334155",
    "white":         "#FFFFFF",
    "black":         "#000000",
}

DARK_THEME = {
    "primary":       "#60A5FA",
    "primary_light": "#93C5FD",
    "primary_dark":  "#2563EB",
    "success":       "#22C55E",
    "warning":       "#F59E0B",
    "danger":        "#EF4444",
    "info":          "#0EA5E9",
    "purple":        "#A78BFA",
    "pink":          "#F472B6",
    "background":    "#0F172A",
    "surface":       "#1E293B",
    "text":          "#F1F5F9",
    "text_secondary":"#94A3B8",
    "border":        "#334155",
    "hover":         "#1E293B",
    "active":        "#1D4ED8",
    "muted":         "#475569",
    "dark":          "#0F172A",
    "white":         "#FFFFFF",
    "black":         "#000000",
}

# ═══════════════════════════════════════════════════════════════
# Font configuration
# ═══════════════════════════════════════════════════════════════

FONTS = {
    "default":       QFont("Microsoft YaHei", 9),
    "default_bold":  QFont("Microsoft YaHei", 9, QFont.Weight.Bold),
    "heading":       QFont("Microsoft YaHei", 10),
    "heading_bold":  QFont("Microsoft YaHei", 10, QFont.Weight.Bold),
    "small":         QFont("Microsoft YaHei", 8),
    "mono":          QFont("Consolas", 10),
    "title":         QFont("Microsoft YaHei", 16, QFont.Weight.Bold),
    "section":       QFont("Microsoft YaHei", 12, QFont.Weight.Bold),
}

# ═══════════════════════════════════════════════════════════════
# Semantic button QSS helpers
# ═══════════════════════════════════════════════════════════════

def _btn_qss(bg: str, fg: str = "#FFFFFF") -> str:
    """Generate QPushButton stylesheet with hover/pressed/disabled states."""
    # Auto-darken for hover
    r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    hr, hg, hb = max(0, r - 25), max(0, g - 25), max(0, b - 25)
    hover_bg = f"#{hr:02x}{hg:02x}{hb:02x}"
    # Darker for pressed
    pr, pg, pb = max(0, r - 40), max(0, g - 40), max(0, b - 40)
    pressed_bg = f"#{pr:02x}{pg:02x}{pb:02x}"

    return f"""
    QPushButton {{
        background-color: {bg};
        color: {fg};
        border: none;
        border-radius: 6px;
        padding: 8px 18px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{
        background-color: {hover_bg};
    }}
    QPushButton:pressed {{
        background-color: {pressed_bg};
    }}
    QPushButton:disabled {{
        background-color: #94A3B8;
        color: #E2E8F0;
    }}
    """


BTN_PRIMARY_QSS = _btn_qss("#2563EB")
BTN_SUCCESS_QSS = _btn_qss("#16A34A")
BTN_WARNING_QSS = _btn_qss("#D97706")
BTN_DANGER_QSS  = _btn_qss("#DC2626")
BTN_INFO_QSS    = _btn_qss("#0284C7")
BTN_PURPLE_QSS  = _btn_qss("#7C3AED")
BTN_PINK_QSS    = _btn_qss("#DB2777")
BTN_MUTED_QSS   = _btn_qss("#64748B")

BTN_DEFAULT_QSS = f"""
    QPushButton {{
        background-color: {LIGHT_THEME["surface"]};
        color: {LIGHT_THEME["text"]};
        border: 1px solid {LIGHT_THEME["border"]};
        border-radius: 6px;
        padding: 8px 18px;
        font-size: 13px;
    }}
    QPushButton:hover {{
        background-color: {LIGHT_THEME["hover"]};
    }}
    QPushButton:pressed {{
        background-color: {LIGHT_THEME["active"]};
    }}
    QPushButton:disabled {{
        background-color: {LIGHT_THEME["hover"]};
        color: {LIGHT_THEME["text_secondary"]};
    }}
"""

# ═══════════════════════════════════════════════════════════════
# Global theme application
# ═══════════════════════════════════════════════════════════════

def get_theme(name: str = "light") -> dict:
    """Return a theme palette by name."""
    if name == "dark":
        return DARK_THEME
    return LIGHT_THEME


def apply_theme(app, name: str = "light") -> None:
    """Apply theme via QPalette + global QSS stylesheet."""
    theme = get_theme(name)

    # --- QPalette ---
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme["background"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme["hover"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme["primary"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    app.setPalette(palette)

    # --- Global QSS ---
    stylesheet = f"""
    QMainWindow {{
        background-color: {theme["background"]};
    }}
    QWidget {{
        font-family: "Microsoft YaHei";
        font-size: 12px;
    }}
    QPushButton {{
        padding: 7px 16px;
        border: 1px solid {theme["border"]};
        border-radius: 6px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
        font-size: 13px;
    }}
    QPushButton:hover {{
        background-color: {theme["hover"]};
        border-color: {theme["primary"]};
    }}
    QPushButton:pressed {{
        background-color: {theme["active"]};
    }}
    QPushButton:disabled {{
        background-color: {theme["hover"]};
        color: {theme["text_secondary"]};
        border-color: {theme["border"]};
    }}
    QMenuBar {{
        background-color: {theme["surface"]};
        border-bottom: 1px solid {theme["border"]};
        padding: 2px 0px;
    }}
    QMenuBar::item:selected {{
        background-color: {theme["active"]};
    }}
    QMenu {{
        background-color: {theme["surface"]};
        border: 1px solid {theme["border"]};
        padding: 4px;
    }}
    QMenu::item:selected {{
        background-color: {theme["active"]};
    }}
    QGroupBox {{
        border: 1px solid {theme["border"]};
        border-radius: 6px;
        margin-top: 12px;
        padding-top: 16px;
        font-weight: bold;
        color: {theme["text"]};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
    }}
    QLineEdit {{
        padding: 5px 8px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
    }}
    QLineEdit:focus {{
        border-color: {theme["primary"]};
    }}
    QLineEdit:disabled {{
        background-color: {theme["hover"]};
        color: {theme["text_secondary"]};
    }}
    QTextEdit {{
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
        padding: 4px;
    }}
    QPlainTextEdit {{
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
        padding: 4px;
    }}
    QComboBox {{
        padding: 5px 8px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
    }}
    QComboBox:disabled {{
        background-color: {theme["hover"]};
        color: {theme["text_secondary"]};
    }}
    QComboBox::drop-down {{
        border: none;
        padding-right: 8px;
    }}
    QSpinBox {{
        padding: 5px 8px;
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["surface"]};
        color: {theme["text"]};
        min-width: 68px;
    }}
    QSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 22px;
        border-left: 1px solid {theme["border"]};
    }}
    QSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 22px;
        border-left: 1px solid {theme["border"]};
    }}
    QTableWidget {{
        border: 1px solid {theme["border"]};
        gridline-color: {theme["border"]};
        background-color: {theme["surface"]};
        alternate-background-color: {theme["hover"]};
    }}
    QTableWidget::item:selected {{
        background-color: {theme["active"]};
        color: {theme["text"]};
    }}
    QHeaderView::section {{
        background-color: {theme["background"]};
        color: {theme["text_secondary"]};
        border: none;
        border-bottom: 2px solid {theme["border"]};
        padding: 6px 8px;
        font-weight: bold;
    }}
    QTreeWidget {{
        border: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
        alternate-background-color: {theme["hover"]};
    }}
    QTreeWidget::item:selected {{
        background-color: {theme["active"]};
        color: {theme["text"]};
    }}
    QProgressBar {{
        border: 1px solid {theme["border"]};
        border-radius: 4px;
        background-color: {theme["hover"]};
        text-align: center;
        color: {theme["text"]};
        height: 20px;
    }}
    QProgressBar::chunk {{
        background-color: {theme["primary"]};
        border-radius: 3px;
    }}
    QStatusBar {{
        border-top: 1px solid {theme["border"]};
        background-color: {theme["surface"]};
        color: {theme["text_secondary"]};
    }}
    QScrollBar:vertical {{
        background-color: {theme["background"]};
        width: 12px;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background-color: {theme["border"]};
        border-radius: 6px;
        min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}
    QScrollBar:horizontal {{
        background-color: {theme["background"]};
        height: 12px;
        border: none;
    }}
    QScrollBar::handle:horizontal {{
        background-color: {theme["border"]};
        border-radius: 6px;
        min-width: 30px;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0px;
    }}
    QCheckBox {{
        color: {theme["text"]};
        spacing: 6px;
    }}
    QLabel {{
        color: {theme["text"]};
    }}
    QSplitter::handle {{
        background-color: {theme["border"]};
        width: 2px;
    }}
    """
    app.setStyleSheet(stylesheet)
