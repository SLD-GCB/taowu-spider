#!/usr/bin/env python3
# build.py — Taowu Spider v12.1.2 打包脚本（PySide6/Qt）
# 功能：Python代码+依赖→单EXE，附带控制台终端实时显示运行日志
#       full_chrome / 潜龙模型 与EXE同级打包
import os
import sys
import shutil
import subprocess

# ── 修复 Windows 控制台 GBK 编码问题 ──────────────────────
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('gbk', 'gb2312', 'gb18030'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if sys.stderr.encoding and sys.stderr.encoding.lower() in ('gbk', 'gb2312', 'gb18030'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

# ── 配置 ──────────────────────────────────────────────
EXE_NAME = "梼杌-v4.3"
ENTRY_SCRIPT = "main_qt.py"
BUILD_DIR = "build"
DIST_DIR = "dist"

# ── 应用图标（参照 baxiadb 零依赖生成多分辨率 ICO）─────────
APP_ICON_SRC = "red_start-taowu.png"   # 源图片
APP_ICON = "taowu.ico"                 # 生成/使用的 ICO 文件
APP_ICON_PNG = "taowu.png"             # 兼容旧代码的 PNG 回退文件
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

# 需要复制到 dist/ 与exe同级的目录
# 格式: (源路径, 目标文件夹名) 或 (源路径, 目标文件夹名, 排除模式元组)
# 源路径相对于项目根目录，目标名即 dist/ 下的文件夹名
COPY_DIRS = [
    ("full_chrome", "full_chrome"),
    # 2026-09-10 修正：原条目是 ("潜龙文本抽取模型", "bin", "潜龙v2.0.0模型无交互版本")
    #   → 目标名那个目录**磁盘上根本不存在**（潜龙文本抽取模型/ 下没有 bin/），每次构建
    #   只打印一行 "⚠ 跳过（不存在）" 就过去了。而 config.rule_extract() 实际调用的是
    #   **Python 模块** infer_api/infer_core（先问它、值有效才用，空才回退正则），跟那个
    #   预编译 EXE 毫无关系（config.MODEL_EXE_PATH 全项目零引用 = 死常量）。
    #   漏掉本条的代价：开发版 _qianlong 加载成功、打包版必然失败，且 import 失败是
    #   `except: pass` 静默的 → 打包版对同一份招聘文本抽出**不同**的岗位名称/薪资范围/
    #   学历要求…，不报任何错。**这就是本条 COPY_DIRS 存在的唯一理由。**
    #   排除项都是"训练/打包/自测"产物，抽取链路一概不读（infer_core.py 只 import re、
    #   不 open 任何文件；load_resources() 是空壳）：
    #     dataset 7.4MB 训练集、model_out 3.6MB 权重、build/__pycache__、
    #     train.py/训练集生成.py/torch_test.py/test_demo.py（训练与自测，import torch 会失败）。
    #   ⚠ cli_entry.py 必须排除：config 会把本目录插到 sys.path[0]，而项目根也有一个
    #     cli_entry.py（在 hidden-imports 里），同名的会被它顶掉。
    ("潜龙文本抽取模型", "潜龙文本抽取模型", (
        "dataset", "model_out", "build", "__pycache__",
        "cli_entry.py", "train.py", "训练集生成.py", "torch_test.py", "test_demo.py",
    )),
    ("runtime/mpv", "runtime/mpv"),   # libmpv-2.dll 外置 exe 同级，config.BASE_DIR(=exe 目录) 命中
    # 视频流抓取 fMP4/CMAF/DASH remux 依赖 ffmpeg.exe（静态单文件，87MB）。_locate_ffmpeg 搜
    # here/runtime/ffmpeg/bin/ffmpeg.exe → 外置同级让 config.BASE_DIR 命中（勿漏，漏则 remux 失败/退化）
    ("runtime/ffmpeg", "runtime/ffmpeg"),
    # 2026-09-07 数据表模板库 + 字段规则库：config.TABLE_TEMPLATE_DIR/FIELD_RULE_DIR 都基于
    # BASE_DIR(=exe 目录) 运行时 open() 读取 → 必须整目录外置同级。漏则打包版“模板/抓取规则”
    # 下拉只剩招聘 legacy 兜底，全部行业模板/规则静默失效。
    ("数据表模板", "数据表模板"),
    ("字段规则", "字段规则"),
    # 2026-09-10 媒体配方库：config.MEDIA_RECIPE_DIR 同样基于 BASE_DIR(=exe 目录)，media_recipe
    # 用 open() 扫 媒体配方/*.json。**漏了这里 = B站/油管/抖音三站全部解析不出**（Python 版配方
    # 已于同日退役，JSON 是唯一来源），且不报错、只表现为"什么都没找到"，故必须外置同级。
    ("媒体配方", "媒体配方"),
    # ⚠ 用户配方（config.MEDIA_RECIPE_USER_DIR）**绝不能加进来**，这是刻意的：
    #   ① 打包步骤对每个 COPY_DIRS 目标是 rmtree + copytree，加进来 = 每次构建把开发机上的
    #      用户配方打进发行包（可能含私货/带签名式 URL 的站），
    #   ② 更要命的是升级时会把老用户自己写的配方**整体删掉**。
    #   该目录由 media_recipe.import_recipe_file / GUI「导入配方」按需 makedirs，缺失即空转。
    #   （它已进下面的 RUNTIME_DIRS，新装时是个**空目录**——可见性归那边管，覆盖与否归这里管。）
    # 同理，**用户字段配置/（config.FIELD_CONFIG_USER_DIR，2026-09-10 新增）也绝不能加进来** ——
    #   它是文字抽取引擎的用户配置目录（对标 用户配方/），上面两条理由一字不差地适用。
    #   该目录由 config.import_field_config / 主窗「导入配置」按钮按需 makedirs，缺失即空转。
    #   （同样已进 RUNTIME_DIRS 预建空目录；config.py:229 扫不到目录就 continue，不报错。）
    #   （数据表模板/ 与 字段规则/ 是**自带**目录，已经在上面，必须继续外置：字段配置说明文档
    #    `数据表模板/_用户字段配置说明.md` 与两份 _ 开头的模板 JSON 也靠它一起发出去。）
]

# 需要打包进 EXE 的资源文件（参照 baxiadb：用 os.pathsep）
RESOURCE_FILES = [
    (APP_ICON_PNG, "."),
    (APP_ICON, "."),
]

# 需要复制到 dist/ 的单文件
COPY_FILES = [
    "browser_paths.py",
    APP_ICON_PNG,
    APP_ICON,
]

# 需要在 dist/ 下创建的运行时目录
# ⚠ 语义：本清单只走 os.makedirs(exist_ok=True) —— **绝不 rmtree**，所以在这里放"用户可能已经
#   写过东西的目录"是安全的（新建空目录 / 已存在则原样保留）。与 COPY_DIRS 的区别就在这一条：
#   COPY_DIRS 是 rmtree + copytree，任何用户可写的目录都**不能**进那边（见上方 COPY_DIRS 注释）。
RUNTIME_DIRS = [
    "runtime",
    "runtime/cookies",
    "runtime/chrome_profile",
    # Chrome 硬骨头站点专用 profile（config.py:4352 作为 --user-data-dir 传入）。Chrome 自己也会建，
    # 这里预建只为让首次运行前目录就可见（与 runtime/chrome_profile 对齐）。
    "runtime/hard_site_profile",
    "output",
    "output/images",
    "output/videos",          # 视频流抓取 + 本地播放器 的视频库目录（config 启动时也会自动建）
    "output/_video_partials", # 视频断点续传影子目录
    "output/audio",           # 抓取音频流的音频库目录（config 启动时也会自动建）
    "output/_audio_partials", # 音频分段 HLS/DASH 影子目录（config 启动时也会自动建）
    "plugins",  # 插件目录（用户可后续放入自定义插件）
    # ── 用户配置目录：**只预建空目录，绝不入 COPY_DIRS**（2026-09-10 补）─────────────
    # 这两个是"用户自己写"的目录，此前完全靠 GUI 导入按钮按需 makedirs，后果=拿到发行包的
    # 人看不见它们存在，得先点一次「导入」才冒出来（凭空多个目录，像出 bug）。
    # 放这里后：新装时是个空目录（可见、可解释），**已存在则原样保留**（makedirs 不删东西）。
    # 绝不上移进 COPY_DIRS —— 那边 rmtree + copytree 会删光用户配置，也会把开发机私货打进包。
    "用户配方",      # config.MEDIA_RECIPE_USER_DIR     用户媒体配方（media_recipe 注册时后注册者赢）
    "用户字段配置",  # config.FIELD_CONFIG_USER_DIR    用户字段配置/规则片段（对标 用户配方/）
]

# dist/ 里**必须**存在的组件（相对 dist/ 的路径）。复制完成即校验，缺一个就让构建失败。
# 只收"缺了不崩、只让打包版静默产出与开发版不同结果"的那几项 —— 这正是本项目的病史：
#   潜龙缺 → 招聘字段值静默走正则兜底（2026-09-10 实例）；
#   媒体配方缺 → B站/油管/抖音三站"什么都没找到"，不报错；
#   数据表模板/字段规则缺 → 模板与抓取规则下拉只剩招聘 legacy 兜底，静默。
# 会**响亮**报错的（full_chrome 缺了浏览器直接起不来、runtime/mpv 缺了播放器会提示找不到
# dll）不在此列 —— 那些自己会喊，加进来只会让别人的机器无谓构建失败。
REQUIRED_IN_DIST = [
    os.path.join("潜龙文本抽取模型", "infer_api.py"),
    os.path.join("潜龙文本抽取模型", "infer_core.py"),
    "媒体配方",
    "数据表模板",
    "字段规则",
]


def run(cmd, **kw):
    """执行命令并打印"""
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    print(f"  >> {cmd_str}")
    return subprocess.run(cmd, shell=not isinstance(cmd, list), **kw)


def print_banner():
    """打印控制台横幅"""
    print("=" * 60)
    print("  ⚡ Taowu Spider v12.1.2 — 打包构建脚本（PySide6/Qt）")
    print("  模式：单EXE + 控制台终端（实时日志）+ 模型/浏览器")
    print("=" * 60)


def step(msg: str):
    """打印步骤标题"""
    print(f"\n{'─' * 50}")
    print(f"  {msg}")
    print(f"{'─' * 50}")


def clean_old_artifacts():
    """清理旧的构建产物"""
    step("[1/6] 清理旧构建产物...")
    for d in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            print(f"  已删除: {d}/")
    for f in os.listdir("."):
        if f.endswith(".spec"):
            os.remove(f)
            print(f"  已删除: {f}")
    # 清理 __pycache__
    for root, dirs, files in os.walk(".", topdown=False):
        if "__pycache__" in dirs:
            p = os.path.join(root, "__pycache__")
            shutil.rmtree(p, ignore_errors=True)
    print("  清理完成 ✓")


def build_exe():
    """PyInstaller 打包 —— --console 模式让控制台窗口弹出显示实时日志"""
    step("[2/6] PyInstaller 打包（--console 控制台可见）...")
    print(f"  入口脚本: {ENTRY_SCRIPT}")
    print(f"  输出名称: {EXE_NAME}.exe")
    print("  控制台模式: 开启 + PySide6/Qt GUI")

    # ═══════════════════════════════════════════════════
    #  关键：--console（非 --noconsole）
    #  exe启动时弹出cmd终端 + Qt GUI窗口
    #  所有 print() 输出实时显示在终端中
    # ═══════════════════════════════════════════════════
    pyi_cmd = [
        sys.executable, "-m", "PyInstaller",

        # ── 基本配置 ──
        "--name=" + EXE_NAME,
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        "--log-level=WARN",
    ]

    # ── 应用图标（参照 baxiadb：绝对路径） ──────────────
    icon_abs = os.path.join(ROOT, APP_ICON)
    if os.path.exists(icon_abs):
        pyi_cmd.append(f"--icon={icon_abs}")
        print(f"  应用图标:    {icon_abs}")
    else:
        print(f"  [WARN] 图标文件不存在: {icon_abs}")

    # ── 资源文件（参照 baxiadb：os.pathsep） ────────────
    for src, dst in RESOURCE_FILES:
        if os.path.exists(src):
            pyi_cmd.append(f"--add-data={src}{os.pathsep}{dst}")
            print(f"  add-data:   {src} -> {dst}/")
        else:
            print(f"  [WARN] 资源文件不存在: {src}")

    # ── 扩展：第三方库 + hidden-import ──────────────────
    pyi_cmd += [
        "--collect-all=zhconv",
        "--collect-all=selenium",
        "--collect-all=bs4",
        "--collect-all=lxml",
        "--collect-all=urllib3",
        "--collect-all=requests",
        "--collect-all=certifi",
        "--collect-all=pymysql",
        # ── 视频流抓取：惰性依赖（模块级 import 只在函数内 → 显式收全）──
        "--collect-all=curl_cffi",   # TLS 指纹伪装：libcurl 静态链进 _wrapper.pyd，连带 cffi/orjson
        "--collect-all=Crypto",      # TS AES-128 解密（pycryptodome）：各算法 .pyd 运行时动态加载
        # 只收集 QtWidgets 相关，跳过 QML/QtQuick（减少 60%+ 体积）
        "--collect-submodules=PySide6.QtCore",
        "--collect-submodules=PySide6.QtGui",
        "--collect-submodules=PySide6.QtWidgets",

        # ── hidden-import：项目内部模块 ──
        "--hidden-import=qiongqi_maw",
        "--hidden-import=baize",
        "--hidden-import=basnake_core",
        "--hidden-import=taotie_filter",
        "--hidden-import=zhujian_watch",
        "--hidden-import=data_extract",
        "--hidden-import=industrial_spider",
        "--hidden-import=DBPlugin",
        "--hidden-import=config_gui",
        "--hidden-import=Spider_instructor",
        "--hidden-import=slider_solver",
        "--hidden-import=slider_debugger",
        "--hidden-import=page_stripper",
        "--hidden-import=browser_paths",
        "--hidden-import=config",
        "--hidden-import=video_grab",   # 视频流抓取后端：仅 qt_dialogs 函数内惰性 from-import → 显式 hidden
        # 数据表模板 schema 抽取（schema_engine）：全项目仅 qiongqi_maw/qt_dialogs 函数体内
        # from-import，无顶层 import → 显式 hidden 防打包版模板抽取静默退化
        "--hidden-import=schema_engine",
        # 2026-09-08 起新增的解密引擎/任务身份/指纹纯模块：仅 video_grab 顶层 try 内可选导入，
        # 或 config 顶层引（stealth_profile）→ 一并显式 hidden 兜底，确保打包版不静默缺失
        "--hidden-import=streamdecrypt",
        "--hidden-import=task_identity",
        "--hidden-import=stealth_profile",
        # 抓取音频流后端（audio_grab）：同上，仅 qt_dialogs 函数内惰性 from-import → 显式 hidden。
        # 依赖=纯 stdlib + config + video_grab + 外置 runtime/ffmpeg（AES-128/remux/打标/重编码全走 ffmpeg，
        # 不新增第三方 py 依赖）；离线回归夹具目录（resolve_e2e/media_e2e/video_e2e）故意不入包。
        "--hidden-import=audio_grab",
        # 通用媒体源解析层 media_resolve + 平台配方包 site_recipes（2026-09-09 起新增）：
        # 仅被 M1 qt_media_grab 等懒加载调用方引入、无顶层静态引用 → 显式 hidden 防打包版静默缺失。
        # site_recipes 的子平台模块由 ensure_registered() importlib 动态载入（PyInstaller 静态图收不到），
        # M2/M3 每新增一个平台子模块须在此补对应 hidden-import（作者打包约定，同 schema_engine 先例）。
        # 注：bilibili/douyin/youtube 三个 Python 配方 2026-09-10 已迁为 媒体配方/*.json（数据文件，
        # 走上面的 COPY_DIRS，不需要 hidden-import）；site_recipes 包保留但 _PLATFORMS 现为空。
        "--hidden-import=media_resolve",
        "--hidden-import=site_recipes",
        # 媒体配方（JSON）引擎：仅 media_resolve.resolve_page 函数体内惰性 import（避免顶层循环依赖）
        # → 静态图收不到，必须显式 hidden（同 schema_engine 先例）。
        "--hidden-import=media_recipe",
        # 通用「页面 API 旁观」捕获缝（media_capture，2026-09-10 起新增）：只被 media_resolve 的
        # 函数体内懒 import（保住 media_resolve 顶层仅 stdlib 的纪律）→ 静态图收不到，必须显式 hidden。
        "--hidden-import=media_capture",
        # 顺带补漏洞：websocket-client 只被 site_recipes/douyin.py 与 media_capture.py 在
        # **函数体内** import（CDP 连接），build.py/spec 此前 0 命中 → 打包版 CDP 缝会静默失效。
        "--collect-all=websocket",
        "--hidden-import=cli_entry",
        "--hidden-import=text_formatter",
        "--hidden-import=qt_theme",
        "--hidden-import=qt_dialogs",
        "--hidden-import=qt_media_grab",   # M1 通用媒体下载弹窗：main_qt._open_media_grab 函数内惰性 import → hidden
        "--hidden-import=qt_recipe_row",   # 配方选择行 mixin（两个抓取弹窗继承）：仅 qt_* 顶层 import → hidden
        # 统一配置管理组件（2026-09-11）：全部调用点都在**函数体内**惰性 import
        # （main_qt._open_config_manager / qt_recipe_row._open_config_manager_dialog），
        # 顶层零引用 → 静态图收不到。漏了它的症状 = 打包版点「配置管理」报"打不开"，
        # 而三套配置的导入/启停/查看在打包版里全部不可用（开发版一切正常）。
        "--hidden-import=qt_config_manager",
        # 网站全方位诊断（2026-09-11）：后端 site_diag 只被 qt_site_diag 的函数体内惰性 import，
        # qt_site_diag 只被 main_qt._open_site_diag 的函数体内惰性 import —— **两个都是顶层零引用**，
        # 静态图一个都收不到。漏了的症状 = 打包版点「网站诊断」报"打不开"（开发版一切正常）。
        # diag_e2e/ 是离线夹具，故意不入包（同 resolve_e2e/media_e2e/video_e2e）。
        "--hidden-import=site_diag",
        "--hidden-import=qt_site_diag",
        # 诊断知识库（P2，2026-09-11）：只被 site_diag._knowledge_stage 的函数体内惰性 import，
        # 顶层零引用 —— 同上面两条，静态图收不到。漏了的症状 = 打包版报告「知识库命中」
        # 一节永远空着（不报错），历史也一条不落（更隐蔽：看起来像"这站没诊断过"）。
        "--hidden-import=site_knowledge",
        "--hidden-import=qt_player",
        "--hidden-import=video_player",
        "--hidden-import=mpv",
        "--hidden-import=RULE_LIB_PATH",
        "--hidden-import=spider_batch_v9",

        # ── hidden-import：PySide6 子模块 ──
        "--hidden-import=PySide6.QtCore",
        "--hidden-import=PySide6.QtGui",
        "--hidden-import=PySide6.QtWidgets",
        "--hidden-import=shiboken6",

        # ── hidden-import：第三方库子模块 ──
        "--hidden-import=zhconv",
        "--hidden-import=bs4",
        "--hidden-import=bs4.builder",
        "--hidden-import=lxml",
        "--hidden-import=lxml.etree",
        "--hidden-import=lxml.html",
        "--hidden-import=pymysql",
        "--hidden-import=pymysql.cursors",
        "--hidden-import=urllib3.util.ssl_",
        "--hidden-import=requests.adapters",
        "--hidden-import=selenium.webdriver.chrome.service",
        "--hidden-import=selenium.webdriver.common.by",
        "--hidden-import=selenium.webdriver.common.keys",
        "--hidden-import=selenium.common.exceptions",
        "--hidden-import=selenium.webdriver.chrome.options",
        "--hidden-import=selenium.webdriver.support.ui",
        "--hidden-import=selenium.webdriver.support.expected_conditions",

        # ── hidden-import：标准库（有些需要显式声明） ──
        "--hidden-import=queue",
        "--hidden-import=concurrent.futures",
        "--hidden-import=pickle",
        "--hidden-import=hashlib",
        "--hidden-import=ssl",
        "--hidden-import=json",
        "--hidden-import=threading",
        "--hidden-import=re",
        "--hidden-import=sqlite3",
        "--hidden-import=shutil",
        "--hidden-import=pathlib",

        # ── 入口脚本 ──
        ENTRY_SCRIPT,
    ]

    result = run(pyi_cmd)
    if result.returncode != 0:
        print("\n" + "!" * 60)
        print("  ❌ PyInstaller 打包失败！请检查上方错误信息")
        print("!" * 60)
        # 检查PyInstaller是否安装
        try:
            import PyInstaller
        except ImportError:
            print("\n  提示：PyInstaller 未安装，请先执行：")
            print("    pip install pyinstaller")
        input("\n  按 Enter 退出...")
        sys.exit(1)

    print("  EXE构建完成 ✓")


def copy_data_to_dist():
    """复制外部文件夹和文件到 dist/，与exe同级"""
    step("[3/6] 复制外部资源到 dist/（与 EXE 同级）...")

    dist_path = os.path.join(ROOT, DIST_DIR)

    # ── 复制文件夹（full_chrome, 潜龙模型） ──
    for dir_spec in COPY_DIRS:
        src_rel, dst_name = dir_spec[0], dir_spec[1]
        ignore = dir_spec[2] if len(dir_spec) > 2 else None
        src = os.path.join(ROOT, src_rel)
        dst = os.path.join(dist_path, dst_name)
        if not os.path.exists(src):
            print(f"  ⚠ 跳过（不存在）: {src_rel}")
            continue

        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)

        print(f"  复制文件夹: {src_rel} -> {dst_name}/ ...")
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns(*ignore) if ignore else None)
        # 统计文件数
        file_count = sum(1 for _ in os.walk(dst) for f in _[2])
        size_mb = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(dst) for f in fs
        ) / (1024 * 1024)
        print(f"    → {file_count} 个文件, {size_mb:.1f} MB")

    # ── 复制单文件 ──
    for file_name in COPY_FILES:
        src = os.path.join(ROOT, file_name)
        dst = os.path.join(dist_path, file_name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"  复制文件: {file_name}")
        else:
            print(f"  ⚠ 跳过（不存在）: {file_name}")

    # ── 出口校验：静默缺失是这条链路的病史，必须让构建**响**而不是"跳过一句就过" ──
    # 上面那条 COPY_DIRS 曾经指向一个不存在的源目录，构建照常成功、发行包照常出，
    # 只有"打包版抽出的字段值和开发版不一样"这一个症状，且不报任何错。
    missing = [p for p in REQUIRED_IN_DIST
               if not os.path.exists(os.path.join(dist_path, p))]
    if missing:
        print("\n" + "!" * 60)
        for p in missing:
            print(f"  ❌ dist/ 缺少必需组件: {p}")
        print("  这些文件缺失不会让程序崩溃，只会让打包版**静默**产出与开发版不同的结果。")
        print("!" * 60)
        try:  # 非交互运行（脚本/CI）没有 stdin，input 会抛 EOFError 盖住上面这段话
            input("\n  按 Enter 退出...")
        except EOFError:
            pass
        sys.exit(1)
    print(f"  出口校验通过（{len(REQUIRED_IN_DIST)} 项必需组件齐全）✓")
    print("  资源复制完成 ✓")


def create_runtime_dirs():
    """创建运行时需要的目录结构"""
    step("[4/6] 创建运行时目录...")

    dist_path = os.path.join(ROOT, DIST_DIR)
    for d in RUNTIME_DIRS:
        full = os.path.join(dist_path, d)
        os.makedirs(full, exist_ok=True)
        print(f"  {full}")

    print("  运行时目录创建完成 ✓")


def create_launcher_bat():
    """创建便捷启动脚本（可选）"""
    step("[5/6] 创建启动说明文件...")

    dist_path = os.path.join(ROOT, DIST_DIR)

    # 创建中文使用说明
    readme_content = f"""╔══════════════════════════════════════════════════════╗
║     ⚡ Taowu Spider v12.1.2 — 饕餮爬虫 使用说明      ║
╚══════════════════════════════════════════════════════╝

【启动方式】
  双击 {EXE_NAME}.exe 即可启动

【启动后你会看到】
  控制台终端 + PySide6/Qt 图形界面（baxiadb 设计风格）
  → 终端显示实时日志，GUI 操作面板

【目录说明】
  full_chrome/                  — Chrome浏览器 + ChromeDriver
  潜龙文本抽取模型/              — 招聘字段抽取器（自动调用，纯 Python 正则）
  媒体配方/                      — 各站点解析配方（JSON，可增删改）
  数据表模板/  字段规则/          — 行业数据表模板与抓取规则库
  用户配方/    用户字段配置/      — 你自己的配方/字段配置（升级不会被覆盖）
  browser_paths.py               — 浏览器路径配置（可按需修改）
  runtime/                       — 运行时数据（Cookie、Profile等）
  output/                        — 采集结果输出
  plugins/                       — 插件目录

【注意事项】
  - 首次启动可能需要以管理员身份运行
  - 如果Chrome路径有变化，请编辑 browser_paths.py
  - 控制台窗口不要关闭，否则程序会退出

【文件清单】
"""
    # 列出 dist 目录结构
    for item in sorted(os.listdir(dist_path)):
        p = os.path.join(dist_path, item)
        if os.path.isdir(p):
            readme_content += f"  {item}/\n"
        else:
            size_kb = os.path.getsize(p) / 1024
            readme_content += f"  {item}  ({size_kb:.0f} KB)\n"

    readme_path = os.path.join(dist_path, "使用说明.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"  已创建: 使用说明.txt")

    print("  启动文件创建完成 ✓")


def print_final_summary():
    """打印最终构建摘要"""
    step("[6/6] 构建完成 — 最终摘要")

    dist_path = os.path.join(ROOT, DIST_DIR)
    exe_path = os.path.join(dist_path, EXE_NAME + ".exe")

    exe_size_mb = os.path.getsize(exe_path) / (1024 * 1024) if os.path.exists(exe_path) else 0

    print(f"""
  ╔════════════════════════════════════════════════════╗
  ║           ✅ 打包构建成功！                        ║
  ╚════════════════════════════════════════════════════╝

  输出目录: {dist_path}

  ┌─ 主程序 ─────────────────────────────────────────┐
  │ {EXE_NAME}.exe     {exe_size_mb:>8.1f} MB  │
  │  (双击启动 → 弹出控制台 + GUI双窗口)              │
  ├─ 资源文件夹 ─────────────────────────────────────┤
  │ full_chrome/            (浏览器运行环境)          │
  │ 潜龙文本抽取模型/        (招聘字段抽取器)          │
  │ 媒体配方/ 数据表模板/ 字段规则/  (数据文件)        │
  ├─ 配置文件 ──────────────────────────────────────┤
  │ browser_paths.py        (浏览器路径配置)          │
  ├─ 运行时目录 ────────────────────────────────────┤
  │ runtime/                (Cookie/Profile)          │
  │ output/                 (采集结果)                │
  │ plugins/                (插件目录)                │
  └─────────────────────────────────────────────────┘

  【启动方式】
    双击 {EXE_NAME}.exe →
    ① 控制台终端自动弹出（显示实时日志）
    ② GUI图形界面弹出（操作面板）

  【提示】
    - 控制台窗口 = 实时运行日志，关闭即退出程序
    - 分发时请将整个 dist/ 文件夹一起打包
""")

    print("=" * 60)
    print("  构建成功！按 Enter 退出...")
    input()


# ═══════════════════════════════════════════════════════════════
# 步骤 0: 生成应用图标 (red_start-taowu.png → taowu.ico + taowu.png)
# 中心裁切 55% 放大老虎主体 + Pillow 高质量多分辨率 ICO
# ═══════════════════════════════════════════════════════════════
def generate_app_icon():
    """从 APP_ICON_SRC 生成多分辨率 .ico 图标（中心裁切放大 + Pillow）。"""
    try:
        from PIL import Image
    except ImportError:
        print("  [ERROR] Pillow 未安装，请执行: pip install Pillow")
        return False

    src = os.path.join(ROOT, APP_ICON_SRC)
    dst = os.path.join(ROOT, APP_ICON)
    png_fallback = os.path.join(ROOT, APP_ICON_PNG)

    if not os.path.exists(src):
        print(f"  [SKIP] 源图片不存在: {APP_ICON_SRC}")
        return False

    # 增量：ICO 已是最新则跳过
    if os.path.exists(dst) and os.path.exists(png_fallback):
        if os.path.getmtime(dst) >= os.path.getmtime(src):
            print(f"  [OK] 图标已是最新，跳过生成")
            return True

    print(f"  正在从 {APP_ICON_SRC} 生成图标 ({len(ICON_SIZES)} 个尺寸)...")

    # ── 打开源图 ──
    img = Image.open(src)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    w, h = img.size

    # ── 中心裁切 55%：放大老虎 + 红星主体 ──
    crop_ratio = 0.55
    crop_w = int(w * crop_ratio)
    crop_h = int(h * crop_ratio)
    left = (w - crop_w) // 2
    top = (h - crop_h) // 2
    cropped = img.crop((left, top, left + crop_w, top + crop_h))

    # ── 正方形画布 + 3% 边距 ──
    cw, ch = cropped.size
    side = max(cw, ch)
    margin = int(side * 0.03)
    canvas = side + margin * 2
    square = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    square.paste(cropped, ((canvas - cw) // 2, (canvas - ch) // 2))

    # ── 保存 PNG 回退文件 ──
    square.save(png_fallback, "PNG")

    # ── 生成多分辨率 ICO ──
    square.save(dst, "ICO", sizes=[(s, s) for s in ICON_SIZES])

    print(f"  [OK] 图标已生成: {APP_ICON} ({len(ICON_SIZES)} 个尺寸, "
          f"{os.path.getsize(dst) / 1024:.0f} KB), "
          f"PNG回退: {APP_ICON_PNG} ({os.path.getsize(png_fallback) / 1024:.0f} KB)")
    return True


def main():
    print_banner()

    try:
        generate_app_icon()
        clean_old_artifacts()
        build_exe()
        copy_data_to_dist()
        create_runtime_dirs()
        create_launcher_bat()
        print_final_summary()
    except KeyboardInterrupt:
        print("\n\n  用户取消构建")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n  ❌ 构建过程出错: {e}")
        import traceback
        traceback.print_exc()
        input("\n  按 Enter 退出...")
        sys.exit(1)


if __name__ == "__main__":
    main()
