from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from selected_retry import fields_from_notes, validated_retry_fields


def notes(unit='tp-inter'):
    return f'''包名：com.example
最终判断:TradPlus聚合（日志与配置综合分析确认）
初始Activity:com.example.Main
归因平台:Adjust
插屏聚合id:{unit}
激励视频聚合id:
TradPlus APP ID:tp-app
TradPlus SDK版本:16.3.0.1
'''


def task(gid, *, completed=False, body=None):
    return SimpleNamespace(gid=gid, package_name='com.example.'+gid, completed=completed,
                           notes=notes() if body is None else body)


def test_saved_fields_do_not_combine_old_and_new_runs():
    data = fields_from_notes(notes('old-id') + '\n最终判断:TradPlus聚合\n归因平台:Adjust\n')
    assert '插屏聚合id' not in data
    with pytest.raises(ValueError, match='重新检测'):
        validated_retry_fields(task('a', body=notes('old-id') + '\n最终判断:TradPlus聚合\n归因平台:Adjust\n'))


def test_saved_sdk_keys_are_available_for_submit_validation():
    body = '最终判断:MAX聚合\n归因平台:AppsFlyer\nAppsflyer SDK Key:af-key\nAppLovin SDK Key:max-key\n初始Activity:com.example.Main\n插屏聚合id:valid-unit'
    data = validated_retry_fields(task('a', body=body))
    assert {'名称': 'AppLovin', 'key': 'max-key'} in data['SDK列表']


def app():
    from gui import APKToolApp
    obj = object.__new__(APKToolApp)
    obj._precheck_running = obj._automation_running = False
    obj._precheck_status = MagicMock()
    obj._precheck_tasks = {'one': task('a'), 'two': task('b'), 'other': task('c')}
    obj._automation_checkpoint_store = MagicMock()
    obj._automation_checkpoint_store.load.return_value = None
    obj._automation_checkpoint_store.save.side_effect = lambda cp: cp
    obj.automation_replay_timeout_var = MagicMock()
    obj.automation_replay_timeout_var.get.return_value = '300'
    obj._precheck_task_is_blacklisted = MagicMock(return_value=False)
    obj._automation_start_batch_queue = MagicMock()
    return obj


def test_selected_submit_queue_contains_exactly_the_captured_tasks():
    obj = app()
    assert obj._start_selected_retry({'a', 'b'}, 'submit')
    queue = obj._automation_start_batch_queue.call_args.args[0]
    assert {t.gid for _, t in queue} == {'a', 'b'}
    assert {r['gid'] for r in obj._automation_checkpoint['tasks']} == {'a', 'b'}
    assert all(r['selected_retry_stage'] == 'submit' and r['retry_fields']['插屏聚合id'] == 'tp-inter'
               for r in obj._automation_checkpoint['tasks'])


def test_refresh_does_not_change_retry_target_identity():
    obj = app()
    obj._automation_access_allowed = MagicMock(return_value=True)
    obj.precheck_task_tree = MagicMock()
    obj.precheck_task_tree.selection.return_value = ('one', 'two')
    obj._selected_retry_stage_var = MagicMock()
    obj._selected_retry_stage_var.get.return_value = '重新检测并适配'
    obj._start_selected_retry = MagicMock()
    def reload(callback):
        obj.precheck_task_tree.selection.return_value = ('other',)
        callback()
        return True
    obj._load_today_asana_tasks_async = reload
    assert obj._on_retry_selected_tasks()
    obj._start_selected_retry.assert_called_once_with({'a', 'b'}, 'detect')


def test_selected_blacklisted_or_missing_task_cannot_start_queue():
    obj = app()
    obj._precheck_task_is_blacklisted.return_value = True
    assert obj._start_selected_retry({'a'}, 'detect') is False
    obj._automation_start_batch_queue.assert_not_called()
    obj._precheck_task_is_blacklisted.return_value = False
    assert obj._start_selected_retry({'a', 'missing'}, 'detect') is False
    obj._automation_start_batch_queue.assert_not_called()


def test_selected_precheck_passes_explicit_scope():
    obj = app()
    obj._on_start_batch_precheck = MagicMock(return_value=True)
    assert obj._start_selected_retry({'b'}, 'precheck')
    obj._on_start_batch_precheck.assert_called_once_with(start_automation_after=True, selected_gids={'b'})


def test_replay_only_retry_never_submits_or_rotates_ids():
    obj = app()
    obj._automation_fields = fields_from_notes(notes())
    obj._automation_current_package_name = lambda: 'com.example'
    for name in ['cp_adapt_api_url_var', 'cp_adapt_x_token_var', 'cp_adapt_token_var']:
        setattr(obj, name, MagicMock())
        getattr(obj, name).get.return_value = 'configured'
    obj._safe_after = lambda delay, callback, *args: callback(*args)
    obj._automation_log = MagicMock()
    obj._automation_replay_sync = MagicMock(return_value={'ok': True, 'code': 'AGGREGATION_REPLAY_SUCCESS'})
    obj._automation_submit_backend_sync = MagicMock()
    obj._automation_replay_with_id_rotation_sync = MagicMock()
    obj._automation_confirm_cleanup_after_replay_sync = MagicMock(return_value=True)
    obj._automation_comment_success = MagicMock()
    obj._automation_handle_replay_result = MagicMock()
    with patch('gui.verify_backend_for_replay_via_api', return_value={'ok': True}):
        assert obj._automation_replay_selected_only_sync()
    obj._automation_replay_sync.assert_called_once()
    obj._automation_submit_backend_sync.assert_not_called()
    obj._automation_replay_with_id_rotation_sync.assert_not_called()


def test_background_install_continuation_retains_selected_scope():
    obj = app()
    obj._closing = False
    obj._automation_run_eligible_batch = MagicMock()
    obj._maybe_start_scoped_background_auto_adapt({'a', 'b'})
    obj._automation_run_eligible_batch.assert_called_once_with(task_gids={'a', 'b'})


def test_incomplete_saved_fields_fail_before_checkpoint_or_reset():
    obj = app()
    obj._precheck_tasks['one'].notes = '最终判断:TradPlus聚合\n归因平台:Adjust'
    assert obj._start_selected_retry({'a'}, 'submit') is False
    obj._automation_checkpoint_store.save.assert_not_called()
    obj._automation_start_batch_queue.assert_not_called()


def test_retry_metadata_survives_checkpoint_roundtrip(tmp_path):
    from automation_checkpoint import AutomationCheckpointStore
    obj = app()
    assert obj._start_selected_retry({'a', 'b'}, 'replay')
    store = AutomationCheckpointStore(str(tmp_path / 'checkpoint.json'))
    store.save(obj._automation_checkpoint)
    restored = store.load()
    assert {r['gid'] for r in restored['tasks']} == {'a', 'b'}
    assert all(r['selected_retry_stage'] == 'replay' and r['retry_fields']['插屏聚合id'] == 'tp-inter'
               for r in restored['tasks'])
