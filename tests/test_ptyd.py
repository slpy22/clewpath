"""터미널 관리 프로세스(PTY 브로커) — docs/designs/pty-broker.md.

진짜 브로커 코드를 스레드로 띄우고(named pipe + authkey 실제 사용) ConPTY 만 가짜로 바꾼다.
핵심 약속: ① Host 가 내려가도 세션은 브로커에서 계속 ② 다시 붙으면 꼬리부터 ③ 일회성(fork)은 화면 주인이 떠나면 종료
④ 브로커가 없으면 열기 거부(D3) ⑤ 하위 claude 에 Claude 세션 환경변수가 새지 않음(S1 실측).
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

from session_manager import agents, hooks, peers, ptyclient, ptyd, webterm
from tests.conftest import write_session

SID = "aaaaaaaa-1111-2222-3333-444444444444"
SID2 = "bbbbbbbb-1111-2222-3333-444444444444"


class FakePty:
    n = 5000
    made: list = []

    def __init__(self, argv, cwd, cols, rows, env):
        FakePty.n += 1
        self.pid = FakePty.n
        self.argv, self.cwd, self.size, self.env = argv, cwd, (rows, cols), env
        self.q: queue.Queue = queue.Queue()
        self.written: list[str] = []
        self.alive = True
        self.q.put(f"hello from {self.pid}\r\n")
        FakePty.made.append(self)

    def read(self):
        item = self.q.get()
        if item is None:
            self.alive = False
            raise EOFError
        return item

    def write(self, d):
        self.written.append(d)
        self.q.put("echo:" + d)

    def setwinsize(self, r, c):
        self.size = (r, c)

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        if self.alive:
            self.alive = False
            self.q.put(None)

    def exit(self):                      # claude 가 스스로 끝남(/exit)
        self.terminate()


@pytest.fixture
def broker(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_PTY_BROKER", "on")
    FakePty.made = []
    monkeypatch.setattr(ptyd, "_pty_spawn", FakePty)
    monkeypatch.setattr(ptyd, "_guard", lambda pid: None)
    monkeypatch.setattr(ptyclient, "launch", lambda: None)
    monkeypatch.setattr(ptyclient, "_LAUNCH_WAIT_S", 0.5)
    monkeypatch.setattr(ptyclient, "_ctl", None)
    monkeypatch.setattr(ptyclient, "_last_hello", {})
    monkeypatch.setattr(agents, "snapshot", lambda max_age=0: [])
    monkeypatch.setattr(hooks, "_persist_status", lambda: None)
    monkeypatch.setattr(webterm, "_SHUTTING_DOWN", False)
    webterm._ACTIVE.clear()
    b = ptyd.Broker(ptyd.pipe_address(), ptyd.authkey(create=True))
    b.exit_process = False
    threading.Thread(target=b.serve_forever, daemon=True).start()
    for _ in range(50):
        if ptyclient.hello():
            break
        time.sleep(0.05)
    yield b
    webterm._ACTIVE.clear()
    b.shutdown()
    if ptyclient._ctl is not None:
        try:
            ptyclient._ctl.close()
        except Exception:  # noqa: BLE001
            pass


def _wait(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _read_until(proc, needle, t=3.0):
    got = []
    done = threading.Event()

    def rd():
        try:
            while needle not in "".join(got):
                got.append(proc.read())
        except EOFError:
            pass
        done.set()
    threading.Thread(target=rd, daemon=True).start()
    done.wait(t)
    return "".join(got)


# ---- 브로커 단독 ----

def test_clean_env_scrubs_claude_session_markers():
    env = ptyd.clean_env({"PATH": "x", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
                          "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PID": "9", "CLAUDE_EFFORT": "high",
                          "CLAUDE_CONFIG_DIR": "keep", "ANTHROPIC_API_KEY": "keep"})
    assert env == {"PATH": "x", "CLAUDE_CONFIG_DIR": "keep", "ANTHROPIC_API_KEY": "keep"}


def test_spawn_stream_write_resize_list_dup_cap(broker):
    p = ptyclient.spawn("k1", SID, ["claude", "--resume", SID], "C:/", 80, 24, True, 2)
    assert _read_until(p, "hello from").startswith("hello from")
    p.write("abc")
    assert "echo:abc" in _read_until(p, "echo:abc")
    p.setwinsize(40, 120)
    assert FakePty.made[0].size == (40, 120)
    assert "CLAUDECODE" not in FakePty.made[0].env, "하위 claude 에 세션 표식이 새지 않는다"
    assert [s["key"] for s in ptyclient.sessions()] == ["k1"]
    with pytest.raises(ptyclient.SpawnRefused) as e:
        ptyclient.spawn("k1", SID, ["x"], "C:/", 80, 24, True, 2)
    assert e.value.code == "dup", "같은 세션에 프로세스 2개 금지(대화 분기)"
    ptyclient.spawn("k2", SID2, ["x"], "C:/", 80, 24, True, 2)
    with pytest.raises(ptyclient.SpawnRefused) as e:
        ptyclient.spawn("k3", "c", ["x"], "C:/", 80, 24, True, 2)
    assert e.value.code == "cap", "상한은 브로커가 최종 강제"


def test_persist_survives_host_detach_and_reattach_gets_tail(broker):
    p = ptyclient.spawn("k1", SID, ["x"], "C:/", 80, 24, True, 12)
    _read_until(p, "hello from")
    p.detach()                                    # Host 가 내려감
    FakePty.made[0].q.put("while-host-away\r\n")  # Host 없는 동안 출력
    assert _wait(lambda: not broker.sessions["k1"].stream)
    time.sleep(0.1)
    assert [s["key"] for s in ptyclient.sessions()] == ["k1"], "세션은 계속 돈다"
    p2 = ptyclient.attach("k1")                    # 새 Host 가 다시 붙음
    out = _read_until(p2, "while-host-away")
    assert "hello from" in out and "while-host-away" in out, "꼬리부터 받는다"


def test_fork_dies_when_its_stream_drops(broker):
    p = ptyclient.spawn("fork1", SID, ["x"], "C:/", 80, 24, False, 12)
    _read_until(p, "hello from")
    p.detach()
    assert _wait(lambda: not ptyclient.sessions()), "일회성(fork)은 화면 주인이 떠나면 종료(D4)"
    assert not FakePty.made[0].alive


def test_session_exit_sends_sentinel_so_host_can_tell_exit_from_broker_death(broker):
    p = ptyclient.spawn("k1", SID, ["x"], "C:/", 80, 24, True, 12)
    _read_until(p, "hello from")
    FakePty.made[0].exit()
    _read_until(p, "never")
    assert p.exited is True and not p.isalive()
    assert _wait(lambda: not ptyclient.sessions())


def test_wrong_authkey_is_rejected(broker):
    from multiprocessing.connection import Client
    with pytest.raises(Exception):
        Client(ptyd.pipe_address(), family="AF_PIPE", authkey=b"x" * 32)


def test_shutdown_refuses_with_sessions_unless_forced(broker):
    ptyclient.spawn("k1", SID, ["x"], "C:/", 80, 24, True, 12)
    r = ptyclient.request("shutdown")
    assert r["ok"] is False and r["error"] == "has_sessions"


# ---- Host(webterm) 통합 ----

def _mk_session(home, sid=SID):
    cwd = home.parent / "work"
    cwd.mkdir(exist_ok=True)
    write_session(home, "F--work", sid, [{"type": "user", "cwd": str(cwd), "slug": "s",
                                          "message": {"role": "user", "content": "hi"}}])


def test_webterm_start_restart_adopt_stop(broker, fake_claude_home, monkeypatch):
    _mk_session(fake_claude_home)
    gone = []
    monkeypatch.setattr(hooks, "mark_gone", lambda sid, reason="process_exit": gone.append(sid))
    r = webterm.start_terminal(SID)
    assert r["status"] == "started" and webterm.has_terminal(SID)
    assert webterm.start_terminal(SID)["status"] == "already_live", "멱등"
    assert webterm.write_to(SID, "y")
    assert _wait(lambda: "y" in FakePty.made[0].written)
    assert _wait(lambda: "hello from" in webterm._ACTIVE[SID].tail())

    webterm.shutdown_all()                       # Host 업데이트 재기동
    assert SID not in webterm._ACTIVE
    assert [s["key"] for s in ptyclient.sessions()] == [SID], "Host 가 내려가도 터미널은 산다"
    assert FakePty.made[0].alive and gone == [], "종료로 처리하지 않는다"

    monkeypatch.setattr(webterm, "_SHUTTING_DOWN", False)   # 새 Host
    assert webterm.adopt_broker_sessions() == 1
    assert webterm.has_terminal(SID)
    assert _wait(lambda: "hello from" in webterm._ACTIVE[SID].tail()), "다시 붙으면 꼬리가 버퍼에"
    assert webterm.start_terminal(SID)["status"] == "already_live", "다시 띄우지 않는다"

    assert webterm.stop_terminal(SID)
    assert _wait(lambda: not ptyclient.sessions()) and not FakePty.made[0].alive
    assert _wait(lambda: SID in gone), "종료되면 '작업중' 을 접는다"


def test_webterm_adopts_unknown_broker_session_instead_of_double_spawn(broker, fake_claude_home):
    _mk_session(fake_claude_home)
    ptyclient.spawn(SID, SID, ["x"], "C:/", 80, 24, True, 12)   # Host 가 모르는 브로커 세션
    r = webterm.start_terminal(SID)
    assert r["status"] == "started" and len(FakePty.made) == 1, "프로세스는 1개 그대로"


def test_webterm_refuses_when_broker_down(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_PTY_BROKER", "on")
    monkeypatch.setattr(ptyclient, "launch", lambda: None)
    monkeypatch.setattr(ptyclient, "_LAUNCH_WAIT_S", 0.3)
    monkeypatch.setattr(ptyclient, "_ctl", None)
    monkeypatch.setattr(agents, "snapshot", lambda max_age=0: [])
    _mk_session(fake_claude_home)
    webterm._ACTIVE.clear()
    with pytest.raises(webterm.TermStartError) as e:
        webterm.start_terminal(SID)
    assert e.value.code == "broker_down", "D3: 내장 PTY 로 몰래 폴백하지 않는다"


def test_broker_off_uses_in_process_path(monkeypatch):
    monkeypatch.setenv("SM_PTY_BROKER", "off")
    assert ptyclient.enabled() is False


# ---- 주변: 출처·원격 게이트·롤백·API ----

def test_origin_of_broker_child_is_clewpath(monkeypatch):
    monkeypatch.setattr(peers, "_proc_info", lambda pid: {"cmdline": ["claude", "--resume", "x"], "parent_name": "python.exe",
                                                          "parent_pid": 777, "create_time": 1.0})
    monkeypatch.setattr(ptyclient, "_last_hello", {"pid": 777})
    peers._ORIGIN_CACHE.clear()
    assert peers.origin_of(4242)["origin"] == "clewpath"
    monkeypatch.setattr(ptyclient, "_last_hello", {"pid": 778})
    peers._ORIGIN_CACHE.clear()
    assert peers.origin_of(4242)["origin"] == "script"


def test_remote_restart_is_privileged_and_rollback_spares_broker():
    from session_manager import connector
    assert connector._is_privileged_api("/api/owner/ptyd/restart")
    assert not connector._is_privileged_api("/api/owner/ptyd")
    ps = (Path(ptyd.__file__).parent / "update_runner.ps1").read_text(encoding="utf-8-sig")
    assert "session_manager.ptyd" in ps and "$keep -notcontains" in ps


def test_status_and_restart_api(broker, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    monkeypatch.setattr(webterm, "adopt_broker_sessions", lambda: 0)
    ptyclient.spawn("k1", SID, ["x"], "C:/", 80, 24, True, 12)
    with TestClient(S.create_app()) as c:
        st = c.get("/api/owner/ptyd").json()
        assert st["running"] and st["sessions"] == 1 and st["update_pending"] is False
        r = c.post("/api/owner/ptyd/restart", json={"force": False})
        assert r.status_code == 409 and r.json()["error"] == "has_sessions", "세션이 있으면 확인(force) 없이 안 바꾼다"


def test_launch_uses_hidden_console_not_detached(fake_claude_home, monkeypatch):
    """DETACHED_PROCESS 면 venv 런처 아래 진짜 python 이 새 콘솔 창을 띄우고, 그 창을 닫으면 브로커·터미널이 다 죽는다(2026-10-10)."""
    import subprocess
    seen = {}

    class P:
        def __init__(self, args, **kw):
            seen.update(kw, args=args)
    monkeypatch.setattr(subprocess, "Popen", P)
    monkeypatch.setattr(ptyclient.sys, "platform", "win32")
    ptyclient.launch()
    f = seen["creationflags"]
    assert not f & 0x00000008, "DETACHED_PROCESS 금지(콘솔 창이 새로 뜸)"
    assert f & 0x08000000 and f & 0x00000200, "CREATE_NO_WINDOW + NEW_PROCESS_GROUP"
    assert seen["args"][1:] == ["-m", "session_manager.ptyd"]
