import json
from types import SimpleNamespace

from automation_checkpoint import (
    AutomationCheckpointStore,
    new_batch_checkpoint,
    resumable_summary,
    validate_checkpoint,
)


def _task(package_name: str, gid: str = "1"):
    return SimpleNamespace(
        gid=gid,
        package_name=package_name,
        up2_appid="app-" + gid,
        gp_link="https://play.google.com/store/apps/details?id=" + package_name,
        notes="原描述",
        completed=False,
    )


def test_checkpoint_round_trip_is_atomic_and_resumable(tmp_path):
    path = tmp_path / "automation_checkpoint.json"
    store = AutomationCheckpointStore(str(path))
    checkpoint = new_batch_checkpoint(
        [("row-1", _task("com.example.one"))],
        replay_timeout_seconds=500,
    )
    checkpoint["stage"] = "fields_detected"
    checkpoint["current_fields"] = {"最终判断": "MAX聚合", "插屏聚合id": "abc"}

    saved = store.save(checkpoint)
    loaded = store.load()

    assert loaded == saved
    assert "com.example.one" in resumable_summary(loaded)
    assert "参数已识别" in resumable_summary(loaded)
    assert not list(tmp_path.glob(".automation-checkpoint-*.tmp"))


def test_checkpoint_marks_interrupted_without_losing_safe_stage(tmp_path):
    store = AutomationCheckpointStore(str(tmp_path / "checkpoint.json"))
    checkpoint = new_batch_checkpoint(
        [("row-1", _task("com.example.one"))],
        replay_timeout_seconds=300,
    )
    checkpoint["stage"] = "backend_verified"
    checkpoint["current_fields"] = {"最终判断": "MAX聚合"}

    interrupted = store.mark_interrupted(checkpoint, "窗口关闭")

    assert interrupted["status"] == "interrupted"
    assert interrupted["stage"] == "backend_verified"
    assert interrupted["last_error"] == "窗口关闭"
    assert "后台已生效" in resumable_summary(store.load())


def test_invalid_or_corrupt_checkpoint_is_not_resumed(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("{bad json", encoding="utf-8")
    store = AutomationCheckpointStore(str(path))
    assert store.load() is None

    invalid = {
        "schema_version": 1,
        "status": "active",
        "current_index": 2,
        "stage": "queued",
        "tasks": [{"package_name": "com.example.one"}],
    }
    path.write_text(json.dumps(invalid), encoding="utf-8")
    assert store.load() is None
    assert validate_checkpoint(invalid) is None


def test_store_clear_removes_completed_runtime_state(tmp_path):
    path = tmp_path / "checkpoint.json"
    store = AutomationCheckpointStore(str(path))
    store.save(
        new_batch_checkpoint(
            [("row-1", _task("com.example.one"))],
            replay_timeout_seconds=500,
        )
    )
    assert path.exists()
    store.clear()
    assert not path.exists()
    assert store.load() is None
