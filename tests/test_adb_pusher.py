import pytest
from unittest.mock import patch, MagicMock, call
import subprocess
import json


@pytest.mark.parametrize(
    "placeholder",
    ["未找到", "未提取到", "暂未找到", "暂未提取到", "暂未检测到"],
)
def test_normalize_optional_parameter_omits_detector_placeholders(placeholder):
    from adb_pusher import normalize_optional_parameter

    assert normalize_optional_parameter(placeholder) == ""


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


class TestAdbConnectionState:
    def test_distinguishes_unauthorized_device(self):
        from adb_pusher import get_adb_connection_state

        with patch(
            "adb_pusher._run_adb",
            return_value=MagicMock(
                returncode=0,
                stdout="List of devices attached\nABC123\tunauthorized\n",
            ),
        ):
            assert get_adb_connection_state() == "unauthorized"

    def test_prefers_an_online_device_when_other_device_is_offline(self):
        from adb_pusher import get_adb_connection_state

        with patch(
            "adb_pusher._run_adb",
            return_value=MagicMock(
                returncode=0,
                stdout=(
                    "List of devices attached\n"
                    "OLD\toffline\n"
                    "ACTIVE\tdevice\n"
                ),
            ),
        ):
            assert get_adb_connection_state() == "device"

    def test_returns_false_when_adb_not_found(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            from adb_pusher import check_device
            assert check_device() is False


class TestAppBitness:
    def test_reports_running_64_bit_process_and_installed_abi(self):
        from adb_pusher import get_app_bitness

        responses = [
            MagicMock(
                returncode=0,
                stdout="primaryCpuAbi=arm64-v8a\nsecondaryCpuAbi=null\n",
                stderr="",
            ),
            MagicMock(returncode=0, stdout="12345\n", stderr=""),
            MagicMock(
                returncode=0,
                stdout="/system/bin/app_process64\n",
                stderr="",
            ),
        ]
        with patch("adb_pusher._run_adb", side_effect=responses):
            ok, message = get_app_bitness("com.example.game")

        assert ok is True
        assert "64 位运行" in message
        assert "arm64-v8a" in message

    def test_reports_32_bit_installed_app_when_not_running(self):
        from adb_pusher import get_app_bitness

        responses = [
            MagicMock(
                returncode=0,
                stdout="primaryCpuAbi=armeabi-v7a\nsecondaryCpuAbi=null\n",
                stderr="",
            ),
            MagicMock(returncode=1, stdout="", stderr=""),
        ]
        with patch("adb_pusher._run_adb", side_effect=responses):
            ok, message = get_app_bitness("com.example.game")

        assert ok is True
        assert message.startswith("32 位")
        assert "应用未运行" in message

    def test_does_not_guess_when_app_has_no_native_abi_and_is_not_running(self):
        from adb_pusher import get_app_bitness

        responses = [
            MagicMock(
                returncode=0,
                stdout="primaryCpuAbi=null\nsecondaryCpuAbi=null\n",
                stderr="",
            ),
            MagicMock(returncode=1, stdout="", stderr=""),
        ]
        with patch("adb_pusher._run_adb", side_effect=responses):
            ok, message = get_app_bitness("com.example.game")

        assert ok is True
        assert "需启动后确认" in message

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

    def test_install_multiple_uses_long_timeout(self, tmp_path):
        from adb_pusher import _install_apks

        apk_paths = [
            tmp_path / "base.apk",
            tmp_path / "config.arm64_v8a.apk",
            tmp_path / "config.en.apk",
            tmp_path / "config.xxhdpi.apk",
        ]
        for path in apk_paths:
            path.write_text("fake apk")

        with patch(
            "adb_pusher._run_adb",
            return_value=MagicMock(returncode=0, stdout="Success", stderr=""),
        ) as mock_run:
            ok, msg = _install_apks([str(path) for path in apk_paths])

        assert ok is True
        assert "4 个 APK" in msg
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 120


class TestInstallAcceptance:
    def test_extracts_package_from_xapk_manifest(self, tmp_path):
        import zipfile
        from adb_pusher import extract_package_name_from_artifact

        artifact = tmp_path / "game.xapk"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps({"package_name": "com.example.game"}),
            )
            archive.writestr("base.apk", b"fake")

        assert extract_package_name_from_artifact(str(artifact)) == "com.example.game"

    def test_verifies_uid_and_launcher(self):
        from adb_pusher import verify_installed_app

        resolved = MagicMock(
            returncode=0,
            stdout="com.example.game/.MainActivity\n",
            stderr="",
        )
        with patch("adb_pusher.is_package_installed", return_value=True), \
             patch("adb_pusher.get_app_uid", return_value=(True, "10123")), \
             patch("adb_pusher._run_adb", return_value=resolved):
            result = verify_installed_app("com.example.game")

        assert result["ok"] is True
        assert result["uid"] == "10123"
        assert result["launcher"].endswith(".MainActivity")

    def test_ambiguous_install_is_reconciled_with_phone(self):
        from adb_pusher import push_apk_with_acceptance

        accepted = {
            "ok": True,
            "uid": "10123",
            "launcher": "com.example.game/.MainActivity",
        }
        with patch(
            "adb_pusher.push_apk",
            return_value=(False, "安装超时：已等待 90 秒"),
        ), patch(
            "adb_pusher.wait_for_package_install_confirmation", return_value=True
        ), patch("adb_pusher.verify_installed_app", return_value=accepted):
            ok, message = push_apk_with_acceptance(
                "/tmp/game.xapk",
                expected_package="com.example.game",
            )

        assert ok is True
        assert "手机已确认安装完成" in message
        assert "UID 10123" in message


class TestMaxDebuggerShareParsing:
    def test_uses_first_id_of_each_type_and_preserves_order(self):
        from adb_pusher import parse_max_debugger_ad_units

        text = """
---------- first_inter ----------
Identifier - d247de3c04245cc9
Format     - INTER
---------- second_inter ----------
Identifier - 3343cc667927214f
Format     - INTER
---------- first_reward ----------
Identifier - 49ccb1123804ca51
Format     - REWARDED
---------- second_reward ----------
Identifier - bccf27bfd37e10c9
Format     - REWARDED
"""
        result = parse_max_debugger_ad_units(text)

        assert result["ok"] is True
        assert result["interstitial_id"] == "d247de3c04245cc9"
        assert result["rewarded_id"] == "49ccb1123804ca51"
        assert result["interstitial_ids"] == [
            "d247de3c04245cc9",
            "3343cc667927214f",
        ]
        assert result["rewarded_ids"] == [
            "49ccb1123804ca51",
            "bccf27bfd37e10c9",
        ]

    def test_ignores_banner_units(self):
        from adb_pusher import parse_max_debugger_ad_units

        result = parse_max_debugger_ad_units(
            "Identifier - banner-id\nFormat - BANNER\n"
        )

        assert result["ok"] is False

    def test_install_multiple_timeout_has_short_message(self, tmp_path):
        from adb_pusher import _install_apks

        apk_paths = [tmp_path / "base.apk", tmp_path / "config.en.apk"]
        for path in apk_paths:
            path.write_text("fake apk")

        with patch(
            "adb_pusher._run_adb",
            side_effect=subprocess.TimeoutExpired("adb install-multiple", 90),
        ):
            ok, msg = _install_apks([str(path) for path in apk_paths])

        assert ok is False
        assert "拆分 APK 安装超时" in msg
        assert str(tmp_path) not in msg

    def test_delayed_package_manager_confirmation_recovers_timeout(self):
        from adb_pusher import wait_for_package_install_confirmation

        with patch(
            "adb_pusher.is_package_installed", side_effect=[False, True]
        ), patch("adb_pusher.time.sleep") as mock_sleep:
            installed = wait_for_package_install_confirmation(
                "com.example.delayed",
                timeout_seconds=5,
                poll_interval_seconds=0,
            )

        assert installed is True
        mock_sleep.assert_called_once_with(0)


class TestApkDownloadUrl:
    def test_builds_apkcombo_search_url_from_google_play_url(self):
        from adb_pusher import build_apkcombo_search_url

        gp_url = "https://play.google.com/store/apps/details?id=com.Lightneer.BazookaBoy"

        assert build_apkcombo_search_url(gp_url) == (
            "https://apkcombo.com/search/"
            "https:%2F%2Fplay.google.com%2Fstore%2Fapps%2Fdetails%3F"
            "id=com.lightneer.bazookaboy"
            "#gsc.tab=0&gsc.q=https%3A%2F%2Fplay.google.com%2Fstore%2Fapps%2F"
            "details%3Fid%3Dcom.lightneer.bazookaboy&gsc.sort="
        )

    def test_builds_apkcombo_search_url_from_package(self):
        from adb_pusher import build_apkcombo_search_url

        assert build_apkcombo_search_url("com.Example.Game") == (
            "https://apkcombo.com/search/com.example.game"
            "#gsc.tab=0&gsc.q=com.example.game&gsc.sort="
        )

    def test_detects_xapk_url_with_query_and_uppercase_extension(self):
        from adb_pusher import is_apk_download_url

        assert is_apk_download_url("https://cdn.example.com/game.XAPK?token=abc")

    def test_infers_filename_from_url_path(self):
        from adb_pusher import download_artifact_filename

        assert download_artifact_filename(
            "https://cdn.example.com/path/game%20name.XAPK?token=abc"
        ) == "game name.XAPK"


class TestGooglePlayPrecheck:
    def test_apkcombo_exact_search_without_redirect_is_not_found(self):
        from adb_pusher import inspect_apkcombo_package

        with patch(
            "adb_pusher._apkcombo_fetch_text",
            return_value=(
                "<title>search</title>",
                "https://apkcombo.com/search/com.example.missing",
            ),
        ):
            result = inspect_apkcombo_package(
                "com.example.missing", attempts=1, retry_interval_seconds=0
            )

        assert result["code"] == "APKCOMBO_NOT_FOUND"
        assert result["available"] is False

    def test_apkcombo_requires_real_download_variant(self):
        from adb_pusher import inspect_apkcombo_package

        with patch(
            "adb_pusher._apkcombo_fetch_text",
            side_effect=[
                (
                    '<a href="/game/com.example.game/download/apk">Download APK</a>',
                    "https://apkcombo.com/game/com.example.game/",
                ),
                (
                    '<a href="/r2?u=file" class="variant">Download</a>',
                    "https://apkcombo.com/game/com.example.game/download/apk",
                ),
            ],
        ):
            result = inspect_apkcombo_package(
                "com.example.game", attempts=1, retry_interval_seconds=0
            )

        assert result["code"] == "APKCOMBO_AVAILABLE"
        assert result["available"] is True
        assert result["artifact_url"] == "https://apkcombo.com/r2?u=file"

    def test_apkcombo_extracts_current_signed_download_link(self):
        from adb_pusher import inspect_apkcombo_package

        signed_link = (
            "https://apkcombo.com/d?u=signed-value&amp;fp=abc&amp;"
            "package_name=com.example.game"
        )
        with patch(
            "adb_pusher._apkcombo_fetch_text",
            side_effect=[
                (
                    '<a href="/game/com.example.game/download/xapk">Download XAPK</a>',
                    "https://apkcombo.com/game/com.example.game/",
                ),
                (
                    f'<a class="variant" href="{signed_link}">Download</a>',
                    "https://apkcombo.com/game/com.example.game/download/xapk",
                ),
            ],
        ):
            result = inspect_apkcombo_package(
                "com.example.game", attempts=1, retry_interval_seconds=0
            )

        assert result["code"] == "APKCOMBO_AVAILABLE"
        assert result["artifact_url"].startswith("https://apkcombo.com/d?u=")
        assert "&amp;" not in result["artifact_url"]

    def test_apkcombo_auto_install_verifies_target_package(self):
        from adb_pusher import download_and_install_apkcombo

        inspected = {
            "available": True,
            "code": "APKCOMBO_AVAILABLE",
            "artifact_url": "https://apkcombo.com/d?u=signed",
            "download_url": "https://apkcombo.com/game/com.example.game/download/xapk",
        }
        with patch("adb_pusher.is_package_installed", side_effect=[False, True]), \
             patch("adb_pusher.inspect_apkcombo_package", return_value=inspected), \
             patch("adb_pusher.download_and_install", return_value=(True, "安装成功")) as install:
            result = download_and_install_apkcombo("com.example.game")

        assert result["ok"] is True
        assert result["code"] == "APKCOMBO_INSTALLED"
        install.assert_called_once_with(
            inspected["artifact_url"],
            on_progress=install.call_args.kwargs["on_progress"],
            referer=inspected["download_url"],
        )

    def test_apkcombo_auto_install_falls_back_to_browser_on_403(self):
        from adb_pusher import download_and_install_apkcombo

        inspected = {
            "available": True,
            "code": "APKCOMBO_AVAILABLE",
            "artifact_url": "https://apkcombo.com/d?u=signed",
            "download_url": "https://apkcombo.com/game/com.example.game/download/xapk",
        }
        with patch("adb_pusher.is_package_installed", side_effect=[False, True]), \
             patch("adb_pusher.inspect_apkcombo_package", return_value=inspected), \
             patch(
                 "adb_pusher.download_and_install",
                 return_value=(False, "下载失败（网络错误）: HTTP Error 403: Forbidden"),
             ), \
             patch(
                 "adb_pusher._download_apkcombo_via_browser",
                 return_value=(True, "Chrome 下载并安装完成"),
             ) as browser_download:
            result = download_and_install_apkcombo("com.example.game")

        assert result["ok"] is True
        assert result["code"] == "APKCOMBO_INSTALLED"
        browser_download.assert_called_once_with(
            inspected["artifact_url"],
            "com.example.game",
            on_progress=None,
        )

    def test_apkcombo_install_timeout_is_reconciled_with_package_manager(self):
        from adb_pusher import download_and_install_apkcombo

        inspected = {
            "available": True,
            "code": "APKCOMBO_AVAILABLE",
            "artifact_url": "https://apkcombo.com/d?u=signed",
            "download_url": "https://apkcombo.com/game/com.example.game/download/xapk",
        }
        progress = []
        with patch(
            "adb_pusher.is_package_installed", side_effect=[False, True]
        ), patch(
            "adb_pusher.inspect_apkcombo_package", return_value=inspected
        ), patch(
            "adb_pusher.download_and_install",
            return_value=(False, "拆分 APK 安装超时：共 3 个 APK"),
        ):
            result = download_and_install_apkcombo(
                "com.example.game",
                on_progress=progress.append,
            )

        assert result["ok"] is True
        assert result["code"] == "APKCOMBO_INSTALLED"
        assert "手机已确认安装完成" in result["message"]
        assert any("最终安装状态" in message for message in progress)

    def test_apkcombo_dynamic_download_not_found_is_unavailable(self):
        from adb_pusher import inspect_apkcombo_package

        dynamic_page = """
            <script>
            var xid = "abc123"
            fetchData("/game/com.example.game/" + xid + "/dl")
            </script>
        """
        with patch(
            "adb_pusher._apkcombo_fetch_text",
            side_effect=[
                (
                    '<a href="/game/com.example.game/download/apk">Download APK</a>',
                    "https://apkcombo.com/game/com.example.game/",
                ),
                (
                    dynamic_page,
                    "https://apkcombo.com/game/com.example.game/download/apk",
                ),
                (
                    "Sorry, the application was not found",
                    "https://apkcombo.com/game/com.example.game/abc123/dl",
                ),
            ],
        ):
            result = inspect_apkcombo_package(
                "com.example.game", attempts=1, retry_interval_seconds=0
            )

        assert result["code"] == "APKCOMBO_NOT_FOUND"
        assert result["available"] is False
        assert "application was not found" in result["message"]

    def test_apkcombo_unavailable_converts_play_restriction_to_all_network_missing(self):
        from adb_pusher import apply_apkcombo_check_to_precheck_result

        with patch(
            "adb_pusher.inspect_apkcombo_package",
            return_value={
                "code": "APKCOMBO_NOT_FOUND",
                "available": False,
                "message": "APKCombo 搜索未找到完全一致的包名",
            },
        ):
            result = apply_apkcombo_check_to_precheck_result({
                "code": "COUNTRY_UNSUPPORTED",
                "title": "所在国家或地区不支持",
                "package_name": "com.example.game",
                "source": "UI 控件",
                "evidence": ["发现地区限制"],
            })

        assert result["code"] == "ALL_NETWORK_NO_PACKAGE"
        assert result["continue_adaptation"] is False
        assert "全网无包" in result["detail"]

    def test_resolves_google_play_url_or_raw_package(self):
        from adb_pusher import resolve_google_play_package

        assert resolve_google_play_package(
            "https://play.google.com/store/apps/details?id=com.example.game"
        ) == "com.example.game"
        assert resolve_google_play_package("com.example.game") == "com.example.game"
        assert resolve_google_play_package("not a package") == ""

    def test_classifies_contains_ads_and_iap_as_continue(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "Contains ads · In-app purchases",
            "Install",
        ])

        assert result["code"] == "HAS_ADS"
        assert result["contains_ads"] is True
        assert result["contains_iap"] is True
        assert result["continue_adaptation"] is True

    def test_classifies_iap_only_as_blacklist(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(["应用内购商品", "安装"])

        assert result["code"] == "IAP_ONLY"
        assert result["continue_adaptation"] is False

    def test_classifies_japanese_package_as_blacklist_before_ads(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(
            [
                "懸賞ウォーク 毎日の歩数でポイントが貯まる",
                "このアプリについて",
                "広告を含む",
                "インストール",
            ],
            "jp.co.barows.kenshowalkprotect",
        )

        assert result["code"] == "JAPANESE_PACKAGE"
        assert result["continue_adaptation"] is False
        assert result["is_japanese_package"] is True
        assert "日文" in result["detail"]

    def test_jp_letters_inside_one_segment_do_not_blacklist(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(
            ["日本語のゲームです", "インストール", "Contains ads"],
            "com.example.jpuzzle",
        )

        assert result["code"] == "HAS_ADS"

    def test_jp_package_without_japanese_page_text_does_not_blacklist(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(
            ["English Game", "Contains ads", "Install"],
            "jp.example.englishgame",
        )

        assert result["code"] == "HAS_ADS"

    def test_classifies_loaded_page_without_labels_as_manual_review(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(["游戏名称", "开发者", "安装"])

        assert result["code"] == "NO_ADS_OR_IAP"
        assert result["page_ready"] is True
        assert result["continue_adaptation"] is True

    def test_does_not_blacklist_before_page_is_known_to_be_loaded(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts(["正在加载..."])

        assert result["code"] == "UNKNOWN"
        assert result["continue_adaptation"] is None

    def test_item_not_found_is_google_no_package_and_stops_workflow(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "Item not found",
            "Contains ads",
        ])

        assert result["code"] == "GOOGLE_NO_PACKAGE"
        assert result["continue_adaptation"] is False
        assert result["item_found"] is False
        assert "Item not found" in result["detail"]

    def test_incompatibility_blocks_download_when_app_contains_ads(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "Contains ads · In-app purchases",
            "Your device isn't compatible with this version.",
        ])

        assert result["code"] == "DEVICE_UNSUPPORTED"
        assert result["continue_adaptation"] is False

    def test_iap_only_has_priority_over_device_incompatibility(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "In-app purchases",
            "Your device isn't compatible with this version.",
        ])

        assert result["code"] == "IAP_ONLY"
        assert result["contains_ads"] is False
        assert result["contains_iap"] is True
        assert result["device_supported"] is False
        assert result["continue_adaptation"] is False

    def test_country_unavailable_without_monetization_stops_download(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "This item isn't available in your country",
        ])

        assert result["code"] == "COUNTRY_UNSUPPORTED"
        assert result["country_supported"] is False
        assert result["continue_adaptation"] is False

    def test_iap_only_has_priority_over_country_unavailable(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "In-app purchases",
            "This item isn't available in your country",
        ])

        assert result["code"] == "IAP_ONLY"
        assert result["country_supported"] is False
        assert result["continue_adaptation"] is False

    def test_device_unavailable_without_monetization_stops_download(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "Your device isn't compatible with this version.",
        ])

        assert result["code"] == "DEVICE_UNSUPPORTED"
        assert result["device_supported"] is False
        assert result["continue_adaptation"] is False

    def test_country_unavailable_is_recognized(self):
        from adb_pusher import classify_google_play_page_texts

        result = classify_google_play_page_texts([
            "Contains ads",
            "This item isn't available in your country",
        ])

        assert result["code"] == "COUNTRY_UNSUPPORTED"

    def test_parses_text_and_content_description_from_ui_xml(self):
        from adb_pusher import parse_uiautomator_texts

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy><node text="包含广告" content-desc="" />'
            '<node text="" content-desc="安装" /></hierarchy>'
        )

        assert parse_uiautomator_texts(xml) == ["包含广告", "安装"]

    def test_parses_clickable_node_bounds_for_install_button(self):
        from adb_pusher import parse_uiautomator_nodes

        xml = (
            '<hierarchy><node text="Install" content-desc="" '
            'resource-id="com.android.vending:id/0_resource_name_obfuscated" '
            'clickable="true" enabled="true" bounds="[100,300][400,420]" />'
            '</hierarchy>'
        )

        nodes = parse_uiautomator_nodes(xml)
        assert nodes == [{
            "text": "Install",
            "content_desc": "",
            "resource_id": "com.android.vending:id/0_resource_name_obfuscated",
            "clickable": True,
            "enabled": True,
            "bounds": "[100,300][400,420]",
        }]

    def test_dismisses_notification_permission_with_dont_allow(self):
        from adb_pusher import dismiss_notification_permission_dialog

        prompt = {
            "text": "Allow Demo Game to send you notifications?",
            "content_desc": "",
            "enabled": True,
            "bounds": "[50,100][450,200]",
        }
        deny = {
            "text": "Don't allow",
            "content_desc": "",
            "enabled": True,
            "bounds": "[80,300][420,380]",
        }
        with patch(
            "adb_pusher.collect_device_ui_nodes", return_value=[prompt, deny]
        ), patch("adb_pusher._tap_ui_node", return_value=True) as mock_tap:
            result = dismiss_notification_permission_dialog()

        assert result["dismissed"] is True
        assert result["code"] == "NOTIFICATION_PERMISSION_DISMISSED"
        mock_tap.assert_called_once_with(deny)

    def test_does_not_deny_an_unrelated_permission_dialog(self):
        from adb_pusher import dismiss_notification_permission_dialog

        camera_prompt = {
            "text": "Allow Demo Game to take pictures and record video?",
            "content_desc": "",
            "enabled": True,
            "bounds": "[50,100][450,200]",
        }
        deny = {
            "text": "Don't allow",
            "content_desc": "",
            "enabled": True,
            "bounds": "[80,300][420,380]",
        }
        with patch(
            "adb_pusher.collect_device_ui_nodes", return_value=[camera_prompt, deny]
        ), patch("adb_pusher._tap_ui_node", return_value=True) as mock_tap:
            result = dismiss_notification_permission_dialog()

        assert result["dismissed"] is False
        assert result["code"] == "NO_NOTIFICATION_PERMISSION_DIALOG"
        mock_tap.assert_not_called()

    def test_auto_install_clicks_install_and_waits_for_package(self):
        from adb_pusher import install_google_play_app

        install_node = {
            "text": "Install",
            "content_desc": "",
            "enabled": True,
            "bounds": "[100,300][400,420]",
        }
        with patch(
            "adb_pusher.is_package_installed",
            side_effect=[False, False, True],
        ), patch(
            "adb_pusher.collect_device_ui_nodes",
            return_value=[install_node],
        ), patch("adb_pusher._tap_ui_node", return_value=True) as mock_tap:
            result = install_google_play_app(
                "com.example.game",
                timeout_seconds=10,
                poll_interval_seconds=0,
            )

        assert result["ok"] is True
        assert result["code"] == "INSTALLED"
        mock_tap.assert_called_once_with(install_node)

    def test_auto_install_stops_on_country_restriction_without_ads_label(self):
        from adb_pusher import install_google_play_app

        country_node = {
            "text": "This item isn't available in your country.",
            "content_desc": "",
            "enabled": True,
            "bounds": "[20,300][900,420]",
        }
        with patch(
            "adb_pusher.is_package_installed", return_value=False
        ), patch(
            "adb_pusher.collect_device_ui_nodes", return_value=[country_node]
        ), patch("adb_pusher._tap_ui_node") as mock_tap:
            result = install_google_play_app(
                "com.gpshopper.moneygram",
                timeout_seconds=1,
                poll_interval_seconds=0,
            )

        assert result["ok"] is False
        assert result["code"] == "COUNTRY_UNSUPPORTED"
        mock_tap.assert_not_called()

    def test_auto_install_stops_when_google_requests_verification(self):
        from adb_pusher import install_google_play_app

        auth_node = {
            "text": "Verify it's you",
            "content_desc": "",
            "enabled": True,
            "bounds": "[10,10][100,100]",
        }
        with patch("adb_pusher.is_package_installed", return_value=False), \
             patch("adb_pusher.collect_device_ui_nodes", return_value=[auth_node]):
            result = install_google_play_app(
                "com.example.game",
                timeout_seconds=10,
                poll_interval_seconds=0,
            )

        assert result["ok"] is False
        assert result["code"] == "AUTH_REQUIRED"

    def test_auto_install_can_return_immediately_after_starting_download(self):
        from adb_pusher import install_google_play_app

        install_node = {
            "text": "Install",
            "content_desc": "",
            "enabled": True,
            "bounds": "[100,300][400,420]",
        }
        with patch("adb_pusher.is_package_installed", return_value=False), \
             patch("adb_pusher.collect_device_ui_nodes", return_value=[install_node]), \
             patch("adb_pusher._tap_ui_node", return_value=True) as mock_tap:
            result = install_google_play_app(
                "com.example.largegame",
                timeout_seconds=10,
                poll_interval_seconds=0,
                return_after_start=True,
            )

        assert result["ok"] is None
        assert result["code"] == "DOWNLOAD_STARTED"
        mock_tap.assert_called_once_with(install_node)


class TestAppLaunchPrecheck:
    def test_extracts_java_crash_for_target_package(self):
        from adb_pusher import extract_package_crash_evidence

        log_text = """
        E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 1234
        E AndroidRuntime: java.lang.RuntimeException: startup failed
        E AndroidRuntime: Caused by: java.lang.IllegalStateException
        """

        result = extract_package_crash_evidence(log_text, "com.example.game")

        assert result["crashed"] is True
        assert result["crash_type"] == "JAVA_CRASH"
        assert "com.example.game" in result["summary"]

    def test_ignores_crash_from_another_package(self):
        from adb_pusher import extract_package_crash_evidence

        log_text = """
        E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.other.game, PID: 1234
        """

        result = extract_package_crash_evidence(log_text, "com.example.game")

        assert result["crashed"] is False

    def test_launch_check_reports_normal_and_force_stops_after_observation(self):
        from adb_pusher import run_app_launch_precheck

        command_result = MagicMock(returncode=0, stdout="Events injected: 1", stderr="")
        with patch("adb_pusher.is_package_installed", return_value=True), \
             patch("adb_pusher._run_adb", return_value=command_result) as mock_run, \
             patch("adb_pusher._is_app_process_running", return_value=True), \
             patch("adb_pusher._read_crash_logcat", return_value=""), \
             patch("adb_pusher.time.monotonic", side_effect=[0, 0.2, 1.2]):
            result = run_app_launch_precheck(
                "com.example.game",
                observation_seconds=1,
                poll_interval_seconds=0,
            )

        assert result["ok"] is True
        assert result["code"] == "LAUNCH_OK"
        assert mock_run.call_args_list[-1] == call(
            ["shell", "am", "force-stop", "com.example.game"], timeout=8
        )

    def test_launch_check_reports_confirmed_crash(self):
        from adb_pusher import run_app_launch_precheck

        command_result = MagicMock(returncode=0, stdout="Events injected: 1", stderr="")
        crash_log = """
        E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 4321
        """
        with patch("adb_pusher.is_package_installed", return_value=True), \
             patch("adb_pusher._run_adb", return_value=command_result), \
             patch(
                 "adb_pusher._is_app_process_running",
                 side_effect=[True, False, False],
             ), \
             patch("adb_pusher._read_crash_logcat", return_value=crash_log), \
             patch("adb_pusher.time.monotonic", side_effect=[0, 0.1, 0.2, 0.3]):
            result = run_app_launch_precheck(
                "com.example.game",
                observation_seconds=10,
                poll_interval_seconds=0,
            )

        assert result["ok"] is False
        assert result["code"] == "APP_CRASHED"
        assert result["message"] == "包体闪退，暂不适配"


class TestPackageRuntimeMonitor:
    def test_parses_focused_anr_package(self):
        from adb_pusher import parse_focused_anr_package

        text = (
            "mCurrentFocus=Window{123 u0 Application Not Responding: "
            "co.vybs.app}\n"
        )

        assert parse_focused_anr_package(text) == "co.vybs.app"

    def test_automation_keeps_selecting_wait_for_repeated_anr(self):
        from adb_pusher import PackageRuntimeMonitor

        action = {
            "dismissed": True,
            "code": "ANR_WAIT_SELECTED",
            "message": "已点击等待",
        }
        with patch(
            "adb_pusher.get_focused_anr_package",
            return_value="com.example.game",
        ), patch(
            "adb_pusher.dismiss_anr_wait_dialog",
            return_value=action,
        ) as dismiss, patch(
            "adb_pusher.time.monotonic",
            side_effect=[0, 6, 12, 18],
        ):
            monitor = PackageRuntimeMonitor(
                "com.example.game",
                auto_recover_anr=True,
                anr_repeat_threshold=2,
            )
            results = [monitor.poll(), monitor.poll(), monitor.poll()]

        assert all(result["ok"] for result in results)
        assert all(result["code"] == "APP_ANR_RECOVERY_WAIT" for result in results)
        assert dismiss.call_count == 3

    def test_process_lookup_accepts_package_child_process(self):
        from adb_pusher import _is_app_process_running

        with patch(
            "adb_pusher._run_adb",
            side_effect=[
                MagicMock(returncode=1, stdout=""),
                MagicMock(
                    returncode=0,
                    stdout=(
                        "USER PID PPID VSZ RSS WCHAN ADDR S NAME\n"
                        "u0_a1 123 1 0 0 0 0 S com.example.game:worker\n"
                    ),
                ),
            ],
        ):
            assert _is_app_process_running("com.example.game") is True

    def test_process_lookup_accepts_resumed_package_activity(self):
        from adb_pusher import _is_app_process_running

        with patch(
            "adb_pusher._run_adb",
            side_effect=[
                MagicMock(returncode=1, stdout=""),
                MagicMock(returncode=0, stdout="USER PID NAME\n"),
                MagicMock(
                    returncode=0,
                    stdout=(
                        "mResumedActivity: ActivityRecord{abc "
                        "com.example.game/.MainActivity}\n"
                    ),
                    stderr="",
                ),
            ],
        ):
            assert _is_app_process_running("com.example.game") is True

    def test_default_startup_grace_is_25_seconds(self):
        from adb_pusher import PackageRuntimeMonitor

        monitor = PackageRuntimeMonitor("com.example.game")

        assert monitor.startup_grace_seconds == 25

    def test_reports_explicit_delayed_crash_immediately(self):
        from adb_pusher import PackageRuntimeMonitor

        crash_log = """
        E AndroidRuntime: FATAL EXCEPTION: main
        E AndroidRuntime: Process: com.example.game, PID: 1234
        """
        with patch("adb_pusher._is_app_process_running", return_value=False), \
             patch("adb_pusher._read_crash_logcat", return_value=crash_log):
            result = PackageRuntimeMonitor("com.example.game").poll()

        assert result["ok"] is False
        assert result["code"] == "APP_CRASHED"
        assert "自动化检测过程中闪退" in result["message"]

    def test_requires_two_missing_polls_for_unexplained_exit(self):
        from adb_pusher import PackageRuntimeMonitor

        with patch(
            "adb_pusher._is_app_process_running",
            side_effect=[True, False, False],
        ), \
             patch("adb_pusher._read_crash_logcat", return_value=""):
            monitor = PackageRuntimeMonitor(
                "com.example.game", startup_grace_seconds=0
            )
            running = monitor.poll()
            first = monitor.poll()
            second = monitor.poll()

        assert running["code"] == "APP_RUNNING"
        assert first["ok"] is True
        assert second["ok"] is False
        assert second["code"] == "APP_EXITED_DURING_AUTOMATION"

    def test_startup_pid_delay_is_not_reported_as_crash(self):
        from adb_pusher import PackageRuntimeMonitor

        with patch("adb_pusher._is_app_process_running", return_value=False), \
             patch("adb_pusher._read_crash_logcat", return_value=""), \
             patch("adb_pusher.time.monotonic", side_effect=[0, 5, 10]):
            monitor = PackageRuntimeMonitor(
                "com.example.game", startup_grace_seconds=10
            )
            starting = monitor.poll()
            not_started = monitor.poll()

        assert starting["ok"] is True
        assert starting["code"] == "APP_STARTING"
        assert not_started["ok"] is False
        assert not_started["code"] == "APP_LAUNCH_NOT_CONFIRMED"
        assert "闪退" not in not_started["message"]

    def test_precheck_requires_two_matching_reads_before_blacklist(self):
        from adb_pusher import run_google_play_precheck

        with patch("adb_pusher.check_device", return_value=True), \
             patch(
                 "adb_pusher.open_google_play_page",
                 return_value=(True, "已打开", "com.example.game"),
             ), \
             patch(
                 "adb_pusher.collect_device_ui_texts",
                 side_effect=[["应用内购", "安装"], ["应用内购", "安装"]],
             ), \
             patch("adb_pusher.collect_device_ocr_text", return_value=""):
            result = run_google_play_precheck(
                "com.example.game",
                attempts=2,
                interval_seconds=0,
            )

        assert result["code"] == "IAP_ONLY"

    def test_single_no_ads_read_is_manual_review_not_blacklist(self):
        from adb_pusher import run_google_play_precheck

        with patch("adb_pusher.check_device", return_value=True), \
             patch(
                 "adb_pusher.open_google_play_page",
                 return_value=(True, "已打开", "com.example.game"),
             ), \
             patch("adb_pusher.collect_device_ui_texts", return_value=["安装"]), \
             patch("adb_pusher.collect_device_ocr_text", return_value=""):
            result = run_google_play_precheck(
                "com.example.game",
                attempts=1,
                interval_seconds=0,
            )

        assert result["code"] == "NO_ADS_OR_IAP"
        assert result["continue_adaptation"] is True


class TestAdbCommandBuilders:
    def test_clear_app_cache_stops_app_and_clears_data(self):
        from adb_pusher import clear_app_cache

        force_stop = MagicMock(returncode=0, stdout="", stderr="")
        clear_cache = MagicMock(returncode=0, stdout="Success\n", stderr="")
        with patch(
            "adb_pusher._run_adb",
            side_effect=[force_stop, clear_cache],
        ) as mock_run:
            ok, msg = clear_app_cache("com.example.app")

        assert ok is True
        assert msg == "缓存清除成功"
        assert mock_run.call_args_list == [
            call(["shell", "am", "force-stop", "com.example.app"], timeout=5),
            call(
                ["shell", "pm", "clear", "com.example.app"],
                timeout=8,
            ),
        ]

    def test_build_clear_cache_cmd_stops_app_and_clears_data(self):
        from adb_pusher import build_clear_cache_cmd

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"):
            cmd = build_clear_cache_cmd("com.example.app")

        assert cmd == [
            "sh",
            "-c",
            (
                "/usr/bin/adb shell am force-stop com.example.app && "
                "/usr/bin/adb shell pm clear com.example.app"
            ),
        ]

    def test_packages_to_uninstall_keeps_whitelisted_packages(self):
        from adb_pusher import packages_to_uninstall

        assert packages_to_uninstall(
            ["com.keep", "com.remove", "com.other"],
            ["com.keep", "com.other"],
        ) == ["com.remove"]

    def test_uninstall_third_party_package_reports_success(self):
        from adb_pusher import uninstall_third_party_package

        result = MagicMock(returncode=0, stdout="Success\n", stderr="")
        with patch("adb_pusher._run_adb", return_value=result) as mock_run:
            ok, message = uninstall_third_party_package("com.example.app")

        assert ok is True
        assert message == "卸载成功"
        mock_run.assert_called_once_with(
            ["shell", "pm", "uninstall", "com.example.app"],
            timeout=30,
        )

    def test_uninstall_third_party_package_rejects_invalid_name(self):
        from adb_pusher import uninstall_third_party_package

        with patch("adb_pusher._run_adb") as mock_run:
            ok, message = uninstall_third_party_package("bad package")

        assert ok is False
        assert message == "无效的应用包名"
        mock_run.assert_not_called()

    def test_build_bulk_uninstall_cmd_filters_invalid_package_names(self):
        from adb_pusher import build_bulk_uninstall_cmd

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"):
            cmd = build_bulk_uninstall_cmd(["com.good.app", "bad name", "org.ok"])

        assert cmd[:4] == ["/usr/bin/adb", "shell", "sh", "-c"]
        assert "pm uninstall com.good.app" in cmd[4]
        assert "pm uninstall org.ok" in cmd[4]
        assert "bad name" not in cmd[4]

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


class TestCancelZygoteholeInjection:
    def test_remove_package_preserves_other_entries_and_top_level_fields(self):
        from adb_pusher import remove_package_from_zygotehole_config

        original = {
            "version": 3,
            "data": [
                {"packageName": "com.current.game", "appId": "current"},
                {"packageName": "com.other.game", "appId": "other"},
            ],
        }

        updated, removed_count = remove_package_from_zygotehole_config(
            original,
            "com.current.game",
        )

        assert removed_count == 1
        assert updated == {
            "version": 3,
            "data": [{"packageName": "com.other.game", "appId": "other"}],
        }
        assert len(original["data"]) == 2

    def test_cancel_updates_device_config_then_force_stops_package(self):
        from adb_pusher import cancel_zygotehole_injection

        current = json.dumps({
            "data": [
                {"packageName": "com.current.game", "appId": "current"},
                {"packageName": "com.other.game", "appId": "other"},
            ]
        })
        read_result = MagicMock(returncode=0, stdout=current, stderr="")
        pushed_config = {}

        call_count = 0

        def fake_run(args, timeout=8):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return read_result
            if args[:1] == ["push"]:
                with open(args[1], "r", encoding="utf-8") as config_file:
                    pushed_config.update(json.load(config_file))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("adb_pusher._run_adb", side_effect=fake_run) as mock_run:
            ok, msg = cancel_zygotehole_injection("com.current.game")

        assert ok is True
        assert "保留其他配置 1 条" in msg
        assert pushed_config["data"] == [
            {"packageName": "com.other.game", "appId": "other"}
        ]
        assert mock_run.call_args_list[-1] == call(
            ["shell", "am", "force-stop", "com.current.game"],
            timeout=8,
        )

    def test_cancel_does_not_write_when_package_is_absent(self):
        from adb_pusher import cancel_zygotehole_injection

        read_result = MagicMock(
            returncode=0,
            stdout=json.dumps({"data": [{"packageName": "com.other.game"}]}),
            stderr="",
        )
        with patch("adb_pusher._run_adb", return_value=read_result) as mock_run:
            ok, msg = cancel_zygotehole_injection("com.current.game")

        assert ok is False
        assert "没有找到" in msg
        mock_run.assert_called_once()


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

    def test_tradplus_is_mapped_as_a_normal_backend_platform(self):
        from urllib.parse import parse_qs

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {
                "最终判断": "TradPlus聚合（自动化检测确认）",
                "归因平台": "Adjust",
                "插屏聚合id": "tradplus-inter",
                "激励视频聚合id": "tradplus-reward",
                "初始Activity": "com.demo.MainActivity",
            },
            "com.demo.tradplus",
        )

        prefix = "http://data_center_web_internet.hongdinghe.cn/#/CpAdaptManage/CpAdapt?"
        params = parse_qs(url.removeprefix(prefix))
        assert params["aggr_platform"] == ["tradplus"]
        assert params["aggr_chaping_id"] == ["tradplus-inter"]
        assert params["aggr_jilishipin_id"] == ["tradplus-reward"]

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

    def test_backend_url_omits_detector_placeholder_values(self):
        from urllib.parse import parse_qs, urlsplit

        from adb_pusher import build_backend_url

        url = build_backend_url(
            {
                "最终判断": "MAX聚合（自动化检测确认）",
                "归因平台": "未找到",
                "初始Activity": "未提取到",
                "插屏聚合id": "inter-1",
                "激励视频聚合id": "未知",
                "af_key": "N/A",
                "SDK列表": [
                    {"名称": "AppLovin", "key": "未找到"},
                    {"名称": "Appsflyer", "key": "null"},
                ],
            },
            "com.demo.game",
        )

        params = parse_qs(urlsplit(url.replace("/#/", "/?")).query)
        assert params["package_name"] == ["com.demo.game"]
        assert params["aggr_platform"] == ["max"]
        assert params["aggr_chaping_id"] == ["inter-1"]
        assert "attribution_platform" not in params
        assert "activity_main_page" not in params
        assert "aggr_jilishipin_id" not in params
        assert "manual_applovin_sdk_key" not in params
        assert "af_key" not in params


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

    def test_uses_ids_for_detected_tradplus_platform(self):
        from adb_pusher import parse_autodetector_fields

        lines = [
            "08-26 10:00:00 I ZGSDK.AutoDetector: 最终判断: TradPlus聚合（自动化检测确认）",
            "08-26 10:00:00 I ZGSDK.AutoDetector: AppLovin:",
            "08-26 10:00:00 I ZGSDK.AutoDetector:   激励视频聚合id: [max-reward]",
            "08-26 10:00:00 I ZGSDK.AutoDetector:   插屏聚合id: [max-inter]",
            "08-26 10:00:00 I ZGSDK.AutoDetector: TradPlus:",
            "08-26 10:00:00 I ZGSDK.AutoDetector:   激励视频聚合id: [tp-reward]",
            "08-26 10:00:00 I ZGSDK.AutoDetector:   插屏聚合id: [tp-inter]",
        ]

        fields = parse_autodetector_fields(lines)

        assert fields["最终判断"].startswith("TradPlus聚合")
        assert fields["激励视频聚合id"] == "tp-reward"
        assert fields["插屏聚合id"] == "tp-inter"


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

    def test_forwards_autodetector_adb_lines_to_automation_log(self):
        from adb_pusher import extract_logcat_fields

        output = (
            "I OtherTag: ignore me\n"
            "I ZGSDK.AutoDetector: 最终判断: max聚合\n"
        )
        received = []
        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"), \
             patch(
                 "adb_pusher.subprocess.run",
                 return_value=MagicMock(returncode=0, stdout=output, stderr=""),
             ):
            extract_logcat_fields(on_line=received.append)

        assert received == ["I ZGSDK.AutoDetector: 最终判断: max聚合"]

    def test_retries_one_online_logcat_timeout_then_recovers(self):
        from adb_pusher import extract_logcat_fields

        recovered = MagicMock(
            returncode=0,
            stdout="I ZGSDK.AutoDetector: 最终判断: IronSource聚合\n",
            stderr="",
        )
        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"), \
             patch("adb_pusher.get_adb_connection_state", return_value="device"), \
             patch(
                 "adb_pusher.subprocess.run",
                 side_effect=[
                     subprocess.TimeoutExpired("adb logcat", 1),
                     recovered,
                 ],
             ):
            fields = extract_logcat_fields(
                attempts=2, timeout_seconds=1, retry_delay_seconds=0
            )

        assert fields["ok"] is True
        assert fields["最终判断"] == "IronSource聚合"
        assert fields["_logcat_attempts"] == 2

    def test_persistent_online_timeout_is_marked_transient_not_empty_detection(self):
        from adb_pusher import extract_logcat_fields

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"), \
             patch("adb_pusher.get_adb_connection_state", return_value="device"), \
             patch(
                 "adb_pusher.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("adb logcat", 1),
             ):
            fields = extract_logcat_fields(
                attempts=2, timeout_seconds=1, retry_delay_seconds=0
            )

        assert fields["ok"] is False
        assert fields["_runtime_code"] == "LOGCAT_READ_TIMEOUT"
        assert fields["_adb_state"] == "device"
        assert fields["_transient"] is True
        assert "设备在线" in fields["error"]

    def test_unauthorized_device_is_reported_without_empty_parameter_misclassification(self):
        from adb_pusher import extract_logcat_fields

        with patch("adb_pusher.get_adb_path", return_value="/usr/bin/adb"), \
             patch("adb_pusher.get_adb_connection_state", return_value="unauthorized"), \
             patch(
                 "adb_pusher.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("adb logcat", 1),
             ):
            fields = extract_logcat_fields(
                attempts=2, timeout_seconds=1, retry_delay_seconds=0
            )

        assert fields["ok"] is False
        assert fields["_runtime_code"] == "ADB_UNAUTHORIZED"
        assert fields["_transient"] is False
        assert fields["_logcat_attempts"] == 1
