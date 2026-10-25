# 穷奇之口 qiongqi_maw.py 【v6.0 轰碎WAF版】
# v6.0：直接启动Chrome + CDP连接（零ChromeDriver）+ 会话持久化
import os, json, time, random, traceback, pickle, re, hashlib, threading, subprocess
import requests as img_requests
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from queue import Queue
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchWindowException, WebDriverException
from datetime import datetime
from config import *
set_selenium_env()
from baize import baize, HEADLESS_BYPASS_JS, CHALLENGE_BYPASS_JS
from basnake_core import basnake_core
from taotie_filter import taotie_filter
from zhujian_watch import zhujian_eye
from data_extract import extract_main
from slider_solver import solve_current_slider
from page_stripper import strip_page
import industrial_spider

current_retry = FAIL_COUNTER = 0
CIRCUIT_BREAKER = False
import data_extract
data_extract.TOTAL_ROUND = EXTRACT_ROUND

# ========== 驱动池（浏览器增强模式复用多个驱动实例） ==========
_driver_pool = []
_driver_pool_lock = threading.Lock()
_driver_use_count = {}
_driver_max_use = DRIVER_MAX_REUSE
_pool_size = DRIVER_POOL_SIZE

# v6.0: 直接Chrome模式标记
_USE_DIRECT_CHROME = True  # True=直接启动Chrome(零ChromeDriver), False=使用ChromeDriver
# v7.0: 验证码攻击策略 —— 检测到验证/屏蔽页面时自动切换IP+指纹重试
_MAX_CAPTCHA_RETRY = 3  # 验证码失败后最多换IP重试次数

# 统计追踪
_stats_lock = threading.Lock()
_global_stats = {
    "total_fetched": 0, "success_fetched": 0, "fail_fetched": 0,
    "total_bytes": 0, "total_time_ms": 0, "start_time": time.time()
}

def get_global_stats() -> dict:
    with _stats_lock:
        s = dict(_global_stats)
        elapsed = time.time() - s["start_time"]
        s["elapsed_sec"] = round(elapsed, 1)
        s["success_rate"] = round(s["success_fetched"] / max(s["total_fetched"], 1) * 100, 1) if s["total_fetched"] > 0 else 0
        return s

def reset_global_stats():
    with _stats_lock:
        for k in _global_stats:
            _global_stats[k] = 0
        _global_stats["start_time"] = time.time()

# ===== v5.0 真实浏览器配置目录（关键：使用持久化profile，不像机器人） =====
REAL_CHROME_PROFILE_DIR = os.path.join(RUNTIME_DIR, "chrome_profile")
os.makedirs(REAL_CHROME_PROFILE_DIR, exist_ok=True)

def _build_chrome_options(ua: str = None) -> Options:
    """构建浏览器配置——Chrome 148+兼容，移除所有已废弃的自动化标记。
    ua 可选（任务档位）：缺省随机。注意调用方须把**同一个 ua** 再传给
    baize.start_defense_by_level —— 消除"启动 --user-agent 一个随机、CDP 覆写又一个随机"的两头不自洽。"""
    opt = Options()
    opt.binary_location = CHROME_PATH

    # 持久化用户目录
    opt.add_argument(f"--user-data-dir={REAL_CHROME_PROFILE_DIR}")

    # 窗口
    opt.add_argument("--window-size=1920,1080")
    opt.add_argument("--start-maximized")

    # Chrome 148+ 反检测（仅保留有效的flags）
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_argument("--disable-features=AutomationControlled")
    opt.add_argument("--disable-infobars")

    # 基础
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--ignore-certificate-errors")
    opt.add_argument("--no-first-run")
    opt.add_argument("--no-default-browser-check")

    # 静默
    opt.add_argument("--log-level=3")
    opt.add_argument("--silent")

    # 页面加载策略
    opt.page_load_strategy = PAGE_LOAD_STRATEGY

    # UA：任务档位显式给 ua，否则随机（新池全量完整串）
    opt.add_argument(f"user-agent={ua or get_random_ua()}")

    # 注意：不再使用以下已废弃的选项（Chrome 148+会忽略并可能暴露自动化）：
    #   - useAutomationExtension (deprecated)
    #   - excludeSwitches: enable-automation (deprecated)

    return opt

# ====== v6.0 核心：直接启动Chrome + CDP连接（零ChromeDriver，轰碎WAF） ======
_DEBUG_PORT = 9222
_direct_chrome_process = None
_direct_chrome_ready = False

def _kill_chrome_for_direct_mode():
    """清理所有Chrome进程，确保干净启动"""
    try:
        subprocess.run("taskkill /f /im chrome.exe 2>$null", shell=True, capture_output=True)
        time.sleep(1)
    except Exception:
        pass

def _launch_chrome_direct(profile_dir: str = None, port: int = None):
    """直接启动Chrome浏览器，返回subprocess.Popen对象"""
    global _DEBUG_PORT, _direct_chrome_process
    if port is None:
        port = _DEBUG_PORT
    if profile_dir is None:
        profile_dir = REAL_CHROME_PROFILE_DIR

    _kill_chrome_for_direct_mode()

    chrome_cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--window-size=1920,1080",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-translate",
        "--disable-notifications",
        "--disable-hang-monitor",
        "--disable-background-networking",
        "--disable-popup-blocking",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--log-level=3",
    ]

    # 代理：如果启用，传给Chrome
    _proxy = get_random_proxy()
    if _proxy:
        proxy_url = _proxy.get("http") or _proxy.get("https") or ""
        if proxy_url:
            chrome_cmd.append(f"--proxy-server={proxy_url}")
            print(f"[DIRECT_CHROME] Using proxy: {proxy_url}")

    print(f"[DIRECT_CHROME] Launching: {CHROME_PATH}")
    print(f"[DIRECT_CHROME] Profile: {profile_dir}")
    print(f"[DIRECT_CHROME] Port: {port}")

    try:
        # Windows: 使用 os.devnull 代替 subprocess.DEVNULL（更可靠）
        devnull = open(os.devnull, 'w')
        _direct_chrome_process = subprocess.Popen(
            chrome_cmd,
            stdout=devnull,
            stderr=devnull,
            stdin=devnull,
        )
        devnull.close()
        # 等待Chrome启动（至少3秒）
        time.sleep(3)
        # 检查进程是否存活
        if _direct_chrome_process.poll() is not None:
            print(f"[DIRECT_CHROME] Chrome exited immediately (exit code: {_direct_chrome_process.returncode})")
            _direct_chrome_process = None
            return None
        print(f"[DIRECT_CHROME] Chrome running (PID: {_direct_chrome_process.pid})")
        return _direct_chrome_process
    except FileNotFoundError:
        print(f"[DIRECT_CHROME] Chrome not found at: {CHROME_PATH}")
        return None
    except Exception as e:
        print(f"[DIRECT_CHROME] Launch error: {e}")
        _direct_chrome_process = None
        return None

def _connect_to_chrome_via_cdp(port: int = None) -> webdriver.Chrome:
    """
    【轰碎WAF核心】通过CDP连接到直接启动的Chrome

    Selenium通过 debuggerAddress 连接到已运行的Chrome实例
    这种方式下没有ChromeDriver参与浏览器启动 → 零CDC痕迹
    """
    if port is None:
        port = _DEBUG_PORT

    # 验证Chrome debug端口是否可访问
    import urllib.request
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
        data = json.loads(resp.read().decode())
        print(f"[DIRECT_CHROME] CDP connected: {data.get('Browser', 'Unknown')}")
    except Exception as e:
        print(f"[DIRECT_CHROME] CDP connection failed: {e}")
        raise ConnectionError(f"Cannot connect to Chrome on port {port}. Is Chrome running?")

    # Selenium连接方式：使用debuggerAddress
    opt = _build_chrome_options()
    opt.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    service = Service(executable_path=CHROMEDRIVER_PATH)
    service.log_output = None

    # 【关键】此时ChromeDriver只作为CDP代理，不启动新浏览器
    # 因为浏览器已通过 _launch_chrome_direct() 直接启动
    driver = webdriver.Chrome(service=service, options=opt)

    # 此时driver连接到已运行的Chrome，但没有CDC注入
    # 因为ChromeDriver没有启动这个浏览器实例
    print("[DIRECT_CHROME] Connected to Chrome via CDP — ZERO ChromeDriver traces")

    return driver

def create_direct_chrome_driver(profile_dir: str = None, ua: str = None):
    """一站式：直接启动Chrome + CDP连接，失败返回None。
    ua 可选（任务档位）：缺省随机完整串。"""
    global _direct_chrome_ready

    if profile_dir is None:
        profile_dir = REAL_CHROME_PROFILE_DIR

    # 步骤1：直接启动Chrome
    proc = _launch_chrome_direct(profile_dir=profile_dir)
    if proc is None:
        return None

    # 步骤2：等待Chrome完全就绪
    time.sleep(2)

    # 步骤3：通过CDP连接
    try:
        driver = _connect_to_chrome_via_cdp()
    except Exception as e:
        print(f"[DIRECT_CHROME] CDP connect failed: {e}")
        _kill_chrome_for_direct_mode()
        return None

    # 步骤4：CDP预注入隐身脚本（直连单实例：真实 Chrome 无 --user-agent，UA 由此处覆写为档位）
    try:
        driver.get("about:blank")
        baize.start_defense_by_level(driver, level=ANTI_DETECT_LEVEL, ua=ua or get_random_ua())
    except Exception:
        pass

    # 步骤5：加载Cookie
    if ENABLE_COOKIE_PERSIST and os.path.exists(COOKIE_JAR_PATH):
        try:
            with open(COOKIE_JAR_PATH, "rb") as f:
                cookies = pickle.load(f)
            for ck in cookies:
                try:
                    driver.add_cookie(ck)
                except Exception:
                    pass
        except Exception:
            pass

    _direct_chrome_ready = True
    print("[DIRECT_CHROME] READY — zero ChromeDriver mode active")
    return driver

def shutdown_direct_chrome():
    """关闭直接启动的Chrome（仅在确实有进程时执行）"""
    global _direct_chrome_process, _direct_chrome_ready, _USE_DIRECT_CHROME
    if _direct_chrome_process is None and not _direct_chrome_ready:
        return  # 已经关闭，不重复执行
    try:
        _kill_chrome_for_direct_mode()
        _direct_chrome_process = None
        _direct_chrome_ready = False
        _USE_DIRECT_CHROME = False
        print("[DIRECT_CHROME] Shutdown complete")
    except Exception:
        pass

def _create_driver(ua: str = None) -> webdriver.Chrome:
    """创建浏览器驱动——v5.0：持久化profile + CDC预检 + CDP预注入。
    ua 可选（任务档位）：缺省随机。**同一个 ua** 贯穿 启动选项(--user-agent) 与 CDP 覆写，
    消除此前"options 一个随机、override 又一个随机"的两头不自洽。"""
    # 【关键】启动前先验证CDC修补状态
    if not baize._cdc_patched:
        print("[PRE-CHECK] CDC patch not applied, patching now...")
        baize._ensure_cdc_patched()
        if not baize._cdc_patched:
            print("[WARNING] CDC patch failed! 51job/Lagou will detect this browser as automated.")

    ua = ua or get_random_ua()
    service = Service(executable_path=CHROMEDRIVER_PATH)
    service.log_output = None
    driver = webdriver.Chrome(service=service, options=_build_chrome_options(ua=ua))

    # 先导航到blank建立CDP通道
    driver.get("about:blank")

    # CDP预注入：所有隐身脚本在页面加载前注册（必须在driver.get(target)之前）；ua 同 options 一个档位
    baize.start_defense_by_level(driver, level=ANTI_DETECT_LEVEL, ua=ua)

    # 多重注入确保覆盖（不同CDP API路径）
    for _js in [LIGHT_DEFENSE_JS, HEADLESS_BYPASS_JS]:
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _js})
        except Exception:
            pass

    # 加载持久化Cookie
    if ENABLE_COOKIE_PERSIST and os.path.exists(COOKIE_JAR_PATH):
        try:
            with open(COOKIE_JAR_PATH, "rb") as f:
                cookies = pickle.load(f)
            for ck in cookies:
                try:
                    driver.add_cookie(ck)
                except Exception:
                    pass
        except Exception:
            pass

    # 重置到干净状态（此时隐身JS已注册，后续所有页面都会自动注入）
    try:
        driver.get("about:blank")
    except Exception:
        pass

    return driver

def _destroy_driver(driver):
    """安全销毁驱动"""
    try:
        if driver:
            driver.quit()
    except Exception:
        pass

def _apply_task_profile(driver, ua: str):
    """把任务档位落到（可能复用的）driver：CDP UA 覆写 + 档位派生的 Sec-CH-UA/Sec-Fetch/Accept-Language 头。
    只对"显式给了 ua"的路径调用（vg 浏览器任务：每任务档位 + 站点覆盖）；ua 缺省的普通爬不触发。
    新起的 driver（_create_driver/create_direct_chrome_driver）已由 start_defense_by_level(ua) 做同样的事，
    这里专为**池内复用 / 直连单实例重连**的 driver 覆写成本任务档位（覆写与额外头均 sticky，需每任务重打）。"""
    if not ua or driver is None:
        return
    try:
        fp = None
        try:
            import stealth_profile as _sp
            fp = _sp.page_fingerprint_for_ua(ua)
        except Exception:
            fp = None
        baize.set_user_agent_override(driver, ua, fp=fp)
        baize.set_extra_headers(driver, fp=fp)
    except Exception:
        pass


def acquire_driver(ua: str = None) -> webdriver.Chrome:
    """获取浏览器驱动——v6.0支持直接Chrome模式（失败自动回退ChromeDriver）。
    ua 可选（任务档位完整 UA 串）：给出 → 落到 driver（选项/覆写/头同一档位自洽），池内复用与
    直连单实例重连也会 CDP 重覆写到新档位（每任务档位切换）。ua 缺省 → 旧行为逐字节不变（普通爬）。"""
    global _driver_pool, _driver_use_count, _USE_DIRECT_CHROME, _direct_chrome_process, _direct_chrome_ready

    if _USE_DIRECT_CHROME:
        try:
            if not _direct_chrome_ready:
                d = create_direct_chrome_driver(ua=ua)
                if d is None:
                    print("[DIRECT_CHROME] Failed, falling back to ChromeDriver mode")
                    _USE_DIRECT_CHROME = False
                    return acquire_driver(ua=ua)
                return d
            else:
                d = _connect_to_chrome_via_cdp()
                _apply_task_profile(d, ua)          # 单实例直连重连：把驻留 Chrome 覆写为本任务档位
                return d
        except Exception as e:
            print(f"[DIRECT_CHROME] Error: {e}, falling back to ChromeDriver")
            _USE_DIRECT_CHROME = False
            _direct_chrome_process = None
            _direct_chrome_ready = False
            return acquire_driver(ua=ua)  # 递归回退

    # 标准ChromeDriver模式：池化复用
    with _driver_pool_lock:
        while _driver_pool:
            d = _driver_pool.pop(0)
            try:
                d.current_window_handle
                cnt = _driver_use_count.get(id(d), 0)
                if cnt >= _driver_max_use:
                    _destroy_driver(d)
                    continue
                _driver_use_count[id(d)] = cnt + 1
                _apply_task_profile(d, ua)          # 池内复用：覆写为本任务档位（每任务轮换）
                return d
            except Exception:
                _destroy_driver(d)
                continue

    d = _create_driver(ua=ua)
    _driver_use_count[id(d)] = 1
    return d

def release_driver(driver):
    """归还驱动到池"""
    global _driver_pool
    if driver is None:
        return
    with _driver_pool_lock:
        try:
            driver.current_window_handle
            if len(_driver_pool) < _pool_size:
                _driver_pool.append(driver)
                return
        except Exception:
            pass
    # 池已满或驱动失效，直接销毁
    _destroy_driver(driver)

def close_driver_pool():
    """关闭所有驱动，退出前保存cookies"""
    global _driver_pool, _USE_DIRECT_CHROME, _direct_chrome_process, _direct_chrome_ready

    if _USE_DIRECT_CHROME:
        if _direct_chrome_process is not None or _direct_chrome_ready:
            shutdown_direct_chrome()
            _direct_chrome_process = None
            _direct_chrome_ready = False
        return

    with _driver_pool_lock:
        cookies_saved = False
        for d in _driver_pool:
            try:
                if not cookies_saved and ENABLE_COOKIE_PERSIST:
                    cookies = d.get_cookies()
                    if cookies:
                        os.makedirs(os.path.dirname(COOKIE_JAR_PATH), exist_ok=True)
                        with open(COOKIE_JAR_PATH, "wb") as f:
                            pickle.dump(cookies, f)
                        cookies_saved = True
            except Exception:
                pass
            _destroy_driver(d)
        _driver_pool.clear()
        _driver_use_count.clear()

def get_all_driver_cookies() -> list:
    """获取驱动池中所有驱动的 cookies（合并去重）。"""
    all_cookies = []
    seen = set()
    with _driver_pool_lock:
        for d in _driver_pool:
            try:
                for ck in d.get_cookies():
                    key = (ck.get("name", ""), ck.get("domain", ""))
                    if key not in seen:
                        seen.add(key)
                        all_cookies.append(ck)
            except Exception:
                pass
    # 也尝试从磁盘加载
    try:
        if os.path.exists(COOKIE_JAR_PATH):
            with open(COOKIE_JAR_PATH, "rb") as f:
                disk_cookies = pickle.load(f)
                for ck in disk_cookies:
                    key = (ck.get("name", ""), ck.get("domain", ""))
                    if key not in seen:
                        seen.add(key)
                        all_cookies.append(ck)
    except Exception:
        pass
    return all_cookies

def clear_all_driver_cookies():
    """清除所有驱动池的cookies + 磁盘文件"""
    global _driver_pool
    with _driver_pool_lock:
        for d in _driver_pool:
            try:
                d.delete_all_cookies()
            except Exception:
                pass
    try:
        if os.path.exists(COOKIE_JAR_PATH):
            os.remove(COOKIE_JAR_PATH)
    except Exception:
        pass
    basnake_core.clear_all_cookies()

def show_tip():
    """显示人机验证提示框（v12.1.2：委托 config.show_verify_tip，Qt 版由 QMessageBox 处理）。"""
    return show_verify_tip()

# ===== v5.0：增强型验证等待——给用户足够时间手动解决 =====
def wait_for_manual_verify(driver, timeout: int = 120) -> bool:
    """
    等待用户手动完成滑块验证。
    每5秒检查一次页面是否已通过验证（挑战元素消失）。
    最多等待timeout秒。
    """
    challenge_selectors = [
        "#challenge-form", ".cf-challenge", "#cf-challenge",
        ".browser-verification", "#browser-verification",
        ".turnstile-widget", "#turnstile-widget",
        ".verify-slider", ".slider-captcha", ".captcha-container",
        "#nc_1_n1z", "#nocaptcha", ".yidun", ".geetest",
        "#captcha", ".captcha", ".verification",
        "iframe[src*='captcha']", "iframe[src*='challenge']",
        "iframe[src*='verify']"
    ]

    start = time.time()
    while time.time() - start < timeout:
        try:
            # 检查挑战是否还存在
            challenge_gone = True
            for sel in challenge_selectors:
                try:
                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                    if els and any(e.is_displayed() for e in els):
                        challenge_gone = False
                        break
                except Exception:
                    continue

            if challenge_gone:
                # 再检查页面内容是否正常加载
                try:
                    strip_page(driver, sk)
                    html = driver.page_source
                    if len(html) > 2000 and not any(
                        k in html[:10000].lower()
                        for k in ["just a moment", "attention required",
                                   "请稍候", "正在验证", "安全验证",
                                   "滑动验证", "请完成验证"]
                    ):
                        print("[VERIFY] 验证已通过！页面正常加载")
                        return True
                except Exception:
                    pass

            time.sleep(5)

        except Exception:
            time.sleep(5)

    print(f"[VERIFY] 等待超时({timeout}s)")
    return False

# ========== 修改后 export_res 函数：仅写入原始 clean_text_2 ==========
def export_res(name, url, fjson, blocks, ext, ok, err, text1="", text2=""):
    ts = datetime.now().strftime(TIME_FORMAT)
    base = f"{name}_{ts}"
    tp, jp = os.path.join(OUTPUT_DIR, f"{base}.txt"), os.path.join(OUTPUT_DIR, f"{base}.json")
    with open(tp, "w", encoding="utf-8") as f:
        f.write(EXPORT_TPL_SOURCE.format(url=url, site=name, time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))+EXPORT_TPL_SPLIT+fjson)

    out = {
        "clean_text_2": text2
    }
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    LOG_ENABLE and (print(f"📄{tp}"),print(f"📊{jp}"))
    return ext

# ========== 模板抽取分支（方案二 schema_v2）==========
# 仅把“抽取字段规则”换成模板 JSON 列（纯数据、零对抗逻辑、零写侧文件）。
# 输入输出与 flow 一致：顶层 wrapper 四键 + 额外 clean_text 供复核弹窗预览。
# 抗反爬链路完全不经过此函数；未绑定/招聘 legacy 不进入。
def _structured_page_text(html: str) -> str:
    """HTML → 按块换行的纯文本（schema_v2 专属，纯新增）。

    保留行首结构供 schema Pass A “标签：值”对位；taotie_filter 会压平空白、
    zhujian 标记是招聘专属，二者都不适合通用行业模板，故本分支自取文本。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "head",
                     "meta", "link", "form"]):
        tag.decompose()
    txt = soup.get_text(separator="\n", strip=True)
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return "\n".join(lines)[:30000]


def _page_titles(html: str) -> list:
    """取页面主标题候选（schema_v2 专属，供 clean:"title" 列兜底）。

    型号/名称类列常只出现在“无标签的大标题”里，文本层面认不出是标题；
    故在 DOM 层把候选标题交给 schema_extract。优先级：h1 → og:title → <title>。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    out = []
    _ws = re.compile(r"[ \t　]+")

    def _add(t):
        t = (t or "").strip()
        t = _ws.sub(" ", t).strip()
        if t and t not in out and len(t) <= 80:
            out.append(t)

    h1 = soup.find("h1")
    if h1:
        # 去掉角标/标签类小元素（bubble-box“即将上市”、tag/badge/status/icon 等），
        # 只留核心名称本体，避免把状态词带进型号列（如 易车 “长安启源Q06 即将上市”）。
        for sub in list(h1.find_all(True)):
            cls = " ".join(sub.get("class") or [])
            if any(k in cls for k in ("bubble", "tag", "badge", "status", "icon")):
                sub.decompose()
        _add(h1.get_text(" ", strip=True))
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        _add(og["content"])
    if soup.title:
        _add(soup.title.get_text(" ", strip=True))
    return out


def _schema_v2_flow(html, url, sk, spec):
    columns = list(spec.get("columns") or [])
    from schema_engine import schema_extract

    clean_text = _structured_page_text(html)
    ctx = {"url": url, "site": sk}
    titles = _page_titles(html)
    if titles:
        ctx["titles"] = titles

    try:
        row = schema_extract(clean_text, columns, ctx=ctx)
    except Exception:
        row = {}
    # 兜底：确保模板每列都有键（空值落库为空列，不报错）
    for c in columns:
        name = c.get("name") if isinstance(c, dict) else c
        if name and isinstance(name, str):
            row.setdefault(name, "")
    row.setdefault("job_url", url)
    row.setdefault("source", sk)

    # 有效性：任一业务列（非 job_url/source）抽出实值即视为命中
    business_names = [
        n for n in (c.get("name") if isinstance(c, dict) else c for c in columns)
        if n and n not in ("job_url", "source")
    ]
    if not business_names:
        has_valid = True
    else:
        has_valid = any(str(row.get(n) or "").strip() not in ("", "不限") for n in business_names)

    return {
        "zhujian_full_content_json": json.dumps(
            {"content": clean_text, "site_key": sk, "source_url": url},
            ensure_ascii=False),
        "qianlong_extract_result": row,
        "normal_data": [row] if has_valid else [],
        "error_data": [] if has_valid else [row],
        "clean_text": clean_text,
    }


# ========== flow 函数：原样保留（并行调用版本见 flow_parallel） ==========
def flow(html, url, sk):
    # 模板抽取分支（纯新增）：ACTIVE_SCHEMA 是运行时可变槽，
    # 须运行时 import config 读最新值（勿用模块顶 from config import * 的旧快照）。
    try:
        import config as _cfg
    except Exception:
        _cfg = None
    if _cfg is not None:
        _spec = getattr(_cfg, "ACTIVE_SCHEMA", None) or None
        if _spec and _spec.get("engine") == "schema_v2" and _spec.get("columns"):
            return _schema_v2_flow(html, url, sk, _spec)
    blk = taotie_filter.process(html)
    first_clean_text = "\n".join(blk)

    fp = zhujian_eye.parse_full(html)
    fj = zhujian_eye.to_json_input(fp)
    second_clean_text = fp

    # v3.1修复：传zhujian_watch的结构化文本（含【公司信息】【岗位名称】等标记）
    # 之前传taotie_filter的纯文本 → 标记丢失 → 企业名/联系方式提取不到
    ext = extract_main(zhujian_eye.to_json_input(fp), sk)
    ok, err = zhujian_eye.watch_batch([ext])

    extract_data = export_res(
        safe_name(sk), url, fjson=fj, blocks=blk,
        ext=ext, ok=ok, err=err,
        text1=first_clean_text,
        text2=second_clean_text
    )
    LOG_ENABLE and print("🎉处理完成\n")
    return extract_data

# ========== 新增：批量并行flow处理（利用多核CPU加速文本处理） ==========
def flow_batch_parallel(html_list: list, url_list: list, sk: str) -> list:
    """对多个HTML页面并行执行flow处理，利用多核加速"""
    if not html_list:
        return []
    result_list = [None] * len(html_list)

    def _flow_one(idx_html):
        idx, html = idx_html
        if not html:
            return idx, {}
        try:
            data = flow(html, url_list[idx], sk)
            return idx, data
        except Exception as e:
            if LOG_ENABLE:
                print(f"[flow并行] 第{idx}条处理异常: {e}")
            return idx, {}

    max_w = min(MAX_CONCURRENT_PARSE, len(html_list))
    with ThreadPoolExecutor(max_workers=max_w) as pool:
        tasks = [(i, h) for i, h in enumerate(html_list)]
        futures = {pool.submit(_flow_one, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            try:
                idx, data = fut.result()
                result_list[idx] = data
            except Exception:
                idx = futures[fut]
                result_list[idx] = {}

    return result_list

# ===================== 【新增：真人行为模拟引擎】 =====================
def _bezier_curve(start, end, steps: int = 60):
    """贝塞尔曲线生成路径点（模拟鼠标移动轨迹）"""
    import math
    # 随机控制点偏移
    dx = abs(end[0] - start[0]) * random.uniform(0.15, 0.45)
    dy = abs(end[1] - start[1]) * random.uniform(-0.3, 0.3)
    cp1 = (start[0] + dx, start[1] + dy)
    cp2 = (end[0] - dx * random.uniform(0.5, 1.2), end[1] + dy * random.uniform(-0.5, 0.5))
    points = []
    for i in range(steps + 1):
        t = i / steps
        # 缓入缓出
        t_eased = t * t * (3 - 2 * t)
        # 三次贝塞尔
        x = (1 - t_eased) ** 3 * start[0] + 3 * (1 - t_eased) ** 2 * t_eased * cp1[0] + \
            3 * (1 - t_eased) * t_eased ** 2 * cp2[0] + t_eased ** 3 * end[0]
        y = (1 - t_eased) ** 3 * start[1] + 3 * (1 - t_eased) ** 2 * t_eased * cp1[1] + \
            3 * (1 - t_eased) * t_eased ** 2 * cp2[1] + t_eased ** 3 * end[1]
        points.append((x, y))
    return points

def simulate_mouse_movement(driver, from_el=None, to_el=None, steps: int = 60):
    """模拟真人鼠标移动（贝塞尔曲线 + 随机微抖动）"""
    try:
        if from_el is None and to_el is None:
            # 随机窗口内移动
            w = driver.execute_script("return window.innerWidth")
            h = driver.execute_script("return window.innerHeight")
            start = (random.randint(50, w // 3), random.randint(50, h // 3))
            end = (random.randint(w // 3, w - 50), random.randint(h // 3, h - 50))
        elif to_el is not None:
            start = (random.randint(100, 400), random.randint(100, 300))
            loc = to_el.location
            size = to_el.size
            end = (loc['x'] + random.randint(5, max(5, size['width'] - 5)),
                   loc['y'] + random.randint(5, max(5, size['height'] - 5)))
        else:
            return

        path = _bezier_curve(start, end, steps)
        script = """
        var event = new MouseEvent('mousemove', {
            bubbles: true, cancelable: true, view: window,
            clientX: arguments[0], clientY: arguments[1],
            screenX: arguments[0], screenY: arguments[1] + 80,
            movementX: arguments[2] || 0, movementY: arguments[3] || 0
        });
        document.elementFromPoint(arguments[0], arguments[1])?.dispatchEvent(event);
        """
        prev_x, prev_y = start
        for i, (x, y) in enumerate(path):
            dx, dy = x - prev_x, y - prev_y
            driver.execute_script(script, int(x), int(y), int(dx), int(dy))
            prev_x, prev_y = x, y
            time.sleep(random.uniform(0.002, 0.008))
    except Exception:
        pass

def simulate_reading_pause(driver):
    """模拟人类阅读停顿（2-10秒随机停顿+微量滚动）"""
    pause_time = random.uniform(2.0, 10.0)
    # 分段停顿并在中间做微滚动（模拟阅读时手指轻划）
    segments = random.randint(2, 4)
    seg_time = pause_time / segments
    for _ in range(segments):
        time.sleep(seg_time * random.uniform(0.6, 1.4))
        try:
            driver.execute_script(f"window.scrollBy(0, {random.randint(-30, 80)})")
        except Exception:
            pass
        time.sleep(random.uniform(0.1, 0.4))

def simulate_scroll_behavior(driver):
    """模拟真人滚屏：加速-减速-暂停-继续 的阅读模式"""
    try:
        total_height = driver.execute_script("return document.body.scrollHeight")
        view_height = driver.execute_script("return window.innerHeight")
        current = 0
        # 模拟分段阅读
        segment_count = random.randint(3, 8)
        for i in range(segment_count):
            # 每次滚动距离 = 约1/2到2/3视口
            scroll_dist = int(view_height * random.uniform(0.4, 0.75))
            target = min(current + scroll_dist, total_height - view_height)
            # 分段微步长滚动（模拟人类逐行/逐段阅读）
            substeps = random.randint(8, 25)
            for j in range(substeps):
                current += (target - current) / (substeps - j)
                driver.execute_script(f"window.scrollTo(0, {int(current)})")
                time.sleep(random.uniform(0.015, 0.08))
            # 在当前位置停顿（模拟阅读）
            time.sleep(random.uniform(0.5, 3.0))
    except Exception:
        pass

def simulate_element_interaction(driver):
    """随机找一个非关键元素悬停（模拟用户浏览行为）"""
    try:
        elements = driver.execute_script("""
        var els = document.querySelectorAll('a, span, p, div, li');
        var visible = [];
        for (var i = 0; i < els.length; i++) {
            var rect = els[i].getBoundingClientRect();
            if (rect.width > 30 && rect.height > 10 && rect.top > 0 && rect.top < window.innerHeight) {
                visible.push(els[i]);
            }
        }
        return visible.slice(0, 50);
        """)
        if not elements:
            return
        # 随机选2-4个元素进行悬停
        hover_count = random.randint(2, min(4, len(elements)))
        for _ in range(hover_count):
            el = random.choice(elements)
            try:
                # mouseenter + mouseover
                driver.execute_script("""
                var e = arguments[0];
                var rect = e.getBoundingClientRect();
                var cx = rect.left + rect.width / 2;
                var cy = rect.top + rect.height / 2;
                ['mouseenter','mouseover','mousemove'].forEach(function(t){
                    e.dispatchEvent(new MouseEvent(t, {bubbles:true,cancelable:true,view:window,clientX:cx,clientY:cy}));
                });
                """, el)
                time.sleep(random.uniform(0.3, 1.5))
            except Exception:
                pass
    except Exception:
        pass

def inject_human_behavior(driver, intensity: str = "medium"):
    """将真人行为注入当前页面会话（根据等级控制行为密度）"""
    if intensity == "low" or intensity not in ("medium", "high", "paranoid"):
        return
    try:
        # 1. 贝塞尔曲线鼠标轨迹
        if intensity in ("high", "paranoid"):
            for _ in range(random.randint(1, 3)):
                simulate_mouse_movement(driver)
                time.sleep(random.uniform(0.3, 1.0))

        # 2. 元素悬停
        if intensity in ("medium", "high", "paranoid"):
            simulate_element_interaction(driver)

        # 3. 真人滚动模式
        if intensity in ("high", "paranoid"):
            simulate_scroll_behavior(driver)
        else:
            # medium: 简单分步滚动
            for r in SCROLL_RATIO_LIST:
                driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight*{r})")
                time.sleep(get_random_sleep(SCROLL_MIN, SCROLL_MAX))

        # 4. 阅读停顿
        if intensity in ("high", "paranoid"):
            simulate_reading_pause(driver)

    except Exception:
        pass

# ===================== v3.0 新增：挑战检测与多策略自动绕过 =====================

# 已知挑战模式特征库
CHALLENGE_SIGNATURES = {
    "cloudflare": {
        "title_keywords": ["just a moment", "attention required", "ddos", "checking"],
        "url_keywords": ["cloudflare", "challenge", "__cf_chl"],
        "body_keywords": ["cf-challenge", "cf-browser-verification", "turnstile", "_cf_chl_opt"],
        "selector": "#challenge-form, #cf-challenge, .cf-browser-verification",
        "wait_min": 5, "wait_max": 12,
        "retry_strategy": "wait_and_refresh"
    },
    "datadome": {
        "title_keywords": ["datadome"],
        "url_keywords": ["datadome", "dd"],
        "body_keywords": ["datadome", "dd-test"],
        "selector": "#datadome-captcha",
        "wait_min": 3, "wait_max": 8,
        "retry_strategy": "cookie_reset"
    },
    "akamai": {
        "title_keywords": ["access denied", "reference"],
        "url_keywords": ["akamai", "akam", "bmp"],
        "body_keywords": ["akamai", "ak-bms", "sensor_data"],
        "selector": ".akamai-captcha",
        "wait_min": 5, "wait_max": 15,
        "retry_strategy": "rotate_fingerprint"
    },
    "recaptcha": {
        "title_keywords": [],
        "url_keywords": ["recaptcha", "google.com/recaptcha"],
        "body_keywords": ["g-recaptcha", "recaptcha/api", "recaptcha-token"],
        "selector": ".g-recaptcha, iframe[src*='recaptcha']",
        "wait_min": 0, "wait_max": 0,
        "retry_strategy": "abort"
    },
    "hcaptcha": {
        "title_keywords": [],
        "url_keywords": ["hcaptcha"],
        "body_keywords": ["h-captcha", "hcaptcha.com"],
        "selector": ".h-captcha, iframe[src*='hcaptcha']",
        "wait_min": 0, "wait_max": 0,
        "retry_strategy": "abort"
    },
    "f5_shape": {
        "title_keywords": ["request blocked", "access denied"],
        "url_keywords": ["shape", "f5"],
        "body_keywords": ["shape-security", "f5-shape", "x-shape"],
        "selector": "meta[name='x-shape']",
        "wait_min": 3, "wait_max": 10,
        "retry_strategy": "rotate_fingerprint"
    },
    "imperva": {
        "title_keywords": ["blocked", "incapsula"],
        "url_keywords": ["incapsula", "imperva", "_imp_"],
        "body_keywords": ["incapsula", "imperva", "visid_incap"],
        "selector": "iframe[src*='incapsula']",
        "wait_min": 5, "wait_max": 10,
        "retry_strategy": "wait_and_refresh"
    },
    # === 滑块验证码（v6.1 新增，支持自动破解） ===
    "geetest": {
        "title_keywords": [],
        "url_keywords": ["geetest"],
        "body_keywords": ["geetest", "极验", "geetest_canvas"],
        "selector": ".geetest_canvas_bg, .geetest_bg",
        "wait_min": 1, "wait_max": 3,
        "retry_strategy": "auto_slider"  # 自动滑块破解
    },
    "yidun": {
        "title_keywords": [],
        "url_keywords": ["yidun", "dun.163"],
        "body_keywords": ["yidun", "易盾", "yidun_bg"],
        "selector": ".yidun_bg-img, .yidun_canvas",
        "wait_min": 1, "wait_max": 3,
        "retry_strategy": "auto_slider"
    },
    "dingxiang": {
        "title_keywords": [],
        "url_keywords": ["dingxiang", "dx.captcha"],
        "body_keywords": ["dingxiang", "顶象", "dx_captcha"],
        "selector": ".dx_captcha_bgimg, #dx_captcha_basic_bgimg",
        "wait_min": 1, "wait_max": 3,
        "retry_strategy": "auto_slider"
    },
    "tencent_captcha": {
        "title_keywords": [],
        "url_keywords": ["tcaptcha", "captcha.qq"],
        "body_keywords": ["tcaptcha", "腾讯验证码", "tcaptcha-bg"],
        "selector": ".tcaptcha-bg-img, #tcaptcha_bg_img",
        "wait_min": 1, "wait_max": 3,
        "retry_strategy": "auto_slider"
    },
    "generic_slider": {
        "title_keywords": ["验证码", "滑块验证", "安全验证", "人机验证"],
        "url_keywords": ["verify", "slider", "slide"],
        "body_keywords": ["slider", "滑块", "拖动滑块", "请按住滑块", "向右滑动",
                         "slide-to-verify", "verify-slider"],
        "selector": ".slider-captcha, .verify-slider, [class*='slider']",
        "wait_min": 1, "wait_max": 3,
        "retry_strategy": "auto_slider"
    }
}

def detect_challenge_type(driver) -> dict:
    """检测当前页面面临的挑战类型，返回匹配的签名配置"""
    try:
        title = driver.title.lower()
        url = driver.current_url.lower()
        body = driver.page_source[:50000].lower()

        for name, sig in CHALLENGE_SIGNATURES.items():
            title_match = any(k in title for k in sig["title_keywords"]) if sig["title_keywords"] else False
            url_match = any(k in url for k in sig["url_keywords"])
            body_match = any(k in body for k in sig["body_keywords"])

            if title_match or url_match or body_match:
                score = sum([title_match, url_match, body_match])
                return {"type": name, "signature": sig, "confidence": score}

        return {"type": "unknown", "signature": None, "confidence": 0}
    except Exception:
        return {"type": "unknown", "signature": None, "confidence": 0}

def handle_challenge(driver, challenge_info: dict) -> bool:
    """根据挑战类型执行对应的绕过策略"""
    sig = challenge_info.get("signature")
    if not sig:
        return False

    strategy = sig.get("retry_strategy", "wait_and_refresh")
    wait_min = sig.get("wait_min", 2)
    wait_max = sig.get("wait_max", 8)
    challenge_type = challenge_info.get("type", "unknown")

    if LOG_ENABLE:
        print(f"[挑战检测] 类型={challenge_type} 策略={strategy} 置信度={challenge_info.get('confidence')}")

    if strategy == "auto_slider":
        # 滑块验证码 → 自动破解
        if LOG_ENABLE:
            print(f"[{challenge_type}] 检测到滑块验证码，启动自动破解...")
        auto_solved = solve_current_slider(driver)
        if auto_solved:
            print(f"[{challenge_type}] ✅ 滑块自动破解成功！")
            return True
        else:
            print(f"[{challenge_type}] ❌ 滑块自动破解失败")
            return False

    if strategy == "abort":
        # reCAPTCHA/hCaptcha无法自动绕过
        if LOG_ENABLE:
            print(f"[{challenge_type}] 无法自动绕过，需要人工介入")
        return False

    elif strategy == "wait_and_refresh":
        # Cloudflare/Imperva: 等待挑战完成
        wait_time = random.uniform(wait_min, wait_max)
        if LOG_ENABLE:
            print(f"[{challenge_type}] 等待 {wait_time:.1f}s 让JS挑战完成...")
        time.sleep(wait_time)

        # 检查挑战是否通过
        try:
            title = driver.title.lower()
            url = driver.current_url.lower()
            if not any(k in title or k in url for k in sig.get("title_keywords", [])):
                if LOG_ENABLE:
                    print(f"[{challenge_type}] 挑战已通过")
                return True
        except Exception:
            pass

        # 刷新重试
        try:
            driver.refresh()
            time.sleep(random.uniform(wait_min, wait_max))
            return True
        except Exception:
            return False

    elif strategy == "cookie_reset":
        # DataDome: 清除Cookie重试
        try:
            driver.delete_all_cookies()
            if LOG_ENABLE:
                print(f"[{challenge_type}] Cookie已清除，重新加载...")
            driver.refresh()
            time.sleep(random.uniform(wait_min, wait_max))
            return True
        except Exception:
            return False

    elif strategy == "rotate_fingerprint":
        # Akamai/F5: 切换指纹配置
        if LOG_ENABLE:
            print(f"[{challenge_type}] 正在轮换浏览器指纹...")
        try:
            # 重新注入隐身脚本（新随机值）
            baize.inject_script(driver)
            baize.inject_challenge_bypass(driver)
            time.sleep(random.uniform(wait_min, wait_max))
            driver.refresh()
            time.sleep(random.uniform(wait_min, wait_max))
            return True
        except Exception:
            return False

    return False

def smart_fetch_with_challenge_handler(driver, url: str, sk: str, max_attempts: int = 3) -> str:
    """智能抓取：自动检测挑战 → 执行绕过 → 重试，最多3轮"""
    for attempt in range(max_attempts):
        try:
            driver.get(url)
            time.sleep(get_random_sleep(BASE_WAIT_MIN, BASE_WAIT_MAX))

            # 检测挑战
            challenge = detect_challenge_type(driver)

            if challenge["confidence"] > 0:
                if LOG_ENABLE:
                    print(f"[第{attempt+1}轮] 检测到挑战: {challenge['type']}")

                if challenge["type"] in ("recaptcha", "hcaptcha"):
                    if not show_tip():
                        raise Exception("用户取消任务")
                    continue

                bypassed = handle_challenge(driver, challenge)
                if not bypassed and challenge["type"] in ("geetest", "yidun", "dingxiang",
                                                          "tencent_captcha", "generic_slider"):
                    # 滑块自动破解失败 → 回退手动
                    print(f"[第{attempt+1}轮] 滑块自动破解失败，回退手动...")
                    if not show_tip():
                        raise Exception("用户取消任务")
                    passed = wait_for_manual_verify(driver, timeout=120)
                    if passed:
                        return driver.page_source
                    continue

                if not bypassed:
                    if challenge["confidence"] >= 2:
                        # 高置信度挑战，切换防御等级重试
                        if LOG_ENABLE:
                            print(f"[第{attempt+1}轮] 升级防御等级重试...")
                        baize.start_extreme_defense(driver, get_random_ua())
                        continue
                else:
                    # 挑战已绕过，返回页面
                    return driver.page_source

            else:
                # 无挑战，检查页面是否有效
                strip_page(driver, sk)
                html = driver.page_source
                if len(html) > PAGE_MIN_LENGTH and not any(k in html for k in PAGE_BLOCK_KEYS):
                    return html
                # 页面无效但无明显挑战标记，可能是静默拦截
                if LOG_ENABLE:
                    print(f"[第{attempt+1}轮] 页面异常(长度={len(html)})，重试...")
                time.sleep(exponential_backoff(attempt, base_sec=2.0, max_sec=30.0))

        except Exception as e:
            if LOG_ENABLE:
                print(f"[第{attempt+1}轮] 异常: {e}")

        if attempt < max_attempts - 1:
            time.sleep(exponential_backoff(attempt, base_sec=3.0, max_sec=60.0))

    return ""

# ===================== fetch 函数 =====================
def fetch(url, sk) -> tuple[bool, dict]:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    if CIRCUIT_BREAKER or not os.path.exists(CHROME_PATH) or not os.path.exists(CHROMEDRIVER_PATH):
        CIRCUIT_BREAKER and LOG_ENABLE and print("[🔴熔断]")
        LOG_ENABLE and print("[❌缺失Chrome或驱动文件]")
        return False, {}

    t0 = time.time()
    driver = None
    baize.full_clean_before_run()

    # v5.0: 站点感知防御升级——51job/拉勾等强检测站点自动启用究极模式
    _site_config = SITE_ANTI_DETECT_CONFIG.get(sk, {})
    _effective_level = _site_config.get("level", ANTI_DETECT_LEVEL)
    if _site_config.get("cdc_patch_required"):
        if not baize._cdc_patched:
            baize._ensure_cdc_patched()

    try:
        driver = acquire_driver()

        # 如果站点需要更高防御等级，在已获取的driver上追加防护
        if _effective_level == "extreme" and ANTI_DETECT_LEVEL != "extreme":
            baize.inject_challenge_bypass(driver)
            baize.protect_js_engine(driver)
            baize.protect_dom_parsing(driver)
            baize.disable_debugger(driver)

        # v3.0: 究极模式使用智能挑战绕过，普通模式使用直接访问
        if ANTI_DETECT_LEVEL == "extreme":
            html = smart_fetch_with_challenge_handler(driver, url, sk, max_attempts=3)
            if not html:
                raise Exception("智能挑战绕过失败")
            html_size = len(html)

            # 究极模式下仍执行真人行为模拟
            inject_human_behavior(driver, intensity="paranoid")
        else:
            driver.get(url)

            # 智能等待关键内容出现
            if ENABLE_SMART_WAIT:
                selector = SITE_CONTENT_SELECTORS.get(sk, SITE_CONTENT_SELECTORS["default"])
                try:
                    WebDriverWait(driver, timeout=15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                except Exception:
                    time.sleep(get_random_sleep(BASE_WAIT_MIN, BASE_WAIT_MAX))
            else:
                time.sleep(get_random_sleep(BASE_WAIT_MIN, BASE_WAIT_MAX))

            # v5.0增强验证码检测——覆盖更广的挑战关键词
            _verify_keywords_extended = VERIFY_KEYWORDS + [
                "just a moment", "attention required", "ddos", "checking",
                "请稍候", "正在验证", "安全验证", "滑动验证", "请完成验证",
                "人机验证", "行为验证", "访问验证", "验证您不是机器人",
                "turnstile", "cf-challenge", "browser-verification"
            ]
            _title_lower = driver.title.lower()
            _url_lower = driver.current_url.lower()
            _page_start = driver.page_source[:5000].lower() if driver.page_source else ""

            _is_challenged = (
                any(k in _title_lower for k in _verify_keywords_extended) or
                any(k in _url_lower for k in _verify_keywords_extended) or
                any(k in _page_start for k in _verify_keywords_extended)
            )

            if _is_challenged:
                print("[CHALLENGE] 检测到人机验证页面...")
                # ===== v6.1: 优先尝试滑块自动破解 =====
                slider_keywords = ["geetest", "yidun", "易盾", "dingxiang", "顶象",
                                   "tcaptcha", "滑块", "slider", "slide", "滑动验证",
                                   "拖动滑块", "请按住滑块", "向右滑动", "slide-to-verify"]
                _is_slider = any(k in _page_start for k in slider_keywords)

                if _is_slider:
                    print("[CHALLENGE] 检测到【滑块验证码】，启动自动破解...")
                    slider_solved = False
                    for auto_attempt in range(3):
                        # 检查driver是否还活着
                        try:
                            driver.current_url
                        except Exception:
                            print("[CHALLENGE] Driver disconnected, aborting slider solve...")
                            raise Exception("driver_disconnected_during_slider")

                        solved = solve_current_slider(driver)
                        if solved:
                            print(f"[CHALLENGE] 滑块自动破解成功! (第{auto_attempt+1}次)")
                            slider_solved = True
                            break
                        else:
                            print(f"[CHALLENGE] 第{auto_attempt+1}次自动破解失败")
                            # 再次检查driver
                            try:
                                driver.current_url
                            except Exception:
                                print("[CHALLENGE] Driver died after failed attempt, aborting...")
                                raise Exception("driver_disconnected_during_slider")
                            time.sleep(1)

                    if not slider_solved:
                        # 3次自动破解均失败 → 回退手动
                        print("[CHALLENGE] 自动破解3次均失败，回退手动处理...")
                        if not show_tip():
                            raise Exception("用户取消任务")
                        print("[CHALLENGE] 等待用户完成验证...")
                        passed = wait_for_manual_verify(driver, timeout=120)
                        if not passed:
                            driver.refresh()
                            time.sleep(get_random_sleep(WAIT_VERIFY_MIN, WAIT_VERIFY_MAX))
                            if not show_tip():
                                raise Exception("用户取消任务")
                            wait_for_manual_verify(driver, timeout=60)
                else:
                    # 非滑块验证（Cloudflare/文字验证码等）→ 手动
                    print("[CHALLENGE] 非滑块验证，弹出提示等待手动解决...")
                    if not show_tip():
                        raise Exception("用户取消任务")
                    print("[CHALLENGE] 等待用户完成验证...")
                    passed = wait_for_manual_verify(driver, timeout=120)
                    if not passed:
                        driver.refresh()
                        time.sleep(get_random_sleep(WAIT_VERIFY_MIN, WAIT_VERIFY_MAX))
                        if not show_tip():
                            raise Exception("用户取消任务")
                        wait_for_manual_verify(driver, timeout=60)

            time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))

            # 真人行为模拟
            cfg = ANTI_DETECT_CONFIG.get(ANTI_DETECT_LEVEL, {})
            if cfg.get("mouse_sim") or cfg.get("scroll_sim"):
                inject_human_behavior(driver, intensity=ANTI_DETECT_LEVEL)
            else:
                for r in SCROLL_RATIO_LIST:
                    driver.execute_script(f"window.scrollTo(0,document.body.scrollHeight*{r})")
                    time.sleep(get_random_sleep(SCROLL_MIN, SCROLL_MAX))

            # 等待页面完全渲染
            try:
                WebDriverWait(driver, timeout=8).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                time.sleep(1.5)

            strip_page(driver, sk)
            html = driver.page_source
            html_size = len(html)

        # v6.1: 智能拦截检测——仅检查标题+头部，不扫描全文（避免合法URL/数据中包含403等数字误判）
        _head = html[:5000].lower()
        _title = driver.title.lower()
        _is_blocked = (
            html_size < PAGE_MIN_LENGTH or
            any(k in _title for k in PAGE_BLOCK_KEYS) or
            (html_size < 5000 and any(k in _head for k in PAGE_BLOCK_KEYS))
        )
        if _is_blocked:
            _debug_dir = os.path.join(OUTPUT_DIR, "blocked_debug")
            os.makedirs(_debug_dir, exist_ok=True)
            _debug_file = os.path.join(_debug_dir, f"blocked_{safe_name(url)}_{datetime.now().strftime('%H%M%S')}.html")
            try:
                with open(_debug_file, "w", encoding="utf-8") as _f:
                    _f.write(html[:10000] if html else "(empty)")
                print(f"[BLOCKED] Saved first 10KB to: {_debug_file}")
            except Exception:
                pass
            raise Exception(f"page blocked/empty (title='{_title[:80]}', len={html_size})")

        # 保存cookies到磁盘
        if ENABLE_COOKIE_PERSIST:
            try:
                cookies = driver.get_cookies()
                if cookies:
                    os.makedirs(os.path.dirname(COOKIE_JAR_PATH), exist_ok=True)
                    with open(COOKIE_JAR_PATH, "wb") as f:
                        pickle.dump(cookies, f)
            except Exception:
                pass

        # 抓取页面图片
        if ENABLE_IMAGE_SCRAPE:
            scrape_images_from_driver(driver)

        # 执行解析并获取结构化数据
        res_data = flow(html, url, sk)

        elapsed = (time.time() - t0) * 1000
        with _stats_lock:
            _global_stats["total_fetched"] += 1
            _global_stats["success_fetched"] += 1
            _global_stats["total_bytes"] += html_size
            _global_stats["total_time_ms"] += elapsed

        return True, res_data
    except Exception as e:
        current_retry += 1
        LOG_ENABLE and print(f"[⚠️第{current_retry}次重试]{e}")
        time.sleep(exponential_backoff(current_retry, base_sec=2.0, max_sec=60.0))
        FAIL_COUNTER += 1
        if FAIL_COUNTER >= BREAKER_LIMIT:
            CIRCUIT_BREAKER = True

        elapsed = (time.time() - t0) * 1000
        with _stats_lock:
            _global_stats["total_fetched"] += 1
            _global_stats["fail_fetched"] += 1
            _global_stats["total_time_ms"] += elapsed

        return False, {}
    finally:
        if driver:
            release_driver(driver)

def semi(sk):
    if not os.path.exists(CHROME_PATH) or not os.path.exists(CHROMEDRIVER_PATH):
        return print("❌缺失Chrome或驱动文件")
    driver = None
    baize.full_clean_before_run()
    try:
        opt = _build_chrome_options()
        service = Service(executable_path=CHROMEDRIVER_PATH)
        service.log_output = None
        driver = webdriver.Chrome(service=service, options=opt)

        baize.start_all_defense(driver)
        driver.get("about:blank")
        input("✅浏览器就绪，操作完回车：")
        try:
            driver.current_window_handle
        except NoSuchWindowException:
            return print("❌窗口关闭")
        cu = driver.current_url
        if cu == "about:blank":
            return print("❌空白页")
        time.sleep(get_random_sleep(WAIT_LOAD_MIN, WAIT_LOAD_MAX))
        strip_page(driver, sk)
        html = driver.page_source
        if len(html) < PAGE_MIN_LENGTH or any(k in html for k in PAGE_BLOCK_KEYS):
            return print("❌被拦截")
        flow(html, cu, sk)
    except Exception as e:
        print(f"❌{e}")
        traceback.print_exc()
    finally:
        if driver:
            _destroy_driver(driver)

def local_file():
    sk = input("站点标识：").strip().lower()
    fp = input("文件路径：").strip()
    if not os.path.isfile(fp):
        return print("❌文件不存在")
    html = ""
    for enc in ("utf-8","gbk","gb2312"):
        try:
            with open(fp,"r",encoding=enc,errors="replace") as f:
                html=f.read()
            break
        except UnicodeDecodeError:
            continue
    if not html.strip():
        return print("❌内容为空")
    print(f"✅读取完成 字符数:{len(html)}")
    flow(html,f"本地文件:{os.path.basename(fp)}",sk)

class QDisp:
    def run(self, url, sk):
        res = None
        for _ in range(3):
            h = industrial_spider.fetch_page(url)
            if h:
                res = h
                break
            time.sleep(2)
        res and flow(res, url, sk) or print("🔴重试失败")

q_disp = QDisp()

# ===================== 【穷奇吞图 —— 页面图片抓取模块】 =====================
def _img_url_to_filename(img_url: str) -> str:
    h = hashlib.md5(img_url.encode("utf-8")).hexdigest()[:12]
    ext = ".jpg"
    m = re.search(r"\.(png|jpg|jpeg|gif|webp|bmp|svg)(?:\?|$)", img_url, re.I)
    if m:
        ext = "." + m.group(1).lower()
    return f"{h}{ext}"

def _download_single_img(img_url: str, save_dir: str, timeout: int = IMAGE_TIMEOUT) -> str:
    try:
        resp = img_requests.get(img_url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64) AppleWebKit/537.36",
            "Referer": img_url
        })
        if resp.status_code != 200:
            return ""
        data = resp.content
        if len(data) < IMAGE_MIN_SIZE:
            return ""
        fname = _img_url_to_filename(img_url)
        fpath = os.path.join(save_dir, fname)
        with open(fpath, "wb") as f:
            f.write(data)
        return fpath
    except Exception:
        return ""

def scrape_images_from_driver(driver, save_dir: str = IMAGE_DIR) -> list:
    saved = []
    try:
        imgs = driver.find_elements(By.TAG_NAME, "img")
        if not imgs:
            return saved
        img_urls = []
        for el in imgs:
            src = el.get_attribute("src") or el.get_attribute("data-src") or ""
            if src.startswith("http"):
                img_urls.append(src)
        if not img_urls:
            return saved
        img_urls = list(set(img_urls))
        if LOG_ENABLE:
            print(f"🖼️ 检测到 {len(img_urls)} 张图片，开始下载...")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
            futures = {pool.submit(_download_single_img, u, save_dir): u for u in img_urls}
            for fut in as_completed(futures):
                path = fut.result()
                if path:
                    saved.append(path)
        if LOG_ENABLE:
            print(f"✅ 图片下载完成：{len(saved)}/{len(img_urls)} 张")
    except Exception as e:
        if LOG_ENABLE:
            print(f"⚠️ 图片抓取异常：{e}")
    return saved

def scrape_images_from_html(html: str, save_dir: str = IMAGE_DIR) -> list:
    saved = []
    try:
        img_urls = re.findall(r'<img[^>]+src=["\'](https?://[^"\']+)["\']', html, re.I)
        img_urls += re.findall(r'<img[^>]+data-src=["\'](https?://[^"\']+)["\']', html, re.I)
        if not img_urls:
            return saved
        img_urls = list(set(img_urls))
        if LOG_ENABLE:
            print(f"🖼️ 从HTML检测到 {len(img_urls)} 张图片，开始下载...")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
            futures = {pool.submit(_download_single_img, u, save_dir): u for u in img_urls}
            for fut in as_completed(futures):
                path = fut.result()
                if path:
                    saved.append(path)
        if LOG_ENABLE:
            print(f"✅ 图片下载完成：{len(saved)}/{len(img_urls)} 张")
    except Exception as e:
        if LOG_ENABLE:
            print(f"⚠️ HTML图片抓取异常：{e}")
    return saved

# ===================== GUI 对外接口（重构版——两模式都大幅优化） =====================
def gui_single_crawl(url: str, site_key: str) -> dict:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    current_retry = 0
    if not url.startswith(("http://", "https://")):
        return {}
    success, data = fetch(url, site_key)
    return data if success else {}

def gui_batch_crawl(url_list: list, site_key: str, use_basnake: bool = False,
                    progress_callback=None) -> list:
    """批量抓取入口：
    - use_basnake=True: HTTP并发抓取 + 并行flow处理（快，适合无反爬页面）
    - use_basnake=False: 浏览器驱动池并发 + 逐条处理（慢但能过反爬）
    """
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER, _driver_pool
    result_list = []
    if not url_list:
        return result_list

    if use_basnake:
        # ===== 巴蛇HTTP并发模式（性能优化版） =====
        # 阶段1：多线程并发抓取HTML
        if progress_callback:
            progress_callback(0, len(url_list), "HTTP并发抓取中...")
        html_list = basnake_core.concurrent_crawl(site_key, url_list)

        # 阶段2：对非空HTML并行提取图片
        if ENABLE_IMAGE_SCRAPE:
            non_empty_html = [(i, h) for i, h in enumerate(html_list) if h]
            for i, h in non_empty_html:
                scrape_images_from_html(h)

        # 阶段3：并行flow处理（多核CPU加速文本分析）
        if progress_callback:
            progress_callback(len(html_list) // 2, len(url_list), "文本提取中...")
        if PARALLEL_PARSE_ENABLED:
            result_list = flow_batch_parallel(html_list, url_list, site_key)
        else:
            for idx, html in enumerate(html_list):
                if html:
                    data = flow(html, url_list[idx], site_key)
                    result_list.append(data)
                else:
                    result_list.append({})
                if progress_callback:
                    progress_callback(idx + 1, len(url_list), url_list[idx])

        if progress_callback:
            progress_callback(len(url_list), len(url_list), "完成")
    else:
        # ===== 浏览器增强模式（驱动池并发版） =====
        result_list = [None] * len(url_list)
        completed = [0]
        lock = threading.Lock()

        def _crawl_single(idx_url):
            idx, url = idx_url
            global current_retry
            current_retry = 0
            success, data = fetch(url, site_key)
            with lock:
                completed[0] += 1
                if progress_callback:
                    progress_callback(completed[0], len(url_list), url)
            return idx, (data if success else {})

        max_w = min(MAX_CONCURRENT_BROWSER, len(url_list))
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            futures = {pool.submit(_crawl_single, (i, u)): i for i, u in enumerate(url_list)}
            for fut in as_completed(futures):
                try:
                    idx, data = fut.result()
                    result_list[idx] = data
                except Exception:
                    result_list[futures[fut]] = {}

        # 任务结束后统一关闭驱动池
        close_driver_pool()

    return result_list

def gui_get_html_by_url(url: str) -> str:
    """预览专用：仅获取页面源码"""
    try:
        for _ in range(2):
            h = industrial_spider.fetch_page(url)
            if h:
                return h
            time.sleep(2)
        return ""
    except Exception:
        return ""

# ===== 新增：JSON文件读取导入接口 =====
def gui_import_json_file(json_path: str) -> list:
    """从指定JSON文件读取数据，返回可入库的数据列表"""
    if not os.path.exists(json_path):
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        from config import get_all_valid_data
        return get_all_valid_data(raw, print)
    except Exception as e:
        print(f"[JSON导入] 失败: {e}")
        return []

# 原有命令行入口（保留不变）
def main():
    print("="*50,"\n    穷奇采集抽取系统\n","="*50)
    while True:
        opt = input("\n1单条 2批量 3本地 4半自动 5代理 exit退出\n选择：").strip().lower()
        if opt=="exit":
            break
        if opt not in ("1","2","3","4","5"):
            print("❌无效选项")
            continue
        if opt=="1":
            sk = input("站点：").strip().lower()
            url = input("URL：").strip()
            if not url.startswith(("http://","https://")):
                print("❌URL错误")
                continue
            if input("1浏览器 2巴蛇：").strip()=="2":
                try:
                    h=basnake_core.request(url)
                    flow(h,url,sk)
                except Exception as e:
                    print(f"❌{e}")
            else:
                fetch(url,sk)
        elif opt=="2":
            sk = input("站点：").strip().lower()
            print("逐行输URL，空行结束：")
            ul = []
            while True:
                l = input().strip()
                if not l:
                    break
                if l.startswith(("http://","https://")):
                    ul.append(l)
            if not ul:
                print("❌无URL")
                continue
            if input("1浏览器 2巴蛇批量：").strip()=="2":
                hl = basnake_core.continuous_crawl(sk, ul)
                for i in range(len(hl)):
                    if i < len(hl) and hl[i]:
                        flow(hl[i], ul[i], sk)
            else:
                for u in ul:
                    fetch(u,sk)
                    time.sleep(get_random_sleep(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX))
        elif opt=="3":
            local_file()
        elif opt=="4":
            semi(input("站点(51job)：").strip().lower())
        elif opt=="5":
            sk = input("站点：").strip()
            url = input("URL：").strip()
            if url.startswith(("http://","https://")):
                q_disp.run(url,sk)
            else:
                print("❌URL错误")

if __name__ == "__main__":
    main()
