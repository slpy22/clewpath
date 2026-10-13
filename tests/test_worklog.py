"""모든 세션 작업 장부(비서 확장, 2026-10-13): 턴 접기·이어 읽기·다시 쓰인 파일·자동 실행 제외·지워진 뒤 보존·검색·
원본 무접촉 · 승인 종류 session_send(꺼져 있으면 띄우고 주소, 백그라운드 점유면 거절)."""
import hashlib
import json
import time

import pytest

from session_manager import worklog
from tests.conftest import write_session

T0 = "2026-10-13T01:00:00.000Z"


def U(text, ts=T0, **kw):
    return {"type": "user", "cwd": "E:/w", "timestamp": ts, "entrypoint": "cli", "message": {"role": "user", "content": text}, **kw}


def A(text, ts=T0, model="claude-opus-5-5"):
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "model": model,
                                                             "content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": text}]}}


def TOOL(ts=T0):
    return {"type": "user", "timestamp": ts, "toolUseResult": {}, "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}


def _sha(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


def test_turns_incremental_rewrite_headless_and_gone(fake_claude_home):
    from session_manager import scanner
    p = write_session(fake_claude_home, "E--w", "s1", [
        {"type": "permission-mode"}, U("로그인 화면 고쳐 줘"), A("보겠습니다"), TOOL(), A("고쳤습니다. 버튼 색만 바꿨어요"),
        U("<command-name>/model</command-name>"),                      # 명령은 요청 아님
        U("비밀 sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA 넣어 둬"),
        A("No response requested.", model="<synthetic>")])
    write_session(fake_claude_home, "E--w", "h1", [U("translate", entrypoint="sdk-cli")])   # 자동 실행 기록
    before = _sha(p)
    r = worklog.collect()
    assert r["sessions"] == 1 and r["turns"] == 2
    t = worklog.session_turns("s1")["turns"]
    assert [x["request"] for x in t][0] == "로그인 화면 고쳐 줘"
    assert t[0]["result"] == "고쳤습니다. 버튼 색만 바꿨어요", "턴의 마지막 글 답(도구 결과·생각 제외)"
    assert "sk-ant" not in t[1]["request"] and t[1]["result"] is None, "비밀값 가림 · 시스템 생성 답은 답 아님"
    assert worklog.session_turns("h1")["found"] is False, "자동 실행 기록은 장부에 안 넣는다"
    # 이어 쓰기 → 새 줄만
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(A("마지막 답 덧붙임", "2026-10-13T02:00:00.000Z")) + "\n")
        f.write(json.dumps(U("배포해 줘", "2026-10-13T03:00:00.000Z")) + "\n")
    scanner.invalidate(str(p))
    worklog.collect()
    t = worklog.session_turns("s1")["turns"]
    assert len(t) == 3 and t[1]["result"] == "마지막 답 덧붙임" and t[2]["request"] == "배포해 줘"
    # 앞부분이 다시 쓰이면 처음부터
    p.write_text(json.dumps(U("새로 쓴 첫 요청")) + "\n", encoding="utf-8")
    scanner.invalidate(str(p))
    worklog.collect()
    assert [x["request"] for x in worklog.session_turns("s1")["turns"]] == ["새로 쓴 첫 요청"]
    # 검색
    assert worklog.search("첫 요청")["items"][0]["session_id"] == "s1"
    assert worklog.search("100%")["items"] == []
    # claude 가 지워도 장부는 남는다
    b2 = _sha(p)
    assert b2 != before            # (위에서 테스트가 직접 고쳐 쓴 것)
    p.unlink()
    scanner.invalidate(str(p))
    worklog.collect()
    v = worklog.session_turns("s1")
    assert v["found"] and v["gone_at"] and v["turns"][0]["request"] == "새로 쓴 첫 요청"


def test_collect_never_writes_claude_files_and_respects_budget(fake_claude_home):
    big = [U(f"요청 {i}", f"2026-10-13T01:{i // 60:02d}:{i % 60:02d}.000Z") for i in range(300)]
    p = write_session(fake_claude_home, "E--w", "big", big)
    st = p.stat()
    r = worklog.collect(run_budget=4000, per_session=4000)
    assert r["behind"] == 1 and 0 < worklog.session_turns("big")["turns_total"] < 300, "한 번에 예산만큼만"
    for _ in range(40):
        worklog.collect(run_budget=4000, per_session=4000)
    assert worklog.session_turns("big")["turns_total"] == 300
    assert p.stat().st_mtime == st.st_mtime and p.stat().st_size == st.st_size, "claude 파일은 읽기만"


def test_brief_shows_recent_and_waiting(fake_claude_home, monkeypatch):
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    now = time.time()
    write_session(fake_claude_home, "E--w", "new1", [U("오늘 일", iso(now - 3600)), A("했음", iso(now - 3500))])
    write_session(fake_claude_home, "E--w", "old1", [U("지난달 일", iso(now - 20 * 86400))])
    write_session(fake_claude_home, "E--w", "wait1", [U("옛날 질문", iso(now - 10 * 86400)), A("어느 쪽으로 할까요?", iso(now - 10 * 86400))])
    monkeypatch.setattr("session_manager.hooks.status_map", lambda: {"wait1": {"phase": "waiting", "waiting_at": now - 60}})
    monkeypatch.setattr("session_manager.peers.peer_map", lambda: {"wait1": {"name": "w", "socket": r"\\.\pipe\x"}})
    worklog.collect()
    b = worklog.brief(days=2)
    ids = [s["session_id"] for s in b["sessions"]]
    assert ids == ["new1", "wait1"], "최근 요청 세션 + 오래됐어도 지금 사람을 기다리는 세션"
    w = b["sessions"][1]
    assert w["state"]["phase"] == "waiting" and w["state"]["address"] == r"uds:\\.\pipe\x"
    assert b["sessions"][0]["recent"][0] == {"n": 1, "ts": b["sessions"][0]["recent"][0]["ts"], "request": "오늘 일", "result": "했음"}


def test_session_send_approval(fake_claude_home, monkeypatch):
    from session_manager import approvals, team, webterm
    write_session(fake_claude_home, "E--w", "tgt", [U("일")])
    peers = {}
    started = []

    def start(sid, skip_permissions=True):
        started.append((sid, skip_permissions))
        peers[sid] = {"name": "tgt-1", "socket": "PIPE"}
        return {"status": "started"}
    monkeypatch.setattr("session_manager.peers.peer_map", lambda: dict(peers))
    monkeypatch.setattr(webterm, "start_terminal", start)
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: False)
    monkeypatch.setattr("session_manager.agents.occupancy_map", lambda: {})
    monkeypatch.setattr("session_manager.push.send", lambda *a, **k: None)
    a = approvals.create("session_send", {"session_id": "tgt", "message": "테스트 돌려 줘", "title": "대상"}, requested_by="me")
    assert a["summary"].startswith("[대상] 세션에 전달: 테스트 돌려 줘")
    with pytest.raises(team.TeamError):
        approvals.execute(a["id"])                                   # 승인 전엔 실행 불가
    approvals.decide(a["id"], True, "otp")
    x = approvals.execute(a["id"])["result"]
    assert x["started"] and x["send_to"] == {"address": "uds:PIPE", "live": True, "name": "tgt-1"}
    assert started == [("tgt", False)], "평소 권한 모드로 띄운다(자동 허용 아님)"
    # 다른 곳에서 백그라운드로 점유 중이면 띄우지 않는다
    peers.clear()
    monkeypatch.setattr("session_manager.agents.occupancy_map", lambda: {"tgt": {"kind": "bg"}})
    b = approvals.create("session_send", {"session_id": "tgt", "message": "또"}, requested_by="me")
    approvals.decide(b["id"], True, "otp")
    with pytest.raises(team.TeamError) as e:
        approvals.execute(b["id"])
    assert e.value.code == "busy_elsewhere" and len(started) == 1
    with pytest.raises(team.TeamError):
        approvals.create("session_send", {"session_id": "tgt"})     # 보낼 글 없음


def test_worklog_api_local_only(fake_claude_home, monkeypatch):
    from session_manager import team
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    write_session(fake_claude_home, "E--w", "s9", [U("검색될 요청")])
    worklog.collect()
    from session_manager import server as S
    with TestClient(S.create_app()) as c:
        assert c.get("/api/v1/worklog/search", params={"q": "x"}).status_code in (401, 403, 503), "이 PC 안에서만"
        monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
        assert c.get("/api/v1/worklog/search", params={"q": "검색될"}).json()["items"][0]["session_id"] == "s9"
        assert c.get("/api/v1/worklog/sessions/s9").json()["turns_total"] == 1
        assert "sessions" in c.get("/api/v1/worklog/brief").json()
