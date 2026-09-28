"""버퍼 초과 폴백 — 실제 `/ws/terminal` 라우트 경유(미검증 소화 2026-09-28).

test_webterm_delta 는 run_terminal 을 직접 불러 서버 회계만 봤다. 여기서는 PWA 가 실제로
쓰는 경로(`/ws/terminal/{sid}?screen=…`)로 붙어 쿼리 파싱 → 재접속 → 폴백 안내까지 한 줄로 확인한다.
"""
from __future__ import annotations

import pytest

from session_manager import webterm

SID = "aaaaaaaa-1111-2222-3333-444444444444"


class _Proc:
    pid = 4242

    def isalive(self):
        return True

    def write(self, d):
        pass

    def setwinsize(self, r, c):
        pass

    def terminate(self, force=False):
        pass


@pytest.fixture
def app_client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def live():
    webterm._ACTIVE.clear()
    sess = webterm._TermSession(SID, _Proc(), persist=True)
    webterm._ACTIVE[SID] = sess
    yield sess
    webterm._ACTIVE.clear()


def test_route_overflow_falls_back_to_tail_with_note(app_client, live, monkeypatch):
    monkeypatch.setattr(webterm, "_BUF_CAP", 10)
    live.feed("aaaa")
    live.mark_sent("S1", 2)                       # 화면 S1 은 'aa' 까지 봤다
    live.feed("bbbb"); live.feed("cccc")          # 그 사이 버퍼가 밀림(base_off=4 > 2)
    with app_client.websocket_connect(f"/ws/terminal/{SID}?screen=S1") as ws:
        assert ws.receive_text() == "bbbbcccc"    # 남은 버퍼 전부(tail 폴백)
        assert "생략" in ws.receive_text()         # 정직한 안내
        assert live.client_screen == "S1" and live.screens["S1"] == live.total_len
    assert live.client is None                    # 화면만 떨어지고 PTY 는 산다


def test_route_exact_delta_no_banner_then_cursor_at_end(app_client, live):
    live.feed("hello "); live.feed("world")
    live.mark_sent("S1", 6)
    with app_client.websocket_connect(f"/ws/terminal/{SID}?screen=S1") as ws:
        assert ws.receive_text() == "world"       # 정확한 델타, 배너 없음
        ws.send_json({"type": "resize", "cols": 80, "rows": 24})
    # 다른(처음 보는) 화면은 tail + 재연결 배너
    with app_client.websocket_connect(f"/ws/terminal/{SID}?screen=S2") as ws:
        assert ws.receive_text() == "hello world"
        assert "다시 연결했습니다" in ws.receive_text()
    assert live.screens == {"S1": 11, "S2": 11}
