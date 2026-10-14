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
