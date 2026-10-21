"""주기 업데이트 감시(server._update_watch) — 방금 롤백된 버전을 auto_apply 가 다시 적용하지 않는다(2회차 검토 R1).

재기동 자동 적용(apply_on_restart)만 recently_failed 를 보고 주기 루프의 auto_apply 분기는 보지 않아,
롤백 → 옛 Host 기동 → 20초 뒤 주기 루프 첫 바퀴가 같은 버전을 다시 받아 적용 → 또 롤백 … 이 끝없이 돌았다.
"""
from __future__ import annotations

import asyncio

import pytest

from session_manager import appconfig, server as S, updater


class _Stop(Exception):
    pass


def _run_watch(monkeypatch, *, auto: bool, on_restart: bool, failed: bool) -> list[str]:
    """감시를 첫 주기 바퀴까지만 돌리고, 적용을 시도한 버전 목록을 돌려준다."""
    applied: list[str] = []
    bools = {"auto_apply": auto, "apply_on_restart": on_restart}
    monkeypatch.setattr(appconfig, "get_bool", lambda sec, key, default=False: bools.get(key, default))
    monkeypatch.setattr(appconfig, "get_int", lambda sec, key, default=0: default)
    monkeypatch.setattr(updater, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(updater, "check", lambda: {"available": True, "latest": "9.9.9", "current": "9.9.8"})
    monkeypatch.setattr(updater, "recently_failed", lambda v: failed and v == "9.9.9")
    monkeypatch.setattr(updater, "check_and_stage", lambda: {"staged": "/tmp/x", "manifest": "tok"})
    monkeypatch.setattr(updater, "verify_manifest", lambda tok: {"ver": "9.9.9"})
    monkeypatch.setattr(updater, "apply", lambda m, staged: applied.append(m["ver"]))

    sleeps: list[float] = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 2:          # 기동 20초 대기 다음의 주기 대기 = 첫 바퀴가 끝났다
            raise _Stop

    monkeypatch.setattr(S.asyncio, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        asyncio.run(S._update_watch())
    return applied


def test_auto_apply_skips_a_version_that_just_rolled_back(monkeypatch):
    assert _run_watch(monkeypatch, auto=True, on_restart=False, failed=True) == []


def test_auto_apply_skips_it_after_the_restart_check_skipped_it(monkeypatch):
    # 실제 사고 모양: 재기동 확인이 건너뛴 직후 주기 루프 첫 바퀴가 같은 버전을 다시 잡았다
    assert _run_watch(monkeypatch, auto=True, on_restart=True, failed=True) == []


def test_auto_apply_still_applies_a_fresh_version(monkeypatch):
    assert _run_watch(monkeypatch, auto=True, on_restart=False, failed=False) == ["9.9.9"]


def test_notify_only_never_applies(monkeypatch):
    assert _run_watch(monkeypatch, auto=False, on_restart=False, failed=False) == []
