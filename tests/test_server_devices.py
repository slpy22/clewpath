"""기기 엔드포인트(0.10.0, eng E-D7/E-D19): 재발급은 새 발급 성공 뒤에만 옛 것을 바꾸고,
CP 가 설정된 환경에서 발급 실패는 503(공유토큰 폴백 금지). 폴백은 CP 미설정 구성에서만."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from session_manager import devices, cp_client


@pytest.fixture
def client(fake_claude_home, monkeypatch):
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL", "SM_APP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app()) as c:
        yield c


def _calls(monkeypatch, issue=None, revoke=True):
    log = []

    def _issue(device_id="", name=""):
        log.append(("issue", device_id, name))
        return issue() if callable(issue) else issue

    def _revoke(cpub):
        log.append(("revoke", cpub))
        return revoke
    monkeypatch.setattr(cp_client, "issue_client_credential", _issue)
    monkeypatch.setattr(cp_client, "revoke_client_credential", _revoke)
    return log


def test_add_without_cp_falls_back_to_shared_token(client, monkeypatch):
    log = _calls(monkeypatch, issue=None)
    r = client.post("/api/owner/devices", json={"name": " 폰\x00 A "})
    assert r.status_code == 200 and r.json()["client_scoped"] is False and r.json()["name"] == "폰 A"
    assert log == [], "CP 미설정이면 CP 를 부르지 않는다"
    assert client.get("/api/owner/devices").json()["devices"][0]["client_scoped"] is False


def test_add_with_cp_configured_refuses_fallback(client, monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    log = _calls(monkeypatch, issue=None)
    r = client.post("/api/owner/devices", json={"name": "a"})
    assert r.status_code == 503 and r.json()["error"] == "cp_unavailable"
    assert devices.list_devices() == [], "행을 남기지 않는다"
    assert [x[0] for x in log] == ["issue"]
    # 성공하면 cpub 이 연결된 행
    log = _calls(monkeypatch, issue={"client_public_id": "cpub_new", "client_secret": "s"})
    r = client.post("/api/owner/devices", json={"name": "a"})
    assert r.status_code == 200 and r.json()["client_scoped"] is True
    assert devices.get_client_public_id(r.json()["id"]) == "cpub_new"


def test_reissue_issue_first_then_revoke_old(client, monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_old")
    old_ver = devices.token_version(d["id"])
    log = _calls(monkeypatch, issue={"client_public_id": "cpub_new", "client_secret": "s"})
    r = client.post(f"/api/owner/devices/{d['id']}/reissue", json={})
    assert r.status_code == 200 and r.json()["client_scoped"] is True
    assert [x[0] for x in log] == ["issue", "revoke"] and log[1][1] == "cpub_old"
    assert devices.get_client_public_id(d["id"]) == "cpub_new"
    assert devices.token_version(d["id"]) != old_ver or devices.verify(d["token"]) is None


def test_reissue_keeps_old_when_issue_fails(client, monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_old")
    log = _calls(monkeypatch, issue=None)
    r = client.post(f"/api/owner/devices/{d['id']}/reissue", json={})
    assert r.status_code == 503
    assert devices.get_client_public_id(d["id"]) == "cpub_old", "옛 cpub 유지"
    assert devices.verify(d["token"]) is not None, "옛 dev 토큰(QR) 그대로 유효"
    assert ("revoke", "cpub_old") not in log


def test_reissue_revoke_unconfirmed_is_logged_not_fatal(client, monkeypatch, capsys):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_old")
    _calls(monkeypatch, issue={"client_public_id": "cpub_new", "client_secret": "s"}, revoke=False)
    r = client.post(f"/api/owner/devices/{d['id']}/reissue", json={})
    assert r.status_code == 200 and devices.get_client_public_id(d["id"]) == "cpub_new"
    assert "revoke unconfirmed" in capsys.readouterr().out


def test_reissue_revoked_device_404(client, monkeypatch):
    d = devices.add_device("a"); devices.revoke(d["id"])
    assert client.post(f"/api/owner/devices/{d['id']}/reissue", json={}).status_code == 404
