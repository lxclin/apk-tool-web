"""ADB WebSocket 代理 — 零依赖，监听 localhost:9527
浏览器页面通过 ws://localhost:9527 连接此代理来执行本地 ADB 命令。
"""
import asyncio
import json
import os
import re
import queue
import shutil
import signal
import subprocess
import sys
import threading
import urllib.request
import urllib.parse
import urllib.error
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import websockets
from websockets.asyncio.server import serve as ws_serve

from adb_pusher import (
    PackageRuntimeMonitor,
    build_apkcombo_search_url,
    download_artifact_filename,
    get_app_uid,
    parse_autodetector_fields,
    set_adb_path as set_core_adb_path,
)
from ad_replay import (
    DEFAULT_REPLAY_TIMEOUT_SECONDS,
    ReplayExpectation,
    build_replay_failure_comment,
    run_ad_replay_check,
    validate_replay_timeout,
)
from automation_adaptation import (
    add_automation_comment_once,
    attribution_gate_issue,
    build_aggregation_assessment,
    detect_aggregation_with_one_retry,
    has_aggregation_type,
    submit_backend_via_api,
    update_asana_aggregation_notes,
)
from daily_summary import generate_daily_asana_summary
from auto_asana.main import build_asana_client
from web_precheck import load_today_precheck_tasks, run_web_precheck


# ── ADB 路径 ─────────────────────────────────────────────────────

# 常见 ADB 路径（兜底扫描用）
_COMMON_ADB_PATHS = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
]

_adb_path: str = "adb"  # 最终确认的 adb 路径
_startup_msgs: list[str] = []  # 启动诊断信息


def _init_adb() -> str:
    """初始化 ADB：查找并验证可用的 adb 二进制。
    优先级：1) 打包内嵌 adb  2) 系统 PATH 中的 adb  3) 常见路径扫描
    """
    global _adb_path, _startup_msgs

    candidates = []

    # 1. 打包内嵌的 adb
    if getattr(sys, 'frozen', False):
        bundled = os.path.join(sys._MEIPASS, "adb", "adb")
        candidates.append(("打包内嵌", bundled))

    # 2. 系统 PATH 中的 adb
    system_adb = shutil.which("adb")
    if system_adb:
        candidates.append(("系统 PATH", system_adb))

    # 3. 常见路径扫描
    for p in _COMMON_ADB_PATHS:
        if os.path.isfile(p) and p not in [c[1] for c in candidates]:
            candidates.append(("常见路径", p))

    for label, path in candidates:
        _startup_msgs.append(f"  检查 [{label}]: {path}")
        if not os.path.isfile(path):
            _startup_msgs.append(f"    → 文件不存在")
            continue

        # 修复执行权限（datas 模式可能丢失 +x）
        try:
            st = os.stat(path)
            if not (st.st_mode & 0o111):
                os.chmod(path, st.st_mode | 0o111)
                _startup_msgs.append(f"    → 已修复执行权限")
        except Exception as e:
            _startup_msgs.append(f"    → 权限修复失败: {e}")
            continue

        # 验证可以执行
        try:
            result = subprocess.run(
                [path, "version"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                _adb_path = path
                version_line = result.stdout.strip().split("\n")[0]
                _startup_msgs.append(f"    → ✓ 可用 ({version_line})")
                return path
            else:
                _startup_msgs.append(f"    → 执行失败 (exit={result.returncode})")
        except FileNotFoundError:
            _startup_msgs.append(f"    → 无法执行（架构不兼容？）")
        except subprocess.TimeoutExpired:
            _startup_msgs.append(f"    → 执行超时")
        except OSError as e:
            _startup_msgs.append(f"    → OS 错误: {e}")

    # 全都不行
    _startup_msgs.append(f"  ❌ 未找到可用的 adb！")
    _startup_msgs.append(f"  请确保已安装 Android Platform Tools:")
    _startup_msgs.append(f"    brew install android-platform-tools")
    _adb_path = "adb"
    return "adb"


_adb_path = _init_adb()
set_core_adb_path(_adb_path)


def _find_adb() -> str:
    """返回已确认可用的 ADB 路径"""
    return _adb_path


def _extract_uid_from_dumpsys_line(line: str) -> str | None:
    match = re.search(r"\b(?:userId|appId)=(\d+)\b", line)
    if match:
        return match.group(1)
    return None


def _extract_af_key_from_content(content: str) -> str:
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


# ── 命令构建 ─────────────────────────────────────────────────────

def _cmd_display(cmd: list[str]) -> str:
    s = " ".join(cmd)
    return s.replace(_find_adb(), "adb")


def _build_push_config(cfg_path: str) -> list[str]:
    return [_find_adb(), "push", cfg_path, "/data/local/tmp/zygotehole/"]


def _build_zygote_build(work_dir: str) -> list[str]:
    return ["sh", os.path.join(work_dir, "zygote_build.sh")]


def _build_get_uid(pkg: str) -> list[str]:
    return [_find_adb(), "shell", "dumpsys", "package", pkg]


def _build_clear_cache(pkg: str) -> list[str]:
    return [_find_adb(), "shell", "pm", "clear", pkg]


def _build_force_stop(pkg: str) -> list[str]:
    return [_find_adb(), "shell", "am", "force-stop", pkg]


def _build_open_app(pkg: str) -> list[str]:
    return [_find_adb(), "shell", "monkey", "-p", pkg,
            "-c", "android.intent.category.LAUNCHER", "1"]


# ── 流式执行 ─────────────────────────────────────────────────────

def _run_stream(cmd: list[str], on_line, on_done, cwd=None, timeout=None):
    def _run():
        returncode = -1
        proc = None
        timer = None
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
                def _kill():
                    if proc and proc.poll() is None:
                        proc.kill()
                timer = threading.Timer(timeout, _kill)
                timer.start()

            for line in proc.stdout:
                on_line(line.rstrip())

            if timer:
                timer.cancel()

            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            returncode = proc.returncode
        except FileNotFoundError:
            on_line(f"[错误] 命令未找到: {cmd[0]}")
        except Exception as e:
            on_line(f"[错误] {e}")
        finally:
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
        on_done(returncode)

    threading.Thread(target=_run, daemon=True).start()


# ── 设备检测 ─────────────────────────────────────────────────────

def _check_device() -> bool:
    try:
        result = subprocess.run(
            [_find_adb(), "devices"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")[1:]
        return any("\tdevice" in line for line in lines)
    except Exception:
        return False


# ── 日志字段提取 ─────────────────────────────────────────────────

def _extract_fields() -> dict:
    """从 adb logcat -d 中提取 ZGSDK.AutoDetector 的结构化字段"""
    # 先扩大缓冲区，防止旧日志被冲掉
    try:
        subprocess.run(
            [_find_adb(), "logcat", "-G", "16M"],
            capture_output=True, timeout=5
        )
    except Exception:
        pass

    try:
        # -b all 读取所有缓冲区（main + system + crash + events）
        result = subprocess.run(
            [_find_adb(), "logcat", "-b", "all", "-d"],
            capture_output=True, text=True, timeout=15
        )
        lines = result.stdout.split("\n")
    except Exception:
        return {"ok": False, "error": "无法读取 logcat"}

    return parse_autodetector_fields(lines)


# ── 配置路径 ─────────────────────────────────────────────────────

CONFIG_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518/config.json"
)
WORK_DIR_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518"
)


def _read_config(path: str) -> dict:
    """读取 config.json，返回 {ok, packageName, appId, taskUUID, error}"""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        import json as _j
        with open(path, 'r') as f:
            data = _j.load(f)
        item = data.get("data", [{}])[0] if data.get("data") else {}
        return {
            "ok": True,
            "packageName": item.get("packageName", ""),
            "appId": item.get("appId", ""),
            "taskUUID": item.get("taskUUID", "mediation_test_snow"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _write_config(path: str, pkg: str, appid: str, task_uuid: str = "") -> dict:
    """写入 config.json"""
    if not pkg:
        return {"ok": False, "error": "包名不能为空"}
    if not appid:
        return {"ok": False, "error": "AppId 不能为空"}
    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        import json as _j
        with open(path, 'r') as f:
            cfg = _j.load(f)
        if not cfg.get("data"):
            cfg["data"] = [{}]
        item = cfg["data"][0]
        if pkg:
            item["packageName"] = pkg
        if appid:
            item["appId"] = appid
        if task_uuid:
            item["taskUUID"] = task_uuid
        with open(path, 'w') as f:
            _j.dump(cfg, f, indent=2, ensure_ascii=False)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Logcat 管理 ──────────────────────────────────────────────────

_logcat_proc: subprocess.Popen | None = None

# ── HTTP 回填服务 ──────────────────────────────────────────────────
_connected_ws: set = set()  # 所有已连接的 WebSocket 客户端
_latest_fill_data: dict = {}  # 最近一次回填数据


def _broadcast_fill(data: dict):
    """广播回填数据到所有已连接的 WebSocket 客户端"""
    dead = set()
    for ws in _connected_ws:
        try:
            # asyncio 跨线程安全：用 call_soon_threadsafe 推送
            ws.loop.call_soon_threadsafe(
                lambda w=ws: asyncio.ensure_future(
                    w.send(json.dumps({"type": "fill_data", "data": data}))
                )
            )
        except Exception:
            dead.add(ws)
    _connected_ws.difference_update(dead)


async def _http_fill_handler(reader, writer):
    """简易 HTTP 服务器：接收 POST /fill 回填数据"""
    global _latest_fill_data
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        writer.close()
        return

    head = raw.decode("utf-8", errors="replace")
    lines = head.split("\r\n")
    if not lines:
        writer.close()
        return

    # 解析请求行
    method, path, *_ = lines[0].split(" ")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    origin = headers.get("origin", "")

    def _cors_response(status, body_json):
        body = json.dumps(body_json, ensure_ascii=False)
        resp = (
            f"HTTP/1.1 {status}\r\n"
            f"Access-Control-Allow-Origin: {origin or '*'}\r\n"
            f"Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n"
            f"Access-Control-Allow-Headers: Content-Type\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(resp.encode("utf-8"))
        writer.close()

    if method == "OPTIONS":
        _cors_response("200 OK", {"ok": True})
        return

    if method == "GET" and path == "/fill/latest":
        _cors_response("200 OK", {"ok": True, "data": _latest_fill_data})
        return

    if method != "POST" or path != "/fill":
        _cors_response("404 Not Found", {"ok": False, "error": "not found"})
        return

    # 读取 body
    content_length = int(headers.get("content-length", 0))
    body_raw = b""
    if content_length > 0:
        # 可能 body 已经在 raw 后面了，也可能还需要读
        header_end = raw.find(b"\r\n\r\n") + 4
        remaining = raw[header_end:]
        body_raw += remaining
        while len(body_raw) < content_length:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=3)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break
            if not chunk:
                break
            body_raw += chunk

    try:
        data = json.loads(body_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _cors_response("400 Bad Request", {"ok": False, "error": "invalid json"})
        return

    # 存储并广播
    _latest_fill_data = data
    _broadcast_fill(data)

    _cors_response("200 OK", {"ok": True})


async def _http_server_main():
    """启动 HTTP 回填服务器 (端口 9528)"""
    server = await asyncio.start_server(_http_fill_handler, "localhost", 9528)
    return server


def _start_logcat(pattern: str, uid: str | None = None):
    global _logcat_proc
    subprocess.run([_find_adb(), "logcat", "-c"], capture_output=True, timeout=5)
    cmd = [_find_adb(), "logcat"]
    if uid:
        cmd.extend(["--uid", uid])
    _logcat_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    _logcat_proc._filter_pattern = pattern
    return _logcat_proc


def _stop_logcat():
    global _logcat_proc
    if _logcat_proc:
        try:
            _logcat_proc.terminate()
            _logcat_proc.wait(timeout=3)
        except Exception:
            try:
                _logcat_proc.kill()
            except Exception:
                pass
        _logcat_proc = None


# ── WebSocket 处理 ───────────────────────────────────────────────

async def handle_connection(ws):
    """处理单个 WebSocket 连接"""
    global _logcat_proc
    _connected_ws.add(ws)
    logcat_task: asyncio.Task | None = None
    automation_stop_event = threading.Event()

    # 推送初始设备状态 + 最新回填数据（如果有）
    try:
        await ws.send(json.dumps({
            "type": "device_status",
            "connected": _check_device(),
            "adb_path": _find_adb(),
        }))
        if _latest_fill_data:
            await ws.send(json.dumps({
                "type": "fill_data",
                "data": _latest_fill_data,
            }))
    except Exception:
        _connected_ws.discard(ws)
        return

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "text": "无效 JSON"}))
                continue

            msg_type = msg.get("type", "")
            params = msg.get("params", {})

            if msg_type == "cmd":
                action = msg.get("action", "")
                pkg = params.get("packageName", "")
                cfg_path = params.get("configPath", "")
                work_dir = params.get("workDir", "")
                url = params.get("url", "")
                filepath = params.get("filepath", "")

                builders = {
                    "push_config":     lambda: ("push_config", _build_push_config(cfg_path), None, None),
                    "zygote_build":    lambda: ("zygote_build", _build_zygote_build(work_dir), work_dir, None),
                    "get_uid":         lambda: ("get_uid", _build_get_uid(pkg), None, None),
                    "clear_cache":     lambda: ("clear_cache", _build_clear_cache(pkg), None, None),
                    "force_stop":      lambda: ("force_stop", _build_force_stop(pkg), None, None),
                    "open_app":        lambda: ("open_app", _build_open_app(pkg), None, 15),
                    "clear_play_store":lambda: ("clear_play_store", _build_clear_cache("com.android.vending"), None, None),
                }

                if action in builders:
                    action_label, cmd, cwd, to = builders[action]()
                    await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))

                    if action == "get_uid":
                        # 特殊处理：需要提取 UID
                        asyncio.create_task(_run_get_uid_ws(ws, cmd))
                    else:
                        asyncio.create_task(_run_cmd_ws(ws, cmd, cwd=cwd, timeout=to))

                elif action == "open_url":
                    if not url:
                        await ws.send(json.dumps({"type": "error", "text": "请输入 URL"}))
                        continue
                    if url.startswith("http"):
                        cmd = [_find_adb(), "shell", "am", "start", "-a",
                               "android.intent.action.VIEW", "-d", url]
                    else:
                        cmd = [_find_adb(), "install", "-r", url]
                    await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))
                    asyncio.create_task(_run_cmd_ws(ws, cmd, timeout=15))

                elif action == "open_apkcombo":
                    target_url = build_apkcombo_search_url(url or pkg)
                    if not target_url:
                        await ws.send(json.dumps({"type": "error", "text": "无法识别 Google Play 链接或包名"}))
                        continue
                    cmd = [_find_adb(), "shell", "am", "start", "-a",
                           "android.intent.action.VIEW", "-d",
                           target_url]
                    await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))
                    asyncio.create_task(_run_cmd_ws(ws, cmd, timeout=15))

                elif action == "apkpure_search":
                    if not pkg:
                        await ws.send(json.dumps({"type": "error", "text": "请输入包名"}))
                        continue
                    script = (
                        f"monkey -p com.apkpure.aegon "
                        f"-c android.intent.category.LAUNCHER 1; "
                        f"sleep 2; input keyevent 84; sleep 0.5; "
                        f"input text {pkg}; sleep 0.3; input keyevent 66"
                    )
                    cmd = [_find_adb(), "shell", script]
                    await ws.send(json.dumps({"type": "cmd_display", "text": f"adb shell (APKPure 搜索 {pkg})"}))
                    asyncio.create_task(_run_cmd_ws(ws, cmd, timeout=15))

                elif action == "install_file":
                    if not filepath or not os.path.isfile(filepath):
                        await ws.send(json.dumps({"type": "error", "text": "文件不存在"}))
                        continue
                    # xapk 文件 → 解压后用 install-multiple 安装
                    if filepath.lower().endswith(".xapk"):
                        asyncio.create_task(_install_xapk_ws(ws, filepath))
                    else:
                        cmd = [_find_adb(), "install", "-r", filepath]
                        await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))
                        asyncio.create_task(_run_cmd_ws(ws, cmd))

                elif action == "download_install":
                    url = msg.get("url", "").strip()
                    if not url:
                        await ws.send(json.dumps({"type": "error", "text": "请输入下载链接"}))
                        continue
                    if not _check_device():
                        await ws.send(json.dumps({"type": "error", "text": "没有已连接的设备"}))
                        continue
                    asyncio.create_task(_download_install_ws(ws, url))

                else:
                    await ws.send(json.dumps({"type": "error", "text": f"未知操作: {action}"}))

            elif msg_type == "logcat_start":
                pattern = msg.get("pattern", "")
                uid = msg.get("uid", "").strip() or None
                if logcat_task and not logcat_task.done():
                    logcat_task.cancel()
                _stop_logcat()
                if not _check_device():
                    await ws.send(json.dumps({"type": "error", "text": "没有已连接的设备"}))
                    continue
                proc = _start_logcat(pattern, uid)
                uid_display = f" --uid={uid}" if uid else ""
                await ws.send(json.dumps({"type": "cmd_display", "text": f"adb logcat{uid_display} | grep {pattern}"}))
                logcat_task = asyncio.create_task(_logcat_reader(ws, pattern, proc))

            elif msg_type == "logcat_stop":
                _stop_logcat()
                if logcat_task and not logcat_task.done():
                    logcat_task.cancel()
                await ws.send(json.dumps({"type": "logcat_stopped"}))

            elif msg_type == "device_check":
                await ws.send(json.dumps({
                    "type": "device_status",
                    "connected": _check_device(),
                    "adb_path": _find_adb(),
                }))

            elif msg_type == "config_read":
                path = msg.get("path", "") or CONFIG_DEFAULT
                result = _read_config(path)
                await ws.send(json.dumps({"type": "config_data", **result}))

            elif msg_type == "config_write":
                path = msg.get("path", "") or CONFIG_DEFAULT
                pkg = msg.get("packageName", "")
                appid = msg.get("appId", "")
                task_uuid = msg.get("taskUUID", "")
                result = _write_config(path, pkg, appid, task_uuid)
                if result.get("ok"):
                    await ws.send(json.dumps({"type": "config_saved"}))
                else:
                    await ws.send(json.dumps({"type": "error", "text": result.get("error", "写入失败")}))

            elif msg_type == "extract_fields":
                if not _check_device():
                    await ws.send(json.dumps({"type": "error", "text": "没有已连接的设备"}))
                    continue
                await ws.send(json.dumps({"type": "cmd_display", "text": "adb logcat -d | grep ZGSDK.AutoDetector (字段提取)"}))
                extracted = _extract_fields()
                await ws.send(json.dumps({"type": "extracted_fields", **extracted}))

            elif msg_type == "automation_extract_fields":
                request_id = str(msg.get("request_id") or "")
                package_name = str(msg.get("package_name") or "").strip()
                task_gid = str(msg.get("task_gid") or "").strip()
                asana_pat = str(msg.get("asana_pat") or "").strip()
                loop = asyncio.get_running_loop()
                automation_stop_event.clear()

                def _detection_progress(text: str):
                    asyncio.run_coroutine_threadsafe(
                        ws.send(json.dumps({
                            "type": "automation_progress",
                            "request_id": request_id,
                            "text": text,
                        }, ensure_ascii=False)),
                        loop,
                    )

                runtime_monitor = PackageRuntimeMonitor(package_name)
                result = await asyncio.to_thread(
                    detect_aggregation_with_one_retry,
                    package_name,
                    _extract_fields,
                    stop_event=automation_stop_event,
                    on_progress=_detection_progress,
                    runtime_check=runtime_monitor.poll,
                    runtime_reset=runtime_monitor.reset,
                )
                if isinstance(result.get("fields"), dict):
                    result["assessment"] = build_aggregation_assessment(
                        result["fields"]
                    )
                if (
                    result.get("code") in {
                        "AGGREGATION_TYPE_EMPTY",
                        "AF_KEY_EMPTY",
                        "AD_IDS_EMPTY",
                        "UNSUPPORTED_ATTRIBUTION",
                        "APP_CRASHED",
                        "APP_EXITED_DURING_AUTOMATION",
                        "AGGREGATION_RESULT_INCOMPLETE",
                    }
                    and task_gid
                    and asana_pat
                ):
                    try:
                        client = build_asana_client(asana_pat)
                        await asyncio.to_thread(
                            add_automation_comment_once,
                            client,
                            task_gid,
                            result.get("code"),
                            f"{result.get('message')}\n包名：{package_name}"
                            + (
                                "\n关键崩溃日志：\n"
                                + str((result.get("runtime") or {}).get("summary"))
                                if (result.get("runtime") or {}).get("summary")
                                else ""
                            ),
                        )
                    except Exception as exc:
                        result["comment_error"] = str(exc)
                await ws.send(json.dumps({
                    "type": "automation_result",
                    "request_id": request_id,
                    **result,
                }, ensure_ascii=False))

            elif msg_type == "automation_asana_fill":
                request_id = str(msg.get("request_id") or "")
                try:
                    client = build_asana_client(str(msg.get("asana_pat") or ""))
                    merged = await asyncio.to_thread(
                        update_asana_aggregation_notes,
                        client,
                        str(msg.get("task_gid") or ""),
                        str(msg.get("existing_notes") or ""),
                        dict(msg.get("fields") or {}),
                        allow_unsupported_attribution=bool(
                            msg.get("allow_unsupported_attribution")
                        ),
                    )
                    await ws.send(json.dumps({
                        "type": "automation_result",
                        "request_id": request_id,
                        "ok": True,
                        "code": "ASANA_FILLED",
                        "message": "聚合参数已回填至 Asana 描述",
                        "notes": merged,
                    }, ensure_ascii=False))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "type": "automation_result",
                        "request_id": request_id,
                        "ok": False,
                        "code": "ASANA_FILL_FAILED",
                        "error": str(exc),
                    }, ensure_ascii=False))

            elif msg_type == "automation_backend_submit":
                request_id = str(msg.get("request_id") or "")
                package_name = str(msg.get("package_name") or "")
                task_gid = str(msg.get("task_gid") or "")
                asana_pat = str(msg.get("asana_pat") or "")
                result = await asyncio.to_thread(
                    submit_backend_via_api,
                    dict(msg.get("fields") or {}),
                    package_name,
                    api_url=str(msg.get("api_url") or ""),
                    x_token=str(msg.get("x_token") or ""),
                    token=str(msg.get("token") or ""),
                    user_name=str(msg.get("user_name") or "rain"),
                    stop_event=automation_stop_event,
                    allow_unsupported_attribution=bool(
                        msg.get("allow_unsupported_attribution")
                    ),
                )
                if (
                    result.get("ok")
                    and msg.get("allow_unsupported_attribution")
                    and task_gid
                    and asana_pat
                ):
                    issue = attribution_gate_issue(dict(msg.get("fields") or {}))
                    if issue:
                        try:
                            client = build_asana_client(asana_pat)
                            await asyncio.to_thread(
                                add_automation_comment_once,
                                client,
                                task_gid,
                                issue[0],
                                f"{issue[1]}\n包名：{package_name}",
                            )
                        except Exception as exc:
                            result["comment_error"] = str(exc)
                if not result.get("ok") and task_gid and asana_pat:
                    try:
                        client = build_asana_client(asana_pat)
                        await asyncio.to_thread(
                            add_automation_comment_once,
                            client,
                            task_gid,
                            result.get("code", "BACKEND_SUBMIT_FAILED"),
                            result.get("message", "后台自动提交失败"),
                        )
                    except Exception as exc:
                        result["comment_error"] = str(exc)
                await ws.send(json.dumps({
                    "type": "automation_result",
                    "request_id": request_id,
                    **result,
                }, ensure_ascii=False))

            elif msg_type == "automation_replay":
                request_id = str(msg.get("request_id") or "")
                package_name = str(msg.get("package_name") or "").strip()
                task_gid = str(msg.get("task_gid") or "")
                asana_pat = str(msg.get("asana_pat") or "")
                loop = asyncio.get_running_loop()
                automation_stop_event.clear()

                def _automation_progress(text: str):
                    asyncio.run_coroutine_threadsafe(
                        ws.send(json.dumps({
                            "type": "automation_progress",
                            "request_id": request_id,
                            "text": text,
                        }, ensure_ascii=False)),
                        loop,
                    )

                try:
                    timeout = validate_replay_timeout(
                        msg.get("timeout_seconds", DEFAULT_REPLAY_TIMEOUT_SECONDS)
                    )
                    expectation = ReplayExpectation.from_values(
                        msg.get("interstitial_ids"),
                        msg.get("rewarded_ids"),
                        msg.get("aggregation_verdict"),
                    )
                    uid_ok, uid = await asyncio.to_thread(get_app_uid, package_name)
                    if not uid_ok:
                        raise RuntimeError(uid)
                    result = await asyncio.to_thread(
                        run_ad_replay_check,
                        package_name,
                        uid,
                        expectation,
                        timeout,
                        stop_event=automation_stop_event,
                        on_progress=_automation_progress,
                    )
                    if not result.get("ok") and task_gid and asana_pat:
                        comment = build_replay_failure_comment(package_name, result)
                        comment = "\n".join(comment.splitlines()[1:])
                        try:
                            client = build_asana_client(asana_pat)
                            await asyncio.to_thread(
                                add_automation_comment_once,
                                client,
                                task_gid,
                                result.get("code", "AD_REPLAY_FAILED"),
                                comment,
                            )
                        except Exception as exc:
                            result["comment_error"] = str(exc)
                    await ws.send(json.dumps({
                        "type": "automation_result",
                        "request_id": request_id,
                        **result,
                    }, ensure_ascii=False))
                except Exception as exc:
                    if task_gid and asana_pat:
                        try:
                            client = build_asana_client(asana_pat)
                            await asyncio.to_thread(
                                add_automation_comment_once,
                                client,
                                task_gid,
                                "AD_REPLAY_FAILED",
                                (
                                    "聚合广告回放检测执行失败，需要测试人员确认\n"
                                    f"包名：{package_name}\n失败原因：{exc}"
                                ),
                            )
                        except Exception:
                            pass
                    await ws.send(json.dumps({
                        "type": "automation_result",
                        "request_id": request_id,
                        "ok": False,
                        "code": "AD_REPLAY_FAILED",
                        "error": str(exc),
                    }, ensure_ascii=False))

            elif msg_type == "automation_stop":
                automation_stop_event.set()
                await ws.send(json.dumps({
                    "type": "automation_progress",
                    "request_id": str(msg.get("request_id") or ""),
                    "text": "已请求停止自动化回放监听",
                }, ensure_ascii=False))

            elif msg_type == "asana_today_tasks":
                request_id = str(msg.get("request_id") or "")
                try:
                    result = await asyncio.to_thread(
                        load_today_precheck_tasks,
                        str(msg.get("project_gid") or ""),
                        str(msg.get("asana_pat") or ""),
                    )
                    await ws.send(json.dumps({
                        "type": "asana_today_tasks_result",
                        "request_id": request_id,
                        "ok": True,
                        **result,
                    }, ensure_ascii=False))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "type": "asana_today_tasks_result",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(exc),
                    }, ensure_ascii=False))

            elif msg_type == "daily_summary_generate":
                request_id = str(msg.get("request_id") or "")
                try:
                    target_date = date.fromisoformat(str(msg.get("date") or ""))
                    client = build_asana_client(str(msg.get("asana_pat") or ""))
                    result = await asyncio.to_thread(
                        generate_daily_asana_summary,
                        client,
                        str(msg.get("project_gid") or ""),
                        target_date,
                    )
                    await ws.send(json.dumps({
                        "type": "daily_summary_result",
                        "request_id": request_id,
                        **result,
                    }, ensure_ascii=False))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "type": "daily_summary_result",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(exc),
                    }, ensure_ascii=False))

            elif msg_type == "play_precheck":
                request_id = str(msg.get("request_id") or "")
                loop = asyncio.get_running_loop()

                def _progress(text: str):
                    asyncio.run_coroutine_threadsafe(
                        ws.send(json.dumps({
                            "type": "play_precheck_progress",
                            "request_id": request_id,
                            "text": text,
                        }, ensure_ascii=False)),
                        loop,
                    )

                try:
                    result = await asyncio.to_thread(
                        run_web_precheck,
                        str(msg.get("value") or ""),
                        auto_install=bool(msg.get("auto_install", False)),
                        launch_check=bool(msg.get("launch_check", True)),
                        observation_seconds=int(msg.get("observation_seconds", 20)),
                        task_gid=str(msg.get("task_gid") or ""),
                        asana_pat=str(msg.get("asana_pat") or ""),
                        backend_api_url=str(msg.get("backend_api_url") or ""),
                        backend_x_token=str(msg.get("backend_x_token") or ""),
                        backend_token=str(msg.get("backend_token") or ""),
                        backend_user_name=str(msg.get("backend_user_name") or "rain"),
                        on_progress=_progress,
                    )
                    await ws.send(json.dumps({
                        "type": "play_precheck_result",
                        "request_id": request_id,
                        "ok": True,
                        "result": result,
                    }, ensure_ascii=False))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "type": "play_precheck_result",
                        "request_id": request_id,
                        "ok": False,
                        "error": str(exc),
                    }, ensure_ascii=False))

            else:
                await ws.send(json.dumps({"type": "error", "text": f"未知消息类型: {msg_type}"}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _connected_ws.discard(ws)
        _stop_logcat()
        if logcat_task and not logcat_task.done():
            logcat_task.cancel()


async def _install_xapk_ws(ws, xapk_path: str):
    """安装 xapk 文件：解压 → install-multiple → 推送 OBB"""
    await ws.send(json.dumps({"type": "cmd_display", "text": f"解压并安装 XAPK: {xapk_path}"}))
    await ws.send(json.dumps({"type": "line", "text": "正在解压 XAPK..."}))

    import tempfile
    import zipfile

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
                await ws.send(json.dumps({"type": "error", "text": "XAPK 中没有找到 APK 文件"}))
                await ws.send(json.dumps({"type": "done", "exit": -1}))
                return

            await ws.send(json.dumps({"type": "line", "text": f"找到 {len(apks)} 个 APK，正在安装..."}))

            # install-multiple
            cmd = [_find_adb(), "install-multiple", "-r"] + apks
            await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))
            await _run_cmd_ws(ws, cmd)

            # 推送 OBB
            for root, _, files in os.walk(tmp):
                for f in files:
                    if f.endswith(".obb"):
                        obb_dir = os.path.relpath(root, tmp)
                        obb_path = os.path.join(root, f)
                        device_obb = f"/sdcard/{obb_dir}"
                        await ws.send(json.dumps({"type": "line", "text": f"推送 OBB: {f} → {device_obb}/"}))
                        mkdir_cmd = [_find_adb(), "shell", "mkdir", "-p", device_obb]
                        await _run_cmd_ws(ws, mkdir_cmd)
                        push_cmd = [_find_adb(), "push", obb_path, device_obb + "/"]
                        await _run_cmd_ws(ws, push_cmd)

    except zipfile.BadZipFile:
        await ws.send(json.dumps({"type": "error", "text": "文件不是有效的 XAPK/ZIP 包"}))
        await ws.send(json.dumps({"type": "done", "exit": -1}))
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "text": f"XAPK 安装失败: {e}"}))
        await ws.send(json.dumps({"type": "done", "exit": -1}))


async def _download_install_ws(ws, url: str):
    """从 URL 下载 APK/XAPK 并安装到手机"""
    filename = download_artifact_filename(url)
    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        await ws.send(json.dumps({"type": "cmd_display", "text": f"下载并安装: {url}"}))
        await ws.send(json.dumps({"type": "line", "text": f"正在下载 {filename}..."}))

        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        })

        loop = asyncio.get_event_loop()

        def _download():
            with urllib.request.urlopen(req, timeout=300) as resp:
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            return True

        await loop.run_in_executor(None, _download)

        await ws.send(json.dumps({"type": "line", "text": "下载完成，正在安装..."}))

        if filename.lower().endswith(".xapk") or _zip_contains_apks(tmp_path):
            await _install_xapk_ws(ws, tmp_path)
        else:
            cmd = [_find_adb(), "install", "-r", tmp_path]
            await ws.send(json.dumps({"type": "cmd_display", "text": _cmd_display(cmd)}))
            await _run_cmd_ws(ws, cmd)

    except urllib.error.URLError as e:
        await ws.send(json.dumps({"type": "error", "text": f"下载失败: {e.reason}"}))
        await ws.send(json.dumps({"type": "done", "exit": -1}))
    except Exception as e:
        await ws.send(json.dumps({"type": "error", "text": f"下载安装失败: {e}"}))
        await ws.send(json.dumps({"type": "done", "exit": -1}))
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _zip_contains_apks(zip_path: str) -> bool:
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            return any(name.lower().endswith(".apk") for name in z.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


async def _run_cmd_ws(ws, cmd: list[str], cwd=None, timeout=None):
    """执行短命令，流式推送结果"""
    q: queue.Queue = queue.Queue()

    def on_line(line):
        q.put(("line", line))

    def on_done(rc):
        q.put(("done", rc))

    _run_stream(cmd, on_line, on_done, cwd=cwd, timeout=timeout)

    while True:
        try:
            kind, val = q.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        if kind == "line":
            try:
                await ws.send(json.dumps({"type": "line", "text": val}))
            except Exception:
                break
        elif kind == "done":
            try:
                await ws.send(json.dumps({"type": "done", "exit": val}))
            except Exception:
                pass
            break


async def _run_get_uid_ws(ws, cmd: list[str]):
    """执行 dumpsys 提取 UID"""
    uid_found = None
    q: queue.Queue = queue.Queue()

    def on_line(line):
        q.put(("line", line))

    def on_done(rc):
        q.put(("done", rc))

    _run_stream(cmd, on_line, on_done)

    while True:
        try:
            kind, val = q.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        if kind == "line":
            try:
                await ws.send(json.dumps({"type": "line", "text": val}))
            except Exception:
                break
            if uid_found is None:
                uid_found = _extract_uid_from_dumpsys_line(val)
        elif kind == "done":
            try:
                await ws.send(json.dumps({"type": "done", "exit": val}))
            except Exception:
                pass
            break

    if uid_found:
        try:
            await ws.send(json.dumps({"type": "uid", "value": uid_found}))
        except Exception:
            pass


async def _logcat_reader(ws, pattern: str, proc: subprocess.Popen):
    """后台线程读 logcat，异步推送匹配行"""
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    ws_ok = [True]

    def _send_line(line):
        if not ws_ok[0]:
            return
        async def _do():
            try:
                await ws.send(json.dumps({"type": "logline", "text": line}))
            except Exception:
                ws_ok[0] = False
        asyncio.run_coroutine_threadsafe(_do(), loop)

    def _read():
        try:
            for line in proc.stdout:
                if stop_event.is_set() or not ws_ok[0]:
                    break
                if pattern in line:
                    _send_line(line.rstrip())
        except Exception:
            pass
        async def _stopped():
            try:
                await ws.send(json.dumps({"type": "logcat_stopped"}))
            except Exception:
                pass
        if ws_ok[0]:
            asyncio.run_coroutine_threadsafe(_stopped(), loop)

    t = threading.Thread(target=_read, daemon=True)
    t.start()

    try:
        while t.is_alive():
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        _stop_logcat()


async def main():
    print("=" * 50)
    print("  APK Tool 本地 ADB 代理")
    print("=" * 50)
    print("  [ADB 检测]")
    for msg in _startup_msgs:
        print(msg)
    print("  ---")
    print(f"  最终路径: {_adb_path}")
    print(f"  设备状态: {'🟢 已连接' if _check_device() else '🔴 未连接（请插 USB 并开启调试）'}")
    print(f"  监听地址: ws://localhost:9527")
    print(f"  回填接口: http://localhost:9528/fill")
    print("  按 Ctrl+C 停止")
    print("=" * 50)

    # 优雅退出
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _shutdown():
        _stop_logcat()
        loop.call_soon_threadsafe(lambda: stop.set_result(None))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    http_server = await _http_server_main()

    async with ws_serve(handle_connection, "localhost", 9527):
        await stop

    http_server.close()
    await http_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
