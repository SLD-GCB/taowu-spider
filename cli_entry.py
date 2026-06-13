# cli_entry.py
from Spider_instructor import SpiderInstructor

# 全局实例
spider_main = SpiderInstructor()

def run_task(site_key: str, url: str) -> dict:
    """对外统一调用接口"""
    return spider_main.single_task(site_key, url)

def run_batch(site_key: str, url_list: list) -> list:
    return spider_main.batch_task(site_key, url_list)

if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        sk = sys.argv[1]
        url = sys.argv[2]
        print(run_task(sk, url))