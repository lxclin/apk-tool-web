import threading
from unittest.mock import MagicMock, patch

from automation_process import matching_package_processes


def _app():
    from gui import APKToolApp

    app = object.__new__(APKToolApp)
    app._automation_current_package_name = lambda: "com.example.game"
    app._automation_set_batch_stage = MagicMock()
    app._automation_stop_active_logcat = MagicMock()
    app._automation_run_command_sync = MagicMock(return_value="")
    app._automation_package_processes_sync = MagicMock(return_value=[])
    app._automation_report_event = MagicMock()
    app._automation_log = MagicMock()
    app._safe_after = lambda _delay, callback, *args: callback(*args)
    return app


def test_match_main_and_child_processes_without_similar_package():
    output = (
        "USER PID NAME\n"
        "u0_a1 111 com.example.game\n"
        "u0_a1 112 com.example.game:remote\n"
        "u0_a2 113 com.example.game.other\n"
    )
    assert matching_package_processes(output, "com.example.game") == [
        "com.example.game", "com.example.game:remote",
    ]


def test_cleanup_verifies_process_exit_before_reporting_success():
    app = _app()
    app._automation_package_processes_sync.side_effect = [
        ["com.example.game:remote"], [],
    ]
    with patch("gui.time.sleep"):
        assert app._automation_cleanup_current_app_sync("done") is True

    assert app._automation_run_command_sync.call_count == 1
    app._automation_report_event.assert_called_with(
        "process_cleanup", status="success",
        details={"force_stop_attempts": 1},
    )


def test_cleanup_retries_force_stop_and_reports_unconfirmed_process():
    app = _app()
    app._automation_package_processes_sync.return_value = ["com.example.game"]
    with patch("gui.time.sleep"):
        assert app._automation_cleanup_current_app_sync("done") is False

    assert app._automation_run_command_sync.call_count == 2
    assert app._automation_report_event.call_args.args[0] == "process_cleanup"
    assert app._automation_report_event.call_args.kwargs["status"] == "failed"


def test_replay_success_is_not_published_when_cleanup_is_unconfirmed():
    app = _app()
    app._automation_cleanup_current_app_sync = MagicMock(return_value=False)
    app._automation_comment_review = MagicMock()
    app._automation_batch_active = True
    app._automation_stop_event = threading.Event()

    assert app._automation_confirm_cleanup_after_replay_sync() is False
    app._automation_comment_review.assert_called_once()
    assert app._automation_comment_review.call_args.args[0] == "PROCESS_CLEANUP_UNCONFIRMED"
    assert app._automation_stop_event.is_set()


def test_standard_success_path_cleans_up_before_success_comment():
    app = _app()
    app._automation_fields = {"最终判断": "MAX聚合"}
    app._automation_stop_event = threading.Event()
    app._automation_ensure_clash_vpn_sync = MagicMock()
    app._automation_save_checkpoint = MagicMock()
    app._automation_prepare_replay_id_candidates = MagicMock()
    app._automation_fill_asana_sync = MagicMock()
    app._automation_submit_backend_sync = MagicMock(return_value={"ok": True})
    app._automation_replay_with_id_rotation_sync = MagicMock(
        return_value={"ok": True, "code": "AGGREGATION_REPLAY_SUCCESS"}
    )
    order = []
    app._automation_confirm_cleanup_after_replay_sync = lambda: order.append("cleanup") or True
    app._automation_comment_success = lambda _result: order.append("success")
    app._automation_handle_replay_result = MagicMock()
    with patch("gui.detection_field_issue", return_value=None):
        assert app._automation_execute_post_detection_sync() is True

    assert order == ["cleanup", "success"]


def test_standard_success_path_does_not_publish_when_cleanup_fails():
    app = _app()
    app._automation_fields = {"最终判断": "MAX聚合"}
    app._automation_stop_event = threading.Event()
    app._automation_ensure_clash_vpn_sync = MagicMock()
    app._automation_save_checkpoint = MagicMock()
    app._automation_prepare_replay_id_candidates = MagicMock()
    app._automation_fill_asana_sync = MagicMock()
    app._automation_submit_backend_sync = MagicMock(return_value={"ok": True})
    app._automation_replay_with_id_rotation_sync = MagicMock(
        return_value={"ok": True, "code": "AGGREGATION_REPLAY_SUCCESS"}
    )
    app._automation_confirm_cleanup_after_replay_sync = MagicMock(return_value=False)
    app._automation_comment_success = MagicMock()
    with patch("gui.detection_field_issue", return_value=None):
        assert app._automation_execute_post_detection_sync() is False

    app._automation_comment_success.assert_not_called()
