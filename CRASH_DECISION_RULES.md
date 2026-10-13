# Android 闪退判定与历史案例规则

> 目的：统一回答“是否闪退、属于哪一层、根因是什么”，降低把系统重启、自动化中断、网络拦截误报成包体闪退的概率。本文是分析与增补规范；运行时当前使用的包体分类规则仍以 `crash_rules.json` 为准。

## 1. 判定原则

1. **先确认归属，再解释异常。** 日志中的进程/包名必须与正在测试的目标包一致。`com.google.android.gms`、`system_server` 等其他进程的异常，不可直接归因给目标游戏。
2. **以致命证据为准，不以界面现象定性。** 回到桌面、设备断连、自动化失败、应用没打开，都只是现象；需要对应进程的崩溃、ANR 或系统重启证据。
3. **追到最内层根因。** 优先读完整 `Caused by` 链和最深层异常。诸如 `InvocationTargetException`、反射封装异常可能只是外层包装，不能盖过真正的缺失类、权限拒绝等根因。
4. **区分应用、Android 系统、第三方服务、自动化工具。** 同一时刻可以有多个事件，时间相近不等于存在因果关系。
5. **证据不足就标“待复检/待人工”，不猜。** 不将“无法证明未崩溃”当成“已经崩溃”，也不因复现时恰逢某操作就断言该操作导致崩溃。

## 2. 判定顺序

按以下顺序检查，并记录命中的最高层级：

1. **设备/系统是否先出故障？** 查找 `system_server`、`zygote`、kernel 等系统进程的 `Fatal signal`、tombstone、重启前后 uptime/boot 变化。若系统先死，随后应用出现 `DeadSystemException` 或进程消失，应优先归为系统级故障；应用异常可能是后续症状。
2. **目标应用是否有 Java 致命异常？** 必须把 `FATAL EXCEPTION` / `am_crash` 与目标 `Process: 包名`、PID 和异常栈对应起来。已确认即为 Java 包体崩溃；再根据最深层异常匹配具体根因。
3. **目标应用是否有 Native 致命异常？** 确认 `>>> 目标包名 <<<` 或等价进程标识，并找到 `Fatal signal`、SIGSEGV/SIGABRT、backtrace 或 tombstone。不能仅凭同一份日志里出现任意 native signal 判给目标应用。
4. **是否为 ANR？** `am_anr`、`Application Not Responding`、输入分发超时等，单列 ANR；它表示无响应，不自动等价于闪退。
5. **没有致命栈时检查生命周期/资源回收。** 任务被移除、系统杀进程、启动被系统阻止、进程自行正常退出，都不是已证实的包体崩溃。需查 ActivityManager 的 exit reason、LMK/OOM 记录、force-stop 和任务移除原因。
6. **检查测试链路本身。** ADB `Broken pipe`、logcat 监听进程提前结束、广告回放超时、VPN 提示/网络不可达，先记作设备连接或自动化/网络问题；除非另有目标进程致命证据，否则不记包体闪退。

## 3. 规则分类表

| 分类 | 足以支持判定的主要证据 | 处理/记录方式 |
|---|---|---|
| Java 包体崩溃 | 目标包的 `FATAL EXCEPTION` 或 `am_crash`，带 PID/异常栈 | 标记包体闪退；提取最深层异常作为根因 |
| Native 包体崩溃 | 目标包进程标记与 fatal signal/backtrace/tombstone 相互对应 | 标记 Native 闪退；保留 signal、线程和 tombstone |
| ANR | 目标包 `am_anr`、ANR trace 或输入超时记录 | 单列 ANR，按无响应问题处理，不混为崩溃 |
| Android 系统/设备故障 | `system_server`/系统关键进程崩溃、boot/uptime 重置或系统重启证据 | 标记设备级事件；暂停把连带退出归因到单个应用 |
| 第三方系统服务故障 | 异常进程属于 GMS 等服务，而不是目标包 | 记录服务故障；除非目标包也有自己的致命栈，否则不判目标包闪退 |
| 启动/生命周期事件 | 启动被阻止、任务移除、系统回收/force-stop，且无目标致命证据 | 标记启动/生命周期问题或进程退出；不标包体闪退 |
| 自动化/连接/网络失败 | ADB 断管、logcat 监听器退出、自动化超时、VPN/网络错误 | 标记工具/连接/网络问题；保持闪退结论待复检 |
| 未知 | 有目标致命证据，但不匹配已知根因；或线索相互冲突 | 保留原始日志，标记未知/待人工；不强行猜根因 |

### 根因细分规则

- `NoClassDefFoundError` / `ClassNotFoundException`：缺失运行时类或依赖未打包/未加载。
- `UnsatisfiedLinkError` / `dlopen failed`：Native 库缺失、加载路径或 ABI 不匹配。
- `NoSuchMethodError`、`NoSuchFieldError`、`AbstractMethodError`、`IncompatibleClassChangeError`：Java/SDK 二进制版本不兼容。
- `OutOfMemoryError`：Java 堆或相关应用内存耗尽；只有系统 LMK 杀进程时应归到系统资源回收，不能冒称 Java OOM。
- `pthread_create` / 创建线程失败：线程或系统资源耗尽。
- `SecurityException`：只有它是**目标包未捕获的致命异常**时，才分类为目标 Java 崩溃。若出现在 GMS/系统服务进程，按服务异常记录。
- 反射/包装类异常：继续沿 `Caused by` 找根因；找到更具体的规则时不再以“反射失败”作为根因。

## 4. 历史案例复盘

### A. `com.mahjongrelax.matchmahjong`：缺少 AppsFlyer 异常处理类

- **现象/证据：** 设备日志中该包多次出现 `FATAL EXCEPTION`，线程名为 `Thread-46`，PID 更换后再次出现；异常栈落在游戏广告/反射调用路径，最深层为 `NoClassDefFoundError` / `ClassNotFoundException`，无法解析 `com.appsflyer.exception_manager.ExceptionManager`。
- **判定：** 已确认的 Java 包体崩溃，规则 `JAVA_MISSING_CLASS`，根因类别 `MISSING_RUNTIME_CLASS`。
- **解释：** 外层 `InvocationTargetException`/反射异常是调用包装；更具体的根因是运行时类或 SDK 依赖缺失/版本装配问题。现有日志支持这一根因类别，但不能单凭日志确定具体是构建漏包、SDK 版本冲突还是初始化/加载配置错误。
- **排除项：** 没有证据指向 VPN、网络、手机重启或 OOM。

### B. 2026-05-28 至 2026-05-29 的 `crash.log`：Android `system_server` 的 ART/Dex 优化崩溃

- **证据：** 日志显示 Pixel 5 (`redfin`)、Android 13；多个时点在 `BackgroundDexOp` 线程中，`system_server` 收到 SIGABRT。abort message 为 `Check failed: dex_files_state_ == kDexFilesChecksumsRead || dex_files_state_ == kDexFilesOpened dex_files_state_=4`，栈经过 ART `ClassLoaderContext::VerifyClassLoaderContextMatch`、`OatFileAssistant`、`PackageDexOptimizer` 和 `BackgroundDexOptService`。
- **判定：** Android 系统服务/ART 后台 Dex 优化路径的设备级崩溃，不是某个游戏的包体闪退。日志可以定位到失败断言与调用路径；**为什么 dex 状态进入该值、最初触发条件是什么，现有材料无法确定**。
- **连带现象：** 同一历史日志中的浏览器 `DeadSystemException` 出现在系统已异常的上下文，应视为系统故障后的连带症状，不能据此判浏览器根因崩溃。
- **独立事件：** 日志还包含多次 `com.google.android.gms` / `com.google.android.gms.persistent` 的 `SecurityException`（Safety Center 权限拒绝、缺少 `ACCESS_BROADCAST_RESPONSE_STATS`）。它们是 GMS 自身工作线程异常，不能与 `system_server` ART abort 或游戏闪退直接画等号。
- **边界：** 该日志证明系统关键进程崩溃；若要严格确认每次都完成整机重启，还需结合 boot ID、uptime 或重启记录。不要仅凭 `system_server` 日志替每个时点宣称“整机已重启”。

### C. “一键清理第三方包体”时设备自动重启：原因未定

- **现有材料：** 操作结果显示 333 个包卸载全部失败，部分提示目标包不存在，并有 `Failure calling service: Broken pipe (32)`；用户反馈操作过程中手机自动重启。
- **判定：** 这是一次设备重启/连接中断的报告，但目前没有同期完整 `logcat`、tombstone、重启原因或逐条 ADB 命令结果，**无法确认是系统崩溃、ADB/USB 断开、设备重启，亦无法证明批量卸载命令导致了重启**。
- **后续采集：** 重现前后保留设备序列号、时间、`adb logcat -b all`、`adb bugreport`、boot ID/uptime、卸载命令和首个错误时间。先查清设备是否真的重启及最先失效的系统组件，再判断因果。

### D. 广告回放超时、监听进程提前结束：自动化异常，尚非闪退证据

- `com.indiez.train.miner` 的广告回放超时，只能说明动作/广告流程在时限内未完成；无目标进程致命栈时不升级成闪退。
- `com.belkagames.mc` 的“Logcat 监听进程已提前结束”，说明观测链路中断；监听器退出本身不能证明被测包崩溃。需要独立核对目标包 `Process:`、PID 和系统 crash buffer 后定性。

### E. VPN 限制弹窗/转发后无网络：网络适配问题，不是闪退

游戏提示需关闭 VPN，或代理/透明转发后游戏网络不可用、服务端未收到请求，属于网络访问/代理兼容性问题。除非同时出现目标进程的 Java/native 致命证据，否则不归入闪退；网络不通也不等同于崩溃。

### F. 2026-09-28 `com.indiez.train.miner`：线程创建失败导致 Java 进程闪退

- **证据：** 设备 crash buffer 在 17:14:45.718–17:14:45.890 连续记录该包 PID 18009 的三次 `FATAL EXCEPTION`（后台 `a2_work_thread`、`Thread-71` 和 `main`）。根异常均为 `java.lang.OutOfMemoryError: pthread_create (1040KB stack) failed: Try again`。调用路径涉及 `GoogleConnectivityChecker.performCheck` 及线程池/网络 SDK；这说明线程创建资源已耗尽，但现有证据不足以把责任单独归到某一个 SDK。
- **退出/恢复：** `dumpsys activity exit-info` 记录 PID 18009 在 17:15:11 退出，原因为系统终止未送达广播；之后查询到该包已有新 PID 18806。进程退出原因与稍早的 Java 致命异常时间相邻，应合并调查，但不把 exit-info 的后续系统终止原因覆盖成最初根因。
- **判定：** 已确认 Java 包体闪退；根因规则 `JAVA_THREAD_RESOURCE`，类别 `THREAD_RESOURCE_EXHAUSTED`（线程/Native 线程资源耗尽）。虽然异常类名是 `OutOfMemoryError`，但错误细节明确为 `pthread_create` 失败，故不应笼统报告为 Java 堆内存耗尽。
- **设备状态：** 采样前后 boot ID 相同、uptime 持续递增；证据支持“应用进程崩溃/退出”，不支持“手机刚刚重启”。查询时前台 Activity 为 Launcher，目标包随后已有新进程。
- **置信度：** 高（目标包名、PID、主线程 FATAL 和具体资源错误均明确）。

### G. 2026-09-29 `io.supercent.linkedcubic`：Native SIGSEGV，崩溃落在 `libpairipcore.so`

- **证据：** 设备 crash buffer 于 15:24:24.955、15:24:32.865、15:24:33.533 三次记录目标包进程 `Fatal signal 11 (SIGSEGV)`；每次 debugger tombstone 的进程标记都是 `>>> io.supercent.linkedcubic <<<`。三次 PID 分别为 15083、15181、15213，进程启动后约 1–2 秒即崩溃。三份 backtrace 均落在包内 `lib/arm64/libpairipcore.so`，PC offset `0x3751c`；fault code 为 `SEGV_ACCERR`。
- **退出记录：** `ApplicationExitInfo` 对前两次明确记为 `APP CRASH(NATIVE)`，第三次记为 `APP CRASH(EXCEPTION)`；但对应 crash buffer 仍记录第三次 SIGSEGV，因此按原始 fatal signal 与 tombstone 判为 Native 崩溃。查询时 `pidof` 未找到该包进程。
- **判定：** 已确认目标包连续 Native 闪退，匹配 `NATIVE_SIGNAL`。可确认崩溃指令位于 `libpairipcore.so` 并发生内存访问权限错误；由于库栈无符号且每份回溯只有该库一帧，**无法从现有日志进一步确定是库内部缺陷、兼容性问题还是其他内存破坏触发**，不得擅自断言具体机制。
- **设备状态：** 采样时 Android 13 `redfin` 设备在线，boot ID/uptime 与重启不符；当前没有证据显示这几次应用崩溃引起了整机重启。
- **置信度：** 闪退类型高；Native 库内部根因未知。

## 5. 结论状态与自动化用语

- **已确认包体崩溃：** 有目标包的 Java 或 Native 致命证据；可使用“包体闪退”。
- **系统/设备级故障：** 系统关键进程崩溃或确认设备重启；记录为设备异常。相关应用退出不得自动算作各自的包体闪退。
- **ANR：** 单列“应用无响应”，不与闪退混用。
- **待复检/证据不足：** 应用进程消失、自动化结束、ADB 断连、启动失败，但没有目标致命证据。与现有 `APP_EXITED_DURING_AUTOMATION` → “启动待复检”的语义一致。
- **非崩溃：** 任务移除、VPN/网络失败、广告未展示/回放超时、普通系统回收等，有相应证据且无目标致命异常。

置信度建议：

- **高（≥0.90）：** 目标进程标识和致命栈/系统 tombstone 清楚，根因模式直接命中。
- **中（0.65–0.89）：** 崩溃类型可确认，但根因不完整，或设备/日志关联存在缺口。
- **低（<0.65）：** 只有用户描述、进程消失、超时或断连；只作线索，不据此建立自动崩溃结论。

## 6. 后续案例增补流程

每次新“闪退”都按以下步骤处理，并在本文历史案例中追加一条：

1. 记录时间及时区、设备/Android 版本、目标包名、自动化阶段、用户可见现象、日志来源和覆盖时间范围。
2. 原样留存关键证据：目标 `Process:`、PID、线程、异常全链/最深层 `Caused by`，或 Native signal/tombstone；系统故障另记 `system_server`/boot/uptime 证据。
3. 按第 2 节先定层级，再按第 3 节定根因；每个结论附证据和置信度，并明确未知项、排除项。
4. 若只是新 app 的一次性特有根因：先增补本文件案例，不急着泛化成全局代码规则。
5. 若模式可复用：将通用特征、必要前置条件和排除条件加入 `crash_rules.json`；同步更新 `tests/test_crash_rules.py`，至少覆盖一个正例、一个相似但应排除的负例和非目标进程噪声。
6. 规则命中后仍保留原始日志摘要/指纹，避免只存“闪退”二字而丢失可复核证据。新规则先标为临时；经复现或更多案例证实后再定为稳定规则。

### 新案例记录模板

```text
案例编号/日期：
设备 / Android / Build：
目标包 / PID：
现象与操作阶段：
证据来源及日志时间范围：
归属层级：应用 Java / 应用 Native / ANR / Android 系统 / 第三方服务 / 自动化或网络 / 未知
原始致命证据（保留最小必要栈）：
根因规则 / 最深层异常：
结论与置信度：
仍未知 / 排除项：
是否更新 crash_rules.json 与测试：
```

## 7. 当前实现边界

`crash_rules.json`/`crash_rules.py` 当前的职责是：在目标包范围内分类已知 Java/native 异常，并对有目标进程致命证据但未命中具体模式的情况保留未知崩溃；它也区分 ANR、任务移除、启动阻止和部分系统杀进程情形。上述历史分析还建立了**系统级故障、第三方服务故障、ADB/自动化故障和网络故障的人工归因规范**，但不能据此声称这些类别已全部实现为运行时自动规则。以后若要将这些归因自动化，应另行扩展分类器及回归测试，并确保系统进程事件不会误落到目标游戏名下。
