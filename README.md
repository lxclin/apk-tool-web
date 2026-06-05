# APK Tool Web

安卓 ADB 适配工具 Web 端 —— 零安装，浏览器即可使用。

## 快速开始

1. 下载 [APK Tool Proxy.dmg](download/APK-Tool-Proxy.dmg)
2. 双击 DMG，把 App 拖入「应用程序」文件夹
3. 双击启动 App（首次需右键 → 打开）
4. 打开本页面，自动连接本地 ADB
5. USB 连接安卓手机，开始使用

## 系统要求

- macOS (Intel / Apple Silicon 均支持)
- Chrome / Edge 浏览器
- 安卓手机 USB 连接 + USB 调试已开启
- **不需要安装 Python、ADB 或任何依赖**

## 功能

- 推送 Config 到设备
- 执行 zygote_build 注入脚本
- Logcat 实时日志监听（按 UID / 广告平台过滤）
- 应用缓存清除、强制停止
- APK 本地安装、URL 跳转、APKPure 搜索

## 故障排除

### App 无法打开
首次启动需右键点击 App →「打开」→ 弹窗中点「打开」

### 提示未找到 ADB
1. 确认终端窗口显示了 "✓ 可用" 字样
2. 若显示 "❌ 未找到可用的 adb"，请在终端执行：
   `brew install android-platform-tools`
3. 若显示 "设备未连接"，请确认 USB 已插好且手机已开启 USB 调试

### 页面显示"未检测到本地代理"
1. 确认 APK Tool Proxy.app 已启动（终端窗口开着）
2. 刷新浏览器页面（Cmd+R）
