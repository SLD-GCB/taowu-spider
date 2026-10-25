#!/usr/bin/env python3
"""
main_qt.py — Taowu Spider v12.1.2  PySide6/Qt GUI 入口。
完全替代原 main_gui.py (Tkinter)，后端逻辑 100% 不变。
架构参照 baxiadb (taowuDB) config_gui/main.py + ui/main_window.py。
"""

from builtins import UserWarning
from logging import config
import sys, os, shutil, threading, time, warnings, json, ctypes
warnings.filterwarnings("ignore", category=UserWarning)

# ── 修复 Windows 控制台 GBK 编码问题 ──────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('gbk', 'gb2312', 'gb18030'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() in ('gbk', 'gb2312', 'gb18030'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ── 路径初始化 ─────────────────────────────────────────────
if getattr(sys, "frozen", False):
    app_root = os.path.dirname(sys.executable)
else:
    app_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_root)
sys.path.insert(0, os.path.join(app_root, "plugins"))

# ── PySide6 ─────────────────────────────────────────────────
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QLabel, QPushButton, QLineEdit,
    QComboBox, QSpinBox, QCheckBox, QTextEdit, QProgressBar,
    QStatusBar, QMenuBar, QMenu, QMessageBox, QFileDialog,
    QSplitter, QFrame, QSizePolicy, QScrollArea,
)
from PySide6.QtCore import Q_ARG, QMetaObject, Qt, Signal, QTimer
from PySide6.QtGui import QFont, QIcon, QAction, QKeySequence, QTextCursor, QBrush

# ── Qt 弹窗（纯 PySide6） ──────────────────────────────────
from qt_dialogs import (
    PreviewDialog, CookieManagerDialog, CsvTranscodeDialog,
    DbViewerDialog, ManualInputDialog, VideoGrabDialog, AudioGrabDialog,
)

# ── 主题 ────────────────────────────────────────────────────
from qt_theme import (
    LIGHT_THEME as T, apply_theme, FONTS,
    BTN_PRIMARY_QSS, BTN_SUCCESS_QSS, BTN_WARNING_QSS,
    BTN_DANGER_QSS, BTN_INFO_QSS, BTN_PURPLE_QSS,
    BTN_PINK_QSS, BTN_MUTED_QSS, BTN_DEFAULT_QSS,
    open_tool_dialog,
)

# ── 图标路径（兼容 PyInstaller frozen，优先 .ico）──────
def _icon_path():
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = app_root
    # 优先使用 .ico（Windows 任务栏支持更好），回退 .png
    ico = os.path.join(base, "taowu.ico")
    if os.path.exists(ico):
        return ico
    return os.path.join(base, "taowu.png")

# ── 后端模块（与旧 main_gui.py 完全相同的导入） ──────────────
from config import *
import qiongqi_maw, DBPlugin
import spider_batch_v9

# ═══════════════════════════════════════════════════════════════
# 全局状态（替代旧 tk.BooleanVar/IntVar/StringVar）
# ═══════════════════════════════════════════════════════════════
STOP_CRAWL = False
_crawl_lock = threading.Lock()

# 功能开关 — 用 Python 原生类型
gui_enable_image_scrape = True
gui_enable_cookie = True
gui_enable_resource_block = True
gui_enable_fast_fail = True
gui_enable_parallel_parse = True
gui_enable_strip = False
gui_concurrent_workers = MAX_CONCURRENT
gui_browser_workers = MAX_CONCURRENT_BROWSER
gui_anti_detect_level = "高"
gui_profile_rotate = False
gui_random_delay_boost = False
gui_direct_chrome = False
gui_target_count = 15


# ═══════════════════════════════════════════════════════════════
# MainWindow
# ═══════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    """Taowu Spider 主窗口 — PySide6/Qt 实现。"""
    log_signal = Signal(str)
    manual_input_signal = Signal(object)
    verify_tip_signal = Signal()       # 跨线程触发验证提示
    progress_signal = Signal(int, int, str)  # 跨线程进度更新

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.setWindowIcon(QIcon(_icon_path()))

        # 数据库连接模式
        self._mode = VAR_DEFAULT_MODE

        # 验证提示跨线程同步
        self._verify_tip_result = False
        self._verify_tip_event = threading.Event()

        # ── 构建 UI ──
        self._setup_menu_bar()
        self._build_central()
        self._setup_status_bar()
        self._setup_log_signal()
        self._setup_verify_tip()

        # ── 后端日志重定向到 Qt 日志窗口 ──
        spider_batch_v9.set_log_func(self.log)

        # 初始日志
        self._startup_log()

    # ═══════════════════════════════════════════════════
    # 菜单栏
    # ═══════════════════════════════════════════════════
    def _setup_menu_bar(self):
        mb = self.menuBar()

        # 文件
        file_menu = mb.addMenu(MENU_FILE)
        act_view_log = QAction(MENU_VIEW_LOG, self)
        act_view_log.triggered.connect(self._on_view_log_file)
        file_menu.addAction(act_view_log)
        file_menu.addSeparator()
        act_exit = QAction(MENU_EXIT, self)
        act_exit.setShortcut(QKeySequence("Ctrl+Q"))
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        # 视图
        view_menu = mb.addMenu(MENU_VIEW)
        act_reset = QAction(MENU_RESET_LAYOUT, self)
        act_reset.triggered.connect(self._on_reset_layout)
        view_menu.addAction(act_reset)

        # 帮助
        help_menu = mb.addMenu(MENU_HELP)
        act_about = QAction(MENU_ABOUT, self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    # ═══════════════════════════════════════════════════
    # 中央部件 — 滚动区域承载所有控件
    # ═══════════════════════════════════════════════════
    def _build_central(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.setCentralWidget(scroll)

        central = QWidget()
        central.setObjectName("centralWidget")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self._build_db_config(layout)
        self._build_url_input(layout)
        self._build_colored_buttons(layout)
        self._build_progress(layout)
        self._build_switches_and_anti(layout)
        self._build_default_buttons(layout)
        self._build_log_area(layout)

        layout.addStretch()
        scroll.setWidget(central)

    # ── 数据库配置区 ──────────────────────────────────
    def _build_db_config(self, parent_layout):
        grp = QGroupBox(GRP_DB_CONFIG)
        grid = QGridLayout(grp)
        grid.setSpacing(6)

        # 存储模式
        grid.addWidget(QLabel(LABEL_STORAGE_MODE), 0, 0)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(COMBO_MODE_OPTIONS)
        self.combo_mode.setCurrentIndex(COMBO_DEFAULT_IDX)
        self.combo_mode.currentTextChanged.connect(self._on_mode_changed)
        grid.addWidget(self.combo_mode, 0, 1)

        # Host
        grid.addWidget(QLabel(LABEL_HOST), 0, 2)
        self.ent_host = QLineEdit(DB_HOST_DEFAULT)
        self.ent_host.setMaximumWidth(120)
        grid.addWidget(self.ent_host, 0, 3)

        # User
        grid.addWidget(QLabel(LABEL_USER), 0, 4)
        self.ent_user = QLineEdit(DB_USER_DEFAULT)
        self.ent_user.setMaximumWidth(100)
        grid.addWidget(self.ent_user, 0, 5)

        # Password
        grid.addWidget(QLabel(LABEL_PWD), 0, 6)
        self.ent_pwd = QLineEdit(DB_PWD_DEFAULT)
        self.ent_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.ent_pwd.setMaximumWidth(120)
        grid.addWidget(self.ent_pwd, 0, 7)

        # DB
        grid.addWidget(QLabel(LABEL_DB), 0, 8)
        self.ent_db = QLineEdit(DB_NAME_DEFAULT)
        self.ent_db.setMaximumWidth(110)
        grid.addWidget(self.ent_db, 0, 9)

        # Table
        grid.addWidget(QLabel(LABEL_TABLE), 1, 0)
        self.ent_table = QLineEdit(DB_TABLE_DEFAULT)
        self.ent_table.setMaximumWidth(120)
        grid.addWidget(self.ent_table, 1, 1)

        # 数据表模板下拉（2026-09-07 新增，改纯目标表）：选模板→自动建同名表并填入“表”输入框，
        # 不再改动抽取规则（抽取规则由右侧“抓取规则：”下拉独立负责）。
        # 2026-09-11：导入/停用/查看搬到「配置管理」组件（白色按钮区），本行只负责**选择**。
        grid.addWidget(QLabel("模板："), 1, 2)
        self.combo_template = QComboBox()
        self.combo_template.setMinimumWidth(150)
        self.combo_template.setToolTip(
            "选一个数据表模板 → 自动建同名表并把表名填进「表」输入框。\n"
            "只列 engine=schema_v2 的模板（其余配不了表）。\n"
            "导入自己的模板 / 停用 / 查看内容：按下面的「配置管理」。")
        # 改用 currentIndexChanged + itemData 取键。原来用 currentTextChanged 拿显示文本，
        # 而文本现在带 [自带]/[用户] 前缀，拿它去 load_table_template 必然查不到；
        # 更要命的是「自带/用户同名」时下拉里有两行**文本完全相同**，按文本取会把两行都解析到
        # 用户那份 —— 选不回自带的那个。键放 itemData，按数据取才不会撞。
        self.combo_template.currentIndexChanged.connect(self._on_template_selected)
        grid.addWidget(self.combo_template, 1, 3)
        self._reload_template_combo()

        # 抓取规则下拉（2026-09-07 新增，独立于目标表）：只切 config.ACTIVE_SCHEMA 匹配规则，
        # 默认“招聘信息（默认）”=招聘老链路；其它=该行业字段规则库。不建表/不改目标表。
        # 2026-09-10 扩成两套目录（自带 数据表模板/+字段规则/、用户 用户字段配置/）。
        # 2026-09-11：右侧那三键（停用/导入配置/字段配置目录）**撤掉了** —— 导入/启停/查看
        # 全部收进「配置管理」组件。本行只负责**选择哪一份生效**，不再兼管理面：
        # 同一条管理路径在三个地方各长一套（这里、音视频配方行、以及将来的新配置类型），
        # 每套都要把"按 itemData 取键 / 导入后热重载 / 停用要能选回来"这些坑重踩一遍。
        grid.addWidget(QLabel("抓取规则："), 1, 4)
        self.combo_rule = QComboBox()
        self.combo_rule.setMinimumWidth(150)
        self.combo_rule.setToolTip("抓取/录入时的字段匹配规则。\n"
                                   "「招聘信息（默认）」＝老链路；其余＝[自带]/[用户] 两套目录里的\n"
                                   "字段配置（有 columns[]，可绑）与规则片段（自动并入规则库）。\n"
                                   "本下拉只切规则，不改目标表。\n"
                                   "导入自己的配置 / 停用 / 查看内容：按下面的「配置管理」。")
        # 必须用 currentIndexChanged（传序号）：条目文本带 [自带]/[用户] 标签，
        # 拿文本去查 load_table_template 必然查不到；真实键放在 itemData 里。
        self.combo_rule.currentIndexChanged.connect(self._on_rule_changed)
        grid.addWidget(self.combo_rule, 1, 5)

        self._reload_rule_combo()

        self._on_mode_changed(COMBO_MODE_OPTIONS[COMBO_DEFAULT_IDX])
        parent_layout.addWidget(grp)

    def _on_mode_changed(self, text):
        if text == COMBO_MODE_OPTIONS[0]:  # 外置MySQL
            self._mode = "out_mysql"
            self.ent_host.setEnabled(True)
            self.ent_user.setEnabled(True)
            self.ent_pwd.setEnabled(True)
        else:
            self._mode = VAR_DEFAULT_MODE
            self.ent_host.setEnabled(False)
            self.ent_user.setEnabled(False)
            self.ent_pwd.setEnabled(False)

    # ── 数据表模板 / 抓取规则（2026-09-07 新增）────────────
    def _reload_template_combo(self, keep_key=None):
        """填充“模板：”（纯目标表）：选行业模板→建同名表并填“表”。idx0=不建模板表。

        数据源用 `config.list_table_configs()` 而不是 `list_table_templates()`，两者差在三处：
          · 前者**带来源限定键**（自带:配置:电商商品）→ 能放进 itemData 精确定位；
          · 前者**把停用与写坏的也列出来**（灰度 + 后缀标注）→ 用户看得见"我那份为什么没出现"，
            而 list_table_templates 对停用项直接不返回，症状是"导入完就凭空消失了"；
          · 前者只有 schema_v2 → 与"能建表"的判据天然一致，不用在这里再滤一次。
        """
        import config
        try:
            rows = config.list_table_configs()
        except Exception as e:
            rows = []
            self.log(f"模板列表加载失败：{e}")
        self._template_rows = rows

        self.combo_template.blockSignals(True)
        self.combo_template.clear()
        self.combo_template.addItem("— 手动填表（不建模板表）—", "")
        for r in rows:
            tag = "[自带]" if r["source"] == "builtin" else "[用户]"
            text = "%s %s" % (tag, r["name"])
            if not r["ok"]:
                text += "（写坏了）"
            elif not r["enabled"]:
                text += "（已停用）"
            self.combo_template.addItem(text, r["key"])
            i = self.combo_template.count() - 1
            tip = "%s\n%s" % (r["key"], r["path"])
            if not r["ok"]:
                tip += "\n校验未通过：" + "；".join(r["errors"][:3])
                self.combo_template.setItemData(i, QBrush(T["muted"]),
                                                Qt.ItemDataRole.ForegroundRole)
            elif not r["enabled"]:
                tip += "\n已停用（到「配置管理」里重新启用）"
                self.combo_template.setItemData(i, QBrush(T["muted"]),
                                                Qt.ItemDataRole.ForegroundRole)
            self.combo_template.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        idx = 0
        if keep_key:
            for i in range(self.combo_template.count()):
                if self.combo_template.itemData(i) == keep_key:
                    idx = i
                    break
        self.combo_template.setCurrentIndex(idx)
        self.combo_template.blockSignals(False)
        return rows

    def _current_template(self):
        """当前选中的模板行；选在「手动填表」上 → None。"""
        key = self.combo_template.currentData(Qt.ItemDataRole.UserRole)
        if not key:
            return None
        for r in getattr(self, "_template_rows", []):
            if r["key"] == key:
                return r
        return None

    def _on_template_selected(self, *_a):
        """“模板：”=纯目标表：建表(缺则建)→填 ent_table。不改 config.ACTIVE_SCHEMA（规则归“抓取规则”下拉管）。"""
        import config
        import config_gui
        if not hasattr(self, "combo_template"):
            return
        r = self._current_template()
        if r is None:
            self.log("模板：不建模板表，目标表用『表』输入框填写（默认 spider_db）")
            return
        if not r["ok"]:
            self.log(f"模板：【{r['key']}】校验没通过，不能建表："
                     + "；".join(r["errors"][:3]) + "（按「配置管理」查看内容/打开目录去改）")
            return
        if not r["enabled"]:
            self.log(f"模板：【{r['key']}】已停用 → 不建表（按「配置管理」可重新启用）")
            return
        try:
            spec = config.load_table_template(r["key"])
        except Exception as e:
            self.log(f"模板加载失败：{e}")
            return
        if not spec:
            self.log(f"模板未找到：{r['key']}")
            return
        # 在目标库建表（若尚无同名表）
        tbl = str(spec.get("name") or r["key"])
        mode = self._mode
        host, user, pwd = self.ent_host.text().strip(), self.ent_user.text().strip(), self.ent_pwd.text().strip()
        db = self.ent_db.text().strip()
        db_file = config_gui.get_sqlite_real_path(db) if mode == "inner_sqlite" else ""
        try:
            tables = config_gui.get_table_list(mode, host, user, pwd, db, db_file)
            if tbl not in tables:
                err = config_gui.create_table_from_template(mode, host, user, pwd, db, db_file, tbl, spec)
                if err and err != "success":
                    self.log(f"建表【{tbl}】失败：{err}")
        except Exception as e:
            self.log(f"选模板建表异常：{e}")
            return
        self.ent_table.setText(tbl)
        self.log(f"模板：已选【{spec.get('name')}】→ 目标表 [{tbl}]（抽取规则请用『抓取规则』下拉，本下拉不再改规则）")
        # 两行选的是两份不相干的配置时点名。**不自动改任何一边** —— 「模板：」管目标表、
        # 「抓取规则：」管抽取，这是 09-07 定下的分工（用户已确认保持），自动纠正等于把
        # 一个下拉变成两个下拉的隐藏写入者。但"按 B 的列抽取、写进 B 的同名表、A 完全没用上"
        # 是个没有任何报错的坑，所以至少要说出来。
        try:
            act = config.ACTIVE_SCHEMA
            act_name = str((act or {}).get("name") or (act or {}).get("id") or "")
            if act and act_name and act_name != str(spec.get("name") or ""):
                self.log(f"  ⚠ 注意：「抓取规则：」现在绑的是【{act_name}】，与本模板【"
                         f"{spec.get('name')}】不是同一份 —— 抽取按【{act_name}】的列走，"
                         f"入库进的是它同名的那张表，本模板这次只起到建表作用。")
        except Exception:
            pass

    def _reload_rule_combo(self, keep_key=None):
        """重建“抓取规则”下拉：idx0=招聘默认；其余＝自带/用户两套目录里的**字段配置**。

        规则片段**不进这个下拉**。它（`字段规则/` 与 `用户字段配置/` 里 `{规则名:{…}}` 那种形状）
        导入即自动并入规则库，本身不是一套列定义，绑不了；列出来的唯一效果是**误导** ——
        选中后 `_bind_active_schema` 把 ACTIVE_SCHEMA 打回 None 回退招聘默认，
        而下拉框仍挂着那条片段的名字，**「显示的」和「生效的」对不上**。
        要管理它们去「配置管理」的规则片段页（导入/停用/启用/查看都在那儿）。

        范式照抄 _reload_recipe_combo：blockSignals → clear → 填 → 复位 → 解锁。
        不 blockSignals 的话 clear() 会触发 currentIndexChanged，回调里再动下拉框 → 递归重建。

        keep_key：重建后停回哪一项（None = 保持当前选中项）。**必须保留选中** —— 否则点一下
        「停用」，下拉框跳回 idx0，用户下一眼就找不到自己刚停用的是哪条，再点一下按钮更是没反应。
        """
        import config
        if keep_key is None and hasattr(self, "combo_rule"):
            keep_key = self.combo_rule.currentData(Qt.ItemDataRole.UserRole)
        try:
            rows = config.list_field_configs()
        except Exception as e:
            rows = []
            self.log(f"字段配置列表加载失败：{e}")
        self._field_rows = rows
        self.combo_rule.blockSignals(True)
        self.combo_rule.clear()
        self.combo_rule.addItem("招聘信息（默认）", "")
        for r in rows:
            if r["kind"] == "rules":
                continue          # 规则片段不是可绑定的目标，理由见 docstring
            text = "%s %s" % ("[自带]" if r["source"] == "builtin" else "[用户]", r["name"])
            if not r["ok"]:
                text += "（写坏了）"
            elif not r["enabled"]:
                text += "（已停用）"
            self.combo_rule.addItem(text, r["key"])
            i = self.combo_rule.count() - 1
            tip = "%s\n%s" % (r["key"], r["path"])
            if not r["ok"]:
                # 不藏起来：用户要能看见自己那份为什么没生效
                tip += "\n校验未通过：" + "；".join(r["errors"][:3])
                self.combo_rule.setItemData(i, QBrush(T["muted"]), Qt.ItemDataRole.ForegroundRole)
            elif not r["enabled"]:
                tip += "\n已停用（到「配置管理」里重新启用）"
                self.combo_rule.setItemData(i, QBrush(T["muted"]), Qt.ItemDataRole.ForegroundRole)
            self.combo_rule.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        idx = 0
        if keep_key:
            for i in range(self.combo_rule.count()):
                if self.combo_rule.itemData(i) == keep_key:
                    idx = i
                    break
        self.combo_rule.setCurrentIndex(idx)
        self.combo_rule.blockSignals(False)

    def _current_field_cfg(self):
        """当前选中的配置行；选在「招聘信息（默认）」上 → None。"""
        if not hasattr(self, "combo_rule"):
            return None
        key = self.combo_rule.currentData(Qt.ItemDataRole.UserRole)
        if not key:
            return None
        for r in getattr(self, "_field_rows", []):
            if r["key"] == key:
                return r
        return None

    def _on_rule_changed(self, *_a):
        self._bind_active_schema()

    def _bind_active_schema(self, quiet=False):
        """“抓取规则”=config.ACTIVE_SCHEMA 唯一写入者。只切规则，不改目标表/不建表。

        idx0 招聘→None；选中「字段配置」→ 绑它的 spec（列里并列的规则一起生效）；
        **选中「规则片段」→ None**：片段是自动并入规则库的，它本身不是一套列定义，
        没法当"绑定的目标"。宁可回到招聘默认并说清楚，也不要留一个看不见的旧状态。
        """
        import config
        if not hasattr(self, "combo_rule"):
            return
        r = self._current_field_cfg()
        if r is None:
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log("抓取规则：招聘（默认老链路）→ 按招聘字段抽取；目标表用『表』输入框所指（默认 spider_db）")
            return
        if r["kind"] != "config":
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log(f"抓取规则：【{r['name']}】是规则片段（导入即自动并入规则库，不需要在这里选）"
                         f"→ 已回到招聘默认")
            return
        if not r["ok"]:
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log(f"抓取规则：【{r['name']}】校验没通过，不能绑：" + "；".join(r["errors"][:3]))
            return
        if not r["enabled"]:
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log(f"抓取规则：【{r['name']}】已停用 → 已回到招聘默认"
                         f"（按「配置管理」可重新启用）")
            return
        try:
            spec = config.load_table_template(r["key"])
        except Exception as e:
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log(f"抓取规则加载失败：{e}")
            return
        if not spec:
            config.ACTIVE_SCHEMA = None
            if not quiet:
                self.log(f"抓取规则未找到：{r['key']}")
            return
        config.ACTIVE_SCHEMA = spec
        if not quiet:
            n = len(spec.get("columns") or [])
            self.log(f"抓取规则：已绑【{spec.get('name')}】→ {n} 个字段按该配置的列/规则匹配，"
                     f"入库自动进同名表")
        # 导入那一刻校验过"引用的规则在不在"，但规则可以在**之后**被停用 —— 那一步过了这道关就
        # 没人管了，表现是"这一列永远抽不出值"（schema_engine 对查不到的规则不报错、直接不给值）。
        # 绑定时再核一遍，把它变成一条看得见的日志。
        try:
            lib = config.load_field_rules()
            miss = [f"{c.get('name')}→{c.get('rule')}" for c in (spec.get("columns") or [])
                    if c.get("rule") and c["rule"] not in lib]
        except Exception:
            miss = []
        if miss:
            self.log(f"  ⚠ 有 {len(miss)} 列引用的规则不在规则库里，这些字段会静默抽不出值："
                     + "、".join(miss[:5]) + "（多半是那份规则片段被停用了）")

    # ── 统一配置管理（2026-09-11 新增）──────────────────
    def _open_config_manager(self):
        """打开「配置管理」—— 媒体配方 / 数据表模板·字段配置 / 字段规则片段 的唯一管理入口。

        原来这套管理面在三个地方各长了一份（本行三键、音视频配方行三键……），每加一类配置就要
        再复制一遍，连"按 itemData 取键""导入后热重载""停用要能选中回来"这些坑都得重踩。
        现在收敛成一个组件（qt_config_manager），主界面只留这一个入口。

        弹窗里的改动（导入/启停）**对主界面是有影响的**，所以关窗后必须把两个下拉框重建一遍：
        停用一份正在生效的配置、或者导入一份同名的把自带的顶掉，都会让下拉框里的 pending 选中项
        与现实脱节。`_bind_active_schema` 也要重跑 —— 它写的 ACTIVE_SCHEMA 是一份**解析后的快照**，
        不是活引用，不重绑就还是旧那份的列。
        """
        import config
        try:
            from qt_config_manager import ConfigManagerDialog
        except Exception as e:
            self.log(f"配置管理打不开：{e}")
            return
        self.log("配置管理：打开（媒体配方 / 数据表模板·字段配置 / 字段规则片段）")
        if os.path.isdir(config.TABLE_TEMPLATE_DIR):
            self.log(f"  写法说明与空白模板：{os.path.join(config.TABLE_TEMPLATE_DIR, '_用户字段配置说明.md')}")
        try:
            dlg = ConfigManagerDialog(self, self)
            # 非模态打开（主窗全程可用）。收尾动作改挂 finished 信号 —— 原先靠 exec() 的阻塞顺序保证。
            open_tool_dialog(self, dlg, on_finished=lambda _c: self._after_config_closed())
        except Exception as e:
            self.log(f"配置管理异常：{e}")
            return

    def _after_config_closed(self):
        """配置管理关窗后对齐主界面（三个动作缺一不可，见 ConfigManagerDialog docstring）。"""
        self._reload_rule_combo()
        self._reload_template_combo()
        self._bind_active_schema(quiet=True)
        self.log("配置管理：已按最新配置刷新「模板：」与「抓取规则：」两个下拉框")

    # ── 网站全方位诊断（2026-09-11 新增）───────────────
    def _open_site_diag(self):
        """打开「网站诊断」—— 粘一个 URL，体检这个站能不能抓、坑在哪、底线是什么。

        它是**观测**入口，跟抓取链路完全隔离：诊断走自己的探针（site_diag.probe_http），
        不碰 vg 的 host 健康表 / 闸门 / 代理，也不调用任何 register_*，
        所以开一次诊断不会改变后面真实抓取的行为。

        URL 预填主界面输入框里的第一行 —— 用户多半是"看到个新站想先摸摸底"，
        让他再粘一遍没有意义。
        """
        try:
            from qt_site_diag import SiteDiagDialog
        except Exception as e:
            self.log(f"网站诊断打不开：{e}")
            return
        urls = []
        try:
            urls = [ln.strip() for ln in self.url_box.toPlainText().splitlines() if ln.strip()][:1]
        except Exception:
            pass
        self.log("网站诊断：打开（只观测不绕过；会抓目标站的 robots.txt 并把规矩摊开）")
        try:
            dlg = SiteDiagDialog(self, urls)
            open_tool_dialog(self, dlg)
        except Exception as e:
            self.log(f"网站诊断异常：{e}")

    # ── URL 输入 ─────────────────────────────────────
    def _build_url_input(self, parent_layout):
        grp = QGroupBox(LABEL_URL_TIP)
        lt = QVBoxLayout(grp)
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText(PREVIEW_DEFAULT_URL)
        self.url_box.setMaximumHeight(100)
        self.url_box.setFont(FONTS["default"])
        lt.addWidget(self.url_box)
        parent_layout.addWidget(grp)

    # ── 彩色按钮（URL输入框下方，多排网格对齐）─────
    def _build_colored_buttons(self, parent_layout):
        COLS = 6
        grid = QGridLayout()
        grid.setSpacing(5)

        btns_colored = [
            (BTN_COMMON,          BTN_PRIMARY_QSS, self._run_common_mode),
            (BTN_ENHANCE,         BTN_INFO_QSS,    self._run_enhance_mode),
            (BTN_BATCH_DISCOVERY, BTN_PINK_QSS,    self._run_batch_discovery_mode),
            (BTN_EXTREME_MODE,    BTN_PURPLE_QSS,  self._run_extreme_mode),
            (BTN_HARD_SITE,       BTN_DANGER_QSS,  self._run_hard_site_mode),
            (BTN_JSON_IMPORT,     BTN_WARNING_QSS, self._open_json_import),
            (BTN_STATS,           BTN_SUCCESS_QSS, self._show_stats),
            (BTN_SAVE_SESSION,    BTN_SUCCESS_QSS, self._save_session_cookies),
            (BTN_CLEAR_COOKIE,    BTN_MUTED_QSS,   self._clear_cookies),
            (BTN_CLEAR_TXT,       BTN_MUTED_QSS,   self._clear_all),
            (BTN_CDC_PATCH,       BTN_DANGER_QSS,  self._apply_cdc_patch),
            (BTN_CDC_STATUS,      BTN_MUTED_QSS,   self._check_cdc_patch_status),
            (BTN_STEALTH_TEST,    BTN_WARNING_QSS, self._stealth_test),
            (BTN_STOP_ALL,        BTN_DANGER_QSS,  self._stop_all_crawl),
        ]

        for i, (txt, qss, fn) in enumerate(btns_colored):
            btn = QPushButton(txt)
            btn.setStyleSheet(qss)
            btn.setMinimumWidth(120)
            if txt == BTN_STOP_ALL:
                btn.setFont(FONTS["default_bold"])
            btn.clicked.connect(fn)
            grid.addWidget(btn, i // COLS, i % COLS)

        # 使各列均匀伸缩
        for c in range(COLS):
            grid.setColumnStretch(c, 1)
        parent_layout.addLayout(grid)

    # ── 进度条 ────────────────────────────────────────
    def _build_progress(self, parent_layout):
        row = QHBoxLayout()
        self.progress_label = QLabel(STATUS_READY)
        self.progress_label.setFixedWidth(85)
        row.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        row.addWidget(self.progress_bar, 1)
        parent_layout.addLayout(row)

    # ── 功能开关 + 条数 + 反检测（2排，进度条下方）───
    def _build_switches_and_anti(self, parent_layout):
        # 第1排：功能开关 + 并发数 + 条数
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self.chk_image = QCheckBox(CHK_IMAGE_SCRAPE)
        self.chk_image.setChecked(True)
        self.chk_image.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_image)

        self.chk_cookie = QCheckBox(CHK_COOKIE_PERSIST)
        self.chk_cookie.setChecked(True)
        self.chk_cookie.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_cookie)

        self.chk_resource = QCheckBox(CHK_RESOURCE_BLOCK)
        self.chk_resource.setChecked(True)
        self.chk_resource.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_resource)

        self.chk_fast_fail = QCheckBox(CHK_FAST_FAIL)
        self.chk_fast_fail.setChecked(True)
        self.chk_fast_fail.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_fast_fail)

        self.chk_parallel = QCheckBox(CHK_PARALLEL_PARSE)
        self.chk_parallel.setChecked(True)
        self.chk_parallel.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_parallel)

        self.chk_strip = QCheckBox(CHK_STRIP_MODE)
        self.chk_strip.setChecked(False)
        self.chk_strip.toggled.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.chk_strip)

        row1.addWidget(QLabel(LABEL_HTTP_CONCURRENT))
        self.spin_http = QSpinBox()
        self.spin_http.setRange(1, 16)
        self.spin_http.setValue(MAX_CONCURRENT)
        self.spin_http.setFixedWidth(72)
        self.spin_http.valueChanged.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.spin_http)

        row1.addWidget(QLabel(LABEL_BROWSER))
        self.spin_browser = QSpinBox()
        self.spin_browser.setRange(1, 5)
        self.spin_browser.setValue(MAX_CONCURRENT_BROWSER)
        self.spin_browser.setFixedWidth(72)
        self.spin_browser.valueChanged.connect(lambda: self._apply_feature_flags())
        row1.addWidget(self.spin_browser)

        row1.addWidget(QLabel(LABEL_TARGET_COUNT))
        self.spin_target = QSpinBox()
        self.spin_target.setRange(1, 200)
        self.spin_target.setValue(15)
        self.spin_target.setFixedWidth(80)
        self.spin_target.valueChanged.connect(lambda v: setattr(sys.modules[__name__], 'gui_target_count', v))
        row1.addWidget(self.spin_target)

        row1.addStretch()
        parent_layout.addLayout(row1)

        # 第2排：反检测等级 + 按钮 + 剩余开关
        row2 = QHBoxLayout()
        row2.setSpacing(5)

        row2.addWidget(QLabel(LABEL_ANTI_DETECT))
        self.combo_defense = QComboBox()
        self.combo_defense.addItems(COMBO_DEFENSE_OPTIONS)
        self.combo_defense.setCurrentText(COMBO_DEFENSE_DEFAULT)
        self.combo_defense.currentTextChanged.connect(lambda: self._apply_anti_detect_level())
        row2.addWidget(self.combo_defense)

        btn = QPushButton(BTN_APPLY)
        btn.setFixedWidth(72)
        btn.clicked.connect(self._apply_anti_detect_level)
        row2.addWidget(btn)

        self.chk_profile = QCheckBox(CHK_PROFILE_ROTATE)
        self.chk_profile.toggled.connect(lambda: self._apply_feature_flags())
        row2.addWidget(self.chk_profile)

        self.chk_delay = QCheckBox(CHK_DELAY_BOOST)
        self.chk_delay.toggled.connect(lambda: self._apply_feature_flags())
        row2.addWidget(self.chk_delay)

        self.chk_direct = QCheckBox(CHK_DIRECT_CHROME)
        self.chk_direct.toggled.connect(self._toggle_direct_chrome_mode)
        row2.addWidget(self.chk_direct)

        row2.addStretch()
        parent_layout.addLayout(row2)

    # ── 白色按钮（功能开关下方，多排网格对齐）───────
    def _build_default_buttons(self, parent_layout):
        # 4 列 × 4 排 = 16 颗，正好排满（2026-09-11 从 5 列改过来）。
        # 少一列 = 每颗按钮占宽度的 1/4 而不是 1/5，**按钮更宽**，同时整块白钮区多出一排。
        # 注意 main_qt.py 里另有一处 COLS = 6（那是另一块布局），别一起改了。
        COLS = 4
        grid = QGridLayout()
        grid.setSpacing(5)

        btns_white = [
            (BTN_PREVIEW,            self._open_preview_dialog),
            (BTN_SINGLE_FETCH,       self._run_single_preview_fetch),
            (BTN_MANUAL_INPUT,       self._open_manual_input),
            (BTN_URL_DEDUP,          self._dedup_urls),
            (BTN_TEST_CONN,          self._test_conn),
            (BTN_BACKUP,             self._export_backup),
            (BTN_VIEW_DATA,          self._open_data_viewer),
            (BTN_RESET_STATS,        self._reset_stats),
            (BTN_COOKIE_MANAGER,     self._open_cookie_manager),
            (BTN_IMAGE_DIR,          self._open_image_dir),
            (BTN_VIDEO_GRAB,         self._open_video_grab),
            (BTN_AUDIO_GRAB,         self._open_audio_grab),
            (BTN_MEDIA_GRAB,         self._open_media_grab),
            (BTN_VIDEO_PLAYER,       self._open_video_player),
            # 2026-09-11：全部可导入配置的**唯一**管理入口（导入 / 启用停用 / 查看 / 打开目录）。
            # 放这儿而不是配置行旁边，是因为它管的远不止那两行（媒体配方、字段规则片段在配置行里
            # 根本没有位置）。
            (BTN_CFG_MGR,            self._open_config_manager),
            # 2026-09-11：网站全方位诊断。粘一个 URL 告诉你这站能不能抓、坑在哪、robots 允许什么。
            # 它是**观测**入口不是抓取入口，所以不跟那几颗下载按钮挨着，放最后收尾。
            (BTN_SITE_DIAG,          self._open_site_diag),
        ]

        for i, (txt, fn) in enumerate(btns_white):
            btn = QPushButton(txt)
            btn.setMinimumWidth(110)
            btn.clicked.connect(fn)
            if txt == BTN_CFG_MGR:
                # 唯一的管理入口，别混在一排白按钮里找不着
                btn.setStyleSheet(BTN_PURPLE_QSS)
                btn.setToolTip(TIP_CFG_MGR)
            elif txt == BTN_SITE_DIAG:
                btn.setStyleSheet(BTN_PURPLE_QSS)
                btn.setToolTip(TIP_SITE_DIAG)
            grid.addWidget(btn, i // COLS, i % COLS)

        for c in range(COLS):
            grid.setColumnStretch(c, 1)
        parent_layout.addLayout(grid)

    # ── 日志区 ────────────────────────────────────────
    def _build_log_area(self, parent_layout):
        parent_layout.addWidget(QLabel(LABEL_LOG_TITLE))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(FONTS["mono"])
        self.log_box.setMinimumHeight(200)
        parent_layout.addWidget(self.log_box, 1)

    # ── 状态栏 ────────────────────────────────────────
    def _setup_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_conn = QLabel(STATUS_CONN_PREFIX + "--")
        self._status_conn.setStyleSheet(f"color: {T['primary']}; font-weight: bold;")
        self.status_bar.addPermanentWidget(self._status_conn)
        self._status_db = QLabel(STATUS_DB_PREFIX + DB_NAME_DEFAULT)
        self.status_bar.addPermanentWidget(self._status_db)

    # ── 日志信号 ──────────────────────────────────────
    def _setup_log_signal(self):
        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.manual_input_signal.connect(self._open_manual_input_slot, Qt.ConnectionType.QueuedConnection)
        self.progress_signal.connect(self._on_progress_update, Qt.ConnectionType.QueuedConnection)

    # ── 验证提示（跨线程 QMessageBox） ──────────────
    def _setup_verify_tip(self):
        """注册 Qt 版验证提示 handler，替代 config.py 的 Tkinter 版本。"""
        self.verify_tip_signal.connect(self._on_verify_tip, Qt.ConnectionType.QueuedConnection)
        import config
        config.set_verify_tip_handler(self._qt_verify_tip_handler)

    def _qt_verify_tip_handler(self) -> bool:
        """线程安全：从任意线程 emit 信号，阻塞等待主线程 QMessageBox 返回。"""
        self._verify_tip_event.clear()
        self.verify_tip_signal.emit()
        self._verify_tip_event.wait(timeout=300)  # 最多等 5 分钟
        return self._verify_tip_result

    def _on_verify_tip(self):
        """在主线程显示 QMessageBox 验证提示。"""
        res = QMessageBox.question(
            self, "人机验证 - 请手动完成",
            "检测到人机验证（滑块/拼图/验证码）\n\n"
            "操作步骤：\n"
            "1. Chrome窗口已打开，请切换到Chrome\n"
            "2. 手动完成滑块验证或拼图\n"
            "3. 验证通过后，点击【确定】继续\n"
            "4. 如无法通过，点击【取消】跳过\n\n"
            "提示：通过后的Cookie会保留，后续URL可能免验证",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok
        )
        self._verify_tip_result = (res == QMessageBox.StandardButton.Ok)
        self._verify_tip_event.set()

    def _append_log(self, msg: str):
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)
        self.log_box.insertPlainText(msg + "\n")
        self.log_box.moveCursor(QTextCursor.MoveOperation.End)

    # ═══════════════════════════════════════════════════
    # 日志方法（线程安全）
    # ═══════════════════════════════════════════════════
    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        console_msg = f"[{timestamp}] {msg}"
        print(console_msg, flush=True)
        self.log_signal.emit(msg)

    def _startup_log(self):
        self.log(LOG_INIT)
        self.log(LOG_TIP1)
        self.log(LOG_TIP2)
        self.log("v12.1.2 PySide6/Qt — baxiadb UI 风格")

    # ═══════════════════════════════════════════════════
    # 核心回调（与 main_gui.py 完全一致）
    # ═══════════════════════════════════════════════════

    # ── URL 工具 ──────────────────────────────────────
    def _get_url_list(self):
        text = self.url_box.toPlainText().strip()
        urls = [line.strip() for line in text.splitlines() if line.strip()]
        return list(dict.fromkeys(urls))

    def _clear_all(self):
        self.url_box.clear()
        self.log_box.clear()

    def _dedup_urls(self):
        urls = self._get_url_list()
        self.url_box.setPlainText("\n".join(urls))
        self.log(LOG_URL_DEDUP_RESULT.format(count=len(urls)))

    # ── 进度条 ────────────────────────────────────────
    def update_progress(self, current, total, extra=""):
        """线程安全：通过 signal 跨线程更新进度条。"""
        self.progress_signal.emit(current, total, extra)

    def _on_progress_update(self, current, total, extra):
        """实际更新进度条控件（必须在主线程执行）。"""
        pct = int(current / max(total, 1) * 100)
        self.progress_bar.setValue(pct)
        self.progress_label.setText(f"{current}/{total} {extra}")

    def reset_progress(self):
        """线程安全：通过 signal 重置进度条。"""
        self.progress_signal.emit(0, 1, STATUS_READY)

    # ── 数据库操作 ────────────────────────────────────
    def _test_conn(self):
        """测试连接（兼缺表自动建表）。建表列由 DBPlugin.create_table 按 ACTIVE_SCHEMA 决定：
        绑定模板 → 模板列；未绑定 → 招聘默认。仅做日志区分，不在此处传字段。"""
        try:
            db = DBPlugin.DBPlugin(
                self._mode, self.ent_host.text().strip(),
                self.ent_user.text().strip(), self.ent_pwd.text().strip(),
                3306, self.ent_db.text().strip(), self.ent_table.text().strip()
            )
            db.close()
            _tpl = None
            try:
                from config import auto_create_fields
                _tpl = auto_create_fields()
            except Exception:
                _tpl = None
            if _tpl:
                self.log(f"连接成功（已绑定模板：缺表自动建 {len(_tpl)} 个模板列）")
            else:
                self.log(LOG_CONN_SUCC)
        except Exception as e:
            self.log(f"{LOG_DB_CONN_ERR.format(e=str(e))}")

    def _export_backup(self):
        m = self._mode
        dbn = self.ent_db.text().strip()
        bak_dir = os.path.join(app_root, "database_backup")
        os.makedirs(bak_dir, exist_ok=True)
        if m == VAR_DEFAULT_MODE:
            src = os.path.join(app_root, f"inner_sqlite_{dbn}.db")
            if os.path.exists(src):
                dst = os.path.join(bak_dir, f"backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
                shutil.copy2(src, dst)
                self.log(f"{LOG_BACKUP_SUCC.format(dst=dst)}")
            else:
                self.log(LOG_BACKUP_FAIL_NOFILE)
        else:
            self.log(LOG_MYSQL_BACKUP_TIP)

    def _clear_cookies(self):
        try:
            qiongqi_maw.clear_all_driver_cookies()
            import basnake_core
            basnake_core.basnake_core.clear_all_cookies()
            self.log(LOG_COOKIE_CLEARED)
        except Exception as e:
            self.log(LOG_COOKIE_CLEAR_FAIL.format(e=e))

    # ── 终止 ─────────────────────────────────────────
    def _stop_all_crawl(self):
        global STOP_CRAWL
        with _crawl_lock:
            STOP_CRAWL = True
        self.log(LOG_STOP_ALL)
        QTimer.singleShot(3000, lambda: setattr(sys.modules[__name__], "STOP_CRAWL", False))

    # ── 统计 ─────────────────────────────────────────
    def _show_stats(self):
        try:
            bs = qiongqi_maw.basnake_core.get_stats()
            gs = qiongqi_maw.get_global_stats()
            self.log(LOG_STATS_HEADER)
            self.log(LOG_STATS_HTTP.format(
                total=bs['total_requests'], success=bs['success_count'],
                fail=bs['fail_count'], block=bs['block_count'],
                rate=bs['success_rate'], speed=bs['req_per_min']))
            self.log(LOG_STATS_BROWSER.format(
                total=gs['total_fetched'], success=gs['success_fetched'],
                fail=gs['fail_fetched'], rate=gs['success_rate']))
            self.log(LOG_STATS_BYTES.format(bytes=bs['total_bytes'] + gs['total_bytes']))
            self.log(LOG_STATS_HEADER)
        except Exception as e:
            self.log(LOG_STATS_ERR.format(e=e))

    def _reset_stats(self):
        try:
            qiongqi_maw.basnake_core.reset_stats()
            qiongqi_maw.reset_global_stats()
            self.log(LOG_STATS_RESET)
        except Exception as e:
            self.log(LOG_STATS_RESET_ERR.format(e=e))

    # ── 功能开关 ──────────────────────────────────────
    def _apply_feature_flags(self, flags=None):
        """应用功能开关。flags 为 None 时从控件读取（主线程），否则使用传入值（跨线程安全）。"""
        import config
        if flags is None:
            flags = {
                "image": self.chk_image.isChecked(),
                "cookie": self.chk_cookie.isChecked(),
                "resource": self.chk_resource.isChecked(),
                "fast_fail": self.chk_fast_fail.isChecked(),
                "parallel": self.chk_parallel.isChecked(),
                "strip": self.chk_strip.isChecked(),
                "http": self.spin_http.value(),
                "browser": self.spin_browser.value(),
            }
        config.ENABLE_IMAGE_SCRAPE = flags["image"]
        config.ENABLE_COOKIE_PERSIST = flags["cookie"]
        config.ENABLE_RESOURCE_BLOCK = flags["resource"]
        config.ENABLE_FAST_FAIL = flags["fast_fail"]
        config.PARALLEL_PARSE_ENABLED = flags["parallel"]
        config.STRIP_ENABLED = flags["strip"]
        config.MAX_CONCURRENT = flags["http"]
        config.MAX_CONCURRENT_BROWSER = flags["browser"]
        self.log(LOG_CONFIG_APPLIED)

    def _apply_anti_detect_level(self, defense_level=None):
        """应用反检测等级。defense_level 为 None 时从控件读取，否则使用传入值。"""
        if defense_level is None:
            level_cn = self.combo_defense.currentText()
        else:
            level_cn = defense_level
        level = DEFENSE_LEVEL_MAP.get(level_cn, "high")
        import config
        config.ANTI_DETECT_LEVEL = level
        cfg = config.ANTI_DETECT_CONFIG.get(level, {})
        self.chk_resource.setChecked(cfg.get("resource_block", True))
        self.chk_cookie.setChecked(cfg.get("cookie_rotate", False))
        self.chk_profile.setChecked(cfg.get("profile_rotate", False))
        self.chk_delay.setChecked(cfg.get("random_delay_boost", False))
        self._apply_feature_flags()
        self.log(LOG_ANTI_DETECT_APPLIED.format(level=level))
        self.log(LOG_ANTI_DETECT_DETAIL.format(
            stealth=cfg.get('stealth_js'), cdp=cfg.get('cdp_block')))

    # ── CDC ───────────────────────────────────────────
    def _check_cdc_patch_status(self):
        try:
            import baize
            is_patched = baize.verify_cdc_patch()
            if is_patched:
                self.log(LOG_CDC_PATCHED)
            else:
                self.log(LOG_CDC_UNPATCHED)
        except Exception as e:
            self.log(LOG_CDC_CHECK_ERR.format(e=e))

    def _apply_cdc_patch(self):
        try:
            import baize
            self.log(LOG_CDC_PATCHING)
            success = baize.patch_chromedriver_cdc()
            if success:
                self.log(LOG_CDC_PATCH_OK)
            else:
                self.log(LOG_CDC_PATCH_FAIL)
            self._check_cdc_patch_status()
        except Exception as e:
            self.log(LOG_CDC_PATCH_ERR.format(e=e))

    def _toggle_direct_chrome_mode(self):
        enabled = self.chk_direct.isChecked()
        import qiongqi_maw
        qiongqi_maw._USE_DIRECT_CHROME = enabled
        if enabled:
            qiongqi_maw._direct_chrome_ready = False
            qiongqi_maw._direct_chrome_process = None
            qiongqi_maw._kill_chrome_for_direct_mode()
            self.log(LOG_DIRECT_CHROME_ON)
        else:
            qiongqi_maw._direct_chrome_ready = False
            self.log(LOG_DIRECT_CHROME_OFF)

    # ── 隐身检测 ──────────────────────────────────────
    def _stealth_test(self):
        self.log(LOG_STEALTH_START)
        def task():
            try:
                driver = qiongqi_maw.acquire_driver()
                driver.get("about:blank")
                results = driver.execute_script("""
                var r = {};
                r.webdriver = navigator.webdriver;
                r.webdriverUndefined = navigator.webdriver === false || navigator.webdriver === undefined;
                r.plugins = navigator.plugins ? navigator.plugins.length : 0;
                r.languages = navigator.languages ? navigator.languages.join(',') : 'N/A';
                r.platform = navigator.platform;
                r.hardwareConcurrency = navigator.hardwareConcurrency;
                r.deviceMemory = navigator.deviceMemory;
                r.maxTouchPoints = navigator.maxTouchPoints;
                r.vendor = navigator.vendor;
                r.chromeExists = typeof window.chrome !== 'undefined';
                r.chromeRuntime = typeof window.chrome !== 'undefined' && typeof window.chrome.runtime !== 'undefined';
                r.canvasModified = typeof HTMLCanvasElement.prototype.toDataURL.toString().indexOf('arguments') > -1;
                r.webglModified = typeof WebGLRenderingContext !== 'undefined';
                var cdcKeys = Object.keys(window).filter(function(k){return /^cdc_|_cdc/.test(k);}).length;
                r.automationKeys = 0;
                ['__driver','__selenium','__webdriver'].forEach(function(k){if(window.hasOwnProperty(k)) r.automationKeys++;});
                r.passed = r.webdriverUndefined && r.plugins >= 2 && r.chromeRuntime && r.automationKeys === 0 && cdcKeys === 0;
                return JSON.stringify(r);
                """)
                data = json.loads(results)
                cdc_dom = driver.execute_script("""
                var r = {docCdcAttrs: 0, cdcElements: 0};
                try {
                    var attrs = document.documentElement.getAttributeNames ? document.documentElement.getAttributeNames() : [];
                    attrs.forEach(function(a){ if(/^cdc_|_cdc/.test(a)) r.docCdcAttrs++; });
                    r.cdcElements = document.querySelectorAll('[class*="cdc_"], [id*="cdc_"]').length;
                } catch(e) {}
                return JSON.stringify(r);
                """)
                cdc_data = json.loads(cdc_dom)
                self.log(LOG_STATS_HEADER)
                self.log(f"WebDriver={data.get('webdriver')}  插件数={data.get('plugins')}")
                self.log(f"系统平台={data.get('platform')}  浏览器厂商={data.get('vendor')}")
                self.log(f"CPU核心={data.get('hardwareConcurrency')}  内存={data.get('deviceMemory')}GB")
                self.log(f"语言={data.get('languages')}  触摸点={data.get('maxTouchPoints')}")
                self.log(f"Chrome运行时={data.get('chromeRuntime')}  Canvas已修改={data.get('canvasModified')}")
                self.log(f"WebGL已修改={data.get('webglModified')}  CDC残留={data.get('cdcKeys')}")
                if data.get('passed'):
                    self.log(LOG_STEALTH_PASS)
                else:
                    self.log(LOG_STEALTH_WARN)
                self.log(LOG_STATS_HEADER)
                qiongqi_maw.release_driver(driver)
            except Exception as e:
                self.log(LOG_STEALTH_ERR.format(e=e))
        threading.Thread(target=task, daemon=True).start()

    # ── 会话Cookie ────────────────────────────────────
    def _save_session_cookies(self):
        try:
            from config import RUNTIME_DIR
            cookies_dir = os.path.join(RUNTIME_DIR, "cookies")
            os.makedirs(cookies_dir, exist_ok=True)
            save_path = os.path.join(cookies_dir, f"session_cookies_{time.strftime('%Y%m%d_%H%M%S')}.pkl")
            cookies = qiongqi_maw.get_all_driver_cookies()
            import pickle
            with open(save_path, "wb") as f:
                pickle.dump(cookies, f)
            self.log(LOG_COOKIE_SAVED.format(path=save_path, count=len(cookies)))
        except Exception as e:
            self.log(LOG_COOKIE_SAVE_FAIL.format(e=e))

    def _open_image_dir(self):
        img_dir = os.path.join(OUTPUT_DIR, "images")
        os.makedirs(img_dir, exist_ok=True)
        try:
            os.startfile(img_dir)
        except Exception:
            self.log(LOG_IMAGE_DIR_FALLBACK.format(dir=img_dir))

    # ── 数据查看器 ────────────────────────────────────
    def _open_data_viewer(self):
        try:
            dlg = DbViewerDialog(self)
            open_tool_dialog(self, dlg)
        except Exception as e:
            self.log(f"{LOG_PLUGIN_ERR.format(e=str(e))}")

    def _open_preview_dialog(self):
        dlg = PreviewDialog(self)
        open_tool_dialog(self, dlg)

    def _open_manual_input(self, data_list=None):
        current_db_config = {
            "mode": self._mode,
            "host": self.ent_host.text().strip(),
            "user": self.ent_user.text().strip(),
            "pwd": self.ent_pwd.text().strip(),
            "db": self.ent_db.text().strip(),
            "table": self.ent_table.text().strip()
        }
        dlg = ManualInputDialog(self, current_db_config, data_list)
        open_tool_dialog(self, dlg)

    def _open_video_grab(self):
        # db_config 为二期"结果可选入库"预留；本版只写文件 + 元数据 JSON
        current_db_config = {
            "mode": self._mode,
            "host": self.ent_host.text().strip(),
            "user": self.ent_user.text().strip(),
            "pwd": self.ent_pwd.text().strip(),
            "db": self.ent_db.text().strip(),
            "table": self.ent_table.text().strip()
        }
        dlg = VideoGrabDialog(self, current_db_config, initial_urls=self._get_url_list())
        open_tool_dialog(self, dlg)

    def _open_audio_grab(self):
        # db_config 为二期"结果可选入库"预留；本版只写文件 + 元数据 JSON（output/audio）
        current_db_config = {
            "mode": self._mode,
            "host": self.ent_host.text().strip(),
            "user": self.ent_user.text().strip(),
            "pwd": self.ent_pwd.text().strip(),
            "db": self.ent_db.text().strip(),
            "table": self.ent_table.text().strip()
        }
        dlg = AudioGrabDialog(self, current_db_config, initial_urls=self._get_url_list())
        open_tool_dialog(self, dlg)

    def _open_media_grab(self):
        """通用媒体下载：粘任意网站 URL → media_resolve 一键自动链（配方/静态/浏览器兜底）。
        惰性 import qt_media_grab：MediaGrabDialog 不依赖 db_config，纯文件落盘（output 视频/音频）。"""
        from qt_media_grab import MediaGrabDialog
        dlg = MediaGrabDialog(self, initial_urls=self._get_url_list())
        open_tool_dialog(self, dlg)

    def _open_video_player(self):
        """本地视频播放器：先探测 libmpv 内核，缺失给引导、不崩；否则惰性开对话框。
        惰性 import：qt_player/video_player 都在本函数才加载，避免模块级 import 期碰 mpv。"""
        import config
        try:
            from video_player import mpv_available
            ok, info = mpv_available()
        except Exception as e:
            QMessageBox.critical(
                self, config.WIN_TITLE_VIDEO_PLAYER,
                config.MSG_PLAYER_LOAD_FAIL.format(err=e))
            return
        if not ok:
            QMessageBox.warning(
                self, config.WIN_TITLE_VIDEO_PLAYER,
                config.MSG_PLAYER_NO_DLL.format(
                    name=config.MPV_DLL_NAME, dir=config.MPV_DIR, reason=info))
            return
        try:
            from qt_player import VideoPlayerDialog
            dlg = VideoPlayerDialog(self)
            open_tool_dialog(self, dlg)
        except Exception as e:
            QMessageBox.critical(
                self, config.WIN_TITLE_VIDEO_PLAYER,
                config.MSG_PLAYER_LOAD_FAIL.format(err=e))

    def _open_json_import(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, TITLE_JSON_OPEN, "",
            FILE_FILTER_JSON
        )
        if not filepath:
            return
        self.log(LOG_JSON_READING.format(path=filepath))

        def task():
            data_list = qiongqi_maw.gui_import_json_file(filepath)
            if not data_list:
                self.log(LOG_JSON_EMPTY)
                return
            self.log(LOG_JSON_PARSED.format(count=len(data_list)))
            self.manual_input_signal.emit(data_list)
        threading.Thread(target=task, daemon=True).start()

    def _open_manual_input_slot(self, data_list):
        self._open_manual_input(data_list)

    def _open_cookie_manager(self):
        dlg = CookieManagerDialog(self)
        open_tool_dialog(self, dlg)

    # ── 菜单回调 ──────────────────────────────────────
    def _on_view_log_file(self):
        log_path = os.path.join(app_root, "run_log.txt")
        if os.path.exists(log_path):
            try:
                os.startfile(log_path)
            except Exception as e:
                self.log(LOG_CANNOT_OPEN_LOGFILE.format(e=e))
        else:
            self.log(LOG_NO_LOGFILE)

    def _on_reset_layout(self):
        self.resize(WINDOW_SIZE[0], WINDOW_SIZE[1])
        self.log(LOG_LAYOUT_RESET)

    def _on_about(self):
        QMessageBox.about(
            self, ABOUT_TITLE,
            ABOUT_TEXT.format(title=WINDOW_TITLE)
        )

    # ═══════════════════════════════════════════════════
    # 采集模式回调
    # ═══════════════════════════════════════════════════

    def _log_schema_hint(self):
        """自动抓取启动时提示当前“抓取规则”绑定状态（防忘了切规则就抓→误入招聘默认抽取/表）。"""
        import config  # 局部 import：模块级 config 被 logging.config(from logging import config) 占名，须屏蔽
        sp = config.ACTIVE_SCHEMA or None
        if not sp:
            self.log("🧩 抓取规则=招聘（默认老链路）：本次自动抓取按『招聘字段』抽取，入库进『表』输入框指向的表"
                     "（默认 spider_db）。要按行业抽列/入库，请在上方『抓取规则』下拉选行业。")
        else:
            name = str(sp.get("name") or "").strip()
            self.log(f"🧩 抓取规则=行业模板【{name}】：抽取按模板列，入库自动进模板同名表。")

    def _run_common_mode(self):
        # ★ 在主线程预读取所有控件值，避免跨线程操作 Qt Widget 导致闪退
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }
        url_list = self._get_url_list()
        http_val = self.spin_http.value()
        image_val = self.chk_image.isChecked()
        parallel_val = self.chk_parallel.isChecked()
        def task():
            global STOP_CRAWL
            with _crawl_lock:
                STOP_CRAWL = False
            self._apply_feature_flags(flags)
            if not url_list:
                self.log(LOG_NO_URL)
                return
            if len(url_list) > MAX_TASK_NUM:
                self.log(f"{LOG_MAX_URL.format(MAX_TASK_NUM=MAX_TASK_NUM)}")
                return

            self.log(LOG_COMMON_START)
            self.log(LOG_COMMON_CONFIG.format(
                http=http_val, image=image_val, parse=parallel_val))
            self._log_schema_hint()

            collect_data = []
            self.reset_progress()
            BATCH_SIZE = 20
            all_urls = url_list
            processed = 0

            for batch_start in range(0, len(all_urls), BATCH_SIZE):
                if STOP_CRAWL:
                    self.log(LOG_USER_TERMINATED)
                    break
                batch_urls = all_urls[batch_start:batch_start + BATCH_SIZE]
                self.update_progress(processed, len(all_urls), STATUS_PROGRESS_CRAWL)

                batch_results = qiongqi_maw.gui_batch_crawl(
                    batch_urls, site_key=SITE_KEY, use_basnake=True,
                    progress_callback=lambda cur, tot, extra: self.update_progress(
                        processed + cur, len(all_urls), extra
                    )
                )

                for i, data in enumerate(batch_results):
                    if data:
                        idx = batch_start + i
                        data["job_url"] = batch_urls[i]
                        data["source"] = SITE_KEY
                        collect_data.append(data)
                        self.log(f"{LOG_INSERT_SUCC.format(idx=idx+1, url=batch_urls[i])}")
                    else:
                        self.log(f"{LOG_NO_DATA.format(idx=batch_start+i+1, url=batch_urls[i])}")
                processed += len(batch_urls)

            self.reset_progress()
            self.log(LOG_COMMON_END)
            if collect_data:
                self.log(LOG_COLLECT_COUNT.format(count=len(collect_data)))
                QMetaObject.invokeMethod(
                    self, "_open_manual_input_slot",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(object, collect_data)
                )
        threading.Thread(target=task, daemon=True).start()

    def _run_enhance_mode(self):
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }
        url_list = self._get_url_list()
        browser_val = self.spin_browser.value()
        image_val = self.chk_image.isChecked()
        cookie_val = self.chk_cookie.isChecked()
        def task():
            global STOP_CRAWL
            with _crawl_lock:
                STOP_CRAWL = False
            self._apply_feature_flags(flags)
            if not url_list:
                self.log(LOG_NO_URL)
                return
            if len(url_list) > MAX_TASK_NUM:
                self.log(f"{LOG_MAX_URL.format(MAX_TASK_NUM=MAX_TASK_NUM)}")
                return

            self.log(LOG_ENHANCE_START)
            self.log(LOG_ENHANCE_CONFIG.format(
                browser=browser_val, image=image_val, cookie=cookie_val))

            collect_data = []
            self.reset_progress()
            BATCH_SIZE = 10
            all_urls = url_list
            processed = 0

            for batch_start in range(0, len(all_urls), BATCH_SIZE):
                if STOP_CRAWL:
                    self.log(LOG_USER_TERMINATED)
                    break
                batch_urls = all_urls[batch_start:batch_start + BATCH_SIZE]
                self.update_progress(processed, len(all_urls), STATUS_PROGRESS_BROWSER)

                batch_results = qiongqi_maw.gui_batch_crawl(
                    batch_urls, site_key=SITE_KEY, use_basnake=False,
                    progress_callback=lambda cur, tot, extra: self.update_progress(
                        processed + cur, len(all_urls), extra
                    )
                )

                for i, data in enumerate(batch_results):
                    if data:
                        idx = batch_start + i
                        data["job_url"] = batch_urls[i]
                        data["source"] = SITE_KEY
                        collect_data.append(data)
                        self.log(f"{LOG_INSERT_SUCC.format(idx=idx+1, url=batch_urls[i])}")
                    else:
                        self.log(f"{LOG_NO_DATA.format(idx=batch_start+i+1, url=batch_urls[i])}")
                processed += len(batch_urls)

            self.reset_progress()
            self.log(LOG_ENHANCE_END)
            if collect_data:
                self.log(LOG_COLLECT_COUNT.format(count=len(collect_data)))
                QMetaObject.invokeMethod(
                    self, "_open_manual_input_slot",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(object, collect_data)
                )
        threading.Thread(target=task, daemon=True).start()

    def _run_extreme_mode(self):
        # ★ 主线程：设置控件状态 + 反检测等级
        self.combo_defense.setCurrentText("究极")
        self._apply_anti_detect_level("究极")
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }
        url_list = self._get_url_list()
        def task():
            global STOP_CRAWL
            with _crawl_lock:
                STOP_CRAWL = False
            self._apply_feature_flags(flags)
            if not url_list:
                self.log(LOG_NO_URL)
                return
            if len(url_list) > MAX_TASK_NUM:
                self.log(f"{LOG_MAX_URL.format(MAX_TASK_NUM=MAX_TASK_NUM)}")
                return

            self.log(LOG_EXTREME_START)
            self.log(LOG_EXTREME_MODE_CONFIG)

            collect_data = []
            self.reset_progress()
            BATCH_SIZE = 5
            all_urls = url_list
            processed = 0

            for batch_start in range(0, len(all_urls), BATCH_SIZE):
                if STOP_CRAWL:
                    self.log(LOG_USER_TERMINATED)
                    break
                batch_urls = all_urls[batch_start:batch_start + BATCH_SIZE]
                self.update_progress(processed, len(all_urls), STATUS_PROGRESS_EXTREME)

                batch_results = qiongqi_maw.gui_batch_crawl(
                    batch_urls, site_key=SITE_KEY, use_basnake=False,
                    progress_callback=lambda cur, tot, extra: self.update_progress(
                        processed + cur, len(all_urls), extra
                    )
                )

                for i, data in enumerate(batch_results):
                    if data:
                        idx = batch_start + i
                        data["job_url"] = batch_urls[i]
                        data["source"] = SITE_KEY
                        collect_data.append(data)
                        self.log(f"{LOG_INSERT_SUCC.format(idx=idx+1, url=batch_urls[i])}")
                    else:
                        self.log(f"{LOG_NO_DATA.format(idx=batch_start+i+1, url=batch_urls[i])}")
                processed += len(batch_urls)

            self.reset_progress()
            self.log(LOG_EXTREME_END)
            if collect_data:
                self.log(LOG_COLLECT_COUNT.format(count=len(collect_data)))
                QMetaObject.invokeMethod(
                    self, "_open_manual_input_slot",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(object, collect_data)
                )
        threading.Thread(target=task, daemon=True).start()

    def _run_batch_discovery_mode(self):
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }
        url_list = self._get_url_list()
        target_count = self.spin_target.value()
        db_config = {
            "mode": self._mode,
            "host": self.ent_host.text().strip(),
            "user": self.ent_user.text().strip(),
            "pwd": self.ent_pwd.text().strip(),
            "db": self.ent_db.text().strip(),
            "table": self.ent_table.text().strip(),
            "port": 3306,
        }
        def task():
            global STOP_CRAWL
            with _crawl_lock:
                STOP_CRAWL = False
            self._apply_feature_flags(flags)
            if not url_list:
                self.log(LOG_NO_URL)
                return

            self.log(f"{LOG_BATCH_DISCOVERY_START} — 目标：{target_count} 条")
            self.log(LOG_BATCH_MODE_DESC)
            self._log_schema_hint()

            self.reset_progress()
            all_urls = url_list

            for idx, url in enumerate(all_urls):
                if STOP_CRAWL:
                    self.log(LOG_USER_TERMINATED)
                    break

                self.update_progress(idx, len(all_urls), STATUS_PROGRESS_BATCH)
                self.log(f"正在打开列表页: {url}")

                try:
                    stats = spider_batch_v9.run_batch_discovery_crawl(
                        list_url=url,
                        site_key=SITE_KEY,
                        db_config=db_config,
                        progress_callback=lambda cur, tot, extra: self.update_progress(
                            cur, tot, extra
                        ),
                        stop_flag=lambda: STOP_CRAWL,
                        target_count=target_count,
                    )
                    self.log(f"{url} → 发现 {stats.get('discovered', 0)} 条，"
                             f"入库 {stats.get('inserted', 0)} 条")
                    if stats.get("error"):
                        self.log(f"  ⚠️ 错误: {stats['error']}")
                except Exception as e:
                    self.log(LOG_URL_RESULT_ERROR.format(url=url, e=e))

            self.reset_progress()
            self.log(LOG_BATCH_DISCOVERY_END)
        threading.Thread(target=task, daemon=True).start()

    def _run_hard_site_mode(self):
        # ★ 主线程：设置控件状态 + 反检测等级
        self.combo_defense.setCurrentText("究极")
        self._apply_anti_detect_level("究极")
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }
        url_list = self._get_url_list()
        target_count = self.spin_target.value()
        db_config = {
            "mode": self._mode,
            "host": self.ent_host.text().strip(),
            "user": self.ent_user.text().strip(),
            "pwd": self.ent_pwd.text().strip(),
            "db": self.ent_db.text().strip(),
            "table": self.ent_table.text().strip(),
            "port": 3306,
        }
        def task():
            global STOP_CRAWL
            with _crawl_lock:
                STOP_CRAWL = False
            self._apply_feature_flags(flags)
            if not url_list:
                self.log(LOG_NO_URL)
                return

            self.log(LOG_HARD_SITE_START)
            self.log(f"目标采集: {target_count} 条 | 搜索页零CDP裸跑 + 详情页引爆究极+剃毛")
            self._log_schema_hint()

            self.reset_progress()
            all_urls = url_list

            for idx, url in enumerate(all_urls):
                if STOP_CRAWL:
                    self.log(LOG_USER_TERMINATED)
                    break

                self.update_progress(idx, len(all_urls), STATUS_PROGRESS_HARD)
                self.log(f"正在打开搜索页: {url}")

                try:
                    stats = spider_batch_v9.run_hard_site_crawl(
                        list_url=url,
                        site_key=SITE_KEY,
                        db_config=db_config,
                        progress_callback=lambda cur, tot, extra: self.update_progress(
                            cur, tot, extra
                        ),
                        stop_flag=lambda: STOP_CRAWL,
                        target_count=target_count,
                    )
                    self.log(f"{url} → 采集 {stats.get('succeeded', 0)} 条，"
                             f"入库 {stats.get('inserted', 0)} 条")
                    if stats.get("error"):
                        self.log(f"  ⚠️ 错误: {stats['error']}")
                except Exception as e:
                    self.log(LOG_URL_RESULT_ERROR.format(url=url, e=e))

            self.reset_progress()
            self.log(LOG_HARD_SITE_END)
        threading.Thread(target=task, daemon=True).start()

    def _run_single_preview_fetch(self):
        """单条抓取：从URL输入框取第一条URL进行采集。"""
        url_list = self._get_url_list()
        if not url_list:
            self.log(LOG_NO_URL)
            return
        url = url_list[0]
        self.log(LOG_SINGLE_FETCH.format(url=url))
        flags = {
            "image": self.chk_image.isChecked(),
            "cookie": self.chk_cookie.isChecked(),
            "resource": self.chk_resource.isChecked(),
            "fast_fail": self.chk_fast_fail.isChecked(),
            "parallel": self.chk_parallel.isChecked(),
            "strip": self.chk_strip.isChecked(),
            "http": self.spin_http.value(),
            "browser": self.spin_browser.value(),
        }

        def task():
            try:
                self._apply_feature_flags(flags)
                results = qiongqi_maw.gui_single_crawl(
                    url, SITE_KEY
                )
                if results:
                    for data in results:
                        data["source"] = SITE_KEY
                    self.log(LOG_SINGLE_FETCH_OK.format(count=len(results)))
                    QMetaObject.invokeMethod(
                        self, "_open_manual_input_slot",
                        Qt.ConnectionType.QueuedConnection,
                        Q_ARG(object, results)
                    )
                else:
                    self.log(LOG_SINGLE_FETCH_FAIL)
            except Exception as e:
                self.log(LOG_SINGLE_FETCH_EXCEPTION.format(e=e))
        threading.Thread(target=task, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# 打印启动横幅
# ═══════════════════════════════════════════════════════════════
def _print_startup_banner():
    print("=" * 60)
    print(f"  ⚡ {STARTUP_BANNER_TITLE}")
    print(f"  {STARTUP_BANNER_SUBTITLE}")
    print(f"  {STARTUP_BANNER_DESC}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════
# 设置 Windows AppUserModelID（任务栏图标）
# ═══════════════════════════════════════════════════════════════
def _set_windows_app_id():
    """在 QApplication 创建前设置 Windows 任务栏 AppID。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
def main():
    _print_startup_banner()
    _set_windows_app_id()       # ★ 必须在 QApplication 之前

    app = QApplication(sys.argv)
    app.setApplicationName("Taowu Spider")
    app.setOrganizationName("taowu")
    app.setApplicationVersion("12.1.2")

    # 图标（.ico 优先，Windows 任务栏支持更好）
    icon_path = _icon_path()
    if os.path.exists(icon_path):
        app_icon = QIcon(icon_path)
        app.setWindowIcon(app_icon)
        # 强制让子窗口也继承此图标
        QApplication.setWindowIcon(app_icon)

    # 全局字体
    default_font = QFont("Microsoft YaHei", 9)
    default_font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(default_font)

    # 主题
    apply_theme(app, "light")

    # 主窗口
    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
