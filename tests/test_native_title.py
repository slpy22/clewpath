"""native_title — custom-title append(네이티브 rename) 및 실행중 가드 테스트."""
import json
import os

import pytest

from session_manager import scanner, native_title


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    scanner._CACHE.clear()  # 캐시 격리
    proj = tmp_path / "projects" / "C--work-demo"
    proj.mkdir(parents=True)
    sid = "11111111-2222-3333-4444-555555555555"
    jf = proj / f"{sid}.jsonl"
    with jf.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "cwd": "C:\\work\\demo",
                            "slug": "brave-flying-otter", "timestamp": "2026-07-28T00:00:00Z"}) + "\n")
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-07-28T00:01:00Z"}) + "\n")
    return tmp_path, sid, jf


def test_set_and_read_native_title(home):
    _, sid, jf = home
    r = native_title.set_native_title(sid, "북한법 파이프라인")
    assert r.get("ok") is True
    assert r["title"] == "북한법 파이프라인"

    # JSONL 끝 두 줄 중 앞 줄이 정확한 custom-title 레코드여야(뒤 줄은 agent-name)
    last = jf.read_text(encoding="utf-8").strip().splitlines()[-2]
    obj = json.loads(last)
    assert obj == {"type": "custom-title", "customTitle": "북한법 파이프라인",
                   "sessionId": sid}

    # 스캐너가 custom_title 로 읽어야
    scanner._CACHE.clear()
    meta = scanner.scan_one(sid)
    assert meta.custom_title == "북한법 파이프라인"


def test_latest_wins(home):
    _, sid, jf = home
    native_title.set_native_title(sid, "첫 이름")
    native_title.set_native_title(sid, "두번째 이름")
    scanner._CACHE.clear()
    assert scanner.scan_one(sid).custom_title == "두번째 이름"


def test_empty_and_not_found(home):
    _, sid, _ = home
    assert native_title.set_native_title(sid, "  ").get("error") == "empty_title"
    assert native_title.set_native_title("no-such-id", "x").get("error") == "not_found"


def test_busy_guard(home):
    root, sid, jf = home
    sess = root / "sessions"
    sess.mkdir()
    (sess / "9999.json").write_text(json.dumps(
        {"pid": 9999, "sessionId": sid, "status": "busy",
         "updatedAt": 1785132193600}), encoding="utf-8")

    # busy → 기본 차단
    r = native_title.set_native_title(sid, "차단테스트")
    assert r.get("error") == "session_live"
    # 기존 줄이 그대로여야(안 써짐)
    assert "차단테스트" not in jf.read_text(encoding="utf-8")

    # force → 강제 기록
    r2 = native_title.set_native_title(sid, "강제기록", allow_live=True)
    assert r2.get("ok") is True
    assert "강제기록" in jf.read_text(encoding="utf-8")


def _add(jf, rec):
    with jf.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_agent_name_follows_title_when_present(home):
    """2026-10-15 사장님 승인: 에이전트 이름이 있는 세션은 이름을 바꿀 때 agent-name 도 같이(피어 이름 = 목록 이름)."""
    _, sid, jf = home
    _add(jf, {"type": "custom-title", "customTitle": "개발관리에이전트", "sessionId": sid})
    _add(jf, {"type": "agent-name", "agentName": "개발관리에이전트", "sessionId": sid})
    scanner._CACHE.clear()
    r = native_title.set_native_title(sid, "북한백과PM")
    assert r["ok"] and r["agent_name"] == "북한백과PM"
    tail = [json.loads(x) for x in jf.read_text(encoding="utf-8").strip().splitlines()[-2:]]
    assert tail == [{"type": "custom-title", "customTitle": "북한백과PM", "sessionId": sid},
                    {"type": "agent-name", "agentName": "북한백과PM", "sessionId": sid}]
    m = scanner.scan_one(sid)
    assert m.custom_title == "북한백과PM" and m.agent_name == "북한백과PM"
    # 이미 같은 이름이면 agent-name 을 또 쓰지 않는다
    n = len(jf.read_text(encoding="utf-8").splitlines())
    native_title.set_native_title(sid, "북한백과PM")
    assert len(jf.read_text(encoding="utf-8").splitlines()) == n + 1


def test_agent_name_set_even_without_one(home):
    """이름 없는 세션에도 claude /rename 과 같은 짝으로 agent-name 을 남긴다(2026-10-15 사장님 결정 — 이름 붙이기가 목적)."""
    _, sid, jf = home
    r = native_title.set_native_title(sid, "새 이름")
    assert r["ok"] and r["agent_name"] == "새 이름"
    tail = [json.loads(x) for x in jf.read_text(encoding="utf-8").strip().splitlines()[-2:]]
    assert [t["type"] for t in tail] == ["custom-title", "agent-name"]
    assert scanner.scan_one(sid).agent_name == "새 이름"


def test_peer_name_conflict_and_restart_note(home):
    root, sid, jf = home
    _add(jf, {"type": "agent-name", "agentName": "옛이름", "sessionId": sid})
    scanner._CACHE.clear()
    sess = root / "sessions"; sess.mkdir()
    (sess / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "other", "name": "겹치는이름"}), encoding="utf-8")
    (sess / "2.json").write_text(json.dumps({"pid": 2, "sessionId": sid, "name": "옛이름", "status": "idle",
                                             "updatedAt": 1}), encoding="utf-8")
    from session_manager import peers
    peers._cache["at"] = 0   # 2초 캐시 무효화
    r = native_title.set_native_title(sid, "겹치는이름")
    assert r["ok"] and r.get("peer_name_after_restart") is True
    assert r.get("peer_name_conflict") is True


def test_agent_name_mismatch_list_and_sync(home, monkeypatch):
    """제목(custom-title)만 있고 통신 이름(agent-name)이 빈 세션을 찾아, 고른 세션에만 agent-name 한 줄을 덧붙인다
    (2026-10-19 사장님 승인). 제목은 다시 쓰지 않고, 생성 중(busy) 세션·자동 실행 기록·옛 줄은 건드리지 않는다."""
    tmp, sid, jf = home
    proj = jf.parent
    def mk(s, lines):
        p = proj / f"{s}.jsonl"
        p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), encoding="utf-8")
        return p
    base = [{"type": "permission-mode"}, {"type": "user", "cwd": "C:\work\demo", "entrypoint": "cli"}]
    with jf.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "custom-title", "customTitle": "북한법서비스", "sessionId": sid}, ensure_ascii=False) + "\n")
    busy = "22222222-2222-3333-4444-555555555555"
    mk(busy, base + [{"type": "custom-title", "customTitle": "바쁜세션", "sessionId": busy}])
    same = "33333333-2222-3333-4444-555555555555"
    mk(same, base + [{"type": "custom-title", "customTitle": "같음", "sessionId": same},
                     {"type": "agent-name", "agentName": "같음", "sessionId": same}])
    auto = "44444444-2222-3333-4444-555555555555"
    mk(auto, [{"type": "user", "cwd": "C:\work\demo", "entrypoint": "sdk-cli"},
              {"type": "custom-title", "customTitle": "자동", "sessionId": auto}])
    old = "55555555-2222-3333-4444-555555555555"
    mk(old, base + [{"type": "custom-title", "customTitle": "옛줄", "sessionId": old},
                    {"type": "continued-in", "continuedInSessionId": "x"}])
    monkeypatch.setattr(native_title, "session_status", lambda s: "busy" if s == busy else None)
    monkeypatch.setattr("session_manager.peers.snapshot", lambda *a, **k: [])
    lst = {x["session_id"]: x for x in native_title.agent_name_mismatches()}
    assert set(lst) == {sid, busy}, "같은 이름·자동 실행·옛 줄은 목록에 없다"
    assert lst[sid]["title"] == "북한법서비스" and lst[sid]["agent_name"] is None and not lst[sid]["conflict"]
    before = jf.read_text(encoding="utf-8")
    r = native_title.sync_agent_names([sid, busy, same])
    assert [d["session_id"] for d in r["done"]] == [sid]
    assert {s["session_id"]: s["reason"] for s in r["skipped"]} == {busy: "busy", same: "not_mismatched"}
    after = jf.read_text(encoding="utf-8")
    assert after.startswith(before), "기존 줄은 그대로 — 끝에 덧붙이기만"
    assert json.loads(after.splitlines()[-1]) == {"type": "agent-name", "agentName": "북한법서비스", "sessionId": sid}
    assert after.count('"custom-title"') == 1, "제목은 다시 쓰지 않는다"
    assert scanner.scan_one(sid).agent_name == "북한법서비스"
    assert sid not in {x["session_id"] for x in native_title.agent_name_mismatches()}
    monkeypatch.setattr("session_manager.peers.snapshot", lambda *a, **k: [{"name": "바쁜세션", "session_id": "other"}])
    assert native_title.agent_name_mismatches()[0]["conflict"] is True, "다른 세션이 이미 그 이름이면 표시"


def test_agent_name_sync_label_named_session(home, monkeypatch):
    """화면 이름이 ClewPath 라벨 이름에서만 오는 세션(claude 이름 없음)도 목록에 나오고, 맞추면 claude 이름과 통신 이름을
    함께 적는다 — 기존 이름 바꾸기와 같은 두 줄(2026-10-19 북한법률정보센터 관리 세션 사례)."""
    _, sid, jf = home
    monkeypatch.setattr(native_title, "session_status", lambda s: None)
    monkeypatch.setattr("session_manager.peers.snapshot", lambda *a, **k: [])
    monkeypatch.setattr("session_manager.labels.all_records", lambda: {sid: {"name": "북한법률정보센터"}})
    lst = native_title.agent_name_mismatches()
    assert [(x["session_id"], x["title"], x["source"]) for x in lst] == [(sid, "북한법률정보센터", "label")]
    assert native_title.sync_agent_names([sid])["done"][0]["name"] == "북한법률정보센터"
    tail = [json.loads(l) for l in jf.read_text(encoding="utf-8").splitlines()[-2:]]
    assert tail == [{"type": "custom-title", "customTitle": "북한법률정보센터", "sessionId": sid},
                    {"type": "agent-name", "agentName": "북한법률정보센터", "sessionId": sid}]
    m = scanner.scan_one(sid)
    assert m.custom_title == m.agent_name == "북한법률정보센터" and native_title.agent_name_mismatches() == []
