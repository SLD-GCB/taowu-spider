import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="zhconv")
import os
import time
import traceback
import tkinter as tk
from tkinter import messagebox
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from config import (
    CHROME_PATH, LOG_ENABLE, BREAKER_LIMIT, get_random_sleep,
    WAIT_SHORT_MIN, WAIT_SHORT_MAX, WAIT_REFRESH_MIN, WAIT_REFRESH_MAX,
    WAIT_LONG_MIN, WAIT_LONG_MAX, ERROR_SLEEP_MIN, ERROR_SLEEP_MAX,
    CHROME_LAUNCH_ARGS, get_random_ua, get_scroll_config,
    get_scroll_sleep, get_full_finger_js, INTERCEPT_KEYWORDS, INVALID_FLAGS
)

# 全局状态变量
current_retry = 0
FAIL_COUNTER = 0
CIRCUIT_BREAKER = False

def show_verify_tip() -> bool:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    res = messagebox.askokcancel(
        "检测人机验证",
        "请切换至Chrome窗口手动完成验证\n完成后点击【确定】继续\n点击【取消】终止任务"
    )
    root.destroy()
    return res

def init_browser_pool():
    pass

def recycle_driver(driver):
    try:
        if driver:
            driver.quit()
    except Exception:
        pass

def preprocess_html(html: str) -> str:
    if not html:
        return ""
    return html.strip()

def fetch_page(url: str, wait_second: int = 0) -> str:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    html_content = ""
    driver = None

    if CIRCUIT_BREAKER:
        if LOG_ENABLE:
            print("[熔断状态，直接返回空]")
        return ""

    # 强制校验：只使用项目内内置浏览器
    if not os.path.exists(CHROME_PATH):
        if LOG_ENABLE:
            print(f"❌ 内置浏览器不存在：{CHROME_PATH}")
        return ""

    try:
        opts = Options()
        # 固定使用你的内置 chrome-headless-shell.exe
        opts.binary_location = CHROME_PATH
        # 加载所有浏览器启动参数
        for arg in CHROME_LAUNCH_ARGS:
            opts.add_argument(arg)
        opts.add_argument(f"user-agent={get_random_ua()}")

        # Selenium 4+ 自动管理驱动，【不需要手动指定 chromedriver】
        driver = webdriver.Chrome(options=opts)

        # 指纹伪装
        driver.execute_script(get_full_finger_js())

        # 拦截验证/广告域名
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": [
                "*captcha*", "*verify*", "*waf*", "*security*",
                "*track*", "*monitor*", "*finger*"
            ]
        })
        driver.execute_cdp_cmd("Network.enable", {})

        driver.get(url)
        time.sleep(get_random_sleep(WAIT_SHORT_MIN, WAIT_SHORT_MAX))

        # 拦截判断
        if any(k in driver.title or k in driver.current_url.lower() for k in INTERCEPT_KEYWORDS):
            if LOG_ENABLE:
                print("[⚠️ 检测人机验证，请手动处理]")
            if not show_verify_tip():
                raise Exception("用户取消任务")
            driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            driver.refresh()
            time.sleep(get_random_sleep(WAIT_REFRESH_MIN, WAIT_REFRESH_MAX))

        # 页面等待
        if wait_second > 0:
            time.sleep(wait_second)
        else:
            time.sleep(get_random_sleep(WAIT_LONG_MIN, WAIT_LONG_MAX))

        # 模拟真人滚动行为
        scroll_times, scroll_step = get_scroll_config()
        for i in range(scroll_times):
            driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {i/scroll_times})")
            get_scroll_sleep()
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3)

        html_content = driver.page_source
        if len(html_content) < 2000 or any(flag in html_content for flag in INVALID_FLAGS):
            raise Exception("页面被拦截或内容为空")

        if LOG_ENABLE:
            print(f"✅ 页面抓取完成，字符数：{len(html_content)}")

    except Exception as e:
        current_retry += 1
        if LOG_ENABLE:
            print(f"[抓取异常 第{current_retry}次重试] {str(e)}")
        traceback.print_exc()
        time.sleep(get_random_sleep(ERROR_SLEEP_MIN, ERROR_SLEEP_MAX))
        FAIL_COUNTER += 1
        if FAIL_COUNTER >= BREAKER_LIMIT:
            CIRCUIT_BREAKER = True
            if LOG_ENABLE:
                print("[🔴 达到熔断阈值，停止抓取]")
    finally:
        if driver:
            recycle_driver(driver)

    return preprocess_html(html_content)

init_browser_pool()
fetch_main = fetch_page