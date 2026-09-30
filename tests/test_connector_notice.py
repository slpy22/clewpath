"""0.11.0 커넥터: notice → 스트림 해체 순서(E-D5), 전송 실패에도 해체(E-D15), bye_device(B-4), pv=2."""
from __future__ import annotations

import asyncio

import pytest

from session_manager import connector as C, devices
from session_manager.connector import Connector, PROTOCOL_VERSION


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")


def test_protocol_version_bumped_for_notice():
    assert PROTOCOL_VERSION >= 2


@pytest.mark.asyncio
async def test_notice_sent_before_teardown_only_to_that_device():
    conn = _conn()
    sent = []

    async def _send(obj): sent.append(obj)
    conn.send = _send
    conn.authed = {"c1": {"id": "d1"}, "c2": {"id": "d1"}, "c3": {"id": "d2"}}
    cancelled = []

    class _T:
        def __init__(self, n): self.n = n
        def cancel(self): cancelled.append(self.n)
    conn.streams = {"r1": _T("r1"), "r9": _T("r9")}
    conn.req_cid = {"r1": "c1", "r9": "c3"}
    n = await conn.drop_device("d1", notice="device_removed")
    assert n == 2
    assert [(f["type"], f["cid"], f["kind"]) for f in sent] == [("notice", "c1", "device_removed"), ("notice", "c2", "device_removed")]
    assert cancelled == ["r1"], "다른 기기(c3)의 스트림은 무접촉"
    assert set(conn.authed) == {"c3"}
    # notice 없이 부르면 프레임 없음(bye_device·revoke 경로)
    sent.clear(); conn.authed = {"c1": {"id": "d1"}}
    assert await conn.drop_device("d1") == 1 and sent == []


@pytest.mark.asyncio
async def test_teardown_happens_even_if_notice_send_fails_or_hangs():
    conn = _conn()

    async def _boom(obj): raise RuntimeError("ws closed")
    conn.send = _boom
    conn.authed = {"c1": {"id": "d1"}}
    assert await conn.drop_device("d1", notice="device_reissued") == 1
    assert conn.authed == {}, "전송 예외에도 해체는 무조건(finally)"

    async def _hang(obj): await asyncio.sleep(5)
    conn.send = _hang
    conn.authed = {"c1": {"id": "d1"}}
    t0 = asyncio.get_event_loop().time()
    assert await conn.drop_device("d1", notice="device_removed") == 1
    assert asyncio.get_event_loop().time() - t0 < 2.0, "notice 전송은 1초 제한"
    assert conn.authed == {}


def test_request_drop_device_passes_notice_and_logs_failure(monkeypatch):
    conn = _conn()
    got = {}

    async def _drop(did, notice=None): got["args"] = (did, notice); return 3
    conn.drop_device = _drop
    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
    conn.loop = loop
    monkeypatch.setattr(C, "CURRENT", conn)
    logs = []
    monkeypatch.setattr(C, "_log", lambda m: logs.append(m))
    try:
        assert C.request_drop_device("d1", notice="device_removed") == 3
        assert got["args"] == ("d1", "device_removed")

        async def _fail(did, notice=None): raise RuntimeError("loop dead")
        conn.drop_device = _fail
        assert C.request_drop_device("d1") == 0
        assert any("drop_device" in m and "RuntimeError" in m for m in logs), "실패는 0 으로 숨지 않고 로그"
    finally:
        loop.call_soon_threadsafe(loop.stop); t.join(2)
    monkeypatch.setattr(C, "CURRENT", None)


@pytest.mark.asyncio
async def test_bye_device_marks_phone_revoked_and_drops(fake_claude_home):
    d = devices.add_device("폰")
    conn = _conn()
    seen = []

    async def _res(rid, ok, data=None, error=None): seen.append((rid, ok, data, error))
    conn._res = _res
    conn.authed = {"c1": {"id": d["id"], "name": "폰", "tver": devices.token_version(d["id"])}}
    conn.req_cid = {}
    await conn._handle_req({"v": 1, "type": "req", "id": "r1", "method": "bye_device", "params": {}, "cid": "c1"})
    assert seen[-1][:2] == ("r1", True) and seen[-1][2] == {"revoked": True}
    row = devices.list_devices()[0]
    assert row["revoked"] and row["revoked_by"] == "phone"
    assert conn.authed == {}, "자기 연결 해체"
    # 미인증 cid 는 거부(enforced 검사에서 auth_required)
    await conn._handle_req({"v": 1, "type": "req", "id": "r2", "method": "bye_device", "params": {}, "cid": "c9"})
    assert seen[-1][1] is False and seen[-1][3] == "auth_required"
