# APK Tool 迭代总结

更新时间：2026-06-17

## 本轮迭代目标

围绕聚合参数提取、后台跳转 URL、Web/GUI 行为一致性、XAPK 安装链路和本地代理稳定性做了一轮集中修复。

## 主要变更

### 1. 后台跳转 URL 格式调整

- 后台 URL 已调整为 hash 路由格式：
  `http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?...`
- 跳转参数包含 `change=1`、`package_name`、`aggr_platform`、`aggr_chaping_id`、`aggr_jilishipin_id` 等字段。
- 插屏聚合 ID 和激励视频聚合 ID 跳转时只取首个值，避免后台不支持多值导致识别失败。
- “一键复制全部”仍保留全部提取值，不受跳转单值规则影响。

### 2. 聚合平台与聚合 ID 提取修复

- 修复了聚合 ID 被后续 SDK 段落覆盖的问题。
- 现在会先按 `ZGSDK.AutoDetector` 日志中的 SDK 段落收集聚合 ID，再根据 `最终判断` 选择对应平台：
  - `max聚合` 取 AppLovin/MAX 段落 ID
  - `IronSource聚合` 取 IronSource 段落 ID
  - `LevelPlay` 取 LevelPlay 段落 ID
  - `AdMob` 取 AdMob 段落 ID
- 对示例日志，`最终判断: max聚合` 时会正确提取：
  - 激励视频聚合 ID：`b5a21c21da9780f9`
  - 插屏聚合 ID：`caa8fdbbdf51c161`

### 3. af_key 提取与回填

- 增加 `af_key` 提取能力。
- 支持从 `af_key`、`AppsFlyer SDK Key`、`AppsFlyer Developer Key` 等日志字段中识别。
- Web 展示、复制和后台跳转参数中都已接入。

### 4. Web 端能力同步

- Web 端聚合参数提取通过本地 `adb_proxy.py` 代理执行。
- `adb_proxy.py` 已复用 GUI 相同的 `parse_autodetector_fields()` 解析逻辑，保证 Web 和 GUI 提取结果一致。
- GitHub Pages 页面此前已部署到：
  `https://lxclin.github.io/apk-tool-web/`

### 5. UID 提取增强

- “获取 UID” 兼容 `dumpsys package` 输出中的 `userId=` 和 `appId=` 两种格式。
- 解决部分游戏点击获取 UID 未识别的问题。

### 6. Google Play 打开方式调整

- GUI 中 Google Play 链接改为优先使用：
  `market://details?id=<package>`
- 并通过 `-p com.android.vending` 指定 Play Store 打开，减少先跳 WebView 再跳 Play Store 的情况。

### 7. GUI 展示优化

- 主窗口默认尺寸调整为 `900x820`，最小尺寸为 `820x640`。
- 顶部按钮拆为两行，避免按钮名称被截断。
- 聚合参数提取结果弹窗默认尺寸加大，底部按钮固定可见。

### 8. XAPK/APK 安装链路修复

- 支持选择本地 `.xapk` 文件安装。
- 支持选择拆分 APK 目录安装，自动递归收集 APK，并优先安装 `base.apk`。
- 单个 APK 使用 `adb install -r`，多个 APK 使用 `adb install-multiple -r`。
- `.xapk` 会解压后安装内部 APK，并推送 OBB 文件。
- 下载直链支持 `.apk/.xapk`，并兼容带 query 参数和大小写扩展名，例如：
  `https://example.com/game.XAPK?token=abc`
- 如果下载文件临时保存为 `.apk`，但内容实际是包含 APK 的 XAPK/ZIP，会自动按 XAPK 处理。
- “APKCombo 下载”按钮现在可以处理本地 APK/XAPK 路径、拆分目录、下载直链和 Google Play 地址。

### 9. 拖拽安装功能撤回

- 曾尝试接入 `tkinterdnd2` 实现拖拽安装。
- 由于 macOS Apple Silicon + Python 3.13 + Tk 8.6 环境下 `tkdnd` 兼容性不稳定，按当前需求已撤回该功能。
- 项目不再依赖 `tkinterdnd2`，避免 GUI 启动受拖拽组件影响。

### 10. 本地代理同步

- `adb_proxy.py` 中 XAPK 文件判断改为大小写不敏感。
- 代理下载后安装时也会识别带 query 的 XAPK 下载链接。
- 代理下载到无准确扩展名的临时文件后，会检查 ZIP 内容是否包含 APK，必要时按 XAPK 安装。

## 验证情况

已执行并通过：

```bash
python3 -m py_compile main.py gui.py adb_pusher.py adb_proxy.py server.py
pytest tests/test_main.py tests/test_gui.py tests/test_adb_pusher.py -q
```

当前测试结果：

```text
42 passed
```

本地 Web 代理 `adb_proxy.py` 已确认监听：

```text
ws://localhost:9527
```

## 涉及的主要文件

- `adb_pusher.py`
- `adb_proxy.py`
- `gui.py`
- `main.py`
- `requirements.txt`
- `static/index.html`
- `tests/test_adb_pusher.py`
- `tests/test_gui.py`
- `tests/test_main.py`
