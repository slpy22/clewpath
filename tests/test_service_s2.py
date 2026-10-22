"""서비스 대장 S2: 서비스별 토큰·원본·한도. docs/designs/service-registry.md §3~5

서비스 토큰으로는 자기 원본(bases)의 사본 질의(ask·resume)·자기 원본 프로필 읽기·자기 정보만 된다.
"""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import write_session
from session_manager import server as S, profiles, services, policy, connector, config

BASE = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER = "bbbbbbbb-0000-0000-0000-000000000002"
LEGACY = "legacy-owner-token"


def _mk(home, sid):
    write_session(home, "E--svc", sid, [
        {"type": "user", "cwd": "E:\\svc", "message": {"role": "user", "content": "준비"},
         "timestamp": "2026-10-20T00:00:00.000Z"}])


@pytest.fixture
def env(fake_claude_home, monkeypatch):
    _mk(fake_claude_home, BASE)
    _mk(fake_claude_home, OTHER)
    profiles.set_record(BASE, system_prompt="guard", allowed_tools=["ToolSearch"])
    profiles.set_record(OTHER, system_prompt="other")
    monkeypatch.setenv("SM_API_TOKEN", LEGACY)
    asked = []

    def fake_ask(sid, prompt, **k):
        asked.append((sid, k.get("guardrails")))
        return {"answer": "ok", "result": "ok", "cost_usd": 0.01, "fork_id": "f1"}
    monkeypatch.setattr(S.webapi, "ephemeral_ask", fake_ask)
    client = TestClient(S.create_app())          # 'testclient' = 원격
    return client, asked


def _local(monkeypatch):
    monkeypatch.setattr(S, "_is_local", lambda x: True)


def _create(client, monkeypatch, **body):
    _local(monkeypatch)
    r = client.post("/api/owner/services", json={"id": "nklaw", "name": "북한법",
                                                  "bases": {"chat": BASE}, **body})
    monkeypatch.setattr(S, "_is_local", lambda x: False)
    return r


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def test_create_returns_token_once_and_stores_hash_only(env, monkeypatch):
    client, _ = env
    r = _create(client, monkeypatch)
    assert r.status_code == 200
    tok = r.json()["token"]
    assert tok.startswith("cps_nklaw_") and len(tok) > 70
    raw = (config.data_dir() / "services.json").read_text(encoding="utf-8")
    assert tok not in raw and "sha256:" in raw
    _local(monkeypatch)
    lst = client.get("/api/owner/services").json()["services"]
    assert lst[0]["id"] == "nklaw" and "token_hash" not in lst[0] and lst[0]["token_hint"].endswith(tok[-4:])


@pytest.mark.parametrize("bases,code,err", [
    ({"chat": "cccccccc-0000-0000-0000-000000000009"}, 404, "base_not_found"),
    ({}, 400, "bad_bases"),
    ({"Chat!": BASE}, 400, "bad_bases"),
])
def test_create_rejects_bad_bases(env, monkeypatch, bases, code, err):
    client, _ = env
    r = _create(client, monkeypatch, bases=bases)
    assert r.status_code == code and r.json()["error"] == err


def test_base_without_profile_rejected(env, monkeypatch):
    client, _ = env
    profiles.delete(BASE)
    r = _create(client, monkeypatch)
    assert r.status_code == 400 and r.json()["error"] == "base_no_profile"


def test_base_cannot_be_shared_between_services(env, monkeypatch):
    client, _ = env
    assert _create(client, monkeypatch).status_code == 200
    r = _create(client, monkeypatch, id="other")
    assert r.status_code == 409 and r.json()["error"] == "base_taken"


def test_owner_endpoints_are_local_only(env):
    client, _ = env
    assert client.get("/api/owner/services").status_code == 403
    assert client.post("/api/owner/services", json={"id": "x"}).status_code == 403
    assert connector._is_local_only_api("/api/owner/services", "GET")
    assert connector._is_local_only_api("/api/owner/services/nklaw/rotate", "POST")


def test_service_token_scope(env, monkeypatch):
    client, asked = env
    tok = _create(client, monkeypatch).json()["token"]
    # 목록·라벨·남의 원본은 안 된다
    assert client.get("/api/v1/sessions", headers=_h(tok)).status_code == 403
    assert client.get("/api/v1/labels", headers=_h(tok)).status_code == 403
    assert client.post(f"/api/v1/sessions/{OTHER}/ask", json={"prompt": "q"}, headers=_h(tok)).status_code == 403
    assert client.get(f"/api/v1/sessions/{OTHER}/profile", headers=_h(tok)).status_code == 403
    assert client.post(f"/api/v1/sessions/{BASE}/profile", json={}, headers=_h(tok)).status_code == 403
    assert not asked
    # 자기 원본은 된다
    assert client.get(f"/api/v1/sessions/{BASE}/profile", headers=_h(tok)).status_code == 200
    r = client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "형법 제1조"}, headers=_h(tok))
    assert r.status_code == 200 and asked[0][0] == BASE
    me = client.get("/api/v1/service/me", headers=_h(tok)).json()
    assert me["service"]["id"] == "nklaw" and me["today"]["calls"] == 1
    # 감사 기록은 토큰 해시가 아니라 서비스 id
    lines = (config.data_dir() / "api_audit.jsonl").read_text(encoding="utf-8").splitlines()
    ask = [json.loads(x) for x in lines if '"event": "ask"' in x][-1]
    assert ask["service"] == "nklaw" and "token" not in ask


def test_service_limits_apply(env, monkeypatch):
    client, asked = env
    tok = _create(client, monkeypatch, limits={"daily_calls": 1, "max_call_usd": 0.2}).json()["token"]
    assert client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "a"}, headers=_h(tok)).status_code == 200
    assert asked[0][1]["max_budget_usd"] == 0.2           # 서비스 1회 한도가 더 빡세면 그것
    r = client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "b"}, headers=_h(tok))
    assert r.status_code == 429


def test_rotate_kills_old_token(env, monkeypatch):
    client, _ = env
    old = _create(client, monkeypatch).json()["token"]
    _local(monkeypatch)
    new = client.post("/api/owner/services/nklaw/rotate").json()["token"]
    monkeypatch.setattr(S, "_is_local", lambda x: False)
    assert client.get("/api/v1/service/me", headers=_h(old)).status_code == 401
    assert client.get("/api/v1/service/me", headers=_h(new)).status_code == 200


@pytest.mark.parametrize("status", ["suspended", "revoked"])
def test_suspend_and_revoke(env, monkeypatch, status):
    client, _ = env
    tok = _create(client, monkeypatch).json()["token"]
    _local(monkeypatch)
    assert client.post("/api/owner/services/nklaw", json={"status": status}).status_code == 200
    monkeypatch.setattr(S, "_is_local", lambda x: False)
    r = client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "a"}, headers=_h(tok))
    assert r.status_code == 401 and r.json()["error"] == f"service_{status}"
    if status == "revoked":    # 폐기하면 원본이 풀려 다른 서비스가 쓸 수 있다
        assert services.base_owner(BASE) is None


def test_resume_ws_only_on_own_base(env, monkeypatch):
    client, _ = env
    tok = _create(client, monkeypatch).json()["token"]
    called = []

    async def fake_run(ws, sid, **k):
        called.append((sid, k))
        await ws.close()
    monkeypatch.setattr(S.webapi, "run_resume_api", fake_run)
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/api/v1/resume/{OTHER}?token={tok}") as ws:
            ws.receive_text()
    assert e.value.code == 1008 and not called
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/resume/{BASE}?token={tok}") as ws:
            ws.receive_text()
    sid, k = called[0]
    assert sid == BASE and k["fork_id"] and k["allow"]()     # 사본 강제 + 턴마다 서비스 상태 확인
    services.update("nklaw", status="suspended")
    assert not k["allow"]()


def test_service_token_restricted_even_from_loopback(env, monkeypatch):
    client, asked = env
    tok = _create(client, monkeypatch).json()["token"]
    _local(monkeypatch)
    r = client.post(f"/api/v1/sessions/{OTHER}/ask", json={"prompt": "q"}, headers=_h(tok))
    assert r.status_code == 403 and not asked


def test_legacy_token_unchanged_and_services_work_without_it(env, monkeypatch):
    client, asked = env
    tok = _create(client, monkeypatch).json()["token"]
    assert client.get("/api/v1/sessions", headers=_h(LEGACY)).status_code == 200
    monkeypatch.delenv("SM_API_TOKEN")
    assert client.get("/api/v1/sessions", headers=_h(LEGACY)).status_code == 503
    assert client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "q"}, headers=_h(tok)).status_code == 200
    assert client.get("/api/v1/service/me", headers=_h("cps_nklaw_" + "0" * 64)).status_code == 401


def test_usage_summary_handles_service_keys(env, monkeypatch):
    client, _ = env
    tok = _create(client, monkeypatch).json()["token"]
    client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "q"}, headers=_h(tok))
    rows = policy.usage_summary()["today_by_token"]
    assert any(r["token"] == "svc:nklaw" and r["calls"] == 1 for r in rows)


def test_legacy_token_switch_off_in_config(env, monkeypatch, tmp_path):
    """S5: clewpath.toml [server] legacy_api_token = false → 공용 토큰은 꺼지고 서비스 토큰은 그대로.
    설정 파일 자리는 이 시험의 임시 폴더로 못 박는다 — 물려받은 CLEWPATH_CONFIG 가 진짜 파일을 가리켜도(맥 실기 3b-2 이슈 B)."""
    from session_manager import appconfig
    client, _ = env
    tok = _create(client, monkeypatch).json()["token"]
    p = tmp_path / "clewpath.toml"
    monkeypatch.setenv("CLEWPATH_CONFIG", str(p))
    assert appconfig.config_path() == p
    p.write_text("[server]\nlegacy_api_token = false\n", encoding="utf-8")
    appconfig.load(refresh=True)
    try:
        assert client.get("/api/v1/labels", headers=_h(LEGACY)).status_code == 503
        assert client.post(f"/api/v1/sessions/{BASE}/ask", json={"prompt": "q"}, headers=_h(tok)).status_code == 200
    finally:
        p.unlink()
        appconfig.load(refresh=True)
