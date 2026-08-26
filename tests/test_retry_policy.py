import threading

import pytest

from retry_policy import is_transient_automation_error, run_with_retry


@pytest.mark.parametrize("message", [
    "命令超时：adb shell", "error: device offline",
    "adb: no devices/emulators found", "Connection reset by peer",
    "HTTPSConnectionPool: Max retries exceeded",
])
def test_transient_errors_are_retryable(message):
    assert is_transient_automation_error(message)


@pytest.mark.parametrize("message", [
    "device unauthorized", "Failure [INSTALL_FAILED_INVALID_APK]",
    "当前任务缺少包名或 UP2 appid", "用户已停止自动化",
])
def test_permanent_errors_are_not_retryable(message):
    assert not is_transient_automation_error(message)


def test_run_with_retry_recovers_and_reports_attempt():
    calls, retries = [], []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("device offline")
        return "ok"

    result = run_with_retry(
        operation, attempts=3, delays=(0, 0),
        on_retry=lambda *args: retries.append(args),
    )
    assert result == "ok"
    assert len(calls) == 3
    assert [item[0] for item in retries] == [2, 3]


def test_run_with_retry_honors_stop_event():
    stop_event = threading.Event()
    stop_event.set()
    with pytest.raises(RuntimeError, match="用户已停止"):
        run_with_retry(lambda: "never", stop_event=stop_event)
