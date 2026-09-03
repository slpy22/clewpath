"""/ws/monitor 통합 — 관제 WS 라우트 + webmonitor 러너.

로컬(TestClient)은 인증 생략(_is_local). 스냅샷 리플레이, 호출선 태깅,
증분 이벤트 push(파일 append 감지)를 확인한다.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import write_session

MGR = "aaaaaaaa-1111-2222-3333-444444444444"
SUB = "59f9577b-0d25-47be-994b-29009cf0fba3"


@pytest.fixture
def app_client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        yield c, fake_claude_home


def _seed(home):
    # 관리 세션: 하위를 claude -p --resume 로 호출(호출선 소스)
    write_session(home, "F--mgr", MGR, [
        {"type": "user", "cwd": "F:/mgr", "slug": "mgr-agent",
         "message": {"role": "user", "content": "포털에 로그인 붙이라고 시켜"},
         "timestamp": "2026-09-03T00:00:00Z"},
        {"type": "assistant", "cwd": "F:/mgr", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": "tu1", "name": "Bash",
                         "input": {"command":
                                   f'claude -p --resume {SUB} "로그인 붙여줘"'}}]},
         "timestamp": "2026-09-03T00:00:01Z"},
    ])
    # 하위 세션: 수신 프롬프트(user) + 응답(assistant)
    write_session(home, "F--portal", SUB, [
        {"type": "user", "cwd": "F:/portal", "slug": "portal-dev",
         "message": {"role": "user", "content": "로그인 붙여줘"},
         "timestamp": "2026-09-03T00:00:02Z"},
        {"type": "assistant", "cwd": "F:/portal",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "auth.py 생성 완료"}]},
         "timestamp": "2026-09-03T00:00:03Z"},
    ])


def test_monitor_snapshot_and_callline(app_client):
    client, home = app_client
    _seed(home)
    with client.websocket_connect(
            f"/ws/monitor?ids={MGR},{SUB}&manager={MGR}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "snapshot"
        evs = msg["events"]
        # 두 세션 이벤트가 시간축으로 병합됨
        labels = [e["session_label"] for e in evs]
        assert "mgr-agent" in labels and "portal-dev" in labels
        # 시간순 정렬
        seqs_ts = [e["ts"] for e in evs]
        assert seqs_ts == sorted(seqs_ts)
        # 관리 세션의 Bash 호출이 호출선(calls_out)으로 하위를 가리킴
        mgr_ev = next(e for e in evs if e["calls_out"])
        assert mgr_ev["calls_out"][0]["target_session_id"] == SUB
        assert mgr_ev["calls_out"][0]["prompt"] == "로그인 붙여줘"
        assert mgr_ev["session_role"] == "manager"


def test_monitor_incremental_push(app_client):
    client, home = app_client
    _seed(home)
    with client.websocket_connect(
            f"/ws/monitor?ids={MGR},{SUB}&manager={MGR}") as ws:
        ws.receive_json()  # snapshot 소비
        # 하위 세션에 새 줄 append → 증분 이벤트가 push 되어야 함
        sub_jsonl = home / "projects" / "F--portal" / f"{SUB}.jsonl"
        with sub_jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "cwd": "F:/portal",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "pytest 5 passed"}]},
                "timestamp": "2026-09-03T00:00:09Z"}, ensure_ascii=False) + "\n")
        # events 메시지 나올 때까지(중간에 heartbeat 올 수 있음)
        got = None
        for _ in range(20):
            m = ws.receive_json()
            if m["type"] == "events":
                got = m
                break
        assert got is not None
        assert any(e["text"] == "pytest 5 passed" for e in got["events"])


def test_monitor_empty_group_errors(app_client):
    client, _home = app_client
    with client.websocket_connect("/ws/monitor?ids=") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["error"] == "empty_group"
