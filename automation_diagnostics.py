"""Local, read-only diagnostics for per-package automation reports.

Run ``python3 automation_diagnostics.py --days 7`` from the project root.
The report deliberately distinguishes execution attempts from package outcomes;
Asana and manual decisions are outside this local data source.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any


EMPTY_VALUES = {"", "未找到", "暂未找到", "none", "null", "unknown"}
FINAL_STATUSES = {"success", "failed", "skipped", "needs_review", "requeued"}
FIELD_RETRY_CODES = {"AF_KEY_EMPTY", "AD_IDS_EMPTY", "AGGREGATION_TYPE_EMPTY"}


def _present(value: Any) -> bool:
    return str(value or "").strip().casefold() not in EMPTY_VALUES


def _ids(value: Any) -> tuple[str, ...]:
    if isinstance(value, (tuple, list)):
        parts = value
    else:
        parts = str(value or "").replace("，", ",").split(",")
    return tuple(part.strip() for part in parts if _present(part))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def field_snapshot(fields: dict[str, Any] | None) -> dict[str, Any]:
    """Small, key-free state for timeline events and diagnostic comparisons."""
    fields = fields or {}
    return {
        "aggregation": str(fields.get("最终判断") or "").strip()[:100],
        "attribution": str(fields.get("归因平台") or "").strip()[:100],
        "interstitial_count": len(_ids(fields.get("插屏聚合id"))),
        "rewarded_count": len(_ids(fields.get("激励视频聚合id"))),
        "af_key_present": _present(fields.get("af_key")) or any(
            str(sdk.get("名称") or "").strip().casefold() in {"appsflyer", "apps flyer"}
            and _present(sdk.get("key"))
            for sdk in fields.get("SDK列表", []) or []
            if isinstance(sdk, dict)
        ),
        "detection_attempts": _safe_int(fields.get("_aggregation_detection_attempts")),
        "detection_retry_exhausted": bool(fields.get("_detection_retry_exhausted")),
        "runtime_code": str(fields.get("_runtime_code") or "")[:80],
        "runtime_rule": str(fields.get("_runtime_rule_id") or "")[:80],
    }


def replay_snapshot(result: dict[str, Any] | None, *, round_number: int) -> dict[str, Any]:
    """Keep replay evidence without raw ad IDs or SDK logs."""
    result = result or {}
    summary = {
        "round": round_number,
        "code": str(result.get("code") or "")[:80],
        "ok": bool(result.get("ok")),
    }
    for ad_type in ("interstitial", "rewarded"):
        state = result.get(ad_type) or {}
        summary[ad_type] = {
            "required": bool(state.get("required")),
            "request_observed": bool(state.get("request_observed")),
            "displayed": bool(state.get("displayed")),
            "error_count": len(state.get("errors") or []),
        }
    return summary


def _field_signature(fields: dict[str, Any] | None) -> tuple[Any, ...]:
    """Compare evidence exactly in memory; never print IDs or keys."""
    fields = fields or {}
    snap = field_snapshot(fields)
    return (
        snap["aggregation"],
        snap["attribution"],
        _ids(fields.get("插屏聚合id")),
        _ids(fields.get("激励视频聚合id")),
        snap["af_key_present"],
        snap["runtime_code"],
    )


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def load_reports(directory: str | Path, *, days: int = 7, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=max(1, days))
    reports = []
    for path in Path(directory).glob("*/*.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or not report.get("package_name"):
            continue
        started = _parse_time(report.get("started_at"))
        if started is None or started.astimezone(now.tzinfo) < cutoff:
            continue
        report["_report_path"] = str(path)
        reports.append(report)
    return sorted(reports, key=lambda item: (item.get("started_at", ""), item.get("report_id", "")))


def analyze_reports(reports: list[dict], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    by_package: dict[str, list[dict]] = defaultdict(list)
    for report in reports:
        by_package[str(report.get("package_name") or "")].append(report)
    findings: list[dict[str, Any]] = []

    def add(package: str, code: str, severity: str, evidence: list[dict], detail: str) -> None:
        findings.append({
            "package_name": package,
            "code": code,
            "severity": severity,
            "detail": detail,
            "report_paths": [item.get("_report_path", "") for item in evidence],
        })

    for package, history in by_package.items():
        history.sort(key=lambda item: (item.get("started_at", ""), item.get("report_id", "")))
        latest = history[-1]
        for previous, current in zip(history, history[1:]):
            if (
                previous.get("status") == "deferred_retry"
                and current.get("status") in FINAL_STATUSES
                and previous.get("result_code") in FIELD_RETRY_CODES
                and previous.get("result_code") == current.get("result_code")
                and _field_signature(previous.get("fields")) == _field_signature(current.get("fields"))
            ):
                add(package, "RETRY_NO_PROGRESS", "high", [previous, current],
                    f"延迟重试后仍为 {current['result_code']}，关键检测字段没有变化")
        earlier_provisional = [item for item in history[:-1] if item.get("status") in {"skipped", "needs_review"}]
        if latest.get("status") == "success" and earlier_provisional:
            add(package, "PROVISIONAL_RESULT_REVERSED", "medium",
                [earlier_provisional[-1], latest], "早期暂不适配或待复检结论后来被回放成功覆盖")
        for item in history:
            if item.get("status") == "running":
                updated = _parse_time(item.get("updated_at"))
                if updated and now - updated.astimezone(now.tzinfo) > timedelta(minutes=15):
                    add(package, "REPORT_NO_HEARTBEAT", "high", [item],
                        "运行中报告超过 15 分钟没有新事件，需核对进程与断点")
            replay_events = [event for event in item.get("events") or []
                             if event.get("stage") == "replay_started"]
            if len(replay_events) >= 3:
                add(package, "MANY_REPLAY_ROUNDS", "medium", [item],
                    f"单次执行启动了 {len(replay_events)} 轮回放，建议检查候选 ID 和超时")
            failures = [event for event in item.get("events") or []
                        if event.get("stage") in {"asana_notes_written", "backend_submitted"}
                        and (event.get("data") or {}).get("status") == "failed"]
            if failures:
                add(package, "PERSISTENCE_STEP_FAILED", "high", [item],
                    "Asana 或后台写入失败，需核对最终任务状态")
            cleanup_failures = [event for event in item.get("events") or []
                                if event.get("stage") == "process_cleanup"
                                and (event.get("data") or {}).get("status") == "failed"]
            if cleanup_failures:
                add(package, "PROCESS_CLEANUP_FAILED", "high", [item],
                    "适配后未能确认目标应用进程退出，需检查设备与后续任务")

    findings.sort(key=lambda item: ({"high": 0, "medium": 1, "low": 2}[item["severity"]], item["package_name"], item["code"]))
    latest_counts = Counter(history[-1].get("status", "unknown") for history in by_package.values())
    return {
        "attempts": len(reports),
        "packages": len(by_package),
        "latest_local_statuses": dict(sorted(latest_counts.items())),
        "findings": findings,
        "scope_note": "仅基于本地自动执行报告；人工处理和 Asana 后续结论需另行核对",
    }


def render_text(result: dict[str, Any]) -> str:
    counts = "、".join(f"{key} {value}" for key, value in result["latest_local_statuses"].items())
    lines = [
        f"自动适配异常诊断：{result['packages']} 个包 / {result['attempts']} 次执行",
        f"最新本地执行状态：{counts or '无'}",
        result["scope_note"],
        f"异常线索：{len(result['findings'])} 条",
    ]
    for finding in result["findings"]:
        lines.append(
            f"[{finding['severity']}] {finding['package_name']} "
            f"{finding['code']}：{finding['detail']}"
        )
        for path in finding["report_paths"]:
            if path:
                lines.append(f"  证据：{path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分析近期自动适配执行报告中的异常线索")
    parser.add_argument("--reports", default=str(Path(__file__).with_name("automation_reports")))
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args(argv)
    result = analyze_reports(load_reports(args.reports, days=args.days))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
