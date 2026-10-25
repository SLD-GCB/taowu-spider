# task_identity.py — L0 任务身份：一次抓取 = 一个(浏览器指纹/TLS档/UA/语言/cookie域)整体。
# 解决"页面态(Chrome) 与 媒体态(requests/curl 下载)"身份不一致的最大结构性弱点：
# 让清单/分片/key 的 UA+Accept-Language 与打开页面的浏览器完全同源（§3.1/§5.1）。
#
# 用法：把一次抓取包进 `use(identity)`，其内所有 _media_headers 组装会自动吃该身份；
# 外层(无任务身份)完全维持旧的随机头行为 —— 零回归、只在需要时一致。
# identity 生产：Chrome 打开页面后 capture_from_driver（真实值）；或 config 预设档/纯值构造。
# 纯模块：不 import vg/GUI/audio；离线可测（use + media_header 断言）。
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

# 参考值（DEFAULT 只做缺省 UA 一致性底，真实任务应从 Chrome 捕获覆盖）
_REF_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 支持的 impersonate 档位（curl_cffi）。真实任务按捕获到的 UA 主版本映射；这里仅存槽名，
# 档位实际落到 host 的取流选择（_impersonate_get / register_impersonate_host 缝）由 vg 决策。
_SUPPORTED_IMP = ("chrome99", "chrome110", "chrome116", "chrome120",
                  "chrome124", "chrome131", "safari17_0")

# UA 主版本 → 就近档位（本地镜像，仅供 stealth_profile 缺载时的兜底；正常优先委托 nearest_imp）
_UA_MAJOR_IMP = (("131", "chrome131"), ("124", "chrome124"), ("120", "chrome120"),
                 ("116", "chrome116"), ("110", "chrome110"), ("99", "chrome99"))


def imp_slot_for_ua(ua: str) -> str:
    """由 UA 解析就近 impersonate 档：Chrome→≤主版本最大已知档；Edg→同主版本 chrome；
    Safari→safari17_0；未知→chrome120。语义与 stealth_profile.nearest_imp 同源 —— 优先委托其
    （唯一实现源，4e 断言二者一致）；本模块缺载时回退下方本地镜像。"""
    try:
        import stealth_profile as _sp
        return _sp.nearest_imp(ua)
    except Exception:
        pass
    import re
    s = ua or ""
    if re.search(r"Safari/", s) and not re.search(r"Chrome/|Edg/", s):
        return "safari17_0"
    if not re.search(r"Chrome/|Edg/", s):
        return "chrome120"
    m = re.search(r"Chrome/(\d+)\.", s)
    major = int(m.group(1)) if m else 0
    for above, slot in _UA_MAJOR_IMP:          # 表为降序：首个 major<=当前 即最接近
        if major >= int(above):
            return slot
    return "chrome99"


@dataclass
class Identity:
    """一次抓取持用的完整浏览器身份。headers 不在此固化（页/媒体/KEY 的 Accept 各不同），
    UA 与 Accept-Language 等"身份锚点"贯穿各态；Referer/Sec-Fetch 按请求用途现组。"""
    ua: str = _REF_UA
    accept_lang: str = "zh-CN,zh;q=0.9,en;q=0.8"
    tz: str = "Asia/Shanghai"
    platform: str = "Win32"
    screen: str = "1920,1080"
    color_depth: int = 24
    device_memory: int = 8
    imp_slot: str = "chrome124"
    profile_key: str = ""            # 可选：该身份 UA 来自 stealth_profile 的哪档（站点覆盖/每任务档的落点标记；
                                     #   浏览器捕获身份留空——捕获 ua 即真值，下载侧以 ua 为锚派生 Profile）
    cookie_domains: dict = field(default_factory=dict)   # {host: {name:value}} 随任务携带的登录域
    note: str = ""

    def media_headers(self, referer: str = "") -> dict:
        """媒体态统一请求头：以身份锚点为基准（UA/Accept-Language 同源），
        Accept/Sec-Fetch 由用途覆盖；Referer 防盗链现组。"""
        h = {
            "User-Agent": self.ua,
            "Accept-Language": self.accept_lang,
        }
        if referer:
            h["Referer"] = referer
        return h


# ---------------------------------------------------------------- ambient
_ACTIVE: Optional[Identity] = None
_LOCK = threading.RLock()


def ambient() -> Optional[Identity]:
    """当前任务的 ambient 身份；无任务时为 None（消费方走旧随机行为）。"""
    with _LOCK:
        return _ACTIVE


@contextmanager
def use(identity: Optional[Identity]):
    """把一次抓取包进该身份上下文：线程安全地设置/恢复 ambient（进程级，跨分片线程生效）。"""
    global _ACTIVE
    with _LOCK:
        prev = _ACTIVE
        _ACTIVE = identity
    try:
        yield identity
    finally:
        with _LOCK:
            _ACTIVE = prev


# ---------------------------------------------------------------- 生产
def from_values(ua: str = None, *, accept_lang: str = None, tz: str = None,
                platform: str = None, screen: str = None, color_depth: int = None,
                device_memory: int = None, imp_slot: str = None,
                profile_key: str = "", cookie_domains: dict = None,
                note: str = "") -> Identity:
    """由捕获到的浏览器值构造 Identity（缺省走参考底）。UA 非空时自动派生 imp_slot。
    profile_key 可选：标记该身份 UA 的来源档（站点覆盖/每任务合成档用；浏览器捕获留空）。"""
    ua = ua or _REF_UA
    imp = imp_slot or imp_slot_for_ua(ua)
    return Identity(ua=ua, accept_lang=accept_lang or "zh-CN,zh;q=0.9,en;q=0.8",
                    tz=tz or "Asia/Shanghai", platform=platform or "Win32",
                    screen=screen or "1920,1080", color_depth=color_depth or 24,
                    device_memory=device_memory or 8, imp_slot=imp,
                    profile_key=profile_key or "",
                    cookie_domains=dict(cookie_domains or {}), note=note)


_CAPTURE_JS = (
    "(()=>{try{const s=window.screen;"
    "return {ua:navigator.userAgent, lang:navigator.language||'',"
    "tz:Intl.DateTimeFormat().resolvedOptions().timeZone||'',"
    "platform:navigator.platform||'', sw:s.width||0, sh:s.height||0,"
    "sd:s.colorDepth||24, mem:navigator.deviceMemory||4};}"
    "catch(e){return null;}})()")


def capture_from_driver(driver, *, cookie_domains: dict = None,
                        note: str = "浏览器捕获") -> Optional[Identity]:
    """在真实 Chrome 上下文取身份值（UA/语言/时区/平台/屏幕/内存）→ Identity。
    读不到或失败返回 None（调用方无需捕获时静默降级，不改变行为）。"""
    try:
        v = driver.execute_script(_CAPTURE_JS)
    except Exception:
        return None
    if not v or not isinstance(v, dict) or not v.get("ua"):
        return None
    scr = "%s,%s" % (v.get("sw") or 0, v.get("sh") or 0)
    return from_values(
        v.get("ua"), accept_lang=v.get("lang") or None,
        tz=v.get("tz") or None, platform=v.get("platform") or None,
        screen=scr if v.get("sw") else None,
        color_depth=int(v.get("sd") or 24), device_memory=int(v.get("mem") or 4),
        cookie_domains=cookie_domains, note=note)


def ua() -> str:
    """消费缝 helper：当前身份的 UA；无任务返回 None（调用方再回退随机 UA）。"""
    a = ambient()
    return a.ua if a else None


def accept_language() -> str:
    a = ambient()
    return a.accept_lang if a else None
