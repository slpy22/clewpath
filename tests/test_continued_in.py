"""이어받기(continued-in) 대응(0.9.9) — 타 PC 실사고 분석(2026-09-29):
옛 줄이 정상 세션으로 나열되고, 옛 id 로 재개돼 대화가 갈라지고, 살아 있는 세션이 삭제돼 기록이 둘로 갈라짐.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from session_manager import scanner, lifecycle, mongroups, monwatch, webterm, webapi
from tests.conftest import write_session

A = "aaaaaaaa-1111-2222-3333-444444444444"
B = "bbbbbbbb-1111-2222-3333-444444444444"
C = "cccccccc-1111-2222-3333-444444444444"
MISSING = "dddddddd-1111-2222-3333-444444444444"


def _lines(cwd="F:/p", extra=()):
    base = [{"type": "user", "cwd": cwd, "slug": "s", "message": {"role": "user", "content": "hi"},
             "timestamp": "2026-09-29T00:00:00Z"}]
    return base + list(extra)


def _cont(old, new):
    return {"type": "continued-in", "timestamp": "2026-09-29T01:00:00Z", "sessionId": old, "continuedInSessionId": new}


# ---- ① 스캐너 ----

def test_scanner_reads_continued_in(fake_claude_home):
    write_session(fake_claude_home, "F--p", A, _lines(extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines())
    a = scanner.scan_one(A); b = scanner.scan_one(B)
    assert a.continued_in == B and b.continued_in is None
    assert a.to_dict()["continued_in"] == B


def test_latest_session_id_follows_chain_and_stops_at_missing(fake_claude_home):
    write_session(fake_claude_home, "F--p", A, _lines(extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines(extra=[_cont(B, C)]))
    write_session(fake_claude_home, "F--p", C, _lines(extra=[_cont(C, MISSING)]))
    assert scanner.latest_session_id(A) == C           # A→B→C, C→(없음) 에서 멈춤
    assert scanner.latest_session_id(B) == C
    assert scanner.latest_session_id(C) == C
    assert scanner.latest_session_id(MISSING) == MISSING


def test_latest_session_id_cycle_guard(fake_claude_home):
    write_session(fake_claude_home, "F--p", A, _lines(extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines(extra=[_cont(B, A)]))
    assert scanner.latest_session_id(A) in (A, B)      # 무한 루프 없이 끝난다


def test_list_api_exposes_continued_in(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    write_session(fake_claude_home, "F--p", A, _lines(extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines())
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")   # TestClient 호스트는 'testclient' → 로컬로 간주
    with TestClient(S.create_app()) as c:
        rows = {s["session_id"]: s for s in c.get("/api/v1/sessions?limit=0").json()["sessions"]}
        assert rows[A]["continued_in"] == B and rows[B]["continued_in"] is None


# ---- ③ 살아 있는 세션 삭제 거절 ----

def test_delete_refuses_live_session(fake_claude_home, monkeypatch):
    write_session(fake_claude_home, "F--p", A, _lines())
    monkeypatch.setattr("session_manager.native_title.session_status", lambda sid: "idle")
    r = lifecycle.delete_session(A, dry_run=False)
    assert r["error"] == "session_live" and r["reason"] == "idle" and r["deleted"] == []
    assert scanner.scan_one(A) is not None                # 파일은 그대로
    monkeypatch.setattr("session_manager.native_title.session_status", lambda sid: None)
    monkeypatch.setattr("session_manager.webterm.has_terminal", lambda sid: True)
    assert lifecycle.delete_session(A, dry_run=False)["reason"] == "terminal"
    monkeypatch.setattr("session_manager.webterm.has_terminal", lambda sid: False)
    assert lifecycle.delete_session(A, dry_run=True)["dry_run"] is True     # dry_run 은 검사 안 함
    r = lifecycle.delete_session(A, dry_run=False)
    assert r.get("recoverable") is True and scanner.scan_one(A) is None


def test_delete_endpoint_409_when_live(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    write_session(fake_claude_home, "F--p", A, _lines())
    monkeypatch.setattr("session_manager.native_title.session_status", lambda sid: "busy")
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        r = c.post(f"/api/sessions/{A}/delete", json={"dry_run": False})
        assert r.status_code == 409 and r.json()["error"] == "session_live"
        assert c.post(f"/api/sessions/{A}/delete", json={"dry_run": True}).status_code == 200


# ---- ② 옛 줄 재개 금지 ----

def test_spawn_refuses_continued_session(fake_claude_home, monkeypatch, tmp_path):
    write_session(fake_claude_home, "F--p", A, _lines(cwd=str(tmp_path), extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines(cwd=str(tmp_path)))
    webterm._ACTIVE.clear()
    with pytest.raises(webterm.TermStartError) as ei:
        webterm._spawn(A)
    assert ei.value.code == "continued" and B[:8] in str(ei.value)
    # 포크는 원본 무접촉이라 통과해야 한다 → 스폰 직전(PtyProcess)까지 간다
    monkeypatch.setattr(webterm, "_claude_argv", lambda *a, **k: ["nope"])
    import winpty
    def _boom(*a, **k): raise RuntimeError("spawn reached")
    monkeypatch.setattr(winpty.PtyProcess, "spawn", _boom)
    with pytest.raises(webterm.TermStartError) as ei:
        webterm._spawn(A, fork_id="ffffffff-1111-2222-3333-444444444444")
    assert ei.value.code == "spawn_failed"


@pytest.mark.asyncio
async def test_web_resume_refuses_continued_session(fake_claude_home, tmp_path):
    write_session(fake_claude_home, "F--p", A, _lines(cwd=str(tmp_path), extra=[_cont(A, B)]))
    write_session(fake_claude_home, "F--p", B, _lines(cwd=str(tmp_path)))
    sent, closed = [], []

    class _Ws:
        async def send_text(self, s): sent.append(json.loads(s))
        async def close(self): closed.append(True)
    await webapi.run_resume_api(_Ws(), A)
    assert sent and sent[0]["type"] == "error" and sent[0]["error"] == "continued" and sent[0]["continued_in"] == B
    assert closed


# ---- ④ 관제 그룹 manager 자동 갱신 ----

def _use(tid, cmd):
    return {"type": "assistant", "timestamp": "2026-09-29T00:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}}


def _res(tid, text, err):
    return {"type": "user", "timestamp": "2026-09-29T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": text}]}}


def test_monwatch_follows_manager_continuation(fake_claude_home):
    S1 = "ffffffff-1111-2222-3333-444444444444"
    old = write_session(fake_claude_home, "F--p", A, [])
    g = mongroups.save("팀", A, [S1], labels={S1: "워커"})
    got = []
    w = monwatch.Watcher(notify=lambda grp, target, text: got.append((grp["manager"], target)) or 1)
    assert w.poll() == 0
    with open(old, "a", encoding="utf-8") as f:
        f.write(json.dumps(_cont(A, B)) + "\n")
    new = write_session(fake_claude_home, "F--p", B, [])
    assert w.poll() == 0
    assert mongroups.get(g["id"])["manager"] == B, "그룹 manager 가 새 세션으로 갱신"
    assert w.tails[g["id"]].manager == B
    with open(new, "a", encoding="utf-8") as f:
        f.write(json.dumps(_use("t1", f'claude -p --resume {S1} "x"')) + "\n")
        f.write(json.dumps(_res("t1", "Error: boom", True)) + "\n")
    assert w.poll() == 1 and got == [(B, S1)], "새 줄의 호출 실패를 잡는다"


# ---- 포크 정리 vs 삭제 보호(0.9.10): 방금 끝낸 프로세스는 레지스트리가 늦게 사라진다 ----

def test_delete_waits_briefly_for_registry_to_clear(fake_claude_home, monkeypatch):
    write_session(fake_claude_home, "F--p", A, _lines())
    seq = iter(["idle", "idle", None])                      # 두 번은 살아 있다가 사라진다
    monkeypatch.setattr("session_manager.native_title.session_status", lambda sid: next(seq, None))
    monkeypatch.setattr("session_manager.webterm.has_terminal", lambda sid: False)
    r = lifecycle.delete_session(A, dry_run=False, wait_live_s=2)
    assert r.get("recoverable") is True                     # 기다린 뒤 삭제됨
    write_session(fake_claude_home, "F--p", B, _lines())
    monkeypatch.setattr("session_manager.native_title.session_status", lambda sid: "busy")
    r = lifecycle.delete_session(B, dry_run=False, wait_live_s=0.6)
    assert r["error"] == "session_live"                     # 끝내 살아 있으면 보류(데몬 승격 포크 등)


def test_delete_endpoint_accepts_wait_live_s_capped(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    write_session(fake_claude_home, "F--p", A, _lines())
    seen = {}
    real = lifecycle.delete_session
    def spy(sid, dry_run=True, force=False, wait_live_s=0.0):
        seen["wait"] = wait_live_s; return real(sid, dry_run=dry_run, force=force, wait_live_s=0)
    monkeypatch.setattr("session_manager.lifecycle.delete_session", spy)
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        assert c.post(f"/api/sessions/{A}/delete", json={"dry_run": True, "wait_live_s": 99}).status_code == 200
        assert seen["wait"] == 5.0                          # 상한 5초
