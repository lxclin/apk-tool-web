import json
import subprocess

from device_health import run_device_health_check


def _result(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_device_health_happy_path(tmp_path):
    adb = tmp_path / "adb"
    adb.write_text("", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data": [{}]}), encoding="utf-8")
    (tmp_path / "zygote_build.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    def runner(command, **kwargs):
        joined = " ".join(command[1:])
        if joined == "devices":
            return _result("List of devices attached\nSERIAL\tdevice\n")
        if "getprop ro.product.model" in joined:
            return _result("Pixel 5\n33\narm64-v8a\n")
        if joined == "shell df -k /data":
            return _result("Filesystem 1K-blocks Used Available Use% Mounted on\n/data 10000000 1000 9999000 1% /data\n")
        if joined == "shell ls -ld /data/local/tmp/zygotehole":
            return _result("drwxrwxrwx root root /data/local/tmp/zygotehole\n")
        return _result("package:/data/app/base.apk\n")

    report = run_device_health_check(
        adb_path=str(adb),
        package_name="com.example.game",
        config_path=str(config),
        work_dir=str(tmp_path),
        command_runner=runner,
    )

    assert report.ok is True
    assert report.serial == "SERIAL"
    assert report.free_data_mb == 9764
    assert all(check.level != "error" for check in report.checks)


def test_device_health_blocks_multiple_devices(tmp_path):
    adb = tmp_path / "adb"
    adb.write_text("", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data": []}), encoding="utf-8")
    (tmp_path / "zygote_build.sh").write_text("", encoding="utf-8")

    def runner(command, **kwargs):
        return _result("List of devices attached\nONE\tdevice\nTWO\tdevice\n")

    report = run_device_health_check(
        adb_path=str(adb),
        package_name="com.example.game",
        config_path=str(config),
        work_dir=str(tmp_path),
        command_runner=runner,
    )

    assert report.ok is False
    assert any("2 台在线设备" in check.message for check in report.checks)


def test_device_health_blocks_missing_app_and_low_space(tmp_path):
    adb = tmp_path / "adb"
    adb.write_text("", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data": [{}]}), encoding="utf-8")
    (tmp_path / "zygote_build.sh").write_text("", encoding="utf-8")

    def runner(command, **kwargs):
        joined = " ".join(command[1:])
        if joined == "devices":
            return _result("List of devices attached\nSERIAL\tdevice\n")
        if "getprop ro.product.model" in joined:
            return _result("Phone\n33\narm64-v8a\n")
        if joined == "shell df -k /data":
            return _result("/data 100000 90000 10000 90% /data\n")
        if joined == "shell pm path com.example.game":
            return _result("", returncode=1)
        if joined == "shell ls -ld /data/local/tmp/zygotehole":
            return _result("drwxrwxrwx /data/local/tmp/zygotehole\n")
        return _result("package:/system/app/base.apk\n")

    report = run_device_health_check(
        adb_path=str(adb),
        package_name="com.example.game",
        config_path=str(config),
        work_dir=str(tmp_path),
        min_free_data_mb=1024,
        command_runner=runner,
    )

    assert report.ok is False
    errors = [check.message for check in report.checks if check.level == "error"]
    assert any("至少需要" in message for message in errors)
    assert any("未安装" in message for message in errors)
