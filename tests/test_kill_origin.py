"""세션 출처 마크·강제 종료(2026-09-30 사장님): 레지스트리 pid 의 부모/명령줄로 출처를 나누고, 어디서 띄웠든 끝낸다."""
from __future__ import annotations

import pytest

from session_manager import peers, lifecycle


def _info(parent="pwsh.exe", cmd=("claude.exe", "--resume", "x"), ppid=100, name="claude.exe"):
    return {"name": name, "cmdline": list(cmd), "parent_name": parent, "parent_pid": ppid, "create_time": 1.0}


@pytest.mark.parametrize("info,entry,host,expect", [
    (None, "cli", 1, "unknown"),
    (_info(parent="python.exe", ppid=4242), "cli", 4242, "clewpath"),          # 부모 = Host 자신
    (_info(parent="python.exe", ppid=9), "cli", 4242, "script"),
    (_info(cmd=("claude.exe", "-p", "hi")), "cli", 1, "headless"),
    (_info(cmd=("claude.exe", "--print", "hi"), parent="claude.exe"), "cli", 1, "headless"),
    (_info(parent="claude.exe"), "cli", 1, "child"),
    (_info(parent="node.exe"), "cli", 1, "child"),
    (_info(parent="pwsh.exe"), "cli", 1, "terminal"),
    (_info(parent="WindowsTerminal.exe"), "cli", 1, "terminal"),
    (_info(parent="Code.exe"), "cli", 1, "terminal"),
    (_info(parent="pwsh.exe"), "sdk", 1, "sdk"),
    (_info(parent="explorer.exe"), "cli", 1, "other"),
])
def test_classify_origin(info, entry, host, expect):
    r = peers.classify_origin(info, entry, host_pid=host)
    assert r["origin"] == expect
    assert r["parent"] == ((info or {}).get("parent_name") or None)


def test_origin_of_caches_by_pid_and_create_time(monkeypatch):
    calls = []
    def _pi(pid): calls.append(pid); return _info(parent="pwsh.exe")
    monkeypatch.setattr(peers, "_proc_info", _pi)
    peers._ORIGIN_CACHE.clear()
    assert peers.origin_of(77, "cli")["origin"] == "terminal"
    assert peers.origin_of(77, "cli")["origin"] == "terminal"
    assert len(calls) == 2, "프로세스 존재 확인은 매번(재사용 pid 방지), 분류는 캐시"
    assert peers.origin_of("nope")["origin"] == "unknown"
    monkeypatch.setattr(peers, "_proc_info", lambda pid: None)
    assert peers.origin_of(77)["origin"] == "unknown", "사라진 프로세스는 캐시를 믿지 않는다"


def test_peer_map_carries_origin(fake_claude_home, monkeypatch):
    import json
    d = fake_claude_home / "sessions"; d.mkdir()
    (d / "5.json").write_text(json.dumps({"pid": 5, "sessionId": "s1", "name": "n", "status": "idle", "kind": "interactive", "entrypoint": "cli"}), encoding="utf-8")
    monkeypatch.setattr(peers, "_proc_info", lambda pid: _info(parent="claude.exe"))
    peers._ORIGIN_CACHE.clear()
    m = peers.peer_map()
    assert m["s1"]["origin"] == {"origin": "child", "parent": "claude.exe"} and m["s1"]["entrypoint"] == "cli"


def test_kill_session_stops_pty_and_kills_claude_pids_only(fake_claude_home, monkeypatch):
    import json, subprocess
    d = fake_claude_home / "sessions"; d.mkdir()
    (d / "10.json").write_text(json.dumps({"pid": 10, "sessionId": "s1", "name": "a"}), encoding="utf-8")
    (d / "11.json").write_text(json.dumps({"pid": 11, "sessionId": "s1", "name": "b"}), encoding="utf-8")   # pid 재사용(다른 프로그램)
    (d / "12.json").write_text(json.dumps({"pid": 12, "sessionId": "other"}), encoding="utf-8")
    infos = {10: _info(parent="pwsh.exe"), 11: {"name": "notepad.exe", "cmdline": ["notepad.exe"], "parent_name": "explorer.exe", "parent_pid": 1, "create_time": 1}}
    monkeypatch.setattr(peers, "_proc_info", lambda pid: infos.get(pid))
    from session_manager import webterm
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: sid == "s1")
    stopped = []
    monkeypatch.setattr(webterm, "stop_terminal", lambda sid: stopped.append(sid) or True)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda args, **k: ran.append(args))
    monkeypatch.setattr(lifecycle.sys, "platform", "win32", raising=False) if hasattr(lifecycle, "sys") else None
    r = lifecycle.kill_session("s1")
    assert stopped == ["s1"]
    assert [k["how"] for k in r["killed"]] == ["clewpath", "process"] and r["killed"][1]["pid"] == 10
    assert r["errors"] == [{"pid": 11, "reason": "gone_or_not_claude"}], "claude 가 아닌 pid 는 절대 안 죽인다"
    assert any(a[:2] == ["taskkill", "/PID"] and a[2] == "10" and "/T" in a for a in ran)
    assert not any("12" in a for a in ran), "다른 세션의 pid 는 무접촉"


def test_kill_endpoint_404_when_not_live_and_privileged_remote(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import server as S, connector as C
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app()) as c:
        assert c.post("/api/sessions/nope/kill").status_code == 404
        monkeypatch.setattr(lifecycle, "kill_session", lambda sid: {"killed": [{"how": "process", "pid": 3}], "errors": []})
        r = c.post("/api/sessions/s1/kill")
        assert r.status_code == 200 and r.json()["ok"] is True
    assert C._is_privileged_api("/api/sessions/s1/kill") is True, "원격은 stop 과 같은 2FA 게이트"
