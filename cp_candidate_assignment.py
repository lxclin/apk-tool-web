"""Score unassigned CP records and assign selected candidates to an operator."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections import defaultdict
from typing import Any, Iterable

import requests

from auto_asana.main import fetch_cp_adapt_records
from private_features import require_private_feature


BASE_SUCCESS_RATE = 58
RECOMMENDED_SCORE = 70
HISTORICAL_PRIOR_SAMPLE_SIZE = 20
TOKEN_PRIOR_SAMPLE_SIZE = 15
MIN_TOKEN_SAMPLE_SIZE = 12

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
CP_PRIORITY_RANK = {"高": 0, "中": 1, "低": 2}

FALLBACK_CATEGORY_RATES = {
    "日本包体": 7,
    "博彩/老虎机": 56,
    "工具/清理": 38,
    "游戏/益智": 78,
    "奖励/赚钱": 57,
    "社交/媒体": 59,
    "普通包名": 48,
}

PACKAGE_TOKEN_STOPWORDS = {
    "android",
    "app",
    "apps",
    "com",
    "game",
    "games",
    "mobile",
    "studio",
}


def classify_package_category(package_name: str) -> str:
    """Classify package names with the same precedence used by scoring."""
    package_name = str(package_name or "").strip()
    if package_name.lower().startswith("jp."):
        return "日本包体"
    if CASINO_PATTERN.search(package_name):
        return "博彩/老虎机"
    if UTILITY_PATTERN.search(package_name):
        return "工具/清理"
    if GAME_PATTERN.search(package_name):
        return "游戏/益智"
    if REWARD_PATTERN.search(package_name):
        return "奖励/赚钱"
    if SOCIAL_PATTERN.search(package_name):
        return "社交/媒体"
    return "普通包名"


def classify_candidate_category(package_name: str, large_category: str = "") -> str:
    """Prefer the backend's explicit GAME category over package-name guesses."""
    package_category = classify_package_category(package_name)
    if package_category == "日本包体":
        return package_category
    normalized = re.sub(
        r"[^A-Z0-9]+", "_", str(large_category or "").upper()
    ).strip("_")
    if normalized == "GAME" or normalized.startswith("GAME_"):
        return "游戏/益智"
    return package_category


def _normalized_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _package_tokens(package_name: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(package_name or "").casefold())
        if len(token) >= 3 and token not in PACKAGE_TOKEN_STOPWORDS
    }


def _resolved_sheet_outcomes(
    sheet_data: list[list[Any]], assignees: Iterable[str]
) -> list[tuple[str, bool]]:
    """Extract resolved package outcomes from one chronological Sheet tab."""
    if not sheet_data:
        return []
    header_row = next(
        (
            index
            for index, row in enumerate(sheet_data)
            if "包名" in {_normalized_header(value) for value in row}
            and "聚合适配" in {_normalized_header(value) for value in row}
        ),
        None,
    )
    if header_row is None:
        return []
    headers = [_normalized_header(value) for value in sheet_data[header_row]]
    try:
        package_col = headers.index("包名")
        owner_col = headers.index("聚合适配")
    except ValueError:
        return []
    status_col = next(
        (
            index
            for index in range(owner_col + 1, len(headers))
            if headers[index] == "适配进度"
        ),
        None,
    )
    if status_col is None:
        return []
    issue_col = next(
        (index for index, header in enumerate(headers) if header == "适配所遇问题"),
        None,
    )
    allowed_assignees = {
        str(value or "").strip().casefold()
        for value in assignees
        if str(value or "").strip()
    }
    outcomes: list[tuple[str, bool]] = []
    for row in sheet_data[header_row + 1 :]:
        owner = str(row[owner_col] if owner_col < len(row) else "").strip().casefold()
        status = str(row[status_col] if status_col < len(row) else "").strip()
        if owner not in allowed_assignees or status not in {
            "已适配",
            "暂不适配",
            "A2关闭",
        }:
            continue
        issue = str(
            row[issue_col] if issue_col is not None and issue_col < len(row) else ""
        ).strip()
        if "tradplus" in issue.casefold() and status != "已适配":
            continue
        package_name = str(row[package_col] if package_col < len(row) else "").strip()
        if package_name:
            outcomes.append((package_name, status == "已适配"))
    return outcomes


def _profile_from_outcomes(
    outcomes: Iterable[tuple[str, bool]], *, assignees: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Build de-duplicated category and token statistics."""
    latest_by_package: dict[str, tuple[str, bool]] = {}
    for package_name, succeeded in outcomes:
        latest_by_package[package_name.casefold()] = (package_name, bool(succeeded))
    resolved = list(latest_by_package.values())
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"success": 0, "total": 0}
    )
    token_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"success": 0, "total": 0}
    )
    for package_name, succeeded in resolved:
        category = classify_package_category(package_name)
        counts[category]["total"] += 1
        counts[category]["success"] += int(succeeded)
        for token in _package_tokens(package_name):
            token_counts[token]["total"] += 1
            token_counts[token]["success"] += int(succeeded)

    resolved_total = len(resolved)
    resolved_success = sum(int(succeeded) for _, succeeded in resolved)
    base_rate = (
        resolved_success / resolved_total
        if resolved_total
        else BASE_SUCCESS_RATE / 100
    )
    profile: dict[str, dict[str, Any]] = {}
    for category, item in counts.items():
        total, success = item["total"], item["success"]
        adjusted_rate = (success + base_rate * HISTORICAL_PRIOR_SAMPLE_SIZE) / (
            total + HISTORICAL_PRIOR_SAMPLE_SIZE
        )
        profile[category] = {
            "success": success,
            "total": total,
            "raw_rate": round(success * 100 / total),
            "score": round(adjusted_rate * 100),
        }
    token_profile: dict[str, dict[str, Any]] = {}
    for token, item in token_counts.items():
        total, success = item["total"], item["success"]
        if total < MIN_TOKEN_SAMPLE_SIZE:
            continue
        adjusted_rate = (success + base_rate * TOKEN_PRIOR_SAMPLE_SIZE) / (
            total + TOKEN_PRIOR_SAMPLE_SIZE
        )
        token_profile[token] = {
            "success": success,
            "total": total,
            "raw_rate": round(success * 100 / total),
            "score": round(adjusted_rate * 100),
        }
    labels = [str(value).strip() for value in assignees if str(value).strip()]
    profile["__tokens__"] = token_profile
    profile["__overall__"] = {
        "success": resolved_success,
        "total": resolved_total,
        "raw_rate": round(base_rate * 100),
        "score": round(base_rate * 100),
        "assignees": "+".join(labels),
    }
    return profile


def build_historical_success_profile(
    sheet_data: list[list[Any]],
    *,
    assignee: str = "snow",
) -> dict[str, dict[str, Any]]:
    """Build category success rates from one Sheet tab.

    Only ``已适配`` is a success. ``暂不适配`` and ``A2关闭`` are resolved
    failures. Pending/blank rows are intentionally excluded so an unfinished
    workday cannot lower the score.
    """
    assignees = (str(assignee or "snow").strip(),)
    outcomes = _resolved_sheet_outcomes(sheet_data, assignees)
    return _profile_from_outcomes(outcomes, assignees=assignees)


def build_historical_success_profile_from_sheets(
    sheet_datasets: Iterable[list[list[Any]]],
    *,
    assignees: Iterable[str] = ("rain", "snow"),
) -> dict[str, dict[str, Any]]:
    """Build one profile from all chronological tabs and both operators."""
    assignees = tuple(assignees)
    outcomes: list[tuple[str, bool]] = []
    for sheet_data in sheet_datasets:
        outcomes.extend(_resolved_sheet_outcomes(sheet_data, assignees))
    return _profile_from_outcomes(outcomes, assignees=assignees)


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


def backend_flag_enabled(value: Any) -> bool:
    """Interpret CP list values using the same YES/NO semantics as the UI."""
    if value is None or value is False:
        return False
    cleaned = str(value).strip().casefold()
    return cleaned not in {"", "0", "no", "false", "none", "null", "-"}


def normalize_cp_priority(value: Any) -> str:
    """Normalize the CP backend priority used for queue ordering."""
    cleaned = str(value or "").strip()
    aliases = {
        "high": "高",
        "middle": "中",
        "medium": "中",
        "low": "低",
    }
    return cleaned if cleaned in CP_PRIORITY_RANK else aliases.get(cleaned.casefold(), "未标注")


def score_cp_candidate(
    record: dict[str, Any],
    historical_profile: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score one CP record using the historical package-name observations.

    The score is only a queue-priority estimate.  It must never be treated as
    a final adaptation verdict because attribution, SDK detection and runtime
    stability cannot be learned from the package name alone.
    """
    package_name = str(record.get("package_name") or "").strip()
    assigned_to = str(record.get("assign") or "").strip()
    up2_appid = extract_up2_appid(record.get("up2_appid"))
    app_name = str(record.get("app_name") or "").strip()
    large_category = str(record.get("categ") or "").strip()
    has_iap = backend_flag_enabled(record.get("in_app_product_price"))
    has_ads = backend_flag_enabled(record.get("contains_ads"))
    priority = normalize_cp_priority(record.get("priority"))
    reasons: list[str] = []
    category = classify_candidate_category(package_name, large_category)
    score = FALLBACK_CATEGORY_RATES[category]
    historical = (historical_profile or {}).get(category) or {}
    if category == "日本包体":
        score = 0
        reasons.append("jp. 前缀按现有规则不推荐适配")
    elif historical.get("total"):
        score = int(historical.get("score", score))
        history_label = str(
            ((historical_profile or {}).get("__overall__") or {}).get("assignees")
            or "历史"
        )
        reasons.append(
            f"{history_label}历史 {historical['success']}/{historical['total']} 成功"
            f"（原始 {historical.get('raw_rate', score)}%，小样本已校正）"
        )
    else:
        reasons.append(f"未读到有效历史样本，使用内置基准 {score}%")
    if category == "游戏/益智" and large_category and not GAME_PATTERN.search(
        package_name
    ):
        reasons.append(f"后台大分类 {large_category} 确认为游戏")

    token_profile = (historical_profile or {}).get("__tokens__") or {}
    token_matches = [
        (token, token_profile[token])
        for token in _package_tokens(package_name)
        if token in token_profile
    ]
    if category != "日本包体" and token_matches:
        strongest_token, strongest = max(
            token_matches,
            key=lambda item: (
                int(item[1].get("score", 0)),
                int(item[1].get("total", 0)),
            ),
        )
        token_score = int(strongest.get("score", 0))
        if token_score > score:
            score = token_score
            reasons.append(
                f"包名词 {strongest_token} 历史 {strongest['success']}/"
                f"{strongest['total']} 成功，校正评分 {token_score}%"
            )

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
    incomplete_no_signal = (
        not app_name and not large_category and not has_iap and not has_ads
    )
    # The CP backend's "应用内广告=NO" is useful as a low-probability lane:
    # IAP-only packages are often fast blacklist results. Metadata-free rows
    # with both flags NO are excluded because NO there usually means unknown.
    quick_black_candidate = eligible and not has_ads and not incomplete_no_signal
    default_selected = recommended or quick_black_candidate
    if quick_black_candidate:
        reasons.append(
            "后台应用内广告为 NO"
            + ("、应用内付费为 YES，属于快速加黑候选" if has_iap else "，列入低概率人工预检")
        )
    elif incomplete_no_signal:
        reasons.append("游戏名称和大分类为空，付费/广告均为 NO，已排除低概率筛选")
    selection_group = (
        "高概率+加黑候选"
        if recommended and quick_black_candidate
        else "高概率"
        if recommended
        else "低概率/加黑候选"
        if quick_black_candidate
        else "普通候选"
    )
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
        "quick_black_candidate": quick_black_candidate,
        "default_selected": default_selected,
        "excluded_incomplete": incomplete_no_signal,
        "selection_group": selection_group,
        "app_name": app_name,
        "large_category": large_category,
        "has_iap": has_iap,
        "has_ads": has_ads,
        "priority": priority,
        "priority_rank": CP_PRIORITY_RANK.get(priority, 3),
        "reason": "；".join(reasons),
    }


def load_cp_assignment_candidates(
    *,
    api_url: str,
    x_token: str,
    token: str,
    historical_profile: dict[str, dict[str, Any]] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """Load all visible pending records and score unassigned candidates."""
    require_private_feature("cp_candidate_assignment")
    records, total = fetch_cp_adapt_records(
        api_url=api_url,
        x_token=x_token,
        token=token,
        assign="",
        limit=999,
        session=session,
    )
    scored = [score_cp_candidate(record, historical_profile) for record in records]
    unassigned = [item for item in scored if not item["assigned_to"]]
    unassigned.sort(
        key=lambda item: (
            int(item["priority_rank"]),
            not item["default_selected"],
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
        "quick_black_count": sum(
            item["quick_black_candidate"] for item in unassigned
        ),
        "default_selected_count": sum(
            item["default_selected"] for item in unassigned
        ),
        "high_priority_count": sum(
            item["priority"] == "高" for item in unassigned
        ),
        "excluded_incomplete_count": sum(
            item["excluded_incomplete"] for item in unassigned
        ),
        "historical_sample_count": int(
            ((historical_profile or {}).get("__overall__") or {}).get("total") or 0
        ),
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
    require_private_feature("cp_candidate_assignment")
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
