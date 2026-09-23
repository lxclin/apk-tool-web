from datetime import datetime, timedelta
import os
from unittest.mock import patch

from automation_report import AutomationReportStore


def test_report_tracks_stages_fields_and_result(tmp_path):
    store = AutomationReportStore(str(tmp_path))
    path = store.begin_task(
        package_name="com.example.game",
        task_gid="task-1",
        appid="app-1",
        mode="batch",
    )
    store.add_event(
        path,
        "fields_detected",
        data={"fields": {"最终判断": "MAX聚合"}},
    )
    final = store.finish(
        path,
        status="success",
        result_code="AGGREGATION_REPLAY_SUCCESS",
        message="回放成功",
    )

    assert final["status"] == "success"
    assert final["fields"]["最终判断"] == "MAX聚合"
    assert [event["stage"] for event in final["events"]] == [
        "fields_detected",
        "finished",
    ]


def test_cleanup_removes_expired_reports(tmp_path):
    store = AutomationReportStore(str(tmp_path), retention_days=2)
    path = store.begin_task(package_name="com.old.game")
    old = (datetime.now() - timedelta(days=10)).timestamp()
    os.utime(path, (old, old))

    assert store.cleanup() == 1
    assert not os.path.exists(path)


def test_reconcile_marks_only_reports_from_exited_process(tmp_path):
    store = AutomationReportStore(str(tmp_path))
    dead = store.begin_task(package_name="com.dead.game")
    active = store.begin_task(package_name="com.active.game")
    completed = store.begin_task(package_name="com.done.game")
    store.finish(completed, status="success", result_code="OK")
    report = store.load(dead)
    report["owner_pid"] = 999999
    store._write(dead, report)

    with patch.object(store, "_owner_is_alive", side_effect=lambda pid: pid == os.getpid()):
        assert store.reconcile_interrupted() == 1

    assert store.load(dead)["status"] == "interrupted"
    assert store.load(dead)["result_code"] == "AUTOMATION_INTERRUPTED"
    assert store.load(active)["status"] == "running"
    assert store.load(completed)["status"] == "success"


def test_reconcile_legacy_report_waits_until_next_day(tmp_path):
    store = AutomationReportStore(str(tmp_path))
    path = store.begin_task(package_name="com.legacy.game")
    report = store.load(path)
    report.pop("owner_pid")
    store._write(path, report)

    assert store.reconcile_interrupted() == 0
    report["updated_at"] = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
    store._write(path, report)
    assert store.reconcile_interrupted() == 1
    assert store.load(path)["status"] == "interrupted"
