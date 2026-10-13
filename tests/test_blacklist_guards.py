from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from auto_asana.main import AsanaPrecheckTask, classify_precheck_workflow_stages


def story(code, automation=False):
    category = "自动化适配" if automation else "页面预检"
    return {"resource_subtype": "comment_added", "text": f"【APK Tool {category}：{code}】"}


@pytest.mark.parametrize("later", ["THIRD_PARTY_DOWNLOAD", "UNKNOWN", "APKCOMBO_CHECK_FAILED"])
def test_iap_blacklist_survives_backend_failure_and_later_prechecks(later):
    stages = classify_precheck_workflow_stages([
        story("IAP_ONLY"), story("PRECHECK_BLACKLIST_RECORD_NOT_FOUND", True), story(later),
    ])
    assert stages.workflow_status == "已加黑"
    assert stages.terminal
    assert stages.precheck_status == stages.final_business_status == "已加黑"
    assert stages.backend_submission_status == "加黑提交失败"


def test_explicit_recheck_can_reset_blacklist():
    stages = classify_precheck_workflow_stages([
        story("IAP_ONLY"), story("RECHECK_REQUESTED", True), story("THIRD_PARTY_DOWNLOAD"),
    ])
    assert stages.workflow_status == "待处理"
    assert stages.precheck_status == "第三方下载待确认"
    assert not stages.terminal
    assert stages.final_business_status == ""


def app_with_blacklist():
    from gui import APKToolApp
    app = APKToolApp.__new__(APKToolApp)
    task = AsanaPrecheckTask(
        gid="iap-task", name="iap-task", package_name="app.villus.d.chat",
        up2_appid="", gp_link="", precheck_status="已加黑", final_business_status="已加黑",
        # Deliberately stale queue flags and display status.
        workflow_terminal=False, workflow_status="待处理",
    )
    app._precheck_tasks = {"row": task}
    app.precheck_task_tree = Mock()
    app.precheck_task_tree.get_children.return_value = ["row"]
    app.precheck_task_tree.exists.side_effect = lambda item: item == "row"
    app.precheck_task_tree.item.return_value = (1, task.package_name, "", "待处理")
    app.precheck_task_tree.selection.return_value = ["row"]
    app._precheck_new_task_gids = set()
    app._precheck_manual_selection = True
    return app, task


def test_blacklist_excluded_even_from_broad_manual_batch_selection():
    app, _ = app_with_blacklist()
    assert app._precheck_batch_queue_from_selection()[0] == []
    assert app._automation_eligible_precheck_tasks({"is_g99": True}) == []
    assert app._apkpure_recovery_queue() == []


def test_single_precheck_does_not_start_worker_for_blacklisted_package():
    app, task = app_with_blacklist()
    app._precheck_running = False
    app.precheck_input = Mock()
    app.precheck_input.get.return_value = task.package_name
    app._selected_precheck_task = Mock(return_value=("row", task))
    app._precheck_status = Mock()
    with patch("gui.threading.Thread") as worker:
        app._on_start_precheck()
    worker.assert_not_called()
    assert "已加黑" in app._precheck_status.config.call_args.kwargs["text"]


@pytest.mark.parametrize("restored", [False, True])
def test_execution_guard_blocks_download_even_for_old_checkpoint_task(restored):
    app, task = app_with_blacklist()
    app._automation_log = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    if restored:
        task = SimpleNamespace(gid=task.gid, package_name=task.package_name)
    with patch("gui.is_package_installed") as installed, \
         patch("gui.download_and_install_apkcombo") as combo, \
         patch("gui.download_and_install_apkpure") as pure:
        assert not app._automation_prepare_g99_task_sync("" if restored else "row", task, {"is_g99": True})
    installed.assert_not_called()
    combo.assert_not_called()
    pure.assert_not_called()
    assert app._automation_task_outcome == "blacklisted"


def test_local_iap_result_locks_task_before_comment_or_backend_success():
    app, task = app_with_blacklist()
    app._precheck_tasks["row"] = AsanaPrecheckTask(
        gid=task.gid, name=task.name, package_name=task.package_name, up2_appid="", gp_link="",
    )
    app._remember_precheck_blacklist("row", {"code": "IAP_ONLY"})
    locked = app._precheck_tasks["row"]
    assert locked.workflow_terminal
    assert locked.final_business_status == "已加黑"
    assert app._precheck_task_is_blacklisted("row")


def test_checkpoint_keeps_blacklist_business_state():
    from automation_checkpoint import task_record
    from gui import APKToolApp
    app, task = app_with_blacklist()
    restored = APKToolApp._automation_checkpoint_task(task_record("row", task))
    assert restored.precheck_status == "已加黑"
    assert restored.final_business_status == "已加黑"


def test_single_auto_adaptation_cannot_recheck_blacklisted_package():
    app, task = app_with_blacklist()
    app._automation_access_allowed = Mock(return_value=True)
    app._automation_running = False
    app._automation_current_package_name = Mock(return_value=task.package_name)
    app._automation_set_status = Mock()
    with patch("gui.threading.Thread") as worker:
        assert app._automation_run_post_detection() is False
    worker.assert_not_called()
