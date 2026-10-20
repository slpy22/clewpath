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


async def _drain(c):
    """따로 도는 느린 요청(api·목록 등 — _SPAWN_METHODS)이 끝날 때까지. _on_frame 은 그것들을 기다리지 않는다(검토 3회차)."""
    while c._req_tasks:
        await asyncio.gather(*list(c._req_tasks))


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
    await _drain(conn)
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


# ---- 검토 2회차: cid 가 client_offline 없이 바뀔 때(릴레이 재기동·커넥터가 끊긴 사이 기기 이탈)·떠난 기기 앞 프레임 ----

class _RelayWS:
    """커넥터가 릴레이로 내보낸 프레임을 모은다 — 실제 send(봉투·버리기)를 거친 결과."""

    def __init__(self):
        self.wire = []

    async def send(self, s):
        self.wire.append(json.loads(s))


@pytest.fixture
def wired(monkeypatch):
    c = _conn()
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c.ws = _RelayWS()
    return c


async def _ticks(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_relay_reconnect_ends_pipes_of_previous_connection(wired, monkeypatch):
    """릴레이에 다시 붙으면(_on_connected) 이전 연결에서 연 파이프(웹 재개·관제)를 끝내고 그 cid 앞 늦은 프레임은 보내지 않는다.

    릴레이가 재기동하면 cid 가 모두 바뀌고 옛 cid 의 client_offline 은 오지 않는다. 남은 파이프는 새 cid 의 stream_close 로도
    닫지 못했고(키가 (옛 cid, id)), 출력은 사라진 cid 앞이라 릴레이가 방 전체에 평문으로 broadcast 했다(재기동으로 enc_cids 를 비움).
    웹 재개라면 Host 의 claude -p 가 Host 재시작까지 남았다. 같은 기기가 새 cid 로 다시 말을 걸면 그 cid 는 그대로 쓴다.
    """
    cms = []

    def fake_connect(url, **kw):
        cm = _BlockCM()
        cms.append(cm)
        return cm
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)
    c = wired
    await c._on_frame(_req("cid1", "s3", "resume", {"session_id": SID, "inplace": False}))
    await c._on_frame(_req("cid1", "mon1", "monitor", {"ids": SID}))
    await _ticks()
    old = list(c.streams.values())
    assert len(old) == 2

    c._on_connected()                       # 릴레이 재기동 → 커넥터가 다시 붙음(run() 이 부르는 것)
    for t in old:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(t, 5)
    await _ticks()
    assert [cm.closed for cm in cms] == [True, True] and c.streams == {} and c.stream_in == {}

    # 옛 cid 앞으로 늦게 나가는 응답·출력은 보내지 않는다(릴레이가 모르는 cid 라 방 전체로 갔다)
    await c._res(("cid1", "c9"), True, data={"secret": 1})
    await c._stream_send(("cid1", "s3"), data="late")
    assert c.ws.wire == []

    # 같은 폰이 새 cid 로 다시 붙어 같은 번호를 다시 써도 새 파이프는 정상이고, 옛 cid 의 닫기 신호는 새 파이프를 건드리지 않는다
    await c._on_frame(_req("cid2", "s3", "resume", {"session_id": SID, "inplace": False}))
    await _ticks()
    new = c.streams[("cid2", "s3")]
    await c._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "s3", "cid": "cid1"}))
    await _ticks()
    assert not new.done()
    await c._stream_send(("cid2", "s3"), data="live")
    assert c.ws.wire[-1] == {"v": 1, "type": "stream", "id": "s3", "cid": "cid2", "data": "live"}
    # 릴레이는 살아 있고 커넥터만 다시 붙은 경우: 옛 cid 의 기기가 다시 말을 걸면 그 cid 는 다시 받는다
    c._on_connected()
    await _ticks()
    await c._on_frame(_req("cid2", "c1", "ping", {}))
    assert c.ws.wire[-1] == {"v": 1, "type": "pong", "id": "c1", "cid": "cid2"}


@pytest.mark.asyncio
async def test_stream_owner_comes_from_its_key_not_the_bounded_route_table(wired, monkeypatch):
    """스트림의 주인(cid)은 키에서 읽는다 — 응답 길 표(req_cid)는 상한(4000)을 넘으면 오래된 것부터 지우고 재접속 때 비운다.

    표에 기대면 오래 열어 둔 터미널의 입력이 '인증 없는 기기'로 몰려 파이프가 끊기고, client_offline·drop_device 가 그 파이프를 찾지 못했다.
    """
    monkeypatch.setattr("session_manager.connector.connect", lambda url, **kw: _BlockCM())
    monkeypatch.setattr("session_manager.connector._priv_ok", lambda params: True)
    c = wired
    await c._on_frame(_req("cidA", "t1", "terminal", {"session_id": SID}))
    await c._on_frame(_req("cidB", "t1", "terminal", {"session_id": SID}))
    await _ticks()
    ta, tb = c.streams[("cidA", "t1")], c.streams[("cidB", "t1")]
    c.req_cid.clear()                       # 상한을 넘겨 오래된 길이 지워진 것과 같다
    monkeypatch.setattr("session_manager.devices.enforced", lambda: True)
    monkeypatch.setattr("session_manager.devices.is_active", lambda did: True)
    c.authed = {"cidA": {"id": "dA"}, "cidB": {"id": "dB"}}
    await c._on_frame(json.dumps({"v": 1, "type": "stream_in", "id": "t1", "cid": "cidA", "data": {"type": "input", "data": "ls"}}))
    assert c.stream_in[("cidA", "t1")].qsize() == 1                       # 살아 있는 인증 → 통과
    await _ticks()
    assert not ta.done()                                                    # 끊지 않음
    assert await c.drop_device("dA") == 1                                   # 그 기기 해제 → 그 파이프만
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(ta, 5)
    assert not tb.done()
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidB"}))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tb, 5)


@pytest.mark.asyncio
async def test_late_answer_to_departed_device_is_dropped_not_broadcast_in_plaintext(wired, monkeypatch):
    """긴 요청 중에 기기가 떠나면(client_offline·기기 해제) 그 응답은 보내지 않는다.

    E2EE 기기는 떠날 때 enc_cids 에서 빠져 응답이 평문으로, 릴레이가 모르는 cid 앞이라 방 전체로 갔다(다른 기기의 같은 번호 대기에도).
    남아 있는 E2EE 기기의 응답은 그대로 봉투로 간다.
    """
    import os

    from session_manager import e2ee
    key = os.urandom(32)
    c = wired
    c.e2ee_key = key
    gate = asyncio.Event()

    async def _get(url, params=None, headers=None):
        await gate.wait()
        return _Resp({"secret": url.rsplit("/", 1)[-1]})
    monkeypatch.setattr(c.http, "get", _get)

    def env(cid, rid, path):
        inner = {"v": 1, "type": "req", "id": rid, "method": "api", "params": {"verb": "GET", "path": path}}
        return json.dumps({"v": 1, "e": "a1", "cid": cid, "p": e2ee.seal_payload(key, inner)})
    tasks = [asyncio.create_task(c._on_frame(env("cidA", "c7", "/api/a"))),       # E2EE 폰 — 떠남
             asyncio.create_task(c._on_frame(env("cidB", "c7", "/api/b"))),       # E2EE 태블릿 — 남음
             asyncio.create_task(c._on_frame(_req("cidC", "c7", "api", {"verb": "GET", "path": "/api/c"})))]   # 평문 기기 — 떠남
    await _ticks()
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"}))
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidC"}))
    gate.set()
    await asyncio.gather(*tasks)
    await _drain(c)
    assert [(f.get("cid"), f.get("e")) for f in c.ws.wire] == [("cidB", "a1")], "떠난 기기 앞 응답은 평문으로도 봉투로도 나가지 않는다"
    inner = e2ee.unwrap_frame(key, c.ws.wire[0])
    assert inner["id"] == "c7" and inner["data"]["json"] == {"secret": "b"}


# ---- 검토 3회차: 느린 요청 하나가 그 PC 의 모든 기기 프레임을 막음(줄 막힘)·그 늦은 응답이 떠난 기기 앞으로 broadcast ----

@pytest.mark.asyncio
async def test_slow_request_does_not_hold_up_other_frames(fake_claude_home, monkeypatch):
    """커넥터가 프레임을 하나씩 처리해, Host 쪽 느린 요청(업데이트 확인·큰 목록·내보내기) 하나가 끝날 때까지 다른 기기의 기기 인증·
    client_offline 까지 막혔다(실제 릴레이 e2e: 다시 붙은 폰의 인증이 3.6초 밀림). 그 사이 떠난 기기의 늦은 응답은 그 뒤에 줄 선
    client_offline 보다 먼저 나가 방 전체로 broadcast 됐다. 느린 요청은 따로 돌고 받는 길은 바로 다음 프레임을 처리한다."""
    from session_manager import devices
    c = _conn()
    c.ws = _RelayWS()
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    gate = asyncio.Event()

    async def _get(url, params=None, headers=None):
        await gate.wait()
        return _Resp({"slow": url.rsplit("/", 1)[-1]})
    monkeypatch.setattr(c.http, "get", _get)
    tablet = devices.add_device("tablet")
    # 폰 A 의 느린 요청 — 받는 길은 바로 다음 프레임으로 간다(고치기 전: 여기서 응답까지 막혀 시간 초과)
    await asyncio.wait_for(c._on_frame(_req("cidA", "c6", "api", {"verb": "GET", "path": "/api/owner/update/status"})), 1)
    # 그 사이 태블릿 B 가 붙어 인증한다 — 느린 요청을 기다리지 않고 바로 답한다
    await asyncio.wait_for(c._on_frame(_req("cidB", "c1", "auth", {"token": tablet["token"], "pv": 2})), 1)
    assert "cidB" in c.authed and [(f.get("cid"), f.get("id"), f.get("ok")) for f in c.ws.wire] == [("cidB", "c1", True)]
    # 폰 A 가 떠난다 — 곧바로 처리돼, 늦게 끝난 A 의 응답은 보내지 않는다
    await asyncio.wait_for(c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"})), 1)
    gate.set()
    await _drain(c)
    assert [f.get("cid") for f in c.ws.wire] == ["cidB"], "떠난 기기 앞 늦은 응답은 나가지 않는다"
    # 남아 있는 기기의 느린 요청은 끝나면 그대로 답한다 — 그사이 다른 프레임(핑)도 바로 처리된다
    gate.clear()
    await asyncio.wait_for(c._on_frame(_req("cidB", "c2", "api", {"verb": "GET", "path": "/api/x"})), 1)
    await asyncio.wait_for(c._on_frame(_req("cidB", "c3", "ping", {})), 1)
    assert c.ws.wire[-1] == {"v": 1, "type": "pong", "id": "c3", "cid": "cidB"}
    gate.set()
    await _drain(c)
    assert c.ws.wire[-1]["id"] == "c2" and c.ws.wire[-1]["data"]["json"] == {"slow": "x"}


@pytest.mark.asyncio
async def test_auth_and_stream_start_keep_their_order(wired, monkeypatch):
    """따로 도는 것은 느릴 수 있는 요청뿐 — 기기 인증 뒤의 요청은 인증된 상태를 보고, 스트림 시작 뒤의 입력은 그 스트림으로 간다."""
    monkeypatch.setattr("session_manager.connector.connect", lambda url, **kw: _BlockCM())
    monkeypatch.setattr("session_manager.connector._priv_ok", lambda params: True)
    c = wired
    await c._on_frame(_req("cidA", "t1", "terminal", {"session_id": SID}))
    await c._on_frame(json.dumps({"v": 1, "type": "stream_in", "id": "t1", "cid": "cidA", "data": {"type": "input", "data": "ls"}}))
    assert c.stream_in[("cidA", "t1")].qsize() == 1          # 시작이 끝난 뒤 입력 — 큐가 이미 있다
    c.streams[("cidA", "t1")].cancel()
    await _ticks()
