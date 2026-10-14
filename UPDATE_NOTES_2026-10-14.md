# 2026-10-14 聚合识别一致性优化

## 问题与行为

检测库先给出 IronSource、后给出 LevelPlay 时，旧符号值与广告源 placementId 可能被当作聚合广告位。现在禁止提交 video/inter 等固定文本；保留诊断标记，历史推断结果重新校验。

同轮出现多平台最终判断，或 IronSource 最终判断与 LevelPlay 专属 API 冲突时，使用 AGGREGATION_RESULT_INCOMPLETE 暂缓提交并复检。最新 LevelPlay 最终判断配合专属运行证据可解除仅 IronSource/LevelPlay 间的冲突；其他冲突、Demand Only/外部聚合控制关系仍需复核。不会仅凭 SDK 存在或类名改判。日报将平台冲突列入待复检。

## 广告位补查

IronSource/LevelPlay 缺少一个或两个广告位时，从带平台和广告类型的运行日志 adUnitId/ad_unit_id 提取，排除广告源 placementId。广告位提取独立于归因字段，提交时仍完整校验归因和 Activity。

日志仍缺失时，按当前 Logcat UID 唯一匹配包名，先 run-as、后兼容两种 su 方式，只读目标包 Mediation_Shared_Preferences.xml。先 XML 解码再解析 last_response JSON，读取 response.providerOrder.rewarded/interstitial 的对象键，排除 external 和固定文本；不借用 SDK Key/App ID。仅补缺失格式，不覆盖已取得的真实 ID，记录来源路径及包名，仍要求广告回放验证。

共享 UID、配置缺失、权限不足或读取失败时保留日志路径；读取总预算 4 秒。不改变手机配置、应用数据、后台记录和 Asana 状态。

## 验证

使用用户提供的 com.truck.transport.manager 日志离线验证：最终 LevelPlay，激励 3j1gzpw8gjkriqhm，插屏 qtlnavopjgc7dh5c，排除广告源 ID。该日志缺少归因和 Activity，不能单独用于完整自动提交。

实际设备只读尝试中目标配置路径不存在，未验证该包的配置读取成功；解析、UID 隔离、root 兼容、超时、固定文本阻断、冲突复检和日报通过自动测试验证。

验证结果：909 项非 GUI 测试、222 项 GUI 测试通过（合计 1131 项）；Web JavaScript 语法检查与 release_check.py 通过。GUI 正常回放测试隔离真实设备清理，避免依赖连接状态。

## TopOn/Thinkup 广告位补查

### 触发与行为

`com.piggywallet.spinfun.pro` 被明确判为 TopOn，但检测库详细段落没有输出 TopOn 广告位；通用日志补查只发现另一条 CloudX 链路的候选 ID。目标包实际上已保存两项 TopOn 策略，因此属于工具未覆盖该缓存格式。

现在 TopOn/AnyThink 缺少任意一种广告位时，通过现有 Logcat 字段提取入口自动补查：

1. 使用当前 Logcat UID 唯一确定包名及 Android 用户目录，共享 UID 跳过。
2. 只读复制 `databases/thinkup.db`，在本机临时目录以 immutable/只读方式查询 `sdkconfig` 的 AP_SY App ID，退出后清理本机临时文件。
3. 读取 `files/tktp/thinkup_place_data_tkv.kva`，校验长度、校验和和记录边界；损坏时尝试 kvb 备份，拒绝加密及未知格式。跳过 DELETE 标记的历史记录，仅解析有效字符串键。
4. 通过 App ID 精确匹配 `*_PL_SY`，读取内嵌 JSON 或严格限定为 32 位十六进制文件名的外部策略。仅使用策略顶层 format（1 激励、3 插屏），不取瀑布流里的广告源 unit_id。
5. 仅补缺失广告类型，保存来源、包名和置信度。不同策略给同一广告位标出不同类型时拒绝自动填充。
6. 继续执行原有归因、Activity、后台保护及回读校验，再以 TopOn 目标 ID 回放。下游 Unity 广告源展示不直接判作聚合回放成功。

总读取预算 8 秒，单次命令最多 2 秒，数据库最多 4 MiB、索引最多 2 MiB、单策略最多 1 MiB、广告位最多 16 项；普通平台和完整参数跳过。权限不足、文件缺失、字段不支持或超时只记复核原因，不改变手机数据。兼容 ADB exec-out 对远端权限错误返回 exit=0 的情况，必须校验内容，不能误用错误文本作为配置。

当前实现覆盖本次实际设备的 Thinkup 缓存布局，不宣称支持所有 TopOn SDK 缓存版本。FastKV 格式依据：[DataType](https://github.com/BillyWei01/FastKV/blob/main/fastkv/src/main/java/io/fastkv/DataType.java)、[DataParser](https://github.com/BillyWei01/FastKV/blob/main/fastkv/src/main/java/io/fastkv/DataParser.java)、[FastBuffer](https://github.com/BillyWei01/FastKV/blob/main/fastkv/src/main/java/io/fastkv/FastBuffer.java)。

### 实机结果与边界

通过新自动入口只读取得：插屏 `n1hb96qs9lv47j`，激励 `n1hb96qs9lusc4`，与人工核对一致，一次读取约 0.65 秒。策略里的 Unity 广告源分别对应 int_bid_topon2、rv_bid_topon2，验证类型没有颠倒。

本次不修改后台参数、备注、Asana 状态或正在运行的自动化；需要重启工具后再检测。数据库/策略解析和提交参数传递已自动测试，真实后台提交后的设备回放仍需后续实际任务验证。

### 本轮验证

新增 27 项 TopOn 测试，覆盖有效/删除记录、内嵌及外部策略、归属隔离、类型冲突、不以旧备份绕过冲突、权限错误、超时与总预算、备份恢复、缺失格式补齐、标准参数提交映射、目标 ID 严格回放及策略读取数量限制。936 项非 GUI 测试与 222 项 GUI 测试通过（合计 1158 项），release_check.py 通过。
