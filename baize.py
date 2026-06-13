# baize.py 轻量防护版 - 解决页面无内容+代码报错，仅隐藏爬虫特征，不拦截正常接口
import os
import shutil
import time
import subprocess
from config import BLOCK_URL_RULES, LIGHT_DEFENSE_JS

class BaiZeCleaner:
    def __init__(self):
        self.chrome_user_data = os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data")

    def kill_chrome_process(self):
        try:
            subprocess.run("taskkill /f /im chromedriver.exe", shell=True, capture_output=True)
            time.sleep(1)
        except Exception:
            pass

    def clear_cache(self):
        try:
            cache_path = os.path.join(self.chrome_user_data, "Default", "Code Cache")
            if os.path.exists(cache_path):
                shutil.rmtree(cache_path)
        except Exception:
            pass
        time.sleep(1)

    def cdp_network_block(self, driver):
        try:
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCK_URL_RULES})
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

    def runtime_clean(self, driver):
        try:
            driver.delete_all_cookies()
        except Exception:
            pass

    def inject_script(self, driver):
        try:
            driver.execute_script(LIGHT_DEFENSE_JS)
        except Exception:
            pass

    # 兼容旧调用名
    def full_clean_before_run(self):
        self.kill_chrome_process()
        self.clear_cache()

    def start_all_defense(self, driver):
        self.runtime_clean(driver)
        self.cdp_network_block(driver)
        self.inject_script(driver)

# 全局单例
baize = BaiZeCleaner()