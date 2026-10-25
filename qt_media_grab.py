"""
qt_media_grab.py — 通用媒体下载弹窗（media_resolve 一键自动链的 GUI）。
粘贴任意网站 URL（视频/音频网页、播放页、直链、iframe 播放页、分享短链）→ 自动解析 →
候选表勾选 → 下载成品可播。解析链四层，任一命中即止：
  ① 平台配方（site_recipes）→ ② 静态双线（页内扫描）→ ③ 浏览器兜底：
  ③-1 平台浏览器缝 → ③-2 通用「页面 API 旁观」（media_capture：开页面看它自己发的
  XHR/fetch 与响应体，从 JSON 里挖媒体地址，覆盖 Web Worker 子会话）→ ③-3 通用页内探测。
③-2 是**旁观**而非构造：只读页面自己发出的请求，绝不代为构造；拿到的可能是**时效直链**
（URL 带 token），离开页面即可能失效 —— 表格里带 [时效] 角标，请尽快下载。
"目标"三选（都要/音频优先/视频优先）透传给 resolve_page(want=…)，决定归类与排序。
"配方"下拉框统一列出自带（媒体配方/，随升级覆盖）与用户（用户配方/，升级不动）两套目录；
选中具体某份时按 resolve_page(force_recipe=…) **只走这一份、解析不出也不兜底**，
用来确认"我的配方到底通不通"。导入 / 启用停用 / 查看 / 打开目录**不在这行上**，
统一到主界面「配置管理」组件（本弹窗的「媒体配方」按钮也打开它）。

下载全走既有健壮栈（media_resolve → vg/ag，不改其既有函数体）。
商业 DRM（Widevine/PlayReady/FairPlay）：只识别、跳过、注记 —— 绝不解密。
纯 PySide6；跨线程回传全走 Signal(QueuedConnection)；停止用对话框内独立 Event，不碰全局 STOP_CRAWL。
"""

import os
import threading

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox,
    QRadioButton,
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QTextCursor

from qt_theme import (
    LIGHT_THEME as T, FONTS,
    BTN_PRIMARY_QSS, BTN_SUCCESS_QSS, BTN_DANGER_QSS, BTN_PURPLE_QSS, BTN_MUTED_QSS,
    apply_dialog_window_flags,
)
from qt_recipe_row import RecipeRowMixin   # 配方行（下拉框/启停/导入）与音频弹窗共用
from config import (
    VIDEO_DIR, AUDIO_DIR, OUTPUT_DIR, MEDIA_RESOLVE_AUTO_BROWSER,
    WIN_TITLE_MEDIA_GRAB, WIN_SIZE_MEDIA_GRAB, GRP_MEDIA_URL,
    LABEL_MEDIA_CANDIDATES, LABEL_MEDIA_LOG,
    BTN_MEDIA_RESOLVE, BTN_MEDIA_DL_SEL, BTN_MEDIA_DL_ALL,
    BTN_MEDIA_STOP, BTN_MEDIA_OPEN, CHK_MEDIA_BROWSER,
    CHK_MEDIA_DRM_SKIP, LOG_MEDIA_STOP, LOG_MEDIA_OPEN_DIR, LOG_MEDIA_RECIPE_FORCED,
    LABEL_MEDIA_WANT, RADIO_MEDIA_WANT, BADGE_MEDIA_EPHEMERAL, TIP_MEDIA_EPHEMERAL,
    BTN_MEDIA_RECIPE, TIP_MEDIA_RECIPE,
    # 配方下拉框本身（选项文案 / 标签 / 状态标记）在 qt_recipe_row；
    # 「停用启用 / 导入配方 / 打开目录」那一整组 BTN_/TIP_/LOG_/MSG_ 常量也随之搬进
    # qt_config_manager.py，本文件只留「媒体配方」这颗按钮要用的两个。
)

_STATUS_GLYPH = {"ok": "✓ 成功", "fail": "✗ 失败", "skipped": "△ 跳过",
                 "stopped": "■ 停止", "unsupported": "△ 不支持", "exception": "✗ 异常"}


def _typestr(track) -> str:
    cls = track.media_class or ("清单" if track.kind in ("m3u8", "dash") else "?")
    return f"{cls} · {track.kind}" + (f" · {track.ext}" if track.ext else "")


def _size_str(b) -> str:
    try:
        b = int(b or 0)
    except Exception:
        return ""
    if b >= 1048576:
        return f"{b / 1048576:.1f}MB"
    if b >= 1024:
        return f"{b / 1024:.1f}KB"
    return f"{b}B" if b else ""


class MediaGrabDialog(RecipeRowMixin, QDialog):
    """通用媒体下载：URL → 自动解析（配方/静态/浏览器兜底）→ 勾选 → 下载。"""

    log_signal = Signal(str)
    resolve_done_signal = Signal(object)        # [MediaTrack, ...] 全部页合并后
    download_done_signal = Signal(object)       # summary dict
    row_update_signal = Signal(int, str, str, str)   # row, status_text, size_text, file

    def __init__(self, parent=None, initial_urls=None):
        super().__init__(parent)
        self.setWindowTitle(WIN_TITLE_MEDIA_GRAB)
        self.resize(*WIN_SIZE_MEDIA_GRAB)
        apply_dialog_window_flags(self)
        self._stop = threading.Event()
        self._running = False
        self._tracks: list = []          # 当前已解析（合并去重后）
        self._row_track: dict = {}       # 表格行 → MediaTrack
        self._initial_urls = list(initial_urls or [])
        self._build_ui()

        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.resolve_done_signal.connect(self._on_resolve_done, Qt.ConnectionType.QueuedConnection)
        self.download_done_signal.connect(self._on_download_done, Qt.ConnectionType.QueuedConnection)
        self.row_update_signal.connect(self._on_row_update, Qt.ConnectionType.QueuedConnection)

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        grp = QGroupBox(GRP_MEDIA_URL)
        glt = QVBoxLayout(grp)
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText(
            "https://www.bilibili.com/video/BVxxxxxx/\n"
            "https://example.com/some/video-page\n"
            "https://cdn.example.com/direct.mp4\n"
            "https://m.某站.com/share/xxx          （每行一个；网页/播放页/直链都行，自动判类）")
        self.url_box.setMaximumHeight(88)
        self.url_box.setFont(FONTS["default"])
        if self._initial_urls:
            self.url_box.setPlainText("\n".join(self._initial_urls))
        glt.addWidget(self.url_box)
        root.addWidget(grp)

        opt = QVBoxLayout()
        opt.setSpacing(2)
        self.chk_browser = QCheckBox(CHK_MEDIA_BROWSER)
        self.chk_browser.setChecked(bool(MEDIA_RESOLVE_AUTO_BROWSER))
        opt.addWidget(self.chk_browser)
        self.chk_drm = QCheckBox(CHK_MEDIA_DRM_SKIP)
        self.chk_drm.setChecked(True)
        self.chk_drm.setEnabled(False)
        opt.addWidget(self.chk_drm)
        root.addLayout(opt)

        # 目标三选：透传给 resolve_page(want=…)，决定音频/视频向的归类与排序
        wrow = QHBoxLayout()
        wrow.setSpacing(10)
        wrow.addWidget(QLabel(LABEL_MEDIA_WANT))
        self._want_btns = []
        for _val, _text in RADIO_MEDIA_WANT:
            rb = QRadioButton(_text)
            rb.setProperty("want_value", _val)
            rb.setChecked(_val == "both")
            wrow.addWidget(rb)
            self._want_btns.append(rb)
        wrow.addStretch()
        root.addLayout(wrow)

        # 配方选择行：下拉框统一列出自带 + 用户两套目录，旁边两个按钮管启停与导入。
        # 放在解析按钮**上方**：它是"怎么解析"的输入，不是解析完之后的动作。
        # 实现在 qt_recipe_row.RecipeRowMixin（音频弹窗共用同一份，别在这里改行为）。
        self._build_recipe_row(root)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_resolve = QPushButton(BTN_MEDIA_RESOLVE)
        self.btn_resolve.setStyleSheet(BTN_PRIMARY_QSS)
        self.btn_resolve.setMinimumWidth(130)
        self.btn_resolve.clicked.connect(self._click_resolve)
        row.addWidget(self.btn_resolve)

        self.btn_dl_sel = QPushButton(BTN_MEDIA_DL_SEL)
        self.btn_dl_sel.setStyleSheet(BTN_SUCCESS_QSS)
        self.btn_dl_sel.setMinimumWidth(110)
        self.btn_dl_sel.setEnabled(False)
        self.btn_dl_sel.clicked.connect(lambda: self._click_download(only_selected=True))
        row.addWidget(self.btn_dl_sel)

        self.btn_dl_all = QPushButton(BTN_MEDIA_DL_ALL)
        self.btn_dl_all.setMinimumWidth(110)
        self.btn_dl_all.setEnabled(False)
        self.btn_dl_all.clicked.connect(lambda: self._click_download(only_selected=False))
        row.addWidget(self.btn_dl_all)

        self.btn_stop = QPushButton(BTN_MEDIA_STOP)
        self.btn_stop.setStyleSheet(BTN_DANGER_QSS)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_click)
        row.addWidget(self.btn_stop)

        row.addStretch()

        # 配方管理入口。原来是"打开配方目录 + 逐条汇报加载情况"，2026-09-11 起指向**统一配置
        # 管理组件**（那边管的远不止配方：数据表模板、字段规则片段也在一起）。改名会动到既有文案
        # 常量，所以按钮文字保持「媒体配方」，工具提示里写清它现在打开的是什么。
        self.btn_recipe = QPushButton(BTN_MEDIA_RECIPE)
        self.btn_recipe.setStyleSheet(BTN_MUTED_QSS)
        self.btn_recipe.setToolTip(TIP_MEDIA_RECIPE)
        self.btn_recipe.clicked.connect(self._open_config_manager_dialog)
        row.addWidget(self.btn_recipe)

        self.btn_open = QPushButton(BTN_MEDIA_OPEN)
        self.btn_open.setStyleSheet(BTN_PURPLE_QSS)
        self.btn_open.clicked.connect(self._open_dir)
        row.addWidget(self.btn_open)
        root.addLayout(row)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color:{T['primary']};")
        root.addWidget(self.status_label)

        root.addWidget(QLabel(LABEL_MEDIA_CANDIDATES))
        self.cand_table = QTableWidget(0, 7)
        self.cand_table.setHorizontalHeaderLabels(
            ["选", "#", "类型", "状态", "大小", "文件", "URL"])
        self.cand_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.cand_table.setColumnWidth(0, 30)
        self.cand_table.setColumnWidth(1, 40)
        self.cand_table.setColumnWidth(2, 150)
        self.cand_table.setColumnWidth(3, 80)
        self.cand_table.setColumnWidth(4, 80)
        self.cand_table.verticalHeader().setVisible(False)
        self.cand_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.cand_table.setAlternatingRowColors(True)
        self.cand_table.setMinimumHeight(170)
        root.addWidget(self.cand_table, 1)

        root.addWidget(QLabel(LABEL_MEDIA_LOG))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(FONTS["mono"])
        self.log_box.setMinimumHeight(120)
        root.addWidget(self.log_box)

        self._reload_recipe_combo()      # 建完控件立刻填一次，打开弹窗就能看见有哪些配方

    # ── 线程安全日志 ─────────────────────────────────────
    def log(self, msg):
        self.log_signal.emit(str(msg))

    def _append_log(self, msg):
        self.log_box.append(msg)
        self.log_box.moveCursor(QTextCursor.End)

    @Slot(int, str, str, str)
    def _on_row_update(self, row, status_text, size_text, file_text):
        if 0 <= row < self.cand_table.rowCount():
            for col, val in ((3, status_text), (4, size_text), (5, file_text)):
                self.cand_table.item(row, col).setText(val)

    # ── 动作 ─────────────────────────────────────────────
    def _want(self) -> str:
        for rb in getattr(self, "_want_btns", []):
            if rb.isChecked():
                return rb.property("want_value") or "both"
        return "both"

    def _click_resolve(self):
        if self._running:
            return
        urls = [ln.strip() for ln in self.url_box.toPlainText().splitlines() if ln.strip()]
        if not urls:
            self.log("请先输入至少一个 URL")
            return
        self._stop.clear()
        self._running = True
        self._set_running_ui(True)
        self._tracks = []
        self._row_track = {}
        # 三个控件值都在主线程读成快照：工作线程直接读 QWidget 不是线程安全的
        self._want_value = self._want()
        self._browser_on = bool(self.chk_browser.isChecked())
        self._forced_recipe = self._forced_key()
        self.cand_table.setRowCount(0)
        if self._forced_recipe:
            self.status_label.setText("已指定配方解析中…")
            self.log(LOG_MEDIA_RECIPE_FORCED % self._forced_recipe)
        else:
            self.status_label.setText("自动解析中…")
            self.log(f"开始自动解析 {len(urls)} 个 URL（配方→静态→页面 API 旁观→浏览器兜底，目标={self._want_value}）")
        threading.Thread(target=self._worker_resolve, args=(urls,), daemon=True).start()

    def _worker_resolve(self, urls):
        try:
            import media_resolve as mr
            all_tracks = []
            for u in urls:
                if self._stop.is_set():
                    break
                page = mr.resolve_page(
                    u, log=self.log,
                    stop_flag=lambda: self._stop.is_set(),
                    use_browser=self._browser_on, want=self._want_value,
                    force_recipe=self._forced_recipe)
                for n in page.notes or []:
                    self.log(f"  · {n}")
                if page.route == "none":
                    self.log(f"  {u} → 未发现可下载媒体")
                else:
                    self.log(f"  {u} → {page.route} 命中 {len(page.tracks)} 条")
                all_tracks.extend(page.tracks or [])
            flat = mr.dedupe_tracks(all_tracks)
            for i, t in enumerate(flat, 1):
                t.index = i
            self.resolve_done_signal.emit(flat)
        except Exception as e:
            self.log(f"自动解析异常：{e}")
            self.resolve_done_signal.emit([])

    @Slot(object)
    def _on_resolve_done(self, tracks):
        self._tracks = list(tracks or [])
        self._row_track = {}
        self.cand_table.setRowCount(len(self._tracks))
        for r, tr in enumerate(self._tracks):
            self._row_track[r] = tr
            drm = tr.is_drm_commercial
            ck = QTableWidgetItem()
            ck.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ck.setCheckState(Qt.CheckState.Unchecked if drm else Qt.CheckState.Checked)
            self.cand_table.setItem(r, 0, ck)
            self.cand_table.setItem(r, 1, QTableWidgetItem(str(tr.index)))
            eph = bool(getattr(tr, "ephemeral", False))
            typ = _typestr(tr)
            if eph:
                typ = f"[{BADGE_MEDIA_EPHEMERAL}] " + typ
            t_item = QTableWidgetItem(typ)
            if eph:
                t_item.setToolTip(TIP_MEDIA_EPHEMERAL)
            self.cand_table.setItem(r, 2, t_item)
            st = "🔒 DRM(跳过)" if drm else ("待下载" if tr.note else "待下载")
            self.cand_table.setItem(r, 3, QTableWidgetItem(st))
            self.cand_table.setItem(r, 4, QTableWidgetItem(""))
            self.cand_table.setItem(r, 5, QTableWidgetItem(""))
            u_item = QTableWidgetItem(tr.url)
            tip = f"来源:{tr.src}\n{tr.note or ''}".strip()
            if eph:
                tip = (tip + "\n" if tip else "") + TIP_MEDIA_EPHEMERAL
            if tip:
                u_item.setToolTip(tip)
            self.cand_table.setItem(r, 6, u_item)
        has = len(self._tracks) > 0
        self.btn_dl_sel.setEnabled(has)
        self.btn_dl_all.setEnabled(has)
        self._running = False
        self._set_running_ui(False)
        self.status_label.setText(f"解析完成：{len(self._tracks)} 条候选（勾选后下载）")
        if not self._tracks:
            self.log("本次未发现可下载的音频/视频（可勾选“浏览器兜底发现”后重试）")

    def _click_download(self, only_selected: bool):
        if self._running or not self._row_track:
            return
        sel = []
        for r, tr in self._row_track.items():
            if not only_selected:
                sel.append((r, tr))
                continue
            it = self.cand_table.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                sel.append((r, tr))
        if not sel:
            self.log("没有选中的候选（勾选表格首列）")
            return
        self._stop.clear()
        self._running = True
        self._set_running_ui(True)
        self.btn_dl_sel.setEnabled(False)
        self.btn_dl_all.setEnabled(False)
        self.status_label.setText(f"下载中… 共 {len(sel)} 项")
        self.log(f"开始下载 {len(sel)} 项（媒体输出目录: 视频→{VIDEO_DIR} / 音频→{AUDIO_DIR}）")
        threading.Thread(target=self._worker_download, args=(sel,), daemon=True).start()

    def _worker_download(self, sel):
        counts = {"ok": 0, "fail": 0, "skip": 0, "stopped": 0}
        try:
            import media_resolve as mr
            for pos, (row, tr) in enumerate(sel):
                if self._stop.is_set():
                    counts["stopped"] += 1
                    break
                self.log(f"[{pos + 1}/{len(sel)}] 下载 {tr.url}")
                res = mr.download_track(
                    tr, progress_callback=lambda c, t, e: None,
                    stop_flag=lambda: self._stop.is_set(), log=self.log)
                st = res.get("status", "")
                counts[st if st in counts else ("skip" if st == "skipped" else "fail")] += 1
                if st in ("ok", "skipped", "stopped", "fail", "unsupported", "exception"):
                    self.row_update_signal.emit(
                        row, _STATUS_GLYPH.get(st, st),
                        _size_str(res.get("bytes", 0)),
                        os.path.basename(res.get("path") or ""))
                if st != "ok" and res.get("error"):
                    self.log(f"    ↳ {res.get('error')}")
                else:
                    self.log(f"    ✓ {res.get('path') or ''}")
        except Exception as e:
            self.log(f"下载异常：{e}")
        self.download_done_signal.emit(counts)

    @Slot(object)
    def _on_download_done(self, counts):
        counts = counts or {}
        self._running = False
        self._set_running_ui(False)
        self.btn_dl_sel.setEnabled(bool(self._row_track))
        self.btn_dl_all.setEnabled(bool(self._row_track))
        self.status_label.setText(
            f"成功 {counts.get('ok', 0)} / 失败 {counts.get('fail', 0)} / "
            f"跳过 {counts.get('skip', 0)} / 停止 {counts.get('stopped', 0)}")
        self.log("任务结束")

    def _stop_click(self):
        self._stop.set()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("正在停止…")
        self.log(LOG_MEDIA_STOP)

    def _open_dir(self):
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            os.startfile(OUTPUT_DIR)
            self.log(LOG_MEDIA_OPEN_DIR + f"：{OUTPUT_DIR}")
        except Exception as e:
            self.log(f"打开目录失败：{e}")

    # ── 配方行的宿主钩子（实现在 qt_recipe_row.RecipeRowMixin） ──
    def _recipe_forced_hint(self, key) -> str:
        """选中＝**强制不降级**：这个下拉框同时充当"我的配方到底通不通"的判据。"""
        return "配方：%s（只走这一份，解析不出也不兜底）" % key

    def _set_running_ui(self, running: bool):
        self.btn_resolve.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if running:
            self.btn_dl_sel.setEnabled(False)
            self.btn_dl_all.setEnabled(False)
        elif self._row_track:
            self.btn_dl_sel.setEnabled(True)
            self.btn_dl_all.setEnabled(True)
        # 跑的过程中锁住配方选择/导入（理由见 qt_recipe_row._recipe_set_busy）
        self._recipe_set_busy(running)

    def closeEvent(self, event):
        self._stop.set()
        self._running = False
        super().closeEvent(event)
