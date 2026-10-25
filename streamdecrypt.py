# streamdecrypt.py — 音视频流解密引擎（纯、可测；audio/video 共用；不依赖 GUI/浏览器）。
#
# 职责：形态探测(probe) → 清单解析(带 KEY 轮换/IV/media-sequence) → key 获取(raw/hex/base64/json) →
#       分片解密(AES-128-CBC) → 容器 sanity（防"假成功"喂 ffmpeg）。
#
# 覆盖（支持级见 开发报告/2026-09-08-media-stream-grab-full-upgrade-plan.md §4）：
#   B  HLS AES-128：IV 显式/默认(media sequence)、EXT-X-KEY 轮换、key 载体 raw/hex/base64/json、
#                    key 拉取带头(cookie/Referer)、逐段按各自 key 解密。  [本轮落地]
#   C  SAMPLE-AES/cbcs：probe 识别 + 自写密码学原语 decrypt_cbcs_range/_subsamples
#      (constant-IV CBC + crypt/skip pattern，双 oracle 验证)；容器级 fMP4 senc 解析未做。  [本轮原语]
#   D1 DASH ClearKey/cenc：probe 识别(pssh 有无) —— 解密管线同列后扩。        [后续]
#   D2/E/F 真 DRM：probe → DRM 注记，引擎不碰。                              [仅检测]
#
# 设计原则：
#   - 不 import video_grab/audio_grab（引擎可独立单测）。二进制获取走注入的 fetch，
#     默认 requests；调用方可传 vg._robust_binary / curl / 本地读盘 等以复用既有健壮栈。
#   - 解密一律在内存做、返回 bytes；由调用方决定落盘/续传策略（不重复 vg 的影子目录逻辑）。
#   - key 缓存键含 (url, referer, cookie 指纹)，避免不同鉴权上下文串 key。

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlparse, urljoin

__all__ = [
    "SCHEME_PLAIN", "SCHEME_HLS_AES128", "SCHEME_HLS_AES256", "SCHEME_HLS_SAMPLE_AES",
    "SCHEME_DASH_CENC", "SCHEME_DRM", "SCHEME_UNKNOWN",
    "DecryptError", "probe_hls", "probe_hls_drm", "probe_mpd", "mpd_clear_key_candidate",
    "parse_hls_media", "segment_iv", "coerce_key", "resolve_aes_key",
    "decrypt_segment", "sanity_ts", "sanity_fmp4", "sanity_media",
    "locate_ffmpeg", "decrypt_cenc_media", "decrypt_cenc_fragments",
    "decrypt_cbcs_range", "decrypt_cbcs_subsamples",
    "DefaultFetch",
]

# ---------------- 形态常量 ----------------
SCHEME_PLAIN = "plain"                    # 无加密
SCHEME_HLS_AES128 = "hls-aes128"
SCHEME_HLS_AES256 = "hls-aes256"
SCHEME_HLS_SAMPLE_AES = "hls-sample-aes"  # SAMPLE-AES / SAMPLE-AES-CTR（cbcs 族）
SCHEME_DASH_CENC = "dash-cenc"            # DASH 带 pssh 加密（含 ClearKey/真 DRM 再细分见 note）
SCHEME_DRM = "drm"                        # 真 CDM-DRM（Widevine/PlayReady/FairPlay）
SCHEME_UNKNOWN = "unknown"

_DRM_PSSH_SYSIDS = {
    "edef8ba979d64acea3c827dcd51d21ed": "Widevine",
    "9a04f07998404286ab92e65be0885f95": "PlayReady",
    "94ce86fb07ff4f43adb893d2fa968ca2": "FairPlay",
}
# Google Widevine "ClearKey"（fake-DRM / 站点用明文 key 但走 pssh 形态）—— 属 D1 可解族，不算商业 DRM
_CLEARKEY_SYSID = "1077efefc0b24d02ace33c1e52e2fb4b"

# HLS 侧的同一件事：EXT-X-KEY 的 KEYFORMAT → 商业 DRM 名。
# 与 _DRM_PSSH_SYSIDS 同构 —— 那边是「pssh 里怎么写」，这边是「清单里怎么写」。
# **判据来自真机勘察原文**（2026-09-10 猫耳FM 只读勘察），不是猜的。
_DRM_KEYFORMAT_MAP = {
    "com.apple.streamingkeydelivery": "FairPlay",
    "com.apple.streamingkeydelivery.v2": "FairPlay",
    "com.microsoft.playready": "PlayReady",
    "com.widevine": "Widevine",
    "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed": "Widevine",
    "urn:uuid:9a04f079-9840-4286-ab92-e65be0885f95": "PlayReady",
    "urn:uuid:94ce86fb-07ff-4f43-adb8-93d2fa968ca2": "FairPlay",
}
# 明确「自家解得开 / 不是商业 DRM」的一族。**必须显式列出来**，
# 否则「表里没命中」与「确实是明文」会被混成同一个结果。
_KEYFORMAT_NONCOMMERCIAL = {
    "",                    # KEYFORMAT 缺省 = identity（RFC 8216 §4.3.2.4）
    "identity",
    "org.w3.clearkey",
    "urn:uuid:1077efef-c0b2-4d02-ace3-3c1e52e2fb4b",
}
# KEYFORMAT 认不得时的第二判据：key URI 的 scheme（skd:// 是 Apple key delivery 专用）
_DRM_URI_SCHEMES = {"skd://": "FairPlay", "skr://": "FairPlay"}

# ---------------- 异常 ----------------
class DecryptError(Exception):
    """解密失败：缺 key / 格式不支持 / 数据异常。"""


class UnsupportedScheme(DecryptError):
    """识别到加密形态但引擎尚未实现解密（cbcs/DASH-ClearKey 等后续扩展）。"""


# ---------------- key 缓存 ----------------
_KEY_CACHE = {}
_KEY_CACHE_LOCK = threading.Lock()


def _reset_key_cache():
    with _KEY_CACHE_LOCK:
        _KEY_CACHE.clear()


def _cache_key(uri, referer, cookie_fp):
    return (uri, referer or "", cookie_fp or "")


# ---------------- 注入式二进制抓取 ----------------
class DefaultFetch:
    """默认抓取：requests 直取。调用方可换成 vg._robust_binary 等更健壮的栈。"""

    def __call__(self, url, referer="", cookies=None, min_len=1):
        import requests
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        if referer:
            h["Referer"] = referer
        if cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in cookies.items())
        r = requests.get(url, headers=h, timeout=20)
        if r.status_code != 200:
            raise DecryptError("HTTP %d 取 %s" % (r.status_code, url))
        return r.content


DEFAULT_FETCH = DefaultFetch()


# ---------------- 容器 sanity ----------------
def sanity_ts(data: bytes) -> bool:
    """MPEG-TS：同步 0x47 且按 188 字节对齐连续出现（≥2 个边界）。
    逐字节扫 0x47 极易把含该字节的 mp4/html 误判为 TS —— 必须验证 188 对齐节奏。"""
    if not data:
        return False
    head = data[:8 * 188]                      # 只看前 ~8 包
    limit = min(len(head), 376)                # 至多找前两个候选起点
    for i in range(limit):
        if head[i] != 0x47 or i % 188 != 0:
            continue
        # 在 i 起 188 边界上确认后续仍为 0x47（至少再 2 处；尾巴不足则按已有计）
        hits = 0
        j = i + 188
        while j < len(head):
            if head[j] != 0x47:
                break
            hits += 1
            j += 188
        if hits >= 2:
            return True
    return False


def sanity_fmp4(data: bytes) -> bool:
    """fMP4/MP4 族：应含 ftyp 或 moov 箱。字节序: [size(4)]['ftyp'/'moov']。"""
    if len(data) < 12:
        return False
    if data[4:8] in (b"ftyp", b"moov", b"styp", b"sidx", b"free", b"mdat"):
        return True
    return data[0:4] in (b"ftyp", b"moov")  # 极个别不带 size 前缀的畸形写法


def sanity_media(data: bytes, hint: str = "") -> str:
    """通用容器识别：返回 'ts' | 'mp4' | 'audio' | 'text' | ''。
    hint: 期望容器（'ts'/'mp4'），用于加强判定。"""
    if not data:
        return ""
    # 明文/解后为文本 → 说明"假成功"（200+HTML/JSON）误进下载链
    if data[:1] in (b"<", b"{", b"[") and len(data) < 4096:
        head = data[:512].lower()
        if b"<html" in head or b"<!doctype" in head or head[:1] == b"<":
            return "text"
    if sanity_ts(data):
        return "ts"
    if sanity_fmp4(data):
        return "mp4"
    if data[:3] == b"ID3" or (len(data) > 4 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0):
        return "audio"   # ID3 / MPEG-audio 帧同步
    if data[:4] == b"OggS":
        return "audio"
    return ""


# ---------------- key 载体 coerce ----------------
_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]{22}==$|^[A-Za-z0-9+/]{24}=?$")

_KEY_JSON_FIELDS = ("key", "enc_key", "encKey", "key_hex", "keyHex", "data", "content", "k")


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if len(data) < 16:
        return True
    sample = data[:64]
    nontext = sum(1 for b in sample if b < 0x20 and b not in (0x09, 0x0A, 0x0D))
    return nontext > len(sample) // 4


def coerce_key(body: bytes) -> bytes:
    """从 key 响应体提取 16 字节 key：raw 二进制 / hex 文本 / base64 文本。"""
    if not body:
        raise DecryptError("key 响应为空")
    # 1) JSON：常见站点把 key 包成 {key: "hex"} / {"data":"base64"}
    if body[:1] in (b"{", b"["):
        try:
            obj = json.loads(body.decode("utf-8", "ignore"))
            for field in _KEY_JSON_FIELDS:
                v = obj.get(field)
                if isinstance(v, str) and v:
                    cand = coerce_key(v.encode("ascii", "ignore"))
                    if cand:
                        return cand
            if isinstance(obj, list) and obj:
                for it in obj:
                    if isinstance(it, dict):
                        for field in _KEY_JSON_FIELDS:
                            v = it.get(field)
                            if isinstance(v, str) and v:
                                cand = coerce_key(v.encode("ascii", "ignore"))
                                if cand:
                                    return cand
        except Exception:
            pass
        raise DecryptError("JSON key 体未含可解析 key 字段")
    # 2) hex 文本（32 hex 字符 = 16B）
    if len(body) >= 32:
        s = body.decode("ascii", "ignore").strip()
        if _HEX_RE.match(s):
            return bytes.fromhex(s)
    # 3) base64 文本
    if len(body) >= 22:
        s = body.decode("ascii", "ignore").strip()
        if _B64_RE.match(s):
            try:
                dec = base64.b64decode(s)
                if len(dec) == 16:
                    return dec
            except Exception:
                pass
    # 4) raw 二进制（ffmpeg key.bin 等），前 16 字节
    if len(body) >= 16:
        return body[:16]
    raise DecryptError("key 载体长度不足 16B 且无法识别格式")


# ---------------- HLS media 解析（带 KEY 轮换 / media-sequence / IV） ----------------
def _resolve(uri, base_url):
    return urljoin(base_url, uri.strip().strip('"'))


def parse_hls_media(text: str, base_url: str) -> dict:
    """解析 HLS **media** 清单。返回 {
      'segments':[{uri,dur,seq,enc}],        # enc = {method,uri,iv_hex,keyformat,keyid} 或 None
      'kind':'media'|'master', 'variants':[], 'media_sequence':int,
      'has_map':bool, 'map':{uri,byterange}|None, 'single_file':bool, 'targetduration':float}
    EXT-X-KEY 自出现行起对之后分片生效（轮换支持）。master 清单只收集 variants。"""
    lines = [ln.strip() for ln in text.splitlines()]
    pl = {"kind": "media", "variants": [], "segments": [],
          "media_sequence": 0, "has_map": False, "map": None,
          "single_file": False, "targetduration": 0.0}
    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        pl["kind"] = "master"
        prev = ""
        for ln in lines:
            if ln.startswith("#EXT-X-STREAM-INF"):
                prev = ln
            elif ln and not ln.startswith("#"):
                bw = 0
                m = re.search(r"BANDWIDTH\s*=\s*(\d+)", prev, re.I)
                if m:
                    bw = int(m.group(1))
                res = 0
                m = re.search(r"RESOLUTION\s*=\s*(\d+)x(\d+)", prev, re.I)
                if m:
                    res = int(m.group(1)) * int(m.group(2))
                pl["variants"].append({"uri": _resolve(ln, base_url), "bandwidth": bw,
                                       "resolution": res})
                prev = ""
        return pl

    enc = None
    seq = 0
    for ln in lines:
        if ln.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.search(r":\s*(\d+)", ln)
            if m:
                seq = int(m.group(1))
                pl["media_sequence"] = seq
        elif ln.startswith("#EXT-X-TARGETDURATION"):
            m = re.search(r":\s*([\d.]+)", ln)
            if m:
                pl["targetduration"] = float(m.group(1))
        elif ln.startswith("#EXT-X-BYTERANGE"):
            pl["single_file"] = True
        elif ln.startswith("#EXT-X-MAP"):
            pl["has_map"] = True
            if pl.get("map") is None:
                mu = re.search(r'URI\s*=\s*"([^"]+)"', ln)
                mb = re.search(r"BYTERANGE\s*=\s*([^,\s]+)", ln)
                pl["map"] = {"uri": _resolve(mu.group(1), base_url) if mu else "",
                             "byterange": mb.group(1).strip('"\'') if mb else ""}
        elif ln.startswith("#EXT-X-KEY"):
            method = "NONE"
            uri = ""
            iv_hex = ""
            keyformat = ""
            keyid = ""
            m = re.search(r"METHOD\s*=\s*([^,]+)", ln)
            if m:
                method = m.group(1).strip().upper()
            m = re.search(r'URI\s*=\s*"([^"]+)"', ln)
            if m:
                uri = m.group(1)
            m = re.search(r"IV\s*=\s*(0x[0-9A-Fa-f]+)", ln)
            if m:
                iv_hex = m.group(1)
            m = re.search(r"KEYFORMAT\s*=\s*\"?([^,\"]+)", ln)
            if m:
                keyformat = m.group(1).strip('"')
            m = re.search(r"KEYID\s*=\s*\"?([^,\"]+)", ln)
            if m:
                keyid = m.group(1).strip('"')
            if method == "NONE":
                enc = None
            else:
                enc = {"method": method,
                       "uri": _resolve(uri, base_url) if uri else "",
                       "iv_hex": iv_hex,
                       "keyformat": keyformat,
                       "keyid": keyid}
        elif ln.startswith("#EXTINF"):
            pass
        elif ln and not ln.startswith("#"):
            pl["segments"].append({
                "uri": _resolve(ln, base_url),
                "seq": seq,
                "enc": dict(enc) if enc else None,
            })
            seq += 1
    return pl


def probe_hls(text: str) -> str:
    """按 media 清单里出现的 EXT-X-KEY 判定形态（master 传 variants 再探）。"""
    methods = []
    for ln in text.splitlines():
        if ln.startswith("#EXT-X-KEY"):
            m = re.search(r"METHOD\s*=\s*([A-Za-z0-9_-]+)", ln)
            if m:
                methods.append(m.group(1).upper())
    if not methods:
        return SCHEME_PLAIN
    if all(m in ("NONE", "AES-128") for m in methods):
        if any(m == "AES-128" for m in methods):
            return SCHEME_HLS_AES128
        return SCHEME_PLAIN
    if any(m in ("SAMPLE-AES", "SAMPLE-AES-CTR") for m in methods):
        return SCHEME_HLS_SAMPLE_AES
    if any(m in ("AES-256", "AES-256-CTR") for m in methods):
        return SCHEME_HLS_AES256
    return SCHEME_UNKNOWN


def _drm_system_from_keyformat(keyformat: str, key_uri: str = "") -> str:
    """KEYFORMAT（+ key URI scheme）→ 商业 DRM 名；自家可解或认不得 → ""。

    **只有明确认得的才返回名字。** 认不得的自定义 KEYFORMAT 一律返 ""（= 不下判）——
    宁可漏报，也不要把站方私有格式说成商业 DRM（那会让人白白放弃一个能抓的站，
    正是 2026-09-11 那次误报的教训）。"""
    kf = (keyformat or "").strip().strip('"').lower()
    if kf in _KEYFORMAT_NONCOMMERCIAL:
        return ""
    if kf in _DRM_KEYFORMAT_MAP:
        return _DRM_KEYFORMAT_MAP[kf]
    u = (key_uri or "").strip().lower()
    for sch, name in _DRM_URI_SCHEMES.items():
        if u.startswith(sch):
            return name
    return ""


def probe_hls_drm(text: str) -> dict:
    """HLS 清单的**静态** DRM 判别：只看清单文本，不发请求、不解密。

    与 `probe_hls` 的分工是正交的，别互相替代：
      - `probe_hls` 答「用什么算法」（METHOD）→ identity 与 streamingkeydelivery 都是 SAMPLE-AES；
      - 本函数答「是不是商业 DRM」（KEYFORMAT / key URI scheme）→ 前者自家 ffmpeg 解得开，
        后者必须过 CDM。2026-09-10 猫耳FM 32/32 条地址全是后者，而旧代码只问 METHOD，
        于是整站 DRM 被看成「SAMPLE-AES，也许能解」。

    返回 {commercial, drm_systems, sample_aes, keys, unknown_keyformats}
      - `keys` = 实际扫到的加密 KEY 行数。**没有它就是没扫到，不等于没有 DRM**：
        调用方必须把「扫了几条」写进报告，不许把 keys==0 说成「无 DRM」。
      - `unknown_keyformats` = 认不得的 KEYFORMAT 原文，摊开给用户看而不是吞掉。
    """
    res = {"commercial": False, "drm_systems": [], "sample_aes": False,
           "keys": 0, "unknown_keyformats": []}
    if not text:
        return res
    for ln in text.splitlines():
        if not ln.startswith("#EXT-X-KEY"):
            continue
        m = re.search(r"METHOD\s*=\s*([^,]+)", ln)
        method = (m.group(1).strip().strip('"').upper() if m else "")
        if not method or method == "NONE":
            continue
        res["keys"] += 1
        if method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
            res["sample_aes"] = True
        m = re.search(r'KEYFORMAT\s*=\s*"?([^,"]+)', ln)
        kf = (m.group(1).strip().strip('"') if m else "")
        m = re.search(r'URI\s*=\s*"([^"]+)"', ln)
        uri = m.group(1) if m else ""
        name = _drm_system_from_keyformat(kf, uri)
        if name:
            if name not in res["drm_systems"]:
                res["drm_systems"].append(name)
        elif kf and kf.strip().strip('"').lower() not in _KEYFORMAT_NONCOMMERCIAL \
                and kf not in res["unknown_keyformats"]:
            res["unknown_keyformats"].append(kf)
    res["commercial"] = bool(res["drm_systems"])
    return res


# ---------------- DASH/mpd 形态探测（识别级；解密同列后扩） ----------------
def probe_mpd(text: str) -> dict:
    """扫描 MPD 的 ContentProtection：
      - 无 → plain
      - 有 cenc → scheme=dash-cenc；drm_systems 命中的商业 DRM 名单(Widevine/PlayReady/FairPlay)，
        clearkey=True 当含 Google ClearKey 标识（非巨头伪 DRM，key 多可达）。
    是否"可解"由调用方按 key 可达性定：无商业 system 且(有 clearkey 标识或能取到 key) → D1 走解密。"""
    res = {"scheme": SCHEME_PLAIN, "drm_systems": [], "cenc": False, "clearkey": False}
    if "<ContentProtection" not in text:
        return res
    res["cenc"] = True
    res["scheme"] = SCHEME_DASH_CENC
    # schemeIdUri 是带连字符的 UUID(edef8ba9-…)，pssh hex 无连字符 —— 统一去连字符比对
    flat = text.lower().replace("-", "")
    if _CLEARKEY_SYSID in flat:
        res["clearkey"] = True
    for sysid, name in _DRM_PSSH_SYSIDS.items():
        if sysid in flat:
            res["drm_systems"].append(name)
    return res


def mpd_clear_key_candidate(text: str) -> bool:
    """D1 可解判据：有 CENC 加密、但未标商业 DRM（无 Widevine/PlayReady/FairPlay）。
    标了商业 system 即视为真 DRM —— 引擎不解（D2/E/F 仅检测注记）。"""
    r = probe_mpd(text)
    return r["cenc"] and not r["drm_systems"]


# ---------------- 解密 ----------------
def segment_iv(enc: dict, seq: int) -> bytes:
    """确定分片 IV：EXT-X-KEY 显式 IV → 用之；否则默认 = 分片 media sequence（RFC 8216）16B BE。"""
    if enc and enc.get("iv_hex"):
        iv_hex = enc["iv_hex"]
        if iv_hex.lower().startswith("0x"):
            iv_hex = iv_hex[2:]
        b = bytes.fromhex(iv_hex)
        if len(b) != 16:
            raise DecryptError("EXT-X-KEY IV 长度非 16 字节")
        return b
    return seq.to_bytes(16, "big")


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data   # 非 PKCS7（可能零填充/无填充），保持原样


# ================================================================
# C: cbcs / SAMPLE-AES 解密原语（自写；容器级 senc/fMP4 解析见 decrypt_segment 注记）
# ISO/IEC 23001-7 'cbcs'：AES-128-CBC + constant IV；以 16B 块为单位走 crypt/skip pattern
# （默认 1 加密块 : 9 明文块，即每 160B 里仅头 16B 加密——FairPlay/行业 cbcs 实际形态）。
# 验证等级（如实）：本原语与 pycryptodome 做双独立 oracle——
#   skip=0（全块加密）= 标准 AES-CBC 全链 → 与 AES.MODE_CBC 全量比对；
#   crypt=1 单块 = AES-ECB解密块 XOR constant IV → 与单块 CBC 逐块比对；
#   任意 pattern 内部做 mirror 加密/解密往返一致。真站 cbcs 容器级验证待真实样本。
# ================================================================
def decrypt_cbcs_range(data: bytes, key: bytes, iv: bytes, *,
                       crypt_byte_blocks: int = 1, skip_byte_blocks: int = 9) -> bytes:
    """对一段【受保护连续字节】做 cbcs pattern 解密，返回明文 bytes。

    规则：每个 (crypt+skip) 块组里前 crypt 个 16B 块是密文、后 skip 个是明文。
    - skip==0 → 全段单条 CBC 链（IV=iv），等价标准 AES-128-CBC（_strip 前全块）。
    - skip>0 → crypt 段内按 CBC 链（段首块 IV=iv，后继块 IV=前一密文块）；
      crypt 段之间被明文块隔断、密文链不存在 → 每段首块复位 IV=iv。
    尾部不足 16B 的明文残块不参与（原样保留）。data/key/iv 长度非法 → DecryptError。"""
    if not data:
        return data
    if key is None or len(key) != 16:
        raise DecryptError("cbcs key 须 16 字节")
    if iv is None or len(iv) != 16:
        raise DecryptError("cbcs constant IV 须 16 字节")
    crypt = int(crypt_byte_blocks or 0)
    skip = int(skip_byte_blocks or 0)
    if crypt < 0 or skip < 0 or (crypt == 0 and skip == 0):
        raise DecryptError("cbcs pattern 非法：crypt=%d skip=%d" % (crypt, skip))
    try:
        from Crypto.Cipher import AES
    except ImportError as e:  # pragma: no cover
        raise DecryptError("缺少 pycryptodome：%s" % e)
    whole = len(data) - (len(data) % 16)       # 只处理整 16B 块，残块保留
    if skip == 0:
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data[:whole]) + data[whole:]
    # skip>0：pattern 逐组
    aes = AES.new(key, AES.MODE_ECB)           # 单块解密 D(C)
    out = bytearray(data)
    group = crypt + skip
    nblock = whole // 16
    blk = 0
    while blk < nblock:
        # 每个 group 从 iv 重新起链（组间有明文块隔断，无密文可链）
        chain = iv
        for _ in range(crypt):
            if blk >= nblock:
                break
            pos = blk * 16
            c = bytes(out[pos:pos + 16])
            p = bytes(x ^ y for x, y in zip(aes.decrypt(c), chain))
            out[pos:pos + 16] = p
            chain = c                           # CBC：下块 IV = 本块密文
            blk += 1
        blk += skip                             # 明文块直接跳
    return bytes(out)


def decrypt_cbcs_subsamples(sample: bytes, key: bytes, iv: bytes, subsamples: list, *,
                            crypt_byte_blocks: int = 1,
                            skip_byte_blocks: int = 9) -> bytes:
    """带 subsample（senc）的 cbcs 样本解密：按 [(clear_len, enc_len), ...] 跳过明文区、
    仅对 enc 区解密，链/pattern 每 subsample 独立复位（IV=constant iv）。返回整样本明文。"""
    out = bytearray(sample)
    off = 0
    for clear_len, enc_len in subsamples:
        off += int(clear_len or 0)
        end = off + int(enc_len or 0)
        if enc_len:
            out[off:end] = decrypt_cbcs_range(bytes(out[off:end]), key, iv,
                                              crypt_byte_blocks=crypt_byte_blocks,
                                              skip_byte_blocks=skip_byte_blocks)
        off = end
    return bytes(out)


def decrypt_segment(data: bytes, enc: dict, *, seq: int = 0, key: bytes = None,
                    referer: str = "", cookies: dict = None,
                    fetch=None) -> bytes:
    """按 enc 解密单个分片字节。key 缺省时按 enc.uri 现取（resolve_aes_key）。
    method AES-128 → AES-128-CBC + PKCS7 去填充；其余 → UnsupportedScheme。"""
    if not enc or not enc.get("method") or enc["method"] in ("NONE", ""):
        return data
    method = enc["method"]
    if method == "AES-128":
        if key is None:
            key = resolve_aes_key(enc, referer=referer, cookies=cookies, fetch=fetch)
        if len(key) not in (16,):
            raise DecryptError("AES-128 key 长度须 16 字节")
        iv = segment_iv(enc, seq)
        try:
            from Crypto.Cipher import AES
        except ImportError as e:  # pragma: no cover
            raise DecryptError("缺少 pycryptodome：%s" % e)
        if len(data) % 16 != 0:
            raise DecryptError("AES 分片长度非 16 的倍数（可能未整片加密或已损坏）")
        out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        return _strip_pkcs7(out)
    if method in ("SAMPLE-AES", "SAMPLE-AES-CTR"):
        # 引擎没有 cbcs 的容器级(fMP4 senc/tenc + TS 载荷)解析，无法仅凭 enc 解密分片。
        # cbcs 密码学原语已落地：站点适配器解析出样本的 (key, constant IV, subsample 表) 后可调
        # decrypt_cbcs_subsamples / decrypt_cbcs_range 自行解密（真站样本待手测验证）。
        raise UnsupportedScheme(
            "HLS %s(cbcs 族)：容器级样本解析未实现；原语 decrypt_cbcs_subsamples/decrypt_cbcs_range "
            "可用于适配器拿到 (key,IV,subsample表) 后自解（真站 cbcs 待真实样本验证）" % method)
    if method in ("AES-256", "AES-256-CTR"):
        raise UnsupportedScheme("HLS %s 暂不支持" % method)
    raise UnsupportedScheme("HLS 加密方式 %s 不支持" % method)


def resolve_aes_key(enc: dict, *, referer: str = "", cookies: dict = None,
                    fetch=None, use_cache: bool = True) -> bytes:
    """按 enc.uri 拉取 AES key。支持 raw/hex/base64/json 载体；带头(referer/cookie)。
    key 缓存键含 cookie 指纹，防跨鉴权上下文串 key。"""
    if not enc or not enc.get("uri"):
        raise DecryptError("AES key URI 缺失")
    fetch = fetch or DEFAULT_FETCH
    uri = enc["uri"]
    cookie_fp = ";".join("%s=%s" % kv for kv in sorted((cookies or {}).items()))
    ck = _cache_key(uri, referer, cookie_fp)
    if use_cache:
        with _KEY_CACHE_LOCK:
            if ck in _KEY_CACHE:
                return _KEY_CACHE[ck]
    body = fetch(uri, referer=referer, cookies=cookies, min_len=1)
    key = coerce_key(body)
    if use_cache:
        with _KEY_CACHE_LOCK:
            _KEY_CACHE[ck] = key
    return key


# ================================================================
# D1: DASH CENC / ClearKey —— 委托随包 ffmpeg 原生解密（实证：cenc-aes-ctr 加密/解密均可用）
# key 属"ClearKey/站点下发"（可解族）；真 DRM(带商业 systemId) 由 mpd_clear_key_candidate 拦在外面。
# ================================================================
def locate_ffmpeg() -> str:
    """定位 ffmpeg：FFMPEG_BIN 环境变量 → PATH → 随包(借 video_grab._locate_ffmpeg)。找不到抛错。"""
    env = os.environ.get("FFMPEG_BIN")
    if env and os.path.isfile(env):
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import video_grab as vg
        p = vg._locate_ffmpeg()
        if p:
            return p
    except Exception:
        pass
    raise DecryptError("未找到 ffmpeg（FFMPEG_BIN 或随包 runtime/ffmpeg）")


def _to_key_hex(key) -> str:
    if isinstance(key, (bytes, bytearray)):
        if len(key) != 16:
            raise DecryptError("CENC key 须 16 字节")
        return bytes(key).hex()
    s = str(key).strip()
    if len(s) == 32:
        return s
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) != 32:
        raise DecryptError("CENC key hex 须 32 字符")
    return s


def _run_ff(args):
    r = subprocess.run(args, capture_output=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or b"").decode("utf-8", "ignore")[-700:]
        raise DecryptError("ffmpeg rc=%d:\n%s" % (r.returncode, tail))
    return r


def decrypt_cenc_media(media: bytes, key, *, ffmpeg: str = None) -> bytes:
    """解密单个 CENC 加密的 fMP4（可含 moov/moof+mdat 多片）。key 为 ClearKey/站点下发。
    委托 ffmpeg -decryption_key（native cenc-aes-ctr），返回解密后完整 mp4 字节。失败 raise。"""
    if not media or len(media) < 12:
        raise DecryptError("CENC 输入字节过短")
    keyhex = _to_key_hex(key)
    ff = ffmpeg or locate_ffmpeg()
    tmp = tempfile.mkdtemp(prefix="sd_cenc_")
    try:
        inp = os.path.join(tmp, "in.mp4")
        out = os.path.join(tmp, "out.mp4")
        with open(inp, "wb") as f:
            f.write(media)
        _run_ff([ff, "-hide_banner", "-loglevel", "error", "-y",
                 "-decryption_key", keyhex, "-i", inp, "-c", "copy", out])
        if not os.path.isfile(out) or os.path.getsize(out) <= 0:
            raise DecryptError("CENC 解密产物为空")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def decrypt_cenc_fragments(fragments: list, key, *, ffmpeg: str = None) -> bytes:
    """解密分段 CENC(DASH: init + 多个 media moof/mdat)。按序拼接成单一 fragmented mp4 再委托 ffmpeg。
    fragments[0] 应为 init(moov/mvex)，其后为各 media 段。"""
    if not fragments:
        raise DecryptError("CENC 分片列表为空")
    blob = b"".join(fragments)
    return decrypt_cenc_media(blob, key, ffmpeg=ffmpeg)
