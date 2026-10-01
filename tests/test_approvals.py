"""승인 요청·사람 증명·비서·사장님 프로필(개인 비서 단계 3, docs/designs/workers-p2p.md A-1~A-10)."""
from __future__ import annotations

import json

import pytest

from session_manager import approvals as A, team, teamlog


@pytest.fixture
def env(fake_claude_home, monkeypatch):
    chain: dict = {}
    peers: dict = {}
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: chain.get(sid, sid))
    monkeypatch.setattr(team, "_peer_by_session", lambda: dict(peers))
    sent = []
    from session_manager import push
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a) or 1)
    return {"chain": chain, "peers": peers, "sent": sent}


def _ok(kind, args, by="s-req"):
    ap = A.create(kind, args, requested_by=by)
    A.decide(ap["id"], True, "otp")
    return A.execute(ap["id"])


def test_create_validates_and_notifies(env):
    with pytest.raises(team.TeamError):
        A.create("rm_rf", {})
    with pytest.raises(team.TeamError) as e:
        A.create("team_create", {})
    assert e.value.code == "missing:name"
    with pytest.raises(team.TeamError):
        A.create("task_decide", {"team": "X", "task": "X-T1", "action": "delete"})
    ap = A.create("team_create", {"name": "Web", "members": [{"alias": "fe"}]})
    assert ap["status"] == "pending" and "팀 만들기: Web" in ap["summary"] and "fe" in ap["summary"]
    assert env["sent"][0][0] == f"approval:{ap['id']}" and env["sent"][0][4] == {"approval": ap["id"]}, "폰 알림 → 승인 화면"


def test_decide_requires_proof_once_and_expiry(env, monkeypatch):
    ap = A.create("team_create", {"name": "Web"})
    with pytest.raises(team.TeamError) as e:
        A.decide(ap["id"], True, None)
    assert e.value.status == 403 and e.value.code == "human_proof_required"
    assert A.decide(ap["id"], True, "phone:dev1")["status"] == "approved"
    with pytest.raises(team.TeamError) as e:
        A.decide(ap["id"], False, "otp")
    assert e.value.code == "not_pending:approved"
    old = A.create("team_create", {"name": "Old"})
    monkeypatch.setattr(A, "_now", lambda: team._now() + A.TTL_S + 5)
    assert A.get(old["id"])["status"] == "expired"
    with pytest.raises(team.TeamError) as e:
        A.decide(old["id"], True, "otp")
    assert e.value.code == "expired"


def test_execute_only_approved_once_and_retry_after_failure(env, monkeypatch):
    ap = A.create("team_create", {"name": "Web", "manager_session": "s-m"})
    with pytest.raises(team.TeamError) as e:
        A.execute(ap["id"])
    assert e.value.code == "not_approved:pending"
    A.decide(ap["id"], True, "otp")
    real = A._EXEC["team_create"]
    monkeypatch.setitem(A._EXEC, "team_create", lambda *a: (_ for _ in ()).throw(team.TeamError("boom", 500)))
    with pytest.raises(team.TeamError):
        A.execute(ap["id"])
    assert A.get(ap["id"])["status"] == "approved", "실패하면 다시 시도할 수 있다"
    monkeypatch.setitem(A._EXEC, "team_create", real)
    r = A.execute(ap["id"])
    assert r["status"] == "executed" and r["result"]["code"] == "WEB"
    assert A.execute(ap["id"])["result"] == r["result"], "재실행 = 같은 결과(팀 하나)"
    assert len(team.list_teams()) == 1
    rej = A.create("team_create", {"name": "Nope"})
    A.decide(rej["id"], False, "otp")
    with pytest.raises(team.TeamError):
        A.execute(rej["id"])


def test_executors_team_create_members_archive_purge(env):
    r = _ok("team_create", {"name": "Web", "manager_session": "s-m",
                            "members": [{"alias": "fe", "session_id": "s-fe", "write_scope": ["web/**"]}]})["result"]
    t = team.get_team(r["team"])
    assert {m["alias"] for m in t["members"]} == {"관리", "fe"} and r["monitor_sync"] == "ok"
    assert _ok("team_archive", {"team": "WEB"})["result"]["archived"] is True
    assert team.list_teams() == []
    _ok("team_archive", {"team": "WEB", "on": False})
    assert _ok("team_purge", {"team": "WEB"})["result"]["purged_events"] == 0


def test_delegate_defaults_to_manager_and_task_decide_version(env):
    _ok("team_create", {"name": "Web", "manager_session": "s-m", "members": [{"alias": "fe", "session_id": "s-fe"}]})
    env["peers"]["s-m"] = {"name": "mgr", "socket": r"\\.\pipe\LOCAL\cc-msg-m"}
    d = _ok("delegate", {"team": "WEB", "goal": "로그인 화면"}, by="s-assist")["result"]
    assert d["send_to"]["alias"] == "관리" and d["send_to"]["address"] == r"uds:\\.\pipe\LOCAL\cc-msg-m"
    k = d["task"]
    assert team.task_detail("WEB", k["id"])["events"][0]["actor_session"] == "s-assist"
    team.transition("WEB", k["id"], "submit", assignment_ver=1)
    team.transition("WEB", k["id"], "reject")
    team.transition("WEB", k["id"], "reassign", owner="fe")                 # 버전 2
    team.transition("WEB", k["id"], "submit", assignment_ver=2)
    ap = A.create("task_decide", {"team": "WEB", "task": k["id"], "action": "accept", "assignment_ver": 1})
    A.decide(ap["id"], True, "otp")
    with pytest.raises(team.TeamError) as e:
        A.execute(ap["id"])                                                  # 오래된 승인으로 다른 결과를 승인하지 않음
    assert e.value.code == "stale_assignment"
    assert _ok("task_decide", {"team": "WEB", "task": k["id"], "action": "accept", "assignment_ver": 2})["result"]["status"] == "accepted"


def test_assistant_set_portfolio_and_same_agent_on_replace(env, monkeypatch):
    t = team.create_team("Existing", manager_session="s-m")
    assert A.assistant_info() is None
    info = _ok("assistant_set", {"session_id": "s-as1"})["result"]
    assert info["session_id"] == "s-as1" and info["team"] == "ASSIST"
    pf = [x for x in team.list_teams() if x["kind"] == "portfolio"][0]
    assert pf["code"] == "ASSIST"
    assert team.get_team(t["id"])["parent"] == info["agent_id"], "기존 팀도 비서 포트폴리오로"
    t2 = team.create_team("New", manager_session="s-m2")
    assert team.get_team(t2["id"])["parent"] == info["agent_id"]
    info2 = _ok("assistant_set", {"session_id": "s-as2"})["result"]
    assert info2["agent_id"] == info["agent_id"] and info2["session_id"] == "s-as2", "같은 비서(경력 유지)에 새 세션"
    with pytest.raises(team.TeamError) as e:
        _ok("assistant_set", {"session_id": "s-m"})                        # 이미 다른 에이전트의 세션
    assert e.value.code == "session_owned_by_other_agent"
    assert team.sync_group(pf["id"])["monitor_sync"] == "portfolio", "포트폴리오는 관제 그룹 안 만듦"


def test_assistant_create_session_and_info_is_pure_read(env, monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: calls.append((args, k)) or
                        type("R", (), {"stdout": json.dumps({"session_id": "s-new"}), "returncode": 0})())
    info = _ok("assistant_set", {"create": True})["result"]
    assert info["session_id"] == "s-new" and calls[0][0][1:3] == ["-p", "--output-format"]
    assert calls[0][1]["cwd"].endswith("assistant")
    env["chain"]["s-new"] = "s-new2"
    import sqlite3
    c = sqlite3.connect(str(team.db_path()))
    before = c.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0]
    assert A.assistant_info()["session_id"] == "s-new2"
    assert c.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0] == before, "원격 조회는 쓰지 않는다(A-7)"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "oops", "returncode": 1})())
    with pytest.raises(team.TeamError) as e:
        A.create_assistant_session()
    assert e.value.code.startswith("assistant_create_failed")


def test_profile_propose_redact_dedupe_and_owner_decisions(env):
    p = A.profile_propose("배포 전에 항상 테스트를 돌린다 password=abc12345", topic="습관", evidence=[1, 2, "x"], confidence=1.7)
    assert p["status"] == "proposed" and "abc12345" not in p["statement"] and p["confidence"] == 1.0 and p["evidence"] == [1, 2]
    assert A.profile_propose("배포 전에 항상 테스트를 돌린다 password=abc12345")["id"] == p["id"], "같은 문장은 하나"
    assert _ok("profile_decide", {"id": p["id"], "decision": "confirm"})["result"]["status"] == "confirmed"
    q = A.profile_propose("야근을 좋아한다")
    _ok("profile_decide", {"id": q["id"], "decision": "reject"})
    with pytest.raises(team.TeamError) as e:
        A.profile_propose("야근을  좋아한다")                               # 공백만 달라도 같은 문장
    assert e.value.code == "previously_rejected"
    r = A.profile_propose("한국어 답변 선호")
    ed = _ok("profile_decide", {"id": r["id"], "decision": "edit", "statement": "한국어로, 결론 먼저"})["result"]
    assert ed["source"] == "owner" and ed["status"] == "confirmed"
    d = _ok("profile_decide", {"id": ed["id"], "decision": "delete"})["result"]
    assert d["status"] == "deleted" and d["statement"] == ""
    assert {x["id"] for x in A.profile_list()} == {p["id"]}


def test_purge_drops_profile_evidence(env, monkeypatch):
    import sqlite3
    t = team.create_team("Web", manager_session="s-m")
    with team._Tx() as w:
        eid = team._event(w, t["id"], "human_input", payload={"text": "x"})
    p = A.profile_propose("근거 있는 문장", evidence=[eid, 999])
    teamlog.purge_team(t["id"])
    assert A.profile_list()[0]["evidence"] == [999]


def test_search_all_across_teams(env, fake_claude_home):
    for code in ("AAA", "BBB"):
        t = team.create_team(code, code=code, manager_session="s-" + code)
        with team._Tx() as w:
            eid = team._event(w, t["id"], "human_input", agent_id=t["manager_agent"], payload={"text": f"{code} 로그인 화면 개선"})
            w.execute("INSERT INTO texts(body,kind,team_id,event_id) VALUES(?,?,?,?)", (f"{code} 로그인 화면 개선", "human_input", t["id"], eid))
    r = A.search_all("로그인 화면")
    assert {x["team"] for x in r["items"]} == {"AAA", "BBB"}


def test_portfolio_delivery_failed_is_violation(env):
    info = A.set_assistant("s-as")
    pf = team.get_team(info["team"])
    with team._Tx() as w:
        team._event(w, pf["id"], "msg_failed", payload={"tool_use_id": "t1", "msg_event": 5})
    keys = teamlog.judge(pf["id"])
    assert keys and keys[0].startswith("v:dfail:")
    assert teamlog.violations(pf["id"])[0]["text"] == "비서 위임 전달 실패"


# ---------------------------------------------------------------- API·사람 증명

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


def test_api_approval_flow_and_human_proof(client, monkeypatch):
    from session_manager import connector as C, devices, owner2fa
    r = client.post("/api/v1/team/approvals", json={"kind": "team_create", "args": {"name": "Web"}, "requested_by": "s-x"})
    aid = r.json()["id"]
    assert client.get("/api/owner/approvals").json()["approvals"][0]["id"] == aid
    assert client.post(f"/api/owner/approvals/{aid}/decide", json={"approve": True}).status_code == 403, "로컬은 증명 없이 못 함"
    forged = {C.PROOF_HEADER: "0" * len(C.RELAY_PROOF), C.DEVICE_HEADER: "d1"}
    assert client.post(f"/api/owner/approvals/{aid}/decide", json={"approve": True}, headers=forged).status_code == 403
    monkeypatch.setattr(devices, "is_active", lambda did: did == "d1")
    good = {C.PROOF_HEADER: C.RELAY_PROOF, C.DEVICE_HEADER: "d1"}
    assert client.post(f"/api/owner/approvals/{aid}/decide", json={"approve": True},
                       headers={**good, C.DEVICE_HEADER: "revoked"}).status_code == 403, "폐기된 기기는 안 됨"
    d = client.post(f"/api/owner/approvals/{aid}/decide", json={"approve": True}, headers=good).json()
    assert d["status"] == "approved" and d["decided_via"] == "phone:d1"
    assert client.post(f"/api/v1/team/approvals/{aid}/execute").json()["result"]["code"] == "WEB"
    # PC: 2차 인증 코드
    a2 = client.post("/api/v1/team/approvals", json={"kind": "team_archive", "args": {"team": "WEB"}}).json()["id"]
    monkeypatch.setattr(owner2fa, "enabled", lambda: True)
    monkeypatch.setattr(owner2fa, "verify", lambda code, **k: code == "123456")
    assert client.post(f"/api/owner/approvals/{a2}/decide", json={"approve": True, "otp": "000000"}).status_code == 403
    assert client.post(f"/api/owner/approvals/{a2}/decide", json={"approve": True, "otp": "123456"}).json()["decided_via"] == "otp"
    assert client.get("/api/owner/assistant").json() == {"assistant": None}
    assert not C._is_local_only_api("/api/owner/approvals/x/decide", "POST"), "폰에서 결정할 수 있어야"
    assert C._is_local_only_api("/api/v1/team/approvals/x/execute", "POST"), "실행은 PC 안에서만"


def test_team_import_is_approval_only(env, tmp_path):
    proj = tmp_path / "proj"; (proj / ".clewpath").mkdir(parents=True)
    (proj / ".clewpath" / "workers.json").write_text(json.dumps({"workers": {"fe": {"session_id": "s-fe"}}}), encoding="utf-8")
    r = _ok("team_import", {"path": str(proj)})["result"]
    assert r["added"] == ["fe"] and r["code"]


def test_task_cancel_via_approval_and_rules(env):
    _ok("team_create", {"name": "Web", "manager_session": "s-m", "members": [{"alias": "fe", "session_id": "s-fe"}]})
    k = team.create_task("WEB", "잘못 만든 일감", "fe")
    team.transition("WEB", k["id"], "block", note="막힘")
    with pytest.raises(team.TeamError) as e:
        _ok("task_decide", {"team": "WEB", "task": k["id"], "action": "reject"})        # 제출 안 된 일감은 반려 불가(실사용 발견)
    assert "blocked->reject" in e.value.code
    ap = A.create("task_decide", {"team": "WEB", "task": k["id"], "action": "cancel", "note": "정리"})
    assert ap["summary"].endswith("취소")
    A.decide(ap["id"], True, "otp")
    r = A.execute(ap["id"])["result"]
    assert r["status"] == "cancelled" and r["closed"]
    assert all(x["id"] != k["id"] for x in team.get_team("WEB")["open_tasks"]), "취소는 열린 일감에서 빠짐"
    k2 = team.create_task("WEB", "제출된 일감", "fe")
    team.transition("WEB", k2["id"], "submit", assignment_ver=1)
    with pytest.raises(team.TeamError):
        team.transition("WEB", k2["id"], "cancel")                                       # 제출된 건 승인/반려로 결정
