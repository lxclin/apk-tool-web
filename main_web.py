"""Web 版入口 — 双击启动，自动打开浏览器"""
import os
import sys
import webbrowser
import threading
import time

# 强制 DEV_MODE，内网/本地使用不限制 IP
os.environ.setdefault("DEV_MODE", "true")

import uvicorn
from server import app


def open_browser():
    """等服务器就绪后自动打开浏览器"""
    time.sleep(1.5)
    webbrowser.open("http://localhost:8000")


if __name__ == "__main__":
    print("=" * 50)
    print("  APK Tool Web 已启动")
    print("  浏览器访问 → http://localhost:8000")
    print("  按 Ctrl+C 停止服务")
    print("=" * 50)

    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
