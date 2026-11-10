"""ClewPath 플러그인 설치 — 플러그인 하나(`clewpath`)에 스킬 3개(세션 제어·워커·비서)와 세션 제어 MCP 도구 9개(2026-11-10 지난 대화 찾기·읽기 2개 추가)를 한 번에 설치한다.

불가침 원칙 예외 N1(2026-11-07 사장님 승인, 범위 확대 포함, docs/designs/session-control.md 5·7 절): Claude 설정(`settings.json` 의
enabledPlugins·extraKnownMarketplaces, `plugins/`)을 claude 명령이 쓴다. 반드시 사용자의 명시 동작(설정 화면에서 들어 있는 스킬·도구를
보고 설치 버튼) 뒤에만 호출된다. 어떤 기동·업데이트·백그라운드 경로에서도 부르지 않는다(가드: tests/test_plugininstall.py).

2026-11-08 사장님: 나누지 말고 한 번에 다 설치, 대신 설치 화면에 플러그인 안의 모든 스킬·도구 설명을 보여 준다. 화면 설명은 손으로 적은
목록이 아니라 스킬 파일의 description 과 MCP 도구 정의의 메타(`com.clewpath/summary`·`guard`)에서 뽑는다.

- 스킬 호출 이름은 `플러그인:스킬` 이다(실측) — `/clewpath:sessions`·`/clewpath:workers`·`/clewpath:assistant`.
- 0.36.1 에서 따로 설치한 `clewpath-workers@clewpath`·`clewpath-assistant@clewpath` 가 있으면 설치 때 제거해 하나로 합친다.
- 옛 `~/.claude/skills` 스킬이 남으면 말로 부를 때 옛것이 골린다(실측) → 플러그인 설치가 끝난 **뒤에** ClewPath 데이터 폴더로 옮긴다(지우지 않음).

X2 실측(2026-11-07, claude 2.1.319): 비대화형으로 marketplace add·install·marketplace update·update·uninstall 이 된다.
로컬 마켓은 자동 갱신이 꺼져 있고 판 번호가 바뀌어야 update 가 적용된다 — 판 번호는 Host 판을 쓴다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from session_manager import config

MARKET = "clewpath"
PLUGIN = "clewpath"
PLUGIN_ID = f"{PLUGIN}@{MARKET}"
LEGACY_IDS = ("clewpath-workers@clewpath", "clewpath-assistant@clewpath")      # 0.36.1 의 따로 설치본
TIMEOUT_S = 90
DESC = "ClewPath: 프롬프트로 세션 다루기(찾기·만들기·이름·옮기기·지우기, 옮기기·지우기는 승인) + 에이전트 지난 대화 찾기 + 워커 팀 규칙 + 비서 규칙"

# 스킬 이름 → (동봉 폴더, 옛 방식 스킬 폴더 이름)
SKILLS = {"sessions": ("clewpath-sessions", None), "workers": ("clewpath-workers", "clewpath-workers"),
          "assistant": ("clewpath-assistant", "clewpath-assistant")}
REMOVE_COMMANDS = [f"claude plugin uninstall {PLUGIN_ID}", f"claude plugin marketplace remove {MARKET}"]


class PluginError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def root() -> Path:
    return config.data_dir() / "claude-plugin"


def bundled_skill(name: str) -> Path:
    return Path(__file__).resolve().parent / "skills" / SKILLS[name][0] / "SKILL.md"


def old_skill_dir(name: str) -> Path | None:
    old = SKILLS[name][1]
    return config.claude_home() / "skills" / old if old else None


def _frontmatter(p: Path) -> dict:
    t = p.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n", t, re.S)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.partition(":")
        if v:
            out[k.strip()] = v.strip()
    return out


def contents() -> dict:
    """플러그인 안의 스킬·도구(설치 화면용). 스킬 파일과 MCP 도구 정의에서 뽑는다."""
    import asyncio
    from session_manager import session_control_mcp as m
    skills = []
    for name in SKILLS:
        fm = _frontmatter(bundled_skill(name))
        old = old_skill_dir(name)
        skills.append({"name": name, "command": f"/{PLUGIN}:{name}", "desc": fm.get("description", ""),
                       "old_skill": str(old) if old and (old / "SKILL.md").is_file() else None})
    tools = []
    for t in asyncio.run(m.mcp.list_tools()):
        meta = t.meta or {}
        tools.append({"name": t.name, "summary": meta.get(m.SUMMARY, ""), "guard": meta.get(m.GUARD, ""),
                      "asks": bool(meta.get("anthropic/requiresUserInteraction"))})
    return {"skills": skills, "tools": tools}


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


def _write(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def build() -> Path:
    """로컬 마켓 폴더를 Host 판에 맞게 다시 만든다. ClewPath 데이터 폴더 안이라 claude 파일이 아니다."""
    from session_manager import session_ops
    for name in SKILLS:
        if not bundled_skill(name).is_file():
            raise PluginError("bundled_missing", name)
    r = root()
    v = _version()
    if r.exists():
        shutil.rmtree(r)
    (r / ".claude-plugin").mkdir(parents=True)
    pl = r / PLUGIN
    (pl / ".claude-plugin").mkdir(parents=True)
    _write(r / ".claude-plugin" / "marketplace.json", {"name": MARKET, "description": "ClewPath 로컬 플러그인", "owner": {"name": "ClewPath"},
                                                       "plugins": [{"name": PLUGIN, "source": f"./{PLUGIN}", "description": DESC, "version": v}]})
    _write(pl / ".claude-plugin" / "plugin.json", {"name": PLUGIN, "version": v, "description": DESC, "author": {"name": "ClewPath"}})
    # 파이썬 경로는 OS 마다 달라 설치 때 만든다. ${CLAUDE_PLUGIN_ROOT} 는 판마다 바뀌어 쓰지 않는다.
    _write(pl / ".mcp.json", {"mcpServers": {"sessions": {
        "command": _python(), "args": ["-m", "session_manager.session_control_mcp"],
        "env": {"CLEWPATH_PLUGIN_API": str(session_ops.API_VERSION)}}}})
    for name in SKILLS:
        (pl / "skills" / name).mkdir(parents=True)
        (pl / "skills" / name / "SKILL.md").write_bytes(bundled_skill(name).read_bytes())
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
    """설치 여부·판·들어 있는 스킬과 도구(읽기만). 설정 화면 표시용."""
    want = _version()
    unknown, err, cur = False, None, {}
    try:
        cur = {p.get("id"): p for p in _listed(("list", "--json"))}
    except PluginError as e:
        unknown, err = True, e.detail or e.code
    me = cur.get(PLUGIN_ID)
    return {"installed": bool(me), "version": (me or {}).get("version"), "want": want,
            "up_to_date": bool(me) and me.get("version") == want, "legacy": [i for i in LEGACY_IDS if i in cur],
            "unknown": unknown, "error": err, "market": str(root()), "remove_commands": REMOVE_COMMANDS, **contents()}


def _move_old(name: str) -> str | None:
    old = old_skill_dir(name)
    if not old or not (old / "SKILL.md").is_file():
        return None
    dst = config.data_dir() / "legacy-skills" / f"{old.name}-{int(time.time())}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(dst))
    return str(dst)


def install() -> dict:
    """사용자 확인 뒤에만 호출. 한 번에 설치(설치돼 있으면 갱신), 0.36.1 따로 설치본 제거, 옛 스킬 옮기기.
    떠 있는 세션은 다음 세션 또는 /reload-plugins 부터."""
    r = build()
    markets = {m.get("name") for m in _listed(("marketplace", "list", "--json"))}
    if MARKET in markets:
        _claude("marketplace", "update", MARKET)
    else:
        _claude("marketplace", "add", str(r))
    cur = {p.get("id") for p in _listed(("list", "--json"))}
    _claude(*(("update", PLUGIN_ID) if PLUGIN_ID in cur else ("install", PLUGIN_ID, "--scope", "user")))
    removed = [i for i in LEGACY_IDS if i in cur]
    for i in removed:
        _claude("uninstall", i)
    moved = {}
    for name in SKILLS:                         # 새 스킬이 자리 잡은 뒤에 — 앞에서 실패하면 옛것이 그대로 남는다
        to = _move_old(name)
        if to:
            moved[name] = to
    return {**status(), "moved": moved, "removed": removed}
