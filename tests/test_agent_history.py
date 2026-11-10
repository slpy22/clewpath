"""에이전트 지난 세션 찾기·읽기(agent-unit.md '인계 연속성 보강' H5, 판정 K6~K10). claude 기록은 읽기만."""
import json
from types import SimpleNamespace

import pytest

from session_manager import agent_history, scanner, team, worklog

SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"


def U(text, ts="2026-11-10T01:00:00Z"):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts}


def A(text, ts="2026-11-10T01:00:01Z"):
    return {"type": "assistant", "message": {"model": "claude-x", "content": [{"type": "text", "text": text}]}, "timestamp": ts}


def TOOL(name="Edit", fp="F:/p/a.py"):
    return {"type": "assistant", "message": {"model": "claude-x", "content": [{"type": "tool_use", "name": name, "input": {"file_path": fp}}]}}


def RESULT(text):
    return {"type": "user", "toolUseResult": {}, "message": {"content": [{"type": "tool_result", "content": text}]}}


@pytest.fixture
def E(fake_claude_home, monkeypatch, tmp_path):
    known = {}

    def write(sid, recs):
        p = tmp_path / f"{sid}.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        known[sid] = SimpleNamespace(session_id=sid, headless=False, cwd=str(tmp_path), jsonl_path=str(p), last_model=None,
                                     custom_title=None, agent_name=None, ai_title=None, slug=None)
        return p
    monkeypatch.setattr(scanner, "scan_one", lambda sid: known.get(sid))
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    agent_history._cache.clear()
    write("s1", [U("배포 규칙: 운영 반영은 서비스 모니터에 의뢰"), RESULT("배포 규칙 도구 출력"), A("알겠습니다. 배포는 의뢰로만 합니다.")])
    write("s2", [U("다음은 검색 도구"), A(f"키 {SECRET} 를 썼습니다")])
    write("s9", [U("남의 에이전트 배포 이야기")])
    a = team.create_agent("s1", "가")
    with team._Tx() as w:
        team.bind_in(w, a["id"], "s2", "replaced")
    team.create_agent("s9", "나")
    return SimpleNamespace(write=write, known=known, id=a["id"], tmp=tmp_path)


def _err(fn, *a, **kw):
    with pytest.raises(team.TeamError) as e:
        fn(*a, **kw)
    return e.value


def test_search_spans_agent_sessions_newest_first_and_skips_tool_output(E):
    r = agent_history.search("s2", "배포")
    who = [(i["session_id"], i["who"]) for i in r["items"]]
    assert who == [("s1", "Claude"), ("s1", "사장님")], "도구 출력·다른 에이전트 글은 빠지고, 최근 글부터"
    assert r["items"][0]["ref"] == "s1:2" and r["agent"] == "가"
    assert [s["current"] for s in r["sessions"]] == [False, True], "지금 세션도 범위에 든다(압축 전 앞부분)"


def test_all_words_must_match(E):
    assert len(agent_history.search("s2", "배포 의뢰")["items"]) == 2
    r = agent_history.search("s2", "배포 없는낱말")
    assert r["items"] == [] and "낱말을 줄이거나" in r["hint"]


@pytest.mark.parametrize("caller,code", [(None, "caller_unknown"), ("zz", "not_an_agent")])
def test_unknown_or_unbound_caller_is_refused(E, caller, code):
    assert _err(agent_history.search, caller, "배포").code == code


def test_other_agents_sessions_are_out_of_scope(E):
    assert _err(agent_history.search, "s2", "배포", session_id="s9").code == "not_this_agent"
    assert _err(agent_history.read, "s2", "s9:1").code == "not_this_agent"
    assert all(i["session_id"] != "s9" for i in agent_history.search("s2", "배포")["items"])


def test_detached_session_cannot_search(E):
    team.detach_session(E.id, "s2")
    assert _err(agent_history.search, "s2", "배포").code == "not_an_agent"


def test_secrets_are_masked(E):
    r = agent_history.search("s2", "썼습니다")
    assert "abcdefghijklmnop" not in json.dumps(r, ensure_ascii=False)
    assert "abcdefghijklmnop" not in json.dumps(agent_history.read("s2", r["items"][0]["ref"]), ensure_ascii=False)


def test_read_centers_on_match_and_budgets_neighbours(E, monkeypatch):
    long = "가" * 9000 + "찾을말" + "나" * 9000
    E.write("s2", [U("앞 글"), A(long), U("뒤 글 " + "다" * 3000)])
    agent_history._cache.clear()
    hit = agent_history.search("s2", "찾을말")["items"][0]
    r = agent_history.read("s2", hit["ref"], before=1, after=1, at=hit["at"])
    tgt = next(i for i in r["items"] if i["target"])
    assert "찾을말" in tgt["text"] and len(tgt["text"]) == agent_history.TARGET_CHARS and tgt["len"] == len(long)
    assert [i["who"] for i in r["items"]] == ["사장님", "Claude", "사장님"] and r["truncated"] is True
    assert sum(len(i["text"]) for i in r["items"]) <= agent_history.READ_CHARS
    assert r["prev"] is None and r["next"] is None


def test_read_caps_and_paging(E):
    E.write("s2", [U(f"글 {i}") for i in range(30)])
    agent_history._cache.clear()
    r = agent_history.read("s2", "s2:15", before=99, after=99)
    assert len(r["items"]) == 1 + 2 * agent_history.AROUND_MAX
    assert r["prev"] == "s2:4" and r["next"] == "s2:26"
    assert _err(agent_history.read, "s2", "s2:999").code == "ref_not_found"


def test_search_result_budget(E, monkeypatch):
    E.write("s2", [U("배포 " + "가" * 400) for _ in range(60)])
    agent_history._cache.clear()
    r = agent_history.search("s2", "배포", limit=999)
    assert len(r["items"]) <= agent_history.LIMIT_MAX and r["truncated"] is True
    assert sum(len(i["snippet"]) for i in r["items"]) <= agent_history.SEARCH_CHARS


def test_falls_back_to_ledger_when_record_is_gone(E, monkeypatch):
    del E.known["s1"]
    monkeypatch.setattr(worklog, "session_turns", lambda sid, off, lim: {"turns": [{"n": 3, "ts": 1, "request": "배포 규칙 다시", "result": "의뢰로만"}]} if off == 0 else {"turns": []})
    r = agent_history.search("s2", "배포")
    assert r["items"][0]["ref"] == "s1:t3" and r["items"][0]["source"] == "ledger" and "ledger_note" in r
    rd = agent_history.read("s2", "s1:t3")
    assert rd["source"] == "ledger" and "원문 일부" in rd["items"][0]["text"] and "ledger_note" in rd


def test_cache_follows_file_changes(E):
    assert len(agent_history.search("s2", "새글")["items"]) == 0
    E.write("s2", [U("새글 하나")])
    assert len(agent_history.search("s2", "새글")["items"]) == 1


def test_routes_use_identified_caller(E, monkeypatch):
    from fastapi.testclient import TestClient
    from session_manager import outer_door
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    monkeypatch.setattr(outer_door, "identify", lambda *a: ("claude", "s2"))
    with TestClient(S.create_app(), base_url="http://127.0.0.1") as c:
        r = c.get("/api/v1/agent-history/search", params={"q": "배포"})
        assert r.status_code == 200 and r.json()["items"][0]["ref"] == "s1:2"
        assert c.get("/api/v1/agent-history/read", params={"ref": "s1:2", "at": 0}).json()["items"]
        monkeypatch.setattr(outer_door, "identify", lambda *a: ("other", "no_claude_ancestor"))
        r = c.get("/api/v1/agent-history/search", params={"q": "배포"})
        assert (r.status_code, r.json()["error"]) == (403, "caller_unknown")


def test_record_files_are_never_written(E):
    before = {sid: open(m.jsonl_path, "rb").read() for sid, m in E.known.items()}
    agent_history.search("s2", "배포")
    agent_history.read("s2", "s1:1")
    assert before == {sid: open(m.jsonl_path, "rb").read() for sid, m in E.known.items()}
