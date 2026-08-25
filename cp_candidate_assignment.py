"""Score unassigned CP records and assign selected candidates to an operator."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Iterable

import requests

from auto_asana.main import fetch_cp_adapt_records


BASE_SUCCESS_RATE = 41
RECOMMENDED_SCORE = 55

GAME_PATTERN = re.compile(
    r"game|puzzle|sort|match|color|arrow|mahjong|solitaire|dice|ball|"
    r"jigsaw|block",
    re.I,
)
UTILITY_PATTERN = re.compile(
    r"clean|cache|wifi|signal|tool|utility|scanner|browser|download|"
    r"recover|storage|phone|weather|vpn|file",
    re.I,
)
CASINO_PATTERN = re.compile(r"casino|slot|bingo|jackpot|poker", re.I)
REWARD_PATTERN = re.compile(r"reward|earn|cash|coin|money|withdraw", re.I)
SOCIAL_PATTERN = re.compile(r"social|chat|video|music|photo|story|reels", re.I)
UP2_APPID_PATTERN = re.compile(r"^(?:A2:)?[0-9a-f]{32}$", re.I)


def extract_up2_appid(value: Any) -> str:
    """Return the usable app id from either a plain value or CP JSON payload."""
    if isinstance(value, dict):
        data = value.get("data")
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
        data = decoded.get("data") if isinstance(decoded, dict) else None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("appId") or "").strip():
                return str(item.get("appId") or "").strip()
    return ""


def score_cp_candidate(record: dict[str, Any]) -> dict[str, Any]:
    """Score one CP record using the historical package-name observations.

    The score is only a queue-priority estimate.  It must never be treated as
    a final adaptation verdict because attribution, SDK detection and runtime
    stability cannot be learned from the package name alone.
    """
    package_name = str(record.get("package_name") or "").strip()
    assigned_to = str(record.get("assign") or "").strip()
    up2_appid = extract_up2_appid(record.get("up2_appid"))
    reasons: list[str] = []
    score = BASE_SUCCESS_RATE
    category = "普通包名"

    if package_name.lower().startswith("jp."):
        score = 0
        category = "日本包体"
        reasons.append("jp. 前缀按现有规则不推荐适配")
    elif CASINO_PATTERN.search(package_name):
        score = 8
        category = "博彩/老虎机"
        reasons.append("历史样本成功率约 7.7%")
    elif UTILITY_PATTERN.search(package_name):
        score = 18
        category = "工具/清理"
        reasons.append("历史样本成功率约 17.8%")
    elif GAME_PATTERN.search(package_name):
        score = 59
        category = "游戏/益智"
        reasons.append("历史样本成功率约 58.7%")
    elif REWARD_PATTERN.search(package_name):
        score = 50
        category = "奖励/赚钱"
        reasons.append("历史样本成功率约 50%，样本较少")
    elif SOCIAL_PATTERN.search(package_name):
        score = 47
        category = "社交/媒体"
        reasons.append("历史样本成功率约 46.7%")
    else:
        reasons.append("没有命中明确的高低风险包名特征")

    eligible = True
    if not package_name:
        eligible = False
        score = 0
        reasons.append("缺少包名")
    if not UP2_APPID_PATTERN.fullmatch(up2_appid):
        eligible = False
        score = 0
        reasons.append("UP2 appid 缺失或格式异常")
    if bool(record.get("is_adapted")):
        eligible = False
        reasons.append("后台已标记适配完成")
    if assigned_to:
        eligible = False
        reasons.append(f"已经分配给 {assigned_to}")
    if str(record.get("ps") or "").strip() or str(record.get("block_ps") or "").strip():
        eligible = False
        reasons.append("后台已有备注或 Block 原因")

    recommended = eligible and score >= RECOMMENDED_SCORE
    return {
        "package_name": package_name,
        "up2_appid": up2_appid,
        "gp_link": (
            "https://play.google.com/store/apps/details?id="
            + urllib.parse.quote(package_name, safe=".")
            if package_name
            else ""
        ),
        "assigned_to": assigned_to,
        "score": score,
        "category": category,
        "eligible": eligible,
        "recommended": recommended,
        "reason": "；".join(reasons),
    }


def load_cp_assignment_candidates(
    *,
    api_url: str,
    x_token: str,
    token: str,
    session: Any = None,
) -> dict[str, Any]:
    """Load all visible pending records and score unassigned candidates."""
    records, total = fetch_cp_adapt_records(
        api_url=api_url,
        x_token=x_token,
        token=token,
        assign="",
        limit=999,
        session=session,
    )
    scored = [score_cp_candidate(record) for record in records]
    unassigned = [item for item in scored if not item["assigned_to"]]
    unassigned.sort(
        key=lambda item: (
            not item["recommended"],
            -int(item["score"]),
            item["package_name"].casefold(),
        )
    )
    return {
        "reported_total": total,
        "visible_count": len(records),
        "unassigned_count": len(unassigned),
        "recommended_count": sum(item["recommended"] for item in unassigned),
        "candidates": unassigned,
    }


def _derive_submit_url(api_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(api_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("后台接口地址格式无效")
    path = parsed.path.rstrip("/")
    if path.endswith("/cp_adapt/list"):
        path = path[: -len("/cp_adapt/list")] + "/s10_package_info"
    elif not path.endswith("/s10_package_info"):
        path += "/s10_package_info"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _derive_list_url(api_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(api_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("后台接口地址格式无效")
    path = parsed.path.rstrip("/")
    if path.endswith("/s10_package_info"):
        path = path[: -len("/s10_package_info")] + "/cp_adapt/list"
    elif not path.endswith("/cp_adapt/list"):
        path += "/cp_adapt/list"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _headers(x_token: str, token: str) -> dict[str, str]:
    if not str(x_token or "").strip():
        raise ValueError("缺少后台 X-Token")
    if not str(token or "").strip():
        raise ValueError("缺少后台固定 token")
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "http://data_center_web_internet.hongdinghe.cn",
        "Referer": "http://data_center_web_internet.hongdinghe.cn/",
        "X-Token": str(x_token).strip(),
        "token": str(token).strip(),
    }


def assign_cp_candidate(
    package_name: str,
    *,
    api_url: str,
    x_token: str,
    token: str,
    assignee: str = "rain",
    user_name: str = "rain",
    timeout_seconds: int = 60,
    session: Any = None,
) -> dict[str, Any]:
    """Assign one package and verify the persisted assignee by list readback."""
    package_name = str(package_name or "").strip()
    assignee = str(assignee or "").strip()
    if not package_name:
        raise ValueError("缺少包名")
    if not assignee:
        raise ValueError("缺少目标适配人员")
    headers = _headers(x_token, token)
    http = session or requests.Session()
    response = http.post(
        _derive_submit_url(api_url),
        headers=headers,
        json={
            "package_name": package_name,
            "assign": assignee,
            "user_name": str(user_name or assignee).strip() or assignee,
        },
        timeout=max(10, min(int(timeout_seconds), 120)),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 200:
        raise RuntimeError("后台未接受适配人员修改请求")

    list_response = http.post(
        _derive_list_url(api_url),
        headers=headers,
        json={
            "is_adapted": "all",
            "hide_remarked": False,
            "hide_no_up2_appid": False,
            "assign": "",
            "package_name": package_name,
            "limit": 999,
            "page": 1,
        },
        timeout=max(10, min(int(timeout_seconds), 120)),
    )
    list_response.raise_for_status()
    list_body = list_response.json()
    data = list_body.get("data") if isinstance(list_body, dict) else None
    records = data.get("data") if isinstance(data, dict) else None
    record = next(
        (
            item
            for item in (records or [])
            if isinstance(item, dict)
            and str(item.get("package_name") or "").strip() == package_name
        ),
        None,
    )
    if record is None or str(record.get("assign") or "").strip() != assignee:
        raise RuntimeError(f"后台回读未确认 {package_name} 已分配给 {assignee}")
    return {
        "ok": True,
        "package_name": package_name,
        "assign": assignee,
    }


def assign_cp_candidates(
    package_names: Iterable[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Assign selected packages independently and return per-package results."""
    results = []
    seen: set[str] = set()
    for raw_name in package_names:
        package_name = str(raw_name or "").strip()
        if not package_name or package_name in seen:
            continue
        seen.add(package_name)
        try:
            results.append(assign_cp_candidate(package_name, **kwargs))
        except Exception as exc:  # Keep the remaining selected packages moving.
            results.append(
                {
                    "ok": False,
                    "package_name": package_name,
                    "error": str(exc),
                }
            )
    return {
        "ok": all(item.get("ok") for item in results),
        "success_count": sum(bool(item.get("ok")) for item in results),
        "failure_count": sum(not item.get("ok") for item in results),
        "results": results,
    }
