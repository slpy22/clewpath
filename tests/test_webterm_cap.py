"""서버측 터미널 PTY 상한 — v0.6.6 '세션 많은 PC 업데이트 사망' 재발 방지.

터미널 1개 = claude 프로세스 1개다. 멀티탭 뷰어가 무심코 수십 개를 띄우지 못하도록
새 스폰에만 상한을 건다. 재접속(이미 열린 세션에 다시 붙기)은 프로세스를 안 늘리니
절대 막지 않는다 — '연결 버튼은 항상 되어야 한다'는 상태 모델을 지킨다.
[[006-update-healthcheck-incident]]
"""
from __future__ import annotations

import threading

import pytest

from session_manager import webterm


def test_max_live_pty_env(monkeypatch):
    monkeypatch.delenv("SM_MAX_TERMINALS", raising=False)
    assert webterm._max_live_pty() == 8            # 기본값
    monkeypatch.setenv("SM_MAX_TERMINALS", "3")
    assert webterm._max_live_pty() == 3
    monkeypatch.setenv("SM_MAX_TERMINALS", "0")     # 0/음수/오타 → 기본값
    assert webterm._max_live_pty() == 8
    monkeypatch.setenv("SM_MAX_TERMINALS", "oops")
    assert webterm._max_live_pty() == 8


def test_live_count_counts_alive_and_reaps_dead(monkeypatch):
    # 살아있는 것 2개 + 시체 1개를 심고, 세는 김에 시체가 정리되는지 본다.
    class _Proc:
        def __init__(self, alive): self._a = alive
        def isalive(self): return self._a
        def terminate(self, force=False): pass
        pid = 1234
    monkeypatch.setattr(webterm, "_ACTIVE", {}, raising=True)
    monkeypatch.setattr(webterm, "_registry_remove", lambda pid: None)
    for k, alive in (("a", True), ("b", True), ("dead", False)):
        s = webterm._TermSession(k, _Proc(alive), persist=True)
        webterm._ACTIVE[k] = s
    assert webterm._live_count() == 2               # 살아있는 것만
    assert "dead" not in webterm._ACTIVE            # 세는 김에 시체 정리됨


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_text(self, s):
        self.sent.append(s)

    async def close(self):
        self.closed = True

    async def receive_text(self):
        # 펌프 루프를 즉시 빠져나가게 한다(재접속 테스트용)
        raise RuntimeError("disconnect")


@pytest.mark.asyncio
async def test_new_spawn_refused_at_cap(monkeypatch):
    import winpty
    # 새 스폰 경로 강제(재접속 대상 없음) + 상한 도달
    monkeypatch.setattr(webterm, "_get_live", lambda key: None)
    monkeypatch.setattr(webterm, "_live_count", lambda: 8)
    monkeypatch.delenv("SM_MAX_TERMINALS", raising=False)   # cap=8

    def _boom(*a, **k):
        raise AssertionError("상한인데 스폰됨!")
    monkeypatch.setattr(winpty.PtyProcess, "spawn", staticmethod(_boom))

    ws = _FakeWS()
    await webterm.run_terminal(ws, "sid-x")

    assert ws.closed is True
    assert any("상한" in s for s in ws.sent)         # 구조화된 안내
    # _boom 이 안 터졌다는 것 = 스폰 경로에 도달하지 않음(상한이 먼저 막음)


@pytest.mark.asyncio
async def test_reattach_bypasses_cap(monkeypatch):
    # 이미 열린(살아있는) 세션에 재접속하는 경로는 상한을 절대 안 탄다.
    class _Proc:
        def isalive(self): return True
        def write(self, d): pass
        def terminate(self, force=False): pass
        pid = 9
    sess = webterm._TermSession("sid-y", _Proc(), persist=True)
    sess.client = None                               # old 화면 없음 → _safe_close 안 함
    monkeypatch.setattr(webterm, "_get_live", lambda key: sess)

    called = {"cap": False}
    monkeypatch.setattr(webterm, "_live_count",
                        lambda: called.__setitem__("cap", True) or 0)

    ws = _FakeWS()
    # 펌프 루프는 receive_text 가 raise 하며 빠져나온다(에러 무시)
    try:
        await webterm.run_terminal(ws, "sid-y")
    except Exception:  # noqa: BLE001
        pass

    assert called["cap"] is False                    # 재접속은 상한 미검사
