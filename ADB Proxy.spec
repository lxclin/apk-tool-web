# -*- mode: python ; coding: utf-8 -*-

import os

ADB_SRC = "/opt/homebrew/Caskroom/android-platform-tools/37.0.0/platform-tools/adb"

a = Analysis(
    ['adb_proxy.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 用 datas 而非 binaries，避免 PyInstaller 裁剪架构为 arm64-only
        (ADB_SRC, 'adb'),
        ('permission_public_key.pem', '.'),
    ],
    hiddenimports=[
        'websockets',
        'asyncio',
        'queue',
        'threading',
        'subprocess',
        'json',
        're',
        'shutil',
        'pathlib',
        'web_precheck',
        'auto_asana.main',
        'asana',
        'asana.api_client',
        'asana.configuration',
        'asana.api.sections_api',
        'asana.api.stories_api',
        'asana.api.tasks_api',
        'cryptography',
        'cryptography.hazmat.primitives.asymmetric.ed25519',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='APK Tool Proxy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['adb*'],  # 不要压缩 adb，保留 universal 架构
    name='APK Tool Proxy',
)

app = BUNDLE(
    coll,
    name='APK Tool Proxy.app',
    icon=None,
    bundle_identifier='com.apktool.proxy',
)
