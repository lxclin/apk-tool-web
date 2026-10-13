"""Exact-package Aptoide downloads, validated before any device installation."""

import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile

from adb_pusher import (
    _run_adb, extract_package_name_from_artifact, push_apk_with_acceptance,
    resolve_google_play_package,
)

# Only the source used in our full download evaluation is enabled initially.
TRUSTED_STORES = frozenset({"appupdater"})


def _check_cancelled(cancelled):
    if cancelled and cancelled():
        raise ValueError("用户已停止 Aptoide 下载")


def _device_spec():
    serial_result = _run_adb(["get-serialno"], timeout=5)
    serial = serial_result.stdout.strip()
    if serial_result.returncode or not serial or serial == "unknown":
        raise ValueError("需要连接唯一的 Android 设备")
    props = _run_adb(["-s", serial, "shell", "getprop"], timeout=8)
    if props.returncode:
        raise ValueError("无法读取设备属性")
    values = dict(re.findall(r"\[([^\]]+)\]: \[([^\]]*)\]", props.stdout))
    abis = (values.get("ro.product.cpu.abilist") or values.get("ro.product.cpu.abi", "")).split(",")
    sdk = int(values.get("ro.build.version.sdk", "0"))
    density_result = _run_adb(["-s", serial, "shell", "wm", "density"], timeout=5)
    densities = re.findall(r"(?:Physical|Override) density:\s*(\d+)", density_result.stdout)
    density = int(densities[-1]) if densities else int(values.get("ro.sf.lcd_density", "0"))
    if sdk <= 0 or density <= 0 or not abis[0]:
        raise ValueError("设备 SDK、CPU 或屏幕密度读取失败")
    return {"serial": serial, "sdk": sdk, "abis": abis, "density": density}


def _metadata(package, device):
    spec = (f"maxSdk={device['sdk']}&myScreen=normal&maxGles=3.2&"
            f"myCPU={device['abis'][0]}&myDensity={device['density']}")
    query = base64.urlsafe_b64encode(spec.encode()).decode().rstrip("=")
    url = ("https://ws75.aptoide.com/api/7/app/get/package_name="
           + urllib.parse.quote(package, safe="") + "/aab=1?"
           + urllib.parse.urlencode({"q": query}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read(4 * 1024 * 1024))
    data = ((body.get("nodes") or {}).get("meta") or {}).get("data") or {}
    if data.get("package") != package:
        raise ValueError("Aptoide 未找到精确包名")
    store = (data.get("store") or {}).get("name", "")
    file = data.get("file") or {}
    if store not in TRUSTED_STORES or (file.get("malware") or {}).get("rank") != "TRUSTED":
        raise ValueError(f"Aptoide 来源未通过筛选：{store or '未知商店'}")
    hardware = file.get("hardware") or {}
    if int(hardware.get("sdk") or 0) > device["sdk"]:
        raise ValueError("Aptoide 包体需要更高的 Android 版本")
    cpus = hardware.get("cpus") or []
    if cpus and not set(cpus).intersection(device["abis"]):
        raise ValueError("Aptoide 包体 CPU 与设备不兼容")
    aab = data.get("aab") or {}
    splits = aab.get("splits") or []
    names = [s.get("name", "") for s in splits]
    if len(names) != len(set(names)) or "base" in names or any(not n for n in names):
        raise ValueError("Aptoide 拆分包列表无效")
    if not set(aab.get("required_splits") or []).issubset(names):
        raise ValueError("Aptoide 缺少必需拆分包")
    for split in splits:
        # AAB architecture splits encode the ABI in their name.
        if split.get("type") == "config" or split.get("name", "").startswith("config."):
            abi = split["name"].removeprefix("config.").replace("_", "-")
            if abi in {"arm64-v8a", "armeabi-v7a", "x86", "x86-64"}:
                supported = {a.replace("_", "-") for a in device["abis"]}
                if abi not in supported:
                    raise ValueError("Aptoide 返回了不兼容的 CPU 拆分包")
    return data, [{**file, "name": "base"}] + splits


def _signature_tool():
    roots = [os.environ.get("ANDROID_SDK_ROOT", ""), os.environ.get("ANDROID_HOME", ""),
             str(Path.home() / "Library/Android/sdk"), str(Path.home() / "Android/Sdk")]
    signer = shutil.which("apksigner")
    if not signer:
        candidates = [p for root in roots if root for p in glob.glob(os.path.join(root, "build-tools", "*", "apksigner"))]
        signer = sorted(candidates, reverse=True)[0] if candidates else None
    if not signer:
        raise ValueError("未找到 Android SDK apksigner，无法核验签名")
    env = dict(os.environ)
    if not env.get("JAVA_HOME"):
        bundled = "/Applications/Android Studio.app/Contents/jbr/Contents/Home"
        if os.path.isfile(os.path.join(bundled, "bin", "java")):
            env["JAVA_HOME"] = bundled
    return signer, env


def _verify_signature(path, device, expected_sha1, tool):
    signer, env = tool
    signed = subprocess.run(
        [signer, "verify", "--print-certs", "--min-sdk-version", str(device["sdk"]),
         "--max-sdk-version", str(device["sdk"]), str(path)],
        env=env, capture_output=True, text=True, timeout=45,
    )
    if signed.returncode:
        raise ValueError("APK 签名验证失败：" + signed.stderr[-400:])
    def digests(label):
        return frozenset(v.lower() for v in re.findall(
            r"^(?:Signer (?:#\d+|\([^\n]+\))|V\d+(?:\.\d+)? Signer(?: #\d+)?:?(?: \([^\n]+\))?) certificate " + label + r" digest: ([0-9a-fA-F]+)",
            signed.stdout, re.MULTILINE))
    sha256, sha1 = digests("SHA-256"), digests("SHA-1")
    if not sha256 or expected_sha1 not in sha1:
        raise ValueError("APK 签名与 Aptoide 元数据不一致")
    return sha256


def _download(item, path, package, device, cancelled):
    url = item.get("path", "")
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Aptoide 未提供 HTTPS 下载地址")
    size = int(item.get("filesize") or 0)
    md5 = item.get("md5sum", "").lower()
    if not 0 < size <= 2 * 1024**3 or not re.fullmatch(r"[0-9a-f]{32}", md5):
        raise ValueError("Aptoide 文件校验信息不完整")
    digest = hashlib.md5()
    count = 0
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response, open(path, "wb") as artifact:
        while True:
            _check_cancelled(cancelled)
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > size:
                raise ValueError("Aptoide 文件长度超过元数据")
            digest.update(chunk)
            artifact.write(chunk)
    if count != size or digest.hexdigest() != md5:
        raise ValueError("Aptoide 文件大小或 MD5 校验失败")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("APK 文件完整性校验失败")
        native_abis = {n.split('/')[1] for n in archive.namelist() if n.startswith('lib/') and n.endswith('.so')}
        if native_abis and not native_abis.intersection(device["abis"]):
            raise ValueError("APK 内原生库与设备 CPU 不兼容")
    if extract_package_name_from_artifact(str(path)) != package:
        raise ValueError("APK 真实包名核验失败；未安装")


def download_and_install_aptoide(package_name, on_progress=None, cancelled=None):
    failure = {"ok": False, "code": "APTOIDE_CHECK_FAILED"}
    try:
        package = resolve_google_play_package(package_name)
        if not package:
            raise ValueError("无效包名")
        _check_cancelled(cancelled)
        device = _device_spec()
        tool = _signature_tool()
        if on_progress:
            on_progress(f"Aptoide 查询 {package}（{device['abis'][0]}）")
        data, items = _metadata(package, device)
        expected = re.sub(r"[^0-9a-f]", "", ((data["file"].get("signature") or {}).get("sha1") or "").lower())
        if len(expected) != 40:
            raise ValueError("Aptoide 未提供有效证书指纹")
        with tempfile.TemporaryDirectory(prefix="aptoide_") as directory:
            paths, certificates = [], []
            for index, item in enumerate(items):
                _check_cancelled(cancelled)
                if on_progress:
                    on_progress(f"Aptoide 下载并校验 {index + 1}/{len(items)}：{item['name']}")
                path = Path(directory) / f"{index:03d}.apk"
                _download(item, path, package, device, cancelled)
                certificates.append(_verify_signature(path, device, expected, tool))
                paths.append(path)
            if any(c != certificates[0] for c in certificates):
                raise ValueError("主包与拆分包签名不一致；未安装")
            _check_cancelled(cancelled)
            if _device_spec()["serial"] != device["serial"]:
                raise ValueError("下载期间设备已切换；未安装")
            artifact = paths[0]
            if len(paths) > 1:
                artifact = Path(directory) / "package.xapk"
                with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_STORED) as archive:
                    for index, path in enumerate(paths):
                        archive.write(path, "base.apk" if index == 0 else f"split_{index}.apk")
            _check_cancelled(cancelled)
            ok, message = push_apk_with_acceptance(str(artifact), expected_package=package, on_progress=on_progress)
            return {"ok": ok, "code": "APTOIDE_INSTALLED" if ok else "APTOIDE_CHECK_FAILED",
                    "message": f"Aptoide：{message}", "store": data["store"]["name"],
                    "version": data["file"].get("vername"), "file_count": len(paths)}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        return {**failure, "message": f"Aptoide：{exc}"}
