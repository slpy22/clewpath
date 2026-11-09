"""별도 프로젝트 폴더(2026-11-03 사장님): 하위 폴더 세션도 상위 폴더 묶음에 넣지 않고 따로 — 폴더 단위, ClewPath 저장소에만."""
from session_manager import connector, session_meta

SID = "11111111-2222-3333-4444-555555555555"   # conftest.sample_session — cwd F:\021_test\proj


def _app(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import housekeeping
    monkeypatch.setattr(housekeeping, "start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")   # TestClient 호스트는 'testclient' → 로컬로 간주
    return TestClient(S.create_app())


def _flag(c):
    old = {s["session_id"]: s["standalone"] for p in c.get("/api/sessions").json()["projects"] for s in p["sessions"]}
    v1 = {s["session_id"]: s["standalone"] for s in c.get("/api/v1/sessions").json()["sessions"]}   # PWA·릴레이가 받는 목록
    assert old == v1
    return v1


def test_folder_key_matches_pwa_path_key():
    assert session_meta.folder_key("F:\\021_Test\\Proj\\") == "f:/021_test/proj"
    assert session_meta.folder_key("/Users/Me/Proj/") == "/Users/Me/Proj", "맥·리눅스 경로는 대소문자 그대로"


def test_set_and_list_standalone(sample_session, monkeypatch):
    with _app(monkeypatch) as c:
        assert _flag(c) == {SID: False}
        r = c.post(f"/api/sessions/{SID}/standalone", json={"on": True}).json()
        assert r == {"session_id": SID, "cwd": "F:\\021_test\\proj", "standalone": True}
        assert _flag(c) == {SID: True}
        assert session_meta.standalone_folders() == {"f:/021_test/proj"}, "세션이 아니라 폴더로 기억한다"
        session_meta.set_record(SID, keep="1")                 # 세션 기록을 바꿔도 폴더 기억은 남는다
        assert _flag(c) == {SID: True}
        c.post(f"/api/sessions/{SID}/standalone", json={"on": False})
        assert _flag(c) == {SID: False} and session_meta.standalone_folders() == set()
        assert c.post("/api/sessions/no-such/standalone", json={"on": True}).status_code == 404
    assert not connector._is_privileged_api(f"/api/sessions/{SID}/standalone"), "보기 설정이라 2차 인증 없음"
