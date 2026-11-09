"""에이전트 단위 단계 1(docs/designs/agent-unit.md): 형식 v8, 팀 밖 에이전트, 연결 규칙, 읽기 전용 current_session, 사장님 API."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from session_manager import scanner, store, team


@pytest.fixture
def T(fake_claude_home, monkeypatch):
    chain: dict[str, str] = {}
    known: dict[str, SimpleNamespace] = {}
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: chain.get(sid, sid))
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    monkeypatch.setattr(scanner, "scan_one", lambda sid: known.get(sid))
    team.chain, team.known = chain, known
    for s in ("s1", "s2", "s3", "s4"):
        known[s] = SimpleNamespace(session_id=s, headless=False)
    return team


def _rows(sql, *a):
    c = sqlite3.connect(str(team.db_path()))
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def _err(fn, *a, **kw):
    with pytest.raises(team.TeamError) as e:
        fn(*a, **kw)
    return e.value.code, e.value.status


# ---------------------------------------------------------------- 형식 v8
def test_v8_closes_extra_open_rows_before_unique_index():
    """어긋난 데이터가 있어도 형식 올리기가 죽지 않고, 가장 최근 줄만 지금 세션으로 남는다."""
    c = sqlite3.connect(":memory:")
    c.executescript("CREATE TABLE agents(id TEXT PRIMARY KEY, alias TEXT, role TEXT, tags_json TEXT, status TEXT, created INTEGER, retired INTEGER);"
                    "CREATE TABLE agent_sessions(agent_id TEXT, session_id TEXT UNIQUE, started INTEGER, ended INTEGER, reason TEXT, team_id TEXT, pc_id TEXT);"
                    "INSERT INTO agent_sessions VALUES('a','old',1,NULL,'created',NULL,'local'),('a','new',2,NULL,'replaced',NULL,'local'),"
                    "('b','only',1,NULL,'created',NULL,'local'),('b','done',0,5,'created',NULL,'local');")
    c.executescript(store._SCHEMA_V8)
    open_rows = {r[0] for r in c.execute("SELECT session_id FROM agent_sessions WHERE ended IS NULL")}
    assert open_rows == {"new", "only"}
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE agent_sessions SET ended=NULL WHERE session_id='old'")


def test_old_version_writes_survive_v8_and_get_uid_on_repair(T):
    """옛 판은 uid 없이 INSERT 하고 다른 줄을 먼저 닫은 뒤 넣는다 — 색인과 안 부딪히고, 빈 uid 는 수리가 채운다(D4·R1)."""
    t = T.create_team("옛판", manager_session="m0")
    a = T.add_member(t["id"], "워커", session_id="s1")
    assert _rows("SELECT uid FROM agents WHERE id=?", a["agent_id"]) == [(None,)]
    with T._Tx() as c:                                       # 옛 _bind 와 같은 순서의 SQL
        c.execute("UPDATE agent_sessions SET ended=? WHERE agent_id=? AND ended IS NULL", (9, a["agent_id"]))
        c.execute("INSERT INTO agent_sessions(agent_id,session_id,started,reason,team_id) VALUES(?,?,?,?,?)",
                  (a["agent_id"], "s2", 9, "replaced", t["id"]))
    assert T.repair_uids() >= 1
    uid = _rows("SELECT uid FROM agents WHERE id=?", a["agent_id"])[0][0]
    assert len(uid) == 36
    T.repair_uids()
    assert _rows("SELECT uid FROM agents WHERE id=?", a["agent_id"])[0][0] == uid, "이미 있는 uid 는 안 바뀐다"


# ---------------------------------------------------------------- 만들기·이름(G4)
def test_create_agent_outside_team(T):
    a = T.create_agent("s1", "  조사원 ", role="자료")
    assert a["name"] == "조사원" and a["session_id"] == "s1" and a["teams"] == [] and a["uid"] and a["sessions"] == 1
    assert a["history"][0]["reason"] == "created"


@pytest.mark.parametrize("name,sid,code,status", [
    ("", "s2", "name_required", 400), ("조사원", "s2", "agent_name_taken", 409),
    ("새이름", "s1", "session_owned_by_other_agent", 409), ("새이름", "없음", "session_not_found", 404),
    ("새이름", "", "session_required", 400)])
def test_create_agent_refusals(T, name, sid, code, status):
    T.create_agent("s1", "조사원")
    assert _err(T.create_agent, sid, name) == (code, status)


def test_create_agent_refuses_headless(T):
    T.known["s2"].headless = True
    assert _err(T.create_agent, "s2", "자동") == ("session_headless", 409)


def test_team_path_may_still_repeat_names(T):
    """팀마다 같은 별칭(관리)을 쓴다 — 겹침 검사는 새 입구에만(G4)."""
    for n in ("가팀", "나팀"):
        t = T.create_team(n, manager_session=f"m-{n}")
        T.add_member(t["id"], "워커", session_id=f"w-{n}")
    assert len(_rows("SELECT id FROM agents WHERE alias='워커'")) == 2


def test_rename_is_agent_only_and_checks_rev(T, monkeypatch):
    from session_manager import native_title
    monkeypatch.setattr(native_title, "set_native_title", lambda *a, **k: pytest.fail("세션 이름을 건드리면 안 된다(G4)"))
    a = T.create_agent("s1", "조사원")
    b = T.create_agent("s2", "분석가")
    assert _err(T.rename_agent, a["id"], "분석가") == ("agent_name_taken", 409)
    assert _err(T.rename_agent, a["id"], "새이름", agent_rev=a["rev"] + 5) == ("agent_rev_conflict", 409)
    r = T.rename_agent(a["id"], "새이름", agent_rev=a["rev"])
    assert r["name"] == "새이름" and r["rev"] == a["rev"] + 1
    assert T.rename_agent(b["id"], "분석가")["name"] == "분석가", "자기 이름 그대로는 겹침이 아니다"


# ---------------------------------------------------------------- 붙이기·떼기(D3)
def test_session_belongs_to_one_agent_forever(T):
    a = T.create_agent("s1", "가")
    b = T.create_agent("s2", "나")
    assert _err(T.attach_session, b["id"], "s1", replace=True) == ("session_owned_by_other_agent", 409)
    T.detach_session(a["id"])
    assert _err(T.attach_session, b["id"], "s1", replace=True) == ("session_owned_by_other_agent", 409), "떼어 내도 다른 에이전트로는 못 간다"


def test_attach_needs_replace_when_current_exists(T):
    a = T.create_agent("s1", "가")
    assert _err(T.attach_session, a["id"], "s2") == ("has_current_session", 409)
    r = T.attach_session(a["id"], "s2", replace=True)
    assert r["session_id"] == "s2" and [h["reason"] for h in r["history"]] == ["imported", "created"]
    assert _err(T.attach_session, a["id"], "없음") == ("session_not_found", 404)


def test_detach_keeps_history_and_means_no_current(T):
    a = T.create_agent("s1", "가")
    r = T.detach_session(a["id"])
    assert r["session_id"] is None and r["history"][0]["detached"] and r["history"][0]["ended"]
    assert T.current_session(a["id"]) is None, "끝난 마지막 세션을 지금 세션으로 돌려주면 안 된다"
    assert _err(T.detach_session, a["id"]) == ("not_current_session", 409)


def test_reattach_keeps_first_record_and_logs(T):
    a = T.create_agent("s1", "가")
    first = T.agent_detail(a["id"])["history"][0]
    T.detach_session(a["id"])
    r = T.attach_session(a["id"], "s1")
    h = r["history"][0]
    assert r["session_id"] == "s1" and not h["detached"] and (h["started"], h["reason"]) == (first["started"], "created")
    kinds = [x[0] for x in _rows("SELECT kind FROM events WHERE agent_id=? ORDER BY id", a["id"])]
    assert kinds == ["agent_created", "session_detached", "session_attached"]


def test_retire_and_revive(T):
    a = T.create_agent("s1", "가")
    assert T.retire_agent(a["id"])["status"] == "retired"
    assert _err(T.attach_session, a["id"], "s2", replace=True) == ("agent_retired", 409)
    b = T.create_agent("s2", "가")                          # 쉬는 동안엔 이름을 쓸 수 있다
    assert _err(T.retire_agent, a["id"], False) == ("agent_name_taken", 409)
    T.retire_agent(b["id"])
    assert T.retire_agent(a["id"], False)["status"] == "active"


# ---------------------------------------------------------------- 읽기 전용(D13)
def test_current_session_reads_without_writing(T):
    a = T.create_agent("s1", "가")
    T.chain["s1"] = "s2"
    assert T.current_session(a["id"]) == "s2"
    assert _rows("SELECT session_id FROM agent_sessions WHERE agent_id=?", a["id"]) == [("s1",)], "읽기 경로는 안 쓴다"


def test_current_session_stops_at_session_owned_by_other(T):
    a = T.create_agent("s1", "가")
    T.create_agent("s2", "나")
    T.chain["s1"] = "s2"
    assert T.current_session(a["id"]) == "s1"
    assert T.follow_continuations(a["id"]) == "s1"


def test_watch_records_continuation_for_agent_outside_team(T, monkeypatch):
    a = T.create_agent("s1", "가")
    T.chain["s1"] = "s2"
    T.watch_once()
    assert _rows("SELECT session_id, reason, ended IS NULL FROM agent_sessions WHERE agent_id=? ORDER BY rowid", a["id"]) == [
        ("s1", "created", 0), ("s2", "continued", 1)]
    assert T.watch_once() is not None and len(_rows("SELECT 1 FROM agent_sessions WHERE agent_id=?", a["id"])) == 2, "멱등"


def test_watch_skips_retired_and_detached(T):
    a = T.create_agent("s1", "가")
    b = T.create_agent("s3", "나")
    T.retire_agent(a["id"])
    T.detach_session(b["id"])
    T.chain.update({"s1": "s2", "s3": "s4"})
    T.watch_once()
    assert len(_rows("SELECT 1 FROM agent_sessions")) == 2


def test_overview_is_db_only_and_marks_sessions(T, monkeypatch):
    a = T.create_agent("s1", "가", role="역")
    b = T.create_agent("s2", "나")
    T.detach_session(b["id"])
    monkeypatch.setattr(T, "_continuation_hops", lambda *x, **k: pytest.fail("목록은 파일을 읽지 않는다"))
    o = T.agents_overview()
    by = {x["name"]: x for x in o["agents"]}
    assert by["가"]["session_id"] == "s1" and by["가"]["role"] == "역" and by["나"]["session_id"] is None
    assert o["by_session"]["s1"] == {"agent_id": a["id"], "name": "가", "current": True, "detached": False}
    assert o["by_session"]["s2"]["detached"] is True and o["by_session"]["s2"]["current"] is False


def test_overview_lists_team_and_assistant_flags(T):
    t = T.create_team("가팀", manager_session="m1")
    m = T.add_member(t["id"], "워커", session_id="w1")
    o = {x["id"]: x for x in T.agents_overview()["agents"]}
    assert o[m["agent_id"]]["teams"] == [{"code": t["code"], "name": "가팀"}] and o[m["agent_id"]]["assistant"] is False


# ---------------------------------------------------------------- 사장님 API
@pytest.fixture
def client(T, monkeypatch):
    import os
    for k in [k for k in os.environ if k.startswith("SM_")]:
        monkeypatch.delenv(k)
    from fastapi.testclient import TestClient
    from session_manager import server as S
    return TestClient(S.create_app(), base_url="http://127.0.0.1")


def test_owner_api_flow(client):
    r = client.post("/api/owner/agents", json={"session_id": "s1", "name": "가", "role": "역"})
    assert r.status_code == 200 and r.json()["name"] == "가"
    aid = r.json()["id"]
    assert client.post("/api/owner/agents", json={"session_id": "s2", "name": "가"}).json() == {"error": "agent_name_taken"}
    assert client.get("/api/owner/agents").json()["by_session"]["s1"]["current"] is True
    assert client.post(f"/api/owner/agents/{aid}/rename", json={"name": "다"}).json()["name"] == "다"
    assert client.post(f"/api/owner/agents/{aid}/attach", json={"session_id": "s2"}).status_code == 409
    assert client.post(f"/api/owner/agents/{aid}/attach", json={"session_id": "s2", "replace": True}).json()["session_id"] == "s2"
    assert client.post(f"/api/owner/agents/{aid}/detach", json={}).json()["session_id"] is None
    assert client.post(f"/api/owner/agents/{aid}/retire", json={}).json()["status"] == "retired"
    assert client.get("/api/owner/agents/없는").status_code == 404
