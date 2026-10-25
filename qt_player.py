# -*- coding: utf-8 -*-
"""本地视频播放器 UI 层（qt_player.py）

MpvWidget：libmpv 以 wid 直嵌（默认 vo，Windows 走 gpu/d3d11）——容器即渲染目标，
尺寸由布局驱动、画面自动信箱；不引入 OpenGL render context / 每帧泵。
VideoPlayerDialog：左剧集树 + 右播放面板（进度/上一集/播放暂停/下一集/音量/自动连播）。

重要纪律：
  * 本文件 import 期不碰 mpv —— 只在 MpvWidget._startup 里经 video_player.bootstrap_mpv()
    惰性 import（main_qt 也只在本按钮被点时才 from qt_player import VideoPlayerDialog）。
  * 所有 mpv 属性轮询走主线程 QTimer；不注册 mpv 事件回调（避免跨线程碰 Qt）。
  * closeEvent 先 widget.shutdown()（stop timer → terminate → join）再放行，杜绝残留线程/崩溃。
"""
import os
import time

from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QTreeWidget, QTreeWidgetItem, QPushButton, QLabel, QSlider,
    QCheckBox, QMessageBox, QFileDialog, QHeaderView, QComboBox,
)
from PySide6.QtCore import Qt, QTimer, Signal

import config
import video_player as vp
from config import (
    BTN_PLAYER_REFRESH, BTN_PLAYER_OPEN_FILE, BTN_PLAYER_OPEN_DIR,
    BTN_PLAYER_PREV, BTN_PLAYER_PLAY, BTN_PLAYER_PAUSE, BTN_PLAYER_NEXT,
    LABEL_PLAYER_VOLUME, CHK_PLAYER_AUTOPLAY, LABEL_PLAYER_FORMAT,
    STATUS_PLAYER_READY, STATUS_PLAYER_SEL, STATUS_PLAYER_PLAYING,
    STATUS_PLAYER_PAUSED, STATUS_PLAYER_END, MSG_PLAYER_SEL_NONE,
    TREE_PLAYER_NAME, TREE_PLAYER_SIZE, TREE_PLAYER_LOOSE,
    MSG_PLAYER_LOAD_FAIL, MSG_PLAYER_EOF_LAST,
    STATUS_PLAYER_FMT, MSG_PLAYER_FMT_EMPTY, MSG_PLAYER_FMT_MISMATCH,
    MSG_PLAYER_FILE_GONE, MSG_PLAYER_OPEN_DIR, MSG_PLAYER_NO_MEDIA,
)
from qt_theme import FONTS, BTN_PRIMARY_QSS, BTN_DEFAULT_QSS, apply_dialog_window_flags


# 树项数据类型标签
_ROLE_KIND = Qt.ItemDataRole.UserRole
_ROLE_DATA = Qt.ItemDataRole.UserRole + 1
_KIND_SERIES, _KIND_LOOSE, _KIND_EP = "series", "loose", "ep"

# 会话内记住上次选的格式（弹窗每次新建 + exec()，不记住就得每次重选）。
# 只活在进程内存里，零磁盘 IO；模块级可变变量是刻意的。
_LAST_FMT_KEY = None


def _fmt_time(sec) -> str:
    sec = int(round(sec or 0))
    if sec < 0:
        sec = 0
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


class MpvWidget(QWidget):
    """内嵌 libmpv 画面容器（wid 直嵌）。mpv 实例在 widget 可见后才创建。"""

    tick = Signal(float, float, bool)     # (当前秒, 总秒, 是否暂停)，≥250ms 轮询
    eof = Signal()                        # 播到结尾（keep_open 停在末帧）
    failed = Signal(str)                  # 内核初始化/播放错误（中文）
    volumeChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # 强制原生 HWND：wid 直嵌要求 winId 非 0 且不建祖先原生窗口
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setStyleSheet("background:#000;")
        self._module = None
        self._mpv = None
        self._closing = False
        self._started = False
        self._start_error = None
        self._last_pause = None
        self._eof_sent = False
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._poll)

    # ── 生命周期 ─────────────────────────────────────────
    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._started and not self._closing:
            QTimer.singleShot(0, self._startup)  # winId 此时非 0

    def _startup(self):
        if self._closing or self._started:
            return
        try:
            self._module = vp.bootstrap_mpv()
            self._mpv = self._module.MPV(
                wid=str(int(self.winId())),      # 容器即渲染目标
                config="no", osc="no",
                keep_open="yes",
                input_default_bindings="no",     # 不要 mpv 内置键抢占窗口焦点
            )
            self._started = True
            self._timer.start()
            try:
                vol = self._mpv.volume
                if vol is not None:
                    self.volumeChanged.emit(int(vol))
            except Exception:
                pass
        except Exception as e:
            self._start_error = str(e)
            self._started = False
            self.failed.emit(str(e))

    def shutdown(self):
        """销毁 mpv（须先于 Qt 销毁 HWND）。幂等。"""
        self._closing = True
        if self._timer.isActive():
            self._timer.stop()
        m, self._mpv = self._mpv, None
        if m is not None:
            try:
                m.terminate()  # 停事件线程并 join
            except Exception:
                pass

    def is_ready(self) -> bool:
        return self._started and self._mpv is not None

    # ── 播放控制 ─────────────────────────────────────────
    def play(self, path: str):
        if not self.is_ready():
            self._startup()
        if not self.is_ready():
            self.failed.emit(self._start_error or "播放内核未就绪")
            return
        self._eof_sent = False
        try:
            self._mpv.play(path)
        except Exception as e:
            self.failed.emit("加载失败：%s" % e)

    def set_pause(self, paused: bool):
        if self._mpv is None:
            return
        try:
            self._mpv.pause = bool(paused)
        except Exception:
            pass
        self._last_pause = bool(paused)

    def toggle_pause(self):
        self.set_pause(not self.paused)

    @property
    def paused(self) -> bool:
        if self._mpv is None:
            return False
        try:
            return bool(self._mpv.pause)
        except Exception:
            return False

    def seek_to(self, seconds: float):
        if self._mpv is None:
            return
        self._eof_sent = False
        try:
            self._mpv.seek(max(0.0, float(seconds)),
                           reference="absolute", precision="keyframes")
        except Exception as e:
            self.failed.emit("跳转失败：%s" % e)

    def set_volume(self, v: int):
        if self._mpv is None:
            return
        try:
            self._mpv.volume = int(max(0, min(100, v)))
        except Exception:
            pass

    # ── 主线程轮询 ───────────────────────────────────────
    def _poll(self):
        mpv_ = self._mpv
        if mpv_ is None or self._closing:
            return
        try:
            pos = mpv_.time_pos
            dur = mpv_.duration
            paus = bool(mpv_.pause)
            eofr = bool(mpv_.eof_reached)
        except Exception:
            return
        if self._last_pause != paus:
            self._last_pause = paus
        if dur and dur > 0:
            self.tick.emit(float(pos) if pos else 0.0, float(dur), paus)
        if eofr and not self._eof_sent:
            self._eof_sent = True
            self.eof.emit()


class VideoPlayerDialog(QDialog):
    """本地媒体播放器：扫已下载库 → 左侧单击选中、▶ 播放。

    「格式」下拉框（cb_fmt）是**唯一**的格式开关：**选中项就是过滤条件**——左树只列
    该格式的文件、不匹配的一律不出现；同时驱动「打开本地文件」的过滤器与「打开目录」
    的目标目录。视频格式扫 config.VIDEO_DIR、音频格式扫 config.AUDIO_DIR，所以切格式
    = 换"我在浏览哪个库"（音频不在视频库里，不这样就永远扫不到）。
    下拉项由 video_player.FORMAT_GROUPS 铺平自 config.VIDEO_EXTS / AUDIO_OUT_EXTS，
    本文件不写任何扩展名字面量。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(config.WIN_TITLE_VIDEO_PLAYER)
        self.resize(*config.WIN_SIZE_VIDEO_PLAYER)
        apply_dialog_window_flags(self)
        self._queue = []          # 当前可上一/下一集的 Episode 序列
        self._queue_idx = -1
        self._current = None      # 当前播放的 Episode
        self._eof = False         # 当前集是否已播到结尾（此时点 ▶ 是重播而非续播）
        self._last_play_ts = 0.0  # 最近一次开播时刻（防抖，连点不重复重启同集）
        self._dragging = False
        self._lib_dur = 0.0       # 最近一次 tick 的总时长（供拖动换算）
        self._build_ui()          # 内部设好 self._fmt_group（默认组 / 上次选的组）
        self._connect()           # 关键！把所有按钮/树/进度信号接上（此前漏调=界面全没反应）
        # 空库提示由 _rebuild_tree 内部按"整个库有没有媒体"判断并延后 150ms 弹，
        # 不在这里再判一次（否则与它重复弹两个窗）。
        self._refresh_library()

    def showEvent(self, ev):
        super().showEvent(ev)
        # 弹出后主动抢焦点/置前，避免首次点击被 Windows 当成"激活窗口"吃掉
        QTimer.singleShot(60, self._ensure_active)

    def _ensure_active(self):
        try:
            self.activateWindow()
            self.raise_()
            self.tree.setFocus()
        except Exception:
            pass

    def _warn_empty(self):
        """库是**空的**（一个受支持的音视频文件都没有）→ 提示去下载。

        判据是"这个 root 下一个媒体文件都没有"，不是"当前格式没匹配到"——
        后者是正常状态（库里只有 .ts 而当前格式是 .mp4），只更新状态行不弹窗，
        否则用户每试一个格式就被弹一次。"""
        if self.isVisible():  # 对话框已关闭则不再弹
            QMessageBox.information(
                self, config.WIN_TITLE_VIDEO_PLAYER,
                MSG_PLAYER_NO_MEDIA.format(dir=self._fmt_group.root))

    # ── UI ───────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # 顶部工具行
        bar = QHBoxLayout()
        self.btn_refresh = QPushButton(BTN_PLAYER_REFRESH)
        self.btn_refresh.setStyleSheet(BTN_DEFAULT_QSS)
        self.btn_open_file = QPushButton(BTN_PLAYER_OPEN_FILE)
        self.btn_open_file.setStyleSheet(BTN_DEFAULT_QSS)
        self.btn_open_dir = QPushButton(BTN_PLAYER_OPEN_DIR)
        self.btn_open_dir.setStyleSheet(BTN_DEFAULT_QSS)
        for b in (self.btn_refresh, self.btn_open_file, self.btn_open_dir):
            bar.addWidget(b)
        # 格式下拉框：**选中项就是过滤条件**（左树只列该格式的文件，不匹配的不出现），
        # 同时驱动「打开本地文件」的过滤器。放在三个按钮**之后**，不动既有按钮位置。
        bar.addSpacing(12)
        self.lab_fmt = QLabel(LABEL_PLAYER_FORMAT)
        bar.addWidget(self.lab_fmt)
        self.cb_fmt = QComboBox()
        self.cb_fmt.setMinimumWidth(150)
        # 初值用 blockSignals 包住：若等 _connect 之后再 setCurrentIndex，
        # currentIndexChanged 会立刻触发一次重扫 → 开窗白扫两遍。
        self.cb_fmt.blockSignals(True)
        for g in vp.FORMAT_GROUPS:
            self.cb_fmt.addItem(g.label, g)
        _g0 = vp.find_group(_LAST_FMT_KEY) or vp.default_group()
        if _g0 is not None:
            self.cb_fmt.setCurrentIndex(vp.FORMAT_GROUPS.index(_g0))
        self.cb_fmt.blockSignals(False)
        self._fmt_group = _g0 or vp.default_group()
        self.cb_fmt.setToolTip(self._fmt_tip())
        bar.addWidget(self.cb_fmt)
        bar.addStretch(1)
        root.addLayout(bar)

        # 左树 + 右播放面板
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([TREE_PLAYER_NAME, TREE_PLAYER_SIZE])
        self.tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setColumnWidth(1, 90)
        self.tree.setFont(FONTS["default"])
        split.addWidget(self.tree)

        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)

        self.widget = MpvWidget()
        self.widget.setMinimumHeight(300)
        pl.addWidget(self.widget, 1)

        # 进度行
        prog = QHBoxLayout()
        self.lab_pos = QLabel("00:00")
        self.lab_pos.setFixedWidth(54)
        self.sl_prog = QSlider(Qt.Orientation.Horizontal)
        self.sl_prog.setRange(0, 1000)
        self.sl_prog.setEnabled(False)
        self.lab_dur = QLabel("00:00")
        self.lab_dur.setFixedWidth(54)
        self.lab_dur.setAlignment(Qt.AlignmentFlag.AlignRight)
        prog.addWidget(self.lab_pos)
        prog.addWidget(self.sl_prog, 1)
        prog.addWidget(self.lab_dur)
        pl.addLayout(prog)

        # 控制行
        ctl = QHBoxLayout()
        self.btn_prev = QPushButton(BTN_PLAYER_PREV)
        self.btn_play = QPushButton(BTN_PLAYER_PLAY)
        self.btn_play.setStyleSheet(BTN_PRIMARY_QSS)
        self.btn_play.setMinimumWidth(96)
        self.btn_next = QPushButton(BTN_PLAYER_NEXT)
        self.btn_play.setEnabled(True)   # 启动即可用：选中左树某行后点播放键开播
        self.btn_prev.setEnabled(False)  # 上一/下一集要有播放队列后才启用（_play 里按队列长度放行）
        self.btn_next.setEnabled(False)
        self.btn_prev.setStyleSheet(BTN_DEFAULT_QSS)
        self.btn_next.setStyleSheet(BTN_DEFAULT_QSS)
        ctl.addWidget(self.btn_prev)
        ctl.addWidget(self.btn_play)
        ctl.addWidget(self.btn_next)
        ctl.addSpacing(12)
        ctl.addWidget(QLabel(LABEL_PLAYER_VOLUME))
        self.sl_vol = QSlider(Qt.Orientation.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(100)
        self.sl_vol.setFixedWidth(150)
        ctl.addWidget(self.sl_vol)
        ctl.addStretch(1)
        self.chk_autoplay = QCheckBox(CHK_PLAYER_AUTOPLAY)
        self.chk_autoplay.setChecked(True)
        ctl.addWidget(self.chk_autoplay)
        pl.addLayout(ctl)

        # 状态行
        self.lab_status = QLabel(STATUS_PLAYER_READY)
        self.lab_status.setStyleSheet("color:#777;")
        self.lab_status.setFont(FONTS["small"])
        pl.addWidget(self.lab_status)

        split.addWidget(panel)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

    # ── 信号 ─────────────────────────────────────────────
    def _connect(self):
        """把所有控件信号接到槽函数。

        交互范式：左侧单击 = 选中（QTreeWidget 原生行为），播放统一走右侧 ▶ 按钮。
        __init__ 里必须调用本方法，否则按钮/树/进度全部"点了没反应"。
        """
        self.btn_refresh.clicked.connect(self._refresh_library)
        self.btn_open_file.clicked.connect(self._open_file)
        self.btn_open_dir.clicked.connect(self._open_dir)
        self.cb_fmt.currentIndexChanged.connect(self._on_format_changed)
        self.tree.currentItemChanged.connect(self._on_selection_changed)
        self.btn_prev.clicked.connect(lambda: self._go(-1))
        self.btn_next.clicked.connect(lambda: self._go(+1))
        self.btn_play.clicked.connect(self._toggle_play)
        self.sl_prog.sliderPressed.connect(
            lambda: setattr(self, "_dragging", True))
        self.sl_prog.sliderReleased.connect(self._on_slider_release)
        self.sl_prog.valueChanged.connect(self._on_slider_move)
        self.sl_vol.valueChanged.connect(self.widget.set_volume)
        self.widget.volumeChanged.connect(self._on_vol_changed)
        self.widget.tick.connect(self._on_tick)
        self.widget.eof.connect(self._on_eof)
        self.widget.failed.connect(self._on_failed)

    # ── 库/树 ────────────────────────────────────────────
    def _fmt_tip(self) -> str:
        """下拉框 tooltip：把该格式的扩展名与扫描根写清楚（切格式=换库，得让人看见）。"""
        return "%s\n扫描根：%s" % (
            " ".join("*" + e for e in sorted(self._fmt_group.exts)),
            self._fmt_group.root)

    def _current_path(self):
        """当前树选中项（若是可播行）对应文件的路径；没有 → None。"""
        it = self.tree.currentItem()
        if it is None:
            return None
        ep = it.data(0, _ROLE_DATA)
        return ep.path if isinstance(ep, vp.Episode) else None

    def _refresh_library(self):
        """「刷新」按钮入口。保持**无参**签名——clicked 带的 bool 由 PySide 丢弃。"""
        self._rebuild_tree(keep_path=self._current_path())

    def _rebuild_tree(self, keep_path=None):
        """按当前格式重扫并重建左树（唯一写 self._lib 的地方）。

        铁律：只碰树，**绝不碰** self._current / _queue / _queue_idx / widget。
        _queue 装的是 Episode 不可变快照（_setup_queue_for 全程 list(...) 拷贝），
        与树节点无引用关系，所以重建对正在播的那一集零影响——切格式不该打断播放。"""
        self._lib = vp.scan_library(self._fmt_group.root, self._fmt_group.exts)
        # blockSignals 包住整段：clear()/回填会让 currentItem 反复变 None → 信号洪水
        self.tree.blockSignals(True)
        self.tree.clear()
        if self._lib.series:
            for s in self._lib.series:
                top = QTreeWidgetItem([s.title, ""])
                top.setData(0, _ROLE_KIND, _KIND_SERIES)
                top.setData(0, _ROLE_DATA, s)
                for ep in s.episodes:
                    self._add_ep_item(top, ep)
                self.tree.addTopLevelItem(top)
        if self._lib.loose:
            top = QTreeWidgetItem([TREE_PLAYER_LOOSE, ""])
            top.setData(0, _ROLE_KIND, _KIND_LOOSE)
            top.setData(0, _ROLE_DATA, self._lib.loose)
            for ep in self._lib.loose:
                self._add_ep_item(top, ep)
            self.tree.addTopLevelItem(top)
        self.tree.expandAll()
        self._reselect(keep_path)
        self.tree.blockSignals(False)
        # 补一次状态行：blockSignals 期间 currentItemChanged 没发出去
        self._on_selection_changed(self.tree.currentItem())
        # "正在播放"优先级最高：切格式不打断播放，状态行也不许被踩（铁律）
        playing = (self._current is not None and self.widget.is_ready()
                   and not self._eof)
        if self._lib.total_episodes == 0:
            # 空树时 _on_selection_changed 会提前 return（currentItem 是 None），
            # 所以状态行必须在这里显式写，否则留的是上一次的陈旧文案。
            if self._root_is_empty():
                # 整个库一个媒体都没有 → 弹一次（去下载）；否则只是"这个格式没匹配到"
                # → 只更新状态行，**绝不弹模态**（否则每试一个格式被弹一次）
                QTimer.singleShot(150, self._warn_empty)
            if not playing:
                self._set_status(MSG_PLAYER_FMT_EMPTY.format(
                    fmt=self._fmt_group.label, dir=self._fmt_group.root))
        elif not playing and self.tree.currentItem() is None:
            self._set_status(STATUS_PLAYER_FMT.format(fmt=self._fmt_group.label))

    def _reselect(self, keep_path):
        """重扫后按路径把选中项找回来（保持操作连续性）。

        顶层组项的数据是 list（_KIND_LOOSE）或 Series，ep 项才是 Episode。"""
        if not keep_path:
            return
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                ch = top.child(j)
                ep = ch.data(0, _ROLE_DATA)
                if isinstance(ep, vp.Episode) and ep.path == keep_path:
                    self.tree.setCurrentItem(ch)
                    return

    def _root_is_empty(self) -> bool:
        """当前 root 下是否**连一个受支持的音视频文件都没有**（不看格式组）。

        用 ANY_MEDIA_EXTS 全量扫一次——只在树空掉时才做，代价可忽略。"""
        try:
            return vp.scan_library(
                self._fmt_group.root, vp.ANY_MEDIA_EXTS).total_episodes == 0
        except Exception:
            return False

    def _on_format_changed(self, index: int):
        """下拉框换格式 → 左树按新格式重扫（选中项尽量保留）。"""
        grp = self.cb_fmt.itemData(index)
        if grp is None or grp == self._fmt_group:
            return
        global _LAST_FMT_KEY
        self._fmt_group = grp
        _LAST_FMT_KEY = grp.key
        self.cb_fmt.setToolTip(self._fmt_tip())
        self._rebuild_tree(keep_path=self._current_path())

    def _add_ep_item(self, top: QTreeWidgetItem, ep: vp.Episode):
        it = QTreeWidgetItem(top)
        it.setText(0, ep.label)
        it.setToolTip(0, ep.name)          # 全名（含扩展名）
        it.setText(1, vp._fmt_size(ep.size))
        it.setData(0, _ROLE_KIND, _KIND_EP)
        it.setData(0, _ROLE_DATA, ep)

    # ── 播放 ─────────────────────────────────────────────
    def _on_selection_changed(self, item: QTreeWidgetItem,
                              _prev: QTreeWidgetItem = None):
        """左侧单击选中某行 → 状态行提示"已选中 XX，点 ▶ 播放"。
        正在播放时不打断状态显示。"""
        if item is None:
            return
        kind = item.data(0, _ROLE_KIND)
        if kind == _KIND_EP:
            label = item.data(0, _ROLE_DATA).label
        elif kind in (_KIND_SERIES, _KIND_LOOSE):
            label = item.text(0)
        else:
            return
        if self._current is not None and self.widget.is_ready() and not self._eof:
            return  # 正在播放中，状态行仍显示播放信息
        self._set_status(STATUS_PLAYER_SEL.format(name=label))

    def _on_activate(self, item: QTreeWidgetItem, _col: int = 0):
        """可播行的开播入口（由 ▶ 播放按钮触发）：
        剧集行→播本集；系列/散组行→从其第一集开始。"""
        if item is None:
            return
        kind = item.data(0, _ROLE_KIND)
        if kind == _KIND_EP:
            ep = item.data(0, _ROLE_DATA)
            self._setup_queue_for(ep, item)
            self._play(ep)
            return
        if kind in (_KIND_SERIES, _KIND_LOOSE):
            eps = self._playables_of(item, kind)
            if eps:
                self._queue = list(eps)
                self._queue_idx = 0
                self._play(eps[0])

    def _setup_queue_for(self, ep: vp.Episode, item: QTreeWidgetItem):
        """构建"本集所属动漫"的续播队列（同目录剧集自然序）。"""
        parent = item.parent()
        if parent is not None:
            kind = parent.data(0, _ROLE_KIND)
            if kind == _KIND_SERIES:
                self._queue = list(parent.data(0, _ROLE_DATA).episodes)
            elif kind == _KIND_LOOSE:
                self._queue = list(parent.data(0, _ROLE_DATA))
            else:
                self._queue = []
        else:
            self._queue = [ep]
        self._queue_idx = next(
            (i for i, e in enumerate(self._queue) if e.path == ep.path), 0)

    def _playables_of(self, item: QTreeWidgetItem, kind: str):
        if kind == _KIND_SERIES:
            s = item.data(0, _ROLE_DATA)
            return list(s.episodes) if s is not None else []
        if kind == _KIND_LOOSE:
            xs = item.data(0, _ROLE_DATA)
            return list(xs) if xs else []
        return []

    def _play(self, ep: vp.Episode):
        if ep is None:
            return
        # 文件不在了：以前是静默 return（表现为"▶ 点了没反应"）。切格式会重扫，
        # 陈旧树项的概率上升，所以这里必须说话——但**不弹模态**（连点会被弹窗打断）。
        if not os.path.isfile(ep.path):
            self._set_status(MSG_PLAYER_FILE_GONE.format(name=ep.name))
            return
        # 防抖：0.6s 内连点 ▶ 落在同一集 → 不重复重启
        now = time.monotonic()
        if (self._current is not None and self._current.path == ep.path
                and self.widget.is_ready()
                and now - self._last_play_ts < 0.6):
            return
        try:
            self.widget.play(ep.path)
        except Exception as e:
            QMessageBox.warning(
                self, config.WIN_TITLE_VIDEO_PLAYER,
                MSG_PLAYER_LOAD_FAIL.format(err=e))
            return
        self._last_play_ts = now
        self._eof = False
        self.widget.set_pause(False)   # 开播即进入播放态（含自动连播续播）
        self._current = ep
        self.sl_prog.setEnabled(True)
        self.btn_play.setEnabled(True)
        self.btn_play.setText(BTN_PLAYER_PAUSE)
        self.btn_play.setStyleSheet(BTN_PRIMARY_QSS)
        self.btn_prev.setEnabled(bool(self._queue) and self._queue_idx > 0)
        self.btn_next.setEnabled(bool(self._queue)
                                 and self._queue_idx < len(self._queue) - 1)
        self._set_status(STATUS_PLAYER_PLAYING.format(name=ep.label))

    def _toggle_play(self):
        # 正在播放且未到结尾 → ▶ 按钮做 暂停/续播 切换
        if (self._current is not None and self.widget.is_ready()
                and not self._eof):
            if self.widget.paused:
                self.widget.set_pause(False)
                self.btn_play.setText(BTN_PLAYER_PAUSE)
                self._set_status(STATUS_PLAYER_PLAYING.format(
                    name=self._current.label))
            else:
                self.widget.set_pause(True)
                self.btn_play.setText(BTN_PLAYER_PLAY)
                self._set_status(STATUS_PLAYER_PAUSED.format(
                    name=self._current.label))
            return
        self._start_play()

    def _start_play(self):
        """未在播放（或刚播完停在结尾）时点 ▶：
        先播左树当前选中行；没选中则重播刚播完的那集；仍没有才提示先点选。
        保证 ▶ 永远有明确反应，绝不静默无动作。"""
        item = self.tree.currentItem()
        if item is not None and item.data(0, _ROLE_KIND) in (
                _KIND_EP, _KIND_SERIES, _KIND_LOOSE):
            self._on_activate(item)
            return
        if self._current is not None:      # 已播完的那集 → 重播它
            self._play(self._current)
            return
        self._set_status(MSG_PLAYER_SEL_NONE)

    def _go(self, delta: int):
        if not self._queue:
            return
        ni = self._queue_idx + delta
        if not (0 <= ni < len(self._queue)):
            if delta > 0 and self._queue:
                self.lab_status.setText(MSG_PLAYER_EOF_LAST)
            return
        self._queue_idx = ni
        self._play(self._queue[ni])

    def _open_file(self):
        # 初始目录与过滤器都跟随当前格式组（与左树同一个真源，不会"树里看不到却能选进来"）
        path, _ = QFileDialog.getOpenFileName(
            self, BTN_PLAYER_OPEN_FILE, self._fmt_group.root,
            vp.group_filter(self._fmt_group))
        if not path:
            return
        # 过滤器里的「所有文件 (*.*)」是逃生口：手挑的文件仍要过**音视频总闸**
        # （比当前格式宽——允许一次性的越界，但不允许把 .json 元数据当媒体播）
        ep = vp._file_episode(path, vp.ANY_MEDIA_EXTS)
        if ep is None:
            QMessageBox.warning(self, config.WIN_TITLE_VIDEO_PLAYER,
                                MSG_PLAYER_FMT_MISMATCH.format(
                                    name=os.path.basename(path)))
            return
        self._queue = [ep]        # 手挑单文件 = 单元素队列（刻意：不牵动左树上下文）
        self._queue_idx = 0
        self._play(ep)

    def _open_dir(self):
        # 打开"当前格式对应的库目录"：音频组下开 output/audio，视频组下开 output/videos
        root = self._fmt_group.root
        try:
            os.startfile(root)
            self._set_status(MSG_PLAYER_OPEN_DIR.format(dir=root))
        except Exception as e:
            QMessageBox.warning(self, config.WIN_TITLE_VIDEO_PLAYER,
                                MSG_PLAYER_LOAD_FAIL.format(err=e))

    # ── 进度/音量/状态回显 ───────────────────────────────
    def _on_slider_move(self, _val: int):
        if self._dragging:
            dur = self._duration_or_0()
            pos = self.sl_prog.value() / 1000.0 * dur
            self.lab_pos.setText(_fmt_time(pos))

    def _on_slider_release(self):
        self._dragging = False
        dur = self._duration_or_0()
        if dur <= 0:
            return
        self.widget.seek_to(self.sl_prog.value() / 1000.0 * dur)

    def _duration_or_0(self) -> float:
        try:
            return float(self._lib_dur or 0)
        except Exception:
            return 0.0

    def _on_tick(self, pos: float, dur: float, paused: bool):
        self._lib_dur = dur
        if not self._dragging:
            val = int(pos / dur * 1000.0) if dur > 0 else 0
            self.sl_prog.blockSignals(True)
            self.sl_prog.setValue(val)
            self.sl_prog.blockSignals(False)
            self.lab_pos.setText(_fmt_time(pos))
            self.lab_dur.setText(_fmt_time(dur))
        self.btn_play.setText(BTN_PLAYER_PAUSE if not paused
                              else BTN_PLAYER_PLAY)
        if not paused and self._current is not None and not self._dragging:
            self._set_status(STATUS_PLAYER_PLAYING.format(
                name=self._current.label))

    def _on_eof(self):
        self.widget.set_pause(True)          # keep_open 停在末帧
        self.btn_play.setText(BTN_PLAYER_PLAY)
        if self._current is None:
            return
        self._eof = True
        has_next = (self._queue
                    and 0 <= self._queue_idx + 1 < len(self._queue))
        if self.chk_autoplay.isChecked() and has_next:
            self._go(+1)
        else:
            self._set_status(STATUS_PLAYER_END.format(name=self._current.label))
            if has_next:
                self.lab_status.setText(
                    "%s｜%s" % (STATUS_PLAYER_END.format(
                        name=self._current.label), MSG_PLAYER_EOF_LAST))

    def _on_failed(self, msg: str):
        self.lab_status.setText(msg)
        QMessageBox.warning(self, config.WIN_TITLE_VIDEO_PLAYER,
                            MSG_PLAYER_LOAD_FAIL.format(err=msg))

    def _on_vol_changed(self, v: int):
        self.sl_vol.blockSignals(True)
        self.sl_vol.setValue(v)
        self.sl_vol.blockSignals(False)

    def _set_status(self, text: str):
        self.lab_status.setText(text)

    # ── 关闭 ─────────────────────────────────────────────
    def closeEvent(self, ev):
        # 先销毁 mpv（stop timer → terminate → join），再让 Qt 销毁原生窗口
        self.widget.shutdown()
        super().closeEvent(ev)
