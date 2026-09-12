"""관제 그룹 API — 릴레이 프록시(GET/POST 만) 호환 계약."""
from __future__ import annotations

M = "aaaaaaaa-1111-2222-3333-444444444444"
S1 = "bbbbbbbb-1111-2222-3333-444444444444"


def _client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    return TestClient(create_app())


def test_groups_crud_over_post_only(fake_claude_home, monkeypatch):
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.post("/api/owner/monitor/groups",
                   json={"name": "포털", "manager": M, "subs": [S1], "labels": {S1: "포털개발"}})
        assert r.status_code == 200
        g = r.json(); gid = g["id"]
        assert g["notify"]["manager_stop"] is True

        r = c.get("/api/owner/monitor/groups")
        assert [x["id"] for x in r.json()["groups"]] == [gid]

        r = c.post(f"/api/owner/monitor/groups/{gid}/notify", json={"notify": {"sub_stop": False}})
        assert r.status_code == 200 and r.json()["notify"]["sub_stop"] is False
        r = c.post("/api/owner/monitor/groups/missing/notify", json={"notify": {}})
        assert r.status_code == 404

        r = c.post(f"/api/owner/monitor/groups/{gid}/delete")
        assert r.json() == {"deleted": True}
        assert c.get("/api/owner/monitor/groups").json()["groups"] == []


def test_groups_validation(fake_claude_home, monkeypatch):
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.post("/api/owner/monitor/groups", json={"name": "x", "manager": "", "subs": []})
        assert r.status_code == 400 and r.json()["error"] == "manager_required"
