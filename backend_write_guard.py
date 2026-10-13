"""Local snapshots and conservative merge rules for CP backend writes."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import uuid
from adb_pusher import normalize_optional_parameter

ROOT = Path(__file__).resolve().parent / 'backend_submission_history'
FIELDS = ('package_name', 'aggr_platform', 'attribution_platform', 'aggr_chaping_id',
          'aggr_jilishipin_id', 'ps', 'block_ps', 'af_key', 'manual_applovin_sdk_key',
          'activity_main_page', 'activity_guide_page', 'user_name')


def text(value):
    return '' if value is None else str(value).strip()


class BackendWriteJournal:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else ROOT

    def folder(self, endpoint, package):
        key = hashlib.sha256((endpoint + '\n' + package).encode()).hexdigest()
        return self.root / key

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def owned_note(self, endpoint, package):
        # A prepared/unverified request never establishes ownership.
        folder = self.folder(endpoint, package)
        for path in sorted(folder.glob('*.json'), reverse=True):
            data = json.loads(path.read_text())
            if data.get('verified') or data.get('ownership_authorized'):
                return text(data.get('owned_note'))
        return ''

    def authorize_note_removal(self, endpoint, package, note, reason):
        """Record an explicit operator authorization for a legacy tool note."""
        now = datetime.now(timezone.utc).isoformat()
        path = self.folder(endpoint, package) / (now.replace(':', '-') + '_' + uuid.uuid4().hex + '.json')
        self.write(path, {'created_at': now, 'package_name': package, 'endpoint': endpoint,
                         'owned_note': note, 'ownership_authorized': True,
                         'verified': False, 'authorization': reason})
        return path

    def prepare(self, endpoint, before, payload, *, owned_note=''):
        now = datetime.now(timezone.utc).isoformat()
        path = self.folder(endpoint, payload['package_name']) / (now.replace(':', '-') + '_' + uuid.uuid4().hex + '.json')
        data = {'created_at': now, 'endpoint': endpoint, 'package_name': payload['package_name'],
                'before': {k: before.get(k) for k in FIELDS},
                'submitted': {k: payload.get(k) for k in FIELDS},
                'owned_note': owned_note, 'verified': False}
        self.write(path, data)
        return path

    def confirm(self, path, record):
        data = json.loads(path.read_text())
        data.update(verified=True, after={k: record.get(k) for k in FIELDS})
        self.write(path, data)


def merge_backend_payload(before, proposed, owned_note='', *, terminal_note=None):
    """Never infer note ownership from wording; stop rather than erase old IDs."""
    if text(before.get('block_ps')):
        raise ValueError('后台已有 Block 原因，保留原记录，需人工确认后再适配')
    payload = dict(proposed)
    existing = text(before.get('ps'))
    # Only an exact standalone note line recorded in a verified receipt may go.
    lines = existing.splitlines()
    owned_lines = text(owned_note).splitlines()
    if owned_lines:
        for index in range(len(lines) - len(owned_lines), -1, -1):
            if lines[index:index + len(owned_lines)] == owned_lines:
                del lines[index:index + len(owned_lines)]
                break
    remaining = '\n'.join(lines).strip()
    if terminal_note is not None:
        new_note = text(terminal_note)
        payload['ps'] = (remaining if ('\n' + new_note + '\n') in ('\n' + remaining + '\n')
                         else '\n'.join(v for v in (remaining, new_note) if v)) or None
    else:
        payload['ps'] = remaining or None
        missing = [k for k in ('aggr_chaping_id', 'aggr_jilishipin_id', 'af_key',
                               'manual_applovin_sdk_key')
                   if normalize_optional_parameter(before.get(k))
                   and not normalize_optional_parameter(payload.get(k))]
        if missing:
            raise ValueError('本次缺少后台已有参数，停止覆盖：' + ', '.join(missing))
        # This field is not detected by this tool; retain its existing value.
        payload['activity_guide_page'] = before.get('activity_guide_page')
    payload['block_ps'] = before.get('block_ps')
    return payload
