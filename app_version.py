"""Single source of truth for desktop, Web and packaged build identity."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys


APP_NAME = "APK Tool"
APP_VERSION = "1.4.0"
CHECKPOINT_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
WORKFLOW_SCHEMA_VERSION = 1


def get_build_info() -> dict:
    frozen = bool(getattr(sys, "frozen", False))
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "runtime": "packaged" if frozen else "source",
        "channel": os.environ.get("APK_TOOL_BUILD_CHANNEL", "local"),
        "commit": os.environ.get("APK_TOOL_BUILD_COMMIT", "").strip(),
        "built_at": os.environ.get("APK_TOOL_BUILD_TIME", "").strip(),
        "python": sys.version.split()[0],
        "schemas": {
            "checkpoint": CHECKPOINT_SCHEMA_VERSION,
            "report": REPORT_SCHEMA_VERSION,
            "workflow": WORKFLOW_SCHEMA_VERSION,
        },
    }


def build_label() -> str:
    info = get_build_info()
    label = f"{info['name']} v{info['version']} · {info['runtime']}"
    if info["commit"]:
        label += f" · {info['commit'][:8]}"
    return label


def release_environment() -> dict:
    """Return environment values a release job can inject into PyInstaller."""
    return {
        "APK_TOOL_BUILD_CHANNEL": "release",
        "APK_TOOL_BUILD_TIME": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
