# config_gui.py — 纯逻辑层（v12.1.2 精简版）
# Qt UI (main_qt.py / qt_dialogs.py) 复用此文件中的类/函数。
# 所有 Tkinter 依赖代码已移除。UI 常量以 config.py 为唯一权威来源。
import sys
import os
import sqlite3
import pymysql
import re
import traceback
import DBPlugin

# ===================== 环境初始化（打包路径终极修复）=====================
if getattr(sys, "frozen", False):
    APP_ROOT = os.path.dirname(sys.executable)
else:
    APP_ROOT = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "plugins"))

# 数据库统一存放目录（程序根目录）
DB_SAVE_DIR = APP_ROOT

# ===================== 【模式常量】=====================
MODE_SQLITE = "inner_sqlite"
MODE_MYSQL = "out_mysql"

# ===================== 【抽取引擎常量】=====================
MAX_EXTRACT_CHAR = 1000
MAX_WORKERS = 3

# ===================== 【单条录入 字段常量】=====================
INPUT_FIELD_LIST = [
    ("岗位名称", 30, "entry"),
    ("企业名称", 30, "entry"),
    ("工作地点", 30, "entry"),
    ("薪资范围", 30, "entry"),
    ("学历要求", 30, "entry"),
    ("工作经验", 30, "entry"),
    ("岗位职责", 80, "entry"),
    ("福利信息", 30, "entry"),
    ("联系方式", 30, "entry"),
    ("job_url", 50, "entry"),
    ("source", 30, "entry")
]

# ===================== 【数据库通用工具函数】=====================
def escape_table_name(tbl: str) -> str:
    """转义表名，兼容反引号、空格、数据库关键字"""
    if not tbl:
        return ""
    safe_tbl = tbl.replace("`", "``")
    return f"`{safe_tbl}`"

def get_sqlite_real_path(db_name: str) -> str:
    """统一获取 SQLite 绝对路径（源码/打包通用）"""
    db_file_name = f"inner_sqlite_{db_name}.db"
    return os.path.join(DB_SAVE_DIR, db_file_name)

# ===================== 【通用工具类 - 文本清洗】=====================
class TextCleaner:
    __slots__ = ("pattern", "noise_words")
    def __init__(self):
        # 修复：将 - 放到字符集末尾，避免被识别为字符区间
        self.pattern = re.compile(r'[^一-龥a-zA-Z0-9\s\.,;:、，。：；~()（）-]')
        self.noise_words = {"爱企查", "直聘", "官网", "本地文件", "为您提供", "招聘信息就到"}

    def clean(self, text: str) -> str:
        if not text:
            return ""
        for word in self.noise_words:
            text = text.replace(word, "")
        return self.pattern.sub('', text).strip()

    def split_by_length(self, text: str, limit: int) -> list[str]:
        chunks = []
        start = 0
        text_len = len(text)
        while start < text_len:
            chunks.append(text[start:start+limit])
            start += limit
        return chunks

# ===================== 【抽取引擎 - 数据模型】=====================
class QianLongResult:
    __slots__ = (
        "岗位名称", "企业名称", "工作地点", "薪资范围",
        "学历要求", "工作经验", "岗位职责", "福利信息",
        "联系方式", "job_url", "source"
    )
    def __init__(self):
        self.岗位名称 = ""
        self.企业名称 = ""
        self.工作地点 = ""
        self.薪资范围 = ""
        self.学历要求 = ""
        self.工作经验 = ""
        self.岗位职责 = ""
        self.福利信息 = ""
        self.联系方式 = ""
        self.job_url = ""
        self.source = ""

# ===================== 【抽取引擎 - 规则匹配】=====================
class QianLongV2:
    __slots__ = ("rule_map",)
    def __init__(self):
        self.rule_map = {
            "企业名称": re.compile(
                r'([一-龥（）()]{3,25}'
                r'(?:有限公司|股份公司|集团|有限责任|科技|实业|网络|信息|文化|传媒|'
                r'教育|医疗|餐饮|酒店|物业|物流|建筑|装饰|设计|咨询|服务|贸易|'
                r'发展|投资|管理|生物|制药|化工|机械|电子|电气|自动化|软件|数据|'
                r'云计算|电子商务|新媒体|广告|影视|娱乐|旅游|金融|保险|房地产|连锁|控股))'
            ),
            "岗位名称": re.compile(
                r'([一-龥a-zA-Z+#·]{2,20}'
                r'(?:工程师|经理|专员|主管|总监|代表|顾问|主任|助理|员|师|工|长|家|生|'
                r'开发|设计|测试|运营|销售|客服|行政|财务|会计|人事|HR|司机|文员|'
                r'策划|编辑|记者|导演|演员|主播|讲师|教练|老师|教师|教授|医生|护士|'
                r'药师|律师|法官|警察|保安|保洁|厨师|服务员|收银|导购|店长|'
                r'美工|UI|Java|Python|PHP|前端|后端|全栈|运维|DBA|'
                r'产品经理|项目经理|架构师|技术总监))'
            ),
            "工作地点": re.compile(
                r'([一-龥]{2,6}(?:市|区|县|省|州|盟|旗|新区|开发区|高新区))|'
                r'([一-龥]{2,4}(?:[-—][一-龥]{2,5})?(?:区|路|街|镇|乡))'
            ),
            "薪资范围": re.compile(
                r'(\d{1,3}(?:[,.\s]?\d)?\s*[kK千万元]\s*[-~至到]\s*\d{1,3}(?:[,.\s]?\d)?\s*[kK千万元])|'
                r'(\d{4,6}\s*[-~至到]\s*\d{4,6})|'
                r'(\d{1,3}(?:\.\d)?\s*万?\s*[-~至到]\s*\d{1,3}(?:\.\d)?\s*万\s*(?:/|每)?\s*[年月])|'
                r'(底薪\s*\d{1,3}(?:\.\d)?\s*[kK千]\s*\+\s*(?:提成|绩效|奖金))|'
                r'(面议|薪资面议|待遇面议|薪资open)'
            ),
            "学历要求": re.compile(
                r'(博士|硕士|研究生|MBA|EMBA|本科|大专|中专|中技|高中|初中|'
                r'学历不限|不限学历|学历无要求|统招本科|全日制本科|全日制大专)'
            ),
            "工作经验": re.compile(
                r'(\d+\s*[-~至到]\s*\d+\s*年(?:以上)?(?:相关)?(?:工作)?(?:经验|经历))|'
                r'(\d+\s*年(?:以上)?(?:相关)?(?:工作)?(?:经验|经历))|'
                r'(应届(?:生|毕业生)?)|'
                r'(经验不限|不限经验|无需经验|无经验要求|接受应届|欢迎应届)'
            ),
            "福利信息": re.compile(
                r'(五险一金|六险一金|七险一金|五险|社保|公积金|补充公积金|双休|单休|大小周|'
                r'弹性工作|不加班|包吃|包住|包吃住|餐补|房补|交通补贴|通讯补贴|话补|'
                r'年终奖|绩效奖金|项目奖金|带薪年假|年假|团建|旅游|体检|定期体检|'
                r'节日福利|生日福利|下午茶|零食|股票期权|期权|股权|免费班车|班车|'
                r'培训|晋升|时间灵活|朝九晚五|朝九晚六|不打卡|远程办公|在家办公)'
            ),
            "联系方式": re.compile(
                r'(?:电话|手机|联系电话|联系方式|联系人|Tel|Phone|联系)[：:\s]*([\d\-+（）() ]{7,20})|'
                r'(1[3-9]\d{9})'
            ),
        }

    def extract_single(self, text: str) -> dict:
        res = {}
        for k, reg in self.rule_map.items():
            matches = [(m.group().strip(), len(m.group())) for m in reg.finditer(text)]
            if matches:
                strong = [(v, l) for v, l in matches if '不限' not in v and '面议' not in v]
                if strong:
                    best = max(strong, key=lambda x: x[1])
                    res[k] = best[0]
                else:
                    best = max(matches, key=lambda x: x[1])
                    res[k] = best[0]
        return res

    def merge_result(self, base: dict, new: dict) -> dict:
        for k, v in new.items():
            if not v:
                continue
            old = base.get(k, "")
            if not old or old in ("不限", "面议", ""):
                base[k] = v
            elif v not in ("不限", "面议", "") and len(v) > len(old):
                base[k] = v
        return base

    def extract_by_batch(self, raw_dict: dict, text_chunks: list[str]) -> QianLongResult:
        res = QianLongResult()
        base_data = {}
        for chunk in text_chunks:
            base_data = self.merge_result(base_data, self.extract_single(chunk))
        for k, v in base_data.items():
            if hasattr(res, k):
                setattr(res, k, v)
        res.job_url = raw_dict.get("job_url", "")
        res.source = raw_dict.get("source", "")
        res.岗位职责 = "".join(text_chunks)[:MAX_EXTRACT_CHAR].strip()
        for k, v in raw_dict.items():
            if hasattr(res, k) and not getattr(res, k):
                setattr(res, k, str(v).strip())
        return res

# ===================== 【数据库通用操作】=====================
def db_operate(mode: str, host: str, user: str, pwd: str, db_name: str, table_name: str, func, **kwargs):
    """统一数据库连接/执行/关闭/异常捕获。kwargs 透传给 DBPlugin（如 fields=[(name,type),...]）
    缺表自动建列由 DBPlugin.create_table 内部按 ACTIVE_SCHEMA 决定（绑定模板→模板列）。"""
    db = None
    try:
        db = DBPlugin.DBPlugin(mode, host, user, pwd, 3306, db_name, table_name, **kwargs)
        ret = func(db)
        db.close()
        return ret
    except Exception as e:
        if db:
            db.close()
        print(f"[db_operate] 异常: {str(e)}")
        return str(e)

# ===================== 【数据表通用操作函数】=====================
def create_custom_table(mode: str, host: str, user: str, pwd: str,
                        db_name: str, db_file: str, table_name: str,
                        fields: list) -> str:
    """创建自定义字段的数据表。fields 格式: [(字段名, 类型), ...]。
    返回 "success" 或错误信息字符串。"""
    if not table_name or not table_name.strip():
        return "表名不能为空"
    if not fields:
        return "至少需要定义一个字段"
    # 验证字段名（去空、去重、查注入字符）
    # 列名会被直接拼进 CREATE TABLE（f"`{name}` {dtype}"），所以它必须是干净的标识符。
    # 2026-09-11 起列名可以由用户导入的模板 JSON 决定，这一道不能再只 strip()。
    import config as _cfg
    clean_fields = []
    seen = set()
    for name, dtype in fields:
        raw = str(name or "").strip()
        if not raw:
            continue
        if any(c in raw for c in _cfg._IDENT_BAD_CHARS) or len(raw) > _cfg._IDENT_MAX:
            return "列名「%s」不合法（不能含反引号/分号/换行，且不超过 %d 字符）" % (raw, _cfg._IDENT_MAX)
        if raw.lower() in _cfg._IDENT_RESERVED:
            return "列名不能用「%s」（建表语句自己会写 id 主键）" % raw
        if raw.lower() in seen:
            continue
        seen.add(raw.lower())
        dtype = str(dtype or "").strip().upper() or "TEXT"
        if dtype not in ("TEXT", "INTEGER", "REAL", "BLOB"):
            dtype = "TEXT"
        clean_fields.append((raw, dtype))
    if not clean_fields:
        return "无有效字段"
    return db_operate(mode, host, user, pwd, db_name, table_name,
                      lambda db: None, fields=clean_fields)


def create_table_from_template(mode: str, host: str, user: str, pwd: str,
                               db_name: str, db_file: str, table_name: str,
                               spec: dict) -> str:
    """按数据表模板建表。spec 取 {columns: [{name,type,...},...]}；
    复用 create_custom_table（显式传字段 → 绝不触发招聘静默建表）。返回 "success" 或错误串。"""
    from config import template_create_fields
    fields = template_create_fields(spec)
    if not fields:
        return "模板无有效字段"
    return create_custom_table(mode, host, user, pwd, db_name, db_file,
                               table_name, fields)


def get_table_list(mode: str, host: str, user: str, pwd: str, db_name: str, db_file: str) -> list:
    """获取库下所有数据表，区分 SQLite / MySQL，增加连接超时"""
    tables = []
    conn = None
    cur = None
    try:
        if mode == MODE_SQLITE:
            real_db = get_sqlite_real_path(db_name)
            print(f"[DEBUG] SQLite 路径: {real_db}")
            if not os.path.exists(real_db):
                print(f"[ERROR] SQLite 文件不存在: {real_db}")
                return []
            conn = sqlite3.connect(real_db, timeout=10.0)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
            tables = [t[0] for t in cur.fetchall() if t[0] != "sqlite_sequence"]
            print(f"[INFO] SQLite 读取到数据表: {tables}")
        else:
            conn = pymysql.connect(
                host=host, user=user, password=pwd,
                database=db_name, charset="utf8mb4",
                connect_timeout=10, read_timeout=15, write_timeout=15
            )
            cur = conn.cursor()
            cur.execute("SHOW TABLES;")
            tables = [t[0] for t in cur.fetchall()]
            print(f"[INFO] MySQL 读取到数据表: {tables}")
    except Exception as e:
        print(f"[ERROR] get_table_list 执行异常: {str(e)}")
        traceback.print_exc()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
    return tables

def load_table_data(mode: str, host: str, user: str, pwd: str, db_name: str, db_file: str, table_name):
    """读取数据表字段+数据，表名转义+连接超时+异常加固"""
    cols, data = [], []
    if not table_name:
        print("[WARNING] 表名为空，无法加载数据")
        return cols, data
    conn = None
    cur = None
    safe_tbl = escape_table_name(table_name)
    try:
        if mode == MODE_SQLITE:
            real_db = get_sqlite_real_path(db_name)
            conn = sqlite3.connect(real_db, timeout=10.0)
            cur = conn.cursor()
            cur.execute(f'PRAGMA table_info({safe_tbl})')
            cols = [c[1] for c in cur.fetchall()]
            print(f"[INFO] SQLite 表【{table_name}】字段: {cols}")
            cur.execute(f'SELECT * FROM {safe_tbl} LIMIT 200;')
        else:
            conn = pymysql.connect(
                host=host, user=user, password=pwd,
                database=db_name, charset="utf8mb4",
                connect_timeout=10, read_timeout=15, write_timeout=15
            )
            cur = conn.cursor()
            cur.execute(f"DESCRIBE {safe_tbl}")
            cols = [c[0] for c in cur.fetchall()]
            print(f"[INFO] MySQL 表【{table_name}】字段: {cols}")
            cur.execute(f"SELECT * FROM {safe_tbl} LIMIT 200")

        data = cur.fetchall()
        print(f"[INFO] 表【{table_name}】读取到 {len(data)} 条数据")
    except Exception as e:
        print(f"[ERROR] load_table_data 异常: {str(e)}")
        traceback.print_exc()
        raise
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
    return cols, data

def add_empty_table_row(mode: str, host: str, user: str, pwd: str, db_name: str, db_file: str, table_name) -> str:
    """数据表新增空行，区分SQLite/MySQL语法，修复MySQL 1064错误"""
    if not table_name:
        return "表名不能为空"
    conn = None
    cur = None
    safe_tbl = escape_table_name(table_name)
    try:
        if mode == MODE_SQLITE:
            real_db = get_sqlite_real_path(db_name)
            conn = sqlite3.connect(real_db, timeout=10.0)
            cur = conn.cursor()
            cur.execute(f'INSERT INTO {safe_tbl} DEFAULT VALUES')
        else:
            # MySQL 不支持 DEFAULT VALUES，改用标准空插入语法
            conn = pymysql.connect(
                host=host, user=user, password=pwd,
                database=db_name, charset="utf8mb4",
                connect_timeout=10, read_timeout=15, write_timeout=15
            )
            cur = conn.cursor()
            cur.execute(f'INSERT INTO {safe_tbl} () VALUES ()')
        conn.commit()
        return "success"
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[ERROR] add_empty_table_row 异常: {str(e)}")
        traceback.print_exc()
        return str(e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

def del_table_rows(mode: str, host: str, user: str, pwd: str, db_name: str, db_file: str, table_name, id_list):
    """批量删除行，IN语法+事务+回滚，防止部分删除脏数据"""
    if not table_name or not id_list:
        return "参数不全"
    conn = None
    cur = None
    safe_tbl = escape_table_name(table_name)
    try:
        if mode == MODE_SQLITE:
            real_db = get_sqlite_real_path(db_name)
            conn = sqlite3.connect(real_db, timeout=10.0)
            placeholder = ",".join(["?"] * len(id_list))
        else:
            conn = pymysql.connect(
                host=host, user=user, password=pwd,
                database=db_name, charset="utf8mb4",
                connect_timeout=10, read_timeout=15, write_timeout=15
            )
            placeholder = ",".join(["%s"] * len(id_list))

        cur = conn.cursor()
        sql = f'DELETE FROM {safe_tbl} WHERE id IN ({placeholder})'
        cur.execute(sql, tuple(id_list))
        conn.commit()
        return "success"
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[ERROR] del_table_rows 异常: {str(e)}")
        traceback.print_exc()
        return str(e)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
