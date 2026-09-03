import json
from unittest.mock import Mock

import pytest
import cp_candidate_assignment

from cp_candidate_assignment import (
    assign_cp_candidate,
    build_historical_success_profile,
    extract_up2_appid,
    load_cp_assignment_candidates,
    normalize_cp_priority,
    score_cp_candidate,
)


@pytest.fixture(autouse=True)
def allow_private_assignment_in_unit_tests(monkeypatch):
    monkeypatch.setattr(cp_candidate_assignment, "require_private_feature", lambda _name: None)


def record(package_name, **values):
    payload = {
        "package_name": package_name,
        "up2_appid": json.dumps(
            {"data": [{"packageName": package_name, "appId": "A2:" + "a" * 32}]}
        ),
        "assign": "",
        "is_adapted": False,
        "ps": None,
        "block_ps": None,
    }
    payload.update(values)
    return payload


def response(body):
    item = Mock()
    item.raise_for_status.return_value = None
    item.json.return_value = body
    return item


def test_extract_up2_appid_supports_cp_nested_json():
    assert extract_up2_appid(record("com.demo.game")["up2_appid"]) == "A2:" + "a" * 32


@pytest.mark.parametrize(
    ("package_name", "score", "recommended", "category"),
    [
        ("com.demo.arrow.puzzle.game", 59, True, "游戏/益智"),
        ("com.demo.wifi.cleaner", 18, False, "工具/清理"),
        ("com.demo.jackpot.slots", 8, False, "博彩/老虎机"),
        ("jp.co.demo.game", 0, False, "日本包体"),
        ("com.demo.application", 41, False, "普通包名"),
    ],
)
def test_score_cp_candidate_uses_historical_package_groups(
    package_name, score, recommended, category
):
    result = score_cp_candidate(record(package_name))
    assert result["score"] == score
    assert result["recommended"] is recommended
    assert result["category"] == category


def test_score_never_recommends_assigned_or_invalid_records():
    assigned = score_cp_candidate(record("com.demo.game", assign="snow"))
    invalid = score_cp_candidate(record("com.demo.game", up2_appid="broken"))
    assert assigned["recommended"] is False
    assert assigned["eligible"] is False
    assert invalid["recommended"] is False
    assert invalid["score"] == 0


def test_historical_profile_uses_only_resolved_snow_aggregation_rows():
    sheet = [
        ["说明"],
        ["包名", "任务链接", "聚合适配", "适配进度", "完成时间", "动作适配", "适配进度"],
        ["com.demo.puzzle.game", "", "snow", "已适配", "26.8.1", "snow", "已适配"],
        ["com.demo.arrow.game", "", "snow", "暂不适配", "26.8.1", "snow", ""],
        ["com.demo.block.game", "", "rain", "已适配", "26.8.1", "rain", "已适配"],
        ["com.demo.match.game", "", "snow", "适配中", "26.8.1", "snow", ""],
    ]
    profile = build_historical_success_profile(sheet)
    assert profile["游戏/益智"]["success"] == 1
    assert profile["游戏/益智"]["total"] == 2
    assert profile["__overall__"]["total"] == 2


def test_score_uses_dynamic_historical_profile_when_available():
    profile = {
        "游戏/益智": {"success": 19, "total": 25, "raw_rate": 76, "score": 70},
        "__overall__": {"success": 40, "total": 80, "raw_rate": 50, "score": 50},
    }
    result = score_cp_candidate(record("com.demo.puzzle.game"), profile)
    assert result["score"] == 70
    assert result["recommended"] is True
    assert "19/25" in result["reason"]


def test_historical_profile_ignores_obsolete_tradplus_unsupported_failures():
    sheet = [
        ["包名", "聚合适配", "适配进度", "适配所遇问题"],
        ["com.alpha.puzzle.game", "snow", "已适配", ""],
        ["com.beta.puzzle.game", "snow", "暂不适配", "TradPlus聚合，暂不适配"],
    ]
    profile = build_historical_success_profile(sheet)
    assert profile["游戏/益智"]["success"] == 1
    assert profile["游戏/益智"]["total"] == 1


def test_japanese_rule_is_not_overridden_by_historical_success():
    profile = {
        "日本包体": {"success": 5, "total": 5, "raw_rate": 100, "score": 90}
    }
    result = score_cp_candidate(record("jp.example.game"), profile)
    assert result["score"] == 0
    assert result["recommended"] is False


def test_iap_without_ads_is_default_selected_as_quick_black_candidate():
    item = record(
        "com.demo.wifi.cleaner",
        app_name="Cleaner Pro",
        categ="TOOLS",
        in_app_product_price="9.99",
        contains_ads="0",
    )
    result = score_cp_candidate(item)
    assert result["recommended"] is False
    assert result["quick_black_candidate"] is True
    assert result["default_selected"] is True
    assert result["selection_group"] == "低概率/加黑候选"
    assert result["has_iap"] is True
    assert result["has_ads"] is False


def test_ads_no_with_real_metadata_enters_low_probability_lane():
    item = record(
        "com.demo.utility",
        app_name="Device Utility",
        categ="TOOLS",
        in_app_product_price="NO",
        contains_ads="NO",
    )
    result = score_cp_candidate(item)
    assert result["quick_black_candidate"] is True
    assert result["default_selected"] is True


def test_empty_metadata_and_both_no_is_excluded_from_low_probability_lane():
    item = record(
        "com.demo.utility",
        app_name="",
        categ="",
        in_app_product_price="NO",
        contains_ads="NO",
    )
    result = score_cp_candidate(item)
    assert result["quick_black_candidate"] is False
    assert result["excluded_incomplete"] is True
    assert result["default_selected"] is False
    assert "已排除低概率筛选" in result["reason"]


def test_ads_yes_does_not_enter_quick_black_lane():
    item = record(
        "com.demo.wifi.cleaner",
        app_name="Cleaner Pro",
        categ="TOOLS",
        in_app_product_price="YES",
        contains_ads="YES",
    )
    result = score_cp_candidate(item)
    assert result["quick_black_candidate"] is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("高", "高"), ("中", "中"), ("低", "低"), ("HIGH", "高"), (None, "未标注")],
)
def test_normalize_cp_priority(value, expected):
    assert normalize_cp_priority(value) == expected


def test_load_candidates_reads_all_assignees_and_defaults_high_score_first():
    session = Mock()
    session.post.return_value = response(
        {
            "code": 200,
            "data": {
                "code": 200,
                "total": 3,
                "data": [
                    record("com.demo.cleaner"),
                    record("com.demo.puzzle.game"),
                    record("com.already.game", assign="snow"),
                ],
            },
        }
    )
    result = load_cp_assignment_candidates(
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        session=session,
    )
    assert result["unassigned_count"] == 2
    assert result["recommended_count"] == 1
    assert result["candidates"][0]["package_name"] == "com.demo.puzzle.game"
    assert session.post.call_args.kwargs["json"]["assign"] == ""


def test_load_candidates_orders_backend_high_priority_before_probability_score():
    session = Mock()
    high_priority = record("com.demo.wifi.cleaner", priority="高")
    low_priority = record("com.demo.puzzle.game", priority="低")
    session.post.return_value = response(
        {
            "code": 200,
            "data": {
                "code": 200,
                "total": 2,
                "data": [low_priority, high_priority],
            },
        }
    )

    result = load_cp_assignment_candidates(
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        session=session,
    )

    assert [item["package_name"] for item in result["candidates"]] == [
        "com.demo.wifi.cleaner",
        "com.demo.puzzle.game",
    ]
    assert result["candidates"][0]["priority"] == "高"
    assert result["high_priority_count"] == 1


def test_assign_candidate_submits_assign_only_and_verifies_readback():
    session = Mock()
    session.post.side_effect = [
        response({"code": 200, "data": {"package_name": "com.demo.game", "assign": "rain"}}),
        response(
            {
                "code": 200,
                "data": {
                    "code": 200,
                    "total": 1,
                    "data": [record("com.demo.game", assign="rain")],
                },
            }
        ),
    ]
    result = assign_cp_candidate(
        "com.demo.game",
        api_url="http://example.test/cp_adapt/list",
        x_token="x",
        token="fixed",
        session=session,
    )
    assert result["ok"] is True
    submit = session.post.call_args_list[0]
    assert submit.args[0].endswith("/s10_package_info")
    assert submit.kwargs["json"] == {
        "package_name": "com.demo.game",
        "assign": "rain",
        "user_name": "rain",
    }


def test_assign_candidate_rejects_unconfirmed_readback():
    session = Mock()
    session.post.side_effect = [
        response({"code": 200, "data": {}}),
        response({"code": 200, "data": {"code": 200, "total": 0, "data": []}}),
    ]
    with pytest.raises(RuntimeError, match="回读未确认"):
        assign_cp_candidate(
            "com.demo.game",
            api_url="http://example.test/cp_adapt/list",
            x_token="x",
            token="fixed",
            session=session,
        )
