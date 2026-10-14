import html
import json
from types import SimpleNamespace

import pytest

from adb_pusher import parse_autodetector_fields, build_backend_url
from automation_adaptation import build_aggregation_assessment, detection_field_issue
from mediation_config import parse_provider_order, supplement_family_config


def config(order):
    value = html.escape(json.dumps({'response': {'providerOrder': order}}))
    return '<map><string name="last_response">' + value + '</string></map>'


def fields():
    return {'最终判断': 'LevelPlay聚合', '归因平台': 'Adjust',
            '初始Activity': 'example.Main', '激励视频聚合id': 'video', '插屏聚合id': 'inter'}


def test_xml_entities_json_and_platform_owned_keys():
    ids = parse_provider_order(config({'rewarded': {'external': {}, 'video': {}, 'real-r': {}},
                                      'interstitial': {'real-i': {}}}))
    assert ids == {'激励视频聚合id': ['real-r'], '插屏聚合id': ['real-i']}


@pytest.mark.parametrize('text', ['invalid', '<map/>', '<map><string name="last_response">[]</string></map>',
    config({'rewarded': ['not-a-key'], 'interstitial': 'bad'}),
    '<map><string name="last_response">{"appKey":"not-an-ad-unit"}</string></map>'])
def test_malformed_or_unrelated_config_does_not_fabricate_ids(text):
    assert not any(parse_provider_order(text).values())


def test_unique_uid_config_supplements_only_missing_units():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='package:com.example.game\n' if 'packages' in args else
            config({'rewarded': {'new-r': {}}, 'interstitial': {'new-i': {}}}))
    f = fields(); f['插屏聚合id'] = 'existing-i'
    supplement_family_config(f, 'adb', '10001', runner=runner)
    assert f['插屏聚合id'] == 'existing-i'
    assert f['激励视频聚合id'] == 'new-r'
    assert f['广告ID来源详情']['激励视频聚合id']['package'] == 'com.example.game'
    assert all('head' in call or 'packages' in call for call in calls)
    assert build_aggregation_assessment(f)['auto_submit']


def test_shared_uid_or_unreadable_config_is_safe():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='package:com.one\npackage:com.two\n')
    f = fields()
    supplement_family_config(f, 'adb', '10001', runner=runner)
    assert len(calls) == 1
    assert not build_aggregation_assessment(f)['auto_submit']
    assert 'aggr_chaping_id' not in build_backend_url(f, 'com.one')


def test_runtime_levelplay_blocks_stale_iron_verdict_until_confirmed():
    lines = ['ZGSDK.AutoDetector: 最终判断: IronSource聚合',
             'ZGSDK.AutoDetector: 归因平台: Adjust',
             'ZGSDK.AutoDetector: 初始页面Activity: example.Main',
             'ZGSDK.AutoDetector: IronSource:',
             'ZGSDK.AutoDetector: 插屏聚合id: actual-i',
             'LevelPlaySDK: API : LevelPlayInterstitialAd.loadAd()']
    f = parse_autodetector_fields(lines)
    assert f['_aggregation_evidence_conflict']
    assert detection_field_issue(f)[0] == 'AGGREGATION_RESULT_INCOMPLETE'
    assert not build_aggregation_assessment(f)['auto_submit']
    assert 'aggr_platform' not in build_backend_url(f, 'com.example')
    lines += ['ZGSDK.AutoDetector: 最终判断: LevelPlay聚合',
              'ZGSDK.AutoDetector: LevelPlay:', 'ZGSDK.AutoDetector: 插屏聚合id: correct-i']
    f = parse_autodetector_fields(lines)
    assert not f.get('_aggregation_evidence_conflict')
    assert f['聚合判定历史'] == ['ironsource', 'levelplay']
    assert f['插屏聚合id'] == 'correct-i'
    assert build_aggregation_assessment(f)['auto_submit']


def test_sdk_presence_and_demand_only_do_not_promote_levelplay():
    lines = ['ZGSDK.AutoDetector: 最终判断: IronSource聚合',
             'ZGSDK.AutoDetector: 检测到的SDK: [IronSource, LevelPlay]',
             'LevelPlaySDK: API : LevelPlayInterstitialAd.loadAd()', 'config {"isDemandOnly":1}']
    f = parse_autodetector_fields(lines)
    assert f.get('_aggregation_evidence_conflict')
    assert f['最终判断'] == 'IronSource聚合'
    assert not build_aggregation_assessment(f)['auto_submit']


def test_legacy_inferred_result_cannot_be_submitted():
    f = fields(); f['最终判断'] = 'IronSource聚合（根据 video/inter 自动推断）'
    assert not build_aggregation_assessment(f)['auto_submit']
    assert not f.get('_aggregation_type_inferred')


def test_real_levelplay_api_units_exclude_adapter_placement_ids_without_attribution():
    lines = ['ZGSDK.AutoDetector: 最终判断: LevelPlay聚合',
        'LevelPlaySDK: API LevelPlayRewardedAd <init> - adUnitId: real-r, config: Config()',
        'LevelPlaySDK: API LevelPlayInterstitialAd <init> - adUnitId: real-i, config: Config()',
        'LevelPlaySDK: ADAPTER_API FacebookInterstitialAdapter placementId: network-i',
        'LevelPlaySDK: ADAPTER_API FacebookInterstitialAdapter adUnitId: network-unit-i',
        'LevelPlaySDK: ADAPTER_API FacebookRewardedAdapter placementId: network-r']
    f = parse_autodetector_fields(lines)
    assert f['插屏聚合id'] == 'real-i'
    assert f['激励视频聚合id'] == 'real-r'
    assert build_aggregation_assessment(f)['submit_mode'] != 'full'  # Missing attribution/activity.


def test_android_uid_suffix_and_su_zero_fallback():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if 'packages' in args:
            return SimpleNamespace(returncode=0, stdout='package:com.example.game uid:10001\n')
        if '0' in args:
            return SimpleNamespace(returncode=0, stdout=config({'interstitial': {'real-i': {}}}))
        return SimpleNamespace(returncode=1, stdout='')
    f = fields()
    supplement_family_config(f, 'adb', '10001', runner=runner)
    assert f['插屏聚合id'] == 'real-i'
    assert len(calls) == 4


def test_config_timeout_preserves_existing_fields():
    import subprocess
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
    f = fields()
    supplement_family_config(f, 'adb', '10001', runner=runner)
    assert '失败' in f['_mediation_config_read']
    assert f['插屏聚合id'] == 'inter'


def test_symbolic_native_detection_cannot_write_terminal_backend_note():
    from automation_adaptation import non_game_empty_backend_note
    f = fields(); f['应用类型'] = 'Native'
    build_aggregation_assessment(f)
    assert non_game_empty_backend_note(f) == ''
