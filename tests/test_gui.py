import pytest
from unittest.mock import call, patch, MagicMock
import tkinter as tk
import json
import time


@pytest.fixture(autouse=True)
def isolate_gui_settings(monkeypatch, tmp_path):
    """GUI tests must not touch the operator's real settings/checkpoint files."""
    import gui

    # The production default is intentionally empty after removing the
    # historical credential from source.  GUI tests use an explicit dummy
    # credential so batch workflows can exercise their validation path.
    monkeypatch.setattr(gui, "DEFAULT_ASANA_PAT", "test-pat")

    original_load = gui.load_gui_settings
    original_save = gui.save_gui_settings

    def load_settings(settings_path=gui.SETTINGS_PATH):
        return {} if settings_path == gui.SETTINGS_PATH else original_load(settings_path)

    def save_settings(settings, settings_path=gui.SETTINGS_PATH):
        if settings_path != gui.SETTINGS_PATH:
            original_save(settings, settings_path)

    monkeypatch.setattr(gui, "load_gui_settings", load_settings)
    monkeypatch.setattr(gui, "save_gui_settings", save_settings)
    monkeypatch.setattr(
        gui,
        "AUTOMATION_CHECKPOINT_PATH",
        str(tmp_path / "automation_checkpoint.json"),
    )
    monkeypatch.setattr(
        gui,
        "AUTOMATION_REPORT_DIR",
        str(tmp_path / "automation_reports"),
    )
    # Keep existing GUI tests on the ordinary Google Play route. Dedicated
    # tests below cover the G99/no-GMS branch explicitly.
    monkeypatch.setattr(gui, "get_connected_device_profile", lambda: {})
    monkeypatch.setattr(gui, "ensure_clash_vpn_connected", lambda **kwargs: {
        "ok": True, "code": "CLASH_VPN_READY", "message": "测试 VPN 已连接",
    })


def wait_until(predicate, root=None, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if root is not None:
            try:
                root.update()
            except tk.TclError:
                pass
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None


class TestResetAutomationFailuresButton:
    @staticmethod
    def _app():
        from gui import APKToolApp
        from auto_asana.main import AsanaPrecheckTask
        from types import SimpleNamespace
        from datetime import datetime
        app = object.__new__(APKToolApp)
        app._precheck_running = False
        app._automation_running = False
        today = datetime.now().date()
        app._precheck_loaded_section_name = f"{today.month}.{today.day}执行"
        app._precheck_asana_status = MagicMock()
        for name in ("_precheck_reset_failed_btn", "_precheck_load_asana_btn", "_precheck_batch_btn", "_precheck_auto_adapt_btn"):
            setattr(app, name, MagicMock())
        app.project_gid_var = SimpleNamespace(get=lambda: "project")
        app.asana_pat_var = SimpleNamespace(get=lambda: "dummy-pat")
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        app._render_today_asana_tasks = MagicMock()
        task = AsanaPrecheckTask(
            gid="failed", name="com.failed", package_name="com.failed", up2_appid="", gp_link="",
            workflow_status="自动化失败", workflow_terminal=True,
        )
        app._precheck_tasks = {"row": task}
        app.precheck_task_tree = MagicMock()
        app.precheck_task_tree.get_children.return_value = ["row"]
        app.precheck_task_tree.item.return_value = (1, "com.failed", "", "自动化失败")
        return app

    def test_vpn_failure_blocks_submission_without_asana_failure_comment(self):
        import threading
        from gui import APKToolApp, AutomationVPNUnavailable
        app = object.__new__(APKToolApp)
        app._automation_stop_event = threading.Event()
        app._automation_log = MagicMock()
        app._automation_set_status = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        app._automation_fill_asana_sync = MagicMock()
        app._automation_submit_backend_sync = MagicMock()
        app._automation_comment_failure = MagicMock()
        with patch("gui.ensure_clash_vpn_connected", return_value={
            "ok": False, "code": "CLASH_VPN_TIMEOUT", "message": "VPN 未连接",
        }), pytest.raises(AutomationVPNUnavailable, match="VPN 未连接"):
            app._automation_execute_post_detection_sync()
        assert app._automation_stop_event.is_set()
        app._automation_fill_asana_sync.assert_not_called()
        app._automation_submit_backend_sync.assert_not_called()
        app._automation_comment_failure.assert_not_called()

    @pytest.mark.parametrize("reset_status", ["自动化失败", "待人工检查", "待处理"])
    def test_confirmed_reset_calls_service_without_starting_automation(self, reset_status):
        app = self._app()
        app.precheck_task_tree.item.return_value = (1, "com.failed", "", reset_status)
        result = {"reset_gids": {"failed"}, "reset_errors": [], "reset_skipped": 0}
        with patch("gui.private_feature_enabled", return_value=True), \
             patch("gui.messagebox.askyesno", return_value=True), \
             patch("gui.build_asana_client") as client, \
             patch("gui.reset_automation_failed_tasks_for_date", return_value=result) as reset, \
             patch("gui.threading.Thread", ImmediateThread):
            assert app._on_reset_automation_failed_tasks() is True
        assert reset.call_args.args == (client.return_value, "project", {"failed"})
        app._render_today_asana_tasks.assert_called_once_with(result)
        assert app._precheck_running is False
        app._precheck_reset_failed_btn.configure.assert_called_with(state=tk.NORMAL)
        assert "重置成功 1 个" in app._precheck_asana_status.config.call_args.kwargs["text"]
        assert app._automation_running is False

    @pytest.mark.parametrize("block", ["cancel", "busy", "permission", "old_day", "no_failed"])
    def test_blocked_reset_never_writes(self, block):
        app = self._app()
        if block == "busy":
            app._automation_running = True
        if block == "old_day":
            app._precheck_loaded_section_name = "旧日期执行"
        if block == "no_failed":
            app.precheck_task_tree.item.return_value = (1, "com.failed", "", "其他归因")
        with patch("gui.private_feature_enabled", return_value=block != "permission"), \
             patch("gui.messagebox.askyesno", return_value=False), \
             patch("gui.reset_automation_failed_tasks_for_date") as reset:
            assert app._on_reset_automation_failed_tasks() is False
        reset.assert_not_called()

    def test_service_error_restores_buttons_and_does_not_change_rows(self):
        app = self._app()
        with patch("gui.private_feature_enabled", return_value=True), \
             patch("gui.messagebox.askyesno", return_value=True), \
             patch("gui.build_asana_client"), \
             patch("gui.reset_automation_failed_tasks_for_date", side_effect=RuntimeError("网络异常")), \
             patch("gui.threading.Thread", ImmediateThread):
            app._on_reset_automation_failed_tasks()
        app._render_today_asana_tasks.assert_not_called()
        assert app._precheck_running is False
        app._precheck_reset_failed_btn.configure.assert_called_with(state=tk.NORMAL)
        assert "网络异常" in app._precheck_asana_status.config.call_args.kwargs["text"]

    def test_reset_rows_survive_refresh_and_reenter_pending_queue(self):
        from gui import APKToolApp
        from auto_asana.main import AsanaPrecheckTask
        from datetime import datetime
        root = tk.Tk()
        try:
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
            task = AsanaPrecheckTask(
                gid="reset", name="com.reset", package_name="com.reset", up2_appid="", gp_link="",
                workflow_status="待处理", workflow_terminal=False,
            )
            today = datetime.now().date()
            snapshot = {"section_name": f"{today.month}.{today.day}执行", "tasks": [task]}
            app._render_today_asana_tasks(snapshot)
            app._render_today_asana_tasks(snapshot)
            queue, _start = app._precheck_batch_queue_from_selection()
            assert [t.gid for _row, t in queue] == ["reset"]
            assert "reset" in app._precheck_new_task_gids
            assert app._precheck_reset_failed_btn.cget("text") == "重置为待处理"
        finally:
            root.destroy()


class TestCpCandidateAutoSync:
    @staticmethod
    def _app(sync_running=False):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._sync_running = sync_running
        app._cp_candidate_sync_pending = False
        app._sync_log = MagicMock()
        app._on_start_prefill_and_sync = MagicMock()
        app._sync_btn = MagicMock()
        app._prefill_sync_btn = MagicMock()
        return app

    def test_successful_assignment_starts_full_sync(self):
        app = self._app()

        started = app._request_prefill_and_sync_after_cp_assignment(3)

        assert started is True
        app._on_start_prefill_and_sync.assert_called_once_with()
        assert app._cp_candidate_sync_pending is False

    def test_zero_success_does_not_start_sync(self):
        app = self._app()

        started = app._request_prefill_and_sync_after_cp_assignment(0)

        assert started is False
        app._on_start_prefill_and_sync.assert_not_called()

    def test_assignment_queues_full_sync_while_another_sync_is_running(self):
        app = self._app(sync_running=True)

        started = app._request_prefill_and_sync_after_cp_assignment(2)

        assert started is False
        assert app._cp_candidate_sync_pending is True
        app._on_start_prefill_and_sync.assert_not_called()

    def test_sync_completion_starts_queued_full_sync(self):
        app = self._app(sync_running=True)
        app._cp_candidate_sync_pending = True

        app._finish_sync_operation()

        assert app._sync_running is False
        assert app._cp_candidate_sync_pending is False
        app._on_start_prefill_and_sync.assert_called_once_with()


class TestPrivateBatchAutomation:
    def test_permission_guard_blocks_combined_precheck_entry(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._precheck_status = MagicMock()
        app._automation_status = MagicMock()
        app._load_today_asana_tasks_async = MagicMock()

        with patch("gui.private_feature_enabled", return_value=False):
            started = app._on_start_precheck_then_automation()

        assert started is False
        app._load_today_asana_tasks_async.assert_not_called()
        app._precheck_status.config.assert_called_once_with(
            text="当前设备未启用批量自动化适配权限",
            foreground="#e53935",
        )

    def test_permission_guard_blocks_batch_queue_service_entry(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._precheck_status = MagicMock()
        app._automation_status = MagicMock()

        with patch("gui.private_feature_enabled", return_value=False), patch(
            "gui.get_connected_device_profile"
        ) as get_profile:
            started = app._automation_start_batch_queue(
                [], start_index=0, resume_stage="queued"
            )

        assert started is False
        get_profile.assert_not_called()


class TestPrivateWriteBoundaries:
    def test_asana_comment_wrapper_is_read_only_without_permission(self):
        import gui

        with patch("gui.private_feature_enabled", return_value=False), patch(
            "gui._add_precheck_comment_once_impl"
        ) as implementation:
            created = gui.add_precheck_comment_once(object(), "task", "comment")

        assert created is False
        implementation.assert_not_called()

    def test_backend_submission_is_rejected_before_reading_form_state(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        with patch("gui.private_feature_enabled", return_value=False):
            result = app._automation_submit_backend_sync()

        assert result["ok"] is False
        assert result["code"] == "PERMISSION_DENIED"

    def test_asana_fill_is_rejected_before_reading_task_state(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        with patch("gui.private_feature_enabled", return_value=False), pytest.raises(
            PermissionError, match="Asana 自动写入"
        ):
            app._automation_fill_asana_sync()

    def test_bulk_cleanup_is_rejected_before_device_mutation(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app.status_label = MagicMock()
        with patch("gui.private_feature_enabled", return_value=False), patch(
            "gui.uninstall_third_party_package"
        ) as uninstall:
            started = app._run_cleanup_uninstall(["com.example.app"], 1)

        assert started is False
        uninstall.assert_not_called()


class TestDeferredAutomationFailure:
    def _app(self, attempt):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_batch_active = True
        app._automation_batch_attempt = attempt
        app._automation_last_result_code = ""
        app._automation_last_result_message = ""
        app._automation_deferred_failure = None
        app._automation_precheck_item_id = ""
        app._automation_finish_report = MagicMock()
        app._automation_log = MagicMock()
        app._automation_set_status = MagicMock()
        app._automation_current_task_gid = MagicMock(return_value="task-1")
        app._automation_asana_client = MagicMock(return_value=object())
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        return app

    def test_first_retryable_failure_is_not_written_to_asana(self):
        app = self._app(attempt=0)
        with patch("gui.add_automation_comment_once") as add_comment:
            app._automation_comment_failure("AF_KEY_EMPTY", "af_key 为空")

        add_comment.assert_not_called()
        assert app._automation_deferred_failure == {
            "code": "AF_KEY_EMPTY",
            "message": "af_key 为空",
        }
        app._automation_finish_report.assert_called_once_with(
            "deferred_retry", "AF_KEY_EMPTY", "af_key 为空"
        )

    def test_second_retryable_failure_becomes_terminal_asana_comment(self):
        app = self._app(attempt=1)
        with patch("gui.add_automation_comment_once") as add_comment:
            app._automation_comment_failure("AF_KEY_EMPTY", "af_key 仍为空")

        add_comment.assert_called_once()
        assert app._automation_deferred_failure is None
        app._automation_finish_report.assert_called_once_with(
            "failed", "AF_KEY_EMPTY", "af_key 仍为空"
        )

    def test_af_key_empty_uses_specific_list_status(self):
        app = self._app(attempt=1)
        app._automation_precheck_item_id = "item-1"
        app._set_precheck_task_status = MagicMock()

        app._automation_mark_failed("af_key为空", code="AF_KEY_EMPTY")

        app._automation_set_status.assert_called_once_with(
            "af_key为空", "#e53935"
        )
        app._set_precheck_task_status.assert_called_once_with(
            "item-1", "af_key为空"
        )

    def test_other_failures_keep_generic_status(self):
        app = self._app(attempt=1)

        app._automation_mark_failed("广告 ID 为空", code="AD_IDS_EMPTY")

        app._automation_set_status.assert_called_once_with(
            "自动化失败", "#e53935"
        )

    def test_missing_package_is_requeued_without_failure_status(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_task_outcome = ""
        app._automation_last_result_code = ""
        app._automation_last_result_message = ""
        app._automation_deferred_failure = {"code": "OLD"}
        app._automation_precheck_item_id = "item-1"
        app._automation_current_package_name = MagicMock(
            return_value="com.example.missing"
        )
        app._automation_set_status = MagicMock()
        app._automation_log = MagicMock()
        app._set_precheck_task_status = MagicMock()
        app._automation_finish_report = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)

        app._automation_requeue_missing_package(
            "未安装: com.example.missing"
        )

        assert app._automation_task_outcome == "requeued"
        assert app._automation_last_result_code == "TARGET_APP_NOT_INSTALLED"
        assert app._automation_deferred_failure is None
        app._automation_set_status.assert_called_once_with(
            "应用未安装，已退回待处理", "#ef6c00"
        )
        app._set_precheck_task_status.assert_called_once_with(
            "item-1", "待处理"
        )
        app._automation_finish_report.assert_called_once_with(
            "requeued",
            "TARGET_APP_NOT_INSTALLED",
            "未安装: com.example.missing；已退回待处理，等待重新下载安装",
        )

    def test_batch_requeues_recoverable_failure_after_other_work(self):
        from types import SimpleNamespace
        from gui import APKToolApp

        root = tk.Tk()
        try:
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                task = SimpleNamespace(
                    gid="task-1",
                    package_name="com.retry.game",
                    up2_appid="appid-1",
                    notes="",
                )
                attempts = []

                def process():
                    attempts.append(app._automation_batch_attempt)
                    if app._automation_batch_attempt == 0:
                        app._automation_comment_failure(
                            "AF_KEY_EMPTY", "首次 af_key 为空"
                        )
                        return False
                    return True

                with patch("gui.threading.Thread", ImmediateThread), patch(
                    "gui.deferred_retry_delay_seconds", return_value=0
                ), patch.object(
                    app, "_automation_prepare_g99_task_sync", return_value=True
                ), patch.object(
                    app, "_automation_process_current_task_sync", side_effect=process
                ), patch.object(
                    app, "_automation_cleanup_current_app_sync"
                ), patch.object(
                    app, "_automation_finish_checkpoint_task"
                ) as finish_checkpoint, patch(
                    "gui.add_automation_comment_once"
                ) as add_comment:
                    app._automation_start_batch_queue(
                        [("item-1", task)],
                        start_index=0,
                        resume_stage="queued",
                    )

                assert attempts == [0, 1]
                add_comment.assert_not_called()
                finish_checkpoint.assert_called_once()
                assert finish_checkpoint.call_args.kwargs["success"] is True
        finally:
            root.destroy()


class TestAppInit:
    def test_default_config_paths_use_latest_directory(self):
        from gui import CONFIG_DEFAULT, WORK_DIR_DEFAULT

        assert CONFIG_DEFAULT == "/Users/a1506/Documents/适配动作与聚合参数获取_260629/config.json"
        assert WORK_DIR_DEFAULT == "/Users/a1506/Documents/适配动作与聚合参数获取_260629"

    def test_creates_main_window_with_title(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert "APK" in root.title()
        finally:
            root.destroy()

    def test_private_cp_candidate_button_is_hidden_by_default(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch("gui.private_feature_enabled", return_value=False), \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert app._cp_candidate_btn is None
                assert app._precheck_auto_adapt_btn.winfo_manager() == ""
                tab_labels = [
                    app._main_notebook.tab(tab_id, "text")
                    for tab_id in app._main_notebook.tabs()
                ]
                assert "自动化适配" not in tab_labels
        finally:
            root.destroy()

    def test_private_cp_candidate_button_is_available_when_enabled(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch("gui.private_feature_enabled", return_value=True), \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert app._cp_candidate_btn is not None
                assert app._precheck_auto_adapt_btn.winfo_manager() == "pack"
                tab_labels = [
                    app._main_notebook.tab(tab_id, "text")
                    for tab_id in app._main_notebook.tabs()
                ]
                assert "自动化适配" in tab_labels
        finally:
            root.destroy()

    def test_cp_candidate_dialog_shows_and_updates_selected_count(self):
        root = tk.Tk()
        try:
            from tkinter import ttk
            from gui import APKToolApp

            candidates = [
                {
                    "package_name": "com.high.game",
                    "priority": "高",
                    "selection_group": "高概率",
                    "score": 75,
                    "default_selected": True,
                    "has_iap": True,
                    "has_ads": True,
                    "category": "游戏/益智",
                    "reason": "高优先级",
                },
                {
                    "package_name": "com.low.game",
                    "priority": "低",
                    "selection_group": "普通候选",
                    "score": 40,
                    "default_selected": False,
                    "has_iap": False,
                    "has_ads": True,
                    "category": "普通包名",
                    "reason": "低优先级",
                },
            ]

            def descendants(widget):
                result = []
                for child in widget.winfo_children():
                    result.append(child)
                    result.extend(descendants(child))
                return result

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._show_cp_candidate_dialog(
                    {
                        "candidates": candidates,
                        "unassigned_count": 2,
                        "high_priority_count": 1,
                        "recommended_count": 1,
                        "quick_black_count": 0,
                        "default_selected_count": 1,
                    },
                    api_url="http://example.test/cp_adapt/list",
                    x_token="x",
                    token="fixed",
                )
                dialog = next(
                    child for child in root.winfo_children() if isinstance(child, tk.Toplevel)
                )
                widgets = descendants(dialog)
                count_label = next(
                    widget
                    for widget in widgets
                    if isinstance(widget, ttk.Label)
                    and "当前已勾选" in str(widget.cget("text"))
                )
                assert count_label.cget("text") == "当前已勾选 1 / 2 个 CP"

                clear_button = next(
                    widget
                    for widget in widgets
                    if isinstance(widget, ttk.Button)
                    and widget.cget("text") == "清空选择"
                )
                clear_button.invoke()
                assert count_label.cget("text") == "当前已勾选 0 / 2 个 CP"
        finally:
            root.destroy()

    def test_loads_remembered_config_and_work_dir(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.load_gui_settings", return_value={
                "config_path": "/tmp/remembered/config.json",
                "work_dir": "/tmp/remembered",
            }), patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert app.config_path_var.get() == "/tmp/remembered/config.json"
                assert app.work_dir_var.get() == "/tmp/remembered"
        finally:
            root.destroy()

    def test_loads_remembered_sync_settings(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            settings = {
                "sync_sheet_id": "saved-sheet",
                "sync_sheet_name": "26年7-8月",
                "sync_project_gid": "saved-project",
                "sync_asana_pat": "saved-pat",
                "sync_sa_file": "/tmp/saved-sa.json",
                "sync_proxy_url": "http://127.0.0.1:9999",
                "sync_parent_task_gid": "saved-parent",
                "sync_cp_adapt_api_url": "http://example.test/list",
                "sync_cp_adapt_x_token": "saved-x-token",
                "sync_cp_adapt_token": "saved-token",
                "sync_cp_adapt_assign": "rain",
            }
            with patch("gui.load_gui_settings", return_value=settings), \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)

                assert app.sheet_id_var.get() == "saved-sheet"
                assert app.sheet_name_var.get() == "26年7-8月"
                assert app.project_gid_var.get() == "saved-project"
                assert app.asana_pat_var.get() == "saved-pat"
                assert app.sa_file_var.get() == "/tmp/saved-sa.json"
                assert app.proxy_url_var.get() == "http://127.0.0.1:9999"
                assert app.parent_task_gid_var.get() == "saved-parent"
                assert app.cp_adapt_api_url_var.get() == "http://example.test/list"
                assert app.cp_adapt_x_token_var.get() == "saved-x-token"
                assert app.cp_adapt_token_var.get() == "saved-token"
                assert app.cp_adapt_assign_var.get() == "rain"
        finally:
            root.destroy()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                "https://app.asana.com/1/1208177697797743/inbox/1215092689545020/"
                "item/1217521692661986/story/1217521755962741",
                "1217521692661986",
            ),
            (
                "https://app.asana.com/1/1208177697797743/project/1215092379542969/"
                "task/1216098488026009",
                "1216098488026009",
            ),
            (
                "https://app.asana.com/0/1215092379542969/1216098488026009",
                "1216098488026009",
            ),
            ("1217521692661986", "1217521692661986"),
        ],
    )
    def test_extract_asana_task_gid(self, value, expected):
        from gui import extract_asana_task_gid

        assert extract_asana_task_gid(value) == expected

    def test_extract_asana_task_gid_rejects_non_task_url(self):
        from gui import extract_asana_task_gid

        with pytest.raises(ValueError):
            extract_asana_task_gid("https://app.asana.com/1/workspace/inbox")

    def test_parent_task_url_button_fills_and_saves_gid(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"), patch("gui.save_gui_settings") as save:
                app = APKToolApp(root)
                app.parent_task_url_var.set(
                    "https://app.asana.com/1/1208177697797743/inbox/1215092689545020/"
                    "item/1217521692661986/story/1217521755962741"
                )

                app._on_fill_parent_task_gid_from_url()

                assert app.parent_task_gid_var.get() == "1217521692661986"
                assert app._settings["sync_parent_task_gid"] == "1217521692661986"
                assert "已填充并保存" in app.parent_task_url_status_label.cget("text")
                save.assert_called()
        finally:
            root.destroy()

    def test_has_url_entry_widget(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert app.url_entry is not None
                assert isinstance(app.url_entry, tk.Entry)
        finally:
            root.destroy()

    def test_has_qr_button(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                # 按钮已改为内联创建，验证生成二维码方法存在
                assert hasattr(app, "_on_generate_qr")
        finally:
            root.destroy()

    def test_has_push_button(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert hasattr(app, "_on_push_apk")
        finally:
            root.destroy()

    def test_has_adb_tab_widgets(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert hasattr(app, "pkg_entry")
                assert hasattr(app, "appid_entry")
                assert hasattr(app, "task_uuid_var")
                assert hasattr(app, "output_text")
                assert hasattr(app, "uid_var")
                assert hasattr(app, "app_bitness_var")
                assert hasattr(app, "_on_get_app_bitness")
                assert hasattr(app, "_on_fix_zygotehole_permissions")
                assert hasattr(app, "_stop_command_btn")
                assert hasattr(app, "cleanup_preview_text")
        finally:
            root.destroy()

    def test_has_google_play_precheck_tab_widgets(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert hasattr(app, "precheck_task_tree")
                assert hasattr(app, "precheck_input")
                assert hasattr(app, "precheck_output")
                assert tuple(app.precheck_task_tree.cget("displaycolumns")) == (
                    "order",
                    "package",
                    "status",
                )
                assert hasattr(app, "_on_load_today_asana_tasks")
                assert hasattr(app, "_on_start_precheck")
                assert hasattr(app, "_on_start_batch_precheck")
                assert app._precheck_batch_btn.cget("text") == "批量预检今日待处理"
                assert (
                    app._precheck_auto_adapt_btn.cget("text")
                    == "批量预检并自动适配"
                )
                assert app.precheck_auto_install_var.get() is True
                assert app.precheck_download_limit_var.get() == "6"
                assert app._automation_batch_btn.cget("text") == "批量自动适配预检合格任务"
        finally:
            root.destroy()

    def test_adb_action_button_order(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                labels = [button.cget("text") for button in app._op_buttons]
                assert labels == [
                    "预览清理",
                    "一键清理第三方包",
                    "推送 Config",
                    "执行 zygote_build",
                    "获取应用 UID",
                    "检测应用位数",
                    "清除缓存",
                    "强制停止",
                    "打开应用",
                    "清空 Play Store 缓存",
                    "修复 zygotehole 权限",
                    "取消当前游戏注入",
                ]
        finally:
            root.destroy()

    def test_clear_cache_keeps_buttons_enabled(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            expected_cmd = ["adb", "shell", "pm", "clear", "com.example.app"]
            with patch("gui.check_device", return_value=True), \
                 patch("gui.build_clear_cache_cmd", return_value=expected_cmd) as mock_build, \
                 patch.object(APKToolApp, "_run_command") as mock_run, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.pkg_entry.delete(0, tk.END)
                app.pkg_entry.insert(0, "com.example.app")

                app._on_clear_cache()

                mock_build.assert_called_once_with("com.example.app")
                mock_run.assert_called_once_with(
                    expected_cmd, timeout=8, disable_buttons=False
                )
        finally:
            root.destroy()

    def test_get_uid_outputs_only_uid_summary(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.pkg_entry.delete(0, tk.END)
                app.pkg_entry.insert(0, "com.example.app")

                with patch("gui.check_device", return_value=True), \
                     patch("gui.get_app_uid", return_value=(True, "10323")) as mock_get_uid, \
                    patch("gui.threading.Thread", ImmediateThread):
                    app._on_get_uid()

                mock_get_uid.assert_called_once_with("com.example.app")
                assert app.uid_var.get() == "10323"
                output = app.output_text.get("1.0", tk.END)
                assert "[UID] com.example.app: 10323" in output
                assert "ContentProvider" not in output
        finally:
            root.destroy()

    def test_stop_current_command_terminates_proc_and_restores_buttons(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                proc = MagicMock()
                proc.poll.return_value = None
                app._current_command_proc = proc
                app._set_buttons_state(False)

                app._on_stop_current_command()

                proc.terminate.assert_called_once_with()
                proc.wait.assert_called_once_with(timeout=1)
                assert app._current_command_proc is None
                assert str(app._stop_command_btn.cget("state")) == tk.DISABLED
        finally:
            root.destroy()

    def test_has_status_label(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert app.status_label is not None
        finally:
            root.destroy()

    def test_tab_order(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                notebook = next(
                    child for child in root.winfo_children()
                    if isinstance(child, tk.ttk.Notebook)
                )
                assert [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()] == [
                    "APK 工具",
                    "页面预检",
                    "ADB 指令",
                    "自动化脚本",
                    "自动化适配",
                    "当日总结",
                    "数据同步",
                ]
                assert app.automation_replay_timeout_var.get() == "300"
        finally:
            root.destroy()


class TestGooglePlayPrecheckActions:
    @pytest.mark.parametrize("entry", ["single", "batch", "open_page"])
    def test_vpn_failure_blocks_precheck_and_result_writes(self, entry):
        from gui import APKToolApp
        from auto_asana.main import AsanaPrecheckTask
        root = tk.Tk()
        try:
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
            app._safe_after = lambda _delay, callback, *args: callback(*args)
            app.precheck_input.set("com.upfsaw.jivrashiftpuz.zawgamelogix")
            app.project_gid_var.set("project")
            app.asana_pat_var.set("dummy-pat")
            task = AsanaPrecheckTask(
                gid="task", name="test", package_name=app.precheck_input.get(),
                up2_appid="", gp_link="",
            )
            app._precheck_batch_queue_from_selection = MagicMock(
                return_value=([("row", task)], 1)
            )
            app._refresh_completed_background_downloads = MagicMock()
            app._selected_precheck_task = MagicMock(return_value=("row", task))
            app._run_precheck_with_unknown_retry = MagicMock()
            app._install_after_precheck = MagicMock()
            app._submit_precheck_blacklist = MagicMock()
            app._start_automation_after_precheck = MagicMock()
            with patch("gui.ensure_clash_vpn_connected", return_value={
                "ok": False, "message": "Clash VPN 启动超时",
            }), patch("gui.threading.Thread", ImmediateThread), \
                 patch("gui.build_asana_client"), \
                 patch("gui.open_google_play_page") as open_page, \
                 patch("gui.add_precheck_comment_once") as comment:
                if entry == "single":
                    app._on_start_precheck()
                elif entry == "batch":
                    app._on_start_batch_precheck(start_automation_after=True)
                else:
                    app._on_precheck_open_page()
            open_page.assert_not_called()
            comment.assert_not_called()
            app._run_precheck_with_unknown_retry.assert_not_called()
            app._install_after_precheck.assert_not_called()
            app._submit_precheck_blacklist.assert_not_called()
            app._start_automation_after_precheck.assert_not_called()
            assert "Clash VPN 启动超时" in app._precheck_status.cget("text")
            assert not app._precheck_running
        finally:
            root.destroy()

    def test_g99_without_gms_routes_precheck_to_apkcombo(self):
        from gui import APKToolApp

        expected = {"code": "APKCOMBO_AVAILABLE", "package_name": "com.g99.game"}
        profile = {
            "connected": True,
            "model": "G99",
            "use_apkcombo_only": True,
        }
        with patch(
            "gui.run_apkcombo_only_precheck", return_value=expected
        ) as apkcombo_precheck, patch("gui.run_google_play_precheck") as play_precheck:
            result = APKToolApp._run_precheck_for_connected_device(
                None,
                "com.g99.game",
                device_profile=profile,
            )

        assert result == expected
        apkcombo_precheck.assert_called_once_with(
            "com.g99.game", device_profile=profile
        )
        play_precheck.assert_not_called()

    def test_unknown_precheck_reopens_once_and_uses_recovered_result(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        first = {
            "code": "UNKNOWN",
            "detail": "没有取得足够的 Play Store 页面信息。",
            "package_name": "com.example.game",
        }
        recovered = {
            "code": "HAS_ADS",
            "detail": "页面明确标注包含广告。",
            "package_name": "com.example.game",
            "evidence": ["发现包含广告标识"],
        }
        progress = MagicMock()
        with patch.object(
            app,
            "_run_precheck_for_connected_device",
            side_effect=[first, recovered],
        ) as precheck, patch("gui.time.sleep") as sleep:
            result = app._run_precheck_with_unknown_retry(
                "com.example.game",
                on_retry=progress,
            )

        assert result["code"] == "HAS_ADS"
        assert result["precheck_retry"] == {
            "performed": True,
            "first_code": "UNKNOWN",
            "retry_code": "HAS_ADS",
        }
        assert any(
            "自动重新打开页面后复检成功" in item
            for item in result["evidence"]
        )
        assert precheck.call_count == 2
        sleep.assert_called_once_with(3)
        progress.assert_called_once()

    def test_conclusive_precheck_is_not_retried(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        conclusive = {
            "code": "IAP_ONLY",
            "detail": "仅检测到应用内购。",
        }
        with patch.object(
            app,
            "_run_precheck_for_connected_device",
            return_value=conclusive,
        ) as precheck, patch("gui.time.sleep") as sleep:
            result = app._run_precheck_with_unknown_retry("com.example.game")

        assert result == conclusive
        precheck.assert_called_once()
        sleep.assert_not_called()

    def test_unknown_retry_still_unknown_records_explicit_reason(self):
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        unknown = {
            "code": "UNKNOWN",
            "detail": "没有取得足够的 Play Store 页面信息。",
            "evidence": [],
        }
        with patch.object(
            app,
            "_run_precheck_for_connected_device",
            side_effect=[dict(unknown), dict(unknown)],
        ), patch("gui.time.sleep"):
            result = app._run_precheck_with_unknown_retry("com.example.game")

        assert result["code"] == "UNKNOWN"
        assert "已自动重新打开同一页面复检一次" in result["detail"]
        assert any(
            "首次检查与自动复检均未取得足够" in item
            for item in result["evidence"]
        )

    def test_selected_asana_task_keeps_only_package_as_internal_action_value(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                result = {
                    "section_name": "7.30执行",
                    "tasks": [
                        AsanaPrecheckTask(
                            gid="task-1",
                            name="聚合/动作适配com.example.game",
                            package_name="com.example.game",
                            up2_appid="up2",
                            gp_link="https://play.google.com/store/apps/details?id=com.example.game",
                        )
                    ],
                }

                app._render_today_asana_tasks(result)

                assert app.precheck_input.get() == "com.example.game"
                assert "1 个任务" in app._precheck_asana_status.cget("text")
        finally:
            root.destroy()

    def test_package_search_immediately_selects_and_reveals_matching_row(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=package_name,
                    up2_appid=f"appid-{index}",
                    gp_link=(
                        "https://play.google.com/store/apps/details?id="
                        + package_name
                    ),
                )
                for index, package_name in enumerate(
                    [
                        "com.first.game",
                        "com.target.puzzle",
                        "com.last.game",
                    ],
                    start=1,
                )
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "9.2执行", "tasks": tasks}
                )

                app.precheck_search_var.set("com.target.puzzle")

                item_id, selected_task = app._selected_precheck_task()
                assert selected_task.gid == "task-2"
                assert app.precheck_task_tree.focus() == item_id
                assert app.precheck_input.get() == "com.target.puzzle"
                assert app.precheck_search_var.get() == "com.target.puzzle"
                assert app._precheck_search_status.cget("text") == "已定位 1/1"
                assert app._precheck_manual_selection is True
        finally:
            root.destroy()

    def test_selecting_package_row_syncs_visible_search_field(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid="selected-task",
                    name="selected-task",
                    package_name="com.selected.package",
                    up2_appid="appid",
                    gp_link="",
                )
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "9.2执行", "tasks": tasks}
                )

                assert app.precheck_search_var.get() == "com.selected.package"
                assert app._selected_precheck_task()[1].package_name == (
                    "com.selected.package"
                )
        finally:
            root.destroy()

    def test_partial_package_search_enter_cycles_matches(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"match-{index}",
                    name=f"match-{index}",
                    package_name=package_name,
                    up2_appid="appid",
                    gp_link="",
                )
                for index, package_name in enumerate(
                    ["com.alpha.puzzle", "com.beta.puzzle", "com.other.game"],
                    start=1,
                )
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "9.2执行", "tasks": tasks}
                )

                app.precheck_search_var.set("puzzle")
                assert app._selected_precheck_task()[1].gid == "match-1"
                app._on_precheck_search_next()

                assert app._selected_precheck_task()[1].gid == "match-2"
                assert app._precheck_search_status.cget("text") == "已定位 2/2"
        finally:
            root.destroy()


class TestAutomationBatchActions:
    def test_single_extract_requeues_missing_package_and_finishes_report(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp, AutomationTaskRequeued

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.missing")
                with patch("gui.threading.Thread", ImmediateThread), patch.object(
                    app,
                    "_automation_prepare_detection_sync",
                    side_effect=AutomationTaskRequeued(
                        "未安装: com.example.missing"
                    ),
                ), patch.object(
                    app, "_automation_cleanup_current_app_sync"
                ), patch.object(
                    app, "_automation_mark_failed"
                ) as mark_failed, patch.object(
                    app, "_automation_finish_report"
                ) as finish_report:
                    app._automation_extract_fields()

                mark_failed.assert_not_called()
                finish_report.assert_called_once_with(
                    "requeued",
                    "TARGET_APP_NOT_INSTALLED",
                    "未安装: com.example.missing；已退回待处理，等待重新下载安装",
                )
                assert app._automation_status.cget("text") == "应用未安装，已退回待处理"
        finally:
            root.destroy()

    def test_single_extract_unexpected_preparation_error_finishes_report(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.broken")
                with patch("gui.threading.Thread", ImmediateThread), patch.object(
                    app,
                    "_automation_prepare_detection_sync",
                    side_effect=RuntimeError("构建脚本执行失败"),
                ), patch.object(
                    app, "_automation_cleanup_current_app_sync"
                ), patch.object(
                    app, "_automation_finish_report"
                ) as finish_report:
                    app._automation_extract_fields()

                finish_report.assert_called_once_with(
                    "failed",
                    "AUTOMATION_PREPARE_FAILED",
                    "聚合参数提取失败: 构建脚本执行失败",
                )
                assert app._automation_status.cget("text") == "自动化失败"
        finally:
            root.destroy()

    def test_selected_precheck_task_is_written_to_shared_adb_config(self, tmp_path):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps({"data": [{"preserved": "value"}]}),
                encoding="utf-8",
            )
            task = AsanaPrecheckTask(
                gid="task-selected",
                name="selected",
                package_name="com.selected.game",
                up2_appid="selected-appid",
                gp_link="",
            )

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.config_path_var.set(str(config_path))
                app._safe_after = lambda _delay, callback, *args: callback(*args)

                def start_monitor(pattern, **kwargs):
                    app._logcat_proc = MagicMock()

                with patch("gui.threading.Thread", ImmediateThread), patch(
                    "gui.check_device", return_value=True
                ), patch(
                    "gui.build_push_config_cmd", return_value=["adb", "push", "config"]
                ) as build_push, patch(
                    "gui.get_app_uid", return_value=(True, "10456")
                ) as get_uid, patch.object(
                    app, "_automation_run_command_sync"
                ) as run_command, patch.object(
                    app, "_start_logcat_stream", side_effect=start_monitor
                ) as start_logcat, patch.object(
                    app,
                    "_selected_precheck_task",
                    return_value=("item-selected", task),
                ):
                    app._automation_use_selected_precheck_task()

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            assert saved["data"][0]["packageName"] == "com.selected.game"
            assert saved["data"][0]["appId"] == "selected-appid"
            assert saved["data"][0]["taskUUID"] == "mediation_test_snow"
            assert saved["data"][0]["preserved"] == "value"
            assert app.pkg_entry.get() == "com.selected.game"
            assert app.appid_entry.get() == "selected-appid"
            assert app.task_uuid_var.get() == "mediation_test_snow"
            build_push.assert_called_once_with(str(config_path))
            run_command.assert_called_once_with(
                ["adb", "push", "config"],
                timeout=30,
                respect_control=False,
            )
            get_uid.assert_called_once_with("com.selected.game")
            assert app.uid_var.get() == "10456"
            start_logcat.assert_called_once_with(
                "ZGSDK.AutoDetector", require_uid=True
            )
            assert "正在监控" in app._automation_status.cget("text")
        finally:
            root.destroy()

    def test_selected_task_push_failure_does_not_reuse_uid_or_start_monitor(
        self, tmp_path
    ):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            config_path = tmp_path / "config.json"
            config_path.write_text('{"data": [{}]}', encoding="utf-8")
            task = AsanaPrecheckTask(
                gid="task-failed-push",
                name="failed-push",
                package_name="com.failed.push",
                up2_appid="failed-push-appid",
                gp_link="",
            )

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.config_path_var.set(str(config_path))
                app._cached_uid = "10001"
                app.uid_var.set("10001")
                app._safe_after = lambda _delay, callback, *args: callback(*args)
                with patch("gui.threading.Thread", ImmediateThread), patch(
                    "gui.check_device", return_value=True
                ), patch.object(
                    app,
                    "_automation_run_command_sync",
                    side_effect=RuntimeError("设备拒绝推送"),
                ), patch(
                    "gui.get_app_uid"
                ) as get_uid, patch.object(
                    app, "_start_logcat_stream"
                ) as start_logcat, patch.object(
                    app,
                    "_selected_precheck_task",
                    return_value=("item-failed", task),
                ):
                    app._automation_use_selected_precheck_task()

                assert app._cached_uid is None
                assert app.uid_var.get() == ""
                get_uid.assert_not_called()
                start_logcat.assert_not_called()
                assert "推送/UID 获取失败" in app._automation_status.cget("text")
        finally:
            root.destroy()

    def test_switching_task_stops_old_listener_before_uid_reset(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                old_proc = MagicMock()
                app._logcat_proc = old_proc
                app._active_pattern = "ZGSDK.AutoDetector"
                app._cached_uid = "011"
                app.uid_var.set("011")
                app._automation_fields = {
                    "ok": True,
                    "最终判断": "MAX聚合",
                    "插屏聚合id": "old-inter-id",
                }
                app._automation_render_fields(app._automation_fields)
                old_context_version = app._automation_context_version
                task = AsanaPrecheckTask(
                    gid="task-b",
                    name="task-b",
                    package_name="com.package.b",
                    up2_appid="appid-b",
                    gp_link="",
                )

                with patch("gui.stop_logcat_stream") as stop_stream:
                    app._automation_switch_task_sync("item-b", task)

                stop_stream.assert_called_once_with(old_proc)
                assert app._logcat_proc is None
                assert app._cached_uid is None
                assert app.uid_var.get() == ""
                assert app.automation_package_var.get() == "com.package.b"
                assert app._automation_fields == {}
                assert app.automation_fields_text.get("1.0", tk.END).strip() == ""
                assert app._automation_field_status.cget("text") == "尚未提取"
                assert app._automation_context_version == old_context_version + 1
        finally:
            root.destroy()

    def test_delayed_old_task_fields_cannot_repopulate_new_task(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                old_context_version = app._automation_context_version
                app._automation_clear_detected_fields()

                app._automation_render_fields(
                    {
                        "ok": True,
                        "最终判断": "MAX聚合",
                        "插屏聚合id": "old-inter-id",
                    },
                    old_context_version,
                )

                assert app.automation_fields_text.get("1.0", tk.END).strip() == ""
                assert app._automation_field_status.cget("text") == "尚未提取"
        finally:
            root.destroy()

    def test_batch_pause_button_toggles_pause_and_resume(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._automation_batch_active = True
                app._automation_set_running(True)

                app._automation_toggle_pause()
                assert app._automation_pause_event.is_set()
                assert app._automation_pause_btn.cget("text") == "继续队列"

                app._automation_toggle_pause()
                assert not app._automation_pause_event.is_set()
                assert app._automation_pause_btn.cget("text") == "暂停队列"
        finally:
            root.destroy()

    def test_automation_comments_use_immutable_active_task_target(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_task_gid_var.set("task-a")
                app.automation_package_var.set("com.example.a")
                app.automation_appid_var.set("appid-a")
                with patch.object(app._automation_report_store, "begin_task", return_value=""):
                    app._automation_begin_report("batch")

                # Simulate a delayed UI callback already painting task B while
                # task A's worker is still writing its terminal comment.
                app.automation_task_gid_var.set("task-b")
                app.automation_package_var.set("com.example.b")

                client = MagicMock()
                with patch.object(app, "_automation_asana_client", return_value=client), patch(
                    "gui.add_automation_comment_once"
                ) as add_comment:
                    app._automation_comment_failure("AUTOMATION_FAILED", "failed")

                assert add_comment.call_args.args[1] == "task-a"
        finally:
            root.destroy()

    def test_backend_submit_and_cleanup_use_immutable_active_package(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_task_gid_var.set("task-a")
                app.automation_package_var.set("com.example.a")
                app.automation_appid_var.set("appid-a")
                app._automation_fields = {
                    "ok": True,
                    "最终判断": "MAX聚合",
                    "归因平台": "Adjust",
                    "插屏聚合id": "inter-a",
                }
                with patch.object(app._automation_report_store, "begin_task", return_value=""):
                    app._automation_begin_report("batch")

                # A delayed UI render has already changed the visible row to
                # B, while the worker is still submitting and cleaning up A.
                app.automation_package_var.set("com.example.b")
                app.automation_appid_var.set("appid-b")

                with patch(
                    "gui.submit_backend_via_api",
                    return_value={"ok": True, "code": "OK", "message": "ok"},
                ) as submit, patch.object(
                    app, "_automation_run_command_sync", return_value="Success"
                ) as run_command:
                    result = app._automation_submit_backend_sync()
                    cleaned = app._automation_cleanup_current_app_sync("done")

                assert result["ok"] is True
                assert cleaned is True
                assert submit.call_args.args[1] == "com.example.a"
                assert "com.example.a" in run_command.call_args.args[0]
                assert "com.example.b" not in run_command.call_args.args[0]
        finally:
            root.destroy()

    def test_infrastructure_logcat_failure_stops_queue_without_asana_comment(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.game")
                failure = {
                    "ok": False,
                    "code": "LOGCAT_READ_TIMEOUT",
                    "message": "设备在线，但 Logcat 读取超时",
                    "fields": {
                        "ok": False,
                        "_runtime_code": "LOGCAT_READ_TIMEOUT",
                        "_transient": True,
                    },
                }

                with patch.object(
                    app, "_automation_prepare_detection_sync", return_value={}
                ), patch(
                    "gui.detect_aggregation_with_one_retry", return_value=failure
                ), patch.object(
                    app, "_automation_comment_failure"
                ) as comment, patch.object(
                    app, "_automation_mark_failed"
                ) as mark_failed:
                    result = app._automation_process_current_task_sync()

                assert result is False
                assert app._automation_stop_event.is_set()
                comment.assert_not_called()
                mark_failed.assert_not_called()
                assert "不写入 Asana" in app.automation_log_text.get("1.0", tk.END)
        finally:
            root.destroy()

    def test_mandatory_google_login_is_finalized_during_launch_precheck(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                source = {
                    "package_name": "com.google.login.game",
                    "install_result": {"ok": True, "code": "INSTALLED"},
                }
                login_result = {
                    "ok": False,
                    "code": "GOOGLE_LOGIN_REQUIRED",
                    "message": "应用只能通过 Google 登录进入，未发现免登录入口",
                }
                backend = {"ok": True, "code": "PRECHECK_BLACKLIST_SUBMITTED"}
                with patch(
                    "gui.run_app_launch_precheck", return_value=login_result
                ) as launch, patch.object(
                    app,
                    "_submit_precheck_blacklist",
                    side_effect=lambda result: {
                        **result,
                        "backend_blacklist": backend,
                    },
                ) as submit:
                    result = app._launch_check_after_install(source, 20)

                launch.assert_called_once()
                submit.assert_called_once()
                assert result["code"] == "GOOGLE_LOGIN_REQUIRED"
                assert result["continue_adaptation"] is False
                assert result["backend_blacklist"] == backend
                assert APKToolApp._precheck_task_status_for_result(result) == "已加黑(后台)"
        finally:
            root.destroy()

    def test_stop_during_backend_step_never_enters_replay_or_writes_failure(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.game")
                app._automation_fields = {
                    "ok": True,
                    "最终判断": "MAX聚合",
                    "归因平台": "Adjust",
                    "插屏聚合id": "inter-1",
                }

                def submit_then_stop():
                    app._automation_stop_event.set()
                    return {
                        "ok": False,
                        "code": "BACKEND_SUBMIT_FAILED",
                        "message": "未找到提交按钮",
                    }

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch.object(app, "_automation_fill_asana_sync"), \
                     patch.object(
                         app,
                         "_automation_submit_backend_sync",
                         side_effect=submit_then_stop,
                     ), \
                     patch.object(app, "_automation_replay_sync") as replay, \
                     patch.object(app, "_automation_comment_failure") as comment:
                    app._automation_run_post_detection()

                replay.assert_not_called()
                comment.assert_not_called()
                assert "不再进入聚合回放" in app.automation_log_text.get(
                    "1.0", tk.END
                )
        finally:
            root.destroy()

    def test_stop_after_detection_never_fills_asana_or_submits_backend(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.game")
                app._automation_fields = {}

                def detection_then_stop(*args, **kwargs):
                    app._automation_stop_event.set()
                    return {
                        "ok": True,
                        "fields": {
                            "最终判断": "MAX聚合",
                            "归因平台": "Adjust",
                            "初始Activity": "MainActivity",
                            "插屏聚合id": "inter-1",
                        },
                    }

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.detection_field_issue", return_value=("EMPTY", "empty")), \
                     patch("gui.detect_aggregation_with_one_retry", side_effect=detection_then_stop), \
                     patch.object(app, "_automation_prepare_detection_sync", return_value={}), \
                     patch.object(app, "_automation_fill_asana_sync") as fill_asana, \
                     patch.object(app, "_automation_submit_backend_sync") as submit:
                    app._automation_run_post_detection()

                fill_asana.assert_not_called()
                submit.assert_not_called()
                assert "不再回填 Asana" in app.automation_log_text.get("1.0", tk.END)
        finally:
            root.destroy()

    def test_unsupported_attribution_is_filled_submitted_and_skips_replay(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.solar.game")
                fields = {
                    "ok": True,
                    "最终判断": "MAX聚合",
                    "初始Activity": "MainActivity",
                    "归因平台": "SolarEngine",
                    "插屏聚合id": "inter-1",
                }
                detection = {
                    "ok": False,
                    "code": "UNSUPPORTED_ATTRIBUTION",
                    "message": "SolarEngine归因，暂不适配",
                    "fields": fields,
                }

                with patch.object(
                    app, "_automation_prepare_detection_sync", return_value=fields
                ), patch(
                    "gui.detect_aggregation_with_one_retry", return_value=detection
                ), patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app,
                    "_automation_clear_inferred_backend_sync",
                    return_value={"ok": True, "message": "后台已生效"},
                ) as submit, patch.object(
                    app, "_automation_replay_sync"
                ) as replay, patch.object(
                    app, "_automation_comment_business_outcome"
                ) as comment:
                    result = app._automation_process_current_task_sync()

                assert result is False
                fill_asana.assert_called_once_with(
                    allow_unsupported_attribution=True
                )
                submit.assert_called_once_with(note="归因为SolarEngine，暂不适配")
                replay.assert_not_called()
                comment.assert_called_once_with(
                    "UNSUPPORTED_ATTRIBUTION",
                    "SolarEngine归因，暂不适配\n包名：com.solar.game",
                )
                assert "跳过聚合回放" in app.automation_log_text.get(
                    "1.0", tk.END
                )
                assert app._automation_task_outcome == "other_attribution"
                assert app._automation_status.cget("text") == "其他归因"
        finally:
            root.destroy()

    @pytest.mark.parametrize("attribution", ["Tenjin", "Singular"])
    def test_empty_aggregation_other_attribution_persists_asana_and_backend_note(self, attribution):
        import threading
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_fields = {
            "最终判断": "未检测到主要聚合平台",
            "归因平台": attribution,
            "初始Activity": "com.unity3d.player.UnityPlayerActivity",
            "应用类型": "Unity",
        }
        package = "com.infinityfun.yatzychoochoo"
        note = f"{attribution}归因，且聚合类型识别为空，暂不适配"
        client = MagicMock()
        app._automation_current_package_name = lambda: package
        app._automation_current_task_gid = lambda: "task-1"
        app._automation_asana_client = lambda: client
        app._automation_task_notes = f"包名：{package}"
        app._automation_stop_event = threading.Event()
        app._automation_precheck_item_id = ""
        app._automation_log = MagicMock()
        app._automation_set_status = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        app._automation_comment_business_outcome = MagicMock()
        app._automation_clear_inferred_backend_sync = MagicMock(
            return_value={"ok": True, "message": "后台备注已生效"}
        )
        app._automation_write_sheet_outcome_sync = MagicMock()
        app._automation_replay_sync = MagicMock()
        app._automation_submit_backend_sync = MagicMock()

        with patch("gui.private_feature_enabled", return_value=True):
            result = app._automation_complete_unsupported_attribution_sync(note)

        assert result is False
        assert app._automation_task_outcome == "other_attribution"
        client.tasks.update_task.assert_called_once()
        saved_notes = client.tasks.update_task.call_args.args[1]["notes"]
        assert f"归因平台:{attribution}" in saved_notes
        assert f"适配结论:{note}" in saved_notes
        app._automation_clear_inferred_backend_sync.assert_called_once_with(note=note)
        app._automation_comment_business_outcome.assert_called_once_with(
            "UNSUPPORTED_ATTRIBUTION", f"{note}\n包名：{package}"
        )
        app._automation_write_sheet_outcome_sync.assert_called_once_with("not_adapted", note)
        app._automation_replay_sync.assert_not_called()
        app._automation_submit_backend_sync.assert_not_called()

    def test_tradplus_clears_backend_and_skips_parameter_submit_and_replay(self):
        import threading
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_current_package_name = lambda: "com.demo.tradplus"
        app._automation_stop_event = threading.Event()
        app._automation_precheck_item_id = "item-1"
        app._automation_log = MagicMock()
        app._automation_set_status = MagicMock()
        app._set_precheck_task_status = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        app._automation_fill_asana_sync = MagicMock()
        app._automation_comment_business_outcome = MagicMock()
        app._automation_clear_inferred_backend_sync = MagicMock(
            return_value={"ok": True, "message": "后台参数已清空"}
        )
        app._automation_mark_failed = MagicMock()
        app._automation_comment_failure = MagicMock()
        app._automation_write_sheet_outcome_sync = MagicMock()

        result = app._automation_complete_unsupported_aggregation_sync(
            "TradPlus聚合，暂不适配"
        )

        assert result is False
        app._automation_fill_asana_sync.assert_called_once_with(
            allow_unsupported_attribution=True,
            terminal_note="TradPlus聚合，暂不适配",
        )
        app._automation_clear_inferred_backend_sync.assert_called_once_with(
            note="TradPlus聚合，暂不适配"
        )
        app._automation_comment_business_outcome.assert_called_once_with(
            "UNSUPPORTED_AGGREGATION",
            "TradPlus聚合，暂不适配\n包名：com.demo.tradplus",
        )
        app._automation_write_sheet_outcome_sync.assert_called_once_with(
            "not_adapted", "TradPlus聚合，暂不适配"
        )
        assert app._automation_task_outcome == "unsupported_aggregation"
        app._automation_set_status.assert_called_with(
            "TradPlus聚合，暂不适配", "#ef6c00"
        )
        app._set_precheck_task_status.assert_called_once_with(
            "item-1", "TradPlus暂不适配"
        )

    def test_single_extract_unknown_attribution_fills_asana_and_skips_replay(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.unknown.attribution")
                fields = {
                    "ok": True,
                    "最终判断": "MAX聚合（强关系证据确认）",
                    "初始Activity": "MainActivity",
                    "归因平台": "",
                    "插屏聚合id": "inter-1",
                }
                detection = {
                    "ok": False,
                    "code": "UNSUPPORTED_ATTRIBUTION",
                    "message": "未知归因，暂不适配",
                    "fields": fields,
                }

                with patch("gui.threading.Thread", ImmediateThread), patch.object(
                    app, "_automation_prepare_detection_sync", return_value=fields
                ), patch(
                    "gui.detect_aggregation_with_one_retry", return_value=detection
                ), patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app,
                    "_automation_clear_inferred_backend_sync",
                    return_value={"ok": True, "message": "后台已生效"},
                ) as submit, patch.object(
                    app, "_automation_replay_sync"
                ) as replay, patch.object(
                    app, "_automation_comment_business_outcome"
                ) as comment:
                    app._automation_extract_fields()

                fill_asana.assert_called_once_with(
                    allow_unsupported_attribution=True
                )
                submit.assert_called_once_with(note="归因为未知，暂不适配")
                replay.assert_not_called()
                comment.assert_called_once_with(
                    "UNSUPPORTED_ATTRIBUTION",
                    "未知归因，暂不适配\n包名：com.unknown.attribution",
                )
                assert "已回填至 Asana 描述" in app.automation_log_text.get(
                    "1.0", tk.END
                )
        finally:
            root.destroy()

    def test_single_extract_high_confidence_auto_runs_fill_submit_and_replay(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.high.confidence")
                fields = {
                    "ok": True,
                    "最终判断": "MAX聚合（强关系证据确认）",
                    "初始Activity": "com.demo.MainActivity",
                    "应用类型": "Native",
                    "归因平台": "Adjust, AppMetrica",
                    "插屏聚合id": "inter-1",
                }
                detection = {
                    "ok": True,
                    "code": "AGGREGATION_TYPE_DETECTED",
                    "message": "已识别聚合类型",
                    "fields": fields,
                }
                replay_result = {
                    "ok": True,
                    "message": "聚合广告回放成功",
                    "interstitial": {"required": True, "displayed": True},
                    "rewarded": {"required": False, "displayed": False},
                }

                with patch("gui.threading.Thread", ImmediateThread), patch.object(
                    app, "_automation_prepare_detection_sync", return_value=fields
                ), patch(
                    "gui.detect_aggregation_with_one_retry", return_value=detection
                ), patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app,
                    "_automation_submit_backend_sync",
                    return_value={"ok": True, "message": "后台已生效"},
                ) as submit, patch.object(
                    app, "_automation_replay_sync", return_value=replay_result
                ) as replay, patch.object(
                    app, "_automation_comment_success"
                ) as comment_success:
                    app._automation_extract_fields()
                    root.update()

                fill_asana.assert_called_once_with()
                submit.assert_called_once_with()
                replay.assert_called_once_with()
                comment_success.assert_called_once_with(replay_result)
                log = app.automation_log_text.get("1.0", tk.END)
                assert "高置信度且参数校验通过" in log
                assert "[1/3] 回填 Asana 描述" in log
                assert "[2/3] 接口提交适配后台" in log
                assert "[3/3] 重启应用并检测聚合回放" in log
        finally:
            root.destroy()

    def test_multi_id_replay_locks_success_and_only_advances_failed_type(self):
        root = tk.Tk()
        try:
            from gui import (
                APKToolApp,
                INTERSTITIAL,
                MULTI_ID_REPLAY_TIMEOUT_SECONDS,
                REWARDED,
            )

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._automation_fields = {
                    "最终判断": "MAX聚合（强关系证据确认）",
                    "归因平台": "AppsFlyer",
                    "插屏聚合id": "inter-1, inter-2",
                    "激励视频聚合id": "reward-1, reward-2, reward-3",
                }
                app._automation_prepare_replay_id_candidates()
                replay_results = [
                    {
                        "ok": False,
                        "code": "REPLAY_TIMEOUT",
                        "interstitial": {"required": True, "displayed": False},
                        "rewarded": {"required": True, "displayed": False},
                    },
                    {
                        "ok": False,
                        "code": "REPLAY_TIMEOUT",
                        "interstitial": {
                            "required": True,
                            "displayed": True,
                            "evidence": ["inter-2 displayed"],
                        },
                        "rewarded": {"required": True, "displayed": False},
                    },
                    {
                        "ok": True,
                        "code": "AGGREGATION_REPLAY_SUCCESS",
                        "interstitial": {"required": False, "displayed": False},
                        "rewarded": {
                            "required": True,
                            "displayed": True,
                            "evidence": ["reward-3 displayed"],
                        },
                    },
                ]

                with patch.object(
                    app,
                    "_automation_replay_sync",
                    side_effect=replay_results,
                ) as replay, patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app,
                    "_automation_submit_backend_sync",
                    return_value={"ok": True, "message": "后台已生效"},
                ) as submit:
                    result = app._automation_replay_with_id_rotation_sync()

                assert result["ok"] is True
                assert result["interstitial"]["displayed"] is True
                assert result["rewarded"]["displayed"] is True
                assert app._automation_fields["插屏聚合id"] == "inter-2"
                assert app._automation_fields["激励视频聚合id"] == "reward-3"
                assert replay.call_args_list == [
                    call(timeout_seconds=MULTI_ID_REPLAY_TIMEOUT_SECONDS),
                    call(
                        required_types={INTERSTITIAL, REWARDED},
                        timeout_seconds=MULTI_ID_REPLAY_TIMEOUT_SECONDS,
                    ),
                    call(
                        required_types={REWARDED},
                        timeout_seconds=MULTI_ID_REPLAY_TIMEOUT_SECONDS,
                    ),
                ]
                assert fill_asana.call_count == 2
                assert submit.call_count == 2
        finally:
            root.destroy()

    def test_single_id_replay_retries_only_transient_failed_type(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._automation_fields = {
                    "最终判断": "MAX聚合",
                    "归因平台": "Adjust",
                    "插屏聚合id": "inter-only",
                    "激励视频聚合id": "reward-only",
                }
                replay_results = [
                    {
                        "ok": False,
                        "code": "REPLAY_TIMEOUT",
                        "interstitial": {
                            "required": True,
                            "displayed": True,
                            "evidence": ["inter displayed"],
                        },
                        "rewarded": {
                            "required": True,
                            "displayed": False,
                            "errors": ["No Fill：本次请求无广告填充"],
                        },
                    },
                    {
                        "ok": True,
                        "code": "AGGREGATION_REPLAY_SUCCESS",
                        "interstitial": {"required": False, "displayed": False},
                        "rewarded": {
                            "required": True,
                            "displayed": True,
                            "evidence": ["reward displayed"],
                        },
                    },
                ]
                with patch.object(
                    app, "_automation_replay_sync", side_effect=replay_results
                ) as replay, patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app, "_automation_submit_backend_sync"
                ) as submit:
                    result = app._automation_replay_with_id_rotation_sync()

                assert result["ok"] is True
                assert result["interstitial"]["displayed"] is True
                assert result["rewarded"]["displayed"] is True
                assert replay.call_args_list == [
                    call(),
                    call(required_types={"rewarded"}),
                ]
                fill_asana.assert_not_called()
                submit.assert_not_called()
        finally:
            root.destroy()

    def test_single_id_transient_retry_stops_after_one_retry(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp, REWARDED

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._automation_fields = {
                    "最终判断": "MAX聚合",
                    "归因平台": "Adjust",
                    "插屏聚合id": "inter-only",
                    "激励视频聚合id": "reward-only",
                }
                failed = {
                    "ok": False,
                    "code": "REPLAY_TIMEOUT",
                    "interstitial": {
                        "required": True,
                        "displayed": True,
                        "evidence": ["inter displayed"],
                    },
                    "rewarded": {
                        "required": True,
                        "displayed": False,
                        "errors": ["No Fill：本次请求无广告填充"],
                    },
                }
                with patch.object(
                    app, "_automation_replay_sync", return_value=failed
                ) as replay:
                    result = app._automation_replay_with_id_rotation_sync()

                assert result["ok"] is False
                assert result["code"] == "REPLAY_ID_CANDIDATES_EXHAUSTED"
                assert result["interstitial"]["displayed"] is True
                assert replay.call_args_list == [
                    call(),
                    call(required_types={REWARDED}),
                ]
        finally:
            root.destroy()

    def test_inferred_ironsource_replay_failure_clears_backend_and_updates_asana(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            fields = {
                "ok": True,
                "最终判断": "IronSource聚合（根据 video/inter 自动推断）",
                "_aggregation_type_inferred": True,
                "初始Activity": "com.demo.MainActivity",
                "应用类型": "Native",
                "归因平台": "Adjust",
                "激励视频聚合id": "video",
                "插屏聚合id": "inter",
            }
            detection = {
                "ok": True,
                "code": "AGGREGATION_TYPE_DETECTED",
                "message": "已识别聚合类型",
                "fields": fields,
            }
            replay = {
                "ok": False,
                "code": "REPLAY_TIMEOUT",
                "message": "回放监听超时",
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.inferred.game")
                with patch.object(
                    app, "_automation_prepare_detection_sync", return_value=fields
                ), patch(
                    "gui.detect_aggregation_with_one_retry", return_value=detection
                ), patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app,
                    "_automation_submit_backend_sync",
                    return_value={"ok": True, "message": "后台已生效"},
                ), patch.object(
                    app, "_automation_replay_sync", return_value=replay
                ), patch.object(
                    app,
                    "_automation_clear_inferred_backend_sync",
                    return_value={
                        "ok": True,
                        "message": "后台适配参数已全部清空",
                    },
                ) as clear_backend, patch.object(
                    app, "_automation_comment_failure"
                ) as comment:
                    result = app._automation_process_current_task_sync()

                assert result is False
                assert fill_asana.call_count == 2
                fill_asana.assert_any_call()
                fill_asana.assert_any_call(
                    terminal_note="未识别出聚合类型，暂不适配"
                )
                clear_backend.assert_called_once_with()
                assert comment.call_args.args[0] == "INFERRED_AGGREGATION_REPLAY_FAILED"
                assert "未识别出聚合类型，暂不适配" in comment.call_args.args[1]
                assert app._automation_status.cget("text") == "未识别出聚合类型，暂不适配"
        finally:
            root.destroy()

    def test_replay_explicit_max_clears_ironsource_then_runs_standard_max_tail(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            inferred = {
                "ok": True,
                "最终判断": "IronSource聚合（根据 video/inter 自动推断）",
                "_aggregation_type_inferred": True,
                "归因平台": "Adjust",
                "激励视频聚合id": "video",
                "插屏聚合id": "inter",
            }
            max_fields = {
                "ok": True,
                "最终判断": "MAX聚合（强关系证据确认）",
                "初始Activity": "com.demo.MainActivity",
                "应用类型": "Unity",
                "归因平台": "Adjust",
                "激励视频聚合id": "reward-max",
                "插屏聚合id": "inter-max",
                "SDK列表": [{"名称": "AppLovin", "key": "max-sdk-key"}],
            }
            replay = {
                "ok": False,
                "code": "AGGREGATION_TYPE_CHANGED_DURING_REPLAY",
                "detected_fields": max_fields,
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.demo")
                app._automation_fields = inferred
                with patch.object(
                    app,
                    "_automation_clear_inferred_backend_sync",
                    return_value={"ok": True, "message": "已清空"},
                ) as clear_backend, patch.object(
                    app, "_automation_execute_post_detection_sync", return_value=True
                ) as max_tail:
                    result = app._automation_handle_replay_type_change_sync(replay)

                assert result is True
                clear_backend.assert_called_once_with(
                    note="回放检测到 MAX，清理临时 IronSource 参数"
                )
                max_tail.assert_called_once_with()
                assert app._automation_fields["最终判断"].startswith("MAX聚合")
                assert app._automation_fields["激励视频聚合id"] == "reward-max"
                assert app._automation_fields["插屏聚合id"] == "inter-max"
                assert "_aggregation_type_inferred" not in app._automation_fields
                assert app._automation_fields["_aggregation_type_changed_during_replay"] is True
        finally:
            root.destroy()

    def test_preparation_requeues_when_device_health_reports_missing_app(self):
        root = tk.Tk()
        try:
            from device_health import DeviceHealthReport, HealthCheck
            from gui import APKToolApp, AutomationTaskRequeued

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.example.missing")
                app.automation_appid_var.set("up2-appid")
                report = DeviceHealthReport(
                    ok=False,
                    checks=(
                        HealthCheck("ADB", "ok", "可用"),
                        HealthCheck(
                            "目标应用",
                            "error",
                            "未安装: com.example.missing",
                        ),
                    ),
                )

                with patch.object(
                    app, "_automation_device_health_sync", return_value=report
                ):
                    with pytest.raises(
                        AutomationTaskRequeued,
                        match="未安装: com.example.missing",
                    ):
                        app._automation_prepare_detection_sync()
        finally:
            root.destroy()

    def test_batch_preparation_runs_full_adb_setup_and_writes_task_config(
        self, tmp_path
    ):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            config_path = tmp_path / "config.json"
            config_path.write_text('{"data": [{}]}', encoding="utf-8")
            (tmp_path / "zygote_build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            detected = {
                "ok": True,
                "最终判断": "MAX聚合",
                "归因平台": "AppsFlyer",
                "af_key": "af-key",
                "插屏聚合id": "inter-id",
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.batch.game")
                app.automation_appid_var.set("up2-appid")
                app.config_path_var.set(str(config_path))
                app.work_dir_var.set(str(tmp_path))
                app.task_uuid_var.set("test_snow")

                with patch("gui.check_device", return_value=True), \
                     patch("gui.get_app_uid", return_value=(True, "10123")), \
                     patch("gui.clear_logcat_buffer"), \
                     patch("gui.PackageRuntimeMonitor") as runtime_monitor, \
                     patch.object(
                         app,
                         "_automation_device_health_sync",
                         return_value=MagicMock(ok=True, checks=[]),
                     ), \
                     patch.object(app, "_automation_run_command_sync") as mock_command, \
                     patch.object(
                         app,
                         "_automation_extract_logcat_fields",
                         return_value=detected,
                     ):
                    runtime_monitor.return_value.poll.return_value = {"ok": True}
                    result = app._automation_prepare_detection_sync()
                    root.update()

                saved = json.loads(config_path.read_text(encoding="utf-8"))
                assert saved["data"][0]["packageName"] == "com.batch.game"
                assert saved["data"][0]["appId"] == "up2-appid"
                assert saved["data"][0]["taskUUID"] == "mediation_test_snow"
                # 自动化写入的最新检测配置会同步到手动 ADB 页，原有
                # 读取/写入按钮仍然保留并继续操作同一份 config.json。
                assert app.pkg_entry.get() == "com.batch.game"
                assert app.appid_entry.get() == "up2-appid"
                assert app.task_uuid_var.get() == "mediation_test_snow"
                assert app.uid_var.get() == "10123"
                assert result == detected
                assert mock_command.call_count == 3
        finally:
            root.destroy()

    def test_batch_preparation_relaunches_once_when_first_pid_is_not_confirmed(
        self, tmp_path
    ):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            config_path = tmp_path / "config.json"
            config_path.write_text('{"data": [{}]}', encoding="utf-8")
            (tmp_path / "zygote_build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            detected = {
                "ok": True,
                "最终判断": "MAX聚合",
                "归因平台": "Adjust",
                "插屏聚合id": "inter-id",
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.first.run.game")
                app.automation_appid_var.set("up2-appid")
                app.config_path_var.set(str(config_path))
                app.work_dir_var.set(str(tmp_path))

                with patch("gui.get_app_uid", return_value=(True, "10123")), \
                     patch("gui.get_app_bitness", return_value=(True, "64位")), \
                     patch("gui.clear_logcat_buffer"), \
                     patch("gui.dismiss_safe_interrupting_dialog"), \
                     patch("gui.PackageRuntimeMonitor") as runtime_monitor, \
                     patch.object(
                         app,
                         "_automation_device_health_sync",
                         return_value=MagicMock(ok=True, checks=[]),
                     ), \
                     patch.object(app, "_automation_run_command_sync") as mock_command, \
                     patch.object(
                         app,
                         "_automation_extract_logcat_fields",
                         side_effect=[{"ok": True, "最终判断": ""}, detected],
                     ):
                    runtime_monitor.return_value.poll.side_effect = [
                        {
                            "ok": False,
                            "code": "APP_LAUNCH_NOT_CONFIRMED",
                            "message": "首次启动未检测到进程",
                            "summary": "permission controller hand-off",
                        },
                        {"ok": True, "code": "APP_RUNNING"},
                    ]
                    result = app._automation_prepare_detection_sync()
                    root.update()

                assert result == detected
                runtime_monitor.return_value.reset.assert_called_once_with()
                # Push, build and initial launch plus one force-stop/relaunch.
                assert mock_command.call_count == 5
                output = app.automation_log_text.get("1.0", tk.END)
                assert "自动重启复检" in output
        finally:
            root.destroy()

    def test_automation_cleanup_force_stops_current_package(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.automation_package_var.set("com.finished.game")
                with patch.object(app, "_automation_stop_active_logcat") as stop_logcat, \
                     patch.object(app, "_automation_run_command_sync") as run_command:
                    cleaned = app._automation_cleanup_current_app_sync(
                        "聚合回放检查结束"
                    )

                assert cleaned is True
                stop_logcat.assert_called_once_with("聚合回放检查结束")
                command = run_command.call_args.args[0]
                assert "force-stop" in command
                assert "com.finished.game" in command
                assert run_command.call_args.kwargs["respect_control"] is False
        finally:
            root.destroy()

    def test_batch_automation_uses_only_eligible_precheck_rows_in_order(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=f"com.auto{index}.game",
                    up2_appid=f"appid-{index}",
                    gp_link="",
                )
                for index in (1, 2, 3, 4)
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.4执行",
                    "tasks": tasks,
                })
                item_ids = list(app.precheck_task_tree.get_children())
                app._set_precheck_task_status(item_ids[0], "启动正常")
                app._set_precheck_task_status(item_ids[1], "已加黑")
                app._set_precheck_task_status(item_ids[2], "安装完成")
                app._set_precheck_task_status(item_ids[3], "启动待复检")

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch.object(
                         app,
                         "_automation_process_current_task_sync",
                         side_effect=[True, False, True],
                     ) as mock_process, \
                     patch.object(app, "_automation_run_command_sync"):
                    app._automation_run_eligible_batch()

                assert mock_process.call_count == 3
                assert app.automation_package_var.get() == "com.auto4.game"
                output = app.automation_log_text.get("1.0", tk.END)
                assert "com.auto1.game" in output
                assert "com.auto2.game" not in output
                assert "com.auto3.game" in output
                assert "com.auto4.game" in output
                assert "成功 2，包体闪退 0，启动失败 0，自动化失败 1" in output
        finally:
            root.destroy()

    def test_unrelated_old_checkpoint_is_cleared_before_starting_current_batch(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from automation_checkpoint import new_batch_checkpoint
            from gui import APKToolApp

            current_task = AsanaPrecheckTask(
                gid="today-task",
                name="today-task",
                package_name="com.today.game",
                up2_appid="today-appid",
                gp_link="",
            )
            old_task = AsanaPrecheckTask(
                gid="old-task",
                name="old-task",
                package_name="com.old.game",
                up2_appid="old-appid",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.31执行",
                    "tasks": [current_task],
                })
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "安装完成")
                old_checkpoint = new_batch_checkpoint(
                    [("old-row", old_task)], replay_timeout_seconds=500
                )
                old_checkpoint = app._automation_checkpoint_store.mark_interrupted(
                    old_checkpoint, "工具窗口已关闭"
                )
                app._automation_checkpoint = old_checkpoint

                with patch.object(app, "_automation_start_batch_queue") as start:
                    started = app._automation_run_eligible_batch()

                assert started is True
                start.assert_called_once()
                stored = app._automation_checkpoint_store.load()
                assert stored["tasks"][0]["gid"] == "today-task"
                output = app.automation_log_text.get("1.0", tk.END)
                assert "已自动清理与当前任务列表无关的旧断点" in output
        finally:
            root.destroy()

    def test_related_checkpoint_blocks_batch_and_reports_reason_on_precheck_page(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from automation_checkpoint import new_batch_checkpoint
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="same-task",
                name="same-task",
                package_name="com.same.game",
                up2_appid="same-appid",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.31执行",
                    "tasks": [task],
                })
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "安装完成")
                checkpoint = new_batch_checkpoint(
                    [(item_id, task)], replay_timeout_seconds=500
                )
                checkpoint = app._automation_checkpoint_store.mark_interrupted(
                    checkpoint, "工具窗口已关闭"
                )
                app._automation_checkpoint = checkpoint

                with patch.object(app, "_automation_start_batch_queue") as start:
                    started = app._automation_run_eligible_batch()

                assert started is False
                start.assert_not_called()
                assert "恢复或放弃恢复记录" in app._precheck_status.cget("text")
                assert app._automation_checkpoint_store.load()["tasks"][0]["gid"] == "same-task"
        finally:
            root.destroy()

    def test_g99_batch_also_includes_failed_crashed_and_manual_review_rows(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            statuses = [
                "安装失败",
                "包体闪退",
                "待人工检查",
                "启动正常",
                "启动失败",
                "已加黑",
            ]
            tasks = [
                AsanaPrecheckTask(
                    gid=f"g99-task-{index}",
                    name=f"g99-task-{index}",
                    package_name=f"com.g99.{index}",
                    up2_appid=f"g99-appid-{index}",
                    gp_link="",
                )
                for index in range(len(statuses))
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.28执行",
                    "tasks": tasks,
                })
                item_ids = list(app.precheck_task_tree.get_children())
                for item_id, status in zip(item_ids, statuses):
                    app._set_precheck_task_status(item_id, status)

                ordinary = app._automation_eligible_precheck_tasks({
                    "is_g99": False,
                })
                g99 = app._automation_eligible_precheck_tasks({
                    "is_g99": True,
                })

                assert [task.package_name for _, task in ordinary] == [
                    "com.g99.3"
                ]
                assert [task.package_name for _, task in g99] == [
                    "com.g99.0",
                    "com.g99.1",
                    "com.g99.2",
                    "com.g99.3",
                    "com.g99.4",
                ]
        finally:
            root.destroy()

    def test_g99_batch_installs_missing_package_before_adaptation(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="g99-install-task",
                name="g99-install-task",
                package_name="com.g99.install",
                up2_appid="g99-install-appid",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.28执行",
                    "tasks": [task],
                })
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "安装失败")

                with patch("gui.is_package_installed", return_value=False), \
                     patch(
                         "gui.download_and_install_apkcombo",
                         return_value={"ok": True, "code": "APKCOMBO_INSTALLED"},
                     ) as install:
                    prepared = app._automation_prepare_g99_task_sync(
                        item_id,
                        task,
                        {"is_g99": True},
                    )

                assert prepared is True
                install.assert_called_once()
                assert install.call_args.args[0] == "com.g99.install"
        finally:
            root.destroy()

    @pytest.mark.parametrize("status", ["包体闪退", "启动失败"])
    def test_g99_batch_reinstalls_known_bad_package_before_adaptation(self, status):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="g99-reinstall-task",
                name="g99-reinstall-task",
                package_name="com.g99.reinstall",
                up2_appid="g99-reinstall-appid",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.28执行",
                    "tasks": [task],
                })
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, status)

                with patch("gui.is_package_installed", return_value=True), \
                     patch(
                         "gui.download_and_install_apkcombo",
                         return_value={"ok": True, "code": "APKCOMBO_INSTALLED"},
                     ) as install:
                    prepared = app._automation_prepare_g99_task_sync(
                        item_id,
                        task,
                        {"is_g99": True},
                    )

                assert prepared is True
                install.assert_called_once_with(
                    "com.g99.reinstall",
                    on_progress=install.call_args.kwargs["on_progress"],
                    force_reinstall=True,
                )
        finally:
            root.destroy()

    def test_g99_batch_skips_failed_install_and_continues_next_task(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"g99-continue-{index}",
                    name=f"g99-continue-{index}",
                    package_name=f"com.g99.continue{index}",
                    up2_appid=f"g99-continue-appid-{index}",
                    gp_link="",
                )
                for index in (1, 2)
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.28执行",
                    "tasks": tasks,
                })
                item_ids = list(app.precheck_task_tree.get_children())
                for item_id in item_ids:
                    app._set_precheck_task_status(item_id, "安装失败")

                with patch(
                    "gui.get_connected_device_profile",
                    return_value={"is_g99": True},
                ), patch("gui.threading.Thread", ImmediateThread), patch.object(
                    app,
                    "_automation_prepare_g99_task_sync",
                    side_effect=[False, True],
                ) as prepare, patch.object(
                    app,
                    "_automation_process_current_task_sync",
                    return_value=True,
                ) as process, patch.object(app, "_automation_run_command_sync"):
                    app._automation_run_eligible_batch()

                assert prepare.call_count == 2
                assert process.call_count == 1
                output = app.automation_log_text.get("1.0", tk.END)
                assert "com.g99.continue1" in output
                assert "com.g99.continue2" in output
                assert "成功 1，包体闪退 0，启动失败 0，自动化失败 1" in output
        finally:
            root.destroy()

    def test_batch_summary_separates_crash_from_automation_failure(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="crashed-task",
                name="crashed-task",
                package_name="com.crashed.game",
                up2_appid="crashed-appid",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.28执行",
                    "tasks": [task],
                })
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "启动正常")

                def process_crashed_task():
                    app._automation_last_result_code = "APP_CRASHED"
                    return False

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch.object(
                         app,
                         "_automation_process_current_task_sync",
                         side_effect=process_crashed_task,
                     ), \
                     patch.object(app, "_automation_run_command_sync"):
                    app._automation_run_eligible_batch()

                output = app.automation_log_text.get("1.0", tk.END)
                assert "包体闪退 1" in output
                assert "自动化失败 0" in output
        finally:
            root.destroy()

    def test_combined_precheck_starts_automation_only_after_successful_batch(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="combined-task",
                name="combined-task",
                package_name="com.combined.game",
                up2_appid="combined-appid",
                gp_link="https://play.google.com/store/apps/details?id=com.combined.game",
            )
            page_result = {
                "code": "HAS_ADS",
                "title": "检测到包含广告",
                "detail": "继续",
                "continue_adaptation": True,
                "package_name": task.package_name,
                "source": "UI 控件",
                "evidence": [],
                "visible_texts": ["Contains ads", "Install"],
            }
            installed_result = {
                **page_result,
                "install_result": {
                    "ok": True,
                    "code": "INSTALLED",
                    "message": "安装完成",
                },
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.18执行", "tasks": [task]}
                )
                app.precheck_launch_check_var.set(False)

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch(
                         "gui.get_asana_tasks_for_date",
                         return_value={"section_name": "8.18执行", "tasks": [task]},
                     ), \
                     patch("gui.run_google_play_precheck", return_value=page_result), \
                     patch.object(
                         app, "_install_after_precheck", return_value=installed_result
                     ), \
                     patch.object(
                         app,
                         "_launch_check_after_install",
                         side_effect=lambda result, _seconds: {
                             **result,
                             "launch_result": {
                                 "ok": True,
                                 "code": "LAUNCH_OK",
                                 "message": "启动预检通过",
                             },
                         },
                     ), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_automation_run_eligible_batch") as run_batch:
                    app._on_start_precheck_then_automation()

                item_id = app.precheck_task_tree.get_children()[0]
                assert app.precheck_task_tree.item(item_id, "values")[3] == "启动正常"
                run_batch.assert_called_once_with()
                assert "开始自动适配 1 个" in app._precheck_status.cget("text")
        finally:
            root.destroy()

    def test_combined_precheck_refreshes_asana_before_building_incremental_queue(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            old_task = AsanaPrecheckTask(
                gid="old-complete",
                name="old-complete",
                package_name="com.old.complete",
                up2_appid="old",
                gp_link="https://play.google.com/store/apps/details?id=com.old.complete",
                completed=True,
            )
            new_task = AsanaPrecheckTask(
                gid="new-since-refresh",
                name="new-since-refresh",
                package_name="com.new.today",
                up2_appid="new",
                gp_link="https://play.google.com/store/apps/details?id=com.new.today",
            )
            page_result = {
                "code": "HAS_ADS",
                "title": "检测到包含广告",
                "detail": "继续",
                "continue_adaptation": True,
                "package_name": new_task.package_name,
                "source": "UI 控件",
                "evidence": [],
                "visible_texts": ["Contains ads", "Install"],
            }
            installed_result = {
                **page_result,
                "install_result": {
                    "ok": True,
                    "code": "INSTALLED",
                    "message": "安装完成",
                },
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.31执行", "tasks": [old_task]}
                )
                app.precheck_launch_check_var.set(False)

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch(
                         "gui.get_asana_tasks_for_date",
                         return_value={
                             "section_name": "8.31执行",
                             "tasks": [new_task, old_task],
                         },
                     ), \
                     patch("gui.run_google_play_precheck", return_value=page_result) as precheck, \
                     patch.object(
                         app, "_install_after_precheck", return_value=installed_result
                     ), \
                     patch.object(
                         app,
                         "_launch_check_after_install",
                         side_effect=lambda result, _seconds: {
                             **result,
                             "launch_result": {
                                 "ok": True,
                                 "code": "LAUNCH_OK",
                                 "message": "启动预检通过",
                             },
                         },
                     ), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_automation_run_eligible_batch"):
                    app._on_start_precheck_then_automation()

                precheck.assert_called_once_with(
                    new_task.gp_link,
                    verify_apkcombo=True,
                )
                packages = [
                    app.precheck_task_tree.item(item_id, "values")[1]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                assert packages == ["com.new.today", "com.old.complete"]
        finally:
            root.destroy()

    def test_precheck_result_is_rendered(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_precheck_result({
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "可以继续。",
                    "continue_adaptation": True,
                    "package_name": "com.example.game",
                    "source": "UI 控件",
                    "evidence": ["发现包含广告标识"],
                    "visible_texts": ["包含广告", "安装"],
                })

                output = app.precheck_output.get("1.0", tk.END)
                assert "继续下载安装和适配" in output
                assert "com.example.game" in output
                assert "发现包含广告标识" in output
        finally:
            root.destroy()

    def test_unlabeled_precheck_result_continues_to_automatic_adaptation(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            from auto_asana.main import AsanaPrecheckTask

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_precheck_result({
                    "code": "NO_ADS_OR_IAP",
                    "title": "未发现广告或应用内购标识（继续自动适配）",
                    "detail": "不能据此加黑，将继续下载安装。",
                    "continue_adaptation": True,
                    "package_name": "com.example.review",
                    "source": "UI 控件",
                })

                output = app.precheck_output.get("1.0", tk.END)
                assert "继续下载安装，启动检查后自动适配；不自动加黑" in output
                assert "加黑并跳过" not in output
                assert APKToolApp._precheck_task_status_for_result({
                    "code": "NO_ADS_OR_IAP",
                    "install_result": {
                        "ok": True,
                        "code": "INSTALLED",
                    },
                }) == "安装完成"
                task = AsanaPrecheckTask(
                    gid="review-task", name="test", package_name="com.example.review",
                    up2_appid="appid", gp_link="",
                )
                app._precheck_tasks = {"review-row": task}
                app.precheck_task_tree.insert(
                    "", "end", iid="review-row",
                    values=(1, task.package_name, "appid", "安装完成"),
                )
                assert app._automation_eligible_precheck_tasks() == [("review-row", task)]
        finally:
            root.destroy()

    def test_japanese_package_result_is_blacklisted_and_not_downloaded(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            result = {
                "code": "JAPANESE_PACKAGE",
                "title": "检测到日本包体",
                "detail": "包名包含 jp 段且页面包含明显日文内容。",
                "continue_adaptation": False,
                "package_name": "jp.co.barows.kenshowalkprotect",
                "source": "UI 控件",
            }
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_precheck_result(result)

                assert APKToolApp._precheck_task_status_for_result(result) == "已加黑"
                assert "加黑并跳过" in app.precheck_output.get("1.0", tk.END)
                with patch("gui.install_google_play_app") as install:
                    assert app._install_after_precheck(result) == result
                install.assert_not_called()
        finally:
            root.destroy()

    def test_apkcombo_available_result_uses_automatic_third_party_install(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            result = {
                "code": "APKCOMBO_AVAILABLE",
                "continue_adaptation": False,
                "package_name": "com.example.game",
            }
            installed = {
                "ok": True,
                "code": "APKCOMBO_INSTALLED",
                "message": "安装完成",
            }
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                with patch(
                    "gui.download_and_install_apkcombo", return_value=installed
                ) as install:
                    output = app._install_after_precheck(result)

            install.assert_called_once()
            assert output["install_result"] == installed
            assert APKToolApp._precheck_task_status_for_result(output) == "安装完成"
        finally:
            root.destroy()

    def test_launch_crash_result_maps_to_asana_comment_and_status(self):
        from gui import APKToolApp

        result = {
            "package_name": "com.example.game",
            "launch_result": {
                "ok": False,
                "code": "APP_CRASHED",
                "message": "包体闪退，暂不适配",
                "summary": "FATAL EXCEPTION: main",
            },
        }

        assert APKToolApp._precheck_task_status_for_result(result) == "包体闪退"
        comment_result = APKToolApp._comment_result_for_precheck(result)
        assert comment_result["code"] == "APP_CRASHED"
        assert "FATAL EXCEPTION" in comment_result["detail"]

    def test_launch_precheck_retries_once_and_uses_second_success(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            source = {
                "package_name": "com.example.game",
                "install_result": {"ok": True, "code": "INSTALLED"},
            }
            first = {
                "ok": False,
                "code": "APP_EXITED",
                "message": "应用启动后进程退出",
            }
            second = {
                "ok": True,
                "code": "LAUNCH_OK",
                "message": "应用持续运行 20 秒",
            }
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                with patch(
                    "gui.run_app_launch_precheck",
                    side_effect=[first, second],
                ) as launch, patch("gui.time.sleep"):
                    result = app._launch_check_after_install(source, 20)

            assert launch.call_count == 2
            assert result["launch_result"]["ok"] is True
            assert result["launch_result"]["code"] == "LAUNCH_OK"
            assert result["launch_result"]["recovered_after_retry"] is True
            assert result["launch_result"]["first_attempt"] == first
            assert APKToolApp._precheck_task_status_for_result(result) == "启动正常"
        finally:
            root.destroy()

    def test_launch_precheck_requires_two_failures_before_terminal_status(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            source = {
                "package_name": "com.example.game",
                "install_result": {"ok": True, "code": "INSTALLED"},
            }
            first = {
                "ok": False,
                "code": "APP_EXITED",
                "message": "首次进程退出",
            }
            second = {
                "ok": False,
                "code": "APP_CRASHED",
                "message": "包体闪退，暂不适配",
                "summary": "FATAL EXCEPTION: main",
            }
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                with patch(
                    "gui.run_app_launch_precheck",
                    side_effect=[first, second],
                ) as launch, patch("gui.time.sleep"):
                    result = app._launch_check_after_install(source, 20)

            assert launch.call_count == 2
            assert result["launch_result"]["ok"] is False
            assert result["launch_result"]["code"] == "APP_CRASHED"
            assert result["launch_result"]["attempts"] == 2
            assert "连续两次" in result["launch_result"]["message"]
            assert APKToolApp._precheck_task_status_for_result(result) == "包体闪退"
        finally:
            root.destroy()

    def test_batch_precheck_skips_completed_and_comments_results(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid="done",
                    name="done",
                    package_name="com.done.game",
                    up2_appid="",
                    gp_link="https://play.google.com/store/apps/details?id=com.done.game",
                    completed=True,
                ),
                AsanaPrecheckTask(
                    gid="iap",
                    name="iap",
                    package_name="com.iap.game",
                    up2_appid="",
                    gp_link="https://play.google.com/store/apps/details?id=com.iap.game",
                ),
                AsanaPrecheckTask(
                    gid="ads",
                    name="ads",
                    package_name="com.ads.game",
                    up2_appid="",
                    gp_link="https://play.google.com/store/apps/details?id=com.ads.game",
                ),
            ]
            results = [
                {
                    "code": "IAP_ONLY",
                    "title": "仅检测到应用内购",
                    "detail": "应加黑",
                    "continue_adaptation": False,
                    "package_name": "com.iap.game",
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["In-app purchases", "Install"],
                },
                {
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "继续",
                    "continue_adaptation": True,
                    "package_name": "com.ads.game",
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["Contains ads", "Install"],
                },
            ]

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "7.30执行",
                    "tasks": tasks,
                })
                app.precheck_auto_install_var.set(False)

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch("gui.run_google_play_precheck", side_effect=results) as mock_precheck, \
                     patch("gui.submit_precheck_blacklist_via_api", return_value={
                         "ok": True,
                         "code": "PRECHECK_BLACKLIST_SUBMITTED",
                         "message": "已提交、刷新缓存并回读确认",
                     }), \
                    patch("gui.add_precheck_comment_once") as mock_comment:
                    app._on_start_batch_precheck()

                assert mock_precheck.call_count == 2
                assert [call.args[0] for call in mock_precheck.call_args_list] == [
                    tasks[1].gp_link,
                    tasks[2].gp_link,
                ]
                assert [call.args[1] for call in mock_comment.call_args_list] == [
                    "iap",
                    "ads",
                ]
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                assert statuses == ["已完成", "已加黑(后台)", "有广告"]
                assert "批量预检完成" in app._precheck_status.cget("text")
        finally:
            root.destroy()

    def test_refresh_restores_comment_status_and_skips_terminal_issue(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid="issue",
                    name="issue",
                    package_name="com.issue.game",
                    up2_appid="",
                    gp_link="",
                    workflow_status="包体闪退",
                    workflow_terminal=True,
                ),
                AsanaPrecheckTask(
                    gid="pending",
                    name="pending",
                    package_name="com.pending.game",
                    up2_appid="",
                    gp_link="",
                ),
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.12执行", "tasks": tasks}
                )
                item_ids = list(app.precheck_task_tree.get_children())
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in item_ids
                ]
                queue, _start = app._precheck_batch_queue_from_selection()

                assert statuses == ["包体闪退", "新增待预检"]
                assert [task.gid for _item_id, task in queue] == ["pending"]
                assert "评论恢复 1 个状态" in app._precheck_asana_status.cget("text")
        finally:
            root.destroy()

    def test_refresh_keeps_restored_apkcombo_result_in_batch_queue(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="apkcombo-task",
                name="apkcombo",
                package_name="com.example.restricted",
                up2_appid="app-id",
                gp_link=(
                    "https://play.google.com/store/apps/details?"
                    "id=com.example.restricted"
                ),
                workflow_status="APKCombo有包",
                workflow_terminal=False,
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.21执行", "tasks": [task]}
                )
                item_id = app.precheck_task_tree.get_children()[0]
                queue, _start = app._precheck_batch_queue_from_selection()

                assert app.precheck_task_tree.item(item_id, "values")[3] == "APKCombo有包"
                assert [queued.gid for _item, queued in queue] == ["apkcombo-task"]
        finally:
            root.destroy()

    def test_pending_rows_without_new_tasks_enter_batch_precheck_queue(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            # "待处理" = 已从 Asana 读取过（历史上记为已见）但从未页面预检。
            tasks = [
                AsanaPrecheckTask(
                    gid="pending-old",
                    name="pending-old",
                    package_name="com.pending.old",
                    up2_appid="",
                    gp_link="",
                ),
                AsanaPrecheckTask(
                    gid="installed-old",
                    name="installed-old",
                    package_name="com.installed.old",
                    up2_appid="",
                    gp_link="",
                    workflow_status="已安装",
                ),
                AsanaPrecheckTask(
                    gid="pending-old-2",
                    name="pending-old-2",
                    package_name="com.pending.old2",
                    up2_appid="",
                    gp_link="",
                ),
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._settings["precheck_seen_task_gids"] = {
                    app._precheck_seen_key(): [
                        "pending-old",
                        "installed-old",
                        "pending-old-2",
                    ]
                }
                app._render_today_asana_tasks(
                    {"section_name": "8.31执行", "tasks": tasks}
                )
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                queue, _start = app._precheck_batch_queue_from_selection()

                assert statuses == ["待处理", "已安装", "待处理"]
                assert [task.gid for _item, task in queue] == [
                    "pending-old",
                    "pending-old-2",
                ]
                # "批量自动适配预检合格任务"仍不得直接接收待处理任务。
                assert [
                    task.gid for _item, task in app._automation_eligible_precheck_tasks(
                        {"is_g99": False}
                    )
                ] == ["installed-old"]
                assert [
                    task.gid for _item, task in app._automation_eligible_precheck_tasks(
                        {"is_g99": True}
                    )
                ] == ["installed-old"]
        finally:
            root.destroy()

    def test_restored_manual_review_rows_are_pending_and_queueable(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="manual-review-restored",
                name="manual-review-restored",
                package_name="com.manual.review.restored",
                up2_appid="",
                gp_link="https://play.google.com/store/apps/details?id=com.manual.review.restored",
                workflow_status="待处理",
                workflow_terminal=False,
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "9.3执行", "tasks": [task]}
                )
                item_id = app.precheck_task_tree.get_children()[0]
                queue, _start = app._precheck_batch_queue_from_selection()

                assert app.precheck_task_tree.item(item_id, "values")[3] == "待处理"
                assert [queued.gid for _item, queued in queue] == [task.gid]
        finally:
            root.destroy()

    def test_pending_rows_join_batch_precheck_alongside_new_tasks(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid="brand-new",
                    name="brand-new",
                    package_name="com.brand.new",
                    up2_appid="",
                    gp_link="",
                ),
                AsanaPrecheckTask(
                    gid="pending-old",
                    name="pending-old",
                    package_name="com.pending.old",
                    up2_appid="",
                    gp_link="",
                ),
                AsanaPrecheckTask(
                    gid="launched-old",
                    name="launched-old",
                    package_name="com.launched.old",
                    up2_appid="",
                    gp_link="",
                    workflow_status="启动正常",
                ),
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._settings["precheck_seen_task_gids"] = {
                    app._precheck_seen_key(): [
                        "pending-old",
                        "launched-old",
                    ]
                }
                app._render_today_asana_tasks(
                    {"section_name": "8.31执行", "tasks": tasks}
                )
                queue, _start = app._precheck_batch_queue_from_selection()

                assert [task.gid for _item, task in queue] == [
                    "brand-new",
                    "pending-old",
                ]
        finally:
            root.destroy()

    def test_combined_precheck_ignores_search_selection_and_scans_all_pending_rows(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"pending-{index}",
                    name=f"pending-{index}",
                    package_name=f"com.pending.{index}",
                    up2_appid=f"appid-{index}",
                    gp_link="",
                )
                for index in range(1, 4)
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "9.3执行", "tasks": tasks}
                )
                item_ids = list(app.precheck_task_tree.get_children())
                # Simulate the user searching for and selecting the last row.
                app.precheck_task_tree.selection_set(item_ids[-1])
                app._precheck_manual_selection = True

                queue, start = app._precheck_batch_queue_from_selection(
                    ignore_selection=True
                )

                assert start == 1
                assert [task.gid for _item, task in queue] == [
                    "pending-1",
                    "pending-2",
                    "pending-3",
                ]
        finally:
            root.destroy()

    def test_combined_entry_prechecks_pending_rows_then_adapts(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="pending-combined",
                name="pending-combined",
                package_name="com.pending.combined",
                up2_appid="",
                gp_link=(
                    "https://play.google.com/store/apps/details?"
                    "id=com.pending.combined"
                ),
            )
            page_result = {
                "code": "HAS_ADS",
                "title": "检测到包含广告",
                "detail": "继续",
                "continue_adaptation": True,
                "package_name": task.package_name,
                "source": "UI 控件",
                "evidence": [],
                "visible_texts": ["Contains ads", "Install"],
            }
            installed_result = {
                **page_result,
                "install_result": {
                    "ok": True,
                    "code": "INSTALLED",
                    "message": "安装完成",
                },
            }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._settings["precheck_seen_task_gids"] = {
                    app._precheck_seen_key(): ["pending-combined"]
                }
                app.precheck_launch_check_var.set(False)

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch(
                         "gui.get_asana_tasks_for_date",
                         return_value={"section_name": "8.31执行", "tasks": [task]},
                     ), \
                     patch(
                         "gui.run_google_play_precheck", return_value=page_result
                     ) as mock_precheck, \
                     patch.object(
                         app, "_install_after_precheck", return_value=installed_result
                     ), \
                     patch.object(
                         app,
                         "_launch_check_after_install",
                         side_effect=lambda result, _seconds: {
                             **result,
                             "launch_result": {
                                 "ok": True,
                                 "code": "LAUNCH_OK",
                                 "message": "启动预检通过",
                             },
                         },
                     ), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_automation_run_eligible_batch") as run_batch:
                    app._on_start_precheck_then_automation()

                # 读取后仍为待处理，但组合入口必须先预检再进入适配队列。
                assert mock_precheck.call_count == 1
                assert mock_precheck.call_args[0] == (task.gp_link,)
                item_id = app.precheck_task_tree.get_children()[0]
                assert app.precheck_task_tree.item(item_id, "values")[3] == "启动正常"
                run_batch.assert_called_once_with()
        finally:
            root.destroy()

    def test_refresh_preserves_local_status_when_comment_has_no_decision(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="same-task",
                name="same",
                package_name="com.same.game",
                up2_appid="",
                gp_link="",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                result = {"section_name": "8.12执行", "tasks": [task]}
                app._render_today_asana_tasks(result)
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "安装完成")

                app._render_today_asana_tasks(result)

                refreshed = app.precheck_task_tree.get_children()[0]
                assert app.precheck_task_tree.item(refreshed, "values")[3] == "安装完成"
        finally:
            root.destroy()

    def test_refresh_reconciles_late_background_installation(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="late-install",
                name="late-install",
                package_name="com.Blox.Tower.Slap.Master",
                up2_appid="",
                gp_link=(
                    "https://play.google.com/store/apps/details?id="
                    "com.Blox.Tower.Slap.Master"
                ),
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                result = {"section_name": "8.19执行", "tasks": [task]}
                app._render_today_asana_tasks(result)
                item_id = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(item_id, "后台下载中")

                with patch("gui.is_package_installed", return_value=True) as installed:
                    app._render_today_asana_tasks(result)

                refreshed = app.precheck_task_tree.get_children()[0]
                assert (
                    app.precheck_task_tree.item(refreshed, "values")[3]
                    == "安装完成"
                )
                installed.assert_called_once_with("com.Blox.Tower.Slap.Master")
                assert "1 个后台下载包体安装完成" in app._precheck_status.cget(
                    "text"
                )
        finally:
            root.destroy()

    def test_refresh_queues_only_tasks_added_after_the_daily_baseline(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            old_task = AsanaPrecheckTask(
                gid="old-task",
                name="old",
                package_name="com.old.game",
                up2_appid="",
                gp_link="https://play.google.com/store/apps/details?id=com.old.game",
            )
            new_task = AsanaPrecheckTask(
                gid="new-task",
                name="new",
                package_name="com.new.game",
                up2_appid="",
                gp_link="https://play.google.com/store/apps/details?id=com.new.game",
            )
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.13执行", "tasks": [old_task]}
                )
                old_item = app.precheck_task_tree.get_children()[0]
                app._set_precheck_task_status(old_item, "有广告")

                app._render_today_asana_tasks(
                    {"section_name": "8.13执行", "tasks": [new_task, old_task]}
                )
                queue, _start = app._precheck_batch_queue_from_selection()

                assert [task.gid for _item, task in queue] == ["new-task"]
                assert "本次新增 1 个" in app._precheck_asana_status.cget("text")
        finally:
            root.destroy()

    def test_batch_precheck_starts_from_selected_row(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=f"com.selected{index}.game",
                    up2_appid="",
                    gp_link=(
                        "https://play.google.com/store/apps/details?id="
                        f"com.selected{index}.game"
                    ),
                )
                for index in (1, 2, 3)
            ]
            results = [
                {
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "继续",
                    "continue_adaptation": True,
                    "package_name": task.package_name,
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["Contains ads", "Install"],
                }
                for task in tasks[1:]
            ]

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.4执行",
                    "tasks": tasks,
                })
                item_ids = list(app.precheck_task_tree.get_children())
                app.precheck_task_tree.selection_set(item_ids[1])
                app.precheck_task_tree.focus(item_ids[1])
                app.precheck_auto_install_var.set(False)

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch(
                         "gui.run_google_play_precheck", side_effect=results
                     ) as mock_precheck, \
                     patch("gui.add_precheck_comment_once"):
                    app._on_start_batch_precheck()

                assert [call.args[0] for call in mock_precheck.call_args_list] == [
                    tasks[1].gp_link,
                    tasks[2].gp_link,
                ]
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in item_ids
                ]
                assert statuses == ["新增待预检", "有广告", "有广告"]
        finally:
            root.destroy()

    def test_batch_auto_download_revisits_items_deferred_by_download_limit(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=f"com.game{index}.app",
                    up2_appid="",
                    gp_link=f"https://play.google.com/store/apps/details?id=com.game{index}.app",
                )
                for index in (1, 2)
            ]
            results = [
                {
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "继续",
                    "continue_adaptation": True,
                    "package_name": task.package_name,
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["Contains ads", "Install"],
                }
                for task in tasks
            ]

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "7.30执行",
                    "tasks": tasks,
                })
                app.precheck_auto_install_var.set(True)
                app.precheck_launch_check_var.set(False)
                app.precheck_download_limit_var.set("1")
                app.precheck_download_cooldown_var.set("0")

                def installed(result, return_after_start=False):
                    return {
                        **result,
                        "install_result": {
                            "ok": True,
                            "code": "INSTALLED",
                            "message": "安装完成",
                        },
                    }

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch("gui.run_google_play_precheck", side_effect=results), \
                     patch("gui.open_google_play_page", return_value=(True, "已打开", "")), \
                     patch.object(app, "_install_after_precheck", side_effect=installed) as mock_install, \
                    patch("gui.add_precheck_comment_once"):
                    app._on_start_batch_precheck()

                assert mock_install.call_count == 2
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                assert statuses == ["安装完成", "安装完成"]
                assert "新增下载 2 个" in app._precheck_status.cget("text")
        finally:
            root.destroy()

    def test_batch_auto_download_does_not_wait_for_or_pause_after_large_download(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=f"com.large{index}.game",
                    up2_appid="",
                    gp_link=(
                        "https://play.google.com/store/apps/details?id="
                        f"com.large{index}.game"
                    ),
                )
                for index in (1, 2)
            ]
            page_results = [
                {
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "继续",
                    "continue_adaptation": True,
                    "package_name": task.package_name,
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["Contains ads", "Install"],
                }
                for task in tasks
            ]

            def started(result, return_after_start=False):
                assert return_after_start is True
                return {
                    **result,
                    "install_result": {
                        "ok": None,
                        "code": "DOWNLOAD_STARTED",
                        "message": "后台下载中",
                    },
                }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.4执行",
                    "tasks": tasks,
                })
                app.precheck_auto_install_var.set(True)
                app.precheck_launch_check_var.set(False)
                app.precheck_download_limit_var.set("6")
                app.precheck_download_cooldown_var.set("0")

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch("gui.run_google_play_precheck", side_effect=page_results), \
                     patch.object(
                         app, "_install_after_precheck", side_effect=started
                     ) as mock_install, \
                     patch.object(app, "_ensure_background_install_watcher"), \
                     patch("gui.is_package_installed", return_value=False), \
                     patch("gui.time.monotonic", side_effect=[0, 601]), \
                     patch("gui.time.sleep"), \
                     patch("gui.add_precheck_comment_once"):
                    app._on_start_batch_precheck()

                assert mock_install.call_count == 2
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                assert statuses == ["后台下载中", "后台下载中"]
                assert "新增下载 2 个" in app._precheck_status.cget("text")
        finally:
            root.destroy()

    def test_combined_precheck_forces_install_and_waits_before_automation(self):
        """The combined action must not stop after page inspection.

        Even when the optional launch-observation checkbox is off, a
        DOWNLOAD_STARTED result has to be polled until the package is
        installed so the follow-up automation queue can consume it.
        """
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            task = AsanaPrecheckTask(
                gid="combined-task",
                name="combined-task",
                package_name="com.combined.download",
                up2_appid="",
                gp_link=(
                    "https://play.google.com/store/apps/details?id="
                    "com.combined.download"
                ),
            )
            page_result = {
                "code": "HAS_ADS",
                "title": "检测到包含广告",
                "detail": "继续",
                "continue_adaptation": True,
                "package_name": task.package_name,
                "source": "UI 控件",
                "evidence": [],
                "visible_texts": ["Contains ads", "Install"],
            }

            def started(result, return_after_start=False):
                assert return_after_start is True
                return {
                    **result,
                    "install_result": {
                        "ok": None,
                        "code": "DOWNLOAD_STARTED",
                        "message": "后台下载中",
                    },
                }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.4执行",
                    "tasks": [task],
                })
                app.precheck_auto_install_var.set(False)
                app.precheck_launch_check_var.set(False)
                app.precheck_download_limit_var.set("6")
                app.precheck_download_cooldown_var.set("0")

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch(
                         "gui.get_asana_tasks_for_date",
                         return_value={"section_name": "8.4执行", "tasks": [task]},
                     ), \
                     patch("gui.run_google_play_precheck", return_value=page_result), \
                     patch.object(app, "_install_after_precheck", side_effect=started), \
                     patch("gui.is_package_installed", side_effect=[False, True]), \
                     patch.object(
                         app._background_install_watch_stop_event,
                         "wait",
                         return_value=False,
                     ), \
                     patch.object(
                         app,
                         "_launch_check_after_install",
                         side_effect=lambda result, _seconds: {
                             **result,
                             "launch_result": {
                                 "ok": True,
                                 "code": "LAUNCH_OK",
                                 "message": "启动预检通过",
                             },
                         },
                     ), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_automation_run_eligible_batch") as run_automation:
                    app._on_start_precheck_then_automation()

                run_automation.assert_called_once_with()
                item_id = app.precheck_task_tree.get_children()[0]
                assert app.precheck_task_tree.item(item_id, "values")[3] == "启动正常"
                assert "批量预检完成，开始自动适配 1 个合格任务" in app._precheck_status.cget("text")
        finally:
            root.destroy()

    def test_batch_monitors_all_downloads_and_checks_whichever_finishes_first(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"task-{index}",
                    name=f"task-{index}",
                    package_name=f"com.background{index}.game",
                    up2_appid="",
                    gp_link=(
                        "https://play.google.com/store/apps/details?id="
                        f"com.background{index}.game"
                    ),
                )
                for index in (1, 2)
            ]
            page_results = [
                {
                    "code": "HAS_ADS",
                    "title": "检测到包含广告",
                    "detail": "继续",
                    "continue_adaptation": True,
                    "package_name": task.package_name,
                    "source": "UI 控件",
                    "evidence": [],
                    "visible_texts": ["Contains ads", "Install"],
                }
                for task in tasks
            ]

            def started(result, return_after_start=False):
                return {
                    **result,
                    "install_result": {
                        "ok": None,
                        "code": "DOWNLOAD_STARTED",
                        "message": "后台下载中",
                    },
                }

            first_package_checks = 0

            def installed(package_name):
                nonlocal first_package_checks
                if package_name == "com.background1.game":
                    first_package_checks += 1
                    return first_package_checks > 1
                return True

            def launch_checked(result, observation_seconds):
                return {
                    **result,
                    "launch_result": {
                        "ok": True,
                        "code": "LAUNCH_OK",
                        "message": "启动正常",
                    },
                }

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks({
                    "section_name": "8.4执行",
                    "tasks": tasks,
                })
                app.precheck_auto_install_var.set(True)
                app.precheck_launch_check_var.set(True)
                app.precheck_download_limit_var.set("6")
                app.precheck_download_cooldown_var.set("0")
                app.precheck_launch_observation_var.set("5")

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch("gui.run_google_play_precheck", side_effect=page_results), \
                     patch.object(app, "_install_after_precheck", side_effect=started), \
                     patch("gui.is_package_installed", side_effect=installed), \
                     patch.object(
                         app, "_launch_check_after_install", side_effect=launch_checked
                     ) as mock_launch, \
                     patch.object(
                         app._background_install_watch_stop_event,
                         "wait",
                         return_value=False,
                     ), \
                     patch("gui.add_precheck_comment_once"):
                    app._on_start_batch_precheck()

                assert [
                    call.args[0]["package_name"]
                    for call in mock_launch.call_args_list
                ] == ["com.background2.game", "com.background1.game"]
                statuses = [
                    app.precheck_task_tree.item(item_id, "values")[3]
                    for item_id in app.precheck_task_tree.get_children()
                ]
                assert statuses == ["启动正常", "启动正常"]
        finally:
            root.destroy()

    def test_background_install_watch_keeps_late_packages_and_resumes_each_one(self):
        root = tk.Tk()
        try:
            from auto_asana.main import AsanaPrecheckTask
            from gui import APKToolApp

            tasks = [
                AsanaPrecheckTask(
                    gid=f"late-{index}",
                    name=f"late-{index}",
                    package_name=f"com.late{index}.game",
                    up2_appid="",
                    gp_link="",
                )
                for index in (1, 2)
            ]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_today_asana_tasks(
                    {"section_name": "8.31执行", "tasks": tasks}
                )
                item_ids = list(app.precheck_task_tree.get_children())
                entries = []
                for item_id, task in zip(item_ids, tasks):
                    app._set_precheck_task_status(item_id, "后台下载中")
                    entries.append(
                        (
                            item_id,
                            task,
                            {
                                "code": "HAS_ADS",
                                "continue_adaptation": True,
                                "package_name": task.package_name,
                                "install_result": {
                                    "ok": None,
                                    "code": "DOWNLOAD_STARTED",
                                    "message": "后台下载中",
                                },
                            },
                        )
                    )

                installed = {task.package_name: False for task in tasks}
                with patch.object(app, "_ensure_background_install_watcher"), \
                     patch(
                         "gui.is_package_installed",
                         side_effect=lambda package: installed[package],
                     ), \
                     patch("gui.build_asana_client", return_value=MagicMock()), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_schedule_background_auto_adapt") as schedule:
                    app._enqueue_background_install_watch(
                        entries,
                        asana_pat="pat",
                        launch_check=False,
                        launch_observation=20,
                        auto_adapt=True,
                    )
                    app._poll_background_install_watch_once()
                    assert app._background_install_watch_count() == 2

                    installed[tasks[1].package_name] = True
                    app._poll_background_install_watch_once()
                    assert app._background_install_watch_count() == 1
                    assert app.precheck_task_tree.item(item_ids[1], "values")[3] == "安装完成"
                    assert app.precheck_task_tree.item(item_ids[0], "values")[3] == "后台下载中"

                    # There is deliberately no elapsed-time cutoff: the first
                    # package remains queued and succeeds on any later poll.
                    installed[tasks[0].package_name] = True
                    app._poll_background_install_watch_once()
                    assert app._background_install_watch_count() == 0
                    assert app.precheck_task_tree.item(item_ids[0], "values")[3] == "安装完成"
                    assert schedule.call_count == 2
        finally:
            root.destroy()


class TestGenerateQrAction:
    def test_generates_qr_and_updates_display(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                # 模拟输入 URL
                app.url_entry.insert(0, "https://play.google.com/store/apps/details?id=com.test.app")
                app._on_generate_qr()
                # 验证状态更新
                assert "成功" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_empty_url_shows_error(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._on_generate_qr()
                assert "请输入" in app.status_label.cget("text")
        finally:
            root.destroy()


class TestGuiSettings:
    def test_save_and_load_gui_settings(self, tmp_path):
        from gui import load_gui_settings, save_gui_settings

        settings_path = tmp_path / "settings.json"
        save_gui_settings(
            {
                "config_path": "/tmp/config.json",
                "work_dir": "/tmp/work",
            },
            str(settings_path),
        )

        assert json.loads(settings_path.read_text()) == {
            "config_path": "/tmp/config.json",
            "work_dir": "/tmp/work",
        }
        assert load_gui_settings(str(settings_path)) == {
            "config_path": "/tmp/config.json",
            "work_dir": "/tmp/work",
        }

    def test_load_gui_settings_returns_empty_for_missing_or_invalid_file(self, tmp_path):
        from gui import load_gui_settings

        missing_path = tmp_path / "missing.json"
        invalid_path = tmp_path / "invalid.json"
        invalid_path.write_text("{")

        assert load_gui_settings(str(missing_path)) == {}
        assert load_gui_settings(str(invalid_path)) == {}

    def test_get_remembered_path_falls_back_to_default(self):
        from gui import get_remembered_path

        assert get_remembered_path({}, "config_path", "/default/config.json") == "/default/config.json"
        assert get_remembered_path({"config_path": ""}, "config_path", "/default/config.json") == "/default/config.json"
        assert get_remembered_path({"config_path": "/saved/config.json"}, "config_path", "/default/config.json") == "/saved/config.json"


class TestPushApkAction:
    def test_empty_url_shows_warning(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._on_push_apk()
                assert "请输入" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_url_opens_on_device(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("subprocess.run") as mock_run, \
                 patch.object(root, "mainloop"):
                mock_run.return_value.returncode = 0
                app = APKToolApp(root)
                app.url_entry.insert(0, "https://play.google.com/store/apps/details?id=com.test.app")
                with patch("gui.threading.Thread", ImmediateThread):
                    app._on_push_apk()
                cmd = mock_run.call_args.args[0]
                assert "market://details?id=com.test.app" in cmd
                assert "com.android.vending" in cmd
                assert "手机" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_local_apk_no_device_shows_error(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.check_device") as mock_check, \
                 patch("os.path.isfile") as mock_isfile, \
                 patch.object(root, "mainloop"):
                mock_check.return_value = False
                mock_isfile.return_value = True
                app = APKToolApp(root)
                app.url_entry.insert(0, "/tmp/test.apk")
                app._on_push_apk()
                assert "设备" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_local_apk_file_not_found(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("os.path.isfile") as mock_isfile, \
                 patch("os.path.isdir") as mock_isdir, \
                 patch.object(root, "mainloop"):
                mock_isfile.return_value = False
                mock_isdir.return_value = False
                app = APKToolApp(root)
                app.url_entry.insert(0, "/tmp/nonexistent.apk")
                app._on_push_apk()
                assert "不存在" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_directory_path_installs_when_device_connected(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.check_device") as mock_check, \
                 patch("gui.push_apk_with_acceptance") as mock_push, \
                 patch("os.path.isfile") as mock_isfile, \
                 patch("os.path.isdir") as mock_isdir, \
                 patch.object(root, "mainloop"):
                mock_check.return_value = True
                mock_push.return_value = (True, "安装成功")
                mock_isfile.return_value = False
                mock_isdir.return_value = True
                app = APKToolApp(root)
                app.url_entry.insert(0, "/tmp/split-apks")
                with patch("gui.threading.Thread", ImmediateThread):
                    app._on_push_apk()
                mock_push.assert_called_once()
                assert mock_push.call_args.args == ("/tmp/split-apks",)
                assert callable(mock_push.call_args.kwargs["on_progress"])
                assert "安装成功" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_apkcombo_button_installs_local_xapk_path(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.check_device") as mock_check, \
                 patch("gui.push_apk_with_acceptance") as mock_push, \
                 patch("os.path.isfile") as mock_isfile, \
                 patch("os.path.isdir") as mock_isdir, \
                 patch.object(root, "mainloop"):
                mock_check.return_value = True
                mock_push.return_value = (True, "安装成功")
                mock_isfile.return_value = True
                mock_isdir.return_value = False
                app = APKToolApp(root)
                app.url_entry.insert(0, "/tmp/app.xapk")
                with patch("gui.threading.Thread", ImmediateThread):
                    app._on_apkcombo_download()
                mock_push.assert_called_once()
                assert mock_push.call_args.args == ("/tmp/app.xapk",)
                assert callable(mock_push.call_args.kwargs["on_progress"])
                assert "安装成功" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_apkcombo_button_downloads_xapk_url_with_query(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.check_device") as mock_check, \
                 patch.object(root, "mainloop"):
                mock_check.return_value = True
                app = APKToolApp(root)
                url = "https://cdn.example.com/game.XAPK?token=abc"
                app.url_entry.insert(0, url)
                with patch("gui.threading.Thread") as mock_thread:
                    app._on_apkcombo_download()

                mock_thread.assert_called_once()
                assert mock_thread.call_args.kwargs["args"] == (url,)
                mock_thread.return_value.start.assert_called_once()
                assert "正在下载" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_apkcombo_button_prefills_google_play_url_in_downloader(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            gp_url = "https://play.google.com/store/apps/details?id=com.example.game"
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.url_entry.insert(0, gp_url)
                with patch("webbrowser.open") as mock_open:
                    app._on_apkcombo_download()

                mock_open.assert_called_once_with(
                    "https://apkcombo.com/search/"
                    "https:%2F%2Fplay.google.com%2Fstore%2Fapps%2Fdetails%3F"
                    "id=com.example.game"
                    "#gsc.tab=0&gsc.q=https%3A%2F%2Fplay.google.com%2Fstore%2Fapps%2F"
                    "details%3Fid%3Dcom.example.game&gsc.sort="
                )
                assert "搜索结果页" in app.status_label.cget("text")
        finally:
            root.destroy()


class TestAdbCommandActions:
    def test_browse_config_remembers_selected_path(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.filedialog.askopenfilename", return_value="/tmp/new/config.json"), \
                 patch("gui.save_gui_settings") as mock_save, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)

                app._on_browse_config()

                assert app.config_path_var.get() == "/tmp/new/config.json"
                mock_save.assert_called_with(app._settings)
                assert app._settings["config_path"] == "/tmp/new/config.json"
        finally:
            root.destroy()

    def test_browse_work_dir_remembers_selected_path(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.filedialog.askdirectory", return_value="/tmp/new-work"), \
                 patch("gui.save_gui_settings") as mock_save, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)

                app._on_browse_work_dir()

                assert app.work_dir_var.get() == "/tmp/new-work"
                mock_save.assert_called_with(app._settings)
                assert app._settings["work_dir"] == "/tmp/new-work"
        finally:
            root.destroy()

    def test_browse_sa_file_remembers_selected_path(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.filedialog.askopenfilename", return_value="/tmp/new-sa.json"), \
                 patch("gui.save_gui_settings") as mock_save, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)

                app._on_browse_sa_file()

                assert app.sa_file_var.get() == "/tmp/new-sa.json"
                mock_save.assert_called_with(app._settings)
                assert app._settings["sync_sa_file"] == "/tmp/new-sa.json"
        finally:
            root.destroy()

    def test_fix_zygotehole_permissions_runs_expected_command(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            expected_cmd = [
                "adb",
                "shell",
                (
                    "chmod 777 /data/local/tmp/zygotehole/config.json; "
                    "chmod 444 /data/local/tmp/zygotehole/zygotehole.apk; "
                    "chmod 777 /data/local/tmp/zygotehole; "
                    "chown root:root /data/local/tmp/zygotehole/zygotehole.apk"
                ),
            ]
            with patch("gui.check_device", return_value=True), \
                 patch("gui.build_fix_zygotehole_permissions_cmd", return_value=expected_cmd) as mock_build, \
                 patch.object(APKToolApp, "_run_command") as mock_run, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)

                app._on_fix_zygotehole_permissions()

                mock_build.assert_called_once_with()
                mock_run.assert_called_once_with(expected_cmd)
        finally:
            root.destroy()

    def test_zygote_build_runs_permission_repair_command(self, tmp_path):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            work_dir = tmp_path / "zygote-work"
            work_dir.mkdir()
            (work_dir / "zygote_build.sh").write_text("echo build\n")
            expected_cmd = [
                "sh",
                "-c",
                "sh '/tmp/zygote-work/zygote_build.sh' && adb shell 'chmod 444'",
            ]

            with patch("gui.check_device", return_value=True), \
                 patch("gui.build_zygote_build_cmd", return_value=expected_cmd) as mock_build, \
                 patch.object(APKToolApp, "_run_command") as mock_run, \
                 patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.work_dir_var.set(str(work_dir))

                app._on_zygote_build()

                mock_build.assert_called_once_with(str(work_dir))
                mock_run.assert_called_once_with(
                    expected_cmd, cwd=str(work_dir), timeout=180
                )
        finally:
            root.destroy()

    def test_close_stops_logcat_with_short_timeout(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                proc = MagicMock()
                app._logcat_proc = proc

                with patch("gui.stop_logcat_stream") as mock_stop:
                    app._on_close()

                mock_stop.assert_called_once_with(proc, timeout=0.2)
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass


class TestActionDelayTool:
    def test_has_action_script_delay_widgets(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert hasattr(app, "action_script_text")
                assert hasattr(app, "following_action_delay_var")
                assert hasattr(app, "_on_normalize_action_delays")
                assert hasattr(app, "_on_copy_action_script")
        finally:
            root.destroy()

    def test_cleanup_keep_packages_includes_current_package(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.cleanup_keep_packages_var.set("com.keep.one, com.keep.two")
                app.pkg_entry.insert(0, "com.current.app")

                assert app._cleanup_keep_packages() == [
                    "com.apktool.sharereceiver",
                    "com.current.app",
                    "com.keep.one",
                    "com.keep.two",
                ]
                assert int(app.cleanup_keep_entry.cget("height")) >= 3
        finally:
            root.destroy()

    def test_cleanup_keep_packages_reads_multiline_editor(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.cleanup_keep_entry.delete("1.0", tk.END)
                app.cleanup_keep_entry.insert(
                    "1.0",
                    "com.keep.one\ncom.keep.two, com.keep.three",
                )

                assert app._cleanup_keep_packages() == [
                    "com.apktool.sharereceiver",
                    "com.keep.one",
                    "com.keep.three",
                    "com.keep.two",
                ]
        finally:
            root.destroy()

    def test_preview_third_party_cleanup_lists_targets(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.cleanup_keep_packages_var.set("com.keep")

                with patch("gui.check_device", return_value=True), \
                     patch("gui.list_third_party_packages", return_value=["com.keep", "com.remove"]), \
                     patch("gui.save_gui_settings"), \
                     patch.object(app, "_console_line") as mock_line, \
                    patch("gui.threading.Thread", ImmediateThread):
                    app._on_preview_third_party_cleanup()

                assert any("[将卸载] com.remove" in call.args[0] for call in mock_line.call_args_list)
                preview = app.cleanup_preview_text.get("1.0", tk.END)
                assert "将卸载:" in preview
                assert "com.remove" in preview
                assert "保留:" in preview
                assert "com.keep" in preview
                assert "将卸载 1 个" in app.cleanup_preview_var.get()
        finally:
            root.destroy()

    def test_cleanup_third_party_packages_updates_uninstall_progress(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.cleanup_keep_packages_var.set("com.keep")

                with patch("gui.check_device", return_value=True), \
                     patch("gui.list_third_party_packages", return_value=["com.keep", "com.remove"]), \
                     patch("gui.messagebox.askyesno", return_value=True) as mock_confirm, \
                     patch("gui.check_package_manager_ready", return_value=(True, "Package Manager 正常")), \
                     patch("gui.uninstall_third_party_package", return_value=(True, "卸载成功")) as mock_uninstall, \
                     patch("gui.save_gui_settings"), \
                    patch("gui.threading.Thread", ImmediateThread):
                    app._on_cleanup_third_party_packages()

                mock_confirm.assert_called_once()
                mock_uninstall.assert_called_once_with("com.remove")
                assert app.cleanup_progress_var.get() == 1
                assert "卸载完成：1/1" in app.cleanup_progress_text_var.get()
                assert "成功 1 个，失败 0 个" in app.cleanup_preview_var.get()
        finally:
            root.destroy()

    def test_cleanup_stops_immediately_when_package_manager_pipe_breaks(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                packages = ["com.one", "com.two", "com.three"]
                fatal_message = "cmd: Failure calling service package: Broken pipe (32)"

                with patch("gui.check_device", return_value=True), \
                     patch("gui.list_third_party_packages", return_value=packages), \
                     patch("gui.messagebox.askyesno", return_value=True), \
                     patch("gui.check_package_manager_ready", return_value=(True, "Package Manager 正常")), \
                     patch("gui.uninstall_third_party_package", return_value=(False, fatal_message)) as mock_uninstall, \
                     patch("gui.save_gui_settings"), \
                     patch("gui.time.sleep"), \
                     patch("gui.threading.Thread", ImmediateThread):
                    app._on_cleanup_third_party_packages()

                mock_uninstall.assert_called_once_with("com.one")
                assert "保护性停止" in app.cleanup_preview_var.get()
                assert "1/3" in app.cleanup_progress_text_var.get()
                assert "剩余 2 个未执行" in app.cleanup_preview_var.get()
        finally:
            root.destroy()

    def test_normalize_action_delays_replaces_pasted_text(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.following_action_delay_var.set("6")
                app.action_script_text.insert(
                    "1.0",
                    '{"ActivityA": [{"delay": 500}, {"delay": 4500}]}',
                )

                app._on_normalize_action_delays()
                result = app.action_script_text.get("1.0", tk.END)

                assert '"delay": 15000' in result
                assert '"delay": 6000' in result
                assert "已调整" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_normalize_action_delays_rejects_invalid_config(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.following_action_delay_var.set("abc")
                app.action_script_text.insert("1.0", '{"ActivityA": [{"delay": 500}]}')

                app._on_normalize_action_delays()

                assert "必须是数字" in app.status_label.cget("text")
        finally:
            root.destroy()


class TestManifestAttributionFallback:
    @staticmethod
    def _app():
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_current_package_name = lambda: "com.example.game"
        app._automation_log = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        return app

    @staticmethod
    def _detection():
        return {
            "ok": False,
            "code": "UNSUPPORTED_ATTRIBUTION",
            "message": "未知归因，暂不适配",
            "fields": {
                "最终判断": "MAX聚合（强关系证据确认）",
                "插屏聚合id": "inter-123",
                "激励视频聚合id": "reward-456",
                "归因平台": "",
            },
        }

    def test_manifest_adjust_recovers_missing_dynamic_attribution(self):
        app = self._app()
        with patch("gui.get_adb_path", return_value="/opt/homebrew/bin/adb"), patch(
            "gui.inspect_installed_package_attribution",
            return_value={
                "ok": True,
                "platforms": ["Adjust"],
                "evidence": ["Adjust: com.adjust.sdk.Adjust"],
            },
        ):
            result = app._automation_apply_manifest_attribution_fallback_sync(
                self._detection()
            )

        assert result["ok"] is True
        assert result["code"] == "MANIFEST_ATTRIBUTION_RECOVERED"
        assert result["fields"]["归因平台"] == "Adjust"
        assert result["fields"]["_attribution_source"] == "AndroidManifest.xml 兜底"

    def test_manifest_without_known_sdk_becomes_unknown_attribution(self):
        app = self._app()
        with patch("gui.get_adb_path", return_value="/opt/homebrew/bin/adb"), patch(
            "gui.inspect_installed_package_attribution",
            return_value={"ok": True, "platforms": [], "evidence": []},
        ):
            result = app._automation_apply_manifest_attribution_fallback_sync(
                self._detection()
            )

        assert result["ok"] is False
        assert result["code"] == "UNSUPPORTED_ATTRIBUTION"
        assert result["fields"]["归因平台"] == "未知"
        assert result["fields"]["_attribution_evidence"] == [
            "AndroidManifest.xml 未检测到已知归因 SDK"
        ]


class TestSuspectedWhitePackageRule:
    @staticmethod
    def _app():
        from gui import APKToolApp

        app = object.__new__(APKToolApp)
        app._automation_current_package_name = lambda: "com.example.game"
        app._automation_log = MagicMock()
        app._safe_after = lambda _delay, callback, *args: callback(*args)
        return app

    @pytest.mark.parametrize("installs", [50000, 100000, 160000, 179999])
    def test_low_download_missing_ids_becomes_white_package(self, installs):
        app = self._app()
        detection = {
            "ok": False,
            "code": "AD_IDS_EMPTY",
            "message": "聚合 ID 为空",
            "fields": {
                "最终判断": "MAX聚合",
                "归因平台": "Adjust",
                "插屏聚合id": "",
                "激励视频聚合id": "",
            },
        }
        with patch(
            "gui.fetch_google_play_install_count",
            return_value={
                "ok": True,
                "installs": installs,
                "display": f"{installs:,}+",
            },
        ):
            result = app._automation_apply_suspected_white_package_rule_sync(
                detection
            )

        assert result["ok"] is False
        assert result["code"] == "SUSPECTED_WHITE_PACKAGE"
        assert result["fields"]["_google_play_installs"] == installs

    def test_180k_downloads_preserves_original_detection_result(self):
        app = self._app()
        detection = {
            "ok": False,
            "code": "AGGREGATION_TYPE_EMPTY",
            "message": "聚合类型为空",
            "fields": {"最终判断": "", "归因平台": "AppsFlyer"},
        }
        with patch(
            "gui.fetch_google_play_install_count",
            return_value={
                "ok": True,
                "installs": 180000,
                "display": "180,000+",
            },
        ):
            result = app._automation_apply_suspected_white_package_rule_sync(
                detection
            )

        assert result["code"] == "AGGREGATION_TYPE_EMPTY"
        assert result["fields"]["_google_play_installs_text"] == "180,000+"
        assert result["message"] == (
            "聚合类型识别为空\nGoogle Play下载量：18w+"
        )

    def test_high_download_empty_aggregation_uses_w_unit_in_failure_message(self):
        app = self._app()
        detection = {
            "ok": False,
            "code": "AGGREGATION_TYPE_EMPTY",
            "message": "聚合类型识别为空",
            "fields": {"最终判断": "", "归因平台": "AppsFlyer"},
        }
        with patch(
            "gui.fetch_google_play_install_count",
            return_value={
                "ok": True,
                "installs": 500_000_000,
                "display": "500M+",
            },
        ):
            result = app._automation_apply_suspected_white_package_rule_sync(
                detection
            )

        assert result["code"] == "AGGREGATION_TYPE_EMPTY"
        assert result["message"] == (
            "聚合类型识别为空\nGoogle Play下载量：50000w+"
        )

    def test_white_package_terminal_writes_asana_backend_and_sheet(self):
        import threading

        app = self._app()
        app._automation_fields = {
            "最终判断": "",
            "_google_play_installs": 10000,
            "_google_play_installs_text": "10,000+",
        }
        app._automation_stop_event = threading.Event()
        app._automation_precheck_item_id = ""
        app._automation_fill_asana_sync = MagicMock()
        app._automation_comment_business_outcome = MagicMock()
        app._automation_clear_inferred_backend_sync = MagicMock(
            return_value={"ok": True, "message": "提交并刷新成功"}
        )
        app._automation_mark_suspected_white_package = MagicMock()
        app._automation_write_sheet_outcome_sync = MagicMock()

        result = app._automation_complete_suspected_white_package_sync(
            "Google Play 下载量10,000+，疑似白包，暂不适配"
        )

        assert result is False
        app._automation_fill_asana_sync.assert_called_once_with(
            allow_unsupported_attribution=True,
            allow_missing_aggregation=True,
            terminal_note="疑似白包，暂不适配",
        )
        app._automation_comment_business_outcome.assert_called_once()
        app._automation_clear_inferred_backend_sync.assert_called_once_with(
            note="疑似白包，暂不适配"
        )
        app._automation_mark_suspected_white_package.assert_called_once()
        app._automation_write_sheet_outcome_sync.assert_called_once_with(
            "not_adapted", "疑似白包，暂不适配"
        )
