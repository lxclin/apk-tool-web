"""Shared third-party download policies and APKPure artifact installation."""

import os
import tempfile
import urllib.error
import urllib.request
import zipfile

from aptoide_recovery import download_and_install_aptoide

from adb_pusher import (
    download_and_install_apkcombo,
    extract_package_name_from_artifact,
    push_apk_with_acceptance,
    resolve_google_play_package,
)


THIRD_PARTY_RETRY_STATUSES = frozenset({
    "APKCombo待确认", "APKCombo有包", "APKPure待确认", "Aptoide待确认", "第三方下载待确认",
})
DOWNLOAD_MODES = {
    "fallback": "自动兜底：Aptoide → APKPure → APKCombo",
    "aptoide": "仅 Aptoide",
    "apkpure": "仅 APKPure",
    "apkcombo": "仅 APKCombo",
}


def third_party_download_result(package_name):
    return {
        "code": "THIRD_PARTY_DOWNLOAD",
        "title": "第三方包体下载待重试",
        "detail": "按所选下载策略获取精确包名的包体；安装验收后继续适配。",
        "package_name": package_name,
        "source": "第三方下载",
    }


def apkcombo_recovery_candidate(task, status):
    return (
        not getattr(task, "completed", False)
        and not getattr(task, "workflow_terminal", False)
        and status in THIRD_PARTY_RETRY_STATUSES
        and bool(resolve_google_play_package(getattr(task, "package_name", "")))
    )


def download_and_install_third_party(
    package_name, *, mode="fallback", on_progress=None, cancelled=None,
    force_reinstall=False, apkcombo_installer=None, apkpure_installer=None,
    aptoide_installer=None,
):
    """One policy for all desktop third-party installation entry points."""
    if mode not in DOWNLOAD_MODES:
        mode = "fallback"
    combo = apkcombo_installer or download_and_install_apkcombo
    pure = apkpure_installer or download_and_install_apkpure
    aptoide = aptoide_installer or download_and_install_aptoide
    providers = ("aptoide", "apkpure", "apkcombo") if mode == "fallback" else (mode,)
    attempts = []
    for provider in providers:
        if cancelled and cancelled():
            return {"ok": False, "code": "DOWNLOAD_CANCELLED", "message": "用户已停止下载", "download_attempts": attempts}
        if on_progress:
            on_progress(f"下载策略：{DOWNLOAD_MODES[mode]}；正在使用 {provider}")
        try:
            if provider == "apkcombo":
                kwargs = {"on_progress": on_progress, "force_reinstall": force_reinstall}
                if mode == "fallback":
                    # Automated fallback must not wait for a manual browser download.
                    kwargs["allow_browser_fallback"] = False
                result = combo(package_name, **kwargs)
            else:
                installer = aptoide if provider == "aptoide" else pure
                result = installer(package_name, on_progress=on_progress, cancelled=cancelled)
        except Exception as exc:
            result = {"ok": False, "message": f"{provider} 下载异常：{exc}"}
        attempts.append({"provider": provider, "ok": bool(result.get("ok")), "message": result.get("message", "")})
        if result.get("ok"):
            return {**result, "provider": provider, "download_attempts": attempts}
        if cancelled and cancelled():
            return {"ok": False, "code": "DOWNLOAD_CANCELLED", "message": "用户已停止下载", "download_attempts": attempts}
        if mode == "fallback" and provider != providers[-1] and on_progress:
            next_provider = providers[providers.index(provider) + 1]
            on_progress(f"{provider} 未成功：{result.get('message', '')}；切换到 {next_provider}...")
    return {
        "ok": False, "code": "THIRD_PARTY_CHECK_FAILED",
        "message": "；".join(f"{attempt['provider']}：{attempt['message']}" for attempt in attempts),
        "download_mode": mode, "download_attempts": attempts,
    }


def download_and_install_apkpure(package_name, on_progress=None, cancelled=None):
    """Try exact-package APKPure artifacts; validate identity before installation."""
    package_name = resolve_google_play_package(package_name)
    failure = {"ok": False, "code": "APKPURE_CHECK_FAILED"}
    if not package_name:
        return {**failure, "message": "无效包名"}
    errors = []
    for kind in ("XAPK", "APK"):
        if cancelled and cancelled():
            return {**failure, "message": "用户已停止 APKPure 重试"}
        url = f"https://d.apkpure.net/b/{kind}/{package_name}?version=latest"
        try:
            with tempfile.TemporaryDirectory(prefix="apkpure_") as directory:
                path = os.path.join(directory, "package.apk")
                if on_progress:
                    on_progress(f"APKPure 正在下载 {package_name}（{kind}）...")
                request = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://apkpure.com/",
                })
                with urllib.request.urlopen(request, timeout=30) as response:
                    with open(path, "wb") as artifact:
                        while True:
                            if cancelled and cancelled():
                                return {**failure, "message": "用户已停止 APKPure 重试"}
                            chunk = response.read(128 * 1024)
                            if not chunk:
                                break
                            if artifact.tell() == 0 and not chunk.startswith(b"PK"):
                                raise ValueError("返回内容不是 APK/XAPK，可能遇到人机验证")
                            artifact.write(chunk)
                with zipfile.ZipFile(path) as archive:
                    if any(name.lower().endswith(".apk") for name in archive.namelist()):
                        xapk_path = os.path.join(directory, "package.xapk")
                        os.rename(path, xapk_path)
                        path = xapk_path
                actual_package = extract_package_name_from_artifact(path)
                if actual_package != package_name:
                    return {**failure, "message": (
                        f"下载包名核验失败：期望 {package_name}，"
                        f"实际 {actual_package or '无法读取'}；未安装"
                    )}
                if cancelled and cancelled():
                    return {**failure, "message": "用户已停止 APKPure 重试"}
                ok, message = push_apk_with_acceptance(
                    path, expected_package=package_name, on_progress=on_progress
                )
                return {
                    "ok": ok,
                    "code": "APKPURE_INSTALLED" if ok else "APKPURE_CHECK_FAILED",
                    "message": f"APKPure：{message}",
                }
        except urllib.error.HTTPError as exc:
            errors.append(f"{kind}: HTTP {exc.code}")
            if exc.code not in {404, 410}:
                break
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            errors.append(f"{kind}: {exc}")
            break
    return {**failure, "message": "APKPure 下载未成功，保留待确认；" + "；".join(errors)}
