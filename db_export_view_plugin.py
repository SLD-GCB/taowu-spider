# db_export_view_plugin.py 动态数据表查看 + CSV导出
import os
import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import sqlite3
import pymysql

class DbViewExportPlugin:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = None
        self.db_path = ""
        self.db_mode = "inner_sqlite"
        self.db_host = "localhost"
        self.db_user = "root"
        self.db_pwd = ""
        self.db_name = "spider_db"

    def _fix_gbk(self, text):
        """中文编码兼容"""
        try:
            return text.encode("gbk", "ignore").decode("gbk")
        except:
            return text

    def _get_all_tables(self, conn):
        """获取数据库内所有表名"""
        cur = conn.cursor()
        if isinstance(conn, sqlite3.Connection):
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        else:
            cur.execute("SHOW TABLES;")
        tables = [t[0] for t in cur.fetchall()]
        return tables

    def _load_table_data(self, conn, table_name, tree):
        """动态加载表数据 + 自动渲染列"""
        for item in tree.get_children():
            tree.delete(item)
        tree["columns"] = ()
        cur = conn.cursor()
        if isinstance(conn, sqlite3.Connection):
            cur.execute(f"PRAGMA table_info(`{table_name}`)")
            cols = [row[1] for row in cur.fetchall()]
        else:
            cur.execute(f"DESCRIBE `{table_name}`")
            cols = [row[0] for row in cur.fetchall()]
        tree["columns"] = cols
        tree["show"] = "headings"
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=120, anchor="w")
        cur.execute(f"SELECT * FROM `{table_name}`")
        rows = cur.fetchall()
        for row in rows:
            row_data = [self._fix_gbk(str(cell)) for cell in row]
            tree.insert("", tk.END, values=row_data)

    def refresh_db(self):
        """切换数据库后刷新表列表"""
        try:
            if self.db_mode == "inner_sqlite":
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
            else:
                conn = pymysql.connect(
                    host=self.db_host, user=self.db_user, password=self.db_pwd,
                    database=self.db_name, charset="utf8mb4"
                )
            tables = self._get_all_tables(conn)
            self.table_combo["values"] = tables
            if tables:
                self.table_combo.current(0)
                self._load_table_data(conn, tables[0], self.tree)
            conn.close()
        except Exception as e:
            messagebox.showerror("错误", f"读取数据库失败：{str(e)}")

    def select_db_file(self):
        """选择SQLite数据库文件"""
        file_path = filedialog.askopenfilename(
            title="选择SQLite数据库",
            filetypes=[("DB文件", "*.db"), ("所有文件", "*.*")]
        )
        if not file_path:
            return
        self.db_path = file_path
        self.db_mode = "inner_sqlite"
        self.db_entry.delete(0, tk.END)
        self.db_entry.insert(0, file_path)
        self.refresh_db()

    def export_csv(self):
        """导出当前表格为CSV"""
        table_name = self.table_combo.get()
        if not table_name:
            messagebox.showwarning("提示", "请先选择数据表")
            return
        save_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV文件", "*.csv")]
        )
        if not save_path:
            return
        try:
            if self.db_mode == "inner_sqlite":
                conn = sqlite3.connect(self.db_path)
            else:
                conn = pymysql.connect(
                    host=self.db_host, user=self.db_user, password=self.db_pwd,
                    database=self.db_name, charset="utf8mb4"
                )
            cur = conn.cursor()
            if isinstance(conn, sqlite3.Connection):
                cur.execute(f"PRAGMA table_info(`{table_name}`)")
                headers = [row[1] for row in cur.fetchall()]
            else:
                cur.execute(f"DESCRIBE `{table_name}`")
                headers = [row[0] for row in cur.fetchall()]
            cur.execute(f"SELECT * FROM `{table_name}`")
            rows = cur.fetchall()
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
            conn.close()
            messagebox.showinfo("成功", f"数据已导出至：{save_path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def run(self):
        """启动查看窗口"""
        self.root = tk.Toplevel()
        self.root.title("数据库查看 & CSV导出")
        self.root.geometry("1100x700")
        top_frame = tk.Frame(self.root)
        top_frame.pack(fill=tk.X, padx=5, pady=5)
        tk.Label(top_frame, text="数据库路径:").grid(row=0, column=0)
        self.db_entry = tk.Entry(top_frame, width=60)
        self.db_entry.grid(row=0, column=1)
        tk.Button(top_frame, text="选择DB文件", command=self.select_db_file).grid(row=0, column=2, padx=2)
        tk.Label(top_frame, text="数据表:").grid(row=0, column=3, padx=(10,0))
        self.table_combo = ttk.Combobox(top_frame, width=20, state="readonly")
        self.table_combo.grid(row=0, column=4)
        self.table_combo.bind("<<ComboboxSelected>>", lambda e: self._load_table_data(
            sqlite3.connect(self.db_path, check_same_thread=False) if self.db_mode=="inner_sqlite"
            else pymysql.connect(host=self.db_host,user=self.db_user,password=self.db_pwd,database=self.db_name,charset="utf8mb4"),
            self.table_combo.get(), self.tree
        ))
        tk.Button(top_frame, text="导出CSV", command=self.export_csv).grid(row=0, column=5, padx=10)
        self.tree = ttk.Treeview(self.root)
        scroll_y = ttk.Scrollbar(self.root, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_x = ttk.Scrollbar(self.root, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.tree.pack(expand=True, fill=tk.BOTH, padx=5)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.root.mainloop()