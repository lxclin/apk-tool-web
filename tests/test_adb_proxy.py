"""adb_proxy 与 adb_pusher 去重后的绑定回归测试。

代理不再维护自己的 ADB 命令构建、流式执行、设备检测和 zip 检测副本；
以下断言锁定代理符号必须直接引用 adb_pusher 的核心实现，
防止未来复制粘贴回潮后两份实现再次漂移。
"""

import pytest


class TestProxyReusesCoreImplementations:
    def test_run_stream_is_core_implementation(self):
        import adb_proxy
        import adb_pusher

        assert adb_proxy._run_stream is adb_pusher.run_stream

    def test_check_device_is_core_implementation(self):
        import adb_proxy
        import adb_pusher

        assert adb_proxy._check_device is adb_pusher.check_device

    @pytest.mark.parametrize(
        "proxy_name,core_name",
        [
            ("_build_push_config", "build_push_config_cmd"),
            ("_build_zygote_build", "build_zygote_build_cmd"),
            ("_build_get_uid", "build_get_uid_cmd"),
            ("_build_clear_cache", "build_clear_cache_cmd"),
            ("_build_force_stop", "build_force_stop_cmd"),
            ("_build_open_app", "build_open_app_cmd"),
        ],
    )
    def test_command_builders_are_core_implementations(
        self, proxy_name, core_name
    ):
        import adb_proxy
        import adb_pusher

        assert getattr(adb_proxy, proxy_name) is getattr(adb_pusher, core_name)

    def test_zip_contains_apks_is_shared(self):
        import adb_proxy
        import adb_pusher

        assert adb_proxy.zip_contains_apks is adb_pusher.zip_contains_apks
        # 旧内部名仍指向同一实现
        assert adb_pusher._zip_contains_apks is adb_pusher.zip_contains_apks

    def test_logcat_wrappers_use_core_stream(self):
        import adb_proxy
        import adb_pusher

        assert adb_proxy.start_logcat_stream is adb_pusher.start_logcat_stream
        assert adb_proxy.stop_logcat_stream is adb_pusher.stop_logcat_stream


class TestProxyUidExtractionParity:
    """代理 get_uid 流程改用核心 extract_uid_from_dumpsys 后行为不变。"""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("    userId=10255    pkg=com.foo", "10255"),
            ("    appId=10142", "10142"),
            ("no uid in this line", None),
        ],
    )
    def test_single_line_extraction(self, line, expected):
        import adb_proxy

        assert adb_proxy.extract_uid_from_dumpsys(line) == expected


class TestCoreZygoteBuildCommand:
    """代理 zygote_build 现在与桌面版一致：构建后串联权限修复。"""

    def test_zygote_build_includes_permission_fix(self):
        import adb_pusher

        cmd = adb_pusher.build_zygote_build_cmd("/tmp/wd")
        joined = " ".join(cmd)
        assert "zygote_build.sh" in joined
        assert "chmod" in joined

    def test_clear_cache_forces_stop_first(self):
        import adb_pusher

        cmd = adb_pusher.build_clear_cache_cmd("com.foo")
        joined = " ".join(cmd)
        assert "force-stop" in joined
        assert "pm clear" in joined
