# -*- mode: python ; coding: utf-8 -*-

import sys
import os

PROJECT_DIR = SPECPATH  # PyInstaller 变量，指向 spec 所在目录

a = Analysis(
    ['main_web.py'],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=[
        (os.path.join(SPECPATH, 'static', 'index.html'), 'static'),
        (os.path.join(SPECPATH, 'ip_whitelist.json'), '.'),
        (os.path.join(SPECPATH, 'permission_public_key.pem'), '.'),
    ],
    hiddenimports=[
        # FastAPI 依赖
        'fastapi',
        'starlette',
        'uvicorn',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # WebSocket
        'websockets',
        'websockets.legacy',
        # 请求解析
        'python_multipart',
        'multipart',
        'multipart.multipart',
        # ADB 模块
        'adb_pusher',
        'qr_generator',
        'server',
        'web_precheck',
        'auto_asana.main',
        'asana',
        'asana.api_client',
        'asana.configuration',
        'asana.api.sections_api',
        'asana.api.stories_api',
        'asana.api.tasks_api',
        # 其他依赖
        'qrcode',
        'PIL',
        'PIL.Image',
        'asyncio',
        'json',
        're',
        'queue',
        'threading',
        'subprocess',
        'tempfile',
        'shutil',
        'pathlib',
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
    name='APK Tool Web',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,            # Web 版保留控制台，方便看日志
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
    upx_exclude=[],
    name='APK Tool Web',
)

app = BUNDLE(
    coll,
    name='APK Tool Web.app',
    icon=None,
    bundle_identifier='com.apktool.web',
)
