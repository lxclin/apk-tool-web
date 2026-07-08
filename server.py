"""Web 后端 —— WebSocket 处理所有 ADB 操作，REST 处理配置读写"""
import json
import os
import sys
import re
import queue
import threading
import subprocess
import asyncio

import tempfile
import shutil
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from adb_pusher import (
    check_device, get_app_uid, set_adb_path,
    build_push_config_cmd, build_zygote_build_cmd,
    build_get_uid_cmd, build_clear_cache_cmd, build_force_stop_cmd,
    build_open_app_cmd, build_logcat_cmd,
    start_logcat_stream, stop_logcat_stream,
    run_stream, cmd_to_str, get_adb_path, extract_uid_from_dumpsys,
)

CONFIG_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518/config.json"
)
WORK_DIR_DEFAULT = os.path.expanduser(
    "~/Documents/test/适配动作与聚合参数获取_260518"
)

# ── IP 白名单 ─────────────────────────────────────────────────────

DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")

WHITELIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ip_whitelist.json")
DEFAULT_WHITELIST = ["127.0.0.1", "::1", "localhost"]


def load_whitelist() -> list[str]:
    if os.path.isfile(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE) as f:
                ips = json.load(f)
                return ips if isinstance(ips, list) else DEFAULT_WHITELIST
        except Exception:
            pass
    return DEFAULT_WHITELIST


ALLOWED_IPS: list[str] = load_whitelist()


def get_client_ip(request: Request) -> str:
    """获取真实客户端 IP（支持 ngrok 等代理转发的头）"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return real_ip.strip()
    host = request.client.host if request.client else ""
    return host


class IPWhitelistMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if DEV_MODE:
            return await call_next(request)
        client_ip = get_client_ip(request)
        if client_ip in ALLOWED_IPS:
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={"error": f"IP 不在白名单中: {client_ip}"},
        )

def _base_dir() -> str:
    """返回应用根目录，兼容 PyInstaller 打包和源码运行"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


app = FastAPI(title="APK Tool Web")
app.add_middleware(IPWhitelistMiddleware)

# logcat 全局状态
_logcat_proc: subprocess.Popen | None = None
_logcat_task: asyncio.Task | None = None


@app.get("/")
async def index():
    return FileResponse(os.path.join(_base_dir(), "static", "index.html"))


# ── 配置 REST ─────────────────────────────────────────────────────

@app.get("/api/config")
async def read_config(path: str = ""):
    cfg_path = path or CONFIG_DEFAULT
    if not os.path.isfile(cfg_path):
        return {"ok": False, "error": f"文件不存在: {cfg_path}"}
    try:
        with open(cfg_path) as f:
            data = json.load(f)
        item = data.get("data", [{}])[0] if data.get("data") else {}
        return {
            "ok": True,
            "packageName": item.get("packageName", ""),
            "appId": item.get("appId", ""),
            "taskUUID": item.get("taskUUID", "mediation_test_snow"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/config")
async def write_config(data: dict):
    pkg = data.get("packageName", "").strip()
    appid = data.get("appId", "").strip()
    if not pkg:
        return {"ok": False, "error": "包名不能为空"}
    if not appid:
        return {"ok": False, "error": "AppId 不能为空"}
    cfg_path = data.get("path") or CONFIG_DEFAULT
    if not os.path.isfile(cfg_path):
        return {"ok": False, "error": f"文件不存在: {cfg_path}"}
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        if not cfg.get("data"):
            cfg["data"] = [{}]
        item = cfg["data"][0]
        if "packageName" in data:
            item["packageName"] = data["packageName"]
        if "appId" in data:
            item["appId"] = data["appId"]
        if "taskUUID" in data:
            item["taskUUID"] = data["taskUUID"]
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/device")
async def device_status():
    return {"ok": True, "connected": check_device()}


@app.post("/api/get-uid")
async def get_uid_api(data: dict):
    pkg = data.get("packageName", "").strip()
    if not pkg:
        return {"ok": False, "error": "请输入包名"}
    if not check_device():
        return {"ok": False, "error": "没有已连接的设备"}
    ok, uid = get_app_uid(pkg)
    if ok:
        return {"ok": True, "uid": uid}
    return {"ok": False, "error": uid}


@app.post("/api/adb-path")
async def set_adb(data: dict):
    path = data.get("path", "")
    if path and os.path.isfile(path):
        set_adb_path(path)
        return {"ok": True}
    return {"ok": False, "error": "文件不存在"}


@app.get("/api/whitelist")
async def get_whitelist():
    return {"ok": True, "ips": ALLOWED_IPS}


@app.post("/api/whitelist")
async def update_whitelist(data: dict):
    global ALLOWED_IPS
    ips = data.get("ips", [])
    if not isinstance(ips, list):
        return {"ok": False, "error": "ips 必须是数组"}
    ALLOWED_IPS = ips
    try:
        with open(WHITELIST_FILE, "w") as f:
            json.dump(ips, f, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── WebSocket ─────────────────────────────────────────────────────

def _adb_display(cmd: list[str]) -> str:
    """命令列表 → 可读显示"""
    s = cmd_to_str(cmd)
    adb = get_adb_path()
    if adb:
        s = s.replace(adb, "adb")
    return s


async def _run_get_uid(ws: WebSocket, cmd: list[str]):
    """后台任务：获取 UID 并发送结果"""
    _, uid = await _run_cmd_stream(ws, cmd, uid_regex=True)
    if uid:
        try:
            await ws.send_json({"type": "uid", "value": uid})
        except Exception:
            pass


async def _run_cmd_stream(ws: WebSocket, cmd: list[str], cwd=None,
                          uid_regex: bool = False, timeout=None):
    """执行短命令，逐行推送到 WebSocket。可作为后台任务运行"""
    q: queue.Queue = queue.Queue()
    uid_found = [None]

    def on_line(line: str):
        q.put(("line", line))
        if uid_regex:
            uid = extract_uid_from_dumpsys(line)
            if uid:
                uid_found[0] = uid

    def on_done(rc: int):
        q.put(("done", rc))

    run_stream(cmd, on_line, on_done, cwd=cwd, timeout=timeout)

    exit_code = -1
    while True:
        try:
            kind, val = q.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        if kind == "line":
            try:
                await ws.send_json({"type": "line", "text": val})
            except Exception:
                break
        elif kind == "done":
            exit_code = val
            break

    try:
        await ws.send_json({"type": "done", "exit": exit_code})
    except Exception:
        pass
    return exit_code, uid_found[0]


async def _stop_logcat():
    global _logcat_proc, _logcat_task
    if _logcat_proc:
        stop_logcat_stream(_logcat_proc)
        _logcat_proc = None
    if _logcat_task and not _logcat_task.done():
        _logcat_task.cancel()
        _logcat_task = None


async def _logcat_reader(ws: WebSocket, pattern: str):
    """后台任务：线程读取 stdout，通过 run_coroutine_threadsafe 推送到 WebSocket"""
    global _logcat_proc
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    ws_ok = [True]

    def _send_line(line: str):
        """线程安全：投递到事件循环发送"""
        if not ws_ok[0]:
            return

        async def _do():
            try:
                await ws.send_json({"type": "logline", "text": line})
            except Exception:
                ws_ok[0] = False

        asyncio.run_coroutine_threadsafe(_do(), loop)

    def _read():
        try:
            for line in _logcat_proc.stdout:
                if stop_event.is_set() or not ws_ok[0]:
                    break
                if pattern in line:
                    _send_line(line.rstrip())
        except Exception:
            pass
        # 读完后投递停止信号
        async def _stopped():
            try:
                await ws.send_json({"type": "logcat_stopped"})
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
        await _stop_logcat()


@app.post("/api/upload-apk")
async def upload_apk(file: UploadFile = File(...)):
    """上传 APK/XAPK 文件到服务器临时目录"""
    try:
        suffix = Path(file.filename).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(file.file, tmp)
            filepath = tmp.name
        return {"ok": True, "filepath": filepath, "filename": file.filename}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    # 连接后立刻推送设备状态
    try:
        adb_path = get_adb_path()
        connected = check_device()
        await ws.send_json({
            "type": "device_status",
            "connected": connected,
            "adb_path": adb_path or "未找到",
        })
    except Exception:
        pass

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type", "")
            params = msg.get("params", {})

            if msg_type == "cmd":
                action = msg.get("action", "")
                pkg = params.get("packageName", "")
                cfg_path = params.get("configPath", CONFIG_DEFAULT)
                work_dir = params.get("workDir", WORK_DIR_DEFAULT)

                if action == "push_config":
                    cmd = build_push_config_cmd(cfg_path)
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_cmd_stream(ws, cmd))

                elif action == "zygote_build":
                    cmd = build_zygote_build_cmd(work_dir)
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_cmd_stream(ws, cmd, cwd=work_dir))

                elif action == "get_uid":
                    if not pkg:
                        await ws.send_json({"type": "error", "text": "请输入包名"})
                        continue
                    cmd = build_get_uid_cmd(pkg)
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_get_uid(ws, cmd))

                elif action == "clear_play_store":
                    cmd = build_clear_cache_cmd("com.android.vending")
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_cmd_stream(ws, cmd))

                elif action == "open_url":
                    url = params.get("url", "")
                    if not url:
                        await ws.send_json({"type": "error", "text": "请输入 URL"})
                        continue
                    if url.startswith("http"):
                        adb = get_adb_path()
                        cmd = [adb, "shell", "am", "start", "-a",
                               "android.intent.action.VIEW", "-d", url]
                        await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                        asyncio.create_task(_run_cmd_stream(ws, cmd, timeout=15))
                    else:
                        cmd = [get_adb_path(), "install", "-r", url]
                        await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                        asyncio.create_task(_run_cmd_stream(ws, cmd))

                elif action == "open_apkcombo":
                    pkg = params.get("packageName", "")
                    if not pkg:
                        await ws.send_json({"type": "error", "text": "无法提取包名"})
                        continue
                    adb = get_adb_path()
                    cmd = [adb, "shell", "am", "start", "-a",
                           "android.intent.action.VIEW", "-d",
                           f"https://apkcombo.com/downloader/#package={pkg}"]
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_cmd_stream(ws, cmd, timeout=15))

                elif action == "apkpure_search":
                    pkg = params.get("packageName", "")
                    if not pkg:
                        await ws.send_json({"type": "error", "text": "请输入包名"})
                        continue
                    adb = get_adb_path()
                    script = (
                        f"monkey -p com.apkpure.aegon "
                        f"-c android.intent.category.LAUNCHER 1; "
                        f"sleep 2; input keyevent 84; sleep 0.5; "
                        f"input text {pkg}; sleep 0.3; input keyevent 66"
                    )
                    cmd = [adb, "shell", script]
                    await ws.send_json({"type": "cmd_display", "text": f"adb shell (APKPure 搜索 {pkg})"})
                    asyncio.create_task(_run_cmd_stream(ws, cmd, timeout=15))

                elif action == "install_file":
                    filepath = params.get("filepath", "")
                    if not filepath or not os.path.isfile(filepath):
                        await ws.send_json({"type": "error", "text": "文件不存在"})
                        continue
                    cmd = [get_adb_path(), "install", "-r", filepath]
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    asyncio.create_task(_run_cmd_stream(ws, cmd))

                elif action in ("clear_cache", "force_stop", "open_app"):
                    if not pkg:
                        await ws.send_json({"type": "error", "text": "请输入包名"})
                        continue
                    builders = {
                        "clear_cache": build_clear_cache_cmd,
                        "force_stop": build_force_stop_cmd,
                        "open_app": build_open_app_cmd,
                    }
                    cmd = builders[action](pkg)
                    await ws.send_json({"type": "cmd_display", "text": _adb_display(cmd)})
                    timeout = 15 if action == "open_app" else None
                    asyncio.create_task(_run_cmd_stream(ws, cmd, timeout=timeout))

                else:
                    await ws.send_json({"type": "error", "text": f"未知操作: {action}"})

            elif msg_type == "logcat_start":
                global _logcat_proc, _logcat_task
                pattern = msg.get("pattern", "")
                uid = msg.get("uid", "").strip() or None
                await _stop_logcat()
                if not check_device():
                    await ws.send_json({"type": "error", "text": "没有已连接的设备"})
                    continue
                _logcat_proc = start_logcat_stream(pattern, uid)
                uid_display = f" --uid={uid}" if uid else ""
                await ws.send_json({"type": "cmd_display", "text": f"adb logcat{uid_display} | grep {pattern}"})
                _logcat_task = asyncio.create_task(_logcat_reader(ws, pattern))

            elif msg_type == "logcat_stop":
                await _stop_logcat()
                await ws.send_json({"type": "logcat_stopped"})

            elif msg_type == "device_check":
                await ws.send_json({
                    "type": "device_status",
                    "connected": check_device(),
                    "adb_path": get_adb_path() or "未找到",
                })

            else:
                await ws.send_json({"type": "error", "text": f"未知消息类型: {msg_type}"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _stop_logcat()


# ── 入口 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    adb = get_adb_path()
    print(f"APK Tool Web → http://localhost:8000")
    print(f"ADB 路径: {adb or '未找到'}")
    print(f"设备状态: {'已连接' if check_device() else '未连接'}")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
