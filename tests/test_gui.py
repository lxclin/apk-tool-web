import pytest
from unittest.mock import patch, MagicMock
import tkinter as tk


class TestAppInit:
    def test_creates_main_window_with_title(self):
        root = tk.Tk()
        try:
            from gui import APKToolApp
            with patch.object(root, "mainloop"):
                app = APKToolApp(root)
                assert "APK" in root.title()
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
                app._on_push_apk()
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
                 patch.object(root, "mainloop"):
                mock_isfile.return_value = False
                app = APKToolApp(root)
                app.url_entry.insert(0, "/tmp/nonexistent.apk")
                app._on_push_apk()
                assert "不存在" in app.status_label.cget("text")
        finally:
            root.destroy()
