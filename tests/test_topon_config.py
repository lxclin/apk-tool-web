import json
import sqlite3
import struct
import subprocess
from types import SimpleNamespace

import pytest

from mediation_config import supplement_family_config
from topon_config import (fastkv_strings, extract_topon_strategies, supplement_topon_config)
from automation_adaptation import (build_backend_submission_payload, build_aggregation_assessment,
                                   format_aggregation_fields, format_detection_review_evidence,
                                   detect_aggregation_with_one_retry)
from ad_replay import AdReplayEvaluator, ReplayExpectation


def pack(records):
    payload = bytearray()
    for key, value, flags in records:
        key, value = key.encode(), value.encode() if isinstance(value, str) else value
        payload += bytes([flags, len(key)]) + key
        payload += struct.pack('<I' if (flags & 63) >= 9 else '<H', len(value)) + value
    checksum = 0
    for offset in range(0, len(payload), 8):
        checksum ^= int.from_bytes(payload[offset:offset+8].ljust(8, b'\0'), 'little')
    checksum = ((checksum << 32) | (checksum >> 32)) & ((1 << 64) - 1)
    return struct.pack('<iQ', len(payload), checksum) + payload + bytes(16)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('create table sdkconfig(key text,type text,value text)')
        connection.execute("insert into sdkconfig values('appABC','AP_SY','{}')")
    return path.read_bytes()


def strategy(format):
    return json.dumps({'format': format, 's2shb_list': [
        {'unit_id': 'network-source-id', 'content': {'placement_id': 'network-placement'}}]}).encode()


def snapshot():
    return pack([('appABCninter01_PL_SY', 'a'*32, 0x46),
                 ('appABCnreward1_PL_SY', 'b'*32, 0x46)])


def fields():
    return {'ok': True, '最终判断': 'TopOn聚合（日志与配置综合分析确认）',
            '初始Activity': 'com.example.Main', '归因平台': 'AppsFlyer, Adjust',
            '插屏聚合id': '', '激励视频聚合id': ''}


def test_live_records_only_not_deleted_hashes_or_padding(database):
    index = pack([('appABCninter01_PL_SY', 'c'*32, 0x86),
                  ('appABCninter01_PL_SY', 'a'*32, 0x46)])
    requested = []
    def external(name):
        requested.append(name)
        return strategy(3)
    result = extract_topon_strategies(database, index, external)
    assert result['插屏聚合id'] == ['ninter01']
    assert requested == ['a'*32]


def test_format_from_root_never_network_unit_or_nested_format(database):
    result = extract_topon_strategies(database, snapshot(),
                                     lambda name: strategy(3 if name == 'a'*32 else 1))
    assert result == {'插屏聚合id': ['ninter01'], '激励视频聚合id': ['nreward1']}
    assert 'network' not in str(result)


@pytest.mark.parametrize('kind', [6, 9])
def test_inline_json_and_large_strings(database, kind):
    index = pack([('appABCnreward1_PL_SY', strategy('1'), kind)])
    result = extract_topon_strategies(database, index, lambda name: pytest.fail('inline'))
    assert result['激励视频聚合id'] == ['nreward1']


def test_app_scope_and_strategy_key_exclude_unrelated_values(database):
    index = pack([('otherAppnreward1_PL_SY', strategy(1), 6),
                  ('appABCnetwork-source-id', strategy(1), 6)])
    assert not any(extract_topon_strategies(database, index, lambda _: b'').values())


def test_conflicting_formats_for_same_id_are_not_auto_filled(tmp_path):
    path = tmp_path / 'conflict.db'
    with sqlite3.connect(path) as connection:
        connection.execute('create table sdkconfig(key text,type text)')
        connection.executemany('insert into sdkconfig values(?,?)',
                               [('appABC','AP_SY'), ('appDEF','AP_SY')])
    index = pack([('appABCnshared1_PL_SY', strategy(1), 6),
                  ('appDEFnshared1_PL_SY', strategy(3), 6)])
    with pytest.raises(ValueError, match='conflicting formats'):
        extract_topon_strategies(path.read_bytes(), index, lambda _: b'')


@pytest.mark.parametrize('raw', [b'', b'garbage', strategy(0), strategy(2), strategy(4),
                               b'{"format":1}', b'[]', b'{"format":3,"hb_list":[]}'])
def test_missing_unknown_or_untyped_strategy_does_not_create_ids(database, raw):
    assert not any(extract_topon_strategies(database, snapshot(), lambda _: raw).values())


@pytest.mark.parametrize('mutation', ['checksum', 'truncated', 'negative-length', 'bad-filename'])
def test_corrupt_or_unsafe_fastkv_is_rejected(mutation):
    index = bytearray(snapshot())
    if mutation == 'checksum': index[13] ^= 1
    elif mutation == 'truncated': index = index[:20]
    elif mutation == 'negative-length': index[:4] = struct.pack('<i', -1)
    else: index = pack([('appABCnreward1_PL_SY', '../../outside', 0x46)])
    with pytest.raises(ValueError): fastkv_strings(index)


def device_runner(database, *, shared_uid=False, fail_reads=False, bad_index=False):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if 'packages' in args:
            text = 'package:com.example.game uid:10001\n'
            if shared_uid: text += 'package:com.other.game uid:10001\n'
            return SimpleNamespace(returncode=0, stdout=text)
        # ADB exec-out can return exit=0 for remote errors. Never choose this mode.
        if 'run-as' in args: return SimpleNamespace(returncode=0, stdout=b'run-as: not debuggable\n')
        if '-c' in args and 'su' in args and '0' not in args:
            return SimpleNamespace(returncode=0, stdout=b"su: invalid uid/gid '-c'\n")
        if fail_reads: return SimpleNamespace(returncode=1, stdout=b'')
        path = args[-1]
        if path.endswith('.db'): data = database
        elif path.endswith('.kva'): data = b'bad' if bad_index else snapshot()
        elif path.endswith('.kvb'): data = snapshot()
        else: data = strategy(3 if path.endswith('a'*32) else 1)
        return SimpleNamespace(returncode=0, stdout=data)
    return run, calls


def test_dispatch_submission_and_exact_id_replay_pipeline(database):
    run, calls = device_runner(database)
    f = fields()
    supplement_family_config(f, 'adb', '10001', runner=run)
    assert f['插屏聚合id'] == 'ninter01'
    assert f['激励视频聚合id'] == 'nreward1'
    assert f['广告ID来源详情']['插屏聚合id']['package'] == 'com.example.game'
    assert f['广告ID识别来源'] == 'topon_config_strategy'
    assessment = build_aggregation_assessment(f)
    assert assessment['confidence'] == '高' and assessment['auto_submit']
    assert assessment['submit_mode'] == 'full'
    detected = detect_aggregation_with_one_retry('com.example.game', lambda: f, first_fields=f)
    assert detected['ok']
    payload = build_backend_submission_payload(f, 'com.example.game')
    assert payload['aggr_platform'] == 'topon'
    assert payload['aggr_chaping_id'] == 'ninter01'
    assert payload['aggr_jilishipin_id'] == 'nreward1'
    assert 'TopOn数据库与有效策略缓存' in format_aggregation_fields(f)
    assert 'TopOn 策略缓存' in format_detection_review_evidence(f)
    evaluator = AdReplayEvaluator(ReplayExpectation.from_values('ninter01', 'nreward1', 'TopOn聚合'))
    evaluator.feed('UnityAds: Started showing ad for placement network-placement')
    assert not evaluator.complete
    for format, unit in [('interstitial','ninter01'), ('rewarded','nreward1')]:
        evaluator.feed('ZGSDK.mediationEvent: '+json.dumps({
            'platform':'TopOn','adType':format,'adUnitId':unit,'status':'load_success'}))
    assert not evaluator.complete
    for format, unit in [('interstitial','ninter01'), ('rewarded','nreward1')]:
        evaluator.feed('ZGSDK.mediationEvent: '+json.dumps({
            'platform':'TopOn','adType':format,'adUnitId':unit,'status':'display_success'}))
    assert evaluator.complete


def test_bad_a_snapshot_falls_back_to_b(database):
    run, calls = device_runner(database, bad_index=True)
    f = supplement_topon_config(fields(), 'adb', '10001', runner=run)
    assert f['插屏聚合id'] == 'ninter01'
    assert any(call[-1].endswith('.kvb') for call in calls)


def test_existing_unit_is_preserved_only_missing_format_is_filled(database):
    run, _ = device_runner(database)
    f = fields(); f['插屏聚合id'] = 'existing-inter'
    supplement_topon_config(f, 'adb', '10001', runner=run)
    assert f['插屏聚合id'] == 'existing-inter'
    assert f['激励视频聚合id'] == 'nreward1'


@pytest.mark.parametrize('shared,fail', [(True, False), (False, True)])
def test_unreadable_or_shared_uid_never_fabricates_ids(database, shared, fail):
    run, calls = device_runner(database, shared_uid=shared, fail_reads=fail)
    f = supplement_topon_config(fields(), 'adb', '10001', runner=run)
    assert not f['插屏聚合id'] and not f['激励视频聚合id']
    assert not build_aggregation_assessment(f)['auto_submit']
    if shared: assert len(calls) == 1


def test_timeout_and_non_topon_are_bounded():
    def timeout(args, **kwargs): raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    f = supplement_topon_config(fields(), 'adb', '10001', runner=timeout)
    assert 'TimeoutExpired' in f['_topon_config_read']
    f = fields(); f['最终判断'] = 'MAX聚合'
    supplement_topon_config(f, 'adb', '10001', runner=lambda *a, **k: pytest.fail('unrelated'))
    assert not f['插屏聚合id']


def test_total_read_budget_stops_before_more_device_commands(database, monkeypatch):
    clock = iter([0, 3, 6, 9])
    monkeypatch.setattr('topon_config.time.monotonic', lambda: next(clock))
    run, calls = device_runner(database)
    f = supplement_topon_config(fields(), 'adb', '10001', runner=run)
    assert len(calls) == 2  # UID query and first permission attempt, then deadline.
    assert 'TimeoutExpired' in f['_topon_config_read']
    assert not f['插屏聚合id'] and not f['激励视频聚合id']


def test_conflict_is_not_bypassed_by_loading_old_backup(database, tmp_path):
    path = tmp_path / 'two-apps.db'
    path.write_bytes(database)
    with sqlite3.connect(path) as connection:
        connection.execute("insert into sdkconfig values('appDEF','AP_SY','{}')")
    run, calls = device_runner(path.read_bytes())
    conflicting = pack([('appABCnshared1_PL_SY', strategy(1), 6),
                        ('appDEFnshared1_PL_SY', strategy(3), 6)])
    def wrapped(args, **kwargs):
        if args[-1].endswith('.kva'):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=conflicting)
        return run(args, **kwargs)
    f = supplement_topon_config(fields(), 'adb', '10001', runner=wrapped)
    assert f['_aggregation_evidence_conflict']
    assert not any(call[-1].endswith('.kvb') for call in calls)
    assert not build_aggregation_assessment(f)['auto_submit']
    assert not f['插屏聚合id'] and not f['激励视频聚合id']


def test_external_read_limit_applies_even_to_unreadable_strategies(database):
    index = pack([(f'appABCnlimit{i:03}_PL_SY', 'a'*32, 0x46) for i in range(17)])
    reads = []
    def unreadable(name):
        reads.append(name)
        return b''
    with pytest.raises(ValueError, match='limit exceeded'):
        extract_topon_strategies(database, index, unreadable)
    assert len(reads) == 16
