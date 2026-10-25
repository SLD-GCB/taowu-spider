# -*- coding: utf-8 -*-
"""
schema_engine.py — 数据表模板驱动的通用字段抽取器（方案二）。

纯函数、无 IO、无浏览器、无任何对抗逻辑；输入是“已拿到的清洗/标记文本”，
输出是按模板 columns（列名 + 同义词 + clean 类型）给值的 {列名: 值} 字典。

两段式：
  Pass A  结构对位：把文本扫成 {标签: 值}（兼容 “标签：值” 与 zhujian “【标签】值”）
  Pass B  列名/同义词查表：每列按 [列名]+[同义词] 顺序认标签取值；取不到再走
          clean 类型的“形态兜底”(¥/日期/电话/链接等)；仍无 → 空串（落库空列，不报错）。

招聘模板 engine=legacy_recruit 不进入本模块（走原 rule_extract），此文件零影响旧链路。
"""

import re

# ─────────────────────────────── 通用文本工具 ───────────────────────────────
_WS = re.compile(r"[ \t　]+")
_NL = re.compile(r"\n{3,}")


def _one(v: str) -> str:
    v = _NL.sub("\n", str(v or "")).strip()
    return _WS.sub(" ", v).strip()


def _keep_long(v: str) -> str:
    v = _NL.sub("\n\n", str(v or "")).strip()
    lines = [ln.strip() for ln in v.splitlines() if ln.strip()]
    return "\n".join(lines)[:8000]


# ─────────────────────────────── Pass A：标签→值 对位 ───────────────────────────────
_MARKER = re.compile(r"【([^】\n]{1,40})】\s*([^\n【]*)")
_COLON = re.compile(r"(?m)^\s*([^：:\s【】\d][^：:\n【】]{0,24}?)[：:]\s*(.+)$")


def _scan_pairs(text: str):
    """返回 {标签: 首个非空值}。容忍两种形态、保留首次出现。"""
    pairs = {}
    for m in _MARKER.finditer(text):
        lab = m.group(1).strip()
        val = m.group(2).strip()
        if lab and val and lab not in pairs:
            pairs[lab] = val
    for m in _COLON.finditer(text):
        lab = m.group(1).strip()
        val = m.group(2).strip()
        low = lab.lower()
        if (not lab) or (not val) or low.startswith("http") or low.startswith("www"):
            continue
        if lab in pairs:
            continue
        pairs[lab] = val
    return pairs


# ─────────────────────────────── 形态兜底（clean 开关驱动，纯数据非代码） ───────────────────────────────
_RE_MONEY = [
    re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"),
    re.compile(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:万元|元|万|人民币|RMB)", re.I),
]
_RE_DT = re.compile(r"(20\d{2})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})(?:日)?(?:\s+(\d{1,2})[:：](\d{1,2}))?")
_RE_PHONE = re.compile(r"(?:1[3-9]\d{9}|0\d{2,3}[-]?\d{7,8})")
_RE_LINK = re.compile(r"https?://[^\s　，。；;\"'<>（）()]+")
_RE_INT = re.compile(r"[0-9,]+")
_TEXT_FALLBACK_LIMIT = 3000


def _fallback(text: str, clean: str) -> str:
    if not text:
        return ""
    head = text[:_TEXT_FALLBACK_LIMIT]
    if clean == "money":
        for rx in _RE_MONEY:
            m = rx.search(head)
            if m:
                return m.group(1).replace(",", "")
        return ""
    if clean == "datetime":
        m = _RE_DT.search(head)
        if not m:
            return ""
        base = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if m.group(4):
            base += f" {m.group(4)}:{m.group(5)}"
        return base
    if clean == "phone":
        m = _RE_PHONE.search(head)
        return m.group(0) if m else ""
    if clean == "link":
        m = _RE_LINK.search(text)
        return m.group(0) if m else ""
    if clean == "int":
        m = _RE_INT.search(head)
        return m.group(0).replace(",", "") if m else ""
    return ""


def _fmt_dt(m) -> str:
    base = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if m.group(4):
        base += f" {m.group(4)}:{m.group(5)}"
    return base


def _clean_value(v, name, cleaner: str) -> str:
    if v is None:
        return ""
    v = str(v).strip()
    if not v:
        return ""
    if v == name:
        return ""
    if cleaner == "longtext":
        return _keep_long(v)
    v = _one(v)
    # 类型列即使由标签命中，也做规范化抽取（去掉前缀/后缀噪声），money 保留原文（单位有价值）
    if cleaner == "phone":
        m = _RE_PHONE.search(v)
        return m.group(0) if m else v
    if cleaner == "datetime":
        m = _RE_DT.search(v)
        return _fmt_dt(m) if m else v
    if cleaner == "link":
        m = _RE_LINK.search(v)
        return m.group(0) if m else v
    if cleaner == "int":
        m = _RE_INT.search(v)
        return m.group(0).replace(",", "") if m else v
    return v


# ─────────────────── 可选列正则（col["re"]：纯数据，str 或 str 列表） ───────────────────
# 语义：标签/synonyms 与 title 都未命中时，按本列正则在全文认“无标签裸值”
# （电商销量/店铺、各类编号等）。置于 clean 形态兜底之前——re 比泛扫更精确，
# 避免 money 泛扫抓成页面上别处的数字。坏正则不崩链路，该列回退空串。
_RE_CACHE = {}


def _compile_re(pattern: str):
    rx = _RE_CACHE.get(pattern)
    if rx is None:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = False  # 坏正则：缓存为 False，后续直接跳过
        _RE_CACHE[pattern] = rx
    return rx


def _regex_find(text: str, patterns: list) -> str:
    """依序尝试每个正则；取第 1 个捕获组（有值用值），否则取整段匹配。"""
    for p in patterns:
        rx = _compile_re(p)
        if not rx:
            continue
        m = rx.search(text)
        if not m:
            continue
        g = m.group(1)
        if g is not None and g.strip():
            return g.strip()
        return m.group(0).strip()
    return ""


# ─────────────────────────────── Pass B：列名/同义词查表 ───────────────────────────────
def schema_extract(text, columns, ctx=None):
    """
    columns: [ {name, type?, synonyms?, clean?}, ... ]（含 job_url/source 等系统列）
    ctx:     {url, site} 可选，用于回填 job_url/source
    返回 {列名: 值}。列名与表列名一致，落库 insert 按表列反查即可命中。
    """
    text = str(text or "")
    pairs = _scan_pairs(text) if text else {}
    result = {}
    for c in columns or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        syn = list(c.get("synonyms") or [])
        cleaner = str(c.get("clean") or "text").lower()
        re_pats = c.get("re") or []
        if isinstance(re_pats, str):
            re_pats = [re_pats]
        re_pats = [p for p in re_pats if p]
        val = ""
        for lab in [name] + syn:
            hit = pairs.get(lab)
            if hit and hit.strip():
                val = hit
                break
        # clean:"title" 兜底：许多站点“型号/名称”只出现在无标签的大标题(h1/标题)里，
        # 文本里没有“标签：值”可认。由 ctx["titles"]（页面主标题候选，flow 端从 DOM 取）补位。
        if not val and cleaner == "title":
            for t in ((ctx or {}).get("titles")) or []:
                if str(t or "").strip():
                    val = str(t).strip()
                    break
        # col["re"]：标签/title 未命中时按本列正则兜底（无标签裸值），先于 clean 形态泛扫。
        if not val and re_pats:
            val = _regex_find(text, re_pats)
        if not val:
            val = _fallback(text, cleaner)
        if not val and name in ("job_url", "source") and ctx:
            val = ctx.get("url") if name == "job_url" else (ctx.get("site") or "")
        result[name] = _clean_value(val, name, cleaner)
    return result
