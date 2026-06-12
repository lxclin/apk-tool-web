import json
import re
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime
from PIL import Image, ImageTk
import os
import subprocess

from qr_generator import generate_qr
from auto_asana.main import (
    build_sync_clients,
    sync_packages,
    SHEET_ID as DEFAULT_SHEET_ID,
    SHEET_NAME as DEFAULT_SHEET_NAME,
    PROJECT_GID as DEFAULT_PROJECT_GID,
    ASANA_PAT as DEFAULT_ASANA_PAT,
    SA_FILE as DEFAULT_SA_FILE,
    PROXY_URL as DEFAULT_PROXY_URL,
    PARENT_TASK_GID as DEFAULT_PARENT_TASK_GID,
)
from adb_pusher import (
    check_device, push_apk, set_adb_path, get_adb_path,
    get_app_uid, start_logcat_stream, stop_logcat_stream,
    run_stream, cmd_to_str, clear_logcat_buffer,
    build_push_config_cmd, build_zygote_build_cmd,
    build_get_uid_cmd, build_clear_cache_cmd, build_force_stop_cmd,
    build_open_app_cmd, build_logcat_cmd,
    extract_logcat_fields, download_and_install,
)

CONFIG_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518/config.json"
)
WORK_DIR_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518"
)


class APKToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("APK 工具")
        self.root.geometry("780x820")
        self.root.resizable(True, True)
        self._qr_image: ImageTk.PhotoImage | None = None
        self._cached_uid: str | None = None
        self._logcat_proc: subprocess.Popen | None = None
        self._logcat_thread: threading.Thread | None = None
        self._active_pattern: str | None = None
        self._running_command = False
        self._last_fill_data: dict = {}
        self._fill_poller_running = True
        self._sync_running = False

        # ── 数据同步配置 ──
        self.sheet_id_var      = tk.StringVar(value=DEFAULT_SHEET_ID)
        self.sheet_name_var    = tk.StringVar(value=DEFAULT_SHEET_NAME)
        self.project_gid_var   = tk.StringVar(value=DEFAULT_PROJECT_GID)
        self.asana_pat_var     = tk.StringVar(value=DEFAULT_ASANA_PAT)
        self.sa_file_var       = tk.StringVar(value=DEFAULT_SA_FILE)
        self.proxy_url_var     = tk.StringVar(value=DEFAULT_PROXY_URL)
        self.parent_task_gid_var = tk.StringVar(value=DEFAULT_PARENT_TASK_GID)

        self._build_ui()
        self._update_adb_status()

        # 后台轮询 HTTP /fill/latest，接收适配表回填数据
        self._fill_poller = threading.Thread(
            target=self._fill_poller_loop, daemon=True
        )
        self._fill_poller.start()

    def _fill_poller_loop(self):
        """后台线程：轮询 localhost:9528/fill/latest，有新数据则回填"""
        while self._fill_poller_running:
            try:
                req = urllib.request.Request(
                    "http://localhost:9528/fill/latest"
                )
                resp = urllib.request.urlopen(req, timeout=2)
                body = json.loads(resp.read())
                fill = body.get("data", {})
                if fill and fill != self._last_fill_data:
                    self._last_fill_data = fill
                    self.root.after(0, lambda f=fill: self._apply_fill_data(f))
            except Exception:
                pass
            time.sleep(2)

    def _apply_fill_data(self, data: dict):
        """将回填数据写入输入框"""
        pkg = data.get("package_name") or data.get("packageName") or ""
        app_id = data.get("up2_appid") or data.get("appId") or ""
        gp_url = data.get("gp_url") or data.get("gpUrl") or ""
        filled = []
        if pkg:
            self.pkg_entry.delete(0, tk.END)
            self.pkg_entry.insert(0, pkg)
            filled.append("包名")
        if app_id:
            self.appid_entry.delete(0, tk.END)
            self.appid_entry.insert(0, app_id)
            filled.append("AppId")
        if gp_url:
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, gp_url)
            filled.append("GP链接")
        if filled:
            self.status_label.config(
                text=f"✅ 已自动填入: {', '.join(filled)}"
            )

    def _update_adb_status(self):
        adb = get_adb_path()
        if adb:
            self.status_label.config(text=f"就绪  |  ADB: {adb}")
        else:
            self.status_label.config(text="就绪  |  ADB: 未找到，请点击「设置ADB」指定路径")

    # ── APK 工具 Tab ──────────────────────────────────────────────

    def _build_apk_tab(self, parent: ttk.Frame):
        top = ttk.Frame(parent, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="APK/XAPK 路径 / Google Play 地址 / 下载直链:").pack(anchor=tk.W)
        self.url_entry = ttk.Entry(top)
        self.url_entry.pack(fill=tk.X, pady=(4, 8))

        btn_frame = ttk.Frame(top)
        btn_frame.pack()
        ttk.Button(btn_frame, text="生成二维码", command=self._on_generate_qr).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="推送安装", command=self._on_push_apk).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="APKCombo 下载", command=self._on_apkcombo_download).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="选择APK", command=self._on_browse).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="设置ADB", command=self._on_set_adb).pack(side=tk.LEFT, padx=3)

        # APKPure 搜索
        apkpure_frame = ttk.Frame(top)
        apkpure_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(apkpure_frame, text="APKPure 包名搜索 (手机打开):").pack(anchor=tk.W)
        search_row = ttk.Frame(apkpure_frame)
        search_row.pack(fill=tk.X, pady=(4, 0))
        self.apkpure_pkg_entry = ttk.Entry(search_row)
        self.apkpure_pkg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(search_row, text="搜索安装", command=self._on_apkpure_search).pack(side=tk.LEFT)

        qr_frame = ttk.LabelFrame(parent, text="二维码", padding=10)
        qr_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.qr_label = ttk.Label(qr_frame)
        self.qr_label.pack()

    # ── ADB 指令 Tab ──────────────────────────────────────────────

    def _build_adb_tab(self, parent: ttk.Frame):
        pad = {"padx": 10, "pady": 5}

        # --- 配置区域 ---
        config_frame = ttk.LabelFrame(parent, text="配置 (config.json)", padding=10)
        config_frame.pack(fill=tk.X, **pad)

        # 行1: 包名 + appId
        row1 = ttk.Frame(config_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="包名:").pack(side=tk.LEFT)
        self.pkg_entry = ttk.Entry(row1, width=30)
        self.pkg_entry.pack(side=tk.LEFT, padx=5)
        ttk.Label(row1, text="AppId:").pack(side=tk.LEFT, padx=(10, 0))
        self.appid_entry = ttk.Entry(row1, width=30)
        self.appid_entry.pack(side=tk.LEFT, padx=5)

        # 行2: taskUUID 下拉 + 读写按钮
        row2 = ttk.Frame(config_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="taskUUID:").pack(side=tk.LEFT)
        TASK_UUID_OPTIONS = [
            "mediation_test_snow",
            "test_snow",
            "rec_view_test_snow",
            "rec_window_test_snow",
        ]
        self.task_uuid_var = tk.StringVar(value=TASK_UUID_OPTIONS[0])
        self.task_uuid_combo = ttk.Combobox(
            row2, textvariable=self.task_uuid_var, values=TASK_UUID_OPTIONS,
            state="readonly", width=24,
        )
        self.task_uuid_combo.pack(side=tk.LEFT, padx=5)
        ttk.Button(row2, text="读取配置", command=self._on_load_config).pack(side=tk.RIGHT, padx=2)
        ttk.Button(row2, text="写入配置", command=self._on_save_config).pack(side=tk.RIGHT, padx=2)

        # 行3: Config 路径
        row3 = ttk.Frame(config_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Config:").pack(side=tk.LEFT)
        self.config_path_var = tk.StringVar(value=CONFIG_DEFAULT)
        self.config_entry = ttk.Entry(row3, textvariable=self.config_path_var)
        self.config_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row3, text="选择", command=self._on_browse_config).pack(side=tk.LEFT)

        # 行4: 工作目录
        row4 = ttk.Frame(config_frame)
        row4.pack(fill=tk.X, pady=2)
        ttk.Label(row4, text="工作目录:").pack(side=tk.LEFT)
        self.work_dir_var = tk.StringVar(value=WORK_DIR_DEFAULT)
        self.work_dir_entry = ttk.Entry(row4, textvariable=self.work_dir_var)
        self.work_dir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row4, text="cd 到此目录", command=self._on_cd_work_dir).pack(side=tk.LEFT)
        ttk.Button(row4, text="选择", command=self._on_browse_work_dir).pack(side=tk.LEFT)

        # 行5: UID 显示
        row5 = ttk.Frame(config_frame)
        row5.pack(fill=tk.X, pady=2)
        ttk.Label(row5, text="应用 UID:").pack(side=tk.LEFT)
        self.uid_var = tk.StringVar(value="(未获取)")
        self.uid_label = ttk.Label(row5, textvariable=self.uid_var, foreground="gray")
        self.uid_label.pack(side=tk.LEFT, padx=5)

        # --- 操作按钮 ---
        action_frame = ttk.LabelFrame(parent, text="操作指令", padding=10)
        action_frame.pack(fill=tk.X, **pad)

        self._op_buttons: list[ttk.Button] = []

        # 第一行：4 个按钮
        btn_row1 = ttk.Frame(action_frame)
        btn_row1.pack(fill=tk.X, pady=2)
        for text, cmd in [
            ("推送 Config", self._on_push_config),
            ("执行 zygote_build", self._on_zygote_build),
            ("获取应用 UID", self._on_get_uid),
            ("清除缓存", self._on_clear_cache),
        ]:
            b = ttk.Button(btn_row1, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self._op_buttons.append(b)

        # 第二行：3 个按钮
        btn_row2 = ttk.Frame(action_frame)
        btn_row2.pack(fill=tk.X, pady=2)
        for text, cmd in [
            ("强制停止", self._on_force_stop),
            ("打开应用", self._on_open_app),
            ("清空 Play Store 缓存", self._on_clear_play_store),
        ]:
            b = ttk.Button(btn_row2, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self._op_buttons.append(b)

        # --- Logcat 区域 ---
        logcat_frame = ttk.LabelFrame(parent, text="Logcat 实时监听", padding=10)
        logcat_frame.pack(fill=tk.X, **pad)

        # 按 UID 过滤（醒目独立按钮）
        uid_filter_row = ttk.Frame(logcat_frame)
        uid_filter_row.pack(fill=tk.X, pady=(0, 6))
        self._uid_filter_btn = ttk.Button(
            uid_filter_row, text="按 UID 过滤 ZGSDK.AutoDetector",
            command=self._on_uid_filter_logcat
        )
        self._uid_filter_btn.pack(side=tk.LEFT, padx=2)
        self._stop_logcat_btn = ttk.Button(
            uid_filter_row, text="停止监听", command=self._on_stop_logcat
        )
        self._stop_logcat_btn.pack(side=tk.LEFT, padx=2)
        self.monitor_label = ttk.Label(uid_filter_row, text="当前未监听", foreground="gray")
        self.monitor_label.pack(side=tk.LEFT, padx=10)

        # 崩溃日志按钮（独立一行，醒目，不需要 UID）
        crash_row = ttk.Frame(logcat_frame)
        crash_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(
            crash_row, text="Java 崩溃",
            command=lambda: self._on_start_logcat_no_uid("AndroidRuntime")
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            crash_row, text="Native 崩溃",
            command=lambda: self._on_start_logcat_no_uid("Fatal signal")
        ).pack(side=tk.LEFT, padx=2)
        ttk.Label(crash_row, text="← 游戏闪退后点这里输出崩溃堆栈", foreground="#81c784").pack(side=tk.LEFT, padx=8)

        # 字段提取按钮
        extract_row = ttk.Frame(logcat_frame)
        extract_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(
            extract_row, text="🔍 提取聚合参数",
            command=self._on_extract_fields
        ).pack(side=tk.LEFT, padx=2)
        ttk.Label(extract_row, text="← 从 ZGSDK.AutoDetector 日志提取 SDK Key、聚合ID、归因平台",
                  foreground="#81c784").pack(side=tk.LEFT, padx=8)

        # 其他过滤按钮
        btn_row2 = ttk.Frame(logcat_frame)
        btn_row2.pack(fill=tk.X, pady=2)
        patterns = [
            ("Max 聚合", "ZGSDK.Max"),
            ("IronSource", "ZGSDK.iron"),
            ("AdMob", "ZGSDK.admob"),
            ("Single", "ZGSDK.single"),
            ("Send", "ZGSDK.send"),
            ("插屏聚合ID", "MaxInterstitialAdMulti"),
            ("激励聚合ID", "MaxUnifiedAd"),
        ]
        for label, pattern in patterns:
            btn = ttk.Button(
                btn_row2, text=label,
                command=lambda p=pattern: self._on_start_logcat(p)
            )
            btn.pack(side=tk.LEFT, padx=2)

        # --- 输出控制台 ---
        output_frame = ttk.LabelFrame(parent, text="ADB 控制台", padding=5)
        output_frame.pack(fill=tk.BOTH, expand=True, **pad)

        output_toolbar = ttk.Frame(output_frame)
        output_toolbar.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(output_toolbar, text="清空", command=self._on_clear_output).pack(side=tk.RIGHT)

        self.output_text = tk.Text(
            output_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Menlo", 11), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", height=14,
        )
        scrollbar = ttk.Scrollbar(output_frame, command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=scrollbar.set)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 配置语法高亮 tag
        self.output_text.tag_config("cmd", foreground="#6acbff")
        self.output_text.tag_config("done", foreground="#81c784")
        self.output_text.tag_config("error", foreground="#ef5350")
        self.output_text.tag_config("logline", foreground="#d4d4d4")

    # ── 数据同步 Tab ──────────────────────────────────────────────

    def _build_sync_tab(self, parent: ttk.Frame):
        pad = {"padx": 10, "pady": 5}

        # --- 凭证配置 ---
        cfg_frame = ttk.LabelFrame(parent, text="API 凭证与目标", padding=10)
        cfg_frame.pack(fill=tk.X, **pad)

        rows = [
            ("Sheet ID",       self.sheet_id_var,      None),
            ("Sheet 名称",     self.sheet_name_var,    None),
            ("Asana 项目 GID", self.project_gid_var,   None),
            ("Asana PAT",      self.asana_pat_var,     None),
            ("SA 密钥文件",    self.sa_file_var,       self._on_browse_sa_file),
            ("代理地址",       self.proxy_url_var,     None),
            ("父任务 GID",     self.parent_task_gid_var, None),
        ]
        for label, var, browse_cmd in rows:
            row_frame = ttk.Frame(cfg_frame)
            row_frame.pack(fill=tk.X, pady=2)
            ttk.Label(row_frame, text=label + ":", width=15, anchor=tk.E).pack(side=tk.LEFT)
            entry = ttk.Entry(row_frame, textvariable=var)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            if browse_cmd:
                ttk.Button(row_frame, text="选择", command=browse_cmd).pack(side=tk.LEFT)

        # --- 操作按钮 ---
        action_frame = ttk.LabelFrame(parent, text="操作", padding=10)
        action_frame.pack(fill=tk.X, **pad)

        btn_row = ttk.Frame(action_frame)
        btn_row.pack()
        self._sync_btn = ttk.Button(
            btn_row, text="🔄 开始同步", command=self._on_start_sync
        )
        self._sync_btn.pack(side=tk.LEFT, padx=3)
        self._sync_status = ttk.Label(btn_row, text="就绪", foreground="gray")
        self._sync_status.pack(side=tk.LEFT, padx=10)

        # --- 同步输出 ---
        output_frame = ttk.LabelFrame(parent, text="同步输出", padding=5)
        output_frame.pack(fill=tk.BOTH, expand=True, **pad)

        toolbar = ttk.Frame(output_frame)
        toolbar.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(toolbar, text="清空", command=self._on_clear_sync_output).pack(side=tk.RIGHT)

        self.sync_output = tk.Text(
            output_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Menlo", 11), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", height=10,
        )
        sync_scrollbar = ttk.Scrollbar(output_frame, command=self.sync_output.yview)
        self.sync_output.configure(yscrollcommand=sync_scrollbar.set)
        self.sync_output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sync_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 配置 tag
        self.sync_output.tag_config("cmd", foreground="#6acbff")
        self.sync_output.tag_config("done", foreground="#81c784")
        self.sync_output.tag_config("error", foreground="#ef5350")
        self.sync_output.tag_config("info", foreground="#d4d4d4")

    def _on_browse_sa_file(self):
        path = filedialog.askopenfilename(
            title="选择 Google Service Account JSON 密钥文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.sa_file_var.set(path)

    def _on_clear_sync_output(self):
        self.sync_output.configure(state=tk.NORMAL)
        self.sync_output.delete("1.0", tk.END)
        self.sync_output.configure(state=tk.DISABLED)

    def _sync_log(self, text: str, tag: str = "info"):
        """向同步输出区追加一行日志。"""
        self.sync_output.configure(state=tk.NORMAL)
        self.sync_output.insert(tk.END, text + "\n", tag)
        self.sync_output.see(tk.END)
        self.sync_output.configure(state=tk.DISABLED)

    def _on_start_sync(self):
        """在后台线程中执行 Google Sheets → Asana 单向同步。"""
        if self._sync_running:
            self._sync_status.config(text="同步已在运行中", foreground="#ef5350")
            return

        # 读取配置
        sheet_id      = self.sheet_id_var.get().strip()
        sheet_name    = self.sheet_name_var.get().strip()
        project_gid   = self.project_gid_var.get().strip()
        asana_pat     = self.asana_pat_var.get().strip()
        sa_file       = self.sa_file_var.get().strip()
        proxy_url     = self.proxy_url_var.get().strip()
        parent_gid    = self.parent_task_gid_var.get().strip()

        # 基本校验
        missing = []
        if not sheet_id: missing.append("Sheet ID")
        if not project_gid: missing.append("Asana 项目 GID")
        if not asana_pat: missing.append("Asana PAT")
        if not sa_file: missing.append("SA 密钥文件")
        if missing:
            self._sync_log(f"配置缺失: {', '.join(missing)}", "error")
            self._sync_status.config(text="配置不完整", foreground="#ef5350")
            return

        if not os.path.isfile(sa_file):
            self._sync_log(f"SA 密钥文件不存在: {sa_file}", "error")
            self._sync_status.config(text="SA 文件不存在", foreground="#ef5350")
            return

        # 禁用按钮
        self._sync_running = True
        self._sync_btn.configure(state=tk.DISABLED)
        self._sync_status.config(text="同步中...", foreground="#ffa726")

        def _run():
            import traceback
            try:
                self.root.after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self.root.after(0, lambda: self._sync_log("  开始 Google Sheets → Asana 同步", "cmd"))
                self.root.after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self.root.after(0, lambda: self._sync_log(f"  Sheet    : {sheet_id} / {sheet_name}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  Project  : {project_gid}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  SA 文件  : {sa_file}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  代理     : {proxy_url or '(无)'}", "info"))
                self.root.after(0, lambda: self._sync_log("", "info"))

                # 1. 构建客户端
                self.root.after(0, lambda: self._sync_log("[1/3] 初始化认证 ...", "cmd"))
                gs_service, asana_client = build_sync_clients(
                    sa_file=sa_file,
                    asana_pat=asana_pat,
                    proxy_url=proxy_url or None,
                )
                self.root.after(0, lambda: self._sync_log("  认证通过 ✓", "done"))

                # 2. 执行同步
                self.root.after(0, lambda: self._sync_log("[2/3] 执行同步 ...", "cmd"))
                result = sync_packages(
                    gs_service=gs_service,
                    asana_client=asana_client,
                    sheet_id=sheet_id,
                    project_gid=project_gid,
                    sheet_name=sheet_name,
                )

                # 3. 展示结果
                self.root.after(0, lambda: self._sync_log("[3/3] 同步结果:", "cmd"))
                self.root.after(0, lambda: self._sync_log(f"  Sheet 匹配日期 : {result['sheet_date']}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  Asana 区段名称 : {result['section_name']}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  Asana 区段 GID  : {result['section_gid']}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  Sheet 筛选包数 : {result['total_packages']}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  Asana 已有任务 : {result['existing_count']}", "info"))
                self.root.after(0, lambda: self._sync_log(f"  本次新建任务   : {result['new_count']}", "done"))
                self.root.after(0, lambda: self._sync_log(f"  本次回填链接   : {result['backfilled_count']}", "info"))
                if result.get("backfill_skipped_reason"):
                    self.root.after(0, lambda: self._sync_log(
                        f"  回填跳过原因   : {result['backfill_skipped_reason']}", "error"
                    ))
                if result["created_gids"]:
                    self.root.after(0, lambda: self._sync_log(
                        f"  新建任务 GIDs  : {', '.join(result['created_gids'])}", "info"
                    ))
                self.root.after(0, lambda: self._sync_log("=" * 50, "cmd"))
                if result["new_count"] == 0:
                    self.root.after(0, lambda: self._sync_log("✓ 幂等：无需新建任务，所有包名已存在。", "done"))
                else:
                    self.root.after(0, lambda: self._sync_log(
                        f"✓ 同步完成：新建 {result['new_count']} 个任务。", "done"
                    ))
                self.root.after(0, lambda: self._sync_status.config(
                    text=f"完成 — 新建 {result['new_count']} 个任务", foreground="#81c784"
                ))
            except ImportError as e:
                self.root.after(0, lambda: self._sync_log(
                    f"缺少依赖: {e}\n请运行: pip install google-auth google-api-python-client asana", "error"
                ))
                self.root.after(0, lambda: self._sync_status.config(text="缺少依赖", foreground="#ef5350"))
            except Exception as e:
                self.root.after(0, lambda: self._sync_log(f"同步失败: {e}", "error"))
                self.root.after(0, lambda: self._sync_log(traceback.format_exc(), "error"))
                self.root.after(0, lambda: self._sync_status.config(text="同步失败", foreground="#ef5350"))
            finally:
                self.root.after(0, lambda: self._sync_btn.configure(state=tk.NORMAL))
                self.root.after(0, lambda: setattr(self, '_sync_running', False))

        threading.Thread(target=_run, daemon=True).start()

    # ── 整体布局 ──────────────────────────────────────────────────

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        apk_tab = ttk.Frame(notebook)
        notebook.add(apk_tab, text="APK 工具")
        self._build_apk_tab(apk_tab)

        adb_tab = ttk.Frame(notebook)
        notebook.add(adb_tab, text="ADB 指令")
        self._build_adb_tab(adb_tab)

        sync_tab = ttk.Frame(notebook)
        notebook.add(sync_tab, text="数据同步")
        self._build_sync_tab(sync_tab)

        self.status_label = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2)
        )
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X)

    # ── 控制台输出 ────────────────────────────────────────────────

    def _console_cmd(self, text: str):
        """输出命令提示符"""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, f"$ {text}\n", "cmd")
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _console_line(self, text: str, tag="logline"):
        """实时追加一行"""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, text + "\n", tag)
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _console_done(self, returncode: int):
        tag = "done" if returncode == 0 else "error"
        msg = f"[完成, exit={returncode}]\n"
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, msg, tag)
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _on_clear_output(self):
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.configure(state=tk.DISABLED)

    # ── 通用命令执行 ──────────────────────────────────────────────

    def _set_buttons_state(self, enabled: bool):
        self._running_command = not enabled
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in self._op_buttons:
            btn.configure(state=state)
        # 超时保护：30 秒后强制恢复按钮，防止命令卡死导致按钮永久禁用
        if not enabled:
            self.root.after(30000, lambda: self._set_buttons_state(True))

    def _cmd_display(self, cmd: list[str]) -> str:
        """将命令转为可读显示，adb 用简写"""
        result = " ".join(cmd)
        adb_path = get_adb_path()
        if adb_path:
            result = result.replace(adb_path, "adb")
        return result

    def _run_command(self, cmd: list[str], cwd=None):
        """在控制台中显示命令并流式执行"""
        self._console_cmd(self._cmd_display(cmd))
        self._set_buttons_state(False)
        self.status_label.config(text="执行中...")
        self.root.update()

        def on_line(line: str):
            self.root.after(0, self._console_line, line)

        def on_done(returncode: int):
            self.root.after(0, self._console_done, returncode)
            self.root.after(0, self._set_buttons_state, True)
            self.root.after(0, lambda: self.status_label.config(
                text="执行成功" if returncode == 0 else f"执行失败 (exit={returncode})"
            ))

        run_stream(cmd, on_line, on_done, cwd=cwd)

    def _maybe_auto_uid(self):
        """如果没有缓存 UID，尝试自动获取"""
        if self._cached_uid:
            return True
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            return False
        ok, uid = get_app_uid(pkg)
        if ok:
            self._cached_uid = uid
            self.uid_var.set(uid)
            self.uid_label.configure(foreground="black")
            return True
        return False

    # ── APK 工具事件 ──────────────────────────────────────────────

    def _on_generate_qr(self):
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.config(text="请输入 URL")
            return
        try:
            img = generate_qr(url)
            img = img.resize((400, 400))
            self._qr_image = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self._qr_image)
            self.status_label.config(text="二维码生成成功")
        except Exception as e:
            self.status_label.config(text=f"生成失败: {e}")

    def _on_push_apk(self):
        path = self.url_entry.get().strip()
        if not path:
            self.status_label.config(text="请输入 APK 路径或 Google Play 地址")
            return

        if path.startswith("http://") or path.startswith("https://"):
            self._open_url_on_device(path)
            return

        if not os.path.isfile(path):
            self.status_label.config(text="文件不存在，请检查路径")
            return

        self.status_label.config(text="正在检查设备...")
        self.root.update()

        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        self.status_label.config(text="正在安装...")
        self.root.update()

        ok, msg = push_apk(path)
        self.status_label.config(text=msg)

    def _open_url_on_device(self, url: str):
        adb = get_adb_path()
        if not adb:
            self.status_label.config(text="未找到 ADB 工具，请点击「设置ADB」指定路径")
            return
        self.status_label.config(text="正在打开手机上的应用页面...")
        self.root.update()
        try:
            subprocess.run(
                [adb, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                capture_output=True, text=True, timeout=15
            )
            self.status_label.config(text="已在手机上打开页面，请在手机上完成下载安装")
        except subprocess.TimeoutExpired:
            self.status_label.config(text="操作超时，请重试")

    def _on_browse(self):
        path = filedialog.askopenfilename(
            title="选择 APK 文件",
            filetypes=[("APK/XAPK files", "*.apk *.xapk"), ("APK files", "*.apk"), ("XAPK files", "*.xapk"), ("All files", "*.*")]
        )
        if path:
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, path)

    def _on_set_adb(self):
        path = filedialog.askopenfilename(
            title="选择 adb 可执行文件",
            filetypes=[("adb", "adb"), ("All files", "*")]
        )
        if path:
            set_adb_path(path)
            self._update_adb_status()

    def _on_apkcombo_download(self):
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.config(text="请输入 APKCombo 下载链接")
            return
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        # 如果是直链(.apk/.xapk) → 直接下载安装
        if url.endswith(".apk") or url.endswith(".xapk"):
            self._set_buttons_state(False)
            self.status_label.config(text="正在下载...")
            self.root.update()
            threading.Thread(
                target=self._do_apkcombo_download, args=(url,), daemon=True
            ).start()
            return

        # Google Play 链接 → 在电脑浏览器打开 apkcombo 下载页
        pkg_match = re.search(r"[?&]id=([^&]+)", url)
        if pkg_match:
            pkg = pkg_match.group(1)
            import webbrowser
            webbrowser.open(f"https://apkcombo.com/{pkg}/download/apk")
            self.status_label.config(
                text="已在电脑浏览器打开 apkcombo，下载后选择 xapk 文件并点「推送安装」"
            )
            return

        self.status_label.config(text="无法识别链接格式，请粘贴 .apk/.xapk 直链或 Google Play 地址")

    def _do_apkcombo_download(self, url):
        ok, msg = download_and_install(
            url,
            on_progress=lambda pct, text: self.root.after(
                0, lambda: self.status_label.config(text=text)
            )
        )
        self.root.after(0, lambda: self.status_label.config(
            text=f"安装{'成功' if ok else '失败'}: {msg}"
        ))
        self.root.after(0, lambda: self._set_buttons_state(True))
        if ok:
            self.root.after(0, lambda: self._console_line(f"[安装成功] {msg}", ""))

    def _on_apkpure_search(self):
        pkg = self.apkpure_pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        self.status_label.config(text="正在 APKPure 中搜索...")
        self.root.update()

        adb = get_adb_path()
        # 在手机端串联执行：打开 APKPure → 等加载 → 触发搜索 → 输入包名 → 回车
        script = (
            f"monkey -p com.apkpure.aegon -c android.intent.category.LAUNCHER 1; "
            f"sleep 2; "
            f"input keyevent 84; "
            f"sleep 0.5; "
            f"input text {pkg}; "
            f"sleep 0.3; "
            f"input keyevent 66"
        )
        cmd = [adb, "shell", script]
        self._run_command(cmd)

    # ── ADB 指令事件 ──────────────────────────────────────────────

    def _on_load_config(self):
        config_path = self.config_path_var.get().strip()
        if not os.path.isfile(config_path):
            self.status_label.config(text=f"配置文件不存在: {config_path}")
            return
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            packages = data.get("data", [])
            if packages:
                item = packages[0]
                self.pkg_entry.delete(0, tk.END)
                self.pkg_entry.insert(0, item.get("packageName", ""))
                self.appid_entry.delete(0, tk.END)
                self.appid_entry.insert(0, item.get("appId", ""))
                uuid = item.get("taskUUID", "")
                if uuid in self.task_uuid_combo["values"]:
                    self.task_uuid_var.set(uuid)
                else:
                    self.task_uuid_var.set("mediation_test_snow")
                self.status_label.config(text="已读取配置")
            else:
                self.status_label.config(text="配置文件中没有 data 字段")
        except Exception as e:
            self.status_label.config(text=f"读取配置失败: {e}")

    def _on_save_config(self):
        pkg = self.pkg_entry.get().strip()
        appid = self.appid_entry.get().strip()
        if not pkg:
            self.status_label.config(text="包名不能为空，写入已取消")
            return
        if not appid:
            self.status_label.config(text="AppId 不能为空，写入已取消")
            return
        config_path = self.config_path_var.get().strip()
        if not os.path.isfile(config_path):
            self.status_label.config(text=f"配置文件不存在: {config_path}")
            return
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            if not data.get("data"):
                data["data"] = [{}]
            item = data["data"][0]
            item["packageName"] = self.pkg_entry.get().strip()
            item["appId"] = self.appid_entry.get().strip()
            item["taskUUID"] = self.task_uuid_var.get()
            with open(config_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._console_cmd("# 配置已写入")
            self._console_line(
                f"  packageName={item['packageName']}  appId={item['appId']}  taskUUID={item['taskUUID']}",
                "done"
            )
            self.status_label.config(text="配置已写入")
        except Exception as e:
            self.status_label.config(text=f"写入配置失败: {e}")

    def _on_browse_config(self):
        path = filedialog.askopenfilename(
            title="选择 config.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.config_path_var.set(path)

    def _on_browse_work_dir(self):
        path = filedialog.askdirectory(title="选择工作目录 (包含 zygote_build.sh)")
        if path:
            self.work_dir_var.set(path)

    def _on_cd_work_dir(self):
        """cd 到工作目录 —— 设定工作上下文"""
        work_dir = self.work_dir_var.get().strip()
        if not os.path.isdir(work_dir):
            self.status_label.config(text=f"目录不存在: {work_dir}")
            self._console_line(f"cd: 目录不存在: {work_dir}", "error")
            return
        # 显示 cd 命令并确认
        self._console_cmd(f"cd {work_dir}")
        self._console_line(f"(当前工作目录已设定为: {work_dir})", "done")
        self.status_label.config(text=f"工作目录: {work_dir}")
        # 尝试自动读取该目录下的 config.json
        cfg = os.path.join(work_dir, "config.json")
        if os.path.isfile(cfg):
            self.config_path_var.set(cfg)

    def _on_push_config(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        config_path = self.config_path_var.get().strip()
        if not os.path.isfile(config_path):
            self.status_label.config(text=f"文件不存在: {config_path}")
            return
        cmd = build_push_config_cmd(config_path)
        self._run_command(cmd)

    def _on_zygote_build(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        work_dir = self.work_dir_var.get().strip()
        script_path = os.path.join(work_dir, "zygote_build.sh")
        if not os.path.isfile(script_path):
            self.status_label.config(text=f"脚本不存在: {script_path}")
            return
        cmd = build_zygote_build_cmd(work_dir)
        self._run_command(cmd, cwd=work_dir)

    def _on_get_uid(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return

        cmd = build_get_uid_cmd(pkg)

        def on_line(line: str):
            self.root.after(0, self._console_line, line)
            for m in re.finditer(r"userId=(\d+)", line):
                uid = m.group(1)
                self.root.after(0, lambda u=uid: self._set_uid(u))

        self._console_cmd(self._cmd_display(cmd))
        self._set_buttons_state(False)
        self.status_label.config(text="查询 UID...")
        self.root.update()

        def on_done(returncode: int):
            self.root.after(0, self._console_done, returncode)
            self.root.after(0, self._set_buttons_state, True)

        run_stream(cmd, on_line, on_done)

    def _set_uid(self, uid: str):
        self._cached_uid = uid
        self.uid_var.set(uid)
        self.uid_label.configure(foreground="black")
        self.status_label.config(text=f"UID: {uid}")

    def _on_clear_cache(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_clear_cache_cmd(pkg)
        self._run_command(cmd)

    def _on_force_stop(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_force_stop_cmd(pkg)
        self._run_command(cmd)

    def _on_open_app(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_open_app_cmd(pkg)
        self._run_command(cmd)

    def _on_clear_play_store(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        cmd = build_clear_cache_cmd("com.android.vending")
        self._run_command(cmd)

    # ── Logcat 流式监听 ───────────────────────────────────────────

    def _on_uid_filter_logcat(self):
        """按 UID 过滤 ZGSDK.AutoDetector — 核心功能"""
        self._start_logcat_stream("ZGSDK.AutoDetector", auto_uid=True)

    def _on_start_logcat(self, pattern: str):
        """通用 logcat 过滤（需要 UID）"""
        self._start_logcat_stream(pattern, require_uid=True)

    def _on_start_logcat_no_uid(self, pattern: str):
        """无需 UID 的 logcat 过滤（崩溃日志等系统级标签）"""
        self._start_logcat_stream(pattern, require_uid=False)

    def _start_logcat_stream(self, pattern: str, auto_uid: bool = False,
                             require_uid: bool = True):
        if self._logcat_proc is not None:
            self.status_label.config(
                text=f"正在监听 {self._active_pattern}，请先点击「停止监听」"
            )
            return

        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        # 决定是否需要 UID
        if require_uid:
            if auto_uid or not self._cached_uid:
                if not self._maybe_auto_uid():
                    self.status_label.config(text="无法获取 UID，请先点击「获取应用 UID」")
                    return
            uid = self._cached_uid
        else:
            uid = None

        self._active_pattern = pattern

        try:
            self._logcat_proc = start_logcat_stream(pattern, uid)
        except FileNotFoundError:
            self.status_label.config(text="未找到 ADB 工具")
            return

        # 显示命令
        uid_part = f" --uid={uid}" if uid else ""
        self._console_cmd(f"adb logcat{uid_part} | grep {pattern}")

        self.monitor_label.config(
            text=f"监听中: {pattern}" + (f" (uid={self._cached_uid})" if self._cached_uid else ""),
            foreground="red"
        )
        self.status_label.config(text=f"开始监听 {pattern} ...")

        def _read_stream():
            try:
                for line in self._logcat_proc.stdout:
                    if self._logcat_proc is None:
                        break
                    if pattern in line:
                        self.root.after(0, self._console_line, line.rstrip())
            except Exception:
                pass

        self._logcat_thread = threading.Thread(target=_read_stream, daemon=True)
        self._logcat_thread.start()

    def _on_stop_logcat(self):
        if self._logcat_proc is None:
            self.status_label.config(text="当前未在监听")
            return

        stop_logcat_stream(self._logcat_proc)
        self._logcat_proc = None
        self._logcat_thread = None
        pattern = self._active_pattern
        self._active_pattern = None

        self.monitor_label.config(text="当前未监听", foreground="gray")
        self.status_label.config(text=f"已停止监听 {pattern}")
        self._console_line(f"[停止监听: {pattern}]", "done")

    # ── 字段提取 ──────────────────────────────────────────────────

    def _on_extract_fields(self):
        """后台提取字段，完成后弹窗展示"""
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        self._set_buttons_state(False)
        self.status_label.config(text="正在提取聚合参数...")
        self._console_cmd("adb logcat -d | grep ZGSDK.AutoDetector (字段提取)")

        def _run():
            result = extract_logcat_fields()
            self.root.after(0, lambda: self._show_fields_popup(result))
            self.root.after(0, lambda: self._set_buttons_state(True))
            if not result.get("ok"):
                self.root.after(0, lambda: self._console_line(
                    f"[提取失败] {result.get('error', '未知错误')}", "error"
                ))
            else:
                sdk_count = len(result.get("SDK列表", []))
                self.root.after(0, lambda: self._console_line(
                    f"[提取完成] SDK:{sdk_count}个 | 判断:{result.get('最终判断','')}",
                    "done"
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _show_fields_popup(self, data: dict):
        """弹窗展示提取的字段，支持一键复制"""
        if not data.get("ok"):
            self.status_label.config(text=data.get("error", "提取失败"))
            self._console_line(f"[错误] {data.get('error')}", "error")
            return

        self.status_label.config(text="聚合参数提取完成")

        # 创建弹窗
        popup = tk.Toplevel(self.root)
        popup.title("🔍 聚合参数提取结果")
        popup.geometry("700x600")
        popup.minsize(600, 480)
        popup.resizable(True, True)
        popup.configure(bg="#ffffff")
        popup.transient(self.root)
        popup.grab_set()

        # 字段表格（用 Label + Frame 模拟）
        sdk_list = data.get("SDK列表", [])
        fields = [
            ("最终判断", data.get("最终判断", "")),
            ("初始Activity", data.get("初始Activity", "")),
            ("应用类型", data.get("应用类型", "")),
            ("激励视频聚合id", data.get("激励视频聚合id", "")),
            ("插屏聚合id", data.get("插屏聚合id", "")),
            ("归因平台", data.get("归因平台", "")),
        ]

        canvas = tk.Canvas(popup, bg="#ffffff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(popup, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=16, pady=12)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _add_row(parent, label, value, copy_text=None):
            row_frame = ttk.Frame(parent)
            row_frame.pack(fill=tk.X, pady=3)
            lbl = ttk.Label(row_frame, text=label, width=16, anchor=tk.E)
            lbl.pack(side=tk.LEFT, padx=(0, 8))
            if value:
                val_label = tk.Label(row_frame, text=value, anchor=tk.W,
                                     font=("Menlo", 11), fg="#333333", bg="#ffffff")
                val_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
                if copy_text:
                    ttk.Button(row_frame, text="复制",
                               command=lambda v=copy_text: self._copy_to_clipboard(v)
                               ).pack(side=tk.RIGHT, padx=(8, 0))
            else:
                tk.Label(row_frame, text="未提取到", anchor=tk.W,
                         font=("Menlo", 11), fg="#e53935", bg="#ffffff"
                         ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        for label, value in fields:
            _add_row(scroll_frame, label, value, value if value else None)

        # SDK 列表
        if sdk_list:
            _add_row(scroll_frame, "", "", None)  # 空行分隔
            for sdk in sdk_list:
                label = f"{sdk['名称']} SDK Key"
                key_val = sdk.get("key", "")
                copy_val = f"{sdk['名称']} SDK Key:{key_val}" if key_val else ""
                _add_row(scroll_frame, label, key_val, copy_val if key_val else None)

        # 原始日志
        raw_frame = ttk.LabelFrame(scroll_frame, text="原始日志", padding=5)
        raw_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        raw_text = tk.Text(
            raw_frame, wrap=tk.WORD, font=("Menlo", 10),
            bg="#f5f5f5", fg="#555555", height=8, state=tk.NORMAL
        )
        raw_text.insert("1.0", data.get("完整日志", "(暂无原始日志)"))
        raw_text.configure(state=tk.DISABLED)
        raw_text.pack(fill=tk.BOTH, expand=True)

        # 底部按钮
        btn_frame = ttk.Frame(scroll_frame)
        btn_frame.pack(fill=tk.X, pady=(12, 0))

        ttk.Button(
            btn_frame, text="📋 一键复制全部",
            command=lambda: self._copy_all_fields(data)
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="关闭",
            command=popup.destroy
        ).pack(side=tk.RIGHT, padx=2)

    @staticmethod
    def _copy_to_clipboard(text: str):
        """复制到剪贴板"""
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()

    @staticmethod
    def _copy_all_fields(data: dict):
        """复制全部字段（格式化，含 SDK 名称前缀）"""
        lines = [
            f"最终判断:{data.get('最终判断', '')}",
            f"初始Activity:{data.get('初始Activity', '')}",
        ]
        for sdk in data.get("SDK列表", []):
            name = sdk.get("名称", "未知")
            key = sdk.get("key", "")
            lines.append(f"{name} SDK Key:{key}")
        lines += [
            f"应用类型:{data.get('应用类型', '')}",
            f"激励视频聚合id:{data.get('激励视频聚合id', '')}",
            f"插屏聚合id:{data.get('插屏聚合id', '')}",
            f"归因平台:{data.get('归因平台', '')}",
        ]
        text = "\n".join(lines)
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
