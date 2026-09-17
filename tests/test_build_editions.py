import os
from pathlib import Path
import runpy

from private_features import ALL_PRIVATE_FEATURES, private_feature_enabled


ROOT = Path(__file__).resolve().parents[1]


def test_standard_runtime_hook_forces_every_private_feature_off(monkeypatch):
    for feature in ALL_PRIVATE_FEATURES:
        env_name = "APK_TOOL_PRIVATE_" + feature.upper()
        monkeypatch.setenv(env_name, "1")

    runpy.run_path(str(ROOT / "runtime_hooks" / "standard_edition.py"))

    assert os.environ["APK_TOOL_BUILD_EDITION"] == "standard"
    assert all(not private_feature_enabled(feature) for feature in ALL_PRIVATE_FEATURES)


def test_internal_runtime_hook_has_a_distinct_build_identity(monkeypatch):
    monkeypatch.delenv("APK_TOOL_BUILD_EDITION", raising=False)

    runpy.run_path(str(ROOT / "runtime_hooks" / "internal_edition.py"))

    assert os.environ["APK_TOOL_BUILD_EDITION"] == "internal"


def test_edition_specs_use_distinct_names_and_bundle_ids():
    standard = (ROOT / "APK Tool Standard.spec").read_text(encoding="utf-8")
    internal = (ROOT / "APK Tool Internal.spec").read_text(encoding="utf-8")

    assert "APK Tool Standard.app" in standard
    assert "com.apktool.desktop.standard" in standard
    assert "APK Tool Internal.app" in internal
    assert "com.apktool.desktop.internal" in internal
