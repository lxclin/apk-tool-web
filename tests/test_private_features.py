import base64
from datetime import datetime, timedelta, timezone
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from private_features import private_feature_enabled, verify_permission_license


MACHINE = "a" * 64


def _license_files(tmp_path, *, machine=MACHINE, expires=None, tamper=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    payload = {
        "version": 1,
        "license_id": "test-license",
        "subject": "test",
        "machine_id": machine,
        "features": ["cp_candidate_assignment"],
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": (expires or datetime(2030, 1, 1, tzinfo=timezone.utc)).isoformat(),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    document = {
        "payload": payload,
        "signature": base64.urlsafe_b64encode(private.sign(canonical)).decode().rstrip("="),
    }
    if tamper:
        document["payload"]["features"].append("backend_submission")
    license_path = tmp_path / "license.json"
    license_path.write_text(json.dumps(document), encoding="utf-8")
    return license_path, public_path


def test_private_feature_is_disabled_without_signed_license(tmp_path):
    assert private_feature_enabled(
        "cp_candidate_assignment",
        license_path=tmp_path / "missing.json",
        public_key_path=tmp_path / "missing-public.pem",
        expected_machine_id=MACHINE,
        environ={},
    ) is False


def test_unsigned_legacy_settings_cannot_enable_feature(tmp_path):
    settings = tmp_path / "features.json"
    settings.write_text('{"cp_candidate_assignment": true}', encoding="utf-8")
    assert private_feature_enabled(
        "cp_candidate_assignment",
        settings_path=str(settings),
        license_path=tmp_path / "missing-license.json",
        public_key_path=tmp_path / "missing-public.pem",
        expected_machine_id=MACHINE,
        environ={},
    ) is False


def test_valid_signed_machine_bound_license_enables_feature(tmp_path):
    license_path, public_path = _license_files(tmp_path)
    assert private_feature_enabled(
        "cp_candidate_assignment",
        license_path=license_path,
        public_key_path=public_path,
        expected_machine_id=MACHINE,
        environ={},
    ) is True


def test_wrong_machine_expired_or_tampered_license_is_rejected(tmp_path):
    valid_license, public_path = _license_files(tmp_path / "wrong", machine="b" * 64)
    assert not verify_permission_license(
        license_path=valid_license,
        public_key_path=public_path,
        expected_machine_id=MACHINE,
    ).valid

    expired_license, expired_public = _license_files(
        tmp_path / "expired",
        expires=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert not verify_permission_license(
        license_path=expired_license,
        public_key_path=expired_public,
        expected_machine_id=MACHINE,
    ).valid

    tampered_license, tampered_public = _license_files(tmp_path / "tampered", tamper=True)
    assert not verify_permission_license(
        license_path=tampered_license,
        public_key_path=tampered_public,
        expected_machine_id=MACHINE,
    ).valid


def test_environment_override_can_force_signed_feature_off(tmp_path):
    license_path, public_path = _license_files(tmp_path)
    assert private_feature_enabled(
        "cp_candidate_assignment",
        license_path=license_path,
        public_key_path=public_path,
        expected_machine_id=MACHINE,
        environ={"APK_TOOL_PRIVATE_CP_CANDIDATE_ASSIGNMENT": "0"},
    ) is False
