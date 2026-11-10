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


def _old(home, name):
    d = home / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("옛 내용", encoding="utf-8")
    return d


def test_build_one_plugin_with_three_skills_and_mcp(claude, fake_claude_home):
    r = pi.build()
    assert r == fake_claude_home / "session_manager" / "claude-plugin"
    mk = json.loads((r / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert mk["name"] == "clewpath" and [p["name"] for p in mk["plugins"]] == ["clewpath"] and mk["plugins"][0]["version"] == "0.36.1"
    pl = r / "clewpath"
    assert json.loads((pl / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == "0.36.1"
    assert sorted(p.name for p in (pl / "skills").iterdir()) == ["assistant", "sessions", "workers"]
    for name in pi.SKILLS:
        skill = (pl / "skills" / name / "SKILL.md").read_bytes()
        assert skill == pi.bundled_skill(name).read_bytes()
        assert skill.replace(b"\r\n", b"\n").startswith(f"---\nname: {name}\n".encode()), "폴더 이름과 스킬 이름이 같아야 호출 이름이 맞다"
    srv = json.loads((pl / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["sessions"]
    assert srv["args"] == ["-m", "session_manager.session_control_mcp"] and Path(srv["command"]).is_file()
    assert srv["env"]["CLEWPATH_PLUGIN_API"] == str(session_ops.API_VERSION)
    assert "CLAUDE_PLUGIN_ROOT" not in json.dumps(srv), "판마다 바뀌어 쓰지 않는다"


def test_build_replaces_old_files(claude):
    old = pi.root() / "clewpath-workers" / "낡은파일"
    old.parent.mkdir(parents=True)
    old.write_text("x", encoding="utf-8")
    pi.build()
    assert not old.parent.exists(), "0.36.1 의 따로 묶음 폴더도 남지 않는다"


def test_python_prefers_console_python(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("")
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    assert Path(pi._python()).name == "python.exe"


def test_install_all_at_once_then_update(claude):
    r = pi.install()
    assert r["installed"] and r["up_to_date"] and r["version"] == "0.36.1"
    assert ["marketplace", "add", str(pi.root())] in claude["calls"] and ["install", "clewpath@clewpath", "--scope", "user"] in claude["calls"]
    assert sum(1 for c in claude["calls"] if c[:1] == ["install"]) == 1, "한 번에 하나"
    claude["calls"].clear()
    claude["plugins"][0]["version"] = "0.36.0"
    assert pi.status()["up_to_date"] is False
    pi.install()
    assert ["marketplace", "update", "clewpath"] in claude["calls"] and ["update", "clewpath@clewpath"] in claude["calls"]
    assert not any(c[:1] == ["install"] or c[:2] == ["marketplace", "add"] for c in claude["calls"])


def test_merges_0361_separate_plugins(claude):
    """0.36.1 에서 따로 설치한 워커·비서 플러그인은 설치 때 제거해 하나로 합친다(같은 스킬이 둘이 되지 않게)."""
    claude["markets"].append("clewpath")
    claude["plugins"] += [{"id": "clewpath@clewpath", "version": "0.36.1"}, {"id": "clewpath-workers@clewpath", "version": "0.36.1"},
                          {"id": "clewpath-assistant@clewpath", "version": "0.36.1"}]
    assert pi.status()["legacy"] == list(pi.LEGACY_IDS)
    r = pi.install()
    assert r["removed"] == list(pi.LEGACY_IDS) and r["legacy"] == []
    assert [c for c in claude["calls"] if c[:1] == ["uninstall"]] == [["uninstall", i] for i in pi.LEGACY_IDS]


def test_old_skills_move_after_install(claude, fake_claude_home):
    w, a = _old(fake_claude_home, "clewpath-workers"), _old(fake_claude_home, "clewpath-assistant")
    other = _old(fake_claude_home, "deploy-request")
    st = pi.status()
    assert {x["name"]: bool(x["old_skill"]) for x in st["skills"]} == {"sessions": False, "workers": True, "assistant": True}
    r = pi.install()
    assert not w.exists() and not a.exists() and other.is_dir(), "ClewPath 스킬만 옮긴다"
    moved = Path(r["moved"]["workers"])
    assert (moved / "SKILL.md").read_text(encoding="utf-8") == "옛 내용", "지우지 않고 옮긴다"
    assert moved.is_relative_to(fake_claude_home / "session_manager")
    assert all(x["old_skill"] is None for x in r["skills"])


def test_old_skill_stays_if_plugin_install_fails(claude, fake_claude_home):
    w = _old(fake_claude_home, "clewpath-workers")
    claude["fail_on"] = "install"
    with pytest.raises(pi.PluginError):
        pi.install()
    assert (w / "SKILL.md").is_file(), "새 스킬이 없는데 옛것을 치우면 스킬이 사라진다"


def test_failed_claude_command_reports_detail(claude):
    claude.update(rc=1, err="marketplace 오류")
    with pytest.raises(pi.PluginError) as e:
        pi.install()
    assert e.value.code == "claude_failed" and "marketplace 오류" in e.value.detail


def test_status_survives_claude_failure(claude):
    claude.update(rc=1, err="망가짐")
    st = pi.status()
    assert st["unknown"] is True and st["installed"] is False and st["remove_commands"] and len(st["tools"]) == 9


def test_sheet_contents_come_from_skill_files_and_tool_definitions(claude):
    """설치 화면 설명은 스킬 파일 description 과 도구 메타에서 — 손으로 맞추는 목록이 없다."""
    c = pi.contents()
    assert [x["name"] for x in c["skills"]] == list(pi.SKILLS)
    for x in c["skills"]:
        assert x["command"] == f"/clewpath:{x['name']}" and x["desc"] and x["desc"] in pi.bundled_skill(x["name"]).read_text(encoding="utf-8")
    assert {t["name"] for t in c["tools"]} == set(_tools())
    for t in c["tools"]:
        assert t["summary"] and t["guard"], f"{t['name']} 에 화면 설명·안전장치가 없다"
    g = {t["name"]: t["guard"] for t in c["tools"]}
    for n in ("sessions_move_request", "sessions_delete_request", "session_create"):
        assert "승인" in g[n]
    assert {t["name"] for t in c["tools"] if t["asks"]} == {"session_rename"}


def test_no_startup_or_background_path_installs():
    """[N1 가드] 기동·업데이트·정기 작업은 플러그인을 건드리지 않는다. 설치는 라우트 하나로만."""
    callers = []
    for p in (ROOT / "session_manager").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if p.name != "plugininstall.py" and re.search(r"plugininstall\.(install|build)\(", src):
            callers.append(p.name)
    assert callers == ["server.py"]
    src = (ROOT / "session_manager" / "server.py").read_text(encoding="utf-8")
    assert src.count("plugininstall.install(") == 1 and "/api/owner/plugin/install" in outer_door.GUARDED


def test_route_installs_only_locally(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    from session_manager import server as S, team
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    seen = []
    monkeypatch.setattr(pi, "install", lambda: seen.append(1) or {"ok": 1})
    with TestClient(S.create_app(), base_url="http://127.0.0.1") as c:
        assert c.post("/api/owner/plugin/install", json={}).json() == {"ok": 1} and seen == [1]
        assert c.post("/api/owner/plugin/install", json={}, headers={"X-ClewPath-Via": "relay"}).status_code == 403
        assert seen == [1], "폰에서는 안 된다"


def test_uninstall_sh_only_explains():
    src = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "plugin_notice" in src and "claude plugin uninstall clewpath@clewpath" in src
    assert not re.search(r"^\s*claude plugin", src, re.M), "지우지 않고 안내만"


# ---------------------------------------------------------------- MCP
def _tools():
    import asyncio
    return {t.name: t for t in asyncio.run(m.mcp.list_tools())}


def test_tool_list_is_exact():
    assert set(_tools()) == {"sessions_find", "session_rename", "session_create", "sessions_move_request",
                             "sessions_delete_request", "request_finish", "session_restore",
                             "agent_history_search", "agent_history_read"}


def test_only_rename_asks_claude_for_confirmation():
    meta = {n: (t.meta or {}) for n, t in _tools().items()}
    asks = {n for n, v in meta.items() if v.get("anthropic/requiresUserInteraction")}
    assert asks == {"session_rename"} and meta["session_rename"]["anthropic/requiresUserInteraction"] is True
    assert all(set(v) <= {"anthropic/requiresUserInteraction", m.SUMMARY, m.GUARD} for v in meta.values()), "모르는 메타 키를 늘리지 않는다"


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


def test_history_tools_call_read_only_routes(monkeypatch):
    """지난 대화 찾기·읽기는 GET 만 부른다(바깥문 대상 아님, Host 가 부른 세션으로 범위를 정한다)."""
    seen = []

    def fake(method, path, *, params=None, body=None):
        seen.append((method, path, params))
        return 200, ({"api": session_ops.API_VERSION} if path.endswith("capabilities") else {"items": []})
    monkeypatch.setattr(m, "_http", fake)
    monkeypatch.setattr(m, "_checked", False)
    monkeypatch.setenv("CLEWPATH_PLUGIN_API", str(session_ops.API_VERSION))
    m.agent_history_search("배포 규칙", limit=5)
    m.agent_history_read("s1:3", at=120)
    m.agent_history_read("s1:4")
    got = [x for x in seen if not x[1].endswith("capabilities")]
    assert got == [("GET", "/api/v1/agent-history/search", {"q": "배포 규칙", "session_id": "", "limit": 5}),
                   ("GET", "/api/v1/agent-history/read", {"ref": "s1:3", "before": 2, "after": 4, "at": 120}),
                   ("GET", "/api/v1/agent-history/read", {"ref": "s1:4", "before": 2, "after": 4})]
