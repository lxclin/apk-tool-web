import json

from private_features import private_feature_enabled


def test_private_feature_is_disabled_without_local_settings(tmp_path):
    assert private_feature_enabled(
        "cp_candidate_assignment",
        settings_path=str(tmp_path / "missing.json"),
        environ={},
    ) is False


def test_private_feature_can_be_enabled_for_one_machine(tmp_path):
    settings_path = tmp_path / "features.json"
    settings_path.write_text(
        json.dumps({"cp_candidate_assignment": True}), encoding="utf-8"
    )
    assert private_feature_enabled(
        "cp_candidate_assignment",
        settings_path=str(settings_path),
        environ={},
    ) is True


def test_environment_override_can_force_private_feature_off(tmp_path):
    settings_path = tmp_path / "features.json"
    settings_path.write_text(
        json.dumps({"cp_candidate_assignment": True}), encoding="utf-8"
    )
    assert private_feature_enabled(
        "cp_candidate_assignment",
        settings_path=str(settings_path),
        environ={"APK_TOOL_PRIVATE_CP_CANDIDATE_ASSIGNMENT": "0"},
    ) is False
