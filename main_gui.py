import sys, os, shutil, threading, time, warnings
warnings.filterwarnings("ignore", category=UserWarning)

# 路径初始化
if getattr(sys, "frozen", False):
    app_root = os.path.dirname(sys.executable)
else:
    app_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, app_root)
sys.path.insert(0, os.path.join(app_root, "plugins"))

# 全局导入
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import qiongqi_maw, DBPlugin
from config import *

# ========= 【删除原顶部 manual_input_win 导入，解决循环导入】 =========
# 原代码这一行直接删掉：from manual_input_win import ManualInputDialog

# 全局控件
root, url_box, log_box = None, None, None
combo_mode, ent_host, ent_user, ent_pwd = None, None, None, None
ent_db, ent_table, mode_var = None, None, None

# 基础工具
def log(msg):
    log_box.insert(tk.END, f"{msg}\n")
    log_box.see(tk.END)
    root.update_idletasks()

def clear_all():
    url_box.delete(1.0, tk.END)
    log_box.delete(1.0, tk.END)

def get_url_list():
    return [line.strip() for line in url_box.get(1.0, tk.END).splitlines() if line.strip()]

# 模式切换
def change_mode(event):
    if combo_mode.get() == COMBO_MODE_OPTIONS[0]:
        mode_var.set("out_mysql")
        ent_host.config(state="normal")
        ent_user.config(state="normal")
        ent_pwd.config(state="normal")
    else:
        mode_var.set(VAR_DEFAULT_MODE)
        ent_host.config(state="disabled")
        ent_user.config(state="disabled")
        ent_pwd.config(state="disabled")

# 数据库操作
def test_conn():
    try:
        db = DBPlugin.DBPlugin(mode_var.get(), ent_host.get().strip(), ent_user.get().strip(),
                               ent_pwd.get().strip(), 3306, ent_db.get().strip(), ent_table.get().strip())
        db.close()
        log(LOG_CONN_SUCC)
    except Exception as e:
        log(f"{LOG_DB_CONN_ERR.format(e=str(e))}")

def export_backup():
    m, dbn = mode_var.get(), ent_db.get().strip()
    bak_dir = "数据库备份"
    os.makedirs(bak_dir, exist_ok=True)
    if m == VAR_DEFAULT_MODE:
        src = f"inner_sqlite_{dbn}.db"
        if os.path.exists(src):
            dst = os.path.join(bak_dir, f"backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
            shutil.copy2(src, dst)
            log(f"{LOG_BACKUP_SUCC.format(dst=dst)}")
    else:
        log(LOG_MYSQL_BACKUP_TIP)

# 数据查看 & 预览
def open_data_viewer():
    try:
        import db_export_view_plugin
        db_export_view_plugin.DbViewExportPlugin({}).run()
    except Exception as e:
        log(f"{LOG_PLUGIN_ERR.format(e=str(e))}")

def open_preview_window():
    win = tk.Toplevel(root)
    win.title(BTN_PREVIEW)
    win.geometry("820x680")
    tk.Label(win, text=LABEL_URL_TIP).grid(row=0, column=0, sticky="w", padx=5, pady=5)
    url_ent = tk.Entry(win, width=90)
    url_ent.insert(0, PREVIEW_DEFAULT_URL)
    url_ent.grid(row=0, column=1, padx=5, pady=5)
    txt = scrolledtext.ScrolledText(win, width=100, height=2)
    txt.grid(row=2, column=0, columnspan=2, padx=5, pady=5)

    def preview():
        url = url_ent.get().strip()
        if not url:
            messagebox.showwarning("提示", MSG_WARN_NO_URL)
            return
        html = qiongqi_maw.gui_get_html_by_url(url)
        txt.delete(1.0, tk.END)
        txt.insert(tk.END, f"{LOG_PREVIEW_LEN.format(len=len(html), content=html[:3000])}")

    tk.Button(win, text=BTN_PREVIEW_RUN, command=preview).grid(row=1, column=0, padx=5, pady=5)
    tk.Button(win, text=BTN_PREVIEW_CLOSE, command=win.destroy).grid(row=1, column=1, padx=5, pady=5)

# 手动录入弹窗（【核心修改】函数内部动态导入，彻底解决循环导入）
def open_manual_input(data_list=None):
    """从主界面读取当前数据库配置，打开手动录入弹窗"""
    # 仅在调用时导入，打破循环依赖
    from manual_input_win import ManualInputDialog

    current_db_config = {
        "mode": mode_var.get(),
        "host": ent_host.get().strip(),
        "user": ent_user.get().strip(),
        "pwd": ent_pwd.get().strip(),
        "db": ent_db.get().strip(),
        "table": ent_table.get().strip()
    }
    # 把采集数据传给弹窗
    dialog = ManualInputDialog(root, current_db_config, data_list)
    dialog.grab_set()

# 采集任务【改造：收集数据，不直接入库，完成后打开选择录入窗口】
def run_common_mode():
    def task():
        url_list = get_url_list()
        if not url_list:
            log(LOG_NO_URL)
            return
        if len(url_list) > MAX_TASK_NUM:
            log(f"{LOG_MAX_URL.format(MAX_TASK_NUM=MAX_TASK_NUM)}")
            return

        log(LOG_COMMON_START)
        fail_cnt = 0
        data_list = qiongqi_maw.gui_batch_crawl(url_list, site_key=SITE_KEY, use_basnake=True)
        # 收集有效数据，不执行db.insert
        collect_data = []
        for idx, url, raw_data in zip(range(1, len(url_list)+1), url_list, data_list):
            if not raw_data:
                log(f"{LOG_NO_DATA.format(idx=idx, url=url)}")
                fail_cnt += 1
                if fail_cnt >= FAIL_BREAK_NUM:
                    log(LOG_MANY_FAIL)
                    break
                continue
            raw_data["job_url"] = url
            raw_data["source"] = SITE_KEY
            collect_data.append(raw_data)
            log(f"{LOG_INSERT_SUCC.format(idx=idx, url=url)}")
            fail_cnt = 0

        log(LOG_COMMON_END)
        # 采集完成，打开手动选择录入窗口，传入采集数据
        if collect_data:
            log(f"共采集到 {len(collect_data)} 条有效数据，已跳转至选择录入界面")
            root.after(0, open_manual_input, collect_data)
    threading.Thread(target=task, daemon=True).start()

def run_enhance_mode():
    def task():
        url_list = get_url_list()
        if not url_list:
            log(LOG_NO_URL)
            return
        if len(url_list) > MAX_TASK_NUM:
            log(f"{LOG_MAX_URL.format(MAX_TASK_NUM)}")
            return

        log(LOG_ENHANCE_START)
        fail_cnt = 0
        data_list = qiongqi_maw.gui_batch_crawl(url_list, site_key=SITE_KEY, use_basnake=False)
        # 收集有效数据，不直接入库
        collect_data = []
        for idx, url, raw_data in zip(range(1, len(url_list)+1), url_list, data_list):
            if not raw_data:
                log(f"{LOG_NO_DATA.format(idx=idx, url=url)}")
                fail_cnt += 1
                if fail_cnt >= FAIL_BREAK_NUM:
                    log(LOG_MANY_FAIL)
                    break
                continue
            raw_data["job_url"] = url
            raw_data["source"] = SITE_KEY
            collect_data.append(raw_data)
            log(f"{LOG_INSERT_SUCC.format(idx=idx, url=url)}")
            fail_cnt = 0

        log(LOG_ENHANCE_END)
        # 采集完成跳转选择录入窗口
        if collect_data:
            log(f"共采集到 {len(collect_data)} 条有效数据，已跳转至选择录入界面")
            root.after(0, open_manual_input, collect_data)
    threading.Thread(target=task, daemon=True).start()

# UI主布局
def main():
    global root, url_box, log_box, combo_mode, ent_host, ent_user, ent_pwd, ent_db, ent_table, mode_var
    root = tk.Tk()
    root.title(WINDOW_TITLE)
    root.geometry(f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}")
    mode_var = tk.StringVar(value=VAR_DEFAULT_MODE)

    # 数据库配置区
    top_frame = tk.Frame(root)
    top_frame.pack(anchor="w", padx=6, pady=3)
    tk.Label(top_frame, text=LABEL_STORAGE_MODE).grid(row=0, column=0)
    combo_mode = ttk.Combobox(top_frame, width=14, values=COMBO_MODE_OPTIONS, state="readonly")
    combo_mode.grid(row=0, column=1, padx=3)
    combo_mode.current(COMBO_DEFAULT_IDX)

    tk.Label(top_frame, text=LABEL_HOST).grid(row=0, column=2)
    ent_host = tk.Entry(top_frame, width=12)
    ent_host.insert(0, DB_HOST_DEFAULT)
    ent_host.grid(row=0, column=3)

    tk.Label(top_frame, text=LABEL_USER).grid(row=0, column=4)
    ent_user = tk.Entry(top_frame, width=10)
    ent_user.insert(0, DB_USER_DEFAULT)
    ent_user.grid(row=0, column=5)

    tk.Label(top_frame, text=LABEL_PWD).grid(row=0, column=6)
    ent_pwd = tk.Entry(top_frame, width=12, show="*")
    ent_pwd.grid(row=0, column=7)

    tk.Label(top_frame, text=LABEL_DB).grid(row=0, column=8)
    ent_db = tk.Entry(top_frame, width=11)
    ent_db.insert(0, DB_NAME_DEFAULT)
    ent_db.grid(row=0, column=9)

    tk.Label(top_frame, text=LABEL_TABLE).grid(row=1, column=0)
    ent_table = tk.Entry(top_frame, width=12)
    ent_table.insert(0, DB_TABLE_DEFAULT)
    ent_table.grid(row=1, column=1)

    combo_mode.bind("<<ComboboxSelected>>", change_mode)
    change_mode(None)

    # URL输入
    tk.Label(root, text=LABEL_URL_TIP).pack(anchor="w", padx=8, pady=2)
    url_box = scrolledtext.ScrolledText(root, width=118, height=5)
    url_box.pack(padx=8)

    # 按钮组1
    btn_frame1 = tk.Frame(root)
    btn_frame1.pack(pady=4)
    tk.Button(btn_frame1, text=BTN_COMMON, command=run_common_mode).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame1, text=BTN_ENHANCE, command=run_enhance_mode).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame1, text=BTN_PREVIEW, command=open_preview_window).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame1, text="📝 手动字段录入", command=open_manual_input).pack(side=tk.LEFT, padx=4)

    # 按钮组3
    btn_frame3 = tk.Frame(root)
    btn_frame3.pack(pady=4)
    tk.Button(btn_frame3, text=BTN_TEST_CONN, command=test_conn).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame3, text=BTN_BACKUP, command=export_backup).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame3, text=BTN_VIEW_DATA, command=open_data_viewer).pack(side=tk.LEFT, padx=4)
    tk.Button(btn_frame3, text=BTN_CLEAR_TXT, command=clear_all).pack(side=tk.LEFT, padx=4)

    # 日志区
    tk.Label(root, text=LABEL_LOG_TITLE).pack(anchor="w", padx=8)
    log_box = scrolledtext.ScrolledText(root, width=118, height=15)
    log_box.pack(padx=8, pady=4)

    log(LOG_INIT)
    log(LOG_TIP1)
    log(LOG_TIP2)
    root.mainloop()

if __name__ == "__main__":
    main()