"""이어하기 되돌리기 + '작업중' 잔상 정리(2026-10-06 실사고).

① 빈 입력칸 ← 로 생긴 백그라운드 사본(92a84e39)이 팀 명부를 가로채고 원래 세션에 '이어받음' 표시를 남김
② 관리 세션이 RESULT 직후 워커 터미널을 내려 Stop 훅이 오지 않아 몇 시간째 '작업중'
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from session_manager import agents, connector, hooks, lifecycle, peers, revert, scanner, team, webterm
from tests.conftest import write_session

A = "aaaaaaaa-1111-2222-3333-444444444444"
B = "bbbbbbbb-1111-2222-3333-444444444444"
C = "cccccccc-1111-2222-3333-444444444444"
U1 = "11111111-0000-0000-0000-000000000001"
U2 = "11111111-0000-0000-0000-000000000002"
U3 = "11111111-0000-0000-0000-000000000003"


def _msg(uuid, role, text, extra=None):
    o = {"type": role, "uuid": uuid, "cwd": "F:/p", "slug": "s", "timestamp": "2026-10-06T01:00:00Z",
         "message": {"role": role, "content": text}}
    o.update(extra or {})
    return o


def _cont(old, new):
    return {"type": "continued-in", "timestamp": "2026-10-06T01:45:39Z", "sessionId": old, "continuedInSessionId": new}


def _bg_pair(home, b_extra=()):
    """A = 원래 대화 + continued-in, B = A 의 복사본(sessionKind bg) + b_extra."""
    base = [_msg(U1, "user", "논문 일감"), _msg(U2, "assistant", "끝냈습니다")]
    write_session(home, "F--p", A, base + [_cont(A, B)])
    bsys = {"type": "system", "subtype": "bridge_status", "uuid": "22222222-0000-0000-0000-000000000009",
            "sessionKind": "bg", "timestamp": "2026-10-06T01:45:43Z"}
    write_session(home, "F--p", B, base + [bsys] + list(b_extra))


@pytest.fixture
def quiet(monkeypatch):
    """살아 있는 프로세스 없음(claude CLI·레지스트리·PTY 호출 차단)."""
    monkeypatch.setattr(agents, "snapshot", lambda max_age=0: [])
    monkeypatch.setattr(peers, "peer_map", lambda: {})
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: False)
    monkeypatch.setattr(hooks, "_persist_status", lambda: None)


# ---- '작업중' 잔상 ----

def test_mark_gone_folds_only_active_phase(monkeypatch):
    monkeypatch.setattr(hooks, "_persist_status", lambda: None)
    monkeypatch.setattr(hooks, "_persist_loaded", True)
    hooks._STATUS.clear()
    hooks._STATUS["x"] = {"phase": "thinking", "thinking_at": 1}
    hooks._STATUS["y"] = {"phase": "ready", "ready_at": 1}
    assert hooks.mark_gone("x") is True and hooks._STATUS["x"]["phase"] == "ended"
    assert "도중" in hooks._STATUS["x"]["note"]
    assert hooks.mark_gone("y") is False and hooks._STATUS["y"]["phase"] == "ready", "정상 종료 기록은 보존"
    assert hooks.mark_gone("none") is False


def test_effective_marks_stale_thinking_without_process():
    st = {"phase": "thinking", "thinking_at": 100}
    out = hooks.effective(st, alive=False, quiet_s=hooks.GONE_QUIET_S + 1)
    assert out["phase"] == "ended" and out["stale_from"] == "thinking"
    assert st["phase"] == "thinking", "원본은 바꾸지 않는다(다음 훅이 바로잡게)"
    assert hooks.effective(st, alive=True, quiet_s=99999) is st, "프로세스가 살아 있으면 그대로"
    assert hooks.effective(st, alive=False, quiet_s=60) is st, "최근에 기록했으면 그대로(긴 도구 실행 보호)"
    assert hooks.effective({"phase": "ready"}, alive=False, quiet_s=99999)["phase"] == "ready"
    assert hooks.effective(None, alive=False, quiet_s=99999) is None


def test_pty_cleanup_marks_gone(monkeypatch):
    seen = []
    monkeypatch.setattr(hooks, "mark_gone", lambda sid, reason="process_exit": seen.append(sid))
    monkeypatch.setattr(webterm, "_registry_remove", lambda pid: None)

    class P:
        pid = 1
        def terminate(self, force=False):
            pass

    s = webterm._TermSession(A, P(), persist=True)
    webterm._cleanup(s)
    assert seen == [A]


def test_kill_session_marks_gone(fake_claude_home, monkeypatch):
    seen = []
    monkeypatch.setattr(hooks, "mark_gone", lambda sid, reason="": seen.append((sid, reason)))
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: True)
    monkeypatch.setattr(webterm, "stop_terminal", lambda sid: True)
    monkeypatch.setattr(peers, "snapshot", lambda max_age=0: [])
    lifecycle.kill_session(A)
    assert seen and seen[0][0] == A


# ---- 되돌리기: 미리보기 ----

def test_plan_background_copy_from_either_side(fake_claude_home, quiet):
    _bg_pair(fake_claude_home)
    for sid in (A, B):
        p = revert.plan(sid)
        assert p["ok"] and p["old"] == A and p["new"] == B
        assert p["kind"] == "background" and p["new_messages"] == 0
        assert p["stop"] is None and any("이어짐" in s for s in p["steps"])


def test_plan_counts_new_conversation_in_copy(fake_claude_home, quiet):
    _bg_pair(fake_claude_home, [_msg(U3, "user", "사본에서 새로 물어본 것")])
    p = revert.plan(A)
    assert p["new_messages"] == 1 and p["new_inputs"] == 1
    assert p["last_inputs"][0]["text"].startswith("사본에서")


def test_plan_refuses_none_chain_and_live_old(fake_claude_home, quiet, monkeypatch):
    write_session(fake_claude_home, "F--p", C, [_msg(U1, "user", "x")])
    assert revert.plan(C)["error"] == "no_continuation"
    _bg_pair(fake_claude_home, [_cont(B, C)])
    assert revert.plan(A)["error"] == "chain"
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: sid == A)
    write_session(fake_claude_home, "F--p", B, [_msg(U1, "user", "x")])
    assert revert.plan(A)["error"] == "old_live"


def test_plan_reports_background_process_to_stop(fake_claude_home, quiet, monkeypatch):
    _bg_pair(fake_claude_home)
    monkeypatch.setattr(agents, "snapshot", lambda max_age=0: [{"sessionId": B, "kind": "background", "pid": 7}])
    p = revert.plan(A)
    assert p["new_process"]["kind"] == "background" and "claude stop" in p["stop"]


# ---- 되돌리기: 실행 ----

def test_execute_trashes_copy_strips_marker_and_backs_up(fake_claude_home, quiet):
    _bg_pair(fake_claude_home)
    pa = Path(scanner.scan_one(A).jsonl_path)
    before = pa.read_bytes()
    r = revert.execute(B)
    assert r["ok"] and r["marker_removed"] == 1
    assert scanner.scan_one(B) is None, "사본은 휴지통으로"
    after = pa.read_bytes()
    assert b"continued-in" not in after and after == b"".join(
        ln for ln in before.splitlines(keepends=True) if b"continued-in" not in ln), "그 한 줄만 지운다"
    assert scanner.scan_one(A).continued_in is None and scanner.latest_session_id(A) == A
    bucket = Path(r["trash"])
    assert (bucket / f"backup_{A}.jsonl").read_bytes() == before, "원래 파일 원본 백업"
    m = json.loads((bucket / "manifest.json").read_text(encoding="utf-8"))
    assert m["session_id"] == B and m["revert"]["old"] == A
    assert revert.plan(A)["error"] == "no_continuation", "다시 누르면 할 일 없음"


def test_execute_keeps_old_untouched_when_copy_cannot_be_deleted(fake_claude_home, quiet, monkeypatch):
    _bg_pair(fake_claude_home)
    pa = Path(scanner.scan_one(A).jsonl_path)
    before = pa.read_bytes()
    monkeypatch.setattr(lifecycle, "delete_session",
                        lambda sid, dry_run=True, wait_live_s=0: {"error": "session_live", "deleted": []})
    r = revert.execute(A)
    assert not r["ok"] and r["error"] == "session_live"
    assert pa.read_bytes() == before, "사본을 못 지우면 원래 파일은 건드리지 않는다"


def test_execute_stops_background_agent_first(fake_claude_home, quiet, monkeypatch):
    _bg_pair(fake_claude_home)
    calls = []
    live = [{"sessionId": B, "kind": "background", "pid": 7}]
    monkeypatch.setattr(agents, "snapshot", lambda max_age=0: list(live))
    monkeypatch.setattr(revert.shutil, "which", lambda n: "claude")

    class R:
        returncode = 0

    def run(args, **k):
        calls.append(args)
        live.clear()
        return R()
    monkeypatch.setattr(revert.subprocess, "run", run)
    r = revert.execute(A)
    assert r["ok"] and calls == [["claude", "stop", B[:8]]]


def test_execute_rolls_team_back_to_original(fake_claude_home, quiet):
    _bg_pair(fake_claude_home)
    t = team.create_team("KP", manager_session="s-mgr")
    m = team.add_member(t["id"], "논문", session_id=A)
    assert team.current_session(m["agent_id"]) == B, "← 사본이 명부를 가로챈 상태(실사고 재현)"
    with team._Tx() as c:   # 사본 수집으로 두 번 잡힌 사용량(실사고 116건)
        c.execute("INSERT INTO usage(session_id, message_id, agent_id) VALUES(?,?,?)", (B, "msg_1", m["agent_id"]))
    r = revert.execute(A)
    assert r["ok"] and m["agent_id"] in r["team"]["agents"]
    assert team.current_session(m["agent_id"]) == A
    c = team._read()
    try:
        assert not c.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (B,)).fetchone()
        assert c.execute("SELECT ended FROM agent_sessions WHERE session_id=?", (A,)).fetchone()["ended"] is None
        assert not c.execute("SELECT 1 FROM usage WHERE session_id=?", (B,)).fetchone()
        ev = c.execute("SELECT payload_json FROM events WHERE kind='session_bound' ORDER BY id DESC LIMIT 1").fetchone()
        assert "되돌리기" in ev["payload_json"]
    finally:
        c.close()


# ---- API·원격 게이트 ----

def test_routes_and_remote_privilege(fake_claude_home, quiet, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    _bg_pair(fake_claude_home)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app()) as c:
        assert c.get(f"/api/sessions/{C}/revert-continuation/preview").status_code == 404
        p = c.get(f"/api/sessions/{A}/revert-continuation/preview").json()
        assert p["ok"] and p["new"] == B
        r = c.post(f"/api/sessions/{B}/revert-continuation")
        assert r.status_code == 200 and r.json()["ok"]
        assert c.post(f"/api/sessions/{A}/revert-continuation").status_code == 404
    assert connector._is_privileged_api(f"/api/sessions/{A}/revert-continuation")
    assert not connector._is_privileged_api(f"/api/sessions/{A}/revert-continuation/preview"), "미리보기는 읽기만"
