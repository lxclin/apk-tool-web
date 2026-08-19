import os
import re
import shutil
import shlex
import subprocess
import threading
import urllib.request
import urllib.parse
import urllib.error
import json
import copy
import tempfile
import time
import xml.etree.ElementTree as ET

DEFAULT_KEEP_THIRD_PARTY_PACKAGES = [
    "com.apktool.sharereceiver",
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
APK_INSTALL_TIMEOUT_SECONDS = 90
SPLIT_APK_INSTALL_TIMEOUT_SECONDS_PER_APK = 30
LOGCAT_READ_TIMEOUT_SECONDS = 15
LOGCAT_READ_ATTEMPTS = 2
LOGCAT_READ_MAX_LINES = 20000
ZYGOTEHOLE_PERMISSION_FIX_SCRIPT = (
    "chmod 777 /data/local/tmp/zygotehole/config.json; "
    "chmod 444 /data/local/tmp/zygotehole/zygotehole.apk; "
    "chmod 777 /data/local/tmp/zygotehole; "
    "chown root:root /data/local/tmp/zygotehole/zygotehole.apk"
)
ZYGOTEHOLE_CONFIG_PATH = "/data/local/tmp/zygotehole/config.json"
ZYGOTEHOLE_CONFIG_TEMP_PATH = "/data/local/tmp/zygotehole/config.json.cancel.tmp"


def remove_package_from_zygotehole_config(
    config: dict,
    package_name: str,
) -> tuple[dict, int]:
    """Return a config without entries matching ``package_name``.

    Other games and unknown top-level fields are preserved. A copy is returned
    so callers can safely retain the original configuration for diagnostics.
    """
    package_name = package_name.strip()
    if not package_name:
        raise ValueError("请输入包名")
    if not isinstance(config, dict) or not isinstance(config.get("data"), list):
        raise ValueError("注入配置格式错误：data 必须是数组")

    updated = copy.deepcopy(config)
    original_entries = updated["data"]
    updated["data"] = [
        entry
        for entry in original_entries
        if not (
            isinstance(entry, dict)
            and str(entry.get("packageName", "")).strip() == package_name
        )
    ]
    return updated, len(original_entries) - len(updated["data"])


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


MISSING_PARAMETER_VALUES = frozenset(
    {
        "未找到",
        "未提取到",
        "未知",
        "无",
        "-",
        "n/a",
        "na",
        "none",
        "null",
        "undefined",
        "unknown",
    }
)


def normalize_optional_parameter(value) -> str:
    """Convert detector placeholder text to an omitted backend parameter."""
    cleaned = str(value or "").strip()
    if cleaned.lower() in MISSING_PARAMETER_VALUES:
        return ""
    return cleaned


def first_csv_value(value: str) -> str:
    """Return the first comma-separated value for backend fields that accept one item."""
    return normalize_optional_parameter(str(value or "").split(",", 1)[0])


def extract_google_play_package(url: str) -> str:
    """Extract the package id from a Google Play details URL."""
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.netloc not in {"play.google.com", "www.play.google.com"}:
        return ""
    if not parsed.path.startswith("/store/apps/details"):
        return ""
    params = urllib.parse.parse_qs(parsed.query)
    return (params.get("id") or [""])[0].strip()


PLAY_STORE_AD_TEXTS = (
    "包含广告",
    "含广告",
    "contains ads",
)
PLAY_STORE_IAP_TEXTS = (
    "应用内购",
    "应用内购商品",
    "in-app purchases",
    "offers in-app purchases",
)
PLAY_STORE_DEVICE_UNSUPPORTED_TEXTS = (
    "您的设备与此版本不兼容",
    "您的设备暂不支持此应用",
    "此应用不适用于您的设备",
    "your device isn't compatible with this version",
    "your device is not compatible with this version",
    "this app isn't compatible with your device",
    "this app is not compatible with your device",
)
PLAY_STORE_COUNTRY_UNSUPPORTED_TEXTS = (
    "您所在的国家或地区无法使用此应用",
    "您所在的地区无法使用此应用",
    "此应用在您所在的国家/地区不可用",
    "this item isn't available in your country",
    "this item is not available in your country",
    "this app isn't available in your country",
    "this app is not available in your country",
    "this item isn't available in your region",
    "this item is not available in your region",
)
PLAY_STORE_ITEM_NOT_FOUND_TEXTS = (
    "item not found",
    "找不到此商品",
    "未找到此商品",
    "未找到商品",
    "此商品不存在",
    "应用不存在",
)
PLAY_STORE_READY_TEXTS = {
    "安装",
    "更新",
    "打开",
    "卸载",
    "install",
    "update",
    "open",
    "uninstall",
}
PLAY_STORE_AUTH_REQUIRED_TEXTS = (
    "验证您的身份",
    "验证身份",
    "需要进行身份验证",
    "登录您的 google 账号",
    "verify it's you",
    "verify your identity",
    "authentication is required",
    "sign in to your google account",
)


def resolve_google_play_package(value: str) -> str:
    """Resolve a package name from either a GP URL or a raw package name."""
    text = value.strip()
    package_name = extract_google_play_package(text) if "://" in text else text
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package_name):
        return ""
    return package_name


def build_apkcombo_search_url(value: str) -> str:
    """Build APKCombo's search-result URL for a GP URL or package name."""
    text = str(value or "").strip()
    package_name = resolve_google_play_package(text)
    if not package_name:
        return ""
    search_value = (text if extract_google_play_package(text) else package_name).lower()
    path_value = urllib.parse.quote(search_value, safe=":=")
    query_value = urllib.parse.quote(search_value, safe="")
    return (
        f"https://apkcombo.com/search/{path_value}"
        f"#gsc.tab=0&gsc.q={query_value}&gsc.sort="
    )


def build_apkcombo_downloader_url(value: str) -> str:
    """Backward-compatible alias for the APKCombo button target URL."""
    return build_apkcombo_search_url(value)


APKCOMBO_NOT_FOUND_TEXTS = (
    "sorry, the application was not found",
    "apk not found",
    "we couldn't find anything for your search",
    "we're sorry, the app was not found on apkcombo",
)


def _apkcombo_fetch_text(
    url: str,
    *,
    timeout_seconds: float = 15,
    data: bytes | None = None,
    referer: str = "",
) -> tuple[str, str]:
    """Fetch one APKCombo page and return decoded HTML plus its final URL."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=max(1, timeout_seconds))
    except urllib.error.HTTPError as exc:
        # APKCombo uses HTTP 404 together with a meaningful HTML body for an
        # app detail that exists but has no downloadable artifact.
        if exc.code != 404:
            raise
        raw = exc.read()
        if not raw:
            raise
        charset = exc.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), exc.geturl()
    with response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), response.geturl()


def _apkcombo_page_says_not_found(html_text: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(html_text or "")).casefold()
    return any(phrase in lowered for phrase in APKCOMBO_NOT_FOUND_TEXTS)


def _apkcombo_final_url_matches_package(final_url: str, package_name: str) -> bool:
    segments = [
        urllib.parse.unquote(segment).casefold()
        for segment in urllib.parse.urlparse(final_url).path.split("/")
        if segment
    ]
    package = package_name.casefold()
    return package in segments and not (
        len(segments) >= 2 and segments[-2] == "search" and segments[-1] == package
    )


def inspect_apkcombo_package(
    package_name: str,
    *,
    attempts: int = 2,
    timeout_seconds: float = 15,
    retry_interval_seconds: float = 2,
) -> dict:
    """Check whether APKCombo has an exact package with a real download.

    APKCombo may expose an app detail page whose download panel later reports
    that the application was not found.  This checker therefore validates both
    the exact package redirect and the download page/dynamic download response.
    """
    package_name = resolve_google_play_package(package_name)
    if not package_name:
        return {
            "code": "APKCOMBO_CHECK_FAILED",
            "available": None,
            "message": "无效的应用包名，无法检查 APKCombo",
            "package_name": package_name,
        }

    search_url = f"https://apkcombo.com/search/{urllib.parse.quote(package_name, safe='.')}"
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            detail_html, detail_url = _apkcombo_fetch_text(
                search_url,
                timeout_seconds=timeout_seconds,
            )
            if _apkcombo_page_says_not_found(detail_html) or not (
                _apkcombo_final_url_matches_package(detail_url, package_name)
            ):
                not_found_result = {
                    "code": "APKCOMBO_NOT_FOUND",
                    "available": False,
                    "message": "APKCombo 搜索未找到完全一致的包名",
                    "package_name": package_name,
                    "search_url": search_url,
                    "detail_url": detail_url,
                }
                if attempt + 1 < max(1, attempts):
                    if retry_interval_seconds > 0:
                        time.sleep(retry_interval_seconds)
                    continue
                return not_found_result

            escaped_package = re.escape(package_name)
            download_match = re.search(
                rf'href=["\']([^"\']*/{escaped_package}/download/(?:apk|xapk)[^"\']*)["\']',
                detail_html,
                re.I,
            )
            if not download_match:
                not_found_result = {
                    "code": "APKCOMBO_NOT_FOUND",
                    "available": False,
                    "message": "APKCombo 存在应用资料，但没有可用下载版本",
                    "package_name": package_name,
                    "search_url": search_url,
                    "detail_url": detail_url,
                }
                if attempt + 1 < max(1, attempts):
                    if retry_interval_seconds > 0:
                        time.sleep(retry_interval_seconds)
                    continue
                return not_found_result

            download_url = urllib.parse.urljoin(detail_url, download_match.group(1))
            download_html, final_download_url = _apkcombo_fetch_text(
                download_url,
                timeout_seconds=timeout_seconds,
                referer=detail_url,
            )
            if _apkcombo_page_says_not_found(download_html):
                not_found_result = {
                    "code": "APKCOMBO_NOT_FOUND",
                    "available": False,
                    "message": "APKCombo 下载页提示 application was not found",
                    "package_name": package_name,
                    "search_url": search_url,
                    "detail_url": detail_url,
                    "download_url": final_download_url,
                }
                if attempt + 1 < max(1, attempts):
                    if retry_interval_seconds > 0:
                        time.sleep(retry_interval_seconds)
                    continue
                return not_found_result

            # Current APKCombo pages expose a signed /r2 link for real files.
            if re.search(r'class=["\'][^"\']*\bvariant\b', download_html, re.I) or re.search(
                r'href=["\']/r2\?u=', download_html,
                re.I,
            ):
                return {
                    "code": "APKCOMBO_AVAILABLE",
                    "available": True,
                    "message": "APKCombo 已找到完全一致的包名和可下载版本",
                    "package_name": package_name,
                    "search_url": search_url,
                    "detail_url": detail_url,
                    "download_url": final_download_url,
                }

            # Some old app pages load the real download result through a POST.
            dynamic_path = ""
            dynamic_match = re.search(
                r'fetchData\(["\']([^"\']+)["\']\)', download_html
            )
            if dynamic_match:
                dynamic_path = dynamic_match.group(1)
            else:
                # Legacy pages concatenate a per-app xid in JavaScript:
                # fetchData("/slug/package/" + xid + "/dl")
                xid_match = re.search(
                    r'var\s+xid\s*=\s*["\']([^"\']+)["\']', download_html
                )
                base_match = re.search(
                    r'fetchData\(["\']([^"\']+)["\']\s*\+\s*xid\s*\+\s*["\']([^"\']+)["\']\)',
                    download_html,
                )
                if xid_match and base_match:
                    dynamic_path = (
                        base_match.group(1) + xid_match.group(1) + base_match.group(2)
                    )
            if dynamic_path:
                dynamic_url = urllib.parse.urljoin(final_download_url, dynamic_path)
                form_data = urllib.parse.urlencode({
                    "package_name": package_name,
                    "version": "",
                }).encode("utf-8")
                dynamic_html, _ = _apkcombo_fetch_text(
                    dynamic_url,
                    timeout_seconds=timeout_seconds,
                    data=form_data,
                    referer=final_download_url,
                )
                if _apkcombo_page_says_not_found(dynamic_html):
                    not_found_result = {
                        "code": "APKCOMBO_NOT_FOUND",
                        "available": False,
                        "message": "APKCombo 下载数据提示 application was not found",
                        "package_name": package_name,
                        "search_url": search_url,
                        "detail_url": detail_url,
                        "download_url": final_download_url,
                    }
                    if attempt + 1 < max(1, attempts):
                        if retry_interval_seconds > 0:
                            time.sleep(retry_interval_seconds)
                        continue
                    return not_found_result
                if re.search(r'class=["\'][^"\']*\bvariant\b', dynamic_html, re.I) or re.search(
                    r'href=["\']/r2\?u=', dynamic_html,
                    re.I,
                ):
                    return {
                        "code": "APKCOMBO_AVAILABLE",
                        "available": True,
                        "message": "APKCombo 已找到完全一致的包名和可下载版本",
                        "package_name": package_name,
                        "search_url": search_url,
                        "detail_url": detail_url,
                        "download_url": final_download_url,
                    }

            last_error = "APKCombo 页面已打开，但未取得明确的下载结果"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = str(exc) or exc.__class__.__name__
        if attempt + 1 < max(1, attempts) and retry_interval_seconds > 0:
            time.sleep(retry_interval_seconds)

    return {
        "code": "APKCOMBO_CHECK_FAILED",
        "available": None,
        "message": last_error or "APKCombo 自动检查失败，需要人工确认",
        "package_name": package_name,
        "search_url": search_url,
    }


def apply_apkcombo_check_to_precheck_result(result: dict) -> dict:
    """Resolve Play download restrictions through APKCombo when appropriate."""
    if result.get("code") not in {
        "GOOGLE_NO_PACKAGE",
        "DEVICE_UNSUPPORTED",
        "COUNTRY_UNSUPPORTED",
    }:
        return result
    package_name = str(result.get("package_name") or "").strip()
    apkcombo = inspect_apkcombo_package(package_name)
    original_title = str(result.get("title") or result.get("code") or "Google Play 无法下载")
    evidence = list(result.get("evidence") or [])
    evidence.append(apkcombo.get("message") or "已检查 APKCombo")
    source = str(result.get("source") or "").strip()
    source = f"{source} + APKCombo" if source else "APKCombo"

    if apkcombo.get("available") is False:
        return {
            **result,
            "code": "ALL_NETWORK_NO_PACKAGE",
            "title": "全网无包",
            "detail": (
                f"Google Play 结论：{original_title}；"
                f"{apkcombo.get('message')}。按当前规则判定全网无包，暂不适配。"
            ),
            "continue_adaptation": False,
            "evidence": evidence,
            "source": source,
            "apkcombo_result": apkcombo,
        }
    if apkcombo.get("available") is True:
        return {
            **result,
            "code": "APKCOMBO_AVAILABLE",
            "title": "Google Play 无法下载，但 APKCombo 有包",
            "detail": (
                f"Google Play 结论：{original_title}；APKCombo 已找到完全一致包名的"
                "可下载版本，请通过第三方包体继续后续检查。"
            ),
            "continue_adaptation": False,
            "evidence": evidence,
            "source": source,
            "apkcombo_result": apkcombo,
        }
    return {
        **result,
        "code": "APKCOMBO_CHECK_FAILED",
        "title": "APKCombo 自动核验未完成",
        "detail": (
            f"Google Play 结论：{original_title}；APKCombo 自动检查没有取得明确结果，"
            "为避免误判全网无包，需要人工确认。"
        ),
        "continue_adaptation": None,
        "evidence": evidence,
        "source": source,
        "apkcombo_result": apkcombo,
    }


def _normalized_play_store_texts(texts: list[str]) -> list[str]:
    result = []
    seen = set()
    for raw in texts:
        value = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _play_store_has_phrase(texts: list[str], phrases: tuple[str, ...]) -> bool:
    lowered = [text.casefold() for text in texts]
    return any(phrase.casefold() in text for text in lowered for phrase in phrases)


def _is_japanese_play_store_listing(package_name: str, texts: list[str]) -> bool:
    """Recognize a Japanese-targeted package using both package and page text."""
    segments = [part.casefold() for part in str(package_name or "").split(".")]
    if "jp" not in segments:
        return False
    joined = "\n".join(texts)
    # Kana is specific enough to distinguish Japanese copy from Chinese UI
    # text, which shares many CJK ideographs.  Require several characters so a
    # single developer symbol cannot blacklist an otherwise unrelated app.
    kana = re.findall(r"[\u3040-\u30ff\uff66-\uff9f]", joined)
    japanese_lines = sum(
        bool(re.search(r"[\u3040-\u30ff\uff66-\uff9f]", text)) for text in texts
    )
    return len(kana) >= 6 or japanese_lines >= 2


def classify_google_play_page_texts(
    texts: list[str], package_name: str = ""
) -> dict:
    """Classify the visible Play Store page from UI/OCR text.

    Absence of a monetization label is only treated as meaningful after an
    action button or another conclusive Play Store state proves the page loaded.
    """
    visible_texts = _normalized_play_store_texts(texts)
    normalized = [text.casefold() for text in visible_texts]
    has_ads = _play_store_has_phrase(visible_texts, PLAY_STORE_AD_TEXTS)
    has_iap = _play_store_has_phrase(visible_texts, PLAY_STORE_IAP_TEXTS)
    device_unsupported = _play_store_has_phrase(
        visible_texts, PLAY_STORE_DEVICE_UNSUPPORTED_TEXTS
    )
    country_unsupported = _play_store_has_phrase(
        visible_texts, PLAY_STORE_COUNTRY_UNSUPPORTED_TEXTS
    )
    item_not_found = _play_store_has_phrase(
        visible_texts, PLAY_STORE_ITEM_NOT_FOUND_TEXTS
    )
    japanese_package = _is_japanese_play_store_listing(
        package_name, visible_texts
    )
    page_ready = (
        has_ads
        or has_iap
        or device_unsupported
        or country_unsupported
        or item_not_found
        or any(text in PLAY_STORE_READY_TEXTS for text in normalized)
    )

    # The requested listing does not exist, so no download or adaptation can run.
    # This is more conclusive than any cached metadata on the screen.
    if item_not_found:
        code = "GOOGLE_NO_PACKAGE"
        title = "Google Play 无包"
        detail = "Google Play 提示 Item not found，链接对应的应用不存在、已下架或无法获取。"
        continue_adaptation = False
    elif japanese_package:
        code = "JAPANESE_PACKAGE"
        title = "检测到日本包体"
        detail = (
            "包名包含独立的 jp 段，且 Google Play 页面检测到明显日文内容；"
            "按当前规则直接加黑并跳过下载和适配。"
        )
        continue_adaptation = False
    # Monetization is the primary business decision for a real Play page.
    # Compatibility matters only for apps that actually contain ads.
    elif has_ads:
        if country_unsupported:
            code = "COUNTRY_UNSUPPORTED"
            title = "包含广告，但所在国家或地区不支持"
            detail = "页面标注包含广告，但当前账号所在国家或地区无法下载此应用。"
            continue_adaptation = False
        elif device_unsupported:
            code = "DEVICE_UNSUPPORTED"
            title = "包含广告，但当前设备不支持"
            detail = "页面标注包含广告，但当前设备与此应用不兼容。"
            continue_adaptation = False
        else:
            code = "HAS_ADS"
            title = "检测到包含广告"
            detail = "页面明确标注包含广告，可以继续下载安装和适配。"
            continue_adaptation = True
    elif has_iap and page_ready:
        code = "IAP_ONLY"
        title = "仅检测到应用内购"
        detail = "页面标注应用内购，但未标注包含广告；按当前规则应加黑并跳过。"
        continue_adaptation = False
    elif page_ready:
        code = "NO_ADS_OR_IAP"
        title = "未发现广告或应用内购标识（待人工确认）"
        detail = (
            "页面已加载，但未发现包含广告或应用内购标识；该结果可能是页面信息未完整展示，"
            "不能据此加黑。将继续下载安装，交由人工确认是否有广告。"
        )
        continue_adaptation = True
    else:
        code = "UNKNOWN"
        title = "暂时无法判断"
        detail = "没有取得足够的 Play Store 页面信息，请等待页面加载后重试。"
        continue_adaptation = None

    evidence = []
    if has_ads:
        evidence.append("发现“包含广告 / Contains ads”标识")
    if has_iap:
        evidence.append("发现“应用内购 / In-app purchases”标识")
    if device_unsupported:
        evidence.append("发现设备不兼容提示")
    if country_unsupported:
        evidence.append("发现国家或地区不可用提示")
    if item_not_found:
        evidence.append("发现“Item not found / 找不到此商品”提示")
    if japanese_package:
        evidence.append("包名包含独立的“jp”段")
        evidence.append("Google Play 页面包含明显日文内容")
    if page_ready:
        evidence.append("Play Store 页面已加载")

    return {
        "code": code,
        "title": title,
        "detail": detail,
        "continue_adaptation": continue_adaptation,
        "page_ready": page_ready,
        "contains_ads": has_ads,
        "contains_iap": has_iap,
        "device_supported": False if device_unsupported else None,
        "country_supported": False if country_unsupported else None,
        "item_found": not item_not_found if item_not_found else None,
        "is_japanese_package": japanese_package,
        "evidence": evidence,
        "visible_texts": visible_texts,
    }


def parse_uiautomator_texts(xml_text: str) -> list[str]:
    """Extract visible text and accessibility descriptions from a UI dump."""
    nodes = parse_uiautomator_nodes(xml_text)
    values = []
    for node in nodes:
        values.extend((node.get("text", ""), node.get("content_desc", "")))
    return _normalized_play_store_texts(values)


def parse_uiautomator_nodes(xml_text: str) -> list[dict]:
    """Extract UI node text, clickability and screen bounds from a dump."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    nodes = []
    for node in root.iter("node"):
        nodes.append({
            "text": node.attrib.get("text", "").strip(),
            "content_desc": node.attrib.get("content-desc", "").strip(),
            "resource_id": node.attrib.get("resource-id", "").strip(),
            "clickable": node.attrib.get("clickable", "false").lower() == "true",
            "enabled": node.attrib.get("enabled", "true").lower() == "true",
            "bounds": node.attrib.get("bounds", "").strip(),
        })
    return nodes


def open_google_play_page(value: str) -> tuple[bool, str, str]:
    """Open a Google Play details page on the connected Android device."""
    package_name = resolve_google_play_package(value)
    if not package_name:
        return False, "请输入有效的 Google Play 链接或应用包名", ""
    try:
        result = _run_adb(
            [
                "shell", "am", "start", "-W",
                "-a", "android.intent.action.VIEW",
                "-d", f"market://details?id={package_name}",
                "-p", "com.android.vending",
            ],
            timeout=15,
        )
    except FileNotFoundError:
        return False, "未找到 ADB 工具", package_name
    except subprocess.TimeoutExpired:
        return False, "打开 Google Play 页面超时", package_name
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "打开失败").strip()
        return False, message, package_name
    return True, "已在手机上打开 Google Play 页面", package_name


def collect_device_ui_nodes() -> list[dict]:
    """Dump the current Android UI hierarchy and return inspectable nodes."""
    remote_path = "/sdcard/apk_tool_precheck_window.xml"
    dump_result = _run_adb(
        ["shell", "uiautomator", "dump", remote_path], timeout=10
    )
    if dump_result.returncode != 0:
        return []
    try:
        read_result = _run_adb(["shell", "cat", remote_path], timeout=5)
        if read_result.returncode != 0:
            return []
        return parse_uiautomator_nodes(read_result.stdout)
    finally:
        try:
            _run_adb(["shell", "rm", "-f", remote_path], timeout=3)
        except Exception:
            pass


def collect_device_ui_texts() -> list[str]:
    """Dump the current Android UI hierarchy and return visible text."""
    values = []
    for node in collect_device_ui_nodes():
        values.extend((node.get("text", ""), node.get("content_desc", "")))
    return _normalized_play_store_texts(values)


def is_package_installed(package_name: str) -> bool:
    """Return whether Android's package manager can resolve the package."""
    if not resolve_google_play_package(package_name):
        return False
    try:
        result = _run_adb(["shell", "pm", "path", package_name], timeout=8)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and "package:" in (result.stdout or "")


def _node_label(node: dict) -> str:
    return (node.get("text") or node.get("content_desc") or "").strip().casefold()


def _node_center(node: dict) -> tuple[int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        return None
    left, top, right, bottom = (int(value) for value in match.groups())
    if right <= left or bottom <= top:
        return None
    return (left + right) // 2, (top + bottom) // 2


def _find_action_node(nodes: list[dict], labels: set[str]) -> dict | None:
    normalized_labels = {label.casefold() for label in labels}
    for node in nodes:
        if not node.get("enabled", True):
            continue
        if _node_label(node) in normalized_labels and _node_center(node) is not None:
            return node
    return None


def _tap_ui_node(node: dict) -> bool:
    center = _node_center(node)
    if center is None:
        return False
    try:
        result = _run_adb(
            ["shell", "input", "tap", str(center[0]), str(center[1])], timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


NOTIFICATION_PERMISSION_PROMPT_PHRASES = (
    "send you notifications",
    "send notifications",
    "发送通知",
    "通知权限",
)
NOTIFICATION_PERMISSION_DENY_LABELS = {
    "don't allow",
    "don’t allow",
    "不允许",
    "拒绝",
}


def dismiss_notification_permission_dialog() -> dict:
    """Dismiss only Android's notification permission prompt.

    Other permission prompts are intentionally left untouched because denying
    storage, camera or other capabilities can change the app's test behavior.
    """
    try:
        nodes = collect_device_ui_nodes()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {
            "dismissed": False,
            "code": "UI_READ_FAILED",
            "message": str(exc) or "无法读取设备界面",
        }
    visible = "\n".join(
        str(value or "").casefold()
        for node in nodes
        for value in (node.get("text"), node.get("content_desc"))
        if value
    )
    if not any(
        phrase.casefold() in visible
        for phrase in NOTIFICATION_PERMISSION_PROMPT_PHRASES
    ):
        return {
            "dismissed": False,
            "code": "NO_NOTIFICATION_PERMISSION_DIALOG",
            "message": "未发现通知权限弹窗",
        }

    deny_node = _find_action_node(nodes, NOTIFICATION_PERMISSION_DENY_LABELS)
    if deny_node is None:
        deny_node = next(
            (
                node
                for node in nodes
                if node.get("enabled", True)
                and str(node.get("resource_id") or "").endswith(
                    (
                        "permission_deny_button",
                        "permission_deny_and_dont_ask_again_button",
                    )
                )
                and _node_center(node) is not None
            ),
            None,
        )
    if deny_node is None:
        return {
            "dismissed": False,
            "code": "NOTIFICATION_DENY_BUTTON_NOT_FOUND",
            "message": "发现通知权限弹窗，但没有找到“不允许”按钮",
        }
    if not _tap_ui_node(deny_node):
        return {
            "dismissed": False,
            "code": "NOTIFICATION_DENY_TAP_FAILED",
            "message": "点击通知权限“不允许”按钮失败",
        }
    return {
        "dismissed": True,
        "code": "NOTIFICATION_PERMISSION_DISMISSED",
        "message": "已自动点击通知权限“不允许”，继续聚合适配",
    }


def install_google_play_app(
    package_name: str,
    timeout_seconds: float = 600,
    poll_interval_seconds: float = 3,
    on_progress=None,
    return_after_start: bool = False,
) -> dict:
    """Click Play Store's Install button and optionally wait for installation."""
    package_name = resolve_google_play_package(package_name)
    if not package_name:
        return {
            "ok": False,
            "code": "INVALID_PACKAGE",
            "message": "无效的应用包名",
        }
    if is_package_installed(package_name):
        return {
            "ok": True,
            "code": "ALREADY_INSTALLED",
            "message": "应用已经安装，无需重复下载",
        }

    install_labels = {"安装", "install"}
    confirmation_labels = {
        "安装", "install", "继续", "continue", "确定", "ok", "接受", "accept"
    }
    deadline = time.monotonic() + max(1, timeout_seconds)
    install_clicked = False
    last_texts: list[str] = []

    while time.monotonic() < deadline:
        if is_package_installed(package_name):
            return {
                "ok": True,
                "code": "INSTALLED",
                "message": "Google Play 下载并安装完成",
            }

        try:
            nodes = collect_device_ui_nodes()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            nodes = []
        texts = _normalized_play_store_texts([
            value
            for node in nodes
            for value in (node.get("text", ""), node.get("content_desc", ""))
        ])
        if texts:
            last_texts = texts
        page_result = classify_google_play_page_texts(texts, package_name)
        if page_result["code"] in {
            "GOOGLE_NO_PACKAGE",
            "DEVICE_UNSUPPORTED",
            "COUNTRY_UNSUPPORTED",
        }:
            return {
                "ok": False,
                "code": page_result["code"],
                "message": page_result["detail"],
            }
        if _play_store_has_phrase(texts, PLAY_STORE_AUTH_REQUIRED_TEXTS):
            return {
                "ok": False,
                "code": "AUTH_REQUIRED",
                "message": "Google Play 要求登录或身份验证，已停止自动下载",
            }

        labels = confirmation_labels if install_clicked else install_labels
        action_node = _find_action_node(nodes, labels)
        if action_node is not None:
            if _tap_ui_node(action_node):
                install_clicked = True
                if on_progress:
                    on_progress("已点击安装，Google Play 将在后台下载")
                if return_after_start:
                    return {
                        "ok": None,
                        "code": "DOWNLOAD_STARTED",
                        "message": "已发起 Google Play 下载，转入后台等待安装",
                    }
                if poll_interval_seconds > 0:
                    time.sleep(poll_interval_seconds)
                continue

        if not install_clicked and texts:
            # A conclusive page without an Install button should not be tapped by coordinates.
            if any(text.casefold() in {"play", "打开", "uninstall", "卸载"} for text in texts):
                return {
                    "ok": False,
                    "code": "INSTALL_BUTTON_NOT_FOUND",
                    "message": "页面没有找到“安装”按钮，且系统未检测到应用已安装",
                }

        if on_progress:
            on_progress(
                "正在等待安装完成..." if install_clicked else "正在等待“安装”按钮出现..."
            )
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)

    if not install_clicked:
        message = "等待 Google Play“安装”按钮超时"
    else:
        message = "Google Play 下载或安装超时"
    return {
        "ok": False,
        "code": "INSTALL_TIMEOUT",
        "message": message,
        "visible_texts": last_texts,
    }


def extract_package_crash_evidence(log_text: str, package_name: str) -> dict:
    """Extract explicit Java/native crash evidence for one package."""
    package_name = package_name.strip()
    lines = [line for line in (log_text or "").splitlines() if line.strip()]
    joined = "\n".join(lines)
    package_pattern = re.escape(package_name)

    java_crash = bool(
        re.search(r"FATAL EXCEPTION", joined, re.I)
        and re.search(rf"Process:\s*{package_pattern}(?:\s|,|$)", joined, re.I)
    )
    am_crash = bool(
        re.search(rf"am_crash[^\n]*\b{package_pattern}\b", joined, re.I)
    )
    native_crash = bool(
        re.search(
            rf"Fatal signal\s+\d+[^\n]*(?:\({package_pattern}\)|\b{package_pattern}\b)",
            joined,
            re.I,
        )
        or (
            re.search(rf">>>\s*{package_pattern}\s*<<<", joined, re.I)
            and re.search(r"signal\s+\d+|backtrace:", joined, re.I)
        )
    )

    if java_crash or am_crash:
        crash_type = "JAVA_CRASH"
    elif native_crash:
        crash_type = "NATIVE_CRASH"
    else:
        crash_type = ""

    relevant = []
    important_patterns = (
        package_name,
        "FATAL EXCEPTION",
        "AndroidRuntime",
        "Fatal signal",
        "am_crash",
        "backtrace:",
        "Caused by:",
    )
    for line in lines:
        if any(pattern.casefold() in line.casefold() for pattern in important_patterns):
            relevant.append(line.strip())
    relevant = relevant[-12:]

    return {
        "crashed": bool(crash_type),
        "crash_type": crash_type,
        "summary": "\n".join(relevant),
    }


def _is_app_process_running(package_name: str) -> bool:
    try:
        result = _run_adb(["shell", "pidof", package_name], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode == 0 and bool((result.stdout or "").strip()):
        return True

    # Some apps hand work to a package-prefixed secondary process before the
    # canonical process is visible to pidof.  Accept only exact package names
    # or package:name children so similarly named apps cannot create a match.
    try:
        processes = _run_adb(["shell", "ps", "-A"], timeout=8)
        for line in (processes.stdout or "").splitlines()[1:]:
            columns = line.split()
            process_name = columns[-1] if columns else ""
            if process_name == package_name or process_name.startswith(
                package_name + ":"
            ):
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # A resumed Activity is also strong launch evidence during the short
    # interval in which process listings can lag behind ActivityManager.
    try:
        activities = _run_adb(
            ["shell", "dumpsys", "activity", "activities"], timeout=8
        )
        activity_text = (activities.stdout or "") + (activities.stderr or "")
        for line in activity_text.splitlines():
            if ("mResumedActivity" in line or "topResumedActivity" in line) and (
                package_name + "/" in line
            ):
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return False


def _read_crash_logcat() -> str:
    try:
        result = _run_adb(["logcat", "-b", "all", "-d"], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


class PackageRuntimeMonitor:
    """Detect a target app crashing or unexpectedly exiting after launch.

    The monitor intentionally requires two consecutive missing-process polls
    before reporting an exit.  This avoids treating a short process hand-off as
    a crash, while explicit Java/native crash evidence is reported immediately.
    """

    def __init__(
        self,
        package_name: str,
        missing_threshold: int = 2,
        startup_grace_seconds: float = 25,
    ):
        self.package_name = resolve_google_play_package(package_name)
        self.missing_threshold = max(1, int(missing_threshold))
        self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        self.reset()

    def reset(self) -> None:
        """Begin a fresh launch observation window after an app restart."""
        self.started_at = time.monotonic()
        self.seen_running = False
        self.consecutive_missing = 0

    def poll(self) -> dict:
        if not self.package_name:
            return {
                "ok": False,
                "code": "INVALID_PACKAGE",
                "message": "无效的应用包名",
            }

        if _is_app_process_running(self.package_name):
            self.seen_running = True
            self.consecutive_missing = 0
            return {"ok": True, "code": "APP_RUNNING", "message": "应用运行中"}

        log_text = _read_crash_logcat()
        evidence = extract_package_crash_evidence(log_text, self.package_name)
        if evidence["crashed"]:
            return {
                "ok": False,
                "code": "APP_CRASHED",
                "message": "包体在自动化检测过程中闪退，暂不适配",
                **evidence,
            }

        # ``monkey`` can return before Android has created the target process.
        # A missing PID during this startup window is not an app exit and must
        # never be labelled as a crash.  Only begin exit counting after the
        # target process has actually been observed once.
        if not self.seen_running:
            self.consecutive_missing = 0
            elapsed = time.monotonic() - self.started_at
            if elapsed < self.startup_grace_seconds:
                return {
                    "ok": True,
                    "code": "APP_STARTING",
                    "message": "应用进程尚在启动宽限期内",
                }
            return {
                "ok": False,
                "code": "APP_LAUNCH_NOT_CONFIRMED",
                "message": (
                    f"启动应用后 {int(self.startup_grace_seconds)} 秒内"
                    "未检测到目标进程，无法进行自动化检测"
                ),
                "summary": evidence.get("summary", ""),
                "crashed": False,
                "crash_type": "",
            }

        self.consecutive_missing += 1

        if self.consecutive_missing >= self.missing_threshold:
            return {
                "ok": False,
                "code": "APP_EXITED_DURING_AUTOMATION",
                "message": "包体在自动化检测过程中异常退出，疑似闪退，暂不适配",
                "summary": evidence.get("summary", ""),
                "crashed": False,
                "crash_type": "",
            }

        return {
            "ok": True,
            "code": "APP_PROCESS_MISSING_ONCE",
            "message": "暂未检测到应用进程，等待再次确认",
        }


def run_app_launch_precheck(
    package_name: str,
    observation_seconds: float = 20,
    poll_interval_seconds: float = 2,
    on_progress=None,
) -> dict:
    """Launch an installed app, detect explicit crashes, then force-stop it."""
    package_name = resolve_google_play_package(package_name)
    if not package_name:
        return {
            "ok": False,
            "code": "INVALID_PACKAGE",
            "message": "无效的应用包名",
        }
    if not is_package_installed(package_name):
        return {
            "ok": False,
            "code": "NOT_INSTALLED",
            "message": "应用尚未安装，无法执行启动预检",
        }

    try:
        _run_adb(["shell", "am", "force-stop", package_name], timeout=8)
        _run_adb(["logcat", "-c"], timeout=8)
        launch = _run_adb(
            [
                "shell", "monkey", "-p", package_name,
                "-c", "android.intent.category.LAUNCHER", "1",
            ],
            timeout=15,
        )
        launch_output = ((launch.stdout or "") + (launch.stderr or "")).strip()
        if launch.returncode != 0 or "No activities found" in launch_output:
            return {
                "ok": False,
                "code": "LAUNCH_FAILED",
                "message": "应用无法正常启动",
                "summary": launch_output[-1500:],
            }

        if on_progress:
            on_progress(f"应用已启动，观察 {int(observation_seconds)} 秒是否闪退...")

        deadline = time.monotonic() + max(1, observation_seconds)
        seen_running = False
        consecutive_missing = 0
        while time.monotonic() < deadline:
            running = _is_app_process_running(package_name)
            if running:
                seen_running = True
                consecutive_missing = 0
            elif seen_running:
                consecutive_missing += 1
                if consecutive_missing >= 2:
                    log_text = _read_crash_logcat()
                    evidence = extract_package_crash_evidence(log_text, package_name)
                    if evidence["crashed"]:
                        return {
                            "ok": False,
                            "code": "APP_CRASHED",
                            "message": "包体闪退，暂不适配",
                            **evidence,
                        }
                    return {
                        "ok": False,
                        "code": "APP_EXITED",
                        "message": "应用启动后进程退出，但没有取得明确崩溃堆栈",
                        "summary": evidence.get("summary", ""),
                    }

            if on_progress:
                remaining = max(0, int(deadline - time.monotonic()))
                on_progress(f"启动预检中，剩余约 {remaining} 秒...")
            if poll_interval_seconds > 0:
                time.sleep(poll_interval_seconds)

        log_text = _read_crash_logcat()
        evidence = extract_package_crash_evidence(log_text, package_name)
        if evidence["crashed"]:
            return {
                "ok": False,
                "code": "APP_CRASHED",
                "message": "包体闪退，暂不适配",
                **evidence,
            }
        if not seen_running:
            return {
                "ok": False,
                "code": "LAUNCH_FAILED",
                "message": "发送启动命令后未检测到应用进程",
                "summary": evidence.get("summary", ""),
            }
        return {
            "ok": True,
            "code": "LAUNCH_OK",
            "message": f"应用持续运行 {int(observation_seconds)} 秒，未发现闪退",
            "summary": "",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "code": "ADB_NOT_FOUND",
            "message": "未找到 ADB 工具",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "code": "LAUNCH_CHECK_TIMEOUT",
            "message": "应用启动预检命令超时",
        }
    finally:
        try:
            _run_adb(["shell", "am", "force-stop", package_name], timeout=8)
        except Exception:
            pass


def collect_device_ocr_text() -> str:
    """OCR the current device screen in memory when Tesseract is available."""
    adb = get_adb_path()
    tesseract = shutil.which("tesseract")
    if not adb or not tesseract:
        return ""
    try:
        screenshot = subprocess.run(
            [adb, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=10,
        )
        if screenshot.returncode != 0 or not screenshot.stdout:
            return ""
        languages = subprocess.run(
            [tesseract, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()
        language = "eng+chi_sim" if "chi_sim" in languages else "eng"
        result = subprocess.run(
            [tesseract, "stdin", "stdout", "-l", language, "--psm", "6"],
            input=screenshot.stdout,
            capture_output=True,
            timeout=20,
        )
        return result.stdout.decode("utf-8", errors="replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def run_google_play_precheck(
    value: str,
    attempts: int = 5,
    interval_seconds: float = 2.0,
    use_ocr: bool = True,
    verify_apkcombo: bool = False,
) -> dict:
    """Open and inspect a Play Store page without downloading the app."""
    if not check_device():
        return {
            "code": "NO_DEVICE",
            "title": "没有已连接的设备",
            "detail": "请连接并授权 Android 设备后重试。",
            "continue_adaptation": None,
            "page_ready": False,
            "evidence": [],
            "visible_texts": [],
            "package_name": resolve_google_play_package(value),
            "source": "",
        }

    opened, message, package_name = open_google_play_page(value)
    if not opened:
        return {
            "code": "OPEN_FAILED",
            "title": "Google Play 页面打开失败",
            "detail": message,
            "continue_adaptation": None,
            "page_ready": False,
            "evidence": [],
            "visible_texts": [],
            "package_name": package_name,
            "source": "",
        }

    last_result = None
    stable_skip_code = None
    stable_skip_count = 0
    for _ in range(max(1, attempts)):
        if interval_seconds > 0:
            time.sleep(interval_seconds)
        try:
            texts = collect_device_ui_texts()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            texts = []
        result = classify_google_play_page_texts(texts, package_name)
        result["package_name"] = package_name
        result["source"] = "UI 控件"
        last_result = result

        if result["code"] in {
            "HAS_ADS",
            "GOOGLE_NO_PACKAGE",
            "DEVICE_UNSUPPORTED",
            "COUNTRY_UNSUPPORTED",
            "JAPANESE_PACKAGE",
        }:
            return (
                apply_apkcombo_check_to_precheck_result(result)
                if verify_apkcombo
                else result
            )
        if result["code"] in {
            "IAP_ONLY", "NO_ADS_OR_IAP", "JAPANESE_PACKAGE"
        }:
            if stable_skip_code == result["code"]:
                stable_skip_count += 1
            else:
                stable_skip_code = result["code"]
                stable_skip_count = 1
            if stable_skip_count >= 2:
                return result
        else:
            stable_skip_code = None
            stable_skip_count = 0

    if use_ocr:
        ocr_text = collect_device_ocr_text()
        if ocr_text:
            ui_texts = last_result.get("visible_texts", []) if last_result else []
            result = classify_google_play_page_texts(
                ui_texts + ocr_text.splitlines(), package_name
            )
            result["package_name"] = package_name
            result["source"] = "UI 控件 + OCR"
            if result["code"] in {
                "HAS_ADS",
                "GOOGLE_NO_PACKAGE",
                "IAP_ONLY",
                "NO_ADS_OR_IAP",
                "DEVICE_UNSUPPORTED",
                "COUNTRY_UNSUPPORTED",
                "JAPANESE_PACKAGE",
            }:
                return (
                    apply_apkcombo_check_to_precheck_result(result)
                    if verify_apkcombo
                    else result
                )
            last_result = result

    if last_result and last_result.get("code") == "IAP_ONLY":
        last_result = {
            **last_result,
            "code": "UNKNOWN",
            "title": "页面结果尚未稳定",
            "detail": "页面信息只成功读取到一次，为避免误加黑，请等待页面加载后重试。",
            "continue_adaptation": None,
        }

    if last_result is None:
        last_result = classify_google_play_page_texts([], package_name)
        last_result["package_name"] = package_name
        last_result["source"] = ""
    return last_result


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


def get_adb_connection_state() -> str:
    """Return a stable ADB connection state for user-facing diagnostics."""
    if not get_adb_path():
        return "missing"
    try:
        result = _run_adb(["devices"], timeout=3)
    except FileNotFoundError:
        return "missing"
    except subprocess.TimeoutExpired:
        return "server_timeout"

    states = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            states.append(parts[1].casefold())
    if "device" in states:
        return "device"
    if "unauthorized" in states:
        return "unauthorized"
    if "offline" in states:
        return "offline"
    return "no_device"


def _logcat_connection_error(state: str) -> tuple[str, str, bool]:
    """Map an ADB state to code, message and whether retrying is safe."""
    mapping = {
        "missing": ("ADB_NOT_FOUND", "未找到 ADB 工具", False),
        "unauthorized": (
            "ADB_UNAUTHORIZED",
            "设备未授权 ADB，请在手机上确认 USB 调试授权",
            False,
        ),
        "offline": ("ADB_OFFLINE", "ADB 设备处于 offline 状态", True),
        "no_device": ("ADB_NO_DEVICE", "未检测到 ADB 设备", True),
        "server_timeout": ("ADB_SERVER_TIMEOUT", "ADB 服务响应超时", True),
    }
    return mapping.get(
        state,
        ("LOGCAT_READ_TIMEOUT", "设备在线，但 Logcat 读取超时", True),
    )


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
    try:
        if len(apk_list) == 1:
            result = _run_adb(
                ["install", "-r", apk_list[0]],
                timeout=APK_INSTALL_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                return True, "安装成功"
            return False, f"安装失败: {result.stderr.strip() or result.stdout.strip()}"

        timeout = max(
            APK_INSTALL_TIMEOUT_SECONDS,
            len(apk_list) * SPLIT_APK_INSTALL_TIMEOUT_SECONDS_PER_APK,
        )
        result = _run_adb(
            ["install-multiple", "-r"] + _sort_apks_for_install(apk_list),
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, f"安装成功 ({len(apk_list)} 个 APK)"
        return False, f"安装失败: {result.stderr.strip() or result.stdout.strip()}"
    except subprocess.TimeoutExpired:
        if len(apk_list) == 1:
            return False, f"安装超时：已等待 {APK_INSTALL_TIMEOUT_SECONDS} 秒"
        return False, f"拆分 APK 安装超时：共 {len(apk_list)} 个 APK，请检查设备连接或稍后重试"


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
    package_name = package_name.strip()
    if not package_name:
        return False, "请输入包名"
    try:
        _run_adb(["shell", "am", "force-stop", package_name], timeout=5)
        result = _run_adb(
            ["shell", "pm", "clear", package_name],
            timeout=8,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0 and "Success" in output:
            return True, "缓存清除成功"
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


def cancel_zygotehole_injection(package_name: str) -> tuple[bool, str]:
    """Remove one package from the active device config and stop its process."""
    package_name = package_name.strip()
    if not package_name:
        return False, "请输入包名"

    local_temp_path = ""
    try:
        read_result = _run_adb(
            ["shell", "cat", ZYGOTEHOLE_CONFIG_PATH],
            timeout=8,
        )
        if read_result.returncode != 0:
            detail = (read_result.stderr or read_result.stdout).strip()
            return False, f"读取设备注入配置失败: {detail or '未知错误'}"

        try:
            current_config = json.loads(read_result.stdout)
            updated_config, removed_count = remove_package_from_zygotehole_config(
                current_config,
                package_name,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            return False, str(exc)

        if removed_count == 0:
            return False, f"设备注入配置中没有找到 {package_name}"

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as temp_file:
            json.dump(updated_config, temp_file, indent=2, ensure_ascii=False)
            temp_file.write("\n")
            local_temp_path = temp_file.name

        push_result = _run_adb(
            ["push", local_temp_path, ZYGOTEHOLE_CONFIG_TEMP_PATH],
            timeout=10,
        )
        if push_result.returncode != 0:
            detail = (push_result.stderr or push_result.stdout).strip()
            return False, f"上传新注入配置失败: {detail or '未知错误'}"

        replace_result = _run_adb(
            [
                "shell",
                (
                    f"chmod 777 {ZYGOTEHOLE_CONFIG_TEMP_PATH} && "
                    f"mv {ZYGOTEHOLE_CONFIG_TEMP_PATH} {ZYGOTEHOLE_CONFIG_PATH} && "
                    f"chmod 777 {ZYGOTEHOLE_CONFIG_PATH}"
                ),
            ],
            timeout=8,
        )
        if replace_result.returncode != 0:
            detail = (replace_result.stderr or replace_result.stdout).strip()
            return False, f"替换设备注入配置失败: {detail or '未知错误'}"

        stop_result = _run_adb(
            ["shell", "am", "force-stop", package_name],
            timeout=8,
        )
        remaining_count = len(updated_config["data"])
        if stop_result.returncode != 0:
            detail = (stop_result.stderr or stop_result.stdout).strip()
            return True, (
                f"已取消 {package_name} 的注入，保留其他配置 {remaining_count} 条；"
                f"但强制停止失败，请手动结束游戏: {detail or '未知错误'}"
            )
        return True, (
            f"已取消 {package_name} 的注入并强制停止游戏，"
            f"保留其他配置 {remaining_count} 条"
        )
    except FileNotFoundError:
        return False, "未找到 ADB 工具"
    except subprocess.TimeoutExpired:
        return False, "取消注入超时，请检查设备连接"
    finally:
        if local_temp_path:
            try:
                os.remove(local_temp_path)
            except OSError:
                pass


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
    adb_cmd = shlex.quote(adb)
    pkg = shlex.quote(package_name)
    return [
        "sh",
        "-c",
        f"{adb_cmd} shell am force-stop {pkg} && "
        f"{adb_cmd} shell pm clear {pkg}",
    ]


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
    final = normalize_optional_parameter(fields.get("最终判断", ""))
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
        val = normalize_optional_parameter(fields.get(src, ""))
        if val:
            params[dst] = val

    single_value_field_map = {
        "插屏聚合id": "aggr_chaping_id",
        "激励视频聚合id": "aggr_jilishipin_id",
    }
    for src, dst in single_value_field_map.items():
        val = normalize_optional_parameter(fields.get(src, ""))
        if val:
            first_value = first_csv_value(val)
            if first_value:
                params[dst] = first_value

    # AppLovin SDK Key
    for sdk in fields.get("SDK列表", []):
        key = normalize_optional_parameter(sdk.get("key"))
        if sdk.get("名称") == "AppLovin" and key:
            params["manual_applovin_sdk_key"] = key
            break

    af_key = normalize_optional_parameter(fields.get("af_key", ""))
    if not af_key:
        for sdk in fields.get("SDK列表", []):
            key = normalize_optional_parameter(sdk.get("key"))
            if sdk.get("名称", "").lower() == "appsflyer" and key:
                af_key = key
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


def extract_logcat_fields(
    uid: str | None = None,
    on_line=None,
    *,
    attempts: int = LOGCAT_READ_ATTEMPTS,
    timeout_seconds: float = LOGCAT_READ_TIMEOUT_SECONDS,
    retry_delay_seconds: float = 0.5,
) -> dict:
    """Read AutoDetector fields with bounded output and ADB-aware retries."""
    adb = get_adb_path()
    if not adb:
        return {
            "ok": False,
            "error": "未找到 ADB 工具",
            "_runtime_code": "ADB_NOT_FOUND",
            "_adb_state": "missing",
            "_transient": False,
        }

    attempts = max(1, int(attempts))
    timeout_seconds = max(1.0, float(timeout_seconds))
    last_error = ""
    last_state = "device"
    for attempt in range(1, attempts + 1):
        logcat_cmd = [adb, "logcat"]
        if uid:
            logcat_cmd.extend(["--uid", str(uid)])
        # Limit the dump so a noisy game cannot make ``logcat -d`` block while
        # returning the whole enlarged buffer.  The buffer is cleared before
        # each automation run, so 20k recent lines comfortably cover detection.
        logcat_cmd.extend(["-d", "-t", str(LOGCAT_READ_MAX_LINES)])
        try:
            result = subprocess.run(
                logcat_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            if result.returncode != 0:
                last_error = (result.stderr or result.stdout or "").strip()
                last_state = get_adb_connection_state()
                if attempt < attempts and last_state != "unauthorized":
                    time.sleep(max(0.0, retry_delay_seconds))
                    continue
                code, message, transient = _logcat_connection_error(last_state)
                if last_state == "device":
                    code = "LOGCAT_READ_FAILED"
                    message = f"设备在线，但 Logcat 读取失败：{last_error or '未知错误'}"
                return {
                    "ok": False,
                    "error": message,
                    "_runtime_code": code,
                    "_adb_state": last_state,
                    "_transient": transient,
                    "_logcat_attempts": attempt,
                }
            lines = result.stdout.split("\n")
            if on_line:
                for line in lines:
                    if "ZGSDK.AutoDetector" in line:
                        on_line(line.rstrip())
            fields = parse_autodetector_fields(lines)
            fields["_logcat_attempts"] = attempt
            return fields
        except subprocess.TimeoutExpired as exc:
            last_error = str(exc)
            last_state = get_adb_connection_state()
            if attempt < attempts and last_state != "unauthorized":
                time.sleep(max(0.0, retry_delay_seconds))
                continue
            code, message, transient = _logcat_connection_error(last_state)
            return {
                "ok": False,
                "error": message,
                "_runtime_code": code,
                "_adb_state": last_state,
                "_transient": transient,
                "_logcat_attempts": attempt,
            }
        except (FileNotFoundError, OSError) as exc:
            return {
                "ok": False,
                "error": f"无法启动 Logcat：{exc}",
                "_runtime_code": "LOGCAT_READ_FAILED",
                "_adb_state": "missing" if isinstance(exc, FileNotFoundError) else "unknown",
                "_transient": False,
                "_logcat_attempts": attempt,
            }

    return {
        "ok": False,
        "error": last_error or "Logcat 读取失败",
        "_runtime_code": "LOGCAT_READ_FAILED",
        "_adb_state": last_state,
        "_transient": True,
        "_logcat_attempts": attempts,
    }
