import web_precheck
from auto_asana.main import AsanaPrecheckTask


def test_load_today_precheck_tasks_serializes_dataclasses(monkeypatch):
    task = AsanaPrecheckTask(
        gid="task-1",
        name="聚合/动作适配com.example.game",
        package_name="com.example.game",
        up2_appid="app-1",
        gp_link="https://play.google.com/store/apps/details?id=com.example.game",
        notes="notes",
        completed=False,
        permalink_url="https://app.asana.com/task-1",
    )
    monkeypatch.setattr(web_precheck, "build_asana_client", lambda pat: "client")
    monkeypatch.setattr(
        web_precheck,
        "get_asana_tasks_for_date",
        lambda client, project_gid, today: {
            "section_name": "7.30执行",
            "section_gid": "section-1",
            "tasks": [task],
        },
    )

    result = web_precheck.load_today_precheck_tasks("project-1", "pat")

    assert result["section_name"] == "7.30执行"
    assert result["tasks"][0]["package_name"] == "com.example.game"


def test_run_web_precheck_installs_launches_and_comments(monkeypatch):
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda value, **kwargs: {
            "code": "HAS_ADS",
            "package_name": "com.example.game",
            "title": "检测到包含广告",
        },
    )
    monkeypatch.setattr(
        web_precheck,
        "install_google_play_app",
        lambda package_name, on_progress=None: {
            "ok": True,
            "code": "INSTALLED",
            "message": "安装完成",
        },
    )
    monkeypatch.setattr(
        web_precheck,
        "run_app_launch_precheck",
        lambda package_name, observation_seconds, on_progress=None: {
            "ok": True,
            "code": "LAUNCH_OK",
            "message": "未闪退",
        },
    )
    monkeypatch.setattr(web_precheck, "build_asana_client", lambda pat: "client")
    comment_calls = []
    monkeypatch.setattr(
        web_precheck,
        "add_precheck_comment_once",
        lambda client, task_gid, result: comment_calls.append(result) or False,
    )

    result = web_precheck.run_web_precheck(
        "com.example.game",
        auto_install=True,
        launch_check=True,
        observation_seconds=10,
        task_gid="task-1",
        asana_pat="pat",
    )

    assert result["install_result"]["code"] == "INSTALLED"
    assert result["launch_result"]["code"] == "LAUNCH_OK"
    assert result["asana_comment"]["attempted"] is True
    assert comment_calls[0]["code"] == "HAS_ADS"


def test_run_web_precheck_downloads_unlabeled_page_for_manual_review(monkeypatch):
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda value, **kwargs: {
            "code": "NO_ADS_OR_IAP",
            "continue_adaptation": True,
            "package_name": "com.example.review",
            "title": "未发现广告或应用内购标识（待人工确认）",
        },
    )
    monkeypatch.setattr(
        web_precheck,
        "install_google_play_app",
        lambda package_name, on_progress=None: {
            "ok": True,
            "code": "INSTALLED",
            "message": "安装完成",
        },
    )
    monkeypatch.setattr(
        web_precheck,
        "run_app_launch_precheck",
        lambda package_name, observation_seconds, on_progress=None: {
            "ok": True,
            "code": "LAUNCH_OK",
            "message": "未闪退",
        },
    )

    result = web_precheck.run_web_precheck(
        "com.example.review",
        auto_install=True,
        launch_check=True,
        observation_seconds=10,
    )

    assert result["install_result"]["code"] == "INSTALLED"
    assert result["launch_result"]["code"] == "LAUNCH_OK"


def test_run_web_precheck_automatically_installs_apkcombo_package(monkeypatch):
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda value, **kwargs: {
            "code": "APKCOMBO_AVAILABLE",
            "continue_adaptation": False,
            "package_name": "com.example.restricted",
            "title": "Google Play 无法下载，但 APKCombo 有包",
        },
    )
    calls = []
    monkeypatch.setattr(
        web_precheck,
        "download_and_install_apkcombo",
        lambda package_name, on_progress=None: calls.append(package_name) or {
            "ok": True,
            "code": "APKCOMBO_INSTALLED",
            "message": "安装完成",
        },
    )
    monkeypatch.setattr(
        web_precheck,
        "run_app_launch_precheck",
        lambda package_name, observation_seconds, on_progress=None: {
            "ok": True,
            "code": "LAUNCH_OK",
            "message": "未闪退",
        },
    )

    result = web_precheck.run_web_precheck(
        "com.example.restricted",
        auto_install=True,
        launch_check=True,
    )

    assert calls == ["com.example.restricted"]
    assert result["install_result"]["code"] == "APKCOMBO_INSTALLED"
    assert result["launch_result"]["code"] == "LAUNCH_OK"


def test_run_web_precheck_forces_historical_failure_into_apkcombo(monkeypatch):
    google_calls = []
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda *args, **kwargs: google_calls.append(args),
    )
    apkcombo_calls = []
    monkeypatch.setattr(
        web_precheck,
        "download_and_install_apkcombo",
        lambda package_name, on_progress=None: apkcombo_calls.append(package_name) or {
            "ok": True,
            "code": "APKCOMBO_INSTALLED",
            "message": "安装完成",
        },
    )

    result = web_precheck.run_web_precheck(
        "https://play.google.com/store/apps/details?id=com.example.retry",
        auto_install=True,
        launch_check=False,
        force_apkcombo=True,
    )

    assert google_calls == []
    assert apkcombo_calls == ["com.example.retry"]
    assert result["install_result"]["code"] == "APKCOMBO_INSTALLED"


def test_run_web_precheck_never_installs_japanese_blacklist(monkeypatch):
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda value, **kwargs: {
            "code": "JAPANESE_PACKAGE",
            "continue_adaptation": False,
            "package_name": "jp.co.barows.kenshowalkprotect",
            "title": "检测到日本包体",
        },
    )
    install_calls = []
    monkeypatch.setattr(
        web_precheck,
        "install_google_play_app",
        lambda *args, **kwargs: install_calls.append(args),
    )
    backend_calls = []
    monkeypatch.setattr(
        web_precheck,
        "submit_precheck_blacklist_via_api",
        lambda result, **kwargs: backend_calls.append((result, kwargs)) or {
            "ok": True,
            "code": "PRECHECK_BLACKLIST_SUBMITTED",
            "message": "已提交并刷新缓存",
        },
    )

    result = web_precheck.run_web_precheck(
        "jp.co.barows.kenshowalkprotect",
        auto_install=True,
        backend_api_url="http://example.test/cp_adapt/list",
        backend_x_token="x",
        backend_token="fixed",
    )

    assert result["code"] == "JAPANESE_PACKAGE"
    assert result["backend_blacklist"]["ok"] is True
    assert backend_calls[0][1]["x_token"] == "x"
    assert install_calls == []


def test_run_web_precheck_submits_all_network_no_package_without_install(monkeypatch):
    monkeypatch.setattr(
        web_precheck,
        "run_google_play_precheck",
        lambda value, **kwargs: {
            "code": "ALL_NETWORK_NO_PACKAGE",
            "continue_adaptation": False,
            "package_name": "com.no.package.game",
            "title": "全网无包",
        },
    )
    install_calls = []
    monkeypatch.setattr(
        web_precheck,
        "install_google_play_app",
        lambda *args, **kwargs: install_calls.append(args),
    )
    backend_calls = []
    monkeypatch.setattr(
        web_precheck,
        "submit_precheck_blacklist_via_api",
        lambda result, **kwargs: backend_calls.append((result, kwargs)) or {
            "ok": True,
            "code": "PRECHECK_BLACKLIST_SUBMITTED",
            "message": "已提交并刷新缓存",
        },
    )

    result = web_precheck.run_web_precheck(
        "com.no.package.game",
        auto_install=True,
        backend_api_url="http://example.test/cp_adapt/list",
        backend_x_token="x",
        backend_token="fixed",
    )

    assert result["code"] == "ALL_NETWORK_NO_PACKAGE"
    assert result["backend_blacklist"]["ok"] is True
    assert backend_calls[0][0]["package_name"] == "com.no.package.game"
    assert install_calls == []


def test_comment_result_prefers_launch_crash_over_page_result():
    result = web_precheck.comment_result_for_precheck({
        "code": "HAS_ADS",
        "package_name": "com.example.game",
        "launch_result": {
            "ok": False,
            "code": "APP_CRASHED",
            "message": "包体闪退，暂不适配",
            "summary": "FATAL EXCEPTION",
        },
    })

    assert result["code"] == "APP_CRASHED"
    assert "FATAL EXCEPTION" in result["detail"]
