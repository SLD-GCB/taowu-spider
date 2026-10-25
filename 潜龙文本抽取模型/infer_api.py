from infer_core import load_resources, infer_core, get_field_tags

def init_service():
    """初始化服务（程序启动调用一次）"""
    load_resources()

def extract_fields(text: str) -> dict:
    """
    给外部插件调用的字段抽取接口
    :param text: 原始招聘文本
    :return: {字段名: 抽取到的原文片段}
    """
    return infer_core(text)

def predict_tags(text: str) -> list:
    """给眼睛插件调用的字段预判接口"""
    return get_field_tags(text)