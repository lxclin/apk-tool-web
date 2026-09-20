# 2026-09-20 修复记录

本文记录今天完成并验证的两项修复。

## 1. 第三方包一键清理导致设备重启

### 现象

使用“一键清理第三方包”连续卸载大量包体时，设备可能自动重启。日志中出现：

- `Failure calling service package: Broken pipe (32)`
- `Can't find service: package`
- `system_server` CPU 长时间接近 100%
- `WATCHDOG KILLING SYSTEM PROCESS` / `*** GOODBYE!`

### 根因

原流程会连续执行大量 `adb shell pm uninstall`，没有批次间隔，也没有检查 Android Package Manager 和 `system_server` 是否仍然可用。高负载下 Package Manager 通信中断，最终触发 Android `system_server` Watchdog 重启。

这属于 Android 系统服务 Watchdog 重启，不是普通游戏闪退，也不是单个 APK 自身崩溃。

### 修复内容

- `adb_pusher.py`
  - 增加 Package Manager 轻量健康检查：`adb shell service check package`。
  - 识别 `Broken pipe`、`Can't find service: package`、设备离线、超时等致命错误。
- `gui.py`
  - 清理开始前先检查 Package Manager。
  - 每个包之间增加 2 秒间隔。
  - 每处理 10 个包暂停 10 秒，并重新检查 Package Manager。
  - 出现致命错误时立即停止，不再继续卸载剩余包。
  - UI 明确显示“保护性停止”、停止原因和剩余未执行数量。

### 修复后的行为

例如 333 个包在第 1 个包发生 `Broken pipe` 时，流程会显示已处理 `1/333`，剩余 332 个包不再执行，避免继续压垮 `system_server`。

### 验证

- 设备健康检查返回：`Service package: found`。
- `python3 -m pytest -q tests/test_adb_pusher.py tests/test_gui.py`：322 passed。
- 针对清理保护逻辑的测试通过，确认致命错误只执行首个包并正确更新 UI。

## 2. Asana 日期父表缺失时同步失败

### 现象

2026-09-20 同步时，程序计算出当周父表 `【2026.9.14-9.18】聚合/动作适配`，由于当天已经超过 9 月 18 日，原逻辑按“同步日期必须落在父表日期范围内”判定为不匹配，并在 CP/Sheet 数据处理前终止同步。

### 修复内容

新的父表选择策略如下：

1. 优先使用包含本次同步日期的当期父表。
2. 如果当期父表尚未创建，则搜索所有“聚合/动作适配”父表。
3. 从同步日期之前的历史父表中，选择结束日期最近的一张。
4. 未来父表不参与回退；同一最近结束日期存在多个候选时中止并提示人工处理。
5. 同步结果和界面日志标记“日期父表（回退）”，避免误以为写入了当期新表。

### 示例

- 9 月 21 日没有新表：回退到 `【2026.9.14-9.18】`。
- 9 月 30 日仍没有新表：仍回退到 `【2026.9.14-9.18】`，不会强制要求创建新表。
- 如果之后创建了覆盖同步日期的 `【2026.9.28-10.2】`：优先使用这张当期表。

因此该策略支持半个月甚至一个月没有创建新父表的情况，同时不会使用未来表或配置中残留的旧 GID。

### 验证

- `python3 -m pytest -q auto_asana/test_sync.py tests/test_gui.py`：305 passed。
- 日期父表相关定向测试：8 passed。
- `python3 -m py_compile auto_asana/main.py gui.py` 通过。
- `git diff --check` 通过。
- 本次仅完成代码和测试验证，没有执行真实批量卸载或真实 Asana 写入。

## 涉及文件

- `adb_pusher.py`
- `gui.py`
- `auto_asana/main.py`
- `auto_asana/test_sync.py`
- `tests/test_adb_pusher.py`
- `tests/test_gui.py`

使用前请重启 APK Tool，使 GUI 和后台模块加载最新代码。
