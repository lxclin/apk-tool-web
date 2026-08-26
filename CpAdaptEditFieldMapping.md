# 待适配CP信息表-适配：修改页面字段映射

## URL自动填写

当进入“待适配CP信息表-适配”页面的 URL query 中携带 `change=1` 时，页面会解析 URL 参数并自动打开“修改适配信息”弹窗。

必须携带 `package_name`，页面会优先用当前表格中该包名对应的数据作为默认值，再用 URL query 中同名字段覆盖弹窗内容。

示例：

```text
/#/CpAdaptManage/CpAdapt?change=1&package_name=com.demo.game&aggr_platform=admob&aggr_chaping_id=xxx&aggr_jilishipin_id=yyy
```

## 字段对应关系

“修改适配信息”弹窗点击“提交”后，请求接口：

```text
POST /admin/gd_web/overseas/s10_package_info
```

页面显示字段、URL query 字段、API 请求字段对应关系如下：

| 修改页面显示字段 | URL query字段 | API请求字段 | 说明 |
| --- | --- | --- | --- |
| 包名 | `package_name` | `package_name` | 必填；URL 自动打开修改弹窗时必须携带 |
| 聚合平台 | `aggr_platform` | `aggr_platform` | 下拉选择；TradPlus 使用值 `tradplus`，其他可选值见页面 `select_platform_list` |
| 归因平台 | `attribution_platform` | `attribution_platform` | 文本输入 |
| 聚合id-插屏 | `aggr_chaping_id` | `aggr_chaping_id` | 文本输入 |
| 聚合id-激励视频 | `aggr_jilishipin_id` | `aggr_jilishipin_id` | 文本输入 |
| 自定义applovin_sdk_key | `manual_applovin_sdk_key` | `manual_applovin_sdk_key` | 文本输入 |
| activity初始页面 | `activity_main_page` | `activity_main_page` | 文本输入 |
| activity引导页面 | `activity_guide_page` | `activity_guide_page` | 文本输入 |
| af_key | `af_key` | `af_key` | 文本输入；后端检测到变更时会同步调用 UP2 设置接口 |
| Block原因 | `block_ps` | `block_ps` | 文本输入 |
| 备注 | `ps` | `ps` | 文本输入 |

提交时页面还会额外传递：

| API请求字段 | 来源 | 说明 |
| --- | --- | --- |
| `user_name` | 当前登录用户 `store.getters.name` | 用于后端日志记录 |

## 独立操作字段

以下字段不属于“修改适配信息”弹窗提交内容，它们通过独立交互单独提交：

| 页面字段/操作 | API请求字段 | 说明 |
| --- | --- | --- |
| 适配人员 | `assign` | 选择下拉框后立即提交 |
| A2是否开启 | `a2_open` | 切换开关后立即提交；未选择适配人员时会提示并恢复开关状态 |
| 清除缓存 | `package_name` | 调用清除 A2 包缓存接口 |
