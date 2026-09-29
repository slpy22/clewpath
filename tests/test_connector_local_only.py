"""Host 0.9.8 핫픽스 — 릴레이 프록시로 로컬 전용 API 가 열리던 구멍(2026-09-29 CEO 리뷰 Codex 발견).

1) 커넥터 제네릭 프록시는 로컬 전용 경로를 서버까지 보내지 않고 `local_only` 로 거부한다.
2) 보내는 요청엔 X-ClewPath-Via 헤더가 실리고, 서버 `_is_local` 은 그 헤더면 루프백이라도 로컬 아님.
3) 기기 삭제/재발급/폐기 → 그 기기의 연결(authed)·스트림이 즉시 끊긴다. 스트림 입력도 살아 있는 인증에 묶인다.
4) 릴레이 재접속 시 authed/req_cid 초기화.
"""
from __future__ import annotations

import asyncio

import pytest

from session_manager import connector as C
from session_manager.connector import Connector


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")


# ---- 1) 덴리스트 ----

@pytest.mark.parametrize("path,verb,expect", [
    ("/api/owner/devices", "GET", True),
    ("/api/owner/devices", "POST", True),
    ("/api/owner/devices/abc/delete", "POST", True),
    ("/api/owner/devices/abc/reissue", "POST", True),
    ("/api/owner/devices?x=1", "GET", True),
    ("/api/owner/2fa/provision", "POST", True),
    ("/api/owner/2fa/toggle", "POST", True),
    ("/api/owner/2fa/status", "GET", False),          # 원격 ensurePriv 가 쓴다
    ("/api/owner/skills/workers/install", "POST", True),
    ("/api/owner/skills/workers", "GET", False),
    ("/api/owner/trash/sessions/restore", "POST", True),
    ("/api/owner/update/apply", "POST", True),
    ("/api/owner/update/status", "GET", False),
    ("/api/sessions/59f9577b-0d25-47be-994b-29009cf0fba3/terminal/start", "POST", True),
    ("/api/sessions/59f9577b-0d25-47be-994b-29009cf0fba3/terminal/stop", "POST", False),   # 2FA 게이트가 담당
    ("/api/owner/monitor/groups", "POST", False),
    ("/api/v1/sessions", "GET", False),
])
def test_local_only_denylist(path, verb, expect):
    assert C._is_local_only_api(path, verb) is expect


@pytest.mark.asyncio
async def test_proxy_refuses_local_only_without_touching_server(monkeypatch):
    conn = _conn()
    hits = []

    async def _http(*a, **k):
        hits.append(a)
    monkeypatch.setattr(conn.http, "get", _http)
    monkeypatch.setattr(conn.http, "post", _http)
    seen = []

    async def _res(rid, ok, data=None, error=None):
        seen.append((rid, ok, error))
    monkeypatch.setattr(conn, "_res", _res)
    await conn._handle_api("r1", {"verb": "POST", "path": "/api/owner/devices", "body": {"name": "x"}})
    await conn._handle_api("r2", {"verb": "GET", "path": "/api/owner/devices"})
    await conn._handle_api("r3", {"verb": "POST", "path": "/api/owner/devices/d1/delete"})
    assert [e for _, _, e in seen] == ["local_only"] * 3
    assert hits == []


# ---- 2) 헤더 + 서버 판정 ----

@pytest.mark.asyncio
async def test_proxied_request_carries_via_header(monkeypatch):
    conn = _conn()
    calls = []

    class _Resp:
        status_code = 200
        def json(self): return {"ok": True}

    async def _get(url, **k):
        calls.append(("GET", url, k.get("headers")))
        return _Resp()

    async def _post(url, **k):
        calls.append(("POST", url, k.get("headers")))
        return _Resp()
    monkeypatch.setattr(conn.http, "get", _get)
    monkeypatch.setattr(conn.http, "post", _post)

    async def _res(*a, **k):
        pass
    monkeypatch.setattr(conn, "_res", _res)
    await conn._handle_api("r1", {"verb": "GET", "path": "/api/v1/sessions"})
    await conn._handle_api("r2", {"verb": "POST", "path": "/api/sessions/abc/rename", "body": {"name": "n"}})
    assert all(h == {C.VIA_HEADER: "relay"} for _, _, h in calls) and len(calls) == 2


def test_server_is_local_rejects_relay_header():
    from session_manager import server as S

    class _Addr:
        def __init__(self, host): self.host = host

    class _Req:
        def __init__(self, host, headers):
            self.client = _Addr(host)      # starlette Request/WebSocket 의 .client 처럼 .host 를 가진다
            self.headers = headers

    class _H(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)
    assert S._is_local(_Req("127.0.0.1", _H())) is True
    assert S._is_local(_Req("127.0.0.1", _H({"x-clewpath-via": "relay"}))) is False
    assert S._is_local(_Req("10.0.0.5", _H())) is False


def test_local_only_endpoints_403_via_relay_header(fake_claude_home, monkeypatch):
    """실제 라우트: 루프백 TestClient 라도 헤더가 있으면 기기 등록/삭제·2FA 토글이 403."""
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")   # TestClient 는 'testclient' 호스트 → 루프백으로 가정
    with TestClient(S.create_app(), base_url="http://127.0.0.1") as c:
        via = {"X-ClewPath-Via": "relay"}
        assert c.post("/api/owner/devices", json={"name": "폰"}, headers=via).status_code == 403
        assert c.post("/api/owner/devices/nope/delete", headers=via).status_code == 403
        assert c.post("/api/owner/2fa/toggle", json={"on": False}, headers=via).status_code == 403
        assert c.post("/api/owner/skills/workers/install", json={}, headers=via).status_code == 403
        # 헤더 없는 진짜 로컬은 그대로(2fa 토글은 비밀 유무에 따라 200)
        assert c.post("/api/owner/2fa/toggle", json={"on": False}).status_code == 200


# ---- 3) 삭제/재발급 → 연결 해제, 스트림 입력 인증 묶기 ----

@pytest.mark.asyncio
async def test_drop_device_cancels_streams_and_clears_auth(monkeypatch):
    conn = _conn()
    conn.authed = {"c1": {"id": "d1", "name": "폰"}, "c2": {"id": "d2", "name": "패드"}}
    conn.enc_cids = {"c1", "c2"}
    conn.req_cid = {"t1": "c1", "t2": "c2", "t3": "c1"}

    async def _forever():
        await asyncio.sleep(3600)
    conn.streams = {"t1": asyncio.create_task(_forever()), "t2": asyncio.create_task(_forever()),
                    "t3": asyncio.create_task(_forever())}
    n = await conn.drop_device("d1")
    await asyncio.sleep(0)
    assert n == 1
    assert conn.streams["t1"].cancelled() and conn.streams["t3"].cancelled()
    assert not conn.streams["t2"].cancelled()
    assert "c1" not in conn.authed and "c2" in conn.authed
    assert conn.enc_cids == {"c2"}
    assert await conn.drop_device("nope") == 0
    for t in conn.streams.values():
        t.cancel()


@pytest.mark.asyncio
async def test_stream_in_requires_live_device_auth_when_enforced(monkeypatch):
    conn = _conn()
    monkeypatch.setattr("session_manager.devices.enforced", lambda: True)
    monkeypatch.setattr("session_manager.devices.is_active", lambda did: did == "d1")
    conn.req_cid = {"t1": "c1", "t2": "c2", "t3": "c3"}
    conn.authed = {"c1": {"id": "d1"}, "c2": {"id": "dead"}}       # c3 은 미인증
    conn.stream_in = {k: asyncio.Queue() for k in ("t1", "t2", "t3")}

    async def _forever():
        await asyncio.sleep(3600)
    conn.streams = {"t2": asyncio.create_task(_forever()), "t3": asyncio.create_task(_forever())}
    await conn._handle_stream_in({"id": "t1", "data": {"type": "input", "data": "ls"}})
    await conn._handle_stream_in({"id": "t2", "data": {"type": "input", "data": "rm"}})
    await conn._handle_stream_in({"id": "t3", "data": {"type": "input", "data": "rm"}})
    await asyncio.sleep(0)
    assert conn.stream_in["t1"].qsize() == 1                     # 살아 있는 인증 → 통과
    assert conn.stream_in["t2"].qsize() == 0 and conn.streams["t2"].cancelled()   # 폐기된 기기 → 버리고 해체
    assert conn.stream_in["t3"].qsize() == 0 and conn.streams["t3"].cancelled()   # 미인증 → 동일
    # 강제 off 면 기존 동작(통과)
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    await conn._handle_stream_in({"id": "t1", "data": "x"})
    assert conn.stream_in["t1"].qsize() == 2


def test_request_drop_device_from_server_thread(monkeypatch):
    conn = _conn()
    conn.authed = {"c1": {"id": "d1"}}
    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
    conn.loop = loop
    monkeypatch.setattr(C, "CURRENT", conn)
    try:
        assert C.request_drop_device("d1") == 1
        assert conn.authed == {}
    finally:
        loop.call_soon_threadsafe(loop.stop); t.join(2)
    monkeypatch.setattr(C, "CURRENT", None)
    assert C.request_drop_device("d1") == 0                       # 커넥터 없으면 무해


# ---- 4) 재접속 초기화 ----

def test_on_connected_resets_auth_state():
    conn = _conn()
    conn.authed = {"old": {"id": "d1"}}; conn.enc_cids = {"old"}; conn.req_cid = {"r": "old"}
    conn._on_connected()
    assert conn.authed == {} and conn.enc_cids == set() and conn.req_cid == {}
