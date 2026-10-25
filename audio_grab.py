# audio_grab.py 【抓取音频流 v1.0】— 纯后端逻辑模块，不依赖 Qt / mpv
# 功能：两类来源都收——
#   ① 网页/分集页 URL：静态扫描 <audio>/<source>、JS 字面量(m3u8/mpd/音频直链)、
#      播放器配置、JSON-LD/AudioObject 等 → 音频候选 → 逐候选下载（含 iframe/refresh 有界跳转）；
#   ② 音频直链/流 URL（.m3u8/.mpd/.mp3/.m4a/.aac/.wav/.flac/.ogg）批量。
# 落盘到 config.AUDIO_DIR（output/audio）：
#   干净单文件音频（mp3/m4a/aac/wav/flac/ogg 直链）→ 原容器原样保存；
#   需合成者（音频 HLS 分片 / DASH 音频 / ADTS / fMP4-audio / 单文件 DASH 取音轨）
#   → 用随包 ffmpeg 无损 remux 成 .m4a（纯 MP3 → .mp3；opus/vorbis → .ogg；flac → .flac）。
#
# 设计约束（与 video_grab.py 对齐）：
#   - 只复用 video_grab 的通用原语（下载/断点/TLS 指纹取流/ffmpeg/清单解析/目录工具），
#     通过 `import video_grab as vg` 调用，**绝不修改 video_grab.py**。
#   - 绝不经过视频那条硬编码门（_classify/0x47 TS 合并/“无视频轨”拒绝/强制 .ts/.mp4 命名）。
#   - 只做合规公开免登录源：无会员/DRM/代理轮换/多 IP；单 IP 礼貌有界，整部串行。
#   - 模块级 import 只 stdlib + config，不引入 Qt/mpv（打包 hidden-import 即靠此守门）。
#
# 用法：
#   python audio_grab.py <URL|媒体直链...>   # 批量跑并打印 summary JSON
#   python audio_grab.py --selftest          # 纯内存自检（无网络/无 ffmpeg）

import os
import re
import json
import time
import hashlib
import shutil
import threading
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import video_grab as vg          # 通用原语库（纯后端，无 Qt）

from config import (
    AUDIO_DIR, AUDIO_TIMEOUT, AUDIO_CONCURRENT,
    AUDIO_MAX_PAGE_BYTES, AUDIO_MAX_SEGMENTS, AUDIO_SERIES_PARALLEL,
    AUDIO_RESOLVE_MAX_HOPS, AUDIO_EXT_DIRECT, AUDIO_OUT_EXTS,
    AUDIO_RE_M3U8, AUDIO_RE_MPD, AUDIO_RE_DIRECT,
    AUDIO_PARTIAL_DIR, AUDIO_PARTIAL_MAX_AGE,
    AUDIO_MIN_SEG_BYTES, AUDIO_MIN_MEDIA_BYTES,
    AUDIO_TAG_ENABLED, AUDIO_COVER_MAX_BYTES,
    TIME_FORMAT, LOG_AUDIO_ENC_NEED, AUDIO_DASH_CLEARKEY,
)

# ================================================================
# 数据模型
# ================================================================


@dataclass
class AudioCandidate:
    """探测/下载一条音频的候选记录。"""
    index: int
    kind: str               # "m3u8" | "dash" | "direct"
    url: str                # 绝对 URL（已 urljoin）
    ext: str                # mp3/m4a/aac/wav/flac/ogg/…，无法判定为 ""
    src: str                # 发现方式：audio/source / JS字面量 / 播放器配置 / 解码 / meta
    page_url: str           # 候选实际所在页（下载 Referer 用）
    title_hint: str = ""
    cookies: dict = field(default_factory=dict)


# ================================================================
# 音频专属分类（绝不动 vg._classify：那是视频扩展白名单）
# ================================================================


def _classify_audio(media_url: str):
    """返回 (kind, ext) 或 (None, None)。只收音频扩展 + m3u8/mpd 清单。"""
    path = media_url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return "m3u8", "m3u8"
    if path.endswith(".mpd"):
        return "dash", "mpd"
    if "." in path:
        ext = path.rsplit(".", 1)[1]
        if ext in AUDIO_EXT_DIRECT:
            return "direct", ext
    return None, None


def _url_extless(url: str) -> bool:
    """URL 路径末段无扩展名（/play?id=9、/stream）→ True。有 .mp3/.jpg 等任何后缀 → False。"""
    path = urlparse(url).path.rstrip("/")
    return not path or "." not in os.path.basename(path)


# #8a Content-Type 嗅探：无扩展名候选（og:audio 无后缀直链、/play?id=9）现被 _classify_audio
# 只认扩展名拒收。这里对"强音频信号但缺扩展名"的候选做一次流式 GET 头探测（读 Content-Type
# 即关、不拉 body），命中 audio/* 才按 direct 收下。任何失败/非 audio → '' 静默跳过，
# 默认零行为变化。键=URL 规范键，进程级小缓存防同页多信号源重复探测。
_AUDIO_CT_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-mp3": "mp3",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/aac": "aac", "audio/x-aac": "aac",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/ogg": "ogg", "audio/opus": "ogg",
    "audio/flac": "flac", "audio/x-flac": "flac",
}
_CT_PROBE_CACHE: dict = {}
_CT_PROBE_LOCK = threading.Lock()


def _content_type_ext(ctype: str) -> str:
    """Content-Type（可带参数）→ 音频扩展名；非 audio/* 白名单返回 ''。"""
    return _AUDIO_CT_EXT.get((ctype or "").split(";")[0].strip().lower(), "")


def _probe_direct_audio(url: str, referer: str = "", cookies: dict = None) -> str:
    """无扩展名候选的 Content-Type 嗅探。复用 vg 通用取流（_http_get，同一把双闸/代理/指纹）。
    流式 GET 只看响应头就关，不下载 body。返回音频扩展名（如 'mp3'）或 ''。"""
    if (urlparse(url).scheme or "").lower() not in ("http", "https"):
        return ""
    ck = vg._canon_key(url)
    with _CT_PROBE_LOCK:
        if ck in _CT_PROBE_CACHE:
            return _CT_PROBE_CACHE[ck]
    ext = ""
    try:
        r, _err = vg._http_get(url,
                               headers=vg._media_headers(url, referer, purpose="direct"),
                               cookies=cookies, timeout=AUDIO_TIMEOUT, stream=True)
        if r is not None:
            try:
                if r.status_code == 200:
                    ext = _content_type_ext(r.headers.get("Content-Type") or "")
            finally:
                try:
                    r.close()
                except Exception:
                    pass
    except Exception:
        ext = ""
    if ext:
        with _CT_PROBE_LOCK:
            _CT_PROBE_CACHE.setdefault(ck, ext)
    return ext


# ================================================================
# 命名 / 去重 / 已存在（音频扩展白名单，兼容 remux 后的 m4a/mp3/ogg/flac）
# ================================================================

_EXT_STRIP_URL = re.compile(r"\.(?:m3u8|mpd|mp3|m4a|aac|wav|flac|ogg|ts)$", re.I)


def _stub_from_url(url: str) -> str:
    """取媒体 URL 的文件名主体（去扩展名），用于直链/无标题时的命名桩。"""
    path = urlparse(url).path
    stub = os.path.basename(path)
    return _EXT_STRIP_URL.sub("", stub)


def _stem_from_title(title: str, url: str) -> str:
    """单文件/单页命名桩：优先页面标题（_fs_clean 保留 CJK），空/纯符号回退 URL 桩，再默认 audio。"""
    base = vg._fs_clean(title or "", 80)
    if not base or set(base) <= {"-", "_", "."}:
        base = vg._fs_clean(_stub_from_url(url), 80)
    if not base or set(base) <= {"-", "_", "."}:
        base = "audio"
    return base[:80]


def _existing_audio(folder: str, stem: str):
    """同 stem 任一音频扩展已有成品 → 返回路径（断点续抓/跳过）。"""
    for ext in AUDIO_OUT_EXTS:
        p = os.path.join(folder, f"{stem}.{ext}")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        except OSError:
            continue
    return None


def _unique_audio_stem(folder: str, base: str) -> str:
    """返回与现存音频均不冲突的 stem（base / base-2 / base-3 …）。
    因 remux 的目标扩展名要下载结束才知道，这里以“任意音频扩展都不冲突”为准。"""
    stem = base
    n = 1
    while _existing_audio(folder, stem) is not None:
        n += 1
        stem = f"{base}-{n}"
    return stem


# ================================================================
# 页面静态扫描：音频来源（只收能归类为音频的 URL）
# ================================================================

_SRC_AUDIO_HINT = re.compile(r"\.(?:mp3|m4a|aac|wav|flac|ogg|m3u8|mpd)(?:[?#]|$)", re.I)

# 播放器/音轨配置字段值（键名容忍 JSON 引号；只收含音频扩展/清单的值过滤噪音）
_RE_AUDIO_FIELD = re.compile(
    r"""(?<![A-Za-z0-9_."'])["']?(?:url|file|src|link|audio|music|song|v_url|playurl|play_url|mac_url)["']?\s*[:=]\s*["']([^"']{4,})["']""",
    re.I)
_RE_AUDIO_EXT_IN_VAL = re.compile(r"\.(?:m3u8|mpd|mp3|m4a|aac|wav|flac|ogg)(?=[?#\"'\s\\]|$)", re.I)


def _audio_decode_urls(html: str, page_url: str) -> list:
    """扫描播放器字段/解码函数负载 → 剥离可逆包裹 → 只收音频明文 URL。"""
    found, seen = [], set()
    payloads = []
    for m in _RE_AUDIO_FIELD.finditer(html):
        payloads.append(m.group(1))
    for m in vg._RE_DECODE_CALL.finditer(html):
        payloads.append(m.group(1))
    for p in payloads:
        for cand in vg._bounded_decode_payload(p):
            norm = vg._normalize(cand, page_url)
            if not norm:
                continue
            key = vg._canon_key(norm)
            if key in seen:
                continue
            seen.add(key)
            kind, ext = _classify_audio(norm)
            if kind is not None:
                found.append((norm, kind, ext))
    return found


def extract_audio_candidates_from_html(html: str, page_url: str):
    """静态扫描页面内音频来源。返回 AudioCandidate 列表（URL 去重）。永不 raise。"""
    cands: list = []
    seen = set()

    def add(raw, src, signal=False):
        """signal=True：来源强声明是音频（<audio>/type=audio source/og:audio/AudioObject
        JSON-LD/meta contentUrl）——即使缺扩展名也保留，交给 Content-Type 嗅探（#8a）；
        嗅到 audio/* 才按 direct 收。非信号源仍只认扩展名（图片/css/js 等静默）。"""
        if not raw:
            return
        norm = vg._normalize(raw, page_url)          # 内部已滤 blob:/data:/javascript:
        if norm is None:
            return
        key = vg._canon_key(norm)
        kind, ext = _classify_audio(norm)
        if kind is None and signal and key not in seen and _url_extless(norm):
            ext = _probe_direct_audio(norm, referer=page_url)     # 无扩展名强信号 → CT 嗅探
            if ext:
                kind = "direct"
        if kind is None:                             # 非音频（图片/css/js/视频容器等）静默
            return
        if key in seen:
            return
        seen.add(key)
        cands.append(AudioCandidate(index=len(cands) + 1, kind=kind, url=norm,
                                    ext=ext, src=src, page_url=page_url,
                                    title_hint=ext if ext not in ("m3u8",) else ""))

    if not html:
        return cands

    # 1) <audio> / <source> 标签的 src/data-src/data-url（type=audio/* 或 src 形态由分类把关）
    for m in re.finditer(r"<(audio|source)\b[^>]*>", html, re.I):
        tag = m.group(0)
        low = tag.lower()
        is_audio_el = low.startswith("<audio")
        src_typed_audio = bool(re.search(r"""\btype\s*=\s*["']audio/[^"']*["']""", tag, re.I))
        if low.startswith("<source") and not src_typed_audio:
            # 独立 <source>（非 audio type）多半是图片/视频源 → 只收 src 本身带音频形态者
            sm = re.search(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, re.I)
            srcv = next((g for g in sm.groups() if g), "") if sm else ""
            if srcv and _SRC_AUDIO_HINT.search(srcv):
                add(srcv, "audio/source标签")
            continue
        signal = bool(is_audio_el or src_typed_audio)   # 强信号：缺扩展名也走 CT 嗅探（#8a）
        for attr in ("src", "data-src", "data-url"):
            am = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % attr, tag, re.I)
            if am:
                val = next((g for g in am.groups() if g), "")
                if val.strip():
                    add(val, "audio/source标签", signal=signal)
        # <audio> 开标签内的子 <source> 也扫一遍（其 src 必然音频）
        if is_audio_el:
            for sm in re.finditer(r"<source\b[^>]*>", tag, re.I):
                ssm = re.search(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", sm.group(0), re.I)
                if ssm:
                    val = next((g for g in ssm.groups() if g), "")
                    if val.strip():
                        add(val, "audio/source标签", signal=True)

    # 2) JS 字符串字面量里的 m3u8 / DASH(.mpd) / 音频直链（兼容 \\/ 转义斜杠）
    for m in re.finditer(AUDIO_RE_M3U8, html):
        add(m.group(1), "JS字面量-m3u8")
    for m in re.finditer(AUDIO_RE_MPD, html):
        add(m.group(1), "JS字面量-mpd")
    for m in re.finditer(AUDIO_RE_DIRECT, html):
        add(m.group(1), "JS字面量-直链")

    # 3) 播放器配置字段（只收值含音频形态者，避免图片噪音）
    for m in _RE_AUDIO_FIELD.finditer(html):
        val = m.group(1)
        if _RE_AUDIO_EXT_IN_VAL.search(val):
            add(val, "播放器配置")

    # 4) 纯文本裸露 URL（未带引号包裹）
    for m in re.finditer(r"(https?://[^\s\"'<>]+?\.m3u8[^\s\"'<>]*)", html, re.I):
        add(m.group(1), "裸URL-m3u8")
    for m in re.finditer(r"(https?://[^\s\"'<>]+?\.mpd[^\s\"'<>]*)", html, re.I):
        add(m.group(1), "裸URL-mpd")
    for m in re.finditer(r"(https?://[^\s\"'<>]+?\.(?:mp3|m4a|aac|wav|flac|ogg)[^\s\"'<>]*)", html, re.I):
        add(m.group(1), "裸URL-直链")

    # 5) 播放器字段/解码函数可逆包裹解码
    for dec_url, _kind, _ext in _audio_decode_urls(html, page_url):
        add(dec_url, "播放器解码")

    # 6) og:audio / twitter:audio meta
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        if not re.search(r'(?:property|name)\s*=\s*["\'](?:og:audio(?::url|:secure_url)?|twitter:audio)["\']', m.group(0), re.I):
            continue
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if cm and cm.group(1).strip():
            add(vg._safe_unescape(cm.group(1)).strip(), "og:audio meta", signal=True)

    # 7) JSON-LD AudioObject 等（contentUrl/streamUrl/url/embedUrl/playUrl…）
    _LD_KEYS = {"contenturl", "contentstreamurl", "streamurl", "embedurl", "playurl",
                "url", "file", "audio"}
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
                _types = node.get("@type") or ""
                if isinstance(_types, list):
                    _types = " ".join(str(t) for t in _types)
                audio_typed = str(_types).strip().lower() in {"audioobject", "musicrecording"}
                for k, v in node.items():
                    if isinstance(v, str) and v.strip():
                        if k.strip().lower() in _LD_KEYS or _SRC_AUDIO_HINT.search(v.strip()):
                            # 缺扩展名直链只在 AudioObject/MusicRecording 节点下才嗅探（#8a）
                            add(vg._safe_unescape(v).strip(), "JSON-LD",
                                signal=audio_typed)
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                stack.extend(node)

    # 8) <meta itemprop=contentUrl> / <link itemprop=embedURL>
    #    （schema 语义即音频来源；缺扩展名直链交给 CT 嗅探 #8a，图片等带后缀者不进嗅探）
    for m in re.finditer(r'<meta\b[^>]*\bitemprop\s*=\s*["\']contentUrl["\'][^>]*>', html, re.I):
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if cm and cm.group(1).strip():
            add(vg._safe_unescape(cm.group(1)).strip(), "meta contentUrl", signal=True)
    for m in re.finditer(r'<link\b[^>]*\bitemprop\s*=\s*["\']embedURL["\'][^>]*>', html, re.I):
        hm = re.search(r'href\s*=\s*["\']([^"\']*)["\']', m.group(0), re.I)
        if hm and hm.group(1).strip():
            add(vg._safe_unescape(hm.group(1)).strip(), "link embedURL", signal=True)

    return cands


# ================================================================
# 页面深挖解析（音频版）：本页无候选时静态跟随 iframe/refresh，有界去重
# ================================================================


def _resolve_audio_from_page(url: str, *, html0=None, final0=None,
                             log=None, stop_flag=None) -> list:
    """任意 URL → 音频候选（AudioCandidate 列表）。永不 raise。
    复用 vg._hop_target 的跳转目标判定，但落点只做音频扫描。"""
    max_hops = max(0, int(AUDIO_RESOLVE_MAX_HOPS))
    visited = set()
    by_key = {}

    def _note(m):
        if log:
            try:
                log(str(m))
            except Exception:
                pass

    def walk(u, html, final, depth):
        if stop_flag and stop_flag():
            return
        k = vg._canon_key(u)
        if k in visited:
            return
        visited.add(k)
        if not html:
            return
        for c in extract_audio_candidates_from_html(html, final or u):
            by_key.setdefault(vg._canon_key(c.url), c)
        if by_key or depth >= max_hops:
            return
        tgt = vg._hop_target(html, final or u)
        if not tgt:
            return
        if vg._canon_key(tgt) in visited:
            return
        h2, f2 = vg.fetch_html(tgt, timeout=AUDIO_TIMEOUT,
                               max_bytes=AUDIO_MAX_PAGE_BYTES, referer=final or u)
        if h2:
            walk(tgt, h2, f2 or tgt, depth + 1)

    if html0:
        walk(url, html0, final0 or url, 0)
    else:
        h, f = vg.fetch_html(url, timeout=AUDIO_TIMEOUT, max_bytes=AUDIO_MAX_PAGE_BYTES)
        walk(url, h, f or url, 0)
    cands = list(by_key.values())
    for i, c in enumerate(cands, start=1):
        c.index = i
    return cands


# ================================================================
# 音频 m3u8：跟随到“只含音频”的最终媒体清单
# ================================================================


def _m3u8_attr(line: str, name: str, default: str = "") -> str:
    """取 EXT-X-* 属性行的属性值（带引号/裸 token 均可）。"""
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), line, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b%s\s*=\s*([A-Za-z0-9_\-\.\/\:]+)" % re.escape(name), line, re.I)
    return m.group(1) if m else default


def _follow_audio_media(m3u8_url: str, referer: str = "", cookies: dict = None):
    """抓取 m3u8 并跟随到最终媒体清单（音频视角，深度 ≤4）：
    ① 有 #EXT-X-MEDIA TYPE=AUDIO 组 → 优先 DEFAULT=YES 的音频播放列表；
    ② 仍是 master(STREAM-INF) → 优先“无 RESOLUTION”（纯音频变体启发）最高带宽，否则整体最高带宽；
    ③ 到达 media → 返回 (pl, media_url)。全程失败 raise RuntimeError（中文文案）。"""
    url = m3u8_url
    for _ in range(4):
        txt = vg._robust_binary(url, referer, cookies=cookies, min_len=1)\
            .decode("utf-8", "ignore")
        # ① EXT-X-MEDIA 音频组（独立纯音频播放列表，最可靠）
        media_lines = [ln for ln in txt.splitlines()
                       if ln.startswith("#EXT-X-MEDIA")
                       and re.search(r"TYPE\s*=\s*AUDIO", ln, re.I)]
        if media_lines:
            chosen = chosen_def = None
            for ln in media_lines:
                uri = _m3u8_attr(ln, "URI")
                if not uri:
                    continue                       # 无独立 URL（音频内联在视频变体里）→ 忽略
                u = vg._normalize(uri, url)
                if not u:
                    continue
                if _m3u8_attr(ln, "DEFAULT").upper() == "YES":
                    if chosen_def is None:
                        chosen_def = u
                elif chosen is None:
                    chosen = u
            tgt = chosen_def or chosen
            if tgt:
                url = tgt
                continue
        pl = vg.parse_hls(txt, url)
        if pl.get("kind") == "master":
            variants = pl.get("variants") or []
            if not variants:
                raise RuntimeError("master 清单无可用清晰度变体")
            pure = [v for v in variants if not (v.get("resolution") or 0)]  # 无 RESOLUTION → 音频变体启发
            pool = pure or variants
            best = max(pool, key=lambda v: (v.get("bandwidth") or 0))
            nxt = vg._normalize(best["uri"], url) or best["uri"]
            if vg._canon_key(nxt) == vg._canon_key(url):
                raise RuntimeError("master 变体指向自身（循环）")
            url = nxt
            continue
        return pl, url
    raise RuntimeError("跟随音频清单超过上限（4 层），可能是异常播放列表")


# ================================================================
# ffmpeg remux → 干净音频单文件
# ================================================================

# ── 假直链防护 / 容器魔数探测（包A）─────────────────────────────
_ERR_FAKE_MEDIA = "FAKE_MEDIA"        # 200 但非媒体（风控假成功）——语义区别于 403 的 ERR_HOTLINK
_AUDIO_SCAN_BYTES = 256 * 1024        # mp3 帧同步扫描窗口


def _find_mpeg_sync(data: bytes) -> bool:
    """在字节窗内找 MPEG 音频帧同步：0xFF + (b1&0xE0)==0xE0（11 位帧同步），
    且 bitrate 索引 1..14、采样率索引 !=3（排除 header 假阳性）。"""
    n = len(data) - 2
    i = 0
    while i < n:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            b2 = data[i + 2] if i + 2 < len(data) else 0
            bi = (b2 >> 4) & 0x0F
            si = (b2 >> 2) & 0x03
            if 0 < bi < 15 and si != 3:
                return True
        i += 1
    return False


def _probe_audio_container(path: str, ext: str):
    """落盘文件“容器魔数 + 最小长度”校验（包A 假直链兜底）。
    返回 (ok, reason)；永不 raise；未知扩展视为通过（不误伤）。
    mp3=ID3 或 MPEG 帧同步；m4a=ftyp；aac=ADTS；wav=RIFF/WAVE；flac=fLaC；ogg=OggS。"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return False, "空文件(0B)"
        with open(path, "rb") as f:
            head = f.read(1024)
    except OSError as e:
        return False, f"无法读取文件：{e}"
    e = (ext or "").lower().lstrip(".")
    reason = "未知容器"
    if e == "mp3":
        if head[:3] == b"ID3":
            return True, ""
        try:
            with open(path, "rb") as f:
                window = f.read(_AUDIO_SCAN_BYTES)
        except OSError:
            window = head
        if _find_mpeg_sync(window):
            return True, ""
        reason = "头非 ID3 且未见 MPEG 帧同步"
    elif e == "m4a":
        if head[4:8] == b"ftyp":
            return True, ""
        reason = "头缺 ftyp 容器魔数"
    elif e == "aac":
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
            return True, ""
        reason = "头非 ADTS 同步字"
    elif e == "wav":
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return True, ""
        reason = "头非 RIFF/WAVE"
    elif e == "flac":
        if head[:4] == b"fLaC":
            return True, ""
        reason = "头非 fLaC 魔数"
    elif e == "ogg":
        if head[:4] == b"OggS":
            return True, ""
        reason = "头非 OggS 魔数"
    else:
        return True, ""
    if size < AUDIO_MIN_MEDIA_BYTES:
        return False, f"{reason}（仅 {size}B < {AUDIO_MIN_MEDIA_BYTES} 下限）"
    return False, f"{reason}；头 {head[:16].hex()}"


# 逐容器尝试（首个成功即收）：m4a(AAC/most) → mp3(纯 MP3) → ogg(opus/vorbis) → flac
_AUDIO_MUX_CANDIDATES = [
    (".m4a", ["-c", "copy", "-movflags", "+faststart"]),
    (".mp3", ["-c", "copy"]),
    (".ogg", ["-c", "copy"]),
    (".flac", ["-c", "copy"]),
]
# copy 全败后的逐容器重编码兜底：编码器/容器合法绑定写在本表内（.mp3 永不配 aac）。
_AUDIO_ENCODE_FALLBACK = [
    (".m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]),
    (".mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    (".ogg", ["-c:a", "libopus", "-b:a", "128k"]),
    (".flac", ["-c:a", "flac"]),
]


def _mux_audio_to(in_args: list, out_base: str, *, log=None, stop_flag=None):
    """remux 到 out_base + <容器扩展>（包A 两阶段）。
    阶段① 逐容器纯 -c copy；阶段② 全败后按 _AUDIO_ENCODE_FALLBACK 逐容器重编码
    （copy 对源/容器不适配，如 MP3-in-TS remux 成 m4a）。in_args 如 ["-i", pl_path]。
    只取音频轨（-map 0:a:0）。返回 (最终路径, 字节数)；全失败 raise RuntimeError。"""
    if not vg._locate_ffmpeg():
        raise RuntimeError("未找到 ffmpeg，无法 remux 音频")
    base = ["-hide_banner", "-y", "-protocol_whitelist", "file,crypto,http,https,tcp,tls"]

    def _try(dest, extra):
        vg._remove_if(dest)
        args = base + list(in_args) + ["-map", "0:a:0"] + list(extra) + [dest]
        rc, tail = vg._run_ffmpeg(args, log=log, stop_flag=stop_flag)
        if rc == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        vg._remove_if(dest)
        if stop_flag and stop_flag():
            raise RuntimeError("已停止")
        return ""

    for ext, mux in _AUDIO_MUX_CANDIDATES:              # 阶段① copy（无损）
        dest = _try(out_base + ext, mux)
        if dest:
            return dest, os.path.getsize(dest)
    for ext, enc in _AUDIO_ENCODE_FALLBACK:             # 阶段② 重编码兜底
        if log:
            try:
                log(f"   ffmpeg copy 全部失败，降级重编码({ext.lstrip('.')} / {enc[1]})")
            except Exception:
                pass
        dest = _try(out_base + ext, enc)
        if dest:
            return dest, os.path.getsize(dest)
    raise RuntimeError("ffmpeg remux 失败：copy 与逐容器重编码兜底均未产出成品")


# ================================================================
# 分段影子续传（包B）：HLS/DASH 分片按文件粒度落 _audio_partials/<key>
# ================================================================

def _audio_shadow_dir(out_base: str, media_url: str) -> str:
    """影子目录：key = sha1(out_base 绝对路径)[:16]_md5(media_url)[:8]。
    与成品同卷（AUDIO_PARTIAL_DIR 与 AUDIO_DIR 平级）保证 os.replace 原子；
    out_base 稳定 → 同一 URL 中断后重跑同 key 命中续传。"""
    try:
        os.makedirs(AUDIO_PARTIAL_DIR, exist_ok=True)
    except OSError:
        pass
    key = (hashlib.sha1(os.path.abspath(out_base).encode("utf-8", "ignore")).hexdigest()[:16]
           + "_" + hashlib.md5(media_url.encode("utf-8", "ignore")).hexdigest()[:8])
    shadow = os.path.join(AUDIO_PARTIAL_DIR, key)
    try:
        os.makedirs(shadow, exist_ok=True)   # init/key 在进 _pull_segments_resumable 前就落盘，目录须先建
    except OSError:
        pass
    return shadow


def _audio_seg_rel(i: int) -> str:
    return f"seg_{i:05d}.mp4"


def _read_audio_index(idx_path: str):
    """读 .idx；损坏/异常返回 None（调用方按全新处理）。"""
    try:
        if os.path.exists(idx_path):
            with open(idx_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_audio_index(idx_path: str, payload: dict) -> None:
    """写 .idx（内容极小；硬杀丢几片只多下几片，不破坏正确性）。宽异常吞。"""
    try:
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _audio_manifest_fp(kind: str, media_url: str, uris: list,
                       init_uri: str = "", key_uri: str = "") -> str:
    """清单指纹：kind + 分片总数 + 首/尾分片 URI + init/key URI 的 sha1[:16]。
    换源/换清晰度/换加密 → 指纹变 → 弃旧影子整下。VOD 静态清单同 URL 重跑稳定。"""
    try:
        h = hashlib.sha1()
        h.update(str(kind).encode("utf-8", "ignore"))
        h.update(b"|")
        h.update(str(media_url).encode("utf-8", "ignore"))
        h.update(b"|")
        h.update(str(len(uris)).encode("utf-8", "ignore"))
        if uris:
            h.update(b"|")
            h.update(str(uris[0]).encode("utf-8", "ignore"))
            h.update(b"|")
            h.update(str(uris[-1]).encode("utf-8", "ignore"))
        if init_uri:
            h.update(b"|i|")
            h.update(str(init_uri).encode("utf-8", "ignore"))
        if key_uri:
            h.update(b"|k|")
            h.update(str(key_uri).encode("utf-8", "ignore"))
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _audio_ensure_file(path: str, uri: str, referer: str, cookies: dict = None,
                       min_len: int = 1, keep: int = 0) -> str:
    """shadow 内单文件（init/key）已存在且 ≥min_len → 跳过；否则取回原样写盘
    （keep>0 截断，如 AES 密钥取前 16 字节）。.part 写盘 + 原子替换。失败 raise。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) >= min_len:
            return path
    except OSError:
        pass
    data = vg._robust_binary(uri, referer, cookies=cookies, min_len=min_len)
    if keep and len(data) > keep:
        data = data[:keep]
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def _audio_fetch_seg(url: str, dest: str, referer: str, cookies: dict = None) -> int:
    """单分片整取（_robust_binary 全量内存 → .part → 原子替换）。成功返字节数，失败 raise。
    绝不续写单分片，杜绝半片；中断只可能留下 .part（下次扫描清理）。"""
    data = vg._robust_binary(url, referer, cookies=cookies, min_len=AUDIO_MIN_SEG_BYTES)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return len(data)


def _pull_segments_resumable(*, shadow: str, media_url: str, kind: str, out_base: str,
                             uris: list, n_total: int, init_uri: str = "", key_uri: str = "",
                             referer: str = "", concurrency: int = AUDIO_CONCURRENT,
                             progress_callback=None, stop_flag=None,
                             keep: tuple = (), cookies: dict = None, log=None) -> list:
    """音频分段影子续传核心。uris 长度==n_total；返回按序 seg 绝对路径列表。
    已完整落盘且尺寸一致的分片跳过；缺失/尺寸不符重取。停止/失败 → 写 .idx 后 raise。
    keep：指纹变化弃旧整下时保留的 basename 元组（调用方刚取回的 init/key，
    必属本次清单，不能被首跑/换源时的整清误删——旧实现会在首次运行把 init.mp4 一起清掉）。"""
    os.makedirs(shadow, exist_ok=True)
    # 清遗留 .part（中断残留，一律忽略并删除）
    for name in os.listdir(shadow):
        if name.endswith(".part"):
            try:
                os.remove(os.path.join(shadow, name))
            except OSError:
                pass
    idx_path = os.path.join(shadow, ".idx")
    fp = _audio_manifest_fp(kind, media_url, uris, init_uri, key_uri)
    idx = _read_audio_index(idx_path)
    idxok = (isinstance(idx, dict) and idx.get("schema") == 1
             and idx.get("fp") == fp and idx.get("total") == n_total
             and idx.get("kind") == kind and idx.get("out_base") == out_base)
    if not idxok:
        # 指纹/总数/落点变化 → 弃旧整下（本次刚取的 init/key 在 keep 里，保留；其余全清）
        for name in os.listdir(shadow):
            if name in keep:
                continue
            p = os.path.join(shadow, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass
        idx = None
    # 重建 done 集：盘上有文件且尺寸>0；若 .idx 记录了该片尺寸，须精确相等才续。
    seg_len = {}
    if idxok:
        for s in idx.get("segs") or []:
            try:
                seg_len[int(s["i"])] = int(s["len"])
            except Exception:
                pass
    done = {}
    for i in range(n_total):
        p = os.path.join(shadow, _audio_seg_rel(i))
        try:
            if os.path.exists(p):
                sz = os.path.getsize(p)
                if sz > 0 and (i not in seg_len or seg_len[i] == sz):
                    done[i] = sz
        except OSError:
            pass
    todo = [i for i in range(n_total) if i not in done]
    resumed = n_total - len(todo)
    if log and resumed > 0:
        try:
            log(f"   续传命中 {resumed}/{n_total} 片已落盘，需补 {len(todo)} 片")
        except Exception:
            pass

    def _persist():
        _write_audio_index(idx_path, {
            "schema": 1, "url": media_url, "kind": kind, "out_base": out_base,
            "fp": fp, "total": n_total,
            "segs": [{"i": i, "len": sz} for i, sz in sorted(done.items())],
            "updated_at": time.strftime(TIME_FORMAT),
        })

    _persist()
    if not todo:
        return [os.path.join(shadow, _audio_seg_rel(i)) for i in range(n_total)]

    inflight = max(1, int(concurrency or AUDIO_CONCURRENT))
    with ThreadPoolExecutor(max_workers=inflight) as pool:
        pending, next_i, done_pump = {}, 0, 0

        def _progress():
            if progress_callback:
                try:
                    progress_callback(len(done), n_total, "音频分片")
                except Exception:
                    pass

        while done_pump < len(todo):
            if stop_flag and stop_flag():
                _persist()
                raise RuntimeError("已停止")
            while len(pending) < inflight and next_i < len(todo):
                i = todo[next_i]
                try:
                    fut = pool.submit(_audio_fetch_seg, uris[i],
                                      os.path.join(shadow, _audio_seg_rel(i)),
                                      referer, cookies)
                except Exception as e:
                    _persist()
                    raise RuntimeError(str(e))
                pending[fut] = i
                next_i += 1
            if not pending:
                break
            dset, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in dset:
                if stop_flag and stop_flag():
                    _persist()
                    raise RuntimeError("已停止")
                i = pending.pop(fut)
                try:
                    sz = fut.result()
                except Exception as e:
                    _persist()
                    raise RuntimeError(f"分片 {i} 下载失败：{e}")
                done[i] = sz
                done_pump += 1
                if done_pump % max(4, inflight) == 0:
                    _persist()
                _progress()
        _persist()
    return [os.path.join(shadow, _audio_seg_rel(i)) for i in range(n_total)]


def cleanup_audio_partials(max_age: float = AUDIO_PARTIAL_MAX_AGE) -> int:
    """清扫 _audio_partials 内超过 max_age 秒未改动的影子目录（防膨胀）。返回清理数。"""
    n = 0
    try:
        now = time.time()
        for name in os.listdir(AUDIO_PARTIAL_DIR):
            p = os.path.join(AUDIO_PARTIAL_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


# ================================================================
# 音频 HLS / DASH / 直链 下载器（返回 status dict，永不 raise）
# ================================================================


def _stopped(status: str = "stopped"):
    return {"status": status, "bytes": 0, "path": "", "error": "", "error_code": ""}


def _fail(error: str, error_code: str):
    return {"status": "fail", "bytes": 0, "path": "", "error": error, "error_code": error_code}


def _audio_close_playlist(pl_path: str) -> None:
    """本地清单补 #EXT-X-ENDLIST（VOD 收尾）。
    vg._write_local_hls 不写 ENDLIST（video 侧冻结不动）——缺它 ffmpeg 会把本地清单当
    直播轮询、读完分片仍从 #EXTM3U 重读、永不退出（实测 AES-128 分段即此卡死）。
    音频侧统一补上，HLS 与 DASH 复用。"""
    try:
        with open(pl_path, "a", encoding="utf-8") as f:
            f.write("#EXT-X-ENDLIST\n")
    except OSError:
        pass


def _download_audio_hls(m3u8_url: str, out_base: str, *, referer: str = "",
                        concurrency: int = AUDIO_CONCURRENT,
                        progress_callback=None, stop_flag=None,
                        cookies: dict = None, log=None,
                        meta=None, cover_url: str = "") -> dict:
    """音频 m3u8 → 干净单文件音频。out_base 不含扩展名（最终扩展名由 remux 定）。
    单文件流整份下载；分段流(TS/CMAF/fMP4/AES-128)原文抓取 → 本地清单 → ffmpeg 解密/remux。
    分段走影子目录跨次续传（包B）；remux 成功后 best-effort 打标（包D，仅 remux 路径）。"""
    if not vg._locate_ffmpeg():
        return _fail("音频 HLS 需随包 ffmpeg remux，请确认 runtime/ffmpeg 就位",
                     vg.ERR_MISSING_FFMPEG)
    cookies = vg._dl_cookies(m3u8_url, cookies)   # 无显式登录态时回退同域 jar
    try:
        pl, media_url = _follow_audio_media(m3u8_url, referer, cookies)
    except RuntimeError as e:
        return _fail(f"无法解析音频 m3u8：{e}", vg.ERR_HLS_MANIFEST)
    except Exception as e:
        return _fail(f"无法获取音频 m3u8 清单：{e}", vg.ERR_HLS_MANIFEST)

    enc = pl.get("encryption") or {}
    method = (enc.get("method") or "NONE").upper()
    mapp = pl.get("map") or {}
    single = bool(pl.get("single_file"))
    segs = pl.get("segments") or []
    if method not in ("NONE", ""):
        if method != "AES-128":
            return {"status": "unsupported", "bytes": 0, "path": "",
                    "error": LOG_AUDIO_ENC_NEED, "error_code": vg.ERR_ENCRYPT_UNSUP}
        if not enc.get("uri"):
            return _fail("AES 密钥 URI 缺失", vg.ERR_AES_KEY)

    # A) 单文件流（EXT-X-BYTERANGE 切分同一大文件）：整份下载 → remux 取音频轨
    if single:
        if method != "NONE":
            return {"status": "unsupported", "bytes": 0, "path": "",
                    "error": LOG_AUDIO_ENC_NEED, "error_code": vg.ERR_ENCRYPT_UNSUP}
        src_uri = mapp.get("uri") or (segs[0]["uri"] if segs else "") or media_url
        if not src_uri:
            return _fail("单文件音频 m3u8 缺文件 URI", vg.ERR_HLS_MANIFEST)
        src = out_base + ".src"
        vg._remove_if(src)
        res = vg.download_direct(src_uri, src, referer=referer, timeout=AUDIO_TIMEOUT,
                                 stop_flag=stop_flag, cookies=cookies)
        if res.get("status") != "ok":
            vg._remove_if(src)
            return {"status": res.get("status", "fail"), "bytes": 0, "path": "",
                    "error": f"单文件音频整份下载失败：{res.get('error') or '网络异常'}",
                    "error_code": res.get("error_code") or vg.ERR_NETWORK}
        try:
            path, nbytes = _mux_audio_to(["-i", src], out_base, log=log,
                                         stop_flag=stop_flag)
            vg._remove_if(src)
            _maybe_tag_file(path, meta, cover_url, referer, cookies, log=log)
            return {"status": "ok", "bytes": nbytes, "path": path, "error": "",
                    "error_code": ""}
        except RuntimeError as e:
            vg._remove_if(src)
            if stop_flag and stop_flag():
                return _stopped()
            return _fail(f"单文件音频 remux 失败：{e}", vg.ERR_FFMPEG)

    # B) 分段音频（TS/CMAF/fMP4 通用）：影子续传 → 本地清单 → ffmpeg
    if not segs:
        return _fail("音频清单内无分片", vg.ERR_PARSER)
    if len(segs) > AUDIO_MAX_SEGMENTS:
        return _fail(f"音频分片数量过多({len(segs)})，超过上限 {AUDIO_MAX_SEGMENTS}，已拒绝",
                     vg.ERR_SIZE_OVER)

    shadow = _audio_shadow_dir(out_base, m3u8_url)
    init_path = None
    if pl.get("has_map"):
        init_uri = mapp.get("uri") or ""
        if not init_uri:
            return _fail("EXT-X-MAP 缺 init URI（无法 remux fMP4 音频）", vg.ERR_HLS_MANIFEST)
        if mapp.get("byterange"):
            return _fail("BYTERANGE 型 init 属单文件流，请走整份下载分支", vg.ERR_HLS_MANIFEST)
        init_path = os.path.join(shadow, "init.mp4")
        try:
            _audio_ensure_file(init_path, init_uri, referer, cookies, min_len=1)
        except Exception as e:
            return _fail(f"音频 init 片段获取失败：{e}", vg.ERR_HLS_SEGMENT)
    key_path = None
    if method == "AES-128":
        # 命名 key.mp4（非 key.bin）：ffmpeg 7 对 file: 读本地密钥做扩展名白名单检查，
        # .bin 会被“not a common multimedia extension, blocked for security”拒绝致解密失败；
        # .mp4 放行（video 侧冻结实现 vg 同用 key.mp4，此处对齐）。文件内容仍是 16B 原始密钥。
        key_path = os.path.join(shadow, "key.mp4")
        try:
            # ffmpeg 只认 raw16 字节 key 文件；站点常以 hex 文本/JSON API 提供，
            # 先整段取回 → engine 在时 coerce 归一（raw/hex/json/base64）→ 截前 16B。
            if not (os.path.exists(key_path) and os.path.getsize(key_path) >= 16):
                key_data = vg._robust_binary(enc["uri"], referer, cookies=cookies,
                                             min_len=1)
                try:
                    if getattr(vg, "_SD", None) is not None:
                        key_data = vg._SD.coerce_key(key_data)
                except Exception:
                    pass
                if len(key_data) < 16:
                    return _fail("AES 密钥长度非法", vg.ERR_AES_KEY)
                _tmp = key_path + ".part"
                with open(_tmp, "wb") as f:
                    f.write(key_data[:16])
                os.replace(_tmp, key_path)
        except Exception:
            return _fail("AES 密钥获取失败", vg.ERR_AES_KEY)

    try:
        seg_paths = _pull_segments_resumable(
            shadow=shadow, media_url=m3u8_url, kind="hls", out_base=out_base,
            uris=[(s.get("uri") or "") for s in segs], n_total=len(segs),
            init_uri=(mapp.get("uri") or "") if pl.get("has_map") else "",
            key_uri=(enc.get("uri") or "") if method == "AES-128" else "",
            referer=referer, concurrency=concurrency,
            progress_callback=progress_callback, stop_flag=stop_flag,
            cookies=cookies, log=log,
            keep=tuple(x for x in (("init.mp4" if pl.get("has_map") else ""),
                                   ("key.mp4" if method == "AES-128" else "")) if x))
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return _stopped()
        return _fail(f"音频分片下载失败：{e}", vg.ERR_HLS_SEGMENT)
    if stop_flag and stop_flag():
        return _stopped()

    durations = [s.get("duration") for s in segs]
    pl_path = os.path.join(shadow, "local.m3u8")
    vg._write_local_hls(pl_path, shadow, seg_paths, init_path=init_path,
                        key_path=key_path, durations=durations, method=method)
    _audio_close_playlist(pl_path)
    try:
        path, nbytes = _mux_audio_to(["-i", pl_path], out_base, log=log,
                                     stop_flag=stop_flag)
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return _stopped()
        return _fail(f"音频 remux 失败：{e}", vg.ERR_FFMPEG)
    # remux 成功 → best-effort 打标，随后才清影子（失败/停止态保留供续传）
    _maybe_tag_file(path, meta, cover_url, referer, cookies, log=log)
    shutil.rmtree(shadow, ignore_errors=True)
    return {"status": "ok", "bytes": nbytes, "path": path, "error": "",
            "error_code": ""}


def _dash_d1_state(mpd_url: str, txt: str):
    """D1 ClearKey 判定（streamdecrypt probe_mpd）：返回 (need_decrypt, keyhex)。
    need=False → 明文/非 ClearKey，绝不碰。need=True + keyhex="" → MPD 声明加密但站侧
    未配密钥（调用方须显式报 enc 错，绝不静默下成密文）。key 供给缝=config AUDIO_DASH_CLEARKEY
    {host 后缀: 16字节内容密钥 hex}，host 后缀匹配（key 由站侧许可/JS 现算得到后人工填）。
    _SD 缺省/探测异常一律 need=False（零默认行为变化）。"""
    _SD = getattr(vg, "_SD", None)
    if _SD is None:
        return False, ""
    try:
        p = _SD.probe_mpd(txt or "")
    except Exception:
        return False, ""
    if not (p and p.get("clearkey")):
        return False, ""
    host = (urlparse(mpd_url).netloc or "").split(":", 1)[0].lower()
    for k, v in (AUDIO_DASH_CLEARKEY or {}).items():
        hk = str(k or "").strip().lower()
        if host == hk or (hk and host.endswith("." + hk)):
            s = str(v or "").strip().lower()
            if len(s) == 32:
                return True, s
    return True, ""


def _download_audio_dash(mpd_url: str, out_base: str, *, referer: str = "",
                         concurrency: int = AUDIO_CONCURRENT,
                         progress_callback=None, stop_flag=None,
                         cookies: dict = None, log=None,
                         meta=None, cover_url: str = "") -> dict:
    """音频 DASH(.mpd) → 干净单文件音频。单文件轨整份下载取音轨；分段轨取 audio 轨
    init+分片 → 本地清单 → remux .m4a/.ogg 等。绝不调 vg.download_dash（会拒纯音频）。
    分段走影子目录跨次续传（包B）；remux 成功后 best-effort 打标（包D）。"""
    if not vg._locate_ffmpeg():
        return _fail("音频 DASH remux 需随包 ffmpeg，请确认 runtime/ffmpeg 就位",
                     vg.ERR_MISSING_FFMPEG)
    try:
        txt = vg._robust_binary(mpd_url, referer, cookies=cookies, min_len=1)\
            .decode("utf-8", "ignore")
    except Exception as e:
        return _fail(f"无法获取 .mpd 清单：{e}", vg.ERR_HLS_MANIFEST)
    plan = vg.parse_dash(txt, mpd_url)
    if not plan.get("ok"):
        return _fail(plan.get("error", ".mpd 解析失败"), vg.ERR_PARSER)

    # D1：ContentProtection→ClearKey 判定 + 站侧 key 供给。声明加密但未配 key → 立即显式失败
    d1_need, d1_keyhex = _dash_d1_state(mpd_url, txt)
    if d1_need and not d1_keyhex:
        return _fail("该 .mpd 为 ClearKey(cenc) 加密但未配置站侧密钥："
                     "config AUDIO_DASH_CLEARKEY 填 {host: 16字节key hex} 后重试",
                     vg.ERR_ENCRYPT_UNSUP)

    audio = plan.get("audio")
    singles = plan.get("single") or []

    # 1) 单文件 DASH（音视频常同文件）：整份下载 → remux 只取音频轨
    if singles:
        s = next((x for x in singles if x.get("type") == "audio"), singles[0])
        src = out_base + ".src"
        vg._remove_if(src)
        res = vg.download_direct(s["url"], src, referer=referer, timeout=AUDIO_TIMEOUT,
                                 progress_callback=progress_callback,
                                 stop_flag=stop_flag, cookies=cookies)
        if res.get("status") != "ok":
            vg._remove_if(src)
            return {"status": res.get("status", "fail"), "bytes": 0, "path": "",
                    "error": f"单文件 DASH 下载失败：{res.get('error') or '网络异常'}",
                    "error_code": res.get("error_code") or vg.ERR_NETWORK}
        cpath = ""
        try:
            if d1_need:                       # ClearKey 单文件：引擎解密整份 → 明文再 remux
                try:
                    with open(src, "rb") as f:
                        data = f.read()
                    clear = vg._SD.decrypt_cenc_media(data, bytes.fromhex(d1_keyhex))
                    cpath = src + ".clear"
                    with open(cpath, "wb") as f:
                        f.write(clear)
                except Exception as e:
                    vg._remove_if(src)
                    return _fail(f"单文件 DASH ClearKey 解密失败：{e}", vg.ERR_ENCRYPT_UNSUP)
            path, nbytes = _mux_audio_to(["-i", cpath or src], out_base, log=log,
                                         stop_flag=stop_flag)
            _maybe_tag_file(path, meta, cover_url, referer, cookies, log=log)
            return {"status": "ok", "bytes": nbytes, "path": path, "error": "",
                    "error_code": ""}
        except RuntimeError as e:
            if stop_flag and stop_flag():
                return _stopped()
            return _fail(f"单文件 DASH remux 失败：{e}", vg.ERR_FFMPEG)
        finally:
            vg._remove_if(src)
            if cpath:
                vg._remove_if(cpath)

    # 2) 分段 DASH：只取独立 audio 轨（影子续传）
    if not audio:
        return _fail("该 .mpd 无独立音频轨、也非单文件（取不到音频）", vg.ERR_PARSER)
    tr = audio
    nseg = len(tr.get("segs") or [])
    if nseg > AUDIO_MAX_SEGMENTS:
        return _fail(f"DASH 音频分片数量超过上限 {AUDIO_MAX_SEGMENTS}，已拒绝", vg.ERR_SIZE_OVER)

    shadow = _audio_shadow_dir(out_base, mpd_url)
    init_path = None
    if tr.get("init"):
        init_path = os.path.join(shadow, "init.mp4")
        try:
            _audio_ensure_file(init_path, tr["init"], referer, cookies, min_len=1)
        except Exception as e:
            return _fail(f"DASH 音频 init 片段获取失败：{e}", vg.ERR_HLS_SEGMENT)
    try:
        seg_paths = _pull_segments_resumable(
            shadow=shadow, media_url=mpd_url, kind="dash", out_base=out_base,
            uris=list(tr["segs"]), n_total=nseg,
            init_uri=tr.get("init") or "",
            referer=referer, concurrency=concurrency,
            progress_callback=progress_callback, stop_flag=stop_flag,
            cookies=cookies, log=log,
            keep=("init.mp4",) if tr.get("init") else ())
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return _stopped()
        return _fail(f"DASH 音频分片下载失败：{e}", vg.ERR_HLS_SEGMENT)
    if stop_flag and stop_flag():
        return _stopped()

    if d1_need:                     # 分段 DASH ClearKey：init+分片引擎解密成明文碎片 → remux
        try:
            init_b = b""
            if init_path and os.path.exists(init_path):
                with open(init_path, "rb") as f:
                    init_b = f.read()
            blobs = []
            for sp in seg_paths:
                with open(sp, "rb") as f:
                    blobs.append(f.read())
            clear = vg._SD.decrypt_cenc_fragments(
                ([init_b] if init_b else []) + blobs, bytes.fromhex(d1_keyhex))
            ctmp = os.path.join(shadow, "clear.mp4")
            with open(ctmp, "wb") as f:
                f.write(clear)
            mux_in = ctmp
        except RuntimeError as e:
            return _fail(f"DASH ClearKey 解密失败：{e}", vg.ERR_ENCRYPT_UNSUP)
        except Exception as e:
            return _fail(f"DASH ClearKey 解密失败：{e}", vg.ERR_ENCRYPT_UNSUP)
        try:
            path, nbytes = _mux_audio_to(["-i", mux_in], out_base, log=log,
                                         stop_flag=stop_flag)
        except RuntimeError as e:
            if stop_flag and stop_flag():
                return _stopped()
            return _fail(f"DASH 音频 remux 失败：{e}", vg.ERR_FFMPEG)
        _maybe_tag_file(path, meta, cover_url, referer, cookies, log=log)
        shutil.rmtree(shadow, ignore_errors=True)
        return {"status": "ok", "bytes": nbytes, "path": path, "error": "",
                "error_code": ""}

    pl_path = os.path.join(shadow, "local.m3u8")
    vg._write_local_hls(pl_path, shadow, seg_paths, init_path=init_path)
    _audio_close_playlist(pl_path)
    try:
        path, nbytes = _mux_audio_to(["-i", pl_path], out_base, log=log,
                                     stop_flag=stop_flag)
    except RuntimeError as e:
        if stop_flag and stop_flag():
            return _stopped()
        return _fail(f"DASH 音频 remux 失败：{e}", vg.ERR_FFMPEG)
    _maybe_tag_file(path, meta, cover_url, referer, cookies, log=log)
    shutil.rmtree(shadow, ignore_errors=True)
    return {"status": "ok", "bytes": nbytes, "path": path, "error": "",
            "error_code": ""}


def _download_audio_direct(url: str, out_base: str, *, ext: str = "",
                           referer: str = "", progress_callback=None,
                           stop_flag=None, cookies: dict = None,
                           log=None, meta=None, cover_url: str = "") -> dict:
    """音频直链下载 + 假直链魔数校验（包A）。
    vg.download_direct 已带 .part/Range 续传与 text/* 拒绝；此处补 200-但-非音频探测
    （风控假成功，如 application/json 伪成功体），命中即删落盘并归 _ERR_FAKE_MEDIA。
    meta/cover_url（包D 对等补齐）：直链成品原样保存后也 best-effort 打标——与
    m3u8/dash remux 路径同缝 _maybe_tag_file（无标题/艺术家/封面或 tag 关掉时零动作）。"""
    dest_ext = ext or "m4a"
    dest = vg.ensure_unique_path(f"{out_base}.{dest_ext}")
    res = vg.download_direct(url, dest, referer=referer, timeout=AUDIO_TIMEOUT,
                             progress_callback=progress_callback,
                             stop_flag=stop_flag, cookies=cookies)
    if res.get("status") != "ok":
        return res
    ok, why = _probe_audio_container(dest, dest_ext)
    if not ok:
        vg._remove_if(dest)
        return _fail(f"直链返回非音频（疑似风控假成功，{res.get('bytes')}B）：{why}",
                     _ERR_FAKE_MEDIA)
    if meta:
        try:
            _maybe_tag_file(dest, meta, cover_url, referer=referer,
                            cookies=cookies, log=log)
        except Exception:
            pass
    return res


def _download_audio_by_kind(kind: str, url: str, out_base: str, *, ext: str = "",
                            referer: str = "", concurrency: int = AUDIO_CONCURRENT,
                            progress_callback=None, stop_flag=None,
                            cookies: dict = None, log=None,
                            meta=None, cover_url: str = "") -> dict:
    """音频媒体 URL 下载总入口。out_base 为不含扩展名的绝对路径前缀。
    direct → 原容器原样保存（下载后魔数校验）；m3u8/dash → remux 成干净单文件。
    meta/cover_url：页面解析出的内容元数据（包D），仅 remux 路径 best-effort 打标。
    返回 status dict。"""
    if kind == "direct":
        return _download_audio_direct(url, out_base, ext=ext, referer=referer,
                                      progress_callback=progress_callback,
                                      stop_flag=stop_flag, cookies=cookies, log=log,
                                      meta=meta, cover_url=cover_url)
    if kind == "m3u8":
        return _download_audio_hls(url, out_base, referer=referer, concurrency=concurrency,
                                   progress_callback=progress_callback,
                                   stop_flag=stop_flag, cookies=cookies, log=log,
                                   meta=meta, cover_url=cover_url)
    if kind == "dash":
        return _download_audio_dash(url, out_base, referer=referer, concurrency=concurrency,
                                    progress_callback=progress_callback,
                                    stop_flag=stop_flag, cookies=cookies, log=log,
                                    meta=meta, cover_url=cover_url)
    return _fail(f"不支持的音频类型：{kind}", vg.ERR_UNKNOWN)


# ================================================================
# 内容元数据抽取 + remux 成品打标（包D）
# ================================================================

_META_LD_TYPES = {"musicrecording", "audioobject"}


def extract_audio_metadata(html: str, page_url: str) -> dict:
    """从页面抽取通用音频内容元数据（只读通用信号：JSON-LD/og:/twitter:）。
    返回 {title, artist, album, cover_url, audio_url}（无则空串）。永不 raise。"""
    meta = {"title": "", "artist": "", "album": "", "cover_url": "", "audio_url": ""}
    if not html:
        return meta

    def _pick(d, *keys):
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _name_of(node):
        if isinstance(node, dict):
            return _pick(node, "name", "title", "headline")
        if isinstance(node, list) and node and isinstance(node[0], dict):
            return _name_of(node[0])
        return ""

    # ① JSON-LD：MusicRecording / AudioObject（name/byArtist/inAlbum/contentUrl/image）
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
                if not (types and (set(types) & _META_LD_TYPES)):
                    stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
                    continue
                if not meta["title"]:
                    meta["title"] = _pick(node, "name", "headline", "title")
                if not meta["artist"]:
                    ba = node.get("byArtist") or node.get("creator")
                    meta["artist"] = _name_of(ba)
                if not meta["album"]:
                    ia = node.get("inAlbum") or node.get("album")
                    meta["album"] = _name_of(ia)
                if not meta["cover_url"]:
                    img = node.get("image") or node.get("thumbnailUrl")
                    if isinstance(img, dict):
                        meta["cover_url"] = _pick(img, "url", "contentUrl", "thumbnailUrl")
                    elif isinstance(img, str):
                        meta["cover_url"] = img.strip()
                if not meta["audio_url"]:
                    meta["audio_url"] = _pick(node, "contentUrl", "contentStreamUrl",
                                              "streamUrl", "embedUrl", "url")
            elif isinstance(node, list):
                stack.extend(node)

    # ② og:/twitter: 兜底（title/cover/audio）
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        tag = m.group(0)
        pm = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        cm = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if not (pm and cm):
            continue
        prop = pm.group(1).strip().lower()
        val = vg._safe_unescape(cm.group(1)).strip()
        if not val:
            continue
        if prop in ("og:title", "twitter:title") and not meta["title"]:
            meta["title"] = val
        elif prop in ("og:image", "og:image:url", "twitter:image") and not meta["cover_url"]:
            meta["cover_url"] = val
        elif prop in ("og:audio", "og:audio:url", "og:audio:secure_url",
                      "twitter:audio") and not meta["audio_url"]:
            meta["audio_url"] = val

    if meta["cover_url"]:
        meta["cover_url"] = vg._normalize(meta["cover_url"], page_url) or meta["cover_url"]
    if meta["audio_url"]:
        meta["audio_url"] = vg._normalize(meta["audio_url"], page_url) or meta["audio_url"]
    meta["title"] = meta["title"][:200]
    meta["artist"] = meta["artist"][:120]
    meta["album"] = meta["album"][:120]
    return meta


def _maybe_tag_file(out_path: str, meta: dict, cover_url: str = "",
                    referer: str = "", cookies: dict = None, log=None) -> str:
    """对 remux 成品做 best-effort 元数据 + 封面写入（包D）。soft-fail，绝不删原文件。
    两档降级：① metadata + attached_pic 封面；② 封面档失败降纯 metadata 档。
    无 title/artist/album 且无封面 → 跳过。返回路径（任何失败保持原文件不变）。"""
    if not out_path or not os.path.exists(out_path) or not AUDIO_TAG_ENABLED:
        return out_path
    meta = meta or {}
    tags = {}
    for k, label in (("title", "title"), ("artist", "artist"), ("album", "album")):
        v = str(meta.get(k) or "").strip()
        if v:
            tags[label] = v
    if not tags and not cover_url:
        return out_path
    if not vg._locate_ffmpeg():
        return out_path
    dirn = os.path.dirname(out_path) or "."
    base = os.path.basename(out_path)
    # 临时名必须保留音频扩展，否则 ffmpeg 无法按扩展名猜输出容器（.tagtmp → Invalid argument）
    ext = os.path.splitext(base)[1] or ".mp4"
    tagtmp = os.path.join(dirn, base + ".tagtmp" + ext)
    art_path = ""
    vg._remove_if(tagtmp)
    if cover_url:                                    # 封面 best-effort 抓取（超限/失败弃）
        art_path = os.path.join(dirn, "." + base + ".art")
        vg._remove_if(art_path)
        try:
            art = vg.download_direct(cover_url, art_path, referer=referer,
                                     timeout=AUDIO_TIMEOUT, cookies=cookies)
            if art.get("status") != "ok":
                vg._remove_if(art_path)
                art_path = ""
            else:
                try:
                    if not (0 < os.path.getsize(art_path) <= AUDIO_COVER_MAX_BYTES):
                        vg._remove_if(art_path)
                        art_path = ""
                except OSError:
                    vg._remove_if(art_path)
                    art_path = ""
        except Exception:
            vg._remove_if(art_path)
            art_path = ""

    def _meta_args():
        out = []
        for k, v in tags.items():
            out += ["-metadata", f"{k}={v}"]
        return out

    ok = False
    if art_path:                                     # 档① metadata + 封面
        args = (["-hide_banner", "-y", "-i", out_path, "-i", art_path,
                 "-map", "0:a:0", "-c:a", "copy",
                 "-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                 "-metadata:s:v", "title=Album cover"]
                + _meta_args() + [tagtmp])
        try:
            rc, _tail = vg._run_ffmpeg(args, log=None, stop_flag=None)
        except Exception:
            rc = 1
        if rc == 0 and os.path.exists(tagtmp) and os.path.getsize(tagtmp) > 0:
            try:
                os.replace(tagtmp, out_path)
                ok = True
            except OSError:
                ok = False
        vg._remove_if(tagtmp)
    if not ok:                                       # 档② 纯 metadata
        args = (["-hide_banner", "-y", "-i", out_path, "-map", "0:a:0", "-c:a", "copy"]
                + _meta_args() + [tagtmp])
        try:
            rc, _tail = vg._run_ffmpeg(args, log=None, stop_flag=None)
        except Exception:
            rc = 1
        if rc == 0 and os.path.exists(tagtmp) and os.path.getsize(tagtmp) > 0:
            try:
                os.replace(tagtmp, out_path)
                ok = True
            except OSError:
                ok = False
        vg._remove_if(tagtmp)
    vg._remove_if(art_path)
    if ok and log:
        try:
            log(f"   已写元数据/封面：{', '.join(tags) or 'cover'}")
        except Exception:
            pass
    return out_path


# ================================================================
# 单页 / 直链处理（run_batch 内逐 URL）
# ================================================================


def _as_record(c: AudioCandidate, res: dict, meta: dict = None) -> dict:
    rec = {"index": c.index, "kind": c.kind, "url": c.url, "ext": c.ext,
           "src": c.src, "page_url": c.page_url,
           "status": res.get("status", "fail"),
           "local_path": res.get("path") or "", "bytes_count": res.get("bytes", 0),
           "error": res.get("error", "")}
    if meta:
        kept = {k: v for k, v in meta.items()
                if k in ("title", "artist", "album") and v}
        if kept:
            rec["meta"] = kept
    return rec


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"


def _log(log_callback, msg):
    if log_callback:
        try:
            log_callback(str(msg))
        except Exception:
            pass


def process_audio_url(url: str, *, audio_dir: str = AUDIO_DIR,
                      progress_callback=None, log_callback=None,
                      stop_flag=None) -> dict:
    """单个 URL（媒体直链/流 或 网页）→ 音频下载记录（page 结构，兼容 run_batch）。"""
    def log(msg):
        _log(log_callback, msg)

    kind, ext = _classify_audio(url)
    # #8a：用户直贴无扩展名 URL（/play?id=9）→ 先做一次 Content-Type 嗅探，audio/* 按直链收。
    if kind is None and _url_extless(url) \
            and (urlparse(url).scheme or "").lower() in ("http", "https"):
        ext = _probe_direct_audio(url, referer=url)
        kind = "direct" if ext else None

    # ── ① 媒体直链/流：直接下载 ──
    if kind is not None:
        c = AudioCandidate(index=1, kind=kind, url=url, ext=ext,
                           src="媒体URL", page_url=url,
                           title_hint=ext if ext not in ("m3u8",) else "")
        stem = _unique_audio_stem(audio_dir, _stem_from_title("", url))
        out_base = os.path.join(audio_dir, stem)
        log(f"🔊 音频媒体直链：{url}")
        try:
            res = _download_audio_by_kind(kind, url, out_base, ext=ext, referer=url,
                                          progress_callback=progress_callback,
                                          stop_flag=stop_flag, log=log)
        except Exception as e:          # 双保险：worker 内异常转失败记录
            res = _fail(f"{e}", vg.ERR_UNKNOWN)
        if res.get("status") == "ok":
            log(f"   ✓ {os.path.basename(res['path'])} {_fmt_size(res.get('bytes', 0))}")
        elif res.get("status") == "stopped":
            log("   已停止")
        elif res.get("status") == "unsupported":
            log(f"   △ {res.get('error') or '不支持'}")
        else:
            log(f"   ✗ {res.get('error') or '下载失败'}")
        rec = _as_record(c, res)
        return {"url": url, "title": "", "error": "", "candidates": [],
                "records": [rec]}

    # ── ② 网页：静态深挖解析 → 逐候选下载 ──
    page = {"url": url, "title": "", "error": "", "candidates": [], "records": []}
    html, final_url = vg.fetch_html(url, timeout=AUDIO_TIMEOUT,
                                    max_bytes=AUDIO_MAX_PAGE_BYTES)
    title = vg.extract_title(html, final_url or url)
    page["title"] = title
    mmeta = extract_audio_metadata(html, final_url or url) if html else {}
    cands = []
    if html:
        cands = _resolve_audio_from_page(url, html0=html, final0=final_url or url,
                                         log=log, stop_flag=stop_flag)
    page["candidates"] = [{k: v for k, v in asdict(c).items() if k != "cookies"}
                          for c in cands]
    if not cands:
        page["error"] = "未解析到音频(m3u8/mpd/直链)"
        log(f"{url} → {page['error']}")
        return page

    total = len(cands)
    log(f"✅ 页面解析到 {total} 个音频候选，开始下载：{title}")
    base = _stem_from_title(title, url)
    for c in cands:
        if stop_flag and stop_flag():
            rec = _as_record(c, _stopped(), mmeta)
            rec["status"] = "stopped"
            page["records"].append(rec)
            continue
        stem = _unique_audio_stem(audio_dir, base)
        out_base = os.path.join(audio_dir, stem)
        referer = c.page_url or url          # 用流实际所在页做 Referer（防盗链更稳）
        log(f"[{c.index}/{total}] {c.kind:5s} 下载中 → {os.path.basename(out_base)}")

        def cb(cur, tot, extra, _c=c):
            if progress_callback:
                try:
                    progress_callback(cur, tot,
                                      f"{_c.kind} {os.path.basename(out_base)} {extra}")
                except Exception:
                    pass

        try:
            res = _download_audio_by_kind(c.kind, c.url, out_base, ext=c.ext,
                                          referer=referer, progress_callback=cb,
                                          stop_flag=stop_flag, cookies=c.cookies or None,
                                          log=log, meta=mmeta or None,
                                          cover_url=(mmeta or {}).get("cover_url") or "")
        except Exception as e:
            res = _fail(str(e), vg.ERR_UNKNOWN)
        if res.get("status") == "ok":
            log("   ✓ " + os.path.basename(res["path"]) + f" {_fmt_size(res.get('bytes', 0))}")
        elif res.get("status") == "stopped":
            log("   已停止")
        elif res.get("status") == "unsupported":
            log(f"   △ {res.get('error') or '不支持'}")
        else:
            log(f"   ✗ {res.get('error') or '下载失败'}")
        page["records"].append(_as_record(c, res, mmeta))
    return page


# ================================================================
# run_batch / 整部 run_series
# ================================================================


def run_batch(urls: list, *, audio_dir: str = AUDIO_DIR,
              progress_callback=None, log_callback=None,
              stop_flag=None) -> dict:
    """批量任务（worker 线程调用）：URL 逐个串行（页面或媒体直链），结束统一写元数据 JSON。"""
    def log(msg):
        _log(log_callback, msg)

    os.makedirs(audio_dir, exist_ok=True)
    cleanup_audio_partials(AUDIO_PARTIAL_MAX_AGE)   # 顺带清扫超龄影子目录
    pages = []
    log(f"音频抓取任务开始，共 {len(urls)} 个 URL")
    session_records = []
    session_file = os.path.join(audio_dir, f"audio_{time.strftime(TIME_FORMAT)}.json")
    for i, u in enumerate(urls, start=1):
        if stop_flag and stop_flag():
            log("已停止（独立停止标志，不影响主爬虫）")
            break
        log(f"\n[{i}/{len(urls)}] {u}")
        page = process_audio_url(u, audio_dir=audio_dir,
                                 progress_callback=progress_callback,
                                 log_callback=log_callback, stop_flag=stop_flag)
        pages.append(page)
        session_records.extend(page["records"])
    meta_path = vg.write_metadata_json(audio_dir, session_records, session_file)
    ok = sum(1 for p in pages for r in p["records"] if r.get("status") == "ok")
    fail = sum(1 for p in pages for r in p["records"] if r.get("status") == "fail")
    skip = sum(1 for p in pages for r in p["records"]
               if r.get("status") in ("skipped", "unsupported"))
    stopped = sum(1 for p in pages for r in p["records"] if r.get("status") == "stopped")
    log(f"全部完成：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}，元数据见 {meta_path}")
    return {"pages": pages, "ok": ok, "fail": fail, "skip": skip,
            "stopped": stopped, "meta_path": meta_path}


# 音频整部分集文件名标签上限 / 目录名上限 / 最少锚点数 —— 复用 vg 同值语义但独立命名
_EP_LABEL_MAX = 60
_SERIES_TITLE_MAX = 60


def _plan_keys_in_range(by_num: dict, ep_start=None, ep_end=None) -> list:
    """分集任务排序：int 集号升序 + 未知集号按 URL 独立成组（放尾部）；
    ep_start/ep_end 过滤区间且丢弃未知集号（与视频整部语义一致）。"""
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
    return num_keys + unk_keys


def _episode_record(index, label, status, local_path="", bytes_count=0,
                    error="", error_code="", page_url="", kind="", url="",
                    line=None) -> dict:
    return {"index": index, "line": line, "label": label, "page_url": page_url,
            "kind": kind, "url": url, "status": status,
            "local_path": local_path, "bytes_count": bytes_count,
            "error": error, "error_code": error_code}


_EP_META_CACHE = {}


def _episode_meta(page: str) -> dict:
    """取某集播放页的通用内容元数据（包D 整部对等补齐）。仅 tag 开启时才拉页（缺省零动作）。
    每集页已由 _resolve_audio_from_page 内部取过一次，这里为不打标不浪费请求，用
    进程级小缓存按页 URL 复用；缓存只增小 dict、超限即清防长整部无界涨。失败 → 空 dict。"""
    if not (AUDIO_TAG_ENABLED and page and str(page).startswith(("http://", "https://"))):
        return {}
    hit = _EP_META_CACHE.get(page)
    if hit is not None:
        return hit
    try:
        html, _final = vg.fetch_html(page)
        meta = extract_audio_metadata(html or "", page)
    except Exception:
        meta = {}
    if len(_EP_META_CACHE) >= 400:
        _EP_META_CACHE.clear()
    _EP_META_CACHE[page] = meta
    return meta


def _download_episode_audio(cands: list, folder: str, base: str, *,
                            skip_existing: bool = True,
                            log=None, stop_flag=None) -> dict:
    """逐条线路尝试把某集音频下到 folder/{base}.<ext>。
    cands=[{num,label,url,line},...] 已按线路优先级排序。
    返回 record dict（status: ok/fail/stopped/unsupported）。"""
    def logmsg(m):
        _log(log, m)

    def try_audio(cand, v, page):
        """尝试下载单个音频候选 → folder/{base}.<ext>。成功 → record；失败 → None（清半成品）。"""
        referer = v.page_url or page
        out_base = os.path.join(folder, base)
        if skip_existing:
            existing = _existing_audio(folder, base)
            if existing:
                size = os.path.getsize(existing)
                logmsg(f"     ✓ 已存在，跳过下载 → {os.path.basename(existing)} ({_fmt_size(size)})")
                return _episode_record(cand.get("num"), cand.get("label"),
                                       "ok", existing, size, page_url=page,
                                       kind=v.kind, url=v.url, line=cand.get("line"))
        meta = _episode_meta(page)
        m = meta if (meta.get("title") or meta.get("artist") or meta.get("album")
                     or meta.get("cover_url")) else None
        try:
            res = _download_audio_by_kind(v.kind, v.url, out_base, ext=v.ext,
                                          referer=referer, stop_flag=stop_flag,
                                          cookies=v.cookies or None, log=logmsg,
                                          meta=m, cover_url=(m or {}).get("cover_url", ""))
        except Exception as e:
            res = _fail(str(e) or "网络异常", vg.ERR_NETWORK)
        if res.get("status") == "ok":
            path = res.get("path") or out_base
            for e in AUDIO_OUT_EXTS:               # 清理同 base 其他容器残留
                p = os.path.join(folder, f"{base}.{e}")
                if p != path and os.path.exists(p):
                    vg._remove_if(p)
            logmsg(f"     ✓ 下载完成 → {os.path.basename(path)} ({_fmt_size(res.get('bytes', 0))})")
            return _episode_record(cand.get("num"), cand.get("label"), "ok",
                                   path, res.get("bytes", 0), page_url=page,
                                   kind=v.kind, url=v.url, line=cand.get("line"))
        if res.get("status") == "stopped":
            return _episode_record(cand.get("num"), cand.get("label"), "stopped",
                                   page_url=page, kind=v.kind, url=v.url,
                                   line=cand.get("line"))
        logmsg(f"     ✗ {res.get('error') or '下载失败'}")
        return None

    for cand in cands:
        if stop_flag and stop_flag():
            return _episode_record(cand.get("num"), cand.get("label"), "stopped",
                                   page_url=cand.get("url"), line=cand.get("line"))
        page = cand.get("url") or ""
        logmsg(f"    源线路{cand.get('line', '-')}：{page}")

        def _line_log(m):
            logmsg("      " + str(m))
        media_cands = _resolve_audio_from_page(page, log=_line_log, stop_flag=stop_flag)
        if not media_cands:
            logmsg("     ✗ 未解析到音频，切换线路")
            continue
        for v in media_cands:               # 逐个尝试：任一成功/停止即整体返回
            rec = try_audio(cand, v, page)
            if rec is not None:
                return rec
        logmsg("     ✗ 该线路所有音频候选均失败，切换下一条线路")
    logmsg("     ✗ 所有线路均失败")
    return _episode_record(None, None, "fail", error="所有线路均失败",
                           error_code=vg.ERR_UNKNOWN)


def process_audio_series(seed_url: str, *, audio_dir: str = AUDIO_DIR,
                         ep_start=None, ep_end=None, skip_existing: bool = True,
                         series_concurrent: int = AUDIO_SERIES_PARALLEL,
                         progress_callback=None, log_callback=None,
                         stop_flag=None) -> dict:
    """整部音频抓取：详情页/任一分集页 → 识别分集 → 按线路故障切换逐集下载。
    音频整部固定串行（单 IP 礼貌有界；series_concurrent>1 会被降为 1 并在日志说明）。
    返回 run_batch 兼容 summary（pages[].records）。"""
    def log(m):
        _log(log_callback, m)

    if int(series_concurrent) > 1:
        log(f"音频整部固定单集串行（单 IP 礼貌有界），series_concurrent={series_concurrent} 已降为 1")
        series_concurrent = 1

    html, final = vg.fetch_html(seed_url, timeout=AUDIO_TIMEOUT,
                                max_bytes=AUDIO_MAX_PAGE_BYTES)
    plan = vg.parse_episode_plan(html, final or seed_url)
    if plan["matched"] == "none" or not plan.get("episodes"):
        return {"mode": "none", "title": plan["title"], "url": seed_url,
                "reason": plan.get("reason") or "未识别到分集列表",
                "pages": [], "ok": 0, "fail": 0, "skip": 0, "stopped": 0,
                "meta_path": ""}

    title = vg._fs_clean(plan["title"], _SERIES_TITLE_MAX) or "未命名专辑"
    folder = os.path.join(audio_dir, title)
    os.makedirs(folder, exist_ok=True)
    log(f"🎧 识别整部音频：{plan['title']}（{plan['matched']} 解析 · {plan['lines']} 条线路 · "
        f"{len(plan['episodes'])} 条分集锚点）")

    by_num = {}
    for e in plan["episodes"]:
        key = e["num"] if e["num"] is not None else ("u", e["url"])
        by_num.setdefault(key, []).append(e)
    target_keys = _plan_keys_in_range(by_num, ep_start, ep_end)
    total = len(target_keys)
    log(f"共 {total} 集进入下载队列，落盘目录：{folder}")

    # 预构建有序任务（文件命名与并行无关，串行下天然稳定）
    tasks = []
    for i, key in enumerate(target_keys, start=1):
        cands = sorted(by_num[key], key=lambda e: (0 if e["line"] is None else e["line"]))
        first = cands[0]
        num = first["num"]
        label_raw = first["label"] or (f"第{num:02d}集" if isinstance(num, int) else "")
        base = vg._fs_clean(label_raw, _EP_LABEL_MAX)
        if not base:
            base = f"第{i:02d}集" if isinstance(num, int) else f"ep{i:02d}"
        disp = f"第{num:02d}集" if isinstance(num, int) else base
        tasks.append({"idx": i, "num": num, "disp": disp, "base": base, "cands": cands})

    records = []
    pages = []
    ok = fail = skip = stopped = 0
    for i, task in enumerate(tasks, start=1):
        if stop_flag and stop_flag():
            log("已停止（整部任务）")
            break
        log(f"[{i}/{total}] {task['disp']}")
        try:
            rec = _download_episode_audio(task["cands"], folder, task["base"],
                                          skip_existing=skip_existing,
                                          log=log, stop_flag=stop_flag)
        except Exception as e:               # 双保险：worker 内异常转失败记录
            rec = _episode_record(task["num"] if isinstance(task["num"], int) else i,
                                  task["disp"], "fail", error=str(e),
                                  error_code=vg.ERR_UNKNOWN)
        if rec.get("status") == "stopped":
            stopped += 1
        elif rec.get("status") == "ok":
            ok += 1
        elif rec.get("status") == "unsupported":
            skip += 1
        else:
            fail += 1
            rec["error"] = (rec.get("error") or "失败") + \
                f"｜尝试线路{[c.get('line') for c in task['cands']]} 分集页 {len(task['cands'])} 个"
        rec.setdefault("index", task["num"] if isinstance(task["num"], int) else task["idx"])
        rec.setdefault("label", task["disp"])
        records.append(rec)
        pages.append({"url": rec.get("page_url") or "", "title": rec.get("label") or "",
                      "records": [rec]})
        if progress_callback:                  # 已完成集数/总数（单调递增，适合状态栏）
            try:
                progress_callback(i, total, task["disp"])
            except Exception:
                pass
    # 停止时未跑到的剩余任务补 stopped
    for task in tasks[len(records):]:
        rec = _episode_record(task["num"] if isinstance(task["num"], int) else task["idx"],
                              task["disp"], "stopped")
        stopped += 1
        records.append(rec)
        pages.append({"url": "", "title": task["disp"], "records": [rec]})

    session_file = os.path.join(folder, f"audio_{time.strftime(TIME_FORMAT)}.json")
    meta_path = vg.write_metadata_json(folder, records, session_file)
    log(f"整部任务结束：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}，元数据 {meta_path}")
    return {"mode": "series", "title": plan["title"], "url": seed_url,
            "folder": folder, "ep_start": ep_start, "ep_end": ep_end,
            "pages": pages, "ok": ok, "fail": fail, "skip": skip,
            "stopped": stopped, "meta_path": meta_path}


def run_series(urls: list, *, audio_dir: str = AUDIO_DIR,
               ep_start=None, ep_end=None,
               series_concurrent: int = AUDIO_SERIES_PARALLEL,
               progress_callback=None, log_callback=None, stop_flag=None) -> dict:
    """整部音频入口（AudioGrabDialog 整部模式调用）：URL 逐个尝试识别为整部，
    识别失败回退为 run_batch；结果合并为 run_batch 兼容 summary。"""
    def log(m):
        _log(log_callback, m)

    pages, ok, fail, skip, stopped = [], 0, 0, 0, 0
    meta_path = ""
    cleanup_audio_partials(AUDIO_PARTIAL_MAX_AGE)   # 顺带清扫超龄影子目录
    log(f"🎧 整部音频抓取任务开始，共 {len(urls)} 个 URL")
    for i, u in enumerate(urls, start=1):
        if stop_flag and stop_flag():
            log("已停止")
            break
        log(f"\n[{i}/{len(urls)}] {u}")
        res = process_audio_series(u, audio_dir=audio_dir,
                                   ep_start=ep_start, ep_end=ep_end,
                                   series_concurrent=series_concurrent,
                                   progress_callback=progress_callback,
                                   log_callback=log_callback, stop_flag=stop_flag)
        if res.get("mode") == "none":
            log(f"该页未识别为整部，回退为单页批量抓取：{res.get('reason') or ''}")
            sub = run_batch([u], audio_dir=audio_dir,
                            progress_callback=progress_callback,
                            log_callback=log_callback, stop_flag=stop_flag)
            pages.extend(sub.get("pages") or [])
            ok += sub.get("ok", 0); fail += sub.get("fail", 0)
            skip += sub.get("skip", 0); stopped += sub.get("stopped", 0)
            meta_path = sub.get("meta_path") or meta_path
            continue
        pages.extend(res.get("pages") or [])
        ok += res.get("ok", 0); fail += res.get("fail", 0)
        skip += res.get("skip", 0); stopped += res.get("stopped", 0)
        meta_path = res.get("meta_path") or meta_path
    log(f"整部音频抓取全部结束：成功 {ok} / 失败 {fail} / 跳过 {skip} / 停止 {stopped}")
    return {"mode": "series", "pages": pages, "ok": ok, "fail": fail,
            "skip": skip, "stopped": stopped, "meta_path": meta_path}


# ================================================================
# 自检 / CLI（纯内存，不联网、不依赖 ffmpeg/Qt）
# ================================================================


def _selftest() -> bool:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="audio_grab_st_")
    try:
        # ① _classify_audio
        assert _classify_audio("https://a.com/x.m3u8") == ("m3u8", "m3u8")
        assert _classify_audio("https://a.com/x.m3u8?token=1") == ("m3u8", "m3u8")
        assert _classify_audio("https://a.com/x.mpd") == ("dash", "mpd")
        for ext in ("mp3", "m4a", "aac", "wav", "flac", "ogg"):
            assert _classify_audio(f"https://a.com/x.{ext}") == ("direct", ext), ext
        assert _classify_audio("https://a.com/x.mp4") == (None, None)
        assert _classify_audio("https://a.com/x.ts") == (None, None)
        assert _classify_audio("https://a.com/x.png") == (None, None)
        assert _classify_audio("blob:https://a.com/x") == (None, None)

        # ② HTML 扫描：<audio>/<source type=audio> / JS m3u8 / JS mp3 / blob 应被滤掉
        html = (
            "<html><head>"
            "<script>var u='https://cdn.x.com/a.m3u8';</script>"
            "<script>const p=\"https://cdn.x.com/track.mp3?k=1\";</script>"
            "<meta property=\"og:audio\" content=\"https://cdn.x.com/cover.ogg\">"
            "</head><body>"
            "<audio controls src=\"https://cdn.x.com/book.mp3\"></audio>"
            "<audio controls><source type=\"audio/mpeg\" src=\"//cdn.x.com/alt.m4a\"></audio>"
            "<source src=\"blob:https://x.com/xyz\">"
            "<source src=\"https://cdn.x.com/pic.jpg\">"
            "</body></html>"
        )
        cands = extract_audio_candidates_from_html(html, "https://page.x.com/audio/1")
        kinds = {c.kind for c in cands}
        assert "direct" in kinds and "m3u8" in kinds, kinds
        urls = [c.url for c in cands]
        assert not any("blob:" in u for u in urls)
        assert not any(u.endswith(".jpg") for u in urls)
        assert len(urls) == len(set(urls)), "URL 应去重"

        # ③ _stem_from_title：CJK 标题保留；空/纯符号 → 回退 URL 桩而非 "-"
        s1 = _stem_from_title("我的有声书", "https://x.com/p4711")
        assert "有声" in s1 or "我的" in s1, s1
        s2 = _stem_from_title("", "https://x.com/abc/def-123.m4a?k=9")
        assert s2 == "def-123", s2
        s3 = _stem_from_title("-", "https://x.com/x.mp3")
        assert s3 not in ("-", ""), s3

        # ④ _unique_audio_stem / _existing_audio：与现存任何音频扩展冲突则 -2
        assert _existing_audio(tmp, "f") is None
        open(os.path.join(tmp, "f.m4a"), "wb").write(b"1")
        assert _existing_audio(tmp, "f") is not None
        assert _unique_audio_stem(tmp, "f") == "f-2"
        open(os.path.join(tmp, "f-2.mp3"), "wb").write(b"1")
        assert _unique_audio_stem(tmp, "f") == "f-3"

        # ⑤ _plan_keys_in_range：乱序集号升序、区间过滤、区间内丢未知集号
        by = {3: [1], 1: [1], ("u", "https://x/u"): [1]}
        assert _plan_keys_in_range(by) == [1, 3, ("u", "https://x/u")]
        assert _plan_keys_in_range(by, 2, 3) == [3]
        assert _plan_keys_in_range(by, None, 1) == [1]

        # ⑥ _m3u8_attr / 音频组解析
        assert _m3u8_attr('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",DEFAULT=YES,URI="a.m3u8"', "URI") == "a.m3u8"
        assert _m3u8_attr('#EXT-X-MEDIA:TYPE=AUDIO,DEFAULT=NO,URI="b.m3u8"', "DEFAULT") == "NO"
        assert _m3u8_attr("x", "NOPE") == ""

        # ⑦ _probe_audio_container（包A）：魔数正反例（纯内存，无 ffmpeg/网络）
        mp3 = os.path.join(tmp, "probe.mp3")
        with open(mp3, "wb") as f:                      # ID3 头 → ok
            f.write(b"ID3\x03\x00" + b"\x00" * 128)
        assert _probe_audio_container(mp3, "mp3")[0] is True
        with open(mp3, "wb") as f:                      # 纯 JSON（>512B，无 MPEG 帧同步）→ 拒
            f.write(b'{"a":1}' * 200)
        ok, why = _probe_audio_container(mp3, "mp3")
        assert (not ok) and "MPEG" in why, why
        with open(mp3, "wb") as f:                      # 真 MPEG 帧同步（<512 也应通过）
            f.write(b"\xff\xfb\x90\x00" * 6)
        assert _probe_audio_container(mp3, "mp3")[0] is True
        m4a = os.path.join(tmp, "probe.m4a")
        with open(m4a, "wb") as f:
            f.write(b"\x00" * 600)                       # 无 ftyp → 拒
        assert _probe_audio_container(m4a, "m4a")[0] is False
        with open(m4a, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 120)
        assert _probe_audio_container(m4a, "m4a")[0] is True
        with open(os.path.join(tmp, "x.ogg"), "wb") as f:
            f.write(b"OggS" + b"\x00" * 100)
        assert _probe_audio_container(os.path.join(tmp, "x.ogg"), "ogg")[0] is True
        with open(os.path.join(tmp, "x.bin"), "wb") as f:
            f.write(b"abc")                              # 未知扩展 → 不误伤
        assert _probe_audio_container(os.path.join(tmp, "x.bin"), "bin")[0] is True
        with open(os.path.join(tmp, "tiny.mp3"), "wb") as f:
            f.write(b"\x00" * 20)                        # 过小且非媒体 → 拒（带下限文案）
        ok, why = _probe_audio_container(os.path.join(tmp, "tiny.mp3"), "mp3")
        assert (not ok) and str(AUDIO_MIN_MEDIA_BYTES) in why, why

        # ⑧ extract_audio_metadata（包D）：JSON-LD 优先 + og 兜底 + 空页
        jh = ("<html><head>"
              '<meta property="og:title" content="OG曲">'
              '<meta property="og:image" content="https://x.com/og.jpg">'
              '<script type="application/ld+json">'
              '{"@type":"MusicRecording","name":"测试曲目","byArtist":{"name":"某歌手"},'
              '"inAlbum":{"name":"某专辑"},"contentUrl":"https://x.com/a.m3u8",'
              '"image":"https://x.com/big.jpg"}'
              "</script></head></html>")
        md = extract_audio_metadata(jh, "https://page.x.com/p/1")
        assert md["title"] == "测试曲目", md
        assert md["artist"] == "某歌手"
        assert md["album"] == "某专辑"
        assert md["cover_url"] == "https://x.com/big.jpg"     # JSON-LD 先于 og
        assert md["audio_url"] == "https://x.com/a.m3u8"
        md2 = extract_audio_metadata(
            '<meta property="og:audio:url" content="https://x.com/t.mp3">'
            '<meta property="og:title" content="T2">',
            "https://p.x/")
        assert md2["title"] == "T2" and md2["audio_url"] == "https://x.com/t.mp3"
        assert extract_audio_metadata(None, "") == {
            "title": "", "artist": "", "album": "",
            "cover_url": "", "audio_url": ""}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("audio_grab --selftest 全部通过")
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    if len(sys.argv) >= 2:
        res = run_batch(sys.argv[1:])
        print(json.dumps(res, ensure_ascii=False, indent=2)[:4000])
    else:
        print("用法: python audio_grab.py <URL|媒体直链...>   或  python audio_grab.py --selftest")
