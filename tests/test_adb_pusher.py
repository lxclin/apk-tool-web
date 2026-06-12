import pytest
from unittest.mock import patch, MagicMock


class TestCheckDevice:
    def test_returns_true_when_device_connected(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\n192.168.1.1:5555\tdevice\n")
            from adb_pusher import check_device
            assert check_device() is True

    def test_returns_false_when_no_device(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            from adb_pusher import check_device
            assert check_device() is False

    def test_returns_false_when_adb_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            from adb_pusher import check_device
            assert check_device() is False


class TestGetDeviceList:
    def test_returns_device_ids(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\nABC123\tdevice\nDEF456\tdevice\n")
            from adb_pusher import get_device_list
            assert get_device_list() == ["ABC123", "DEF456"]

    def test_returns_empty_list_when_no_devices(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            from adb_pusher import get_device_list
            assert get_device_list() == []


class TestPushApk:
    def test_successful_install(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Success\n")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is True
            assert "成功" in msg

    def test_failed_install(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="INSTALL_FAILED\n")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False
            assert "失败" in msg

    def test_adb_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False
            assert "ADB" in msg

    def test_no_device_connected(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no devices/emulators found")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False
