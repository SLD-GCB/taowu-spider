# -*- coding: utf-8 -*-
"""网站全方位诊断弹窗 —— 粘一个 URL，告诉你这个站能不能抓、有什么坑、底线在哪。

后端全在 `site_diag.py`（纯函数 + 探针 + 报告渲染），这里只做三件事：
  1. 收 URL 与选项；
  2. 后台线程调 `site_diag.run_diagnosis()`，把日志/进度通过 Signal 送回主线程；
  3. 把渲染好的报告铺在只读框里，能复制能另存。

线程纪律（与全项目一致，别在这里发明新写法）：
  - `threading.Thread(daemon=True)`，**不用 QThread/QThreadPool**；
  - 跨线程一律走 `Signal` + `Qt.ConnectionType.QueuedConnection`；
  - 停止用**本对话框私有的 `threading.Event`**，**绝不碰全局 STOP_CRAWL**
    （那是爬取任务的总闸，诊断停了不该把正在跑的任务也停了）；
  - 工作线程体内才惰性 import 重型后端（打包静态图收不到，build.py 有 hidden-import 兜着）。

诊断**只观测、不绕过**：DRM 只识别不解密，验证码不代过，付费内容不碰；
robots.txt 抓下来是把规矩摊开给你看，**不替你跳过**。
"""

import os
import threading

from PySide6.QtWidgets import (
    QDialog, QApplication, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTextEdit, QCheckBox, QComboBox, QFileDialog,
)
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QTextCursor

from qt_theme import (
    LIGHT_THEME as T, FONTS,
    BTN_PRIMARY_QSS, BTN_SUCCESS_QSS, BTN_DANGER_QSS, BTN_MUTED_QSS,
    apply_dialog_window_flags,
)
from config import (
    OUTPUT_DIR,
    TITLE_SITE_DIAG, WIN_SIZE_SITE_DIAG,
    GRP_DIAG_URL, LABEL_DIAG_REPORT, LABEL_DIAG_LOG,
    LABEL_DIAG_SCOPE, LABEL_DIAG_PROFILE, SCOPE_DIAG_PAGE, SCOPE_DIAG_SITE,
    DIAG_PROFILE_CHOICES,
    BTN_DIAG_RUN, BTN_DIAG_STOP, BTN_DIAG_COPY, BTN_DIAG_SAVE, BTN_DIAG_CLOSE,
    CHK_DIAG_BROWSER, DIAG_WAIT_DEFAULT, DIAG_USE_BROWSER, DIAG_SITE_DELAY,
    LOG_DIAG_START, LOG_DIAG_SITE_START, LOG_DIAG_STOP, LOG_DIAG_DONE, LOG_DIAG_COPIED,
    LOG_DIAG_SAVED, LOG_DIAG_SAVE_FAIL, MSG_DIAG_NO_URL, MSG_DIAG_BAD_URL,
    MSG_DIAG_RUNNING, MSG_DIAG_SAVE_TITLE, MSG_DIAG_SAVE_FILTER,
    HINT_DIAG_EMPTY, DIAG_REPORT_HEADER,
)


def _row_line(row) -> str:
    """一条运行期观测 -> 一行日志。跨域拿不到状态码的显式写成 opaque，别装成 0。"""
    st = getattr(row, "status", 0) or 0
    st_s = str(st) if st else "opaque"
    ct = (getattr(row, "content_type", "") or "").split(";")[0].strip()
    u = getattr(row, "url", "") or ""
    if len(u) > 120:
        u = u[:117] + "…"
    kind = getattr(row, "kind", "") or ""
    return f"  · [{st_s}] {kind:5s} {ct:24s} {u}"


class SiteDiagDialog(QDialog):
    """网站全方位诊断。粘 URL → 跑 → 读报告。"""

    log_signal = Signal(str)
    report_signal = Signal(str)       # 一段渲染好的报告文本（多 URL 时逐份追加）
    row_signal = Signal(str)          # 一条运行期观测（已格式化成一行）
    done_signal = Signal(object)      # {"ok":n, "fail":n, "stopped":bool}

    def __init__(self, parent=None, initial_urls=None):
        super().__init__(parent)
        self.setWindowTitle(TITLE_SITE_DIAG)
        self.resize(*WIN_SIZE_SITE_DIAG)
        apply_dialog_window_flags(self)
        self._stop = threading.Event()
        self._running = False
        self._last_report = ""
        self._initial_urls = list(initial_urls or [])
        self._build_ui()

        self.log_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.report_signal.connect(self._append_report, Qt.ConnectionType.QueuedConnection)
        self.row_signal.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        self.done_signal.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        grp = QGroupBox(GRP_DIAG_URL)
        glt = QVBoxLayout(grp)
        self.url_box = QTextEdit()
        self.url_box.setPlaceholderText(
            "https://www.example.com/some/article/123.html\n"
            "https://another-site.com/vod/456-1-1/")
        self.url_box.setMaximumHeight(72)
        self.url_box.setFont(FONTS["default"])
        if self._initial_urls:
            self.url_box.setPlainText("\n".join(self._initial_urls))
        glt.addWidget(self.url_box)
        root.addWidget(grp)

        opt = QHBoxLayout()
        opt.setSpacing(6)
        opt.addWidget(QLabel(LABEL_DIAG_SCOPE))
        self.cmb_scope = QComboBox()
        # **按 itemData 绑定，绝不按文本回查** —— 带前缀/改措辞的文本查不到，
        # 这是本项目（配置管理那轮）最易复发的一个坑。
        self.cmb_scope.addItem(SCOPE_DIAG_PAGE, "page")
        self.cmb_scope.addItem(SCOPE_DIAG_SITE, "site")
        self.cmb_scope.setMinimumWidth(300)
        self.cmb_scope.currentIndexChanged.connect(self._on_scope_changed)
        opt.addWidget(self.cmb_scope)

        opt.addSpacing(10)
        opt.addWidget(QLabel(LABEL_DIAG_PROFILE))
        self.cmb_profile = QComboBox()
        for _key, _label in DIAG_PROFILE_CHOICES:
            self.cmb_profile.addItem(_label, _key)
        self.cmb_profile.setCurrentIndex(1)          # 默认「标准深诊」
        self.cmb_profile.setMinimumWidth(320)
        opt.addWidget(self.cmb_profile)
        opt.addStretch()
        root.addLayout(opt)

        self.chk_browser = QCheckBox(CHK_DIAG_BROWSER)
        self.chk_browser.setChecked(bool(DIAG_USE_BROWSER))
        root.addWidget(self.chk_browser)
        self._on_scope_changed()                     # 初始态按单页摆好

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_run = QPushButton(BTN_DIAG_RUN)
        self.btn_run.setStyleSheet(BTN_PRIMARY_QSS)
        self.btn_run.setMinimumWidth(120)
        self.btn_run.clicked.connect(self._click_run)
        row.addWidget(self.btn_run)

        self.btn_stop = QPushButton(BTN_DIAG_STOP)
        self.btn_stop.setStyleSheet(BTN_DANGER_QSS)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_click)
        row.addWidget(self.btn_stop)

        self.btn_copy = QPushButton(BTN_DIAG_COPY)
        self.btn_copy.setStyleSheet(BTN_SUCCESS_QSS)
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._click_copy)
        row.addWidget(self.btn_copy)

        self.btn_save = QPushButton(BTN_DIAG_SAVE)
        self.btn_save.setStyleSheet(BTN_MUTED_QSS)
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._click_save)
        row.addWidget(self.btn_save)

        row.addStretch()
        self.btn_close = QPushButton(BTN_DIAG_CLOSE)
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        root.addLayout(row)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color:{T['primary']};")
        root.addWidget(self.status_label)

        root.addWidget(QLabel(LABEL_DIAG_REPORT))
        self.report_box = QTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setFont(FONTS["mono"])
        self.report_box.setPlainText(HINT_DIAG_EMPTY)
        root.addWidget(self.report_box, 3)

        root.addWidget(QLabel(LABEL_DIAG_LOG))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(FONTS["mono"])
        self.log_box.setMaximumHeight(140)
        root.addWidget(self.log_box, 1)

    # ── 模式切换 ─────────────────────────────────────────
    def _on_scope_changed(self, *_a):
        """档位只对整站模式有意义 —— 单页时灰掉，免得人选了「极致」却发现没生效。"""
        site = (self.cmb_scope.currentData() == "site")
        self.cmb_profile.setEnabled(site and not self._running)
        tip = ("整站模式会先抓 robots 的 Sitemap 行递归展开、再补首页链接，"
               "把 URL 全集归约成模板，然后按模板抽代表页深诊。**恒串行，页间有节拍。**"
               if site else
               "单页模式只看你粘的那一个 URL，不受档位影响。"
               "要铺开整个站，把「诊断范围」换成整站。")
        self.cmb_profile.setToolTip(tip)
        self.cmb_scope.setToolTip(tip)

    # ── 线程安全日志 ─────────────────────────────────────
    def log(self, msg):
        self.log_signal.emit(str(msg))

    @Slot(str)
    def _append_log(self, msg):
        self.log_box.append(msg)
        self.log_box.moveCursor(QTextCursor.End)

    @Slot(str)
    def _append_report(self, text):
        self.report_box.append(text)
        self.report_box.moveCursor(QTextCursor.End)

    # ── 动作 ─────────────────────────────────────────────
    def _click_run(self):
        if self._running:
            self.log(MSG_DIAG_RUNNING)
            return
        urls = [ln.strip() for ln in self.url_box.toPlainText().splitlines() if ln.strip()]
        if not urls:
            self.log(MSG_DIAG_NO_URL)
            return
        bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
        if bad:
            self.log(MSG_DIAG_BAD_URL + f"（{bad[0]}）")
            return

        self._stop.clear()
        self.report_box.clear()
        self._last_report = ""
        self.report_box.setPlainText(DIAG_REPORT_HEADER)
        # 控件值在主线程读成快照：工作线程直接读 QWidget 不是线程安全的。
        # **itemData 才是真值** —— 下拉文本带了说明后缀，按文本回查会查不到（本项目复发过）。
        self._browser_on = bool(self.chk_browser.isChecked())
        self._scope = self.cmb_scope.currentData() or "page"
        self._profile = self.cmb_profile.currentData() or "standard"
        self._running = True
        self._set_running_ui(True)
        self.status_label.setText("诊断中…")
        if self._scope == "site":
            # 档位名取下拉里显示的那行原文（用户看到的就是它），别在这边再抄一份中文名
            self.log(LOG_DIAG_SITE_START % (urls[0], self.cmb_profile.currentText()))
        else:
            self.log(LOG_DIAG_START % (f"{len(urls)} 个 URL", "单页",
                                       self._browser_on, DIAG_WAIT_DEFAULT))
        threading.Thread(target=self._worker, args=(urls,), daemon=True).start()

    def _worker(self, urls):
        ok, fail, stopped = 0, 0, False
        try:
            import site_diag as sd
        except Exception as e:
            self.log(f"诊断后端不可用（site_diag 导入失败）：{e!r}")
            self.done_signal.emit({"ok": 0, "fail": len(urls), "stopped": False})
            return
        site_mode = (self._scope == "site")
        # **整站模式一行只认一个站**：URL 是"站点的入口"，不是"要检查的页面"。
        # 多粘了几个就每个各跑一轮整站诊断（各是一份独立报告），语义仍然清楚。
        for u in urls:
            if self._stop.is_set():
                stopped = True
                break
            # 逐条观测最多往日志里铺 80 行 —— 一个重前端页面几百条请求，
            # 全铺出来会把真正该看的诊断日志冲走。完整清单在报告的「后端观测」节里。
            seen_rows = [0]

            def _on_row(r, _n=seen_rows):
                if _n[0] >= 80:
                    return
                _n[0] += 1
                if _n[0] == 80:
                    self.row_signal.emit("  · …（后续请求不再逐条铺，见报告「后端观测」节）")
                else:
                    self.row_signal.emit(_row_line(r))

            try:
                if site_mode:
                    report = sd.run_site_diagnosis(
                        u,
                        profile=self._profile,
                        wait_sec=DIAG_WAIT_DEFAULT,
                        use_browser=self._browser_on,
                        log=self.log,
                        stop_flag=self._stop,
                        on_row=_on_row,
                        delay_sec=DIAG_SITE_DELAY,
                    )
                else:
                    report = sd.run_diagnosis(
                        u,
                        wait_sec=DIAG_WAIT_DEFAULT,
                        use_browser=self._browser_on,
                        log=self.log,
                        stop_flag=self._stop,
                        on_row=_on_row,
                    )
                self.report_signal.emit(report.text or "")
                self._last_report = (self._last_report + "\n" + (report.text or "")).strip()
                if report.feasibility.get("verdict") in ("输入有误", None):
                    fail += 1
                else:
                    ok += 1
                self.log(LOG_DIAG_DONE % f"{u} → {report.feasibility.get('verdict', '?')}")
            except Exception as e:
                fail += 1
                self.log(f"诊断异常（{u}）：{e!r}")
        self.done_signal.emit({"ok": ok, "fail": fail, "stopped": stopped})

    @Slot(object)
    def _on_done(self, counts):
        counts = counts or {}
        self._running = False
        self._set_running_ui(False)
        tail = "已停止" if counts.get("stopped") else "完成"
        scope = "整站" if getattr(self, "_scope", "page") == "site" else "单页"
        self.status_label.setText(
            f"{scope}诊断{tail}：成功 {counts.get('ok', 0)} / 失败 {counts.get('fail', 0)}")
        self.btn_copy.setEnabled(bool(self._last_report))
        self.btn_save.setEnabled(bool(self._last_report))

    def _stop_click(self):
        self._stop.set()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("正在停止…")
        self.log(LOG_DIAG_STOP)

    def _click_copy(self):
        text = self.report_box.toPlainText()
        try:
            QApplication.clipboard().setText(text)
            self.log(LOG_DIAG_COPIED % len(text))
        except Exception as e:
            self.log(f"复制失败：{e!r}")

    def _click_save(self):
        text = self.report_box.toPlainText()
        if not text.strip():
            return
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(
                self, MSG_DIAG_SAVE_TITLE,
                os.path.join(OUTPUT_DIR, "网站诊断报告.txt"), MSG_DIAG_SAVE_FILTER)
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(LOG_DIAG_SAVED % path)
        except Exception as e:
            self.log(LOG_DIAG_SAVE_FAIL % e)

    def _set_running_ui(self, running: bool):
        self.btn_run.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.chk_browser.setEnabled(not running)
        self.url_box.setReadOnly(running)
        self.cmb_scope.setEnabled(not running)
        # 档位的可编辑性由 `_on_scope_changed` 统一裁定（整站才可点），
        # 这里只负责把 `_running` 的变化传进去 —— 两处各写一份判断迟早走岔。
        self._on_scope_changed()
        if running:
            self.btn_copy.setEnabled(False)
            self.btn_save.setEnabled(False)

    def closeEvent(self, event):
        # 关窗即请求停止：worker 是 daemon，主线程不等它；它自己会在下一个检查点退出。
        self._stop.set()
        self._running = False
        super().closeEvent(event)
