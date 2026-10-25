# media_capture.py —— 通用「页面 API 旁观」捕获缝。
#
# ── 为什么存在 ─────────────────────────────────────────────────────────────────
# 现在大多数音视频站是"前端播放器 + 后端 API"，页面上根本没有直链。此时：
#   · 静态层（vg._resolve_candidates / ag._resolve_audio_from_page）扫 HTML → 必然空手；
#   · 通用浏览器发现（vg.detect_media_browser_enhanced）靠**页面层 fetch/XHR 打点**注入 →
#     看不见 Web Worker 里的请求、读不到跨域响应体、更读不到"响应体 JSON 里藏的 URL"。
# 本模块走第三条路：**用 CDP 旁观页面自己发出的请求与响应**，把媒体地址读回来。
#
# ── 与 site_recipes/douyin.py 的关系 ──────────────────────────────────────────
# douyin.py 那条"站点浏览器辅助捕获缝"是本模块的**原型**，但它写死成平台专属，且只覆盖
# **页面主 context**（单会话 Network.enable、事件不看 sessionId、无 Target.setAutoAttach）——
# 它能读到 aweme/detail 是因为那条请求恰好由主 context 发出。本模块显式挂子目标
# （Target.setAutoAttach flatten），把 worker/iframe 一并覆盖。douyin 缝**保留不动**
# （平台私有知识更准），本模块作为**通用兜底**排在它之后。
#
# ── 红线（一律不实现，如实标注）──────────────────────────────────────────────
# · 商业 DRM（Widevine/PlayReady/FairPlay）只识别跳过，绝不解密；旁观到 license 只入注记。
# · 不生成任何签名（a_bogus / wbi / n-sig / PO token / botguard），不过验证码。
# · **只旁观页面自己发出的请求，绝不代它构造请求**（哪怕参数已知）——这既是红线也是定位。
# · 不做全程收流 / MSE 抓帧；只取地址，下载交既有栈（media_resolve.download_track）。
# · 不改 vg/ag/qm 既有函数体，只 import 复用。
#
# 【依赖边界】本模块顶层仅 stdlib；websocket / Chrome 生命周期只在函数体内出现。
#   media_resolve 只持一个可替换函数指针（_page_watch），本模块被它函数体内懒 import。
# 【打包约定】本模块由 media_resolve 函数体内懒 import → PyInstaller 静态图收不到，
#   必须双登记：build.py hidden-import 序列 + taowu-spider-v12.1.2.spec 的 hiddenimports。
import json as _json
import logging as _logging
import re as _re
import time as _time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from media_resolve import MediaTrack

_log = _logging.getLogger("media_capture")

# CDP 端点（共享 Chrome 的调试端口；douyin.py 由本模块 import 复用）
CDP_BASE = "http://127.0.0.1:9222"

WANT_AUDIO, WANT_VIDEO, WANT_BOTH = "audio", "video", "both"

# 默认参数（可被 config.MEDIA_CAPTURE_* 覆盖，见 _cfg）
D_WAIT_SEC = 20.0            # 硬上限（典型 3-7s 因早停返回）
D_IDLE_GRACE = 2.5           # 命中后静默期：这么久没有新候选即收工
D_BODY_MAX_BYTES = 1 << 20   # 单响应体读取上限（1 MiB）
D_MAX_BODIES = 40            # 响应体读取总数上限
D_MAX_TRACKS = 60            # 候选上限
D_MAX_MANIFESTS = 8          # 同类清单保留数
D_BUFFER_TOTAL = 64 << 20    # Network.enable maxTotalBufferSize
D_BUFFER_RES = 8 << 20       # Network.enable maxResourceBufferSize


def _cfg(name: str, default):
    """读 config.MEDIA_CAPTURE_* 槽；config 不可用（离线测试）时用默认值。"""
    try:
        import config as _c
        return getattr(_c, name, default)
    except Exception:
        return default


# ================================================================
# 结果体
# ================================================================

@dataclass
class CaptureResult:
    """一次页面旁观的产出。notes 是排查的生命线：
    requests_seen/bodies_read/api_hits 三个观测量让"没旁观到请求"与"旁观到但没识别出"
    两种失败**可区分**——没有这条区分，排查会凭空多花几小时。"""
    url: str = ""
    tracks: list = field(default_factory=list)     # [MediaTrack, ...]
    notes: list = field(default_factory=list)
    api_hits: list = field(default_factory=list)   # [{"url","mime","reason"}, ...]
    requests_seen: int = 0
    bodies_read: int = 0
    route_hint: str = ""                            # ""|"ext"|"mime"|"api"


# 内部候选（收敛 A/B 两层信号）
@dataclass
class _Cand:
    url: str
    kind: str = "direct"
    ext: str = ""
    media_class: str = ""
    hint: str = "ext"        # ext / mime / api
    key_path: str = ""
    note: str = ""


# ================================================================
# 纯函数层（零网络零 Chrome，离线 canned 直测）
# ================================================================

_MISS = object()
_RE_JSONP = _re.compile(
    r"^\s*(?:/\*.*?\*/\s*)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\(\s*(.*?)\s*\)\s*;?\s*$", _re.S)
_RE_ESC_SLASH = _re.compile(r"\\/")

# 清单 Content-Type → kind
_CT_MANIFEST = {
    "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8",
    "audio/mpegurl": "m3u8", "audio/x-mpegurl": "m3u8", "application/mpegurl": "m3u8",
    "application/dash+xml": "dash",
}
# 视频 Content-Type → 扩展名（音频侧复用 ag._AUDIO_CT_EXT，唯一真源，不 copy）
_VIDEO_CT_EXT = {
    "video/mp4": "mp4", "video/webm": "webm", "video/x-matroska": "mkv",
    "video/quicktime": "mov", "video/x-msvideo": "avi", "video/mp2t": "ts",
}

# 文本兜底扫描的媒体扩展名（**不含 ts/m4s**：那是分片，收了只会污染候选表）
_TEXT_EXTS = ("m3u8", "mpd", "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus",
              "mp4", "webm", "mov", "mkv")
_RE_ABS_MEDIA = _re.compile(
    r"""(?:https?:)?//[^\s"'<>\\\)\]]+\.(?:%s)(?:\?[^\s"'<>\\\)\]]*)?""" % "|".join(_TEXT_EXTS), _re.I)
_RE_REL_MEDIA = _re.compile(
    r"""["'(](/[^\s"'<>\\\)\]]*\.(?:%s)(?:\?[^\s"'<>\\\)\]]*)?)["')]""" % "|".join(_TEXT_EXTS), _re.I)

# JSON 键名语义（媒体类判定与扩展名线索）
_KEY_AUDIO = _re.compile(r"audio|music|song|mp3|m4a|sound|audiourl|play_?url|hls_?url", _re.I)
_KEY_VIDEO = _re.compile(r"video|vod|movie|film|play_?addr|bit_?rate|stream|dash|m3u8", _re.I)
_KEY_EXTNAME = frozenset(("format", "ext", "extension", "container", "suffix",
                          "mime", "mimetype", "contenttype", "content_type",
                          "filetype", "file_type", "file_ext"))

# 分片形态
_RE_SEG_PATH = _re.compile(
    r"/(?:init|seg|seg-|chunk|frag|part|slice)[^/]*\.(?:m4s|mp4|ts|cmfv|cmfa)$", _re.I)
_RE_SEG_NUM = _re.compile(r"[-_]\d{1,6}\.(?:ts|m4s|cmfv|cmfa)$", _re.I)
_SEG_EXTS = frozenset(("ts", "m4s", "cmfv", "cmfa"))

# 噪声 URL（埋点/上报/广告/静态资源）
_RE_NOISE = _re.compile(
    r"(?:^|[/.?&=_-])(?:logs?|report\w*|beacon|collect\w*|analytics|sentry|tracking|"
    r"stat\w*|metric\w*|ping|heartbeat|telemetry|advert\w*|doubleclick|gtag)\b"
    r"|\.(?:gif|png|jpe?g|webp|svg|ico|css|woff2?|ttf|eot|map)(?:$|[?])", _re.I)

# 时效 token 特征
_EPH_KEYS = frozenset((
    "token", "expire", "expires", "deadline", "ts", "sign", "signature", "sig",
    "auth_key", "authkey", "auth_token", "hdnts", "wssecret", "wstime",
    "x-expires", "x-signature", "e", "n"))
_RE_EPH_PATH = _re.compile(r"/(?:expire|token|sign)/|/[0-9a-f]{32,}/", _re.I)

# 值得读响应体的 Content-Type 前缀（媒体本体/图片/字体一律不读）
_READABLE_CT = ("json", "text", "javascript", "xml", "x-www-form-urlencoded")
# API 路径加分 / 直接拒读
_API_GOOD = _re.compile(
    r"/api/|play|url|detail|media|song|music|audio|video|vod|mv|episode|track|album|stream", _re.I)
_API_BAD = _re.compile(r"/log|report|beacon|collect|analytics|sentry|track_|ping|\.gif", _re.I)


def _try_json(s: str):
    """json.loads，失败返回 _MISS（区分"解析出 null"与"解析失败"）。"""
    try:
        return _json.loads(s)
    except Exception:
        return _MISS


def unwrap_body(text):
    """响应体文本 → Python 对象 或 None。覆盖：裸 JSON / JSONP(`cb({...})`) /
    转义斜杠 `\\/` / HTML 实体 / 字符串化 JSON（值本身是 JSON 字符串）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.lstrip("﻿").strip()
    obj = _try_json(s)
    if obj is not _MISS:
        return obj
    m = _RE_JSONP.match(s)
    if m:
        obj = _try_json(m.group(1))
        if obj is not _MISS:
            return obj
    # 含转义斜杠 / HTML 实体（&times 这种无分号实体曾腐蚀签名 URL，故用 vg._safe_unescape）
    try:
        import video_grab as _vg
        s2 = _vg._safe_unescape(_RE_ESC_SLASH.sub("/", s))
    except Exception:
        s2 = _RE_ESC_SLASH.sub("/", s)
    if s2 != s:
        obj = _try_json(s2)
        if obj is not _MISS:
            return obj
        m = _RE_JSONP.match(s2)
        if m:
            obj = _try_json(m.group(1))
            if obj is not _MISS:
                return obj
    return None


def classify_response(url: str, mime_type: str = "", request_type: str = "",
                      status: int = 0, headers: dict = None):
    """URL + 响应元信息 → (kind, ext, media_class)；无法识别 → (None, None, None)。
    判据顺序：① 清单 Content-Type ② 音频 Content-Type（复用 ag 唯一真源）
    ③ 视频 Content-Type ④ 扩展名白名单（复用 mr.classify_media，与配方层同一判据）。"""
    ct = (mime_type or "").split(";")[0].strip().lower()
    k = _CT_MANIFEST.get(ct)
    if k:
        return k, ("m3u8" if k == "m3u8" else "mpd"), ""
    if ct.startswith("audio/"):
        try:
            import audio_grab as _ag
            ext = _ag._content_type_ext(ct)
        except Exception:
            ext = ""
        return "direct", ext, "audio"      # 未在册的 audio/* 也给收，ext 留空由 note 提示
    if ct in _VIDEO_CT_EXT:
        return "direct", _VIDEO_CT_EXT[ct], "video"
    if ct.startswith("video/"):
        return "direct", "", "video"
    # 扩展名线（懒 import 避免与 media_resolve 形成顶层环）
    try:
        import media_resolve as _mr
        kind, ext, mc = _mr.classify_media(url)
    except Exception:
        kind = ext = mc = None
    if kind:
        return kind, ext, mc
    return None, None, None


def is_probably_segment(url: str, mime_type: str = "", request_type: str = "",
                        status: int = 0, headers: dict = None, *, seg_urls=frozenset()):
    """是否是分片请求（不进候选表）。一个 HLS 页不打这个，候选表会被几百条 .ts 冲垮。"""
    u = (url or "").split("#", 1)[0]
    if u in seg_urls:
        return True
    ct = (mime_type or "").split(";")[0].strip().lower()
    if ct in ("video/mp2t", "video/iso.segment"):
        return True
    path = urlsplit(u).path.lower()
    tail = path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
    if ext not in _SEG_EXTS:
        return False
    if ct == "application/octet-stream":
        return True
    hdr = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if status == 206 or "range" in hdr:
        return True
    return bool(_RE_SEG_PATH.search(path) or _RE_SEG_NUM.search(path))


def is_noise_url(url: str) -> bool:
    """埋点/上报/广告/静态资源 → 不值得看。对真 URL 保守，宁可漏杀不可误杀。"""
    return bool(_RE_NOISE.search(url or ""))


def ephemeral_hint(url: str) -> bool:
    """URL 带时效 token 特征 → 须现拉现下。"""
    u = url or ""
    q = urlsplit(u).query
    if q:
        for kv in q.split("&"):
            if kv.split("=", 1)[0].strip().lower() in _EPH_KEYS:
                return True
    return bool(_RE_EPH_PATH.search(u))


_AUDIO_EXTS_HINT = frozenset(("mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "mp4a"))
_VIDEO_EXTS_HINT = frozenset(("mp4", "webm", "mkv", "mov", "avi", "flv", "m3u8", "ts"))


def _mk_cand(url: str, base_url: str, hint: str, *, media_class="", ext="", key_path=""):
    """把一条 URL 变成候选；不成媒体 → None。"""
    if not url:
        return None
    u = url.strip()
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = urljoin(base_url or "", u)
    if not u.lower().startswith(("http://", "https://")):
        return None
    if is_noise_url(u):
        return None
    kind, xext, xmc = classify_response(u)
    if not kind:
        # 扩展名线索（JSON 兄弟键给的）——"无扩展名音频 + format 字段"这类站的关键增量
        if ext:
            ex = ext.strip().lstrip(".").lower()
            if ex in _TEXT_EXTS or ex:
                mc = media_class or ("audio" if ex in _AUDIO_EXTS_HINT else
                                     ("video" if ex in _VIDEO_EXTS_HINT else ""))
                if mc:
                    return _Cand(url=u, kind="direct", ext=ex, media_class=mc,
                                 hint=hint, key_path=key_path)
        return None
    if kind in ("m3u8", "dash"):
        mc = ""                       # 纯清单：音视频由上下文/下载侧定夺
    else:
        mc = media_class or xmc or ""
    return _Cand(url=u, kind=kind, ext=xext or "", media_class=mc, hint=hint, key_path=key_path)


def walk_json_for_media(obj, base_url: str, *, want: str = WANT_BOTH,
                        max_depth: int = 8, max_nodes: int = 20000, key_path: str = "") -> list:
    """递归遍历 JSON，收集媒体 URL。返回 [_Cand, ...]。
    · 字符串值先做 URL 归一（相对路径 urljoin）；
    · 值本身能 json.loads → 递归（字符串化 JSON）；
    · 兄弟键 format/ext/type/container/mime → 扩展名线索；
    · key_path 含 audio|music|song|play_url → 音频类强提示。"""
    out = []
    nodes = [0]

    def _class_hint(kp: str) -> str:
        if _KEY_AUDIO.search(kp or ""):
            return "audio"
        if _KEY_VIDEO.search(kp or ""):
            return "video"
        return ""

    def _walk(node, depth, kp, ext_hint):
        if nodes[0] > max_nodes or depth > max_depth:
            return
        nodes[0] += 1
        if isinstance(node, dict):
            eh = ""
            for k, v in node.items():
                if isinstance(v, str) and str(k).lower() in _KEY_EXTNAME and 0 < len(v) <= 16:
                    eh = v.strip().lstrip(".").lower()
                    break
            for k, v in node.items():
                _walk(v, depth + 1, ("%s.%s" % (kp, k)) if kp else str(k), eh or ext_hint)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, depth + 1, "%s[%d]" % (kp, i), ext_hint)
        elif isinstance(node, str):
            _leaf(node, kp, ext_hint)

    def _leaf(s, kp, ext_hint):
        if not (8 <= len(s) <= 2048):
            return
        t = s.strip()
        # 字符串化 JSON：值本身是 JSON 文本
        if t[:1] in ("{", "["):
            inner = unwrap_body(t)
            if isinstance(inner, (dict, list)):
                _walk(inner, 1, kp, ext_hint)
                return
        if t[:1] in ("<", "{", "["):
            return
        cls = _class_hint(kp)
        c = _mk_cand(t, base_url, "api", media_class=cls, ext=ext_hint, key_path=kp)
        if c:
            out.append(c)

    if isinstance(obj, (dict, list)):
        _walk(obj, 0, key_path, "")
    elif isinstance(obj, str):
        _leaf(obj, key_path, "")
    return out


def extract_media_urls_from_text(text: str, base_url: str, *, want: str = WANT_BOTH) -> list:
    """非 JSON 响应体（JS/HTML/plain）的正则兜底：抓绝对与相对媒体 URL。"""
    out = []
    if not isinstance(text, str) or not text:
        return out
    for m in _RE_ABS_MEDIA.finditer(text):
        c = _mk_cand(m.group(0), base_url, "api")
        if c:
            out.append(c)
    for m in _RE_REL_MEDIA.finditer(text):
        c = _mk_cand(m.group(1), base_url, "api")
        if c:
            out.append(c)
    return out


def parse_playlist_segments(text: str, base_url: str) -> set:
    """m3u8 体 → 段 URL 集合（供后续 A 层请求抑制）。非 m3u8 体返回空集。"""
    segs = set()
    if not isinstance(text, str) or "#EXTM3U" not in text[:2048]:
        return segs
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            # #EXT-X-MEDIA:...,URI="x.m3u8" / #EXT-X-KEY:...,URI="key"
            m = _re.search(r'URI="([^"]+)"', ln)
            if m:
                segs.add(_abs(m.group(1), base_url))
            continue
        segs.add(_abs(ln, base_url))
    return segs


def _abs(u: str, base_url: str) -> str:
    u = (u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/") or not urlsplit(u).scheme:
        return urljoin(base_url or "", u)
    return u


def order_tracks(tracks: list, want: str = WANT_BOTH) -> list:
    """按 want 稳定排序（绝不丢轨）。
    want=audio：audio 在前，其次清单，最后 video；want=video 镜像；both：清单先（音视频待探针）。
    同组内清单按路径段数升序（master 先于变体），其余保持发现序。"""
    def rank(t):
        mc = getattr(t, "media_class", "") or ""
        is_man = (getattr(t, "kind", "") or "") in ("m3u8", "dash")
        if want == WANT_AUDIO:
            return 0 if mc == "audio" else (1 if is_man else 2)
        if want == WANT_VIDEO:
            return 0 if mc == "video" else (1 if is_man else 2)
        return 0 if is_man else (1 if mc == "video" else 2)

    def depth(t):
        return len(urlsplit(getattr(t, "url", "") or "").path.strip("/").split("/"))

    def key(p):
        i, t = p
        is_man = (getattr(t, "kind", "") or "") in ("m3u8", "dash")
        return (rank(t), depth(t) if is_man else 0, i)

    return [t for _, t in sorted(enumerate(tracks), key=key)]


def dedupe_manifests(tracks: list, *, cap: int = D_MAX_MANIFESTS):
    """同类清单（同 host+kind）超过 cap 时截断。返回 (保留列表, 丢弃数)。非清单轨原样保留。"""
    groups = {}
    for t in tracks:
        if (getattr(t, "kind", "") or "") in ("m3u8", "dash"):
            try:
                host = urlsplit(t.url).netloc.lower()
            except Exception:
                host = ""
            groups.setdefault((host, t.kind), []).append(t)
    drop = set()
    dropped = 0
    for key, items in groups.items():
        if len(items) <= cap:
            continue
        items_sorted = sorted(items, key=lambda t: len(urlsplit(t.url).path.strip("/").split("/")))
        for t in items_sorted[cap:]:
            drop.add(id(t))
            dropped += 1
    return [t for t in tracks if id(t) not in drop], dropped


# ================================================================
# 事件状态机（send / body_fetcher / now 全可注入 → 完全离线可测）
# ================================================================

class _Watcher:
    """CDP 消息 → 候选的增量状态机。

    on_message 只做"记录 + 入队"；真正读响应体在 drain() 里做——避免在事件回调里发起
    同步 CDP 往返导致的深层重入（douyin 的 _send 是当场重入的，通用化后有预算与
    sessionId 路由两件事，重入容易出错）。
    """

    def __init__(self, *, page_url: str = "", want: str = WANT_BOTH, send=None,
                 body_fetcher=None, now=None, wait_sec: float = D_WAIT_SEC,
                 idle_grace: float = D_IDLE_GRACE, body_max_bytes: int = D_BODY_MAX_BYTES,
                 max_bodies: int = D_MAX_BODIES, max_tracks: int = D_MAX_TRACKS,
                 buffer_total: int = D_BUFFER_TOTAL, buffer_res: int = D_BUFFER_RES,
                 log=None):
        self.page_url = page_url or ""
        self.want = want
        self._send = send
        self._body_fetcher = body_fetcher
        self._now = now or _time.time
        self.wait_sec = float(wait_sec)
        self.idle_grace = float(idle_grace)
        self.body_max_bytes = int(body_max_bytes)
        self.max_bodies = int(max_bodies)
        self.max_tracks = int(max_tracks)
        self.buffer_total = int(buffer_total)
        self.buffer_res = int(buffer_res)
        self.log = log

        self.t0 = self._now()
        self.deadline = self.t0 + self.wait_sec
        self.last_hit = 0.0
        self.loaded = False
        self.title = ""                 # 触发时从 document.title 抄回（真 CDP 路径才会填）

        self.cands = []                 # [_Cand]
        self.seen = set()               # 去重键
        self.reqs = {}                  # requestId → 元信息
        self.seg_urls = set()
        self.sessions = set()           # 子目标 sessionId
        self.events_seen = 0
        self.requests_seen = 0
        self.bodies_read = 0
        self.bodies_skipped_big = 0
        self.api_hits = []
        self.notes = []
        self.route_hint = ""
        self._body_q = []               # [(requestId, sessionId)]
        self._triggers = 0

    # ---------- 事件入口 ----------

    def on_message(self, msg):
        if not isinstance(msg, dict):
            return
        self.events_seen += 1
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        sid = msg.get("sessionId") or ""
        if method == "Target.attachedToTarget":
            self._on_attached(params)
        elif method == "Network.requestWillBeSent":
            self._on_request(params, sid)
        elif method == "Network.responseReceived":
            self._on_response(params, sid)
        elif method == "Network.loadingFinished":
            rid = params.get("requestId")
            if rid:
                self._enqueue_body(rid, sid)
        elif method == "Page.loadEventFired":
            self.mark_loaded()

    def _on_attached(self, params):
        """子目标（worker / iframe / OOPIF）挂上来 → 该会话也必须 Network.enable，
        否则 worker 里发的请求我们一条都看不见。"""
        new_sid = params.get("sessionId")
        info = params.get("targetInfo") or {}
        if not new_sid:
            return
        if new_sid in self.sessions:
            return          # 幂等：同一 session 重复挂载不再补发（否则补发又会引出事件 → 自激）
        self.sessions.add(new_sid)
        self._i("子目标挂载：%s sessionId=%s", info.get("type") or "?", new_sid[:12])
        self._cmd("Network.enable", {"maxTotalBufferSize": self.buffer_total,
                                     "maxResourceBufferSize": self.buffer_res}, sid=new_sid)
        if (info.get("type") or "") in ("worker", "service_worker", "shared_worker"):
            self._cmd("Runtime.enable", {}, sid=new_sid)

    def _on_request(self, params, sid):
        self.requests_seen += 1
        rid = params.get("requestId")
        req = params.get("request") or {}
        u = req.get("url") or ""
        rtype = params.get("type") or ""
        if rid:
            self.reqs[rid] = {"url": u, "session": sid, "type": rtype,
                              "mime": "", "status": 0, "headers": {},
                              "enc_len": 0, "media": False}
        if not u or is_noise_url(u):
            return
        # A 层（早信号）：仅凭 URL 就能定类的（扩展名/清单）
        kind, ext, mc = classify_response(u, request_type=rtype)
        if kind and not is_probably_segment(u, request_type=rtype, seg_urls=self.seg_urls):
            self._add(_Cand(url=u, kind=kind, ext=ext or "", media_class=mc or "", hint="ext"))

    def _on_response(self, params, sid):
        rid = params.get("requestId")
        resp = params.get("response") or {}
        rec = self.reqs.get(rid)
        if rec is None:
            return
        rec["mime"] = resp.get("mimeType") or ""
        rec["status"] = int(resp.get("status") or 0)
        rec["headers"] = resp.get("headers") or {}
        rec["enc_len"] = int(resp.get("encodedDataLength") or 0)
        rec["media"] = (resp.get("mimeType") or "").lower().startswith(("audio/", "video/")) or \
            (params.get("type") or "") == "Media"
        u = rec.get("url") or ""
        if not u or is_noise_url(u):
            return
        if is_probably_segment(u, rec["mime"], rec["type"], rec["status"], rec["headers"],
                               seg_urls=self.seg_urls):
            return
        # A 层（强信号）：Content-Type 定类——**扩展名白名单做不到的那一类**
        kind, ext, mc = classify_response(u, rec["mime"], rec["type"], rec["status"], rec["headers"])
        if kind:
            self._add(_Cand(url=u, kind=kind, ext=ext or "", media_class=mc or "", hint="mime"))

    # ---------- 响应体 ----------

    def _readable(self, rec) -> bool:
        if self.bodies_read >= self.max_bodies:
            return False
        if _API_BAD.search(rec["url"] or ""):
            return False
        ct = (rec["mime"] or "").split(";")[0].strip().lower()
        if not ct:
            # 无 Content-Type 的响应：只在"像 API 的路径"上读（否则无从判断是否为二进制）
            return bool(_API_GOOD.search(rec["url"] or ""))
        if not any(ct.startswith(p) or p in ct for p in _READABLE_CT):
            return False          # 媒体本体/图片/字体/css 一律不读
        return True

    def _enqueue_body(self, rid, sid):
        rec = self.reqs.get(rid)
        if not rec:
            return
        if rec["enc_len"] and rec["enc_len"] > self.body_max_bytes:
            self.bodies_skipped_big += 1
            return
        if not self._readable(rec):
            return
        self._body_q.append((rid, rec["session"] or sid))

    def drain(self):
        """消费读体队列。真实泵在每轮 recv 后调用；replay 在每条事件后调用。"""
        while self._body_q:
            rid, sid = self._body_q.pop(0)
            rec = self.reqs.get(rid) or {}
            if not self._readable(rec):
                continue
            txt = self._fetch_body(rid, sid)
            if not isinstance(txt, str) or not txt:
                continue
            self.bodies_read += 1
            self._consume_body(rec, txt)

    def _fetch_body(self, rid, sid):
        if self._body_fetcher is not None:
            try:
                return self._body_fetcher(rid, sid)
            except Exception:
                return None
        res = self._cmd("Network.getResponseBody", {"requestId": rid}, sid=sid)
        if not isinstance(res, dict):
            return None
        txt = res.get("body") or ""
        if res.get("base64Encoded") and txt:
            try:
                import base64 as _b64
                txt = _b64.b64decode(txt).decode("utf-8", "replace")
            except Exception:
                return None
        return txt

    def _consume_body(self, rec, txt):
        """B 层：响应体里找媒体 URL —— 本模块相对通用 detect 的核心增量。"""
        u = rec.get("url") or ""
        before = len(self.cands)
        # 清单体：先学段表（后续 A 层请求命中即抑制），再单独产出清单轨
        if "#EXTM3U" in txt[:2048] or "<MPD" in txt[:2048]:
            self.seg_urls |= parse_playlist_segments(txt, u)
            kind = "m3u8" if "#EXTM3U" in txt[:2048] else "dash"
            self._add(_Cand(url=u, kind=kind, ext=("m3u8" if kind == "m3u8" else "mpd"),
                            media_class="", hint="api"))
            return
        obj = unwrap_body(txt)
        found = []
        if isinstance(obj, (dict, list)):
            found = walk_json_for_media(obj, u or self.page_url, want=self.want)
        if not found:
            found = extract_media_urls_from_text(txt, u or self.page_url, want=self.want)
        for c in found:
            c.note = "取自 %s" % _short(u)
            self._add(c)
        if len(self.cands) > before:
            self.api_hits.append({"url": _short(u), "mime": rec.get("mime") or "",
                                  "reason": "%d 条" % (len(self.cands) - before)})

    # ---------- 订阅 / 触发 ----------

    def subscribe(self):
        """把本会话该发的命令一次性发掉（Page.navigate 之前调用）。"""
        self._cmd("Target.setDiscoverTargets", {"discover": True})
        self._cmd("Target.setAutoAttach", {"autoAttach": True,
                                           "waitForDebuggerOnStart": False, "flatten": True})
        self._cmd("Network.enable", {"maxTotalBufferSize": self.buffer_total,
                                     "maxResourceBufferSize": self.buffer_res})
        self._cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        self._cmd("Page.enable", {})
        self._cmd("Page.bringToFront", {})     # 后台 tab 会被节流：JS 不跑、媒体不加载
        self._cmd("Runtime.enable", {})

    def mark_trigger(self):
        self._triggers += 1

    # ---------- 早停 ----------

    def mark_loaded(self):
        self.loaded = True

    def wants_more(self) -> bool:
        now = self._now()
        if now >= self.deadline:
            return False
        if self.cands and self.last_hit and (now - self.last_hit) >= self.idle_grace:
            return False
        return True

    # ---------- 收口 ----------

    def _add(self, c: _Cand):
        if not c:
            return
        key = _canon(c.url)
        if not key or key in self.seen:
            return
        if len(self.cands) >= self.max_tracks:
            return
        self.seen.add(key)
        self.cands.append(c)
        self.last_hit = self._now()
        # 命中所属层级：api（读响应体）> mime（Content-Type）> ext（URL 扩展名）
        if _HINT_RANK.get(c.hint, 0) > _HINT_RANK.get(self.route_hint, 0):
            self.route_hint = c.hint

    def result(self) -> CaptureResult:
        tracks = []
        for i, c in enumerate(self.cands):
            note = c.note or ""
            if c.hint == "api":
                note = (note + " " if note else "") + "页面 API 响应体"
            eph = ephemeral_hint(c.url)
            if eph:
                note = (note + " " if note else "") + "现场时效直链，请尽快下载"
            if not c.ext and c.kind == "direct":
                note = (note + " " if note else "") + "扩展名未知，建议开 MEDIA_TRACK_PROBE"
            tracks.append(MediaTrack(
                index=i + 1, media_class=c.media_class, kind=c.kind, url=c.url,
                ext=c.ext or "", src=("browser:api" if c.hint == "api" else "browser:net"),
                page_url=self.page_url, title_hint=self.title, note=note.strip(),
                ephemeral=eph))
        tracks = order_tracks(tracks, self.want)
        kept, dropped = dedupe_manifests(tracks, cap=_cfg("MEDIA_CAPTURE_MAX_MANIFESTS",
                                                          D_MAX_MANIFESTS))
        for n, t in enumerate(kept):
            t.index = n + 1
        notes = list(self.notes)
        if dropped:
            notes.append("同类清单过多，截断 %d 条（可调 MEDIA_CAPTURE_MAX_MANIFESTS）" % dropped)
        if self.bodies_skipped_big:
            notes.append("跳过 %d 个大响应体（>%.0fKB）" % (
                self.bodies_skipped_big, self.body_max_bytes / 1024.0))
        # **两条不同文案**：区分"没旁观到请求"与"旁观到但没识别出"（否则排查会凭空多花几小时）
        if not kept:
            if self.requests_seen == 0:
                notes.append("旁观到 0 个请求（页面未发起网络活动：可能需登录/被挑战/触发失败/未到前台）")
            else:
                notes.append("旁观到 %d 个请求但未识别出媒体（可提高 wait_sec，或看 api_hits 排查）"
                             % self.requests_seen)
        return CaptureResult(url=self.page_url, tracks=kept, notes=notes,
                             api_hits=self.api_hits, requests_seen=self.requests_seen,
                             bodies_read=self.bodies_read, route_hint=self.route_hint)

    # ---------- 工具 ----------

    def _cmd(self, method, params=None, sid=""):
        if self._send is None:
            return None
        try:
            return self._send(method, params or {}, sid)
        except Exception as e:
            _log.debug("CDP %s 下发失败：%s", method, e)
            return None

    def _i(self, fmt, *a):
        if self.log:
            try:
                self.log(fmt % a)
            except Exception:
                pass


# 命中层级优先级（api=读响应体 > mime=Content-Type > ext=URL 扩展名）
_HINT_RANK = {"": 0, "ext": 1, "mime": 2, "api": 3}


def _canon(u: str) -> str:
    return (u or "").split("#", 1)[0]


def _short(u: str, n: int = 70) -> str:
    u = u or ""
    return u if len(u) <= n else u[:n] + "…"


# ================================================================
# 离线事件回放（零真网零 Chrome —— canned 测试的唯一入口）
# ================================================================

def replay_capture(events, bodies=None, *, url: str = "", want: str = WANT_BOTH,
                   body_calls=None, **kw):
    """把一串 canned CDP 消息喂给 _Watcher，返回 (CaptureResult, sent)。
    events: [CDP 消息 dict]，可带 "sessionId"（缺省=页面主会话）
    bodies: {requestId: 响应体文本}
    body_calls: 可选 list，记录每次 getResponseBody 的 (rid, sid) —— 断言"大/二进制体没被读"
    sent: [(sessionId, method, params), ...] —— 断言"本应下发的命令"确实发生过
    """
    sent = []
    calls = body_calls if body_calls is not None else []

    def _send(method, params, sid=""):
        sent.append((sid, method, params))
        return {}

    def _fetch(rid, sid):
        calls.append((rid, sid))
        return (bodies or {}).get(rid)

    w = _Watcher(page_url=url, want=want, send=_send, body_fetcher=_fetch, **kw)
    w.subscribe()
    for ev in (events or []):
        w.on_message(ev)
        w.drain()
    return w.result(), sent


# ================================================================
# 真 CDP 编排（本模块唯一有副作用的一段：拉 Chrome / 开 tab / 跳转 / 触发 / 关 tab）
#
# 这里的三件事都极易静默失败，故逐条写明：
#   ① 首屏不 prefetch + 无用户手势 → 页面根本没发请求，返回空，会被误判成"这个站没有 API"。
#      对策 = Page.bringToFront + 三轮触发 + 真鼠标点击；且把"没旁观到请求"与"旁观到但没识别出"
#      写成两条不同 note（见 _Watcher.result）。
#   ② 后台 tab 会被 Chrome 节流（JS 不跑、媒体不加载）——/json/new 开的 tab 默认不在前台。
#   ③ 直连模式下 vg._acquire_browser 会先 taskkill chrome.exe —— 会关掉用户自己的 Chrome。
# ================================================================

_ALIVE_TIMEOUT = 3.0           # 探 9222 的单次超时
_TRIGGER_AT = (0.8, 2.0, 4.5)  # 触发时点（相对 Page.navigate）：纯 JS → +鼠标点击 → +Esc
_CMD_TIMEOUT = 8.0             # 单条 CDP 命令等待 id 回包的硬上限

# 触发 JS：静音并 play 所有媒体 → 量最大播放器容器中心（供真鼠标点击）→ 关遮挡浮层 → 逼一次懒加载。
# 刻意不写任何站点选择器；通用播放按钮表复用 vg._CLICK_PLAY_JS（只读其常量，不改 vg）。
_TRIGGER_JS = r"""
(function(){
    var out = {title: document.title || '', cx: 0, cy: 0, w: 0, h: 0,
               played: 0, dismissed: 0, media: 0};
    try {
        var ms = document.querySelectorAll('video,audio');
        out.media = ms.length;
        for (var i = 0; i < ms.length; i++){
            try { ms[i].muted = true; ms[i].play(); out.played++; } catch(e){}
        }
    } catch(e){}
    // 最大可见播放器容器（120x80 以下的小图标不算）→ 真鼠标点击落点
    var best = null, area = 0;
    var sels = ['video','audio','[class*="player"]','[class*="video"]','[id*="player"]','iframe'];
    for (var s = 0; s < sels.length; s++){
        var ns = document.querySelectorAll(sels[s]);
        for (var j = 0; j < ns.length; j++){
            var r = ns[j].getBoundingClientRect();
            if (r.width < 120 || r.height < 80) continue;
            if (r.width * r.height > area){ area = r.width * r.height; best = r; }
        }
    }
    if (best){
        out.cx = Math.round(best.left + best.width / 2);
        out.cy = Math.round(best.top + best.height / 2);
        out.w = Math.round(best.width); out.h = Math.round(best.height);
    }
    // 关掉 fixed/absolute 的遮挡浮层（登录框/同意条/引导层），否则点击落在浮层上白点
    var ov = ['.modal','[class*="mask"]','[class*="overlay"]','[class*="login"]','[class*="guide"]','[class*="dialog"]'];
    for (var k = 0; k < ov.length; k++){
        var os = document.querySelectorAll(ov[k]);
        for (var m = 0; m < os.length; m++){
            try {
                var st = getComputedStyle(os[m]);
                if (st.position === 'fixed' || st.position === 'absolute'){
                    os[m].style.display = 'none'; out.dismissed++;
                }
            } catch(e){}
        }
    }
    try { window.scrollBy(0, Math.round(window.innerHeight * 0.8)); } catch(e){}
    return out;
})()
"""


def _i(log, fmt, *a):
    if not log:
        return
    try:
        log(fmt % a if a else fmt)
    except Exception:
        pass


# ---------------- Chrome 生命周期 ----------------

def ensure_shared_chrome(url: str = "", *, allow_launch=None, log=None) -> str:
    """确保共享 Chrome（CDP 9222）在跑。返回状态：alive / launched / denied / failed。

    ⚠ 拉起走 vg._acquire_browser → qiongqi_maw.acquire_driver，**直连模式下会先 taskkill chrome.exe**，
    即会关掉用户自己开着的 Chrome 窗口。douyin 缝已有此行为，但通用缝在**任意 URL** 上都会触发，
    风险面被放大 → 由 MEDIA_CAPTURE_ALLOW_CHROME_LAUNCH 把关（默认 True，置 False 则只复用不拉起）。
    """
    import urllib.request as _ur
    if _probe_cdp():
        return "alive"
    if allow_launch is None:
        allow_launch = bool(_cfg("MEDIA_CAPTURE_ALLOW_CHROME_LAUNCH", True))
    if not allow_launch:
        return "denied"
    _i(log, "共享 Chrome 未在跑 → 尝试拉起（直连模式下会先关掉用户自己的 Chrome 窗口）")
    try:
        import video_grab as _vg
        d = _vg._acquire_browser(url or "about:blank")
        if d is None:
            return "failed"
        _vg._release_browser(d)      # 归还池/单实例：Chrome 保持存活（与 douyin 缝同法）
    except Exception as e:
        _i(log, "拉起共享 Chrome 失败：%s", e)
        return "failed"
    for _ in range(24):              # 拉起后调试端口需要一点时间才监听
        if _probe_cdp():
            return "launched"
        _time.sleep(0.25)
    return "failed"


def _probe_cdp() -> bool:
    import urllib.request as _ur
    try:
        _ur.urlopen(CDP_BASE + "/json/version", timeout=_ALIVE_TIMEOUT).read()
        return True
    except Exception:
        return False


def _cdp_browser_ws() -> str:
    """浏览器级 WebSocket 端点（可对任意 target 发命令）。"""
    import urllib.request as _ur
    try:
        info = _json.loads(_ur.urlopen(CDP_BASE + "/json/version",
                                       timeout=_ALIVE_TIMEOUT).read().decode("utf-8", "replace"))
        return info.get("webSocketDebuggerUrl") or ""
    except Exception:
        return ""


def _cdp_open_tab(url: str):
    """开一个定位到 url 的**新** tab，返回 CDP tab dict；失败返回 None。

    与 douyin 缝的关键差别：**不回落到"任意同站现有 tab"**。那条回退会导航用户自己的标签页，
    在"任意 URL 都会走这里"的通用缝上等于随机毁掉用户的浏览现场；失败就如实返回 None。
    """
    import urllib.parse as _up
    import urllib.request as _ur
    try:
        req = _ur.Request(CDP_BASE + "/json/new?" + _up.quote(url, safe=""), method="PUT")
        tab = _json.loads(_ur.urlopen(req, timeout=8).read().decode("utf-8", "replace"))
    except Exception as e:
        _log.debug("CDP /json/new 失败：%s", e)
        return None
    if not isinstance(tab, dict) or not tab.get("webSocketDebuggerUrl"):
        return None
    return tab


def _cdp_close_tab(tab_id) -> bool:
    """只关**我们自己开的** targetId。绝不关共享 Chrome 进程，也绝不关用户的标签页。"""
    if not tab_id:
        return False
    import urllib.request as _ur
    try:
        _ur.urlopen(CDP_BASE + "/json/close/" + str(tab_id), timeout=5).read()
        return True
    except Exception:
        pass
    ws_url = _cdp_browser_ws()
    if not ws_url:
        return False
    try:
        import websocket as _ws
        ws = _ws.create_connection(ws_url, timeout=8, suppress_origin=True,
                                   http_proxy_host=None, http_proxy_port=None)
        try:
            ws.send(_json.dumps({"id": 1, "method": "Target.closeTarget",
                                 "params": {"targetId": tab_id}}))
            try:
                ws.recv()
            except Exception:
                pass
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return True
    except Exception:
        return False


def _ws_connect(ws_url: str, timeout: float = 15.0):
    """连 CDP WebSocket。两个参数都是踩过坑的：

    · suppress_origin=True —— Chrome 未开 --remote-allow-origins 时会拒带 Origin 的握手（403）；
    · http_proxy_host=None —— 防止"本机调试 socket 被塞进代理"而直接超时（表现为 Chrome 明明活着
      却什么都旁观不到）。已核实 **websocket-client 1.9.0 的 proxy_info 只读显式参数、不读
      http_proxy 环境变量**（老版本才读），此处显式置空是为防旧版本/打包环境里那条老行为。
    """
    if not ws_url:
        return None
    try:
        import websocket as _ws
    except Exception as e:
        _log.debug("websocket-client 不可用：%s", e)
        return None
    try:
        return _ws.create_connection(ws_url, timeout=timeout, suppress_origin=True,
                                     http_proxy_host=None, http_proxy_port=None)
    except Exception as e:
        _log.debug("CDP ws 连接失败：%s", e)
        return None


# ---------------- CDP 泵（id 配对 + 事件分发）----------------

class _Pump:
    """一条 CDP WebSocket 的收发泵。命令按 id 阻塞等回包，途中收到的事件一律喂给 watcher。

    重入保护：事件处理里（Target.attachedToTarget → 对新 session 补发 Network.enable）
    会**在等回包的过程中**再发命令 → 直接嵌套 send 会吃掉彼此的 id 回包。故 _busy 时命令
    进 _deferred，由最外层 send 的 drain 循环统一发掉。
    """

    def __init__(self, ws, *, log=None, cmd_timeout: float = _CMD_TIMEOUT):
        self.ws = ws
        self.watcher = None
        self.log = log
        self.cmd_timeout = float(cmd_timeout)
        self._mid = 0
        self._busy = False
        self._deferred = []
        self.closed = False

    # -- 收发原语 --

    def _recv_raw(self, timeout: float):
        """收一条消息。超时/非 JSON → None（正常）；连接关闭 → 置 closed 并返回 None。"""
        try:
            self.ws.settimeout(timeout)
            raw = self.ws.recv()
        except Exception as e:
            name = type(e).__name__.lower()
            if not any(k in name for k in ("timeout", "blockingio", "wouldblock", "again")):
                self.closed = True
            return None
        if not raw:
            self.closed = True
            return None
        try:
            return _json.loads(raw)
        except Exception:
            return None

    def _dispatch(self, msg):
        if not isinstance(msg, dict):
            return
        if msg.get("method") and self.watcher is not None:
            self.watcher.on_message(msg)

    # -- 对外 --

    def send(self, method, params=None, sid=""):
        if self.closed:
            return None
        if self._busy:                      # 重入：排队，交给最外层 drain 循环
            self._deferred.append((method, params or {}, sid))
            return None
        self._busy = True
        try:
            res = self._send_now(method, params or {}, sid)
            while self._deferred:
                m, p, s = self._deferred.pop(0)
                self._send_now(m, p, s)
            return res
        finally:
            self._busy = False

    def _send_now(self, method, params, sid):
        self._mid += 1
        mid = self._mid
        msg = {"id": mid, "method": method, "params": params}
        if sid:
            msg["sessionId"] = sid
        try:
            self.ws.send(_json.dumps(msg))
        except Exception as e:
            _log.debug("CDP 下发 %s 失败：%s", method, e)
            self.closed = True
            return None
        deadline = _time.time() + self.cmd_timeout
        while not self.closed and _time.time() < deadline:
            m = self._recv_raw(0.25)
            if m is None:
                continue
            if m.get("id") == mid:
                if m.get("error"):
                    _log.debug("CDP %s 报错：%s", method, m.get("error"))
                return m.get("result")
            self._dispatch(m)
        return None

    def poll(self, timeout: float = 0.25):
        """阻塞一次收包 + 把已排队的包收干净；事件全部喂 watcher。"""
        m = self._recv_raw(timeout)
        if m is not None:
            self._dispatch(m)
        while not self.closed:
            m = self._recv_raw(0.05)
            if m is None:
                return
            self._dispatch(m)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------- 触发 ----------------

def _dispatch_trigger(pump, round_i: int, *, log=None):
    """一轮触发。返回 _TRIGGER_JS 的 {title,cx,cy,…}；失败返回 None。

    升级节奏：轮 0 纯 JS → 轮 1 加真鼠标点击 → 轮 2 再加 Esc（关掉 Esc 可关的浮层）。
    真鼠标点击是成败所系：桌面 Chrome 默认"无用户手势不 play()"，纯 JS .play() 会被拒。
    """
    if round_i <= 1:                       # 复用 vg 的通用播放按钮选择器表（只读常量，不改 vg）
        try:
            import video_grab as _vg
            pump.send("Runtime.evaluate", {"expression": _vg._CLICK_PLAY_JS,
                                           "userGesture": True, "returnByValue": True})
        except Exception:
            pass
    info = None
    try:
        r = pump.send("Runtime.evaluate", {"expression": _TRIGGER_JS,
                                           "userGesture": True, "returnByValue": True})
        v = ((r or {}).get("result") or {}).get("value")
        if isinstance(v, dict):
            info = v
    except Exception:
        info = None
    if info is None:
        return None
    cx, cy = int(info.get("cx") or 0), int(info.get("cy") or 0)
    if round_i >= 1 and cx > 0 and cy > 0:
        for typ, btns in (("mousePressed", 1), ("mouseReleased", 0)):
            try:
                pump.send("Input.dispatchMouseEvent", {
                    "type": typ, "x": cx, "y": cy, "button": "left",
                    "clickCount": 1, "buttons": btns})
            except Exception:
                pass
        _i(log, "轮%d 触发：点击播放器中心 (%d,%d) %dx%d", round_i + 1, cx, cy,
           int(info.get("w") or 0), int(info.get("h") or 0))
    if round_i >= 2:
        for typ in ("keyDown", "keyUp"):
            try:
                pump.send("Input.dispatchKeyEvent", {
                    "type": typ, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
            except Exception:
                pass
    return info


# ---------------- 主入口 ----------------

def capture_tracks(url: str, *, want: str = WANT_BOTH, wait_sec=None, idle_grace=None,
                   driver=None, log=None, stop_flag=None, replay=None,
                   allow_launch=None, keep_tab: bool = False) -> CaptureResult:
    """打开 url、旁观页面自己的网络活动、返回 CaptureResult。**绝不 raise。**

    driver      调用方已有浏览器会话 → 强制 allow_launch=False（绝不在别人持有浏览器时去拉起/杀进程）
    stop_flag   可调用对象，返回 True 即提前收工
    replay      离线测试钩子：canned 事件（或 (events, bodies) 二元组）→ 直接走 replay_capture
    keep_tab    调试用：True 则收工后不关 tab
    """
    if replay is not None:
        ev, bodies = (replay if isinstance(replay, tuple) and len(replay) == 2
                      else (replay, None))
        res, _sent = replay_capture(ev, bodies, url=url, want=want)
        return res

    pre = []                                     # 结构性失败（与 watcher 的识别失败分开记）
    if driver is not None and allow_launch is None:
        allow_launch = False

    st = ensure_shared_chrome(url, allow_launch=allow_launch, log=log)
    if st == "denied":
        return CaptureResult(url=url, notes=[
            "共享 Chrome（CDP 9222）未在跑，且 MEDIA_CAPTURE_ALLOW_CHROME_LAUNCH=False → 不做浏览器旁观"])
    if st == "failed":
        return CaptureResult(url=url, notes=[
            "共享 Chrome 未在跑且拉起失败 → 不做浏览器旁观（可先手工开一次浏览器再重试）"])
    if st == "launched":
        pre.append("本次为旁观拉起了共享 Chrome（直连模式下可能已关闭用户自己的 Chrome 窗口）")

    tab = _cdp_open_tab(url)
    if not tab:
        return CaptureResult(url=url, notes=pre + [
            "无法在共享 Chrome 中开新 tab（/json/new 失败）——为不干扰用户，不回落到现有标签页"])
    tab_id = tab.get("id")

    ws = _ws_connect(tab.get("webSocketDebuggerUrl") or "")
    if ws is None:
        if not keep_tab:
            _cdp_close_tab(tab_id)
        return CaptureResult(url=url, notes=pre + [
            "CDP WebSocket 连接失败（Chrome 在跑但连不上调试 socket）——与「页面没发请求」是两回事"])

    pump = _Pump(ws, log=log)
    w = _Watcher(page_url=url, want=want, send=pump.send, now=_time.time, log=log,
                 wait_sec=(_cfg("MEDIA_CAPTURE_WAIT_SEC", D_WAIT_SEC) if wait_sec is None else wait_sec),
                 idle_grace=(_cfg("MEDIA_CAPTURE_IDLE_GRACE", D_IDLE_GRACE) if idle_grace is None else idle_grace),
                 body_max_bytes=_cfg("MEDIA_CAPTURE_BODY_MAX_BYTES", D_BODY_MAX_BYTES),
                 max_bodies=_cfg("MEDIA_CAPTURE_MAX_BODIES", D_MAX_BODIES),
                 max_tracks=_cfg("MEDIA_CAPTURE_MAX_TRACKS", D_MAX_TRACKS))
    pump.watcher = w

    w.subscribe()                                # 必须在 Page.navigate **之前**
    nav = pump.send("Page.navigate", {"url": url})
    if isinstance(nav, dict) and nav.get("errorText"):
        pre.append("页面导航报错：%s" % nav["errorText"])

    t_nav = _time.time()
    rounds = 0
    try:
        while w.wants_more():
            if stop_flag is not None:
                try:
                    if stop_flag():
                        break
                except Exception:
                    pass
            while rounds < len(_TRIGGER_AT) and (_time.time() - t_nav) >= _TRIGGER_AT[rounds]:
                info = _dispatch_trigger(pump, rounds, log=log)
                if isinstance(info, dict) and info.get("title") and not w.title:
                    w.title = str(info["title"])
                w.mark_trigger()
                rounds += 1
            pump.poll(0.25)
            w.drain()
        w.drain()
    finally:
        pump.close()
        if not keep_tab:
            _cdp_close_tab(tab_id)               # 只关自建 tab；共享 Chrome 进程不动

    res = w.result()
    for n in reversed(pre):
        res.notes.insert(0, n)
    if w.requests_seen == 0 and not pre:
        res.notes.insert(0, "触发未生效或页面未发起网络活动（已前台化+三轮触发；可能是纯 JS 渲染失败/需登录）")
    return res


def page_watch(url: str, *, want: str = WANT_BOTH, **kw) -> list:
    """给 media_resolve._page_watch 用的便捷入口：只回 [MediaTrack, …]（失败恒 []，绝不 raise）。"""
    try:
        return list(capture_tracks(url, want=want, **kw).tracks)
    except Exception as e:
        _log.debug("page_watch 失败：%s", e)
        return []
