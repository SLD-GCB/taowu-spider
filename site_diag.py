# -*- coding: utf-8 -*-
"""网站全方位诊断（后端）—— 粘一个 URL，告诉你这个站能不能抓、有什么坑。

设计依据：`开发报告/2026-09-09-网站前后端诊断监测-方案.md`（已批）+ 2026-09-11 扩展
（用户口径「全身检查、深度要能穿透整个网站」+「robots.txt 抓下来才知道底线在哪」）。

层次（**这是本模块最重要的纪律**）：

  Layer 1  纯函数：fingerprint_html / summarize_requests / parse_robots / robots_check /
           normalize_url / url_template / parse_sitemap / build_structure_map /
           pagination_candidates / match_hard_site / grade_difficulty
           —— 绝不自行 import 请求库、绝不读盘。外部数据一律参数注入，
              传 None 才去取真实来源。这样离线用例不需要网络也不需要真 Chrome。

  Layer 2  探针：probe_http —— 唯一发网络请求的地方（除浏览器阶段）。
  Layer 3  编排：run_diagnosis(单页) / run_site_diagnosis(整站)。
  Layer 4  渲染：render_* —— 只读 report，不产生新结论。

整站模式（P3）的一句话设计：

    **覆盖率来自「结构」，深度来自「链路走到底」—— 不靠抓更多页换来。**

  L1 导览：robots 的 Sitemap: 行 → sitemap index **递归**展开 → 全量 loc；再补首页链接。
           代价是**个位数请求**，产出是 URL 全集（可能几万条）。
  L2 归纳：URL 全集 → 模板归约 → **结构地图**（有哪些页面类型、各多少条）。0 请求。
  L3 穿透：每个模板沿 列表 → 翻页 → 详情 走到底，回答「能不能**系统性**拿下来」。
  L4 体检：每个模板抽 N 页跑**完整单页诊断**（复用 run_diagnosis，不另起一套）。

    贵的是 L3/L4，而 L3/L4 的量由**模板数**决定，不由站点总页数决定 —— 这才叫全覆盖。

    **深度用时间换，绝不靠并发换**：恒串行、页间 ≥ delay 秒、只发 GET、遇 429 立刻停整站。

为什么不用 vg.fetch_html 取页面（09-09 方案的老缺口）：

    fetch_html 非 200 时返回 ("", url) —— **把状态码吞了**。403 和 404 都变成空字符串，
    而「这站返回 403 还是 404」恰恰是诊断最该说的第一句话。所以 probe_http 自己取，
    一次 GET 同时拿到 status / headers / body，body 直接喂给 fingerprint_html
    （不做两次请求）。

为什么 probe_http 不走 vg._http_get（**与真实抓取路径的已知差异**）：

    _http_get 带整套吞吐机器 —— 全局闸 + host 闸 + _health_mark + 首次调用会
    _ensure_controller() 起自适应控制线程。诊断是「串行、每站十几次请求」的流量形态，
    塞进那套机器有两个坏处：
      (1) 副作用：_health_mark 连续失败 3 次会把该 host 并发压到 MIN —— 诊断的失败
          会去改真实下载的调速状态；
      (2) 会为一次诊断拉起控制线程。
    所以 probe_http 自己发请求，但**请求头与指纹路由仍走 vg**（_media_headers /
    _imp_state_for / _imp_cffi_module）—— 那两样决定「站看到的是谁」，必须与真实抓取一致。
    代理默认不走（诊断要可复现）；这一差异在报告的「本次探测范围」节里如实写明。

绝不修改 video_grab / audio_grab / qiongqi_maw / media_resolve 的任何函数体，
也绝不调用任何 register_*（诊断零注册副作用，D13 用例钉死这一条）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from urllib.parse import (urlparse, urljoin, urlsplit, urlunsplit,
                          parse_qsl, urlencode)

DIAG_VERSION = "1.0"

# ================================================================
# 结论码（ASCII 稳定）
# 测试**断言 code，绝不断言中文** —— 否则改一个措辞就红一片。
# ================================================================
C_REACH_DNS      = "risk.reach.dns"
C_REACH_TLS      = "risk.reach.tls"
C_REACH_CONN     = "risk.reach.conn"
C_REACH_TIMEOUT  = "risk.reach.timeout"
C_HTTP_5XX       = "risk.http.server_error"
C_RATE_LIMIT     = "risk.http.rate_limit"
C_AUTH_WALL      = "risk.http.auth_wall"
C_CHALLENGE_AUTO = "risk.http.challenge_auto"
C_CHALLENGE_MAN  = "risk.http.challenge_manual"
C_HOTLINK        = "risk.http.hotlink"
C_TLS_RESCUED    = "risk.tls.curl_rescued"
C_NOT_FOUND      = "risk.http.not_found"

C_FRONT_FRAMEWORK = "front.framework"
C_FRONT_SSR       = "front.ssr"
C_FRONT_SPA       = "front.spa"
C_FRONT_STATIC    = "front.static"
C_FRONT_CHALLENGE = "front.challenge_page"
C_FRONT_CAPTCHA_SDK = "front.captcha_sdk"   # 页面**引用**了验证码/防护组件（≠ 挑战页）
C_FRONT_EMPTY     = "front.empty_shell"
C_FRONT_LOGIN     = "front.login_wall"
C_FRONT_MEDIA     = "front.media_signals"
# 「这个站声明了商业 DRM」——**两条观测通道，同一个事实**，所以共用一个码：
#   via="eme"      浏览器跑起来，页面申请了 CDM（findings_from_eme）
#   via="manifest" 清单里的 KEYFORMAT / pssh（probe_track_drm）—— **不开浏览器也能拿到**
# 值不再写死 `_eme`：那会让「清单里判出来的」在数据里冒充浏览器观测。
C_FRONT_DRM       = "front.drm"

C_GRAB_ROUTE      = "grab.route"
C_GRAB_TRACKS     = "grab.tracks"
C_GRAB_NO_UA      = "grab.missing_ua"

C_RULES_ROBOTS    = "rules.robots.declared"
C_RULES_NOROBOTS  = "rules.robots.absent"
C_RULES_HIT       = "rules.robots.path_disallowed"
C_RULES_SITEMAP   = "rules.robots.sitemap_declared"

C_KNOW_HARDSITE   = "knowledge.hard_site"
C_KNOW_CHALLENGE  = "knowledge.challenge_signature"
C_KNOW_RECIPE     = "knowledge.recipe_available"
C_KNOW_BLOCKWORD  = "knowledge.block_word"

# ── 整站（P3）──
C_SITE_STRUCTURE  = "site.structure"           # 结构地图给出了什么（信息性）
C_SITE_SITEMAP    = "site.sitemap"             # sitemap 声明与展开情况
C_SITE_NOSITEMAP  = "site.sitemap_absent"      # 没有 sitemap —— 导览只能靠首页链接
C_SITE_PAGINATION = "site.pagination_ok"       # 列表页**翻得动**（正面证据）
C_SITE_NO_PAGE    = "site.pagination_ignored"  # 页码被忽略：200 但内容一模一样（网易云那个坑）
C_SITE_BUDGET     = "site.budget_exhausted"    # 预算用尽，覆盖到此为止（**绝不静默**）
C_SITE_ABORT429   = "site.aborted_rate_limit"  # 429 → 整站立即停

# ── 文字轴（2026-09-11）：工作台另一条腿，之前诊断零覆盖 ──
# **一个都不进 `_OBS_TO_LEVEL`**，与七个 `site.*` 同纪律：它们答的是「我们用这套模板
# 能不能从这页抽出来」，是**我们走了多远**，不是「站方在防你」——不配抬难度档。
C_TEXT_HIT        = "text.schema_hit"        # 有模板抽得出业务列（正面证据）
C_TEXT_EMPTY      = "text.schema_empty"      # 所有模板业务列全空 → 壳页/纯前端
C_TEXT_NOTITLE    = "text.no_title"          # 连标题都认不出
C_TEXT_NODEDUP    = "text.no_unique_key"     # 落库无唯一键：跨会话重抓会重复入库

SEV_INFO, SEV_LOW, SEV_MID, SEV_HIGH, SEV_BLOCKER = "info", "low", "mid", "high", "blocker"

SRC_STATIC, SRC_BROWSER, SRC_KNOWLEDGE = "static", "browser", "knowledge"
SRC_RECIPE, SRC_CONFIG, SRC_HISTORY = "recipe", "config", "history"


# ================================================================
# 数据模型
# ================================================================
@dataclass
class DiagFinding:
    """一条结论：ASCII 稳定 code + 中文 label/detail + evidence + severity + source。"""
    code: str = ""
    label: str = ""
    detail: str = ""
    severity: str = SEV_INFO
    source: str = SRC_STATIC
    evidence: dict = field(default_factory=dict)


@dataclass
class DiagRow:
    """浏览器阶段观测到的一条请求。字段沿用 09-09 方案，不改。"""
    method: str = "GET"          # open/init 里的 method，大写
    url: str = ""                # 绝对 URL
    status: int = 0              # 真实 status；跨域 opaque -> 0
    content_type: str = ""       # 响应 Content-Type（尽力，跨域可能空）
    body_sample: str = ""        # 尽力首 ~2048 字符（仅同源 json/text；二进制/跨域不读）
    kind: str = "xhr"            # "xhr" | "fetch" | "perf"（performance resource，方法未知）
    frame_url: str = ""          # 发起 frame 的 location.href
    page_url: str = ""           # 入口 URL


@dataclass
class DiagReport:
    """诊断报告。前 7 个字段是 09-09 方案的原始形状（不删不改），其余为 09-11 扩展。"""
    url: str = ""
    final_url: str = ""
    title: str = ""
    fingerprint: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)          # [DiagRow, ...]
    summary: dict = field(default_factory=dict)
    route_verdict: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    text: str = ""                                    # render_report() 产物

    # ── 09-11 扩展 ──
    scope: str = "page"                               # "page" | "site"
    findings: list = field(default_factory=list)      # [DiagFinding, ...]
    feasibility: dict = field(default_factory=dict)   # 能不能抓 / 走哪条路
    risk: dict = field(default_factory=dict)          # 难度分级
    media: dict = field(default_factory=dict)         # 媒体与 DRM
    knowledge: dict = field(default_factory=dict)     # 知识库命中（P2 填）
    suggestions: list = field(default_factory=list)   # [str, ...] 可执行建议
    history: dict = field(default_factory=dict)       # 历史（P2 填；永不参与判级）
    # ── 整站扩展（P3）。单页模式一律为空，渲染时如实写「未做」。 ──
    profile: str = ""                                 # 档位键（shallow/standard/deep/all）
    profile_name: str = ""                            # 档位中文名
    discovery: dict = field(default_factory=dict)     # L1：sitemap 展开 + 首页链接的账目
    structure: dict = field(default_factory=dict)     # L2：结构地图（模板 / 计数 / 样例）
    penetration: dict = field(default_factory=dict)   # L3：翻页探针等穿透结论
    pages: list = field(default_factory=list)         # L4：代表页逐页摘要（**普通 dict**，
                                                      #     不嵌 DiagReport —— 免得 JSON 往返要递归还原）
    budget: dict = field(default_factory=dict)        # 请求账目：用了多少、上限多少、剩多少没走
    probe: dict = field(default_factory=dict)         # probe_http 原始结果
    rules: dict = field(default_factory=dict)         # robots 解析结果 + 本页合规判定
    elapsed_ms: int = 0
    diag_version: str = DIAG_VERSION
    sections: dict = field(default_factory=dict)      # 分节渲染产物
    # 本次**覆盖到哪儿、没覆盖哪儿**。存在的理由见 build_coverage 的 docstring：
    # 「没测」不能长得跟「测了没问题」一样。**只陈述事实，不参与判级**。
    coverage: dict = field(default_factory=dict)
    # 文字轴：拿数据表模板对真页面干跑一遍的结果（纯解析、零请求）。
    # 字段名不叫 `text` —— 那个已经是渲染出来的报告全文了。
    text_probe: dict = field(default_factory=dict)


def finding(code, label, detail="", severity=SEV_INFO, source=SRC_STATIC, **evidence):
    """构造 DiagFinding（evidence 走 kwargs，省得每处写 dict）。"""
    return DiagFinding(code=code, label=label, detail=detail,
                       severity=severity, source=source, evidence=dict(evidence))


# ================================================================
# Layer 1-a：URL 归一化与模板归约（纯字符串，不发任何请求）
#
# 为什么不复用 vg._hop_target / _resolve_candidates 做站点导览：
#   那是「找媒体流」的语义，会跟着重定向一层层往下挖，拿来做站点导览会语义错位
#   且请求数失控。导览要的是「去重 + 归纳」，纯字符串就够，零请求。
# ================================================================
_TRACK_PARAMS = frozenset((
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_referrer", "spm", "scm", "fbclid", "gclid", "msclkid",
    "yclid", "from_source", "share_source", "share_medium", "_t", "timestamp",
))


def normalize_url(u: str, base: str = "") -> str:
    """URL 归一：绝对化、去 fragment、去跟踪参数、query 排序、host 小写、去默认端口。

    **保守**：不动尾斜杠（`/a/` 与 `/a` 在多数站点是不同资源）—— 归一不足只会产生
    重复项，归一过度会丢页面，前者无害后者致命。不可解析 → 返回空串。
    """
    s = (u or "").strip()
    if not s or s.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
        return ""
    try:
        if base:
            s = urljoin(base, s)
        sp = urlsplit(s)
    except Exception:
        return ""
    if sp.scheme.lower() not in ("http", "https") or not sp.netloc:
        return ""
    host = sp.hostname or ""
    if not host:
        return ""
    host = host.lower().rstrip(".")
    netloc = host
    try:
        port = sp.port
    except Exception:
        port = None
    if port and not ((sp.scheme.lower() == "http" and port == 80)
                     or (sp.scheme.lower() == "https" and port == 443)):
        netloc = f"{host}:{port}"
    path = sp.path or "/"
    try:
        pairs = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True)
                 if k.lower() not in _TRACK_PARAMS]
    except Exception:
        pairs = []
    pairs.sort()
    query = urlencode(pairs, doseq=True)
    return urlunsplit((sp.scheme.lower(), netloc, path, query, ""))


_SEG_NUM    = re.compile(r"^\d+$")
_SEG_HASH   = re.compile(r"^[0-9a-f]{8,}$", re.I)
_SEG_GUID   = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SEG_MIXID  = re.compile(r"^[a-z]{1,6}\d{3,}$", re.I)                 # vod12345 / article2026
_SEG_WORDID = re.compile(r"^[a-z]{1,10}[-_]?\d{2,}$", re.I)           # product-123 / news2026
_SEG_NUMPLX = re.compile(r"^\d+(?:[-_]\d+)+$")                        # 123-1-2（MacCMS 分集形态）
_SEG_WORDY  = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+){2,}$", re.I)   # slug 状的段


def _tmpl_segment(seg: str) -> str:
    """把单个路径段归约成模板段：数字/哈希/GUID/混合ID -> {id}，长 slug -> {slug}。

    **归约不足比归约过度安全**：不足只是模板多几条（结构地图啰嗦，但结论保守），
    过度会把两类页面并成一条（结论会覆盖不到）。所以只认**明确的 ID 形状**，
    语义词（`about` / `category` / `tags`）一律原样保留。
    """
    if not seg:
        return seg
    stem, dot, ext = seg.partition(".")
    ext_part = (dot + ext) if dot else ""
    if (not stem) or (stem in ("index", "default")):
        return seg
    if _SEG_GUID.match(stem) or _SEG_NUM.match(stem) or _SEG_HASH.match(stem) \
            or _SEG_MIXID.match(stem) or _SEG_WORDID.match(stem) or _SEG_NUMPLX.match(stem):
        return "{id}" + ext_part
    if _SEG_WORDY.match(stem) and len(stem) >= 12:
        return "{slug}" + ext_part
    return seg


def url_template(u: str) -> str:
    """URL -> 模板串，如 `/news/1.html` 与 `/news/2.html` -> `/news/{id}.html`。

    模板 = 站点的**页面类型**。诊断的「覆盖率来自结构」全建立在这一步上：
    几万条 URL 归约成几十个模板，然后每模板抽代表页深诊。
    query 只保留**键名**（键名决定这是搜索页还是分页），值一律丢弃。
    """
    s = (u or "").strip()
    if not s:
        return ""
    try:
        sp = urlsplit(s)
    except Exception:
        return ""
    segs = [x for x in (sp.path or "/").split("/")]
    tmpl = "/".join(_tmpl_segment(x) for x in segs)
    try:
        keys = sorted({(k or "").lower() for k, _ in parse_qsl(sp.query, keep_blank_values=True)
                       if (k or "").lower() not in _TRACK_PARAMS})
    except Exception:
        keys = []
    if keys:
        tmpl += "?" + "&".join(keys)
    return tmpl


def extract_links(html: str, base_url: str) -> list:
    """HTML -> 同 host 归一 URL 列表（保序去重）。纯正则，不引 bs4。"""
    out, seen = [], set()
    base_host = (_host_of(base_url) if base_url else "")
    for m in re.finditer(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", html or "", re.I):
        n = normalize_url(m.group(1), base_url)
        if not n or n in seen:
            continue
        if base_host and _host_of(n) != base_host:
            continue
        if _looks_like_asset(n):
            continue
        seen.add(n)
        out.append(n)
    return out


_ASSET_EXT = frozenset((
    "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "avif",
    "css", "js", "mjs", "map", "woff", "woff2", "ttf", "otf", "eot",
    "mp4", "m4v", "mov", "avi", "mkv", "flv", "ts", "m3u8", "mpd",
    "mp3", "m4a", "aac", "flac", "wav", "ogg", "zip", "rar", "7z", "gz", "pdf",
))


def _looks_like_asset(u: str) -> bool:
    try:
        path = urlsplit(u).path or ""
    except Exception:
        return False
    if "." not in path.rsplit("/", 1)[-1]:
        return False
    return path.rsplit(".", 1)[1].lower() in _ASSET_EXT


def _host_of(u: str) -> str:
    """URL 或裸 host -> 纯 host（小写、去端口）。**本地实现**，不依赖 video_grab
    —— Layer 1 不许把重型后端拖进来。语义与 vg._netloc_of 一致。"""
    s = (u or "").strip().lower()
    if not s:
        return ""
    try:
        if "://" in s:
            s = urlparse(s).netloc
    except Exception:
        return ""
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s.rstrip(".")


# ================================================================
# Layer 1-b：前端指纹
# ================================================================
_FRAMEWORK_MARKERS = (
    ("nextjs",   (r"__NEXT_DATA__", r"/_next/static")),
    ("nuxt",     (r"window\.__NUXT__", r"/_nuxt/")),
    ("vue-ssr",  (r"__INITIAL_STATE__", r"data-server-rendered")),
    ("react",    (r'id\s*=\s*["\']root["\']', r"react-dom", r"__REACT_DEVTOOLS")),
    ("angular",  (r"ng-version", r"ng-app", r"angular\.min\.js")),
    ("gatsby",   (r"___gatsby", r"/page-data/")),
    ("shopify",  (r"cdn\.shopify\.com", r"Shopify\.theme")),
    ("wordpress", (r"wp-content", r"wp-includes", r"/wp-json/")),
    ("jquery",   (r"jquery[\.-]min\.js", r"jquery-\d")),
    ("element-ui", (r"element-ui", r"el-button")),
)

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
_SCRIPT_SRC_OPEN_RE = re.compile(r"<script\b[^>]*\bsrc\s*=", re.I)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style\s*>", re.I | re.S)
_VISIBLE_STRIP_RE = re.compile(
    r"<(script|style|noscript|head|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_LOGIN_HINTS = (r'type\s*=\s*["\']password["\']', r"登录后查看", r"请先登录", r"登陆后可见",
                r"sign\s*in\s*to\s*continue", r"login\s*required")
# 尾部那个分组必须**非捕获**：findall 遇到多捕获组会返回元组，
# 而调用方拿的是扩展名字符串（踩过：'tuple' object has no attribute 'lower'）。
_MEDIA_EXT_HINT = re.compile(r"\.(m3u8|mpd|mp4|webm|mp3|m4a|flac)(?:\?|$|[\"'])", re.I)


def _visible_text(html: str) -> str:
    """剥 script/style/head 后的可见文本。**本地实现**，不引 qiongqi_maw
    —— qm 拉的是 bs4，Layer 1 必须能在没装浏览器栈的环境里跑。"""
    s = _VISIBLE_STRIP_RE.sub(" ", html or "")
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _script_chars(html: str) -> int:
    n = 0
    for m in _SCRIPT_RE.finditer(html or ""):
        if _SCRIPT_SRC_OPEN_RE.search(m.group(0)):
            continue                       # 外链脚本不计「内联脚本量」
        n += len(m.group(2) or "")
    return n


def _title_of(html: str, url: str) -> str:
    """标题：尽量复用 vg.extract_title（它对 og:title 的处理更全），失败退回本地正则。"""
    try:
        import video_grab as vg
        t = vg.extract_title(html, url)
        if t:
            return str(t)
    except Exception:
        pass
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


# 这两张表**必须分开**，混在一起会让诊断给出与事实相反的结论（2026-09-11 真机抓出来的）。
#
# **整页插页特征** —— 出现这些，说明「你要的页面根本没拿到，横在中间的是验证墙」。
# 只有这一类才配叫「挑战页」，才配抬难度档位、才配改可行性判定。
_CHALLENGE_INTERSTITIAL = ("just a moment", "cf-challenge", "cf-browser-verification",
                           "checking your browser", "verify you are human",
                           "请稍候", "正在验证")

# **组件引用特征** —— 页面**引用了**验证码/防护组件，不代表它拦了你。
# 带登录框的正常页面几乎必然命中（bilibili 首页就引了 geetest 的登录 SDK）。
# 这一类只能走「旁证」：`C_FRONT_CAPTCHA_SDK`，**不进 `_OBS_TO_LEVEL`、不参与可行性判定** ——
# 与 `site_knowledge` 的 `C_KNOW_CHALLENGE` 是同一条纪律（知识是旁证，观测才是证据）。
#
# 归类的判据只有一条：**这个串只有在"整页就是验证墙"时才出现，还是正常页面也会出现？**
#   - `challenge-platform` / `turnstile`（CF 的脚本与隐形组件）、`incapsula` / `visid_incap`
#     （Imperva 的探针脚本与 cookie 名）在**正常页面**上都会跑 → 引用类。
#   - 真正可靠的"整页插页"信号是**状态码那条线**（`probe_http` 的 403 归因 →
#     `risk.http.challenge_auto/manual` 进 `_OBS_TO_LEVEL`）——那才是观测。
_CAPTCHA_SDK_HINTS = ("challenge-platform", "turnstile", "incapsula", "visid_incap",
                      "g-recaptcha", "h-captcha", "geetest")


def fingerprint_html(html: str, url: str, final_url: str = "") -> dict:
    """静态前端指纹（纯函数）。返回 dict，键与 09-09 方案一致并扩展。"""
    html = html or ""
    text = _visible_text(html)
    sc = _script_chars(html)
    low = html[:400000].lower()

    hits = []
    for name, pats in _FRAMEWORK_MARKERS:
        for p in pats:
            if re.search(p, html, re.I):
                hits.append({"name": name, "marker": p, "found": True})
                break

    # 只认「整页插页特征」—— 组件引用见 `captcha_sdk_hint`，那是旁证不是证据。
    challenge_hint = ""
    for h in _CHALLENGE_INTERSTITIAL:
        if h in low:
            challenge_hint = h
            break
    title_low = (_title_of(html, url) or "").lower()
    if not challenge_hint and ("just a moment" in title_low or "attention required" in title_low):
        challenge_hint = "title"

    captcha_sdk_hint = ""
    for h in _CAPTCHA_SDK_HINTS:
        if h in low:
            captcha_sdk_hint = h
            break

    login_wall = ""
    for p in _LOGIN_HINTS:
        if re.search(p, html, re.I):
            login_wall = p
            break

    media_hits = _MEDIA_EXT_HINT.findall(html[:400000])
    media_signals = {
        "json_ld_video": bool(re.search(r'"@type"\s*:\s*"(VideoObject|Movie|TVEpisode|AudioObject)"',
                                        html, re.I)),
        "og_video": bool(re.search(r'(?:property|name)\s*=\s*["\']og:video', html, re.I)),
        "og_audio": bool(re.search(r'(?:property|name)\s*=\s*["\']og:audio', html, re.I)),
        "inline_m3u8": bool(re.search(r"\.m3u8", html, re.I)),
        "inline_mpd": bool(re.search(r"\.mpd\b", html, re.I)),
        "media_ext_n": len({x.lower() for x in media_hits}),
    }

    # ── SSR / SPA 判定 ──
    # 顺序要紧：**挑战页先判**，否则挑战壳（正文≈0 + 大段脚本）会被误判成 SPA，
    # 报告就会给出一条完全跑偏的建议。
    text_len, html_len = len(text), len(html)
    reason = ""
    if challenge_hint:
        spa_guess = "challenge"
        reason = f"命中挑战页特征：{challenge_hint}"
    elif text_len < 400 and sc > 3000 and re.search(r'id\s*=\s*["\'](__next|root|app)["\']', html, re.I):
        spa_guess = "spa"
        reason = f"可见正文仅 {text_len} 字符而内联脚本 {sc} 字符，且存在空壳挂载点"
    elif any(h["name"] in ("nextjs", "nuxt", "vue-ssr", "gatsby") for h in hits) and text_len >= 400:
        spa_guess = "ssr"
        reason = "含服务端渲染数据载荷且正文充实"
    elif text_len >= 400:
        spa_guess = "static"
        reason = "正文充实且无框架载荷标记"
    elif html_len and text_len == 0:
        spa_guess = "unknown"
        reason = "剥脚本后正文为空（可能是纯 SPA、纯图页或空壳）"
    else:
        spa_guess = "unknown"
        reason = "信号不足以判定"

    return {
        "ok": bool(html),
        "url": url,
        "final_url": final_url or url,
        "title": _title_of(html, url),
        "html_len": html_len,
        "script_chars": sc,
        "body_text_len": text_len,
        "framework_hits": hits,
        "spa_guess": spa_guess,
        "spa_reason": reason,
        "media_signals": media_signals,
        "challenge_hint": challenge_hint,
        "captcha_sdk_hint": captcha_sdk_hint,
        "login_wall_hint": login_wall,
    }


# ================================================================
# Layer 1-c：请求汇总
# ================================================================
_CT_KIND = (
    ("json",  (r"json",)),
    ("html",  (r"text/html", r"xhtml")),
    ("js",    (r"javascript", r"ecmascript")),
    ("css",   (r"text/css",)),
    ("media", (r"video/", r"audio/", r"mpegurl", r"dash\+xml", r"octet-stream")),
    ("image", (r"image/",)),
    ("text",  (r"text/plain", r"xml", r"x-www-form-urlencoded")),
)


def ct_kind(ct: str) -> str:
    """Content-Type -> 粗类。未知/空 -> 'other'。"""
    s = (ct or "").lower()
    if not s:
        return "other"
    for kind, pats in _CT_KIND:
        for p in pats:
            if p in s:
                return kind
    return "other"


def _same_site(h1: str, h2: str) -> bool:
    """同一站点的判定：相等，或一个是另一个的子域。

    不做 PSL（公共后缀表）—— 那要拖一个外部依赖，而这里只需要"别把本站 CDN 报成第三方"
    这一个效果。`cdn.a.com` vs `a.com` → 同站；`third.com` → 第三方。
    多一个尾点是常见笔误，先归一。
    """
    a, b = (h1 or "").strip(".").lower(), (h2 or "").strip(".").lower()
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def summarize_requests(rows, *, page_url: str = "") -> dict:
    """请求列表 -> 后端汇总（纯函数）。page_url 用于判同源/第三方。"""
    rows = list(rows or [])
    page_host = _host_of(page_url) if page_url else ""
    by_host, status, types = {}, {}, {}
    json_apis, seen_api = [], set()
    media_urls, seen_media = [], set()
    third_party, seen_tp = [], set()
    auth_keys = set()

    for r in rows:
        u = getattr(r, "url", "") or (r.get("url") if isinstance(r, dict) else "") or ""
        if not u:
            continue
        st = int(getattr(r, "status", 0) or (r.get("status", 0) if isinstance(r, dict) else 0) or 0)
        ct = getattr(r, "content_type", "") or (r.get("content_type", "") if isinstance(r, dict) else "") or ""
        method = (getattr(r, "method", "") or (r.get("method", "") if isinstance(r, dict) else "") or "GET").upper()
        host = _host_of(u)
        k = ct_kind(ct)

        h = by_host.setdefault(host, {"total": 0, "by_status": {}, "json": 0, "media": 0})
        h["total"] += 1
        h["by_status"][str(st)] = h["by_status"].get(str(st), 0) + 1
        if k == "json":
            h["json"] += 1
        if k == "media":
            h["media"] += 1

        if st == 0:
            status["opaque_0"] = status.get("opaque_0", 0) + 1
        else:
            status[str(st)] = status.get(str(st), 0) + 1
        if 400 <= st < 500:
            status["4xx"] = status.get("4xx", 0) + 1
        elif 500 <= st < 600:
            status["5xx"] = status.get("5xx", 0) + 1

        types[k] = types.get(k, 0) + 1

        if k == "json" and st and st != 0:
            key = (method, normalize_url(u) or u)
            if key not in seen_api:
                seen_api.add(key)
                json_apis.append({"method": method, "url": u, "status": st, "host": host})
        if k == "media":
            n = normalize_url(u) or u
            if n not in seen_media:
                seen_media.add(n)
                media_urls.append(u)
        if page_host and host and not _same_site(host, page_host):
            if host not in seen_tp:
                seen_tp.add(host)
                third_party.append(host)

    return {
        "total": len(rows),
        "by_host": by_host,
        "status": status,
        "types": types,
        "json_apis": json_apis,
        "auth_header_keys": sorted(auth_keys),
        "media_urls": media_urls,
        "third_party": third_party,
    }


def merge_header_keys(summary: dict, keys) -> dict:
    """把浏览器阶段收到的请求头键名并进汇总（键名来自注入 JS，只有键没有值）。"""
    s = set(summary.get("auth_header_keys") or ())
    for k in (keys or ()):
        kk = str(k or "").strip().lower()
        if kk:
            s.add(kk)
    summary["auth_header_keys"] = sorted(s)
    return summary


# ================================================================
# Layer 1-d：robots.txt 解析
#
# 用户口径：「诊断系统把 robots.txt 抓下来，这样我才知道底线在哪里，要不然做事没有规矩」。
# 所以这里**不是**一个自动跳过的闸 —— 是「把规矩摊开 + 标出本次探测的路径落在哪一类」。
# 默认不自动跳过；要改成真跳过，把 robots_policy="enforce" 传进来即可（信息已备齐）。
# ================================================================
_ROBOTS_MAX_CHARS = 65536

# 路径匹配：* 匹配任意序列，$ 匹配结尾（RFC 9309）。转义其余字符。
_RB_SPECIAL = re.compile(r"[.+^${}()|\[\]\\]")


def _robots_path_re(pattern: str):
    pat = (pattern or "").strip()
    if not pat:
        return None
    body = pat
    anchored = body.endswith("$")
    if anchored:
        body = body[:-1]
    out = []
    for ch in body:
        if ch == "*":
            out.append(".*")
        else:
            out.append(_RB_SPECIAL.sub(r"\\\g<0>", ch))
    expr = "^" + "".join(out) + ("$" if anchored else "")
    try:
        return re.compile(expr)
    except re.error:
        return None


def parse_robots(text: str) -> dict:
    """robots.txt 原文 -> 结构化规矩（纯函数）。

    给不出规矩时**返回「无声明」而不是报错** —— 404 / 空文件 / 格式乱都是合法状态，
    诊断不能因此失败。
    """
    raw = text or ""
    out = {"ok": False, "raw": raw[:_ROBOTS_MAX_CHARS], "truncated": len(raw) > _ROBOTS_MAX_CHARS,
           "groups": [], "sitemaps": [], "errors": [], "empty": not raw.strip()}
    if not raw.strip():
        return out

    groups, cur = [], None
    seen_agent_in_group = False
    for i, line in enumerate(raw.splitlines()):
        s = line.split("#", 1)[0].strip()
        if not s or ":" not in s:
            continue
        key, _, val = s.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "user-agent":
            if cur is None or seen_agent_in_group:
                cur = {"agents": [], "rules": [], "crawl_delay": "", "raw_line": i + 1}
                groups.append(cur)
                seen_agent_in_group = False
            cur["agents"].append(val.lower() or "*")
        elif key in ("disallow", "allow"):
            if cur is None:
                cur = {"agents": ["*"], "rules": [], "crawl_delay": "", "raw_line": i + 1}
                groups.append(cur)
            seen_agent_in_group = True
            cur["rules"].append({"type": key, "path": val})
        elif key == "crawl-delay":
            if cur is None:
                cur = {"agents": ["*"], "rules": [], "crawl_delay": "", "raw_line": i + 1}
                groups.append(cur)
            seen_agent_in_group = True
            cur["crawl_delay"] = val
        elif key == "sitemap":
            if val:
                out["sitemaps"].append(val)
        elif key in ("host", "request-rate", "clean-param"):
            pass
        else:
            out["errors"].append(f"第 {i + 1} 行：未知指令 {key}")

    out["groups"] = groups
    out["ok"] = bool(groups) or bool(out["sitemaps"])
    return out


def _rb_group_matches(agents, agent: str) -> bool:
    a = (agent or "*").lower()
    return any(x == "*" or x == a for x in (agents or ()))


def robots_check(parsed: dict, path: str, *, agent: str = "*") -> dict:
    """某条路径的合规判定（纯函数）。

    返回 {"state": "allowed"|"disallowed"|"none", "rule": 命中的那条, "group_agents": [...]}。
    冲突按 **最长匹配优先**；长度相同则 Allow 胜（RFC 9309）。
    命中 `Disallow:` 且路径为空 = 全站放行（robots 里写 `Disallow:` 空值就是"不禁止"）。
    """
    p = path or "/"
    if "?" in p:
        p = p.split("?", 1)[0]
    if not p.startswith("/"):
        p = "/" + p

    if not parsed or not parsed.get("groups"):
        return {"state": "none", "rule": None, "group_agents": []}

    best = None                      # (match_len, is_allow, rule, agents)
    for g in parsed["groups"]:
        if not _rb_group_matches(g.get("agents"), agent):
            continue
        for rule in g.get("rules") or ():
            pat = rule.get("path") or ""
            if pat == "" and rule.get("type") == "disallow":
                continue             # Disallow: 空值 = 不禁止任何东西
            rx = _robots_path_re(pat)
            if rx is None:
                continue
            m = rx.search(p)
            if not m:
                continue
            is_allow = (rule.get("type") == "allow")
            cand = (len(pat), is_allow, rule, g.get("agents"))
            if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                best = cand

    if best is None:
        return {"state": "allowed", "rule": None, "group_agents": []}
    if best[1]:
        return {"state": "allowed", "rule": best[2], "group_agents": list(best[3] or ())}
    return {"state": "disallowed", "rule": best[2], "group_agents": list(best[3] or ())}


def robots_page_rules(parsed: dict, url: str, *, agent: str = "*") -> dict:
    """把「本页 URL 的合规判定」+「整站声明的规矩摘要」打包（纯函数）。"""
    try:
        sp = urlsplit(url or "")
        path = sp.path or "/"
    except Exception:
        path = "/"
    chk = robots_check(parsed, path, agent=agent)
    return {
        "path": path,
        "state": chk.get("state"),
        "matched_rule": chk.get("rule"),
        "matched_group": chk.get("group_agents"),
        "declared": bool(parsed and parsed.get("ok")),
        "absent": (not parsed) or (not parsed.get("ok")),
        "sitemaps": list((parsed or {}).get("sitemaps") or ()),
        "groups": list((parsed or {}).get("groups") or ()),
        "raw": (parsed or {}).get("raw", ""),
        "truncated": bool((parsed or {}).get("truncated")),
        "errors": list((parsed or {}).get("errors") or ()),
    }


# ================================================================
# Layer 1-b：L1 导览层 / L2 结构归纳层（纯函数，零请求）
#
# 「覆盖率来自结构」全靠这一段：几万条 URL 归约成几十个模板，
# 后面的深诊量由**模板数**决定，不由站点总页数决定。
# ================================================================
_LOC_RE = re.compile(r"<loc\b[^>]*>(.*?)</loc>", re.I | re.S)
_SITEMAP_INDEX_RE = re.compile(r"<sitemapindex\b", re.I)
_URLSET_RE = re.compile(r"<urlset\b", re.I)

# XML 实体：**单趟替换**，所以 `&amp;lt;` 不会被二次解成 `<`。
# 必须带分号才认 —— 与 vg._safe_unescape 同一个理由：`&times` 这种没有分号的
# 只要被解成 `×`，签名 URL 就被改坏了。
_ENT_RE = re.compile(r"&(?:#x([0-9a-fA-F]+)|#(\d+)|(amp|lt|gt|quot|apos));")
_NAMED_ENT = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _xml_unescape(s: str) -> str:
    """XML 实体还原，**单趟**（防二次解码）。"""
    def _rep(m):
        try:
            if m.group(1):
                return chr(int(m.group(1), 16))
            if m.group(2):
                return chr(int(m.group(2)))
        except Exception:
            return m.group(0)
        return _NAMED_ENT.get(m.group(3) or "", m.group(0))
    return _ENT_RE.sub(_rep, s or "")


def _looks_like_sitemap(u: str) -> bool:
    """URL 看着像"另一份 sitemap"（.xml / .xml.gz）而不是一个页面。"""
    try:
        p = (urlsplit(u).path or "").lower()
    except Exception:
        return False
    return p.endswith(".xml") or p.endswith(".xml.gz")


def parse_sitemap(text: str, *, max_locs: int = 100000) -> dict:
    """sitemap / sitemap index -> {"kind", "locs", "truncated", "n"}（纯函数）。

    只认 `<loc>`，**刻意不读 lastmod/priority/changefreq** —— 诊断要的是「有哪些 URL」，
    不是「站点自认为哪个重要」（那是站点自己的排序，不是事实）。

    kind 判定：有 `<sitemapindex>` → index；有 `<urlset>` → urlset；
    标签都没了（XML 被截断/站点写坏）就按 loc 的**外形**兜底判一次 ——
    「全是 .xml」当 index，否则当 urlset。判不出来给 unknown，绝不硬猜。
    """
    s = text or ""
    raw, truncated = [], False
    for m in _LOC_RE.finditer(s):
        if len(raw) >= max_locs:
            truncated = True
            break
        raw.append(m.group(1))
    locs, seen = [], set()
    for x in raw:
        u = normalize_url(_xml_unescape(x).strip())
        if u and u not in seen:
            seen.add(u)
            locs.append(u)
    if _SITEMAP_INDEX_RE.search(s):
        kind = "index"
    elif _URLSET_RE.search(s):
        kind = "urlset"
    elif locs and all(_looks_like_sitemap(u) for u in locs):
        kind = "index"
    elif locs:
        kind = "urlset"
    else:
        kind = "unknown"
    return {"kind": kind, "locs": locs, "truncated": truncated, "n": len(locs)}


# 与 well-known 路径同级的兜底：robots 里没写 Sitemap: 时才试，试不着就算没有。
_DEFAULT_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                          "/sitemap.xml.gz")


def sitemap_seeds(parsed_robots, base_url: str) -> list:
    """L1 的种子（纯函数）：robots 声明的 Sitemap: 行优先；没有才试 well-known 路径。

    返回 [(url, 来源)]，来源 ∈ {"robots", "wellknown"} —— 报告里要能说清
    「这份 sitemap 是站点自己声明的」还是「我们自己猜的」，这两件事分量不同。
    """
    out, seen = [], set()
    for sm in ((parsed_robots or {}).get("sitemaps") or ()):
        u = normalize_url(str(sm or "").strip())
        if u and u not in seen:
            seen.add(u)
            out.append((u, "robots"))
    if out:
        return out
    try:
        sp = urlsplit(base_url or "")
        if not sp.scheme or not sp.netloc:
            return out
        root = f"{sp.scheme}://{sp.netloc}"
    except Exception:
        return out
    for p in _DEFAULT_SITEMAP_PATHS:
        u = normalize_url(root + p)
        if u and u not in seen:
            seen.add(u)
            out.append((u, "wellknown"))
    return out


# 列表页的 query 键名（决定「这模板是列表页还是详情页」）。
# 宁可多收几个：判成 list 只是多花一次有界的翻页试探，判漏了会答不出「能不能穷举」。
_LIST_Q_KEYS = frozenset((
    "page", "p", "pg", "pn", "pageno", "pageindex", "pagenum", "page_num", "pages",
    "curpage", "cur_page", "current", "cur", "offset", "start", "from", "begin",
    "s", "skip", "pagenation",
))


def template_kind(tmpl: str) -> str:
    """模板 -> 页面类型**猜测**：list / detail / channel / other。

    这是排序与试探的**依据**，不是结论 —— 报告里写的是模板本身与条数，
    类型只用来决定「谁先探、谁值得做翻页试探」。猜错的代价是有限的。
    """
    t = tmpl or ""
    path, _, query = t.partition("?")
    if {k for k in query.split("&") if k} & _LIST_Q_KEYS:
        return "list"
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "channel"
    last = segs[-1]
    if "{id}" in last or "{slug}" in last:
        return "detail"
    if len(segs) <= 2:
        return "channel"
    return "other"


def build_structure_map(urls, *, max_samples: int = 3, sources=None) -> dict:
    """URL 全集 -> **结构地图**（纯函数，0 请求）。这是「穿透整个网站」的地基。

    返回：
      templates   [{tmpl, n, samples[≤max_samples], kind, depth, query_keys}]，n 降序
      by_template {tmpl: [url, ...]}  **全量**，供逐条走完用；渲染只读 samples，
                  所以报告不会被几万条 URL 撑爆
      urls_n      去重后的 URL 总数
      sources     这批 URL 从哪来（sitemap / 首页链接）—— 报告要如实写清
    """
    by = {}
    seen = set()
    n_in = 0
    for u in (urls or ()):
        n_in += 1
        if not u or u in seen:
            continue
        seen.add(u)
        t = url_template(u)
        if not t:
            continue
        by.setdefault(t, []).append(u)
    tmpls = []
    for t, lst in by.items():
        segs = [s for s in t.partition("?")[0].split("/") if s]
        qk = sorted({k for k in t.partition("?")[2].split("&") if k})
        tmpls.append({"tmpl": t, "n": len(lst), "samples": list(lst[:max(1, max_samples)]),
                      "kind": template_kind(t), "depth": len(segs), "query_keys": qk})
    # n 降序（大头先覆盖），同数量按模板名定序 —— 顺序必须可复现，否则用例会抖
    tmpls.sort(key=lambda x: (-x["n"], x["tmpl"]))
    return {"templates": tmpls, "by_template": by, "urls_n": len(seen),
            "urls_in": n_in, "sources": dict(sources or {})}


def select_representatives(structure, *, per_template: int = 2,
                           max_templates: int = 0, max_pages: int = 0) -> dict:
    """结构地图 -> 代表页清单（纯函数）。

    **轮转取材**：先每个模板各取一个，再回头取第二个 —— 预算中途耗尽时保证
    「每个模板至少有一个代表页」，而不是前几个模板各来两个、后面一个没有。
    这一点直接决定覆盖率的形状，不是实现细节。

    per_template <= 0 且 max_templates <= 0 表示「全部」档：逐模板把它**所有的** URL
    都排上（这时候轮转没有意义，反正每条都要走）。max_pages=0 表示不限。
    """
    st = structure or {}
    tmpls = [t for t in (st.get("templates") or ()) if isinstance(t, dict)]
    tmpls.sort(key=lambda x: (-x.get("n", 0), x.get("tmpl", "")))
    if max_templates and max_templates > 0:
        tmpls = tmpls[:max_templates]
    by = st.get("by_template") or {}
    picks, truncated = [], False

    def _add(t, u):
        picks.append({"tmpl": t.get("tmpl", ""), "kind": t.get("kind", ""), "url": u,
                      "n": t.get("n", 0)})

    if per_template <= 0:
        for t in tmpls:
            for u in (by.get(t.get("tmpl")) or t.get("samples") or ()):
                if max_pages and len(picks) >= max_pages:
                    truncated = True
                    break
                _add(t, u)
            if truncated:
                break
    else:
        for rnd in range(per_template):
            for t in tmpls:
                smp = list(t.get("samples") or ())
                if rnd >= len(smp):
                    continue
                if max_pages and len(picks) >= max_pages:
                    truncated = True
                    break
                _add(t, smp[rnd])
            if truncated:
                break
    done = {p["tmpl"] for p in picks}
    return {"picks": picks, "truncated": truncated,
            "templates_n": len(tmpls),
            "templates_untouched": [t["tmpl"] for t in tmpls if t["tmpl"] not in done]}


# ================================================================
# Layer 1-c：L3 穿透层 —— 翻页探针（纯函数部分）
#
# 本项目在这一层踩得最狠：网易云的 s/offset/n/limit **全被忽略、静默翻不了页**，
# 酷狗搜索页**翻页根本没做**。两次都是「HTTP 200 + 合法内容 + 内容是第一页」。
# ================================================================
_PAGE_KEYS = frozenset(("page", "p", "pg", "pn", "pageno", "pageindex", "pagenum",
                        "page_num", "curpage", "cur_page", "current", "offset",
                        "start", "s", "skip"))


def pagination_candidates(list_url: str, *, depth: int = 3) -> list:
    """纯函数：给出「下一页」的候选 URL（保序去重）。

    真实站点的翻页就三种形态，全在这里：
      1. query 里**已经有**页码键 → 把它 +1（`?page=1` → `?page=2`）
      2. query 里没有 → 直接拼 `?page=N` / `?p=N` / `?pg=N`
      3. 路径式 → `/news/index_2.html` `/news/2.html`

    只构造**同 host 同路径**的变体，绝不跳到别的路径去。
    """
    try:
        sp = urlsplit(list_url or "")
    except Exception:
        return []
    if not sp.netloc:
        return []
    out, seen = [], set()

    def _add(u):
        n = normalize_url(u)
        if n and n not in seen:
            seen.add(n)
            out.append(n)

    try:
        pairs = parse_qsl(sp.query, keep_blank_values=True)
    except Exception:
        pairs = []
    hit = next((i for i, (k, _) in enumerate(pairs) if (k or "").lower() in _PAGE_KEYS), None)
    steps = max(1, int(depth or 1))
    if hit is not None:
        k, v = pairs[hit]
        try:
            base = int(v or 1)
        except Exception:
            base = 1
        for step in range(1, steps + 1):
            np_ = list(pairs)
            np_[hit] = (k, str(base + step))
            _add(urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(np_, doseq=True), "")))
    else:
        for key in ("page", "p", "pg"):
            for step in range(2, steps + 2):
                np_ = list(pairs) + [(key, str(step))]
                _add(urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(np_, doseq=True), "")))
        path = sp.path or "/"
        if path.endswith("/") or path.lower().endswith("/index.html"):
            head = path if path.endswith("/") else path[: -len("index.html")]
            for step in range(2, steps + 2):
                _add(urlunsplit((sp.scheme, sp.netloc, head + f"index_{step}.html", "", "")))
                _add(urlunsplit((sp.scheme, sp.netloc, head + f"{step}.html", "", "")))
    return out


def page_signature(html: str, base_url: str = "") -> dict:
    """页面内容签名（纯函数）—— 用来判断「翻页到底有没有换内容」。

    **刻意不用整文哈希**：同一页两次请求会因为时间戳/随机广告/CSRF token 而不同，
    那样每个站的翻页都会被判成「有效」，这个探针就等于没做。
    用**同 host 链接集合**做判据：列表页换了页就该换一批链接，这是最强的信号；
    正文长度只作辅证（按 50 字符分桶，容忍一两条广告的抖动）。
    """
    h = html or ""
    links = extract_links(h, base_url)
    return {"links": frozenset(links), "links_n": len(links),
            "text_bucket": len(_visible_text(h)) // 50}


def same_page_content(sig1, sig2) -> dict:
    """两个签名是否「同一页内容」。

    **首页没有链接时判不了** —— 返回 unknown，绝不因为两个空集合相等就宣布「翻页无效」
    （那是拿"没得比"当"比过了"）。

    比出「不一样」还不够：**得看是哪种不一样**。三种结果强弱不同，混为一谈就会说出
    一句证不了的话（2026-09-11 那次「弱信号当证据」的教训）：

      same      —— 链接集合完全一致。**强信号**：页码确实被忽略了。
      different —— 出现了第 1 页**没有的**链接。**强信号**：翻页确实换了一批内容。
      weak      —— 集合变了，但**一条新链接都没有**（只少了、没多）。首页/栏目页本来就会
                   抖（推荐位、轮播、时间戳、A/B 位），所以这个变化**证明不了是翻页造成的**
                   —— 宁可说不知道，不可说一句它证不了的话。
    """
    if not sig1 or not sig2:
        return {"verdict": "unknown", "same_links": None, "new_links_n": 0, "why": "缺签名"}
    if not sig1.get("links_n"):
        return {"verdict": "unknown", "same_links": None, "new_links_n": 0,
                "why": "该页没有同 host 链接可比（可能是纯 JS 渲染的列表），判不了"}
    a, b = set(sig1["links"]), set(sig2["links"])
    if a == b:
        return {"verdict": "same", "same_links": True, "new_links_n": 0, "why": ""}
    new = b - a
    if not new:
        return {"verdict": "weak", "same_links": False, "new_links_n": 0,
                "why": f"链接集合变了，但只有少的没有多的（少 {len(a - b)} 条、"
                       f"新增 0 条）—— 这**证明不了**是翻页造成的"}
    return {"verdict": "different", "same_links": False, "new_links_n": len(new), "why": ""}


# ================================================================
# Layer 1-e：难度分级
# ================================================================
LEVEL_ORDER = ("D0", "D1", "D2", "D3", "D4")
LEVEL_NAME = {"D0": "open", "D1": "easy", "D2": "high", "D3": "paranoid",
              "D4": "extreme", "??": "unknown"}

_ANTI_TO_LEVEL = {"extreme": "D4", "paranoid": "D3", "high": "D2",
                  "medium": "D1", "low": "D1"}

# 观测 -> 档位贡献。合成规则：**只上调**（取最高）。
#
# **刻意不收 C_RULES_HIT（robots 命中禁止）**：难度档位回答的是「抓下来有多难」，
# 而 robots 命中回答的是「该不该抓」—— 两把尺子，混在一起会让报告不说实话
# （一个 robots 写得严、实际毫无反爬的静态站，会被报成 D2 高难度）。
# robots 的结论走它自己那条线：findings 里 high 级 + 建议里点明 + 报告单列一节。
#
# **P3 的七个 site.* 码同样一个都不进这张表**（D39 用例钉死）。理由与 robots 逐条同源：
#   · C_SITE_NO_PAGE   —— 页码被忽略答的是「能不能**穷举**」，不是「站方在防你」。
#                         一个零反爬的静态站只要分页用了前端路由，就会被报成高难度 ——
#                         正是上面 robots 那个错误的翻版。
#   · C_SITE_BUDGET / C_SITE_PAGINATION / C_SITE_STRUCTURE / C_SITE_SITEMAP /
#     C_SITE_NOSITEMAP —— 全是关于**我们自己走了多远**的账目，不是站点的属性。
#   · C_SITE_ABORT429  —— 429 这件事本身已经由页级的 C_RATE_LIMIT 抬档（表里有），
#                         再挂一条是同一个事实重复计权。
# 判断口径一句话：**这条证据是"站方怎么样"还是"我们怎么样"？只有前者配抬档。**
_OBS_TO_LEVEL = {
    C_CHALLENGE_MAN:  "D4",
    C_TLS_RESCUED:    "D3",
    C_CHALLENGE_AUTO: "D3",
    C_RATE_LIMIT:     "D2",
    C_AUTH_WALL:      "D2",
    C_HOTLINK:        "D2",
    C_HTTP_5XX:       "D1",
    C_FRONT_CHALLENGE: "D2",
}


def match_hard_site(url_or_host: str, *, hard_cfg=None):
    """host -> HARD_SITE_CONFIG 条目（**该表 url_patterns 的第一个消费方**）。

    铁律：**命中不到返回 None，绝不回退 "default"** —— 那张表的 default 是
    anti_level=extreme，回退它等于把每个陌生站都判成究极对抗。

    匹配用最长后缀赢（`a.example.com` 命中 `example.com`；`badexample.com` 不命中）。
    """
    host = _host_of(url_or_host)
    if not host:
        return None
    cfg = _hard_cfg_source(hard_cfg)
    if not cfg:
        return None
    best = None
    for key, entry in cfg.items():
        if key == "default" or not isinstance(entry, dict):
            continue
        for pat in (entry.get("url_patterns") or ()):
            p = str(pat or "").strip().lower().lstrip(".").rstrip(".")
            if not p:
                continue
            if host == p or host.endswith("." + p):
                if best is None or len(p) > best[0]:
                    best = (len(p), key, entry, p)
    if best is None:
        return None
    return {"key": best[1], "matched_pattern": best[3], "config": best[2],
            "anti_level": str(best[2].get("anti_level") or "")}


def _hard_cfg_source(hard_cfg):
    """hard_cfg 参数优先；None -> 惰性读 config（纯函数层不做顶层 import）。"""
    if hard_cfg is not None:
        return hard_cfg if isinstance(hard_cfg, dict) else {}
    try:
        import config
        v = getattr(config, "HARD_SITE_CONFIG", None)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def grade_difficulty(*, findings=None, hard_site=None, reachable: bool = True) -> dict:
    """难度分级（纯函数，可断言）。

    合成规则：
      - 知识库命中（HARD_SITE_CONFIG）先给一个起始档；
      - 观测只能**上调**，绝不因为"看着挺宽松"而下调；
      - 历史**永不参与**（避免自我实现）；
      - 不变量：**任何高于 D0 的档位都必须至少有 1 条发现支撑**。
        这条由结构保证 —— 抬高档位的只有 findings，reasons 就是那些 findings。

    触达不到（DNS/TLS/连接失败）→ "??" / unknown：**判不了不等于容易**。
    """
    findings = list(findings or [])
    reasons, ev = [], {}
    level = "D0"
    source = "none"

    if hard_site:
        lv = _ANTI_TO_LEVEL.get(str(hard_site.get("anti_level") or "").lower())
        if lv:
            level = max(level, lv, key=LEVEL_ORDER.index)
            source = "knowledge"
            reasons.append(C_KNOW_HARDSITE)
            ev["hard_site_key"] = hard_site.get("key")
            ev["hard_site_level"] = hard_site.get("anti_level")

    if reachable:
        for f in findings:
            lv = _OBS_TO_LEVEL.get(getattr(f, "code", ""))
            if not lv:
                continue
            if LEVEL_ORDER.index(lv) > LEVEL_ORDER.index(level):
                level = lv
                if source != "knowledge":
                    source = "observation"
            reasons.append(getattr(f, "code", ""))
    elif not hard_site:
        level = "??"
        source = "observation"

    reasons = sorted(set(r for r in reasons if r))
    if level != "D0" and level != "??" and not reasons:
        # 结构上不该发生；真发生了就是分级逻辑坏了，宁可退回未知也不给无据的档位。
        level, source = "??", "observation"

    return {"level": level, "name": LEVEL_NAME.get(level, "unknown"),
            "source": source, "reasons": reasons, "evidence": ev}


# ================================================================
# Layer 2：HTTP 探针（唯一发网络请求的地方）
# ================================================================
def _cfg_get(name, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def _exc_kind(e) -> str:
    """连接级异常 -> dns / tls / timeout / conn。"""
    try:
        import requests
        ex = requests.exceptions
    except Exception:
        ex = None
    s = f"{type(e).__name__}: {e}".lower()
    if ex is not None and isinstance(e, ex.SSLError):
        return "tls"
    if ex is not None and isinstance(e, ex.Timeout):
        return "timeout"
    if "getaddrinfo" in s or "name or service not known" in s or "nodename nor servname" in s \
            or "no address associated" in s or "name resolution" in s:
        return "dns"
    if "ssl" in s or "certificate" in s or "tls" in s:
        return "tls"
    if "timed out" in s or "timeout" in s:
        return "timeout"
    return "conn"


def probe_http(url: str, *, timeout=None, referer: str = "", max_bytes=None,
               want_body: bool = True, allow_curl_fallback=True) -> dict:
    """状态码级探针：一次 GET 拿到 status / headers / body / 归因。

    这是 09-09 方案缺口的补丁 —— `vg.fetch_html` 把非 200 吞成空字符串，
    而 403 与 404 对诊断是完全不同的两件事。

    连接级失败时**只重试一次 curl_cffi**：curl 能通而 requests 不通 ⇒ 这是
    **TLS 指纹墙**（本项目踩过：forward 套站、酷狗证书真配错），是最强的单条信号。
    绝不调用任何 register_*，绝不改 host 健康表。
    """
    t0 = time.time()
    timeout = int(timeout if timeout is not None else _cfg_get("VIDEO_TIMEOUT", 20))
    max_bytes = int(max_bytes if max_bytes is not None else _cfg_get("DIAG_MAX_BYTES", 4 * 1024 * 1024))
    out = {
        "url": url, "final_url": "", "status": 0, "content_type": "", "content_length": "",
        "body": "", "body_len": 0, "sample": "",
        "server": "", "headers_subset": {},
        "error_kind": "", "error": "",
        "tls_rescued": False, "engine": "requests",
        "classification": "", "hint": "", "elapsed_ms": 0, "redirects": 0,
    }

    def _finish(resp, body_bytes, engine, err_kind="", err=""):
        out["engine"] = engine
        out["error_kind"] = err_kind
        out["error"] = err
        if resp is not None:
            try:
                out["status"] = int(getattr(resp, "status_code", 0) or 0)
                out["final_url"] = str(getattr(resp, "url", "") or url)
            except Exception:
                pass
            try:
                h = dict(getattr(resp, "headers", {}) or {})
            except Exception:
                h = {}
            hl = {k.lower(): v for k, v in h.items()}
            out["content_type"] = hl.get("content-type", "")
            out["content_length"] = hl.get("content-length", "")
            out["server"] = hl.get("server", "")
            out["headers_subset"] = {k: hl[k] for k in
                                     ("content-type", "content-length", "server",
                                      "location", "set-cookie", "cf-ray", "x-powered-by",
                                      "via", "x-cache", "strict-transport-security")
                                     if k in hl}
            out["redirects"] = 1 if out["final_url"] and out["final_url"] != url else 0
        if body_bytes:
            out["sample"] = body_bytes[:2048].decode("utf-8", "ignore")
            out["body_len"] = len(body_bytes)
            if want_body:
                out["body"] = _decode(body_bytes, out["content_type"])
        st = out["status"]
        # 归因只对**归因表认识的状态码**做 —— _classify_dl_403 对 403/406/451 之外的
        # 码一律回落成 hotlink，拿它去解释 500 会把"站方自己炸了"说成"防盗链"。
        if st in _CLASSIFY_CODES:
            out["classification"] = _classify_status(st, body_bytes)
            out["hint"] = _hint_for(st, out["classification"])
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        return out

    # ── 引擎选择：指纹 host 走 curl_cffi（与真实抓取一致） ──
    engine = "requests"
    try:
        import video_grab as vg
        if vg._imp_state_for(url) == "curl":
            engine = "curl"
        headers = vg._media_headers(url, referer, purpose="page")
        session_get = vg._http_session().get
    except Exception:
        import requests as _rq
        headers = {"User-Agent": _rq.utils.default_headers().get("User-Agent", "")}
        session_get = _rq.Session().get

    if engine == "curl":
        r = _curl_fetch(url, headers, timeout, max_bytes)
        if r is not None:
            return _finish(r[0], r[1], "curl_cffi")
        # curl 路径失败 → 退 requests 再试一次（下面的通用路径）

    # 注意：except 块结束时 Python 会删掉异常变量 e —— 后面还要用它，
    # 所以必须另存一份，不能靠 `except Exception as e:` 的 e 熬过这个块。
    kind, err_msg = "conn", ""
    resp = None
    try:
        resp = session_get(url, headers=headers,
                           timeout=(int(_cfg_get("VIDEO_CONNECT_TIMEOUT", 10)), timeout),
                           allow_redirects=True, stream=not want_body)
        body = _read_body(resp, max_bytes) if want_body else b""
        return _finish(resp, body, "requests")
    except Exception as e:
        kind, err_msg = _exc_kind(e), f"{type(e).__name__}: {e}"[:300]

    if allow_curl_fallback:
        r = _curl_fetch(url, headers, timeout, max_bytes)
        if r is not None:
            out2 = _finish(r[0], r[1], "curl_cffi")
            out2["tls_rescued"] = True
            out2["error_kind"] = ""
            out2["error"] = ""
            return out2

    return _finish(None, b"", engine, kind, err_msg)


def _read_body(resp, max_bytes: int) -> bytes:
    """有界读 body（迭代读，不全量进内存）。"""
    parts, size = [], 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            room = max_bytes - size
            if room <= 0:
                break
            parts.append(chunk[:room])
            size += len(parts[-1])
    except Exception:
        pass
    return b"".join(parts)


def _decode(raw: bytes, content_type: str = "") -> str:
    """字节 -> 文本。charset 优先，再 utf-8/gbk/latin-1。

    先看 gzip 魔数：sitemap 常见 `.xml.gz` —— 那是把文件本身压成 gzip，**不是**
    Content-Encoding，requests 不解它。不解的话 XML 到手就是乱码，`<loc>` 一条都抽不出来，
    **而且不报任何错**（整站导览会静默地只剩首页链接）。
    """
    if raw[:2] == b"\x1f\x8b":
        try:
            import gzip
            raw = gzip.decompress(raw)
        except Exception:
            pass
    charsets = []
    m = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type or "", re.I)
    if m:
        charsets.append(m.group(1))
    for enc in charsets + ["utf-8", "gbk", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def _curl_fetch(url: str, headers: dict, timeout: int, max_bytes: int):
    """curl_cffi 取一次。不可用/失败 → None（调用方决定退路）。"""
    try:
        import video_grab as vg
        cffi = vg._imp_cffi_module()
    except Exception:
        cffi = None
    if cffi is None:
        return None
    try:
        r = cffi.get(url, headers=headers, timeout=timeout,
                     allow_redirects=True, impersonate="chrome")
        body = b""
        try:
            body = (r.content or b"")[:max_bytes]
        except Exception:
            pass
        return r, body
    except Exception:
        return None


# 归因表认识的状态码。**这个集合必须与 vg._classify_dl_403 的分支对齐** ——
# 它对集合外的码回落成 hotlink，不设这道闸会把 500 说成防盗链。
_CLASSIFY_CODES = frozenset((401, 403, 406, 429, 451))


def _classify_status(code: int, body: bytes) -> str:
    """状态归因。**直接复用 vg._classify_dl_403**（纯函数、离线可测、关键词表来自
    qm.CHALLENGE_SIGNATURES），保证诊断与下载层对同一个 403 说同一句话。"""
    try:
        import video_grab as vg
        return str(vg._classify_dl_403(code, body) or "")
    except Exception:
        if code == 429:
            return "ratelimit"
        if code == 401:
            return "auth"
        return ""


def _hint_for(code: int, cls: str) -> str:
    """中文可执行提示。复用 vg._dl_failure_hint 的措辞口径。"""
    try:
        import video_grab as vg
        return str(vg._dl_failure_hint(vg._HTTPStatus("", code, body_sample=b"")) or "")
    except Exception:
        pass
    return {
        "ratelimit": f"限频 {code}：调大节拍或等待后再试",
        "auth": f"HTTP {code} 疑似需登录/授权",
        "challenge_auto": f"HTTP {code} 人机验证（可自动类）",
        "challenge_manual": f"HTTP {code} 触发滑块/人工验证码（红线不自动）",
        "hotlink": f"HTTP {code} 防盗链/签名失效",
    }.get(cls, f"HTTP {code}")


# ================================================================
# 注入 JS（站点诊断专用打点，**独立于 vg 的 _NET_PATCH_JS / _NET_COLLECT_JS**）
#
# 与 vg 打点的区别：vg 只记媒体 URL；这里记**全量**请求的 method/status/ct/样本。
# 两者并存互不干扰。setRequestHeader / fetch init 的 headers 只记**键名**不记值
# （防 token 落进报告）。全程 try 吞 —— 跨域读 body 会抛，绝不能因此弄坏页面。
# ================================================================
_DIAG_PATCH_JS = r"""
(function(){
  if (window.__taowuDiagPatch) return; window.__taowuDiagPatch = 1;
  var CAP = 3000;
  window.__diagRows = window.__diagRows || [];
  window.__diagHdrKeys = window.__diagHdrKeys || [];
  window.__diagEme = window.__diagEme || [];
  function push(o){ try{ if(window.__diagRows.length < CAP) window.__diagRows.push(o); }catch(e){} return o; }
  function hdr(k){ try{ k=String(k||'').toLowerCase(); if(k && window.__diagHdrKeys.indexOf(k)<0) window.__diagHdrKeys.push(k); }catch(e){} }
  function wantSample(ct){ ct=String(ct||'').toLowerCase();
    return /json|text\/|xml|javascript|x-www-form-urlencoded/.test(ct); }

  try{
    var XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send,
        XH = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function(m,u){
      try{ this.__d_m = String(m||'GET').toUpperCase(); this.__d_u = String(u||''); }catch(e){}
      return XO.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(k,v){
      try{ hdr(k); }catch(e){}
      return XH.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(){
      var self = this;
      try{
        self.addEventListener('loadend', function(){
          var ct='', s=0, sample='';
          try{ s = self.status|0; }catch(e){}
          try{ ct = self.getResponseHeader('Content-Type') || ''; }catch(e){}
          try{
            if(wantSample(ct) && self.responseType !== 'blob' && self.responseType !== 'arraybuffer'){
              sample = String(self.responseText||'').slice(0,2048);
            }
          }catch(e){}
          push({m:self.__d_m||'GET', u:self.__d_u||'', s:s, ct:ct, sample:sample,
                kind:'xhr', f:String(location.href||'')});
        });
      }catch(e){}
      return XS.apply(this, arguments);
    };
  }catch(e){}

  try{
    if(!window.__origFetch){ window.__origFetch = window.fetch; }
    var OF = window.__origFetch;
    window.fetch = function(input, init){
      var u='', m='GET';
      try{ u = (typeof input === 'string') ? input : ((input && input.url) || ''); }catch(e){}
      try{ m = String((init && init.method) || (input && input.method) || 'GET').toUpperCase(); }catch(e){}
      try{
        var hs = (init && init.headers) || null;
        if(hs){
          if(typeof hs.forEach === 'function'){ hs.forEach(function(v,k){ hdr(k); }); }
          else if(typeof hs === 'object'){ Object.keys(hs).forEach(function(k){ hdr(k); }); }
        }
      }catch(e){}
      var p = OF.apply(this, arguments);
      try{
        p.then(function(resp){
          var ct='', s=0;
          try{ s = resp.status|0; }catch(e){}
          try{ ct = (resp.headers && resp.headers.get && resp.headers.get('content-type')) || ''; }catch(e){}
          var row = push({m:m, u:u, s:s, ct:ct, sample:'', kind:'fetch', f:String(location.href||'')});
          try{
            if(row && resp && wantSample(ct) && resp.clone){
              resp.clone().text().then(function(t){
                try{ row.sample = String(t||'').slice(0,2048); }catch(e){}
              })['catch'](function(){});
            }
          }catch(e){}
          return resp;
        })['catch'](function(){});
      }catch(e){}
      return p;
    };
  }catch(e){}

  try{
    if(navigator.requestMediaKeySystemAccess){
      var RE = navigator.requestMediaKeySystemAccess.bind(navigator);
      navigator.requestMediaKeySystemAccess = function(ks, cfg){
        try{ if(window.__diagEme.indexOf(ks)<0) window.__diagEme.push(String(ks)); }catch(e){}
        return RE(ks, cfg);
      };
    }
  }catch(e){}
})();
"""

_DIAG_TRIGGER_JS = r"""
(function(){
  try{ window.__taowuDiagTrigger = (window.__taowuDiagTrigger||0) + 1; }catch(e){}
  try{
    var vs = document.querySelectorAll('video, audio');
    for(var i=0;i<vs.length;i++){ try{ vs[i].muted = true; if(vs[i].play) vs[i].play(); }catch(e){} }
  }catch(e){}
  try{
    var sels = ['button[aria-label*="播放"]', '.vjs-big-play-button', '.dplayer-play-icon',
                '[class*="play-btn"]', '[class*="playBtn"]', '[class*="play_btn"]'];
    for(var j=0;j<sels.length;j++){
      var ns = document.querySelectorAll(sels[j]);
      for(var k=0;k<ns.length && k<3;k++){ try{ ns[k].click(); }catch(e){} }
    }
  }catch(e){}
  try{ window.scrollBy(0, Math.max(400, window.innerHeight)); }catch(e){}
})();
"""

_DIAG_COLLECT_JS = r"""
(function(){
  var __taowuDiagCollect = 1;
  var out = {rows: [], hdr: [], eme: [], frames: 0, err: ''};
  function grab(w){
    try{
      var r = w.__diagRows;
      if(r && r.length){ for(var i=0;i<r.length;i++){ try{ out.rows.push(r[i]); }catch(e){} } }
    }catch(e){}
    try{
      var h = w.__diagHdrKeys;
      if(h && h.length){ for(var j=0;j<h.length;j++){ try{ if(out.hdr.indexOf(h[j])<0) out.hdr.push(h[j]); }catch(e){} } }
    }catch(e){}
    try{
      var m = w.__diagEme;
      if(m && m.length){ for(var k=0;k<m.length;k++){ try{ if(out.eme.indexOf(m[k])<0) out.eme.push(m[k]); }catch(e){} } }
    }catch(e){}
  }
  try{
    grab(window);
    try{
      for(var i=0;i<window.frames.length;i++){ out.frames++; try{ grab(window.frames[i]); }catch(e){} }
    }catch(e){}
  }catch(e){ out.err = String(e); }
  try{
    var es = (performance.getEntriesByType) ? performance.getEntriesByType('resource') : [];
    for(var n=0;n<es.length;n++){
      var r = es[n] || {}, it = String(r.initiatorType||'').toLowerCase();
      if(it === 'xhr' || it === 'fetch' || it === 'beacon'){
        out.rows.push({m:'', u:String(r.name||''), s:0, ct:'', sample:'', kind:'perf',
                       f:String(location.href||'')});
      }
    }
  }catch(e){}
  return out;
})();
"""


def _install_diag_patch(driver) -> bool:
    """每新文档注入打点。CDP 优先，不可用退 execute_script（镜像 vg._install_net_patch）。"""
    if driver is None:
        return False
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _DIAG_PATCH_JS})
        return True
    except Exception:
        pass
    try:
        driver.execute_script(_DIAG_PATCH_JS)
        return True
    except Exception:
        return False


def _collect_diag_rows(driver, *, page_url: str = "") -> dict:
    """收一次观测。返回 {"rows":[DiagRow], "hdr":[...], "eme":[...], "frames":n}。绝不 raise。"""
    empty = {"rows": [], "hdr": [], "eme": [], "frames": 0}
    if driver is None:
        return empty
    try:
        data = driver.execute_script(_DIAG_COLLECT_JS)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    rows = []
    for r in (data.get("rows") or ()):
        if not isinstance(r, dict):
            continue
        try:
            rows.append(DiagRow(
                method=str(r.get("m") or "GET").upper() or "GET",
                url=str(r.get("u") or ""),
                status=int(r.get("s") or 0),
                content_type=str(r.get("ct") or ""),
                body_sample=str(r.get("sample") or ""),
                kind=str(r.get("kind") or "xhr"),
                frame_url=str(r.get("f") or ""),
                page_url=page_url,
            ))
        except Exception:
            continue
    return {"rows": rows,
            "hdr": [str(x) for x in (data.get("hdr") or ()) if x],
            "eme": [str(x) for x in (data.get("eme") or ()) if x],
            "frames": int(data.get("frames") or 0)}


def _dedup_rows(rows) -> list:
    """按 (method, url, status) 去重，保序。"""
    out, seen = [], set()
    for r in rows or ():
        k = (getattr(r, "method", ""), getattr(r, "url", ""), getattr(r, "status", 0))
        if not k[1] or k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ================================================================
# 结论合成（纯函数：把指纹 / 探针 / robots / 路由 变成 findings）
# ================================================================
def findings_from_static(finger: dict, *, not_found: bool = False) -> list:
    """静态阶段 -> findings。"""
    out = []
    if not finger:
        return out
    for h in (finger.get("framework_hits") or ()):
        out.append(finding(C_FRONT_FRAMEWORK, f"前端框架：{h.get('name')}",
                           f"命中标记 {h.get('marker')}", SEV_INFO, SRC_STATIC,
                           framework=h.get("name"), marker=h.get("marker")))
    guess = finger.get("spa_guess")
    if guess == "ssr":
        out.append(finding(C_FRONT_SSR, "服务端渲染（SSR）",
                           finger.get("spa_reason", ""), SEV_INFO, SRC_STATIC))
    elif guess == "spa":
        out.append(finding(C_FRONT_SPA, "疑似纯前端渲染（SPA）",
                           finger.get("spa_reason", ""), SEV_MID, SRC_STATIC,
                           body_text_len=finger.get("body_text_len")))
    elif guess == "static":
        out.append(finding(C_FRONT_STATIC, "静态输出", finger.get("spa_reason", ""),
                           SEV_INFO, SRC_STATIC))
    if finger.get("challenge_hint"):
        out.append(finding(C_FRONT_CHALLENGE, "页面是反爬挑战页",
                           f"命中特征：{finger['challenge_hint']}", SEV_HIGH, SRC_STATIC,
                           hint=finger.get("challenge_hint")))
    if finger.get("captcha_sdk_hint"):
        # **故意用 SEV_MID 且不抬档**：这只是一次关键词命中，证明不了"页面被拦了"。
        # 真被拦是状态码那条线的事（403 + 挑战正文 → risk.http.challenge_*）。
        out.append(finding(C_FRONT_CAPTCHA_SDK, "页面引用了验证码/防护组件",
                           f"命中：{finger['captcha_sdk_hint']}"
                           "　—— 带登录框的页面通常都会引用，**不构成挑战页**",
                           SEV_MID, SRC_STATIC, hint=finger.get("captcha_sdk_hint")))
    if finger.get("login_wall_hint"):
        out.append(finding(C_FRONT_LOGIN, "疑似登录墙",
                           f"命中特征：{finger['login_wall_hint']}", SEV_MID, SRC_STATIC))
    ms = finger.get("media_signals") or {}
    if any(ms.get(k) for k in ("json_ld_video", "og_video", "og_audio",
                               "inline_m3u8", "inline_mpd")) or ms.get("media_ext_n"):
        out.append(finding(C_FRONT_MEDIA, "页面含媒体信号",
                           "JSON-LD / og / 内联媒体地址至少命中一项", SEV_INFO, SRC_STATIC,
                           **ms))
    if finger.get("html_len") and finger.get("body_text_len", 0) == 0 and not not_found:
        out.append(finding(C_FRONT_EMPTY, "空壳页（剥脚本后无正文）",
                           "可能整页由 JS 渲染，静态抓取拿不到内容", SEV_MID, SRC_STATIC))
    return out


def findings_from_probe(probe: dict) -> list:
    """探针 -> findings。**这是诊断话术的第一来源**。"""
    out, p = [], probe or {}
    if not p:
        return out
    st = int(p.get("status") or 0)
    ek = p.get("error_kind") or ""

    if p.get("tls_rescued"):
        out.append(finding(C_TLS_RESCUED, "TLS 指纹墙",
                           "requests 连不上而 curl_cffi 能通 —— 该站按 TLS 指纹拦人，"
                           "必须走 impersonate（本项目的 register_impersonate_host 就是干这个的）",
                           SEV_HIGH, SRC_STATIC, engine=p.get("engine")))
    if ek:
        lab = {"dns": ("DNS 解析失败", C_REACH_DNS, SEV_BLOCKER),
               "tls": ("TLS 握手失败", C_REACH_TLS, SEV_BLOCKER),
               "timeout": ("连接/读取超时", C_REACH_TIMEOUT, SEV_HIGH),
               "conn": ("连接失败", C_REACH_CONN, SEV_HIGH)}
        name, code, sev = lab.get(ek, ("连接失败", C_REACH_CONN, SEV_HIGH))
        out.append(finding(code, name, str(p.get("error") or "")[:300], sev, SRC_STATIC,
                           error_kind=ek))

    if st:
        if st == 404:
            out.append(finding(C_NOT_FOUND, "HTTP 404",
                               "页面不存在 —— 检查 URL 是不是拼错了", SEV_MID, SRC_STATIC,
                               status=st))
        elif 500 <= st < 600:
            out.append(finding(C_HTTP_5XX, f"HTTP {st} 服务端错误",
                               "站方自己出问题了，不是反爬", SEV_MID, SRC_STATIC, status=st))

        cls = p.get("classification") or ""
        pair = {
            "ratelimit":        (C_RATE_LIMIT, "被限频", SEV_HIGH),
            "auth":             (C_AUTH_WALL, "需要登录/授权", SEV_HIGH),
            "challenge_auto":   (C_CHALLENGE_AUTO, "人机验证（可自动类）", SEV_HIGH),
            "challenge_manual": (C_CHALLENGE_MAN, "滑块/人工验证码", SEV_BLOCKER),
            "hotlink":          (C_HOTLINK, "防盗链/签名失效", SEV_MID),
        }.get(cls)
        if pair and st >= 400:
            out.append(finding(pair[0], f"HTTP {st}：{pair[1]}",
                               str(p.get("hint") or ""), pair[2], SRC_STATIC,
                               status=st, classification=cls))
    return out


def findings_from_rules(rules: dict, url: str) -> list:
    """robots 判定 -> findings。命中禁止**显著标红**并写明是哪一条规则。"""
    out = []
    if not rules:
        return out
    if rules.get("absent"):
        out.append(finding(C_RULES_NOROBOTS, "该站没有 robots.txt 或内容为空",
                           "目标站未声明任何抓取规矩", SEV_INFO, SRC_STATIC))
        return out
    groups = rules.get("groups") or []
    if groups:
        n_dis = sum(1 for g in groups for r in (g.get("rules") or ())
                    if r.get("type") == "disallow" and (r.get("path") or ""))
        out.append(finding(C_RULES_ROBOTS, "该站声明了 robots.txt",
                           f"{len(groups)} 个 User-agent 分组、{n_dis} 条 Disallow 规则",
                           SEV_INFO, SRC_STATIC, groups_n=len(groups), disallow_n=n_dis))
    for sm in (rules.get("sitemaps") or ()):
        out.append(finding(C_RULES_SITEMAP, "robots 里声明了 Sitemap",
                           str(sm), SEV_INFO, SRC_STATIC, sitemap=sm))
    if rules.get("state") == "disallowed":
        r = rules.get("matched_rule") or {}
        out.append(finding(C_RULES_HIT, f"本次探测的路径被该站 robots 禁止",
                           f"路径 {rules.get('path')} 命中规则 `Disallow: {r.get('path')}`"
                           f"（对 {', '.join(rules.get('matched_group') or ['*'])}）。"
                           f"**诊断默认不自动跳过** —— 底线已摊开，怎么做由你定。",
                           SEV_HIGH, SRC_STATIC,
                           path=rules.get("path"), rule=r.get("path"),
                           agents=rules.get("matched_group")))
    return out


def findings_from_route(route_verdict: dict) -> list:
    out = []
    rv = route_verdict or {}
    route = str(rv.get("route") or "")
    if route:
        out.append(finding(C_GRAB_ROUTE, f"工作台解析路线：{route}",
                           f"命中媒体候选 {rv.get('tracks_n', 0)} 条",
                           SEV_INFO, SRC_RECIPE, route=route, tracks_n=rv.get("tracks_n", 0)))
    if rv.get("tracks_n"):
        out.append(finding(C_GRAB_TRACKS, "有可直接下载的媒体轨",
                           f"{rv['tracks_n']} 条", SEV_INFO, SRC_RECIPE))

    # ── 静态 DRM（清单 KEYFORMAT / pssh）──
    # 这是**观测**：站方自己在清单里写下的 KEYFORMAT，不是我们翻旧账翻出来的。
    # 与 EME 那条共用一个码、用 via 区分通道 —— 两条通道都成立时只报一条更全的。
    dp = rv.get("drm_probe") or {}
    systems = list(dp.get("commercial") or [])
    if systems:
        ev = "清单 KEYFORMAT/pssh：%s" % ", ".join(systems)
        ev += "；判别依据 %d 条清单" % dp.get("probed", 0)
        out.append(finding(C_FRONT_DRM, "媒体清单声明了商业 DRM",
                           ev + "　—— 本项目只识别不解密",
                           SEV_BLOCKER, SRC_STATIC, via="manifest",
                           systems=systems, probed=dp.get("probed", 0),
                           candidates=dp.get("candidates", 0)))
    elif dp.get("unknown"):
        # 认不得的 KEYFORMAT：**不下判**，但摊开。宁可说不知道，不可说错。
        out.append(finding(C_FRONT_DRM, "清单里有认不得的加密标记",
                           "KEYFORMAT：%s　—— 判不出是不是商业 DRM，请在报告里自行核对"
                           % ", ".join(str(x) for x in dp["unknown"]),
                           SEV_LOW, SRC_STATIC, via="manifest_unknown",
                           unknown=list(dp["unknown"])))
    return out


def findings_from_text(tp: dict) -> list:
    """文字轴（干抽取）→ 结论。

    **这些码一个都不抬难度档**（不在 `_OBS_TO_LEVEL` 里），与七个 `site.*` 同纪律：
    它们答的是「我们用这套模板能不能从这页抽出来」= **我们走了多远**，
    不是「站方在怎么样」。把前者当后者，就是在给自己发假证据。
    """
    out = []
    tp = tp or {}
    if not tp:
        return out
    if not tp.get("ok"):
        out.append(finding(C_TEXT_EMPTY, "文字抽不出内容",
                           tp.get("note") or "正文为空", SEV_LOW, SRC_STATIC))
        return out
    if not tp.get("titles_n"):
        out.append(finding(C_TEXT_NOTITLE, "页面认不出标题",
                           "h1 / og:title / <title> 一个都没取到 —— "
                           "靠标题兜底的列（型号、名称）会抽空", SEV_LOW, SRC_STATIC))
    ranking = tp.get("ranking") or []
    best = ranking[0] if ranking else None
    if best and best.get("hits"):
        out.append(finding(C_TEXT_HIT, "有模板能抽出业务列（已静默干跑一遍）",
                           "「%s」命中 %d/%d 列：%s"
                           % (best.get("name"), best["hits"], best["total"],
                              "、".join(str(c) for c in (best.get("filled") or []))),
                           SEV_INFO, SRC_STATIC, template=best.get("name"),
                           hits=best["hits"], total=best["total"],
                           filled=list(best.get("filled") or [])))
    elif ranking:
        out.append(finding(C_TEXT_EMPTY, "没有一套模板能抽出业务列",
                           "试了 %d 套模板，业务列全空 —— 这一页的内容多半不在正文里"
                           "（纯前端渲染 / 壳页 / 需要登录）"
                           % tp.get("templates_n", len(ranking)),
                           SEV_MID, SRC_STATIC, templates_n=tp.get("templates_n", 0)))
    # 落库口径的提醒：与站点无关，是我们自己的事 —— 但恰恰是抓之前该知道的。
    if tp.get("nodup"):
        out.append(finding(C_TEXT_NODEDUP, "落库没有唯一键（重复入库风险）",
                           "表只有自增 id，去重只靠会话内的 seen_urls —— "
                           "**换个会话重抓同一个站会再插一遍**。",
                           SEV_LOW, SRC_CONFIG, nodup=True))
    return out


def findings_from_eme(eme_keys) -> list:
    """EME keySystem -> DRM 识别（**只识别，绝不解密**）。"""
    out = []
    if not eme_keys:
        return out
    commercial = []
    try:
        import media_resolve as mr
        # EME 的 keySystem 是 `com.widevine.alpha` 这种**全名**，而 mr.drm_is_commercial
        # 吃的是**方案名** `widevine` —— 直接拿 keySystem 喂它恒为 False（且不报错）。
        # 中间那步 drm_name_from_key_system 就是为这个映射存在的。
        commercial = [k for k in eme_keys
                      if mr.drm_is_commercial(mr.drm_name_from_key_system(str(k)))]
    except Exception:
        commercial = [k for k in eme_keys
                      if any(x in str(k).lower() for x in
                             ("widevine", "playready", "fairplay"))]
    out.append(finding(C_FRONT_DRM,
                       "页面申请了 DRM（商业加密）" if commercial else "页面申请了 EME",
                       f"keySystem：{', '.join(map(str, eme_keys))}"
                       + ("　—— 本项目只识别不解密" if commercial else ""),
                       SEV_BLOCKER if commercial else SEV_LOW, SRC_BROWSER, via="eme",
                       key_systems=list(map(str, eme_keys)), commercial=bool(commercial)))
    return out


def findings_from_knowledge(facts: dict, *, status: int = 0) -> list:
    """知识库命中 -> 结论（P2）。facts 由 site_knowledge.knowledge_facts 产出。

    **这些结论刻意都不进 `_OBS_TO_LEVEL`**，理由各不相同但同一条原则
    ——「知识是旁证，观测才是证据」：

      · `C_KNOW_CHALLENGE`（静态文本命中反爬厂商特征）：正文里出现 "recaptcha" 可能只是
        一篇讲验证码的文章，或者页面**引用了**组件（bilibili 首页就引了 geetest 的登录 SDK）。
        真正的挑战页由观测分支负责抬档 —— 即 `C_FRONT_CHALLENGE`（**只认整页插页特征**，
        见 `_CHALLENGE_INTERSTITIAL`）与状态码那条线的 `C_CHALLENGE_*`。
        这里只提供**厂商归属**，让报告能说清"是 Cloudflare 在挡"。
      · `C_KNOW_BLOCKWORD`：只在**非 2xx** 时才有值（由 site_knowledge 侧把关），
        而 403/5xx 本身已经在 `_OBS_TO_LEVEL` 里了 —— 再加一次是重复计权，不是补全。
      · `C_KNOW_RECIPE`：已有配方是**好事**（说明工作台认识这站），更不该抬高难度。

    这条与「robots 命中不进难度档位」是同一条纪律的两个实例：**别让旁证冒充证据。**
    """
    f = facts if isinstance(facts, dict) else {}
    out = []

    ch = f.get("challenge") or {}
    if ch.get("type") and ch.get("type") != "unknown":
        where = "、".join(ch.get("where") or ())
        out.append(finding(C_KNOW_CHALLENGE,
                           f"反爬特征命中：{ch['type']}",
                           f"置信度 {ch.get('confidence', 0)}（命中位置：{where}）"
                           f"　—— 与工作台 detect_challenge_type 同一张特征表，"
                           f"这里是从页面正文静态匹配出来的",
                           SEV_MID, SRC_KNOWLEDGE,
                           vendor=ch["type"], confidence=ch.get("confidence", 0),
                           where=list(ch.get("where") or ())))

    bw = f.get("block_words") or {}
    if bw.get("hits"):
        out.append(finding(C_KNOW_BLOCKWORD,
                           "正文命中该站已知拦截词",
                           f"命中：{'、'.join(bw['hits'])}（站键 {bw.get('key')}）"
                           f"　—— 这是站方明确拒绝的措辞，不是普通 403",
                           SEV_MID, SRC_KNOWLEDGE,
                           key=bw.get("key"), hits=list(bw["hits"]), status=int(status or 0)))

    rc = f.get("recipe") or {}
    if rc.get("name"):
        out.append(finding(C_KNOW_RECIPE,
                           f"工作台已有该站媒体配方：{rc['name']}",
                           f"命中 host_suffix={rc.get('matched')}（来源 {rc.get('source')}）"
                           f"　—— 直接走媒体配方比从零摸快得多",
                           SEV_INFO, SRC_RECIPE,
                           # evidence 键名**不能叫 source** —— finding() 的第 5 个位置参数
                           # 就是 source，撞名会 TypeError: got multiple values for argument
                           name=rc["name"], recipe_source=rc.get("source"),
                           matched=rc.get("matched")))
    return out


def findings_from_site(*, discovery=None, structure=None, penetration=None,
                       budget=None, aborted: bool = False) -> list:
    """整站层的账目与结论（P3）。

    **这七个 site.* 码一个都不进 `_OBS_TO_LEVEL`** —— 理由见那张表上方的注释。
    一句话：它们答的是「我们走了多远」，不是「站方在防你」。唯一例外是 429，
    那件事由**页级**的 `C_RATE_LIMIT` 抬档（同一条事实绝不重复计权）。
    """
    d = discovery if isinstance(discovery, dict) else {}
    st = structure if isinstance(structure, dict) else {}
    pen = penetration if isinstance(penetration, dict) else {}
    bud = budget if isinstance(budget, dict) else {}
    out = []

    if st.get("templates"):
        tmpls = st["templates"]
        kinds = {}
        for t in tmpls:
            kinds[t.get("kind", "other")] = kinds.get(t.get("kind", "other"), 0) + 1
        out.append(finding(
            C_SITE_STRUCTURE,
            f"站点结构：{st.get('urls_n', 0)} 条 URL 归约出 {len(tmpls)} 个页面模板",
            "　".join(f"{k} {v} 个" for k, v in sorted(kinds.items()))
            + f"　—— 内容结构就这样铺开了，「穿透」的量由模板数决定，不由站点总页数决定。",
            SEV_INFO, SRC_STATIC,
            urls_n=st.get("urls_n", 0), templates_n=len(tmpls), kinds=kinds))

    if d.get("sitemap_found"):
        out.append(finding(
            C_SITE_SITEMAP,
            f"通过 sitemap 导览到 {d.get('from_sitemap', 0)} 条 URL",
            f"展开 sitemap {d.get('sitemaps_ok', 0)} 份"
            + (f"（失败 {d.get('sitemaps_fail', 0)} 份）" if d.get("sitemaps_fail") else "")
            + f"　来源：{'站点在 robots 里自己声明的' if d.get('seed_source') == 'robots' else 'well-known 路径，我们自己猜的'}",
            SEV_INFO, SRC_STATIC,
            from_sitemap=d.get("from_sitemap", 0), sitemaps_ok=d.get("sitemaps_ok", 0),
            seed_source=d.get("seed_source", "")))
    else:
        out.append(finding(
            C_SITE_NOSITEMAP, "该站没有可用的 sitemap",
            "导览只能靠首页链接，URL 全集**可能远小于**站点实际内容 —— "
            "下面的结构地图是下限，不是全貌。",
            SEV_LOW, SRC_STATIC))

    for p in (pen.get("probes") or ()):
        v = p.get("verdict")
        if v == "ignored":
            out.append(finding(
                C_SITE_NO_PAGE, f"列表页翻页是假的：{p.get('tmpl')}",
                f"试了 {p.get('tested', 0)} 个翻页写法（{p.get('mode')}），"
                f"HTTP 200 但内容与第 1 页**一模一样** —— 页码参数被忽略。"
                f"　⇒ 这类页面**没法穷举**，只能拿到第一页。"
                f"　参照：网易云的 s/offset/n/limit 全被忽略，酷狗的限速伪装成付费档，"
                f"都是这种「200 + 合法内容 + 其实是第一页」。",
                SEV_HIGH, SRC_STATIC,
                tmpl=p.get("tmpl"), mode=p.get("mode"), tested=p.get("tested", 0),
                tried=list(p.get("tried") or ())[:5]))
        elif v == "ok":
            out.append(finding(
                C_SITE_PAGINATION, f"列表页翻得动：{p.get('tmpl')}",
                f"用 `{p.get('key') or p.get('mode')}` 翻页，第 2 页**多出了 "
                f"{p.get('new_links_n', 0)} 条第 1 页没有的链接**"
                f"（已验证 {p.get('effective', 0)} 步）"
                f"　⇒ 判据是「多了新链接」而不只是「内容变了」，这类页面可以顺着翻下去。",
                SEV_INFO, SRC_STATIC,
                tmpl=p.get("tmpl"), mode=p.get("mode"), key=p.get("key"),
                effective=p.get("effective", 0), new_links_n=p.get("new_links_n", 0)))
        elif v == "weak":
            # **弱信号**：内容变了但没多出新链接。宁可说「判不了」，也不说「翻得动」。
            out.append(finding(
                C_SITE_PAGINATION, f"列表页翻页**没验出来**：{p.get('tmpl')}",
                str(p.get("why") or "内容有变化，但没有多出新链接"),
                SEV_LOW, SRC_STATIC, tmpl=p.get("tmpl"), mode=p.get("mode"),
                verdict="weak", new_links_n=0))
        elif v == "unknown":
            out.append(finding(
                C_SITE_PAGINATION, f"列表页翻页判不了：{p.get('tmpl')}",
                str(p.get("why") or "拿不到可比较的内容"), SEV_LOW, SRC_STATIC,
                tmpl=p.get("tmpl"), mode=p.get("mode"), verdict="unknown"))

    if aborted:
        out.append(finding(
            C_SITE_ABORT429, "整站诊断被 429 中止",
            "目标站开始限频，**立刻停手不再发任何请求** —— 继续探下去只会让情况更糟"
            "（本项目踩过「限速伪装成付费档」：200 + 合法 JSON + 空结果，不报任何错）。"
            "　报告里剩下的覆盖是中止前的，不完整。",
            SEV_HIGH, SRC_STATIC, at_page=bud.get("aborted_at", "")))

    if bud.get("truncated"):
        left = bud.get("templates_untouched") or []
        out.append(finding(
            C_SITE_BUDGET, "请求预算用尽，覆盖到此为止",
            f"本次上限 {bud.get('max_pages', 0)} 页，实际走了 {bud.get('pages_done', 0)} 页"
            + (f"，还有 {len(left)} 个模板一个代表页都没跑到" if left else "")
            + "　—— **这不是静默截断**：上面每一条结论只对「走过的这些页面」负责。"
              "要更深就换档位（「极致」200 页 / 「全部」不限）。",
            SEV_MID, SRC_CONFIG,
            max_pages=bud.get("max_pages", 0), pages_done=bud.get("pages_done", 0),
            templates_untouched=list(left)[:20]))
    return out


def current_page_rule(rows, parsed, *, agent="*") -> list:
    """把浏览器阶段观测到的**每条请求路径**标上 robots 合规状态（纯函数）。

    这是用户要的「知道底线」真正的落点：不是空泛列规矩，而是
    「我这次探的 /news/ 正好在禁止名单里」。
    """
    out = []
    if not parsed or not parsed.get("groups"):
        return out
    seen = set()
    for r in rows or ():
        u = getattr(r, "url", "") or ""
        h = _host_of(u)
        if not h:
            continue
        try:
            path = urlsplit(u).path or "/"
        except Exception:
            continue
        key = (h, path)
        if key in seen:
            continue
        seen.add(key)
        chk = robots_check(parsed, path, agent=agent)
        if chk.get("state") == "disallowed":
            out.append({"url": u, "host": h, "path": path,
                        "rule": (chk.get("rule") or {}).get("path"),
                        "agents": chk.get("group_agents") or []})
    return out


# ================================================================
# 建议合成（纯函数）
# ================================================================
def build_suggestions(report: DiagReport) -> list:
    """按 findings 生成可执行建议。**只说能做的事，不代替用户决定。**"""
    codes = {getattr(f, "code", "") for f in (report.findings or ())}
    s = []
    if C_TLS_RESCUED in codes:
        s.append("该站按 TLS 指纹拦人：抓之前先 register_impersonate_host 把域名登记进"
                 "指纹 host（本项目的 curl_cffi impersonate 就是干这个的）。")
    if C_CHALLENGE_MAN in codes:
        s.append("命中滑块/人工验证码 —— 这是红线，不会自动过。先人工在浏览器里过一次，"
                 "再让工作台复用那份 cookie。")
    if C_CHALLENGE_AUTO in codes:
        s.append("命中可自动类人机验证（CF/imperva 一类）：确认 Chrome 可用，"
                 "下载层会走 _dl_auto_challenge_once 自动收 cookie。")
    if C_RATE_LIMIT in codes:
        s.append("被限频：调大 VIDEO_PACE_* / 降低并发。**注意本项目踩过的坑 —— "
                 "限速会伪装成付费档（200 + 合法 JSON + error:操作太频繁），不报 429。**")
    if C_AUTH_WALL in codes:
        s.append("需要登录态：先在弹窗或浏览器里登录一次，cookie 会进 jar。")
    if C_HOTLINK in codes:
        s.append("防盗链/签名失效：带对 Referer，必要时 register_signer 自动重签。")
    if C_FRONT_SPA in codes:
        s.append("页面是纯前端渲染：静态抓取拿不到正文，得走浏览器兜底线。")
    if C_FRONT_CHALLENGE in codes:
        s.append("先过挑战页再谈抓取 —— 挑战没过时拿到的一切都只是空壳，不算数。")
    if C_FRONT_DRM in codes:
        s.append("命中 DRM：本项目只识别不解密，也不建议投入。")
    if C_RULES_HIT in codes:
        s.append("本次探测的路径落在该站 robots 禁止名单里。诊断按你的要求**没自动跳过**，"
                 "要继续还是收手由你定。")
    if C_FRONT_EMPTY in codes:
        s.append("页面剥脚本后无正文：确认是真 SPA 还是被拦了（看上面有没有挑战/限频结论）。")
    hs = report.risk.get("evidence", {}).get("hard_site_key")
    if hs:
        s.append(f"该站命中了内置高难度站点表（key={hs}）—— 工作台会自动上究极对抗档，"
                 f"你也可以先把已有配置读一遍再决定是否调整。")
    k = report.knowledge or {}
    rc = k.get("recipe") or {}
    if rc.get("name"):
        s.append(f"该站已有启用中的媒体配方「{rc['name']}」—— 直接走媒体配方，"
                 f"别再从零摸一遍（配方就是这条路的成文经验）。")
    if C_KNOW_CHALLENGE in codes:
        v = (k.get("challenge") or {}).get("type")
        s.append(f"反爬特征指向 {v}：这是**静态文本**匹配出来的厂商归属，不等于真被拦。"
                 f"先按上面的观测结论走；真要开浏览器时，工作台会按厂商选对应的等待/重试策略。")
    if C_KNOW_BLOCKWORD in codes:
        s.append("正文命中了该站已知的拦截词 —— 这是站方明确拒绝的措辞，"
                 "别把它当成普通的 403 反复重试。")
    pc = k.get("pacing") or {}
    if pc and (pc.get("min") or pc.get("max")):
        s.append(f"本站建议节拍 {pc.get('min')} ~ {pc.get('max')} 秒/请求"
                 f"（来自 SITE_DELAY_CONFIG，比全局默认更保守）。")
    # ── 整站（P3）──
    if C_SITE_NO_PAGE in codes:
        bad = [f.evidence.get("tmpl") for f in (report.findings or ())
               if getattr(f, "code", "") == C_SITE_NO_PAGE]
        s.append(f"这些列表页翻页是假的（{', '.join(str(x) for x in bad[:3])}）—— "
                 f"别指望顺着翻。先人工看一页，确认它到底靠什么换页"
                 f"（滚动加载？POST？带签名的接口？），再决定值不值得为它写配方。")
    if C_SITE_BUDGET in codes:
        s.append(f"本次只走到了预算上限。要更全就换档位重跑："
                 f"「极致」200 页 / 「全部」不限（但会更慢，恒串行不并发）。")
    if C_SITE_NOSITEMAP in codes:
        s.append("该站没有 sitemap：URL 全集是**从首页链接推的**，很可能远小于实际内容。"
                 "真要穷举得另找入口（栏目页翻到底、或站内搜索）。")
    if C_SITE_ABORT429 in codes:
        s.append("被 429 打断了：等一会儿再跑，并把节拍调大（DIAG_SITE_DELAY）。"
                 "**别立刻重试** —— 本项目踩过「限速伪装成付费档」，越急越糟。")
    if not s:
        s.append("未发现明显阻碍。可以先拿少量样本试抓，跑通了再放量。")
    return s


# ================================================================
# 渲染（9 个分节 + 汇总）
# ================================================================
_SEC = "=" * 62


def _sev_mark(sev: str) -> str:
    return {"blocker": "[!!]", "high": "[! ]", "mid": "[~ ]",
            "low": "[. ]", "info": "[  ]"}.get(sev, "[  ]")


def _line(*parts) -> str:
    return "  ".join(str(p) for p in parts if p not in (None, ""))


def render_overview(r: DiagReport) -> str:
    L = [_SEC, "网站诊断报告  v" + str(r.diag_version), _SEC,
         f"目标 URL   ：{r.url}"]
    if r.final_url and r.final_url != r.url:
        L.append(f"最终 URL   ：{r.final_url}")
    if r.title:
        L.append(f"页面标题   ：{r.title}")
    sc = "整站" if r.scope == "site" else "单页"
    if r.scope == "site" and (r.profile_name or r.profile):
        sc += f"（档位：{r.profile_name or r.profile}）"
    L.append(f"诊断范围   ：{sc}")
    if r.scope == "site" and r.structure:
        L.append(f"结构规模   ：{r.structure.get('urls_n', 0)} 条 URL / "
                 f"{len(r.structure.get('templates') or ())} 个模板")
    risk = r.risk or {}
    L.append(f"难度档位   ：{risk.get('level', '?')} / {risk.get('name', 'unknown')}"
             f"（依据：{risk.get('source', 'none')}）")
    feas = r.feasibility or {}
    L.append(f"可行性     ：{feas.get('verdict', '未知')}")
    if feas.get("summary"):
        L.append(f"             {feas['summary']}")
    # 覆盖标记**必须进抬头**：抬头是最容易被单独摘出来读的地方（2026-09-11 用户贴回来的
    # 猫耳那份就是抬头先被看到的）。一个「可以试」被摘走时，得带着它自己的限定条件走。
    cov = r.coverage or {}
    if cov.get("missing_n"):
        L.append(f"覆盖声明   ：本次有 {cov['missing_n']} 条轴**没测到位**"
                 f"（{'、'.join(str(x) for x in (cov.get('missing_labels') or []))}）"
                 f" —— 见「一、本次没覆盖什么」")
    elif cov.get("axes"):
        L.append("覆盖声明   ：本次该覆盖的轴全部覆盖到了")
    L.append("")
    return "\n".join(L)


def render_coverage(r: DiagReport) -> str:
    """本次没覆盖什么。

    **刻意排在所有细节之前**：它限定的是后面每一节的效力 —— 先说清楚这把尺子量到哪，
    再让人看尺子量出来的数。放在报告末尾等于不说（没人会先翻到底再回头看结论）。
    """
    cov = r.coverage or {}
    L = ["", "── 一、本次没覆盖什么 ──"]
    axes = cov.get("axes") or []
    if not axes:
        L.append("  （本报告未产出覆盖清单）")
        return "\n".join(L)
    miss = [a for a in axes if a.get("state") != COV_DONE]
    if not miss:
        L.append("  本次该覆盖的轴全部覆盖到了。")
    else:
        L.append(f"  下面 {len(miss)} 条**本次没测到位**。"
                 "没测 ≠ 没问题 —— 本报告在那些地方不下判断，也别把它读成通过。")
        for a in miss:
            tag = "[半]" if a.get("state") == COV_PARTIAL else "[未]"
            L.append(f"  {tag} {a.get('label')}")
            if a.get("why"):
                L.append(f"       原因：{a['why']}")
            if a.get("fix"):
                L.append(f"       补法：{a['fix']}")
    done = [a.get("label") for a in axes if a.get("state") == COV_DONE]
    if done:
        L.append("  已覆盖：" + "、".join(str(x) for x in done))
    return "\n".join(L)


def render_frontend(r: DiagReport) -> str:
    f = r.fingerprint or {}
    L = ["", "── 二、前端指纹 ──"]
    if not f or not f.get("ok"):
        L.append("  未取到页面正文（看下面的「可达性」节）")
        return "\n".join(L)
    L.append(f"  页面大小   ：{f.get('html_len', 0)} 字符"
             f"（内联脚本 {f.get('script_chars', 0)}／可见正文 {f.get('body_text_len', 0)}）")
    L.append(f"  渲染方式   ：{f.get('spa_guess')}　{f.get('spa_reason', '')}")
    fw = [h.get("name") for h in (f.get("framework_hits") or ())]
    L.append(f"  框架标记   ：{', '.join(fw) if fw else '（无）'}")
    ms = f.get("media_signals") or {}
    if ms:
        hot = [k for k in ("json_ld_video", "og_video", "og_audio", "inline_m3u8", "inline_mpd")
               if ms.get(k)]
        L.append(f"  媒体信号   ：{', '.join(hot) if hot else '（无）'}"
                 f"（页面内媒体扩展名 {ms.get('media_ext_n', 0)} 种）")
    if f.get("challenge_hint"):
        L.append(f"  [!] 挑战特征：{f['challenge_hint']}")
    if f.get("captcha_sdk_hint") and not f.get("challenge_hint"):
        # 两行都出现会让人以为是一件事；挑战页已经判出来时就不再提组件引用。
        L.append(f"  [ ] 页面引用了验证码组件：{f['captcha_sdk_hint']}"
                 "（正常页面带登录框就会引用，**不等于被拦**）")
    if f.get("login_wall_hint"):
        L.append(f"  [~] 疑似登录墙：{f['login_wall_hint']}")
    return "\n".join(L)


def render_rules(r: DiagReport) -> str:
    """目标站声明的规矩（robots）。用户明确要这一节 —— 原文与解析结果都给。"""
    rules = r.rules or {}
    L = ["", "── 三、目标站声明的规矩（robots.txt）──"]
    if not rules:
        L.append("  未抓取 robots.txt")
        return "\n".join(L)
    if rules.get("absent"):
        L.append("  该站没有 robots.txt，或文件为空 —— **无声明**。")
        L.append("  （这既不是「允许」也不是「禁止」，只是站方没写。）")
        return "\n".join(L)
    st = {"allowed": "允许", "disallowed": "被禁止", "none": "无声明"}.get(rules.get("state"), "?")
    L.append(f"  本次探测路径：{rules.get('path')}　->　{st}")
    if rules.get("state") == "disallowed":
        mr = rules.get("matched_rule") or {}
        L.append(f"  [!] 命中规则：Disallow: {mr.get('path')}")
        L.append(f"      作用于：{', '.join(rules.get('matched_group') or ['*'])}")
        L.append("      诊断默认**不自动跳过**。底线摆在这里，继续还是收手由你定。")
    for g in (rules.get("groups") or ()):
        agents = ", ".join(g.get("agents") or ["*"])
        L.append(f"  [{agents}]")
        for rule in (g.get("rules") or ()):
            L.append(f"      {str(rule.get('type', '')).capitalize():8s} {rule.get('path')}")
        if g.get("crawl_delay"):
            L.append(f"      Crawl-delay: {g['crawl_delay']}")
    for sm in (rules.get("sitemaps") or ()):
        L.append(f"  Sitemap: {sm}")
    raw = (rules.get("raw") or "").strip()
    if raw:
        L.append("")
        L.append("  ── 原文（前 4000 字符）──")
        for ln in raw[:4000].splitlines():
            L.append("  | " + ln)
        if len(raw) > 4000 or rules.get("truncated"):
            L.append(f"  | ...（原文共 {len(raw)} 字符，已截断）")
    if rules.get("errors"):
        L.append(f"  解析注记：{len(rules['errors'])} 条未识别指令")
    return "\n".join(L)


def _src_desc(sources) -> str:
    s = sources or {}
    parts = []
    if s.get("sitemap"):
        parts.append(f"sitemap {s['sitemap']} 条")
    if s.get("seed"):
        parts.append(f"种子页 {s['seed']} 条")
    if s.get("home"):
        parts.append(f"首页链接 {s['home']} 条")
    return "、".join(parts) or "—"


def render_structure(r: DiagReport) -> str:
    """L2 结构地图。单页模式**如实写"没做"**，而不是空着让人以为是漏了。"""
    L = ["", "── 四、站点结构地图 ──"]
    st = r.structure or {}
    tmpls = st.get("templates") or []
    if r.scope != "site" or not tmpls:
        L.append("  本次是**单页**诊断，没做整站结构归纳。")
        L.append("  换「整站诊断」档位，会先把 URL 全集铺开（robots 的 Sitemap 行 → 递归展开 "
                 "→ 首页链接），归约成模板，再按模板深诊。")
        return "\n".join(L)
    L.append(f"  URL 全集 {st.get('urls_n', 0)} 条 → {len(tmpls)} 个页面模板"
             f"　（来源：{_src_desc(st.get('sources'))}）")
    L.append("  模板（按条数降序；每类只列样例，全量清单不进报告）：")
    for t in tmpls[:40]:
        L.append(f"    {t.get('n', 0):6d} 条  [{t.get('kind', 'other')}]  {t.get('tmpl')}")
        for s in (t.get("samples") or ())[:2]:
            L.append(f"                 · {s}")
    if len(tmpls) > 40:
        L.append(f"    …（还有 {len(tmpls) - 40} 个模板未列出）")
    for n in (st.get("notes") or ()):
        L.append(f"  注：{n}")
    return "\n".join(L)


def render_penetration(r: DiagReport) -> str:
    """L3 穿透 + L4 代表页体检。翻页探针是本项目踩得最狠的那一层的现成答案。"""
    L = ["", "── 五、穿透结果（列表 → 翻页 → 详情）──"]
    if r.scope != "site":
        L.append("  单页模式不做穿透：没有列表页可翻，也没有别的模板可比。")
        return "\n".join(L)
    pen = r.penetration or {}
    probes = pen.get("probes") or []
    if not probes:
        L.append("  没找到可做翻页试探的列表页/栏目页模板"
                 "（前端路由翻页、纯 AJAX 分页探不到 —— 这是本探针的已知边界）。")
    for p in probes:
        v = {"ok": "翻得动", "ignored": "页码被忽略", "unknown": "判不了",
             "none": "没找到翻页写法"}.get(p.get("verdict"), str(p.get("verdict")))
        L.append(f"  [{v}]  {p.get('tmpl')}")
        L.append(f"        试探 {p.get('tested', 0)} 个候选写法（{p.get('mode')}）"
                 f"　有效步数 {p.get('effective', 0)}")
        if p.get("why"):
            L.append(f"        {p['why']}")
        for u in (p.get("tried") or ())[:3]:
            L.append(f"        · {u}")
    pages = r.pages or []
    if pages:
        L.append("")
        L.append(f"  代表页体检（{len(pages)} 页，按模板抽的，每页跑的是**完整单页诊断**）：")
        for p in pages[:60]:
            L.append(f"    · [{p.get('status', 0) or 'opaque'}] {p.get('level', '?')}"
                     f"　{str(p.get('verdict') or '')[:16]}　{p.get('url', '')}")
        if len(pages) > 60:
            L.append(f"    …（还有 {len(pages) - 60} 页未列出）")
    for n in (pen.get("notes") or ()):
        L.append(f"  注：{n}")
    if pen.get("route_note"):
        L.append(f"  路由解析：{pen['route_note']}")
    if pen.get("browser_note"):
        L.append(f"  浏览器观测：{pen['browser_note']}")
    return "\n".join(L)


def render_backend(r: DiagReport) -> str:
    s = r.summary or {}
    L = ["", "── 六、后端观测（页面运行时到底请求了什么）──"]
    if not r.rows:
        L.append("  没有浏览器观测数据（本次未启用真机浏览，或页面无额外请求）")
        return "\n".join(L)
    L.append(f"  观测到请求 {s.get('total', 0)} 条，涉及 host {len(s.get('by_host') or {})} 个")
    stt = s.get("status") or {}
    L.append("  状态分布   ：" + (", ".join(f"{k}x{v}" for k, v in sorted(stt.items()))
                                 or "（无）"))
    tt = s.get("types") or {}
    L.append("  类型分布   ：" + (", ".join(f"{k}x{v}" for k, v in sorted(tt.items()))
                                 or "（无）"))
    for h, d in sorted((s.get("by_host") or {}).items(),
                       key=lambda x: -x[1].get("total", 0))[:12]:
        L.append(f"      {h:40s} {d.get('total', 0):4d} 条"
                 f"（json {d.get('json', 0)} / media {d.get('media', 0)}）")
    apis = s.get("json_apis") or []
    if apis:
        L.append(f"  JSON API（{len(apis)} 条，前 15）：")
        for a in apis[:15]:
            L.append(f"      {a.get('method', 'GET'):5s} {a.get('status', 0)}  {a.get('url', '')[:110]}")
    if s.get("auth_header_keys"):
        L.append("  请求头键名（只记键不记值）：" + ", ".join(s["auth_header_keys"]))
    if s.get("third_party"):
        L.append(f"  第三方 host：{', '.join(s['third_party'][:10])}")
    if stt.get("opaque_0"):
        L.append(f"  注：{stt['opaque_0']} 条跨域请求拿不到状态码（opaque）—— 只知道它发生了，"
                 f"不知道结果。这是浏览器的同源策略，不是抓取失败。")
    return "\n".join(L)


def render_media(r: DiagReport) -> str:
    m = r.media or {}
    rv = r.route_verdict or {}
    L = ["", "── 七、媒体与 DRM ──"]
    L.append(f"  工作台解析路线：{rv.get('route') or '（未判定）'}"
             f"　媒体候选 {rv.get('tracks_n', 0)} 条")
    if rv.get("routes"):
        L.append(f"  代表页出现过的路线：{', '.join(rv['routes'])}"
                 f"（整站模式下每类页面的解析路线可能不同）")
    for n in (rv.get("notes") or ())[:6]:
        L.append(f"      {n}")
    if m.get("key_systems"):
        L.append(f"  EME keySystem：{', '.join(m['key_systems'])}")
        L.append("      **本项目只识别不解密** —— 商业 DRM 一律跳过。")
    elif m.get("probed"):
        L.append("  未观测到 DRM 申请")
    return "\n".join(L)


def render_text(r: DiagReport) -> str:
    """文字可抽性 —— 拿数据表模板对真页面**干跑一遍**的结果。

    与「媒体与 DRM」并列：工作台两条腿，报告不能只有一条。
    """
    tp = r.text_probe or {}
    L = ["", "── 八、文字可抽性（数据表模板干跑）──"]
    if not tp:
        L.append("  本次未做干抽取（没取到页面正文）。")
        return "\n".join(L)
    if tp.get("note"):
        L.append(f"  说明：{tp['note']}")
    if not tp.get("ok"):
        L.append("  正文为空或结构化失败 —— 没有可抽的内容。")
        return "\n".join(L)
    L.append(f"  标题候选   ：{tp.get('titles_n', 0)} 条"
             + ("（0 条 = 靠标题兜底的列会抽空）" if not tp.get("titles_n") else ""))
    act = tp.get("active")
    if act:
        L.append(f"  当前绑定模板：「{act.get('name')}」"
                 f"命中 {act['hits']}/{act['total']} 业务列")
        for c in (act.get("cols") or []):
            v = (act.get("values") or {}).get(c)
            L.append(f"      {'✔' if v else '·'} {c}"
                     + (f"：{str(v)[:60]}" if v else "（空）"))
    else:
        L.append("  当前绑定模板：（未绑定 / 未找到）")
    rk = tp.get("ranking") or []
    if rk:
        L.append(f"  模板命中排行（共试 {tp.get('templates_n', 0)} 套，列前 {len(rk)}）：")
        for i, h in enumerate(rk, 1):
            L.append(f"      {i}. {h.get('name')}　{h['hits']}/{h['total']} 列"
                     f"（命中率 {h.get('rate')}）")
    dd = tp.get("dedup") or {}
    if dd:
        if dd.get("with_unique"):
            L.append(f"  落库去重   ：表 {', '.join(map(str, dd['with_unique']))} 有 UNIQUE 约束")
        else:
            L.append(f"  落库去重   ：**没有** UNIQUE 约束"
                     f"（看过 {len(dd.get('tables') or ())} 张表："
                     f"{', '.join(map(str, dd.get('tables') or ())) or '（无）'}）"
                     " —— 换个会话重抓同一个站会**再插一遍**，不报错。")
    elif tp:
        L.append("  落库去重   ：未确认（还没建过库 / 读不动）—— 不替你打包票。")
    L.append("  边界：这里只说「这一页的正文里有没有这些字段」。列表页翻不翻得动、"
             "要不要登录、限不限频，**都不在这一节射程内**。")
    return "\n".join(L)


def render_risk(r: DiagReport) -> str:
    L = ["", "── 九、结论与风险（逐条结论码）──"]
    fs = list(r.findings or ())
    if not fs:
        L.append("  （无）")
        return "\n".join(L)
    order = {"blocker": 0, "high": 1, "mid": 2, "low": 3, "info": 4}
    for f in sorted(fs, key=lambda x: order.get(x.severity, 9)):
        L.append(f"  {_sev_mark(f.severity)} {f.label}")
        if f.detail:
            L.append(f"        {f.detail}")
        L.append(f"        码 {f.code}　来源 {f.source}")
    risk = r.risk or {}
    if risk.get("reasons"):
        L.append("")
        L.append(f"  难度档位 {risk.get('level')} / {risk.get('name')}"
                 f"　依据：{', '.join(risk['reasons'])}")
    return "\n".join(L)


def render_knowledge(r: DiagReport) -> str:
    k = r.knowledge or {}
    L = ["", "── 十、知识库命中 ──"]
    hit = (k.get("hard_site") or k.get("challenge") or k.get("recipe")
           or k.get("pacing") or k.get("block_words"))
    if not hit:
        L.append("  （本次无命中）")
        return "\n".join(L)
    if k.get("hard_site"):
        hs = k["hard_site"]
        L.append(f"  内置高难度站点表：key={hs.get('key')}"
                 f"　anti_level={hs.get('anti_level')}"
                 f"　命中 url_patterns={hs.get('matched_pattern')}")
    ch = k.get("challenge") or {}
    if ch.get("type") and ch.get("type") != "unknown":
        L.append(f"  反爬厂商特征：{ch['type']}（置信度 {ch.get('confidence')}，"
                 f"命中位置 {'、'.join(ch.get('where') or ())}）"
                 f"　—— 静态文本匹配，与 detect_challenge_type 同一张表")
    rc = k.get("recipe") or {}
    if rc.get("name"):
        L.append(f"  已有媒体配方：{rc['name']}（来源 {rc.get('source')}，"
                 f"命中 host_suffix={rc.get('matched')}）")
    pc = k.get("pacing") or {}
    if pc:
        L.append(f"  该站建议节拍：{pc.get('min')} ~ {pc.get('max')} 秒/请求"
                 f"（站键 {pc.get('key')}）")
    bw = k.get("block_words") or {}
    if bw.get("hits"):
        L.append(f"  已知拦截词命中：{'、'.join(bw['hits'])}"
                 f"（站键 {bw.get('key')}）　—— 站方明确拒绝的措辞")
    if k.get("index_summary"):
        s = k["index_summary"]
        L.append(f"  知识库规模：反爬特征 {s.get('challenge')} 条 / 高难度站点 {s.get('hard')} 个 / "
                 f"节拍 {s.get('delay')} 个 / 拦截词表 {s.get('blocks')} 个 / 配方 {s.get('recipes')} 份")
    for e in (k.get("errors") or ()):
        L.append(f"  ⚠ 知识库：{e}")
    return "\n".join(L)


def render_history(r: DiagReport) -> str:
    h = r.history or {}
    L = ["", "── 十一、历史（旁证，不参与判级）──"]
    if not h or not h.get("entries"):
        L.append("  （该 host 无历史诊断记录）")
        return "\n".join(L)
    L.append(f"  该 host 共 {h.get('total', 0)} 条历史记录，最近 {len(h['entries'])} 条：")
    for e in h["entries"]:
        L.append(f"      {e.get('at', '')}  {e.get('level', '?')}"
                 f"  {e.get('verdict', '')}"
                 f"  结论 {len(e.get('codes') or [])} 条")
    if h.get("error"):
        L.append(f"  ⚠ 读历史出错：{h['error']}")
    L.append("  **这几条只作旁证：判级不看它**（避免同一条记录反复自我印证）。")
    return "\n".join(L)


def render_scope(r: DiagReport) -> str:
    """本次探测的范围与代价 —— **不隐藏请求次数、不隐藏没走到哪**。"""
    L = ["", "── 十二、本次探测范围与代价 ──"]
    b = r.budget or {}
    if r.scope == "site":
        L.append(f"  档位：{r.profile_name or r.profile or '（未指定）'}")
        L.append(f"  导览阶段 GET（robots + sitemap）：{b.get('sitemaps_fetched', 0)} 次"
                 f"（其中 sitemap 展开成功 {b.get('sitemaps_ok', 0)} 份）")
        L.append(f"  页面诊断请求：{b.get('pages_done', 0)} 次"
                 f"（上限 {'不限' if not b.get('max_pages') else str(b.get('max_pages')) + ' 页'}）"
                 f"　恒串行，页间 ≥{b.get('delay', 0)} 秒")
        L.append(f"  实际耗时：{r.elapsed_ms / 1000.0:.1f} 秒")
        # 计数口径必须说死：路由解析走的是既有 media_resolve，**它的内部请求数本项目数不出来**。
        # 唯一的保证是「每模板至多调一次、且调之前先扣了账」—— 照实写，别含糊。
        ex = int(b.get("external_calls") or 0)
        L.append(f"  **计数口径**：上面这个数含 {ex} 次媒体路由解析"
                 f"（每个模板至多一次，调之前已先扣账）。"
                 "路由解析走的是既有 media_resolve，"
                 "**它内部还会自己发若干次请求，本项目数不出准确条数** ——"
                 "所以真实请求数 ≥ 上面这个数。每模板一次的硬限就是为此设的。")
        if b.get("truncated"):
            left = b.get("templates_untouched") or []
            L.append(f"  [!] 预算用尽，有 {len(left)} 个模板一个代表页都没跑到"
                     f"—— 报告里没有它们，**不是它们没问题**。")
        if b.get("aborted"):
            L.append("  [!] 本次因 429 提前中止，覆盖不完整。")
    else:
        n = int((r.probe or {}).get("requests_n") or 0)
        if n:
            L.append(f"  对目标站发起页面请求：{n} 次（恒串行）")
        L.append("  另有 1 次媒体路由解析（既有 media_resolve，"
                 "它内部还会自己发若干次请求、条数数不出来）—— 真实请求数 ≥ 上面这个数。")
        L.append(f"  实际耗时：{r.elapsed_ms / 1000.0:.1f} 秒")
    L.append("  覆盖声明：静态页 + 浏览器阶段主文档与同源 frame 的 XHR/fetch，"
             "外加 performance resource 可见项；跨源 iframe 内部的响应体不可见。")
    L.append("  代理：未使用（诊断走直连，保证结果可复现）。")
    if r.scope != "site":
        L.append("  本次为**单页**诊断 —— 结论只对这一个 URL 负责，"
                 "不代表同站其它页面（整站模式会把 URL 空间铺开再判）。")
    return "\n".join(L)


def render_suggestions(r: DiagReport) -> str:
    L = ["", "── 十三、建议 ──"]
    for i, s in enumerate(r.suggestions or (), 1):
        L.append(f"  {i}. {s}")
    if not r.suggestions:
        L.append("  （无）")
    return "\n".join(L)


def render_report(r: DiagReport) -> str:
    """把 12 个分节拼成完整报告，并把各节存进 r.sections（供 GUI 分页显示）。

    顺序固定 —— 结论（七）排在观测（三/四/五）之后、账目（十）之前。
    单页模式下三、四两节会渲染成"本次没做"，**不隐藏**（空着会让人以为是漏了）。
    """
    secs = {
        "overview": render_overview(r),
        "coverage": render_coverage(r),     # 排在细节之前：它限定后面每一节的效力
        "frontend": render_frontend(r),
        "rules": render_rules(r),
        "structure": render_structure(r),
        "penetration": render_penetration(r),
        "backend": render_backend(r),
        "media": render_media(r),
        "text": render_text(r),             # 文字轴：与媒体轴并列的另一条腿
        "risk": render_risk(r),
        "knowledge": render_knowledge(r),
        "history": render_history(r),
        "scope": render_scope(r),
        "suggestions": render_suggestions(r),
    }
    r.sections = secs
    out = "\n".join(secs[k] for k in
                    ("overview", "coverage", "frontend", "rules", "structure", "penetration",
                     "backend", "media", "text", "risk", "knowledge", "history",
                     "scope", "suggestions"))
    r.text = out
    return out


COV_DONE, COV_PARTIAL, COV_SKIPPED = "done", "partial", "skipped"


def build_coverage(report: DiagReport, *, used_browser: bool = False) -> dict:
    """本次诊断**覆盖到哪儿、没覆盖哪儿**。

    存在的理由（2026-09-11 猫耳FM 那次）：`build_feasibility` 的兜底分支**不读任何覆盖
    信息**，于是「没测」和「测了没问题」在报告里长得一模一样，最后落成一句很确定的
    「未发现硬阻碍 → 可以试」—— 而那是个整站 FairPlay。它不是判错了，是**它没有那个
    信息却照样说了一句确定的话**。

    每条轴三样都给：state（done/partial/skipped）、why（为什么没做）、fix（怎么补上）。
    **只陈述事实，不参与判级** —— 与本项目其余纪律一致（覆盖度是「我们走了多远」，
    不是「站方怎么样」）。
    """
    axes = []
    rv = report.route_verdict or {}
    # 整站模式的判别结果并上来后放在 media 里（见代表页聚合），单页模式在 route_verdict 里。
    dp = rv.get("drm_probe") or (report.media or {}).get("drm_probe") or {}

    def add(key, label, state, why="", fix=""):
        axes.append({"key": key, "label": label, "state": state, "why": why, "fix": fix})

    # ── 前端指纹 / 静态正文 ──
    f = report.fingerprint or {}
    if f.get("ok"):
        add("front", "前端指纹（静态正文）", COV_DONE)
    else:
        add("front", "前端指纹（静态正文）", COV_SKIPPED,
            "没取到页面正文", "看「可达性」结论；确认 URL 与网络")

    # ── robots ──
    if report.rules:
        add("rules", "robots.txt 声明的规矩", COV_DONE)
    else:
        add("rules", "robots.txt 声明的规矩", COV_SKIPPED,
            "未抓取或抓取失败", "重跑一次；站方也可能把它挂在别的路径")

    # ── 媒体解析路线 ──
    if rv.get("route"):
        add("media", "媒体解析路线", COV_DONE)
    else:
        add("media", "媒体解析路线", COV_SKIPPED, "未解析出路线",
            "开浏览器兜底一条线")

    # ── 静态 DRM（清单）──
    probed, cands = int(dp.get("probed") or 0), int(dp.get("candidates") or 0)
    if probed:
        if cands > probed:
            add("drm_manifest", "DRM：媒体清单静态判别", COV_PARTIAL,
                f"已判 {probed} 条清单，候选共 {cands} 条 —— **其余未判，未判 ≠ 无 DRM**",
                "调大 DIAG_DRM_MAX_TRACKS 可提高抽样数（每条多 1~2 次请求）")
        else:
            add("drm_manifest", "DRM：媒体清单静态判别", COV_DONE, f"已判 {probed} 条清单")
    elif cands:
        add("drm_manifest", "DRM：媒体清单静态判别", COV_SKIPPED,
            f"有 {cands} 条清单候选，一条也没判成", "看 notes 里的取样失败原因")
    elif rv:
        add("drm_manifest", "DRM：媒体清单静态判别", COV_SKIPPED,
            "本次没有清单候选（m3u8/mpd），无从判别")
    else:
        add("drm_manifest", "DRM：媒体清单静态判别", COV_SKIPPED,
            "路由未解析出来，没有候选可判")

    # ── 文字轴（干抽取）──
    tp = report.text_probe or {}
    if tp.get("ok") and tp.get("ranking"):
        hits = tp["ranking"][0].get("hits") or 0
        if hits:
            add("text", "文字可抽性（模板干跑）", COV_DONE,
                f"最好的一套模板命中 {hits} 个业务列")
        else:
            add("text", "文字可抽性（模板干跑）", COV_PARTIAL,
                f"试了 {tp.get('templates_n', 0)} 套模板，业务列全空 —— "
                "内容多半不在正文里（纯前端渲染 / 壳页 / 需登录）",
                "开浏览器看渲染后的内容，或改用别的抓法")
    elif tp:
        add("text", "文字可抽性（模板干跑）", COV_SKIPPED,
            tp.get("note") or "没取到可抽的正文")
    else:
        add("text", "文字可抽性（模板干跑）", COV_SKIPPED, "本篇没做出代表页，未跑干抽取")

    # ── 浏览器专属的那几条线 ──
    if used_browser:
        add("browser", "EME / 运行期请求 / 跨源 iframe", COV_DONE)
    else:
        add("browser", "EME / 运行期请求 / 跨源 iframe", COV_SKIPPED,
            "本次未开浏览器",
            "勾上「启用浏览器观测」重跑 —— **这是 EME 唯一的观测通道**，"
            "本节能测的只是清单里写下的 DRM")

    # ── 整站专有 ──
    if report.scope == "site":
        disc = report.discovery or {}
        st = report.structure or {}
        tmpls = st.get("templates") or {}
        if disc.get("sitemap_absent"):
            add("sitemap", "URL 全集（sitemap 递归展开）", COV_PARTIAL,
                "站方没有 sitemap，URL 全集只来自首页链接 —— **这是下限，不是全部**",
                "换更深的档位多铺几层")
        elif tmpls:
            add("sitemap", "URL 全集（sitemap 递归展开）", COV_DONE)
        else:
            add("sitemap", "URL 全集（sitemap 递归展开）", COV_SKIPPED, "导览阶段没铺开")
        if tmpls:
            add("structure", "结构地图（模板归约）", COV_DONE, f"{len(tmpls)} 个模板")
        else:
            add("structure", "结构地图（模板归约）", COV_SKIPPED, "没归约出模板",
                "站方结构很平或样本太少时会发生")
        pg = report.pages or []
        if pg:
            add("penetration", "代表页深诊 + 翻页探针", COV_DONE, f"{len(pg)} 个代表页")
        else:
            add("penetration", "代表页深诊 + 翻页探针", COV_SKIPPED, "没跑到代表页")
        b = report.budget or {}
        if b.get("aborted"):
            add("budget", "请求预算", COV_PARTIAL,
                f"中途被中止（{b.get('abort_reason') or '?'}）—— 下面的覆盖是残缺的",
                "放宽预算或换档位重跑")
        elif b.get("truncated"):
            add("budget", "请求预算", COV_PARTIAL,
                f"预算用尽，{len(b.get('templates_untouched') or ())} 个模板一个代表页都没跑到",
                "换更浅的档位，或提高预算上限")
        else:
            add("budget", "请求预算", COV_DONE)

    missing = [a for a in axes if a["state"] != COV_DONE]
    return {"axes": axes, "missing_n": len(missing),
            "missing_labels": [a["label"] for a in missing],
            "partial_n": sum(1 for a in axes if a["state"] == COV_PARTIAL)}


def build_feasibility(report: DiagReport) -> dict:
    """可行性判定（纯函数）：把 findings 收敛成一句人话。"""
    codes = {getattr(f, "code", "") for f in (report.findings or ())}
    rv = report.route_verdict or {}
    route = str(rv.get("route") or "")

    if codes & {C_REACH_DNS, C_REACH_TLS, C_REACH_CONN, C_REACH_TIMEOUT}:
        return {"verdict": "够不着",
                "summary": "根本没连上，先解决可达性再谈抓取。"}
    if C_FRONT_CHALLENGE in codes or C_CHALLENGE_MAN in codes:
        return {"verdict": "先过验证再说",
                "summary": "有反爬验证挡在门口。人工过验之前，后面的一切结论都不作数。"}
    if C_FRONT_DRM in codes:
        return {"verdict": "不做",
                "summary": "命中商业 DRM，本项目只识别不解密。"}
    if C_SITE_ABORT429 in codes:
        return {"verdict": "能抓，但被限频打断",
                "summary": "探到一半目标站开始限频，诊断**立刻停手**了 —— "
                           "所以下面的覆盖是残缺的，别拿它当全貌。"}
    if C_RATE_LIMIT in codes:
        return {"verdict": "能抓，但要压节拍",
                "summary": "已经被限频了。降速、串行、别并发。"}
    # 整站模式特有：翻页是假的这件事，比"能不能抓"更该先说 ——
    # 抓得下来 ≠ 拿得全。答不出「能不能穷举」，整站抓取就只是抽奖。
    if C_SITE_NO_PAGE in codes:
        bad = [f.evidence.get("tmpl") for f in (report.findings or ())
               if getattr(f, "code", "") == C_SITE_NO_PAGE]
        return {"verdict": "能抓，但翻页是假的",
                "summary": f"{len(bad)} 个列表页模板的页码参数被忽略"
                           f"（HTTP 200，内容却是第一页）。这类页面**抓得下来，穷举不了** —— "
                           f"先确认内容是不是只能一页一页手工喂。"}
    # 已有配方是**最强的一条正面证据**：它不是推测，是工作台自己跑通过的成文经验。
    # 但必须排在上面那些硬阻碍之后 —— 有配方也救不了 DRM / 挑战页 / 够不着。
    rc = (report.knowledge or {}).get("recipe") or {}
    if rc.get("name"):
        return {"verdict": "能抓（已有配方）",
                "summary": f"工作台已有该站配方「{rc['name']}」且处于启用状态，"
                           f"可直接走媒体配方，不必从零摸。"}
    if route and route not in ("none", ""):
        return {"verdict": "能抓",
                "summary": f"工作台已能走通解析（路线：{route}），可直接试抓。"}
    if C_FRONT_SPA in codes:
        return {"verdict": "能抓，但要开浏览器",
                "summary": "纯前端渲染，静态拿不到内容，走浏览器兜底线。"}

    # ── 兜底分支：**不准再说无条件的话** ──
    # 这里曾经恒定返回「未发现硬阻碍 → 可以试」。问题不在措辞，在**它没有那个信息**：
    # 上面 9 个分支一个都没命中，只说明「没发现阻碍」，不等于「检查过了没阻碍」。
    # 猫耳FM 那次就是这么被说成「可以试」的 —— 那是个整站 FairPlay，浏览器没开、
    # 清单没取，于是两条 DRM 通道**同时沉默**，沉默被读成了通过。
    cov = report.coverage or {}
    miss = [a for a in (cov.get("axes") or ()) if a.get("state") != COV_DONE]
    hard_miss = [a for a in miss if a.get("key") in ("drm_manifest", "media", "browser")]
    if hard_miss:
        names = "、".join(str(a.get("label")) for a in hard_miss)
        return {"verdict": "可以试（覆盖不全）",
                "summary": f"没发现硬阻碍，但**本次没测到的东西里有要紧的**：{names}。"
                           f"在补齐之前，这句话只代表「没撞上」，不代表「没有」。"}
    if miss:
        names = "、".join(str(a.get("label")) for a in miss)
        return {"verdict": "可以试（有未测项）",
                "summary": f"没发现硬阻碍。本次未覆盖：{names} —— 见「本次没覆盖什么」节。"}
    # 全覆盖且无阻碍：这时才配说一句完整的话。
    return {"verdict": "可以试",
            "summary": "本次该测的都测了，未发现硬阻碍。建议先小量试抓再放量。"}


# ================================================================
# Layer 3：单页编排
# ================================================================
def _log(log, msg: str) -> None:
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _knowledge_stage(url, *, index=None, facts=None, title="", body="", status=0,
                     hard_site=None, log=None) -> dict:
    """知识层（P2）：这一页在知识库里命中了什么。**绝不抛。**

    知识库坏了（qiongqi_maw 导不进来 / 配方目录没了）只能让「知识」这一节空着，
    绝不能让整份诊断失败 —— 旁证再重要也不配掀桌子。

    facts 参数给了就直接用（离线用例注入）；否则现建索引跑一遍匹配。
    """
    if isinstance(facts, dict):
        return facts
    try:
        import site_knowledge as sk
        idx = index if isinstance(index, dict) else sk.build_index()
        f = sk.knowledge_facts(index=idx, url=url, title=title, body=body,
                               status=status, hard_site=hard_site)
        s = sk.index_summary(idx)
        if any(s.values()):
            f["index_summary"] = s
        errs = [e for e in (idx.get("errors") or ()) if e]
        if errs:
            f["errors"] = errs
            for e in errs:
                _log(log, f"知识库：{e}")
        return f
    except Exception as e:
        _log(log, f"知识库不可用：{e!r}")
        return {"hard_site": hard_site} if hard_site else {}


def _robots_url(url: str) -> str:
    try:
        sp = urlsplit(url)
        return f"{sp.scheme}://{sp.netloc}/robots.txt"
    except Exception:
        return ""


def fetch_robots(url: str, *, log=None, parsed=None, timeout=None, probe_fn=None) -> dict:
    """抓 + 解析 robots.txt。**绝不让 robots 抓取失败影响主诊断。**
    parsed 非 None 则跳过网络（离线用例注入）。

    probe_fn：整站模式把请求塞进它自己的账房（计数 + 节拍 + 429 中止）时用。
    """
    if parsed is not None:
        return parsed
    ru = _robots_url(url)
    if not ru:
        return parse_robots("")
    try:
        p = (probe_fn or probe_http)(ru, timeout=timeout, max_bytes=_ROBOTS_MAX_CHARS,
                                     want_body=True, allow_curl_fallback=False)
        if not p:
            # 账房拒绝（预算用尽 / 已中止）—— 视为无声明，但要说出来
            _log(log, "robots.txt 未取（预算用尽或已中止）—— 视为无声明")
            return parse_robots("")
        st = int(p.get("status") or 0)
        if st and st != 200:
            _log(log, f"robots.txt 返回 HTTP {st} —— 视为无声明")
            return parse_robots("")
        if p.get("error_kind"):
            _log(log, f"robots.txt 取不到（{p.get('error_kind')}）—— 视为无声明")
            return parse_robots("")
        return parse_robots(p.get("body") or "")
    except Exception as e:
        _log(log, f"robots.txt 解析异常：{e!r} —— 视为无声明")
        return parse_robots("")


def _stage_browser(url, *, wait_sec, driver=None, log=None, stop_flag=None,
                   on_row=None, rounds=None) -> tuple:
    """浏览器阶段：真机观测有界。返回 (rows, hdr, eme, notes)。绝不 raise。"""
    rows, hdr, eme, notes = [], [], [], []
    own = driver is None
    try:
        import video_grab as vg
    except Exception as e:
        notes.append(f"video_grab 不可用：{e!r}")
        return rows, hdr, eme, notes
    if driver is None:
        try:
            driver = vg._acquire_browser(url)
        except Exception as e:
            notes.append(f"浏览器不可用：{e!r}")
            return rows, hdr, eme, notes
    try:
        _install_diag_patch(driver)
        try:
            driver.get(url)
        except Exception as e:
            notes.append(f"打开页面失败：{e!r}")
            return rows, hdr, eme, notes

        _log(log, f"已打开页面，开始 {wait_sec} 秒有界观测…")
        deadline = time.time() + max(1, int(wait_sec))
        n_rounds = int(rounds if rounds is not None else _cfg_get("DIAG_ROUNDS", 0) or 0)
        rnd = 0
        seen = set()
        while time.time() < deadline:
            if stop_flag is not None and stop_flag.is_set():
                notes.append("用户手动停止观测")
                break
            if n_rounds and rnd >= n_rounds:
                break
            rnd += 1
            try:
                driver.execute_script(_DIAG_TRIGGER_JS)
            except Exception:
                pass
            # 切片 sleep：停止要在 1 秒内响应，不能一觉睡 1.2s
            left = deadline - time.time()
            for _ in range(12):
                if left <= 0 or (stop_flag is not None and stop_flag.is_set()):
                    break
                time.sleep(min(0.1, max(0.0, left)))
                left = deadline - time.time()
            try:
                got = _collect_diag_rows(driver, page_url=url)
            except Exception:
                continue
            if on_row:
                for r in got["rows"]:
                    k = (r.method, r.url, r.status)
                    if k in seen:
                        continue
                    seen.add(k)
                    try:
                        on_row(r)
                    except Exception:
                        pass
            rows = got["rows"] or rows
            for x in got["hdr"]:
                if x not in hdr:
                    hdr.append(x)
            for x in got["eme"]:
                if x not in eme:
                    eme.append(x)

        try:
            notes.append(f"观测 {rnd} 轮，累计请求 {len(rows)} 条，"
                         f"frame {_collect_diag_rows(driver, page_url=url).get('frames', 0)} 个")
        except Exception:
            pass
        try:
            import qiongqi_maw as qm
            ch = qm.detect_challenge_type(driver) or {}
            if ch.get("type"):
                notes.append(f"挑战识别：{ch.get('type')}（置信度 {ch.get('confidence')}）")
        except Exception:
            pass
    finally:
        if own and driver is not None:
            try:
                vg._release_browser(driver)
            except Exception:
                pass
    return rows, hdr, eme, notes


def run_diagnosis(url: str, *, wait_sec=None, use_browser=None, log=None,
                  stop_flag=None, driver=None, on_row=None,
                  hard_cfg=None, robots_parsed=None, probe=None,
                  route_verdict=None, skip_network: bool = False,
                  knowledge_index=None, knowledge_facts=None,
                  history_path=None, record_history: bool = True,
                  read_history: bool = True) -> DiagReport:
    """单页诊断。公开入口**绝不 raise**，失败都收进 notes。

    probe / route_verdict / robots_parsed / driver 都是**注入点** —— 离线用例靠它们
    不碰网络；传 None 才走真实来源。

    P2 新增的三个注入点：
      · knowledge_index —— 知识库索引（整站模式会建一次、所有页复用；单页传 None 就现建）
      · knowledge_facts —— 直接给"命中了什么"的事实，跳过索引与匹配（离线用例用）
      · history_path / record_history —— 历史读写落点与开关（离线用例指到临时文件，
        或直接 record_history=False）。**历史只是旁证：它进不了 findings，也进不了判级。**
    """
    t0 = time.time()
    rep = DiagReport(url=url or "", scope="page")
    if wait_sec is None:
        wait_sec = int(_cfg_get("DIAG_WAIT_DEFAULT", 15) or 15)
    if use_browser is None:
        use_browser = bool(_cfg_get("DIAG_USE_BROWSER", True))
    n_requests = 0

    if not url or _host_of(url) == "" or not url.lower().startswith(("http://", "https://")):
        rep.notes.append("只支持 http/https 的单条 URL")
        rep.feasibility = {"verdict": "输入有误", "summary": "URL 不合法。"}
        render_report(rep)
        rep.elapsed_ms = int((time.time() - t0) * 1000)
        return rep

    # ── 1. 探针（状态码级）──
    if probe is None and not skip_network:
        _log(log, f"探测 {url} …")
        try:
            probe = probe_http(url)
            n_requests += 1
        except Exception as e:
            rep.notes.append(f"探针异常：{e!r}")
            probe = {}
    probe = dict(probe or {})

    # ── 2. 静态指纹（用探针拿回来的 body，不再发第二次请求）──
    html = probe.get("body") or ""
    rep.final_url = probe.get("final_url") or url
    st = int(probe.get("status") or 0)
    if html:
        try:
            rep.fingerprint = fingerprint_html(html, url, rep.final_url)
            rep.title = rep.fingerprint.get("title", "")
        except Exception as e:
            rep.notes.append(f"指纹解析异常：{e!r}")
    elif st and st != 200:
        rep.notes.append(f"HTTP {st} —— 没有正文可做指纹（这正是 fetch_html 会吞掉的信息）")
    elif probe.get("error_kind"):
        rep.notes.append(f"取不到页面正文：{probe.get('error_kind')}")

    # ── 3. robots ──
    try:
        parsed = fetch_robots(url, log=log, parsed=robots_parsed)
        rep.rules = robots_page_rules(parsed, url)
        if not robots_parsed and not skip_network:
            n_requests += 1
    except Exception as e:
        rep.notes.append(f"robots 阶段异常：{e!r}")
        rep.rules = {}

    # ── 4. 路由判定（复用 media_resolve，绝不起 Chrome）──
    if route_verdict is None and not skip_network:
        try:
            import media_resolve as mr
            res = mr.resolve_page(url, use_browser=False)
            tracks = getattr(res, "tracks", None) or []
            rep.route_verdict = {"route": str(getattr(res, "route", "") or ""),
                                 "tracks_n": len(tracks),
                                 "notes": list(getattr(res, "notes", None) or [])}
            # 静态 DRM 判别：**不开浏览器也能判「拿得到、但用不了」**。
            # resolve_page 只解析出地址、不发清单请求，所以 track.drm 是空的；
            # 那一步非得自己取清单不可（每条 1~2 次 GET，抽样上限见常量）。
            rep.route_verdict["drm_probe"] = probe_track_drm(
                tracks, log=log, stop_flag=stop_flag)
        except Exception as e:
            rep.notes.append(f"路由判定不可用：{e!r}")
            rep.route_verdict = {}
    elif route_verdict is not None:
        rep.route_verdict = dict(route_verdict)

    # ── 5. 浏览器观测 ──
    rows, hdr, eme, bnotes = [], [], [], []
    if use_browser:
        rows, hdr, eme, bnotes = _stage_browser(
            url, wait_sec=wait_sec, driver=driver, log=log,
            stop_flag=stop_flag, on_row=on_row)
        rep.notes.extend(bnotes)
    rep.rows = _dedup_rows(rows)
    rep.summary = summarize_requests(rep.rows, page_url=url)
    merge_header_keys(rep.summary, hdr)

    # ── 6. 合成 findings ──
    # 高难度站点表命中要**先算**：知识层的节拍与拦截词都以它的 key 为索引。
    hard_site = match_hard_site(url, hard_cfg=hard_cfg)
    kfacts = _knowledge_stage(url, index=knowledge_index, facts=knowledge_facts,
                              title=rep.title, body=html, status=st, hard_site=hard_site,
                              log=log)

    # 文字轴：拿数据表模板对本页干跑一遍。**纯解析、零请求**（schema_extract 是纯函数），
    # 所以它排在这里不影响任何网络纪律；而它答的是动手前最想问的两个问题。
    try:
        rep.text_probe = probe_text_extraction(html, url=url)
    except Exception as e:
        rep.text_probe = {"ok": False, "note": f"文字轴探针异常：{e!r}"}

    rep.findings = (findings_from_probe(probe)
                    + findings_from_static(rep.fingerprint, not_found=(st == 404))
                    + findings_from_rules(rep.rules, url)
                    + findings_from_text(rep.text_probe)
                    + findings_from_route(rep.route_verdict)
                    + findings_from_eme(eme)
                    + findings_from_knowledge(kfacts, status=st))

    # 浏览器观测里落在 robots 禁止名单上的路径（用户要的「知道底线」落点）
    hits = current_page_rule(rep.rows, robots_parsed if robots_parsed is not None else _last_parsed(rep))
    if hits:
        for h in hits[:20]:
            rep.findings.append(finding(
                C_RULES_HIT, f"观测到的请求落在 robots 禁止名单：{h['path']}",
                f"命中 Disallow: {h.get('rule')}", SEV_HIGH, SRC_BROWSER,
                url=h["url"], rule=h.get("rule")))

    # ── 7. 难度 / 可行性 / 建议 ──
    reachable = bool(st) or not (probe.get("error_kind") or "")
    rep.risk = grade_difficulty(findings=rep.findings, hard_site=hard_site,
                                reachable=reachable)
    rep.knowledge = {k: v for k, v in kfacts.items()
                     if v and k not in ("host",)} if kfacts else {}
    rep.media = {"key_systems": list(eme), "probed": bool(use_browser)}

    # ── 7.5 历史（旁证；只读进来，**不参与上面任何一步判断**）──
    # read_history=False：整站模式的代表页用得上 —— 那些子报告只取 findings 与摘要，
    # 读回来的历史既没人看又会变成 N 次磁盘读（每读一次都要把整份 json 解析一遍）。
    if read_history:
        try:
            import site_knowledge as sk
            rep.history = sk.history_for(url, path=history_path)
        except Exception as e:
            rep.history = {"host": _host_of(url), "total": 0, "entries": [], "error": repr(e)}

    # 覆盖声明必须**先于**可行性算出来 —— 兜底分支要读它，否则「没测」又会被说成「可以试」。
    rep.coverage = build_coverage(rep, used_browser=bool(use_browser))
    rep.feasibility = build_feasibility(rep)
    rep.suggestions = build_suggestions(rep)

    probe["requests_n"] = n_requests
    rep.probe = probe
    rep.elapsed_ms = int((time.time() - t0) * 1000)

    # ── 8. 落历史（**只存结论码与计数**；失败只记一笔，绝不影响报告）──
    # 放在渲染**之前**：这样"没落上盘"这件事能进 notes，进而进报告 —— 静默失败是最坏的。
    # 注意 rep.history 是上面读进来的**快照**，这里写盘不会把它变成"本次诊断"。
    if record_history:
        try:
            import site_knowledge as sk
            ok, err = sk.record(rep, path=history_path)
            if not ok and err:
                rep.notes.append(f"历史未落盘：{err}")
        except Exception as e:
            rep.notes.append(f"历史未落盘：{e!r}")

    # ── 9. 渲染 ──
    render_report(rep)
    _log(log, f"诊断完成：{rep.feasibility.get('verdict')}　"
              f"难度 {rep.risk.get('level')}　耗时 {rep.elapsed_ms / 1000.0:.1f}s")
    return rep


# ================================================================
# Layer 3-b：整站编排（P3）
#
# 深度用时间换，绝不靠并发换 —— 恒串行、页间 ≥delay 秒、只发 GET、429 立刻停。
# ================================================================
SITE_PROFILES = {
    "shallow": {
        "name": "浅", "per_template": 1, "max_templates": 8, "max_pages": 20,
        "page_depth": 0, "browser_pages": 0, "max_sitemaps": 4, "max_urls": 3000,
        "note": "只铺个面：每类页面看一眼，不翻页。",
    },
    "standard": {
        "name": "标准深诊", "per_template": 2, "max_templates": 24, "max_pages": 60,
        "page_depth": 3, "browser_pages": 3, "max_sitemaps": 20, "max_urls": 30000,
        "note": "默认。结构铺全 + 每类抽 2 页 + 列表页翻 3 页。",
    },
    "deep": {
        "name": "极致", "per_template": 5, "max_templates": 60, "max_pages": 200,
        "page_depth": 10, "browser_pages": 8, "max_sitemaps": 60, "max_urls": 200000,
        "note": "每类抽 5 页、翻 10 页。大站要跑到半小时以上。",
    },
    "all": {
        "name": "全部", "per_template": 0, "max_templates": 0, "max_pages": 0,
        "page_depth": 0, "browser_pages": 20, "max_sitemaps": 0, "max_urls": 0,
        "note": "把结构地图里每条 URL 都走一遍。可能几小时，恒串行、随时可停。",
    },
}
_PROFILE_ORDER = ("shallow", "standard", "deep", "all")

# 导览的硬顶：某个站的 sitemap 可能铺出百万条 URL，内存不能跟着走。
# **撞上了必须在报告里写明** —— 静默截断会让人以为「结构就是这些」。
_URLS_HARD = 500000
_SITEMAP_MAX_BYTES = 16 * 1024 * 1024
# 单个列表页最多试几个翻页候选（每个候选都是一次真实请求）
_PAGE_CAND_MAX = 4


def site_profile(key, *, profiles=None) -> dict:
    """档位键 -> 档位参数（纯函数）。未知键**回落 standard** 并把键名带出去，
    绝不静默给个空档 —— 空档会让 max_pages 变成 0，那等于不限量。"""
    table = profiles if isinstance(profiles, dict) and profiles else SITE_PROFILES
    k = str(key or "").strip().lower()
    if k not in table:
        k = "standard"
    p = dict(table.get(k) or {})
    p["key"] = k
    return p


def merge_findings(per_page) -> list:
    """多页结论 -> 一份**按结论码聚合**的清单（纯函数）。

    per_page 形如 [(url, [DiagFinding, ...]), ...]。

    20 个页面都报 `front.ssr` 时，报告里不该出现 20 条一模一样的话 ——
    应该是**一条**「20/20 页都是这样」+ 两三个例子 URL。严重度取**见过的最高**
    （聚合只能让结论更重，不会更轻）。同一页的重复码只算一次。
    """
    order = {"blocker": 0, "high": 1, "mid": 2, "low": 3, "info": 4}
    agg = {}
    for item in (per_page or ()):
        try:
            u, fs = item
        except Exception:
            continue
        seen_here = set()
        for f in (fs or ()):
            code = getattr(f, "code", "") or ""
            if not code or code in seen_here:
                continue
            seen_here.add(code)
            a = agg.get(code)
            if a is None:
                a = agg[code] = {"f": f, "n": 0, "ex": [],
                                 "sev": getattr(f, "severity", SEV_INFO)}
            a["n"] += 1
            if u and u not in a["ex"] and len(a["ex"]) < 3:
                a["ex"].append(u)
            if order.get(getattr(f, "severity", ""), 9) < order.get(a["sev"], 9):
                a["sev"] = getattr(f, "severity", SEV_INFO)
    out = []
    for code, a in agg.items():
        f = a["f"]
        if a["n"] <= 1:
            out.append(f)
            continue
        ev = dict(getattr(f, "evidence", {}) or {})
        ev["pages_n"] = a["n"]
        ev["examples"] = list(a["ex"])
        out.append(DiagFinding(code=f.code, label=f.label, detail=f.detail,
                               severity=a["sev"], source=f.source, evidence=ev))
    return out


class _SiteCrawler:
    """整站模式的**账房**：请求计数、预算、节拍、429 中止、同 URL 去重。

    所有网络都必须从 `.get()` 走 —— 这样「没超预算」和「没偷偷多发」是同一件事：
    预算判断与发请求写在同一个函数里，没有第二条路能绕过它。
    """

    def __init__(self, *, max_pages=0, delay=0.0, fetch=None, log=None,
                 stop_flag=None, timeout=None, max_bytes=None):
        self.max_pages = int(max_pages or 0)
        self.delay = max(0.0, float(delay or 0.0))
        self.fetch = fetch
        self.log = log
        self.stop_flag = stop_flag
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.pages = 0                 # 页面级 GET 次数（代表页 + 翻页候选）
        self.sitemaps = 0              # 导览阶段（robots/sitemap）GET 次数
        self.sitemaps_ok = 0
        self.external = 0              # 借 `charge()` 记的账（见那条方法）
        self.external_what = []
        self.aborted = False
        self.abort_reason = ""         # "429" | "stop" | ""
        self.abort_url = ""            # 撞上 429 的那一条（报告里要能指名）
        self.cache = {}                # url -> probe（**同一 URL 只打一次**）
        self._last = 0.0

    # ── 闸门 ────────────────────────────────────────────
    def _stopped(self) -> bool:
        return bool(self.stop_flag is not None and self.stop_flag.is_set())

    def can(self, *, kind="page") -> bool:
        if self.aborted or self._stopped():
            return False
        if kind == "page" and self.max_pages and self.pages >= self.max_pages:
            return False
        return True

    def _pace(self) -> bool:
        """页间节拍。切片睡，停止要在 1 秒内响应。"""
        if self.delay <= 0:
            return True
        left = self.delay - (time.time() - self._last)
        while left > 0:
            if self._stopped():
                return False
            time.sleep(min(0.1, left))
            left = self.delay - (time.time() - self._last)
        return True

    def charge(self, n: int = 1, *, what: str = "") -> bool:
        """给**接入不了 fetch 的既有子系统**记账 —— 目前只有 `media_resolve.resolve_page`。

        这是「所有网络都必须走 `.get()`」这条纪律的**唯一例外**，而且是**先扣后发**：
        预算不够就根本不调它，所以"没超预算"依然成立。
        但它**内部**发几次请求本项目数不出来（resolve_page 自带配方/静态/浏览器三条线），
        所以报告里如实写明这一点，不装作只发了 n 次 —— 见 `render_scope` 的计数口径。
        """
        if n <= 0:
            return True
        if not self.can(kind="page"):
            return False
        if self.max_pages and self.pages + n > self.max_pages:
            return False
        if not self._pace():
            self.aborted = True
            self.abort_reason = self.abort_reason or "stop"
            return False
        self.pages += n
        self.external += n
        if what:
            self.external_what.append(what)
        self._last = time.time()
        return True

    def get(self, url, *, kind="page", **kw):
        """发一次 GET（或命中缓存）。返回 probe 形状 dict，**拒绝时返回 None**。"""
        if not url:
            return None
        if url in self.cache:
            return self.cache[url]
        if not self.can(kind=kind):
            return None
        if not self._pace():
            self.aborted = True
            self.abort_reason = self.abort_reason or "stop"
            return None
        kw.setdefault("want_body", True)
        if self.timeout is not None:
            kw.setdefault("timeout", self.timeout)
        if self.max_bytes is not None and kind == "page":
            kw.setdefault("max_bytes", self.max_bytes)
        try:
            p = (self.fetch or probe_http)(url, **kw)
        except Exception as e:
            _log(self.log, f"取 {url} 异常：{e!r}")
            p = {"url": url, "status": 0, "body": "", "error_kind": "conn",
                 "error": repr(e)}
        self._last = time.time()
        if kind == "sitemap":
            self.sitemaps += 1
            if int((p or {}).get("status") or 0) == 200 and not (p or {}).get("error_kind"):
                self.sitemaps_ok += 1
        else:
            self.pages += 1
        if int((p or {}).get("status") or 0) == 429:
            # **立刻停整站**：继续探只会让情况更糟。本项目踩过「限速伪装成付费档」
            # ——200 + 合法 JSON + 空结果，不报任何错。
            self.aborted = True
            self.abort_reason = "429"
            self.abort_url = url
            _log(self.log, "目标站返回 429 —— 立即停止整站诊断，不再发任何请求")
        self.cache[url] = p
        return p


def fetch_sitemap_tree(crawler: _SiteCrawler, base_url: str, parsed_robots, *,
                       max_sitemaps=20, max_urls=30000, log=None) -> dict:
    """L1：sitemap index **递归**展开 → 全量 loc。**有界**（份数 / URL 数 / 环）。

    环靠 `seen` 挡：一个 sitemap 反复引用自己是常见写法（也常见于写坏的站），
    没有这道闸就是无限循环。

    **只收同站（含子域）的 sitemap** —— 跨站的不跟，跟了就是把别人的站点也拖进来。
    """
    seeds = sitemap_seeds(parsed_robots, base_url)
    base_host = _host_of(base_url)
    # robots.txt 也走 kind="sitemap" 的账，所以这里要减去基数 ——
    # 否则"展开 sitemap 几份"会把 robots 那份也算进去，账就不实了。
    base_n = crawler.sitemaps
    out = {"urls": [], "sitemaps_ok": 0, "sitemaps_fail": 0, "sitemaps_fetched": 0,
           "seed_source": "", "seeds": [u for u, _ in seeds], "notes": [], "cross_host": []}
    if seeds:
        out["seed_source"] = seeds[0][1]
    seen_sm, seen_url = set(), set()
    queue = [u for u, _ in seeds]
    while queue and not crawler.aborted:
        if max_sitemaps and (crawler.sitemaps - base_n) >= max_sitemaps:
            out["notes"].append(f"sitemap 展开到上限 {max_sitemaps} 份就停了")
            break
        if max_urls and len(out["urls"]) >= max_urls:
            out["notes"].append(f"sitemap 展开到 URL 上限 {max_urls} 条就停了")
            break
        if len(out["urls"]) >= _URLS_HARD:
            out["notes"].append(f"sitemap 展开撞到硬顶 {_URLS_HARD} 条就停了（内存保护）")
            break
        u = queue.pop(0)
        if u in seen_sm:
            continue
        seen_sm.add(u)
        if base_host and not _same_site(_host_of(u), base_host):
            out["cross_host"].append(u)
            continue
        p = crawler.get(u, kind="sitemap", max_bytes=_SITEMAP_MAX_BYTES)
        if p is None:
            if crawler.aborted:
                break
            continue
        st = int(p.get("status") or 0)
        if st != 200 or p.get("error_kind"):
            out["sitemaps_fail"] += 1
            out["notes"].append(f"{u} → HTTP {st or p.get('error_kind')}（跳过）")
            continue
        out["sitemaps_ok"] += 1
        if int(p.get("body_len") or 0) >= _SITEMAP_MAX_BYTES:
            out["notes"].append(f"{u} 正文达到 {_SITEMAP_MAX_BYTES // 1048576}MB 上限，"
                                f"可能被截断 —— 结构地图可能少一批 URL")
        sm = parse_sitemap(p.get("body") or "")
        if sm.get("truncated"):
            out["notes"].append(f"{u} 单文件 loc 数超过上限，已截断")
        if sm["kind"] == "index":
            for c in sm["locs"]:
                if c not in seen_sm:
                    queue.append(c)
        else:
            for c in sm["locs"]:
                if c in seen_url or len(out["urls"]) >= (max_urls or _URLS_HARD):
                    continue
                seen_url.add(c)
                out["urls"].append(c)
    out["sitemaps_fetched"] = crawler.sitemaps - base_n
    out["found"] = bool(out["sitemaps_ok"])
    if out["cross_host"]:
        out["notes"].append(f"跳过了 {len(out['cross_host'])} 个跨站 sitemap（不跟出去）")
    return out


def pagination_probe(crawler: _SiteCrawler, list_url: str, *, first_body="",
                     depth=3, log=None) -> dict:
    """L3：一个列表页**翻不翻得动**。

    判据见 `same_page_content` —— 要害是「HTTP 200 + 内容合理 + 其实还是第一页」，
    这是本项目踩过两次的坑，所以这里**不靠状态码，只比内容**。
    first_body 已经拿到就直接用，绝不为了探翻页把第一页再打一次。
    """
    out = {"tmpl": url_template(list_url), "url": list_url, "mode": "",
           "key": "", "tested": 0, "tried": [], "effective": 0,
           "verdict": "none", "why": ""}
    cands = pagination_candidates(list_url, depth=depth)
    out["candidates_n"] = len(cands)
    if not cands:
        out["why"] = "这个 URL 既没有页码参数、也没有数字段，构造不出翻页候选"
        return out
    sig1 = page_signature(first_body or "", list_url)
    if not sig1.get("links_n"):
        out["verdict"] = "unknown"
        out["why"] = ("第一页没有同 host 链接可比（可能是纯 JS 渲染的列表）—— 判不了，"
                      "不硬猜")
        return out
    same_n = 0
    weak_n = 0
    for c in cands[:_PAGE_CAND_MAX]:
        if not crawler.can(kind="page"):
            break
        p = crawler.get(c, kind="page")
        if p is None:
            break
        out["tested"] += 1
        out["tried"].append(c)
        st = int(p.get("status") or 0)
        if st != 200 or p.get("error_kind"):
            continue
        cmp_ = same_page_content(sig1, page_signature(p.get("body") or "", c))
        # **只有「多了新链接」才算数。**「集合变了但没多」是弱信号，首页本来就会抖，
        # 拿它当翻页证据就是在说一句证不了的话 —— 分开计数，别混进 effective。
        if cmp_.get("verdict") == "different":
            out["effective"] += 1
            out["new_links_n"] = max(out.get("new_links_n", 0), cmp_.get("new_links_n", 0))
            out["verdict"] = "ok"
            q = []
            try:
                q = [k for k, _ in parse_qsl(urlsplit(c).query) if k.lower() in _PAGE_KEYS]
            except Exception:
                pass
            out["key"] = (q[0] if q else "")
            out["mode"] = "query" if q else "path"
            if out["effective"] >= 2:
                break
        elif cmp_.get("verdict") == "same":
            same_n += 1
        elif cmp_.get("verdict") == "weak":
            weak_n += 1
            out["weak_why"] = cmp_.get("why") or ""
    if out["verdict"] != "ok":
        if same_n:
            out["verdict"] = "ignored"
            out["mode"] = out["mode"] or "query"
            out["why"] = (f"试了 {out['tested']} 个翻页写法，凡是返回 200 的，"
                          f"内容与第 1 页**一模一样** —— 页码参数被忽略。")
        elif weak_n:
            # 内容确实变了，但**没有一条新链接** —— 证不了是翻页造成的。
            out["verdict"] = "weak"
            out["mode"] = out["mode"] or "query"
            out["why"] = (f"试了 {out['tested']} 个翻页写法，内容与第 1 页不完全相同，"
                          f"但**没有多出任何一条新链接** —— 首页/栏目页本来就会抖"
                          f"（推荐位、轮播、时间戳），这证明不了是翻页生效。"
                          + (f"　（{out.get('weak_why')}）" if out.get("weak_why") else ""))
        elif not out["tested"]:
            out["why"] = "一个候选都没试成（预算用尽或已中止）"
        else:
            out["why"] = (f"试了 {out['tested']} 个候选，没有一个返回可用内容"
                          f"（可能翻页走的是 AJAX/前端路由，本探针探不到）")
    return out


# 静态 DRM 判别的抽样上限与体积上限。
# 每条清单 1~2 次 GET（master 要下钻一层），所以**必须**有个上限，且抽了几条要写进报告。
DIAG_DRM_MAX_TRACKS = 3
DIAG_DRM_MAX_BYTES = 512 * 1024      # 清单是纯文本，512KB 是宽裕的上限


def _is_manifest_track(t) -> bool:
    """这条候选是不是「要先取一份清单才知道怎么回事」的形态（m3u8 / mpd）。"""
    k = str(getattr(t, "kind", "") or "").strip().lower()
    if k in ("m3u8", "dash", "mpd"):
        return True
    u = str(getattr(t, "url", "") or "").strip().lower().split("?")[0]
    return u.endswith(".m3u8") or u.endswith(".mpd")


def _get_manifest_text(url: str, *, fetch=None, log=None):
    """取**整份**清单文本。返回 (text, base_url, head_only)。

    **不是取前 2KB。** `media_resolve.probe_track` 那条路只 Range 取首 2KB 做魔数嗅探，
    判不了 DRM：`#EXT-X-KEY` 完全可以出现在头部之后。只嗅头再报「不加密」，
    正是整站 DRM 长期没被看见的那一类错。

    head_only=True 表示只拿到样本（正文解码失败时的兜底）——
    此时**命中 DRM 算数，没命中不算数**，调用方要分开计。
    """
    try:
        r = fetch(url) if fetch is not None else probe_http(
            url, max_bytes=DIAG_DRM_MAX_BYTES, want_body=True)
    except Exception as e:
        _log(log, f"清单取样失败 {url}：{e!r}")
        return None, url, False
    if not isinstance(r, dict):
        return None, url, False
    base = str(r.get("final_url") or url)
    body = r.get("body") or ""
    if body.strip():
        return body, base, False
    sample = r.get("sample") or ""
    if sample.strip():
        return sample, base, True
    return None, base, False


def _probe_mpd_drm(text: str) -> dict:
    """MPD 侧的静态判别（复用 streamdecrypt.probe_mpd 的 pssh systemId 表）。"""
    try:
        import streamdecrypt as sd
        r = sd.probe_mpd(text)
    except Exception:
        return {"drm_systems": [], "keys": 0, "unknown_keyformats": []}
    return {"drm_systems": list(r.get("drm_systems") or []),
            "keys": 1 if r.get("cenc") else 0,
            "unknown_keyformats": []}


def _probe_one_manifest(track, *, fetch=None, log=None, charge=None):
    """取一条轨的清单并判 DRM。master → **下钻一层**拿 variant（KEY 行只在 media 清单里）。
    取不到 / 扣不到账 → None。返回 dict 额外带 head_only 标记。

    扣账是**逐次先扣后发**：下钻那一层只在真要下钻时才扣第二笔，
    不会为了一份 media 清单白扣两笔。"""
    url = str(getattr(track, "url", "") or "")
    if not url:
        return None
    kind = str(getattr(track, "kind", "") or "").strip().lower()

    def _take(u):
        if charge is not None and not charge(1, what="DRM 清单判别"):
            return None, u, False
        return _get_manifest_text(u, fetch=fetch, log=log)

    txt, base, head_only = _take(url)
    if txt is None:
        return None
    if kind in ("dash", "mpd") or base.lower().split("?")[0].endswith(".mpd"):
        d = _probe_mpd_drm(txt)
        d["head_only"] = head_only
        return d
    try:
        import streamdecrypt as sd
        pl = sd.parse_hls_media(txt, base)
        if pl.get("kind") == "master":
            vs = pl.get("variants") or []
            # 取带宽最高的一条：DRM 在同一 master 下各 variant 一致，挑最大只是确定性选择
            vs = sorted(vs, key=lambda v: int(v.get("bandwidth") or 0), reverse=True)
            if vs:
                t2, b2, ho2 = _take(str(vs[0].get("uri") or ""))
                if t2 is not None:
                    d = sd.probe_hls_drm(t2)
                    d["head_only"] = head_only and ho2
                    return d
        d = sd.probe_hls_drm(txt)
    except Exception as e:
        _log(log, f"清单判别失败 {url}：{e!r}")
        return None
    d["head_only"] = head_only
    return d


def probe_track_drm(tracks, *, fetch=None, log=None, stop_flag=None, charge=None,
                    max_tracks: int = DIAG_DRM_MAX_TRACKS) -> dict:
    """对候选清单做**静态 DRM 判别**：不开浏览器也能判「拿得到、但用不了」。

    存在的理由（2026-09-11 猫耳FM 那次）：`media_resolve.resolve_page` 只解析出**地址**，
    不发清单请求，所以 `track.drm` 除配方显式声明的以外一律是空的。浏览器一没开，
    `findings_from_eme` 就无从下手 —— 报告于是滑到「可以试」，而那是个整站 FairPlay。
    本函数补的正是这一段：**清单里的 KEYFORMAT 是站方自己写下的证据，不是我们的猜测。**

    诚实边界（都写进返回值，调用方必须照抄进报告）：
      - 只抽 `max_tracks` 条 → `probed` / `candidates` 一并给出，**没抽到的不算「无 DRM」**；
      - `head_only` 命中的只算 `probed_head_only`，**不混进「已判」的计数**；
      - 认不得的 KEYFORMAT 原样进 `unknown`，摊开而不是吞掉。

    代价：每条清单 1~2 次 GET。本函数只管发；**记账由调用方先扣**（整站走 crawler.charge()）。
    """
    out = {"probed": 0, "probed_head_only": 0, "candidates": 0,
           "commercial": [], "unknown": [], "keys": 0, "detail": [], "note": ""}
    cands = [t for t in (tracks or []) if _is_manifest_track(t)]
    out["candidates"] = len(cands)
    if not cands:
        out["note"] = "无清单候选（m3u8/mpd），未做静态 DRM 判别"
        return out
    for t in cands[:max(0, int(max_tracks))]:
        if stop_flag is not None and stop_flag.is_set():
            out["note"] = "已停止：抽样未做满"
            break
        d = _probe_one_manifest(t, fetch=fetch, log=log, charge=charge)
        if d is None:
            # 扣不到账（预算尽 / 已中止）就停：接着试只是拿「没试成」冒充「没问题」。
            if charge is not None:
                out["note"] = "预算不足或已中止：抽样提前收手，未判的清单不代表无 DRM"
                break
            continue
        if d.get("head_only"):
            out["probed_head_only"] += 1
        else:
            out["probed"] += 1
        out["keys"] += int(d.get("keys") or 0)
        for s in d.get("drm_systems") or []:
            if s not in out["commercial"]:
                out["commercial"].append(s)
        for k in d.get("unknown_keyformats") or []:
            if k not in out["unknown"]:
                out["unknown"].append(k)
        out["detail"].append({
            "url": str(getattr(t, "url", "") or ""),
            "systems": list(d.get("drm_systems") or []),
            "keys": int(d.get("keys") or 0),
            "head_only": bool(d.get("head_only"))})
    if len(cands) > int(max_tracks):
        out["note"] = "抽样 %d/%d 条清单（其余未判，不代表无 DRM）" % (
            min(len(cands), int(max_tracks)), len(cands))
    return out


# 模板里的系统列：由 ctx 回填（job_url 取 URL、source 取站点），**不是业务内容**。
# 数命中率时必须排除 —— 否则任何页面都恒定「命中 2 列」，看着像抽到了东西。
_TEXT_SYS_COLS = frozenset({"job_url", "source"})
TEXT_PROBE_TOP_N = 3


def _text_template_hits(spec, text, titles, url) -> dict:
    """拿一套模板对本页跑一次**干抽取**，数非系统列命中几个。"""
    import schema_engine as se
    cols = [c for c in (spec.get("columns") or []) if isinstance(c, dict)]
    got = se.schema_extract(text, cols, ctx={"url": url, "titles": titles}) or {}
    biz = []
    for c in cols:
        n = (c.get("name") or "").strip()
        if n and n not in _TEXT_SYS_COLS and n not in biz:
            biz.append(n)
    filled = [n for n in biz if str(got.get(n) or "").strip()]
    return {"hits": len(filled), "total": len(biz), "cols": biz, "filled": filled,
            "values": {n: got.get(n) for n in filled}}


def probe_dedup_capability() -> dict:
    """落库去重能力：库里的表**有没有 UNIQUE 约束**。读真库的建表 SQL，不靠代码推断。

    为什么值得测：`DBPlugin.insert` 是纯 INSERT（没有 UPSERT），去重只靠**会话内存**的
    `seen_urls` —— 于是「换个会话重抓同一个站」会**再插一遍**，而且全程不报错。
    这件事在抓之前知道，比抓完几千条之后发现强得多。

    查不到（还没建过库 / 读不动）→ `checked=False`，报告写「未确认」，
    **绝不在没看过的前提写「没有」**。
    """
    out = {"checked": False, "tables": [], "with_unique": [], "note": ""}
    try:
        import config as _cfg
        base = str(getattr(_cfg, "base_path", "") or "")
        if not base or not os.path.isdir(base):
            out["note"] = "拿不到库目录，未确认"
            return out
        dbs = [f for f in os.listdir(base)
               if f.startswith("inner_sqlite_") and f.endswith(".db")]
        if not dbs:
            out["note"] = "还没建过库，未确认"
            return out
        import sqlite3
        for fn in sorted(dbs):
            p = os.path.join(base, fn).replace("\\", "/")
            try:
                con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            except Exception:
                continue
            try:
                rows = con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
            finally:
                con.close()
            for name, ddl in rows:
                out["tables"].append(name)
                if "UNIQUE" in (ddl or "").upper():
                    out["with_unique"].append(name)
        out["checked"] = True
    except Exception as e:
        out["note"] = f"读取库结构失败：{e!r}"
    return out


def probe_text_extraction(html, *, url="", active_key=None, templates=None,
                          top_n: int = TEXT_PROBE_TOP_N) -> dict:
    """文字轴诊断：拿数据表模板对本页做**干抽取**，报逐列命中率。

    与音视频轴并列 —— 工作台是两条腿走路，诊断不能只认一条。之前这条腿在报告里**零覆盖**。

    代价是 **0**：`schema_engine.schema_extract` 是纯函数（不读盘、不发请求），模板装载是
    本地读一次。所以这一步没有理由不做，而它正好回答动手前最想问的两个问题：
      「这站该用哪套模板抓」　「我绑的那套模板对这站行不行」。

    诚实边界：命中列数**只说明「这个页面的正文里有这些字段」**，不代表能整站抓 ——
    列表页翻不翻得动、要不要登录、限不限频，都不在这一节的射程内。
    """
    out = {"ok": False, "titles_n": 0, "templates_n": 0,
           "active": None, "ranking": [], "note": ""}
    if not html:
        out["note"] = "没取到页面正文，无法干抽取"
        return out
    try:
        import qiongqi_maw as qm
        text = qm._structured_page_text(html)
        titles = qm._page_titles(html)
    except Exception as e:
        out["note"] = f"正文结构化失败：{e!r}"
        return out
    out["ok"] = bool(text.strip())
    out["titles_n"] = len(titles or [])
    if not out["ok"]:
        out["note"] = "正文为空（可能是壳页/纯前端渲染），没有可抽的内容"
        return out

    # ── 当前绑定的那套（给逐列明细）──
    try:
        import config as _cfg
        key = active_key or getattr(_cfg, "ACTIVE_SCHEMA", None)
        spec = _cfg.load_table_template(key) if key else None
        if spec:
            h = _text_template_hits(spec, text, titles, url)
            h["name"] = spec.get("name") or spec.get("id") or str(key)
            out["active"] = h
        elif key:
            out["note"] = f"当前绑定的模板「{key}」没找到（可能被停用）"
    except Exception as e:
        out["note"] = f"读取当前绑定模板失败：{e!r}"

    # ── 全部模板各跑一遍，排序取前 N（纯解析，零请求）──
    ranks = []
    try:
        import config as _cfg
        specs = templates if templates is not None else _cfg.list_table_templates()
        for spec in (specs or []):
            if not isinstance(spec, dict):
                continue
            h = _text_template_hits(spec, text, titles, url)
            if not h["total"]:
                continue
            h["name"] = spec.get("name") or spec.get("id") or "?"
            h["rate"] = round(h["hits"] / float(h["total"]), 3)
            ranks.append(h)
        out["templates_n"] = len(ranks)
    except Exception as e:
        out["note"] = (out["note"] + "；" if out["note"] else "") + f"模板扫描失败：{e!r}"
    # 命中数优先、同分按命中率排 —— 命中多的那套才更可能是正确的模板
    ranks.sort(key=lambda h: (-h["hits"], -h["rate"], str(h.get("name"))))
    out["ranking"] = ranks[:max(1, int(top_n))]

    # 落库去重能力：与站点无关，但**抓之前该知道**（抓完几千条才发现重复就晚了）。
    # 查不到就不写这个键 —— 报告于是写「未确认」，而不是替库打包票。
    try:
        dd = probe_dedup_capability()
        if dd.get("checked"):
            out["dedup"] = dd
            out["nodup"] = not dd.get("with_unique")
    except Exception:
        pass
    return out


def _route_of(u: str, log=None, charge=None, fetch=None, stop_flag=None) -> dict:
    """调既有 media_resolve 拿**解析路线**（route_verdict 形状）+ 静态 DRM 判别。

    它自己发请求、且没有 fetch 注入点，所以调用方必须**先** `crawler.charge()`。
    整站模式下每**个模板**只调一次（不是每页）—— 同一模板的页面路线几乎一样，
    多调一次就是多一串不受节拍约束的请求。

    之后**再取一次清单**判 DRM（`probe_track_drm`）：resolve_page 只给地址、不给加密态，
    不取清单就判不出 DRM，整站模式在浏览器没开时就会把 DRM 站说成「可以试」。
    这两笔都走 `charge`（先扣后发），所以整站模式**没有任何一次请求绕过账房**。
    """
    try:
        import media_resolve as mr
        res = mr.resolve_page(u, use_browser=False)
        tracks = getattr(res, "tracks", None) or []
        return {"route": str(getattr(res, "route", "") or ""), "tracks_n": len(tracks),
                "notes": [f"（路线取自该模板的第一个代表页：{u}）"]
                         + list(getattr(res, "notes", None) or ()),
                "drm_probe": probe_track_drm(tracks, fetch=fetch, log=log,
                                             stop_flag=stop_flag, charge=charge)}
    except Exception as e:
        _log(log, f"路由解析不可用：{e!r}")
        return {}


def run_site_diagnosis(url: str, *, profile: str = "standard", profiles=None,
                       wait_sec=None, use_browser: bool = False, log=None,
                       stop_flag=None, on_row=None, on_progress=None,
                       hard_cfg=None, robots_parsed=None, knowledge_index=None,
                       history_path=None, record_history: bool = True,
                       fetch=None, route_fn=None, delay_sec=None, budget_pages=None,
                       max_sitemaps=None, max_urls=None) -> DiagReport:
    """整站诊断（P3）。公开入口**绝不 raise**，失败都收进 notes。

    与单页模式的**唯一区别是编排**：L1 导览 → L2 归纳 → 选代表页 → 逐页跑
    **完整的单页诊断**（复用 `run_diagnosis`，不另起一套）+ 列表页翻页探针。

    注入点（离线用例靠它们完全不碰网络）：
      fetch      —— 取页函数，签名同 probe_http(url, **kw) → probe dict。
                    默认 probe_http。**所有请求都必须走它**，所以它也是"没偷偷多发"的证明。
      route_fn   —— 媒体路由解析函数，签名 fn(url, log=None) → route_verdict dict。
                    默认 `_route_of`（真调 media_resolve，它会自己发网络请求）。
                    离线用例必须注入它，否则注入的假域名会被真解析器拿去发请求。
                    它仍然**照常被 charge() 扣账** —— 预算与节拍的纪律不因注入而放宽。
      delay_sec  —— 页间节拍秒数，默认读 config.DIAG_SITE_DELAY（2.0）。用例传 0。
      budget_pages / max_sitemaps / max_urls —— 覆盖档位里的上限（用例用小值）。
    """
    t0 = time.time()
    rep = DiagReport(url=url or "", scope="site")
    pf = site_profile(profile, profiles=profiles)
    rep.profile = pf.get("key", "standard")
    rep.profile_name = pf.get("name", "")

    def _bail(msg):
        rep.notes.append(msg)
        rep.feasibility = {"verdict": "输入有误", "summary": msg}
        render_report(rep)
        rep.elapsed_ms = int((time.time() - t0) * 1000)
        return rep

    if not url or _host_of(url) == "" or not url.lower().startswith(("http://", "https://")):
        return _bail("只支持 http/https 的单条 URL")

    if wait_sec is None:
        wait_sec = int(_cfg_get("DIAG_WAIT_DEFAULT", 15) or 15)
    delay = (float(_cfg_get("DIAG_SITE_DELAY", 2.0) or 0.0)
             if delay_sec is None else max(0.0, float(delay_sec)))
    mp = int(budget_pages if budget_pages is not None else (pf.get("max_pages") or 0))
    ms = int(max_sitemaps if max_sitemaps is not None else (pf.get("max_sitemaps") or 0))
    mu = int(max_urls if max_urls is not None else (pf.get("max_urls") or 0))

    crawler = _SiteCrawler(max_pages=mp, delay=delay, fetch=fetch, log=log,
                           stop_flag=stop_flag)

    def _prog(msg):
        _log(log, msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    _prog(f"整站诊断开始（档位：{pf.get('name')}）　上限 {mp or '不限'} 页，页间 ≥{delay} 秒")

    # ── 1. robots（并入导览阶段的账）──
    try:
        parsed = fetch_robots(
            url, log=log, parsed=robots_parsed,
            probe_fn=(lambda u, **kw: crawler.get(u, kind="sitemap", **kw)))
        rep.rules = robots_page_rules(parsed, url)
    except Exception as e:
        rep.notes.append(f"robots 阶段异常：{e!r}")
        parsed, rep.rules = {}, {}

    # ── 2. 首页（1 次；后面若首页也是代表页，账房会命中缓存不重复打）──
    home = crawler.get(url)
    home_html = (home or {}).get("body") or ""
    st_home = int((home or {}).get("status") or 0)
    if home_html:
        try:
            fp = fingerprint_html(home_html, url, (home or {}).get("final_url") or url)
            rep.title = fp.get("title", "")
            rep.fingerprint = fp
        except Exception as e:
            rep.notes.append(f"首页指纹解析异常：{e!r}")
    elif home is None:
        rep.notes.append("首页未取（预算用尽或已中止）")
    elif st_home and st_home != 200:
        rep.notes.append(f"首页返回 HTTP {st_home}")

    # ── 3. L1 导览 ──
    try:
        sm = fetch_sitemap_tree(crawler, url, parsed, max_sitemaps=ms, max_urls=mu, log=log)
    except Exception as e:
        rep.notes.append(f"sitemap 展开异常：{e!r}")
        sm = {"urls": [], "notes": [], "sitemaps_ok": 0, "sitemaps_fail": 0,
              "sitemaps_fetched": crawler.sitemaps, "seed_source": "", "found": False}
    home_links = extract_links(home_html, url) if home_html else []
    # 首页链接排在前面：sitemap 可能有几万条，但首页链接是"站方自己认为重要的入口"
    all_urls = list(home_links) + list(sm.get("urls") or ())
    if crawler.aborted:
        rep.notes.append(f"导览阶段已中止（{crawler.abort_reason}）—— 覆盖不完整")

    # ── 4. L2 结构归纳 ──
    per_t = int(pf.get("per_template") or 0)
    full = build_structure_map(all_urls, max_samples=max(3, per_t or 3),
                               sources={"sitemap": len(sm.get("urls") or ()),
                                        "home": len(home_links), "seed": 1})
    rep.structure = {"templates": full["templates"], "urls_n": full["urls_n"],
                     "sources": full["sources"], "notes": list(sm.get("notes") or ())}
    rep.discovery = {"seed_source": sm.get("seed_source", ""),
                     "seeds": sm.get("seeds") or [], "sitemaps_fetched": sm.get("sitemaps_fetched", 0),
                     "sitemaps_ok": sm.get("sitemaps_ok", 0), "sitemaps_fail": sm.get("sitemaps_fail", 0),
                     "sitemap_found": bool(sm.get("found")),
                     "from_sitemap": len(sm.get("urls") or ()), "from_home": len(home_links),
                     "urls_n": full["urls_n"], "templates_n": len(full["templates"])}
    _prog(f"导览完成：URL 全集 {full['urls_n']} 条 / {len(full['templates'])} 个模板"
          f"（sitemap {'命中' if sm.get('found') else '无'}）")

    # ── 5. 选代表页 ──
    sel = select_representatives(full, per_template=per_t,
                                 max_templates=int(pf.get("max_templates") or 0),
                                 max_pages=mp)
    _prog(f"代表页 {len(sel['picks'])} 个（模板 {sel['templates_n']} 个，"
          f"轮转取材，预算中途耗尽也保证每模板至少一个）")

    # ── 6. L3 穿透 + L4 体检 ──
    idx = knowledge_index
    if idx is None:
        try:
            import site_knowledge as sk
            idx = sk.build_index()
        except Exception as e:
            _log(log, f"知识库索引建不起来：{e!r}")
            idx = None
    browser_left = int(pf.get("browser_pages") or 0) if use_browser else 0
    used_browser = 0
    per_page, pages, probes = [], [], []
    probed_tmpl, seen_pick, routed_tmpl = set(), set(), {}
    routes, tracks_total = {}, 0
    for pick in sel["picks"]:
        if crawler.aborted or (stop_flag is not None and stop_flag.is_set()):
            break
        u, tmpl = pick["url"], pick["tmpl"]
        if u in seen_pick:
            continue
        seen_pick.add(u)
        p = crawler.get(u, kind="page")
        if p is None:
            break
        # 路由解析必须**先经账房扣账**再发 —— 否则 resolve_page 内部的请求
        # 会绕过预算、绕过节拍、也绕过 429 中止（那是整站模式最要紧的三条纪律）。
        # 每个模板只做一次：同模板的路由几乎一样，多做的部分是纯浪费。
        if tmpl in routed_tmpl:
            rv = routed_tmpl[tmpl]
        else:
            # 扣不到账（预算已尽 / 已中止）就**不解析**，也不缓存 —— 缓存了会让
            # 同模板后续页面拿到一个"没试过"的空路线，看起来像"这个模板没有路线"。
            rv = {}
            if crawler.charge(1, what=f"路由解析 {tmpl}"):
                # 注入的桩签名是固定的 fn(url, log=None)，**不能**跟着 _route_of 一起加关键字
                # ——桩自己负责把 drm_probe 放进返回的 dict（用例想造 DRM 就在那儿造）。
                # 清单取样的账在 `_route_of` 里逐笔扣（见 probe_track_drm 的 charge 参数），
                # 所以「路由解析」这 1 笔之外的开销不会被漏记。
                if route_fn is not None:
                    rv = route_fn(u, log)
                else:
                    rv = _route_of(u, log, charge=crawler.charge,
                                   fetch=fetch, stop_flag=stop_flag)
                routed_tmpl[tmpl] = rv
        try:
            sub = run_diagnosis(u, probe=p, robots_parsed=parsed, knowledge_index=idx,
                                route_verdict=rv,
                                use_browser=(browser_left > 0), wait_sec=wait_sec,
                                record_history=False, read_history=False,
                                log=None, on_row=on_row)
        except Exception as e:
            rep.notes.append(f"代表页 {u} 诊断异常：{e!r}")
            continue
        if browser_left > 0:
            browser_left -= 1
            used_browser += 1
        pages.append({"url": u, "tmpl": tmpl, "kind": pick.get("kind", ""),
                      "status": int((p or {}).get("status") or 0),
                      "title": sub.title, "level": (sub.risk or {}).get("level", "?"),
                      "verdict": (sub.feasibility or {}).get("verdict", ""),
                      "n_findings": len(sub.findings or ())})
        per_page.append((u, sub.findings or ()))
        rv = sub.route_verdict or {}
        r_ = str(rv.get("route") or "")
        if r_ and r_ != "none":
            routes[r_] = routes.get(r_, 0) + 1
        tracks_total += int(rv.get("tracks_n") or 0)
        if sub.media.get("key_systems"):
            rep.media.setdefault("key_systems", [])
            for k in sub.media["key_systems"]:
                if k not in rep.media["key_systems"]:
                    rep.media["key_systems"].append(k)
        # 静态 DRM 判别结果从代表页并上来（每个代表页都判过自己的清单抽样）。
        # 取**并集**：任一页判出商业 DRM，整站就是 DRM —— 这比「平均」或「取第一条」诚实。
        sdp = (sub.route_verdict or {}).get("drm_probe") or {}
        if sdp:
            agg = rep.media.setdefault("drm_probe", {
                "probed": 0, "probed_head_only": 0, "candidates": 0,
                "commercial": [], "unknown": [], "keys": 0, "detail": [], "note": ""})
            for k in ("probed", "probed_head_only", "candidates", "keys"):
                agg[k] = int(agg.get(k) or 0) + int(sdp.get(k) or 0)
            for k in ("commercial", "unknown"):
                for x in (sdp.get(k) or []):
                    if x not in agg[k]:
                        agg[k].append(x)
            agg["detail"].extend(sdp.get("detail") or [])
        # 文字轴：取**第一个抽得出东西**的代表页 —— 它最能代表这个站的正文形态。
        # 全试一遍也可以（纯解析不花钱），但报告里摆五六份逐列明细只会淹掉重点。
        if not (rep.text_probe or {}).get("ok") and (sub.text_probe or {}).get("ok"):
            rep.text_probe = dict(sub.text_probe)
            rep.text_probe["from_page"] = u
        # 列表页/栏目页：顺着试一次翻页（第一页正文刚拿到，不重复请求）
        if pick.get("kind") in ("list", "channel") and tmpl not in probed_tmpl:
            probed_tmpl.add(tmpl)
            probes.append(pagination_probe(crawler, u, first_body=p.get("body") or "",
                                           depth=int(pf.get("page_depth") or 0), log=log))
        _prog(f"[{len(pages)}/{len(sel['picks'])}] {pick.get('kind')} "
              f"{sub.feasibility.get('verdict')}　{u[:70]}")

    rep.pages = pages
    pen_notes = []
    if probes:
        pen_notes.append(f"翻页探针只对**列表页/栏目页**模板做（{len(probed_tmpl)} 个）；"
                         f"详情页没有「翻页」这回事。")
    if len(sel["picks"]) > len(pages):
        pen_notes.append(f"排了 {len(sel['picks'])} 个代表页，实际只跑成 {len(pages)} 个"
                         f"（预算用尽 / 已中止 / 重复 URL）。")
    rep.penetration = {
        "probes": probes,
        "notes": pen_notes,
        "templates_probed": sorted(probed_tmpl),
        "route_note": (f"媒体路由解析按**模板**做（每模板至多一次，共 {len(routed_tmpl)} 个模板），"
                       f"不是按页做 —— 同模板的页面路线几乎一样，多做的部分是纯浪费。"
                       if routed_tmpl else
                       "本次没有做媒体路由解析（第一个代表页就没跑成）。"),
        "browser_note": (f"浏览器观测只用于前 {used_browser} 个代表页"
                        f"（档位上限 {pf.get('browser_pages')}）；其余页只做静态级诊断。"
                        if use_browser else
                        "本次未开浏览器观测 —— 代表页只做了静态级诊断"
                        "（运行期请求、EME/DRM 这几项拿不到）。"),
    }
    if not probes and not pages:
        rep.penetration["notes"].append(
            "一个代表页都没跑成 —— 上面的结构地图仍然有效，但没有任何深诊结论。")

    # ── 7. 合成结论 ──
    hard_site = match_hard_site(url, hard_cfg=hard_cfg)
    kfacts = _knowledge_stage(url, index=idx, title=rep.title, body=home_html,
                              status=st_home, hard_site=hard_site, log=log)
    kfacts["host"] = _host_of(url)
    try:
        import site_knowledge as sk
        ks = sk.index_summary(idx) if idx else {}
        if any((ks or {}).values()):
            kfacts["index_summary"] = ks
    except Exception:
        pass
    rep.knowledge = {k: v for k, v in kfacts.items() if v and k not in ("host",)}

    # 路线：代表页各自给出的解析路线合起来看。**不取"最乐观的那条"** ——
    # 把每条路线出现几次原样列出来，让人自己看哪条是主路。
    if routes:
        best = max(routes.items(), key=lambda x: (x[1], x[0]))[0]
        rep.route_verdict = {
            "route": best, "tracks_n": tracks_total, "routes": sorted(routes),
            "notes": ["代表页出现过的解析路线："
                      + "，".join(f"{k}×{v}" for k, v in sorted(routes.items()))]}

    bud = {"max_pages": mp, "pages_done": crawler.pages, "delay": delay,
           "external_calls": crawler.external, "external_what": list(crawler.external_what),
           "sitemaps_fetched": crawler.sitemaps, "sitemaps_ok": crawler.sitemaps_ok,
           "truncated": bool(sel.get("truncated") or len(pages) < len(sel["picks"])),
           "templates_untouched": list(sel.get("templates_untouched") or ()),
           "aborted": crawler.aborted, "abort_reason": crawler.abort_reason,
           "aborted_at": crawler.abort_url or (pages[-1]["url"] if pages else url)}
    rep.budget = bud
    rep.probe = {"requests_n": crawler.pages, "sitemaps_n": crawler.sitemaps}

    rep.findings = (merge_findings(per_page)
                    + findings_from_knowledge(kfacts, status=st_home)
                    + findings_from_site(discovery=rep.discovery, structure=rep.structure,
                                         penetration=rep.penetration, budget=bud,
                                         aborted=crawler.aborted))
    # 首页本身也算一页观测 —— 它的挑战页/登录墙不该因为"没被选中做代表页"而丢掉
    home_probe_findings = findings_from_probe(home or {}) + findings_from_rules(rep.rules, url)
    have = {getattr(f, "code", "") for f in rep.findings}
    for f in home_probe_findings:
        if getattr(f, "code", "") not in have:
            rep.findings.append(f)
            have.add(getattr(f, "code", ""))

    reachable = bool(st_home) or not ((home or {}).get("error_kind") or "")
    rep.risk = grade_difficulty(findings=rep.findings, hard_site=hard_site,
                                reachable=reachable)
    rep.media.setdefault("key_systems", [])
    rep.media["probed"] = bool(used_browser)

    # ── 8. 历史（旁证；只读进来，**不参与上面任何一步判断**）──
    # 整站模式只落**一条**记录（代表页跑 run_diagnosis 时 record_history=False）——
    # 否则一次整站诊断会往历史里灌几十条，把「这个站我诊断过几次」彻底搅浑。
    try:
        import site_knowledge as sk
        rep.history = sk.history_for(url, path=history_path)
    except Exception as e:
        rep.history = {"host": _host_of(url), "total": 0, "entries": [], "error": repr(e)}

    rep.coverage = build_coverage(rep, used_browser=bool(used_browser))
    rep.feasibility = build_feasibility(rep)
    rep.suggestions = build_suggestions(rep)
    rep.elapsed_ms = int((time.time() - t0) * 1000)

    if record_history:
        try:
            import site_knowledge as sk
            ok, err = sk.record(rep, path=history_path)
            if not ok and err:
                rep.notes.append(f"历史未落盘：{err}")
        except Exception as e:
            rep.notes.append(f"历史未落盘：{e!r}")

    render_report(rep)
    _prog(f"整站诊断完成：{rep.feasibility.get('verdict')}　难度 {rep.risk.get('level')}　"
          f"请求 {crawler.pages} 页 + sitemap {crawler.sitemaps} 份　"
          f"耗时 {rep.elapsed_ms / 1000.0:.1f}s")
    return rep


def _last_parsed(rep: DiagReport) -> dict:
    """把 rep.rules 还原成 parse_robots 形状（供 current_page_rule 复用）。"""
    r = rep.rules or {}
    if not r or r.get("absent"):
        return {}
    return {"ok": r.get("declared"), "groups": r.get("groups") or [],
            "sitemaps": r.get("sitemaps") or [], "raw": r.get("raw", ""),
            "errors": r.get("errors") or []}


def report_to_dict(rep: DiagReport) -> dict:
    """报告 -> 可 JSON 化的 dict（历史落盘 / 导出用）。"""
    return asdict(rep)


def report_from_dict(d: dict) -> DiagReport:
    """dict -> DiagReport（读历史 / 读导出）。坏数据 → 空报告，绝不 raise。"""
    rep = DiagReport()
    if not isinstance(d, dict):
        return rep
    for k, v in d.items():
        if not hasattr(rep, k):
            continue
        try:
            setattr(rep, k, v)
        except Exception:
            pass
    rep.findings = [f if isinstance(f, DiagFinding) else DiagFinding(**{
        k: v for k, v in (f or {}).items() if k in DiagFinding.__dataclass_fields__
    }) for f in (rep.findings or []) if isinstance(f, (dict, DiagFinding))]
    rep.rows = [r if isinstance(r, DiagRow) else DiagRow(**{
        k: v for k, v in (r or {}).items() if k in DiagRow.__dataclass_fields__
    }) for r in (rep.rows or []) if isinstance(r, (dict, DiagRow))]
    return rep
