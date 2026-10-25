# test_local.py
from infer_api import init_service, extract_fields

def main():
    print("===== 潜龙模型 本地功能测试 =====")
    print("正在初始化模型...")
    # 初始化模型
    init_service()
    print("模型初始化完成！\n")

    # 测试文本（可自行替换长短文本）
    test_text_1 = "招聘Python开发工程师，工作地点北京，薪资15k-25k，要求本科，3年以上经验，负责后端开发，五险一金、年终奖，联系电话13800001111"
    test_text_2 = "炼狱级反爬靶场，测试专用内容，联系号码17808831243"

    print("【测试用例1】")
    res1 = extract_fields(test_text_1)
    for k, v in res1.items():
        print(f"{k}: {v}")

    print("\n【测试用例2】")
    res2 = extract_fields(test_text_2)
    for k, v in res2.items():
        print(f"{k}: {v}")

    print("\n===== 本地测试结束，模型运行正常 =====")

if __name__ == "__main__":
    main()