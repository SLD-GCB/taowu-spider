# media_recipe.py — 声明式「媒体配方」引擎（JSON 配方 → MediaTrack）。
#
# 目的：把平台抓取策略从 Python 代码变成**用户可编辑的 JSON 配置文件**。一个站 = 一个
# 媒体配方/*.json，写明"API 在哪、怎么求、从哪个字段取流、选哪一档、标题怎么拼"，用户改配置
# 即可新增/调整站点，不必写代码、不必重打包。
#
# 组织范式对等 数据表模板/ 与 字段规则/：BASE_DIR 下的中文目录 + config 目录扫描 + 容错加载
# + 一套"目录缺失就空转"的兜底。目录名见 config.MEDIA_RECIPE_DIR（默认 <BASE_DIR>/媒体配方）。
#
# ── 顶层纪律 ────────────────────────────────────────────────────────────
# · 本模块顶层仅 stdlib（json/os/re/threading/time/logging）；requests / websocket / media_capture
#   一律在函数体内懒加载 —— 同 media_resolve / schema_engine 先例，保住离线可测性。
# · 加载失败绝不抛：坏配方只记一条日志并跳过，不拖垮其它配方，也不影响通用层。
#
# ── 红线（写进 schema 校验，不是口头约定）────────────────────────────────
# 引擎**只提供**"按声明取值 / 拼 URL / 排序选优 / 旁观页面已发请求"这几类原语。schema 采用
# **白名单校验**，因此下列能力在 JSON 里**无处可写**（带了也是非法键，校验直接报错）：
#   · 无 eval / 无脚本 / 无表达式执行 —— 用户无法在配置里塞代码；
#   · 无签名计算（a_bogus / wbi / n-sig / PO token / botguard）、无解密、无验证码相关原语；
#   · 浏览器缝只**旁观页面自己发出的**请求与响应体，绝不代页面构造请求；
#   · 商业 DRM（Widevine/PlayReady/FairPlay）一律只识别、跳过、注记，绝不解密。
#
# ── 配方结构总览（每一层都有白名单，未知键 = 校验错误）───────────────────
# {
#   "schema_version": 1,
#   "name": "bilibili",                  # 配方名（缺省=文件名主干）
#   "desc": "…",                          # 说明（给人看）
#   "enabled": true,
#   "order": 100,                         # 同后缀时的注册次序（小者先）
#   "host_suffix": ["bilibili.com"],      # 命中后缀（子域自动命中，最长后缀优先）
#   "short_hosts": ["b23.tv"],            # 分享短链域（先归一重定向再进配方）
#   "cookies": "session+jar",             # 默认 cookie 取值方式：none/session/jar/session+jar
#   "session": { … },                     # 可选：身份引导（UA/Referer/首访种 cookie）
#   "identity": { … },                    # URL → 变量（正则/查询参数），any_of 分组
#   "steps":   [ … ],                     # HTTP 取数步骤（可带 qn 阶梯、expect 判据）
#   "emit":    [ … ],                     # 变量 → 轨道（可 loop 分P、produce 多形态择优）
#   "browser_seam": { … }                 # 可选：浏览器辅助缝（旁观页面自己拉的 API JSON）
# }
# 完整字段手册见 媒体配方/说明.md；三个随包配方即活样例。
import json
import logging
import os
import re
import threading
import time

# media_resolve 顶层纯 stdlib，安全（site_recipes/* 同此先例）
from media_resolve import (
    MediaTrack, register_browser_seam, register_media_resolver, register_short_host,
)

_log = logging.getLogger("media_recipe")

SCHEMA_VERSION = 1
_MAX_SPEC_BYTES = 512 * 1024          # 单个配方文件上限（防手滑塞进巨大文件）
_MAX_EMIT_DEPTH = 4                   # emit 规则递归上限（防自引用）
_MAX_TRACKS_PER_RULE = 500            # 单条 emit 规则的产出上限（防分P爆炸）

# 缺失哨兵：与 None 区分（None 是合法 JSON 值，缺失是"路径没走到"）
_MISS = object()


# ================================================================
# 0. 路径取值（点分 + [n] 下标 + [] 通配）
# ================================================================
# "view.data.pages[0].cid" → ["view","data","pages",0,"cid"]
# "dash.video[].baseUrl"   → 通配，返回该层全部值的列表
# 切片段 `[a:b]` 必须排在普通下标 `[n]` 之前，否则 `[0:200]` 会被 `[0` 先吃掉半截。
# 三选一：切片 / 下标或通配 / 键名。
_TOK = re.compile(r"\[\s*(\d*)\s*:\s*(\d*)\s*\]|\[\s*(\d*)\s*\]|([^\.\[\]]+)")


def _tokens(path: str):
    out = []
    for m in _TOK.finditer(path or ""):
        if m.group(4) is not None:            # 键名
            out.append(("k", m.group(4)))
        elif m.group(3) is not None:          # `[n]` 下标 / `[]` 通配
            g = m.group(3)
            out.append(("i", int(g)) if g else ("w", None))
        else:                                 # `[a:b]` 切片（两端都可省：[:200] / [200:]）
            s = int(m.group(1)) if m.group(1) else None
            e = int(m.group(2)) if m.group(2) else None
            out.append(("s", (s, e)))
    return out


def _walk(obj, path: str):
    """按路径取值；任一段缺失返回 _MISS。通配段返回列表（可为空列表）。"""
    toks = _tokens(path)
    if not toks:
        return obj
    cur = [obj]
    for kind, val in toks:
        nxt = []
        for c in cur:
            if kind == "k":
                if isinstance(c, dict) and val in c:
                    nxt.append(c[val])
            elif isinstance(c, list):
                if kind == "w":
                    nxt.extend(c)
                elif kind == "s":
                    # 切片**摊平**元素（同通配段），这样 `a[0:2].id` 能接着按 id 取。
                    # 空切片 → nxt 仍为空 → 下方返回 _MISS，调用点据此判"这段没有"。
                    nxt.extend(c[val[0]:val[1]])
                elif 0 <= val < len(c):
                    nxt.append(c[val])
        if not nxt:
            return _MISS
        cur = nxt
    if len(cur) == 1:
        return cur[0]
    return cur


def _get(ns, path, default=None):
    """取值（缺失/None → default）。"""
    v = _walk(ns, path) if path else _MISS
    return default if v is _MISS else v


# ================================================================
# 1. 值解析（模板 / 算术 / 条件选择 / 选优）
# ================================================================
_RE_TPL = re.compile(r"\{([^{}]+)\}")


def _render(tpl: str, ns) -> str:
    """模板渲染：{a.b[0].c} → 字符串。缺失键渲染为空串。"""
    def _sub(m):
        v = _walk(ns, m.group(1))
        if v is _MISS or v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    return _RE_TPL.sub(_sub, tpl or "")


def _render_one(tpl: str, ns):
    """整串就是单个占位符时返回**原值**（保留 int/list 类型）；否则返回渲染后的字符串。"""
    m = _RE_TPL.fullmatch(tpl or "")
    if m:
        v = _walk(ns, m.group(1))
        return None if v is _MISS else v
    return _render(tpl, ns)


def _as_num(v, default=0.0) -> float:
    try:
        if isinstance(v, bool):
            return float(int(v))
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        return float(s) if s else default
    except Exception:
        return default


def _as_int(v, default=0) -> int:
    n = _as_num(v, float(default))
    try:
        return int(n)
    except Exception:
        return default


def _fmt_size(b) -> str:
    b = _as_int(b, 0)
    if b >= 1073741824:
        return "%.2fGB" % (b / 1073741824.0)
    if b >= 1048576:
        return "%.1fMB" % (b / 1048576.0)
    if b >= 1024:
        return "%.1fKB" % (b / 1024.0)
    return "%dB" % b if b else ""


def _ext_of(url: str, whitelist=None, default: str = "") -> str:
    """URL 路径扩展名（命中白名单才算，否则给 default）。查询串不参与。"""
    path = (url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    e = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    wl = [str(x).lower().lstrip(".") for x in (whitelist or [])]
    if e and (not wl or e in wl):
        return e
    return default or (e if e and not wl else "")


# ── 选优：where 过滤 + 多键排序 + take ──────────────────────────────
def _sort_value(item, rule):
    """一条排序规则 → 可比较的元组 (类型序, 数值, 字符串)。"""
    if "prefer_re" in rule:
        try:
            hit = bool(re.search(str(rule["prefer_re"]), str(_get(item, rule.get("path", ""), "") or "")))
        except Exception:
            hit = False
        return (1, 1.0 if hit else 0.0, "")
    if "mul" in rule or "add" in rule:
        args = rule.get("mul") or rule.get("add")
        vals = [_as_num(_get(item, a, 0)) for a in (args or [])]
        acc = 1.0
        for v in vals:
            acc *= v
        return (1, acc, "")
    v = _get(item, rule.get("path", ""), None)
    if v is None:
        return (0, 0.0, "")
    if isinstance(v, bool):
        return (1, float(int(v)), "")
    if isinstance(v, (int, float)):
        return (1, float(v), "")
    if isinstance(v, (list, tuple)):
        return (2, 0.0, str(len(v)))
    return (2, 0.0, str(v))


def _cmp2(a, b):
    return (a > b) - (a < b)


def _sort_items(items, rules):
    """按 rules（有序）做稳定排序；每键可 order=asc/desc。"""
    if not items or not rules:
        return list(items or [])
    import functools

    def _cmp(x, y):
        for r in rules:
            va, vb = _sort_value(x, r), _sort_value(y, r)
            c = _cmp2(va[0], vb[0]) or _cmp2(va[1], vb[1]) or _cmp2(va[2], vb[2])
            if r.get("order", "desc") == "desc":
                c = -c
            if c:
                return c
        return 0
    return sorted(items, key=functools.cmp_to_key(_cmp))


def _where_items(items, rules, ns=None):
    if not rules:
        return list(items or [])
    return [x for x in (items or []) if _cond(rules, x if isinstance(x, dict) else {}, ns)]


def _is_rule_list(lst) -> bool:
    """列表是"规则表"还是"字面量数组"：元素里出现 value/when 即判规则表。
    字面量数组（POST body 的参数数组）里不会恰好每个元素都是带 value 的 dict。"""
    if not lst:
        return False
    for x in lst:
        if not isinstance(x, dict) or ("value" not in x and "when" not in x):
            return False
    return True


# ── 统一取值入口 ────────────────────────────────────────────────────
def _resolve(sel, ns):
    """把配方里的一段"值声明"求值。支持：字符串模板 / 规则表 / 各类 op 字典。"""
    if sel is None:
        return None
    if isinstance(sel, bool):
        return sel
    if isinstance(sel, (int, float)):
        return sel
    if isinstance(sel, str):
        return _render_one(sel, ns)
    if isinstance(sel, list):
        # 列表有两种含义，靠形状区分：
        #   规则表 [{"when":…,"value":…}, …] → 首个命中者胜（至少一个元素带 value/when）
        #   字面量列表 ["a", {"path":"x"}]   → 逐元素求值（POST body 的数组参数走这条）
        if _is_rule_list(sel):
            for r in sel:
                if isinstance(r, dict) and _cond(r.get("when"), ns):
                    return _resolve(r.get("value"), ns)
            return None
        return [_resolve(x, ns) for x in sel]
    if not isinstance(sel, dict):
        return None

    if "rules" in sel:
        return _resolve(sel.get("rules"), ns)
    if "const" in sel:
        return sel["const"]
    if "template" in sel:
        return _render(str(sel.get("template") or ""), ns)
    if "path" in sel and len(sel) <= 2:
        v = _walk(ns, sel["path"])
        if v is _MISS:
            return _resolve(sel.get("default"), ns) if "default" in sel else None
        if v is None and "default" in sel:
            return _resolve(sel.get("default"), ns)
        return v
    if "pick" in sel:
        return _pick(sel["pick"], ns)
    if "len" in sel:
        v = _resolve(sel["len"], ns)
        try:
            return len(v)
        except Exception:
            return 0
    if "bool" in sel:
        return bool(_cond(sel.get("bool"), ns))
    if "not" in sel and "path" not in sel:
        return not bool(_resolve(sel["not"], ns))
    if "any" in sel and "path" not in sel:
        vals = [_resolve(x, ns) for x in (sel.get("any") or [])]
        return next((v for v in vals if v not in (None, "", False, 0, [], {})), None)
    # match：从字符串里正则抽一段（codecs="avc1.640028" → avc1）。抽不到给 default。
    # 正则**只做提取**，不做任何计算/替换 —— 与 _cond 的 re 同属只读匹配。
    if "match" in sel and "path" not in sel:
        c = sel["match"] or {}
        # match.path 按约定就是**路径**（同 pick.path / 条件 path），不是值表达式：
        # 写字符串即当路径取；写 {"path":…} 这类字典则按值表达式求（两种都收）。
        pv = c.get("path")
        txt = _resolve(pv, ns) if isinstance(pv, dict) else (_walk(ns, pv) if pv else "")
        txt = "" if txt is _MISS or txt is None else str(txt)
        try:
            m = re.search(str(c.get("pattern") or ""), txt)
        except Exception:
            m = None
        if not m:
            return _resolve(c.get("default"), ns) if "default" in c else ""
        try:
            return m.group(_as_int(c.get("group"), 1))
        except Exception:
            return m.group(0)
    if "size" in sel:
        return _fmt_size(_resolve(sel["size"], ns))
    if "ext_of" in sel:
        c = sel["ext_of"] or {}
        return _ext_of(str(_resolve(c.get("url"), ns) or ""),
                       c.get("whitelist"), str(c.get("default") or ""))
    if "int" in sel or "float" in sel or "str" in sel:
        key = "int" if "int" in sel else ("float" if "float" in sel else "str")
        v = _resolve(sel[key], ns)
        if key == "int":
            return _as_int(v, 0)
        if key == "float":
            return _as_num(v, 0.0)
        return "" if v is None else str(v)
    if "div" in sel or "mul" in sel or "add" in sel or "sub" in sel:
        for op in ("div", "mul", "add", "sub"):
            if op in sel:
                args = sel[op] or []
                a = _as_num(_resolve(args[0], ns) if len(args) > 0 else 0)
                b = _as_num(_resolve(args[1], ns) if len(args) > 1 else 0)
                if op == "div":
                    return (a / b) if b else 0.0
                return a * b if op == "mul" else (a + b if op == "add" else a - b)
    if "min" in sel or "max" in sel:
        vals = [_as_num(_resolve(x, ns)) for x in (sel.get("min") or sel.get("max") or [])]
        if not vals:
            return 0.0
        return min(vals) if "min" in sel else max(vals)
    if "join" in sel:
        sep = str(sel.get("sep") or "")
        parts = [_resolve(x, ns) for x in (sel.get("join") or [])]
        return sep.join("" if p is None else str(p) for p in parts if p not in (None, ""))
    if "list" in sel:
        return [_resolve(x, ns) for x in (sel.get("list") or [])]
    # 普通对象：逐值递归（POST body / 字面量对象）。放在最后，故不会抢上面任何 op 的含义。
    # 渲染成空串的键**整个丢掉**：这样"某档客户端不需要某个字段"（如 WEB 无 androidSdkVersion）
    # 就写成同一个 body 模板 + 引用该档没有的变量，不必为每档抄一份 body。
    # 真要一个空串值，写 {"const": ""} 显式表达。
    out = {}
    for k, v in sel.items():
        if str(k).startswith("_"):
            continue
        rv = _resolve(v, ns)
        if rv == "":
            continue
        out[str(k)] = rv
    return out


def _pick(cfg, ns):
    """选优：定位列表 → where 过滤 → sort 排序 → take 第 N 个。
    require_len：过滤后条数不足即返回 None（"视频音频都得有才算一对"这类语义）。"""
    cfg = cfg or {}
    items = _resolve(cfg.get("list") if "list" in cfg else {"path": cfg.get("path")}, ns)
    if not isinstance(items, list):
        return None
    items = _where_items(items, cfg.get("where"), ns)
    need = cfg.get("require_len")
    if need is not None and len(items) < _as_int(need, 0):
        return None
    items = _sort_items(items, cfg.get("sort"))
    take = _as_int(cfg.get("take"), 0)
    if take < 0 or take >= len(items):
        return None
    return items[take]


# ================================================================
# 2. 条件（微表达式：只有比较，没有脚本）
# ================================================================
_CMP_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "nin",
            "contains", "contains_i", "re", "startswith", "endswith",
            "any_re", "all_re", "any_eq", "all_eq")
_FLAG_OPS = ("exists", "nonempty", "empty", "truthy", "falsy")
_LEN_OPS = {"len_eq": "eq", "len_ne": "ne", "len_gt": "gt",
            "len_ge": "ge", "len_lt": "lt", "len_le": "le"}
# 全部合法算子（含 len_* 前缀形式）。校验器拿它查拼写，见 _check_cond_tree。
_ALL_OPS = _CMP_OPS + _FLAG_OPS + tuple(_LEN_OPS)


def _truthy(v) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (str, bytes, list, tuple, dict, set)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return v != 0
    return True


def _one_cond(c, ns) -> bool:
    if not isinstance(c, dict):
        return bool(c)
    if "not" in c:
        return not _cond(c.get("not"), ns)
    if "any" in c:
        return any(_one_cond(x, ns) for x in (c.get("any") or []))
    if "all" in c:
        return all(_one_cond(x, ns) for x in (c.get("all") or []))
    op = str(c.get("op") or ("exists" if "value" not in c else "eq")).lower()
    raw = _walk(ns, c.get("path", "")) if c.get("path") else _MISS
    v = None if raw is _MISS else raw

    if op == "exists":
        return raw is not _MISS
    if op == "nonempty":
        return _truthy(v)
    if op == "empty":
        return not _truthy(v)
    if op == "truthy":
        return _truthy(v)
    if op == "falsy":
        return not _truthy(v)
    if op in _LEN_OPS:
        try:
            n = len(v)
        except Exception:
            return False
        return _compare_num(float(n), _LEN_OPS[op], _as_num(_resolve(c.get("value"), ns)))
    if op in ("gt", "ge", "lt", "le"):
        return _compare_num(_as_num(v), op, _as_num(_resolve(c.get("value"), ns)))
    want = _resolve(c.get("value"), ns)
    if op in ("eq", "ne"):
        r = (v == want)
        # 宽松数值比较：JSON 里 "64" 与 64 常被混用
        if not r and isinstance(v, (int, float, str)) and isinstance(want, (int, float, str)):
            try:
                r = (float(v) == float(want))
            except Exception:
                r = False
        return r if op == "eq" else not r
    if op in ("in", "nin"):
        try:
            r = v in (want or [])
        except Exception:
            r = False
        return r if op == "in" else not r
    if op == "contains":
        try:
            return want in v
        except Exception:
            return False
    if op == "contains_i":
        try:
            return str(want).lower() in str(v).lower()
        except Exception:
            return False
    if op == "re":
        try:
            return bool(re.search(str(want), str(v or "")))
        except Exception:
            return False
    # 列表判定：path 用了 [] 通配时值是列表（只有一个命中元素时 _walk 会退化成标量，
    # 故两种形状都得吃）。用于"一组对象里至少/每一个的某字段匹配"这类判断。
    if op in ("any_re", "all_re"):
        vals = v if isinstance(v, list) else ([v] if v is not None else [])
        vals = [x for x in vals if x is not None]
        if not vals:
            return False
        try:
            hits = [bool(re.search(str(want), str(x))) for x in vals]
        except Exception:
            return False
        return any(hits) if op == "any_re" else all(hits)
    if op in ("any_eq", "all_eq"):
        vals = v if isinstance(v, list) else ([v] if v is not None else [])
        vals = [x for x in vals if x is not None]
        if not vals:
            return False
        hits = []
        for x in vals:
            try:
                hits.append(bool(x == want) or float(x) == float(want))
            except Exception:
                hits.append(False)
        return any(hits) if op == "any_eq" else all(hits)
    if op == "startswith":
        return str(v or "").startswith(str(want))
    if op == "endswith":
        return str(v or "").endswith(str(want))
    return False


def _compare_num(a: float, op: str, b: float) -> bool:
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b,
            "eq": a == b, "ne": a != b}.get(op, False)


def _cond(conds, ns, base=None) -> bool:
    """条件列表（AND）。空/None → True（"无条件=放行"）。

    base：外层命名空间。合并成 {**base, **ns} —— **ns 在前者之上**，于是 pick/where
    里的 item 短路径（url / mimeType）优先命中，同时 value 里仍可用外层变量（{video_id}）。
    """
    if not conds:
        return True
    if base:
        merged = dict(base)
        if isinstance(ns, dict):
            merged.update(ns)
        ns = merged
    seq = conds if isinstance(conds, list) else [conds]
    return all(_one_cond(c, ns) for c in seq)


# ================================================================
# 3. 校验（白名单：未知键一律报错 —— 这也是红线在 schema 上的落点）
# ================================================================
_TOP_KEYS = frozenset((
    "schema_version", "name", "desc", "enabled", "order", "host_suffix", "short_hosts",
    "cookies", "session", "identity", "steps", "emit", "browser_seam"))
_SESSION_KEYS = frozenset(("user_agent", "referer", "headers", "bootstrap"))
_BOOT_KEYS = frozenset(("url", "method", "timeout", "cookies_from", "cookie_domain",
                        "headers", "expect"))
_IDENT_KEYS = frozenset(("any_of", "all_of", "query"))
# identity 规则：name=捕获组存进哪个变量（**必填**，缺了这条规则永远不命中且不报错）；
# pattern=正则；type=int 则转整数；from 只是写给人看的出处备注，引擎不读。
_IDENT_RULE_KEYS = frozenset(("name", "from", "pattern", "type"))
_IDENT_QUERY_KEYS = frozenset(("name", "param", "type", "default", "min", "max"))
_STEP_KEYS = frozenset((
    "name", "method", "url", "alt_url", "referer", "alt_referer", "headers", "cookies",
    "timeout", "as", "when", "expect", "ladder", "required", "json", "body", "desc",
    "parse"))
_LADDER_KEYS = frozenset(("var", "values"))
# step.parse：从**非 JSON 响应文本**（HTML/JS）里抽取内联 JSON —— SSR 站常把初始数据
# 这样嵌在页面里（酷狗 `<script>var dataFromSmarty = [{...}]`、很多视频站同款）。
# 加它的直接动因：酷狗现在的歌页是 `/mixsong/<8位slug>.html`，**不带 32 位 hash**，
# hash 只在页面里；而 `_http_get` 原本只收 JSON dict → 配方读不了 HTML → 真歌 URL 恒 0 轨。
# · re    必填，正则；有捕获组取第 1 组，没有则取整个匹配
# · json  默认 true：把捕获文本当 JSON 解析；false 则原样给字符串
# · index 可选：结果是列表时取第几个元素（酷狗就是 [{...}] 这种单元素数组）
# · unescape 默认 false：对抽出来的值做 HTML 实体解码（dict/list 递归，字符串叶子才解）。
#   从 HTML 属性 / <title> 里抠文字时要开（酷我歌页 `<title>歌名_歌手&amp;…` 不解就是 `&amp;`）；
#   抠内联 JSON 时**别开**（那里的 & 是字面量，不是转义文本）。用 video_grab._safe_unescape，
#   `&times` 这类无分号名实体绝不解 —— 否则 `&timestamp=` 会被腐蚀成 `×stamp=`。
# · from  同 identity.from —— 只是写给人看的出处备注，引擎不读
_PARSE_KEYS = frozenset(("from", "re", "json", "index", "unescape"))
_EMIT_KEYS = frozenset(("name", "when", "loop", "set", "steps", "produce", "track", "desc"))
_LOOP_KEYS = frozenset(("path", "as", "index_as", "episode_param", "max", "fallback",
                        "pause"))
_PRODUCE_KEYS = frozenset(("name", "when", "set", "track", "desc", "stop"))
_TRACK_KEYS = frozenset((
    "media_class", "kind", "url", "ext", "title_hint", "duration_s", "note",
    "cookies", "ephemeral", "parts", "drm"))
_PART_KEYS = frozenset(("media_class", "kind", "url", "ext", "note"))

# kind 归一（09-10 修）：说明.md 教用户按地址形态写 `direct / hls / dash`，但 MediaTrack.kind 的
# **内部契约只有三值**（media_resolve.py:57 `"m3u8" | "dash" | "direct"`），下载侧也只认 m3u8/dash
# 走清单分支。原来的做法是原样透传 → 用户写 `"kind": "hls"`（音频站最常见的形态）会落进**直链分支**，
# 把那份 .m3u8 **播放列表文本本身**当媒体文件下回来，全程不报错。`mpd` 同理。
# 修法 = 载入期归一（用户写 hls/mpd 都真的能用）+ 认不出来的写法**报错**（照"算子拼错""identity
# 缺 name"两个既有先例，把静默失效提前变成一条响亮报错）。
_KIND_ALIASES = {
    "": "direct", "direct": "direct", "file": "direct",
    "m3u8": "m3u8", "hls": "m3u8", "m3u": "m3u8", "hls_m3u8": "m3u8", "m3u8_url": "m3u8",
    "dash": "dash", "mpd": "dash", "dash_mpd": "dash",
}
_KIND_ALLOWED = ("direct", "hls", "mpd", "m3u8", "dash")   # 用户可写的写法（写给人看的）
_KIND_CONTRACT = ("direct", "m3u8", "dash")                # 归一后必须落进 MediaTrack 的三值契约


def _norm_kind(v):
    """配方 kind 写法 → MediaTrack 内部契约值（m3u8/dash/direct）。认不出的原样返回，由校验器报错。"""
    k = str(v or "").strip().lower()
    return _KIND_ALIASES.get(k, k)


_SEAM_KEYS = frozenset(("name", "url", "watch", "wait_sec", "emit", "identity", "enabled"))
_WATCH_KEYS = frozenset(("path_contains", "url_contains", "json_require", "validate",
                         "as", "max_bytes"))

# 红线用词：这些键名一旦出现即判定为"试图在配置里写代码/做规避"，直接拒绝并说明原因
_FORBIDDEN_KEYS = frozenset((
    "eval", "exec", "script", "js", "javascript", "code", "python", "lambda", "expr",
    "sign", "signature", "sign_url", "a_bogus", "abogus", "wbi", "nsig", "n_sig",
    "decrypt", "decode", "decipher", "key", "keys", "license", "widevine", "playready",
    "fairplay", "clearkey", "captcha", "verify_code", "token_gen", "potoken", "botguard"))


def _bad_keys(d, allowed, where):
    errs = []
    if not isinstance(d, dict):
        return ["%s 必须是对象" % where]
    for k in d:
        if str(k).startswith("_"):      # _开头=注释/自定义，引擎忽略
            continue
        if k in _FORBIDDEN_KEYS:
            errs.append("%s 含被禁键 %r —— 配置不支持签名/解密/验证码/脚本一类能力（红线）"
                        % (where, k))
        elif k not in allowed:
            errs.append("%s 含未知键 %r（允许：%s）"
                        % (where, k, "/".join(sorted(allowed))))
    return errs


def _deep_scan_forbidden(node, where, errs, depth=0):
    """递归扫禁键：任何层级出现 eval/sign/decrypt… 一律拒绝。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and not k.startswith("_") and k in _FORBIDDEN_KEYS:
                errs.append("%s 出现被禁键 %r（红线：配置不支持签名/解密/验证码/脚本）" % (where, k))
            _deep_scan_forbidden(v, where + "." + str(k), errs, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:64]):
            _deep_scan_forbidden(v, "%s[%d]" % (where, i), errs, depth + 1)


# 携带条件的键名（值 = 条件树）。只认这些名字，故不会把 POST body 里恰好叫 op 的
# 字段误判成条件算子。
_COND_KEYS = frozenset(("expect", "when", "json_require", "validate", "any", "all", "not"))


def _check_cond_tree(node, where, errs, depth=0):
    """检查一棵条件树里的算子拼写。"""
    if depth > 8 or len(errs) > 40:
        return
    if isinstance(node, list):
        for i, x in enumerate(node[:64]):
            _check_cond_tree(x, "%s[%d]" % (where, i), errs, depth + 1)
        return
    if not isinstance(node, dict):
        return
    op = node.get("op")
    if isinstance(op, str) and op.lower() not in _ALL_OPS:
        errs.append("%s 用了不认识的算子 op=%r（可用：%s）"
                    % (where, op, "/".join(_ALL_OPS)))
    for k in ("any", "all", "not"):
        if k in node:
            _check_cond_tree(node.get(k), where + "." + k, errs, depth + 1)


def _deep_check_conds(node, where, errs, depth=0):
    """在整份配方里找出条件树并逐个查算子。

    存在意义：`_one_cond` 碰到不认识的算子**静默返回 False**（=条件不成立），于是拼错一个
    算子（如把 `nonempty` 写成 `non_empty`）既不报错也不生效，只表现为"这个分支永远不命中"
    ——排查时看到的是"配方什么都没抓到"，而不是"配方写错了"。故在载入期就把拼错喊出来。
    """
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k in _COND_KEYS:
                _check_cond_tree(v, where + "." + k, errs)
            else:
                _deep_check_conds(v, where + "." + str(k), errs, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:64]):
            _deep_check_conds(v, "%s[%d]" % (where, i), errs, depth + 1)


def _validate_identity(ident) -> list:
    """校验 identity 的捕获规则与 query 表。

    必要性（实测踩到）：规则不写 `name` 时 `_identity_vars` 判 `ok=False` 直接放弃这条，
    即**整条规则静默失效**；把 `pattern` 误写成 `re` 之类更是连正则都不编译。
    两种都表现为"这个站解析不出，且毫无报错"，故在载入期喊出来。
    """
    errs = []
    for key in ("any_of", "all_of"):
        groups = ident.get(key)
        if groups is None:
            continue
        groups = [groups] if isinstance(groups, dict) else list(groups or [])
        for gi, grp in enumerate(groups):
            rules = grp if isinstance(grp, list) else [grp]
            for ri, r in enumerate(rules):
                w = "identity.%s[%d]" % (key, gi) if not isinstance(grp, list) \
                    else "identity.%s[%d][%d]" % (key, gi, ri)
                errs.extend(_bad_keys(r, _IDENT_RULE_KEYS, w))
                if not isinstance(r, dict):
                    continue
                if not str(r.get("name") or "").strip():
                    errs.append("%s 缺少 name —— 捕获组要存进哪个变量（缺了这条规则永远不命中）" % w)
                if not str(r.get("pattern") or "").strip():
                    errs.append("%s 缺少 pattern —— 用来抓取的正则" % w)
                elif isinstance(r.get("pattern"), str):
                    try:
                        re.compile(r["pattern"])
                    except Exception as e:
                        errs.append("%s 的 pattern 不是合法正则：%s" % (w, e))
    for qi, q in enumerate(ident.get("query") or []):
        w = "identity.query[%d]" % qi
        errs.extend(_bad_keys(q, _IDENT_QUERY_KEYS, w))
        if isinstance(q, dict) and not (q.get("name") or q.get("param")):
            errs.append("%s 需要 name 或 param（从查询串取哪个参数、存成什么变量）" % w)
    return errs


def validate_spec(spec) -> list:
    """校验配方 → 错误字符串列表（空 = 合法）。绝不抛。"""
    errs = []
    try:
        if not isinstance(spec, dict):
            return ["配方根必须是 JSON 对象"]
        errs.extend(_bad_keys(spec, _TOP_KEYS, "配方"))
        if not str(spec.get("name") or "").strip():
            errs.append("缺少 name（配方名）")
        hs = spec.get("host_suffix")
        if isinstance(hs, str):
            hs = [hs]
        if not hs or not all(isinstance(x, str) and x.strip() for x in hs):
            errs.append("host_suffix 必须是非空字符串或字符串数组")
        sv = spec.get("schema_version", SCHEMA_VERSION)
        if _as_int(sv, 0) > SCHEMA_VERSION:
            errs.append("schema_version=%s 高于本引擎支持的 %d，请升级程序" % (sv, SCHEMA_VERSION))
        if str(spec.get("cookies") or "jar").lower() not in ("none", "session", "jar", "session+jar"):
            errs.append("cookies 只能是 none/session/jar/session+jar")

        sess = spec.get("session")
        if sess is not None:
            errs.extend(_bad_keys(sess, _SESSION_KEYS, "session"))
            if isinstance(sess, dict) and sess.get("bootstrap") is not None:
                errs.extend(_bad_keys(sess.get("bootstrap"), _BOOT_KEYS, "session.bootstrap"))

        ident = spec.get("identity")
        if ident is not None:
            errs.extend(_bad_keys(ident, _IDENT_KEYS, "identity"))
            if isinstance(ident, dict) and not (ident.get("any_of") or ident.get("all_of")):
                errs.append("identity 至少要有 any_of 或 all_of")
            if isinstance(ident, dict):
                errs.extend(_validate_identity(ident))

        for i, st in enumerate(spec.get("steps") or []):
            w = "steps[%d]" % i
            errs.extend(_bad_keys(st, _STEP_KEYS, w))
            if isinstance(st, dict):
                if not st.get("url"):
                    errs.append("%s 缺少 url" % w)
                if st.get("ladder") is not None:
                    errs.extend(_bad_keys(st.get("ladder"), _LADDER_KEYS, w + ".ladder"))
                    lad = st.get("ladder") or {}
                    if not lad.get("var") or not isinstance(lad.get("values"), list):
                        errs.append("%s.ladder 需要 var 与 values[]" % w)
                if st.get("parse") is not None:
                    errs.extend(_bad_keys(st.get("parse"), _PARSE_KEYS, w + ".parse"))
                    ps = st.get("parse") or {}
                    if not ps.get("re"):
                        errs.append("%s.parse 缺少 re（从响应文本里抽内联 JSON 的正则，"
                                    "建议带一个捕获组）" % w)
                    if ps.get("unescape") is not None and not isinstance(ps.get("unescape"), bool):
                        # 写成 "true"/1 都会被真值判断当开 —— 与其猜，不如载入期说清楚
                        errs.append("%s.parse.unescape 必须是 true / false（要不要做 HTML 实体解码）" % w)
                    if str(st.get("method") or "GET").upper() == "POST":
                        # _http_post 没有 raw 通道；与其静默不生效，不如载入期说清楚
                        errs.append("%s.parse 只支持 GET（parse 要的是响应原文，"
                                    "POST 目前没有 raw 通道）" % w)
                for k in ("alt_url", "alt_referer"):
                    v = st.get(k)
                    if v is None:
                        continue
                    # 裸字符串收成单元素（与 url/referer 同约定）；但列表里混进非字符串要报错 ——
                    # 那是最难查的一类：`list("http://…")` 会变成一堆单字符，第一个 'h' 恰好
                    # "占位符齐备"从而被当成候选地址发出去（真机就是这么表现成 0 轨的）。
                    if isinstance(v, str):
                        continue
                    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                        errs.append("%s.%s 必须是字符串或字符串数组" % (w, k))

        emit = spec.get("emit") or []
        if not emit and not spec.get("browser_seam"):
            errs.append("至少要有一条 emit（或 browser_seam）")
        for i, e in enumerate(emit):
            errs.extend(_validate_emit(e, "emit[%d]" % i))

        seam = spec.get("browser_seam")
        if seam is not None:
            errs.extend(_bad_keys(seam, _SEAM_KEYS, "browser_seam"))
            if isinstance(seam, dict):
                if not seam.get("url"):
                    errs.append("browser_seam 缺少 url")
                errs.extend(_bad_keys(seam.get("watch"), _WATCH_KEYS, "browser_seam.watch"))
                for i, e in enumerate(seam.get("emit") or []):
                    errs.extend(_validate_emit(e, "browser_seam.emit[%d]" % i))

        _deep_scan_forbidden(spec, "配方", errs)
        _deep_check_conds(spec, "配方", errs)
    except Exception as e:                      # 校验器自身绝不抛
        errs.append("校验异常：%s" % e)
    return errs


def _check_kind(tr, where, errs):
    """track / track.parts 的 kind 别名归一后必须落进三值契约，否则就是笔误 —— 载入期喊出来。"""
    if not isinstance(tr, dict):
        return
    nodes = [(where, tr)] + [("%s.parts[%d]" % (where, j), pt)
                             for j, pt in enumerate(tr.get("parts") or [])]
    for w, node in nodes:
        if not isinstance(node, dict):
            continue
        k = node.get("kind")
        if k is None:
            continue
        if _norm_kind(k) not in _KIND_CONTRACT:
            errs.append("%s.kind 写的是 %r，引擎不认识（可用：%s；留空 = direct）"
                        % (w, k, " / ".join(_KIND_ALLOWED)))


def _validate_emit(e, where):
    errs = _bad_keys(e, _EMIT_KEYS, where)
    if not isinstance(e, dict):
        return errs
    if not (e.get("track") or e.get("produce")):
        errs.append("%s 需要 track 或 produce" % where)
    if e.get("loop") is not None:
        errs.extend(_bad_keys(e.get("loop"), _LOOP_KEYS, where + ".loop"))
        if isinstance(e.get("loop"), dict) and not e["loop"].get("path"):
            errs.append("%s.loop 缺少 path" % where)
        lp = e["loop"].get("pause") if isinstance(e.get("loop"), dict) else None
        if lp is not None and not isinstance(lp, (int, float)):
            errs.append("%s.loop.pause 必须是数字（每项之间等的秒数；不写=不等）" % where)
    for i, st in enumerate(e.get("steps") or []):
        errs.extend(_bad_keys(st, _STEP_KEYS, "%s.steps[%d]" % (where, i)))
        if isinstance(st, dict) and not st.get("url"):
            errs.append("%s.steps[%d] 缺少 url" % (where, i))
    for i, p in enumerate(e.get("produce") or []):
        w = "%s.produce[%d]" % (where, i)
        errs.extend(_bad_keys(p, _PRODUCE_KEYS, w))
        if isinstance(p, dict) and not p.get("track"):
            errs.append("%s 需要 track" % w)
        if isinstance(p, dict) and isinstance(p.get("track"), dict):
            errs.extend(_bad_keys(p["track"], _TRACK_KEYS, w + ".track"))
            _check_kind(p["track"], w + ".track", errs)
    if isinstance(e.get("track"), dict):
        errs.extend(_bad_keys(e["track"], _TRACK_KEYS, where + ".track"))
        _check_kind(e["track"], where + ".track", errs)
        for j, pt in enumerate(e["track"].get("parts") or []):
            errs.extend(_bad_keys(pt, _PART_KEYS, "%s.track.parts[%d]" % (where, j)))
    return errs


# ================================================================
# 4. HTTP 取数（requests 懒加载；测试可注入同签名钩子）
# ================================================================
class _Session:
    """配方级会话：UA/Referer + 首访引导种 cookie（bilibili 匿名指纹即此类）。
    引导只跑一次，且只在配方真正要发请求时跑 —— 不含引导的配方零额外往返。"""

    def __init__(self, cfg, log=None):
        self.cfg = cfg or {}
        self.log = log
        self._s = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._s is None:
                self._s = self._build()
            return self._s

    def _build(self):
        import requests
        s = requests.Session()
        ua = self.cfg.get("user_agent")
        if ua:
            s.headers["User-Agent"] = str(ua)
        ref = self.cfg.get("referer")
        if ref:
            s.headers["Referer"] = str(ref)
        for k, v in (self.cfg.get("headers") or {}).items():
            s.headers[str(k)] = str(v)
        boot = self.cfg.get("bootstrap") or {}
        if boot.get("url"):
            try:
                r = s.request(str(boot.get("method") or "GET").upper(), str(boot["url"]),
                              timeout=_as_int(boot.get("timeout"), 8))
                j = r.json()
                data = j
                from_ = boot.get("cookies_from")
                if from_:
                    data = _walk(j, str(from_))
                    if data is _MISS:
                        data = {}
                dom = str(boot.get("cookie_domain") or "")
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, str):
                            s.cookies.set(k, v, domain=dom or None)
            except Exception as e:
                _i(self.log, "会话引导失败（不影响主流程）：%s", e)
        return s


def _i(log, fmt, *a):
    if not log:
        return
    try:
        log(fmt % a if a else fmt)
    except Exception:
        pass


def _jar_cookies(url):
    """共享 cookie jar 里同域 cookie（浏览器轮收割的登录态）。失败恒 {}。"""
    try:
        import video_grab as _vg
        return dict(_vg._jar_cookies_for(url) or {})
    except Exception:
        return {}


def _resolve_cookies(mode, url, session, log=None):
    """按 cookies 模式取本次请求要带的 cookie。"""
    mode = (mode or "jar").lower()
    out = {}
    if mode in ("session", "session+jar"):
        try:
            s = session.get()
            out.update({c.name: c.value for c in s.cookies})
        except Exception:
            pass
    if mode in ("jar", "session+jar"):
        out.update(_jar_cookies(url))
    return out


def _cookies_for(mode, ns, url, session, log=None):
    """本次请求/本条轨道该带什么 cookie。

    未声明（None）→ 直接用配方已经解析好的 ns["cookies"]（**零额外往返**：避免每条轨
    再触发一次会话引导或 jar 检索）；字符串 → none/session/jar/session+jar；对象 → 字面量（值可渲染）。
    """
    if mode is None:
        return dict(ns.get("cookies") or {})
    if isinstance(mode, dict):
        return {str(k): _resolve(v, ns) for k, v in mode.items()}
    return _resolve_cookies(str(mode), url, session, log)


def _http_get(url, *, headers=None, cookies=None, referer=None, timeout=15,
              session=None, transport=None, raw=False):
    """GET → dict。transport 为注入钩子（签名 (url, cookies=, referer=)，供离线 canned 测试）。

    raw=True 时不解析 JSON，直接返回响应原文（str）—— 给 step.parse 抽 HTML/JS 里的内联 JSON 用。
    raw 下 transport 钩子返回什么就原样给什么（canned 测试直接喂 HTML 字符串）。
    """
    if transport is not None:
        j = transport(url, cookies=cookies or None, referer=referer)
    else:
        s = session.get() if session is not None else None
        if s is None:
            import requests
            s = requests
        h = dict(headers or {})
        if referer:
            h["Referer"] = referer
        r = s.get(url, headers=h or None, cookies=cookies or None, timeout=timeout)
        r.raise_for_status()
        j = r.text if raw else r.json()
    if raw:
        # 不走 json.loads 兜底：parse 要的就是"原文"，在这里把 HTML 解析失败吞掉
        # 会让后面的正则永远拿不到东西，且看起来像"站变了"。
        return j if isinstance(j, str) else None
    if isinstance(j, str):
        try:
            j = json.loads(j)
        except Exception:
            return None
    return j if isinstance(j, dict) else None


def _safe_unescape(s):
    """HTML 实体解码 —— **借 video_grab 那一份，不另写**。

    那条规则很细（只解带分号或无分号的 6 个 HTML4 名实体 + 数字引用，**绝不解 `&times`**
    —— 否则 `&timestamp=` 会被腐蚀成 `×stamp=`），是这个项目踩出来的，复制一份就多一个会走样的地方。
    """
    try:
        from video_grab import _safe_unescape as f
    except Exception:
        return s                      # 借不到就原样返回：宁可不解码，也不做半吊子解码
    try:
        return f(s)
    except Exception:
        return s


def _unescape_deep(v):
    """字符串做实体解码；dict/list 递归（**不留"写在内层就静默不生效"的坑**）；其它原样。"""
    if isinstance(v, str):
        return _safe_unescape(v)
    if isinstance(v, list):
        return [_unescape_deep(x) for x in v]
    if isinstance(v, dict):
        return {k: _unescape_deep(x) for k, x in v.items()}
    return v


def _apply_parse(spec, text):
    """从响应原文里抽内联 JSON（SSR 站把初始数据嵌在 <script> 里的那种）。

    任何一步失败都返回 None（绝不抛）—— 抽取失败由调用方当成"这一步没拿到"，
    与请求失败同路处理（走阶梯下一档 / 标 required 失败），语义一致。

    `unescape`（默认 false）：对抽出来的值做 HTML 实体解码。**默认关**是故意的 ——
    内联 JSON 里的 `&` 不是转义文本，只有从 HTML 属性/<title> 里抠出来的才需要解；
    让配方作者自己说要不要解，比替他猜干净。写错类型载入期就报错（见 _validate_parse）。
    """
    if not isinstance(spec, dict) or not isinstance(text, str):
        return None
    try:
        m = re.search(str(spec.get("re") or ""), text, re.S)
    except Exception:
        return None
    if not m:
        return None
    cap = m.group(1) if m.groups() else m.group(0)
    if spec.get("json", True):
        try:
            cap = json.loads(cap)
        except Exception:
            return None
    idx = spec.get("index")
    if idx is not None and isinstance(cap, list):
        try:
            cap = cap[int(idx)]
        except Exception:
            return None
    if spec.get("unescape"):
        cap = _unescape_deep(cap)
    return cap


def _http_post(url, *, headers=None, cookies=None, timeout=15, json_body=None,
               session=None, transport=None):
    """POST JSON → dict。transport 注入钩子签名 (url, payload, headers)。"""
    if transport is not None:
        j = transport(url, json_body, headers or {})
    else:
        s = session.get() if session is not None else None
        if s is None:
            import requests
            s = requests
        r = s.post(url, json=json_body, headers=headers or None,
                   cookies=cookies or None, timeout=timeout)
        r.raise_for_status()
        j = r.json()
    if isinstance(j, str):
        try:
            j = json.loads(j)
        except Exception:
            return None
    return j if isinstance(j, dict) else None


# ================================================================
# 5. 步骤执行（含 qn 阶梯）
# ================================================================
def _tpl_list(v):
    """把 url / alt_url / referer / alt_referer 收成"模板列表"。

    裸字符串 = 一个模板（与配方里其他地方同约定）。**绝不能直接 `list(v)`** ——
    那会把字符串拆成一堆单字符，而第一个字符（如 'h'）没有占位符、恰好"齐备"，
    会被 _first_renderable 当成合法地址发出去，症状是"请求发到一个叫 h 的地方"。
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str)]
    return []


def _first_renderable(cands, ns):
    """从候选模板里取第一个"占位符都齐备"的（bilibili 的 bvid/aid 二选一即此类）。"""
    for t in (cands or []):
        if not isinstance(t, str):
            continue
        ok = True
        for m in _RE_TPL.finditer(t):
            v = _walk(ns, m.group(1))
            if v is _MISS or v is None or v == "":
                ok = False
                break
        if ok:
            return _render(t, ns)
    return None


def _run_step(st, ns, *, session, log=None, get_transport=None, post_transport=None):
    """执行一个 HTTP 步骤：把响应存进 ns[st.as]。返回 True=成功（expect 通过）。绝不抛。"""
    if not _cond(st.get("when"), ns):
        return False
    as_ = st.get("as") or st.get("name") or "_step"
    method = str(st.get("method") or "GET").upper()
    timeout = _as_int(st.get("timeout"), 15)

    lad = st.get("ladder") or {}
    values = list(lad.get("values") or [None]) if lad.get("var") else [None]
    if not values:
        values = [None]
    # 阶梯变量先落首档：URL 模板里引用 {qn} 才算"可渲染"（否则 _first_renderable 会误判模板不全）
    if lad.get("var") and values[0] is not None:
        ns.setdefault(str(lad["var"]), values[0])

    urls = _tpl_list(st.get("url")) + _tpl_list(st.get("alt_url"))
    url = _first_renderable(urls, ns)
    if not url:
        _i(log, "步骤 %s 跳过：URL 模板变量不全", as_)
        return False
    ref = _first_renderable(_tpl_list(st.get("referer")) + _tpl_list(st.get("alt_referer")), ns)
    body_sel = st.get("json") if "json" in st else st.get("body")
    mode = st.get("cookies")
    pspec = st.get("parse") if isinstance(st.get("parse"), dict) else None

    for val in values:
        if lad.get("var"):
            ns[str(lad["var"])] = val
        u = _render(url, ns)
        # 头与体在**每档内**求值：阶梯值可以是个对象（如 {客户端的 clientVersion/ua}），
        # 于是同一个 POST 能按档换 body 里嵌的客户端身份。渲染后为空的头直接丢弃
        # （"本档不需要 UA" 靠写空串表达，而不是塞一个空值把会话默认 UA 顶掉）。
        hdr = {}
        for k, v in (st.get("headers") or {}).items():
            sv = _render(str(v), ns)
            if sv != "":
                hdr[str(k)] = sv
        body = _resolve(body_sel, ns)
        cookies = _cookies_for(mode, ns, u, session, log)
        try:
            if method == "POST":
                j = _http_post(u, headers=hdr, cookies=cookies, timeout=timeout,
                               json_body=body, session=session, transport=post_transport)
            else:
                j = _http_get(u, headers=hdr, cookies=cookies, referer=ref,
                              timeout=timeout, session=session, transport=get_transport,
                              raw=bool(pspec))
        except Exception as e:
            _i(log, "步骤 %s 请求失败：%s", as_, e)
            j = None
        if pspec is not None:
            # raw 文本 → 抽内联 JSON。抽不出来就当这一步没拿到（与请求失败同路）。
            if isinstance(j, str):
                j = _apply_parse(pspec, j)
                if j is None:
                    _i(log, "步骤 %s：parse 未能从响应里抽出数据", as_)
            else:
                j = None
        if j is None and get_transport is None and post_transport is None:
            continue
        ns[as_] = j if j is not None else {}
        # expect 既可写响应根的短路径（code / data.durl[0].url —— 最常见的写法），
        # 也可写带 as 名的全路径；故在"命名空间 + 本次响应"的合并视图上判。
        probe = dict(ns)
        if isinstance(j, dict):
            probe.update(j)
        if _cond(st.get("expect"), probe):
            ns.setdefault("cookies", {})
            return True
    ns.setdefault(as_, {})
    if st.get("required"):
        ns["_failed"] = True
    return False


def _run_steps(steps, ns, *, session, log=None, get_transport=None, post_transport=None):
    """按序执行步骤；任一 required 步骤失败 → 返回 False（该 emit 规则放弃）。"""
    for st in (steps or []):
        _run_step(st, ns, session=session, log=log,
                  get_transport=get_transport, post_transport=post_transport)
        if ns.get("_failed"):
            return False
    return True


# ================================================================
# 6. 轨道产出
# ================================================================
def _build_track(tr, ns, spec, url, session, log=None):
    """track 声明 → MediaTrack。url 为空 → None（该 produce 规则不出轨）。"""
    u = _resolve(tr.get("url"), ns)
    u = "" if u is None else str(u).strip()
    if not u:
        return None
    parts = []
    for pt in (tr.get("parts") or []):
        pu = _resolve(pt.get("url"), ns)
        if pu:
            parts.append({
                "media_class": str(_resolve(pt.get("media_class"), ns) or ""),
                "kind": _norm_kind(_resolve(pt.get("kind"), ns)),
                "url": str(pu),
                "ext": str(_resolve(pt.get("ext"), ns) or ""),
                "note": str(_resolve(pt.get("note"), ns) or ""),
            })
    ck = _cookies_for(tr.get("cookies"), ns, u, session, log)
    title = _resolve(tr.get("title_hint"), ns)
    note = _resolve(tr.get("note"), ns)
    dur = _resolve(tr.get("duration_s"), ns)
    return MediaTrack(
        index=0,
        media_class=str(_resolve(tr.get("media_class"), ns) or ""),
        kind=_norm_kind(_resolve(tr.get("kind"), ns)),
        url=u,
        ext=str(_resolve(tr.get("ext"), ns) or ""),
        src="recipe:" + str(spec.get("name") or "?"),
        page_url=url,
        title_hint="" if title is None else str(title),
        duration_s=_as_num(dur, 0.0),
        cookies=ck,
        drm=list(tr.get("drm") or []),
        note="" if note is None else str(note),
        parts=parts,
        ephemeral=bool(_resolve(tr.get("ephemeral"), ns)),
    )


def _produce_tracks(rule, ns, spec, url, session, log=None, depth=0):
    """一条 produce 规则 → [MediaTrack]（0 或 1）。"""
    if depth > _MAX_EMIT_DEPTH:
        return []
    if not _cond(rule.get("when"), ns):
        return []
    local = dict(ns)
    for k, sel in (rule.get("set") or {}).items():
        if str(k).startswith("_"):
            continue
        local[str(k)] = _resolve(sel, local)
    if local.get("_failed"):
        return []
    tr = _build_track(rule.get("track") or {}, local, spec, url, session, log)
    return [tr] if tr else []


def _apply_set(bindings, ns):
    for k, sel in (bindings or {}).items():
        if str(k).startswith("_"):
            continue
        ns[str(k)] = _resolve(sel, ns)


def _emit_rule(e, ns, spec, url, session, log=None, depth=0,
               get_transport=None, post_transport=None):
    """执行一条 emit 规则 → [MediaTrack]。loop 存在则按分集展开。"""
    if depth > _MAX_EMIT_DEPTH or not _cond(e.get("when"), ns):
        return []
    loop = e.get("loop")
    if not loop:
        local = dict(ns)
        _apply_set(e.get("set"), local)          # 前置：供 steps 拼 URL 用
        if not _run_steps(e.get("steps"), local, session=session, log=log,
                          get_transport=get_transport, post_transport=post_transport):
            return []
        _apply_set(e.get("set"), local)          # 后置：供引用步骤结果的绑定刷新
        return _emit_tracks(e, local, spec, url, session, log, depth)

    items = _resolve({"path": loop.get("path")}, ns)
    if not isinstance(items, list) or not items:
        # fallback：分集列表缺失时的字面量兜底（如 B站 view 未回 pages → 用 data.cid 单集）
        items = [it for it in (loop.get("fallback") or []) if isinstance(it, dict)]
        items = [{k: _render(str(v), ns) if isinstance(v, str) else v
                  for k, v in it.items()} for it in items]
    if not items:
        return []
    items = items[:max(1, _as_int(loop.get("max"), _MAX_TRACKS_PER_RULE))]
    n = len(items)
    pname = loop.get("episode_param")
    p = _as_int(_get(ns, pname, 0), 0) if pname else 0
    # 分集语义（通用）：带 ?p=N 且 N>1 → 只该集；否则多集全给、单集只给一个
    if p > 1 and n > 1:
        chosen = [(min(p, n) - 1, min(p, n))]
    elif n > 1:
        chosen = [(i, i + 1) for i in range(n)]
    else:
        chosen = [(0, 1)]

    # pause：每项之间的间隔秒数（0/不写 = 不等）。给"逐个问接口"的配方用 ——
    # 有些站按 IP 限速（发太密会返 200+空数据，症状像"这站挂了"），配方作者写个数字即可放慢。
    # 只在**项与项之间**等：第一项之前、最后一项之后都不等，多集配方不受额外影响。
    pause = max(0.0, _as_num(loop.get("pause"), 0.0))

    out = []
    for seq, (i, idx) in enumerate(chosen):
        if seq and pause:
            time.sleep(pause)
        local = dict(ns)
        local[str(loop.get("as") or "item")] = items[i]
        local[str(loop.get("index_as") or "idx")] = idx
        local["picked"] = items[i]
        local["count"] = n
        _apply_set(e.get("set"), local)          # 前置：供 steps 拼 URL 用
        if not _run_steps(e.get("steps"), local, session=session, log=log,
                          get_transport=get_transport, post_transport=post_transport):
            continue
        _apply_set(e.get("set"), local)          # 后置：供引用步骤结果的绑定刷新
        out.extend(_emit_tracks(e, local, spec, url, session, log, depth))
    return out


def _emit_tracks(e, local, spec, url, session, log, depth):
    if e.get("track"):
        return _produce_tracks(e, local, spec, url, session, log, depth)
    out = []
    for pr in (e.get("produce") or []):
        got = _produce_tracks(pr, local, spec, url, session, log, depth)
        out.extend(got)
        # "stop": true —— 命中即停。用于**互斥的形态**：adaptive 分轨拿到了就别再退渐进混流，
        # 否则同一个视频会出两条轨（同一资源两种封装）。默认不写=继续试后面的 produce。
        if got and pr.get("stop"):
            break
    return out


# ================================================================
# 7. 主入口
# ================================================================
def _identity_vars(spec, url, cookies=None):
    """URL → 变量命名空间（含 identity 捕获 + query 参数 + cookies）。不合形 → None。"""
    from urllib.parse import parse_qs, urlparse
    ident = spec.get("identity") or {}
    ns = {}
    groups = ident.get("any_of")
    if groups is None:
        groups = [ident.get("all_of") or []]
    if isinstance(groups, dict):
        groups = [groups]
    hit = False
    for grp in (groups or []):
        rules = grp if isinstance(grp, list) else [grp]
        got, ok = {}, True
        for r in rules:
            if not isinstance(r, dict):
                ok = False
                break
            try:
                m = re.search(str(r.get("pattern") or ""), url or "")
            except Exception:
                m = None
            if not m:
                ok = False
                break
            name = str(r.get("name") or "")
            if not name:
                ok = False
                break
            val = m.group(1) if m.groups() else m.group(0)
            if str(r.get("type") or "").lower() == "int":
                val = _as_int(val, 0)
            got[name] = val
        if ok and got:
            ns.update(got)
            hit = True
            break
    if not hit and (ident.get("any_of") or ident.get("all_of")):
        return None
    for q in (ident.get("query") or []):
        if not isinstance(q, dict):
            continue
        try:
            raw = parse_qs(urlparse(url or "").query).get(str(q.get("param") or ""), [])
        except Exception:
            raw = []
        val = raw[0] if raw else q.get("default")
        if str(q.get("type") or "").lower() == "int":
            val = _as_int(val, _as_int(q.get("default"), 0))
            lo, hi = q.get("min"), q.get("max")
            if lo is not None:
                val = max(_as_int(lo, 0), val)
            if hi is not None:
                val = min(_as_int(hi, 0), val)
        if val is not None:
            ns[str(q.get("name") or q.get("param"))] = val
    ns["cookies"] = dict(cookies or {})
    ns["_url"] = url
    return ns


def tracks_from_spec(spec, url, *, http_get_json=None, http_post_json=None,
                     cookies=None, log=None):
    """按 JSON 配方解析 URL → [MediaTrack, …]。失败一律 []，绝不 raise。

    http_get_json(url, cookies=, referer=) / http_post_json(url, payload, headers)
      离线测试注入钩子（默认真网）。形参与 site_recipes 时代一致，既有 canned 夹具可平移。
    cookies：外部给定的 cookie（None → 由配方的 cookies 模式自行取：会话引导 + 共享 jar）。
    """
    try:
        if not isinstance(spec, dict):
            return []
        errs = validate_spec(spec)
        if errs:
            _i(log, "配方 %s 校验未通过：%s", spec.get("name") or "?", errs[0])
            return []

        if cookies is None:
            session = _Session(spec.get("session"), log)
            cookies = _resolve_cookies(spec.get("cookies") or "jar", url, session, log)
        else:
            session = _Session(spec.get("session"), log)

        ns = _identity_vars(spec, url, cookies)
        if ns is None:
            return []
        ns["cookies"] = dict(cookies or {})

        _run_steps(spec.get("steps"), ns, session=session, log=log,
                   get_transport=http_get_json, post_transport=http_post_json)
        if ns.get("_failed"):
            return []

        out = []
        for e in (spec.get("emit") or []):
            out.extend(_emit_rule(e, ns, spec, url, session, log,
                                  get_transport=http_get_json,
                                  post_transport=http_post_json))
        keep = [t for t in out if t and t.url][:_MAX_TRACKS_PER_RULE]
        # 这里**不**按 URL 去重：分P/分集天然可能指向同一 URL（镜像线路、同一源的多个稿件），
        # 在配方层并掉会丢集数。去重是 resolve_page 的事（dedupe_tracks），配方针只负责如实产出。
        for i, t in enumerate(keep, 1):
            t.index = i
        return keep
    except Exception as e:
        _log.warning("配方 %s 解析异常：%s", (spec or {}).get("name"), e)
        return []


def make_resolver(spec, owner=""):
    """配方 → register_media_resolver 用的 resolve_fn(url)。

    owner：**来源限定**的属主键（"自带:bilibili" / "用户:bilibili"），不是配方名。见
    `recipe_owner()` —— 用配方名当属主时，自带与用户各有一份同名配方就会互相撤掉对方。
    """
    def _resolve(url):
        try:
            return tracks_from_spec(spec, url)
        except Exception:
            return []
    # 盖章：注册表里的这个函数是不是"本引擎产出的"（_spec_alive 体检用；也给排查留痕）
    _resolve._recipe_owner = owner or str(spec.get("name") or "")
    return _resolve


# ================================================================
# 8. 浏览器辅助缝（只旁观页面自己已发出的请求；复用 media_capture 的 CDP 管道）
# ================================================================
def _seam_watcher_class():
    """延迟取 media_capture._Watcher 做基类 —— 未装 websocket 的环境不该 import 失败。"""
    from media_capture import _Watcher

    class _SeamWatcher(_Watcher):
        """定制旁观器：只认目标 API 响应体，拿到即收工。事件管道/worker 补挂全部继承。"""

        def __init__(self, cfg, **kw):
            self.probe_ns = kw.pop("probe_ns", None) or {}
            super().__init__(**kw)
            self.cfg = cfg or {}
            self.captured = None
            self.matched = 0

        def _probe(self, obj):
            """校验/判据用的合并视图：命名空间 + 响应体（响应键在上，路径可写短名）。"""
            p = dict(self.probe_ns)
            if isinstance(obj, dict):
                p.update(obj)
            return p

        def _hit(self, u):
            u = u or ""
            for pat in (self.cfg.get("path_contains") or []):
                if str(pat) and str(pat) in u:
                    break
            else:
                if self.cfg.get("path_contains"):
                    return False
            for pat in (self.cfg.get("url_contains") or []):
                if str(pat) and str(pat) in u:
                    return True
            return not self.cfg.get("url_contains")

        def _add(self, c):          # 本缝不做通用发现：候选集合恒空
            return

        def _readable(self, rec):
            if self.captured is not None:
                return False
            if not self._hit(rec.get("url")):
                return False
            maxb = _as_int(self.cfg.get("max_bytes"), self.body_max_bytes)
            return not (rec.get("enc_len") and rec["enc_len"] > maxb)

        def _consume_body(self, rec, txt):
            if self.captured is not None:
                return
            try:
                obj = json.loads(txt)
            except Exception:
                return
            if not isinstance(obj, dict):
                return
            self.matched += 1
            if not _cond(self.cfg.get("json_require"), self._probe(obj)):
                return
            self.captured = obj
            self._i("旁观到目标 API 响应体（%d 字节）", len(txt))

        def wants_more(self):
            if self.captured is not None:
                return False
            return super().wants_more()

        def subscribe(self):
            super().subscribe()
            # Runtime.enable 已有；这里补 JSON 体所需的缓冲上限
            self._cmd("Network.setCacheDisabled", {"cacheDisabled": True})

    return _SeamWatcher


def _run_browser_seam(seam, spec, url, *, log=None, stop_flag=None):
    """浏览器辅助缝：开一次页面 → 旁观它自己拉的 API JSON → 按 emit 出轨。绝不 raise。"""
    import time as _time
    try:
        import media_capture as mc
    except Exception as e:
        _i(log, "浏览器缝不可用（media_capture 载入失败）：%s", e)
        return []

    ns = _identity_vars(spec, url, {})
    if ns is None:
        ns = {}
    extra = seam.get("identity")
    if isinstance(extra, dict):
        sub = _identity_vars({"identity": extra}, url, {})
        if sub:
            ns.update(sub)
    ns["cookies"] = {}

    target = _first_renderable([seam.get("url")], ns)
    if not target:
        _i(log, "浏览器缝跳过：目标 URL 模板变量不全")
        return []

    st = mc.ensure_shared_chrome(target, log=log)
    if st in ("denied", "failed"):
        _i(log, "浏览器缝跳过：共享 Chrome 不可用（%s）", st)
        return []
    tab = mc._cdp_open_tab(target)
    if not tab:
        _i(log, "浏览器缝跳过：无法在共享 Chrome 开新 tab")
        return []
    tab_id = tab.get("id")
    ws = mc._ws_connect(tab.get("webSocketDebuggerUrl") or "")
    if ws is None:
        mc._cdp_close_tab(tab_id)
        _i(log, "浏览器缝跳过：CDP WebSocket 连不上")
        return []

    watch = dict(seam.get("watch") or {})
    for key in ("path_contains", "url_contains"):
        watch[key] = [_render(str(x), ns) for x in (watch.get(key) or [])]
    for key in ("json_require", "validate"):
        if watch.get(key):
            watch[key] = [{**c, "value": _render(str(c["value"]), ns) if "value" in c else None}
                          for c in watch[key] if isinstance(c, dict)]

    session = _Session(spec.get("session"), log)
    pump = mc._Pump(ws, log=log)
    w = _seam_watcher_class()(
        watch, probe_ns=ns, page_url=target, want="both", send=pump.send, now=_time.time,
        wait_sec=_as_num(seam.get("wait_sec"), mc.D_WAIT_SEC), idle_grace=1e9, log=log)
    pump.watcher = w
    try:
        w.subscribe()
        pump.send("Page.navigate", {"url": target})
        t0 = _time.time()
        rounds = 0
        while w.wants_more():
            if stop_flag is not None:
                try:
                    if stop_flag():
                        break
                except Exception:
                    pass
            while rounds < len(mc._TRIGGER_AT) and (_time.time() - t0) >= mc._TRIGGER_AT[rounds]:
                mc._dispatch_trigger(pump, rounds, log=log)
                w.mark_trigger()
                rounds += 1
            pump.poll(0.25)
            w.drain()
        w.drain()
    except Exception as e:
        _i(log, "浏览器缝旁观异常：%s", e)
    finally:
        pump.close()
        mc._cdp_close_tab(tab_id)

    detail = w.captured
    if not isinstance(detail, dict):
        _i(log, "浏览器缝未旁观到目标 API 响应体（页面请求命中 %d 次）", w.matched)
        return []
    probe = dict(ns)
    probe.update(detail)
    if not _cond(watch.get("validate"), probe):
        _i(log, "浏览器缝：响应体校验未通过")
        return []
    if watch.get("json_require") and not _cond(watch.get("json_require"), probe):
        _i(log, "浏览器缝：响应体缺少所需字段")
        return []
    # 响应体的顶层键直接并进命名空间（emit 里就能写 aweme_detail.video… 这样的短路径），
    # 同时以 as 名再绑一份，两种写法都通。
    for k, v in detail.items():
        if not str(k).startswith("_"):
            ns[str(k)] = v
    ns[str(watch.get("as") or "detail")] = detail

    out = []
    for e in (seam.get("emit") or []):
        out.extend(_emit_rule(e, ns, spec, url, session, log))
    keep = [t for t in out if t and t.url]
    for i, t in enumerate(keep, 1):
        t.index = i
    return keep


def make_browser_seam(spec, owner=""):
    """配方 → register_browser_seam 用的 seam_fn(url)。

    owner 同 make_resolver。多出的 log / stop_flag 形参是给"指定配方"路径直调用的：
    注册表调用方只传 url（向后兼容），强制配方时则直调并带上日志与停止位。
    """
    seam = spec.get("browser_seam") or {}

    def _seam(url, log=None, stop_flag=None):
        try:
            return _run_browser_seam(seam, spec, url, log=log, stop_flag=stop_flag)
        except Exception:
            return []
    # 盖章：注册表里的这条缝是不是"本引擎产出的"（_spec_alive 体检 + 退场时按属主撤）
    _seam._recipe_owner = owner or str(spec.get("name") or "")
    return _seam


# ================================================================
# 9. 注册
# ================================================================
def register_spec(spec, *, source="", owner="", enabled=None, log=None) -> bool:
    """校验并注册一个配方（resolver + 短链 + 可选浏览器缝）。非法配方返回 False 不注册。

    owner：来源限定的属主键（见 recipe_owner）；缺省退回配方名（旧调用方的行为）。
    enabled：None → 听配方自己的 `enabled` 字段；显式 True/False → 以调用方为准。
      存在的意义是"用户可用界面停用一份**自带**配方"——那份 json 的 enabled 字段是
      配方作者的意志，用户改不了（也不该去改：自带目录升级时会被整体覆盖），停用状态
      必须由外部状态文件覆盖进来，故这里要能被外部顶掉。
    """
    errs = validate_spec(spec)
    if errs:
        _i(log, "配方 %s 未注册（%s）：%s", (spec or {}).get("name"), source or "?", errs[0])
        _log.warning("媒体配方未注册 %s：%s", source or spec.get("name"), errs)
        return False
    on = spec.get("enabled", True) if enabled is None else bool(enabled)
    if not on:
        # 停用 = **不注册**，绝不能注册成 enabled=False：register_media_resolver 对同 host_suffix
        # 是**无条件顶替**（不看新旧条目的 enabled），注册一条停用的会把同后缀那条好用的顶掉，
        # 效果与"停用"完全相反。
        _log.info("媒体配方 %s 已停用", spec.get("name"))
        return False
    name = str(spec.get("name"))
    owner = owner or name
    order = _as_int(spec.get("order"), 100)
    hs = spec.get("host_suffix")
    hs = [hs] if isinstance(hs, str) else list(hs or [])
    for s in hs:
        register_media_resolver(str(s), make_resolver(spec, owner), name=name, order=order)
    for s in (spec.get("short_hosts") or []):
        register_short_host(str(s))
    if spec.get("browser_seam") and spec["browser_seam"].get("url"):
        for s in hs:
            register_browser_seam(str(s), make_browser_seam(spec, owner),
                                  name=str(spec["browser_seam"].get("name") or (name + "浏览器辅助")))
    _log.info("媒体配方已注册：%s（%s）", name, source or "内置")
    return True


# ================================================================
# 10. 目录加载（BASE_DIR/媒体配方/*.json + BASE_DIR/用户配方/*.json；目录缺失=空转）
#
# 两套目录的分工见 config.MEDIA_RECIPE_* 注释。本节的要害只有两条：
#   · **扫描次序 = 注册次序**：自带全部先、用户全部后 → 后注册者赢 = 用户配置优先。
#   · **停用 = 不注册**（不是注册成 enabled=False）：注册表对同 host_suffix 是无条件顶替，
#     注册一条停用的会把同后缀那条好用的顶掉，效果与"停用"完全相反。
# ================================================================
SRC_BUILTIN = "自带"
SRC_USER = "用户"

_load_lock = threading.Lock()
_loaded = {}                # path → mtime（另含状态文件的伪键）
_live = {}                  # path → (spec, owner)：上一轮各文件注册成了什么（重载时据此退场）
_health = []                # [(spec, owner)]：上一轮结束时**确认仍归我**的条目（幂等体检用）
_live_ok = False            # 上一轮是否完整跑完（首次调用 / 中途异常 → 必须走注册）
_errors = {}                # path → [错误串]


def recipe_owner(source: str, stem: str) -> str:
    """来源限定的属主键："自带:bilibili" / "用户:bilibili"。

    为什么不能用**配方名**当属主（_recipe_owner 的旧取值）：`_retract` 是按属主撤注册的，
    而"用户放一份 bilibili.json 覆盖自带的 bilibili.json"恰恰是本功能的正当用法。用配方名
    做属主时，撤掉用户那份会**连带把自带那份也从注册表里抹掉**，而自带文件还在、mtime 没变
    —— 表现是"我把自己的配方删了，结果 B 站也解析不出来了"，且毫无报错。
    """
    return "%s:%s" % (source or "", stem or "")


def _retract(spec, owner=""):
    """把一份配方注册过的东西全部撤掉（解析器 + 短链 + 浏览器缝）。绝不抛。

    按**属主**撤，不是整体清空注册表 —— 整体清空会连带抹掉别处的注册，而
    site_recipes.ensure_registered() 有 _registered 记位不会重新补上。属主取值见 recipe_owner。
    """
    try:
        import media_resolve as _mr
        ow = owner or str(spec.get("name") or "")
        if not ow:
            return
        _mr.unregister_media_resolvers(ow)
        _mr.unregister_browser_seams(ow)
        for s in (spec.get("short_hosts") or []):
            _mr.unregister_short_host(s)
    except Exception:
        pass


def recipe_dir() -> str:
    """自带配方目录（延迟读 config，避免 config 的重量级 import 副作用）。"""
    try:
        import config as _cfg
        d = getattr(_cfg, "MEDIA_RECIPE_DIR", "")
        if d:
            return d
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "媒体配方")


def user_recipe_dir() -> str:
    """用户配方目录（用户自己写的/导入的；程序升级绝不覆盖这里）。"""
    try:
        import config as _cfg
        d = getattr(_cfg, "MEDIA_RECIPE_USER_DIR", "")
        if d:
            return d
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "用户配方")


def recipe_roots() -> list:
    """[(来源, 目录)]，**顺序即注册次序**：自带全部在前、用户全部在后。"""
    return [(SRC_BUILTIN, recipe_dir()), (SRC_USER, user_recipe_dir())]


def _json_files(d) -> list:
    """目录下全部配方文件名（排序、跳过 _ 开头）。目录不存在 → []。"""
    out = []
    if d and os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".json") and not fn.startswith("_"):
                out.append(fn)
    return out


def list_recipe_files() -> list:
    """自带配方目录下的 .json 文件名。全量（含用户目录）见 iter_recipe_files()。"""
    return _json_files(recipe_dir())


def iter_recipe_files() -> list:
    """全部配方文件 → [(来源, 路径, 文件名主干)]。次序 = 注册次序（自带先、用户后）。"""
    out = []
    for src, d in recipe_roots():
        for fn in _json_files(d):
            out.append((src, os.path.join(d, fn), os.path.splitext(fn)[0]))
    return out


def load_recipe(path):
    """读一个配方文件 → (spec, errors)。绝不抛。"""
    try:
        if os.path.getsize(path) > _MAX_SPEC_BYTES:
            return None, ["文件超过 %d 字节上限" % _MAX_SPEC_BYTES]
        with open(path, "r", encoding="utf-8-sig") as fp:
            spec = json.load(fp)
    except Exception as e:
        return None, ["JSON 解析失败：%s" % e]
    if isinstance(spec, dict) and not spec.get("name"):
        spec["name"] = os.path.splitext(os.path.basename(path))[0]
    errs = validate_spec(spec)
    return (None, errs) if errs else (spec, [])


# ----------------------------------------------------------------
# 10.1 启用 / 停用状态
#   存 runtime/media_recipe_state.json（不在配方目录里：那个目录的语义是"每个 json 都是配方"）。
#   为什么状态要独立存放，而不是写回 json：用户要停用的可能是**自带**配方，而自带目录升级时
#   被整体覆盖，写进去的停用会丢；何况自带 json 的 enabled 字段是配方作者的意志。
#   键 = 来源限定的属主键（见 recipe_owner），与配方名无关 —— 用户随手改 name 不影响记录。
# ----------------------------------------------------------------
_STATE_VERSION = 1
_state_lock = threading.RLock()
_state_cache = (None, {})   # (mtime, {key: bool})；mtime 为 None 表示未读过


def state_path() -> str:
    """启停状态文件路径（延迟读 config）。"""
    try:
        import config as _cfg
        p = getattr(_cfg, "MEDIA_RECIPE_STATE_PATH", "")
        if p:
            return p
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runtime", "media_recipe_state.json")


def _read_state() -> dict:
    """读状态文件 → {key: bool}。缺失/损坏一律当空表（= 全部按配方自带的 enabled 走）。绝不抛。

    fail-safe 的方向很重要：状态文件坏掉只该让用户的选择丢失（退回默认全开），
    绝不能因此把整套配方停掉 —— 那会让"某个站突然全都解析不出"且无迹可寻。
    """
    p = state_path()
    try:
        with open(p, "r", encoding="utf-8-sig") as fp:
            obj = json.load(fp)
    except FileNotFoundError:
        return {}
    except Exception as e:
        _log.warning("配方启停状态文件读不了，按“全部启用”处理：%s（%s）", p, e)
        return {}
    ov = obj.get("overrides") if isinstance(obj, dict) else None
    if not isinstance(ov, dict):
        _log.warning("配方启停状态文件格式不对（缺 overrides 对象），按“全部启用”处理：%s", p)
        return {}
    return {str(k): bool(v) for k, v in ov.items()}


def _load_state() -> dict:
    """按 mtime 缓存的 _read_state。"""
    global _state_cache
    p = state_path()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = 0.0
    with _state_lock:
        if _state_cache[0] is not None and _state_cache[0] == mt:
            return dict(_state_cache[1])
    m = _read_state()
    with _state_lock:
        _state_cache = (mt, m)
    return dict(m)


def set_recipe_enabled(key, enabled):
    """写一条启停状态 → (ok, err)。绝不抛。

    ⚠ 调用方持锁纪律：本函数只拿 _state_lock，**绝不可**在持锁期间调入
    ensure_user_recipes_loaded（那个拿 _load_lock 又回调 _load_state）。GUI 的用法是
    "先 set_recipe_enabled 返回，再 force 重载"，顺序不能反过来。
    """
    p = state_path()
    k = str(key or "")
    if not k:
        return False, "配方键为空"
    try:
        with _state_lock:
            cur = _read_state()
            cur[k] = bool(enabled)
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump({"version": _STATE_VERSION, "overrides": cur},
                          fp, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, p)          # 原子替换：写到一半断电不会留下半截文件
        global _state_cache
        with _state_lock:
            _state_cache = (None, {})   # 让下一次 _load_state 重读
        return True, ""
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------
# 10.2 配方清单（GUI 下拉框的唯一数据源）
# ----------------------------------------------------------------
def list_recipes() -> list:
    """全部配方（自带 + 用户）→ [{key, source, file, path, stem, name, enabled, ok, errors, spec}]。

    enabled 是**最终生效值**：状态文件覆盖 > 配方自带 enabled 字段。
    校验没过的也列出来（ok=False、spec=None、errors 非空），好让用户在下拉框里**看得见**
    "我这份写错了"，而不是它凭空消失 —— 引擎对坏配方只记日志不报错，看不见就等于没有反馈。
    """
    ov = _load_state()
    out = []
    for src, p, stem in iter_recipe_files():
        key = recipe_owner(src, stem)
        spec, errs = load_recipe(p)
        if errs:
            out.append({"key": key, "source": src, "file": os.path.basename(p),
                        "path": p, "stem": stem, "name": stem,
                        "enabled": False, "ok": False, "errors": errs, "spec": None})
            continue
        on = ov.get(key, spec.get("enabled", True))
        out.append({"key": key, "source": src, "file": os.path.basename(p),
                    "path": p, "stem": stem,
                    "name": spec.get("name") or stem,
                    "enabled": bool(on), "ok": True, "errors": [], "spec": spec})
    return out


def find_recipe(key):
    """按 key（"自带:bilibili"）/ 文件名主干 / 配方名找一份配方 → list_recipes() 的行；无 → None。

    容三种写法是为了容错：GUI 传 key，而人手写时往往直接写名字。
    """
    k = str(key or "")
    if not k:
        return None
    rows = list_recipes()
    for r in rows:
        if r["key"] == k:
            return r
    for r in rows:
        if r["stem"] == k:
            return r
    for r in rows:
        if r["name"] == k:
            return r
    return None


def overlapping_builtin(spec) -> str:
    """这份配方的 host_suffix 是否与某个自带配方重叠 → 那个自带配方的 key（无 → ""）。

    导入前提示用：重叠不是错误（用户配置优先是本功能的卖点），但用户该知道自己这份
    会顶掉哪一份自带配方。
    """
    hs = _hs_set(spec)
    if not hs:
        return ""
    for r in list_recipes():
        if r["source"] == SRC_BUILTIN and (hs & _hs_set(r["spec"])):
            return r["key"]
    return ""


def _hs_set(spec) -> set:
    hs = (spec or {}).get("host_suffix")
    hs = [hs] if isinstance(hs, str) else list(hs or [])
    return {str(x).strip().lower().lstrip(".") for x in hs if str(x).strip()}


# import_recipe_file 的返回码。用码而不是让 GUI 去匹配错误串 —— 匹配串一旦改文案就静默失效。
IMP_OK = ""
IMP_BAD = "bad"            # 校验没通过（含文件名不合法）
IMP_EXISTS = "exists"      # 目标已存在，需 overwrite=True 再来一次
IMP_IO = "io"              # 落盘失败


def import_recipe_file(src, *, overwrite=False):
    """把一份配方 json 导入用户配方目录 → (ok, 目标路径, [错误串], 码)。绝不抛。

    **先校验再复制**：不这么做的后果是"导入了一张永远不生效的哑配方"——引擎对不合法的
    配方只记一条日志、不报错，用户会以为已经装上了。宁可当场拒绝并把他写错的那一行指出来。
    """
    src = os.path.abspath(src or "")
    base = os.path.basename(src)
    if not base:
        return False, "", ["没有选择文件"], IMP_BAD
    if not base.lower().endswith(".json"):
        return False, "", ["只支持 .json 配方文件"], IMP_BAD
    if base.startswith("_"):
        return False, "", ["文件名以 _ 开头的文件引擎不加载（这类文件是模板/草稿位）；"
                           "请先改成别的名字（比如 mysite.json）再导入"], IMP_BAD
    spec, errs = load_recipe(src)
    if errs:
        return False, "", errs, IMP_BAD
    d = user_recipe_dir()
    dst = os.path.join(d, base)
    try:
        if os.path.exists(dst) and os.path.realpath(dst) == os.path.realpath(src):
            return True, dst, [], IMP_OK      # 已经躺在用户目录里 → 无操作（GUI 只需刷新）
        if os.path.exists(dst) and not overwrite:
            return False, dst, ["用户配方目录里已经有 %s 了（要替换请确认覆盖）" % base], IMP_EXISTS
        os.makedirs(d, exist_ok=True)
        import shutil
        shutil.copy2(src, dst)
    except Exception as e:
        return False, dst, ["写入失败：%s" % e], IMP_IO
    return True, dst, [], IMP_OK


def validate_dir() -> list:
    """扫描**两套**配方目录 → 同 list_recipes() 的形状，供 GUI 展示"哪些配置写错了"。"""
    return list_recipes()


def _spec_alive(spec, owner="") -> bool:
    """注册表里这些 host_suffix 是否**仍然是本配方**（而不是被 reset 抹掉/被别的实现顶替）。

    只看 mtime 是不够的：media_resolve.reset_media_resolvers() 或其它重新注册会把我们的
    条目清掉，而文件没变 → 纯 mtime 幂等会让配方**静默消失**（表现为"某个站突然解析不出，
    又没有任何报错"）。故每次都要花几次字典查找确认自己还活着。

    认的是 **owner** 而不是注册表条目的 name：用户配方覆盖自带配方时两条 host_suffix 相同，
    条目 name 可能一样也可能不一样，只有来源限定的 owner 能唯一确定"这条是我的"。
    """
    from media_resolve import _media_resolver_for
    ow = owner or str(spec.get("name") or "")
    if not ow:
        return False
    hs = spec.get("host_suffix")
    hs = [hs] if isinstance(hs, str) else list(hs or [])
    if not hs:
        return False
    for s in hs:
        r = _media_resolver_for("https://%s/" % str(s).strip("."))
        if not r or getattr(r.get("resolve"), "_recipe_owner", None) != ow:
            return False
    # 有浏览器缝的配方还要确认缝也在（否则"配方在、缝没了"同样静默降级）
    if (spec.get("browser_seam") or {}).get("url"):
        from media_resolve import _browser_seam_for
        for s in hs:
            b = _browser_seam_for("https://%s/" % str(s).strip("."))
            if not b or getattr(b.get("seam"), "_recipe_owner", None) != ow:
                return False
    return True


def ensure_user_recipes_loaded(*, force=False) -> int:
    """幂等载入 媒体配方/ + 用户配方/ 下全部 *.json 并注册（有增删改则增量重注册）。

    在 media_resolve.resolve_page 的配方步之后调用 → 同 host_suffix **后注册者覆盖**，
    配合"自带先、用户后"的扫描次序 = "用户配置优先"，且不必新增任何优先级机制。

    两级幂等：文件（或状态文件）mtime 变了要重载；没变但**注册表里已经找不到自己**也要重注册。
    """
    global _live_ok
    with _load_lock:
        entries, cur = [], {}
        for src, d in recipe_roots():
            for fn in _json_files(d):
                p = os.path.join(d, fn)
                try:
                    cur[p] = os.path.getmtime(p)
                except OSError:
                    continue
                entries.append((src, p, os.path.splitext(fn)[0]))
        # 状态文件也进 mtime 表 → 直接改那个 json 也能触发重载（不只是从界面点按钮）
        st = state_path()
        try:
            cur[st] = os.path.getmtime(st)
        except OSError:
            cur[st] = 0.0
        # `all([])` 在这里是**对的**：_live_ok 为真且体检清单为空，意味着上一轮完整跑完、
        # 且没有任何一条属于我们（全被停用/全被别的来源顶替）—— 那就无事可做。
        # 而"_live_ok 初始为假"接住了原注释里那个坑：首次调用、或注册表刚被 reset 时，
        # 绝不能因为"空列表 all() 恒真"就当自己还活着。
        if not force and _live_ok and cur == _loaded and all(
                _spec_alive(s, o) for s, o in _health):
            return 0
        # 先让上一轮注册的**全部退场**，再按当前目录重新注册。
        # 必要性：注册表没有"只撤一条"的老接口，若不撤，用户删掉/停用/改名/换 host_suffix 的
        # 配方会**留在注册表里继续生效**直到进程重启 —— 表现为"我改了 json 怎么没变化"，
        # 而且他没有任何办法看出来。撤自己再注册自己，代价只是几条字典操作。
        for _p, (_sp, _ow) in list(_live.items()):
            _retract(_sp, _ow)
        _live.clear()
        _errors.clear()
        _health[:] = []
        _live_ok = False
        n = 0
        ov = _load_state()
        # entries 的次序就是注册次序：自带全部先、用户全部后（见 recipe_roots）
        for src, p, stem in entries:
            spec, errs = load_recipe(p)
            if errs:
                _errors[p] = errs
                _log.warning("媒体配方 %s 校验未通过：%s", os.path.basename(p), errs[:3])
                continue
            key = recipe_owner(src, stem)
            on = ov.get(key, spec.get("enabled", True))
            if not on:
                _log.info("媒体配方已停用：%s（%s）", key, p)
                continue
            if register_spec(spec, source=os.path.basename(p), owner=key, enabled=True):
                _live[p] = (spec, key)
                n += 1
        # 体检清单只收"确认仍归我"的条目。这一步不是优化而是**必需**：用户配方覆盖自带配方时，
        # 自带那份的 host_suffix 已被用户顶替、_spec_alive 对它恒为假；若拿全部 _live 去体检，
        # 幂等短路永远不成立，**每次 resolve_page 都会整目录重载一遍**。
        _health[:] = [(s, o) for _p, (s, o) in sorted(_live.items()) if _spec_alive(s, o)]
        _loaded.clear()
        _loaded.update(cur)
        _live_ok = True
        if n:
            _log.info("媒体配方库已载入 %d 个（%s）", n,
                      "；".join("%s=%s" % (s, d) for s, d in recipe_roots()))
        return n


def reload_user_recipes() -> int:
    """清掉本引擎注册过的配方再重载（GUI"重新加载"按钮用）。"""
    return ensure_user_recipes_loaded(force=True)


def load_errors() -> dict:
    """上次载入中未通过的配方 → {文件路径: [错误串]}。"""
    return dict(_errors)


def registered_specs():
    """已注册配方名快照（诊断用）。"""
    from media_resolve import registered_media_resolvers
    return sorted({n for _s, n, _e in registered_media_resolvers()})
