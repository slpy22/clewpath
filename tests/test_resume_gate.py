"""재개 직전 'ClewPath 밖에서 사용 중' 판정의 근거(2026-11-04 사장님).

사고: STT 세션의 ClewPath 터미널을 닫고(강제 종료 → SessionEnd 훅 없음) 바로 재개하자, 어디에도 떠 있지 않은데
'ClewPath 밖에서 연동 중' 확인 창이 10분 동안 떴다. 화면의 훅 시각 추정(10분)이 실행 중 목록을 받아 온 때에도 늘 돌았다.

① 실행 중 목록(`claude agents --json`)을 받았는지(agent_known) — '아무것도 안 떠 있음' 과 '못 받음' 을 구분
② ClewPath 가 프로세스를 끝낸 시각(gone_at) — 상태 이름과 상관없이 남긴다
③ 피어 레지스트리에 강제 종료로 남은 파일(죽은 pid·재사용된 pid)은 떠 있는 세션으로 치지 않는다
"""
from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest

from session_manager import agents, hooks, peers

_REAL_GONE = peers._gone          # conftest 가 시험마다 끄기 전의 진짜 함수(모듈을 읽을 때 잡아 둔다)
SID = "11111111-2222-3333-4444-555555555555"     # conftest.sample_session


@pytest.fixture(autouse=True)
def _fresh():
    agents._cache.update({"at": 0.0, "list": [], "ok": False})
    hooks._STATUS.clear()
    yield
    agents._cache.update({"at": 0.0, "list": [], "ok": False})
    hooks._STATUS.clear()


# ---------------------------------------------------------------- ① 실행 중 목록을 받았나
def _fake_run(monkeypatch, stdout: bytes, rc: int = 0):
    monkeypatch.setattr(agents.shutil, "which", lambda n: "claude.exe")
    monkeypatch.setattr(agents.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=stdout, returncode=rc))


def test_empty_list_is_known_not_unavailable(monkeypatch):
    _fake_run(monkeypatch, b"[]")
    assert agents.snapshot() == [] and agents.available() is True, "빈 목록 = 받아 왔고 아무것도 안 떠 있음"


@pytest.mark.parametrize("stdout,rc", [(b"", 0), (b"Usage: claude ...", 1), (b'{"a":1}', 0), (b"[]", 2)])
def test_unusable_answer_is_unavailable(monkeypatch, stdout, rc):
    _fake_run(monkeypatch, stdout, rc)
    assert agents.snapshot() == [] and agents.available() is False, "빈 출력·옛 CLI·목록 아님·실패 코드 = 못 받음"


def test_no_claude_or_timeout_is_unavailable(monkeypatch):
    monkeypatch.setattr(agents.shutil, "which", lambda n: None)
    assert agents.available() is False
    agents._cache["at"] = 0.0

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("claude", 15)
    monkeypatch.setattr(agents.shutil, "which", lambda n: "claude.exe")
    monkeypatch.setattr(agents.subprocess, "run", boom)
    assert agents.available() is False


def _app(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import housekeeping
    monkeypatch.setattr(housekeeping, "start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app())


@pytest.mark.parametrize("ok", [True, False])
def test_session_list_says_whether_agents_known(sample_session, monkeypatch, ok):
    def fetch():
        if not ok:
            raise agents.Unavailable("옛 CLI")
        return []
    monkeypatch.setattr(agents, "_fetch", fetch)
    with _app(monkeypatch) as c:
        j = c.get("/api/v1/sessions").json()
    s = {x["session_id"]: x for x in j["sessions"]}[SID]
    assert s["agent"] is None and s["agent_known"] is ok and j["agents_ok"] is ok


# ---------------------------------------------------------------- ② ClewPath 가 끝낸 시각
def test_mark_gone_records_gone_at_even_when_ready():
    hooks.update_from_event({"session_id": "a", "hook_event_name": "Stop"})
    assert hooks.mark_gone("a") is False and hooks._STATUS["a"]["phase"] == "ready", "완료 표시는 그대로"
    st = hooks._STATUS["a"]
    assert st["gone_at"] >= st["active_at"], "그 뒤로 어디서도 신호가 없었다"
    hooks.update_from_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    assert hooks.mark_gone("a") is True and hooks._STATUS["a"]["phase"] == "ended", "응답 도중이면 예전처럼 종료로 접는다"
    assert hooks.mark_gone("없음") is False


def test_terminal_cleanup_records_gone_at():
    from session_manager import webterm

    class P:
        pid = None

        def terminate(self, force=False):
            pass
    hooks.update_from_event({"session_id": "t", "hook_event_name": "Stop"})
    s = webterm._TermSession("t", P(), persist=True)
    webterm._cleanup(s)
    assert hooks._STATUS["t"].get("gone_at"), "ClewPath 터미널을 닫으면(강제 종료) 끝난 시각이 남는다"


# ---------------------------------------------------------------- ③ 피어 레지스트리의 죽은 pid
def test_peer_gone_for_dead_or_reused_pid():
    pytest.importorskip("psutil")
    import psutil
    me = os.getpid()
    ct = psutil.Process(me).create_time()
    ft = int((ct + peers._FILETIME_EPOCH) * 1e7)
    assert _REAL_GONE(me, {"proc_start": ft}) is False, "살아 있고 같은 프로세스"
    assert _REAL_GONE(me, {"proc_start": ft - 3600 * 10**7}) is True, "pid 가 다른 프로세스로 재사용됨"
    assert _REAL_GONE(me, {}) is False, "시각 정보가 없으면 판별 불가 → 믿는다"
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    if not psutil.pid_exists(p.pid):
        assert _REAL_GONE(p.pid, {}) is True, "끝난 프로세스의 파일"
    assert _REAL_GONE("x", {}) is False and _REAL_GONE(None, {}) is False


def test_stale_registry_file_is_not_a_peer(fake_claude_home, monkeypatch):
    import json
    d = fake_claude_home / "sessions"
    d.mkdir()
    (d / "111.json").write_text(json.dumps({"pid": 111, "sessionId": "dead", "name": "죽음"}), encoding="utf-8")
    (d / "222.json").write_text(json.dumps({"pid": 222, "sessionId": "live", "name": "삶"}), encoding="utf-8")
    monkeypatch.setattr(peers, "_gone", lambda pid, peer: pid == 111)
    monkeypatch.setattr(peers, "origin_of", lambda *a, **k: {"origin": "unknown", "parent": None})
    peers._cache["at"] = 0
    assert set(peers.peer_map()) == {"live"}
