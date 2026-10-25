# video_grab.py 【视频流抓取 v1.0】— 纯后端逻辑模块，不依赖 Qt
# 功能：给定网页 URL，自动探测页面内视频流（m3u8 / mp4/flv/webm 直链），
#       下载到本地并输出元数据 JSON。
# 说明：本模块只从 config 导入常量与工具函数，不修改任何既有采集逻辑。
import os
import re
import json
import pickle
import time
import random
import base64
import html as _html_mod
import hashlib
import shutil
import subprocess
import threading
import contextlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse, unquote

import requests
import requests.exceptions as _rqex
from requests.adapters import HTTPAdapter
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed, FIRST_COMPLETED, wait

from config import (
    VIDEO_DIR, VIDEO_TIMEOUT, VIDEO_CONNECT_TIMEOUT, VIDEO_CONCURRENT,
    VIDEO_RETRY, VIDEO_CHUNK_SIZE, VIDEO_MAX_PAGE_BYTES,
    VIDEO_MAX_SEGMENTS, VIDEO_MIN_TS_SEG_BYTES,
    VIDEO_EXT_DIRECT, VIDEO_EXT_PROBE, VIDEO_RE_M3U8, VIDEO_RE_DIRECT,
    VIDEO_SERIES_PARALLEL, VIDEO_POOL_CONNECTIONS, VIDEO_POOL_MAXSIZE,
    VIDEO_ORDERED_WINDOW, VIDEO_HOST_CONCURRENCY,
    VIDEO_CONCURRENCY_MIN, VIDEO_CONCURRENCY_MAX,
    VIDEO_GLOBAL_CONCURRENCY, VIDEO_RANGE_SPLITS, VIDEO_RANGE_MIN_BYTES,
    VIDEO_PARTIAL_DIR, VIDEO_RESOLVE_MAX_HOPS,
    TIME_FORMAT, safe_name, get_random_headers, get_random_ua,
    LOG_ENABLE, LOG_VIDEO_ENC_NEED,
)
import config as _CFG   # 动态读 VIDEO_ADAPTIVE_CONCURRENCY / MIN / MAX（离线测试可变后生效）

try:
    import stealth_profile as _SP   # 指纹档位矩阵：页/媒体两态同源形状（P1 #4，纯模块必在）
except Exception:                   # noqa: E722 — 档位缺失时退回旧随机头行为（防导入硬断）
    _SP = None

try:
    import streamdecrypt as _SD   # 解密引擎：HLS AES-128 轮换/hex·json key/默认 IV=media-seq（可选依赖）
except Exception:                 # noqa: E722 — 引擎缺失时 AES 走回退单 key 路径
    _SD = None

try:
    import task_identity as _TI   # L0 任务身份：一次抓取 = 页/媒体两态同源（可选）
except Exception:                 # noqa: E722 — 身份缺失时媒体请求维持随机头行为
    _TI = None

# ================================================================
# 数据模型与通用约定
# ================================================================

# 不可下载的 URL 前缀（blob:/data:/javascript:）
_SKIP_SCHEMES = ("blob:", "data:", "javascript:")

# DASH(.mpd) JS 字面量（兼容 \\/ 转义斜杠）
RE_MPD_URL = re.compile(r'["\']((?:https?:)?(?://|\\/\\/)[^"\']*?\.mpd[^"\']*?)["\']', re.I)

# 下载失败的结构化归因：下载函数返回 dict 统一带 error_code，error 字符串保留人类可读说明。
ERR_NETWORK = "NETWORK"                     # 连接/传输层异常（超时、断连、TLS 拦截等）
ERR_HTTP_STATUS = "HTTP_STATUS"             # 非 2xx/206 HTTP 状态码
ERR_HOTLINK = "HOTLINK_AUTH"                # 返回网页/403 类，疑似防盗链或需登录
ERR_EMPTY = "EMPTY"                         # 空文件/无数据
ERR_SIZE_OVER = "SIZE_OVER_LIMIT"           # 分片数量超上限等体量拒绝
ERR_HLS_MANIFEST = "HLS_MANIFEST"           # m3u8 清单获取/解析失败
ERR_HLS_SEGMENT = "HLS_SEGMENT"             # 分片下载/合并失败
ERR_AES_KEY = "AES_KEY"                     # AES 密钥缺失/获取失败
ERR_ENCRYPT_UNSUP = "ENCRYPT_UNSUPPORTED"   # 加密方式暂不支持
ERR_NO_VIDEO_TRACK = "NO_VIDEO_TRACK"       # DASH 无视频轨
ERR_FFMPEG = "FFMPEG_REMUX"                 # 需要 ffmpeg 而缺失/remux 失败
ERR_MISSING_FFMPEG = "FFMPEG_MISSING"
ERR_PARSER = "PARSER"                       # 清单可拿但解析不出可用内容
ERR_DRM = "DRM_PROTECTED"                   # 商业 DRM（Widevine/PlayReady/FairPlay）：只识别，不解密
ERR_UNKNOWN = "UNKNOWN"

_ERROR_HINT = {
    ERR_NETWORK: "网络异常",
    ERR_HTTP_STATUS: "HTTP 状态异常",
    ERR_HOTLINK: "疑似防盗链或需登录",
    ERR_EMPTY: "空文件",
    ERR_SIZE_OVER: "超体量上限",
    ERR_HLS_MANIFEST: "清单获取/解析异常",
    ERR_HLS_SEGMENT: "分片异常",
    ERR_AES_KEY: "AES 密钥异常",
    ERR_ENCRYPT_UNSUP: "加密方式暂不支持",
    ERR_NO_VIDEO_TRACK: "无视频轨",
    ERR_FFMPEG: "ffmpeg remux 失败",
    ERR_MISSING_FFMPEG: "缺少 ffmpeg",
    ERR_PARSER: "清单解析失败",
    ERR_DRM: "商业 DRM 保护",
    ERR_UNKNOWN: "未知错误",
}


@dataclass
class VideoCandidate:
    """探测/下载一条视频流的记录。"""
    index: int              # 页面内序号（从 1 开始）
    kind: str               # "m3u8" | "dash" | "direct"
    url: str                # 绝对 URL（已 urljoin）
    ext: str                # mp4/flv/webm/...，无法判定为 ""
    src: str                # 发现方式：<video>/<source>/JS字面量/播放器配置/浏览器DOM
    page_url: str           # 来源页面
    title_hint: str = ""
    skipped: bool = False
    skip_reason: str = ""
    cookies: dict = field(default_factory=dict)   # 从浏览器捕获的同域 Cookie（防盗链/登录用）
    status: str = "pending"      # pending/ok/fail/skipped/stopped
    local_path: str = ""
    bytes_count: int = 0
    error: str = ""
    downloaded_at: str = ""


def _canon_key(url: str) -> str:
    """去重 key：去掉 query 的 hash/fragment。"""
    return url.split("#", 1)[0]


def _remove_if(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ================================================================
# #6 换身份档：线程级单次 override（仅媒体下载传输侧；浏览器层/文字路径不触碰）
# 403/429 恢复里对「下一次请求」换档用：_request_profile 首判 override，_media_headers
# 一次性取走 extra_headers。绝不污染并行集的 ambient 身份；用毕即清（上下文/恢复编排 finally）。
# ================================================================
_TL_OVERRIDE = threading.local()   # .profile_key / .extra_headers（线程级，缺省无）


def _override_clear():
    for a in ("profile_key", "extra_headers"):
        try:
            delattr(_TL_OVERRIDE, a)
        except Exception:
            pass


def _override_set(profile_key=None, extra_headers=None):
    if profile_key is not None:
        try:
            _TL_OVERRIDE.profile_key = profile_key
        except Exception:
            pass
    if extra_headers:
        try:
            _TL_OVERRIDE.extra_headers = dict(extra_headers)
        except Exception:
            pass


def _take_extra_headers():
    """一次性取走 override extra_headers（只对紧随其后的那一次媒体请求生效）。"""
    extra = getattr(_TL_OVERRIDE, "extra_headers", None)
    if extra:
        try:
            delattr(_TL_OVERRIDE, "extra_headers")
        except Exception:
            pass
    return extra or None


@contextlib.contextmanager
def _dl_profile_override(profile_key=None, extra_headers=None):
    """下载层恢复重试的换档上下文：包住单次媒体请求，退出即清（不跨段/不跨线程泄漏）。"""
    _override_set(profile_key=profile_key, extra_headers=extra_headers)
    try:
        yield
    finally:
        _override_clear()


def _swap_profile_key(exclude=None):
    """从活动档池排除当前档(exclude=其 key)后抽一个换档档位 key；池空/不可抽 → None。"""
    if _SP is None:
        return None
    try:
        pool = _stealth_active_pool()
        if exclude:
            pool = tuple(p for p in pool if p.key != exclude)
        if not pool:
            return None
        return random.choice(pool).key
    except Exception:
        return None


# ================================================================
# 页面抓取与视频 URL 探测（静态优先，不走浏览器资源拦截逻辑）
# ================================================================

def _request_profile():
    """一次媒体请求的档位：线程级 override(换档) 优先 → ambient 任务身份在位 → 由身份派生
    （捕获 ua 即真值，同源自洽）；否则按 STEALTH_PROFILE_MODE：auto(默认)→活动池随机抽一档；
    none→None(旧逐请求随机)。全部派生函数在 stealth_profile，这里只做"来源选择"。"""
    if _SP is None:
        return None
    ov = getattr(_TL_OVERRIDE, "profile_key", None)     # #6 下载层换档：profile_for_key 未知 key 即 fallback
    if ov:
        return _SP.profile_for_key(ov)
    a = None
    if _TI is not None:
        a = _TI.ambient()
    if a is not None:
        return _SP.profile_for_identity(a)
    if (getattr(_CFG, "STEALTH_PROFILE_MODE", "auto") or "auto").strip().lower() == "none":
        return None
    return _SP.random_profile(_stealth_active_pool())


def _stealth_active_pool() -> tuple:
    """config.STEALTH_PROFILE_POOL（可运行时改小/改序）→ 有效 Profile 元组；全无效回退全量交付池。"""
    if _SP is None:
        return ()
    keys = tuple(getattr(_CFG, "STEALTH_PROFILE_POOL", None) or ())
    out, seen = [], set()
    for k in keys:
        p = _SP.PROFILES.get(k)
        if p is not None and p.key not in seen:
            out.append(p)
            seen.add(p.key)
    return tuple(out) or _SP.PROFILE_POOL


def _task_profile(url: str):
    """一次抓取的任务档（页面态 Chrome + 纯 requests 下载共用同一决定点）：
    STEALTH_SITE_PROFILE[host] 站点覆盖 > STEALTH_PROFILE_KEY(fixed/显式) > auto 活动池每任务抽一档。
    返回 Profile 或 None —— mode=none / _SP 缺失 → None（调用方维持旧行为 / 逐请求随机，零回归）。"""
    if _SP is None:
        return None
    mode = (getattr(_CFG, "STEALTH_PROFILE_MODE", "auto") or "auto").strip().lower()
    if mode == "none":
        return None
    host = (urlparse(url).netloc or "").lower()
    try:
        site_map = getattr(_CFG, "STEALTH_SITE_PROFILE", None) or {}
        for h, k in site_map.items():
            h = str(h).lower()
            if host == h or host.endswith("." + h):     # 整 host 或子域命中覆盖
                return _SP.profile_for_key(k)           # 未知 key → fallback（永不抛）
    except Exception:
        pass
    key = (getattr(_CFG, "STEALTH_PROFILE_KEY", "") or "").strip().lower()
    if key:
        return _SP.profile_for_key(key)
    if mode == "fixed":
        return _SP.profile_for_key(_SP.FALLBACK_KEY)
    return _SP.random_profile(_stealth_active_pool())


def _synthetic_task_identity(prof):
    """把任务档 Profile 折成 L0 身份（纯 requests 源任务用：整页/整集下载同源同档）。
    无 _TI 或缺档 → None。ua 即锚，profile_key 落点标记使 profile_for_identity 拿到 base 缺省。"""
    if prof is None or _TI is None:
        return None
    try:
        return _TI.from_values(prof.ua, accept_lang=prof.accept_lang, tz=prof.tz,
                                imp_slot=prof.imp, profile_key=prof.key, note="每任务档位")
    except Exception:
        return None


def _acquire_browser(url):
    """浏览器任务档位入口：_task_profile 定本任务档 → 其 ua 交给 acquire_driver（含池/直连复用重覆写）。
    档位决定失败 / mode=none → 传 None（qiongqi_maw 维持旧随机启动，零回归）。"""
    import qiongqi_maw
    prof = _task_profile(url)
    return qiongqi_maw.acquire_driver(ua=prof.ua if prof is not None else None)


def _release_browser(driver):
    """把浏览器驱动归还 qiongqi_maw 池/单实例（懒 import，与 _acquire_browser 同缝）。"""
    if driver is None:
        return
    try:
        import qiongqi_maw
        qiongqi_maw.release_driver(driver)
    except Exception:
        pass


def _media_headers(url: str, referer: str = "", *, purpose=None) -> dict:
    """媒体态统一请求头（requests/`_http_session` 路径）。
    - ambient 任务身份 → 身份派生档（捕获 UA/语言即锚点，Sec-CH-UA 精确对应当前 Chrome 版本）；
    - 无身份 auto → 每请求抽一档，UA/语言/Sec-CH-UA/Sec-Fetch 由同一档派生（请求内部自洽，
      修掉"清单/分片 Accept 还是 text/html"的媒体态形状错）；
    - 无身份 none → 旧随机头（逃逸缝，零回归）。
    purpose 显式（page/api/manifest/segment/key/direct/range）优先；缺省按 URL 扩展名归类。"""
    prof = _request_profile()
    if prof is None:
        h = dict(get_random_headers())
        h.setdefault("User-Agent", get_random_ua())
    else:
        if purpose is None:
            purpose = _SP.purpose_for_url(url)
        h = _SP.media_headers(prof, purpose, referer=referer, url=url)
        h.setdefault("User-Agent", prof.ua)
    if referer and "Referer" not in h:
        h["Referer"] = referer
    _extra = _take_extra_headers()            # #6 换档 extra（signer 附加头等，一次性取走）
    if _extra:
        h.update(_extra)
    return h


def _curl_profile_args(purpose: str, url: str, referer: str):
    """curl.exe 回退路径的一次请求身份头：同一档派生 (ua, accept_lang, extra_args)。
    requests 被掐/拉黑切到 curl.exe schannel 时，UA/语言/Sec-CH-UA/Sec-Fetch 由同一档位派生
    —— 该请求内部自洽（L0 同源 + 媒体态形状），不是 requests 态与 curl 态各随机。
    返回 extra_args 为 [“-H”, “k: v”, …] 扁平串，已剔除 UA/AL（走 -A 与单独 -H）。"""
    prof = _request_profile()
    if prof is None:                       # mode=none / 模块缺失 → 旧行为（仅 UA，无 AL/提示头）
        return get_random_ua(), "", []
    extra = _SP.curl_extra(prof, purpose, referer=referer, url=url)
    return prof.ua, prof.accept_lang, extra


def _identity_ambient(identity):
    """把一次媒体下载包进 L0 任务身份：identity 在位且引擎就绪 → _TI.use(identity)
    （下载全程 ambient，页/清单/分片/key 同源）；否则 nullcontext（旧随机行为，零回归）。
    供 process_single_page / _download_episode / audio 等编排下载点使用。"""
    if _TI is not None and identity is not None:
        return _TI.use(identity)
    return contextlib.nullcontext()


# ================================================================
# 共享 HTTP 会话：连接复用(keep-alive) + stale-pool 自愈 + 可调并发闸门
# ================================================================
_SESSION = None
_SESSION_LOCK = threading.Lock()
_BLOCKED_LOCK = threading.Lock()


class _Gate:
    """可调并发闸门：同一时刻最多 cap 个在途，线程安全，支持在线 set_cap。
    附带 tick 统计（完成/失败/字节/耗时/排队采样），供自适应控制器读取后清零。
    任何下载路径（_http_get、Range 分片 worker）都在闸门内完成整次体传输，
    因此"在途请求数"即真实打到源上的并发连接数——礼貌吃满的单一约束点。"""
    __slots__ = ("name", "_cap", "_active", "_cv",
                 "_stat_lock", "n_ok", "n_fail", "n_bytes", "n_dt", "n_wait", "n_pass")

    def __init__(self, name: str, cap: int):
        self.name = name
        self._cap = max(1, int(cap))
        self._active = 0
        self._cv = threading.Condition()
        self._stat_lock = threading.Lock()
        self.n_ok = self.n_fail = self.n_bytes = 0   # n_wait/n_pass 由 acquire 在 _cv 下自增（CPython 单赋值原子）
        self.n_dt = 0.0
        self.n_wait = self.n_pass = 0

    def acquire(self):
        with self._cv:
            if self._active >= self._cap:
                self.n_wait += 1
                while self._active >= self._cap:
                    self._cv.wait()
            else:
                self.n_pass += 1
            self._active += 1

    def release(self):
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def set_cap(self, n: int):
        """在线调整并发上限（自适应控制器/降级用）。"""
        with self._cv:
            self._cap = max(1, int(n))
            self._cv.notify_all()

    def record(self, ok: bool, dt: float, nbytes: int):
        with self._stat_lock:
            if ok:
                self.n_ok += 1
                self.n_bytes += nbytes
            else:
                self.n_fail += 1
            self.n_dt += dt

    def take_stats(self) -> dict:
        """取走并清零窗口统计（控制器每个 tick 对每个活跃闸门调用一次）。"""
        with self._stat_lock:
            s = {"ok": self.n_ok, "fail": self.n_fail,
                 "bytes": self.n_bytes, "dt": self.n_dt,
                 "wait": self.n_wait, "pass": self.n_pass}
            self.n_ok = self.n_fail = self.n_bytes = 0
            self.n_dt = 0.0
            self.n_wait = self.n_pass = 0
            return s

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def active(self) -> int:
        return self._active


# 全局闸门：整进程在途请求总量上限（多 host 分源时每 host ≤12 而总量 ≤24，礼貌约束）。
_GLOBAL_GATE = _Gate("global", max(1, int(VIDEO_GLOBAL_CONCURRENCY)))
# 单 host 并发闸门：整部多集常命中同一 CDN(host)，共享一份在途配额，
# 既把该 host 带宽吃满、又不因 并发×集数 超发把源打爆。cap 可被自适应控制器调。
_HOST_GATES = {}
_HOST_GATES_LOCK = threading.Lock()
# 单 host 连续失败计数（主机健康：≥3 快速降级到 MIN，成功序列恢复由控制器拉升）。
_HOST_HEALTH = {}
_HOST_HEALTH_LOCK = threading.Lock()


def _host_gate(netloc: str) -> "_Gate":
    """返回该 host 的可调闸门（初始并发 VIDEO_HOST_CONCURRENCY）。"""
    gate = _HOST_GATES.get(netloc)
    if gate is None:
        with _HOST_GATES_LOCK:
            gate = _HOST_GATES.get(netloc)
            if gate is None:
                gate = _Gate("host:" + netloc, max(1, int(VIDEO_HOST_CONCURRENCY)))
                _HOST_GATES[netloc] = gate
    return gate


def _health_mark(netloc: str, ok: bool) -> None:
    """主机健康：失败连续计数、降级与恢复的开关量。
    连续失败 ≥3 → 立即把该 host 并发压到 MIN（避免继续超发把源打崩）；
    连续成功 ≥5 → 解除 down，让自适应控制器按吞吐把它拉回。"""
    if not netloc:
        return
    with _HOST_HEALTH_LOCK:
        h = _HOST_HEALTH.get(netloc)
        if h is None:
            h = {"fail": 0, "ok": 0, "down": False}
            _HOST_HEALTH[netloc] = h
        if ok:
            h["fail"] = 0
            h["ok"] += 1
            if h["down"] and h["ok"] >= 5:
                h["down"] = False
                h["ok"] = 0
                # 恢复：cap 退回静态默认附近（自适应控制器之后按吞吐微调）
                gate = _HOST_GATES.get(netloc)
                if gate is not None:
                    target = max(int(getattr(_CFG, "VIDEO_CONCURRENCY_MIN", 2)),
                                 min(int(getattr(_CFG, "VIDEO_HOST_CONCURRENCY", 12)),
                                     int(getattr(_CFG, "VIDEO_CONCURRENCY_MAX", 20))))
                    if gate.cap < target:
                        gate.set_cap(target)
        else:
            h["ok"] = 0
            h["fail"] += 1
            if h["fail"] >= 3:
                h["down"] = True
                gate = _HOST_GATES.get(netloc)
                if gate is not None and gate.cap > _CFG.VIDEO_CONCURRENCY_MIN:
                    gate.set_cap(_CFG.VIDEO_CONCURRENCY_MIN)


def _is_health_down(netloc: str) -> bool:
    with _HOST_HEALTH_LOCK:
        h = _HOST_HEALTH.get(netloc)
        return bool(h and h["down"])


# ================================================================
# #6 每host下载代理（仅下载传输层：requests / curl_cffi / curl.exe；不含浏览器层）
# 范围红线（config VIDEO_PROXY_* 注释同源）：浏览器层(qiongqi 单共享 Chrome)不接代理；
# datadome/akamai/f5 rotate_fingerprint/cookie_reset 不自动；socks5 只走 curl_cffi/curl.exe，
# requests 路径拒推(不引 PySocks，避免拖文字/页面路径)。
# 健康表键 = _host_of 语义(去端口 lower)；勿与 _host_gate 的含端口 netloc 键混用。
# 默认空 map+空 fallback → _proxy_pick 恒 None → 所有接入点零开销、逐字节保现状。
# ================================================================
_PROXY_HEALTH = {}          # (host, proxy) -> {"fails":int,"oks":int,"down_until":float}
_PROXY_HEALTH_LOCK = threading.Lock()


def _proxy_host_key(url_or_host: str) -> str:
    """代理映射/健康表键：URL 取 netloc 去端口 lower；裸 host 剥可选端口。"""
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        return _host_of(s)
    return (s.split(":")[0]).strip() if s else s


def _proxy_candidates_for(host: str):
    """某 host 的有序代理候选：pool[host] + map[host] + fallback，支持 ".suffix" 子域匹配。
    返回去重保序的 [proxy_url,...]；无 → []。"""
    if not getattr(_CFG, "VIDEO_PROXY_ENABLED", True):
        return []
    cfgmap = getattr(_CFG, "VIDEO_PROXY_HOST_MAP", None) or {}
    cfgpool = getattr(_CFG, "VIDEO_PROXY_HOST_POOLS", None) or {}
    use_suffix = bool(getattr(_CFG, "VIDEO_PROXY_SUFFIX_MATCH", True))
    cands = []
    for m in (cfgpool, cfgmap):
        for h, val in m.items():
            h = str(h).strip().lower()
            if not (host == h or (use_suffix and h.startswith(".") and host.endswith(h))):
                continue
            if isinstance(val, (list, tuple)):
                cands.extend(v for v in val if isinstance(v, str) and v)
            elif isinstance(val, str) and val:
                cands.append(val)
    if not cands:
        fb = (getattr(_CFG, "VIDEO_PROXY_FALLBACK", "") or "").strip()
        if fb:
            cands.append(fb)
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _proxy_pick(url: str):
    """为一次媒体请求挑上游：跳过冷却中(down_until>now)的，余下 random.choice。无 → None(直连)。"""
    host = _proxy_host_key(url)
    cands = _proxy_candidates_for(host)
    if not cands:
        return None
    now = time.time()
    alive = []
    with _PROXY_HEALTH_LOCK:
        for c in cands:
            st = _PROXY_HEALTH.get((host, c))
            if not st or not st.get("down_until") or st["down_until"] <= now:
                alive.append(c)
    return random.choice(alive) if alive else None


def _proxy_mark(url: str, proxy: str, ok: bool) -> None:
    """健康记账：失败++(≥FAILS_TO_DROP → 冷却 COOLDOWN_SEC)；冷却中成功++(≥OK_TO_RESTORE → 解)。"""
    if not proxy:
        return
    host = _proxy_host_key(url)
    fails = int(getattr(_CFG, "VIDEO_PROXY_FAILS_TO_DROP", 3))
    oks = int(getattr(_CFG, "VIDEO_PROXY_OK_TO_RESTORE", 2))
    cooldown = float(getattr(_CFG, "VIDEO_PROXY_COOLDOWN_SEC", 300.0))
    with _PROXY_HEALTH_LOCK:
        st = _PROXY_HEALTH.setdefault((host, proxy), {"fails": 0, "oks": 0, "down_until": 0.0})
        if ok:
            st["oks"] += 1
            st["fails"] = 0
            if st.get("down_until") and st["oks"] >= oks:
                st["down_until"] = 0.0
        else:
            st["fails"] += 1
            st["oks"] = 0
            if st["fails"] >= fails:
                st["down_until"] = time.time() + cooldown
                st["fails"] = 0


def _proxy_apply(url: str, *, allow_socks: bool):
    """requests/cffi 每请求用：返回 (proxy_url_or_None, proxies_dict_or_None)。
    proxy 非 None 且 dict 非 None ⇔ 本路径真路由该上游。
    allow_socks=False(requests)：socks5 上游不路由 → (None,None)（本路径走直连）。
    allow_socks=True(curl_cffi)：socks5/http(s) 均映射 {scheme:url}（curl_cffi 底层即 curl）。"""
    p = _proxy_pick(url)
    if not p:
        return None, None
    if p.split("://", 1)[0].lower() in ("http", "https") or allow_socks:
        return p, {"http": p, "https": p}
    return None, None


def _proxy_curl_args(url: str):
    """curl.exe subprocess 用：socks5/http(s) 均原生支持。返回 ["--proxy", url] 或 []。"""
    p = _proxy_pick(url)
    return ["--proxy", p] if p else []


def _curl_proxy_fail(stderr: bytes) -> bool:
    """curl stderr 是否命中代理侧失败（与源侧 403/404 区分——源侧错误不进代理健康计数）。"""
    if not stderr:
        return False
    s = stderr.decode("utf-8", "ignore").lower()
    return any(k in s for k in ("failed to connect to proxy", "407", "could not resolve proxy",
                                "socks", "proxy authentication", "received http code 407"))


# ---- 自适应并发控制器 ----
# 目标：任何源都自动吃到它的并发上限。host 闸门顶满(排队)且健康 → 试探性升 cap，
# 下一 tick 用"每并发吞吐"(bytes/s/cap) 校验：新增并发把单连接吞吐摊薄到 <0.9 基线 →
# 收回；反之保留并继续试探。失败率升高 → 快速降级。整段空闲 → 不动。
_ADAPTIVE_ON = bool(getattr(_CFG, "VIDEO_ADAPTIVE_CONCURRENCY", False))
_CTRL_THREAD = None
_CTRL_STARTED = False
_ADAPT_TICK = 1.2            # 采样 tick（秒）
_PROBE_EVERY = 3             # 稳定饱和多少 tick 后发起一次试探性升
_CPT_FLOOR = 0.90            # 试探后每并发吞吐低于基线此比例 → 收回并发
_OK_RATE_DROP = 0.90         # 成功率低于此 → 降级


def _adapt_decide(cap: int, *, ok_rate: float, saturated: bool,
                  cpt: float, prev_cpt: float,
                  probing: bool, probe_due: bool,
                  cap_min: int, cap_max: int,
                  cpt_floor: float = _CPT_FLOOR):
    """纯决策函数（离线可测）：由上一 tick 状态决定下一 tick 的 cap。
    返回 (new_cap, new_probing)。
      ok_rate    : 本 tick 请求成功率(0..1)
      saturated  : 闸门是否被顶满（大部分 acquire 都排队过）
      cpt        : 本 tick 每并发吞吐 bytes/s/并发
      prev_cpt   : 试探升前的基线 cpt（probing 时用来校验）
      probing    : 上 tick 刚升过，本 tick 用于验证是否值得保留
      probe_due  : 稳定饱和够久，可发起试探性升
    规则：失败率高→降；未饱和→持平（说明并发够用）；试探后无增益→收回；
         饱和且久未试探、未到上限→+2 试探。
    """
    cap_min, cap_max = max(1, int(cap_min)), max(1, int(cap_max))
    if ok_rate < _OK_RATE_DROP:
        return max(cap_min, int(cap) - 3), False
    if not saturated:
        return int(cap), False
    if probing:
        # 校验上次试探：新增并发若把单连接吞吐摊薄到 <0.9× 基线 → 收回
        if prev_cpt > 0 and cpt > 0 and cpt < prev_cpt * cpt_floor:
            return max(cap_min, int(cap) - 2), False
        return int(cap), False
    if probe_due and int(cap) < cap_max:
        return min(cap_max, int(cap) + 2), True    # 试探升，下一 tick 校验
    return int(cap), False


def _ctrl_loop():
    """自适应 daemon 控制器：周期采样每个有流量的 host 闸门 → _adapt_decide → set_cap。"""
    state = {}   # netloc -> {"prev_cpt": float, "probing": bool, "since": int}
    while True:
        time.sleep(_ADAPT_TICK)
        try:
            host_gates = dict(_HOST_GATES)
        except Exception:
            host_gates = {}
        snap = {}
        for netloc, gate in host_gates.items():
            st = gate.take_stats()
            if st["ok"] or st["fail"]:
                snap[netloc] = (gate, st)
        if not snap:
            continue
        cap_min = max(1, int(getattr(_CFG, "VIDEO_CONCURRENCY_MIN", 2)))
        cap_max = max(cap_min, int(getattr(_CFG, "VIDEO_CONCURRENCY_MAX", 20)))
        for netloc, (gate, st) in snap.items():
            st0 = state.setdefault(netloc, {"prev_cpt": 0.0, "probing": False, "since": 0})
            total = st["ok"] + st["fail"]
            if total <= 0:
                continue
            ok_rate = st["ok"] / float(total)
            dt = st["dt"]
            tp = ((st["bytes"] if st["bytes"] else st["ok"]) / dt) if dt > 0 else 0.0
            aq = st["wait"] + st["pass"]
            saturated = aq > 0 and st["wait"] >= aq * 0.5
            cpt = (tp / gate.cap) if (gate.cap > 0 and tp > 0) else 0.0
            new_cap, new_prob = _adapt_decide(
                gate.cap, ok_rate=ok_rate, saturated=saturated,
                cpt=cpt, prev_cpt=st0["prev_cpt"],
                probing=st0["probing"],
                probe_due=(not st0["probing"] and st0["since"] >= _PROBE_EVERY),
                cap_min=cap_min, cap_max=cap_max)
            if new_cap != gate.cap:
                gate.set_cap(new_cap)
            # 更新控制器记忆
            if new_prob:
                st0["prev_cpt"] = cpt if cpt > 0 else st0["prev_cpt"]
                st0["probing"] = True
                st0["since"] = 0
            else:
                st0["probing"] = False
                if cpt > 0:
                    st0["prev_cpt"] = cpt
                st0["since"] = 0 if (ok_rate < _OK_RATE_DROP or not saturated) else st0["since"] + 1


def _ensure_controller():
    """惰性启动自适应 daemon 控制器（整进程一个）。开关关闭时不启动。"""
    global _CTRL_THREAD, _CTRL_STARTED
    if _CTRL_STARTED:
        return
    if not _ADAPTIVE_ON:
        return
    with _SESSION_LOCK:
        if _CTRL_STARTED:
            return
        if not _ADAPTIVE_ON:
            return
        _CTRL_STARTED = True
        t = threading.Thread(target=_ctrl_loop, daemon=True, name="video-adaptive")
        _CTRL_THREAD = t
        t.start()


# ---- 进程级已下载字节合计（供整部遥测：同刻 MB/s / 活跃 host）----
_DL_BYTES = 0
_DL_LOCK = threading.Lock()


def _tally_bytes(n: int) -> None:
    global _DL_BYTES
    if n and n > 0:
        with _DL_LOCK:
            _DL_BYTES += n


def _total_downloaded_bytes() -> int:
    with _DL_LOCK:
        return _DL_BYTES


def _session_pool_sizes():
    """并发分片 / 整部多集可能同时打同一 host：单 host 池需容下全部并发连接。"""
    concurrency = max(1, VIDEO_CONCURRENT)
    parallel = max(1, VIDEO_SERIES_PARALLEL)
    per_host = max(int(VIDEO_POOL_MAXSIZE), concurrency * parallel + 2)
    return max(1, int(VIDEO_POOL_CONNECTIONS)), per_host


def _http_session() -> requests.Session:
    """惰性共享 Session 单例。会话头被清空：UA/Referer/Cookie 一律 per-call 传入，
    既避免跨 host 泄漏登录态，又保证并发 GET 只共享连接池（requests.Session 并发安全）。"""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                conns, per_host = _session_pool_sizes()
                s = requests.Session()
                s.headers.clear()
                adapter = HTTPAdapter(pool_connections=conns,
                                      pool_maxsize=per_host,
                                      max_retries=0)   # 复用的 stale 连接靠下面自愈，不靠 requests 盲目重试
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                _SESSION = s
    return _SESSION


def _drop_host_conns(session, url: str) -> None:
    """尽力关闭某 host 的复用连接池（被 CDN 掐掉的 stale 连接必须丢弃，否则每次复用它都白失败一次）。
    跨 urllib3 版本 best-effort；失败仅意味着下次多一次首连开销，不影响正确性。"""
    try:
        adapter = session.get_adapter(url)
        pm = adapter.poolmanager
        p = urlparse(url)
        host, scheme = (p.hostname or "").lower(), p.scheme or "https"
        port = p.port or (443 if scheme == "https" else 80)
        try:
            pm.clear(host, port)          # 部分版本 PoolManager.clear(host,port)
            return
        except Exception:
            pass
        pools = getattr(pm, "pools", None)   # urllib3 内部: {(scheme, host, port): pool}
        if pools is not None:
            key = (scheme, host, port)
            if key in pools:
                try:
                    pools[key].close()
                except Exception:
                    pass
                try:
                    del pools[key]
                except Exception:
                    pass
    except Exception:
        pass


# ================================================================
# 浏览器级 TLS 指纹取流（"转发套站"专项）：只对 register_impersonate_host 注册的
# host 生效，其余 host 一律维持 requests —— 离线/其它站零回归。
# curl_cffi impersonate 优先；其不可用时由调用方（sorani play API）回退单实例 Chrome。
# 内容本就是站方免费公开给浏览器的流（无 DRM/登录/付费墙），仅让请求在 TLS/头指纹层面
# 表现为正常 Chrome；保持单 IP、礼貌、有界、有节拍。
# ================================================================
_IMPERSONATE_HOSTS = set()        # host 后缀 → 需指纹伪装的 host
_IMPERSONATE_LOCK = threading.Lock()
_IMP_CFFI = None                  # curl_cffi.requests 模块（惰性缓存）
_IMP_CFFI_TRIED = False           # 是否已尝试 import
_IMP_CFFI_LOCK = threading.Lock()
_IMP_LOCAL = threading.local()    # per-thread curl Session（curl_cffi Session 非线程安全）
# requests 浏览器头里会被 curl_cffi impersonate 默认头覆盖/冲突的键——剥掉让 curl 自管
# UA/Sec-Fetch/Accept 系与头序（完整指纹要求头序由 impersonate 引擎决定）。
_IMP_DROP_HDRS = {
    "connection", "upgrade-insecure-requests", "user-agent", "accept",
    "accept-encoding", "accept-language", "dnt", "cache-control", "pragma",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
}
# 签名叶子授权门：sorani play-API 下发 playUrl 后，叶子 CDN 会拒掉"立刻取流"（403 No permission，
# 真 Chrome 因播放器启动有自然延迟）。记录最近一次授权时刻，叶子首次取流须 ≥ 最小间隔。
_IMP_GRANT_LOCK = threading.Lock()
_IMP_LAST_GRANT = 0.0
_IMP_LEAF_SUFFIXES = ("sorani-vids.xyz",)    # 实测受该门控的签名叶子；新叶子站点可在此追加


def _imp_mark_grant() -> None:
    """拿到新签名流（playUrl 等）后调用：后续对签名叶子的首次取流将按最小间隔节拍。"""
    global _IMP_LAST_GRANT
    with _IMP_GRANT_LOCK:
        _IMP_LAST_GRANT = time.time()


def _imp_leaf_pace(netloc: str) -> None:
    """签名叶子取流节拍：请求距上次授权须 ≥ VIDEO_SORANI_GRANT_TO_LEAF_DELAY 秒。
    只对签名叶子生效；授权 host(api) 自身与未注册 host 不触发。长下载期间授权已过期 → 零延迟。"""
    if not any(netloc == s or netloc.endswith("." + s) for s in _IMP_LEAF_SUFFIXES):
        return
    gap = float(getattr(_CFG, "VIDEO_SORANI_GRANT_TO_LEAF_DELAY", 4.0) or 0.0)
    if gap <= 0:
        return
    while True:
        with _IMP_GRANT_LOCK:
            last = _IMP_LAST_GRANT
        wait = last + gap - time.time()
        if wait <= 0:
            return
        time.sleep(min(wait, 2.0))


def register_impersonate_host(host_suffix: str) -> None:
    """把某 host 后缀注册进指纹层：其后所有 http 请求走 curl_cffi（若引擎就绪）。
    只加在实测被 requests/curl schannel 掐连的 host；叶子 CDN 默认不加。"""
    s = (host_suffix or "").strip().lower().lstrip(".")
    if not s:
        return
    with _IMPERSONATE_LOCK:
        _IMPERSONATE_HOSTS.add(s)


def _imp_matches(host_or_url: str) -> bool:
    """注册后缀是否命中该 url/host（最长后缀容忍子域）。"""
    host = _netloc_of(host_or_url)
    if not host:
        return False
    with _IMPERSONATE_LOCK:
        hosts = tuple(_IMPERSONATE_HOSTS)
    return any(host == h or host.endswith("." + h) for h in hosts)


def _imp_cffi_module():
    """惰性 import curl_cffi.requests 并缓存；失败缓存 None。只在命中指纹 host 时才被触发，
    未注册 host / 主开关关时绝不 import（离线 127.0.0.1 探针不受影响）。"""
    global _IMP_CFFI, _IMP_CFFI_TRIED
    if _IMP_CFFI_TRIED:
        return _IMP_CFFI
    with _IMP_CFFI_LOCK:
        if _IMP_CFFI_TRIED:
            return _IMP_CFFI
        try:
            import curl_cffi.requests as _cffi_requests   # 无顶层 import：缺失时自动 None
            _IMP_CFFI = _cffi_requests
        except Exception:
            _IMP_CFFI = None
        _IMP_CFFI_TRIED = True
    return _IMP_CFFI


def _imp_state_for(url_or_host: str) -> str:
    """url/host 应走的指纹引擎态：""=不走指纹(requests) | "curl"=curl_cffi | "chrome"=单实例 Chrome。
    curl_cffi 就绪且引擎允许 → curl；engine=auto 且 curl_cffi 不可用 → chrome（仅 API 兜底走）。
    主开关关 / 未注册 host → 一律 ""（此分支不触碰 curl_cffi import）。"""
    if not getattr(_CFG, "VIDEO_IMPERSONATE_ENABLED", True):
        return ""
    if not _imp_matches(url_or_host):
        return ""
    engine = (getattr(_CFG, "VIDEO_IMPERSONATE_ENGINE", "auto") or "auto").strip().lower()
    if engine == "chrome":
        return "chrome"
    if engine == "curl_cffi":
        return "curl" if _imp_cffi_module() is not None else ""
    if _imp_cffi_module() is not None:
        return "curl"
    return "chrome"


def _imp_clean_headers(headers: dict) -> dict:
    """剥 requests 浏览器随机头里与 impersonate 冲突的键，仅保留 Referer/Origin/自定义 X-* 等。"""
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in _IMP_DROP_HDRS:
            continue
        out[k] = v
    return out


def _ambient_imp_slot() -> str:
    """任务身份应走的 impersonate 槽：ambient 在位的 UA 就近派生（与档位派生同一函数）。
    无 ambient / 引擎无档位概念 → ""（退 config 槽）。"""
    if _TI is None:
        return ""
    a = _TI.ambient()
    if a is None or not getattr(a, "ua", ""):
        return ""
    if _SP is not None:
        try:
            return _SP.nearest_imp(a.ua)
        except Exception:
            return ""
    return getattr(a, "imp_slot", "") or ""


def _imp_thread_session():
    """每线程惰性建 curl Session（impersonate 逐候选降级），缓存键含**档位槽**：
    同一 worker 线程跨任务(ambient 档位变化) → 关旧建新，不把上一个任务的 TLS 档继续打给
    下一个任务（跨任务复用旧档指纹 = 身份泄漏）。断连后 _imp_drop_local 置 None 下次全新握手。"""
    prim = _ambient_imp_slot() or (getattr(_CFG, "VIDEO_IMPERSONATE_IMP", "") or "")
    s = getattr(_IMP_LOCAL, "cffi", None)
    if s is not None and getattr(_IMP_LOCAL, "cffi_slot", None) == prim:
        return s
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    mod = _imp_cffi_module()
    if mod is None:
        _IMP_LOCAL.cffi = None
        return None
    cands = (prim,) + tuple(getattr(_CFG, "VIDEO_IMPERSONATE_CANDIDATES", ()) or ())
    seen, made = set(), None
    for imp in cands:
        if not imp or imp in seen:
            continue
        seen.add(imp)
        try:
            made = mod.Session(impersonate=imp)
            break
        except Exception:
            made = None
    if made is None:
        _IMP_LOCAL.cffi = None
        return None
    _IMP_LOCAL.cffi = made
    _IMP_LOCAL.cffi_slot = prim
    return made


def _imp_drop_local() -> None:
    """丢弃本线程 curl Session（断连后下一个请求全新握手，等价 requests 侧的 drop_host_conns）。"""
    try:
        s = getattr(_IMP_LOCAL, "cffi", None)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
            _IMP_LOCAL.cffi = None
    except Exception:
        pass


def _imp_retryable_excs(mod):
    """curl_cffi 的连接级可重试异常元组；引擎未暴露对应类型则宽兜底 Exception。"""
    try:
        E = mod.exceptions
        return tuple(getattr(E, n) for n in
                     ("ConnectionError", "ChunkedEncodingError", "Timeout"))
    except Exception:
        return (Exception,)


def _impersonate_get(url: str, *, headers: dict, timeout: int = VIDEO_TIMEOUT,
                     cookies: dict = None, stream: bool = False):
    """指纹 host 的取流入口（_http_get 命中"curl"时转发于此）。契约与 _http_get 一致：
    global→host 双闸同序、host 统计/健康上报、成功 (resp,None)、失败 (None,exc)。
    curl Session 断连 → 关本线程 session 全新握手重试一次；二次失败 (None, e2)。
    由调用方保证：命中 curl 的 host 不落 requests 黑名单、不进 curl.exe schannel 兜底。"""
    netloc = (urlparse(url).netloc or "").lower()
    host_gate = _host_gate(netloc)
    _imp_leaf_pace(netloc)                  # 签名叶子：距上次授权 < 间隔则等（403 门）
    if _ADAPTIVE_ON and _CTRL_STARTED is False and _CTRL_THREAD is None:
        _ensure_controller()
    t0 = time.time()
    hdrs = _imp_clean_headers(headers)
    ex_types = _imp_retryable_excs(_imp_cffi_module())
    holder = {}                                 # 本次尝试真实路由的代理（健康记账用）

    def _do():
        with _GLOBAL_GATE:
            with host_gate:
                s = _imp_thread_session()
                if s is None:
                    raise _rqex.ConnectionError("curl_cffi 引擎不可用")
                proxy, proxies = _proxy_apply(url, allow_socks=True)
                if proxies:
                    holder["proxy"] = proxy
                return s.get(url, headers=hdrs, cookies=cookies, stream=stream,
                             proxies=proxies,
                             timeout=(VIDEO_CONNECT_TIMEOUT, timeout))

    def _finish_ok(r):
        if holder.get("proxy"):
            _proxy_mark(url, holder["proxy"], True)
        nbytes = 0
        if not stream:
            try:
                nbytes = len(r.content)
            except Exception:
                nbytes = 0
        host_gate.record(True, time.time() - t0, nbytes)
        _tally_bytes(nbytes)
        _health_mark(netloc, True)
        return r, None

    try:
        return _finish_ok(_do())
    except ex_types:
        if holder.get("proxy"):
            _proxy_mark(url, holder["proxy"], False)   # 先记失败 → 重试 _do 再 _proxy_pick 自然跳过该上游
        _imp_drop_local()                       # 掐掉的 stale 握手必须丢，下次全新握手
        try:
            return _finish_ok(_do())
        except Exception as e2:
            host_gate.record(False, time.time() - t0, 0)
            _health_mark(netloc, False)
            return None, e2
    except Exception as e2:
        host_gate.record(False, time.time() - t0, 0)
        _health_mark(netloc, False)
        return None, e2


def _http_get(url: str, *, headers: dict, timeout: int = VIDEO_TIMEOUT,
              cookies: dict = None, stream: bool = False):
    """Session GET（幂等）。取 global→host 双闸（固定顺序防死锁、反序释放），
    把整进程在途总量与单 host 并发都限制住（礼貌吃满的约束点）；
    stream=False 时整份 body 在闸门内读完（闸门=真实并发）；stream=True 仅头部在闸门内。
    连接级异常（CDN 掐 stale 连接/超时/截断）→ 丢弃该 host 池、用新连接重试一次，
    再失败则抛原异常（由调用方回退 curl 或归因）。
    成功/失败都做 host 统计与健康上报（供自适应控制器/降级用）。
    返回 (response, None)；两次都失败返回 (None, exc)。"""
    if _imp_state_for(url) == "curl":
        # 指纹 host：请求在 TLS/头指纹层面表现为 Chrome（_impersonate_get 自带双闸/健康/重试）。
        return _impersonate_get(url, headers=headers, timeout=timeout,
                                cookies=cookies, stream=stream)
    netloc = (urlparse(url).netloc or "").lower()
    host_gate = _host_gate(netloc)
    if _ADAPTIVE_ON and _CTRL_STARTED is False and _CTRL_THREAD is None:
        _ensure_controller()
    t0 = time.time()
    holder = {}                                 # 本次尝试真实路由的代理（健康记账用）

    def _do():
        with _GLOBAL_GATE:
            with host_gate:
                proxy, proxies = _proxy_apply(url, allow_socks=False)
                if proxies:
                    holder["proxy"] = proxy     # requests 路径 socks5 不路由 → 不记账
                return _http_session().get(url, headers=headers, cookies=cookies,
                                           stream=stream, proxies=proxies,
                                           timeout=(VIDEO_CONNECT_TIMEOUT, timeout))

    def _finish_ok(r):
        if holder.get("proxy"):
            _proxy_mark(url, holder["proxy"], True)
        nbytes = len(r.content) if not stream else 0
        host_gate.record(True, time.time() - t0, nbytes)
        _tally_bytes(nbytes)
        _health_mark(netloc, True)
        return r, None

    try:
        r = _do()
        return _finish_ok(r)
    except (_rqex.ConnectionError, _rqex.ChunkedEncodingError, _rqex.Timeout) as e:
        if holder.get("proxy"):
            _proxy_mark(url, holder["proxy"], False)
        _drop_host_conns(_http_session(), url)       # 丢弃坏掉的复用连接
        try:
            r = _do()
            return _finish_ok(r)
        except Exception as e2:
            host_gate.record(False, time.time() - t0, 0)
            _health_mark(netloc, False)
            return None, e2


def fetch_html(url: str, timeout: int = VIDEO_TIMEOUT,
               max_bytes: int = VIDEO_MAX_PAGE_BYTES, referer: str = ""):
    """requests GET 网页，返回 (html, final_url)。失败/非200 → ("", url)。
    超大页面按 max_bytes 截断（迭代读取，不全量进内存）。
    referer 用于里层播放器页跳转（防盗链：Referer=父页）。"""
    try:
        r, _err = _http_get(url, headers=_media_headers(url, referer, purpose="page"),
                            timeout=timeout, stream=True)
        if r is None:
            return "", url
        if r.status_code != 200:
            r.close()
            return "", url
        charset = getattr(r, "encoding", None) or "utf-8"   # curl_cffi Response 无 .encoding，鸭子兼容
        final_url = r.url or url
        parts = []
        size = 0
        for chunk in r.iter_content(chunk_size=VIDEO_CHUNK_SIZE):
            if not chunk:
                continue
            room = max_bytes - size
            if room <= 0:
                break
            parts.append(chunk[:room])
            size += len(parts[-1])
        r.close()
        if not parts:
            return "", final_url
        raw = b"".join(parts)
        for enc in (charset, "utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(enc), final_url
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "ignore"), final_url
    except Exception:
        return "", url


_SAFE_ENTITY = re.compile(
    r"&(amp|lt|gt|quot|apos|nbsp)(;|(?![A-Za-z0-9]))"
    r"|&#(\d+);?"
    r"|&#[xX]([0-9a-fA-F]+);?")
# 只解 XML/数字字符引用与 6 个 HTML4 无分号实体。绝不解 &times/&trade/&copy 等带名实体：
# Python html.unescape 会把无分号片段 &times 解成 ×（U+00D7），而 &timestamp 恰以 &times 开头
# → 整条签名 URL 的 query 被腐蚀 → CDN 判 "No permission"（_normalize 曾因此吃下所有含此类
# query 的 URL；从 JSON/API 取回的流地址本就不是 HTML 转义文本，根本不该做全量 unescape）。
# 命名实体按浏览器属性值规则：仅当带 ';' 或后继非字母数字时才解（&ampersand 保持字面）。
_NAMED_ENT = {"amp": "&", "lt": "<", "gt": ">", "quot": '"',
              "apos": "'", "nbsp": "\xa0"}


def _safe_unescape(s: str) -> str:
    def rep(m):
        if m.group(1):
            return _NAMED_ENT[m.group(1)]
        if m.group(3):
            try:
                return chr(int(m.group(3), 10))
            except Exception:
                return m.group(0)
        try:
            return chr(int(m.group(4), 16))
        except Exception:
            return m.group(0)
    return _SAFE_ENTITY.sub(rep, s)


def _normalize(raw: str, page_url: str):
    """把原始字符串规整为可下载的绝对 http(s) URL。返回 None 表示不可下载。"""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _safe_unescape(s)
    s = s.replace("\\/", "/")            # JS 转义斜杠
    s = s.strip().strip("\"' \t")
    if s.startswith("//"):
        scheme = urlparse(page_url).scheme or "https"
        s = f"{scheme}:{s}"
    low = s.lower()
    if low.startswith(_SKIP_SCHEMES):
        return None
    u = urljoin(page_url, s)
    u = u.split("#", 1)[0]
    if u.lower().startswith(_SKIP_SCHEMES):
        return None
    if not u.lower().startswith(("http://", "https://")):
        return None
    return u


def _classify(media_url: str):
    """返回 (kind, ext) 或 (None, None) 表示无法识别/暂不支持。"""
    path = media_url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return "m3u8", "m3u8"
    if path.endswith(".mpd"):
        return "dash", "mpd"
    if "." in path:
        ext = path.rsplit(".", 1)[1]
        if ext in VIDEO_EXT_DIRECT:
            return "direct", ext
    return None, None


def extract_title(html: str, url: str) -> str:
    """提取页面标题：<title> / og:title / twitter:title → 压空白截 80；兜底域名。"""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = m.group(1).strip()
    if not title:
        for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
            if re.search(r'(?:property|name)\s*=\s*["\'](?:og:title|twitter:title)["\']', tag, re.I):
                cm = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.I | re.S)
                if cm:
                    title = cm.group(1).strip()
                    break
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        host = urlparse(url).netloc
        title = host or "video"
    return title[:80]


# ================================================================
# 播放器字段轻量混淆解码（有界、通用；不做站点私有算法逆向）
# ================================================================
# 开放影视/CMS 站常见把真实流地址包一层可逆编码再放进播放器字段：
#   "url": Base64(...) / 再叠加 percent / JS 转义；encrypt:1 + atob/decodeURIComponent/unescape。
# 这里只做公开、可逆的通用包裹逐层剥离，任一中间结果能规整成 http(s) 媒体 URL 即收。
# 私有 XOR/异或等算法不在此列（需真实源标定），解码失败静默丢弃，不误报。

# 收集播放器字段值：url/file/src/link/v_url/playurl/...（值含 4+ 字符）。
# 键名容忍 JSON 引号（"url": / url: 两种形态，CMS 播放器配置多为前者）。
_RE_PLAYER_FIELD = re.compile(
    r"""(?<![A-Za-z0-9_."'])["']?(?:url|file|src|link|v_url|playurl|play_url|video|mac_url|enc|content)["']?\s*[:=]\s*["']([^"']{4,})["']""",
    re.I)
# 收集常见解码函数实参：atob(...) / decodeURIComponent(...) / unescape(...) 等
_RE_DECODE_CALL = re.compile(
    r"""(?:atob|decodeURIComponent|decodeURI|unescape|Base64\.decode)\s*\(\s*["']([^"']{4,})["']\s*\)""",
    re.I)


def _bounded_decode_payload(raw: str) -> list:
    """对单条负载做有界逐层剥离，返回可能的明文候选（含原串）。"""
    out = [raw]
    cur = raw

    def push(nxt):
        nonlocal cur
        if nxt and nxt != cur and nxt not in out:
            out.append(nxt)
            cur = nxt
            return True
        return False

    # ① HTML 实体
    try:
        push(_safe_unescape(cur))
    except Exception:
        pass
    # ② 多重 percent / JS %uXXXX（unescape 语义）解码，最多 3 轮
    for _ in range(3):
        changed = False
        nxt = re.sub(r"%u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), cur)
        if nxt != cur:
            push(nxt)
            changed = True
        try:
            nxt2 = unquote(cur)
            if nxt2 != cur:
                push(nxt2)
                changed = True
        except Exception:
            pass
        if not changed:
            break
    # ③ base64（含 url-safe、去空白；要求可解码出可见字符串）
    for cand in list(out):
        b = cand
        if b.startswith("base64,"):
            b = b[len("base64,"):]
        cleaned = "".join(b.split())
        if len(cleaned) < 4:
            continue
        # 尝试多种 padding/变体
        tried = [cleaned]
        if "=" not in cleaned:
            tried.append(cleaned + "=" * (-len(cleaned) % 4))
        tried.append(cleaned.replace("-", "+").replace("_", "/"))
        for t in tried:
            try:
                dec = base64.b64decode(t, validate=True).decode("utf-8", "ignore")
            except Exception:
                continue
            if dec and len(dec) >= 4 and dec not in out:
                out.append(dec)
    return out


def _decode_obfuscated_urls(html: str, page_url: str) -> list:
    """扫描播放器字段/解码函数负载，剥离可逆包裹，返回 (绝对URL, ext) 列表。
    只收能经 _normalize 规整、_classify 识别为媒体的明文结果。"""
    found = []
    seen = set()
    payloads = []
    for m in _RE_PLAYER_FIELD.finditer(html):
        payloads.append(m.group(1))
    for m in _RE_DECODE_CALL.finditer(html):
        payloads.append(m.group(1))
    for p in payloads:
        for cand in _bounded_decode_payload(p):
            norm = _normalize(cand, page_url)
            if not norm:
                continue
            key = _canon_key(norm)
            if key in seen:
                continue
            seen.add(key)
            kind, ext = _classify(norm)
            if kind is not None:
                found.append((norm, ext))
    # G1 站点 deobf 槽（适配器军备包）：通用剥离层不认识的站点私货混淆，交给注册的
    # fn(payload)->list[str] 现算；其输出仍过一次有界剥离（可能再套一层通用编码），
    # 再走同一条规整/分类门槛。异常/非 callable 一律静默（机制位，缺省零行为变化）。
    deobf = _adapter_get(page_url, "deobf")
    if callable(deobf):
        extra = []
        try:
            for p in payloads:
                r = deobf(p)
                if isinstance(r, str):
                    extra.append(r)
                elif r:
                    extra.extend(str(x) for x in r if isinstance(x, str))
            for r in list(extra):
                for sub in _bounded_decode_payload(r):
                    if sub not in extra:
                        extra.append(sub)
        except Exception:
            extra = []
        for cand in extra:
            norm = _normalize(cand, page_url)
            if not norm:
                continue
            kk = _canon_key(norm)
            if kk in seen:
                continue
            seen.add(kk)
            kind, ext = _classify(norm)
            if kind is not None:
                found.append((norm, ext))
    return found


def extract_video_candidates_from_html(html: str, page_url: str):
    """静态解析网页 HTML，返回 (candidates, notes)。candidates 已按 URL 去重。"""
    cands: list = []
    notes: list = []
    seen = set()

    def add(u, src, quiet=False):
        norm = _normalize(u, page_url)
        if norm is None:
            low = str(u).lower()
            if low.startswith(_SKIP_SCHEMES):
                notes.append(f"{src}: 检测到 {low.split(':', 1)[0]}: 视频，无独立文件地址，无法直接下载")
            return
        key = _canon_key(norm)
        if key in seen:
            return
        seen.add(key)
        kind, ext = _classify(norm)
        if kind is None:
            return  # 非视频格式（图片/css/js 等）静默忽略
        cands.append(VideoCandidate(index=len(cands) + 1, kind=kind, url=norm,
                                    ext=ext, src=src, page_url=page_url,
                                    title_hint=ext if ext not in ("m3u8", "ts") else "hls"))

    if not html:
        return cands, notes

    # 1) <video>/<source> 标签的 src / data-src / data-url
    attr_re = re.compile(
        r"<(video|source)\b[^>]*?\b(src|data-src|data-url)\s*=\s*([\"'])(.*?)\3",
        re.I | re.S)
    for m in attr_re.finditer(html):
        val = m.group(4)
        if val.strip():
            add(_safe_unescape(val), "video/source标签")

    # 2) JS 字符串字面量中的 m3u8 / DASH(.mpd) / 直链（兼容 \\/ 转义斜杠）
    for m in re.finditer(VIDEO_RE_M3U8, html):
        add(m.group(1), "JS字面量-m3u8")
    for m in re.finditer(RE_MPD_URL, html):
        add(m.group(1), "JS字面量-mpd")
    for m in re.finditer(VIDEO_RE_DIRECT, html):
        add(m.group(1), "JS字面量-直链")

    # 3) 播放器配置字段 url:/file:/src:（宽松匹配；容忍 JSON 引号键名；
    #    只收含媒体扩展名的值，避免图片噪音）
    cfg_re = re.compile(
        r"""(?<![A-Za-z0-9_."'])["']?(?:url|file|src|playurl|play_url|video)["']?\s*[:=]\s*["']([^"']{4,})["']""",
        re.I)
    ext_in_val = re.compile(r"\.(?:m3u8|mp4|mpd|flv|webm|mov|m4v)(?=[?#\"'\s\\]|$)", re.I)
    for m in cfg_re.finditer(html):
        val = m.group(1)
        if ext_in_val.search(val):
            add(val, "播放器配置", quiet=True)

    # 4) 纯文本裸露 URL（未带引号包裹的 .m3u8/.mpd 结尾串）
    bare_re = re.compile(r"(https?://[^\s\"'<>]+?\.m3u8[^\s\"'<>]*)", re.I)
    for m in bare_re.finditer(html):
        add(m.group(1), "裸URL")
    for m in re.finditer(r"(https?://[^\s\"'<>]+?\.mpd[^\s\"'<>]*)", html, re.I):
        add(m.group(1), "裸URL-mpd")

    # 5) 播放器字段轻量混淆解码（base64/percent/unescape 等可逆包裹，只收能规整成媒体 URL 者）
    for dec_url, _ext in _decode_obfuscated_urls(html, page_url):
        add(dec_url, "播放器解码")

    # 6) og:video / twitter:player:stream 等社交卡片 meta（部分站靠 meta 头直给媒体地址）
    _OG_VIDEO_KEY = re.compile(
        r'(?:property|name|itemprop)\s*=\s*["\'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["\']',
        re.I)
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        if not _OG_VIDEO_KEY.search(m.group(0)):
            continue
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if cm and cm.group(1).strip():
            add(_safe_unescape(cm.group(1)).strip(), "og:video meta")

    # 7) JSON-LD VideoObject（contentUrl/embedUrl/url/playUrl…）；解析失败静默，靠 _classify 滤非媒体
    _LD_KEYS = {"contenturl", "contentstreamurl", "embedurl", "playurl",
                "streamurl", "m3u8", "mpd"}
    for m in re.finditer(
            r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and v.strip():
                        if k.strip().lower() in _LD_KEYS or (v.strip().lower().startswith("http")
                                                             and ".m3u8" in v.lower()):
                            add(_safe_unescape(v).strip(), "JSON-LD")
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)

    # 8) <meta itemprop=contentUrl> / <link itemprop=embedURL>
    for m in re.finditer(r'<meta\b[^>]*\bitemprop\s*=\s*["\']contentUrl["\'][^>]*>', html, re.I):
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if cm and cm.group(1).strip():
            add(_safe_unescape(cm.group(1)).strip(), "meta contentUrl")
    for m in re.finditer(r'<link\b[^>]*\bitemprop\s*=\s*["\']embedURL["\'][^>]*>', html, re.I):
        hm = re.search(r'href\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if hm and hm.group(1).strip():
            add(_safe_unescape(hm.group(1)).strip(), "link embedURL")

    return cands, notes


# ================================================================
# 全站覆盖：站点/CMS 适配注册表 + 通用深挖解析器
# 通用扫描器只认"当前页已知媒体形态"。不少站的详情/播放页只嵌播放器
# iframe，真流在里层页；还有各 CMS 独有的取流字段。这里的注册表让新站
# 以 register_site_adapter() 加一条规则（不动核心），深挖解析器则负责
# 跨 iframe/refresh 有界跳转 + 多信号合并去重，覆盖任意静态可解析页。
# 边界：只静态跟随，blob/MSE 或需登录/DRM 的页自然落空（走浏览器兜底/归因）。
# ================================================================

# host 后缀 → {"fn": finder(html,page_url), "serial": bool}（裸候选 URL，交给核心规整/分类）
# serial=True：该站点需"逐页串行"抓取——多集页并发/浏览器多开会触发掐连/封禁，
# 整部抓取据此强制单集串行、不启用浏览器（sorani 等 SPA+签名 API 站即此型）。
_SITE_ADAPTERS = {}
_SITE_ADAPTERS_LOCK = threading.Lock()


def register_site_adapter(host_suffix: str, finder=None, *, serial: bool = False,
                          signer=None, deobf=None, player_extractor=None,
                          imp_slot=None) -> None:
    """注册站点/CMS 适配器（一站完整军备包，roadmap §5.8）。当页面 host 以 host_suffix
    结尾时调用 finder(html, page_url) 取裸媒体 URL/分集。后缀最长匹配、可覆盖既有。
    serial=True 声明该站不能多页并发（见上注释）。供外部模块在 import 后注册；结果与
    通用扫描合并去重。旧形态 register_site_adapter(host, fn) 兼容。
    附加槽（全部可选，把“这一站的特异处理”收进同一条注册）：
      signer            #6 下载层403 → 站点签名重签 fn(url,*,referer,status,resp_meta)->(url,headers)|None
                         → 自动 register_signer：裸域+子域两键都挂（signer 表需显式 "." 才子域命中），
                           403 螺旋直接可用
      deobf            G1 简单混淆解码 fn(payload)->list[str]，命中 host 时对播放器字段先跑
      player_extractor G2 浏览器上下文取 window.player_data 族 fn(driver, url)->list[str]|dict
      imp_slot         该站指纹档 key（写进 config.STEALTH_SITE_PROFILE，页面+下载同档）
    语义：finder 非 None = 整条注册（覆盖既有记录并清掉未再提供的旧槽，防 stale）；
         finder None    = 只补槽（给既有适配器挂 signer/deobf/…，不动 fn）。"""
    hs = (host_suffix or "").strip().lower().lstrip("*.")
    if not hs:
        raise ValueError("host_suffix 不能为空")
    with _SITE_ADAPTERS_LOCK:
        if finder is not None:
            rec = {"fn": finder, "serial": bool(serial)}
        else:
            rec = _SITE_ADAPTERS.get(hs) or {}
        if deobf is not None:
            rec["deobf"] = deobf
        if player_extractor is not None:
            rec["player_extractor"] = player_extractor
        if imp_slot:
            rec["imp_slot"] = imp_slot
        _SITE_ADAPTERS[hs] = rec
    # 副作用槽：signer → 全局 signer 注册表（裸域精确 + ".x" 子域两键）；imp_slot → 站档覆盖
    if signer is not None:
        register_signer(hs, signer)
        if not hs.startswith("."):
            register_signer("." + hs, signer)
    if imp_slot:
        try:
            sp = getattr(_CFG, "STEALTH_SITE_PROFILE", None)
            if sp is None:
                sp = {}
                try:
                    setattr(_CFG, "STEALTH_SITE_PROFILE", sp)
                except Exception:
                    sp = None
            if sp is not None and hs not in sp:
                sp[hs] = imp_slot
        except Exception:
            pass


def _adapter_get(host: str, slot: str, default=None):
    """取命中 host 的适配器某槽（signer/deobf/player_extractor/fn/serial…）；无适配器 → default。"""
    key = _match_adapter_suffix(host)
    if not key:
        return default
    rec = _SITE_ADAPTERS.get(key) or {}
    return rec.get(slot, default)


def _netloc_of(host_or_url: str) -> str:
    """把小写 host/netloc 或完整 URL 归一成纯 host（剥 scheme/userinfo/端口/尾点）。
    供适配器后缀匹配与指纹 host 匹配共用——传整 URL 也不会退化成 scheme（曾致串行护栏死代码）。"""
    s = (host_or_url or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        s = urlparse(s).netloc
    if "@" in s:
        s = s.rsplit("@", 1)[1]        # 去 userinfo（本项目不涉及但防御）
    if ":" in s:
        s = s.split(":", 1)[0]         # 去端口
    return s.rstrip(".")


def _match_adapter_suffix(host: str):
    """返回命中 host 的最长后缀适配器 key；无命中返回 None。接受裸 host 或完整 URL。"""
    host = _netloc_of(host)
    if not host:
        return None
    keys = sorted((k for k in _SITE_ADAPTERS if host == k or host.endswith("." + k)),
                  key=len, reverse=True)
    return keys[0] if keys else None


def _site_adapter_serial(host: str) -> bool:
    """命中该 host 的适配器是否声明 serial（需逐页串行 + 不开浏览器）。"""
    key = _match_adapter_suffix(host)
    return bool(key and _SITE_ADAPTERS.get(key, {}).get("serial"))


def _site_result(out) -> dict:
    """把适配器返回值规整成 {"candidates": [裸URL], "episodes": [分集]}。
    兼容两种形态：list/tuple=媒体 URL 列表；dict 可含 candidates/urls 与 episodes。"""
    d = {"candidates": [], "episodes": []}
    if isinstance(out, dict):
        c = out.get("candidates") or out.get("urls") or []
        e = out.get("episodes") or []
    elif isinstance(out, (list, tuple)):
        c, e = out, []
    else:
        return d
    for x in c or []:
        s = str(x).strip()
        if s:
            d["candidates"].append(s)
    for x in e or []:
        if isinstance(x, dict) and x.get("url"):
            d["episodes"].append({
                "num": x.get("num"), "label": str(x.get("label") or ""),
                "url": str(x["url"]).strip(), "line": x.get("line")})
    return d


def _apply_adapters(host: str, html: str, page_url: str) -> dict:
    """按最长 host 后缀命中适配器，返回 {"candidates": [...], "episodes": [...]}。
    host 小写、去端口。适配器异常/无命中 → 空结构（调用方走既有归因/兜底）。"""
    key = _match_adapter_suffix(host)
    if not key:
        return {"candidates": [], "episodes": []}
    try:
        return _site_result(_SITE_ADAPTERS[key]["fn"](html, page_url))
    except Exception:
        return {"candidates": [], "episodes": []}


# 播放器/取流 iframe 提示词：命中则优先跟随该 iframe（避免跟无关广告框）
_PLAYER_IFRAME_HINT = re.compile(
    r"player|play|vod|video|jplayer|dplayer|ckplayer|artplayer|xgplayer|hls|m3u8|mpd|url=|\?url",
    re.I)


def _hop_target(html: str, page_url: str):
    """找本页值得跟随的下一跳：meta refresh 优先，其次"播放器感" iframe/frame，
    最后才任意 iframe。找不到返回 None。"""
    for m in re.finditer(r'<meta\b[^>]*http-equiv\s*=\s*["\']?refresh["\']?[^>]*>',
                         html, re.I):
        cm = re.search(r'url\s*=\s*["\']?([^"\'>\s]+)', m.group(0), re.I)
        if cm:
            # _normalize 内部已做安全解码（&amp;→&，绝不动 &times= 等），此处直接喂原始值
            u = _normalize(cm.group(1), page_url)
            if u:
                return u
    hinted = None
    fallback = None
    for m in re.finditer(r"<(?:iframe|frame)\b[^>]*>", html, re.I):
        sm = re.search(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                       m.group(0), re.I)
        if not sm:
            continue
        src = next((g for g in sm.groups() if g), "")
        if not src:
            continue
        low = src.strip().lower()
        if low.startswith(("data:", "blob:", "javascript:", "#")):
            continue
        u = _normalize(src, page_url)
        if not u:
            continue
        if _PLAYER_IFRAME_HINT.search(src):
            return u                        # 播放器感 iframe：最可能一步到位
        if hinted is None and _PLAYER_IFRAME_HINT.search(u):
            hinted = u
        if fallback is None:
            fallback = u
    return hinted or fallback


def _resolve_candidates(url: str, *, html0=None, final0=None,
                        log=None, stop_flag=None) -> list:
    """通用深挖解析器：任意 URL → 媒体候选（VideoCandidate 列表）。
    逐页：①host 适配器 ②通用扫描器；本页无候选且 depth < VIDEO_RESOLVE_MAX_HOPS
    时静态跟随播放器 iframe/refresh 再探。visited 去重防环；每候选的 page_url
    记为其实际所在页 final（下载 Referer 正确）。html0/final0 供已抓过首页的
    调用方透传，避免重复 GET。永不 raise。"""
    max_hops = max(0, int(VIDEO_RESOLVE_MAX_HOPS))
    visited = set()
    by_key = {}

    def _note(m):
        if log:
            try:
                log(str(m))
            except Exception:
                pass

    def _record(cands):
        for c in cands:
            by_key.setdefault(_canon_key(c.url), c)

    def _record_raw(raws, page_final):
        for raw in raws:
            u = _normalize(raw, page_final)
            if not u:
                continue
            key = _canon_key(u)
            if key in by_key:
                continue
            kind, ext = _classify(u)
            if kind is None:
                continue
            by_key[key] = VideoCandidate(index=0, kind=kind, url=u, ext=ext,
                                         src="站点适配", page_url=page_final,
                                         title_hint=ext if ext not in ("m3u8", "ts") else "hls")

    def walk(u, html, final, depth):
        if stop_flag and stop_flag():
            return
        k = _canon_key(u)
        if k in visited:
            return
        visited.add(k)
        if not html:
            return
        host = (urlparse(final or u).netloc or "").lower()
        _record_raw(_apply_adapters(host, html, final or u).get("candidates", []),
                    final or u)
        page_cands, notes = extract_video_candidates_from_html(html, final or u)
        for n in notes:
            _note(n)
        if page_cands:
            _record(page_cands)
        if by_key or depth >= max_hops:
            return
        tgt = _hop_target(html, final or u)
        if not tgt:
            return
        if _canon_key(tgt) in visited:          # 环/回头路：不重复取页
            return
        h2, f2 = fetch_html(tgt, referer=final or u)
        if h2:
            walk(tgt, h2, f2 or tgt, depth + 1)

    if html0:
        walk(url, html0, final0 or url, 0)
    else:
        h, f = fetch_html(url)
        walk(url, h, f or url, 0)
    cands = list(by_key.values())
    for i, c in enumerate(cands, start=1):
        c.index = i
    return cands


# ================================================================
# sorani(青空次元) 站点适配：React SPA，流在签名 JSON API 里（经注册表接入，不动核心）
# 索引页/播放页 SSR 内嵌 JS 对象字面量：videoId/episodeOrders/episodeRouteKey/
# episodeId/selectedLineCode(playLines)。取流只走公开 play 接口（内容免费、无 DRM）：
#   GET https://api.sorani.cc/sorani-cms/api/video/episode/{episodeId}/play?lineCode={code}
#     → data.playUrl（sorani-vids.xyz 的签名 m3u8；TS+AES-128，key 由清单给出）
# 整部没有 <a> 分集锚点族 → 用 episodeOrders 合成 /anime/mal/{videoId}/episode/NN。
# 解析失败/限流返回空 → 既有归因或浏览器兜底接管。
# ================================================================
_SORANI_RE_EPID = re.compile(r"episodeId\s*[:=]\s*(\d{1,12})")
_SORANI_RE_VID = re.compile(r"videoId\s*[:=]\s*(\d{1,12})")
_SORANI_RE_ORDERS = re.compile(r"episodeOrders\s*[:=]\s*\[([^\]]*)\]")
_SORANI_RE_ROUTEKEY = re.compile(r"episodeRouteKey\s*[:=]\s*\"([^\"]+)\"")
_SORANI_RE_LINE = re.compile(r"selectedLineCode\s*[:=]\s*\"([A-Za-z0-9_]+)\"")
_SORANI_PLAY_API = ("https://api.sorani.cc/sorani-cms/api/video/episode/"
                    "{ep}/play?lineCode={code}")
_SORANI_API_LOCK = threading.Lock()   # play-API 全局节拍锁（串行/并发下都只放行 ≥ MIN_INTERVAL 间隔）
_SORANI_API_LAST = 0.0


def _sorani_api_throttle() -> None:
    """play-API 最小间隔节拍：相邻两次 play GET（跨集/跨线程）至少隔
    VIDEO_SORANI_API_MIN_INTERVAL 秒——真 Chrome 身份下也别 burst，防掐连/封禁。"""
    global _SORANI_API_LAST
    interval = float(getattr(_CFG, "VIDEO_SORANI_API_MIN_INTERVAL", 2.0) or 0.0)
    with _SORANI_API_LOCK:                     # 持锁 sleep：并发调用自然排队
        now = time.time()
        need = _SORANI_API_LAST + interval - now
        if need > 0:
            time.sleep(need)
            _SORANI_API_LAST = time.time()
        else:
            _SORANI_API_LAST = now


def _sorani_episodes(html: str, page_url: str, res: dict) -> None:
    """索引/播放页 SSR 里的 episodeOrders → 合成整部 URL（无 <a> 锚点也可枚举）。"""
    mv = _SORANI_RE_VID.search(html)
    mo = _SORANI_RE_ORDERS.search(html)
    if not (mv and mo):
        return
    try:
        nums = sorted({int(x) for x in re.findall(r"\d{1,6}", mo.group(1))})
    except Exception:
        return
    if not nums:
        return
    mk = _SORANI_RE_ROUTEKEY.search(html)
    width = min(4, max(1, len(mk.group(1)) if mk else 2))
    p = urlparse(page_url)
    base = f"{p.scheme}://{p.netloc}/anime/mal/{mv.group(1)}/episode/"
    for n in nums:
        z = f"{n:0{width}d}"
        res["episodes"].append({"num": n, "label": f"第{z}集",
                                "url": base + z, "line": None})


def _impersonate_chrome_json(api: str, referer: str, timeout: int = 20) -> str:
    """curl_cffi 不可用时 play-API 的单实例 Chrome 兜底：带 Referer/Origin 头直开 JSON，
    取 <body> 文本解析 → data.playUrl。驱动池取用、本函数内归还（逐集单实例，不并发多开）。
    任何失败返回 ""（不 raise），交由既有归因/浏览器兜底提示。"""
    try:
        import qiongqi_maw
    except Exception:
        return ""
    driver = None
    try:
        driver = _acquire_browser(referer)   # 档位按播放页 host 决定：兜底 Chrome 与下载同档
    except Exception:
        return ""
    try:
        # 防盗链：Referer=播放页；同源 Origin 一并补（站方按页来源校验）。
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            p = urlparse(referer)
            hdrs = {"Referer": referer}
            if p.scheme and p.netloc:
                hdrs["Origin"] = f"{p.scheme}://{p.netloc}"
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": hdrs})
        except Exception:
            pass
        driver.set_page_load_timeout(max(5, int(timeout)))
        driver.get(api)
        text = ""
        for _ in range(12):                      # ≤~6s 等 <body> 出 JSON 文本
            text = _safe_exec(driver, "return document.body ? document.body.innerText : '';")
            if isinstance(text, str) and text.strip():
                break
            time.sleep(0.5)
        if not text:
            return ""
        d = json.loads(text)                     # 直开 JSON：body.innerText == 原文本
        return str((d or {}).get("data", {}).get("playUrl") or "").strip()
    except Exception:
        return ""
    finally:
        try:
            _release_browser(driver)
        except Exception:
            pass


def _sorani_stream(html: str, page_url: str, res: dict) -> None:
    """播放页：episodeId + selectedLineCode → play API → 签名 m3u8 裸 URL。
    先节拍（防 burst）；state=="chrome"（curl_cffi 不可用）→ 单实例 Chrome 兜底直取；
    其余（""/curl）→ 三重试经 _http_get——curl_cffi 就绪时 _http_get 自动转指纹层。"""
    if "/episode/" not in urlparse(page_url).path:
        return
    me = _SORANI_RE_EPID.search(html)
    if not me:
        return
    ml = _SORANI_RE_LINE.search(html)
    code = ml.group(1) if ml else "anime_jp_m3u8"
    from urllib.parse import quote
    api = _SORANI_PLAY_API.format(ep=me.group(1), code=quote(code, safe=""))
    _sorani_api_throttle()                        # 相邻 play-API GET ≥ MIN_INTERVAL，防 burst 掐连
    if _imp_state_for(api) == "chrome":
        pu = _impersonate_chrome_json(api, page_url)
        if pu:
            _imp_mark_grant()
            res["candidates"].append(pu)
        return
    # 两态失败分开处理：连接级失败（掐坏连接，短退避）；200 但 canPlay=false（burst 限流，
    # 真 Chrome 身份下也会被间歇拒发 playUrl）→ 长退避重试，等待令牌桶回填。
    for i in range(6):
        if i:
            _sorani_api_throttle()            # 每轮重试前保持节拍（相邻 GET ≥ MIN_INTERVAL）
        try:
            r, err = _http_get(api, headers=_media_headers(api, page_url, purpose="api"),
                               timeout=20)
        except Exception as e:
            r, err = None, e
        if r is None:
            try:
                _drop_host_conns(_http_session(), api)   # requests 侧：掐掉被 CDN 掐死的复用连接
            except Exception:
                pass
            time.sleep(1.5 + i * 1.0)
            continue
        try:
            d = json.loads(r.content.decode("utf-8", "ignore")) or {}
            data = d.get("data") or {}
            pu = str(data.get("playUrl") or "").strip()
        except Exception:
            data, pu = {}, ""
        if pu:
            _imp_mark_grant()
            res["candidates"].append(pu)
            return
        if not data.get("canPlay", True):
            # burst 限流 / 源暂不可播：长退避后重试（消息≈"请勿频繁请求，稍后再试"）
            time.sleep(3.0 + i * 2.0)
            continue
        return                                  # 200 且可播但无 playUrl → 结构变化，不空转


def _sorani_adapter(html: str, page_url: str) -> dict:
    """host 以 sorani.net 结尾。索引页→整部分集；播放页→play API 签名 m3u8。"""
    res = {"candidates": [], "episodes": []}
    if html:
        _sorani_episodes(html, page_url, res)
        _sorani_stream(html, page_url, res)
    return res


register_site_adapter("sorani.net", _sorani_adapter, serial=True)
# play API host 与叶子 CDN 都走浏览器级 TLS 指纹（实测 requests 与 curl.exe schannel 均被掐连，
# 真 Chrome 不受影响）。叶子初版未预置，2026-09-06 真机量到 manifest/分片全被拒后按计划补注册。
register_impersonate_host("api.sorani.cc")
register_impersonate_host("sorani-vids.xyz")


# ================================================================
# 浏览器兜底探测（可选；需要外部传入 qiongqi_maw 的 driver）
# ================================================================

# 采集脚本：值必须经顶层 `return` 返回 —— CDP 直驱以"函数体"求值，裸 IIFE/裸表达式的
# completion value 会被丢弃（2026-09-08 E2E 实测：bare `1+2;`→None、bare IIFE→None）。
_COLLECT_JS = r"""
return (function(){
    var out = [];
    document.querySelectorAll('video,audio,source').forEach(function(v){
        try{
            if (v.currentSrc && v.currentSrc.indexOf('blob:') !== 0) out.push(v.currentSrc);
            if (v.src && v.src.indexOf('blob:') !== 0 && v.src.indexOf('data:') !== 0) out.push(v.src);
        }catch(e){}
    });
    try{
        performance.getEntriesByType('resource').forEach(function(e){
            if (/\.(m3u8|mpd|mp4|flv|webm|mov|m4v|mp3|m4a|aac|wav|flac|ogg)(\?|$)/i.test(e.name)) out.push(e.name);
        });
    }catch(e){}
    var scanned = {};
    var scannedCount = 0;
    function scan(o, d){
        if (d > 3 || !o || o === window.location || scannedCount > 4000) return;
        try{
            for (var k in o){
                if (typeof k !== 'string' || k.indexOf('_') === 0) continue;
                var v;
                try { v = o[k]; } catch(e){ continue; }
                if (scanned[k]) continue;
                scanned[k] = true; scannedCount++;
                if (typeof v === 'string' && v.indexOf('http') !== -1 && (
                  v.indexOf('m3u8') !== -1 || v.indexOf('.mpd') !== -1
                  || /\.(mp4|flv|webm|mov|m4v|mp3|m4a|aac|wav|flac|ogg)(\?|$)/i.test(v)
                  )) out.push(v);
                else if (typeof v === 'object') scan(v, d + 1);
            }
        }catch(e){}
    }
    try { scan(window, 0); } catch(e){}
    return out;
})()
"""


# 尝试触发播放：静音并 play 所有 video；命中常见播放按钮则点击一次
_CLICK_PLAY_JS = r"""
(function(){
    var clicked = false;
    document.querySelectorAll('video,audio').forEach(function(v){
        try { v.muted = true; v.play(); } catch(e){}
    });
    var sels = [
        '.play-button','.player-play','.btn-play','.play-icon','[class*="play-btn"]',
        '.vjs-big-play-button','.jw-icon-display','.preroll','.start-button',
        '[class*="player"] [class*="play"]'
    ];
    for (var i = 0; i < sels.length; i++){
        var el = document.querySelector(sels[i]);
        if (el){
            try { el.click(); clicked = true; } catch(e){}
            if (clicked) break;
        }
    }
    var v = document.querySelector('video');
    if (v){ try { v.scrollIntoView({block:'center'}); } catch(e){} }
    return clicked;
})()
"""


def _safe_cur_url(driver) -> str:
    try:
        return driver.current_url or ""
    except Exception:
        return ""


def _safe_exec(driver, js, *args):
    try:
        return driver.execute_script(js, *args)
    except Exception:
        return None


def _driver_cookies_for(driver, target_host: str) -> dict:
    """取与 target_host 同域/子域匹配的浏览器 Cookie，转成 {name: value}（防盗链/登录态）。"""
    if not target_host:
        return {}
    try:
        host = (target_host.split(":")[0]).lower()
    except Exception:
        host = target_host.lower()
    out = {}
    try:
        for c in driver.get_cookies() or []:
            name = c.get("name")
            value = c.get("value")
            domain = str(c.get("domain") or "").lstrip(".").lower()
            if not name:
                continue
            if domain and (host == domain or host.endswith("." + domain)
                           or domain.endswith("." + host) or domain in host):
                out[name] = value
        return out
    except Exception:
        return {}


def detect_videos_browser(url: str, driver, wait_sec: int = 8, *, classify_fn=None,
                          challenge_rounds: int = 2, manual_verify: bool = False):
    """浏览器抓流：解除媒体拦截 → 打开页面 → 自动播放/点击播放 → 轮询资源。
    静态无果时自动沿 <iframe> 追踪进入播放器页（最多 3 层），为命中候选捕获
    同域 Cookie 供 requests 下载时携带。返回 (candidates, notes)。
    challenge_rounds/manual_verify：0 候选且落页为 cookie 型 JS 挑战(非滑块/验证码)时的
    有界自动通过轮数与超限人工兜底开关 —— 全默认即旧行为(仅 CHALLENGE 注记)。
    classify_fn: (url)->(kind,ext)，默认模块 _classify（视频白名单）；audio 侧可注入
    audio_grab._classify_audio 以收集音频候选。落页疑似人机验证时 notes 追加 CHALLENGE:<name>。"""
    cands: list = []
    notes: list = []
    visited_urls = set()
    ident = None          # L0 任务身份（浏览器捕获，未声明字段：不进 asdict/JSON）
    try:
        # 关键：解除 CDP 媒体资源拦截（BLOCK_RESOURCE_EXTS 含 *.mp4/*.flv/*.webm）
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": []})
    except Exception:
        pass

    def snapshot(base_page, raw_items, label):
        host = urlparse(base_page).netloc
        ck = _driver_cookies_for(driver, host)
        for raw in dict.fromkeys(raw_items):
            norm = _normalize(raw, base_page)
            if norm is None:
                continue
            key = _canon_key(norm)
            if key in visited_urls:
                continue
            visited_urls.add(key)
            classify = classify_fn or _classify
            kind, ext = classify(norm)
            if kind is None:
                continue
            cand = VideoCandidate(index=len(cands) + 1, kind=kind, url=norm,
                                  ext=ext, src=label, page_url=base_page,
                                  cookies=dict(ck), title_hint=ext)
            if ident is not None:
                cand.identity = ident     # 随候选带到下载点：下载全程 ambient 同源（L0）
            cands.append(cand)

    def collect_on_current(label):
        base = _safe_cur_url(driver) or url
        found = _safe_exec(driver, _COLLECT_JS)
        raw = [x for x in found if isinstance(x, str)] if found else []
        # G2 站点 player_extractor 槽：命中 host 的适配器声明的"取 window.player_data 族"
        # fn(driver, base_url) 在浏览器上下文现算（可能 driver.execute_script 跑站点 JS / 造 key），
        # 返回 str/list[str] 媒体候选 → 与通用采集合并走同一条 snapshot 门槛。异常/无槽=零变化。
        px = _adapter_get(base, "player_extractor")
        if callable(px):
            try:
                out = px(driver, base)
            except Exception:
                out = None
            if isinstance(out, str):
                raw.append(out)
            elif out:
                raw.extend(str(x) for x in out if isinstance(x, str))
        if raw:
            snapshot(base, raw, label)

    try:
        driver.get(url)
    except Exception as e:
        notes.append(f"浏览器打开失败：{e}")
        return cands, notes
    time.sleep(1.5)
    # L0：捕获该 Chrome 实例身份锚点（UA/语言/时区/平台/屏幕）—— 同一 Chrome 对导航恒定，
    # 首帧即取；读不到/失败返回 None → 候选不带身份，下载走旧随机（静默降级，零回归）。
    if _TI is not None:
        try:
            ident = _TI.capture_from_driver(driver)
        except Exception:
            ident = None

    # 主页面轮询：自动播放/点播 → 多次采样
    rounds = max(1, wait_sec // 2)
    for _ in range(rounds):
        _safe_exec(driver, _CLICK_PLAY_JS)
        time.sleep(1.2)
        collect_on_current("浏览器播放页")
        if cands:
            break

    # 静态/主页面无果 → 沿 iframe 追踪进播放器页（最多 3 层）
    hops = 0
    seen_pages = {_canon_key(_safe_cur_url(driver) or url)}
    while not cands and hops < 3:
        cur = _safe_cur_url(driver) or url
        frames = _safe_exec(
            driver,
            "return Array.from(document.querySelectorAll('iframe')).map(function(f){"
            " return f.src || f.getAttribute('data-src') || ''; });") or []
        next_url = None
        for fu in frames:
            if not isinstance(fu, str) or not fu.strip():
                continue
            resolved = _normalize(fu, cur)
            if resolved and resolved.startswith(("http://", "https://")) \
                    and _canon_key(resolved) not in seen_pages:
                next_url = resolved
                break
        if not next_url:
            notes.append("页面内无更多可进入的 iframe 播放器页")
            break
        seen_pages.add(_canon_key(next_url))
        try:
            driver.get(next_url)
        except Exception as e:
            notes.append(f"进入播放器页失败：{next_url}（{e}）")
            break
        hops += 1
        time.sleep(1.2)
        for _ in range(2):
            _safe_exec(driver, _CLICK_PLAY_JS)
            time.sleep(1.0)
            collect_on_current("浏览器播放器页")
            if cands:
                break

    if not cands:
        _mark_challenge_if_seen(driver, notes)

    # #5 cookie 型 JS 挑战（Cloudflare「Just a moment」/Imperva 等）：0 候选且落页判定为
    # 自动类(非滑块/验证码)时，让真 Chrome 原生跑挑战 → 收割 anti-bot cookie 进本地 jar →
    # 重跑候选收集。有界轮数；人工类/无 qiongqi_maw 一律不进入（维持旧注记行为）。
    if not cands and challenge_rounds > 0:
        tried_auto = 0
        info = _challenge_is_auto(driver)
        while not cands and tried_auto < challenge_rounds and info is not None:
            tried_auto += 1
            try:
                _lazy_qm().handle_challenge(driver, info)
            except Exception:
                pass
            _challenge_host_cookies(driver, url)          # 收割该 host cookie → jar（0 候选也种）
            # 挑战通过/刷新后原样重跑一轮采集（CLICK_PLAY + 主页面 collect）
            for _ in range(2):
                _safe_exec(driver, _CLICK_PLAY_JS)
                time.sleep(1.0)
                collect_on_current("浏览器挑战后重采")
                if cands:
                    break
            info = _challenge_is_auto(driver) if not cands else None

        if not cands and tried_auto > 0:
            # CHALLENGE:<vendor> 已在进入自动轮前注记；这里只补"自动轮耗尽仍 0 候选"
            notes.append("CHALLENGE:auto-exhausted")
            if manual_verify:
                try:
                    _lazy_qm().wait_for_manual_verify(driver)
                    for _ in range(2):
                        _safe_exec(driver, _CLICK_PLAY_JS)
                        time.sleep(1.0)
                        collect_on_current("浏览器人工验证后重采")
                        if cands:
                            break
                except Exception:
                    pass

    return cands, notes


# ---- 浏览器取流探测的通用支撑：人机验证注记 + 本地 Cookie jar + 公开入口 ----

_CHALLENGE_MARKERS = (
    ("cloudflare", re.compile(r"cloudflare|cf-challenge|cf-chl|__cf_chl", re.I)),
    ("hcaptcha", re.compile(r"hcaptcha", re.I)),
    ("recaptcha", re.compile(r"recaptcha|g-recaptcha", re.I)),
    ("geetest", re.compile(r"geetest|\bgt4\b|极验", re.I)),
    ("verify", re.compile(r"captcha|访问验证|安全验证|verify.{0,20}human|please verify|imperva|incapsula", re.I)),
)


def _mark_challenge_if_seen(driver, notes: list) -> None:
    """浏览器兜底落页疑似人机验证时给 CHALLENGE:<name> 注记，供上层把失败归因为"人机验证"
    而非静默空手。只在无候选时调用（不改变返回结构）。"""
    try:
        sample = (_safe_cur_url(driver) + "\n" +
                  str(_safe_exec(driver, "return document.title || '';") or "") + "\n" +
                  str(_safe_exec(driver,
                       "try{return (document.body?document.body.innerText:'').slice(0,2000);}"
                       "catch(e){return '';}") or ""))
    except Exception:
        return
    if not sample:
        return
    low = sample.lower()
    for name, rx in _CHALLENGE_MARKERS:
        if rx.search(low):
            notes.append("CHALLENGE:" + name)
            return


# ---- #5 cookie 型 JS 挑战自动类(媒体探测用)：仅 wait_and_refresh 族；滑块/验证码(人工类)
#      与 cookie_reset(清登录 cookie)/rotate_fingerprint(动页面指纹，越本块职权)一律不自动。 ----
_CHALLENGE_AUTO_STRATEGIES = frozenset({"wait_and_refresh"})

_QM = None            # lazy：媒体路径与 qiongqi_maw 保持解耦，缺载静默降级


def _lazy_qm():
    global _QM
    if _QM is None:
        import qiongqi_maw
        _QM = qiongqi_maw
    return _QM


def _challenge_is_auto(driver) -> dict:
    """当前页面若为『自动类』cookie 型 JS 挑战 → 返回其挑战 info；否则 None。
    判定复用 qiongqi_maw.CHALLENGE_SIGNATURES 的 retry_strategy(单一分类源，不逆向 vendor)；
    qiongqi_maw 缺载/非自动类/unknown → None(媒体探测保持旧行为)。"""
    try:
        qm = _lazy_qm()
        info = qm.detect_challenge_type(driver) or {}
        sig = info.get("signature") or {}
        if info.get("type") and info.get("confidence", 0) > 0 \
                and (sig.get("retry_strategy") in _CHALLENGE_AUTO_STRATEGIES):
            return info
    except Exception:
        pass
    return None


def _challenge_host_cookies(driver, url: str) -> dict:
    """挑战通过后收割该 host 的浏览器 cookie → 并入本地 jar(0 候选也种，
    供后续无浏览器下载带 anti-bot cookie，见 _dl_cookies 兜底)。返回收割到的 ck。"""
    host = _host_of(url)
    if not host:
        return {}
    ck = _driver_cookies_for(driver, host)
    if ck:
        _jar_update({host: ck})
    return ck


_JAR_FILE = os.path.join(_CFG.RUNTIME_DIR, "video_cookie_jar.pkl")


def _jar_load() -> dict:
    try:
        with open(_JAR_FILE, "rb") as f:
            data = pickle.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _jar_update(host_cookies: dict) -> None:
    """把 {host: {name:value}} 并入本地 jar 并落盘（只合并不删既有键）。
    供后续无浏览器下载跨轮复用登录态/防盗链 Cookie；与 qiongqi_maw 的进程内 cookie 分离。"""
    if not host_cookies:
        return
    data = _jar_load()
    changed = False
    for h, ck in host_cookies.items():
        if not h or not isinstance(ck, dict):
            continue
        cur = data.setdefault(h, {})
        for k, v in ck.items():
            if isinstance(v, str) and cur.get(k) != v:
                cur[k] = v
                changed = True
    if not changed:
        return
    try:
        os.makedirs(_CFG.RUNTIME_DIR, exist_ok=True)
        tmp = _JAR_FILE + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(data, f)
        os.replace(tmp, _JAR_FILE)
    except Exception:
        pass


def _jar_cookies_for(url: str) -> dict:
    """从本地 jar 取与 url 同域/子域匹配的 cookie（name->value）。"""
    host = _host_of(url)
    if not host:
        return {}
    out = {}
    for h, ck in _jar_load().items():
        if not isinstance(ck, dict):
            continue
        if host == h or host.endswith("." + h) or h.endswith("." + host) or h in host:
            for k, v in ck.items():
                if isinstance(v, str):
                    out.setdefault(k, v)
    return out


def _dl_cookies(url: str, cookies: dict):
    """防盗链/登录态 cookie 兜底：调用方显式 cookies 优先；为空/None 时回退同域 jar
    （浏览器轮 _jar_update 已存的登录/防盗链 cookie），让无浏览器下载也能带上上一轮的登录态。"""
    if cookies:
        return cookies
    jar = _jar_cookies_for(url)
    return jar or None


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url).netloc or "").split(":", 1)[0].lower()
    except Exception:
        host = ""
    return host


# ================================================================
# #6d 下载层 HTTP 状态螺旋（403/401/406/429/451）
# _HTTPStatus 把「HTTP 状态级失败」与「连接级异常」明确分开——连接异常才 _block_host，
# HTTP 4xx 绝不拉黑 host（否则 403 把 host 全踢去 curl.exe，与 TLS 拦截语义混淆）。
# 恢复编排有限、不递归：一次 _robust_binary/download_direct 只吃一次恢复(换 URL/换档/cookie/退避)，
# 仍未过即人工注记兜底。默认恢复开关开但无 signer/无换档池/非自动挑战 → 逐字节保现状。
# ================================================================
class _HTTPStatus(Exception):
    """下载层可恢复的 HTTP 状态失败。携带 url/code/headers/body_sample 供恢复编排用。
    与 requests 连接级异常不同：绝不当 host 黑名单依据（不 _block_host）。"""

    def __init__(self, url: str, code: int, headers=None,
                 body_sample: bytes = b"", purpose: str = None):
        super().__init__(f"HTTP {int(code or 0)}: {url[:160]}")
        self.url = url
        self.code = int(code or 0)
        self.headers = dict(headers or {})
        self.body_sample = body_sample
        self.purpose = purpose


_DL_CHALLENGE_TABLE = None
_DL_CHALLENGE_TABLE_LOCK = threading.Lock()
_DL_CHAL_COOLDOWN = {}              # host -> 自动 Chrome 挑战冷却到时间
_DL_CHAL_COOLDOWN_LOCK = threading.Lock()


def _dl_challenge_table():
    """正文关键词表：懒抽 qm.CHALLENGE_SIGNATURES（retry_strategy 单一分类源）；qm 缺载回退
    CF/imperva 静态关键词，保证下载层 403 归因不因浏览器依赖缺失而失效。"""
    global _DL_CHALLENGE_TABLE
    if _DL_CHALLENGE_TABLE is None:
        with _DL_CHALLENGE_TABLE_LOCK:
            if _DL_CHALLENGE_TABLE is None:
                rows = []
                try:
                    sigs = _lazy_qm().CHALLENGE_SIGNATURES
                except Exception:
                    sigs = {}
                if sigs:
                    for name, sig in sigs.items():
                        if not isinstance(sig, dict):
                            continue
                        # 注意 qm 关键词既有 tuple 也有 list（recaptcha 等 title_keywords 为 []）；
                        # 不能 `(..) or () + list`（tuple+list 崩整个表），先统一 list 再拼。
                        kw = list(sig.get("title_keywords") or ()) \
                            + list(sig.get("body_keywords") or ())
                        rows.append((str(name).lower(),
                                     str(sig.get("retry_strategy") or "abort").lower(),
                                     [str(k).lower() for k in kw if k]))
                else:
                    rows.append(("cloudflare", "wait_and_refresh",
                                 ["cf-challenge", "just a moment", "cf-browser-verification",
                                  "turnstile", "challenge-platform", "checking your browser"]))
                    rows.append(("imperva", "wait_and_refresh",
                                 ["incapsula", "imperva", "visid_incap"]))
                    rows.append(("generic", "abort",
                                 ["captcha", "验证码", "滑块", "拖动滑块", "g-recaptcha",
                                  "h-captcha", "verify you are human"]))
                _DL_CHALLENGE_TABLE = rows
    return _DL_CHALLENGE_TABLE


def _classify_dl_403(code: int, body_sample) -> str:
    """下载层状态归因（纯函数，离线可测）：
    429→ratelimit；401→auth；(403/406/451)正文命中 qm CHALLENGE_SIGNATURES 关键词 →
    wait_and_refresh 类=challenge_auto（可自动开 Chrome），滑块/人工类=challenge_manual（红线不自动）；
    其余 → hotlink（防盗链/签名失效，交 signer/换档）。"""
    code = int(code or 0)
    if code == 429:
        return "ratelimit"
    if code == 401:
        return "auth"
    if code not in (403, 406, 451):
        return "hotlink"
    try:
        if isinstance(body_sample, (bytes, bytearray)):
            body = body_sample[:16384].decode("utf-8", "ignore")
        else:
            body = (body_sample or "")[:16384]
        body = body.lower()
    except Exception:
        body = ""
    auto = _CHALLENGE_AUTO_STRATEGIES
    hit_auto, hit_human = False, False
    try:
        for _name, strat, kws in _dl_challenge_table():
            if any(k in body for k in kws):
                if strat in auto:
                    hit_auto = True
                else:
                    hit_human = True
    except Exception:
        pass
    if hit_auto:
        return "challenge_auto"
    if hit_human:
        return "challenge_manual"
    return "hotlink"


def _dl_auto_challenge_once(url: str, referer: str) -> dict:
    """下载层 403 归因 CF/imperva(wait_and_refresh) → 自动开一次真 Chrome 跑挑战收 cookie 入 jar。
    返回收割 cookie（调用方并入重试）；滑块/验证码/rotate/cookie_reset/Chrome 缺载/冷却中 → {}。
    同 host 受 VIDEO_DL_CHALLENGE_MIN_INTERVAL 冷却（防每分片都开 Chrome）。异常即 {}。"""
    host = _host_of(url)
    if not host:
        return {}
    try:
        min_int = float(getattr(_CFG, "VIDEO_DL_CHALLENGE_MIN_INTERVAL", 180.0) or 0)
    except (TypeError, ValueError):
        min_int = 180.0
    now = time.time()
    with _DL_CHAL_COOLDOWN_LOCK:
        if _DL_CHAL_COOLDOWN.get(host, 0.0) > now:
            return {}
        _DL_CHAL_COOLDOWN[host] = now + min_int
    driver = None
    try:
        driver = _acquire_browser(url)
        if driver is None:
            return {}
        try:
            cur = (driver.current_url or "")
        except Exception:
            cur = ""
        if url not in cur:                       # 服务端按缺 cookie 返回挑战壳 → 让浏览器去接
            driver.get(url)
            time.sleep(1.0)
        info = _challenge_is_auto(driver)        # 判定 wait_and_refresh 类；滑块/验证码 → None
        if info is None:
            return {}
        _lazy_qm().handle_challenge(driver, info)     # 等待+刷新：真 Chrome 原生跑完挑战
        return _challenge_host_cookies(driver, url) or {}
    except Exception:
        return {}
    finally:
        _release_browser(driver)


def _recover_media_status(orig_url: str, referer: str, cookies: dict,
                          code: int, headers: dict, body, *, purpose: str = None):
    """下载层 403/429 恢复编排（_robust_binary / download_direct 在 _HTTPStatus 上调用一次）。
    顺序：①归因 challenge 且 CHALLENGE_AUTO 且未冷却 → 自动 Chrome 收 jar cookie（仅一次）；
    ②429 → jitter 退避 sleep_sec（调用方执行）；③有 signer 且 RECOVER_SIGNER → 重签包；
    ④RECOVER_SWAP_PROFILE → 换档包（排除 ambient 当前档）；⑤None（给不出方案 → 人工注记）。
    返回 plan dict / None；有限不递归（recovered_once 由调用方守）。"""
    reason = _classify_dl_403(code, body)
    if reason == "challenge_auto":
        if getattr(_CFG, "VIDEO_DL_CHALLENGE_AUTO", False):
            ck = _dl_auto_challenge_once(orig_url, referer)
            if ck:
                merged = dict(cookies or {})
                merged.update(ck)
                return {"kind": "retry", "cookies": merged,
                        "note": f"下载403→自动Chrome过挑战收{len(ck)} cookie"}
    if reason == "ratelimit":
        try:
            cap = max(0.0, float(getattr(_CFG, "VIDEO_DL_429_BACKOFF_MAX", 30.0) or 0))
        except (TypeError, ValueError):
            cap = 30.0
        return {"kind": "sleep", "sleep_sec": round(random.uniform(0.5, cap or 0.5), 3)}
    if reason != "ratelimit" and getattr(_CFG, "VIDEO_DL_RECOVER_SIGNER", True):
        out = _apply_signer(orig_url, referer, int(code or 0),
                            {"headers": dict(headers or {}),
                             "body": (body if isinstance(body, bytes) else b"")[:4096],
                             "purpose": purpose})
        if out:
            new_url, extra = out
            if new_url or extra:
                return {"kind": "sign", "url": new_url,
                        "extra_headers": extra or None, "note": "signer 重签"}
    if reason != "ratelimit" and getattr(_CFG, "VIDEO_DL_RECOVER_SWAP_PROFILE", True):
        excl = None
        if _TI is not None and _SP is not None:
            try:
                a = _TI.ambient()
            except Exception:
                a = None
            if a is not None:
                try:
                    excl = _SP.profile_for_identity(a).key
                except Exception:
                    excl = None
        key = _swap_profile_key(excl)
        if key:
            return {"kind": "swap", "swap_key": key, "note": "换身份档"}
    return None


def _dl_failure_hint(hs: "_HTTPStatus") -> str:
    """最终仍失败的 _HTTPStatus → 人工可执行 hint（UI log 可见，不自动做红线操作）。"""
    reason = _classify_dl_403(hs.code, hs.body_sample)
    if reason == "challenge_manual":
        return f"HTTP {hs.code} 触发滑块/人工验证码（红线不自动），请人工过验后重试"
    if reason == "challenge_auto":
        return (f"HTTP {hs.code} 人机验证(可自动类)仍挡——确认 Chrome 可用，或调大 "
                f"VIDEO_DL_CHALLENGE_MIN_INTERVAL 后重试")
    if reason == "ratelimit":
        return f"限频 {hs.code}：请调大 VIDEO_PACE_* / 等待后再试"
    if reason == "auth":
        return f"HTTP {hs.code} 疑似需登录/授权（jar 无有效登录态），请先在浏览器登录后重试"
    return f"HTTP {hs.code} 防盗链/签名失效——注册 signer(register_signer) 可自动重签"


def detect_media_browser(url: str, *, classify_fn=None, wait_sec: int = 8,
                         use_browser: bool = True, challenge_rounds: int = 2,
                         manual_verify: bool = False):
    """通用浏览器取流探测（公开入口）：真实 Chrome 打开页面 → 自动播放/点击 → 收集媒体候选。
    classify_fn: (url)->(kind,ext)；默认 vg._classify（视频白名单）；audio 传 _classify_audio。
    命中候选的同域 cookie 顺带并入本地 jar（_jar_update），供后续无浏览器下载跨轮复用。
    challenge_rounds/manual_verify：透传给 detect_videos_browser 的 cookie 型 JS 挑战自动通过
    轮数与人工兜底开关（默认=旧行为，仅 CHALLENGE 注记）。
    返回 (cands, notes)。依赖 qiongqi_maw 驱动池，但仅在函数内懒 import（audio 纯后端打包守门）。"""
    if not use_browser:
        return [], []
    try:
        driver = _acquire_browser(url)      # 任务档（站点覆盖/auto）落到页面态 Chrome
        try:
            cands, notes = detect_videos_browser(url, driver, wait_sec,
                                                 classify_fn=classify_fn,
                                                 challenge_rounds=challenge_rounds,
                                                 manual_verify=manual_verify)
            if cands:
                merge = {}
                for c in cands:
                    h = _host_of(getattr(c, "url", "") or "")
                    ck = getattr(c, "cookies", None)
                    if h and ck:
                        merge.setdefault(h, {}).update(ck)
                if merge:
                    _jar_update(merge)
            return cands, notes
        finally:
            _release_browser(driver)
    except Exception as e:
        return [], [f"浏览器探测失败：{e}"]


# ================================================================
# Range 多连接切分（aria2 式：对同一源文件按字节分片并发拉取）
# ================================================================

def _parse_content_range_total(header: str) -> int:
    """从 `Content-Range: bytes 0-0/12345`（或 `*/12345`）解析文件总长；失败返回 -1。"""
    try:
        total_s = (header or "").rsplit("/", 1)[-1].strip()
        if total_s.isdigit():
            return int(total_s)
    except Exception:
        pass
    return -1


def _range_probe(url: str, *, referer: str = "", cookies: dict = None,
                 timeout: int = VIDEO_TIMEOUT):
    """探测目标是否支持 Range 与文件总长：GET `Range: bytes=0-0`。
    返回 (total:int, supports:bool)。supports=True 表示服务端回 206 且给出了总长；
    网络错误/不支持 → (-1, False)，由调用方回退原单流。"""
    try:
        headers = _media_headers(url, referer)
        headers["Range"] = "bytes=0-0"
        r, err = _http_get(url, headers=headers, cookies=cookies, timeout=timeout)
        if r is None:
            return -1, False
        try:
            if r.status_code == 206:
                total = _parse_content_range_total(r.headers.get("Content-Range") or "")
                return (total if total > 0 else -1), (total > 0)
            return -1, False
        finally:
            r.close()
    except Exception:
        return -1, False


def _range_spans(total: int, splits: int) -> list:
    """把 [0, total) 均分为 ≤splits 段（ceil 分块，末段收短）。返回 [(start,end)] 闭区间。"""
    splits = max(1, int(splits))
    total = max(0, int(total))
    if total <= 0:
        return []
    if splits <= 1:
        return [(0, total - 1)]
    chunk = -(-total // splits)          # ceil(total/splits)
    spans = []
    for k in range(splits):
        start = k * chunk
        end = min(total - 1, (k + 1) * chunk - 1)
        if start <= end:
            spans.append((start, end))
    return spans


def _stream_range(url: str, start: int, end: int, fh, *,
                  referer: str = "", cookies: dict = None,
                  timeout: int = VIDEO_TIMEOUT) -> int:
    """在 global→host 闸门内流式拉取闭区间 [start,end] 并写入 fh 当前偏移处。
    返回写入字节数；HTTP 非 206/200、断连、字节不足都 raise（由 worker 重试或回退 curl）。
    注意：连接级异常这里不重试（worker 层已按尝试循环重新打开文件，重试需从 0 清文件）。

    传输选择（修绕指纹分派破绽）：url 命中注册 host 且引擎态 curl → 走 per-thread curl_cffi
    Session（TLS 指纹表现为 Chrome），不再直连 _http_session() 丢指纹；失败断连丢本线程会话，
    worker 下次全新握手。未注册 host → 维持 requests（旧行为逐字节不变）。
    不能改调 _http_get 分派——那会与这里已取的 host 闸门嵌套加锁。"""
    netloc = (urlparse(url).netloc or "").lower()
    host_gate = _host_gate(netloc)
    headers = _media_headers(url, referer, purpose="range")
    headers["Range"] = f"bytes={start}-{end}"
    use_cffi = (_imp_state_for(url) == "curl")
    if use_cffi:
        headers = _imp_clean_headers(headers)    # 交给 curl_cffi 引擎自管 UA/Sec-Fetch/头序
    expect = end - start + 1
    t0 = time.time()
    ok = False
    n = 0
    holder = {}                             # 本次真实路由的代理（健康记账；Range worker 不加 403 螺旋）
    try:
        with _GLOBAL_GATE:
            with host_gate:
                if use_cffi:
                    s = _imp_thread_session()
                    if s is None:
                        raise _rqex.ConnectionError("curl_cffi 引擎不可用")
                    _, proxies = _proxy_apply(url, allow_socks=True)
                    if proxies:
                        holder["proxy"] = proxies["http"]
                    r = s.get(url, headers=headers, cookies=cookies, stream=True,
                              proxies=proxies,
                              timeout=(VIDEO_CONNECT_TIMEOUT, timeout))
                else:
                    _, proxies = _proxy_apply(url, allow_socks=False)
                    if proxies:
                        holder["proxy"] = proxies["http"]
                    r = _http_session().get(url, headers=headers, cookies=cookies,
                                            stream=True, proxies=proxies,
                                            timeout=(VIDEO_CONNECT_TIMEOUT, timeout))
                try:
                    if r.status_code not in (200, 206):
                        code = r.status_code
                        r.close()
                        raise RuntimeError(f"HTTP {code}（Range 请求失败）")
                    for chunk in r.iter_content(chunk_size=VIDEO_CHUNK_SIZE):
                        if not chunk:
                            continue
                        room = expect - n
                        if room <= 0:
                            break
                        fh.write(chunk[:room])
                        n += room if len(chunk) >= room else len(chunk)
                    r.close()
                except Exception:
                    try:
                        r.close()
                    except Exception:
                        pass
                    raise
        if n != expect:
            raise RuntimeError(f"Range 段字节不符：期望 {expect} 实得 {n}")
        ok = True
        return n
    finally:
        if holder.get("proxy"):
            _proxy_mark(url, holder["proxy"], ok)
        host_gate.record(ok, time.time() - t0, n if ok else 0)
        if ok:
            _tally_bytes(n)
            _health_mark(netloc, True)
        else:
            if use_cffi:
                _imp_drop_local()      # 掐掉的 stale curl 连接必须丢，worker 下次全新握手
            _health_mark(netloc, False)


def _curl_range_file(url: str, dest: str, start: int, end: int,
                     referer: str = "", cookies: dict = None,
                     timeout: int = VIDEO_TIMEOUT) -> int:
    """curl.exe 拉取单段 Range 到 dest（requests 被 CDN 指纹拦时的回退）。成功返回字节数。"""
    exe = _locate_curl()
    if not exe:
        return 0
    ua, al, extra = _curl_profile_args("range", url, referer)
    cmd = [exe, "-sS", "-L", "--fail", "--max-time", str(timeout),
           "-A", ua, "-H", "Accept: */*",
           "-H", f"Range: bytes={start}-{end}",
           "-H", f"Referer: {referer or url}"]
    cmd += extra                        # Sec-CH-UA / Sec-Fetch-*（同档派生，与 UA 自洽）
    if al:
        cmd += ["-H", f"Accept-Language: {al}"]
    ck = _curl_cookie_arg(cookies)
    if ck:
        cmd += ["-H", f"Cookie: {ck}"]
    prox_url = _proxy_pick(url)             # curl 原生支持 socks5/http(s)
    if prox_url:
        cmd += ["--proxy", prox_url]
    cmd += ["-o", dest, url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except Exception:
        if prox_url:
            _proxy_mark(url, prox_url, False)
        _remove_if(dest)
        return 0
    if p.returncode != 0:
        if prox_url and _curl_proxy_fail(p.stderr or b""):
            _proxy_mark(url, prox_url, False)
        _remove_if(dest)
        return 0
    try:
        return os.path.getsize(dest)
    except OSError:
        _remove_if(dest)
        return 0


def _fetch_one_range(url: str, k: int, span, fpath: str, *,
                     referer: str = "", cookies: dict = None,
                     timeout: int = VIDEO_TIMEOUT, stop_flag=None) -> int:
    """下载第 k 段到 fpath。已有完整段（大小==期望区长）直接复用（续传）。
    不完整段删掉重下；requests 尝试 VIDEO_RETRY+1 次仍失败 → curl --range 回退。失败 raise。"""
    start, end = span
    expect = end - start + 1
    if os.path.exists(fpath) and os.path.getsize(fpath) == expect:
        return expect                       # 续传：跳过已完成段
    _remove_if(fpath)
    last = ""
    for attempt in range(1 + VIDEO_RETRY):
        if stop_flag and stop_flag():
            raise RuntimeError("已停止")
        try:
            with open(fpath, "wb") as fh:
                n = _stream_range(url, start, end, fh, referer=referer,
                                  cookies=cookies, timeout=timeout)
            if n == expect:
                return n
            last = f"段字节不符({n}!={expect})"
        except Exception as e:
            last = str(e)
            _remove_if(fpath)
            if stop_flag and stop_flag():
                raise RuntimeError("已停止")
    # requests 全部失败 → curl --range 整段回退
    _remove_if(fpath)
    ok = _curl_range_file(url, fpath, start, end, referer, cookies, timeout)
    if ok == expect:
        return ok
    _remove_if(fpath)
    raise RuntimeError(f"Range 段 {k}(bytes {start}-{end}) 下载失败：{last or '未知'}")


def _concat_ranges(frag_paths: list, dest_path: str, total: int) -> None:
    """把各完整段按序流式拼接写 dest.part 再原子改名成 dest（不整读进内存）。"""
    tmp = dest_path + ".part"
    _remove_if(tmp)
    try:
        with open(tmp, "wb") as out:
            for p in frag_paths:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, out, length=VIDEO_CHUNK_SIZE)
        os.replace(tmp, dest_path)
    except Exception:
        _remove_if(tmp)
        raise


def _cleanup_range_fragments(dest_path: str) -> None:
    """清掉 <dest>.rng_* 与 .rng_meta.json（单流成功/最终收尾时避免残留）。"""
    base = dest_path
    meta = dest_path + ".rng_meta.json"
    _remove_if(meta)
    for f in os.listdir(os.path.dirname(base)) if os.path.isdir(os.path.dirname(base)) else []:
        if f.startswith(os.path.basename(base) + ".rng_"):
            _remove_if(os.path.join(os.path.dirname(base), f))


def _range_parallel_download(url: str, dest_path: str, *, total: int,
                             referer: str = "", cookies: dict = None,
                             timeout: int = VIDEO_TIMEOUT,
                             progress_callback=None, stop_flag=None) -> dict:
    """直链 Range 并行（aria2 式）：把 [0,total) 切 VIDEO_RANGE_SPLITS 段并发拉取，
    段写 <dest>.rng_%02d；已完成且大小==期望区长的段跳过（跨次续传）；全齐按序拼接为 dest 原子改名。
    用户停止：保留完整段 + meta 供下次续传，仅删不完整段。失败：同样保留完整段。
    返回 download_direct 兼容 dict。永不 raise。"""
    try:
        splits = max(1, int(VIDEO_RANGE_SPLITS))
        spans = _range_spans(total, splits)
        if not spans:
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": "Range: 文件为空", "error_code": ERR_EMPTY}
        frag = [f"{dest_path}.rng_{k:02d}" for k in range(len(spans))]
        meta = dest_path + ".rng_meta.json"
        src_id = hashlib.sha1((url + "#" + str(total)).encode("utf-8", "ignore")).hexdigest()[:16]
        # 源指纹不一致（同 dest 换源/换大小）→ 作废旧片段，防拼错
        reuse_ok = False
        if os.path.exists(meta):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    m = json.load(f)
                reuse_ok = isinstance(m, dict) and m.get("id") == src_id
            except Exception:
                reuse_ok = False
        if not reuse_ok:
            for p in frag:
                _remove_if(p)
        try:
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"id": src_id, "total": total}, f)
        except OSError:
            pass

        done_bytes = 0
        plock = threading.Lock()
        errs = []

        def _worker(k):
            nonlocal done_bytes
            n = _fetch_one_range(url, k, spans[k], frag[k], referer=referer,
                                 cookies=cookies, timeout=timeout, stop_flag=stop_flag)
            with plock:
                done_bytes += n
                if progress_callback:
                    try:
                        progress_callback(done_bytes, total, "直链")
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=max(1, len(spans))) as pool:
            futs = [pool.submit(_worker, k) for k in range(len(spans))]
            for fut in futs:
                try:
                    fut.result()
                except RuntimeError as e:
                    errs.append(str(e))
                    if stop_flag and stop_flag():
                        break        # 停止：保留完整段，不取消其它 worker（已完成的留下）

        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": done_bytes, "path": "", "error": ""}
        if errs:
            # 失败：删不完整段，完整段留作下次续传（meta 也保留以匹配源指纹）
            for k, p in enumerate(frag):
                exp = spans[k][1] - spans[k][0] + 1
                try:
                    if os.path.exists(p) and os.path.getsize(p) != exp:
                        _remove_if(p)
                except OSError:
                    pass
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": "; ".join(errs[:3]), "error_code": ERR_NETWORK}
        # 全齐 → 校验 → 拼接 → 原子改名 → 清片段与 meta
        for k, p in enumerate(frag):
            exp = spans[k][1] - spans[k][0] + 1
            if os.path.getsize(p) != exp:
                _remove_if(dest_path)
                return {"status": "fail", "bytes": 0, "path": "",
                        "error": f"段 {k} 校验失败", "error_code": ERR_NETWORK}
        _concat_ranges(frag, dest_path, total)
        for p in frag:
            _remove_if(p)
        _remove_if(meta)
        if not os.path.exists(dest_path) or os.path.getsize(dest_path) != total:
            _remove_if(dest_path)
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": "拼接结果校验失败", "error_code": ERR_NETWORK}
        return {"status": "ok", "bytes": total, "path": dest_path, "error": ""}
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                "error_code": ERR_NETWORK}
    except Exception as e:
        return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                "error_code": ERR_NETWORK}


# ================================================================
# 直链下载（stream=True 分块写盘，先 .part 后原子改名）
# ================================================================

def download_direct(video_url: str, dest_path: str, *,
                    referer: str = "", timeout: int = VIDEO_TIMEOUT,
                    progress_callback=None, stop_flag=None,
                    cookies: dict = None) -> dict:
    """直接下载 mp4/flv/... 直链。返回 {"status", "bytes", "path", "error", "error_code"}。
    断点续传：.part 已存在且非空时先发 Range 续写；服务器不支持续传(回 200)自动全量重下。
    Block C：无半截 .part 且服务端支持 Range 且文件 ≥ VIDEO_RANGE_MIN_BYTES →
    先走多连接切分（aria2 式）；Range 失败自动回退到本单流路径。"""
    cookies = _dl_cookies(video_url, cookies)
    part = dest_path + ".part"
    if not os.path.exists(part):
        _total, _sup = _range_probe(video_url, referer=referer, cookies=cookies,
                                    timeout=timeout)
        if _sup and _total >= max(1, int(VIDEO_RANGE_MIN_BYTES)):
            res = _range_parallel_download(video_url, dest_path, total=_total,
                                           referer=referer, cookies=cookies,
                                           timeout=timeout,
                                           progress_callback=progress_callback,
                                           stop_flag=stop_flag)
            if res.get("status") == "ok":
                return res
            if stop_flag and stop_flag():
                return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
            # Range 失败 → 落到下方单流 .part 路径（成功收尾时清掉残留片段）
    attempts = 1 + VIDEO_RETRY
    last_err = ""
    for attempt in range(attempts):
        if stop_flag and stop_flag():
            _remove_if(part)
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        try:
            resume = os.path.getsize(part) if os.path.exists(part) else 0
        except OSError:
            resume = 0
        try:
            headers = _media_headers(video_url, referer)
            if resume > 0:
                headers["Range"] = f"bytes={resume}-"
            r, _rerr = _http_get(video_url, headers=headers, cookies=cookies,
                                 timeout=timeout, stream=True)
            if r is None:
                raise _rerr or requests.exceptions.ConnectionError(
                    f"连接失败：{video_url[:100]}")
            if r.status_code not in (200, 206):
                code = r.status_code
                # #6d 下载层 403 螺旋：对可恢复码先读 body_sample(stream 态) → 归因+恢复一次
                plan = None
                if code in (401, 403, 406, 429, 451) \
                        and getattr(_CFG, "VIDEO_DL_RECOVER_403", True):
                    budget = int(getattr(_TL_OVERRIDE, "dl_recover_left", 1))
                    if budget > 0:
                        body = b""
                        try:
                            body = next(r.iter_content(4096), b"")
                        except Exception:
                            body = b""
                        try:
                            _hdrs = dict(r.headers or {})
                        except Exception:
                            _hdrs = {}
                        r.close()
                        plan = _recover_media_status(video_url, referer, cookies,
                                                     code, _hdrs, body)
                    else:
                        r.close()
                else:
                    r.close()
                if plan is None:
                    if code == 429:
                        return {"status": "fail", "bytes": 0, "path": "",
                                "error": f"HTTP {code}（限频）",
                                "error_code": ERR_HTTP_STATUS}
                    if code in (401, 403, 406, 451):
                        return {"status": "fail", "bytes": 0, "path": "",
                                "error": f"HTTP {code}（可能需登录或防盗链）",
                                "error_code": ERR_HOTLINK}
                    return {"status": "fail", "bytes": 0, "path": "",
                            "error": f"HTTP {code}",
                            "error_code": ERR_HTTP_STATUS}
                if plan.get("kind") == "sleep":              # 429 退避后下一轮
                    sl = float(plan.get("sleep_sec") or 0)
                    if sl > 0:
                        time.sleep(sl)
                    last_err = f"HTTP {code}（限频）"
                    continue
                # swap / sign / retry：换档或附加头包住单次递归重试（预算=1，深≤1 不无限）
                if plan.get("kind") == "swap":
                    ctx = _dl_profile_override(profile_key=plan.get("swap_key"))
                else:
                    ctx = _dl_profile_override(extra_headers=plan.get("extra_headers"))
                if plan.get("url"):                          # 源变了(重签) → resume 强制 0
                    _remove_if(part)
                nurl = plan.get("url") or video_url
                nck = plan.get("cookies") or cookies
                try:
                    _TL_OVERRIDE.dl_recover_left = budget - 1
                    with ctx:
                        return download_direct(nurl, dest_path, referer=referer,
                                               timeout=timeout,
                                               progress_callback=progress_callback,
                                               stop_flag=stop_flag, cookies=nck)
                finally:
                    try:
                        delattr(_TL_OVERRIDE, "dl_recover_left")
                    except Exception:
                        pass
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype.startswith("text/"):
                r.close()
                return {"status": "fail", "bytes": 0, "path": "",
                        "error": "返回内容为网页(非视频)，疑似防盗链/需登录",
                        "error_code": ERR_HOTLINK}
            append = (r.status_code == 206 and resume > 0)
            if not append:
                resume = 0          # 服务器不支持续传 → 全量重下（避免拼坏文件）
            total_raw = r.headers.get("Content-Length") or ""
            if append:
                total = resume + (int(total_raw) if total_raw.isdigit() else 0)
            else:
                total = int(total_raw) if total_raw.isdigit() else -1
            done = resume
            mode = "ab" if append else "wb"
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=VIDEO_CHUNK_SIZE):
                    if not chunk:
                        continue
                    if stop_flag and stop_flag():
                        r.close()
                        _remove_if(part)
                        return {"status": "stopped", "bytes": done, "path": "", "error": ""}
                    f.write(chunk)
                    done += len(chunk)
                    if progress_callback:
                        progress_callback(done, total, "直链")
            r.close()
            if done <= resume:
                return {"status": "fail", "bytes": 0, "path": "", "error": "空文件",
                        "error_code": ERR_EMPTY}
            os.replace(part, dest_path)
            _cleanup_range_fragments(dest_path)
            return {"status": "ok", "bytes": done, "path": dest_path, "error": "",
                    "error_code": ""}
        except (requests.RequestException, OSError) as e:
            last_err = str(e)
            # requests 被 TLS 指纹拦截 → 回退 curl.exe 直接落盘（自带 -C - 续传）
            if _locate_curl():
                curl_sz = _curl_download_file(video_url, part, referer,
                                              cookies=cookies, resume_from=resume)
                if curl_sz:
                    try:
                        os.replace(part, dest_path)
                        _cleanup_range_fragments(dest_path)
                        return {"status": "ok", "bytes": curl_sz,
                                "path": dest_path, "error": "", "error_code": ""}
                    except OSError:
                        pass
                _remove_if(part)
            if attempt < attempts - 1:
                time.sleep(0.6 * (attempt + 1))
            else:
                return {"status": "fail", "bytes": 0, "path": "", "error": last_err,
                        "error_code": ERR_NETWORK}
        except Exception as e:
            _remove_if(part)
            return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                    "error_code": ERR_NETWORK}
    return {"status": "fail", "bytes": 0, "path": "", "error": last_err,
            "error_code": ERR_NETWORK}


# ================================================================
# HLS (m3u8) 下载
# ================================================================

def _fetch_binary(url: str, referer: str = "", timeout: int = VIDEO_TIMEOUT,
                  cookies: dict = None, purpose: str = None):
    r, err = _http_get(url, headers=_media_headers(url, referer, purpose=purpose),
                       cookies=cookies, timeout=timeout)
    if r is None:
        raise err or requests.exceptions.ConnectionError(f"连接失败：{url[:100]}")
    if r.status_code != 200:
        code = r.status_code
        # #6d：可恢复 4xx 且恢复编排总开关开 → 抛 _HTTPStatus 让 _robust_binary/download_direct
        # 归因+换 URL/换档/cookie/退避；其余(404 等 / master off)维持 b"" 逐字节保现状。
        if code in (401, 403, 406, 429, 451) and getattr(_CFG, "VIDEO_DL_RECOVER_403", True):
            try:
                body = (r.content or b"")[:4096]
            except Exception:
                body = b""
            try:
                hdrs = dict(r.headers or {})
            except Exception:
                hdrs = {}
            r.close()
            raise _HTTPStatus(url, code, hdrs, body, purpose=purpose)
        r.close()
        return b""
    data = r.content
    r.close()
    return data


# ---- 浏览器级传输回退：curl.exe（系统 schannel TLS） ----
# 动漫 CDN 常对 python requests(OpenSSL 指纹) 做选择性断连/拦 TS 分片，
# 而 curl.exe 走 Windows schannel 可正常通过 → 作为 requests 失败时的回退通道。
_CURL_BIN = None


def _locate_curl() -> str:
    global _CURL_BIN
    if _CURL_BIN is not None:
        return _CURL_BIN
    exe = os.environ.get("CURL_BIN") or ""
    if not exe:
        exe = shutil.which("curl") or ""
    if not exe:
        cand = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "curl.exe")
        if os.path.exists(cand):
            exe = cand
    _CURL_BIN = exe or ""
    return _CURL_BIN


# ---- ffmpeg(静态版) 定位与调用：用于 fMP4/CMAF 与 DASH(.mpd) 的 remux/mux ----
_FFMPEG_BIN = None


def _locate_ffmpeg() -> str:
    """定位 ffmpeg.exe。搜索顺序：环境变量 FFMPEG_BIN → 项目内 runtime/ffmpeg/bin
    → PATH。找不到返回 ""（对应功能自动降级为“拼接/暂不支持”）。"""
    global _FFMPEG_BIN
    if _FFMPEG_BIN is not None:
        return _FFMPEG_BIN
    exe = os.environ.get("FFMPEG_BIN") or ""
    if not exe:
        here = os.path.dirname(os.path.abspath(__file__))
        cand = os.path.join(here, "runtime", "ffmpeg", "bin", "ffmpeg.exe")
        if os.path.exists(cand):
            exe = cand
    if not exe:
        exe = shutil.which("ffmpeg") or ""
    _FFMPEG_BIN = exe or ""
    return _FFMPEG_BIN


def _run_ffmpeg(args: list, *, log=None, stop_flag=None,
                timeout: int = 0) -> tuple:
    """运行 ffmpeg，返回 (returncode, 输出尾部文本)。args 为完整参数(不含可执行文件名)。
    后台线程读输出避免管道阻塞；stop_flag 置位时终止进程并返回非 0。"""
    exe = _locate_ffmpeg()
    if not exe:
        raise RuntimeError("未找到 ffmpeg，无法 remux")
    cmd = [exe] + args
    logmsg = None
    if log:
        def logmsg(m):
            try:
                log(str(m))
            except Exception:
                pass
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace",
                         bufsize=1)
    out_lines = []
    import queue

    def _reader():
        try:
            for ln in p.stdout:
                out_lines.append(ln)
        except Exception:
            pass

    thr = threading.Thread(target=_reader, daemon=True)
    thr.start()
    try:
        start = time.time()
        while True:
            if stop_flag and stop_flag():
                p.terminate()
                break
            if p.poll() is not None:
                break
            if timeout and time.time() - start > timeout:
                p.terminate()
                break
            time.sleep(0.2)
        p.wait(timeout=10)
    finally:
        thr.join(timeout=5)
    tail = "".join(out_lines[-40:])
    if logmsg:
        for ln in out_lines:
            if ln.startswith(("[hls", "error", "Error", "Invalid", "Failed", "Unable")) or "Error" in ln:
                logmsg("  ffmpeg: " + ln.strip())
    return p.returncode, tail


def _curl_cookie_arg(cookies: dict) -> str:
    """把 {name: value} 拼成 Cookie 头值；空则返回 ""。"""
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def _curl_fetch(url: str, referer: str = "", timeout: int = VIDEO_TIMEOUT,
                cookies: dict = None, purpose: str = None) -> bytes:
    """curl.exe 抓取二进制到内存（供清单/分片/密钥回退）。失败返回 b""。
    purpose 缺省按 URL 扩展名归类（清单/分片/密钥 → media 形状；直链 → native）。"""
    exe = _locate_curl()
    if not exe:
        return b""
    if purpose is None:
        purpose = _SP.purpose_for_url(url) if _SP is not None else "segment"
    ua, al, extra = _curl_profile_args(purpose, url, referer)
    cmd = [exe, "-sS", "-L", "--max-time", str(timeout),
           "-A", ua,
           "-H", "Accept: */*",
           "-H", f"Referer: {referer or url}"]
    cmd += extra                        # Sec-CH-UA / Sec-Fetch-*（同档派生）
    if al:
        cmd += ["-H", f"Accept-Language: {al}"]
    ck = _curl_cookie_arg(cookies)
    if ck:
        cmd += ["-H", f"Cookie: {ck}"]
    prox_url = _proxy_pick(url)             # curl 原生支持 socks5/http(s)
    if prox_url:
        cmd += ["--proxy", prox_url]
    cmd.append(url)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except Exception:
        if prox_url:
            _proxy_mark(url, prox_url, False)
        return b""
    if p.returncode != 0:
        if prox_url and _curl_proxy_fail(p.stderr or b""):
            _proxy_mark(url, prox_url, False)
        return b""
    return p.stdout or b""


_REQUESTS_BLOCKED_HOSTS = set()   # requests(OpenSSL) 被 CDN 指纹拦截的 host 缓存，后续分片直接走 curl


def _is_blocked(host: str) -> bool:
    with _BLOCKED_LOCK:
        return host in _REQUESTS_BLOCKED_HOSTS


def _block_host(host: str) -> None:
    with _BLOCKED_LOCK:
        _REQUESTS_BLOCKED_HOSTS.add(host)


# ================================================================
# #6c 分片级重签：注册表 + 单飞协商（被下载层 403 恢复编排引用；无注册恒 no-op）
# signer 职责：拿到一次失败的 (url, status, headers/body 摘要) → 决定换什么签名/参数，
# 产出 (new_url, extra_headers) 交给传输层重试。全改动默认零行为变化。
# ================================================================
_SIGNER_LOCK = threading.Lock()
_SIGNERS = []                 # [(host_suffix, fn), ...]，.suffix 子域匹配，先注册先命中
_SIGNER_TTL = 60.0            # 同 key 签名结果缓存秒数（后续同源槽免重复协商）
_SIGNER_CACHE = {}            # sfkey -> (expiry, result_or_None)
_SIGNER_CACHE_LOCK = threading.Lock()
_SF_LOCK = threading.Lock()
_SF = {}                      # sfkey -> {"evt":Event, "result":.., "err":..}（单飞在途表）


def _signer_match(host: str):
    """按注册表匹配 signer：".suffix" 前缀子域（host endswith），否则精确 host。无 → None。"""
    host = (host or "").lower()
    with _SIGNER_LOCK:
        for suffix, fn in _SIGNERS:
            s = (suffix or "").lower()
            if host == s or (s.startswith(".") and host.endswith(s)):
                return fn
    return None


def register_signer(host_suffix: str, fn):
    """注册一个签名协商器（下载层 403 恢复用）。fn(url, *, referer, status, resp_meta)
    -> (new_url, extra_headers:dict)|None。host_suffix ".example" 匹配其子域。
    供调用方(进程/离线夹具)注册；同后缀重复注册以后者覆盖。"""
    with _SIGNER_LOCK:
        s = (host_suffix or "").strip().lower()
        _SIGNERS[:] = [(x, f) for x, f in _SIGNERS if x != s]
        _SIGNERS.append((s, fn))
    _signer_cache_flush()


def unregister_signer(host_suffix: str) -> bool:
    """卸载某后缀的 signer（离线夹具防串案）。返回是否确有卸载。"""
    s = (host_suffix or "").strip().lower()
    with _SIGNER_LOCK:
        before = len(_SIGNERS)
        _SIGNERS[:] = [(x, f) for x, f in _SIGNERS if x != s]
    _signer_cache_flush()
    return len(_SIGNERS) != before


def _signer_cache_flush():
    with _SIGNER_CACHE_LOCK:
        _SIGNER_CACHE.clear()


def _singleflight(key: str, producer):
    """同 key 并发只跑一次 producer，其余线程等结果后原样取回；try/finally 删条目防泄漏。"""
    with _SF_LOCK:
        entry = _SF.get(key)
        if entry is None:
            entry = _SF[key] = {"evt": threading.Event(), "result": None, "err": None}
            first = True
        else:
            first = False
    if first:
        try:
            entry["result"] = producer()
        except BaseException as e:          # noqa: BLE001 — 存起让等待方同抛，不留半吊子
            entry["err"] = e
        finally:
            entry["evt"].set()
            with _SF_LOCK:
                if _SF.get(key) is entry:
                    del _SF[key]
    else:
        entry["evt"].wait()
    if entry["err"] is not None:
        raise entry["err"]
    return entry["result"]


def _apply_signer(url: str, referer: str, status: int, resp_meta: dict,
                  *, negotiate_key: str = None):
    """命中注册 signer → 单飞产签名一次、TTL 缓存给同源后续槽。
    返回 (new_url, extra_headers:dict)|None；无 signer/异常一律 None（下载语义不受影响）。
    negotiate_key 提供时按协商键单飞（如 AES key URI / 整份源清单），缺省按 url 单飞。"""
    try:
        fn = _signer_match(_host_of(url))
        if fn is None:
            return None
        sfkey = negotiate_key or url
        now = time.time()
        with _SIGNER_CACHE_LOCK:
            hit = _SIGNER_CACHE.get(sfkey)
            if hit and hit[0] > now:
                return hit[1]

        def _prod():
            try:
                return fn(url, referer=referer or "", status=int(status or 0),
                          resp_meta=dict(resp_meta or {}))
            except Exception:
                return None

        out = _singleflight(sfkey, _prod)
        try:
            with _SIGNER_CACHE_LOCK:
                _SIGNER_CACHE[sfkey] = (time.time() + _SIGNER_TTL, out)
        except Exception:
            pass
        return out
    except Exception:
        return None


def _robust_binary(url: str, referer: str = "", cookies: dict = None,
                   min_len: int = 0, tries: int = 0, purpose: str = None) -> bytes:
    """拉取二进制并做长度校验：requests 失败/过短 → 自动回退 curl.exe，带退避重试。
    同一 host 首次 requests 连接失败即记入黑名单（多线程下经锁），后续分片不再浪费一次握手失败。
    #6d：HTTP 4xx(401/403/406/429/451，_HTTPStatus) 是「源侧状态」而非握手/TLS 被拦——
    绝不 _block_host（否则 403 把 host 全踢去 curl schannel，与 TLS 拦截语义混淆）。恢复编排
    只吃一次（recovered_once）：归因 challenge→自动Chrome收cookie / 429→短退避 /
    signer 重签 / 换身份档；无方案仍走一次 curl schannel 兜底（可能 requests 403/curl 指纹能过）。
    全部失败 raise：_HTTPStatus 尾则附人工可执行 hint 进 RuntimeError 文案。
    purpose 缺省按 URL 扩展名归类（manifest/segment/key/direct…），requests 与 curl 两路同源。"""
    if tries <= 0:
        tries = 1 + max(1, VIDEO_RETRY)
    if purpose is None and _SP is not None:
        purpose = _SP.purpose_for_url(url)
    last = None
    host = urlparse(url).netloc.lower()
    imp = _imp_matches(url) and bool(getattr(_CFG, "VIDEO_IMPERSONATE_ENABLED", True))
    _before_ov = (getattr(_TL_OVERRIDE, "profile_key", None),
                  getattr(_TL_OVERRIDE, "extra_headers", None))
    recovered_once = False
    attempt = 0
    try:
        while attempt < tries:
            hs = None
            data = b""
            if imp or not _is_blocked(host):
                try:
                    data = _fetch_binary(url, referer, cookies=cookies, purpose=purpose)
                except _HTTPStatus as e:
                    hs = e
                    last = e
                except Exception as e:
                    last = e
                    if not imp:
                        _block_host(host)                     # 握手/TLS 级被拦 → 记住
            if data and len(data) >= min_len:
                return data

            if hs is not None:
                # HTTP 状态级：归因 + 恢复一次（绝不 _block_host）
                plan = None
                if not recovered_once:
                    plan = _recover_media_status(url, referer, cookies, hs.code,
                                                 hs.headers, hs.body_sample, purpose=purpose)
                if plan is None:
                    # 无方案 → 仍试一次 curl schannel（可能 requests 403 而 curl 指纹能过），不拉黑
                    if not imp:
                        fb = _curl_fetch(url, referer, cookies=cookies, purpose=purpose)
                        if fb and len(fb) >= min_len:
                            return fb
                    if attempt < tries - 1:
                        time.sleep(0.5 * (attempt + 1))
                    attempt += 1
                    continue
                if plan.get("kind") == "sleep":          # 429 短退避（不换档硬试）
                    sl = float(plan.get("sleep_sec") or 0)
                    if sl > 0:
                        time.sleep(sl)
                    attempt += 1
                    continue
                recovered_once = True                    # 换 URL/换档/cookie：不耗尝试数、不 sleep
                if plan.get("url"):
                    url = plan["url"]
                if plan.get("cookies"):
                    cookies = plan["cookies"]
                if plan.get("kind") == "swap":
                    _override_set(profile_key=plan.get("swap_key"))     # 线程级换档(本次段有效)
                elif plan.get("extra_headers"):
                    _override_set(extra_headers=plan.get("extra_headers"))
                continue

            # data 空/过短（非 4xx）：非指纹 host 才做 schannel curl 兜底
            if not imp and (not data or len(data) < min_len):
                if not _is_blocked(host) and not data:
                    _block_host(host)                     # 空响应也视为该 host 不可靠
                cdata = _curl_fetch(url, referer, cookies=cookies, purpose=purpose)
                if cdata and len(cdata) >= min_len:
                    return cdata
            if attempt < tries - 1:
                time.sleep(0.5 * (attempt + 1))
            attempt += 1
        if isinstance(last, _HTTPStatus):
            raise RuntimeError(f"抓取失败：{url[:100]}｜{_dl_failure_hint(last)}")
        raise last or RuntimeError(f"抓取失败：{url[:100]}")
    finally:
        _override_clear()
        if _before_ov[0] is not None or _before_ov[1] is not None:
            _override_set(profile_key=_before_ov[0], extra_headers=_before_ov[1])


def _curl_download_file(url: str, dest: str, referer: str = "",
                        timeout: int = VIDEO_TIMEOUT, cookies: dict = None,
                        resume_from: int = 0) -> int:
    """curl.exe 直接落盘（直链下载回退）。成功返回字节数，失败删残留返回 0。
    resume_from>0 时用 curl -C - 从既有文件续传（适合 requests 失败时的 .part）。"""
    exe = _locate_curl()
    if not exe:
        return 0
    ua, al, extra = _curl_profile_args("direct", url, referer)
    cmd = [exe, "-sS", "-L", "--fail", "--max-time", str(timeout),
           "-A", ua,
           "-H", "Accept: */*",
           "-H", f"Referer: {referer or url}"]
    cmd += extra                        # Sec-CH-UA / Sec-Fetch-*（同档派生）
    if al:
        cmd += ["-H", f"Accept-Language: {al}"]
    ck = _curl_cookie_arg(cookies)
    if ck:
        cmd += ["-H", f"Cookie: {ck}"]
    prox_url = _proxy_pick(url)             # curl 原生支持 socks5/http(s)
    if prox_url:
        cmd += ["--proxy", prox_url]
    if resume_from > 0:
        cmd += ["-C", "-"]        # 断点续传：curl 从已有文件大小自动续
    cmd += ["-o", dest, url]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except Exception:
        if prox_url:
            _proxy_mark(url, prox_url, False)
        _remove_if(dest)
        return 0
    if p.returncode != 0:
        if prox_url and _curl_proxy_fail(p.stderr or b""):
            _proxy_mark(url, prox_url, False)
        _remove_if(dest)
        return 0
    try:
        sz = os.path.getsize(dest)
    except OSError:
        sz = 0
    if sz <= 0:
        _remove_if(dest)
        return 0
    return sz


def parse_hls(text: str, base_url: str) -> dict:
    """解析 m3u8 文本。返回 {"kind", "variants", "segments", "encryption",
    "has_map", "map", "single_file"}。
    map: fMP4/CMAF 的 init 段信息 {"uri": 绝对URL, "byterange": ""|"len@off"}；
    single_file: 所有分片共用一个文件、靠 BYTERANGE 切分（下载整份再 remux）。"""
    lines = [ln.strip() for ln in text.splitlines()]
    pl = {"kind": "media", "variants": [], "segments": [],
          "encryption": None, "has_map": False,
          "map": None, "single_file": False, "media_seq": 0}

    def resolve(uri):
        u = uri.strip().strip('"')
        return _normalize(u, base_url) or uri

    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        pl["kind"] = "master"
        prev_attr = ""
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF"):
                prev_attr = ln
            elif ln and not ln.startswith("#"):
                bw = 0
                m = re.search(r"BANDWIDTH\s*=\s*(\d+)", prev_attr, re.I)
                if m:
                    bw = int(m.group(1))
                res = 0
                m = re.search(r"RESOLUTION\s*=\s*(\d+)x(\d+)", prev_attr, re.I)
                if m:
                    res = int(m.group(1)) * int(m.group(2))
                pl["variants"].append({"uri": resolve(ln), "bandwidth": bw, "resolution": res})
                prev_attr = ""
        return pl

    # media 清单。cur_enc 记录当前生效的 EXT-X-KEY（KEY 轮换时后续分片各自命中自己的 key），
    # 每分片快照进 seg["enc"]；pl["encryption"] 保持末值(兼容旧读法)。MEDIA-SEQUENCE 用于
    # 默认 IV（RFC 8216：无显式 IV 时 = 分片 media sequence，16B 大端）。
    pending_dur = 0.0
    cur_enc = None
    for ln in lines:
        if ln.startswith("#EXT-X-BYTERANGE"):
            pl["single_file"] = True          # 分片靠 BYTERANGE 切分 → 单文件 fMP4
        elif ln.startswith("#EXT-X-MAP"):
            pl["has_map"] = True
            if pl.get("map") is None:
                mu = re.search(r'URI\s*=\s*"([^"]+)"', ln)
                mb = re.search(r'BYTERANGE\s*=\s*([^,\s]+)', ln)
                map_uri = resolve(mu.group(1)) if mu else ""
                pl["map"] = {"uri": map_uri,
                             "byterange": mb.group(1).strip('"\'') if mb else ""}
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            m = re.search(r"#EXT-X-MEDIA-SEQUENCE\s*:\s*(\d+)", ln)
            if m:
                pl["media_seq"] = int(m.group(1))
        elif ln.startswith("#EXT-X-KEY"):
            method = "NONE"
            uri = ""
            iv_hex = ""
            m = re.search(r"METHOD\s*=\s*([^,]+)", ln)
            if m:
                method = m.group(1).strip().upper()
            m = re.search(r'URI\s*=\s*"([^"]+)"', ln)
            if m:
                uri = m.group(1)
            m = re.search(r"IV\s*=\s*(0x[0-9A-Fa-f]+)", ln)
            if m:
                iv_hex = m.group(1)
            enc = {"method": method, "uri": resolve(uri) if uri else "",
                   "iv": iv_hex}
            pl["encryption"] = enc
            cur_enc = dict(enc) if method not in ("NONE", "") else None
        elif ln.startswith("#EXTINF"):
            m = re.search(r"#EXTINF\s*:\s*([\d.]+)", ln)
            if m:
                pending_dur = float(m.group(1))
        elif ln and not ln.startswith("#"):
            pl["segments"].append({"uri": resolve(ln), "duration": pending_dur,
                                   "idx": len(pl["segments"]),
                                   "enc": dict(cur_enc) if cur_enc else None})
            pending_dur = 0.0
    return pl


def choose_best_variant(pl: dict):
    """master 清单 → 选带宽最高的 variant 绝对 URL；media 清单 → None。"""
    if pl.get("kind") != "master" or not pl.get("variants"):
        return None
    best = max(pl["variants"], key=lambda v: (v.get("bandwidth") or 0, v.get("resolution") or 0))
    return best["uri"]


def _engine_fetch(url: str, referer: str = "", cookies: dict = None, min_len: int = 0) -> bytes:
    """让解密引擎的 key 拉取走 vg 的 _robust_binary（requests→curl.exe 回退 / 指纹 / cookie），
    而非引擎内置的纯 requests。加密 key 端点常与 CDN 同防（需同样 Referer/cookie/UA）。
    purpose=key：key 端点常无 .key 扩展名，显式标注防被归成 page(html Accept)。"""
    return _robust_binary(url, referer, cookies=cookies, min_len=max(1, min_len),
                          purpose="key")


def _fetch_one_segment(seg: dict, tmpdir: str, referer: str, aes_key=None, enc=None,
                       media_seq: int = 0, cookies: dict = None):
    """下载单个 TS 分片到 tmpdir/seg_%05d.ts，返回路径。失败 raise。
    内置重试：动漫站 CDN 常对非浏览器客户端做间歇性断连（RemoteDisconnected），
    单分片小重试可显著降低整集失败率。
    加密分片解密：解密引擎在位 → 逐分片 engine（KEY 轮换 / hex·json key 载体 /
    默认 IV=media_seq+idx）；引擎缺失 → aes_key 预取的单 key 回退（保持旧行为）。"""
    uri = seg["uri"]
    try:
        # requests(OpenSSL) 被 CDN 指纹拦截时自动回退 curl.exe(schannel)
        data = _robust_binary(uri, referer, cookies=cookies,
                              min_len=VIDEO_MIN_TS_SEG_BYTES)
    except Exception as e:
        raise RuntimeError(f"分片 {seg['idx']} 下载失败：{e}")
    enc = enc or seg.get("enc") or {}
    method = enc.get("method") or ""
    if method not in ("", "NONE"):
        if method != "AES-128":
            raise RuntimeError(f"分片 {seg['idx']} 加密方式 {method} 不支持")
        if aes_key is None and _SD is not None:
            # engine 逐分片：enc 转成引擎 schema（uri/iv_hex），seq=media_seq+idx
            eenc = {"method": "AES-128", "uri": enc.get("uri") or "",
                    "iv_hex": enc.get("iv") or ""}
            try:
                data = _SD.decrypt_segment(data, eenc,
                                           seq=media_seq + seg["idx"],
                                           referer=referer, cookies=cookies,
                                           fetch=_engine_fetch)
            except _SD.DecryptError as e:
                raise RuntimeError(f"分片 {seg['idx']} 解密失败：{e}")
        else:
            # 回退：清单级单 key 自解密
            from Crypto.Cipher import AES
            iv = None
            if enc.get("iv"):
                iv = bytes.fromhex(enc["iv"][2:])
            else:
                iv = (media_seq + seg["idx"]).to_bytes(16, "big")
            if len(iv) != 16:
                raise RuntimeError("IV 长度非法")
            if aes_key is None or len(aes_key) != 16:
                raise RuntimeError("AES 密钥缺失/非法")
            if len(data) % 16 != 0:
                raise RuntimeError("AES 分片长度非 16 的倍数，可能非 PKCS7 加密")
            data = AES.new(aes_key, AES.MODE_CBC, iv).decrypt(data)
            # 去掉 PKCS7 填充（加密 TS 分片整片填充）
            pad = data[-1]
            if 1 <= pad <= 16:
                data = data[:-pad]
    path = os.path.join(tmpdir, f"seg_{seg['idx']:05d}.ts")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _merge_ts_files(sorted_paths, dest_path):
    """按序拼接 TS 分片。首字节非 0x47（非 MPEG-TS）则报错，避免产出废文件。"""
    if not sorted_paths:
        raise RuntimeError("没有可合并的分片")
    with open(sorted_paths[0], "rb") as f:
        head = f.read(1)
    if head != b"\x47":
        raise RuntimeError("首分片不是 MPEG-TS(0x47)，可能是 fMP4/CMAF，无法拼接")
    with open(dest_path, "wb") as out:
        for p in sorted_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=VIDEO_CHUNK_SIZE)
    return os.path.getsize(dest_path)


def download_m3u8(m3u8_url: str, dest_path: str, *,
                  referer: str = "", timeout: int = VIDEO_TIMEOUT,
                  concurrency: int = VIDEO_CONCURRENT,
                  progress_callback=None, stop_flag=None,
                  cookies: dict = None) -> dict:
    """下载 HLS(m3u8)（永不 raise，异常统一转 fail dict）。
    返回 {"status": ok/fail/stopped/unsupported, "bytes", "path", "error"}。"""
    try:
        _stale = cleanup_partials()   # 启动扫一次影子目录陈旧半成品，防膨胀
        cookies = _dl_cookies(m3u8_url, cookies)
        return _download_m3u8_impl(m3u8_url, dest_path, referer=referer,
                                   timeout=timeout, concurrency=concurrency,
                                   progress_callback=progress_callback,
                                   stop_flag=stop_flag, cookies=cookies)
    except Exception as e:
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"HLS 下载异常：{e}", "error_code": ERR_UNKNOWN}


def _resolve_media_playlist(m3u8_url: str, referer: str = "", cookies: dict = None):
    """抓取 m3u8 并跟随 master→media，返回 (pl, media_url)。全程失败 raise RuntimeError。"""
    txt = _robust_binary(m3u8_url, referer, cookies=cookies, min_len=1,
                         purpose="manifest").decode("utf-8", "ignore")
    base_url = m3u8_url
    pl = parse_hls(txt, base_url)
    depth = 0
    while pl["kind"] == "master" and depth < 2:
        variant = choose_best_variant(pl)
        if not variant:
            raise RuntimeError("master 清单无可用清晰度")
        variant = _normalize(variant, base_url) or variant
        txt = _robust_binary(variant, referer, cookies=cookies, min_len=1,
                             purpose="manifest").decode("utf-8", "ignore")
        base_url = variant
        pl = parse_hls(txt, base_url)
        depth += 1
    if pl["kind"] == "master":
        raise RuntimeError("未解析到最终媒体清单")
    # 静态 DRM 判别：**此刻 txt 是下钻后的最终 media 清单原文**，EXT-X-KEY 就在里面。
    # 放在这里而不是 master 那一层 —— KEY 行只存在于 media 清单，master 里没有，
    # 在 master 上判会一律判成「不加密」（这正是整站 DRM 长期没被看见的原因）。
    # 零额外请求：清单本来就要取。
    pl["drm"] = _SD.probe_hls_drm(txt) if _SD is not None else None
    return pl, base_url


def probe_m3u8_format(m3u8_url: str, referer: str = "", cookies: dict = None) -> str:
    """轻量探测 m3u8 是普通 TS 分片还是 fMP4/CMAF(EXT-X-MAP)。
    返回 "ts"/"cmaf"；失败返回 ""（由下载端自行兜底）。"""
    try:
        pl, _media_url = _resolve_media_playlist(m3u8_url, referer, cookies=cookies)
    except Exception:
        return ""
    if pl.get("single_file") or pl.get("has_map"):
        return "cmaf"
    return "ts"


def _mp4_sibling(dest_path: str) -> str:
    """把 .ts/.m3u8 等目标名改成同名 .mp4（CMAF/DASH 产物）。"""
    root, ext = os.path.splitext(dest_path)
    if ext.lower() != ".mp4":
        return root + ".mp4"
    return dest_path


# ================================================================
# #6 人像节拍：协调线程级节奏器（调度层，绝不进 worker/_robust_binary）
# 范围红线：节拍只负责「礼貌速率」，与 _Gate/自适应(在途并发)并存不互替——自适应管并发、
# 节拍管大单总速率。默认阈值巨大(min 分片/集数) → 小单/离线夹具不触发、逐字节保现状。
# sleep 只允许在泵/整部串行的协调线程；worker 与 _robust_binary 永不 sleep(防占满线程池)。
# 小时闸门为模块级单例(跨集/跨清单共享)——否则每清单重建窗口则小时硬闸形同虚设。
# ================================================================
_PACE_HOURLY = None
_PACE_HOURLY_LOCK = threading.Lock()


class _HourlyGate:
    """滚动 1 小时速率闸门（纯逻辑、线程安全；now/sleep 可注入，离线夹具推假钟）。
    - 请求硬闸：窗口内请求条目数 ≥ max_req 时 wait_request 阻塞，直到最旧一条滑出 1 小时窗。
    - 字节记账：note_bytes 追加 (ts, n)，bytes_in_window() 给 pacer 判字节软闸。
    reset() 清空两窗（离线用例重启窗口用）。"""

    def __init__(self, *, max_req: int = 0, max_bytes: float = 0,
                 now=None, sleep=None, window: float = 3600.0):
        self._reqs = deque()          # 请求提交时刻（滚动计数）
        self._bts = deque()           # (ts, nbytes) 字节记账（可乱序/晚到，软闸用）
        self._max_req = max(int(max_req or 0), 0)
        self._max_bytes = float(max_bytes or 0)
        self._window = float(window or 3600.0)
        self._now = now or time.time
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._reqs.clear()
            self._bts.clear()

    def _prune(self, t: float):
        # <=：恰在窗口边界(ts==t-window)的请求算已滑出——否则阻塞放行后 need==0 会让计数无界增长
        cutoff = t - self._window
        while self._reqs and self._reqs[0] <= cutoff:
            self._reqs.popleft()
        while self._bts and self._bts[0][0] <= cutoff:
            self._bts.popleft()

    def request_wait(self) -> float:
        """距允许再发一个请求还需等待秒数（0=可发）。只读不阻塞。"""
        t = self._now()
        with self._lock:
            self._prune(t)
            if self._max_req <= 0 or len(self._reqs) < self._max_req:
                return 0.0
            oldest = self._reqs[0]
        need = oldest + self._window - t
        return need if need > 0 else 0.0

    def wait_request(self, est_bytes: int = 0):
        """阻塞直到窗口允许再发一个请求，随即记账该请求时刻。est_bytes 预占软闸窗口(次要)。"""
        est = int(est_bytes or 0)
        while True:
            need = self.request_wait()
            if need <= 0:
                t = self._now()
                with self._lock:
                    self._prune(t)
                    self._reqs.append(t)
                    if est > 0:
                        self._bts.append((t, est))
                return
            self._sleep(need)

    def note_bytes(self, n: int):
        n = int(n or 0)
        if n > 0:
            t = self._now()
            with self._lock:
                self._prune(t)
                self._bts.append((t, n))

    def requests_in_window(self) -> int:
        t = self._now()
        with self._lock:
            self._prune(t)
            return len(self._reqs)

    def bytes_in_window(self) -> float:
        t = self._now()
        with self._lock:
            self._prune(t)
            return float(sum(b for _, b in self._bts))


class _SegmentPacer:
    """分片级节奏器：before_submit 由泵协调线程在每段提交前调 —— jitter 睡 + 小时请求硬闸。
    note_bytes 累积软字节闸：窗口内字节首超限 → note() 注记 + 一次 COOLDOWN 秒短停
    （超限只短冷却、不整小时停机，保证大单仍能推进）。"""

    def __init__(self, *, note=None, now=None, sleep=None):
        self._hourly = _pace_hourly()
        try:
            self._jmin = float(getattr(_CFG, "VIDEO_PACE_JITTER_MIN", 0.15) or 0)
            self._jmax = max(self._jmin,
                             float(getattr(_CFG, "VIDEO_PACE_JITTER_MAX", 0.80) or 0))
        except (TypeError, ValueError):
            self._jmin, self._jmax = 0.15, 0.80
        self._maxb = float(getattr(_CFG, "VIDEO_PACE_HOURLY_MAX_BYTES", 60 * 1024 ** 3) or 0)
        self._cd = float(getattr(_CFG, "VIDEO_PACE_HOURLY_BYTE_COOLDOWN", 90.0) or 0)
        self._note = note or (lambda m: None)
        self._now = now or time.time
        self._sleep = sleep or time.sleep
        self._cooldown_until = 0.0
        self._noted = False
        self._lock = threading.Lock()

    def active(self) -> bool:
        return bool(getattr(_CFG, "VIDEO_PACE_ENABLED", True))

    def before_submit(self, _i=None):
        """协调线程提交每段前：jitter(0.15..0.8s) + 软字节冷却(若有) + 小时请求硬闸。
        慢而稳，sleep 全在此线程，不占 worker。"""
        if not self.active():
            return
        self._sleep(random.uniform(self._jmin, self._jmax))
        cd = self._byte_cooldown_wait()
        if cd > 0:
            self._sleep(cd)
        self._hourly.wait_request()

    def _byte_cooldown_wait(self) -> float:
        """若正处于字节软闸冷却 → 剩余秒数（只算不睡）；冷却过后复位 once 标记。"""
        t = self._now()
        with self._lock:
            if t < self._cooldown_until:
                return self._cooldown_until - t
            self._cooldown_until = 0.0
            self._noted = False
            return 0.0

    def note_bytes(self, n: int):
        n = int(n or 0)
        if n > 0:
            self._hourly.note_bytes(n)
        if self._maxb <= 0:
            return
        t = self._now()
        with self._lock:
            if t < self._cooldown_until:
                return
        if self._hourly.bytes_in_window() > self._maxb:
            with self._lock:
                if not self._noted:
                    self._noted = True
                    self._cooldown_until = self._now() + self._cd
            try:
                self._note("VIDEO_PACE 字节软闸：1 小时下载量超限，短暂冷却 %gs" % self._cd)
            except Exception:
                pass


def _pace_hourly():
    """滚动 1 小时请求/字节闸门模块级单例（懒建；离线夹具经 _reset_pace_state 复位）。"""
    global _PACE_HOURLY
    if _PACE_HOURLY is None:
        with _PACE_HOURLY_LOCK:
            if _PACE_HOURLY is None:
                gate = _HourlyGate(
                    max_req=int(getattr(_CFG, "VIDEO_PACE_HOURLY_MAX_REQUESTS", 4000) or 0),
                    max_bytes=float(getattr(_CFG, "VIDEO_PACE_HOURLY_MAX_BYTES",
                                            60 * 1024 ** 3) or 0))
                _PACE_HOURLY = gate
    return _PACE_HOURLY


def _pace_for_count(n: int):
    """清单分片数 ≥ 节拍最小分片数且总开关开 → 分片级节奏器；否则 None（泵零开销、逐字节保现状）。"""
    if not bool(getattr(_CFG, "VIDEO_PACE_ENABLED", True)):
        return None
    try:
        mn = int(getattr(_CFG, "VIDEO_PACE_MIN_SEGMENTS", 600) or 0)
    except (TypeError, ValueError):
        mn = 600
    if int(n or 0) < mn:
        return None
    return _SegmentPacer()


def _pace_episode_gap(total_eps: int):
    """整部集间人工间隔：集数 ≥ MIN_EPISODES 才 sleep uniform（协调线程）。
    仅对「串行整部」由调用方在集间调——小整部/单集不触发，行为逐字节不变。"""
    if not bool(getattr(_CFG, "VIDEO_PACE_ENABLED", True)):
        return
    try:
        mn = int(getattr(_CFG, "VIDEO_PACE_MIN_EPISODES", 10) or 0)
    except (TypeError, ValueError):
        mn = 10
    if int(total_eps or 0) < mn:
        return
    lo = float(getattr(_CFG, "VIDEO_PACE_EPISODE_GAP_MIN", 8.0) or 0)
    hi = float(getattr(_CFG, "VIDEO_PACE_EPISODE_GAP_MAX", 20.0) or 0)
    if hi < lo:
        hi = lo
    time.sleep(random.uniform(lo, hi))


def _reset_pace_state():
    """离线测试缝：重建小时闸门单例（清掉跨用例累积的窗口计数）。"""
    global _PACE_HOURLY
    with _PACE_HOURLY_LOCK:
        _PACE_HOURLY = None


# ================================================================
# 提速基础设施：滑窗下载泵 + TS 有序直写
# ================================================================

def _pump_pool(pool, total: int, inflight: int, stop_flag,
               submit, on_done, progress_callback=None, label: str = "",
               pace=None):
    """通用滑窗下载泵：顺序提交且常驻 ≤inflight 个 in-flight 任务，用
    wait(FIRST_COMPLETED) 逐个收尾 —— 拖尾慢任务不再像旧整窗 barrier 那样让整个线程池空转。
    submit(i) → future；on_done(i, fut) 内 fut.result() 出错即抛（中断整个下载）。
    停止/出错时 raise RuntimeError；停止文案固定 "已停止"，由调用方区分归因。
    pace：可选 _SegmentPacer，协调线程在每段提交前 jitter+小时闸（#6 人像节拍，绝不进 worker）。"""
    pending = {}
    next_i = 0
    done_count = 0

    def _cancel_all():
        for f in list(pending):
            try:
                f.cancel()
            except Exception:
                pass

    while done_count < total:
        if stop_flag and stop_flag():
            _cancel_all()
            raise RuntimeError("已停止")
        while len(pending) < inflight and next_i < total:
            i = next_i
            if pace is not None and pace.active():
                pace.before_submit(i)
            try:
                pending[submit(i)] = i
            except Exception as e:
                _cancel_all()
                raise RuntimeError(str(e))
            next_i += 1
        if not pending:
            break
        done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
        for fut in done:
            if stop_flag and stop_flag():
                _cancel_all()
                raise RuntimeError("已停止")
            i = pending.pop(fut)
            try:
                on_done(i, fut)
            except RuntimeError:
                _cancel_all()
                raise
            except Exception as e:
                _cancel_all()
                raise RuntimeError(str(e))
            done_count += 1
            if progress_callback:
                try:
                    progress_callback(done_count, total, f"{label} {done_count}/{total}")
                except Exception:
                    pass


class _OrderedTsAppender:
    """按分片序把字节直写临时文件的乱序缓冲写入器（仅协调线程调用）。
    半成品放影子目录(<key>.part)，配合 <key>.idx 支持跨次断点续传 —— 输出目录保持干净。
    add(idx, data)：分片号允许乱序到达，连续段边到边写盘；idx0 首字节必须 0x47。
    全部就绪 commit() 把影子部分原子改名成 dest；abort(keep_partial=True) 保留影子+idx 供续传。
    flushed>0 时从第 flushed 片续写（前 flushed 片已在影子里，跳过，不再校验）。"""

    def __init__(self, dest_path: str, partial_path: str = None,
                 flushed: int = 0, file_bytes: int = 0):
        self._dest = dest_path
        self._tmp = partial_path or (dest_path + ".part")
        self._f = open(self._tmp, "ab" if flushed > 0 else "wb")
        self._buf = {}
        self._next = max(0, int(flushed))
        self._bytes = file_bytes
        self._closed = False

    def add(self, idx: int, data: bytes):
        if idx == 0 and self._next == 0:
            if not data or data[:1] != b"\x47":
                self.abort(keep_partial=False)
                raise RuntimeError("首分片不是 MPEG-TS(0x47)，可能是 fMP4/CMAF，无法拼接")
        self._buf[idx] = data
        self._flush()

    def _flush(self):
        while self._next in self._buf:
            chunk = self._buf.pop(self._next)
            try:
                self._f.write(chunk)
            except OSError:
                self.abort(keep_partial=False)
                raise
            self._bytes += len(chunk)
            self._next += 1

    def done(self) -> int:
        """已连续写盘的分片序号（=缓冲已清空的连续长度）。"""
        return self._next

    def buffered(self) -> int:
        return len(self._buf)

    def file_bytes(self) -> int:
        return self._bytes

    def commit(self) -> int:
        """全部写完后收尾：关文件 + 把影子部分原子改名成 dest。返回写盘分片数。"""
        if not self._closed:
            try:
                self._f.close()
            except OSError:
                pass
            self._closed = True
        try:
            os.replace(self._tmp, self._dest)
        except OSError:
            _remove_if(self._tmp)
            raise
        return self._next

    def abort(self, keep_partial: bool = False):
        if not self._closed:
            try:
                self._f.close()
            except OSError:
                pass
            self._closed = True
        # 只有真正 flush 过（next>0）的 keep 才有续传价值；
        # 一字节都没写就被停 → 留空壳毫无意义，删掉免得影子目录积尘。
        if keep_partial and self._next > 0:
            return
        _remove_if(self._tmp)


def _ts_fingerprint(segs: list) -> str:
    """纯 TS 续传指纹：分片总数 + 首/尾片段 URL hash。
    同一 dest 换清单/换源 → 指纹不同 → 丢弃旧影子重下。"""
    try:
        if not segs:
            return ""
        h = hashlib.sha1()
        h.update(str(len(segs)).encode("utf-8", "ignore"))
        first = (segs[0].get("uri") or "")
        last = (segs[-1].get("uri") or "")
        h.update(first.encode("utf-8", "ignore"))
        h.update(b"|")
        h.update(last.encode("utf-8", "ignore"))
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _partial_paths(dest_path: str):
    """影子目录路径：key=sha1(dest 绝对路径)[:16]，与 dest 同卷保证 os.replace 原子。"""
    key = hashlib.sha1(os.path.abspath(dest_path).encode("utf-8", "ignore")).hexdigest()[:16]
    try:
        os.makedirs(VIDEO_PARTIAL_DIR, exist_ok=True)
    except OSError:
        pass
    return (os.path.join(VIDEO_PARTIAL_DIR, key + ".part"),
            os.path.join(VIDEO_PARTIAL_DIR, key + ".idx"))


def _save_index(idx_path: str, fp: str, total: int, writer) -> None:
    """把续传点写进 .idx（内容极小，随 flush 落盘；硬杀丢几片只多下几片，不破坏正确性）。"""
    try:
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump({"fp": fp, "total": total,
                       "flushed": writer.done(), "bytes": writer.file_bytes()}, f)
    except OSError:
        pass


def _resume_state(partial_path: str, idx_path: str, fp: str, total: int):
    """读 .idx 决定续传点。返回 (flushed, file_bytes)。
    指纹/总数不符、.part 缺失或比 .idx 记录更短（尾部被硬杀）→ 清影子整片重下。"""
    try:
        if os.path.exists(idx_path):
            with open(idx_path, "r", encoding="utf-8") as f:
                idx = json.load(f)
            if (isinstance(idx, dict) and idx.get("fp") == fp
                    and idx.get("total") == total
                    and isinstance(idx.get("flushed"), int)
                    and 0 <= idx["flushed"] <= total):
                flushed = idx["flushed"]
                fbytes = int(idx.get("bytes") or 0)
                if flushed > 0:
                    try:
                        # 恰好相等才续：短了=尾部截断；长了=写盘后计数/idx 未落被硬杀
                        # （追加续写会重复一片）→ 两种都保守清掉整片重下。
                        if not os.path.exists(partial_path) \
                                or os.path.getsize(partial_path) != fbytes:
                            flushed, fbytes = 0, 0
                    except OSError:
                        flushed, fbytes = 0, 0
                return flushed, fbytes
    except Exception:
        pass
    _remove_if(partial_path)
    _remove_if(idx_path)
    return 0, 0


def cleanup_partials(max_age: float = 7 * 86400) -> int:
    """清扫影子目录内超过 max_age 秒未改动的半成品（防膨胀）。返回清理文件数。"""
    n = 0
    try:
        now = time.time()
        for name in os.listdir(VIDEO_PARTIAL_DIR):
            p = os.path.join(VIDEO_PARTIAL_DIR, name)
            if not os.path.isfile(p):
                continue
            try:
                if now - os.path.getmtime(p) > max_age:
                    _remove_if(p)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def _fetch_ts_bytes(seg: dict, referer: str, cookies: dict = None) -> bytes:
    """下载单个无加密 TS 分片到内存（有序直写路径的 worker）。失败 raise。"""
    data = _robust_binary(seg["uri"], referer, cookies=cookies,
                          min_len=VIDEO_MIN_TS_SEG_BYTES)
    return data


def _download_hls_plain_ts(segs: list, dest_path: str, *, referer: str = "",
                           timeout: int = VIDEO_TIMEOUT,
                           concurrency: int = VIDEO_CONCURRENT,
                           progress_callback=None, stop_flag=None,
                           cookies: dict = None) -> dict:
    """纯 TS（无 AES 加密）多分片：滑窗并发拉取 + 按序直写影子半成品，免临时文件与二次合并 IO。
    断点续传：半成品放 _video_partials/<key>.part + <key>.idx，同 dest 同清单重跑自动从续传点继续
    （前 flushed 片不再重下）；全部就绪后把影子部分原子改名成 dest 并删 .idx —— 输出目录保持干净。
    返回 {"status": ok/fail/stopped, "bytes", "path", "error", "error_code"}。永不 raise。"""
    total = len(segs)
    pace = _pace_for_count(total)      # #6 人像节拍：大清单才非 None，小单零开销
    window = max(int(VIDEO_ORDERED_WINDOW), max(1, concurrency) * 2)
    inflight = max(1, concurrency)
    fp = _ts_fingerprint(segs)
    partial_path, idx_path = _partial_paths(dest_path)
    flushed, file_bytes = _resume_state(partial_path, idx_path, fp, total)
    writer = _OrderedTsAppender(dest_path, partial_path=partial_path,
                                flushed=flushed, file_bytes=file_bytes)
    saved_done = flushed

    def _maybe_save():
        nonlocal saved_done
        if writer.done() != saved_done:
            _save_index(idx_path, fp, total, writer)
            saved_done = writer.done()

    try:
        with ThreadPoolExecutor(max_workers=inflight) as pool:
            pending = {}
            next_i = flushed
            try:
                while writer.done() < total:
                    if stop_flag and stop_flag():
                        for f in pending:
                            f.cancel()
                        raise RuntimeError("已停止")
                    # 提交守门：乱序缓冲 ≤ window（防 seg0 挂起时把整片拉进内存）
                    while (len(pending) < inflight and next_i < total
                           and next_i - writer.done() < window):
                        i = next_i
                        if pace is not None and pace.active():
                            pace.before_submit(i)
                        pending[pool.submit(_fetch_ts_bytes, segs[i],
                                            referer, cookies)] = i
                        next_i += 1
                    if not pending:
                        break
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for fut in done:
                        if stop_flag and stop_flag():
                            for f in pending:
                                f.cancel()
                            raise RuntimeError("已停止")
                        i = pending.pop(fut)
                        try:
                            data = fut.result()
                        except Exception as e:
                            for f in pending:
                                f.cancel()
                            raise RuntimeError(f"分片 {segs[i]['idx']} 下载失败：{e}")
                        try:
                            writer.add(i, data)
                        except RuntimeError:
                            for f in pending:
                                f.cancel()
                            raise
                        if pace is not None:
                            pace.note_bytes(len(data))   # 软字节闸记账（协调线程，安全）
                        _maybe_save()
                        if progress_callback:
                            try:
                                progress_callback(writer.done(), total,
                                                  f"HLS {writer.done()}/{total}")
                            except Exception:
                                pass
            except RuntimeError:
                writer.abort(keep_partial=True)   # 停/失败：保留影子 + .idx 供下次续传
                raise
        writer.commit()          # 全部就绪 → 影子部分原子改名成 dest
        _remove_if(idx_path)     # 已提交：删除续传点
        if not os.path.exists(dest_path) or os.path.getsize(dest_path) <= 0:
            _remove_if(dest_path)
            return {"status": "fail", "bytes": 0, "path": "", "error": "空文件",
                    "error_code": ERR_EMPTY}
        return {"status": "ok", "bytes": os.path.getsize(dest_path),
                "path": dest_path, "error": ""}
    except RuntimeError as e:
        writer.abort(keep_partial=True)
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                "error_code": ERR_HLS_SEGMENT}


def _download_m3u8_impl(m3u8_url: str, dest_path: str, *,
                        referer: str = "", timeout: int = VIDEO_TIMEOUT,
                        concurrency: int = VIDEO_CONCURRENT,
                        progress_callback=None, stop_flag=None,
                        cookies: dict = None, log=None) -> dict:
    """下载 HLS(m3u8)。返回 {"status": ok/fail/stopped/unsupported, "bytes", "path", "error"}。
    普通 TS 分片直接拼接成 .ts；fMP4/CMAF(EXT-X-MAP) 需 ffmpeg remux，产物自动改名为 .mp4。"""
    try:
        pl, media_url = _resolve_media_playlist(m3u8_url, referer, cookies=cookies)
    except Exception as e:
        return {"status": "fail", "bytes": 0, "path": "", "error": f"无法获取 m3u8 清单：{e}",
                "error_code": ERR_HLS_MANIFEST}

    # 商业 DRM：**在取任何分片之前**收手。原先要么走到 SAMPLE-AES 分支报「加密方式暂不支持」
    # （措辞含糊，看不出是 DRM），要么 fMP4 分支已经把一堆分片拉下来才发现拼不出画面。
    # 只识别、不解密 —— 这条是底线，不因为「多试一下」而松动。
    _drm = pl.get("drm") or {}
    if _drm.get("commercial"):
        return {"status": "skipped", "bytes": 0, "path": "",
                "error": "DRM(%s) 保护：只识别跳过，绝不解密" % ", ".join(_drm.get("drm_systems") or ["?"]),
                "error_code": ERR_DRM,
                "drm_systems": list(_drm.get("drm_systems") or [])}

    if pl.get("single_file"):
        # 单文件 fMP4（分片靠 BYTERANGE 切分同一文件）
        return _download_fmp4_singlefile(pl, media_url, dest_path, referer=referer,
                                         cookies=cookies, stop_flag=stop_flag, log=log)
    if pl.get("has_map"):
        return _download_fmp4(pl, media_url, dest_path, referer=referer,
                              concurrency=concurrency, progress_callback=progress_callback,
                              stop_flag=stop_flag, cookies=cookies, log=log)

    segs = pl.get("segments") or []
    if not segs:
        return {"status": "fail", "bytes": 0, "path": "", "error": "清单内无视频分片",
                "error_code": ERR_PARSER}
    if len(segs) > VIDEO_MAX_SEGMENTS:
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"分片数量过多({len(segs)})，超过上限 {VIDEO_MAX_SEGMENTS}，已拒绝",
                "error_code": ERR_SIZE_OVER}

    # 各分片加密态（KEY 轮换 → 逐分片命中自己的 key；单 key 清单各分片同 enc）
    methods = {e["method"] for e in (s.get("enc") or {} for s in segs)
               if e and e.get("method") not in ("NONE", "")}
    if methods - {"AES-128"}:
        return {"status": "unsupported", "bytes": 0, "path": "",
                "error": f"加密方式 {next(iter(methods - {'AES-128'}))} 暂不支持",
                "error_code": ERR_ENCRYPT_UNSUP}
    need_aes = "AES-128" in methods

    # 纯 TS（无加密）→ 滑窗并发 + 有序直写 dest（省临时文件与二次合并 IO）
    if not need_aes:
        return _download_hls_plain_ts(segs, dest_path, referer=referer, timeout=timeout,
                                      concurrency=concurrency,
                                      progress_callback=progress_callback,
                                      stop_flag=stop_flag, cookies=cookies)

    # AES-128 → 解密后合并；用滑窗泵去掉整窗 barrier
    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        return {"status": "fail", "bytes": 0, "path": "", "error": LOG_VIDEO_ENC_NEED,
                "error_code": ERR_AES_KEY}

    aes_key = None
    if _SD is None:
        # 引擎缺失回退：清单级单 key 预取（raw 前 16 字节），轮换在此路径下会失败
        enc0 = pl.get("encryption")
        if not (enc0 and enc0.get("uri")):
            return {"status": "fail", "bytes": 0, "path": "", "error": "AES 密钥 URI 缺失",
                    "error_code": ERR_AES_KEY}
        try:
            key_data = _robust_binary(enc0["uri"], referer, cookies=cookies, min_len=16)
        except Exception:
            return {"status": "fail", "bytes": 0, "path": "", "error": "AES 密钥获取失败",
                    "error_code": ERR_AES_KEY}
        aes_key = key_data[:16]

    media_seq = int(pl.get("media_seq") or 0)
    tmpdir = dest_path + f".parts_{hashlib.md5(m3u8_url.encode('utf-8', 'ignore')).hexdigest()[:8]}"
    os.makedirs(tmpdir, exist_ok=True)
    try:
        ok_paths = {}
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            def _seg_submit(i):
                return pool.submit(_fetch_one_segment, segs[i], tmpdir, referer,
                                   aes_key=aes_key, enc=segs[i].get("enc") or {},
                                   media_seq=media_seq, cookies=cookies)

            def _seg_on_done(i, fut):
                try:
                    path = fut.result()
                except Exception as e:
                    raise RuntimeError(f"分片 {segs[i]['idx']} 下载失败：{e}")
                ok_paths[i] = path

            _pump_pool(pool, len(segs), max(1, concurrency), stop_flag,
                       _seg_submit, _seg_on_done,
                       progress_callback=progress_callback, label="HLS",
                       pace=_pace_for_count(len(segs)))
        merged = _merge_ts_files([ok_paths[i] for i in sorted(ok_paths)], dest_path)
        return {"status": "ok", "bytes": merged, "path": dest_path, "error": ""}
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                "error_code": ERR_HLS_SEGMENT}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ================================================================
# fMP4/CMAF(EXT-X-MAP) 与 DASH(.mpd)：本地抓取 + ffmpeg remux
# ================================================================

def _fetch_one_raw(item: dict, tmpdir: str, name: str, referer: str,
                   cookies: dict = None) -> str:
    """抓取单个原始分片到 tmpdir/name（不做解密，密文原文交给 ffmpeg 处理）。失败 raise。"""
    uri = item.get("uri") or ""
    if not uri:
        raise RuntimeError("空分片 URL")
    data = _robust_binary(uri, referer, cookies=cookies, min_len=1)
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _dl_raw_list(items: list, tmpdir: str, prefix: str, referer: str,
                 cookies: dict = None, concurrency: int = VIDEO_CONCURRENT,
                 stop_flag=None, progress_callback=None, label: str = "分片"):
    """并发抓取 items([{uri, ...}]) 到 tmpdir/prefix_%05d.mp4，返回按原序的路径列表。失败 raise。
    用滑窗泵（常驻 inflight 个、逐个收尾），拖尾慢分片不再让整个池空转。"""
    n = len(items)
    outs = [None] * n
    if n == 0:
        return outs
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        def _submit(i):
            name = f"{prefix}_{i:05d}.mp4"
            return pool.submit(_fetch_one_raw, items[i], tmpdir, name, referer, cookies)

        def _on_done(i, fut):
            try:
                outs[i] = fut.result()
            except Exception as e:
                raise RuntimeError(str(e))

        _pump_pool(pool, n, max(1, concurrency), stop_flag, _submit, _on_done,
                   progress_callback=progress_callback, label=label,
                   pace=_pace_for_count(n))
    return outs


def _write_local_hls(pl_path: str, tmpdir: str, seg_paths: list,
                     init_path: str = None, key_path: str = None,
                     durations: list = None, method: str = "") -> None:
    """把本地 init/分片文件写成临时 m3u8 供 ffmpeg 读取。
    key_path/method 存在时输出 EXT-X-KEY（让 ffmpeg 自行 AES-128 解密）。"""
    segs = [os.path.basename(p) for p in seg_paths]
    target_dur = 10
    if durations:
        try:
            target_dur = max(10, int(max(d for d in durations if d)) + 1)
        except (ValueError, TypeError):
            pass
    with open(pl_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:7\n")
        f.write(f"#EXT-X-TARGETDURATION:{target_dur}\n")
        if key_path and method:
            f.write(f'#EXT-X-KEY:METHOD={method},URI="{os.path.basename(key_path)}"\n')
        if init_path:
            f.write(f'#EXT-X-MAP:URI="{os.path.basename(init_path)}"\n')
        for i, s in enumerate(segs):
            d = 0.0
            if durations and i < len(durations):
                try:
                    d = float(durations[i] or 0) or 0.0
                except (ValueError, TypeError):
                    d = 0.0
            f.write(f"#EXTINF:{d:.3f},\n{s}\n")
        # 显式 VOD 终止：本地合成清单是完整下载，缺 #EXT-X-ENDLIST 会让 ffmpeg 按 live
        # 处理、读完全部分片后仍等待清单刷新 → DASH/整轨 remux 偶发永久挂起
        f.write("#EXT-X-ENDLIST\n")
    return pl_path


def _ffmpeg_remux(pl_path: str, dest_path: str, *,
                  stop_flag=None, log=None) -> int:
    """ffmpeg 读临时 m3u8 → 无损 remux 成 .mp4。返回字节数；失败 raise。"""
    args = ["-hide_banner", "-y",
            "-protocol_whitelist", "file,crypto,http,https,tcp,tls",
            "-i", pl_path,
            "-c", "copy", "-movflags", "+faststart", dest_path]
    rc, tail = _run_ffmpeg(args, log=log, stop_flag=stop_flag)
    if rc != 0:
        raise RuntimeError(f"ffmpeg remux 失败(rc={rc})：{tail.strip()[:300]}")
    if not os.path.exists(dest_path) or os.path.getsize(dest_path) <= 0:
        raise RuntimeError("ffmpeg remux 产物为空")
    return os.path.getsize(dest_path)


def _concat_init_segs(init_path, seg_paths, dest_path) -> int:
    """无 ffmpeg 时的尽力而为：init + 分片 顺序拼接成单文件 fMP4（部分播放器可播）。"""
    with open(dest_path, "wb") as out:
        if init_path and os.path.exists(init_path):
            with open(init_path, "rb") as f:
                shutil.copyfileobj(f, out, length=VIDEO_CHUNK_SIZE)
        for p in seg_paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=VIDEO_CHUNK_SIZE)
    return os.path.getsize(dest_path)


_MOV_FASTSTART_EXTS = (".mp4", ".mov", ".m4v")


def _mux_local_hls(pls: list, roles: list, out_path: str, *,
                   log=None, stop_flag=None):
    """把多条本地【单轨】临时 m3u8 用 ffmpeg -c copy 合成到 out_path（#7 通用编排，
    自 download_dash 历史双轨 inline 抽离）。返回 (rc, tail)（_run_ffmpeg 同形）。

    pls   : 本地 m3u8 路径列表（各自 EXT-X-MAP init + 分片，单轨）
    roles : 与 pls 等长的轨道角色 "video"/"audio"；None=整轨不显式 map（整文件拷贝）
    out_path: 目标路径，输出容器由扩展名决定（.mp4/.mov/.m4v 自动 +faststart；
              .mkv 等纯 remux 不加）。容器可选由此体现。
    显式 role 逐一 -map {i}:v:0 / {i}:a:0；含 None → 不 map（ffmpeg 默认全选）。
    单轨纯音频 roles=["audio"] → 等价 -map 0:a:0。"""
    cmd = ["-hide_banner", "-y",
           "-protocol_whitelist", "file,crypto,http,https,tcp,tls"]
    for p in pls:
        cmd += ["-i", p]
    maps = []
    explicit = True
    for i, role in enumerate(roles):
        if role == "video":
            maps += ["-map", f"{i}:v:0"]
        elif role == "audio":
            maps += ["-map", f"{i}:a:0"]
        else:                        # 整轨 → 全选（不显式 map）
            explicit = False
    if explicit:
        cmd += maps
    cmd += ["-c", "copy"]
    if os.path.splitext(out_path)[1].lower() in _MOV_FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd += [out_path]
    return _run_ffmpeg(cmd, log=log, stop_flag=stop_flag)


def _download_fmp4(pl: dict, media_url: str, dest_path: str, *,
                   referer: str = "", concurrency: int = VIDEO_CONCURRENT,
                   progress_callback=None, stop_flag=None,
                   cookies: dict = None, log=None) -> dict:
    """fMP4/CMAF(EXT-X-MAP) 多分片下载。有 ffmpeg → 标准 .mp4；无 ffmpeg 且未加密 → 拼接尽力而为。"""
    dest_path = _mp4_sibling(dest_path)
    mapp = pl.get("map") or {}
    init_uri = mapp.get("uri") or ""
    if not init_uri:
        return {"status": "fail", "bytes": 0, "path": "",
                "error": "EXT-X-MAP 缺 init URI（无法 remux fMP4）",
                "error_code": ERR_HLS_MANIFEST}
    if mapp.get("byterange"):
        return {"status": "fail", "bytes": 0, "path": "",
                "error": "BYTERANGE 型 init 属于单文件流，请走整份下载分支",
                "error_code": ERR_HLS_MANIFEST}
    segs = pl.get("segments") or []
    if not segs:
        return {"status": "fail", "bytes": 0, "path": "", "error": "清单内无 fMP4 分片",
                "error_code": ERR_PARSER}
    if len(segs) > VIDEO_MAX_SEGMENTS:
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"分片数量过多({len(segs)})，超过上限 {VIDEO_MAX_SEGMENTS}，已拒绝",
                "error_code": ERR_SIZE_OVER}

    enc = pl.get("encryption")
    method = (enc or {}).get("method") or "NONE"
    if method not in ("NONE", ""):
        if method != "AES-128":
            return {"status": "unsupported", "bytes": 0, "path": "",
                    "error": f"fMP4 加密方式 {method} 暂不支持（需 SAMPLE-AES 解密）",
                    "error_code": ERR_ENCRYPT_UNSUP}
        if not _locate_ffmpeg():
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": "fMP4 已 AES-128 加密，解密需 ffmpeg，请安装后重试",
                    "error_code": ERR_MISSING_FFMPEG}
        if not enc.get("uri"):
            return {"status": "fail", "bytes": 0, "path": "", "error": "AES 密钥 URI 缺失",
                    "error_code": ERR_AES_KEY}

    tmpdir = dest_path + f".parts_{hashlib.md5(media_url.encode('utf-8', 'ignore')).hexdigest()[:8]}"
    os.makedirs(tmpdir, exist_ok=True)
    try:
        try:
            init_path = _fetch_one_raw({"uri": init_uri}, tmpdir, "init.mp4", referer, cookies)
        except Exception as e:
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": f"fMP4 init 片段获取失败：{e}", "error_code": ERR_HLS_SEGMENT}
        # AES-128：把密钥也取到本地，让 ffmpeg 解密。engine 在 → 先做 key 载体归一
        # （站点常以 hex 文本 / JSON API 提供，raw[:16] 会拿到 JSON 前缀报废）。
        key_path = None
        if method == "AES-128":
            try:
                key_data = _robust_binary(enc["uri"], referer, cookies=cookies, min_len=1)
            except Exception:
                return {"status": "fail", "bytes": 0, "path": "", "error": "AES 密钥获取失败",
                        "error_code": ERR_AES_KEY}
            try:
                if _SD is not None:
                    key_data = _SD.coerce_key(key_data)
            except Exception:
                pass
            if len(key_data) < 16:
                return {"status": "fail", "bytes": 0, "path": "", "error": "AES 密钥长度非法",
                        "error_code": ERR_AES_KEY}
            key_path = os.path.join(tmpdir, "key.mp4")
            with open(key_path, "wb") as f:
                f.write(key_data[:16])
        try:
            seg_paths = _dl_raw_list(segs, tmpdir, "seg", referer, cookies,
                                     concurrency=concurrency, stop_flag=stop_flag,
                                     progress_callback=progress_callback, label="fMP4")
        except RuntimeError as e:
            if stop_flag and stop_flag():
                return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
            return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                    "error_code": ERR_HLS_SEGMENT}
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        durations = [s.get("duration") for s in segs]
        if _locate_ffmpeg():
            pl_path = os.path.join(tmpdir, "local.m3u8")
            _write_local_hls(pl_path, tmpdir, seg_paths, init_path=init_path,
                             key_path=key_path, durations=durations, method=method)
            try:
                nbytes = _ffmpeg_remux(pl_path, dest_path, stop_flag=stop_flag, log=log)
                return {"status": "ok", "bytes": nbytes, "path": dest_path, "error": ""}
            except RuntimeError as e:
                if stop_flag and stop_flag():
                    return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
                return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                        "error_code": ERR_FFMPEG}
        # 无 ffmpeg 兜底
        if method in ("NONE", ""):
            try:
                nbytes = _concat_init_segs(init_path, seg_paths, dest_path)
                return {"status": "ok", "bytes": nbytes, "path": dest_path,
                        "error": "", "note": "best-effort fMP4 拼接（建议安装 ffmpeg 获得标准 mp4）"}
            except OSError as e:
                return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                        "error_code": ERR_UNKNOWN}
        return {"status": "fail", "bytes": 0, "path": "",
                "error": "fMP4 加密流需要 ffmpeg 才能 remux",
                "error_code": ERR_MISSING_FFMPEG}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _download_fmp4_singlefile(pl: dict, media_url: str, dest_path: str, *,
                              referer: str = "", cookies: dict = None,
                              stop_flag=None, log=None) -> dict:
    """单文件 fMP4（EXT-X-BYTERANGE 切分同一文件）：整份下载 → ffmpeg 重排成标准 .mp4。"""
    dest_path = _mp4_sibling(dest_path)
    enc = pl.get("encryption")
    method = (enc or {}).get("method") or "NONE"
    if method not in ("NONE", ""):
        return {"status": "unsupported", "bytes": 0, "path": "",
                "error": f"单文件 fMP4 已加密({method})，暂不支持",
                "error_code": ERR_ENCRYPT_UNSUP}
    mapp = pl.get("map") or {}
    full_uri = mapp.get("uri") or media_url
    src = dest_path + ".src"
    _remove_if(src)
    res = download_direct(full_uri, src, referer=referer, stop_flag=stop_flag,
                          cookies=cookies)
    if res.get("status") != "ok":
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"单文件 fMP4 整份下载失败：{res.get('error') or '网络异常'}",
                "error_code": res.get("error_code") or ERR_NETWORK}
    try:
        if _locate_ffmpeg():
            rc, tail = _run_ffmpeg(["-hide_banner", "-y", "-i", src,
                                    "-c", "copy", "-movflags", "+faststart", dest_path],
                                   log=log, stop_flag=stop_flag)
            if rc != 0:
                _remove_if(src)
                return {"status": "fail", "bytes": 0, "path": "",
                        "error": f"ffmpeg remux 失败(rc={rc})：{tail.strip()[:200]}",
                        "error_code": ERR_FFMPEG}
            os.remove(src)
            return {"status": "ok", "bytes": os.path.getsize(dest_path),
                    "path": dest_path, "error": ""}
        # 无 ffmpeg：单文件 fMP4 本身多数播放器可播，直接改名保留
        os.replace(src, dest_path)
        return {"status": "ok", "bytes": os.path.getsize(dest_path), "path": dest_path,
                "error": "", "note": "best-effort 单文件 fMP4（建议安装 ffmpeg）"}
    except Exception as e:
        _remove_if(src)
        return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                "error_code": ERR_UNKNOWN}


# ---- DASH(.mpd) ----

def _iso8601_seconds(txt: str) -> float:
    """把 ISO8601 时长(PT...H/M/S, 可含小数)转秒。解析失败返回 0。"""
    if not txt:
        return 0.0
    s = txt.strip().upper()
    if not s.startswith("PT"):
        return 0.0
    import re as _re
    body = s[2:]
    sec = 0.0
    m = _re.match(r"^(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$", body)
    if not m:
        return 0.0
    h, mn, ss = m.groups()
    if h:
        sec += float(h) * 3600
    if mn:
        sec += float(mn) * 60
    if ss:
        sec += float(ss)
    return sec


def _dash_tag(elem) -> str:
    """取 XML 元素的本地标签名（去命名空间）。"""
    return (elem.tag or "").rsplit("}", 1)[-1]


def _dash_children(elem, name):
    return [c for c in list(elem) if _dash_tag(c) == name]


def _dash_first(elem, name):
    ch = _dash_children(elem, name)
    return ch[0] if ch else None


def _dash_attr(elem, name, default=""):
    if elem is None:
        return default
    v = elem.attrib.get(name)
    return v if v is not None else default


def _dash_text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _dash_substitute(tmpl: str, rep_id: str, bandwidth, num, t) -> str:
    """展开 SegmentTemplate 占位符：$RepresentationID$/$Bandwidth$/$Number[%0Nd]$/$Time[%0Nd]$。"""
    def repl(m):
        name = m.group(1)
        width = m.group(3)
        if name == "RepresentationID":
            val = rep_id or ""
        elif name == "Bandwidth":
            val = str(bandwidth or "")
        elif name == "Number":
            val = (f"%0{width}d" % num) if width else str(num)
        elif name == "Time":
            val = (f"%0{width}d" % t) if width else str(t)
        else:
            return m.group(0)
        return val
    return re.sub(r"\$([A-Za-z]+)(%0(\d+)d)?\$", repl, tmpl)


def _dash_join(base: str, rel: str) -> str:
    """把分片/init 相对地址按 RFC3986 解析到 base。
    无 BaseURL 时 base 即 .mpd 自身 URL：urljoin 会正确落到其目录。
    BaseURL 若是目录，DASH 规范要求带尾斜杠，urljoin 同样成立。"""
    if not rel:
        return ""
    if rel.startswith(("http://", "https://")):
        return rel
    return urljoin(base, rel)


def _rep_attr_num(elem, name, default):
    try:
        return int(_dash_attr(elem, name, default))
    except ValueError:
        return default


def _build_dash_part(rep, as_type, tmpl, base, rep_id, bandwidth, total_sec):
    """把单个 Representation 的 SegmentTemplate/SegmentList 展开成分片 URL 列表。
    返回 {"type","init","segs":[url...],"bandwidth"} 或抛 RuntimeError。"""
    # 分片媒体模板
    media_tmpl = _dash_attr(tmpl, "media", "")
    # ET 空元素 bool() 为 False，存在性判断一律用 is not None
    list_el = None
    for _cand in (_dash_first(rep, "SegmentList"), _dash_first(tmpl, "SegmentList")):
        if _cand is not None:
            list_el = _cand
            break
    if list_el is None and _dash_tag(tmpl) == "SegmentList":
        list_el = tmpl   # 容器本身就是 SegmentList
    # init 来源：Initialization 子元素 / template.initialization 属性
    init_url = ""
    init_el = None
    for _cand in (_dash_first(rep, "Initialization"), _dash_first(tmpl, "Initialization")):
        if _cand is not None:
            init_el = _cand
            break
    init_src = _dash_attr(init_el, "sourceURL", "")
    if not init_src:
        init_src = _dash_attr(tmpl, "initialization", "")
    if not init_src:
        init_src = _dash_attr(rep, "initialization", "")
    if init_src:
        init_url = _dash_join(base, _dash_substitute(init_src, rep_id, bandwidth, 1, 0))

    segs = []
    if list_el is not None:
        for su in _dash_children(list_el, "SegmentURL"):
            m = _dash_attr(su, "media", "")
            if m:
                segs.append(_dash_join(base, m))
    elif media_tmpl:
        timescale = _rep_attr_num(tmpl, "timescale", 1) or 1
        start_num = _rep_attr_num(tmpl, "startNumber", 1)
        has_num = "$Number" in media_tmpl
        has_time = "$Time" in media_tmpl
        timeline = _dash_first(tmpl, "SegmentTimeline")
        if timeline is not None:
            t = 0
            num = start_num
            first = True
            for s_el in _dash_children(timeline, "S"):
                sd = _rep_attr_num(s_el, "d", 0)
                r = _rep_attr_num(s_el, "r", 0)
                st = _dash_attr(s_el, "t", "")
                if st:
                    t = int(st)
                elif first:
                    t = 0
                else:
                    pass  # 延续上一段结束时间
                for _k in range(max(0, r) + 1):
                    if has_time and not has_num:
                        url = _dash_join(base, _dash_substitute(media_tmpl, rep_id, bandwidth, num, t))
                    else:
                        url = _dash_join(base, _dash_substitute(media_tmpl, rep_id, bandwidth, num, t))
                    segs.append(url)
                    num += 1
                    t += sd
                first = False
        else:
            dur = _rep_attr_num(tmpl, "duration", 0)
            if dur <= 0:
                raise RuntimeError("SegmentTemplate 无 duration 且无 SegmentTimeline，无法推算分片")
            if total_sec <= 0:
                raise RuntimeError("无法确定 DASH 媒体总时长(mediaPresentationDuration 缺失)")
            count = int((total_sec * timescale + dur - 1) // dur)
            count = max(1, count)
            for k in range(count):
                num = start_num + k
                t = int(k * dur)
                segs.append(_dash_join(base, _dash_substitute(media_tmpl, rep_id, bandwidth, num, t)))
    else:
        raise RuntimeError("Representation 既无 SegmentTemplate 也无 SegmentList")
    if not segs:
        raise RuntimeError("DASH 分片展开为空")
    if len(segs) > VIDEO_MAX_SEGMENTS:
        raise RuntimeError(f"DASH 分片数量过多({len(segs)})，超过上限 {VIDEO_MAX_SEGMENTS}")
    return {"type": as_type, "init": init_url, "segs": segs,
            "bandwidth": bandwidth or 0}


def _dash_adaptation_type(as_el) -> str:
    t = (_dash_attr(as_el, "contentType", "")
         or _dash_attr(as_el, "mimeType", "")).lower()
    cc = _dash_first(as_el, "ContentComponent")
    if cc:
        t = (_dash_attr(cc, "contentType", "") or t).lower()
    if "audio" in t:
        return "audio"
    if "video" in t:
        return "video"
    # 依首个 Representation 的 codecs/mimeType 兜底
    for rep in _dash_children(as_el, "Representation"):
        codec = (_dash_attr(rep, "codecs", "") + " " + _dash_attr(rep, "mimeType", "")).lower()
        if "mp4a" in codec or "audio" in codec:
            return "audio"
        if codec:
            return "video"
    return ""


def parse_dash(text: str, page_url: str) -> dict:
    """解析 DASH .mpd。返回 {"ok", "error", "single"(整文件列表), "video"/"audio"(分段轨)}。
    single 项: {"url", "bandwidth"} 已按质量降序；video/audio 各含 {"init","segs","bandwidth"}。"""
    plan = {"ok": True, "error": "", "single": [], "video": None, "audio": None}
    try:
        root = ET.fromstring(text)
    except Exception as e:
        plan["ok"] = False
        plan["error"] = f".mpd XML 解析失败：{e}"
        return plan
    if _dash_tag(root) != "MPD":
        # 个别站点根节点直接包 MPD
        root = _dash_first(root, "MPD") or root

    total_sec = _iso8601_seconds(_dash_attr(root, "mediaPresentationDuration", ""))
    # 定位首个 Period
    periods = _dash_children(root, "Period")
    period = periods[0] if periods else root

    # 从根/Period 继承 BaseURL
    def _node_base(el, parent_base):
        b = parent_base
        for bu in _dash_children(el, "BaseURL")[:1]:
            txt = _dash_text(bu)
            if txt:
                b = urljoin(parent_base, txt)
        return b

    mpd_base = _dash_attr(root, "baseUri", page_url)
    # Period 自带 duration 则覆盖总时长
    pdur = _dash_attr(period, "duration", "")
    if pdur and not total_sec:
        total_sec = _iso8601_seconds(pdur)
    period_base = _node_base(period, urljoin(mpd_base or page_url, ""))
    # Period 里若没有 BaseURL，Period 级 parent 就是 mpd 的基址
    if not _dash_children(period, "BaseURL"):
        period_base = urljoin(page_url, period_base) if not period_base.startswith("http") else period_base

    singles = []
    parts = []
    adapts = _dash_children(period, "AdaptationSet")
    if not adapts:
        # 极简 mpd：Period 下直接是 Representation
        adapts = [_dash_first(period, "Representation")]
    for as_el in adapts:
        if as_el is None:
            continue
        as_type = _dash_adaptation_type(as_el)
        if not as_type:
            # Representation 直接带 mimeType
            for rep in _dash_children(as_el, "Representation"):
                rt = _dash_adaptation_type(rep)
                if rt:
                    as_type = rt
                    break
            if not as_type:
                continue
        as_base = _node_base(as_el, period_base)
        for rep in _dash_children(as_el, "Representation"):
            rep_id = _dash_attr(rep, "id", "")
            bandwidth = _rep_attr_num(rep, "bandwidth", 0)
            rep_base = _node_base(rep, as_base)
            tmpl = _dash_first(rep, "SegmentTemplate") or _dash_first(as_el, "SegmentTemplate")
            seglist = _dash_first(rep, "SegmentList") or _dash_first(as_el, "SegmentList")
            segbase = _dash_first(rep, "SegmentBase") or _dash_first(as_el, "SegmentBase")
            try:
                # 注意：ET 空元素无子节点时 bool() 为 False，判存在必须用 is not None
                if tmpl is not None or seglist is not None:
                    container = tmpl if tmpl is not None else seglist
                    part = _build_dash_part(rep, as_type, container,
                                            rep_base, rep_id, bandwidth, total_sec)
                    parts.append(part)
                elif rep_base and (segbase is not None or _dash_children(rep, "BaseURL")):
                    # 单文件 DASH：BaseURL 即完整媒体文件
                    singles.append({"url": rep_base, "bandwidth": bandwidth or 0,
                                    "type": as_type})
            except RuntimeError as e:
                plan["ok"] = False
                plan["error"] = str(e)
                return plan
    if singles:
        singles.sort(key=lambda x: x.get("bandwidth") or 0, reverse=True)
        # 去重同一文件
        seen = set()
        uniq = []
        for s in singles:
            if s["url"] not in seen:
                seen.add(s["url"])
                uniq.append(s)
        plan["single"] = uniq
    if parts:
        vids = [p for p in parts if p["type"] == "video"]
        auds = [p for p in parts if p["type"] == "audio"]
        if vids:
            plan["video"] = max(vids, key=lambda p: p["bandwidth"])
        if auds:
            plan["audio"] = max(auds, key=lambda p: p["bandwidth"])
        if not vids and not auds:
            # type 探测失败：取全部作为 video 尝试
            plan["video"] = max(parts, key=lambda p: p["bandwidth"])
    if not singles and not plan.get("video") and not plan.get("audio"):
        plan["ok"] = False
        plan["error"] = ".mpd 内未解析到可用视频/音频轨"
    return plan


def download_dash(mpd_url: str, dest_path: str, *,
                  referer: str = "", timeout: int = VIDEO_TIMEOUT,
                  concurrency: int = VIDEO_CONCURRENT,
                  progress_callback=None, stop_flag=None,
                  cookies: dict = None, log=None) -> dict:
    """下载 DASH(.mpd)。产物 .mp4：有 ffmpeg 时合成音视频轨；
    无 ffmpeg 且只有单路视频轨 → init+分片 拼接尽力而为。返回 status dict。"""
    cookies = _dl_cookies(mpd_url, cookies)
    dest_path = _mp4_sibling(dest_path)
    try:
        txt = _robust_binary(mpd_url, referer, cookies=cookies,
                             min_len=1).decode("utf-8", "ignore")
    except Exception as e:
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"无法获取 .mpd 清单：{e}", "error_code": ERR_HLS_MANIFEST}
    plan = parse_dash(txt, mpd_url)
    if not plan.get("ok"):
        return {"status": "fail", "bytes": 0, "path": "", "error": plan.get("error", ".mpd 解析失败"),
                "error_code": ERR_PARSER}

    has_ffmpeg = bool(_locate_ffmpeg())
    last_err = ""
    last_code = ERR_UNKNOWN
    # 1) 单文件 DASH：整份下载（音视频通常同文件），ffmpeg 重排
    if plan.get("single"):
        for cand in plan["single"]:
            if stop_flag and stop_flag():
                return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
            src = dest_path + ".src"
            _remove_if(src)
            res = download_direct(cand["url"], src, referer=referer,
                                  stop_flag=stop_flag, cookies=cookies)
            if res.get("status") != "ok":
                last_err = res.get("error") or "单文件下载失败"
                last_code = res.get("error_code") or ERR_HOTLINK
                continue
            try:
                if has_ffmpeg:
                    rc, tail = _run_ffmpeg(["-hide_banner", "-y", "-i", src,
                                            "-c", "copy", "-movflags", "+faststart", dest_path],
                                           log=log, stop_flag=stop_flag)
                    if rc == 0 and os.path.getsize(dest_path) > 0:
                        os.remove(src)
                        return {"status": "ok", "bytes": os.path.getsize(dest_path),
                                "path": dest_path, "error": ""}
                    raise RuntimeError(f"ffmpeg rc={rc}：{tail.strip()[:150]}")
                # 无 ffmpeg：文件本身是 mp4 分片单文件，直接改名（尽力而为）
                os.replace(src, dest_path)
                return {"status": "ok", "bytes": os.path.getsize(dest_path), "path": dest_path,
                        "error": "", "note": "best-effort 单文件 DASH（建议安装 ffmpeg）"}
            except Exception as e:
                _remove_if(src)
                last_err = str(e)
                last_code = ERR_FFMPEG
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"单文件 DASH 全部尝试失败：{last_err}",
                "error_code": last_code}

    # 2) 分段 DASH（视频/音频轨）
    video = plan.get("video")
    audio = plan.get("audio")
    if not video:
        return {"status": "fail", "bytes": 0, "path": "", "error": "无视频轨可下载",
                "error_code": ERR_NO_VIDEO_TRACK}
    if audio and not has_ffmpeg:
        return {"status": "unsupported", "bytes": 0, "path": "",
                "error": "该 DASH 含独立音轨，合成单文件需 ffmpeg，请先安装",
                "error_code": ERR_MISSING_FFMPEG}

    tmpdir = dest_path + f".parts_{hashlib.md5(mpd_url.encode('utf-8', 'ignore')).hexdigest()[:8]}"
    os.makedirs(tmpdir, exist_ok=True)
    try:
        # 视频轨与音频轨并行抓取（各自内部仍走滑窗泵）；总在途连接不超 concurrency，保持克制
        tracks = [("video", video)]
        if audio:
            tracks.append(("audio", audio))
        per_track = concurrency if len(tracks) == 1 else max(1, concurrency // len(tracks))
        downloaded = [None] * len(tracks)

        def _fetch_track(job):
            tag, tr = job
            paths = _dl_raw_list([{"uri": u} for u in tr["segs"]], tmpdir, tag,
                                 referer, cookies, concurrency=per_track,
                                 stop_flag=stop_flag, progress_callback=progress_callback,
                                 label=f"DASH{tag}")
            init_p = None
            if tr.get("init"):
                init_p = _fetch_one_raw({"uri": tr["init"]}, tmpdir,
                                        f"{tag}_init.mp4", referer, cookies)
            return tag, init_p, paths

        with ThreadPoolExecutor(max_workers=len(tracks)) as tp:
            futs = [tp.submit(_fetch_track, j) for j in tracks]
            for i, f in enumerate(futs):
                downloaded[i] = f.result()      # 失败/停止原样抛出（stop 由外层归因）
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}

        if has_ffmpeg:
            pls = []
            roles = []
            for tag, init_p, paths in downloaded:
                pl_path = os.path.join(tmpdir, f"{tag}.m3u8")
                _write_local_hls(pl_path, tmpdir, paths, init_path=init_p,
                                 durations=[0.0] * len(paths))
                pls.append(pl_path)
                roles.append(tag)      # "video"/"audio" → _mux_local_hls 按角色 -map
            rc, tail = _mux_local_hls(pls, roles, dest_path,
                                      log=log, stop_flag=stop_flag)
            if rc != 0:
                return {"status": "fail", "bytes": 0, "path": "",
                        "error": f"ffmpeg DASH mux 失败(rc={rc})：{tail.strip()[:300]}",
                        "error_code": ERR_FFMPEG}
            return {"status": "ok", "bytes": os.path.getsize(dest_path),
                    "path": dest_path, "error": ""}
        # 无 ffmpeg：仅单视频轨拼接兜底
        if not audio:
            # 结果并行抓取后被收进 downloaded（见上 _fetch_track）；旧变量名 v_init/v_paths
            # 在改成并行那轮就没了，这里一直是 NameError。走到本分支必定 audio 为空 →
            # tracks 只有 ("video", …) 一条，故取 downloaded[0]。
            _, v_init, v_paths = downloaded[0]
            try:
                nbytes = _concat_init_segs(v_init, v_paths, dest_path)
                return {"status": "ok", "bytes": nbytes, "path": dest_path,
                        "error": "", "note": "best-effort DASH 视频拼接（建议安装 ffmpeg）"}
            except OSError as e:
                return {"status": "fail", "bytes": 0, "path": "", "error": str(e),
                        "error_code": ERR_UNKNOWN}
        return {"status": "fail", "bytes": 0, "path": "",
                "error": "无 ffmpeg 且含音轨，无法合成",
                "error_code": ERR_MISSING_FFMPEG}
    except Exception as e:
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        return {"status": "fail", "bytes": 0, "path": "", "error": f"DASH 下载失败：{e}",
                "error_code": ERR_HLS_SEGMENT}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ================================================================
# 落盘辅助 / 元数据 JSON
# ================================================================

def ensure_unique_path(path: str) -> str:
    """重名时追加 -2/-3，避免覆盖已有文件。"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root}-{i}{ext}"):
        i += 1
    return f"{root}-{i}{ext}"


def write_metadata_json(video_dir: str, session_records: list, session_file: str = None) -> str:
    """写元数据 JSON 到 video_dir/video_<TIME_FORMAT>.json。
    session_file 已存在时合并 items 幂等重写。返回文件绝对路径。"""
    items = []
    ts = time.strftime(TIME_FORMAT)
    if session_file and os.path.exists(session_file):
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                old = json.load(f)
            items = list(old.get("items") or [])
        except Exception:
            items = []
    if not session_file:
        session_file = os.path.join(video_dir, f"video_{ts}.json")
    items.extend(session_records)
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}
    try:
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[video_grab] 元数据写入失败: {e}")
    return session_file


# ================================================================
# 任务编排
# ================================================================

def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _dest_path_for(video_dir: str, title: str, media_url: str, ext: str, num: int) -> str:
    base = safe_name(title or "")
    # 标题为空或纯中文被映射成 "-" 时，改用媒体 URL 的文件名主体，避免出现 "-.mp4"
    if not base or set(base) <= {"-", "_", "."}:
        path = urlparse(media_url).path
        stub = os.path.basename(path)
        stub = re.sub(r"\.(m3u8|ts|mp4|flv|webm|mov|m4v|mkv)$", "", stub, flags=re.I)
        base = safe_name(stub or "video")
    if not base or set(base) <= {"-", "_", "."}:
        base = "video"
    if len(base) > 80:
        base = base[:80]
    if num > 1:
        base = f"{base}-{num}"
    ext = ext or "mp4"
    return ensure_unique_path(os.path.join(video_dir, f"{base}.{ext}"))


def process_single_page(url: str, *, video_dir: str = VIDEO_DIR,
                        use_browser: bool = False,
                        progress_callback=None, log_callback=None,
                        stop_flag=None) -> dict:
    """单个页面：探测 → 逐个下载全部候选 → 返回记录。"""
    page = {"url": url, "title": "", "error": "", "candidates": [], "records": []}

    def log(msg):
        if log_callback:
            try:
                log_callback(str(msg))
            except Exception:
                pass

    html, final_url = fetch_html(url)
    title = extract_title(html, final_url or url)
    page["title"] = title
    mmeta = extract_video_metadata(html, final_url or url)   # #7 出片打标用（best-effort）

    cands = []
    if html:
        cands = _resolve_candidates(url, html0=html, final0=final_url or url, log=log)
    if not cands and use_browser:
        log(f"静态解析无结果，尝试浏览器抓流（将自动播放/点击并追踪播放器页）：{url}")
        try:
            driver = _acquire_browser(url)   # 任务档：站点覆盖/fixed/auto → Chrome 呈现同一档 UA
            try:
                cands, bnotes = detect_videos_browser(url, driver)
                for n in bnotes:
                    log(n)
                if not cands:
                    page["error"] = "浏览器抓流未找到可直接下载的 m3u8/直链（可能是加密流/MSE 或需登录）"
            finally:
                _release_browser(driver)
        except Exception as e:
            log(f"浏览器抓流失败：{e}")

    # 供 UI/元数据展示用的候选（剔除 cookies，避免把登录态写进 JSON）
    page["candidates"] = [{k: v for k, v in asdict(c).items() if k != "cookies"}
                          for c in cands]
    if not cands:
        page["error"] = page["error"] or "未解析到视频(m3u8/直链)"
        log(f"{url} → {page['error']}")
        return page

    # L0/每任务档：整页一档。浏览器源候选已带捕获身份(ua 真值)优先；纯 requests 源候选无身份 →
    # 由 _task_profile(url)（站点覆盖>fixed>auto 每任务抽一档）合成任务身份，整页下载同源。
    task_ident = _synthetic_task_identity(_task_profile(url)) if not any(
        getattr(c, "identity", None) is not None for c in cands) else None

    total = len(cands)
    log(f"✅ 页面解析到 {total} 个视频候选，开始下载：{title}")

    titles = {c.url: title for c in cands}
    name_num = {}
    for c in cands:
        if stop_flag and stop_flag():
            c.status = "stopped"
            page["records"].append({k: v for k, v in asdict(c).items() if k != "cookies"})
            continue
        num = name_num.get(c.url, 0) + 1
        name_num[c.url] = num
        referer = c.page_url or url          # 用流实际所在页做 Referer（防盗链更稳）
        if c.kind == "m3u8":
            fmt = probe_m3u8_format(c.url, referer=referer)   # TS 或 fMP4/CMAF → .ts/.mp4
            dest_ext = "mp4" if fmt == "cmaf" else "ts"
        elif c.kind == "dash":
            dest_ext = "mp4"
        else:
            dest_ext = c.ext or "mp4"
        dest = _dest_path_for(video_dir, titles[c.url], c.url, dest_ext, num)
        log(f"[{c.index}/{total}] {c.kind:5s} 下载中 → {os.path.basename(dest)}")

        def cb(cur, tot, extra, _c=c):
            if progress_callback:
                progress_callback(cur, tot, f"{_c.kind} {os.path.basename(dest)} {extra}")

        cookies = c.cookies or None
        try:
            # L0：浏览器源候选带身份 → 下载全程 ambient 同源（页 Chrome 打开者 UA/语言/时区 贯穿）；
            # 静态源候选无身份 → nullcontext 维持旧随机（零回归）
            with _identity_ambient(getattr(c, "identity", None) or task_ident):
                if c.kind == "m3u8":
                    res = download_m3u8(c.url, dest, referer=referer,
                                        progress_callback=cb, stop_flag=stop_flag,
                                        cookies=cookies)
                elif c.kind == "dash":
                    res = download_dash(c.url, dest, referer=referer,
                                        progress_callback=cb, stop_flag=stop_flag,
                                        cookies=cookies)
                else:
                    res = download_direct(c.url, dest, referer=referer,
                                          progress_callback=cb, stop_flag=stop_flag,
                                          cookies=cookies)
            c.status = res.get("status", "fail")
            c.error = res.get("error", "")
            c.local_path = res.get("path", "") or dest
            c.bytes_count = res.get("bytes", 0)
        except Exception as e:
            c.status = "fail"
            c.error = str(e)

        if c.status == "ok":
            log(("   ✓ " + os.path.basename(c.local_path) + f" {_fmt_size(c.bytes_count)}"))
            # #7 出片增强：容器(extension)允许且页面给了 title/封面才动文件；soft-fail
            _maybe_tag_video(c.local_path, mmeta, mmeta.get("cover_url", ""),
                             referer=referer, cookies=cookies, log=log)
        elif c.status == "stopped":
            log("   已停止")
        else:
            log(f"   ✗ {c.error or '下载失败'}")
        page["records"].append({k: v for k, v in asdict(c).items() if k != "cookies"})

    return page


def run_batch(urls: list, *, video_dir: str = VIDEO_DIR,
              use_browser: bool = False,
              progress_callback=None, log_callback=None,
              stop_flag=None) -> list:
    """批量任务（worker 线程调用）：URL 逐个串行，全部结束后统一写元数据 JSON。"""
    def log(msg):
        if log_callback:
            try:
                log_callback(str(msg))
            except Exception:
                pass

    pages = []
    log(f"视频抓取任务开始，共 {len(urls)} 个 URL")
    session_records = []
    session_file = os.path.join(video_dir, f"video_{time.strftime(TIME_FORMAT)}.json")

    for i, u in enumerate(urls, start=1):
        if stop_flag and stop_flag():
            log("已停止（独立停止标志，不影响主爬虫）")
            break
        log(f"\n[{i}/{len(urls)}] 页面：{u}")
        page = process_single_page(u, video_dir=video_dir, use_browser=use_browser,
                                   progress_callback=progress_callback,
                                   log_callback=log_callback, stop_flag=stop_flag)
        pages.append(page)
        session_records.extend(page["records"])

    meta_path = write_metadata_json(video_dir, session_records, session_file)
    ok = sum(1 for p in pages for r in p["records"] if r.get("status") == "ok")
    fail = sum(1 for p in pages for r in p["records"] if r.get("status") == "fail")
    skip = sum(1 for p in pages for r in p["records"] if r.get("status") in ("skipped", "unsupported"))
    stopped = sum(1 for p in pages for r in p["records"] if r.get("status") == "stopped")
    log(f"全部完成：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}，元数据见 {meta_path}")
    return {"pages": pages, "ok": ok, "fail": fail, "skip": skip,
            "stopped": stopped, "meta_path": meta_path}


# ================================================================
# 【整部动漫抓取 v1.2】— 详情页/分集页 → 识别全部分集列表 → 按线路自动故障切换逐集下载
# 1) 优先 MacCMS 模板精确识别 /v/{vodid}-{线路}-{集}/；识别失败退回通用锚点启发式。
# 2) 每"集"按线路顺序(1→2→3…)逐个尝试对应分集页，全部候选下载失败才切下一条线路，
#    成功即停，保证每集只保留一个可用文件。
# 3) 落盘到 {video_dir}/{动漫名}/第NN集.{ts|mp4}，文件已存在则跳过（天然支持断点续抓）。
# 本区块为纯后端扩展，不依赖 Qt，不修改任何既有采集/单页下载逻辑。
# ================================================================

_EP_LABEL_MAX = 60            # 分集文件标签长度上限
_SERIES_TITLE_MAX = 60        # 动漫目录名长度上限
_SERIES_ANCHOR_MIN = 2        # 少于该数量同类锚点则不视为分集列表

# 识别"第X集 / 第X话 / EPx"等分集文本标记（按优先级）
_EP_TEXT_RES = [
    re.compile(r"第\s*(\d{1,4})\s*[集话話话]"),
    re.compile(r"第\s*(\d{1,4})"),
    re.compile(r"^\s*(\d{1,4})\s*[集话話话]"),
    re.compile(r"(?<![A-Za-z0-9])(?:ep|EP)\s*\.?\s*[:：\-]?\s*(\d{1,4})(?![A-Za-z0-9])"),
]
# MacCMS 模板链接：/<模块>/<vodid>-<线路>-<集>/
_MACCMS_LINK_RE = re.compile(r"^/?([A-Za-z0-9_]+)/(\d{1,9})-(\d{1,4})-(\d{1,4})/?$")
# 播放器"上一集/下一集/播放/预告…"等导航锚点（文本本身不含集号，仅靠路径归入某集，需剔除，
# 否则用非第1集当种子页时，会把某集标签污染成"下一集"）
_NAV_EP_RE = re.compile(r"上一[集话話]|下一[集话話]|播放|正片|预告|預告|简介|簡介|全集|更新|首播|加載|加载", re.I)


def _fs_clean(name: str, max_len: int = 80) -> str:
    """Windows 安全文件名：保留 CJK（safe_name 会把中文压成 '-'，不适用目录/分集名）。
    剔除路径非法字符与控制符，压空白，去除首尾点/空格。"""
    if not name:
        return ""
    s = str(name).strip()
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s


def _clean_series_title(title: str) -> str:
    """播放页/详情页标题 → 动漫名：去掉"- 站点名/分类"尾巴与" 第01集"部分。
    e.g. "国漫之女子非常可爱 第01集 - 国产动漫超清在线观看 - 天天动漫" → "国漫之女子非常可爱"。"""
    t = (title or "").strip()
    t = re.split(r"\s*[-–]\s*", t, maxsplit=1)[0]   # "-" / "–" 分隔
    m = re.search(r"第\s*\d{1,4}\s*[集话話]|_?第\d{1,4}集| · 第\d+集", t)
    if m:
        t = t[:m.start()]
    return re.sub(r"\s+", " ", t).strip(" .-_—")


def _ep_num_from_text(text: str):
    """从锚点文本识别分集号，识别不出返回 None。"""
    for rx in _EP_TEXT_RES:
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


def _collect_anchors(html: str, page_url: str):
    """收集页面所有 <a>：去标签取文本、urljoin、识别分集号。返回 dict 列表。
    href 缺失时兜底取 data-href / data-url / data-link（JS 渲染后常驻留的
    动态链接属性），urljoin 语义与 href 一致。"""
    out = []
    _A_LINK_ATTRS = ("href", "data-href", "data-url", "data-link")
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.I | re.S):
        attrs, inner = m.group(1), m.group(2)
        href = ""
        for name in _A_LINK_ATTRS:
            hm = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name,
                           attrs, re.I)
            if hm:
                href = next((g for g in hm.groups() if g), "")
                break
        if not href:
            continue
        text = _html_mod.unescape(re.sub(r"<[^>]+>", " ", inner))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        u = _normalize(href, page_url)
        if not u or not u.startswith(("http://", "https://")):
            continue
        out.append({"text": text, "url": u, "path": urlparse(u).path or "/",
                    "num": _ep_num_from_text(text)})
    return out


def _path_fp(path: str) -> str:
    """URL 路径指纹：纯数字段替换为 @，用于通用启发式给"同一族"分集链接聚类。"""
    segs = []
    for s in (x for x in path.rstrip("/").split("/") if x):
        s = re.sub(r"\.(html?|htm|php)$", "", s, flags=re.I)
        segs.append(re.sub(r"\d+", "@", s))
    return "/".join(segs)


def _empty_plan(page_url, title, notes, reason):
    return {"title": title, "url": page_url, "matched": "none",
            "episodes": [], "lines": 0, "notes": list(notes), "reason": reason}


def parse_episode_plan(html: str, page_url: str) -> dict:
    """从任一详情页/分集播放页 HTML 识别整部分集。
    返回 dict：title / url / matched("maccms"|"generic"|"none") /
              episodes=[{num(int|None),label,url,line}]（line 从1起，None=未分线路）/
              lines / notes / reason。"""
    title = _clean_series_title(extract_title(html, page_url))
    # 站点适配器可直接给分集清单（SPA 索引页无 <a> 锚点族时靠它枚举整部）
    host = (urlparse(page_url).netloc or "").lower()
    ad = _apply_adapters(host, html, page_url) if html else {}
    ad_eps = ad.get("episodes") or []
    if ad_eps:
        eps, seen = [], set()
        for a in ad_eps:
            u = a.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            eps.append({"num": a.get("num"), "label": a.get("label") or "",
                        "url": u, "line": a.get("line")})
        if eps:
            return {"title": title, "url": page_url, "matched": "adapter",
                    "episodes": eps, "lines": 1,
                    "notes": ["站点适配器给出分集清单"], "reason": ""}
    anchors = _collect_anchors(html, page_url)
    if not anchors:
        return _empty_plan(page_url, title, [], "页面无任何链接")
    return _plan_from_anchors(anchors, page_url, title)


def _plan_from_anchors(anchors: list, page_url: str, title: str) -> dict:
    """由已收集的分集锚点列表识别整部（macs/gens 两路判定核心）。
    与 parse_episode_plan 共用同一套语义：静态 HTML 与浏览器快照收集到的
    锚点都汇入这里判定，保证两种入口结果一致。anchors 元素需含
    path/url/text/num（_collect_anchors 产出）。"""
    notes = []
    if not anchors:
        return _empty_plan(page_url, title, notes, "页面无任何链接")

    pg_path = urlparse(page_url).path.rstrip("/")
    pm = _MACCMS_LINK_RE.match(pg_path)
    macs, gens = [], []
    for a in anchors:
        mac = _MACCMS_LINK_RE.match(a["path"].rstrip("/"))
        if mac:
            a["mac"] = mac
            a["txt_num"] = a["num"]                         # 文本识别的集号（可能为 None）
            a["group"] = (mac.group(1), mac.group(2))       # (模块, vodid)
            a["line"] = int(mac.group(3))
            a["nid"] = int(mac.group(4))
            if a["num"] is None:
                a["num"] = a["nid"]                         # 无数集文本时退回路径集号
            macs.append(a)
        elif a["num"] is not None:
            a["line"] = None
            a["group"] = _path_fp(a["path"])
            gens.append(a)

    episodes = []
    if macs:
        # 选定主动漫：优先当前页自身的(模块,vodid)，否则取锚点最多的一组
        cnt = {}
        for a in macs:
            cnt[a["group"]] = cnt.get(a["group"], 0) + 1
        target = None
        if pm:
            cand = (pm.group(1), pm.group(2))
            if cand in cnt:
                target = cand
        if target is None:
            target = max(cnt.items(), key=lambda kv: kv[1])[0]
        lines = {}
        for a in macs:
            if a["group"] == target:
                lines.setdefault(a["line"], []).append(a)
        for line in sorted(lines):
            arr = sorted(lines[line], key=lambda x: (x["num"], x["nid"]))
            seen_url = set()
            for a in arr:
                if a.get("txt_num") is None and _NAV_EP_RE.search(a["text"]):
                    continue                                # 剔除"上一/下一集"等导航锚点
                if a["url"] in seen_url:      # 同一页会有"播放器快捷选集+完整列表"两份重复
                    continue
                seen_url.add(a["url"])
                episodes.append({"num": a["num"], "label": a["text"],
                                 "url": a["url"], "line": line})
        if episodes:
            return {"title": title, "url": page_url, "matched": "maccms",
                    "episodes": episodes, "lines": len(lines),
                    "notes": notes, "reason": ""}

    # 通用启发式：按路径指纹聚类，选锚点最多的一族（要求半数以上含分集号）
    if gens:
        fam = {}
        for a in gens:
            fam.setdefault(a["group"], []).append(a)
        best, best_key = None, None
        for g, arr in fam.items():
            if len(arr) < _SERIES_ANCHOR_MIN:
                continue
            numy = sum(1 for x in arr if x["num"] is not None)
            if numy * 2 < len(arr):        # 多数无分集号 → 不是分集列表（可能是站内导航）
                continue
            if best is None or (len(arr), numy) > best:
                best = (len(arr), numy)
                best_key = g
        if best_key:
            seen = set()
            for a in sorted(fam[best_key], key=lambda x: (x["num"] if x["num"] is not None else 10 ** 9)):
                if a["url"] in seen:
                    continue
                seen.add(a["url"])
                episodes.append({"num": a["num"], "label": a["text"],
                                 "url": a["url"], "line": None})
            if episodes:
                return {"title": title, "url": page_url, "matched": "generic",
                        "episodes": episodes, "lines": 1,
                        "notes": notes, "reason": ""}

    notes.append("未识别到分集列表链接")
    return _empty_plan(page_url, title, notes, "未识别到分集列表链接")


def _browser_probe(url: str, log) -> list:
    """静态解析无果时用浏览器抓流（复用 qiongqi_maw 驱动池），返回候选列表。"""
    try:
        driver = _acquire_browser(url)   # 分集页任务档：浏览器呈现与后续下载同档 UA
        try:
            cands, bnotes = detect_videos_browser(url, driver)
            for n in bnotes:
                log(n)
            return cands
        finally:
            _release_browser(driver)
    except Exception as e:
        log(f"浏览器抓流失败：{e}")
        return []


# 展开"加载更多 / 展开全部选集"类触发器的 JS：滚动到底并尝试点击常见展开控件。
# 用于整部抓取的浏览器补全：让 JS 惰性渲染/分页加载的分集列表先落到 DOM。
_EXPAND_JS = r"""
(function(){
    function visClick(sel){
        var els = document.querySelectorAll(sel);
        var done = 0;
        for (var i = 0; i < els.length; i++){
            var el = els[i];
            if (el && el.offsetParent !== null){
                try { el.click(); done++; } catch(e){}
            }
        }
        return done;
    }
    var clicks = 0;
    window.scrollTo(0, document.body.scrollHeight);
    var sels = [
        '.episode-more','.load-more','.expand-more','.more-episode',
        '[class*="loadmore"]','[class*="load-more"]','[class*="more-btn"]',
        '[class*="expand"] button','.btn-more','.ant-collapse-header',
        '[class*="episode"] [class*="more"]'
    ];
    for (var i = 0; i < sels.length; i++){ clicks += visClick(sels[i]); }
    window.scrollTo(0, document.body.scrollHeight);
    return clicks;
})()
"""


def _browser_episode_plan(seed_url: str, log=None) -> dict:
    """静态整部识别失败时，用浏览器补全分集列表：滚动/点击"展开/加载更多"
    触发器 → 多轮快照当前 DOM → 收集 <a>(含 data-href) 锚点 → 汇入与静态
    完全一致的 _plan_from_anchors 判定。只收分集锚点，不下载。
    返回形态与 parse_episode_plan 一致（matched ∈ maccms/generic/none）。"""
    def logmsg(m):
        try:
            if log:
                log(str(m))
        except Exception:
            pass
    try:
        driver = _acquire_browser(seed_url)   # 详情页任务档：补充分集也呈现该站覆盖档
    except Exception as e:
        logmsg(f"浏览器补充分集列表失败（驱动不可用）：{e}")
        return _empty_plan(seed_url, "", [], "浏览器驱动不可用")
    try:
        try:
            driver.get(seed_url)
        except Exception as e:
            logmsg(f"浏览器打开详情页失败：{e}")
            return _empty_plan(seed_url, "", [], f"浏览器打开失败：{e}")
        time.sleep(1.2)
        seen_url = set()
        anchors_all = []
        last_n = 0
        stable = 0
        last_html = ""
        for _ in range(6):                       # 多轮直到锚点稳定
            try:
                _safe_exec(driver, _EXPAND_JS)
                time.sleep(0.8)
            except Exception:
                pass
            html = _safe_exec(driver, "return document.documentElement.outerHTML;")
            if isinstance(html, str) and html:
                last_html = html
                for a in _collect_anchors(html, seed_url):
                    if a["url"] not in seen_url:
                        seen_url.add(a["url"])
                        anchors_all.append(a)
            if len(anchors_all) == last_n:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_n = len(anchors_all)
        title = _clean_series_title(
            extract_title(last_html or "", seed_url) or getattr(driver, "title", None) or "")
        logmsg(f"整部·浏览器收集到 {len(anchors_all)} 个分集锚点")
        return _plan_from_anchors(anchors_all, seed_url, title)
    except Exception as e:
        logmsg(f"浏览器补充分集列表异常：{e}")
        return _empty_plan(seed_url, "", [], f"浏览器解析异常：{e}")
    finally:
        try:
            _release_browser(driver)
        except Exception:
            pass


def _media_rec(video: VideoCandidate, status: str, local_path: str,
               bytes_count: int, error: str) -> dict:
    return {"kind": video.kind, "url": video.url, "ext": video.ext,
            "src": video.src, "page_url": video.page_url, "status": status,
            "local_path": local_path, "bytes_count": bytes_count, "error": error}


# ================================================================
# #7 video 成品元数据 / 封面打标（对等音频 _maybe_tag_file；best-effort soft-fail）
# ================================================================

_VIDEO_META_LD_TYPES = {"videoobject", "movie", "tvepisode", "episode"}
_VIDEO_TAG_EXTS = (".mp4", ".mov", ".m4v", ".mkv")     # 其余容器(ts/flv/webm…)暂不支持不打标
_MOV_FAMILY = (".mp4", ".mov", ".m4v")


def extract_video_metadata(html: str, page_url: str) -> dict:
    """页面 → video 元数据（title / artist / description / cover_url）。JSON-LD
    VideoObject/Movie/TVEpisode 优先，og:/twitter 兜底；cover_url 归一化成绝对 URL。
    只作 best-effort（无匹配返回空 dict），绝不影响抓取主流程。"""
    meta = {"title": "", "artist": "", "description": "", "cover_url": ""}
    if not html:
        return meta

    def _pick(node, *keys):
        for k in keys:
            v = node.get(k)
            if isinstance(v, dict):
                v = _pick(v, "name", "url")
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, list) and v:
                v = v[0]
                if isinstance(v, dict):
                    v = _pick(v, "name", "url")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    def _name_of(node):
        if isinstance(node, dict):
            return _pick(node, "name", "title")
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return _name_of(node[0])
        return ""

    # ① JSON-LD：VideoObject/Movie/TVEpisode（name/author/contentUrl/image）
    for m in re.finditer(
            r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                types = node.get("@type")
                if isinstance(types, list):
                    types = [str(x).lower() for x in types if x]
                else:
                    types = [str(types).lower()] if types else []
                if not (types and (set(types) & _VIDEO_META_LD_TYPES)):
                    stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                    continue
                if not meta["title"]:
                    meta["title"] = _pick(node, "name", "headline", "title")
                if not meta["artist"]:
                    meta["artist"] = _name_of(node.get("author") or node.get("creator")
                                             or node.get("director"))
                if not meta["description"]:
                    meta["description"] = _pick(node, "description", "abstract")
                if not meta["cover_url"]:
                    img = node.get("image") or node.get("thumbnailUrl")
                    if isinstance(img, dict):
                        meta["cover_url"] = _pick(img, "url", "contentUrl", "thumbnailUrl")
                    elif isinstance(img, str):
                        meta["cover_url"] = img.strip()
            elif isinstance(node, list):
                stack.extend(node)

    # ② og:/twitter: 兜底（title/description/cover）
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        tag = m.group(0)
        pm = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if not (pm and cm):
            continue
        prop = pm.group(1).strip().lower()
        val = _safe_unescape(cm.group(1)).strip()
        if not val:
            continue
        if prop in ("og:title", "twitter:title") and not meta["title"]:
            meta["title"] = val
        elif prop in ("og:description", "twitter:description") and not meta["description"]:
            meta["description"] = val
        elif prop in ("og:image", "og:image:url", "og:image:secure_url",
                      "twitter:image") and not meta["cover_url"]:
            meta["cover_url"] = val

    if meta["cover_url"]:
        meta["cover_url"] = _normalize(meta["cover_url"], page_url) or meta["cover_url"]
    meta["title"] = meta["title"][:200]
    meta["artist"] = meta["artist"][:120]
    meta["description"] = meta["description"][:300]
    return meta


def _maybe_tag_video(out_path: str, meta: dict, cover_url: str = "",
                     referer: str = "", cookies: dict = None, log=None) -> str:
    """对视频成品做 best-effort 元数据 + 封面写入（#7，对等音频 _maybe_tag_file）。
    两档降级：① metadata + attached_pic 封面；② 封面档失败降纯 metadata 档。
    无 title/artist/description 且无封面 → 跳过。soft-fail，绝不删原文件。"""
    if not out_path or not os.path.exists(out_path):
        return out_path
    if not getattr(_CFG, "VIDEO_TAG_ENABLED", True):
        return out_path
    ext = os.path.splitext(out_path)[1].lower()
    if ext not in _VIDEO_TAG_EXTS:                       # ts/flv/webm 等不支持 → 不动
        return out_path
    meta = meta or {}
    tags = {}
    for k, label in (("title", "title"), ("artist", "artist"),
                     ("description", "comment")):
        v = str(meta.get(k) or "").strip()
        if v:
            tags[label] = v
    if not tags and not cover_url:
        return out_path
    if not _locate_ffmpeg():
        return out_path
    dirn = os.path.dirname(out_path) or "."
    base = os.path.basename(out_path)
    # 临时名保留容器扩展，否则 ffmpeg 无法按扩展名猜输出容器
    tagtmp = os.path.join(dirn, base + ".tagtmp" + ext)
    art_path = ""
    _remove_if(tagtmp)
    if cover_url:                                        # 封面 best-effort 抓取（超限/失败弃）
        art_path = os.path.join(dirn, "." + base + ".art")
        _remove_if(art_path)
        try:
            art = download_direct(cover_url, art_path, referer=referer, cookies=cookies)
            if art.get("status") != "ok":
                _remove_if(art_path)
                art_path = ""
            else:
                try:
                    _cap = int(getattr(_CFG, "VIDEO_COVER_MAX_BYTES", 2 * 1024 * 1024))
                    if not (0 < os.path.getsize(art_path) <= _cap):
                        _remove_if(art_path)
                        art_path = ""
                except OSError:
                    _remove_if(art_path)
                    art_path = ""
        except Exception:
            _remove_if(art_path)
            art_path = ""

    def _meta_args():
        return [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]

    def _movflags():
        return ["-movflags", "+faststart"] if ext in _MOV_FAMILY else []

    def _commit(tmp):
        """tmp 生成成功 → 原子替换回 out_path；失败清理。返回 bool。"""
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            try:
                os.replace(tmp, out_path)
                return True
            except OSError:
                pass
        _remove_if(tmp)
        return False

    ok = False
    if art_path:                                          # 档① metadata + 封面
        args = (["-hide_banner", "-y", "-i", out_path, "-i", art_path,
                 "-map", "0:v:0", "-map", "0:a?", "-map", "1:v:0",
                 "-c:v:0", "copy", "-c:a", "copy", "-c:v:1", "mjpeg",
                 "-disposition:v:1", "attached_pic",
                 "-metadata:s:v:1", "title=Album cover"]
                + _movflags() + _meta_args() + [tagtmp])
        try:
            rc, _tail = _run_ffmpeg(args, log=None, stop_flag=None)
        except Exception:
            rc = 1
        ok = rc == 0 and _commit(tagtmp)
    if not ok:                                             # 档② 纯 metadata
        args = (["-hide_banner", "-y", "-i", out_path,
                 "-map", "0:v:0", "-map", "0:a?", "-c:v:0", "copy", "-c:a", "copy"]
                + _movflags() + _meta_args() + [tagtmp])
        try:
            rc, _tail = _run_ffmpeg(args, log=None, stop_flag=None)
        except Exception:
            rc = 1
        ok = rc == 0 and _commit(tagtmp)
    _remove_if(art_path)
    if ok and log:
        try:
            log(f"   已写视频元数据/封面：{', '.join(tags) or 'cover'}")
        except Exception:
            pass
    return out_path


_VIDEO_EP_META_LOCK = threading.Lock()
_VIDEO_EP_META: dict = {}


def _episode_meta_video(page_url: str) -> dict:
    """整部路径：单集页 → video 元数据（进程级缓存；tag 开启且首次遇到该页才拉页）。
    任何失败返回 {}（打标 soft-fail，不影响下载）。"""
    if not getattr(_CFG, "VIDEO_TAG_ENABLED", True):
        return {}
    if not page_url:
        return {}
    k = _canon_key(page_url)
    with _VIDEO_EP_META_LOCK:
        if k in _VIDEO_EP_META:
            return _VIDEO_EP_META[k]
    m = {}
    try:
        h, f = fetch_html(page_url, timeout=int(getattr(_CFG, "VIDEO_TIMEOUT", 20)))
        if h:
            m = extract_video_metadata(h, f or page_url)
    except Exception:
        m = {}
    with _VIDEO_EP_META_LOCK:
        _VIDEO_EP_META[k] = m
    return m


_VIDEO_OUT_EXTS = ("ts", "mp4", "flv", "webm", "mov", "m4v", "mkv")


def _find_existing_video(folder: str, base: str):
    """同 base 任一扩展名已有成品则返回其路径（支持 TS↔MP4 格式切换后断点续抓）。"""
    for ext in _VIDEO_OUT_EXTS:
        p = os.path.join(folder, f"{base}.{ext}")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def _cand_output_ext(v, referer: str = "") -> str:
    """候选 → 落盘扩展名（整部路径）。m3u8 先按 .ts 落盘：fMP4/CMAF 下载完成会
    自动改名成 .mp4（见 _mp4_sibling / try_download 的 real 处理），
    不必再多抓一次清单猜扩展名。单页路径的探测保留在 process_single_page。"""
    if v.kind == "m3u8":
        return "ts"
    if v.kind == "dash":
        return "mp4"
    return v.ext or "mp4"


def _download_episode(cands, folder: str, base: str, *,
                      use_browser: bool = False, skip_existing: bool = True,
                      log=None, stop_flag=None) -> dict:
    """逐条线路尝试把某集下载到 folder/{base}.{ext}。
    cands=[{num,label,url,line},...] 已按线路优先级排序。
    返回 record dict（status: ok/fail/stopped/unsupported）。"""
    def logmsg(m):
        try:
            if log:
                log(str(m))
        except Exception:
            pass

    retry = max(0, VIDEO_RETRY)
    attempts_done = 0
    last_fail = {"code": ERR_UNKNOWN}        # 供所有线路失败时的兜底归因

    # 每集任务档：浏览器源候选带捕获身份(ua 真值)优先；纯静态候选无身份 → 按集(页)抽一档合成任务身份，
    # 该集所有线路/所有尝试同一档（跨页缓存，杜绝逐候选再随机 → 跨请求不同源）。
    _task_cache = {}

    def _episode_task_ident(page):
        if page not in _task_cache:
            _task_cache[page] = _synthetic_task_identity(_task_profile(page))
        return _task_cache[page]

    def try_download(v, page):
        """尝试下载单个候选 → folder/{base}.{ext}。
        成功/已存在/停止 → 返回最终 record；失败 → 清理半成品并返回 None。"""
        referer = v.page_url or page
        ext = _cand_output_ext(v, referer)
        dest = os.path.join(folder, f"{base}.{ext}")
        if skip_existing:
            existing = _find_existing_video(folder, base)
            if existing:
                size = os.path.getsize(existing)
                logmsg(f"     ✓ 已存在，跳过下载 → {os.path.basename(existing)} ({_fmt_size(size)})")
                return {"index": cand.get("num"), "line": cand.get("line"),
                        "label": cand.get("label"), "page_url": page,
                        "kind": v.kind, "url": v.url, "status": "ok",
                        "local_path": existing, "bytes_count": size,
                        "error": "", "existing": True}
        cookies = v.cookies or None
        ok_res = None
        last_err = ""
        last_code = ERR_UNKNOWN
        total_att = 1 + retry + 1            # 候选级尝试次数（含退避）
        for att in range(total_att):
            if stop_flag and stop_flag():
                return {"index": cand.get("num"), "line": cand.get("line"),
                        "label": cand.get("label"), "page_url": page,
                        "kind": v.kind, "url": v.url, "status": "stopped",
                        "local_path": "", "bytes_count": 0, "error": ""}
            try:
                # L0/每任务档：该线路候选带捕获身份 → 全程 ambient 同源；否则用该集(页)任务档身份
                with _identity_ambient(getattr(v, "identity", None) or _episode_task_ident(page)):
                    if v.kind == "m3u8":
                        ok_res = download_m3u8(v.url, dest, referer=referer,
                                               stop_flag=stop_flag, cookies=cookies)
                    elif v.kind == "dash":
                        ok_res = download_dash(v.url, dest, referer=referer,
                                               stop_flag=stop_flag, cookies=cookies)
                    else:
                        ok_res = download_direct(v.url, dest, referer=referer,
                                                 stop_flag=stop_flag, cookies=cookies)
            except Exception as e:           # 双保险：任何网络异常都转可重试失败
                ok_res = {"status": "fail", "bytes": 0, "path": "",
                          "error": str(e) or "网络异常", "error_code": ERR_NETWORK}
            if ok_res.get("status") == "ok":
                break
            last_err = ok_res.get("error") or "下载失败"
            last_code = ok_res.get("error_code") or ERR_NETWORK
            if stop_flag and stop_flag():
                break
            if att < total_att - 1:
                time.sleep(0.8 * (att + 1))  # 间歇性断连 → 指数退避后重试
        if ok_res and ok_res.get("status") == "ok":
            real = ok_res.get("path") or dest    # fMP4 实际落成 .mp4，以返回值为准
            if real != dest:
                _remove_if(dest)                 # 清理误猜扩展名留下的空壳
            _prune_sibling_dupes(folder, base, real)
            logmsg(f"     ✓ 下载完成 → {os.path.basename(real)} ({_fmt_size(ok_res.get('bytes', 0))})")
            # #7 整部打标：对应当集页 title/封面；标签开启 + 容器允许才拉页（进程缓存），soft-fail
            em = _episode_meta_video(page)
            _maybe_tag_video(real, em, em.get("cover_url", ""),
                             referer=referer, cookies=cookies, log=logmsg)
            return {"index": cand.get("num"), "line": cand.get("line"),
                    "label": cand.get("label"), "page_url": page,
                    "kind": v.kind, "url": v.url, "status": "ok",
                    "local_path": real, "bytes_count": ok_res.get("bytes", 0),
                    "error": ""}
        # 该候选失败 → 清理可能的半成品
        for e in _VIDEO_OUT_EXTS:
            _remove_if(os.path.join(folder, f"{base}.{e}"))
        _remove_if(os.path.join(folder, f"{base}.part"))
        if stop_flag and stop_flag():
            return {"index": cand.get("num"), "line": cand.get("line"),
                    "label": cand.get("label"), "page_url": page,
                    "kind": "", "url": "", "status": "stopped",
                    "local_path": "", "bytes_count": 0, "error": ""}
        hint = _ERROR_HINT.get(last_code)
        logmsg(f"     ✗ {last_err or '下载失败'}"
               + (f"〔{hint}〕" if hint else ""))
        last_fail["code"] = last_code
        return None

    for cand in cands:
        if stop_flag and stop_flag():
            return {"index": cand.get("num"), "line": cand.get("line"),
                    "label": cand.get("label"), "page_url": cand.get("url"),
                    "kind": "", "url": "", "status": "stopped", "local_path": "",
                    "bytes_count": 0, "error": ""}
        page = cand.get("url") or ""
        logmsg(f"    源线路{cand.get('line', '-')}：{page}")
        # 深挖解析：单页 + 播放器 iframe/refresh 有界跳转，兼顾各 CMS 独有取流字段
        def _line_log(m):
            logmsg("      " + str(m))
        media_cands = _resolve_candidates(page, log=_line_log)
        if not media_cands and use_browser:
            logmsg("     静态无视频，浏览器兜底探测...")
            media_cands = _browser_probe(page, logmsg)
        if not media_cands:
            logmsg("     ✗ 未解析到视频流，切换线路")
            continue

        # 先尝试静态解析到的候选；若全部下载失败且启用浏览器，
        # 对该分集页再做一次浏览器重探（抓播放器实际播放时暴露的流），只补试新增 URL。
        tried = list(media_cands)
        browser_probed = False
        while True:
            failed_any = False
            for v in tried:                   # 逐个尝试：任一成功/已存在/停止即整体返回
                rec = try_download(v, page)
                if rec is not None:
                    return rec
                failed_any = True
            if not failed_any:
                break                          # 无候选可试（防御）
            if use_browser and not browser_probed:
                browser_probed = True
                logmsg("     静态候选均下载失败，浏览器重探真实播放流...")
                b_cands = _browser_probe(page, logmsg)
                known = {v.url for v in media_cands}
                fresh = [c for c in b_cands if c.url not in known]
                if fresh:
                    tried = fresh
                    continue                   # 用浏览器新增候选再走一轮
            break                              # 无浏览器兜底 → 该线路失败
        logmsg("     ✗ 该线路所有候选均失败，切换下一条线路")
    # 所有线路均失败
    logmsg("     ✗ 所有线路均下载失败")
    return {"index": None, "line": None, "label": base, "page_url": "",
            "kind": "", "url": "", "status": "fail", "local_path": "",
            "bytes_count": 0, "error": "所有线路均失败",
            "error_code": last_fail["code"]}


def _prune_sibling_dupes(folder: str, base: str, keep: str):
    """删除与 keep 同 base、不同扩展名的历史残留视频，避免一集多文件。"""
    for ext in VIDEO_EXT_DIRECT + ("ts", "m3u8"):
        p = os.path.join(folder, f"{base}.{ext}")
        if p != keep and os.path.exists(p):
            _remove_if(p)


def _series_rec_to_page(rec: dict) -> dict:
    """把分集记录包装成 UI 兼容的 page（外层结构沿用 run_batch 的 pages[].records）。"""
    return {"url": rec.get("page_url") or rec.get("url") or "",
            "title": rec.get("label") or "", "records": [rec]}


def process_series(seed_url: str, *, video_dir: str = VIDEO_DIR,
                   use_browser: bool = False, ep_start=None, ep_end=None,
                   skip_existing: bool = True,
                   series_concurrent: int = VIDEO_SERIES_PARALLEL,
                   progress_callback=None, log_callback=None,
                   stop_flag=None) -> dict:
    """整部抓取：给定详情页/任一分集页 URL → 解析分集 → 按线路故障切换逐集下载。
    series_concurrent>1 时逐集并行（下载线程各自隔离，UI 单 worker 线程不变）。
    返回 run_batch 兼容 summary（pages[].records），以便 VideoGrabDialog 直接渲染。"""
    def log(m):
        try:
            if log_callback:
                log_callback(str(m))
        except Exception:
            pass

    html, final = fetch_html(seed_url)
    plan = parse_episode_plan(html, final or seed_url)
    if (plan["matched"] == "none" or not plan.get("episodes")) and use_browser:
        log("整部：静态未识别到分集列表，尝试浏览器补全...")
        bplan = _browser_episode_plan(seed_url, log=log)
        if bplan["matched"] != "none" and bplan.get("episodes"):
            plan = bplan
            log(f"整部：浏览器补全成功（{plan['matched']} 解析 · "
                f"{plan['lines']} 条线路 · {len(plan['episodes'])} 条分集锚点）")
    if plan["matched"] == "none" or not plan.get("episodes"):
        return {"mode": "none", "title": plan["title"], "url": seed_url,
                "reason": plan.get("reason") or "未识别到分集列表",
                "pages": [], "ok": 0, "fail": 0, "skip": 0, "stopped": 0,
                "meta_path": ""}

    # 声明 serial 的站点（转发/签名 API 型，多集页并发或浏览器多开会触发掐连/封禁）：
    # 整部强制单集串行、不开浏览器多开——逐集每次只开一个集页 + 一次取流接口。
    if _site_adapter_serial(seed_url):
        if int(series_concurrent) > 1 or use_browser:
            log(f"整部：{plan['matched']} 站点要求逐页串行——并发降为 1、关闭浏览器兜底")
        series_concurrent = 1
        use_browser = False

    title = _fs_clean(plan["title"], _SERIES_TITLE_MAX) or "未命名动漫"
    folder = os.path.join(video_dir, title)
    os.makedirs(folder, exist_ok=True)
    log(f"📺 识别整部：{plan['title']}（{plan['matched']} 解析 · {plan['lines']} 条线路 · "
        f"{len(plan['episodes'])} 条分集锚点）")

    # num -> 候选线路分集页；未知集号按 URL 独立成组
    by_num = {}
    for e in plan["episodes"]:
        key = e["num"] if e["num"] is not None else ("u", e["url"])
        by_num.setdefault(key, []).append(e)
    num_keys = sorted((k for k in by_num if isinstance(k, int)))
    unk_keys = [k for k in by_num if not isinstance(k, int)]
    if ep_start is not None or ep_end is not None:
        def in_range(k):
            if not isinstance(k, int):
                return False
            if ep_start is not None and k < ep_start:
                return False
            if ep_end is not None and k > ep_end:
                return False
            return True
        num_keys = [k for k in num_keys if in_range(k)]
        unk_keys = []
    target_keys = num_keys + unk_keys

    total = len(target_keys)
    log(f"共 {total} 集进入下载队列，落盘目录：{folder}")

    # 预构建有序任务（文件名/标签与串行版完全一致，保证并行下命名稳定）
    tasks = []
    for i, key in enumerate(target_keys, start=1):
        cands = sorted(by_num[key], key=lambda e: (0 if e["line"] is None else e["line"]))
        first = cands[0]
        num = first["num"]
        label_raw = first["label"] or (f"第{num:02d}集" if isinstance(num, int) else "")
        base = _fs_clean(label_raw, _EP_LABEL_MAX)
        if not base:
            base = f"第{i:02d}集" if isinstance(num, int) else f"ep{i:02d}"
        disp = f"第{num:02d}集" if isinstance(num, int) else base
        tasks.append({"idx": i, "num": num, "disp": disp, "base": base, "cands": cands})

    recs = [None] * total
    parallel_n = max(1, int(series_concurrent))
    done_cnt = 0
    done_lock = threading.Lock()

    def _stopped_rec(task):
        return {"index": task["num"] if isinstance(task["num"], int) else task["idx"],
                "line": None, "label": task["disp"], "page_url": "",
                "kind": "", "url": "", "status": "stopped",
                "local_path": "", "bytes_count": 0, "error": ""}

    def _run_one(task):
        i = task["idx"]
        log(f"[{i}/{total}] {task['disp']}")
        try:
            return _download_episode(task["cands"], folder, task["base"],
                                     use_browser=use_browser,
                                     skip_existing=skip_existing,
                                     log=log, stop_flag=stop_flag)
        except Exception as e:                 # 双保险：worker 内异常转失败记录
            return {"index": task["num"] if isinstance(task["num"], int) else i,
                    "line": None, "label": task["disp"], "page_url": "",
                    "kind": "", "url": "", "status": "fail",
                    "local_path": "", "bytes_count": 0, "error": str(e),
                    "error_code": ERR_UNKNOWN}

    def _report_done(task):
        nonlocal done_cnt
        with done_lock:
            done_cnt += 1
        if progress_callback:                  # 已完成集数/总数（单调递增，适合状态栏）
            try:
                progress_callback(done_cnt, total, task["disp"])
            except Exception:
                pass

    if parallel_n <= 1 or total <= 1:
        # 串行兜底（语义与旧版一致，行为不回归）
        for i, task in enumerate(tasks):
            if stop_flag and stop_flag():
                log("已停止（整部任务）")
                break
            if i > 0:
                _pace_episode_gap(total)   # #6 人像节拍：大整部集间人工间隔（≥MIN_EPISODES 才睡）
            recs[task["idx"] - 1] = _run_one(task)
            _report_done(task)
    else:
        log(f"整部 {series_concurrent} 路并发抓取（每集 {VIDEO_CONCURRENT} 并发 · "
            f"host 共享配额 {VIDEO_HOST_CONCURRENCY} · 全局在途 ≤ {_GLOBAL_GATE.cap}）")
        # 吞吐遥测 daemon：每 ~5s 经 log_callback 输出 已下载/均速/活跃host/在途（供状态栏，不新增控件）
        tl_stop = threading.Event()
        t0 = time.time()
        dl0 = _total_downloaded_bytes()

        def _telemetry():
            while not tl_stop.wait(5.0):
                el = time.time() - t0
                mb = (_total_downloaded_bytes() - dl0) / 1e6
                with done_lock:
                    dc = done_cnt
                active_hosts = sum(1 for g in _HOST_GATES.values() if g.active > 0)
                inflight = sum(g.active for g in _HOST_GATES.values())
                log(f"⏱ {dc}/{total} 集完成 · {mb:.1f} MB · "
                    f"{mb / el:.1f} MB/s · 活跃host {active_hosts} · 在途 {inflight}")

        threading.Thread(target=_telemetry, daemon=True,
                         name="series-telemetry").start()
        try:
            with ThreadPoolExecutor(max_workers=parallel_n) as pool:
                pending = {}
                next_i = 0
                while next_i < total or pending:
                    if stop_flag and stop_flag():
                        for f in pending:
                            f.cancel()
                        log("已停止（整部任务）")
                        break
                    while next_i < total and len(pending) < parallel_n:
                        task = tasks[next_i]
                        pending[pool.submit(_run_one, task)] = task
                        next_i += 1
                    if not pending:
                        break
                    done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                    for fut in done:
                        task = pending.pop(fut)
                        try:
                            recs[task["idx"] - 1] = fut.result()
                        except Exception as e:
                            recs[task["idx"] - 1] = {
                                "index": task["num"] if isinstance(task["num"], int) else task["idx"],
                                "line": None, "label": task["disp"], "page_url": "",
                                "kind": "", "url": "", "status": "fail",
                                "local_path": "", "bytes_count": 0, "error": str(e),
                                "error_code": ERR_UNKNOWN}
                        _report_done(task)
        finally:
            tl_stop.set()
            # 已取消/未提交（停止）的槽位统一补 stopped
            for task in tasks:
                if recs[task["idx"] - 1] is None:
                    recs[task["idx"] - 1] = _stopped_rec(task)

    records = []
    pages = []
    ok = fail = skip = stopped = 0
    for task in tasks:
        rec = recs[task["idx"] - 1]
        if rec is None:                  # 停止时未提交/被取消的槽位 → 补 stopped
            rec = _stopped_rec(task)
        if rec["status"] == "stopped":
            stopped += 1
        elif rec["status"] == "ok":
            ok += 1
        elif rec["status"] == "unsupported":
            skip += 1
        else:
            fail += 1
            rec["error"] = (rec.get("error") or "失败") + \
                f"｜尝试线路{[c.get('line') for c in task['cands']]} 分集页 {len(task['cands'])} 个"
            if parallel_n > 1:
                rec["error"] = (rec.get("error") or "失败") + "｜多集并行"
        rec.setdefault("index", task["num"] if isinstance(task["num"], int) else task["idx"])
        rec.setdefault("label", task["disp"])
        records.append(rec)
        pages.append(_series_rec_to_page(rec))

    meta_path = write_metadata_json(folder, records)
    log(f"整部任务结束：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}，元数据 {meta_path}")
    return {"mode": "series", "title": plan["title"], "url": seed_url,
            "folder": folder, "ep_start": ep_start, "ep_end": ep_end,
            "pages": pages, "ok": ok, "fail": fail, "skip": skip,
            "stopped": stopped, "meta_path": meta_path}


def run_series(urls: list, *, video_dir: str = VIDEO_DIR,
               use_browser: bool = False, ep_start=None, ep_end=None,
               series_concurrent: int = VIDEO_SERIES_PARALLEL,
               progress_callback=None, log_callback=None, stop_flag=None) -> dict:
    """整部抓取入口（VideoGrabDialog 整部模式调用）：URL 逐个尝试识别为整部，
    识别失败回退为单页 run_batch；结果合并为 run_batch 兼容 summary。"""
    def log(m):
        try:
            if log_callback:
                log_callback(str(m))
        except Exception:
            pass

    pages, ok, fail, skip, stopped = [], 0, 0, 0, 0
    meta_path = ""
    try:
        _stale = cleanup_partials()   # 启动扫一次影子目录陈旧半成品，防膨胀
    except Exception:
        pass
    log(f"🎬 整部抓取任务开始，共 {len(urls)} 个 URL")
    for i, u in enumerate(urls, start=1):
        if stop_flag and stop_flag():
            log("已停止")
            break
        log(f"\n[{i}/{len(urls)}] {u}")
        res = process_series(u, video_dir=video_dir, use_browser=use_browser,
                             ep_start=ep_start, ep_end=ep_end,
                             series_concurrent=series_concurrent,
                             progress_callback=progress_callback,
                             log_callback=log_callback, stop_flag=stop_flag)
        if res.get("mode") == "none":
            log(f"该页未识别为整部，回退为单页批量抓取：{res.get('reason') or ''}")
            sub = run_batch([u], video_dir=video_dir, use_browser=use_browser,
                            progress_callback=progress_callback,
                            log_callback=log_callback, stop_flag=stop_flag)
            pages.extend(sub.get("pages") or [])
            ok += sub.get("ok", 0)
            fail += sub.get("fail", 0)
            skip += sub.get("skip", 0)
            stopped += sub.get("stopped", 0)
            meta_path = sub.get("meta_path") or meta_path
            continue
        pages.extend(res.get("pages") or [])
        ok += res.get("ok", 0)
        fail += res.get("fail", 0)
        skip += res.get("skip", 0)
        stopped += res.get("stopped", 0)
        meta_path = res.get("meta_path") or meta_path
    log(f"整部抓取全部结束：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}")
    return {"mode": "series", "pages": pages, "ok": ok, "fail": fail,
            "skip": skip, "stopped": stopped, "meta_path": meta_path}


# ================================================================
# 通用媒体解析层 media_resolve 的浏览器捕获旁支（2026-09-09 M1 纯新增；上方既有函数一行未动）
# 范式：真 Chrome 只用来【找真实媒体地址 + 收割登录 cookie + 探 DRM】，命中即关(几秒)；
# 下载走既有健壮栈。资源级 URL 捕获 = 注入【透明 fetch/XHR 打点 + performance resource 轮询 +
# requestMediaKeySystemAccess 探针】，只记 URL、绝不读响应体/改页面语义。
# DRM：探针只识别（window.__emeSeen），商业 DRM(Widevine/PlayReady/FairPlay) 由上层恒跳过，绝不解密。
# ================================================================

# 注入到每个新文档（Page.addScriptToEvaluateOnNewDocument）：
# 记录“媒体态”fetch/XHR 的真实 URL → window.__mUrls（有界去重，扩展名或常见 CDN 特征命中）；
# 拦截 navigator.requestMediaKeySystemAccess → window.__emeSeen（只记 keySystem 名）。不改任何语义。
_NET_PATCH_JS = r"""
(function(){
  try {
    if (window.__taowuNetPatch) return;
    window.__taowuNetPatch = true;
    window.__mUrls = window.__mUrls || [];
    window.__emeSeen = window.__emeSeen || [];
    var MAX = 4000;
    function isMediaUrl(u){
      u = String(u || '').toLowerCase();
      if (/\.(m3u8|mpd|mp4|m4s|flv|webm|mov|m4v|mp3|m4a|aac|wav|flac|ogg|ts)(\?|$)/.test(u)) return true;
      return /(^|[\/?&.])(videoplayback|googlevideo|playurl|play_addr|segment|seg[-\/]|init\.|index\.|mediaurl|videourl|murl|stream|cdn|\.dash|mse)([\/?&.]|$)/i.test(u);
    }
    function push(u){ if (u && isMediaUrl(u) && window.__mUrls.length < MAX && window.__mUrls.indexOf(u) < 0) window.__mUrls.push(u); }
    try {
      var _open = XMLHttpRequest.prototype.open;
      var _send = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(m, u){ try { this.__taowuU = u; } catch(e){} return _open.apply(this, arguments); };
      XMLHttpRequest.prototype.send = function(){
        try { if (this.__taowuU) push(this.__taowuU); } catch(e){}
        return _send.apply(this, arguments);
      };
    } catch(e){}
    try {
      if (window.fetch && !window.__taowuFetch){
        window.__taowuFetch = window.fetch;
        window.fetch = function(input, init){
          try { var u = (typeof input === 'string') ? input : (input && input.url) || ''; push(u); } catch(e){}
          return window.__taowuFetch.apply(this, arguments);
        };
      }
    } catch(e){}
    try {
      var nav = window.navigator;
      var _k = nav && nav.requestMediaKeySystemAccess ? nav.requestMediaKeySystemAccess.bind(nav) : null;
      if (_k){
        nav.requestMediaKeySystemAccess = function(ks, cfg){
          try { if (window.__emeSeen.indexOf(String(ks)) < 0) window.__emeSeen.push(String(ks)); } catch(e){}
          return _k(ks, cfg);
        };
      }
    } catch(e){}
  } catch(e){}
})();
"""

# 采集脚本（CDP 以函数体求值，须顶层 return）：读回各 frame 的 __mUrls/__emeSeen + performance
# resource 媒体项。返回扁平数组；eme 项以 'eme:<keySystem>' 前缀区分（调用方拆开处理）。
_NET_COLLECT_JS = r"""
return (function(){
  var out = [];
  function push(v){ if (v && typeof v === 'string' && out.indexOf(v) < 0 && out.length < 5000) out.push(v); }
  function walk(w){
    try {
      var m = w.__mUrls;
      if (m && m.length){ for (var i = 0; i < m.length; i++) push(m[i]); }
      var eme = w.__emeSeen;
      if (eme && eme.length){ for (var j = 0; j < eme.length; j++) push('eme:' + eme[j]); }
    } catch(e){}
    try {
      var per = w.performance && w.performance.getEntriesByType ? w.performance.getEntriesByType('resource') : null;
      if (per){ for (var k = 0; k < per.length; k++){
        try {
          var n = per[k].name;
          if (n && /\.(m3u8|mpd|mp4|m4s|flv|webm|mov|m4v|mp3|m4a|aac|wav|flac|ogg|ts)(\?|$)/i.test(n)) push(n);
        } catch(e){}
      } }
    } catch(e){}
    try { for (var f = 0; f < w.frames.length; f++){ try { walk(w.frames[f]); } catch(e){} } } catch(e){}
  }
  walk(window);
  return out;
})()
"""


def _install_net_patch(driver) -> bool:
    """给 driver 挂资源捕获打点：优先 Page.addScriptToEvaluateOnNewDocument（对随后每个新文档/子帧
    生效，含 iframe 播放器页），CDP 不可用则退而执行到当前文档。返回是否成功（失败=零打点降级）。"""
    if driver is None:
        return False
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": _NET_PATCH_JS})
        return True
    except Exception:
        pass
    try:
        driver.execute_script(_NET_PATCH_JS)
        return True
    except Exception:
        return False


def collect_media_streams_browser(url: str, driver, *, wait_sec: int = 9,
                                  classify_fn=None, net_capture: bool = True,
                                  challenge_rounds: int = 2, manual_verify: bool = False):
    """在【已持有的 driver】上跑增强发现（媒体解析层 M1 的浏览器兜底核心）。
    先挂资源捕获打点(对新文档生效) → 复用 detect_videos_browser 的点播轮询/iframe 追踪/
    cookie 型挑战/同域 cookie 收割 → 读回 __mUrls/__emeSeen 并入候选。
    classify_fn: (url)->(kind,ext)；默认视频白名单；audio/混合可注入（media_resolve 用
    classify_media 的 (kind,ext) 适配，视频+音频一次收齐）。
    返回 (cands, notes, eme)：cands=VideoCandidate 列表；eme=探测到的 EME keySystem 列表
    （只识别；商业 DRM 由上层只注记/跳过）。永不 raise。"""
    cands: list = []
    notes: list = []
    eme: list = []
    if net_capture:
        _install_net_patch(driver)
    base_cands, base_notes = detect_videos_browser(
        url, driver, wait_sec=wait_sec, classify_fn=classify_fn,
        challenge_rounds=challenge_rounds, manual_verify=manual_verify)
    cands = list(base_cands)
    notes = list(base_notes)
    if not net_capture:
        return cands, notes, eme
    # 读回资源捕获（__mUrls + __emeSeen + performance resource），跨同源 frame 枚举
    raw = []
    try:
        raw = driver.execute_script(_NET_COLLECT_JS) or []
    except Exception:
        raw = []
    raw = [x for x in raw if isinstance(x, str)]
    eme_raw = [x[len("eme:"):] for x in raw if x.startswith("eme:")]
    raw_media = [x for x in raw if not x.startswith("eme:")]
    for ks in dict.fromkeys(eme_raw):
        if ks and ks not in eme:
            eme.append(ks)
    if eme:
        notes.append("DRM:" + ",".join(eme))
    if raw_media:
        base = _safe_cur_url(driver) or url
        ck = _driver_cookies_for(driver, _host_of(base))
        seen = {_canon_key(c.url) for c in cands}
        classify = classify_fn or _classify
        for rawu in dict.fromkeys(raw_media):
            norm = _normalize(rawu, base)
            if norm is None:
                continue
            key = _canon_key(norm)
            if key in seen:
                continue
            seen.add(key)
            kind, ext = classify(norm)
            if kind is None:
                continue
            cands.append(VideoCandidate(index=len(cands) + 1, kind=kind, url=norm,
                                        ext=ext, src="资源捕获", page_url=base,
                                        cookies=dict(ck), title_hint=ext))
    return cands, notes, eme


def detect_media_browser_enhanced(url: str, *, classify_fn=None, wait_sec: int = 9,
                                  net_capture: bool = None, challenge_rounds: int = 2,
                                  manual_verify: bool = False):
    """通用媒体解析层增强浏览器探测（公开入口）：包 acquire/release driver 生命周期，语义同
    detect_media_browser 的增强版。net_capture=None → 读 config MEDIA_RESOLVE_NET_CAPTURE。
    返回 (cands, notes, eme)；eme 为 EME keySystem 列表（只识别，商业 DRM 由上层只跳过）。"""
    if net_capture is None:
        try:
            net_capture = bool(_CFG.MEDIA_RESOLVE_NET_CAPTURE)
        except Exception:
            net_capture = True
    try:
        driver = _acquire_browser(url)
        try:
            cands, notes, eme = collect_media_streams_browser(
                url, driver, wait_sec=wait_sec, classify_fn=classify_fn,
                net_capture=net_capture, challenge_rounds=challenge_rounds,
                manual_verify=manual_verify)
            # 命中候选 → 各 host cookie 并入本地 jar（供后续无浏览器下载跨轮复用，同 detect_media_browser）
            merge = {}
            for c in cands:
                h = _host_of(getattr(c, "url", "") or "")
                ck = getattr(c, "cookies", None)
                if h and ck:
                    merge.setdefault(h, {}).update(ck)
            if merge:
                _jar_update(merge)
            return cands, notes, eme
        finally:
            _release_browser(driver)
    except Exception as e:
        return [], [f"浏览器探测失败：{e}"], []


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2:
        test_url = sys.argv[1]
        res = run_batch([test_url])
        print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
    else:
        print("用法: python video_grab.py <页面URL>")
