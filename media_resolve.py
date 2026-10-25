# media_resolve.py — 通用媒体源解析层（任意 URL → MediaTrack 候选列表 → 可下载）。
# 目标：把散在 video_grab/audio_grab 的散件串成"粘贴任意 URL → 自动判类 → 发现媒体 → 勾选下载"的
# 一键自动链；平台配方 site_recipes/* 命中优先，未命中落通用层（静态深挖 → 浏览器兜底，M1 接）。
# 本文件为地基（M0）：只含数据模型 + 纯函数 + 平台配方注册表；浏览器发现/下载路由由 M1 独立模块接入。
#
# 既有 video_grab/audio_grab 下载函数体一行不改：本模块顶层仅 stdlib，vg/ag 一律在函数内懒加载
# （同 schema_engine"仅函数体 import"先例）；分类/转换复用的是其原语，不 copy 实现。
#
# 边界：商业 DRM（Widevine/PlayReady/FairPlay）在此只识别、注记并跳过，绝不尝试解密。
import threading
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ================================================================
# DRM 识别（只识别跳过，绝不解密）
# ================================================================
# EME keySystem → 方案名。ClearKey 是"明文密钥即解"的保护（D1 已能离线解密/或人工给 key），
# 不算规避商业技术保护措施 → 不入 COMMERCIAL_DRM。
DRM_KEY_SYSTEMS = {
    "com.widevine.alpha": "widevine",
    "com.microsoft.playready": "playready",
    "com.apple.fps": "fairplay",
    "org.w3.clearkey": "clearkey",
}
# 商业 DRM：解密=规避技术保护措施，任何站点/任何授权都只识别不实现解密。
COMMERCIAL_DRM = frozenset({"widevine", "playready", "fairplay"})


def drm_name_from_key_system(key_system: str) -> str:
    """EME keySystem 字符串 → 方案名；未知返回原串小写。"""
    ks = (key_system or "").strip().lower()
    return DRM_KEY_SYSTEMS.get(ks, ks)


def drm_is_commercial(scheme: str) -> bool:
    """该 DRM 方案是否属商业 DRM（Widevine/PlayReady/FairPlay）→ 只可跳过不可解密。"""
    return (scheme or "").strip().lower() in COMMERCIAL_DRM


# ================================================================
# 数据模型（轻量导出；内部可承载现成 VideoCandidate/AudioCandidate，见文件尾转换器）
# ================================================================

# 解析路径注记（PageResolve.route / MediaTrack.src 前缀可复用的取值约定）
ROUTE_RECIPE = "recipe"     # 平台配方命中（site_recipes/*）
ROUTE_STATIC = "static"     # 静态深挖（页内元素/JS字面量/播放器配置）
ROUTE_BROWSER = "browser"   # 浏览器资源级捕获兜底
ROUTE_BROWSER_API = "browser-api"   # 浏览器旁观页面自己的 API 请求/响应体捞出的媒体地址
ROUTE_NONE = "none"         # 未发现任何候选


@dataclass
class MediaTrack:
    """解析出的一条可下载媒体（视频或音频）。M1 起作为 GUI 表格行 / 下载路由的载体。"""
    index: int              # 页面内序号（从 1 开始）
    media_class: str        # "video" | "audio" | ""（""=纯清单 m3u8/mpd，音视频待探针/上下文定）
    kind: str               # "m3u8" | "dash" | "direct"
    url: str                # 绝对 URL（已 urljoin）
    ext: str                # mp4/flv/mp3/m4a/…；无法判定为 ""
    src: str                # 发现方式（元素/JS字面量/播放器配置/浏览器DOM/资源捕获/配方）
    page_url: str           # 来源页面（下载 Referer）
    title_hint: str = ""
    duration_s: float = 0.0   # 已知时长(秒)；未知 0.0
    cookies: dict = field(default_factory=dict)   # 浏览器收割的同域登录/防盗链 cookie
    drm: list = field(default_factory=list)       # 识别到的 DRM 方案（含 commercial → 上层跳过注记）
    note: str = ""            # 附注（DRM 命中 / 降级说明等）
    parts: list = field(default_factory=list)     # 复合轨子件（可选）：[{media_class,kind,url,ext},...]
    #  → download_track 命中即先下载全部子件、再 ffmpeg 按媒体类合成单文件（DASH 分轨 v+a m4s 等）。
    ephemeral: bool = False   # 页面现场直链（带时效 token，离开页面即可能失效）→ GUI 打角标、须尽快下载
    #  → 由 media_capture 旁观到的 API 直链一律置位；只写 note 会被 _safe_stem/GUI 淹没。

    @property
    def is_drm_commercial(self) -> bool:
        """是否命中商业 DRM → 该轨只注记不可下载。"""
        return any(drm_is_commercial(d) for d in self.drm)


@dataclass
class PageResolve:
    """单个 URL 的解析结果（resolve_page 的返回体，M1 实现；此处先定型供 GUI/测试引用）。"""
    url: str                 # 被解析的原始 URL
    route: str = ROUTE_NONE  # 命中路径：recipe/static/browser/none
    tracks: list = field(default_factory=list)   # [MediaTrack, ...]
    notes: list = field(default_factory=list)    # [str, ...] 注记（如配方名、DRM 跳过说明）


# ================================================================
# 通用小工具
# ================================================================


def _netloc_of(url: str) -> str:
    """URL → 去端口/去 userinfo 的 netloc（lower）。解析失败返回 ''。"""
    try:
        netloc = urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return netloc.split(":", 1)[0]


def host_suffix_match(host: str, suffix: str) -> bool:
    """host 是否等于/以 suffix 结尾（子域命中）。suffix 形如 "bilibili.com"（首点可省）。
    精确前缀边界保护：evilbilibili.com 绝不命中 bilibili.com。"""
    host = (host or "").strip().lower().rstrip(".")
    suffix = (suffix or "").strip().lower().lstrip(".").rstrip(".")
    if not host or not suffix:
        return False
    return host == suffix or host.endswith("." + suffix)


def _canon_url(url: str) -> str:
    """去重规范键：剥 fragment（#…）。与 vg._canon_key 同语义。"""
    return (url or "").split("#", 1)[0]


def is_http_url(url: str) -> bool:
    """仅 http/https 可下载（其余直返 False）。"""
    try:
        return urlparse(url or "").scheme.lower() in ("http", "https")
    except Exception:
        return False


# ================================================================
# 判类（kind/ext/media_class 三线合一；懒加载 vg/ag 分类原语）
# ================================================================


def classify_media(media_url: str):
    """任意媒体 URL → (kind, ext, media_class)；无法识别 → (None, None, None)。
    media_class: "video"/"audio"/""（"" 只出现在纯清单 m3u8/mpd —— 音视频须由上下文/探针定夺）。
    判类顺序：先视频白名单（vg._classify）再音频白名单（ag._classify_audio），二者互斥不收异类扩展；
    m3u8/mpd 双线同收、以清单看待（media_class=""）。"""
    import video_grab as _vg
    import audio_grab as _ag
    v = _vg._classify(media_url)
    if v and v[0]:
        kind, ext = v
        return kind, ext, ("" if kind in ("m3u8", "dash") else "video")
    a = _ag._classify_audio(media_url)
    if a and a[0]:
        kind, ext = a
        return kind, ext, ("" if kind in ("m3u8", "dash") else "audio")
    return None, None, None


# ================================================================
# 平台配方注册表（site_recipes/* 在 import 时经 register_media_resolver 自注册）
# ================================================================
_RESOLVERS = []          # [(suffix, entry)]，保插入序；_media_resolver_for 按最长 suffix 命中
_RESOLVER_LOCK = threading.Lock()


def register_media_resolver(host_suffix: str, resolve_fn, *, name: str = "",
                            enabled: bool = True, order: int = 100):
    """注册一个平台解析器。host_suffix：命中后缀（"bilibili.com"，子域自动命中）。
    resolve_fn：签名 (url, html=None) → 现成候选列表或 None（M2 起有配方才真正调用，此处仅入册）。
    name：配方名（空→用 host_suffix）。enabled=False 注册但不参与命中。同 suffix 重复注册覆盖旧的。"""
    global _RESOLVERS
    entry = {
        "host_suffix": (host_suffix or "").strip().lower().lstrip("."),
        "resolve": resolve_fn,
        "name": name or host_suffix,
        "enabled": bool(enabled),
        "order": int(order),
    }
    with _RESOLVER_LOCK:
        _RESOLVERS = [e for e in _RESOLVERS if e["host_suffix"] != entry["host_suffix"]]
        _RESOLVERS.append(entry)
        _RESOLVERS.sort(key=lambda e: (e["order"], e["host_suffix"]))
    return entry


def reset_media_resolvers():
    """清空注册表（供测试/热重载）。"""
    global _RESOLVERS
    with _RESOLVER_LOCK:
        _RESOLVERS = []


def _media_resolver_for(url: str):
    """URL → 命中的配方 entry（enabled 且后缀最长命中优先）；无 → None。"""
    host = _netloc_of(url)
    if not host:
        return None
    hit = None
    with _RESOLVER_LOCK:
        for e in _RESOLVERS:
            if not e["enabled"]:
                continue
            if host_suffix_match(host, e["host_suffix"]):
                if hit is None or len(e["host_suffix"]) > len(hit["host_suffix"]):
                    hit = e
    return hit


def registered_media_resolvers() -> list:
    """已注册配方快照（(suffix, name, enabled) 元组列表，供 GUI/诊断展示）。"""
    with _RESOLVER_LOCK:
        return [(e["host_suffix"], e["name"], e["enabled"]) for e in _RESOLVERS]


def unregister_media_resolvers(owner: str) -> int:
    """撤掉某一属主注册的**全部**解析器，返回撤掉条数（属主=注册器盖在 resolve 上的
    `_recipe_owner`）。配方热重载用：JSON 被删除/停用/改名/换后缀时，旧条目必须能退场。

    没有这个函数时，注册表**只能整体清空**（reset_media_resolvers），而整体清空会连带
    抹掉别处以同名注册的东西；结果是旧配方永久留在注册表里直到进程重启 —— 表现为
    "我把 json 删了/停用了，怎么还在生效"，且毫无提示。
    """
    global _RESOLVERS
    with _RESOLVER_LOCK:
        keep = [e for e in _RESOLVERS
                if getattr(e.get("resolve"), "_recipe_owner", None) != owner]
        n = len(_RESOLVERS) - len(keep)
        _RESOLVERS = keep
    return n


# ================================================================
# 分享/短链重定向归一（M3 A 件）：b23.tv / youtu.be / iesdouyin.com 等
#   → resolve_page 配方命中前先取重定向 final，否则短链 host 匹配不到任何配方/静态走错页。
#   仅当 host 命中已注册短表才触发（免去普通 URL 一次多余网络往返）。
#   _short_http 为测试注入钩子（默认 None → 懒 requests GET 只取 r.url，失败/非 http 原样返回）。
#   短表种子由各平台配方 import 时 register_short_host 自注（bilibili→b23.tv，youtube→youtu.be…）。
# ================================================================
_SHORT_HOSTS = []            # 已注册短链/分享域名后缀（同 host_suffix_match 语义）
_SHORT_LOCK = threading.Lock()
_short_http = None           # 测试可注入: (url)->final_url_str；None → 默认真网 GET
_SHORT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def register_short_host(host_suffix):
    """注册短链/分享域名后缀（"b23.tv"，子域自动命中）。配方 import 时自注；重复注册覆盖。"""
    global _SHORT_HOSTS
    s = (host_suffix or "").strip().lower().lstrip(".")
    if not s:
        return
    with _SHORT_LOCK:
        _SHORT_HOSTS = [h for h in _SHORT_HOSTS if h != s] + [s]


def reset_short_hosts():
    """清空短表（测试隔离用）。"""
    with _SHORT_LOCK:
        _SHORT_HOSTS = []


def unregister_short_host(host_suffix):
    """撤掉一个短链/分享域名后缀（配方热重载：换掉 short_hosts 时旧的要退场）。"""
    global _SHORT_HOSTS
    s = (host_suffix or "").strip().lower().lstrip(".")
    if not s:
        return
    with _SHORT_LOCK:
        _SHORT_HOSTS = [h for h in _SHORT_HOSTS if h != s]


def _is_short_host(host: str) -> bool:
    return any(host_suffix_match(host, s) for s in _SHORT_HOSTS)


def _http_final(url: str) -> str:
    """默认短链实现：requests GET allow_redirects=True, stream=True 立即关体只取 r.url。"""
    import requests as _requests
    r = _requests.get(url, headers={"User-Agent": _SHORT_UA},
                      timeout=8, allow_redirects=True, stream=True)
    try:
        return r.url or url
    finally:
        r.close()


def _expand_short(url: str, *, max_hops: int = 5) -> str:
    """命中短表才重定向取 final（最多 max_hops 跳）；落到普通域名即止。
    失败/非 http/未命中短表 → 原样返回。绝不上抛。"""
    if not is_http_url(url):
        return url
    host = _netloc_of(url)
    if not host or not _is_short_host(host):
        return url
    cur = url
    try:
        for _ in range(max_hops):
            nxt = _short_http(cur) if _short_http is not None else _http_final(cur)
            if not nxt or not is_http_url(nxt) or nxt == cur:
                break
            cur = nxt
            if not _is_short_host(_netloc_of(nxt)):   # 已到普通目标页，收敛
                break
        return cur
    except Exception:
        return url


# ================================================================
# 站点"浏览器辅助"捕获缝（M3 douyin 等平台）
#   resolve_url 纯 HTTP 恒 []（a_bogus 等签名挡门，登录也只提档不解签名）→ 配方模块另注册一个 seam：
#   在 resolve_page 浏览器兜底步内、通用 detect 之前被调用。seam 自带 Chrome 生命周期，只做
#   "让真 Chrome 现场借自己签名拉一次数据/自己已拉的数据"（douyin：CDP Network 读 detail JSON 取直链），
#   返回 [MediaTrack, ...]。红线：不解密、不产品化签名、不重做全程整流录制；失败返回 [] 落通用兜底。
# ================================================================
_BROWSER_SEAMS = []          # [(suffix, entry)]，保插入序；_browser_seam_for 按最长 suffix 命中
_BSEAM_LOCK = threading.Lock()


def register_browser_seam(host_suffix, seam_fn, *, name: str = ""):
    """注册一个站点浏览器辅助捕获函数。host_suffix：命中后缀（"douyin.com"，子域自动命中）。
    seam_fn：签名 (url) → [MediaTrack, ...]（现场起/连共享 Chrome 完成平台私有数据拉取）。
    name：名称（空→用 host_suffix）。同 suffix 重复注册覆盖旧的。"""
    global _BROWSER_SEAMS
    entry = {
        "host_suffix": (host_suffix or "").strip().lower().lstrip("."),
        "seam": seam_fn,
        "name": name or host_suffix,
    }
    with _BSEAM_LOCK:
        _BROWSER_SEAMS = [s for s in _BROWSER_SEAMS if s["host_suffix"] != entry["host_suffix"]]
        _BROWSER_SEAMS.append(entry)
    return entry


def reset_browser_seams():
    """清空浏览器辅助缝注册表（测试隔离用）。"""
    global _BROWSER_SEAMS
    with _BSEAM_LOCK:
        _BROWSER_SEAMS = []


def registered_browser_seams() -> list:
    """已注册浏览器辅助缝快照（(suffix, name) 列表，供诊断展示）。"""
    with _BSEAM_LOCK:
        return [(s["host_suffix"], s["name"]) for s in _BROWSER_SEAMS]


def unregister_browser_seams(owner: str) -> int:
    """撤掉某一属主注册的浏览器辅助缝（同 unregister_media_resolvers 的理由）。"""
    global _BROWSER_SEAMS
    with _BSEAM_LOCK:
        keep = [s for s in _BROWSER_SEAMS
                if getattr(s.get("seam"), "_recipe_owner", None) != owner]
        n = len(_BROWSER_SEAMS) - len(keep)
        _BROWSER_SEAMS = keep
    return n


def _browser_seam_for(url: str):
    """URL → 命中的浏览器辅助缝 entry（后缀最长优先）；无 → None。"""
    host = _netloc_of(url)
    if not host:
        return None
    hit = None
    with _BSEAM_LOCK:
        for s in _BROWSER_SEAMS:
            if host_suffix_match(host, s["host_suffix"]):
                if hit is None or len(s["host_suffix"]) > len(hit["host_suffix"]):
                    hit = s
    return hit


# ================================================================
# 去重 / DRM 分流
# ================================================================


def dedupe_tracks(tracks: list) -> list:
    """按 (media_class, kind, 规范URL) 去重，保序返回新列表。空/坏候选跳过。"""
    seen = set()
    out = []
    for t in tracks:
        if t is None or not getattr(t, "url", None):
            continue
        key = (t.media_class, t.kind, _canon_url(t.url))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def partition_drm(tracks: list):
    """候选 → (可下载候选, DRM 命中候选)。DRM 命中=任一 drm 为商业 DRM —— 只注记不下载。"""
    clean, blocked = [], []
    for t in tracks:
        (blocked if t.is_drm_commercial else clean).append(t)
    return clean, blocked


# ================================================================
# MediaTrack ↔ VideoCandidate/AudioCandidate 转换（下载路由复用 vg/ag 现成栈）
# ================================================================


def from_video_candidate(c) -> MediaTrack:
    """video_grab.VideoCandidate → MediaTrack（media_class='video'）。"""
    return MediaTrack(index=c.index, media_class="video", kind=c.kind, url=c.url,
                      ext=c.ext, src=c.src, page_url=c.page_url,
                      title_hint=getattr(c, "title_hint", "") or "",
                      cookies=dict(getattr(c, "cookies", None) or {}))


def from_audio_candidate(c) -> MediaTrack:
    """audio_grab.AudioCandidate → MediaTrack（media_class='audio'）。"""
    return MediaTrack(index=c.index, media_class="audio", kind=c.kind, url=c.url,
                      ext=c.ext, src=c.src, page_url=c.page_url,
                      title_hint=getattr(c, "title_hint", "") or "",
                      cookies=dict(getattr(c, "cookies", None) or {}))


def to_video_candidate(t: MediaTrack):
    """MediaTrack → video_grab.VideoCandidate（喂 vg.download_direct/dash/m3u8）。"""
    import video_grab as _vg
    return _vg.VideoCandidate(index=t.index, kind=t.kind, url=t.url, ext=t.ext,
                              src=t.src, page_url=t.page_url,
                              title_hint=t.title_hint,
                              cookies=dict(t.cookies or {}))


def to_audio_candidate(t: MediaTrack):
    """MediaTrack → audio_grab.AudioCandidate（喂 ag._download_audio_by_kind）。"""
    import audio_grab as _ag
    return _ag.AudioCandidate(index=t.index, kind=t.kind, url=t.url, ext=t.ext,
                              src=t.src, page_url=t.page_url,
                              title_hint=t.title_hint,
                              cookies=dict(t.cookies or {}))


# ================================================================
# M1 通用自动链：resolve_page / resolve_pages + download_track / download_tracks
# 顶层仍只 stdlib；vg/ag/config 一律函数内懒加载。静态双线深挖复用 vg._resolve_candidates /
# ag._resolve_audio_from_page（现有原语，不重写）；浏览器兜底经 _browser_discover 间接层
# （默认=vg.detect_media_browser_enhanced；离线测试可整体替换成伪 driver 版，确定性跑 E2E）。
# DRM：浏览器 EME 探测只注记到 notes（是否整页 DRM 无法据此逐轨断定，不误标真免费轨）；
# 显式带 drm 的候选（配方/未来接入）仍由 partition_drm / download_track 前置恒跳过。
# ================================================================

_browser_discover = None     # (url, classify_fn, wait_sec) -> (cands, notes, eme)；None=默认实现


def _browser_discover_default(url, classify_fn, wait_sec):
    import video_grab as _vg
    return _vg.detect_media_browser_enhanced(url, classify_fn=classify_fn, wait_sec=wait_sec)


# 通用「页面 API 旁观」间接层（media_capture）：用 CDP 旁观页面自己发出的请求与响应体，
# 解决"前端播放器 + 后端 API、页面无直链"的普遍形态；worker 内请求也能覆盖。
# 排在平台缝之后、通用 detect 之前（见 resolve_page ③-2）。同样可整体替换以便离线确定性测试。
_page_watch = None           # (url, want) -> [MediaTrack, …]；None=默认实现


def _page_watch_default(url, want="both"):
    """懒 import media_capture（顶层仅 stdlib 的纪律：重依赖与 Chrome 生命周期都不进顶层）。"""
    from media_capture import page_watch as _pw
    return _pw(url, want=want)


def set_page_watch(fn):
    """替换/清空（传 None）通用 API 旁观实现；返回值可用于还原。"""
    global _page_watch
    old = _page_watch
    _page_watch = fn
    return old


def _combined_kind_ext(u):
    """(url) -> (kind, ext)：视频+音频一次收齐的分类器适配（喂 detect_videos_browser）。"""
    k, e, _cls = classify_media(u)
    return k, e


# ================================================================
# probe_track（M3 B 件，预留 MEDIA_TRACK_PROBE；默认关）
#   对"无扩展名 / 未知清单"候选做内容嗅探：Range bytes=0-2047 取首 2KB → 魔数补 kind/ext。
#   顶层零网络依赖；_probe_http 为测试注入钩子（默认 None → 函数内懒 requests）。
# ================================================================
_probe_http = None    # 测试可注入: (url)->bytes 首块（≤2KB）；None → 默认真网 Range GET


def _fetch_head_bytes(url: str) -> bytes:
    import requests as _requests
    h = {"User-Agent": _SHORT_UA, "Range": "bytes=0-2047", "Accept": "*/*"}
    r = _requests.get(url, headers=h, timeout=8, stream=True)
    try:
        try:
            return r.raw.read(2048)
        except Exception:
            return (r.content or b"")[:2048]
    finally:
        r.close()


def _sniff_head(data: bytes):
    """首 2KB 内容嗅探 → (kind, ext, media_class)；无法判定 → (None, None, None)。
    判据：EXTM3U/MPD 文本清单；ftyp=mp4(直链)；ID3/帧同步=mp3；RIFF WAVE；Ogg；Matroska。"""
    if not data:
        return None, None, None
    head = data[:2048]
    lead = head.lstrip()
    if lead.startswith(b"#EXTM3U"):
        return "m3u8", "m3u8", ""
    if b"<MPD" in head or b"<mpd" in head:
        return "dash", "mpd", ""
    ftyp = head.find(b"ftyp", 0, 32)
    if ftyp >= 0 and (ftyp + 4) <= len(head):
        return "direct", "mp4", "video"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "direct", "mp3", "audio"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "direct", "wav", "audio"
    if head[:4] == b"OggS":
        return "direct", "ogg", "audio"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "direct", "mkv", "video"
    return None, None, None


def probe_track(track) -> tuple:
    """对候选做内容嗅探补齐：仅当 ext/kind 缺失或为清单时取首 2KB 判类，就地补 track.ext/kind/media_class。
    失败一律吞掉不阻塞（返回 (None,None,None)）。返回补得的 (kind, ext, media_class)。"""
    if track is None or not getattr(track, "url", None):
        return None, None, None
    if getattr(track, "ext", None) and getattr(track, "kind", None) \
            not in (None, "", "m3u8", "dash"):
        return None, None, None          # 已具扩展名且非清单 → 无需嗅探
    try:
        data = _probe_http(track.url) if _probe_http is not None else _fetch_head_bytes(track.url)
    except Exception:
        return None, None, None
    k, e, cls = _sniff_head(data)
    if k:
        track.kind, track.ext = k, e
        if cls and not track.media_class:
            track.media_class = cls
    return k, e, cls


def _tracks_from_cands(cands, fallback_class: str) -> list:
    """候选 → MediaTrack 列表。
    - 平台配方直产 MediaTrack（最富契约：media_class/kind/drm/note/duration_s 全保真）→ 原样透传。
    - 现成 vg/ag 候选：media_class 由 classify_media 定夺（直链→video/audio；manifest→类留来源上下文）。
    坏 URL 跳过。"""
    tracks = []
    _want_probe = False
    try:
        import config as _cfg
        _want_probe = bool(getattr(_cfg, "MEDIA_TRACK_PROBE", False))
    except Exception:
        pass

    def _adopt(tr):
        # 内容嗅探兜底：仅当 MEDIA_TRACK_PROBE 开启且该候选缺扩展名/是未知清单 → 补 kind/ext（失败吞掉）
        if _want_probe and (not tr.ext or tr.kind in (None, "", "m3u8", "dash")):
            probe_track(tr)
        tracks.append(tr)

    def _fill_recipe(tr):
        """配方自产的轨：**只补空字段，绝不覆盖配方显式写下的值**。
        修的是一条从未兑现的承诺 —— 说明.md:420 教用户"media_class 留空由程序按扩展名猜"，但配方轨
        原来是整条透传、根本不猜：留空 → download_track 判 == "audio" 为假 → 落视频分支（下进
        output/video、还被命名成 .mp4），全程不报错。
        kind **不碰**：载入期已由 media_recipe._norm_kind 归一，这里再判会盖掉显式的 direct/dash。
        m3u8/mpd 清单的 media_class 本就该留空由上下文定夺（classify_media 对它们返回 ""）→ 只有
        fallback_class（want 上下文）会补；want="both"（默认）时与改动前完全一致。"""
        if tr.media_class and tr.ext:
            return
        try:
            _k, _e, _cls = classify_media(tr.url)
        except Exception:
            _e, _cls = None, None
        if not tr.media_class and _cls:
            tr.media_class = _cls
        if not tr.ext and _e:
            tr.ext = _e
        if not tr.media_class and fallback_class:
            tr.media_class = fallback_class

    for c in cands or []:
        u = getattr(c, "url", "")
        if not u:
            continue
        if isinstance(c, MediaTrack):
            _fill_recipe(c)        # 配方自产：显式值原样保留，只补留空的（见 _fill_recipe）
            _adopt(c)
            continue
        tr = MediaTrack(
            index=int(getattr(c, "index", 0) or 0),
            media_class="",
            kind=getattr(c, "kind", "") or "",
            url=u,
            ext=getattr(c, "ext", "") or "",
            src=getattr(c, "src", "") or "通用发现",
            page_url=getattr(c, "page_url", u) or u,
            title_hint=getattr(c, "title_hint", "") or "",
            cookies=dict(getattr(c, "cookies", None) or {}))
        k, e, cls = classify_media(u)
        if cls:
            tr.media_class, tr.kind, tr.ext = cls, k, e
        else:
            if k:
                tr.kind, tr.ext = k, e          # manifest：kind/ext 规范化
            if not tr.media_class and fallback_class:
                tr.media_class = fallback_class  # 音频/视频来源上下文（静态线）
        if not tr.media_class:
            tr.media_class = fallback_class
        _adopt(tr)
    return tracks


def resolve_page(url: str, *, log=None, stop_flag=None, use_browser=None,
                 wait_sec: int = 9, want: str = "both", force_recipe="") -> PageResolve:
    """任意 URL → PageResolve（配方 → 静态双线 → 浏览器兜底 自动链）。绝不 raise。
    use_browser：显式 True/False 开关；None → 读 config MEDIA_RESOLVE_AUTO_BROWSER。
    want：音频向/视频向排序提示（"audio"/"video"/"both"）。归类兜底见下面的 fb_class。
    force_recipe：指定只用哪份配方（"自带:youtube" / "用户:mysite" 之类，空=按域名自动匹配）。
      指定后**不降级** —— 见下面 ⓪ 段的理由。
    浏览器 EME 探测只进 notes（注记不误标）；download_track 对显式商业 DRM 候选恒跳过。"""
    import config as _cfg
    notes: list = []
    if not is_http_url(url):
        return PageResolve(url, ROUTE_NONE, [], ["仅支持 http/https URL"])
    tracks: list = []
    route = ROUTE_NONE

    # want 上下文归类兜底：音频向调用（「抓取音频流」弹窗传 want="audio"）下，**无归类**的轨
    # （留空 media_class 的直链、或 m3u8/mpd 这类本来就不定类的清单）归到该侧，下载侧才会走对分支。
    # 改动前只有浏览器旁观（③-2）那条路算了这个值，配方/站点缝三条路恒传 "" —— 同一条规则漏了两处。
    # want="both"（默认）→ ""，与改动前完全一致。详见 _tracks_from_cands._fill_recipe。
    fb_class = {"audio": "audio", "video": "video"}.get(want, "")

    def _note(m):
        if log:
            try:
                log(str(m))
            except Exception:
                pass

    # 短链/分享链接归一：命中短表先取重定向 final（配方/静态命中基于真页；返回体保留用户粘贴原链）
    orig_url = url
    expanded = _expand_short(url)
    if expanded and expanded != url:
        _note("短链/分享链接 → " + expanded)
        url = expanded

    # ⓪ 指定配方（界面上选定了某一份）：只用它，**到不到都就此收工，不降级**。
    #   不降级的理由：这个下拉框同时充当"我写的那份配方到底通不通"的判据。若允许降级，
    #   配额失败后静态线/浏览器捞到了流，界面照样显示解析成功，用户会认定是自己的配方生效了
    #   —— 调试信息就此被抹掉。落空时明确写一条 note 说清楚"没兜底"。
    if force_recipe:
        try:
            import media_recipe as _mr
            ent = _mr.find_recipe(force_recipe)
        except Exception as e:
            ent = None
            notes.append("指定配方不可用（配方引擎载入失败）：%s" % e)
        if ent is None:
            notes.append("指定配方不可用（没找到这份配方）：%s" % force_recipe)
            return PageResolve(orig_url, ROUTE_NONE, [], notes)
        if not ent.get("ok"):
            notes.append("指定配方 %s 校验未通过，本次不解析：%s"
                         % (ent.get("key"), (ent.get("errors") or [""])[0]))
            return PageResolve(orig_url, ROUTE_NONE, [], notes)
        if not ent.get("enabled"):
            notes.append("指定配方 %s 已停用（请先在配方下拉框旁点「启用」），本次不解析"
                         % ent.get("key"))
            return PageResolve(orig_url, ROUTE_NONE, [], notes)
        spec = ent.get("spec") or {}
        _note("指定配方：" + str(ent.get("key")))
        try:
            trs = _mr.tracks_from_spec(spec, url, log=log) or []
        except Exception as e:
            trs = []
            notes.append("指定配方解析异常：%s" % e)
        # 缝一起强制：**现从这份 spec 造一条**，不去按 host_suffix 查注册表。查表会拿到
        # 别的站的缝（拿 youtube 配方去解析抖音网址，就会借到抖音的缝），与"只用这一份"相悖。
        if not trs and (spec.get("browser_seam") or {}).get("url"):
            use_browser_eff = use_browser if use_browser is not None \
                else bool(getattr(_cfg, "MEDIA_RESOLVE_AUTO_BROWSER", True))
            if use_browser_eff:
                _note("指定配方带浏览器辅助缝，旁观页面自己拉的接口…")
                try:
                    seam_fn = _mr.make_browser_seam(spec, str(ent.get("key")))
                    trs = [x for x in (seam_fn(url, log=log, stop_flag=stop_flag) or [])
                           if x is not None]
                except Exception as e:
                    trs = []
                    notes.append("指定配方的浏览器辅助失败：%s" % e)
        tracks = _tracks_from_cands(trs, fb_class) if trs else []
        for t in tracks:
            t.src = "recipe:" + str(ent.get("name") or ent.get("key"))
        if not tracks:
            notes.append("指定配方 %s 没解析出媒体；按设置未做静态/浏览器兜底"
                         % ent.get("key"))
            return PageResolve(orig_url, ROUTE_NONE, [], notes)
        # 走的是提前返回，够不着函数末尾那段统一 dedupe/index，这里自己收口
        tracks = dedupe_tracks(tracks)
        for i, t in enumerate(tracks, 1):
            t.index = i
        notes.append("指定配方命中：" + str(ent.get("key")))
        return PageResolve(orig_url, ROUTE_RECIPE, tracks, notes)

    # ① 平台配方（site_recipes 命中优先；import 自注册，幂等；单平台异常不拖垮）
    try:
        from site_recipes import ensure_registered as _ensure_site_recipes
        _ensure_site_recipes()
    except Exception:
        pass
    # ①-2 用户 JSON 配方（媒体配方/*.json）：**必须在内置之后**加载 —— 注册表是同 suffix 覆盖，
    # 后注册者赢，故用户改一个 json 就能盖掉内置配方，不用等改代码。mtime 幂等，改文件自动重载。
    try:
        import media_recipe as _mr
        _mr.ensure_user_recipes_loaded()
    except Exception:
        pass
    resolver = _media_resolver_for(url)
    if resolver is not None:
        _note("配方命中：" + resolver["name"])
        try:
            res = resolver["resolve"](url)
        except Exception as e:
            res = None
            notes.append(f"配方 {resolver['name']} 解析失败：{e}")
        if res:
            raws = res if isinstance(res, (list, tuple)) else [res]
            tracks = _tracks_from_cands([x for x in raws if x is not None], fb_class)
            for t in tracks:
                t.src = "recipe:" + resolver["name"]
            route = ROUTE_RECIPE

    # ② 静态双线深挖（视频 + 音频，各自 hops；同一 URL 由 dedupe_tracks 收口）
    if not tracks:
        import video_grab as _vg
        import audio_grab as _ag
        try:
            vc = _vg._resolve_candidates(url, log=log, stop_flag=stop_flag) or []
        except Exception:
            vc = []
        try:
            ac = _ag._resolve_audio_from_page(url, log=log, stop_flag=stop_flag) or []
        except Exception:
            ac = []
        tracks = _tracks_from_cands(vc, "video") + _tracks_from_cands(ac, "audio")
        if tracks:
            route = ROUTE_STATIC

    # ③ 浏览器兜底（只找地址+收 cookie，几秒即关；下载走既有栈）
    if not tracks:
        # 注意：这个布尔量原名 `want`，与新参数 `want`（音/视频向）撞名会把参数静默抹掉 —— 改名。
        use_browser_eff = use_browser if use_browser is not None \
            else bool(getattr(_cfg, "MEDIA_RESOLVE_AUTO_BROWSER", True))
        if use_browser_eff:
            # 站点"浏览器辅助"捕获缝优先（douyin 等：HTTP 签名挡门，须真 Chrome 现场借签名拉数据；
            # 先于通用浏览器发现，省一次重型页面加载；空/失败仍落通用兜底）
            seam = _browser_seam_for(url)
            if seam is not None:
                _note("站点浏览器辅助配方：" + seam["name"])
                try:
                    seam_tracks = seam["seam"](url) or []
                except Exception as e:
                    seam_tracks = []
                    notes.append(f"站点浏览器辅助失败：{e}")
                seam_tracks = [x for x in seam_tracks if x is not None]
                if seam_tracks:
                    tracks = _tracks_from_cands(seam_tracks, fb_class)
                    route = ROUTE_BROWSER
                    notes.append("浏览器辅助配方命中：" + seam["name"])
            # ③-2 通用「页面 API 旁观」（media_capture）：CDP 旁观页面自己发的请求/响应体，
            # 专治"前端播放器 + 后端 API、页面无直链"；worker 内请求也覆盖。
            # 排在平台缝之后（缝有平台私有档位知识、更准更省），通用 detect 之前（detect 是页面层
            # fetch/XHR 打点，看不见 worker、读不到跨域响应体）。
            if not tracks:
                _note("静态与平台缝未命中，旁观页面 API…")
                watcher = _page_watch or _page_watch_default
                try:
                    w_tracks = watcher(url, want=want) or []
                except Exception as e:
                    w_tracks = []
                    notes.append(f"页面 API 旁观失败：{e}")
                w_tracks = [x for x in w_tracks if x is not None]
                if w_tracks:
                    # 09-10：原来这里对 MediaTrack 补归一次（因为 _tracks_from_cands 的配方分支会忽略
                    # fallback_class）。现在那条分支统一走 _fill_recipe，补归已在上游完成 —— 这段
                    # 兜底循环是它的特例，删掉（行为不变，规则收成一处）。
                    tracks = _tracks_from_cands(w_tracks, fb_class)
                    route = ROUTE_BROWSER_API
                    notes.append("页面 API 旁观命中 %d 条" % len(tracks))
            if not tracks:
                _note("静态未命中，启用浏览器发现…")
                impl = _browser_discover or _browser_discover_default
                try:
                    cands, bnotes, eme = impl(url, _combined_kind_ext, wait_sec)
                except Exception as e:
                    cands, bnotes, eme = [], [f"浏览器发现异常：{e}"], []
                if eme:
                    notes.append("页探测到 EME:" + ",".join(eme) + "（商业 DRM 只跳过，绝不解密）")
                if cands:
                    tracks = _tracks_from_cands(cands, "")
                    route = ROUTE_BROWSER
                notes.extend(bnotes or [])

    # 旁观来源的候选在去重前按 want 重排（音频优先时清单轨须先于视频直链被判类）；
    # 只对本步生效，配方/静态路径的既有次序一动不动。
    if route == ROUTE_BROWSER_API and tracks:
        try:
            from media_capture import order_tracks as _order_tracks
            tracks = _order_tracks(tracks, want)
        except Exception:
            pass

    tracks = dedupe_tracks(tracks)
    for i, t in enumerate(tracks, 1):
        t.index = i
    if not tracks and not notes:
        notes.append("未发现可下载的音频/视频")
    return PageResolve(orig_url, route, tracks, notes)


def resolve_pages(urls, *, log=None, stop_flag=None, wait_sec: int = 9,
                  want: str = "both", force_recipe="") -> list:
    """多个 URL 依次解析 → [PageResolve, ...]（默认串行；stop_flag 置位即停）。"""
    out = []
    for u in urls or []:
        if stop_flag and stop_flag():
            break
        out.append(resolve_page(u, log=log, stop_flag=stop_flag, wait_sec=wait_sec,
                                want=want, force_recipe=force_recipe))
    return out


def _safe_stem(track: MediaTrack) -> str:
    """下载文件名主干（标题清洗→截断；空标题/坏标题退回域名）。"""
    import re as _re
    s = track.title_hint or ""
    s = _re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s).strip(" ._")
    if not s:
        s = _netloc_of(track.page_url or track.url) or "media"
    if track.index:
        s = "%02d_%s" % (track.index, s)
    return s[:90]


def _unique_file(dirpath: str, stem: str, fext: str) -> str:
    """取 dirpath 下不冲突的完整文件路径（stemN.ext，N=2,3,… 递增）。"""
    import os as _os
    p = _os.path.join(dirpath, stem + fext)
    n = 1
    while _os.path.exists(p):
        n += 1
        p = _os.path.join(dirpath, "%s_%d%s" % (stem, n, fext))
    return p


def _absorb_drm(track, result) -> None:
    """把下载结果里**静态判别**出的 DRM 回填到 track.drm（小写方案名，口径同配方声明）。

    为什么需要：`download_track` 开头那道闸只在「配方显式写了 drm」时拦得住 ——
    那一刻清单还没取，无从知道站方实际怎么标的。本函数补的是**取到清单之后**才成立的
    那部分真相（video_grab 在解析 media 清单时判的）。两者是同一道闸的上下游，别混。

    只增不减：配方已声明的值绝不被覆盖或清空。
    """
    systems = (result or {}).get("drm_systems") or []
    if not systems or track is None:
        return
    have = set()
    for d in (track.drm or []):
        have.add(str(d).strip().lower())
    for s in systems:
        n = str(s).strip().lower()
        if n and n not in have:
            track.drm.append(n)
            have.add(n)


def download_track(track, *, out_dir: str = "", dest: str = "", referer: str = "",
                   progress_callback=None, stop_flag=None, log=None) -> dict:
    """下载单条 MediaTrack → status dict（形态同 vg/ag 下载返回：{status,bytes,path,error,…}）。
    - 显式商业 DRM（track.drm 含 widevine/playready/fairplay）→ 恒跳过，不解密。
    - media_class='audio' → ag._download_audio_by_kind（dest/返回为【无扩展名前缀】）；否则 vg 现成
      download_direct/m3u8/dash（dest 为完整文件路径）。out_dir 空 → config AUDIO_DIR/VIDEO_DIR。"""
    import os as _os
    import config as _cfg
    if track is None:
        return {"status": "fail", "bytes": 0, "path": "", "error": "空候选"}
    if not is_http_url(track.url):
        return {"status": "fail", "bytes": 0, "path": "", "error": "仅支持 http/https"}
    if track.is_drm_commercial:
        return {"status": "skipped", "bytes": 0, "path": "",
                "error": "DRM(%s) 保护：只识别跳过，绝不解密" % ",".join(track.drm)}
    referer = referer or track.page_url
    ck = dict(track.cookies or {}) or None
    stem = _safe_stem(track)
    kind = track.kind or "direct"
    if track.media_class == "audio":
        import audio_grab as _ag
        base_dir = out_dir or _cfg.AUDIO_DIR
        _os.makedirs(base_dir, exist_ok=True)
        out_base = dest or _unique_file(base_dir, stem, "")
        try:
            return _ag._download_audio_by_kind(
                kind, track.url, out_base, ext=track.ext or "",
                referer=referer, progress_callback=progress_callback,
                stop_flag=stop_flag, cookies=ck, log=log)
        except Exception as e:
            return {"status": "fail", "bytes": 0, "path": "", "error": f"音频下载异常：{e}"}
    # 视频 / 未知 manifest（默认按视频出 mp4，产物仍可播）
    import video_grab as _vg
    base_dir = out_dir or _cfg.VIDEO_DIR
    _os.makedirs(base_dir, exist_ok=True)
    if kind in ("m3u8", "dash"):
        fext = ".mp4"
    else:
        fext = "." + (track.ext or "mp4")
    # 复合轨（DASH 分轨 v+a m4s 等）：先下全部子件 → ffmpeg 合成单文件（产物恒 .mp4）
    if getattr(track, "parts", None):
        full = dest or _unique_file(base_dir, stem, ".mp4")
        try:
            return _download_composite(track, full, referer=referer, cookies=ck or {},
                                       progress_callback=progress_callback,
                                       stop_flag=stop_flag, log=log)
        except Exception as e:
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": f"复合轨下载异常：{e}"}
    full = dest or _unique_file(base_dir, stem, fext)
    try:
        if kind == "m3u8":
            r = _vg.download_m3u8(track.url, full, referer=referer,
                                  progress_callback=progress_callback,
                                  stop_flag=stop_flag, cookies=ck)
            _absorb_drm(track, r)      # 清单里读出的 DRM 回填，供诊断/表格/GUI 角标用
            return r
        if kind == "dash":
            return _vg.download_dash(track.url, full, referer=referer,
                                     progress_callback=progress_callback,
                                     stop_flag=stop_flag, cookies=ck)
        return _vg.download_direct(track.url, full, referer=referer,
                                   progress_callback=progress_callback,
                                   stop_flag=stop_flag, cookies=ck)
    except Exception as e:
        return {"status": "fail", "bytes": 0, "path": "", "error": f"下载异常：{e}"}


def download_tracks(tracks, *, out_dir: str = "", stop_flag=None,
                    progress_callback=None, log=None) -> list:
    """顺序下载一批 MediaTrack → [status dict, ...]（默认串行礼貌节拍；单条失败不中断）。"""
    out = []
    for t in tracks or []:
        if stop_flag and stop_flag():
            break
        out.append(download_track(t, out_dir=out_dir, progress_callback=progress_callback,
                                  stop_flag=stop_flag, log=log))
    return out


def _download_composite(track, full: str, *, referer: str = "", cookies=None,
                        progress_callback=None, stop_flag=None, log=None) -> dict:
    """复合轨下载：把 track.parts 各子件整份下载到临时目录 → ffmpeg 按 media_class -map 合成单文件。
    子件字段：media_class("video"/"audio")/kind/url/ext。产物 .mp4。
    典型用途：DASH 分轨（独立 video/audio m4s）各取整份 fMP4，再合成一个可播 mp4。返回 status dict。"""
    import os as _os
    import shutil as _shutil
    import video_grab as _vg
    parts = [p for p in (track.parts or []) if p and p.get("url")]
    if not parts:
        return {"status": "fail", "bytes": 0, "path": "", "error": "复合轨无子件"}
    if not _vg._locate_ffmpeg():
        return {"status": "fail", "bytes": 0, "path": "",
                "error": "复合轨(DASH 分轨)合成需随包 ffmpeg，请确认 runtime/ffmpeg 就位",
                "error_code": _vg.ERR_MISSING_FFMPEG}
    tmpdir = full + ".parts"
    _os.makedirs(tmpdir, exist_ok=True)
    files = []
    try:
        for i, p in enumerate(parts, 1):
            if stop_flag and stop_flag():
                return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
            dst = _os.path.join(tmpdir, "part%d.%s" % (i, p.get("ext") or "mp4"))
            res = _vg.download_direct(
                p["url"], dst, referer=referer or track.page_url,
                progress_callback=progress_callback, stop_flag=stop_flag,
                cookies=cookies)
            if res.get("status") != "ok":
                return {"status": res.get("status", "fail"), "bytes": 0, "path": "",
                        "error": "复合轨子件%d 下载失败：%s" % (i, res.get("error") or ""),
                        "error_code": res.get("error_code") or _vg.ERR_NETWORK}
            files.append(dst)
        args = ["-hide_banner", "-y"]
        for f in files:
            args += ["-i", f]
        for i, p in enumerate(parts):
            cls = p.get("media_class", "")
            if cls == "audio":
                args += ["-map", "%d:a:0" % i]
            elif cls == "video":
                args += ["-map", "%d:v:0" % i]
        args += ["-c", "copy", "-movflags", "+faststart", full]
        rc, tail = _vg._run_ffmpeg(args, log=log, stop_flag=stop_flag)
        if rc != 0:
            return {"status": "fail", "bytes": 0, "path": "",
                    "error": "ffmpeg 复合合成失败(rc=%d)：%s" % (rc, (tail or "").strip()[:200]),
                    "error_code": _vg.ERR_FFMPEG}
        return {"status": "ok", "bytes": _os.path.getsize(full), "path": full, "error": ""}
    except Exception as e:
        if stop_flag and stop_flag():
            return {"status": "stopped", "bytes": 0, "path": "", "error": ""}
        return {"status": "fail", "bytes": 0, "path": "",
                "error": f"复合轨下载异常：{e}", "error_code": _vg.ERR_UNKNOWN}
    finally:
        _shutil.rmtree(tmpdir, ignore_errors=True)
