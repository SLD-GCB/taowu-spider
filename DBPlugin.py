import sqlite3
import pymysql
import os
import sys
from config import DB_BASE_FIELDS

if getattr(sys, "frozen", False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.getcwd()

# 固定入库字段 从config读取
FIX_FIELDS = DB_BASE_FIELDS

class DBPlugin:
    def __init__(self, mode, host, user, pwd, port, dbname, tblname):
        self.mode = mode
        self.host = host
        self.user = user
        self.pwd = pwd
        self.port = port
        self.dbname = dbname
        self.tbl = tblname.strip()
        self.conn = None
        self._connect()

    def _connect(self):
        if self.mode == "inner_sqlite":
            db_file = os.path.join(base_path, f"inner_sqlite_{self.dbname}.db")
            self.conn = sqlite3.connect(db_file, check_same_thread=False)
        else:
            self.conn = pymysql.connect(
                host=self.host,
                user=self.user,
                password=self.pwd,
                port=self.port,
                database=self.dbname,
                charset="utf8mb4"
            )
        self.create_table()

    def create_table(self):
        cur = self.conn.cursor()
        col_sql = ", ".join([f"`{f}` TEXT" for f in FIX_FIELDS])
        if self.mode == "inner_sqlite":
            sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.tbl}` (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {col_sql}
            )
            """
        else:
            sql = f"""
            CREATE TABLE IF NOT EXISTS `{self.tbl}` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                {col_sql}
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        cur.execute(sql)
        self.conn.commit()

    def insert(self, data_dict):
        cur = self.conn.cursor()
        values = [data_dict.get(field, "") for field in FIX_FIELDS]
        placeholder = "?" if self.mode == "inner_sqlite" else "%s"
        place_list = [placeholder] * len(FIX_FIELDS)
        sql = f"""
        INSERT INTO `{self.tbl}` ({', '.join([f"`{f}`" for f in FIX_FIELDS])})
        VALUES ({', '.join(place_list)})
        """
        cur.execute(sql, values)
        self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.close()