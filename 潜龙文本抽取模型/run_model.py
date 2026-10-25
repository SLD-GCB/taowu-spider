# run_model.py 改造后版本
import sys
import json
from infer_api import init_service, extract_fields

def main():
    # 初始化模型
    init_service()

    # 一次性读取全部传入参数
    raw_data = sys.stdin.read().strip()
    if not raw_data:
        print(json.dumps({"error": "empty input"}, ensure_ascii=False))
        return

    # 解析入参 JSON
    try:
        input_json = json.loads(raw_data)
        content = input_json.get("content", "")
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid json format"}, ensure_ascii=False))
        return

    # 调用抽取逻辑
    try:
        result = extract_fields(content)
        # 输出纯JSON，无任何多余打印
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))

if __name__ == "__main__":
    main()