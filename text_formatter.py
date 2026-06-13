import re

def format_job_content(raw_text: str) -> str:
    """
    爬虫文本格式统一修整器
    1. 清除网页残留无效换行、回车、制表符
    2. 压缩连续空格
    3. 按招聘大类关键词分段换行
    4. 按顿号/分号拆分成单行条目
    5. 清理多余空行，输出规整排版
    """
    if not isinstance(raw_text, str):
        return ""

    # ========== 第一步：清洗原始脏字符 ==========
    # 清除所有 \r 回车、\n 无效换行、\t 制表符
    text = re.sub(r"[\r\n\t]", " ", raw_text.strip())
    # 把多个连续空格压缩为单个空格
    text = re.sub(r"\s+", " ", text)

    # ========== 第二步：大类关键词强制换行分隔 ==========
    split_keywords = [
        "岗位职责", "工作内容",
        "任职要求", "岗位要求",
        "学历要求", "工作经验",
        "福利待遇", "薪资待遇", "薪资",
        "工作地点", "地址"
    ]
    for kw in split_keywords:
        text = text.replace(kw, f"\n{kw}")

    # ========== 第三步：按标点拆分细项（一条一行） ==========
    # 中文分号、英文分号、顿号 全部替换为换行
    text = re.sub(r"[；;、]", r"\n", text)

    # ========== 第四步：清理多余连续空行 ==========
    text = re.sub(r"\n+", r"\n", text)
    # 去除首尾空行
    final_text = text.strip("\n")

    return final_text