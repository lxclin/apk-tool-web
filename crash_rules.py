"""Rule-based Android crash classification.

The runtime monitor should not treat every missing process as a crash.  This
module turns the historical log patterns we care about into explicit,
versioned classifications while keeping the result small and JSON-friendly
for the desktop, Web, and Asana workflows.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


DEFAULT_RULE_FILE = Path(__file__).with_name("crash_rules.json")
_EVENT_MARKER_RE = re.compile(
    r"FATAL EXCEPTION|am_crash|Fatal signal|am_anr|Application Not Responding|"
    r"Native crash|Segmentation fault|SIGSEGV|SIGABRT|"
    r"wm_task_removed|wm_destroy_activity|finish-imm:remove-task|"
    r"Failure starting process|currently frozen|has died: (?:cch|cached|empty)",
    re.IGNORECASE,
)


def load_crash_rules(path: str | Path | None = None) -> list[dict]:
    """Load the ordered rule definitions used by :func:`classify_crash_log`.

    A missing or malformed local rule file is deliberately non-fatal: the
    built-in fallback rules keep launch checks useful in packaged builds where
    an external data file may not have been copied.
    """
    rule_path = Path(path) if path else DEFAULT_RULE_FILE
    try:
        data = json.loads(rule_path.read_text(encoding="utf-8"))
        rules = data.get("rules", data) if isinstance(data, dict) else data
        if not isinstance(rules, list):
            raise ValueError("rules must be an array")
        normalized = [rule for rule in rules if isinstance(rule, dict)]
        if normalized:
            return normalized
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return list(_FALLBACK_RULES)


_FALLBACK_RULES = [
    {
        "id": "JAVA_MISSING_CLASS",
        "kind": "crash",
        "priority": 100,
        "crash_type": "JAVA_CRASH",
        "reason_code": "MISSING_RUNTIME_CLASS",
        "reason": "Java 运行时缺少类或依赖不完整",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": ["ClassNotFoundException", "NoClassDefFoundError"],
        "extract": "missing_class",
        "confidence": 0.99,
    },
    {
        "id": "JAVA_UNSATISFIED_LINK",
        "kind": "crash",
        "priority": 95,
        "crash_type": "JAVA_CRASH",
        "reason_code": "MISSING_NATIVE_LIBRARY",
        "reason": "Native 库缺失或 ABI 不匹配",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": ["UnsatisfiedLinkError", "dlopen failed"],
        "confidence": 0.98,
    },
    {
        "id": "JAVA_THREAD_RESOURCE",
        "kind": "crash",
        "priority": 93,
        "crash_type": "JAVA_CRASH",
        "reason_code": "THREAD_RESOURCE_EXHAUSTED",
        "reason": "线程资源不足，无法创建新线程",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": [
            "pthread_create",
            "unable to create new native thread",
            "could not create thread",
            "failed to create thread"
        ],
        "extract": "thread_resource",
        "confidence": 0.98,
    },
    {
        "id": "JAVA_OOM",
        "kind": "crash",
        "priority": 90,
        "crash_type": "JAVA_CRASH",
        "reason_code": "OUT_OF_MEMORY",
        "reason": "应用内存不足",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": ["OutOfMemoryError"],
        "confidence": 0.96,
    },
    {
        "id": "NATIVE_SIGNAL",
        "kind": "crash",
        "priority": 85,
        "crash_type": "NATIVE_CRASH",
        "reason_code": "NATIVE_SIGNAL",
        "reason": "Native 层信号或 abort 崩溃",
        "requires_any_of": [
            "am_crash",
            "Fatal signal",
            "backtrace:",
            "SIGSEGV",
            "SIGABRT",
        ],
        "patterns": [
            "Fatal signal",
            "backtrace:",
            "Native crash",
            "Segmentation fault",
            "Aborted",
            "SIGSEGV",
            "SIGABRT",
            "signal 11",
            "signal 6",
        ],
        "confidence": 0.98,
    },
    {
        "id": "JAVA_BINARY_INCOMPATIBILITY",
        "kind": "crash",
        "priority": 88,
        "crash_type": "JAVA_CRASH",
        "reason_code": "JAVA_API_BINARY_INCOMPATIBILITY",
        "reason": "Java/SDK API 二进制不兼容，调用的方法或字段不存在",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": [
            "NoSuchMethodError",
            "NoSuchFieldError",
            "AbstractMethodError",
            "IncompatibleClassChangeError",
        ],
        "confidence": 0.97,
    },
    {
        "id": "JAVA_SECURITY_EXCEPTION",
        "kind": "crash",
        "priority": 80,
        "crash_type": "JAVA_CRASH",
        "reason_code": "SECURITY_EXCEPTION",
        "reason": "权限或系统安全策略异常",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": ["SecurityException"],
        "confidence": 0.92,
    },
    {
        "id": "ANR",
        "kind": "anr",
        "priority": 70,
        "crash_type": "ANR",
        "reason_code": "ANR",
        "reason": "应用无响应",
        "patterns": [
            "am_anr",
            "Application Not Responding",
            "input dispatching timed out",
            "not responding",
            "ANR",
        ],
        "confidence": 0.95,
    },
    {
        "id": "APP_START_BLOCKED",
        "kind": "lifecycle",
        "priority": 65,
        "crash_type": "",
        "reason_code": "APP_START_BLOCKED",
        "reason": "应用启动被系统阻止，不属于包体闪退",
        "patterns": [
            "Failure starting process",
            "currently frozen",
            "start failure",
            "Force stopping",
        ],
        "confidence": 0.94,
    },
    {
        "id": "TASK_REMOVED",
        "kind": "lifecycle",
        "priority": 60,
        "crash_type": "",
        "reason_code": "TASK_REMOVED",
        "reason": "应用任务被移除，不属于闪退",
        "patterns": ["wm_task_removed", "finish-imm:remove-task"],
        "confidence": 0.98,
    },
    {
        "id": "PROCESS_KILLED_BY_SYSTEM",
        "kind": "lifecycle",
        "priority": 58,
        "crash_type": "",
        "reason_code": "PROCESS_KILLED_BY_SYSTEM",
        "reason": "进程被系统回收或杀死，不属于闪退",
        "patterns": [
            "has died: cch",
            "has died: cached",
            "has died: empty",
        ],
        "confidence": 0.88,
    },
    {
        "id": "JAVA_REFLECTION_FAILURE",
        "kind": "crash",
        "priority": 40,
        "crash_type": "JAVA_CRASH",
        "reason_code": "REFLECTION_FAILURE",
        "reason": "Java 反射调用失败",
        "requires_any_of": ["FATAL EXCEPTION", "am_crash"],
        "patterns": ["ReflectException", "InvocationTargetException"],
        "confidence": 0.85,
    },
]


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(str(pattern).casefold() in lowered for pattern in patterns)


def _target_markers(text: str, package_name: str) -> bool:
    package_pattern = re.escape(package_name)
    target_line = re.compile(
        rf"Process:\s*{package_pattern}(?:\s|,|$)|"
        rf"am_crash[^\n]*\b{package_pattern}\b|"
        rf">>>\s*{package_pattern}\s*<<<|"
        rf"(?:wm_|finish-imm:)[^\n]*\b{package_pattern}\b|"
        rf"(?:Failure starting process|Force stopping)[^\n]*\b{package_pattern}\b|"
        rf"Package\s+{package_pattern}\s+is currently frozen|"
        rf"(?=[^\n]*\b{package_pattern}\b)[^\n]*"
        rf"(?:ANR|not responding|input dispatching timed out)|"
        rf"(?=[^\n]*\b{package_pattern}\b)[^\n]*"
        rf"has died:\s*(?:cch|cached|empty)",
        re.IGNORECASE,
    )
    return any(target_line.search(line) for line in (text or "").splitlines())


def _target_scope(log_text: str, package_name: str) -> str:
    """Keep the target crash and nearby stack trace, excluding noisy apps."""
    lines = [line for line in (log_text or "").splitlines() if line.strip()]
    if not lines:
        return ""

    package_pattern = re.escape(package_name)
    target_indexes = [
        index
        for index, line in enumerate(lines)
        if re.search(rf"Process:\s*{package_pattern}(?:\s|,|$)", line, re.IGNORECASE)
        or re.search(rf"am_crash[^\n]*\b{package_pattern}\b", line, re.IGNORECASE)
        or re.search(rf">>>\s*{package_pattern}\s*<<<", line, re.IGNORECASE)
        or re.search(rf"(?:wm_|finish-imm:)[^\n]*\b{package_pattern}\b", line, re.IGNORECASE)
        or re.search(
            rf"(?:Failure starting process|Force stopping)[^\n]*\b{package_pattern}\b",
            line,
            re.IGNORECASE,
        )
        or re.search(
            rf"Package\s+{package_pattern}\s+is currently frozen",
            line,
            re.IGNORECASE,
        )
        or (
            re.search(rf"\b{package_pattern}\b", line, re.IGNORECASE)
            and re.search(
                r"ANR|not responding|input dispatching timed out|"
                r"has died:\s*(?:cch|cached|empty)",
                line,
                re.IGNORECASE,
            )
        )
    ]
    if not target_indexes:
        return "\n".join(lines)

    scoped: list[str] = []
    for index in target_indexes[-4:]:
        start = max(0, index - 40)
        end = min(len(lines), index + 140)
        scoped.extend(lines[start:end])
    # Preserve order while removing duplicates caused by overlapping windows.
    return "\n".join(dict.fromkeys(scoped))


def _normalize_class_name(value: str) -> str:
    value = str(value or "").strip().strip('"')
    if value.startswith("L") and value.endswith(";"):
        value = value[1:-1]
    value = value.rstrip(";")
    return value.replace("/", ".")


def _extract_missing_class(text: str) -> str:
    patterns = (
        r"ClassNotFoundException:\s*(?:Failed resolution of:\s*)?L?([\w/$.-]+);?",
        r"Didn't find class\s+[\"']([^\"']+)[\"']",
        r"Failed resolution of:\s*L([\w/$.-]+);",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _normalize_class_name(match.group(1))
    return ""


def _extract_exception(text: str) -> str:
    match = re.search(
        r"(?:AndroidRuntime[^:]*:\s*)?(?:java\.|android\.)?"
        r"([\w.$]+(?:Exception|Error))\b",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _extract_pid(text: str) -> str:
    match = re.search(r"Process:\s*[^,\s]+,\s*PID:\s*(\d+)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _build_evidence_fingerprint(
    package_name: str,
    rule_id: str,
    reason_code: str,
    exception: str,
    root_cause: str,
    missing_class: str = "",
) -> str:
    """Build a stable root-cause fingerprint without PIDs or timestamps."""
    canonical = "|".join(
        value.strip().casefold()
        for value in (
            package_name,
            rule_id,
            reason_code,
            exception,
            root_cause,
            missing_class,
        )
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _extract_summary(text: str, package_name: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    important = (
        package_name.casefold(),
        "fatal exception",
        "androidruntime",
        "fatal signal",
        "am_crash",
        "am_anr",
        "anr",
        "wm_task_removed",
        "remove-task",
        "native crash",
        "segmentation fault",
        "sigsegv",
        "sigabrt",
        "failure starting process",
        "currently frozen",
        "has died:",
        "caused by:",
        "classname",
        "classnotfoundexception",
        "noclassdeffounderror",
        "unsatisfiedlinkerror",
        "outofmemoryerror",
        "securityexception",
        "reflexception",
        "invocationtargetexception",
    )
    selected = [line for line in lines if any(token in line.casefold() for token in important)]
    if not selected:
        selected = lines[-12:]
    return "\n".join(dict.fromkeys(selected[-20:]))


def _rule_matches(rule: dict, text: str) -> bool:
    if rule.get("requires_any_of") and not _contains_any(text, rule["requires_any_of"]):
        return False
    if rule.get("requires_all_of") and not all(
        str(pattern).casefold() in text.casefold()
        for pattern in rule["requires_all_of"]
    ):
        return False
    if rule.get("excludes_any_of") and _contains_any(text, rule["excludes_any_of"]):
        return False
    return _contains_any(text, rule.get("patterns", []))


def _empty_result() -> dict:
    return {
        "crashed": False,
        "crash_type": "",
        "rule_id": "",
        "reason_code": "",
        "reason": "",
        "confidence": 0.0,
        "root_cause": "",
        "missing_class": "",
        "resource_detail": "",
        "exception": "",
        "pid": "",
        "exit_reason": "",
        "summary": "",
        "evidence_fingerprint": "",
        "unknown_signature": "",
    }


def classify_crash_log(
    log_text: str,
    package_name: str,
    *,
    rules: list[dict] | None = None,
) -> dict:
    """Classify one package's log evidence using the historical rule set."""
    package_name = str(package_name or "").strip()
    result = _empty_result()
    if not package_name:
        return result

    scope = _target_scope(log_text, package_name)
    if not _target_markers(scope, package_name):
        return result

    ordered_rules = sorted(
        rules if rules is not None else load_crash_rules(),
        key=lambda rule: int(rule.get("priority", 0)),
        reverse=True,
    )
    matched = next((rule for rule in ordered_rules if _rule_matches(rule, scope)), None)

    # A target FATAL EXCEPTION/am_crash without a known pattern is still a
    # confirmed crash; it should never degrade to a generic process exit.
    java_authoritative = bool(
        re.search(r"FATAL EXCEPTION", scope, re.IGNORECASE)
        and re.search(rf"Process:\s*{re.escape(package_name)}\b", scope, re.IGNORECASE)
    ) or bool(re.search(rf"am_crash[^\n]*\b{re.escape(package_name)}\b", scope, re.IGNORECASE))
    native_authoritative = bool(
        re.search(r"Fatal signal|backtrace:", scope, re.IGNORECASE)
        and _target_markers(scope, package_name)
    )

    if matched is None and java_authoritative:
        matched = {
            "id": "JAVA_CRASH_UNKNOWN",
            "kind": "crash",
            "crash_type": "JAVA_CRASH",
            "reason_code": "UNKNOWN_JAVA_CRASH",
            "reason": "Java 异常，暂未匹配到历史规则",
            "confidence": 0.70,
        }
    elif matched is None and native_authoritative:
        matched = {
            "id": "NATIVE_CRASH_UNKNOWN",
            "kind": "crash",
            "crash_type": "NATIVE_CRASH",
            "reason_code": "UNKNOWN_NATIVE_CRASH",
            "reason": "Native 崩溃，暂未匹配到历史规则",
            "confidence": 0.70,
        }

    if matched is None:
        result["summary"] = _extract_summary(scope, package_name)
        result["unknown_signature"] = _extract_exception(scope)
        return result

    kind = str(matched.get("kind") or "crash")
    crash_type = str(matched.get("crash_type") or "")
    result.update(
        {
            "crashed": kind == "crash",
            "crash_type": crash_type,
            "rule_id": str(matched.get("id") or ""),
            "reason_code": str(matched.get("reason_code") or ""),
            "reason": str(matched.get("reason") or ""),
            "confidence": float(matched.get("confidence", 0.0) or 0.0),
            "pid": _extract_pid(scope),
            "exception": _extract_exception(scope),
            "summary": _extract_summary(scope, package_name),
        }
    )
    if kind == "lifecycle":
        result["exit_reason"] = result["reason_code"]

    cause = re.search(r"Caused by:\s*([^\n]+)", scope, re.IGNORECASE)
    result["root_cause"] = cause.group(1).strip() if cause else result["exception"]
    if matched.get("extract") == "missing_class":
        result["missing_class"] = _extract_missing_class(scope)
        if result["missing_class"]:
            result["root_cause"] = (
                f"缺少运行时类：{result['missing_class']}"
            )
    elif matched.get("extract") == "thread_resource":
        thread_failure = re.search(
            r"(?:pthread_create|unable to create new native thread|"
            r"could not create thread|failed to create thread)[^\n]*",
            scope,
            re.IGNORECASE,
        )
        result["resource_detail"] = (
            thread_failure.group(0).strip() if thread_failure else "线程创建失败"
        )
        result["root_cause"] = result["resource_detail"]
    result["evidence_fingerprint"] = _build_evidence_fingerprint(
        package_name,
        result["rule_id"],
        result["reason_code"],
        result["exception"],
        result["root_cause"],
        result["missing_class"],
    )
    if result["rule_id"] in {"JAVA_CRASH_UNKNOWN", "NATIVE_CRASH_UNKNOWN"}:
        result["unknown_signature"] = result["exception"] or result["reason_code"]
    return result
