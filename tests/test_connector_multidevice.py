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


def test_local_http_keepalive_expires_before_host_server_closes():
    """커넥터 → 로컬 Host(uvicorn) 호출의 연결 재사용 기한은 Host 의 keep-alive 보다 넉넉히 짧아야 한다(검토 3회차).

    httpx 기본 재사용 기한(5초)이 uvicorn 기본 keep-alive(5초)와 같아, 직전 호출 뒤 5초 언저리에 온 요청이 서버가 막 닫은 연결을
    다시 써 'ReadError' 로 실패했다(실제 릴레이 e2e 의 reqAfter, 따로 잰 4.98초 3/10·5.00초 1/10). Host 는 uvicorn 기본값으로 뜬다.
    """
    import inspect

    import uvicorn
    server_keepalive = inspect.signature(uvicorn.Config).parameters["timeout_keep_alive"].default
    c = _conn()
    expiry = c.http._transport._pool._keepalive_expiry
    assert expiry is not None and expiry <= server_keepalive - 2, (expiry, server_keepalive)


# ---- 검토 4회차: 따로 도는 요청의 답 — 봉투로 받은 요청은 봉투로만, 받은 뒤 그 기기가 끝났으면(떠남·해제·릴레이 재접속) 보내지 않는다 ----
# 받을 때 정하지 않고 보낼 때 정하면, 그 사이 릴레이가 위조한 client_offline(enc_cids 에서 뺌) + 평문 ping(떠남 표시를 풂) 두 프레임만으로
# 봉투로 받은 요청의 답(대화·목록·2차 인증 유예 토큰)이 평문으로 나갔다. E2EE 가 막아야 할 상대가 바로 릴레이다.

def _env(key, cid, inner):
    from session_manager import e2ee
    return json.dumps({"v": 1, "e": "a1", "cid": cid, "p": e2ee.seal_payload(key, inner)})


def _opened(key, wire):
    """선에 나간 프레임 — 봉투면 열어서(sealed 표시와 함께)."""
    from session_manager import e2ee
    return [({**e2ee.unwrap_frame(key, f), "sealed": True} if f.get("e") else {**f, "sealed": False}) for f in wire]


async def _e2ee_phone(monkeypatch, *, cid="cidA"):
    """진짜 기기 인증(enforced 그대로)으로 붙은 E2EE 폰 하나 + Host 에서 기다리는 GET(gate)·바로 끝나는 POST."""
    import os

    from session_manager import devices
    key = os.urandom(32)
    c = _conn()
    c.ws = _RelayWS()
    c.e2ee_key = key
    gate, hits = asyncio.Event(), []

    async def _get(url, params=None, headers=None):
        hits.append(("GET", url, dict(headers or {})))
        await gate.wait()
        return _Resp({"transcript": "SECRET-" + url.rsplit("/", 2)[-2]})

    async def _post(url, params=None, json=None, headers=None, **kw):
        hits.append(("POST", url, dict(headers or {})))
        return _Resp({"ok": True})
    monkeypatch.setattr(c.http, "get", _get)
    monkeypatch.setattr(c.http, "post", _post)
    dev = devices.add_device("phone")
    await c._on_frame(_env(key, cid, {"v": 1, "type": "req", "id": "c1", "method": "auth", "params": {"token": dev["token"], "pv": 2}}))
    assert cid in c.authed and cid in c.enc_cids
    return c, key, gate, hits, dev


def _answers(key, c, rid):
    return [f for f in _opened(key, c.ws.wire) if f.get("type") == "res" and f.get("id") == rid]


@pytest.mark.asyncio
@pytest.mark.parametrize("require", [False, True])
async def test_forged_offline_and_plaintext_ping_never_release_a_sealed_answer(fake_claude_home, monkeypatch, require):
    """릴레이가 위조할 수 있는 두 프레임(client_offline + 평문 ping — ping 은 [e2ee] require 에도 막히지 않는다)이 봉투로 받은 요청의
    답을 평문으로 내보냈다. 받은 뒤 그 기기가 떠났으면(위조라도) 그 답은 보내지 않는다."""
    from session_manager import appconfig
    orig = appconfig.get_bool
    monkeypatch.setattr(appconfig, "get_bool", lambda s, k, d: True if (s, k) == ("e2ee", "require") and require else orig(s, k, d))
    c, key, gate, hits, _ = await _e2ee_phone(monkeypatch)
    await c._on_frame(_env(key, "cidA", {"v": 1, "type": "req", "id": "c2", "method": "api",
                                         "params": {"verb": "GET", "path": "/api/sessions/S1/messages"}}))
    await _ticks()
    assert hits, "요청이 Host 에서 기다리는 중"
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"}))
    await c._on_frame(json.dumps({"v": 1, "type": "ping", "id": "x", "cid": "cidA"}))
    gate.set()
    await _drain(c)
    assert _answers(key, c, "c2") == [], "떠난(위조라도) 뒤의 답은 평문으로도 봉투로도 나가지 않는다"
    assert not any("SECRET" in json.dumps(f) for f in c.ws.wire)


@pytest.mark.asyncio
async def test_answer_from_previous_relay_connection_is_not_sent_on_the_new_one(fake_claude_home, monkeypatch):
    """커넥터만 릴레이에 다시 붙고(같은 cid 의 폰은 그대로) 그 cid 로 평문 ping 이 오면, 이전 연결에서 받은 요청의 답이 새 연결로 평문으로
    나갔다. 이전 연결에서 받은 요청의 답은 보내지 않는다 — 폰은 PC 가 떠난 것으로 보고 그 대기를 이미 끝냈다(agent_offline)."""
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_env(key, "cidA", {"v": 1, "type": "req", "id": "c2", "method": "api",
                                         "params": {"verb": "GET", "path": "/api/sessions/S1/messages"}}))
    await _ticks()
    c._on_connected()                                     # 릴레이 재접속(run() 이 부르는 것)
    await c._on_frame(json.dumps({"v": 1, "type": "ping", "id": "x2", "cid": "cidA"}))
    # 진짜 폰은 agent_online 에 봉투로 다시 인증한다 — 그 답은 봉투로 간다
    await c._on_frame(_env(key, "cidA", {"v": 1, "type": "req", "id": "c3", "method": "auth", "params": {"token": dev["token"], "pv": 2}}))
    gate.set()
    await _drain(c)
    assert _answers(key, c, "c2") == []
    a = _answers(key, c, "c3")
    assert len(a) == 1 and a[0]["sealed"] and a[0]["ok"] is True
    assert all(f.get("e") for f in c.ws.wire), "봉투로 말한 기기 앞 프레임은 모두 봉투"


@pytest.mark.asyncio
async def test_relay_injected_plaintext_request_under_an_e2ee_device_is_answered_sealed_or_not_at_all(fake_claude_home, monkeypatch):
    """[e2ee] require 가 꺼져 있어도(기본) 릴레이가 E2EE 폰의 cid 로 넣은 평문 요청은 실행하지 않고 내용 없는 e2ee_required 로만
    거절한다(0.32.4). 위조한 client_offline + 평문 ping 을 이어 보내도 봉투로 받은 요청의 답은 평문으로 나가지 않는다."""
    c, key, gate, hits, _ = await _e2ee_phone(monkeypatch)
    gate.set()
    n = len(hits)
    await c._on_frame(_req("cidA", "c2", "api", {"verb": "GET", "path": "/api/sessions/S2/messages"}))
    await _drain(c)
    refused = [f for f in c.ws.wire if f.get("type") == "res" and f.get("id") == "c2"]
    assert [(f.get("error"), "data" in f) for f in refused] == [("e2ee_required", False)] and len(hits) == n, "Host 에 닿지 않는다"
    c3 = _env(key, "cidA", {"v": 1, "type": "req", "id": "c3", "method": "api",
                            "params": {"verb": "GET", "path": "/api/sessions/S3/messages"}})
    gate.clear()
    await c._on_frame(c3)
    await _ticks()
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"}))
    await c._on_frame(json.dumps({"v": 1, "type": "ping", "id": "x", "cid": "cidA"}))
    gate.set()
    await _drain(c)
    assert _answers(key, c, "c3") == []
    assert not any(f.get("type") == "res" and not f.get("e") and f.get("error") != "e2ee_required" for f in c.ws.wire),         "내용 없는 거절 말고 평문 답은 하나도 없다"


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_up", ["sealed", "plain"])
async def test_revoked_device_gets_no_late_answer_even_if_it_keeps_talking(fake_claude_home, monkeypatch, follow_up):
    """PC 에서 기기를 해제하면(drop_device) 그 기기가 프레임을 하나 더 보내도 해제 전에 받은 요청의 답은 가지 않는다 — 1eb2b26 의
    '해제한 기기 앞 늦은 응답은 보내지 않는다'가 새 프레임 하나로 풀렸다(봉투면 봉투로, 평문이면 평문으로 나갔다)."""
    from session_manager import devices
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_env(key, "cidA", {"v": 1, "type": "req", "id": "c2", "method": "api",
                                         "params": {"verb": "GET", "path": "/api/sessions/S1/messages"}}))
    await _ticks()
    devices.revoke(dev["id"])
    assert await c.drop_device(dev["id"], notice="device_removed") == 1      # /api/owner/devices/{id}/revoke 가 하는 것
    ping = {"v": 1, "type": "req", "id": "c3", "method": "ping", "params": {}}
    await c._on_frame(_env(key, "cidA", ping) if follow_up == "sealed" else _req("cidA", "c3", "ping", {}))
    gate.set()
    await _drain(c)
    assert _answers(key, c, "c2") == []


@pytest.mark.asyncio
async def test_sealed_request_without_cid_is_answered_sealed(fake_claude_home, monkeypatch):
    """cid 를 싣지 않은 봉투(옛 릴레이 — 또는 cid 를 지운 릴레이)의 답도 봉투로 — 전엔 cid 가 없으면 평문으로 답했다."""
    import os
    key = os.urandom(32)
    c = _conn()
    c.ws = _RelayWS()
    c.e2ee_key = key
    await c._on_frame(json.dumps({"v": 1, "e": "a1", "p": __import__("session_manager.e2ee", fromlist=["x"]).seal_payload(
        key, {"v": 1, "type": "req", "id": "c1", "method": "ping", "params": {}})}))
    assert len(c.ws.wire) == 1 and c.ws.wire[0].get("e") == "a1", c.ws.wire


# ---- 검토 4회차: 따로 도는 요청에 순서·상한이 없었다 — 한 기기의 두 쓰기가 Host 에서 겹쳐 저장소가 깨지고(관제 그룹 사라짐), 몰린 요청이
# Host 의 스레드(40)를 다 써 PC 자신의 /api/health 가 3초를 넘겼다. 기기마다 받은 순서대로 하나씩, 전체 동시 실행은 상한, 쓰기는 한 번에 하나.

class _Host:
    """Host 대역: 경로마다 문(gate)을 두고 지금 몇 개가 동시에 도는지 잰다."""

    def __init__(self):
        self.gates, self.log, self.cur, self.max = {}, [], 0, 0

    def gate(self, key):
        return self.gates.setdefault(key, asyncio.Event())

    async def _run(self, verb, url):
        key = url.rsplit("/", 1)[-1]
        self.cur += 1
        self.max = max(self.max, self.cur)
        self.log.append(("start", verb, key))
        try:
            await self.gate(key).wait()
        finally:
            self.cur -= 1
            self.log.append(("end", verb, key))
        return _Resp({"done": key})

    def install(self, monkeypatch, c):
        async def _get(url, params=None, headers=None):
            return await self._run("GET", url)

        async def _post(url, params=None, json=None, headers=None, **kw):
            return await self._run("POST", url)
        monkeypatch.setattr(c.http, "get", _get)
        monkeypatch.setattr(c.http, "post", _post)

    def started(self, key):
        return ("start", "GET", key) in self.log or ("start", "POST", key) in self.log


def _api(cid, rid, verb, key):
    return _req(cid, rid, "api", {"verb": verb, "path": f"/api/x/{key}"})


@pytest.mark.asyncio
async def test_one_devices_requests_run_one_at_a_time_in_the_order_received(conn, monkeypatch):
    """팀 알림 체크 두 개를 연달아 누르면 POST 둘이 한꺼번에 온다 — Host 에서 겹치면 관제 그룹 저장(같은 .tmp 이름)이 깨졌다(실제 Host:
    k=2 12회 중 18건 오류, 그룹 사라짐). 한 기기의 요청은 받은 순서대로 앞 요청이 끝난 뒤 시작한다(main·f1dcf7b 와 같은 순서)."""
    h = _Host()
    h.install(monkeypatch, conn)
    await conn._on_frame(_api("cidA", "c1", "POST", "n1"))
    await conn._on_frame(_api("cidA", "c2", "POST", "n2"))
    await conn._on_frame(_api("cidA", "c3", "GET", "g3"))
    await _ticks(10)
    assert h.started("n1") and not h.started("n2") and not h.started("g3"), h.log
    h.gate("n1").set()
    await _ticks(10)
    assert h.started("n2") and not h.started("g3"), h.log
    h.gate("n2").set(); h.gate("g3").set()
    await _drain(conn)
    assert [k for ev, _, k in h.log if ev == "start"] == ["n1", "n2", "g3"] and h.max == 1
    assert [f["id"] for f in conn.sent if f.get("type") == "res"] == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_one_devices_reads_run_together_but_never_cross_its_writes(conn, monkeypatch):
    """폰 화면은 목록·상세·통계를 한꺼번에 묻는다 — 같은 기기의 읽기(GET)끼리는 함께 돈다(2026-10-27). 다만 쓰기는 앞서 받은 읽기가
    다 끝난 뒤, 쓰기 뒤에 받은 읽기는 그 쓰기가 끝난 뒤 — 쓰기 앞뒤의 읽기는 받은 순서대로 쓰기 전·후의 상태를 본다."""
    h = _Host()
    h.install(monkeypatch, conn)
    await conn._on_frame(_api("cidA", "c1", "GET", "g1"))
    await conn._on_frame(_api("cidA", "c2", "GET", "g2"))
    await conn._on_frame(_api("cidA", "c3", "POST", "w3"))
    await conn._on_frame(_api("cidA", "c4", "GET", "g4"))
    await conn._on_frame(_api("cidA", "c5", "GET", "g5"))
    await _ticks(10)
    assert h.started("g1") and h.started("g2") and not h.started("w3") and not h.started("g4"), h.log
    h.gate("g2").set()                       # 늦게 받은 읽기가 먼저 끝나도 된다(답은 끝난 순서로)
    await _ticks(10)
    assert not h.started("w3"), "쓰기는 앞서 받은 읽기가 모두 끝난 뒤"
    h.gate("g1").set()
    await _ticks(10)
    assert h.started("w3") and not h.started("g4") and not h.started("g5"), h.log
    h.gate("w3").set()
    await _ticks(10)
    assert h.started("g4") and h.started("g5"), "쓰기 뒤의 읽기 둘은 함께"
    h.gate("g4").set(); h.gate("g5").set()
    await _drain(conn)
    starts = [k for ev, _, k in h.log if ev == "start"]
    assert starts.index("w3") > max(starts.index("g1"), starts.index("g2"))
    assert h.log.index(("start", "GET", "g4")) > h.log.index(("end", "POST", "w3"))
    assert sorted(f["id"] for f in conn.sent if f.get("type") == "res") == ["c1", "c2", "c3", "c4", "c5"]
    assert conn._lanes == {}, "끝난 기기의 순서 표는 남기지 않는다"


@pytest.mark.asyncio
async def test_another_devices_read_is_not_held_up_but_writes_never_overlap(conn, monkeypatch):
    """다른 기기의 읽기는 기다리지 않는다(3회차 고침 유지) — 쓰기는 기기가 달라도 한 번에 하나(main 처럼 릴레이로 온 쓰기는 겹치지 않는다)."""
    h = _Host()
    h.install(monkeypatch, conn)
    await conn._on_frame(_api("cidA", "c1", "POST", "a1"))     # 폰 A 의 느린 쓰기
    await conn._on_frame(_api("cidB", "c1", "GET", "b1"))      # 태블릿 B 의 읽기 — 바로
    await conn._on_frame(_api("cidB", "c2", "POST", "b2"))     # B 의 쓰기 — A 의 쓰기가 끝난 뒤
    await _ticks(10)
    assert h.started("a1") and h.started("b1") and not h.started("b2"), h.log
    h.gate("b1").set()
    await _ticks(10)
    assert not h.started("b2"), "쓰기는 기기가 달라도 겹치지 않는다"
    assert [f["cid"] for f in conn.sent if f.get("type") == "res"] == ["cidB"]
    h.gate("a1").set()
    await _ticks(10)
    assert h.started("b2")
    h.gate("b2").set()
    await _drain(conn)
    assert h.log.index(("start", "POST", "b2")) > h.log.index(("end", "POST", "a1"))
    assert [k for ev, _, k in h.log if ev == "start"] == ["a1", "b1", "b2"]


@pytest.mark.asyncio
async def test_requests_from_many_devices_are_capped(conn, monkeypatch):
    """몰린 요청이 Host 의 스레드를 다 쓰지 않게 전체 동시 실행에 상한(_REQ_CONCURRENCY) — 실측: 120건이 Host 에서 40개씩 돌아 PC 의
    /api/health 가 5초(런처 3초 제한 초과)."""
    from session_manager import connector
    h = _Host()
    h.install(monkeypatch, conn)
    n = connector._REQ_CONCURRENCY + 6
    for i in range(n):
        await conn._on_frame(_api(f"cid{i}", "c1", "GET", f"r{i}"))
    await _ticks(20)
    assert h.cur == connector._REQ_CONCURRENCY and h.max == connector._REQ_CONCURRENCY
    for i in range(n):
        h.gate(f"r{i}").set()
    await _drain(conn)
    assert len([f for f in conn.sent if f.get("type") == "res"]) == n and h.max == connector._REQ_CONCURRENCY


# ---- 검토 4회차: 떠나기 직전에 받은 요청 — 같은 읽기에 온 client_offline·bye_device 가 인증을 먼저 지워, 받아 둔 요청이 실행되지 않았다 ----
# run() 은 `async for raw in ws: await self._on_frame(raw)` 이고 websockets 는 쌓인 프레임을 쉬지 않고 돌려준다. 따로 도는 요청은 받을 때 인증을 보고
# 그 기기를 쥔다(main·f1dcf7b 처럼 떠나기 전에 보낸 요청은 실행된다). 실행 직전에는 그 사이 PC 에서 해제·재발급했는지만 다시 본다.

def _sealed_req(key, rid, method, params):
    return _env(key, "cidA", {"v": 1, "type": "req", "id": rid, "method": method, "params": params})


@pytest.mark.asyncio
async def test_request_received_just_before_the_device_leaves_is_still_executed(fake_claude_home, monkeypatch):
    """폰이 요청 하나를 보내고 바로 닫혀도(앱 종료·화면 끄기 — 릴레이의 client_offline 이 같은 읽기로 온다) 그 요청은 실행된다."""
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_sealed_req(key, "c2", "api", {"verb": "POST", "path": "/api/sessions/S1/label", "body": {"name": "x"}}))
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"}))
    await _drain(c)
    posts = [h for h in hits if h[0] == "POST"]
    assert [p[1] for p in posts] == ["http://127.0.0.1:5100/api/sessions/S1/label"]
    from session_manager import connector
    assert posts[0][2].get(connector.DEVICE_HEADER) == dev["id"] and posts[0][2].get(connector.PROOF_HEADER) == connector.RELAY_PROOF, \
        "사람 증명은 받을 때 인증된 기기로"
    assert _answers(key, c, "c2") == [], "떠난 기기 앞 답은 보내지 않는다"


@pytest.mark.asyncio
async def test_request_received_just_before_bye_device_runs_before_the_self_revoke(fake_claude_home, monkeypatch):
    """폰이 요청 하나 뒤에 곧바로 자기 해제(bye_device)를 보내면 그 요청이 먼저 실행되고, 해제는 그 뒤에 — 받은 순서대로."""
    from session_manager import devices
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_sealed_req(key, "c2", "api", {"verb": "POST", "path": "/api/sessions/S1/label", "body": {"name": "x"}}))
    await c._on_frame(_sealed_req(key, "c3", "bye_device", {}))
    await _drain(c)
    assert [h[1] for h in hits if h[0] == "POST"] == ["http://127.0.0.1:5100/api/sessions/S1/label"]
    a = _answers(key, c, "c3")
    assert len(a) == 1 and a[0]["sealed"] and a[0]["data"] == {"revoked": True}
    assert not devices.is_active(dev["id"]) and "cidA" not in c.authed


@pytest.mark.asyncio
async def test_bye_device_still_revokes_when_the_phone_left_before_it_ran(fake_claude_home, monkeypatch):
    """bye_device 가 앞 요청 뒤에 줄 서 있는 사이 폰이 떠나도(client_offline) 자기 해제는 된다 — 받을 때 인증한 기기로."""
    from session_manager import devices
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_sealed_req(key, "c2", "api", {"verb": "GET", "path": "/api/sessions/S1/messages"}))
    await c._on_frame(_sealed_req(key, "c3", "bye_device", {}))
    await c._on_frame(json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": "cidA"}))
    gate.set()
    await _drain(c)
    assert not devices.is_active(dev["id"])


@pytest.mark.asyncio
async def test_queued_request_of_a_device_revoked_on_the_pc_is_not_executed(fake_claude_home, monkeypatch):
    """줄 서 있는 사이 PC 에서 그 기기를 해제(또는 QR 재발급)했으면 아직 시작하지 않은 요청은 실행하지 않는다."""
    from session_manager import devices
    c, key, gate, hits, dev = await _e2ee_phone(monkeypatch)
    await c._on_frame(_sealed_req(key, "c2", "api", {"verb": "GET", "path": "/api/sessions/S1/messages"}))
    await c._on_frame(_sealed_req(key, "c3", "api", {"verb": "POST", "path": "/api/sessions/S1/label", "body": {"name": "x"}}))
    await _ticks()
    devices.revoke(dev["id"])
    assert await c.drop_device(dev["id"], notice="device_removed") == 1
    gate.set()
    await _drain(c)
    assert [h[0] for h in hits] == ["GET"], hits


@pytest.mark.asyncio
async def test_request_from_an_unauthenticated_device_is_refused_when_received(fake_claude_home, monkeypatch):
    """인증 없는 기기의 요청은 받을 때 바로 auth_required — 줄을 세우지 않는다."""
    import os
    c = _conn()
    c.ws = _RelayWS()
    hits = []

    async def _get(url, params=None, headers=None):
        hits.append(url)
        return _Resp({})
    monkeypatch.setattr(c.http, "get", _get)
    await c._on_frame(_req("cidZ", "c1", "api", {"verb": "GET", "path": "/api/x"}))
    assert c.ws.wire == [{"v": 1, "type": "res", "id": "c1", "cid": "cidZ", "ok": False, "error": "auth_required"}]
    assert not c._req_tasks and hits == []
