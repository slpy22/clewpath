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

    # JSONL 마지막 줄이 정확한 custom-title 레코드여야
    last = jf.read_text(encoding="utf-8").strip().splitlines()[-1]
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


def test_no_agent_name_created_for_sessions_without_one(home):
    """agent-name 이 없던 세션엔 만들지 않는다 — 생기면 claude 재개 피커 표시 등 claude 동작이 바뀐다(불가침 원칙)."""
    _, sid, jf = home
    r = native_title.set_native_title(sid, "새 이름")
    assert r["ok"] and r["agent_name"] is None
    assert '"agent-name"' not in jf.read_text(encoding="utf-8")
    assert scanner.scan_one(sid).agent_name is None


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
