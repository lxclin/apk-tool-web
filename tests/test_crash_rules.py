from unittest.mock import MagicMock

from crash_rules import classify_crash_log, load_crash_rules
from auto_asana.main import (
    _AUTOMATION_CODE_STATUSES,
    add_precheck_comment_once,
)
from workflow_engine import precheck_comment_result


def test_rule_file_contains_historical_missing_class_rule():
    rules = load_crash_rules()
    assert any(rule.get("id") == "JAVA_MISSING_CLASS" for rule in rules)


def test_native_signal_rule_is_target_specific():
    log_text = """
    F libc: Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR) in tid 1234 (game)
    I DEBUG: >>> com.example.game <<<
    I DEBUG: backtrace:
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is True
    assert result["crash_type"] == "NATIVE_CRASH"
    assert result["rule_id"] == "NATIVE_SIGNAL"


def test_thread_resource_shortage_is_not_generic_heap_oom():
    log_text = """
    E AndroidRuntime: FATAL EXCEPTION: Thread-49
    E AndroidRuntime: Process: com.example.game, PID: 1234
    E AndroidRuntime: java.lang.OutOfMemoryError: pthread_create (1040KB stack) failed: Try again
    E AndroidRuntime: at java.lang.Thread.nativeCreate(Native Method)
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is True
    assert result["rule_id"] == "JAVA_THREAD_RESOURCE"
    assert result["reason_code"] == "THREAD_RESOURCE_EXHAUSTED"
    assert "pthread_create" in result["resource_detail"]


def test_other_package_crash_is_ignored():
    log_text = """
    E AndroidRuntime: FATAL EXCEPTION: main
    E AndroidRuntime: Process: com.other.game, PID: 1234
    E AndroidRuntime: java.lang.RuntimeException: failed
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is False
    assert result["rule_id"] == ""


def test_native_am_crash_aborted_without_backtrace_is_detected():
    log_text = """
    I am_crash: [123,0,com.example.game,820559428,Native crash,Aborted,unknown,0]
    W ActivityTaskManager: Force finishing activity com.example.game/.MainActivity,force-crash
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is True
    assert result["crash_type"] == "NATIVE_CRASH"
    assert result["rule_id"] == "NATIVE_SIGNAL"


def test_java_binary_incompatibility_is_distinguished_from_missing_class():
    log_text = """
    E AndroidRuntime: FATAL EXCEPTION: main
    E AndroidRuntime: Process: com.example.game, PID: 1234
    E AndroidRuntime: java.lang.NoSuchMethodError: No virtual method build()Lcom/example/AdRequest;
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is True
    assert result["rule_id"] == "JAVA_BINARY_INCOMPATIBILITY"
    assert result["reason_code"] == "JAVA_API_BINARY_INCOMPATIBILITY"


def test_frozen_package_start_failure_is_not_a_crash():
    log_text = """
    E ActivityManager: Failure starting process com.example.game
    E ActivityManager: java.lang.SecurityException: Package com.example.game is currently frozen!
    I ActivityManager: Force stopping com.example.game appid=10203 user=0: start failure
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is False
    assert result["rule_id"] == "APP_START_BLOCKED"
    assert result["exit_reason"] == "APP_START_BLOCKED"


def test_cached_process_death_is_not_a_crash():
    log_text = """
    I ActivityManager: Process com.example.game (pid 1234) has died: cch+5 CEM
    I am_proc_died: [0,1234,com.example.game,0,2]
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is False
    assert result["rule_id"] == "PROCESS_KILLED_BY_SYSTEM"
    assert result["exit_reason"] == "PROCESS_KILLED_BY_SYSTEM"


def test_remote_exception_without_target_crash_marker_is_ignored():
    log_text = """
    W PlatformConfigurator: Caused by: android.os.RemoteException: Remote stack trace:
    D AndroidRuntime: Shutting down VM
    """

    result = classify_crash_log(log_text, "com.example.game")

    assert result["crashed"] is False
    assert result["rule_id"] == ""


def test_automation_process_exit_is_pending_review_not_package_crash():
    assert _AUTOMATION_CODE_STATUSES["APP_EXITED_DURING_AUTOMATION"] == "启动待复检"


def test_evidence_fingerprint_ignores_pid_and_timestamps():
    first = classify_crash_log(
        """
        09-17 11:00:00 E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 1234
        E AndroidRuntime: java.lang.ClassNotFoundException: MissingThing
        """,
        "com.example.game",
    )
    second = classify_crash_log(
        """
        09-17 12:05:00 E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 9876
        E AndroidRuntime: java.lang.ClassNotFoundException: MissingThing
        """,
        "com.example.game",
    )

    assert first["evidence_fingerprint"]
    assert first["evidence_fingerprint"] == second["evidence_fingerprint"]


def test_precheck_comment_deduplicates_by_evidence_not_only_status():
    missing_class = classify_crash_log(
        """
        E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 1234
        E AndroidRuntime: java.lang.ClassNotFoundException: MissingThing
        """,
        "com.example.game",
    )
    native_crash = classify_crash_log(
        """
        I am_crash: [123,0,com.example.game,820559428,Native crash,Aborted,unknown,0]
        W ActivityTaskManager: Force finishing activity com.example.game/.MainActivity,force-crash
        """,
        "com.example.game",
    )
    missing_result = precheck_comment_result(
        {
            "package_name": "com.example.game",
            "launch_result": {"ok": False, "code": "APP_CRASHED", **missing_class},
        }
    )
    native_result = precheck_comment_result(
        {
            "package_name": "com.example.game",
            "launch_result": {"ok": False, "code": "APP_CRASHED", **native_crash},
        }
    )

    client = MagicMock()
    client.stories.get_stories_for_task.return_value = []
    assert add_precheck_comment_once(client, "task-1", missing_result) is True
    first_comment = client.stories.create_comment.call_args.args[1]
    assert "证据指纹：" in first_comment

    client.stories.get_stories_for_task.return_value = [{"text": first_comment}]
    assert add_precheck_comment_once(client, "task-1", missing_result) is False
    assert add_precheck_comment_once(client, "task-1", native_result) is True
