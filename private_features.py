"""Local-only feature switches that default to disabled for distributed builds."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping


PRIVATE_FEATURES_PATH = os.path.expanduser("~/.apk-tool-private-features.json")
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def private_feature_enabled(
    feature_name: str,
    *,
    settings_path: str = PRIVATE_FEATURES_PATH,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Read a per-machine switch outside the repository; default to off."""
    feature_name = str(feature_name or "").strip()
    if not feature_name:
        return False

    env = os.environ if environ is None else environ
    env_name = "APK_TOOL_PRIVATE_" + "".join(
        char if char.isalnum() else "_" for char in feature_name.upper()
    )
    env_value = str(env.get(env_name, "")).strip().casefold()
    if env_value:
        return env_value in _TRUE_VALUES

    try:
        with open(os.path.expanduser(settings_path), "r", encoding="utf-8") as file:
            settings: Any = json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return bool(settings.get(feature_name)) if isinstance(settings, dict) else False
