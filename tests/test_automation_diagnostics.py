from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from automation_diagnostics import (
    analyze_reports,
    field_snapshot,
    render_text,
    replay_snapshot,
)


def _report(package, status, code, minute, *, fields=None, events=None):
    started = datetime(2026, 9, 29, 10, minute, tzinfo=timezone(timedelta(hours=8)))
    return {
        "package_name": package,
        "report_id": str(minute),
        "status": status,
        "result_code": code,
        "started_at": started.isoformat(),
        "updated_at": started.isoformat(),
        "fields": fields or {},
        "events": events or [],
        "_report_path": f"/tmp/{package}-{minute}.json",
    }


def test_field_and_replay_snapshots_omit_keys_ids_and_logs():
    fields = {
        "最终判断": "MAX聚合",
        "归因平台": "AppsFlyer, Adjust",
        "激励视频聚合id": "secret-ad-1, secret-ad-2",
        "插屏聚合id": "secret-interstitial",
        "af_key": "secret-af-key",
        "完整日志": "secret-logcat",
    }
    state = field_snapshot(fields)
    replay = replay_snapshot({
        "code": "REPLAY_TIMEOUT",
        "interstitial": {"required": True, "request_observed": True,
                         "displayed": False, "errors": ["No Fill"]},
    }, round_number=2)

    assert state["af_key_present"] is True
    assert state["rewarded_count"] == 2
    assert replay["interstitial"]["request_observed"] is True
    assert replay["interstitial"]["error_count"] == 1
    assert "secret" not in str(state) + str(replay)


def test_diagnostics_find_unchanged_af_retry_and_reversed_provisional_result():
    af_fields = {
        "最终判断": "MAX聚合", "归因平台": "AppsFlyer, Adjust",
        "af_key": "", "激励视频聚合id": "secret-id",
    }
    reports = [
        _report("com.af", "deferred_retry", "AF_KEY_EMPTY", 1, fields=af_fields),
        _report("com.af", "failed", "AF_KEY_EMPTY", 2, fields=af_fields),
        _report("com.provisional", "needs_review", "SUSPECTED_WHITE_PACKAGE_REVIEW", 3),
        _report("com.provisional", "success", "AGGREGATION_REPLAY_SUCCESS", 4),
    ]
    result = analyze_reports(reports, now=datetime(2026, 9, 29, 12, tzinfo=timezone(timedelta(hours=8))))

    assert result["attempts"] == 4
    assert result["packages"] == 2
    assert result["latest_local_statuses"] == {"failed": 1, "success": 1}
    assert {(item["package_name"], item["code"]) for item in result["findings"]} == {
        ("com.af", "RETRY_NO_PROGRESS"),
        ("com.provisional", "PROVISIONAL_RESULT_REVERSED"),
    }
    assert "secret-id" not in render_text(result)


def test_replay_failure_with_unchanged_fields_is_not_mislabeled_detection_retry():
    fields = {"最终判断": "MAX聚合", "激励视频聚合id": "one"}
    reports = [
        _report("com.replay", "deferred_retry", "AD_REPLAY_FAILED", 1, fields=fields),
        _report("com.replay", "failed", "AD_REPLAY_FAILED", 2, fields=fields),
    ]
    assert analyze_reports(reports)["findings"] == []


def test_running_report_without_heartbeat_and_many_replay_rounds():
    now = datetime(2026, 9, 29, 12, tzinfo=timezone(timedelta(hours=8)))
    report = _report(
        "com.slow", "running", "", 1,
        events=[{"stage": "replay_started"}] * 3,
    )
    result = analyze_reports([report], now=now)
    assert {item["code"] for item in result["findings"]} == {
        "REPORT_NO_HEARTBEAT", "MANY_REPLAY_ROUNDS",
    }


def test_gui_report_event_accepts_retry_details_without_tk_window():
    from gui import APKToolApp

    app = object.__new__(APKToolApp)
    app._automation_report_path = "/tmp/current.json"
    app._automation_fields = {"af_key": "secret-key", "激励视频聚合id": "secret-id"}
    app._automation_report_store = MagicMock()

    app._automation_report_event(
        "retry", "稍后重试", status="retrying",
        details={"operation": "backend_submit", "attempt": 2},
    )

    args, kwargs = app._automation_report_store.add_event.call_args
    assert args == ("/tmp/current.json", "retry")
    assert kwargs["data"]["status"] == "retrying"
    assert kwargs["data"]["details"]["attempt"] == 2
    assert "fields" not in kwargs["data"]
    assert "secret" not in str(kwargs["data"])


def test_gui_diagnostics_button_reads_local_reports_only():
    from gui import APKToolApp

    app = object.__new__(APKToolApp)
    app._automation_log = MagicMock()
    with patch("gui.load_reports", return_value=[]):
        app._automation_show_recent_diagnostics()

    message = app._automation_log.call_args.args[0]
    assert "自动适配异常诊断" in message
    assert "仅基于本地自动执行报告" in message
