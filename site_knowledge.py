# -*- coding: utf-8 -*-
"""网站诊断的知识库与历史（P2）。

两块东西，都是给 `site_diag` 用的**只读/旁证**，绝不参与判级：

  一、知识聚合 —— 把项目里散落多年的"经验表"汇总成一个索引，让诊断能引用它们：
        qiongqi_maw.CHALLENGE_SIGNATURES  → 反爬厂商特征（本项目最结构化的一份）
        config.HARD_SITE_CONFIG           → 高难度站点表
        config.SITE_DELAY_CONFIG          → 每站节拍建议
        config.SITE_BLOCK_WORDS           → 每站已知拦截词
        媒体配方/*.json                    → "这个站该怎么抓"的成文经验

  二、跨会话历史 —— runtime/site_diag_history.json，**只存结论码与计数，绝不存 URL**。
        历史在报告里灰显为旁证，`grade_difficulty` 永不读它（避免自我实现）。

分层纪律（与 site_diag 一致，**破了这条离线用例就得联网**）：
  · 匹配函数是**纯函数**，表一律参数注入（`sigs=` / `hard_cfg=` / `delays=` / `blocks=` / `recipes=`），
    传 None 才去惰性 import 真实来源；
  · 本模块**不 import site_diag 的顶层**（只在函数体内惰性 import 取 `_host_of`），
    反过来 site_diag 也只在 run_diagnosis 体内 import 本模块 —— 两边都惰性，没有循环。
    ⚠ 因此 build.py 与 .spec 都要注册 `site_knowledge`（顶层零引用，静态图收不到）。
  · 本模块**只产出事实 dict**，不产出结论码 —— 结论码归 site_diag 管（它才拥有 C_* 常量
    和 DiagFinding）。这条是刻意的：知识层说事实，诊断层下判断。

底线（与全项目一致）：只读、只观测。本模块不发起任何网络请求（连 robots 都不抓）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlsplit

KNOW_VERSION = "1.0"

# 与 qm.detect_challenge_type 保持一致的扫描窗口：静态路径（探针拿回来的 body）和
# 浏览器路径（driver.page_source）必须用同一段长度，否则同一个站两条路会给出不同答案。
_BODY_SCAN = 50000


class _Miss:
    """"没传这个参数"与"显式传了空表"要区分开：前者去取真实来源，后者就用给的那份。"""


_MISS = _Miss()


# ================================================================
# 一、只读聚合（惰性 import + 兜底，绝不抛）
# ================================================================
def challenge_signatures(*, src=None) -> dict:
    """反爬厂商特征表。src 非 None 则直接用它（离线用例注入）。"""
    if src is not None:
        return src if isinstance(src, dict) else {}
    try:
        import qiongqi_maw as qm
        v = getattr(qm, "CHALLENGE_SIGNATURES", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def hard_sites(*, src=None) -> dict:
    if src is not None:
        return src if isinstance(src, dict) else {}
    try:
        import config
        v = getattr(config, "HARD_SITE_CONFIG", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def site_delays(*, src=None) -> dict:
    if src is not None:
        return src if isinstance(src, dict) else {}
    try:
        import config
        v = getattr(config, "SITE_DELAY_CONFIG", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def block_words(*, src=None) -> dict:
    if src is not None:
        return src if isinstance(src, dict) else {}
    try:
        import config
        v = getattr(config, "SITE_BLOCK_WORDS", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def recipe_hosts(*, recipes=None) -> list:
    """已启用的媒体配方 → [{name, source, enabled, hosts:[...]}]。

    **只收 enabled 的**：停用 = 不注册 = 引擎根本不认这份配方，把它算成"已有配方"
    等于告诉用户"这站能抓"而实际抓的时候引擎压根不看它 —— 正是本项目最忌讳的静默失真。

    **必须幂等**：本函数的输出形状（已带 hosts）也接受。这条不是洁癖 ——
    `build_index()` 存的就是本函数的输出，而 `match_recipe` 又要吃 `idx["recipes"]`，
    于是它会拿自己的输出喂自己。第一版没做这层兼容，`spec` 取不到 → hosts 为空 → 全部
    `continue` → **静默返回空表**，报告里"已有配方"永远不出现，且不报任何错。
    （D33m 端到端用例抓到的；纯函数用例喂的是 list_recipes 的原始形状，照不出来。）
    """
    if recipes is None:
        try:
            import media_recipe as mr
            recipes = mr.list_recipes()
        except Exception:
            return []
    out = []
    for r in (recipes or ()):
        if not isinstance(r, dict):
            continue
        if "hosts" in r and isinstance(r.get("hosts"), list):
            # 已归一的形状：名字/主机都在顶层，用它的（不再看 spec）
            hosts = [str(h).strip().lower().lstrip(".")
                     for h in r["hosts"] if str(h or "").strip()]
            if hosts and r.get("enabled", True):
                out.append({"name": str(r.get("name") or ""),
                            "source": str(r.get("source") or ""),
                            "enabled": True, "hosts": hosts})
            continue
        spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
        if not r.get("enabled", True) or not r.get("ok", True):
            continue
        hosts = []
        for k in ("host_suffix", "short_hosts"):
            for h in (spec.get(k) or ()):
                h = str(h or "").strip().lower().lstrip(".")
                if h and h not in hosts:
                    hosts.append(h)
        if not hosts:
            continue
        out.append({"name": str(spec.get("name") or r.get("name") or r.get("stem") or ""),
                    "source": str(r.get("source") or ""),
                    "enabled": True, "hosts": hosts})
    return out


def build_index(*, src=None, recipes=None, cfg_src=None) -> dict:
    """一次性把上面五张表聚成索引。src/cfg_src/recipes 是离线注入点。

    src     —— CHALLENGE_SIGNATURES 替身
    cfg_src —— dict 形如 {"hard":…, "delay":…, "blocks":…}，三张 config 表的替身
    recipes —— 配方清单替身
    """
    cfg = cfg_src if isinstance(cfg_src, dict) else {}
    errs = []
    hard = cfg.get("hard", _MISS)
    delay = cfg.get("delay", _MISS)
    blocks = cfg.get("blocks", _MISS)
    idx = {
        "version": KNOW_VERSION,
        "challenge": challenge_signatures(src=src),
        "hard": hard_sites(src=None) if hard is _MISS else hard_sites(src=hard),
        "delay": site_delays(src=None) if delay is _MISS else site_delays(src=delay),
        "blocks": block_words(src=None) if blocks is _MISS else block_words(src=blocks),
        "recipes": recipe_hosts(recipes=recipes),
    }
    if not idx["challenge"]:
        errs.append("反爬特征表为空（qiongqi_maw 不可用？）")
    if not idx["hard"]:
        errs.append("高难度站点表为空")
    idx["errors"] = errs
    return idx


def index_summary(idx: dict) -> dict:
    """索引规模，用来在报告里说清"这套知识库有多大"（也让空索引一眼看得出来）。"""
    idx = idx or {}
    return {"challenge": len(idx.get("challenge") or {}),
            "hard": len([k for k in (idx.get("hard") or {}) if k != "default"]),
            "delay": len([k for k in (idx.get("delay") or {}) if k != "default"]),
            "blocks": len([k for k in (idx.get("blocks") or {}) if k != "default"]),
            "recipes": len(idx.get("recipes") or ())}


# ================================================================
# 二、纯匹配（表全部参数注入）
# ================================================================
def _host_matches(host: str, suffix: str) -> bool:
    """最长后缀式匹配：a.example.com 命中 example.com；badexample.com 不命中。

    注意与 site_diag._same_site（子域感知）不是一回事：这里问的是"这个 host 属不属于
    这个模式"，不是"两个 host 是不是同一家"。
    """
    h, s = (host or "").strip(".").lower(), (suffix or "").strip(".").lower()
    if not h or not s:
        return False
    return h == s or h.endswith("." + s)


def match_challenge_text(title: str, url: str, body: str, *, sigs=None) -> dict:
    """静态版挑战识别：**同一张特征表、同一套打分规则**，只是不依赖 Chrome。

    `qm.detect_challenge_type(driver)` 吃 driver，诊断在"不开浏览器"档位下就只有页面正文。
    两边必须给出同一个答案，所以这里逐字复刻它的规则：
      · 三段文本各自小写、body 截到前 50000 字符（与 qm 一致）；
      · 命中 title/url/body 任一即算该厂商；score = 命中段数；
      · 表是有序的，**先命中先赢**（qm 的 dict 顺序即优先级）；
      · 关键词表为空的段一律不算命中（`if keys else False`）—— 复刻这个细节很重要，
        recaptcha/hcaptcha 的 title_keywords 是空表，写成 `all(...)` 会让它们恒命中。

    返回 {"type": name, "confidence": score, "where": [...]}；没命中 → type="unknown"。
    """
    t = (title or "").lower()
    u = (url or "").lower()
    b = (body or "")[:_BODY_SCAN].lower()
    for name, sig in (sigs if sigs is not None else challenge_signatures()).items():
        if not isinstance(sig, dict):
            continue
        tk, uk, bk = (sig.get("title_keywords") or ()), (sig.get("url_keywords") or ()), \
                     (sig.get("body_keywords") or ())
        where = []
        if tk and any(k in t for k in tk):
            where.append("title")
        if uk and any(k in u for k in uk):
            where.append("url")
        if bk and any(k in b for k in bk):
            where.append("body")
        if where:
            return {"type": name, "confidence": len(where), "where": where}
    return {"type": "unknown", "confidence": 0, "where": []}


def match_recipe(url: str, *, recipes=None) -> dict:
    """该 URL 是否已有启用中的媒体配方。长后缀赢（与 site_diag.match_hard_site 同规则）。"""
    host = _host(url)
    if not host:
        return {}
    best = None
    for r in recipe_hosts(recipes=recipes):
        for h in r["hosts"]:
            if _host_matches(host, h):
                if best is None or len(h) > len(best[1]):
                    best = (r, h)
    if best is None:
        return {}
    r, h = best
    return {"name": r["name"], "source": r["source"], "matched": h}


def pacing_for_key(key: str, *, delays=None) -> dict:
    """按站键取节拍建议。key 来自 site_diag.match_hard_site（SITE_DELAY_CONFIG 的键
    是站键不是 host，所以这里只认键，不自己再匹配一遍 host）。"""
    k = str(key or "").strip()
    if not k:
        return {}
    d = delays if delays is not None else site_delays()
    e = (d or {}).get(k)
    if not isinstance(e, dict):
        return {}
    return {"key": k, "min": e.get("min"), "max": e.get("max")}


def match_block_words(text: str, *, key: str, words=None) -> dict:
    """该站**已知拦截词**是否出现在正文里。

    **刻意不回退 default 表**：那张表是 ["403","验证码"] —— 几乎每个 403 页都含这两个词之一，
    回退过去等于"每条 403 都命中拦截词"，那不是信息是噪音。只认被点名站点的专属词表。

    调用方还应只在**非 2xx** 时来问（见 site_diag.findings_from_knowledge）：
    一个正常 200 页面正文里出现"验证码"多半是登录框，不是拦截。
    """
    k = str(key or "").strip()
    if not k or not text:
        return {}
    w = words if words is not None else block_words()
    lst = (w or {}).get(k)
    if not isinstance(lst, (list, tuple)) or not lst:
        return {}
    low = text.lower()
    hits = [str(x) for x in lst if str(x) and str(x).lower() in low]
    if not hits:
        return {}
    return {"key": k, "hits": hits}


def knowledge_facts(*, index=None, url="", title="", body="", status=0,
                    hard_site=None) -> dict:
    """把上面几个匹配合成一份"该 URL 命中了什么"的事实 dict（纯函数，不产结论码）。

    site_diag 拿这份事实去下判断 —— 知识层只说事实。
    """
    idx = index if isinstance(index, dict) else build_index()
    host = _host(url)
    facts = {
        "host": host,
        "challenge": match_challenge_text(title, url, body, sigs=idx.get("challenge")),
        "recipe": match_recipe(url, recipes=idx.get("recipes")),
        "hard_site": hard_site,
        "pacing": pacing_for_key((hard_site or {}).get("key", ""), delays=idx.get("delay")),
        "block_words": {},
    }
    # 拦截词只在"站方明确拒绝"的响应上查（见 match_block_words 的注释）。
    if status and int(status) >= 400:
        facts["block_words"] = match_block_words(
            body, key=(hard_site or {}).get("key", ""), words=idx.get("blocks"))
    return facts


# ================================================================
# 三、跨会话历史（runtime/site_diag_history.json）
#     四条铁律（方案里定死）：
#       1. 只存结论码与计数 —— 白名单字段，绝不含 URL / path / query；
#       2. 每 host 10 条、全局 500 条，超出丢最旧；
#       3. 原子写 + 坏文件当空（沿用 media_recipe_state.json 的既有约定）；
#       4. 在界面上是旁证，**永不参与判级**（grade_difficulty 根本不接历史的参数）。
# ================================================================
_HIST_VERSION = 1
_PER_HOST = 10
_TOTAL = 500

# **白名单**（不是黑名单）：新增字段必须显式加到这里，否则不会被落盘。
# 写成黑名单的坏处是"将来谁加了个 url 字段"会静默泄进去，且没人会发现。
_ENTRY_KEYS = ("at", "scope", "level", "level_source", "codes",
               "n_findings", "n_requests", "verdict")

_hist_lock = threading.RLock()


def history_path() -> str:
    """历史文件路径。优先级：
      1. config.SITE_DIAG_HISTORY_PATH 显式指定；
      2. config.base_path/runtime/… ——  **跟着 base_path 走**，
         于是所有把 base_path 指到沙箱的测试/冒烟脚本自动重定向历史，不会污染开发库。
         （09-11 那轮的教训：沙箱只指 DB_SAVE_DIR 不够，得指 base_path。）
      3. 本模块所在目录/runtime/…（config 不可用时的兜底）。
    """
    try:
        import config
        p = getattr(config, "SITE_DIAG_HISTORY_PATH", "")
        if p:
            return p
        bp = getattr(config, "base_path", "")
        if bp:
            return os.path.join(bp, "runtime", "site_diag_history.json")
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runtime", "site_diag_history.json")


def load_history(*, path=None) -> dict:
    """读历史 → {host: [entry, ...]}（新在前）。缺失/损坏一律当空表，绝不抛。

    fail-safe 的方向同 media_recipe._read_state：坏文件只该让旁证丢失，
    绝不能因此让诊断本身出问题。
    """
    p = path or history_path()
    try:
        with open(p, "r", encoding="utf-8-sig") as fp:
            obj = json.load(fp)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    hosts = obj.get("hosts") if isinstance(obj, dict) else None
    if not isinstance(hosts, dict):
        return {}
    out = {}
    for h, lst in hosts.items():
        if not isinstance(lst, list):
            continue
        good = [e for e in lst if isinstance(e, dict)]
        if good:
            out[str(h)] = good
    return out


def _entry_from_report(rep) -> dict:
    """报告 -> 一条历史记录（**白名单字段**，见 _ENTRY_KEYS）。"""
    codes = sorted({str(getattr(f, "code", "") or "") for f in (getattr(rep, "findings", None) or ())})
    risk = getattr(rep, "risk", None) or {}
    feas = getattr(rep, "feasibility", None) or {}
    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scope": str(getattr(rep, "scope", "") or "page"),
        "level": str(risk.get("level") or ""),
        "level_source": str(risk.get("source") or ""),
        "codes": [c for c in codes if c],
        "n_findings": len(getattr(rep, "findings", None) or ()),
        "n_requests": int((getattr(rep, "probe", None) or {}).get("requests_n") or 0),
        "verdict": str(feas.get("verdict") or ""),
    }


def prune_entries(hist: dict, *, per_host: int = _PER_HOST, total: int = _TOTAL) -> dict:
    """每 host 留最新 per_host 条；全局留最新 total 条（按 at 倒序，同刻按 host 名定序，
    保证同样的输入产出同样的结果）。"""
    out, flat = {}, []
    for h, lst in (hist or {}).items():
        if not isinstance(lst, list):
            continue
        keep = [e for e in lst if isinstance(e, dict)][:max(0, int(per_host))]
        if keep:
            out[str(h)] = keep
            for e in keep:
                flat.append((str(e.get("at") or ""), str(h), e))
    flat.sort(key=lambda t: (t[0], t[1]), reverse=True)
    allowed = {}
    for _, h, e in flat[:max(0, int(total))]:
        allowed.setdefault(h, 0)
        allowed[h] += 1
    return {h: out[h][:allowed[h]] for h in out if allowed.get(h)}


def record(rep, *, path=None) -> tuple:
    """把一次诊断写进历史 → (ok, err)。**绝不抛**。

    ⚠ 只有 host 能取到时才写；取不到（非法 URL）直接跳过 —— 历史以 host 为键，
      没有键就没地方放，硬塞一个空键会让所有坏 URL 挤在同一条上。
    """
    try:
        import site_diag as sd            # 惰性：本模块顶层不依赖 site_diag
        host = sd._host_of(getattr(rep, "url", "") or "")
    except Exception:
        return False, "取不到 host"
    # `_host_of` 对裸字符串是宽容的（"不是网址"会被当成 host），所以这里要再卡一道：
    # **hostname 一定是 ASCII**（国际化域名走 punycode）。带中文/空格的"host"不是 host，
    # 写进去就是一条永远查不到的垃圾键。宁可少写一条，也不留脏数据。
    if not host or not host.isascii() or any(c.isspace() for c in host):
        return False, "取不到 host"
    p = path or history_path()
    try:
        with _hist_lock:
            hist = load_history(path=p)
            hist.setdefault(host, [])
            hist[host] = [_entry_from_report(rep)] + list(hist[host])
            hist = prune_entries(hist)
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump({"version": _HIST_VERSION, "hosts": hist},
                          fp, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, p)            # 原子替换：写到一半断电不会留下半截文件
        return True, ""
    except Exception as e:
        return False, str(e)


def history_for(url_or_host: str, *, path=None, limit: int = _PER_HOST) -> dict:
    """该 host 的历史 → {host, total, entries}。site_diag 直接塞进 DiagReport.history。"""
    try:
        import site_diag as sd
        host = sd._host_of(url_or_host)
    except Exception:
        host = ""
    hist = load_history(path=path)
    lst = hist.get(host) or []
    return {"host": host, "total": len(lst),
            "entries": lst[:max(0, int(limit))] if limit else lst}


def clear_history(*, path=None) -> tuple:
    """清空历史 → (ok, err)。GUI 目前没有入口；留着是为了让"删历史"这件事有确定实现，
    而不是让用户去手删 runtime 文件。"""
    p = path or history_path()
    try:
        with _hist_lock:
            if os.path.exists(p):
                os.remove(p)
        return True, ""
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------
# 内部
# ----------------------------------------------------------------
def _host(u: str) -> str:
    """URL 或裸 host -> 纯 host。与 site_diag._host_of 同语义（那边是 Layer 1 的本地实现）。"""
    s = (u or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        try:
            s = urlsplit(s).netloc
        except Exception:
            return ""
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s.rstrip(".")
