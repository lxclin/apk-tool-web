from datetime import datetime, timedelta
import os

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
