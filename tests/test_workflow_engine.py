from workflow_engine import (
    needs_precheck_backend_submission,
    precheck_comment_result,
    precheck_task_status,
    should_install_after_precheck,
)


def test_precheck_policies_are_shared_and_explicit():
    assert needs_precheck_backend_submission({"code": "IAP_ONLY"})
    assert needs_precheck_backend_submission({"code": "ALL_NETWORK_NO_PACKAGE"})
    assert not needs_precheck_backend_submission({"code": "HAS_ADS"})
    assert should_install_after_precheck({"code": "NO_ADS_OR_IAP"})
    assert should_install_after_precheck({"continue_adaptation": True})


def test_unlabeled_page_install_and_launch_remain_eligible_for_adaptation():
    result = {"code": "NO_ADS_OR_IAP", "continue_adaptation": True}
    assert should_install_after_precheck(result)
    assert not needs_precheck_backend_submission(result)
    assert precheck_task_status(result) == "待处理"
    result["install_result"] = {"code": "INSTALLED", "ok": True}
    assert precheck_task_status(result) == "安装完成"
    result["launch_result"] = {"code": "LAUNCH_OK", "ok": True}
    assert precheck_task_status(result) == "启动正常"
    result["launch_result"] = {"code": "APP_CRASHED", "ok": False}
    assert precheck_task_status(result) == "包体闪退"


def test_deferred_download_does_not_become_install_failure_comment():
    result = {
        "code": "HAS_ADS",
        "install_result": {"ok": False, "code": "DOWNLOAD_STARTED"},
    }
    assert precheck_comment_result(result) is result
    assert precheck_task_status(result) == "后台下载中"


def test_launch_failure_has_same_terminal_shape():
    result = {
        "package_name": "com.example.game",
        "launch_result": {
            "ok": False,
            "code": "APP_CRASHED",
            "message": "闪退",
            "summary": "fatal exception",
        },
    }
    terminal = precheck_comment_result(result)
    assert terminal["code"] == "APP_CRASHED"
    assert terminal["package_name"] == "com.example.game"
    assert "fatal exception" in terminal["detail"]
    assert precheck_task_status(result) == "包体闪退"


def test_clean_process_exit_is_pending_recheck_not_terminal_crash():
    result = {
        "package_name": "com.example.game",
        "launch_result": {
            "ok": False,
            "code": "APP_EXITED",
            "message": "进程退出，但没有崩溃证据",
            "summary": "VM exiting with result code 0",
        },
    }

    review = precheck_comment_result(result)
    assert review["code"] == "APP_EXITED"
    assert precheck_task_status(result) == "启动待复检"
