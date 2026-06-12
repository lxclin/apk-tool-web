# APK Tool — ADB 桌面控制台

基于 Python + Tkinter 的安卓 ADB 测试工具，用于适配流程中替代手动敲 ADB 指令，提供一键化操作按钮和实时控制台输出。

## 功能概览

### Tab 1：APK 工具

| 功能 | 说明 |
|------|------|
| 生成二维码 | 输入 Google Play 链接，生成二维码供手机扫码跳转 |
| 推送安装 | 输入本地 `.apk` 路径，通过 `adb install -r` 安装到手机 |
| URL 跳转 | 输入 Google Play URL，通过 ADB 在手机上打开应用页面 |

### Tab 2：ADB 指令

#### 配置区域

| 字段 | 说明 |
|------|------|
| 包名 | 目标应用包名，可从 config.json 读取或手动输入 |
| Config 路径 | `config.json` 位置，默认指向工作目录 |
| 工作目录 | 适配脚本与 `zygote_build.sh` 所在目录 |
| 应用 UID | 点击"获取应用 UID"后自动填入，供 logcat 过滤使用 |

#### 操作按钮（点击后在控制台实时显示命令 + 流式输出结果）

| 按钮 | 等价 ADB 命令 |
|------|---------------|
| **cd 到此目录** | `cd /path/to/workdir`（设定工作上下文，自动发现 config.json） |
| **推送 Config** | `adb push config.json /data/local/tmp/zygotehole/` |
| **执行 zygote_build** | `sh zygote_build.sh`（推送 zygotehole.apk 并注入 Zygote） |
| **获取应用 UID** | `adb shell dumpsys package <包名> \| grep userId` |
| **清除缓存** | `adb shell pm clear <包名>` |
| **强制停止** | `adb shell am force-stop <包名>` |

#### Logcat 实时监听

| 按钮 | 过滤关键字 |
|------|-----------|
| **按 UID 过滤 ZGSDK.AutoDetector** | `ZGSDK.AutoDetector`（核心功能，自动附加 `--uid`） |
| Max 聚合 | `ZGSDK.Max` |
| IronSource | `ZGSDK.iron` |
| AdMob | `ZGSDK.admob` |
| Single | `ZGSDK.single` |
| Send | `ZGSDK.send` |
| 插屏聚合ID | `MaxInterstitialAdMulti` |
| 激励聚合ID | `MaxUnifiedAd` |
| **停止监听** | 终止当前 logcat 流 |

## 实现原理

### 架构总览

```
main.py          → 入口，启动 tkinter 主循环
gui.py           → 界面层，双标签页 + 控制台输出 + 事件处理（约 360 行）
adb_pusher.py    → 命令层，ADB 命令构建 + 流式执行（约 100 行）
qr_generator.py  → 工具层，URL → 二维码图片
```

### 核心设计

#### 1. 流式命令执行（`run_stream`）

所有操作按钮（短命令）和 logcat 监听（长命令）统一走流式管道，不阻塞 GUI：

```
用户点击按钮
  → build_*_cmd() 构建命令列表 ["adb", "push", ...]
  → _run_command(cmd) 显示命令文本到控制台
  → run_stream(cmd, on_line, on_done)
       → subprocess.Popen 启动进程，stdout=PIPE, text=True, bufsize=1（行缓冲）
       → 后台线程逐行读取 proc.stdout
       → 每行触发 on_line → root.after(0, ...) 回到主线程追加到 Text 控件
       → 进程结束后触发 on_done → 显示退出码
  → 控制台实时滚动显示输出
```

关键点：
- `bufsize=1` + `text=True` 实现行缓冲，每写一行立即可读
- `root.after(0, callback)` 将 UI 更新切回主线程，线程安全
- 短命令（push、clear 等）执行完自动终止；长命令（logcat）持续运行直到点击"停止"

#### 2. ADB 自动发现

```
get_adb_path() 优先级:
  1. 用户手动指定的路径（set_adb_path）
  2. 系统 PATH 中的 adb（shutil.which）
  3. 常见安装位置列表（Homebrew、Android SDK 等）
```

执行时使用完整路径，控制台显示统一用 `adb` 简写（`_cmd_display` 函数做替换）。

#### 3. UID 自动回填

```
获取 UID 流程:
  1. 执行 adb shell dumpsys package <包名>
  2. 正则匹配 userId=(\d+)，实时提取 UID
  3. 存入 _cached_uid，界面显示
  4. 后续 logcat 自动附加 --uid=<值>

按 UID 过滤按钮:
  1. 如果未缓存 UID，自动调用 get_app_uid() 获取
  2. 执行 adb logcat -c 清除旧缓冲
  3. 启动 adb logcat --uid=<UID> 持续监控
  4. Python 侧过滤包含 pattern 的行，实时追加到控制台
```

#### 4. 控制台终端风格

- 暗底亮字配色（`bg=#1e1e1e, fg=#d4d4d4`），等宽字体 Menlo
- 命令提示符 `$ ...` 蓝色，输出默认灰色，成功绿色，错误红色
- 所有按钮操作同时显示命令和执行结果，模拟真实终端体验

#### 5. 按钮互斥保护

操作按钮执行期间自动 `DISABLED`，完成后恢复，防止重复点击导致竞态。logcat 监听期间如已有流在跑，会提示先停止。

### 模块职责

**`adb_pusher.py`**
- `run_stream(cmd, on_line, on_done, cwd)` — 通用流式命令执行引擎
- `build_*_cmd(...)` — 各命令的命令行构建（返回 `list[str]`）
- `start_logcat_stream(pattern, uid)` — 启动 logcat 持续流，返回 Popen 对象
- `stop_logcat_stream(proc)` — 终止 logcat 流
- `get_app_uid(pkg)` — 同步查询 UID（用于自动回填）
- 原有函数 `push_config`、`check_device`、`push_apk` 等保留兼容

**`gui.py`**
- `APKToolApp` 类管理全部 UI 状态
- `_build_apk_tab` / `_build_adb_tab` — 两个标签页布局
- `_run_command(cmd, cwd)` — 显示命令 → 流式执行 → 显示结果
- `_start_logcat_stream` / `_on_stop_logcat` — logcat 生命周期管理
- `_cmd_display` — 全路径替换为 `adb` 简写用于显示

## 依赖

```
qrcode>=8.0       # 二维码生成
Pillow>=10.0      # 图片处理
pytest>=8.0       # 测试框架
```

## 启动

```bash
cd /Users/a1506/Documents/apk-tool-1.0
pip install -r requirements.txt
python3 main.py
```

## 前提条件

- macOS 已安装 ADB（`brew install android-platform-tools`）
- 安卓手机 USB 连接并开启 USB 调试
- 工作目录下有 `zygote_build.sh` 和 `zygotehole.apk`
- `config.json` 配置正确的 `packageName`、`appId`、`taskUUID`
