from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from automation_adaptation import (
    build_aggregation_assessment,
    detect_aggregation_with_one_retry,
    detection_field_issue,
    submit_backend_via_api,
)
from auto_asana.main import classify_precheck_workflow_stages


CODE = "APPLOVIN_SDK_KEY_EMPTY"
STATUS = "AppLovin SDK Key为空，暂不适配"
FIELDS = {
    "ok": True,
    "最终判断": "MAX聚合（强关系证据确认）",
    "归因平台": "Adjust",
    "初始Activity": "com.hobby.goal.ui.activity.PassConsentBindingActivity",
    "应用类型": "Native",
    "插屏聚合id": "f6b2a7c240648227",
    "激励视频聚合id": "",
}


@pytest.mark.parametrize("key", [None, "", "未找到", "未提取到", "null", "unknown", "   "])
def test_adjust_max_missing_key_is_terminal_without_restart_or_submission(key):
    fields = dict(FIELDS, SDK列表=[{"名称": "AppLovin", "key": key}])
    restart = MagicMock()
    result = detect_aggregation_with_one_retry(
        "com.songn.est", MagicMock(), first_fields=fields, restart_app=restart
    )
    assert (result["ok"], result["code"], result["message"]) == (False, CODE, STATUS)
    assert result["fields"]["_adaptation_terminal"] is True
    restart.assert_not_called()
    assert build_aggregation_assessment(fields)["auto_submit"] is False
    session = MagicMock()
    submitted = submit_backend_via_api(
        fields, "com.songn.est", api_url="https://example.test/a2/list",
        x_token="test", token="test", session=session,
    )
    assert submitted["ok"] is False
    assert STATUS in submitted["message"]
    assert session.mock_calls == []


@pytest.mark.parametrize("verdict", ["MAX聚合", "AppLovin聚合", "AppLovin MAX聚合"])
def test_missing_key_gate_handles_platform_names(verdict):
    assert detection_field_issue(dict(FIELDS, 最终判断=verdict)) == (CODE, STATUS)


@pytest.mark.parametrize("sdk_name", ["AppLovin", "AppLovin MAX", "MAX"])
def test_adjust_max_with_detected_key_can_continue(sdk_name):
    fields = dict(FIELDS, SDK列表=[{"名称": sdk_name, "key": "valid-sdk-key"}])
    assert detection_field_issue(fields) is None
    assert build_aggregation_assessment(fields)["auto_submit"] is True


@pytest.mark.parametrize("platform", ["IronSource", "LevelPlay", "AdMob", "TopOn", "Fyber"])
def test_adjust_other_platforms_do_not_require_applovin_key(platform):
    assert detection_field_issue(dict(FIELDS, 最终判断=platform + "聚合")) is None


def test_appsflyer_rule_is_unchanged():
    fields = dict(FIELDS, 归因平台="AppsFlyer", af_key="af-key")
    assert detection_field_issue(fields) is None
    fields["af_key"] = ""
    assert detection_field_issue(fields)[0] == "AF_KEY_EMPTY"


def test_progressive_log_waits_for_key_before_deciding_empty():
    completed = dict(FIELDS, SDK列表=[{"名称": "AppLovin", "key": "valid-sdk-key"}])
    restart = MagicMock()
    result = detect_aggregation_with_one_retry(
        "com.songn.est", MagicMock(side_effect=[deepcopy(FIELDS), completed]),
        restart_app=restart, initial_wait_seconds=10, initial_poll_seconds=5,
        sleep=MagicMock(),
    )
    assert result["ok"] is True
    restart.assert_not_called()


def test_refresh_restores_terminal_status_and_explicit_reset_clears_it():
    stories = [{"text": f"【APK Tool 自动化适配：{CODE}】\n{STATUS}"}]
    stage = classify_precheck_workflow_stages(stories)
    assert stage.terminal is True
    assert stage.workflow_status == stage.aggregation_detection_status == stage.final_business_status == STATUS
    stories.append({"text": "【APK Tool 自动化适配：RECHECK_REQUESTED】"})
    stage = classify_precheck_workflow_stages(stories)
    assert stage.workflow_status == "待处理"
    assert stage.terminal is False


def test_cached_fields_cannot_enter_backend_or_replay():
    from gui import APKToolApp

    app = APKToolApp.__new__(APKToolApp)
    app._automation_fields = deepcopy(FIELDS)
    app._automation_ensure_clash_vpn_sync = MagicMock()
    app._automation_current_package_name = MagicMock(return_value="com.songn.est")
    app._automation_persist_detected_failure_fields_sync = MagicMock()
    app._automation_mark_failed = MagicMock()
    app._automation_comment_failure = MagicMock()
    app._automation_fill_asana_sync = MagicMock()
    app._automation_submit_backend_sync = MagicMock()
    app._automation_replay_with_id_rotation_sync = MagicMock()
    assert app._automation_execute_post_detection_sync() is False
    app._automation_mark_failed.assert_called_once_with(STATUS, code=CODE)
    app._automation_comment_failure.assert_called_once_with(CODE, STATUS + "\n包名：com.songn.est")
    app._automation_fill_asana_sync.assert_not_called()
    app._automation_submit_backend_sync.assert_not_called()
    app._automation_replay_with_id_rotation_sync.assert_not_called()


def test_missing_key_has_specific_current_list_status():
    from gui import APKToolApp

    app = APKToolApp.__new__(APKToolApp)
    app._automation_precheck_item_id = "row"
    app._safe_after = lambda _delay, callback, *args: callback(*args)
    app._automation_set_status = MagicMock()
    app._automation_log = MagicMock()
    app._set_precheck_task_status = MagicMock()
    app._automation_mark_failed(STATUS, code=CODE)
    app._set_precheck_task_status.assert_called_once_with("row", STATUS)


def test_replay_entry_blocks_missing_key_before_device_operations():
    from gui import APKToolApp

    app = APKToolApp.__new__(APKToolApp)
    app._automation_fields = deepcopy(FIELDS)
    app._automation_set_batch_stage = MagicMock()
    app._automation_ensure_clash_vpn_sync = MagicMock()
    assert app._automation_replay_sync() == {"ok": False, "code": CODE, "message": STATUS}
    app._automation_set_batch_stage.assert_not_called()
    app._automation_ensure_clash_vpn_sync.assert_not_called()
