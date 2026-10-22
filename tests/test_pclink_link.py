"""pclink 짝 채널(link.py) — 로컬 가짜 릴레이(websockets.serve)와 가짜 CP(httpx MockTransport)로만 시험한다.

가짜 릴레이는 relay.py 의 client 쪽 동작을 흉내 낸다: hello{cid, peer_present}, client 프레임에 cid 를 넣어 Host 쪽(여기선
시험의 on_req)에 넘기고, 자격 거절은 닫기 코드(4402)로 한다.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.asyncio.server import serve

from session_manager.pclink import link as L
from session_manager.pclink.link import LinkError, LinkPool, PeerLink

pytestmark = pytest.mark.asyncio

SECRET = "CRED-SECRET-zz91"
JWT = "JWT-TOKEN-qq77"
ROOM = "room-abcdef0123456789"
CRED = "cred-pub-1"
CP = "https://cp.test/cp"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    L._TOKENS.clear()
    # 가짜 릴레이(127.0.0.1)로 가는 길에 사용자의 프록시가 끼지 않게
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY", "SOCKS_PROXY"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(k.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    yield
    L._TOKENS.clear()


class FakeCP:
    def __init__(self, status: int = 200, ttl: float = 3600, token: str = JWT, boom: bool = False):
        self.status, self.ttl, self.token, self.boom = status, ttl, token, boom
        self.calls: list[dict] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        if self.boom:
            raise httpx.ConnectError(f"연결 거부 {SECRET}")     # 예외 문구에 비밀이 섞여도 밖으로 새지 않아야 한다
        self.calls.append({"url": str(req.url), "body": json.loads(req.content)})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        return httpx.Response(200, json={"token": self.token, "expires_at": time.time() + self.ttl})


class FakeRelay:
    """relay.py 의 client 쪽 흉내. on_req(relay, ws, frame) 가 Host(agent) 역할을 한다."""

    def __init__(self, on_req=None, peer_present=True, reject_code=None, reject_times=10**6, no_hello=False):
        self.on_req = on_req
        self.no_hello = no_hello
        self.peer_present = peer_present
        self.reject_code = reject_code
        self.reject_times = reject_times
        self.paths: list[str] = []
        self.reqs: list[dict] = []
        self.socks: list = []
        self.conns = 0
        self.url = ""

    async def _handler(self, ws):
        self.conns += 1
        self.paths.append(ws.request.path)
        if self.reject_code and self.conns <= self.reject_times:
            await ws.close(code=self.reject_code)
            return
        cid = f"c{self.conns}"
        ws.cid = cid
        self.socks.append(ws)
        if self.no_hello:
            await ws.wait_closed()
            return
        await ws.send(json.dumps({"v": 1, "type": "hello", "role": "client", "cid": cid,
                                  "peer_present": self.peer_present}))
        try:
            async for raw in ws:
                f = json.loads(raw)
                f["cid"] = cid
                self.reqs.append(f)
                if self.on_req is not None:
                    asyncio.ensure_future(self._safe(ws, f))
        except Exception:  # noqa: BLE001
            pass

    async def _safe(self, ws, f):
        try:
            await self.on_req(self, ws, f)
        except Exception:  # noqa: BLE001 — 시험이 이미 끊은 소켓에 보내려 한 경우
            pass

    async def __aenter__(self):
        self._srv = await serve(self._handler, "127.0.0.1", 0, max_size=None)
        port = self._srv.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws"
        return self

    async def __aexit__(self, *a):
        self._srv.close()
        await self._srv.wait_closed()


def res(f, ok=True, data=None, error=None, cid="same", rid="same"):
    m = {"v": 1, "type": "res", "id": f["id"] if rid == "same" else rid, "ok": ok}
    if cid == "same":
        m["cid"] = f["cid"]
    elif cid is not None:
        m["cid"] = cid
    if data is not None:
        m["data"] = data
    if error is not None:
        m["error"] = error
    return json.dumps(m)


async def two_step(relay, ws, f):
    """B 커넥터처럼: 받자마자 접수 res, 조금 뒤 답 res(같은 바깥 id)."""
    await ws.send(res(f, data={"env": {"kind": "accepted", "n": f["params"].get("n")}}))
    await asyncio.sleep(0.05)
    await ws.send(res(f, data={"env": {"kind": "answer", "n": f["params"].get("n")}}))


def make(relay, cp, **kw):
    return PeerLink(relay=relay.url if hasattr(relay, "url") else relay, cp=CP, room=ROOM, cred_id=CRED,
                    cred_secret=SECRET, http=cp.http, connect_timeout=kw.pop("connect_timeout", 3.0), **kw)


def until_answer(got):
    def on_res(r):
        got.append(r)
        return (r.get("data") or {}).get("env", {}).get("kind") == "answer"
    return on_res


# ---------- 기본 왕복 ----------

async def test_call_gets_accepted_then_answer():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        lk = make(relay, cp)
        got = []
        await lk.call("pclink", {"env": {"x": 1}}, on_res=until_answer(got), timeout=5)
        assert [g["data"]["env"]["kind"] for g in got] == ["accepted", "answer"]
        assert all(g["ok"] is True for g in got)
        f = relay.reqs[0]
        assert f["type"] == "req" and f["v"] == 1 and f["method"] == "pclink" and f["params"] == {"env": {"x": 1}}
        assert lk.inflight == 0 and not lk.closed and lk.cid == "c1" and lk.peer_present is True
        await lk.close()
        assert lk.closed


async def test_query_string_and_cp_body():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        lk = make(relay, cp)
        await lk.call("pclink", {}, on_res=until_answer([]), timeout=5)
        q = parse_qs(urlsplit(relay.paths[0]).query)
        assert q == {"role": ["client"], "room": [ROOM], "v": ["1"], "jwt": [JWT]}
        assert cp.calls == [{"url": f"{CP}/client/token", "body": {"public_id": CRED, "secret": SECRET}}]
        await lk.close()


async def test_async_on_res_supported():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        lk = make(relay, cp)
        got = []

        async def on_res(r):
            await asyncio.sleep(0)
            got.append(r)
            return r["data"]["env"]["kind"] == "answer"
        await lk.call("pclink", {}, on_res=on_res, timeout=5)
        assert len(got) == 2
        await lk.close()


# ---------- 오류 코드 ----------

async def test_unknown_method_is_peer_outdated():
    async def old_host(relay, ws, f):
        await ws.send(res(f, ok=False, error="unknown_method:pclink"))
    cp = FakeCP()
    async with FakeRelay(old_host) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_outdated"
        assert lk.inflight == 0 and not lk.closed
        await lk.close()


async def test_other_ok_false_goes_to_on_res():
    async def refuse(relay, ws, f):
        await ws.send(res(f, ok=False, error="pclink:not_paired"))
    cp = FakeCP()
    async with FakeRelay(refuse) as relay:
        lk = make(relay, cp)
        got = []
        await lk.call("pclink", {}, on_res=lambda r: got.append(r) or True, timeout=5)
        assert got[0]["ok"] is False and got[0]["error"] == "pclink:not_paired"
        await lk.close()


async def test_relay_agent_offline_res_is_peer_offline():
    async def relay_says_offline(relay, ws, f):     # 릴레이가 직접 답한 모양(cid 없음, 원 id)
        await ws.send(res(f, ok=False, error="agent_offline", cid=None))
    cp = FakeCP()
    async with FakeRelay(relay_says_offline) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_offline"
        assert lk.peer_present is False and not lk.closed
        # 상대가 없다고 안 뒤로는 보내지 않고 바로 peer_offline
        n = len(relay.reqs)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_offline" and len(relay.reqs) == n
        await lk.close()


async def test_hello_peer_absent_fails_fast_then_agent_online_resumes():
    cp = FakeCP()
    async with FakeRelay(two_step, peer_present=False) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_offline" and relay.reqs == []
        assert lk.peer_present is False and not lk.closed
        # 상대 Host 가 방에 들어오면 릴레이가 agent_online 을 뿌린다 — 같은 연결로 다시 묻는다
        await relay.socks[0].send(json.dumps({"v": 1, "type": "peer", "event": "agent_online"}))
        for _ in range(100):
            if lk.peer_present:
                break
            await asyncio.sleep(0.01)
        got = []
        await lk.call("pclink", {}, on_res=until_answer(got), timeout=5)
        assert len(got) == 2 and relay.conns == 1
        await lk.close()


async def test_peer_agent_offline_during_call():
    async def accept_then_leave(relay, ws, f):
        await ws.send(res(f, data={"env": {"kind": "accepted"}}))
        await asyncio.sleep(0.05)
        await ws.send(json.dumps({"v": 1, "type": "peer", "event": "agent_offline"}))
    cp = FakeCP()
    async with FakeRelay(accept_then_leave) as relay:
        lk = make(relay, cp)
        got = []
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=until_answer(got), timeout=5)
        assert ei.value.code == "peer_offline" and len(got) == 1
        assert lk.peer_present is False and lk.inflight == 0
        await lk.close()


async def test_server_drops_socket_during_call():
    async def accept_then_drop(relay, ws, f):
        await ws.send(res(f, data={"env": {"kind": "accepted"}}))
        await asyncio.sleep(0.05)
        await ws.close()
    cp = FakeCP()
    async with FakeRelay(accept_then_drop) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=until_answer([]), timeout=5)
        assert ei.value.code == "peer_offline"
        assert lk.closed and lk.inflight == 0
        with pytest.raises(LinkError) as ei:            # 끊긴 연결은 다시 쓰지 않는다
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_offline"


async def test_timeout_then_link_still_usable():
    calls = {"n": 0}

    async def slow_first(relay, ws, f):
        calls["n"] += 1
        if calls["n"] == 1:
            await ws.send(res(f, data={"env": {"kind": "accepted"}}))   # 답은 오지 않는다
            return
        await two_step(relay, ws, f)
    cp = FakeCP()
    async with FakeRelay(slow_first) as relay:
        lk = make(relay, cp)
        t0 = time.monotonic()
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=until_answer([]), timeout=0.3)
        assert ei.value.code == "timeout" and time.monotonic() - t0 < 2
        assert lk.inflight == 0 and not lk.closed
        got = []
        await lk.call("pclink", {}, on_res=until_answer(got), timeout=5)
        assert len(got) == 2 and relay.conns == 1
        await lk.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_cp_rejects_is_auth(status):
    cp = FakeCP(status=status)
    async with FakeRelay(two_step) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "auth"
        assert relay.conns == 0 and lk.inflight == 0
        await lk.close()


async def test_cp_unreachable_or_5xx_is_peer_offline():
    async with FakeRelay(two_step) as relay:
        for cp in (FakeCP(boom=True), FakeCP(status=502)):
            lk = make(relay, cp)
            with pytest.raises(LinkError) as ei:
                await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
            assert ei.value.code == "peer_offline"
            await lk.close()
        assert relay.conns == 0


async def test_relay_refused_is_peer_offline():
    async with FakeRelay() as relay:
        url = relay.url
    # 서버를 닫은 뒤 그 포트로 — 접속 거부
    cp = FakeCP()
    lk = make(url, cp)
    with pytest.raises(LinkError) as ei:
        await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
    assert ei.value.code == "peer_offline"
    assert lk.inflight == 0 and not lk.closed     # 붙기 실패는 연결을 버리지 않는다 — 다음 call 이 다시 붙어 본다
    await lk.close()


async def test_relay_connect_timeout_is_peer_offline():
    async def silent(reader, writer):            # TCP 는 받지만 웹소켓 핸드셰이크에 답하지 않는다
        await reader.read()
        writer.close()
    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        lk = make(f"ws://127.0.0.1:{port}/ws", FakeCP(), connect_timeout=0.3)
        t0 = time.monotonic()
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "peer_offline" and time.monotonic() - t0 < 2
        await lk.close()
    finally:
        srv.close()


async def test_relay_rejects_jwt_is_auth_and_retries_cached_once():
    cp = FakeCP()
    async with FakeRelay(reject_code=4402) as relay:
        lk = make(relay, cp)
        with pytest.raises(LinkError) as ei:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert ei.value.code == "auth" and relay.conns == 1 and len(cp.calls) == 1
        await lk.close()
        assert L._TOKENS == {}                       # 거절된 토큰은 캐시에 남지 않는다
    # 캐시한 토큰이 거절되면 새로 받아 한 번만 더 붙는다
    cp2 = FakeCP()
    L._TOKENS[(CP, CRED)] = ("OLD-" + JWT, time.time() + 3600)
    async with FakeRelay(two_step, reject_code=4402, reject_times=1) as relay:
        lk = make(relay, cp2)
        got = []
        await lk.call("pclink", {}, on_res=until_answer(got), timeout=5)
        assert len(got) == 2 and len(cp2.calls) == 1
        assert parse_qs(urlsplit(relay.paths[0]).query)["jwt"] == ["OLD-" + JWT]
        assert parse_qs(urlsplit(relay.paths[1]).query)["jwt"] == [JWT]
        await lk.close()


# ---------- 나누기·무시 ----------

async def test_concurrent_calls_do_not_mix():
    async def reverse(relay, ws, f):
        n = f["params"]["n"]
        await asyncio.sleep(0.2 if n == 1 else 0.02)    # 먼저 보낸 것이 나중에 답한다
        await ws.send(res(f, data={"env": {"kind": "accepted", "n": n}}))
        await asyncio.sleep(0.02)
        await ws.send(res(f, data={"env": {"kind": "answer", "n": n}}))
    cp = FakeCP()
    async with FakeRelay(reverse) as relay:
        lk = make(relay, cp)
        g1, g2 = [], []
        order = []

        async def one(n, g):
            await lk.call("pclink", {"n": n}, on_res=until_answer(g), timeout=5)
            order.append(n)
        await asyncio.gather(one(1, g1), one(2, g2))
        assert [r["data"]["env"]["n"] for r in g1] == [1, 1]
        assert [r["data"]["env"]["n"] for r in g2] == [2, 2]
        assert order == [2, 1] and relay.conns == 1 and len(cp.calls) == 1
        assert len({f["id"] for f in relay.reqs}) == 2
        await lk.close()


async def test_ignores_foreign_res_broadcast_and_unknown_types():
    async def noisy(relay, ws, f):
        await ws.send(res(f, rid="pl999", data={"env": {"kind": "answer", "who": "other-id"}}))
        await ws.send(res(f, cid="someone-else", data={"env": {"kind": "answer", "who": "other-cid"}}))
        await ws.send(res(f, cid=None, data={"env": {"kind": "answer", "who": "broadcast"}}))
        await ws.send(json.dumps({"v": 1, "type": "notice", "cid": f["cid"], "event": "x"}))
        await ws.send(json.dumps({"v": 1, "type": "stream", "id": f["id"], "cid": f["cid"], "data": "zz"}))
        await ws.send(json.dumps({"v": 1, "type": "peer", "event": "client_online", "cid": "c9"}))
        await ws.send("not json")
        await ws.send(json.dumps([1, 2]))
        await ws.send(res(f, data={"env": {"kind": "answer", "who": "me"}}))
    cp = FakeCP()
    async with FakeRelay(noisy) as relay:
        lk = make(relay, cp)
        got = []
        await lk.call("pclink", {}, on_res=until_answer(got), timeout=5)
        assert [g["data"]["env"]["who"] for g in got] == ["me"]
        assert not lk.closed and lk.peer_present is True
        await lk.close()


# ---------- 토큰 캐시 ----------

async def test_token_cached_across_links():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        for _ in range(3):                             # 쉬다 닫힌 연결을 새로 만들어도 CP 는 한 번
            lk = make(relay, cp)
            await lk.call("pclink", {}, on_res=until_answer([]), timeout=5)
            await lk.close()
        assert len(cp.calls) == 1 and relay.conns == 3


async def test_token_near_expiry_is_refetched():
    cp = FakeCP(ttl=200)                               # 만료 5분 안 — 캐시하지 않는다
    async with FakeRelay(two_step) as relay:
        for _ in range(2):
            lk = make(relay, cp)
            await lk.call("pclink", {}, on_res=until_answer([]), timeout=5)
            await lk.close()
        assert len(cp.calls) == 2


# ---------- pool ----------

async def test_pool_reuses_one_link():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        pool = LinkPool()
        made = []

        def factory():
            made.append(make(relay, cp))
            return made[-1]
        a = await pool.get("B", factory)
        await a.call("pclink", {}, on_res=until_answer([]), timeout=5)
        b = await pool.get("B", factory)
        await b.call("pclink", {}, on_res=until_answer([]), timeout=5)
        assert a is b and len(made) == 1 and relay.conns == 1 and len(pool) == 1
        await pool.stop()
        assert a.closed and len(pool) == 0


async def test_pool_concurrent_get_makes_one():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        pool = LinkPool()
        made = []

        async def factory():                           # 만드는 데 시간이 걸리는 공장(자격 읽기 등)
            await asyncio.sleep(0.05)
            made.append(make(relay, cp))
            return made[-1]
        links = await asyncio.gather(*(pool.get("B", factory) for _ in range(5)))
        assert len(made) == 1 and all(x is links[0] for x in links)
        # 한 연결에 동시 call 들 — 붙기도 한 번
        await asyncio.gather(*(x.call("pclink", {}, on_res=until_answer([]), timeout=5) for x in links))
        assert relay.conns == 1 and len(cp.calls) == 1
        await pool.stop()


async def test_pool_replaces_closed_link():
    async def drop(relay, ws, f):
        await ws.close()
    cp = FakeCP()
    async with FakeRelay(drop) as relay:
        pool = LinkPool()
        a = await pool.get("B", lambda: make(relay, cp))
        with pytest.raises(LinkError):
            await a.call("pclink", {}, on_res=lambda r: True, timeout=5)
        assert a.closed
        b = await pool.get("B", lambda: make(relay, cp))
        assert b is not a and not b.closed
        await pool.stop()


async def test_sweep_closes_idle_only():
    gate = asyncio.Event()

    async def held(relay, ws, f):
        if f["params"].get("hold"):
            await gate.wait()
        await two_step(relay, ws, f)
    cp = FakeCP()
    async with FakeRelay(held) as relay:
        pool = LinkPool(idle_s=0.05)
        idle = await pool.get("idle", lambda: make(relay, cp))
        unused = await pool.get("unused", lambda: make(relay, cp))
        busy = await pool.get("busy", lambda: make(relay, cp))
        fresh = await pool.get("fresh", lambda: make(relay, cp))
        await idle.call("pclink", {}, on_res=until_answer([]), timeout=5)
        t = asyncio.ensure_future(busy.call("pclink", {"hold": True}, on_res=until_answer([]), timeout=5))
        await asyncio.sleep(0.15)                      # 모두 idle_s 를 넘겼다 — busy 는 진행 중
        fresh.last_used = time.monotonic() + 10        # 방금 쓴 것
        assert busy.inflight == 1
        n = await pool.sweep()
        assert n == 2                                  # 쓰고 쉬던 것 + 만들고 안 쓴 것
        assert idle.closed and unused.closed and not busy.closed and not fresh.closed
        assert len(pool) == 2
        gate.set()
        await t
        assert not busy.closed
        await pool.stop()


async def test_sweep_counts_and_skips_recent():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        pool = LinkPool(idle_s=60)
        a = await pool.get("A", lambda: make(relay, cp))
        await a.call("pclink", {}, on_res=until_answer([]), timeout=5)
        assert await pool.sweep() == 0 and not a.closed
        a.last_used -= 61
        assert await pool.sweep() == 1 and a.closed and len(pool) == 0
        await pool.stop()


async def test_drop_closes_now_and_fails_inflight():
    async def never(relay, ws, f):
        await ws.send(res(f, data={"env": {"kind": "accepted"}}))
    cp = FakeCP()
    async with FakeRelay(never) as relay:
        pool = LinkPool()
        a = await pool.get("B", lambda: make(relay, cp))
        t = asyncio.ensure_future(a.call("pclink", {}, on_res=until_answer([]), timeout=30))
        for _ in range(200):
            if relay.reqs:
                break
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        await pool.drop("B")
        with pytest.raises(LinkError) as ei:
            await t
        assert ei.value.code == "peer_offline" and time.monotonic() - t0 < 2
        assert a.closed and len(pool) == 0
        b = await pool.get("B", lambda: make(relay, cp))
        assert b is not a
        await pool.drop("B")
        await pool.drop("없는 짝")                       # 없는 key 도 조용히


async def test_close_while_connecting_fails_call():
    async def silent(reader, writer):
        await reader.read()
        writer.close()
    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        lk = make(f"ws://127.0.0.1:{port}/ws", FakeCP(), connect_timeout=5)
        t = asyncio.ensure_future(lk.call("pclink", {}, on_res=lambda r: True, timeout=5))
        await asyncio.sleep(0.1)
        t0 = time.monotonic()
        await lk.close()
        with pytest.raises(LinkError) as ei:
            await t
        assert ei.value.code == "peer_offline" and time.monotonic() - t0 < 1
    finally:
        srv.close()


async def test_close_while_waiting_hello_closes_socket():
    async with FakeRelay(no_hello=True) as relay:
        lk = make(relay, FakeCP(), connect_timeout=5)
        t = asyncio.ensure_future(lk.call("pclink", {}, on_res=lambda r: True, timeout=5))
        for _ in range(200):
            if relay.socks:
                break
            await asyncio.sleep(0.01)
        await lk.close()
        with pytest.raises(LinkError) as ei:
            await t
        assert ei.value.code == "peer_offline"
        await asyncio.wait_for(relay.socks[0].wait_closed(), 3)    # 반쯤 연 소켓을 남기지 않는다


async def test_background_sweep_start_stop():
    cp = FakeCP()
    async with FakeRelay(two_step) as relay:
        pool = LinkPool(idle_s=0.01)
        pool.sweep_every = 0.05
        pool.start()
        pool.start()                                   # 두 번 불러도 작업 하나
        a = await pool.get("B", lambda: make(relay, cp))
        await a.call("pclink", {}, on_res=until_answer([]), timeout=5)
        for _ in range(100):
            if a.closed:
                break
            await asyncio.sleep(0.02)
        assert a.closed and len(pool) == 0
        await pool.stop()
        assert pool._task is None


# ---------- 비밀값 ----------

def _all_text(e: BaseException) -> str:
    return str(e) + repr(e) + "".join(traceback.format_exception(e))


async def test_no_secrets_in_errors_or_logs(capsys):
    errs: list[BaseException] = []

    async def collect(lk, **kw):
        try:
            await lk.call("pclink", {}, on_res=lambda r: True, timeout=kw.get("timeout", 5))
        except LinkError as e:
            errs.append(e)
        await lk.close()

    async def drop(relay, ws, f):
        await ws.close()
    # CP 거절·CP 불통·릴레이 거절·접속 거부·끊김·시간 초과
    await collect(make("ws://127.0.0.1:1/ws", FakeCP(status=401)))
    await collect(make("ws://127.0.0.1:1/ws", FakeCP(boom=True)))
    L._TOKENS.clear()
    await collect(make("ws://127.0.0.1:1/ws", FakeCP()))
    L._TOKENS.clear()
    await collect(make("bad://127.0.0.1:1/ws", FakeCP()))     # InvalidURI 문구에는 주소 전체(jwt 포함)가 들어간다
    async with FakeRelay(reject_code=4402) as relay:
        L._TOKENS.clear()
        await collect(make(relay, FakeCP()))
    async with FakeRelay(drop) as relay:
        L._TOKENS.clear()
        await collect(make(relay, FakeCP()))
    async with FakeRelay(lambda *a: asyncio.sleep(0)) as relay:
        L._TOKENS.clear()
        await collect(make(relay, FakeCP()), timeout=0.2)
    lk = make("ws://127.0.0.1:1/ws", FakeCP())
    codes = [e.code for e in errs]
    assert codes == ["auth", "peer_offline", "peer_offline", "peer_offline", "auth", "peer_offline", "timeout"]
    L._TOKENS.clear()
    for e in errs:
        t = _all_text(e)
        assert SECRET not in t and JWT not in t, t
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and JWT not in out.out + out.err
    assert SECRET not in repr(lk) and JWT not in repr(lk)
