from datetime import date
from unittest.mock import MagicMock

import pytest

from daily_summary import (
    DailyComment,
    classify_task_comments,
    classify_issue_reason,
    comments_for_date,
    concise_issue_reason,
    generate_daily_asana_summary,
    render_daily_summary,
    summarize_issue_reasons,
)


def test_comments_are_filtered_in_shanghai_timezone():
    stories = [
        {
            "text": "聚合适配通过",
            "resource_subtype": "comment_added",
            "type": "comment",
            "created_at": "2026-08-02T16:30:00Z",
        },
        {
            "text": "旧评论",
            "resource_subtype": "comment_added",
            "type": "comment",
            "created_at": "2026-08-02T10:00:00Z",
        },
    ]

    result = comments_for_date(stories, date(2026, 8, 3))

    assert [item.text for item in result] == ["聚合适配通过"]


def test_last_terminal_aggregation_comment_wins():
    result = classify_task_comments(
        "com.demo",
        [
            DailyComment("聚合适配通过", "2026-08-03T01:00:00Z"),
            DailyComment("包体闪退，暂不适配", "2026-08-03T02:00:00Z"),
            DailyComment("动作适配通过", "2026-08-03T03:00:00Z"),
        ],
    )

    assert result == {
        "aggregation_state": "not_adapted",
        "aggregation_reason": "包体闪退，暂不适配",
        "action_success": "1",
        "action_issue": "",
        "action_package_name": "",
    }


def test_action_only_failure_is_not_counted_as_aggregation_issue():
    result = classify_task_comments(
        "com.demo",
        [DailyComment("动作适配难度较高，暂不适配")],
    )

    assert result["aggregation_state"] == ""


def test_action_success_wording_is_counted():
    result = classify_task_comments(
        "com.arrow.romantic.puzzle.goddess",
        [DailyComment("动作适配成功")],
    )

    assert result["action_success"] == "1"


def test_action_issue_wording_is_counted_and_keeps_full_reason():
    result = classify_task_comments(
        "com.colorwater.sort",
        [
            DailyComment(
                "com.colorwater.sort动作适配失败，动作适配度动作已录制，"
                "但是回放不成功，且不产生任何日志"
            )
        ],
    )

    assert result["action_success"] == ""
    assert result["action_issue"] == (
        "动作适配失败，动作适配度动作已录制，但是回放不成功，且不产生任何日志"
    )
    assert result["action_package_name"] == "com.colorwater.sort"


def test_latest_action_conclusion_wins():
    recovered = classify_task_comments(
        "com.demo",
        [
            DailyComment("动作适配异常，点击无反应", "2026-08-31T01:00:00Z"),
            DailyComment("动作适配成功", "2026-08-31T02:00:00Z"),
        ],
    )
    regressed = classify_task_comments(
        "com.demo",
        [
            DailyComment("动作适配成功", "2026-08-31T01:00:00Z"),
            DailyComment("动作适配回放失败", "2026-08-31T02:00:00Z"),
        ],
    )

    assert recovered["action_success"] == "1"
    assert recovered["action_issue"] == ""
    assert regressed["action_success"] == ""
    assert regressed["action_issue"] == "动作适配回放失败"


def test_action_issue_uses_explicit_legacy_package_prefix_from_comment():
    result = classify_task_comments(
        "com.pawdoku.puzzle.game",
        [
            DailyComment(
                "com.pawdoku.p动作适配已录制，但是适配涉及快速连点的方法，"
                "该方法疑似代码未合并，目前无法通过的动作适配"
            )
        ],
    )

    assert result["action_package_name"] == "com.pawdoku.p"
    assert result["action_issue"].startswith("动作适配已录制")


def test_aggregation_success_wording_is_counted():
    result = classify_task_comments(
        "com.example.game",
        [DailyComment("聚合适配成功")],
    )

    assert result["aggregation_state"] == "success"


def test_no_ads_review_comment_is_not_counted_as_blacklist():
    result = classify_task_comments(
        "com.review.game",
        [
            DailyComment(
                "【APK Tool 页面预检：NO_ADS_OR_IAP】\n"
                "未发现广告或应用内购标识，继续下载并人工确认（不加黑）\n"
                "识别说明：不能据此加黑，将继续下载安装，交由人工确认。"
            )
        ],
    )

    assert result["aggregation_state"] == ""


def test_structured_automation_comment_extracts_terminal_reason():
    text = (
        "【APK Tool 自动化适配：UNSUPPORTED_ATTRIBUTION】\n"
        "Singular归因，暂不适配\n"
        "包名：com.demo"
    )

    assert concise_issue_reason(text, "com.demo") == "Singular归因，暂不适配"


@pytest.mark.parametrize(
    ("code", "message", "expected_reason"),
    [
        ("AF_KEY_EMPTY", "af_key为空，再次确认", "af_key为空，暂不适配"),
        (
            "LOGCAT_ENDED",
            "聚合广告回放失败，失败原因：Logcat 监听进程已提前结束",
            "聚合回放失败（Logcat监听提前结束），暂不适配",
        ),
        (
            "AUTOMATION_FAILED",
            "自动化执行失败: unexpected keyword argument 'status'",
            "自动化适配失败，暂不适配",
        ),
    ],
)
def test_structured_terminal_failures_are_counted_without_legacy_wording(
    code, message, expected_reason
):
    result = classify_task_comments(
        "com.demo",
        [DailyComment(f"【APK Tool 自动化适配：{code}】\n{message}")],
    )

    assert result["aggregation_state"] == "not_adapted"
    assert result["aggregation_reason"] == expected_reason


def test_later_success_supersedes_structured_terminal_failure():
    result = classify_task_comments(
        "com.demo",
        [
            DailyComment(
                "【APK Tool 自动化适配：LOGCAT_ENDED】\nLogcat 监听进程已提前结束",
                "2026-09-02T01:00:00Z",
            ),
            DailyComment("聚合适配成功", "2026-09-02T02:00:00Z"),
        ],
    )

    assert result["aggregation_state"] == "success"
    assert result["aggregation_reason"] == ""


def test_skip_adaptation_wording_is_normalized_for_daily_report():
    result = classify_task_comments(
        "com.demo", [DailyComment("当前设备暂不支持此应用，跳过适配")]
    )

    assert result["aggregation_reason"] == "当前设备暂不支持此应用，暂不适配"


def test_render_matches_requested_layout():
    text = render_daily_summary(
        date(2026, 8, 3),
        ["com.success"],
        [
            {
                "package_name": "com.crash",
                "state": "not_adapted",
                "reason": "包体闪退，暂不适配",
            },
            {
                "package_name": "com.black",
                "state": "blacklist",
                "reason": "应用内购，无广告，加黑",
            },
        ],
        ["com.success"],
    )

    assert text == (
        "【8.3】\n"
        "完成1个聚合适配\n"
        "com.success\n\n"
        "2个聚合适配问题:1个暂不适配，其中1个包体闪退或运行异常；"
        "1个加黑，其中1个应用内购无广告\n\n"
        "com.crash：包体闪退，暂不适配\n"
        "com.black：应用内购，无广告，加黑\n\n\n"
        "完成1个动作适配\n"
        "com.success\n"
    )


def test_render_includes_action_issue_section_after_action_successes():
    text = render_daily_summary(
        date(2026, 8, 31),
        [],
        [],
        ["com.action.success"],
        [
            {
                "package_name": "puzzle.meow.and.cat",
                "reason": "动作适配异常，点击事件无反应，暂不适配，已给panda看过",
            },
            {
                "package_name": "com.colorwater.sort",
                "reason": "动作适配失败，动作已录制，但是回放不成功，且不产生任何日志",
            },
        ],
    )

    assert text.endswith(
        "完成1个动作适配\n"
        "com.action.success\n\n\n"
        "2个动作适配问题\n"
        "puzzle.meow.and.cat动作适配异常，点击事件无反应，暂不适配，已给panda看过\n"
        "com.colorwater.sort动作适配失败，动作已录制，但是回放不成功，且不产生任何日志\n"
    )


def test_issue_reasons_are_exclusive_and_grouped_by_terminal_state():
    issues = [
        {
            "package_name": "com.attr",
            "state": "not_adapted",
            "reason": "归因平台:Singular，暂不适配",
        },
        {
            "package_name": "com.empty",
            "state": "not_adapted",
            "reason": "未识别出聚合类型和广告id，暂不适配",
        },
        {
            "package_name": "com.replay",
            "state": "not_adapted",
            "reason": "已配置id，但聚合回放失败，未播放广告",
        },
        {
            "package_name": "com.login",
            "state": "blacklist",
            "reason": "需要google登陆，加黑",
        },
        {
            "package_name": "com.iap",
            "state": "blacklist",
            "reason": "应用内购，无广告，加黑",
        },
    ]

    assert classify_issue_reason(issues[2]["reason"]) == "replay_failed"
    assert summarize_issue_reasons(issues, "not_adapted") == [
        {"key": "replay_failed", "label": "聚合回放失败", "count": 1},
        {"key": "attribution", "label": "归因问题", "count": 1},
        {
            "key": "aggregation_missing",
            "label": "未识别聚合类型或广告ID",
            "count": 1,
        },
    ]
    assert summarize_issue_reasons(issues, "blacklist") == [
        {"key": "google_login", "label": "需Google登录", "count": 1},
        {"key": "iap_only", "label": "应用内购无广告", "count": 1},
    ]


def test_empty_ad_ids_and_af_key_are_separate_summary_categories():
    issues = [
        {
            "package_name": "com.empty.ids",
            "state": "not_adapted",
            "reason": "插屏聚合id和激励视频聚合id均为空，暂不适配",
        },
        {
            "package_name": "com.empty.af",
            "state": "not_adapted",
            "reason": "af_key为空，暂不适配",
        },
    ]

    assert summarize_issue_reasons(issues, "not_adapted") == [
        {
            "key": "both_ad_ids_empty",
            "label": "插屏聚合id和激励视频聚合id均为空",
            "count": 1,
        },
        {"key": "af_key_missing", "label": "af_key为空缺失", "count": 1},
    ]


def test_account_login_blacklist_is_counted_not_mistaken_for_negation():
    result = classify_task_comments(
        "co.vybs.app",
        [DailyComment("需要账号登录，不做适配，加黑")],
    )

    assert result["aggregation_state"] == "blacklist"
    assert result["aggregation_reason"] == "需要账号登录，不做适配，加黑"
    assert classify_issue_reason(result["aggregation_reason"]) == "account_login"


def test_report_counts_google_login_account_login_and_iap_separately():
    issues = [
        {"package_name": "a", "state": "blacklist", "reason": "需要google登录，加黑"},
        {"package_name": "b", "state": "blacklist", "reason": "需要账号登录，不做适配，加黑"},
        {"package_name": "c", "state": "blacklist", "reason": "应用内购，无广告，加黑"},
    ]

    assert summarize_issue_reasons(issues, "blacklist") == [
        {"key": "google_login", "label": "需Google登录", "count": 1},
        {"key": "account_login", "label": "需账号登录", "count": 1},
        {"key": "iap_only", "label": "应用内购无广告", "count": 1},
    ]


def test_issue_summary_splits_tradplus_slow_download_and_unknown_reasons():
    issues = [
        {
            "package_name": "com.trad.one",
            "state": "not_adapted",
            "reason": "检测出TradPlus聚合，暂不适配",
        },
        {
            "package_name": "com.trad.two",
            "state": "not_adapted",
            "reason": "TradPlus聚合，暂不适配",
        },
        {
            "package_name": "com.slow",
            "state": "blacklist",
            "reason": "下载前置资源包时间太长，需要10分钟，加黑",
        },
        {
            "package_name": "com.custom",
            "state": "not_adapted",
            "reason": "需要人工破解登录流程，暂不适配",
        },
    ]

    assert summarize_issue_reasons(issues, "not_adapted") == [
        {"key": "tradplus", "label": "TradPlus聚合", "count": 2},
        {
            "key": "other:需要人工破解登录流程",
            "label": "需要人工破解登录流程",
            "count": 1,
        },
    ]
    assert summarize_issue_reasons(issues, "blacklist") == [
        {
            "key": "resource_download_slow",
            "label": "前置资源下载耗时过长",
            "count": 1,
        }
    ]


def test_concise_issue_reason_separates_package_and_unsupported_mediation():
    assert concise_issue_reason(
        "com.apparmor.aparmr.qwert.yuiopTradPlus聚合，暂不适配",
        "com.apparmor.aparmr.qwert.yuiop",
    ) == "检测出TradPlus聚合，暂不适配"


def test_missing_aggregation_wording_variants_are_classified():
    assert classify_issue_reason("未检测出聚合类型和聚合id，暂不适配") == (
        "aggregation_missing"
    )
    assert classify_issue_reason("未识别到聚合类型，暂不适配") == (
        "aggregation_missing"
    )
    assert classify_issue_reason("有聚合类型，但是没有广告id，暂不适配") == (
        "aggregation_missing"
    )
    assert classify_issue_reason(
        "未检测出聚合类型和聚合id，疑似白包，暂不适配"
    ) == "white_package"


def test_generate_reads_date_section_and_comments():
    client = MagicMock()
    client.sections.get_sections_for_project.return_value = [
        {"gid": "section-1", "name": "8.3执行"}
    ]
    client.tasks.get_tasks_for_section.return_value = [
        {
            "gid": "task-1",
            "name": "聚合/动作适配com.demo",
            "notes": "包名：com.demo",
            "completed": False,
        }
    ]
    client.stories.get_stories_for_task.return_value = [
        {
            "text": "聚合适配通过",
            "resource_subtype": "comment_added",
            "type": "comment",
            "created_at": "2026-08-03T03:00:00Z",
        },
        {
            "text": "动作适配通过",
            "resource_subtype": "comment_added",
            "type": "comment",
            "created_at": "2026-08-03T04:00:00Z",
        },
    ]

    result = generate_daily_asana_summary(
        client, "project-1", date(2026, 8, 3)
    )

    assert result["aggregation_success_count"] == 1
    assert result["action_success_count"] == 1
    assert result["action_issue_count"] == 0
    assert "com.demo" in result["text"]
