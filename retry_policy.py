"""Conservative retry helpers for automation infrastructure operations."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import TypeVar


T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "timed out", "timeout", "超时", "device offline",
    "no devices/emulators found", "device not found", "transport error",
    "connection reset", "connection aborted", "connection refused",
    "remote end closed", "broken pipe", "server didn't ack",
    "adb server is out of date", "cannot connect to daemon",
    "temporarily unavailable", "temporary failure", "max retries exceeded",
)
_PERMANENT_MARKERS = (
    "unauthorized", "permission denied", "用户已停止自动化", "invalid_apk",
    "install_failed", "parse_error", "not installed", "未安装", "缺少包名",
    "缺少 up2", "配置文件", "json", "归因", "聚合类型", "广告id",
)


def is_transient_automation_error(error: BaseException | str) -> bool:
    """Return True only for errors that are reasonably safe to retry."""
    message = str(error).strip().casefold()
    if not message:
        return False
    if any(marker.casefold() in message for marker in _PERMANENT_MARKERS):
        return False
    return any(marker.casefold() in message for marker in _TRANSIENT_MARKERS)


def run_with_retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    delays: Iterable[float] = (1.0, 2.0),
    should_retry: Callable[[BaseException], bool] = is_transient_automation_error,
    on_retry: Callable[[int, int, BaseException, float], None] | None = None,
    stop_event=None,
) -> T:
    """Run an operation with bounded, stop-aware retries."""
    attempts = max(1, int(attempts))
    retry_delays = list(delays)
    for attempt in range(1, attempts + 1):
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("用户已停止自动化")
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts or not should_retry(exc):
                raise
            delay = (
                retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                if retry_delays else 0.0
            )
            if on_retry is not None:
                on_retry(attempt + 1, attempts, exc, delay)
            if delay > 0:
                if stop_event is not None:
                    if stop_event.wait(delay):
                        raise RuntimeError("用户已停止自动化") from exc
                else:
                    time.sleep(delay)
    raise AssertionError("unreachable")
