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
