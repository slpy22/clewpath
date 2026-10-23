"""로그에 실리는 주소의 비밀 쿼리값(jwt·token)이 가려지는지 — 실제 웹소켓 연결로 본다(TODOS 27행 끝).

커넥터(릴레이 agent)·pclink(상대 방 client)·mcp_server(로컬 Host)는 쿼리스트링에 jwt·token 을 실은 주소로 웹소켓을 연다.
websockets 는 DEBUG 수준에서 요청 줄(`> GET /ws?…&jwt=… HTTP/1.1`)을, uvicorn 은 기본 수준에서 받은 웹소켓 주소를 로그로 남긴다.
누가 로그 수준을 DEBUG 로 올려도 토큰 글자가 로그에 없어야 한다.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from websockets.asyncio.server import serve

JWT = "JWT-TOKEN-aa71"
SHARED = "SHARED-TOKEN-bb82"
ROOM = "room-0123456789abcdef"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    # 가짜 릴레이(127.0.0.1)로 가는 길에 사용자의 프록시가 끼지 않게
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY", "SOCKS_PROXY"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(k.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


class _Relay:
    """받은 요청 주소만 적고 바로 닫는 가짜 릴레이."""

    def __init__(self, hello: bool = False):
        self.paths: list[str] = []
        self.hello = hello
        self.got = asyncio.Event()

    async def _handler(self, ws):
        self.paths.append(ws.request.path)
        self.got.set()
        if self.hello:
            await ws.send('{"v": 1, "type": "hello", "role": "client", "cid": "c1", "peer_present": true}')
            await ws.wait_closed()
            return
        await ws.close()

    async def __aenter__(self):
        self._srv = await serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._srv.sockets[0].getsockname()[1]}/ws"
        return self

    async def __aexit__(self, *a):
        self._srv.close()
        await self._srv.wait_closed()


def _debug_logs(caplog) -> None:
    """누가 websockets 로그 수준을 DEBUG 로 올린 상황."""
    caplog.set_level(logging.DEBUG, logger="websockets")
    caplog.set_level(logging.DEBUG, logger="websockets.client")


def _all(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_redact_hides_query_secrets_only():
    from session_manager import logredact
    s = f"> GET /ws?role=agent&room={ROOM}&token={SHARED}&v=2&jwt={JWT} HTTP/1.1"
    out = logredact.redact(s)
    assert JWT not in out and SHARED not in out
    assert f"room={ROOM}" in out and "v=2" in out and "role=agent" in out    # 나머지는 그대로(진단에 쓴다)
    assert logredact.redact("주소 없음 jwt 이라는 낱말") == "주소 없음 jwt 이라는 낱말"


@pytest.mark.asyncio
async def test_pclink_debug_log_has_no_jwt(caplog):
    from session_manager.pclink.link import PeerLink

    class _CP:          # PeerLink._fetch_token 이 부르는 httpx 클라이언트 흉내
        async def post(self, url, json=None):
            import httpx
            return httpx.Response(200, json={"token": JWT, "expires_at": 4_000_000_000})

    _debug_logs(caplog)
    async with _Relay(hello=True) as relay:
        lk = PeerLink(relay=relay.url, cp="https://cp.test/cp", room=ROOM, cred_id="cred-1",
                      cred_secret="CRED-SECRET-cc93", http=_CP(), connect_timeout=3.0)
        ws, hello = await lk._open(JWT)
        await ws.close()
    assert any(JWT in p for p in relay.paths)              # 실제로 jwt 를 실어 보냈고
    text = _all(caplog)
    assert "> GET /ws?" in text                             # websockets 가 요청 줄을 남겼는데
    assert JWT not in text                                  # 토큰은 가려졌다


@pytest.mark.asyncio
async def test_pclink_unreachable_debug_log_has_no_jwt(caplog):
    from session_manager.pclink.link import LinkError, PeerLink
    _debug_logs(caplog)
    lk = PeerLink(relay="ws://127.0.0.1:1/ws", cp="https://cp.test/cp", room=ROOM, cred_id="cred-1",
                  cred_secret="CRED-SECRET-cc93", http=None, connect_timeout=2.0)
    with pytest.raises(LinkError):
        await lk._open(JWT)
    assert JWT not in _all(caplog)


@pytest.mark.asyncio
async def test_connector_debug_log_and_disconnect_line_have_no_token(caplog, capsys, monkeypatch):
    from session_manager import connector as connector_mod
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "cp_url", lambda: "https://cp.test")
    monkeypatch.setattr(cp_client, "fetch_jwt", lambda name="": {"token": JWT, "room_key": ROOM})
    monkeypatch.setattr(cp_client, "set_mode", lambda *a, **k: None)
    monkeypatch.setattr(cp_client, "status", lambda: {})
    _debug_logs(caplog)
    async with _Relay() as relay:
        c = connector_mod.Connector(relay_url=relay.url, room=ROOM, token=SHARED, local_base="http://127.0.0.1:5100")
        t = asyncio.ensure_future(c.run())
        try:
            await asyncio.wait_for(relay.got.wait(), 5)
            await asyncio.sleep(0.2)
        finally:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
            await c.http.aclose()
    assert any(JWT in p and SHARED in p for p in relay.paths)
    text = _all(caplog)
    assert "> GET /ws?" in text
    assert JWT not in text and SHARED not in text
    out = capsys.readouterr()
    assert JWT not in out.out + out.err and SHARED not in out.out + out.err


@pytest.mark.asyncio
async def test_connector_bad_uri_disconnect_line_has_no_token(capsys, monkeypatch):
    """InvalidURI 의 문구에는 주소 전체(쿼리의 토큰 포함)가 들어간다 — 커넥터의 '연결 끊김' 줄로 새지 않게."""
    from session_manager import connector as connector_mod
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "cp_url", lambda: None)
    c = connector_mod.Connector(relay_url="bad://127.0.0.1:1/ws", room=ROOM, token=SHARED,
                                local_base="http://127.0.0.1:5100")
    t = asyncio.ensure_future(c.run())
    await asyncio.sleep(0.3)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    await c.http.aclose()
    out = capsys.readouterr()
    assert "연결 끊김" in out.out and "InvalidURI" in out.out
    assert SHARED not in out.out + out.err


def test_uvicorn_ws_accept_log_has_no_token(caplog):
    """uvicorn 은 기본(INFO) 수준에서 받은 웹소켓 주소를 남긴다 — mcp_server·PWA 가 `?token=` 으로 Host 에 붙는다.
    접근 로그 포매터는 args 를 자리별로 풀어 쓰므로 인자 모양은 그대로여야 한다."""
    from session_manager import logredact
    logredact.install()
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5",
                                            f"/api/v1/resume/x?token={SHARED}&skip=0")
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
                                             f"/api/x?token={SHARED}", "1.1", 200)
    acc = [r for r in caplog.records if r.name == "uvicorn.access"][0]
    assert isinstance(acc.args, tuple) and len(acc.args) == 5 and acc.args[4] == 200
    assert SHARED not in _all(caplog)
    assert "skip=0" in _all(caplog)
