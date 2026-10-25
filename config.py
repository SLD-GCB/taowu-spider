import pickle
import random
import os
import sys
import json
import time
import re
import csv
from typing import Dict, List, Tuple, Optional

# 指纹档位矩阵（纯模块，无 config 依赖，这里仅取其合法 UA 池 / 档位 key 表）
import stealth_profile as _sp

# 导入独立的浏览器路径配置（这里会自动初始化 BASE_DIR）
# ── browser_paths 内联（避免环导入）──
def _get_app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))
BASE_DIR = _get_app_root()
FULL_CHROME_DIR = os.path.join(BASE_DIR, "full_chrome")
CHROME_PATH = os.path.join(FULL_CHROME_DIR, "chrome.exe")
CHROMEDRIVER_PATH = os.path.join(FULL_CHROME_DIR, "chromedriver.exe")

# 路径配置（只保留非浏览器相关的路径）
CHROME_DIR = FULL_CHROME_DIR  # 直接用 browser_paths 里定义好的变量
RULE_LIB_PATH = os.path.join(BASE_DIR, "rule_lib", "anti_crawl_rule.json")
SELECTOR_LIB_PATH = os.path.join(BASE_DIR, "selector_lib", "job_sites.json")
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
MODEL_EXE_PATH = os.path.join(BASE_DIR, "潜龙v2.0.0模型无交互版本", "run_model.exe")
WATCH_DIR = OUTPUT_DIR

os.makedirs(RUNTIME_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ===================== 【数据库字段】 =====================
DB_FIELDS = [
    "企业名称",
    "岗位名称",
    "工作地点",
    "薪资范围",
    "学历要求",
    "工作经验",
    "岗位职责",
    "福利信息",
    "联系方式",
    "job_url",
    "source",
    "content"
]

DB_BASE_FIELDS = [
    "企业名称",
    "岗位名称",
    "工作地点",
    "薪资范围",
    "学历要求",
    "工作经验",
    "岗位职责",
    "福利信息",
    "联系方式",
    "job_url",
    "source"
]

# ===================== 【数据表模板库】2026-09-07 新增，纯数据驱动，不改旧逻辑 =====================
# 多行业数据表模板见 数据表模板/*.json；每套 columns = 9 业务列 + job_url + source，
# 建表后 + id 自动主键 = 库内 12 列（对齐现招聘表）。engine: legacy_recruit=走原招聘抽取；
# schema_v2=按表列名+同义词的通用抽取（schema_engine）。
TABLE_TEMPLATE_DIR = os.path.join(BASE_DIR, "数据表模板")
SYSTEM_COLUMNS = ["job_url", "source"]
# 非招聘字段规则库：字段规则/*.json（每行业一个文件）。模板列可用 "rule":"销量_通用" 引用，
# 共享该字段的 synonyms/re/clean——改规则库一处、引用它的所有行业模板同时生效；列内同键可再局部调整。
FIELD_RULE_DIR = os.path.join(BASE_DIR, "字段规则")
# 运行期“当前任务绑定的模板 schema”。None=招聘默认(原逻辑)，由 GUI 在选模板时设置、解绑时清回 None。
ACTIVE_SCHEMA = None

# ======= 【用户字段配置】2026-09-10 新增，纯数据驱动（对标媒体配方的两目录 + 导入 + 启停）=======
# 用户自己写的字段匹配配置放 用户字段配置/*.json，一个文件两种形状，按形状自动分流：
#   · **字段配置**（顶层有非空 "columns"）：一份完整配置 = 目标表列 + 每列的匹配方式。匹配可以
#     直接写在列里（synonyms / clean / re —— schema_engine 本来就认这些键），也可以写
#     "rule":"规则名" 去引用规则库。导入后出现在「抓取规则：」下拉里，选中即生效。
#   · **规则片段**（顶层是 {规则名: {...}}）：只往字段规则库里补/覆盖若干条，供任何模板用 rule 引用。
# 与媒体配方同款：
#   · 自带目录（数据表模板/ 字段规则/）随升级覆盖；**用户目录升级绝不动**。
#   · 同名时**用户优先**（媒体配方也是"后注册者赢"）；自带那份仍在，停用用户那份即可退回。
#   · 用户目录**绝不加进 build.py 的 COPY_DIRS** —— 那一步是 rmtree + copytree，会把升级用户
#     已写的配置整体删掉，也会把开发机的私货打进发行包（与 用户配方/ 同一条理由）。
FIELD_CONFIG_USER_DIR = os.path.join(BASE_DIR, "用户字段配置")
# 启停状态放 runtime/ 而非配置目录：那个目录的语义是"这里每个 *.json 都是配置"，状态文件放进去
# 必须起 _ 开头才不被扫到，是个留给下一个人踩的雷（媒体配方同款理由）。
FIELD_CONFIG_STATE_PATH = os.path.join(RUNTIME_DIR, "field_config_state.json")
# 导入返回码（与 media_recipe.IMP_* 同款语义）
IMP_FIELD_OK, IMP_FIELD_BAD, IMP_FIELD_EXISTS, IMP_FIELD_IO = "ok", "bad", "exists", "io"

# ===================== 【媒体配方库】2026-09-10 新增，纯数据驱动 =====================
# 音视频站点的抓取策略（“配方”）见 媒体配方/*.json —— 平台 API 位置 + 身份识别 + 参数 + 档位选择，
# 全部写在 JSON 里，用户改配置不改代码。由 media_recipe.py 加载并注册进 media_resolve 的
# 解析器注册表；同 host_suffix 的用户配方**覆盖**内置同名配方（用户配置优先）。
# 文件名以 _ 开头的跳过（放 _模板.json / 说明.md 不会被误当家配方注册）。
#
# 【两套目录】2026-09-10 拆开：自带（随包发、随升级覆盖）与 用户（用户自己写的，升级绝不动）。
#   · 注册次序 = 自带全部先、用户全部后 → 同 host_suffix 后注册者赢 = 用户配置优先。
#   · 用户目录**绝不加进 build.py 的 COPY_DIRS**：加了会把开发机上的用户配方打进发行包，
#     而且那一步是 rmtree + copytree，会把升级用户已写的配方**整体删掉**。
MEDIA_RECIPE_DIR = os.path.join(BASE_DIR, "媒体配方")           # 自带配方（会被升级覆盖）
MEDIA_RECIPE_USER_DIR = os.path.join(BASE_DIR, "用户配方")      # 用户配方（升级不动）
# 启用/停用状态。放 runtime/ 而非配方目录：那个目录的语义是“这里每个 *.json 都是配方”，
# 状态文件放进去必须起 _ 开头才不被扫到，是个留给下一个人踩的雷。runtime/ 是本项目既定的
# 运行期可写位置（cookie jar 就在那儿），且已 makedirs。
MEDIA_RECIPE_STATE_PATH = os.path.join(RUNTIME_DIR, "media_recipe_state.json")

# ===== 【标识符体检】2026-09-11 新增 =====
# 表名与列名不是"随便一个字符串"：DBPlugin.create_table 把它们**直接拼进 CREATE TABLE**
# （config.py 那句 f"`{name}` {dtype}"），所以它们就是 SQL 标识符。
# 以前模板只有作者写的 16 份，无所谓；2026-09-11 开放"用户自由导入数据表模板"之后，这里成了
# 注入口 —— 列名里放一个反引号就能闭合标识符，放一个分号就能续写语句。
# 两道防线，缺一不可：
#   ① 导入期 validate_field_config 拒绝（告诉用户错在哪一条，坏文件根本不落盘）
#   ② 建表期 _clean_ident / _clean_type 净化（挡"手写文件直接丢进目录绕过了导入按钮"）
_IDENT_MAX = 64
_IDENT_BAD_CHARS = "`;\n\r\x00"
_IDENT_BAD_HINT = {"`": "反引号", ";": "分号", "\n": "换行", "\r": "回车", "\x00": "空字符"}
# 建表列类型白名单。create_custom_table 早就有一份同值的，但**自动建表那条路
# （auto_create_fields → template_create_fields → DBPlugin.create_table）不过它**，
# 所以这里这份是必需的，不是重复。
_SAFE_TYPES = ("TEXT", "INTEGER", "REAL", "BLOB")
# 建表语句自己会写 id 主键，模板列再来一个 id 会撞成 "duplicate column name"
_IDENT_RESERVED = ("id",)


def ident_errors(v, where):
    """标识符（表名/列名）体检 → [错误文案]。空值算错，由调用方决定要不要放行。"""
    s = str(v or "").strip()
    if not s:
        return ["%s 不能为空" % where]
    errs = []
    if len(s) > _IDENT_MAX:
        errs.append("%s 太长（%d 个字符，上限 %d）" % (where, len(s), _IDENT_MAX))
    bad = [c for c in _IDENT_BAD_CHARS if c in s]
    if bad:
        errs.append("%s 不能含 %s（它会被拼进建表 SQL，属于注入字符）"
                    % (where, "、".join(_IDENT_BAD_HINT.get(c, repr(c)) for c in bad)))
    return errs


def _clean_ident(v, *, allow_reserved=False):
    """净化标识符 → 合格的返回原名，不合格的返回 ""（调用方据此跳过该列）。"""
    s = str(v or "").strip()
    if not s or len(s) > _IDENT_MAX:
        return ""
    if any(c in s for c in _IDENT_BAD_CHARS):
        return ""
    # id 由建表语句自己写，模板列再叫 id 会撞成 duplicate column
    if not allow_reserved and s.lower() in _IDENT_RESERVED:
        return ""
    return s


def _clean_type(v):
    """类型净化 → 白名单外的（含用户乱写的 "TEXT(255)"、注入串）一律降级 TEXT。"""
    t = str(v or "").strip().upper()
    return t if t in _SAFE_TYPES else "TEXT"


def _legacy_recruit_spec():
    return {"id": "招聘信息", "name": "招聘信息", "engine": "legacy_recruit",
            "desc": "现有招聘表（引擎原样，未改）",
            "columns": [{"name": n, "type": "TEXT", "synonyms": [], "clean": "text"}
                        for n in DB_BASE_FIELDS]}


def list_table_templates():
    """列出可用模板 spec（数据表模板/ + 用户字段配置/ 里的「字段配置」形状）；目录缺失给招聘 legacy 兜底。

    停用的不列入（停用=不可选，与媒体配方的"停用=不注册"同一条语义）。同名时用户那份赢。
    """
    st = _field_config_state()
    by_key = {}
    for source, p, stem, spec in _field_cfg_files("config"):
        if not isinstance(spec, dict) or not st.get(field_cfg_key(source, stem, "config"), True):
            continue
        by_key[spec.get("id") or spec.get("name") or stem] = spec   # 后扫的用户覆盖自带
    out = list(by_key.values())
    if not out:
        out.append(_legacy_recruit_spec())
    return out


def load_table_template(name):
    """按模板 id / name / 文件名主干 / 来源限定键 任一匹配返回 spec；找不到返回 None。

    匹配**取最后一个**——用户目录排在后，所以同名时用户那份赢（与 list_table_templates 一致）。
    传 field_cfg_key() 那种来源限定键（如「自带:配置:电商商品」）时只会命中那一份 —— GUI 下拉框就用它，
    否则「自带/用户同名」两条会指向同一个 spec，看着有两条其实是一条。
    """
    if not name:
        return None
    want = str(name).strip()
    st = _field_config_state()
    found = None
    for source, p, stem, spec in _field_cfg_files("config"):
        if not isinstance(spec, dict) or not st.get(field_cfg_key(source, stem, "config"), True):
            continue
        if want in (spec.get("id"), spec.get("name"), stem,
                    field_cfg_key(source, stem, "config")):
            found = spec
    if found is not None:
        return _resolve_template_spec(found)
    if not os.path.isdir(TABLE_TEMPLATE_DIR) and not os.path.isdir(FIELD_CONFIG_USER_DIR):
        s = _legacy_recruit_spec()
        return s if want in (s.get("id"), s.get("name")) else None
    return None


def template_create_fields(spec):
    """模板 spec → 建表字段 [(列名, 类型), ...]。

    ⚠ 这里是**建表期最后一道净化**，不是简单取字段：两条建表路径（显式 create_custom_table /
    自动 auto_create_fields → DBPlugin.create_table）都汇聚到本函数，所以哪怕有人把坏 JSON
    手写进 用户字段配置/ 绕过了导入期校验，落到 SQL 上的也是净化过的列名与类型。
    跳过：空名、非法名（注入字符/超长）、保留字 id、重名列（重名列会让 CREATE TABLE 直接失败，
    而 create_custom_table 那条路是**静默去重**的 —— 两条路必须同一种行为）。
    """
    fields, seen = [], set()
    for c in (spec or {}).get("columns") or []:
        if not isinstance(c, dict):
            continue
        nm = _clean_ident(c.get("name"))
        if not nm or nm.lower() in seen:
            continue
        seen.add(nm.lower())
        fields.append((nm, _clean_type(c.get("type"))))
    return fields


def auto_create_fields():
    """模板绑定时的“自动建表”列列表。

    供各自动入库入口（不显式传 fields、靠 DBPlugin “缺表自动建”）复用：
      已绑 schema_v2 模板 → 返回模板列，让自动建出的表就是模板结构；
      未绑定 / 招聘 legacy → 返回 None，完全保持原招聘默认静默建表。
    """
    s = ACTIVE_SCHEMA
    if not s or s.get("engine") == "legacy_recruit":
        return None
    if not s.get("columns"):
        return None
    return template_create_fields(s)


# ===================== 【字段规则库】规则合并层（2026-09-07 增，纯加式） =====================
# 数据：字段规则/*.json，每行业一个文件，形如 { "销量_通用": {"synonyms":[...], "clean":"text", "re":[...]}, ... }
# 引用：数据表模板/<行业>.json 的列加 "rule":"销量_通用"。规则名在全部规则文件里唯一（首文件优先）。
# 合并：type/clean 列内覆盖规则；synonyms=规则在前+列内追加(去重)；re=列内在前+规则追加(去重，列内更精确优先)。
_field_rules_cache = {"sig": None, "rules": {}}


def _field_cfg_kind(spec):
    """一份 JSON 是「字段配置」还是「规则片段」——按形状认，没有显式类型字段。

    config：顶层有非空 columns[]（一份完整配置：列 + 匹配）
    rules ：顶层是 {规则名: {...}}（只补/覆盖规则库若干条）
    认不出 → ""（校验会拿它报错，绝不静默当成某一种）。
    """
    if not isinstance(spec, dict):
        return ""
    cols = spec.get("columns")
    if isinstance(cols, list) and cols:
        return "config"
    for k, v in spec.items():
        if str(k).startswith("_"):          # _说明 一类的注释键，不是规则
            continue
        if isinstance(v, dict):
            return "rules"
    return ""


def _field_cfg_files(kind):
    """扫描两个目录 → [(source, path, stem), ...]。**次序即优先级：自带先、用户后。**

    kind="config" → 数据表模板/ + 用户字段配置/ 里的「字段配置」形状
    kind="rules"  → 字段规则/  + 用户字段配置/ 里的「规则片段」形状
    用户目录一个文件两种形状混放，所以两边都要按形状挑（另一形状的交给另一个 kind）。
    文件名 `_` 开头跳过（放 _模板.json / 说明.md 不会被误当配置）。
    """
    out = []
    for source, d in (("builtin", TABLE_TEMPLATE_DIR if kind == "config" else FIELD_RULE_DIR),
                      ("user", FIELD_CONFIG_USER_DIR)):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".json") or fn.startswith("_"):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p, "r", encoding="utf-8") as fp:
                    spec = json.load(fp)
            except Exception:
                out.append((source, p, fn[:-5], None))   # 读不了也列出来：GUI 要能看见"这份是坏的"
                continue
            if _field_cfg_kind(spec) == kind:
                out.append((source, p, fn[:-5], spec))
    return out


def field_cfg_key(source, stem, kind):
    """配置在状态文件/下拉框里的唯一键 —— **来源 + 形状 + 文件名主干**，三段都不能少。

    ① 来源：自带 数据表模板/电商.json 与用户 用户字段配置/电商.json 是**两份不同的东西**（后者覆盖
       前者）。用裸文件名当键，两者会撞成一个，于是最常用的那个动作做不到 ——「我这份自定义的有问题，
       先停掉退回自带的」；一停就两份一起停。
    ② 形状：`企业工商` 在 数据表模板/ 和 字段规则/ 里**同时存在**（模板 + 规则库各一份），只带来源
       就会撞成一个键 —— 停用会连坐（把模板和规则一起停掉），下拉框里两行的 itemData 还会相同，
       「停用/启用」按钮会作用到用户没选的那一行。这不是假想：自带数据今天就是这个样子。
    """
    return "%s:%s:%s" % ("自带" if source == "builtin" else "用户",
                         "配置" if kind == "config" else "规则", stem)


def _field_rules_sig():
    sigs = []
    for source, p, stem, _spec in _field_cfg_files("rules"):
        try:
            sigs.append((source, os.path.basename(p), os.path.getmtime(p)))
        except OSError:
            pass
    return sigs


def load_field_rules():
    """合并 字段规则/ + 用户字段配置/ 里的规则片段 → {规则名: 规则}。文件有改自动重读。

    同名**用户优先**（后扫的覆盖先扫的）——与媒体配方"后注册者赢"同一条语义：
    用户要改某条通用规则的行为，导一份同名规则片段进去即可，不用碰自带文件。
    """
    sig = _field_rules_sig()
    if sig == _field_rules_cache.get("sig") and _field_rules_cache.get("rules") is not None:
        return _field_rules_cache["rules"]
    st = _field_config_state()
    rules = {}
    for source, p, stem, spec in _field_cfg_files("rules"):
        if not isinstance(spec, dict) or not st.get(field_cfg_key(source, stem, "rules"), True):
            continue                        # 读不出来的 / 用户显式停用的，都不参与合并
        for k, v in spec.items():
            if str(k).startswith("_"):
                continue
            if isinstance(v, dict):
                rules[k] = v                # 后扫的（用户）覆盖先扫的（自带）
    _field_rules_cache["sig"] = sig
    _field_rules_cache["rules"] = rules
    return rules


def _dedup_keep(lst):
    seen, out = set(), []
    for x in lst or []:
        if isinstance(x, str) and x.strip() and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _resolve_column(c, rules):
    """单列套规则：无 rule 引用或规则不存在 → 原样（行为逐字节不变）。"""
    name = (c.get("name") or "").strip()
    if not name:
        return dict(c)
    rule = (rules or {}).get(c.get("rule")) if c.get("rule") else None
    if not isinstance(rule, dict):
        return dict(c)
    out = {k: v for k, v in rule.items() if k != "name"}
    out["name"] = name
    for k in ("type", "clean"):
        v = c.get(k)
        if v is not None and str(v) != "":
            out[k] = v
    syn = _dedup_keep(list(rule.get("synonyms") or []) + list(c.get("synonyms") or []))
    if syn:
        out["synonyms"] = syn
    rep = _dedup_keep(list(c.get("re") or []) + list(rule.get("re") or []))
    if rep:
        out["re"] = rep
    return out


def _resolve_template_spec(spec):
    """模板 spec 逐列套用字段规则；无引用/库为空 → columns 原样。"""
    if not isinstance(spec, dict) or not spec.get("columns"):
        return spec
    cols = [_resolve_column(c, load_field_rules()) for c in spec["columns"]]
    out = dict(spec)
    out["columns"] = cols
    return out


# ===================== 【用户字段配置】状态 / 校验 / 导入（2026-09-10 增） =====================
_field_cfg_state_cache = {"mtime": None, "state": {}}


def _field_config_state():
    """读启停状态 {文件名主干: bool}。文件缺失/损坏 → {}（=全启用），绝不抛。

    落盘 {"version":1, "items": {stem: {"enabled": bool}}}；带 version 是为了以后加字段
    （导入时间、属主之类）时认得出老文件。也容忍手写的简写 {"stem": false}。
    """
    p = FIELD_CONFIG_STATE_PATH
    try:
        mt = os.path.getmtime(p)
    except OSError:
        _field_cfg_state_cache.update({"mtime": None, "state": {}})
        return {}
    if _field_cfg_state_cache.get("mtime") == mt:
        return _field_cfg_state_cache["state"]
    out = {}
    try:
        with open(p, "r", encoding="utf-8-sig") as fp:
            raw = json.load(fp)
        items = raw.get("items") if isinstance(raw, dict) else None
        if isinstance(items, dict):
            for k, v in items.items():
                if isinstance(v, dict):
                    out[str(k)] = bool(v.get("enabled", True))
                elif isinstance(v, bool):
                    out[str(k)] = v
    except Exception:
        out = {}       # 状态文件坏了当"全启用"：宁可多启用，也不要静默把用户的配置全停掉
    _field_cfg_state_cache.update({"mtime": mt, "state": out})
    return out


def set_field_config_enabled(key, enabled):
    """写启停状态（key=**来源限定键**，即 field_cfg_key() 的三段式）。返回 (ok, err)。绝不抛。

    别传文件名主干：状态表是按 field_cfg_key() 存、也是按它查的（list_field_configs /
    list_table_configs / get_field_config 三处都是 st.get(field_cfg_key(...))），
    传主干会写进一个**没有任何读者**的键 —— 表现是「点了停用，界面刷新后还是启用」，
    而且不报错。自带/用户同名、模板/规则同名这两种情况也只有三段式键分得开。

    写盘走 tmp + os.replace，读者永远看不到半截文件。**不加锁**：调用点只有 GUI 主线程的
    「停用/启用」按钮，读者（引擎）读的是内存 dict —— 最坏读到旧值，不会读到坏值。
    """
    key = str(key or "").strip()
    if not key:
        return False, "空的配置名"
    st = dict(_field_config_state())
    st[key] = bool(enabled)
    try:
        d = os.path.dirname(FIELD_CONFIG_STATE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = FIELD_CONFIG_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1,
                       "items": {k: {"enabled": v} for k, v in sorted(st.items())}},
                      fp, ensure_ascii=False, indent=2)
        os.replace(tmp, FIELD_CONFIG_STATE_PATH)
    except Exception as e:
        return False, "写状态文件失败：%s" % e
    # 两处缓存都要作废：状态变了，规则合并结果和"可选项列表"都可能变。
    # 规则缓存的签名只看文件，**看不出状态变化** —— 不作废它，用户会看到"停用了但还生效"。
    _field_cfg_state_cache.update({"mtime": None, "state": {}})
    _field_rules_cache["sig"] = None
    return True, ""


def _as_str_list(v, where, errs):
    """取"字符串列表"字段。裸字符串也算一项（与配方各处同约定）；非字符串元素报错。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        out = []
        for i, x in enumerate(v):
            if isinstance(x, str):
                out.append(x)
            else:
                errs.append("%s[%d] 必须是字符串" % (where, i))
        return out
    errs.append("%s 必须是字符串或字符串数组" % where)
    return []


def _check_re_list(v, where, errs):
    """re 逐条试编译 —— **这是全套校验里最值钱的一条**。

    schema_engine._compile_re 对坏正则缓存 False 之后直接跳过，所以用户正则写错的表现是
    "这个字段永远抽不出值"，页面上没有任何提示。载入期拦住，用户才知道错在哪。
    """
    for i, pat in enumerate(_as_str_list(v, where + " 的 re", errs)):
        try:
            re.compile(pat)
        except Exception as e:
            errs.append("%s 的 re[%d] 正则写错了：%s  ——  %s" % (where, i, pat, e))


def validate_field_config(spec, *, rules=None):
    """校验一份「字段配置 / 规则片段」→ [错误文案]（空列表 = 没问题）。绝不抛。"""
    if not isinstance(spec, dict):
        return ["顶层必须是 JSON 对象 {…}"]
    kind = _field_cfg_kind(spec)
    if not kind:
        return ["认不出形状：要么有非空的 columns[]（字段配置），要么顶层是 {规则名: {…}}（规则片段）"]
    errs = []
    if kind == "config":
        # name 与 id 都会当**表名**用（建表 + _batch_insert 的入库路由），所以按标识符体检。
        # 这不是洁癖：用户自由导入之后，这里进来的字符串会被拼进 CREATE TABLE。
        _nm = str(spec.get("name") or spec.get("id") or "").strip()
        if not _nm:
            errs.append("缺少 name（这份配置在下拉框里显示的名字，同时也是要建的表名）")
        else:
            errs.extend(ident_errors(_nm, "name（表名）"))
        if spec.get("id") not in (None, ""):
            errs.extend(ident_errors(spec.get("id"), "id"))
        if spec.get("engine") not in (None, "schema_v2", "legacy_recruit"):
            errs.append("engine 只认 schema_v2（通用字段抽取 + 建表）或 legacy_recruit（招聘老链路），"
                        "现在写的是「%s」" % spec.get("engine"))
        lib = load_field_rules() if rules is None else rules
        _seen_cols = {}
        for i, c in enumerate(spec.get("columns") or []):
            if not isinstance(c, dict):
                errs.append("columns[%d] 必须是对象" % i)
                continue
            nm = str(c.get("name") or "").strip()
            if not nm:
                errs.append("columns[%d] 缺少 name（列名）" % i)
                continue
            w = "列「%s」" % nm
            errs.extend(ident_errors(nm, w + " 的 name"))
            if nm.lower() in _IDENT_RESERVED:
                errs.append("%s 不能用 id 当列名 —— 建表语句自己会写 id 主键，会撞成"
                            "「duplicate column name」" % w)
            if nm.lower() in _seen_cols:
                errs.append("%s 与 columns[%d] 重名（重名列会让建表直接失败）"
                            % (w, _seen_cols[nm.lower()]))
            else:
                _seen_cols[nm.lower()] = i
            if c.get("type") is not None and str(c.get("type")).strip().upper() not in _SAFE_TYPES:
                errs.append("%s 的 type「%s」不在白名单里（只能是 %s）"
                            % (w, c.get("type"), " / ".join(_SAFE_TYPES)))
            if c.get("rule") and str(c["rule"]) not in lib:
                errs.append("%s 引用了规则「%s」，但规则库里没有它（拼错了？还是那份规则片段"
                            "还没导入？）" % (w, c["rule"]))
            if c.get("clean") is not None and not isinstance(c.get("clean"), str):
                errs.append("%s 的 clean 必须是字符串（title/money/datetime/phone/link/longtext/text）" % w)
            _as_str_list(c.get("synonyms"), w + " 的 synonyms", errs)
            _check_re_list(c.get("re"), w, errs)
    else:
        for k, v in spec.items():
            if str(k).startswith("_"):
                continue                      # 注释键（_说明 之类），合并时本来就会跳过
            if not isinstance(v, dict):
                errs.append("规则「%s」的值必须是对象（如 {\"synonyms\":[…], \"clean\":\"text\"}）" % k)
                continue
            w = "规则「%s」" % k
            if v.get("clean") is not None and not isinstance(v.get("clean"), str):
                errs.append("%s 的 clean 必须是字符串" % w)
            _as_str_list(v.get("synonyms"), w + " 的 synonyms", errs)
            _check_re_list(v.get("re"), w, errs)
    return errs


def list_field_configs():
    """列出全部「字段配置」与「规则片段」→ [{key,name,source,kind,path,ok,errors,enabled}]。

    带 source（builtin/user）是给下拉框打 [自带]/[用户] 标签用；带 ok/errors 是为了**把坏文件
    也列出来** —— 原来的扫描对读不了的文件是静默 continue，用户看不见自己写错了哪一份。
    """
    st = _field_config_state()
    rows = []
    for kind in ("config", "rules"):
        for source, p, stem, spec in _field_cfg_files(kind):
            if isinstance(spec, dict):
                errs = validate_field_config(spec)
                name = str(spec.get("name") or spec.get("id") or stem)
            else:
                errs = ["这个 JSON 读不了（语法错误？）"]
                name = stem
            k = field_cfg_key(source, stem, kind)
            rows.append({"key": k, "name": name, "source": source, "stem": stem, "kind": kind,
                         "path": p, "ok": not errs, "errors": errs,
                         "enabled": bool(st.get(k, True))})
    return rows


def list_table_configs():
    """列出**能建表**的数据表模板（engine == schema_v2 的「字段配置」）→ 与 list_field_configs() 同形状。

    为什么要单独一个函数、而不是让 GUI 自己 filter：「模板：」下拉与「抓取规则：」下拉是
    **同一批文件的两种视图** —— 后者列全部字段配置（含 legacy_recruit、含没写 engine 的），
    前者只列能建出表来的。这个判定跨两个目录（数据表模板/ + 用户字段配置/），
    散在 GUI 里早晚两处写岔，收在这里一处。
    """
    st = _field_config_state()
    rows = []
    for source, p, stem, spec in _field_cfg_files("config"):
        if not isinstance(spec, dict):
            continue
        if str(spec.get("engine") or "") != "schema_v2":
            continue                     # 建不了模板表，在「抓取规则：」下拉里照常可见
        errs = validate_field_config(spec)
        k = field_cfg_key(source, stem, "config")
        rows.append({"key": k, "name": str(spec.get("name") or spec.get("id") or stem),
                     "source": source, "stem": stem, "kind": "config",
                     "path": p, "ok": not errs, "errors": errs,
                     "enabled": bool(st.get(k, True))})
    return rows


def get_field_config(key):
    """按来源限定键取一行 → (行, spec)；找不到 / 读不了 → (None, None)。

    行与 list_field_configs() 同形状，多带一个 spec 供「查看」用。
    """
    k = str(key or "").strip()
    if not k:
        return None, None
    for kind in ("config", "rules"):
        for source, p, stem, spec in _field_cfg_files(kind):
            if field_cfg_key(source, stem, kind) != k:
                continue
            errs = (validate_field_config(spec) if isinstance(spec, dict)
                    else ["这个 JSON 读不了（语法错误？）"])
            return ({"key": k, "name": str((spec or {}).get("name") or stem),
                     "source": source, "stem": stem, "kind": kind, "path": p,
                     "ok": not errs, "errors": errs,
                     "enabled": bool(_field_config_state().get(k, True))},
                    spec if isinstance(spec, dict) else None)
    return None, None


def import_field_config(src_path, *, overwrite=False, require_engine=None):
    """把用户选中的 JSON 导进 用户字段配置/ → (code, msg)。

    流程照抄 media_recipe.import_recipe_file：**先校验后复制**（坏文件绝不落盘 —— 否则目录里
    多一份永远不生效的东西，用户只会以为"软件坏了"）、同名要 overwrite、返回码交给 GUI 分流。

    require_engine：管理组件里「导入表模板」用 "schema_v2"。不满足就拒绝 —— 用户点的是
    "导入表模板"，进去一份不出现在「模板：」下拉里的东西，等于什么也没发生，必须当场说清楚。
    """
    import shutil
    fn = os.path.basename(str(src_path or "").strip())
    if not fn:
        return IMP_FIELD_IO, "没选文件"
    if not fn.lower().endswith(".json"):
        return IMP_FIELD_BAD, "只认 .json 文件"
    if fn.startswith("_"):
        return IMP_FIELD_BAD, "文件名不能以 _ 开头（约定：_ 开头的文件引擎不扫）"
    try:
        with open(src_path, "r", encoding="utf-8-sig") as fp:
            spec = json.load(fp)
    except Exception as e:
        return IMP_FIELD_BAD, "读不了这个 JSON：%s" % e
    errs = validate_field_config(spec)
    if errs:
        return IMP_FIELD_BAD, "；".join(errs[:5])
    if require_engine is not None and str(spec.get("engine") or "") != require_engine:
        got = spec.get("engine")
        return IMP_FIELD_BAD, (
            "这份 JSON 的 engine 是「%s」，不是 %s —— 它不会出现在「模板：」下拉里，"
            "建不出表来。它是字段匹配配置吗？那请用「导入字段配置」那个按钮。"
            % (got if got not in (None, "") else "(没写)", require_engine))
    dest = os.path.join(FIELD_CONFIG_USER_DIR, fn)
    if os.path.exists(dest) and not overwrite:
        return IMP_FIELD_EXISTS, "用户字段配置/ 里已有同名文件：%s" % fn
    try:
        os.makedirs(FIELD_CONFIG_USER_DIR, exist_ok=True)
        tmp = dest + ".tmp"
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, dest)
    except Exception as e:
        return IMP_FIELD_IO, "写不进去：%s" % e
    _field_rules_cache["sig"] = None          # 新文件可能改变规则合并结果
    kind = "字段配置" if _field_cfg_kind(spec) == "config" else "规则片段"
    return IMP_FIELD_OK, "已导入（%s）：%s" % (kind, fn)


# 数据库默认连接配置
DEFAULT_DB_INFO = {
    "mode": "inner_sqlite",
    "host": "localhost",
    "user": "root",
    "pwd": "",
    "port": 3306,
    "db": "spider_db",
    "table": "spider_data"
}

# ===================== 【任务/采集常量】 =====================
MAX_TASK_NUM = 200
FAIL_BREAK_NUM = 5
SITE_KEY = "job"
BASE_TIMEOUT = 20
MIN_SLEEP = 0.6
MAX_SLEEP = 2.0
RETRY_TIMES = 3
RUN_MODE = "auto"

# 风控等级映射（Spider_instructor使用）
RISK_LEVEL_MAP = {"low": 1, "medium": 3, "high": 5}
LOG_ENABLE = True
BREAKER_LIMIT = 8

# 任务运行状态标记（修复GUI报错字段）
TASK_FLAG = False

# 延时配置
BASE_WAIT_MIN, BASE_WAIT_MAX = 3, 6
WAIT_SHORT_MIN, WAIT_SHORT_MAX = 1.5, 3
WAIT_REFRESH_MIN, WAIT_REFRESH_MAX = 5, 8
WAIT_VERIFY_MIN, WAIT_VERIFY_MAX = 10, 15
WAIT_LOAD_MIN, WAIT_LOAD_MAX = 15, 25
WAIT_LONG_MIN, WAIT_LONG_MAX = 12, 25
SCROLL_MIN, SCROLL_MAX = 0.8, 2.5
ERROR_SLEEP_MIN, ERROR_SLEEP_MAX = 30, 90
BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX = 3, 8

# Playwright 配置
PLAYWRIGHT_SLOW_MO_MIN, PLAYWRIGHT_SLOW_MO_MAX = 80, 500
SCROLL_TIMES_MIN, SCROLL_TIMES_MAX = 2, 10
SCROLL_STEP_MIN, SCROLL_STEP_MAX = 120, 950
SCROLL_SLEEP_MIN, SCROLL_SLEEP_MAX = 0.15, 1.5
STAY_SLEEP_MIN, STAY_SLEEP_MAX = 2.0, 9.0
PAGE_PRE_SLEEP_MIN, PAGE_PRE_SLEEP_MAX = 0.3, 1.2

# ===================== 【新增：爬虫增强常量】 =====================
# 并发抓取线程数（默认巴蛇8线程，浏览器3线程）
MAX_CONCURRENT = 8
MAX_CONCURRENT_BROWSER = 3
MAX_CONCURRENT_PARSE = 6

# 驱动池大小（浏览器增强模式复用驱动数）
DRIVER_POOL_SIZE = 2
DRIVER_MAX_REUSE = 15

# 巴蛇性能优化
SESSION_POOL_SIZE = 8
DNS_CACHE_TTL = 300
COOKIE_SAVE_INTERVAL = 30
KEEP_ALIVE_ENABLED = True
CONNECTION_POOL_SIZE = 16

# 图片抓取配置
IMAGE_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(IMAGE_DIR, exist_ok=True)
IMAGE_MIN_SIZE = 1024
IMAGE_TIMEOUT = 8
IMAGE_SCRAPE_ENABLED = True

# ===== 【视频流抓取配置】 =====
VIDEO_DIR = os.path.join(OUTPUT_DIR, "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)
# 出片元数据/封面 best-effort 写入总开关（#7，对等 AUDIO_TAG_ENABLED）；封面文件抓取大小上限
VIDEO_TAG_ENABLED = True
VIDEO_COVER_MAX_BYTES = 2 * 1024 * 1024
VIDEO_TIMEOUT = 20            # 单请求读超时(秒)
VIDEO_CONNECT_TIMEOUT = 8
VIDEO_CONCURRENT = 12          # 单集分片并发线程数（实测多数源约 12 吃到带宽顶）
VIDEO_RETRY = 2
VIDEO_CHUNK_SIZE = 65536      # 直链流式写块
VIDEO_MAX_PAGE_BYTES = 2 * 1024 * 1024  # 探测用 HTML 截断上限
VIDEO_MAX_SEGMENTS = 3000     # HLS 分片上限(防拉爆)
VIDEO_MIN_TS_SEG_BYTES = 256  # 单分片最小字节(防空片/错误页)
# ---- 提速（平衡档）----
VIDEO_SERIES_PARALLEL = 3     # 整部同时下载集数（默认 3；多集命中同一 CDN 时由 host 闸门
                              # 共享配额不超发，命中不同 CDN 时并行各自吃满 → 分源成倍）
VIDEO_POOL_CONNECTIONS = 24   # Session 可缓存的不同 host 连接数
VIDEO_POOL_MAXSIZE = 16       # 单 host 可复用连接数（构建时与 并发×集数 取大）
VIDEO_ORDERED_WINDOW = 8      # TS 有序落盘乱序缓冲上限(分片数)→峰值内存≈window×单分片
# ---- 提速（进阶·按实测定档）----
VIDEO_HOST_CONCURRENCY = 12   # 单 host 并发下限/初始并发（实测多数 CDN 约 12 到带宽顶；
                              # 整部多集命中同一 CDN 时共用此配额，避免超发把源打掉）
VIDEO_CONCURRENCY_MIN = 2     # 自适应并发下限（降级地板）
VIDEO_CONCURRENCY_MAX = 20    # 自适应并发上限（升顶；仅在源允许更高并发且带宽吃满时逼近）
VIDEO_ADAPTIVE_CONCURRENCY = True   # 自适应并发控制器总开关（离线测试设 False → cap 冻结静态）
VIDEO_GLOBAL_CONCURRENCY = 24  # 全局在途请求上限（多 host 分源时每 host ≤12 而总量 ≤24，
                               # 把并行集数带来的总并发约束到礼貌范围内）
# ---- Range 分片（直链/单文件 fMP4 多连接切分）----
VIDEO_RANGE_SPLITS = 6        # 单文件切分数（aria2 式多连接对同一源文件按字节分片）
VIDEO_RANGE_MIN_BYTES = 8 * 1024 * 1024   # total 小于此值不值得切分，走单流
VIDEO_PARTIAL_DIR = os.path.join(os.path.dirname(VIDEO_DIR), "_video_partials")  # 断点续传影子目录
os.makedirs(VIDEO_PARTIAL_DIR, exist_ok=True)
# ---- 全站覆盖（通用深挖解析）----
VIDEO_RESOLVE_MAX_HOPS = 2     # 页面自身无候选时，静态跟随播放器 iframe/refresh 跳转的深度上限
VIDEO_EXT_DIRECT = ("mp4", "flv", "webm", "mov", "m4v", "mkv", "ts")
VIDEO_EXT_PROBE = ("m3u8", "mp4", "flv", "webm", "mov", "m4v")
VIDEO_RE_M3U8 = r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.m3u8[^"\']*?)["\']'
VIDEO_RE_DIRECT = r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.(?:mp4|flv|webm|mov|m4v)[^"\']*?)["\']'
ENABLE_VIDEO_BROWSER_PROBE = True  # 静态未命中或抓流时是否用浏览器兜底（动漫/JS 播放器站点建议开启）
# ---- TLS 指纹伪装（sorani 型"转发套站"：play API 拒绝 requests/curl schannel，但放行真 Chrome）----
# 只作用于 register_impersonate_host() 注册过的 host；其余 host 一律维持 requests，零回归。
VIDEO_IMPERSONATE_ENABLED = True      # 总开关
VIDEO_IMPERSONATE_ENGINE = "auto"     # "auto"|"curl_cffi"|"chrome"（curl_cffi 可用优先；chrome 仅作 API 兜底）
VIDEO_IMPERSONATE_IMP = "chrome124"   # 首选 impersonate 目标；失效自动按候选降级
VIDEO_IMPERSONATE_CANDIDATES = (
    "chrome124", "chrome123", "chrome120", "chrome119",
    "chrome116", "chrome110", "chrome", "safari17_0",
)
VIDEO_SORANI_API_MIN_INTERVAL = 2.0   # 相邻 play-API GET 最小间隔(秒)，防 burst 触发掐连/封禁
VIDEO_SORANI_GRANT_TO_LEAF_DELAY = 10.0  # playUrl 下发后首个叶子(manifest/分片)取流最小间隔(秒)；
#   实测签名授权后过快去取 manifest，CDN 回 403 "No permission"（真 Chrome 播放器启动+缓冲有自然时延）；
#   密集连发会再触发该 IP 临时封（冷却 60~100s+），间隔取保守值，宁慢勿被掐

# ---- #6 P1收官：媒体下载每host代理（仅下载传输层：requests / curl_cffi / curl.exe；不含浏览器层）----
# 只服务媒体下载的每 host 可选 HTTP/SOCKS5 上游。浏览器层(qiongqi 单共享 Chrome)不接代理、不动架构；
# datadome/akamai/f5 的 rotate_fingerprint/cookie_reset 不自动（清 cookie 丢登录态、无代理不治 IP 封）。
# 空 map+空 fallback 时恒 no-op —— 即使 ENABLED=True 也不改变任何请求。socks5 只走 curl_cffi/curl.exe，
# requests 路径拒推（不引 PySocks，避免拖文字/页面路径）。
VIDEO_PROXY_ENABLED = True            # 总开关；空 map/fallback 时恒 no-op
VIDEO_PROXY_HOST_MAP = {}             # {host: "http://.."|"socks5://.."} 单上游（键=netloc 去端口 lower）
VIDEO_PROXY_HOST_POOLS = {}           # {host: [url, ...]} 多上游（健康摘除后在池内轮换）
VIDEO_PROXY_FALLBACK = ""             # 未命中任何映射的 host 用；""=直连
VIDEO_PROXY_SUFFIX_MATCH = True       # host 键支持 ".example" 前缀子域匹配（同 STEALTH_SITE_PROFILE 语义）
VIDEO_PROXY_FAILS_TO_DROP = 3         # 同(host,proxy) 连续失败 ≥ N → 摘除冷却
VIDEO_PROXY_OK_TO_RESTORE = 2         # 冷却后连续成功 ≥ M → 解除冷却
VIDEO_PROXY_COOLDOWN_SEC = 300.0      # 摘除冷却时长(秒)
VIDEO_PROXY_MARK_CODES = (407, 429)   # 这些响应码计代理失败（429 由源或代理 IP 限频均计；仅标记不额外自旋）

# ---- #6 人像节奏（自适应阈值：小单零节拍保吞吐；大单加节拍"别像机器"，与 _Gate/自适应并发并存）----
# 与 VIDEO_ADAPTIVE_CONCURRENCY 分工：自适应管"在途并发"，节拍管"大单礼貌速率/总量闸门"。
# 默认开但阈值巨大 → 单集/小清单（< MIN_SEGMENTS）与短视频集（< MIN_EPISODES）零节拍，回归夹具不受影响；
# 整部/超大清单才自动介入。泵只在调度线程 sleep，绝不进 worker。
VIDEO_PACE_ENABLED = True             # 总开关
VIDEO_PACE_MIN_SEGMENTS = 600         # 单集/单清单分片数 ≥ 此值 → 激活分片级节拍(jitter)
VIDEO_PACE_MIN_EPISODES = 10          # 整部集数 ≥ 此值 → 激活集与集间歇
VIDEO_PACE_JITTER_MIN = 0.15          # 每请求 jitter 下界(秒)
VIDEO_PACE_JITTER_MAX = 0.80          # 每请求 jitter 上界(秒)
VIDEO_PACE_EPISODE_GAP_MIN = 8.0      # 集与集间歇下界(秒)
VIDEO_PACE_EPISODE_GAP_MAX = 20.0     # 集与集间歇上界(秒)
VIDEO_PACE_HOURLY_MAX_REQUESTS = 4000 # 滚动1小时请求硬闸（调度层阻塞到窗口滑出）
VIDEO_PACE_HOURLY_MAX_BYTES = 60 * 1024 ** 3  # 滚动1小时字节软闸(默认60GB)；超限注记+一次短冷却，不整小时停
VIDEO_PACE_HOURLY_BYTE_COOLDOWN = 90.0  # 超字节软闸后的暂停(秒)

# ---- #6 下载层403螺旋（媒体传输侧：被挡→归因→冷却→换档→重签→仍不行人工；滑块/验证码永不自动）----
# 只对 (401,403,406,429,451) 介入；404 等其他 4xx 维持原 b"" 路径（逐字节保现状）。4xx 绝不进 _block_host。
VIDEO_DL_RECOVER_403 = True           # 恢复编排总开关（master off → _fetch_binary 维持 b""，行为与旧一致）
VIDEO_DL_RECOVER_SWAP_PROFILE = True  # 换身份档：profile 池排除当前再抽（仅下载传输侧，线程级单次 override）
VIDEO_DL_RECOVER_SIGNER = True        # 允许已注册 signer 重签（无注册则 no-op）
VIDEO_DL_CHALLENGE_AUTO = False       # 人机验证特征 & wait_and_refresh 类 → 自动开真 Chrome 跑挑战收 cookie（默认关）
VIDEO_DL_CHALLENGE_MIN_INTERVAL = 180.0  # 同 host 自动挑战最小间隔(秒)，防每分片都开 Chrome
VIDEO_DL_429_BACKOFF_MAX = 30.0       # 429 冷却 jitter 退避上限(秒)

# ---- 本地视频播放器（libmpv 内嵌；只播 output/videos 已下载库，不做远程 m3u8）----
MPV_DIR = os.path.join(RUNTIME_DIR, "mpv")
os.makedirs(MPV_DIR, exist_ok=True)
MPV_DLL_NAME = "libmpv-2.dll"       # LGPL dev 构建（含同层依赖 dll），非 mpv.exe
MPV_DLL_PATH = os.path.join(MPV_DIR, MPV_DLL_NAME)

# 页面加载策略：eager = DOMContentLoaded即交互，不必等所有资源加载完
PAGE_LOAD_STRATEGY = "eager"

# Cookie持久化路径
COOKIE_JAR_DIR = os.path.join(RUNTIME_DIR, "cookies")
COOKIE_JAR_PATH = os.path.join(COOKIE_JAR_DIR, "session_cookies.pkl")
os.makedirs(COOKIE_JAR_DIR, exist_ok=True)

# ===================== 【新增：性能开关 & 功能开关】 =====================
# 功能开关（由GUI按钮控制）
ENABLE_IMAGE_SCRAPE = True
ENABLE_COOKIE_PERSIST = True
ENABLE_PROXY = False
ENABLE_RESOURCE_BLOCK = True
ENABLE_AUTO_PAGINATION = False
ENABLE_FAST_FAIL = True
ENABLE_SMART_WAIT = True
ENABLE_CDP_STEALTH = True

# 性能调优参数
PARALLEL_PARSE_ENABLED = True
PREFETCH_DNS_ENABLED = True
DEFERRED_COOKIE_SAVE = True
HTTP2_ENABLED = True
SESSION_REUSE_ENABLED = True

# 请求超时与重试
REQUEST_TIMEOUT = 20
CONNECT_TIMEOUT = 8
MAX_RETRY_PER_URL = 2

# 智能等待倍速（根据页面大小动态调整等待时间）
SMART_WAIT_MULTIPLIER = 0.6

# 统计追踪
STATS_TRACK_ENABLED = True

# 需要拦截的无用资源类型（图片/CSS/字体/媒体），只保留HTML文本
BLOCK_RESOURCE_EXTS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
    "*.css", "*.woff", "*.woff2", "*.ttf", "*.eot", "*.otf",
    "*.mp4", "*.mp3", "*.webm", "*.ogg", "*.flv",
    "*.pdf", "*.zip", "*.rar", "*.7z"
]

# 站点内容选择器：WebDriverWait 等待的关键DOM元素
SITE_CONTENT_SELECTORS = {
    "zhilian": ".job-detail, .job-main, .job-primary",
    "51job": ".job_msg, .tCompanyPage, .cn, .jtag, .job-detail-box, .bmsg",
    "liepin": ".job-intro, .job-info, .resume",
    "boss": ".job-detail, .job-boss-info, .detail-content",
    "lagou": ".job-detail, .job_detail, .position-content, .job-detail-container",
    "default": "body"
}

# ===== v5.0 新增：站点专属反检测配置 =====
SITE_ANTI_DETECT_CONFIG = {
    "51job": {
        "level": "extreme",           # 强制究极防御
        "cdc_patch_required": True,   # 必须修补CDC
        "extra_headers": True,        # 额外请求头
        "cookie_preheat": True,       # 预热Cookie
        "referrer_spoof": True,       # 伪造Referrer
        "wait_extra": 3,              # 额外等待时间
        "url_patterns": ["51job.com", "jobs.51job.com", "search.51job.com"],
    },
    "lagou": {
        "level": "extreme",
        "cdc_patch_required": True,
        "extra_headers": True,
        "cookie_preheat": True,
        "referrer_spoof": True,
        "wait_extra": 3,
        "url_patterns": ["lagou.com", "www.lagou.com", "jobs.lagou.com"],
    },
    "zhilian": {
        "level": "high",
        "cdc_patch_required": False,
    },
    "boss": {
        "level": "high",
        "cdc_patch_required": True,
    },
    "liepin": {
        "level": "high",
        "cdc_patch_required": False,
    },
}

# ===== v9.0 新增：批量发现采集 — 链接发现配置 =====
# 每个站点配置：如何从列表页/搜索页发现详情页链接
LINK_DISCOVERY_CONFIG = {
    "51job": {
        # URL正则：匹配详情页链接的特征（JS端用 RegExp 执行）
        "url_patterns": [
            r"jobs\.51job\.com/[a-z]+/\d+\.html",
            r"jobs\.51job\.com/.*?/\d+\.html\?",
        ],
        # 卡片容器选择器：先找到卡片，再从卡片内找链接
        "card_selectors": [
            ".joblist-item", "[class*='joblist-item']",
            ".e", ".el",
            "[class*='job-card']", "[class*='jobCard']",
            "[class*='result-item']", "[class*='search-result'] li",
        ],
        # CSS选择器兜底：如果正则匹配返回太少，遍历这些选择器找链接
        "selectors": [
            ".joblist-item a[href*='jobs.51job.com']",
            ".e .el[href*='jobs.51job.com']",
            ".j_joblist a[href*='jobs.51job.com']",
            "a[href*='jobs.51job.com']",
        ],
        # JS端链接过滤表达式（eval执行，用于精细筛选）
        "js_link_filters": [
            "href.includes('jobs.51job.com')",
        ],
        # 排除模式：这些URL不是详情页（搜索页、登录页、企业版等）
        "exclude_patterns": [
            r"51job\.com/search",
            r"51job\.com/sso",
            r"ehire\.51job\.com",
        ],
        "min_links_expected": 5,     # 最少期望发现的链接数
        "scroll_needed": True,       # 是否需要滚屏触发懒加载
        "scroll_times": 2,           # 滚屏次数
        "scroll_pause_sec": 2.0,     # 滚屏后等待秒数
        "pagination": {              # 分页支持（v9.0 保留配置，暂不启用）
            "enabled": False,
            "selector": ".p_next, .page-next, [class*='pagination'] a.next",
        },
    },
    "zhilian": {
        "url_patterns": [
            r"jobs\.zhaopin\.com/.*?\.htm",
        ],
        "card_selectors": [
            ".joblist-box__item", ".job-card",
            "[class*='joblist'] [class*='item']",
            "[class*='job-card']",
        ],
        "selectors": [
            ".joblist-box__item a",
            ".jobinfo a[href*='jobs.zhaopin.com']",
            "a[href*='jobs.zhaopin.com']",
        ],
        "exclude_patterns": [],
        "min_links_expected": 5,
        "scroll_needed": True,
        "scroll_times": 2,
        "scroll_pause_sec": 2.0,
        "pagination": {"enabled": False},
    },
    "boss": {
        "url_patterns": [
            r"zhipin\.com/job_detail/[a-z0-9]+\.html",
        ],
        "card_selectors": [
            ".job-card-wrapper", ".job-primary",
            "[class*='job-card']", ".job-list li",
        ],
        "selectors": [
            ".job-card-left a",
            ".job-primary a",
            "a[href*='zhipin.com/job_detail']",
        ],
        "exclude_patterns": [],
        "min_links_expected": 3,
        "scroll_needed": True,
        "scroll_times": 2,
        "scroll_pause_sec": 2.0,
        "pagination": {"enabled": False},
    },
    "liepin": {
        "url_patterns": [
            r"liepin\.com/job/\d+",
            r"liepin\.com/a/\d+",
        ],
        "card_selectors": [
            ".job-list-item", ".job-card",
            "[class*='job-list'] li", "[class*='job-card']",
        ],
        "selectors": [
            ".job-list-item a",
            ".job-card a",
            "a[href*='liepin.com/job']",
            "a[href*='liepin.com/a/']",
        ],
        "exclude_patterns": [],
        "min_links_expected": 3,
        "scroll_needed": True,
        "scroll_times": 2,
        "scroll_pause_sec": 2.0,
        "pagination": {"enabled": False},
    },
    "lagou": {
        "url_patterns": [
            r"lagou\.com/jobs/\d+\.html",
        ],
        "card_selectors": [
            ".item__10RTO", ".job-card",
            "[class*='list'] [class*='item']", "[class*='job']",
        ],
        "selectors": [
            ".item__10RTO a",
            ".job-card a",
            "a[href*='lagou.com/jobs']",
        ],
        "exclude_patterns": [],
        "min_links_expected": 3,
        "scroll_needed": True,
        "scroll_times": 2,
        "scroll_pause_sec": 2.0,
        "pagination": {"enabled": False},
    },
    "job": {  # 通用job站点兜底
        "url_patterns": [],
        "card_selectors": [
            "[class*='job']", "[class*='list-item']",
            "[class*='card']", "li", ".item",
            "[class*='result'] li", "[class*='search'] li",
        ],
        "selectors": [
            "a[href*='/job/']",
            "a[href*='/job_detail/']",
            "a[href*='jobdetail']",
            "a[href*='/jobs/']",
            "a[href*='zhaopin']",
            "a[href*='zhipin']",
        ],
        "js_link_filters": [],
        "exclude_patterns": [
            "javascript:",
            "mailto:",
            "#",
            "login",
            "register",
            "search",
        ],
        "min_links_expected": 1,
        "scroll_needed": False,
        "scroll_times": 1,
        "scroll_pause_sec": 1.0,
        "pagination": {"enabled": False},
    },
}

# 批量发现抓取 — 详情页之间的延时（秒，随机区间）
BATCH_DISCOVERY_DELAY_MIN = 3.0
BATCH_DISCOVERY_DELAY_MAX = 8.0

# 批量发现 — 最多发现的链接数量（安全上限，防止无限抓取）
BATCH_DISCOVERY_MAX_LINKS = 30

# 半自动采集 — 目标采集条数（GUI可调）
BATCH_DISCOVERY_TARGET_COUNT = 15

# 风控 & 代理 & UA
USE_PROXY = False
PROXY_LIST = []
USE_RANDOM_UA = True
USE_HEADERS = True
HIDE_WEBDRIVER = True
SIMULATE_BEHAVIOR = True
PERSIST_COOKIE = True
MIX_REQUEST_SIGN = True
MULTI_BROWSER_ROTATE = True
STATIC_SNAPSHOT_FALLBACK = True
ALTERNATIVE_DOM = False

UA_POOL = list(_sp.PROFILE_UA_POOL)   # 修复旧残缺串（缺 (KHTML, like Gecko)/无 .0.0.0 尾）
                                       # → 完整 Chrome-Win 世代 + Edge 合法 UA；来源单一 = stealth_profile 池
VIEWPORT_POOL = [(1920,1080),(1600,900),(1366,768),(1440,900),(1280,720)]
GEO_POOL = [{"latitude":39.9042,"longitude":116.4074},{"latitude":31.2304,"longitude":121.4737},{"latitude":22.5431,"longitude":114.0579}]
LANG_POOL = ["zh-CN", "zh", "zh-CN,zh;q=0.9"]
TIME_ZONE = "Asia/Shanghai"
BASE_HEADERS_LIST = [{"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"zh-CN,zh;q=0.9","Connection":"keep-alive","Upgrade-Insecure-Requests":"1"}]

# 站点规则
SITE_DELAY_CONFIG = {"zhilian":{"min":2.8,"max":5.5},"51job":{"min":3.0,"max":6.0},"default":{"min":2.2,"max":4.5}}
SITE_BLOCK_WORDS = {"zhilian":["安全拦截","访问受限","403"],"51job":["禁止访问","安全验证","403"],"default":["403","验证码"]}
MAX_REQ_PER_IP = 8

# Chrome 启动参数
CHROME_LAUNCH_ARGS = [
    "--no-sandbox","--disable-setuid-sandbox","--disable-gpu",
    "--disable-blink-features=AutomationControlled","--disable-popup-blocking",
    "--disable-extensions","--disable-dev-shm-usage","--ignore-certificate-errors",
    "--disable-web-security"
]

# 正则 & 过滤规则
CHAR_MAP = {"Ｏ":"0","ｏ":"0","ｌ":"1","Ⅰ":"1"}
NOISE_SYM_PAT = re.compile(r'[┌┐└┘─│㈠㈡㈢㈣㈤㈥〓「」『』《》【】〔〕├┤]')
RUBBISH_PAT = re.compile(r'^\d+:|第\d+条|条\d+第|爬反|反爬|测试风控|DOM|遮罩层|组合|逐条|标签拆分|同色隐藏|交错混排|CSS文字|干扰文本|纯干扰假数据|全局悬浮|低透明度假节点|零宽字符|繁简混用|跨span|碎片|语义撕裂|炼狱级靶场|全类型合集|这里是,无有效信息|XX市人民政府|内部绝密资料', re.M)
WORD_SPACE_PAT = re.compile(r'(\w)\s+(\w)')
TAIL_RUBBISH_PAT = re.compile(r'前方广告无关内容.*$|后方无效文本.*$|={3,}|\(.*\)$')
USELESS_BLOCK_PAT = re.compile(r'<div class="page-header.*?</div>|<div class="business-vip.*?</div>|<div class="safe-tips.*?</div>|<div class="elp-overlay.*?</div>|<script.*?plausible.*?</script>', re.S|re.I)
JOB_MAIN_PAT = re.compile(r'<div class="app__main.*?</div>', re.S|re.I)
RECRUIT_PAT = re.compile(r'(招|招聘|诚聘|急招|高薪诚聘)')
PHONE_REG = re.compile(r"1[3-9]\d{9}")
SALARY_REG = re.compile(r"\d{3,5}[-~至]\d{3,5}")
INTERCEPT_KEYWORDS = ["405", "403", "访问被阻断", "滑动验证", "captcha"]
INVALID_FLAGS = ["405", "访问被阻断", "captcha"]

# 抽取词库
FIELD_LIST = ["岗位名称","工作地点","薪资范围","学历要求","工作经验","岗位职责","福利信息","联系方式"]
BATCH_MAX_LEN = 512
JOB_WORDS = {"收纳整理师","保洁","家政","文员","销售","客服","运营","导购"}
EDU_WORDS = {"大专","本科","高中","中专","硕士","博士","学历不限"}
EXP_WORDS = {"零经验","应届生","1年","2年","3年","经验不限"}
WELFARE_WORDS = {"提成","包吃住","五险一金","双休","时间灵活","晋升"}
AREA_WORDS = {"文冲","同创汇","广州","深圳","北京","上海"}

# 噪声词库
JS_TAGS = ["ko if","/ko","viewType","topbarFlag","tabsFlag","component","flag","isCodeListShow","showError","isSendingSms","accessUpFlag","inputAcceptFlag","tipsFlag","qrcode","register-sms"]
LICENSE_NOISE = ["ICP备","公网安备","人力资源许可证","合字","版权所有","招聘网","网上有害信息","违法不良信息","举报专区","举报电话","监督电话","人力资源与社会保障局","电子营业执照","网络110报警","防骗指南","客服中心","资质公示","发票制度","法律协议","隐私策略"]
LOGIN_NOISE = ["微信扫码","验证码","登录","注册","获取验证码","已阅读并同意","用户服务协议","没有账号","立即注册","+86"]
AD_NOISE = ["VIP","会员","学生版","特权","职业测评","新发岗推荐","竞争力分析","AI改简历","简历模版","AI模拟面试","1V1 HR导师","尊贵身份","立即开通","收藏立即投递"]
WARN_NOISE = ["安全提示","强迫投资","虚拟货币","比特币","转账","汇款","担保人","担保金","扣押证件","牟取利益"]
COMPANY_EXTRA = ["所属行业","企业规模","企业性质","查看企业详情","备受大学生欢迎","求职成长平台","公司介绍","联系方式"]

# 页面结构标记
STRUCTURE_MARKERS = {"岗位名称":["h1","h2","title"],"薪资范围":["元","/月","k","万"],"工作地点":["市","区","省"],"学历要求":["学历","大专","本科","硕士","博士"],"工作经验":["经验","不限","1-3年","3-5年"],"岗位职责":["职责","要求","岗位描述","工作职责"],"公司信息":["公司","企业","规模"]}
STRUCTURE_CLASS_KEYS = {"job_detail":["job-detail","detail","desc","requirement","position-desc"],"company":["company","firm","enterprise"]}

# 白泽防护
BLOCK_URL_RULES = ["*plausible.io*","*google-analytics*","*baidu.com/hm*"]

# ===================== 【核心：硬对抗反检测隐身脚本 v4.0 究极版】 =====================
# 原理：人机检测本质是检测"非真实浏览器"的信号
# 硬对抗策略：Proxy拦截 + 原型链保护 + toString伪装 + CDC内核清理 + iframe穿透 + 调试阻断
# 对抗目标：所有已知人机检测厂商（Cloudflare/Akamai/DataDome/Radware/F5/Imperva/腾讯/阿里）
FULL_STEALTH_JS = r"""
(function() {
    'use strict';
    var _u = {
        r: function(min,max){return Math.floor(Math.random()*(max-min+1))+min;},
        f: function(min,max){return Math.random()*(max-min)+min;},
        p: function(a){return a[Math.floor(Math.random()*a.length)];}
    };

    // ====== 硬对抗核心：Proxy保护所有被修改的原生函数，维持[native code]外观 ======
    var _nativeCode = 'function () { [native code] }';
    var _proxyCache = new WeakMap();

    function _wrapNative(fn, wrapper) {
        if (_proxyCache.has(fn)) return _proxyCache.get(fn);
        var p = new Proxy(fn, {
            apply: function(target, thisArg, args) { return wrapper.apply(thisArg||window, [target].concat(Array.from(args))); },
            get: function(target, prop) {
                if (prop === 'toString') return function(){return _nativeCode;};
                if (prop === Symbol.toPrimitive) return function(){return _nativeCode;};
                return target[prop];
            },
            has: function(target, key) { return key in target; },
            ownKeys: function(target) { return Reflect.ownKeys(target); },
            getOwnPropertyDescriptor: function(target, key) {
                var d = Object.getOwnPropertyDescriptor(target, key);
                if (key === 'toString') { d.value = function(){return _nativeCode;}; d.writable = false; }
                return d;
            }
        });
        _proxyCache.set(fn, p);
        return p;
    }

    function _safeToString(fn) { return _nativeCode; }

    // ====== 第0层：内核级 webdriver 清除（在所有JS执行前，多重路径封死） ======
    // 路径1: 直接属性
    Object.defineProperty(Navigator.prototype, 'webdriver', {
        get: function(){return false;}, set: function(){}, configurable: true, enumerable: false
    });
    // 路径2: 实例属性
    if (navigator.hasOwnProperty('webdriver')) { delete navigator.webdriver; }
    Object.defineProperty(navigator, 'webdriver', {
        get: function(){return false;}, set: function(){}, configurable: true, enumerable: false
    });
    // 路径3: 原型链getOwnPropertyDescriptor
    var _origGOPD = Object.getOwnPropertyDescriptor;
    Object.defineProperty(Object, 'getOwnPropertyDescriptor', {
        value: function(obj, prop) {
            if ((obj === navigator || obj === Navigator.prototype) && prop === 'webdriver') {
                return undefined;
            }
            return _origGOPD.apply(this, arguments);
        },
        writable: true, configurable: true
    });
    // 路径4: Object.keys/Object.getOwnPropertyNames 过滤
    var _origKeys = Object.keys, _origGOPN = Object.getOwnPropertyNames;
    Object.keys = function(obj) {
        var keys = _origKeys(obj);
        if (obj === navigator) { return keys.filter(function(k){return k!=='webdriver';}); }
        return keys;
    };
    Object.getOwnPropertyNames = function(obj) {
        var keys = _origGOPN(obj);
        if (obj === navigator || obj === Navigator.prototype) {
            return keys.filter(function(k){return k!=='webdriver';});
        }
        return keys;
    };
    // 路径5: hasOwnProperty
    var _origHOP = Object.prototype.hasOwnProperty;
    Object.prototype.hasOwnProperty = function(prop) {
        if ((this === navigator || this === Navigator.prototype) && prop === 'webdriver') {
            return false;
        }
        return _origHOP.call(this, prop);
    };

    // ====== 第1层：Chrome Devtools Protocol 痕迹清除 ======
    // 删除所有CDC_前缀属性（ChromeDriver注入的自动化标记）
    var _cdcKeys = [];
    try {
        for (var _k in window) { if (/^cdc_|_cdc/.test(_k)) _cdcKeys.push(_k); }
        _cdcKeys.forEach(function(k){
            try { delete window[k]; } catch(e) {}
            try { Object.defineProperty(window, k, {get:function(){return undefined;},configurable:true}); } catch(e) {}
        });
        // DOM层
        if (document && document.documentElement) {
            var _attrs = document.documentElement.getAttributeNames ? document.documentElement.getAttributeNames() : [];
            _attrs.forEach(function(a){
                if (/^cdc_|_cdc/.test(a)) {
                    try { document.documentElement.removeAttribute(a); } catch(e) {}
                }
            });
        }
    } catch(e) {}

    // ====== 第2层：Chrome Runtime Proxy包装（toString永远返回[native code]） ======
    var _extId = 'nkbihfbeogaeaoehlefnkodbefgpgknn';
    window.chrome = new Proxy(window.chrome || {}, {get: function(t,p){
        if (p === 'runtime') {
            return {id:_extId, onConnect:{addListener:function(){},removeListener:function(){}},
                onMessage:{addListener:function(){},removeListener:function(){}},
                sendMessage:function(){}, connect:function(){return{onMessage:{addListener:function(){}},onDisconnect:{addListener:function(){}},postMessage:function(){},disconnect:function(){}};},
                getManifest:function(){return{name:'MetaMask',version:'12.0.0',manifest_version:3};},
                getURL:function(p){return 'chrome-extension://'+_extId+'/'+p;}, lastError:undefined};
        }
        if (p === 'loadTimes') { return function(){return {requestTime:Date.now()/1000-0.5,startLoadTime:Date.now()/1000-0.48,commitLoadTime:Date.now()/1000-0.3,finishDocumentLoadTime:Date.now()/1000-0.15,finishLoadTime:Date.now()/1000-0.05,firstPaintTime:Date.now()/1000-0.35,firstPaintAfterLoadTime:Date.now()/1000-0.02,navigationType:'Other',wasFetchedViaSpdy:true,wasNpnNegotiated:true,npnNegotiatedProtocol:'http/1.1',connectionInfo:'http/1.1'};}; }
        if (p === 'csi') { return function(){return {startE:Date.now()-500,onloadT:Date.now()-200,pageT:_u.f(200,800),tran:15};}; }
        if (p === 'app') { return {isInstalled:false,InstallState:{DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'},RunningState:{CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'}}; }
        if (p === 'toString') return function(){return _nativeCode;};
        return t[p];
    }});

    // ====== 第3层：Navigator属性Prototype级覆盖（内核级，拦截所有检测路径） ======
    var _navProps = {
        platform: ['Win32','Win32','Win32','Linux x86_64','MacIntel'],
        vendor: ['Google Inc.'],
        vendorSub: [''],
        productSub: ['20030107'],
        hardwareConcurrency: [4,8,8,12,16,8],
        deviceMemory: [4,8,8,16,8],
        maxTouchPoints: [0,0,0,1,0],
        language: ['zh-CN','zh-CN','zh','en-US'],
        languages: [['zh-CN','zh','en-US','en']],
        cookieEnabled: [true],
        doNotTrack: [null,'1',null],
        appVersion: ['5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'],
        userAgent: [navigator.userAgent],
        onLine: [true],
        pdfViewerEnabled: [true]
    };
    Object.keys(_navProps).forEach(function(prop){
        var vals = _navProps[prop];
        try {
            Object.defineProperty(Navigator.prototype, prop, {
                get: function(){return _u.p(vals);},
                configurable: true, enumerable: true
            });
        } catch(e) {}
    });

    // ====== 第2层：Chrome Runtime 完整伪装 ======
    var extId = 'nkbihfbeogaeaoehlefnkodbefgpgknn';
    window.chrome = window.chrome || {};
    window.chrome.app = {isInstalled:false, InstallState:{DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'},RunningState:{CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'}};
    window.chrome.runtime = {
        id: extId, onConnect:{addListener:function(){},removeListener:function(){}},
        onMessage:{addListener:function(){},removeListener:function(){}},
        onInstalled:{addListener:function(){},removeListener:function(){}},
        sendMessage:function(a,b,c){if(c) c({});},
        connect:function(){return{onMessage:{addListener:function(){}},onDisconnect:{addListener:function(){}},postMessage:function(){},disconnect:function(){}};},
        getManifest:function(){return{name:'MetaMask',version:'12.0.0',manifest_version:3};},
        getURL:function(p){return 'chrome-extension://'+extId+'/'+p;},
        lastError: undefined, setUninstallURL: function(){}, reload: function(){}, requestUpdateCheck: function(){}
    };
    window.chrome.loadTimes = function(){return {
        requestTime: Date.now()/1000-0.5, startLoadTime: Date.now()/1000-0.48,
        commitLoadTime: Date.now()/1000-0.3, finishDocumentLoadTime: Date.now()/1000-0.15,
        finishLoadTime: Date.now()/1000-0.05, firstPaintTime: Date.now()/1000-0.35,
        firstPaintAfterLoadTime: Date.now()/1000-0.02, navigationType:'Other',
        wasFetchedViaSpdy:true, wasNpnNegotiated:true, npnNegotiatedProtocol:'http/1.1',
        connectionInfo:'http/1.1', wasAlternateProtocolAvailable:false
    };};
    window.chrome.csi = function(){return {
        startE: Date.now()-500, onloadT: Date.now()-200, pageT: _u.f(200,800),
        tran: 15, onloadT: _u.f(200,600)
    };};

    // ====== 第3层：Navigator Prototype级覆盖（已在上面完成，此处跳过旧代码） ======

    // ====== 第4层：Plugins/MimeTypes Proxy保护 + toString伪装 ======
    (function(){
        var _pArr = [{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
                      {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
                      {name:'Native Client',filename:'internal-nacl-plugin',description:''}];
        var _makeP = function(n,f,d){return{name:n,filename:f,description:d,length:1,item:function(){return null;},namedItem:function(){return null;},
            0:{type:'application/pdf',suffixes:'pdf',description:'Portable Document Format',enabledPlugin:null}};};
        Object.defineProperty(navigator, 'plugins', {get:function(){
            var a = _pArr.map(function(x){return _makeP(x.name,x.filename,x.description);});
            a.item = function(i){return this[i]||null;}; a.namedItem = function(){return null;}; a.refresh = function(){};
            try{Object.setPrototypeOf(a, PluginArray.prototype);}catch(e){}
            return a;
        },configurable:true});
        Object.defineProperty(navigator, 'mimeTypes', {get:function(){
            var a = [{type:'application/pdf',suffixes:'pdf',description:'',enabledPlugin:navigator.plugins[0]},
                     {type:'text/pdf',suffixes:'pdf',description:'',enabledPlugin:navigator.plugins[1]}];
            a.item = function(i){return this[i]||null;}; a.namedItem = function(){return null;}; a.refresh = function(){};
            try{Object.setPrototypeOf(a, MimeTypeArray.prototype);}catch(e){}
            return a;
        },configurable:true});
    })();

    // ====== 第5层：WebGL/Canvas/Audio 硬对抗（Proxy保护 + toString伪装 + 双层噪声） ======
    (function(){
        var _gpuVendors = [
        {vendor:'Google Inc. (Intel)',renderer:'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
        {vendor:'Google Inc. (NVIDIA)',renderer:'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
        {vendor:'Google Inc. (AMD)',renderer:'ANGLE (AMD, Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)'},
        {vendor:'Google Inc. (Intel)',renderer:'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)'}
    ];
    try {
        var _gpu = _u.p(_gpuVendors);
        var _origGetParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p) {
            if (p === 37445) return _gpu.vendor;
            if (p === 37446) return _gpu.renderer;
            if (p === 7937) return _u.p(['Google Inc.','Google Inc. (Intel)','Google Inc. (NVIDIA)']);
            if (p === 7938) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
            if (p === 33901) return {RED_BITS:8,GREEN_BITS:8,BLUE_BITS:8,ALPHA_BITS:8,DEPTH_BITS:24,STENCIL_BITS:8};
            if (p === 33902) return {MAX_TEXTURE_SIZE:16384,MAX_VIEWPORT_DIMS:[16384,16384],MAX_VERTEX_TEXTURE_IMAGE_UNITS:16,MAX_TEXTURE_IMAGE_UNITS:16,MAX_COMBINED_TEXTURE_IMAGE_UNITS:32,MAX_VERTEX_ATTRIBS:16,MAX_VARYING_VECTORS:15};
            if (p === 34176) return _u.p(['WebKit WebGL','ANGLE (Intel)','ANGLE (NVIDIA)']);
            if (p === 34177) return _u.p(['WebKit','ANGLE']);
            return _origGetParam.call(this, p);
        };
        // 同样覆盖 WebGL2
        if (typeof WebGL2RenderingContext !== 'undefined') {
            var _origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(p) {
                if (p === 37445) return _gpu.vendor;
                if (p === 37446) return _gpu.renderer;
                return _origGetParam2.call(this, p);
            };
        }
    } catch(e) {}

    // ====== 第6层：Canvas 指纹噪声注入 ======
    try {
        var _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
            var ctx = this.getContext('2d');
            if (ctx) {
                var imgData = ctx.getImageData(0, 0, 1, 1);
                if (imgData && imgData.data) {
                    imgData.data[_u.r(0,3)] ^= 1;
                    ctx.putImageData(imgData, 0, 0);
                }
            }
            return _origToDataURL.apply(this, arguments);
        };
        var _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
            var result = _origGetImageData.apply(this, arguments);
            if (w > 1 && h > 1 && result && result.data) {
                result.data[_u.r(0, result.data.length-1)] ^= 1;
            }
            return result;
        };
        var _origToBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function(cb, type, quality) {
            var ctx = this.getContext('2d');
            if (ctx) {
                var imgData = ctx.getImageData(0, 0, 1, 1);
                if (imgData && imgData.data) {
                    imgData.data[0] ^= 1;
                    ctx.putImageData(imgData, 0, 0);
                }
            }
            return _origToBlob.apply(this, arguments);
        };
    } catch(e) {}

    // ====== 第7层：AudioContext 指纹噪声 ======
    try {
        var _origCreateOscillator = AudioContext.prototype.createOscillator || window.AudioContext.prototype.createOscillator;
        var _origCreateAnalyser = AudioContext.prototype.createAnalyser || window.AudioContext.prototype.createAnalyser;
        if (_origCreateOscillator) {
            var _origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {
                var data = _origGetChannelData.call(this, channel);
                for (var i = 0; i < Math.min(data.length, 5); i++) {
                    data[i] += _u.f(-0.0000001, 0.0000001);
                }
                return data;
            };
        }
    } catch(e) {}

    // ====== 第8层：Screen/Window 属性真实化 ======
    var _screenW = screen.width || 1920;
    var _screenH = screen.height || 1080;
    var _availW = _screenW;
    var _availH = _screenH - (screen.height - screen.availHeight || 40);
    var _outerW = _screenW - _u.r(0,8);
    var _outerH = _screenH - _u.r(40,80);
    var _innerW = _outerW - _u.r(14,20);
    var _innerH = _outerH - _u.r(80,120);
    Object.defineProperties(screen, {
        width: {get: function(){return _screenW;}},
        height: {get: function(){return _screenH;}},
        availWidth: {get: function(){return _availW;}},
        availHeight: {get: function(){return _availH;}},
        availTop: {get: function(){return 0;}},
        availLeft: {get: function(){return 0;}},
        colorDepth: {get: function(){return 24;}},
        pixelDepth: {get: function(){return 24;}}
    });
    Object.defineProperties(window, {
        outerWidth: {get: function(){return _outerW;}},
        outerHeight: {get: function(){return _outerH;}},
        innerWidth: {get: function(){return _innerW;}},
        innerHeight: {get: function(){return _innerH;}},
        screenX: {get: function(){return 0;}},
        screenY: {get: function(){return 0;}},
        screenLeft: {get: function(){return 0;}},
        screenTop: {get: function(){return 0;}},
        devicePixelRatio: {get: function(){return _u.p([1,1,1.25,2]);}}
    });

    // ====== 第9层：Permissions API 模拟 ======
    var _origPermissionsQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = function(params) {
        var denyList = ['persistent-storage','midi','midi-sysex','usb','bluetooth','nfc','ambient-light-sensor','accelerometer','gyroscope','magnetometer','camera','microphone'];
        if (denyList.indexOf(params.name) >= 0) {
            return Promise.resolve({state:'prompt', onchange:null, addEventListener:function(){}, removeEventListener:function(){}});
        }
        if (params.name === 'notifications') {
            return Promise.resolve({state:_u.p(['denied','prompt']), onchange:null, addEventListener:function(){}, removeEventListener:function(){}});
        }
        try { return _origPermissionsQuery.call(this, params); } catch(e) {
            return Promise.resolve({state:'prompt', onchange:null});
        }
    };

    // ====== 第10层：WebRTC 泄露防护 ======
    try {
        if (window.RTCPeerConnection) {
            var _origCreateDataChannel = RTCPeerConnection.prototype.createDataChannel;
            RTCPeerConnection.prototype.createDataChannel = function() {
                var dc = _origCreateDataChannel.apply(this, arguments);
                Object.defineProperty(dc, 'id', {get: function(){return _u.r(0,65535);}});
                return dc;
            };
            var _origCreateOffer = RTCPeerConnection.prototype.createOffer;
            RTCPeerConnection.prototype.createOffer = function() {
                var args = arguments;
                return _origCreateOffer.apply(this, args).then(function(offer) {
                    if (offer && offer.sdp) {
                        offer.sdp = offer.sdp.replace(/([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})/g, '0.0.0.0');
                        offer.sdp = offer.sdp.replace(/a=candidate:.*\r\n/g, '');
                    }
                    return offer;
                });
            };
        }
    } catch(e) {}

    // ====== 第11层：Battery API 模拟 ======
    try {
        if (navigator.getBattery) {
            var _batteryPromise = navigator.getBattery();
            _batteryPromise.then = function(resolve, reject) {
                return Promise.resolve({
                    charging: true, chargingTime: 0, dischargingTime: Infinity,
                    level: _u.f(0.7, 1.0), onchargingchange: null, onchargingtimechange: null,
                    ondischargingtimechange: null, onlevelchange: null,
                    addEventListener: function(){}, removeEventListener: function(){}
                }).then(resolve, reject);
            };
        }
    } catch(e) {}

    // ====== 第12层：Connection API 模拟 ======
    try {
        Object.defineProperty(navigator, 'connection', {get: function(){
            return {effectiveType:_u.p(['4g','4g','3g']), rtt:_u.r(50,150),
                    downlink:_u.f(5,20), saveData:false, type:_u.p(['wifi','ethernet','wifi']),
                    addEventListener:function(){}, removeEventListener:function(){},
                    onchange:null, ontypechange:null};
        }});
    } catch(e) {}

    // ====== 第13层：Media Devices 模拟 ======
    try {
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            var _origEnumDevices = navigator.mediaDevices.enumerateDevices;
            navigator.mediaDevices.enumerateDevices = function() {
                return _origEnumDevices.call(this).then(function(devices) {
                    if (!devices || devices.length === 0) {
                        return [
                            {deviceId:'default',kind:'audioinput',label:'',groupId:'default'},
                            {deviceId:'default',kind:'audiooutput',label:'',groupId:'default'},
                            {deviceId:'default',kind:'videoinput',label:'',groupId:'default'}
                        ];
                    }
                    return devices;
                });
            };
        }
    } catch(e) {}

    // ====== 第14层：Intl API 时区伪装 ======
    try {
        var _origDateTimeFormat = Intl.DateTimeFormat;
        Intl.DateTimeFormat = function() {
            var instance = new _origDateTimeFormat(...arguments);
            var _origResolved = instance.resolvedOptions;
            instance.resolvedOptions = function() {
                var opts = _origResolved.call(this);
                if (!opts.timeZone || opts.timeZone === 'Etc/Unknown') {
                    opts.timeZone = _u.p(['Asia/Shanghai','Asia/Shanghai','Asia/Tokyo','America/New_York','Europe/London']);
                }
                return opts;
            };
            return instance;
        };
    } catch(e) {}

    // ====== 第15层：Worker 检测防护 ======
    try {
        var _origWorker = window.Worker;
        window.Worker = function() {
            return new _origWorker(...arguments);
        };
        window.Worker.prototype = _origWorker.prototype;
        Object.defineProperty(window.Worker, 'length', {get: function(){return 1;}});
        Object.defineProperty(window.Worker, 'name', {get: function(){return 'Worker';}});
    } catch(e) {}

    // ====== 第16层：Service Worker 隐藏 ======
    try {
        if (navigator.serviceWorker) {
            var _origRegister = navigator.serviceWorker.register;
            navigator.serviceWorker.register = function() {
                return Promise.reject(new DOMException('SecurityError','SecurityError'));
            };
            // 但保持 controller 为 null（真实浏览器首次访问特征）
            Object.defineProperty(navigator.serviceWorker, 'controller', {get: function(){return null;}});
        }
    } catch(e) {}

    // ====== 第17层：SharedWorker 检测 ======
    try {
        if (typeof SharedWorker !== 'undefined') {
            var _origSharedWorker = window.SharedWorker;
            window.SharedWorker = function() {
                return new _origSharedWorker(...arguments);
            };
            window.SharedWorker.prototype = _origSharedWorker.prototype;
        }
    } catch(e) {}

    // ====== 第18层：自动化工具痕迹清理 ======
    var _automationKeys = ['__driver','__selenium','__webdriver','__fxdriver','__nightmare','__phantomas',
                           '__proto__','__defineGetter__','__defineSetter__','__lookupGetter__','__lookupSetter__',
                           'callPhantom','_phantom','phantom','callSelenium','_selenium','_Selenium_IDE_Recorder',
                           'domAutomation','domAutomationController','__webdriver_evaluate','__selenium_evaluate',
                           '__webdriver_script_function','__webdriver_script_func','__webdriver_script_fn',
                           '__fxdriver_evaluate','__driver_unwrapped','__webdriver_unwrapped',
                           '__selenium_unwrapped','__fxdriver_unwrapped','__webdriver_script_fn',
                           '_WEBDRIVER_ELEM_CACHE','__lastWatirAlert','__lastWatirConfirm','__lastWatirPrompt',
                           '_chromedriver','chrome_driver','driver_fingerprint','webdriver_fingerprint',
                           'navigator.webdriver','navigator.__proto__.webdriver'];
    _automationKeys.forEach(function(k){ try{delete window[k];}catch(e){} });
    // 清理 CDC 属性
    Object.keys(window).filter(function(k){return /^cdc_|_cdc/.test(k);}).forEach(function(k){delete window[k];});
    // 清理 document 上的 CDC
    if (document) {
        Object.keys(document).filter(function(k){return /^cdc_|_cdc/.test(k);}).forEach(function(k){delete document[k];});
    }

    // ====== 第19层：document.hidden / visibilityState 动态调整 ======
    Object.defineProperty(document, 'hidden', {get: function(){return false;}, configurable: true});
    Object.defineProperty(document, 'visibilityState', {get: function(){return 'visible';}, configurable: true});
    document.hasFocus = function(){return true;};

    // ====== 第20层：navigator.credentials 防护 ======
    try {
        if (navigator.credentials) {
            var _origPreventSilentAccess = navigator.credentials.preventSilentAccess;
            navigator.credentials.preventSilentAccess = function() {
                return Promise.resolve();
            };
        }
    } catch(e) {}

    // ====== 第21层：requestAnimationFrame 时序保护（防止通过帧率检测无头浏览器） ======
    var _origRAF = window.requestAnimationFrame;
    var _lastTime = 0;
    window.requestAnimationFrame = function(callback) {
        return _origRAF.call(window, function(timestamp) {
            if (_lastTime > 0 && timestamp - _lastTime < 10) {
                timestamp = _lastTime + _u.r(14, 20);
            }
            _lastTime = timestamp;
            callback(timestamp);
        });
    };

    // ====== 第22层：Notification API 隐藏 ======
    try {
        if (window.Notification) {
            Object.defineProperty(window.Notification, 'permission', {get:function(){return _u.p(['default','denied']);}});
            var _origReqPerm = window.Notification.requestPermission;
            window.Notification.requestPermission = function(cb) {
                var result = 'denied';
                if (cb && typeof cb === 'function') setTimeout(function(){cb(result);}, _u.r(50,150));
                return Promise.resolve(result);
            };
        }
    } catch(e) {}

    // ====== 第23层：navigator.keyboard / navigator.bluetooth 等高级API隐藏 ======
    try {
        delete navigator.bluetooth;
        delete navigator.usb;
        delete navigator.serial;
        delete navigator.hid;
        delete navigator.xr;
    } catch(e) {}

    // ====== 第24层：CSS 媒体查询欺骗 ======
    try {
        var _origMatchMedia = window.matchMedia;
        window.matchMedia = function(query) {
            var result = _origMatchMedia.call(window, query);
            if (query === '(prefers-color-scheme: dark)') {
                Object.defineProperty(result, 'matches', {get:function(){return false;}});
            }
            if (query === '(prefers-reduced-motion: reduce)') {
                Object.defineProperty(result, 'matches', {get:function(){return false;}});
            }
            return result;
        };
    } catch(e) {}

    // ====== 第25层：Error.stack 清理（防止追踪到注入脚本） ======
    var _origErrorStack = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (_origErrorStack && _origErrorStack.get) {
        Object.defineProperty(Error.prototype, 'stack', {get: function(){
            var stack = _origErrorStack.get.call(this);
            if (stack) {
                stack = stack.replace(/chrome-extension:\/\/[a-z]{32}\//g, '');
                stack = stack.replace(/\(eval at|<anonymous>/g, '');
            }
            return stack;
        }});
    }

    // ====== 完成标记 ======
    var _k = '_' + Math.random().toString(36).substr(2,8);
    window[_k] = 1;
})();
"""

# 兼容旧变量名（baize.py 引用 LIGHT_DEFENSE_JS）
LIGHT_DEFENSE_JS = FULL_STEALTH_JS

# ===== 多配置profile（随机选取不同指纹组合，用于高级对抗） =====
STEALTH_PROFILES = [
    {"gpu":"Intel UHD 620","cores":8,"memory":8,"resolution":(1920,1080),"os":"Win32"},
    {"gpu":"NVIDIA GTX 1650","cores":12,"memory":16,"resolution":(2560,1440),"os":"Win32"},
    {"gpu":"Intel Iris Xe","cores":4,"memory":8,"resolution":(1366,768),"os":"Win32"},
    {"gpu":"AMD Radeon RX 580","cores":16,"memory":32,"resolution":(1920,1080),"os":"Linux x86_64"},
]

# ===== 反检测等级 =====
ANTI_DETECT_LEVEL = "high"  # low / medium / high / paranoid / extreme
ANTI_DETECT_CONFIG = {
    "low": {
        "stealth_js": True, "cdp_block": True, "resource_block": False,
        "mouse_sim": False, "scroll_sim": False, "cookie_rotate": False,
        "webrtc_block": False, "canvas_noise": False
    },
    "medium": {
        "stealth_js": True, "cdp_block": True, "resource_block": True,
        "mouse_sim": True, "scroll_sim": True, "cookie_rotate": False,
        "webrtc_block": True, "canvas_noise": True
    },
    "high": {
        "stealth_js": True, "cdp_block": True, "resource_block": True,
        "mouse_sim": True, "scroll_sim": True, "cookie_rotate": True,
        "webrtc_block": True, "canvas_noise": True, "profile_rotate": True
    },
    "paranoid": {
        "stealth_js": True, "cdp_block": True, "resource_block": True,
        "mouse_sim": True, "scroll_sim": True, "cookie_rotate": True,
        "webrtc_block": True, "canvas_noise": True, "profile_rotate": True,
        "random_delay_boost": True, "extra_headers_spoof": True
    },
    "extreme": {
        "stealth_js": True, "cdp_block": True, "resource_block": True,
        "mouse_sim": True, "scroll_sim": True, "cookie_rotate": True,
        "webrtc_block": True, "canvas_noise": True, "profile_rotate": True,
        "random_delay_boost": True, "extra_headers_spoof": True,
        "headless_bypass": True, "challenge_bypass": True, "target_hide": True,
        "virtual_display": True, "network_emulate": True, "js_protect": True,
        "dom_protect": True, "smart_challenge_handler": True
    }
}

# ===== #4 stealth_profile 指纹档位矩阵（页/媒体两态同源自洽）=====
# 消费方一律 `import config` 运行时读（勿 from config import * 快照）；本轮无 GUI，改值=改此表。
STEALTH_PROFILE_MODE = "auto"        # "auto"=每任务轮换抽档 / "fixed"=STEALTH_PROFILE_KEY 固定档
                                     #   / "none"=关闭(媒体请求回到逐请求随机,页面态用捕获或随机)
STEALTH_PROFILE_KEY = ""             # fixed 模式锁定的档位 key（chrome131/124/120/116/110/edge131）
STEALTH_PROFILE_POOL = list(_sp.PROFILE_KEYS)   # auto 模式的活动子池（可缩小成仅想用的世代）
STEALTH_SITE_PROFILE = {}            # {netloc/host: profile_key} 站点覆盖：该 host 的每任务档固定
                                     #   （优先于 auto 随机，常用于已被识别为某 Chrome 世代的站点）
STEALTH_SITE_PROFILE_FALLBACK = _sp.FALLBACK_KEY  # 未知档 key → 兜底档（chrome120）

# 穷奇主干常量
PAGE_BLOCK_KEYS = ["403", "405", "访问被阻断", "安全拦截", "禁止访问"]
VERIFY_KEYWORDS = ["滑动验证", "验证码", "人机验证", "captcha", "verify"]
PAGE_MIN_LENGTH = 100
EXTRACT_ROUND = 15
SCROLL_RATIO_LIST = [0.15, 0.35, 0.6, 0.85, 1.0]
TIME_FORMAT = "%Y%m%d_%H%M%S"
EXPORT_TPL_SOURCE = "来源链接：{url}\n站点标识：{site}\n抓取时间：{time}\n"
EXPORT_TPL_SPLIT = "=" * 60
VERIFY_DIALOG_TITLE = "检测人机验证"
VERIFY_DIALOG_MSG = "请手动完成验证码/滑动验证\n完成后点击【确定】继续\n点击【取消】终止任务"

# ===================== 【UI界面 常量/文本/默认值】 =====================
# 主窗口基础配置
WINDOW_TITLE = "梼杌-v4.3"
WINDOW_SIZE = (960, 800)

# 按钮文本
BTN_CLEAR_TXT = "清空日志"
BTN_COMMON = "🔹巴蛇通用模式"
BTN_ENHANCE = "🔸浏览器增强渲染"
BTN_PREVIEW = "页面预览"
BTN_JSON_IMPORT = "📁 读取JSON入库"
BTN_TEST_CONN = "测试连接"
BTN_BACKUP = "备份数据库"
BTN_VIEW_DATA = "查看数据"
BTN_PREVIEW_RUN = "预览"
BTN_PREVIEW_CLOSE = "关闭"
BTN_CSV_TRANSCODE = "CSV乱码转译"
WIN_TITLE_CSV_TRANSCODE = "CSV乱码转译工具"
WIN_SIZE_CSV_TRANSCODE = (1000, 720)
LABEL_CSV_FILE = "CSV文件:"
LABEL_CSV_BROWSE = "浏览..."
LABEL_CSV_DETECT = "检测编码"
LABEL_CSV_ENCODING = "编码:"
LABEL_CSV_CONFIDENCE = "置信度:"
LABEL_CSV_TABLE_PREVIEW = "▼ 表格预览（转译后）"
LABEL_CSV_RAW_PREVIEW = "▼ 原始内容预览（前8000字符）"
LABEL_CSV_SAVE_AS = "另存为UTF-8 CSV"
MSG_CSV_SELECT_FILE = "请先选择一个有效的CSV文件"
MSG_CSV_DETECTING = "正在检测编码..."
MSG_CSV_NO_ENCODING = "未检测到有效编码，无法转存"
MSG_CSV_NO_FILE = "请先选择一个CSV文件并检测编码"
MSG_CSV_DETECT_RESULT = "检测到编码: {enc}  |  置信度: {conf}  |  预览行数: {count}"
MSG_CSV_EXPORTED = "已导出: {name}"
MSG_CSV_SUCCESS = "转译完成！\n源编码: {enc}\n目标编码: UTF-8-BOM\n共 {total} 行\n已保存至: {path}"
TITLE_CSV_OPEN = "选择乱码CSV文件"
TITLE_CSV_SAVE = "另存为UTF-8 CSV"
CSV_PREVIEW_LINES = 80
CSV_DETECT_READ_BYTES = 100 * 1024  # 探测时最多读100KB
CSV_PREVIEW_RAW_CHARS = 8000

# CSV编码探测候选列表（按中文编码优先排列）
CSV_ENCODING_CANDIDATES = [
    "gbk", "gb2312", "gb18030", "big5",
    "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
    "latin-1", "cp1252", "shift_jis", "euc-jp",
]

# 日志文本框配置
LOG_ROWS = 20
LOG_COLS = 80
LOG_SAVE_PATH = os.path.join(BASE_DIR, "run_log.txt")

# 下拉框选项
COMBO_MODE_OPTIONS = ["外置MySQL", "内置SQLite"]
COMBO_DEFAULT_IDX = 1
VAR_DEFAULT_MODE = "inner_sqlite"

# 控件默认值
DB_HOST_DEFAULT = "localhost"
DB_USER_DEFAULT = "root"
DB_PWD_DEFAULT = ""
DB_NAME_DEFAULT = "spider_db"
DB_TABLE_DEFAULT = "spider_db"
PREVIEW_DEFAULT_URL = "https://yiqifu.baidu.com/g/aqc/joblist"

# 标签文本
LABEL_STORAGE_MODE = "存储模式："
LABEL_HOST = "地址:"
LABEL_USER = "账号:"
LABEL_PWD = "密码:"
LABEL_DB = "库名:"
LABEL_TABLE = "表名:"
LABEL_URL_TIP = "批量URL（供巴蛇/浏览器采集使用）"
LABEL_JSON_PATH = "JSON文件路径："
LABEL_LOG_TITLE = "运行日志："

# 日志提示文本
LOG_INIT = "【系统启动完成】"
LOG_TIP1 = "1. 巴蛇/浏览器模式：直接采集并解析字段入库"
LOG_TIP2 = "2. 填写JSON路径 → 点击【读取JSON入库】执行导入"
LOG_NO_URL = "❌ 无URL输入"
LOG_MAX_URL = "❌ 最多支持{MAX_TASK_NUM}条URL"
LOG_COMMON_START = "【巴蛇通用模式】开始采集"
LOG_ENHANCE_START = "【浏览器增强模式】开始采集"
LOG_NO_DATA = "[{idx}] {url} → 无数据"
LOG_INSERT_SUCC = "[{idx}] {url} → 入库成功"
LOG_MANY_FAIL = "⚠️ 连续失败过多，任务终止"
LOG_COMMON_END = "==== 通用采集结束 ===="
LOG_ENHANCE_END = "==== 增强采集结束 ===="
LOG_FILE_NOT_EXIST = "❌ 文件不存在，请检查路径！"
LOG_JSON_ERR = "❌ 读取/解析JSON失败：{e}"
LOG_PARSE_COUNT = "🔍 解析完成，共获取 {cnt} 条候选数据"
LOG_ITEM_SKIP = "第{idx}条 → 全空，跳过"
LOG_ITEM_SUCC = "第{idx}条 → 入库成功"
LOG_FINISH = "✅ 本次完成：入库 {succ} 条，跳过 {skip} 条"
LOG_DB_CONN_ERR = "❌ 数据库连接失败：{e}"
LOG_CONN_SUCC = "✅ 数据库连接正常"
LOG_BACKUP_SUCC = "✅ 备份完成：{dst}"
LOG_MYSQL_BACKUP_TIP = "💡 MySQL备份请手动执行：mysqldump -u账号 -p 库名 > 备份.sql"
LOG_PLUGIN_ERR = "⚠️ 数据查看插件加载失败：{e}"
LOG_PREVIEW_LEN = "长度：{len}\n\n{content}"

# 弹窗提示
MSG_WARN_NO_URL = "请输入URL"

# ===================== 【v12.1 Qt UI 新增按钮文本】 =====================
# 采集模式按钮（彩色按钮区）
BTN_BATCH_DISCOVERY = "半自动采集"
BTN_EXTREME_MODE = "究极模式采集"
BTN_HARD_SITE = "🔥高难度网站抓取"
BTN_STATS = "查看统计"
BTN_SAVE_SESSION = "保存会话"
BTN_CLEAR_COOKIE = "清除Cookie"
BTN_CDC_PATCH = "修补CDC"
BTN_CDC_STATUS = "CDC状态"
BTN_STEALTH_TEST = "隐身检测"
BTN_STOP_ALL = "终止全部"

# 工具按钮（白色按钮区）
BTN_SINGLE_FETCH = "单条抓取"
BTN_MANUAL_INPUT = "手动录入"
BTN_URL_DEDUP = "URL去重"
BTN_RESET_STATS = "重置统计"
BTN_COOKIE_MANAGER = "Cookie管理"
BTN_IMAGE_DIR = "图片目录"
BTN_VIDEO_GRAB = "视频流抓取"
BTN_MANUAL_FIELD_INPUT = "📝 手动字段录入"
BTN_APPLY = "应用"

# 功能开关标签
LABEL_HTTP_CONCURRENT = "HTTP并发:"
LABEL_BROWSER = "浏览器:"
LABEL_TARGET_COUNT = "条数:"
LABEL_ANTI_DETECT = "反检测等级:"

# 反检测等级下拉选项
COMBO_DEFENSE_OPTIONS = ["低", "中", "高", "终极", "究极"]
COMBO_DEFENSE_DEFAULT = "高"

# 反检测等级中英文映射
DEFENSE_LEVEL_MAP = {"低": "low", "中": "medium", "高": "high", "终极": "paranoid", "究极": "extreme"}

# 模式配置日志模板（字符串常量引用在后面定义，此处用字面量）
LOG_COMMON_CONFIG = "模式配置：HTTP并发={http} 图片抓取={image} 并行解析={parse}"
LOG_ENHANCE_CONFIG = "模式配置：浏览器并发={browser} 图片抓取={image} Cookie持久化={cookie}"
LOG_ANTI_DETECT_DETAIL = "  隐身JS={stealth} CDP拦截={cdp}"

# 开关标签文本
CHK_IMAGE_SCRAPE = "图片抓取"
CHK_COOKIE_PERSIST = "Cookie持久化"
CHK_RESOURCE_BLOCK = "资源拦截"
CHK_FAST_FAIL = "快速失败"
CHK_PARALLEL_PARSE = "并行解析"
CHK_STRIP_MODE = "剃毛模式"
CHK_PROFILE_ROTATE = "配置轮换"
CHK_DELAY_BOOST = "延迟增强"
CHK_DIRECT_CHROME = "零ChromeDriver"

# 新增采集模式日志
LOG_BATCH_DISCOVERY_START = "【半自动采集】启动 — 手动辅助 + 自动发现详情页"
LOG_EXTREME_START = "【究极模式】启动20层全量防御 + 智能挑战绕过..."
LOG_HARD_SITE_START = "【🔥高难度网站抓取】启动全量反检测 + 剃毛 + 额外等待..."
LOG_BATCH_DISCOVERY_END = "==== 半自动采集结束 ===="
LOG_EXTREME_END = "==== 究极采集结束 ===="
LOG_HARD_SITE_END = "==== 高难度抓取结束 ===="
LOG_SINGLE_FETCH = "单条抓取：{url}"
LOG_SINGLE_FETCH_OK = "单条抓取成功：{count} 条"
LOG_SINGLE_FETCH_FAIL = "单条抓取 → 无数据"

# 状态栏文本
STATUS_CONN_PREFIX = "连接: "
STATUS_DB_PREFIX = "库: "
STATUS_READY = "就绪"
STATUS_PROGRESS_CRAWL = "抓取中..."
STATUS_PROGRESS_BROWSER = "浏览器抓取中..."
STATUS_PROGRESS_EXTREME = "究极抓取中..."
STATUS_PROGRESS_BATCH = "半自动抓取中..."
STATUS_PROGRESS_HARD = "高难度抓取中..."

# Cookie / 隐身操作日志
LOG_COOKIE_CLEARED = "Cookie已全部清除"
LOG_COOKIE_CLEAR_FAIL = "清除Cookie失败：{e}"
LOG_COOKIE_SAVED = "会话Cookie已保存：{path}  ({count} 条)"
LOG_COOKIE_SAVE_FAIL = "保存会话Cookie失败：{e}"
LOG_SESSION_SAVED = "会话Cookie已保存：{path}  ({count} 条)"
LOG_CDC_PATCHING = "正在修补ChromeDriver CDC特征..."
LOG_CDC_PATCH_OK = "CDC修补成功！chromedriver.exe中的$cdc_已被替换"
LOG_CDC_PATCH_FAIL = "CDC修补失败或已经是修补状态"
LOG_CDC_PATCH_ERR = "CDC修补异常：{e}"
LOG_CDC_PATCHED = "CDC修补状态：已修补（chromedriver.exe中无$cdc_残留）"
LOG_CDC_UNPATCHED = "CDC修补状态：未修补！请点击【修补CDC】按钮修复"
LOG_CDC_CHECK_ERR = "CDC检查异常：{e}"
LOG_STEALTH_START = "隐身检测启动中..."
LOG_STEALTH_ERR = "隐身检测异常：{e}"
LOG_STEALTH_PASS = "隐身检测通过：未发现机器人特征"
LOG_STEALTH_WARN = "隐身检测警告：可能暴露部分机器人特征"
LOG_STOP_ALL = "已强制终止所有爬虫任务"
LOG_DIRECT_CHROME_ON = "零ChromeDriver模式已开启"
LOG_DIRECT_CHROME_OFF = "已切换回标准ChromeDriver模式"

# 统计日志
LOG_STATS_HEADER = "=" * 50
LOG_STATS_HTTP = "[巴蛇HTTP] 总请求={total} 成功={success} 失败={fail} 拦截={block} 成功率={rate}% 速度={speed}条/分"
LOG_STATS_BROWSER = "[浏览器增强] 总请求={total} 成功={success} 失败={fail} 成功率={rate}%"
LOG_STATS_BYTES = "[总计下载] {bytes} 字节"
LOG_STATS_ERR = "获取统计失败：{e}"
LOG_STATS_RESET = "统计已重置"
LOG_STATS_RESET_ERR = "重置统计失败：{e}"

# 菜单文本
MENU_FILE = "文件(&F)"
MENU_VIEW = "视图(&V)"
MENU_HELP = "帮助(&H)"
MENU_VIEW_LOG = "查看日志文件"
MENU_EXIT = "退出(&X)"
MENU_RESET_LAYOUT = "重置布局"
MENU_ABOUT = "关于(&A)"

# Windows 任务栏 AppID（必须在 QApplication 创建前设置）
APP_USER_MODEL_ID = "taowu.spider.v12.1.2"

# 关于对话框
ABOUT_TITLE = "关于"
ABOUT_TEXT = "<b>{title}</b><br><br>智能反检测爬虫系统<br>PySide6/Qt GUI — baxiadb 设计风格<br>基于饕餮引擎 v12.1.2"

# 其他Qt UI 文本
LABEL_JSON_FILE_PATH = "JSON文件路径："
LABEL_BROWSE = "浏览"
LABEL_LOAD_JSON = "加载JSON"
LABEL_SELECT_ITEM = "选择条目："
LABEL_DELETE_CURRENT = "删除当前条目"
LABEL_BATCH_CHARS = "每批字符数："
LABEL_MANUAL_KEYWORDS = "手动关键词："
LABEL_DB_PATH = "数据库路径:"
LABEL_SELECT_DB = "选择DB文件"
LABEL_DATA_TABLE = "数据表:"
LABEL_EXPORT_CSV = "导出CSV"
LABEL_CSV_TRANSCODE_BTN = "CSV乱码转译"
LABEL_CLOSE = "关闭"
LABEL_DELETE_ROW = "删除本条"
LABEL_CLEAR_ALL_ROWS = "清空所有行"
LABEL_SAVE_TO_DB = "选中条目入库"
LABEL_ADD_ROW = "新增录入行"
LABEL_CURRENT_TABLE = "当前库表："
LABEL_REFRESH_TABLES = "刷新表"
LABEL_TABLE_ADD_EMPTY = "新增空行"
LABEL_TABLE_DEL_SELECTED = "删除选中"
LABEL_SUBMIT_ALL = "批量提交全部"
LABEL_CLOSE_WINDOW = "关闭窗口"
LABEL_CLEAR_FORM = "清空表单"
LABEL_AUTO_EXTRACT = "模型自动抽取"
LABEL_MANUAL_EXTRACT = "手动关键词提取"
LABEL_CONFIRM_TO_FORM = "确认填入表单"
LABEL_OPERATION_LOG = "操作日志："

# v12.1 启动横幅
STARTUP_BANNER_TITLE = "Taowu Spider v12.1.2 — 饕餮爬虫"
STARTUP_BANNER_SUBTITLE = "PySide6/Qt GUI — baxiadb (taowuDB) UI 风格"
STARTUP_BANNER_DESC = "智能反检测 | 巴蛇HTTP并发 | 浏览器增强 | 究极隐身"

# 分组框标题
GRP_DB_CONFIG = "数据库配置"

# 重复使用的日志模板
LOG_USER_TERMINATED = "爬虫任务已被用户终止"
LOG_COLLECT_COUNT = "共采集到 {count} 条记录"
LOG_EXTREME_MODE_CONFIG = "模式配置：究极防御 智能挑战=开 指纹轮换=开"
LOG_BATCH_MODE_DESC = "半自动采集模式：手动辅助 + 自动发现详情页 + 剃毛清洗"
LOG_TARGET_REACHED = "已达到目标采集条数 {count}，采集结束"
LOG_URL_RESULT_FOUND = "{url} → 发现 {count} 条数据"
LOG_URL_RESULT_COLLECTED = "{url} → 采集 {count} 条"
LOG_URL_RESULT_NONE = "{url} → 无数据"
LOG_URL_RESULT_ERROR = "{url} → 异常：{e}"
LOG_SINGLE_FETCH_EXCEPTION = "单条抓取异常：{e}"
LOG_CONFIG_APPLIED = "功能配置已应用"
LOG_ANTI_DETECT_APPLIED = "反检测等级：{level}"
LOG_LAYOUT_RESET = "布局已重置"
LOG_BACKUP_FAIL_NOFILE = "备份失败：数据库文件不存在"
LOG_IMAGE_DIR_FALLBACK = "图片目录：{dir}"
LOG_NO_LOGFILE = "日志文件不存在"
LOG_CANNOT_OPEN_LOGFILE = "无法打开日志文件：{e}"
LOG_URL_DEDUP_RESULT = "URL去重完成，共 {count} 条"

# JSON 导入对话框
TITLE_JSON_OPEN = "选择JSON文件"
FILE_FILTER_JSON = "JSON文件 (*.json);;所有文件 (*.*)"
LOG_JSON_READING = "正在读取JSON文件：{path}"
LOG_JSON_EMPTY = "JSON文件中无有效数据"
LOG_JSON_PARSED = "JSON解析完成，共 {count} 条记录"

# ManualInputDialog 弹窗标题
WIN_TITLE_MANUAL_INPUT = "单条文本抽取 | 自动+手动关键词提取"
WIN_SIZE_MANUAL_INPUT = (1500, 1000)

# ===================== 【视频流抓取 UI 文案】 =====================
WIN_TITLE_VIDEO_GRAB = "视频流抓取 | 网页视频下载"
WIN_SIZE_VIDEO_GRAB = (980, 680)
GRP_VIDEO_URL = "页面 URL（每行一个）"
LABEL_VIDEO_CANDIDATES = "解析到的视频候选："
LABEL_VIDEO_LOG = "操作日志："
BTN_VIDEO_START = "开始探测并下载"
BTN_VIDEO_STOP = "停止"
BTN_VIDEO_OPEN_DIR = "打开视频目录"
CHK_VIDEO_BROWSER = "静态解析失败时用浏览器探测(慢)"
CHK_VIDEO_SERIES = "整部抓取：粘贴详情页/任一分集页，自动识别全部分集并下载"
GRP_VIDEO_SERIES_OPT = "整部抓取选项"
LABEL_VIDEO_RANGE = "集数范围：从第"
LABEL_VIDEO_RANGE_TO = "集，到第"
LABEL_VIDEO_RANGE_END = "集（0 或留空 = 全部）"
LABEL_VIDEO_SERIES_TIP = "整部模式自动切换线路(源)，某集失败会用备用线路补齐；按 动漫名/第NN集 落盘，已下载自动跳过。"
LOG_VIDEO_START = "视频抓取任务开始，共 {n} 个 URL"
LOG_VIDEO_NO_CAND = "{url} → 未解析到视频(m3u8/直链)"
LOG_VIDEO_CAND = "[{i}] {kind:5s} | {url}"
LOG_VIDEO_OK = "{name} → ✓ {size} 已保存"
LOG_VIDEO_FAIL = "{name} → ✗ {err}"
LOG_VIDEO_STOP = "已停止（独立停止标志，不影响主爬虫）"
LOG_VIDEO_DONE = "全部完成：成功 {ok} / 失败 {fail} / 跳过 {skip}，元数据见 {json}"
LOG_VIDEO_ENC_NEED = "该流已加密(AES-128)，需 pip install pycryptodome 后重试"
LOG_VIDEO_OPEN_DIR = "视频目录：{dir}"

# ===================== 【本地视频播放器 UI 文案】 =====================
BTN_VIDEO_PLAYER = "本地视频播放"
WIN_TITLE_VIDEO_PLAYER = "本地视频播放器"
WIN_SIZE_VIDEO_PLAYER = (1220, 780)
BTN_PLAYER_REFRESH = "刷新列表"
BTN_PLAYER_OPEN_FILE = "打开本地文件…"
BTN_PLAYER_OPEN_DIR = "打开视频目录"
BTN_PLAYER_PREV = "上一集"
BTN_PLAYER_PLAY = "播放"
BTN_PLAYER_PAUSE = "暂停"
BTN_PLAYER_NEXT = "下一集"
LABEL_PLAYER_VOLUME = "音量"
CHK_PLAYER_AUTOPLAY = "自动连播"
STATUS_PLAYER_READY = "就绪：左侧单击选中剧集，再点「播放」开始"
STATUS_PLAYER_SEL = "已选中：{name}　点「播放」开始"
STATUS_PLAYER_PLAYING = "正在播放：{name}"
STATUS_PLAYER_PAUSED = "已暂停：{name}"
STATUS_PLAYER_END = "播放结束：{name}"
MSG_PLAYER_SEL_NONE = "还没选中任何剧集：请先在左侧单击点选要播的一集"
TREE_PLAYER_NAME = "剧集"
TREE_PLAYER_SIZE = "大小"
TREE_PLAYER_LOOSE = "(根目录单集)"
# ── 播放器可播格式清单（2026-09-10 增）──────────────────────────────────────
# **单一真源**：video_player.MEDIA_EXTS / AUDIO_MEDIA_EXTS 都由这里派生，
# 禁止在别处再写第二份扩展名字面量——历史上 FILE_FILTER_VIDEO 就是一份独立字面量，
# 结果和 video_player.MEDIA_EXTS 分叉漏掉了 .wmv（选了 .wmv 文件从"所有文件"能进来，
# 但过滤器里看不见它）。video_player 有一条 selftest-formats 断言专门防它再分叉。
# 顺序即播放器「格式」下拉框的顺序，**第 0 项是默认选中项**。
VIDEO_EXTS = (".mp4", ".ts", ".mkv", ".flv", ".webm", ".mov", ".m4v", ".avi", ".wmv")
# 音频**不另立清单**：直接复用 audio_grab 的真实产物清单 AUDIO_OUT_EXTS（该常量无点，
# 在本文件下方定义），避免出现第三份格式清单；播放器侧补点后使用（video_player._with_dots）。
LABEL_PLAYER_FORMAT = "格式"          # 下拉框前的小标签
FMT_KIND_VIDEO = "视频"                # 下拉项文案里的类别前缀
FMT_KIND_AUDIO = "音频"
ITEM_PLAYER_FMT_ONE = "{kind}：{ext}"  # 下拉项文案（每个具体格式一项，不做"全部"聚合项）
STATUS_PLAYER_FMT = "当前格式：{fmt}"
MSG_PLAYER_FMT_EMPTY = "{fmt}：没有匹配的媒体文件\n目录：{dir}"
MSG_PLAYER_FMT_MISMATCH = "该文件不是支持的音视频格式：{name}"
MSG_PLAYER_FILE_GONE = "文件已不存在（可能已被移动或删除）：{name}"
FILE_FILTER_MEDIA_TMPL = "媒体文件 ({exts})"
FILE_FILTER_ALL = "所有文件 (*.*)"
MSG_PLAYER_NO_DLL = ("找不到播放内核 {name}。\n\n"
                     "路径：{dir}\n原因：{reason}\n\n"
                     "请把 LGPL 版 {name}（连同同层依赖 dll）放进该目录后重试；"
                     "否则可继续用「视频流抓取」只下载、暂不播放。")
# 空库提示：判据是"这个库里连任何受支持的音视频格式都没有"（不是"当前格式没匹配到"）。
# 按格式过滤后两者不再等价——库里只有 .ts 而当前格式是 .mp4 时不该弹"还没下载"。
MSG_PLAYER_NO_MEDIA = ("这个库里还没有已下载的媒体：{dir}\n\n"
                       "先用「视频流抓取」或「抓取音频流」下载几个，再回来播放。")
MSG_PLAYER_LOAD_FAIL = "无法播放该文件：{err}"
MSG_PLAYER_EOF_LAST = "已是最后一集"
MSG_PLAYER_OPEN_DIR = "已打开视频目录：{dir}"

# ===================== 【抓取音频流 运行参数】 =====================
# 纯后端 audio_grab.py 使用：直链/音频 m3u8/mpd/页面解析。只播/存 output/audio 已下载库。
AUDIO_DIR = os.path.join(OUTPUT_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
# 分段 HLS/DASH 影子续传目录（与 AUDIO_DIR 同卷，保证 os.replace 原子性）
AUDIO_PARTIAL_DIR = os.path.join(os.path.dirname(AUDIO_DIR), "_audio_partials")
os.makedirs(AUDIO_PARTIAL_DIR, exist_ok=True)
AUDIO_PARTIAL_MAX_AGE = 7 * 86400     # 超龄影子目录清理阈值（7 天）
AUDIO_TIMEOUT = 20
AUDIO_CONCURRENT = 6
AUDIO_MAX_PAGE_BYTES = 2 * 1024 * 1024
AUDIO_MAX_SEGMENTS = 3000
AUDIO_MIN_SEG_BYTES = 256             # 单分片最小字节门槛（_audio_fetch_seg 生效）
AUDIO_MIN_MEDIA_BYTES = 512           # 直链落盘文件魔数探测的最小长度门槛
AUDIO_SERIES_PARALLEL = 1     # 整部串行：单 IP 礼貌有界
AUDIO_RESOLVE_MAX_HOPS = 2    # 页内无音频时静态跟随 iframe/refresh 跳转深度上限
AUDIO_EXT_DIRECT = ("mp3", "m4a", "aac", "wav", "flac", "ogg")
AUDIO_OUT_EXTS = AUDIO_EXT_DIRECT
AUDIO_RE_M3U8 = r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.m3u8[^"\']*?)["\']'
AUDIO_RE_MPD = r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.mpd[^"\']*?)["\']'
AUDIO_RE_DIRECT = r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.(?:mp3|m4a|aac|wav|flac|ogg)[^"\']*?)["\']'
# 内容元数据：ID3/封面 best-effort 写入总开关；封面文件抓取大小上限
AUDIO_TAG_ENABLED = True
AUDIO_COVER_MAX_BYTES = 2 * 1024 * 1024
# DASH D1 ClearKey：站侧 key 供给缝（MPD ContentProtection 命中 ClearKey 时用）。
# 值 = {host 后缀: 内容密钥 hex(16字节=32 hex)}。key 由站侧许可/JS 现算得到后人工填入；
# 空默认=永不触发（清 key MPD 若未配置 → 显式报 enc 错而非静默下成密文）。
AUDIO_DASH_CLEARKEY = {}

# ===================== 【通用媒体源解析层 media_resolve 运行参数】 =====================
# 任意 URL → 自动判类 → 发现媒体(平台配方 site_recipes/* 优先 → 静态深挖 → 浏览器兜底) → 勾选下载。
# 默认"一键自动链"：浏览器只用来找真实媒体地址 + 收割登录 cookie，命中即关页(几秒)，下载走既有健壮栈；
# 不做全程播放/收流(会话捕获=死路，已回滚)。真 DRM(Widevine/PlayReady/FairPlay) 恒只识别跳过，绝不解密。
MEDIA_RESOLVE_AUTO_BROWSER = True    # 配方未命中且静态深挖无候选时，是否自动启浏览器发现兜底
MEDIA_RESOLVE_NET_CAPTURE = True     # 浏览器发现时是否注入 fetch/XHR URL 捕获打点(资源级收真实流地址)
MEDIA_TRACK_PROBE = False            # 对清单/无扩展名候选做内容嗅探(Content-Type/时长)；默认关(留 M3 probe_track)

# ---- 通用「页面 API 旁观」捕获缝（media_capture.py）----
# 现状：大多数音视频站是"前端播放器 + 后端 API"，页面上根本没有直链。此缝用 CDP 旁观**页面自己
# 发出的**请求与响应体，把藏在 API JSON 里的媒体地址读回来（worker 内请求也覆盖）。只读页面自己的
# 流量，绝不代它构造请求；不生成任何签名；商业 DRM 只识别跳过。
MEDIA_CAPTURE_WAIT_SEC = 20.0          # 单页旁观硬上限(秒)；命中后静默期到即提前收工，典型 3-7s
MEDIA_CAPTURE_IDLE_GRACE = 2.5         # 命中后多久没有新候选即收工(秒)
MEDIA_CAPTURE_BODY_MAX_BYTES = 1048576  # 单响应体读取上限(1MiB)；超过不读(媒体本体一律不读)
MEDIA_CAPTURE_MAX_BODIES = 40          # 响应体读取总数上限
MEDIA_CAPTURE_MAX_TRACKS = 60          # 单页候选上限
MEDIA_CAPTURE_MAX_MANIFESTS = 8        # 同类清单(m3u8/dash)保留条数
MEDIA_CAPTURE_ALLOW_CHROME_LAUNCH = True   # 9222 没活时是否允许拉起共享 Chrome。
#   ⚠ 直连模式下拉起会先 taskkill chrome.exe —— 即关掉用户自己开着的 Chrome 窗口。置 False 则只复用不拉起。

# ===================== 【通用媒体下载 media_resolve UI 文案】 =====================
# 任意 URL → 自动判类 → 发现媒体(配方→静态→浏览器兜底) → 勾选下载。浏览器只找地址+收 cookie。
WIN_TITLE_MEDIA_GRAB = "通用媒体下载 | 任意网站音频 / 视频"
WIN_SIZE_MEDIA_GRAB = (1000, 700)
GRP_MEDIA_URL = "URL（每行一个：视频/音频网页、播放页、直链、iframe 播放页 均可，自动判类解析）"
LABEL_MEDIA_CANDIDATES = "解析到的媒体候选："
LABEL_MEDIA_LOG = "操作日志："
BTN_MEDIA_RESOLVE = "自动解析"
BTN_MEDIA_DL_SEL = "下载选中"
BTN_MEDIA_DL_ALL = "下载全部"
BTN_MEDIA_STOP = "停止"
BTN_MEDIA_OPEN = "打开媒体目录"
CHK_MEDIA_BROWSER = "浏览器兜底发现（静态解析不到时，自动开 Chrome 找真实媒体地址并收登录 cookie）"
CHK_MEDIA_DRM_SKIP = "命中商业 DRM(Widevine/PlayReady/FairPlay) 自动跳过（恒开，绝不解密）"
LOG_MEDIA_STOP = "已请求停止，等待当前项结束…"
LOG_MEDIA_OPEN_DIR = "已打开媒体输出目录"
LABEL_MEDIA_WANT = "目标："
RADIO_MEDIA_WANT = (("both", "音视频都要"), ("audio", "音频优先"), ("video", "视频优先"))
BADGE_MEDIA_EPHEMERAL = "时效"
TIP_MEDIA_EPHEMERAL = "现场时效直链（URL 带时效 token）：离开页面即可能失效，请尽快下载"
# 配方库入口。2026-09-11 起它打开的是**统一配置管理组件**（媒体配方只是其中一页），
# 不再是"打开配方目录 + 汇报"那一个动作。
BTN_MEDIA_RECIPE = "媒体配方"
TIP_MEDIA_RECIPE = ("打开「配置管理」—— 媒体配方的导入 / 启用停用 / 查看 / 打开目录都在那儿\n"
                    "（那里同时管数据表模板与字段规则片段，是全部配置的唯一管理入口）。\n"
                    "音视频站点的抓取策略是 媒体配方/*.json（自带，随升级覆盖）；\n"
                    "您自己写的配方放 用户配方/（升级不动它），在配置管理里「导入」。\n"
                    "字段手册见 媒体配方/说明.md；_模板.json 是抄写用骨架。\n"
                    "文件名以 _ 开头的不会被当成配方。")
# 下面这组「配方行」常量里，SEL/上传的标签仍被 qt_recipe_row 用；TOGGLE/IMPORT 两颗按钮
# 已随管理面搬进配置管理组件，仅留作文案备份（配置管理用的是 BTN_CFG_* 那一组）。
LOG_MEDIA_RECIPE_OK = "媒体配方目录：%s（已生效 %d 个：%s）"
LOG_MEDIA_RECIPE_ERR = "有 %d 个配方文件未通过校验，未生效：%s"
LABEL_MEDIA_RECIPE_SEL = "配方："
ITEM_MEDIA_RECIPE_AUTO = "自动（按网址匹配，默认）"
TAG_MEDIA_RECIPE_BUILTIN = "[自带]"
TAG_MEDIA_RECIPE_USER = "[用户]"
MARK_MEDIA_RECIPE_DISABLED = "（已停用）"
MARK_MEDIA_RECIPE_INVALID = "（校验未通过）"
TIP_MEDIA_RECIPE_SEL = ("自动＝按网址域名挑配方（默认）。选了具体配方＝只用它解析，"
                        "解析不出也不退回静态/浏览器兜底，便于确认配方本身是否生效。\n"
                        "导入配方 / 启用停用 / 查看内容：按「配置管理」。")
TIP_MEDIA_RECIPE_TOGGLE = "启用 / 停用当前选中的配方（对自带配方同样有效，且升级后仍记得）"
TIP_MEDIA_RECIPE_IMPORT = ("导入一份配方 json（导入前先校验，写错了会被拒绝并列出原因）。\n"
                           "导入到 用户配方/；同 host_suffix 时用户配方优先于自带配方。")
BTN_MEDIA_RECIPE_TOGGLE_ON = "启用"
BTN_MEDIA_RECIPE_TOGGLE_OFF = "停用"
BTN_MEDIA_RECIPE_IMPORT = "导入配方"
LOG_MEDIA_RECIPE_ENABLED = "已启用配方 %s（当前生效 %d 个）"
LOG_MEDIA_RECIPE_DISABLED = "已停用配方 %s（当前生效 %d 个）"
LOG_MEDIA_RECIPE_STATE_FAIL = "配方启停状态写入失败：%s"
LOG_MEDIA_RECIPE_FORCED = "已指定配方 %s：只走这一份，解析不出也不做静态/浏览器兜底"
LOG_MEDIA_RECIPE_FORCE_SKIPPED = "所选配方 %s 当前不可用（已停用或校验未通过），本次按「自动」解析"
LOG_MEDIA_RECIPE_IMPORT_OK = "已导入配方：%s（当前生效 %d 个）"
LOG_MEDIA_RECIPE_IMPORT_FAIL = "导入失败：%s"
TITLE_MEDIA_RECIPE_IMPORT = "选择要导入的配方 JSON"
MSG_MEDIA_RECIPE_OVERWRITE = "用户配方目录里已有 %s，要覆盖它吗？"
MSG_MEDIA_RECIPE_OVERRIDE_WARN = ("注意：这份配方的 host_suffix 与自带配方 %s 相同，"
                                  "导入后将优先使用您导入的这份。")
MSG_MEDIA_RECIPE_BAD = "这份配方没有通过校验，未导入：\n\n%s"
# 主窗白钮（供 main_qt 白钮网格注册）
BTN_MEDIA_GRAB = "通用媒体下载"

# ===================== 【抓取音频流 UI 文案】 =====================
BTN_AUDIO_GRAB = "抓取音频流"
WIN_TITLE_AUDIO_GRAB = "抓取音频流 | 音频下载"
WIN_SIZE_AUDIO_GRAB = (980, 660)
GRP_AUDIO_URL = "音频页 / 直链 URL（每行一个）"
LABEL_AUDIO_CANDIDATES = "解析到的音频候选："
LABEL_AUDIO_LOG = "操作日志："
BTN_AUDIO_START = "开始探测并下载"
BTN_AUDIO_STOP = "停止"
BTN_AUDIO_OPEN_DIR = "打开音频目录"
CHK_AUDIO_SERIES = "整部抓取：粘贴详情页/任一分集页，自动识别全部分集并下载"
GRP_AUDIO_SERIES_OPT = "整部抓取选项"
LABEL_AUDIO_RANGE = "集数范围：从第"
LABEL_AUDIO_RANGE_TO = "集，到第"
LABEL_AUDIO_RANGE_END = "集（0 或留空 = 全部）"
LABEL_AUDIO_SERIES_TIP = "整部模式自动切换线路(源)，某集失败会用备用线路补齐；按 专辑名/第NN集 落盘，已下载自动跳过。"
LOG_AUDIO_START = "音频抓取任务开始，共 {n} 个 URL"
LOG_AUDIO_NO_CAND = "{url} → 未解析到音频(m3u8/mpd/直链)"
LOG_AUDIO_CAND = "[{i}] {kind:5s} | {url}"
LOG_AUDIO_OK = "{name} → ✓ {size} 已保存"
LOG_AUDIO_FAIL = "{name} → ✗ {err}"
LOG_AUDIO_STOP = "已停止（独立停止标志，不影响主爬虫）"
LOG_AUDIO_DONE = "全部完成：成功 {ok} / 失败 {fail} / 跳过 {skip}，元数据见 {json}"
LOG_AUDIO_ENC_NEED = "该流加密方式暂不支持（音频 AES-128 由随包 ffmpeg 解密，非 AES-128/明文需人工处理）"
LOG_AUDIO_OPEN_DIR = "音频目录：{dir}"
# 浏览器旁观兜底（仅当常规音频解析 0 候选且勾选时启用；走 media_resolve 通用链，独立停止标志）
CHK_AUDIO_PAGE_WATCH = "浏览器旁观兜底（常规解析 0 候选时，开 Chrome 旁观页面自己的 API 取音频地址）"
LOG_AUDIO_WATCH_TRY = "{url} → 常规解析 0 候选，改走浏览器旁观兜底…"
LOG_AUDIO_WATCH_HIT = "{url} → 旁观命中 {n} 条音频候选"
LOG_AUDIO_WATCH_MISS = "{url} → 旁观亦未命中（详见解析注记）"
# ---- 配方（音频侧）：语义与通用媒体下载弹窗**刻意不同** ----
# 那边选中＝强制不降级（用于验证"我写的配方通不通"）；这边是批量任务，配方作为**补充线** ——
# 常规线 0 候选的页面才拿它再试一次，命中就并入结果，没命中不影响常规线已经拿到的。
# 理由：一批粘 10 个 URL，不该因为其中几个配方没命中就整体空手。
LABEL_AUDIO_RECIPE_SEL = "配方（补充线）："
TIP_AUDIO_RECIPE_SEL = ("自动（默认）＝常规音频解析为主。\n"
                        "选了具体配方＝常规解析 0 候选的页面，再拿这份配方试一次；\n"
                        "命中就并入结果，没命中不影响常规解析已经拿到的那些。")
LOG_AUDIO_RECIPE_TRY = "{url} → 常规解析 0 候选，改用配方 {key} 再试一次…"
LOG_AUDIO_RECIPE_HIT = "{url} → 配方 {key} 命中 {n} 条音频"
LOG_AUDIO_RECIPE_MISS = "{url} → 配方 {key} 未命中（详见解析注记）"
LOG_AUDIO_RECIPE_SERIES_SKIP = "整部分集模式不用配方补充线（分集有自己的线路切换语义）"
LOG_AUDIO_RECIPE_SEAM_HINT = ("注意：配方 {key} 带「浏览器辅助缝」，而「浏览器旁观」没勾选 —— "
                              "本轮只走它声明的接口，不做旁观（要启用请把那个复选框勾上）")

# ============ 【配置管理组件】2026-09-11 新增 ============
# 统一管理**全部用户可导入的配置**（媒体配方 / 数据表模板 / 字段规则片段）的启用停用、导入、查看。
# 为什么要有它：这三类配置各自长了一套"下拉框 + 停用 + 导入 + 目录"按钮 —— 主窗「抓取规则：」那行
# 三个、音视频弹窗的配方行三个，而且互相之间的规则（来源限定键、停用=不注册、先校验后复制）完全一样。
# 每加一类配置就要在界面上再复制一套，按钮越堆越乱、坑也各踩一遍。收敛成一处之后，
# 新增配置类型只需往 FAMILIES 里加一项。
BTN_CFG_MGR = "配置管理"
TIP_CFG_MGR = ("一个地方管全部配置：媒体配方 / 数据表模板 / 字段规则片段。\n"
               "每类都分「自带」（随软件来、升级会覆盖）与「用户」（你导入的、升级绝不动）。\n"
               "在这里可以：导入 / 启用·停用 / 查看内容 / 打开目录。\n"
               "停用＝引擎不读它（文件还在，随时能再启用）；同名的用户那份会覆盖自带那份。")
TITLE_CFG_MGR = "配置管理"
TITLE_CFG_VIEW = "查看配置内容"
TAB_CFG_MEDIA = "媒体配方"
# 「数据表模板」与「字段配置」是**同一批文件**的两种叫法：一份 JSON 既能建表（columns[].type）、
# 又带字段匹配（synonyms/re/rule）。所以不拆成两页 —— 拆了会让用户在两个页签里看到同一份文件。
TAB_CFG_TABLE = "数据表模板·字段配置"
TAB_CFG_RULES = "字段规则片段"
COL_CFG_NAME = "名称"
COL_CFG_SOURCE = "来源"
COL_CFG_STATE = "状态"
COL_CFG_USE = "用途"
COL_CFG_FILE = "文件"
USE_CFG_TABLE = "建表 + 字段匹配"
USE_CFG_MATCH = "仅字段匹配"
SRC_CFG_BUILTIN, SRC_CFG_USER = "自带", "用户"
STATE_CFG_ON, STATE_CFG_OFF, STATE_CFG_BAD = "生效", "已停用", "写坏了"
MARK_CFG_DISABLED, MARK_CFG_INVALID = "（已停用）", "（写坏了）"
BTN_CFG_IMPORT = "导入…"
BTN_CFG_TOGGLE_ON, BTN_CFG_TOGGLE_OFF = "启用", "停用"
BTN_CFG_VIEW = "查看内容"
BTN_CFG_OPEN_DIR = "打开目录"
BTN_CFG_REFRESH = "重新扫描"
BTN_CFG_CLOSE = "关闭"
BTN_CFG_OPEN_FILE = "用系统程序打开"
TIP_CFG_TOGGLE = ("停用＝引擎不读它（文件还在原地，随时能再启用）。\n"
                  "自带与用户的那一份**各算各的**：停用你同名的那份，自带的会立刻回来。")
TIP_CFG_IMPORT = ("选一份 .json 导进你自己的目录（升级不会覆盖）。\n"
                  "导入前会先校验：不合格的当场拒绝并指出错在哪一行，不会落进一个永不生效的坏文件。")
TIP_CFG_VIEW = "看这份配置的原文（只读）。写坏了的会因为语法错误看不了，这时按提示改文件。"
TIP_CFG_OPEN_DIR = "打开你自己的配置目录（自带目录只读，升级会覆盖，别往里写）。"
TIP_CFG_REFRESH = "手动改过磁盘上的文件之后点这里重新扫描（改 json 不必重启软件）。"
HINT_CFG_PICK = "选中一行看详情；双击＝查看内容。"
HINT_CFG_EMPTY = "这一类现在一份都没有。"
HINT_CFG_BUILTIN_RO = ("自带配置不能改也不能删：升级时这一整个目录会被覆盖。要改成你自己的，"
                       "复制一份出来改好再「导入…」。")
HINT_CFG_SHARED_STATE = ("「数据表模板·字段配置」与主界面「抓取规则：」是**同一份文件**，"
                         "在这里停用它，那边也会一起标成已停用 —— 一个文件只有一个开关。")
LOG_CFG_LOADED = "配置管理：%s —— 自带 %d 份 / 用户 %d 份，其中写坏 %d 份、已停用 %d 份"
LOG_CFG_TOGGLED = "配置管理：%s %s"
LOG_CFG_IMPORTED = "配置管理：已导入 %s（%s）"
LOG_CFG_IMPORT_FAIL = "配置管理：导入失败 —— %s"
LOG_CFG_OPEN_DIR_FAIL = "配置管理：打开目录失败 —— %s"
LOG_CFG_RELOADED = "配置管理：重新扫描完毕"
MSG_CFG_IMPORT_TITLE = "导入配置"
MSG_CFG_BAD = "这份配置没通过校验，不能导入：\n\n%s"
MSG_CFG_EXISTS = "用户目录里已经有同名文件：\n%s\n\n要覆盖它吗？"
MSG_CFG_OVERRIDE = "这份配置会和自带的「%s」重叠。\n导入后会由你这份接管（自带的仍在，停用你这份即可退回）。\n\n继续导入吗？"
MSG_CFG_NO_SELECT = "先在上面选中一行。"
MSG_CFG_READ_FAIL = "这份文件读不出来：\n%s\n\n多半是 JSON 语法写坏了，用「打开目录」找到它改一改。"
MSG_CFG_TOGGLE_FAIL = "改状态失败：%s"
MSG_CFG_OWNER_MISMATCH = ("这份文件已经不在配置目录里了（可能刚被删/改名）。"
                          "点「重新扫描」刷新一下列表。")

# ===================== 【网站全方位诊断】2026-09-11 新增 =====================
# 粘一个 URL → 告诉你这个站能不能抓、有什么坑、目标站自己声明了什么规矩。
# 后端 site_diag.py（纯函数 + 探针），界面 qt_site_diag.py。
#
# 为什么要有它：这几年每一个站都是**靠失败发现的** —— 网易云 fee 档位看错、
# 酷我 PAY 是位掩码不是布尔、酷狗证书真配错、猫耳整站 DRM、酷狗限速伪装成付费档……
# 撞一次修一次，教训只留在 开发报告/ 里，下回遇到同类型的站还是从头踩。
#
# 两条底线（与全项目一致，别在界面文案上含糊）：
#   1. 诊断**只观测、不绕过**：DRM 只识别不解密；签名/验证码不代解；付费内容不做。
#   2. robots.txt 抓下来是让你**知情**，不是替你决定 —— 命中禁止名单只标红写明，
#      默认**不自动跳过**（要改这个行为是 robots_policy 的事，不是悄悄改的）。
BTN_SITE_DIAG = "网站诊断"
TIP_SITE_DIAG = ("粘一个网址，全方位体检这个站：能不能抓、有什么坑、底线在哪。\n"
                 "报告包含：可达性(状态码/超时/DNS) · 前端指纹(框架/SSR/SPA) ·\n"
                 "运行期请求(真实 XHR/fetch) · 媒体与 DRM · 难度档位 · 建议。\n"
                 "会抓目标站的 robots.txt 并把它的规矩摊开给你看（命中禁止会标红）。\n"
                 "诊断只观测不绕过：DRM 只识别不解密，验证码不代过，付费内容不碰。")
TITLE_SITE_DIAG = "网站全方位诊断"
WIN_SIZE_SITE_DIAG = (1040, 740)
GRP_DIAG_URL = ("URL（一行一个。单页：页级结论只对这一个 URL 负责；"
                "整站：把 URL 空间铺开再判）")
LABEL_DIAG_SCOPE = "诊断范围："
LABEL_DIAG_PROFILE = "档位："
SCOPE_DIAG_PAGE = "单页（快，只看这一个 URL）"
SCOPE_DIAG_SITE = "整站（铺开 URL 全集 → 结构地图 → 按模板深诊）"
# 档位只影响**整站**模式。键必须与 site_diag.SITE_PROFILES 一致 ——
# 对不上会被 site_profile() 静默回落到 standard（那正是"看着选了极致实际跑的是标准"）。
DIAG_PROFILE_CHOICES = (
    ("shallow", "浅 — 每类页面看一眼，不翻页（约 2 分钟）"),
    ("standard", "标准深诊 — 结构铺全 + 每类 2 页 + 翻 3 页（约 8 分钟，默认）"),
    ("deep", "极致 — 每类 5 页 + 翻 10 页（约 30 分钟+）"),
    ("all", "全部 — 结构地图里每条 URL 都走一遍（可能几小时，随时可停）"),
)
# 整站模式的**页间节拍**（秒）。这是礼貌，不是性能旋钮：
# 恒串行 + 节拍是「深度用时间换，绝不靠并发换」的落点。
DIAG_SITE_DELAY = 2.0
LABEL_DIAG_REPORT = "诊断报告："
LABEL_DIAG_LOG = "操作日志："
BTN_DIAG_RUN = "开始诊断"
BTN_DIAG_STOP = "停止"
BTN_DIAG_COPY = "复制报告"
BTN_DIAG_SAVE = "另存报告…"
BTN_DIAG_CLOSE = "关闭"
CHK_DIAG_BROWSER = ("浏览器观测（开真机 Chrome 看这个页面运行期到底请求了什么；"
                    "关掉则只做静态+状态码级诊断，快很多）")
# 有界观测：默认轮询轮数与等待秒数。观测是**有时间上限**的，不然遇到长轮询页面会挂着不走。
DIAG_WAIT_DEFAULT = 15
DIAG_ROUNDS = 0                     # 0 = 只看时钟（跑满 wait_sec），非 0 = 轮数上限
DIAG_USE_BROWSER = True
DIAG_MAX_BYTES = 4 * 1024 * 1024    # 页面正文上限（超了截断，别把大文件拖进内存）

# 跨会话诊断历史（P2）。留空 = 用 BASE_DIR/runtime/site_diag_history.json。
# **跟着 base_path 走**是刻意的：测试/冒烟脚本把 base_path 指到沙箱，历史就一起被指走，
# 不会往开发库里攒记录（09-11 那轮"只指 DB_SAVE_DIR 导致冒烟污染开发库"的坑）。
# 内容只存结论码与计数，**不存 URL**；条数上限每 host 10 / 全局 500；永不参与判级。
SITE_DIAG_HISTORY_PATH = ""

WHY_DIAG_ROBOTS = ("抓 robots.txt 是为了让你知道这个站自己声明的规矩是什么，"
                   "好判断这次抓取有没有越界。诊断**不会**替你跳过它禁止的路径。")
LOG_DIAG_START = "开始诊断：%s（范围：%s，浏览器观测：%s，最多等 %d 秒）"
LOG_DIAG_SITE_START = "开始整站诊断：%s（档位：%s）"
LOG_DIAG_STOP = "已请求停止，等待当前步骤结束…"
LOG_DIAG_DONE = "诊断结束：%s"
LOG_DIAG_COPIED = "报告已复制到剪贴板（%d 字符）"
LOG_DIAG_SAVED = "报告已保存：%s"
LOG_DIAG_SAVE_FAIL = "保存失败：%s"
LOG_DIAG_OPEN_DIR_FAIL = "打开目录失败：%s"
MSG_DIAG_NO_URL = "先填一个网址。"
MSG_DIAG_BAD_URL = "只认 http:// 或 https:// 开头的完整网址。"
MSG_DIAG_RUNNING = "正在诊断中，先等这一轮跑完（或点「停止」）。"
MSG_DIAG_SAVE_TITLE = "保存诊断报告"
MSG_DIAG_SAVE_FILTER = "文本文件 (*.txt);;全部文件 (*.*)"
HINT_DIAG_EMPTY = ("粘一个网址，点「开始诊断」。\n\n"
                   "【单页】只对你粘的那一个 URL 负责：抓 robots.txt（摊开目标站声明的规矩）\n"
                   "→ 状态码级探测（区分 403/404/200，这一点老路径会吞掉）→ 静态前端指纹\n"
                   "→ 媒体路由判定 → 可选的真机运行期观测 → 汇总成一份报告。\n\n"
                   "【整站】把 URL 空间铺开再判：robots 的 Sitemap 行递归展开 + 首页链接\n"
                   "→ 归约成模板（结构地图）→ 按模板抽代表页深诊 → 列表页试翻页。\n"
                   "**恒串行、页间有节拍、只发 GET、遇 429 立刻停** —— 深度用时间换，不靠并发换。\n"
                   "档位只对整站有效；「全部」档可能跑几小时，随时可停。")
DIAG_REPORT_HEADER = ("本报告由「网站诊断」生成。诊断只观测、不绕过；"
                      "结论基于本次探测到的那一次响应与那一个 URL，"
                      "站点改版后需重跑。")

# ===================== 【全局通用工具函数】 =====================
def has_any_value(item: dict) -> bool:
    """空值过滤：任意字段有内容则判定有效"""
    for key in DB_FIELDS:
        val = str(item.get(key, "")).strip()
        if val:
            return True
    return False

def dig_all_qianlong(data, data_list: list):
    """递归提取 qianlong_extract_result"""
    if isinstance(data, dict):
        if "qianlong_extract_result" in data and isinstance(data["qianlong_extract_result"], dict):
            data_list.append(data["qianlong_extract_result"])
        for k in ["error_data", "normal_data"]:
            if k in data and isinstance(data[k], list):
                for sub in data[k]:
                    dig_all_qianlong(sub, data_list)
    elif isinstance(data, list):
        for sub in data:
            dig_all_qianlong(sub, data_list)

def get_all_valid_data(root_data, log_func):
    """通用JSON解析函数（兼容 error_data / normal_data / 列表/字典）"""
    data_list = []
    if isinstance(root_data, dict):
        if "error_data" in root_data and isinstance(root_data["error_data"], list):
            log_func("  读取 error_data 列表数据")
            for item in root_data["error_data"]:
                if isinstance(item, dict):
                    if "qianlong_extract_result" in item:
                        data_list.append(item["qianlong_extract_result"])
                    else:
                        data_list.append(item)
        if "normal_data" in root_data and isinstance(root_data["normal_data"], list):
            log_func("  读取 normal_data 列表数据")
            data_list.extend(root_data["normal_data"])
        if "qianlong_extract_result" in root_data:
            log_func("  读取 qianlong_extract_result 单条数据")
            single_item = root_data["qianlong_extract_result"]
            if isinstance(single_item, dict):
                data_list.append(single_item)
        if not data_list:
            data_list.append(root_data)
    elif isinstance(root_data, list):
        log_func("  直接解析外层列表")
        data_list = root_data
    return data_list

def get_random_sleep(min_sec: float = None, max_sec: float = None) -> float:
    return random.uniform(min_sec, max_sec) if min_sec and max_sec else random.uniform(MIN_SLEEP, MAX_SLEEP)

def get_random_proxy() -> Optional[Dict]:
    return random.choice(PROXY_LIST) if USE_PROXY and PROXY_LIST else None

def get_random_ua() -> str:
    return random.choice(UA_POOL) if USE_RANDOM_UA else UA_POOL[0]

def get_random_headers() -> Dict:
    return random.choice(BASE_HEADERS_LIST)

def get_scroll_config() -> Tuple[int, int]:
    return (random.randint(SCROLL_TIMES_MIN, SCROLL_TIMES_MAX), random.randint(SCROLL_STEP_MIN, SCROLL_STEP_MAX))

def get_scroll_sleep():
    time.sleep(get_random_sleep(SCROLL_SLEEP_MIN, SCROLL_SLEEP_MAX))

def sleep_page_pre():
    time.sleep(get_random_sleep(PAGE_PRE_SLEEP_MIN, PAGE_PRE_SLEEP_MAX))

def sleep_stay():
    time.sleep(get_random_sleep(STAY_SLEEP_MIN, STAY_SLEEP_MAX))

def get_slow_mo() -> int:
    return random.randint(PLAYWRIGHT_SLOW_MO_MIN, PLAYWRIGHT_SLOW_MO_MAX)

# ===================== 【新增：指数退避重试】 =====================
def exponential_backoff(attempt: int, base_sec: float = 3.0, max_sec: float = 120.0) -> float:
    """指数退避：第1次~3s，第2次~6s，第3次~12s，上限120s，带±20%随机抖动"""
    sleep_val = min(base_sec * (2 ** attempt), max_sec)
    jitter = sleep_val * random.uniform(0.8, 1.2)
    return jitter

def load_selector_lib() -> Dict:
    if not os.path.exists(SELECTOR_LIB_PATH):
        return {}
    try:
        with open(SELECTOR_LIB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_full_finger_js() -> str:
    if not HIDE_WEBDRIVER:
        return ""
    return """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {};
"""

def full2half(s: str) -> str:
    return "".join(chr(ord(c)-0xfee0) if 0xff01 <= ord(c) <= 0xff5e else c for c in s)

def rebuild_number(text: str) -> str:
    text = re.sub(r'\((\d+)\)', r'\1', text)
    text = re.sub(r'(\d)\s+(\d)', r'\1\2', text, flags=0)
    text = re.sub(r'(\d+)\s+(\d+)', r'\1\2', text, flags=0)
    return text

def deep_extract_content(raw_str: str) -> str:
    text = raw_str
    for _ in range(10):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "content" in data:
                text = data["content"]
            else:
                break
        except (json.JSONDecodeError, TypeError):
            break
    return text.strip()

def full_noise_filter(text: str) -> str:
    if not text:
        return ""
    for lst in [JS_TAGS, LICENSE_NOISE, LOGIN_NOISE, AD_NOISE, WARN_NOISE, COMPANY_EXTRA]:
        for w in lst:
            text = text.replace(w, "")
    text = re.sub(r'[\\\"\'"<>{}[\]]', "", text)
    text = re.sub(r"[\t\r\n]", " ", text)
    text = re.sub(r"[^\u4e00-\u9fff0-9，。、：；？！()（） ]", "", text)
    return re.sub(r"\s+", " ", text).strip()

def semantic_split(text: str) -> list:
    if len(text) <= BATCH_MAX_LEN:
        return [text]
    sentences = [s.strip() for s in re.split(r"[。；]", text) if s.strip()]
    chunks, current = [], ""
    for sent in sentences:
        if len(current + sent) > BATCH_MAX_LEN and current:
            chunks.append(current)
            current = sent
        else:
            current += sent + "。"
    if current:
        chunks.append(current)
    return chunks

def set_selenium_env():
    os.environ["SE_AVOID_DOWNLOAD"] = "1"
    os.environ["WDM_LOCAL"] = "1"
    os.environ["SELENIUM_MANAGER_DISABLE"] = "1"

def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).lower()


# =======================================================
# 【CSV乱码转译 — 后端编码探测 & 读写】
# =======================================================

def detect_csv_encoding(filepath: str) -> Tuple[str, float]:
    """探测CSV文件的最可能编码。
    遍历常见中文编码列表，以 strict 模式尝试解码，对成功者按
    中文字符占比评分，返回 (编码名, 得分)。
    得分 > 0.05 为"高"置信度，> 0.01 为"中"，否则为"低"。
    """
    best_enc = ""
    best_score = -1.0

    for enc in CSV_ENCODING_CANDIDATES:
        try:
            with open(filepath, "r", encoding=enc, errors="strict") as f:
                text = f.read(CSV_DETECT_READ_BYTES)
        except (UnicodeDecodeError, UnicodeError, LookupError):
            continue

        total = len(text) or 1
        chinese = sum(1 for c in text if "一" <= c <= "鿿")
        japanese = sum(1 for c in text if "぀" <= c <= "ヿ")
        score = (chinese * 3 + japanese) / total

        if score > best_score:
            best_score = score
            best_enc = enc

    # 兜底：用 errors="replace" 再试一轮
    if not best_enc:
        for enc in CSV_ENCODING_CANDIDATES:
            try:
                with open(filepath, "r", encoding=enc, errors="replace") as f:
                    f.read(CSV_DETECT_READ_BYTES)
                best_enc = enc
                best_score = 0.0
                break
            except LookupError:
                continue

    return best_enc, best_score


def read_csv_preview(filepath: str, encoding: str, preview_lines: int = CSV_PREVIEW_LINES):
    """用指定编码读取CSV文件的前N行。
    返回 (headers, rows, raw_text_preview)
    """
    headers: List[str] = []
    rows: List[List[str]] = []
    raw_preview = ""

    try:
        with open(filepath, "r", encoding=encoding, errors="replace") as f:
            reader = csv.reader(f)
            for i, line in enumerate(reader):
                if i == 0:
                    headers = line
                elif i <= preview_lines:
                    rows.append(line)
    except Exception:
        pass

    # 原始文本预览（前 preview_lines 行）
    try:
        with open(filepath, "r", encoding=encoding, errors="replace") as f:
            raw_lines = []
            for i, line in enumerate(f):
                if i >= preview_lines:
                    break
                raw_lines.append(line.rstrip("\n\r"))
            raw_preview = "\n".join(raw_lines)
    except Exception:
        pass

    return headers, rows, raw_preview


def transcode_csv_to_utf8(src_path: str, src_encoding: str, dst_path: str) -> int:
    """将CSV文件从源编码转存为UTF-8-BOM，返回总行数。
    若源编码为空或未知则抛出 ValueError。
    """
    if not src_encoding:
        raise ValueError("源编码不能为空")

    all_rows: List[List[str]] = []
    with open(src_path, "r", encoding=src_encoding, errors="replace") as f:
        reader = csv.reader(f)
        for line in reader:
            all_rows.append(line)

    with open(dst_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(all_rows)

    return len(all_rows)


def confidence_label(score: float) -> str:
    """将编码探测得分转为中文置信度标签"""
    if score > 0.05:
        return "高"
    if score > 0.01:
        return "中"
    return "低"
from selenium.webdriver.remote.webdriver import WebDriver

# 剃毛豁免站点（如果需要保留完整DOM的站点）
STRIP_EXEMPT_SITES = []  # 例如 ["特定站点key"]

# 剃毛是否启用（默认关闭，GUI按键控制）
STRIP_ENABLED = False

# ===================== 【高难度网站抓取 v1.0 — 针对拉勾/51job等顶尖反爬站】 =====================
# 原理：半自动手动点击 + 详情页一开立刻 究极隐身 + 剃毛清洗
# 搜索页保持正常交互（与半自动逻辑相同），详情页自动引爆全量对抗

# 高难度抓取总开关（GUI按钮控制）
HARD_SITE_SCRAPE_ENABLED = False

# 高难度模式下的剃毛开关 — 详情页打开后强制执行剃毛（不受全局STRIP_ENABLED影响）
HARD_SITE_STRIP_FORCE = True

# 高难度站点专属配置（究极对抗 + 剃毛 + 额外等待）
HARD_SITE_CONFIG = {
    "51job": {
        "anti_level": "extreme",          # 究极反检测
        "strip_force": True,              # 强制剃毛
        "strip_layers": "all",            # 剃毛层数：all = 全部6层
        "stealth_reinject": True,         # 详情页重新注入隐身JS
        "cdp_patch_required": True,       # 必须修补CDC
        "cookie_preheat": True,           # 预热Cookie
        "referrer_spoof": True,           # 伪造Referrer
        "wait_extra_sec": 2.0,            # 详情页额外等待（让反爬JS先执行完）
        "url_patterns": ["51job.com", "jobs.51job.com", "search.51job.com"],
    },
    "lagou": {
        "anti_level": "extreme",
        "strip_force": True,
        "strip_layers": "all",
        "stealth_reinject": True,
        "cdc_patch_required": True,
        "cookie_preheat": True,
        "referrer_spoof": True,
        "wait_extra_sec": 2.5,
        "url_patterns": ["lagou.com", "www.lagou.com", "jobs.lagou.com"],
    },
    "zhilian": {
        "anti_level": "paranoid",
        "strip_force": True,
        "strip_layers": "all",
        "stealth_reinject": True,
        "cdc_patch_required": True,
        "cookie_preheat": False,
        "referrer_spoof": True,
        "wait_extra_sec": 1.0,
        "url_patterns": ["zhaopin.com", "jobs.zhaopin.com"],
    },
    "boss": {
        "anti_level": "extreme",
        "strip_force": True,
        "strip_layers": "all",
        "stealth_reinject": True,
        "cdc_patch_required": True,
        "cookie_preheat": True,
        "referrer_spoof": True,
        "wait_extra_sec": 2.0,
        "url_patterns": ["zhipin.com", "www.zhipin.com"],
    },
    "liepin": {
        "anti_level": "high",
        "strip_force": True,
        "strip_layers": "all",
        "stealth_reinject": True,
        "cdc_patch_required": False,
        "cookie_preheat": False,
        "referrer_spoof": True,
        "wait_extra_sec": 1.0,
        "url_patterns": ["liepin.com", "www.liepin.com"],
    },
    "default": {
        "anti_level": "extreme",
        "strip_force": True,
        "strip_layers": "all",
        "stealth_reinject": True,
        "cdc_patch_required": True,
        "cookie_preheat": True,
        "referrer_spoof": True,
        "wait_extra_sec": 2.0,
        "url_patterns": [],
    },
}

# 高难度模式下的详情页提取等待时间（比普通模式更久，让反爬JS先执行完）
HARD_SITE_PAGE_SETTLE_SEC = 3.0

# 高难度模式下的目标采集条数默认值
HARD_SITE_DEFAULT_TARGET = 10

# 高难度模式下 — 剃毛执行时机标记（detail_page_opened → strip → stealth → extract）
HARD_SITE_STRIP_TIMING = "on_detail_page_open"  # 详情页一开立即执行

# 高难度模式 — 超时配置
HARD_SITE_DETAIL_TIMEOUT = 30  # 详情页加载超时（秒）
HARD_SITE_LIST_TIMEOUT = 45    # 列表页加载超时（秒）

# =========================== 剃毛JS ===========================

STRIP_JS = r"""
(function() {
    var stats = {fixed:0, absolute:0, iframe:0, script:0, style:0, nav:0, hidden:0};

    try {
        // === 第1层：杀覆盖层（fixed/absolute高z-index） ===
        var all = document.querySelectorAll('*');
        var toRemove = [];
        for (var i = all.length - 1; i >= 0; i--) {
            try {
                var el = all[i];
                if (el.tagName === 'BODY' || el.tagName === 'HTML') continue;
                if (el.offsetWidth === 0 && el.offsetHeight === 0) continue; // 尺寸为0的先不管

                var cs = window.getComputedStyle(el);
                var pos = cs.position;
                var zi = parseInt(cs.zIndex || 0);

                // fixed + 覆盖大面积 → 很可能是遮罩/弹窗
                if (pos === 'fixed') {
                    var r = el.getBoundingClientRect();
                    if (r.width > 100 || r.height > 100 || zi > 90) {
                        toRemove.push(el); stats.fixed++; continue;
                    }
                }
                // absolute + 高z-index → 悬浮层
                if (pos === 'absolute' && zi > 500) {
                    var r = el.getBoundingClientRect();
                    if (r.width > 80 || r.height > 80) {
                        toRemove.push(el); stats.absolute++; continue;
                    }
                }
            } catch(e) {}
        }
        toRemove.forEach(function(el) { try { el.remove(); } catch(e) {} });

        // === 第2层：杀iframe/embed/object ===
        document.querySelectorAll('iframe, embed, object').forEach(function(el) {
            try { el.remove(); stats.iframe++; } catch(e) {}
        });

        // === 第3层：杀script/style/stylesheet ===
        document.querySelectorAll('script, style, link[rel="stylesheet"]').forEach(function(el) {
            try { el.remove(); stats.script++; } catch(e) {}
        });

        // === 第4层：杀导航/装饰区 ===
        document.querySelectorAll(
            'nav, footer, aside, ' +
            '[role="navigation"], [role="banner"], [role="contentinfo"], ' +
            '[class*="navbar"], [class*="nav-bar"], [class*="sidebar"], ' +
            '[class*="footer"], [class*="header-bar"], [class*="topbar"]'
        ).forEach(function(el) {
            if (el.tagName === 'HEADER') return; // header可能含标题，保留
            try { el.remove(); stats.nav++; } catch(e) {}
        });

        // === 第5层：杀不可见元素 ===
        document.querySelectorAll(
            '[style*="display:none"], [style*="display: none"], ' +
            '[style*="visibility:hidden"], [style*="visibility: hidden"], ' +
            '[aria-hidden="true"], [hidden]'
        ).forEach(function(el) {
            try { el.remove(); stats.hidden++; } catch(e) {}
        });

        // === 第6层：恢复可读 ===
        try {
            if (document.body) {
                document.body.style.overflow = '';
                document.body.style.position = '';
                document.body.style.maxHeight = '';
            }
            document.documentElement.style.overflow = '';
        } catch(e) {}

    } catch(e) {
        stats.error = e.message;
    }

    return JSON.stringify(stats);
})();
"""

# =========================== 核心函数 ===========================

def strip_page(driver: WebDriver, site_key: str = "default") -> dict:
    """
    剃光当前页面所有非内容元素。

    参数:
        driver: Selenium WebDriver
        site_key: 站点标识（用于豁免检查）

    返回:
        dict: 剃毛统计 {fixed, absolute, iframe, script, style, nav, hidden}
    """
    if not STRIP_ENABLED:
        return {"stripped": False, "reason": "disabled"}

    if site_key in STRIP_EXEMPT_SITES:
        return {"stripped": False, "reason": f"site '{site_key}' exempted"}

    result_json = driver.execute_script(STRIP_JS)
    if result_json is None:
        return {"stripped": False, "reason": "js returned None"}

    stats = json.loads(result_json)
    total = sum(v for k, v in stats.items() if isinstance(v, int))
    print(f"[Strip] Removed {total} elements: {stats}")
    return {"stripped": True, "stats": stats, "total_removed": total}


def strip_and_get_text(driver: WebDriver, site_key: str = "default") -> str:
    """
    剃毛后返回纯文本内容。
    一站式：剃毛 → 取innerText
    """
    strip_page(driver, site_key)
    time.sleep(0.3)
    try:
        return driver.execute_script(
            "return document.body ? document.body.innerText : '';") or ""
    except Exception:
        return ""


def strip_and_get_html(driver: WebDriver, site_key: str = "default") -> str:
    """
    剃毛后返回清洗过的HTML（body.innerHTML）。
    适合需要保留部分标签结构的数据提取场景。
    """
    strip_page(driver, site_key)
    time.sleep(0.3)
    try:
        return driver.execute_script(
            "return document.body ? document.body.innerHTML : '';") or ""
    except Exception:
        return ""


# =========================== 自检 ===========================

if __name__ == "__main__":
    print("=" * 50)
    print("PageStripper v1.0 - Self Check")
    print("=" * 50)
    print(f"[OK] STRIP_ENABLED: {STRIP_ENABLED}")
    print(f"[OK] Exempt sites: {STRIP_EXEMPT_SITES}")
    print(f"[OK] Strip JS: {len(STRIP_JS)} chars")
    print("\nLayers:")
    print("  1. fixed/absolute+z-index overlays")
    print("  2. iframe/embed/object")
    print("  3. script/style/link[stylesheet]")
    print("  4. nav/footer/aside + ARIA + class patterns")
    print("  5. display:none/visibility:hidden/aria-hidden/hidden")
    print("  6. restore body overflow/position")



# =======================================================
# 【收拢: taotie_filter.py】
# =======================================================
import zhconv
import re
import functools

# ===== 预编译额外正则（加速重复匹配） =====
_WHITESPACE_PAT = re.compile(r'\s+')
_SCRIPT_STYLE_PAT = re.compile(r'<(script|style|noscript|iframe)[^>]*>.*?</\1>', re.S | re.I)
_HTML_TAG_PAT = re.compile(r'<[^>]+>')
_HTML_COMMENT_PAT = re.compile(r'<!--.*?-->', re.S)
_MULTI_NEWLINE_PAT = re.compile(r'\n{3,}')
_CONTROL_CHAR_PAT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# ===== 繁简转换缓存（减少重复转换） =====
_zhconv_cache = {}
_CACHE_MAX_SIZE = 256

def _cached_zhconv(text: str) -> str:
    """缓存繁简转换结果，命中率极高的URL重复内容"""
    h = hash(text)
    if h in _zhconv_cache:
        return _zhconv_cache[h]
    result = zhconv.convert(text, 'zh-cn')
    if len(_zhconv_cache) > _CACHE_MAX_SIZE:
        _zhconv_cache.clear()
    _zhconv_cache[h] = result
    return result

class TaoTieFilter:
    __slots__ = ("recruit_keywords", "zero_width", "_char_map_table")

    def __init__(self):
        self.recruit_keywords = ["招", "招聘", "诚聘", "急招", "高薪诚聘"]
        self.zero_width = ['​', '‌', '‍', '﻿']
        # 预构建字符映射表（str.translate 比多次 replace 快10倍+）
        self._char_map_table = str.maketrans(dict.fromkeys(
            [chr(c) for c in range(0x200b, 0x200e)] + [chr(0xfeff), chr(0x200d)],
            None
        ))

    def fix_reverse_text(self, text: str) -> str:
        for kw in self.recruit_keywords:
            if text.strip().endswith(kw) and len(text) > 20:
                text = text[::-1]
        return text

    def clean_text(self, text: str) -> str:
        """高性能文本清洗：str.translate + 链式正则"""
        if not text:
            return ""

        # 1. 零宽字符批量移除（translate比replace快10倍）
        text = text.translate(self._char_map_table)

        # 2. 字符映射
        for old, new in CHAR_MAP.items():
            text = text.replace(old, new)

        # 3. 全角转半角
        text = full2half(text)

        # 4. 正则清洗（预编译模式）
        text = NOISE_SYM_PAT.sub("", text)
        text = WORD_SPACE_PAT.sub(r'\1\2', text)
        text = TAIL_RUBBISH_PAT.split(text)[0]
        text = _WHITESPACE_PAT.sub(' ', text).strip()
        text = RUBBISH_PAT.sub("", text)

        return text

    def split_recruit_block(self, html: str) -> list:
        """快速分块：先剥离script/style，再按招聘关键词分割"""
        # 快速剥离无用标签
        html = USELESS_BLOCK_PAT.sub("", html)
        html = _HTML_COMMENT_PAT.sub("", html)
        html = _SCRIPT_STYLE_PAT.sub("", html)

        # 尝试匹配主内容区
        job_match = JOB_MAIN_PAT.search(html)
        if job_match:
            job_html = job_match.group(0)
            return [job_html]

        # fallback：按招聘关键词分割
        parts = RECRUIT_PAT.split(html)
        blocks = []
        current = ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if p in self.recruit_keywords:
                if current and len(current) > 200:
                    blocks.append(current)
                current = p
            else:
                current += p
        if current and len(current) > 200:
            blocks.append(current)

        return blocks if blocks else [html]

    def process(self, raw_html: str) -> list:
        """主处理流程：修复反序 -> 繁转简 -> 清洗 -> 数字修复 -> 分块"""
        if not raw_html:
            return []

        txt = self.fix_reverse_text(raw_html)

        # 快速剥离HTML标签获取纯文本（减少繁简转换的数据量）
        txt_stripped = _HTML_TAG_PAT.sub(" ", txt)
        txt_stripped = _HTML_COMMENT_PAT.sub("", txt_stripped)
        txt_stripped = _MULTI_NEWLINE_PAT.sub("\n", txt_stripped)

        # 繁简转换（使用缓存加速）
        txt_stripped = _cached_zhconv(txt_stripped)

        # 清洗
        txt_stripped = self.clean_text(txt_stripped)
        txt_stripped = rebuild_number(txt_stripped)

        # v3.1修复：返回清洗后的纯文本，不是原始HTML
        # split_recruit_block 返回的是HTML片段，正则无法在上面匹配
        return [txt_stripped] if txt_stripped else []

    def clean_for_qianlong(self, raw_text: str) -> str:
        """供潜龙模型使用的快速清洗"""
        if not raw_text:
            return ""
        text = _CONTROL_CHAR_PAT.sub("", raw_text)
        text = _WHITESPACE_PAT.sub(" ", text)
        return text.strip()

# 全局单例
taotie_filter = TaoTieFilter()


# =======================================================
# 【收拢: zhujian_watch.py】
# =======================================================
import re
from bs4 import BeautifulSoup

class ZhuJianEye:
    __slots__ = ()

    def rebuild_structured_text(self, html: str) -> str:
        """
        结构化文本重建器（高性能版）
        1. lxml解析器替代html.parser（快2-3倍）
        2. decompose批量移除无用节点
        3. 按业务优先级拼接文本
        """
        # 使用lxml解析器（比html.parser快2-3倍）
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        # 1. 批量剥离无用节点
        for tag in soup(["script", "style", "noscript", "iframe", "head",
                          "header", "footer", "nav", "comment", "meta", "link"]):
            tag.decompose()

        structured_parts = []

        # 2. 岗位名称（h1/h2/title标签）
        for tag_name in STRUCTURE_MARKERS["岗位名称"]:
            tag = soup.find(tag_name)
            if tag:
                text = tag.get_text(strip=True)
                if text and 2 < len(text) < 50:
                    structured_parts.append(f"【岗位名称】{text}")
                    break

        # 3. 基础信息（span标签中薪资/地点/经验/学历）
        info_keys = (STRUCTURE_MARKERS["薪资范围"] + STRUCTURE_MARKERS["工作地点"] +
                     STRUCTURE_MARKERS["学历要求"] + STRUCTURE_MARKERS["工作经验"])
        info_parts = []
        for span in soup.find_all("span", limit=200):
            text = span.get_text(strip=True)
            if text and any(k in text for k in info_keys):
                info_parts.append(text)
                if len(info_parts) >= 15:
                    break
        if info_parts:
            structured_parts.append(f"【岗位基础信息】{' | '.join(info_parts)}")

        # 4. 岗位职责与要求（扩展类名覆盖51job/智联/boss等）
        detail_class_keys = set(STRUCTURE_CLASS_KEYS.get("job_detail", ["job-detail","detail","desc"]))
        detail_class_keys.update(["tmsg","bmsg","job_msg","job-detail","job_detail",
                                   "position-desc","job-intro","detail-content",
                                   "job-description","jobDescription","description",
                                   "job-content","jobContent","content-box","info","intro"])
        for tag in soup.find_all(["div", "section", "article"], limit=150):
            cls = tag.get("class", [])
            cls_str = " ".join(cls) if isinstance(cls, list) else str(cls)
            if any(c in cls_str for c in detail_class_keys):
                desc = tag.get_text(separator="\n", strip=True)
                if len(desc) > 30:
                    structured_parts.append(f"【岗位职责与要求】{desc}")
                    break

        # 5. 公司信息（多策略）
        company_found = False
        # 5a. class匹配（扩展：cname/com_name/company-name等常见类名）
        company_class_keys = set(STRUCTURE_CLASS_KEYS["company"])
        company_class_keys.update(["cname","com_name","company-name","comp_name",
                                   "corp_name","corp-name","tCompany","tCompanyPage"])
        for tag in soup.find_all(["h3", "div", "a", "span", "p"], limit=120):
            cls = tag.get("class", [])
            cls_str = " ".join(cls) if isinstance(cls, list) else str(cls)
            if any(c in cls_str for c in company_class_keys):
                company_text = tag.get_text(strip=True)
                if company_text and 2 < len(company_text) < 80:
                    structured_parts.append(f"【公司信息】{company_text}")
                    company_found = True
                    break
        # 5b. 兜底：从title标签提取公司名（"职位-公司-站点"格式）
        if not company_found:
            title_tag = soup.find("title")
            if title_tag:
                title_text = title_tag.get_text(strip=True)
                # 尝试按 - 或 | 分割，找含"公司/有限/科技"的片段
                for seg in re.split(r'[-|—]', title_text):
                    seg = seg.strip()
                    if any(kw in seg for kw in ["公司","有限","科技","实业","集团","信息","网络","文化",
                                                  "传媒","教育","医疗","咨询","服务","贸易","管理","控股"]):
                        if 2 < len(seg) < 60:
                            structured_parts.append(f"【公司信息】{seg}")
                            company_found = True
                            break

        # 6. 拼接全文兜底
        full_text = soup.get_text(separator="\n", strip=True)
        structured_parts.append(f"【页面全文】\n{full_text}")

        # 7. 清理多余空行和重复文本
        final_text = "\n".join(structured_parts)
        lines = [line.strip() for line in final_text.splitlines() if line.strip()]
        return "\n".join(lines)

    def parse_full(self, html: str) -> str:
        return self.rebuild_structured_text(html)

    def to_json_input(self, text: str) -> str:
        import json
        return json.dumps({"content": text, "site_key": "zhilian"}, ensure_ascii=False)

    def watch_batch(self, result_list: list) -> tuple:
        """批量结果质检"""
        ok_data = []
        err_data = []
        for item in result_list:
            if item.get("岗位名称") or item.get("薪资范围") or item.get("工作地点"):
                ok_data.append(item)
            else:
                err_data.append(item)
        return ok_data, err_data

# 全局单例
zhujian_eye = ZhuJianEye()


# =======================================================
# 【收拢: data_extract.py】
# =======================================================
import json
import re
import os
import sys

# ── 尝试加载潜龙模型（纯优化，失败不影响） ──
# ⚠ 必须用 BASE_DIR（_get_app_root 的产物，frozen 时=exe 所在目录），**不能用
#   os.path.dirname(__file__)** —— 单文件 EXE 里 config 在 PYZ 内，__file__ 指向的是
#   sys._MEIPASS（临时解包目录），拿它拼路径永远找不到外置的模型目录，于是下面这个
#   try 静默失败 → 抽取全走正则兜底。打包版与开发版就会**对同一份文本抽出不同字段值**
#   且不报任何错（2026-09-10 发现：dev 实测验到 _qianlong 加载成功、打包版必然失败）。
_MODEL_DIR = os.path.join(BASE_DIR, "潜龙文本抽取模型")
_qianlong = None
try:
    if _MODEL_DIR not in sys.path:
        sys.path.insert(0, _MODEL_DIR)
    from infer_api import init_service as _init_svc, extract_fields as _ext
    _init_svc()
    _qianlong = _ext
except Exception:
    pass  # 静默失败，走纯正则

# ── 基础清洗 ──
_SYMBOL_PAT = re.compile(r'[┌┐└┘─│㈠㈡㈢㈣㈤㈥〓「」『』《》〔〕├┤\t\r]+')
_SPACE_PAT = re.compile(r'\s+')
_MULTI_NL = re.compile(r'\n{3,}')


def simple_clean(text: str) -> str:
    if not text: return ""
    text = _SYMBOL_PAT.sub("", text)
    text = _SPACE_PAT.sub(" ", text)
    seen = set(); lines = []
    for l in text.splitlines():
        s = l.strip()
        if s and s not in seen:
            seen.add(s); lines.append(s)
    return _MULTI_NL.sub("\n", "\n".join(lines)).strip()


# ===================================================================
# 逐字段提取器 — 每个字段独立、多层匹配
# ===================================================================

def _x_jobname(text: str) -> str:
    """岗位名称 — 从文本中提取职位名"""
    # 1. 标记引导
    for pfx in ["【岗位名称】", "岗位名称：", "岗位名称:", "职位名称：", "职位：", "招聘职位："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 2 <= len(s) <= 40: return s
    # 2. 常见职位名后缀匹配（独立词）
    m = re.search(
        r'([一-龥a-zA-Z+#·]{2,25}'
        r'(?:工程师|经理|专员|主管|总监|代表|顾问|主任|助理|员|师|工|长|家|'
        r'开发|设计|测试|运营|销售|客服|行政|财务|会计|人事|HR|司机|文员|'
        r'策划|编辑|记者|导演|演员|主播|讲师|教练|老师|教师|教授|医生|护士|'
        r'药师|律师|保安|保洁|厨师|服务员|收银|导购|店长|美工|'
        r'Java|Python|PHP|前端|后端|全栈|运维|DBA|产品经理|项目经理|架构师))',
        text[:500])
    if m: return m.group(1)
    # 3. 取第一行前30字当岗位名
    first = text.split("\n")[0][:30].strip()
    return first if 2 <= len(first) <= 30 else ""


def _x_company(text: str) -> str:
    """企业名称 — 多策略层层兜底，精准到词"""
    # 1. 标记引导（优先级最高）
    for pfx in ["【公司信息】", "【企业名称】", "【公司名称】",
                 "公司名称：", "企业名称：", "招聘单位：", "用人单位："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("【")[0].strip()[:60]
            if len(s) >= 2: return s
    # 2. 强正则：XX有限公司 / XX有限责任公司 / XX科技有限公司（全文本搜索）
    m = re.search(
        r'([一-龥a-zA-Z（）()·]{2,60})'
        r'(有限公司|有限责任公司|股份公司|集团有限公司|集团公司|'
        r'科技有限公司|信息技术有限公司|网络科技有限公司|'
        r'实业有限公司|文化传媒有限公司|教育科技有限公司|'
        r'医疗科技有限公司|生物科技有限公司|制药有限公司|'
        r'餐饮管理有限公司|酒店管理有限公司|物业管理有限公司|'
        r'物流有限公司|建筑工程有限公司|装饰工程有限公司|'
        r'设计有限公司|咨询有限公司|服务有限公司|贸易有限公司|'
        r'发展有限公司|投资有限公司|管理有限公司|控股有限公司|'
        r'合伙企业|外资企业|事务所|工作室)',
        text)  # 全文本搜索，不限字数
    if m: return (m.group(1) + m.group(2)).strip()
    # 3. 弱正则：XX公司/XX集团/XX科技（单独的"公司"后缀）
    m = re.search(
        r'([一-龥a-zA-Z（）()·]{3,40})'
        r'(公司|集团|科技|实业|网络|信息|文化|传媒|教育|医疗|'
        r'餐饮|酒店|物业|物流|建筑|装饰|设计|咨询|服务|贸易|'
        r'发展|投资|管理|生物|制药|化工|机械|电子|电气|自动化|'
        r'软件|数据|云计算|电子商务|新媒体|广告|影视|娱乐|旅游|'
        r'金融|保险|房地产|连锁|控股)',
        text)
    if m: return (m.group(1) + m.group(2)).strip()
    # 4. 从引导词后取短文本
    for pfx in ["公司：", "企业：", "公司:", "企业:"]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("【")[0].split(" ")[0].strip()[:40]
            if len(s) >= 2: return s
    return ""


def _x_location(text: str) -> str:
    """工作地点"""
    # 1. 标记引导
    for pfx in ["【工作地点】", "工作地点：", "工作地点:", "【地点】", "上班地点：",
                 "工作地址：", "办公地点：", "上班地址：", "base：", "Base："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 2 <= len(s) <= 20: return s
    # 2. "位于/坐落于 XX"
    m = re.search(r'(?:位于|坐落于|在)\s*([一-龥]{2,8}(?:市|区|县|省|新区|开发区|高新区))', text[:500])
    if m: return m.group(1)
    # 3. 城市名单 + 区后缀
    m = re.search(r'([一-龥]{2,6}(?:市|区|县|新区|开发区|街道|镇))', text[:500])
    if m: return m.group(1)
    # 4. 纯城市名
    cities = ['北京','上海','广州','深圳','杭州','南京','成都','武汉','重庆',
              '天津','苏州','西安','长沙','郑州','东莞','青岛','沈阳','宁波',
              '昆明','合肥','大连','福州','厦门','济南','无锡','佛山','珠海',
              '中山','惠州','温州','常州','嘉兴','南通','泉州','绍兴','烟台',
              '南宁','贵阳','兰州','海口','三亚','哈尔滨','长春','沈阳','太原',
              '南昌','石家庄','呼和浩特','乌鲁木齐','拉萨','银川','西宁']
    for c in sorted(cities, key=len, reverse=True):
        if c in text[:500]: return c
    return ""


def _x_salary(text: str) -> str:
    """薪资范围"""
    # 1. 标记引导
    for pfx in ["【薪资范围】", "薪资范围：", "薪资：", "薪资:", "薪酬：", "工资："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if len(s) >= 3 and len(s) <= 30: return s
    # 2. K/千/万格式
    m = re.search(
        r'(\d{1,3}(?:\.\d)?(?:\s*[kK千])?\s*[-~至到]\s*\d{1,3}(?:\.\d)?\s*[kK千万]'
        r'(?:\s*[/每]\s*月)?)',
        text[:800])
    if m: return m.group(1)
    # 3. 纯数字+元
    m = re.search(r'(\d{4,6}\s*[-~至到]\s*\d{4,6}\s*元?(?:\s*[/每]\s*月)?)', text[:800])
    if m: return m.group(1)
    # 4. X万-Y万
    m = re.search(r'(\d{1,2}(?:\.\d)?\s*万\s*[-~至到]\s*\d{1,2}(?:\.\d)?\s*万)', text[:800])
    if m: return m.group(1)
    # 5. 底薪+提成/面议
    m = re.search(r'(底薪\s*\d+\s*[kK千]?\s*\+\s*(?:提成|绩效|奖金))', text[:800])
    if m: return m.group(1)
    m = re.search(r'(面议|薪资面议|待遇面议)', text[:800])
    if m: return m.group(1)
    return ""


def _x_edu(text: str) -> str:
    """学历要求"""
    for pfx in ["【学历要求】", "学历要求：", "学历：", "学历:"]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 1 <= len(s) <= 15: return s
    m = re.search(
        r'(博士|硕士|研究生|MBA|EMBA|统招本科|全日制本科|本科|'
        r'全日制大专|大专|中专|中技|高中|初中|'
        r'学历不限|不限学历|学历无要求|学历不作要求)',
        text[:800])
    return m.group(1) if m else ""


def _x_exp(text: str) -> str:
    """工作经验"""
    for pfx in ["【工作经验】", "工作经验：", "经验要求：", "经验："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 1 <= len(s) <= 20: return s
    # 数字范围
    m = re.search(r'(\d{1,2}\s*[-~至到]\s*\d{1,2}\s*年(?:以上|相关|工作)?(?:经验|经历)?)', text[:800])
    if m: return m.group(1).strip()
    # X年经验
    m = re.search(r'(\d{1,2}\s*年\s*(?:以上|相关|工作)?(?:经验|经历))', text[:800])
    if m: return m.group(1).strip()
    # 中文数字
    m = re.search(r'([一二三四五六七八九十]+\s*年\s*(?:以上|相关|工作)?(?:经验|经历))', text[:800])
    if m: return m.group(1).strip()
    # 应届/无经验
    m = re.search(r'(应届(?:生|毕业生)?|经验不限|不限经验|无需经验|无经验|接受应届)', text[:800])
    if m: return m.group(1).strip()
    return ""


def _x_duties(text: str) -> str:
    """岗位职责"""
    for pfx in ["【岗位职责】", "岗位职责：", "岗位职责:", "【岗位职责与要求】",
                 "【职位描述】", "职位描述：", "【工作内容】", "工作内容："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].strip()
            # 取到下一个【标记或8000字符
            end = len(s)
            for sep in ["\n【", "\n岗位", "\n公司", "\n福利", "\n联系"]:
                j = s.find(sep)
                if 0 < j < end: end = j
            return s[:end].strip()[:8000]
    # 兜底：全文前8000（已经是清洗过的）
    return text[:8000] if len(text) > 10 else ""


def _x_welfare(text: str) -> str:
    """福利信息"""
    for pfx in ["【福利信息】", "福利信息：", "福利待遇：", "【福利待遇】"]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 2 <= len(s) <= 200: return s
    # 扫描福利关键词
    pat = re.compile(
        r'(五险一金|六险一金|七险一金|五险|社保|公积金|补充公积金|'
        r'双休|单休|大小周|弹性工作|弹性工时|不加班|不打卡|远程办公|在家办公|'
        r'包吃|包住|包吃住|食宿全包|住宿免费|餐补|房补|交通补贴|通讯补贴|交通补助|话补|'
        r'年终奖|年终奖金|绩效奖金|项目奖金|高额提成|计件提成|全勤奖|'
        r'带薪年假|年假|带薪休假|带薪培训|团建|团队旅游|旅游|'
        r'定期体检|年度体检|体检|健康体检|节日福利|生日福利|下午茶|零食|'
        r'股票期权|期权|股权|免费班车|班车|培训|晋升|时间灵活|朝九晚五|朝九晚六|'
        r'高温补贴|防寒补贴|夜班补贴|晚班补助|岗位补贴|工龄补贴|持证补贴|'
        r'劳保用品|防护用品|意外险|商业保险|补充医疗)', re.S)
    found = list(set(pat.findall(text)))
    return '、'.join(found) if found else ""


def _x_contact(text: str) -> str:
    """联系方式 — 手机/电话/邮箱/QQ"""
    parts = []
    # 1. 标记引导的行
    for pfx in ["【联系方式】", "联系方式：", "联系方式:", "联系电话：", "联系人：",
                 "联系电话", "电话：", "手机：", "邮箱：", "Email：", "QQ："]:
        i = text.find(pfx)
        if i >= 0:
            s = text[i+len(pfx):].split("\n")[0].split("【")[0].strip()
            if 1 <= len(s) <= 80:
                parts.append(s)
                break
    # 2. 手机号（全量搜索）
    phones = re.findall(r'1[3-9]\d{9}', text[:10000])
    if phones:
        parts.extend(list(set(phones)))
    # 3. 座机号（带区号）
    fixed = re.findall(r'(?:0\d{2,3}[-]\d{7,8}|\d{3,4}[-]\d{7,8})', text[:5000])
    if fixed:
        parts.extend(list(set(fixed)))
    # 4. 邮箱
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text[:5000])
    if emails:
        parts.extend(list(set(emails)))
    # 5. QQ号
    qqs = re.findall(r'QQ[：:\s]*(\d{5,11})', text[:3000])
    if qqs:
        parts.extend(['QQ:' + q for q in list(set(qqs))])
    # 6. 微信
    wxs = re.findall(r'(?:微信|WeChat|wx|VX|vx)[：:\s]*([a-zA-Z0-9_-]{5,20})', text[:3000])
    if wxs:
        parts.extend(['微信:' + w for w in list(set(wxs))])

    return '、'.join(parts[:5]) if parts else ""  # 最多5条，防过长


# ===================================================================
# 主提取入口
# ===================================================================

def rule_extract(text_list):
    raw_text = "\n".join(text_list) if isinstance(text_list, list) else str(text_list)

    # 解 JSON 包裹
    if raw_text.startswith("{") and '"content":' in raw_text:
        try:
            raw_text = json.loads(raw_text).get("content", raw_text)
        except (json.JSONDecodeError, TypeError):
            pass

    clean = simple_clean(raw_text)

    # ── 优先：潜龙模型 ──
    model = {}
    if _qianlong:
        try: model = _qianlong(clean)
        except Exception: pass

    # ── 合并：模型值有效就用，否则走本地正则 ──
    def pick(key, local_fn):
        mv = (model.get(key) or "").strip()
        # 过滤：不能是"不限/无/空"，不能等于字段名本身（模型可能误把标签当值）
        if (mv and mv not in ("不限", "无", "") and mv != key
            and not mv.startswith("【") and len(mv) >= 2):
            return mv
        lv = local_fn(clean)
        return lv if lv else "不限"

    return {
        "企业名称": _x_company(clean) or "不限",
        "岗位名称": pick("岗位名称", _x_jobname),
        "工作地点": pick("工作地点", _x_location),
        "薪资范围": pick("薪资范围", _x_salary),
        "学历要求": pick("学历要求", _x_edu),
        "工作经验": pick("工作经验", _x_exp),
        "岗位职责": pick("岗位职责", _x_duties),
        "福利信息": pick("福利信息", _x_welfare),
        "联系方式": pick("联系方式", _x_contact),
    }


def build_output(field_data: dict, raw_html: str, site_key: str, source_url: str) -> dict:
    qianlong_res = {
        "企业名称": field_data.get("企业名称", "不限"),
        "岗位名称": field_data.get("岗位名称", "不限"),
        "工作地点": field_data.get("工作地点", "不限"),
        "薪资范围": field_data.get("薪资范围", "不限"),
        "学历要求": field_data.get("学历要求", "不限"),
        "工作经验": field_data.get("工作经验", "不限"),
        "岗位职责": field_data.get("岗位职责", "不限"),
        "福利信息": field_data.get("福利信息", "不限"),
        "联系方式": field_data.get("联系方式", "不限"),
        "job_url": source_url,
        "source": site_key
    }
    has_valid = qianlong_res["岗位职责"].strip() not in ("", "不限")
    normal_data = [qianlong_res] if has_valid else []
    error_data = [] if has_valid else [qianlong_res]
    return {
        "zhujian_full_content_json": json.dumps(
            {"content": f"【原始页面】\n{raw_html}", "site_key": site_key, "source_url": source_url},
            ensure_ascii=False),
        "qianlong_extract_result": qianlong_res,
        "normal_data": normal_data,
        "error_data": error_data
    }


TOTAL_ROUND = 15


def extract_main(raw_html_text, site_key="zhilian", source_url="local_file"):
    return build_output(rule_extract([raw_html_text]), raw_html_text, site_key, source_url)


# =======================================================
# [merge: spider_batch_v9.py]
# =======================================================
import json
import time
import random
import traceback

from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By


# 延迟导入
_qiongqi_maw = None
_DBPlugin = None
_baize_module = None
_baize_instance = None
_page_stripper = None

def _lazy_imports():
    global _qiongqi_maw, _DBPlugin, _baize_module, _baize_instance, _page_stripper
    if _qiongqi_maw is None:
        import qiongqi_maw as _qiongqi_maw
    if _DBPlugin is None:
        # DBPlugin 类定义在 config 自身中（行3403），直接引用自身模块
        # 避免 import DBPlugin 模块造成的循环导入
        import sys as _lazy_sys
        _DBPlugin = _lazy_sys.modules['config']
    if _baize_module is None:
        import baize as _baize_module
        _baize_instance = _baize_module.baize
    if _page_stripper is None:
        from page_stripper import strip_page as _page_stripper


# =========================================================================
# 日志
# =========================================================================

_log_func = print

def set_log_func(fn):
    global _log_func
    _log_func = fn

def _log(msg):
    _log_func(f"[手动采集] {msg}")


# =========================================================================
# 核心：详情页数据提取（从当前driver页面提取）
# =========================================================================

def _extract_current_page(driver, url: str, site_key: str) -> dict:
    """对当前driver所在页面执行：等待加载 → 清洗 → flow()提取 → 返回qianlong_extract_result"""
    try:
        # 等待内容
        if ENABLE_SMART_WAIT:
            selector = SITE_CONTENT_SELECTORS.get(site_key, SITE_CONTENT_SELECTORS["default"])
            try:
                WebDriverWait(driver, timeout=15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            except Exception:
                time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))
        else:
            time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))

        # 滚动渲染
        for r in [0.3, 0.6, 0.85]:
            driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight*{r})")
            get_scroll_sleep()

        try:
            WebDriverWait(driver, timeout=6).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            time.sleep(1.5)

        # 清洗
        _page_stripper(driver, site_key)
        html = driver.page_source or ""

        if len(html) < PAGE_MIN_LENGTH:
            _log(f"页面内容过短({len(html)}字符)")
            return {}

        if any(k in html[:5000].lower() for k in PAGE_BLOCK_KEYS):
            _log("页面被拦截")
            return {}

        # flow提取
        from qiongqi_maw import flow as _flow
        flow_result = _flow(html, url, site_key)
        if not flow_result:
            return {}

        if isinstance(flow_result, dict):
            inner = flow_result.get("qianlong_extract_result", {})
            if inner and isinstance(inner, dict):
                return inner
            return flow_result
        return {}

    except Exception as e:
        _log(f"提取异常: {e}")
        return {}


# =========================================================================
# 高难度网站专用：详情页提取（究极隐身 + 剃毛 双开）
# =========================================================================

def _hard_extract_current_page(driver, url: str, site_key: str) -> dict:
    """
    高难度网站专用提取函数。
    与普通_extract_current_page的区别：
      1. 强制启用剃毛模式（不受全局STRIP_ENABLED开关影响）
      2. 重新注入究极隐身JS（FULL_STEALTH_JS），确保详情页也被保护
      3. 先剃毛 → 再隐身加固 → 最后提取
      4. 额外等待时间让反爬JS先执行完

    执行顺序：
      detail页打开 → wait extra → strip（剃毛）→ stealth re-inject（隐身加固）
      → scroll render → extract
    """
    try:
        # 获取站点高难度配置
        hard_cfg = HARD_SITE_CONFIG.get(site_key, HARD_SITE_CONFIG.get("default", {}))
        wait_extra = hard_cfg.get("wait_extra_sec", 2.0)

        # —— 第0步：额外等待（让目标站的反爬JS先执行完，避免检测到自动化） ——
        _log(f"  ⏳ 高难度模式：等待 {wait_extra}s 让页面稳定...")
        time.sleep(wait_extra)

        # —— 第1步：究极隐身重新注入（详情页可能刷新了window环境） ——
        if hard_cfg.get("stealth_reinject", True):
            _log("  🛡️ 重新注入究极隐身JS（FULL_STEALTH_JS）...")
            try:
                driver.execute_script(FULL_STEALTH_JS)
                _log("  ✅ 究极隐身JS注入完成")
            except Exception as e:
                _log(f"  ⚠️ 隐身JS注入异常: {e}")

        # —— 第2步：剃毛模式强制执行（清除遮罩/弹窗/悬浮层/iframe等） ——
        if hard_cfg.get("strip_force", True):
            _log("  🪒 强制执行剃毛模式（STRIP_JS）...")
            try:
                # 直接执行STRIP_JS（绕过全局STRIP_ENABLED开关）
                strip_result_json = driver.execute_script(STRIP_JS)
                if strip_result_json:
                    strip_stats = json.loads(strip_result_json)
                    total_removed = sum(
                        v for k, v in strip_stats.items()
                        if isinstance(v, int)
                    )
                    _log(f"  ✅ 剃毛完成：移除 {total_removed} 个干扰元素 "
                         f"(fixed={strip_stats.get('fixed',0)} "
                         f"absolute={strip_stats.get('absolute',0)} "
                         f"iframe={strip_stats.get('iframe',0)} "
                         f"script={strip_stats.get('script',0)} "
                         f"nav={strip_stats.get('nav',0)} "
                         f"hidden={strip_stats.get('hidden',0)})")
            except Exception as e:
                _log(f"  ⚠️ 剃毛执行异常: {e}")

        # —— 第3步：等待内容加载 ——
        if ENABLE_SMART_WAIT:
            selector = SITE_CONTENT_SELECTORS.get(
                site_key, SITE_CONTENT_SELECTORS["default"]
            )
            try:
                WebDriverWait(driver, timeout=HARD_SITE_DETAIL_TIMEOUT).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                _log(f"  ✅ 内容选择器 '{selector}' 已就绪")
            except Exception:
                _log(f"  ⚠️ 内容选择器超时，继续执行...")
                time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))
        else:
            time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))

        # —— 第4步：滚动渲染（触发懒加载） ——
        for r in [0.3, 0.6, 0.85]:
            try:
                driver.execute_script(
                    f"window.scrollTo(0, document.body.scrollHeight*{r})"
                )
                get_scroll_sleep()
            except Exception:
                pass

        # —— 第5步：等待DOM完全就绪 ——
        try:
            WebDriverWait(driver, timeout=8).until(
                lambda d: d.execute_script(
                    "return document.readyState"
                ) == "complete"
            )
        except Exception:
            time.sleep(2.0)

        # —— 第6步：二次剃毛（有些遮罩是懒加载/异步渲染出来的） ——
        if hard_cfg.get("strip_force", True):
            try:
                driver.execute_script(STRIP_JS)
            except Exception:
                pass
            time.sleep(0.5)

        # —— 第7步：获取清洗后的HTML ——
        html = driver.page_source or ""

        if len(html) < PAGE_MIN_LENGTH:
            _log(f"  ❌ 页面内容过短({len(html)}字符)")
            return {}

        # 检测是否被拦截
        if any(k in html[:5000].lower() for k in PAGE_BLOCK_KEYS):
            _log("  ❌ 页面被拦截（检测到拦截关键词）")
            return {}

        # —— 第8步：flow提取 ——
        from qiongqi_maw import flow as _flow
        flow_result = _flow(html, url, site_key)
        if not flow_result:
            return {}

        if isinstance(flow_result, dict):
            inner = flow_result.get("qianlong_extract_result", {})
            if inner and isinstance(inner, dict):
                return inner
            return flow_result
        return {}

    except Exception as e:
        _log(f"高难度提取异常: {e}")
        return {}


# =========================================================================
# 半自动监控采集器
# =========================================================================

class ManualClickMonitor:
    """
    打开列表页 → 把浏览器交给用户 → 监控URL变化 → 自动提取

    用户操作：
      - 在列表页点击职位卡片
      - 浏览详情页
      - 返回列表页继续点下一个
    爬虫自动：
      - 检测到新URL（非列表页、未采集过）→ 自动提取
      - 满 TARGET_COUNT 条 → 批量入库 → 结束
    """

    POLL_INTERVAL = 1.5        # URL轮询间隔（秒）
    PAGE_SETTLE_SEC = 2.0      # 进入新页面后等待稳定的时间

    def __init__(self, driver, site_key: str, target_count: int = 15):
        self.driver = driver
        self.site_key = site_key
        self.target_count = target_count

    def run(self, list_url: str, db_config: dict,
            progress_callback=None, stop_flag=None) -> dict:
        """
        主流程：
          1. 打开列表页
          2. 提示用户手动点击
          3. 循环监控URL变化
          4. 自动提取新页面
          5. 满15条入库结束
        """
        collected = []
        seen_urls = set()
        last_url = ""

        # —— 打开列表页 ——
        _log("=" * 50)
        _log(f"正在打开列表页: {list_url}")
        self.driver.get(list_url)

        # 等待列表页加载
        content_sel = SITE_CONTENT_SELECTORS.get(self.site_key, SITE_CONTENT_SELECTORS["default"])
        try:
            WebDriverWait(self.driver, timeout=20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, content_sel))
            )
        except Exception:
            time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))

        # 滚一下触发懒加载
        for i in range(2):
            self.driver.execute_script(
                f"window.scrollTo(0, document.body.scrollHeight * {(i+1)/3})")
            time.sleep(1.5)
        self.driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(1)

        list_page_url = self.driver.current_url
        list_handle = self.driver.current_window_handle  # 列表页tab句柄
        seen_urls.add(list_page_url)  # 不采集列表页自身
        last_url = list_page_url
        known_handles = set(self.driver.window_handles)   # 已知道的所有tab

        _log("=" * 50)
        _log(f"📌 列表页已就绪，浏览器交给你了！")
        _log(f"📌 请在Chrome窗口中手动点击职位卡片浏览详情页")
        _log(f"📌 爬虫会自动检测你打开的详情页并提取数据")
        _log(f"📌 支持：同tab跳转 / 新标签页打开 / 弹窗")
        _log(f"📌 目标: 采集满 {self.target_count} 条后自动入库结束")
        _log(f"📌 可随时在GUI点【终止全部】停止")
        _log("=" * 50)

        if progress_callback:
            progress_callback(0, self.target_count, f"等待手动点击... (0/{self.target_count})")

        # —— 监控循环 ——
        while len(collected) < self.target_count:
            if stop_flag and stop_flag():
                _log("任务已被用户终止")
                break

            try:
                # ── 检测1：新标签页 ──
                current_handles = set(self.driver.window_handles)
                new_handles = current_handles - known_handles

                if new_handles:
                    for new_handle in new_handles:
                        known_handles.add(new_handle)
                        self.driver.switch_to.window(new_handle)
                        time.sleep(self.PAGE_SETTLE_SEC)

                        detail_url = self.driver.current_url
                        if detail_url in seen_urls:
                            # 已经采集过，关掉这个tab回到列表页
                            try:
                                self.driver.close()
                            except Exception:
                                pass
                            try:
                                self.driver.switch_to.window(list_handle)
                            except Exception:
                                pass
                            continue

                        seen_urls.add(detail_url)

                        if self._is_detail_page(detail_url, list_page_url):
                            n = len(collected) + 1
                            _log(f"🔍 检测到新标签页 [{n}/{self.target_count}]")
                            _log(f"   URL: {detail_url[:120]}")

                            data = _extract_current_page(
                                self.driver, detail_url, self.site_key)

                            if data and isinstance(data, dict):
                                data.setdefault("job_url", detail_url)
                                data.setdefault("source", self.site_key)
                                collected.append(data)
                                _log(f"   ✅ 提取成功! ({len(collected)}/{self.target_count}) "
                                     f"岗位: {data.get('岗位名称','?')[:30]}")
                                if progress_callback:
                                    progress_callback(
                                        len(collected), self.target_count,
                                        f"手动采集 {len(collected)}/{self.target_count}")
                            else:
                                _log(f"   ⚠️ 提取失败/无数据，跳过")

                        # 关闭这个详情页tab，回到列表页
                        try:
                            self.driver.close()
                        except Exception:
                            pass
                        try:
                            self.driver.switch_to.window(list_handle)
                        except Exception:
                            # 列表页tab可能也被关了，尝试切到第一个活着的
                            remaining = self.driver.window_handles
                            if remaining:
                                self.driver.switch_to.window(remaining[0])
                                list_handle = remaining[0]
                        last_url = self.driver.current_url

                # ── 检测2：同tab内URL跳转 ──
                try:
                    current_url = self.driver.current_url
                except Exception:
                    current_url = ""

                if current_url and current_url != last_url and current_url not in seen_urls:
                    seen_urls.add(current_url)

                    if self._is_detail_page(current_url, list_page_url):
                        n = len(collected) + 1
                        _log(f"🔍 检测到同tab跳转 [{n}/{self.target_count}]")
                        _log(f"   URL: {current_url[:120]}")
                        time.sleep(self.PAGE_SETTLE_SEC)

                        data = _extract_current_page(
                            self.driver, current_url, self.site_key)

                        if data and isinstance(data, dict):
                            data.setdefault("job_url", current_url)
                            data.setdefault("source", self.site_key)
                            collected.append(data)
                            _log(f"   ✅ 提取成功! ({len(collected)}/{self.target_count}) "
                                 f"岗位: {data.get('岗位名称','?')[:30]}")
                            if progress_callback:
                                progress_callback(
                                    len(collected), self.target_count,
                                    f"手动采集 {len(collected)}/{self.target_count}")
                        else:
                            _log(f"   ⚠️ 提取失败/无数据，跳过")

                        # 自动返回列表页（不用手动退）
                        _log("   ↩ 自动返回列表页...")
                        try:
                            self.driver.back()
                            time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))
                        except Exception:
                            try:
                                self.driver.get(list_page_url)
                                time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))
                            except Exception:
                                pass

                    last_url = current_url

                # ── 清理已关闭的tab句柄 ──
                try:
                    current_handles = set(self.driver.window_handles)
                    known_handles = known_handles & current_handles  # 只保留还活着的
                    if list_handle not in current_handles and current_handles:
                        list_handle = list(current_handles)[0]
                        self.driver.switch_to.window(list_handle)
                except Exception:
                    pass

            except Exception as e:
                _log(f"监控循环异常: {e}")
                time.sleep(1)

            # 检查是否完成
            if len(collected) >= self.target_count:
                break

            time.sleep(self.POLL_INTERVAL)

        # —— 入库 ——
        insert_count = 0
        if collected:
            _log(f"正在批量入库 {len(collected)} 条记录...")
            insert_count = self._batch_insert(collected, db_config)
            _log(f"入库完成: {insert_count}/{len(collected)} 条")

        _log("=" * 50)
        _log(f"🎉 手动采集结束!")
        _log(f"   采集成功: {len(collected)} 条")
        _log(f"   入库成功: {insert_count} 条")
        _log("=" * 50)

        return {
            "discovered": len(collected),
            "processed": len(collected),
            "succeeded": len(collected),
            "failed": 0,
            "inserted": insert_count,
        }

    def _is_detail_page(self, url: str, list_url: str) -> bool:
        """判断URL是否是详情页（不是列表页、不是通用导航页）"""
        # 和列表页完全一样 → 不是详情页
        if url == list_url or url.rstrip("/") == list_url.rstrip("/"):
            return False
        # about:blank 等
        if url.startswith("about:") or url.startswith("data:"):
            return False
        # 太短的URL（可能是主页）
        if len(url) < 30:
            return False
        # 列表页域名相关的搜索/过滤页
        skip_patterns = ["/search", "/sso", "/login", "/register",
                         "about:blank", "/verify", "/captcha"]
        for p in skip_patterns:
            if p in url.lower():
                return False
        return True

    @staticmethod
    def _batch_insert(data_list: list, db_config: dict) -> int:
        if not data_list:
            return 0
        # —— 模板入库匹配（方案二：把“数据库表模板”装进自动入库总闸）——
        # 绑定 schema_v2 行业模板，且本批行确含模板业务列（非 job_url/source）
        # → 落库目标改为模板同名表【模板名】（缺表由 DBPlugin 自动按模板列建）。
        # 未绑定 / 招聘 legacy / 行是招聘键 → 一律走原 db_config 目标，逐字节不变。
        # 只读首行键判断即可（同一批来自同一条抽取链，形状一致）。
        _sp = ACTIVE_SCHEMA or None
        if _sp and _sp.get("engine") == "schema_v2":
            _tbl = str(_sp.get("name") or "").strip()
            _cols = {
                str(c.get("name"))
                for c in (_sp.get("columns") or [])
                if isinstance(c, dict)
            }
            _business = _cols - {"job_url", "source"}
            _first = data_list[0] if data_list else {}
            _first_keys = set(_first.keys()) if isinstance(_first, dict) else set()
            if _tbl and (_business & _first_keys):
                _old = str(db_config.get("table") or "")
                db_config = dict(db_config)
                db_config["table"] = _tbl
                if _old != _tbl:
                    _log(f"🧩 已绑定行业模板【{_tbl}】：入库目标由 "
                         f"{_old or '(默认)'} 改为模板表 {_tbl}（缺则自动建）")
            elif _tbl and not (_business & _first_keys):
                _log(f"⚠️ 已绑定模板【{_tbl}】但本批行不含该模板字段，仍按调用方表入库")
        db = None
        inserted = 0
        try:
            db = _DBPlugin.DBPlugin(
                mode=db_config.get("mode", "inner_sqlite"),
                host=db_config.get("host", "localhost"),
                user=db_config.get("user", "root"),
                pwd=db_config.get("pwd", ""),
                port=db_config.get("port", 3306),
                dbname=db_config.get("db", "spider_db"),
                tblname=db_config.get("table", "spider_data"),
            )
            for data in data_list:
                try:
                    db.insert(data)
                    inserted += 1
                except Exception as e:
                    _log(f"入库失败 [{data.get('岗位名称','?')[:20]}]: {e}")
        except Exception as e:
            _log(f"数据库连接失败: {e}")
        finally:
            if db:
                try:
                    db.close()
                except Exception:
                    pass
        return inserted


# =========================================================================
# 顶层入口 — GUI调用
# =========================================================================

def run_batch_discovery_crawl(
    list_url: str,
    site_key: str = "51job",
    db_config: dict = None,
    progress_callback=None,
    stop_flag=None,
    target_count: int = 15,
) -> dict:
    """
    半自动采集顶层入口。

    流程：
      获取driver → 注入反检测 → 打开列表页 → 交给用户手动点击
      → 后台监控URL变化 → 自动提取新页面 → 满15条入库结束
    """
    _lazy_imports()

    if db_config is None:
        db_config = {
            "mode": "inner_sqlite", "host": "localhost", "user": "root",
            "pwd": "", "port": 3306, "db": "spider_db", "table": "spider_data",
        }

    driver = None

    try:
        # —— 反检测 ——
        _log(f"站点={site_key} 列表页={list_url}")
        site_anti = SITE_ANTI_DETECT_CONFIG.get(site_key, {})
        effective_level = site_anti.get("level", ANTI_DETECT_LEVEL)
        _log(f"反检测等级: {effective_level}")

        if site_anti.get("cdc_patch_required"):
            _baize_instance._ensure_cdc_patched()

        # —— 获取驱动 ——
        _log("获取浏览器驱动...")
        if progress_callback:
            progress_callback(0, target_count, "启动浏览器...")

        driver = _qiongqi_maw.acquire_driver()

        # 关闭CDP资源拦截（让页面正常渲染CSS/图片）
        _baize_instance.disable_resource_blocking(driver)
        _log("已关闭资源拦截，页面将正常渲染")

        # 追加JS隐身
        if effective_level == "extreme":
            _baize_instance.inject_challenge_bypass(driver)
            _baize_instance.protect_js_engine(driver)
            _baize_instance.protect_dom_parsing(driver)
            _baize_instance.disable_debugger(driver)

        # —— 手动点击监控模式 ——
        monitor = ManualClickMonitor(driver, site_key, target_count=target_count)
        stats = monitor.run(
            list_url=list_url,
            db_config=db_config,
            progress_callback=progress_callback,
            stop_flag=stop_flag,
        )
        return stats

    except Exception as e:
        _log(f"半自动采集异常: {e}")
        traceback.print_exc()
        return {"discovered": 0, "processed": 0, "succeeded": 0, "failed": 0, "inserted": 0, "error": str(e)}

    finally:
        if driver:
            try:
                _qiongqi_maw.release_driver(driver)
            except Exception:
                pass
        _log("半自动采集任务结束")


# =========================================================================
# 高难度网站专用：HardSiteClickMonitor（究极隐身 + 剃毛 双引爆）
# =========================================================================

class HardSiteClickMonitor(ManualClickMonitor):
    """
    高难度网站专用监控器 — 继承自 ManualClickMonitor。

    与半自动模式的区别：
      - 搜索页/列表页：正常可交互，遇到验证页等待用户手动完成
      - 详情页：一打开立刻引爆 究极隐身 + 剃毛模式
      - 验证页：不自动关闭/跳转，等待用户手动验证后继续
      - 提取函数：使用 _hard_extract_current_page（内含双开逻辑）

    适用站点：拉勾/51job/BOOS直聘 等国内顶尖5%反爬站

    验证流程：
      - 搜索页出验证 → 不跳转不关闭 → 等用户手动滑动/点选 → 验证通过后刷新继续
      - 详情页出验证 → 等用户手动完成 → 验证后继续提取
    """

    # 验证页面识别关键词（URL和标题匹配）
    VERIFY_URL_PATTERNS = [
        "/verify", "/captcha", "/challenge", "/auth", "/check",
        "verify", "captcha", "challenge", "geetest", "dingxiang",
        "turing", "seck", "_uq_", "nvc", "ncov",
    ]
    VERIFY_TITLE_KEYWORDS = [
        "验证", "安全检测", "人机", "滑块", "captcha",
        "verify", "challenge", "请稍候", "安全检查",
        "访问验证", "身份验证", "滑动验证",
    ]

    @staticmethod
    def _is_verify_url(url: str) -> bool:
        """判断URL是否为验证/人机检测页面"""
        url_lower = url.lower()
        for pat in HardSiteClickMonitor.VERIFY_URL_PATTERNS:
            if pat in url_lower:
                return True
        return False

    def _check_verify_page(self, driver) -> bool:
        """检查当前页面是否为验证页（URL + 标题双重判断）"""
        try:
            current_url = driver.current_url.lower()
            for pat in self.VERIFY_URL_PATTERNS:
                if pat in current_url:
                    return True
            title = (driver.title or "").lower()
            for kw in self.VERIFY_TITLE_KEYWORDS:
                if kw in title:
                    return True
        except Exception:
            pass
        return False

    def _wait_for_verify_done(self, driver, handle_name: str = "当前页面",
                               poll_sec: float = 2.0,
                               max_wait_sec: float = 300.0) -> str:
        """
        等待用户手动完成验证。
        轮询直到页面不再是验证页，或超时。

        返回: 验证后页面的URL（如果是验证页超时则返回空字符串）
        """
        _log(f"🔐 [{handle_name}] 检测到验证页面！")
        _log(f"   ⏳ 等待你手动完成验证（滑块/点选/扫码等）...")
        _log(f"   📌 完成后会自动检测并继续提取")
        _log(f"   ⚠️ 超时时间 {int(max_wait_sec)}s，超时后将跳过")

        waited = 0.0
        while waited < max_wait_sec:
            time.sleep(poll_sec)
            waited += poll_sec

            try:
                # 检查是否还在验证页
                if not self._check_verify_page(driver):
                    current_url = driver.current_url
                    _log(f"   ✅ 验证已通过！当前URL: {current_url[:100]}")
                    return current_url
            except Exception:
                pass

            # 每30秒提示一次
            if int(waited) % 30 == 0 and waited > 0:
                _log(f"   ⏳ 仍在等待验证... (已等待 {int(waited)}s)")

        _log(f"   ⚠️ 验证等待超时 ({int(max_wait_sec)}s)，跳过此页面")
        return ""

    def run(self, list_url: str, db_config: dict,
            progress_callback=None, stop_flag=None) -> dict:
        """
        高难度主流程：
          1. 打开搜索页/列表页（正常模式，页面完好可点击）
          2. 提示用户手动点击
          3. 循环监控URL变化
          4. 检测到验证页 → 等待用户手动完成 → 继续
          5. 检测到详情页 → 立刻 究极隐身+剃毛 → 提取
          6. 满目标条数入库结束
        """
        collected = []
        seen_urls = set()
        last_url = ""

        hard_cfg = HARD_SITE_CONFIG.get(self.site_key,
                                         HARD_SITE_CONFIG.get("default", {}))
        anti_level = hard_cfg.get("anti_level", "extreme")

        # —— 打开搜索页 ——
        _log("=" * 60)
        _log(f"🔥 高难度网站抓取模式启动")
        _log(f"  站点: {self.site_key}  反检测: {anti_level}")
        _log(f"  剃毛: 详情页引爆  隐身: 详情页运行时注入")
        _log(f"  验证策略: 遇到验证页不关闭，等你手动完成")
        _log("=" * 60)
        _log(f"正在打开搜索页: {list_url}")
        self.driver.get(list_url)

        # 等待加载 — 如果搜索页本身就触发了验证，等待用户完成
        time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))
        if self._check_verify_page(self.driver):
            _log("⚠️ 搜索页触发了人机验证！")
            verified_url = self._wait_for_verify_done(
                self.driver, "搜索页", poll_sec=2.0, max_wait_sec=600.0)
            if verified_url:
                list_url = verified_url  # 更新为验证后的URL
                _log("✅ 搜索页验证通过，继续...")
            else:
                _log("⚠️ 搜索页验证超时，继续尝试...")

        # 等待内容加载
        content_sel = SITE_CONTENT_SELECTORS.get(
            self.site_key, SITE_CONTENT_SELECTORS["default"])
        try:
            WebDriverWait(self.driver, timeout=HARD_SITE_LIST_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, content_sel))
            )
            _log("✅ 搜索页加载完成")
        except Exception:
            _log("⚠️ 搜索页内容选择器超时 — 页面可能仍在加载，继续监控...")
            time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))

        # 滚屏触发懒加载
        for i in range(3):
            try:
                self.driver.execute_script(
                    f"window.scrollTo(0, document.body.scrollHeight * {(i+1)/4})")
                time.sleep(1.5)
            except Exception:
                pass
        self.driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(1)

        list_page_url = self.driver.current_url
        list_handle = self.driver.current_window_handle
        seen_urls.add(list_page_url)
        last_url = list_page_url
        known_handles = set(self.driver.window_handles)

        _log("=" * 60)
        _log(f"📌 搜索页已就绪，浏览器交给你了！")
        _log(f"📌 在Chrome中手动点击职位卡片 → 自动引爆究极+剃毛")
        _log(f"📌 遇到验证页不用管，手动验证完会自动继续")
        _log(f"📌 目标: {self.target_count} 条 | 支持新标签页/同tab跳转")
        _log("=" * 60)

        if progress_callback:
            progress_callback(0, self.target_count,
                              f"🔥等待点击... (0/{self.target_count})")

        # —— 监控循环 ——
        while len(collected) < self.target_count:
            if stop_flag and stop_flag():
                _log("任务已被用户终止")
                break

            try:
                # ── 检测1：新标签页 ──
                current_handles = set(self.driver.window_handles)
                new_handles = current_handles - known_handles

                if new_handles:
                    for new_handle in new_handles:
                        known_handles.add(new_handle)
                        self.driver.switch_to.window(new_handle)
                        time.sleep(HARD_SITE_PAGE_SETTLE_SEC)

                        new_url = self.driver.current_url

                        # ★ 验证页检测：不关闭，等用户手动验证
                        if self._check_verify_page(self.driver):
                            _log(f"🔐 新窗口出现验证页")
                            verified_url = self._wait_for_verify_done(
                                self.driver, "新窗口验证", poll_sec=2.0, max_wait_sec=300.0)
                            if verified_url:
                                new_url = verified_url
                            else:
                                # 超时，关闭这个窗口回去
                                try:
                                    self.driver.close()
                                except Exception:
                                    pass
                                try:
                                    self.driver.switch_to.window(list_handle)
                                except Exception:
                                    pass
                                continue

                        if new_url in seen_urls:
                            try:
                                self.driver.close()
                            except Exception:
                                pass
                            try:
                                self.driver.switch_to.window(list_handle)
                            except Exception:
                                pass
                            continue

                        seen_urls.add(new_url)

                        if self._is_detail_page(new_url, list_page_url):
                            n = len(collected) + 1
                            _log(f"🔥 新标签页详情 [{n}/{self.target_count}]")
                            _log(f"   URL: {new_url[:120]}")
                            _log(f"   💥 引爆究极隐身 + 剃毛...")

                            data = _hard_extract_current_page(
                                self.driver, new_url, self.site_key)

                            if data and isinstance(data, dict):
                                data.setdefault("job_url", new_url)
                                data.setdefault("source", self.site_key)
                                collected.append(data)
                                _log(f"   ✅ 提取成功! ({len(collected)}/{self.target_count}) "
                                     f"岗位: {data.get('岗位名称','?')[:30]}")
                                if progress_callback:
                                    progress_callback(
                                        len(collected), self.target_count,
                                        f"🔥采集 {len(collected)}/{self.target_count}")
                            else:
                                _log(f"   ⚠️ 提取失败/无数据，跳过")

                        # 关闭详情tab，回到搜索页
                        try:
                            self.driver.close()
                        except Exception:
                            pass
                        try:
                            self.driver.switch_to.window(list_handle)
                        except Exception:
                            remaining = self.driver.window_handles
                            if remaining:
                                self.driver.switch_to.window(remaining[0])
                                list_handle = remaining[0]
                        last_url = self.driver.current_url

                # ── 检测2：同tab内URL跳转 ──
                try:
                    current_url = self.driver.current_url
                except Exception:
                    current_url = ""

                if current_url and current_url != last_url \
                   and current_url not in seen_urls:

                    # ★ 验证页检测：搜索页可能跳转到验证页
                    if self._check_verify_page(self.driver):
                        _log(f"🔐 搜索页跳转到验证页 — 等你手动验证...")
                        verified_url = self._wait_for_verify_done(
                            self.driver, "搜索页验证", poll_sec=2.0, max_wait_sec=300.0)
                        if verified_url:
                            # 验证通过后，检查是不是回到了详情页
                            current_url = verified_url
                            seen_urls.add(current_url)
                            # 验证后可能回到搜索页，更新 last_url
                            if not self._is_detail_page(current_url, list_url):
                                last_url = current_url
                                continue
                        else:
                            # 超时，回到搜索页
                            try:
                                self.driver.get(list_page_url)
                                time.sleep(get_random_sleep(
                                    WAIT_LOAD_MIN, WAIT_LOAD_MAX))
                            except Exception:
                                pass
                            last_url = self.driver.current_url
                            continue

                    seen_urls.add(current_url)

                    if self._is_detail_page(current_url, list_page_url):
                        n = len(collected) + 1
                        _log(f"🔥 同tab跳转详情 [{n}/{self.target_count}]")
                        _log(f"   URL: {current_url[:120]}")
                        _log(f"   💥 引爆究极隐身 + 剃毛...")
                        time.sleep(HARD_SITE_PAGE_SETTLE_SEC)

                        # 详情页也可能跳验证 — 再检查一次
                        if self._check_verify_page(self.driver):
                            _log(f"🔐 详情页触发验证 — 等你手动完成...")
                            verified_url = self._wait_for_verify_done(
                                self.driver, "详情页验证", poll_sec=2.0, max_wait_sec=300.0)
                            if not verified_url:
                                _log("   ⚠️ 详情页验证超时，返回搜索页")
                                try:
                                    self.driver.get(list_page_url)
                                except Exception:
                                    pass
                                last_url = self.driver.current_url
                                continue
                            # 验证通过，当前URL可能已变
                            current_url = self.driver.current_url
                            time.sleep(HARD_SITE_PAGE_SETTLE_SEC)

                        data = _hard_extract_current_page(
                            self.driver, current_url, self.site_key)

                        if data and isinstance(data, dict):
                            data.setdefault("job_url", current_url)
                            data.setdefault("source", self.site_key)
                            collected.append(data)
                            _log(f"   ✅ 提取成功! ({len(collected)}/{self.target_count}) "
                                 f"岗位: {data.get('岗位名称','?')[:30]}")
                            if progress_callback:
                                progress_callback(
                                    len(collected), self.target_count,
                                    f"🔥采集 {len(collected)}/{self.target_count}")
                        else:
                            _log(f"   ⚠️ 提取失败/无数据，跳过")

                        # 返回搜索页
                        _log("   ↩ 返回搜索页...")
                        try:
                            self.driver.back()
                            time.sleep(get_random_sleep(
                                WAIT_SHORT_MIN, WAIT_SHORT_MAX))
                        except Exception:
                            try:
                                self.driver.get(list_page_url)
                                time.sleep(get_random_sleep(
                                    WAIT_LOAD_MIN, WAIT_LOAD_MAX))
                            except Exception:
                                pass

                    last_url = current_url

                # ── 清理已关闭的tab句柄 ──
                try:
                    current_handles = set(self.driver.window_handles)
                    known_handles = known_handles & current_handles
                    if list_handle not in current_handles and current_handles:
                        list_handle = list(current_handles)[0]
                        self.driver.switch_to.window(list_handle)
                except Exception:
                    pass

            except Exception as e:
                _log(f"监控循环异常: {e}")
                time.sleep(1)

            if len(collected) >= self.target_count:
                break

            time.sleep(ManualClickMonitor.POLL_INTERVAL)

        # —— 入库 ——
        insert_count = 0
        if collected:
            _log(f"正在批量入库 {len(collected)} 条记录...")
            insert_count = ManualClickMonitor._batch_insert(
                collected, db_config)
            _log(f"入库完成: {insert_count}/{len(collected)} 条")

        _log("=" * 60)
        _log(f"🎉 高难度网站抓取结束!")
        _log(f"   采集成功: {len(collected)} 条")
        _log(f"   入库成功: {insert_count} 条")
        _log("=" * 60)

        return {
            "discovered": len(collected),
            "processed": len(collected),
            "succeeded": len(collected),
            "failed": 0,
            "inserted": insert_count,
        }


# =========================================================================
# 高难度网站抓取 — 顶层入口（GUI调用）
# =========================================================================

def _create_clean_driver_for_hard_site():
    """
    为高难度模式创建一个完全干净的 Chrome driver。

    ★ 关键：不注入任何 CDP 隐身脚本（FULL_STEALTH_JS / HEADLESS_BYPASS_JS 等）
    搜索页需要完全自然渲染，让用户正常浏览点击。
    详情页的反爬保护由 _hard_extract_current_page 在运行时通过 execute_script 注入。
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    # 修补CDC（chromedriver.exe中去掉$cdc_标记）
    _baize_instance._ensure_cdc_patched()

    opts = ChromeOptions()
    opts.binary_location = CHROME_PATH

    # 持久化profile（保持登录态/缓存）
    opts.add_argument(f"--user-data-dir={os.path.join(RUNTIME_DIR, 'hard_site_profile')}")

    # 窗口
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--start-maximized")

    # 反检测（仅 Chrome 启动参数级别，不注入JS）
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-features=AutomationControlled")
    opts.add_argument("--disable-infobars")

    # 基础
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")

    # 静默
    opts.add_argument("--log-level=3")
    opts.add_argument("--silent")

    # 页面加载策略
    opts.page_load_strategy = PAGE_LOAD_STRATEGY

    # 随机UA
    opts.add_argument(f"user-agent={get_random_ua()}")

    # 排除自动化标记（Chrome 148+ 兼容）
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # 创建driver
    service = ChromeService(executable_path=CHROMEDRIVER_PATH)
    service.log_output = None
    driver = webdriver.Chrome(service=service, options=opts)

    # ★ 只注入最轻量的 webdriver 隐藏（就一行，不破坏页面渲染）
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""
        })
    except Exception:
        pass

    # 加载持久化Cookie（如果有的话，免验证）
    if os.path.exists(COOKIE_JAR_PATH):
        try:
            driver.get("about:blank")
            with open(COOKIE_JAR_PATH, "rb") as f:
                cookies = pickle.load(f)
            for ck in cookies:
                try:
                    driver.add_cookie(ck)
                except Exception:
                    pass
        except Exception:
            pass

    driver.get("about:blank")
    return driver


def run_hard_site_crawl(
    list_url: str,
    site_key: str = "51job",
    db_config: dict = None,
    progress_callback=None,
    stop_flag=None,
    target_count: int = None,
) -> dict:
    """
    高难度网站抓取顶层入口。

    ★ 核心设计（v1.2 彻底修复空白页）：
      - 搜索页：用自己的干净driver，零CDP隐身注入，页面完全自然渲染
      - 详情页：运行时注入 FULL_STEALTH_JS + STRIP_JS（execute_script方式）
      - 不经过驱动池，不受之前其他模式的CDP污染

    流程：
      创建干净driver(零CDP) → 打开搜索页（完全自然渲染）
      → 交给用户手动点击 → 后台监控URL变化
      → 详情页打开时 → _hard_extract_current_page()
        → 运行时注入究极隐身 + 强制执行剃毛 → 提取
      → 满目标条数入库结束
    """
    _lazy_imports()

    if target_count is None:
        target_count = HARD_SITE_DEFAULT_TARGET

    if db_config is None:
        db_config = {
            "mode": "inner_sqlite", "host": "localhost", "user": "root",
            "pwd": "", "port": 3306, "db": "spider_db", "table": "spider_data",
        }

    driver = None

    try:
        hard_cfg = HARD_SITE_CONFIG.get(site_key,
                                         HARD_SITE_CONFIG.get("default", {}))
        _log(f"🔥 高难度模式 | 站点={site_key} | 搜索页={list_url}")
        _log(f"   策略: 搜索页零CDP裸跑 + 详情页运行时引爆究极+剃毛")
        _log(f"   目标={target_count}条 | driver=独立创建(不经过池)")

        # ★★★ v1.2 关键修复 ★★★
        # 自己创建干净driver，零CDP隐身注入，页面完全自然渲染
        _log("创建独立干净driver（零CDP隐身）...")
        if progress_callback:
            progress_callback(0, target_count, "🔥启动浏览器...")
        driver = _create_clean_driver_for_hard_site()
        _log("✅ 干净driver就绪 — 搜索页将完全自然渲染")

        # —— 高难度手动点击监控模式 ——
        monitor = HardSiteClickMonitor(
            driver, site_key, target_count=target_count)
        stats = monitor.run(
            list_url=list_url,
            db_config=db_config,
            progress_callback=progress_callback,
            stop_flag=stop_flag,
        )
        return stats

    except Exception as e:
        _log(f"高难度抓取异常: {e}")
        import traceback
        traceback.print_exc()
        return {
            "discovered": 0, "processed": 0, "succeeded": 0,
            "failed": 0, "inserted": 0, "error": str(e),
        }

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        _log("高难度抓取任务结束")


# =========================================================================
# 自检
# =========================================================================

if __name__ == "__main__":
    print("=" * 50)
    print("spider_batch_v9.py — 半自动手动采集引擎 自检")
    print("=" * 50)
    print(f"[OK] 默认目标采集数: 15 条 (可在GUI自定义)")
    print(f"[OK] URL轮询间隔: {ManualClickMonitor.POLL_INTERVAL}s")
    print(f"[OK] BATCH_DISCOVERY_MAX_LINKS: {BATCH_DISCOVERY_MAX_LINKS}")
    print("工作流程:")
    print("  1. 打开列表页")
    print("  2. 用户在Chrome中手动点击职位卡片")
    print("  3. 爬虫自动检测新页面并提取数据")
    print("  4. 满15条自动入库结束")
    print("=" * 50)


# =======================================================
# [merge: DBPlugin.py]
# =======================================================
import sqlite3
import pymysql
import os
import sys

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

# 固定入库字段 从config读取
FIX_FIELDS = DB_BASE_FIELDS

class DBPlugin:
    def __init__(self, mode, host, user, pwd, port, dbname, tblname, fields=None):
        self.mode = mode
        self.host = host
        self.user = user
        self.pwd = pwd
        self.port = port
        self.dbname = dbname
        # 表名转义：本类用到 self.tbl 的三处（create_table / get_columns / insert）都把它写在
        # 反引号里 —— 转义本来就是那三处的意图。2026-09-11 之前表名只来自 GUI 输入框或作者写的
        # 模板，转不转义看不出区别；开放"用户自由导入数据表模板"之后，表名可以由 JSON 里的 name
        # 决定，不转义等于给它一个闭合标识符的口子。只有**原本就带反引号**的坏表名会受本行影响。
        self.tbl = tblname.strip().replace("`", "``")
        self._fields = fields  # 自定义字段列表 [(name, type), ...]，None 则用默认
        self.conn = None
        self._connect()

    def _connect(self):
        if self.mode == "inner_sqlite":
            db_file = os.path.join(base_path, f"inner_sqlite_{self.dbname}.db")
            self.conn = sqlite3.connect(db_file, check_same_thread=False)
        else:
            self.conn = pymysql.connect(
                host=self.host,
                user=self.user,
                password=self.pwd,
                port=self.port,
                database=self.dbname,
                charset="utf8mb4"
            )
        self.create_table(self._fields)

    def create_table(self, fields=None):
        """创建数据表。fields=None 时自动决定列：
        已绑定行业模板(ACTIVE_SCHEMA, schema_v2) → 用模板列建；否则 → 招聘默认 FIX_FIELDS（原样）。
        传入显式 [(name, type), ...] 时以传入为准。"""
        if fields is None:
            _tpl_fields = auto_create_fields()
            fields = _tpl_fields if _tpl_fields else [(f, "TEXT") for f in FIX_FIELDS]
        col_sql = ", ".join([f"`{name}` {dtype}" for name, dtype in fields])
        if self.mode == "inner_sqlite":
            sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.tbl}` (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {col_sql}
            )
            """
        else:
            sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.tbl}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                {col_sql}
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        cur = self.conn.cursor()
        cur.execute(sql)
        self.conn.commit()

    def get_columns(self):
        """读取表的实际列名列表（排除 id）。SQLite/MySQL 通用。"""
        cur = self.conn.cursor()
        if self.mode == "inner_sqlite":
            cur.execute(f'PRAGMA table_info(`{self.tbl}`)')
            cols = [r[1] for r in cur.fetchall() if r[1] != "id"]
        else:
            cur.execute(f"DESCRIBE `{self.tbl}`")
            cols = [r[0] for r in cur.fetchall() if r[0] != "id"]
        return cols

    def insert(self, data_dict):
        """动态适配表列名插入数据，兼容旧表和新自定义表。"""
        cols = self.get_columns()
        if not cols:
            return
        values = [data_dict.get(col, "") for col in cols]
        placeholder = "?" if self.mode == "inner_sqlite" else "%s"
        place_list = [placeholder] * len(cols)
        sql = f"""
        INSERT INTO `{self.tbl}` ({', '.join([f"`{c}`" for c in cols])})
        VALUES ({', '.join(place_list)})
        """
        cur = self.conn.cursor()
        cur.execute(sql, values)
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()

# =======================================================
# [merge: browser_paths.py]
# =======================================================
import os
import sys

def get_app_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_app_root()

FULL_CHROME_DIR = os.path.join(BASE_DIR, "full_chrome")
CHROME_PATH = os.path.join(FULL_CHROME_DIR, "chrome.exe")
CHROMEDRIVER_PATH = os.path.join(FULL_CHROME_DIR, "chromedriver.exe")

def validate_paths(exit_on_fail=False):
    """验证Chrome/ChromeDriver路径是否存在"""
    ok = True
    if not os.path.exists(CHROME_PATH):
        print(f"[WARN] Chrome not found: {CHROME_PATH}")
        ok = False
    if not os.path.exists(CHROMEDRIVER_PATH):
        print(f"[WARN] ChromeDriver not found: {CHROMEDRIVER_PATH}")
        ok = False
    if not ok and exit_on_fail:
        print("[FATAL] Chrome/ChromeDriver missing. Please check browser_paths.py")
        input("Press Enter to exit...")
        sys.exit(1)
    return ok

# 静默验证（不退出，打包/导入安全）
if __name__ == "__main__":
    print(f"[INFO] BASE_DIR: {BASE_DIR}")
    print(f"[INFO] Chrome: {CHROME_PATH}")
    print(f"[INFO] Driver: {CHROMEDRIVER_PATH}")
    validate_paths(exit_on_fail=True)
else:
    # 被导入时只打印调试信息，不退出
    if os.environ.get("BROWSER_DEBUG"):
        print(f"[DEBUG] BASE_DIR: {BASE_DIR}")
        print(f"[DEBUG] Chrome: {CHROME_PATH}")
        print(f"[DEBUG] Driver: {CHROMEDRIVER_PATH}")

# =======================================================
# [merge: slider_solver.py]
# =======================================================
import time
import json
import random
import traceback
from typing import Optional

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By

# =========================== 全量指纹伪装 ===========================

# 随机Chrome版本指纹池
CHROME_FINGERPRINTS = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "version": "130",
        "sec_ch_ua": '"Google Chrome";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "platform": "Windows",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "version": "129",
        "sec_ch_ua": '"Google Chrome";v="129", "Chromium";v="129", "Not?A_Brand";v="99"',
        "platform": "Windows",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "version": "128",
        "sec_ch_ua": '"Google Chrome";v="128", "Chromium";v="128", "Not?A_Brand";v="99"',
        "platform": "Windows",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "version": "130",
        "sec_ch_ua": '"Microsoft Edge";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "platform": "Windows",
    },
]

# 随机视口
VIEWPORTS = [(1920, 1080), (1600, 900), (1440, 900), (1366, 768), (1536, 864), (1280, 720)]

# 验证码域名拦截列表
CAPTCHA_DOMAINS = [
    "*tcaptcha.qq.com*", "*captcha.qq.com*", "*captcha.gtimg.com*",
    "*captcha.geetest.com*", "*geetest.com/captcha*", "*gcaptcha4.geetest.com*",
    "*c.dun.163.com*", "*yidun*",
    "*dingxiang-inc.com*", "*cap.dingxiang-inc.com*",
    "*challenges.cloudflare.com*", "*recaptcha.net*", "*google.com/recaptcha*",
    "*hcaptcha.com*", "*aliyuncs.com/captcha*", "*nc_static*",
    "*verify.51job.com*", "*51job.com/*verify*",
]


# =========================== 反制JS ===========================

COUNTER_JS = r"""
(function() {
    'use strict';
    var __c = console.log.bind(console, '[COUNTER]');

    // ===== 1. 劫持验证码SDK初始化 =====
    var SDK_NAMES = [
        'TencentCaptcha','TCaptcha','tencentCaptcha',
        'initGeetest','Geetest',
        'capTile','capInit','capShow',
        'initCaptcha','showCaptcha','startVerify',
        'initSlideVerify','showSlideVerify',
        'aliyunCaptcha','aliCaptcha',
        'yidunInit','YDInit','initYidun',
        'dxCaptcha','dingxiangInit',
        'initTurnstile','hpjyInit',
    ];

    SDK_NAMES.forEach(function(name) {
        if (!window[name]) return;
        var orig = window[name];
        window[name] = function() {
            var cb = arguments[arguments.length-1];
            if (typeof cb === 'function') {
                setTimeout(function() {
                    cb({ret:0, ticket:'bypass_'+Date.now(),
                        randstr:'@'+Math.random().toString(36).substr(2,8)});
                }, 50 + Math.random()*100);
            }
            return {destroy:function(){}, refresh:function(){}, show:function(){},
                    getValidate:function(){return {}}};
        };
    });

    // ===== 2. 拦截验证码iframe创建 =====
    try {
        var _origCreateEl = document.createElement.bind(document);
        document.createElement = function(tag, opts) {
            var el = _origCreateEl(tag, opts);
            if (tag.toLowerCase() === 'iframe') {
                var _setAttr = el.setAttribute.bind(el);
                el.setAttribute = function(name, val) {
                    if (name === 'src' && typeof val === 'string' && (
                        /captcha|verify|tcaptcha|geetest|yidun|turnstile/i.test(val)
                    )) { return; }
                    return _setAttr(name, val);
                };
                Object.defineProperty(el, 'src', {
                    set: function(v) {
                        if (/captcha|verify|tcaptcha|geetest|yidun|turnstile/i.test(v)) return;
                        _setAttr('src', v);
                    }
                });
            }
            return el;
        };
    } catch(e) {}

    // ===== 3. 拦截XHR/fetch到验证域名 =====
    try {
        var _origFetch = window.fetch;
        window.fetch = function(url, opts) {
            if (typeof url === 'string' && /captcha|verify|tcaptcha|geetest|yidun|turnstile/i.test(url)) {
                return Promise.reject(new Error('Blocked'));
            }
            return _origFetch.apply(this, arguments);
        };

        var _origOpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {
            if (typeof url === 'string' && /captcha|verify|tcaptcha|geetest|yidun|turnstile/i.test(url)) {
                throw new Error('Blocked');
            }
            return _origOpen.apply(this, arguments);
        };
    } catch(e) {}

    // ===== 4. 每500ms清扫一次验证码元素 =====
    function sweepVerification() {
        try {
            // 删iframe
            document.querySelectorAll('iframe').forEach(function(f) {
                try {
                    var s = (f.src||'').toLowerCase();
                    if (!s || s.indexOf('captcha')>-1 || s.indexOf('verify')>-1 ||
                        s.indexOf('tcaptcha')>-1 || s.indexOf('geetest')>-1) {
                        f.remove();
                    }
                } catch(e) {}
            });
            // 删fixed遮罩
            document.querySelectorAll('*').forEach(function(el) {
                try {
                    if (el.tagName === 'BODY' || el.tagName === 'HTML') return;
                    var cs = window.getComputedStyle(el);
                    if (cs.position === 'fixed' && parseInt(cs.zIndex||0) > 100) {
                        var r = el.getBoundingClientRect();
                        if (r.width > window.innerWidth*0.5 || r.height > window.innerHeight*0.5) {
                            el.remove();
                        }
                    }
                } catch(e) {}
            });
        } catch(e) {}
    }
    setInterval(sweepVerification, 500);
    sweepVerification();

    // ===== 5. 恢复body可滚动 =====
    try {
        var _observer = new MutationObserver(function() {
            if (document.body) {
                document.body.style.overflow = '';
                document.body.style.position = '';
                document.documentElement.style.overflow = '';
            }
        });
        _observer.observe(document.documentElement, {attributes:true, childList:true, subtree:true});
    } catch(e) {}
})();
"""

# =========================== 核心反制类 ===========================

class CaptchaAttacker:

    def __init__(self, driver: WebDriver):
        self.driver = driver

    # ---- CDP层：全面伪造指纹 ----
    def _spoof_all_fingerprints(self):
        """CDP层全量伪造浏览器信号"""
        fp = random.choice(CHROME_FINGERPRINTS)
        vp = random.choice(VIEWPORTS)

        # UserAgent覆写
        try:
            self.driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": fp["ua"],
                "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                "platform": fp["platform"],
            })
        except Exception:
            pass

        # 额外请求头
        try:
            self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                "headers": {
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Sec-CH-UA": fp["sec_ch_ua"],
                    "Sec-CH-UA-Platform": f'"{fp["platform"]}"',
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "Cache-Control": "max-age=0",
                }
            })
        except Exception:
            pass

        # 视口
        try:
            self.driver.set_window_size(vp[0], vp[1])
        except Exception:
            pass

        # 地理位置
        try:
            geo_list = [
                {"lat": 31.2304, "lon": 121.4737},   # 上海
                {"lat": 39.9042, "lon": 116.4074},    # 北京
                {"lat": 22.5431, "lon": 114.0579},    # 深圳
                {"lat": 30.5728, "lon": 104.0668},    # 成都
            ]
            geo = random.choice(geo_list)
            self.driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
                "latitude": geo["lat"], "longitude": geo["lon"], "accuracy": random.randint(30, 200)
            })
        except Exception:
            pass

        # 时区
        try:
            self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
        except Exception:
            pass

        # 设备指标
        try:
            self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": vp[0], "height": vp[1],
                "deviceScaleFactor": random.choice([1, 1.25, 1.5, 2]),
                "mobile": False,
                "screenWidth": vp[0], "screenHeight": vp[1],
                "positionX": 0, "positionY": 0,
            })
        except Exception:
            pass

        print(f"[COUNTER] Fingerprint spoofed: Chrome {fp['version']}, {vp[0]}x{vp[1]}")

    # ---- 阻断验证域名 + 注入反制JS ----
    def _setup_countermeasures(self):
        """一次性部署所有反制措施"""
        # 阻断验证码域名
        try:
            self.driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": CAPTCHA_DOMAINS})
            self.driver.execute_cdp_cmd("Network.enable", {})
            print(f"[COUNTER] Blocked {len(CAPTCHA_DOMAINS)} captcha domains")
        except Exception as e:
            print(f"[COUNTER] Domain block error: {e}")

        # 注入预加载反制JS
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": COUNTER_JS
            })
            print("[COUNTER] Counter JS injected")
        except Exception as e:
            print(f"[COUNTER] JS inject error: {e}")

    # ---- 清空浏览器状态 ----
    def _nuke_browser_state(self):
        """彻底清空浏览器状态"""
        try:
            self.driver.delete_all_cookies()
            self.driver.execute_script("""
                try { localStorage.clear(); } catch(e) {}
                try { sessionStorage.clear(); } catch(e) {}
                try {
                    indexedDB.databases().then(function(dbs) {
                        dbs.forEach(function(db) { indexedDB.deleteDatabase(db.name); });
                    }).catch(function(){});
                } catch(e) {}
                try {
                    navigator.serviceWorker.getRegistrations().then(function(r) {
                        r.forEach(function(s) { s.unregister(); });
                    }).catch(function(){});
                } catch(e) {}
                try {
                    document.cookie.split(';').forEach(function(c) {
                        document.cookie = c.trim().split('=')[0] +
                            '=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/';
                    });
                } catch(e) {}
            """)
            print("[COUNTER] Browser state nuked")
        except Exception as e:
            print(f"[COUNTER] Nuke error: {e}")

    # ---- DOM摧毁 ----
    def _destroy_verification_dom(self) -> dict:
        """摧毁验证码DOM，返回页面状态"""
        result = self.driver.execute_script("""
            var r = 0;
            try {
                document.querySelectorAll('iframe').forEach(function(f) {
                    try { f.remove(); r++; } catch(e){}
                });
                var all = document.querySelectorAll('*');
                for(var i=all.length-1; i>=0; i--) {
                    try {
                        if(all[i].tagName==='BODY'||all[i].tagName==='HTML') continue;
                        var cs=window.getComputedStyle(all[i]);
                        if(cs.position==='fixed'&&parseInt(cs.zIndex||0)>50) {
                            var rect=all[i].getBoundingClientRect();
                            if(rect.width>100||rect.height>100){all[i].remove();r++;}
                        }
                    } catch(e){}
                }
                if(document.body) {
                    document.body.style.overflow='';
                    document.body.style.position='';
                }
                document.documentElement.style.overflow='';
            } catch(e) {}
            var body = document.body ? document.body.innerText.substring(0, 500) : '';
            return JSON.stringify({removed:r, body:body});
        """)
        if result:
            return json.loads(result)
        return {"removed": 0, "body": ""}

    def _safe_get_body(self) -> str:
        """安全获取页面文本，处理 no such execution context"""
        for _ in range(3):
            try:
                return self.driver.execute_script(
                    "return document.body ? document.body.innerText.substring(0, 500) : '';") or ""
            except Exception:
                time.sleep(0.5)
        return ""

    def _is_blocked(self) -> bool:
        blocked_kw = ["滑动验证", "验证码", "人机验证", "访问验证",
                      "请按住滑块", "拖动到最右边", "captcha", "verify"]
        body = self._safe_get_body()
        return any(k in body for k in blocked_kw) if body else True

    def _page_has_content(self) -> bool:
        body = self._safe_get_body()
        return len(body) > 300 and not any(
            k in body for k in ["滑动验证", "验证码", "人机验证", "访问验证",
                               "请按住滑块", "拖动到最右边", "captcha", "verify"])

    # ===========================
    # 主入口
    # ===========================

    def attack(self) -> bool:
        """
        精准反制（v7.1）：
        - 不做多轮猛攻，一轮一击
        - 不频繁清Cookie（那是自爆行为）
        - 部署反制JS + 阻断域名 + 静默重载
        - 还不行就退避，等外层指数退避重试
        """
        print("[COUNTER] ====== SURGICAL STRIKE ======")
        current_url = self.driver.current_url

        # 部署反制（轻量版：只做指纹伪造 + DOM清除，不拦截网络）
        # 注意：Network.setBlockedURLs 会拦截页面正常资源导致"连不上网"
        # 改用 Page.addScriptToEvaluateOnNewDocument 注入轻量JS
        self._spoof_all_fingerprints()

        # 注入轻量反制JS（不拦截fetch/XHR/createElement，避免影响正常页面）
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": r"""
                (function() {
                    // 只劫持验证码SDK初始化，不拦截网络请求
                    var names = ['TencentCaptcha','TCaptcha','initGeetest','Geetest',
                        'capTile','capInit','initCaptcha','showCaptcha','startVerify',
                        'initSlideVerify','aliyunCaptcha','yidunInit','initYidun',
                        'dxCaptcha','initTurnstile'];
                    names.forEach(function(n) {
                        if (window[n]) window[n] = function() {
                            var cb = arguments[arguments.length-1];
                            if (typeof cb === 'function') setTimeout(function() {
                                cb({ret:0,ticket:'bypass_'+Date.now(),randstr:'@'+Math.random().toString(36)});
                            }, 100);
                            return {destroy:function(){},refresh:function(){}};
                        };
                    });
                })();
                """
            })
            print("[COUNTER] Lightweight JS injected (SDK hijack only)")
        except Exception as e:
            print(f"[COUNTER] JS inject: {e}")

        # 摧毁当前验证DOM
        dom = self._destroy_verification_dom()
        print(f"  DOM cleared: {dom['removed']} elements")

        # 静默重载（超时保护 + 降级JS跳转 + 上下文容错）
        print(f"  Reloading silently...")
        try:
            self.driver.set_page_load_timeout(15)
            self.driver.get(current_url)
        except Exception as e:
            err_msg = str(e)[:80]
            print(f"  driver.get(): {err_msg}, trying JS navigation...")
            try:
                self.driver.execute_script(f"window.location.href = '{current_url}';")
            except Exception:
                print(f"  JS navigation also failed, continuing anyway...")
        finally:
            try:
                self.driver.set_page_load_timeout(30)
            except Exception:
                pass

        # 等页面稳定（处理"no such execution context"）
        for wait_i in range(10):
            time.sleep(1)
            try:
                ready = self.driver.execute_script("return document.readyState || 'loading';")
                if ready == "complete":
                    break
            except Exception:
                pass

        # 再等一下确保DOM完全渲染
        time.sleep(2)

        # 检查
        if self._page_has_content():
            print(f"[COUNTER] ====== BYPASSED ======")
            return True

        body = self.driver.execute_script(
            "return document.body ? document.body.innerText.substring(0, 200) : '';") or ""
        print(f"[COUNTER] Still blocked: '{body[:100]}'")

        # 如果是IP封杀(访问被阻断)，不要继续——让外层退避等冷却
        if "访问被阻断" in body:
            print("[COUNTER] IP blocked - backing off, let outer retry handle it")
            return False

        print("[COUNTER] ====== STRIKE FAILED ======")
        return False


# =========================== 对外接口 ===========================

def solve_current_slider(driver: WebDriver) -> bool:
    return CaptchaAttacker(driver).attack()


# =========================== 自检 ===========================

if __name__ == "__main__":
    print("=" * 50)
    print("CaptchaAttacker v7.0 - Counter-Attack Module")
    print("=" * 50)
    print(f"[OK] {len(CHROME_FINGERPRINTS)} Chrome fingerprints")
    print(f"[OK] {len(VIEWPORTS)} viewports")
    print(f"[OK] {len(CAPTCHA_DOMAINS)} captcha domains blocked")
    print(f"[OK] Counter JS: {len(COUNTER_JS)} chars")
    print("\nStrategy: 全量指纹伪造 + 域名阻断 + JS劫持 + 状态清空 + 多轮重试")


# =======================================================
# [merge: industrial_spider.py]
# =======================================================
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="zhconv")
import os
import time
import traceback
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

# 全局状态变量
current_retry = 0
FAIL_COUNTER = 0
CIRCUIT_BREAKER = False

# ═══════════════════════════════════════════════════════════════
# 验证提示回调（v12.1.2：Qt 版应在 main_qt.py 开头注入 handler）
# ═══════════════════════════════════════════════════════════════
# 默认回退：控制台 input，非阻塞场景返回 True
def _console_verify_tip() -> bool:
    """控制台回退 — 无 GUI 可用的验证提示。"""
    print("\n" + "=" * 50)
    print("⚠️  检测到人机验证（滑块/拼图/验证码）")
    print("   请切换到 Chrome 窗口手动完成验证")
    print("   完成后按 Enter 继续，输入 q 跳过")
    print("=" * 50)
    try:
        choice = input("> ").strip().lower()
        return choice != "q"
    except (EOFError, KeyboardInterrupt):
        return False

# 可替换的验证提示处理器 — Qt 启动时注入 QMessageBox 版本
_verify_tip_handler = _console_verify_tip

def set_verify_tip_handler(handler):
    """注入自定义验证提示函数。Qt 版应在 main() 中调用此函数。"""
    global _verify_tip_handler
    _verify_tip_handler = handler

def show_verify_tip() -> bool:
    """弹出验证提示（调用已注册的 handler，默认控制台 input）。"""
    try:
        return _verify_tip_handler()
    except Exception as e:
        print(f"[verify_tip] handler 异常: {e}，回退到控制台")
        return _console_verify_tip()

def init_browser_pool():
    pass

def recycle_driver(driver):
    try:
        if driver:
            driver.quit()
    except Exception:
        pass

def preprocess_html(html: str) -> str:
    if not html:
        return ""
    return html.strip()

def fetch_page(url: str, wait_second: int = 0, site_key: str = "default") -> str:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    html_content = ""
    driver = None

    if CIRCUIT_BREAKER:
        if LOG_ENABLE:
            print("[circuit_breaker_active]")
        return ""

    if not os.path.exists(CHROME_PATH):
        if LOG_ENABLE:
            print(f"chrome_not_found: {CHROME_PATH}")
        return ""

    try:
        opts = Options()
        opts.binary_location = CHROME_PATH

        # 性能优化参数
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-sync")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-translate")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument("--mute-audio")
        opts.add_argument("--disable-hang-monitor")
        opts.add_argument("--disable-prompt-on-repost")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--log-level=3")

        for arg in CHROME_LAUNCH_ARGS:
            if arg not in ["--disable-gpu", "--disable-dev-shm-usage",
                           "--no-sandbox", "--disable-extensions",
                           "--disable-background-networking", "--disable-sync",
                           "--disable-default-apps"]:
                opts.add_argument(arg)

        opts.add_argument(f"user-agent={get_random_ua()}")
        opts.page_load_strategy = PAGE_LOAD_STRATEGY

        driver = webdriver.Chrome(options=opts)

        # 隐身脚本注入
        if ENABLE_CDP_STEALTH:
            driver.execute_script(get_full_finger_js())

        # 拦截验证/广告域名
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*captcha*", "*verify*", "*waf*", "*security*",
                "*track*", "*monitor*", "*finger*",
                "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg",
                "*.css", "*.woff", "*.woff2", "*.ttf",
                "*.mp4", "*.mp3", "*.webm"
            ]
        })
        driver.execute_cdp_cmd("Network.enable", {})

        driver.get(url)

        # 智能等待
        if ENABLE_SMART_WAIT:
            selector = SITE_CONTENT_SELECTORS.get(site_key, SITE_CONTENT_SELECTORS["default"])
            try:
                WebDriverWait(driver, timeout=15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            except Exception:
                time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))
        else:
            time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))

        # 拦截判断
        if any(k in driver.title or k in driver.current_url.lower() for k in INTERCEPT_KEYWORDS):
            if LOG_ENABLE:
                print("[human_verification_detected]")
            # v6.1: 尝试滑块自动破解
            slider_keywords = ["geetest", "yidun", "dingxiang", "tcaptcha", "滑块", "slider", "slide"]
            page_text = driver.page_source[:8000].lower() if driver.page_source else ""
            if any(k in page_text for k in slider_keywords):
                print("[INDUSTRIAL] 检测到滑块，启动自动破解...")
                try:
                    from slider_solver import solve_current_slider
                    if solve_current_slider(driver):
                        print("[INDUSTRIAL] ✅ 滑块自动破解成功！")
                        # 不弹窗，继续执行
                    else:
                        print("[INDUSTRIAL] ❌ 自动破解失败，回退手动")
                        if not show_verify_tip():
                            raise Exception("user_cancelled")
                except Exception:
                    if not show_verify_tip():
                        raise Exception("user_cancelled")
            else:
                if not show_verify_tip():
                    raise Exception("user_cancelled")
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            driver.refresh()
            time.sleep(get_random_sleep(WAIT_REFRESH_MIN, WAIT_REFRESH_MAX))

        # 页面等待
        if wait_second > 0:
            time.sleep(wait_second)
        else:
            time.sleep(get_random_sleep(WAIT_LONG_MIN, WAIT_LONG_MAX))

        # 快速滚动
        scroll_times, scroll_step = get_scroll_config()
        for i in range(scroll_times):
            driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {i/scroll_times})")
            get_scroll_sleep()
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")

        # 等待完全渲染
        try:
            WebDriverWait(driver, timeout=6).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            time.sleep(1.5)

        from page_stripper import strip_page
        strip_page(driver, site_key)
        html_content = driver.page_source
        if len(html_content) < 2000 or any(flag in html_content for flag in INVALID_FLAGS):
            raise Exception("page_blocked_or_empty")

        if LOG_ENABLE:
            print(f"page_fetched: {len(html_content)} chars")

    except Exception as e:
        current_retry += 1
        if LOG_ENABLE:
            print(f"[fetch_error_retry_{current_retry}] {str(e)}")
        traceback.print_exc()
        time.sleep(exponential_backoff(current_retry, base_sec=2.0, max_sec=60.0))
        FAIL_COUNTER += 1
        if FAIL_COUNTER >= BREAKER_LIMIT:
            CIRCUIT_BREAKER = True
            if LOG_ENABLE:
                print("[circuit_breaker_reached]")
    finally:
        if driver:
            recycle_driver(driver)

    return preprocess_html(html_content)

init_browser_pool()
fetch_main = fetch_page
