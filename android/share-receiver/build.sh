#!/bin/sh
set -eu

MODULE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SDK_DIR=${ANDROID_SDK_ROOT:-/Users/a1506/Library/Android/sdk}
BUILD_TOOLS=${ANDROID_BUILD_TOOLS:-$SDK_DIR/build-tools/36.1.0}
PLATFORM_JAR=${ANDROID_PLATFORM_JAR:-$SDK_DIR/platforms/android-36.1/android.jar}
JDK_HOME=${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}
export JAVA_HOME="$JDK_HOME"
OUT_DIR="$MODULE_DIR/build"
CLASSES_DIR="$OUT_DIR/classes"
DEX_DIR="$OUT_DIR/dex"
UNSIGNED_APK="$OUT_DIR/share-receiver-unsigned.apk"
ALIGNED_APK="$OUT_DIR/share-receiver-aligned.apk"
FINAL_APK="$OUT_DIR/apk-tool-share-receiver.apk"
KEYSTORE="$MODULE_DIR/debug.keystore"

rm -rf "$OUT_DIR"
mkdir -p "$CLASSES_DIR" "$DEX_DIR"

"$BUILD_TOOLS/aapt2" link \
  -I "$PLATFORM_JAR" \
  --manifest "$MODULE_DIR/AndroidManifest.xml" \
  --min-sdk-version 26 \
  --target-sdk-version 35 \
  -o "$UNSIGNED_APK"

"$JDK_HOME/bin/javac" \
  -encoding UTF-8 \
  -source 8 -target 8 \
  -classpath "$PLATFORM_JAR" \
  -d "$CLASSES_DIR" \
  "$MODULE_DIR/src/com/apktool/sharereceiver/ShareReceiverActivity.java" \
  "$MODULE_DIR/src/com/apktool/sharereceiver/ShareTextProvider.java"

"$BUILD_TOOLS/d8" \
  --lib "$PLATFORM_JAR" \
  --output "$DEX_DIR" \
  "$CLASSES_DIR/com/apktool/sharereceiver/ShareReceiverActivity.class" \
  "$CLASSES_DIR/com/apktool/sharereceiver/ShareTextProvider.class"

"$JDK_HOME/bin/jar" uf "$UNSIGNED_APK" -C "$DEX_DIR" classes.dex
"$BUILD_TOOLS/zipalign" -f 4 "$UNSIGNED_APK" "$ALIGNED_APK"

if [ ! -f "$KEYSTORE" ]; then
  "$JDK_HOME/bin/keytool" -genkeypair -noprompt \
    -keystore "$KEYSTORE" -storepass android -keypass android \
    -alias androiddebugkey -dname "CN=APK Tool Debug,O=APK Tool,C=CN" \
    -keyalg RSA -keysize 2048 -validity 10000
fi

"$BUILD_TOOLS/apksigner" sign \
  --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$FINAL_APK" "$ALIGNED_APK"
"$BUILD_TOOLS/apksigner" verify --verbose "$FINAL_APK"
printf '%s\n' "$FINAL_APK"
