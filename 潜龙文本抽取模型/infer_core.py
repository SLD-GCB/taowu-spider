import re

vocab_global = None
model_global = None
MAX_SEQ_LEN = 512
FIELD_MAP = [
    "岗位名称", "工作地点", "薪资范围", "学历要求",
    "工作经验", "岗位职责", "福利信息", "联系方式"
]


def raw_text_clean(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return text.strip()


def load_resources():
    global vocab_global, model_global
    vocab_global = {"<UNK>": 0}
    model_global = "dummy_model"


def extract_field_content(text: str, field: str) -> str:
    text_clean = raw_text_clean(text)

    # ── 岗位名称 ──
    if field == "岗位名称":
        # 1. "岗位名称：" 或 "职位名称：" 引导（优先，避免标签误匹配）
        m = re.search(r"(?:岗位名称|职位名称|招聘职位|职位)[：:]\s*([^，。\n]{2,30})", text_clean)
        if m:
            return m.group(1).strip()
        # 2. 【标记】内容（跳过标签本身：岗位名称/职位名称/公司信息等）
        m = re.search(r"【([^】]{2,30})】", text_clean)
        if m:
            val = m.group(1).strip()
            # 跳过标签类内容
            if val not in ("岗位名称", "职位名称", "岗位基础信息", "岗位职责与要求",
                           "岗位职责", "职位描述", "工作内容", "公司信息", "公司名称",
                           "企业名称", "福利信息", "福利待遇", "联系方式", "页面全文",
                           "工作地点", "薪资范围", "学历要求", "工作经验"):
                return val
        for sep in ["，", ",", "。", ".", "；"]:
            if sep in text_clean:
                first = text_clean.split(sep)[0].strip()
                # 排除含"地点"、"薪资"等关键词的片段
                if (2 <= len(first) <= 30 and
                    not any(kw in first for kw in ["地点", "薪资", "学历", "经验", "福利", "联系"])):
                    return first
        return ""

    # ── 工作地点 ──
    elif field == "工作地点":
        # 1. 引导词 + 城市: "工作地点深圳南山区", "base上海浦东"
        m = re.search(
            r"(?:工作地点|上班地点|办公地点|工作地址|base|Base|BASE)"
            r"(?:[：:是为在]\s*)?"
            r"([一-龥]{2,10}?(?:市|区|县|省|新区|开发区|高新区|街道|镇)?)",
            text_clean)
        if m and m.group(1) and len(m.group(1)) >= 2:
            return m.group(1).strip()
        # 2. "位于/坐落于 XX市"
        m = re.search(r"(?:位于|坐落于)\s*([一-龥]{2,10}?(?:市|区|县))", text_clean)
        if m and m.group(1):
            return m.group(1).strip()
        # 3. 纯粹的城市名（常见城市名单兜底）
        cities = ['北京','上海','广州','深圳','杭州','南京','成都','武汉','重庆',
                  '天津','苏州','西安','长沙','郑州','东莞','青岛','沈阳','宁波',
                  '昆明','合肥','大连','福州','厦门','济南','无锡','佛山','珠海',
                  '中山','惠州','温州','常州','嘉兴','南通','泉州','绍兴','烟台']
        for city in sorted(cities, key=len, reverse=True):
            if city in text_clean[:300]:
                return city
        return ""

    # ── 薪资范围 ──
    elif field == "薪资范围":
        # 1. 双K格式: 15K-20K, 8k-12k, 15-20K/月
        m = re.search(
            r'(\d{1,3}(?:\.\d)?(?:\s*[kK])?\s*[-~至到]\s*\d{1,3}(?:\.\d)?\s*[kK千]'
            r'(?:\s*[/每]\s*月)?)',
            text_clean)
        if m:
            return m.group(1).strip()
        # 2. 万/月格式
        m = re.search(
            r'(\d{1,3}(?:\.\d)?\s*万?\s*[-~至到]\s*\d{1,3}(?:\.\d)?\s*万\s*(?:/|每)?\s*[年月])',
            text_clean)
        if m:
            return m.group(1).strip()
        # 3. 纯数字范围 + 元
        m = re.search(r'(\d{4,6}\s*[-~至到]\s*\d{4,6}\s*元)', text_clean)
        if m:
            return m.group(1).strip()
        # 4. 底薪+提成
        m = re.search(r'(底薪\s*\d{1,3}(?:\.\d)?\s*[kK千]\s*\+\s*(?:提成|绩效|奖金))', text_clean)
        if m:
            return m.group(1).strip()
        # 5. 面议
        m = re.search(r'(面议|薪资面议|待遇面议)', text_clean)
        if m:
            return m.group(1)
        return ""

    # ── 学历要求 ──
    elif field == "学历要求":
        m = re.search(
            r"(博士|硕士|研究生|MBA|EMBA|本科|统招本科|全日制本科|"
            r"大专|全日制大专|中专|中技|高中|初中|"
            r"学历不限|不限学历|学历无要求|学历无硬性要求|学历不作要求)",
            text_clean)
        return m.group(1).strip() if m else ""

    # ── 工作经验 ──
    elif field == "工作经验":
        # 1. 数字范围: 3-5年, 1~3年
        m = re.search(r'(\d{1,2}\s*[-~至到]\s*\d{1,2}\s*年(?:以上)?(?:相关)?(?:工作)?(?:经验|经历)?)', text_clean)
        if m:
            return m.group(1).strip()
        # 2. X年以上: 3年以上经验, 2年以上工作经历
        m = re.search(r'(\d{1,2}\s*年\s*以\s*上(?:相关)?(?:工作)?(?:经验|经历)?)', text_clean)
        if m:
            return m.group(1).strip()
        # 3. X年经验/经历: 2年经验, 3年工作经历
        m = re.search(r'(\d{1,2}\s*年(?:相关)?(?:工作)?(?:经验|经历))', text_clean)
        if m:
            return m.group(1).strip()
        # 4. 中文数字: 三年以上
        m = re.search(r'([一二三四五六七八九十]+\s*年\s*以\s*上(?:相关)?(?:工作)?(?:经验|经历)?)', text_clean)
        if m:
            return m.group(1).strip()
        # 5. 应届/无经验
        m = re.search(r'(应届(?:生|毕业生)?|经验不限|不限经验|无需经验|无经验要求|'
                      r'有无经验均可|接受应届|欢迎应届)', text_clean)
        if m:
            return m.group(1).strip()
        return ""

    # ── 岗位职责 ──
    elif field == "岗位职责":
        # 清除薪资数字干扰
        no_salary = re.sub(r'\d+[~-]\s*\d+\s*元', '', text_clean)
        no_salary = re.sub(r'\d+[~-]\d+', '', no_salary)
        # 过滤词
        filter_words = ("经验", "学历", "应届生")
        lines = re.split(r'[。，]', no_salary)

        # 1. 标准引导词
        lead_pat = re.compile(
            r'(?:主要负责|负责|专门负责|配合|对.*?进行|专注|每日完成|协助|把控)\s*(.+)', re.S)
        for line in lines:
            line = line.strip()
            if any(w in line for w in filter_words) or '【' in line:
                continue
            lead_match = lead_pat.search(line)
            if lead_match:
                return lead_match.group(1).strip()

        # 2. 兜底纯动作句
        for line in lines:
            line = line.strip()
            if any(w in line for w in filter_words) or '【' in line or len(line) < 8:
                continue
            return line[:500]
        return ""

    # ── 福利信息 ──
    elif field == "福利信息":
        pattern = re.compile(
            r'(六险一金|七险一金|五险一金|五险|社保|公积金|补充公积金|带薪年假|年假|'
            r'节日福利|健康体检|团建|餐补|住宿补贴|交通补贴|通讯补贴|交通补助|话补|'
            r'年终奖金|年终奖|绩效奖金|高额提成|定期体检|包吃住|包吃|包住|食宿全包|'
            r'高温补贴|晚班补贴|全勤奖|夜班补助|年终分红|团队旅游|防寒补贴|'
            r'持证补贴|工龄补贴|工龄津贴|花艺培训|节日花礼|野外补贴|防护用品|'
            r'岗位补贴|夜班津贴|文化补贴|专项津贴|双休|单休|大小周|弹性工作|弹性工时|'
            r'带薪学术交流|包教包会|技能入股|产品内购福利|节日红包|'
            r'水下作业补贴|高危岗位津贴|意外险叠加|计件提成|师徒带教补贴|劳保用品|'
            r'带薪休假|播出绩效奖|观影福利|农业专项补贴|采摘奖励|住宿免费|手艺补贴|'
            r'户外补贴|马术课程福利|校对绩效|护眼补贴|下午茶福利|下午茶|零食|'
            r'洗护提成|宠物用品内购|带薪轮休|高空作业补贴|演出绩效|'
            r'劳保物资|年度体检|晚班补助|分拣计件奖励|飞行补贴|意外险|'
            r'股票期权|期权|股权|免费班车|班车|培训|晋升|时间灵活|朝九晚五|朝九晚六|'
            r'不打卡|远程办公|在家办公|生日福利|不加班)', re.S)
        matches = pattern.findall(text_clean)
        return '、'.join(list(set(matches))) if matches else ''

    # ── 联系方式 ──
    elif field == "联系方式":
        phone_pat = re.compile(r'1[3-9]\d{9}', re.S)
        phones = phone_pat.findall(text_clean)
        if phones:
            return '、'.join(phones)
        # 带标签的电话
        m = re.search(r'(?:电话|手机|联系电话|联系方式|联系人|Tel|Phone|联系)[：:\s]*([\d\-+（）() ]{7,20})', text_clean)
        if m:
            return m.group(1).strip()
        return ''

    else:
        return ''


def infer_core(text: str) -> dict:
    global vocab_global
    if vocab_global is None:
        load_resources()
    text_clean = raw_text_clean(text)
    result = {}
    for k in FIELD_MAP:
        result[k] = extract_field_content(text_clean, k)
    return result


def get_field_tags(text: str) -> list:
    return list(infer_core(text).keys())
