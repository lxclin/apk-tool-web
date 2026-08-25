"""Shared helpers for the new aggregation automation flow.

These helpers are intentionally separate from the existing manual button
callbacks.  The automation tab can reuse the same extracted field format and
backend URL without changing the established manual workflow.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import time
import urllib.parse
from typing import Any

import requests

from adb_pusher import build_backend_url, get_adb_path, normalize_optional_parameter


AUTOMATION_COMMENT_PREFIX = "【APK Tool 自动化适配："
AGGREGATION_RETRY_WAIT_SECONDS = 60
AGGREGATION_RETRY_GRACE_SECONDS = 30
AGGREGATION_RETRY_POLL_SECONDS = 5
INVALID_AD_ID_VALUES = frozenset(
    {"video", "reward", "rewarded", "rewarded_video", "inter", "interstitial"}
)
SYMBOLIC_AD_IDS_BY_FIELD = {
    "激励视频聚合id": frozenset({"video"}),
    "插屏聚合id": frozenset({"inter"}),
}
INFERRED_IRONSOURCE_VERDICT = "IronSource聚合（根据 video/inter 自动推断）"
INFERRED_AGGREGATION_FAILURE_NOTE = "未识别出聚合类型，暂不适配"
BACKEND_CACHE_CLEAR_URL = (
    "https://a2-2.hilong.vip/a2/delete_a2_package_cache"
)
BACKEND_READBACK_ATTEMPTS = 3
BACKEND_READBACK_DELAY_SECONDS = 1.0
PRECHECK_BLACKLIST_REASONS = {
    "IAP_ONLY": "应用内购，无广告，加黑",
    "JAPANESE_PACKAGE": "日本包体，加黑",
    "ALL_NETWORK_NO_PACKAGE": "全网无包，暂不适配",
}


def _system_ca_bundle_candidates() -> list[str]:
    """Return existing OS/Python CA bundles suitable for a safe TLS retry."""
    candidates = [
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        ssl.get_default_verify_paths().cafile or "",
    ]
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = os.path.realpath(str(candidate or "").strip())
        if candidate and candidate not in seen and os.path.isfile(candidate):
            seen.add(candidate)
            result.append(candidate)
    return result


def _get_with_system_ca_retry(http: Any, url: str, **kwargs):
    """GET normally, retrying SSL failures only with trusted system CAs.

    PyInstaller/Python distributions can carry a CA bundle that differs from
    the macOS system bundle. A failed TLS handshake has not sent the HTTP
    request, so retrying this package-scoped, idempotent cache clear is safe.
    Certificate verification is never disabled.
    """
    try:
        return http.get(url, **kwargs)
    except requests.exceptions.SSLError as original_error:
        last_error = original_error
        for ca_bundle in _system_ca_bundle_candidates():
            try:
                return http.get(url, verify=ca_bundle, **kwargs)
            except requests.exceptions.SSLError as exc:
                last_error = exc
        raise last_error


def apply_aggregation_type_fallback(
    fields: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Infer IronSource only from its exact standard placement-name pair."""
    if not isinstance(fields, dict):
        return fields
    verdict = normalize_optional_parameter(fields.get("最终判断"))
    if verdict:
        # A later AutoDetector pass can replace an earlier provisional
        # video/inter inference with an explicit verdict. Do not leave the
        # stale inference marker attached to that authoritative result.
        if verdict != INFERRED_IRONSOURCE_VERDICT:
            fields.pop("_aggregation_type_inferred", None)
        return fields
    rewarded = normalize_optional_parameter(fields.get("激励视频聚合id")).casefold()
    interstitial = normalize_optional_parameter(fields.get("插屏聚合id")).casefold()
    if rewarded == "video" and interstitial == "inter":
        fields["最终判断"] = INFERRED_IRONSOURCE_VERDICT
        fields["_aggregation_type_inferred"] = True
    return fields


def is_inferred_aggregation_result(fields: dict[str, Any] | None) -> bool:
    """Return whether the current verdict is specifically our provisional one."""
    fields = fields or {}
    return bool(fields.get("_aggregation_type_inferred")) and (
        normalize_optional_parameter(fields.get("最终判断"))
        == INFERRED_IRONSOURCE_VERDICT
    )


def has_aggregation_type(fields: dict[str, Any] | None) -> bool:
    """Return whether AutoDetector produced or safely inferred a verdict."""
    apply_aggregation_type_fallback(fields)
    return bool(normalize_optional_parameter((fields or {}).get("最终判断")))


def get_af_key(fields: dict[str, Any] | None) -> str:
    """Return the normalized AppsFlyer key from either supported field shape."""
    fields = fields or {}
    direct = normalize_optional_parameter(fields.get("af_key"))
    if direct:
        return direct
    for sdk in fields.get("SDK列表", []) or []:
        name = str(sdk.get("名称") or "").strip().casefold()
        if name in {"appsflyer", "apps flyer"}:
            key = normalize_optional_parameter(sdk.get("key"))
            if key:
                return key
    return ""


def requires_af_key(fields: dict[str, Any] | None) -> bool:
    """AppsFlyer attribution requires af_key before backend submission."""
    attribution = normalize_optional_parameter((fields or {}).get("归因平台"))
    return "appsflyer" in attribution.casefold().replace(" ", "")


def has_explicit_attribution(fields: dict[str, Any] | None) -> bool:
    """Return whether AutoDetector has actually emitted attribution data."""
    return bool(normalize_optional_parameter((fields or {}).get("归因平台")))


def attribution_gate_issue(fields: dict[str, Any] | None) -> tuple[str, str] | None:
    """Allow adaptation only when attribution contains Adjust or AppsFlyer."""
    attribution = normalize_optional_parameter((fields or {}).get("归因平台"))
    compact = attribution.casefold().replace(" ", "")
    if "adjust" in compact or "appsflyer" in compact:
        return None
    return "UNSUPPORTED_ATTRIBUTION", f"{attribution or '未知'}归因，暂不适配"


def get_ad_unit_id(fields: dict[str, Any] | None, field_name: str) -> str:
    """Return one usable ad ID, including IronSource symbolic placements."""
    fields = fields or {}
    apply_aggregation_type_fallback(fields)
    value = normalize_optional_parameter(fields.get(field_name))
    first = value.split(",", 1)[0].strip() if value else ""
    if first.casefold() in INVALID_AD_ID_VALUES:
        final = normalize_optional_parameter(fields.get("最终判断")).casefold()
        is_ironsource_family = "ironsource" in final or "levelplay" in final
        allowed = SYMBOLIC_AD_IDS_BY_FIELD.get(field_name, frozenset())
        if not is_ironsource_family or first.casefold() not in allowed:
            return ""
    return first


def has_any_ad_unit_id(fields: dict[str, Any] | None) -> bool:
    return bool(
        get_ad_unit_id(fields, "插屏聚合id")
        or get_ad_unit_id(fields, "激励视频聚合id")
    )


def has_partial_aggregation_evidence(fields: dict[str, Any] | None) -> bool:
    """Return whether progressive logs already contain mediation evidence."""
    fields = fields or {}
    if has_any_ad_unit_id(fields):
        return True
    aggregation_sdks = {
        "applovin", "max", "ironsource", "levelplay", "admob", "unityads"
    }
    for sdk in fields.get("SDK列表", []) or []:
        name = str(sdk.get("名称") or "").casefold().replace(" ", "")
        key = normalize_optional_parameter(sdk.get("key"))
        if key and any(token in name for token in aggregation_sdks):
            return True
    return False


def detection_field_issue(fields: dict[str, Any] | None) -> tuple[str, str] | None:
    """Return the terminal field issue that needs one detection retry."""
    if not has_aggregation_type(fields):
        if has_partial_aggregation_evidence(fields):
            return (
                "AGGREGATION_RESULT_INCOMPLETE",
                "已检测到聚合参数，但综合检测结果未完整输出，需要人工确认",
            )
        return "AGGREGATION_TYPE_EMPTY", "聚合类型识别为空"
    unsupported = attribution_gate_issue(fields)
    if unsupported:
        return unsupported
    if requires_af_key(fields) and not get_af_key(fields):
        return "AF_KEY_EMPTY", "af_key为空，再次确认"
    if not has_any_ad_unit_id(fields):
        return "AD_IDS_EMPTY", "插屏聚合id和激励视频聚合id均为空，再次确认"
    return None


def reconcile_detection_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Re-evaluate a stale terminal result against its final field snapshot.

    Logcat is progressive.  A caller can receive an ``incomplete`` code from
    an earlier snapshot while the returned field dictionary already contains
    the exact IronSource ``video``/``inter`` pair.  Always let the final
    structured fields win so this race cannot bypass the fallback rule.
    """
    normalized = dict(result or {})
    if normalized.get("ok"):
        return normalized
    if normalized.get("code") not in {
        "AGGREGATION_TYPE_EMPTY",
        "AGGREGATION_RESULT_INCOMPLETE",
        "AF_KEY_EMPTY",
        "AD_IDS_EMPTY",
    }:
        return normalized

    fields = normalized.get("fields")
    if not isinstance(fields, dict):
        return normalized
    issue = detection_field_issue(fields)
    if issue is None and has_aggregation_type(fields):
        normalized.update(
            {
                "ok": True,
                "code": "AGGREGATION_FIELDS_RECONCILED",
                "message": (
                    "最终字段已按 video/inter 规则推断为 IronSource，"
                    "已纠正较早的不完整结论"
                    if is_inferred_aggregation_result(fields)
                    else "最终字段已完整，已纠正较早的不完整结论"
                ),
            }
        )
        return normalized
    if issue is not None:
        normalized["code"], normalized["message"] = issue
    return normalized


def build_aggregation_assessment(fields: dict[str, Any] | None) -> dict[str, Any]:
    """Explain the aggregation verdict and whether it is safe to auto-submit."""
    fields = fields or {}
    apply_aggregation_type_fallback(fields)
    verdict = normalize_optional_parameter(fields.get("最终判断"))
    inferred = is_inferred_aggregation_result(fields)
    evidence: list[str] = []
    if inferred:
        method = "业务规则推断"
        confidence = "中"
        evidence.extend(
            [
                "AutoDetector 原始最终判断为空",
                "激励视频聚合id精确为 video",
                "插屏聚合id精确为 inter",
            ]
        )
    elif verdict:
        method = "AutoDetector明确判断"
        confidence = "高"
        evidence.append(f"AutoDetector 输出最终判断：{verdict}")
    else:
        method = "证据不足"
        confidence = "低"
        evidence.append("AutoDetector 未输出可用的最终判断")

    interstitial = get_ad_unit_id(fields, "插屏聚合id")
    rewarded = get_ad_unit_id(fields, "激励视频聚合id")
    if interstitial:
        evidence.append(f"检测到插屏聚合id：{interstitial}")
    if rewarded:
        evidence.append(f"检测到激励视频聚合id：{rewarded}")
    attribution = normalize_optional_parameter(fields.get("归因平台"))
    if attribution:
        evidence.append(f"检测到归因平台：{attribution}")

    log_text = str(fields.get("完整日志") or "")
    platform_token = ""
    verdict_compact = verdict.casefold().replace(" ", "")
    if "ironsource" in verdict_compact:
        platform_token = "IronSource"
    elif "levelplay" in verdict_compact:
        platform_token = "LevelPlay"
    elif "max" in verdict_compact or "applovin" in verdict_compact:
        platform_token = "AppLovin MAX"
    elif "admob" in verdict_compact:
        platform_token = "AdMob"
    if platform_token:
        match = re.search(
            rf"{re.escape(platform_token)}\s*:\s*(\d+)\s*次匹配",
            log_text,
            re.I,
        )
        if match:
            evidence.append(f"日志分析中 {platform_token} 匹配 {match.group(1)} 次")

    issue = detection_field_issue(fields)
    activity = normalize_optional_parameter(fields.get("初始Activity"))
    unsupported_attribution = bool(
        issue and issue[0] == "UNSUPPORTED_ATTRIBUTION"
    )
    auto_submit = (issue is None or unsupported_attribution) and bool(activity)
    if not activity:
        evidence.append("缺少初始 Activity，禁止自动提交")
    if issue and not unsupported_attribution:
        evidence.append(f"阻断原因：{issue[1]}")
    elif unsupported_attribution:
        evidence.append(f"终态规则：{issue[1]}，提交参数后跳过回放")
    return {
        "method": method,
        "confidence": confidence,
        "evidence": evidence,
        "auto_submit": auto_submit,
        "policy": (
            "允许提交并跳过回放"
            if auto_submit and unsupported_attribution
            else ("允许自动提交" if auto_submit else "禁止自动提交")
        ),
    }


def restart_app_for_aggregation_detection(package_name: str) -> tuple[bool, str]:
    """Restart one app with an empty log buffer before the retry detection."""
    package_name = str(package_name or "").strip()
    if not package_name:
        return False, "缺少包名，无法重启应用"
    adb = get_adb_path()
    try:
        stopped = subprocess.run(
            [adb, "shell", "am", "force-stop", package_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if stopped.returncode != 0:
            detail = (stopped.stderr or stopped.stdout or "").strip()
            return False, f"强制停止应用失败：{detail or '未知错误'}"
        # The second pass must not read the first pass from logcat again.
        subprocess.run(
            [adb, "logcat", "-c"], capture_output=True, text=True, timeout=8
        )
        launched = subprocess.run(
            [
                adb,
                "shell",
                "monkey",
                "-p",
                package_name,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = ((launched.stdout or "") + (launched.stderr or "")).strip()
        if launched.returncode != 0 or "No activities found" in output:
            return False, f"重新启动应用失败：{output or '未知错误'}"
        launch_summary = ""
        for line in reversed(output.splitlines()):
            clean = line.strip()
            if clean and (
                "Events injected" in clean
                or "Monkey finished" in clean
                or "cmp=" in clean
            ):
                launch_summary = clean
                break
        return True, (
            f"应用已重新启动（{launch_summary}）"
            if launch_summary
            else "应用已重新启动"
        )
    except FileNotFoundError:
        return False, "未找到 ADB 工具"
    except subprocess.TimeoutExpired:
        return False, "重新启动应用超时"


def detect_aggregation_with_one_retry(
    package_name: str,
    extract_fields,
    *,
    first_fields: dict[str, Any] | None = None,
    restart_app=restart_app_for_aggregation_detection,
    initial_wait_seconds: int = AGGREGATION_RETRY_WAIT_SECONDS,
    initial_poll_seconds: int = AGGREGATION_RETRY_POLL_SECONDS,
    wait_seconds: int = AGGREGATION_RETRY_WAIT_SECONDS,
    grace_seconds: int = AGGREGATION_RETRY_GRACE_SECONDS,
    poll_seconds: int = AGGREGATION_RETRY_POLL_SECONDS,
    sleep=time.sleep,
    stop_event=None,
    on_progress=None,
    runtime_check=None,
    runtime_reset=None,
    retry_inferred: bool = False,
) -> dict[str, Any]:
    """Extract aggregation fields and retry once after restarting the app."""
    progress = on_progress or (lambda _text: None)
    fields = dict(first_fields) if first_fields is not None else dict(extract_fields())
    if not fields.get("ok", True):
        if first_fields is None and fields.get("_transient"):
            progress(
                f"{fields.get('error', 'Logcat 暂时无法读取')}，继续等待检测日志"
            )
            fields = {"ok": True, "最终判断": ""}
        else:
            return {
                "ok": False,
                "code": fields.get("_runtime_code", "AGGREGATION_DETECTION_FAILED"),
                "message": fields.get("error", "聚合参数提取失败"),
                "fields": fields,
                "attempts": 1,
            }
    first_issue = detection_field_issue(fields)
    first_was_inferred = is_inferred_aggregation_result(fields)
    if first_was_inferred and retry_inferred:
        first_issue = (
            "AGGREGATION_TYPE_INFERRED",
            "聚合类型暂按 IronSource 推断，需要重启游戏复检明确结果",
        )
    # When the caller did not provide a first snapshot, the app may have just
    # been opened and AutoDetector may still be writing its result.  Do not
    # treat that initial empty snapshot as a failed detection.  Poll the
    # current logcat first so a normal 40-50 second detector run is captured.
    if first_fields is None and first_issue is not None:
        initial_wait_seconds = max(0, int(initial_wait_seconds))
        initial_poll_seconds = max(1, int(initial_poll_seconds))
        initial_elapsed = 0
        while initial_elapsed < initial_wait_seconds:
            if (
                first_issue[0] == "UNSUPPORTED_ATTRIBUTION"
                and has_explicit_attribution(fields)
            ):
                break
            if stop_event is not None and stop_event.is_set():
                return {
                    "ok": False,
                    "code": "AUTOMATION_STOPPED",
                    "message": "用户已停止自动化检测",
                    "fields": fields,
                    "attempts": 1,
                }
            if runtime_check is not None:
                runtime = dict(runtime_check() or {})
                if runtime and not runtime.get("ok", True):
                    return {
                        "ok": False,
                        "code": runtime.get("code", "APP_EXITED_DURING_AUTOMATION"),
                        "message": runtime.get("message", "应用在检测过程中异常退出"),
                        "fields": fields,
                        "runtime": runtime,
                        "attempts": 1,
                    }
            sleep_for = min(
                initial_poll_seconds,
                initial_wait_seconds - initial_elapsed,
            )
            if initial_elapsed == 0 or initial_elapsed % 10 == 0:
                progress(
                    f"首次聚合检测尚未完成，等待日志生成（剩余约 "
                    f"{initial_wait_seconds - initial_elapsed} 秒）"
                )
            sleep(sleep_for)
            initial_elapsed += sleep_for
            snapshot = dict(extract_fields())
            if not snapshot.get("ok", True):
                if snapshot.get("_transient"):
                    progress(
                        f"{snapshot.get('error', 'Logcat 暂时无法读取')}，继续等待检测日志"
                    )
                    continue
                return {
                    "ok": False,
                    "code": snapshot.get(
                        "_runtime_code", "AGGREGATION_DETECTION_FAILED"
                    ),
                    "message": snapshot.get("error", "聚合参数提取失败"),
                    "fields": snapshot,
                    "attempts": 1,
                }
            fields = snapshot
            first_issue = detection_field_issue(fields)
            first_was_inferred = is_inferred_aggregation_result(fields)
            if first_was_inferred and retry_inferred:
                first_issue = (
                    "AGGREGATION_TYPE_INFERRED",
                    "聚合类型暂按 IronSource 推断，需要重启游戏复检明确结果",
                )
            if first_issue is None:
                fields["_aggregation_detection_attempts"] = 1
                return {
                    "ok": True,
                    "code": "AGGREGATION_TYPE_DETECTED",
                    "message": "已识别聚合类型",
                    "fields": fields,
                    "attempts": 1,
                }
    if first_issue is None:
        fields["_aggregation_detection_attempts"] = 1
        return {
            "ok": True,
            "code": "AGGREGATION_TYPE_DETECTED",
            "message": "已识别聚合类型",
            "fields": fields,
            "attempts": 1,
        }
    if first_issue[0] == "UNSUPPORTED_ATTRIBUTION":
        fields["_adaptation_terminal"] = True
        return {
            "ok": False,
            "code": first_issue[0],
            "message": first_issue[1],
            "fields": fields,
            "attempts": 1,
        }

    if first_issue[0] == "AF_KEY_EMPTY":
        progress("首次 af_key 识别为空，正在重启游戏后重新检测")
    elif first_issue[0] == "AD_IDS_EMPTY":
        progress("首次插屏和激励视频聚合 ID 均为空，正在重启游戏后重新检测")
    elif first_issue[0] == "AGGREGATION_RESULT_INCOMPLETE":
        progress("首次检测已发现聚合参数，但综合结果未完整输出，正在重启游戏后重新检测")
    elif first_issue[0] == "AGGREGATION_TYPE_INFERRED":
        progress("首次仅能根据 video/inter 临时推断 IronSource，正在重启游戏确认真实聚合类型")
    else:
        progress("首次聚合类型识别为空，正在重启游戏后重新检测")
    restarted, restart_message = restart_app(package_name)
    if not restarted:
        return {
            "ok": False,
            "code": "AGGREGATION_RESTART_FAILED",
            "message": restart_message,
            "fields": fields,
            "attempts": 1,
        }
    if runtime_reset is not None:
        runtime_reset()
    progress(restart_message)

    wait_seconds = max(0, int(wait_seconds))
    grace_seconds = max(0, int(grace_seconds)) if wait_seconds > 0 else 0
    poll_seconds = max(1, int(poll_seconds))
    hard_timeout = wait_seconds + grace_seconds
    elapsed = 0
    last_log_signature = ""
    last_log_change = 0
    second_fields: dict[str, Any] = {}
    second_issue = first_issue
    last_transient_error: dict[str, Any] = {}

    # Poll throughout the retry instead of sleeping for a fixed period and
    # taking one snapshot at its boundary.  The device detector commonly emits
    # its final verdict at about 45 seconds, and a snapshot taken just before
    # that line used to restart/fail an otherwise valid detection.
    while elapsed <= hard_timeout:
        if stop_event is not None and stop_event.is_set():
            return {
                "ok": False,
                "code": "AUTOMATION_STOPPED",
                "message": "用户已停止自动化检测",
                "fields": fields,
                "attempts": 1,
            }
        if runtime_check is not None:
            runtime = dict(runtime_check() or {})
            if runtime and not runtime.get("ok", True):
                return {
                    "ok": False,
                    "code": runtime.get("code", "APP_EXITED_DURING_AUTOMATION"),
                    "message": runtime.get("message", "应用在检测过程中异常退出"),
                    "fields": fields,
                    "runtime": runtime,
                    "attempts": 2,
                }
        if elapsed == 0 or elapsed % 10 == 0:
            progress(
                f"游戏已重启，等待最终判断（最长剩余约 "
                f"{max(0, hard_timeout - elapsed)} 秒）"
            )
        snapshot = dict(extract_fields())
        if not snapshot.get("ok", True):
            if snapshot.get("_transient"):
                last_transient_error = snapshot
                progress(
                    f"{snapshot.get('error', 'Logcat 暂时无法读取')}，继续等待第二次检测日志"
                )
                if elapsed >= hard_timeout:
                    break
                sleep_for = min(poll_seconds, hard_timeout - elapsed)
                sleep(sleep_for)
                elapsed += sleep_for
                continue
            return {
                "ok": False,
                "code": snapshot.get(
                    "_runtime_code", "AGGREGATION_DETECTION_FAILED"
                ),
                "message": snapshot.get("error", "第二次聚合参数提取失败"),
                "fields": snapshot,
                "attempts": 2,
            }
        second_fields = snapshot
        second_fields["_aggregation_detection_attempts"] = 2
        second_issue = detection_field_issue(second_fields)
        second_is_inferred = is_inferred_aggregation_result(second_fields)
        # The provisional IronSource verdict must never hide a real blocking
        # field issue.  In particular, AppsFlyer with a missing af_key still
        # has to stop after the retry instead of being submitted merely because
        # video/inter allow the aggregation type itself to be inferred.
        if second_is_inferred and second_issue is None:
            second_issue = (
                "AGGREGATION_TYPE_INFERRED",
                "第二次仍只能根据 video/inter 推断 IronSource",
            )
        if second_issue is None:
            if first_was_inferred:
                second_fields["_aggregation_type_changed_after_retry"] = True
                return {
                    "ok": True,
                    "code": "AGGREGATION_TYPE_CHANGED_AFTER_RETRY",
                    "message": (
                        "复检取得 AutoDetector 明确判断，已放弃首次临时 "
                        "IronSource 推断，将按第二次聚合类型和参数提交"
                    ),
                    "fields": second_fields,
                    "attempts": 2,
                }
            return {
                "ok": True,
                "code": "AGGREGATION_TYPE_DETECTED_AFTER_RETRY",
                "message": {
                    "AF_KEY_EMPTY": "重启游戏后已提取 af_key",
                    "AD_IDS_EMPTY": "重启游戏后已提取聚合 ID",
                }.get(first_issue[0], "重启游戏后已识别聚合类型"),
                "fields": second_fields,
                "attempts": 2,
            }
        if (
            second_issue[0] == "UNSUPPORTED_ATTRIBUTION"
            and has_explicit_attribution(second_fields)
        ):
            second_fields["_adaptation_terminal"] = True
            return {
                "ok": False,
                "code": second_issue[0],
                "message": second_issue[1],
                "fields": second_fields,
                "attempts": 2,
            }

        signature = str(second_fields.get("完整日志") or "")
        if signature and signature != last_log_signature:
            last_log_signature = signature
            last_log_change = elapsed

        # Sixty seconds is the normal completion allowance.  After that, only
        # stop early when no new AutoDetector evidence has appeared for the
        # whole grace window; otherwise continue to the 90-second hard limit.
        if elapsed >= wait_seconds and elapsed - last_log_change >= grace_seconds:
            break
        if elapsed >= hard_timeout:
            break
        sleep_for = min(poll_seconds, hard_timeout - elapsed)
        sleep(sleep_for)
        elapsed += sleep_for

    if not second_fields and last_transient_error:
        return {
            "ok": False,
            "code": last_transient_error.get(
                "_runtime_code", "LOGCAT_READ_TIMEOUT"
            ),
            "message": last_transient_error.get(
                "error", "Logcat 持续无法读取，自动化检测已停止"
            ),
            "fields": last_transient_error,
            "attempts": 2,
        }
    second_fields["_aggregation_retry_exhausted"] = True
    second_fields["_detection_retry_exhausted"] = True
    if (
        is_inferred_aggregation_result(second_fields)
        and second_issue
        and second_issue[0] == "AGGREGATION_TYPE_INFERRED"
    ):
        return {
            "ok": True,
            "code": "AGGREGATION_TYPE_INFERRED_AFTER_RETRY",
            "message": (
                "两次均未得到明确最终判断，仍按 video/inter 规则临时推断 "
                "IronSource，后续必须通过广告回放验证"
            ),
            "fields": second_fields,
            "attempts": 2,
        }
    return {
        "ok": False,
        "code": second_issue[0],
        "message": second_issue[1],
        "fields": second_fields,
        "attempts": 2,
    }


def format_aggregation_fields(data: dict[str, Any]) -> str:
    """Format extracted fields exactly like the manual one-click copy action."""
    apply_aggregation_type_fallback(data)
    assessment = build_aggregation_assessment(data)
    lines = [
        f"最终判断:{data.get('最终判断', '')}",
        f"识别方式:{assessment['method']}",
        f"识别置信度:{assessment['confidence']}",
        f"识别依据:{'；'.join(assessment['evidence'])}",
        f"自动提交策略:{assessment['policy']}",
        f"初始Activity:{data.get('初始Activity', '')}",
    ]
    for sdk in data.get("SDK列表", []) or []:
        name = str(sdk.get("名称") or "未知")
        key = str(sdk.get("key") or "")
        lines.append(f"{name} SDK Key:{key}")
    lines.extend(
        [
            f"应用类型:{data.get('应用类型', '')}",
            f"激励视频聚合id:{data.get('激励视频聚合id', '')}",
            f"插屏聚合id:{data.get('插屏聚合id', '')}",
            f"归因平台:{data.get('归因平台', '')}",
        ]
    )
    return "\n".join(lines)


def merge_aggregation_fields_into_notes(existing_notes: str, fields_text: str) -> str:
    """Keep the package/AppId/GP header and put the new result below GP link.

    Existing aggregation results are replaced rather than appended.  This
    matches the current Asana record layout where the managed result occupies
    the part of the description below the GP link.
    """
    existing_notes = str(existing_notes or "").replace("\r\n", "\n").strip()
    fields_text = str(fields_text or "").replace("\r\n", "\n").strip()
    if not fields_text:
        raise ValueError("没有可回填的聚合参数")

    lines = existing_notes.splitlines()
    gp_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().lower().startswith(("gp链接:", "gp链接："))
        ),
        -1,
    )
    if gp_index >= 0:
        header = "\n".join(lines[: gp_index + 1]).rstrip()
        return f"{header}\n\n{fields_text}\n"
    if existing_notes:
        return f"{existing_notes}\n\n{fields_text}\n"
    return fields_text + "\n"


def update_asana_aggregation_notes(
    client,
    task_gid: str,
    existing_notes: str,
    fields: dict[str, Any],
    *,
    allow_unsupported_attribution: bool = False,
    terminal_note: str = "",
) -> str:
    """Write the formatted aggregation block to one known Asana task."""
    task_gid = str(task_gid or "").strip()
    if not task_gid:
        raise ValueError("当前自动化任务缺少 Asana task GID")
    if not has_aggregation_type(fields):
        raise ValueError("聚合类型识别为空，不能回填 Asana 描述")
    unsupported = attribution_gate_issue(fields)
    if unsupported and not allow_unsupported_attribution:
        raise ValueError(f"{unsupported[1]}，不能回填 Asana 描述")
    fields_text = format_aggregation_fields(fields)
    terminal_note = str(terminal_note or "").strip()
    if terminal_note:
        fields_text += f"\n适配结论:{terminal_note}"
    merged = merge_aggregation_fields_into_notes(existing_notes, fields_text)
    client.tasks.update_task(task_gid, {"notes": merged})
    return merged


def add_automation_comment_once(
    client,
    task_gid: str,
    code: str,
    text: str,
) -> bool:
    """Add one idempotent terminal automation comment."""
    task_gid = str(task_gid or "").strip()
    code = str(code or "UNKNOWN").strip().upper()
    marker = f"{AUTOMATION_COMMENT_PREFIX}{code}】"
    stories = client.stories.get_stories_for_task(
        task_gid,
        opt_fields=["text", "resource_subtype", "type"],
    )
    if any(marker in str(story.get("text") or "") for story in stories):
        return False
    body = str(text or "").strip()
    client.stories.create_comment(task_gid, marker + ("\n" + body if body else ""))
    return True


def validate_backend_fields(
    fields: dict[str, Any],
    package_name: str,
    *,
    allow_unsupported_attribution: bool = False,
) -> list[str]:
    """Return validation errors before opening the automation browser step."""
    errors: list[str] = []
    if not str(package_name or "").strip():
        errors.append("缺少包名")
    if not normalize_optional_parameter(fields.get("最终判断")):
        errors.append("缺少最终判断")
    unsupported = attribution_gate_issue(fields)
    if unsupported and not allow_unsupported_attribution:
        errors.append(unsupported[1])
    if not normalize_optional_parameter(fields.get("初始Activity")):
        errors.append("缺少初始 Activity")
    if not has_any_ad_unit_id(fields):
        errors.append("插屏和激励视频聚合 ID 均为空")
    if requires_af_key(fields) and not get_af_key(fields):
        errors.append("缺少 af_key（归因平台包含 AppsFlyer）")
    assessment = build_aggregation_assessment(fields)
    if not assessment["auto_submit"] and not errors:
        errors.append("聚合识别证据不足，禁止自动提交")
    return errors


def derive_backend_submit_url(list_or_submit_url: str) -> str:
    """Derive the S10 write endpoint from the existing data-sync list URL."""
    value = str(list_or_submit_url or "").strip()
    if not value:
        raise ValueError("缺少后台接口地址")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("后台接口地址格式无效")
    path = parsed.path.rstrip("/")
    if path.endswith("/s10_package_info"):
        submit_path = path
    elif path.endswith("/cp_adapt/list"):
        submit_path = path[: -len("/cp_adapt/list")] + "/s10_package_info"
    else:
        submit_path = path + "/s10_package_info"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, submit_path, "", "")
    )


def derive_backend_list_url(list_or_submit_url: str) -> str:
    """Derive the CP read endpoint from either configured backend endpoint."""
    value = str(list_or_submit_url or "").strip()
    if not value:
        raise ValueError("缺少后台接口地址")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("后台接口地址格式无效")
    path = parsed.path.rstrip("/")
    if path.endswith("/cp_adapt/list"):
        list_path = path
    elif path.endswith("/s10_package_info"):
        list_path = path[: -len("/s10_package_info")] + "/cp_adapt/list"
    else:
        list_path = path + "/cp_adapt/list"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, list_path, "", ""))


def build_backend_submission_payload(
    fields: dict[str, Any],
    package_name: str,
    user_name: str = "rain",
) -> dict[str, Any]:
    """Build the API payload using the established manual URL field mapping."""
    apply_aggregation_type_fallback(fields)
    backend_url = build_backend_url(fields, package_name.strip())
    query = backend_url.split("?", 1)[1] if "?" in backend_url else ""
    mapped = {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            query, keep_blank_values=True
        ).items()
        if key != "change"
    }
    payload = {
        "package_name": mapped.get("package_name") or package_name.strip(),
        "aggr_platform": mapped.get("aggr_platform"),
        "attribution_platform": mapped.get("attribution_platform"),
        "aggr_chaping_id": mapped.get("aggr_chaping_id"),
        "aggr_jilishipin_id": mapped.get("aggr_jilishipin_id"),
        "ps": None,
        "block_ps": None,
        "af_key": mapped.get("af_key"),
        "manual_applovin_sdk_key": mapped.get("manual_applovin_sdk_key"),
        "activity_main_page": mapped.get("activity_main_page"),
        "activity_guide_page": None,
        "user_name": str(user_name or "rain").strip() or "rain",
    }
    return payload


def _verify_backend_response_record(
    payload: dict[str, Any], response_record: dict[str, Any]
) -> list[str]:
    """Return field mismatches between submitted and persisted response data."""
    checked_fields = (
        "package_name",
        "aggr_platform",
        "attribution_platform",
        "aggr_chaping_id",
        "aggr_jilishipin_id",
        "ps",
        "block_ps",
        "af_key",
        "manual_applovin_sdk_key",
        "activity_main_page",
        "activity_guide_page",
    )
    mismatches = []
    for key in checked_fields:
        expected = "" if payload.get(key) is None else str(payload.get(key)).strip()
        actual = (
            ""
            if response_record.get(key) is None
            else str(response_record.get(key)).strip()
        )
        if expected != actual:
            mismatches.append(key)
    return mismatches


def _extract_backend_list_records(body: Any) -> list[dict[str, Any]]:
    """Extract CP records from the list endpoint's nested response envelope."""
    if not isinstance(body, dict) or body.get("code") != 200:
        return []
    data = body.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    if data.get("code") not in (None, 200):
        return []
    records = data.get("data")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    if isinstance(records, dict):
        return [records]
    return []


def verify_backend_persisted_record(
    http: Any,
    *,
    api_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: int = 60,
    attempts: int = BACKEND_READBACK_ATTEMPTS,
    delay_seconds: float = BACKEND_READBACK_DELAY_SECONDS,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Read the CP record back and strictly compare all submitted key fields."""
    list_url = derive_backend_list_url(api_url)
    package_name = str(payload.get("package_name") or "").strip()
    query = {
        "is_adapted": "all",
        "hide_remarked": False,
        "hide_no_up2_appid": False,
        "assign": "",
        "package_name": package_name,
        "limit": 999,
        "page": 1,
    }
    attempts = max(1, int(attempts))
    last_result: dict[str, Any] = {
        "ok": False,
        "code": "BACKEND_READBACK_NOT_FOUND",
        "message": f"后台提交后回读不到包名 {package_name}",
        "url": list_url,
    }
    for attempt in range(1, attempts + 1):
        try:
            response = http.post(
                list_url,
                headers=headers,
                json=query,
                timeout=max(10, min(int(timeout_seconds), 120)),
            )
            response.raise_for_status()
            body = response.json()
        except requests.Timeout:
            last_result = {
                "ok": False,
                "code": "BACKEND_READBACK_TIMEOUT",
                "message": "后台参数已提交并清除缓存，但回读校验超时；不会进入聚合回放",
                "url": list_url,
            }
        except (requests.RequestException, ValueError) as exc:
            last_result = {
                "ok": False,
                "code": "BACKEND_READBACK_FAILED",
                "message": f"后台参数已提交并清除缓存，但回读校验失败：{exc}",
                "url": list_url,
            }
        else:
            if not isinstance(body, dict) or body.get("code") != 200:
                last_result = {
                    "ok": False,
                    "code": "BACKEND_READBACK_REJECTED",
                    "message": (
                        "后台参数已提交并清除缓存，但回读接口返回异常："
                        f"code={body.get('code') if isinstance(body, dict) else 'invalid'}"
                    ),
                    "url": list_url,
                }
            else:
                records = _extract_backend_list_records(body)
                record = next(
                    (
                        item
                        for item in records
                        if str(item.get("package_name") or "").strip()
                        == package_name
                    ),
                    None,
                )
                if record is None:
                    last_result = {
                        "ok": False,
                        "code": "BACKEND_READBACK_NOT_FOUND",
                        "message": f"后台提交后回读不到包名 {package_name}",
                        "url": list_url,
                    }
                else:
                    mismatches = _verify_backend_response_record(payload, record)
                    if not mismatches:
                        return {
                            "ok": True,
                            "code": "BACKEND_READBACK_VERIFIED",
                            "message": "CP 后台回读字段一致",
                            "url": list_url,
                            "record": record,
                            "attempt": attempt,
                        }
                    last_result = {
                        "ok": False,
                        "code": "BACKEND_READBACK_MISMATCH",
                        "message": (
                            "后台提交后回读字段不一致：" + ", ".join(mismatches)
                        ),
                        "url": list_url,
                        "record": record,
                        "mismatches": mismatches,
                    }
        if attempt < attempts and delay_seconds > 0:
            sleep(delay_seconds)
    return last_result


def submit_backend_via_api(
    fields: dict[str, Any],
    package_name: str,
    *,
    api_url: str,
    x_token: str,
    token: str,
    user_name: str = "rain",
    timeout_seconds: int = 60,
    readback_attempts: int = BACKEND_READBACK_ATTEMPTS,
    readback_delay_seconds: float = BACKEND_READBACK_DELAY_SECONDS,
    sleep=time.sleep,
    stop_event=None,
    session: Any = None,
    allow_unsupported_attribution: bool = False,
) -> dict:
    """Submit aggregation parameters through the CP backend API and verify them."""
    errors = validate_backend_fields(
        fields,
        package_name,
        allow_unsupported_attribution=allow_unsupported_attribution,
    )
    if not str(api_url or "").strip():
        errors.append("缺少后台接口地址")
    if not str(x_token or "").strip():
        errors.append("缺少后台 X-Token")
    if not str(token or "").strip():
        errors.append("缺少后台固定 token")
    if errors:
        return {
            "ok": False,
            "code": "BACKEND_VALIDATION_FAILED",
            "message": "；".join(errors),
        }
    if stop_event is not None and stop_event.is_set():
        return {
            "ok": False,
            "code": "USER_STOPPED",
            "message": "用户已停止自动化，未提交后台参数",
        }

    submit_url = derive_backend_submit_url(api_url)
    payload = build_backend_submission_payload(fields, package_name, user_name)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "http://data_center_web_internet.hongdinghe.cn",
        "Referer": "http://data_center_web_internet.hongdinghe.cn/",
        "X-Token": str(x_token).strip(),
        "token": str(token).strip(),
    }
    http = session or requests.Session()
    try:
        response = http.post(
            submit_url,
            headers=headers,
            json=payload,
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        response.raise_for_status()
        body = response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "BACKEND_SUBMIT_TIMEOUT",
            "message": "后台接口提交超时",
            "url": submit_url,
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "BACKEND_SUBMIT_FAILED",
            "message": f"后台接口提交失败：{exc}",
            "url": submit_url,
        }

    if not isinstance(body, dict) or body.get("code") != 200:
        return {
            "ok": False,
            "code": "BACKEND_SUBMIT_REJECTED",
            "message": f"后台接口返回异常：code={body.get('code') if isinstance(body, dict) else 'invalid'}",
            "url": submit_url,
        }
    record = body.get("data") or {}
    if not isinstance(record, dict):
        record = {}
    mismatches = _verify_backend_response_record(payload, record)
    if mismatches:
        return {
            "ok": False,
            "code": "BACKEND_VERIFY_FAILED",
            "message": "后台已响应，但返回字段校验不一致：" + ", ".join(mismatches),
            "url": submit_url,
        }

    # The S10 write endpoint persists fields, while A2 serves package config
    # through a cache.  The new values do not take effect on device until this
    # package-scoped cache is cleared, so treat both calls as one commit step.
    try:
        cache_response = _get_with_system_ca_retry(
            http,
            BACKEND_CACHE_CLEAR_URL,
            headers=headers,
            params={"package_name": package_name.strip()},
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        cache_response.raise_for_status()
        cache_body = cache_response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "BACKEND_CACHE_CLEAR_TIMEOUT",
            "message": "聚合参数已提交，但清除 A2 包缓存超时；不会进入聚合回放",
            "url": BACKEND_CACHE_CLEAR_URL,
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "BACKEND_CACHE_CLEAR_FAILED",
            "message": f"聚合参数已提交，但清除 A2 包缓存失败：{exc}",
            "url": BACKEND_CACHE_CLEAR_URL,
        }
    if (
        not isinstance(cache_body, dict)
        or cache_body.get("code") != 200
        or str(cache_body.get("message") or "").casefold() != "success"
    ):
        return {
            "ok": False,
            "code": "BACKEND_CACHE_CLEAR_REJECTED",
            "message": (
                "聚合参数已提交，但清缓存接口返回异常："
                f"code={cache_body.get('code') if isinstance(cache_body, dict) else 'invalid'}"
            ),
            "url": BACKEND_CACHE_CLEAR_URL,
        }
    readback = verify_backend_persisted_record(
        http,
        api_url=api_url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        attempts=readback_attempts,
        delay_seconds=readback_delay_seconds,
        sleep=sleep,
    )
    if not readback.get("ok"):
        return readback
    if stop_event is not None and stop_event.is_set():
        return {
            "ok": False,
            "code": "USER_STOPPED_AFTER_SUBMIT",
            "message": (
                "聚合参数已提交、清除缓存并通过后台回读校验，但用户已停止；"
                "不会进入聚合回放"
            ),
            "url": submit_url,
            "backend_readback_verified": True,
        }
    return {
        "ok": True,
        "code": "BACKEND_SUBMITTED",
        "message": (
            "聚合参数已通过接口提交、清除 A2 包缓存，并经 CP 后台回读确认字段已生效；"
            "设备侧生效由接下来的真实广告回放验证"
        ),
        "url": submit_url,
        "backend_readback_verified": True,
        "backend_record": readback.get("record"),
        "device_verification": "pending_replay",
    }


def build_backend_clear_payload(
    package_name: str,
    user_name: str = "rain",
    note: str = INFERRED_AGGREGATION_FAILURE_NOTE,
) -> dict[str, Any]:
    """Build a terminal payload that removes every adaptation parameter."""
    return {
        "package_name": str(package_name or "").strip(),
        "aggr_platform": None,
        "attribution_platform": None,
        "aggr_chaping_id": None,
        "aggr_jilishipin_id": None,
        "ps": str(note or INFERRED_AGGREGATION_FAILURE_NOTE).strip(),
        "block_ps": None,
        "af_key": None,
        "manual_applovin_sdk_key": None,
        "activity_main_page": None,
        "activity_guide_page": None,
        "user_name": str(user_name or "rain").strip() or "rain",
    }


def clear_backend_adaptation_via_api(
    package_name: str,
    *,
    api_url: str,
    x_token: str,
    token: str,
    user_name: str = "rain",
    note: str = INFERRED_AGGREGATION_FAILURE_NOTE,
    timeout_seconds: int = 60,
    readback_attempts: int = BACKEND_READBACK_ATTEMPTS,
    readback_delay_seconds: float = BACKEND_READBACK_DELAY_SECONDS,
    sleep=time.sleep,
    session: Any = None,
) -> dict[str, Any]:
    """Clear an inferred aggregation submission after replay cannot verify it."""
    missing = []
    package_name = str(package_name or "").strip()
    if not package_name:
        missing.append("缺少包名")
    if not str(api_url or "").strip():
        missing.append("缺少后台接口地址")
    if not str(x_token or "").strip():
        missing.append("缺少后台 X-Token")
    if not str(token or "").strip():
        missing.append("缺少后台固定 token")
    if missing:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_VALIDATION_FAILED",
            "message": "；".join(missing),
        }

    try:
        submit_url = derive_backend_submit_url(api_url)
    except ValueError as exc:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_VALIDATION_FAILED",
            "message": str(exc),
        }
    payload = build_backend_clear_payload(package_name, user_name, note)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "http://data_center_web_internet.hongdinghe.cn",
        "Referer": "http://data_center_web_internet.hongdinghe.cn/",
        "X-Token": str(x_token).strip(),
        "token": str(token).strip(),
    }
    http = session or requests.Session()
    try:
        response = http.post(
            submit_url,
            headers=headers,
            json=payload,
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        response.raise_for_status()
        body = response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_TIMEOUT",
            "message": "清空后台适配参数超时",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_FAILED",
            "message": f"清空后台适配参数失败：{exc}",
        }

    record = body.get("data") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("code") != 200 or not isinstance(record, dict):
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_REJECTED",
            "message": "后台未接受适配参数清空请求",
        }
    mismatches = _verify_backend_response_record(payload, record)
    if mismatches:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_VERIFY_FAILED",
            "message": "后台清空响应字段不一致：" + ", ".join(mismatches),
        }

    try:
        cache_response = _get_with_system_ca_retry(
            http,
            BACKEND_CACHE_CLEAR_URL,
            headers=headers,
            params={"package_name": package_name},
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        cache_response.raise_for_status()
        cache_body = cache_response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_CACHE_TIMEOUT",
            "message": "适配参数已清空，但刷新 A2 包缓存超时",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_CACHE_FAILED",
            "message": f"适配参数已清空，但刷新 A2 包缓存失败：{exc}",
        }
    if (
        not isinstance(cache_body, dict)
        or cache_body.get("code") != 200
        or str(cache_body.get("message") or "").casefold() != "success"
    ):
        return {
            "ok": False,
            "code": "BACKEND_CLEAR_CACHE_REJECTED",
            "message": "适配参数已清空，但清缓存接口返回异常",
        }

    readback = verify_backend_persisted_record(
        http,
        api_url=api_url,
        headers=headers,
        payload=payload,
        timeout_seconds=timeout_seconds,
        attempts=readback_attempts,
        delay_seconds=readback_delay_seconds,
        sleep=sleep,
    )
    if not readback.get("ok"):
        return {
            **readback,
            "code": "BACKEND_CLEAR_READBACK_FAILED",
            "message": "已提交清空并刷新缓存，但后台回读未确认全部字段已清空："
            + str(readback.get("message") or ""),
        }
    return {
        "ok": True,
        "code": "BACKEND_ADAPTATION_CLEARED",
        "message": "后台适配参数已全部清空、备注已保留，并刷新缓存及回读确认",
        "backend_record": readback.get("record"),
    }


def submit_precheck_blacklist_via_api(
    result: dict[str, Any],
    *,
    api_url: str,
    x_token: str,
    token: str,
    user_name: str = "rain",
    timeout_seconds: int = 60,
    readback_attempts: int = BACKEND_READBACK_ATTEMPTS,
    readback_delay_seconds: float = BACKEND_READBACK_DELAY_SECONDS,
    sleep=time.sleep,
    session: Any = None,
) -> dict[str, Any]:
    """Persist a terminal Play precheck decision and clear the A2 package cache."""
    code = str((result or {}).get("code") or "").strip().upper()
    package_name = str((result or {}).get("package_name") or "").strip()
    reason = PRECHECK_BLACKLIST_REASONS.get(code, "")
    missing = []
    if not reason:
        missing.append("当前预检结果不是可自动提交的终止类型")
    if not package_name:
        missing.append("缺少包名")
    if not str(api_url or "").strip():
        missing.append("缺少后台接口地址")
    if not str(x_token or "").strip():
        missing.append("缺少后台 X-Token")
    if not str(token or "").strip():
        missing.append("缺少后台固定 token")
    if missing:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_VALIDATION_FAILED",
            "message": "；".join(missing),
        }

    try:
        list_url = derive_backend_list_url(api_url)
        submit_url = derive_backend_submit_url(api_url)
    except ValueError as exc:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_VALIDATION_FAILED",
            "message": str(exc),
        }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "http://data_center_web_internet.hongdinghe.cn",
        "Referer": "http://data_center_web_internet.hongdinghe.cn/",
        "X-Token": str(x_token).strip(),
        "token": str(token).strip(),
    }
    query = {
        "is_adapted": "all",
        "hide_remarked": False,
        "hide_no_up2_appid": False,
        "assign": "",
        "package_name": package_name,
        "limit": 999,
        "page": 1,
    }
    http = session or requests.Session()
    try:
        lookup_response = http.post(
            list_url,
            headers=headers,
            json=query,
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        lookup_response.raise_for_status()
        records = _extract_backend_list_records(lookup_response.json())
    except requests.Timeout:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_LOOKUP_TIMEOUT",
            "message": "查询待加黑包体的后台记录超时",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_LOOKUP_FAILED",
            "message": f"查询待加黑包体的后台记录失败：{exc}",
        }
    record = next(
        (
            item
            for item in records
            if str(item.get("package_name") or "").strip() == package_name
        ),
        None,
    )
    if record is None:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_RECORD_NOT_FOUND",
            "message": f"后台未找到包名 {package_name}，未执行预检结论提交",
        }

    preserved_fields = (
        "aggr_platform",
        "attribution_platform",
        "aggr_chaping_id",
        "aggr_jilishipin_id",
        "ps",
        "af_key",
        "manual_applovin_sdk_key",
        "activity_main_page",
        "activity_guide_page",
    )
    payload = {key: record.get(key) for key in preserved_fields}
    payload.update(
        {
            "package_name": package_name,
            "block_ps": reason,
            "user_name": str(user_name or "rain").strip() or "rain",
        }
    )
    try:
        submit_response = http.post(
            submit_url,
            headers=headers,
            json=payload,
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        submit_response.raise_for_status()
        submit_body = submit_response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_SUBMIT_TIMEOUT",
            "message": "后台预检结论接口提交超时",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_SUBMIT_FAILED",
            "message": f"后台预检结论接口提交失败：{exc}",
        }
    submitted_record = submit_body.get("data") if isinstance(submit_body, dict) else None
    if (
        not isinstance(submit_body, dict)
        or submit_body.get("code") != 200
        or not isinstance(submitted_record, dict)
        or str(submitted_record.get("block_ps") or "").strip() != reason
    ):
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_SUBMIT_REJECTED",
            "message": "后台接口未返回一致的 block_ps 字段",
        }

    try:
        cache_response = _get_with_system_ca_retry(
            http,
            BACKEND_CACHE_CLEAR_URL,
            headers=headers,
            params={"package_name": package_name},
            timeout=max(10, min(int(timeout_seconds), 120)),
        )
        cache_response.raise_for_status()
        cache_body = cache_response.json()
    except requests.Timeout:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_CACHE_TIMEOUT",
            "message": "后台结论已提交，但刷新 A2 包缓存超时",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_CACHE_FAILED",
            "message": f"后台结论已提交，但刷新 A2 包缓存失败：{exc}",
        }
    if (
        not isinstance(cache_body, dict)
        or cache_body.get("code") != 200
        or str(cache_body.get("message") or "").casefold() != "success"
    ):
        return {
            "ok": False,
            "code": "PRECHECK_BLACKLIST_CACHE_REJECTED",
            "message": "后台结论已提交，但清缓存接口返回异常",
        }

    attempts = max(1, int(readback_attempts))
    for attempt in range(1, attempts + 1):
        try:
            readback_response = http.post(
                list_url,
                headers=headers,
                json=query,
                timeout=max(10, min(int(timeout_seconds), 120)),
            )
            readback_response.raise_for_status()
            readback_records = _extract_backend_list_records(readback_response.json())
        except (requests.RequestException, ValueError):
            readback_records = []
        persisted = next(
            (
                item
                for item in readback_records
                if str(item.get("package_name") or "").strip() == package_name
            ),
            None,
        )
        if persisted and str(persisted.get("block_ps") or "").strip() == reason:
            return {
                "ok": True,
                "code": "PRECHECK_BLACKLIST_SUBMITTED",
                "message": "预检结论已通过接口提交、刷新 A2 包缓存并回读确认生效",
                "reason": reason,
                "backend_record": persisted,
            }
        if attempt < attempts and readback_delay_seconds > 0:
            sleep(readback_delay_seconds)
    return {
        "ok": False,
        "code": "PRECHECK_BLACKLIST_READBACK_FAILED",
        "message": "已提交预检结论并刷新缓存，但后台回读未确认 block_ps 生效",
    }


def _applescript_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_chrome_submit_script(
    backend_url: str,
    wait_seconds: int = 45,
    expected_package: str = "",
) -> str:
    """Build a Chrome AppleScript that submits in a dedicated new tab.

    Chrome requires “Allow JavaScript from Apple Events”.  Runtime failures are
    returned to the automation flow and can be written to Asana.
    """
    wait_seconds = max(10, min(int(wait_seconds), 120))
    click_js = r"""(() => {
 const visible = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length))
   && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none';
 if (document.readyState !== 'complete') return 'page_loading';
 const expectedPackage = __EXPECTED_PACKAGE__;
 const roots = [document];
 for (const frame of document.querySelectorAll('iframe')) {
   try { if (frame.contentDocument) roots.push(frame.contentDocument); } catch (_) {}
 }
 const candidates = roots.flatMap(root => Array.from(root.querySelectorAll(
   'button,[role="button"],input[type="submit"],.el-button,.ant-btn'
 ))).filter(visible).filter(e => {
   const label = String(e.innerText || e.textContent || e.value || '')
     .replace(/\s+/g, ' ').trim();
   return label === '提交' && !e.disabled && e.getAttribute('aria-disabled') !== 'true';
 });
 if (!candidates.length) return 'submit_waiting';
 const packageMatches = scope => {
   if (!expectedPackage) return true;
   const expected = expectedPackage.toLowerCase();
   const values = Array.from(scope.querySelectorAll('input,textarea'))
     .map(e => String(e.value || '').trim().toLowerCase());
   return values.includes(expected)
     || String(scope.textContent || '').toLowerCase().includes(expected);
 };
 let submit = null;
 for (const candidate of candidates) {
   const scope = candidate.closest(
     '[role="dialog"],[aria-modal="true"],.el-dialog,.el-dialog__wrapper,' +
     '.ant-modal,.ant-modal-wrap,.vxe-modal--box,.modal,.modal-dialog,form'
   ) || candidate.ownerDocument.body;
   if (packageMatches(scope)) { submit = candidate; break; }
 }
 if (!submit) return 'package_waiting';
 submit.scrollIntoView({block: 'center', inline: 'center'});
 for (const type of ['mousedown', 'mouseup', 'click']) {
   submit.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
 }
 if (submit.tagName === 'INPUT') submit.click();
 return 'clicked';
})()""".replace(
        "__EXPECTED_PACKAGE__", _applescript_string(expected_package.strip())
    )
    verify_js = r"""(() => {
 const visible = e => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
 const success = Array.from(document.querySelectorAll(
   '.el-message--success,.ant-message-success,.ant-notification-notice-success,[role="alert"]'
 ))
   .filter(visible).some(e => /成功|success/i.test(e.textContent || ''));
 if (success) return 'submitted';
 const dialogs = Array.from(document.querySelectorAll(
   '[role="dialog"],[aria-modal="true"],.el-dialog__wrapper,.el-dialog,' +
   '.ant-modal-wrap,.ant-modal,.vxe-modal--box,.modal-dialog'
 )).filter(visible);
 const scope = dialogs.find(d => /修改适配信息/.test(d.textContent || ''))
   || dialogs.find(d => Array.from(d.querySelectorAll('button')).some(
     b => (b.textContent || '').trim() === '提交'));
 if (!scope) return 'submitted';
 return 'waiting';
})()"""
    return f"""on run
    tell application "Google Chrome"
        activate
        if (count of windows) is 0 then make new window
        set targetWindow to front window
        tell targetWindow
            set targetTab to make new tab at end of tabs with properties {{URL:{_applescript_string(backend_url)}}}
            set active tab index to (count of tabs)
        end tell
    end tell
    set clickedSubmit to false
    set lastClickResult to "page_loading"
    set lastScriptError to ""
    repeat {wait_seconds * 2} times
        delay 0.5
        try
            tell application "Google Chrome"
                set clickResult to execute targetTab javascript {_applescript_string(click_js)}
            end tell
            set lastClickResult to clickResult
            if clickResult is "clicked" then
                set clickedSubmit to true
                exit repeat
            end if
        on error errMsg number errNum
            set lastScriptError to errMsg & " (" & errNum & ")"
        end try
    end repeat
    if clickedSubmit is false then
        if lastScriptError is not "" then error "Chrome JavaScript 执行失败：" & lastScriptError & "；请确认 Chrome 已开启‘允许来自 Apple 事件的 JavaScript’"
        if lastClickResult is "package_waiting" then error "后台表单包名与当前任务不一致，已停止自动提交"
        if lastClickResult is "dialog_waiting" then error "未打开修改适配信息弹窗；请检查登录状态和页面加载情况"
        if lastClickResult is "page_loading" then error "后台页面在等待时间内未加载完成"
        error "后台弹窗中未找到可点击的提交按钮；请检查页面是否完整加载"
    end if
    repeat 20 times
        delay 0.5
        try
            tell application "Google Chrome"
                set verifyResult to execute targetTab javascript {_applescript_string(verify_js)}
            end tell
            if verifyResult is "submitted" then return "submitted"
        end try
    end repeat
    error "已点击提交，但未确认后台提交成功"
end run"""


def auto_submit_backend_url(
    fields: dict[str, Any],
    package_name: str,
    wait_seconds: int = 45,
    stop_event=None,
) -> dict:
    """Open the parameter URL in signed-in Chrome and click the visible submit."""
    errors = validate_backend_fields(fields, package_name)
    if errors:
        return {
            "ok": False,
            "code": "BACKEND_VALIDATION_FAILED",
            "message": "；".join(errors),
        }
    if os.name != "posix" or subprocess.run(
        ["uname", "-s"], capture_output=True, text=True, timeout=3
    ).stdout.strip() != "Darwin":
        return {
            "ok": False,
            "code": "BACKEND_BROWSER_UNSUPPORTED",
            "message": "当前自动提交仅支持 macOS Google Chrome",
        }

    url = build_backend_url(fields, package_name.strip())
    script = build_chrome_submit_script(
        url,
        wait_seconds=wait_seconds,
        expected_package=package_name.strip(),
    )
    process = subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + max(30, int(wait_seconds) + 20)
    while process.poll() is None:
        if stop_event is not None and stop_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            return {
                "ok": False,
                "code": "USER_STOPPED",
                "message": "用户已停止自动化，后台提交已取消",
                "url": url,
            }
        if time.monotonic() >= deadline:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            return {
                "ok": False,
                "code": "BACKEND_SUBMIT_TIMEOUT",
                "message": "后台页面自动提交超时",
                "url": url,
            }
        time.sleep(0.2)
    stdout, stderr = process.communicate()
    result = subprocess.CompletedProcess(
        process.args, process.returncode, stdout=stdout, stderr=stderr
    )
    if result.returncode == 0 and "submitted" in (result.stdout or ""):
        return {
            "ok": True,
            "code": "BACKEND_SUBMITTED",
            "message": "已在 Chrome 新标签页打开后台并自动提交参数",
            "url": url,
        }
    detail = (result.stderr or result.stdout or "").strip()
    if "Apple Events" in detail or "JavaScript" in detail or "Apple 事件" in detail:
        detail += "；请在 Chrome 菜单“查看→开发者”中允许来自 Apple 事件的 JavaScript"
    return {
        "ok": False,
        "code": "BACKEND_SUBMIT_FAILED",
        "message": detail or "后台页面自动提交失败",
        "url": url,
    }
