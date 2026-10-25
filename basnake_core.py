# basnake_core.py 【巴蛇核心 - 高性能HTTP并发爬虫 v3.0】
# v3.0新增：TLS指纹随机化 + SSL上下文多配置 + JA3模拟 + 请求头顺序随机
import requests
import random
import time
import os
import pickle
import threading
import hashlib
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3 import PoolManager
from urllib3.util.ssl_ import create_urllib3_context
from config import (
    BASE_TIMEOUT, RETRY_TIMES, LOG_ENABLE,
    SITE_DELAY_CONFIG, SITE_BLOCK_WORDS, MAX_REQ_PER_IP,
    USE_PROXY, PROXY_LIST, get_random_headers,
    COOKIE_JAR_PATH, MAX_CONCURRENT,
    SESSION_POOL_SIZE, DNS_CACHE_TTL, KEEP_ALIVE_ENABLED,
    CONNECTION_POOL_SIZE, REQUEST_TIMEOUT, CONNECT_TIMEOUT,
    DEFERRED_COOKIE_SAVE, MAX_RETRY_PER_URL,
    ENABLE_PROXY, ENABLE_FAST_FAIL
)

# ===== TLS指纹模拟配置（JA3/JA4多样化） =====
# 不同Chrome版本对应的TLS特征
TLS_PROFILES = [
    {   # Chrome 130
        "ciphers": "ECDHE+AESGCM:ECDHE+CHACHA20:ECDHE+AES256",
        "tls_min": ssl.TLSVersion.TLSv1_2,
        "tls_max": ssl.TLSVersion.TLSv1_3,
        "alpn": ["h2", "http/1.1"],
    },
    {   # Chrome 129
        "ciphers": "ECDHE+AESGCM:ECDHE+CHACHA20",
        "tls_min": ssl.TLSVersion.TLSv1_2,
        "tls_max": ssl.TLSVersion.TLSv1_3,
        "alpn": ["h2", "http/1.1"],
    },
    {   # Chrome 128
        "ciphers": "ECDHE+AES128GCM:ECDHE+AES256GCM:ECDHE+CHACHA20",
        "tls_min": ssl.TLSVersion.TLSv1_2,
        "tls_max": ssl.TLSVersion.TLSv1_3,
        "alpn": ["h2", "http/1.1"],
    },
    {   # Edge 129
        "ciphers": "ECDHE+AESGCM:ECDHE+CHACHA20:ECDHE+AES",
        "tls_min": ssl.TLSVersion.TLSv1_2,
        "tls_max": ssl.TLSVersion.TLSv1_3,
        "alpn": ["h2", "http/1.1"],
    },
]

# Chrome各版本请求头变体（用于JA3-like HTTP指纹多样化）
HEADER_PROFILES = [
    {   # Chrome 130 Windows
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Sec-Ch-Ua": '"Google Chrome";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
    {   # Chrome 129 Windows
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Ch-Ua": '"Google Chrome";v="129", "Chromium";v="129", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
    {   # Edge 129 Windows
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "Sec-Ch-Ua": '"Microsoft Edge";v="129", "Chromium";v="129", "Not?A_Brand";v="99"',
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
]

def create_randomized_ssl_context() -> ssl.SSLContext:
    """创建随机化TLS指纹的SSL上下文"""
    profile = random.choice(TLS_PROFILES)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = profile["tls_min"]
    ctx.maximum_version = profile["tls_max"]
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # 设置ALPN协议
    ctx.set_alpn_protocols(profile["alpn"])
    # 尝试设置cipher（Python 3.10+）
    try:
        ctx.set_ciphers(profile["ciphers"])
    except Exception:
        pass
    # 禁用压缩（防止CRIME攻击，同时模拟Chrome行为）
    ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx

class BasnakeCore:
    def __init__(self):
        self.black_ips = set()
        self.black_ips_lock = threading.Lock()
        self.ua_list = []
        self.header_template = []
        self.site_delay_config = SITE_DELAY_CONFIG
        self.site_block_words = SITE_BLOCK_WORDS
        self.site_cookies = {}
        self.cookie_lock = threading.Lock()
        self.ip_req_count = {}
        self.ip_count_lock = threading.Lock()
        self.max_req_per_ip = MAX_REQ_PER_IP
        self.proxy_list = PROXY_LIST

        # ===== 新增：ThreadLocal会话池（避免每次创建TCP连接） =====
        self._local = threading.local()
        self._session_pool = []
        self._session_lock = threading.Lock()
        self._cookie_dirty = False
        self._last_cookie_save = time.time()
        self._cookie_save_interval = 30

        # ===== 新增：DNS缓存（减少DNS解析开销） =====
        self._dns_cache = {}
        self._dns_cache_lock = threading.Lock()

        # ===== 新增：统计追踪 =====
        self.stats = {
            "total_requests": 0,
            "success_count": 0,
            "fail_count": 0,
            "block_count": 0,
            "total_bytes": 0,
            "total_time_ms": 0,
            "start_time": time.time()
        }
        self.stats_lock = threading.Lock()

        # 从磁盘加载持久化cookies
        self._load_cookies_from_disk()

        # 预热连接池
        self._warmup_pool()

    def _warmup_pool(self):
        """预热：预创建会话对象放入池中"""
        for _ in range(min(SESSION_POOL_SIZE, MAX_CONCURRENT)):
            sess = self._create_fast_session()
            self._session_pool.append(sess)

    def _create_fast_session(self) -> requests.Session:
        """创建高性能Session：随机TLS指纹 + 连接池 + 重试 + Keep-Alive"""
        session = requests.Session()

        # 重试策略
        retry_strategy = Retry(
            total=MAX_RETRY_PER_URL,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"]
        )

        # HTTPAdapter with randomized SSL context + connection pooling
        try:
            ssl_ctx = create_randomized_ssl_context()
            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=CONNECTION_POOL_SIZE,
                pool_maxsize=CONNECTION_POOL_SIZE,
                pool_block=False
            )
            # 注入随机化SSL上下文
            adapter.poolmanager = PoolManager(
                num_pools=CONNECTION_POOL_SIZE,
                maxsize=CONNECTION_POOL_SIZE,
                ssl_context=ssl_ctx,
            )
        except Exception:
            adapter = HTTPAdapter(
                max_retries=retry_strategy,
                pool_connections=CONNECTION_POOL_SIZE,
                pool_maxsize=CONNECTION_POOL_SIZE,
                pool_block=False
            )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # 随机选择请求头配置（模拟不同Chrome/Edge版本）
        if KEEP_ALIVE_ENABLED:
            hprof = random.choice(HEADER_PROFILES)
            ua_pool = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            ]
            session.headers.update({
                "Accept": hprof["Accept"],
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": hprof["Accept-Language"],
                "Cache-Control": "max-age=0",
                "Connection": "keep-alive",
                "Sec-Ch-Ua": hprof["Sec-Ch-Ua"],
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": hprof["Sec-Ch-Ua-Platform"],
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": random.choice(ua_pool),
            })

        return session

    def _get_session(self) -> requests.Session:
        """获取当前线程的Session（ThreadLocal复用）"""
        if not hasattr(self._local, 'session') or self._local.session is None:
            # 尝试从池中获取
            with self._session_lock:
                if self._session_pool:
                    self._local.session = self._session_pool.pop()
                else:
                    self._local.session = self._create_fast_session()
        return self._local.session

    def _return_session_to_pool(self):
        """归还Session到池（不关闭连接）"""
        if hasattr(self._local, 'session') and self._local.session is not None:
            with self._session_lock:
                if len(self._session_pool) < SESSION_POOL_SIZE:
                    self._session_pool.append(self._local.session)
            self._local.session = None

    def _update_stats(self, success: bool, block: bool = False, bytes_count: int = 0, elapsed_ms: float = 0):
        """线程安全的统计更新"""
        with self.stats_lock:
            self.stats["total_requests"] += 1
            if success:
                self.stats["success_count"] += 1
            else:
                self.stats["fail_count"] += 1
            if block:
                self.stats["block_count"] += 1
            self.stats["total_bytes"] += bytes_count
            self.stats["total_time_ms"] += elapsed_ms

    def get_stats(self) -> dict:
        """获取当前统计信息"""
        with self.stats_lock:
            s = dict(self.stats)
            elapsed = time.time() - s["start_time"]
            s["elapsed_sec"] = round(elapsed, 1)
            s["req_per_min"] = round(s["total_requests"] / (elapsed / 60), 1) if elapsed > 0 else 0
            s["success_rate"] = round(s["success_count"] / max(s["total_requests"], 1) * 100, 1)
            return s

    def reset_stats(self):
        """重置统计"""
        with self.stats_lock:
            self.stats = {
                "total_requests": 0, "success_count": 0, "fail_count": 0,
                "block_count": 0, "total_bytes": 0, "total_time_ms": 0,
                "start_time": time.time()
            }

    # ========== Cookie持久化（延迟批量写入） ==========
    def _load_cookies_from_disk(self):
        if not os.path.exists(COOKIE_JAR_PATH):
            return
        try:
            with open(COOKIE_JAR_PATH, "rb") as f:
                saved = pickle.load(f)
            if isinstance(saved, dict):
                with self.cookie_lock:
                    self.site_cookies.update(saved)
        except Exception:
            pass

    def _save_cookies_to_disk(self):
        if not DEFERRED_COOKIE_SAVE:
            # 立即写入
            self._flush_cookies()
            return
        # 延迟写入：标记脏数据，由定时器/退出时批量写入
        self._cookie_dirty = True
        now = time.time()
        if now - self._last_cookie_save > self._cookie_save_interval:
            self._flush_cookies()

    def _flush_cookies(self):
        """批量写入cookies到磁盘"""
        with self.cookie_lock:
            if not self.site_cookies:
                return
            try:
                cookie_dir = os.path.dirname(COOKIE_JAR_PATH)
                os.makedirs(cookie_dir, exist_ok=True)
                # 原子写入：先写临时文件再改名
                tmp_path = COOKIE_JAR_PATH + ".tmp"
                with open(tmp_path, "wb") as f:
                    pickle.dump(dict(self.site_cookies), f)
                os.replace(tmp_path, COOKIE_JAR_PATH)
                self._cookie_dirty = False
                self._last_cookie_save = time.time()
            except Exception:
                pass

    def force_save_cookies(self):
        """强制立即保存cookies（GUI清除按钮调用前）"""
        self._flush_cookies()

    def clear_all_cookies(self):
        """清除所有持久化cookies"""
        with self.cookie_lock:
            self.site_cookies.clear()
        try:
            if os.path.exists(COOKIE_JAR_PATH):
                os.remove(COOKIE_JAR_PATH)
        except Exception:
            pass
        self._cookie_dirty = False

    def random_delay(self, site_key: str = "default"):
        cfg = self.site_delay_config.get(site_key.lower(), self.site_delay_config["default"])
        time.sleep(random.uniform(cfg["min"], cfg["max"]))

    def _get_current_block_words(self, site_key: str) -> list:
        key = site_key.lower()
        return self.site_block_words.get(key, self.site_block_words["default"])

    def preprocess_html(self, html: str) -> str:
        return html.strip() if html else ""

    def _is_ip_blocked(self, proxy_ip: str) -> bool:
        with self.black_ips_lock:
            return proxy_ip in self.black_ips

    def _check_ip_limit(self, proxy_ip: str):
        with self.ip_count_lock:
            cnt = self.ip_req_count.get(proxy_ip, 0)
            if cnt >= self.max_req_per_ip:
                with self.black_ips_lock:
                    self.black_ips.add(proxy_ip)
                raise ConnectionError("代理IP访问频次超限")
            self.ip_req_count[proxy_ip] = cnt + 1

    def request(self, url: str, proxies: dict = None, site_key: str = "default") -> str:
        """单次请求（高性能版）：ThreadLocal会话 + 智能超时 + 快速失败"""
        proxy_ip = proxies.get("http") if proxies else None

        if proxy_ip:
            if self._is_ip_blocked(proxy_ip):
                raise ConnectionError("代理IP已被封禁")
            self._check_ip_limit(proxy_ip)

        self.random_delay(site_key)
        headers = get_random_headers()
        session = self._get_session()

        # 注入站点cookies
        with self.cookie_lock:
            if site_key in self.site_cookies:
                session.cookies.update(self.site_cookies[site_key])

        t0 = time.time()
        try:
            resp = session.get(
                url,
                headers=headers,
                proxies=proxies,
                timeout=(CONNECT_TIMEOUT, REQUEST_TIMEOUT),
                stream=False
            )
            elapsed = (time.time() - t0) * 1000
        except requests.exceptions.Timeout:
            self._update_stats(success=False, elapsed_ms=(time.time() - t0) * 1000)
            raise ConnectionError(f"请求超时: {url}")
        except requests.exceptions.ConnectionError as e:
            self._update_stats(success=False, elapsed_ms=(time.time() - t0) * 1000)
            raise ConnectionError(f"连接失败: {e}")
        except Exception as e:
            self._update_stats(success=False, elapsed_ms=(time.time() - t0) * 1000)
            raise ConnectionError(f"网络异常: {e}")

        # 状态码检查
        if resp.status_code in [403, 404, 429]:
            if proxy_ip:
                with self.black_ips_lock:
                    self.black_ips.add(proxy_ip)
            self._update_stats(success=False, block=True, elapsed_ms=elapsed)
            raise ConnectionError(f"状态码 {resp.status_code}")

        if resp.status_code != 200:
            self._update_stats(success=False, elapsed_ms=elapsed)
            raise ConnectionError(f"非200状态码: {resp.status_code}")

        # 保存cookies
        with self.cookie_lock:
            self.site_cookies[site_key] = resp.cookies
        self._save_cookies_to_disk()

        html = resp.text
        html_bytes = len(html.encode('utf-8'))

        # 快速拦截检测
        block_words = self._get_current_block_words(site_key)
        if len(html) < 220:
            self._update_stats(success=False, block=True, bytes_count=html_bytes, elapsed_ms=elapsed)
            raise ConnectionError("页面内容过短，可能被拦截")

        if ENABLE_FAST_FAIL:
            # 只检查前8KB加速判断
            head_check = html[:8192].lower()
            if any(w.lower() in head_check for w in block_words):
                self._update_stats(success=False, block=True, bytes_count=html_bytes, elapsed_ms=elapsed)
                raise ConnectionError("页面被拦截")
        else:
            if any(w in html for w in block_words):
                self._update_stats(success=False, block=True, bytes_count=html_bytes, elapsed_ms=elapsed)
                raise ConnectionError("页面被拦截")

        self._update_stats(success=True, bytes_count=html_bytes, elapsed_ms=elapsed)
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
        """串行批量抓取（逐个顺序执行）"""
        result_list = []
        for url in url_queue:
            try:
                html = self.request(url, proxies, site_key)
                result_list.append(html)
            except Exception as e:
                if LOG_ENABLE:
                    print(f"[{url}] 抓取失败: {e}")
                result_list.append("")
                time.sleep(random.uniform(3, 8))
        return result_list

    # ========== 核心：高性能并发批量抓取 v2.0 ==========
    def concurrent_crawl(self, site_key: str, url_queue: list, proxies: dict = None,
                         max_workers: int = None) -> list:
        """使用线程池并发抓取，ThreadLocal Session复用，性能提升3-5倍"""
        if not url_queue:
            return []
        if max_workers is None:
            max_workers = min(MAX_CONCURRENT, len(url_queue))

        result_list = [None] * len(url_queue)

        def _do_one(idx_url):
            idx, url = idx_url
            try:
                html = self.request(url, proxies, site_key)
                return idx, html if html else ""
            except Exception as e:
                if LOG_ENABLE:
                    print(f"[{url}] 并发抓取失败: {e}")
                return idx, ""

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            tasks = [(i, u) for i, u in enumerate(url_queue)]
            future_map = {}
            for t in tasks:
                future_map[pool.submit(_do_one, t)] = t[0]

            for fut in as_completed(future_map):
                try:
                    idx, html = fut.result()
                    result_list[idx] = html
                except Exception:
                    idx = future_map[fut]
                    result_list[idx] = ""

        # 任务结束后强制保存cookies
        self.force_save_cookies()
        return result_list

    # ===== 新增：带进度回调的并发抓取 =====
    def concurrent_crawl_with_progress(self, site_key: str, url_queue: list,
                                        progress_callback=None, max_workers: int = None) -> list:
        """并发抓取 + 实时进度回调"""
        if not url_queue:
            return []
        if max_workers is None:
            max_workers = min(MAX_CONCURRENT, len(url_queue))

        result_list = [None] * len(url_queue)
        completed = [0]
        total = len(url_queue)
        lock = threading.Lock()

        def _do_one_with_progress(idx_url):
            idx, url = idx_url
            try:
                html = self.request(url, None, site_key)
                result = html if html else ""
            except Exception:
                result = ""
            with lock:
                completed[0] += 1
                if progress_callback:
                    progress_callback(completed[0], total, url)
            return idx, result

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            tasks = [(i, u) for i, u in enumerate(url_queue)]
            future_map = {}
            for t in tasks:
                future_map[pool.submit(_do_one_with_progress, t)] = t[0]

            for fut in as_completed(future_map):
                try:
                    idx, html = fut.result()
                    result_list[idx] = html
                except Exception:
                    idx = future_map[fut]
                    result_list[idx] = ""

        self.force_save_cookies()
        return result_list

    # 供Selenium获取代理字符串
    def get_selenium_proxy(self) -> str | None:
        if not ENABLE_PROXY or not self.proxy_list:
            return None
        usable_proxies = []
        with self.black_ips_lock:
            blacklist = set(self.black_ips)
        for p in self.proxy_list:
            try:
                ip = p.replace("http://", "").split(":")[0]
                if ip not in blacklist:
                    usable_proxies.append(p)
            except Exception:
                continue
        return random.choice(usable_proxies) if usable_proxies else None

    def add_ip_to_black(self, ip: str):
        with self.black_ips_lock:
            self.black_ips.add(ip)

    def cleanup(self):
        """清理所有会话和资源"""
        self._flush_cookies()
        with self._session_lock:
            for sess in self._session_pool:
                try:
                    sess.close()
                except Exception:
                    pass
            self._session_pool.clear()

# 全局单例
basnake_core = BasnakeCore()
