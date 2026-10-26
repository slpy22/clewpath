"""pclink 묻는 쪽(routes)·받는 쪽(inbound)·보낸 세션 확인(sender)·관리 API 관문.

두 Host(A·B)의 정책을 한 프로세스에 두고, A 의 짝 채널(link)을 B 의 inbound.handle 로 곧장 잇는 가짜 링크로 묻기 전 과정을
돈다. 실제 릴레이·CP 를 거치는 끝까지 시험은 tests/e2e(두 Host 실 e2e, 출시 전 게이트).
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import pytest

from session_manager.pclink import inbound, policy as POL, protocol as P, routes, sender
from tests.test_pclink_policy import ROOM_A, ROOM_B, SID, Clock, FakeCp, FakeDevices, _pair

SA = "5d1e0000-0000-4000-8000-00000000000a"


# ---------------------------------------------------------------- 가짜 짝 채널: A 의 call → B 의 inbound.handle

class FakeLink:
    def __init__(self, b_pol, drop_after_accept=False):
        self.b, self.calls, self.closed = b_pol, [], False
        self.drop_after_accept = drop_after_accept

    async def call(self, method, params, *, on_res, timeout):
        from session_manager.pclink.link import LinkError
        self.calls.append(method)
        q: asyncio.Queue = asyncio.Queue()

        async def reply(ok, data=None, error=None):
            await q.put({"type": "res", "ok": ok, **({"data": data} if data is not None else {}),
                         **({"error": error} if error is not None else {})})
        await inbound.handle(method, params, "cid1", reply, pol=self.b)
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise LinkError("timeout")
            try:
                res = await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                raise LinkError("timeout") from None
            if on_res(res):
                return
            if self.drop_after_accept:
                raise LinkError("timeout")

    async def close(self):
        self.closed = True


class FakePool:
    def __init__(self, link):
        self.link, self.dropped = link, []

    async def get(self, key, factory):
        return self.link

    async def drop(self, key):
        self.dropped.append(key)

    async def sweep(self):
        return 0


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(POL.Policy, "_my_name", staticmethod(lambda: "PC-A"))
    clk = Clock(int(time.time()))
    a = POL.Policy(tmp_path / "A", devices=FakeDevices(), cp=FakeCp("A"), clock=clk)
    b = POL.Policy(tmp_path / "B", devices=FakeDevices(), cp=FakeCp("B"), clock=clk)
    _pair(a, b)
    link = FakeLink(b)
    monkeypatch.setattr(routes, "pool", lambda: FakePool(link))
    calls = []

    def fake_clone(sid, prompt, ask_id):
        calls.append((sid, prompt))
        return "answer", {"ask_id": ask_id, "answer": f"답:{prompt}", "truncated": False, "cost_usd": 0.001,
                          "duration_s": 0.1}
    monkeypatch.setattr(inbound, "_ask_clone", fake_clone)
    contacts = []

    def fake_request_contact(pol, peer, sid):
        contacts.append((peer, sid))
        pol.mark_contact_pending(peer, sid, "ap_x", pol.now() + 1800)
    from session_manager.pclink import service
    monkeypatch.setattr(service, "request_contact", fake_request_contact)
    monkeypatch.setattr(inbound, "_running", 0)
    monkeypatch.setattr(inbound, "_waiting", 0)
    monkeypatch.setattr(inbound, "_tail_bytes", lambda sid, limit: 100_000)     # 어림 0.19달러
    return a, b, link, calls, contacts, clk


def run(coro):
    return asyncio.run(coro)


async def _ask(a, b, prompt="배포 순서는?"):
    r = await routes.ask_remote(a, b.host_id(), SID, prompt, SA)
    await asyncio.sleep(0)
    return r


# ---------------------------------------------------------------- 묻기 흐름

def test_ask_needs_allow_then_first_contact_then_answers(env):
    a, b, link, calls, contacts, _ = env
    r = run(_ask(a, b))
    assert r["status"] == "refused" and r["code"] == "not_allowed" and calls == []
    b.set_allow(SID, True)
    r = run(_ask(a, b))
    assert r["status"] == "refused" and r["code"] == "pending_approval"
    assert contacts == [(a.host_id(), SID)]
    r = run(_ask(a, b))                                 # 승인 대기 중에는 다시 요청하지 않는다
    assert r["code"] == "pending_approval" and len(contacts) == 1
    b.grant_contact(a.host_id(), SID)
    r = run(_ask(a, b))
    assert r["status"] == "answer" and r["answer"] == "답:배포 순서는?" and r["untrusted"] is True
    assert r["request_id"] and calls == [(SID, "배포 순서는?")]
    assert link.calls[0] == "pclink"                    # hello 를 먼저 보낸다


def _ready(a, b):
    b.set_allow(SID, True)
    b.grant_contact(a.host_id(), SID)


def test_paused_and_limit_and_too_long(env, monkeypatch):
    a, b, _, calls, _, _ = env
    _ready(a, b)
    b.set_paused(True)
    assert run(_ask(a, b))["code"] == "paused"
    b.set_paused(False)
    with pytest.raises(routes.PclinkFail) as ei:
        run(_ask(a, b, "x" * (POL.PROMPT_MAX + 1)))
    assert ei.value.code == "too_long"
    monkeypatch.setattr(POL, "PER_MIN", 2)
    assert run(_ask(a, b))["status"] == "answer"
    assert run(_ask(a, b))["status"] == "answer"
    r = run(_ask(a, b))
    assert r["code"] == "limit" and r["retry_after"] > 0
    assert len(calls) == 2


def test_busy_when_host_full(env, monkeypatch):
    a, b, _, calls, _, _ = env
    _ready(a, b)
    monkeypatch.setattr(inbound, "_running", POL.HOST_CONCURRENCY)
    monkeypatch.setattr(inbound, "_waiting", POL.HOST_QUEUE)
    assert run(_ask(a, b))["code"] == "busy" and calls == []


def test_timeout_then_poll_returns_stored_answer_without_rerun(env, monkeypatch):
    a, b, link, calls, _, _ = env
    _ready(a, b)
    gate = {"hold": True}

    def slow_clone(sid, prompt, ask_id):
        t_end = time.monotonic() + 10                  # 시험이 실패해도 스레드가 남아 pytest 가 끝나지 못하는 일이 없게
        while gate["hold"] and time.monotonic() < t_end:
            time.sleep(0.02)
        calls.append((sid, prompt))
        return "answer", {"ask_id": ask_id, "answer": "늦은 답", "truncated": False}
    monkeypatch.setattr(inbound, "_ask_clone", slow_clone)
    monkeypatch.setattr(routes, "CALL_TIMEOUT_S", 0.3)

    async def go():
        with pytest.raises(routes.PclinkFail) as ei:
            await routes.ask_remote(a, b.host_id(), SID, "질문", SA)
        assert ei.value.code == "timeout"
        rid = ei.value.extra["request_id"]
        r = await routes.poll_remote(a, b.host_id(), rid, SA)
        assert r["status"] == "pending" and r["state"] in ("queued", "running")
        gate["hold"] = False
        for _ in range(100):
            await asyncio.sleep(0.02)
            r = await routes.poll_remote(a, b.host_id(), rid, SA)
            if r["status"] == "answer":
                break
        assert r["status"] == "answer" and r["answer"] == "늦은 답"
        r = await routes.poll_remote(a, b.host_id(), "00000000-0000-4000-8000-000000000000", SA)
        assert r["status"] == "refused" and r["code"] == "unknown_ask"
    run(go())
    assert len(calls) == 1


def test_replayed_ask_is_not_executed_again(env):
    """같은 봉투를 다시 보내도(릴레이 재전송) 다시 실행하지 않고 보관된 결과를 돌려준다."""
    a, b, _, calls, _, _ = env
    _ready(a, b)
    out = a.peer_out(b.host_id())
    h = P.header("ask", P.addr(a.host_id(), SA), P.addr(b.host_id(), SID))
    env_ = P.seal(out["key"], h, {"prompt": "한 번만"})
    got = []

    async def reply(ok, data=None, error=None):
        got.append((ok, data, error))

    async def go():
        await inbound.handle("pclink", {"env": env_}, "c", reply, pol=b)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(got) >= 2:
                break
        await inbound.handle("pclink", {"env": env_}, "c", reply, pol=b)
    run(go())
    assert len(calls) == 1
    kinds = [P.open_env(out["key"], d["env"], my_host=a.host_id(), peer_host=b.host_id())[0]["kind"]
             for ok, d, _ in got if ok]
    assert kinds == ["accepted", "answer", "answer"]


def test_tampered_or_unpaired_envelope_rejected(env):
    a, b, _, calls, _, _ = env
    _ready(a, b)
    out = a.peer_out(b.host_id())
    h = P.header("ask", P.addr(a.host_id(), SA), P.addr(b.host_id(), SID))
    env_ = P.seal(out["key"], h, {"prompt": "x"})
    env_["h"] = {**env_["h"], "to": {"host": b.host_id(), "session": SA}}
    got = []

    async def reply(ok, data=None, error=None):
        got.append(error)
    run(inbound.handle("pclink", {"env": env_}, "c", reply, pol=b))
    run(inbound.handle("pclink", {"env": {"h": {}, "n": "", "c": ""}}, "c", reply, pol=b))
    run(inbound.handle("pclink", {}, "c", reply, pol=b))
    assert got == ["pclink:bad_seal", "pclink:not_paired", "pclink:not_paired"] and calls == []


def test_isolation_breach_or_failure_becomes_ask_failed(env, monkeypatch):
    a, b, _, _, _, _ = env
    _ready(a, b)
    from session_manager import webapi
    monkeypatch.setattr(inbound, "_ask_clone", _real_clone)
    monkeypatch.setattr(webapi, "ephemeral_ask", lambda *a_, **k: {"answer": "x", "isolation_breach": True,
                                                                    "tool_uses": ["Bash"]})
    r = run(_ask(a, b))
    assert r["status"] == "error" and r["code"] == "ask_failed" and r["reason"] == "isolation_breach"
    monkeypatch.setattr(webapi, "ephemeral_ask", lambda *a_, **k: {"error": "세션 작업 폴더를 찾을 수 없습니다"})
    r = run(_ask(a, b))
    assert r["code"] == "ask_failed"


_real_clone = inbound._ask_clone


def test_ask_clone_uses_isolated_limits(monkeypatch):
    from session_manager import webapi
    seen = {}

    def fake(sid, prompt, **kw):
        seen.update(kw, sid=sid)
        return {"answer": "가" * (POL.ANSWER_MAX + 5), "cost_usd": 0.01, "duration_s": 1.0}
    monkeypatch.setattr(webapi, "ephemeral_ask", fake)
    kind, out = _real_clone(SID, "q", "id1")
    assert seen["isolated"] is True and seen["timeout"] == POL.ASK_TIMEOUT_S
    assert seen["guardrails"] == {}            # --max-budget-usd 는 과거 누적 비용과 비교돼 쓰지 않는다(T12)
    assert kind == "answer" and out["truncated"] is True and len(out["answer"]) == POL.ANSWER_MAX
    assert out["cost_usd"] == 0.01                     # webapi 가 이번 실행분으로 계산한 값을 그대로


def test_list_only_allowed_sessions(env, monkeypatch):
    a, b, _, _, _, _ = env
    monkeypatch.setattr(inbound, "_allowed_sessions", lambda pol: [{"id": s, "title": "t", "updated_at": 1}
                                                                   for s in pol.allowed_sessions()])
    assert run(routes.list_remote(a, b.host_id()))["sessions"] == []
    b.set_allow(SID, True)
    assert [s["id"] for s in run(routes.list_remote(a, b.host_id()))["sessions"]] == [SID]


def test_unpair_notifies_peer_and_both_forget(env, monkeypatch):
    a, b, link, _, _, _ = env
    monkeypatch.setattr(routes, "_new_link", lambda out: FakeLink(b))
    assert run(routes.notify_unpair(a, b.host_id())) is True
    assert b.list_pairs() == [] and not b.devices.rows        # 상대(B)는 알림을 받고 지웠다
    a.unpair(b.host_id(), notified=True)
    assert a.list_pairs() == []
    with pytest.raises(routes.PclinkFail) as ei:
        run(_ask(a, b))
    assert ei.value.code == "not_paired"


def test_not_paired_peer(env):
    a, _, _, _, _, _ = env
    with pytest.raises(routes.PclinkFail) as ei:
        run(routes.ask_remote(a, "1111111111111111", SID, "x", SA))
    assert ei.value.code == "not_paired"


# ---------------------------------------------------------------- 짝짓기(A 의 진행) — 가짜 링크로 B 에 곧장

def test_accept_invite_driver_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(POL.Policy, "_my_name", staticmethod(lambda: "PC-A"))
    clk = Clock(int(time.time()))
    a = POL.Policy(tmp_path / "A", devices=FakeDevices(), cp=FakeCp("A"), clock=clk)
    b = POL.Policy(tmp_path / "B", devices=FakeDevices(), cp=FakeCp("B"), clock=clk)
    from session_manager.pclink import service
    monkeypatch.setattr(service, "my_room", lambda: dict(ROOM_A))
    monkeypatch.setattr(routes, "_new_link", lambda out: FakeLink(b))
    inv = b.create_invite(name="노트북", **ROOM_B)
    st = a.stash_accept(b.invite_string(inv["inv"]))
    done = run(routes.accept_invite(a, st["accept_id"]))
    assert done["host"] == b.host_id()
    assert a.list_pairs()[0]["state"] == "active" and b.list_pairs()[0]["state"] == "active"


def test_accept_invite_rolls_back_on_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(POL.Policy, "_my_name", staticmethod(lambda: "PC-A"))
    clk = Clock(int(time.time()))
    a = POL.Policy(tmp_path / "A", devices=FakeDevices(), cp=FakeCp("A"), clock=clk)
    b = POL.Policy(tmp_path / "B", devices=FakeDevices(), cp=FakeCp("B"), clock=clk)
    from session_manager.pclink import service
    monkeypatch.setattr(service, "my_room", lambda: dict(ROOM_A))
    monkeypatch.setattr(routes, "_new_link", lambda out: FakeLink(b))
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    b.cancel_invite(inv["inv"])                                  # B 가 취소한 초대
    st = a.stash_accept(s)
    with pytest.raises(routes.PclinkFail) as ei:
        run(routes.accept_invite(a, st["accept_id"]))
    assert ei.value.code == "pair_refused"
    assert a.list_pairs() == [] and not a.devices.rows and not a.cp.live


# ---------------------------------------------------------------- 보낸 세션 확인

REG = [{"session_id": "s-claude", "pid": 100, "proc_start": None, "started_at": None}]


def _chain(*pids):
    return lambda pid: [(p, {"name": "x", "create_time": None}) for p in pids]


def test_sender_finds_claude_ancestor():
    assert sender.session_of_pid(7, registry=REG, chain=_chain(7, 50, 100, 1)) == "s-claude"


def test_sender_rejects_duplicate_claim_and_no_ancestor():
    dup = REG + [{"session_id": "s-claude", "pid": 200}]
    with pytest.raises(sender.BadSender) as ei:
        sender.session_of_pid(7, registry=dup, chain=_chain(7, 100))
    assert ei.value.reason == "duplicate_claim"
    with pytest.raises(sender.BadSender) as ei:
        sender.session_of_pid(7, registry=REG, chain=_chain(7, 8, 9))
    assert ei.value.reason == "no_claude_ancestor"
    with pytest.raises(sender.BadSender):
        sender.session_of_pid(7, registry=REG, chain=lambda pid: [])


def test_sender_skips_reused_pid(monkeypatch):
    from session_manager import peers
    monkeypatch.setattr(peers, "same_process", lambda e, info: False)
    with pytest.raises(sender.BadSender) as ei:
        sender.session_of_pid(7, registry=REG, chain=_chain(7, 100))
    assert ei.value.reason == "no_claude_ancestor"


def test_sender_not_local():
    with pytest.raises(sender.BadSender):
        sender.caller_session("10.0.0.5", 5555, 5100)


import contextlib
import os
from types import SimpleNamespace as NS


@contextlib.contextmanager
def _child_conn(hold_in_grandchild=False):
    """루프백 서버 + 자식 프로세스가 연결. (서버 포트, 클라이언트 포트, 자식 pid). hold_in_grandchild 면 손주도 그 소켓을 물려받는다(POSIX)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    code = ("import socket,subprocess,sys,time;s=socket.create_connection(('127.0.0.1',%d));"
            "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)'],pass_fds=(s.fileno(),)) if %r else None;"
            "print(s.getsockname()[1],flush=True);time.sleep(5)" % (port, hold_in_grandchild))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    conn = None
    try:
        conn, _ = srv.accept()
        cport = int(p.stdout.readline())
        yield port, cport, p.pid
    finally:
        if conn:
            conn.close()
        import psutil
        try:
            for c in psutil.Process(p.pid).children(recursive=True):
                c.kill()
        except psutil.Error:
            pass
        p.kill()
        p.wait()
        srv.close()


def _deny_system_wide(monkeypatch, exc=None):
    import psutil

    def deny(kind="inet"):
        raise (exc or psutil.AccessDenied())
    monkeypatch.setattr(psutil, "net_connections", deny)


def test_owner_pid_of_real_socket():
    """실제 프로세스가 연 루프백 연결의 주인 pid 를 찾는다 — 모든 OS(맥은 같은 사용자 프로세스별 조회로, D15)."""
    with _child_conn() as (port, cport, pid):
        assert sender._owner_pid(cport, port) == pid


@pytest.mark.parametrize("exc", ["AccessDenied", "NotImplementedError"])
def test_owner_pid_falls_back_to_same_user_scan(monkeypatch, exc):
    """시스템 전체 목록이 막히면(맥) 같은 사용자 프로세스마다 연결을 봐서 진짜 자식 pid 를 찾는다."""
    import psutil
    _deny_system_wide(monkeypatch, psutil.AccessDenied() if exc == "AccessDenied" else NotImplementedError())
    monkeypatch.setattr(sender, "OWNER_SCAN_TIMEOUT", 10.0)      # 느린 시험 기계에서도(상한 자체는 아래 시험)
    used = []
    real = sender._owner_pid_same_user
    monkeypatch.setattr(sender, "_owner_pid_same_user", lambda c, s: used.append(1) or real(c, s))
    with _child_conn() as (port, cport, pid):
        assert sender._owner_pid(cport, port) == pid
    assert used == [1]


@pytest.mark.skipif(os.name == "nt", reason="pass_fds 로 소켓 물려주기는 POSIX")
def test_owner_pid_fallback_rejects_inherited_socket(monkeypatch):
    """한 연결을 두 프로세스가 가지면(fork 로 물려받음) 누가 보냈는지 모르니 거부."""
    _deny_system_wide(monkeypatch)
    monkeypatch.setattr(sender, "OWNER_SCAN_TIMEOUT", 10.0)
    with _child_conn(hold_in_grandchild=True) as (port, cport, pid):
        with pytest.raises(sender.BadSender) as ei:
            sender._owner_pid(cport, port)
    assert ei.value.reason == "ambiguous_socket_owner"


# 가짜 프로세스 목록 — 같은 사용자 판정·중복·시간 상한

ME = ("uid", 1000)
CPORT, SPORT = 50001, 5100


def _conn(lport=CPORT, rport=SPORT, ip="127.0.0.1"):
    return NS(laddr=NS(ip=ip, port=lport), raddr=NS(ip="127.0.0.1", port=rport), pid=None)


class _Proc:
    def __init__(self, pid, real=1000, eff=None, conns=(), delay=0.0, deny=False):
        self.pid, self.info = pid, {"uids": NS(real=real, effective=real if eff is None else eff)}
        self._conns, self.delay, self.deny, self.read = list(conns), delay, deny, 0

    def net_connections(self, kind="inet"):
        import psutil
        self.read += 1
        if self.delay:
            time.sleep(self.delay)
        if self.deny:
            raise psutil.AccessDenied(self.pid)
        return self._conns


def _fake_procs(monkeypatch, procs):
    import psutil
    _deny_system_wide(monkeypatch)
    monkeypatch.setattr(sender, "_my_identity", lambda: ME)
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))


def test_owner_pid_fallback_never_reads_other_users(monkeypatch):
    """다른 사용자(·setuid 로 남이 된) 프로세스의 연결은 읽지도 않는다 — 그 연결의 주인이어도 못 찾음 = 거부."""
    other = _Proc(10, real=501, conns=[_conn()])
    setuid = _Proc(11, real=1000, eff=0, conns=[_conn()])
    blocked = _Proc(12, deny=True)                               # 같은 사용자라도 막힌 것(맥 login)은 건너뜀
    mine = _Proc(13, conns=[_conn(lport=CPORT + 1)])            # 다른 연결
    _fake_procs(monkeypatch, [other, setuid, blocked, mine])
    assert sender._owner_pid(CPORT, SPORT) is None
    assert other.read == 0 and setuid.read == 0 and blocked.read == 1 and mine.read == 1
    with pytest.raises(sender.BadSender) as ei:
        sender.caller_session("127.0.0.1", CPORT, SPORT)
    assert ei.value.reason == "no_socket_owner"


def test_owner_pid_fallback_match_rules(monkeypatch):
    """루프백·클라이언트 포트·서버 포트가 모두 맞는 쪽만. 서버 쪽 소켓(포트 뒤집힘)·바깥 주소는 아니다."""
    procs = [_Proc(20, conns=[_conn(lport=SPORT, rport=CPORT)]),       # Host 의 받는 쪽
             _Proc(21, conns=[_conn(ip="10.0.0.5")]),
             _Proc(22, conns=[_conn(), _conn()])]                       # 한 프로세스의 같은 연결 두 줄 = 하나
    _fake_procs(monkeypatch, procs)
    assert sender._owner_pid(CPORT, SPORT) == 22


def test_owner_pid_fallback_rejects_two_owners(monkeypatch):
    _fake_procs(monkeypatch, [_Proc(30, conns=[_conn()]), _Proc(31), _Proc(32, conns=[_conn()])])
    with pytest.raises(sender.BadSender) as ei:
        sender._owner_pid(CPORT, SPORT)
    assert ei.value.reason == "ambiguous_socket_owner"


def test_owner_pid_fallback_time_limit(monkeypatch):
    """다 훑는 데 상한을 넘으면 찾았어도 거부(fail-closed)."""
    monkeypatch.setattr(sender, "OWNER_SCAN_TIMEOUT", 0.05)
    procs = [_Proc(40 + i, delay=0.02) for i in range(10)] + [_Proc(99, conns=[_conn()])]
    _fake_procs(monkeypatch, procs)
    with pytest.raises(sender.BadSender) as ei:
        sender._owner_pid(CPORT, SPORT)
    assert ei.value.reason == "socket_owner_timeout"
    assert procs[-1].read == 0


def test_owner_pid_system_wide_requires_same_user(monkeypatch):
    """시스템 전체 목록에서 찾은 주인도 같은 사용자여야 한다(진짜 자식 프로세스로 사용자만 바꿔 본다)."""
    import psutil
    with _child_conn() as (port, cport, pid):
        rows = [NS(pid=pid, laddr=NS(ip="127.0.0.1", port=cport), raddr=NS(ip="127.0.0.1", port=port))]
        monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": rows)
        assert sender._owner_pid(cport, port) == pid
        monkeypatch.setattr(sender, "_my_identity", lambda: ("uid", -7) if hasattr(os, "getuid") else ("user", "남"))
        with pytest.raises(sender.BadSender) as ei:
            sender._owner_pid(cport, port)
        assert ei.value.reason == "other_user"
        rows.append(NS(pid=pid + 100000, laddr=rows[0].laddr, raddr=rows[0].raddr))
        with pytest.raises(sender.BadSender) as ei:
            sender._owner_pid(cport, port)
        assert ei.value.reason == "ambiguous_socket_owner"


# ---------------------------------------------------------------- HTTP 관문(로컬 전용·폰 경유 E2EE+2FA)

@pytest.fixture
def client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL", "SM_APP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app()) as c:
        yield c


def _phone(monkeypatch, e2ee=True):
    from session_manager import connector as C, devices
    r = devices.add_device("폰")
    h = {C.VIA_HEADER: "relay", C.PROOF_HEADER: C.RELAY_PROOF, C.DEVICE_HEADER: r["id"]}
    if e2ee:
        h[routes.E2EE_HEADER] = "1"
    return h


def test_asker_api_is_local_only(client):
    from session_manager import connector as C
    for path in ("/api/v1/pclink/peers",):
        assert client.get(path).status_code == 200
        assert client.get(path, headers={C.VIA_HEADER: "relay"}).status_code == 403
    r = client.post("/api/v1/pclink/ask", json={"peer": "x", "session": "y", "prompt": "z"},
                    headers={C.VIA_HEADER: "relay"})
    assert r.status_code == 403 and r.json()["error"] == "local_only"


def test_ask_api_rejects_unverified_sender(client, monkeypatch):
    monkeypatch.setattr(sender, "caller_session", lambda *a: (_ for _ in ()).throw(sender.BadSender("no_claude_ancestor")))
    r = client.post("/api/v1/pclink/ask", json={"peer": "x", "session": "y", "prompt": "z"})
    assert r.status_code == 403 and r.json()["code"] == "bad_sender"


def test_phone_on_actions_need_e2ee_and_2fa(client, monkeypatch):
    from session_manager import connector as C, owner2fa
    no_proof = {C.VIA_HEADER: "relay"}
    assert client.post("/api/owner/pclink/invite", json={}, headers=no_proof).json()["error"] == "human_proof_required"
    plain = _phone(monkeypatch, e2ee=False)
    assert client.post("/api/owner/pclink/invite", json={}, headers=plain).json()["error"] == "e2ee_required"
    sealed = _phone(monkeypatch)
    monkeypatch.setattr(owner2fa, "enabled", lambda: False)
    assert client.post("/api/owner/pclink/invite", json={}, headers=sealed).json()["error"] == "2fa_required"
    monkeypatch.setattr(owner2fa, "enabled", lambda: True)
    monkeypatch.setattr(owner2fa, "verify", lambda c, *a, **k: c == "123456")
    monkeypatch.setattr(owner2fa, "verify_grace", lambda t: False)
    assert client.post("/api/owner/pclink/invite", json={"otp": "000000"}, headers=sealed).json()["error"] == "2fa_invalid"
    r = client.post("/api/owner/pclink/invite", json={"otp": "123456"}, headers=sealed)
    assert r.status_code == 200 and r.json()["approval"]["kind"] == "pclink_pair"
    for path, body in (("/api/owner/pclink/accept", {"invite": "x"}),
                       ("/api/owner/pclink/sessions/s1/allow", {"on": True}),
                       ("/api/owner/pclink/pause", {"on": False})):
        assert client.post(path, json=body, headers=plain).json()["error"] == "e2ee_required", path
    assert client.get("/api/owner/pclink/invites/abc", headers=plain).json()["error"] == "e2ee_required"


def test_phone_off_actions_need_only_proof(client, monkeypatch):
    from session_manager import connector as C
    assert client.post("/api/owner/pclink/pause", json={"on": True},
                       headers={C.VIA_HEADER: "relay"}).json()["error"] == "human_proof_required"
    plain = _phone(monkeypatch, e2ee=False)
    assert client.post("/api/owner/pclink/pause", json={"on": True}, headers=plain).json() == {"paused": True}
    r = client.post("/api/owner/pclink/sessions/s1/allow", json={"on": False}, headers=plain)
    assert r.json() == {"session_id": "s1", "allow": False}


def test_local_screen_manages_freely(client):
    assert client.post("/api/owner/pclink/sessions/s1/allow", json={"on": True}).json()["allow"] is True
    st = client.get("/api/owner/pclink").json()
    assert [s["id"] for s in st["sessions"]] == ["s1"] and st["paused"] is False and P.is_host_id(st["host_id"])
    assert client.post("/api/owner/pclink/pause", json={"on": True}).json() == {"paused": True}
    assert client.post("/api/owner/pclink/pause", json={"on": False}).json() == {"paused": False}


def test_phone_approval_decide_for_pclink_needs_e2ee(client, monkeypatch):
    from session_manager import approvals
    ap = approvals.create("pclink_contact", {"peer": "0f1e2d3c4b5a6978", "session_id": "s1", "peer_name": "노트북"},
                          notify=False)
    plain = _phone(monkeypatch, e2ee=False)
    r = client.post(f"/api/owner/approvals/{ap['id']}/decide", json={"approve": True}, headers=plain)
    assert r.status_code == 403 and r.json()["error"] == "e2ee_required"
    r = client.post(f"/api/owner/approvals/{ap['id']}/decide", json={"approve": False}, headers=plain)
    assert r.status_code == 200 and r.json()["status"] == "rejected"    # 거절(끄는 쪽)은 폰 증명만


def test_pclink_approval_executes_on_decide(client, monkeypatch):
    """첫 연락 승인은 요청한 쪽(다른 PC)이 실행할 수 없다 — 결정하면 서버가 바로 실행한다."""
    from session_manager import approvals, owner2fa
    from session_manager.pclink import service
    got = []
    monkeypatch.setattr(service, "execute_contact", lambda a: got.append(a) or {"granted": True})
    monkeypatch.setattr(owner2fa, "enabled", lambda: True)
    monkeypatch.setattr(owner2fa, "verify", lambda c, *a, **k: c == "123456")
    ap = approvals.create("pclink_contact", {"peer": "0f1e2d3c4b5a6978", "session_id": "s1", "peer_name": "노트북"},
                          notify=False)
    r = client.post(f"/api/owner/approvals/{ap['id']}/decide", json={"approve": True, "otp": "123456"})
    assert r.status_code == 200 and r.json()["status"] == "executed" and r.json()["result"] == {"granted": True}
    assert got and got[0]["session_id"] == "s1"


def test_pclink_approval_execute_failure_stays_approved(client, monkeypatch):
    from session_manager import approvals, owner2fa
    from session_manager.pclink import service
    monkeypatch.setattr(service, "execute_contact", lambda a: (_ for _ in ()).throw(POL.PolicyError("not_allowed", 409)))
    monkeypatch.setattr(owner2fa, "enabled", lambda: True)
    monkeypatch.setattr(owner2fa, "verify", lambda c, *a, **k: True)
    ap = approvals.create("pclink_contact", {"peer": "0f1e2d3c4b5a6978", "session_id": "s1"}, notify=False)
    r = client.post(f"/api/owner/approvals/{ap['id']}/decide", json={"approve": True, "otp": "1"})
    assert r.status_code == 409 and r.json()["error"] == "not_allowed" and r.json()["approval"]["status"] == "approved"


def test_old_host_without_pclink_is_peer_outdated(env, monkeypatch):
    """pclink 을 모르는 옛 Host 는 기기 인증 관문에서 auth_required 로 답한다 — 세션에는 peer_outdated 로."""
    a, b, link, _, _, _ = env

    async def old_host(method, params, cid, reply, pol=None):
        await reply(False, error="auth_required")
    monkeypatch.setattr(inbound, "handle", old_host)
    with pytest.raises(routes.PclinkFail) as ei:
        run(_ask(a, b))
    assert ei.value.code == "peer_outdated"


# ---------------------------------------------------------------- 비용 어림(T12 — 사장님 결정 묻기당 2달러·짝당 하루 20달러)

def _jsonl(home, sid, lines):
    from tests.conftest import write_session
    return write_session(home, "P--x", sid, lines)


def test_tail_bytes_counts_from_last_compact_boundary(fake_claude_home):
    big = {"type": "user", "message": {"role": "user", "content": "가" * 2000}}
    f = _jsonl(fake_claude_home, "t1", [big] * 50 + [{"type": "system", "subtype": "compact_boundary"}] + [big] * 3)
    data = f.read_bytes()
    want = len(data) - (data.rfind(b'"compact_boundary"') - data[:data.rfind(b'"compact_boundary"')][::-1].find(b"\n"))
    got = inbound._tail_bytes("t1", 10_000_000, chunk=1024)
    assert got == want and got < len(data) // 5
    assert inbound._tail_bytes("t1", 100, chunk=1024) is None                        # 상한을 넘으면 너무 큼
    _jsonl(fake_claude_home, "t2", [big] * 5)                                         # 압축 지점 없음 = 파일 전체
    assert inbound._tail_bytes("t2", 10_000_000) == (fake_claude_home / "projects" / "P--x" / "t2.jsonl").stat().st_size
    assert inbound._tail_bytes("t2", 1000) is None
    assert inbound._tail_bytes("nope", 1000) == 0


def test_too_big_session_is_refused_before_running(env, monkeypatch):
    a, b, _, calls, _, _ = env
    _ready(a, b)
    monkeypatch.setattr(inbound, "_tail_bytes", lambda sid, limit: None)
    r = run(_ask(a, b))
    assert r["status"] == "refused" and r["code"] == "too_big" and calls == []


def test_day_budget_is_reserved_before_running(env, monkeypatch):
    a, b, _, calls, _, _ = env
    _ready(a, b)
    monkeypatch.setattr(inbound, "_tail_bytes", lambda sid, limit: 1_000_000)        # 어림 1.9달러
    monkeypatch.setattr(POL, "PER_MIN", 100)
    n = 0
    while True:
        r = run(_ask(a, b))
        if r["status"] != "answer":
            break
        n += 1
    assert n == int(POL.DAY_BUDGET_USD // 1.9) and r["code"] == "limit" and r["retry_after"] > 0


def test_allowed_sessions_carry_titles(fake_claude_home, tmp_path):
    """목록에 세션 제목이 실린다(SessionMeta 에 title 칸이 없어 늘 비던 결함 — 노트북 실기에서 발견)."""
    from tests.conftest import write_session
    write_session(fake_claude_home, "P--x", "s-titled", [{"type": "user", "cwd": "C:/x", "message": {"role": "user", "content": "hi"}},
                                                         {"type": "custom-title", "customTitle": "배포 정리"}])
    write_session(fake_claude_home, "P--x", "s-plain", [{"type": "user", "cwd": "C:/x", "message": {"role": "user", "content": "hi"}}])
    pol = POL.Policy(tmp_path / "pol")
    pol.set_allow("s-titled", True)
    pol.set_allow("s-plain", True)
    pol.set_allow("s-gone", True)                                    # 기록이 없는 세션은 빠진다
    got = {x["id"]: x for x in inbound._allowed_sessions(pol)}
    assert set(got) == {"s-titled", "s-plain"}
    assert got["s-titled"]["title"] == "배포 정리" and got["s-plain"]["title"] == "s-plain"[:8]
    assert isinstance(got["s-titled"]["updated_at"], float)


def test_phone_on_actions_need_recent_grace(client, monkeypatch):
    """폰의 pclink '켜는' 동작은 10분 안에 코드를 넣고 받은 grace 만 — 터미널 특권의 8시간 유예를 그대로 쓰지 않는다."""
    from session_manager import owner2fa
    monkeypatch.setattr(owner2fa, "secret", lambda: "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(owner2fa, "enabled", lambda: True)
    sealed = _phone(monkeypatch)
    g = owner2fa.issue_grace()
    assert g["issued"] <= g["exp"] and owner2fa.grace_age(g["grace"]) <= 1
    r = client.post("/api/owner/pclink/sessions/s1/allow", json={"on": True, "grace": g["grace"]}, headers=sealed)
    assert r.status_code == 200 and r.json()["allow"] is True
    # 같은 grace 가 11분 전에 받은 것처럼(유예 기간 설정을 늘려 발급 시각을 앞당겨 계산) — 터미널엔 아직 유효하지만 pclink 엔 오래됨
    monkeypatch.setenv("SM_2FA_GRACE_SEC", str(owner2fa.grace_ttl() + 660))
    assert owner2fa.verify_grace(g["grace"]) is True and owner2fa.grace_age(g["grace"]) > routes.GRACE_FRESH_S
    r = client.post("/api/owner/pclink/sessions/s2/allow", json={"on": True, "grace": g["grace"]}, headers=sealed)
    assert r.status_code == 403 and r.json()["error"] == "2fa_stale"
    assert client.post("/api/owner/pclink/sessions/s2/allow", json={"on": False}, headers=sealed).status_code == 200   # 끄기는 그대로
