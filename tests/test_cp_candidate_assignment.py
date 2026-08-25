import json
from unittest.mock import Mock

import pytest

from cp_candidate_assignment import (
    assign_cp_candidate,
    extract_up2_appid,
    load_cp_assignment_candidates,
    score_cp_candidate,
)


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
