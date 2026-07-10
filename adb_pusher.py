import os
import re
import shutil
import shlex
import subprocess
import threading
import urllib.request
import urllib.parse
import json
import copy

DEFAULT_KEEP_THIRD_PARTY_PACKAGES = [
    "com.github.kr328.clash",
    "com.google.android.contactkeys",
    "com.google.android.safetycore",
    "com.google.ar.core",
    "org.telegram.messenger",
]

# 常见 adb 安装位置
_COMMON_ADB_PATHS = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools/adb",
]

_adb_path: str | None = None

FIRST_ACTION_MIN_DELAY_MS = 15000
DEFAULT_FOLLOWING_ACTION_MIN_DELAY_MS = 5000
ZYGOTEHOLE_PERMISSION_FIX_SCRIPT = (
    "chmod 777 /data/local/tmp/zygotehole/config.json; "
    "chmod 444 /data/local/tmp/zygotehole/zygotehole.apk; "
    "chmod 777 /data/local/tmp/zygotehole; "
    "chown root:root /data/local/tmp/zygotehole/zygotehole.apk"
)


def extract_uid_from_dumpsys(output: str, package_name: str = "") -> str | None:
    """Extract an app UID from dumpsys package output.

    Android builds may print the app identifier as either userId=... or
    appId=... inside the package block. Both are the UID value used by logcat
    --uid for a normal single-user install.
    """
    if package_name:
        package_match = re.search(
            rf"Package \[{re.escape(package_name)}\].*?(?=\n\s*Package \[|\nQueries:|\Z)",
            output,
            re.S,
        )
        if package_match:
            package_block = package_match.group(0)
            uid_match = re.search(r"\b(?:userId|appId)=(\d+)\b", package_block)
            if uid_match:
                return uid_match.group(1)

    uid_match = re.search(r"\b(?:userId|appId)=(\d+)\b", output)
    if uid_match:
        return uid_match.group(1)
    return None


def first_csv_value(value: str) -> str:
    """Return the first comma-separated value for backend fields that accept one item."""
    return value.split(",", 1)[0].strip()


def extract_google_play_package(url: str) -> str:
    """Extract the package id from a Google Play details URL."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.netloc not in {"play.google.com", "www.play.google.com"}:
        return ""
    if not parsed.path.startswith("/store/apps/details"):
        return ""
    params = urllib.parse.parse_qs(parsed.query)
    return (params.get("id") or [""])[0].strip()


def is_apk_download_url(url: str) -> bool:
    """Return whether a URL path points directly to an APK/XAPK artifact."""
    parsed = urllib.parse.urlparse(url.strip())
    return parsed.path.lower().endswith((".apk", ".xapk"))


def download_artifact_filename(url: str) -> str:
    """Infer a temporary APK/XAPK filename from a download URL path."""
    parsed = urllib.parse.urlparse(url.strip())
    filename = os.path.basename(urllib.parse.unquote(parsed.path))
    if filename and filename.lower().endswith((".apk", ".xapk")):
        return filename
    return "download.apk"


def normalize_action_delays(
    script: dict | list,
    min_delay_ms: int = DEFAULT_FOLLOWING_ACTION_MIN_DELAY_MS,
) -> tuple[dict | list, dict[str, int]]:
    """Return a copy of an automation script with conservative delay values.

    The first action that contains a numeric ``delay`` is always raised to at
    least ``FIRST_ACTION_MIN_DELAY_MS``. Every later action delay is raised to
    at least ``min_delay_ms``. Other fields are preserved.
    """
    normalized = copy.deepcopy(script)
    stats = {
        "delay_count": 0,
        "updated_count": 0,
        "first_delay_ms": FIRST_ACTION_MIN_DELAY_MS,
        "min_delay_ms": min_delay_ms,
    }

    def visit(node):
        if isinstance(node, dict):
            if "delay" in node and isinstance(node["delay"], (int, float)):
                stats["delay_count"] += 1
                minimum = (
                    FIRST_ACTION_MIN_DELAY_MS
                    if stats["delay_count"] == 1
                    else min_delay_ms
                )
                if node["delay"] < minimum:
                    node["delay"] = minimum
                    stats["updated_count"] += 1
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(normalized)
    return normalized, stats


def normalize_action_script_text(
    text: str,
    min_delay_ms: int = DEFAULT_FOLLOWING_ACTION_MIN_DELAY_MS,
) -> tuple[str, dict[str, int]]:
    """Parse pasted action JSON and normalize delay fields.

    Some pasted snippets omit the opening ``{`` while keeping the closing
    ``}``; this helper accepts that common fragment format as well.
    """
    raw = text.strip()
    if not raw:
        raise ValueError("脚本内容不能为空")

    parse_text = raw
    if not parse_text.startswith(("{", "[")) and parse_text.endswith("}"):
        parse_text = "{" + parse_text

    script = json.loads(parse_text)
    normalized, stats = normalize_action_delays(
        script,
        min_delay_ms=min_delay_ms,
    )
    return json.dumps(normalized, indent=2, ensure_ascii=False), stats


def parse_fill_url(url: str) -> dict:
    """Parse a web/backoffice URL into fields that can fill the GUI form."""
    text = url.strip()
    if not text:
        return {}

    parsed = urllib.parse.urlparse(text)
    query_parts = []
    if parsed.query:
        query_parts.append(parsed.query)
    if parsed.fragment and "?" in parsed.fragment:
        query_parts.append(parsed.fragment.split("?", 1)[1])
    if not query_parts and "=" in text and not parsed.scheme:
        query_parts.append(text.lstrip("?"))

    values: dict[str, str] = {}
    for query in query_parts:
        params = urllib.parse.parse_qs(query, keep_blank_values=False)
        for key, items in params.items():
            if items and key not in values:
                values[key] = items[0]

    package_name = (
        values.get("package_name")
        or values.get("packageName")
        or values.get("包名")
        or extract_google_play_package(text)
        or ""
    )
    app_id = (
        values.get("up2_appid")
        or values.get("appid")
        or values.get("appId")
        or values.get("UP2 appid")
        or ""
    )
    gp_url = (
        values.get("google_download_url")
        or values.get("gp_url")
        or values.get("gpUrl")
        or values.get("GP链接")
        or ""
    )

    if not gp_url and "play.google.com" in parsed.netloc and package_name:
        gp_url = text

    result = {}
    if package_name:
        result["package_name"] = package_name
    if app_id:
        result["appId"] = app_id
    if gp_url:
        result["gpUrl"] = gp_url
    return result


def extract_af_key_from_content(content: str) -> str:
    """Extract af_key from one normalized AutoDetector log content line."""
    patterns = [
        r"^af[_\s-]*key\s*[:：]\s*(.+)$",
        r"^Apps[Ff]lyer\s+(?:SDK\s+)?Key\s*[:：]\s*(.+)$",
        r"^Apps[Ff]lyer\s+Dev(?:eloper)?\s+Key\s*[:：]\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.I)
        if match:
            value = match.group(1).strip().strip("[]")
            if value and value != "未找到":
                return value
    return ""


def _canonical_aggr_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    if normalized in {"max", "applovin", "applovinmax"}:
        return "max"
    if normalized in {"ironsource", "iron"}:
        return "ironsource"
    if normalized in {"levelplay", "level"}:
        return "levelplay"
    if normalized in {"admob", "googleadmob"}:
        return "admob"
    if normalized in {"topon"}:
        return "topon"
    if normalized in {"fyber"}:
        return "fyber"
    return normalized


def _target_aggr_from_final(final: str) -> str:
    final_lower = final.lower()
    if "levelplay" in final_lower or "level_play" in final_lower:
        return "levelplay"
    if "ironsource" in final_lower or "iron_source" in final_lower:
        return "ironsource"
    if "applovin" in final_lower or "max" in final_lower:
        return "max"
    if "admob" in final_lower:
        return "admob"
    if "topon" in final_lower:
        return "topon"
    if "fyber" in final_lower:
        return "fyber"
    platform_match = re.match(r"^([A-Za-z_]+)", final)
    if platform_match:
        return _canonical_aggr_name(platform_match.group(1))
    return ""


def _clean_detected_value(value: str) -> str:
    cleaned = value.strip().strip("[]").strip()
    if cleaned == "未找到":
        return ""
    return cleaned


def parse_autodetector_fields(lines: list[str]) -> dict:
    """Parse ZGSDK.AutoDetector log lines into structured fields."""
    tag_lines = [line for line in lines if "ZGSDK.AutoDetector" in line]
    fields = {
        "ok": True,
        "最终判断": "",
        "初始Activity": "",
        "应用类型": "",
        "激励视频聚合id": "",
        "插屏聚合id": "",
        "归因平台": "",
        "af_key": "",
        "SDK列表": [],  # [{"名称": "AppLovin", "key": "xxx"}, ...]
        "完整日志": "",
    }

    sdk_ids: dict[str, dict[str, str]] = {}
    sdk_order: list[str] = []
    current_sdk = ""

    def ensure_sdk(name: str) -> str:
        canonical = _canonical_aggr_name(name)
        if canonical and canonical not in sdk_order:
            sdk_order.append(canonical)
        sdk_ids.setdefault(canonical, {"reward": "", "interstitial": ""})
        return canonical

    def remember_sdk_display(name: str, key: str = ""):
        for sdk in fields["SDK列表"]:
            if sdk["名称"] == name:
                if key:
                    sdk["key"] = key
                return
        fields["SDK列表"].append({"名称": name, "key": key})

    for line in tag_lines:
        m = re.search(r"ZGSDK\.AutoDetector:\s+(.*)", line)
        if not m:
            continue
        content = m.group(1).strip()
        af_key = extract_af_key_from_content(content)
        if af_key:
            fields["af_key"] = af_key
            remember_sdk_display("Appsflyer", af_key)

        if content.startswith("最终判断:"):
            fields["最终判断"] = content.replace("最终判断:", "").strip()

        elif content.startswith("初始页面Activity:"):
            fields["初始Activity"] = content.replace("初始页面Activity:", "").strip()

        elif content.startswith("应用类型:"):
            fields["应用类型"] = content.replace("应用类型:", "").strip()

        elif content.startswith("归因平台:"):
            raw = content.replace("归因平台:", "").strip()
            fields["归因平台"] = raw.strip("[]")

        elif re.match(r"^[A-Za-z][A-Za-z0-9 ]*:$", content):
            current_sdk = content.rstrip(":").strip()
            ensure_sdk(current_sdk)
            remember_sdk_display(current_sdk)

        elif "SDK Key:" in content:
            key_val = content.split("SDK Key:", 1)[1].strip()
            prefix_m = re.match(r"^(\S+)\s+SDK\s+Key:", content)
            if prefix_m:
                sdk_name = prefix_m.group(1)
                if sdk_name.lower() == "appsflyer" and key_val and key_val != "未找到":
                    fields["af_key"] = key_val.strip("[]")
                remember_sdk_display(sdk_name, key_val)
                current_sdk = sdk_name
            elif current_sdk:
                remember_sdk_display(current_sdk, key_val)
            else:
                remember_sdk_display("未知", key_val)

        elif "激励视频聚合id:" in content:
            val = _clean_detected_value(content.split("激励视频聚合id:", 1)[1])
            if val:
                sdk_key = ensure_sdk(current_sdk) if current_sdk else ""
                if sdk_key:
                    sdk_ids[sdk_key]["reward"] = val

        elif "插屏聚合id:" in content:
            val = _clean_detected_value(content.split("插屏聚合id:", 1)[1])
            if val:
                sdk_key = ensure_sdk(current_sdk) if current_sdk else ""
                if sdk_key:
                    sdk_ids[sdk_key]["interstitial"] = val

        elif "激励视频广告单元ID列表:" in content:
            val = _clean_detected_value(content.split("激励视频广告单元ID列表:", 1)[1])
            if val:
                sdk_ids.setdefault("max", {"reward": "", "interstitial": ""})["reward"] = val
                if "max" not in sdk_order:
                    sdk_order.append("max")

        elif "插屏广告单元ID列表:" in content:
            val = _clean_detected_value(content.split("插屏广告单元ID列表:", 1)[1])
            if val:
                sdk_ids.setdefault("max", {"reward": "", "interstitial": ""})["interstitial"] = val
                if "max" not in sdk_order:
                    sdk_order.append("max")

    target_sdk = _target_aggr_from_final(fields["最终判断"])
    selected_ids = sdk_ids.get(target_sdk, {}) if target_sdk else {}
    if not selected_ids:
        for sdk_key in sdk_order:
            candidate = sdk_ids.get(sdk_key, {})
            if candidate.get("reward") or candidate.get("interstitial"):
                selected_ids = candidate
                break

    fields["激励视频聚合id"] = selected_ids.get("reward", "")
    fields["插屏聚合id"] = selected_ids.get("interstitial", "")
    fields["完整日志"] = "\n".join(tag_lines[-50:])
    return fields


def set_adb_path(path: str):
    global _adb_path
    _adb_path = path


def get_adb_path() -> str | None:
    """查找 adb 路径：优先使用手动设置的，再查 PATH，最后查常见位置"""
    global _adb_path
    if _adb_path and os.path.isfile(_adb_path):
        return _adb_path
    which = shutil.which("adb")
    if which:
        return which
    for p in _COMMON_ADB_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _run_adb(args: list[str], timeout: float = 8) -> subprocess.CompletedProcess:
    adb = get_adb_path()
    if not adb:
        raise FileNotFoundError("adb")
    return subprocess.run(
        [adb] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def check_device() -> bool:
    try:
        result = _run_adb(["devices"], timeout=3)
        lines = result.stdout.strip().split("\n")[1:]
        return any("\tdevice" in line for line in lines)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_device_list() -> list[str]:
    try:
        result = _run_adb(["devices"], timeout=3)
        lines = result.stdout.strip().split("\n")[1:]
        return [line.split("\t")[0] for line in lines if "\tdevice" in line]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def list_third_party_packages() -> list[str]:
    """列出设备上的第三方包名。"""
    result = _run_adb(["shell", "pm", "list", "packages", "-3"], timeout=15)
    packages = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkg = line.removeprefix("package:").strip()
            if pkg:
                packages.append(pkg)
    return sorted(set(packages))


def packages_to_uninstall(
    installed_packages: list[str],
    keep_packages: list[str] | set[str],
) -> list[str]:
    """根据保留白名单计算需要卸载的第三方包。"""
    keep = {pkg.strip() for pkg in keep_packages if pkg and pkg.strip()}
    return sorted(pkg for pkg in set(installed_packages) if pkg not in keep)


def push_apk(apk_path: str) -> tuple[bool, str]:
    """安装 APK。支持单文件、目录（多 APK）、.xapk 文件"""
    try:
        # .xapk 文件 → 先解压
        if apk_path.lower().endswith(".xapk"):
            return _install_xapk(apk_path)

        # 目录 → 安装目录下所有 APK
        if os.path.isdir(apk_path):
            apks = _collect_apks(apk_path)
            if not apks:
                return False, f"目录中没有 APK 文件: {apk_path}"
            return _install_apks(apks)

        # 单文件 → 先尝试直接安装，失败则检查同目录是否有拆分 APK
        result = _run_adb(["install", "-r", apk_path])
        if result.returncode == 0:
            return True, "安装成功"

        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        combined = stderr + stdout

        if "no devices" in combined.lower():
            return False, "没有已连接的设备"

        # 下载链接有时不带 .xapk 后缀，临时文件会按 .apk 保存。
        # 若实际是包含 APK 的 XAPK/ZIP，则改走 XAPK 安装。
        if _zip_contains_apks(apk_path):
            return _install_xapk(apk_path)

        # 拆分 APK 相关错误 → 尝试安装同目录所有 APK
        if any(kw in combined for kw in [
            "INSTALL_FAILED", "split", "config", "Missing"
        ]):
            parent = os.path.dirname(apk_path)
            siblings = _collect_apks(parent, recursive=False)
            if len(siblings) > 1:
                # 确保主 APK 排第一
                siblings.remove(apk_path)
                siblings.insert(0, apk_path)
                return _install_apks(siblings)
            return False, f"安装失败: {stderr}"

        return False, f"安装失败: {stderr or stdout}"

    except FileNotFoundError:
        return False, "未找到 ADB 工具，请确认已安装 Android SDK"


def _sort_apks_for_install(apk_list: list[str]) -> list[str]:
    """Sort split APKs with base APK first for install-multiple."""
    def key(path: str):
        name = os.path.basename(path).lower()
        is_base = name == "base.apk" or name.startswith("base-")
        return (0 if is_base else 1, name)

    return sorted(apk_list, key=key)


def _zip_contains_apks(zip_path: str) -> bool:
    import zipfile

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            return any(name.lower().endswith(".apk") for name in z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _collect_apks(root_dir: str, recursive: bool = True) -> list[str]:
    """Collect APK files from a directory, optionally recursively."""
    apks: list[str] = []
    if recursive:
        for root, _, files in os.walk(root_dir):
            for filename in files:
                if filename.lower().endswith(".apk"):
                    apks.append(os.path.join(root, filename))
    else:
        apks = [
            os.path.join(root_dir, filename)
            for filename in os.listdir(root_dir)
            if filename.lower().endswith(".apk")
        ]
    return _sort_apks_for_install(apks)


def _install_apks(apk_list: list[str]) -> tuple[bool, str]:
    """安装多个 APK（拆分 APK）"""
    if len(apk_list) == 1:
        result = _run_adb(["install", "-r", apk_list[0]])
        if result.returncode == 0:
            return True, "安装成功"
        return False, f"安装失败: {result.stderr.strip() or result.stdout.strip()}"

    result = _run_adb(["install-multiple", "-r"] + _sort_apks_for_install(apk_list))
    if result.returncode == 0:
        return True, f"安装成功 ({len(apk_list)} 个 APK)"
    return False, f"安装失败: {result.stderr.strip() or result.stdout.strip()}"


def _install_xapk(xapk_path: str) -> tuple[bool, str]:
    """解压 .xapk 并安装"""
    import zipfile
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="xapk_") as tmp:
            with zipfile.ZipFile(xapk_path, "r") as z:
                z.extractall(tmp)

            # 收集所有 APK
            apks = []
            for root, _, files in os.walk(tmp):
                for f in files:
                    if f.lower().endswith(".apk"):
                        apks.append(os.path.join(root, f))

            apks = _sort_apks_for_install(apks)

            if not apks:
                return False, "xapk 中没有找到 APK 文件"

            # 装 APK
            ok, msg = _install_apks(apks)

            # 推送 OBB（如果有）
            for root, _, files in os.walk(tmp):
                for f in files:
                    if f.endswith(".obb"):
                        obb_dir = os.path.relpath(root, tmp)
                        obb_path = os.path.join(root, f)
                        device_obb = f"/sdcard/{obb_dir}"
                        _run_adb(["shell", "mkdir", "-p", device_obb])
                        _run_adb(["push", obb_path, device_obb + "/"])

            return ok, msg

    except zipfile.BadZipFile:
        return False, "文件不是有效的 xapk/zip 包"
    except Exception as e:
        return False, f"xapk 处理失败: {e}"


def download_and_install(url: str, on_progress=None) -> tuple[bool, str]:
    """从 URL 下载 APK/XAPK 并安装到手机。

    on_progress(percent, status_text) — 可选进度回调
    """
    import tempfile

    url = url.strip()
    if not url:
        return False, "请输入下载链接"

    filename = download_artifact_filename(url)

    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        if on_progress:
            on_progress(0, f"正在下载: {filename}")

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        })

        with urllib.request.urlopen(req, timeout=300) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if total else 0
            downloaded = 0

            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total > 0:
                        pct = int(downloaded * 100 / total)
                        size_mb = total / (1024 * 1024)
                        dl_mb = downloaded / (1024 * 1024)
                        on_progress(pct, f"下载中 {dl_mb:.1f}/{size_mb:.1f} MB")

        if on_progress:
            on_progress(100, "下载完成，正在安装...")

        return push_apk(tmp_path)

    except urllib.error.URLError as e:
        return False, f"下载失败（网络错误）: {e.reason}"
    except Exception as e:
        return False, f"下载失败: {e}"
    finally:
        # 清理临时文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def push_config(local_path: str) -> tuple[bool, str]:
    """推送 config.json 到设备的 /data/local/tmp/zygotehole/"""
    if not os.path.isfile(local_path):
        return False, f"文件不存在: {local_path}"
    try:
        result = _run_adb(["push", local_path, "/data/local/tmp/zygotehole/"])
        if result.returncode == 0:
            return True, f"推送成功\n{result.stdout.strip()}"
        return False, f"推送失败: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"


def run_zygote_build(script_dir: str) -> tuple[bool, str]:
    """在指定目录执行 zygote_build.sh"""
    script_path = os.path.join(script_dir, "zygote_build.sh")
    if not os.path.isfile(script_path):
        return False, f"脚本不存在: {script_path}"
    try:
        result = subprocess.run(
            ["sh", script_path],
            cwd=script_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if result.returncode == 0:
            return True, f"执行成功\n{output}"
        return False, f"执行失败 (exit={result.returncode})\n{output}"
    except subprocess.TimeoutExpired:
        return False, "执行超时 (60s)"
    except FileNotFoundError:
        return False, "未找到 sh 命令"


def get_app_uid(package_name: str) -> tuple[bool, str]:
    """查询应用 UID"""
    if not package_name.strip():
        return False, "请输入包名"
    try:
        result = _run_adb(
            ["shell", "dumpsys", "package", package_name.strip()],
            timeout=10,
        )
        output = result.stdout
        uid = extract_uid_from_dumpsys(output, package_name.strip())
        if uid:
            return True, uid
        return False, f"未找到包名 {package_name} 的 UID，请确认应用已安装"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"
    except subprocess.TimeoutExpired:
        return False, "查询 UID 超时，请检查设备连接或 ADB 状态"


def clear_app_cache(package_name: str) -> tuple[bool, str]:
    """清除应用缓存"""
    if not package_name.strip():
        return False, "请输入包名"
    try:
        result = _run_adb(["shell", "pm", "clear", package_name.strip()], timeout=20)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0 and "Success" in output:
            return True, f"缓存清除成功\n{output}"
        return False, f"清除失败\n{output}"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"
    except subprocess.TimeoutExpired:
        return False, "清除缓存超时，请检查设备连接或应用状态"


def force_stop_app(package_name: str) -> tuple[bool, str]:
    """强制停止应用"""
    if not package_name.strip():
        return False, "请输入包名"
    try:
        result = _run_adb(["shell", "am", "force-stop", package_name.strip()], timeout=15)
        if result.returncode == 0:
            return True, f"已强制停止 {package_name}"
        return False, f"停止失败: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"
    except subprocess.TimeoutExpired:
        return False, "强制停止超时，请检查设备连接"


def logcat_dump(filter_pattern: str, uid: str | None = None) -> tuple[bool, str]:
    """获取 logcat 日志快照 (dump 模式，非阻塞)"""
    try:
        cmd = ["logcat", "-d"]
        if uid:
            cmd.extend(["--uid", uid])
        result = _run_adb(cmd)
        output = result.stdout

        lines = output.split("\n")
        matched = [line for line in lines if filter_pattern in line]

        if not matched:
            return True, f"(无匹配 {filter_pattern} 的日志)"

        return True, "\n".join(matched)
    except FileNotFoundError:
        return False, "未找到 ADB 工具"


def start_logcat_stream(pattern: str, uid: str | None = None):
    """启动持续 logcat 流，返回 Popen 进程对象。调用方负责读取和终止。"""
    adb = get_adb_path()
    if not adb:
        raise FileNotFoundError("adb")

    # 先清除旧日志缓冲，只获取启动后的新日志
    subprocess.run([adb, "logcat", "-c"], capture_output=True, timeout=5)

    cmd = [adb, "logcat"]
    if uid:
        cmd.extend(["--uid", uid])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    proc._filter_pattern = pattern  # type: ignore[attr-defined]
    return proc


def stop_logcat_stream(proc: subprocess.Popen, timeout: float = 3) -> None:
    """终止 logcat 流进程"""
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        proc.kill()


def run_stream(cmd: list[str], on_line, on_done, cwd=None, timeout=None, on_proc=None):
    """后台线程逐行执行命令。timeout 覆盖整个执行周期（含 stdout 读取）"""

    def _run():
        returncode = -1
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=cwd,
            )
            if on_proc:
                on_proc(proc)

            if timeout:
                # 看门狗：超时后强制杀进程
                def _kill():
                    if proc and proc.poll() is None:
                        on_line(f"[超时] 命令超过 {timeout} 秒未结束，已终止")
                        proc.kill()
                timer = threading.Timer(timeout, _kill)
                timer.start()

            for line in proc.stdout:
                on_line(line.rstrip())

            if timeout:
                timer.cancel()

            # 进程可能已被看门狗杀掉，再 wait 一次收尸
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            returncode = proc.returncode
            if returncode != 0 and returncode != -9:
                pass  # 非零退出码，由调用方处理

        except FileNotFoundError:
            on_line(f"[错误] 命令未找到: {cmd[0]}")
        except Exception as e:
            on_line(f"[错误] {e}")
        finally:
            # 确保看门狗被取消
            if timeout:
                try:
                    timer.cancel()
                except (NameError, AttributeError):
                    pass
        on_done(returncode)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def cmd_to_str(cmd: list[str]) -> str:
    """将命令列表转成可显示的字符串，含空格转义"""
    return " ".join(cmd)


def build_push_config_cmd(local_path: str) -> list[str]:
    adb = get_adb_path()
    return [adb, "push", local_path, "/data/local/tmp/zygotehole/"]


def build_fix_zygotehole_permissions_cmd() -> list[str]:
    adb = get_adb_path()
    return [
        adb,
        "shell",
        ZYGOTEHOLE_PERMISSION_FIX_SCRIPT,
    ]


def build_zygote_build_cmd(script_dir: str) -> list[str]:
    adb = get_adb_path() or "adb"
    script_path = os.path.join(script_dir, "zygote_build.sh")
    shell_script = (
        f"sh {shlex.quote(script_path)} && "
        f"{shlex.quote(adb)} shell {shlex.quote(ZYGOTEHOLE_PERMISSION_FIX_SCRIPT)}"
    )
    return ["sh", "-c", shell_script]


def build_get_uid_cmd(package_name: str) -> list[str]:
    adb = get_adb_path()
    return [adb, "shell", "dumpsys", "package", package_name]


def build_clear_cache_cmd(package_name: str) -> list[str]:
    adb = get_adb_path()
    return [adb, "shell", "pm", "clear", package_name]


def build_force_stop_cmd(package_name: str) -> list[str]:
    adb = get_adb_path()
    return [adb, "shell", "am", "force-stop", package_name]


def build_open_app_cmd(package_name: str) -> list[str]:
    adb = get_adb_path()
    return [adb, "shell", "monkey", "-p", package_name,
            "-c", "android.intent.category.LAUNCHER", "1"]


def build_logcat_cmd(uid: str | None = None) -> list[str]:
    adb = get_adb_path()
    cmd = [adb, "logcat"]
    if uid:
        cmd.extend(["--uid", uid])
    return cmd


def build_bulk_uninstall_cmd(packages: list[str]) -> list[str]:
    adb = get_adb_path()
    safe_packages = [
        pkg.strip()
        for pkg in packages
        if pkg and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.]*", pkg.strip())
    ]
    if not safe_packages:
        return [adb, "shell", "true"]
    body = "; ".join(
        f"echo '[卸载] {pkg}'; pm uninstall {shlex.quote(pkg)}"
        for pkg in safe_packages
    )
    return [adb, "shell", "sh", "-c", body]


def build_backend_url(fields: dict, package_name: str) -> str:
    """根据提取的字段构造适配后台 URL（同事文档: CpAdaptEditFieldMapping.md）"""
    import urllib.parse

    params = {"change": "1", "package_name": package_name}

    # 最终判断 → 聚合平台（映射到下拉框合法值）
    PLATFORM_MAP = {
        "max": "max",
        "admob": "admob",
        "applovin": "max",  # AppLovin 聚合即 max
        "ironsource": "iron_source",
        "iron": "iron_source",
        "topon": "topon",
        "fyber": "fyber",
        "levelplay": "level_play",
        "level": "level_play",
    }
    final = fields.get("最终判断", "")
    platform_match = re.match(r"^([A-Za-z_]+)", final)
    if platform_match:
        raw = platform_match.group(1).lower()
        params["aggr_platform"] = PLATFORM_MAP.get(raw, raw)

    # 其他直接映射字段
    field_map = {
        "归因平台": "attribution_platform",
        "初始Activity": "activity_main_page",
    }
    for src, dst in field_map.items():
        val = fields.get(src, "")
        if val:
            params[dst] = val

    single_value_field_map = {
        "插屏聚合id": "aggr_chaping_id",
        "激励视频聚合id": "aggr_jilishipin_id",
    }
    for src, dst in single_value_field_map.items():
        val = fields.get(src, "")
        if val:
            params[dst] = first_csv_value(val)

    # AppLovin SDK Key
    for sdk in fields.get("SDK列表", []):
        if sdk.get("名称") == "AppLovin" and sdk.get("key"):
            params["manual_applovin_sdk_key"] = sdk["key"]
            break

    af_key = fields.get("af_key", "")
    if not af_key:
        for sdk in fields.get("SDK列表", []):
            if sdk.get("名称", "").lower() == "appsflyer" and sdk.get("key"):
                af_key = sdk["key"]
                break
    if af_key:
        params["af_key"] = af_key

    return (
        "http://data_center_web_internet.hongdinghe.cn"
        "/#/CpAdaptManage/CpAdapt?"
        + urllib.parse.urlencode(params)
    )


def clear_logcat_buffer() -> None:
    """清除 logcat 旧缓冲"""
    adb = get_adb_path()
    if adb:
        subprocess.run([adb, "logcat", "-c"], capture_output=True, timeout=5)


def extract_logcat_fields() -> dict:
    """从 adb logcat -d 中提取 ZGSDK.AutoDetector 的结构化字段"""
    adb = get_adb_path()
    if not adb:
        return {"ok": False, "error": "未找到 ADB"}

    # 先扩大缓冲区，防止旧日志被冲掉
    try:
        subprocess.run(
            [adb, "logcat", "-G", "16M"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

    try:
        result = subprocess.run(
            [adb, "logcat", "-d"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        lines = result.stdout.split("\n")
    except Exception as e:
        return {"ok": False, "error": f"无法读取 logcat: {e}"}

    return parse_autodetector_fields(lines)
