from copy import deepcopy
from unittest.mock import MagicMock
from urllib.parse import parse_qs
import pytest
from adb_pusher import (parse_autodetector_fields, build_backend_url, prioritized_ad_unit_ids)
from automation_adaptation import (build_backend_submission_payload, format_aggregation_fields,
                                   get_ad_unit_id, is_authoritative_aggregation_upgrade,
                                   apply_aggregation_type_fallback)
from ad_replay import ReplayExpectation


def level_fields():
    return {'ok': True, '最终判断': 'LevelPlay聚合（自动化检测确认）',
            '初始Activity': 'com.coredian.BoomBitMainActivity', '应用类型': 'Unity',
            '归因平台': 'AppsFlyer, Adjust, Singular',
            'SDK列表': [{'名称': 'Appsflyer', 'key': 'test-af-key'}],
            '激励视频聚合id': '3j1gzpw8gjkriqhm, video',
            '插屏聚合id': 'inter, qtlnavopjgc7dh5c'}


def test_user_example_uses_only_concrete_levelplay_units_everywhere():
    fields = level_fields()
    assert get_ad_unit_id(fields, '插屏聚合id') == 'qtlnavopjgc7dh5c'
    assert get_ad_unit_id(fields, '激励视频聚合id') == '3j1gzpw8gjkriqhm'
    payload = build_backend_submission_payload(fields, 'com.example')
    assert payload['aggr_platform'] == 'level_play'
    assert payload['aggr_chaping_id'] == 'qtlnavopjgc7dh5c'
    assert payload['aggr_jilishipin_id'] == '3j1gzpw8gjkriqhm'
    text = format_aggregation_fields(fields)
    assert '插屏聚合id:qtlnavopjgc7dh5c' in text
    assert '激励视频聚合id:3j1gzpw8gjkriqhm' in text
    assert 'inter,' not in text and ', video' not in text
    expectation = ReplayExpectation.from_values('inter, qtlnavopjgc7dh5c',
               'video, 3j1gzpw8gjkriqhm', 'LevelPlay聚合')
    assert expectation.interstitial_ids == ('qtlnavopjgc7dh5c',)
    assert expectation.rewarded_ids == ('3j1gzpw8gjkriqhm',)


@pytest.mark.parametrize('verdict', ['IronSource聚合', 'LevelPlay聚合', 'MAX聚合', 'TradPlus聚合'])
def test_concrete_ids_remove_symbolic_fallback_candidates(verdict):
    assert prioritized_ad_unit_ids('inter, new-a, inter, new-b', verdict, '插屏聚合id') == ('new-a', 'new-b')
    assert prioritized_ad_unit_ids('video, new-r', verdict, '激励视频聚合id') == ('new-r',)


def test_exact_placeholder_pair_is_rejected_instead_of_legacy_inference():
    fields = parse_autodetector_fields(['ZGSDK.AutoDetector: IronSource:',
                 'ZGSDK.AutoDetector: 激励视频聚合id: video',
                 'ZGSDK.AutoDetector: 插屏聚合id: inter'])
    apply_aggregation_type_fallback(fields)
    assert not fields.get('_aggregation_type_inferred')
    assert fields['_symbolic_ad_ids_rejected']
    assert fields['激励视频聚合id'] == ''
    assert fields['插屏聚合id'] == ''
    assert get_ad_unit_id({"最终判断": None, "插屏聚合id": "inter"}, "插屏聚合id") == ""


def test_later_platform_selects_own_units_and_not_old_ironsource_units():
    lines = ['最终判断: IronSource聚合', 'IronSource:', '激励视频聚合id: video',
             '插屏聚合id: inter', '最终判断: LevelPlay聚合', 'LevelPlay:',
             '激励视频聚合id: 3j1gzpw8gjkriqhm, video', '插屏聚合id: inter, qtlnavopjgc7dh5c']
    fields = parse_autodetector_fields(['ZGSDK.AutoDetector: '+line for line in lines])
    assert fields['插屏聚合id'] == 'qtlnavopjgc7dh5c'
    assert fields['激励视频聚合id'] == '3j1gzpw8gjkriqhm'
    incomplete = parse_autodetector_fields(['ZGSDK.AutoDetector: '+line for line in lines[:5]])
    assert incomplete['插屏聚合id'] == incomplete['激励视频聚合id'] == ''


def test_levelplay_late_result_is_an_upgrade_only_with_complete_concrete_units():
    previous = {'最终判断': 'IronSource聚合', '插屏聚合id': 'inter', '激励视频聚合id': 'video'}
    assert is_authoritative_aggregation_upgrade(previous, level_fields())
    pending = level_fields()
    pending['SDK列表'] = []
    pending['归因平台'] = 'AppsFlyer'
    assert not is_authoritative_aggregation_upgrade(previous, pending)  # Missing AF key.
    pending = level_fields()
    pending.update(插屏聚合id='inter', 激励视频聚合id='video')
    assert not is_authoritative_aggregation_upgrade(previous, pending)


def test_candidate_queue_is_rebuilt_without_symbolic_ids():
    from gui import APKToolApp
    app = object.__new__(APKToolApp)
    app._automation_fields = level_fields()
    app._automation_replay_id_candidates = {'interstitial': ('inter',), 'rewarded': ('video',)}
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_render_fields = MagicMock()
    app._automation_context_version = 1
    app._automation_prepare_replay_id_candidates()
    assert app._automation_replay_id_candidates == {'interstitial': ('qtlnavopjgc7dh5c',), 'rewarded': ('3j1gzpw8gjkriqhm',)}


def test_replay_platform_upgrade_replaces_old_candidates_and_runs_standard_tail():
    from gui import APKToolApp
    app = object.__new__(APKToolApp)
    app._automation_fields = {'最终判断': 'IronSource聚合', '插屏聚合id': 'inter', '激励视频聚合id': 'video'}
    app._automation_replay_id_candidates = {'interstitial': ('inter',), 'rewarded': ('video',)}
    app._automation_context_version = 1
    app._safe_after = lambda delay, callback, *args: callback(*args)
    app._automation_log = app._automation_render_fields = MagicMock()
    app._automation_clear_inferred_backend_sync = MagicMock(return_value={'ok': True})
    app._automation_execute_post_detection_sync = MagicMock(return_value=True)
    assert app._automation_handle_replay_type_change_sync({'detected_fields': level_fields()})
    app._automation_clear_inferred_backend_sync.assert_called_once_with(note='回放检测到 LevelPlay，清理临时 IronSource 参数')
    assert app._automation_replay_id_candidates == {}
    assert app._automation_fields['插屏聚合id'] == 'qtlnavopjgc7dh5c'
    app._automation_execute_post_detection_sync.assert_called_once()
