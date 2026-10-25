"""
qt_recipe_row.py — 音视频抓取弹窗共用的「配方」选择行（**只有下拉框**）。

单独成模块的理由：媒体配方这套东西要出现在**两个**弹窗里 —— 通用媒体下载
（qt_media_grab.MediaGrabDialog）与 抓取音频流（qt_dialogs.AudioGrabDialog）。这一行坑很密
（重建后要保住选中项、`Qt.ItemDataRole` 不是 `ItemRole`…），复制第二份等于把每个坑再踩一遍。
项目里两个抓取弹窗确实是"复制-改写不继承"，但那是针对**整体**结构（按钮集、worker 拓扑差别
很大）；配方行两边完全同构，抽出来是净收益。

⚠ 2026-09-11 起本行**只管选择，不管管理**。原来这里还有「停用/启用」「导入配方」（音频弹窗多
一个「打开配方目录」），现在导入/启停/查看/打开目录统一收进 `qt_config_manager.ConfigManagerDialog`。
不这么收的话，同一套管理面会在这里、主窗配置行、以及将来的新配置类型里各长一份，每份都要把
"导入后热重载""停用要能选回来""按 itemData 取键"重踩一遍；而且两处都能改启停状态是实打实的
隐患 —— 各自持一份下拉快照，一边改完另一边不会刷新。宿主若要给个管理入口，调
`_open_config_manager_dialog()`（它会顺手把本下拉框重建一遍）。

⚠ `RecipeRowMixin` 必须是**纯 Python 类**：不继承 QObject、不写 `__init__`、不声明 Signal。
PySide6/Shiboken 的元类与带 `__init__` 的 mixin 同用容易出问题；Signal 一律留在各自的 QDialog 上。

宿主类需要提供：
  · `self.log(msg)`      写操作日志
  · `self.status_label`  QLabel（钩子改它的文案）—— **可以晚于本行创建**：状态行在布局里通常
    在最下方，而配方行靠上，`_build_recipe_row` 跑的时候它往往还不存在。见 `_recipe_status`。
并实现三个钩子（不实现则用下面的默认值）：
  · `_recipe_auto_hint()`       选中「自动」时的状态文案
  · `_recipe_forced_hint(key)`  选中某份配方时的状态文案
  · `_recipe_busy_widgets()`    任务进行中要一并禁用的控件（默认只有本行下拉框）

语义由各弹窗自己定（钩子就是干这个的）：通用媒体下载里"选中＝强制不降级"（用来验证配方本身
通不通），抓取音频流里"选中＝常规线 0 候选时的补充线"（批量任务不该因为配方没命中就全废）。
本模块只管"选哪份"，不管"选中之后怎么用"，也不管"这份配方是从哪来、开没开"（那是配置管理的事）。
"""

import os

from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush

from qt_theme import LIGHT_THEME as T, open_tool_dialog
from config import (
    LABEL_MEDIA_RECIPE_SEL, ITEM_MEDIA_RECIPE_AUTO, TAG_MEDIA_RECIPE_BUILTIN,
    TAG_MEDIA_RECIPE_USER, MARK_MEDIA_RECIPE_DISABLED, MARK_MEDIA_RECIPE_INVALID,
    TIP_MEDIA_RECIPE_SEL,
    LOG_MEDIA_RECIPE_FORCE_SKIPPED,
)


class RecipeRowMixin:
    """配方选择行。见模块 docstring 的宿主契约。"""

    # ---------------- 宿主可覆盖的钩子 ----------------
    def _recipe_status(self, text):
        """写宿主的状态行。宿主**允许**晚于本行创建 status_label（音频弹窗就是这样：配方行在
        「浏览器旁观」上方，状态行在整个布局最下面）—— 那时本行还没建出来，静默跳过即可，
        下一次交互（切下拉框/启停/跑任务结束）自然会把文案补上。"""
        lbl = getattr(self, "status_label", None)
        if lbl is not None:
            lbl.setText(text)

    def _recipe_auto_hint(self) -> str:
        return "就绪（配方：自动按网址匹配）"

    def _recipe_forced_hint(self, key) -> str:
        return "配方：%s" % key

    def _recipe_busy_widgets(self) -> list:
        return [self.combo_recipe]

    # ---------------- 建行 ----------------
    def _build_recipe_row(self, root, *, label=None, tip=None):
        """把「配方：」下拉框加到 root。

        2026-09-11：原来这一行还有「停用/启用」「导入配方」（音频弹窗多一个「打开配方目录」）。
        现在**只有下拉框** —— 那三个动作连同整个"管理面"收进了统一组件
        `qt_config_manager.ConfigManagerDialog`（主界面「配置管理」白色按钮打开）。
        理由：同一套管理逻辑在这里、主窗配置行、以及将来的新配置类型里各长一份，每份都要把
        "按 itemData 取键""导入后热重载""停用文件仍在、要能选回来"这些坑重踩一遍；
        而两处都能改启停状态还有个实打实的隐患 —— 两边各持一份下拉快照，一边改完另一边不刷新。
        **选中之后怎么用（强制/补充线）仍然由各弹窗自己定**，那一层语义没有动。
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        _tip = tip or TIP_MEDIA_RECIPE_SEL
        lbl = QLabel(label or LABEL_MEDIA_RECIPE_SEL)
        lbl.setToolTip(_tip)
        row.addWidget(lbl)

        self.combo_recipe = QComboBox()
        self.combo_recipe.setMinimumWidth(240)
        self.combo_recipe.setToolTip(_tip)
        self.combo_recipe.currentIndexChanged.connect(self._on_recipe_changed)
        row.addWidget(self.combo_recipe, 1)

        root.addLayout(row)
        self._reload_recipe_combo()
        return row

    def _open_config_manager_dialog(self):
        """打开统一配置管理（本行不再自带管理按钮，宿主若有「媒体配方」按钮就指到这里）。

        关窗后必须重建下拉框：配置管理里停用/导入的那一份，正是本下拉框列出来的东西，
        不刷新的话这里显示的还是旧状态，用户会以为"改了没反应"。
        """
        try:
            from qt_config_manager import ConfigManagerDialog
            dlg = ConfigManagerDialog(self, self)
            # 非模态打开（主窗全程可用）；"关窗后重建下拉框"改挂 finished 信号
            open_tool_dialog(self, dlg, on_finished=lambda _c: self._reload_recipe_combo())
        except Exception as e:
            self.log("配置管理打不开：%s" % e)
            return

    # ---------------- 下拉框 ----------------
    def _reload_recipe_combo(self, keep_key=None):
        """重建配方下拉框（第 0 项是「自动」哨兵）。

        范式照抄 main_qt 的 _reload_template_combo：blockSignals → clear → 填 → 复位 → 解锁。
        不 blockSignals 的话 clear() 会触发 currentIndexChanged，回调里再动下拉框 → 递归重建。

        keep_key：重建后停回哪一项（None = 保持当前选中项）。**必须保留选中**：这在配置管理
        改完启停回来刷新时最容易出问题 —— 一重建就跳回「自动」，用户下一眼就找不到自己刚改的是
        哪条，再操作一次更是什么都不发生。
        """
        if keep_key is None:
            keep_key = self.combo_recipe.currentData(Qt.ItemDataRole.UserRole)
        try:
            import media_recipe as mr
            rows = mr.list_recipes()
        except Exception as e:
            rows = []
            mr = None
            self.log(f"读取配方列表失败：{e}")
        self._recipe_rows = rows
        self.combo_recipe.blockSignals(True)
        self.combo_recipe.clear()
        self.combo_recipe.addItem(ITEM_MEDIA_RECIPE_AUTO, "")
        for r in rows:
            tag = TAG_MEDIA_RECIPE_BUILTIN if r["source"] == mr.SRC_BUILTIN \
                else TAG_MEDIA_RECIPE_USER
            text = "%s %s" % (tag, r["name"])
            if not r["ok"]:
                text += " " + MARK_MEDIA_RECIPE_INVALID
            elif not r["enabled"]:
                text += " " + MARK_MEDIA_RECIPE_DISABLED
            self.combo_recipe.addItem(text, r["key"])
            i = self.combo_recipe.count() - 1
            tip = "%s\n%s" % (r["key"], r["path"])
            if not r["ok"]:
                # 不藏起来：用户要能看见自己的配方为什么没生效
                tip += "\n校验未通过：" + "；".join(r["errors"][:3])
                self.combo_recipe.setItemData(i, QBrush(T["muted"]),
                                              Qt.ItemDataRole.ForegroundRole)
            elif not r["enabled"]:
                tip += "\n已停用（可点右侧按钮重新启用）"
                self.combo_recipe.setItemData(i, QBrush(T["muted"]),
                                              Qt.ItemDataRole.ForegroundRole)
            self.combo_recipe.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        idx = 0
        if keep_key:
            for i in range(self.combo_recipe.count()):
                if self.combo_recipe.itemData(i) == keep_key:
                    idx = i
                    break
        self.combo_recipe.setCurrentIndex(idx)
        self.combo_recipe.blockSignals(False)
        self._on_recipe_changed()

    def _current_recipe(self):
        """当前选中的配方行；选在「自动」上 → None。"""
        key = self.combo_recipe.currentData(Qt.ItemDataRole.UserRole)
        if not key:
            return None
        for r in getattr(self, "_recipe_rows", []):
            if r["key"] == key:
                return r
        return None

    def _on_recipe_changed(self, *_a):
        """按选中项给出提示（启停按钮已搬去配置管理，这里只写状态行）。"""
        r = self._current_recipe()
        if r is None:
            self._recipe_status(self._recipe_auto_hint())
            return
        if not r["ok"]:
            self._recipe_status("配方 %s 校验未通过，无法使用" % r["key"])
        elif not r["enabled"]:
            self._recipe_status("配方 %s 已停用（到「配置管理」里启用后才能用它解析）" % r["key"])
        else:
            self._recipe_status(self._recipe_forced_hint(r["key"]))

    # ---------------- 任务进行中 ----------------
    def _recipe_set_busy(self, running: bool):
        """跑任务时锁住配方选择：选中值是按下开始那一刻拍的快照，中途改下拉框会让人
        以为改动生效了（其实这一轮用的还是旧值）。"""
        for w in self._recipe_busy_widgets():
            try:
                w.setEnabled(not running)
            except Exception:
                pass
        if not running:
            self._on_recipe_changed()

    def _forced_key(self) -> str:
        """下拉框选中项 → 传给 resolve_page 的 force_recipe。

        已停用 / 校验没过的配方**不下传**（会在引擎里直接返回 0 轨，用户看到的却只是一句
        "没解析出媒体"）；这里改成按「自动」走，并明确写一条日志说明为什么不听他的。
        """
        r = self._current_recipe()
        if r is None:
            return ""
        if not r["ok"] or not r["enabled"]:
            self.log(LOG_MEDIA_RECIPE_FORCE_SKIPPED % r["key"])
            return ""
        return r["key"]
