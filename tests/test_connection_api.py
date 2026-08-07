"""T10 — /api/owner/connection (로컬 웹 배지가 의존하는 API) 검증.

배지가 잘못 뜨면 사용자는 'JWT 로 붙은 줄' 알고 실제로는 공유토큰인 상태를 못 본다
(무음 실패). 그래서 이 API 의 계약을 고정해 둔다.
"""
import pytest

from session_manager import cp_client


@pytest.fixture
def app_client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    # 릴레이 모드는 꺼진 상태로 앱만 띄운다(커넥터 태스크 없이 API 계약만 검증)
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        yield c


def _reset_state():
    cp_client._state.update({"mode": "shared", "reason": "cp_not_configured",
                             "cp_url": None, "connector_id": None,
                             "checked_at": None, "jwt_exp": None})
    cp_client.clear_room_mismatch()


def test_connection_defaults_to_shared(app_client):
    _reset_state()
    r = app_client.get("/api/owner/connection")
    assert r.status_code == 200
    b = r.json()
    assert b["mode"] == "shared"
    assert b["reason"] == "cp_not_configured"
    assert b["has_credentials"] is False
    assert b["room_mismatch"] is None


def test_connection_reports_jwt_mode(app_client, monkeypatch):
    _reset_state()
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client._set_state("jwt", "ok", connector_id="c_1", jwt_exp=1234567890)
    b = app_client.get("/api/owner/connection").json()
    assert b["mode"] == "jwt" and b["reason"] == "ok"
    assert b["connector_id"] == "c_1"
    assert b["jwt_exp"] == 1234567890
    assert b["cp_url"] == "http://cp.test"


def test_connection_surfaces_fallback_reason(app_client):
    """CP 장애가 배지에 '이유까지' 드러나야 한다(무음 실패 금지)."""
    _reset_state()
    cp_client._set_state("fallback", "token_error:ConnectError", connector_id="c_2")
    b = app_client.get("/api/owner/connection").json()
    assert b["mode"] == "fallback"
    assert b["reason"] == "token_error:ConnectError"


def test_connection_surfaces_room_mismatch(app_client):
    """room 이사를 막은 상태를 사용자가 볼 수 있어야 한다(T7 사고 대응)."""
    _reset_state()
    cp_client._set_state("jwt", "ok")
    cp_client.mark_room_mismatch("rm_from_cp", "home-manual")
    b = app_client.get("/api/owner/connection").json()
    assert b["room_mismatch"] == {"cp_room": "rm_from_cp", "used_room": "home-manual"}
    _reset_state()


def test_connection_reflects_relay_room_env(app_client, monkeypatch):
    _reset_state()
    monkeypatch.setenv("SM_RELAY_ROOM", "home-xyz")
    assert app_client.get("/api/owner/connection").json()["relay_room"] == "home-xyz"
