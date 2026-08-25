import json
import re
import sys
import threading
import time
import traceback
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from PIL import Image, ImageTk
import os
import selectors
import subprocess

from qr_generator import generate_qr
from auto_asana.main import (
    add_precheck_comment_once,
    build_asana_client,
    build_sync_clients,
    get_asana_tasks_for_date,
    sync_packages,
    sync_cp_adapt_records_to_sheet,
    SHEET_ID as DEFAULT_SHEET_ID,
    SHEET_NAME as DEFAULT_SHEET_NAME,
    PROJECT_GID as DEFAULT_PROJECT_GID,
    ASANA_PAT as DEFAULT_ASANA_PAT,
    SA_FILE as DEFAULT_SA_FILE,
    PROXY_URL as DEFAULT_PROXY_URL,
    PARENT_TASK_GID as DEFAULT_PARENT_TASK_GID,
    CP_ADAPT_LIST_URL as DEFAULT_CP_ADAPT_LIST_URL,
    CP_ADAPT_TOKEN as DEFAULT_CP_ADAPT_TOKEN,
)
from adb_pusher import (
    check_device, push_apk, set_adb_path, get_adb_path,
    get_app_uid, start_logcat_stream, stop_logcat_stream,
    run_stream, cmd_to_str, clear_logcat_buffer,
    build_push_config_cmd, build_fix_zygotehole_permissions_cmd, build_zygote_build_cmd,
    build_get_uid_cmd, build_clear_cache_cmd, build_force_stop_cmd,
    build_open_app_cmd, build_logcat_cmd,
    list_third_party_packages, packages_to_uninstall,
    uninstall_third_party_package,
    extract_logcat_fields, download_and_install, download_and_install_apkcombo,
    build_backend_url, extract_uid_from_dumpsys, parse_fill_url,
    extract_google_play_package, is_apk_download_url,
    build_apkcombo_search_url,
    normalize_action_script_text,
    normalize_optional_parameter,
    DEFAULT_FOLLOWING_ACTION_MIN_DELAY_MS,
    DEFAULT_KEEP_THIRD_PARTY_PACKAGES,
    cancel_zygotehole_injection,
    install_google_play_app,
    is_package_installed,
    open_google_play_page,
    run_app_launch_precheck,
    PackageRuntimeMonitor,
    dismiss_notification_permission_dialog,
    run_google_play_precheck,
)
from ad_replay import (
    DEFAULT_REPLAY_TIMEOUT_SECONDS,
    INTERSTITIAL,
    REWARDED,
    ReplayExpectation,
    build_replay_failure_comment,
    run_ad_replay_check,
    split_ad_unit_ids,
    validate_replay_timeout,
)
from automation_adaptation import (
    add_automation_comment_once,
    attribution_gate_issue,
    build_aggregation_assessment,
    clear_backend_adaptation_via_api,
    detect_aggregation_with_one_retry,
    detection_field_issue,
    format_aggregation_fields,
    has_explicit_attribution,
    has_aggregation_type,
    is_inferred_aggregation_result,
    reconcile_detection_result,
    submit_backend_via_api,
    submit_precheck_blacklist_via_api,
    update_asana_aggregation_notes,
    INFERRED_AGGREGATION_FAILURE_NOTE,
)
from daily_summary import generate_daily_asana_summary
from cp_candidate_assignment import (
    assign_cp_candidates,
    load_cp_assignment_candidates,
)
from private_features import private_feature_enabled

CONFIG_DEFAULT = os.path.expanduser(
    "~/Documents/适配动作与聚合参数获取_260629/config.json"
)
MULTI_ID_REPLAY_TIMEOUT_SECONDS = 300
WORK_DIR_DEFAULT = os.path.expanduser(
    "~/Documents/适配动作与聚合参数获取_260629"
)

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "gui_settings.json")
CRASH_LOG = os.path.join(os.path.dirname(__file__), "gui_crash.log")
AUTOMATION_AGGREGATION_TASK_UUID = "mediation_test_snow"
AUTOMATION_INFRASTRUCTURE_FAILURE_CODES = frozenset(
    {
        "ADB_NOT_FOUND",
        "ADB_UNAUTHORIZED",
        "ADB_OFFLINE",
        "ADB_NO_DEVICE",
        "ADB_SERVER_TIMEOUT",
        "LOGCAT_READ_TIMEOUT",
        "LOGCAT_READ_FAILED",
    }
)


def load_gui_settings(settings_path: str = SETTINGS_PATH) -> dict:
    try:
        with open(settings_path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_gui_settings(settings: dict, settings_path: str = SETTINGS_PATH) -> None:
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def get_remembered_path(settings: dict, key: str, default: str) -> str:
    value = settings.get(key)
    return value if isinstance(value, str) and value.strip() else default


def get_remembered_text(settings: dict, key: str, default: str) -> str:
    value = settings.get(key)
    return value if isinstance(value, str) else default


def extract_asana_task_gid(value: str) -> str:
    """Extract the task GID from a pasted Asana task URL or a raw GID."""
    text = (value or "").strip()
    if re.fullmatch(r"\d+", text):
        return text

    # Inbox activity links identify the actual task after /item/.  The
    # trailing /story/ number is an activity/comment GID and must be ignored.
    match = re.search(r"/(?:item|task)/(\d+)(?=[/?#]|$)", text, re.IGNORECASE)
    if match:
        return match.group(1)

    # Canonical Asana links commonly use /0/<project_gid>/<task_gid>.
    match = re.search(
        r"app\.asana\.com/(?:0|1)/(\d+)/(\d+)(?=[/?#]|$)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(2)

    raise ValueError(
        "未从地址中找到 Asana 任务 GID；请粘贴包含 /item/、/task/ 的任务地址"
    )


def _setup_crash_log():
    """捕获未处理异常并写入日志文件"""
    def _handler(exc_type, exc_val, exc_tb):
        tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"\n=== CRASH {ts} ===\n{tb_str}\n"
        try:
            with open(CRASH_LOG, "a") as f:
                f.write(msg)
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_val, exc_tb)

    sys.excepthook = _handler


class APKToolApp:
    def __init__(self, root: tk.Tk):
        _setup_crash_log()
        self.root = root
        self.root.title("APK 工具")
        self.root.geometry("900x820")
        self.root.minsize(820, 640)
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
        self._precheck_running = False
        self._precheck_cancel_requested = False
        self._precheck_auto_adapt_btn = None
        self._precheck_tasks: dict[str, object] = {}
        self._precheck_new_task_gids: set[str] = set()
        self._precheck_manual_selection = False
        self._closing = False
        self._after_ids: set[str] = set()
        self._settings = load_gui_settings()
        self._sync_settings_after_id: str | None = None
        self._command_state_token = 0
        self._button_restore_after_id: str | None = None
        self._console_line_count = 0
        self._console_max_lines = 1200
        self._current_command_proc: subprocess.Popen | None = None
        self._op_buttons: list[ttk.Button] = []
        self.cleanup_preview_var = tk.StringVar(value="未预览")
        self.cleanup_progress_var = tk.IntVar(value=0)
        self.cleanup_progress_text_var = tk.StringVar(value="卸载进度：未开始")
        self.precheck_auto_install_var = tk.BooleanVar(value=True)
        self.precheck_download_limit_var = tk.StringVar(value="6")
        self.precheck_download_cooldown_var = tk.StringVar(value="60")
        self.precheck_launch_check_var = tk.BooleanVar(value=True)
        self.precheck_launch_observation_var = tk.StringVar(value="20")
        self.automation_task_gid_var = tk.StringVar(value="")
        self.automation_package_var = tk.StringVar(value="")
        self.automation_appid_var = tk.StringVar(value="")
        self.automation_replay_timeout_var = tk.StringVar(
            value=str(DEFAULT_REPLAY_TIMEOUT_SECONDS)
        )
        self._automation_task_notes = ""
        self._automation_precheck_item_id = ""
        self._automation_fields: dict = {}
        self._automation_replay_id_candidates: dict[str, tuple[str, ...]] = {}
        # Incremented whenever the current automation task/field extraction
        # context changes.  Delayed Tk callbacks from package A must not paint
        # its fields back onto package B after the batch switches tasks.
        self._automation_context_version = 0
        self._automation_running = False
        self._automation_stop_event = threading.Event()
        self._automation_pause_event = threading.Event()
        self._automation_batch_active = False
        self._automation_batch_btn = None
        self._automation_pause_btn = None
        self._daily_summary_running = False
        self._cp_candidate_running = False
        self._cp_candidate_preview: list[dict] = []
        self._cp_candidate_enabled = private_feature_enabled(
            "cp_candidate_assignment"
        )
        self.daily_summary_date_var = tk.StringVar(
            value=datetime.now().strftime("%Y-%m-%d")
        )
        self._syncing_cleanup_keep_text = False

        # ── 数据同步配置 ──
        self.sheet_id_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_sheet_id", DEFAULT_SHEET_ID)
        )
        self.sheet_name_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_sheet_name", DEFAULT_SHEET_NAME)
        )
        self.project_gid_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_project_gid", DEFAULT_PROJECT_GID)
        )
        self.asana_pat_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_asana_pat", DEFAULT_ASANA_PAT)
        )
        self.sa_file_var = tk.StringVar(
            value=get_remembered_path(self._settings, "sync_sa_file", DEFAULT_SA_FILE)
        )
        self.proxy_url_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_proxy_url", DEFAULT_PROXY_URL)
        )
        self.parent_task_gid_var = tk.StringVar(
            value=get_remembered_text(
                self._settings, "sync_parent_task_gid", DEFAULT_PARENT_TASK_GID
            )
        )
        self.parent_task_url_var = tk.StringVar(value="")
        self.cp_adapt_api_url_var = tk.StringVar(
            value=get_remembered_text(
                self._settings, "sync_cp_adapt_api_url", DEFAULT_CP_ADAPT_LIST_URL
            )
        )
        self.cp_adapt_x_token_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_cp_adapt_x_token", "")
        )
        self.cp_adapt_token_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_cp_adapt_token", DEFAULT_CP_ADAPT_TOKEN)
        )
        self.cp_adapt_assign_var = tk.StringVar(
            value=get_remembered_text(self._settings, "sync_cp_adapt_assign", "rain")
        )
        self.following_action_delay_var = tk.StringVar(
            value=str(DEFAULT_FOLLOWING_ACTION_MIN_DELAY_MS // 1000)
        )
        self.config_path_var = tk.StringVar(
            value=get_remembered_path(self._settings, "config_path", CONFIG_DEFAULT)
        )
        self.work_dir_var = tk.StringVar(
            value=get_remembered_path(self._settings, "work_dir", WORK_DIR_DEFAULT)
        )
        self.cleanup_keep_packages_var = tk.StringVar(
            value=get_remembered_text(
                self._settings,
                "cleanup_keep_packages",
                ", ".join(DEFAULT_KEEP_THIRD_PARTY_PACKAGES),
            )
        )

        self._build_ui()
        self._setup_sync_settings_memory()
        self._update_adb_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")

        # 由 main() 在窗口构建完成后启动，避免仅构建窗口的调用方产生
        # 不必要的网络轮询线程。
        self._fill_poller: threading.Thread | None = None

    def start_background_services(self):
        """Start long-running desktop services after the UI is ready."""
        self._start_fill_poller()

    def _on_root_destroy(self, event):
        if event.widget is self.root:
            self._closing = True
            self._fill_poller_running = False

    def _root_exists(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except (tk.TclError, RuntimeError):
            return False

    def _safe_after(self, delay_ms: int, callback, *args):
        """Schedule a Tk callback only while the main window is alive."""
        if self._closing or not self._root_exists():
            return None
        if delay_ms == 0 and threading.current_thread() is threading.main_thread():
            callback(*args)
            return None

        after_id = None

        def _run():
            if after_id is not None:
                self._after_ids.discard(after_id)
            if self._closing or not self._root_exists():
                return
            try:
                callback(*args)
            except tk.TclError:
                if not self._closing:
                    raise

        try:
            after_id = self.root.after(delay_ms, _run)
            self._after_ids.add(after_id)
            return after_id
        except (RuntimeError, tk.TclError):
            return None

    def _cancel_pending_afters(self):
        for after_id in list(self._after_ids):
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
            finally:
                self._after_ids.discard(after_id)

    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._fill_poller_running = False
        if self._logcat_proc is not None:
            try:
                stop_logcat_stream(self._logcat_proc, timeout=0.2)
            except Exception:
                pass
            self._logcat_proc = None
            self._logcat_thread = None
            self._active_pattern = None
        self._automation_stop_event.set()
        self._cancel_pending_afters()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

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
                    self._safe_after(0, self._apply_fill_data, fill)
            except Exception:
                pass
            time.sleep(2)

    def _start_fill_poller(self):
        if self._closing or not self._fill_poller_running:
            return
        if self._fill_poller is not None and self._fill_poller.is_alive():
            return
        self._fill_poller = threading.Thread(
            target=self._fill_poller_loop, daemon=True
        )
        self._fill_poller.start()

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
        btn_frame.pack(fill=tk.X)
        first_row = ttk.Frame(btn_frame)
        first_row.pack(anchor=tk.W)
        second_row = ttk.Frame(btn_frame)
        second_row.pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(first_row, text="生成二维码", command=self._on_generate_qr).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(first_row, text="解析URL", command=self._on_parse_fill_url).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(first_row, text="推送安装", command=self._on_push_apk).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(first_row, text="APKCombo 下载", command=self._on_apkcombo_download).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(second_row, text="选择APK", command=self._on_browse).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(second_row, text="选择目录", command=self._on_browse_apk_dir).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(second_row, text="设置ADB", command=self._on_set_adb).pack(side=tk.LEFT, padx=(0, 6))

        # APKPure 搜索
        apkpure_frame = ttk.Frame(top)
        apkpure_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(apkpure_frame, text="APKPure 包名搜索 (手机打开):").pack(anchor=tk.W)
        search_row = ttk.Frame(apkpure_frame)
        search_row.pack(fill=tk.X, pady=(4, 0))
        self.apkpure_pkg_entry = ttk.Entry(search_row)
        self.apkpure_pkg_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(search_row, text="搜索安装", command=self._on_apkpure_search).pack(side=tk.LEFT)

        cleanup_frame = ttk.LabelFrame(top, text="第三方包清理", padding=10)
        cleanup_frame.pack(fill=tk.X, pady=(10, 0))

        cleanup_row = ttk.Frame(cleanup_frame)
        cleanup_row.pack(fill=tk.X, pady=2)
        ttk.Label(cleanup_row, text="保留包名:").pack(side=tk.LEFT, anchor=tk.N, pady=4)

        keep_text_frame = ttk.Frame(cleanup_row)
        keep_text_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.cleanup_keep_entry = tk.Text(
            keep_text_frame,
            height=3,
            wrap=tk.WORD,
            font=("Menlo", 10),
            bg="white",
            fg="black",
            insertbackground="black",
            relief=tk.SOLID,
            borderwidth=1,
        )
        keep_scrollbar = ttk.Scrollbar(
            keep_text_frame, command=self.cleanup_keep_entry.yview
        )
        self.cleanup_keep_entry.configure(yscrollcommand=keep_scrollbar.set)
        self.cleanup_keep_entry.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        keep_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.cleanup_keep_entry.insert("1.0", self.cleanup_keep_packages_var.get())
        self.cleanup_keep_entry.bind("<KeyRelease>", self._on_cleanup_keep_text_changed)
        self.cleanup_keep_entry.bind("<FocusOut>", self._on_cleanup_keep_text_changed)
        self.cleanup_keep_packages_var.trace_add("write", self._on_cleanup_keep_var_changed)

        cleanup_btn_row = ttk.Frame(cleanup_frame)
        cleanup_btn_row.pack(fill=tk.X, pady=(6, 0))
        for text, cmd in [
            ("预览清理", self._on_preview_third_party_cleanup),
            ("一键清理第三方包", self._on_cleanup_third_party_packages),
        ]:
            b = ttk.Button(cleanup_btn_row, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self._op_buttons.append(b)
        ttk.Label(
            cleanup_btn_row,
            text="ADB 页包名输入框里的包会自动保留",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=8)

        self.cleanup_preview_label = ttk.Label(
            cleanup_frame,
            textvariable=self.cleanup_preview_var,
            foreground="gray",
            anchor=tk.W,
        )
        self.cleanup_preview_label.pack(fill=tk.X, pady=(8, 4))

        cleanup_progress_row = ttk.Frame(cleanup_frame)
        cleanup_progress_row.pack(fill=tk.X, pady=(0, 6))
        self.cleanup_progress = ttk.Progressbar(
            cleanup_progress_row,
            mode="determinate",
            maximum=1,
            variable=self.cleanup_progress_var,
        )
        self.cleanup_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            cleanup_progress_row,
            textvariable=self.cleanup_progress_text_var,
            anchor=tk.W,
        ).pack(side=tk.LEFT, padx=(8, 0))

        preview_frame = ttk.Frame(cleanup_frame)
        preview_frame.pack(fill=tk.BOTH, expand=True)
        self.cleanup_preview_text = tk.Text(
            preview_frame,
            height=7,
            wrap=tk.NONE,
            state=tk.DISABLED,
            font=("Menlo", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
        )
        preview_scrollbar = ttk.Scrollbar(
            preview_frame, command=self.cleanup_preview_text.yview
        )
        self.cleanup_preview_text.configure(yscrollcommand=preview_scrollbar.set)
        self.cleanup_preview_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        preview_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.cleanup_preview_text.tag_config("done", foreground="#81c784")
        self.cleanup_preview_text.tag_config("error", foreground="#ef5350")
        self.cleanup_preview_text.tag_config("keep", foreground="#6acbff")
        self.cleanup_preview_text.tag_config("target", foreground="#ffb74d")

        qr_frame = ttk.LabelFrame(parent, text="二维码", padding=10)
        qr_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.qr_label = ttk.Label(qr_frame)
        self.qr_label.pack()

    # ── Google Play 页面预检 Tab ────────────────────────────────

    def _build_precheck_tab(self, parent: ttk.Frame):
        pad = {"padx": 10, "pady": 6}
        today_section = f"{datetime.now().month}.{datetime.now().day}执行"

        intro = ttk.LabelFrame(parent, text="预检说明", padding=10)
        intro.pack(fill=tk.X, **pad)
        ttk.Label(
            intro,
            text=(
                "优先读取手机 UI 控件，信息不足时自动使用内存截图 OCR，不保留正常截图。"
                "检测到加黑结论后，自动使用“数据同步”页后台凭证提交加黑原因并刷新缓存；"
                "提交失败会保留明确状态和 Asana 失败评论。"
            ),
            foreground="gray",
            wraplength=800,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        asana_frame = ttk.LabelFrame(
            parent, text=f"今日 Asana 任务（仅 {today_section}）", padding=8
        )
        asana_frame.pack(fill=tk.BOTH, expand=False, **pad)
        asana_toolbar = ttk.Frame(asana_frame)
        asana_toolbar.pack(fill=tk.X, pady=(0, 5))
        self._precheck_load_asana_btn = ttk.Button(
            asana_toolbar,
            text="读取今日任务",
            command=self._on_load_today_asana_tasks,
        )
        self._precheck_load_asana_btn.pack(side=tk.LEFT)
        self._precheck_batch_btn = ttk.Button(
            asana_toolbar,
            text="批量预检今日待处理",
            command=self._on_start_batch_precheck,
        )
        self._precheck_batch_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._precheck_auto_adapt_btn = ttk.Button(
            asana_toolbar,
            text="批量预检并自动适配",
            command=self._on_start_precheck_then_automation,
        )
        self._precheck_auto_adapt_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._precheck_stop_btn = ttk.Button(
            asana_toolbar,
            text="停止",
            command=self._on_stop_batch_precheck,
            state=tk.DISABLED,
        )
        self._precheck_stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(
            asana_toolbar,
            text="自动跳过已完成任务",
            foreground="gray",
        ).pack(side=tk.LEFT, padx=10)
        self._precheck_asana_status = ttk.Label(
            asana_toolbar, text="尚未读取", foreground="gray"
        )
        self._precheck_asana_status.pack(side=tk.RIGHT)

        task_table_frame = ttk.Frame(asana_frame)
        task_table_frame.pack(fill=tk.BOTH, expand=True)
        self.precheck_task_tree = ttk.Treeview(
            task_table_frame,
            columns=("order", "package", "gp", "status"),
            show="headings",
            height=6,
            selectmode="browse",
        )
        self.precheck_task_tree.heading("order", text="顺序")
        self.precheck_task_tree.heading("package", text="包名")
        self.precheck_task_tree.heading("gp", text="GP 链接")
        self.precheck_task_tree.heading("status", text="任务状态")
        self.precheck_task_tree.column(
            "order", width=48, minwidth=48, anchor=tk.CENTER, stretch=False
        )
        self.precheck_task_tree.column("package", width=260, minwidth=220)
        self.precheck_task_tree.column("gp", width=390, minwidth=280)
        # Statuses such as "APKCombo待确认" and "已加黑(后台)" were clipped
        # by the old fixed 80 px column. Keep enough reserved space for the
        # complete workflow state while allowing package/GP columns to absorb
        # window resizing.
        self.precheck_task_tree.column(
            "status", width=180, minwidth=160, anchor=tk.W, stretch=False
        )
        task_scrollbar = ttk.Scrollbar(
            task_table_frame, orient=tk.VERTICAL, command=self.precheck_task_tree.yview
        )
        self.precheck_task_tree.configure(yscrollcommand=task_scrollbar.set)
        self.precheck_task_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        task_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.precheck_task_tree.bind("<<TreeviewSelect>>", self._on_precheck_task_selected)

        inspect_frame = ttk.LabelFrame(parent, text="页面检查", padding=10)
        inspect_frame.pack(fill=tk.X, **pad)
        ttk.Label(inspect_frame, text="GP 链接或包名:").pack(anchor=tk.W)
        input_row = ttk.Frame(inspect_frame)
        input_row.pack(fill=tk.X, pady=(4, 6))
        self.precheck_input = ttk.Entry(input_row)
        self.precheck_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            input_row, text="从 APK 工具带入", command=self._on_precheck_use_apk_input
        ).pack(side=tk.LEFT, padx=(6, 0))

        action_row = ttk.Frame(inspect_frame)
        action_row.pack(fill=tk.X)
        ttk.Button(
            action_row, text="只打开页面", command=self._on_precheck_open_page
        ).pack(side=tk.LEFT)
        self._precheck_start_btn = ttk.Button(
            action_row, text="开始页面预检", command=self._on_start_precheck
        )
        self._precheck_start_btn.pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(
            action_row,
            text="检测到广告后自动下载安装",
            variable=self.precheck_auto_install_var,
        ).pack(side=tk.LEFT, padx=(4, 6))
        ttk.Label(action_row, text="单批上限").pack(side=tk.LEFT)
        ttk.Spinbox(
            action_row,
            from_=1,
            to=10,
            width=3,
            textvariable=self.precheck_download_limit_var,
        ).pack(side=tk.LEFT, padx=(3, 3))
        ttk.Label(action_row, text="个，间隔").pack(side=tk.LEFT)
        ttk.Spinbox(
            action_row,
            from_=0,
            to=600,
            increment=10,
            width=4,
            textvariable=self.precheck_download_cooldown_var,
        ).pack(side=tk.LEFT, padx=(3, 3))
        ttk.Label(action_row, text="秒").pack(side=tk.LEFT, padx=(0, 6))
        self._precheck_status = ttk.Label(action_row, text="就绪", foreground="gray")
        self._precheck_status.pack(side=tk.LEFT, padx=8)

        launch_row = ttk.Frame(inspect_frame)
        launch_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Checkbutton(
            launch_row,
            text="安装完成后启动应用检查闪退",
            variable=self.precheck_launch_check_var,
        ).pack(side=tk.LEFT)
        ttk.Label(launch_row, text="观察").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Spinbox(
            launch_row,
            from_=5,
            to=120,
            increment=5,
            width=4,
            textvariable=self.precheck_launch_observation_var,
        ).pack(side=tk.LEFT, padx=3)
        ttk.Label(
            launch_row,
            text="秒；检查结束后自动强制停止应用，再处理下一条",
            foreground="gray",
        ).pack(side=tk.LEFT)

        result_frame = ttk.LabelFrame(parent, text="预检结果", padding=8)
        result_frame.pack(fill=tk.BOTH, expand=True, **pad)
        self._precheck_result_title = ttk.Label(
            result_frame, text="等待检查", font=("TkDefaultFont", 13, "bold")
        )
        self._precheck_result_title.pack(anchor=tk.W, pady=(0, 5))
        output_wrap = ttk.Frame(result_frame)
        output_wrap.pack(fill=tk.BOTH, expand=True)
        self.precheck_output = tk.Text(
            output_wrap,
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=10,
            font=("Menlo", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
        )
        result_scrollbar = ttk.Scrollbar(output_wrap, command=self.precheck_output.yview)
        self.precheck_output.configure(yscrollcommand=result_scrollbar.set)
        self.precheck_output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        result_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _set_precheck_input(self, value: str):
        if not value:
            return
        self.precheck_input.delete(0, tk.END)
        self.precheck_input.insert(0, value)

    def _on_precheck_use_apk_input(self):
        self._set_precheck_input(self.url_entry.get().strip())

    def _on_precheck_task_selected(self, _event=None):
        if _event is not None:
            self._precheck_manual_selection = True
        selected = self.precheck_task_tree.selection()
        if not selected:
            return
        task = self._precheck_tasks.get(selected[0])
        if task is None:
            return
        value = getattr(task, "gp_link", "") or getattr(task, "package_name", "")
        self._set_precheck_input(value)

    def _selected_precheck_task(self):
        selected = self.precheck_task_tree.selection()
        if not selected:
            return None, None
        item_id = selected[0]
        return item_id, self._precheck_tasks.get(item_id)

    def _precheck_batch_queue_from_selection(self):
        """Return only newly discovered tasks unless an old row is selected."""
        ordered_item_ids = list(self.precheck_task_tree.get_children())
        selected = self.precheck_task_tree.selection()
        start_index = 0
        if selected and selected[0] in ordered_item_ids:
            start_index = ordered_item_ids.index(selected[0])
        selected_gid = ""
        if selected:
            selected_task = self._precheck_tasks.get(selected[0])
            selected_gid = str(getattr(selected_task, "gid", "") or "")
        if not self._precheck_new_task_gids and not self._precheck_manual_selection:
            return [], start_index + 1
        new_only = bool(self._precheck_new_task_gids) and (
            not selected_gid or selected_gid in self._precheck_new_task_gids
        )
        queue = []
        for item_id in ordered_item_ids[start_index:]:
            task = self._precheck_tasks.get(item_id)
            gid = str(getattr(task, "gid", "") or "")
            values = self.precheck_task_tree.item(item_id, "values")
            status = str(values[3] or "") if len(values) >= 4 else ""
            if (
                task is None
                or getattr(task, "completed", False)
                or getattr(task, "workflow_terminal", False)
                or (new_only and gid not in self._precheck_new_task_gids)
                or (
                    new_only
                    and status not in {"新增待预检", "APKCombo有包"}
                )
            ):
                continue
            queue.append((item_id, task))
        return queue, start_index + 1

    def _precheck_seen_key(self) -> str:
        project_gid = self.project_gid_var.get().strip()
        return f"{project_gid}:{datetime.now().date().isoformat()}"

    def _known_precheck_gids(self) -> set[str]:
        records = self._settings.get("precheck_seen_task_gids")
        if not isinstance(records, dict):
            return set()
        values = records.get(self._precheck_seen_key())
        return {str(value) for value in values} if isinstance(values, list) else set()

    def _remember_precheck_gids(self, gids: set[str]):
        records = self._settings.get("precheck_seen_task_gids")
        records = dict(records) if isinstance(records, dict) else {}
        records[self._precheck_seen_key()] = sorted(gids)
        # Keep the settings file bounded while preserving recent workdays.
        if len(records) > 14:
            records = dict(sorted(records.items())[-14:])
        self._settings["precheck_seen_task_gids"] = records
        try:
            save_gui_settings(self._settings)
        except OSError as exc:
            self._precheck_asana_status.config(
                text=f"任务增量记录保存失败: {exc}", foreground="#ef5350"
            )

    def _pending_precheck_gids(self) -> set[str]:
        records = self._settings.get("precheck_pending_task_gids")
        if not isinstance(records, dict):
            return set()
        values = records.get(self._precheck_seen_key())
        return {str(value) for value in values} if isinstance(values, list) else set()

    def _remember_pending_precheck_gids(self, gids: set[str]):
        records = self._settings.get("precheck_pending_task_gids")
        records = dict(records) if isinstance(records, dict) else {}
        records[self._precheck_seen_key()] = sorted(gids)
        if len(records) > 14:
            records = dict(sorted(records.items())[-14:])
        self._settings["precheck_pending_task_gids"] = records
        try:
            save_gui_settings(self._settings)
        except OSError as exc:
            self._precheck_asana_status.config(
                text=f"增量待办记录保存失败: {exc}", foreground="#ef5350"
            )

    def _set_precheck_task_status(self, item_id: str, status: str):
        if not item_id or not self.precheck_task_tree.exists(item_id):
            return
        values = list(self.precheck_task_tree.item(item_id, "values"))
        if len(values) >= 4:
            values[3] = status
            self.precheck_task_tree.item(item_id, values=values)
        task = self._precheck_tasks.get(item_id)
        gid = str(getattr(task, "gid", "") or "")
        active_statuses = {
            "新增待预检",
            "检查中",
            "准备下载",
            "下载中",
            "补下载中",
            "启动检查",
            "后台下载中",
            "待补下载",
            "下载已暂停",
        }
        if gid in self._precheck_new_task_gids and status not in active_statuses:
            self._precheck_new_task_gids.discard(gid)
            self._remember_pending_precheck_gids(self._precheck_new_task_gids)

    def _refresh_completed_background_downloads(self) -> int:
        """Replace stale download states when the package is now installed."""
        background_statuses = {
            "准备下载",
            "下载中",
            "后台下载中",
            "补下载中",
            "待补下载",
            "下载已暂停",
        }
        refreshed = 0
        for item_id in self.precheck_task_tree.get_children():
            values = self.precheck_task_tree.item(item_id, "values")
            status = str(values[3] if len(values) >= 4 else "").strip()
            if status not in background_statuses:
                continue
            task = self._precheck_tasks.get(item_id)
            package_name = str(getattr(task, "package_name", "") or "").strip()
            if package_name and is_package_installed(package_name):
                self._set_precheck_task_status(item_id, "安装完成")
                refreshed += 1
        if refreshed:
            self._precheck_status.config(
                text=f"已检测到 {refreshed} 个后台下载包体安装完成",
                foreground="#2e7d32",
            )
        return refreshed

    @staticmethod
    def _precheck_task_status_for_result(result: dict) -> str:
        backend_blacklist = result.get("backend_blacklist") or {}
        if result.get("code") == "ALL_NETWORK_NO_PACKAGE" and backend_blacklist:
            return (
                "全网无包(后台)"
                if backend_blacklist.get("ok")
                else "全网无包提交失败"
            )
        if result.get("code") in {"IAP_ONLY", "JAPANESE_PACKAGE"} and backend_blacklist:
            return "已加黑(后台)" if backend_blacklist.get("ok") else "加黑提交失败"
        review_monetization = result.get("code") == "NO_ADS_OR_IAP"
        launch_result = result.get("launch_result") or {}
        if launch_result:
            launch_status = {
                "LAUNCH_OK": "启动正常",
                "APP_CRASHED": "包体闪退",
                "APP_EXITED": "启动异常",
                "LAUNCH_FAILED": "启动失败",
            }.get(launch_result.get("code"), "启动异常")
            if review_monetization and launch_result.get("ok"):
                return "待人工检查"
            return launch_status
        install_result = result.get("install_result") or {}
        if install_result:
            install_code = install_result.get("code")
            if install_code in {
                "DOWNLOAD_LIMIT_REACHED",
                "DOWNLOAD_DEFERRED",
            }:
                return "待补下载"
            if install_code == "DOWNLOAD_PAUSED":
                return "下载已暂停"
            if install_code == "DOWNLOAD_STARTED":
                return "后台下载中"
            if install_result.get("ok"):
                if review_monetization:
                    return "待人工检查"
                if install_code == "ALREADY_INSTALLED":
                    return "已安装"
                return "安装完成"
            return "安装失败"
        return {
            "HAS_ADS": "有广告",
            "GOOGLE_NO_PACKAGE": "google无包",
            "ALL_NETWORK_NO_PACKAGE": "全网无包",
            "APKCOMBO_AVAILABLE": "APKCombo有包",
            "APKCOMBO_CHECK_FAILED": "APKCombo待确认",
            "IAP_ONLY": "已加黑",
            "JAPANESE_PACKAGE": "已加黑",
            "NO_ADS_OR_IAP": "待人工检查",
            "DEVICE_UNSUPPORTED": "设备不支持",
            "COUNTRY_UNSUPPORTED": "地区不支持",
            "UNKNOWN": "待人工",
            "NO_DEVICE": "未执行",
        }.get(result.get("code"), "失败")

    def _submit_precheck_blacklist(self, result: dict) -> dict:
        if result.get("code") not in {
            "IAP_ONLY",
            "JAPANESE_PACKAGE",
            "ALL_NETWORK_NO_PACKAGE",
        }:
            return result
        backend_result = submit_precheck_blacklist_via_api(
            result,
            api_url=self.cp_adapt_api_url_var.get().strip(),
            x_token=self.cp_adapt_x_token_var.get().strip(),
            token=self.cp_adapt_token_var.get().strip(),
            user_name=self.cp_adapt_assign_var.get().strip() or "rain",
        )
        return {**result, "backend_blacklist": backend_result}

    def _install_after_precheck(
        self,
        result: dict,
        return_after_start: bool = False,
    ) -> dict:
        """Install one app after a successful ads precheck in a worker thread."""
        if (
            result.get("continue_adaptation") is not True
            and result.get("code") not in {
                "HAS_ADS",
                "NO_ADS_OR_IAP",
                "APKCOMBO_AVAILABLE",
            }
        ):
            return result

        def _progress(message: str):
            self._safe_after(
                0,
                lambda message=message: self._precheck_status.config(
                    text=message, foreground="#1976d2"
                ),
            )

        if result.get("code") == "APKCOMBO_AVAILABLE":
            install_result = download_and_install_apkcombo(
                result.get("package_name", ""),
                on_progress=_progress,
            )
        else:
            install_result = install_google_play_app(
                result.get("package_name", ""),
                on_progress=_progress,
                return_after_start=return_after_start,
            )
        return {**result, "install_result": install_result}

    def _launch_check_after_install(
        self,
        result: dict,
        observation_seconds: int,
    ) -> dict:
        install_result = result.get("install_result") or {}
        if not install_result.get("ok"):
            return result

        def _progress(message: str):
            self._safe_after(
                0,
                lambda message=message: self._precheck_status.config(
                    text=message, foreground="#7b1fa2"
                ),
            )

        launch_result = run_app_launch_precheck(
            result.get("package_name", ""),
            observation_seconds=observation_seconds,
            on_progress=_progress,
        )
        return {**result, "launch_result": launch_result}

    @staticmethod
    def _comment_result_for_precheck(result: dict) -> dict:
        launch_result = result.get("launch_result") or {}
        if launch_result and not launch_result.get("ok"):
            summary = str(launch_result.get("summary") or "").strip()
            detail = str(launch_result.get("message") or "应用启动预检失败")
            if summary:
                detail += "\n崩溃摘要：\n" + summary[-1800:]
            return {
                "code": launch_result.get("code", "LAUNCH_FAILED"),
                "package_name": result.get("package_name", ""),
                "detail": detail,
            }
        install_result = result.get("install_result") or {}
        if (
            install_result
            and install_result.get("code") not in {
                "DOWNLOAD_LIMIT_REACHED",
                "DOWNLOAD_DEFERRED",
                "DOWNLOAD_PAUSED",
                "DOWNLOAD_STARTED",
            }
            and not install_result.get("ok")
        ):
            return {
                "code": "INSTALL_FAILED",
                "package_name": result.get("package_name", ""),
                "detail": install_result.get("message", "自动下载安装失败"),
            }
        return result

    def _select_precheck_item(self, item_id: str):
        if not self.precheck_task_tree.exists(item_id):
            return
        self.precheck_task_tree.selection_set(item_id)
        self.precheck_task_tree.focus(item_id)
        self.precheck_task_tree.see(item_id)
        self._on_precheck_task_selected()

    def _render_today_asana_tasks(self, result: dict):
        self._precheck_manual_selection = False
        previous_statuses = {}
        for item_id in self.precheck_task_tree.get_children():
            task = self._precheck_tasks.get(item_id)
            values = self.precheck_task_tree.item(item_id, "values")
            gid = str(getattr(task, "gid", "") or "")
            if gid and len(values) >= 4:
                previous_statuses[gid] = str(values[3] or "")
        for item_id in self.precheck_task_tree.get_children():
            self.precheck_task_tree.delete(item_id)
        self._precheck_tasks.clear()

        tasks = result.get("tasks", [])
        known_gids = self._known_precheck_gids()
        current_gids = {
            str(getattr(task, "gid", "") or "") for task in tasks
            if getattr(task, "gid", "")
        }
        actionable_restored_statuses = {"APKCombo有包"}
        eligible_gids = {
            str(getattr(task, "gid", "") or "")
            for task in tasks
            if getattr(task, "gid", "")
            and not getattr(task, "completed", False)
            and not getattr(task, "workflow_terminal", False)
            and (
                not getattr(task, "workflow_status", "")
                or getattr(task, "workflow_status", "")
                in actionable_restored_statuses
            )
        }
        actionable_restored_gids = {
            str(getattr(task, "gid", "") or "")
            for task in tasks
            if getattr(task, "gid", "")
            and getattr(task, "workflow_status", "")
            in actionable_restored_statuses
            and not getattr(task, "completed", False)
            and not getattr(task, "workflow_terminal", False)
        }
        pending_gids = self._pending_precheck_gids() & eligible_gids
        self._precheck_new_task_gids = (
            (eligible_gids - known_gids)
            | pending_gids
            | actionable_restored_gids
        )
        # On the first read of a workday there is no baseline, so all current
        # eligible tasks are the initial batch. Subsequent reads contain only
        # genuinely new Task GIDs in this set.
        self._remember_precheck_gids(known_gids | current_gids)
        self._remember_pending_precheck_gids(self._precheck_new_task_gids)
        first_incomplete = None
        for index, task in enumerate(tasks, start=1):
            if task.completed:
                status = "已完成"
            elif (
                getattr(task, "workflow_status", "")
                and getattr(task, "workflow_status", "")
                not in actionable_restored_statuses
            ):
                status = str(task.workflow_status)
            else:
                gid = str(getattr(task, "gid", "") or "")
                status = (
                    str(getattr(task, "workflow_status", "") or "")
                    or previous_statuses.get(gid)
                    or (
                    "新增待预检" if gid in self._precheck_new_task_gids else "待处理"
                    )
                )
            item_id = self.precheck_task_tree.insert(
                "",
                tk.END,
                values=(
                    index,
                    task.package_name or task.name,
                    task.gp_link or "未填写",
                    status,
                ),
            )
            self._precheck_tasks[item_id] = task
            if (
                first_incomplete is None
                and not task.completed
                and not getattr(task, "workflow_terminal", False)
                and str(getattr(task, "gid", "") or "") in self._precheck_new_task_gids
            ):
                first_incomplete = item_id

        # A package can finish after the previous batch's finite monitoring
        # window. Re-reading today's tasks must reconcile those stale local
        # download states against the device instead of preserving them
        # forever as "后台下载中".
        self._refresh_completed_background_downloads()

        section_name = result.get("section_name", "")
        if tasks:
            comment_errors = result.get("comment_status_errors") or []
            restored = sum(
                bool(getattr(task, "workflow_status", "")) for task in tasks
            )
            status_text = (
                f"{section_name} · {len(tasks)} 个任务（建立日期递减），"
                f"本次新增 {len(self._precheck_new_task_gids)} 个，"
                f"评论恢复 {restored} 个状态"
            )
            if comment_errors:
                status_text += f"，{len(comment_errors)} 个评论读取失败"
            self._precheck_asana_status.config(
                text=status_text,
                foreground="#ef6c00" if comment_errors else "#2e7d32",
            )
            selected = first_incomplete or self.precheck_task_tree.get_children()[0]
            self.precheck_task_tree.selection_set(selected)
            self.precheck_task_tree.focus(selected)
            self.precheck_task_tree.see(selected)
            self._on_precheck_task_selected()
        else:
            self._precheck_asana_status.config(
                text=f"{section_name} 没有任务", foreground="#ef5350"
            )

    def _on_load_today_asana_tasks(self):
        if self._precheck_running:
            return
        project_gid = self.project_gid_var.get().strip()
        asana_pat = self.asana_pat_var.get().strip()
        if not project_gid or not asana_pat:
            self._precheck_asana_status.config(
                text="请先在数据同步页填写项目 GID 和 Asana PAT", foreground="#ef5350"
            )
            return

        self._precheck_running = True
        self._precheck_load_asana_btn.configure(state=tk.DISABLED)
        self._precheck_asana_status.config(text="正在读取今日任务...", foreground="#ffa726")

        def _run():
            try:
                client = build_asana_client(asana_pat)
                result = get_asana_tasks_for_date(
                    client,
                    project_gid,
                    today=datetime.now().date(),
                )
                self._safe_after(0, self._render_today_asana_tasks, result)
            except Exception as exc:
                self._safe_after(
                    0,
                    lambda exc=exc: self._precheck_asana_status.config(
                        text=f"读取失败: {exc}", foreground="#ef5350"
                    ),
                )
            finally:
                self._safe_after(
                    0, lambda: self._precheck_load_asana_btn.configure(state=tk.NORMAL)
                )
                self._safe_after(0, lambda: setattr(self, "_precheck_running", False))

        threading.Thread(target=_run, daemon=True).start()

    def _on_stop_batch_precheck(self):
        if self._precheck_running:
            self._precheck_cancel_requested = True
            self._precheck_status.config(
                text="将在当前任务结束后停止...", foreground="#ef6c00"
            )

    def _on_start_precheck_then_automation(self):
        """Precheck pending rows, then adapt every installed eligible row."""
        if self._precheck_running:
            return
        if self._automation_running:
            self._precheck_status.config(
                text="自动化适配队列正在运行", foreground="#ef6c00"
            )
            return

        # Refresh late Google Play installations before deciding whether the
        # visible list contains an automation-eligible installed package.
        self._refresh_completed_background_downloads()

        queue, _ = self._precheck_batch_queue_from_selection()
        if not queue:
            # This also makes the combined entry useful after the operator has
            # already completed precheck and installation in an earlier run.
            if self._automation_eligible_precheck_tasks():
                self._precheck_status.config(
                    text="没有新的待预检任务，直接启动已安装合格任务队列",
                    foreground="#1976d2",
                )
                self._automation_run_eligible_batch()
            else:
                self._precheck_status.config(
                    text="没有待预检任务，也没有可自动适配的已安装任务",
                    foreground="#ef5350",
                )
            return
        self._on_start_batch_precheck(start_automation_after=True)

    def _start_automation_after_precheck(self):
        eligible_count = len(self._automation_eligible_precheck_tasks())
        if not eligible_count:
            self._precheck_status.config(
                text="批量预检完成，但没有已安装且合格的自动适配任务",
                foreground="#ef6c00",
            )
            return
        self._precheck_status.config(
            text=f"批量预检完成，开始自动适配 {eligible_count} 个合格任务",
            foreground="#1976d2",
        )
        self._automation_run_eligible_batch()

    def _on_start_batch_precheck(self, *, start_automation_after: bool = False):
        if self._precheck_running:
            return
        self._refresh_completed_background_downloads()
        project_gid = self.project_gid_var.get().strip()
        asana_pat = self.asana_pat_var.get().strip()
        if not project_gid or not asana_pat:
            self._precheck_status.config(
                text="请先在数据同步页填写 Asana 项目 GID 和 PAT",
                foreground="#ef5350",
            )
            return

        auto_install = self.precheck_auto_install_var.get()
        launch_check = self.precheck_launch_check_var.get()
        try:
            download_limit = int(self.precheck_download_limit_var.get().strip())
            download_cooldown = int(
                self.precheck_download_cooldown_var.get().strip()
            )
            launch_observation = int(
                self.precheck_launch_observation_var.get().strip()
            )
        except ValueError:
            self._precheck_status.config(
                text="下载上限和安装间隔必须是整数", foreground="#ef5350"
            )
            return
        if download_limit < 1 or download_limit > 10 or download_cooldown < 0:
            self._precheck_status.config(
                text="单批下载上限需为 1-10，安装间隔不能小于 0",
                foreground="#ef5350",
            )
            return
        if launch_observation < 5 or launch_observation > 120:
            self._precheck_status.config(
                text="启动观察时间需为 5-120 秒", foreground="#ef5350"
            )
            return

        queue, batch_start_position = self._precheck_batch_queue_from_selection()
        if not queue:
            self._precheck_status.config(
                text="选中项及其后没有待处理任务", foreground="#ef5350"
            )
            return

        self._precheck_running = True
        self._precheck_cancel_requested = False
        self._precheck_load_asana_btn.configure(state=tk.DISABLED)
        self._precheck_batch_btn.configure(state=tk.DISABLED)
        if self._precheck_auto_adapt_btn is not None:
            self._precheck_auto_adapt_btn.configure(state=tk.DISABLED)
        self._precheck_start_btn.configure(state=tk.DISABLED)
        self._precheck_stop_btn.configure(state=tk.NORMAL)
        self._precheck_status.config(
            text=(
                f"从列表第 {batch_start_position} 项开始，"
                f"准备批量预检 {len(queue)} 个待处理任务..."
            ),
            foreground="#ffa726",
        )

        def _run():
            processed = 0
            new_downloads = 0
            downloaded_total = 0
            deferred_downloads = []
            pending_downloads = []
            remaining_background_downloads = 0
            backend_blacklist_failures = 0
            batch_completed = False
            try:
                client = build_asana_client(asana_pat)
                total = len(queue)
                for position, (item_id, task) in enumerate(queue, start=1):
                    if self._precheck_cancel_requested:
                        break
                    value = getattr(task, "gp_link", "") or getattr(task, "package_name", "")
                    self._safe_after(0, self._select_precheck_item, item_id)
                    self._safe_after(0, self._set_precheck_task_status, item_id, "检查中")
                    self._safe_after(
                        0,
                        lambda position=position, total=total: self._precheck_status.config(
                            text=f"正在检查 {position}/{total}...", foreground="#ffa726"
                        ),
                    )

                    if getattr(task, "workflow_status", "") == "APKCombo有包":
                        result = {
                            "code": "APKCOMBO_AVAILABLE",
                            "title": "APKCombo 有包，重新尝试自动下载",
                            "detail": "历史安装失败任务已转入 APKCombo 新下载链路重试。",
                            "continue_adaptation": False,
                            "package_name": getattr(task, "package_name", ""),
                            "source": "历史预检状态迁移",
                            "evidence": ["此前安装失败，按新规则使用 APKCombo 重试"],
                        }
                    else:
                        result = run_google_play_precheck(
                            value, verify_apkcombo=True
                        )
                    result = self._submit_precheck_blacklist(result)
                    backend_blacklist = result.get("backend_blacklist") or {}
                    if backend_blacklist and not backend_blacklist.get("ok"):
                        backend_blacklist_failures += 1
                    if auto_install and (
                        result.get("continue_adaptation") is True
                        or result.get("code") in {
                            "HAS_ADS",
                            "NO_ADS_OR_IAP",
                            "APKCOMBO_AVAILABLE",
                        }
                    ):
                        if new_downloads >= download_limit:
                            deferred_downloads.append((item_id, task, result))
                            result = {
                                **result,
                                "install_result": {
                                    "ok": None,
                                    "code": "DOWNLOAD_DEFERRED",
                                    "message": (
                                        f"已达到本轮下载上限 {download_limit} 个；"
                                        "全部页面预检完成后自动补下载"
                                    ),
                                },
                            }
                        else:
                            # The eligibility check has passed, but Play has
                            # not necessarily accepted an Install tap yet.
                            self._safe_after(
                                0, self._set_precheck_task_status, item_id, "准备下载"
                            )
                            result = self._install_after_precheck(result, True)
                            install_result = result.get("install_result") or {}
                            if install_result.get("code") in {
                                "DOWNLOAD_STARTED",
                                "INSTALLED",
                                "APKCOMBO_INSTALLED",
                            }:
                                new_downloads += 1
                                downloaded_total += 1
                            if install_result.get("code") == "DOWNLOAD_STARTED":
                                pending_downloads.append((item_id, task, result))
                            if launch_check and install_result.get("ok"):
                                self._safe_after(
                                    0,
                                    self._set_precheck_task_status,
                                    item_id,
                                    "启动检查",
                                )
                                result = self._launch_check_after_install(
                                    result,
                                    launch_observation,
                                )

                    status = self._precheck_task_status_for_result(result)
                    comment_error = ""
                    if result.get("code") not in {"NO_DEVICE", "OPEN_FAILED"}:
                        try:
                            add_precheck_comment_once(
                                client,
                                task.gid,
                                self._comment_result_for_precheck(result),
                            )
                            backend_blacklist = result.get("backend_blacklist") or {}
                            if backend_blacklist and not backend_blacklist.get("ok"):
                                add_automation_comment_once(
                                    client,
                                    task.gid,
                                    backend_blacklist.get(
                                        "code", "PRECHECK_BLACKLIST_SUBMIT_FAILED"
                                    ),
                                    backend_blacklist.get(
                                        "message", "预检后台标记提交失败，需要人工处理"
                                    ),
                                )
                        except Exception as exc:
                            comment_error = str(exc)
                            status = "评论失败"

                    self._safe_after(0, self._set_precheck_task_status, item_id, status)
                    self._safe_after(0, self._render_precheck_result, result)
                    if comment_error:
                        self._safe_after(
                            0,
                            lambda comment_error=comment_error: self._precheck_status.config(
                                text=f"Asana 评论失败: {comment_error}", foreground="#ef5350"
                            ),
                        )
                    processed += 1

                    if result.get("code") == "NO_DEVICE":
                        break

                    install_result = result.get("install_result") or {}
                    if (
                        install_result.get("code") in {
                            "DOWNLOAD_STARTED",
                            "INSTALLED",
                            "APKCOMBO_INSTALLED",
                        }
                        and download_cooldown > 0
                        and position < total
                    ):
                        remaining = download_cooldown
                        while remaining > 0 and not self._precheck_cancel_requested:
                            self._safe_after(
                                0,
                                lambda remaining=remaining: self._precheck_status.config(
                                    text=f"安装冷却中，{remaining} 秒后继续预检...",
                                    foreground="#1976d2",
                                ),
                            )
                            sleep_for = min(5, remaining)
                            time.sleep(sleep_for)
                            remaining -= sleep_for

                # 第二阶段：页面全部检查完成后，回头处理因本轮上限而延后的下载。
                if (
                    deferred_downloads
                    and not self._precheck_cancel_requested
                ):
                    new_downloads = 0
                    deferred_total = len(deferred_downloads)
                    self._safe_after(
                        0,
                        lambda deferred_total=deferred_total: self._precheck_status.config(
                            text=f"页面预检已完成，开始补下载 {deferred_total} 个任务...",
                            foreground="#1976d2",
                        ),
                    )
                    for deferred_position, (item_id, task, base_result) in enumerate(
                        deferred_downloads,
                        start=1,
                    ):
                        if self._precheck_cancel_requested:
                            break
                        value = (
                            getattr(task, "gp_link", "")
                            or getattr(task, "package_name", "")
                        )
                        self._safe_after(0, self._select_precheck_item, item_id)
                        self._safe_after(
                            0, self._set_precheck_task_status, item_id, "补下载中"
                        )
                        self._safe_after(
                            0,
                            lambda deferred_position=deferred_position,
                            deferred_total=deferred_total: self._precheck_status.config(
                                text=(
                                    f"正在补下载 {deferred_position}/{deferred_total}..."
                                ),
                                foreground="#1976d2",
                            ),
                        )

                        if base_result.get("code") == "APKCOMBO_AVAILABLE":
                            result = self._install_after_precheck(base_result, True)
                        else:
                            opened, open_message, _ = open_google_play_page(value)
                            if opened:
                                result = self._install_after_precheck(base_result, True)
                            else:
                                result = {
                                    **base_result,
                                    "install_result": {
                                        "ok": False,
                                        "code": "OPEN_FAILED",
                                        "message": open_message,
                                    },
                                }

                        install_result = result.get("install_result") or {}
                        if install_result.get("code") in {
                            "DOWNLOAD_STARTED",
                            "INSTALLED",
                            "APKCOMBO_INSTALLED",
                        }:
                            new_downloads += 1
                            downloaded_total += 1
                        if install_result.get("code") == "DOWNLOAD_STARTED":
                            pending_downloads.append((item_id, task, result))

                        if launch_check and install_result.get("ok"):
                            self._safe_after(
                                0,
                                self._set_precheck_task_status,
                                item_id,
                                "启动检查",
                            )
                            result = self._launch_check_after_install(
                                result,
                                launch_observation,
                            )

                        status = self._precheck_task_status_for_result(result)
                        try:
                            add_precheck_comment_once(
                                client,
                                task.gid,
                                self._comment_result_for_precheck(result),
                            )
                        except Exception:
                            status = "评论失败"
                        self._safe_after(
                            0, self._set_precheck_task_status, item_id, status
                        )
                        self._safe_after(0, self._render_precheck_result, result)

                        if new_downloads >= download_limit:
                            new_downloads = 0
                        if (
                            install_result.get("code") in {
                                "DOWNLOAD_STARTED",
                                "INSTALLED",
                                "APKCOMBO_INSTALLED",
                            }
                            and download_cooldown > 0
                            and deferred_position < deferred_total
                        ):
                            remaining = download_cooldown
                            while (
                                remaining > 0
                                and not self._precheck_cancel_requested
                            ):
                                self._safe_after(
                                    0,
                                    lambda remaining=remaining: self._precheck_status.config(
                                        text=f"补下载冷却中，{remaining} 秒后继续...",
                                        foreground="#1976d2",
                                    ),
                                )
                                sleep_for = min(5, remaining)
                                time.sleep(sleep_for)
                                remaining -= sleep_for

                # 所有可下载任务都已发起后，统一监控安装状态。
                # 任何大包都不会阻塞其他包发起下载；谁先安装完就先检查谁。
                if launch_check and pending_downloads and not self._precheck_cancel_requested:
                    pending_remaining = list(pending_downloads)
                    monitor_deadline = time.monotonic() + 600
                    while (
                        pending_remaining
                        and time.monotonic() < monitor_deadline
                        and not self._precheck_cancel_requested
                    ):
                        self._safe_after(
                            0,
                            lambda count=len(pending_remaining): self._precheck_status.config(
                                text=f"已发起全部下载，正在后台等待 {count} 个包体安装...",
                                foreground="#1976d2",
                            ),
                        )
                        installed_this_round = []
                        for item_id, task, pending_result in pending_remaining:
                            package_name = pending_result.get("package_name", "")
                            if not package_name or not is_package_installed(package_name):
                                continue
                            result = {
                                **pending_result,
                                "install_result": {
                                    "ok": True,
                                    "code": "INSTALLED",
                                    "message": "Google Play 后台下载并安装完成",
                                },
                            }
                            self._safe_after(
                                0, self._set_precheck_task_status, item_id, "启动检查"
                            )
                            result = self._launch_check_after_install(
                                result,
                                launch_observation,
                            )
                            status = self._precheck_task_status_for_result(result)
                            try:
                                add_precheck_comment_once(
                                    client,
                                    task.gid,
                                    self._comment_result_for_precheck(result),
                                )
                            except Exception:
                                status = "评论失败"
                            self._safe_after(
                                0, self._set_precheck_task_status, item_id, status
                            )
                            self._safe_after(0, self._render_precheck_result, result)
                            installed_this_round.append(item_id)
                        if installed_this_round:
                            installed_ids = set(installed_this_round)
                            pending_remaining = [
                                entry
                                for entry in pending_remaining
                                if entry[0] not in installed_ids
                            ]
                        if pending_remaining and not self._precheck_cancel_requested:
                            time.sleep(3)
                    remaining_background_downloads = len(pending_remaining)

                cancelled = self._precheck_cancel_requested
                batch_completed = not cancelled and processed == len(queue)
                self._safe_after(
                    0,
                    lambda downloaded_total=downloaded_total,
                    remaining_background_downloads=remaining_background_downloads,
                    backend_blacklist_failures=backend_blacklist_failures: self._precheck_status.config(
                        text=(
                            f"已停止，完成 {processed}/{len(queue)}"
                            if cancelled
                            else (
                                f"批量预检完成，共处理 {processed} 个任务，"
                                f"新增下载 {downloaded_total} 个"
                                + (
                                    f"，仍在后台下载 {remaining_background_downloads} 个"
                                    if remaining_background_downloads
                                    else ""
                                )
                                + (
                                    f"，后台提交失败 {backend_blacklist_failures} 个"
                                    if backend_blacklist_failures
                                    else ""
                                )
                            )
                        ),
                        foreground=(
                            "#ef6c00"
                            if cancelled
                            else (
                                "#ef5350"
                                if backend_blacklist_failures
                                else "#2e7d32"
                            )
                        ),
                    ),
                )
            except Exception as exc:
                self._safe_after(
                    0,
                    lambda exc=exc: self._precheck_status.config(
                        text=f"批量预检失败: {exc}", foreground="#ef5350"
                    ),
                )
            finally:
                self._precheck_cancel_requested = False
                self._precheck_running = False
                self._safe_after(
                    0, lambda: self._precheck_load_asana_btn.configure(state=tk.NORMAL)
                )
                self._safe_after(
                    0, lambda: self._precheck_batch_btn.configure(state=tk.NORMAL)
                )
                if self._precheck_auto_adapt_btn is not None:
                    self._safe_after(
                        0,
                        lambda: self._precheck_auto_adapt_btn.configure(
                            state=(
                                tk.DISABLED
                                if self._automation_running
                                else tk.NORMAL
                            )
                        ),
                    )
                self._safe_after(
                    0, lambda: self._precheck_start_btn.configure(state=tk.NORMAL)
                )
                self._safe_after(
                    0, lambda: self._precheck_stop_btn.configure(state=tk.DISABLED)
                )
                if start_automation_after and batch_completed:
                    self._safe_after(0, self._start_automation_after_precheck)

        threading.Thread(target=_run, daemon=True).start()

    def _on_precheck_open_page(self):
        value = self.precheck_input.get().strip()
        if not value:
            self._precheck_status.config(text="请输入 GP 链接或包名", foreground="#ef5350")
            return
        self._precheck_status.config(text="正在打开手机页面...", foreground="#ffa726")

        def _run():
            ok, message, _package_name = open_google_play_page(value)
            self._safe_after(
                0,
                lambda: self._precheck_status.config(
                    text=message,
                    foreground="#2e7d32" if ok else "#ef5350",
                ),
            )

        threading.Thread(target=_run, daemon=True).start()

    def _render_precheck_result(self, result: dict):
        code = result.get("code", "UNKNOWN")
        colors = {
            "HAS_ADS": "#2e7d32",
            "GOOGLE_NO_PACKAGE": "#c62828",
            "ALL_NETWORK_NO_PACKAGE": "#c62828",
            "APKCOMBO_AVAILABLE": "#1976d2",
            "APKCOMBO_CHECK_FAILED": "#ef6c00",
            "IAP_ONLY": "#ef6c00",
            "JAPANESE_PACKAGE": "#ef6c00",
            "NO_ADS_OR_IAP": "#ef6c00",
            "DEVICE_UNSUPPORTED": "#c62828",
            "COUNTRY_UNSUPPORTED": "#c62828",
        }
        self._precheck_result_title.config(
            text=result.get("title", "预检完成"),
            foreground=colors.get(code, "#616161"),
        )
        decision = result.get("continue_adaptation")
        if code == "NO_ADS_OR_IAP":
            recommendation = "继续下载，人工检查是否包含广告；不自动加黑"
        elif decision is True:
            recommendation = "继续下载安装和适配"
        elif code in {"IAP_ONLY", "JAPANESE_PACKAGE"}:
            recommendation = "加黑并跳过，不下载、不适配"
        elif code == "ALL_NETWORK_NO_PACKAGE":
            recommendation = "Google Play 无法下载且 APKCombo 无可用包，暂不适配"
        elif code == "APKCOMBO_AVAILABLE":
            recommendation = "通过 APKCombo 下载第三方包体，再继续检查和适配"
        elif code == "APKCOMBO_CHECK_FAILED":
            recommendation = "APKCombo 自动核验无明确结果，需要人工确认"
        elif code == "GOOGLE_NO_PACKAGE":
            recommendation = "Google Play 无包，等待 APKCombo 核验"
        elif decision is False:
            recommendation = "跳过当前任务"
        else:
            recommendation = "等待页面加载后重试，暂不自动加黑"

        lines = [
            f"包名：{result.get('package_name') or '未识别'}",
            f"结论：{result.get('title', '')}",
            f"建议：{recommendation}",
            f"识别方式：{result.get('source') or '未取得页面信息'}",
            "",
            result.get("detail", ""),
        ]
        evidence = result.get("evidence", [])
        if evidence:
            lines.extend(["", "判断依据："])
            lines.extend(f"- {item}" for item in evidence)
        install_result = result.get("install_result") or {}
        if install_result:
            lines.extend([
                "",
                "自动下载：",
                f"- 状态：{install_result.get('code', '')}",
                f"- 结果：{install_result.get('message', '')}",
            ])
        launch_result = result.get("launch_result") or {}
        if launch_result:
            lines.extend([
                "",
                "启动预检：",
                f"- 状态：{launch_result.get('code', '')}",
                f"- 结果：{launch_result.get('message', '')}",
            ])
            if launch_result.get("summary"):
                lines.extend(["- 崩溃摘要：", launch_result["summary"]])
        backend_blacklist = result.get("backend_blacklist") or {}
        if backend_blacklist:
            lines.extend([
                "",
                "后台提交：",
                f"- 状态：{backend_blacklist.get('code', '')}",
                f"- 结果：{backend_blacklist.get('message', '')}",
            ])
        visible_texts = result.get("visible_texts", [])
        if visible_texts:
            lines.extend(["", "页面识别文字："])
            lines.extend(f"- {item}" for item in visible_texts)

        self.precheck_output.configure(state=tk.NORMAL)
        self.precheck_output.delete("1.0", tk.END)
        self.precheck_output.insert("1.0", "\n".join(lines))
        self.precheck_output.configure(state=tk.DISABLED)
        if backend_blacklist and not backend_blacklist.get("ok"):
            self._precheck_status.config(text="预检完成，但后台提交失败", foreground="#ef5350")
        elif backend_blacklist.get("ok"):
            self._precheck_status.config(text="预检完成，后台提交及缓存刷新成功", foreground="#2e7d32")
        else:
            self._precheck_status.config(text="预检完成", foreground=colors.get(code, "#616161"))

    def _on_start_precheck(self):
        if self._precheck_running:
            return
        value = self.precheck_input.get().strip()
        if not value:
            self._precheck_status.config(text="请输入 GP 链接或包名", foreground="#ef5350")
            return
        selected_item_id, selected_task = self._selected_precheck_task()
        asana_pat = self.asana_pat_var.get().strip()
        auto_install = self.precheck_auto_install_var.get()
        launch_check = self.precheck_launch_check_var.get()
        try:
            launch_observation = int(
                self.precheck_launch_observation_var.get().strip()
            )
        except ValueError:
            self._precheck_status.config(
                text="启动观察时间必须是整数", foreground="#ef5350"
            )
            return
        if launch_observation < 5 or launch_observation > 120:
            self._precheck_status.config(
                text="启动观察时间需为 5-120 秒", foreground="#ef5350"
            )
            return
        self._precheck_running = True
        self._precheck_start_btn.configure(state=tk.DISABLED)
        self._precheck_status.config(text="正在打开并识别手机页面...", foreground="#ffa726")

        def _run():
            try:
                if (
                    selected_task is not None
                    and getattr(selected_task, "workflow_status", "")
                    == "APKCombo有包"
                ):
                    result = {
                        "code": "APKCOMBO_AVAILABLE",
                        "title": "APKCombo 有包，重新尝试自动下载",
                        "detail": "历史安装失败任务已转入 APKCombo 新下载链路重试。",
                        "continue_adaptation": False,
                        "package_name": selected_task.package_name,
                        "source": "历史预检状态迁移",
                        "evidence": ["此前安装失败，按新规则使用 APKCombo 重试"],
                    }
                else:
                    result = run_google_play_precheck(
                        value, verify_apkcombo=True
                    )
                result = self._submit_precheck_blacklist(result)
                if auto_install and (
                    result.get("continue_adaptation") is True
                    or result.get("code") in {
                        "HAS_ADS",
                        "NO_ADS_OR_IAP",
                        "APKCOMBO_AVAILABLE",
                    }
                ):
                    if selected_item_id:
                        self._safe_after(
                            0, self._set_precheck_task_status, selected_item_id, "准备下载"
                        )
                    result = self._install_after_precheck(result)
                    if launch_check and (result.get("install_result") or {}).get("ok"):
                        if selected_item_id:
                            self._safe_after(
                                0,
                                self._set_precheck_task_status,
                                selected_item_id,
                                "启动检查",
                            )
                        result = self._launch_check_after_install(
                            result,
                            launch_observation,
                        )
                comment_error = ""
                if (
                    selected_task is not None
                    and selected_task.gid
                    and selected_task.package_name == result.get("package_name")
                    and result.get("code") not in {"NO_DEVICE", "OPEN_FAILED"}
                    and asana_pat
                ):
                    try:
                        client = build_asana_client(asana_pat)
                        add_precheck_comment_once(
                            client,
                            selected_task.gid,
                            self._comment_result_for_precheck(result),
                        )
                        backend_blacklist = result.get("backend_blacklist") or {}
                        if backend_blacklist and not backend_blacklist.get("ok"):
                            add_automation_comment_once(
                                client,
                                selected_task.gid,
                                backend_blacklist.get(
                                    "code", "PRECHECK_BLACKLIST_SUBMIT_FAILED"
                                ),
                                backend_blacklist.get(
                                    "message", "预检后台标记提交失败，需要人工处理"
                                ),
                            )
                        self._safe_after(
                            0,
                            self._set_precheck_task_status,
                            selected_item_id,
                            self._precheck_task_status_for_result(result),
                        )
                    except Exception as exc:
                        comment_error = str(exc)
                        self._safe_after(
                            0, self._set_precheck_task_status, selected_item_id, "评论失败"
                        )
                self._safe_after(0, self._render_precheck_result, result)
                if comment_error:
                    self._safe_after(
                        0,
                        lambda comment_error=comment_error: self._precheck_status.config(
                            text=f"预检完成，但 Asana 评论失败: {comment_error}",
                            foreground="#ef5350",
                        ),
                    )
            except Exception as exc:
                self._safe_after(
                    0,
                    lambda exc=exc: self._precheck_status.config(
                        text=f"预检失败: {exc}", foreground="#ef5350"
                    ),
                )
            finally:
                self._safe_after(
                    0, lambda: self._precheck_start_btn.configure(state=tk.NORMAL)
                )
                self._safe_after(0, lambda: setattr(self, "_precheck_running", False))

        threading.Thread(target=_run, daemon=True).start()

    # ── 自动化脚本 Tab ────────────────────────────────────────────

    def _build_action_script_tab(self, parent: ttk.Frame):
        pad = {"padx": 10, "pady": 8}

        info_frame = ttk.LabelFrame(parent, text="Delay 规则", padding=10)
        info_frame.pack(fill=tk.X, **pad)
        ttk.Label(
            info_frame,
            text="粘贴自动化脚本 JSON 后一键调整：首个 delay 固定至少 15000ms，其余 delay 使用下方配置。",
            foreground="gray",
            justify=tk.LEFT,
            wraplength=760,
        ).pack(anchor=tk.W)
        delay_row = ttk.Frame(info_frame)
        delay_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(delay_row, text="其余动作最小 delay:").pack(side=tk.LEFT)
        self.following_action_delay_entry = ttk.Entry(
            delay_row,
            textvariable=self.following_action_delay_var,
            width=8,
        )
        self.following_action_delay_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Label(delay_row, text="秒").pack(side=tk.LEFT)

        script_frame = ttk.LabelFrame(parent, text="自动化脚本", padding=5)
        script_frame.pack(fill=tk.BOTH, expand=True, **pad)

        toolbar = ttk.Frame(script_frame)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(
            toolbar,
            text="调整 delay",
            command=self._on_normalize_action_delays,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            toolbar,
            text="复制结果",
            command=self._on_copy_action_script,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            toolbar,
            text="清空",
            command=self._on_clear_action_script,
        ).pack(side=tk.RIGHT)
        self.action_script_status = ttk.Label(toolbar, text="待粘贴", foreground="gray")
        self.action_script_status.pack(side=tk.LEFT, padx=8)

        self.action_script_text = tk.Text(
            script_frame,
            wrap=tk.NONE,
            font=("Menlo", 11),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="white",
            height=24,
        )
        y_scrollbar = ttk.Scrollbar(script_frame, command=self.action_script_text.yview)
        x_scrollbar = ttk.Scrollbar(
            script_frame,
            orient=tk.HORIZONTAL,
            command=self.action_script_text.xview,
        )
        self.action_script_text.configure(
            yscrollcommand=y_scrollbar.set,
            xscrollcommand=x_scrollbar.set,
        )
        self.action_script_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        x_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)

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
        self.config_entry = ttk.Entry(row3, textvariable=self.config_path_var)
        self.config_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row3, text="选择", command=self._on_browse_config).pack(side=tk.LEFT)

        # 行4: 工作目录
        row4 = ttk.Frame(config_frame)
        row4.pack(fill=tk.X, pady=2)
        ttk.Label(row4, text="工作目录:").pack(side=tk.LEFT)
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
            ("修复 zygotehole 权限", self._on_fix_zygotehole_permissions),
            ("取消当前游戏注入", self._on_cancel_zygotehole_injection),
        ]:
            b = ttk.Button(btn_row2, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self._op_buttons.append(b)

        stop_row = ttk.Frame(action_frame)
        stop_row.pack(fill=tk.X, pady=(6, 2))
        self._stop_command_btn = ttk.Button(
            stop_row, text="停止当前命令", command=self._on_stop_current_command
        )
        self._stop_command_btn.pack(side=tk.LEFT, padx=2)
        self._stop_command_btn.configure(state=tk.DISABLED)

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

        outer = ttk.Frame(parent)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        sync_scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sync_scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sync_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        content = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_content_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(window_id, width=event.width)

        def _on_mousewheel(event):
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")

        content.bind("<Configure>", _on_content_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        def _bind_scroll_events(_event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_mousewheel)
            canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind_scroll_events(_event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_scroll_events)
        canvas.bind("<Leave>", _unbind_scroll_events)

        # --- 凭证配置 ---
        cfg_frame = ttk.LabelFrame(content, text="API 凭证与目标", padding=10)
        cfg_frame.pack(fill=tk.X, **pad)

        rows = [
            (
                "Sheet ID",
                self.sheet_id_var,
                None,
                "Google 表格 URL 中 /spreadsheets/d/ 后面的长字符串。",
            ),
            (
                "Sheet 名称",
                self.sheet_name_var,
                None,
                "表格底部的工作表标签名，例如 26年5-6月，需完全一致。",
            ),
            (
                "Asana 项目 GID",
                self.project_gid_var,
                None,
                "Asana 项目 URL 里的项目数字 ID，决定任务同步到哪个项目。",
            ),
            (
                "Asana PAT",
                self.asana_pat_var,
                None,
                "Asana Personal Access Token，用于调用 Asana API，请当作密码保管。",
            ),
            (
                "SA 密钥文件",
                self.sa_file_var,
                self._on_browse_sa_file,
                "Google Service Account 的 JSON 密钥文件，需有该 Sheet 的读写权限。",
            ),
            (
                "代理地址",
                self.proxy_url_var,
                None,
                "访问 Google Sheets API 的本机 HTTP 代理；不需要代理可留空。",
            ),
        ]
        for label, var, browse_cmd, help_text in rows:
            row_frame = ttk.Frame(cfg_frame)
            row_frame.pack(fill=tk.X, pady=4)
            ttk.Label(row_frame, text=label + ":", width=15, anchor=tk.E).pack(
                side=tk.LEFT, anchor=tk.N
            )

            field_frame = ttk.Frame(row_frame)
            field_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

            entry_row = ttk.Frame(field_frame)
            entry_row.pack(fill=tk.X)
            entry = ttk.Entry(entry_row, textvariable=var)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            if browse_cmd:
                ttk.Button(entry_row, text="选择", command=browse_cmd).pack(
                    side=tk.LEFT, padx=(8, 0)
                )

            ttk.Label(
                field_frame,
                text=help_text,
                foreground="gray",
                wraplength=640,
                justify=tk.LEFT,
            ).pack(fill=tk.X, pady=(2, 0))

        parent_url_row = ttk.Frame(cfg_frame)
        parent_url_row.pack(fill=tk.X, pady=4)
        ttk.Label(
            parent_url_row, text="父任务地址:", width=15, anchor=tk.E
        ).pack(side=tk.LEFT, anchor=tk.N)
        parent_url_field = ttk.Frame(parent_url_row)
        parent_url_field.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        parent_url_entry_row = ttk.Frame(parent_url_field)
        parent_url_entry_row.pack(fill=tk.X)
        self.parent_task_url_entry = ttk.Entry(
            parent_url_entry_row, textvariable=self.parent_task_url_var
        )
        self.parent_task_url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.parent_task_fill_btn = ttk.Button(
            parent_url_entry_row,
            text="解析填充",
            command=self._on_fill_parent_task_gid_from_url,
        )
        self.parent_task_fill_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            parent_url_field,
            text="粘贴 Asana 任务地址，自动识别任务 GID；不会误取末尾的 story 评论 GID。",
            foreground="gray",
            wraplength=640,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(2, 0))

        parent_gid_row = ttk.Frame(cfg_frame)
        parent_gid_row.pack(fill=tk.X, pady=4)
        ttk.Label(
            parent_gid_row, text="父任务 GID:", width=15, anchor=tk.E
        ).pack(side=tk.LEFT, anchor=tk.N)
        parent_gid_field = ttk.Frame(parent_gid_row)
        parent_gid_field.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Entry(parent_gid_field, textvariable=self.parent_task_gid_var).pack(fill=tk.X)
        self.parent_task_url_status_label = ttk.Label(
            parent_gid_field,
            text="Asana 父任务数字 ID，新建任务会挂到它下面。",
            foreground="gray",
            wraplength=640,
            justify=tk.LEFT,
        )
        self.parent_task_url_status_label.pack(fill=tk.X, pady=(2, 0))

        # --- CP 后台数据源 ---
        source_frame = ttk.LabelFrame(content, text="前置数据源（CP 后台 → Google Sheet）", padding=10)
        source_frame.pack(fill=tk.X, **pad)

        source_rows = [
            (
                "接口地址",
                self.cp_adapt_api_url_var,
                "CP 后台列表接口，默认读取待适配 CP 信息表。",
            ),
            (
                "X-Token",
                self.cp_adapt_x_token_var,
                "从浏览器开发者工具或 HAR 请求头复制；登录过期后需要更新。",
            ),
            (
                "固定 token",
                self.cp_adapt_token_var,
                "后台接口固定 token 请求头，通常无需修改。",
            ),
            (
                "适配人员",
                self.cp_adapt_assign_var,
                "筛选目标适配人，默认 rain。",
            ),
        ]
        for label, var, help_text in source_rows:
            row_frame = ttk.Frame(source_frame)
            row_frame.pack(fill=tk.X, pady=4)
            ttk.Label(row_frame, text=label + ":", width=15, anchor=tk.E).pack(
                side=tk.LEFT, anchor=tk.N
            )
            field_frame = ttk.Frame(row_frame)
            field_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
            ttk.Entry(field_frame, textvariable=var).pack(fill=tk.X)
            ttk.Label(
                field_frame,
                text=help_text,
                foreground="gray",
                wraplength=640,
                justify=tk.LEFT,
            ).pack(fill=tk.X, pady=(2, 0))

        # --- 操作按钮 ---
        action_frame = ttk.LabelFrame(content, text="操作", padding=10)
        action_frame.pack(fill=tk.X, **pad)

        btn_row = ttk.Frame(action_frame)
        btn_row.pack()
        self._prefill_sync_btn = ttk.Button(
            btn_row, text="⬇ 写入 Sheet 并同步", command=self._on_start_prefill_and_sync
        )
        self._prefill_sync_btn.pack(side=tk.LEFT, padx=3)
        self._sync_btn = ttk.Button(
            btn_row, text="🔄 仅同步 Asana", command=self._on_start_sync
        )
        self._sync_btn.pack(side=tk.LEFT, padx=3)
        self._cp_candidate_btn = None
        if self._cp_candidate_enabled:
            self._cp_candidate_btn = ttk.Button(
                btn_row,
                text="🎯 筛选高概率 CP",
                command=self._on_preview_cp_candidates,
            )
            self._cp_candidate_btn.pack(side=tk.LEFT, padx=3)
        self._sync_status = ttk.Label(btn_row, text="就绪", foreground="gray")
        self._sync_status.pack(side=tk.LEFT, padx=10)

        # --- 同步输出 ---
        output_frame = ttk.LabelFrame(content, text="同步输出", padding=5)
        output_frame.pack(fill=tk.BOTH, expand=True, **pad)

        toolbar = ttk.Frame(output_frame)
        toolbar.pack(fill=tk.X, pady=(0, 2))
        ttk.Button(toolbar, text="清空", command=self._on_clear_sync_output).pack(side=tk.RIGHT)

        self.sync_output = tk.Text(
            output_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Menlo", 11), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", height=8,
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
            self._save_sync_settings()

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

    def _on_preview_cp_candidates(self):
        """Load unassigned CP records and open a user-confirmed score preview."""
        if not self._cp_candidate_enabled:
            self._sync_status.config(text="当前设备未启用此功能", foreground="#ef5350")
            return
        if self._cp_candidate_running:
            self._sync_status.config(text="候选数据正在读取", foreground="#ffa726")
            return
        api_url = self.cp_adapt_api_url_var.get().strip()
        x_token = self.cp_adapt_x_token_var.get().strip()
        token = self.cp_adapt_token_var.get().strip()
        if not api_url or not x_token or not token:
            self._sync_log("请先填写 CP 后台接口、X-Token 和固定 token", "error")
            self._sync_status.config(text="后台凭证不完整", foreground="#ef5350")
            return
        self._save_sync_settings()
        self._cp_candidate_running = True
        if self._cp_candidate_btn is not None:
            self._cp_candidate_btn.configure(state=tk.DISABLED)
        self._sync_status.config(text="正在筛选高概率 CP...", foreground="#ffa726")

        def _run():
            try:
                result = load_cp_assignment_candidates(
                    api_url=api_url,
                    x_token=x_token,
                    token=token,
                )
                self._safe_after(
                    0,
                    lambda result=result: self._show_cp_candidate_dialog(
                        result,
                        api_url=api_url,
                        x_token=x_token,
                        token=token,
                    ),
                )
            except Exception as exc:
                self._safe_after(
                    0,
                    lambda exc=exc: self._sync_log(
                        f"读取高概率 CP 失败: {exc}", "error"
                    ),
                )
                self._safe_after(
                    0,
                    lambda: self._sync_status.config(
                        text="候选读取失败", foreground="#ef5350"
                    ),
                )
            finally:
                self._safe_after(0, self._finish_cp_candidate_operation)

        threading.Thread(target=_run, daemon=True).start()

    def _finish_cp_candidate_operation(self):
        self._cp_candidate_running = False
        if (
            self._cp_candidate_btn is not None
            and self._cp_candidate_btn.winfo_exists()
        ):
            self._cp_candidate_btn.configure(state=tk.NORMAL)

    def _show_cp_candidate_dialog(
        self,
        result: dict,
        *,
        api_url: str,
        x_token: str,
        token: str,
    ):
        candidates = list(result.get("candidates") or [])
        self._cp_candidate_preview = candidates
        self._sync_status.config(
            text=(
                f"未分配 {result.get('unassigned_count', 0)} 条，"
                f"推荐 {result.get('recommended_count', 0)} 条"
            ),
            foreground="#81c784",
        )
        self._sync_log(
            f"高概率筛选：后台可见 {result.get('visible_count', 0)} 条，"
            f"未分配 {result.get('unassigned_count', 0)} 条，"
            f"默认勾选推荐项 {result.get('recommended_count', 0)} 条",
            "done",
        )

        dialog = tk.Toplevel(self.root)
        dialog.title("高概率 CP 预览与分配")
        dialog.geometry("1080x560")
        dialog.transient(self.root)

        ttk.Label(
            dialog,
            text=(
                "评分只用于调整队列优先级，不代表最终一定适配成功。"
                "默认勾选历史成功率较高的游戏/益智类包体；提交前可自由增删。"
            ),
            foreground="gray",
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=12, pady=(12, 8))

        table_frame = ttk.Frame(dialog)
        table_frame.pack(fill=tk.BOTH, expand=True, padx=12)
        columns = ("selected", "score", "package", "category", "reason")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "selected": "选择",
            "score": "预计成功率",
            "package": "包名",
            "category": "类型",
            "reason": "判断依据",
        }
        widths = {"selected": 55, "score": 90, "package": 300, "category": 100, "reason": 430}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor=tk.W)
        selected: set[str] = set()
        for index, item in enumerate(candidates):
            iid=str(index)
            if item.get("recommended"):
                selected.add(iid)
            tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    "☑" if iid in selected else "☐",
                    f"{item.get('score', 0)}%",
                    item.get("package_name", ""),
                    item.get("category", ""),
                    item.get("reason", ""),
                ),
            )
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _paint(iid: str):
            values = list(tree.item(iid, "values"))
            values[0] = "☑" if iid in selected else "☐"
            tree.item(iid, values=values)

        def _toggle(event=None):
            iid = (
                tree.identify_row(event.y)
                if event is not None and getattr(event, "num", None) == 1
                else tree.focus()
            )
            if not iid:
                return
            if iid in selected:
                selected.remove(iid)
            else:
                selected.add(iid)
            _paint(iid)

        tree.bind("<ButtonRelease-1>", _toggle)
        tree.bind("<space>", _toggle)

        button_row = ttk.Frame(dialog)
        button_row.pack(fill=tk.X, padx=12, pady=12)

        def _select_recommended():
            selected.clear()
            selected.update(
                str(index)
                for index, item in enumerate(candidates)
                if item.get("recommended")
            )
            for iid in tree.get_children():
                _paint(iid)

        def _clear_selection():
            selected.clear()
            for iid in tree.get_children():
                _paint(iid)

        ttk.Button(button_row, text="恢复推荐勾选", command=_select_recommended).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(button_row, text="清空选择", command=_clear_selection).pack(
            side=tk.LEFT
        )
        ttk.Button(button_row, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)
        submit_btn = ttk.Button(button_row, text="将勾选项分配为 rain")
        submit_btn.pack(side=tk.RIGHT, padx=8)

        def _submit():
            packages = [
                candidates[int(iid)]["package_name"]
                for iid in sorted(selected, key=int)
            ]
            if not packages:
                messagebox.showwarning("未选择", "请至少勾选一个包体", parent=dialog)
                return
            if not messagebox.askyesno(
                "确认分配",
                f"确定把 {len(packages)} 个包体分配给 rain 吗？",
                parent=dialog,
            ):
                return
            submit_btn.configure(state=tk.DISABLED)
            self._sync_status.config(text="正在批量分配 rain...", foreground="#ffa726")

            def _assign():
                result = assign_cp_candidates(
                    packages,
                    api_url=api_url,
                    x_token=x_token,
                    token=token,
                    assignee="rain",
                    user_name="rain",
                )
                self._safe_after(0, lambda result=result: _finish(result))

            threading.Thread(target=_assign, daemon=True).start()

        def _finish(assign_result: dict):
            for item in assign_result.get("results") or []:
                if item.get("ok"):
                    self._sync_log(
                        f"✓ {item.get('package_name')} 已分配给 rain", "done"
                    )
                else:
                    self._sync_log(
                        f"✗ {item.get('package_name')} 分配失败: {item.get('error')}",
                        "error",
                    )
            success_count = int(assign_result.get("success_count") or 0)
            failure_count = int(assign_result.get("failure_count") or 0)
            self._sync_status.config(
                text=f"分配完成：成功 {success_count}，失败 {failure_count}",
                foreground="#81c784" if not failure_count else "#ffa726",
            )
            if failure_count:
                submit_btn.configure(state=tk.NORMAL)
                messagebox.showwarning(
                    "部分失败",
                    f"成功 {success_count} 条，失败 {failure_count} 条；请查看同步输出。",
                    parent=dialog,
                )
            else:
                messagebox.showinfo(
                    "分配完成",
                    f"已将 {success_count} 个包体分配给 rain，并完成后台回读确认。",
                    parent=dialog,
                )
                dialog.destroy()

        submit_btn.configure(command=_submit)

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
        self._save_sync_settings()

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
        self._prefill_sync_btn.configure(state=tk.DISABLED)
        self._sync_status.config(text="同步中...", foreground="#ffa726")

        def _run():
            import traceback
            try:
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self._safe_after(0, lambda: self._sync_log("  开始 Google Sheets → Asana 同步", "cmd"))
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet    : {sheet_id} / {sheet_name}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Project  : {project_gid}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  SA 文件  : {sa_file}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  代理     : {proxy_url or '(无)'}", "info"))
                self._safe_after(0, lambda: self._sync_log("", "info"))

                # 1. 构建客户端
                self._safe_after(0, lambda: self._sync_log("[1/3] 初始化认证 ...", "cmd"))
                gs_service, asana_client = build_sync_clients(
                    sa_file=sa_file,
                    asana_pat=asana_pat,
                    proxy_url=proxy_url or None,
                )
                self._safe_after(0, lambda: self._sync_log("  认证通过 ✓", "done"))

                # 2. 执行同步
                self._safe_after(0, lambda: self._sync_log("[2/3] 执行同步 ...", "cmd"))
                result = sync_packages(
                    gs_service=gs_service,
                    asana_client=asana_client,
                    sheet_id=sheet_id,
                    project_gid=project_gid,
                    sheet_name=sheet_name,
                    parent_task_gid=parent_gid or None,
                )

                # 3. 展示结果
                self._safe_after(0, lambda: self._sync_log("[3/3] 同步结果:", "cmd"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet 匹配日期 : {result['sheet_date']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Asana 区段名称 : {result['section_name']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Asana 区段 GID  : {result['section_gid']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet 筛选包数 : {result['total_packages']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Asana 已有任务 : {result['existing_count']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  本次新建任务   : {result['new_count']}", "done"))
                self._safe_after(0, lambda: self._sync_log(f"  本次写入描述   : {result['notes_updated_count']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  本次回填链接   : {result['backfilled_count']}", "info"))
                if result.get("backfill_skipped_reason"):
                    self._safe_after(0, lambda: self._sync_log(
                        f"  回填跳过原因   : {result['backfill_skipped_reason']}", "error"
                    ))
                if result["created_gids"]:
                    self._safe_after(0, lambda: self._sync_log(
                        f"  新建任务 GIDs  : {', '.join(result['created_gids'])}", "info"
                    ))
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                if result["new_count"] == 0:
                    self._safe_after(0, lambda: self._sync_log("✓ 幂等：无需新建任务，所有包名已存在。", "done"))
                else:
                    self._safe_after(0, lambda: self._sync_log(
                        f"✓ 同步完成：新建 {result['new_count']} 个任务。", "done"
                    ))
                self._safe_after(0, lambda: self._sync_status.config(
                    text=f"完成 — 新建 {result['new_count']} 个任务", foreground="#81c784"
                ))
            except ImportError as e:
                self._safe_after(0, lambda: self._sync_log(
                    f"缺少依赖: {e}\n请运行: pip install google-auth google-api-python-client asana", "error"
                ))
                self._safe_after(0, lambda: self._sync_status.config(text="缺少依赖", foreground="#ef5350"))
            except Exception as e:
                tb = traceback.format_exc()
                self._safe_after(0, lambda e=e: self._sync_log(f"同步失败: {e}", "error"))
                self._safe_after(0, lambda tb=tb: self._sync_log(tb, "error"))
                self._safe_after(0, lambda: self._sync_status.config(text="同步失败", foreground="#ef5350"))
            finally:
                self._safe_after(0, lambda: self._sync_btn.configure(state=tk.NORMAL))
                self._safe_after(0, lambda: self._prefill_sync_btn.configure(state=tk.NORMAL))
                self._safe_after(0, lambda: setattr(self, '_sync_running', False))

        threading.Thread(target=_run, daemon=True).start()

    def _on_start_prefill_and_sync(self):
        """先拉取 CP 后台数据写入 Sheet，再执行 Google Sheets → Asana 同步。"""
        if self._sync_running:
            self._sync_status.config(text="同步已在运行中", foreground="#ef5350")
            return

        sheet_id      = self.sheet_id_var.get().strip()
        sheet_name    = self.sheet_name_var.get().strip()
        project_gid   = self.project_gid_var.get().strip()
        asana_pat     = self.asana_pat_var.get().strip()
        sa_file       = self.sa_file_var.get().strip()
        proxy_url     = self.proxy_url_var.get().strip()
        parent_gid    = self.parent_task_gid_var.get().strip()
        api_url       = self.cp_adapt_api_url_var.get().strip()
        x_token       = self.cp_adapt_x_token_var.get().strip()
        cp_token      = self.cp_adapt_token_var.get().strip()
        assign        = self.cp_adapt_assign_var.get().strip() or "rain"
        self._save_sync_settings()

        missing = []
        if not sheet_id: missing.append("Sheet ID")
        if not project_gid: missing.append("Asana 项目 GID")
        if not asana_pat: missing.append("Asana PAT")
        if not sa_file: missing.append("SA 密钥文件")
        if not api_url: missing.append("接口地址")
        if not x_token: missing.append("X-Token")
        if not cp_token: missing.append("固定 token")
        if missing:
            self._sync_log(f"配置缺失: {', '.join(missing)}", "error")
            self._sync_status.config(text="配置不完整", foreground="#ef5350")
            return

        if not os.path.isfile(sa_file):
            self._sync_log(f"SA 密钥文件不存在: {sa_file}", "error")
            self._sync_status.config(text="SA 文件不存在", foreground="#ef5350")
            return

        self._sync_running = True
        self._sync_btn.configure(state=tk.DISABLED)
        self._prefill_sync_btn.configure(state=tk.DISABLED)
        self._sync_status.config(text="写入 Sheet 并同步中...", foreground="#ffa726")

        def _run():
            import traceback
            try:
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self._safe_after(0, lambda: self._sync_log("  开始 CP 后台 → Google Sheets → Asana 同步", "cmd"))
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet    : {sheet_id} / {sheet_name}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Project  : {project_gid}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  CP 接口  : {api_url}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  筛选人员 : {assign}", "info"))
                self._safe_after(0, lambda: self._sync_log("", "info"))

                self._safe_after(0, lambda: self._sync_log("[1/4] 初始化认证 ...", "cmd"))
                gs_service, asana_client = build_sync_clients(
                    sa_file=sa_file,
                    asana_pat=asana_pat,
                    proxy_url=proxy_url or None,
                )
                self._safe_after(0, lambda: self._sync_log("  认证通过 ✓", "done"))

                self._safe_after(0, lambda: self._sync_log("[2/4] 拉取 CP 后台并写入 Sheet ...", "cmd"))
                prefill_result = sync_cp_adapt_records_to_sheet(
                    gs_service=gs_service,
                    sheet_id=sheet_id,
                    sheet_name=sheet_name,
                    api_url=api_url,
                    x_token=x_token,
                    token=cp_token,
                    assign=assign,
                )
                self._safe_after(0, lambda: self._sync_log(
                    f"  后台返回 {prefill_result['fetched_count']} / total={prefill_result['reported_total']} 条", "info"
                ))
                self._safe_after(0, lambda: self._sync_log(
                    f"  Sheet 更新 {prefill_result['updated_count']} 行，追加 {prefill_result['appended_count']} 行", "done"
                ))

                self._safe_after(0, lambda: self._sync_log("[3/4] 执行 Asana 同步 ...", "cmd"))
                result = sync_packages(
                    gs_service=gs_service,
                    asana_client=asana_client,
                    sheet_id=sheet_id,
                    project_gid=project_gid,
                    sheet_name=sheet_name,
                    parent_task_gid=parent_gid or None,
                    notes_by_name=prefill_result.get("notes_by_name") or None,
                )

                self._safe_after(0, lambda: self._sync_log("[4/4] 同步结果:", "cmd"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet 匹配日期 : {result['sheet_date']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Asana 区段名称 : {result['section_name']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  Sheet 筛选包数 : {result['total_packages']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  本次新建任务   : {result['new_count']}", "done"))
                self._safe_after(0, lambda: self._sync_log(f"  本次写入描述   : {result['notes_updated_count']}", "info"))
                self._safe_after(0, lambda: self._sync_log(f"  本次回填链接   : {result['backfilled_count']}", "info"))
                self._safe_after(0, lambda: self._sync_log("=" * 50, "cmd"))
                self._safe_after(0, lambda: self._sync_log("✓ 完整链路同步完成。", "done"))
                self._safe_after(0, lambda: self._sync_status.config(
                    text=f"完成 — 写入 {prefill_result['written_count']} 行，新建 {result['new_count']} 个任务",
                    foreground="#81c784",
                ))
            except ImportError as e:
                self._safe_after(0, lambda: self._sync_log(
                    f"缺少依赖: {e}\n请运行: pip install google-auth google-api-python-client asana", "error"
                ))
                self._safe_after(0, lambda: self._sync_status.config(text="缺少依赖", foreground="#ef5350"))
            except Exception as e:
                tb = traceback.format_exc()
                self._safe_after(0, lambda e=e: self._sync_log(f"同步失败: {e}", "error"))
                self._safe_after(0, lambda tb=tb: self._sync_log(tb, "error"))
                self._safe_after(0, lambda: self._sync_status.config(text="同步失败", foreground="#ef5350"))
            finally:
                self._safe_after(0, lambda: self._sync_btn.configure(state=tk.NORMAL))
                self._safe_after(0, lambda: self._prefill_sync_btn.configure(state=tk.NORMAL))
                self._safe_after(0, lambda: setattr(self, '_sync_running', False))

        threading.Thread(target=_run, daemon=True).start()

    # ── 整体布局 ──────────────────────────────────────────────────

    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        apk_tab = ttk.Frame(notebook)
        notebook.add(apk_tab, text="APK 工具")
        self._build_apk_tab(apk_tab)

        precheck_tab = ttk.Frame(notebook)
        notebook.add(precheck_tab, text="页面预检")
        self._build_precheck_tab(precheck_tab)

        adb_tab = ttk.Frame(notebook)
        notebook.add(adb_tab, text="ADB 指令")
        self._build_adb_tab(adb_tab)

        action_script_tab = ttk.Frame(notebook)
        notebook.add(action_script_tab, text="自动化脚本")
        self._build_action_script_tab(action_script_tab)

        automation_tab = ttk.Frame(notebook)
        notebook.add(automation_tab, text="自动化适配")
        self._build_automation_tab(automation_tab)

        summary_tab = ttk.Frame(notebook)
        notebook.add(summary_tab, text="当日总结")
        self._daily_summary_tab = summary_tab
        self._daily_summary_built = False
        notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed, add="+")

        sync_tab = ttk.Frame(notebook)
        notebook.add(sync_tab, text="数据同步")
        self._build_sync_tab(sync_tab)

        self.status_label = ttk.Label(
            self.root, text="就绪", relief=tk.SUNKEN, anchor=tk.W, padding=(6, 2)
        )
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X)

    def _on_notebook_tab_changed(self, event):
        if self._daily_summary_built:
            return
        notebook = event.widget
        if notebook.select() != str(self._daily_summary_tab):
            return
        self._daily_summary_built = True
        self._build_daily_summary_tab(self._daily_summary_tab)

    # ── 当日总结 Tab ──────────────────────────────────────────────

    def _build_daily_summary_tab(self, parent: ttk.Frame):
        container = ttk.Frame(parent, padding=18)
        container.pack(fill=tk.BOTH, expand=True)

        info = ttk.LabelFrame(container, text="Asana 当日适配总结", padding=12)
        info.pack(fill=tk.X)
        ttk.Label(
            info,
            text=(
                "读取所选日期执行分组中的当日评论，自动归类聚合通过、动作通过、"
                "暂不适配和加黑；使用“数据同步”页中的项目 GID 与 Asana PAT。"
            ),
            foreground="#757575",
            wraplength=780,
        ).pack(anchor=tk.W)

        toolbar = ttk.Frame(container)
        toolbar.pack(fill=tk.X, pady=(12, 8))
        ttk.Label(toolbar, text="日期：").pack(side=tk.LEFT)
        ttk.Entry(
            toolbar, textvariable=self.daily_summary_date_var, width=14
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._daily_summary_generate_btn = ttk.Button(
            toolbar, text="生成总结", command=self._on_generate_daily_summary
        )
        self._daily_summary_generate_btn.pack(side=tk.LEFT)
        ttk.Button(
            toolbar, text="一键复制", command=self._copy_daily_summary
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._daily_summary_status = ttk.Label(
            toolbar, text="就绪", foreground="#757575"
        )
        self._daily_summary_status.pack(side=tk.RIGHT)

        result_frame = ttk.LabelFrame(container, text="总结预览", padding=8)
        result_frame.pack(fill=tk.BOTH, expand=True)
        self.daily_summary_text = tk.Text(
            result_frame,
            wrap=tk.WORD,
            font=("SF Mono", 11),
            padx=10,
            pady=10,
        )
        scroll = ttk.Scrollbar(
            result_frame, orient=tk.VERTICAL, command=self.daily_summary_text.yview
        )
        self.daily_summary_text.configure(yscrollcommand=scroll.set)
        self.daily_summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _set_daily_summary_result(self, result: dict):
        self.daily_summary_text.delete("1.0", tk.END)
        self.daily_summary_text.insert("1.0", result.get("text", ""))
        self._daily_summary_status.config(
            text=(
                f"{result.get('section_name', '')} · "
                f"{result.get('task_count', 0)} 个任务 · "
                f"{result.get('comment_count', 0)} 条当日评论"
            ),
            foreground="#2e7d32",
        )

    def _on_generate_daily_summary(self):
        if self._daily_summary_running:
            return
        try:
            target_date = datetime.strptime(
                self.daily_summary_date_var.get().strip(), "%Y-%m-%d"
            ).date()
        except ValueError:
            self._daily_summary_status.config(
                text="日期格式应为 YYYY-MM-DD", foreground="#e53935"
            )
            return
        project_gid = self.project_gid_var.get().strip()
        asana_pat = self.asana_pat_var.get().strip()
        if not project_gid or not asana_pat:
            self._daily_summary_status.config(
                text="请先在数据同步页填写项目 GID 和 Asana PAT",
                foreground="#e53935",
            )
            return

        self._daily_summary_running = True
        self._daily_summary_generate_btn.configure(state=tk.DISABLED)
        self._daily_summary_status.config(text="正在读取 Asana 评论...", foreground="#ef6c00")

        def _run():
            try:
                result = generate_daily_asana_summary(
                    build_asana_client(asana_pat), project_gid, target_date
                )
                self._safe_after(0, self._set_daily_summary_result, result)
            except Exception as exc:
                self._safe_after(
                    0,
                    self._daily_summary_status.config,
                    {"text": f"生成失败：{exc}", "foreground": "#e53935"},
                )
            finally:
                self._daily_summary_running = False
                self._safe_after(
                    0, self._daily_summary_generate_btn.configure, {"state": tk.NORMAL}
                )

        threading.Thread(target=_run, daemon=True).start()

    def _copy_daily_summary(self):
        text = self.daily_summary_text.get("1.0", tk.END).strip()
        if not text:
            self._daily_summary_status.config(
                text="请先生成总结", foreground="#e53935"
            )
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._daily_summary_status.config(text="总结已复制", foreground="#2e7d32")

    # ── 自动化适配 Tab（不改变现有手动按钮） ────────────────────

    def _build_automation_tab(self, parent: ttk.Frame):
        outer = ttk.Frame(parent, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        intro = ttk.LabelFrame(outer, text="自动化说明", padding=10)
        intro.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            intro,
            text=(
                "本页使用独立自动化函数，不改变 ADB 指令页原有按钮。"
                "单包按钮保留手动校对；批量模式会按预检列表顺序自动完成 "
                "ADB 配置、参数检测、Asana 回填、后台提交和广告回放验证。"
            ),
            foreground="gray",
            wraplength=820,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        task_frame = ttk.LabelFrame(outer, text="当前 Asana 任务", padding=10)
        task_frame.pack(fill=tk.X, pady=(0, 8))
        first = ttk.Frame(task_frame)
        first.pack(fill=tk.X)
        ttk.Label(first, text="Task GID:").pack(side=tk.LEFT)
        ttk.Entry(first, textvariable=self.automation_task_gid_var, width=24).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        ttk.Label(first, text="包名:").pack(side=tk.LEFT)
        ttk.Entry(first, textvariable=self.automation_package_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 10)
        )
        ttk.Button(
            first,
            text="从页面预检选中任务带入",
            command=self._automation_use_selected_precheck_task,
        ).pack(side=tk.RIGHT)
        second = ttk.Frame(task_frame)
        second.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(second, text="UP2 appid:").pack(side=tk.LEFT)
        ttk.Entry(second, textvariable=self.automation_appid_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 12)
        )
        ttk.Label(second, text="回放监听:").pack(side=tk.LEFT)
        ttk.Spinbox(
            second,
            from_=10,
            to=600,
            width=5,
            textvariable=self.automation_replay_timeout_var,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(second, text="秒（默认 500）", foreground="gray").pack(side=tk.LEFT)

        fields_frame = ttk.LabelFrame(outer, text="聚合参数校对", padding=8)
        fields_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 8))
        field_toolbar = ttk.Frame(fields_frame)
        field_toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(
            field_toolbar,
            text="提取当前聚合参数",
            command=self._automation_extract_fields,
        ).pack(side=tk.LEFT)
        ttk.Button(
            field_toolbar,
            text="自动回填 Asana",
            command=self._automation_fill_asana,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            field_toolbar,
            text="接口提交后台",
            command=self._automation_submit_backend,
        ).pack(side=tk.LEFT)
        self._automation_field_status = ttk.Label(
            field_toolbar, text="尚未提取", foreground="gray"
        )
        self._automation_field_status.pack(side=tk.RIGHT)
        self.automation_fields_text = tk.Text(
            fields_frame,
            height=10,
            wrap=tk.WORD,
            font=("Menlo", 10),
            bg="#f5f5f5",
            fg="#333333",
            state=tk.DISABLED,
        )
        self.automation_fields_text.pack(fill=tk.BOTH, expand=True)

        control = ttk.LabelFrame(outer, text="自动化执行", padding=10)
        control.pack(fill=tk.X, pady=(0, 8))
        self._automation_run_btn = ttk.Button(
            control,
            text="参数确认无误：回填 → 提交 → 回放",
            command=self._automation_run_post_detection,
        )
        self._automation_run_btn.pack(side=tk.LEFT)
        self._automation_batch_btn = ttk.Button(
            control,
            text="批量自动适配预检合格任务",
            command=self._automation_run_eligible_batch,
        )
        self._automation_batch_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(
            control,
            text="仅开始聚合回放检测",
            command=self._automation_start_replay,
        ).pack(side=tk.LEFT, padx=6)
        self._automation_stop_btn = ttk.Button(
            control,
            text="停止",
            command=self._automation_stop,
            state=tk.DISABLED,
        )
        self._automation_stop_btn.pack(side=tk.LEFT)
        self._automation_pause_btn = ttk.Button(
            control,
            text="暂停队列",
            command=self._automation_toggle_pause,
            state=tk.DISABLED,
        )
        self._automation_pause_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._automation_status = ttk.Label(control, text="就绪", foreground="gray")
        self._automation_status.pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(outer, text="自动化日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.automation_log_text = tk.Text(
            log_frame,
            height=11,
            wrap=tk.WORD,
            font=("Menlo", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            state=tk.DISABLED,
        )
        log_scroll = ttk.Scrollbar(log_frame, command=self.automation_log_text.yview)
        self.automation_log_text.configure(yscrollcommand=log_scroll.set)
        self.automation_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _automation_log(self, message: str):
        if not hasattr(self, "automation_log_text"):
            return
        self.automation_log_text.configure(state=tk.NORMAL)
        self.automation_log_text.insert(
            tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
        )
        self.automation_log_text.see(tk.END)
        self.automation_log_text.configure(state=tk.DISABLED)

    def _automation_set_status(self, text: str, color: str = "#616161"):
        self._automation_status.config(text=text, foreground=color)

    def _automation_set_running(self, running: bool):
        self._automation_running = running
        self._automation_run_btn.configure(state=tk.DISABLED if running else tk.NORMAL)
        if self._automation_batch_btn is not None:
            self._automation_batch_btn.configure(
                state=tk.DISABLED if running else tk.NORMAL
            )
        if self._precheck_auto_adapt_btn is not None:
            self._precheck_auto_adapt_btn.configure(
                state=tk.DISABLED if running else tk.NORMAL
            )
        self._automation_stop_btn.configure(state=tk.NORMAL if running else tk.DISABLED)
        if self._automation_pause_btn is not None:
            self._automation_pause_btn.configure(
                state=(
                    tk.NORMAL
                    if running and self._automation_batch_active
                    else tk.DISABLED
                ),
                text="暂停队列",
            )
        if running:
            self._automation_stop_event.clear()
            self._automation_pause_event.clear()
        else:
            self._automation_pause_event.clear()
            self._automation_batch_active = False

    def _automation_use_selected_precheck_task(self):
        item_id, task = self._selected_precheck_task()
        if task is None:
            self._automation_set_status("请先在页面预检中选择任务", "#e53935")
            return
        self._automation_clear_detected_fields()
        self._automation_precheck_item_id = item_id or ""
        self.automation_task_gid_var.set(str(getattr(task, "gid", "") or ""))
        self.automation_package_var.set(str(getattr(task, "package_name", "") or ""))
        self.automation_appid_var.set(str(getattr(task, "up2_appid", "") or ""))
        self._automation_task_notes = str(getattr(task, "notes", "") or "")
        package_name = self.automation_package_var.get().strip()
        appid = self.automation_appid_var.get().strip()
        try:
            self._write_automation_task_config(package_name, appid)
            self._sync_adb_tab_from_automation(
                package_name,
                appid,
                AUTOMATION_AGGREGATION_TASK_UUID,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._automation_log(f"任务已带入，但写入 config 失败: {exc}")
            self._automation_set_status(f"写入 config 失败: {exc}", "#e53935")
            return
        self._automation_log(
            f"已带入并写入 config: {package_name} / {appid}"
        )
        self._automation_set_status("任务已带入并写入 config", "#2e7d32")

    def _automation_clear_detected_fields(self, status_text: str = "尚未提取"):
        """Drop every detected value before another package is processed."""
        self._automation_context_version += 1
        self._automation_fields = {}
        self._automation_replay_id_candidates = {}
        self.automation_fields_text.configure(state=tk.NORMAL)
        self.automation_fields_text.delete("1.0", tk.END)
        self.automation_fields_text.configure(state=tk.DISABLED)
        self._automation_field_status.config(
            text=status_text,
            foreground="gray",
        )

    def _automation_render_fields(
        self,
        data: dict,
        context_version: int | None = None,
    ):
        if (
            context_version is not None
            and context_version != self._automation_context_version
        ):
            return
        text = format_aggregation_fields(data)
        assessment = build_aggregation_assessment(data)
        self.automation_fields_text.configure(state=tk.NORMAL)
        self.automation_fields_text.delete("1.0", tk.END)
        self.automation_fields_text.insert("1.0", text)
        self.automation_fields_text.configure(state=tk.DISABLED)
        confidence = assessment["confidence"]
        method = assessment["method"]
        policy = assessment["policy"]
        color = (
            "#2e7d32"
            if confidence == "高" and assessment["auto_submit"]
            else "#ef6c00"
            if assessment["auto_submit"]
            else "#e53935"
        )
        self._automation_field_status.config(
            text=f"{confidence}置信度 · {method} · {policy}",
            foreground=color,
        )

    def _automation_extract_fields(self):
        if self._automation_running:
            return
        self._automation_clear_detected_fields("正在提取当前应用参数...")
        context_version = self._automation_context_version
        self._automation_set_running(True)
        self._automation_set_status("正在提取参数...", "#ef6c00")

        def _run():
            try:
                self._safe_after(
                    0,
                    self._automation_log,
                    "[ADB 前置] 配置、推送、构建、UID 和 UID 日志过滤",
                )
                initial_fields = self._automation_prepare_detection_sync()
                runtime_monitor = PackageRuntimeMonitor(
                    self.automation_package_var.get().strip()
                )
                result = detect_aggregation_with_one_retry(
                    self.automation_package_var.get().strip(),
                    lambda: self._automation_extract_logcat_fields(),
                    first_fields=initial_fields,
                    stop_event=self._automation_stop_event,
                    on_progress=lambda text: self._safe_after(
                        0, self._automation_log, text
                    ),
                    runtime_check=runtime_monitor.poll,
                    runtime_reset=runtime_monitor.reset,
                )
                result = reconcile_detection_result(result)
                data = result.get("fields") or {}
                if context_version != self._automation_context_version:
                    self._safe_after(
                        0,
                        self._automation_log,
                        "已丢弃上一应用延迟返回的聚合参数",
                    )
                    return
                self._automation_fields = data
                if data:
                    self._safe_after(
                        0,
                        self._automation_render_fields,
                        data,
                        context_version,
                    )
                if result.get("ok"):
                    self._safe_after(
                        0, self._automation_log, result.get("message", "聚合参数提取完成")
                    )
                    assessment = build_aggregation_assessment(data)
                    if (
                        (
                            assessment.get("confidence") == "高"
                            or data.get("_aggregation_type_inferred")
                        )
                        and assessment.get("auto_submit")
                    ):
                        self._safe_after(
                            0,
                            self._automation_log,
                            (
                                "video/inter 已按规则推断为 IronSource，"
                                "自动继续回填 → 提交 → 回放"
                                if data.get("_aggregation_type_inferred")
                                else "高置信度且参数校验通过，自动继续回填 → 提交 → 回放"
                            ),
                        )
                        self._automation_execute_post_detection_sync()
                    else:
                        self._safe_after(
                            0, self._automation_set_status, "参数待校对", "#ef6c00"
                        )
                    return
                message = result.get("message", "聚合参数提取失败")
                if result.get("code") == "UNSUPPORTED_ATTRIBUTION":
                    # A non-whitelisted or unknown attribution is still a
                    # valid aggregation detection result.  Persist all fields
                    # before ending this task, just like the batch path.
                    self._automation_complete_unsupported_attribution_sync(
                        message
                    )
                    return
                else:
                    self._automation_persist_detected_failure_fields_sync(
                        result.get("code", ""), message
                    )
                    self._automation_mark_failed(message)
                if result.get("code") in {
                    "AGGREGATION_TYPE_EMPTY",
                    "AGGREGATION_RESULT_INCOMPLETE",
                    "AF_KEY_EMPTY",
                    "AD_IDS_EMPTY",
                    "UNSUPPORTED_ATTRIBUTION",
                    "APP_CRASHED",
                    "APP_EXITED_DURING_AUTOMATION",
                }:
                    package_name = self.automation_package_var.get().strip()
                    runtime_summary = str(
                        (result.get("runtime") or {}).get("summary")
                        or (result.get("fields") or {}).get("_runtime_summary")
                        or ""
                    ).strip()
                    self._automation_comment_failure(
                        result.get("code"),
                        f"{message}\n包名：{package_name}"
                        + (f"\n关键崩溃日志：\n{runtime_summary}" if runtime_summary else ""),
                    )
            except Exception as exc:
                if self._automation_stop_event.is_set() or "用户已停止" in str(exc):
                    self._safe_after(
                        0, self._automation_log, "聚合参数提取已由用户停止"
                    )
                    self._safe_after(
                        0, self._automation_set_status, "自动化已停止", "#ef6c00"
                    )
                else:
                    self._automation_mark_failed(f"聚合参数提取失败: {exc}")
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_asana_client(self):
        pat = self.asana_pat_var.get().strip()
        if not pat:
            raise ValueError("请先在数据同步页填写 Asana PAT")
        return build_asana_client(pat)

    def _automation_fill_asana_sync(
        self,
        *,
        allow_unsupported_attribution: bool = False,
        terminal_note: str = "",
    ) -> str:
        if not self._automation_fields:
            raise ValueError("请先提取并校对聚合参数")
        if not has_aggregation_type(self._automation_fields):
            raise ValueError("聚合类型识别为空，不能回填 Asana 描述")
        task_gid = self.automation_task_gid_var.get().strip()
        client = self._automation_asana_client()
        merged = update_asana_aggregation_notes(
            client,
            task_gid,
            self._automation_task_notes,
            self._automation_fields,
            allow_unsupported_attribution=allow_unsupported_attribution,
            terminal_note=terminal_note,
        )
        self._automation_task_notes = merged
        return merged

    def _automation_persist_detected_failure_fields_sync(
        self, code: str, message: str
    ) -> bool:
        """Keep usable detection evidence in Asana without backend submission."""
        if code not in {"AF_KEY_EMPTY", "AD_IDS_EMPTY", "AGGREGATION_RESULT_INCOMPLETE"}:
            return False
        if not has_aggregation_type(self._automation_fields):
            return False
        try:
            self._automation_fill_asana_sync()
        except Exception as exc:
            self._safe_after(
                0,
                self._automation_log,
                f"检测结果回填 Asana 描述失败：{exc}",
            )
            return False
        self._safe_after(
            0,
            self._automation_log,
            f"检测结果已回填至 Asana 描述；因{message}，未提交适配后台",
        )
        return True

    def _automation_fill_asana(self):
        if self._automation_running:
            return
        self._automation_set_running(True)
        self._automation_set_status("正在回填 Asana...", "#ef6c00")

        def _run():
            try:
                self._automation_fill_asana_sync()
                self._safe_after(0, self._automation_log, "聚合参数已回填至 Asana 描述")
                self._safe_after(0, self._automation_set_status, "Asana 回填完成", "#2e7d32")
            except Exception as exc:
                self._safe_after(0, self._automation_log, f"Asana 回填失败: {exc}")
                self._safe_after(0, self._automation_set_status, "Asana 回填失败", "#e53935")
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_submit_backend_sync(
        self, *, allow_unsupported_attribution: bool = False
    ) -> dict:
        if not self._automation_fields:
            raise ValueError("请先提取并校对聚合参数")
        return submit_backend_via_api(
            self._automation_fields,
            self.automation_package_var.get().strip(),
            api_url=self.cp_adapt_api_url_var.get().strip(),
            x_token=self.cp_adapt_x_token_var.get().strip(),
            token=self.cp_adapt_token_var.get().strip(),
            user_name=self.cp_adapt_assign_var.get().strip() or "rain",
            stop_event=self._automation_stop_event,
            allow_unsupported_attribution=allow_unsupported_attribution,
        )

    def _automation_clear_inferred_backend_sync(
        self,
        note: str = INFERRED_AGGREGATION_FAILURE_NOTE,
    ) -> dict:
        """Rollback the provisional video/inter submission after replay failure."""
        return clear_backend_adaptation_via_api(
            self.automation_package_var.get().strip(),
            api_url=self.cp_adapt_api_url_var.get().strip(),
            x_token=self.cp_adapt_x_token_var.get().strip(),
            token=self.cp_adapt_token_var.get().strip(),
            user_name=self.cp_adapt_assign_var.get().strip() or "rain",
            note=note,
        )

    def _automation_handle_replay_type_change_sync(self, replay: dict) -> bool:
        """Replace provisional IronSource data with authoritative MAX data."""
        detected_fields = dict(replay.get("detected_fields") or {})
        verdict = normalize_optional_parameter(detected_fields.get("最终判断"))
        assessment = build_aggregation_assessment(detected_fields)
        if (
            "max" not in verdict.casefold()
            or assessment.get("confidence") != "高"
            or not assessment.get("auto_submit")
        ):
            return self._automation_handle_inferred_replay_failure_sync(replay)

        self._safe_after(
            0,
            self._automation_log,
            "回放期间明确识别为 MAX；正在先清空临时 IronSource 参数",
        )
        cleared = self._automation_clear_inferred_backend_sync(
            note="回放检测到 MAX，清理临时 IronSource 参数"
        )
        self._safe_after(0, self._automation_log, cleared.get("message", ""))
        if not cleared.get("ok"):
            message = (
                "检测到聚合类型变为 MAX，但清空旧 IronSource 参数失败；"
                "为避免混合配置，已停止自动化"
            )
            self._automation_mark_failed(message)
            self._automation_comment_failure(
                cleared.get("code", "AGGREGATION_TYPE_CHANGE_CLEAR_FAILED"),
                message + "\n" + cleared.get("message", ""),
            )
            return False

        detected_fields["_aggregation_type_changed_during_replay"] = True
        detected_fields.pop("_aggregation_type_inferred", None)
        self._automation_fields = detected_fields
        # The authoritative MAX result owns a different set of ad-unit
        # candidates.  Never reuse the provisional IronSource candidate state.
        self._automation_replay_id_candidates = {}
        self._safe_after(
            0,
            self._automation_render_fields,
            detected_fields,
            self._automation_context_version,
        )
        self._safe_after(
            0,
            self._automation_log,
            "旧 IronSource 参数已置空；改用回放期间检测到的 MAX 参数",
        )
        # Reuse the standard tail: replace Asana data, submit MAX through the
        # API, clear/read back cache, then start a fresh MAX replay.
        return self._automation_execute_post_detection_sync()

    def _automation_handle_inferred_replay_failure_sync(
        self, replay: dict
    ) -> bool:
        """Clear provisional backend data and record the terminal conclusion."""
        package_name = self.automation_package_var.get().strip()
        replay_message = replay.get("message", "聚合广告回放失败")
        self._safe_after(
            0,
            self._automation_log,
            "推断 IronSource 回放未成功，正在清空刚提交的后台适配参数",
        )
        cleared = self._automation_clear_inferred_backend_sync()
        self._safe_after(0, self._automation_log, cleared.get("message", ""))

        description_error = ""
        try:
            self._automation_fill_asana_sync(
                terminal_note=INFERRED_AGGREGATION_FAILURE_NOTE
            )
            self._safe_after(
                0,
                self._automation_log,
                "暂不适配结论已回填至 Asana 描述",
            )
        except Exception as exc:
            description_error = str(exc)
            self._safe_after(
                0,
                self._automation_log,
                f"Asana 暂不适配描述回填失败: {exc}",
            )

        comment_lines = [
            INFERRED_AGGREGATION_FAILURE_NOTE,
            f"包名：{package_name}",
            f"回放结果：{replay_message}",
            f"后台清空：{cleared.get('message', '未返回结果')}",
        ]
        if description_error:
            comment_lines.append(f"Asana 描述回填失败：{description_error}")
        comment_code = (
            "INFERRED_AGGREGATION_REPLAY_FAILED"
            if cleared.get("ok")
            else "INFERRED_AGGREGATION_CLEAR_FAILED"
        )
        self._automation_comment_failure(comment_code, "\n".join(comment_lines))

        if cleared.get("ok"):
            self._safe_after(
                0,
                self._automation_set_status,
                "未识别出聚合类型，暂不适配",
                "#ef6c00",
            )
            if self._automation_precheck_item_id:
                self._safe_after(
                    0,
                    self._set_precheck_task_status,
                    self._automation_precheck_item_id,
                    "未识别聚合类型",
                )
        else:
            self._automation_mark_failed(
                "回放失败且后台参数清空失败，需要人工立即处理"
            )
        return False

    def _automation_comment_failure(self, code: str, message: str):
        task_gid = self.automation_task_gid_var.get().strip()
        if not task_gid:
            return
        try:
            add_automation_comment_once(
                self._automation_asana_client(), task_gid, code, message
            )
            self._safe_after(0, self._automation_log, "失败原因已写入 Asana 评论")
        except Exception as exc:
            self._safe_after(0, self._automation_log, f"Asana 评论失败: {exc}")

    def _automation_comment_success(self, result: dict):
        task_gid = self.automation_task_gid_var.get().strip()
        package_name = self.automation_package_var.get().strip()
        if not task_gid:
            return
        interstitial = result.get("interstitial") or {}
        rewarded = result.get("rewarded") or {}
        lines = ["聚合适配完成", f"包名：{package_name}"]
        lines.append(
            "插屏广告："
            + ("回放成功" if interstitial.get("displayed") else "未配置")
        )
        lines.append(
            "激励视频："
            + ("回放成功" if rewarded.get("displayed") else "未配置")
        )
        try:
            created = add_automation_comment_once(
                self._automation_asana_client(),
                task_gid,
                "AGGREGATION_REPLAY_SUCCESS",
                "\n".join(lines),
            )
            self._safe_after(
                0,
                self._automation_log,
                "聚合适配成功已写入 Asana 评论"
                if created
                else "Asana 已存在聚合适配成功评论",
            )
        except Exception as exc:
            self._safe_after(0, self._automation_log, f"Asana 成功评论写入失败: {exc}")

    def _automation_mark_failed(self, message: str):
        self._safe_after(0, self._automation_set_status, "自动化失败", "#e53935")
        self._safe_after(0, self._automation_log, message)
        if self._automation_precheck_item_id:
            self._safe_after(
                0,
                self._set_precheck_task_status,
                self._automation_precheck_item_id,
                "自动化失败",
            )

    def _automation_mark_not_adapted(self, message: str):
        self._safe_after(0, self._automation_set_status, "归因暂不适配", "#ef6c00")
        self._safe_after(0, self._automation_log, message)
        if self._automation_precheck_item_id:
            self._safe_after(
                0,
                self._set_precheck_task_status,
                self._automation_precheck_item_id,
                "归因暂不适配",
            )

    def _automation_complete_unsupported_attribution_sync(
        self, message: str
    ) -> bool:
        """Persist unsupported-attribution parameters, then skip replay."""
        package_name = self.automation_package_var.get().strip()
        self._safe_after(
            0,
            self._automation_log,
            "[归因暂不适配 1/2] 回填 Asana 聚合参数描述",
        )
        self._automation_fill_asana_sync(allow_unsupported_attribution=True)
        self._safe_after(
            0,
            self._automation_log,
            "非白名单归因的聚合检测结果已回填至 Asana 描述",
        )
        self._automation_comment_failure(
            "UNSUPPORTED_ATTRIBUTION",
            f"{message}\n包名：{package_name}",
        )
        if self._automation_stop_event.is_set():
            return False
        self._safe_after(
            0,
            self._automation_log,
            "[归因暂不适配 2/2] 提交适配后台、清除缓存并回读校验",
        )
        submit = self._automation_submit_backend_sync(
            allow_unsupported_attribution=True
        )
        self._safe_after(0, self._automation_log, submit.get("message", ""))
        if self._automation_stop_event.is_set():
            self._safe_after(
                0,
                self._automation_log,
                "已停止：后台步骤结束，不进入聚合回放",
            )
            return False
        if not submit.get("ok"):
            submit_message = submit.get("message", "后台自动提交失败")
            self._automation_mark_failed(submit_message)
            self._automation_comment_failure(
                submit.get("code", "BACKEND_SUBMIT_FAILED"), submit_message
            )
            return False
        self._automation_mark_not_adapted(message)
        self._safe_after(
            0,
            self._automation_log,
            "非白名单归因参数已提交并清除缓存；按规则跳过聚合回放",
        )
        return False

    def _automation_submit_backend(self):
        if self._automation_running:
            return
        self._automation_set_running(True)
        self._automation_set_status("正在通过接口提交后台...", "#ef6c00")

        def _run():
            try:
                result = self._automation_submit_backend_sync()
                self._safe_after(0, self._automation_log, result.get("message", ""))
                if result.get("ok"):
                    self._safe_after(0, self._automation_set_status, "后台提交成功", "#2e7d32")
                else:
                    message = result.get("message", "后台自动提交失败")
                    self._automation_mark_failed(message)
                    self._automation_comment_failure(result.get("code", "BACKEND_SUBMIT_FAILED"), message)
            except Exception as exc:
                self._automation_mark_failed(f"后台自动提交失败: {exc}")
                self._automation_comment_failure("BACKEND_SUBMIT_FAILED", str(exc))
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_prepare_replay_id_candidates(self) -> None:
        """Remember all IDs and select the first candidate for initial submit.

        Backend adaptation accepts one ID per ad type.  Keeping the complete
        candidate list outside ``_automation_fields`` also prevents the Asana
        description and backend payload from showing a comma-separated list
        that was never actually replayed.
        """
        candidates = {
            INTERSTITIAL: split_ad_unit_ids(
                self._automation_fields.get("插屏聚合id", "")
            ),
            REWARDED: split_ad_unit_ids(
                self._automation_fields.get("激励视频聚合id", "")
            ),
        }
        self._automation_replay_id_candidates = candidates
        if candidates[INTERSTITIAL]:
            self._automation_fields["插屏聚合id"] = candidates[INTERSTITIAL][0]
        if candidates[REWARDED]:
            self._automation_fields["激励视频聚合id"] = candidates[REWARDED][0]
        self._safe_after(
            0,
            self._automation_render_fields,
            self._automation_fields,
            self._automation_context_version,
        )

    def _automation_replay_sync(
        self,
        *,
        required_types: set[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        if not self._automation_fields:
            raise ValueError("请先提取并校对聚合参数")
        package_name = self.automation_package_var.get().strip()
        timeout = validate_replay_timeout(
            self.automation_replay_timeout_var.get()
            if timeout_seconds is None
            else timeout_seconds
        )
        interstitial_ids = self._automation_fields.get("插屏聚合id", "")
        rewarded_ids = self._automation_fields.get("激励视频聚合id", "")
        if required_types is not None:
            if INTERSTITIAL not in required_types:
                interstitial_ids = ""
            if REWARDED not in required_types:
                rewarded_ids = ""
        expectation = ReplayExpectation.from_values(
            interstitial_ids,
            rewarded_ids,
            self._automation_fields.get("最终判断", ""),
        )
        ok, uid = get_app_uid(package_name)
        if not ok:
            raise RuntimeError(uid)
        self._safe_after(0, self._set_uid, uid)

        def _higher_priority_max_detected(fields: dict) -> bool:
            verdict = normalize_optional_parameter(fields.get("最终判断"))
            if "max" not in verdict.casefold():
                return False
            assessment = build_aggregation_assessment(fields)
            return bool(
                assessment.get("confidence") == "高"
                and assessment.get("auto_submit")
            )

        current_verdict = normalize_optional_parameter(
            self._automation_fields.get("最终判断")
        ).casefold()
        # MAX is authoritative over both an explicit IronSource result and the
        # provisional video/inter business-rule inference.  Keep watching the
        # same UID during the first replay so a later complete AutoDetector MAX
        # result can replace the already-submitted IronSource configuration.
        watch_type_change = bool(
            is_inferred_aggregation_result(self._automation_fields)
            or "ironsource" in current_verdict
            or "iron_source" in current_verdict
        )
        return run_ad_replay_check(
            package_name,
            uid,
            expectation,
            timeout_seconds=timeout,
            stop_event=self._automation_stop_event,
            on_line=lambda line: self._safe_after(0, self._automation_log, line),
            on_progress=lambda text: self._safe_after(
                0, self._automation_set_status, text, "#ef6c00"
            ),
            dismiss_interrupting_dialog=dismiss_notification_permission_dialog,
            aggregation_change_detector=(
                _higher_priority_max_detected if watch_type_change else None
            ),
            aggregation_change_grace_seconds=90,
        )

    def _automation_replay_with_id_rotation_sync(self) -> dict:
        """Replay candidates, locking a successful ad type independently."""
        if not self._automation_replay_id_candidates:
            self._automation_prepare_replay_id_candidates()
        candidates = self._automation_replay_id_candidates
        configured = {
            ad_type
            for ad_type in (INTERSTITIAL, REWARDED)
            if candidates.get(ad_type)
        }
        # Explicit product rule: a single-ID task keeps the original one-shot
        # behavior. Candidate rotation is enabled only if at least one type has
        # more than one detected ID.
        if not any(len(candidates.get(item, ())) > 1 for item in configured):
            return self._automation_replay_sync()

        field_names = {
            INTERSTITIAL: "插屏聚合id",
            REWARDED: "激励视频聚合id",
        }
        labels = {INTERSTITIAL: "插屏", REWARDED: "激励视频"}
        indexes = {item: 0 for item in configured}
        locked: dict[str, dict] = {}
        exhausted: set[str] = set()
        last_result: dict = {}
        attempts = 0

        while not self._automation_stop_event.is_set():
            pending = configured - set(locked) - exhausted
            if not pending:
                break
            attempts += 1
            if attempts == 1:
                result = self._automation_replay_sync(
                    timeout_seconds=MULTI_ID_REPLAY_TIMEOUT_SECONDS
                )
            else:
                result = self._automation_replay_sync(
                    required_types=pending,
                    timeout_seconds=MULTI_ID_REPLAY_TIMEOUT_SECONDS,
                )
            last_result = result

            if result.get("code") == "AGGREGATION_TYPE_CHANGED_DURING_REPLAY":
                return result
            if result.get("action_success"):
                return result

            for ad_type in tuple(pending):
                state = dict(result.get(ad_type) or {})
                if state.get("displayed"):
                    locked[ad_type] = state
                    self._safe_after(
                        0,
                        self._automation_log,
                        f"{labels[ad_type]}广告 ID {candidates[ad_type][indexes[ad_type]]} "
                        "回放成功，后续保持不变",
                    )

            if configured <= set(locked):
                final = dict(result)
                final.update(
                    ok=True,
                    code="AGGREGATION_REPLAY_SUCCESS",
                    message=f"多 ID 轮换完成，已配置的聚合广告均回放成功（共 {attempts} 轮）",
                )
                for ad_type, state in locked.items():
                    final[ad_type] = state
                return final

            if result.get("ok"):
                # A pending-only replay can be successful while a previously
                # exhausted type is still unresolved; keep the combined result.
                pass
            elif result.get("code") != "REPLAY_TIMEOUT":
                return result

            changed: list[str] = []
            for ad_type in configured - set(locked) - exhausted:
                next_index = indexes[ad_type] + 1
                if next_index >= len(candidates[ad_type]):
                    exhausted.add(ad_type)
                    continue
                indexes[ad_type] = next_index
                selected = candidates[ad_type][next_index]
                self._automation_fields[field_names[ad_type]] = selected
                changed.append(f"{labels[ad_type]}={selected}")

            if not changed:
                break
            self._safe_after(
                0,
                self._automation_log,
                "本轮未成功的广告类型切换下一候选 ID：" + "，".join(changed),
            )
            self._safe_after(
                0,
                self._automation_render_fields,
                self._automation_fields,
                self._automation_context_version,
            )
            self._automation_fill_asana_sync()
            submit = self._automation_submit_backend_sync()
            self._safe_after(0, self._automation_log, submit.get("message", ""))
            if not submit.get("ok"):
                return {
                    "ok": False,
                    "code": submit.get("code", "BACKEND_SUBMIT_FAILED"),
                    "message": "切换候选广告 ID 后，后台提交失败："
                    + submit.get("message", "未知错误"),
                }

        final = dict(last_result)
        final.update(
            ok=False,
            code="REPLAY_ID_CANDIDATES_EXHAUSTED",
            message="多 ID 已按顺序尝试完毕，仍有广告类型未确认展示："
            + "、".join(labels[item] for item in configured - set(locked)),
        )
        for ad_type in configured:
            if ad_type in locked:
                final[ad_type] = locked[ad_type]
            else:
                state = dict(final.get(ad_type) or {})
                state.update(
                    required=True,
                    displayed=False,
                    expected_ids=[candidates[ad_type][indexes[ad_type]]],
                )
                final[ad_type] = state
        return final

    def _automation_handle_replay_result(self, result: dict):
        if result.get("ok"):
            self._automation_log(result.get("message", "聚合广告回放成功"))
            self._automation_set_status("聚合适配成功", "#2e7d32")
            if self._automation_precheck_item_id:
                self._set_precheck_task_status(
                    self._automation_precheck_item_id, "聚合适配成功"
                )
            return
        message = result.get("message", "聚合广告回放失败")
        self._automation_mark_failed(message)
        if result.get("code") in {
            "REPLAY_TIMEOUT",
            "REPLAY_ID_CANDIDATES_EXHAUSTED",
        }:
            comment = build_replay_failure_comment(
                self.automation_package_var.get().strip(), result
            )
            comment = "\n".join(comment.splitlines()[1:])
            comment_code = "AD_REPLAY_FAILED"
        else:
            comment = (
                "聚合广告回放失败，自动化适配终止，需要测试人员确认\n"
                f"包名：{self.automation_package_var.get().strip()}\n"
                f"失败原因：{message}"
            )
            comment_code = result.get("code", "AD_REPLAY_FAILED")
        self._automation_comment_failure(comment_code, comment)

    def _automation_start_replay(self):
        if self._automation_running:
            return
        self._automation_set_running(True)

        def _run():
            try:
                result = self._automation_replay_with_id_rotation_sync()
                if result.get("code") == "AGGREGATION_TYPE_CHANGED_DURING_REPLAY":
                    self._automation_handle_replay_type_change_sync(result)
                elif result.get("ok"):
                    self._automation_comment_success(result)
                    self._safe_after(0, self._automation_handle_replay_result, result)
                elif self._automation_fields.get("_aggregation_type_inferred"):
                    self._automation_handle_inferred_replay_failure_sync(result)
                else:
                    self._safe_after(0, self._automation_handle_replay_result, result)
            except Exception as exc:
                if self._automation_fields.get("_aggregation_type_inferred"):
                    self._automation_handle_inferred_replay_failure_sync(
                        {
                            "ok": False,
                            "code": "REPLAY_EXCEPTION",
                            "message": f"聚合回放检测异常：{exc}",
                        }
                    )
                else:
                    self._automation_mark_failed(f"聚合回放检测失败: {exc}")
                    self._automation_comment_failure("AD_REPLAY_FAILED", str(exc))
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_execute_post_detection_sync(self) -> bool:
        """Run the common fill/submit/replay tail for validated fields."""
        if self._automation_stop_event.is_set():
            self._safe_after(
                0, self._automation_log, "已停止：不再回填 Asana 或提交后台"
            )
            self._safe_after(
                0, self._automation_set_status, "自动化已停止", "#ef6c00"
            )
            return False
        self._automation_prepare_replay_id_candidates()
        self._safe_after(0, self._automation_log, "[1/3] 回填 Asana 描述")
        self._automation_fill_asana_sync()
        if self._automation_stop_event.is_set():
            return False
        self._safe_after(0, self._automation_log, "[2/3] 接口提交适配后台")
        submit = self._automation_submit_backend_sync()
        self._safe_after(0, self._automation_log, submit.get("message", ""))
        if self._automation_stop_event.is_set():
            self._safe_after(
                0,
                self._automation_log,
                "已停止：后台步骤结束后不再进入聚合回放",
            )
            self._safe_after(
                0, self._automation_set_status, "自动化已停止", "#ef6c00"
            )
            return False
        if not submit.get("ok"):
            message = submit.get("message", "后台自动提交失败")
            self._automation_mark_failed(message)
            self._automation_comment_failure(
                submit.get("code", "BACKEND_SUBMIT_FAILED"), message
            )
            return False
        self._safe_after(0, self._automation_log, "[3/3] 重启应用并检测聚合回放")
        try:
            replay = self._automation_replay_with_id_rotation_sync()
        except Exception as exc:
            if self._automation_fields.get("_aggregation_type_inferred"):
                return self._automation_handle_inferred_replay_failure_sync(
                    {
                        "ok": False,
                        "code": "REPLAY_EXCEPTION",
                        "message": f"聚合回放检测异常：{exc}",
                    }
                )
            raise
        if replay.get("code") == "AGGREGATION_TYPE_CHANGED_DURING_REPLAY":
            return self._automation_handle_replay_type_change_sync(replay)
        if replay.get("ok"):
            self._automation_comment_success(replay)
        elif self._automation_fields.get("_aggregation_type_inferred"):
            return self._automation_handle_inferred_replay_failure_sync(replay)
        self._safe_after(0, self._automation_handle_replay_result, replay)
        return bool(replay.get("ok"))

    def _automation_run_post_detection(self):
        if self._automation_running:
            return
        context_version = self._automation_context_version
        self._automation_set_running(True)
        self._automation_set_status("开始自动化后半程...", "#ef6c00")

        def _run():
            try:
                field_issue = detection_field_issue(self._automation_fields)
                if field_issue:
                    if self._automation_fields.get("_detection_retry_exhausted") or self._automation_fields.get("_aggregation_retry_exhausted"):
                        detection = {
                            "ok": False,
                            "code": field_issue[0],
                            "message": field_issue[1],
                            "fields": self._automation_fields,
                        }
                    else:
                        first_fields = self._automation_fields or self._automation_prepare_detection_sync()
                        runtime_monitor = PackageRuntimeMonitor(
                            self.automation_package_var.get().strip()
                        )
                        detection = detect_aggregation_with_one_retry(
                            self.automation_package_var.get().strip(),
                            lambda: self._automation_extract_logcat_fields(),
                            first_fields=first_fields,
                            stop_event=self._automation_stop_event,
                            on_progress=lambda text: self._safe_after(
                                0, self._automation_log, text
                            ),
                            runtime_check=runtime_monitor.poll,
                            runtime_reset=runtime_monitor.reset,
                        )
                        detection = reconcile_detection_result(detection)
                    if context_version != self._automation_context_version:
                        self._safe_after(
                            0,
                            self._automation_log,
                            "已丢弃上一应用延迟返回的聚合参数",
                        )
                        return
                    self._automation_fields = detection.get("fields") or {}
                    if self._automation_fields:
                        self._safe_after(
                            0,
                            self._automation_render_fields,
                            self._automation_fields,
                            context_version,
                        )
                    if not detection.get("ok"):
                        message = detection.get("message", "聚合参数提取失败")
                        if detection.get("code") == "UNSUPPORTED_ATTRIBUTION":
                            self._automation_complete_unsupported_attribution_sync(
                                message
                            )
                            return
                        else:
                            self._automation_persist_detected_failure_fields_sync(
                                detection.get("code", ""), message
                            )
                            self._automation_mark_failed(message)
                        if detection.get("code") in {
                            "AGGREGATION_TYPE_EMPTY",
                            "AGGREGATION_RESULT_INCOMPLETE",
                            "AF_KEY_EMPTY",
                            "AD_IDS_EMPTY",
                            "UNSUPPORTED_ATTRIBUTION",
                            "APP_CRASHED",
                            "APP_EXITED_DURING_AUTOMATION",
                        }:
                            runtime_summary = str(
                                (detection.get("runtime") or {}).get("summary")
                                or (detection.get("fields") or {}).get("_runtime_summary")
                                or ""
                            ).strip()
                            self._automation_comment_failure(
                                detection.get("code"),
                                f"{message}\n"
                                f"包名：{self.automation_package_var.get().strip()}"
                                + (f"\n关键崩溃日志：\n{runtime_summary}" if runtime_summary else ""),
                            )
                        return
                self._automation_execute_post_detection_sync()
            except Exception as exc:
                self._automation_mark_failed(f"自动化执行失败: {exc}")
                self._automation_comment_failure("AUTOMATION_FAILED", str(exc))
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_eligible_precheck_tasks(self):
        """Return installed, non-terminal precheck tasks in visible list order."""
        eligible_statuses = {"启动正常", "安装完成", "已安装"}
        queue = []
        for item_id in self.precheck_task_tree.get_children():
            task = self._precheck_tasks.get(item_id)
            if task is None or getattr(task, "completed", False):
                continue
            values = self.precheck_task_tree.item(item_id, "values")
            status = str(values[3] if len(values) >= 4 else "").strip()
            if status in eligible_statuses:
                queue.append((item_id, task))
        return queue

    def _automation_apply_task_context(self, item_id: str, task):
        self._automation_clear_detected_fields()
        self._automation_precheck_item_id = item_id
        self.automation_task_gid_var.set(str(getattr(task, "gid", "") or ""))
        self.automation_package_var.set(
            str(getattr(task, "package_name", "") or "")
        )
        self.automation_appid_var.set(str(getattr(task, "up2_appid", "") or ""))
        self._automation_task_notes = str(getattr(task, "notes", "") or "")
        self._select_precheck_item(item_id)

    def _automation_switch_task_sync(self, item_id: str, task):
        # A page-level UID listener belongs to the previous package.  Stop it
        # before changing the task context so no A-process output can race with
        # the setup and UID switch for package B.
        self._automation_stop_active_logcat("切换到下一个包体")
        self._cached_uid = None
        self._safe_after(0, self.uid_var.set, "")
        if threading.current_thread() is threading.main_thread():
            self._automation_apply_task_context(item_id, task)
            return
        applied = threading.Event()

        def _apply():
            try:
                self._automation_apply_task_context(item_id, task)
            finally:
                applied.set()

        self._safe_after(0, _apply)
        if not applied.wait(timeout=10):
            raise RuntimeError("切换自动化任务超时")

    def _automation_run_command_sync(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 180,
        respect_control: bool = True,
    ) -> str:
        if respect_control and not self._automation_wait_if_paused():
            raise RuntimeError("用户已停止自动化")
        display = self._cmd_display(cmd)
        self._safe_after(0, self._automation_log, f"$ {display}")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise RuntimeError(f"无法执行命令：{display}：{exc}") from exc

        output_lines = []
        started = time.monotonic()
        selector = selectors.DefaultSelector()
        if proc.stdout is not None:
            selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            while proc.poll() is None:
                if respect_control and self._automation_stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise RuntimeError("用户已停止自动化")
                if timeout and time.monotonic() - started >= timeout:
                    proc.kill()
                    proc.wait()
                    raise RuntimeError(f"命令超时：{display}")
                for key, _mask in selector.select(timeout=0.5):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    clean = line.rstrip()
                    output_lines.append(clean)
                    self._safe_after(0, self._automation_log, f"ADB | {clean}")
            if proc.stdout is not None:
                for line in proc.stdout:
                    clean = line.rstrip()
                    output_lines.append(clean)
                    self._safe_after(0, self._automation_log, f"ADB | {clean}")
        finally:
            selector.close()

        output = "\n".join(output_lines).strip()
        if proc.returncode != 0:
            raise RuntimeError(
                f"命令失败 (exit={proc.returncode})：{output or display}"
            )
        return output

    def _automation_extract_logcat_fields(
        self,
        seen_lines: set[str] | None = None,
        uid: str | None = None,
    ):
        seen_lines = seen_lines if seen_lines is not None else set()

        def _line(line: str):
            clean = line.strip()
            if not clean or clean in seen_lines:
                return
            seen_lines.add(clean)
            self._safe_after(0, self._automation_log, f"ADB | {clean}")

        uid = uid or self._cached_uid or self.uid_var.get().strip()
        return extract_logcat_fields(uid=uid or None, on_line=_line)

    def _automation_stop_active_logcat(self, reason: str = ""):
        """Stop any page-level logcat process before switching package/UID."""
        proc = self._logcat_proc
        if proc is None:
            return
        stop_logcat_stream(proc)
        self._logcat_proc = None
        self._logcat_thread = None
        previous_pattern = self._active_pattern
        self._active_pattern = None
        # ``_safe_after`` forwards positional arguments only.  Use a closure
        # here so this cleanup is also safe when called from the batch worker.
        self._safe_after(
            0,
            lambda: self.monitor_label.config(text="当前未监听", foreground="gray"),
        )
        self._safe_after(
            0,
            self._automation_log,
            f"已停止旧 UID 监听{f'（{reason}）' if reason else ''}: {previous_pattern or 'logcat'}",
        )

    def _write_automation_task_config(
        self,
        package_name: str,
        appid: str,
    ) -> str:
        """Write one automation task into the shared manual ADB config."""
        package_name = str(package_name or "").strip()
        appid = str(appid or "").strip()
        if not package_name or not appid:
            raise ValueError("当前任务缺少包名或 UP2 appid")

        config_path = self.config_path_var.get().strip()
        if not os.path.isfile(config_path):
            raise ValueError(f"配置文件不存在: {config_path}")
        with open(config_path, "r", encoding="utf-8") as file:
            config = json.load(file)
        if not isinstance(config, dict):
            raise ValueError("配置文件格式错误：根节点必须是对象")
        if not isinstance(config.get("data"), list) or not config["data"]:
            config["data"] = [{}]
        if not isinstance(config["data"][0], dict):
            raise ValueError("配置文件格式错误：data[0] 必须是对象")
        config["data"][0].update(
            {
                "packageName": package_name,
                "appId": appid,
                "taskUUID": AUTOMATION_AGGREGATION_TASK_UUID,
            }
        )
        with open(config_path, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=2, ensure_ascii=False)
        return config_path

    def _automation_prepare_detection_sync(self) -> dict:
        """Run the independent ADB preparation pipeline for the current task."""
        self._automation_stop_active_logcat("切换包体前")
        # Do not let a previous task's UID be used as a fallback while B is
        # being prepared.  The UID is populated again only after B is installed
        # and queried with get_app_uid().
        self._cached_uid = None
        self._safe_after(0, self.uid_var.set, "")
        package_name = self.automation_package_var.get().strip()
        appid = self.automation_appid_var.get().strip()
        config_path = self.config_path_var.get().strip()
        work_dir = self.work_dir_var.get().strip()
        if not package_name or not appid:
            raise ValueError("当前任务缺少包名或 UP2 appid")
        if not check_device():
            raise RuntimeError("没有已连接的 ADB 设备")
        if not os.path.isfile(config_path):
            raise ValueError(f"配置文件不存在: {config_path}")
        script_path = os.path.join(work_dir, "zygote_build.sh")
        if not os.path.isfile(script_path):
            raise ValueError(f"构建脚本不存在: {script_path}")

        self._safe_after(0, self._automation_log, "[ADB 1/6] 写入当前包体配置")
        self._write_automation_task_config(package_name, appid)
        # The automation tab and the manual ADB tab intentionally keep
        # separate widgets, but they operate on the same config.json.  Refresh
        # the ADB tab after automation writes the current task so opening that
        # tab never shows package A while package B is being detected.
        self._safe_after(
            0,
            self._sync_adb_tab_from_automation,
            package_name,
            appid,
            AUTOMATION_AGGREGATION_TASK_UUID,
        )
        self._safe_after(
            0,
            self._automation_log,
            (
                f"ADB | packageName={package_name} appId={appid} "
                f"taskUUID={AUTOMATION_AGGREGATION_TASK_UUID}"
            ),
        )

        self._safe_after(0, self._automation_log, "[ADB 2/6] 推送 config.json")
        self._automation_run_command_sync(build_push_config_cmd(config_path), timeout=45)
        if self._automation_stop_event.is_set():
            raise RuntimeError("用户已停止自动化")

        self._safe_after(0, self._automation_log, "[ADB 3/6] 执行 zygote_build")
        self._automation_run_command_sync(
            build_zygote_build_cmd(work_dir), cwd=work_dir, timeout=180
        )
        if self._automation_stop_event.is_set():
            raise RuntimeError("用户已停止自动化")

        self._safe_after(0, self._automation_log, "[ADB 4/6] 获取应用 UID")
        ok, uid = get_app_uid(package_name)
        if not ok:
            raise RuntimeError(uid)
        self._cached_uid = uid
        self._safe_after(0, self._set_uid, uid)
        self._safe_after(0, self._automation_log, f"ADB | UID={uid}")

        self._safe_after(0, self._automation_log, "[ADB 5/6] 清理旧日志并启动应用")
        clear_logcat_buffer()
        self._automation_run_command_sync(build_open_app_cmd(package_name), timeout=20)
        runtime_monitor = PackageRuntimeMonitor(package_name)

        self._safe_after(
            0,
            self._automation_log,
            "[ADB 6/6] 监听 ZGSDK.AutoDetector 并提取聚合参数",
        )
        seen_lines: set[str] = set()
        latest_fields: dict = {}
        last_good_fields: dict = {}
        last_logcat_error = ""
        initial_started = time.monotonic()
        initial_hard_deadline = initial_started + 90
        minimum_wait_seconds = 60
        quiet_window_seconds = 25
        last_log_signature = ""
        last_log_change = initial_started
        next_status_second = 90
        last_dialog_check = -3.0
        notification_dialog_dismissed = False
        while time.monotonic() < initial_hard_deadline:
            if self._automation_stop_event.is_set():
                raise RuntimeError("用户已停止自动化")
            if not self._automation_wait_if_paused():
                raise RuntimeError("用户已停止自动化")
            elapsed = time.monotonic() - initial_started
            if (
                not notification_dialog_dismissed
                and elapsed - last_dialog_check >= 3.0
            ):
                last_dialog_check = elapsed
                dialog_result = dismiss_notification_permission_dialog()
                if dialog_result.get("dismissed"):
                    notification_dialog_dismissed = True
                    self._safe_after(
                        0,
                        self._automation_log,
                        f"ADB | {dialog_result.get('message')}",
                    )
            snapshot = self._automation_extract_logcat_fields(seen_lines, uid=uid)
            runtime = runtime_monitor.poll()
            if not runtime.get("ok", True):
                summary = str(runtime.get("summary") or "").strip()
                message = runtime.get("message", "应用在检测过程中异常退出")
                if summary:
                    self._safe_after(0, self._automation_log, f"ADB | 崩溃证据: {summary}")
                return {
                    "ok": False,
                    "error": message,
                    "_runtime_code": runtime.get(
                        "code", "APP_EXITED_DURING_AUTOMATION"
                    ),
                    "_runtime_summary": summary,
                }
            if not snapshot.get("ok", True):
                if snapshot.get("_transient"):
                    error = str(snapshot.get("error") or "Logcat 暂时无法读取")
                    if error != last_logcat_error:
                        last_logcat_error = error
                        self._safe_after(
                            0,
                            self._automation_log,
                            f"ADB | {error}，设备状态={snapshot.get('_adb_state', 'unknown')}，继续等待",
                        )
                    time.sleep(
                        min(2, max(0.1, initial_hard_deadline - time.monotonic()))
                    )
                    latest_fields = snapshot
                    continue
                return snapshot
            latest_fields = snapshot
            last_good_fields = snapshot
            issue = detection_field_issue(latest_fields)
            if issue is None or (
                issue is not None
                and issue[0] == "UNSUPPORTED_ATTRIBUTION"
                and has_explicit_attribution(latest_fields)
            ):
                return latest_fields
            now = time.monotonic()
            signature = str(latest_fields.get("完整日志") or "")
            if signature and signature != last_log_signature:
                last_log_signature = signature
                last_log_change = now
            elapsed = now - initial_started
            # Do not stop at the old 45-second boundary.  A normal detector can
            # emit its final verdict just after 45 seconds.  Allow at least 60
            # seconds, then stop only after a 25-second quiet period, with a
            # hard cap of 90 seconds.
            if (
                elapsed >= minimum_wait_seconds
                and now - last_log_change >= quiet_window_seconds
            ):
                break
            remaining = max(0, int(initial_hard_deadline - now))
            if remaining <= next_status_second:
                self._safe_after(
                    0,
                    self._automation_log,
                    f"ADB | 等待检测日志，剩余约 {remaining} 秒",
                )
                next_status_second -= 10
            time.sleep(min(5, max(0.1, initial_hard_deadline - time.monotonic())))
        return last_good_fields or latest_fields or self._automation_extract_logcat_fields(
            seen_lines, uid=uid
        )

    def _sync_adb_tab_from_automation(
        self, package_name: str, appid: str, task_uuid: str
    ) -> None:
        """Show the latest automation config in the existing manual ADB tab."""
        self.pkg_entry.delete(0, tk.END)
        self.pkg_entry.insert(0, package_name)
        self.appid_entry.delete(0, tk.END)
        self.appid_entry.insert(0, appid)
        if task_uuid in self.task_uuid_combo["values"]:
            self.task_uuid_var.set(task_uuid)
        else:
            self.task_uuid_var.set(AUTOMATION_AGGREGATION_TASK_UUID)
        self.status_label.config(text="ADB 指令页已同步当前聚合检测配置")

    def _automation_process_current_task_sync(self) -> bool:
        context_version = self._automation_context_version
        package_name = self.automation_package_var.get().strip()
        initial_fields = self._automation_prepare_detection_sync()
        runtime_monitor = PackageRuntimeMonitor(package_name)
        detection = detect_aggregation_with_one_retry(
            package_name,
            lambda: self._automation_extract_logcat_fields(),
            first_fields=initial_fields,
            stop_event=self._automation_stop_event,
            on_progress=lambda text: self._safe_after(0, self._automation_log, text),
            runtime_check=runtime_monitor.poll,
            runtime_reset=runtime_monitor.reset,
        )
        detection = reconcile_detection_result(detection)
        if context_version != self._automation_context_version:
            self._safe_after(
                0,
                self._automation_log,
                "已丢弃上一应用延迟返回的聚合参数",
            )
            return False
        self._automation_fields = detection.get("fields") or {}
        if self._automation_fields:
            self._safe_after(
                0,
                self._automation_render_fields,
                self._automation_fields,
                context_version,
            )
        if not detection.get("ok"):
            message = detection.get("message", "聚合参数提取失败")
            failure_code = detection.get(
                "code", "AGGREGATION_DETECTION_FAILED"
            )
            if failure_code in AUTOMATION_INFRASTRUCTURE_FAILURE_CODES:
                self._safe_after(
                    0,
                    self._automation_log,
                    f"ADB | {message}；批量队列安全停止，不写入 Asana 业务结论",
                )
                self._safe_after(
                    0,
                    self._automation_set_status,
                    "ADB/Logcat 异常，队列已停止",
                    "#e53935",
                )
                self._automation_stop_event.set()
                return False
            if detection.get("code") == "UNSUPPORTED_ATTRIBUTION":
                return self._automation_complete_unsupported_attribution_sync(
                    message
                )
            else:
                self._automation_persist_detected_failure_fields_sync(
                    failure_code, message
                )
                self._automation_mark_failed(message)
            runtime_summary = str(
                (detection.get("runtime") or {}).get("summary")
                or (detection.get("fields") or {}).get("_runtime_summary")
                or ""
            ).strip()
            self._automation_comment_failure(
                failure_code,
                f"{message}\n包名：{package_name}"
                + (f"\n关键崩溃日志：\n{runtime_summary}" if runtime_summary else ""),
            )
            return False

        if not self._automation_wait_if_paused():
            return False
        self._automation_prepare_replay_id_candidates()
        self._safe_after(0, self._automation_log, "[1/3] 回填 Asana 描述")
        self._automation_fill_asana_sync()
        if self._automation_stop_event.is_set():
            return False
        if not self._automation_wait_if_paused():
            return False
        self._safe_after(0, self._automation_log, "[2/3] 接口提交适配后台")
        submit = self._automation_submit_backend_sync()
        self._safe_after(0, self._automation_log, submit.get("message", ""))
        if self._automation_stop_event.is_set():
            self._safe_after(
                0,
                self._automation_log,
                "已停止：后台步骤结束后不再进入聚合回放",
            )
            self._safe_after(0, self._automation_set_status, "自动化已停止", "#ef6c00")
            return False
        if not submit.get("ok"):
            message = submit.get("message", "后台自动提交失败")
            self._automation_mark_failed(message)
            self._automation_comment_failure(
                submit.get("code", "BACKEND_SUBMIT_FAILED"), message
            )
            return False
        if self._automation_stop_event.is_set():
            return False
        if not self._automation_wait_if_paused():
            return False
        self._safe_after(0, self._automation_log, "[3/3] 重启应用并检测聚合回放")
        try:
            replay = self._automation_replay_with_id_rotation_sync()
        except Exception as exc:
            if self._automation_fields.get("_aggregation_type_inferred"):
                return self._automation_handle_inferred_replay_failure_sync(
                    {
                        "ok": False,
                        "code": "REPLAY_EXCEPTION",
                        "message": f"聚合回放检测异常：{exc}",
                    }
                )
            raise
        if replay.get("code") == "AGGREGATION_TYPE_CHANGED_DURING_REPLAY":
            return self._automation_handle_replay_type_change_sync(replay)
        if replay.get("ok"):
            self._automation_comment_success(replay)
            self._safe_after(
                0, self._automation_log, replay.get("message", "聚合广告回放成功")
            )
            self._safe_after(0, self._automation_set_status, "聚合适配成功", "#2e7d32")
            if self._automation_precheck_item_id:
                self._safe_after(
                    0,
                    self._set_precheck_task_status,
                    self._automation_precheck_item_id,
                    "聚合适配成功",
                )
            return True

        if self._automation_fields.get("_aggregation_type_inferred"):
            return self._automation_handle_inferred_replay_failure_sync(replay)

        message = replay.get("message", "聚合广告回放失败")
        self._automation_mark_failed(message)
        if replay.get("code") in {
            "REPLAY_TIMEOUT",
            "REPLAY_ID_CANDIDATES_EXHAUSTED",
        }:
            comment = build_replay_failure_comment(package_name, replay)
            comment = "\n".join(comment.splitlines()[1:])
            code = "AD_REPLAY_FAILED"
        else:
            comment = (
                "聚合广告回放失败，自动化适配终止，需要测试人员确认\n"
                f"包名：{package_name}\n失败原因：{message}"
            )
            code = replay.get("code", "AD_REPLAY_FAILED")
        self._automation_comment_failure(code, comment)
        return False

    def _automation_run_eligible_batch(self):
        if self._automation_running:
            return
        queue = self._automation_eligible_precheck_tasks()
        if not queue:
            self._automation_set_status("预检列表中没有可自动适配的已安装任务", "#e53935")
            self._automation_log(
                "批量队列为空：仅处理状态为启动正常、安装完成或已安装的任务"
            )
            return
        self._automation_batch_active = True
        self._automation_set_running(True)
        self._automation_set_status(f"批量自动适配 0/{len(queue)}", "#ef6c00")

        def _run():
            succeeded = 0
            failed = 0
            try:
                for position, (item_id, task) in enumerate(queue, start=1):
                    if self._automation_stop_event.is_set():
                        break
                    if not self._automation_wait_if_paused():
                        break
                    self._automation_switch_task_sync(item_id, task)
                    package_name = str(getattr(task, "package_name", "") or "")
                    self._safe_after(
                        0,
                        self._automation_set_status,
                        f"批量自动适配 {position}/{len(queue)}",
                        "#ef6c00",
                    )
                    self._safe_after(
                        0,
                        self._automation_log,
                        f"========== [{position}/{len(queue)}] {package_name} ==========",
                    )
                    try:
                        if self._automation_process_current_task_sync():
                            succeeded += 1
                        else:
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        message = f"自动化执行失败: {exc}"
                        self._automation_mark_failed(message)
                        self._automation_comment_failure("AUTOMATION_FAILED", message)
                    finally:
                        self._automation_stop_active_logcat("当前包体已完成")
                        try:
                            self._automation_run_command_sync(
                                build_force_stop_cmd(package_name),
                                timeout=15,
                                respect_control=False,
                            )
                        except Exception as exc:
                            self._safe_after(
                                0,
                                self._automation_log,
                                f"停止当前应用失败: {exc}",
                            )
                stopped = self._automation_stop_event.is_set()
                summary = (
                    f"批量自动适配已停止：成功 {succeeded}，失败 {failed}"
                    if stopped
                    else f"批量自动适配完成：成功 {succeeded}，失败 {failed}"
                )
                self._safe_after(0, self._automation_log, summary)
                self._safe_after(
                    0,
                    self._automation_set_status,
                    summary,
                    "#ef6c00" if stopped or failed else "#2e7d32",
                )
            finally:
                self._safe_after(0, self._automation_set_running, False)

        threading.Thread(target=_run, daemon=True).start()

    def _automation_wait_if_paused(self) -> bool:
        """Pause the batch at a safe boundary; return False when stopped."""
        logged = False
        while self._automation_pause_event.is_set():
            if self._automation_stop_event.is_set():
                return False
            if not logged:
                self._safe_after(0, self._automation_log, "批量队列已暂停，等待继续")
                logged = True
            time.sleep(0.2)
        return not self._automation_stop_event.is_set()

    def _automation_toggle_pause(self):
        if not self._automation_running or not self._automation_batch_active:
            return
        if self._automation_pause_event.is_set():
            self._automation_pause_event.clear()
            self._automation_pause_btn.configure(text="暂停队列")
            self._automation_set_status("批量队列继续执行", "#ef6c00")
            self._automation_log("已继续批量自动适配")
        else:
            self._automation_pause_event.set()
            self._automation_pause_btn.configure(text="继续队列")
            self._automation_set_status("将在当前安全步骤后暂停", "#ef6c00")
            self._automation_log("已请求暂停，不会切换到下一包")

    def _automation_stop(self):
        self._automation_stop_event.set()
        self._automation_pause_event.clear()
        self._automation_set_status("正在停止...", "#ef6c00")
        self._automation_log("已请求停止，将在当前步骤安全结束后停止")

    # ── 自动化脚本事件 ────────────────────────────────────────────

    def _on_normalize_action_delays(self):
        raw = self.action_script_text.get("1.0", tk.END).strip()
        try:
            following_delay_seconds = float(self.following_action_delay_var.get().strip())
        except ValueError:
            msg = "其余动作 delay 必须是数字"
            self.action_script_status.config(text=msg, foreground="#ef5350")
            self.status_label.config(text=msg)
            return
        if following_delay_seconds <= 0:
            msg = "其余动作 delay 必须大于 0"
            self.action_script_status.config(text=msg, foreground="#ef5350")
            self.status_label.config(text=msg)
            return

        following_delay_ms = int(round(following_delay_seconds * 1000))
        try:
            normalized, stats = normalize_action_script_text(
                raw,
                min_delay_ms=following_delay_ms,
            )
        except json.JSONDecodeError as e:
            msg = f"JSON 格式错误: 第 {e.lineno} 行第 {e.colno} 列"
            self.action_script_status.config(text=msg, foreground="#ef5350")
            self.status_label.config(text=msg)
            return
        except Exception as e:
            msg = f"调整失败: {e}"
            self.action_script_status.config(text=msg, foreground="#ef5350")
            self.status_label.config(text=msg)
            return

        self.action_script_text.delete("1.0", tk.END)
        self.action_script_text.insert("1.0", normalized)
        msg = (
            f"已调整 {stats['updated_count']} / {stats['delay_count']} 个 delay "
            f"(首个 >= {stats['first_delay_ms']}ms，其余 >= {stats['min_delay_ms']}ms)"
        )
        self.action_script_status.config(text=msg, foreground="#81c784")
        self.status_label.config(text=msg)

    def _on_copy_action_script(self):
        text = self.action_script_text.get("1.0", tk.END).strip()
        if not text:
            self.action_script_status.config(text="没有可复制的脚本", foreground="#ef5350")
            self.status_label.config(text="没有可复制的脚本")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.action_script_status.config(text="已复制到剪贴板", foreground="#81c784")
        self.status_label.config(text="脚本已复制到剪贴板")

    def _on_clear_action_script(self):
        self.action_script_text.delete("1.0", tk.END)
        self.action_script_status.config(text="已清空", foreground="gray")
        self.status_label.config(text="自动化脚本已清空")

    # ── 控制台输出 ────────────────────────────────────────────────

    def _console_cmd(self, text: str):
        """输出命令提示符"""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, f"$ {text}\n", "cmd")
        self._console_line_count += 1
        self._trim_console_if_needed()
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _console_line(self, text: str, tag="logline"):
        """实时追加一行"""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, text + "\n", tag)
        self._console_line_count += 1
        self._trim_console_if_needed()
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _console_done(self, returncode: int):
        tag = "done" if returncode == 0 else "error"
        msg = f"[完成, exit={returncode}]\n"
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, msg, tag)
        self._console_line_count += 1
        self._trim_console_if_needed()
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _on_clear_output(self):
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self._console_line_count = 0
        self.output_text.configure(state=tk.DISABLED)

    def _trim_console_if_needed(self):
        """限制控制台文本体积，避免长时间 logcat 后 Tk Text 变慢。"""
        if self._console_line_count <= self._console_max_lines:
            return
        try:
            total_lines = int(self.output_text.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return
        if total_lines <= self._console_max_lines:
            self._console_line_count = total_lines
            return
        delete_to = max(1, total_lines - self._console_max_lines + 1)
        self.output_text.delete("1.0", f"{delete_to}.0")
        self._console_line_count = self._console_max_lines

    # ── 通用命令执行 ──────────────────────────────────────────────

    def _set_buttons_state(self, enabled: bool, token: int | None = None,
                           auto_restore_ms: int = 45000):
        if token is not None and token != self._command_state_token:
            return
        self._running_command = not enabled
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in self._op_buttons:
            btn.configure(state=state)
        self._set_stop_command_state(not enabled and self._has_running_command_proc())

        if self._button_restore_after_id:
            try:
                self.root.after_cancel(self._button_restore_after_id)
            except tk.TclError:
                pass
            finally:
                self._after_ids.discard(self._button_restore_after_id)
                self._button_restore_after_id = None

        if not enabled:
            self._command_state_token += 1
            token = self._command_state_token
            self._button_restore_after_id = self._safe_after(
                auto_restore_ms,
                lambda t=token: self._set_buttons_state(True, token=t),
            )
        return self._command_state_token

    def _has_running_command_proc(self) -> bool:
        proc = self._current_command_proc
        return proc is not None and proc.poll() is None

    def _set_stop_command_state(self, enabled: bool):
        stop_btn = getattr(self, "_stop_command_btn", None)
        if stop_btn is not None:
            stop_btn.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _cmd_display(self, cmd: list[str]) -> str:
        """将命令转为可读显示，adb 用简写"""
        result = " ".join(cmd)
        adb_path = get_adb_path()
        if adb_path:
            result = result.replace(adb_path, "adb")
        return result

    def _run_command(
        self,
        cmd: list[str],
        cwd=None,
        timeout: int = 45,
        disable_buttons: bool = True,
    ):
        """在控制台中显示命令并流式执行"""
        self._console_cmd(self._cmd_display(cmd))
        token = None
        if disable_buttons:
            token = self._set_buttons_state(
                False,
                auto_restore_ms=(timeout + 5) * 1000 if timeout else 60000,
            )
        self.status_label.config(text="执行中...")
        self.root.update_idletasks()

        def on_line(line: str):
            self._safe_after(0, self._console_line, line)

        cmd_proc: dict[str, subprocess.Popen | None] = {"proc": None}

        def on_proc(proc: subprocess.Popen):
            cmd_proc["proc"] = proc
            self._current_command_proc = proc
            self._safe_after(0, self._set_stop_command_state, True)

        def on_done(returncode: int):
            self._safe_after(0, self._console_done, returncode)
            if self._current_command_proc is cmd_proc["proc"]:
                self._current_command_proc = None
            if disable_buttons:
                self._safe_after(0, self._set_buttons_state, True, token)
            else:
                self._safe_after(0, self._set_stop_command_state, False)
            self._safe_after(0, lambda: self.status_label.config(
                text="执行成功" if returncode == 0 else f"执行失败 (exit={returncode})"
            ))

        run_stream(cmd, on_line, on_done, cwd=cwd, timeout=timeout, on_proc=on_proc)

    def _on_stop_current_command(self):
        proc = self._current_command_proc
        if proc is None or proc.poll() is not None:
            self._console_line("[停止] 当前没有正在执行的命令", "done")
            self._set_stop_command_state(False)
            return

        self._console_line("[停止] 正在终止当前命令...", "cmd")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            self._console_line("[停止] 已终止当前命令", "done")
        except Exception as e:
            self._console_line(f"[停止失败] {e}", "error")
        finally:
            self._current_command_proc = None
            self._set_buttons_state(True)
            self.status_label.config(text="已停止当前命令")

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

    def _on_parse_fill_url(self):
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.config(text="请输入要解析的 URL")
            return
        data = parse_fill_url(url)
        if not data:
            self.status_label.config(text="URL 中没有可回填的包名、AppId 或 GP 链接")
            return
        self._apply_fill_data(data)

    def _on_push_apk(self):
        path = self.url_entry.get().strip()
        if not path:
            self.status_label.config(text="请输入 APK 路径或 Google Play 地址")
            return

        if path.startswith("http://") or path.startswith("https://"):
            if is_apk_download_url(path):
                self._start_download_install(path)
                return
            self._open_url_on_device(path)
            return

        if not os.path.isfile(path) and not os.path.isdir(path):
            self.status_label.config(text="文件或目录不存在，请检查路径")
            return

        self.status_label.config(text="正在检查设备...")
        self.root.update_idletasks()

        def _run_install():
            if not check_device():
                self._safe_after(0, lambda: self.status_label.config(text="没有已连接的设备"))
                return
            self._safe_after(0, lambda: self.status_label.config(text="正在安装..."))
            ok, msg = push_apk(path)
            self._safe_after(0, lambda: self.status_label.config(text=msg))
            if ok:
                self._safe_after(0, lambda: self._console_line(f"[安装成功] {msg}", "done"))
            else:
                self._safe_after(0, lambda: self._console_line(f"[安装失败] {msg}", "error"))

        threading.Thread(target=_run_install, daemon=True).start()

    def _start_download_install(self, url: str):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        self._set_buttons_state(False)
        self.status_label.config(text="正在下载...")
        self.root.update_idletasks()
        threading.Thread(
            target=self._do_apkcombo_download, args=(url,), daemon=True
        ).start()

    def _open_url_on_device(self, url: str):
        adb = get_adb_path()
        if not adb:
            self.status_label.config(text="未找到 ADB 工具，请点击「设置ADB」指定路径")
            return
        self.status_label.config(text="正在打开手机上的应用页面...")
        self.root.update_idletasks()

        def _run_open():
            pkg = extract_google_play_package(url)
            if pkg:
                cmd = [
                    adb, "shell", "am", "start",
                    "-a", "android.intent.action.VIEW",
                    "-d", f"market://details?id={pkg}",
                    "-p", "com.android.vending",
                ]
            else:
                cmd = [
                    adb, "shell", "am", "start",
                    "-a", "android.intent.action.VIEW",
                    "-d", url,
                ]
            try:
                subprocess.run(
                    cmd,
                    capture_output=True, text=True, timeout=15
                )
                self._safe_after(0, lambda: self.status_label.config(
                    text="已在手机上打开页面，请在手机上完成下载安装"
                ))
            except subprocess.TimeoutExpired:
                self._safe_after(0, lambda: self.status_label.config(text="操作超时，请重试"))

        threading.Thread(target=_run_open, daemon=True).start()

    def _on_browse(self):
        path = filedialog.askopenfilename(
            title="选择 APK 文件",
            filetypes=[("APK/XAPK files", "*.apk *.xapk"), ("APK files", "*.apk"), ("XAPK files", "*.xapk"), ("All files", "*.*")]
        )
        if path:
            self._set_install_path(path)

    def _on_browse_apk_dir(self):
        path = filedialog.askdirectory(title="选择 APK 拆分目录")
        if path:
            self._set_install_path(path)

    def _set_install_path(self, path: str):
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

        if os.path.isfile(url) or os.path.isdir(url):
            self._on_push_apk()
            return

        # 如果是直链(.apk/.xapk) → 直接下载安装
        if url.startswith("http://") or url.startswith("https://"):
            apkcombo_url = build_apkcombo_search_url(url)
            if apkcombo_url and not is_apk_download_url(url):
                import webbrowser
                webbrowser.open(apkcombo_url)
                self.status_label.config(
                    text="已打开 APKCombo 搜索结果页"
                )
                return
            self._start_download_install(url)
            return

        self.status_label.config(text="无法识别链接格式，请粘贴本地 APK/XAPK 路径、下载直链或 Google Play 地址")

    def _do_apkcombo_download(self, url):
        ok, msg = download_and_install(
            url,
            on_progress=lambda pct, text: self._safe_after(
                0, lambda: self.status_label.config(text=text)
            )
        )
        self._safe_after(0, lambda: self.status_label.config(
            text=f"安装{'成功' if ok else '失败'}: {msg}"
        ))
        self._safe_after(0, lambda: self._set_buttons_state(True))
        if ok:
            self._safe_after(0, lambda: self._console_line(f"[安装成功] {msg}", ""))

    def _on_apkpure_search(self):
        pkg = self.apkpure_pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        self.status_label.config(text="正在 APKPure 中搜索...")
        self.root.update_idletasks()

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

    def _remember_path_setting(self, key: str, value: str):
        self._settings[key] = value
        try:
            save_gui_settings(self._settings)
        except OSError as e:
            self.status_label.config(text=f"路径记忆保存失败: {e}")

    def _on_fill_parent_task_gid_from_url(self):
        try:
            task_gid = extract_asana_task_gid(self.parent_task_url_var.get())
        except ValueError as exc:
            self.parent_task_url_status_label.config(
                text=str(exc), foreground="#ef5350"
            )
            return

        self.parent_task_gid_var.set(task_gid)
        # Persist immediately so the next sync uses the new parent even if the
        # user clicks the sync action before the normal debounced save fires.
        self._save_sync_settings()
        self.parent_task_url_status_label.config(
            text=f"已填充并保存父任务 GID：{task_gid}", foreground="#2e7d32"
        )

    def _remember_cleanup_keep_packages(self):
        self._settings["cleanup_keep_packages"] = self._cleanup_keep_packages_text()
        try:
            save_gui_settings(self._settings)
        except OSError as e:
            self.status_label.config(text=f"清理白名单保存失败: {e}")

    def _sync_settings_map(self) -> dict[str, tk.StringVar]:
        return {
            "sync_sheet_id": self.sheet_id_var,
            "sync_sheet_name": self.sheet_name_var,
            "sync_project_gid": self.project_gid_var,
            "sync_asana_pat": self.asana_pat_var,
            "sync_sa_file": self.sa_file_var,
            "sync_proxy_url": self.proxy_url_var,
            "sync_parent_task_gid": self.parent_task_gid_var,
            "sync_cp_adapt_api_url": self.cp_adapt_api_url_var,
            "sync_cp_adapt_x_token": self.cp_adapt_x_token_var,
            "sync_cp_adapt_token": self.cp_adapt_token_var,
            "sync_cp_adapt_assign": self.cp_adapt_assign_var,
        }

    def _setup_sync_settings_memory(self):
        for var in self._sync_settings_map().values():
            var.trace_add("write", lambda *_args: self._schedule_sync_settings_save())

    def _schedule_sync_settings_save(self):
        if self._sync_settings_after_id:
            try:
                self.root.after_cancel(self._sync_settings_after_id)
            except tk.TclError:
                pass
        self._sync_settings_after_id = self._safe_after(300, self._save_sync_settings)

    def _save_sync_settings(self):
        self._sync_settings_after_id = None
        for key, var in self._sync_settings_map().items():
            self._settings[key] = var.get()
        try:
            save_gui_settings(self._settings)
        except OSError as e:
            if hasattr(self, "status_label"):
                self.status_label.config(text=f"同步配置记忆保存失败: {e}")

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
            self._remember_path_setting("config_path", path)

    def _on_browse_work_dir(self):
        path = filedialog.askdirectory(title="选择工作目录 (包含 zygote_build.sh)")
        if path:
            self.work_dir_var.set(path)
            self._remember_path_setting("work_dir", path)

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
        self._remember_path_setting("work_dir", work_dir)
        # 尝试自动读取该目录下的 config.json
        cfg = os.path.join(work_dir, "config.json")
        if os.path.isfile(cfg):
            self.config_path_var.set(cfg)
            self._remember_path_setting("config_path", cfg)

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

    def _on_fix_zygotehole_permissions(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        cmd = build_fix_zygotehole_permissions_cmd()
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
        self._run_command(cmd, cwd=work_dir, timeout=180)

    def _on_get_uid(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return

        token = self._set_buttons_state(False, auto_restore_ms=50000)
        self._console_cmd(f"查询 UID: {pkg}")
        self.status_label.config(text="查询 UID...")
        self.root.update_idletasks()

        def _run():
            ok, msg = get_app_uid(pkg)

            def _finish():
                if ok:
                    self._set_uid(msg)
                    self._console_line(f"[UID] {pkg}: {msg}", "done")
                    self.status_label.config(text=f"UID: {msg}")
                else:
                    self._console_line(f"[UID 查询失败] {msg}", "error")
                    self.status_label.config(text=msg)
                self._set_buttons_state(True, token)

            # Normal execution runs in a worker and must marshal back to Tk.
            # Synchronous thread adapters (used by embedders/tests) already run
            # on Tk's main thread, where deferring would leave the 50s safety
            # timer pending unnecessarily.
            if threading.current_thread() is threading.main_thread():
                _finish()
            else:
                self._safe_after(0, _finish)

        threading.Thread(target=_run, daemon=True).start()

    def _set_uid(self, uid: str):
        self._cached_uid = uid
        self.uid_var.set(uid)
        self.uid_label.configure(foreground="black")
        self.status_label.config(text=f"UID: {uid}")

    def _set_cleanup_preview(self, lines: list[tuple[str, str]]):
        preview_text = getattr(self, "cleanup_preview_text", None)
        if preview_text is None:
            return
        preview_text.configure(state=tk.NORMAL)
        preview_text.delete("1.0", tk.END)
        for text, tag in lines:
            preview_text.insert(tk.END, text + "\n", tag)
        preview_text.configure(state=tk.DISABLED)

    def _render_cleanup_preview(self, installed, keep, targets):
        self._reset_cleanup_progress()
        keep_set = sorted(set(keep))
        target_count = len(targets)
        self.cleanup_preview_var.set(
            f"预览完成：第三方包 {len(installed)} 个，保留 {len(keep_set)} 个，将卸载 {target_count} 个"
        )

        lines: list[tuple[str, str]] = []
        if target_count == 0:
            lines.append(("没有需要卸载的第三方包", "done"))
        else:
            lines.append(("将卸载:", "target"))
            lines.extend((pkg, "target") for pkg in targets)

        if keep_set:
            lines.append(("", ""))
            lines.append(("保留:", "keep"))
            lines.extend((pkg, "keep") for pkg in keep_set)

        self._set_cleanup_preview(lines)

    def _reset_cleanup_progress(self, total: int = 0):
        progress = getattr(self, "cleanup_progress", None)
        if progress is not None:
            progress.configure(maximum=max(1, total))
        self.cleanup_progress_var.set(0)
        self.cleanup_progress_text_var.set(
            f"卸载进度：0/{total}" if total else "卸载进度：未开始"
        )

    def _update_cleanup_progress(
        self,
        completed: int,
        total: int,
        package_name: str,
        succeeded: int,
        failed: int,
    ):
        self.cleanup_progress_var.set(completed)
        self.cleanup_progress_text_var.set(
            f"卸载进度：{completed}/{total}（成功 {succeeded}，失败 {failed}）"
        )
        self.cleanup_preview_var.set(
            f"正在卸载 {completed}/{total}：{package_name}"
        )

    def _run_cleanup_uninstall(self, targets: list[str], scan_token: int):
        # 扫描阶段的 90 秒安全恢复计时不适合大批量卸载，按包数量重新计时。
        self._set_buttons_state(True, scan_token)
        token = self._set_buttons_state(
            False,
            auto_restore_ms=max(120000, len(targets) * 35000),
        )
        self._reset_cleanup_progress(len(targets))
        self.status_label.config(text=f"正在卸载 0/{len(targets)}...")

        def _run():
            succeeded = 0
            failures: list[tuple[str, str]] = []
            for index, package_name in enumerate(targets, start=1):
                ok, message = uninstall_third_party_package(package_name)
                if ok:
                    succeeded += 1
                else:
                    failures.append((package_name, message))
                self._safe_after(
                    0,
                    self._console_line,
                    f"[卸载 {'成功' if ok else '失败'}] {package_name}"
                    + ("" if ok else f"：{message}"),
                    "done" if ok else "error",
                )
                self._safe_after(
                    0,
                    self._update_cleanup_progress,
                    index,
                    len(targets),
                    package_name,
                    succeeded,
                    len(failures),
                )

            def _finish():
                failed = len(failures)
                summary = (
                    f"卸载完成：成功 {succeeded} 个，失败 {failed} 个"
                )
                self.cleanup_preview_var.set(summary)
                self.cleanup_progress_text_var.set(
                    f"卸载完成：{len(targets)}/{len(targets)}（成功 {succeeded}，失败 {failed}）"
                )
                lines: list[tuple[str, str]] = [
                    (summary, "done" if not failures else "error")
                ]
                if failures:
                    lines.append(("", ""))
                    lines.append(("卸载失败:", "error"))
                    lines.extend(
                        (f"{package_name}: {message}", "error")
                        for package_name, message in failures
                    )
                self._set_cleanup_preview(lines)
                self._set_buttons_state(True, token)
                self.status_label.config(text=summary)

            self._safe_after(0, _finish)

        threading.Thread(target=_run, daemon=True).start()

    def _on_clear_cache(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_clear_cache_cmd(pkg)
        self._run_command(cmd, timeout=8, disable_buttons=False)

    def _on_force_stop(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_force_stop_cmd(pkg)
        self._run_command(cmd, timeout=8, disable_buttons=False)

    def _on_cancel_zygotehole_injection(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        if not messagebox.askyesno(
            "确认取消注入",
            f"将从设备注入配置中删除 {pkg}，保留其他游戏配置，并强制停止该游戏。是否继续？",
        ):
            self.status_label.config(text="已取消操作")
            return

        token = self._set_buttons_state(False, auto_restore_ms=35000)
        self._console_cmd(f"取消当前游戏注入: {pkg}")
        self.status_label.config(text="正在取消注入...")
        self.root.update_idletasks()

        def _run():
            ok, msg = cancel_zygotehole_injection(pkg)

            def _finish():
                self._console_line(
                    f"[取消注入] {msg}",
                    "done" if ok else "error",
                )
                self.status_label.config(text=msg)
                self._set_buttons_state(True, token)

            self._safe_after(0, _finish)

        threading.Thread(target=_run, daemon=True).start()

    def _on_open_app(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请输入包名")
            return
        cmd = build_open_app_cmd(pkg)
        self._run_command(cmd, timeout=10, disable_buttons=False)

    def _on_clear_play_store(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return
        cmd = build_clear_cache_cmd("com.android.vending")
        self._run_command(cmd, timeout=8, disable_buttons=False)

    def _cleanup_keep_packages(self) -> list[str]:
        raw = self._cleanup_keep_packages_text()
        packages = [
            item.strip()
            for item in re.split(r"[\s,，;；]+", raw)
            if item.strip()
        ]
        current_pkg = self.pkg_entry.get().strip()
        if current_pkg:
            packages.append(current_pkg)
        # The MAX Mediation Debugger share receiver is part of the automation
        # toolchain and must survive third-party package cleanup even when an
        # older saved keep-list predates the helper app.
        packages.append("com.apktool.sharereceiver")
        return sorted(set(packages))

    def _cleanup_keep_packages_text(self) -> str:
        entry = getattr(self, "cleanup_keep_entry", None)
        if entry is not None:
            return entry.get("1.0", "end-1c")
        return self.cleanup_keep_packages_var.get()

    def _on_cleanup_keep_text_changed(self, _event=None):
        if self._syncing_cleanup_keep_text:
            return
        self._syncing_cleanup_keep_text = True
        try:
            self.cleanup_keep_packages_var.set(self._cleanup_keep_packages_text())
        finally:
            self._syncing_cleanup_keep_text = False

    def _on_cleanup_keep_var_changed(self, *_args):
        if self._syncing_cleanup_keep_text:
            return
        entry = getattr(self, "cleanup_keep_entry", None)
        if entry is None:
            return
        value = self.cleanup_keep_packages_var.get()
        current = entry.get("1.0", "end-1c")
        if current == value:
            return
        self._syncing_cleanup_keep_text = True
        try:
            entry.delete("1.0", tk.END)
            entry.insert("1.0", value)
        finally:
            self._syncing_cleanup_keep_text = False

    def _load_third_party_cleanup_plan(self, on_done):
        keep = self._cleanup_keep_packages()
        self._remember_cleanup_keep_packages()
        self.status_label.config(text="正在扫描第三方应用...")
        self.cleanup_preview_var.set("正在扫描第三方应用...")
        self._set_cleanup_preview([("adb shell pm list packages -3", "keep")])
        self._console_cmd("adb shell pm list packages -3")
        token = self._set_buttons_state(False, auto_restore_ms=90000)

        def _run():
            try:
                installed = list_third_party_packages()
                targets = packages_to_uninstall(installed, keep)
                self._safe_after(0, on_done, installed, keep, targets, token)
            except Exception as e:
                self._safe_after(0, self._console_line, f"[扫描失败] {e}", "error")
                self._safe_after(0, lambda err=e: self.cleanup_preview_var.set(f"扫描失败: {err}"))
                self._safe_after(0, self._set_cleanup_preview, [(f"[扫描失败] {e}", "error")])
                self._safe_after(0, self._set_buttons_state, True, token)
                self._safe_after(0, lambda: self.status_label.config(text=f"扫描失败: {e}"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_preview_third_party_cleanup(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        def _show_preview(installed, keep, targets, token):
            self._render_cleanup_preview(installed, keep, targets)
            self._console_line(
                f"[清理预览] 第三方包 {len(installed)} 个，保留 {len(set(keep))} 个，将卸载 {len(targets)} 个",
                "done",
            )
            if keep:
                self._console_line("[保留] " + ", ".join(sorted(set(keep))), "logline")
            for pkg in targets:
                self._console_line(f"[将卸载] {pkg}", "logline")
            self._set_buttons_state(True, token)
            self.status_label.config(text=f"预览完成：将卸载 {len(targets)} 个第三方包")

        self._load_third_party_cleanup_plan(_show_preview)

    def _on_cleanup_third_party_packages(self):
        if not check_device():
            self.status_label.config(text="没有已连接的设备")
            return

        def _confirm_and_run(installed, keep, targets, token):
            self._render_cleanup_preview(installed, keep, targets)
            if not targets:
                self._console_line(
                    f"[清理] 第三方包 {len(installed)} 个，没有需要卸载的包",
                    "done",
                )
                self._set_buttons_state(True, token)
                self.status_label.config(text="没有需要清理的第三方包")
                return

            preview = "\n".join(targets[:30])
            if len(targets) > 30:
                preview += f"\n... 还有 {len(targets) - 30} 个"
            ok = messagebox.askyesno(
                "确认清理第三方包",
                (
                    f"将卸载 {len(targets)} 个第三方包，保留 {len(set(keep))} 个。\n\n"
                    f"{preview}\n\n确认继续？"
                ),
            )
            if not ok:
                self._console_line("[清理取消] 未卸载任何应用", "done")
                self._set_buttons_state(True, token)
                self.status_label.config(text="已取消第三方包清理")
                return

            self._console_line(
                f"[开始清理] 卸载 {len(targets)} 个第三方包；保留: {', '.join(sorted(set(keep))) or '(无)'}",
                "cmd",
            )
            self._run_cleanup_uninstall(targets, token)

        self._load_third_party_cleanup_plan(_confirm_and_run)

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
                        self._safe_after(0, self._console_line, line.rstrip())
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

        token = self._set_buttons_state(False, auto_restore_ms=50000)
        self.status_label.config(text="正在提取聚合参数...")
        self._console_cmd("adb logcat -d | grep ZGSDK.AutoDetector (字段提取)")

        def _run():
            result = extract_logcat_fields()
            self._safe_after(0, lambda: self._show_fields_popup(result))
            self._safe_after(0, lambda: self._set_buttons_state(True, token))
            if not result.get("ok"):
                self._safe_after(0, lambda: self._console_line(
                    f"[提取失败] {result.get('error', '未知错误')}", "error"
                ))
            else:
                sdk_count = len(result.get("SDK列表", []))
                self._safe_after(0, lambda: self._console_line(
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
        popup.geometry("920x720")
        popup.minsize(760, 620)
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

        content_frame = ttk.Frame(popup)
        content_frame.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(content_frame, bg="#ffffff", highlightthickness=0)
        scrollbar = ttk.Scrollbar(content_frame, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(canvas_window, width=e.width)
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=16, pady=(12, 8))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _add_row(parent, label, value, copy_text=None):
            row_frame = ttk.Frame(parent)
            row_frame.pack(fill=tk.X, pady=3)
            lbl = ttk.Label(row_frame, text=label, width=16, anchor=tk.E)
            lbl.pack(side=tk.LEFT, padx=(0, 8))
            if value:
                val_label = tk.Label(row_frame, text=value, anchor=tk.W,
                                     font=("Menlo", 11), fg="#333333", bg="#ffffff",
                                     wraplength=620, justify=tk.LEFT)
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
        btn_frame = ttk.Frame(popup, padding=(16, 8, 16, 12))
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)

        ttk.Button(
            btn_frame, text="📋 一键复制全部",
            command=lambda: self._copy_all_fields(data)
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="🔗 跳转后台",
            command=lambda: self._open_backend_url(data)
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(
            btn_frame, text="关闭",
            command=popup.destroy
        ).pack(side=tk.RIGHT, padx=2)

    def _copy_to_clipboard(self, text: str):
        """复制到剪贴板"""
        if self._closing or not self._root_exists():
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_all_fields(self, data: dict):
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
        self._copy_to_clipboard(text)

    def _open_backend_url(self, data: dict):
        """构造后台 URL 并在浏览器打开，自动填写适配信息"""
        import webbrowser
        pkg = self.pkg_entry.get().strip()
        if not pkg:
            self.status_label.config(text="请先在「配置」中填写包名")
            return
        url = build_backend_url(data, pkg)
        webbrowser.open(url)
        self.status_label.config(text="已在浏览器打开后台页面，请确认信息后提交")
