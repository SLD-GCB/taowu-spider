# taotie_filter.py 【饕餮 · 噬滤净化模块】
import zhconv
import re
from config import (
    CHAR_MAP, NOISE_SYM_PAT, WORD_SPACE_PAT, TAIL_RUBBISH_PAT,
    RUBBISH_PAT, USELESS_BLOCK_PAT, JOB_MAIN_PAT, RECRUIT_PAT,
    full2half, rebuild_number
)

class TaoTieFilter:
    def __init__(self):
        self.recruit_keywords = ["招", "招聘", "诚聘", "急招", "高薪诚聘"]
        self.zero_width = ['\u200b','\u200c','\u200d','\ufeff']

    def fix_reverse_text(self, text: str) -> str:
        for kw in self.recruit_keywords:
            if text.strip().endswith(kw) and len(text) > 20:
                text = text[::-1]
        return text

    def clean_text(self, text: str) -> str:
        for c in self.zero_width:
            text = text.replace(c, "")
        for old, new in CHAR_MAP.items():
            text = text.replace(old, new)
        text = full2half(text)
        text = NOISE_SYM_PAT.sub("", text)
        text = WORD_SPACE_PAT.sub(r'\1\2', text)
        text = TAIL_RUBBISH_PAT.split(text)[0]
        text = re.sub(r'\s+', ' ', text).strip()
        text = RUBBISH_PAT.sub("", text)
        return text

    def split_recruit_block(self, html: str) -> list:
        html = USELESS_BLOCK_PAT.sub("", html)
        job_match = JOB_MAIN_PAT.search(html)
        if job_match:
            job_html = job_match.group(0)
            return [job_html]
        parts = RECRUIT_PAT.split(html)
        blocks = []
        current = ""
        for p in parts:
            p = p.strip()
            if p in self.recruit_keywords:
                if current and len(current) > 200:
                    blocks.append(current)
                current = p
            else:
                current += p
        if current and len(current) > 200:
            blocks.append(current)
        return blocks

    def process(self, raw_html: str) -> list:
        txt = self.fix_reverse_text(raw_html)
        txt = zhconv.convert(txt, 'zh-cn')
        txt = self.clean_text(txt)
        txt = rebuild_number(txt)
        blocks = self.split_recruit_block(raw_html)
        return blocks

    def clean_for_qianlong(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        text = raw_text.replace("\n", "").replace("\r", "").replace("\t", "")
        text = " ".join(text.split())
        return text.strip()

# 全局单例
taotie_filter = TaoTieFilter()