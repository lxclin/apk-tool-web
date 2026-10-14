"""Read-only, package-scoped IronSource/LevelPlay configuration evidence."""
import json
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET

SYMBOLIC_IDS = frozenset({"video", "inter", "reward", "rewarded", "rewarded_video", "interstitial", "external"})


def parse_provider_order(xml_text):
    """Decode XML entities before JSON; never mistake an app key for an ad unit."""
    result = {"插屏聚合id": [], "激励视频聚合id": []}
    try:
        root = ET.fromstring(xml_text)
        response = next(node.text for node in root.findall("string") if node.get("name") == "last_response")
        order = json.loads(response)["response"]["providerOrder"]
        if not isinstance(order, dict):
            return result
        for source, target in (("interstitial", "插屏聚合id"), ("rewarded", "激励视频聚合id")):
            entries = order.get(source, {})
            if not isinstance(entries, dict):
                continue
            for value in entries:
                if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
                        and value.casefold() not in SYMBOLIC_IDS):
                    result[target].append(value)
    except (ET.ParseError, StopIteration, TypeError, KeyError, ValueError):
        pass
    return result


def supplement_family_config(fields, adb, uid, *, runner=subprocess.run):
    """Read only the unique package belonging to the Logcat UID; no device writes."""
    verdict = str(fields.get("最终判断") or "").casefold().replace("_", "")
    if not any(name in verdict for name in ("ironsource", "levelplay")):
        return fields
    names = ("插屏聚合id", "激励视频聚合id")
    def concrete(name):
        return any(v and v.casefold() not in SYMBOLIC_IDS
                   for v in re.split(r"[,，\s]+", str(fields.get(name) or "")))
    if all(concrete(name) for name in names) or not str(uid or "").isdigit():
        return fields
    started = time.monotonic()
    def run(args):
        remaining = 4 - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, 4)
        return runner([adb, *args], capture_output=True, text=True, timeout=min(2, remaining))
    try:
        packages = run(["shell", "pm", "list", "packages", "--uid", str(uid)])
        if packages.returncode:
            return fields
        rows = re.findall(r"^package:([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)(?: uid:(\d+))?[ \t]*$", packages.stdout, re.M)
        matches = [package for package, actual_uid in rows if not actual_uid or actual_uid == str(uid)]
        if len(matches) != 1:
            fields["_mediation_config_read"] = "UID 未唯一对应包名，跳过配置读取"
            return fields
        package = matches[0]
        user_id = int(uid) // 100000
        path = f"/data/user/{user_id}/{package}/shared_prefs/Mediation_Shared_Preferences.xml"
        # head bounds output; run-as supports debug builds, su supports the
        # rooted inspection devices already used by this tool.
        output = run(["shell", "run-as", package, "head", "-c", "2097152", path]) if user_id == 0 else None
        if output is None or output.returncode:
            output = run(["shell", "su", "-c", shlex.quote(f"head -c 2097152 {shlex.quote(path)}")])
        if output.returncode:
            output = run(["shell", "su", "0", "head", "-c", "2097152", path])
        if output.returncode:
            fields["_mediation_config_read"] = "配置不可读，继续使用日志证据"
            return fields
        ids = parse_provider_order(output.stdout)
        for name in names:
            if ids[name] and not concrete(name):
                fields[name] = ", ".join(ids[name])
                fields.setdefault("广告ID来源详情", {})[name] = {
                    "package": package, "path": path,
                    "source": "last_response.response.providerOrder",
                    "platform": "levelplay" if "levelplay" in verdict else "ironsource",
                    "confidence": "配置证据；仍需真实广告回放验证",
                }
        fields["_mediation_config_read"] = "已读取目标包配置"
    except (OSError, subprocess.SubprocessError):
        fields["_mediation_config_read"] = "配置读取超时或失败，继续使用日志证据"
    return fields
