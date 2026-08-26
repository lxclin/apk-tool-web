"""Durable checkpoints for long-running automation batches.

The GUI process, ADB server or macOS session can stop while a package is in
progress.  This module keeps only JSON-serializable business state and writes
it atomically, so the next launch can resume from a known safe boundary.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import tempfile
import threading
from typing import Any, Iterable
from app_version import CHECKPOINT_SCHEMA_VERSION


SCHEMA_VERSION = CHECKPOINT_SCHEMA_VERSION
ACTIVE_STATUSES = frozenset({"active", "interrupted"})
SAFE_RESUME_STAGES = frozenset(
    {
        "queued",
        "preparing",
        "detecting",
        "fields_detected",
        "backend_verified",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    """Return a defensive JSON-safe copy of nested automation data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)


def task_record(item_id: str, task: Any) -> dict:
    """Serialize the Asana/precheck fields required to restore one task."""
    return {
        "item_id": str(item_id or ""),
        "gid": str(getattr(task, "gid", "") or ""),
        "package_name": str(getattr(task, "package_name", "") or ""),
        "up2_appid": str(getattr(task, "up2_appid", "") or ""),
        "gp_link": str(getattr(task, "gp_link", "") or ""),
        "notes": str(getattr(task, "notes", "") or ""),
        "completed": bool(getattr(task, "completed", False)),
        "result": "pending",
        "message": "",
    }


def new_batch_checkpoint(
    queue: Iterable[tuple[str, Any]],
    *,
    replay_timeout_seconds: int,
) -> dict:
    tasks = [task_record(item_id, task) for item_id, task in queue]
    now = _now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "active",
        "mode": "batch",
        "created_at": now,
        "updated_at": now,
        "current_index": 0,
        "stage": "queued",
        "replay_timeout_seconds": int(replay_timeout_seconds),
        "tasks": tasks,
        "current_fields": {},
        "replay_id_candidates": {},
        "last_error": "",
    }


def validate_checkpoint(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return None
    current_index = data.get("current_index")
    if not isinstance(current_index, int) or current_index < 0:
        return None
    if current_index >= len(tasks) and data.get("status") in ACTIVE_STATUSES:
        return None
    stage = str(data.get("stage") or "queued")
    if stage not in SAFE_RESUME_STAGES and data.get("status") in ACTIVE_STATUSES:
        return None
    for task in tasks:
        if not isinstance(task, dict) or not str(task.get("package_name") or "").strip():
            return None
    return deepcopy(data)


def resumable_summary(data: dict | None) -> str:
    checkpoint = validate_checkpoint(data)
    if not checkpoint or checkpoint.get("status") not in ACTIVE_STATUSES:
        return ""
    index = checkpoint["current_index"]
    task = checkpoint["tasks"][index]
    remaining = len(checkpoint["tasks"]) - index
    stage_labels = {
        "queued": "等待开始",
        "preparing": "ADB 前置",
        "detecting": "聚合检测",
        "fields_detected": "参数已识别",
        "backend_verified": "后台已生效，等待回放",
    }
    return (
        f"上次队列停在 {task['package_name']} · "
        f"{stage_labels.get(checkpoint.get('stage'), checkpoint.get('stage'))} · "
        f"剩余 {remaining} 个"
    )


class AutomationCheckpointStore:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()

    def load(self) -> dict | None:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    return validate_checkpoint(json.load(handle))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                return None

    def save(self, data: dict) -> dict:
        with self._lock:
            checkpoint = _json_safe(data)
            checkpoint["schema_version"] = SCHEMA_VERSION
            checkpoint["updated_at"] = _now_iso()
            directory = os.path.dirname(self.path)
            os.makedirs(directory, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=".automation-checkpoint-", suffix=".tmp", dir=directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(checkpoint, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
            finally:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
            return checkpoint

    def mark_interrupted(self, data: dict, error: str = "") -> dict:
        checkpoint = deepcopy(data)
        if checkpoint.get("status") == "active":
            checkpoint["status"] = "interrupted"
        if error:
            checkpoint["last_error"] = str(error)
        return self.save(checkpoint)

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
