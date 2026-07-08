# APK Tool 1.3

用于安卓适配流程的 ADB 辅助工具集合。项目包含 Tkinter 桌面控制台、FastAPI Web 控制台、本地 ADB WebSocket 代理，以及 Asana / Google Sheets 数据同步脚本。

## 功能概览

### 桌面版

入口：`python3 main.py`

- 生成 Google Play 链接二维码。
- 安装本地 APK、XAPK、拆分 APK 目录，或从直链下载安装。
- 使用 ADB 打开 Google Play / market 链接。
- 读取和保存适配 `config.json`。
- 推送 config、执行 `zygote_build.sh`、修复 zygotehole 权限。
- 获取应用 UID、清除缓存、强制停止、打开应用、清理 Play Store。
- 实时监听 logcat，并支持按 UID 与关键字过滤。
- 从 `ZGSDK.AutoDetector` 日志中提取聚合平台、广告位 ID、`af_key` 等字段。
- 生成后台跳转 URL，复制适配字段。
- 规范化自动化动作脚本延迟。
- 同步 CP 适配数据到 Google Sheets，并创建或更新 Asana 任务。

### Web 版

入口：`python3 main_web.py`

- 启动 FastAPI 服务并自动打开 `http://localhost:8000`。
- 通过 REST API 读取/写入 config、查询设备状态、设置 ADB 路径和白名单。
- 通过 WebSocket 执行 ADB 命令、安装 APK/XAPK、监听 logcat。
- 静态页面位于 `static/index.html`。

### 本地代理

入口：`python3 adb_proxy.py`

- 提供 WebSocket 代理，默认服务地址为 `ws://localhost:9527`。
- 供远端或静态 Web 页面调用本机 ADB。
- 复用 `adb_pusher.py` 中的日志字段解析逻辑，保证桌面版和 Web 版结果一致。

### 数据同步

入口：`python3 -m auto_asana.main`

- 拉取 CP 适配记录。
- 写入 Google Sheets。
- 创建、更新和回填 Asana 任务链接。
- 相关依赖位于 `auto_asana/requirements.txt`。

## 项目结构

```text
.
├── main.py                  # Tkinter 桌面版入口
├── gui.py                   # 桌面版 UI 与交互逻辑
├── adb_pusher.py            # ADB、APK/XAPK 安装、logcat、字段解析核心能力
├── qr_generator.py          # 二维码生成
├── main_web.py              # Web 版入口
├── server.py                # FastAPI REST / WebSocket 服务
├── adb_proxy.py             # 本地 ADB WebSocket 代理
├── static/
│   └── index.html           # Web 前端
├── auto_asana/
│   ├── main.py              # Google Sheets / Asana 同步逻辑
│   ├── requirements.txt     # 同步模块额外依赖
│   └── test_sync.py         # 同步模块测试
├── tests/                   # 桌面版和核心能力测试
├── *.spec                   # PyInstaller 打包配置
├── build/                   # PyInstaller 中间产物
└── dist/                    # PyInstaller 打包输出
```

## 依赖

主项目：

```bash
pip install -r requirements.txt
```

包含：

- `qrcode`
- `Pillow`
- `pytest`
- `fastapi`
- `uvicorn`
- `websockets`
- `python-multipart`

数据同步模块：

```bash
pip install -r auto_asana/requirements.txt
```

包含：

- `pytest`
- `google-api-python-client`
- `asana`

## 启动

### 桌面版

```bash
cd /Users/a1506/Documents/apk-tool-1.3
python3 main.py
```

### Web 版

```bash
cd /Users/a1506/Documents/apk-tool-1.3
python3 main_web.py
```

浏览器访问：

```text
http://localhost:8000
```

### 本地代理

```bash
cd /Users/a1506/Documents/apk-tool-1.3
python3 adb_proxy.py
```

默认 WebSocket：

```text
ws://localhost:9527
```

## 测试与检查

语法检查：

```bash
python3 -m py_compile main.py gui.py adb_pusher.py adb_proxy.py server.py main_web.py qr_generator.py auto_asana/main.py
```

完整测试：

```bash
pytest tests auto_asana/test_sync.py -q
```

当前本地验证结果：

```text
159 passed
```

## 打包

项目保留了三个 PyInstaller 目标：

- `APK Tool.spec`：桌面版。
- `APK Tool Web.spec`：Web 版。
- `ADB Proxy.spec`：本地代理，包含 ADB 二进制。

示例：

```bash
pyinstaller "APK Tool.spec"
pyinstaller "APK Tool Web.spec"
pyinstaller "ADB Proxy.spec"
```

打包输出默认进入 `dist/`，中间产物进入 `build/`。

## 前提条件

- macOS 环境。
- 已安装 ADB，例如 `brew install android-platform-tools`。
- 安卓设备已通过 USB 连接并开启 USB 调试。
- 适配工作目录中有 `config.json`、`zygote_build.sh` 和 `zygotehole.apk`。
- `config.json` 中包含正确的 `packageName`、`appId`、`taskUUID`。

## 常见本地文件

- `gui_settings.json`：桌面版记忆路径和输入内容。
- `ip_whitelist.json`：Web 服务 IP 白名单。
- `crash.log`、`server.log`、`ngrok.log`：运行日志。
- `*.har`：接口抓包或调试资料。
- `build/`、`dist/`：打包产物，体积较大，通常不提交版本库。
