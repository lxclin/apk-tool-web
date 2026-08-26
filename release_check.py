"""Small deterministic preflight used before producing desktop/Web builds."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from app_version import APP_VERSION, get_build_info


REQUIRED_FILES = (
    "main.py", "main_web.py", "gui.py", "server.py", "static/index.html",
    "APK Tool.spec", "APK Tool Web.spec", "android/share-receiver/build/apk-tool-share-receiver.apk",
)


def release_check(root: Path | None = None) -> dict:
    root = (root or Path(__file__).resolve().parent).resolve()
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    manifest_path = root / "release_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    if manifest.get("version") != APP_VERSION:
        errors.append(
            f"release_manifest.json={manifest.get('version')}，代码={APP_VERSION}"
        )
    if missing:
        errors.append("缺少发布文件: " + ", ".join(missing))
    return {
        "ok": not errors,
        "version": APP_VERSION,
        "build": get_build_info(),
        "errors": errors,
    }


if __name__ == "__main__":
    result = release_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
