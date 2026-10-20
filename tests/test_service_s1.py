"""서비스 대장 S1(0.24.0): 외부 API 토큰으로 가드레일·라벨·이름 변경과 원본 직접 재개를 못 한다.

docs/designs/service-registry.md §5 · 이전에는 토큰 하나로 모든 세션의 프로필을 풀고 원본을 자동 허용으로 열 수 있었다.
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from session_manager import server as S, profiles

TOK = "svc-test-token-123"
H = {"Authorization": f"Bearer {TOK}"}


@pytest.fixture
def client(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_API_TOKEN", TOK)
    return TestClient(S.create_app())       # 기본 호스트 'testclient' = 원격(비루프백)


@pytest.mark.parametrize("ep,body", [("profile", {"system_prompt": "x"}),
                                     ("labels", {"labels": ["x"]}),
                                     ("title", {"title": "x"})])
def test_token_cannot_change_session_settings(client, ep, body):
    r = client.post(f"/api/v1/sessions/abcd1234/{ep}", json=body, headers=H)
    assert r.status_code == 403 and r.json()["error"] == "local_only"
    assert profiles.get("abcd1234") == {}


def test_token_still_reads_and_lists(client):
    assert client.get("/api/v1/sessions", headers=H).status_code == 200
    assert client.get("/api/v1/sessions/abcd1234/profile", headers=H).status_code == 200
    assert client.post("/api/v1/sessions/abcd1234/profile", json={}).status_code == 401   # 토큰 없으면 미들웨어가 먼저


def test_local_can_still_change(client, monkeypatch):
    monkeypatch.setattr(S, "_is_local", lambda x: True)
    r = client.post("/api/v1/sessions/abcd1234/profile", json={"system_prompt": "hi"})
    assert r.status_code == 200 and profiles.get("abcd1234")["system_prompt"] == "hi"


def test_token_cannot_resume_inplace(client, monkeypatch):
    called = []
    monkeypatch.setattr(S.webapi, "run_resume_api", lambda *a, **k: called.append(1))
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/v1/resume-inplace/abcd1234?token={TOK}") as ws:
            ws.receive_text()
    assert e.value.code == 1008 and not called
