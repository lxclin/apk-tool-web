"""Signed, machine-bound permissions for owner-only APK Tool features."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from typing import Any, Iterable, Mapping
import uuid


APP_DIR = Path(__file__).resolve().parent
PUBLIC_KEY_PATH = APP_DIR / "permission_public_key.pem"
LICENSE_PATH = Path("~/.apk-tool/license.json").expanduser()
OWNER_PRIVATE_KEY_PATH = Path("~/.apk-tool-owner/ed25519-private.pem").expanduser()
PRIVATE_FEATURES_PATH = os.path.expanduser("~/.apk-tool-private-features.json")

FEATURE_LABELS = {
    "cp_candidate_assignment": "候选 CP 自动指派",
    "backend_submission": "适配后台自动提交",
    "batch_automation": "批量自动化适配",
    "asana_write": "Asana 自动写入",
    "bulk_device_cleanup": "批量清理设备应用",
}
ALL_PRIVATE_FEATURES = tuple(FEATURE_LABELS)


@dataclass(frozen=True)
class PermissionStatus:
    valid: bool
    features: frozenset[str]
    reason: str
    subject: str = ""
    expires_at: str = ""
    machine_id: str = ""

    def allows(self, feature_name: str) -> bool:
        return self.valid and feature_name in self.features


class PermissionDeniedError(PermissionError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _mac_platform_uuid() -> str:
    if platform.system() != "Darwin":
        return ""
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        for line in result.stdout.splitlines():
            if "IOPlatformUUID" in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def machine_id() -> str:
    raw = _mac_platform_uuid() or f"{platform.node()}:{uuid.getnode()}"
    return hashlib.sha256(("apk-tool-v1:" + raw).encode()).hexdigest()


def machine_code(value: str | None = None) -> str:
    digest = (value or machine_id()).upper()
    return "-".join(digest[index:index + 4] for index in range(0, 20, 4))


def _load_crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise RuntimeError("缺少权限验证组件 cryptography") from exc
    return serialization, Ed25519PrivateKey


def verify_permission_license(
    *, license_path: str | os.PathLike[str] = LICENSE_PATH,
    public_key_path: str | os.PathLike[str] = PUBLIC_KEY_PATH,
    expected_machine_id: str | None = None,
    now: datetime | None = None,
) -> PermissionStatus:
    expected = expected_machine_id or machine_id()
    try:
        document = json.loads(Path(license_path).expanduser().read_text("utf-8"))
        payload = document["payload"]
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        serialization, _ = _load_crypto()
        public_key = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
        public_key.verify(_b64decode(str(document["signature"])), _canonical_json(payload))
    except FileNotFoundError:
        return PermissionStatus(False, frozenset(), "未安装授权文件", machine_id=expected)
    except Exception:
        return PermissionStatus(False, frozenset(), "授权文件签名无效", machine_id=expected)
    if int(payload.get("version", 0)) != 1:
        return PermissionStatus(False, frozenset(), "授权版本不受支持", machine_id=expected)
    if str(payload.get("machine_id") or "") != expected:
        return PermissionStatus(False, frozenset(), "授权文件不属于当前设备", machine_id=expected)
    expires_at = str(payload.get("expires_at") or "")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if expiry <= current:
            return PermissionStatus(False, frozenset(), "授权已过期", expires_at=expires_at, machine_id=expected)
    except (TypeError, ValueError):
        return PermissionStatus(False, frozenset(), "授权有效期无效", machine_id=expected)
    features = frozenset(
        feature for feature in payload.get("features", []) if feature in FEATURE_LABELS
    )
    return PermissionStatus(
        True, features, "授权有效", str(payload.get("subject") or ""), expires_at, expected
    )


def private_feature_enabled(
    feature_name: str, *, settings_path: str = PRIVATE_FEATURES_PATH,
    environ: Mapping[str, str] | None = None,
    license_path: str | os.PathLike[str] = LICENSE_PATH,
    public_key_path: str | os.PathLike[str] = PUBLIC_KEY_PATH,
    expected_machine_id: str | None = None, now: datetime | None = None,
) -> bool:
    """Unsigned settings cannot grant access; environment can only force off."""
    del settings_path
    feature_name = str(feature_name or "").strip()
    if feature_name not in FEATURE_LABELS:
        return False
    env = os.environ if environ is None else environ
    env_name = "APK_TOOL_PRIVATE_" + "".join(
        char if char.isalnum() else "_" for char in feature_name.upper()
    )
    if str(env.get(env_name, "")).strip().casefold() in {"0", "false", "no", "off"}:
        return False
    return verify_permission_license(
        license_path=license_path, public_key_path=public_key_path,
        expected_machine_id=expected_machine_id, now=now,
    ).allows(feature_name)


def require_private_feature(feature_name: str, **kwargs) -> None:
    if private_feature_enabled(feature_name, **kwargs):
        return
    label = FEATURE_LABELS.get(feature_name, feature_name)
    status = verify_permission_license()
    raise PermissionDeniedError(
        f"当前设备无权使用“{label}”（{status.reason}，设备码 {machine_code()}）"
    )


def owner_identity_available(
    *, private_key_path: str | os.PathLike[str] = OWNER_PRIVATE_KEY_PATH,
    public_key_path: str | os.PathLike[str] = PUBLIC_KEY_PATH,
) -> bool:
    try:
        serialization, _ = _load_crypto()
        private_key = serialization.load_pem_private_key(
            Path(private_key_path).expanduser().read_bytes(), password=None
        )
        expected = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
        encoding, public_format = serialization.Encoding.Raw, serialization.PublicFormat.Raw
        return private_key.public_key().public_bytes(encoding, public_format) == expected.public_bytes(encoding, public_format)
    except Exception:
        return False


def issue_permission_license(
    target_machine_id: str, features: Iterable[str], *, subject: str,
    expires_at: datetime, output_path: str | os.PathLike[str],
    private_key_path: str | os.PathLike[str] = OWNER_PRIVATE_KEY_PATH,
) -> Path:
    if not owner_identity_available(private_key_path=private_key_path):
        raise PermissionDeniedError("当前电脑没有匹配的所有者签名密钥")
    target = str(target_machine_id or "").strip().lower()
    if len(target) != 64 or any(char not in "0123456789abcdef" for char in target):
        raise ValueError("目标设备 ID 必须是 64 位十六进制字符")
    granted = sorted(set(features).intersection(FEATURE_LABELS))
    if not granted:
        raise ValueError("请至少选择一个授权功能")
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utc_now():
        raise ValueError("授权到期时间必须晚于当前时间")
    serialization, _ = _load_crypto()
    private_key = serialization.load_pem_private_key(
        Path(private_key_path).expanduser().read_bytes(), password=None
    )
    payload = {
        "version": 1, "license_id": str(uuid.uuid4()),
        "subject": str(subject or "授权设备").strip(), "machine_id": target,
        "features": granted,
        "issued_at": _utc_now().isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    document = {"payload": payload, "signature": _b64encode(private_key.sign(_canonical_json(payload)))}
    destination = Path(output_path).expanduser()
    _atomic_json_write(destination, document)
    return destination


def install_permission_license(
    source_path: str | os.PathLike[str], *,
    destination_path: str | os.PathLike[str] = LICENSE_PATH,
) -> PermissionStatus:
    source = Path(source_path).expanduser()
    status = verify_permission_license(license_path=source)
    if not status.valid:
        raise PermissionDeniedError(status.reason)
    destination = Path(destination_path).expanduser()
    _atomic_json_write(destination, json.loads(source.read_text("utf-8")))
    return verify_permission_license(license_path=destination)


def _atomic_json_write(destination: Path, document: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".license-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(document, file, ensure_ascii=False, indent=2)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
