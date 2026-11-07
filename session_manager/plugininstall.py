"""세션 제어 플러그인 설치 — 로컬 마켓 폴더를 만들고 `claude plugin` 명령으로 설치·갱신한다.

불가침 원칙 예외 N1(2026-11-07 사장님 승인, docs/designs/session-control.md 5·7 절): Claude 설정(`settings.json` 의 enabledPlugins·
extraKnownMarketplaces, `plugins/`)을 claude 명령이 쓴다. 반드시 사용자의 명시 동작(설정 화면 버튼 + 확인창) 뒤에만 호출된다.
어떤 기동·업데이트·백그라운드 경로에서도 부르지 않는다(가드: tests/test_plugininstall.py). 제거는 하지 않고 명령만 안내한다.

X2 실측(2026-11-07, claude 2.1.319): 비대화형으로 marketplace add·install·marketplace update·update 가 모두 된다.
로컬 마켓은 자동 갱신이 꺼져 있고 판 번호가 바뀌어야 update 가 적용된다 — 판 번호는 Host 판을 쓴다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from session_manager import config

MARKET = "clewpath"
PLUGIN = "clewpath"
PLUGIN_ID = f"{PLUGIN}@{MARKET}"
SKILL = "clewpath-sessions"
TIMEOUT_S = 90
REMOVE_COMMANDS = [f"claude plugin uninstall {PLUGIN_ID}", f"claude plugin marketplace remove {MARKET}"]


class PluginError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def root() -> Path:
    return config.data_dir() / "claude-plugin"


def bundled_skill() -> Path:
    return Path(__file__).resolve().parent / "skills" / SKILL / "SKILL.md"


def _python() -> str:
    """MCP 는 표준 입출력을 쓴다 — 창 없는 pythonw 대신 같은 폴더의 python 을 쓴다."""
    p = Path(sys.executable)
    if p.name.lower().startswith("pythonw"):
        q = p.with_name(p.name.lower().replace("pythonw", "python"))
        if q.is_file():
            return str(q)
    return str(p)


def _version() -> str:
    from session_manager import updater
    return updater.current_version()


def build() -> Path:
    """로컬 마켓 폴더를 Host 판에 맞게 다시 만든다. ClewPath 데이터 폴더 안이라 claude 파일이 아니다."""
    from session_manager import session_ops
    src = bundled_skill()
    if not src.is_file():
        raise PluginError("bundled_missing")
    r = root()
    v = _version()
    if r.exists():
        shutil.rmtree(r)
    (r / ".claude-plugin").mkdir(parents=True)
    pl = r / PLUGIN
    (pl / ".claude-plugin").mkdir(parents=True)
    (pl / "skills" / "sessions").mkdir(parents=True)
    desc = "프롬프트로 세션 찾기·만들기·이름 변경·이동·삭제(ClewPath Host 승인)"
    (r / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": MARKET, "description": "ClewPath 로컬 플러그인", "owner": {"name": "ClewPath"},
        "plugins": [{"name": PLUGIN, "source": f"./{PLUGIN}", "description": desc, "version": v}]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (pl / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": PLUGIN, "version": v, "description": desc, "author": {"name": "ClewPath"}},
        ensure_ascii=False, indent=2), encoding="utf-8")
    # 파이썬 경로는 OS 마다 달라 설치 때 만든다. ${CLAUDE_PLUGIN_ROOT} 는 판마다 바뀌어 쓰지 않는다.
    (pl / ".mcp.json").write_text(json.dumps({"mcpServers": {"sessions": {
        "command": _python(), "args": ["-m", "session_manager.session_control_mcp"],
        "env": {"CLEWPATH_PLUGIN_API": str(session_ops.API_VERSION)}}}}, ensure_ascii=False, indent=2), encoding="utf-8")
    (pl / "skills" / "sessions" / "SKILL.md").write_bytes(src.read_bytes())
    return r


def _claude(*args: str) -> str:
    try:
        p = subprocess.run([config.claude_exe(), "plugin", *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PluginError("claude_failed", type(e).__name__) from e
    if p.returncode != 0:
        raise PluginError("claude_failed", (p.stderr or p.stdout or "").strip()[-300:])
    return p.stdout


def _listed(args: tuple[str, ...]) -> list[dict]:
    out = _claude(*args)
    try:
        d = json.loads(out[out.index("["):])
    except ValueError:
        return []
    return d if isinstance(d, list) else []


def status() -> dict:
    """설치 여부·판(읽기만). 설정 화면 표시용."""
    want = _version()
    try:
        cur = next((p for p in _listed(("list", "--json")) if p.get("id") == PLUGIN_ID), None)
    except PluginError as e:
        return {"installed": False, "unknown": True, "want": want, "error": e.detail or e.code, "remove_commands": REMOVE_COMMANDS}
    return {"installed": bool(cur), "enabled": bool(cur and cur.get("enabled")), "version": (cur or {}).get("version"), "want": want,
            "up_to_date": bool(cur) and cur.get("version") == want, "market": str(root()), "remove_commands": REMOVE_COMMANDS}


def install() -> dict:
    """사용자 확인 뒤에만 호출. 설치돼 있으면 갱신. 떠 있는 세션은 다음 세션 또는 /reload-plugins 부터 적용."""
    r = build()
    markets = {m.get("name") for m in _listed(("marketplace", "list", "--json"))}
    if MARKET in markets:
        _claude("marketplace", "update", MARKET)
    else:
        _claude("marketplace", "add", str(r))
    cur = next((p for p in _listed(("list", "--json")) if p.get("id") == PLUGIN_ID), None)
    if cur:
        _claude("update", PLUGIN_ID)
    else:
        _claude("install", PLUGIN_ID, "--scope", "user")
    return status()
