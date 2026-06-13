# 穷奇之口 qiongqi_maw.py 最终修复版（离线驱动+配置统一）
import os, json, time, random, traceback, tkinter
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchWindowException
from datetime import datetime
from config import *
set_selenium_env()
from baize import baize
from basnake_core import basnake_core
from taotie_filter import taotie_filter
from zhujian_watch import zhujian_eye
from data_extract import extract_main
import industrial_spider

current_retry = FAIL_COUNTER = 0
CIRCUIT_BREAKER = False
import data_extract
# 修复变量名：EXTRACT → EXTRACT_ROUND
data_extract.TOTAL_ROUND = EXTRACT_ROUND

def recycle_driver(d):
    try:
        d.quit()
    except:
        pass

def show_tip():
    r = tkinter.Tk()
    r.withdraw()
    r.attributes("-topmost", True)
    res = tkinter.messagebox.askokcancel(VERIFY_DIALOG_TITLE, VERIFY_DIALOG_MSG)
    r.destroy()
    return res

# ========== 修改后 export_res 函数：仅写入原始 clean_text_2 ==========
def export_res(name, url, fjson, blocks, ext, ok, err, text1="", text2=""):
    ts = datetime.now().strftime(TIME_FORMAT)
    base = f"{name}_{ts}"
    tp, jp = os.path.join(OUTPUT_DIR, f"{base}.txt"), os.path.join(OUTPUT_DIR, f"{base}.json")
    with open(tp, "w", encoding="utf-8") as f:
        f.write(EXPORT_TPL_SOURCE.format(url=url, site=name, time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))+EXPORT_TPL_SPLIT+fjson)
    
    # 直接写入原始 text2，不做任何格式化处理
    out = {
        "clean_text_2": text2
    }
    # 写入JSON，保留原始文本格式
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    
    LOG_ENABLE and (print(f"📄{tp}"),print(f"📊{jp}"))
    # 返回原有结构化数据（程序内部逻辑不变）
    return ext
# ==================================================

# ========== flow 函数：原样保留 ==========
def flow(html, url, sk):
    # 第一轮清洗：饕餮 文本净化、分块处理
    blk = taotie_filter.process(html)
    first_clean_text = "\n".join(blk)

    # 第二轮清洗：诸犍之眼 结构化文本重建
    fp = zhujian_eye.parse_full(html)
    fj = zhujian_eye.to_json_input(fp)
    second_clean_text = fp

    # 原有抽取、质检逻辑不变
    ext = extract_main(zhujian_eye.to_json_input("\n".join(blk)), sk)
    ok, err = zhujian_eye.watch_batch([ext])
    
    # 传递两轮清洗文本至导出函数
    extract_data = export_res(
        safe_name(sk), url, fjson=fj, blocks=blk, 
        ext=ext, ok=ok, err=err,
        text1=first_clean_text,
        text2=second_clean_text
    )
    LOG_ENABLE and print("🎉处理完成\n")
    return extract_data
# ==================================================

def fetch(url, sk) -> tuple[bool, dict]:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    if CIRCUIT_BREAKER or not os.path.exists(CHROME_PATH) or not os.path.exists(CHROMEDRIVER_PATH):
        CIRCUIT_BREAKER and LOG_ENABLE and print("[🔴熔断]")
        LOG_ENABLE and print("[❌缺失Chrome或驱动文件]")
        return False, {}
    driver = None
    baize.full_clean_before_run()
    try:
        opt = Options()
        opt.binary_location = CHROME_PATH

        # 拆分参数 + 补充离线禁用联网配置
        opt.add_argument("--window-size=1920,1080")
        opt.add_argument("--start-maximized")
        opt.add_argument("--disable-blink-features=AutomationControlled")
        opt.add_argument("--incognito")
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")
        opt.add_argument("--ignore-certificate-errors")
        # 彻底禁用后台联网、同步、扩展
        opt.add_argument("--disable-background-networking")
        opt.add_argument("--disable-sync")
        opt.add_argument("--disable-extensions")
        opt.add_argument("--disable-default-apps")

        opt.add_experimental_option("excludeSwitches", ["enable-automation"])
        opt.add_experimental_option("useAutomationExtension", False)
        opt.add_argument(f"user-agent={get_random_ua()}")

        # 【核心】使用本地驱动，关闭驱动日志与版本联网校验
        service = Service(executable_path=CHROMEDRIVER_PATH)
        service.log_output = None
        driver = webdriver.Chrome(service=service, options=opt)

        baize.start_all_defense(driver)
        driver.get(url)
        time.sleep(get_random_sleep(BASE_WAIT_MIN,BASE_WAIT_MAX))
        if any(k in driver.title.lower() or k in driver.current_url.lower() for k in VERIFY_KEYWORDS):
            if not show_tip():
                raise Exception("任务终止")
            driver.refresh()
            time.sleep(get_random_sleep(WAIT_VERIFY_MIN,WAIT_VERIFY_MAX))
        time.sleep(get_random_sleep(WAIT_LOAD_MIN,WAIT_LOAD_MAX))
        for r in SCROLL_RATIO_LIST:
            driver.execute_script(f"window.scrollTo(0,document.body.scrollHeight*{r})")
            time.sleep(get_random_sleep(SCROLL_MIN,SCROLL_MAX))
        html = driver.page_source
        if len(html) < PAGE_MIN_LENGTH or any(k in html for k in PAGE_BLOCK_KEYS):
            raise Exception("拦截/空内容")
        # 执行解析并获取结构化数据
        res_data = flow(html, url, sk)
        return True, res_data
    except Exception as e:
        current_retry += 1
        LOG_ENABLE and print(f"[⚠️第{current_retry}次重试]{e}")
        time.sleep(get_random_sleep(ERROR_SLEEP_MIN,ERROR_SLEEP_MAX))
        FAIL_COUNTER += 1
        if FAIL_COUNTER >= BREAKER_LIMIT:
            CIRCUIT_BREAKER = True
        return False, {}
    finally:
        recycle_driver(driver)

def semi(sk):
    if not os.path.exists(CHROME_PATH) or not os.path.exists(CHROMEDRIVER_PATH):
        return print("❌缺失Chrome或驱动文件")
    driver = None
    baize.full_clean_before_run()
    try:
        opt = Options()
        opt.binary_location = CHROME_PATH
        opt.add_argument("--window-size=1920,1080")
        opt.add_argument("--start-maximized")
        opt.add_argument("--disable-blink-features=AutomationControlled")
        opt.add_argument("--incognito")
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")
        opt.add_argument("--disable-background-networking")
        opt.add_argument("--disable-sync")
        opt.add_argument("--disable-extensions")

        opt.add_experimental_option("excludeSwitches", ["enable-automation"])
        opt.add_experimental_option("useAutomationExtension", False)
        opt.add_argument(f"user-agent={get_random_ua()}")

        # 本地驱动加载
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
        time.sleep(get_random_sleep(WAIT_LOAD_MIN,WAIT_LOAD_MAX))
        html = driver.page_source
        if len(html) < PAGE_MIN_LENGTH or any(k in html for k in PAGE_BLOCK_KEYS):
            return print("❌被拦截")
        flow(html, cu, sk)
    except Exception as e:
        print(f"❌{e}")
        traceback.print_exc()
    finally:
        recycle_driver(driver)

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
        except:
            continue
    if not html.strip():
        return print("❌内容为空")
    print(f"✅读取完成 字符数:{len(html)}")
    flow(html,f"本地文件:{os.path.basename(fp)}",sk)

class QDisp:
    def run(self,url,sk):
        res = None
        for _ in range(3):
            h = industrial_spider.fetch_page(url)
            if h:
                res=h
                break
            time.sleep(3)
        res and flow(res,url,sk) or print("🔴重试失败")

q_disp = QDisp()

# ===================== GUI 对外接口（完全不变） =====================
def gui_single_crawl(url: str, site_key: str) -> dict:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    current_retry = 0
    if not url.startswith(("http://", "https://")):
        return {}
    success, data = fetch(url, site_key)
    return data if success else {}

def gui_batch_crawl(url_list: list, site_key: str, use_basnake: bool = False) -> list:
    global current_retry, FAIL_COUNTER, CIRCUIT_BREAKER
    result_list = []
    if not url_list:
        return result_list
    if use_basnake:
        # 巴蛇批量模式
        html_list = basnake_core.continuous_crawl(site_key, url_list)
        for idx, html in enumerate(html_list):
            if html:
                data = flow(html, url_list[idx], site_key)
                result_list.append(data)
            else:
                result_list.append({})
    else:
        # 浏览器渲染模式
        for url in url_list:
            current_retry = 0
            success, data = fetch(url, site_key)
            result_list.append(data if success else {})
            time.sleep(get_random_sleep(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX))
    return result_list

def gui_get_html_by_url(url: str) -> str:
    """预览专用：仅获取页面源码"""
    try:
        for _ in range(3):
            h = industrial_spider.fetch_page(url)
            if h:
                return h
            time.sleep(3)
        return ""
    except:
        return ""

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
                hl = basnake_core.continuous_crawl(sk,ul)
                for i in range(len(hl)):
                    if i < len(hl) and hl[i]:
                        flow(hl[i],ul[i],sk)
            else:
                for u in ul:
                    fetch(u,sk)
                    time.sleep(get_random_sleep(BATCH_INTERVAL_MIN,BATCH_INTERVAL_MAX))
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