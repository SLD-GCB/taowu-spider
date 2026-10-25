# -*- coding: utf-8 -*-
"""开发期一次性脚本：把 LGPL libmpv-2.dll（含同层依赖 dll）放进 runtime/mpv/。

不进 app 运行时、不打包。用法：
  python tools\\ensure_libmpv.py                # 自动解析 zhongfly/mpv-winbuild 最新 LGPL x86_64 dev 资产
  python tools\\ensure_libmpv.py <7z 或直接 libmpv dll 路径>   # 手工指定已下载文件
下载 7z 需网络；解压用 py7zr（pip install py7zr）。
取 LGPL 构建以避免 GPL 合规负担；普通 x86_64（非 v3）兼顾旧 CPU。"""
import os
import re
import sys
import json
import shutil
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "runtime", "mpv")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _resolve_asset_url():
    url = ("https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    pat = re.compile(r"^mpv-dev-lgpl-x86_64-\d+-git-.*\.7z$")
    for a in d.get("assets", []):
        name = a["name"]
        if pat.match(name):
            return a["browser_download_url"], name
    raise RuntimeError("latest 里没找到 LGPL x86_64 dev 资产")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    os.makedirs(DEST, exist_ok=True)
    work = tempfile.mkdtemp(prefix="mpvdev_")
    try:
        if arg.lower().endswith(".7z"):
            src7z, name = arg, os.path.basename(arg)
        elif arg.lower().endswith(".dll"):
            shutil.copy2(arg, os.path.join(DEST, os.path.basename(arg)))
            print("copied single dll ->", DEST)
            return 0
        else:
            dl, name = _resolve_asset_url()
            print("downloading", name)
            req = urllib.request.Request(dl, headers=UA)
            src7z = os.path.join(work, name)
            with urllib.request.urlopen(req, timeout=300) as r, open(src7z, "wb") as f:
                shutil.copyfileobj(r, f)
        # 解压
        import py7zr
        ex = os.path.join(work, "ex")
        with py7zr.SevenZipFile(src7z, "r") as z:
            z.extractall(ex)
        # 找 libmpv-2.dll 与其同层 *.dll
        found = []
        for root, _dirs, files in os.walk(ex):
            if "libmpv-2.dll" in files:
                found.append(root)
        if not found:
            raise RuntimeError("解压结果里没有 libmpv-2.dll")
        libdir = found[0]
        copied = 0
        for fn in os.listdir(libdir):
            if fn.lower().endswith(".dll"):
                shutil.copy2(os.path.join(libdir, fn), os.path.join(DEST, fn))
                copied += 1
                print("  +", fn)
        print("copied %d dll(s) -> %s" % (copied, DEST))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
