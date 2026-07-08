import pytest
from unittest.mock import patch, MagicMock
import subprocess


class TestCheckDevice:
    def test_returns_true_when_device_connected(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\n192.168.1.1:5555\tdevice\n")
            from adb_pusher import check_device
            assert check_device() is True

    def test_returns_false_when_no_device(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            from adb_pusher import check_device
            assert check_device() is False

    def test_returns_false_when_adb_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            from adb_pusher import check_device
            assert check_device() is False


class TestGetDeviceList:
    def test_returns_device_ids(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="List of devices attached\nABC123\tdevice\nDEF456\tdevice\n")
            from adb_pusher import get_device_list
            assert get_device_list() == ["ABC123", "DEF456"]

    def test_returns_empty_list_when_no_devices(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            from adb_pusher import get_device_list
            assert get_device_list() == []


class TestStopLogcatStream:
    def test_kills_process_when_terminate_wait_times_out_without_closing_stdout(self):
        from adb_pusher import stop_logcat_stream

        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("adb logcat", 0.2),
            None,
        ]

        stop_logcat_stream(proc, timeout=0.2)

        proc.terminate.assert_called_once_with()
        proc.stdout.close.assert_not_called()
        proc.wait.assert_any_call(timeout=0.2)
        proc.kill.assert_called_once_with()


class TestPushApk:
    def test_successful_install(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="Success\n")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is True
            assert "成功" in msg

    def test_failed_install(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="INSTALL_FAILED\n")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False
            assert "失败" in msg

    def test_adb_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False
            assert "ADB" in msg

    def test_no_device_connected(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no devices/emulators found")
            from adb_pusher import push_apk
            ok, msg = push_apk("/path/to/app.apk")
            assert ok is False

    def test_falls_back_to_xapk_when_downloaded_file_contains_apks(self, tmp_path):
        from adb_pusher import push_apk
        import zipfile

        downloaded = tmp_path / "download.apk"
        with zipfile.ZipFile(downloaded, "w") as z:
            z.writestr("base.apk", b"fake apk")

        failed_install = MagicMock(returncode=1, stdout="", stderr="not a valid APK")
        with patch("adb_pusher._run_adb", return_value=failed_install), \
             patch("adb_pusher._install_xapk", return_value=(True, "安装成功")) as mock_install_xapk:
            ok, msg = push_apk(str(downloaded))

        assert ok is True
        assert "成功" in msg
        mock_install_xapk.assert_called_once_with(str(downloaded))


class TestApkDownloadUrl:
    def test_detects_xapk_url_with_query_and_uppercase_extension(self):
        from adb_pusher import is_apk_download_url

        assert is_apk_download_url("https://cdn.example.com/game.XAPK?token=abc")

    def test_infers_filename_from_url_path(self):
        from adb_pusher import download_artifact_filename

        assert download_artifact_filename(
            "https://cdn.example.com/path/game%20name.XAPK?token=abc"
        ) == "game name.XAPK"


class TestAdbCommandBuilders:
    def test_build_fix_zygotehole_permissions_cmd(self):
        from adb_pusher import build_fix_zygotehole_permissions_cmd

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"):
            assert build_fix_zygotehole_permissions_cmd() == [
                "/usr/bin/adb",
                "shell",
                (
                    "chmod 777 /data/local/tmp/zygotehole/config.json; "
                    "chmod 444 /data/local/tmp/zygotehole/zygotehole.apk; "
                    "chmod 777 /data/local/tmp/zygotehole; "
                    "chown root:root /data/local/tmp/zygotehole/zygotehole.apk"
                ),
            ]

    def test_build_zygote_build_cmd_repairs_permissions_after_build(self):
        from adb_pusher import build_zygote_build_cmd

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"):
            cmd = build_zygote_build_cmd("/tmp/work dir")

        assert cmd[0:2] == ["sh", "-c"]
        assert "sh '/tmp/work dir/zygote_build.sh'" in cmd[2]
        assert "&& /usr/bin/adb shell" in cmd[2]
        assert "chmod 444 /data/local/tmp/zygotehole/zygotehole.apk" in cmd[2]
        assert "chown root:root /data/local/tmp/zygotehole/zygotehole.apk" in cmd[2]


class TestNormalizeActionDelays:
    def test_raises_first_delay_to_15s_and_others_to_configured_minimum(self):
        from adb_pusher import normalize_action_delays

        script = {
            "ActivityA": [
                {"delay": 1000, "type": "click"},
                {"delay": 3999, "type": "click"},
                {"delay": 4500, "type": "click"},
            ],
            "ActivityB": [
                {"delay": 300, "type": "click"},
            ],
        }

        normalized, stats = normalize_action_delays(script, min_delay_ms=6000)

        assert [item["delay"] for item in normalized["ActivityA"]] == [
            15000,
            6000,
            6000,
        ]
        assert normalized["ActivityB"][0]["delay"] == 6000
        assert stats["delay_count"] == 4
        assert stats["updated_count"] == 4
        assert stats["first_delay_ms"] == 15000
        assert stats["min_delay_ms"] == 6000
        assert script["ActivityA"][0]["delay"] == 1000

    def test_keeps_existing_delays_when_already_above_minimums(self):
        from adb_pusher import normalize_action_delays

        script = {"ActivityA": [{"delay": 18383}, {"delay": 5427}]}

        normalized, stats = normalize_action_delays(script)

        assert [item["delay"] for item in normalized["ActivityA"]] == [18383, 5427]
        assert stats["updated_count"] == 0

    def test_normalizes_pasted_fragment_without_opening_brace(self):
        from adb_pusher import normalize_action_script_text

        text, stats = normalize_action_script_text(
            '"ActivityA": [{"delay": 500}], "ActivityB": [{"delay": 900}]}',
            min_delay_ms=7000,
        )

        assert '"delay": 15000' in text
        assert '"delay": 7000' in text
        assert stats["delay_count"] == 2


class TestBuildBackendUrl:
    def test_uses_hash_route_and_maps_fields(self):
        from urllib.parse import parse_qs

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {
                "最终判断": "AppLovin Max",
                "归因平台": "AppsFlyer",
                "插屏聚合id": "inter_123",
                "激励视频聚合id": "reward_456",
                "初始Activity": "com.demo.MainActivity",
                "SDK列表": [
                    {"名称": "AppLovin", "key": "sdk-key-xxx"},
                ],
            },
            "com.demo.game",
        )

        prefix = "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?"
        assert url.startswith(prefix)

        params = parse_qs(url.removeprefix(prefix))
        assert params["change"] == ["1"]
        assert params["package_name"] == ["com.demo.game"]
        assert params["aggr_platform"] == ["max"]
        assert params["attribution_platform"] == ["AppsFlyer"]
        assert params["aggr_chaping_id"] == ["inter_123"]
        assert params["aggr_jilishipin_id"] == ["reward_456"]
        assert params["activity_main_page"] == ["com.demo.MainActivity"]
        assert params["manual_applovin_sdk_key"] == ["sdk-key-xxx"]

    def test_backend_url_includes_af_key(self):
        from urllib.parse import parse_qs

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {"af_key": "af-key-123"},
            "com.demo.game",
        )

        prefix = "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?"
        params = parse_qs(url.removeprefix(prefix))
        assert params["af_key"] == ["af-key-123"]

    def test_backend_url_uses_appsflyer_sdk_key_as_af_key(self):
        from urllib.parse import parse_qs

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {
                "SDK列表": [
                    {"名称": "Appsflyer", "key": "appsflyer-key-123"},
                ],
            },
            "com.demo.game",
        )

        prefix = "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?"
        params = parse_qs(url.removeprefix(prefix))
        assert params["af_key"] == ["appsflyer-key-123"]

    def test_backend_url_uses_first_ad_unit_id_only(self):
        from urllib.parse import parse_qs

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {
                "插屏聚合id": "first_inter, second_inter, third_inter",
                "激励视频聚合id": "first_reward, second_reward",
            },
            "com.demo.game",
        )

        prefix = "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?"
        params = parse_qs(url.removeprefix(prefix))
        assert params["aggr_chaping_id"] == ["first_inter"]
        assert params["aggr_jilishipin_id"] == ["first_reward"]


class TestExtractUidFromDumpsys:
    def test_extracts_user_id(self):
        from adb_pusher import extract_uid_from_dumpsys

        output = """
Packages:
  Package [com.demo.game] (abc):
    userId=10234
"""

        assert extract_uid_from_dumpsys(output, "com.demo.game") == "10234"

    def test_extracts_app_id(self):
        from adb_pusher import extract_uid_from_dumpsys

        output = """
Packages:
  Package [mahjong.solitaire.puzzle.game] (d1ca149):
    appId=10275
    pkg=Package{2c856e9 mahjong.solitaire.puzzle.game}
"""

        assert extract_uid_from_dumpsys(
            output,
            "mahjong.solitaire.puzzle.game",
        ) == "10275"


class TestParseFillUrl:
    def test_parses_web_query_fill_fields(self):
        from adb_pusher import parse_fill_url

        data = parse_fill_url(
            "https://lxclin.github.io/apk-tool-web/"
            "?package_name=com.demo.game&appid=app-123"
            "&google_download_url=https%3A%2F%2Fplay.google.com%2Fstore%2Fapps%2Fdetails%3Fid%3Dcom.demo.game"
        )

        assert data == {
            "package_name": "com.demo.game",
            "appId": "app-123",
            "gpUrl": "https://play.google.com/store/apps/details?id=com.demo.game",
        }

    def test_parses_hash_route_query(self):
        from adb_pusher import parse_fill_url

        data = parse_fill_url(
            "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt"
            "?change=1&package_name=com.demo.game&aggr_platform=admob"
        )

        assert data == {"package_name": "com.demo.game"}

    def test_parses_google_play_package_id(self):
        from adb_pusher import parse_fill_url

        url = "https://play.google.com/store/apps/details?id=com.demo.game"

        assert parse_fill_url(url) == {
            "package_name": "com.demo.game",
            "gpUrl": url,
        }


class TestExtractGooglePlayPackage:
    def test_extracts_package_from_google_play_url(self):
        from adb_pusher import extract_google_play_package

        assert extract_google_play_package(
            "https://play.google.com/store/apps/details?id=com.demo.game&hl=en"
        ) == "com.demo.game"

    def test_ignores_non_google_play_url(self):
        from adb_pusher import extract_google_play_package

        assert extract_google_play_package("https://example.com/?id=com.demo.game") == ""


class TestExtractAfKey:
    def test_extracts_af_key_label(self):
        from adb_pusher import extract_af_key_from_content

        assert extract_af_key_from_content("af_key: 3F4A8Ct5TV8mYprgtEZhFF") == (
            "3F4A8Ct5TV8mYprgtEZhFF"
        )

    def test_extracts_appsflyer_sdk_key_label(self):
        from adb_pusher import extract_af_key_from_content

        assert extract_af_key_from_content("Appsflyer SDK Key: 3F4A8Ct5TV8mYprgtEZhFF") == (
            "3F4A8Ct5TV8mYprgtEZhFF"
        )


class TestParseAutodetectorFields:
    def test_af_key_is_displayed_as_appsflyer_sdk_key(self):
        from adb_pusher import parse_autodetector_fields

        fields = parse_autodetector_fields([
            "06-25 16:17:22.910 I ZGSDK.AutoDetector: af_key: EngPP6V7VKqrHykVBFGczi",
        ])

        assert fields["af_key"] == "EngPP6V7VKqrHykVBFGczi"
        assert {"名称": "Appsflyer", "key": "EngPP6V7VKqrHykVBFGczi"} in fields["SDK列表"]

    def test_uses_ids_for_detected_max_platform(self):
        from adb_pusher import parse_autodetector_fields

        lines = [
            "06-16 17:35:06.846 I ZGSDK.AutoDetector: 最终判断: max聚合（自动化检测确认）",
            "06-16 17:35:06.856 I ZGSDK.AutoDetector: AppLovin: ",
            "06-16 17:35:06.856 I ZGSDK.AutoDetector:   SDK Key: max-key",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector:   激励视频聚合id: [b5a21c21da9780f9]",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector:   插屏聚合id: [caa8fdbbdf51c161]",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector: IronSource: ",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector:   SDK Key: iron-key",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector:   激励视频聚合id: [wmnzcfb4fv7uxvba]",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector:   插屏聚合id: [u22erde2tomtjipg]",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector: LevelPlay: ",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector:   SDK Key: level-key",
            "06-16 17:35:06.860 I ZGSDK.AutoDetector:   激励视频聚合id: [level-reward]",
            "06-16 17:35:06.861 I ZGSDK.AutoDetector:   插屏聚合id: [level-inter]",
        ]

        fields = parse_autodetector_fields(lines)

        assert fields["最终判断"] == "max聚合（自动化检测确认）"
        assert fields["激励视频聚合id"] == "b5a21c21da9780f9"
        assert fields["插屏聚合id"] == "caa8fdbbdf51c161"

    def test_uses_ids_for_detected_ironsource_platform(self):
        from adb_pusher import parse_autodetector_fields

        lines = [
            "06-16 17:35:06.846 I ZGSDK.AutoDetector: 最终判断: IronSource聚合",
            "06-16 17:35:06.856 I ZGSDK.AutoDetector: AppLovin: ",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector:   激励视频聚合id: [max-reward]",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector:   插屏聚合id: [max-inter]",
            "06-16 17:35:06.857 I ZGSDK.AutoDetector: IronSource: ",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector:   激励视频聚合id: [iron-reward]",
            "06-16 17:35:06.858 I ZGSDK.AutoDetector:   插屏聚合id: [iron-inter]",
        ]

        fields = parse_autodetector_fields(lines)

        assert fields["激励视频聚合id"] == "iron-reward"
        assert fields["插屏聚合id"] == "iron-inter"


class TestExtractLogcatFields:
    def test_decodes_logcat_with_replacement_for_invalid_bytes(self):
        from adb_pusher import extract_logcat_fields

        def fake_run(cmd, **kwargs):
            if cmd[-2:] == ["logcat", "-d"] and kwargs.get("errors") != "replace":
                raise UnicodeDecodeError("utf-8", b"\xc0", 0, 1, "invalid start byte")
            return MagicMock(
                returncode=0,
                stdout=(
                    "07-01 18:58:58.314 I ZGSDK.AutoDetector: "
                    "最终判断: max聚合（自动化检测确认）\n"
                ),
                stderr="",
            )

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"), \
             patch("adb_pusher.subprocess.run", side_effect=fake_run):
            fields = extract_logcat_fields()

        assert fields["ok"] is True
        assert fields["最终判断"] == "max聚合（自动化检测确认）"
