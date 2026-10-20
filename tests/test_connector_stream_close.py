"""connector rid 단위 stream_close — 배경 탭 detach 의 서버측 근거.

멀티 터미널 탭 뷰어의 전제: 릴레이 모드에서 클라이언트가 특정 스트림(터미널 탭)만
닫으면, 그 rid 의 로컬 WS 를 끊어 Host 가 화면을 detach 하게 한다(persist PTY 유지).
기존엔 client `close()` 가 클라 핸들러만 지우고 서버로 아무것도 안 보내, 릴레이
모드에서 서버 화면이 계속 붙어 있었다(누수). stream_close 프레임이 이를 메운다.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from session_manager.connector import Connector


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="home-manual",
                     token="rly-agt-x", local_base="http://127.0.0.1:5100")


class _BlockWS:
    """취소될 때까지 영원히 살아있는 로컬 WS 대역(터미널 persist 흉내)."""

    def __init__(self, closed_flag):
        self._closed = closed_flag

    def __aiter__(self):
        async def gen():
            await asyncio.Event().wait()   # 취소되기 전까지 블록
            yield  # 도달 안 함
        return gen()

    async def send(self, m):
        pass


class _BlockCM:
    def __init__(self, closed_flag):
        self._closed = closed_flag

    async def __aenter__(self):
        return _BlockWS(self._closed)

    async def __aexit__(self, *a):
        self._closed["v"] = True           # async with 해제 = 로컬 WS 닫힘
        return False


@pytest.mark.asyncio
async def test_stream_close_cancels_pipe_and_detaches(monkeypatch):
    conn = _conn()
    closed = {"v": False}
    monkeypatch.setattr("session_manager.connector.connect",
                        lambda url, **kw: _BlockCM(closed))

    # 터미널 파이프 시작(살아있는 로컬 WS 에 붙어 대기)
    conn.stream_in["rT"] = asyncio.Queue()
    conn.streams["rT"] = asyncio.create_task(
        conn._pipe_terminal("rT", "ws://127.0.0.1:5100/ws/terminal/x"))
    task = conn.streams["rT"]
    for _ in range(3):
        await asyncio.sleep(0)             # 파이프가 connect 진입해 대기하도록

    # 클라이언트가 이 rid 만 닫는다(다른 rid·기기엔 영향 없음)
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "rT"}))

    # 파이프 태스크가 취소되고, 로컬 WS 가 닫히고, 등록부가 정리된다
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed["v"] is True             # async with connect 해제 → 로컬 WS close
    assert "rT" not in conn.streams        # finally 정리
    assert "rT" not in conn.stream_in


@pytest.mark.asyncio
async def test_stream_close_unknown_rid_is_noop():
    conn = _conn()
    # 없는 rid 를 닫아도 예외 없이 조용히 무시(다른 스트림 영향 없음)
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "nope"}))
    assert conn.streams == {}


@pytest.mark.asyncio
async def test_stream_close_only_targets_its_rid(monkeypatch):
    conn = _conn()
    closedA, closedB = {"v": False}, {"v": False}

    def fake_connect(url, **kw):
        return _BlockCM(closedB if url.endswith("/B") else closedA)
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)

    for rid, suffix in (("rA", "/A"), ("rB", "/B")):
        conn.stream_in[rid] = asyncio.Queue()
        conn.streams[rid] = asyncio.create_task(
            conn._pipe_terminal(rid, f"ws://127.0.0.1:5100/ws/terminal{suffix}"))
    taskA = conn.streams["rA"]
    taskB = conn.streams["rB"]
    for _ in range(3):
        await asyncio.sleep(0)

    # rA 만 닫는다 — rB 는 계속 살아있어야 한다
    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "rA"}))
    with pytest.raises(asyncio.CancelledError):
        await taskA
    assert "rA" not in conn.streams
    assert "rB" in conn.streams and not taskB.done()   # 다른 탭 무영향
    assert closedB["v"] is False

    taskB.cancel()                          # 정리
    with pytest.raises(asyncio.CancelledError):
        await taskB


@pytest.mark.asyncio
async def test_stream_close_ends_inplace_resume_pipe(monkeypatch):
    """웹 재개(in-place, 폰은 2차 인증 유예로 연다)도 stream_close 로 그 파이프를 끊는다.

    로컬 WS 가 닫히면 Host 가 claude -p 를 끝낸다(webapi.run_resume_api 의 finally). PWA 의 릴레이 재개
    close 가 앱 쪽 핸들러만 지우던 때는 릴레이가 끊길 때까지 claude -p 가 남았다(quick-switch 1-2)."""
    conn = _conn()
    closed = {"v": False}
    urls = []

    def fake_connect(url, **kw):
        urls.append(url)
        return _BlockCM(closed)
    monkeypatch.setattr("session_manager.connector.connect", fake_connect)
    monkeypatch.setattr("session_manager.owner2fa.required", lambda: True)
    monkeypatch.setattr("session_manager.owner2fa.verify_grace", lambda g: g == "g-ok")
    monkeypatch.setattr("session_manager.owner2fa.verify", lambda o: False)

    await conn._start_resume("s7", {"session_id": "abc", "inplace": True, "grace": "g-ok", "skip": True})
    task = conn.streams["s7"]
    for _ in range(3):
        await asyncio.sleep(0)
    assert urls and "/api/v1/resume-inplace/abc?" in urls[0]

    await conn._on_frame(json.dumps({"v": 1, "type": "stream_close", "id": "s7"}))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)    # 못 끊으면 매달리지 않고 TimeoutError 로 실패
    assert closed["v"] is True             # 로컬 재개 WS 닫힘 → Host 가 claude -p 종료
    assert "s7" not in conn.streams and "s7" not in conn.stream_in
