"""커넥터가 로컬 Host(127.0.0.1)로 가는 길은 프록시를 쓰지 않는다 — docs/designs/posix-install.md 0절-5·3.0.

런처는 사용자의 env 를 그대로 Host 에 물려준다(S38). httpx 와 websockets(15+)는 http_proxy·ALL_PROXY(맥·Windows 는 시스템
프록시 설정까지)를 루프백에도 써서, 폰·웹의 모든 요청(세션 목록·터미널·관제 WS)이 프록시로 나가 실패하고 사람 증명 헤더
(X-ClewPath-Relay-Proof)·세션 내용이 프록시로 샌다. 릴레이로 나가는 연결(원격)은 그대로 사용자의 프록시를 따른다.

시험 전제를 먼저 확인한다 — 기본 클라이언트는 정말 프록시로 간다(그래야 이 시험이 무언가를 지킨다).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from session_manager import connector
from session_manager.connector import Connector

PROOF = "PROOF-SECRET-123"


class _Recorder:
    """받은 요청 줄·헤더를 모으는 HTTP 수신기. 가짜 Host 는 200 JSON, 가짜 프록시는 CONNECT 까지 502 로 거절한다."""

    def __init__(self, status: int):
        self.got: list[str] = []
        box = self

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                box.got.append(f"{self.requestline}\n{self.headers}")
                raw = json.dumps({"ok": status == 200}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_CONNECT = _answer      # noqa: N815

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def proxy(monkeypatch):
    """프록시 env 가 있는 사용자 셸 — 소문자·대문자·ALL_PROXY 를 다 둔다(no_proxy 없음)."""
    p = _Recorder(502)
    for k in ("no_proxy", "NO_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    for k in ("http_proxy", "HTTP_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{p.port}")
    try:
        yield p
    finally:
        p.close()


@pytest.mark.asyncio
async def test_local_api_calls_skip_the_env_proxy(proxy):
    host = _Recorder(200)
    try:
        url = f"http://127.0.0.1:{host.port}/api/v1/sessions"
        async with httpx.AsyncClient(timeout=10) as plain:          # 시험 전제: 기본 클라이언트는 프록시로 간다
            assert (await plain.get(url)).status_code == 502 and proxy.got and not host.got
        proxy.got.clear()
        conn = Connector(relay_url="ws://relay.test/ws", room="r", token="t", local_base=f"http://127.0.0.1:{host.port}")
        try:
            r = await conn.http.get(f"{conn.local_base}/api/v1/sessions",
                                    headers={connector.PROOF_HEADER: PROOF})
        finally:
            await conn.http.aclose()
        assert r.status_code == 200 and len(host.got) == 1
        assert proxy.got == [], "로컬 호출(증명 헤더 포함)이 프록시로 샜다"
    finally:
        host.close()


@pytest.mark.asyncio
@pytest.mark.skipif("proxy" not in inspect.signature(connect).parameters,
                    reason="websockets < 15 는 프록시를 쓰지 않는다(지킬 것이 없다)")
async def test_local_stream_pipes_skip_the_env_proxy(proxy):
    """터미널·재개·관제 파이프의 로컬 WS(ws://127.0.0.1) — 관제 파이프로 끝까지 본다."""
    async def handler(ws):
        await ws.send('{"type":"snapshot","events":[]}')

    async with serve(handler, "127.0.0.1", 0) as srv:
        port = next(iter(srv.sockets)).getsockname()[1]
        with pytest.raises(Exception):                                # 시험 전제: 기본 connect 는 프록시(CONNECT)로 간다
            async with connect(f"ws://127.0.0.1:{port}/ws/monitor", open_timeout=5):
                pass
        assert any(g.startswith("CONNECT ") for g in proxy.got)
        proxy.got.clear()
        conn = Connector(relay_url="ws://relay.test/ws", room="r", token="t", local_base=f"http://127.0.0.1:{port}")
        sent = []

        async def fake_stream_send(rid, **kw):
            sent.append(kw)

        conn._stream_send = fake_stream_send
        try:
            await conn._start_monitor("r1", {"ids": ["a", "b"]})
            await asyncio.wait_for(conn.streams["r1"], 10)
        finally:
            await conn.http.aclose()
    assert [kw.get("data") for kw in sent if "data" in kw] == ['{"type":"snapshot","events":[]}'], sent
    assert not any(kw.get("error") for kw in sent), sent
    assert proxy.got == [], "로컬 WS 가 프록시로 샜다"
