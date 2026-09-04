"""업데이트 안전성 회귀 가드 (2026-09-04 실사고 대응).

사고: 세션 많은 PC 에서 헬스 엔드포인트가 scan_all(전체 파싱) 로 느려 업데이트
헬스체크(45초)가 타임아웃 → 멀쩡한 새 버전이 롤백. 사용자가 재시도하자 runner
2개 동시 실행 → 포트 경합으로 서비스 완전 사망.

가드:
1. /api/owner/diagnostics 는 scan_all 을 부르지 않는다(빠른 파일 카운트).
2. updater.apply 는 5분 내 동시 재실행을 거부한다(잠금).
"""
from __future__ import annotations

import pytest

from tests.conftest import write_session


def _client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    return TestClient(create_app())


def test_diagnostics_does_not_call_scan_all(fake_claude_home, monkeypatch):
    # scan_all 을 부르면 폭발하도록 심어두고, diagnostics 가 여전히 정상이면
    # = scan_all 을 안 부른다는 증거(콜드 캐시에서 느려지지 않음).
    write_session(fake_claude_home, "F--p", "aaaaaaaa-1111-2222-3333-444444444444", [
        {"type": "user", "cwd": "F:/p", "message": {"role": "user", "content": "hi"},
         "timestamp": "t0"}])
    write_session(fake_claude_home, "F--p", "bbbbbbbb-1111-2222-3333-444444444444", [
        {"type": "user", "cwd": "F:/p", "message": {"role": "user", "content": "hi"},
         "timestamp": "t0"}])
    from session_manager import scanner
    monkeypatch.setattr(scanner, "scan_all",
                        lambda: (_ for _ in ()).throw(AssertionError("scan_all 호출됨!")))
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.get("/api/owner/diagnostics")
        assert r.status_code == 200
        j = r.json()
        assert j["session_count"] == 2      # 파일 카운트로 정확
        assert j["ok"] is True


def test_apply_concurrency_lock(fake_claude_home, monkeypatch):
    from session_manager import updater
    # 실제 runner 스폰은 막는다(부작용 없이 잠금 로직만 검증)
    calls = {"n": 0}

    class _FakePopen:
        def __init__(self, *a, **k):
            calls["n"] += 1
    monkeypatch.setattr(updater.subprocess, "Popen", _FakePopen)

    m = {"ver": "9.9.9"}
    staged = fake_claude_home / "stage"
    staged.mkdir()
    # 1회차: 정상 실행(잠금 생성)
    updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 1
    # 2회차(5분 내): 거부 → runner 재스폰 안 됨
    with pytest.raises(updater.UpdateError):
        updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 1     # 두 번째는 Popen 안 함

    # 잠금이 만료(5분 경과 시뮬)되면 다시 허용
    import os
    lock = updater.work_dir() / "apply.lock"
    old = os.stat(lock).st_mtime - 400
    os.utime(lock, (old, old))
    updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 2
