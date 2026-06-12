#!/bin/bash
# 构建 ADB 代理 .app + .dmg
# PyInstaller 会强制裁剪 universal binary，构建后用原版替换回来

set -e

# 自动查找 adb 真实路径（跟随符号链接）
ADB_SRC=""
if command -v adb &>/dev/null; then
    ADB_SRC=$(readlink -f "$(which adb)" 2>/dev/null || readlink "$(which adb)")
fi
if [ -z "$ADB_SRC" ] || [ ! -f "$ADB_SRC" ]; then
    ADB_SRC=$(find /opt/homebrew /usr/local -name "adb" -type f -path "*/platform-tools/*" 2>/dev/null | head -1)
fi
if [ -z "$ADB_SRC" ] || [ ! -f "$ADB_SRC" ]; then
    echo "错误: 找不到 ADB 二进制文件。请先执行 brew install android-platform-tools"
    exit 1
fi
echo "ADB 源文件: $ADB_SRC ($(lipo -info "$ADB_SRC" 2>/dev/null || file "$ADB_SRC"))"
DIST_DIR="dist"
PROXY_DIR="$DIST_DIR/APK Tool Proxy"
APP_DIR="$DIST_DIR/APK Tool Proxy.app"

echo "=== 1. PyInstaller 构建 ==="
rm -rf "$PROXY_DIR" "$APP_DIR"
python3 -m PyInstaller "ADB Proxy.spec" --clean

echo ""
echo "=== 2. 替换 universal ADB（PyInstaller 裁剪了 x86_64）==="
# COLLECT 目录
cp -f "$ADB_SRC" "$PROXY_DIR/_internal/adb/adb"
chmod +x "$PROXY_DIR/_internal/adb/adb"
# .app bundle
cp -f "$ADB_SRC" "$APP_DIR/Contents/Frameworks/adb/adb"
chmod +x "$APP_DIR/Contents/Frameworks/adb/adb"
# 重新签名 .app（修改内容后必须重签）
codesign --force --sign - "$APP_DIR" 2>/dev/null || true

echo ""
echo "=== 3. 验证架构 ==="
echo "COLLECT: $(lipo -info "$PROXY_DIR/_internal/adb/adb")"
echo ".app:    $(lipo -info "$APP_DIR/Contents/Frameworks/adb/adb")"

echo ""
echo "=== 4. 创建 DMG ==="
rm -f "$DIST_DIR/APK-Tool-Proxy.dmg"
hdiutil create -volname "APK Tool Proxy" \
  -srcfolder "$APP_DIR" \
  -ov -format UDZO \
  "$DIST_DIR/APK-Tool-Proxy.dmg"

echo ""
echo "=== 完成 ==="
ls -lh "$DIST_DIR/APK-Tool-Proxy.dmg" "$APP_DIR"
echo ""
echo "测试: open $APP_DIR"
