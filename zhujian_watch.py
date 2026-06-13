# zhujian_watch.py 【诸犍之眼 - 结构化文本重建增强版】
from bs4 import BeautifulSoup
from config import STRUCTURE_MARKERS, STRUCTURE_CLASS_KEYS

class ZhuJianEye:
    def __init__(self):
        pass

    def rebuild_structured_text(self, html: str) -> str:
        """
        【核心新增】结构化文本重建器
        1. 剥离无用节点（脚本、样式、注释）
        2. 提取并标记关键信息节点
        3. 按业务优先级拼接文本，供潜龙模型识别
        """
        soup = BeautifulSoup(html, "html.parser")

        # 1. 剥离所有无用节点，减少噪音
        for tag in soup(["script", "style", "noscript", "iframe", "head", "comment"]):
            tag.decompose()

        # 2. 提取关键信息并结构化标记
        structured_parts = []

        # 岗位名称
        for tag in soup.find_all(STRUCTURE_MARKERS["岗位名称"]):
            text = tag.get_text(strip=True)
            if text and len(text) > 2 and len(text) < 50:
                structured_parts.append(f"【岗位名称】{text}")
                break

        # 基础信息（薪资、地点、经验、学历）
        info_parts = []
        for span in soup.find_all("span"):
            text = span.get_text(strip=True)
            if text and any(k in text for k in STRUCTURE_MARKERS["薪资范围"] + STRUCTURE_MARKERS["工作地点"] + STRUCTURE_MARKERS["学历要求"] + STRUCTURE_MARKERS["工作经验"]):
                info_parts.append(text)
        if info_parts:
            structured_parts.append(f"【岗位基础信息】{' | '.join(info_parts)}")

        # 岗位职责与要求
        for div in soup.find_all("div"):
            cls = div.get("class", [])
            if any(c in STRUCTURE_CLASS_KEYS["job_detail"] for c in cls):
                desc = div.get_text(separator="\n", strip=True)
                if len(desc) > 30:
                    structured_parts.append(f"【岗位职责与要求】{desc}")
                    break

        # 公司信息
        for tag in soup.find_all(["h3", "div"]):
            cls = tag.get("class", [])
            if any(c in STRUCTURE_CLASS_KEYS["company"] for c in cls):
                company_text = tag.get_text(strip=True)
                if company_text and len(company_text) > 2:
                    structured_parts.append(f"【公司信息】{company_text}")
                    break

        # 3. 拼接全文兜底文本（防止标记没命中）
        full_text = soup.get_text(separator="\n", strip=True)
        structured_parts.append(f"【页面全文】\n{full_text}")

        # 4. 清理多余空行和重复文本
        final_text = "\n".join(structured_parts)
        lines = [line.strip() for line in final_text.splitlines() if line.strip()]
        return "\n".join(lines)

    def parse_full(self, html: str) -> str:
        """对外统一接口：调用结构化重建器"""
        return self.rebuild_structured_text(html)

    def to_json_input(self, text: str) -> str:
        """将文本转为潜龙模型可接受的JSON格式"""
        import json
        return json.dumps({"content": text, "site_key": "zhilian"}, ensure_ascii=False)

    def watch_batch(self, result_list: list) -> tuple:
        """批量结果质检（兼容原有接口）"""
        ok_data = []
        err_data = []
        for item in result_list:
            # 简单质检：岗位名称/薪资/地点至少有一个不为空
            if item.get("岗位名称") or item.get("薪资范围") or item.get("工作地点"):
                ok_data.append(item)
            else:
                err_data.append(item)
        return ok_data, err_data

# 全局单例（和主程序导入保持一致）
zhujian_eye = ZhuJianEye()