# stealth_profile.py — 指纹档位矩阵（P1 #4）纯模块。
# 设计核心：**所有形状由"一个完整 UA 字符串"派生**——同一档位(Profile)一次选定，
# 其页面态(Chrome Sec-CH-UA/Platform/Lang) 与媒体态(requests/curl Accept·Sec-Fetch·UA)
# 全部由它衍生 → 页/清单/分片/key/TLS 天然同源自洽，消灭"Chrome v130 写死 vs 随机 UA"
# 式多头不自洽破绽。
#
# 纯模块约束：不 import config / vg / task_identity / audio —— config 反过来 import 本模块
# 建 UA 池不产生环；本模块全部派生函数离线可单测（media_e2e case_stealth_* / video_e2e）。
#
# Accept-Encoding 纪律：requests 不显式设(保自动解压)；curl.exe 不加 --compressed；
# curl_cffi impersonate 态由引擎自管(头交给引擎)。因此离线断言不对 AE 做强断言。
from __future__ import annotations

import random
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ------------------------------------------------------------------ 常量与形状

# 各请求用途的 Accept / Sec-Fetch-* 形态（Chrome 真实语义分型）：
#   page     = 顶层/子页导航（html）
#   api      = fetch/XHR JSON 接口
#   media    = 播放器内取 清单/分片/key（hls.js 类 fetch → cors/empty）
#   direct/range = <video> 原生直链播放（Range 探针 / 直链下载 → no-cors/video）
PURPOSE_PAGE = "page"
PURPOSE_API = "api"
PURPOSE_MANIFEST = "manifest"
PURPOSE_SEGMENT = "segment"
PURPOSE_KEY = "key"
PURPOSE_DIRECT = "direct"
PURPOSE_RANGE = "range"
PURPOSES = (PURPOSE_PAGE, PURPOSE_API, PURPOSE_MANIFEST, PURPOSE_SEGMENT,
            PURPOSE_KEY, PURPOSE_DIRECT, PURPOSE_RANGE)

DEF_ACCEPT_LANG = "zh-CN,zh;q=0.9,en;q=0.8"
DEF_TZ = "Asia/Shanghai"
DEF_WINDOW = (1920, 1080)


@dataclass(frozen=True)
class PurposeShape:
    """一类请求用途的"非身份"形状（Accept / Sec-Fetch-*；身份部分不在此）。"""
    accept: str
    sec_fetch_mode: str
    sec_fetch_dest: str
    sec_fetch_site: str = "cross-site"   # 缺省；带 Referer 时按同源改写
    sec_fetch_user: str = ""             # 仅导航用 "?1"
    extra: tuple = ()                    # (k, v) 二元组序列，如 (("Upgrade-Insecure-Requests", "1"),)


_PAGE = PurposeShape(
    accept=("text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"),
    sec_fetch_mode="navigate", sec_fetch_dest="document",
    sec_fetch_site="cross-site", sec_fetch_user="?1",
    extra=(("Upgrade-Insecure-Requests", "1"),))
_API = PurposeShape(
    accept="application/json, text/javascript, */*; q=0.01",
    sec_fetch_mode="cors", sec_fetch_dest="empty")
_MEDIA = PurposeShape(accept="*/*", sec_fetch_mode="cors", sec_fetch_dest="empty")
_NATIVE = PurposeShape(accept="*/*", sec_fetch_mode="no-cors", sec_fetch_dest="video")

_PURPOSE_SHAPE = {
    PURPOSE_PAGE: _PAGE, PURPOSE_API: _API,
    PURPOSE_MANIFEST: _MEDIA, PURPOSE_SEGMENT: _MEDIA, PURPOSE_KEY: _MEDIA,
    PURPOSE_DIRECT: _NATIVE, PURPOSE_RANGE: _NATIVE,
}

# URL 扩展名兜底 → 用途（显式 purpose= 优先；这里只服务未标注的调用）
_EXT_PURPOSE = ((".m3u8", PURPOSE_MANIFEST), (".mpd", PURPOSE_MANIFEST),
                (".ts", PURPOSE_SEGMENT), (".m4s", PURPOSE_SEGMENT),
                (".key", PURPOSE_KEY),
                (".mp4", PURPOSE_DIRECT), (".flv", PURPOSE_DIRECT),
                (".webm", PURPOSE_DIRECT), (".mov", PURPOSE_DIRECT),
                (".m4v", PURPOSE_DIRECT), (".mp3", PURPOSE_DIRECT),
                (".m4a", PURPOSE_DIRECT), (".aac", PURPOSE_DIRECT),
                (".wav", PURPOSE_DIRECT), (".flac", PURPOSE_DIRECT),
                (".ogg", PURPOSE_DIRECT))


def purpose_for_url(url: str) -> str:
    """按扩展名兜底归类一个请求的用途；未知/无扩展 → page 语义（文档/HTML 导航）。"""
    path = urlparse(url or "").path.lower()
    for ext, purpose in _EXT_PURPOSE:
        if path.endswith(ext):
            return purpose
    return PURPOSE_PAGE


def shape_for_purpose(purpose) -> PurposeShape:
    """把显式用途/别名归一到 PurposeShape；未知名一律 media 语义（安全下限 Accept=*/*）。"""
    return _PURPOSE_SHAPE.get(purpose, _MEDIA)


# ------------------------------------------------------------------ 品牌串派生

# Chrome >= 103 广泛观测的完整 Sec-CH-UA 品牌串形式（GREASE 品牌为 "Not)A;Brand";v="99"）。
# 不做逐 minor GREASE 考古——交付档内一致且与真实 Chrome 同形即可满足自洽要求。
_GREASE = '"Not)A;Brand";v="99"'


def sec_ch_ua_for_major(family: str, major: int) -> str:
    """family ∈ chrome|edge → 完整 Sec-CH-UA 品牌串，与主版本精确一致。
    Safari/unknown 返回空（WebKit 不送客户端提示；unknown 不硬猜），调用方据此不发该头。"""
    m = int(major)
    if family == "edge":
        return ('%s, "Chromium";v="%d", "Microsoft Edge";v="%d"' % (_GREASE, m, m))
    if family == "chrome":
        return '%s, "Chromium";v="%d", "Google Chrome";v="%d"' % (_GREASE, m, m)
    return ""


def _family_of(ua: str) -> str:
    ua = ua or ""
    if "Edg/" in ua:
        return "edge"
    if "Chrome/" in ua:
        return "chrome"
    if "Safari/" in ua and "Version/" in ua:
        return "safari"
    return "chrome" if "Safari/" in ua else "unknown"


def _major_of(ua: str, family: str):
    import re
    if family in ("chrome", "edge"):
        m = re.search(r"(?:Chrome|Edg)/(\d+)\.", ua)
    elif family == "safari":
        m = re.search(r"Version/(\d+)", ua)
    else:
        m = None
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return 0
    return 0


# 支持本地化的 TLS impersonate 槽位（curl_cffi），由 UA 主版本向下就近映射。
_IMP_TABLE = (("131", "chrome131"), ("124", "chrome124"), ("120", "chrome120"),
              ("116", "chrome116"), ("110", "chrome110"), ("99", "chrome99"))
_DEFAULT_IMP = "chrome120"


def nearest_imp(ua: str) -> str:
    """由 UA 解析就近 impersonate 档：Edg→同主版本 chrome；Safari→safari17_0；
    Chrome→≤主版本最大已知档；未知引擎→chrome120 兜底。与 task_identity 语义同源。"""
    fam = _family_of(ua)
    if fam == "safari":
        return "safari17_0"
    if fam == "unknown":
        return _DEFAULT_IMP
    major = _major_of(ua, fam)
    for above, slot in _IMP_TABLE:
        if major >= int(above):
            return slot
    return "chrome99"


# ------------------------------------------------------------------ Profile 档位

@dataclass(frozen=True)
class Profile:
    """一次抓取选定的一整套指纹档位：身份锚点(ua/lang/tz/平台) + 客户端提示 + 用途形状。
    除 shapes 外均为最终可直接下发的值；shapes 留作未来按用途微调的缝（现为共享默认）。"""
    key: str                 # "chrome124" | "edge131"
    family: str              # "chrome"|"edge"|"safari"
    major: int
    ua: str                  # 完整 Win 串，含 (KHTML, like Gecko) + Safari/537.36 尾
    imp: str                 # curl_cffi 槽（edge→同主版本 chrome，文档注明非 JA3 等价）
    sec_ch_ua: str           # 完整 Sec-CH-UA 品牌串（与本档 major 精确一致）
    sec_ch_ua_mobile: str = "?0"
    sec_ch_ua_platform: str = '"Windows"'
    platform_js: str = "Win32"       # navigator.platform 语义
    platform_cdp: str = "Windows"    # Chrome DevTools platform（UA override 语义）
    accept_lang: str = DEF_ACCEPT_LANG
    tz: str = DEF_TZ
    window: tuple = DEF_WINDOW
    shapes: dict = field(default_factory=dict)   # {purpose: PurposeShape}；空→默认

    @property
    def screen_js(self) -> str:
        return "%d,%d" % (self.window[0], self.window[1])


def _win_ua(family: str, major: int) -> str:
    if family == "edge":
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/%d.0.0.0 Safari/537.36 Edg/%d.0.0.0"
                % (major, major))
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/%d.0.0.0 Safari/537.36" % major)


def profile_from_ua(ua: str = None, *, accept_lang: str = None, tz: str = None,
                    platform_js: str = None, imp_slot: str = None,
                    window: tuple = None, key: str = "") -> Profile:
    """由"一个完整 UA"构造 Profile：sec_ch_ua/imp/平台全部从 ua 派生（捕获值同源真值）。
    Safari 的 sec_ch_ua 置空串（WebKit 不送客户端提示），消费方据此不发该头。"""
    ua = ua or _win_ua("chrome", 120)
    fam = _family_of(ua)
    major = _major_of(ua, fam)
    sec_ch_ua = sec_ch_ua_for_major(fam, major)
    if fam == "safari":
        sec_ch_ua = ""
    return Profile(
        key=key or "%s%d" % (fam, major),
        family=fam, major=major, ua=ua,
        imp=imp_slot or nearest_imp(ua),
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_mobile="?0",
        sec_ch_ua_platform='""' if fam == "safari" else '"Windows"',
        platform_js=platform_js or ("MacIntel" if fam == "safari" else "Win32"),
        platform_cdp="MacIntel" if fam == "safari" else "Windows",
        accept_lang=accept_lang or DEF_ACCEPT_LANG,
        tz=tz or DEF_TZ,
        window=window or DEF_WINDOW,
    )


def _mk(key: str, family: str, major: int, imp: str) -> Profile:
    return profile_from_ua(_win_ua(family, major), key=key,
                           imp_slot=imp if imp else None)


# ---- 交付档位表（Chrome-Win 世代 + Edge；Safari 留槽不入池）----
PROFILES = {}
for _m, _im in (("131", "chrome131"), ("124", "chrome124"), ("120", "chrome120"),
                ("116", "chrome116"), ("110", "chrome110")):
    p = _mk("chrome" + _m, "chrome", int(_m), _im)
    PROFILES[p.key] = p
PROFILES["edge131"] = _mk("edge131", "edge", 131, "chrome131")
# Safari 预留槽（不入池）：后续按需启用
PROFILES["safari17_0"] = profile_from_ua(
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    key="safari17_0", accept_lang="zh-CN,zh;q=0.9")

FALLBACK_KEY = "chrome120"
PROFILE_POOL = tuple(PROFILES[k] for k in
                     ("chrome131", "chrome124", "chrome120", "chrome116",
                      "chrome110", "edge131"))
# 完整合法的 UA 池：修复旧 config.UA_POOL 的残缺串（缺 KHTML 段 / 无 .0.0.0 尾）
PROFILE_UA_POOL = tuple(p.ua for p in PROFILE_POOL)
PROFILE_KEYS = tuple(p.key for p in PROFILE_POOL)
_PROF_BY_KEY = dict((p.key, p) for p in PROFILES.values())


def profile_for_key(key: str) -> Profile:
    """取交付档；未知 key → fallback chrome120（永不抛）。"""
    return _PROF_BY_KEY.get(key) or PROFILES[FALLBACK_KEY]


def unify_ua(ua: str) -> bool:
    """校验 UA 是否为完整 Win 世代串（修复点回归门）：含 (KHTML, like Gecko)、
    Chrome/Edg 主版本带 .0.0.0 尾。残缺旧串(缺 KHTML 段/无 .0.0.0) → False。
    Edge 尾形为 `Safari/537.36 Edg/x.0.0.0`，Chrome 尾形为 `Chrome/x.0.0.0 Safari/537.36`。"""
    import re
    if not ua or "(KHTML, like Gecko)" not in ua:
        return False
    if re.search(r"Chrome/\d+\.0\.0\.0 Safari/537\.36$", ua):
        return True
    if re.search(r"Safari/537\.36 Edg/\d+\.0\.0\.0$", ua):
        return True
    return False


# ---- 身份 → Profile（捕获真值优先）----
def profile_for_identity(identity) -> Profile:
    """把一个 L0 任务身份折叠成 Profile：
    1) profile_key 命中交付档 → 以其为底（取 window/tz 缺省）；
    2) 但 ua/accept_lang/imp 一律以身份捕获值为锚（同一 Chrome 导航值就是真值）——
       故被捕获 UA 的 sec_ch_ua 精确对应当前打开浏览器的版本，而非池内写死档。"""
    base = None
    if identity is not None:
        pk = getattr(identity, "profile_key", "") or ""
        base = _PROF_BY_KEY.get(pk)
    ua = getattr(identity, "ua", "") or (base.ua if base else PROFILES[FALLBACK_KEY].ua)
    # imp 一律由 ua 就近派生（不沿用 identity.imp_slot：它可能是 dataclass 默认的陈旧值，
    # 而捕获/合成身份经 from_values 时已按 nearest_imp 算过 → 同值；这里以 ua 为唯一真值源）。
    return profile_from_ua(
        ua,
        accept_lang=getattr(identity, "accept_lang", None)
        or (base.accept_lang if base else None) or DEF_ACCEPT_LANG,
        tz=getattr(identity, "tz", None) or (base.tz if base else DEF_TZ),
        window=base.window if base else DEF_WINDOW,
        imp_slot=None,
    )


# ---- 随机档（每任务轮换；站点可覆盖；测试强制 seam）----
_FORCED_KEY = None


def force_key(key: str = None):
    """测试 seam：锁定 random_profile 返回的档位；force_key() 解除。"""
    global _FORCED_KEY
    _FORCED_KEY = key or None


def random_profile(pool: tuple = None) -> Profile:
    """从活动池随机抽一档。pool 缺省 = 全部交付池；调用方(读 config 覆盖)可传子池。
    测试用 force_key 锁定后返回该档（确定性离线断言）。"""
    if _FORCED_KEY and _FORCED_KEY in _PROF_BY_KEY:
        return _PROF_BY_KEY[_FORCED_KEY]
    candidates = pool or PROFILE_POOL
    if not candidates:
        candidates = PROFILE_POOL
    return random.choice(candidates)


# ------------------------------------------------------------------ 请求头组装

def _sec_fetch_site(purpose: str, url: str, referer: str, default_site: str) -> str:
    if referer:
        try:
            same = (urlparse(url).netloc == urlparse(referer).netloc)
        except Exception:                                            # noqa: E722
            same = False
        return "same-origin" if same else "cross-site"
    # 顶层导航无 Referer → none（Chrome 从地址栏输入）
    if purpose == PURPOSE_PAGE and not referer:
        return "none"
    return default_site


def header_items(profile: Profile, purpose: str = PURPOSE_SEGMENT, *,
                 referer: str = "", url: str = "") -> list:
    """单请求的完整规范头（身份锚点 + 客户端提示 + Accept/Sec-Fetch）。requests 消费
    直接 dict；curl 消费经 curl_extra 再滤 UA/AL。顺序贴近 Chrome 实际下发顺序。"""
    sh = shape_for_purpose(purpose)
    items = []
    if profile.sec_ch_ua:
        items.append(("sec-ch-ua", profile.sec_ch_ua))
        if profile.sec_ch_ua_mobile:
            items.append(("sec-ch-ua-mobile", profile.sec_ch_ua_mobile))
        items.append(("sec-ch-ua-platform", profile.sec_ch_ua_platform))
    for k, v in sh.extra:
        items.append((k, v))
    items.append(("User-Agent", profile.ua))
    items.append(("Accept", sh.accept))
    items.append(("Sec-Fetch-Site", _sec_fetch_site(purpose, url, referer, sh.sec_fetch_site)))
    items.append(("Sec-Fetch-Mode", sh.sec_fetch_mode))
    if sh.sec_fetch_user:
        items.append(("Sec-Fetch-User", sh.sec_fetch_user))
    items.append(("Sec-Fetch-Dest", sh.sec_fetch_dest))
    items.append(("Accept-Language", profile.accept_lang))
    return items


def media_headers(profile: Profile, purpose: str = PURPOSE_SEGMENT, *,
                  referer: str = "", url: str = "") -> dict:
    """requests/`_http_session` 路径用的头 dict（可后续 setdefault Referer 等）。"""
    return dict(header_items(profile, purpose, referer=referer, url=url))


def curl_extra(profile: Profile, purpose: str = PURPOSE_SEGMENT, *,
               referer: str = "", url: str = "") -> list:
    """curl.exe 命令追加项：Sec-CH-UA/Sec-Fetch-*。UA 走既有 -A、语言走既有 -H、
    Accept 由 curl 基座固定给 `Accept: */*`（媒体态恒 */*），故此处一并剔除避免双发/重复。
    返回 ["-H", "k: v", ...] 扁平串。"""
    args = []
    for k, v in header_items(profile, purpose, referer=referer, url=url):
        if k.lower() in ("user-agent", "accept-language", "accept"):
            continue
        args += ["-H", "%s: %s" % (k, v)]
    return args


# ------------------------------------------------------------------ 页面态派生

def page_fingerprint_for_ua(ua: str) -> dict:
    """由 UA 派生页面态指纹覆写（供 baize.set_user_agent_override / set_extra_headers）：
    Sec-CH-UA 与 UA 主版本精确一致，消灭 baize 里写死的 Chrome v130 不自洽。
    Safari 无 Sec-CH-UA 语义 → 返回空串并置 mobile ?0；unknown 同样给空（不硬猜）。"""
    p = profile_from_ua(ua)
    return {
        "sec_ch_ua": p.sec_ch_ua,             # 空串 → 调用方不发该头
        "sec_ch_ua_mobile": p.sec_ch_ua_mobile,
        "sec_ch_ua_platform": p.sec_ch_ua_platform,
        "accept_lang": p.accept_lang,
        "platform_js": p.platform_js,
        "platform_cdp": p.platform_cdp,
        "tz": p.tz,
        "screen": p.screen_js,
        "color_depth": 24,
        "imp": p.imp,
    }
