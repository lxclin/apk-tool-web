import pytest
from unittest.mock import patch, MagicMock
import tkinter as tk
import json
import time


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
                assert hasattr(app, "_on_fix_zygotehole_permissions")
                assert hasattr(app, "_stop_command_btn")
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
                    "清除缓存",
                    "强制停止",
                    "打开应用",
                    "清空 Play Store 缓存",
                    "修复 zygotehole 权限",
                ]
        finally:
            root.destroy()

    def test_clear_cache_uses_short_timeout(self):
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
                mock_run.assert_called_once_with(expected_cmd, timeout=15)
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
                APKToolApp(root)
                notebook = next(
                    child for child in root.winfo_children()
                    if isinstance(child, tk.ttk.Notebook)
                )
                assert [notebook.tab(tab_id, "text") for tab_id in notebook.tabs()] == [
                    "APK 工具",
                    "ADB 指令",
                    "自动化脚本",
                    "数据同步",
                ]
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
                root.update()
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
                 patch("gui.push_apk") as mock_push, \
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
                root.update()
                mock_push.assert_called_once_with("/tmp/split-apks")
                assert "安装成功" in app.status_label.cget("text")
        finally:
            root.destroy()

    def test_apkcombo_button_installs_local_xapk_path(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch("gui.check_device") as mock_check, \
                 patch("gui.push_apk") as mock_push, \
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
                root.update()
                mock_push.assert_called_once_with("/tmp/app.xapk")
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
                    "com.current.app",
                    "com.keep.one",
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
                    root.update()

                assert any("[将卸载] com.remove" in call.args[0] for call in mock_line.call_args_list)
        finally:
            root.destroy()

    def test_cleanup_third_party_packages_confirms_and_runs_bulk_command(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp

            expected_cmd = ["adb", "shell", "sh", "-c", "pm uninstall com.remove"]
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                app.cleanup_keep_packages_var.set("com.keep")

                with patch("gui.check_device", return_value=True), \
                     patch("gui.list_third_party_packages", return_value=["com.keep", "com.remove"]), \
                     patch("gui.messagebox.askyesno", return_value=True) as mock_confirm, \
                     patch("gui.build_bulk_uninstall_cmd", return_value=expected_cmd) as mock_build, \
                     patch("gui.save_gui_settings"), \
                     patch.object(app, "_run_command") as mock_run, \
                     patch("gui.threading.Thread", ImmediateThread):
                    app._on_cleanup_third_party_packages()
                    root.update()

                mock_confirm.assert_called_once()
                mock_build.assert_called_once_with(["com.remove"])
                mock_run.assert_called_once_with(expected_cmd, timeout=60)
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
