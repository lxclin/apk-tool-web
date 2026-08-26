"""Preflight checks for the Android aggregation automation pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import re
import subprocess
from typing import Callable


@dataclass(frozen=True)
class HealthCheck:
    name: str
    level: str
    message: str


@dataclass(frozen=True)
class DeviceHealthReport:
    ok: bool
    checks: tuple[HealthCheck, ...]
    serial: str = ""
    model: str = ""
    android_api: str = ""
    abi: str = ""
    free_data_mb: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def lines(self) -> list[str]:
        icons = {"ok": "✓", "warning": "!", "error": "✗"}
        return [
            f"{icons.get(check.level, '-')} {check.name}: {check.message}"
            for check in self.checks
        ]


def _parse_devices(output: str) -> list[tuple[str, str]]:
    devices = []
    for line in str(output or "").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1].casefold()))
    return devices


def _parse_free_data_mb(output: str) -> int | None:
    for line in reversed(str(output or "").splitlines()):
        columns = line.split()
        if not columns or not any("/data" in value for value in columns):
            continue
        numeric = [int(value) for value in columns if re.fullmatch(r"\d+", value)]
        if len(numeric) >= 3:
            # POSIX df columns: 1K-blocks, Used, Available, Use%, Mounted on.
            return numeric[2] // 1024
    return None


def run_device_health_check(
    *,
    adb_path: str | None,
    package_name: str,
    config_path: str,
    work_dir: str,
    min_free_data_mb: int = 1024,
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> DeviceHealthReport:
    """Check prerequisites without changing the device or local configuration."""
    checks: list[HealthCheck] = []
    package_name = str(package_name or "").strip()
    serial = model = android_api = abi = ""
    free_data_mb: int | None = None

    if not adb_path or not os.path.isfile(adb_path):
        checks.append(HealthCheck("ADB", "error", "未找到可用的 ADB"))
        return DeviceHealthReport(False, tuple(checks))
    checks.append(HealthCheck("ADB", "ok", adb_path))

    if not os.path.isfile(config_path):
        checks.append(HealthCheck("config.json", "error", "配置文件不存在"))
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
            valid = isinstance(config, dict) and isinstance(config.get("data"), list)
            checks.append(
                HealthCheck(
                    "config.json",
                    "ok" if valid else "error",
                    "格式正常" if valid else "根节点必须包含 data 数组",
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(HealthCheck("config.json", "error", f"无法读取: {exc}"))

    script_path = os.path.join(work_dir, "zygote_build.sh")
    checks.append(
        HealthCheck(
            "zygote_build",
            "ok" if os.path.isfile(script_path) else "error",
            script_path if os.path.isfile(script_path) else "构建脚本不存在",
        )
    )

    def run_adb(*args: str, timeout: int = 10):
        return command_runner(
            [adb_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    try:
        device_result = run_adb("devices", timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks.append(HealthCheck("设备连接", "error", f"ADB 无响应: {exc}"))
        return DeviceHealthReport(False, tuple(checks))

    devices = _parse_devices(device_result.stdout)
    online = [item for item in devices if item[1] == "device"]
    unauthorized = [item[0] for item in devices if item[1] == "unauthorized"]
    offline = [item[0] for item in devices if item[1] == "offline"]
    if len(online) != 1:
        if len(online) > 1:
            message = f"检测到 {len(online)} 台在线设备，当前工具未指定目标序列号"
        elif unauthorized:
            message = f"设备未授权: {', '.join(unauthorized)}"
        elif offline:
            message = f"设备离线: {', '.join(offline)}"
        else:
            message = "未检测到在线设备"
        checks.append(HealthCheck("设备连接", "error", message))
        return DeviceHealthReport(False, tuple(checks))

    serial = online[0][0]
    checks.append(HealthCheck("设备连接", "ok", serial))

    try:
        props = run_adb(
            "shell",
            "getprop ro.product.model; getprop ro.build.version.sdk; getprop ro.product.cpu.abi",
        )
        prop_lines = [line.strip() for line in props.stdout.splitlines() if line.strip()]
        model = prop_lines[0] if prop_lines else ""
        android_api = prop_lines[1] if len(prop_lines) > 1 else ""
        abi = prop_lines[2] if len(prop_lines) > 2 else ""
        checks.append(
            HealthCheck(
                "设备信息",
                "ok" if model else "warning",
                f"{model or '未知型号'} · API {android_api or '?'} · {abi or '未知 ABI'}",
            )
        )
    except (OSError, subprocess.TimeoutExpired):
        checks.append(HealthCheck("设备信息", "warning", "读取超时"))

    try:
        storage = run_adb("shell", "df", "-k", "/data")
        free_data_mb = _parse_free_data_mb(storage.stdout)
        if free_data_mb is None:
            checks.append(HealthCheck("设备空间", "warning", "无法解析 /data 剩余空间"))
        elif free_data_mb < min_free_data_mb:
            checks.append(
                HealthCheck(
                    "设备空间",
                    "error",
                    f"仅剩 {free_data_mb} MB，至少需要 {min_free_data_mb} MB",
                )
            )
        else:
            checks.append(HealthCheck("设备空间", "ok", f"剩余 {free_data_mb} MB"))
    except (OSError, subprocess.TimeoutExpired):
        checks.append(HealthCheck("设备空间", "warning", "读取超时"))

    if not package_name:
        checks.append(HealthCheck("目标应用", "error", "包名为空"))
    else:
        try:
            installed = run_adb("shell", "pm", "path", package_name)
            is_installed = installed.returncode == 0 and "package:" in installed.stdout
            checks.append(
                HealthCheck(
                    "目标应用",
                    "ok" if is_installed else "error",
                    "已安装" if is_installed else f"未安装: {package_name}",
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            checks.append(HealthCheck("目标应用", "error", "Package Manager 无响应"))

    try:
        remote = run_adb(
            "shell", "ls", "-ld", "/data/local/tmp/zygotehole", timeout=10
        )
        available = remote.returncode == 0 and "zygotehole" in remote.stdout
        checks.append(
            HealthCheck(
                "注入目录",
                "ok" if available else "error",
                "可访问" if available else "/data/local/tmp/zygotehole 不存在或不可访问",
            )
        )
    except (OSError, subprocess.TimeoutExpired):
        checks.append(HealthCheck("注入目录", "error", "读取超时"))

    for label, package in (
        ("Google Play", "com.android.vending"),
        ("Google Play 服务", "com.google.android.gms"),
    ):
        try:
            result = run_adb("shell", "pm", "path", package)
            present = result.returncode == 0 and "package:" in result.stdout
            checks.append(
                HealthCheck(
                    label,
                    "ok" if present else "warning",
                    "已安装" if present else "未安装；部分应用可能无法启动",
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            checks.append(HealthCheck(label, "warning", "状态读取超时"))

    ok = not any(check.level == "error" for check in checks)
    return DeviceHealthReport(
        ok,
        tuple(checks),
        serial=serial,
        model=model,
        android_api=android_api,
        abi=abi,
        free_data_mb=free_data_mb,
    )
