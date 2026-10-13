from pathlib import Path
from unittest.mock import MagicMock
import json
import pytest
import requests
from backend_write_guard import BackendWriteJournal, merge_backend_payload
from automation_adaptation import (build_backend_submission_payload, clear_backend_adaptation_via_api,
                                  derive_backend_submit_url, submit_backend_via_api)

FIELDS = {'最终判断': 'TradPlus聚合（日志与配置综合分析确认）', '归因平台': 'Adjust',
          '初始Activity': 'com.example.Main', '插屏聚合id': 'tp-inter'}
URL = 'https://backend.test/cp_adapt/list'


class Backend:
    def __init__(self, before):
        self.record = dict(before)
        self.writes = []
        self.session = MagicMock()
        self.session.post.side_effect = self.post
        self.session.get.return_value.json.return_value = {'code': 200, 'message': 'success', 'data': 1}

    def post(self, url, **kwargs):
        response = MagicMock()
        if url.endswith('/cp_adapt/list'):
            body = {'code': 200, 'data': {'data': [dict(self.record)], 'total': 1}}
        else:
            self.writes.append(dict(kwargs['json']))
            self.record.update(kwargs['json'])
            body = {'code': 200, 'data': dict(self.record)}
        response.json.return_value = body
        return response


def submit(backend, journal, **kwargs):
    return submit_backend_via_api(FIELDS, 'com.example', api_url=URL, x_token='secret-x',
                                 token='secret-token', session=backend.session, journal=journal,
                                 readback_delay_seconds=0, **kwargs)


@pytest.fixture
def journal(tmp_path):
    return BackendWriteJournal(tmp_path)


def before(**kwargs):
    return {**build_backend_submission_payload(FIELDS, 'com.example'), **kwargs}


def test_manual_note_and_guide_activity_survive_submission(journal):
    backend = Backend(before(ps='人工备注：待商务确认', activity_guide_page='com.example.Guide'))
    result = submit(backend, journal)
    assert result['ok'] and result['device_verification'] == 'pending_replay'
    assert backend.record['ps'] == '人工备注：待商务确认'
    assert backend.record['activity_guide_page'] == 'com.example.Guide'
    receipt = json.loads(Path(result['snapshot_path']).read_text())
    assert receipt['before']['ps'] == backend.record['ps']
    assert receipt['verified']
    assert 'secret-x' not in Path(result['snapshot_path']).read_text()
    assert 'secret-token' not in Path(result['snapshot_path']).read_text()


def test_text_looking_like_tool_note_without_receipt_is_preserved(journal):
    backend = Backend(before(ps='TradPlus聚合，暂不适配'))
    assert submit(backend, journal)['ok']
    assert backend.record['ps'] == 'TradPlus聚合，暂不适配'


def test_verified_tool_note_is_removed_but_manual_lines_remain(journal):
    note = 'TradPlus聚合，暂不适配'
    old = before(ps=note)
    path = journal.prepare(derive_backend_submit_url(URL), old, old, owned_note=note)
    journal.confirm(path, old)
    backend = Backend(before(ps='人工备注\n'+note))
    assert submit(backend, journal)['ok']
    assert backend.record['ps'] == '人工备注'


def test_unverified_snapshot_does_not_establish_note_ownership(journal):
    note = 'TradPlus聚合，暂不适配'
    old = before(ps=note)
    journal.prepare(derive_backend_submit_url(URL), old, old, owned_note=note)
    backend = Backend(old)
    assert submit(backend, journal)['ok']
    assert backend.record['ps'] == note


def test_existing_block_reason_prevents_any_write(journal):
    backend = Backend(before(ps='备注', block_ps='应用内购，加黑'))
    result = submit(backend, journal)
    assert result['code'] == 'BACKEND_PROTECTION_FAILED'
    assert backend.writes == []
    assert backend.record['block_ps'] == '应用内购，加黑'
    backend.session.get.assert_not_called()


def test_missing_previous_ad_unit_prevents_erasure(journal):
    backend = Backend(before(aggr_jilishipin_id='old-reward'))
    result = submit(backend, journal)
    assert result['code'] == 'BACKEND_PROTECTION_FAILED'
    assert 'aggr_jilishipin_id' in result['message']
    assert backend.writes == []


def test_failed_lookup_prevents_write(journal):
    backend = Backend(before())
    backend.session.post.side_effect = requests.Timeout('read timeout')
    assert submit(backend, journal)['code'] == 'BACKEND_PROTECTION_FAILED'
    assert backend.writes == []


def test_failed_snapshot_prevents_write(journal, monkeypatch):
    backend = Backend(before())
    monkeypatch.setattr(journal, 'prepare', MagicMock(side_effect=OSError('disk full')))
    assert submit(backend, journal)['code'] == 'BACKEND_PROTECTION_FAILED'
    assert backend.writes == []


def test_terminal_note_preserves_manual_note_then_is_removed_on_success(journal):
    backend = Backend(before(ps='人工备注'))
    note = '未识别出聚合类型，暂不适配'
    result = clear_backend_adaptation_via_api('com.example', api_url=URL, x_token='x', token='token',
            note=note, session=backend.session, journal=journal, readback_delay_seconds=0)
    assert result['ok']
    assert backend.record['ps'] == '人工备注\n'+note
    assert submit(backend, journal)['ok']
    assert backend.record['ps'] == '人工备注'


def test_existing_manual_note_cannot_be_claimed_by_terminal_write(journal):
    note = '未识别出聚合类型，暂不适配'
    backend = Backend(before(ps=note))
    assert clear_backend_adaptation_via_api('com.example', api_url=URL, x_token='x', token='token',
            note=note, session=backend.session, journal=journal, readback_delay_seconds=0)['ok']
    assert submit(backend, journal)['ok']
    assert backend.record['ps'] == note


def test_replay_validation_only_reads_and_preserves_notes(journal):
    from automation_adaptation import verify_backend_for_replay_via_api
    backend = Backend(before(ps='人工备注'))
    result = verify_backend_for_replay_via_api(FIELDS, 'com.example', api_url=URL,
                     x_token='x', token='t', session=backend.session)
    assert result['ok']
    assert backend.writes == []
    assert backend.record['ps'] == '人工备注'
    backend.session.get.assert_not_called()
    backend.record['aggr_chaping_id'] = 'different'
    result = verify_backend_for_replay_via_api(FIELDS, 'com.example', api_url=URL,
                     x_token='x', token='t', session=backend.session)
    assert not result['ok']
    assert backend.writes == []


def test_multiline_owned_note_removal_preserves_other_text():
    owned = '工具检测结论\n下载量：10K+'
    current = before(ps='人工备注\n'+owned)
    result = merge_backend_payload(current, before(), owned)
    assert result['ps'] == '人工备注'


def test_explicit_legacy_authorization_is_scoped_to_package_and_endpoint(journal):
    journal.authorize_note_removal(derive_backend_submit_url(URL), 'com.example',
                                  'TradPlus聚合，暂不适配', 'operator explicitly requested removal')
    assert journal.owned_note(derive_backend_submit_url(URL), 'com.other') == ''
    assert journal.owned_note('https://other.test/s10_package_info', 'com.example') == ''
    backend = Backend(before(ps='TradPlus聚合，暂不适配'))
    assert submit(backend, journal)['ok']
    assert not backend.record['ps']
    assert journal.owned_note(derive_backend_submit_url(URL), 'com.example') == ''
