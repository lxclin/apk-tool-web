"""
test_sync.py — Google Sheets → Asana 幂等同步 单元测试

采用 pytest + unittest.mock，拦截所有对 Google Sheets / Asana 的真实 HTTP 请求。
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import date

from auto_asana.main import (
    PackageRow,
    AsanaTaskInfo,
    AsanaPrecheckTask,
    SheetCellUpdate,
    SheetRowUpdate,
    build_task_name,
    build_asana_task_notes,
    merge_asana_task_notes,
    task_descriptions_from_package_rows,
    task_descriptions_from_cp_records,
    unique_task_names_from_package_rows,
    build_asana_task_link,
    column_index_to_a1,
    quote_sheet_name,
    build_cell_range,
    build_row_range,
    parse_up2_appid,
    extract_up2_appid,
    build_cp_adapt_request_payload,
    build_cp_adapt_sheet_values,
    plan_cp_adapt_sheet_upserts,
    get_task_link_column_index,
    plan_task_link_backfills,
    update_sheet_value,
    update_sheet_row,
    batch_update_sheet_values,
    backfill_task_links,
    apply_sheet_row_updates,
    GS_SCOPES,
    generate_target_dates,
    filter_packages,
    compute_diff,
    get_sheet_data,
    find_or_create_section,
    get_existing_task_names,
    get_existing_tasks_by_name,
    get_asana_tasks_for_date,
    parse_asana_precheck_task,
    build_precheck_asana_comment,
    add_precheck_comment_once,
    classify_precheck_workflow_status,
    create_tasks_for_packages,
    update_task_notes_for_packages,
    _SectionsFacade,
    _StoriesFacade,
    _TasksFacade,
    _build_gs_service,
    sync_packages,
)


# ═══════════════════════════════════════════════════════════════
# TestGenerateTargetDates — 日期格式化
# ═══════════════════════════════════════════════════════════════

class TestGenerateTargetDates:
    """验证日期生成逻辑：yy.m.d 和 m.d执行 格式，个位数不补零。"""

    def test_normal_date(self):
        """2026-06-08 → sheet '26.6.8' / asana '6.8执行'"""
        sheet_date, asana_name = generate_target_dates(date(2026, 6, 8))
        assert sheet_date == "26.6.8"
        assert asana_name == "6.8执行"

    def test_single_digit_month_day_no_zero_pad(self):
        """个位数月/日不补零：1月5日 → '26.1.5' / '1.5执行'"""
        sheet_date, asana_name = generate_target_dates(date(2026, 1, 5))
        assert sheet_date == "26.1.5"
        assert asana_name == "1.5执行"

    def test_double_digit_month_day(self):
        """双位数月/日保持原样：12月31日 → '26.12.31' / '12.31执行'"""
        sheet_date, asana_name = generate_target_dates(date(2026, 12, 31))
        assert sheet_date == "26.12.31"
        assert asana_name == "12.31执行"

    def test_cross_year_boundary(self):
        """跨年：2027-01-01 → '27.1.1' / '1.1执行'"""
        sheet_date, asana_name = generate_target_dates(date(2027, 1, 1))
        assert sheet_date == "27.1.1"
        assert asana_name == "1.1执行"

    def test_default_uses_today(self):
        """不传参数时使用当天日期，格式正确即可。"""
        sheet_date, asana_name = generate_target_dates()
        assert "执行" in asana_name
        # 确保返回的是合理格式
        parts = sheet_date.split(".")
        assert len(parts) == 3
        assert len(parts[0]) == 2  # yy


class TestAsanaPrecheckTasks:
    def test_apkcombo_available_comment_is_actionable_not_terminal(self):
        comment = (
            "【APK Tool 页面预检：APKCOMBO_AVAILABLE】\n"
            "Google Play 无法下载，但 APKCombo 存在包体，等待第三方下载"
        )

        assert classify_precheck_workflow_status([{"text": comment}]) == (
            "APKCombo有包",
            False,
        )

    def test_historical_install_failure_migrates_to_apkcombo_retry(self):
        comment = (
            "【APK Tool 页面预检：INSTALL_FAILED】\n"
            "自动下载安装失败，需要人工确认"
        )

        assert classify_precheck_workflow_status([{"text": comment}]) == (
            "APKCombo有包",
            False,
        )

    def test_latest_comment_restores_business_status(self):
        stories = [
            {
                "text": "应用内购，无广告，加黑",
                "resource_subtype": "comment_added",
                "created_at": "2026-08-12T01:00:00Z",
            },
            {
                "text": "聚合适配完成",
                "resource_subtype": "comment_added",
                "created_at": "2026-08-12T02:00:00Z",
            },
        ]

        assert classify_precheck_workflow_status(stories) == (
            "聚合适配成功",
            True,
        )

    @pytest.mark.parametrize(
        ("comment", "expected"),
        [
            ("【APK Tool 页面预检：APP_CRASHED】\n包体闪退，暂不适配", "包体闪退"),
            ("Singular归因，暂不适配", "归因暂不适配"),
            ("【APK Tool 自动化适配：AF_KEY_EMPTY】\naf_key为空", "参数待确认"),
            ("【APK Tool 自动化适配：AD_REPLAY_FAILED】\n未确认广告展示", "回放失败"),
            (
                "【APK Tool 自动化适配：PRECHECK_BLACKLIST_CACHE_FAILED】\n刷新缓存失败",
                "加黑缓存刷新失败",
            ),
        ],
    )
    def test_comment_status_categories(self, comment, expected):
        assert classify_precheck_workflow_status([{"text": comment}]) == (
            expected,
            True,
        )

    def test_parses_fields_from_task_notes(self):
        task = parse_asana_precheck_task({
            "gid": "task-1",
            "name": "聚合/动作适配com.example.game",
            "notes": (
                "包名：com.example.game\n"
                "UP2 appid：up2-123\n"
                "GP链接：https://play.google.com/store/apps/details?id=com.example.game"
            ),
            "completed": False,
        })

        assert isinstance(task, AsanaPrecheckTask)
        assert task.package_name == "com.example.game"
        assert task.up2_appid == "up2-123"
        assert task.gp_link.endswith("id=com.example.game")

    def test_parses_markdown_google_play_link(self):
        task = parse_asana_precheck_task({
            "gid": "task-1",
            "name": "聚合/动作适配com.example.game",
            "notes": (
                "GP链接： [https://play.google.com/store/apps/details?id=com.example.game]"
                "(https://play.google.com/store/apps/details?id=com.example.game)"
            ),
        })

        assert task.gp_link == (
            "https://play.google.com/store/apps/details?id=com.example.game"
        )

    def test_reads_only_section_matching_today_and_preserves_order(self):
        client = MagicMock()
        client.sections.get_sections_for_project.return_value = [
            {"gid": "old", "name": "7.29执行"},
            {"gid": "today", "name": "7.30执行"},
            {"gid": "future", "name": "7.31执行"},
        ]
        client.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "2",
                "name": "聚合/动作适配com.second.game",
                "notes": "包名：com.second.game",
            },
            {
                "gid": "1",
                "name": "聚合/动作适配com.first.game",
                "notes": "包名：com.first.game",
            },
        ]

        result = get_asana_tasks_for_date(
            client,
            "project-1",
            today=date(2026, 7, 30),
        )

        assert result["section_name"] == "7.30执行"
        assert result["section_gid"] == "today"
        assert [task.package_name for task in result["tasks"]] == [
            "com.second.game",
            "com.first.game",
        ]
        client.tasks.get_tasks_for_section.assert_called_once()
        assert client.tasks.get_tasks_for_section.call_args.args[0] == "today"
        assert "created_at" in (
            client.tasks.get_tasks_for_section.call_args.kwargs["opt_fields"]
        )

    def test_sorts_tasks_by_created_at_descending_like_asana_view(self):
        client = MagicMock()
        client.sections.get_sections_for_project.return_value = [
            {"gid": "today", "name": "8.13执行"},
        ]
        client.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "older",
                "name": "聚合/动作适配com.older.game",
                "notes": "包名：com.older.game",
                "created_at": "2026-08-13T01:00:00.000Z",
            },
            {
                "gid": "no-time",
                "name": "聚合/动作适配com.legacy.game",
                "notes": "包名：com.legacy.game",
            },
            {
                "gid": "newest",
                "name": "聚合/动作适配com.newest.game",
                "notes": "包名：com.newest.game",
                "created_at": "2026-08-13T03:00:00.000Z",
            },
            {
                "gid": "middle",
                "name": "聚合/动作适配com.middle.game",
                "notes": "包名：com.middle.game",
                "created_at": "2026-08-13T02:00:00.000Z",
            },
        ]

        result = get_asana_tasks_for_date(
            client, "project-1", today=date(2026, 8, 13)
        )

        assert [task.package_name for task in result["tasks"]] == [
            "com.newest.game",
            "com.middle.game",
            "com.older.game",
            "com.legacy.game",
        ]

    def test_reads_task_comments_into_workflow_status(self):
        client = MagicMock()
        client.sections.get_sections_for_project.return_value = [
            {"gid": "today", "name": "8.12执行"},
        ]
        client.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-1",
                "name": "聚合/动作适配com.issue.game",
                "notes": "包名：com.issue.game",
                "completed": False,
            }
        ]
        client.stories.get_stories_for_task.return_value = [
            {
                "text": "【APK Tool 页面预检：IAP_ONLY】\n应用内购，无广告，加黑",
                "resource_subtype": "comment_added",
                "created_at": "2026-08-12T01:00:00Z",
            }
        ]

        result = get_asana_tasks_for_date(
            client, "project-1", today=date(2026, 8, 12)
        )

        assert result["tasks"][0].workflow_status == "已加黑"
        assert result["tasks"][0].workflow_terminal is True
        assert result["comment_status_errors"] == []

    def test_missing_today_section_does_not_create_or_read_tasks(self):
        client = MagicMock()
        client.sections.get_sections_for_project.return_value = [
            {"gid": "old", "name": "7.29执行"},
        ]

        result = get_asana_tasks_for_date(
            client,
            "project-1",
            today=date(2026, 7, 30),
        )

        assert result == {
            "section_name": "7.30执行",
            "section_gid": "",
            "tasks": [],
        }
        client.tasks.get_tasks_for_section.assert_not_called()

    def test_builds_expected_blacklist_comment_for_iap_only(self):
        comment = build_precheck_asana_comment({
            "code": "IAP_ONLY",
            "package_name": "com.example.game",
            "detail": "页面仅标注应用内购。",
        })

        assert "【APK Tool 页面预检：IAP_ONLY】" in comment
        assert "应用内购，无广告，加黑" in comment
        assert "com.example.game" in comment

    def test_builds_google_no_package_comment(self):
        comment = build_precheck_asana_comment({
            "code": "GOOGLE_NO_PACKAGE",
            "package_name": "com.example.removed",
            "detail": "Google Play 提示 Item not found",
        })

        assert "【APK Tool 页面预检：GOOGLE_NO_PACKAGE】" in comment
        assert "google无包" in comment
        assert "com.example.removed" in comment

    def test_builds_all_network_no_package_comment(self):
        comment = build_precheck_asana_comment({
            "code": "ALL_NETWORK_NO_PACKAGE",
            "package_name": "com.example.missing",
            "detail": "Google Play 无法下载，APKCombo 也无可用包。",
        })

        assert "【APK Tool 页面预检：ALL_NETWORK_NO_PACKAGE】" in comment
        assert "全网无包，暂不适配" in comment
        assert "com.example.missing" in comment

    def test_builds_japanese_package_blacklist_comment(self):
        comment = build_precheck_asana_comment({
            "code": "JAPANESE_PACKAGE",
            "package_name": "jp.co.barows.kenshowalkprotect",
            "detail": "包名包含 jp 段且页面包含明显日文内容。",
        })

        assert "【APK Tool 页面预检：JAPANESE_PACKAGE】" in comment
        assert "日本包体，加黑" in comment
        assert "jp.co.barows.kenshowalkprotect" in comment

    def test_has_ads_does_not_create_comment(self):
        assert build_precheck_asana_comment({"code": "HAS_ADS"}) == ""

    def test_install_failure_creates_manual_review_comment(self):
        comment = build_precheck_asana_comment({
            "code": "INSTALL_FAILED",
            "package_name": "com.example.game",
            "detail": "Google Play 要求身份验证",
        })

        assert "自动下载安装失败" in comment
        assert "Google Play 要求身份验证" in comment

    def test_app_crash_comment_uses_required_wording(self):
        comment = build_precheck_asana_comment({
            "code": "APP_CRASHED",
            "package_name": "com.playcus.crosstitchcoloringart",
            "detail": "JAVA_CRASH",
        })

        assert "包体闪退，暂不适配" in comment
        assert "com.playcus.crosstitchcoloringart" in comment

    def test_adds_comment_only_once_for_same_result(self):
        client = MagicMock()
        client.stories.get_stories_for_task.return_value = []
        result = {
            "code": "NO_ADS_OR_IAP",
            "package_name": "com.example.game",
        }

        assert add_precheck_comment_once(client, "task-1", result) is True
        client.stories.create_comment.assert_called_once()
        assert "未发现广告或应用内购标识，继续下载并人工确认（不加黑）" in (
            client.stories.create_comment.call_args.args[1]
        )

        client.stories.get_stories_for_task.return_value = [{
            "text": "【APK Tool 页面预检：NO_ADS_OR_IAP】\n已经评论",
        }]
        assert add_precheck_comment_once(client, "task-1", result) is False
        client.stories.create_comment.assert_called_once()

    def test_corrects_legacy_no_ads_blacklist_comment(self):
        client = MagicMock()
        client.stories.get_stories_for_task.return_value = [{
            "text": (
                "【APK Tool 页面预检：NO_ADS_OR_IAP】\n"
                "未标注广告或应用内购，加黑"
            ),
        }]

        created = add_precheck_comment_once(client, "task-1", {
            "code": "NO_ADS_OR_IAP",
            "package_name": "com.example.game",
        })

        assert created is True
        new_comment = client.stories.create_comment.call_args.args[1]
        assert "继续下载并人工确认（不加黑）" in new_comment
        assert "此前的自动预检结论已被本次结果替代" in new_comment

    def test_new_decision_marks_previous_precheck_comment_as_replaced(self):
        client = MagicMock()
        client.stories.get_stories_for_task.return_value = [{
            "text": "【APK Tool 页面预检：DEVICE_UNSUPPORTED】\n旧结论",
        }]

        created = add_precheck_comment_once(client, "task-1", {
            "code": "IAP_ONLY",
            "package_name": "com.example.game",
        })

        assert created is True
        new_comment = client.stories.create_comment.call_args.args[1]
        assert "应用内购，无广告，加黑" in new_comment
        assert "此前的自动预检结论已被本次结果替代" in new_comment

    def test_stories_facade_uses_asana_sdk_body_shape(self):
        api = MagicMock()
        facade = _StoriesFacade(api)

        facade.create_comment("task-1", "测试评论")

        api.create_story_for_task.assert_called_once_with(
            {"data": {"text": "测试评论"}}, "task-1", {}
        )


# ═══════════════════════════════════════════════════════════════
# TestFilterPackages — 数据筛选（纯逻辑）
# ═══════════════════════════════════════════════════════════════

class TestFilterPackages:
    """验证从 Sheet 二维数组筛选 rain + 当天日期 的包名。"""

    SAMPLE_HEADERS = ["包名", "聚合适配", "完成时间", "备注", "任务链接"]

    def test_filters_rain_and_matching_date(self):
        """筛选出 聚合适配='rain' 且 完成时间 匹配的包名，保持顺序。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain", "26.6.8", "备注A", ""],
            ["com.app.beta", "rain", "26.6.9", "备注B", "https://existing"],
            ["com.app.gamma", "other", "26.6.8", "备注C", ""],
            ["com.app.delta", "rain", "26.6.8", "备注D", ""],
        ]
        result = filter_packages(data, "26.6.8")
        assert len(result) == 2
        assert result[0].package_name == "com.app.alpha"
        assert result[0].row_number == 2
        assert result[0].task_link == ""
        assert result[1].package_name == "com.app.delta"
        assert result[1].row_number == 5
        assert result[1].task_link == ""

    def test_no_matches_returns_empty_list(self):
        """没有任何行匹配时返回空列表。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain", "26.6.9", "x", ""],
            ["com.app.beta", "other", "26.6.8", "x", ""],
        ]
        result = filter_packages(data, "26.6.8")
        assert result == []

    def test_empty_data_only_headers_returns_empty(self):
        """只有表头、无数据行时返回空列表。"""
        data = [self.SAMPLE_HEADERS]
        result = filter_packages(data, "26.6.8")
        assert result == []

    def test_case_sensitive_rain_match(self):
        """'rain' 匹配区分大小写：'Rain'/'RAIN' 不应匹配。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.one", "Rain", "26.6.8", "x"],
            ["com.app.two", "RAIN", "26.6.8", "x"],
            ["com.app.three", "rain", "26.6.8", "x"],
        ]
        result = filter_packages(data, "26.6.8")
        assert result == [PackageRow("com.app.three", 4, "")]

    def test_skips_incomplete_rows(self):
        """列数不足的行应被安全跳过，不抛异常。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain"],                       # 缺少"完成时间"列
            ["com.app.beta", "rain", "26.6.8", "备注"],      # 完整行
        ]
        result = filter_packages(data, "26.6.8")
        assert result == [PackageRow("com.app.beta", 3, "")]

    def test_headers_in_different_order(self):
        """表头列顺序变化时仍能正确识别。"""
        headers = ["完成时间", "备注", "包名", "聚合适配", "任务链接"]
        data = [
            headers,
            ["26.6.8", "x", "com.app.foo", "rain", ""],
            ["26.6.9", "x", "com.app.bar", "rain", ""],
        ]
        result = filter_packages(data, "26.6.8")
        assert result == [PackageRow("com.app.foo", 2, "")]

    def test_missing_required_columns_returns_empty(self):
        """表头缺少必要列时返回空列表，不抛异常。"""
        data = [["包名", "其他列"]]
        result = filter_packages(data, "26.6.8")
        assert result == []

    def test_header_not_on_first_line_row_number_still_correct(self):
        """表头不在第一行时，行号仍然对应真实 Sheet 行号。"""
        data = [
            ["说明文字"],
            ["包名", "聚合适配", "完成时间", "任务链接"],
            ["com.app.alpha", "rain", "26.6.8", ""],
        ]
        result = filter_packages(data, "26.6.8")
        assert result == [PackageRow("com.app.alpha", 3, "")]

    def test_reads_existing_task_link_value(self):
        """能读取"任务链接"列中已有的值。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain", "26.6.8", "备注A", "https://existing-link"],
        ]
        result = filter_packages(data, "26.6.8")
        assert result[0].task_link == "https://existing-link"

    def test_reads_up2_appid_and_gp_link(self):
        """能读取 Asana 描述需要的 UP2 appid 与 GP链接。"""
        data = [
            ["包名", "聚合适配", "完成时间", "UP2 appid", "GP链接", "任务链接"],
            [
                "com.app.alpha",
                "rain",
                "26.6.8",
                "appid-001",
                "https://play.google.com/store/apps/details?id=com.app.alpha",
                "",
            ],
        ]
        result = filter_packages(data, "26.6.8")
        assert result[0].up2_appid == "appid-001"
        assert result[0].gp_link == "https://play.google.com/store/apps/details?id=com.app.alpha"

    def test_reads_up2_appid_with_whitespace_headers(self):
        data = [
            [" 包名 ", " 聚合适配 ", " 完成时间 ", " UP2 appid ", " GP链接 ", " 任务链接 "],
            [
                "com.app.alpha",
                "rain",
                "26.6.8",
                "appid-001",
                "https://play.google.com/store/apps/details?id=com.app.alpha",
                "",
            ],
        ]
        result = filter_packages(data, "26.6.8")
        assert result[0].up2_appid == "appid-001"
        assert result[0].gp_link == "https://play.google.com/store/apps/details?id=com.app.alpha"

    def test_ragged_row_task_link_defaults_to_empty(self):
        """行比表头短时，任务链接默认为空字符串。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain", "26.6.8"],  # 缺少任务链接列
        ]
        result = filter_packages(data, "26.6.8")
        assert result[0].task_link == ""

    def test_duplicate_package_names_no_longer_deduplicated(self):
        """现在不按包名去重，返回所有符合条件的行。"""
        data = [
            self.SAMPLE_HEADERS,
            ["com.app.alpha", "rain", "26.6.8", "", ""],
            ["com.app.alpha", "rain", "26.6.8", "", ""],
        ]
        result = filter_packages(data, "26.6.8")
        assert len(result) == 2
        assert result[0].row_number == 2
        assert result[1].row_number == 3


# ═══════════════════════════════════════════════════════════════
# TestTaskNamePlanning — Asana 任务名生成与去重
# ═══════════════════════════════════════════════════════════════

class TestTaskNamePlanning:
    """验证包名到 Asana 任务名的转换和保序去重。"""

    def test_build_task_name_adds_required_prefix(self):
        assert build_task_name("com.app.alpha") == "聚合/动作适配com.app.alpha"

    def test_unique_task_names_from_package_rows_preserves_first_seen_order(self):
        rows = [
            PackageRow("com.app.alpha", 2, ""),
            PackageRow("com.app.alpha", 3, ""),
            PackageRow("com.app.beta", 4, ""),
        ]
        result = unique_task_names_from_package_rows(rows)
        assert result == [
            "聚合/动作适配com.app.alpha",
            "聚合/动作适配com.app.beta",
        ]

    def test_build_asana_task_notes(self):
        row = PackageRow(
            "com.app.alpha",
            2,
            "",
            "appid-001",
            "https://play.google.com/store/apps/details?id=com.app.alpha",
        )
        assert build_asana_task_notes(row) == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

    def test_merge_asana_task_notes_preserves_manual_content(self):
        current_notes = "\n".join([
            "包名：com.old",
            "UP2 appid：old-appid",
            "GP链接： https://play.google.com/store/apps/details?id=com.old",
            "",
            "适配备注：已和研发确认",
            "问题记录：需要二次验证",
        ])
        managed_notes = "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

        assert merge_asana_task_notes(current_notes, managed_notes) == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
            "",
            "适配备注：已和研发确认",
            "问题记录：需要二次验证",
        ])

    def test_merge_asana_task_notes_adds_managed_fields_to_manual_notes(self):
        current_notes = "\n".join([
            "适配备注：只写了人工内容",
            "风险：等待确认",
        ])
        managed_notes = "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

        assert merge_asana_task_notes(current_notes, managed_notes) == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
            "",
            "适配备注：只写了人工内容",
            "风险：等待确认",
        ])

    def test_merge_asana_task_notes_keeps_existing_up2_when_new_value_is_empty(self):
        current_notes = "\n".join([
            "包名：com.smartpdf.launcher.toolkit",
            "UP2 appid：existing-appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.smartpdf.launcher.toolkit",
            "",
            "最终判断:TopOn聚合（日志分析确认）",
            "初始Activity:com.ooo.packagebox.SplashActivity",
        ])
        managed_notes = "\n".join([
            "包名：com.smartpdf.launcher.toolkit",
            "UP2 appid：",
            "GP链接： https://play.google.com/store/apps/details?id=com.smartpdf.launcher.toolkit",
        ])

        assert merge_asana_task_notes(current_notes, managed_notes) == "\n".join([
            "包名：com.smartpdf.launcher.toolkit",
            "UP2 appid：existing-appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.smartpdf.launcher.toolkit",
            "",
            "最终判断:TopOn聚合（日志分析确认）",
            "初始Activity:com.ooo.packagebox.SplashActivity",
        ])

    def test_task_descriptions_from_package_rows(self):
        rows = [
            PackageRow("com.app.alpha", 2, "", "appid-001", ""),
            PackageRow("com.app.alpha", 3, "", "appid-002", ""),
        ]
        result = task_descriptions_from_package_rows(rows)
        assert result["聚合/动作适配com.app.alpha"] == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

    def test_task_descriptions_prefers_non_empty_up2_appid(self):
        rows = [
            PackageRow("com.app.alpha", 2, "", "", ""),
            PackageRow("com.app.alpha", 3, "", "appid-002", ""),
        ]
        result = task_descriptions_from_package_rows(rows)
        assert result["聚合/动作适配com.app.alpha"] == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-002",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

    def test_task_descriptions_from_cp_records_uses_cp_payload(self):
        records = [
            {"package_name": "com.app.alpha", "up2_appid": '{"data":[{"appId":"A2:appid-001"}]}'},
            {"package_name": "com.app.alpha", "appId": "A2:ignored"},
        ]
        result = task_descriptions_from_cp_records(records)
        assert result["聚合/动作适配com.app.alpha"] == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])


# ═══════════════════════════════════════════════════════════════
# TestComputeDiff — 差集计算（纯逻辑）
# ═══════════════════════════════════════════════════════════════

class TestComputeDiff:
    """验证包名差集计算：仅返回不在已有集合中的项，保持原始顺序。"""

    def test_returns_only_new_packages(self):
        packages = ["com.a", "com.b", "com.c"]
        existing = {"com.b"}
        result = compute_diff(packages, existing)
        assert result == ["com.a", "com.c"]

    def test_all_existing_returns_empty(self):
        packages = ["com.a", "com.b"]
        existing = {"com.a", "com.b", "com.c"}
        result = compute_diff(packages, existing)
        assert result == []

    def test_none_existing_returns_all(self):
        packages = ["com.a", "com.b"]
        existing = set()
        result = compute_diff(packages, existing)
        assert result == ["com.a", "com.b"]

    def test_preserves_order(self):
        """返回结果保持原始输入顺序。"""
        packages = ["com.z", "com.a", "com.m"]
        existing = {"com.a"}
        result = compute_diff(packages, existing)
        assert result == ["com.z", "com.m"]

    def test_empty_input_returns_empty(self):
        assert compute_diff([], {"com.a"}) == []


# ═══════════════════════════════════════════════════════════════
# TestSheetRangeHelpers — Google Sheets 单元格范围工具
# ═══════════════════════════════════════════════════════════════

class TestSheetRangeHelpers:
    """验证 A1 单元格范围构造。"""

    def test_column_index_to_a1(self):
        assert column_index_to_a1(0) == "A"
        assert column_index_to_a1(25) == "Z"
        assert column_index_to_a1(26) == "AA"
        assert column_index_to_a1(27) == "AB"

    def test_quote_sheet_name(self):
        assert quote_sheet_name("26年5-6月") == "'26年5-6月'"
        assert quote_sheet_name("Foo's Sheet") == "'Foo''s Sheet'"

    def test_build_cell_range(self):
        assert build_cell_range("26年5-6月", 3, 4) == "'26年5-6月'!E3"

    def test_build_row_range(self):
        assert build_row_range("26年5-6月", 3, 5) == "'26年5-6月'!A3:E3"

    def test_get_task_link_column_index(self):
        data = [["包名", "任务链接", "聚合适配", "完成时间"]]
        assert get_task_link_column_index(data) == 1

    def test_get_task_link_column_index_missing_returns_none(self):
        data = [["包名", "聚合适配", "完成时间"]]
        assert get_task_link_column_index(data) is None


# ═══════════════════════════════════════════════════════════════
# TestCpAdaptPrefill — CP 后台数据写入 Sheet 规划
# ═══════════════════════════════════════════════════════════════

class TestCpAdaptPrefill:
    """验证 CP 后台列表数据到 Google Sheets 的前置同步逻辑。"""

    def test_request_payload_matches_target_filters(self):
        payload = build_cp_adapt_request_payload(assign="rain")
        assert payload == {
            "is_adapted": "no",
            "hide_remarked": True,
            "hide_no_up2_appid": True,
            "assign": "rain",
            "limit": 999,
            "page": 1,
        }

    def test_parse_up2_appid_strips_a2_prefix(self):
        raw = '{"data":[{"packageName":"com.app","appId":"A2:abc123"}]}'
        assert parse_up2_appid(raw) == "abc123"

    def test_extract_up2_appid_accepts_appid_field(self):
        record = {"appId": "A2:appid-001"}
        assert extract_up2_appid(record) == "appid-001"

    def test_extract_up2_appid_prefers_up2_appid_field(self):
        record = {
            "up2_appid": '{"data":[{"appId":"A2:preferred"}]}',
            "appId": "A2:ignored",
        }
        assert extract_up2_appid(record) == "preferred"

    def test_build_cp_adapt_sheet_values(self):
        record = {
            "package_name": "com.app.alpha",
            "app_name": "Alpha",
            "categ": "GAME",
            "in_app_product_price": "$1.99",
            "contains_ads": "0",
            "a2_open": True,
            "is_adapted": False,
            "assign": "rain",
            "up2_appid": '{"data":[{"appId":"A2:appid-001"}]}',
        }
        values = build_cp_adapt_sheet_values(record, "26.6.23")
        assert values["包名"] == "com.app.alpha"
        assert values["游戏名称"] == "Alpha"
        assert values["应用内付费"] == "YES"
        assert values["应用内广告"] == "NO"
        assert values["聚合适配"] == "rain"
        assert values["完成时间"] == "26.6.23"
        assert values["UP2 appid"] == "appid-001"
        assert values["GP链接"] == "https://play.google.com/store/apps/details?id=com.app.alpha"

    def test_build_cp_adapt_sheet_values_accepts_appid_fallback(self):
        record = {
            "package_name": "com.app.alpha",
            "app_name": "Alpha",
            "assign": "rain",
            "appId": "A2:fallback-001",
        }
        values = build_cp_adapt_sheet_values(record, "26.6.23")
        assert values["UP2 appid"] == "fallback-001"

    def test_plan_upserts_updates_existing_and_appends_new(self):
        sheet_data = [
            ["说明"],
            ["包名", "游戏名称", "聚合适配", "完成时间", "UP2 appid", "任务链接"],
            ["com.app.old", "Old", "rain", "26.6.22", "old-appid", "keep-link"],
        ]
        records = [
            {
                "package_name": "com.app.old",
                "app_name": "Old New",
                "assign": "rain",
                "up2_appid": '{"data":[{"appId":"A2:new-appid"}]}',
            },
            {
                "package_name": "com.app.new",
                "app_name": "New",
                "assign": "rain",
                "up2_appid": '{"data":[{"appId":"A2:brand-new"}]}',
            },
        ]

        updates = plan_cp_adapt_sheet_upserts(
            sheet_data, records, "26年5-6月", "26.6.23"
        )

        assert len(updates) == 2
        assert updates[0].row_number == 3
        assert updates[0].is_new is False
        assert updates[0].range_name == "'26年5-6月'!A3:F3"
        assert updates[0].values == [
            "com.app.old", "Old New", "rain", "26.6.23", "new-appid", "keep-link"
        ]
        assert updates[1].row_number == 4
        assert updates[1].is_new is True
        assert updates[1].values[:5] == [
            "com.app.new", "New", "rain", "26.6.23", "brand-new"
        ]


# ═══════════════════════════════════════════════════════════════
# TestPlanTaskLinkBackfills — 任务链接回填规划
# ═══════════════════════════════════════════════════════════════

class TestPlanTaskLinkBackfills:
    """验证只为空的任务链接单元格规划回填。"""

    def test_empty_task_link_generates_update(self):
        rows = [PackageRow("com.app.alpha", 2, "")]
        tasks = {
            "聚合/动作适配com.app.alpha": AsanaTaskInfo(
                "聚合/动作适配com.app.alpha",
                "task-001",
                "https://app.asana.com/0/proj/task-001",
            )
        }
        updates = plan_task_link_backfills(rows, tasks, "26年5-6月", 1)
        assert updates == [SheetCellUpdate(
            row_number=2,
            column_index=1,
            range_name="'26年5-6月'!B2",
            value="https://app.asana.com/0/proj/task-001",
        )]

    def test_existing_task_link_is_not_overwritten(self):
        rows = [PackageRow("com.app.alpha", 2, "https://manual-link")]
        tasks = {
            "聚合/动作适配com.app.alpha": AsanaTaskInfo(
                "聚合/动作适配com.app.alpha", "task-001", "https://new-link"
            )
        }
        updates = plan_task_link_backfills(rows, tasks, "26年5-6月", 1)
        assert updates == []

    def test_missing_task_info_generates_no_update(self):
        rows = [PackageRow("com.app.alpha", 2, "")]
        updates = plan_task_link_backfills(rows, {}, "26年5-6月", 1)
        assert updates == []

    def test_duplicate_package_rows_can_backfill_same_link(self):
        rows = [
            PackageRow("com.app.alpha", 2, ""),
            PackageRow("com.app.alpha", 5, ""),
        ]
        tasks = {
            "聚合/动作适配com.app.alpha": AsanaTaskInfo(
                "聚合/动作适配com.app.alpha", "task-001", "https://same-link"
            )
        }
        updates = plan_task_link_backfills(rows, tasks, "26年5-6月", 1)
        assert [update.range_name for update in updates] == [
            "'26年5-6月'!B2",
            "'26年5-6月'!B5",
        ]


# ═══════════════════════════════════════════════════════════════
# TestSheetWriteApi — Google Sheets 单元格写入
# ═══════════════════════════════════════════════════════════════

class TestSheetWriteApi:
    """验证 Google Sheets API 写入调用。"""

    def test_gs_scopes_include_google_sheets(self):
        assert "https://www.googleapis.com/auth/spreadsheets" in GS_SCOPES
        assert "googleapis.com/auth/spreadsheets.readonly" not in GS_SCOPES

    def test_update_sheet_value_calls_api(self):
        mock_service = MagicMock()
        mock_execute = (
            mock_service.spreadsheets.return_value
            .values.return_value
            .update.return_value
            .execute
        )
        mock_execute.return_value = {"updatedCells": 1}

        update_sheet_value(
            mock_service,
            "sheet-123",
            "'26年5-6月'!B2",
            "https://app.asana.com/0/x/y",
        )

        mock_service.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
            spreadsheetId="sheet-123",
            range="'26年5-6月'!B2",
            valueInputOption="RAW",
            body={"values": [["https://app.asana.com/0/x/y"]]},
        )

    def test_batch_update_sheet_values_calls_api_once(self):
        mock_service = MagicMock()
        mock_execute = (
            mock_service.spreadsheets.return_value
            .values.return_value
            .batchUpdate.return_value
            .execute
        )
        mock_execute.return_value = {"totalUpdatedCells": 2}
        data = [
            {"range": "'26年5-6月'!B2", "values": [["link-1"]]},
            {"range": "'26年5-6月'!B3", "values": [["link-2"]]},
        ]

        result = batch_update_sheet_values(mock_service, "sheet-123", data)

        assert result == {"totalUpdatedCells": 2}
        mock_service.spreadsheets.return_value.values.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-123",
            body={
                "valueInputOption": "RAW",
                "data": data,
            },
        )

    def test_backfill_task_links_batches_updates(self):
        mock_service = MagicMock()
        updates = [
            SheetCellUpdate(2, 1, "'26年5-6月'!B2", "link-1"),
            SheetCellUpdate(3, 1, "'26年5-6月'!B3", "link-2"),
        ]
        backfill_task_links(mock_service, "sheet-123", updates)
        values = mock_service.spreadsheets.return_value.values.return_value
        values.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-123",
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": "'26年5-6月'!B2", "values": [["link-1"]]},
                    {"range": "'26年5-6月'!B3", "values": [["link-2"]]},
                ],
            },
        )
        values.update.assert_not_called()

    def test_apply_sheet_row_updates_batches_updates(self):
        mock_service = MagicMock()
        updates = [
            SheetRowUpdate(2, "'26年5-6月'!A2:P2", ["a", "b"], is_new=False),
            SheetRowUpdate(3, "'26年5-6月'!A3:P3", ["c", "d"], is_new=True),
        ]

        apply_sheet_row_updates(mock_service, "sheet-123", updates)

        values = mock_service.spreadsheets.return_value.values.return_value
        values.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-123",
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": "'26年5-6月'!A2:P2", "values": [["a", "b"]]},
                    {"range": "'26年5-6月'!A3:P3", "values": [["c", "d"]]},
                ],
            },
        )
        values.update.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# TestSheetDataRetrieval — Google Sheets 数据获取
# ═══════════════════════════════════════════════════════════════

class TestSheetDataRetrieval:
    """验证 Google Sheets API 调用及返回值处理。"""

    def test_get_sheet_data_returns_values(self):
        mock_service = MagicMock()
        # 使用 .return_value 链避免 setup 阶段触发多余的 mock call
        mock_execute = (
            mock_service.spreadsheets.return_value
            .values.return_value
            .get.return_value
            .execute
        )
        mock_execute.return_value = {"values": [["A", "B"], ["1", "2"]]}

        result = get_sheet_data(mock_service, "sheet-id-123")
        assert result == [["A", "B"], ["1", "2"]]

        get_mock = mock_service.spreadsheets.return_value.values.return_value.get
        get_mock.assert_called_once_with(
            spreadsheetId="sheet-id-123", range="A:Z"
        )

    def test_get_sheet_data_empty_sheet_returns_empty_list(self):
        mock_service = MagicMock()
        mock_service.spreadsheets().values().get().execute.return_value = {}
        result = get_sheet_data(mock_service, "sheet-id-456")
        assert result == []

    def test_get_sheet_data_custom_range(self):
        mock_service = MagicMock()
        mock_execute = (
            mock_service.spreadsheets.return_value
            .values.return_value
            .get.return_value
            .execute
        )
        mock_execute.return_value = {"values": [["x"]]}

        result = get_sheet_data(mock_service, "sheet-id", range_name="Sheet1!A1:E100")
        assert result == [["x"]]

        get_mock = mock_service.spreadsheets.return_value.values.return_value.get
        get_mock.assert_called_once_with(
            spreadsheetId="sheet-id", range="Sheet1!A1:E100"
        )

    def test_build_gs_service_ignores_system_proxy_and_uses_explicit_proxy(self):
        """Google Sheets 请求只使用工具内填写的代理，不读取 macOS 系统代理。"""
        captured = {}
        credentials = MagicMock()

        class FakeCredentials:
            @staticmethod
            def from_service_account_file(sa_file, scopes):
                captured["sa_file"] = sa_file
                captured["scopes"] = scopes
                return credentials

        class FakeRequest:
            def __init__(self, session):
                captured["session"] = session

        with patch("google.oauth2.service_account.Credentials", FakeCredentials), \
             patch("google.auth.transport.requests.Request", FakeRequest):
            service = _build_gs_service(
                sa_file="/tmp/sa.json",
                proxy_url="http://127.0.0.1:7897",
            )

        session = captured["session"]
        assert captured["sa_file"] == "/tmp/sa.json"
        assert captured["scopes"] == GS_SCOPES
        assert session.trust_env is False
        assert session.proxies == {
            "http": "http://127.0.0.1:7897",
            "https": "http://127.0.0.1:7897",
        }
        credentials.refresh.assert_called_once()
        assert service._session is session


# ═══════════════════════════════════════════════════════════════
# TestAsanaSdkFacades — Asana SDK 5.x 适配层
# ═══════════════════════════════════════════════════════════════

class TestAsanaSdkFacades:
    """验证 facade 调用实际 Asana SDK 5.x 时的参数形状。"""

    def test_create_section_for_project_passes_two_sdk_args(self):
        api = MagicMock()
        api.create_section_for_project.return_value = {"gid": "section-new"}
        facade = _SectionsFacade(api)

        result = facade.create_section_for_project("proj-123", {"name": "6.9执行"})

        assert result == {"gid": "section-new"}
        api.create_section_for_project.assert_called_once_with(
            "proj-123", {"body": {"data": {"name": "6.9执行"}}}
        )

    def test_create_task_passes_one_sdk_arg(self):
        api = MagicMock()
        api.create_task.return_value = {"gid": "task-new"}
        facade = _TasksFacade(api)

        result = facade.create_task({"name": "task"})

        assert result == {"gid": "task-new"}
        api.create_task.assert_called_once_with({"data": {"name": "task"}}, {})

    def test_update_task_passes_sdk_args(self):
        api = MagicMock()
        api.update_task.return_value = {"gid": "task-001"}
        facade = _TasksFacade(api)

        result = facade.update_task("task-001", {"notes": "desc"})

        assert result == {"gid": "task-001"}
        api.update_task.assert_called_once_with(
            {"data": {"notes": "desc"}}, "task-001", {}
        )


# ═══════════════════════════════════════════════════════════════
# TestAsanaTaskInfo — 任务信息与链接映射
# ═══════════════════════════════════════════════════════════════

class TestAsanaTaskInfo:
    """验证 Asana 任务链接构建和按名称映射。"""

    def test_build_asana_task_link(self):
        result = build_asana_task_link("proj-123", "task-456")
        assert result == "https://app.asana.com/0/proj-123/task-456"

    def test_get_existing_tasks_by_name_returns_task_infos(self):
        mock_client = MagicMock()
        mock_client.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-001",
                "name": "聚合/动作适配com.app.alpha",
                "permalink_url": "https://app.asana.com/0/proj/task-001",
                "notes": "人工备注",
            }
        ]
        result = get_existing_tasks_by_name(mock_client, "section-123", "proj-123")
        assert "聚合/动作适配com.app.alpha" in result
        info = result["聚合/动作适配com.app.alpha"]
        assert info.name == "聚合/动作适配com.app.alpha"
        assert info.gid == "task-001"
        assert info.link == "https://app.asana.com/0/proj/task-001"
        assert info.notes == "人工备注"
        mock_client.tasks.get_tasks_for_section.assert_called_once_with(
            "section-123",
            opt_fields=["gid", "name", "permalink_url", "notes"],
        )

    def test_get_existing_tasks_by_name_falls_back_when_no_permalink(self):
        mock_client = MagicMock()
        mock_client.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-002",
                "name": "聚合/动作适配com.app.beta",
                # 缺少 permalink_url
            }
        ]
        result = get_existing_tasks_by_name(mock_client, "section-123", "proj-123")
        assert result["聚合/动作适配com.app.beta"].link == \
               "https://app.asana.com/0/proj-123/task-002"

    def test_create_tasks_for_packages_returns_name_to_info_map(self):
        mock_client = MagicMock()
        mock_client.tasks.create_task.side_effect = [
            {"gid": "task-003"},
            {"gid": "task-004"},
        ]
        result = create_tasks_for_packages(
            mock_client,
            "proj-123",
            "section-456",
            ["聚合/动作适配com.app.new1", "聚合/动作适配com.app.new2"],
        )
        assert len(result) == 2
        assert result["聚合/动作适配com.app.new1"].gid == "task-003"
        assert result["聚合/动作适配com.app.new2"].link == \
               "https://app.asana.com/0/proj-123/task-004"


# ═══════════════════════════════════════════════════════════════
# TestAsanaSection — 区段查找/创建（幂等）
# ═══════════════════════════════════════════════════════════════

class TestAsanaSection:
    """验证 Asana 区段的幂等查找/创建逻辑。"""

    def test_finds_existing_section(self):
        mock_client = MagicMock()
        mock_client.sections.get_sections_for_project.return_value = [
            {"gid": "s-001", "name": "6.8执行"},
            {"gid": "s-002", "name": "6.9执行"},
        ]
        gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        assert gid == "s-001"
        # 不应调用创建
        mock_client.sections.create_section_for_project.assert_not_called()

    def test_creates_section_when_not_found(self):
        mock_client = MagicMock()
        mock_client.sections.get_sections_for_project.return_value = []
        mock_client.sections.create_section_for_project.return_value = {
            "gid": "s-new", "name": "6.8执行"
        }
        gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        assert gid == "s-new"
        mock_client.sections.create_section_for_project.assert_called_once_with(
            "proj-123", {"name": "6.8执行"}
        )

    def test_creates_section_when_name_not_in_list(self):
        """区段列表中有其他区段但没有目标名称时，创建新区段。"""
        mock_client = MagicMock()
        mock_client.sections.get_sections_for_project.return_value = [
            {"gid": "s-001", "name": "6.7执行"},
        ]
        mock_client.sections.create_section_for_project.return_value = {
            "gid": "s-new", "name": "6.8执行"
        }
        gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        assert gid == "s-new"


# ═══════════════════════════════════════════════════════════════
# TestAsanaTasks — 任务查询/创建
# ═══════════════════════════════════════════════════════════════

class TestAsanaTasks:
    """验证 Asana 任务的查询与创建逻辑。"""

    def test_get_existing_task_names(self):
        mock_client = MagicMock()
        mock_client.tasks.get_tasks_for_section.return_value = [
            {"gid": "t-1", "name": "com.app.alpha"},
            {"gid": "t-2", "name": "com.app.beta"},
        ]
        names = get_existing_task_names(mock_client, "section-123")
        assert names == {"com.app.alpha", "com.app.beta"}
        mock_client.tasks.get_tasks_for_section.assert_called_once_with(
            "section-123", opt_fields=["name"]
        )

    def test_get_existing_task_names_empty_section(self):
        mock_client = MagicMock()
        mock_client.tasks.get_tasks_for_section.return_value = []
        names = get_existing_task_names(mock_client, "section-123")
        assert names == set()

    def test_create_tasks_for_packages(self):
        mock_client = MagicMock()
        mock_client.tasks.create_task.side_effect = [
            {"gid": "t-new-1"},
            {"gid": "t-new-2"},
        ]
        created = create_tasks_for_packages(
            mock_client, "proj-123", "section-456",
            ["com.app.new1", "com.app.new2"],
        )
        assert created == {
            "com.app.new1": AsanaTaskInfo(
                "com.app.new1", "t-new-1", "https://app.asana.com/0/proj-123/t-new-1"
            ),
            "com.app.new2": AsanaTaskInfo(
                "com.app.new2", "t-new-2", "https://app.asana.com/0/proj-123/t-new-2"
            ),
        }
        assert mock_client.tasks.create_task.call_count == 2

        # 验证每次调用的参数包含 workspace、name、section membership
        expected_call_1 = {
            "workspace": "1208177697797743",
            "name": "com.app.new1",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-456"}],
        }
        expected_call_2 = {
            "workspace": "1208177697797743",
            "name": "com.app.new2",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-456"}],
        }
        mock_client.tasks.create_task.assert_any_call(expected_call_1)
        mock_client.tasks.create_task.assert_any_call(expected_call_2)

    def test_create_tasks_for_packages_includes_notes(self):
        mock_client = MagicMock()
        mock_client.tasks.create_task.return_value = {"gid": "t-new-1"}

        create_tasks_for_packages(
            mock_client,
            "proj-123",
            "section-456",
            ["聚合/动作适配com.app.alpha"],
            notes_by_name={"聚合/动作适配com.app.alpha": "desc"},
        )

        mock_client.tasks.create_task.assert_called_once_with({
            "workspace": "1208177697797743",
            "name": "聚合/动作适配com.app.alpha",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-456"}],
            "notes": "desc",
        })

    def test_update_task_notes_for_packages(self):
        mock_client = MagicMock()
        tasks = {
            "聚合/动作适配com.app.alpha": AsanaTaskInfo(
                "聚合/动作适配com.app.alpha",
                "task-001",
                "link",
                "\n".join([
                    "包名：com.old",
                    "UP2 appid：old",
                    "GP链接： https://play.google.com/store/apps/details?id=com.old",
                    "",
                    "人工备注：不要覆盖",
                ]),
            )
        }

        count = update_task_notes_for_packages(
            mock_client,
            tasks,
            {
                "聚合/动作适配com.app.alpha": "\n".join([
                    "包名：com.app.alpha",
                    "UP2 appid：appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
                ])
            },
        )

        assert count == 1
        mock_client.tasks.update_task.assert_called_once_with(
            "task-001",
            {
                "notes": "\n".join([
                    "包名：com.app.alpha",
                    "UP2 appid：appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
                    "",
                    "人工备注：不要覆盖",
                ])
            },
        )

    def test_update_task_notes_skips_when_merged_notes_are_unchanged(self):
        mock_client = MagicMock()
        notes = "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
            "",
            "人工备注：不要覆盖",
        ])
        tasks = {
            "聚合/动作适配com.app.alpha": AsanaTaskInfo(
                "聚合/动作适配com.app.alpha", "task-001", "link", notes
            )
        }

        count = update_task_notes_for_packages(
            mock_client,
            tasks,
            {
                "聚合/动作适配com.app.alpha": "\n".join([
                    "包名：com.app.alpha",
                    "UP2 appid：appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
                ])
            },
        )

        assert count == 0
        mock_client.tasks.update_task.assert_not_called()

    def test_create_tasks_empty_list(self):
        mock_client = MagicMock()
        created = create_tasks_for_packages(mock_client, "p", "s", [])
        assert created == {}
        mock_client.tasks.create_task.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# TestAsanaSyncIdempotency — 幂等性核心场景
# ═══════════════════════════════════════════════════════════════

class TestAsanaSyncIdempotency:
    """
    验证端到端幂等同步核心场景：
      - 已存在的 Section 被复用
      - 已存在的 Task 不重复创建
      - 仅增量包名触发 create_task
    """

    def test_only_creates_new_packages(self):
        """区段已存在，部分任务已存在 → 仅创建缺失的包名。"""
        mock_client = MagicMock()

        # 模拟区段已存在
        mock_client.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]

        # 模拟已有任务
        mock_client.tasks.get_tasks_for_section.return_value = [
            {"gid": "t-1", "name": "com.app.existing"},
            {"gid": "t-2", "name": "com.app.old"},
        ]

        # 包名列表：2 个已存在，1 个新增
        all_packages = ["com.app.existing", "com.app.old", "com.app.new"]

        section_gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        existing = get_existing_task_names(mock_client, section_gid)
        new_packages = compute_diff(all_packages, existing)

        mock_client.tasks.create_task.return_value = {"gid": "t-new-1"}
        created = create_tasks_for_packages(
            mock_client, "proj-123", section_gid, new_packages
        )

        # 断言：仅创建 1 个任务
        assert mock_client.tasks.create_task.call_count == 1
        assert len(created) == 1

        # 断言：创建参数正确
        mock_client.tasks.create_task.assert_called_once_with({
            "workspace": "1208177697797743",
            "name": "com.app.new",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-001"}],
        })

    def test_no_new_packages_creates_nothing(self):
        """所有包名均已存在 → 不发起任何 create_task 请求。"""
        mock_client = MagicMock()

        mock_client.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_client.tasks.get_tasks_for_section.return_value = [
            {"gid": "t-1", "name": "com.app.alpha"},
            {"gid": "t-2", "name": "com.app.beta"},
        ]

        section_gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        existing = get_existing_task_names(mock_client, section_gid)
        new_packages = compute_diff(["com.app.alpha", "com.app.beta"], existing)
        created = create_tasks_for_packages(
            mock_client, "proj-123", section_gid, new_packages
        )

        assert len(new_packages) == 0
        assert len(created) == 0
        mock_client.tasks.create_task.assert_not_called()

    def test_section_not_exist_creates_section_then_tasks(self):
        """区段不存在 → 创建区段后创建任务。"""
        mock_client = MagicMock()

        # 区段不存在
        mock_client.sections.get_sections_for_project.return_value = []
        mock_client.sections.create_section_for_project.return_value = {
            "gid": "section-new", "name": "6.8执行"
        }
        # 无已有任务
        mock_client.tasks.get_tasks_for_section.return_value = []
        mock_client.tasks.create_task.return_value = {"gid": "t-new"}

        section_gid = find_or_create_section(mock_client, "proj-123", "6.8执行")
        assert section_gid == "section-new"

        existing = get_existing_task_names(mock_client, section_gid)
        new_packages = compute_diff(["com.app.fresh"], existing)

        created = create_tasks_for_packages(
            mock_client, "proj-123", section_gid, new_packages
        )

        assert len(created) == 1
        mock_client.sections.create_section_for_project.assert_called_once()
        mock_client.tasks.create_task.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# TestEndToEndSync — 编排函数全流程（全 Mock）
# ═══════════════════════════════════════════════════════════════

class TestEndToEndSync:
    """验证 sync_packages 编排函数的完整流程。"""

    def test_full_sync_flow(self):
        """端到端测试：Sheet 3 行数据，同步到 Asana 的完整链路。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        # -- Mock Google Sheets --
        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间"],
                ["com.app.alpha", "rain", "26.6.8"],
                ["com.app.beta", "rain", "26.6.8"],
                ["com.app.gamma", "other", "26.6.8"],
            ]
        }

        # -- Mock Asana: 区段已存在 --
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]

        # -- Mock Asana: 已有任务 com.app.alpha --
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {"gid": "task-001", "name": "com.app.alpha"},
        ]

        # -- Mock Asana: 已有任务（带前缀） --
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {"gid": "task-001", "name": "聚合/动作适配com.app.alpha"},
        ]

        # -- Mock Asana: 创建任务 --
        mock_asana.tasks.create_task.return_value = {"gid": "task-002"}

        # -- 执行 --
        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            today=date(2026, 6, 8),
        )

        # -- 断言结果 --
        assert result["sheet_date"] == "26.6.8"
        assert result["section_name"] == "6.8执行"
        assert result["section_gid"] == "section-001"
        assert result["total_packages"] == 2    # alpha + beta (gamma 不是 rain)
        assert result["existing_count"] == 1    # alpha
        assert result["new_count"] == 1         # beta
        assert len(result["created_gids"]) == 1
        assert result["created_gids"] == ["task-002"]

        # -- 断言：仅创建 beta（带前缀），未重复创建 alpha --
        mock_asana.tasks.create_task.assert_called_once_with({
            "workspace": "1208177697797743",
            "name": "聚合/动作适配com.app.beta",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-001"}],
            "notes": "\n".join([
                "包名：com.app.beta",
                "UP2 appid：",
                "GP链接： https://play.google.com/store/apps/details?id=com.app.beta",
            ]),
        })

    def test_sync_idempotent_on_second_run(self):
        """第二次运行：Sheet 数据不变，不应创建任何新任务。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间"],
                ["com.app.alpha", "rain", "26.6.8"],
            ]
        }

        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {"gid": "task-001", "name": "聚合/动作适配com.app.alpha"},
        ]

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 0
        assert result["created_gids"] == []
        mock_asana.tasks.create_task.assert_not_called()

    def test_creates_task_then_backfills_task_link(self):
        """新建 Asana 任务后，把任务链接回填到 Google 表格任务链接列。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "任务链接", "聚合适配", "完成时间"],
                ["com.app.alpha", "", "rain", "26.6.8"],
            ]
        }
        mock_gs.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {
            "updatedCells": 1
        }

        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = []
        mock_asana.tasks.create_task.return_value = {
            "gid": "task-001",
            "permalink_url": "https://app.asana.com/0/proj/task-001",
        }

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 1
        assert result["backfilled_count"] == 1
        assert result["backfilled_ranges"] == ["'26年5-6月'!B2"]
        values = mock_gs.spreadsheets.return_value.values.return_value
        values.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-123",
            body={
                "valueInputOption": "RAW",
                "data": [{
                    "range": "'26年5-6月'!B2",
                    "values": [["https://app.asana.com/0/proj/task-001"]],
                }],
            },
        )
        values.update.assert_not_called()

    def test_sync_packages_reads_sheet_through_az_for_description_fields(self):
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间", "UP2 appid", "GP链接"],
                [
                    "com.app.alpha",
                    "rain",
                    "26.6.25",
                    "appid-001",
                    "https://play.google.com/store/apps/details?id=com.app.alpha",
                ],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.25执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = []
        mock_asana.tasks.create_task.return_value = {"gid": "task-001"}

        sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 25),
        )

        calls = mock_gs.spreadsheets.return_value.values.return_value.get.call_args_list
        assert any(
            call.kwargs == {"spreadsheetId": "sheet-123", "range": "26年5-6月!A:AZ"}
            for call in calls
        )
        mock_asana.tasks.create_task.assert_called_once_with({
            "workspace": "1208177697797743",
            "name": "聚合/动作适配com.app.alpha",
            "parent": "1215490559662224",
            "memberships": [{"project": "proj-123", "section": "section-001"}],
            "notes": "\n".join([
                "包名：com.app.alpha",
                "UP2 appid：appid-001",
                "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
            ]),
        })

    def test_sync_packages_uses_prefill_notes_when_provided(self):
        mock_gs = MagicMock()
        mock_asana = MagicMock()
        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "任务链接", "聚合适配", "完成时间"],
                ["com.app.alpha", "", "rain", "26.6.25"],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.25执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = []
        mock_asana.tasks.create_task.return_value = {"gid": "task-001"}

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 25),
            notes_by_name={
                "聚合/动作适配com.app.alpha": "\n".join([
                    "包名：com.app.alpha",
                    "UP2 appid：appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
                ])
            },
        )

        assert result["new_count"] == 1
        args, _ = mock_asana.tasks.create_task.call_args
        assert args[0]["notes"] == "\n".join([
            "包名：com.app.alpha",
            "UP2 appid：appid-001",
            "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
        ])

    def test_existing_task_with_empty_sheet_link_is_backfilled(self):
        """Asana 任务已存在但 Sheet 链接为空时，补写任务链接。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "任务链接", "聚合适配", "完成时间"],
                ["com.app.alpha", "", "rain", "26.6.8"],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-001",
                "name": "聚合/动作适配com.app.alpha",
                "permalink_url": "https://app.asana.com/0/proj/task-001",
            }
        ]

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 0
        assert result["backfilled_count"] == 1
        mock_asana.tasks.create_task.assert_not_called()
        values = mock_gs.spreadsheets.return_value.values.return_value
        values.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-123",
            body={
                "valueInputOption": "RAW",
                "data": [{
                    "range": "'26年5-6月'!B2",
                    "values": [["https://app.asana.com/0/proj/task-001"]],
                }],
            },
        )
        values.update.assert_not_called()

    def test_existing_sheet_link_is_not_overwritten(self):
        """任务链接列已有值时不覆盖。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "任务链接", "聚合适配", "完成时间"],
                ["com.app.alpha", "https://manual-link", "rain", "26.6.8"],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-001",
                "name": "聚合/动作适配com.app.alpha",
                "permalink_url": "https://app.asana.com/0/proj/task-001",
            }
        ]

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["backfilled_count"] == 0
        mock_gs.spreadsheets.return_value.values.return_value.update.assert_not_called()

    def test_existing_asana_notes_preserve_manual_content(self):
        """已有 Asana 描述中的人工内容不被同步字段覆盖掉。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间", "UP2 appid", "GP链接"],
                [
                    "com.app.alpha",
                    "rain",
                    "26.6.8",
                    "appid-001",
                    "https://play.google.com/store/apps/details?id=com.app.alpha",
                ],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-001",
                "name": "聚合/动作适配com.app.alpha",
                "permalink_url": "https://app.asana.com/0/proj/task-001",
                "notes": "\n".join([
                    "包名：com.old",
                    "UP2 appid：old",
                    "GP链接： https://play.google.com/store/apps/details?id=com.old",
                    "",
                    "人工备注：这段不能被删",
                ]),
            }
        ]

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 0
        assert result["notes_updated_count"] == 1
        mock_asana.tasks.update_task.assert_called_once_with(
            "task-001",
            {
                "notes": "\n".join([
                    "包名：com.app.alpha",
                    "UP2 appid：appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.app.alpha",
                    "",
                    "人工备注：这段不能被删",
                ])
            },
        )

    def test_existing_asana_up2_is_not_cleared_by_empty_sheet_value(self):
        """Sheet/CP 来源 UP2 为空时，不清空 Asana 已有非空 UP2 appid。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间", "UP2 appid", "GP链接"],
                [
                    "com.smartpdf.launcher.toolkit",
                    "rain",
                    "26.6.8",
                    "",
                    "https://play.google.com/store/apps/details?id=com.smartpdf.launcher.toolkit",
                ],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = [
            {
                "gid": "task-001",
                "name": "聚合/动作适配com.smartpdf.launcher.toolkit",
                "permalink_url": "https://app.asana.com/0/proj/task-001",
                "notes": "\n".join([
                    "包名：com.smartpdf.launcher.toolkit",
                    "UP2 appid：existing-appid-001",
                    "GP链接： https://play.google.com/store/apps/details?id=com.smartpdf.launcher.toolkit",
                    "",
                    "最终判断:TopOn聚合（日志分析确认）",
                    "初始Activity:com.ooo.packagebox.SplashActivity",
                ]),
            }
        ]

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 0
        assert result["notes_updated_count"] == 0
        mock_asana.tasks.update_task.assert_not_called()

    def test_missing_task_link_column_skips_backfill_but_creates_task(self):
        """缺少任务链接列时不阻塞 Asana 任务创建。"""
        mock_gs = MagicMock()
        mock_asana = MagicMock()

        mock_gs.spreadsheets().values().get().execute.return_value = {
            "values": [
                ["包名", "聚合适配", "完成时间"],
                ["com.app.alpha", "rain", "26.6.8"],
            ]
        }
        mock_asana.sections.get_sections_for_project.return_value = [
            {"gid": "section-001", "name": "6.8执行"},
        ]
        mock_asana.tasks.get_tasks_for_section.return_value = []
        mock_asana.tasks.create_task.return_value = {"gid": "task-001"}

        result = sync_packages(
            gs_service=mock_gs,
            asana_client=mock_asana,
            sheet_id="sheet-123",
            project_gid="proj-123",
            sheet_name="26年5-6月",
            today=date(2026, 6, 8),
        )

        assert result["new_count"] == 1
        assert result["backfilled_count"] == 0
        assert result["backfill_skipped_reason"] == "missing_task_link_column"
        mock_asana.tasks.create_task.assert_called_once()
        mock_gs.spreadsheets.return_value.values.return_value.update.assert_not_called()
