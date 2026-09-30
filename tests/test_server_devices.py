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


# ---- 0.11.0 (E-D7/E-D15): 폐기 결과 불명 → 행 유지(delete_pending/cp_pending), 재발급 pending_cpub, notice ----

def test_delete_keeps_row_pending_when_revoke_unconfirmed(client, monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_a")
    from session_manager import connector as C
    drops = []
    monkeypatch.setattr(C, "request_drop_device", lambda did, wait_s=2.5, notice=None: drops.append((did, notice)) or 0)
    _calls(monkeypatch, revoke=False)
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "client_status", lambda ids: None)     # 상태 조회도 불명
    r = client.post(f"/api/owner/devices/{d['id']}/delete", json={})
    assert r.status_code == 200 and r.json()["deleted"] is False and r.json()["pending"] is True
    row = devices.list_devices()[0]
    assert row["revoked"] and row["revoked_by"] == "pc" and row["delete_pending"] and row["cp_pending"]
    assert drops == [(d["id"], "device_removed")], "notice → 해체는 어느 경우든"
    assert devices.verify(d["token"]) is None, "토큰은 즉시 무효"
    # 폐기가 확인되면 행 제거(delete 재호출 = 정상 삭제)
    _calls(monkeypatch, revoke=True)
    r = client.post(f"/api/owner/devices/{d['id']}/delete", json={})
    assert r.json()["deleted"] is True and devices.list_devices() == []


def test_delete_phone_revoked_row_deletes_now(client, monkeypatch):
    # 폰이 이미 self-revoke 한 📵 행: CP revoke 는 False(이미 폐기)지만 상태 조회가 revoked 면 바로 삭제(2026-09-30 실사용 버그)
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_a")
    devices.mark_revoked(d["id"], by="phone")
    from session_manager import connector as C, cp_client
    monkeypatch.setattr(C, "request_drop_device", lambda did, wait_s=2.5, notice=None: 0)
    _calls(monkeypatch, revoke=False)
    monkeypatch.setattr(cp_client, "client_status", lambda ids: {"cpub_a": {"status": "revoked"}})
    r = client.post(f"/api/owner/devices/{d['id']}/delete", json={})
    assert r.json()["deleted"] is True and r.json()["pending"] is False and devices.list_devices() == []
    # 상태가 active(= 폐기가 정말 안 됨)면 여전히 pending
    d2 = devices.add_device("b"); devices.set_client_public_id(d2["id"], "cpub_b")
    monkeypatch.setattr(cp_client, "client_status", lambda ids: {"cpub_b": {"status": "active"}})
    assert client.post(f"/api/owner/devices/{d2['id']}/delete", json={}).json()["pending"] is True


def test_reissue_records_pending_cpub_then_confirms_and_pending_on_unconfirmed_revoke(client, monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    d = devices.add_device("a"); devices.set_client_public_id(d["id"], "cpub_old")
    from session_manager import connector as C
    drops = []
    monkeypatch.setattr(C, "request_drop_device", lambda did, wait_s=2.5, notice=None: drops.append((did, notice)) or 0)
    _calls(monkeypatch, issue={"client_public_id": "cpub_new", "client_secret": "s"}, revoke=False)
    r = client.post(f"/api/owner/devices/{d['id']}/reissue", json={})
    assert r.status_code == 200
    snap = devices.sync_snapshot()[0]
    assert snap["cpub"] == "cpub_new" and snap["pending_cpub"] is None, "확정 뒤 pending_cpub 정리"
    assert snap["cp_pending"] == ["revoke:cpub_old"], "옛 cpub 폐기 불명 → devsync 재시도 대상"
    assert drops == [(d["id"], "device_reissued")]


def test_list_reports_cp_synced_and_kicks_devsync(client, monkeypatch):
    from session_manager import devsync
    kicks = []
    monkeypatch.setattr(devsync, "kick", lambda: kicks.append(1))
    devices.set_cp_synced(1700000000)
    r = client.get("/api/owner/devices").json()
    assert r["cp_synced"] == 1700000000 and kicks == [1]


# ---- 0.10.5: 원격(릴레이 경유, 커넥터 2FA 게이트) 업데이트 적용 허용 ----

def test_update_apply_allowed_via_relay_header(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S, updater, connector as C
    monkeypatch.setattr(S, "_client_host", lambda r: "10.0.0.9")          # 로컬 아님
    monkeypatch.setattr(updater, "RELEASE_KEYS", [], raising=False)
    with TestClient(S.create_app()) as c:
        assert c.post("/api/owner/update/apply").status_code == 403, "릴레이도 로컬도 아닌 직접 접근은 거부"
        r = c.post("/api/owner/update/apply", headers={C.VIA_HEADER: "relay"})
        assert r.status_code == 400 and "공개키" in r.json()["error"], "릴레이 경유는 로컬 게이트를 통과해 다음 검사(키)로 간다"
    assert C._is_local_only_api("/api/owner/update/apply", "POST") is False
    assert C._is_privileged_api("/api/owner/update/apply") is True
