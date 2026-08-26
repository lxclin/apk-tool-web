"""Deterministic Android ad replay detection shared by desktop and Web flows.

The detector deliberately separates ad loading from a real display.  A replay
only succeeds when a display/impression/revenue callback belongs to the
configured ad unit and to an ad type that is required for the current task.
"""

from __future__ import annotations

import re
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Pattern

from adb_pusher import (
    PackageRuntimeMonitor,
    build_open_app_cmd,
    force_stop_app,
    normalize_optional_parameter,
    parse_autodetector_fields,
    start_logcat_stream,
    stop_logcat_stream,
)


DEFAULT_REPLAY_TIMEOUT_SECONDS = 500
MIN_REPLAY_TIMEOUT_SECONDS = 10
MAX_REPLAY_TIMEOUT_SECONDS = 600
REPLAY_LOG_QUEUE_POLL_SECONDS = 0.25
REPLAY_IN_FLIGHT_GRACE_SECONDS = 30

INTERSTITIAL = "interstitial"
REWARDED = "rewarded"


def split_ad_unit_ids(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return normalized, de-duplicated ad unit IDs in stable order."""
    if value is None:
        return ()
    chunks = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for chunk in chunks:
        for item in re.split(r"[,，\s]+", str(chunk or "")):
            item = normalize_optional_parameter(item)
            if item and item not in result:
                result.append(item)
    return tuple(result)


def validate_replay_timeout(value: int | str | float) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("回放监听时间必须是整数") from exc
    if not MIN_REPLAY_TIMEOUT_SECONDS <= timeout <= MAX_REPLAY_TIMEOUT_SECONDS:
        raise ValueError(
            f"回放监听时间需为 {MIN_REPLAY_TIMEOUT_SECONDS}-"
            f"{MAX_REPLAY_TIMEOUT_SECONDS} 秒"
        )
    return timeout


@dataclass(frozen=True)
class ReplayExpectation:
    interstitial_ids: tuple[str, ...] = ()
    rewarded_ids: tuple[str, ...] = ()
    platform_log_token: str = ""

    @classmethod
    def from_values(
        cls,
        interstitial_ids: str | Iterable[str] | None,
        rewarded_ids: str | Iterable[str] | None,
        aggregation_verdict: str | None = None,
    ) -> "ReplayExpectation":
        return cls(
            interstitial_ids=split_ad_unit_ids(interstitial_ids),
            rewarded_ids=split_ad_unit_ids(rewarded_ids),
            platform_log_token=replay_platform_log_token(aggregation_verdict),
        )

    @property
    def required_types(self) -> tuple[str, ...]:
        required: list[str] = []
        if self.interstitial_ids:
            required.append(INTERSTITIAL)
        if self.rewarded_ids:
            required.append(REWARDED)
        return tuple(required)

    def ids_for(self, ad_type: str) -> tuple[str, ...]:
        return self.interstitial_ids if ad_type == INTERSTITIAL else self.rewarded_ids


def replay_platform_log_token(aggregation_verdict: str | None) -> str:
    """Map the detected mediation verdict to the operator's Logcat keyword."""
    verdict = normalize_optional_parameter(aggregation_verdict).casefold()
    if "levelplay" in verdict or "level_play" in verdict:
        return "level"
    if "ironsource" in verdict or "iron_source" in verdict:
        return "iron"
    if "admob" in verdict:
        return "admob"
    if "max" in verdict or "applovin" in verdict:
        return "max"
    if "tradplus" in verdict or "trad_plus" in verdict:
        return "tradplus"
    return ""


@dataclass
class AdTypeReplayState:
    required: bool
    expected_ids: tuple[str, ...]
    displayed: bool = False
    revenue_reported: bool = False
    evidence: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "required": self.required,
            "expected_ids": list(self.expected_ids),
            "displayed": self.displayed,
            "revenue_reported": self.revenue_reported,
            "evidence": list(self.evidence),
            "errors": list(self.errors),
        }


_SESSION_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"', re.I)
_STATUS_RE = re.compile(r'"status"\s*:\s*"([^"]+)"', re.I)
_JSON_AD_TYPE_RE = re.compile(r'"adType"\s*:\s*"([^"]+)"', re.I)
_TEXT_AD_TYPE_RE = re.compile(r"\badType\s*=\s*([A-Z_]+)", re.I)
_FORMAT_RE = re.compile(r"\bformat\s*=\s*([A-Z_]+)", re.I)
_AD_UNIT_PATTERNS = (
    re.compile(r'"adUnitId"\s*:\s*"([^"]*)"', re.I),
    re.compile(r"\badUnitId\s*=\s*['\"]?([^,'\"}\s)]+)", re.I),
)


def _ad_type_from_token(token: str) -> str | None:
    token = token.strip().lower()
    if token in {"interstitial", "inter", "inter_video"}:
        return INTERSTITIAL
    if token in {"reward", "rewarded", "reward_video", "rewarded_video"}:
        return REWARDED
    return None


def _extract_ad_type(line: str) -> str | None:
    for pattern in (_JSON_AD_TYPE_RE, _TEXT_AD_TYPE_RE, _FORMAT_RE):
        match = pattern.search(line)
        if match:
            ad_type = _ad_type_from_token(match.group(1))
            if ad_type:
                return ad_type
    lowered = line.lower()
    if "interstitial ad shown" in lowered:
        return INTERSTITIAL
    if "onuserearnedreward" in lowered or "reward confirmed" in lowered:
        return REWARDED
    return None


def _extract_ad_unit_id(line: str) -> str:
    for pattern in _AD_UNIT_PATTERNS:
        match = pattern.search(line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def _short_evidence(line: str, limit: int = 600) -> str:
    clean = line.strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


class AdReplayEvaluator:
    """Consume logcat lines and track replay completion for configured IDs."""

    _DISPLAY_MARKERS = (
        "onAdDisplayed",
        "onAdImpression",
        "onAdShown",
        "Interstitial ad shown",
        '"status":"display_success"',
        '"status": "display_success"',
        "onAdRevenuePaid",
    )
    _REWARD_CONFIRM_MARKERS = (
        "onUserEarnedReward",
        "Reward confirmed",
        "onAdRewarded",
    )
    _ERROR_RULES = (
        (re.compile(r"(?:error\s*508|errorCode[\"'=:\s]+508|init\(\) must be called before)", re.I), "Error 508：SDK 未正确初始化"),
        (re.compile(r"\bno[ _-]?fill\b", re.I), "No Fill：本次请求无广告填充"),
        (re.compile(r"\bnot[ _-]?ready\b", re.I), "Not Ready：广告尚未准备完成"),
        (re.compile(r'"status"\s*:\s*"(?:display_failed|load_failed)"', re.I), "广告加载或展示失败"),
    )

    def __init__(
        self,
        expectation: ReplayExpectation,
        action_success_patterns: Iterable[str | Pattern[str]] = (),
    ):
        if not expectation.required_types:
            raise ValueError("至少需要一个插屏或激励视频聚合 ID")
        self.expectation = expectation
        self.states = {
            INTERSTITIAL: AdTypeReplayState(
                required=bool(expectation.interstitial_ids),
                expected_ids=expectation.interstitial_ids,
            ),
            REWARDED: AdTypeReplayState(
                required=bool(expectation.rewarded_ids),
                expected_ids=expectation.rewarded_ids,
            ),
        }
        self._sessions: dict[str, tuple[str, str]] = {}
        self._active_attempts: set[tuple[str, str, str]] = set()
        self.action_success_patterns = tuple(
            re.compile(item, re.I) if isinstance(item, str) else item
            for item in action_success_patterns
        )
        self.action_success = False
        self.action_evidence = ""

    @property
    def aggregation_success(self) -> bool:
        return all(self.states[item].displayed for item in self.expectation.required_types)

    @property
    def complete(self) -> bool:
        return self.aggregation_success or self.action_success

    @property
    def has_in_flight_attempt(self) -> bool:
        """Whether a configured ad request has started but not yet terminated."""
        return bool(self._active_attempts)

    def feed(self, line: str) -> bool:
        """Consume one line and return True once either success route completes."""
        if not line:
            return self.complete

        for pattern in self.action_success_patterns:
            if pattern.search(line):
                self.action_success = True
                self.action_evidence = _short_evidence(line)
                return True

        # AutoMediaDetect/mDetector replay old SDK lines and are inference only.
        # They remain useful for errors, but must never create a display success.
        inference_only = "ZGSDK.AutoMediaDetect" in line or "ZGSDK.mDetector" in line

        ad_type = _extract_ad_type(line)
        ad_unit_id = _extract_ad_unit_id(line)
        session_match = _SESSION_RE.search(line)
        session_id = session_match.group(1) if session_match else ""
        status_match = _STATUS_RE.search(line)
        status = status_match.group(1).strip().casefold() if status_match else ""

        if session_id and ad_type and ad_unit_id:
            self._sessions[session_id] = (ad_type, ad_unit_id)
        elif session_id and session_id in self._sessions:
            session_type, session_ad_unit = self._sessions[session_id]
            ad_type = ad_type or session_type
            ad_unit_id = ad_unit_id or session_ad_unit

        if (
            ad_type in self.expectation.required_types
            and ad_unit_id in self.expectation.ids_for(ad_type)
        ):
            attempt = (session_id, ad_type, ad_unit_id)
            if status in {"load_start", "display_start"}:
                # A later status for the same session replaces its previous
                # phase without creating a second in-flight attempt.
                self._active_attempts = {
                    item for item in self._active_attempts
                    if not (session_id and item[0] == session_id)
                }
                self._active_attempts.add(attempt)
            elif status in {
                "load_failed", "display_failed", "display_success", "clicked"
            }:
                self._active_attempts = {
                    item for item in self._active_attempts
                    if not (
                        (session_id and item[0] == session_id)
                        or (not session_id and item[1:] == attempt[1:])
                    )
                }

        for pattern, message in self._ERROR_RULES:
            if pattern.search(line):
                targets = [ad_type] if ad_type else list(self.expectation.required_types)
                for target in targets:
                    if target and message not in self.states[target].errors:
                        self.states[target].errors.append(message)

        if inference_only or not ad_type or ad_type not in self.expectation.required_types:
            return self.complete
        if not ad_unit_id or ad_unit_id not in self.expectation.ids_for(ad_type):
            return self.complete

        is_display = any(marker.lower() in line.lower() for marker in self._DISPLAY_MARKERS)
        if ad_type == REWARDED:
            is_display = is_display or any(
                marker.lower() in line.lower() for marker in self._REWARD_CONFIRM_MARKERS
            )
        if is_display:
            state = self.states[ad_type]
            state.displayed = True
            evidence = _short_evidence(line)
            if evidence not in state.evidence:
                state.evidence.append(evidence)
            if "revenuepaid" in line.lower():
                state.revenue_reported = True
        return self.complete

    def result(self, *, timed_out: bool = False, elapsed_seconds: float = 0.0) -> dict:
        if self.action_success:
            code = "ACTION_REPLAY_SUCCESS"
            message = "检测到动作适配广告播放成功"
        elif self.aggregation_success:
            code = "AGGREGATION_REPLAY_SUCCESS"
            message = "已配置的聚合广告均回放成功"
        elif timed_out:
            code = "REPLAY_TIMEOUT"
            missing = [
                "插屏" if item == INTERSTITIAL else "激励视频"
                for item in self.expectation.required_types
                if not self.states[item].displayed
            ]
            message = "回放监听超时，未确认" + "、".join(missing) + "广告展示"
        else:
            code = "REPLAY_PENDING"
            message = "正在等待广告展示"
        return {
            "ok": self.complete,
            "code": code,
            "message": message,
            "elapsed_seconds": round(elapsed_seconds, 1),
            "interstitial": self.states[INTERSTITIAL].as_dict(),
            "rewarded": self.states[REWARDED].as_dict(),
            "action_success": self.action_success,
            "action_evidence": self.action_evidence,
        }


def is_replay_diagnostic_line(
    line: str,
    expectation: ReplayExpectation | None = None,
) -> bool:
    """Return whether one raw UID line is useful to a human replay diagnosis.

    The evaluator still consumes every line.  This predicate only controls
    what is forwarded to the GUI, preventing verbose mediation SDK startup
    logs from flooding Tk/WebSocket output and delaying success detection.
    """
    lowered = str(line or "").casefold()
    if not lowered:
        return False
    zgsdk_authoritative = (
        "zgsdk.mediationevent" in lowered
        or "zgsdk.scheduledads" in lowered
        or ("zgsdk.max:" in lowered and ("超时" in lowered or "timed out" in lowered))
    )
    callback_markers = (
        "onaddisplayed",
        "onadimpression",
        "onadshown",
        "interstitial ad shown",
        "display_success",
        "onadrevenuepaid",
        "onuserearnedreward",
        "reward confirmed",
        "onadrewarded",
    )
    severe_markers = (
        "error 508",
        "init() must be called before",
    )
    if expectation is not None and expectation.platform_log_token:
        # Mirror Android Studio's common AND query, for example "ZGSDK max".
        # Fatal initialization errors are retained even when the SDK itself
        # did not tag them with the expected platform keyword.
        if any(marker in lowered for marker in severe_markers):
            return True
        return (
            "zgsdk" in lowered
            and expectation.platform_log_token in lowered
        )
    if not (
        zgsdk_authoritative
        or any(marker in lowered for marker in callback_markers)
        or any(marker in lowered for marker in severe_markers)
    ):
        return False
    if expectation is None:
        return True
    expected_ids = expectation.interstitial_ids + expectation.rewarded_ids
    # Only fatal SDK errors are useful without an ID. Adapter-level No Fill
    # lines remain available to the evaluator but are deliberately hidden;
    # the authoritative ZGSDK load_failed event summarizes the request once.
    if any(marker in lowered for marker in severe_markers):
        return True
    return (
        zgsdk_authoritative and not _extract_ad_unit_id(line)
    ) or any(ad_id.casefold() in lowered for ad_id in expected_ids)


def _start_logcat_reader(proc: subprocess.Popen, output: queue.Queue) -> threading.Thread:
    """Continuously drain stdout on a dedicated thread to prevent pipe backlog."""
    def _read():
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    output.put(line)
        finally:
            output.put(None)

    thread = threading.Thread(target=_read, name="ad-replay-logcat-reader", daemon=True)
    thread.start()
    return thread


def build_replay_failure_comment(package_name: str, result: dict) -> str:
    """Build the terminal Asana comment requested by the automation flow."""
    lines = [
        "【APK Tool 自动化适配：AD_REPLAY_FAILED】",
        "广告回放监听超时，自动化适配失败，需要测试人员确认",
        f"包名：{package_name}",
        f"监听时间：{result.get('elapsed_seconds', 0)} 秒",
    ]
    runtime = result.get("runtime") or {}
    if result.get("code") in {
        "APP_CRASHED",
        "APP_EXITED_DURING_AUTOMATION",
        "APP_LAUNCH_NOT_CONFIRMED",
    }:
        lines[1] = result.get("message") or "应用在广告回放过程中异常退出"
        summary = str(runtime.get("summary") or "").strip()
        if summary:
            lines.extend(["关键崩溃日志：", summary])
    for label, key in (("插屏广告", "interstitial"), ("激励视频", "rewarded")):
        state = result.get(key) or {}
        if not state.get("required"):
            lines.append(f"{label}：未配置，不要求验证")
        elif state.get("displayed"):
            lines.append(f"{label}：回放成功")
        else:
            lines.append(f"{label}：未检测到真实展示")
            for error in state.get("errors") or []:
                lines.append(f"- {error}")
    return "\n".join(lines)


def run_ad_replay_check(
    package_name: str,
    uid: str,
    expectation: ReplayExpectation,
    timeout_seconds: int = DEFAULT_REPLAY_TIMEOUT_SECONDS,
    *,
    action_success_patterns: Iterable[str | Pattern[str]] = (),
    stop_event: threading.Event | None = None,
    on_line: Callable[[str], None] | None = None,
    on_progress: Callable[[str], None] | None = None,
    dismiss_interrupting_dialog: Callable[[], dict] | None = None,
    aggregation_change_detector: Callable[[dict], bool] | None = None,
    aggregation_change_grace_seconds: int = 90,
) -> dict:
    """Force-stop/relaunch one app and monitor only fresh UID logcat output."""
    package_name = package_name.strip()
    uid = uid.strip()
    timeout_seconds = validate_replay_timeout(timeout_seconds)
    if not package_name:
        raise ValueError("请输入包名")
    if not uid:
        raise ValueError("请输入应用 UID")

    evaluator = AdReplayEvaluator(expectation, action_success_patterns)
    runtime_monitor = PackageRuntimeMonitor(
        package_name,
        auto_recover_anr=True,
        on_event=on_progress,
    )
    stop_event = stop_event or threading.Event()
    ok, message = force_stop_app(package_name)
    if not ok:
        return {"ok": False, "code": "FORCE_STOP_FAILED", "message": message}

    if on_progress:
        on_progress("已强制停止应用，开始监听新日志")
    proc = start_logcat_stream("", uid)
    started = time.monotonic()
    try:
        launch = subprocess.run(
            build_open_app_cmd(package_name),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if launch.returncode != 0:
            detail = ((launch.stdout or "") + (launch.stderr or "")).strip()
            return {
                "ok": False,
                "code": "LAUNCH_FAILED",
                "message": detail or "应用启动失败",
            }
        # force-stop and the launch command must not consume the process
        # monitor's startup allowance or the replay timeout.
        runtime_monitor.reset()
        started = time.monotonic()
        if on_progress:
            on_progress(f"应用已启动，最长监听 {timeout_seconds} 秒")
            if expectation.platform_log_token:
                on_progress(
                    "回放日志过滤：ZGSDK " + expectation.platform_log_token
                )

        if proc.stdout is None:
            return {"ok": False, "code": "LOGCAT_FAILED", "message": "无法读取 logcat"}
        line_queue: queue.Queue = queue.Queue()
        _start_logcat_reader(proc, line_queue)
        last_runtime_check = 0.0
        last_dialog_check = -3.0
        dialog_dismissed = False
        reader_finished = False
        grace_announced = False
        autodetector_lines: list[str] = []
        deferred_success: dict | None = None
        aggregation_change_grace_seconds = max(
            0,
            min(int(aggregation_change_grace_seconds), timeout_seconds),
        )
        if aggregation_change_detector is not None and on_progress:
            on_progress(
                "临时聚合配置回放中，同时监听 AutoDetector 明确聚合类型"
            )
        while not stop_event.is_set():
            elapsed = time.monotonic() - started
            if (
                dismiss_interrupting_dialog is not None
                and not dialog_dismissed
                and elapsed - last_dialog_check >= 3.0
            ):
                last_dialog_check = elapsed
                try:
                    dialog_result = dict(dismiss_interrupting_dialog() or {})
                except Exception:
                    dialog_result = {}
                if dialog_result.get("dismissed"):
                    dialog_dismissed = True
                    if on_progress:
                        on_progress(
                            dialog_result.get("message")
                            or "已自动关闭通知权限弹窗"
                        )
            if elapsed - last_runtime_check >= 1.0:
                runtime = runtime_monitor.poll()
                last_runtime_check = elapsed
                if not runtime.get("ok", True):
                    result = evaluator.result(elapsed_seconds=elapsed)
                    result.update(
                        ok=False,
                        code=runtime.get("code", "APP_EXITED_DURING_AUTOMATION"),
                        message=runtime.get("message", "应用在广告回放过程中异常退出"),
                        runtime=runtime,
                    )
                    return result

            monitor_deadline = timeout_seconds
            if evaluator.has_in_flight_attempt:
                monitor_deadline += REPLAY_IN_FLIGHT_GRACE_SECONDS
            wait = min(
                REPLAY_LOG_QUEUE_POLL_SECONDS,
                max(0.0, monitor_deadline - elapsed),
            )
            try:
                first_line = line_queue.get(timeout=wait) if wait > 0 else line_queue.get_nowait()
                pending = [first_line]
                # Drain every line already captured by the reader before doing
                # UI work. This is the key protection against high-volume SDK
                # logs delaying a display callback by minutes.
                while True:
                    try:
                        pending.append(line_queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pending = []

            aggregation_snapshot_dirty = False
            for line in pending:
                if line is None:
                    reader_finished = True
                    continue
                clean = line.rstrip()
                if "ZGSDK.AutoDetector" in clean:
                    autodetector_lines.append(clean)
                    if len(autodetector_lines) > 1000:
                        autodetector_lines = autodetector_lines[-1000:]
                    aggregation_snapshot_dirty = True
                if on_line and is_replay_diagnostic_line(clean, expectation):
                    on_line(clean)
                if evaluator.feed(line):
                    completed = evaluator.result(
                        elapsed_seconds=time.monotonic() - started
                    )
                    if aggregation_change_detector is None:
                        return completed
                    deferred_success = completed

            if aggregation_snapshot_dirty and aggregation_change_detector is not None:
                detected_fields = parse_autodetector_fields(autodetector_lines)
                try:
                    aggregation_changed = bool(
                        aggregation_change_detector(detected_fields)
                    )
                except Exception:
                    aggregation_changed = False
                if aggregation_changed:
                    result = evaluator.result(
                        elapsed_seconds=time.monotonic() - started
                    )
                    result.update(
                        ok=False,
                        code="AGGREGATION_TYPE_CHANGED_DURING_REPLAY",
                        message=(
                            "回放期间 AutoDetector 检测到更高优先级的明确聚合类型"
                        ),
                        detected_fields=detected_fields,
                    )
                    return result

            elapsed = time.monotonic() - started
            if (
                deferred_success is not None
                and elapsed >= aggregation_change_grace_seconds
            ):
                return deferred_success
            if elapsed >= timeout_seconds:
                if (
                    evaluator.has_in_flight_attempt
                    and elapsed < timeout_seconds + REPLAY_IN_FLIGHT_GRACE_SECONDS
                ):
                    if on_progress and not grace_announced:
                        on_progress(
                            "监听时间已到，目标广告请求仍在执行，"
                            f"宽限最多 {REPLAY_IN_FLIGHT_GRACE_SECONDS} 秒"
                        )
                        grace_announced = True
                    continue
                # The reader drains independently and the queue was emptied
                # above, so a timeout now reflects processed current logs, not
                # stale pipe backlog.
                return evaluator.result(timed_out=True, elapsed_seconds=elapsed)
            if reader_finished and proc.poll() is not None:
                result = evaluator.result(elapsed_seconds=elapsed)
                result.update(
                    ok=False,
                    code="LOGCAT_ENDED",
                    message="Logcat 监听进程已提前结束",
                )
                return result
        result = evaluator.result(elapsed_seconds=time.monotonic() - started)
        result.update(ok=False, code="REPLAY_CANCELLED", message="广告回放监听已停止")
        return result
    finally:
        stop_logcat_stream(proc)
