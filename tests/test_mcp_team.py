"""MCP 팀·비서 도구(단계 3 A-10): 읽기 + 승인 요청/상태/실행만, 직접 쓰기 도구 없음."""
from __future__ import annotations

import pytest

mcp_server = pytest.importorskip("session_manager.mcp_server")


class _R:
    def __init__(self, code, data):
        self.status_code, self._d, self.text = code, data, str(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_team_tools_call_local_team_api(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server.httpx, "get", lambda url, params=None, **k: calls.append(("GET", url, params)) or _R(200, {"ok": 1}))
    monkeypatch.setattr(mcp_server.httpx, "post", lambda url, json=None, **k: calls.append(("POST", url, json)) or
                        _R(403 if url.endswith("/execute") else 200, {"error": "not_approved:pending"} if url.endswith("/execute") else {"id": "ap_1"}))
    mcp_server.team_list(); mcp_server.team_status("WEB"); mcp_server.task_timeline("WEB", "WEB-T1")
    mcp_server.search_inputs("로그인")
    assert mcp_server.approval_request("delegate", {"team": "WEB", "goal": "g"}, "s-1") == {"id": "ap_1"}
    assert mcp_server.approval_execute("ap_1") == {"error": "not_approved:pending", "status": 403}, "승인 전 실행은 오류를 그대로"
    urls = [c[1] for c in calls]
    assert urls[0].endswith("/api/v1/team") and urls[1].endswith("/api/v1/team/WEB")
    assert urls[2].endswith("/api/v1/team/WEB/tasks/WEB-T1/timeline") and urls[3].endswith("/api/v1/team/search")
    assert calls[4][2] == {"kind": "delegate", "args": {"team": "WEB", "goal": "g"}, "requested_by": "s-1"}


def test_no_direct_write_tools():
    names = {n for n in dir(mcp_server) if not n.startswith("_")}
    for forbidden in ("team_create", "task_delegate", "task_decide", "team_purge", "team_archive"):
        assert forbidden not in names, f"쓰기는 승인 요청으로만: {forbidden}"
