"""팀으로 통합(docs/designs/team-unify.md, 2026-10-13): 팀 없는 관제 그룹 → 팀(무손실·멱등) · 보관 = 그룹 삭제 ·
사장님 팀 API(폰에서도) · 화면에서 낸 승인 요청(생성·실행) · 에이전트 경로(/api/v1/team)는 여전히 로컬 전용."""
from __future__ import annotations

import pytest

from session_manager import mongroups, team


@pytest.fixture
def T(fake_claude_home, monkeypatch):
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {"s-m": {"name": "m", "socket": "P"}})
    return team


def test_adopt_groups_lossless_idempotent(T):
    g = mongroups.save("개발관리", "s-m", ["s-a", "s-b"], labels={"s-m": "총괄 · 관리", "s-a": "대백과 서비스", "s-b": "논문 · 논문 서비스"},
                       notify={"sub_stop": False})
    r = T.adopt_groups()
    assert len(r["adopted"]) == 1
    t = T.get_team(r["adopted"][0]["team"])
    assert t["name"] == "개발관리" and t["monitor_group_id"] == g["id"], "그룹 id 그대로 팀에 연결"
    al = {m["alias"]: (m["member_role"], m["role"], m["session_id"]) for m in t["members"]}
    assert al == {"총괄": ("manager", "관리", "s-m"), "대백과 서비스": ("worker", "", "s-a"), "논문": ("worker", "논문 서비스", "s-b")}
    g2 = mongroups.get(g["id"])
    assert g2["name"] == "개발관리" and g2["notify"]["sub_stop"] is False, "이름·알림 설정은 그룹 값 유지"
    assert T.adopt_groups()["adopted"] == [], "두 번 불러도 팀이 늘지 않는다"
    assert len(mongroups.list_groups()) == 1


def test_archive_drops_group_and_unarchive_recreates(T):
    t = T.create_team("Web", manager_session="s-m")
    T.add_member(t["id"], "fe", session_id="s-a")
    gid = T.sync_group(t["id"])["monitor_group_id"]
    assert mongroups.get(gid)
    T.archive(t["id"], True)
    assert mongroups.get(gid) is None and T.get_team(t["id"])["monitor_group_id"] is None, "보관 = 알림 멈춤"
    assert T.adopt_groups()["adopted"] == []
    T.archive(t["id"], False)
    g = T.get_team(t["id"])["monitor_group_id"]
    assert g and mongroups.get(g)["subs"] == ["s-a"]


def test_adopt_drops_group_left_by_archived_team(T):
    t = T.create_team("Old", manager_session="s-m")
    gid = T.sync_group(t["id"])["monitor_group_id"]
    with team._Tx() as c:                         # 옛 버전처럼 보관만 되고 그룹은 남은 상태
        c.execute("UPDATE teams SET archived=1 WHERE id=?", (t["id"],))
    r = T.adopt_groups()
    assert r["dropped"] == [gid] and r["adopted"] == [] and mongroups.get(gid) is None


def test_rename_and_owner_views(T):
    t = T.create_team("Web", manager_session="s-m")
    T.add_member(t["id"], "fe", session_id="s-a")
    T.sync_group(t["id"])
    T.create_task(t["id"], "로그인 화면", "fe")
    T.rename(t["code"], "웹 포털")
    assert mongroups.get(T.get_team(t["id"])["monitor_group_id"])["name"] == "웹 포털"
    ls = T.owner_list()
    assert ls[0]["name"] == "웹 포털" and ls[0]["tasks"] == {"assigned": 1} and ls[0]["notify"]["manager_stop"] is True
    assert [m["live"] for m in ls[0]["members"]] == [True, False]
    d = T.owner_detail(t["code"])
    assert d["tasks"][0]["goal"] == "로그인 화면" and d["archived"] is False and "violations" in d


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app(), base_url="http://127.0.0.1")


def test_owner_team_api_works_via_relay_and_agent_api_stays_local(T, monkeypatch):
    t = T.create_team("Web", manager_session="s-m")
    T.sync_group(t["id"])
    via = {"X-ClewPath-Via": "relay"}
    with _client(monkeypatch) as c:
        assert c.get("/api/v1/team", headers=via).status_code == 403, "에이전트 경로는 로컬 전용 그대로"
        assert c.get("/api/owner/teams", headers=via).json()["teams"][0]["code"] == t["code"]
        assert c.get(f"/api/owner/teams/{t['code']}", headers=via).json()["name"] == "Web"
        r = c.post(f"/api/owner/teams/{t['code']}/members", json={"alias": "fe", "session_id": "s-a", "role": "UI"}, headers=via)
        assert r.status_code == 200 and r.json()["monitor_sync"] == "ok"
        assert mongroups.get(T.get_team(t["id"])["monitor_group_id"])["subs"] == ["s-a"]
        mgr = T.get_team(t["id"])["manager_agent"]
        assert c.post(f"/api/owner/teams/{t['code']}/members/{mgr}/leave", headers=via).status_code == 409
        fe = [m for m in T.get_team(t["id"])["members"] if m["alias"] == "fe"][0]["agent_id"]
        assert c.post(f"/api/owner/teams/{t['code']}/members/{fe}/leave", headers=via).status_code == 200
        r = c.post(f"/api/owner/teams/{t['code']}/notify", json={"notify": {"error": False}}, headers=via)
        assert r.json()["notify"]["error"] is False
        assert c.post(f"/api/owner/teams/{t['code']}/rename", json={"name": "웹"}, headers=via).json()["name"] == "웹"


def test_owner_approval_create_execute(T, monkeypatch):
    sent = []
    monkeypatch.setattr("session_manager.push.send", lambda *a, **k: sent.append(a))
    from session_manager import approvals
    with _client(monkeypatch) as c:
        a = c.post("/api/owner/approvals", json={"kind": "team_create", "args": {"name": "새팀", "manager_session": "s-m"}}).json()
        assert a["status"] == "pending" and a["requested_by"] == "owner" and not [x for x in sent if str(x[0]).startswith("approval:")],             "화면에서 낸 요청은 폰 알림 없이 바로 시트"
        assert c.post(f"/api/owner/approvals/{a['id']}/execute").status_code == 409, "승인 전 실행 불가"
        approvals.decide(a["id"], True, "otp")
        x = c.post(f"/api/owner/approvals/{a['id']}/execute").json()
        assert x["status"] == "executed" and x["result"]["code"]
