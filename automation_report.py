"""Structured, local per-package automation execution reports."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import re
import tempfile
import threading
import uuid
from app_version import REPORT_SCHEMA_VERSION


SCHEMA_VERSION = REPORT_SCHEMA_VERSION


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(item) for item in value]
    return str(value)


class AutomationReportStore:
    def __init__(self, directory: str, *, retention_days: int = 30):
        self.directory = os.path.abspath(directory)
        self.retention_days = max(1, int(retention_days))
        self._lock = threading.RLock()

    def _write(self, path: str, report: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".automation-report-", suffix=".tmp", dir=os.path.dirname(path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_safe(report), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    def begin_task(
        self,
        *,
        package_name: str,
        task_gid: str = "",
        appid: str = "",
        mode: str = "batch",
    ) -> str:
        with self._lock:
            now = datetime.now().astimezone()
            safe_package = re.sub(r"[^A-Za-z0-9._-]+", "_", package_name)[:120]
            report_id = uuid.uuid4().hex[:12]
            day_dir = os.path.join(self.directory, now.strftime("%Y-%m-%d"))
            path = os.path.join(
                day_dir,
                f"{now.strftime('%H%M%S')}_{safe_package}_{report_id}.json",
            )
            report = {
                "schema_version": SCHEMA_VERSION,
                "report_id": report_id,
                "package_name": package_name,
                "task_gid": task_gid,
                "appid": appid,
                "mode": mode,
                "status": "running",
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "finished_at": "",
                "result_code": "",
                "message": "",
                "fields": {},
                "events": [],
            }
            self._write(path, report)
            return path

    def add_event(self, path: str, stage: str, *, message: str = "", data=None) -> dict:
        with self._lock:
            report = self.load(path)
            if not report:
                return {}
            report["updated_at"] = _now_iso()
            report["events"].append(
                {
                    "at": report["updated_at"],
                    "stage": str(stage or ""),
                    "message": str(message or ""),
                    "data": _safe(data or {}),
                }
            )
            report["events"] = report["events"][-300:]
            if isinstance(data, dict) and isinstance(data.get("fields"), dict):
                report["fields"] = _safe(data["fields"])
            self._write(path, report)
            return deepcopy(report)

    def finish(
        self,
        path: str,
        *,
        status: str,
        result_code: str = "",
        message: str = "",
        data=None,
    ) -> dict:
        with self._lock:
            report = self.add_event(
                path,
                "finished",
                message=message,
                data=data,
            )
            if not report:
                return {}
            report["status"] = str(status or "failed")
            report["result_code"] = str(result_code or "")
            report["message"] = str(message or "")
            report["finished_at"] = _now_iso()
            report["updated_at"] = report["finished_at"]
            self._write(path, report)
            return deepcopy(report)

    @staticmethod
    def load(path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def cleanup(self) -> int:
        cutoff = datetime.now().astimezone() - timedelta(days=self.retention_days)
        removed = 0
        if not os.path.isdir(self.directory):
            return removed
        for root, _dirs, files in os.walk(self.directory, topdown=False):
            for filename in files:
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(root, filename)
                try:
                    modified = datetime.fromtimestamp(
                        os.path.getmtime(path), tz=cutoff.tzinfo
                    )
                    if modified < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
            try:
                if root != self.directory and not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass
        return removed
