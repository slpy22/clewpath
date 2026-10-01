"""팀 세션 수집기(단계 2): 원본 보존·장부 자동 채움·위반 감지·경력 읽기·지우기(docs/designs/workers-p2p.md)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from session_manager import team, teamlog

PIPE_BE = r"\\.\pipe\LOCAL\cc-msg-be00000000000000000000000000000"
PIPE_FE = r"\\.\pipe\LOCAL\cc-msg-fe00000000000000000000000000000"
PIPE_MG = r"\\.\pipe\LOCAL\cc-msg-mg00000000000000000000000000000"


def _ts(sec: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def human(uid, text, t=1000):
    return {"type": "user", "uuid": uid, "timestamp": _ts(t), "message": {"role": "user", "content": text}}


def assistant_usage(mid, out, t=1000, text="ok"):
    return {"type": "assistant", "timestamp": _ts(t), "message": {"id": mid, "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5}}}


def send(tid, to, body, t=1000, mid="m-send"):
    return {"type": "assistant", "timestamp": _ts(t), "message": {"id": mid, "role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "SendMessage", "input": {"to": "uds:" + to, "message": body, "summary": "s"}}]}}


def result(tid, ok=True, t=1001):
    txt = json.dumps({"success": ok, "message": "x"})
    return {"type": "user", "timestamp": _ts(t), "uuid": "r-" + tid,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                    "content": [{"type": "text", "text": txt}]}]}}


def received(frm, name, body, uid, t=1002):
    return human(uid, f'Another Claude session sent a message:\n<cross-session-message from="uds:{frm}" '
                      f'from-name="{name}" from-mode="bypass">{body}</cross-session-message>', t)


def cw(task, thread, typ, turn=1, reply="yes", frm="x"):
    return f"결론: 질문\n[cw] task={task} thread={thread} type={typ} turn={turn}/4 reply={reply} from={frm}\n- 본문"


@pytest.fixture
def env(fake_claude_home, monkeypatch):
    proj = fake_claude_home / "projects" / "F--proj"
    proj.mkdir(parents=True)
    paths = {}

    def write(sid, recs, append=False):
        p = proj / f"{sid}.jsonl"
        with p.open("a" if append else "w", encoding="utf-8", newline="\n") as f:   # claude 처럼 LF
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        paths[sid] = p
        return p

    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    snap = [{"session_id": "s-be", "socket": PIPE_BE}, {"session_id": "s-fe", "socket": PIPE_FE},
            {"session_id": "s-mg", "socket": PIPE_MG}]
    from session_manager import peers
    monkeypatch.setattr(peers, "snapshot", lambda *a, **k: list(snap))
    monkeypatch.setattr(peers, "resolve", lambda ref: None)
    sent = []
    from session_manager import push
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a) or 1)
    t = team.create_team("Web", code="WEB", manager_session="s-mg")
    team.add_member(t["id"], "백엔드", session_id="s-be")
    team.add_member(t["id"], "프론트", session_id="s-fe")
    k = team.create_task(t["id"], "문서", "프론트", collaborators=["백엔드"])
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    c.execute("UPDATE agent_sessions SET started=0")                               # 합성 기록(1970 시각)이 '가입 후' 가 되게
    c.commit(); c.close()
    return {"team": t, "write": write, "paths": paths, "sent": sent, "task": k["id"], "proj": proj}


def _events(kind=None):
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    c.row_factory = sqlite3.Row
    q = "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY id"
    return [dict(r) for r in c.execute(q, (kind,) if kind else ())]


def test_redact_common_secrets():
    s = ("key sk-ant-api03-abcdefghijklmnopqrstuv and ghp_abcdefghijklmnopqrstuvwxyz0123 AKIAABCDEFGHIJKLMNOP "
         "password=hunter2xx token eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefghijkl "
         "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----")
    r = teamlog.redact(s)
    for leak in ("sk-ant-api03-abcdef", "ghp_abcdef", "AKIAABCD", "hunter2xx", "eyJhbGciOiJIUzI1", "MIIabc"):
        assert leak not in r, leak
    assert r.count("[REDACTED]") >= 6 and "password=[REDACTED]" in r
    assert teamlog.redact("평범한 문장 sk 이야기") == "평범한 문장 sk 이야기"


def test_parse_cw():
    assert teamlog.parse_cw(cw("WEB-T1", "T1-a", "ask", 2)) == {
        "task": "WEB-T1", "thread": "T1-a", "type": "ASK", "turn": "2/4", "reply": "yes", "from": "x", "turn_n": 2}
    assert teamlog.parse_cw("헤더 없음") == {}


def test_ingest_classifies_and_is_idempotent(env):
    k = env["task"]
    env["write"]("s-fe", [
        human("h1", "client.md 작성해 줘 password=abcd1234", 900),
        assistant_usage("m1", 50, 901), assistant_usage("m1", 80, 902),            # 같은 message.id — 최댓값 80
        send("tu1", PIPE_BE, cw(k, "T1-a", "ASK"), 903),
        result("tu1", True, 904),
        send("tu2", PIPE_BE, cw(k, "T1-b", "REVIEW"), 905, mid="m2"),
        result("tu2", False, 906),                                                 # 전달 실패
        received(PIPE_BE, "be", cw(k, "T1-a", "ANSWER", 2, "no"), "x1", 907),       # 팀 안 수신 → 이벤트 없음
        received(r"\\.\pipe\LOCAL\cc-msg-outsider", "outsider", "결론: 밖에서 온 메시지", "x2", 908),
        {"type": "user", "uuid": "meta", "isMeta": True, "message": {"content": "<command-name>/clear</command-name>"}},
        human("cmd", "<command-name>/model</command-name>", 909),
    ])
    r = teamlog.ingest_all()
    assert r["sessions"] >= 1 and r["events"] == 5
    kinds = [e["kind"] for e in _events() if e["kind"] in ("human_input", "msg", "msg_failed", "msg_in")]
    assert kinds == ["human_input", "msg", "msg", "msg_failed", "msg_in"]
    h = json.loads(_events("human_input")[0]["payload_json"])
    assert "abcd1234" not in h["text"] and "[REDACTED]" in h["text"], "색인도 비밀값 가림"
    m = json.loads(_events("msg")[0]["payload_json"])
    assert m["cw"]["type"] == "ASK" and m["to_session"] == "s-be" and m["to_agent"], "uds 주소 → 받는 에이전트"
    assert _events("msg")[0]["task_id"] == k and _events("msg")[0]["src_ts"] == 903
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    assert c.execute("SELECT output FROM usage WHERE message_id='m1'").fetchone()[0] == 80
    assert teamlog.ingest_all()["events"] == 0, "다시 돌려도 무변화"
    # 보존본: gzip 을 풀면 가린 원본과 같다
    d = teamlog.archive_root() / "s-fe"
    files = list(d.glob("g1-*.jsonl.gz"))
    assert len(files) == 1
    raw = gzip.open(files[0], "rb").read().decode("utf-8")
    assert raw == teamlog.redact(env["paths"]["s-fe"].read_text(encoding="utf-8"))
    out = teamlog.archive_lines("s-fe", 0, 3)
    assert len(out["lines"]) == 3 and out["next_offset"] and json.loads(out["lines"][0]["line"])["uuid"] == "h1"
    more = teamlog.archive_lines("s-fe", out["next_offset"], 100)
    assert len(more["lines"]) == 8 and more["next_offset"] is None


def test_incomplete_line_waits_and_append_continues(env):
    p = env["write"]("s-be", [human("a", "첫 줄", 1)])
    with p.open("a", encoding="utf-8") as f:
        f.write('{"type":"user","uuid":"b","message":{"content":"반쪽')               # 아직 안 끝난 줄
    teamlog.ingest_all()
    assert len(_events("human_input")) == 1
    with p.open("a", encoding="utf-8") as f:
        f.write(' 완성"}}\n')
    teamlog.ingest_all()
    assert [json.loads(e["payload_json"])["text"] for e in _events("human_input")] == ["첫 줄", "반쪽 완성"]
    assert len(list((teamlog.archive_root() / "s-be").glob("*.gz"))) == 2, "청크는 시작 오프셋마다 하나"


def test_rewritten_file_starts_new_generation(env):
    env["write"]("s-be", [human("a", "원래 첫 줄 내용이 길다", 1), human("b", "둘째", 2)])
    teamlog.ingest_all()
    env["write"]("s-be", [human("z", "새 파일", 3)])                                # 재작성(크기 줄어듦)
    r = teamlog.ingest_session("s-be")
    assert r["rewritten"] is True
    texts = [json.loads(e["payload_json"])["text"] for e in _events("human_input")]
    assert texts == ["원래 첫 줄 내용이 길다", "둘째", "새 파일"]
    assert list((teamlog.archive_root() / "s-be").glob("g2-*.gz")), "새 세대 청크"
    assert teamlog.archive_lines("s-be")["gen"] == 2


def test_deleted_file_marks_gone_and_final_ingest_on_delete(env, monkeypatch):
    p = env["write"]("s-be", [human("a", "삭제 전", 1)])
    teamlog.final_ingest("s-be")
    assert len(_events("human_input")) == 1
    p.unlink()
    r = teamlog.ingest_session("s-be")
    assert r["gone"] is True
    assert "s-be" not in teamlog.sessions_to_ingest(), "사라진 세션은 수집 대상에서 빠짐"
    teamlog.final_ingest("not-a-team-session")                                    # 팀 밖 세션은 무동작


def test_lifecycle_delete_calls_final_ingest(env, monkeypatch):
    called = []
    monkeypatch.setattr(teamlog, "final_ingest", lambda sid: called.append(sid))
    from session_manager import lifecycle
    monkeypatch.setattr(lifecycle, "_live_reason", lambda sid: None, raising=False)
    env["write"]("s-be", [human("a", "x", 1)])
    lifecycle.delete_session("s-be", dry_run=False, force=True)
    assert called == ["s-be"]


def test_context_task_attribution_and_usage_summary(env):
    k = env["task"]
    env["write"]("s-fe", [assistant_usage("u0", 10, 1),                              # 문맥 없음 → 미분류
                          send("tu1", PIPE_BE, cw(k, "T1-a", "ASK"), 2, mid="u1"),
                          assistant_usage("u2", 30, 3)])
    env["write"]("s-be", [received(PIPE_FE, "fe", cw(k, "T1-a", "ASK"), "r1", 2),   # 수신으로 문맥 갱신(C2-5)
                          assistant_usage("b1", 40, 3)])
    teamlog.ingest_all()
    s = teamlog.usage_summary("WEB")
    by = {x["task"]: x["output"] for x in s["by_task_estimate"]}
    assert by[None] == 10 and by[k] == 70
    assert s["total"]["output"] == 80 and {x["alias"] for x in s["by_agent"]} == {"프론트", "백엔드"}


def _send_n(env, pairs, base=1000):
    """[(보내는 세션, 받는 파이프, 본문, ok)] 를 세션별 jsonl 로."""
    per: dict[str, list] = {}
    for i, (sid, to, body, ok) in enumerate(pairs):
        per.setdefault(sid, []).extend([send(f"t{i}", to, body, base + i, mid=f"mm{i}"), result(f"t{i}", ok, base + i)])
    for sid, recs in per.items():
        env["write"](sid, recs)
    teamlog.ingest_all()


def test_violation_turn_overrun_and_failed_not_counted(env):
    k = env["task"]
    pairs = [("s-fe" if i % 2 == 0 else "s-be", PIPE_BE if i % 2 == 0 else PIPE_FE,
              cw(k, "T1-a", "ASK" if i % 2 == 0 else "ANSWER", i + 1), True) for i in range(5)]
    pairs.append(("s-fe", PIPE_BE, cw(k, "T1-z", "ASK"), False))                   # 실패 전송 — 무응답 판정 제외
    _send_n(env, pairs)
    new = teamlog.judge("WEB", now=10 ** 9)
    rules = sorted(json.loads(e["payload_json"])["rule"] for e in _events("violation"))
    assert "turn_overrun" in rules and not any("T1-z" in x for x in new)
    assert teamlog.judge("WEB", now=10 ** 9) == [], "같은 위반은 한 번만"


def test_violation_unanswered_forward_and_deadlock(env):
    k = env["task"]
    _send_n(env, [("s-fe", PIPE_BE, cw(k, "T1-a", "ASK"), True),                    # 프론트→백엔드 ASK
                  ("s-be", PIPE_MG, cw(k, "T1-a", "ASK"), True),                    # 백엔드→관리: 관리는 제외
                  ("s-be", PIPE_FE, cw(k, "T1-b", "ASK"), True)])                   # 서로 무응답 ASK → 순환
    teamlog.judge("WEB", now=1000 + 60)                                             # 아직 30분 안 됨
    assert not [e for e in _events("violation") if json.loads(e["payload_json"])["rule"] == "unanswered_ask"]
    teamlog.judge("WEB", now=1000 + 31 * 60)
    rules = [json.loads(e["payload_json"])["rule"] for e in _events("violation")]
    assert rules.count("unanswered_ask") >= 2 and "deadlock" in rules
    assert "forward_chain" not in rules, "관리 세션에 올리는 건 전달 사슬이 아니다"


def test_violation_forward_chain(env, monkeypatch):
    t = team.get_team("WEB")
    team.add_member(t["id"], "QA", session_id="s-qa")
    from session_manager import peers
    snap = peers.snapshot() + [{"session_id": "s-qa", "socket": r"\\.\pipe\LOCAL\cc-msg-qa"}]
    monkeypatch.setattr(peers, "snapshot", lambda *a, **k: snap)
    k = env["task"]
    _send_n(env, [("s-fe", PIPE_BE, cw(k, "T1-a", "ASK"), True),
                  ("s-be", r"\\.\pipe\LOCAL\cc-msg-qa", cw(k, "T1-a", "ASK"), True)])  # 받은 질문을 제3자에게
    teamlog.judge("WEB", now=1100)
    assert "forward_chain" in [json.loads(e["payload_json"])["rule"] for e in _events("violation")]


def test_violation_notify_once_unique_kind_and_retry(env, monkeypatch):
    k = env["task"]
    _send_n(env, [("s-fe", PIPE_BE, cw(k, "T1-a", "ASK"), True)])
    teamlog.judge("WEB", now=10 ** 9)
    from session_manager import push
    calls = []
    monkeypatch.setattr(push, "send", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    assert teamlog.notify_violations("WEB") == 0
    monkeypatch.setattr(push, "send", lambda *a, **kw: calls.append(a) or 1)
    assert teamlog.notify_violations("WEB") == 1 and calls[0][0].startswith("team-violation:v:noans:")
    assert teamlog.notify_violations("WEB") == 0, "알린 위반은 다시 안 알림"
    v = teamlog.violations("WEB")
    assert v[0]["notified"] and v[0]["who"] == ["프론트", "백엔드"]


def test_read_apis_history_career_timeline_inputs(env):
    k = env["task"]
    env["write"]("s-fe", [human("h1", "토큰 만료 처리 문서를 써 줘", 10), send("tu1", PIPE_BE, cw(k, "T1-a", "ASK"), 11),
                          result("tu1", True, 12), assistant_usage("u1", 33, 13)])
    teamlog.ingest_all()
    team.transition("WEB", k, "submit", assignment_ver=1)
    h = teamlog.member_history("WEB", "프론트")
    assert h["counts"]["messages_sent"] == 1 and h["counts"]["human_inputs"] == 1 and h["tokens"]["output"] == 33
    assert h["tasks_owned"][0]["id"] == k and h["sessions"][0]["session_id"] == "s-fe"
    car = teamlog.agent_career(h["agent_id"])
    assert car["memberships"][0]["code"] == "WEB" and car["tasks_by_status"] == {"submitted": 1}
    tl = teamlog.timeline("WEB", k)
    assert [e["kind"] for e in tl["events"]][:2] == ["assigned", "human_input"] or "msg" in [e["kind"] for e in tl["events"]]
    assert any(e["kind"] == "msg" and e["to"] == "백엔드" for e in tl["events"])
    assert teamlog.inputs("WEB", q="만료 처리")["items"][0]["text"].startswith("토큰"), "trigram 전문 검색"
    assert teamlog.inputs("WEB", q="토큰")["items"], "짧은 검색어는 LIKE"
    assert teamlog.inputs("WEB", q="없는말입니다")["items"] == []
    team.leave("WEB", "프론트")
    assert teamlog.member_history("WEB", "프론트")["alias"] == "프론트", "탈퇴자 경력도 조회"


def test_purge_removes_archive_and_text_keeps_structure_and_tokens(env):
    k = env["task"]
    env["write"]("s-fe", [human("h1", "비밀 계획", 1), assistant_usage("u1", 9, 2)])
    teamlog.ingest_all()
    r = teamlog.purge_team("WEB")
    assert r["purged_sessions"] == 1 and r["purged_events"] == 1
    assert not (teamlog.archive_root() / "s-fe").exists() and _events("human_input") == []
    assert teamlog.usage_summary("WEB")["total"]["output"] == 9, "토큰 수는 내용이 아니라 남김"
    assert team.task_detail("WEB", k)["events"][0]["kind"] == "assigned"
    assert teamlog.inputs("WEB", q="비밀 계획")["items"] == []


def test_schema_migration_from_v1(fake_claude_home):
    import sqlite3
    p = team.db_path(); p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.executescript(team._SCHEMA); c.execute("PRAGMA user_version=1")
    c.execute("INSERT INTO teams(id,code,name,created) VALUES('tm_1','OLD','old',1)")
    c.execute("INSERT INTO agents(id,alias,created) VALUES('ag_1','a',1)")
    c.execute("INSERT INTO memberships(team_id,agent_id,alias,member_role,joined) VALUES('tm_1','ag_1','a','worker',1)")
    c.execute("INSERT INTO agent_sessions(agent_id,session_id,started,reason) VALUES('ag_1','s-old',1,'created')")
    c.commit(); c.close()
    assert team.list_teams()[0]["code"] == "OLD"
    c = sqlite3.connect(str(p))
    assert c.execute("PRAGMA user_version").fetchone()[0] == team.SCHEMA_VERSION
    assert c.execute("SELECT team_id FROM agent_sessions").fetchone()[0] == "tm_1", "옛 바인딩에 당시 소속 채움"


def test_continuation_middle_sessions_bound(fake_claude_home, monkeypatch):
    chain = {"s1": "s2", "s2": "s3"}
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: chain.get(sid, sid))
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    t = team.create_team("C")
    a = team.add_member(t["id"], "w", session_id="s1")
    assert team.current_session(a["agent_id"]) == "s3"
    h = teamlog.member_history(t["id"], "w")
    assert [s["session_id"] for s in h["sessions"]] == ["s1", "s2", "s3"], "중간 세션도 이력에(C2-3)"


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    with TestClient(S.create_app()) as c:
        yield c


def test_api_stage2(client, env):
    env["write"]("s-fe", [human("h1", "검색될 사람 입력", 1)])
    teamlog.ingest_all()
    assert client.get("/api/v1/team/WEB/inputs", params={"q": "사람 입력"}).json()["items"][0]["agent"] == "프론트"
    assert client.get("/api/v1/team/WEB/members/프론트/history").status_code == 200
    assert client.get(f"/api/v1/team/WEB/tasks/{env['task']}/timeline").json()["id"] == env["task"]
    assert "total" in client.get("/api/v1/team/WEB/usage").json()
    assert client.get("/api/v1/team/WEB/violations").json() == {"violations": []}
    assert client.get("/api/v1/team/archive/s-fe").json()["lines"]
    assert client.get("/api/v1/team/archive/nope").status_code == 404
    aid = client.get("/api/v1/team/WEB/members/프론트/history").json()["agent_id"]
    assert client.get(f"/api/v1/team/agent/{aid}").json()["alias"] == "프론트"
    r = client.post("/api/v1/team/WEB/purge", json={"confirm": "WEB"})
    assert r.status_code == 403 and r.json()["error"] == "approval_required", "화면 확인만으로는 못 지움(단계 3)"
    assert client.get("/api/v1/team/WEB/inputs").json()["items"], "지워지지 않았다"


def test_new_worker_session_created_just_before_binding_is_taken_whole(env):
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    c.execute("UPDATE agent_sessions SET started=? WHERE session_id='s-be'", (5000,))
    c.commit(); c.close()
    env["write"]("s-be", [human("p1", "너는 백엔드 워커다", 5000 - 90), human("p2", "다음", 5000 + 5)])
    teamlog.ingest_all()
    assert [json.loads(e["payload_json"])["text"] for e in _events("human_input")] == ["너는 백엔드 워커다", "다음"]


def test_existing_session_ingests_only_after_joining(env, monkeypatch):
    """기존 세션(관리 세션 등)을 팀에 붙이면 그 이전 대화는 보존·색인하지 않는다."""
    import sqlite3
    joined = 5000
    c = sqlite3.connect(str(team.db_path()))
    c.execute("UPDATE agent_sessions SET started=? WHERE session_id='s-mg'", (joined,))
    c.commit(); c.close()
    env["write"]("s-mg", [human("old1", "팀 전의 개인 대화", joined - 3600), human("old2", "역시 예전", joined - 120),
                          human("new1", "팀 일 시작", joined + 5), human("new2", "계속", joined + 10)])
    teamlog.ingest_all()
    texts = [json.loads(e["payload_json"])["text"] for e in _events("human_input")]
    assert texts == ["팀 일 시작", "계속"]
    raw = b"".join(gzip.open(f, "rb").read() for f in (teamlog.archive_root() / "s-mg").glob("*.gz"))
    assert "팀 전의 개인 대화".encode() not in raw, "보존본에도 이전 대화 없음"
    env["write"]("s-mg", [human("new3", "추가", joined + 20)], append=True)
    teamlog.ingest_all()
    assert len(_events("human_input")) == 3, "이후 수집은 정상 이어감(첫 수집이 재작성으로 오인되지 않음)"
