"""PC 등급 기기(pclink 짝) — 접속 토큰이 없어 auth 로 인증될 수 없고, 재발급도 안 된다. 폰 기기는 그대로."""
from __future__ import annotations

from session_manager import approvals, devices


def test_pc_device_cannot_authenticate(fake_claude_home):
    did = devices.add_pc_device("노트북", "0f1e2d3c4b5a6978")
    row = next(x for x in devices.list_devices() if x["id"] == did)
    assert row["kind"] == "pc" and row["name"] == "노트북" and not row["revoked"]
    assert devices.is_active(did)
    assert devices.verify(None) is None
    assert devices.rotate_token(did) is None                 # PC 짝에는 접속 토큰을 주지 않는다
    assert devices.token_version(did) == 0


def test_phone_device_unchanged(fake_claude_home):
    r = devices.add_device("폰")
    v = devices.verify(r["token"])
    assert v == {"id": r["id"], "name": "폰", "kind": "phone"}
    assert next(x for x in devices.list_devices() if x["id"] == r["id"])["kind"] == "phone"
    assert devices.rotate_token(r["id"])


def test_pc_device_cred_link_and_remove(fake_claude_home):
    did = devices.add_pc_device("", "0f1e2d3c4b5a6978")
    assert devices.set_client_public_id(did, "cpub-1")
    assert devices.get_client_public_id(did) == "cpub-1"
    assert next(x for x in devices.list_devices() if x["id"] == did)["name"] == "PC"
    assert devices.remove(did)
    assert not devices.is_active(did)


def test_pclink_approval_kinds_validate():
    assert "pclink_pair" in approvals.KINDS and "pclink_contact" in approvals.KINDS
    approvals._validate("pclink_pair", {"action": "invite"})
    approvals._validate("pclink_pair", {"action": "accept", "accept_id": "abc"})
    approvals._validate("pclink_contact", {"peer": "0f1e2d3c4b5a6978", "session_id": "s"})
    import pytest
    from session_manager import team
    for bad in ({"action": "steal"}, {"action": "accept"}, {}):
        with pytest.raises(team.TeamError):
            approvals._validate("pclink_pair", bad)
    with pytest.raises(team.TeamError):
        approvals._validate("pclink_contact", {"peer": "x"})


def test_pclink_approval_summaries_name_scope():
    s = approvals._summary("pclink_contact", {"peer_name": "노트북", "title": "배포", "session_id": "s"})
    assert "노트북" in s and "[배포]" in s and "지금과 앞으로의 대화 전체" in s and "30일" in s
    assert "1회용·24시간" in approvals._summary("pclink_pair", {"action": "invite"})
    assert "노트북" in approvals._summary("pclink_pair", {"action": "accept", "peer_name": "노트북"})
