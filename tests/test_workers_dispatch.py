"""v0.9.0 워커 분배 — 피어 레지스트리·멱등 start·SendMessage 호출선/실패 감지·스킬 설치 가드."""
from __future__ import annotations

import json

import pytest

from session_manager import monitor, monwatch, mongroups, peers, skillinstall, webterm
from tests.conftest import write_session

M = "aaaaaaaa-1111-2222-3333-444444444444"
W = "bbbbbbbb-1111-2222-3333-444444444444"
PIPE = r"\\.\pipe\LOCAL\cc-msg-abc"


def _peer(home, pid, sid, name, status="idle", sock=PIPE):
    d = home / "sessions"; d.mkdir(exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": sid, "name": name, "status": status, "kind": "interactive",
        "messagingSocketPath": sock, "cwd": "C:/x"}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_cache():
    peers._cache["at"] = 0.0
    yield
    peers._cache["at"] = 0.0


# ---------------------------------------------------------------- peers

def test_peer_map_and_resolve(fake_claude_home):
    _peer(fake_claude_home, 111, W, "worker-f5")
    _peer(fake_claude_home, 222, M, "관리 [dup]", status="busy")
    (fake_claude_home / "sessions" / "333.json").write_text("{broken", encoding="utf-8")
    pm = peers.peer_map()
    assert pm[W]["name"] == "worker-f5" and pm[M]["status"] == "busy" and len(pm) == 2
    assert peers.resolve("worker-f5") == W
    assert peers.resolve("worker-f5 [f0c2da]") == W            # ListAgents 꼬리표 무시
    assert peers.resolve("uds:" + PIPE) == W                   # 답장 주소(파이프)
    assert peers.resolve("uds:\\\\.\\pipe\\LOCAL\\cc-msg-abc") == W   # 이스케이프 변형
    assert peers.resolve("nobody") is None and peers.resolve("") is None


def test_peer_dir_missing_is_empty(fake_claude_home):
    assert peers.peer_map() == {}


# ---------------------------------------------------------------- start_terminal

class _FakeProc:
    def __init__(self, pid): self.pid = pid; self.alive = True
    def isalive(self): return self.alive
    def terminate(self, force=False): self.alive = False
    def read(self): raise EOFError


def test_start_terminal_idempotent(monkeypatch):
    webterm._ACTIVE.clear()
    spawned = []
    def fake_spawn(sid, skip, fork):
        p = _FakeProc(1000 + len(spawned)); spawned.append(sid)
        s = webterm._TermSession(sid, p, persist=True); webterm._ACTIVE[sid] = s; return s
    monkeypatch.setattr(webterm, "_spawn", fake_spawn)
    r1 = webterm.start_terminal(W)
    r2 = webterm.start_terminal(W)
    assert r1 == {"status": "started", "pid": 1000}
    assert r2 == {"status": "already_live", "pid": 1000} and spawned == [W]   # 프로세스 1개
    webterm._ACTIVE[W].proc.alive = False                                     # 죽으면 다시 띄운다
    assert webterm.start_terminal(W)["status"] == "started" and len(spawned) == 2
    webterm._ACTIVE.clear()


def test_spawn_cap_and_no_cwd(monkeypatch, fake_claude_home):
    webterm._ACTIVE.clear()
    monkeypatch.setenv("SM_MAX_TERMINALS", "1")
    webterm._ACTIVE["other"] = webterm._TermSession("other", _FakeProc(1), persist=True)
    with pytest.raises(webterm.TermStartError) as e:
        webterm._spawn(W, True, None)
    assert e.value.code == "cap" and "상한" in e.value.tty_text
    webterm._ACTIVE.clear(); monkeypatch.delenv("SM_MAX_TERMINALS")
    with pytest.raises(webterm.TermStartError) as e:
        webterm._spawn(W, True, None)             # 세션 파일 없음 → 작업 폴더 미상
    assert e.value.code == "no_cwd"


def test_start_endpoint_local_only_and_errors(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as _srv
    from session_manager.server import create_app
    calls = []
    monkeypatch.setattr(webterm, "start_terminal", lambda sid, skip_permissions=True: calls.append((sid, skip_permissions)) or {"status": "started", "pid": 7})
    with TestClient(create_app()) as c:
        assert c.post(f"/api/sessions/{W}/terminal/start").status_code == 403   # TestClient 호스트는 비루프백 → 원격 취급
        monkeypatch.setattr(_srv, "_is_local", lambda r: True)
        r = c.post(f"/api/sessions/{W}/terminal/start", json={"skip": False})
        assert r.status_code == 200 and r.json()["status"] == "started" and calls == [(W, False)]
        r = c.post(f"/api/sessions/{W}/terminal/start")                     # body 없음 → skip 기본 True
        assert r.status_code == 200 and calls[-1] == (W, True)
        def boom(sid, skip_permissions=True):
            raise webterm.TermStartError("cap", "상한", "x")
        monkeypatch.setattr(webterm, "start_terminal", boom)
        r = c.post(f"/api/sessions/{W}/terminal/start")
        assert r.status_code == 409 and r.json()["error"] == "cap"


# ---------------------------------------------------------------- monitor: SendMessage 호출선·수신

def _use_sendmsg(tid, to, msg="일감 1"):
    return {"type": "assistant", "timestamp": "2026-09-23T01:00:00Z", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "SendMessage", "input": {"to": to, "summary": "assign", "message": msg}}]}}


def _res(tid, text, err=False):
    return {"type": "user", "timestamp": "2026-09-23T01:00:01Z", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": text}]}}


def _recv(text):
    return {"type": "user", "timestamp": "2026-09-23T01:00:02Z", "message": {"role": "user", "content": text}}


def test_monitor_sendmessage_callline_and_receive(fake_claude_home):
    _peer(fake_claude_home, 111, W, "worker-f5")
    _peer(fake_claude_home, 222, M, "manager", sock=r"\\.\pipe\LOCAL\cc-msg-mgr")
    write_session(fake_claude_home, "F--m", M, [_use_sendmsg("t1", "worker-f5"), _use_sendmsg("t2", "stranger")])
    write_session(fake_claude_home, "F--w", W, [_recv(
        'Another Claude session sent a message:\n<cross-session-message from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-mgr" '
        'from-name="manager" from-mode="bypass">\n일감 1: 테스트\n</cross-session-message>')])
    g = monitor.build_group([{"session_id": M, "role": "manager"}, {"session_id": W}])
    evs = g.prime()
    calls = [e for e in evs if e.get("calls_out")]
    assert len(calls) == 1 and calls[0]["calls_out"][0] == {
        "target_session_id": W, "prompt": "assign", "tool_use_id": "t1", "via": "message"}   # 그룹 밖 stranger 는 생략
    rx = next(e for e in evs if e.get("from_peer"))
    assert rx["from_peer"] == "manager" and rx["from_peer_session_id"] == M and rx["text"] == "일감 1: 테스트"


def test_monwatch_sendmessage_failure_push(fake_claude_home):
    _peer(fake_claude_home, 111, W, "worker-f5")
    path = write_session(fake_claude_home, "F--m", M, [])
    mongroups.save("포털", M, [W], labels={W: "포털개발"})
    got = []
    w = monwatch.Watcher(notify=lambda g, target, text: got.append((target, text)) or 1)
    w.poll()
    with open(path, "a", encoding="utf-8") as f:
        for o in (_use_sendmsg("t1", "worker-f5"), _res("t1", '{"success":true,"message":"queued"}'),
                  _use_sendmsg("t2", "worker-f5"), _res("t2", '{"success":false,"message":"No agent named \'worker-f5\' is reachable."}'),
                  _use_sendmsg("t3", "stranger"), _res("t3", '{"success":false,"message":"x"}')):
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    assert w.poll() == 1 and got[0][0] == W and "not reachable" not in got[0][1] and "reachable" in got[0][1]


# ---------------------------------------------------------------- 스킬 설치 가드

def test_skill_install_only_on_explicit_call(fake_claude_home):
    dst = skillinstall.target_path()
    assert skillinstall.bundled_path().is_file()
    assert not dst.exists() and skillinstall.status()["installed"] is False
    r = skillinstall.install()
    assert r["installed"] and dst.is_file() and "clewpath-workers" in dst.read_text(encoding="utf-8")
    assert skillinstall.status()["up_to_date"] is True
    dst.write_text("user edited", encoding="utf-8")
    assert skillinstall.install()["exists"] is True and dst.read_text(encoding="utf-8") == "user edited"   # 무단 덮어쓰기 없음
    assert skillinstall.install(overwrite=True)["installed"] is True


def test_no_skill_install_on_startup(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as _srv
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        assert c.get("/api/owner/skills/workers").json()["installed"] is False
        assert not skillinstall.target_path().exists()          # 기동·상태 조회는 파일을 만들지 않는다
        assert c.post("/api/owner/skills/workers/install").status_code == 403   # 원격에서는 설치 불가
        monkeypatch.setattr(_srv, "_is_local", lambda r: True)
        r = c.post("/api/owner/skills/workers/install")
        assert r.status_code == 200 and skillinstall.target_path().is_file()
        assert c.post("/api/owner/skills/workers/install").status_code == 409
