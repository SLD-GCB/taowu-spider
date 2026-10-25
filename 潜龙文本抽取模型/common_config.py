import os
import re
import torch

# ---------------------- 设备配置 ----------------------
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ---------------------- 模型超参 ----------------------
VOCAB_SIZE = 8000
EMBED_DIM = 80
CLASS_NUM = 8
DROPOUT_RATE = 0.4
MAX_SEQ_LEN = 600
THRESHOLD = 0.3  # 模型输出阈值

# ---------------------- 字段映射 ----------------------
FIELD_MAP = [
    "工作地点",
    "薪资范围",
    "岗位名称",
    "学历要求",
    "工作经验",
    "岗位职责",
    "福利信息",
    "联系方式"
]

# ---------------------- 路径配置（和你目录对齐） ----------------------
BASE_DIR = os.path.join(os.getcwd(), "model_out", "eye_plugin")
VOCAB_PATH = os.path.join(BASE_DIR, "dynamic_vocab.json")
WEIGHT_PATH = os.path.join(BASE_DIR, "eye_model.pth")

# ---------------------- 规则匹配库 ----------------------
RULE_PATTERNS = {
    "学历要求": re.compile(r"(大专|本科|硕士|博士|中专|高中|学历|文凭)"),
    "工作经验": re.compile(r"(\d+年|1-3年|3-5年|经验|应届生|无经验)"),
    "岗位职责": re.compile(r"(负责|主要工作|岗位内容|工作职责|开展|执行)"),
    "福利信息": re.compile(r"(五险一金|社保|公积金|餐补|交通补贴|住宿|团建|节日福利|带薪休假)"),
    "联系方式": re.compile(r"(电话|微信|手机号|联系|咨询|投递简历)")
}