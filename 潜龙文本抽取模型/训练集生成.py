import random
import json

# ===================== 语料库 =====================
start_pool = [
    "本企业业务持续扩张，现面向社会公开招聘",
    "我司深耕行业多年，现扩招",
    "因项目规模扩大，现急招",
    "公司稳步发展，团队扩招，现诚聘",
    "品牌拓展市场，对外招聘优秀人才",
    "厂区扩大产能，现招聘",
    "企业为满足发展需求，现面向全社会公开招聘",
    "为进一步扩充团队力量，现诚邀有志之士加入",
    "随着业务版图的不断扩大，现启动新一轮招聘计划",
    "为优化团队结构，提升服务质量，现对外发布招聘信息"
]

post_pool = [
    "人事专员", "销售代表", "后端开发工程师", "仓储管理员", "平面设计专员",
    "物流调度员", "短视频运营专员", "门店店长", "财务会计", "质检专员",
    "招商专员", "客服专员", "数据分析员", "文案编辑", "直播助理",
    "生产组长", "行政综合专员", "法务助理", "前端开发工程师", "运维工程师",
    "市场推广专员", "采购专员", "培训专员", "项目助理", "技术支持工程师"
]

duty_start = ["主要负责", "日常工作包含", "本职工作为", "需完成", "岗位职责涵盖", "核心工作包括"]
duty_pool = [
    "员工入职离职办理、考勤统计、招聘渠道维护、员工档案整理以及部门日常行政事务统筹",
    "到店客户接待、产品讲解推介、销售单据录入、货品盘点以及老客户关系维护",
    "后台接口开发、数据库维护、系统漏洞修复、功能迭代以及前后端联调工作",
    "货物出入库登记、仓库货品整理、库存数据核对、货架维护以及配合物流完成发货对接",
    "海报设计、宣传物料制作、图片精修、活动视觉排版以及配合运营完成创意输出",
    "运输路线规划、车辆调度安排、货运信息登记、对接司机与客户以及跟进货物运输状态",
    "短视频脚本撰写、视频剪辑发布、账号日常维护、评论区互动以及数据分析优化",
    "门店日常管理、员工排班调度、店内卫生监督、食材库存盘点以及客户投诉处理",
    "账务处理、票据整理、纳税申报、财务报表制作以及对接税务部门相关工作",
    "成品外观检测、功能测试、不合格品登记分类、质检数据记录以及把控出厂产品质量",
    "合作客户开发、商务对接洽谈、合作资料整理、客户关系维护以及跟进合作落地事宜",
    "线上客户咨询承接、订单答疑、售后问题处理、客户诉求登记以及维护店铺好评率",
    "业务数据整理、报表制作、数据趋势分析、异常数据排查以及输出数据分析报告",
    "宣传文案撰写、公众号内容编辑、活动软文创作、图文排版优化以及素材收集整理",
    "协助主播开展直播、直播场控、商品链接上架、后台数据记录以及直播间互动"
]

edu_pool = [
    "大专及以上学历", "本科及以上学历", "高中及以上学历", "中专及以上学历", "学历无硬性要求"
]

exp_pool = [
    "拥有一年以上相关工作经验", "具备两年以上从业经验", "三年左右实战经验",
    "应届生可接受", "无经验亦可入职", "有对应岗位经历优先", "有相关项目经验者优先考虑"
]

addr_start = ["办公地址位于", "工作地点设在", "厂区坐落于", "门店地处", "办公园区在", "公司地址选在"]
addr_pool = [
    "城北商务大厦", "高新区产业园", "西城工业园", "城东科创园", "市中心商圈",
    "城郊物流产业园", "政务区商务大厦", "城南商圈", "滨江智慧大厦", "城西数字产业园",
    "城东商务区", "城北科创园", "城南数字大厦", "市中心写字楼"
]

welfare_pool = [
    "五险一金、带薪年假、节日礼品、年度体检、月度团建活动",
    "六险一金、餐补、全勤奖、节日福利、岗前培训",
    "七险一金、带薪病假、年终奖、户外团建、健康体检",
    "五险一金、包吃住、交通补贴、高温补贴、节日福利",
    "六险一金、绩效奖金、带薪节假日、年度体检、团队聚餐",
    "五险一金、带薪年假、年终福利、定期团建、技术培训"
]

extra_text_pool = [
    "，周边交通便利，配套设施齐全，办公环境舒适，团队氛围轻松融洽。",
    "，周边出行便捷，办公氛围良好，配套生活设施完善，员工生活便利。",
    "，交通四通八达，办公区域宽敞明亮，团队年轻活力，沟通氛围简单直接。",
    "，周边餐饮、购物一应俱全，通勤方便，为员工提供舒适的工作与生活环境。",
    "，园区环境优美，绿化覆盖率高，配套设施完善，适合长期稳定发展。"
]

# 固定字段
company_name_val = ""
company_start = -1
company_end = -1

def generate_single_sample():
    """生成单条文本 + 8个字段+坐标"""
    start_txt = random.choice(start_pool)
    post_name = random.choice(post_pool)
    duty_s = random.choice(duty_start)
    duty_txt = random.choice(duty_pool)
    edu_txt = random.choice(edu_pool)
    exp_txt = random.choice(exp_pool)
    addr_s = random.choice(addr_start)
    addr_txt = random.choice(addr_pool)
    welfare_txt = random.choice(welfare_pool)
    extra_txt = random.choice(extra_text_pool)

    # 随机薪资
    min_sal = random.randint(4000, 7000)
    max_sal = random.randint(min_sal + 800, 13000)
    salary_txt = f"月薪{min_sal}~{max_sal}元，绩效奖金按月发放"
    salary_val = f"{min_sal}~{max_sal}元"

    # 拼接完整原文
    raw = (
        f"{start_txt}{post_name}一名。{duty_s}{duty_txt}。任职要求为{edu_txt}，{exp_txt}，综合能力优秀者优先。"
        f"{addr_s}{addr_txt}{extra_txt}该岗位{salary_txt}。入职即可享受{welfare_txt}。公司提供完善的新人培训，晋升机制透明，工作氛围和谐，"
        f"欢迎有意向的求职者投递简历，加入我们一同发展成长，在稳定的平台上实现个人价值与职业成长。"
    )

    # ========== 逐个计算字符坐标 ==========
    def get_char_pos(full_text, sub_text):
        """返回子串在全文中的 [start, end]，找不到返回 [-1,-1]"""
        idx = full_text.find(sub_text)
        if idx == -1:
            return -1, -1
        return idx, idx + len(sub_text) - 1

    # 1. 岗位名称
    p_st, p_ed = get_char_pos(raw, post_name)
    # 2. 薪资完整值
    s_st, s_ed = get_char_pos(raw, salary_val)
    # 3. 工作地点
    a_st, a_ed = get_char_pos(raw, addr_txt)
    # 4. 公司名称（无）
    c_st, c_ed = company_start, company_end
    # 5. 学历
    e_st, e_ed = get_char_pos(raw, edu_txt)
    # 6. 工作经验
    ex_st, ex_ed = get_char_pos(raw, exp_txt)
    # 7. 岗位职责
    d_st, d_ed = get_char_pos(raw, duty_txt)
    # 8. 福利待遇
    w_st, w_ed = get_char_pos(raw, welfare_txt)

    # 构造8条数据：raw / key / val / start / end
    items = [
        {"raw": raw, "key": "岗位名称", "val": post_name, "start": p_st, "end": p_ed},
        {"raw": raw, "key": "薪资", "val": salary_val, "start": s_st, "end": s_ed},
        {"raw": raw, "key": "工作地点", "val": addr_txt, "start": a_st, "end": a_ed},
        {"raw": raw, "key": "公司名称", "val": company_name_val, "start": c_st, "end": c_ed},
        {"raw": raw, "key": "学历", "val": edu_txt, "start": e_st, "end": e_ed},
        {"raw": "raw", "key": "工作经验", "val": exp_txt, "start": ex_st, "end": ex_ed},
        {"raw": raw, "key": "岗位职责", "val": duty_txt, "start": d_st, "end": d_ed},
        {"raw": raw, "key": "福利待遇", "val": welfare_txt, "start": w_st, "end": w_ed}
    ]
    # 修正上一行笔误
    items[5]["raw"] = raw
    return items

def main(total_num=1000):
    all_data = []
    for idx in range(total_num):
        group = generate_single_sample()
        all_data.extend(group)
        if (idx + 1) % 100 == 0:
            print(f"已生成 {idx+1}/{total_num} 篇招聘文本")

    out_path = "job_dataset_with_pos.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f"\n完成！共 {total_num} 篇文本，{len(all_data)} 条标注，已保存至 {out_path}")

if __name__ == "__main__":
    main(total_num=1000)