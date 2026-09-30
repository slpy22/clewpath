"""페어링 e2e 게이트(설계 6-1 / eng E-D11): **실제 CP** 를 상대로 Host 쪽 흐름을 인프로세스로 돈다.

돌리는 조건: 환경변수 `SM_E2E_CP_URL`(예: https://clewpath.pyongso.com/cp). 없으면 전부 skip.
정리: `SM_E2E_ADMIN_TOKEN` 이 있으면 finally 에서 커넥터를 revoke(임시 room 소멸). 없으면 이름 접두 `e2e-` 로 남는다.
기본 pytest 는 `-m "not e2e"`(pyproject addopts) 라 제외되고, release.ps1 이 환경변수가 있을 때 `-m e2e` 로 따로 돌린다.

흐름: 커넥터 발급 → 기기 등록+cpub 발급 → 폰 JWT 교환 OK → 폰 self-revoke(/client/revoke-self)
      → devsync 가 📵 반영·감사 → PC 삭제(폐기 확인 → 행 제거) → 재발급(새 cpub 활성, 옛 cpub 폐기)
      → 커넥터 notice→해체 순서(인프로세스) → 프록시 로컬 전용 403.
"""
from __future__ import annotations

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.e2e

CP = (os.environ.get("SM_E2E_CP_URL") or "").rstrip("/")
ADMIN = os.environ.get("SM_E2E_ADMIN_TOKEN") or ""

if not CP:
    pytest.skip("SM_E2E_CP_URL 없음 — 실 CP e2e 는 릴리스 게이트에서만", allow_module_level=True)


@pytest.fixture
def cp_conn(monkeypatch, fake_claude_home):
    """실 CP 에 임시 커넥터(room)를 만들고 cp_client 가 그 자격을 쓰게 한다."""
    r = httpx.post(f"{CP}/provision/anonymous", json={"display_name": f"e2e-{int(time.time())}"}, timeout=15)
    assert r.status_code == 200, r.text
    p = r.json()
    creds = {"credential_public_id": p["credential_public_id"], "secret": p["secret"], "connector_id": p["connector_id"]}
    from session_manager import cp_client
    monkeypatch.setenv("SM_CP_URL", CP)
    monkeypatch.setattr(cp_client, "load_creds", lambda: creds)
    try:
        yield p
    finally:
        if ADMIN:
            try:
                httpx.post(f"{CP}/admin/connectors/{p['connector_id']}/revoke", headers={"X-Admin-Token": ADMIN}, timeout=15)
            except Exception:  # noqa: BLE001
                pass


def _status(p, ids):
    r = httpx.post(f"{CP}/client/status", json={"public_id": p["credential_public_id"], "secret": p["secret"], "public_ids": ids}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["statuses"]


def test_pairing_flow_against_real_cp(cp_conn, monkeypatch):
    from session_manager import devices, devsync, cp_client, push
    p = cp_conn
    pushes = []
    monkeypatch.setattr(push, "send", lambda kind, sid, title, body, extra=None: pushes.append(title) or 1)

    # 1) 기기 등록 + cpub 발급(재발급 순서와 같은 경로)
    d = devices.add_device("")
    cc = cp_client.issue_client_credential(device_id=d["id"], name="e2e-phone")
    assert cc and cc["client_public_id"].startswith("cpub")
    devices.set_client_public_id(d["id"], cc["client_public_id"])
    # 2) 폰: secret → JWT
    r = httpx.post(f"{CP}/client/token", json={"public_id": cc["client_public_id"], "secret": cc["client_secret"]}, timeout=15)
    assert r.status_code == 200 and r.json().get("token")
    # 3) 폰 self-revoke(오프라인 큐 경로 = revoke-self 만)
    r = httpx.post(f"{CP}/client/revoke-self", json={"public_id": cc["client_public_id"], "secret": cc["client_secret"]}, timeout=15)
    assert r.status_code == 200 and r.json()["revoked"] is True
    assert httpx.post(f"{CP}/client/token", json={"public_id": cc["client_public_id"], "secret": cc["client_secret"]}, timeout=15).status_code == 401
    # 4) Host devsync → 📵 + 감사 + 웹푸시
    res = devsync.sync_once(notify=devsync._push_phone_revoked)
    assert res and res["phone_revoked"] == [d["id"]]
    row = devices.list_devices()[0]
    assert row["revoked"] and row["revoked_by"] == "phone"
    assert any(e["event"] == "self_revoke" for e in devices.audit_tail(10))
    assert pushes and "페어링을 해제함" in pushes[-1]
    # 5) PC 삭제: 이미 폐기된 cpub → revoke False(불명) → pending 으로 남고 devsync 가 status(revoked) 확인 후 제거
    assert cp_client.revoke_client_credential(cc["client_public_id"]) is False
    devices.set_delete_pending(d["id"]); devices.add_cp_pending(d["id"], "revoke:" + cc["client_public_id"])
    res = devsync.sync_once()
    assert d["id"] in res["removed"] and devices.list_devices() == []
    # 6) 재발급 순서: 새 발급 → 옛 폐기, CP 상태로 확인
    d2 = devices.add_device("second")
    old = cp_client.issue_client_credential(device_id=d2["id"], name="old"); devices.set_client_public_id(d2["id"], old["client_public_id"])
    new = cp_client.issue_client_credential(device_id=d2["id"], name="new")
    assert new and cp_client.revoke_client_credential(old["client_public_id"]) is True
    devices.set_client_public_id(d2["id"], new["client_public_id"])
    st = _status(p, [old["client_public_id"], new["client_public_id"]])
    assert st[old["client_public_id"]]["status"] == "revoked" and st[new["client_public_id"]]["status"] == "active"
    # 7) 남의 cpub 은 status 에 안 보인다(소유권 경계) — 다른 커넥터로 조회
    other = httpx.post(f"{CP}/provision/anonymous", json={"display_name": f"e2e-other-{int(time.time())}"}, timeout=15).json()
    if "credential_public_id" in other:      # 같은 IP 하루 한도에 걸리면 이 검사는 건너뛴다
        assert _status(other, [new["client_public_id"]]) == {}
        if ADMIN:
            httpx.post(f"{CP}/admin/connectors/{other['connector_id']}/revoke", headers={"X-Admin-Token": ADMIN}, timeout=15)


@pytest.mark.asyncio
async def test_connector_notice_then_drop_in_process(fake_claude_home):
    """8) notice → 해체 순서와 프록시 로컬 전용 403 은 인프로세스로 확인(릴레이 불필요)."""
    from session_manager.connector import Connector, _is_local_only_api
    conn = Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")
    sent = []

    async def _send(o): sent.append(o)
    conn.send = _send
    conn.authed = {"c1": {"id": "d1"}}
    assert await conn.drop_device("d1", notice="device_removed") == 1
    assert sent[0]["type"] == "notice" and conn.authed == {}
    assert _is_local_only_api("/api/owner/devices/d1/delete", "POST") is True
