"""Rules for one delayed retry after recoverable automation failures."""

from __future__ import annotations

import time


DEFAULT_DEFERRED_RETRY_DELAY_SECONDS = 120
REPLAY_DEFERRED_RETRY_DELAY_SECONDS = 300
DEFAULT_DEFERRED_RETRY_LIMIT = 1

DEFERRED_RETRYABLE_CODES = frozenset(
    {
        "AD_IDS_EMPTY",
        "AF_KEY_EMPTY",
        "APP_LAUNCH_NOT_CONFIRMED",
        "APP_EXITED_DURING_AUTOMATION",
        "AD_REPLAY_FAILED",
        "REPLAY_TIMEOUT",
    }
)

REPLAY_DEFERRED_RETRY_CODES = frozenset(
    {
        "AD_REPLAY_FAILED",
        "REPLAY_TIMEOUT",
    }
)


def should_defer_automation_failure(
    code: str,
    attempt: int,
    *,
    max_retries: int = DEFAULT_DEFERRED_RETRY_LIMIT,
) -> bool:
    """Return whether this failure should be retried after the rest of the queue."""
    return (
        str(code or "").strip().upper() in DEFERRED_RETRYABLE_CODES
        and int(attempt or 0) < int(max_retries)
    )


def deferred_retry_due_at(
    *,
    delay_seconds: int = DEFAULT_DEFERRED_RETRY_DELAY_SECONDS,
    now: float | None = None,
) -> float:
    """Build a monotonic due timestamp for an interruptible queue wait."""
    base = time.monotonic() if now is None else float(now)
    return base + max(0, int(delay_seconds))


def deferred_retry_delay_seconds(code: str) -> int:
    """Return the business-specific wait before the single deferred retry."""
    normalized = str(code or "").strip().upper()
    if normalized in REPLAY_DEFERRED_RETRY_CODES:
        return REPLAY_DEFERRED_RETRY_DELAY_SECONDS
    return DEFAULT_DEFERRED_RETRY_DELAY_SECONDS
