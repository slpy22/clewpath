"""ClewPath 플러그인 설치 — 로컬 마켓 하나에 플러그인 3개(세션 제어·워커·비서), 사용자가 골라서 설치한다.

불가침 원칙 예외 N1(2026-11-07 사장님 승인, 범위 확대 포함, docs/designs/session-control.md 5·7 절): Claude 설정(`settings.json` 의
enabledPlugins·extraKnownMarketplaces, `plugins/`)을 claude 명령이 쓴다. 반드시 사용자의 명시 동작(설정 화면 체크박스 + 적용 버튼)
뒤에만 호출된다. 어떤 기동·업데이트·백그라운드 경로에서도 부르지 않는다(가드: tests/test_plugininstall.py).

선택 설치가 플러그인 단위인 이유: claude 는 플러그인 일부만 설치하는 기능이 없다. 그래서 항목 하나 = 플러그인 하나다.
스킬 호출 이름은 `플러그인:스킬` 이다(실측: /clewpath-workers:workers).

옛 스킬(`~/.claude/skills/clewpath-workers` 등)이 남아 있으면 같은 스킬이 둘이 되고 말로 부를 때 옛것이 골린다(실측). 사용자가 고르면
플러그인을 설치한 **뒤에** ClewPath 데이터 폴더로 옮긴다(지우지 않는다, 되돌릴 수 있다).

X2 실측(2026-11-07, claude 2.1.319): 비대화형으로 marketplace add·install·marketplace update·update 가 된다.
로컬 마켓은 자동 갱신이 꺼져 있고 판 번호가 바뀌어야 update 가 적용된다 — 판 번호는 Host 판을 쓴다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from session_manager import config

MARKET = "clewpath"
TIMEOUT_S = 90

# 항목 = 플러그인. old = 예전 설치 방식의 스킬 폴더 이름(~/.claude/skills 아래).
ITEMS = {
    "sessions": {"plugin": "clewpath", "skill": "sessions", "source": "clewpath-sessions", "mcp": True, "old": None,
                 "title": "세션 제어", "command": "/clewpath:sessions",
                 "desc": "프롬프트로 세션 찾기·만들기·이름 변경·옮기기·지우기. 옮기기·지우기는 폰/2차 인증 승인을 거칩니다. 도구 7개와 스킬 1개."},
    "workers": {"plugin": "clewpath-workers", "skill": "workers", "source": "clewpath-workers", "mcp": False, "old": "clewpath-workers",
                "title": "워커 규칙", "command": "/clewpath-workers:workers",
                "desc": "여러 세션이 한 팀으로 일하는 운영 규칙. 관리 세션이 일감을 나누고 워커가 서로 통신합니다. 스킬 1개."},
    "assistant": {"plugin": "clewpath-assistant", "skill": "assistant", "source": "clewpath-assistant", "mcp": False, "old": "clewpath-assistant",
                  "title": "비서 규칙", "command": "/clewpath-assistant:assistant",
                  "desc": "개인 비서 세션의 규칙. 모든 세션의 장부를 보고 브리핑·제안하며, 실행은 승인 요청으로만 합니다. 스킬 1개."},
}


class PluginError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _id(key: str) -> str:
    return f"{ITEMS[key]['plugin']}@{MARKET}"


def remove_commands() -> list[str]:
    return [f"claude plugin uninstall {_id(k)}" for k in ITEMS] + [f"claude plugin marketplace remove {MARKET}"]


def root() -> Path:
    return config.data_dir() / "claude-plugin"


def bundled_skill(key: str) -> Path:
    return Path(__file__).resolve().parent / "skills" / ITEMS[key]["source"] / "SKILL.md"


def old_skill_dir(key: str) -> Path | None:
    name = ITEMS[key]["old"]
    return config.claude_home() / "skills" / name if name else None


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
    for k in ITEMS:
        if not bundled_skill(k).is_file():
            raise PluginError("bundled_missing", k)
    r = root()
    v = _version()
    if r.exists():
        shutil.rmtree(r)
    (r / ".claude-plugin").mkdir(parents=True)
    plugins = []
    for k, it in ITEMS.items():
        pl = r / it["plugin"]
        (pl / ".claude-plugin").mkdir(parents=True)
        (pl / "skills" / it["skill"]).mkdir(parents=True)
        _write(pl / ".claude-plugin" / "plugin.json",
               {"name": it["plugin"], "version": v, "description": it["desc"], "author": {"name": "ClewPath"}})
        (pl / "skills" / it["skill"] / "SKILL.md").write_bytes(bundled_skill(k).read_bytes())
        if it["mcp"]:
            # 파이썬 경로는 OS 마다 달라 설치 때 만든다. ${CLAUDE_PLUGIN_ROOT} 는 판마다 바뀌어 쓰지 않는다.
            _write(pl / ".mcp.json", {"mcpServers": {"sessions": {
                "command": _python(), "args": ["-m", "session_manager.session_control_mcp"],
                "env": {"CLEWPATH_PLUGIN_API": str(session_ops.API_VERSION)}}}})
        plugins.append({"name": it["plugin"], "source": f"./{it['plugin']}", "description": it["desc"], "version": v})
    _write(r / ".claude-plugin" / "marketplace.json",
           {"name": MARKET, "description": "ClewPath 로컬 플러그인", "owner": {"name": "ClewPath"}, "plugins": plugins})
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


def _installed() -> dict[str, dict]:
    return {p.get("id"): p for p in _listed(("list", "--json"))}


def status() -> dict:
    """항목별 설치 여부·판·옛 스킬(읽기만). 설정 화면 표시용."""
    want = _version()
    unknown, err, cur = False, None, {}
    try:
        cur = _installed()
    except PluginError as e:
        unknown, err = True, e.detail or e.code
    items = []
    for k, it in ITEMS.items():
        c = cur.get(_id(k))
        old = old_skill_dir(k)
        items.append({"key": k, "title": it["title"], "desc": it["desc"], "command": it["command"], "installed": bool(c),
                      "version": (c or {}).get("version"), "up_to_date": bool(c) and c.get("version") == want,
                      "old_skill": str(old) if old and (old / "SKILL.md").is_file() else None})
    return {"items": items, "want": want, "unknown": unknown, "error": err, "market": str(root()), "remove_commands": remove_commands()}


def _move_old(key: str) -> str | None:
    old = old_skill_dir(key)
    if not old or not (old / "SKILL.md").is_file():
        return None
    dst = config.data_dir() / "legacy-skills" / f"{old.name}-{int(time.time())}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(dst))
    return str(dst)


def apply(keys, move_old: bool = False) -> dict:
    """체크한 대로 맞춘다: 체크 = 설치·갱신, 체크 해제 = 제거. 사용자 확인 뒤에만 호출. 떠 있는 세션은 다음 세션 또는 /reload-plugins 부터."""
    want = list(dict.fromkeys(str(k) for k in (keys or [])))
    bad = [k for k in want if k not in ITEMS]
    if bad:
        raise PluginError("unknown_item", ",".join(bad))
    r = build()
    markets = {m.get("name") for m in _listed(("marketplace", "list", "--json"))}
    if MARKET in markets:
        _claude("marketplace", "update", MARKET)
    else:
        _claude("marketplace", "add", str(r))
    cur = _installed()
    moved = {}
    for k in ITEMS:
        pid = _id(k)
        if k in want:
            _claude(*(("update", pid) if pid in cur else ("install", pid, "--scope", "user")))
            if move_old:                       # 새 스킬이 자리 잡은 뒤에 — 실패하면 옛것이 그대로 남는다
                to = _move_old(k)
                if to:
                    moved[k] = to
        elif pid in cur:
            _claude("uninstall", pid)
    return {**status(), "moved": moved}
