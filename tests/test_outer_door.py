"""바깥문 B(docs/designs/session-control.md 6·11 절): Claude 세션이 부르면 403, 판별 실패도 403, 확인된 비-Claude 만 통과.
변경 라우트는 모두 정책 표(GUARDED·OPEN)에 있어야 한다 — 손으로 맞추는 목록 방지(R2)."""
from __future__ import annotations

import pytest

from session_manager import outer_door, team, webterm
from session_manager.pclink import sender


def _client(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL", "SM_OUTER_DOOR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app(), base_url="http://127.0.0.1")


def _caller(monkeypatch, result):
    def fake(host, port, server):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(sender, "caller_session", fake)


def test_every_mutating_route_is_on_the_table(fake_claude_home, monkeypatch):
    """새 변경 라우트는 보호/해당 없음 중 하나로 표에 올려야 한다. 표에만 남은 옛 항목도 실패."""
    with _client(monkeypatch) as c:
        routes = {r.path for r in c.app.routes if (getattr(r, "methods", None) or set()) & outer_door.MUTATING}
    assert routes - (outer_door.GUARDED | outer_door.OPEN) == set(), "표에 없는 변경 라우트"
    assert (outer_door.GUARDED | outer_door.OPEN) - routes == set(), "라우트가 사라졌는데 표에 남음"
    assert outer_door.GUARDED & outer_door.OPEN == set()


@pytest.mark.parametrize("path", [
    "/api/sessions/abc/title", "/api/v1/sessions/abc/title", "/api/owner/names/agent-sync", "/api/sessions/abc/delete",
    "/api/owner/trash/b1/restore", "/api/sessions/abc/change-cwd", "/api/sessions/abc/revert-continuation", "/api/import",
    "/api/owner/plugin/install", "/api/owner/teams/T/root"])
def test_claude_caller_gets_403_on_every_guarded_route(fake_claude_home, monkeypatch, path):
    _caller(monkeypatch, "sess-1")
    with _client(monkeypatch) as c:
        r = c.post(path, json={})
    assert r.status_code == 403 and r.json()["error"] == "claude_session_blocked" and r.json()["caller"] == "sess-1"


@pytest.mark.parametrize("reason", ["no_socket_owner", "no_process", "ambiguous_pid", "duplicate_claim"])
def test_unidentified_caller_is_refused_with_retry_hint(fake_claude_home, monkeypatch, reason):
    _caller(monkeypatch, sender.BadSender(reason))
    with _client(monkeypatch) as c:
        r = c.post("/api/sessions/abc/delete", json={"dry_run": True})
    assert r.status_code == 403 and r.json()["error"] == "caller_unknown" and "다시 시도" in r.json()["message"]


@pytest.mark.parametrize("reason", ["no_claude_ancestor", "not_local"])
def test_confirmed_non_claude_passes(fake_claude_home, monkeypatch, reason):
    _caller(monkeypatch, sender.BadSender(reason))
    with _client(monkeypatch) as c:
        r = c.post("/api/sessions/없는세션/delete", json={"dry_run": True})
    assert r.status_code != 403, r.text


def test_detection_tool_crash_counts_as_unknown(fake_claude_home, monkeypatch):
    _caller(monkeypatch, RuntimeError("psutil 고장"))
    with _client(monkeypatch) as c:
        assert c.post("/api/sessions/abc/delete", json={}).json()["error"] == "caller_unknown"


def test_relay_header_does_not_skip_the_check(fake_claude_home, monkeypatch):
    """X-ClewPath-Via 는 curl 로 붙일 수 있다 — 헤더로 건너뛰면 안 된다."""
    _caller(monkeypatch, "sess-1")
    with _client(monkeypatch) as c:
        r = c.post("/api/sessions/abc/delete", json={}, headers={"X-ClewPath-Via": "relay"})
    assert r.status_code == 403 and r.json()["error"] == "claude_session_blocked"


def test_open_routes_and_reads_are_not_traced(fake_claude_home, monkeypatch):
    def boom(*a):
        raise AssertionError("보호 라우트가 아닌데 추적했다")
    monkeypatch.setattr(sender, "caller_session", boom)
    with _client(monkeypatch) as c:
        assert c.post("/api/sessions/abc/keep", json={"keep": True}).status_code != 403
        assert c.get("/api/sessions").status_code != 403


def test_emergency_switch_env_and_config(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_OUTER_DOOR", "off")
    assert outer_door.enabled() is False
    monkeypatch.delenv("SM_OUTER_DOOR")
    from session_manager import appconfig
    monkeypatch.setattr(appconfig, "get", lambda s, k, d=None: "off" if (s, k) == ("security", "outer_door") else d)
    assert outer_door.enabled() is False
    monkeypatch.setattr(appconfig, "get", lambda s, k, d=None: d)
    assert outer_door.enabled() is True


def test_switch_off_lets_claude_through(fake_claude_home, monkeypatch):
    _caller(monkeypatch, "sess-1")
    with _client(monkeypatch) as c:
        monkeypatch.setenv("SM_OUTER_DOOR", "off")
        assert c.post("/api/sessions/없는세션/delete", json={"dry_run": True}).status_code != 403


# ---------------------------------------------------------------- 새 자동 허용 세션: 통과 + 감사(사장님 결정 D1 C)
@pytest.fixture
def started(monkeypatch):
    monkeypatch.setattr(webterm, "start_new",
                        lambda cwd, prompt, skip_permissions, session_id: {"status": "started", "session_id": "new-1"})
    events = {"audit": [], "push": []}
    from session_manager import policy, push
    monkeypatch.setattr(policy, "audit", lambda e: events["audit"].append(e))
    monkeypatch.setattr(push, "send", lambda *a, **k: str(a[0]).startswith("auto-session:") and events["push"].append(a))
    return events


def test_claude_creating_auto_session_passes_but_is_recorded(fake_claude_home, monkeypatch, started, tmp_path):
    _caller(monkeypatch, "sess-1")
    with _client(monkeypatch) as c:
        r = c.post("/api/terminal/new", json={"cwd": str(tmp_path), "skip": True})
    assert r.status_code == 200 and r.json()["session_id"] == "new-1", "워커 스킬은 그대로 돈다"
    assert started["audit"][0]["event"] == "auto_session_created" and started["audit"][0]["caller"] == "sess-1"
    assert len(started["push"]) == 1 and "자동 허용" in started["push"][0][2]


def test_unknown_caller_is_also_recorded(fake_claude_home, monkeypatch, started, tmp_path):
    _caller(monkeypatch, sender.BadSender("no_socket_owner"))
    with _client(monkeypatch) as c:
        assert c.post("/api/terminal/new", json={"cwd": str(tmp_path), "skip": True}).status_code == 200
    assert started["audit"][0]["caller_kind"] == "unknown"


@pytest.mark.parametrize("skip,caller", [(True, sender.BadSender("no_claude_ancestor")), (False, "sess-1")])
def test_no_alert_for_humans_or_safe_sessions(fake_claude_home, monkeypatch, started, tmp_path, skip, caller):
    _caller(monkeypatch, caller)
    with _client(monkeypatch) as c:
        assert c.post("/api/terminal/new", json={"cwd": str(tmp_path), "skip": skip}).status_code == 200
    assert not started["audit"] and not started["push"]


def test_audit_failure_does_not_block_creation(fake_claude_home, monkeypatch, started, tmp_path):
    _caller(monkeypatch, "sess-1")
    from session_manager import policy

    def boom(e):
        raise OSError("디스크 가득")
    monkeypatch.setattr(policy, "audit", boom)
    with _client(monkeypatch) as c:
        assert c.post("/api/terminal/new", json={"cwd": str(tmp_path), "skip": True}).status_code == 200
