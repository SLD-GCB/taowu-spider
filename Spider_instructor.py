from config import RISK_LEVEL_MAP, RETRY_TIMES, load_selector_lib
from industrial_spider import fetch_main
import data_extract

class SpiderInstructor:
    def __init__(self):
        self.risk_map = RISK_LEVEL_MAP
        self.retry_times = RETRY_TIMES

    def get_site_risk(self, site_key: str) -> int:
        selector_data = load_selector_lib()
        site_cfg = selector_data.get(site_key, {})
        risk_tag = site_cfg.get("risk_level", "low")
        return self.risk_map.get(risk_tag, 1)

    def single_task(self, site_key: str, target_url: str) -> dict:
        print(f"【梼杌本体】任务启动 | 站点:{site_key}")
        risk_level = self.get_site_risk(site_key)
        html = ""
        for i in range(self.retry_times):
            html = fetch_main(target_url, wait_second=risk_level)
            if html:
                break
            print(f"重试第 {i+1} 次...")
        if not html:
            return {"status": "fail", "msg": "页面获取失败", "data": {}}
        extract_data = data_extract.extract_main(html, site_key)
        return {"status": "success", "msg": "执行完成", "data": extract_data}

    def batch_task(self, site_key: str, url_list: list) -> list:
        result_list = []
        for url in url_list:
            res = self.single_task(site_key, url)
            if res.get("status") == "success":
                result_list.append(res["data"])
        return result_list

spider_instructor = SpiderInstructor()