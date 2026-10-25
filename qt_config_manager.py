"""
qt_config_manager.py — 统一配置管理组件（2026-09-11 新增）。

一个地方管**全部用户可导入的配置**：媒体配方 / 数据表模板·字段配置 / 字段规则片段。
每一类都提供同一套动作：导入、启用·停用、查看内容、打开目录、重新扫描。

## 为什么要有它

这三类配置在功能上是三套独立机制（媒体配方走 media_recipe 的注册表，字段配置走
config 的 ACTIVE_SCHEMA），但**管理面是一模一样的**：两目录（自带/用户）、`[自带]/[用户]`
标签、停用=引擎不读、导入先校验后复制、状态落在 runtime/*.json。在 GUI 上它们各自长了
一套按钮 —— 主窗「抓取规则：」那行三个、音视频弹窗的配方行三个 —— 每加一类配置就要再
复制一遍，按钮越堆越乱，而且每个坑（按 itemData 取键、导入后要热重载、停用要能选中回来）
都得再踩一次。

收敛成这一处之后，**新增一类配置 = 在 ALL_FAMILIES 里加一个适配器**，界面代码不动。

## 分层

  · `_Family`（及其三个子类）—— 一类配置的适配器。把该类配置的扫描/启停/导入包成**同一组
    方法签名**，界面只认这组签名。三类的底层 API 差别不小（媒体配方返回 (ok, dst, errs, code)
    四元组、字段配置返回 (code, msg) 二元组；前者的 source 字面量是"自带"/"用户"、后者是
    "builtin"/"user"），全部在这一层抹平。
  · `_CfgPage` —— 一个页签：一张表 + 一排按钮。**零业务逻辑**，全走适配器。
  · `ConfigManagerDialog` —— 三个页签 + 底部关闭。

## 宿主用法

    dlg = ConfigManagerDialog(self, log_func=self.log)
    open_tool_dialog(self, dlg)

`log_func` 是宿主的操作日志函数（主窗是 MainWindow.log）——本组件所有动作都往那儿写一行，
用户关掉弹窗后在主界面还能看到自己刚干了什么。

⚠ 本模块**不缓存任何配置数据**：每次 `_scan()` 都重新问引擎要一遍。配置列表本来就小
（三四十份），而缓存正是"用户改了 json / 在别处停用了某份，这里显示的还是旧的"这类
说不清道不明的来源。
"""

import os
import json

from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget, QFileDialog,
    QMessageBox, QAbstractItemView, QTextEdit,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush

from qt_theme import (
    LIGHT_THEME as T, FONTS,
    BTN_SUCCESS_QSS, BTN_WARNING_QSS, BTN_DANGER_QSS,
    BTN_INFO_QSS, BTN_PURPLE_QSS, BTN_MUTED_QSS,
    apply_dialog_window_flags, open_tool_dialog,
)
from config import (
    TITLE_CFG_MGR, TITLE_CFG_VIEW,
    TAB_CFG_MEDIA, TAB_CFG_TABLE, TAB_CFG_RULES,
    COL_CFG_NAME, COL_CFG_SOURCE, COL_CFG_STATE, COL_CFG_USE, COL_CFG_FILE,
    USE_CFG_TABLE, USE_CFG_MATCH,
    SRC_CFG_BUILTIN, SRC_CFG_USER,
    STATE_CFG_ON, STATE_CFG_OFF, STATE_CFG_BAD,
    BTN_CFG_IMPORT, BTN_CFG_TOGGLE_ON, BTN_CFG_TOGGLE_OFF, BTN_CFG_VIEW,
    BTN_CFG_OPEN_DIR, BTN_CFG_REFRESH, BTN_CFG_CLOSE, BTN_CFG_OPEN_FILE,
    TIP_CFG_TOGGLE, TIP_CFG_IMPORT, TIP_CFG_VIEW, TIP_CFG_OPEN_DIR, TIP_CFG_REFRESH,
    HINT_CFG_PICK, HINT_CFG_EMPTY, HINT_CFG_BUILTIN_RO, HINT_CFG_SHARED_STATE,
    LOG_CFG_LOADED, LOG_CFG_TOGGLED, LOG_CFG_IMPORTED, LOG_CFG_IMPORT_FAIL,
    LOG_CFG_OPEN_DIR_FAIL, LOG_CFG_RELOADED,
    MSG_CFG_IMPORT_TITLE, MSG_CFG_BAD, MSG_CFG_EXISTS, MSG_CFG_OVERRIDE,
    MSG_CFG_NO_SELECT, MSG_CFG_READ_FAIL, MSG_CFG_TOGGLE_FAIL, MSG_CFG_OWNER_MISMATCH,
    FILE_FILTER_JSON,
)

# 导入结果码。三类配置的底层返回码字面量不一样（media_recipe.IMP_* 是 "bad"/"exists"/"io"，
# config 是 "bad"/"exists"/"io"），这里统一成自己的一套，界面不去 import 两家的常量。
_IMP_OK, _IMP_BAD, _IMP_EXISTS, _IMP_IO = "ok", "bad", "exists", "io"


# ═══════════════════════════════════════════════════════════════
# 适配器层：把三类配置抹成同一组方法签名
# ═══════════════════════════════════════════════════════════════
class _Family:
    """一类配置的适配器基类。

    子类实现五个方法；界面只调这五个。行字典的**统一形状**：

        {key, name, source, ok, enabled, errors, file, path, use}

    其中 source 一律是给人看的「自带」/「用户」（引擎内部那套 "builtin"/"user" 在子类里抹掉
    —— 这两个词表在 config._field_cfg_files 与 media_recipe.iter_recipe_files 里并存，
    混用过的代价是拿 "自带" 去比 "builtin" 永远为假，而且不报错）。
    """

    key = ""
    tab = ""
    blurb = ""
    has_use_col = False        # 是否需要「用途」列（只有字段配置两类区分得出来）
    import_hint = ""           # 导入文件框的标题

    def scan(self) -> list:
        raise NotImplementedError

    def toggle(self, row, enabled):
        """写启停状态 → (ok, err)。"""
        raise NotImplementedError

    def import_file(self, src, overwrite=False):
        """导入 → (code, msg)，code ∈ {_IMP_OK, _IMP_BAD, _IMP_EXISTS, _IMP_IO}。"""
        raise NotImplementedError

    def dirs(self) -> list:
        """[(标签, 路径)]，用户目录必须排第一（「打开目录」开的是它）。"""
        raise NotImplementedError

    def user_dir(self) -> str:
        raise NotImplementedError

    def override_warn(self, src) -> str:
        """这份要导入的东西会不会顶掉某份自带配置 → 被顶掉那份的键（无 → ""）。

        重叠**不是错误**（用户配置优先正是本功能的卖点），但用户该知道自己这份会顶掉谁 ——
        不然"我导了一份，怎么另一个站点也跟着变了"永远是笔糊涂账。
        """
        return ""

    def view_text(self, row):
        """取原文 → (text, err)。原文优先，因为这个对话框就是给"看看到底写错在哪"用的，
        坏 JSON 解析不出来，恰恰是最需要看到原文的时候。"""
        try:
            with open(row["path"], "r", encoding="utf-8-sig") as fp:
                return fp.read(), ""
        except Exception as e:
            return "", str(e)

    # ---- 供子类复用的小工具 ----
    @staticmethod
    def _row(key, name, source, ok, enabled, errors, path, use=""):
        return {"key": key, "name": name, "source": source, "ok": bool(ok),
                "enabled": bool(enabled), "errors": list(errors or []),
                "file": os.path.basename(path or ""), "path": path or "", "use": use}

    @staticmethod
    def _zh_source(src):
        """引擎内部来源字面量 → 界面用字。认不出的原样返回（不猜，但也不至于把行吞掉）。"""
        return {"builtin": SRC_CFG_BUILTIN, "user": SRC_CFG_USER}.get(str(src or ""), str(src or ""))


class _MediaFamily(_Family):
    """媒体配方（媒体配方/ + 用户配方/）。引擎：media_recipe。"""

    key = "media"
    tab = TAB_CFG_MEDIA
    blurb = ("音视频站点的抓取策略。选中哪份在音视频弹窗里用；这里管的是**全部**配方。\n"
             "同一批站点的两份配方，用户那份会覆盖自带那份（停用你这份，自带的立刻回来）。")
    import_hint = "导入媒体配方"

    @staticmethod
    def _mr():
        import media_recipe as mr      # 懒 import：主窗启动时不背这个模块
        return mr

    def scan(self):
        mr = self._mr()
        out = []
        for r in mr.list_recipes():
            out.append(self._row(r["key"], r["name"], self._zh_source(r["source"]),
                                 r["ok"], r["enabled"], r["errors"], r["path"]))
        return out

    def toggle(self, row, enabled):
        mr = self._mr()
        ok, err = mr.set_recipe_enabled(row["key"], enabled)
        if ok:
            # ⚠ 顺序不能反：set_recipe_enabled 只写状态文件，注册表要 reload 才跟着变。
            # 反过来的话 reload 会拿旧状态重建一遍，界面看起来"点了没反应"。
            mr.reload_user_recipes()
        return ok, err

    def import_file(self, src, overwrite=False):
        mr = self._mr()
        ok, _dst, errs, code = mr.import_recipe_file(src, overwrite=overwrite)
        if ok:
            mr.reload_user_recipes()
            return _IMP_OK, ""
        # media_recipe 的码与我们的字面量一致（bad/exists/io），但仍然显式映射一遍：
        # 哪天那边改了常量值，这里不会静默串味。
        return {"bad": _IMP_BAD, "exists": _IMP_EXISTS, "io": _IMP_IO}.get(code, _IMP_BAD), \
               "；".join(errs[:5]) if errs else "导入失败"

    def override_warn(self, src):
        mr = self._mr()
        spec, errs = mr.load_recipe(src)
        if errs:
            return ""            # 自身就读不了，交给导入期的校验去拒（这里不重复报错）
        return mr.overlapping_builtin(spec)

    def dirs(self):
        mr = self._mr()
        # 自带目录放后面：它只读，列出来是给用户"照着抄一份改"用的
        return [("用户", mr.user_recipe_dir()), ("自带", mr.recipe_dir())]

    def user_dir(self):
        return self._mr().user_recipe_dir()


class _FieldCfgFamily(_Family):
    """字段配置（数据表模板/ + 用户字段配置/ 里「有 columns」的那些）。

    这一页同时是「数据表模板」：一份 JSON 既是建表模板（columns[].type）又是字段匹配配置
    （synonyms / re / rule），所以不拆两页 —— 拆了同一份文件会在两个页签里各出现一次。
    「用途」列区分它能不能建表（engine == schema_v2 才能，这正是「模板：」下拉的入围条件）。
    """

    key = "field"
    tab = TAB_CFG_TABLE
    blurb = ("一份 JSON 同时是**数据表模板**（columns[].type 建表用）和**字段匹配配置**"
             "（synonyms / re / rule 抽取用）。\n"
             "「用途」列写着能不能建表：只有 engine 是 schema_v2 的才会出现在主界面「模板：」下拉里。\n"
             + HINT_CFG_SHARED_STATE)
    has_use_col = True
    import_hint = "导入数据表模板 / 字段配置"
    # ⚠ **不传** import_field_config 的 require_engine：本页同时管"能建表"和"仅字段匹配"两种，
    # 强制 schema_v2 会把后者（合法且常见）挡在门外。用户点了导入却被一个跟他意图无关的
    # 技术条件拒绝，比导进去再用「用途」列告诉他能不能建表糟糕得多。
    # require_engine 本身保留（它是 import_field_config 的公开参数，脚本/单独入口仍可用）。

    def scan(self):
        import config as cfg
        out = []
        for r in cfg.list_field_configs():
            if r["kind"] != "config":
                continue                      # 规则片段归另一个页签
            spec = None
            try:
                with open(r["path"], "r", encoding="utf-8-sig") as fp:
                    spec = json.load(fp)
            except Exception:
                pass
            use = USE_CFG_TABLE if str((spec or {}).get("engine") or "") == "schema_v2" \
                else USE_CFG_MATCH
            out.append(self._row(r["key"], r["name"], self._zh_source(r["source"]),
                                 r["ok"], r["enabled"], r["errors"], r["path"], use))
        return out

    def toggle(self, row, enabled):
        import config as cfg
        ok, err = cfg.set_field_config_enabled(row["key"], enabled)
        if ok:
            _invalidate_field_caches(cfg)
        return ok, err

    def import_file(self, src, overwrite=False):
        import config as cfg
        code, msg = cfg.import_field_config(src, overwrite=overwrite)
        if code == cfg.IMP_FIELD_OK:
            _invalidate_field_caches(cfg)
            return _IMP_OK, msg
        return {cfg.IMP_FIELD_BAD: _IMP_BAD, cfg.IMP_FIELD_EXISTS: _IMP_EXISTS,
                cfg.IMP_FIELD_IO: _IMP_IO}.get(code, _IMP_BAD), msg

    def override_warn(self, src):
        """同名自带配置会被这份顶掉吗？（按文件名主干比 —— 覆盖的判据就是同名）"""
        stem = os.path.splitext(os.path.basename(str(src or "")))[0]
        for r in self.scan():
            if r["source"] == SRC_CFG_BUILTIN and os.path.splitext(r["file"])[0] == stem:
                return r["key"]
        return ""

    def dirs(self):
        import config as cfg
        return [("用户", cfg.FIELD_CONFIG_USER_DIR),
                ("自带·数据表模板", cfg.TABLE_TEMPLATE_DIR)]

    def user_dir(self):
        import config as cfg
        return cfg.FIELD_CONFIG_USER_DIR


class _FieldRuleFamily(_FieldCfgFamily):
    """字段规则片段（字段规则/ + 用户字段配置/ 里「顶层是 {规则名:{…}}」的那些）。

    与字段配置**同一个用户目录**、同一份状态文件、同一套导入接口，只是 kind 不同；
    「导入」时收的也是同一种 json —— 形状由 config._field_cfg_kind() 认，认不出的当场拒绝。
    """

    key = "rule"
    tab = TAB_CFG_RULES
    blurb = ("往字段规则库里补几条 / 覆盖几条规则，供任何字段配置用 \"rule\":\"规则名\" 引用。\n"
             "这类片段**不用选择、导入即自动生效**（它只是给规则库加料）。\n"
             "⚠ 顺序有讲究：配置引用的规则名不在库里时，那份配置会被拒绝导入。\n"
             "   所以**先导规则片段，再导引用它的字段配置**。")
    has_use_col = False
    import_hint = "导入字段规则片段"

    def scan(self):
        import config as cfg
        out = []
        for r in cfg.list_field_configs():
            if r["kind"] != "rules":
                continue
            out.append(self._row(r["key"], r["name"], self._zh_source(r["source"]),
                                 r["ok"], r["enabled"], r["errors"], r["path"]))
        return out

    def dirs(self):
        import config as cfg
        return [("用户", cfg.FIELD_CONFIG_USER_DIR),
                ("自带·字段规则", cfg.FIELD_RULE_DIR)]

    # override_warn 也继承：规则片段的"同名覆盖"判据与字段配置完全一样（按文件名主干），
    # 而两类共用 字段规则/ 与 用户字段配置/ 这两个目录。
    # toggle / import_file / user_dir 同样继承 —— 同一份状态文件、同一套导入接口，
    # 只有 scan() 挑的行和 dirs() 里那个自带目录不同。重写一遍等于给"两处必须一致"制造机会。


ALL_FAMILIES = (_MediaFamily(), _FieldCfgFamily(), _FieldRuleFamily())


def _invalidate_field_caches(cfg):
    """启停/导入之后，把 config 里那两个"看文件签名"的缓存作废掉。

    `config.set_field_config_enabled` 自己就作废了，**但导入新文件不会** —— 新文件改变了
    `字段规则/` 的合并结果，而规则缓存的签名只看已存在文件的内容，看不到"多了一个文件"。
    不作废它，用户会看到"我导入了规则片段，可它在别的配置里不生效"。

    这里是**防御性**调用（`_field_rules_sig` 走 os.listdir，新文件其实会改变签名），
    所以整块 try 包住：它的私有名字改了也只是少一次保险，不该把用户的操作变成报错。
    """
    try:
        cfg._field_rules_cache["sig"] = None
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 页签：一张表 + 一排按钮（零业务逻辑）
# ═══════════════════════════════════════════════════════════════
class _CfgPage(QWidget):
    """一类配置的管理页。所有动作都转给 `self.fam`，本类不认识任何一类配置的细节。"""

    def __init__(self, fam, owner):
        """owner 是 ConfigManagerDialog（不是 QWidget 父子关系用的 parent）。"""
        super().__init__(owner)
        self.fam = fam
        self.owner = owner
        self.log = owner.log
        self.rows = []
        self._build()

    # ---------------- 建界面 ----------------
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        blurb = QLabel(self.fam.blurb)
        blurb.setWordWrap(True)
        blurb.setStyleSheet(
            "color:%s;background:%s;padding:8px;border-radius:6px;" % (T["text_secondary"], T["hover"]))
        root.addWidget(blurb)

        cols = [COL_CFG_NAME, COL_CFG_SOURCE, COL_CFG_STATE]
        if self.fam.has_use_col:
            cols.append(COL_CFG_USE)
        cols.append(COL_CFG_FILE)
        self.tbl = QTableWidget(0, len(cols))
        self.tbl.setHorizontalHeaderLabels(cols)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.itemSelectionChanged.connect(self._refresh_buttons)
        # currentCellChanged 也要接：`setCurrentCell(-1, -1)` 只清"当前格"、不动选择集，
        # 于是 itemSelectionChanged **不触发**，按钮就停在旧的可点状态上（点了才什么都不发生）。
        # reload() 里 setRowCount(0) 前后也是这个形态。多接一个信号是零成本的保险，
        # _refresh_buttons 对"表格里还没有这一行"已经做了防护。
        self.tbl.currentCellChanged.connect(lambda *_: self._refresh_buttons())
        self.tbl.cellDoubleClicked.connect(lambda *_: self._click_view())
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(cols)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.tbl, 1)

        self.detail = QLabel(HINT_CFG_PICK)
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setStyleSheet("color:%s;" % T["text_secondary"])
        root.addWidget(self.detail)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_toggle = QPushButton(BTN_CFG_TOGGLE_ON)
        self.btn_toggle.setStyleSheet(BTN_WARNING_QSS)
        self.btn_toggle.setMinimumWidth(80)
        self.btn_toggle.setToolTip(TIP_CFG_TOGGLE)
        self.btn_toggle.setEnabled(False)
        self.btn_toggle.clicked.connect(self._click_toggle)
        row.addWidget(self.btn_toggle)

        self.btn_import = QPushButton(BTN_CFG_IMPORT)
        self.btn_import.setStyleSheet(BTN_PURPLE_QSS)
        self.btn_import.setToolTip(TIP_CFG_IMPORT)
        self.btn_import.clicked.connect(self._click_import)
        row.addWidget(self.btn_import)

        self.btn_view = QPushButton(BTN_CFG_VIEW)
        self.btn_view.setStyleSheet(BTN_INFO_QSS)
        self.btn_view.setToolTip(TIP_CFG_VIEW)
        self.btn_view.setEnabled(False)
        self.btn_view.clicked.connect(self._click_view)
        row.addWidget(self.btn_view)

        self.btn_dir = QPushButton(BTN_CFG_OPEN_DIR)
        self.btn_dir.setStyleSheet(BTN_MUTED_QSS)
        self.btn_dir.setToolTip(TIP_CFG_OPEN_DIR)
        self.btn_dir.clicked.connect(self._click_open_dir)
        row.addWidget(self.btn_dir)

        btn_re = QPushButton(BTN_CFG_REFRESH)
        btn_re.setStyleSheet(BTN_MUTED_QSS)
        btn_re.setToolTip(TIP_CFG_REFRESH)
        btn_re.clicked.connect(lambda: self.reload())
        row.addWidget(btn_re)

        row.addStretch(1)
        root.addLayout(row)

    # ---------------- 扫描与填充 ----------------
    def reload(self, keep_key=None, quiet=False):
        """重新问引擎要一遍列表并重建表。**不缓存**：缓存正是"显示的还是旧的"的来源。

        keep_key：重建后重新选中哪一行（导入完要停在刚进来的那份上；用户点停用后视线不该跳走）。
        """
        if keep_key is None:
            cur = self.current_row()
            keep_key = cur["key"] if cur else None
        try:
            self.rows = list(self.fam.scan())
        except Exception as e:
            self.rows = []
            self.log("配置管理：读取 %s 列表失败 —— %s" % (self.fam.tab, e))

        self.tbl.setRowCount(0)
        sel = -1
        for r in self.rows:
            i = self.tbl.rowCount()
            self.tbl.insertRow(i)
            state = STATE_CFG_ON if (r["ok"] and r["enabled"]) else \
                (STATE_CFG_BAD if not r["ok"] else STATE_CFG_OFF)
            vals = [r["name"], r["source"], state]
            if self.fam.has_use_col:
                vals.append(r["use"])
            vals.append(r["file"])
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 0:
                    # 键存在第 0 列的 UserRole 里 —— 绝不用显示文本反查：同名（自带/用户各一份）
                    # 反查会拿到第一行，于是「停用」作用到用户没选的那一行。
                    it.setData(Qt.ItemDataRole.UserRole, r["key"])
                if not r["ok"] or not r["enabled"]:
                    it.setForeground(QBrush(T["muted"]))     # 标灰而不是藏起来
                tip = "%s\n%s" % (r["key"], r["path"])
                if not r["ok"]:
                    tip += "\n校验未通过：" + "；".join(r["errors"][:3])
                elif not r["enabled"]:
                    tip += "\n已停用（点左侧按钮可重新启用）"
                it.setToolTip(tip)
                self.tbl.setItem(i, c, it)
            if keep_key and r["key"] == keep_key:
                sel = i

        if sel < 0 and self.tbl.rowCount():
            sel = 0
        if sel >= 0:
            self.tbl.selectRow(sel)
        self._refresh_buttons()

        if not quiet:
            n_b = sum(1 for r in self.rows if r["source"] == SRC_CFG_BUILTIN)
            n_u = sum(1 for r in self.rows if r["source"] == SRC_CFG_USER)
            n_bad = sum(1 for r in self.rows if not r["ok"])
            n_off = sum(1 for r in self.rows if r["ok"] and not r["enabled"])
            self.log(LOG_CFG_LOADED % (self.fam.tab, n_b, n_u, n_bad, n_off))
            for r in self.rows:
                if not r["ok"]:
                    # 坏文件必须点名：引擎对坏配置是**静默跳过**的，不写出来用户根本不知道
                    self.log("  ✗ %s：%s" % (r["key"], r["errors"][0] if r["errors"] else "读不了"))
        return self.rows

    # ---------------- 选中项 ----------------
    def current_row(self):
        i = self.tbl.currentRow()
        if i < 0 or i >= len(self.rows):
            return None
        key = self.tbl.item(i, 0).data(Qt.ItemDataRole.UserRole) if self.tbl.item(i, 0) else None
        # 按 UserRole 里的键回查，不用行号直接索引：表格与 self.rows 只在 reload 内同步，
        # 中间任何一处 selectRow 都不会错位，但用键更稳（将来插入排序/过滤也不会错行）。
        for r in self.rows:
            if r["key"] == key:
                return r
        return None

    def _refresh_buttons(self):
        r = self.current_row()
        if r is None:
            self.btn_toggle.setEnabled(False)
            self.btn_view.setEnabled(False)
            self.btn_toggle.setText(BTN_CFG_TOGGLE_ON)
            self.detail.setText(HINT_CFG_EMPTY if not self.rows else HINT_CFG_PICK)
            return
        # 写坏了的不能启停：它压根没被引擎读进去，禁用它只是在改一个没有意义的开关。
        # 但「查看」要留给它 —— 恰恰是坏的那些才最需要看原文。
        self.btn_toggle.setEnabled(bool(r["ok"]))
        self.btn_view.setEnabled(True)
        self.btn_toggle.setText(BTN_CFG_TOGGLE_OFF if r["enabled"] else BTN_CFG_TOGGLE_ON)
        self.btn_toggle.setStyleSheet(BTN_DANGER_QSS if r["enabled"] else BTN_SUCCESS_QSS)
        bits = ["%s　%s　%s" % (r["key"], r["source"], r["path"])]
        if not r["ok"]:
            bits.append("校验未通过：" + "；".join(r["errors"][:3]))
        if r["source"] == SRC_CFG_BUILTIN:
            bits.append(HINT_CFG_BUILTIN_RO)
        self.detail.setText("\n".join(bits))

    # ---------------- 动作 ----------------
    def _click_toggle(self):
        r = self.current_row()
        if r is None:
            self.log(MSG_CFG_NO_SELECT)
            return
        want = not r["enabled"]
        ok, err = self.fam.toggle(r, want)
        if not ok:
            QMessageBox.warning(self, TITLE_CFG_MGR, MSG_CFG_TOGGLE_FAIL % err)
            self.log(LOG_CFG_IMPORT_FAIL % err)
            return
        self.reload(keep_key=r["key"])
        self.log(LOG_CFG_TOGGLED % (self.fam.tab, "%s【%s】" % (
            "已启用" if want else "已停用", r["key"])))
        if not want and r["source"] == SRC_CFG_USER:
            self.log("  （同名自带配置会重新生效；这份文件还在 %s 里，随时能再启用）"
                     % self.fam.user_dir())

    def _click_import(self):
        try:
            d = self.fam.user_dir()
            os.makedirs(d, exist_ok=True)   # 目录不存在时 Qt 把起始位置落到 cwd，用户会一脸茫然
            src, _f = QFileDialog.getOpenFileName(self, self.fam.import_hint, d, FILE_FILTER_JSON)
        except Exception as e:
            self.log(LOG_CFG_IMPORT_FAIL % e)
            return
        if not src:
            return

        # 顶掉自带的那份之前先说一声。放在正式的导入之前（而不是 after）是因为导入是**落盘**的
        # 动作，问晚了就得回滚；而这里只是想让人知道"你这份会接管哪个站点/哪张表"。
        warn = self.fam.override_warn(src)
        if warn and QMessageBox.question(self, MSG_CFG_IMPORT_TITLE,
                                         MSG_CFG_OVERRIDE % warn) \
                != QMessageBox.StandardButton.Yes:
            return

        code, msg = self.fam.import_file(src)
        if code == _IMP_EXISTS:
            # 目标路径自己拼：两个底层 import 都是"按原文件名拷进用户目录"（已核对），
            # 而它们返回的 msg 一个是错误串、一个是提示串，都指不到具体是哪个文件要覆盖。
            dst = os.path.join(self.fam.user_dir(), os.path.basename(src))
            if QMessageBox.question(self, MSG_CFG_IMPORT_TITLE, MSG_CFG_EXISTS % dst) \
                    != QMessageBox.StandardButton.Yes:
                return
            code, msg = self.fam.import_file(src, overwrite=True)
        if code != _IMP_OK:
            QMessageBox.warning(self, MSG_CFG_IMPORT_TITLE, MSG_CFG_BAD % msg)
            self.log(LOG_CFG_IMPORT_FAIL % msg)
            return

        base = os.path.basename(src)
        self.log(LOG_CFG_IMPORTED % (base, msg or "校验通过"))
        self.log("  存放在 %s（升级不会覆盖这里）" % self.fam.user_dir())
        self.owner.after_import(self.fam.key, base)

    def _click_view(self):
        r = self.current_row()
        if r is None:
            self.log(MSG_CFG_NO_SELECT)
            return
        text, err = self.fam.view_text(r)
        if err:
            QMessageBox.warning(self, TITLE_CFG_VIEW, MSG_CFG_READ_FAIL % err)
            self.log("配置管理：读不了 %s —— %s" % (r["path"], err))
            return
        dlg = ConfigViewDialog(r, text, self)
        open_tool_dialog(self, dlg)

    def _click_open_dir(self):
        try:
            d = self.fam.user_dir()
            os.makedirs(d, exist_ok=True)
            # 汇报比按钮本身重要：配置有毛病时引擎是**静默跳过**的，用户改完 json 需要一条
            # 看得见的反馈。所以打开目录的同时把"哪份生效、哪份写坏、坏在哪"写进操作日志。
            self.reload()
            self.log("配置管理：%s 目录 → %s" % (self.fam.tab, d))
            for label, p in self.fam.dirs():
                self.log("  %s：%s" % (label, p))
            self.detail.setText("已打开：%s" % d)
            os.startfile(d)          # 项目既有写法（main_qt / qt_dialogs / qt_recipe_row 同款）
        except Exception as e:
            self.log(LOG_CFG_OPEN_DIR_FAIL % e)


# ═══════════════════════════════════════════════════════════════
# 内容查看
# ═══════════════════════════════════════════════════════════════
class ConfigViewDialog(QDialog):
    """只读展示一份配置的**原文**（不美化、不重排 —— 用户要对照的是他磁盘上那个文件）。"""

    def __init__(self, row, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("%s — %s" % (TITLE_CFG_VIEW, row["name"]))
        self.resize(760, 560)
        apply_dialog_window_flags(self)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        head = QLabel("%s\n%s" % (row["key"], row["path"]))
        head.setWordWrap(True)
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.setStyleSheet("color:%s;background:%s;padding:8px;border-radius:6px;"
                           % (T["text_secondary"], T["hover"]))
        root.addWidget(head)

        if not row["ok"]:
            bad = QLabel("校验未通过：" + "；".join(row["errors"][:5]))
            bad.setWordWrap(True)
            bad.setStyleSheet("color:%s;background:%s;padding:8px;border-radius:6px;"
                              % (T["danger"], T["hover"]))
            root.addWidget(bad)

        box = QTextEdit()
        box.setReadOnly(True)
        box.setFont(FONTS["mono"])
        box.setPlainText(text)
        root.addWidget(box, 1)

        row_btns = QHBoxLayout()
        b_open = QPushButton(BTN_CFG_OPEN_FILE)
        b_open.setStyleSheet(BTN_MUTED_QSS)
        b_open.setToolTip("用系统默认的编辑器打开这个文件（改完回来点「重新扫描」）")
        b_open.clicked.connect(lambda: self._open_external(row["path"]))
        row_btns.addWidget(b_open)
        row_btns.addStretch(1)
        b_close = QPushButton(BTN_CFG_CLOSE)
        b_close.clicked.connect(self.accept)
        row_btns.addWidget(b_close)
        root.addLayout(row_btns)

    def _open_external(self, path):
        try:
            os.startfile(path)
        except Exception as e:
            QMessageBox.warning(self, TITLE_CFG_VIEW, str(e))


# ═══════════════════════════════════════════════════════════════
# 管理弹窗本体
# ═══════════════════════════════════════════════════════════════
class ConfigManagerDialog(QDialog):
    """统一配置管理。三个页签，每个页签一套同样的动作。

    host 需要提供 `log(msg)`；本组件把它透给各页签，好让用户关掉弹窗后在主界面还能看到
    刚才干了什么（配置类操作的反馈几乎全靠日志 —— 很多东西是"静默生效/静默失效"的）。
    """

    def __init__(self, host=None, parent=None):
        super().__init__(parent)
        self.log = getattr(host, "log", None) or (lambda *_a: None)
        self.setWindowTitle(TITLE_CFG_MGR)
        self.resize(940, 640)
        apply_dialog_window_flags(self)
        self._build()
        self.reload_all(first=True)

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        tip = QLabel(
            "这里是**全部可导入配置**的总入口。三类配置各有自己的页签，动作完全一样：\n"
            "  · 导入…　—— 把你写好的 .json 放进你自己的目录（升级不会碰）\n"
            "  · 停用/启用 —— 停用＝引擎不读它，文件还在原地，随时能再启用\n"
            "  · 查看内容 —— 看原文，写坏了也看得到\n"
            "  · 打开目录 —— 打开你自己的配置目录，并把「哪份生效、哪份写坏」逐条写进主界面日志")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s;background:%s;padding:8px;border-radius:6px;"
                          % (T["text"], T["hover"]))
        root.addWidget(tip)

        self.tabs = QTabWidget()
        self.pages = {}
        for fam in ALL_FAMILIES:
            page = _CfgPage(fam, self)
            self.pages[fam.key] = page
            self.tabs.addTab(page, fam.tab)
        root.addWidget(self.tabs, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        b_close = QPushButton(BTN_CFG_CLOSE)
        b_close.clicked.connect(self.accept)
        row.addWidget(b_close)
        root.addLayout(row)

    # ---------------- 刷新 ----------------
    def reload_all(self, first=False):
        """扫全部页签。

        **每次都要三页全扫**，不只扫当前页：三类配置共用 `用户字段配置/` 这个目录，
        「导入字段配置」也可能把一份规则片段放进来 —— 只刷当前页会让用户以为"导了没反应"。
        页签只有三个、每页几十行，全扫的代价可以忽略。
        """
        for page in self.pages.values():
            page.reload(quiet=True)
        if first:
            self._log_overview()
        else:
            self.log(LOG_CFG_RELOADED)

    def _log_overview(self):
        total_b = total_u = total_bad = total_off = 0
        for page in self.pages.values():
            total_b += sum(1 for r in page.rows if r["source"] == SRC_CFG_BUILTIN)
            total_u += sum(1 for r in page.rows if r["source"] == SRC_CFG_USER)
            total_bad += sum(1 for r in page.rows if not r["ok"])
            total_off += sum(1 for r in page.rows if r["ok"] and not r["enabled"])
        self.log("配置管理：全部配置 自带 %d 份 / 用户 %d 份（写坏 %d、已停用 %d）"
                 % (total_b, total_u, total_bad, total_off))
        self.log("  三类：%s" % "｜".join(
            "%s %d 份" % (p.fam.tab, len(p.rows)) for p in self.pages.values()))
        if total_bad:
            self.log("  ⚠ 有 %d 份配置写坏了，引擎对它们是**静默跳过**的 —— "
                     "在各页签按「打开目录」看逐条汇报，或双击行查看原文" % total_bad)

    # ---------------- 导入后的跨页定位 ----------------
    def after_import(self, fam_key, base_name):
        """导入成功后：三页全刷，并切到文件**真正落进**的那一页。

        为什么要跨页找：用户点的是「数据表模板」页的「导入…」，但他选的 json 可能是一份
        **规则片段**（形状由内容决定，不由按钮决定）。它确实导进去了、也确实生效了，
        只是归到「字段规则片段」页 —— 这个页签是按内容分的，不是按按钮分的。

        ⚠ **不做拦截**。想过给「字段规则片段」页的导入加一道"形状不对就拒绝"的闸，那等于
        逼用户先判断自己写的是哪种形状、再挑按钮，判断错了还得看错误文案去改按钮 —— 比
        直接导进去再把他送到那一页麻烦得多。这里只负责"送到"，不负责"拦"。
        """
        for key, page in self.pages.items():
            page.reload(quiet=True)
        for key, page in self.pages.items():
            hit = [i for i, r in enumerate(page.rows)
                   if r["file"] == base_name and r["source"] == SRC_CFG_USER]
            if hit:
                self.tabs.setCurrentWidget(page)
                page.tbl.selectRow(hit[0])
                if key != fam_key:
                    self.log("  这份文件的形状是「%s」，已归到「%s」页签（同一个用户目录，"
                             "引擎按内容认形状，不看你是从哪个按钮导的）"
                             % (page.fam.tab, page.fam.tab))
                return
        # 落盘成功却扫不到：只可能是刚被删/改名，或者形状引擎认不出（那样导入期就该被拒）。
        # 不静默 —— 用户下一次动作前得知道列表是旧的。
        self.log("  " + MSG_CFG_OWNER_MISMATCH)
