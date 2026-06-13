import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
import DBPlugin

# 数据库配置（和你的项目保持一致）
DB_CFG = {
    "mode": "inner_sqlite",
    "host": "localhost",
    "user": "root",
    "pwd": "",
    "db": "spider_db",
    "table": "spider_data"
}

# 字段配置（和你的数据库表一致）
FIELD_LIST = [
    ("岗位名称", 30, "entry"),
    ("工作地点", 30, "entry"),
    ("薪资范围", 30, "entry"),
    ("学历要求", 30, "entry"),
    ("工作经验", 30, "entry"),
    ("岗位职责", 80, "text"),
    ("福利信息", 30, "entry"),
    ("联系方式", 30, "entry"),
    ("job_url", 50, "entry"),
    ("source", 30, "entry"),
    ("content", 80, "text")
]

class MultiManualInputWin:
    def __init__(self, root):
        self.root = root
        self.root.title("多组手动字段录入工具")
        self.root.geometry("1000x750")
        self.root.resizable(True, True)

        # 存储所有数据行的控件列表
        self.rows = []
        self.row_frame_container = None

        # 顶部按钮区
        top_frame = tk.Frame(root)
        top_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Button(top_frame, text="➕ 添加一组新数据", command=self.add_new_row, width=15).pack(side=tk.LEFT, padx=5)
        tk.Button(top_frame, text="🗑️ 清空所有数据", command=self.clear_all_rows, width=15).pack(side=tk.LEFT, padx=5)

        # 滚动容器（放多组数据）
        self.canvas = tk.Canvas(root)
        self.scrollbar = ttk.Scrollbar(root, orient="vertical", command=self.canvas.yview)
        self.row_frame_container = tk.Frame(self.canvas)
        self.row_frame_container.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.row_frame_container, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 底部操作区
        bottom_frame = tk.Frame(root)
        bottom_frame.pack(fill=tk.X, padx=10, pady=10)
        tk.Button(bottom_frame, text="✅ 批量提交入库", command=self.submit_all_rows, width=20, bg="#90EE90").pack(side=tk.LEFT, padx=10)
        tk.Button(bottom_frame, text="❌ 关闭窗口", command=self.root.destroy, width=12).pack(side=tk.RIGHT, padx=10)

        # 日志区
        tk.Label(root, text="操作日志：").pack(anchor=tk.W, padx=10)
        self.log_box = scrolledtext.ScrolledText(root, width=100, height=8)
        self.log_box.pack(padx=10, fill=tk.BOTH, expand=True)

        # 默认添加一组数据
        self.add_new_row()

    def log(self, msg):
        """日志输出"""
        self.log_box.insert(tk.END, f"{msg}\n")
        self.log_box.see(tk.END)
        self.root.update()

    def add_new_row(self):
        """添加一组新的数据录入行"""
        row_idx = len(self.rows)
        row_widgets = {}

        # 行容器
        row_frame = tk.LabelFrame(self.row_frame_container, text=f"第 {row_idx+1} 条数据")
        row_frame.pack(fill=tk.X, padx=5, pady=5)

        # 逐字段创建输入控件
        for idx, (field_name, width, widget_type) in enumerate(FIELD_LIST):
            field_frame = tk.Frame(row_frame)
            field_frame.pack(fill=tk.X, padx=5, pady=2)

            tk.Label(field_frame, text=f"{field_name}：", width=12, anchor=tk.W).pack(side=tk.LEFT)

            if widget_type == "text":
                widget = scrolledtext.ScrolledText(field_frame, width=width, height=3)
            else:
                widget = tk.Entry(field_frame, width=width)
            widget.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            row_widgets[field_name] = widget

        # 删除当前行按钮
        del_btn = tk.Button(row_frame, text="删除本条", command=lambda: self.delete_row(row_idx), fg="red")
        del_btn.pack(side=tk.RIGHT, padx=5, pady=2)

        self.rows.append((row_frame, row_widgets))
        self.log(f"ℹ️ 已添加第 {row_idx+1} 条数据录入行")

    def delete_row(self, idx):
        """删除指定行"""
        if 0 <= idx < len(self.rows):
            frame, _ = self.rows.pop(idx)
            frame.destroy()
            self.log(f"ℹ️ 已删除第 {idx+1} 条数据录入行")
            # 重新编号
            for i, (frame, _) in enumerate(self.rows):
                frame.config(text=f"第 {i+1} 条数据")

    def clear_all_rows(self):
        """清空所有数据行"""
        for frame, _ in self.rows:
            frame.destroy()
        self.rows.clear()
        self.log("ℹ️ 已清空所有数据录入行")
        # 重新添加一条空行
        self.add_new_row()

    def submit_all_rows(self):
        """批量提交所有数据行到数据库"""
        if not self.rows:
            messagebox.showwarning("提示", "没有可提交的数据！")
            return

        # 读取所有数据
        data_list = []
        for row_idx, (_, widgets) in enumerate(self.rows):
            row_data = {}
            for field_name, widget in widgets.items():
                if isinstance(widget, scrolledtext.ScrolledText):
                    content = widget.get(1.0, tk.END).strip()
                else:
                    content = widget.get().strip()
                row_data[field_name] = content
            data_list.append(row_data)

        # 入库统计
        success_count = 0
        fail_count = 0
        db = None
        try:
            db = DBPlugin.DBPlugin(
                DB_CFG["mode"],
                DB_CFG["host"],
                DB_CFG["user"],
                DB_CFG["pwd"],
                3306,
                DB_CFG["db"],
                DB_CFG["table"]
            )
            self.log("=" * 50)
            self.log("开始批量提交数据...")

            for idx, data in enumerate(data_list, 1):
                if not any(data.values()):
                    self.log(f"⚠️ 第 {idx} 条数据全部为空，跳过")
                    continue
                try:
                    db.insert(data)
                    success_count += 1
                    self.log(f"✅ 第 {idx} 条数据入库成功 | 岗位：{data.get('岗位名称','无')}")
                except Exception as e:
                    fail_count += 1
                    self.log(f"❌ 第 {idx} 条数据入库失败：{str(e)}")

            db.close()
            self.log("=" * 50)
            self.log(f"📊 批量提交完成：成功 {success_count} 条，失败 {fail_count} 条")
            messagebox.showinfo("提交完成", f"成功入库：{success_count} 条\n失败：{fail_count} 条")
        except Exception as e:
            err_msg = f"❌ 数据库连接失败：{str(e)}"
            self.log(err_msg)
            messagebox.showerror("错误", err_msg)
            if db:
                db.close()

def run_manual_tool():
    root = tk.Tk()
    app = MultiManualInputWin(root)
    root.mainloop()

if __name__ == "__main__":
    run_manual_tool()