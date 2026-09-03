from automation_deferred_retry import (
    DEFERRED_RETRYABLE_CODES,
    deferred_retry_delay_seconds,
    deferred_retry_due_at,
    should_defer_automation_failure,
)


def test_retryable_failure_is_deferred_only_once():
    for code in DEFERRED_RETRYABLE_CODES:
        assert should_defer_automation_failure(code, 0) is True
        assert should_defer_automation_failure(code, 1) is False


def test_hard_business_failure_is_not_deferred():
    assert should_defer_automation_failure("AGGREGATION_TYPE_EMPTY", 0) is False
    assert should_defer_automation_failure("APP_CRASHED", 0) is False
    assert should_defer_automation_failure("BACKEND_SUBMIT_FAILED", 0) is False


def test_due_at_uses_monotonic_base_and_non_negative_delay():
    assert deferred_retry_due_at(delay_seconds=600, now=100.0) == 700.0
    assert deferred_retry_due_at(delay_seconds=-1, now=100.0) == 100.0


def test_retry_delay_is_shorter_for_parameters_than_replay():
    assert deferred_retry_delay_seconds("AF_KEY_EMPTY") == 120
    assert deferred_retry_delay_seconds("AD_IDS_EMPTY") == 120
    assert deferred_retry_delay_seconds("APP_LAUNCH_NOT_CONFIRMED") == 120
    assert deferred_retry_delay_seconds("AD_REPLAY_FAILED") == 300
    assert deferred_retry_delay_seconds("REPLAY_TIMEOUT") == 300
