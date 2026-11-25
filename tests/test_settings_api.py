from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web import settings_api


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    env = tmp_path / ".env"
    env.write_text('GEMINI_API_KEY=""\n', encoding="utf-8")
    monkeypatch.setattr(settings_api, "ENV_PATH", env)
    for key in settings_api.FIELDS_BY_KEY:
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    app.include_router(settings_api.build_router())
    return TestClient(app)


def test_save_writes_env_and_never_returns_secrets(client: TestClient) -> None:
    res = client.put("/api/settings", json={"STORE_NAME": "테스트샵", "GEMINI_API_KEY": "AIzaRealKey"})
    assert res.status_code == 200
    text = settings_api.ENV_PATH.read_text(encoding="utf-8")
    assert 'STORE_NAME="테스트샵"' in text and 'GEMINI_API_KEY="AIzaRealKey"' in text

    fields = {f["key"]: f for f in client.get("/api/settings").json()["fields"]}
    assert fields["GEMINI_API_KEY"]["is_set"] is True
    assert fields["GEMINI_API_KEY"]["value"] == ""
    assert fields["STORE_NAME"]["value"] == "테스트샵"

    # 비워 둔 비밀 값은 기존 값을 지우지 않는다.
    assert client.put("/api/settings", json={"GEMINI_API_KEY": ""}).json()["saved"] == []
    assert 'GEMINI_API_KEY="AIzaRealKey"' in settings_api.ENV_PATH.read_text(encoding="utf-8")


def test_rejects_unknown_keys_and_injection(client: TestClient) -> None:
    assert client.put("/api/settings", json={"EVIL": "1"}).status_code == 400
    assert client.put("/api/settings", json={"STORE_NAME": 'a"\nGEMINI_API_KEY="x'}).status_code == 400
    assert client.put("/api/settings", json={"NAVER_CAFE_MENU_ID": "12a"}).status_code == 400


def test_postiz_app_keys_flag_restart(client: TestClient) -> None:
    assert client.put("/api/settings", json={"X_API_KEY": "k"}).json()["postiz_restart_needed"] is True


def test_settings_are_local_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_api, "ENV_PATH", tmp_path / ".env")
    app = FastAPI()
    app.include_router(settings_api.build_router())
    remote = TestClient(app, client=("192.168.0.10", 5000))
    assert remote.get("/api/settings").status_code == 403
    assert remote.put("/api/settings", json={"STORE_NAME": "x"}).status_code == 403
