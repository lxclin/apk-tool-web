import os
import re
import shutil
import subprocess
import threading
import urllib.request
import urllib.parse

# 常见 adb 安装位置
_COMMON_ADB_PATHS = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
    "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools/adb",
]

_adb_path: str | None = None


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


def _run_adb(args: list[str]) -> subprocess.CompletedProcess:
    adb = get_adb_path()
    if not adb:
        raise FileNotFoundError("adb")
    return subprocess.run([adb] + args, capture_output=True, text=True)


def check_device() -> bool:
    try:
        result = _run_adb(["devices"])
        lines = result.stdout.strip().split("\n")[1:]
        return any("\tdevice" in line for line in lines)
    except FileNotFoundError:
        return False


def get_device_list() -> list[str]:
    try:
        result = _run_adb(["devices"])
        lines = result.stdout.strip().split("\n")[1:]
        return [line.split("\t")[0] for line in lines if "\tdevice" in line]
    except FileNotFoundError:
        return []


def push_apk(apk_path: str) -> tuple[bool, str]:
    """安装 APK。支持单文件、目录（多 APK）、.xapk 文件"""
    try:
        # .xapk 文件 → 先解压
        if apk_path.endswith(".xapk"):
            return _install_xapk(apk_path)

        # 目录 → 安装目录下所有 APK
        if os.path.isdir(apk_path):
            apks = sorted(
                [os.path.join(apk_path, f) for f in os.listdir(apk_path)
                 if f.endswith(".apk")]
            )
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

        # 拆分 APK 相关错误 → 尝试安装同目录所有 APK
        if any(kw in combined for kw in [
            "INSTALL_FAILED", "split", "config", "Missing"
        ]):
            parent = os.path.dirname(apk_path)
            siblings = sorted([
                os.path.join(parent, f) for f in os.listdir(parent)
                if f.endswith(".apk")
            ])
            if len(siblings) > 1:
                # 确保主 APK 排第一
                siblings.remove(apk_path)
                siblings.insert(0, apk_path)
                return _install_apks(siblings)
            return False, f"安装失败: {stderr}"

        return False, f"安装失败: {stderr or stdout}"

    except FileNotFoundError:
        return False, "未找到 ADB 工具，请确认已安装 Android SDK"


def _install_apks(apk_list: list[str]) -> tuple[bool, str]:
    """安装多个 APK（拆分 APK）"""
    result = _run_adb(["install-multiple", "-r"] + apk_list)
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
                    if f.endswith(".apk"):
                        apks.append(os.path.join(root, f))

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

    # 从 URL 推断文件名，或从 Content-Disposition 获取
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path)
    if not filename or not (filename.endswith(".apk") or filename.endswith(".xapk")):
        filename = "download.apk"  # 兜底

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
            capture_output=True, text=True, timeout=60
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
        result = _run_adb(["shell", "dumpsys", "package", package_name.strip()])
        output = result.stdout
        match = re.search(r"userId=(\d+)", output)
        if match:
            uid = match.group(1)
            return True, uid
        return False, f"未找到包名 {package_name} 的 UID，请确认应用已安装"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"


def clear_app_cache(package_name: str) -> tuple[bool, str]:
    """清除应用缓存"""
    if not package_name.strip():
        return False, "请输入包名"
    try:
        result = _run_adb(["shell", "pm", "clear", package_name.strip()])
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0 and "Success" in output:
            return True, f"缓存清除成功\n{output}"
        return False, f"清除失败\n{output}"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"


def force_stop_app(package_name: str) -> tuple[bool, str]:
    """强制停止应用"""
    if not package_name.strip():
        return False, "请输入包名"
    try:
        result = _run_adb(["shell", "am", "force-stop", package_name.strip()])
        if result.returncode == 0:
            return True, f"已强制停止 {package_name}"
        return False, f"停止失败: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "未找到 ADB 工具"


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


def stop_logcat_stream(proc: subprocess.Popen) -> None:
    """终止 logcat 流进程"""
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        proc.kill()


def run_stream(cmd: list[str], on_line, on_done, cwd=None, timeout=None):
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

            if timeout:
                # 看门狗：超时后强制杀进程
                def _kill():
                    if proc and proc.poll() is None:
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


def build_zygote_build_cmd(script_dir: str) -> list[str]:
    return ["sh", os.path.join(script_dir, "zygote_build.sh")]


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
            capture_output=True, text=True, timeout=15
        )
        lines = result.stdout.split("\n")
    except Exception as e:
        return {"ok": False, "error": f"无法读取 logcat: {e}"}

    tag_lines = [line for line in lines if "ZGSDK.AutoDetector" in line]

    fields = {
        "ok": True,
        "最终判断": "",
        "初始Activity": "",
        "应用类型": "",
        "激励视频聚合id": "",
        "插屏聚合id": "",
        "归因平台": "",
        "SDK列表": [],  # [{"名称": "AppLovin", "key": "xxx"}, ...]
        "完整日志": "",
    }

    current_sdk = ""  # 当前正在处理的 SDK 名称

    for line in tag_lines:
        m = re.search(r"ZGSDK\.AutoDetector:\s+(.*)", line)
        if not m:
            continue
        content = m.group(1).strip()

        if content.startswith("最终判断:"):
            fields["最终判断"] = content.replace("最终判断:", "").strip()

        elif content.startswith("初始页面Activity:"):
            fields["初始Activity"] = content.replace("初始页面Activity:", "").strip()

        elif content.startswith("应用类型:"):
            fields["应用类型"] = content.replace("应用类型:", "").strip()
        elif content.startswith("归因平台:"):
            raw = content.replace("归因平台:", "").strip()
            fields["归因平台"] = raw.strip("[]")

        elif re.match(r"^[A-Za-z][A-Za-z0-9]+:$", content):
            # SDK 名称行，如 "AppLovin:"、"IronSource:"、"LevelPlay:"、"AdMob:"
            current_sdk = content.rstrip(":")
            found = False
            for sdk in fields["SDK列表"]:
                if sdk["名称"] == current_sdk:
                    found = True
                    break
            if not found:
                fields["SDK列表"].append({"名称": current_sdk, "key": ""})

        elif "SDK Key:" in content:
            key_val = content.split("SDK Key:", 1)[1].strip()
            # 检查是否 key 行自带 SDK 名称前缀，如 "Appsflyer SDK Key: xxx"
            prefix_m = re.match(r"^(\S+)\s+SDK\s+Key:", content)
            if prefix_m:
                sdk_name = prefix_m.group(1)
                # 确保该 SDK 在列表中
                found = False
                for sdk in fields["SDK列表"]:
                    if sdk["名称"] == sdk_name:
                        sdk["key"] = key_val
                        found = True
                        break
                if not found:
                    fields["SDK列表"].append({"名称": sdk_name, "key": key_val})
                current_sdk = sdk_name
            elif current_sdk:
                # 关联到当前 SDK（标准格式：SDK Key 跟在 SDK名称 后面）
                for sdk in fields["SDK列表"]:
                    if sdk["名称"] == current_sdk:
                        sdk["key"] = key_val
                        break
            else:
                # 兜底：既无前缀也无上下文
                fields["SDK列表"].append({"名称": "未知", "key": key_val})

        elif "激励视频聚合id:" in content:
            val = content.split("激励视频聚合id:", 1)[1].strip()
            if val != "未找到":
                fields["激励视频聚合id"] = val.strip("[]")

        elif "插屏聚合id:" in content:
            val = content.split("插屏聚合id:", 1)[1].strip()
            if val != "未找到":
                fields["插屏聚合id"] = val.strip("[]")

    fields["完整日志"] = "\n".join(tag_lines[-50:])
    return fields
