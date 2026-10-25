"""
qt_dialogs.py — Taowu Spider 纯 PySide6/Qt 弹窗。
完全替代原 Tkinter 弹窗（manual_input_win / db_export_view_plugin / config_gui 弹窗部分）。
后端纯逻辑函数（TextCleaner / QianLongV2 / db_operate 等）直接复用 config_gui。
"""

import os, sys, json, time, csv, sqlite3, concurrent.futures, threading

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QComboBox,
    QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QSplitter, QScrollArea, QWidget,
    QFrame, QProgressBar, QCheckBox, QSpinBox,
)
from PySide6.QtCore import Qt, Signal, Slot, QMetaObject
from PySide6.QtGui import QFont, QTextCursor, QBrush

from qt_theme import (
    LIGHT_THEME as T, FONTS,
    BTN_PRIMARY_QSS, BTN_SUCCESS_QSS, BTN_WARNING_QSS,
    BTN_DANGER_QSS, BTN_INFO_QSS, BTN_PURPLE_QSS, BTN_MUTED_QSS,
    apply_dialog_window_flags, open_tool_dialog,
)

# ── 复用纯逻辑（无 Tkinter 依赖） ──────────────────────────
from config import (
    detect_csv_encoding, confidence_label, read_csv_preview, transcode_csv_to_utf8,
    WIN_TITLE_CSV_TRANSCODE, WIN_SIZE_CSV_TRANSCODE,
    LABEL_CSV_FILE, LABEL_CSV_BROWSE, LABEL_CSV_DETECT,
    LABEL_CSV_ENCODING, LABEL_CSV_CONFIDENCE,
    LABEL_CSV_TABLE_PREVIEW, LABEL_CSV_RAW_PREVIEW, LABEL_CSV_SAVE_AS,
    MSG_CSV_SELECT_FILE, MSG_CSV_DETECTING, MSG_CSV_NO_ENCODING,
    MSG_CSV_NO_FILE, MSG_CSV_DETECT_RESULT, MSG_CSV_EXPORTED, MSG_CSV_SUCCESS,
    TITLE_CSV_OPEN, TITLE_CSV_SAVE,
    BTN_PREVIEW, BTN_PREVIEW_RUN, BTN_PREVIEW_CLOSE,
    PREVIEW_DEFAULT_URL, LOG_PREVIEW_LEN,
    DB_HOST_DEFAULT, DB_USER_DEFAULT, DB_PWD_DEFAULT, DB_NAME_DEFAULT, DB_TABLE_DEFAULT,
    VAR_DEFAULT_MODE, LOG_FILE_NOT_EXIST, LOG_JSON_ERR,
    WIN_TITLE_MANUAL_INPUT, WIN_SIZE_MANUAL_INPUT,
    LABEL_JSON_FILE_PATH, LABEL_BROWSE, LABEL_LOAD_JSON,
    LABEL_SELECT_ITEM, LABEL_DELETE_CURRENT,
    LABEL_BATCH_CHARS, LABEL_MANUAL_KEYWORDS,
    LABEL_AUTO_EXTRACT, LABEL_MANUAL_EXTRACT, LABEL_CONFIRM_TO_FORM,
    LABEL_SAVE_TO_DB, LABEL_CLEAR_FORM, LABEL_ADD_ROW, LABEL_CLEAR_ALL_ROWS,
    LABEL_CURRENT_TABLE, LABEL_REFRESH_TABLES, LABEL_DATA_TABLE,
    LABEL_TABLE_ADD_EMPTY, LABEL_TABLE_DEL_SELECTED,
    LABEL_SUBMIT_ALL, LABEL_CLOSE_WINDOW, LABEL_OPERATION_LOG,
    LABEL_DELETE_ROW, LABEL_CLOSE,
    TITLE_JSON_OPEN, FILE_FILTER_JSON,
    LOG_JSON_READING, LOG_JSON_PARSED,
)
from config_gui import (
    INPUT_FIELD_LIST, MAX_WORKERS,
    TextCleaner, QianLongV2, QianLongResult,
    db_operate, get_table_list, load_table_data, add_empty_table_row,
    del_table_rows, get_sqlite_real_path, create_custom_table,
)

# ── 视频流抓取相关常量（v12.2 新增，独立小导入避免改动巨型列表） ──
from config import (
    VIDEO_DIR, ENABLE_VIDEO_BROWSER_PROBE,
    WIN_TITLE_VIDEO_GRAB, WIN_SIZE_VIDEO_GRAB, GRP_VIDEO_URL,
    LABEL_VIDEO_CANDIDATES, LABEL_VIDEO_LOG,
    BTN_VIDEO_START, BTN_VIDEO_STOP, BTN_VIDEO_OPEN_DIR, CHK_VIDEO_BROWSER,
    LOG_VIDEO_STOP, LOG_VIDEO_OPEN_DIR,
    CHK_VIDEO_SERIES, GRP_VIDEO_SERIES_OPT, LABEL_VIDEO_RANGE,
    LABEL_VIDEO_RANGE_TO, LABEL_VIDEO_RANGE_END, LABEL_VIDEO_SERIES_TIP,
)

# ── 抓取音频流相关常量（独立小导入，镜像上面的视频块） ──
from config import (
    AUDIO_DIR,
    WIN_TITLE_AUDIO_GRAB, WIN_SIZE_AUDIO_GRAB, GRP_AUDIO_URL,
    LABEL_AUDIO_CANDIDATES, LABEL_AUDIO_LOG,
    BTN_AUDIO_START, BTN_AUDIO_STOP, BTN_AUDIO_OPEN_DIR,
    LOG_AUDIO_STOP, LOG_AUDIO_OPEN_DIR,
    CHK_AUDIO_SERIES, GRP_AUDIO_SERIES_OPT, LABEL_AUDIO_RANGE,
    LABEL_AUDIO_RANGE_TO, LABEL_AUDIO_RANGE_END, LABEL_AUDIO_SERIES_TIP,
    CHK_AUDIO_PAGE_WATCH, LOG_AUDIO_WATCH_TRY, LOG_AUDIO_WATCH_HIT,
    LOG_AUDIO_WATCH_MISS, LOG_AUDIO_RECIPE_SEAM_HINT,
    LABEL_AUDIO_RECIPE_SEL, TIP_AUDIO_RECIPE_SEL, LOG_AUDIO_RECIPE_TRY,
    LOG_AUDIO_RECIPE_HIT, LOG_AUDIO_RECIPE_MISS, LOG_AUDIO_RECIPE_SERIES_SKIP,
)

# 配方行（下拉框/启停/导入）与「通用媒体下载」弹窗共用同一份实现
from qt_recipe_row import RecipeRowMixin

# ── 全局单例 ──────────────────────────────────────────
_text_cleaner = TextCleaner()
_qianlong = QianLongV2()


def _safe_str(s):
    if s is None:
        return ""
    return str(s)


# ═══════════════════════════════════════════════════════════════
# 1. PreviewDialog — URL 页面预览（纯Qt，简单弹窗）
# ═══════════════════════════════════════════════════════════════
class PreviewDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(BTN_PREVIEW)
        self.resize(820, 680)
        apply_dialog_window_flags(self)

        lt = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("URL:"))
        self.url_edit = QLineEdit(PREVIEW_DEFAULT_URL)
        row.addWidget(self.url_edit, 1)
        lt.addLayout(row)

        btn_row = QHBoxLayout()
        b = QPushButton(BTN_PREVIEW_RUN); b.clicked.connect(self._do_preview); btn_row.addWidget(b)
        b = QPushButton(BTN_PREVIEW_CLOSE); b.clicked.connect(self.accept); btn_row.addWidget(b)
        btn_row.addStretch()
        lt.addLayout(btn_row)

        self.text = QTextEdit(); self.text.setReadOnly(True); self.text.setFont(FONTS["mono"])
        lt.addWidget(self.text, 1)

    def _do_preview(self):
        url = self.url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "警告", "请输入URL")
            return
        import qiongqi_maw
        html = qiongqi_maw.gui_get_html_by_url(url)
        self.text.setPlainText(LOG_PREVIEW_LEN.format(len=len(html), content=html[:3000]))


# ═══════════════════════════════════════════════════════════════
# 2. CookieManagerDialog — Cookie 管理器（纯Qt）
# ═══════════════════════════════════════════════════════════════
class CookieManagerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cookie管理器")
        self.resize(500, 400)
        apply_dialog_window_flags(self)

        lt = QVBoxLayout(self)
        self.text = QTextEdit(); self.text.setReadOnly(True); self.text.setFont(FONTS["mono"])
        lt.addWidget(self.text, 1)

        br = QHBoxLayout()
        b = QPushButton("加载"); b.clicked.connect(self._load); br.addWidget(b)
        b = QPushButton("全部清除"); b.clicked.connect(self._clear); br.addWidget(b)
        b = QPushButton("关闭"); b.clicked.connect(self.accept); br.addWidget(b)
        lt.addLayout(br)
        self._load()

    def _load(self):
        try:
            import qiongqi_maw
            cookies = qiongqi_maw.get_all_driver_cookies()
            self.text.clear()
            for c in cookies:
                self.text.append(f"{c.get('name','?')} = {c.get('value','?')[:80]}")
        except Exception as e:
            self.text.setPlainText(f"加载失败：{e}")

    def _clear(self):
        try:
            import qiongqi_maw; qiongqi_maw.clear_all_driver_cookies()
            import basnake_core; basnake_core.basnake_core.clear_all_cookies()
            self.text.clear(); self.text.setPlainText("Cookie已全部清除")
        except Exception as e:
            self.text.setPlainText(f"清除失败：{e}")


# ═══════════════════════════════════════════════════════════════
# 3. CsvTranscodeDialog — CSV 乱码转译
# ═══════════════════════════════════════════════════════════════
class CsvTranscodeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(WIN_TITLE_CSV_TRANSCODE)
        self.resize(WIN_SIZE_CSV_TRANSCODE[0], WIN_SIZE_CSV_TRANSCODE[1])
        apply_dialog_window_flags(self)

        lt = QVBoxLayout(self)
        # 文件选择
        r0 = QHBoxLayout()
        r0.addWidget(QLabel(LABEL_CSV_FILE))
        self.file_edit = QLineEdit(); self.file_edit.setPlaceholderText("请选择CSV文件...")
        r0.addWidget(self.file_edit, 1)
        b = QPushButton(LABEL_CSV_BROWSE); b.clicked.connect(self._browse); r0.addWidget(b)
        b = QPushButton(LABEL_CSV_DETECT); b.setStyleSheet(BTN_INFO_QSS); b.clicked.connect(self._detect)
        r0.addWidget(b)
        lt.addLayout(r0)

        # 编码信息
        r1 = QHBoxLayout()
        r1.addWidget(QLabel(LABEL_CSV_ENCODING))
        self.enc_lbl = QLabel("--")
        self.enc_lbl.setStyleSheet(f"background:{T['hover']};padding:4px 8px;border-radius:4px;")
        r1.addWidget(self.enc_lbl)
        r1.addWidget(QLabel(LABEL_CSV_CONFIDENCE))
        self.conf_lbl = QLabel("--")
        self.conf_lbl.setStyleSheet(f"background:{T['hover']};padding:4px 8px;border-radius:4px;")
        r1.addWidget(self.conf_lbl)
        self.status_lbl = QLabel("请选择一个CSV文件")
        self.status_lbl.setStyleSheet(f"color:{T['text_secondary']};")
        r1.addWidget(self.status_lbl, 1)
        lt.addLayout(r1)

        # 表格
        lt.addWidget(QLabel(LABEL_CSV_TABLE_PREVIEW))
        self.tree = QTableWidget(); self.tree.setAlternatingRowColors(True)
        self.tree.setMaximumHeight(200); lt.addWidget(self.tree)

        # 原始文本
        lt.addWidget(QLabel(LABEL_CSV_RAW_PREVIEW))
        self.raw = QTextEdit(); self.raw.setReadOnly(True); self.raw.setFont(FONTS["default"])
        self.raw.setMaximumHeight(150); lt.addWidget(self.raw)

        # 按钮
        br = QHBoxLayout()
        b = QPushButton(LABEL_CSV_SAVE_AS); b.setStyleSheet(BTN_SUCCESS_QSS)
        b.clicked.connect(self._save); br.addWidget(b)
        b = QPushButton("关闭"); b.clicked.connect(self.accept); br.addWidget(b)
        br.addStretch(); lt.addLayout(br)

        self._enc = ""; self._headers = []; self._rows = []

    def _browse(self):
        fp, _ = QFileDialog.getOpenFileName(self, TITLE_CSV_OPEN, "", "CSV文件 (*.csv);;所有文件 (*.*)")
        if fp: self.file_edit.setText(fp); self._detect()

    def _detect(self):
        fp = self.file_edit.text().strip()
        if not fp or not os.path.isfile(fp):
            QMessageBox.warning(self, "提示", MSG_CSV_SELECT_FILE); return
        self.status_lbl.setText(MSG_CSV_DETECTING)
        enc, score = detect_csv_encoding(fp)
        conf = confidence_label(score)
        if enc: headers, rows, raw = read_csv_preview(fp, enc)
        else: enc, headers, rows, raw = "未知", [], [], "无法识别文件编码"
        self._enc = enc; self._headers = headers; self._rows = rows
        self.enc_lbl.setText(enc); self.conf_lbl.setText(conf)

        vh = headers[:25]; self.tree.setColumnCount(len(vh)); self.tree.setHorizontalHeaderLabels(vh)
        vr = rows[:50]; self.tree.setRowCount(len(vr))
        for r, rd in enumerate(vr):
            for c, v in enumerate(rd[:25]):
                it = QTableWidgetItem(_safe_str(v)); it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.tree.setItem(r, c, it)
        self.tree.resizeColumnsToContents()
        self.raw.clear()
        if raw:
            lines = [l[:250] for l in raw.splitlines()[:50]]
            self.raw.setPlainText("\n".join(lines))
        self.status_lbl.setText(MSG_CSV_DETECT_RESULT.format(enc=enc, conf=conf, count=len(rows)))

    def _save(self):
        fp = self.file_edit.text().strip(); enc = self._enc
        if not fp or not enc or enc == "未知":
            QMessageBox.warning(self, "提示", MSG_CSV_NO_FILE); return
        sp, _ = QFileDialog.getSaveFileName(self, TITLE_CSV_SAVE, "", "CSV文件 (*.csv)")
        if not sp: return
        try:
            total = transcode_csv_to_utf8(fp, enc, sp)
            QMessageBox.information(self, "成功", MSG_CSV_SUCCESS.format(enc=enc, total=total, path=sp))
            self.status_lbl.setText(MSG_CSV_EXPORTED.format(name=os.path.basename(sp)))
        except Exception as e:
            QMessageBox.critical(self, "转存失败", str(e))


# ═══════════════════════════════════════════════════════════════
# 4. DbViewerDialog — 数据库查看 & CSV导出（纯Qt）
# ═══════════════════════════════════════════════════════════════
class DbViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("数据库查看 & CSV导出 & 乱码转译")
        self.resize(1150, 700)
        apply_dialog_window_flags(self)
        self.db_path = ""; self.db_mode = "inner_sqlite"

        lt = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("数据库路径:"))
        self.db_entry = QLineEdit(); self.db_entry.setMinimumWidth(200); top.addWidget(self.db_entry, 1)
        b = QPushButton("选择DB文件"); b.clicked.connect(self._select_db); top.addWidget(b)
        top.addWidget(QLabel("数据表:"))
        self.tbl_combo = QComboBox(); self.tbl_combo.setMinimumWidth(150)
        self.tbl_combo.currentTextChanged.connect(self._on_table_changed); top.addWidget(self.tbl_combo)
        b = QPushButton("导出CSV"); b.clicked.connect(self._export_csv); top.addWidget(b)
        b = QPushButton("CSV乱码转译"); b.setStyleSheet(BTN_WARNING_QSS)
        b.clicked.connect(lambda: open_tool_dialog(self, CsvTranscodeDialog(self))); top.addWidget(b)
        b = QPushButton("关闭"); b.clicked.connect(self.accept); top.addWidget(b)
        lt.addLayout(top)

        self.table = QTableWidget(); self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True); lt.addWidget(self.table, 1)

    def _select_db(self):
        fp, _ = QFileDialog.getOpenFileName(self, "选择SQLite数据库", "", "SQLite (*.db *.sqlite *.sqlite3);;所有文件 (*.*)")
        if not fp: return
        self.db_path = fp; self.db_entry.setText(fp); self.db_mode = "inner_sqlite"
        try:
            conn = sqlite3.connect(fp, check_same_thread=False)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r[0] for r in cur.fetchall()]
            conn.close()
            self.tbl_combo.clear(); self.tbl_combo.addItems(tables)
            if tables: self._load_table(tables[0])
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _on_table_changed(self, name):
        if name and self.db_path: self._load_table(name)

    def _load_table(self, name):
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.cursor(); cur.execute(f'SELECT * FROM "{name}" LIMIT 500')
            rows = cur.fetchall(); cols = [d[0] for d in cur.description]; conn.close()
            self.table.setColumnCount(len(cols)); self.table.setHorizontalHeaderLabels(cols)
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, v in enumerate(row):
                    it = QTableWidgetItem(_safe_str(v)); it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(r, c, it)
            self.table.resizeColumnsToContents()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _export_csv(self):
        if self.table.rowCount() == 0: QMessageBox.information(self, "提示", "无数据"); return
        sp, _ = QFileDialog.getSaveFileName(self, "导出CSV", "", "CSV (*.csv)")
        if not sp: return
        try:
            with open(sp, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([self.table.horizontalHeaderItem(c).text() for c in range(self.table.columnCount())])
                for r in range(self.table.rowCount()):
                    w.writerow([self.table.item(r, c).text() if self.table.item(r, c) else "" for c in range(self.table.columnCount())])
            QMessageBox.information(self, "成功", f"已导出至：{sp}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))


# ═══════════════════════════════════════════════════════════════
# 5. CreateTableDialog — 自定义字段建表
# ═══════════════════════════════════════════════════════════════
class CreateTableDialog(QDialog):
    """自定义数据库表字段，自由创建数据表。"""

    DEFAULT_FIELDS = [
        ("企业名称", "TEXT"), ("岗位名称", "TEXT"), ("工作地点", "TEXT"),
        ("薪资范围", "TEXT"), ("学历要求", "TEXT"), ("工作经验", "TEXT"),
        ("岗位职责", "TEXT"), ("福利信息", "TEXT"), ("联系方式", "TEXT"),
        ("job_url", "TEXT"), ("source", "TEXT"),
    ]
    FIELD_TYPES = ["TEXT", "INTEGER", "REAL", "BLOB"]

    def __init__(self, parent, db_config):
        super().__init__(parent)
        self.setWindowTitle("新建自定义数据表")
        self.resize(560, 480)
        apply_dialog_window_flags(self)
        self.db_config = db_config
        self._build_ui()

    def _build_ui(self):
        ml = QVBoxLayout(self); ml.setSpacing(6)

        # ── 表名 ──
        nr = QHBoxLayout()
        nr.addWidget(QLabel("表名："))
        self.tbl_name_edit = QLineEdit(); self.tbl_name_edit.setPlaceholderText("输入新表名称")
        nr.addWidget(self.tbl_name_edit, 1); ml.addLayout(nr)

        # ── 字段编辑区 ──
        ml.addWidget(QLabel("字段定义（字段名 + 类型）："))
        self.field_table = QTableWidget(0, 3)
        self.field_table.setHorizontalHeaderLabels(["字段名", "类型", ""])
        self.field_table.horizontalHeader().setStretchLastSection(False)
        self.field_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.field_table.setColumnWidth(1, 90)
        self.field_table.setColumnWidth(2, 50)
        ml.addWidget(self.field_table, 1)

        # ── 操作按钮 ──
        br = QHBoxLayout()
        b = QPushButton("＋ 添加字段"); b.clicked.connect(self._add_field_row); br.addWidget(b)
        b = QPushButton("📋 使用默认字段模板"); b.clicked.connect(self._load_default_template); br.addWidget(b)
        br.addStretch(); ml.addLayout(br)

        # ── 从行业模板载入列（2026-09-07 新增） ──
        tr = QHBoxLayout()
        tr.addWidget(QLabel("行业模板："))
        self.tpl_combo = QComboBox(); self.tpl_combo.setMinimumWidth(160)
        tr.addWidget(self.tpl_combo, 1)
        b = QPushButton("载入模板列"); b.setStyleSheet(BTN_INFO_QSS)
        b.clicked.connect(self._load_template_preset); tr.addWidget(b)
        ml.addLayout(tr)
        self._reload_tpl_combo()

        # ── 底部 ──
        br2 = QHBoxLayout()
        b = QPushButton("创建表"); b.setStyleSheet(BTN_SUCCESS_QSS)
        b.clicked.connect(self._do_create); br2.addWidget(b)
        b = QPushButton("取消"); b.clicked.connect(self.reject); br2.addWidget(b)
        ml.addLayout(br2)

    def _add_field_row(self, name="", dtype="TEXT"):
        row = self.field_table.rowCount(); self.field_table.insertRow(row)
        # 字段名
        name_edit = QLineEdit(); name_edit.setText(name); name_edit.setPlaceholderText("字段名")
        self.field_table.setCellWidget(row, 0, name_edit)
        # 类型下拉
        type_combo = QComboBox(); type_combo.addItems(self.FIELD_TYPES)
        type_combo.setCurrentText(dtype)
        self.field_table.setCellWidget(row, 1, type_combo)
        # 删除按钮
        del_btn = QPushButton("✕"); del_btn.setFixedWidth(40)
        del_btn.setStyleSheet(f"color:{T['danger']};font-weight:bold;")
        del_btn.clicked.connect(lambda: self._del_field_row(del_btn))
        self.field_table.setCellWidget(row, 2, del_btn)

    def _del_field_row(self, btn):
        for r in range(self.field_table.rowCount()):
            if self.field_table.cellWidget(r, 2) is btn:
                self.field_table.removeRow(r); return

    def _load_default_template(self):
        self.field_table.setRowCount(0)
        for name, dtype in self.DEFAULT_FIELDS:
            self._add_field_row(name, dtype)

    # ── 从行业模板载入列（2026-09-07 新增） ──
    def _reload_tpl_combo(self):
        """填行业模板下拉。**键放 itemData，绝不按显示文本取 spec**。

        原来用 currentText() 去 load_table_template —— 而该函数匹配"取最后一个命中的"，用户在
        `用户字段配置/` 放一份与自带同名的模板时，下拉里会出现**两行文本完全相同**的项，两行都
        解析到用户那份，**选不回自带的**。改成与主窗「模板：」下拉同款：itemData 存来源限定键。

        数据源用 list_table_configs()：它带键，且会把停用/写坏的也列出来（灰度标出），
        而 list_table_templates() 对停用项直接不返回 —— 用户会以为自己的模板"导完就消失了"。
        """
        import config
        try:
            rows = config.list_table_configs()
        except Exception:
            rows = []
        self.tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        self.tpl_combo.addItem("— 选择行业模板 —", "")
        for r in rows:
            tag = "[自带]" if r["source"] == "builtin" else "[用户]"
            text = "%s %s" % (tag, r["name"])
            if not r["ok"]:
                text += "（写坏了）"
            elif not r["enabled"]:
                text += "（已停用）"
            self.tpl_combo.addItem(text, r["key"])
            i = self.tpl_combo.count() - 1
            tip = "%s\n%s" % (r["key"], r["path"])
            if not r["ok"]:
                tip += "\n校验未通过：" + "；".join(r["errors"][:3])
                self.tpl_combo.setItemData(i, QBrush(T["muted"]), Qt.ItemDataRole.ForegroundRole)
            elif not r["enabled"]:
                tip += "\n已停用（到主界面「配置管理」里重新启用）"
                self.tpl_combo.setItemData(i, QBrush(T["muted"]), Qt.ItemDataRole.ForegroundRole)
            self.tpl_combo.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        self.tpl_combo.setCurrentIndex(0)
        self.tpl_combo.blockSignals(False)

    def _load_template_preset(self):
        key = self.tpl_combo.currentData(Qt.ItemDataRole.UserRole)
        if not key:
            QMessageBox.information(self, "提示", "请先在“行业模板”下拉选择一个模板")
            return
        import config
        row, spec = config.get_field_config(key)
        if not spec:
            QMessageBox.warning(self, "提示", f"模板 [{key}] 加载失败")
            return
        if not row["ok"]:
            QMessageBox.warning(self, "提示", f"模板 [{row['name']}] 校验没通过：\n"
                                              + "；".join(row["errors"][:5]))
            return
        if not row["enabled"]:
            QMessageBox.warning(self, "提示", f"模板 [{row['name']}] 已停用。\n"
                                              "请到主界面「配置管理」里重新启用后再载入。")
            return
        fields = config.template_create_fields(spec)
        self.field_table.setRowCount(0)
        for fname, dtype in fields:
            self._add_field_row(fname, dtype)
        QMessageBox.information(self, "载入完成",
                                f"已载入模板【{spec.get('name')}】共 {len(fields)} 列，可继续增删改")

    def _get_fields(self):
        fields = []
        for r in range(self.field_table.rowCount()):
            name_w = self.field_table.cellWidget(r, 0)
            type_w = self.field_table.cellWidget(r, 1)
            if name_w and type_w:
                name = name_w.text().strip()
                if name:
                    fields.append((name, type_w.currentText()))
        return fields

    def _do_create(self):
        tbl_name = self.tbl_name_edit.text().strip()
        if not tbl_name:
            QMessageBox.warning(self, "提示", "请输入表名"); return
        fields = self._get_fields()
        if not fields:
            QMessageBox.warning(self, "提示", "至少需要定义一个字段"); return

        cfg = self.db_config
        err = create_custom_table(cfg["mode"], cfg["host"], cfg["user"], cfg["pwd"],
                                  cfg["db"], cfg.get("db_file", ""), tbl_name, fields)
        if err and err != "success":
            QMessageBox.critical(self, "创建失败", f"无法创建表：{err}")
        else:
            QMessageBox.information(self, "成功", f"表 [{tbl_name}] 创建成功！")
            self.accept()


# ═══════════════════════════════════════════════════════════════
# 6. ManualInputDialog — 手动数据录入（纯Qt，最复杂弹窗）
# ═══════════════════════════════════════════════════════════════
class ManualInputDialog(QDialog):
    """单条文本抽取 + 自动/手动关键词提取 + 数据库入库。"""

    # 线程安全信号（跨线程 → 主线程 UI 更新）
    log_signal = Signal(str)

    def __init__(self, parent, db_config, data_list=None):
        super().__init__(parent)
        self.setWindowTitle(WIN_TITLE_MANUAL_INPUT)
        self.resize(WIN_SIZE_MANUAL_INPUT[0], WIN_SIZE_MANUAL_INPUT[1])
        apply_dialog_window_flags(self)

        # 数据库配置
        self.db_mode   = db_config.get("mode", VAR_DEFAULT_MODE)
        self.db_host   = db_config.get("host", DB_HOST_DEFAULT)
        self.db_user   = db_config.get("user", DB_USER_DEFAULT)
        self.db_pwd    = db_config.get("pwd", "")
        self.db_name   = db_config.get("db", DB_NAME_DEFAULT)
        self.db_table  = db_config.get("table", DB_TABLE_DEFAULT)
        self.db_file   = get_sqlite_real_path(self.db_name)

        # 数据状态
        self.raw_data_list = data_list or []
        self.cur_select_idx = -1
        self.rows = []          # [(QWidget, dict[str, QLineEdit])]
        self.manual_result = ""
        self.current_table = ""
        self.current_columns = []  # 当前选中表的列名列表（动态读取）
        self.running = True

        # 线程池
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)

        self._build_ui()
        self._init_load()

    # ═══════════════════════════════════════════════════
    # UI 构建
    # ═══════════════════════════════════════════════════
    def _build_ui(self):
        ml = QVBoxLayout(self); ml.setSpacing(4)

        # ── JSON 路径栏 ──
        jr = QHBoxLayout()
        jr.addWidget(QLabel(LABEL_JSON_FILE_PATH))
        self.json_path_entry = QLineEdit(); jr.addWidget(self.json_path_entry, 1)
        b = QPushButton(LABEL_BROWSE); b.clicked.connect(self._browse_json); jr.addWidget(b)
        b = QPushButton(LABEL_LOAD_JSON); b.setStyleSheet(BTN_SUCCESS_QSS)
        b.clicked.connect(self._load_json); jr.addWidget(b)
        ml.addLayout(jr)

        # ── 条目选择器 ──
        sr = QHBoxLayout(); sr.setSpacing(4)
        sr.addWidget(QLabel(LABEL_SELECT_ITEM))
        self.item_combo = QComboBox(); self.item_combo.setMinimumWidth(300)
        self.item_combo.currentIndexChanged.connect(self._on_item_selected); sr.addWidget(self.item_combo, 1)
        b = QPushButton(LABEL_DELETE_CURRENT); b.setStyleSheet(f"color:{T['danger']};")
        b.clicked.connect(self._delete_current_item); sr.addWidget(b)
        sr.addWidget(QLabel(LABEL_BATCH_CHARS))
        self.batch_entry = QLineEdit("512"); self.batch_entry.setFixedWidth(72); sr.addWidget(self.batch_entry)
        sr.addWidget(QLabel(LABEL_MANUAL_KEYWORDS))
        self.manual_key_entry = QLineEdit(); self.manual_key_entry.setMaximumWidth(180); sr.addWidget(self.manual_key_entry)
        b = QPushButton(LABEL_AUTO_EXTRACT); b.setStyleSheet(BTN_PRIMARY_QSS)
        b.clicked.connect(self._auto_extract); sr.addWidget(b)
        b = QPushButton(LABEL_MANUAL_EXTRACT); b.setStyleSheet(BTN_PURPLE_QSS)
        b.clicked.connect(self._manual_extract); sr.addWidget(b)
        b = QPushButton(LABEL_CONFIRM_TO_FORM); b.setStyleSheet(BTN_WARNING_QSS)
        b.clicked.connect(self._confirm_to_form); sr.addWidget(b)
        b = QPushButton(LABEL_SAVE_TO_DB); b.clicked.connect(self._save_selected_item); sr.addWidget(b)
        b = QPushButton(LABEL_CLEAR_FORM); b.clicked.connect(self._clear_form); sr.addWidget(b)
        self.btn_toggle = QPushButton("解除独占"); self.btn_toggle.setStyleSheet(BTN_DANGER_QSS)
        self.btn_toggle.clicked.connect(self._toggle_grab); sr.addWidget(self.btn_toggle)
        ml.addLayout(sr)

        # ── 预览 & 手动结果 ──
        spl = QSplitter(Qt.Orientation.Horizontal)
        pg = QGroupBox("当前条目清洗后文本"); pl = QVBoxLayout(pg)
        self.preview_text = QTextEdit(); self.preview_text.setReadOnly(True)
        self.preview_text.setFont(FONTS["default"]); pl.addWidget(self.preview_text); spl.addWidget(pg)

        mg = QGroupBox("手动提取结果预览"); ml2 = QVBoxLayout(mg)
        self.manual_result_text = QTextEdit(); self.manual_result_text.setReadOnly(True)
        self.manual_result_text.setFont(FONTS["default"]); ml2.addWidget(self.manual_result_text)
        spl.addWidget(mg); ml.addWidget(spl, 2)

        # ── 表单 + 数据库表 ──
        bs = QSplitter(Qt.Orientation.Horizontal)
        # 左侧：表单
        fw = QWidget(); fl = QVBoxLayout(fw); fl.setAlignment(Qt.AlignmentFlag.AlignTop)
        fbr = QHBoxLayout()
        b = QPushButton("当前表单入库"); b.setStyleSheet(BTN_SUCCESS_QSS)
        b.clicked.connect(self._add_current_row_to_db); fbr.addWidget(b)
        b = QPushButton(LABEL_ADD_ROW); b.clicked.connect(self._add_new_row); fbr.addWidget(b)
        b = QPushButton(LABEL_CLEAR_ALL_ROWS); b.clicked.connect(self._clear_all_rows); fbr.addWidget(b)
        fl.addLayout(fbr)
        self.row_container = QWidget(); self.row_layout = QVBoxLayout(self.row_container)
        self.row_layout.setAlignment(Qt.AlignmentFlag.AlignTop); self.row_layout.setContentsMargins(0, 0, 0, 0)
        sa = QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(self.row_container)
        fl.addWidget(sa, 1); bs.addWidget(fw)

        # 右侧：数据库表
        dw = QWidget(); dl = QVBoxLayout(dw)
        dg = QGridLayout()
        dg.addWidget(QLabel(LABEL_CURRENT_TABLE), 0, 0)
        self.db_combo = QComboBox(); self.db_combo.setMinimumWidth(150); dg.addWidget(self.db_combo, 0, 1)
        b = QPushButton(LABEL_REFRESH_TABLES); b.clicked.connect(self._refresh_table_list); dg.addWidget(b, 0, 2)
        b = QPushButton("＋ 新建表"); b.setStyleSheet(BTN_PRIMARY_QSS)
        b.clicked.connect(self._open_create_table_dialog); dg.addWidget(b, 0, 3)
        dg.addWidget(QLabel(LABEL_DATA_TABLE), 1, 0)
        self.table_combo = QComboBox(); self.table_combo.setMinimumWidth(150)
        self.table_combo.currentTextChanged.connect(self._on_table_selected); dg.addWidget(self.table_combo, 1, 1)
        dl.addLayout(dg)

        tbr = QHBoxLayout()
        b = QPushButton(LABEL_TABLE_ADD_EMPTY); b.clicked.connect(self._table_add_empty); tbr.addWidget(b)
        b = QPushButton(LABEL_TABLE_DEL_SELECTED); b.setStyleSheet(f"color:{T['danger']};")
        b.clicked.connect(self._table_del_selected); tbr.addWidget(b); tbr.addStretch()
        dl.addLayout(tbr)

        self.db_tree = QTableWidget(); self.db_tree.setAlternatingRowColors(True)
        self.db_tree.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # 水平/垂直滚动条：内容超出时自动出现
        self.db_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.db_tree.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.db_tree.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 表头可手动拖拽调整列宽，不强制最后一列撑满
        self.db_tree.horizontalHeader().setStretchLastSection(False)
        dl.addWidget(self.db_tree, 1); bs.addWidget(dw)
        ml.addWidget(bs, 4)

        # ── 底部按钮 ──
        bbr = QHBoxLayout()
        b = QPushButton(LABEL_SUBMIT_ALL); b.setStyleSheet(BTN_SUCCESS_QSS)
        b.clicked.connect(self._submit_all); bbr.addWidget(b)
        bbr.addStretch()
        b = QPushButton(LABEL_CLOSE_WINDOW); b.clicked.connect(self._on_close); bbr.addWidget(b)
        ml.addLayout(bbr)

        # ── 日志 ──
        ml.addWidget(QLabel(LABEL_OPERATION_LOG))
        self.log_box = QTextEdit(); self.log_box.setReadOnly(True); self.log_box.setMaximumHeight(100)
        self.log_box.setFont(FONTS["mono"]); ml.addWidget(self.log_box)

        # 信号（QueuedConnection 确保跨线程安全）
        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self._is_grabbed = False

    # ═══════════════════════════════════════════════════
    # 日志
    # ═══════════════════════════════════════════════════
    def log(self, msg):
        self.log_signal.emit(msg)

    def _append_log(self, msg):
        self.log_box.append(msg)

    # ═══════════════════════════════════════════════════
    # 初始化
    # ═══════════════════════════════════════════════════
    def _init_load(self):
        self._refresh_item_combo()
        self._refresh_table_list()
        self._add_new_row()

    # ═══════════════════════════════════════════════════
    # 数据表模板（JSON 文件驱动：字段名取自绑定的模板文件）
    # ═══════════════════════════════════════════════════
    def _bound_template_spec(self):
        """返回当前绑定模板的说明（engine=schema_v2）；未绑定/招聘默认 → None。
        每次按名重新读 JSON 文件（改文件即生效），读不到再退回 ACTIVE_SCHEMA 内存副本。"""
        import config
        active = getattr(config, "ACTIVE_SCHEMA", None)
        if not active or active.get("engine") != "schema_v2":
            return None
        spec = active
        try:
            name = str(active.get("name") or "")
            if name:
                reloaded = config.load_table_template(name)
                if reloaded and reloaded.get("columns"):
                    spec = reloaded
        except Exception:
            pass
        return spec

    def _fallback_field_names(self):
        """无已选表时表单字段名兜底：优先绑定模板 JSON 的列名（数据驱动，方便他人加模板），
        否则回招聘默认 INPUT_FIELD_LIST。返回列名列表。"""
        spec = self._bound_template_spec()
        if spec:
            names = [str(c.get("name", "")).strip() for c in (spec.get("columns") or [])]
            names = [n for n in names if n]
            if names:
                return names
        return [f[0] for f in INPUT_FIELD_LIST]

    def _refresh_item_combo(self):
        self.item_combo.blockSignals(True)
        self.item_combo.clear()
        if self.raw_data_list:
            self.item_combo.addItems([f"第{i+1}条" for i in range(len(self.raw_data_list))])
            self.item_combo.setCurrentIndex(0)
        self.item_combo.blockSignals(False)
        if self.raw_data_list:
            self._on_item_selected(0)

    def _on_item_selected(self, idx):
        if idx < 0 or idx >= len(self.raw_data_list):
            self.preview_text.clear(); self.manual_result_text.clear(); return
        self.cur_select_idx = idx
        data = self.raw_data_list[idx]
        # 模板抽取产物（wrapper 带 clean_text）：直接预览结构化文本，而非整包 dict 的 repr
        clean = (data.get("clean_text")
                 if isinstance(data, dict) and data.get("clean_text")
                 else _text_cleaner.clean(str(data)))
        self.preview_text.setPlainText(clean)
        self._clear_form()
        self.manual_result_text.clear()
        self.manual_result = ""
        self.log(f"已切换至第{idx+1}条条目")

    # ═══════════════════════════════════════════════════
    # 自定义建表
    # ═══════════════════════════════════════════════════
    def _open_create_table_dialog(self):
        dlg = CreateTableDialog(self, {
            "mode": self.db_mode, "host": self.db_host, "user": self.db_user,
            "pwd": self.db_pwd, "db": self.db_name,
            "db_file": self.db_file,
        })
        # 非模态打开；"建表成功才刷新表名列表"改挂 finished 信号
        def _done(code):
            if code == QDialog.DialogCode.Accepted:
                self._refresh_table_list()
        open_tool_dialog(self, dlg, on_finished=_done)

    # ═══════════════════════════════════════════════════
    # JSON 操作
    # ═══════════════════════════════════════════════════
    def _browse_json(self):
        fp, _ = QFileDialog.getOpenFileName(self, TITLE_JSON_OPEN, "", FILE_FILTER_JSON)
        if fp: self.json_path_entry.setText(fp)

    def _load_json(self):
        fp = self.json_path_entry.text().strip()
        if not fp or not os.path.exists(fp):
            self.log(LOG_FILE_NOT_EXIST); return
        try:
            with open(fp, "r", encoding="utf-8") as f:
                nd = json.load(f)
            if isinstance(nd, list): self.raw_data_list.extend(nd)
            elif isinstance(nd, dict): self.raw_data_list.append(nd)
            self.log(LOG_JSON_PARSED.format(count=len(nd) if isinstance(nd, list) else 1))
            self._refresh_item_combo()
        except Exception as e:
            self.log(LOG_JSON_ERR.format(e=str(e)))

    def _delete_current_item(self):
        if 0 <= self.cur_select_idx < len(self.raw_data_list):
            del self.raw_data_list[self.cur_select_idx]
            self.log(f"已删除第{self.cur_select_idx+1}条")
            self._refresh_item_combo()

    # ═══════════════════════════════════════════════════
    # 提取
    # ═══════════════════════════════════════════════════
    def _auto_extract(self):
        if self.cur_select_idx < 0: self.log("请先选择条目"); return
        try: limit = int(self.batch_entry.text().strip()); assert limit > 0
        except (ValueError, AssertionError): limit = 512; self.batch_entry.setText("512"); self.log("字符数非法，已恢复512")

        def task():
            import config
            item = self.raw_data_list[self.cur_select_idx]

            # ── 抽取方案与表单同源 ──
            # 表单侧（_add_new_row / _fallback_field_names）走的是 _bound_template_spec()，
            # 它每次按名**从盘上重读 JSON**；这里原先读的是裸 config.ACTIVE_SCHEMA 内存快照，
            # 改了模板文件后「抽取用的列」与「表单显示的列」会不一致。统一走前者。
            spec = self._bound_template_spec()
            if spec:
                cols = [c for c in (spec.get("columns") or []) if isinstance(c, dict)]
                plan = f"配置【{spec.get('name') or '?'}】"
                producible = {str(c.get("name") or "").strip() for c in cols}
                bound = True
            else:
                cols = []
                plan = "招聘（默认老链路）"
                producible = set(QianLongResult.__slots__)
                bound = False
            producible.discard("")

            # ── 系统列不参与判定 ──
            # job_url / source 是**每张表都有**的系统列，两边恒有 → 若算进交集，
            # 任何表都能「命中 2 列」而蒙混过关，预检就白做了。
            # 这正是 site_diag._TEXT_SYS_COLS 当初踩过的同一个坑（不算进去，
            # 「否则任何页恒定命中 2 列」）。
            syscols = set(getattr(config, "SYSTEM_COLUMNS", None) or ()) or {"job_url", "source"}
            fields = list(self.current_columns or self._fallback_field_names())
            biz_fields = [f for f in fields if f not in syscols]
            biz_producible = producible - syscols

            self._pending_plan = plan
            self._pending_fields = fields

            # ── 预检：这套方案能产出的**业务**字段，和表单要的，一个都对不上？ ──
            # 典型场景：没绑配置（默认「招聘」）却对着「车型/价格/里程」这类表点按钮。
            # 老链路只填 QianLongResult.__slots__ 里那 8 个招聘字段，其余字段 getattr 恒为空，
            # 原来会**静默填空再谎报「已填入表单」**。这里改成明确拒绝并指路。
            if not [f for f in biz_fields if f in biz_producible]:
                self._pending_extract_res = None
                self._pending_extract_is_dict = False
                self._pending_chunk_count = 0
                self._pending_block = (plan, biz_fields, bound)
                QMetaObject.invokeMethod(
                    self, "_on_extract_done",
                    Qt.ConnectionType.QueuedConnection)
                return
            self._pending_block = None

            if spec:
                # 条目可能是整包 wrapper 或内层行字典：统一取 抽取文本 + url/site ctx
                if isinstance(item, dict):
                    row = item.get("qianlong_extract_result")
                    row = row if isinstance(row, dict) else item
                    text = item.get("clean_text") or _text_cleaner.clean(str(item))
                    url = item.get("job_url") or row.get("job_url") or ""
                    site = item.get("source") or row.get("source") or ""
                else:
                    row, text = None, _text_cleaner.clean(str(item))
                    url = site = ""
                ctx = {"url": url, "site": site} if (url or site) else None
                # 按当前模板列名+同义词 通用抽取（键=模板表列名）
                try:
                    from schema_engine import schema_extract
                    res = schema_extract(text, cols, ctx=ctx)
                    # 复核重抽时保留原产物里已填的值，只补空格——
                    # 若 flow 已靠页面标题(DOM)填了 车型 等，而纯文本重抽认不到，
                    # 不应把好值覆盖成空。
                    if isinstance(res, dict) and isinstance(row, dict) and row:
                        for c in cols:
                            kn = c.get("name") if isinstance(c, dict) else c
                            if not kn:
                                continue
                            have = str((row.get(kn) if isinstance(row, dict) else "") or "").strip()
                            fresh = str(res.get(kn) or "").strip()
                            res[kn] = have or fresh
                    self._pending_extract_res = res
                    self._pending_extract_is_dict = True
                    self._pending_chunk_count = 1
                except Exception as e:
                    self.log(f"按模板抽取失败：{e}")
                    self._pending_extract_res = None
                    self._pending_extract_is_dict = False
                    self._pending_chunk_count = 0
            else:
                clean = _text_cleaner.clean(str(item))
                chunks = _text_cleaner.split_by_length(clean, limit)
                res = _qianlong.extract_by_batch(item, chunks)
                self._pending_extract_res = res
                self._pending_extract_is_dict = False
                self._pending_chunk_count = len(chunks)
            QMetaObject.invokeMethod(
                self, "_on_extract_done",
                Qt.ConnectionType.QueuedConnection)
        self.executor.submit(task)

    @Slot()
    def _on_extract_done(self):
        """主线程：自动抽取结果填入表单（兼容 dict=模板结果 / 对象=招聘结果）"""
        if not self.running: return

        # 预检拦下（方案与表单的业务字段一个都对不上）——不填、不谎报
        blocked = getattr(self, '_pending_block', None)
        if blocked:
            plan, biz_fields, bound = blocked
            self._pending_block = None
            show = "、".join(biz_fields[:6]) + ("…" if len(biz_fields) > 6 else "")
            if bound:
                self.log(f"未抽取：当前绑的是「{plan}」，它的字段与这张表的 "
                         f"{len(biz_fields)} 个业务字段（{show}）一个都对不上。"
                         f"请在主窗「抓取规则：」下拉里换一套与这张表匹配的配置。")
            else:
                self.log(f"未抽取：当前是「{plan}」，只认招聘字段，与这张表的 "
                         f"{len(biz_fields)} 个业务字段（{show}）一个都对不上。"
                         f"请先在主窗「抓取规则：」下拉里选一套覆盖这些字段的配置，再点本按钮。")
            return

        res = getattr(self, '_pending_extract_res', None)
        chunk_count = getattr(self, '_pending_chunk_count', 0)
        is_dict = getattr(self, '_pending_extract_is_dict', False)
        if res is None: return
        if not self.rows: self._add_new_row()
        _, wids = self.rows[0]
        # 使用当前表的列名进行字段映射（无表时按绑定模板 JSON 字段名兜底）
        fields = (getattr(self, '_pending_fields', None)
                  or (self.current_columns if self.current_columns else self._fallback_field_names()))
        filled = 0
        for field in fields:
            if field in wids:
                val = res.get(field, "") if is_dict else getattr(res, field, "")
                s = "" if val is None else str(val)
                wids[field].setText(s)
                if s.strip(): filled += 1
        # 如实报数：填了几个就是几个。原来无论填没填上都说「已填入表单」。
        plan = getattr(self, '_pending_plan', "")
        dropped = [f for f in fields if f not in wids]
        if filled:
            msg = f"自动抽取完成（{plan}，共{chunk_count}批）：{filled}/{len(fields)} 个字段填入"
            if dropped:
                msg += f"；另有 {len(dropped)} 个不在当前表里未填：{'、'.join(dropped[:5])}"
            self.log(msg)
        else:
            self.log(f"自动抽取完成（{plan}，共{chunk_count}批）：0/{len(fields)} 个字段有值"
                     f" —— 一个字都没填上")

    def _manual_extract(self):
        if self.cur_select_idx < 0: self.log("请先选择条目"); return
        kt = self.manual_key_entry.text().strip()
        if not kt: self.log("请输入手动关键词"); return
        keys = kt.split()

        def task():
            clean = _text_cleaner.clean(str(self.raw_data_list[self.cur_select_idx]))
            matches = []
            for k in keys:
                if k in clean:
                    idx = clean.find(k)
                    matches.append(clean[max(0,idx-50):min(len(clean),idx+len(k)+50)])
            self.manual_result = "\n---\n".join(matches) if matches else "无匹配内容"
            QMetaObject.invokeMethod(
                self, "_on_manual_extract_done",
                Qt.ConnectionType.QueuedConnection)
        self.executor.submit(task)

    @Slot()
    def _on_manual_extract_done(self):
        """主线程：显示手动提取结果"""
        if not self.running: return
        self.manual_result_text.setPlainText(self.manual_result)
        self.log("手动关键词提取完成")

    def _confirm_to_form(self):
        if not self.manual_result: self.log("请先进行手动关键词提取"); return
        if not self.rows: self._add_new_row()
        _, wids = self.rows[0]
        # 优先填入第一个 TEXT 类型字段（兼容自定义表）
        cols = self.current_columns if self.current_columns else self._fallback_field_names()
        if len(cols) > 0 and cols[0] in wids:
            wids[cols[0]].setText(self.manual_result[:100])
        if len(cols) > 1 and cols[1] in wids:
            wids[cols[1]].setText(self.manual_result)
        self.log("手动提取结果已确认填入表单")

    # ═══════════════════════════════════════════════════
    # 表单行管理
    # ═══════════════════════════════════════════════════
    def _add_new_row(self):
        frame = QWidget()
        fl = QVBoxLayout(frame); fl.setContentsMargins(0, 0, 0, 4)
        wids = {}
        # 使用当前表的列名；若未选中表则回退到默认字段（绑定模板时=模板 JSON 字段名）
        fields = self.current_columns if self.current_columns else self._fallback_field_names()
        for field in fields:
            fr = QHBoxLayout()
            fr.addWidget(QLabel(f"{field}："))
            le = QLineEdit(); fr.addWidget(le, 1); fl.addLayout(fr)
            wids[field] = le
        dbr = QHBoxLayout()
        dbr.addStretch()
        idx = len(self.rows)
        b = QPushButton(LABEL_DELETE_ROW); b.setStyleSheet(f"color:{T['danger']};")
        b.clicked.connect(lambda: self._del_row(idx)); dbr.addWidget(b)
        fl.addLayout(dbr)
        self.row_layout.addWidget(frame)
        self.rows.append((frame, wids))
        self.log("新增录入行")

    def _del_row(self, idx):
        if 0 <= idx < len(self.rows):
            f, _ = self.rows.pop(idx); f.hide(); f.deleteLater()
            self.log("删除录入行")

    def _clear_form(self):
        for f, wids in self.rows:
            for w in wids.values(): w.clear()
        self.manual_result_text.clear(); self.manual_result = ""

    def _clear_all_rows(self):
        for f, _ in self.rows: f.hide(); f.deleteLater()
        self.rows.clear(); self.log("已清空所有录入行")
        self._add_new_row()

    def _rebuild_form_rows(self):
        """根据当前表的列名重建所有表单行。"""
        # 保存第一行的数据以便恢复
        old_data = {}
        if self.rows:
            _, wids = self.rows[0]
            old_data = {k: v.text() for k, v in wids.items()}
        # 清除所有行
        for f, _ in self.rows: f.hide(); f.deleteLater()
        self.rows.clear()
        # 新建一行并尝试恢复匹配的字段值
        self._add_new_row()
        if old_data and self.rows:
            _, wids = self.rows[0]
            for col in self.current_columns:
                if col in old_data and col in wids:
                    wids[col].setText(old_data[col])
        self.log(f"表单已适配表【{self.current_table}】（{len(self.current_columns)} 个字段）")

    # ═══════════════════════════════════════════════════
    # 数据库操作
    # ═══════════════════════════════════════════════════
    def _refresh_table_list(self):
        def task():
            try:
                # 自动创建数据库（SQLite 模式，首次使用）
                if self.db_mode == "inner_sqlite":
                    real_db = get_sqlite_real_path(self.db_name)
                    if not os.path.exists(real_db):
                        self.log("数据库文件不存在，正在自动创建…")
                        err = db_operate(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                                        self.db_name, self.db_table, lambda db: None)
                        if err:
                            self.log(f"[警告] 数据库创建失败: {err}")
                        else:
                            self.log(f"数据库及默认表 [{self.db_table}] 已创建")
                self._pending_tables = get_table_list(
                    self.db_mode, self.db_host, self.db_user, self.db_pwd,
                    self.db_name, self.db_file)
            except Exception as e:
                self._pending_tables = []
                self.log(f"[错误] 刷新表列表失败: {str(e)}")
            QMetaObject.invokeMethod(
                self, "_on_table_list_ready",
                Qt.ConnectionType.QueuedConnection)
        self.executor.submit(task)

    @Slot()
    def _on_table_list_ready(self):
        """主线程：更新表列表下拉框"""
        if not self.running: return
        tables = list(getattr(self, '_pending_tables', []) or [])
        # 绑定行业模板时优先切到“模板对应的数据表”：表单字段名=模板 JSON 列名，
        # 使 自动抽取/保存 全部落在模板字段上；缺表则按 JSON 模板就地自动建表（数据驱动，无需预建）。
        spec = self._bound_template_spec()
        if spec:
            tname = str(spec.get("name") or "").strip()
            if tname and tname not in tables:
                try:
                    import config_gui as _cg
                    err = _cg.create_table_from_template(
                        self.db_mode, self.db_host, self.db_user, self.db_pwd,
                        self.db_name, self.db_file, tname, spec)
                    if err and err != "success":
                        self.log(f"按模板自动建表【{tname}】失败：{err}")
                    else:
                        tables.append(tname)
                        self.log(f"按模板【{spec.get('name')}】自动建表【{tname}】成功")
                except Exception as e:
                    self.log(f"按模板自动建表异常：{e}")
            if tname in tables:
                self.table_combo.blockSignals(True)
                self.table_combo.clear(); self.table_combo.addItems(tables)
                self.table_combo.setCurrentIndex(tables.index(tname))
                self.table_combo.blockSignals(False)
                self.current_table = tname
                self.log(f"已绑定模板【{spec.get('name')}】，当前表自动切换为【{tname}】")
                self._load_table_data()
                return
        self.table_combo.blockSignals(True)
        self.table_combo.clear(); self.table_combo.addItems(tables)
        self.table_combo.blockSignals(False)
        if tables:
            self.log(f"加载数据表列表：{tables}")
            # ★ setCurrentIndex 可能不触发 currentTextChanged（Qt 可能已自动选中 index 0）
            # 因此手动设置 current_table 并加载，不依赖信号
            self.table_combo.setCurrentIndex(0)
            self.current_table = tables[0]
            self._load_table_data()
        else:
            self.log("未读取到任何数据表，请检查数据库配置或连接")

    def _on_table_selected(self, name):
        self.current_table = name
        self._load_table_data()

    def _load_table_data(self):
        if not self.current_table or not self.running: return
        cur_table = self.current_table
        def task():
            cols, data = [], []
            try:
                cols, data = load_table_data(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                                             self.db_name, self.db_file, cur_table)
            except Exception as e:
                self.log(f"[错误] 加载表【{cur_table}】数据失败: {str(e)}")
            # ★ 无论成功与否都通知主线程更新，避免表格永远空白
            self._pending_cols = cols
            self._pending_rows = data
            QMetaObject.invokeMethod(
                self, "_on_table_data_ready",
                Qt.ConnectionType.QueuedConnection)
        self.executor.submit(task)

    @Slot()
    def _on_table_data_ready(self):
        """主线程：更新数据表显示 + 动态重建表单字段"""
        if not self.running: return
        cols = getattr(self, '_pending_cols', [])
        data = getattr(self, '_pending_rows', [])
        # 如果表的列名发生变化（切换了表），重建表单
        if cols and cols != self.current_columns:
            self.current_columns = cols
            self._rebuild_form_rows()
        self.db_tree.setColumnCount(len(cols))
        self.db_tree.setHorizontalHeaderLabels(cols)
        self.db_tree.setRowCount(len(data))
        for r, row in enumerate(data):
            for c, v in enumerate(row):
                it = QTableWidgetItem(_safe_str(v))
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.db_tree.setItem(r, c, it)
        self.db_tree.resizeColumnsToContents()
        if data:
            self.log(f"表【{self.current_table}】加载完成，共{len(data)}条记录")
        else:
            self.log(f"表【{self.current_table}】已加载，当前无数据（{len(cols)} 个字段）")

    def _save_selected_item(self):
        if self.cur_select_idx < 0: self.log("无有效条目"); return
        if not self.current_table: self.log("请先选择数据表"); return
        data = self.raw_data_list[self.cur_select_idx]
        def task():
            err = db_operate(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                            self.db_name, self.current_table, lambda db: db.insert(data))
            if err: self.log(f"入库失败：{err}")
            else:
                self.log(f"第{self.cur_select_idx+1}条入库到表【{self.current_table}】成功")
                self._load_table_data()
        self.executor.submit(task)

    def _add_current_row_to_db(self):
        if not self.rows: self.log("无表单数据"); return
        if not self.current_table: self.log("请先选择数据表"); return
        _, wids = self.rows[0]
        row_data = {k: v.text().strip() for k, v in wids.items()}
        def task():
            err = db_operate(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                            self.db_name, self.current_table, lambda db: db.insert(row_data))
            if err: self.log(f"入库失败：{err}")
            else:
                self.log(f"当前表单入库到表【{self.current_table}】成功")
                self._load_table_data()
        self.executor.submit(task)

    def _submit_all(self):
        if not self.rows: self.log("暂无数据"); return
        if not self.current_table: self.log("请先选择数据表"); return
        rows_data = []
        for _, wids in self.rows:
            rd = {k: v.text().strip() for k, v in wids.items()}
            if any(rd.values()): rows_data.append(rd)
        if not rows_data: self.log("无有效数据"); return
        def task():
            def batch_ins(db):
                total = 0
                for rd in rows_data: db.insert(rd); total += 1
                self.log(f"批量完成，共入库{total}条到表【{self.current_table}】")
            err = db_operate(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                            self.db_name, self.current_table, batch_ins)
            if err:
                self.log(f"批量失败：{err}")
        self.executor.submit(task)

    def _table_add_empty(self):
        if not self.current_table: self.log("请先选择数据表"); return
        def task():
            ret = add_empty_table_row(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                                      self.db_name, self.db_file, self.current_table)
            if ret == "success": self.log("数据表新增空行成功"); self._load_table_data()
            else: self.log(f"新增失败：{ret}")
        self.executor.submit(task)

    def _table_del_selected(self):
        sel = self.db_tree.selectionModel().selectedRows()
        if not sel or not self.current_table: self.log("请先选中数据行"); return
        id_list = []
        for idx in sel:
            item = self.db_tree.item(idx.row(), 0)
            if item: id_list.append(item.text())
        if not id_list: self.log("无法获取行ID"); return
        def task():
            ret = del_table_rows(self.db_mode, self.db_host, self.db_user, self.db_pwd,
                                 self.db_name, self.db_file, self.current_table, id_list)
            if ret == "success":
                self.log("删除选中行成功")
                self._load_table_data()
            else:
                self.log(f"删除失败：{ret}")
        self.executor.submit(task)

    # ═══════════════════════════════════════════════════
    # 其他
    # ═══════════════════════════════════════════════════
    def _toggle_grab(self):
        if self._is_grabbed:
            self._is_grabbed = False
            self.btn_toggle.setText("解除独占"); self.btn_toggle.setStyleSheet(BTN_DANGER_QSS)
            self.log("已恢复独占模式")
        else:
            self._is_grabbed = True
            self.btn_toggle.setText("恢复独占"); self.btn_toggle.setStyleSheet(BTN_SUCCESS_QSS)
            self.log("已解除独占模式")

    def _on_close(self):
        self.running = False
        if hasattr(self, "executor"):
            self.executor.shutdown(wait=False)
        self.accept()

    def closeEvent(self, event):
        self._on_close()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════
# 7. VideoGrabDialog — 视频流抓取工作台（v12.2 新增）
# ═══════════════════════════════════════════════════════════════
class VideoGrabDialog(QDialog):
    """网页视频流抓取：批量 URL → 自动探测 m3u8/直链 → 下载 → 元数据 JSON。
    跨线程回传全部走 Signal(QueuedConnection)；停止用对话框内独立 Event，
    绝不触碰全局 STOP_CRAWL / _crawl_lock（避免与主爬虫打架）。"""

    log_signal = Signal(str)
    progress_signal = Signal(int, int, str)
    done_signal = Signal(object)

    def __init__(self, parent, db_config=None, initial_urls=None):
        super().__init__(parent)
        self.setWindowTitle(WIN_TITLE_VIDEO_GRAB)
        self.resize(*WIN_SIZE_VIDEO_GRAB)
        apply_dialog_window_flags(self)
        self.db_config = db_config or {}
        self._stop = threading.Event()      # 对话框内独立停止标志
        self._running = False
        self._urls = initial_urls or []
        self._build_ui()

        # 跨线程信号（一律 QueuedConnection）
        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.progress_signal.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self.done_signal.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # URL 输入
        grp = QGroupBox(GRP_VIDEO_URL)
        glt = QVBoxLayout(grp)
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText(
            "https://example.com/video-page\n"
            "（每行一个视频网页 URL；勾选上方\"整部抓取\"后，填动漫详情页/任一分集页即可整部下载）")
        self.url_box.setMaximumHeight(90)
        self.url_box.setFont(FONTS["default"])
        if self._urls:
            self.url_box.setPlainText("\n".join(self._urls))
        glt.addWidget(self.url_box)
        root.addWidget(grp)

        # 整部抓取设置（勾选后按详情页/分集页识别全部分集批量下载）
        sgrp = QGroupBox(GRP_VIDEO_SERIES_OPT)
        sv = QVBoxLayout(sgrp)
        sv.setSpacing(6)
        self.chk_series = QCheckBox(CHK_VIDEO_SERIES)
        sv.addWidget(self.chk_series)
        rng = QHBoxLayout()
        rng.setSpacing(4)
        rng.addWidget(QLabel(LABEL_VIDEO_RANGE))
        self.sp_from = QSpinBox()
        self.sp_from.setRange(0, 99999)
        self.sp_from.setSpecialValueText("全部")
        self.sp_from.setFixedWidth(110)
        rng.addWidget(self.sp_from)
        rng.addWidget(QLabel(LABEL_VIDEO_RANGE_TO))
        self.sp_to = QSpinBox()
        self.sp_to.setRange(0, 99999)
        self.sp_to.setSpecialValueText("全部")
        self.sp_to.setFixedWidth(110)
        rng.addWidget(self.sp_to)
        rng.addWidget(QLabel(LABEL_VIDEO_RANGE_END))
        rng.addStretch()
        sv.addLayout(rng)
        tip = QLabel(LABEL_VIDEO_SERIES_TIP)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        sv.addWidget(tip)
        self.sp_from.setEnabled(False)
        self.sp_to.setEnabled(False)
        self.chk_series.toggled.connect(self._on_series_toggled)
        root.addWidget(sgrp)

        # 操作行：开始 / 停止 / 浏览器开关 / 打开目录
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_start = QPushButton(BTN_VIDEO_START)
        self.btn_start.setStyleSheet(BTN_SUCCESS_QSS)
        self.btn_start.setMinimumWidth(140)
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)

        self.btn_stop = QPushButton(BTN_VIDEO_STOP)
        self.btn_stop.setStyleSheet(BTN_DANGER_QSS)
        self.btn_stop.setMinimumWidth(100)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_click)
        row.addWidget(self.btn_stop)

        self.chk_browser = QCheckBox(CHK_VIDEO_BROWSER)
        self.chk_browser.setChecked(bool(ENABLE_VIDEO_BROWSER_PROBE))
        row.addWidget(self.chk_browser)

        row.addStretch()

        self.btn_open_dir = QPushButton(BTN_VIDEO_OPEN_DIR)
        self.btn_open_dir.setStyleSheet(BTN_PURPLE_QSS)
        self.btn_open_dir.clicked.connect(self._open_dir)
        row.addWidget(self.btn_open_dir)
        root.addLayout(row)

        # 状态行
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color:{T['primary']};")
        root.addWidget(self.status_label)

        # 候选结果表
        root.addWidget(QLabel(LABEL_VIDEO_CANDIDATES))
        self.cand_table = QTableWidget(0, 6)
        self.cand_table.setHorizontalHeaderLabels(["#", "类型", "状态", "大小", "文件", "URL"])
        self.cand_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.cand_table.verticalHeader().setVisible(False)
        self.cand_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cand_table.setAlternatingRowColors(True)
        self.cand_table.setMinimumHeight(180)
        root.addWidget(self.cand_table, 1)

        # 日志
        root.addWidget(QLabel(LABEL_VIDEO_LOG))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(FONTS["mono"])
        self.log_box.setMinimumHeight(120)
        root.addWidget(self.log_box)

    # ── 线程安全日志 ─────────────────────────────────────
    def log(self, msg):
        self.log_signal.emit(str(msg))

    def _append_log(self, msg):
        self.log_box.append(msg)
        self.log_box.moveCursor(QTextCursor.End)

    # ── 进度/结束槽 ──────────────────────────────────────
    def _on_progress(self, cur, tot, extra):
        self.status_label.setText(f"{extra}  ({cur}/{tot})")

    @Slot(object)
    def _on_done(self, summary):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._running = False
        if not summary or not isinstance(summary, dict):
            self.status_label.setText("任务异常结束")
            return
        # 渲染结果表（MVP：结束后一次性回填）
        self.cand_table.setRowCount(0)
        rows = []
        for page in summary.get("pages") or []:
            for rec in page.get("records") or []:
                rows.append(rec)
        self.cand_table.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            size = ""
            if rec.get("bytes_count"):
                b = int(rec["bytes_count"])
                size = f"{b/1024/1024:.1f}MB" if b >= 1048576 else f"{b/1024:.1f}KB"
            status = rec.get("status", "")
            mark = {"ok": "✓ 成功", "fail": "✗ 失败",
                    "stopped": "■ 停止", "unsupported": "△ 不支持",
                    "skipped": "△ 跳过"}.get(status, status)
            values = [
                str(rec.get("index", "")),
                rec.get("kind", ""),
                mark,
                size,
                os.path.basename(rec.get("local_path") or ""),
                rec.get("url", ""),
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                self.cand_table.setItem(r, c, item)
        self.status_label.setText(
            f"成功 {summary.get('ok', 0)} / 失败 {summary.get('fail', 0)} / "
            f"跳过 {summary.get('skip', 0)} / 停止 {summary.get('stopped', 0)}")

    # ── 动作 ─────────────────────────────────────────────
    def _on_series_toggled(self, checked):
        self.sp_from.setEnabled(checked)
        self.sp_to.setEnabled(checked)
        if checked:
            self.log("已开启整部抓取：每行 URL 视为动漫详情页/分集页，将识别全部分集批量下载")

    def _start(self):
        if self._running:
            return
        urls = [ln.strip() for ln in self.url_box.toPlainText().splitlines() if ln.strip()]
        if not urls:
            self.log("请先输入至少一个页面 URL")
            return
        use_browser = self.chk_browser.isChecked()
        series_mode = self.chk_series.isChecked()
        ep_from = self.sp_from.value() or None
        ep_to = self.sp_to.value() or None
        self._stop.clear()
        self._running = True
        self.cand_table.setRowCount(0)
        self.status_label.setText("任务进行中...")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        mode = "整部" if series_mode else "单页/批量"
        self.log(f"视频抓取任务开始（{mode}模式），共 {len(urls)} 个 URL")
        threading.Thread(target=self._worker,
                         args=(urls, use_browser, series_mode, ep_from, ep_to),
                         daemon=True).start()

    def _worker(self, urls, use_browser, series_mode, ep_from, ep_to):
        try:
            if series_mode:
                from video_grab import run_series
                summary = run_series(
                    urls, use_browser=use_browser,
                    ep_start=ep_from, ep_end=ep_to,
                    progress_callback=lambda cur, tot, extra: self.progress_signal.emit(cur, tot, extra),
                    log_callback=self.log,
                    stop_flag=lambda: self._stop.is_set(),
                )
            else:
                from video_grab import run_batch
                summary = run_batch(
                    urls, use_browser=use_browser,
                    progress_callback=lambda cur, tot, extra: self.progress_signal.emit(cur, tot, extra),
                    log_callback=self.log,
                    stop_flag=lambda: self._stop.is_set(),
                )
            self.done_signal.emit(summary)
        except Exception as e:
            self.log(f"任务异常：{e}")
            self.done_signal.emit(None)

    def _stop_click(self):
        self._stop.set()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("正在停止...")
        self.log(LOG_VIDEO_STOP)

    def _open_dir(self):
        try:
            os.makedirs(VIDEO_DIR, exist_ok=True)
            os.startfile(VIDEO_DIR)
        except Exception as e:
            self.log(LOG_VIDEO_OPEN_DIR.format(dir=VIDEO_DIR) + f"（{e}）")

    def closeEvent(self, event):
        self._stop.set()
        self._running = False
        super().closeEvent(event)


class AudioGrabDialog(RecipeRowMixin, QDialog):
    """抓取音频流：批量 URL →（网页静态解析 或 音频直链/流）→ 下载 → 元数据 JSON。
    两类来源：①网页/分集页（含 <audio>/JS m3u8-mpd/直链、iframe 有界跳转）；
    ②音频直链/流（.m3u8/.mpd/.mp3/.m4a/.aac/.wav/.flac/.ogg）批量。
    跨线程回传全走 Signal；停止用对话框独立 Event，绝不触碰全局 STOP_CRAWL。
    与 VideoGrabDialog 镜像（复制-改写，不继承）：唯一结构性差异是【无浏览器抓流开关】。

    配方（媒体配方/*.json）在这边是**补充线**，不是优先线：常规音频解析 0 候选的页面，才拿选中的
    配方再试一次，命中就并入结果。语义刻意与「通用媒体下载」弹窗不同 —— 那边选中＝强制不降级
    （用来验证"我写的配方通不通"），这边一次批量粘多个 URL，不该因为配方没命中就整体空手。
    配方行的下拉/启停/导入实现在 qt_recipe_row.RecipeRowMixin（与那边共用同一份）。"""

    log_signal = Signal(str)
    progress_signal = Signal(int, int, str)
    done_signal = Signal(object)

    def __init__(self, parent, db_config=None, initial_urls=None):
        super().__init__(parent)
        self.setWindowTitle(WIN_TITLE_AUDIO_GRAB)
        self.resize(*WIN_SIZE_AUDIO_GRAB)
        apply_dialog_window_flags(self)
        self.db_config = db_config or {}
        self._stop = threading.Event()
        self._running = False
        self._urls = initial_urls or []
        self._build_ui()

        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.progress_signal.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self.done_signal.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # URL 输入
        grp = QGroupBox(GRP_AUDIO_URL)
        glt = QVBoxLayout(grp)
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText(
            "https://example.com/audio-page\n"
            "https://cdn.example.com/book.mp3        （音频直链/流也可以，一行一个）\n"
            "https://cdn.example.com/stream.m3u8\n"
            "（勾选上方\"整部抓取\"后，填音频专辑/详情页/任一分集页即可整部分集下载）")
        self.url_box.setMaximumHeight(90)
        self.url_box.setFont(FONTS["default"])
        if self._urls:
            self.url_box.setPlainText("\n".join(self._urls))
        glt.addWidget(self.url_box)
        root.addWidget(grp)

        # 整部抓取设置
        sgrp = QGroupBox(GRP_AUDIO_SERIES_OPT)
        sv = QVBoxLayout(sgrp)
        sv.setSpacing(6)
        self.chk_series = QCheckBox(CHK_AUDIO_SERIES)
        sv.addWidget(self.chk_series)
        rng = QHBoxLayout()
        rng.setSpacing(4)
        rng.addWidget(QLabel(LABEL_AUDIO_RANGE))
        self.sp_from = QSpinBox()
        self.sp_from.setRange(0, 99999)
        self.sp_from.setSpecialValueText("全部")
        self.sp_from.setFixedWidth(110)
        rng.addWidget(self.sp_from)
        rng.addWidget(QLabel(LABEL_AUDIO_RANGE_TO))
        self.sp_to = QSpinBox()
        self.sp_to.setRange(0, 99999)
        self.sp_to.setSpecialValueText("全部")
        self.sp_to.setFixedWidth(110)
        rng.addWidget(self.sp_to)
        rng.addWidget(QLabel(LABEL_AUDIO_RANGE_END))
        rng.addStretch()
        sv.addLayout(rng)
        tip = QLabel(LABEL_AUDIO_SERIES_TIP)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        sv.addWidget(tip)
        self.sp_from.setEnabled(False)
        self.sp_to.setEnabled(False)
        self.chk_series.toggled.connect(self._on_series_toggled)
        root.addWidget(sgrp)

        # 配方（补充线）：只有下拉框 —— 导入/启停/查看搬到了主界面「配置管理」。
        # 放在「浏览器旁观兜底」**上方**：配方是"怎么解析"的输入，复选框管的是"够不到时要不要开浏览器"，
        # 两件事，且选中的配方即使不勾复选框也会试一次（见 _start 的 _watch_on）。
        self._build_recipe_row(root, label=LABEL_AUDIO_RECIPE_SEL, tip=TIP_AUDIO_RECIPE_SEL)

        # 浏览器旁观兜底（默认关）：常规音频解析 0 候选时才启用；仅单页/批量模式生效
        wrow = QHBoxLayout()
        wrow.setSpacing(6)
        self.chk_page_watch = QCheckBox(CHK_AUDIO_PAGE_WATCH)
        self.chk_page_watch.setChecked(False)
        wrow.addWidget(self.chk_page_watch)
        wrow.addStretch()
        root.addLayout(wrow)

        # 操作行：开始 / 停止 / 打开音频目录（无浏览器抓流开关）
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_start = QPushButton(BTN_AUDIO_START)
        self.btn_start.setStyleSheet(BTN_SUCCESS_QSS)
        self.btn_start.setMinimumWidth(140)
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)

        self.btn_stop = QPushButton(BTN_AUDIO_STOP)
        self.btn_stop.setStyleSheet(BTN_DANGER_QSS)
        self.btn_stop.setMinimumWidth(100)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_click)
        row.addWidget(self.btn_stop)

        row.addStretch()

        self.btn_open_dir = QPushButton(BTN_AUDIO_OPEN_DIR)
        self.btn_open_dir.setStyleSheet(BTN_PURPLE_QSS)
        self.btn_open_dir.clicked.connect(self._open_dir)
        row.addWidget(self.btn_open_dir)
        root.addLayout(row)

        # 状态行
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color:{T['primary']};")
        root.addWidget(self.status_label)
        # 配方行建在上方、状态行在这里才出生，故那时写不进状态文案；补刷一次，开局就把
        # "当前配方＝自动/补充线"摆出来（与通用媒体下载弹窗的开局一致）。
        self._on_recipe_changed()

        # 结果表
        root.addWidget(QLabel(LABEL_AUDIO_CANDIDATES))
        self.cand_table = QTableWidget(0, 6)
        self.cand_table.setHorizontalHeaderLabels(["#", "类型", "状态", "大小", "文件", "URL"])
        self.cand_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.cand_table.verticalHeader().setVisible(False)
        self.cand_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cand_table.setAlternatingRowColors(True)
        self.cand_table.setMinimumHeight(180)
        root.addWidget(self.cand_table, 1)

        # 日志
        root.addWidget(QLabel(LABEL_AUDIO_LOG))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(FONTS["mono"])
        self.log_box.setMinimumHeight(120)
        root.addWidget(self.log_box)

    # ── 线程安全日志 ─────────────────────────────────────
    def log(self, msg):
        self.log_signal.emit(str(msg))

    def _append_log(self, msg):
        self.log_box.append(msg)
        self.log_box.moveCursor(QTextCursor.End)

    # ── 进度/结束槽 ──────────────────────────────────────
    def _on_progress(self, cur, tot, extra):
        self.status_label.setText(f"{extra}  ({cur}/{tot})")

    @Slot(object)
    def _on_done(self, summary):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._running = False
        self._recipe_set_busy(False)      # 解冻配方行；顺序在 status_label 之前，
        if not summary or not isinstance(summary, dict):   # 因为解冻会按选中项写 status_label
            self.status_label.setText("任务异常结束")
            return
        self.cand_table.setRowCount(0)
        rows = []
        for page in summary.get("pages") or []:
            for rec in page.get("records") or []:
                rows.append(rec)
        self.cand_table.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            size = ""
            if rec.get("bytes_count"):
                b = int(rec["bytes_count"])
                size = f"{b/1024/1024:.1f}MB" if b >= 1048576 else f"{b/1024:.1f}KB"
            status = rec.get("status", "")
            mark = {"ok": "✓ 成功", "fail": "✗ 失败",
                    "stopped": "■ 停止", "unsupported": "△ 不支持",
                    "skipped": "△ 跳过"}.get(status, status)
            values = [
                str(rec.get("index", "")),
                rec.get("kind", ""),
                mark,
                size,
                os.path.basename(rec.get("local_path") or ""),
                rec.get("url", ""),
            ]
            for c, val in enumerate(values):
                item = QTableWidgetItem(val)
                self.cand_table.setItem(r, c, item)
        self.status_label.setText(
            f"成功 {summary.get('ok', 0)} / 失败 {summary.get('fail', 0)} / "
            f"跳过 {summary.get('skip', 0)} / 停止 {summary.get('stopped', 0)}")

    # ── 动作 ─────────────────────────────────────────────
    def _on_series_toggled(self, checked):
        self.sp_from.setEnabled(checked)
        self.sp_to.setEnabled(checked)
        if checked:
            self.log("已开启整部抓取：每行 URL 视为音频专辑/分集页，将识别全部分集批量下载")

    # ── 配方行的宿主钩子（实现在 qt_recipe_row.RecipeRowMixin） ──
    def _recipe_auto_hint(self) -> str:
        return "就绪（配方：自动，常规音频解析为主）"

    def _recipe_forced_hint(self, key) -> str:
        """选中＝**补充线**：常规解析 0 候选的页面才用它，不是"只走它、不降级"。"""
        return "配方 %s（补充线：常规解析 0 候选的页面再用它试一次）" % key

    def _start(self):
        if self._running:
            return
        urls = [ln.strip() for ln in self.url_box.toPlainText().splitlines() if ln.strip()]
        if not urls:
            self.log("请先输入至少一个音频页/直链 URL")
            return
        series_mode = self.chk_series.isChecked()
        ep_from = self.sp_from.value() or None
        ep_to = self.sp_to.value() or None
        # 主线程读控件成快照（工作线程直接读 QWidget 不是线程安全的）
        self._forced_recipe = self._forced_key()
        # 选中配方＝补充线，**等价于额外允许多试一次**，故没勾复选框也跑；不选配方时行为与
        # 改动前完全一致（仍需勾复选框）。整部模式下两条都不参与。
        self._watch_on = not series_mode and (
            bool(self.chk_page_watch.isChecked()) or bool(self._forced_recipe))
        self._stop.clear()
        self._running = True
        self.cand_table.setRowCount(0)
        self.status_label.setText("任务进行中...")
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._recipe_set_busy(True)
        mode = "整部" if series_mode else "单页/批量"
        self.log(f"音频抓取任务开始（{mode}模式），共 {len(urls)} 个 URL")
        if self._forced_recipe:
            self.log("配方 %s 作为补充线：常规解析 0 候选的页面会再用它试一次"
                     % self._forced_recipe)
            _r = self._current_recipe() or {}
            if not self.chk_page_watch.isChecked() \
                    and (_r.get("spec") or {}).get("browser_seam"):
                # 复选框才是"允不允许开浏览器"的开关；配方声明了缝却没勾 = 白写，提前说清楚
                self.log(LOG_AUDIO_RECIPE_SEAM_HINT.format(key=self._forced_recipe))
        if series_mode and (self.chk_page_watch.isChecked() or self._forced_recipe):
            self.log(LOG_AUDIO_RECIPE_SERIES_SKIP if self._forced_recipe
                     else "整部分集模式不走浏览器旁观兜底（分集有自己的线路切换语义）")
        threading.Thread(target=self._worker,
                         args=(urls, series_mode, ep_from, ep_to),
                         daemon=True).start()

    def _page_watch_fallback(self, summary):
        """常规音频解析**零候选**的 URL → 走 media_resolve 通用链再试一次。

        两条并存的语义，靠 `_forced_recipe` 区分（都在 _start 里于主线程拍快照）：
          · 未选配方（默认）：页面 API **旁观**线，用户需勾「浏览器旁观」复选框（旧行为，未改）；
          · 选中某份配方：先用该配方（HTTP 为主）试一次，命不中就算了（补充线，不影响常规线已有结果）。
        与 ag.run_batch 同一 worker 线程内串行执行；结果并入既有 summary（表格/元数据 JSON 零改动）。
        """
        try:
            import media_resolve as mr
            import video_grab as vg
        except Exception as e:
            self.log(f"旁观兜底不可用：{e}")
            return summary
        forced = getattr(self, "_forced_recipe", "") or ""
        use_b = bool(self.chk_page_watch.isChecked())   # 复选框是"允不允许开浏览器"的唯一开关
        pages = summary.get("pages") or []
        merged = []
        for page in pages:
            merged.extend(page.get("records") or [])
        added = []                             # 只把**新增**记录喂回 write_metadata_json：
        changed = False                        # 它是"读回旧 items 再 extend"的幂等合并，喂全集会整份翻倍
        for page in pages:
            if self._stop.is_set():
                break
            if page.get("candidates"):
                continue                       # 常规线有候选 → 不抢
            u = page.get("url") or ""
            if not u:
                continue
            # 直链失败 ≠ 页面解析不出：URL 本身就是媒体文件时，"0 候选"来自直链下载失败，
            # 开浏览器去围观这个 mp3 地址不会有任何新信息（旧行为会白等十几秒再报一句"未命中"）。
            try:
                if mr.classify_media(u)[0]:
                    continue
            except Exception:
                pass
            if forced:
                self.log(LOG_AUDIO_RECIPE_TRY.format(url=u, key=forced))
            else:
                self.log(LOG_AUDIO_WATCH_TRY.format(url=u))
            try:
                pr = mr.resolve_page(u, want="audio", use_browser=use_b, log=self.log,
                                     force_recipe=forced, stop_flag=lambda: self._stop.is_set())
            except Exception as e:
                self.log(f"  · 旁观兜底异常：{e}")
                continue
            for n in pr.notes or []:
                self.log(f"  · {n}")
            # media_class 空的清单轨在 want=audio 下已由 resolve_page 归为 audio；这里再兜一层
            trs = [t for t in (pr.tracks or []) if (t.media_class or "audio") == "audio"]
            if not trs:
                if forced:
                    self.log(LOG_AUDIO_RECIPE_MISS.format(url=u, key=forced))
                else:
                    self.log(LOG_AUDIO_WATCH_MISS.format(url=u))
                continue
            self.log(LOG_AUDIO_RECIPE_HIT.format(url=u, key=forced, n=len(trs)) if forced
                     else LOG_AUDIO_WATCH_HIT.format(url=u, n=len(trs)))
            changed = True
            page["candidates"] = list(page.get("candidates") or []) + [t.url for t in trs]
            for t in trs:
                if self._stop.is_set():
                    break
                try:
                    res = mr.download_track(t, out_dir=AUDIO_DIR,
                                            stop_flag=lambda: self._stop.is_set(),
                                            log=self.log)
                except Exception as e:
                    res = {"status": "fail", "bytes": 0, "path": "", "error": str(e)}
                rec = {"index": t.index, "kind": t.kind, "url": t.url, "ext": t.ext,
                       "src": t.src, "page_url": t.page_url,
                       "status": res.get("status", "fail"),
                       "local_path": res.get("path") or "",
                       "bytes_count": res.get("bytes", 0),
                       "error": res.get("error", "")}
                if t.title_hint:
                    rec["meta"] = {"title": t.title_hint}
                page["records"].append(rec)
                merged.append(rec)
                added.append(rec)
        if not changed:
            return summary
        # 计数与元数据 JSON 重算/重写（run_batch 写的那份不含兜底新增记录）
        summary["ok"] = sum(1 for r in merged if r.get("status") == "ok")
        summary["fail"] = sum(1 for r in merged if r.get("status") == "fail")
        summary["skip"] = sum(1 for r in merged
                              if r.get("status") in ("skipped", "unsupported"))
        summary["stopped"] = sum(1 for r in merged if r.get("status") == "stopped")
        try:
            if summary.get("meta_path") and added:
                vg.write_metadata_json(AUDIO_DIR, added, summary["meta_path"])
        except Exception as e:
            self.log(f"元数据重写失败：{e}")
        return summary

    def _worker(self, urls, series_mode, ep_from, ep_to):
        try:
            if series_mode:
                from audio_grab import run_series
                summary = run_series(
                    urls,
                    ep_start=ep_from, ep_end=ep_to,
                    progress_callback=lambda cur, tot, extra: self.progress_signal.emit(cur, tot, extra),
                    log_callback=self.log,
                    stop_flag=lambda: self._stop.is_set(),
                )
            else:
                from audio_grab import run_batch
                summary = run_batch(
                    urls,
                    progress_callback=lambda cur, tot, extra: self.progress_signal.emit(cur, tot, extra),
                    log_callback=self.log,
                    stop_flag=lambda: self._stop.is_set(),
                )
                # 浏览器旁观兜底：仅常规解析 0 候选的页，且用户勾选时（同一 worker 线程内串行）
                if getattr(self, "_watch_on", False) and summary:
                    summary = self._page_watch_fallback(summary)
            self.done_signal.emit(summary)
        except Exception as e:
            self.log(f"任务异常：{e}")
            self.done_signal.emit(None)

    def _stop_click(self):
        self._stop.set()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("正在停止...")
        self.log(LOG_AUDIO_STOP)

    def _open_dir(self):
        try:
            os.makedirs(AUDIO_DIR, exist_ok=True)
            os.startfile(AUDIO_DIR)
        except Exception as e:
            self.log(LOG_AUDIO_OPEN_DIR.format(dir=AUDIO_DIR) + f"（{e}）")

    def closeEvent(self, event):
        self._stop.set()
        self._running = False
        super().closeEvent(event)
