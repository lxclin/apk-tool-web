import io
from unittest.mock import MagicMock, patch

import pytest

from ad_replay import (
    AdReplayEvaluator,
    ReplayExpectation,
    build_replay_failure_comment,
    is_replay_diagnostic_line,
    run_ad_replay_check,
    split_ad_unit_ids,
    validate_replay_timeout,
)


def test_split_ad_unit_ids_accepts_chinese_commas_and_deduplicates():
    assert split_ad_unit_ids("first, second，first\nthird") == (
        "first",
        "second",
        "third",
    )


def test_split_ad_unit_ids_omits_detector_placeholders():
    assert split_ad_unit_ids("未找到, reward-1, N/A") == ("reward-1",)


def test_replay_timeout_defaults_allow_500_seconds():
    assert validate_replay_timeout("500") == 500
    with pytest.raises(ValueError):
        validate_replay_timeout("5")


def test_load_and_show_requests_do_not_count_as_display():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("inter-1", ""))

    evaluator.feed(
        'ZGSDK.mediationEvent: {"status":"load_success","adUnitId":"inter-1",'
        '"adType":"interstitial","session_id":"s-1"}'
    )
    evaluator.feed("ZGSDK.Max: showInterAd showAd adUnitId=inter-1 adType=INTERSTITIAL")

    assert evaluator.complete is False
    assert evaluator.states["interstitial"].displayed is False


def test_matching_interstitial_display_success_completes_single_type():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("inter-1", ""))

    assert evaluator.feed(
        'ZGSDK.mediationEvent: {"status":"display_success","platform":"MAX",'
        '"adUnitId":"inter-1","adType":"interstitial","session_id":"s-1"}'
    )

    result = evaluator.result()
    assert result["code"] == "AGGREGATION_REPLAY_SUCCESS"
    assert result["interstitial"]["displayed"] is True
    assert result["rewarded"]["required"] is False


def test_both_configured_types_must_display_before_aggregation_succeeds():
    evaluator = AdReplayEvaluator(
        ReplayExpectation.from_values("inter-1", "reward-1")
    )

    assert not evaluator.feed(
        "ZGSDK.scheduledAds: onAdDisplayed unifiedAd = MaxUnifiedAd{"
        "adUnitId='inter-1', adType=INTERSTITIAL}"
    )
    assert evaluator.feed(
        "ZGSDK.scheduledAds: onAdDisplayed unifiedAd = MaxUnifiedAd{"
        "adUnitId='reward-1', adType=REWARD_VIDEO}"
    )

    assert evaluator.states["interstitial"].displayed is True
    assert evaluator.states["rewarded"].displayed is True


def test_wrong_ad_unit_id_is_not_accepted():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("expected", ""))

    evaluator.feed(
        "ZGSDK.scheduledAds: onAdDisplayed unifiedAd = MaxUnifiedAd{"
        "adUnitId='some-old-id', adType=INTERSTITIAL}"
    )

    assert evaluator.complete is False


def test_automedia_detect_replayed_callback_is_ignored():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("inter-1", ""))

    evaluator.feed(
        "ZGSDK.AutoMediaDetect: MaxInterstitialAd: "
        "MaxAdListener.onAdDisplayed(adUnitId=inter-1, format=INTER)"
    )

    assert evaluator.complete is False


def test_revenue_callback_is_strong_display_and_revenue_evidence():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("", "reward-1"))

    evaluator.feed(
        "ZGSDK.scheduledAds: onAdRevenuePaid unifiedAd = MaxUnifiedAd{"
        "adUnitId='reward-1', revenue=0.001, adType=REWARD_VIDEO}"
    )

    state = evaluator.states["rewarded"]
    assert state.displayed is True
    assert state.revenue_reported is True


def test_error_508_is_recorded_but_waits_for_possible_later_success():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("", "reward-1"))

    assert not evaluator.feed(
        'IronSource Error 508 init() must be called before show '
        'adUnitId=reward-1 adType=reward'
    )

    assert "Error 508" in evaluator.states["rewarded"].errors[0]


def test_action_success_pattern_can_finish_replay_independently():
    evaluator = AdReplayEvaluator(
        ReplayExpectation.from_values("inter-1", "reward-1"),
        action_success_patterns=[r"ACTION_AD_PLAYED"],
    )

    assert evaluator.feed("Custom.Action: ACTION_AD_PLAYED")
    assert evaluator.result()["code"] == "ACTION_REPLAY_SUCCESS"


def test_timeout_comment_marks_missing_type_and_not_configured_type():
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values("inter-1", ""))
    result = evaluator.result(timed_out=True, elapsed_seconds=200)

    comment = build_replay_failure_comment("com.demo", result)

    assert "AD_REPLAY_FAILED" in comment
    assert "包名：com.demo" in comment
    assert "插屏广告：未检测到真实展示" in comment
    assert "激励视频：未配置，不要求验证" in comment


def test_replay_comment_reports_runtime_crash_evidence():
    result = {
        "ok": False,
        "code": "APP_CRASHED",
        "message": "包体在自动化检测过程中闪退，暂不适配",
        "elapsed_seconds": 37,
        "runtime": {"summary": "FATAL EXCEPTION: main"},
        "interstitial": {"required": True, "displayed": False, "errors": []},
        "rewarded": {"required": False, "displayed": False, "errors": []},
    }

    comment = build_replay_failure_comment("com.demo", result)

    assert "自动化检测过程中闪退" in comment
    assert "FATAL EXCEPTION: main" in comment


def test_replay_comment_does_not_call_unconfirmed_launch_a_crash():
    result = {
        "ok": False,
        "code": "APP_LAUNCH_NOT_CONFIRMED",
        "message": "启动应用后 10 秒内未检测到目标进程，无法进行自动化检测",
        "elapsed_seconds": 10,
        "runtime": {},
        "interstitial": {"required": True, "displayed": False, "errors": []},
        "rewarded": {"required": False, "displayed": False, "errors": []},
    }

    comment = build_replay_failure_comment("com.demo", result)

    assert "未检测到目标进程" in comment
    assert "闪退" not in comment


def test_gui_log_filter_keeps_replay_events_and_drops_verbose_sdk_noise():
    expectation = ReplayExpectation.from_values("inter-1", "reward-1")

    assert not is_replay_diagnostic_line(
        "AppLovinSdk: Signal collection successful for random adapter", expectation
    )
    assert is_replay_diagnostic_line(
        'ZGSDK.mediationEvent: {"status":"display_success",'
        '"adUnitId":"inter-1","adType":"interstitial"}',
        expectation,
    )
    assert not is_replay_diagnostic_line(
        'ZGSDK.mediationEvent: {"status":"display_success",'
        '"adUnitId":"old-id","adType":"interstitial"}',
        expectation,
    )
    assert not is_replay_diagnostic_line(
        "AppLovinSdk: rewarded ad failed with No Fill for reward-1",
        expectation,
    )
    assert is_replay_diagnostic_line(
        'ZGSDK.mediationEvent: {"status":"load_failed",'
        '"adUnitId":"reward-1","adType":"reward"}',
        expectation,
    )


def test_platform_log_filter_matches_android_studio_and_query():
    max_expectation = ReplayExpectation.from_values(
        "inter-1", "reward-1", "MAX聚合（自动化检测确认）"
    )
    assert max_expectation.platform_log_token == "max"
    assert is_replay_diagnostic_line(
        "ZGSDK.Max: showRewardAd loadAd", max_expectation
    )
    assert not is_replay_diagnostic_line(
        "ZGSDK.LevelPlay: showRewardAd loadAd", max_expectation
    )
    assert not is_replay_diagnostic_line(
        "AppLovinSdk: MAX waterfall No Fill", max_expectation
    )


def test_platform_log_filter_mapping_for_supported_mediation_types():
    assert ReplayExpectation.from_values("a", "", "LevelPlay聚合").platform_log_token == "level"
    assert ReplayExpectation.from_values("a", "", "IronSource聚合").platform_log_token == "iron"
    assert ReplayExpectation.from_values("a", "", "AdMob聚合").platform_log_token == "admob"


def test_in_flight_attempt_tracks_matching_session_until_terminal_status():
    evaluator = AdReplayEvaluator(
        ReplayExpectation.from_values("inter-1", "reward-1")
    )

    evaluator.feed(
        'ZGSDK.mediationEvent: {"status":"load_start",'
        '"adUnitId":"reward-1","adType":"reward","session_id":"s-1"}'
    )
    assert evaluator.has_in_flight_attempt is True

    evaluator.feed(
        'ZGSDK.mediationEvent: {"status":"load_failed",'
        '"adUnitId":"reward-1","adType":"reward","session_id":"s-1"}'
    )
    assert evaluator.has_in_flight_attempt is False


@patch("ad_replay.stop_logcat_stream")
@patch("ad_replay.subprocess.run")
@patch("ad_replay.force_stop_app", return_value=(True, "ok"))
@patch("ad_replay.PackageRuntimeMonitor")
@patch("ad_replay.start_logcat_stream")
def test_high_volume_logcat_is_drained_before_success_is_evaluated(
    start_stream, runtime_monitor, _force_stop, launch, _stop_stream
):
    noise = "".join(
        f"AppLovinSdk: verbose adapter initialization line {index}\n"
        for index in range(5000)
    )
    success = (
        'ZGSDK.mediationEvent: mediation_call_status extra = '
        '{"status":"display_success","platform":"MAX",'
        '"adUnitId":"inter-1","adType":"interstitial",'
        '"session_id":"s-1"}\n'
    )
    proc = MagicMock()
    proc.stdout = io.StringIO(noise + success)
    proc.poll.return_value = None
    start_stream.return_value = proc
    runtime_monitor.return_value.poll.return_value = {"ok": True}
    launch.return_value = MagicMock(returncode=0, stdout="", stderr="")
    displayed = []
    dismiss_dialog = MagicMock(
        return_value={
            "dismissed": True,
            "message": "已自动点击通知权限“不允许”，继续聚合适配",
        }
    )
    progress = []

    result = run_ad_replay_check(
        "com.demo",
        "10250",
        ReplayExpectation.from_values("inter-1", ""),
        timeout_seconds=10,
        on_line=displayed.append,
        on_progress=progress.append,
        dismiss_interrupting_dialog=dismiss_dialog,
    )

    assert result["ok"] is True
    assert result["code"] == "AGGREGATION_REPLAY_SUCCESS"
    assert result["interstitial"]["displayed"] is True
    runtime_monitor.return_value.reset.assert_called_once_with()
    assert len(displayed) == 1
    assert "display_success" in displayed[0]
    dismiss_dialog.assert_called_once_with()
    assert any("通知权限" in message for message in progress)


@patch("ad_replay.stop_logcat_stream")
@patch("ad_replay.subprocess.run")
@patch("ad_replay.force_stop_app", return_value=(True, "ok"))
@patch("ad_replay.PackageRuntimeMonitor")
@patch("ad_replay.start_logcat_stream")
def test_replay_returns_explicit_max_autodetector_upgrade_from_same_uid(
    start_stream, runtime_monitor, _force_stop, launch, _stop_stream
):
    logs = "\n".join(
        [
            "ZGSDK.Max: showRewardAd loadAd",
            "ZGSDK.AutoDetector: === 综合检测结果 ===",
            "ZGSDK.AutoDetector: 最终判断: MAX聚合（强关系证据确认）",
            "ZGSDK.AutoDetector: === 详细检测结果 ===",
            "ZGSDK.AutoDetector: 包名: com.demo",
            "ZGSDK.AutoDetector: 初始页面Activity: com.demo.MainActivity",
            "ZGSDK.AutoDetector: 应用类型: Unity",
            "ZGSDK.AutoDetector: AppLovin:",
            "ZGSDK.AutoDetector:   SDK Key: max-sdk-key",
            "ZGSDK.AutoDetector:   激励视频聚合id: reward-max",
            "ZGSDK.AutoDetector:   插屏聚合id: inter-max",
            "ZGSDK.AutoDetector: 归因平台: Adjust",
        ]
    ) + "\n"
    proc = MagicMock()
    proc.stdout = io.StringIO(logs)
    proc.poll.return_value = None
    start_stream.return_value = proc
    runtime_monitor.return_value.poll.return_value = {"ok": True}
    launch.return_value = MagicMock(returncode=0, stdout="", stderr="")

    def is_authoritative_max(fields):
        return bool(
            "max" in fields.get("最终判断", "").casefold()
            and fields.get("激励视频聚合id") == "reward-max"
            and fields.get("插屏聚合id") == "inter-max"
        )

    result = run_ad_replay_check(
        "com.demo",
        "10250",
        ReplayExpectation.from_values("inter", "video", "IronSource聚合"),
        timeout_seconds=10,
        aggregation_change_detector=is_authoritative_max,
    )

    assert result["ok"] is False
    assert result["code"] == "AGGREGATION_TYPE_CHANGED_DURING_REPLAY"
    assert result["detected_fields"]["最终判断"].startswith("MAX聚合")
    assert result["detected_fields"]["激励视频聚合id"] == "reward-max"
    assert result["detected_fields"]["插屏聚合id"] == "inter-max"


@patch("ad_replay.stop_logcat_stream")
@patch("ad_replay.subprocess.run")
@patch("ad_replay.force_stop_app", return_value=(True, "ok"))
@patch("ad_replay.PackageRuntimeMonitor")
@patch("ad_replay.start_logcat_stream")
def test_replay_does_not_upgrade_on_generic_max_sdk_logs(
    start_stream, runtime_monitor, _force_stop, launch, _stop_stream
):
    proc = MagicMock()
    proc.stdout = io.StringIO(
        "ZGSDK.Max: showRewardAd loadAd\n"
        'ZGSDK.mediationEvent: {"status":"display_success",'
        '"platform":"IRONSOURCE","adUnitId":"inter",'
        '"adType":"interstitial"}\n'
    )
    proc.poll.return_value = None
    start_stream.return_value = proc
    runtime_monitor.return_value.poll.return_value = {"ok": True}
    launch.return_value = MagicMock(returncode=0, stdout="", stderr="")

    result = run_ad_replay_check(
        "com.demo",
        "10250",
        ReplayExpectation.from_values("inter", "", "IronSource聚合"),
        timeout_seconds=10,
        aggregation_change_detector=lambda fields: bool(fields.get("最终判断")),
        aggregation_change_grace_seconds=0,
    )

    assert result["ok"] is True
    assert result["code"] == "AGGREGATION_REPLAY_SUCCESS"
