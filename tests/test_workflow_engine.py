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
