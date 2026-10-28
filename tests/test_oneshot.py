"""세션 없는 단발 실행(2026-10-28, 번역 서비스 요청) — POST /api/v1/oneshot · webapi.oneshot · 서비스 oneshot 표시.

이어받을 세션 없이 claude -p 를 한 번 돌린다: PC 사이 묻기와 같은 격리(도구·MCP·훅 없음, 기록 안 남김, 권한 건너뛰기 없음),
비어 있는 고정 폴더에서, --system-prompt 로 시스템 프롬프트를 바꾼다. 서비스 토큰은 oneshot 이 켜진 것만 부른다.
"""
import json
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from session_manager import server as S, config, lifecycle, osdep, policy, services, webapi

EXE = "C:/fake/bin/claude.exe"
ISO_FLAGS = ["--no-session-persistence", "--tools", "", "--strict-mcp-config",
             "--settings", '{"disableAllHooks":true}']


def _text(t):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _tool(name):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu1", "name": name, "input": {}}]}}


def _result(t, cost=0.002, is_error=False):
    return {"type": "result", "result": t, "total_cost_usd": cost, "is_error": is_error}


class FakeProc:
    def __init__(self, argv, st, **kw):
        self.args, self.kw, self.st = argv, kw, st
        self.pid = 5555
        self.returncode = None
        self.killed = False
        self.input = None

    def communicate(self, input=None, timeout=None):  # noqa: A002
        self.input = input
        sc = self.st["script"]
        if sc.get("hang") and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = -9 if self.killed else 0
        if self.killed:
            return b"", b""
        out = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sc.get("events", []))
        return out.encode("utf-8"), b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.fixture
def run(fake_claude_home, monkeypatch):
    st = {"procs": [], "guarded": [], "deleted": [], "persisted": False,
          "script": {"events": [_text("안녕하세요"), _result("안녕하세요")]}}
    real_popen = subprocess.Popen

    def fake_popen(argv, **kw):
        if not (isinstance(argv, (list, tuple)) and argv and argv[0] == EXE):
            return real_popen(argv, **kw)
        p = FakeProc(argv, st, **kw)
        st["procs"].append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(webapi.shutil, "which", lambda name, *a, **k: EXE)
    monkeypatch.setattr(osdep, "guard_child", lambda pid: st["guarded"].append(pid) or True)
    monkeypatch.setattr(webapi, "_fork_jsonl_exists", lambda sid: st["persisted"])
    monkeypatch.setattr(lifecycle, "delete_fork", lambda sid, origin=None, wait_live_s=3.0:
                        st["deleted"].append(sid) or {"deleted": ["(가짜)"]})
    return st


def _sid(argv):
    return argv[argv.index("--session-id") + 1]


# ---- webapi.oneshot ----

def test_argv_isolated_no_resume_no_skip(run):
    r = webapi.oneshot("번역해", model="claude-sonnet-5-5", system="너는 번역기다", effort="low",
                       timeout=30, max_budget_usd=0.5)
    p = run["procs"][0]
    sid = _sid(p.args)
    assert p.args == [EXE, "-p", "번역해", "--session-id", sid, "--output-format", "stream-json", "--verbose",
                      *ISO_FLAGS, "--model", "claude-sonnet-5-5", "--system-prompt", "너는 번역기다",
                      "--effort", "low", "--max-budget-usd", "0.5"]
    assert "--resume" not in p.args and "--dangerously-skip-permissions" not in p.args
    assert p.kw["cwd"] == str(config.data_dir() / "oneshot") and not any((config.data_dir() / "oneshot").iterdir())
    assert run["guarded"] == [5555]
    assert r == {"answer": "안녕하세요", "is_error": False, "model": "claude-sonnet-5-5",
                 "duration_s": r["duration_s"], "cost_usd": 0.002}
    assert run["deleted"] == []


def test_optional_args_left_out(run):
    webapi.oneshot("q", model="claude-haiku-5-5")
    a = run["procs"][0].args
    assert "--system-prompt" not in a and "--effort" not in a and "--max-budget-usd" not in a
    assert "--append-system-prompt" not in a


def test_dash_prompt_not_read_as_option(run):
    webapi.oneshot("--tools=Bash", model="claude-haiku-5-5")
    assert run["procs"][0].args[2] == " --tools=Bash"


def test_long_prompt_goes_to_stdin(run):
    big = "가" * (webapi.ARGV_PROMPT_MAX + 10)
    webapi.oneshot(big, model="claude-haiku-5-5")
    p = run["procs"][0]
    assert big not in p.args and p.args[1] == "-p" and p.args[2] == "--session-id"
    assert p.input == big.encode("utf-8") and p.kw["stdin"] == subprocess.PIPE


def test_tool_use_discards_answer(run):
    run["script"]["events"] = [_tool("Bash"), _text("비밀"), _result("비밀")]
    r = webapi.oneshot("q", model="claude-haiku-5-5")
    assert r["answer"] == "" and r["is_error"] is True and r["isolation_breach"] is True


def test_persisted_record_is_deleted(run):
    run["persisted"] = True
    r = webapi.oneshot("q", model="claude-haiku-5-5")
    assert r["persisted"] is True and run["deleted"] == [_sid(run["procs"][0].args)]


def test_timeout_kills(run):
    run["script"]["hang"] = True
    r = webapi.oneshot("q", model="claude-haiku-5-5", timeout=7)
    assert r["is_error"] is True and r["answer"] == "시간초과(7s)" and run["procs"][0].killed


# ---- 서비스 oneshot 표시 ----

def test_service_oneshot_without_bases(fake_claude_home):
    rec, tok = services.create("mt-translator", "번역", {}, oneshot=True)
    assert rec["oneshot"] is True and rec["bases"] == {}
    assert services.http_allowed(rec, "POST", "/api/v1/oneshot")
    assert not services.http_allowed(rec, "GET", "/api/v1/oneshot")
    assert not services.http_allowed(rec, "GET", "/api/v1/sessions")
    with pytest.raises(services.ServiceError):
        services.create("plain", "x", {})                      # oneshot 아니면 원본 필수(그대로)
    with pytest.raises(services.ServiceError):
        services.update("mt-translator", oneshot=False)        # 원본 없이 oneshot 을 끄면 아무 것도 못 하는 토큰
    assert services.update("mt-translator", status="suspended")["status"] == "suspended"


# ---- POST /api/v1/oneshot ----

@pytest.fixture
def api(fake_claude_home, monkeypatch):
    calls = []

    def fake_oneshot(prompt, **k):
        calls.append((prompt, k))
        return {"answer": "ok", "is_error": False, "model": k["model"], "duration_s": 1.2, "cost_usd": 0.01}
    monkeypatch.setattr(S.webapi, "oneshot", fake_oneshot)
    monkeypatch.setattr(S, "_is_local", lambda x: True)
    return TestClient(S.create_app()), calls


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def _audit():
    f = policy._audit_file()
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []


def test_local_call_and_audit_has_no_body(api):
    client, calls = api
    r = client.post("/api/v1/oneshot", json={"prompt": "비밀 원문", "system": "시스템 원문",
                                             "model": "claude-sonnet-5-5", "effort": "low"})
    assert r.status_code == 200 and r.json()["answer"] == "ok"
    prompt, k = calls[0]
    assert prompt == "비밀 원문" and k["system"] == "시스템 원문" and k["effort"] == "low" and k["timeout"] == 60
    ev = [e for e in _audit() if e.get("event") == "oneshot"]
    assert ev and ev[-1]["model"] == "claude-sonnet-5-5" and ev[-1]["cost_usd"] == 0.01
    raw = policy._audit_file().read_text(encoding="utf-8")
    assert "비밀 원문" not in raw and "시스템 원문" not in raw


@pytest.mark.parametrize("body,code,err", [
    ({"prompt": "q", "model": "gpt-9"}, 400, "bad_model"),
    ({"prompt": "q", "model": "claude-sonnet-5-5", "effort": "max"}, 400, "bad_effort"),
    ({"prompt": "  ", "model": "claude-sonnet-5-5"}, 400, "empty_prompt"),
    ({"prompt": "가" * (S.ONESHOT_MAX_CHARS + 1), "model": "claude-sonnet-5-5"}, 413, "too_long"),
])
def test_rejects_bad_input(api, body, code, err):
    client, calls = api
    r = client.post("/api/v1/oneshot", json=body)
    assert r.status_code == code and r.json()["error"] == err and not calls


def test_timeout_clamped(api):
    client, calls = api
    client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5", "timeout": 9999})
    client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5", "timeout": 1})
    client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5", "timeout": 0})   # 0 = 기본
    assert [k["timeout"] for _, k in calls] == [300, 5, 60]


def test_service_token_needs_oneshot_flag(api, monkeypatch):
    client, calls = api
    _, plain = services.create("plain", "x", {}, oneshot=True)
    services.update("plain", status="active")
    # oneshot 이 꺼진 서비스(원본 있음)를 흉내 — 장부를 직접 고친다
    d = services._load()
    d["services"]["plain"]["oneshot"] = False
    services._save(d)
    for local in (True, False):
        monkeypatch.setattr(S, "_is_local", lambda x, v=local: v)
        r = client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5"}, headers=_h(plain))
        assert r.status_code == 403 and r.json()["error"] == "service_scope"
    assert not calls


def test_service_token_remote_ok_and_counted(api, monkeypatch):
    client, calls = api
    _, tok = services.create("mt-translator", "번역", {}, oneshot=True, limits={"max_call_usd": 0.3})
    monkeypatch.setattr(S, "_is_local", lambda x: False)            # 컨테이너(host.docker.internal) = 원격
    r = client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-haiku-5-5"}, headers=_h(tok))
    assert r.status_code == 200
    assert calls[0][1]["max_budget_usd"] == 0.3
    svc = services.identify(tok)
    assert policy.today_usage(tok, svc)["calls"] == 1
    assert [e for e in _audit() if e.get("event") == "oneshot"][-1]["service"] == "mt-translator"
    # 다른 경로는 여전히 막힌다
    assert client.get("/api/v1/sessions", headers=_h(tok)).status_code == 403


def test_busy_when_slots_full(api, monkeypatch):
    client, calls = api
    monkeypatch.setattr(S, "_oneshot_slots", threading.BoundedSemaphore(1))
    S._oneshot_slots.acquire()
    try:
        r = client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5"})
        assert r.status_code == 429 and r.json()["error"] == "busy" and not calls
    finally:
        S._oneshot_slots.release()
    assert client.post("/api/v1/oneshot", json={"prompt": "q", "model": "claude-sonnet-5-5"}).status_code == 200
