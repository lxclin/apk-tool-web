"""Generate one daily Asana adaptation summary from task comments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from auto_asana.main import generate_target_dates, parse_asana_precheck_task


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
AGGREGATION_SUCCESS_RE = re.compile(
    r"聚合(?:适配|测试)?(?:验证)?(?:已)?(?:成功|通过|完成)|聚合(?:适配)?已完成",
    re.I,
)
ACTION_SUCCESS_RE = re.compile(
    r"动作适配(?:验证)?(?:已)?(?:成功|通过|完成)|动作适配脚本.{0,12}(?:完成|上线)",
    re.I,
)
ACTION_ISSUE_RE = re.compile(
    r"动作适配.{0,80}(?:异常|失败|不成功|未成功|无反应|无法|不能|未通过|不通过|"
    r"暂不适配|不做适配|停止适配|疑似)|"
    r"(?:异常|失败|不成功|未成功|无反应|无法|不能|未通过|不通过|暂不适配).{0,80}动作适配",
    re.I | re.S,
)
ACTION_PACKAGE_PREFIX_RE = re.compile(
    r"^\s*([A-Za-z0-9_]+(?:\\?\.[A-Za-z0-9_]+)+)(?=动作适配)",
    re.I,
)
BLACKLIST_RE = re.compile(r"加黑|黑名单")
BLACKLIST_NEGATION_RE = re.compile(
    r"不加黑|未加黑|无需加黑|不要加黑|避免加黑|不能据此加黑|"
    r"不应加黑|禁止加黑|不做加黑"
)
NOT_ADAPTED_RE = re.compile(r"暂不适配|不适配|跳过适配|暂不自动适配")
AGGREGATION_CONTEXT_RE = re.compile(
    r"聚合|归因|广告|包体|闪退|设备|下载|安装|应用内购|无广告|"
    r"Google|谷歌|预检|白包|登录|注册|网络|版本|黑屏|卡死",
    re.I,
)
AUTOMATION_MARKER_RE = re.compile(r"^【APK Tool[^】]*】\s*")
AUTOMATION_CODE_RE = re.compile(
    r"^【APK Tool\s*自动化适配：([A-Z0-9_]+)】", re.I
)
URL_RE = re.compile(r"https?://\S+")

# These codes are already terminal after the automation retry policy has been
# exhausted.  They must appear in the daily issue total even when an older
# comment did not explicitly include the words "暂不适配".
STRUCTURED_AUTOMATION_ISSUES = {
    "AF_KEY_EMPTY": "af_key为空，暂不适配",
    "LOGCAT_ENDED": "聚合回放失败（Logcat监听提前结束），暂不适配",
    "AUTOMATION_FAILED": "自动化适配失败，暂不适配",
}

# Ordered from the most specific outcome to broader symptoms so every task is
# counted once even when a comment contains several related phrases.
ISSUE_REASON_RULES = (
    (
        "no_package",
        "全网无包",
        re.compile(r"全网无包|google\s*无包|item\s+not\s+found|Google\s*Play.{0,24}无包", re.I),
    ),
    (
        "replay_failed",
        "聚合回放失败",
        re.compile(r"(?:聚合|广告).{0,10}回放.{0,8}(?:失败|未成功|超时)|未播放广告", re.I),
    ),
    (
        "resource_download_slow",
        "前置资源下载耗时过长",
        re.compile(
            r"(?:下载)?前置资源包?.{0,12}(?:时间太长|耗时.{0,4}(?:过长|太长)|需要\s*\d+\s*分钟)|"
            r"资源包.{0,12}(?:下载太久|下载过久)",
            re.I,
        ),
    ),
    (
        "attribution",
        "归因问题",
        re.compile(r"归因|Singular|ThinkingData|Tenjin|Airbridge|Kochava|Branch", re.I),
    ),
    (
        "tradplus",
        "TradPlus聚合",
        re.compile(r"TradPlus\s*聚合", re.I),
    ),
    (
        "white_package",
        "疑似白包",
        re.compile(r"白包", re.I),
    ),
    (
        "both_ad_ids_empty",
        "插屏聚合id和激励视频聚合id均为空",
        re.compile(
            r"(?:插屏(?:广告)?(?:聚合)?\s*(?:id|ID).{0,16}激励(?:视频)?(?:广告)?(?:聚合)?\s*(?:id|ID)|"
            r"激励(?:视频)?(?:广告)?(?:聚合)?\s*(?:id|ID).{0,16}插屏(?:广告)?(?:聚合)?\s*(?:id|ID))"
            r".{0,12}(?:均为空|都为空|全部为空|同时为空|均未找到|都未找到)",
            re.I,
        ),
    ),
    (
        "aggregation_missing",
        "未识别聚合类型或广告ID",
        re.compile(
            r"未(?:识别|检测|检出)(?:出|到)?聚合(?:了)?类型|聚合类型识别为空|"
            r"未发现聚合平台|(?:聚合|广告).{0,12}(?:id|ID).{0,10}"
            r"(?:未识别|未找到|为空|没有|缺失)|"
            r"(?:没有|缺少|未找到|未识别到?).{0,8}(?:聚合|广告).{0,6}(?:id|ID)",
            re.I,
        ),
    ),
    (
        "crash",
        "包体闪退或运行异常",
        re.compile(r"闪退|崩溃|黑屏|卡死|不断重新加载|反复重新加载", re.I),
    ),
    (
        "google_login",
        "需Google登录",
        re.compile(r"(?:Google|谷歌).{0,8}(?:登录|登陆|账号)|(?:登录|登陆).{0,8}(?:Google|谷歌)", re.I),
    ),
    (
        "account_login",
        "需账号登录",
        re.compile(
            r"(?:需要|要求|必须|需).{0,6}(?:账号|账户).{0,4}(?:登录|登陆|注册)|"
            r"(?:登录|登陆|注册).{0,4}(?:账号|账户)|"
            r"(?:账号|账户).{0,4}(?:登录|登陆|注册)",
            re.I,
        ),
    ),
    (
        "iap_only",
        "应用内购无广告",
        re.compile(r"应用内购|In[- ]app purchases", re.I),
    ),
    (
        "download_restricted",
        "设备、地区或下载限制",
        re.compile(r"设备.{0,8}不支持|国家|地区|无法下载|不支持下载|下载.{0,8}(?:失败|受限)", re.I),
    ),
    (
        "japanese_package",
        "日本包体",
        re.compile(r"日本包体|日文包体", re.I),
    ),
    (
        "af_key_missing",
        "af_key为空缺失",
        re.compile(
            r"(?:af_key|AppsFlyer SDK Key).{0,8}(?:为空|未找到|缺失|暂未找到)",
            re.I,
        ),
    ),
    (
        "missing_parameter",
        "关键参数缺失",
        re.compile(r"关键参数.{0,8}(?:为空|未找到|缺失)", re.I),
    ),
    (
        "monetization_unmarked",
        "未标注广告或应用内购",
        re.compile(r"未标注广告或应用内购|未发现广告或应用内购", re.I),
    ),
)
ISSUE_REASON_LABELS = {key: label for key, label, _pattern in ISSUE_REASON_RULES}


@dataclass(frozen=True)
class DailyComment:
    text: str
    created_at: str = ""


def _comment_local_date(created_at: str) -> date | None:
    value = str(created_at or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ).date()


def comments_for_date(stories: list[dict[str, Any]], target_date: date) -> list[DailyComment]:
    comments: list[DailyComment] = []
    for story in stories:
        subtype = str(story.get("resource_subtype") or "")
        story_type = str(story.get("type") or "")
        if subtype and subtype != "comment_added":
            continue
        if story_type and story_type not in {"comment", "system"}:
            continue
        text = str(story.get("text") or "").strip()
        if not text:
            continue
        created_at = str(story.get("created_at") or "")
        local_date = _comment_local_date(created_at)
        if local_date is not None and local_date != target_date:
            continue
        comments.append(DailyComment(text=text, created_at=created_at))
    comments.sort(key=lambda item: item.created_at)
    return comments


def _is_aggregation_issue(text: str) -> bool:
    if _has_explicit_blacklist_marker(text):
        return True
    if not NOT_ADAPTED_RE.search(text):
        return False
    # Do not count a comment that is explicitly and exclusively about action adaptation.
    if "动作适配" in text and not AGGREGATION_CONTEXT_RE.search(
        text.replace("动作适配", "")
    ):
        return False
    return True


def _has_explicit_blacklist_marker(text: str) -> bool:
    """Detect an actual blacklist decision, ignoring negated wording."""
    value = str(text or "")
    if "黑名单" in value:
        return True
    remaining = BLACKLIST_NEGATION_RE.sub("", value)
    return bool(BLACKLIST_RE.search(remaining))


def concise_issue_reason(text: str, package_name: str = "") -> str:
    """Extract the user-facing terminal reason from a possibly structured comment."""
    text = URL_RE.sub("", str(text or "")).strip()
    text = AUTOMATION_MARKER_RE.sub("", text)
    candidates = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" -")
        if not line or line.startswith(("包名：", "包名:")):
            continue
        if line.startswith(("识别说明：", "识别说明:")) and candidates:
            continue
        candidates.append(line)
    reason = next(
        (
            line
            for line in candidates
            if _has_explicit_blacklist_marker(line) or NOT_ADAPTED_RE.search(line)
        ),
        candidates[0] if candidates else "需要人工确认",
    )
    package_name = str(package_name or "").strip()
    if package_name and reason.startswith(package_name):
        reason = reason[len(package_name):].lstrip(" ：:")
    reason = reason.replace("跳过适配", "暂不适配")
    mediation_match = re.match(
        r"^\s*([A-Za-z][A-Za-z0-9+._ -]{1,30})\s*聚合([，,].*)?$",
        reason,
        re.I,
    )
    if mediation_match and not reason.lower().startswith("检测出"):
        platform = mediation_match.group(1).strip()
        suffix = mediation_match.group(2) or ""
        reason = f"检测出{platform}聚合{suffix}"
    return reason.rstrip("。 ")


def concise_action_issue_reason(text: str, package_name: str = "") -> str:
    """Keep the operator's action-adaptation diagnosis for the daily report."""
    value = URL_RE.sub("", str(text or "")).strip()
    value = AUTOMATION_MARKER_RE.sub("", value)
    candidates: list[str] = []
    for raw_line in value.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" -")
        if not line or line.startswith(("包名：", "包名:")):
            continue
        candidates.append(line)
    reason = next(
        (line for line in candidates if ACTION_ISSUE_RE.search(line)),
        candidates[0] if candidates else "动作适配异常，需要人工确认",
    )
    package_name = str(package_name or "").strip()
    if package_name and reason.startswith(package_name):
        reason = reason[len(package_name):].lstrip(" ：:")
    else:
        # Operators occasionally write a shorter/legacy package identifier in
        # the action comment. Keep it as the display package, but do not repeat
        # it inside the reason text.
        reason = ACTION_PACKAGE_PREFIX_RE.sub("", reason, count=1)
    return reason.replace("跳过适配", "暂不适配").rstrip("。 ")


def action_issue_package_name(text: str, fallback: str) -> str:
    """Prefer an explicit package prefix from the action issue comment."""
    value = AUTOMATION_MARKER_RE.sub("", str(text or "").strip())
    for raw_line in value.splitlines():
        match = ACTION_PACKAGE_PREFIX_RE.match(raw_line.strip())
        if match:
            return match.group(1)
    return str(fallback or "").strip()


def classify_issue_reason(reason: str) -> str:
    """Return one exclusive reason category for a terminal issue comment."""
    value = str(reason or "")
    for key, _label, pattern in ISSUE_REASON_RULES:
        if pattern.search(value):
            return key
    return "other"


def summarize_issue_reasons(
    issues: list[dict[str, str]], state: str
) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    labels = dict(ISSUE_REASON_LABELS)
    for item in issues:
        if item.get("state") != state:
            continue
        reason = item.get("reason", "")
        key = classify_issue_reason(reason)
        if key == "other":
            # Unknown wording should still be visible in the summary instead
            # of disappearing into an unhelpful "其他原因" bucket.
            label = re.sub(
                r"[，,。;；]?\s*(?:暂不适配|不适配|加黑)\s*$",
                "",
                str(reason or "").strip(),
            ).strip(" ，,。;；") or "需人工确认"
            key = f"other:{label}"
            labels[key] = label
        counts[key] = counts.get(key, 0) + 1
    ordered_keys = [key for key, _label, _pattern in ISSUE_REASON_RULES]
    ordered_keys.extend(key for key in counts if key.startswith("other:"))
    return [
        {"key": key, "label": labels[key], "count": counts[key]}
        for key in ordered_keys
        if counts.get(key)
    ]


def _format_issue_state_summary(
    issues: list[dict[str, str]], state: str, state_label: str
) -> str:
    count = sum(item.get("state") == state for item in issues)
    text = f"{count}个{state_label}"
    categories = summarize_issue_reasons(issues, state)
    if categories:
        text += "，其中" + "、".join(
            f"{item['count']}个{item['label']}" for item in categories
        )
    return text


def classify_task_comments(
    package_name: str,
    comments: list[DailyComment],
) -> dict[str, str]:
    """Use the last relevant comment as the task's terminal daily state."""
    aggregation_state = ""
    aggregation_reason = ""
    action_state = ""
    action_reason = ""
    action_package_name = ""
    for comment in comments:
        text = comment.text
        # The last action-specific conclusion of the day wins. This lets a
        # later successful manual retry supersede an earlier automation error,
        # and vice versa.
        if ACTION_ISSUE_RE.search(text):
            action_state = "issue"
            action_reason = concise_action_issue_reason(text, package_name)
            action_package_name = action_issue_package_name(text, package_name)
        elif ACTION_SUCCESS_RE.search(text):
            action_state = "success"
            action_reason = ""
            action_package_name = ""
        if (
            "NO_ADS_OR_IAP" in text
            and ("不加黑" in text or "不能据此加黑" in text)
        ):
            # A correction comment supersedes an earlier legacy blacklist.
            aggregation_state = ""
            aggregation_reason = ""
            continue
        automation_match = AUTOMATION_CODE_RE.match(text.strip())
        if automation_match:
            structured_reason = STRUCTURED_AUTOMATION_ISSUES.get(
                automation_match.group(1).upper()
            )
            if structured_reason:
                aggregation_state = "not_adapted"
                aggregation_reason = structured_reason
        if AGGREGATION_SUCCESS_RE.search(text):
            aggregation_state = "success"
            aggregation_reason = ""
        if _is_aggregation_issue(text):
            aggregation_state = (
                "blacklist"
                if _has_explicit_blacklist_marker(text)
                else "not_adapted"
            )
            aggregation_reason = concise_issue_reason(text, package_name)
    return {
        "aggregation_state": aggregation_state,
        "aggregation_reason": aggregation_reason,
        "action_success": "1" if action_state == "success" else "",
        "action_issue": action_reason if action_state == "issue" else "",
        "action_package_name": (
            action_package_name if action_state == "issue" else ""
        ),
    }


def render_daily_summary(
    target_date: date,
    aggregation_success: list[str],
    issues: list[dict[str, str]],
    action_success: list[str],
    action_issues: list[dict[str, str]] | None = None,
) -> str:
    action_issues = action_issues or []
    not_adapted_summary = _format_issue_state_summary(
        issues, "not_adapted", "暂不适配"
    )
    blacklist_summary = _format_issue_state_summary(issues, "blacklist", "加黑")
    lines = [
        f"【{target_date.month}.{target_date.day}】",
        f"完成{len(aggregation_success)}个聚合适配",
        *aggregation_success,
        "",
        (
            f"{len(issues)}个聚合适配问题:"
            f"{not_adapted_summary}；{blacklist_summary}"
        ),
        "",
    ]
    lines.extend(
        f"{item['package_name']}：{item['reason']}" for item in issues
    )
    lines.extend(
        [
            "",
            "",
            f"完成{len(action_success)}个动作适配",
            *action_success,
        ]
    )
    if action_issues:
        lines.extend(
            [
                "",
                "",
                f"{len(action_issues)}个动作适配问题",
                *(
                    f"{item['package_name']}{item['reason']}"
                    for item in action_issues
                ),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def generate_daily_asana_summary(client, project_gid: str, target_date: date) -> dict:
    """Read one date section and produce the configured text summary."""
    _, section_name = generate_target_dates(target_date)
    sections = client.sections.get_sections_for_project(str(project_gid or "").strip())
    section = next(
        (
            item
            for item in sections
            if str(item.get("name") or "").strip() == section_name
        ),
        None,
    )
    if section is None:
        raise ValueError(f"Asana 中没有找到 {section_name} 分组")

    tasks = client.tasks.get_tasks_for_section(
        str(section.get("gid") or ""),
        opt_fields=["gid", "name", "notes", "completed", "permalink_url"],
    )
    aggregation_success: list[str] = []
    action_success: list[str] = []
    action_issues: list[dict[str, str]] = []
    issues: list[dict[str, str]] = []
    scanned_comments = 0
    for raw_task in tasks:
        task = parse_asana_precheck_task(raw_task)
        stories = client.stories.get_stories_for_task(
            task.gid,
            opt_fields=["text", "resource_subtype", "type", "created_at"],
        )
        comments = comments_for_date(stories, target_date)
        scanned_comments += len(comments)
        result = classify_task_comments(task.package_name, comments)
        if result["aggregation_state"] == "success":
            aggregation_success.append(task.package_name)
        elif result["aggregation_state"] in {"not_adapted", "blacklist"}:
            issues.append(
                {
                    "package_name": task.package_name,
                    "state": result["aggregation_state"],
                    "reason": result["aggregation_reason"],
                }
            )
        if result["action_success"]:
            action_success.append(task.package_name)
        elif result["action_issue"]:
            action_issues.append(
                {
                    "package_name": (
                        result["action_package_name"] or task.package_name
                    ),
                    "reason": result["action_issue"],
                }
            )

    return {
        "ok": True,
        "section_name": section_name,
        "task_count": len(tasks),
        "comment_count": scanned_comments,
        "aggregation_success_count": len(aggregation_success),
        "issue_count": len(issues),
        "not_adapted_count": sum(item["state"] == "not_adapted" for item in issues),
        "blacklist_count": sum(item["state"] == "blacklist" for item in issues),
        "not_adapted_categories": summarize_issue_reasons(
            issues, "not_adapted"
        ),
        "blacklist_categories": summarize_issue_reasons(issues, "blacklist"),
        "action_success_count": len(action_success),
        "action_issue_count": len(action_issues),
        "text": render_daily_summary(
            target_date,
            aggregation_success,
            issues,
            action_success,
            action_issues,
        ),
    }
