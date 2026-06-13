# manual_input_win.py 纯视图层 | 仅UI渲染，所有逻辑托管 config_gui
import tkinter as tk
from tkinter import scrolledtext, ttk, filedialog
import concurrent.futures
import os
import json
from config_gui import (
    INPUT_FIELD_LIST,
    ui_log,
    UIRenderEngine,
    dialog_init_load,
    dialog_on_close,
    dialog_on_mouse_wheel,
    dialog_on_item_selected,
    dialog_auto_extract,
    dialog_manual_extract,
    dialog_confirm_to_form,
    dialog_clear_form,
    dialog_add_new_row,
    dialog_del_row,
    dialog_clear_all_rows,
    dialog_save_selected_item,
    dialog_add_current_row,
    dialog_submit_all,
    dialog_refresh_table_list,
    dialog_on_table_selected,
    dialog_table_add_empty,
    dialog_table_del_selected
)

__all__ = ["ManualInputDialog"]

class ManualInputDialog(tk.Toplevel):
    def __init__(self, parent, db_config, data_list=None):
        super().__init__(parent)
        self.mode = db_config["mode"]
        self.host = db_config["host"]
        self.user = db_config["user"]
        self.pwd = db_config["pwd"]
        self.db_name = db_config["db"]
        self.table_name = db_config["table"]
        self.raw_data_list = []  # 条目数据池
        self.cur_select_idx = -1
        self.running = True
        self.batch_char_limit = 512
        self.manual_result = ""
        self.current_table = ""
        self.rows = []
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        if self.mode == "inner_sqlite":
            self.db_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"inner_sqlite_{self.db_name}.db")
            self.db_display = f"SQLite: {os.path.basename(self.db_file)}"
        else:
            self.db_file = ""
            self.db_display = f"MySQL {self.host} / {self.db_name}"

        self.withdraw()
        self.title("单条文本抽取 | 自动+手动关键词提取")
        self.geometry("1500x1000")
        self.resizable(True, True)
        self._build_all_ui()
        self.deiconify()
        self.grab_set()
        self.after(10, dialog_init_load, self)

    def _build_all_ui(self):
        self._build_json_path_area()
        self._build_top_area()
        self._build_preview_area()
        self._build_manual_result_area()
        self._build_log_area()
        self._build_main_content()
        self._build_bottom_btn()

    # JSON路径栏：输入、浏览、加载按钮
    def _build_json_path_area(self):
        f = tk.Frame(self)
        f.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(f, text="JSON文件路径：", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=5)

        self.json_path_entry = tk.Entry(f, width=80)
        self.json_path_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        tk.Button(f, text="浏览", command=self._browse_json_file, width=8).pack(side=tk.LEFT, padx=5)
        tk.Button(f, text="加载JSON", command=self._load_json_and_append, bg="#32CD32", fg="white", width=10).pack(side=tk.LEFT, padx=5)

    # 浏览选择JSON文件
    def _browse_json_file(self):
        file_path = filedialog.askopenfilename(
            title="选择JSON文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if file_path:
            self.json_path_entry.delete(0, tk.END)
            self.json_path_entry.insert(0, file_path)

    # 加载JSON：追加条目，空列表时重新从第1条开始
    def _load_json_and_append(self):
        path = self.json_path_entry.get().strip()
        if not path:
            self.log("提示：请先填写或选择JSON文件路径")
            return
        if not os.path.isfile(path):
            self.log(f"错误：文件不存在 -> {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                json_data = json.load(f)

            # 写入主文本框
            content = json.dumps(json_data, ensure_ascii=False, indent=2)
            self.preview_text.delete(1.0, tk.END)
            self.preview_text.insert(tk.END, content)

            # 追加新条目
            self.raw_data_list.append(json_data)
            new_idx = len(self.raw_data_list) - 1
            self.cur_select_idx = new_idx

            # 更新下拉选项
            combo_vals = [f"第{i+1}条" for i in range(len(self.raw_data_list))]
            self.item_combo["values"] = combo_vals
            self.item_combo.current(new_idx)

            self.log(f"成功加载JSON：{os.path.basename(path)}，当前为第{len(self.raw_data_list)}条")
        except json.JSONDecodeError:
            self.log("错误：文件不是标准JSON格式")
        except Exception as e:
            self.log(f"加载失败：{str(e)}")

    def _build_top_area(self):
        f = tk.Frame(self)
        f.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(f, text="选择条目：", font=("微软雅黑", 10)).pack(side=tk.LEFT, padx=5)
        
        self.item_combo = ttk.Combobox(f, width=35, state="readonly")
        self.item_combo.pack(side=tk.LEFT, padx=5)
        self.item_combo.bind("<<ComboboxSelected>>", lambda e: dialog_on_item_selected(self))

        # 新增：删除当前条目按钮
        tk.Button(f, text="删除当前条目", command=self._delete_current_item, width=10, fg="red").pack(side=tk.LEFT, padx=5)

        tk.Label(f, text="每批字符数：").pack(side=tk.LEFT, padx=(15, 5))
        self.batch_entry = tk.Entry(f, width=6)
        self.batch_entry.insert(0, "512")
        self.batch_entry.pack(side=tk.LEFT)

        tk.Label(f, text="手动关键词：").pack(side=tk.LEFT, padx=(15, 5))
        self.manual_key_entry = tk.Entry(f, width=25)
        self.manual_key_entry.pack(side=tk.LEFT)

        tk.Button(f, text="模型自动抽取", command=lambda: dialog_auto_extract(self),
                  bg="#2E8B57", fg="white", width=14).pack(side=tk.LEFT, padx=10)
        tk.Button(f, text="手动关键词提取", command=lambda: self._do_manual_extract(),
                  bg="#4169E1", fg="white", width=14).pack(side=tk.LEFT, padx=5)
        tk.Button(f, text="确认填入表单", command=lambda: dialog_confirm_to_form(self),
                  bg="#FFA500", fg="white", width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(f, text="选中条目入库", width=12, command=lambda: dialog_save_selected_item(self)).pack(side=tk.LEFT, padx=5)
        tk.Button(f, text="清空表单", command=self._clear_all_items, width=10).pack(side=tk.LEFT, padx=5)

    # 删除当前选中条目
    def _delete_current_item(self):
        if self.cur_select_idx < 0 or len(self.raw_data_list) == 0:
            self.log("提示：暂无条目可删除")
            return
        
        # 移除当前索引数据
        del self.raw_data_list[self.cur_select_idx]
        
        # 重置选中与界面
        if len(self.raw_data_list) == 0:
            # 全部删空
            self.cur_select_idx = -1
            self.item_combo["values"] = []
            self.item_combo.set("")
            self.preview_text.delete(1.0, tk.END)
            self.manual_result_text.delete(1.0, tk.END)
            self.log("已删除最后一条条目，列表已清空")
        else:
            # 重新生成下拉列表，自动选中前一条
            new_vals = [f"第{i+1}条" for i in range(len(self.raw_data_list))]
            self.item_combo["values"] = new_vals
            # 选中删除位置的前一项
            new_idx = max(0, self.cur_select_idx - 1)
            self.cur_select_idx = new_idx
            self.item_combo.current(new_idx)
            # 刷新文本框内容
            content = json.dumps(self.raw_data_list[new_idx], ensure_ascii=False, indent=2)
            self.preview_text.delete(1.0, tk.END)
            self.preview_text.insert(tk.END, content)
            self.manual_result_text.delete(1.0, tk.END)
            self.log(f"删除成功，当前共{len(self.raw_data_list)}条，已切换至第{new_idx+1}条")

    # 清空全部条目，计数器重置
    def _clear_all_items(self):
        self.raw_data_list.clear()
        self.cur_select_idx = -1
        self.item_combo["values"] = []
        self.item_combo.set("")
        self.preview_text.delete(1.0, tk.END)
        self.manual_result_text.delete(1.0, tk.END)
        self.log("已清空所有条目，下次加载将重新从第1条开始")
        dialog_clear_form(self)

    # 手动关键词提取
    def _do_manual_extract(self):
        text = self.preview_text.get(1.0, tk.END).strip()
        keyword = self.manual_key_entry.get().strip()

        if not text:
            self.log("错误：当前条目清洗后文本为空，请先加载JSON文件")
            return
        if not keyword:
            self.log("错误：请先输入要提取的关键词")
            return

        results = []
        lines = text.splitlines()
        for line in lines:
            if keyword in line:
                results.append(line)

        if results:
            self.manual_result = "\n".join(results)
            self.manual_result_text.delete(1.0, tk.END)
            self.manual_result_text.insert(tk.END, self.manual_result)
            self.log(f"成功提取关键词「{keyword}」相关内容，共{len(results)}条")
        else:
            self.manual_result = ""
            self.manual_result_text.delete(1.0, tk.END)
            self.log(f"未找到包含关键词「{keyword}」的内容")

    def _build_preview_area(self):
        f = tk.LabelFrame(self, text="当前条目清洗后文本")
        f.pack(fill=tk.X, padx=10, pady=3)
        self.preview_text = tk.Text(f, height=8, wrap=tk.NONE, font=("微软雅黑", 9))
        self.scroll_x = ttk.Scrollbar(f, orient=tk.HORIZONTAL, command=self.preview_text.xview)
        self.preview_text.config(xscrollcommand=self.scroll_x.set)
        self.preview_text.pack(fill=tk.X, padx=5, pady=(5, 0), expand=True)
        self.scroll_x.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.render_engine = UIRenderEngine(self.preview_text, self)
        self.render_engine.start_render_loop()

    def _build_manual_result_area(self):
        f = tk.LabelFrame(self, text="手动提取结果预览")
        f.pack(fill=tk.X, padx=10, pady=3)
        self.manual_result_text = tk.Text(f, height=5, wrap=tk.NONE, font=("微软雅黑", 9))
        self.manual_scroll_x = ttk.Scrollbar(f, orient=tk.HORIZONTAL, command=self.manual_result_text.xview)
        self.manual_result_text.config(xscrollcommand=self.manual_scroll_x.set)
        self.manual_result_text.pack(fill=tk.X, padx=5, pady=(5, 0), expand=True)
        self.manual_scroll_x.pack(fill=tk.X, padx=5, pady=(0, 5))

    def _build_log_area(self):
        tk.Label(self, text="操作日志：").pack(anchor=tk.W, padx=10)
        self.log_box = scrolledtext.ScrolledText(self, width=100, height=4)
        self.log_box.pack(padx=10, fill=tk.X, expand=False)

    def _build_main_content(self):
        main_f = tk.Frame(self, height=400)
        main_f.pack(fill=tk.BOTH, expand=False, padx=5, pady=5)
        main_f.pack_propagate(False)

        left_f = tk.Frame(main_f, width=400, height=400)
        left_f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5,15), pady=5)
        left_f.pack_propagate(False)
        btn_f = tk.Frame(left_f)
        btn_f.pack(fill=tk.X, pady=5)
        tk.Button(btn_f, text="当前表单入库", command=lambda: dialog_add_current_row(self),
                  bg="#90EE90", width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_f, text="新增录入行", command=lambda: dialog_add_new_row(self), width=12).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_f, text="清空所有行", command=lambda: dialog_clear_all_rows(self), width=12).pack(side=tk.LEFT, padx=5)

        self.canvas = tk.Canvas(left_f, width=370, height=300, highlightthickness=0)
        self.row_container = tk.Frame(self.canvas)
        self.scrollbar = ttk.Scrollbar(left_f, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.create_window((0, 0), window=self.row_container, anchor="nw")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=5)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.row_container.bind("<Configure>", lambda e: self.canvas.config(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas.find_all()[0], width=e.width))
        self.canvas.bind_all("<MouseWheel>", lambda e: dialog_on_mouse_wheel(self, e))

        right_f = tk.Frame(main_f, width=520, height=400)
        right_f.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        right_f.pack_propagate(False)
        db_f = tk.Frame(right_f)
        db_f.pack(fill=tk.X, pady=5)
        tk.Label(db_f, text="当前库表：").grid(row=0, column=0, padx=5)
        self.db_show = tk.Entry(db_f, width=25, state="readonly")
        self.db_show.insert(0, self.db_display)
        self.db_show.grid(row=0, column=1, padx=5)
        tk.Button(db_f, text="刷新表", command=lambda: dialog_refresh_table_list(self), width=10).grid(row=0, column=2, padx=5)

        tk.Label(db_f, text="数据表：").grid(row=1, column=0, padx=5)
        self.table_combo = ttk.Combobox(db_f, width=18, state="readonly")
        self.table_combo.grid(row=1, column=1, padx=5)
        self.table_combo.bind("<<ComboboxSelected>>", lambda e: dialog_on_table_selected(self))

        table_btn_f = tk.Frame(right_f)
        table_btn_f.pack(fill=tk.X, pady=3)
        tk.Button(table_btn_f, text="新增空行", command=lambda: dialog_table_add_empty(self), width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(table_btn_f, text="删除选中", fg="red", command=lambda: dialog_table_del_selected(self), width=10).pack(side=tk.LEFT, padx=5)

        self.tree = ttk.Treeview(right_f, show="headings", height=12)
        y_scroll = ttk.Scrollbar(right_f, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(right_f, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.pack(fill=tk.BOTH, expand=True, pady=5)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_bottom_btn(self):
        f = tk.Frame(self)
        f.pack(fill=tk.X, padx=10, pady=8)
        tk.Button(f, text="批量提交全部", command=lambda: dialog_submit_all(self),
                  width=18, bg="#90EE90").pack(side=tk.LEFT, padx=10)
        tk.Button(f, text="关闭窗口", command=lambda: dialog_on_close(self), width=12).pack(side=tk.RIGHT, padx=10)

    def log(self, msg):
        ui_log(self.log_box, self, msg)