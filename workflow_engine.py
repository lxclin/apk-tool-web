"""Shared decision engine used by both desktop and Web workflows.

This module intentionally contains no Tk, FastAPI, WebSocket, or Asana client
code.  Both front ends can therefore render the same result without copying
business rules.
"""

from __future__ import annotations


BACKEND_TERMINAL_PRECHECK_CODES = frozenset(
    {"IAP_ONLY", "JAPANESE_PACKAGE", "ALL_NETWORK_NO_PACKAGE"}
)
INSTALLABLE_PRECHECK_CODES = frozenset(
    {"HAS_ADS", "NO_ADS_OR_IAP", "APKCOMBO_AVAILABLE"}
)
DEFERRED_INSTALL_CODES = frozenset(
    {"DOWNLOAD_LIMIT_REACHED", "DOWNLOAD_DEFERRED", "DOWNLOAD_PAUSED", "DOWNLOAD_STARTED"}
)


def needs_precheck_backend_submission(result: dict) -> bool:
    return str(result.get("code") or "") in BACKEND_TERMINAL_PRECHECK_CODES


def should_install_after_precheck(result: dict) -> bool:
    return result.get("continue_adaptation") is True or str(
        result.get("code") or ""
    ) in INSTALLABLE_PRECHECK_CODES


def precheck_comment_result(result: dict) -> dict:
    """Convert nested install/launch output into a stable terminal result."""
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
        and install_result.get("code") not in DEFERRED_INSTALL_CODES
        and not install_result.get("ok")
    ):
        return {
            "code": "INSTALL_FAILED",
            "package_name": result.get("package_name", ""),
            "detail": install_result.get("message", "自动下载安装失败"),
        }
    return result


def precheck_task_status(result: dict) -> str:
    """Return the canonical Chinese status shown by every front end."""
    code = str(result.get("code") or "")
    backend = result.get("backend_blacklist") or {}
    if code == "ALL_NETWORK_NO_PACKAGE" and backend:
        return "全网无包(后台)" if backend.get("ok") else "全网无包提交失败"
    if code in {"IAP_ONLY", "JAPANESE_PACKAGE"} and backend:
        return "已加黑(后台)" if backend.get("ok") else "加黑提交失败"

    review_monetization = code == "NO_ADS_OR_IAP"
    launch_result = result.get("launch_result") or {}
    if launch_result:
        status = {
            "LAUNCH_OK": "启动正常",
            "APP_CRASHED": "包体闪退",
            "APP_EXITED": "启动异常",
            "LAUNCH_FAILED": "启动失败",
        }.get(launch_result.get("code"), "启动异常")
        return "待人工检查" if review_monetization and launch_result.get("ok") else status

    install_result = result.get("install_result") or {}
    if install_result:
        install_code = install_result.get("code")
        if install_code in {"DOWNLOAD_LIMIT_REACHED", "DOWNLOAD_DEFERRED"}:
            return "待补下载"
        if install_code == "DOWNLOAD_PAUSED":
            return "下载已暂停"
        if install_code == "DOWNLOAD_STARTED":
            return "后台下载中"
        if install_result.get("ok"):
            if review_monetization:
                return "待人工检查"
            return "已安装" if install_code == "ALREADY_INSTALLED" else "安装完成"
        return "安装失败"

    return {
        "HAS_ADS": "有广告", "GOOGLE_NO_PACKAGE": "google无包",
        "ALL_NETWORK_NO_PACKAGE": "全网无包", "APKCOMBO_AVAILABLE": "APKCombo有包",
        "APKCOMBO_CHECK_FAILED": "APKCombo待确认", "IAP_ONLY": "已加黑",
        "JAPANESE_PACKAGE": "已加黑", "NO_ADS_OR_IAP": "待人工检查",
        "DEVICE_UNSUPPORTED": "设备不支持", "COUNTRY_UNSUPPORTED": "地区不支持",
        "UNKNOWN": "待人工", "NO_DEVICE": "未执行",
    }.get(code, "失败")
