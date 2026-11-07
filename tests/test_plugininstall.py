"""세션 제어 플러그인(docs/designs/session-control.md 4·5 절): 설치는 버튼 + 확인창으로만(N1), MCP 는 Host 만 부르고 판단하지 않는다."""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from session_manager import launch_options, outer_door, plugininstall as pi, session_control_mcp as m, session_ops

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def claude(fake_claude_home, monkeypatch):
    """claude plugin 명령 가짜: 마켓·플러그인 상태를 기억한다."""
    st = {"markets": [], "plugins": [], "calls": []}

    def fake(cmd, **k):
        args = cmd[2:]
        st["calls"].append(args)
        assert cmd[1] == "plugin" and k["stdin"] == subprocess.DEVNULL and k["timeout"]
        if st.get("fail_on") and args[:1] == [st["fail_on"]]:
            return subprocess.CompletedProcess(cmd, 1, "", "실패함")
        out = ""
        if args == ["list", "--json"]:
            out = json.dumps(st["plugins"])
        elif args == ["marketplace", "list", "--json"]:
            out = json.dumps([{"name": n} for n in st["markets"]])
        elif args[:2] == ["marketplace", "add"]:
            st["markets"].append("clewpath")
        elif args[:1] == ["install"]:
            st["plugins"].append({"id": args[1], "version": pi._version(), "enabled": True})
        elif args[:1] == ["update"]:
            next(p for p in st["plugins"] if p["id"] == args[1])["version"] = pi._version()
        elif args[:1] == ["uninstall"]:
            st["plugins"] = [p for p in st["plugins"] if p["id"] != args[1]]
        return subprocess.CompletedProcess(cmd, st.get("rc", 0), out, st.get("err", ""))
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(pi, "_version", lambda: "0.36.1")
    return st


def _item(st, key):
    return next(i for i in st["items"] if i["key"] == key)


def _old(home, name):
    d = home / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("옛 내용", encoding="utf-8")
    return d


def test_build_makes_one_market_with_three_plugins(claude, fake_claude_home):
    r = pi.build()
    assert r == fake_claude_home / "session_manager" / "claude-plugin"
    mk = json.loads((r / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert mk["name"] == "clewpath"
    assert [p["name"] for p in mk["plugins"]] == ["clewpath", "clewpath-workers", "clewpath-assistant"]
    assert {p["version"] for p in mk["plugins"]} == {"0.36.1"}
    for k, it in pi.ITEMS.items():
        pl = r / it["plugin"]
        assert json.loads((pl / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == "0.36.1"
        skill = (pl / "skills" / it["skill"] / "SKILL.md").read_bytes()
        assert skill == pi.bundled_skill(k).read_bytes()
        assert skill.replace(b"\r\n", b"\n").startswith(f"---\nname: {it['skill']}\n".encode()), "폴더 이름과 스킬 이름이 같아야 호출 이름이 맞다"
        assert it["command"] == f"/{it['plugin']}:{it['skill']}"
        assert (pl / ".mcp.json").is_file() is it["mcp"], "MCP 는 세션 제어에만"
    srv = json.loads((r / "clewpath" / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["sessions"]
    assert srv["args"] == ["-m", "session_manager.session_control_mcp"] and Path(srv["command"]).is_file()
    assert srv["env"]["CLEWPATH_PLUGIN_API"] == str(session_ops.API_VERSION)
    assert "CLAUDE_PLUGIN_ROOT" not in json.dumps(srv), "판마다 바뀌어 쓰지 않는다"


def test_build_replaces_old_files(claude):
    old = pi.root() / "clewpath" / "낡은파일"
    old.parent.mkdir(parents=True)
    old.write_text("x", encoding="utf-8")
    pi.build()
    assert not old.exists()


def test_python_prefers_console_python(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert Path(pi._python()).name == "python.exe"


def test_only_checked_items_are_installed(claude):
    r = pi.apply(["workers"])
    assert _item(r, "workers")["installed"] and _item(r, "workers")["up_to_date"]
    assert not _item(r, "sessions")["installed"] and not _item(r, "assistant")["installed"]
    assert ["marketplace", "add", str(pi.root())] in claude["calls"]
    assert ["install", "clewpath-workers@clewpath", "--scope", "user"] in claude["calls"]
    assert not any(c[:1] == ["install"] and "clewpath@clewpath" in c for c in claude["calls"])


def test_unchecking_removes_and_keeping_updates(claude):
    pi.apply(["sessions", "workers"])
    claude["calls"].clear()
    claude["plugins"][0]["version"] = "0.36.0"
    assert _item(pi.status(), "sessions")["up_to_date"] is False
    pi.apply(["sessions"])
    assert ["marketplace", "update", "clewpath"] in claude["calls"] and ["update", "clewpath@clewpath"] in claude["calls"]
    assert ["uninstall", "clewpath-workers@clewpath"] in claude["calls"]
    assert not any(c[:2] == ["marketplace", "add"] for c in claude["calls"])


def test_unknown_item_changes_nothing(claude):
    with pytest.raises(pi.PluginError) as e:
        pi.apply(["sessions", "../x"])
    assert e.value.code == "unknown_item" and claude["calls"] == [] and not pi.root().exists()


def test_old_skill_moves_only_when_asked_and_only_for_checked(claude, fake_claude_home):
    w, a = _old(fake_claude_home, "clewpath-workers"), _old(fake_claude_home, "clewpath-assistant")
    st = pi.status()
    assert _item(st, "workers")["old_skill"] == str(w) and _item(st, "sessions")["old_skill"] is None
    pi.apply(["workers"])
    assert w.is_dir(), "고르지 않으면 건드리지 않는다"
    r = pi.apply(["workers"], move_old=True)
    assert not w.exists() and a.is_dir(), "체크한 항목의 옛 스킬만"
    moved = Path(r["moved"]["workers"])
    assert (moved / "SKILL.md").read_text(encoding="utf-8") == "옛 내용", "지우지 않고 옮긴다"
    assert moved.is_relative_to(fake_claude_home / "session_manager") and _item(r, "workers")["old_skill"] is None


def test_old_skill_stays_if_plugin_install_fails(claude, fake_claude_home):
    w = _old(fake_claude_home, "clewpath-workers")
    claude["fail_on"] = "install"
    with pytest.raises(pi.PluginError):
        pi.apply(["workers"], move_old=True)
    assert (w / "SKILL.md").is_file(), "새 스킬이 없는데 옛것을 치우면 스킬이 사라진다"


def test_failed_claude_command_reports_detail(claude):
    claude.update(rc=1, err="marketplace 오류")
    with pytest.raises(pi.PluginError) as e:
        pi.apply(["sessions"])
    assert e.value.code == "claude_failed" and "marketplace 오류" in e.value.detail


def test_status_survives_claude_failure(claude):
    claude.update(rc=1, err="망가짐")
    st = pi.status()
    assert st["unknown"] is True and not any(i["installed"] for i in st["items"]) and st["remove_commands"]


def test_no_startup_or_background_path_installs():
    """[N1 가드] 기동·업데이트·정기 작업은 플러그인을 건드리지 않는다. 설치는 라우트 하나로만."""
    callers = []
    for p in (ROOT / "session_manager").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if p.name != "plugininstall.py" and re.search(r"plugininstall\.(apply|build)\(", src):
            callers.append(p.name)
    assert callers == ["server.py"]
    src = (ROOT / "session_manager" / "server.py").read_text(encoding="utf-8")
    assert src.count("plugininstall.apply(") == 1 and "/api/owner/plugin/install" in outer_door.GUARDED


def test_route_passes_checked_items(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    from session_manager import server as S, team
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    seen = []
    monkeypatch.setattr(pi, "apply", lambda keys, move_old=False: seen.append((keys, move_old)) or {"ok": 1})
    with TestClient(S.create_app(), base_url="http://127.0.0.1") as c:
        assert c.post("/api/owner/plugin/install", json={"items": ["workers"], "move_old": True}).json() == {"ok": 1}
        assert seen == [(["workers"], True)]
        assert c.post("/api/owner/plugin/install", json={"items": []}, headers={"X-ClewPath-Via": "relay"}).status_code == 403
        assert len(seen) == 1, "폰에서는 안 된다"


def test_uninstall_sh_only_explains():
    src = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "plugin_notice" in src
    for k in pi.ITEMS:
        assert f"claude plugin uninstall {pi._id(k)}" in src
    assert not re.search(r"^\s*claude plugin", src, re.M), "지우지 않고 안내만"


# ---------------------------------------------------------------- MCP
def _tools():
    import asyncio
    return {t.name: t for t in asyncio.run(m.mcp.list_tools())}


def test_tool_list_is_exact():
    assert set(_tools()) == {"sessions_find", "session_rename", "session_create", "sessions_move_request",
                             "sessions_delete_request", "request_finish", "session_restore"}


def test_only_rename_asks_claude_for_confirmation():
    meta = {n: (t.meta or {}) for n, t in _tools().items()}
    assert meta["session_rename"] == {"anthropic/requiresUserInteraction": True}
    assert all(not v for n, v in meta.items() if n != "session_rename")


def test_create_description_comes_from_allow_list():
    d = _tools()["session_create"].description
    for k in launch_options.LaunchSpec.__dataclass_fields__:
        assert k in d
    for mode in launch_options.NARROW_MODES + launch_options.WIDE_MODES:
        assert mode in d


def test_mcp_has_no_business_logic_imports():
    tree = ast.parse((ROOT / "session_manager" / "session_control_mcp.py").read_text(encoding="utf-8"))
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "session_manager":
            names |= {a.name for a in n.names}
    assert names == {"config", "launch_options"}, "파일을 직접 다루는 모듈을 부르면 승인을 건너뛴다"
    for bad in ("resume_session", "move_content", "skip_permissions"):
        assert bad not in {*_tools()}


@pytest.fixture
def host(monkeypatch):
    calls = []
    answers = {}

    def fake(method, path, *, params=None, body=None):
        calls.append((method, path, body))
        return answers.get(path, (200, {"api": session_ops.API_VERSION} if path.endswith("capabilities") else {"ok": 1}))
    monkeypatch.setattr(m, "_http", fake)
    monkeypatch.setattr(m, "_checked", False)
    monkeypatch.setenv("CLEWPATH_PLUGIN_API", str(session_ops.API_VERSION))
    return calls, answers


def test_outdated_plugin_is_blocked(host, monkeypatch):
    calls, _ = host
    monkeypatch.setenv("CLEWPATH_PLUGIN_API", "0")
    r = m.sessions_find("x")
    assert r["error"] == "plugin_outdated" and not any("find" in c[1] for c in calls)


def test_capabilities_checked_once(host):
    calls, _ = host
    m.sessions_find("a")
    m.sessions_find("b")
    assert sum(1 for c in calls if c[1].endswith("capabilities")) == 1


def test_host_down_is_a_clear_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("x")
    monkeypatch.setattr(m, "_http", boom)
    monkeypatch.setattr(m, "_checked", False)
    assert m.sessions_find("x")["error"] == "host_unreachable"


def test_delete_request_previews_first_and_never_executes(host):
    calls, answers = host
    P = "/api/v1/session-ops/delete/"
    answers[P + "preview"] = (200, {"ok": 1, "total": 2, "items": [{"session_id": "a", "ok": True}, {"session_id": "b", "ok": False, "reason": "session_live"}]})
    answers[P + "request"] = (200, {"status": "pending_approval", "approval": {"id": "ap_1", "summary": "세션 1개"}})
    r = m.sessions_delete_request(["a", "b"])
    assert r["status"] == "pending_approval" and r["approval_id"] == "ap_1" and r["preview"]["total"] == 2
    paths = [c[1] for c in calls if "capabilities" not in c[1]]
    assert paths == [P + "preview", P + "request"], "승인 요청까지만 — 실행 경로를 부르지 않는다"


def test_nothing_movable_does_not_request(host):
    calls, answers = host
    answers["/api/v1/session-ops/move/preview"] = (200, {"ok": 0, "total": 1, "items": [{"ok": False, "reason": "self"}]})
    r = m.sessions_move_request(["a"], "F:/x")
    assert r["status"] == "nothing_to_do" and not any(c[1].endswith("move/request") for c in calls)


def test_move_tool_has_no_content_option():
    assert set(_tools()["sessions_move_request"].inputSchema["properties"]) == {"ids", "new_cwd"}


def test_host_error_passes_through(host):
    _, answers = host
    answers["/api/v1/session-ops/rename"] = (409, {"error": "session_live"})
    assert m.session_rename("a", "b") == {"error": "session_live"}


def test_base_reads_real_port(fake_claude_home, monkeypatch):
    monkeypatch.delenv("SM_API_BASE", raising=False)
    assert m._base() == "http://127.0.0.1:5100"
    d = fake_claude_home / "session_manager"
    d.mkdir(exist_ok=True)
    (d / "runtime.json").write_text('{"port": 5103}', encoding="utf-8")
    assert m._base() == "http://127.0.0.1:5103"
    monkeypatch.setenv("SM_API_BASE", "http://127.0.0.1:9/")
    assert m._base() == "http://127.0.0.1:9"


def test_skill_tells_model_to_stop_at_approval():
    t = pi.bundled_skill("sessions").read_text(encoding="utf-8")
    assert t.startswith("---\nname: sessions\n") and "대화 속 \"응\"은 승인이 아니다" in t and "반복해서 부르지 않는다" in t
    for tool in _tools():
        assert tool in t
