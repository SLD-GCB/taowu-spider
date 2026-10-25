# -*- coding: utf-8 -*-
"""本地视频播放 · 纯后端（video_player.py）

只播**已下载**的本地库文件（抓取链路合规公开源的产物）：
视频扫 config.VIDEO_DIR、音频扫 config.AUDIO_DIR（音/视频由「格式」下拉框切换，
见 FormatGroup）。不做远程 m3u8、不 import video_grab（4600 行抓取大模块）。

本模块约定：任何 import 期都**不碰 mpv / PySide6**——mpv 只在
bootstrap_mpv / mpv_available / decodecheck 里被显式 import（qt_player 同理）。

**格式清单单一真源**：扩展名清单是纯数据，住在 config（VIDEO_EXTS / AUDIO_OUT_EXTS）；
本模块只做派生（_with_dots、MEDIA_EXTS、FORMAT_GROUPS）。方向被 import 约束死——
本文件 import config，所以 config 绝不能反过来 import 本模块，任何"派生出来的
字符串"都不可能住在 config 里；config.FILE_FILTER_VIDEO 当年就是因为只能写成
字面量才与 MEDIA_EXTS 分叉（漏了 .wmv）。selftest-formats 是防再分叉的闸门。

职责：
  * 扫库：scan_library(root, exts) -> Library（Episode/Series，自然序、滤无媒体目录）
  * 格式组：FORMAT_GROUPS / default_group / find_group / group_filter（下拉框 + 文件对话框）
  * 握手：locate_mpv_dll / bootstrap_mpv / mpv_available（UI 缺 dll 前探测，不崩）
  * CLI：python video_player.py selftest-scan [root] [--exts .mp4|all]
         python video_player.py decodecheck <file>      # vo/ao=null 真 demux 验证
         python video_player.py selftest-formats        # 格式清单一致性自检（纯数据）
"""
import ctypes
import locale
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import config

# ── 格式闸门（**唯一真源是 config 的格式清单**，此处只做派生）──────────────
def _with_dots(seq):
    """扩展名归一化：'mp3' → '.mp3'；小写、去空、去重保序。
    config 的格式清单有不带点的传统（AUDIO_OUT_EXTS），故统一在此补点。"""
    out = []
    for e in seq or ():
        s = str(e).strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = "." + s
        if s not in out:
            out.append(s)
    return tuple(out)


# 视频闸门（含抓取产物 .ts/.mp4 及本地常见容器）；名字保留，全项目引用点不改
MEDIA_EXTS = frozenset(_with_dots(config.VIDEO_EXTS))
# 音频闸门：复用 audio_grab 的真实产物清单，不另立第三份清单
AUDIO_MEDIA_EXTS = frozenset(_with_dots(config.AUDIO_OUT_EXTS))
ANY_MEDIA_EXTS = MEDIA_EXTS | AUDIO_MEDIA_EXTS


@dataclass(frozen=True)
class FormatGroup:
    """播放器「格式」下拉框的一项 = 一个具体媒体格式 + 它的扫描根。

    没有"全部格式"聚合项：**选中项就是过滤条件**——左树只列该格式的文件，
    不匹配的一律不出现（用户定的交互）。
    视频格式扫 config.VIDEO_DIR、音频格式扫 config.AUDIO_DIR，所以切换格式
    等同于切换"我在浏览哪个库"（音频不在视频库里，不这样就得不到任何结果）。"""
    key: str         # 稳定标识（"视频:.mp4"），供会话内记忆上次选择
    label: str       # 下拉显示文案
    exts: frozenset  # 该格式的扩展名白名单（单元素集）
    root: str        # 扫描根


def _build_format_groups():
    """按 config 清单的次序铺平：先全部视频格式，再全部音频格式。"""
    out = []
    for kind, exts, root in (
        (config.FMT_KIND_VIDEO, _with_dots(config.VIDEO_EXTS), config.VIDEO_DIR),
        (config.FMT_KIND_AUDIO, _with_dots(config.AUDIO_OUT_EXTS), config.AUDIO_DIR),
    ):
        for ext in exts:
            out.append(FormatGroup(
                key="%s:%s" % (kind, ext),
                label=config.ITEM_PLAYER_FMT_ONE.format(kind=kind, ext=ext),
                exts=frozenset({ext}),
                root=root))
    return tuple(out)


FORMAT_GROUPS = _build_format_groups()


def default_group() -> FormatGroup:
    """默认选中项 = 清单第 1 项（config.VIDEO_EXTS[0]，当前是 .mp4）。"""
    return FORMAT_GROUPS[0]


def find_group(key):
    """按 key 找格式组；找不到 → None（供会话内记忆恢复用）。"""
    for g in FORMAT_GROUPS:
        if g.key == key:
            return g
    return None


def group_filter(grp: FormatGroup) -> str:
    """格式组 → QFileDialog 过滤器字符串。

    尾段固定留「所有文件」逃生口：手挑一个当前格式之外的文件（仍要过
    ANY_MEDIA_EXTS 校验），这是最常用的一次性越界需求。"""
    exts = " ".join("*" + e for e in sorted(grp.exts))
    return "%s;;%s" % (config.FILE_FILTER_MEDIA_TMPL.format(exts=exts),
                       config.FILE_FILTER_ALL)

# 集数识别：中文「第NN集/話/话」优先，其次 EP/NN，最后 E-NN（防尾部普通数字误判）
_RE_EP_CN = re.compile(r"第\s*0*(\d{1,6})\s*[集話话]")
_RE_EP_EN = re.compile(r"(?<![0-9A-Za-z])[Ee][Pp]\.?\s*0*(\d{1,4})(?![0-9])")
_RE_EP_E = re.compile(r"(?<![0-9A-Za-z])[Ee]0*(\d{1,3})(?![0-9])")
_EP_PATTERNS = (_RE_EP_CN, _RE_EP_EN, _RE_EP_E)


# ── 数据模型 ────────────────────────────────────────────────
@dataclass(frozen=True)
class Episode:
    """一个可播放媒体文件。index=None 表示识别不出集数。"""
    path: str             # 绝对路径
    name: str             # 文件名（含扩展名，作显示用）
    index: Optional[int]  # 集数；None = 未识别
    ext: str              # 小写扩展名（含点，如 .ts）
    size: int             # 字节；stat 失败 = -1

    @property
    def label(self) -> str:
        """树/状态栏显示名（去扩展名；空则回退全名）"""
        return os.path.splitext(self.name)[0] or self.name


@dataclass
class Series:
    """一部动漫 = 一个目录 + 其内媒体文件（已按集数自然序排好）。"""
    title: str
    dir_path: str
    episodes: List[Episode] = field(default_factory=list)


@dataclass
class Library:
    root: str                                   # 被扫目录（=config.VIDEO_DIR）
    series: List[Series] = field(default_factory=list)   # 一层目录（含媒体才算）
    loose: List[Episode] = field(default_factory=list)   # 根目录散文件

    @property
    def total_episodes(self) -> int:
        return sum(len(s.episodes) for s in self.series) + len(self.loose)


# ── 集数 / 自然排序 ─────────────────────────────────────────
def parse_ep_index(name: str) -> Optional[int]:
    """从文件名提出集数：'第01集.ts'→1、'第 3 集.mp4'→3、'EP04.mp4'→4、无→None。"""
    base = os.path.splitext(os.path.basename(name))[0]
    for rx in _EP_PATTERNS:
        m = rx.search(base)
        if m:
            return int(m.group(1))
    return None


def _natural_key(s: str):
    """'第10集' 排在 '第2集' 之后：数字段按 int、其余小写。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _ep_sort_key(ep: Episode):
    """有集数的按集数排最前；无集数的按自然序排后。"""
    if ep.index is not None:
        return (0, ep.index, ep.name.lower())
    return (1, 0, _natural_key(ep.name))


def _sort_episodes(eps: List[Episode]) -> List[Episode]:
    return sorted(eps, key=_ep_sort_key)


def _file_episode(path: str, exts=MEDIA_EXTS) -> Optional[Episode]:
    """单个文件 → Episode；扩展名不在 exts 白名单 → None。

    exts 为 frozenset 白名单；**显式传 None 才表示"不看扩展名"**（放行任意文件）；
    不传参 = MEDIA_EXTS（视频），与加 exts 参数之前的行为一字不差。"""
    ext = os.path.splitext(path)[1].lower()
    if exts is not None and ext not in exts:
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        size = -1
    return Episode(path=path, name=os.path.basename(path),
                   index=parse_ep_index(os.path.basename(path)),
                   ext=ext, size=size)


def scan_library(root: Optional[str] = None, exts=MEDIA_EXTS) -> Library:
    """扫已下载库：
    - 一层子目录：内含**匹配 exts** 的媒体才算 Series（纯 .json/无媒体目录不出现）；
    - 根目录散媒体归 loose；
    - 目录名/文件名以 '.' 开头跳过；stat 失败 size=-1 不中断。
    - exts = 扩展名白名单（默认 MEDIA_EXTS 即视频）；传 None 表示不看扩展名。
    返回的 series/loose 均已按集数自然序排好。"""
    root = os.path.abspath(root or config.VIDEO_DIR)
    lib = Library(root=root)
    if not os.path.isdir(root):
        return lib
    try:
        entries = sorted(os.listdir(root), key=_natural_key)
    except OSError:
        return lib

    for ent in entries:
        if ent.startswith("."):
            continue
        p = os.path.join(root, ent)
        if os.path.isdir(p):
            eps = []
            try:
                sub = sorted(os.listdir(p), key=_natural_key)
            except OSError:
                continue
            for f in sub:
                if f.startswith("."):
                    continue
                ep = _file_episode(os.path.join(p, f), exts)
                if ep:
                    eps.append(ep)
            if eps:  # 目录内确有匹配媒体才算一部动漫
                lib.series.append(Series(title=ent, dir_path=p,
                                         episodes=_sort_episodes(eps)))
        else:
            ep = _file_episode(p, exts)
            if ep:
                lib.loose.append(ep)

    lib.series.sort(key=lambda s: _natural_key(s.title))
    lib.loose = _sort_episodes(lib.loose)
    return lib


def _fmt_size(n) -> str:
    """字节数 → '3.2 MB'；未知=-1 → '?'。"""
    if n is None or n < 0:
        return "?"
    if n < 1024:
        return "%d B" % n
    v = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        v /= 1024.0
        if v < 1024:
            return "%.1f %s" % (v, unit)
    return "%.1f PB" % v


# ── libmpv 握手（只在被调用时才 import mpv）─────────────────
_MPV_PATH_READY = False  # runtime/mpv 已入 DLL 搜索路径（幂等）


def locate_mpv_dll() -> Optional[str]:
    """返回 libmpv-2.dll 绝对路径；不在 → None。"""
    p = config.MPV_DLL_PATH
    return p if os.path.isfile(p) else None


def _prepare_mpv_search_path() -> None:
    """把 runtime/mpv 挂上 DLL 搜索路径。
    python-mpv 在 import 期用 ctypes.util.find_library 找 dll，它只搜 PATH、不看
    已加载模块 → 必须在 import mpv 之前把 dll 目录放上 PATH。依赖 dll 由 Windows
    DLL_LOAD_DIR（CDLL 传 LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR）自动从同目录解析。"""
    global _MPV_PATH_READY
    if _MPV_PATH_READY:
        return
    d = os.path.dirname(config.MPV_DLL_PATH)
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    _MPV_PATH_READY = True


def bootstrap_mpv():
    """返回已导入的 python-mpv 模块（调用方自建/销毁 MPV() 实例）。
    缺 dll / dll 加载失败 / import 失败 → 抛中文 RuntimeError，绝不静默。"""
    dll = locate_mpv_dll()
    if not dll:
        raise RuntimeError("未找到 %s（%s）——请放入 LGPL 版 %s 及其同层依赖 dll"
                           % (config.MPV_DLL_NAME, config.MPV_DIR, config.MPV_DLL_NAME))
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")  # mpv 内部 printf 对 locale 敏感
    except locale.Error:
        pass
    _prepare_mpv_search_path()
    try:
        ctypes.CDLL(dll)  # 绝对路径预载：同 base name 已加载 → 后续 CDLL 命中句柄
    except OSError as e:
        raise RuntimeError("加载 %s 失败：%s" % (config.MPV_DLL_NAME, e))
    try:
        import mpv as _mpvmod
    except Exception as e:  # 缺 VC 运行库 / 位数不符等都会在此暴露
        raise RuntimeError("python-mpv 导入失败：%s" % e)
    return _mpvmod


def mpv_available():
    """真握手探测：能否建一个 vo/ao=null 的 MPV 上下文（不碰窗口）。
    返回 (ok: bool, info: str)：ok=False 时 info 为中文原因，可直接弹给用户。"""
    if not locate_mpv_dll():
        return False, ("未找到 {name}（{dir}）——请把 LGPL 版 {name}（连同同层依赖 dll）"
                       "放入该目录后重试。"
                       .format(name=config.MPV_DLL_NAME, dir=config.MPV_DIR))
    try:
        m = bootstrap_mpv()
        p = m.MPV(vo="null", ao="null", config="no", osc="no",
                  keep_open="yes", input_default_bindings="no")
        try:
            try:
                _ = p.mpv_version  # 构造即 initialize；版本可读即上下文健康
            except Exception:
                pass
        finally:
            try:
                p.terminate()
            except Exception:
                pass
    except Exception as e:
        return False, str(e)
    return True, locate_mpv_dll()


# ── CLI 子命令 ──────────────────────────────────────────────
def _parse_exts_arg(s: str):
    """--exts 取值：'all' → 全部受支持音视频格式；否则逗号/空格分隔的扩展名。
    （不支持"含非媒体文件"——那需要 exts=None，且会把 .json 元数据也列出来。）"""
    s = (s or "").strip()
    if s.lower() == "all":
        return ANY_MEDIA_EXTS
    return frozenset(_with_dots(s.replace(",", " ").split())) or MEDIA_EXTS


def _cmd_selftest(root: Optional[str], exts=None) -> int:
    # 不给 --exts 时走 MEDIA_EXTS，输出与加该参数之前**逐字节一致**（有回归价值）
    lib = scan_library(root, MEDIA_EXTS if exts is None else exts)
    print("root:  %s" % lib.root)
    print("series=%d  loose=%d  total_episodes=%d"
          % (len(lib.series), len(lib.loose), lib.total_episodes))
    for s in lib.series:
        idxs = [ep.index for ep in s.episodes]
        print("  [series] %s  eps=%d  indexes=%s" % (s.title, len(s.episodes), idxs))
        for ep in s.episodes:
            print("      %s  idx=%s  %s  %s"
                  % (ep.name, ep.index, _fmt_size(ep.size), ep.path))
    print("  [loose]")
    for ep in lib.loose:
        print("      %s  idx=%s  %s  %s"
              % (ep.name, ep.index, _fmt_size(ep.size), ep.path))
    print("SELFTEST_SCAN_OK" if lib.series or lib.loose else "SELFTEST_SCAN_EMPTY")
    return 0


def _cmd_decodecheck(path: str) -> int:
    """vo=null/ao=null 真 demux：能读到 duration>0 即 dll+真实流解码通。"""
    if not os.path.isfile(path):
        print("DECODECHECK_FAIL=no-file %s" % path)
        return 2
    try:
        m = bootstrap_mpv()
    except RuntimeError as e:
        print("DECODECHECK_FAIL=bootstrap %s" % e)
        return 3
    p = None
    try:
        p = m.MPV(vo="null", ao="null", config="no", osc="no",
                  keep_open="yes", input_default_bindings="no")
        p.play(path)
        deadline = time.time() + 25.0
        dur = w = h = fmt = None
        while time.time() < deadline:
            try:
                dur = p.duration
                w = p.width
                h = p.height
                fmt = p.video_format
            except Exception:
                pass
            if dur and dur > 0 and w and w > 0:
                break
            time.sleep(0.2)
        print("duration=%r width=%r height=%r video_format=%r" % (dur, w, h, fmt))
        if not (dur and dur > 0):
            print("DECODECHECK_FAIL=no-duration")
            return 3
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        print("DECODECHECK_OK bytes=%d duration=%.2fs size=%dx%d"
              % (size, float(dur), w or 0, h or 0))
        return 0
    except Exception as e:
        print("DECODECHECK_FAIL=play %r" % repr(e)[:200])
        return 4
    finally:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass


def _cmd_formats() -> int:
    """纯数据自检（无 Qt / 无 mpv / 无 GUI）：格式清单与下拉项的一致性闸门。

    存在的唯一理由：这套清单**历史上分叉过**——config.FILE_FILTER_VIDEO 曾是一份
    独立字面量，和 MEDIA_EXTS 分叉漏掉了 .wmv。下面每条断言都只查"关系"不查绝对值
    （不在这里再写第三份格式清单），专门用来防止再分叉一次。"""
    bad = []
    if MEDIA_EXTS != frozenset(_with_dots(config.VIDEO_EXTS)):
        bad.append("MEDIA_EXTS 与 config.VIDEO_EXTS 派生结果不符")
    if AUDIO_MEDIA_EXTS != frozenset(_with_dots(config.AUDIO_OUT_EXTS)):
        bad.append("AUDIO_MEDIA_EXTS 与 config.AUDIO_OUT_EXTS 派生结果不符")
    # 独立期望集合：上面所有断言都是**自洽性**检查，删掉一个扩展名照样全绿
    # （MEDIA_EXTS 与 FORMAT_GROUPS 同源，一起少一个，谁都不吭声）。这里写死
    # "应该有哪些"才拦得住静默丢格式——历史分叉就是 .wmv 被漏掉。
    # 若确实要增删格式：改这里 + config 对应清单，两处都动即视为有意为之。
    _want_v = frozenset(_with_dots(
        (".mp4", ".ts", ".mkv", ".flv", ".webm", ".mov", ".m4v", ".avi", ".wmv")))
    _want_a = frozenset(_with_dots(
        ("mp3", "m4a", "aac", "wav", "flac", "ogg")))
    if MEDIA_EXTS != _want_v:
        bad.append("视频格式集与期望不符：多=%s 少=%s"
                   % (sorted(MEDIA_EXTS - _want_v), sorted(_want_v - MEDIA_EXTS)))
    if AUDIO_MEDIA_EXTS != _want_a:
        bad.append("音频格式集与期望不符：多=%s 少=%s"
                   % (sorted(AUDIO_MEDIA_EXTS - _want_a),
                      sorted(_want_a - AUDIO_MEDIA_EXTS)))
    if ".wmv" not in MEDIA_EXTS:
        bad.append("MEDIA_EXTS 缺 .wmv（历史分叉点，当年就是它漏了）")
    if not FORMAT_GROUPS:
        bad.append("FORMAT_GROUPS 为空")
    keys = [g.key for g in FORMAT_GROUPS]
    if len(set(keys)) != len(keys):
        bad.append("FORMAT_GROUPS 有重复 key")
    # 下拉项必须恰好铺满两份清单，不多不少（多一个=幽灵格式，少一个=格式选不到）
    want = set(MEDIA_EXTS) | set(AUDIO_MEDIA_EXTS)
    got = set()
    for g in FORMAT_GROUPS:
        got |= set(g.exts)
    if got != want:
        bad.append("下拉项与格式清单不吻合：多=%s 少=%s"
                   % (sorted(got - want), sorted(want - got)))
    for g in FORMAT_GROUPS:
        if len(g.exts) != 1:
            bad.append("组 %s 不是单格式（%s）" % (g.key, sorted(g.exts)))
        if not (g.exts <= ANY_MEDIA_EXTS):
            bad.append("组 %s 的 exts 越界" % g.key)
        if not os.path.isdir(g.root):
            bad.append("组 %s 的 root 不存在：%s" % (g.key, g.root))
        # 核心断言：该组的文件对话框过滤器必须含它自己的扩展名
        f = group_filter(g)
        for e in g.exts:
            if ("*" + e) not in f:
                bad.append("组 %s 的过滤器漏了 %s" % (g.key, e))
    if FORMAT_GROUPS[0].exts != frozenset({_with_dots(config.VIDEO_EXTS)[0]}):
        bad.append("默认组（下拉第 1 项）不是 config.VIDEO_EXTS[0]")
    print("groups=%d  video=%d  audio=%d  default=%s"
          % (len(FORMAT_GROUPS), len(MEDIA_EXTS), len(AUDIO_MEDIA_EXTS),
             default_group().label))
    for g in FORMAT_GROUPS:
        print("  %-14s exts=%-8s root=%s"
              % (g.label, ",".join(sorted(g.exts)), g.root))
    if bad:
        for b in bad:
            print("FMTCHECK_FAIL=%s" % b)
        return 1
    print("FMTCHECK_OK")
    return 0


def _usage() -> None:
    print("用法：")
    print("  python video_player.py selftest-scan [root] [--exts .mp4|all]")
    print("        扫库自检（默认 config.VIDEO_DIR + 视频格式）")
    print("  python video_player.py decodecheck <file>     无窗口真解码验证（demux 真实 .ts/.mp4）")
    print("  python video_player.py selftest-formats       格式清单/下拉项一致性自检")


def main(argv: List[str]) -> int:
    try:  # 控制台 GBK 兜底：避免标题含生僻/日文字符时打印崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv:
        _usage()
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "selftest-scan":
        # `--exts` 从位置参数里摘出来，剩下的第一个才是 root（顺序可互换）
        root, exts = None, None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--exts":
                if i + 1 >= len(rest):
                    print("--exts 需要取值（如 --exts .mp4 或 --exts all）")
                    return 2
                exts = _parse_exts_arg(rest[i + 1])
                i += 2
                continue
            if a.startswith("--exts="):
                exts = _parse_exts_arg(a.split("=", 1)[1])
                i += 1
                continue
            if root is None:
                root = a
            i += 1
        return _cmd_selftest(root, exts)
    if cmd == "decodecheck":
        if not rest:
            print("decodecheck 需要 <file> 参数")
            return 2
        return _cmd_decodecheck(rest[0])
    if cmd == "selftest-formats":
        return _cmd_formats()
    print("未知子命令：%s" % cmd)
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
