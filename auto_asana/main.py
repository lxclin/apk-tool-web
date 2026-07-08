"""
main.py — Google Sheets → Asana 幂等同步

将 Google Sheets 中满足条件的数据（聚合适配=rain，完成时间=当天）
同步到 Asana 项目任务中，具备幂等性。

架构分层：
  1. 纯逻辑层（无 IO）：generate_target_dates / filter_packages / compute_diff
  2. API 交互层（有 IO）：get_sheet_data / find_or_create_section /
     get_existing_task_names / create_tasks_for_packages
  3. 编排层：sync_packages（依赖注入，便于测试）

运行方式：
  python3 main.py
"""

import os
import sys
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import requests as http_requests

# ═══════════════════════════════════════════════════════════════
# 硬编码常量
# ═══════════════════════════════════════════════════════════════

SHEET_ID = "11ExyD6bM_m4ECKMNTx4XqsqzA75LuFJy5y6YZz8_tYs"
SHEET_NAME = "26年5-6月"                    # 目标工作表名称
PROJECT_GID = "1215092379542969"            # Asana 项目 GID
WORKSPACE_GID = "1208177697797743"          # Asana 工作区 GID
SA_FILE = ""
ASANA_PAT = ""
PROXY_URL = "http://127.0.0.1:7897"         # Clash 代理地址
PARENT_TASK_GID = "1215490559662224"         # 父任务 GID（新建任务将作为其子任务）
GS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CP_ADAPT_LIST_URL = "http://8.131.54.114/admin/gd_web/overseas/cp_adapt/list"
CP_ADAPT_TOKEN = ""


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PackageRow:
    """Google Sheets 中符合筛选条件的一行包名数据。"""

    package_name: str
    row_number: int
    task_link: str
    up2_appid: str = ""
    gp_link: str = ""


@dataclass(frozen=True)
class AsanaTaskInfo:
    """Asana 任务名称、GID 与可访问链接。"""

    name: str
    gid: str
    link: str
    notes: str = ""


@dataclass(frozen=True)
class SheetCellUpdate:
    """Google Sheets 单个单元格更新计划。"""

    row_number: int
    column_index: int
    range_name: str
    value: str


@dataclass(frozen=True)
class SheetRowUpdate:
    """Google Sheets 单行更新计划。"""

    row_number: int
    range_name: str
    values: list[str]
    is_new: bool = False


def build_task_name(package_name: str) -> str:
    """从包名生成 Asana 任务名，加上约定前缀。"""
    return f"聚合/动作适配{package_name}"


def unique_task_names_from_package_rows(package_rows: list[PackageRow]) -> list[str]:
    """从 PackageRow 列表生成去重后的 Asana 任务名，保持首次出现顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for row in package_rows:
        task_name = build_task_name(row.package_name)
        if task_name not in seen:
            seen.add(task_name)
            result.append(task_name)
    return result


def task_descriptions_from_package_rows(package_rows: list[PackageRow]) -> dict[str, str]:
    """按 Asana 任务名生成描述，优先保留 UP2 appid 不为空的那条行。"""
    result: dict[str, str] = {}
    has_up2_appid: dict[str, bool] = {}
    for row in package_rows:
        task_name = build_task_name(row.package_name)
        row_has_appid = bool(row.up2_appid.strip())
        if task_name not in result or (row_has_appid and not has_up2_appid.get(task_name, False)):
            result[task_name] = build_asana_task_notes(row)
            has_up2_appid[task_name] = row_has_appid
    return result


def task_descriptions_from_cp_records(records: list[dict[str, Any]]) -> dict[str, str]:
    """按 CP 后台原始记录生成 Asana 描述，优先保留 UP2 appid 不为空的那条记录。"""
    result: dict[str, str] = {}
    has_up2_appid: dict[str, bool] = {}
    for record in records:
        package_name = str(record.get("package_name") or "").strip()
        if not package_name:
            continue
        task_name = build_task_name(package_name)
        row = PackageRow(
            package_name=package_name,
            row_number=0,
            task_link="",
            up2_appid=extract_up2_appid(record),
            gp_link=f"https://play.google.com/store/apps/details?id={package_name}",
        )
        row_has_appid = bool(row.up2_appid.strip())
        if task_name not in result or (row_has_appid and not has_up2_appid.get(task_name, False)):
            result[task_name] = build_asana_task_notes(row)
            has_up2_appid[task_name] = row_has_appid
    return result


def build_asana_task_notes(row: PackageRow) -> str:
    """生成 Asana 任务描述。"""
    gp_link = row.gp_link.strip()
    if not gp_link and row.package_name:
        gp_link = f"https://play.google.com/store/apps/details?id={row.package_name}"
    return "\n".join([
        f"包名：{row.package_name}",
        f"UP2 appid：{row.up2_appid}",
        f"GP链接： {gp_link}",
    ])


MANAGED_ASANA_NOTE_PREFIXES = ("包名：", "UP2 appid：", "GP链接：")


def _managed_note_lines_by_prefix(notes: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (notes or "").splitlines():
        for prefix in MANAGED_ASANA_NOTE_PREFIXES:
            if line.startswith(prefix):
                result[prefix] = line
                break
    return result


def merge_asana_task_notes(current_notes: str, managed_notes: str) -> str:
    """合并同步字段与 Asana 现有描述，保留人工补充内容。"""
    current_managed = _managed_note_lines_by_prefix(current_notes)
    incoming_managed = _managed_note_lines_by_prefix(managed_notes)
    managed_lines: list[str] = []
    for prefix in MANAGED_ASANA_NOTE_PREFIXES:
        line = incoming_managed.get(prefix)
        if (
            prefix == "UP2 appid："
            and line is not None
            and not line.removeprefix(prefix).strip()
            and current_managed.get(prefix, "").removeprefix(prefix).strip()
        ):
            line = current_managed[prefix]
        if line is not None:
            managed_lines.append(line)
    manual_lines = [
        line for line in (current_notes or "").splitlines()
        if not line.startswith(MANAGED_ASANA_NOTE_PREFIXES)
    ]
    while manual_lines and not manual_lines[0].strip():
        manual_lines.pop(0)

    if manual_lines:
        return "\n".join(managed_lines + [""] + manual_lines)
    return "\n".join(managed_lines)


def build_asana_task_link(project_gid: str, task_gid: str) -> str:
    """根据项目 GID 与任务 GID 构造 Asana 任务链接。"""
    return f"https://app.asana.com/0/{project_gid}/{task_gid}"


def column_index_to_a1(column_index: int) -> str:
    """将 0-based 列索引转换为 A1 列名。"""
    if column_index < 0:
        raise ValueError("column_index must be >= 0")

    result = ""
    current = column_index + 1
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def quote_sheet_name(sheet_name: str) -> str:
    """将 Sheet 名转成 A1 notation 中安全的工作表名。"""
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'"


def build_cell_range(sheet_name: str, row_number: int, column_index: int) -> str:
    """构造单个单元格的 A1 range。"""
    column_name = column_index_to_a1(column_index)
    if sheet_name:
        return f"{quote_sheet_name(sheet_name)}!{column_name}{row_number}"
    return f"{column_name}{row_number}"


def build_row_range(sheet_name: str, row_number: int, column_count: int) -> str:
    """构造整行指定列数的 A1 range。"""
    if column_count <= 0:
        raise ValueError("column_count must be > 0")
    last_column = column_index_to_a1(column_count - 1)
    if sheet_name:
        return f"{quote_sheet_name(sheet_name)}!A{row_number}:{last_column}{row_number}"
    return f"A{row_number}:{last_column}{row_number}"


def parse_up2_appid(raw: Any) -> str:
    """从后台 up2_appid JSON 字符串中提取 appId，去掉 A2: 前缀。"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        rows = parsed.get("data") if isinstance(parsed, dict) else None
        if rows and isinstance(rows, list):
            app_id = rows[0].get("appId") or ""
            if ":" in app_id:
                return app_id.split(":", 1)[1]
            return app_id
    except Exception:
        pass
    return raw


def extract_up2_appid(record: dict[str, Any]) -> str:
    """兼容不同字段名，提取 UP2 appid。"""
    for key in ("up2_appid", "appId", "UP2 appid"):
        value = record.get(key)
        parsed = parse_up2_appid(value)
        if parsed:
            if ":" in parsed:
                return parsed.split(":", 1)[1]
            return parsed
    return ""


def _yes_no(value: Any) -> str:
    """按后台页面展示规则，把空/0 转成 NO，其余转成 YES。"""
    if value is None or value == "" or value == "0":
        return "NO"
    return "YES"


def _bool_status(value: Any, true_text: str = "YES", false_text: str = "NO") -> str:
    return true_text if bool(value) else false_text


def build_cp_adapt_sheet_values(record: dict[str, Any], sheet_date: str) -> dict[str, str]:
    """把 CP 后台一条记录映射为可写入 Sheet 的表头值。"""
    package_name = str(record.get("package_name") or "").strip()
    gp_link = f"https://play.google.com/store/apps/details?id={package_name}" if package_name else ""
    is_adapted = bool(record.get("is_adapted"))

    return {
        "包名": package_name,
        "游戏名称": record.get("app_name") or "",
        "应用名称": record.get("app_name") or "",
        "大分类": record.get("categ") or "",
        "应用内付费": _yes_no(record.get("in_app_product_price")),
        "应用内广告": _yes_no(record.get("contains_ads")),
        "A2是否开启": _bool_status(record.get("a2_open")),
        "适配完成状态": "已适配" if is_adapted else "未适配",
        "适配人员": record.get("assign") or "",
        "聚合适配": record.get("assign") or "",
        "聚合平台": record.get("aggr_platform") or "",
        "归因平台": record.get("attribution_platform") or "",
        "聚合id-插屏": record.get("aggr_chaping_id") or "",
        "聚合id-激励视频": record.get("aggr_jilishipin_id") or "",
        "自定义applovin_sdk_key": record.get("manual_applovin_sdk_key") or "",
        "activity初始页面": record.get("activity_main_page") or "",
        "activity引导页面": record.get("activity_guide_page") or "",
        "af_key": record.get("af_key") or "",
        "Block原因": record.get("block_ps") or "",
        "备注": record.get("ps") or "",
        "UP2 appid": extract_up2_appid(record),
        "GP链接": gp_link,
        "优先级": record.get("priority") or "",
        "完成时间": sheet_date,
    }


def _header_index_by_name(headers: list[Any]) -> dict[str, int]:
    return {str(header).strip(): idx for idx, header in enumerate(headers)}


def plan_cp_adapt_sheet_upserts(
    sheet_data: list[list],
    records: list[dict[str, Any]],
    sheet_name: str,
    sheet_date: str,
) -> list[SheetRowUpdate]:
    """
    根据后台记录规划 Sheet 行更新：
      - 包名已存在则更新原行
      - 包名不存在则追加到表尾
      - 只更新能在表头中匹配到的字段，其他列保留原值
    """
    if not sheet_data:
        raise ValueError("Sheet 为空，无法定位表头")

    header_row = _find_header_row(sheet_data)
    headers = sheet_data[header_row]
    header_index = _header_index_by_name(headers)
    if "包名" not in header_index:
        raise ValueError("Sheet 缺少必需列: 包名")

    package_col = header_index["包名"]
    existing_rows: dict[str, int] = {}
    for row_index, row in enumerate(sheet_data[header_row + 1:], start=header_row + 1):
        if package_col < len(row):
            package_name = str(row[package_col]).strip()
            if package_name and package_name not in existing_rows:
                existing_rows[package_name] = row_index + 1

    updates: list[SheetRowUpdate] = []
    seen: set[str] = set()
    next_row_number = len(sheet_data) + 1
    column_count = len(headers)

    for record in records:
        package_name = str(record.get("package_name") or "").strip()
        if not package_name or package_name in seen:
            continue
        seen.add(package_name)

        row_number = existing_rows.get(package_name)
        is_new = row_number is None
        if is_new:
            row_number = next_row_number
            next_row_number += 1
            row_values = [""] * column_count
        else:
            current_row = sheet_data[row_number - 1] if row_number - 1 < len(sheet_data) else []
            row_values = [str(v) for v in current_row] + [""] * max(0, column_count - len(current_row))
            row_values = row_values[:column_count]

        values_by_header = build_cp_adapt_sheet_values(record, sheet_date)
        for header, value in values_by_header.items():
            if header in header_index:
                row_values[header_index[header]] = str(value)

        updates.append(SheetRowUpdate(
            row_number=row_number,
            range_name=build_row_range(sheet_name, row_number, column_count),
            values=row_values,
            is_new=is_new,
        ))

    return updates


def get_task_link_column_index(sheet_data: list[list]) -> Optional[int]:
    """获取"任务链接"列的 0-based 索引；不存在时返回 None。"""
    if not sheet_data:
        return None
    header_row = _find_header_row(sheet_data)
    headers = sheet_data[header_row]
    try:
        return headers.index("任务链接")
    except ValueError:
        return None


def plan_task_link_backfills(
    package_rows: list[PackageRow],
    task_links_by_name: dict[str, AsanaTaskInfo],
    sheet_name: str,
    task_link_column_index: int,
) -> list[SheetCellUpdate]:
    """规划需要回填的任务链接，只为空单元格生成更新。"""
    updates: list[SheetCellUpdate] = []
    for row in package_rows:
        if row.task_link.strip():
            continue
        task_name = build_task_name(row.package_name)
        task_info = task_links_by_name.get(task_name)
        if task_info is None:
            continue
        updates.append(SheetCellUpdate(
            row_number=row.row_number,
            column_index=task_link_column_index,
            range_name=build_cell_range(sheet_name, row.row_number, task_link_column_index),
            value=task_info.link,
        ))
    return updates


# ═══════════════════════════════════════════════════════════════
# 纯逻辑：日期生成
# ═══════════════════════════════════════════════════════════════

def generate_target_dates(today: Optional[date] = None) -> tuple[str, str]:
    """
    根据当前日期生成两种目标格式。

    Args:
        today: 可选，指定日期（用于测试注入），默认取 date.today()。

    Returns:
        (sheet_date, asana_section_name)
          - sheet_date:  用于匹配 Google Sheets 中的"完成时间"列，格式 yy.m.d
                         例如 2026-06-08 → "26.6.8"（个位数不补零）
          - asana_name:  用于 Asana 区段命名，格式 m.d执行
                         例如 2026-06-08 → "6.8执行"（个位数不补零）
    """
    if today is None:
        today = date.today()

    yy = str(today.year)[2:]   # 年份后两位，如 "26"
    m = str(today.month)        # 月份，不补零，如 "6"
    d = str(today.day)          # 日期，不补零，如 "8"

    sheet_date = f"{yy}.{m}.{d}"
    asana_name = f"{m}.{d}执行"

    return sheet_date, asana_name


# ═══════════════════════════════════════════════════════════════
# 纯逻辑：表头定位
# ═══════════════════════════════════════════════════════════════

def _find_header_row(sheet_data: list[list]) -> int:
    """
    在二维数组中定位真正的表头行。
    有些 Sheet 第一行是合并的说明文字，需要跳过。

    Returns:
        表头行索引；找不到返回 0。
    """
    for i, row in enumerate(sheet_data):
        if "包名" in row and "聚合适配" in row:
            return i
    return 0


def _header_index(headers: list[Any], name: str) -> Optional[int]:
    """按去空白后的表头名查找列索引。"""
    target = str(name).strip()
    for idx, header in enumerate(headers):
        if str(header).strip() == target:
            return idx
    return None


# ═══════════════════════════════════════════════════════════════
# 纯逻辑：数据过滤
# ═══════════════════════════════════════════════════════════════

def filter_packages(sheet_data: list[list], target_date: str) -> list[PackageRow]:
    """
    从 Google Sheets 原始二维数组中筛选符合条件的包名行。

    筛选条件（同时满足）：
      1. "聚合适配" 列值为 'rain'（精确匹配，区分大小写）
      2. "完成时间" 列值为 target_date（如 '26.6.8'）

    Args:
        sheet_data: Google Sheets values.get 返回的二维列表。
        target_date: 当天对应的 Sheet 日期格式（由 generate_target_dates 生成）。

    Returns:
        符合条件的 PackageRow 列表，保持 Sheet 中的原始出现顺序。
        缺少必要列或数据为空时返回空列表。
    """
    if not sheet_data or len(sheet_data) < 2:
        return []

    header_row = _find_header_row(sheet_data)
    headers = sheet_data[header_row]

    # 定位列索引
    pkg_idx = _header_index(headers, "包名")
    adapter_idx = _header_index(headers, "聚合适配")
    if pkg_idx is None or adapter_idx is None:
        return []

    # Sheet 中可能有多个"完成时间"列（如 "\n完成时间" 和 "完成时间"），
    # 任意一列匹配 target_date 即视为符合条件
    time_indices = [
        i for i, h in enumerate(headers)
        if h.strip() == "完成时间"
    ]
    if not time_indices:
        return []

    task_link_idx = _header_index(headers, "任务链接")
    up2_appid_idx = _header_index(headers, "UP2 appid")
    gp_link_idx = _header_index(headers, "GP链接")

    # 必需的列（包名、聚合适配）
    required_idx = max(pkg_idx, adapter_idx)
    package_rows: list[PackageRow] = []

    for row_index, row in enumerate(sheet_data[header_row + 1:], start=header_row + 1):
        # 只要包名和聚合适配列存在即可，时间列可能有多个且不同行长短不一
        if len(row) <= required_idx:
            continue
        if row[adapter_idx] != "rain":
            continue
        if any(
            ti < len(row) and row[ti] == target_date
            for ti in time_indices
        ):
            pkg = row[pkg_idx]
            task_link = ""
            if task_link_idx is not None and task_link_idx < len(row):
                task_link = row[task_link_idx]
            up2_appid = ""
            if up2_appid_idx is not None and up2_appid_idx < len(row):
                up2_appid = row[up2_appid_idx]
            gp_link = ""
            if gp_link_idx is not None and gp_link_idx < len(row):
                gp_link = row[gp_link_idx]
            package_rows.append(PackageRow(
                pkg, row_index + 1, task_link, up2_appid, gp_link
            ))

    return package_rows


# ═══════════════════════════════════════════════════════════════
# 纯逻辑：差集计算
# ═══════════════════════════════════════════════════════════════

def compute_diff(packages: list[str], existing_names: set[str]) -> list[str]:
    """
    计算需要新建的包名差集（纯逻辑，不涉及 IO）。

    Args:
        packages: 从 Sheet 中筛选出的全部包名（有序）。
        existing_names: Asana 区段下已存在的任务名称集合。

    Returns:
        在 packages 中但不在 existing_names 中的包名列表，保持原始顺序。
    """
    return [p for p in packages if p not in existing_names]


# ═══════════════════════════════════════════════════════════════
# API 交互：Google Sheets（基于 googleapiclient + 代理）
# ═══════════════════════════════════════════════════════════════

def get_sheet_data(service, sheet_id: str, range_name: str = "A:Z") -> list[list]:
    """
    从 Google Sheets 读取原始数据。

    Args:
        service:  googleapiclient.discovery.build('sheets', 'v4', ...) 产物。
        sheet_id: Google Sheet ID。
        range_name: 读取范围，如 "A:Z" 或 "26年5-6月!A:Z"。

    Returns:
        二维列表；空表格或结果为空时返回 []。
    """
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_name)
        .execute()
    )
    return result.get("values", [])


def update_sheet_value(
    service,
    sheet_id: str,
    range_name: str,
    value: str,
) -> dict:
    """向 Google Sheets 的单个单元格写入值。"""
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption="RAW",
            body={"values": [[value]]},
        )
        .execute()
    )
    return result


def update_sheet_row(
    service,
    sheet_id: str,
    range_name: str,
    values: list[str],
) -> dict:
    """向 Google Sheets 的整行范围写入值。"""
    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=range_name,
            valueInputOption="RAW",
            body={"values": [values]},
        )
        .execute()
    )
    return result


def batch_update_sheet_values(
    service,
    sheet_id: str,
    data: list[dict[str, Any]],
    value_input_option: str = "RAW",
) -> dict:
    """批量写入 Google Sheets 多个范围，减少网络请求次数。"""
    if not data:
        return {"totalUpdatedCells": 0, "responses": []}
    result = (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": value_input_option,
                "data": data,
            },
        )
        .execute()
    )
    return result


def backfill_task_links(
    service,
    sheet_id: str,
    updates: list[SheetCellUpdate],
) -> list[dict]:
    """按计划批量回填 Asana 任务链接。"""
    if not updates:
        return []
    result = batch_update_sheet_values(
        service,
        sheet_id,
        [
            {
                "range": update.range_name,
                "values": [[update.value]],
            }
            for update in updates
        ],
    )
    return [result]


def apply_sheet_row_updates(
    service,
    sheet_id: str,
    updates: list[SheetRowUpdate],
) -> list[dict]:
    """按计划批量写入 Google Sheets。"""
    if not updates:
        return []
    result = batch_update_sheet_values(
        service,
        sheet_id,
        [
            {
                "range": update.range_name,
                "values": [update.values],
            }
            for update in updates
        ],
    )
    return [result]


def build_cp_adapt_request_payload(
    assign: str = "rain",
    hide_adapted: bool = True,
    hide_remarked: bool = True,
    hide_no_up2_appid: bool = True,
    limit: int = 999,
    page: int = 1,
) -> dict[str, Any]:
    """构造与后台页面筛选条件一致的请求体。"""
    return {
        "is_adapted": "no" if hide_adapted else "all",
        "hide_remarked": hide_remarked,
        "hide_no_up2_appid": hide_no_up2_appid,
        "assign": assign,
        "limit": limit,
        "page": page,
    }


def fetch_cp_adapt_records(
    api_url: str,
    x_token: str,
    token: str = CP_ADAPT_TOKEN,
    assign: str = "rain",
    limit: int = 999,
    session: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """从 CP 后台列表接口获取符合筛选条件的数据。"""
    if not x_token:
        raise ValueError("缺少后台 X-Token")
    if not token:
        raise ValueError("缺少后台 token")

    http = session or http_requests.Session()
    payload = build_cp_adapt_request_payload(assign=assign, limit=limit)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "http://data_center_web_internet.hongdinghe.cn",
        "Referer": "http://data_center_web_internet.hongdinghe.cn/",
        "X-Token": x_token,
        "token": token,
    }
    resp = http.post(api_url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 200:
        raise RuntimeError(f"CP 后台接口返回异常: {body}")
    data = body.get("data") or {}
    rows = data.get("data") or []
    total = int(data.get("total") or len(rows))
    return rows, total


def sync_cp_adapt_records_to_sheet(
    gs_service,
    sheet_id: str,
    sheet_name: str,
    api_url: str,
    x_token: str,
    token: str = CP_ADAPT_TOKEN,
    assign: str = "rain",
    today: Optional[date] = None,
    session: Any = None,
) -> dict[str, Any]:
    """拉取 CP 后台待适配数据，并写入 Google Sheets。"""
    sheet_date, _ = generate_target_dates(today)
    records, total = fetch_cp_adapt_records(
        api_url=api_url,
        x_token=x_token,
        token=token,
        assign=assign,
        session=session,
    )
    notes_by_name = task_descriptions_from_cp_records(records)
    range_name = f"{sheet_name}!A:AZ" if sheet_name else "A:AZ"
    sheet_data = get_sheet_data(gs_service, sheet_id, range_name)
    updates = plan_cp_adapt_sheet_upserts(sheet_data, records, sheet_name, sheet_date)
    apply_sheet_row_updates(gs_service, sheet_id, updates)

    return {
        "sheet_date": sheet_date,
        "fetched_count": len(records),
        "reported_total": total,
        "updated_count": sum(1 for update in updates if not update.is_new),
        "appended_count": sum(1 for update in updates if update.is_new),
        "written_count": len(updates),
        "written_ranges": [update.range_name for update in updates],
        "notes_by_name": notes_by_name,
    }


def _build_gs_service(sa_file=None, proxy_url=None):
    """
    构建 Google Sheets service（兼容 googleapiclient 链式调用接口）。

    使用 requests + 代理 代替 httplib2（后者不尊重代理设置），
    对外暴露与 googleapiclient 相同的 .spreadsheets().values().get().execute() 链。

    Args:
        sa_file:  Service Account JSON 文件路径，默认用模块级 SA_FILE。
        proxy_url: 代理地址，默认用模块级 PROXY_URL。
    """
    if sa_file is None:
        sa_file = SA_FILE
    if proxy_url is None:
        proxy_url = PROXY_URL

    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as ga_requests
    except ImportError:
        sys.exit("缺少依赖，请运行：pip install google-auth google-api-python-client")

    credentials = service_account.Credentials.from_service_account_file(
        sa_file,
        scopes=GS_SCOPES,
    )

    session = http_requests.Session()
    session.trust_env = False
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    auth_request = ga_requests.Request(session=session)
    credentials.refresh(auth_request)

    print(f"[GS] 服务账号已认证 → {sa_file}")
    return _SheetsService(session, credentials)


class _SheetsService:
    """googleapiclient 兼容层 — 底层用 requests + 代理。"""

    def __init__(self, session, credentials):
        self._session = session
        self._credentials = credentials

    def spreadsheets(self):
        return _SpreadsheetsResource(self._session, self._credentials)


class _SpreadsheetsResource:
    def __init__(self, session, credentials):
        self._session = session
        self._credentials = credentials

    def values(self):
        return _ValuesResource(self._session, self._credentials)


class _ValuesResource:
    BASE = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, session, credentials):
        self._session = session
        self._credentials = credentials

    def get(self, spreadsheetId, range):
        return _GetRequest(self._session, self._credentials, spreadsheetId, range)

    def update(self, spreadsheetId, range, valueInputOption, body):
        return _UpdateRequest(
            self._session, self._credentials, spreadsheetId, range, valueInputOption, body
        )

    def batchUpdate(self, spreadsheetId, body):
        return _BatchUpdateRequest(self._session, self._credentials, spreadsheetId, body)


def _request_with_retries(request_fn, attempts: int = 4, base_delay: float = 1.0):
    """对 Google API HTTP 请求做轻量重试，兜住代理/网络偶发断连。"""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return request_fn()
        except http_requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(base_delay * attempt)
    raise last_error


class _GetRequest:
    def __init__(self, session, credentials, spreadsheet_id, range_name):
        self._session = session
        self._credentials = credentials
        self._spreadsheet_id = spreadsheet_id
        self._range = range_name

    def execute(self):
        import google.auth.transport.requests as ga_requests

        # refresh token if needed
        self._credentials.refresh(ga_requests.Request(session=self._session))

        url = f"{_ValuesResource.BASE}/{self._spreadsheet_id}/values/{self._range}"
        headers = {"Authorization": f"Bearer {self._credentials.token}"}
        resp = _request_with_retries(
            lambda: self._session.get(url, headers=headers, timeout=60)
        )
        resp.raise_for_status()
        return resp.json()


class _UpdateRequest:
    def __init__(
        self,
        session,
        credentials,
        spreadsheet_id,
        range_name,
        value_input_option,
        body,
    ):
        self._session = session
        self._credentials = credentials
        self._spreadsheet_id = spreadsheet_id
        self._range = range_name
        self._value_input_option = value_input_option
        self._body = body

    def execute(self):
        import google.auth.transport.requests as ga_requests

        # refresh token if needed
        self._credentials.refresh(ga_requests.Request(session=self._session))

        url = f"{_ValuesResource.BASE}/{self._spreadsheet_id}/values/{self._range}"
        headers = {"Authorization": f"Bearer {self._credentials.token}"}
        resp = _request_with_retries(
            lambda: self._session.put(
                url,
                headers=headers,
                params={"valueInputOption": self._value_input_option},
                json=self._body,
                timeout=60,
            )
        )
        resp.raise_for_status()
        return resp.json()


class _BatchUpdateRequest:
    def __init__(self, session, credentials, spreadsheet_id, body):
        self._session = session
        self._credentials = credentials
        self._spreadsheet_id = spreadsheet_id
        self._body = body

    def execute(self):
        import google.auth.transport.requests as ga_requests

        # refresh token if needed
        self._credentials.refresh(ga_requests.Request(session=self._session))

        url = f"{_ValuesResource.BASE}/{self._spreadsheet_id}/values:batchUpdate"
        headers = {"Authorization": f"Bearer {self._credentials.token}"}
        resp = _request_with_retries(
            lambda: self._session.post(
                url,
                headers=headers,
                json=self._body,
                timeout=120,
            )
        )
        resp.raise_for_status()
        return resp.json()


# ═══════════════════════════════════════════════════════════════
# API 交互：Asana（基于 Asana SDK 5.x）
# ═══════════════════════════════════════════════════════════════

# ── Asana SDK 5.x 适配层 ──────────────────────────────────

class AsanaClient:
    """
    Asana SDK 5.x 适配器 — 对外暴露与旧版 SDK 兼容的 .sections / .tasks 接口。
    便于单元测试用 MagicMock 替换。
    """

    def __init__(self, access_token: str):
        from asana import ApiClient, Configuration
        from asana.api.sections_api import SectionsApi
        from asana.api.tasks_api import TasksApi

        config = Configuration()
        config.access_token = access_token
        self._api_client = ApiClient(config)
        self._sections_api = SectionsApi(self._api_client)
        self._tasks_api = TasksApi(self._api_client)

    @property
    def sections(self):
        return _SectionsFacade(self._sections_api)

    @property
    def tasks(self):
        return _TasksFacade(self._tasks_api)


class _SectionsFacade:
    """将 SDK 5.x SectionsApi 适配为旧版链式调用风格。"""

    def __init__(self, api):
        self._api = api

    def get_sections_for_project(self, project_gid):
        return list(self._api.get_sections_for_project(project_gid, {}))

    def create_section_for_project(self, project_gid, body):
        result = self._api.create_section_for_project(
            project_gid, {"body": {"data": body}}
        )
        return result


class _TasksFacade:
    """将 SDK 5.x TasksApi 适配为旧版链式调用风格。"""

    def __init__(self, api):
        self._api = api

    def get_tasks_for_section(self, section_gid, opt_fields=None):
        opts = {}
        if opt_fields:
            opts["opt_fields"] = ",".join(opt_fields) if isinstance(opt_fields, list) else opt_fields
        return list(self._api.get_tasks_for_section(section_gid, opts))

    def create_task(self, body):
        result = self._api.create_task({"data": body}, {})
        return result

    def update_task(self, task_gid, body):
        result = self._api.update_task({"data": body}, task_gid, {})
        return result


def _build_asana_client(access_token=None):
    """构建 Asana 适配客户端。

    Args:
        access_token: Asana Personal Access Token，默认用模块级 ASANA_PAT。
    """
    if access_token is None:
        access_token = ASANA_PAT
    try:
        return AsanaClient(access_token)
    except ImportError:
        sys.exit("缺少依赖，请运行：pip install asana")


def build_sync_clients(
    sa_file: str = None,
    asana_pat: str = None,
    proxy_url: str = None,
):
    """
    构建 Google Sheets 和 Asana 双客户端（GUI 集成入口）。

    Args:
        sa_file:    Google Service Account JSON 文件路径。
        asana_pat:  Asana Personal Access Token。
        proxy_url:  代理地址（如 http://127.0.0.1:7897）。

    Returns:
        (gs_service, asana_client) 元组。
    """
    gs_service = _build_gs_service(sa_file=sa_file, proxy_url=proxy_url)
    asana_client = _build_asana_client(access_token=asana_pat)
    return gs_service, asana_client


# ── Asana 业务函数（接口不变，与测试一致） ──────────────────

def find_or_create_section(client, project_gid: str, section_name: str) -> str:
    """
    在 Asana 项目中查找或创建指定名称的区段（幂等）。

    Args:
        client:       AsanaClient 实例或 MagicMock。
        project_gid:  项目 GID。
        section_name: 区段名称（如 "6.8执行"）。

    Returns:
        已有或新创建的 Section GID。
    """
    sections = client.sections.get_sections_for_project(project_gid)
    for section in sections:
        if section.get("name") == section_name:
            return section["gid"]

    new_section = client.sections.create_section_for_project(
        project_gid, {"name": section_name}
    )
    return new_section["gid"]


def get_existing_task_names(client, section_gid: str) -> set[str]:
    """
    获取指定区段下已有任务的名称集合。

    Args:
        client:      AsanaClient 实例或 MagicMock。
        section_gid: 区段 GID。

    Returns:
        任务名称的 set[str]。
    """
    tasks = client.tasks.get_tasks_for_section(
        section_gid, opt_fields=["name"]
    )
    return {task["name"] for task in tasks}


def get_existing_tasks_by_name(
    client,
    section_gid: str,
    project_gid: str,
) -> dict[str, AsanaTaskInfo]:
    """获取指定区段下已有任务，并按任务名映射为 AsanaTaskInfo。"""
    tasks = client.tasks.get_tasks_for_section(
        section_gid,
        opt_fields=["gid", "name", "permalink_url", "notes"],
    )
    result: dict[str, AsanaTaskInfo] = {}
    for task in tasks:
        gid = task["gid"]
        name = task["name"]
        link = task.get("permalink_url") or build_asana_task_link(project_gid, gid)
        result[name] = AsanaTaskInfo(name, gid, link, task.get("notes") or "")
    return result


def create_tasks_for_packages(
    client,
    project_gid: str,
    section_gid: str,
    packages: list[str],
    workspace_gid: str = WORKSPACE_GID,
    parent_task_gid: str = PARENT_TASK_GID,
    notes_by_name: Optional[dict[str, str]] = None,
) -> dict[str, AsanaTaskInfo]:
    """
    为给定任务名列表创建 Asana 任务，绑定到指定项目与区段。

    Args:
        client:       AsanaClient 实例或 MagicMock。
        project_gid:  项目 GID。
        section_gid:  区段 GID（任务将归入此区段）。
        packages:     待创建的任务名列表（应为增量差集结果）。
        workspace_gid: 工作区 GID。
        parent_task_gid: 父任务 GID，新建任务将作为其子任务。
        notes_by_name: 任务名到 Asana 描述的映射。

    Returns:
        任务名到 AsanaTaskInfo 的映射。
    """
    created: dict[str, AsanaTaskInfo] = {}
    for pkg_name in packages:
        body = {
            "workspace": workspace_gid,
            "name": pkg_name,
            "parent": parent_task_gid,
            "memberships": [
                {"project": project_gid, "section": section_gid}
            ],
        }
        notes = (notes_by_name or {}).get(pkg_name)
        if notes:
            body["notes"] = notes
        task = client.tasks.create_task(body)
        gid = task["gid"]
        link = task.get("permalink_url") or build_asana_task_link(project_gid, gid)
        created[pkg_name] = AsanaTaskInfo(pkg_name, gid, link, notes or "")
    return created


def update_task_notes_for_packages(
    client,
    tasks_by_name: dict[str, AsanaTaskInfo],
    notes_by_name: dict[str, str],
) -> int:
    """把包信息描述写入已有或新建的 Asana 任务。"""
    updated = 0
    for task_name, notes in notes_by_name.items():
        task_info = tasks_by_name.get(task_name)
        if not task_info or not notes:
            continue
        merged_notes = merge_asana_task_notes(task_info.notes, notes)
        if merged_notes == task_info.notes:
            continue
        client.tasks.update_task(task_info.gid, {"notes": merged_notes})
        updated += 1
    return updated


# ═══════════════════════════════════════════════════════════════
# 编排：主同步流程
# ═══════════════════════════════════════════════════════════════

def sync_packages(
    gs_service,
    asana_client,
    sheet_id: str,
    project_gid: str,
    sheet_name: str = "",
    today: Optional[date] = None,
    parent_task_gid: str = PARENT_TASK_GID,
    notes_by_name: Optional[dict[str, str]] = None,
) -> dict:
    """
    主同步编排函数 — Sheet → 过滤 → 差集 → Asana 增量同步（幂等）。

    Args:
        gs_service:   Google Sheets API service。
        asana_client: Asana API client。
        sheet_id:     Google Sheet ID。
        project_gid:  Asana 项目 GID。
        sheet_name:   工作表名称（如 "26年5-6月"），空则读全表。
        today:        可选，注入日期用于测试。
        parent_task_gid: 父任务 GID，新建任务将作为其子任务。

    Returns:
        dict 同步结果摘要。
    """
    # 1. 日期
    sheet_date, section_name = generate_target_dates(today)

    # 2. 读取 Sheet
    range_name = f"{sheet_name}!A:AZ" if sheet_name else "A:AZ"
    raw_data = get_sheet_data(gs_service, sheet_id, range_name)

    # 3. 筛选包名
    packages = filter_packages(raw_data, sheet_date)

    # 3.5 拼接任务标题前缀（与已有任务命名保持一致，确保 diff 能匹配）
    task_names = unique_task_names_from_package_rows(packages)
    task_notes_by_name = task_descriptions_from_package_rows(packages)
    if notes_by_name:
        task_notes_by_name.update({k: v for k, v in notes_by_name.items() if v})

    # 4. 幂等区段
    section_gid = find_or_create_section(asana_client, project_gid, section_name)

    # 5. 定位任务链接列
    task_link_col = get_task_link_column_index(raw_data)

    # 6. 已有任务名 + 链接信息
    existing_tasks = get_existing_tasks_by_name(asana_client, section_gid, project_gid)
    existing_names = set(existing_tasks.keys())

    # 7. 差集
    new_task_names = compute_diff(task_names, existing_names)

    # 8. 创建任务
    created_tasks = create_tasks_for_packages(
        asana_client, project_gid, section_gid, new_task_names,
        parent_task_gid=parent_task_gid or PARENT_TASK_GID,
        notes_by_name=task_notes_by_name,
    )

    # 9. 合并已有和新创建的任务链接映射
    task_links_by_name = {**existing_tasks, **created_tasks}

    # 9.5 写入/补齐 Asana 任务描述
    notes_updated_count = update_task_notes_for_packages(
        asana_client, task_links_by_name, task_notes_by_name
    )

    # 10. 回填任务链接
    if task_link_col is None:
        updates = []
        backfill_skipped = "missing_task_link_column"
    else:
        updates = plan_task_link_backfills(
            packages, task_links_by_name, sheet_name, task_link_col
        )
        backfill_task_links(gs_service, sheet_id, updates)
        backfill_skipped = None

    return {
        "sheet_date": sheet_date,
        "section_name": section_name,
        "section_gid": section_gid,
        "total_packages": len(packages),
        "existing_count": len(existing_names),
        "new_count": len(new_task_names),
        "created_gids": [task.gid for task in created_tasks.values()],
        "notes_updated_count": notes_updated_count,
        "backfilled_count": len(updates),
        "backfilled_ranges": [u.range_name for u in updates],
        "backfill_skipped_reason": backfill_skipped,
    }


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    """主入口：构建客户端 → 执行同步 → 打印结果。"""
    print("=" * 60)
    print("  Google Sheets → Asana 幂等同步")
    print("=" * 60)

    # 1. 构建客户端
    print("\n[1/4] 初始化认证 ...")
    gs_service = _build_gs_service()
    asana_client = _build_asana_client()
    print("  认证通过 ✓")

    # 2. 日期
    sheet_date, section_name = generate_target_dates()
    print(f"\n[2/4] 目标日期: Sheet={sheet_date} / Section={section_name}")

    # 3. 执行同步
    print(f"\n[3/4] 执行同步 ...")
    print(f"  Sheet    : {SHEET_NAME}")
    print(f"  Project  : {PROJECT_GID}")

    result = sync_packages(
        gs_service=gs_service,
        asana_client=asana_client,
        sheet_id=SHEET_ID,
        project_gid=PROJECT_GID,
        sheet_name=SHEET_NAME,
    )

    # 4. 输出结果
    print(f"\n[4/4] 同步结果:")
    print(f"  Sheet 匹配日期 : {result['sheet_date']}")
    print(f"  Asana 区段名称 : {result['section_name']}")
    print(f"  Asana 区段 GID  : {result['section_gid']}")
    print(f"  Sheet 筛选包数 : {result['total_packages']}")
    print(f"  Asana 已有任务 : {result['existing_count']}")
    print(f"  本次新建任务   : {result['new_count']}")
    print(f"  本次回填链接   : {result['backfilled_count']}")
    if result["backfill_skipped_reason"]:
        print(f"  回填跳过原因   : {result['backfill_skipped_reason']}")
    if result["created_gids"]:
        print(f"  新建任务 GIDs  : {', '.join(result['created_gids'])}")
    print("=" * 60)

    if result["new_count"] == 0:
        print("✓ 幂等：无需新建任务，所有包名已存在。")
    else:
        print(f"✓ 同步完成：新建 {result['new_count']} 个任务。")


if __name__ == "__main__":
    main()
