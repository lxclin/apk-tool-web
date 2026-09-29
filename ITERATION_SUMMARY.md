# APK Tool 迭代总结

更新时间：2026-09-29

> 本文汇总近期已完成的修复与优化，并保留 2026-06-17 的历史迭代细节。自动适配的详细规则与验证记录见 [9 月 21–24 日迭代记录](UPDATE_NOTES_2026-09-21.md)，设备清理与 Asana 日期父表修复见 [9 月 20 日修复记录](UPDATE_NOTES_2026-09-20.md)。

## 最近更新：2026-09-29 CP 批量分配结果误报修复

- 修复勾选 14 个 CP 后弹窗显示“成功 13 条、失败 1 条”，但随后 CP 后台和 Sheet 均显示 14 条已分配的误报。原因是单条提交后立即回读一次，后台记录尚未可见就被计为失败。
- 批量写入结束后，对首次回读未确认的包体按目标适配人员重新拉取后台列表，支持分页，并在短暂间隔后最多核对四次；仅确认包名和适配人员均匹配的记录才转为成功，仍未确认的保留失败。核对过程不会重复提交分配请求。
- 该弹窗统计的是 CP 后台分配结果；Sheet/Asana 同步是后续独立步骤。Asana 日期父表“回退使用”提示也不计入分配失败数。
- 已通过 CP 分配测试 `31 passed` 和相关 GUI 测试 `4 passed`；尚未用新的真实批量分配再次验证弹窗数字。

## 最近更新：2026-09-28 广告回放轮询优化

- 回放只把目标广告位的 `ZGSDK.mediationEvent` 最终加载或展示失败作为提前结束依据；单个广告网络的 `No Fill`、横幅广告或其他广告 ID 的失败不会提前结束本轮。
- 目标广告位最终失败后继续观察 20 秒；期间若出现新的请求或成功展示，继续正常判断。没有重试且没有进行中的目标请求时提前结束本轮，减少无效等待。普通未触发或仍在请求中的回放保留原超时与宽限机制。
- 原有成功条件不变：插屏和激励广告仍以真实展示证据判定成功。定向回放测试 `36 passed`；尚无足够的实测数据量化平均节省时间。

## 最近更新：2026-09-24 聚合平台支持

- 后台、Web 跳转和自动化字段映射统一覆盖 MAX、IronSource、AdMob、TopOn、Fyber、LevelPlay 六个下拉选项。
- MAX、IronSource、AdMob、LevelPlay 原有支持保持；本轮补齐 TopOn、Fyber 在回放过滤、Asana 状态同步和渐进式证据识别中的缺口，并兼容 AnyThink、Digital Turbine 别名。
- 相关测试 `494 passed`，Python 编译与 Web 内联脚本语法检查通过；真实包体回放尚未逐类实测。

## 最近更新：2026-09-24 空聚合与疑似白包备注

- 非游戏应用识别不到聚合类型时，后台接口和 Asana 统一写入“聚合类型识别为空，暂不适配”。
- 疑似白包通过接口清空适配参数并写入“疑似白包，暂不适配”；Asana 描述和评论写入“疑似白包，暂不适配（待复检）”，任务仍保留待复检状态，不写终局 Sheet 结果。
- 两条路径的定向回归测试共 `29 passed`；未对这两个真实包体执行线上提交。
- 疑似白包的 Google Play 下载量核验现使用 GUI 配置的代理；读取失败时会说明未执行白包判定。

## 最近更新：2026-09-21–23 自动适配与诊断

- 闪退识别加入可版本化规则，区分目标包真实崩溃、系统回收、任务移除和启动阻止；G99 二次真实闪退会提交“闪退，g99也闪退，暂不适配”，并在 Asana 保留原因及证据。
- 批量自动适配纳入“待处理”“待人工检查”“待人工”任务；未安装包体先自动下载安装。批量入口扫描整张任务列表，并增加实时阶段、进度和失败分类。
- 修复断点将 `pending` 误当已完成的问题；恢复时保留未完成任务、延迟重试和重试次数。回放无真实展示时区分“广告未触发待验证”和“广告展示待验证”，允许延后复测，保留待人工验证结论。
- 加强回放与提交校验：后台回读按页查找精确包名；目标广告请求后的填充波动不直接否定聚合类型；临时 MAX/IronSource 推断未获真实展示时撤销临时结论并保留原始检测证据。运行期广告 ID 兜底仅在满足平台、归因及双 ID 缺失条件时启用。
- Native 空聚合先核验 Google Play 下载量与剩余聚合线索，再决定是否进入疑似白包复检；下载量读取失败时不凭空判定白包。详细规则和当轮测试结果见 [9 月 21–24 日迭代记录](UPDATE_NOTES_2026-09-21.md)。

## 最近更新：2026-09-20 设备清理与 Asana 同步

- 批量卸载第三方包前及每 10 个包检查 Android Package Manager 健康状态；包间增加 2 秒间隔、每 10 个包暂停 10 秒。出现 `Broken pipe`、服务不可用或设备离线等致命错误时保护性停止，并显示剩余数量。
- Asana 同步优先选择覆盖同步日期的父表；缺失时回退到日期之前结束时间最近的父表，并在日志中明确标注“日期父表（回退）”。详细验证见 [9 月 20 日修复记录](UPDATE_NOTES_2026-09-20.md)。

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
