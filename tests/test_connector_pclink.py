"""커넥터의 pclink 분기(pclink T3) — 짝지은 PC(A)가 이 Host(B)의 릴레이 방에 client 로 붙어 보내는 `pclink`·`pclink.pair`.

정본 docs/designs/pclink-protocol-v0.md 6절 끝·7절:
- pclink 요청은 기기 인증 관문 앞에서 본체(pclink.inbound.handle)로 간다 — PC 짝은 접속 토큰이 없고 글마다 짝 키로 스스로 인증한다.
- 한 요청에 res 가 여러 번(접수 → 답) 나간다. 답은 그 cid 로만 — 떠났으면 버리고 방 전체에 뿌리지 않는다.
- 긴 묻기가 도는 동안 폰 요청이 막히지 않는다(커넥터의 접속 루프는 글을 하나씩 직렬로 처리한다 — run()).
- PC 등급 기기(kind=pc)는 pclink·pclink.pair·ping 만.

본체는 다른 작업이 채운다 — 여기서는 monkeypatch 로 가짜 handle 을 끼운다.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from session_manager import connector as connector_mod
from session_manager import e2ee
from session_manager.connector import Connector
from session_manager.pclink import inbound

SID = "59f9577b-0d25-47be-994b-29009cf0fba3"
ENV = {"h": {"v": 0, "id": "3b1f0b0e", "kind": "ask"}, "n": "x", "c": "y"}   # 커넥터는 봉투를 열지 않는다 — 모양만


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")


def _req(cid, rid, method, params=None):
    f = {"v": 1, "type": "req", "id": rid, "method": method, "params": params or {}}
    if cid is not None:
        f["cid"] = cid
    return json.dumps(f, ensure_ascii=False)


def _offline(cid):
    return json.dumps({"v": 1, "type": "peer", "event": "client_offline", "cid": cid})


class _Resp:
    def __init__(self, j):
        self.status_code, self._j, self.text = 200, j, ""

    def json(self):
        return self._j


class _Relay:
    """릴레이 쪽 소켓 흉내. run() 의 접속 루프가 실제로 돈다 — 받은 글은 한 번에 하나씩 `await _on_frame`(직렬)."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.wire: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raw = await self.inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def send(self, s):
        self.wire.append(json.loads(s))

    def push(self, raw: str) -> None:
        self.inbox.put_nowait(raw)


class _RelayWS:
    """_on_frame 을 직접 부르는 시험용 — 실제 send(봉투·버리기)를 거친 결과만 모은다."""

    def __init__(self):
        self.wire = []

    async def send(self, s):
        self.wire.append(json.loads(s))


async def _until(pred, timeout=3.0, what="기다린 프레임"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError(f"{what}이(가) {timeout}초 안에 오지 않았다")
        await asyncio.sleep(0.005)


async def _live(monkeypatch, key=None):
    """진짜 run() 을 가짜 릴레이에 붙여 돌린다. (커넥터, 릴레이, run 작업, connect 호출 수 목록)."""
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    monkeypatch.setattr(connector_mod, "CURRENT", None)          # run() 이 바꾸는 전역 — 시험 뒤 되돌린다
    monkeypatch.setattr("session_manager.e2ee.room_key", lambda room, create=True: key)
    relay = _Relay()
    opened = []

    def fake_connect(uri, **kw):
        opened.append(uri)
        return relay
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)
    c = _conn()

    async def _uri():
        return "ws://relay.test/ws?role=agent&room=rm_x", "shared"
    monkeypatch.setattr(c, "_build_uri", _uri)
    task = asyncio.create_task(c.run())
    await _until(lambda: c.ws is relay and c.conn_gen == 1, what="릴레이 접속")
    return c, relay, task, opened


async def _stop(task, jobs=()):
    for j in jobs:
        j.cancel()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _slow_handle(monkeypatch, gate, seen=None, jobs=None):
    """가짜 본체: 접수를 바로 답하고, 실제 묻기(복제본 실행)는 asyncio 작업에서 gate 가 열린 뒤 답한다."""
    jobs = [] if jobs is None else jobs

    async def handle(method, params, cid, reply):
        if seen is not None:
            seen.append((method, params, cid))
        await reply(True, data={"env": {"kind": "accepted"}})

        async def work():
            await gate.wait()
            await reply(True, data={"env": {"kind": "answer"}})
        jobs.append(asyncio.create_task(work()))
    monkeypatch.setattr(inbound, "handle", handle)
    return jobs


def _brief(frames):
    return [(f.get("type"), f.get("id"), f.get("cid"), (f.get("data") or {}).get("env", {}).get("kind")
             if isinstance(f.get("data"), dict) else None) for f in frames]


# ---- ① 긴 pclink 작업 중에도 폰 요청은 막히지 않는다 ----

@pytest.mark.asyncio
async def test_long_pclink_job_does_not_block_phone_requests(monkeypatch):
    c, relay, task, _ = await _live(monkeypatch)
    gate, seen = asyncio.Event(), []
    jobs = _slow_handle(monkeypatch, gate, seen)

    async def _get(url, params=None, headers=None):
        await asyncio.sleep(0.02)
        return _Resp({"path": url.rsplit("/", 1)[-1]})
    monkeypatch.setattr(c.http, "get", _get)
    try:
        relay.push(_req("cidPC", "k7", "pclink", {"env": ENV}))
        await _until(lambda: len(relay.wire) == 1, what="접수")
        relay.push(_req("cidPhone", "c1", "ping"))
        relay.push(_req("cidPhone", "c2", "api", {"verb": "GET", "path": "/api/sessions"}))
        await _until(lambda: len(relay.wire) == 3, what="폰 응답")
        assert jobs and not jobs[0].done(), "묻기는 아직 도는 중인데 폰 요청은 이미 처리됐다"
        gate.set()
        await _until(lambda: len(relay.wire) == 4, what="묻기의 답")
        assert _brief(relay.wire) == [("res", "k7", "cidPC", "accepted"),
                                      ("pong", "c1", "cidPhone", None),
                                      ("res", "c2", "cidPhone", None),
                                      ("res", "k7", "cidPC", "answer")]
        assert relay.wire[2]["data"]["json"] == {"path": "sessions"}
        assert seen == [("pclink", {"env": ENV}, "cidPC")]
    finally:
        await _stop(task, jobs)


# ---- ② 결과 전에 A 가 떠나면 늦은 결과는 버린다(방 전체로 나가지 않는다) ----

@pytest.mark.asyncio
async def test_late_pclink_answer_to_departed_pc_is_dropped_not_broadcast(monkeypatch):
    c, relay, task, _ = await _live(monkeypatch)
    gate = asyncio.Event()
    jobs = _slow_handle(monkeypatch, gate)
    try:
        relay.push(_req("cidPC", "k7", "pclink", {"env": ENV}))
        await _until(lambda: len(relay.wire) == 1, what="접수")
        relay.push(_offline("cidPC"))
        relay.push(_req("cidPhone", "c1", "ping"))                     # 앞 글(client_offline)까지 처리됐다는 표시
        await _until(lambda: len(relay.wire) == 2, what="pong")
        gate.set()
        await asyncio.wait_for(jobs[0], 3)
        await asyncio.sleep(0.02)
        assert _brief(relay.wire) == [("res", "k7", "cidPC", "accepted"), ("pong", "c1", "cidPhone", None)]
        assert all(f.get("cid") is not None for f in relay.wire), "cid 없는 프레임은 릴레이가 방 전체에 뿌린다"
    finally:
        await _stop(task, jobs)


@pytest.mark.asyncio
async def test_late_pclink_answer_after_relay_reconnect_is_dropped(monkeypatch):
    """릴레이에 다시 붙으면(_on_connected) 이전 연결에서 받은 pclink 요청의 답은 버린다.

    A 의 cid 는 인증·암호화·응답 길 표 어디에도 없어(튜플 키, 토큰 없음) 재접속 때 '떠남'으로 적히지 않는다. 릴레이가 재기동했으면
    그 cid 는 사라졌고, 모르는 cid 앞 프레임은 방 전체에 평문으로 뿌려진다. A 는 시간 초과 뒤 poll 로 보관된 결과를 받는다.
    """
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c = _conn()
    c.ws = _RelayWS()
    gate = asyncio.Event()
    jobs = _slow_handle(monkeypatch, gate)
    await c._on_frame(_req("cidPC", "k7", "pclink", {"env": ENV}))
    assert _brief(c.ws.wire) == [("res", "k7", "cidPC", "accepted")]
    c._on_connected()
    gate.set()
    await asyncio.wait_for(jobs[0], 3)
    assert len(c.ws.wire) == 1, "재접속 뒤 늦은 답이 나갔다"
    # 새 연결에서 다시 물으면(poll) 정상으로 답한다
    await c._on_frame(_req("cidPC2", "k8", "pclink", {"env": ENV}))
    assert _brief(c.ws.wire[1:]) == [("res", "k8", "cidPC2", "accepted")]
    jobs[1].cancel()


@pytest.mark.asyncio
async def test_pclink_request_without_cid_never_answers_to_whole_room(monkeypatch):
    """릴레이는 client 글에 늘 cid 를 붙이지만, 붙지 않은 pclink 요청이 와도 답을 방 전체(cid 없는 res)로 보내지 않는다."""
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c = _conn()
    c.ws = _RelayWS()
    gate = asyncio.Event()
    gate.set()
    jobs = _slow_handle(monkeypatch, gate)
    await c._on_frame(_req(None, "k1", "pclink", {"env": ENV}))
    await asyncio.wait_for(jobs[0], 3)
    assert c.ws.wire == []


# ---- ③ pclink 작업과 동시에 폰이 E2EE 로 보낸 요청의 답은 봉인된 채 나간다 ----

@pytest.mark.asyncio
async def test_phone_e2ee_answer_stays_sealed_while_pclink_runs(monkeypatch):
    key = os.urandom(32)
    c, relay, task, _ = await _live(monkeypatch, key=key)
    gate = asyncio.Event()
    jobs = _slow_handle(monkeypatch, gate)

    async def _get(url, params=None, headers=None):
        return _Resp({"secret": "폰-비밀-" + url.rsplit("/", 1)[-1]})
    monkeypatch.setattr(c.http, "get", _get)

    def sealed(cid, rid, method, params):
        inner = {"v": 1, "type": "req", "id": rid, "method": method, "params": params}
        return json.dumps({"v": 1, "e": "a1", "cid": cid, "p": e2ee.seal_payload(key, inner)})   # 릴레이가 겉에 cid 주입
    try:
        relay.push(_req("cidPC", "k7", "pclink", {"env": ENV}))
        await _until(lambda: len(relay.wire) == 1, what="접수")
        relay.push(sealed("cidPhone", "c3", "api", {"verb": "GET", "path": "/api/x"}))
        relay.push(sealed("cidPhone", "c4", "ping", {}))
        await _until(lambda: len(relay.wire) == 3, what="폰 응답")
        gate.set()
        await _until(lambda: len(relay.wire) == 4, what="묻기의 답")
        phone = [f for f in relay.wire if f.get("cid") == "cidPhone"]
        pc = [f for f in relay.wire if f.get("cid") == "cidPC"]
        assert len(phone) == 2 and all(f.get("e") == "a1" and set(f) == {"v", "e", "cid", "p"} for f in phone)
        assert "폰-비밀" not in json.dumps(relay.wire, ensure_ascii=False), "폰 답이 평문으로 샜다"
        inner = [e2ee.unwrap_frame(key, f) for f in phone]
        assert inner[0]["id"] == "c3" and inner[0]["data"]["json"] == {"secret": "폰-비밀-x"}
        assert inner[1]["type"] == "pong" and inner[1]["id"] == "c4"
        # A 는 방 E2EE 키가 없다 — A 앞 답은 봉투(짝 키로 봉인한 pclink 봉투)를 실은 바깥 평문 프레임 그대로
        assert _brief(pc) == [("res", "k7", "cidPC", "accepted"), ("res", "k7", "cidPC", "answer")]
        assert "cidPC" not in c.enc_cids
    finally:
        await _stop(task, jobs)


# ---- ④ PC 등급 관문 + 인증 관문 앞 분기 ----

PC_DENIED = [
    ("api", {"verb": "GET", "path": "/api/sessions"}),
    ("terminal", {"session_id": SID}),
    ("monitor", {"ids": SID}),
    ("ask", {"session_id": SID, "prompt": "hi"}),
    ("export", {"session_id": SID}),
    ("import", {"b64": ""}),
    ("grant", {"otp": "000000"}),
    ("list_sessions", {}),
    ("resume", {"session_id": SID}),
    ("get_stats", {"session_id": SID}),
    ("bye_device", {}),
    ("nope", {}),
]


@pytest.fixture
def gated(monkeypatch):
    """인증 강제 켜짐 + 기기 상태는 늘 살아 있음(PC 기기도 관문 자체는 통과할 수 있는 최악의 경우)."""
    c = _conn()
    c.sent = []

    async def _send(obj):
        c.sent.append(obj)
    monkeypatch.setattr(c, "send", _send)
    monkeypatch.setattr("session_manager.devices.enforced", lambda: True)
    monkeypatch.setattr("session_manager.devices.is_active", lambda did: True)
    monkeypatch.setattr("session_manager.devices.token_version", lambda did: 0)
    monkeypatch.setattr("session_manager.devices.mark_revoked", lambda *a, **k: pytest.fail("PC 가 bye_device 로 기기를 바꿨다"))

    def _no_local(*a, **k):
        raise AssertionError("PC 요청이 로컬 Host 로 나갔다")
    monkeypatch.setattr(c.http, "get", _no_local)
    monkeypatch.setattr(c.http, "post", _no_local)
    monkeypatch.setattr("session_manager.connector.connect", _no_local)
    monkeypatch.setattr("session_manager.connector._priv_ok", lambda params: True)
    c.handled = []

    async def handle(method, params, cid, reply):
        c.handled.append((method, cid))
        await reply(True, data={"env": {"kind": "accepted"}})
    monkeypatch.setattr(inbound, "handle", handle)
    return c


@pytest.mark.asyncio
async def test_pc_device_gets_only_pclink_and_ping(gated, capsys):
    c = gated
    c.authed["cidPC"] = {"id": "pc0123456789abcd", "name": "A-PC", "kind": "pc", "tver": 0}
    for i, (m, p) in enumerate(PC_DENIED):
        await c._on_frame(_req("cidPC", f"r{i}", m, p))
        assert c.sent[-1] == {"v": 1, "type": "res", "id": f"r{i}", "cid": "cidPC", "ok": False,
                              "error": "pc_not_allowed"}, m
    assert len(c.sent) == len(PC_DENIED) and c.streams == {} and c.handled == []
    logs = [ln for ln in capsys.readouterr().out.splitlines() if "pc_not_allowed" in ln]
    assert len(logs) == len(PC_DENIED) and "pc012345" in logs[0]
    # 통과: pclink·pclink.pair → 본체, ping → pong
    await c._on_frame(_req("cidPC", "k1", "pclink", {"env": ENV}))
    await c._on_frame(_req("cidPC", "k2", "pclink.pair", {"step": "init"}))
    await c._on_frame(_req("cidPC", "p1", "ping"))
    assert c.handled == [("pclink", "cidPC"), ("pclink.pair", "cidPC")]
    assert c.sent[-1] == {"v": 1, "type": "pong", "id": "p1", "cid": "cidPC"}
    assert c.authed["cidPC"]["kind"] == "pc", "거부는 인증을 지우지 않는다(auth_required 와 다르다)"


@pytest.mark.asyncio
async def test_pc_kind_from_devices_verify_is_gated_after_auth(gated, monkeypatch):
    """devices.verify 가 kind=pc 를 돌려주는 경우(다른 작업) — auth 는 되더라도 그 뒤 요청은 pclink·ping 만."""
    c = gated
    monkeypatch.setattr("session_manager.devices.verify",
                        lambda tok: {"id": "pcfedcba98765432", "name": "A-PC", "kind": "pc"} if tok == "pc-tok" else None)
    monkeypatch.setattr("session_manager.devices.first_seen", lambda did: False)
    monkeypatch.setattr("session_manager.devices.touch", lambda did: None)
    monkeypatch.setattr("session_manager.devices.set_name_if_empty", lambda did, name: None)
    monkeypatch.setattr("session_manager.appconfig.machine_name", lambda: "B-PC")
    await c._on_frame(_req("cidPC", "a1", "auth", {"token": "pc-tok"}))
    assert c.sent[-1]["ok"] is True and c.authed["cidPC"]["kind"] == "pc"
    await c._on_frame(_req("cidPC", "a2", "list_sessions"))
    assert c.sent[-1]["error"] == "pc_not_allowed"
    await c._on_frame(_req("cidPC", "a3", "pclink", {"env": ENV}))
    assert c.handled == [("pclink", "cidPC")]


@pytest.mark.asyncio
async def test_unauthenticated_pclink_reaches_handle_others_still_auth_required(gated):
    c = gated
    await c._on_frame(_req("cidX", "k1", "pclink", {"env": ENV}))
    await c._on_frame(_req("cidX", "k2", "pclink.pair", {"step": "init"}))
    assert c.handled == [("pclink", "cidX"), ("pclink.pair", "cidX")]
    assert [f["error"] if not f["ok"] else "ok" for f in c.sent] == ["ok", "ok"]
    c.sent.clear()
    for i, (m, p) in enumerate(PC_DENIED + [("pclink.other", {})]):
        await c._on_frame(_req("cidX", f"r{i}", m, p))
        assert c.sent[-1]["error"] == "auth_required", m
    assert c.handled == [("pclink", "cidX"), ("pclink.pair", "cidX")]
    # 인증된 폰은 그대로(PC 관문은 kind=pc 만)
    c.authed["cidPhone"] = {"id": "ph0123456789abcd", "name": "폰", "tver": 0}
    hits = []

    async def _get(url, params=None, headers=None):
        hits.append(url)
        return _Resp({"ok": 1})
    c.http.get = _get
    await c._on_frame(_req("cidPhone", "c1", "api", {"verb": "GET", "path": "/api/sessions"}))
    assert c.sent[-1]["ok"] is True and hits == ["http://127.0.0.1:5100/api/sessions"]


# ---- ⑤ 한 요청에 reply 두 번 → 같은 id·같은 cid 로 res 두 개(응답 길 표가 비거나 같은 id 가 겹쳐도) ----

@pytest.mark.asyncio
async def test_two_replies_to_one_request_go_to_same_id_and_cid(monkeypatch):
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c = _conn()
    c.ws = _RelayWS()
    gate = asyncio.Event()
    jobs = _slow_handle(monkeypatch, gate)
    await c._on_frame(_req("cidPC", "k7", "pclink", {"env": ENV}))
    # 그 사이: 폰이 같은 번호 k7 을 쓰고, 응답 길 표가 상한을 넘겨 지워진다
    await c._on_frame(_req("cidPhone", "k7", "ping"))
    for i in range(4100):
        c._track_cid(f"x{i}", "cidOther")
    c.req_cid.clear()
    gate.set()
    await asyncio.wait_for(jobs[0], 3)
    res = [f for f in c.ws.wire if f["type"] == "res"]
    assert res == [{"v": 1, "type": "res", "id": "k7", "cid": "cidPC", "ok": True, "data": {"env": {"kind": "accepted"}}},
                   {"v": 1, "type": "res", "id": "k7", "cid": "cidPC", "ok": True, "data": {"env": {"kind": "answer"}}}]
    assert ("cidPC", "k7") not in c.req_cid


@pytest.mark.asyncio
async def test_id_less_pclink_request_answer_is_not_stolen_by_later_id_less_request(monkeypatch):
    """id 없는 요청은 튜플 키가 안 되고 응답 길 표의 한 칸(None)을 함께 쓴다 — 늦은 pclink 답이 그 뒤 id 없이 말한 기기로 가지 않는다."""
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    c = _conn()
    c.ws = _RelayWS()
    gate = asyncio.Event()
    jobs = _slow_handle(monkeypatch, gate)
    await c._on_frame(_req("cidPC", None, "pclink", {"env": ENV}))
    await c._on_frame(_req("cidPhone", None, "nope"))          # 응답 길 표 None 칸을 폰 cid 로 덮는다
    gate.set()
    await asyncio.wait_for(jobs[0], 3)
    to_pc = [f for f in c.ws.wire if (f.get("data") or {}).get("env")]
    assert [(f["id"], f["cid"]) for f in to_pc] == [(None, "cidPC"), (None, "cidPC")]


# ---- 방 client 자격만 있으면 커넥터를 끊을 수 있던 글(초대 문자열·짝 PC 도 방 client 자격을 갖는다) ----

@pytest.mark.asyncio
async def test_non_object_frames_do_not_break_the_relay_loop(monkeypatch):
    c, relay, task, opened = await _live(monkeypatch)
    try:
        for raw in ("[1]", "null", '"x"', "5", "{bad"):
            relay.push(raw)
        relay.push(_req("cidPhone", "c1", "ping"))
        await _until(lambda: len(relay.wire) == 1, what="pong")
        assert relay.wire == [{"v": 1, "type": "pong", "id": "c1", "cid": "cidPhone"}]
        assert len(opened) == 1 and c.conn_gen == 1, "접속 루프가 끊겨 다시 붙었다(모든 파이프 해체)"
    finally:
        await _stop(task)
