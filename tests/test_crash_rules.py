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


def test_target_task_removal_does_not_adopt_another_process_fatal_exception():
    log = '''10-12 10:45:51.000 2292 2328 I wm_task_removed: com.example.game/.Main
10-12 10:45:51.100 19598 5053 E AndroidRuntime: FATAL EXCEPTION: main
10-12 10:45:51.101 19598 5053 E AndroidRuntime: Process: com.google.android.gms, PID: 19598
10-12 10:45:51.102 19598 5053 E AndroidRuntime: java.lang.SecurityException: alarm permission
10-12 10:45:51.103 18570 18589 W System.err: java.lang.ClassNotFoundException: com.android.zygotehole.ZygoteHoleImpl'''
    r = classify_crash_log(log, 'com.example.game')
    assert not r['crashed']
    assert r['rule_id'] == 'TASK_REMOVED'
    assert 'SecurityException' not in r['summary']
    assert 'ZygoteHoleImpl' not in r['root_cause']


def test_target_java_crash_does_not_adopt_system_server_missing_class():
    log = '''10-12 14:20:13.000 8743 8743 E AndroidRuntime: FATAL EXCEPTION: main
10-12 14:20:13.001 8743 8743 E AndroidRuntime: Process: com.example.game, PID: 8743
10-12 14:20:13.002 18570 18589 W System.err: java.lang.ClassNotFoundException: com.android.zygotehole.ZygoteHoleImpl
10-12 14:20:13.003 8743 8743 E AndroidRuntime: java.lang.IllegalStateException: target failure
10-12 14:20:13.004 18570 18589 I am_crash: [8743,0,com.example.game,0,java.lang.IllegalStateException,target failure,unknown,0]'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed']
    assert r['rule_id'] == 'JAVA_CRASH_UNKNOWN'
    assert not r['missing_class']
    assert 'ZygoteHoleImpl' not in r['summary']
    assert 'IllegalStateException' in r['root_cause']


def test_native_event_is_not_reclassified_as_java_missing_class_from_nearby_noise():
    log = '''10-12 14:21:47.000 18570 18589 W System.err: java.lang.ClassNotFoundException: com.android.zygotehole.ZygoteHoleImpl
10-12 14:21:47.781 18570 9720 I am_crash: [9653,0,com.example.game,553172548,Native crash,Aborted,unknown,0]
10-12 14:21:48.020 23957 24168 E AlarmManager: java.lang.SecurityException: Caller com.google.android.gms needs permission'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'NATIVE_SIGNAL'
    assert not r['missing_class']
    assert 'SecurityException' not in r['root_cause']


def test_interleaved_android_runtime_processes_keep_their_own_exceptions():
    log = '''10-12 10:00:00.000 1234 1234 E AndroidRuntime: FATAL EXCEPTION: main
10-12 10:00:00.001 1234 1234 E AndroidRuntime: Process: com.example.game, PID: 1234
10-12 10:00:00.002 5678 5678 E AndroidRuntime: FATAL EXCEPTION: main
10-12 10:00:00.003 5678 5678 E AndroidRuntime: Process: com.other.game, PID: 5678
10-12 10:00:00.004 5678 5678 E AndroidRuntime: java.lang.ClassNotFoundException: ForeignMissingClass
10-12 10:00:00.005 1234 1234 E AndroidRuntime: java.lang.NoSuchMethodError: targetMethod'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'JAVA_BINARY_INCOMPATIBILITY'
    assert 'ForeignMissingClass' not in r['summary']


def test_untimestamped_sequential_java_crashes_are_separate_blocks():
    log = '''E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: Process: com.other.game, PID: 9876
E AndroidRuntime: java.lang.ClassNotFoundException: ForeignMissingClass
E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: Process: com.example.game, PID: 1234
E AndroidRuntime: java.lang.SecurityException: target permission'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['rule_id'] == 'JAVA_SECURITY_EXCEPTION'
    assert 'ForeignMissingClass' not in r['summary']


def test_target_native_dump_stops_at_other_package_dump():
    log = '''10-12 10:00:00.000 9000 9000 F DEBUG: >>> com.example.game <<<
10-12 10:00:00.001 9000 9000 F DEBUG: backtrace:
10-12 10:00:00.002 9000 9000 F DEBUG: #00 pc 123 libtarget.so
10-12 10:00:00.003 9000 9000 F DEBUG: >>> com.other.game <<<
10-12 10:00:00.004 9000 9000 F DEBUG: Abort message: ClassNotFoundException ForeignMissingClass'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'NATIVE_SIGNAL'
    assert 'ForeignMissingClass' not in r['summary']


def test_foreign_native_crash_plus_target_lifecycle_does_not_confirm_target_crash():
    log = '''I wm_task_removed: com.example.game/.Main
F libc: Fatal signal 11 (SIGSEGV) in pid 9876 (com.other.game)
F DEBUG: >>> com.other.game <<<
F DEBUG: backtrace:'''
    r = classify_crash_log(log, 'com.example.game')
    assert not r['crashed'] and r['rule_id'] == 'TASK_REMOVED'


def test_fatal_signal_naming_target_process_is_sufficient_evidence():
    r = classify_crash_log('F libc: Fatal signal 11 (SIGSEGV) in pid 1234 (com.example.game)', 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'NATIVE_SIGNAL'


def test_process_name_prefix_is_not_a_target_match():
    for name in ['com.example.game2', 'com.example.game.clone']:
        log = f'I am_crash: [123,0,{name},0,Native crash,Aborted,unknown,0]'
        assert not classify_crash_log(log, 'com.example.game')['crashed']


def test_secondary_process_crash_is_still_owned_by_target_package():
    log = '''E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: Process: com.example.game:remote, PID: 1234
E AndroidRuntime: java.lang.ClassNotFoundException: OwnedMissingClass'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'JAVA_MISSING_CLASS'


def test_foreign_am_crash_mentioning_target_in_exception_is_ignored():
    log = 'I am_crash: [123,0,com.other.game,0,ClassNotFoundException,com.example.game,unknown,0]'
    assert not classify_crash_log(log, 'com.example.game')['crashed']


def test_long_crash_summary_keeps_package_and_fatal_marker():
    log = '''E AndroidRuntime: FATAL EXCEPTION: main
E AndroidRuntime: Process: com.example.game, PID: 1234
E AndroidRuntime: java.lang.NoClassDefFoundError: MissingClass
''' + '\n'.join(f'E AndroidRuntime: Caused by: java.lang.Exception: cause{i}' for i in range(40))
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed']
    assert 'FATAL EXCEPTION' in r['summary'] and 'Process: com.example.game' in r['summary']


def test_target_tombstone_signal_without_backtrace_is_still_a_crash():
    log = '''F DEBUG: >>> com.example.game <<<
F DEBUG: signal 6 (SIGABRT), code -1 (SI_QUEUE), fault addr --------'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'NATIVE_SIGNAL'
    assert '>>>' in r['summary'] and 'SIGABRT' in r['summary']


def test_real_target_security_crash_is_preserved_despite_foreign_missing_class():
    log = '''10-12 10:00:00.000 1234 1234 E AndroidRuntime: FATAL EXCEPTION: main
10-12 10:00:00.001 1234 1234 E AndroidRuntime: Process: com.example.game, PID: 1234
10-12 10:00:00.002 18570 18589 W System.err: java.lang.ClassNotFoundException: com.android.zygotehole.ZygoteHoleImpl
10-12 10:00:00.003 1234 1234 E AndroidRuntime: java.lang.SecurityException: target permission'''
    r = classify_crash_log(log, 'com.example.game')
    assert r['crashed'] and r['rule_id'] == 'JAVA_SECURITY_EXCEPTION'
    assert 'ZygoteHoleImpl' not in r['root_cause']
