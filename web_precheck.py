"""Shared Web/proxy adapter for the desktop Play Store precheck workflow."""

from dataclasses import asdict
from datetime import datetime
from typing import Callable, Optional

from adb_pusher import (
    download_and_install_apkcombo,
    install_google_play_app,
    resolve_google_play_package,
    run_app_launch_precheck,
    run_google_play_precheck,
)
from auto_asana.main import (
    add_precheck_comment_once,
    build_asana_client,
    get_asana_tasks_for_date,
)
from automation_adaptation import (
    add_automation_comment_once,
    submit_precheck_blacklist_via_api,
)


ProgressCallback = Optional[Callable[[str], None]]


def load_today_precheck_tasks(project_gid: str, asana_pat: str = "") -> dict:
    """Return today's Asana section and JSON-serializable task records."""
    project_gid = project_gid.strip()
    if not project_gid:
        raise ValueError("请输入 Asana 项目 GID")
    asana_pat = asana_pat.strip()
    if not asana_pat:
        raise ValueError("请输入 Asana PAT")
    client = build_asana_client(asana_pat)
    result = get_asana_tasks_for_date(
        client,
        project_gid,
        today=datetime.now().date(),
    )
    return {
        **result,
        "tasks": [asdict(task) for task in result.get("tasks", [])],
    }


def comment_result_for_precheck(result: dict) -> dict:
    """Convert nested install/launch output into the Asana terminal result."""
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
    if install_result and not install_result.get("ok"):
        return {
            "code": "INSTALL_FAILED",
            "package_name": result.get("package_name", ""),
            "detail": install_result.get("message", "自动下载安装失败"),
        }
    return result


def run_web_precheck(
    value: str,
    *,
    auto_install: bool = False,
    launch_check: bool = True,
    observation_seconds: int = 20,
    task_gid: str = "",
    asana_pat: str = "",
    backend_api_url: str = "",
    backend_x_token: str = "",
    backend_token: str = "",
    backend_user_name: str = "rain",
    force_apkcombo: bool = False,
    on_progress: ProgressCallback = None,
) -> dict:
    """Run one complete Web precheck and optionally add its Asana comment."""
    value = value.strip()
    if not value:
        raise ValueError("请输入 Google Play 链接或包名")
    observation_seconds = max(5, min(120, int(observation_seconds)))

    if force_apkcombo:
        package_name = resolve_google_play_package(value)
        result = {
            "code": "APKCOMBO_AVAILABLE",
            "title": "APKCombo 有包，重新尝试自动下载",
            "detail": "历史安装失败任务已转入 APKCombo 新下载链路重试。",
            "continue_adaptation": False,
            "package_name": package_name,
            "source": "历史预检状态迁移",
            "evidence": ["此前安装失败，按新规则使用 APKCombo 重试"],
        }
    else:
        if on_progress:
            on_progress("正在打开并识别 Google Play 页面...")
        result = run_google_play_precheck(value, verify_apkcombo=True)

    if result.get("code") in {
        "IAP_ONLY",
        "JAPANESE_PACKAGE",
        "ALL_NETWORK_NO_PACKAGE",
    }:
        if on_progress:
            on_progress("检测到终止预检结论，正在提交后台并刷新缓存...")
        backend_blacklist = submit_precheck_blacklist_via_api(
            result,
            api_url=backend_api_url,
            x_token=backend_x_token,
            token=backend_token,
            user_name=backend_user_name,
        )
        result = {**result, "backend_blacklist": backend_blacklist}

    if auto_install and (
        result.get("continue_adaptation") is True
        or result.get("code") in {
            "HAS_ADS",
            "NO_ADS_OR_IAP",
            "APKCOMBO_AVAILABLE",
        }
    ):
        if on_progress:
            on_progress("页面结果需继续人工确认，准备自动下载安装...")
        if result.get("code") == "APKCOMBO_AVAILABLE":
            install_result = download_and_install_apkcombo(
                result.get("package_name", ""),
                on_progress=on_progress,
            )
        else:
            install_result = install_google_play_app(
                result.get("package_name", ""),
                on_progress=on_progress,
            )
        result = {**result, "install_result": install_result}
        if launch_check and install_result.get("ok"):
            if on_progress:
                on_progress("安装已完成，开始启动闪退检查...")
            launch_result = run_app_launch_precheck(
                result.get("package_name", ""),
                observation_seconds=observation_seconds,
                on_progress=on_progress,
            )
            result = {**result, "launch_result": launch_result}

    comment_status = {
        "attempted": False,
        "created": False,
        "message": "",
    }
    if task_gid and result.get("code") not in {"NO_DEVICE", "OPEN_FAILED"}:
        comment_status["attempted"] = True
        try:
            if not asana_pat.strip():
                raise ValueError("未提供 Asana PAT，无法写入评论")
            client = build_asana_client(asana_pat.strip())
            created = add_precheck_comment_once(
                client,
                task_gid,
                comment_result_for_precheck(result),
            )
            comment_status["created"] = created
            comment_status["message"] = (
                "已写入 Asana 评论" if created else "无需评论或相同评论已存在"
            )
            backend_blacklist = result.get("backend_blacklist") or {}
            if backend_blacklist and not backend_blacklist.get("ok"):
                add_automation_comment_once(
                    client,
                    task_gid,
                    backend_blacklist.get(
                        "code", "PRECHECK_BLACKLIST_SUBMIT_FAILED"
                    ),
                    backend_blacklist.get(
                        "message", "预检后台标记提交失败，需要人工处理"
                    ),
                )
        except Exception as exc:
            comment_status["message"] = f"Asana 评论失败: {exc}"

    return {**result, "asana_comment": comment_status}
