from pathlib import Path

from app_version import APP_VERSION, build_label, get_build_info
from release_check import release_check


def test_version_and_runtime_are_visible():
    info = get_build_info()
    assert info["version"] == APP_VERSION
    assert info["runtime"] in {"source", "packaged"}
    assert info["edition"] in {"source", "standard", "internal"}
    assert APP_VERSION in build_label()


def test_edition_is_visible_in_build_label(monkeypatch):
    monkeypatch.setenv("APK_TOOL_BUILD_EDITION", "standard")
    assert "标准版" in build_label()
    monkeypatch.setenv("APK_TOOL_BUILD_EDITION", "internal")
    assert "内部版" in build_label()


def test_release_manifest_matches_code():
    root = Path(__file__).resolve().parents[1]
    result = release_check(root)
    assert result["ok"], result["errors"]
