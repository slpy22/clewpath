"""두 기기(폰·태블릿 등)가 같은 PC 에 동시에 붙을 때 — 요청·스트림이 섞이지 않는다(0.29.0).

기기는 저마다 요청 id 를 1 부터 센다('c5'·'t3'·'mon7'). 커넥터가 id 만으로 응답 길·스트림을 들면, 같은 id 를 쓴 두 기기 사이에서
응답이 다른 기기로 가고, 터미널 출력이 다른 기기 탭에 섞이고, 한쪽의 stream_close 가 다른 기기 스트림을 끊었다.
커넥터는 릴레이가 붙인 cid 와 묶은 (cid, id) 를 내부 키로 쓰고, 보낼 때는 원래 id 와 cid 를 싣는다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from session_manager.connector import Connector

SID = "59f9577b-0d25-47be-994b-29009cf0fba3"


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")


class _BlockWS:
    def __aiter__(self):
        async def gen():
            await asyncio.Event().wait()   # 취소될 때까지 대기(살아 있는 로컬 WS 흉내)
            yield
        return gen()

    async def send(self, m):
        pass


class _BlockCM:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return _BlockWS()

    async def __aexit__(self, *a):
        self.closed = True                 # async with 해제 = 로컬 WS 닫힘
        return False


class _Resp:
    def __init__(self, j):
        self.status_code, self._j, self.text = 200, j, ""

    def json(self):
        return self._j


def _req(cid, rid, method, params):
    return json.dumps({"v": 1, "type": "req", "id": rid, "cid": cid, "method": method, "params": params})


@pytest.fixture
def conn(monkeypatch):
    c = _conn()
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c.sent = []

    async def _send(obj):
        c.sent.append(obj)
    monkeypatch.setattr(c, "send", _send)
    return c


@pytest.mark.asyncio
async def test_same_request_id_from_two_devices_each_gets_own_answer(conn, monkeypatch):
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def _get(url, params=None, headers=None):
        who = url.rsplit("/", 1)[-1]
        await gates[who].wait()
        return _Resp({"who": who})
    monkeypatch.setattr(conn.http, "get", _get)

    fa = asyncio.create_task(conn._on_frame(_req("cidA", "c5", "api", {"verb": "GET", "path": "/api/a"})))
    fb = asyncio.create_task(conn._on_frame(_req("cidB", "c5", "api", {"verb": "GET", "path": "/api/b"})))
    for _ in range(3):
        await asyncio.sleep(0)              # 둘 다 응답을 기다리는 중(같은 id 가 동시에 떠 있음)
    gates["b"].set()
    await fb
    gates["a"].set()
    await fa
    res = {f["cid"]: f for f in conn.sent if f.get("type") == "res"}
    assert res["cidA"]["id"] == "c5" and res["cidA"]["data"]["json"] == {"who": "a"}
    assert res["cidB"]["id"] == "c5" and res["cidB"]["data"]["json"] == {"who": "b"}


@pytest.mark.asyncio
async def test_same_stream_id_from_two_devices_stays_separate(conn, monkeypatch):
    cms = []

    def fake_connect(url, **kw):
        cm = _BlockCM()
        cms.append(cm)
        return cm
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)

    for cid in ("cidA", "cidB"):
        await conn._on_frame(_req(cid, "mon3", "monitor", {"ids": SID}))
    for _ in range(3):
        await asyncio.sleep(0)
    ta, tb = conn.streams[("cidA", "mon3")], conn.streams[("cidB", "mon3")]
    assert ta is not tb and not any(f.get("error") == "stream_exists" for f in conn.sent)

    # B 로 가는 스트림 프레임은 원래 id·B 의 cid 로 나간다
    await conn._stream_send(("cidB", "mon3"), data="x")
    assert conn.sent[-1] == {"v": 1, "type": "stream", "id": "mon3", "cid": "cidB", "data": "x"}

    # 입력도 보낸 기기의 스트림으로만
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_in", "id": "mon3", "cid": "cidB", "data": "up"}))
    assert conn.stream_in[("cidB", "mon3")].qsize() == 1 and conn.stream_in[("cidA", "mon3")].qsize() == 0

    # A 가 닫으면 A 의 것만
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "mon3", "cid": "cidA"}))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(ta, 5)
    assert cms[0].closed is True and not tb.done() and cms[1].closed is False
    assert ("cidA", "mon3") not in conn.streams and ("cidB", "mon3") in conn.streams

    # B 가 떠나면 B 의 것도 끝난다
    await conn._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidB"}))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tb, 5)
    assert conn.streams == {}


@pytest.mark.asyncio
async def test_other_device_cannot_close_my_stream(conn, monkeypatch):
    monkeypatch.setattr("session_manager.connector.connect", lambda url, **kw: _BlockCM())
    await conn._on_frame(_req("cidA", "t1", "monitor", {"ids": SID}))
    for _ in range(3):
        await asyncio.sleep(0)
    ta = conn.streams[("cidA", "t1")]
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "t1", "cid": "cidZ"}))
    for _ in range(3):
        await asyncio.sleep(0)
    assert not ta.done()                    # 다른 기기의 같은 id 닫기는 무시
    ta.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ta


@pytest.mark.asyncio
async def test_ping_pong_keeps_device_id(conn):
    await conn._on_frame(_req("cidA", "c9", "ping", {}))
    assert conn.sent[-1] == {"v": 1, "type": "pong", "id": "c9", "cid": "cidA"}
