"""에이전트 기록 보관(agent-unit.md '에이전트 탭·세션 관리·기록 보관' H9, 판정 K12~K19). 원본은 읽기만."""
import json
from types import SimpleNamespace

import pytest

from session_manager import agent_archive, agent_history, config, scanner, team


def U(text):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": "2026-11-10T01:00:00Z"}


def line(r):
    return (json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8")


@pytest.fixture
def E(fake_claude_home, monkeypatch, tmp_path):
    known = {}
    src = tmp_path / "src"
    src.mkdir()

    def write(sid, data: bytes):
        p = src / f"{sid}.jsonl"
        p.write_bytes(data)
        known[sid] = SimpleNamespace(session_id=sid, headless=False, cwd=str(tmp_path), jsonl_path=str(p), last_model=None,
                                     custom_title=None, agent_name=None, ai_title=None, slug=None)
        return p
    monkeypatch.setattr(scanner, "scan_one", lambda sid: known.get(sid))
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    monkeypatch.setattr(agent_archive, "_free", lambda d: 10 ** 12)
    agent_history._cache.clear()
    write("s1", line(U("첫 요청 배포 규칙")))
    write("s2", line(U("둘째 세션")))
    write("s9", line(U("남의 세션")))
    a = team.create_agent("s1", "가")
    with team._Tx() as w:
        team.bind_in(w, a["id"], "s2", "replaced")
    team.create_agent("s9", "나")
    uid = team._read().execute("SELECT COALESCE(uid,id) AS u FROM agents WHERE id=?", (a["id"],)).fetchone()["u"]
    return SimpleNamespace(write=write, known=known, id=a["id"], arc=config.data_dir() / "agents" / uid / "archive", src=src)


def test_run_copies_agent_sessions_only(E):
    r = agent_archive.run()
    assert r["archived"] == 3 and r["updated"] == 3, "에이전트 둘의 세션 셋"
    assert (E.arc / "s1.jsonl").read_bytes() == (E.src / "s1.jsonl").read_bytes()
    assert agent_archive.run()["updated"] == 0, "그대로면 다시 쓰지 않는다"


def test_appends_only_new_complete_lines(E):
    agent_archive.run()
    first = (E.src / "s1.jsonl").read_bytes()
    E.write("s1", first + line(U("다음 요청")) + b'{"type":"user","mess')          # 쓰다 만 줄
    agent_archive.run()
    assert (E.arc / "s1.jsonl").read_bytes() == first + line(U("다음 요청")), "완전한 줄까지만"
    assert list(E.arc.glob("s1.*.jsonl")) == [], "이어 붙였으면 옛 판을 따로 남기지 않는다"


def test_rewritten_source_keeps_old_archive_version(E):
    agent_archive.run()
    old = (E.arc / "s1.jsonl").read_bytes()
    E.write("s1", line(U("다시 쓰인 파일")))
    agent_archive.run()
    assert (E.arc / "s1.jsonl").read_bytes() == line(U("다시 쓰인 파일"))
    kept = list(E.arc.glob("s1.*.jsonl"))
    assert len(kept) == 1 and kept[0].read_bytes() == old, "옛 보관본은 지우지 않고 남긴다"


def test_missing_source_keeps_archive(E):
    agent_archive.run()
    del E.known["s1"]
    r = agent_archive.run()
    assert r["missing"] == 1 and (E.arc / "s1.jsonl").is_file()


def test_low_disk_stops_without_deleting(E, monkeypatch):
    agent_archive.run()
    E.write("s1", (E.src / "s1.jsonl").read_bytes() + line(U("더")))
    monkeypatch.setattr(agent_archive, "_free", lambda d: 1)
    r = agent_archive.run()
    assert r["no_space"] is True and "2GB" in r["summary"] and (E.arc / "s1.jsonl").is_file()


def test_budget_defers_big_sessions(E):
    r = agent_archive.run(budget=1)
    assert r["skipped"] == 3 and r["archived"] == 0


def test_detached_session_is_not_archived(E):
    team.detach_session(E.id, "s2")
    agent_archive.run()
    assert not (E.arc / "s2.jsonl").exists()


def test_history_falls_back_to_archive_with_same_refs(E):
    agent_archive.run()
    ref = agent_history.search("s2", "배포")["items"][0]["ref"]
    del E.known["s1"]
    agent_history._cache.clear()
    r = agent_history.search("s2", "배포")
    assert r["items"][0]["ref"] == ref and r["items"][0]["source"] == "archive"
    assert agent_history.read("s2", ref)["source"] == "archive"


def test_archive_now_is_per_agent_and_reports_each(E):
    r = agent_archive.archive_now(E.id, ["s1", "s9", "zz"])["items"]
    assert r["s1"]["ok"] is True and r["s9"] == {"ok": False, "status": "not_this_agent"} and r["zz"]["ok"] is False
    del E.known["s1"]
    assert agent_archive.archive_now(E.id, ["s1"])["items"]["s1"] == {"ok": True, "status": "no_source"}, "원본은 없어도 보관본이 있으면 지워도 된다"


def test_info_reports_archive_and_original(E):
    agent_archive.run()
    del E.known["s2"]
    i = agent_archive.info(E.id)
    assert i["count"] == 2 and i["sessions"]["s2"] == {"archived": True, "bytes": i["sessions"]["s2"]["bytes"], "original": False}
    assert i["sessions"]["s1"]["original"] is True


def test_original_is_never_written(E):
    before = {s: (E.src / f"{s}.jsonl").read_bytes() for s in ("s1", "s2", "s9")}
    agent_archive.run()
    agent_archive.archive_now(E.id, ["s1", "s2"])
    assert before == {s: (E.src / f"{s}.jsonl").read_bytes() for s in ("s1", "s2", "s9")}


def test_owner_routes(E, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app(), base_url="http://127.0.0.1") as c:
        d = c.get(f"/api/owner/agents/{E.id}").json()
        assert d["archive"]["sessions"]["s1"]["original"] is True and d["archive"]["count"] == 0
        r = c.post(f"/api/owner/agents/{E.id}/archive-now", json={"session_ids": ["s1"]}).json()
        assert r["items"]["s1"]["ok"] is True
        hits = c.post(f"/api/owner/agents/{E.id}/history/search", json={"q": "배포"}).json()
        assert hits["items"][0]["ref"] == "s1:1" and all(s["session_id"] != "s9" for s in hits["sessions"])
        rd = c.post(f"/api/owner/agents/{E.id}/history/read", json={"ref": "s1:1"}).json()
        assert rd["items"][0]["target"] is True
        assert c.post(f"/api/owner/agents/{E.id}/history/read", json={"ref": "s9:1"}).json()["error"] == "not_this_agent"


def test_new_routes_are_in_outer_door_table():
    from session_manager import outer_door
    for p in ("/api/v1/agent-history/search", "/api/v1/agent-history/read", "/api/owner/agents/{agent_id}/history/search",
              "/api/owner/agents/{agent_id}/history/read", "/api/owner/agents/{agent_id}/archive-now"):
        assert p in outer_door.OPEN and p not in outer_door.GUARDED
