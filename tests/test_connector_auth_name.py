"""커넥터 auth(0.10.0, B-1/E-D9): Connector._auth 가 폰이 보고한 이름으로 임시 이름만 채우고 정규화한다."""
from __future__ import annotations

import pytest

from session_manager import devices
from session_manager.connector import Connector, PROTOCOL_VERSION


def _conn():
    return Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")


async def _auth(conn, params, cid="c1", rid="r1"):
    seen = []

    async def _res(rid_, ok, data=None, error=None):
        seen.append((rid_, ok, data, error))
    conn._res = _res
    await conn._handle_req({"v": 1, "type": "req", "id": rid, "method": "auth", "params": params, "cid": cid})
    return seen[-1]


@pytest.mark.asyncio
async def test_auth_reports_name_fills_auto_name_only(fake_claude_home, monkeypatch):
    monkeypatch.setattr("session_manager.appconfig.machine_name", lambda: "  MY-PC\x00 ")
    auto = devices.add_device("")
    named = devices.add_device("내 폰")
    conn = _conn()

    rid, ok, data, err = await _auth(conn, {"token": auto["token"], "name": " iPhone\x01 15  Pro ", "pv": PROTOCOL_VERSION})
    assert ok and data["name"] == "iPhone 15 Pro" and data["hostname"] == "MY-PC"
    assert conn.authed["c1"]["name"] == "iPhone 15 Pro"
    assert devices.list_devices()[0]["name"] == "iPhone 15 Pro"

    rid, ok, data, err = await _auth(conn, {"token": named["token"], "name": "Galaxy"}, cid="c2")
    assert ok and data["name"] == "내 폰", "사용자가 붙인 이름은 폰 보고로 안 바뀐다"

    # 구 폰(name 없음)도 그대로 통과
    rid, ok, data, err = await _auth(conn, {"token": named["token"]}, cid="c3")
    assert ok and data["name"] == "내 폰"


@pytest.mark.asyncio
async def test_auth_invalid_token_unchanged(fake_claude_home):
    conn = _conn()
    rid, ok, data, err = await _auth(conn, {"token": "dev-nope", "name": "x"})
    assert not ok and err == "auth_invalid" and "c1" not in conn.authed
