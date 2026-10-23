"""pclink T6 — ephemeral_ask 격리 옵션(도구·MCP·훅·기록 없는 복제본)과 Popen 전환.

다른 PC 의 '묻기' 는 이 PC 세션의 복제본에 도구·MCP·훅 없이, 기록도 남기지 않고 묻는다:
    claude -p <질문> --resume X --fork-session --session-id F --no-session-persistence
           --tools "" --strict-mcp-config --settings {"disableAllHooks":true}
기존 호출처(/api/v1/sessions/{id}/ask · 릴레이 ask)의 argv 는 한 글자도 바뀌면 안 된다.

subprocess.Popen(모듈 속성)을 가짜로 바꿔 argv 를 잡고 stream-json 줄을 돌려준다. subprocess.run 도 안에서
같은 Popen 을 쓰므로, 고치기 전 코드(subprocess.run)로도 '기본 argv 불변' 시험이 그대로 돈다(고치기 전 박아 둔 기준).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from session_manager import lifecycle, osdep, webapi
from tests.conftest import write_session

SID = "9a9a9a9a-1111-2222-3333-444444444444"
EXE = "C:/fake/bin/claude.exe"
FULL_G = {"allowed_tools": ["Read", "Grep"], "denied_tools": ["Bash"], "permission_mode": "plan",
          "model": "claude-haiku-4-5", "system_prompt": "짧게 답하라", "max_budget_usd": 0.5}
ISO_FLAGS = ["--no-session-persistence", "--tools", "", "--strict-mcp-config",
             "--settings", '{"disableAllHooks":true}']


def _text(t):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _tool(name):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "tu1", "name": name, "input": {}}]}}


def _result(t, cost=0.001, is_error=False):
    return {"type": "result", "result": t, "total_cost_usd": cost, "is_error": is_error}


class FakeProc:
    """subprocess.Popen 흉내 — communicate 에서 대본(events)을 stream-json 줄로 돌려준다."""

    def __init__(self, argv, st, **kw):
        self.args, self.kw, self.st = argv, kw, st
        self.pid = 4321
        self.returncode = None
        self.killed = False
        self.comm_timeouts = []

    def communicate(self, input=None, timeout=None):  # noqa: A002
        self.comm_timeouts.append(timeout)
        sc = self.st["script"]
        if sc.get("hang") and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.killed:
            self.returncode = -9
            return b"", b""
        if sc.get("on_run"):
            sc["on_run"](self)
        self.returncode = 0
        out = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in sc.get("events", []))
        return out.encode("utf-8"), b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self):
        self.killed = True

    terminate = kill

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(fake_claude_home, tmp_path, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    write_session(fake_claude_home, "F--work", SID, [
        {"type": "user", "cwd": str(cwd), "slug": "s", "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-10-23T00:00:00Z"}])
    st = {"home": fake_claude_home, "cwd": str(cwd), "procs": [], "deleted": [], "guarded": [],
          "script": {"events": [_text("청포도"), _result("청포도")]}}

    real_popen = subprocess.Popen

    def fake_popen(argv, **kw):
        # claude 실행만 잡는다 — subprocess.Popen 은 표준 모듈 그 자체라, 앞 시험 앱의 배경 스레드(orphan-sweep 등)가 부르는
        # 다른 명령까지 여기로 와 procs 에 섞이면 argv 단언이 가끔 틀어졌다(0.32.3 출시 시험에서 실측)
        if not (isinstance(argv, (list, tuple)) and argv and argv[0] == EXE):
            return real_popen(argv, **kw)
        p = FakeProc(argv, st, **kw)
        st["procs"].append(p)
        return p

    def fake_delete_fork(fid, origin=None, wait_live_s=3.0):
        p = st["procs"][-1] if st["procs"] else None
        st["deleted"].append({"fork_id": fid, "origin": origin, "proc_done": bool(p and p.returncode is not None)})
        return {"session_id": fid, "deleted": ["(가짜)"]}

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(webapi.shutil, "which", lambda name, *a, **k: EXE)
    monkeypatch.setattr(lifecycle, "delete_fork", fake_delete_fork)
    monkeypatch.setattr(osdep, "guard_child", lambda pid: st["guarded"].append(pid) or True)
    return st


def _base(fork_id, prompt="질문"):
    return [EXE, "-p", prompt, "--resume", SID, "--fork-session", "--session-id", fork_id,
            "--output-format", "stream-json", "--verbose"]


# ---- 기본(isolated=False) 동작 불변 — 고치기 전 main 의 argv 그대로 ----

def test_default_argv_unchanged_no_guardrails(env):
    r = webapi.ephemeral_ask(SID, "질문", skip_permissions=True, timeout=30, guardrails={})
    p = env["procs"][0]
    assert p.args == _base(r["fork_id"]) + ["--dangerously-skip-permissions"]
    assert p.kw["cwd"] == env["cwd"]
    assert r["answer"] == "청포도" and r["result"] == "청포도" and r["cost_usd"] == 0.001
    assert r["is_error"] is False and r["tool_uses"] == [] and r["fork_deleted"] is True
    assert r["session_id"] == SID


def test_default_argv_unchanged_full_guardrails(env):
    r = webapi.ephemeral_ask(SID, "질문", skip_permissions=True, timeout=30, guardrails=FULL_G)
    assert env["procs"][0].args == _base(r["fork_id"]) + [
        "--allowed-tools", "Read Grep", "--disallowed-tools", "Bash", "--model", "claude-haiku-4-5",
        "--append-system-prompt", "짧게 답하라", "--permission-mode", "plan"]   # 이어받기엔 --max-budget-usd 없음(과거 누적과 비교돼 엉뚱하게 실패)


def test_default_argv_unchanged_no_skip(env):
    r = webapi.ephemeral_ask(SID, "-앞이 대시인 질문", skip_permissions=False, timeout=30)
    assert env["procs"][0].args == _base(r["fork_id"], "-앞이 대시인 질문")     # 기본 경로는 질문도 손대지 않는다


def test_default_deletes_fork_after_process_ends(env):
    r = webapi.ephemeral_ask(SID, "질문", timeout=30)
    assert env["deleted"] == [{"fork_id": r["fork_id"], "origin": SID, "proc_done": True}]
    assert "fork_persisted" not in r and "isolation_breach" not in r


def test_default_tool_use_is_not_breach(env):
    env["script"]["events"] = [_tool("Bash"), _text("답"), _result("답")]
    r = webapi.ephemeral_ask(SID, "질문", timeout=30)
    assert r["tool_uses"] == ["Bash"] and "isolation_breach" not in r


# ---- isolated=True ----

def test_isolated_argv(env):
    r = webapi.ephemeral_ask(SID, "질문", skip_permissions=True, timeout=30, guardrails=FULL_G, isolated=True)
    argv = env["procs"][0].args
    assert argv == _base(r["fork_id"]) + ISO_FLAGS + [
        "--model", "claude-haiku-4-5", "--append-system-prompt", "짧게 답하라"]
    assert argv[argv.index("--tools") + 1] == ""                     # 빈 문자열 인자 = 도구 0개
    for bad in ("--dangerously-skip-permissions", "--allowed-tools", "--disallowed-tools", "--permission-mode"):
        assert bad not in argv
    assert r["answer"] == "청포도"


def test_isolated_ignores_skip_permissions_flag(env):
    webapi.ephemeral_ask(SID, "질문", skip_permissions=True, timeout=30, guardrails={}, isolated=True)
    webapi.ephemeral_ask(SID, "질문", skip_permissions=False, timeout=30, guardrails=None, isolated=True)
    for p in env["procs"]:
        assert "--dangerously-skip-permissions" not in p.args
        assert p.args[11:] == ISO_FLAGS


def test_isolated_dash_prompt_is_not_an_option(env):
    # 원격 질문이 '-' 로 시작하면 claude 가 옵션으로 읽는다(--tools=Bash 등) — 앞에 공백을 붙여 질문으로만 읽게
    r = webapi.ephemeral_ask(SID, "--tools=Bash", timeout=30, isolated=True)
    argv = env["procs"][0].args
    assert argv[2] == " --tools=Bash" and "--tools=Bash" not in argv
    assert r["fork_id"]


def test_isolated_skips_delete_fork_when_nothing_persisted(env):
    r = webapi.ephemeral_ask(SID, "질문", timeout=30, isolated=True)
    assert env["deleted"] == []
    assert "fork_persisted" not in r and r["fork_deleted"] is False


def test_isolated_deletes_fork_if_jsonl_appeared(env):
    def _persist(proc):          # 미래 claude 가 --no-session-persistence 를 무시하고 기록을 남긴 척
        fid = proc.args[proc.args.index("--session-id") + 1]
        write_session(env["home"], "F--work", fid, [
            {"type": "user", "cwd": env["cwd"], "message": {"role": "user", "content": "질문"},
             "timestamp": "2026-10-23T00:00:01Z"}])
    env["script"]["on_run"] = _persist
    r = webapi.ephemeral_ask(SID, "질문", timeout=30, isolated=True)
    assert env["deleted"] == [{"fork_id": r["fork_id"], "origin": SID, "proc_done": True}]
    assert r["fork_persisted"] is True and r["fork_deleted"] is True


def test_isolated_tool_use_marks_breach(env):
    env["script"]["events"] = [_tool("Bash"), _text("답"), _result("답")]
    r = webapi.ephemeral_ask(SID, "질문", timeout=30, isolated=True)
    assert r["tool_uses"] == ["Bash"] and r["isolation_breach"] is True


def test_isolated_clean_answer_has_no_breach_key(env):
    r = webapi.ephemeral_ask(SID, "질문", timeout=30, isolated=True)
    assert r["tool_uses"] == [] and "isolation_breach" not in r


# ---- 프로세스 다루기(Popen + Job 편입 + 시간 초과) ----

def test_guard_child_called_with_pid(env):
    webapi.ephemeral_ask(SID, "질문", timeout=30)
    webapi.ephemeral_ask(SID, "질문", timeout=30, isolated=True)
    assert env["guarded"] == [4321, 4321]
    p = env["procs"][0]
    assert p.kw.get("stdout") == subprocess.PIPE and p.comm_timeouts[0] == 30
    assert p.kw.get("creationflags", 0) == osdep.NO_WINDOW


@pytest.mark.parametrize("isolated", [False, True])
def test_timeout_kills_then_cleans(env, isolated):
    env["script"]["hang"] = True
    r = webapi.ephemeral_ask(SID, "질문", timeout=7, isolated=isolated)
    p = env["procs"][0]
    assert p.killed is True and p.returncode is not None
    assert r["is_error"] is True and r["result"] == "시간초과(7s)" and r["answer"] == "시간초과(7s)"
    if isolated:
        assert env["deleted"] == []
    else:
        assert env["deleted"] == [{"fork_id": r["fork_id"], "origin": SID, "proc_done": True}]


def test_duration_and_existing_keys(env):
    r = webapi.ephemeral_ask(SID, "질문", timeout=30)
    for k in ("session_id", "fork_id", "answer", "result", "cost_usd", "is_error", "tool_uses", "fork_deleted"):
        assert k in r
    assert isinstance(r["duration_s"], float) and r["duration_s"] >= 0
    assert round(r["duration_s"], 2) == r["duration_s"]


def test_missing_cwd_returns_error_without_spawn(env):
    r = webapi.ephemeral_ask("ffffffff-0000-0000-0000-000000000000", "질문", isolated=True)
    assert "error" in r and env["procs"] == []


def test_spawn_failure_still_cleans_default_fork(env, monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError("claude 없음")
    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(FileNotFoundError):
        webapi.ephemeral_ask(SID, "질문", timeout=30)
    assert len(env["deleted"]) == 1                   # 고치기 전과 같이 예외는 올리고 fork 정리는 시도
