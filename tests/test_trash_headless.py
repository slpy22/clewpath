"""🤖 자동 실행 기록 한꺼번에 휴지통(2026-10-12): 서버가 다시 읽어 headless 인 것만, 실행 중은 건너뜀."""
from fastapi.testclient import TestClient

from tests.conftest import write_session

H1 = "aaaa0000-1111-2222-3333-444444444444"
H2 = "aaaa0001-1111-2222-3333-444444444444"
N1 = "bbbb0000-1111-2222-3333-444444444444"


def _rec(ep):
    return [{"type": "user", "cwd": "E:\c", "entrypoint": ep, "message": {"role": "user", "content": "x"},
             "timestamp": "2026-10-12T04:00:00.000Z"}]


def test_trash_headless_only_moves_headless(fake_claude_home, monkeypatch):
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    write_session(fake_claude_home, "E--c", H1, _rec("sdk-cli"))
    write_session(fake_claude_home, "E--c", H2, _rec("sdk-cli"))
    write_session(fake_claude_home, "E--c", N1, [{"type": "permission-mode"}] + _rec("cli"))
    live = {H2}
    monkeypatch.setattr("session_manager.lifecycle._live_reason", lambda sid: "busy" if sid in live else None)
    from session_manager import scanner
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        r = c.post("/api/sessions/trash-headless", json={"ids": [H1, H2, N1, "nope", H1]}).json()
    assert r["moved"] == [H1]
    reasons = {x["session_id"]: x["reason"] for x in r["skipped"]}
    assert reasons == {H2: "session_live", N1: "not_headless", "nope": "not_found"}
    left = {m.session_id for m in scanner.scan_all()}
    assert H1 not in left and {H2, N1} <= left, "일반 세션·실행 중 기록은 그대로"
