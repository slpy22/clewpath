"""서비스 대장 S3: 등록 신청 → 폴더 검사 → 승인(service_register) → claim 으로 원본 준비·토큰 1회.

docs/designs/service-registry.md §6~8
"""
import pytest
from fastapi.testclient import TestClient

from conftest import write_session
from session_manager import server as S, approvals, connector, config, profiles, services, servicereg, labels

PROF = {"system_prompt": "북한 법령만 답한다", "allowed_tools": ["ToolSearch"]}


@pytest.fixture
def env(fake_claude_home, tmp_path, monkeypatch):
    folder = tmp_path / "svc" / "nklaw_chatbot"
    folder.mkdir(parents=True)
    made = []

    def fake_make(f, prof, timeout=240):
        sid = f"dddddddd-0000-0000-0000-00000000000{len(made) + 1}"
        profiles.set_record(sid, **prof)
        write_session(fake_claude_home, "E--svc-nk", sid, [
            {"type": "user", "cwd": str(f), "message": {"role": "user", "content": "준비"},
             "timestamp": "2026-10-20T00:00:00.000Z"}])
        made.append((sid, prof))
        return sid
    monkeypatch.setattr(servicereg, "make_base", fake_make)
    monkeypatch.setattr(S.webapi, "ephemeral_ask",
                        lambda sid, p, **k: {"answer": "ok", "result": "ok", "cost_usd": 0, "fork_id": "f"})
    monkeypatch.setattr(S, "_is_local", lambda x: True)
    return TestClient(S.create_app()), folder, made


def _req(client, folder, **kw):
    body = {"id": "nklaw", "name": "북한법", "folder": str(folder), "purpose": "법령 챗봇",
            "bases": {"chat": {"new": PROF}}, **kw}
    return client.post("/api/services/requests", json=body)


def _approve(aid):
    return approvals.decide(aid, True, "otp")


def test_full_flow_claim_once(env):
    client, folder, made = env
    (folder / ".env").write_text("X=1")
    r = _req(client, folder)
    assert r.status_code == 200, r.text
    j = r.json()
    rid, claim = j["request"]["id"], j["claim"]
    assert claim not in (config.data_dir() / "services.json").read_text(encoding="utf-8")
    ap = approvals.get(j["request"]["approval"])
    assert ap["kind"] == "service_register" and ap["status"] == "pending"
    warns = " ".join(ap["args"]["warnings"])
    assert ".env" in warns and "permissions.deny" in warns
    # 승인 전·틀린 비밀
    assert client.post(f"/api/services/requests/{rid}/claim", json={"claim": claim}).status_code == 409
    assert client.post(f"/api/services/requests/{rid}/claim", json={"claim": "x"}).status_code == 403
    _approve(ap["id"])
    assert client.get(f"/api/services/requests/{rid}").json()["status"] == "approved"
    # 화면의 '실행' 으로는 원본을 만들지 않는다(토큰이 사라지므로)
    with pytest.raises(Exception) as e:
        approvals.execute(ap["id"])
    assert getattr(e.value, "code", "") == "claim_required" and not made
    assert approvals.get(ap["id"])["status"] == "approved"
    c = client.post(f"/api/services/requests/{rid}/claim", json={"claim": claim})
    assert c.status_code == 200, c.text
    tok, svc = c.json()["token"], c.json()["service"]
    base = made[0][0]
    assert svc["bases"] == {"chat": base} and made[0][1]["permission_mode"] == "dontAsk"
    assert labels.get(base)["labels"] == ["서비스: 북한법"]
    assert services.get("nklaw")["folder"].endswith("svc/nklaw_chatbot")
    # 받은 토큰으로 자기 원본 질의(원격처럼)
    S._is_local = lambda x: False
    assert client.post(f"/api/v1/sessions/{base}/ask", json={"prompt": "q"},
                       headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    S._is_local = lambda x: True
    assert client.post(f"/api/services/requests/{rid}/claim", json={"claim": claim}).status_code == 410
    assert client.get(f"/api/services/requests/{rid}").json()["status"] == "claimed"


def test_rejected_request_cannot_claim(env):
    client, folder, made = env
    j = _req(client, folder).json()
    approvals.decide(j["request"]["approval"], False, "otp")
    r = client.post(f"/api/services/requests/{j['request']['id']}/claim", json={"claim": j["claim"]})
    assert r.status_code == 409 and "rejected" in r.json()["error"] and not made


def test_folder_rules(env, fake_claude_home, tmp_path):
    client, folder, _ = env
    assert _req(client, tmp_path / "nope").json()["error"] == "folder_rejected"
    assert "claude" in _req(client, tmp_path).json()["message"]           # ~/.claude 를 품은 폴더
    inner = folder / "app"
    inner.mkdir()
    write_session(fake_claude_home, "E--dev", "eeeeeeee-0000-0000-0000-000000000001", [
        {"type": "user", "cwd": str(inner), "message": {"role": "user", "content": "개발"},
         "timestamp": "2026-10-20T00:00:00.000Z"}])
    r = _req(client, folder).json()
    assert r["error"] == "folder_rejected" and "다른 세션" in r["message"]
    chk = servicereg.check_folder(str(folder.parent.parent.parent))
    assert chk["errors"]


def test_home_is_rejected(tmp_path, monkeypatch):
    # 가짜 홈으로 본다(2026-10-23 맥 실기 3b-4): check_folder 는 받은 폴더를 훑는다 — 진짜 홈이면 ~/Library 의 다른 앱 데이터까지 닿아
    # macOS 개인정보 확인 창이 뜨고, 답할 때까지 시험이 멈췄다(화면이 꺼진 맥, ClewPath 웹 터미널 안 전체 시험 7시간).
    import os
    real = os.path.realpath(os.path.expanduser("~"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    walked = []
    walk = os.walk
    monkeypatch.setattr(servicereg.os, "walk", lambda top, *a, **k: (walked.append(os.path.realpath(top)), walk(top, *a, **k))[1])
    chk = servicereg.check_folder(os.path.expanduser("~"))
    assert any("홈" in e for e in chk["errors"])
    assert real not in walked, "진짜 홈을 훑었다"


def test_rejected_folder_is_not_walked(env, tmp_path, monkeypatch):
    """제품 쪽(맥 4회차 제안): 이미 승인할 수 없는 폴더(홈·드라이브 루트·~/.claude 를 품은 폴더 등)는 훑지 않는다 — 훑으면 맥에서
    다른 앱 데이터에 닿아 개인정보 확인 창의 답을 기다리며 Host 의 그 요청이 멈출 수 있다. 승인할 수 있는 폴더는 지금처럼 훑는다."""
    _client, folder, _ = env
    walked = []
    walk = servicereg.os.walk
    monkeypatch.setattr(servicereg.os, "walk", lambda top, *a, **k: (walked.append(top), walk(top, *a, **k))[1])
    chk = servicereg.check_folder(str(tmp_path))                          # ~/.claude(가짜 홈)를 품은 폴더 → 오류
    assert chk["errors"] and walked == []
    ok = servicereg.check_folder(str(folder))
    assert not ok["errors"] and walked == [str(folder)]


def test_adopt_requires_same_folder_and_profile(env, fake_claude_home):
    client, folder, made = env
    sid = "ffffffff-0000-0000-0000-000000000001"
    write_session(fake_claude_home, "E--nk", sid, [
        {"type": "user", "cwd": str(folder), "message": {"role": "user", "content": "x"},
         "timestamp": "2026-10-20T00:00:00.000Z"}])
    r = _req(client, folder, bases={"chat": {"adopt": sid}})
    assert r.json()["error"] == "base_no_profile"
    profiles.set_record(sid, **PROF)
    j = _req(client, folder, bases={"chat": {"adopt": sid}}).json()
    ap = approvals.get(j["request"]["approval"])
    assert any("입양" in w for w in ap["args"]["warnings"])
    assert not any("다른 대화" in w for w in ap["args"]["warnings"])   # 입양 세션 자신은 세지 않는다
    _approve(ap["id"])
    c = client.post(f"/api/services/requests/{j['request']['id']}/claim", json={"claim": j["claim"]}).json()
    assert c["service"]["bases"] == {"chat": sid} and not made


def test_new_base_needs_system_prompt(env):
    client, folder, _ = env
    r = _req(client, folder, bases={"chat": {"new": {"allowed_tools": ["Read"]}}})
    assert r.status_code == 400 and r.json()["error"] == "bad_profile"


def test_base_failure_retry_keeps_done_bases(env, monkeypatch):
    client, folder, made = env
    real = servicereg.make_base
    calls = []

    def flaky(f, prof, timeout=240):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("base_create_failed")
        return real(f, prof)
    monkeypatch.setattr(servicereg, "make_base", flaky)
    j = _req(client, folder, bases={"chat": {"new": PROF}, "gen": {"new": PROF}}).json()
    _approve(j["request"]["approval"])
    rid = j["request"]["id"]
    r = client.post(f"/api/services/requests/{rid}/claim", json={"claim": j["claim"]})
    assert r.status_code == 502 and len(made) == 1
    r = client.post(f"/api/services/requests/{rid}/claim", json={"claim": j["claim"]})
    assert r.status_code == 200 and len(made) == 2                       # chat 은 다시 만들지 않는다
    assert set(r.json()["service"]["bases"]) == {"chat", "gen"}


def test_requests_are_local_only(env):
    client, folder, _ = env
    S._is_local = lambda x: False
    try:
        assert _req(client, folder).status_code == 403
        assert client.post("/api/services/requests/sr_x/claim", json={"claim": "x"}).status_code == 403
    finally:
        S._is_local = lambda x: True
    assert connector._is_local_only_api("/api/services/requests", "POST")
    assert connector._is_local_only_api("/api/services/requests/sr_x", "GET")


def test_existing_service_id_rejected(env):
    client, folder, _ = env
    j = _req(client, folder).json()
    _approve(j["request"]["approval"])
    client.post(f"/api/services/requests/{j['request']['id']}/claim", json={"claim": j["claim"]})
    assert _req(client, folder).json()["error"] == "exists"
