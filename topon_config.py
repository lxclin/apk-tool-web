"""Read-only TopOn/Thinkup strategy extraction; never copy network unit IDs.

FastKV layout reference: https://github.com/BillyWei01/FastKV/tree/main/fastkv/src/main/java/io/fastkv
Only unencrypted, checksum-valid snapshots and live string records are accepted.
"""
import json
import re
import shlex
import sqlite3
import struct
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

FIELDS = {"1": "激励视频聚合id", "3": "插屏聚合id"}
MAX_BYTES = 4 * 1024 * 1024


class TopOnConflictError(ValueError):
    """Conflicting live strategies must not be bypassed using an older backup."""


def fastkv_strings(data):
    """Return active string values as key -> (external, bytes), failing closed."""
    if len(data) < 12:
        raise ValueError("FastKV header missing")
    length, expected = struct.unpack_from("<iQ", data)
    if not 0 <= length <= min(len(data) - 12, MAX_BYTES):
        raise ValueError("FastKV size/encryption unsupported")
    payload = data[12:12 + length]
    checksum = 0
    for offset in range(0, length, 8):
        checksum ^= int.from_bytes(payload[offset:offset + 8].ljust(8, b"\0"), "little")
    checksum = ((checksum << 32) | (checksum >> 32)) & ((1 << 64) - 1)
    if checksum != expected:
        raise ValueError("FastKV checksum mismatch")
    cursor, result = 0, {}
    def take(size):
        nonlocal cursor
        if size < 0 or cursor + size > length:
            raise ValueError("FastKV truncated record")
        value = payload[cursor:cursor + size]
        cursor += size
        return value
    while cursor < length:
        flags, key_size = take(2)
        kind = flags & 0x3f
        if not key_size or kind not in range(1, 12):
            raise ValueError("FastKV invalid record")
        key_bytes = take(key_size)
        if kind <= 5:
            size = (0, 1, 4, 4, 8, 8)[kind]
        else:
            size = int.from_bytes(take(4 if kind >= 9 else 2), "little")
        value = take(size)
        if flags & 0x80:
            continue  # deleted historical record, not an active reference
        if kind in {6, 9}:
            key = key_bytes.decode("utf-8")
            external = bool(flags & 0x40)
            if external and not re.fullmatch(rb"[a-f0-9]{32}", value):
                raise ValueError("FastKV invalid external filename")
            result[key] = (external, value)
    return result


def topon_database_scope(data):
    """Inspect a local immutable copy, without opening or changing device DBs."""
    if len(data) > MAX_BYTES or not data.startswith(b"SQLite format 3\0"):
        raise ValueError("TopOn database invalid")
    with tempfile.TemporaryDirectory(prefix="apk-topon-") as directory:
        path = Path(directory) / "snapshot.db"
        path.write_bytes(data)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            # AP_SY binds strategy keys to a TopOn app, not an SDK/ad-source key.
            apps = {str(row[0]) for row in connection.execute(
                "SELECT DISTINCT key FROM sdkconfig WHERE type='AP_SY' LIMIT 16")
                if re.fullmatch(r"[A-Za-z0-9]{1,128}", str(row[0]))}
    return apps


def extract_topon_strategies(database, index, read_external):
    """Resolve live *_PL_SY strategy keys and use root format, never unit_id."""
    apps = topon_database_scope(database)
    records = fastkv_strings(index)
    candidates = {}
    seen_units = set()
    for key, (external, value) in records.items():
        matches = [key[len(app):-6] for app in apps
                   if key.startswith(app) and key.endswith("_PL_SY")
                   and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{5,127}", key[len(app):-6])]
        if len(matches) != 1:
            continue
        unit = matches[0]
        seen_units.add(unit)
        if len(seen_units) > 16:
            raise ValueError("TopOn strategy limit exceeded")
        raw = read_external(value.decode("ascii")) if external else value
        if not raw or len(raw) > 1024 * 1024:
            continue
        try:
            strategy = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(strategy, dict):
            continue
        # Require an actual waterfall/bidding strategy, not an arbitrary JSON.
        if not any(isinstance(strategy.get(name), list) and strategy[name]
                   for name in ("ug_list", "hb_list", "s2shb_list", "adx_list")):
            continue
        field = FIELDS.get(str(strategy.get("format")))
        if field:
            candidates.setdefault(unit, set()).add(field)
    result = {name: [] for name in FIELDS.values()}
    for unit, formats in candidates.items():
        if len(formats) != 1:
            raise TopOnConflictError("TopOn placement has conflicting formats")
        result[next(iter(formats))].append(unit)
    return result


def supplement_topon_config(fields, adb, uid, *, runner=subprocess.run):
    """Bound all device reads to eight seconds and a uniquely matched UID."""
    verdict = str(fields.get("最终判断") or "").casefold()
    if not any(name in verdict for name in ("topon", "anythink")):
        return fields
    if all(fields.get(name) for name in FIELDS.values()) or not str(uid or "").isdigit():
        return fields
    started = time.monotonic()
    def run(args, *, binary=False):
        remaining = 8 - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, 8)
        return runner([adb, *args], capture_output=True, text=not binary,
                      timeout=min(2, remaining))
    try:
        output = run(["shell", "pm", "list", "packages", "--uid", str(uid)])
        if output.returncode:
            raise ValueError("UID 查询失败")
        rows = re.findall(r"^package:([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)(?: uid:(\d+))?[ \t]*$", output.stdout, re.M)
        packages = [name for name, actual in rows if not actual or actual == str(uid)]
        if len(packages) != 1:
            raise ValueError("UID 未唯一对应包名")
        package = packages[0]
        base = f"/data/user/{int(uid) // 100000}/{package}/"
        mode = None
        def read(relative, limit=MAX_BYTES):
            nonlocal mode
            path = base + relative
            modes = [mode] if mode else ["run-as", "su-c", "su-0"]
            for candidate in modes:
                if candidate == "run-as":
                    if int(uid) >= 100000:
                        continue
                    args = ["exec-out", "run-as", package, "head", "-c", str(limit), path]
                elif candidate == "su-c":
                    args = ["exec-out", "su", "-c", shlex.quote(f"head -c {limit} {shlex.quote(path)}")]
                else:
                    args = ["exec-out", "su", "0", "head", "-c", str(limit), path]
                response = run(args, binary=True)
                readable = (response.returncode == 0 and response.stdout
                            and not response.stdout.startswith((b"head:", b"su:", b"run-as:", b"/system/bin/sh:")))
                if relative.endswith(".db"):
                    readable = readable and response.stdout.startswith(b"SQLite format 3\0")
                if readable:
                    mode = candidate
                    return response.stdout
            return b""
        database = read("databases/thinkup.db")
        if not database:
            raise ValueError("TopOn 数据库不可读或不存在")
        index_path = "files/tktp/thinkup_place_data_tkv"
        external_cache = {}
        def external(filename):
            if filename not in external_cache:
                external_cache[filename] = read(index_path + "/" + filename, 1024 * 1024)
            return external_cache[filename]
        ids = None
        for suffix in (".kva", ".kvb"):
            snapshot = read(index_path + suffix, 2 * 1024 * 1024)
            try:
                fastkv_strings(snapshot)
            except ValueError:
                continue
            # Only a corrupt binary snapshot allows backup fallback. Valid
            # strategies with conflicts/limits must not use an older backup.
            ids = extract_topon_strategies(database, snapshot, external)
            break
        if ids is None:
            raise ValueError("TopOn 策略索引损坏、加密或格式不支持")
        applied = False
        for name, values in ids.items():
            if values and not fields.get(name):
                fields[name] = ", ".join(values)
                fields.setdefault("广告ID来源详情", {})[name] = {
                    "platform": "topon", "package": package,
                    "path": base + index_path, "source": "AP_SY + live PL_SY + root format",
                    "confidence": "高；仍需目标广告位真实回放验证",
                }
                applied = True
        if applied:
            fields["广告ID识别来源"] = "topon_config_strategy"
            fields["广告ID识别置信度"] = "高"
        fields["_topon_config_read"] = "已读取有效策略" if applied else "有效策略中未取得缺失广告类型"
    except (OSError, subprocess.SubprocessError, ValueError, sqlite3.Error) as error:
        if isinstance(error, TopOnConflictError):
            fields["_aggregation_evidence_conflict"] = "TopOn 有效策略对同一广告位给出不同广告类型，需复核"
        fields["_topon_config_read"] = "配置补查未完成：" + (
            str(error) if isinstance(error, ValueError) else type(error).__name__)
    return fields
