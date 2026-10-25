# browser_paths.py — 浏览器路径配置（v12.1.2）
# 源码运行时由 config.py 自动检测，打包后此文件可被用户手动编辑。
# 此文件会在 build.py 打包时复制到 dist/ 目录，与 EXE 同级。
import os
import sys

# 优先使用 config.py 的自动检测路径
try:
    from config import CHROME_PATH as _cfg_chrome, CHROMEDRIVER_PATH as _cfg_driver, FULL_CHROME_DIR as _cfg_dir
except ImportError:
    _cfg_chrome = None
    _cfg_driver = None
    _cfg_dir = None

# ── 如果打包后 config 不可用，则回退到手动路径 ──
def _get_dist_root():
    """获取 EXE / 脚本所在目录（dist/ 或源码根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))

_DIST_ROOT = _get_dist_root()

# Chrome 浏览器可执行文件路径
CHROME_PATH = _cfg_chrome if _cfg_chrome and os.path.exists(_cfg_chrome) else os.path.join(
    _DIST_ROOT, "full_chrome", "chrome.exe"
)

# ChromeDriver 可执行文件路径
CHROMEDRIVER_PATH = _cfg_driver if _cfg_driver and os.path.exists(_cfg_driver) else os.path.join(
    _DIST_ROOT, "full_chrome", "chromedriver.exe"
)

# Chrome 完整目录
FULL_CHROME_DIR = _cfg_dir if _cfg_dir and os.path.exists(_cfg_dir) else os.path.join(
    _DIST_ROOT, "full_chrome"
)

# 程序根目录
BASE_DIR = _DIST_ROOT

# ── 用户可在下方手动设置路径（覆盖自动检测） ──
# 取消注释并填写你自己的路径：
# CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# CHROMEDRIVER_PATH = r"D:\tools\chromedriver.exe"
# FULL_CHROME_DIR = r"D:\my_chrome"
