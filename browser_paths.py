import os
import sys

def get_app_root():
    if getattr(sys, "frozen", False):
        # 打包后：BASE_DIR 就是 dist 目录
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_app_root()

# 关键：加上 .exe 后缀，和实际文件匹配
FULL_CHROME_DIR = os.path.join(BASE_DIR, "full_chrome")
CHROME_PATH = os.path.join(FULL_CHROME_DIR, "chrome.exe")
CHROMEDRIVER_PATH = os.path.join(FULL_CHROME_DIR, "chromedriver.exe")

# 启动自检（方便你看路径）
print(f"[DEBUG] BASE_DIR: {BASE_DIR}")
print(f"[DEBUG] 尝试读取Chrome: {CHROME_PATH}")
print(f"[DEBUG] 尝试读取Driver: {CHROMEDRIVER_PATH}")

if not os.path.exists(CHROME_PATH):
    print(f"❌ 找不到 Chrome: {CHROME_PATH}")
    input("按回车退出...")
    sys.exit(1)

if not os.path.exists(CHROMEDRIVER_PATH):
    print(f"❌ 找不到 Chromedriver: {CHROMEDRIVER_PATH}")
    input("按回车退出...")
    sys.exit(1)