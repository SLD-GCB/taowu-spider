import json
import re
from config import FIELD_LIST

# 过滤装饰符号、制表符等干扰字符，保留正常文字、数字、常用标点
SYMBOL_PAT = re.compile(r'[┌┐└┘─│㈠㈡㈢㈣㈤㈥〓「」『』《》〔〕├┤\t\r]+')
# 合并连续空格
SPACE_PAT = re.compile(r'\s+')

def simple_clean(text: str) -> str:
    """仅做：清杂符号 + 合并空白 + 行去重"""
    if not text:
        return ""
    # 移除特殊符号
    text = SYMBOL_PAT.sub("", text)
    # 合并多余空格
    text = SPACE_PAT.sub(" ", text)
    # 行去重
    lines = list(set(text.splitlines()))
    text = "\n".join(lines)
    return text.strip()

def rule_extract(text_list):
    field_data = {k: "不限" for k in FIELD_LIST}
    raw_text = "\n".join(text_list)
    # 轻量清洗后的完整文本存入岗位职责
    field_data["岗位职责"] = simple_clean(raw_text)
    return field_data

def build_output(field_data: dict, raw_html: str, site_key: str, source_url: str) -> dict:
    qianlong_res = {
        "岗位名称": field_data["岗位名称"],
        "工作地点": field_data["工作地点"],
        "薪资范围": field_data["薪资范围"],
        "学历要求": field_data["学历要求"],
        "工作经验": field_data["工作经验"],
        "岗位职责": field_data["岗位职责"],
        "福利信息": field_data["福利信息"],
        "联系方式": field_data["联系方式"],
        "job_url": source_url,
        "source": site_key
    }

    has_valid = qianlong_res["岗位职责"].strip() not in ("", "不限")
    normal_data = [qianlong_res] if has_valid else []
    error_data = [] if has_valid else [qianlong_res]

    return {
        "zhujian_full_content_json": json.dumps(
            {"content": f"【原始页面】\n{raw_html}", "site_key": site_key, "source_url": source_url},
            ensure_ascii=False
        ),
        "qianlong_extract_result": qianlong_res,
        "normal_data": normal_data,
        "error_data": error_data
    }

def extract_main(raw_html_text, site_key="zhilian", source_url="本地文件"):
    chunks = [raw_html_text]
    field_result = rule_extract(chunks)
    return build_output(field_result, raw_html_text, site_key, source_url)