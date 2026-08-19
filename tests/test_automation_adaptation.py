from unittest.mock import MagicMock, patch

import pytest

from automation_adaptation import (
    add_automation_comment_once,
    apply_aggregation_type_fallback,
    attribution_gate_issue,
    auto_submit_backend_url,
    build_aggregation_assessment,
    build_backend_submission_payload,
    build_backend_clear_payload,
    build_chrome_submit_script,
    detect_aggregation_with_one_retry,
    detection_field_issue,
    format_aggregation_fields,
    has_explicit_attribution,
    has_aggregation_type,
    derive_backend_list_url,
    derive_backend_submit_url,
    merge_aggregation_fields_into_notes,
    clear_backend_adaptation_via_api,
    submit_backend_via_api,
    submit_precheck_blacklist_via_api,
    update_asana_aggregation_notes,
    validate_backend_fields,
)


FIELDS = {
    "最终判断": "MAX聚合（强关系证据确认）",
    "初始Activity": "com.demo.MainActivity",
    "应用类型": "Unity",
    "插屏聚合id": "inter-1",
    "激励视频聚合id": "reward-1",
    "归因平台": "AppsFlyer",
    "af_key": "af-key",
    "SDK列表": [{"名称": "AppLovin", "key": "sdk-key"}],
}


def test_formats_fields_like_manual_copy():
    text = format_aggregation_fields(FIELDS)

    assert text.splitlines() == [
        "最终判断:MAX聚合（强关系证据确认）",
        "识别方式:AutoDetector明确判断",
        "识别置信度:高",
        (
            "识别依据:AutoDetector 输出最终判断：MAX聚合（强关系证据确认）；"
            "检测到插屏聚合id：inter-1；检测到激励视频聚合id：reward-1；"
            "检测到归因平台：AppsFlyer"
        ),
        "自动提交策略:允许自动提交",
        "初始Activity:com.demo.MainActivity",
        "AppLovin SDK Key:sdk-key",
        "应用类型:Unity",
        "激励视频聚合id:reward-1",
        "插屏聚合id:inter-1",
        "归因平台:AppsFlyer",
    ]


def test_merges_below_gp_link_and_replaces_old_result():
    existing = (
        "包名：com.demo\nUP2 appid：app-1\n"
        "GP链接：https://play.google.com/store/apps/details?id=com.demo\n\n"
        "最终判断:旧结果\n插屏聚合id:old\n"
    )

    merged = merge_aggregation_fields_into_notes(existing, "最终判断:新结果")

    assert merged == (
        "包名：com.demo\nUP2 appid：app-1\n"
        "GP链接：https://play.google.com/store/apps/details?id=com.demo\n\n"
        "最终判断:新结果\n"
    )


def test_updates_known_asana_task_notes():
    client = MagicMock()

    merged = update_asana_aggregation_notes(
        client,
        "task-1",
        "包名：com.demo\nUP2 appid：app-1\nGP链接：https://example.test",
        FIELDS,
    )

    client.tasks.update_task.assert_called_once_with("task-1", {"notes": merged})
    assert "最终判断:MAX聚合" in merged


def test_terminal_note_is_written_to_asana_description():
    client = MagicMock()

    merged = update_asana_aggregation_notes(
        client,
        "task-1",
        "包名：com.demo\nGP链接：https://example.test",
        FIELDS,
        terminal_note="未识别出聚合类型，暂不适配",
    )

    assert "适配结论:未识别出聚合类型，暂不适配" in merged
    client.tasks.update_task.assert_called_once_with("task-1", {"notes": merged})


def test_empty_aggregation_type_is_not_written_to_asana_notes():
    client = MagicMock()

    with pytest.raises(ValueError, match="聚合类型识别为空"):
        update_asana_aggregation_notes(
            client,
            "task-1",
            "包名：com.demo\nGP链接：https://example.test",
            {"最终判断": "未提取到"},
        )

    client.tasks.update_task.assert_not_called()


def test_empty_aggregation_type_restarts_and_second_detection_can_succeed():
    restart = MagicMock(return_value=(True, "应用已重新启动"))
    extract = MagicMock(return_value={
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "inter-123",
    })
    sleep = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.demo",
        extract,
        first_fields={"ok": True, "最终判断": "未提取到"},
        restart_app=restart,
        wait_seconds=2,
        sleep=sleep,
    )

    assert result["ok"] is True
    assert result["code"] == "AGGREGATION_TYPE_DETECTED_AFTER_RETRY"
    assert result["attempts"] == 2
    restart.assert_called_once_with("com.demo")
    extract.assert_called_once_with()
    # The retry now polls immediately, so a ready verdict is returned without
    # wasting the old fixed two-second sleep.
    sleep.assert_not_called()


def test_retry_grace_poll_catches_final_verdict_arriving_just_after_45_seconds():
    empty = {"ok": True, "最终判断": ""}
    detected = {
        "ok": True,
        "最终判断": "MAX聚合（强关系证据确认）",
        "归因平台": "AppsFlyer",
        "af_key": "qi48MruY6ShSexcEgTDWrm",
        "插屏聚合id": "7f56ca2cd7bc5e7b",
    }
    extract = MagicMock(side_effect=[empty, detected])
    sleep = MagicMock()
    runtime_reset = MagicMock()
    progress = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.mobilityware.MahjongSolitaire",
        extract,
        first_fields=empty,
        restart_app=MagicMock(return_value=(True, "应用已重新启动")),
        wait_seconds=1,
        grace_seconds=10,
        poll_seconds=5,
        sleep=sleep,
        runtime_reset=runtime_reset,
        on_progress=progress,
    )

    assert result["ok"] is True
    assert result["code"] == "AGGREGATION_TYPE_DETECTED_AFTER_RETRY"
    assert result["fields"]["最终判断"].startswith("MAX聚合")
    assert extract.call_count == 2
    assert [call.args[0] for call in sleep.call_args_list] == [5]
    runtime_reset.assert_called_once_with()
    assert any(
        "应用已重新启动" in call.args[0]
        for call in progress.call_args_list
    )


def test_partial_mediation_evidence_is_not_reported_as_empty_type():
    fields = {
        "ok": True,
        "最终判断": "",
        "激励视频聚合id": "1d9ba7a67f93b368, 109629fea9927539",
        "SDK列表": [{"名称": "AppLovin", "key": "sdk-key"}],
    }

    issue = detection_field_issue(fields)

    assert issue[0] == "AGGREGATION_RESULT_INCOMPLETE"
    assert "综合检测结果未完整输出" in issue[1]


def test_retry_polls_past_old_45_second_boundary_until_final_verdict():
    empty = {
        "ok": True,
        "最终判断": "",
        "激励视频聚合id": "reward-1",
        "完整日志": "激励视频广告单元ID列表: [reward-1]",
    }
    detected = {
        "ok": True,
        "最终判断": "MAX聚合（强关系证据确认）",
        "归因平台": "Adjust",
        "激励视频聚合id": "reward-1",
        "完整日志": "最终判断: MAX聚合（强关系证据确认）",
    }
    snapshots = [empty] * 10 + [detected]
    sleep = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.match.mahjong.pair.puzzle",
        MagicMock(side_effect=snapshots),
        first_fields=empty,
        restart_app=MagicMock(return_value=(True, "应用已重新启动")),
        wait_seconds=60,
        grace_seconds=30,
        poll_seconds=5,
        sleep=sleep,
    )

    assert result["ok"] is True
    assert result["fields"]["最终判断"].startswith("MAX聚合")
    # Ten five-second polls prove that the old 45-second cut-off was crossed.
    assert sleep.call_count == 10


def test_initial_empty_snapshot_waits_for_app_detector_before_restart():
    empty = {"ok": True, "最终判断": ""}
    detected = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "inter-123",
    }
    extract = MagicMock(side_effect=[empty, detected])
    restart = MagicMock()
    sleep = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.havystudio.siegearena",
        extract,
        restart_app=restart,
        initial_wait_seconds=10,
        initial_poll_seconds=5,
        sleep=sleep,
    )

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert result["fields"]["最终判断"] == "MAX聚合"
    restart.assert_not_called()
    assert extract.call_count == 2
    assert sleep.call_count == 1


def test_initial_poll_tolerates_transient_logcat_timeout_and_then_detects():
    transient = {
        "ok": False,
        "error": "设备在线，但 Logcat 读取超时",
        "_runtime_code": "LOGCAT_READ_TIMEOUT",
        "_transient": True,
    }
    detected = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "inter-123",
    }
    extract = MagicMock(side_effect=[transient, detected])
    restart = MagicMock()
    progress = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.demo",
        extract,
        restart_app=restart,
        initial_wait_seconds=10,
        initial_poll_seconds=5,
        sleep=MagicMock(),
        on_progress=progress,
    )

    assert result["ok"] is True
    assert result["attempts"] == 1
    restart.assert_not_called()
    assert any("Logcat 读取超时" in call.args[0] for call in progress.call_args_list)


def test_retry_poll_tolerates_transient_logcat_timeout_and_then_detects():
    first = {"ok": True, "最终判断": ""}
    transient = {
        "ok": False,
        "error": "设备在线，但 Logcat 读取超时",
        "_runtime_code": "LOGCAT_READ_TIMEOUT",
        "_transient": True,
    }
    detected = {
        "ok": True,
        "最终判断": "IronSource聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "inter",
        "激励视频聚合id": "video",
    }

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(side_effect=[transient, detected]),
        first_fields=first,
        restart_app=MagicMock(return_value=(True, "ok")),
        wait_seconds=10,
        grace_seconds=0,
        poll_seconds=5,
        sleep=MagicMock(),
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["fields"]["最终判断"] == "IronSource聚合"


def test_two_empty_aggregation_detections_return_terminal_empty_status():
    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value={"ok": True, "最终判断": ""}),
        first_fields={"ok": True, "最终判断": "未找到"},
        restart_app=MagicMock(return_value=(True, "应用已重新启动")),
        wait_seconds=0,
    )

    assert result["ok"] is False
    assert result["code"] == "AGGREGATION_TYPE_EMPTY"
    assert result["message"] == "聚合类型识别为空"
    assert result["attempts"] == 2
    assert result["fields"]["_aggregation_retry_exhausted"] is True
    assert has_aggregation_type(result["fields"]) is False


def test_appsflyer_attribution_retries_when_af_key_is_empty():
    first = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "AppsFlyer, AppMetrica",
        "插屏聚合id": "inter-123",
        "af_key": "未找到",
    }
    second = {**first, "af_key": "real-af-key"}

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value=second),
        first_fields=first,
        restart_app=MagicMock(return_value=(True, "ok")),
        wait_seconds=0,
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["message"] == "重启游戏后已提取 af_key"


def test_appsflyer_af_key_still_empty_after_retry_is_terminal():
    fields = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "AppsFlyer",
        "激励视频聚合id": "reward-123",
        "af_key": "",
    }

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value=fields),
        first_fields=fields,
        restart_app=MagicMock(return_value=(True, "ok")),
        wait_seconds=0,
    )

    assert result["ok"] is False
    assert result["code"] == "AF_KEY_EMPTY"
    assert result["message"] == "af_key为空，再次确认"


def test_both_ad_ids_empty_retry_once_but_one_detected_id_is_enough():
    first = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "",
        "激励视频聚合id": "未找到",
    }
    second = {**first, "激励视频聚合id": "reward-123"}

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value=second),
        first_fields=first,
        restart_app=MagicMock(return_value=(True, "ok")),
        wait_seconds=0,
    )

    assert result["ok"] is True
    assert result["message"] == "重启游戏后已提取聚合 ID"
    assert detection_field_issue(second) is None


def test_both_ad_ids_still_empty_after_retry_is_terminal():
    fields = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Adjust",
        "插屏聚合id": "inter",
        "激励视频聚合id": "video",
    }

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value=fields),
        first_fields=fields,
        restart_app=MagicMock(return_value=(True, "ok")),
        wait_seconds=0,
    )

    assert result["ok"] is False
    assert result["code"] == "AD_IDS_EMPTY"
    assert result["message"] == "插屏聚合id和激励视频聚合id均为空，再次确认"


def test_ironsource_symbolic_video_and_inter_are_valid_ad_ids():
    fields = {
        "最终判断": "IronSource聚合（自动化检测确认）",
        "初始Activity": "com.unity3d.player.UnityPlayerActivity",
        "归因平台": "AppsFlyer",
        "af_key": "3F4A8Ct5TV8mYprgtEZhFF",
        "激励视频聚合id": "video",
        "插屏聚合id": "inter",
        "SDK列表": [{"名称": "LevelPlay", "key": "19992b405"}],
    }

    assert detection_field_issue(fields) is None
    assert validate_backend_fields(fields, "com.demo") == []
    payload = build_backend_submission_payload(fields, "com.demo", "rain")
    assert payload["aggr_platform"] == "iron_source"
    assert payload["aggr_chaping_id"] == "inter"
    assert payload["aggr_jilishipin_id"] == "video"


def test_empty_verdict_with_exact_video_inter_pair_is_inferred_as_ironsource():
    first = {
        "ok": True,
        "最终判断": "",
        "初始Activity": "sheep.animal.parking.puzzle.game.jam.StartAct",
        "归因平台": "AppsFlyer",
        "af_key": "HNAPrdT7tExxnkubcSetvF",
        "激励视频聚合id": "video",
        "插屏聚合id": "inter",
    }
    restart = MagicMock()

    result = detect_aggregation_with_one_retry(
        "sheep.animal.parking.puzzle.game.jam",
        MagicMock(),
        first_fields=first,
        restart_app=restart,
        wait_seconds=0,
    )

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert result["code"] == "AGGREGATION_TYPE_DETECTED"
    assert result["fields"]["最终判断"].startswith("IronSource聚合")
    assert result["fields"]["_aggregation_type_inferred"] is True
    restart.assert_not_called()
    payload = build_backend_submission_payload(
        result["fields"], "sheep.animal.parking.puzzle.game.jam", "rain"
    )
    assert payload["aggr_platform"] == "iron_source"
    assert payload["aggr_chaping_id"] == "inter"
    assert payload["aggr_jilishipin_id"] == "video"
    assessment = build_aggregation_assessment(result["fields"])
    assert assessment["confidence"] == "中"
    assert assessment["method"] == "业务规则推断"
    assert assessment["auto_submit"] is True
    assert "AutoDetector 原始最终判断为空" in assessment["evidence"]


def test_explicit_max_retry_overrides_provisional_ironsource_inference():
    first = {
        "ok": True,
        "最终判断": "",
        "初始Activity": "com.easybrain.number.sums.puzzle.SplashActivity",
        "归因平台": "Adjust",
        "应用类型": "Unity",
        "激励视频聚合id": "video",
        "插屏聚合id": "inter",
    }
    second = {
        "ok": True,
        "最终判断": "MAX聚合（强关系证据确认）",
        "初始Activity": "com.easybrain.number.sums.puzzle.SplashActivity",
        "归因平台": "Adjust",
        "应用类型": "Unity",
        "激励视频聚合id": "dda50d1ff1b9edcf",
        "插屏聚合id": "7feec795ef299377",
        "SDK列表": [
            {
                "名称": "AppLovin",
                "key": "VZuNZ7p1cRtpHhz4vIOd6XWAq5N",
            }
        ],
    }
    restart = MagicMock(return_value=(True, "应用已重新启动"))

    result = detect_aggregation_with_one_retry(
        "com.easybrain.number.sums.puzzle",
        MagicMock(return_value=second),
        first_fields=first,
        restart_app=restart,
        wait_seconds=0,
        retry_inferred=True,
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert result["code"] == "AGGREGATION_TYPE_CHANGED_AFTER_RETRY"
    assert result["fields"]["最终判断"].startswith("MAX聚合")
    assert result["fields"]["激励视频聚合id"] == "dda50d1ff1b9edcf"
    assert result["fields"]["插屏聚合id"] == "7feec795ef299377"
    assert result["fields"].get("_aggregation_type_inferred") is not True
    assert result["fields"]["_aggregation_type_changed_after_retry"] is True
    payload = build_backend_submission_payload(
        result["fields"], "com.easybrain.number.sums.puzzle", "rain"
    )
    assert payload["aggr_platform"] == "max"
    assert payload["aggr_chaping_id"] == "7feec795ef299377"
    assert payload["aggr_jilishipin_id"] == "dda50d1ff1b9edcf"


@pytest.mark.parametrize(
    ("rewarded", "interstitial"),
    [("video", ""), ("", "inter"), ("inter", "video"), ("rewarded", "inter")],
)
def test_incomplete_or_mismatched_symbolic_ids_do_not_infer_ironsource(
    rewarded, interstitial
):
    fields = {
        "最终判断": "",
        "激励视频聚合id": rewarded,
        "插屏聚合id": interstitial,
    }

    apply_aggregation_type_fallback(fields)

    assert fields["最终判断"] == ""


def test_levelplay_symbolic_video_and_inter_are_valid_ad_ids():
    fields = {
        "最终判断": "LevelPlay聚合（自动化检测确认）",
        "初始Activity": "MainActivity",
        "归因平台": "Adjust",
        "激励视频聚合id": "video",
        "插屏聚合id": "inter",
    }

    assert detection_field_issue(fields) is None
    assert validate_backend_fields(fields, "com.demo") == []


def test_symbolic_video_and_inter_remain_invalid_for_max():
    fields = {
        "最终判断": "MAX聚合",
        "初始Activity": "MainActivity",
        "归因平台": "Adjust",
        "激励视频聚合id": "video",
        "插屏聚合id": "inter",
    }

    assert detection_field_issue(fields) == (
        "AD_IDS_EMPTY",
        "插屏聚合id和激励视频聚合id均为空，再次确认",
    )
    assert "插屏和激励视频聚合 ID 均为空" in validate_backend_fields(
        fields, "com.demo"
    )


def test_backend_requires_af_key_only_for_appsflyer_attribution():
    appsflyer_fields = {
        "最终判断": "MAX聚合",
        "初始Activity": "Main",
        "归因平台": "AppsFlyer, AppMetrica",
        "插屏聚合id": "inter-123",
    }
    adjust_fields = {**appsflyer_fields, "归因平台": "Adjust"}

    assert "缺少 af_key（归因平台包含 AppsFlyer）" in validate_backend_fields(
        appsflyer_fields, "com.demo"
    )
    assert "缺少 af_key（归因平台包含 AppsFlyer）" not in validate_backend_fields(
        adjust_fields, "com.demo"
    )


@pytest.mark.parametrize(
    "attribution",
    ["Adjust", "AppsFlyer", "Adjust, AppMetrica", "AppsFlyer, Singular"],
)
def test_attribution_gate_allows_values_containing_adjust_or_appsflyer(attribution):
    assert attribution_gate_issue({"归因平台": attribution}) is None


@pytest.mark.parametrize(
    "attribution",
    ["Singular", "Tenjin", "ThinkingData", "AppMetrica", "Airbridge", ""],
)
def test_attribution_gate_rejects_other_or_unknown_platforms(attribution):
    issue = attribution_gate_issue({"归因平台": attribution})

    assert issue[0] == "UNSUPPORTED_ATTRIBUTION"
    assert issue[1].endswith("归因，暂不适配")


def test_unsupported_attribution_stops_without_restart_or_retry():
    fields = {
        "ok": True,
        "最终判断": "MAX聚合",
        "归因平台": "Singular, AppMetrica",
        "插屏聚合id": "inter-123",
    }
    restart = MagicMock()
    extract = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.demo",
        extract,
        first_fields=fields,
        restart_app=restart,
        wait_seconds=0,
    )

    assert result["ok"] is False
    assert result["code"] == "UNSUPPORTED_ATTRIBUTION"
    assert result["message"] == "Singular, AppMetrica归因，暂不适配"
    assert result["attempts"] == 1
    restart.assert_not_called()
    extract.assert_not_called()


def test_progressive_final_verdict_waits_for_detailed_attribution_fields():
    verdict_only = {
        "ok": True,
        "最终判断": "MAX聚合（强关系证据确认）",
        "归因平台": "",
        "插屏聚合id": "",
        "激励视频聚合id": "",
        "完整日志": "最终判断: MAX聚合（强关系证据确认）",
    }
    detailed = {
        **verdict_only,
        "初始Activity": "com.pfg.uflm.BootstrapActivity",
        "应用类型": "Native",
        "归因平台": "AppsFlyer",
        "af_key": "oTiS8cJmQRTDXbixob6wQL",
        "插屏聚合id": "inter-1",
        "完整日志": "归因平台: AppsFlyer",
    }
    extract = MagicMock(side_effect=[verdict_only, detailed])
    restart = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.xten.ufl",
        extract,
        first_fields=None,
        restart_app=restart,
        initial_wait_seconds=10,
        initial_poll_seconds=5,
        sleep=lambda _seconds: None,
    )

    assert result["ok"] is True
    assert result["fields"]["归因平台"] == "AppsFlyer"
    assert result["fields"]["af_key"] == "oTiS8cJmQRTDXbixob6wQL"
    restart.assert_not_called()


def test_empty_attribution_is_not_explicit_but_named_platform_is():
    assert has_explicit_attribution({"归因平台": ""}) is False
    assert has_explicit_attribution({"归因平台": "未找到"}) is False
    assert has_explicit_attribution({"归因平台": "SolarEngine"}) is True


def test_runtime_crash_stops_detection_before_empty_field_retry():
    runtime_check = MagicMock(return_value={
        "ok": False,
        "code": "APP_CRASHED",
        "message": "包体在自动化检测过程中闪退，暂不适配",
        "summary": "FATAL EXCEPTION: main",
    })
    restart = MagicMock()

    result = detect_aggregation_with_one_retry(
        "com.demo",
        MagicMock(return_value={"ok": True}),
        first_fields=None,
        restart_app=restart,
        initial_wait_seconds=5,
        initial_poll_seconds=1,
        sleep=lambda _seconds: None,
        runtime_check=runtime_check,
    )

    assert result["ok"] is False
    assert result["code"] == "APP_CRASHED"
    assert result["runtime"]["summary"] == "FATAL EXCEPTION: main"
    restart.assert_not_called()


def test_unsupported_attribution_is_not_written_or_submitted():
    client = MagicMock()
    fields = {
        "最终判断": "MAX聚合",
        "初始Activity": "Main",
        "归因平台": "Tenjin",
        "插屏聚合id": "inter-123",
    }

    with pytest.raises(ValueError, match="Tenjin归因，暂不适配"):
        update_asana_aggregation_notes(client, "task-1", "原描述", fields)
    assert "Tenjin归因，暂不适配" in validate_backend_fields(fields, "com.demo")
    client.tasks.update_task.assert_not_called()


def test_unsupported_attribution_can_be_persisted_by_terminal_workflow():
    client = MagicMock()
    fields = {
        "最终判断": "MAX聚合",
        "初始Activity": "Main",
        "归因平台": "SolarEngine",
        "插屏聚合id": "inter-123",
    }

    merged = update_asana_aggregation_notes(
        client,
        "task-1",
        "包名：com.demo\nGP链接：https://example.test",
        fields,
        allow_unsupported_attribution=True,
    )
    errors = validate_backend_fields(
        fields,
        "com.demo",
        allow_unsupported_attribution=True,
    )

    assert "归因平台:SolarEngine" in merged
    assert errors == []
    client.tasks.update_task.assert_called_once()
    assessment = build_aggregation_assessment(fields)
    assert assessment["auto_submit"] is True
    assert assessment["policy"] == "允许提交并跳过回放"


def test_automation_comment_is_idempotent():
    client = MagicMock()
    client.stories.get_stories_for_task.return_value = [
        {"text": "【APK Tool 自动化适配：AD_REPLAY_FAILED】\n旧评论"}
    ]

    created = add_automation_comment_once(
        client, "task-1", "AD_REPLAY_FAILED", "新评论"
    )

    assert created is False
    client.stories.create_comment.assert_not_called()


def test_validates_at_least_one_ad_id():
    errors = validate_backend_fields(
        {"最终判断": "MAX聚合", "初始Activity": "Main"},
        "com.demo",
    )
    assert "插屏和激励视频聚合 ID 均为空" in errors


def test_derives_submit_endpoint_from_configured_list_endpoint():
    assert derive_backend_submit_url(
        "http://8.131.54.114/admin/gd_web/overseas/cp_adapt/list"
    ) == "http://8.131.54.114/admin/gd_web/overseas/s10_package_info"


def test_derives_list_endpoint_from_submit_endpoint():
    assert derive_backend_list_url(
        "http://8.131.54.114/admin/gd_web/overseas/s10_package_info"
    ) == "http://8.131.54.114/admin/gd_web/overseas/cp_adapt/list"


def test_builds_api_payload_with_same_mapping_as_manual_url():
    payload = build_backend_submission_payload(FIELDS, "com.demo", "rain")

    assert payload == {
        "package_name": "com.demo",
        "aggr_platform": "max",
        "attribution_platform": "AppsFlyer",
        "aggr_chaping_id": "inter-1",
        "aggr_jilishipin_id": "reward-1",
        "ps": None,
        "block_ps": None,
        "af_key": "af-key",
        "manual_applovin_sdk_key": "sdk-key",
        "activity_main_page": "com.demo.MainActivity",
        "activity_guide_page": None,
        "user_name": "rain",
    }


def test_api_submit_posts_tokens_and_verifies_response_fields():
    session = MagicMock()
    payload = build_backend_submission_payload(FIELDS, "com.demo", "rain")
    submit_response = MagicMock()
    submit_response.json.return_value = {
        "code": 200,
        "data": payload,
    }
    readback_response = MagicMock()
    readback_response.json.return_value = {
        "code": 200,
        "data": {"code": 200, "data": [payload], "total": 1},
    }
    session.post.side_effect = [submit_response, readback_response]
    cache_response = MagicMock()
    cache_response.json.return_value = {
        "code": 200,
        "message": "success",
        "data": 1,
    }
    session.get.return_value = cache_response

    result = submit_backend_via_api(
        FIELDS,
        "com.demo",
        api_url="http://8.131.54.114/admin/gd_web/overseas/cp_adapt/list",
        x_token="session-token",
        token="fixed-token",
        user_name="rain",
        readback_delay_seconds=0,
        session=session,
    )

    assert result["ok"] is True
    assert result["code"] == "BACKEND_SUBMITTED"
    assert result["backend_readback_verified"] is True
    assert result["device_verification"] == "pending_replay"
    request = session.post.call_args_list[0]
    assert request.args[0].endswith("/s10_package_info")
    assert request.kwargs["headers"]["X-Token"] == "session-token"
    assert request.kwargs["headers"]["token"] == "fixed-token"
    assert request.kwargs["json"]["package_name"] == "com.demo"
    readback_request = session.post.call_args_list[1]
    assert readback_request.args[0].endswith("/cp_adapt/list")
    assert readback_request.kwargs["json"]["package_name"] == "com.demo"
    cache_request = session.get.call_args
    assert cache_request.args[0].endswith("/a2/delete_a2_package_cache")
    assert cache_request.kwargs["params"] == {"package_name": "com.demo"}
    assert cache_request.kwargs["headers"]["X-Token"] == "session-token"


def test_inferred_replay_failure_clears_all_backend_fields_and_keeps_note():
    session = MagicMock()
    payload = build_backend_clear_payload("com.inferred.game", "rain")
    submit_response = MagicMock()
    submit_response.json.return_value = {"code": 200, "data": payload}
    readback_response = MagicMock()
    readback_response.json.return_value = {
        "code": 200,
        "data": {"code": 200, "data": [payload], "total": 1},
    }
    session.post.side_effect = [submit_response, readback_response]
    cache_response = MagicMock()
    cache_response.json.return_value = {
        "code": 200,
        "message": "success",
        "data": 1,
    }
    session.get.return_value = cache_response

    result = clear_backend_adaptation_via_api(
        "com.inferred.game",
        api_url="http://8.131.54.114/admin/gd_web/overseas/cp_adapt/list",
        x_token="session-token",
        token="fixed-token",
        user_name="rain",
        readback_delay_seconds=0,
        session=session,
    )

    assert result["ok"] is True
    submitted = session.post.call_args_list[0].kwargs["json"]
    assert submitted["ps"] == "未识别出聚合类型，暂不适配"
    for field in (
        "aggr_platform",
        "attribution_platform",
        "aggr_chaping_id",
        "aggr_jilishipin_id",
        "block_ps",
        "af_key",
        "manual_applovin_sdk_key",
        "activity_main_page",
        "activity_guide_page",
    ):
        assert submitted[field] is None
    session.get.assert_called_once()


def test_precheck_blacklist_preserves_record_submits_reason_and_clears_cache():
    session = MagicMock()
    existing = {
        "package_name": "com.iap.game",
        "aggr_platform": None,
        "attribution_platform": None,
        "aggr_chaping_id": None,
        "aggr_jilishipin_id": None,
        "ps": "旧备注",
        "block_ps": None,
        "af_key": None,
        "manual_applovin_sdk_key": None,
        "activity_main_page": None,
        "activity_guide_page": None,
    }
    lookup = MagicMock()
    lookup.json.return_value = {
        "code": 200,
        "data": {"code": 200, "data": [existing], "total": 1},
    }
    submitted = {
        **existing,
        "block_ps": "应用内购，无广告，加黑",
        "user_name": "rain",
    }
    submit = MagicMock()
    submit.json.return_value = {"code": 200, "data": submitted}
    readback = MagicMock()
    readback.json.return_value = {
        "code": 200,
        "data": {"code": 200, "data": [submitted], "total": 1},
    }
    session.post.side_effect = [lookup, submit, readback]
    cache = MagicMock()
    cache.json.return_value = {"code": 200, "message": "success", "data": 1}
    session.get.return_value = cache

    result = submit_precheck_blacklist_via_api(
        {"code": "IAP_ONLY", "package_name": "com.iap.game"},
        api_url="http://example.test/cp_adapt/list",
        x_token="session-token",
        token="fixed-token",
        readback_delay_seconds=0,
        session=session,
    )

    assert result["ok"] is True
    assert result["code"] == "PRECHECK_BLACKLIST_SUBMITTED"
    submit_request = session.post.call_args_list[1]
    assert submit_request.args[0].endswith("/s10_package_info")
    assert submit_request.kwargs["json"]["block_ps"] == "应用内购，无广告，加黑"
    assert submit_request.kwargs["json"]["ps"] == "旧备注"
    assert session.get.call_args.kwargs["params"] == {"package_name": "com.iap.game"}


def test_precheck_blacklist_rejects_non_blacklist_result():
    result = submit_precheck_blacklist_via_api(
        {"code": "HAS_ADS", "package_name": "com.ads.game"},
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
    )

    assert result["ok"] is False
    assert result["code"] == "PRECHECK_BLACKLIST_VALIDATION_FAILED"


def test_api_submit_rejects_response_field_mismatch():
    session = MagicMock()
    response = MagicMock()
    record = build_backend_submission_payload(FIELDS, "com.demo", "rain")
    record["aggr_chaping_id"] = "wrong-id"
    response.json.return_value = {"code": 200, "data": record}
    session.post.return_value = response

    result = submit_backend_via_api(
        FIELDS,
        "com.demo",
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        session=session,
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_VERIFY_FAILED"
    assert "aggr_chaping_id" in result["message"]
    session.get.assert_not_called()


def test_api_submit_does_not_succeed_when_cache_clear_fails():
    session = MagicMock()
    response = MagicMock()
    response.json.return_value = {
        "code": 200,
        "data": build_backend_submission_payload(FIELDS, "com.demo", "rain"),
    }
    session.post.return_value = response
    cache_response = MagicMock()
    cache_response.json.return_value = {
        "code": 500,
        "message": "failed",
        "data": 0,
    }
    session.get.return_value = cache_response

    result = submit_backend_via_api(
        FIELDS,
        "com.demo",
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        session=session,
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_CACHE_CLEAR_REJECTED"
    assert "已提交" in result["message"]


def test_api_submit_blocks_replay_when_readback_fields_do_not_match():
    session = MagicMock()
    payload = build_backend_submission_payload(FIELDS, "com.demo", "rain")
    submit_response = MagicMock()
    submit_response.json.return_value = {"code": 200, "data": payload}
    wrong_record = {**payload, "aggr_jilishipin_id": "stale-reward-id"}
    readback_response = MagicMock()
    readback_response.json.return_value = {
        "code": 200,
        "data": {"code": 200, "data": [wrong_record], "total": 1},
    }
    session.post.side_effect = [
        submit_response,
        readback_response,
        readback_response,
        readback_response,
    ]
    cache_response = MagicMock()
    cache_response.json.return_value = {
        "code": 200,
        "message": "success",
        "data": 1,
    }
    session.get.return_value = cache_response

    result = submit_backend_via_api(
        FIELDS,
        "com.demo",
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        readback_delay_seconds=0,
        session=session,
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_READBACK_MISMATCH"
    assert result["mismatches"] == ["aggr_jilishipin_id"]
    assert session.post.call_count == 4


def test_placeholder_ad_ids_are_not_treated_as_real_parameters():
    errors = validate_backend_fields(
        {
            "最终判断": "MAX聚合",
            "初始Activity": "Main",
            "插屏聚合id": "未找到",
            "激励视频聚合id": "N/A",
        },
        "com.demo",
    )

    assert "插屏和激励视频聚合 ID 均为空" in errors


def test_chrome_script_contains_url_and_submit_action():
    script = build_chrome_submit_script(
        "http://example.test/?a=1",
        wait_seconds=20,
        expected_package="com.demo",
    )

    assert "http://example.test/?a=1" in script
    assert "提交" in script
    assert "submitted" in script
    assert "make new tab" in script
    assert "execute targetTab javascript" in script
    assert "com.demo" in script
    assert "set URL of active tab" not in script


@patch("automation_adaptation.subprocess.Popen")
@patch("automation_adaptation.subprocess.run")
def test_auto_submit_reports_success(mock_run, mock_popen):
    mock_run.return_value = MagicMock(stdout="Darwin\n", returncode=0)
    process = MagicMock()
    process.poll.return_value = 0
    process.communicate.return_value = ("submitted\n", "")
    process.returncode = 0
    process.args = ["osascript"]
    mock_popen.return_value = process

    result = auto_submit_backend_url(FIELDS, "com.demo", wait_seconds=20)

    assert result["ok"] is True
    assert result["code"] == "BACKEND_SUBMITTED"


@patch("automation_adaptation.subprocess.Popen")
@patch("automation_adaptation.subprocess.run")
def test_auto_submit_can_be_stopped(mock_run, mock_popen):
    import threading

    mock_run.return_value = MagicMock(stdout="Darwin\n", returncode=0)
    process = MagicMock()
    process.poll.return_value = None
    process.wait.return_value = 0
    mock_popen.return_value = process
    stopped = threading.Event()
    stopped.set()

    result = auto_submit_backend_url(
        FIELDS, "com.demo", wait_seconds=20, stop_event=stopped
    )

    assert result["code"] == "USER_STOPPED"
    process.terminate.assert_called_once()
