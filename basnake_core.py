import requests
import random
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import (
    BASE_TIMEOUT, RETRY_TIMES, LOG_ENABLE,
    SITE_DELAY_CONFIG, SITE_BLOCK_WORDS, MAX_REQ_PER_IP,
    USE_PROXY, PROXY_LIST, get_random_headers
)

class BasnakeCore:
    def __init__(self):
        self.black_ips = set()
        self.ua_list = []
        self.header_template = []
        self.site_delay_config = SITE_DELAY_CONFIG
        self.site_block_words = SITE_BLOCK_WORDS
        self.site_cookies = {}
        self.ip_req_count = dict()
        self.max_req_per_ip = MAX_REQ_PER_IP
        self.session = self._init_session()
        self.proxy_list = PROXY_LIST

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=RETRY_TIMES,
            backoff_factor=1.2,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def random_delay(self, site_key: str = "default"):
        cfg = self.site_delay_config.get(site_key.lower(), self.site_delay_config["default"])
        time.sleep(random.uniform(cfg["min"], cfg["max"]))

    def _get_current_block_words(self, site_key: str) -> list:
        key = site_key.lower()
        return self.site_block_words.get(key, self.site_block_words["default"])

    def preprocess_html(self, html: str) -> str:
        return html.strip()

    def request(self, url: str, proxies: dict = None, site_key: str = "default") -> str:
        proxy_ip = proxies.get("http") if proxies else None
        if proxy_ip and proxy_ip in self.black_ips:
            raise ConnectionError("代理IP已被封禁")
        if proxy_ip:
            cnt = self.ip_req_count.get(proxy_ip, 0)
            if cnt >= self.max_req_per_ip:
                self.black_ips.add(proxy_ip)
                raise ConnectionError("代理IP访问频次超限")
            self.ip_req_count[proxy_ip] = cnt + 1
        self.random_delay(site_key)
        headers = get_random_headers()
        if site_key in self.site_cookies:
            self.session.cookies.update(self.site_cookies[site_key])
        try:
            resp = self.session.get(url, headers=headers, proxies=proxies, timeout=BASE_TIMEOUT)
        except Exception as e:
            raise ConnectionError(f"网络异常: {e}")
        if resp.status_code in [403, 404, 429]:
            if proxy_ip:
                self.black_ips.add(proxy_ip)
            raise ConnectionError(f"状态码 {resp.status_code}")
        self.site_cookies[site_key] = resp.cookies
        html = resp.text.strip()
        block_words = self._get_current_block_words(site_key)
        is_block = any(w in html for w in block_words)
        if len(html) < 220 or is_block:
            raise ConnectionError("页面被拦截")
        return self.preprocess_html(html)

    def traverse_page(self, site_key: str, start_url: str, page_limit: int = 0, proxies: dict = None) -> list:
        page_data_list = []
        current_url = start_url
        current_page = 0
        while True:
            if page_limit > 0 and current_page >= page_limit:
                break
            try:
                html = self.request(current_url, proxies, site_key)
                page_data_list.append(html)
                current_page += 1
                break
            except Exception as e:
                if LOG_ENABLE:
                    print(f"分页异常: {e}")
                break
        return page_data_list

    def continuous_crawl(self, site_key: str, url_queue: list, proxies: dict = None) -> list:
        result_list = []
        for url in url_queue:
            try:
                html = self.request(url, proxies, site_key)
                result_list.append(html)
            except Exception as e:
                if LOG_ENABLE:
                    print(f"[{url}] 抓取失败: {e}")
                result_list.append("")
                time.sleep(random.uniform(10, 16))
        return result_list

    # 新增：供Selenium获取代理字符串
    def get_selenium_proxy(self) -> str | None:
        if not USE_PROXY or not self.proxy_list:
            return None
        usable_proxies = []
        for p in self.proxy_list:
            try:
                ip = p.replace("http://", "").split(":")[0]
                if ip not in self.black_ips:
                    usable_proxies.append(p)
            except Exception:
                continue
        return random.choice(usable_proxies) if usable_proxies else None

    # 新增：IP加入黑名单
    def add_ip_to_black(self, ip: str):
        self.black_ips.add(ip)

# 全局单例（保留原有）
basnake_core = BasnakeCore()