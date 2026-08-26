# APK Tool 1.4

面向安卓适配流程的 ADB 工具箱，包含桌面 GUI、FastAPI Web 控制台、本地 ADB WebSocket 代理，以及 Google Sheets / Asana 同步脚本。

近期迭代说明：[2026-08-04 ～ 2026-08-26 更新记录](UPDATE_NOTES_2026-08-04_TO_2026-08-26.md)

桌面标题和 `GET /api/version` 会明确显示版本及 `source/packaged` 运行方式，便于区分源码启动与旧打包应用。发布前运行 `python3 release_check.py`，确认版本清单、MAX Share 接收器和桌面/Web 打包入口完整。

## 功能清单

### 桌面版

入口：`python3 main.py`

- APK 工具页
  - 生成 Google Play 链接二维码。
  - 解析 URL，并自动回填包名、AppId、GP 链接。
  - 安装本地 APK、XAPK、拆分 APK 目录。
  - 从直链下载 APK/XAPK 并自动安装。
  - 在手机上打开 Google Play / market 页面。
  - APKCombo 下载。
  - APKPure 包名搜索安装。
  - 选择 APK、选择目录、设置 ADB 路径。
  - 第三方包清理，支持预览、白名单保留、一键卸载，以及逐包卸载进度和失败统计。
  - 记忆上次使用的路径和清理白名单。

- 页面预检页
  - “读取今日任务”会同步读取任务评论，按最新有效评论恢复已加黑、暂不适配、闪退、参数待确认、后台提交失败、回放失败和聚合适配成功等状态；刷新不会再统一重置为待处理。
  - 评论已有业务结论的任务不会再次进入批量预检；没有评论结论时保留当前程序会话中的下载、安装和检查状态。
  - 只读取与当天日期一致的 Asana 区段，例如 `7.30执行`，并保持任务顺序。
  - 从任务描述提取包名、UP2 AppId 和 Google Play 链接。
  - 可按顺序批量预检今日未完成任务，自动跳过已完成任务，并支持停止。
  - 支持“批量预检并自动适配”：完成页面检查、下载及启动检查后，将本轮合格且已安装的包体按列表顺序移交自动化适配队列。
  - 记录当日已读取 Task GID；一天内再次读取时只把真正新增的任务标记为“新增待预检”，避免重复处理旧任务。
  - 通过 ADB 在手机 Play Store 打开应用页面，不自动下载安装。
  - 读取 Android UI 控件文字，识别包含广告、仅应用内购、设备不支持和地区不支持。
  - 优先判断业务终止规则：仅标注应用内购且无广告时加黑；包名含独立 `jp` 段且页面存在明显日文内容时按日本包体加黑；未发现广告和内购标识的应用继续下载并人工确认。
  - UI 信息不足时可使用本机 Tesseract 对内存截图进行 OCR，不保留正常截图。
  - 页面信息未稳定时返回“暂时无法判断”，避免错误加黑。
  - 对仅内购、无广告/内购、设备不支持、地区不支持和无法判断结果写入幂等 Asana 评论。
  - 可选“检测到广告后自动下载安装”，通过 UI 控件定位安装按钮并等待包管理器确认完成。
  - 自动下载默认开启；默认单轮最多新下载 6 个、安装间隔 60 秒，页面全部预检后会继续补下载延后任务，遇到登录/验证或下载失败后暂停后续自动下载。
  - 大包在 Google Play 后台下载时不会阻塞后续任务；再次读取任务或开始批处理前会按设备安装状态把“后台下载中”自动刷新为“安装完成”。
  - Google Play 因地区、设备或无包而无法下载时，会自动检查 APKCombo 是否存在完全一致包名及真实下载版本，并区分“全网无包”“APKCombo 有包”和“需要人工确认”；“全网无包”会自动回填 Asana、通过接口写入后台 `block_ps`，刷新 A2 缓存并回读确认。
  - 安装完成或检测到应用已安装后，可自动启动并观察进程与 Java/Native 崩溃日志（默认 20 秒）。
  - 确认闪退时评论“包体闪退，暂不适配”；无闪退时强制停止当前应用，再处理下一条任务。
- 自动化适配页（聚合适配后半程）
  - 与原有手动“一键复制全部”“跳转后台”和 ADB 操作按钮相互独立，不改变旧按钮逻辑。
  - 可从页面预检选中的 Asana 任务带入 Task GID、包名、UP2 appid 和原描述。
  - 聚合参数检测持续轮询 `最终判断`，正常至少等待 60 秒；日志仍在增长时最长等待 90 秒，避免在设备约 45 秒输出综合结果前提前判空。
  - 已取得 SDK Key 或聚合 ID、但始终没有综合结果时标记“检测结果未完整输出”，不误报为“聚合类型为空”。
  - 参数结果显示识别方式、识别依据、置信度和自动提交策略：AutoDetector 明确结论为高置信度；仅通过 IronSource 标准 `video/inter` 组合推断时为中置信度；证据不完整为低置信度并禁止提交。
  - 自动将聚合参数写到 Asana 描述的 GP 链接下方，并保留包名、UP2 appid 与 GP 链接。
  - 自动化适配使用 `s10_package_info` 接口提交聚合参数，复用“数据同步”页的接口地址、X-Token、固定 token 和适配人员；依次校验写入响应、调用 `delete_a2_package_cache` 清除当前包缓存，再通过 `cp_adapt/list` 回读该包并逐字段比对。三个步骤均成功才进入广告回放。原有手动跳转后台逻辑保持不变。
  - 自动化 Logcat 读取限制单次返回量并带 ADB 状态检查和自动重试；设备在线时的瞬时读取超时会继续等待，持续的 ADB/Logcat 基础设施故障会安全停止批量队列且不会写入 Asana 业务失败结论。
  - 后台提交成功后强制停止并重启游戏，清除旧 logcat 后按应用 UID 监听聚合广告回放。
  - 每次重启成功后重新计算进程启动宽限，默认等待 25 秒；同时检查主进程、包名前缀子进程和前台 Activity。只有进程曾成功运行后再消失才判定异常退出，明确闪退仍要求 Java/Native 崩溃证据。
  - 后台提交期间收到停止请求时，当前接口提交、清缓存和回读校验原子步骤结束后立即终止，不再进入聚合回放。
  - 回放监听默认最长 500 秒，可在 10-600 秒范围内配置；只验证已配置 ID 的广告类型，两种 ID 都存在时必须分别回放成功。
  - 回放 Logcat 由独立读取线程持续批量排空，避免 AppLovin 等 SDK 的高流量初始化日志堵塞管道；判定器处理完整 UID 日志，界面仅显示匹配当前广告 ID 的展示、收益、状态和错误证据。
  - `loadAd`、`showAd`、`onAdLoaded` 和 `display_start` 不算成功；仅接受匹配广告 ID 的 `onAdDisplayed`、`onAdImpression`、`display_success` 或收益回调。
  - 超时或执行失败会将原因写入 Asana 评论并把当前自动化任务标记为失败。
  - 聚合回放成功后写入结构化 Asana 成功评论，供页面预检刷新和当日总结恢复成功状态。
  - 首次按 IronSource（含 `video/inter` 推断结果）提交后，第一次回放仍持续监听同一 UID 的 AutoDetector；若随后明确、高置信度识别为 MAX，则先通过接口清空 IronSource 参数，再用完整 MAX 参数覆盖提交、刷新缓存、更新 Asana 描述并执行第二次 MAX 回放。普通 MAX SDK 初始化日志不会触发切换。
  - 自动识别并关闭 Android 通知权限弹窗，避免弹窗遮挡游戏启动流程和广告触发。

- ADB 指令页
  - 读取和写入 `config.json`。
  - 推送 config 到 `/data/local/tmp/zygotehole/`。
  - 执行 `zygote_build.sh`。
  - 修复 `zygotehole` 权限。
  - 获取应用 UID。
  - 清除缓存、强制停止、打开应用、清空 Play Store 缓存。
  - 实时监听 logcat，支持按 UID 过滤。
  - 查看 Java 崩溃和 Native 崩溃日志。
  - 从 `ZGSDK.AutoDetector` 日志提取 `af_key`、聚合平台、聚合 ID、归因平台和各 SDK Key。
  - 生成适配后台 URL。
  - 一键复制全部提取字段。
  - 显示 ADB 控制台输出，并支持停止当前命令。

- 自动化脚本页
  - 粘贴 JSON 后批量规范化 `delay`。
  - 首个 `delay` 至少 15000ms，后续按配置最小值调整。
  - 复制结果、清空脚本。

- 数据同步页
  - 从 CP 后台拉取适配记录并写入 Google Sheets。
  - Google Sheets 到 Asana 的幂等同步。
  - 创建或更新 Asana 任务，并回填任务链接。
  - 记忆同步相关凭证和路径。
  - 支持粘贴 Asana 任务地址后自动解析并保存父任务 GID，避免把链接中的 story/comment GID 当成任务 GID。

- 当日总结页
  - 读取指定日期执行区段的任务评论，汇总聚合成功、动作成功、暂不适配和加黑结果。
  - 对归因问题、聚合类型或广告 ID 缺失、回放失败、闪退、全网无包、设备/地区限制、Google 登录和人工备注等原因分别计数。
  - 同时识别“动作适配成功”“动作适配完成”等人工评论表达，并优化包名与原因之间的换行和可读性。

### Web 版

入口：`python3 main_web.py`

- 启动 FastAPI 服务，并自动打开 `http://localhost:8000`。
- 提供 REST 接口：
  - `GET /api/config`
  - `POST /api/config`
  - `GET /api/device`
  - `POST /api/get-uid`
  - `POST /api/adb-path`
  - `GET /api/whitelist`
  - `POST /api/whitelist`
  - `POST /api/upload-apk`
- 提供 WebSocket `/ws`
  - 推送 Config、执行 `zygote_build`、获取 UID、清缓存、强制停止、打开应用。
  - 打开 URL、安装文件、APKCombo、APKPure。
  - 实时 logcat 监听。
- 已同步桌面版“页面预检”页：
  - 读取当天 Asana 区段，保持任务顺序并自动跳过已完成任务。
  - Asana PAT 由用户在页面输入，只保留在当前页面内存，不写入浏览器存储。
  - 单条或批量执行 Google Play 页面预检，复用桌面版广告优先判断逻辑。
  - 自动为仅内购、无广告、设备/地区不支持、无法判断等结果写入幂等 Asana 评论。
  - 自动下载安装默认开启，默认单轮下载上限为 6，支持下载冷却间隔和预检结束后的补下载。
  - 安装完成后可启动应用检查闪退，检查结束自动杀死应用进程。
- 已同步“自动化适配”页的聚合参数回填、识别证据与置信度、后台接口提交/清缓存/回读校验和广告回放检测能力。
- 前端位于 `static/index.html`。
- 支持 IP 白名单控制。

### 本地代理

入口：`python3 adb_proxy.py`

- 默认监听 `ws://localhost:9527`。
- 提供本机 ADB 能力给远端页面或静态页面调用。
- 共享页面预检、自动安装、启动闪退检查与 `ZGSDK.AutoDetector` 解析逻辑，保证桌面版和 Web 版一致。
- 提供 `POST /fill` 和 `GET /fill/latest`，GUI 会轮询 `http://localhost:9528/fill/latest` 自动回填数据。
- 打包后会自动查找可用 ADB。

### 数据同步脚本

入口：`python3 -m auto_asana.main`

- 拉取 CP 适配记录。
- 写入 Google Sheets。
- 创建、更新和回填 Asana 任务链接。
- 保持幂等同步。

## 启动方式

### 安装依赖

```bash
pip install -r requirements.txt
pip install -r auto_asana/requirements.txt
```

### 桌面版

```bash
python3 main.py
```

### Web 版

```bash
python3 main_web.py
```

浏览器访问：

```text
http://localhost:8000
```

### 本地代理

```bash
python3 adb_proxy.py
```

监听：

```text
ws://localhost:9527
```

回填接口：

```text
http://localhost:9528/fill/latest
```

### 数据同步脚本

```bash
python3 -m auto_asana.main
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
- `requests`
- `asana`

数据同步模块：

```bash
pip install -r auto_asana/requirements.txt
```

包含：

- `pytest`
- `google-api-python-client`
- `asana`

## 测试

```bash
pytest tests auto_asana/test_sync.py -q
```

## 打包

项目保留了三个 PyInstaller 目标：

- `APK Tool.spec`：桌面版。
- `APK Tool Web.spec`：Web 版。
- `ADB Proxy.spec`：本地代理。

```bash
pyinstaller "APK Tool.spec"
pyinstaller "APK Tool Web.spec"
pyinstaller "ADB Proxy.spec"
```

`build_proxy.sh` 可用于构建带 adb 的 macOS 代理 `.app` 和 `.dmg`。

## 前提条件

- macOS 环境。
- 已安装 ADB，例如 `brew install android-platform-tools`。
- 安卓设备已通过 USB 连接并开启 USB 调试。
- 适配工作目录中有 `config.json`、`zygote_build.sh` 和 `zygotehole.apk`。
- 使用同步功能时，还需要配置 Google Service Account、Asana PAT 和相关 ID。

## 常见本地文件

- `gui_settings.json`：桌面版记忆路径和输入内容。
- `gui_crash.log`：桌面版未捕获异常日志。
- `ip_whitelist.json`：Web 服务 IP 白名单。
- `coordinate_preview.html`：坐标预览文件。
- `simplified_actions.json`：自动化脚本示例。
- `CpAdaptEditFieldMapping.md`：适配后台字段映射说明。
- `build/`、`dist/`：打包产物，通常不提交版本库。
