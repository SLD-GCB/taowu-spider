# cli_entry.py
"""
潜龙2.0.0 纯接口命令行工具
说明：
- 取消交互终端，改为通过标准输入接收JSON参数
- 输入格式：{"content": "待抽取的招聘文本"}
- 输出格式：直接打印 extract_fields 返回的JSON结果
- 程序执行一次，加载模型 → 抽取 → 输出 → 退出
"""
import sys
import json
from infer_api import init_service, extract_fields

def main():
    # 1. 一次性初始化模型（仅在启动时加载一次）
    init_service()

    # 2. 从标准输入读取JSON参数
    raw_input = sys.stdin.read().strip()
    if not raw_input:
        print(json.dumps({"error": "No input provided"}, ensure_ascii=False))
        return

    # 3. 解析输入
    try:
        data = json.loads(raw_input)
        text_content = data.get("content", "")
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid JSON format"}, ensure_ascii=False))
        return

    # 4. 调用你预留的抽取接口
    result = extract_fields(text_content)

    # 5. 直接输出JSON结果（无任何多余日志/提示符）
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()