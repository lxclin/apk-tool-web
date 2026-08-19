from datetime import date
from unittest.mock import MagicMock

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
        "完成1个聚合动作适配结果\n"
        "com.success\n\n"
        "2个聚合适配问题:1个暂不适配，其中1个包体闪退或运行异常；"
        "1个加黑，其中1个应用内购无广告\n\n"
        "com.crash：包体闪退，暂不适配\n"
        "com.black：应用内购，无广告，加黑\n\n\n"
        "完成1个动作适配\n"
        "com.success\n"
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
    assert "com.demo" in result["text"]
