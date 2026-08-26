import pytest
from unittest.mock import call, patch, MagicMock
import tkinter as tk
import json
import time


@pytest.fixture(autouse=True)
def isolate_gui_settings(monkeypatch, tmp_path):
    """GUI tests must not touch the operator's real settings/checkpoint files."""
    import gui

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
                assert app.automation_replay_timeout_var.get() == "500"
        finally:
            root.destroy()


class TestGooglePlayPrecheckActions:
    def test_selected_asana_task_fills_gp_link(self):
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

                assert app.precheck_input.get().endswith("id=com.example.game")
                assert "1 个任务" in app._precheck_asana_status.cget("text")
        finally:
            root.destroy()


class TestAutomationBatchActions:
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
                with patch.object(
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
            assert "写入 config" in app._automation_status.cget("text")
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

    def test_single_id_replay_keeps_original_one_shot_flow(self):
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
                replay_result = {
                    "ok": False,
                    "code": "REPLAY_TIMEOUT",
                    "interstitial": {"required": True, "displayed": False},
                    "rewarded": {"required": True, "displayed": False},
                }
                with patch.object(
                    app, "_automation_replay_sync", return_value=replay_result
                ) as replay, patch.object(
                    app, "_automation_fill_asana_sync"
                ) as fill_asana, patch.object(
                    app, "_automation_submit_backend_sync"
                ) as submit:
                    result = app._automation_replay_with_id_rotation_sync()

                assert result is replay_result
                replay.assert_called_once_with()
                fill_asana.assert_not_called()
                submit.assert_not_called()
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
                for index in (1, 2, 3)
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

                with patch("gui.threading.Thread", ImmediateThread), \
                     patch.object(
                         app,
                         "_automation_process_current_task_sync",
                         side_effect=[True, False],
                     ) as mock_process, \
                     patch.object(app, "_automation_run_command_sync"):
                    app._automation_run_eligible_batch()

                assert mock_process.call_count == 2
                assert app.automation_package_var.get() == "com.auto3.game"
                output = app.automation_log_text.get("1.0", tk.END)
                assert "com.auto1.game" in output
                assert "com.auto2.game" not in output
                assert "com.auto3.game" in output
                assert "成功 1，其他归因 0，失败 1" in output
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
                     patch("gui.run_google_play_precheck", return_value=page_result), \
                     patch.object(
                         app, "_install_after_precheck", return_value=installed_result
                     ), \
                     patch("gui.add_precheck_comment_once"), \
                     patch.object(app, "_automation_run_eligible_batch") as run_batch:
                    app._on_start_precheck_then_automation()

                item_id = app.precheck_task_tree.get_children()[0]
                assert app.precheck_task_tree.item(item_id, "values")[3] == "安装完成"
                run_batch.assert_called_once_with()
                assert "开始自动适配 1 个" in app._precheck_status.cget("text")
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

    def test_unlabeled_precheck_result_recommends_download_and_manual_review(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app._render_precheck_result({
                    "code": "NO_ADS_OR_IAP",
                    "title": "未发现广告或应用内购标识（待人工确认）",
                    "detail": "不能据此加黑，将继续下载安装。",
                    "continue_adaptation": True,
                    "package_name": "com.example.review",
                    "source": "UI 控件",
                })

                output = app.precheck_output.get("1.0", tk.END)
                assert "继续下载，人工检查是否包含广告；不自动加黑" in output
                assert "加黑并跳过" not in output
                assert APKToolApp._precheck_task_status_for_result({
                    "code": "NO_ADS_OR_IAP",
                    "install_result": {
                        "ok": True,
                        "code": "INSTALLED",
                    },
                }) == "待人工检查"
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
                     patch("gui.time.sleep"), \
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
