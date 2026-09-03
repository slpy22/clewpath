"""connector relay monitor 터널 — 읽기전용 단방향 파이프 검증.

실제 relay/로컬 WS 없이 connector.connect 를 페이크로 가로채, _start_monitor 가
올바른 /ws/monitor URL 을 열고 local→client 로 이벤트를 _stream_send 하는지 본다.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse, parse_qs

import pytest

from session_manager.connector import Connector

MGR = "aaaaaaaa-1111-2222-3333-444444444444"
SUB = "59f9577b-0d25-47be-994b-29009cf0fba3"


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="home-manual",
                     token="rly-agt-x", local_base="http://127.0.0.1:5100")


class _FakeWS:
    """async for 로 미리 정한 메시지를 흘리고 끝나는 로컬 WS 대역."""

    def __init__(self, msgs):
        self._msgs = list(msgs)

    def __aiter__(self):
        async def gen():
            for m in self._msgs:
                yield m
        return gen()


class _FakeCM:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_start_monitor_builds_url_and_pipes(monkeypatch):
    conn = _conn()
    captured = {}

    def fake_connect(url, **kw):
        captured["url"] = url
        return _FakeCM(_FakeWS(['{"type":"snapshot","events":[]}',
                                '{"type":"events","events":[1]}']))
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)

    sent = []

    async def fake_stream_send(rid, **kw):
        sent.append((rid, kw))
    monkeypatch.setattr(conn, "_stream_send", fake_stream_send)

    await conn._start_monitor("r1", {"ids": [MGR, SUB], "manager": MGR})
    await conn.streams["r1"]          # 파이프 태스크 완료 대기

    # 1) 올바른 로컬 관제 WS URL
    u = urlparse(captured["url"])
    assert u.path == "/ws/monitor"
    q = parse_qs(u.query)
    assert q["ids"] == [f"{MGR},{SUB}"]
    assert q["manager"] == [MGR]

    # 2) local→client 단방향: 두 메시지 data 전송 후 eof
    datas = [kw.get("data") for _rid, kw in sent if "data" in kw]
    assert datas == ['{"type":"snapshot","events":[]}',
                     '{"type":"events","events":[1]}']
    assert any(kw.get("eof") for _rid, kw in sent)


@pytest.mark.asyncio
async def test_start_monitor_empty_group(monkeypatch):
    conn = _conn()
    sent = []

    async def fake_stream_send(rid, **kw):
        sent.append((rid, kw))
    monkeypatch.setattr(conn, "_stream_send", fake_stream_send)

    await conn._start_monitor("r2", {"ids": ""})
    assert sent and sent[0][1].get("error") == "empty_group"
    assert sent[0][1].get("eof") is True
    assert "r2" not in conn.streams        # 스트림 태스크 안 만들어짐


@pytest.mark.asyncio
async def test_start_monitor_stream_exists(monkeypatch):
    conn = _conn()
    conn.streams["r3"] = asyncio.get_event_loop().create_future()
    sent = []

    async def fake_res(rid, ok, data=None, error=None):
        sent.append((rid, ok, error))
    monkeypatch.setattr(conn, "_res", fake_res)

    await conn._start_monitor("r3", {"ids": MGR})
    assert sent == [("r3", False, "stream_exists")]
    conn.streams.pop("r3")
