"""팀·에이전트 명부 + 일감 장부(docs/designs/workers-p2p.md 단계 1, eng E-1~E-13)."""
from __future__ import annotations

import json

import pytest

from session_manager import team


@pytest.fixture
def T(fake_claude_home, monkeypatch):
    chain: dict[str, str] = {}
    peers: dict[str, dict] = {}
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: chain.get(sid, sid))
    monkeypatch.setattr(team, "_peer_by_session", lambda: dict(peers))
    team.chain, team.peers_stub = chain, peers
    return team


def _mk(T, root=None):
    t = T.create_team("Portal App", root=root, manager_session="s-mgr")
    a = T.add_member(t["id"], "백엔드", role="API", tags=["py"], session_id="s-be", write_scope=["api/**"])
    b = T.add_member(t["id"], "프론트", role="UI", session_id="s-fe")
    return t, a, b


def test_team_create_code_members_and_manager(T):
    t, a, b = _mk(T)
    assert t["code"] == "PORTALAPP"[:6] or t["code"].startswith("PORTAL")
    g = T.get_team(t["id"])
    assert [m["alias"] for m in g["members"]][0] == "관리" and g["manager_agent"]
    assert {m["alias"] for m in g["members"]} == {"관리", "백엔드", "프론트"}
    assert T.get_team(t["code"].lower())["id"] == t["id"], "코드로도 찾는다(대소문자 무시)"
    t2 = T.create_team("Portal App")
    assert t2["code"] != t["code"], "같은 이름이면 코드 접미사"
    with pytest.raises(T.TeamError) as e:
        T.add_member(t["id"], "백엔드", session_id="s-x")
    assert e.value.code == "alias_taken" and e.value.status == 409
    with pytest.raises(T.TeamError) as e:
        T.add_member(t["id"], "관리2", member_role="manager")
    assert e.value.code == "manager_exists"
    with pytest.raises(T.TeamError):
        T.create_team("  ")


def test_session_binding_idempotent_and_exclusive(T):
    t, a, b = _mk(T)
    T.bind_session(t["id"], a["agent_id"], "s-be")                          # 같은 에이전트·같은 세션 = 멱등
    with pytest.raises(T.TeamError) as e:
        T.bind_session(t["id"], b["agent_id"], "s-be")
    assert e.value.code == "session_owned_by_other_agent" and e.value.status == 409
    m = T.bind_session(t["id"], "백엔드", "s-be2", reason="replaced")       # 별칭으로도
    assert m["session_id"] == "s-be2"
    with pytest.raises(T.TeamError):
        T.bind_session(t["id"], "백엔드", "s-be3", reason="weird")


def test_current_session_follows_continued_in_and_records(T):
    t, a, b = _mk(T)
    T.chain["s-be"] = "s-be-c2"
    assert T.member(t["id"], "백엔드")["session_id"] == "s-be-c2"
    import sqlite3
    c = sqlite3.connect(str(T.db_path()))
    rows = c.execute("SELECT session_id, reason, ended IS NULL FROM agent_sessions WHERE agent_id=? ORDER BY rowid",
                     (a["agent_id"],)).fetchall()
    assert rows == [("s-be", "created", 0), ("s-be-c2", "continued", 1)], "교체 이력이 경력으로 남는다"
    kinds = [r[0] for r in c.execute("SELECT kind FROM events WHERE agent_id=?", (a["agent_id"],))]
    assert "session_bound" in kinds


def test_member_view_address_is_pipe_not_name(T):
    t, a, b = _mk(T)
    T.peers_stub["s-be"] = {"name": "be-7f", "socket": r"\\.\pipe\LOCAL\cc-msg-abc", "status": "idle"}
    m = T.member(t["id"], "백엔드")
    assert m["live"] and m["address"] == r"uds:\\.\pipe\LOCAL\cc-msg-abc" and m["name"] == "be-7f"
    f = T.member(t["id"], "프론트")
    assert f["live"] is False and f["address"] is None


def test_task_state_machine_idem_and_stale_assignment(T, tmp_path):
    t, a, b = _mk(T, root=str(tmp_path))
    k = T.create_task(t["id"], "토큰 만료 처리", "백엔드", collaborators=["프론트"], done_when="e2e", idem_key="k1")
    assert k["id"].endswith("-T1") and k["status"] == "assigned" and k["assignment_ver"] == 1
    assert T.create_task(t["id"], "토큰 만료 처리", "백엔드", idem_key="k1")["id"] == k["id"], "재시도 = 같은 일감"
    assert T.create_task(t["id"], "다른 일", "프론트")["id"].endswith("-T2")
    with pytest.raises(T.TeamError) as e:
        T.transition(t["id"], k["id"], "accept")
    assert e.value.status == 409 and "assigned->accept" in e.value.code
    with pytest.raises(T.TeamError) as e:
        T.transition(t["id"], k["id"], "submit")                            # 배정 버전 없이 제출 불가
    assert e.value.code == "stale_assignment"
    s = T.transition(t["id"], k["id"], "submit", assignment_ver=1, idem_key="sub1")
    assert s["status"] == "submitted"
    assert T.transition(t["id"], k["id"], "submit", assignment_ver=1, idem_key="sub1")["status"] == "submitted", \
        "같은 idem_key 재전송은 한 번만 반영(409 아님)"
    r = T.transition(t["id"], k["id"], "reject", note="테스트 실패")
    assert r["status"] == "rejected"
    ro = T.transition(t["id"], k["id"], "reassign", owner="프론트")
    assert ro["status"] == "assigned" and ro["assignment_ver"] == 2 and ro["owner"] == b["agent_id"]
    with pytest.raises(T.TeamError) as e:
        T.transition(t["id"], k["id"], "submit", assignment_ver=1)          # 옛 담당자의 늦은 제출
    assert e.value.code == "stale_assignment"
    T.transition(t["id"], k["id"], "submit", assignment_ver=2)
    acc = T.transition(t["id"], k["id"], "accept")
    assert acc["status"] == "accepted" and acc["closed"]
    d = T.task_detail(t["id"], k["id"])
    assert [e["kind"] for e in d["events"]] == ["assigned", "submitted", "rejected", "reassigned", "submitted", "accepted"]
    with pytest.raises(T.TeamError):
        T.transition(t["id"], k["id"], "fly")
    T.transition(t["id"], k["id"], "note", note="회고: 단위 확인 필요")
    assert T.task_detail(t["id"], k["id"])["events"][-1]["kind"] == "note"


def test_submit_evidence_hash_missing_and_git(T, tmp_path, monkeypatch):
    t, a, b = _mk(T, root=str(tmp_path))
    (tmp_path / "out.md").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(T, "_git_head", lambda p: "a" * 40)
    k = T.create_task(t["id"], "문서", "백엔드")
    T.transition(t["id"], k["id"], "submit", assignment_ver=1, artifacts=["out.md", "nope.md"])
    ev = T.task_detail(t["id"], k["id"])["events"][-1]["payload"]["evidence"]
    a0, a1 = ev["artifacts"]
    import hashlib
    assert a0["sha256"] == hashlib.sha256(b"hello").hexdigest() and a0["size"] == 5 and a0["git_head"] == "a" * 40
    assert a1.get("missing") is True, "없는 파일도 제출은 받고 missing 표기"
    assert ev["root_git_head"] == "a" * 40


def test_move_keeps_history(T):
    t, a, b = _mk(T)
    t2 = T.create_team("Ops")
    m = T.move(t["id"], "백엔드", t2["id"], alias="운영")
    assert m["agent_id"] == a["agent_id"] and m["alias"] == "운영"
    assert "백엔드" not in {x["alias"] for x in T.get_team(t["id"])["members"]}
    import sqlite3
    c = sqlite3.connect(str(T.db_path()))
    rows = c.execute("SELECT team_id, left_at IS NOT NULL FROM memberships WHERE agent_id=? ORDER BY joined, rowid",
                     (a["agent_id"],)).fetchall()
    assert rows == [(t["id"], 1), (t2["id"], 0)]


def test_sync_group_preserves_user_name_notify_and_pending(T, monkeypatch):
    from session_manager import mongroups
    t, a, b = _mk(T)
    r = T.sync_group(t["id"])
    assert r["monitor_sync"] == "ok" and r["monitor_group_id"]
    g = mongroups.get(r["monitor_group_id"])
    assert g["manager"] == "s-mgr" and set(g["subs"]) == {"s-be", "s-fe"} and g["labels"]["s-be"].startswith("백엔드")
    mongroups.update(g["id"], name="내 이름", notify={"manager_stop": False})
    T.add_member(t["id"], "QA", session_id="s-qa")
    r2 = T.sync_group(t["id"])
    g2 = mongroups.get(r2["monitor_group_id"])
    assert r2["monitor_group_id"] == g["id"], "그룹은 팀당 1개(중복 생성 없음)"
    assert g2["name"] == "내 이름" and g2["notify"]["manager_stop"] is False and "s-qa" in g2["subs"]
    T.chain["s-fe"] = "s-fe2"                                                # 워커 교체 → 그룹도 따라감
    g3 = mongroups.get(T.sync_group(t["id"])["monitor_group_id"])
    assert "s-fe2" in g3["subs"] and "s-fe" not in g3["subs"]
    monkeypatch.setattr(mongroups, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    T.add_member(t["id"], "문서", session_id="s-doc")
    r4 = T.sync_group(t["id"])
    assert r4["monitor_sync"] == "pending" and r4["monitor_group_id"] == g["id"]


def test_sync_group_without_manager(T):
    t = T.create_team("Solo")
    T.add_member(t["id"], "w", session_id="s-w")
    assert T.sync_group(t["id"])["monitor_sync"] == "no_manager"


def test_overdue_notifies_once_per_assignment(T, monkeypatch):
    sent = []
    from session_manager import push
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a) or 1)
    t, a, b = _mk(T)
    k = T.create_task(t["id"], "급한 일", "백엔드", due=1000)
    T.create_task(t["id"], "여유", "백엔드", due=10 ** 12)
    assert [x["task"]["id"] for x in T.overdue()] == [k["id"]]
    T.watch_once(); T.watch_once()
    assert len(sent) == 1 and "기한 초과" in sent[0][2] and sent[0][1] == "s-mgr"
    T.transition(t["id"], k["id"], "reassign", owner="프론트")              # 새 배정 버전 → 다시 감시
    T.watch_once()
    assert len(sent) == 2
    T.transition(t["id"], k["id"], "submit", assignment_ver=2)
    assert T.overdue() == [], "제출되면 기한 감시 해제"


def test_import_v1(T, tmp_path):
    proj = tmp_path / "proj"; (proj / ".clewpath").mkdir(parents=True)
    (proj / ".clewpath" / "workers.json").write_text(json.dumps({
        "workers": {"포털": {"session_id": "s-p", "role": "프론트"}, "깨짐": {"role": "x"}},
        "monitor_group_id": "abcd1234"}), encoding="utf-8")
    r = T.import_v1(str(proj))
    assert r["added"] == ["포털"] and r["skipped"] == [{"alias": "깨짐", "reason": "no_session"}]
    assert r["team"]["root"] == str(proj) and r["team"]["monitor_group_id"] == "abcd1234"
    with pytest.raises(T.TeamError) as e:
        T.import_v1(str(tmp_path / "none"))
    assert e.value.code == "v1_not_found"
    bad = tmp_path / "bad.json"; bad.write_text("{", encoding="utf-8")
    with pytest.raises(T.TeamError) as e:
        T.import_v1(str(bad))
    assert e.value.code == "v1_invalid"


def test_corrupt_db_is_503_not_crash(T):
    T.db_path().parent.mkdir(parents=True, exist_ok=True)
    T.db_path().write_bytes(b"not a sqlite file at all" * 100)
    with pytest.raises(T.TeamError) as e:
        T.list_teams()
    assert e.value.status == 503


# ---------------------------------------------------------------- API·보안

@pytest.fixture
def client(T, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    with TestClient(S.create_app()) as c:
        c.S = S
        yield c


def test_api_flow(client):
    r = client.post("/api/v1/team", json={"name": "Web", "manager_session": "s-m"})
    assert r.status_code == 403 and r.json()["error"] == "approval_required", "팀 만들기는 승인 요청으로만(단계 3)"
    tid = team.create_team("Web", manager_session="s-m")["id"]
    assert client.post(f"/api/v1/team/{tid}/members", json={"alias": "a", "session_id": "s-a"}).status_code == 200
    assert client.post(f"/api/v1/team/{tid}/members", json={"alias": "a"}).status_code == 409
    k = client.post(f"/api/v1/team/{tid}/tasks", json={"goal": "g", "owner": "a"}).json()
    assert client.post(f"/api/v1/team/{tid}/tasks/{k['id']}/submit", json={"assignment_ver": 1}).json()["status"] == "submitted"
    assert client.post(f"/api/v1/team/{tid}/tasks/{k['id']}/submit", json={"assignment_ver": 1}).status_code == 409
    assert client.get(f"/api/v1/team/{tid}/tasks/{k['id']}").json()["events"][-1]["kind"] == "submitted"
    assert client.get("/api/v1/team/nope").status_code == 404
    assert [x["id"] for x in client.get("/api/v1/team").json()["teams"]] == [tid]


def test_api_is_local_only_on_server_side(client, monkeypatch):
    monkeypatch.setattr(client.S, "_client_host", lambda r: "10.0.0.5")
    assert client.get("/api/v1/team").status_code in (401, 403, 503), "원격은 인증 미들웨어부터 막힌다"
    monkeypatch.setattr(client.S, "_client_host", lambda r: "127.0.0.1")
    monkeypatch.setattr(client.S, "_via_relay", lambda r: True)              # 루프백이어도 커넥터 경유면 원격
    assert client.get("/api/v1/team").status_code == 503, "미들웨어가 먼저 막는다"
    # 라우트 자체 검사(eng E-5)만 보려고 미들웨어(scope dict)는 통과시키고 라우트(Request)는 원격으로
    real = client.S._is_local
    monkeypatch.setattr(client.S, "_is_local", lambda x: isinstance(x, dict) or real(x))
    for verb, path in (("get", "/api/v1/team"), ("post", "/api/v1/team")):
        r = getattr(client, verb)(path, **({"json": {"name": "x"}} if verb == "post" else {}))
        assert r.status_code == 403 and r.json()["error"] == "local_only", path
    from session_manager import connector as C
    assert C._is_local_only_api("/api/v1/team", "GET") and C._is_local_only_api("/api/v1/team/x/tasks/y/submit", "POST")


@pytest.mark.parametrize("skill", ["clewpath-workers", "clewpath-assistant"])
def test_skill_doc_api_paths_exist(client, skill):
    """eng E-4: SKILL.md 가 세션에게 부르라고 적은 /api/… 경로가 전부 실제 라우트여야 한다(워커·비서 스킬)."""
    import re
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "session_manager" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"(/api/[A-Za-z0-9_./{}<>-]+)", doc))
    assert paths, "스킬 문서에 API 경로가 있어야 한다"
    routes = [getattr(r, "path", "") for r in client.app.routes]

    def norm(p):
        p = re.sub(r"<[^>]+>", "{x}", p.rstrip(".")).split("?")[0]
        return re.sub(r"\{[^}]+\}", "{x}", p)
    have = {norm(r) for r in routes}
    missing = sorted(p for p in paths if norm(p) not in have)
    assert not missing, f"문서에 있지만 라우트에 없는 경로: {missing}"


def test_webmonitor_group_diff_follows_saved_group_only(fake_claude_home):
    """eng E-8: 저장 그룹으로 연 관제는 그룹 구성원 변경을 따라가되, 화면에서 손으로 더한 세션은 안 뺀다."""
    from session_manager import mongroups, webmonitor
    assert webmonitor._MAX_SESSIONS == 12
    g = mongroups.save("g", "m", ["a", "b"])
    synced = {"m", "a", "b"}
    current = {"m", "a", "b", "manual"}
    mongroups.save("g", "m", ["a", "c"], gid=g["id"])                     # b 빠지고 c 들어옴
    add, rm, synced = webmonitor.group_diff(g["id"], synced, current)
    assert add == ["c"] and rm == ["b"] and "manual" not in rm
    assert webmonitor.group_diff(None, {"x"}, {"x"}) == ([], [], {"x"})
    assert webmonitor.group_diff("nope", {"x"}, {"x"}) == ([], [], {"x"})


def test_integrity_race_is_409(T):
    t, a, b = _mk(T)
    with pytest.raises(T.TeamError) as e:
        with T._Tx() as c:                       # 다른 요청이 같은 세션을 먼저 넣은 상황을 직접 재현
            c.execute("INSERT INTO agent_sessions(agent_id,session_id,started,reason) VALUES('x','s-be',1,'created')")
    assert e.value.status == 409 and e.value.code == "conflict"


def test_archive_hides_team_from_list_watch_and_overdue(T, monkeypatch):
    sent = []
    from session_manager import push
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a) or 1)
    t, a, b = _mk(T)
    T.create_task(t["id"], "늦은 일", "백엔드", due=1000)
    T.archive(t["id"])
    assert T.list_teams() == [] and T.list_teams(include_archived=True)[0]["archived"] is True
    w = T.watch_once()
    assert w["teams"] == 0 and w["overdue"] == 0 and sent == [], "보관 팀은 감시·알림 제외"
    assert T.get_team(t["id"])["members"], "이력은 그대로"
    T.archive(t["id"], on=False)
    assert len(T.list_teams()) == 1


def test_same_session_reused_across_teams_and_failed_create_leaves_no_team(T, monkeypatch):
    t1 = T.create_team("One", manager_session="s-mgr")
    t2 = T.create_team("Two", manager_session="s-mgr")                       # 같은 관리 세션 → 같은 에이전트, 두 팀 소속
    assert T.get_team(t1["id"])["manager_agent"] == T.get_team(t2["id"])["manager_agent"]
    w = T.add_member(t1["id"], "w", session_id="s-w")
    with pytest.raises(T.TeamError) as e:
        T.add_member(t1["id"], "w2", session_id="s-w")                        # 같은 팀에 같은 에이전트 두 번
    assert e.value.code == "already_member"
    t3 = T.create_team("Three", manager_session="s-w")                      # 워커 세션이 다른 팀 관리자 = 같은 에이전트
    assert T.get_team(t3["id"])["manager_agent"] == w["agent_id"]
    n = len(T.list_teams())
    def boom(*a, **k):
        raise T.TeamError("session_owned_by_other_agent", 409)
    monkeypatch.setattr(T, "add_member", boom)
    with pytest.raises(T.TeamError):
        T.create_team("Bad", manager_session="s-x")
    assert len(T.list_teams()) == n, "관리자 등록 실패 시 반쪽 팀이 남지 않는다"
