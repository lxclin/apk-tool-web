import io
import urllib.error
import zipfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import pytest

from apkpure_recovery import (
    apkcombo_recovery_candidate, download_and_install_apkpure,
    download_and_install_third_party,
)
from workflow_engine import precheck_comment_result, precheck_task_status
from auto_asana.main import classify_precheck_workflow_stages


def artifact():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
    data.seek(0)
    return data


@pytest.mark.parametrize("status,completed,terminal,expected", [
    ("APKCombo待确认", False, False, True),
    ("APKCombo有包", False, False, True),
    ("APKPure待确认", False, False, True),
    ("APKCombo有包", True, False, False),
    ("APKCombo有包", False, True, False),
    ("聚合适配成功", False, True, False),
    ("待处理", False, False, False),
])
def test_recovery_selection(status, completed, terminal, expected):
    task = SimpleNamespace(package_name="com.example.game", completed=completed,
                           workflow_terminal=terminal)
    assert apkcombo_recovery_candidate(task, status) is expected


def test_wrong_package_never_installed():
    with patch("apkpure_recovery.urllib.request.urlopen", return_value=artifact()), \
         patch("apkpure_recovery.extract_package_name_from_artifact", return_value="com.other.game"), \
         patch("apkpure_recovery.push_apk_with_acceptance") as install:
        result = download_and_install_apkpure("com.example.game")
    assert not result["ok"]
    install.assert_not_called()


def test_404_tries_apk_after_xapk_and_accepts_exact_package():
    with patch("apkpure_recovery.urllib.request.urlopen", side_effect=[
        urllib.error.HTTPError("url", 404, "missing", {}, None), artifact(),
    ]) as fetch, \
         patch("apkpure_recovery.extract_package_name_from_artifact", return_value="com.example.game"), \
         patch("apkpure_recovery.push_apk_with_acceptance", return_value=(True, "accepted")) as install:
        result = download_and_install_apkpure("com.example.game")
    assert result["code"] == "APKPURE_INSTALLED"
    assert "/XAPK/" in fetch.call_args_list[0].args[0].full_url
    assert "/APK/" in fetch.call_args_list[1].args[0].full_url
    assert install.call_args.kwargs["expected_package"] == "com.example.game"


def test_challenge_preserves_review_status_and_comment():
    with patch("apkpure_recovery.urllib.request.urlopen", side_effect=
               urllib.error.HTTPError("url", 403, "challenge", {}, None)) as fetch:
        install = download_and_install_apkpure("com.example.game")
    assert fetch.call_count == 1
    result = {"code": "APKPURE_AVAILABLE", "package_name": "com.example.game",
              "install_result": install}
    assert precheck_task_status(result) == "APKPure待确认"
    comment = precheck_comment_result(result)
    assert comment["code"] == "APKPURE_CHECK_FAILED"
    assert "403" in comment["detail"]


def test_cancel_before_request():
    with patch("apkpure_recovery.urllib.request.urlopen") as fetch:
        result = download_and_install_apkpure("com.example.game", cancelled=lambda: True)
    fetch.assert_not_called()
    assert not result["ok"]


@pytest.mark.parametrize("code,status", [
    ("APKCOMBO_CHECK_FAILED", "APKCombo待确认"),
    ("APKPURE_CHECK_FAILED", "APKPure待确认"),
])
def test_refreshed_review_task_remains_retryable(code, status):
    stages = classify_precheck_workflow_stages([{
        "resource_subtype": "comment_added",
        "text": f"【APK Tool 页面预检：{code}】\n需要人工确认",
    }])
    assert stages.workflow_status == status
    assert not stages.terminal
    assert not stages.final_business_status


def test_adaptation_handoff_excludes_other_tasks_and_failed_downloads():
    from gui import APKToolApp

    app = APKToolApp.__new__(APKToolApp)
    app._automation_access_allowed = Mock(return_value=True)
    app._automation_running = False
    app._automation_checkpoint_store = Mock()
    app._automation_checkpoint_store.load.return_value = None
    tasks = [SimpleNamespace(gid=str(i), package_name=f"com.example.game{i}") for i in range(3)]
    app._automation_eligible_precheck_tasks = Mock(return_value=list(enumerate(tasks)))
    app.precheck_task_tree = Mock()
    statuses = ["启动正常", "启动正常", "安装失败"]
    app.precheck_task_tree.item.side_effect = lambda item, _: (item, "pkg", "url", statuses[item])
    app.automation_replay_timeout_var = Mock()
    app.automation_replay_timeout_var.get.return_value = "300"
    app._automation_start_batch_queue = Mock()
    with patch("gui.get_connected_device_profile", return_value={}), \
         patch("gui.new_batch_checkpoint", return_value={}):
        assert app._automation_run_eligible_batch(task_gids={"0", "2"})
    assert app._automation_start_batch_queue.call_args.args[0] == [(0, tasks[0])]


def recovery_app(status):
    from gui import APKToolApp

    app = APKToolApp.__new__(APKToolApp)
    task = SimpleNamespace(gid="old-task", package_name="com.example.game",
                           completed=False, workflow_terminal=False, workflow_status=status)
    app.precheck_task_tree = Mock()
    app.precheck_task_tree.get_children.return_value = ["row"]
    app.precheck_task_tree.selection.return_value = []
    app.precheck_task_tree.item.return_value = (1, task.package_name, "", status)
    app._precheck_tasks = {"row": task}
    app._precheck_new_task_gids = set()
    app._precheck_manual_selection = False
    app._third_party_download_mode = "apkpure"
    app._run_precheck_with_unknown_retry = Mock(return_value={
        "code": "THIRD_PARTY_DOWNLOAD", "package_name": task.package_name,
        "detail": "Google Play 地区不支持",
    })
    return app, task


@pytest.mark.parametrize("status", ["APKCombo待确认", "APKCombo有包", "APKPure待确认"])
def test_normal_queues_include_older_download_failures(status):
    app, task = recovery_app(status)
    queue, _ = app._precheck_batch_queue_from_selection(ignore_selection=True)
    assert queue == [("row", task)]
    assert app._automation_eligible_precheck_tasks({"is_g99": False}) == queue


@pytest.mark.parametrize("status", ["APKCombo待确认", "APKCombo有包", "APKPure待确认"])
@pytest.mark.parametrize("is_g99", [False, True])
def test_normal_auto_adaptation_routes_failed_download_to_apkpure(status, is_g99):
    app, task = recovery_app(status)
    app._automation_set_batch_stage = Mock()
    app._automation_log = Mock()
    app._set_precheck_task_status = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_stop_event = threading.Event()
    with patch("gui.is_package_installed", return_value=False), \
         patch("gui.download_and_install_apkpure", return_value={"ok": True}) as pure, \
         patch("gui.download_and_install_apkcombo") as combo:
        assert app._automation_prepare_g99_task_sync("row", task, {"is_g99": is_g99})
    pure.assert_called_once()
    combo.assert_not_called()
    app._set_precheck_task_status.assert_called_once_with("row", "安装完成")


def test_auto_adaptation_apkpure_failure_keeps_provider_for_retry():
    app, task = recovery_app("APKCombo待确认")
    app._automation_set_batch_stage = Mock()
    app._automation_log = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_stop_event = threading.Event()
    app._automation_comment_review = Mock()
    with patch("gui.is_package_installed", return_value=False), \
         patch("gui.download_and_install_apkpure", return_value={"ok": False, "message": "HTTP 404"}), \
         patch("gui.download_and_install_apkcombo") as combo:
        assert not app._automation_prepare_g99_task_sync("row", task, {})
    combo.assert_not_called()
    assert app._automation_comment_review.call_args.args[2] == "第三方下载待确认"


@pytest.mark.parametrize("status", ["待处理", "待人工检查", "第三方下载待确认"])
@pytest.mark.parametrize("mode", ["fallback", "apkcombo", "apkpure", "aptoide"])
def test_ordinary_automation_prefers_play_even_after_third_party_failure(status, mode):
    app, task = recovery_app(status)
    app._third_party_download_mode = mode
    app._automation_set_batch_stage = Mock()
    app._automation_log = Mock()
    app._set_precheck_task_status = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_stop_event = threading.Event()
    app._run_precheck_with_unknown_retry.return_value = {"code": "HAS_ADS"}
    app._download_third_party_package = Mock()
    order = Mock()
    order.attach_mock(app._run_precheck_with_unknown_retry, "precheck")
    with patch("gui.is_package_installed", return_value=False), \
         patch("gui.install_google_play_app", return_value={"ok": True}) as play:
        order.attach_mock(play, "play")
        assert app._automation_prepare_g99_task_sync("row", task, {})
    assert [call[0] for call in order.mock_calls] == ["precheck", "play"]
    app._download_third_party_package.assert_not_called()


def test_ordinary_automation_falls_back_only_after_play_install_failure():
    app, task = recovery_app("待处理")
    app._automation_set_batch_stage = Mock()
    app._automation_log = Mock()
    app._set_precheck_task_status = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_stop_event = threading.Event()
    app._run_precheck_with_unknown_retry.return_value = {"code": "HAS_ADS"}
    app._download_third_party_package = Mock(return_value={"ok": True})
    order = Mock()
    with patch("gui.is_package_installed", return_value=False), \
         patch("gui.install_google_play_app", return_value={"ok": False, "message": "下载超时"}) as play:
        order.attach_mock(play, "play")
        order.attach_mock(app._download_third_party_package, "third_party")
        assert app._automation_prepare_g99_task_sync("row", task, {})
    assert [call[0] for call in order.mock_calls] == ["play", "third_party"]


@pytest.mark.parametrize("code", ["IAP_ONLY", "UNKNOWN", "NO_DEVICE", "OPEN_FAILED"])
def test_play_precheck_rejection_never_downloads_from_any_source(code):
    app, task = recovery_app("待处理")
    app._automation_set_batch_stage = Mock()
    app._automation_log = Mock()
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_stop_event = threading.Event()
    app._run_precheck_with_unknown_retry.return_value = {"code": code}
    app._remember_precheck_blacklist = Mock()
    app._submit_precheck_blacklist = Mock(side_effect=lambda result: result)
    app._automation_comment_review = Mock()
    app._download_third_party_package = Mock()
    with patch("gui.is_package_installed", return_value=False), \
         patch("gui.install_google_play_app") as play:
        assert not app._automation_prepare_g99_task_sync("row", task, {})
    play.assert_not_called()
    app._download_third_party_package.assert_not_called()
    if code == "IAP_ONLY":
        assert app._automation_task_outcome == "blacklisted"


def test_automation_download_review_restores_as_retryable():
    stages = classify_precheck_workflow_stages([{
        "resource_subtype": "comment_added",
        "text": "【APK Tool 自动化适配：APKPURE_CHECK_FAILED】\nHTTP 404",
    }])
    assert stages.workflow_status == "APKPure待确认"
    assert not stages.terminal
    assert not stages.final_business_status


@pytest.mark.parametrize("mode,aptoide_ok,pure_ok,combo_ok,expected", [
    ("fallback", True, True, True, ["aptoide"]),
    ("fallback", False, True, True, ["aptoide", "apkpure"]),
    ("fallback", False, False, True, ["aptoide", "apkpure", "apkcombo"]),
    ("fallback", False, False, False, ["aptoide", "apkpure", "apkcombo"]),
    ("aptoide", False, True, True, ["aptoide"]),
    ("apkcombo", True, True, False, ["apkcombo"]),
    ("apkpure", True, False, True, ["apkpure"]),
])
def test_download_policy_order_and_single_provider_modes(mode, aptoide_ok, pure_ok, combo_ok, expected):
    calls = []
    def installer(provider, ok):
        def run(*args, **kwargs):
            calls.append(provider)
            if provider == "apkcombo" and mode == "fallback":
                assert kwargs["allow_browser_fallback"] is False
            return {"ok": ok, "message": provider + " result"}
        return run
    result = download_and_install_third_party(
        "com.example.game", mode=mode,
        aptoide_installer=installer("aptoide", aptoide_ok),
        apkcombo_installer=installer("apkcombo", combo_ok),
        apkpure_installer=installer("apkpure", pure_ok),
    )
    assert calls == expected
    assert [attempt["provider"] for attempt in result["download_attempts"]] == expected
    if not result["ok"]:
        assert result["code"] == "THIRD_PARTY_CHECK_FAILED"


def test_cancel_after_first_failure_never_attempts_other_sources():
    stopped = threading.Event()
    def aptoide(*args, **kwargs):
        stopped.set()
        return {"ok": False, "message": "interrupted"}
    pure, combo = Mock(), Mock()
    result = download_and_install_third_party(
        "com.example.game", cancelled=stopped.is_set, aptoide_installer=aptoide,
        apkcombo_installer=combo, apkpure_installer=pure,
    )
    pure.assert_not_called()
    combo.assert_not_called()
    assert result["code"] == "DOWNLOAD_CANCELLED"


@pytest.mark.parametrize("mode", ["fallback", "apkcombo", "apkpure", "aptoide"])
def test_failure_restores_policy_neutral_retry_status(mode):
    stages = classify_precheck_workflow_stages([{
        "resource_subtype": "comment_added",
        "text": f"【APK Tool 自动化适配：THIRD_PARTY_CHECK_FAILED】\n策略：{mode}",
    }])
    assert stages.workflow_status == "第三方下载待确认"
    assert not stages.terminal
    assert not stages.final_business_status


def test_pure_only_g99_precheck_never_contacts_apkcombo():
    from gui import APKToolApp
    app = APKToolApp.__new__(APKToolApp)
    app._third_party_download_mode = "apkpure"
    with patch("gui.run_apkcombo_only_precheck") as combo:
        result = app._run_precheck_for_connected_device(
            "com.example.game", {"use_apkcombo_only": True},
        )
    combo.assert_not_called()
    assert result["code"] == "THIRD_PARTY_DOWNLOAD"


@pytest.mark.parametrize("code", ["APKCOMBO_CHECK_FAILED", "ALL_NETWORK_NO_PACKAGE"])
def test_combo_precheck_failure_enters_download_policy_before_blacklisting(code):
    from gui import APKToolApp
    from workflow_engine import needs_precheck_backend_submission, should_install_after_precheck
    app = APKToolApp.__new__(APKToolApp)
    app._third_party_download_mode = "apkcombo"
    with patch("gui.run_apkcombo_only_precheck", return_value={
        "code": code, "package_name": "com.example.game",
    }):
        result = app._run_precheck_for_connected_device(
            "com.example.game", {"use_apkcombo_only": True},
        )
    assert should_install_after_precheck(result)
    assert not needs_precheck_backend_submission(result)


@pytest.mark.parametrize("mode", ["fallback", "aptoide", "apkpure"])
def test_g99_selected_policy_never_prequeries_apkcombo(mode):
    from gui import APKToolApp
    app = APKToolApp.__new__(APKToolApp)
    app._third_party_download_mode = mode
    with patch("gui.run_apkcombo_only_precheck") as combo, patch("gui.run_google_play_precheck") as play:
        result = app._run_precheck_for_connected_device("com.example.game", {"use_apkcombo_only": True})
    assert result["code"] == "THIRD_PARTY_DOWNLOAD"
    combo.assert_not_called()
    play.assert_not_called()


@pytest.mark.parametrize("mode", ["fallback", "aptoide", "apkpure"])
def test_play_precheck_leaves_provider_order_to_policy(mode):
    from gui import APKToolApp
    app = APKToolApp.__new__(APKToolApp)
    app._third_party_download_mode = mode
    with patch("gui.run_google_play_precheck", return_value={"code": "GOOGLE_NO_PACKAGE", "package_name": "com.example.game"}) as play:
        result = app._run_precheck_for_connected_device("com.example.game", {"connected": True})
    play.assert_called_once_with("com.example.game", verify_apkcombo=False)
    assert result["code"] == "THIRD_PARTY_DOWNLOAD"


@pytest.mark.parametrize("play_result,expect_fallback", [
    ({"ok": False, "code": "INSTALL_FAILED"}, True),
    ({"ok": True, "code": "INSTALLED"}, False),
    ({"ok": False, "code": "DOWNLOAD_STARTED"}, False),
])
def test_page_precheck_download_falls_back_on_play_failure_only(play_result, expect_fallback):
    from gui import APKToolApp
    app = APKToolApp.__new__(APKToolApp)
    app._precheck_cancel_requested = False
    app._safe_after = Mock()
    app._download_third_party_package = Mock(return_value={"ok": True, "code": "APTOIDE_INSTALLED"})
    with patch("gui.install_google_play_app", return_value=play_result):
        result = app._install_after_precheck({"code": "HAS_ADS", "package_name": "com.example.game"})
    assert app._download_third_party_package.called is expect_fallback
    if expect_fallback:
        assert result["install_result"]["code"] == "APTOIDE_INSTALLED"
