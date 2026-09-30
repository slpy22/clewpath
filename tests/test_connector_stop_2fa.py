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
    assert C._is_privileged_api("/api/sessions/x/kill") is True   # 강제 종료도 특권(2026-09-30)
    assert C._is_privileged_api("/api/owner/update/apply") is True   # 원격 업데이트 적용도 특권(2FA)
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


# ---- 2FA on 상태의 OTP 실경로(미검증 소화 2026-09-28): grant → grace → 터미널 start 통과/거부 ----
# 이 PC Host 는 2FA off 라 실측이 없었다. 진짜 TOTP 비밀로 owner2fa 를 돌려 커넥터 디스패치까지 검증.

@pytest.mark.asyncio
async def test_grant_then_grace_gates_terminal_start(monkeypatch):
    import time
    from session_manager import owner2fa
    secret = "JBSWY3DPEHPK3PXP"
    monkeypatch.setattr(owner2fa, "_data", lambda: {"secret": secret, "enforced": True})
    monkeypatch.setattr(owner2fa, "_save", lambda d: None)
    # 기기 페어링 계층(devices.enforced)은 별도 테스트 몫 — 여기선 2FA 계층만 본다
    monkeypatch.setattr("session_manager.devices.enforced", lambda: False)
    conn = _conn()
    res, streams = [], []

    async def _res(rid, ok, data=None, error=None):
        res.append((rid, ok, data, error))
    async def _stream_send(rid, **kw):
        streams.append((rid, kw))
    async def _pipe(rid, url):
        streams.append((rid, {"pipe": url}))
    monkeypatch.setattr(conn, "_res", _res)
    monkeypatch.setattr(conn, "_stream_send", _stream_send)
    monkeypatch.setattr(conn, "_pipe_terminal", _pipe)

    sid = "59f9577b-0d25-47be-994b-29009cf0fba3"
    # 자격 없이 터미널 → eof + 2fa_invalid(비밀은 있음)
    await conn._handle_req({"id": "t0", "method": "terminal", "params": {"session_id": sid}})
    assert streams[-1] == ("t0", {"eof": True, "error": "2fa_invalid"})
    # 틀린 OTP → grant 거부
    await conn._handle_req({"id": "g0", "method": "grant", "params": {"otp": "000000"}})
    assert res[-1][1] is False and res[-1][3] == "2fa_invalid"
    # 맞는 OTP → grace 발급
    code = owner2fa._hotp(secret, int(time.time()) // 30)
    await conn._handle_req({"id": "g1", "method": "grant", "params": {"otp": code}})
    assert res[-1][1] is True and res[-1][2]["grace"]
    grace = res[-1][2]["grace"]
    # grace 로 터미널 start 통과(screen 커서까지 URL 에 실림) / stop 도 통과
    await conn._handle_req({"id": "t1", "method": "terminal", "params": {"session_id": sid, "grace": grace, "screen": "S1"}})
    import asyncio
    await asyncio.sleep(0)                      # create_task 된 파이프가 한 틱 돌게
    assert streams[-1][0] == "t1" and "screen=S1" in streams[-1][1]["pipe"]
    conn.streams["t1"].cancel()
    assert C._priv_ok({"grace": grace}) and not C._priv_ok({"grace": grace[:-2] + "xx"})
    # 비밀 재설정 → 기존 grace 전부 무효(다시 물어야 한다)
    monkeypatch.setattr(owner2fa, "_data", lambda: {"secret": "GEZDGNBVGY3TQOJQ", "enforced": True})
    assert not C._priv_ok({"grace": grace})
