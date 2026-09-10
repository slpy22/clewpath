"""터미널 stop 엔드포인트의 2FA 게이트 — start 와의 비대칭 갭 차단.

터미널 start(_start_terminal)는 원격 경유 시 2FA 를 요구하는데, 종료(stop)는 제네릭
/api/* 프록시로 새어 OTP 없이 남의 claude 프로세스를 죽일 수 있었다(외부 리뷰 발견).
이 테스트는 stop 이 특권 경로로 취급돼 2FA 를 요구하되, 2FA off 이거나 특권이 아닌
경로는 그대로 통과함을 검증한다.
"""
from __future__ import annotations

import pytest

from session_manager import connector as C
from session_manager.connector import Connector


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="home-manual",
                     token="rly-agt-x", local_base="http://127.0.0.1:5100")


STOP = "/api/sessions/59f9577b-0d25-47be-994b-29009cf0fba3/terminal/stop"


def test_is_privileged_api_matches_only_stop():
    assert C._is_privileged_api(STOP) is True
    assert C._is_privileged_api(STOP + "/") is True
    assert C._is_privileged_api(STOP + "?x=1") is True
    assert C._is_privileged_api("/api/sessions/abc/stats") is False
    assert C._is_privileged_api("/api/v1/sessions") is False


@pytest.mark.asyncio
async def test_stop_refused_without_2fa(monkeypatch):
    conn = _conn()
    # 2FA 필수 + 자격 없음
    monkeypatch.setattr("session_manager.owner2fa.required", lambda: True)
    monkeypatch.setattr("session_manager.owner2fa.verify_grace", lambda g: False)
    monkeypatch.setattr("session_manager.owner2fa.verify", lambda o: False)
    monkeypatch.setattr("session_manager.owner2fa.enabled", lambda: True)

    posted = {"n": 0}

    async def _no_post(*a, **k):
        posted["n"] += 1
    monkeypatch.setattr(conn.http, "post", _no_post)

    seen = []

    async def _res(rid, ok, data=None, error=None):
        seen.append((ok, error))
    monkeypatch.setattr(conn, "_res", _res)

    await conn._handle_api("r1", {"verb": "POST", "path": STOP})   # grace/otp 없음
    assert seen and seen[0][0] is False
    assert seen[0][1] in ("2fa_required", "2fa_invalid")
    assert posted["n"] == 0                       # 프록시로 안 나감(프로세스 안 죽음)


@pytest.mark.asyncio
async def test_stop_allowed_when_2fa_off(monkeypatch):
    conn = _conn()
    # 2FA off(사용자 선택) → 코드 없이 허용, 그대로 프록시
    monkeypatch.setattr("session_manager.owner2fa.required", lambda: False)

    class _Resp:
        status_code = 200
        def json(self): return {"ok": True}
    posted = {"n": 0}

    async def _post(url, **k):
        posted["n"] += 1
        return _Resp()
    monkeypatch.setattr(conn.http, "post", _post)

    seen = []

    async def _res(rid, ok, data=None, error=None):
        seen.append((ok, data, error))
    monkeypatch.setattr(conn, "_res", _res)

    await conn._handle_api("r2", {"verb": "POST", "path": STOP})
    assert posted["n"] == 1                       # 프록시 통과
    assert seen and seen[0][0] is True


@pytest.mark.asyncio
async def test_nonprivileged_post_not_gated(monkeypatch):
    conn = _conn()
    # 2FA 필수여도, 특권 경로가 아니면 게이트 안 함
    monkeypatch.setattr("session_manager.owner2fa.required", lambda: True)

    class _Resp:
        status_code = 200
        def json(self): return {"ok": True}
    posted = {"n": 0}

    async def _post(url, **k):
        posted["n"] += 1
        return _Resp()
    monkeypatch.setattr(conn.http, "post", _post)

    async def _res(rid, ok, data=None, error=None):
        pass
    monkeypatch.setattr(conn, "_res", _res)

    await conn._handle_api("r3", {"verb": "POST", "path": "/api/sessions/abc/rename"})
    assert posted["n"] == 1                       # 특권 아님 → 2FA 없이 통과
