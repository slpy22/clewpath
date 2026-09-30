"""claude 피어 세션 레지스트리 — `~/.claude/sessions/<pid>.json` 읽기 전용 조회.

Claude Code 는 살아 있는 대화형 세션마다 이 파일을 두고(프로세스가 끝나면 지운다),
`ListAgents`/`SendMessage` 가 이걸로 서로를 찾는다(실측 2026-09-23, docs/2026-09-23-worker-dispatch-spike.md).
필드: pid, sessionId, name(제목 또는 cwd 파생), status(idle|busy), kind, messagingSocketPath(명명 파이프), cwd.

ClewPath 는 이걸로 ① 우리 PTY 가 아닌 세션도 "떠 있음"으로 판정하고 ② 관제 호출선의
`SendMessage(to=이름|uds:파이프)` 를 세션 UUID 로 해석한다. **읽기만 한다** — 불가침 원칙 무관.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from session_manager import config

_CACHE_TTL = 2.0
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "list": []}
# 한 번이라도 본 이름/파이프 → 세션(Host 수명 동안). 워커가 죽으면 레지스트리 항목이 사라져
# "No agent named … is reachable" 실패를 세션으로 못 잇는다(e2e 실측) → 마지막으로 본 매핑으로 해석.
_seen_name: dict[str, str] = {}
_seen_sock: dict[str, str] = {}
_MAX_SEEN = 500
_REF_RE = re.compile(r"\s*\[[0-9a-f]{4,}\]\s*$")   # "이름 [f0c2da]" 의 꼬리표


def sessions_dir() -> Path:
    return config.claude_home() / "sessions"


def _read_all() -> list[dict]:
    out: list[dict] = []
    base = sessions_dir()
    try:
        if not base.is_dir():
            return out
        for p in base.glob("*.json"):
            try:
                with p.open(encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:  # noqa: BLE001  쓰는 중이거나 손상 — 건너뜀
                continue
            sid = d.get("sessionId")
            if not isinstance(d, dict) or not sid:
                continue
            out.append({
                "session_id": str(sid), "pid": d.get("pid"),
                "name": str(d.get("name") or ""), "status": str(d.get("status") or ""),
                "kind": str(d.get("kind") or ""), "socket": str(d.get("messagingSocketPath") or ""),
                "cwd": str(d.get("cwd") or ""), "started_at": d.get("startedAt"),
                "entrypoint": str(d.get("entrypoint") or ""),
                # 어디서 띄운 프로세스인가(2026-09-30 사장님): clewpath|terminal|child|headless|sdk|script|other|unknown
                "origin": origin_of(d.get("pid"), str(d.get("entrypoint") or "")),
            })
    except OSError:
        return out
    return out


def _remember(peers: list[dict]) -> None:
    for p in peers:
        if p["name"]:
            _seen_name[p["name"]] = p["session_id"]
        if p["socket"]:
            _seen_sock[_norm_pipe(p["socket"])] = p["session_id"]
    for d in (_seen_name, _seen_sock):
        if len(d) > _MAX_SEEN:
            for k in list(d)[: len(d) - _MAX_SEEN]:
                d.pop(k, None)


def snapshot(max_age: float = _CACHE_TTL) -> list[dict]:
    with _lock:
        now = time.time()
        if now - _cache["at"] > max_age:
            _cache["list"] = _read_all()
            _cache["at"] = now
            _remember(_cache["list"])
        return list(_cache["list"])


# ---- 프로세스 출처(origin): 레지스트리의 pid 를 psutil 로 들여다본다(읽기만). pid 별 캐시 ----
_SHELLS = {"pwsh.exe", "pwsh", "powershell.exe", "powershell", "cmd.exe", "cmd", "bash.exe", "bash", "zsh", "sh",
           "windowsterminal.exe", "conhost.exe", "wt.exe", "mintty.exe", "alacritty.exe", "wezterm-gui.exe", "code.exe", "cursor.exe"}
_CLAUDE_NAMES = {"claude.exe", "claude", "node.exe", "node", "bun.exe", "bun"}
_ORIGIN_CACHE: dict[int, dict] = {}


def _proc_info(pid: int) -> dict | None:
    """pid → {name, cmdline, parent_name, parent_pid, create_time} 또는 None(없음/권한 없음/psutil 없음)."""
    try:
        import psutil
        p = psutil.Process(int(pid))
        with p.oneshot():
            par = p.parent()
            return {"name": p.name() or "", "cmdline": list(p.cmdline() or []),
                    "parent_name": (par.name() if par else "") or "", "parent_pid": (par.pid if par else None),
                    "create_time": p.create_time()}
    except Exception:  # noqa: BLE001  NoSuchProcess/AccessDenied/ImportError
        return None


def classify_origin(info: dict | None, entrypoint: str = "", host_pid: int | None = None) -> dict:
    """프로세스 정보 → 출처. 순수 함수(테스트용).

    clewpath  : ClewPath Host 가 띄운 PTY(부모 pid = Host)
    headless  : `claude -p/--print`(스크립트·SendMessage 의뢰 등 비대화형)
    sdk       : entrypoint 가 cli 가 아닌 것(Agent SDK 등)
    child     : 다른 claude/node 프로세스가 띄움(클로드가 의뢰해 띄운 세션)
    terminal  : 사용자가 셸/터미널/에디터에서 직접 띄움
    script    : 파이썬 등 다른 프로그램이 띄움
    other/unknown: 그 외 / 프로세스 정보 없음(권한·종료)
    """
    if not info:
        return {"origin": "unknown", "parent": None}
    argv = [str(a).lower() for a in info.get("cmdline") or []]
    parent = (info.get("parent_name") or "").lower()
    ppid = info.get("parent_pid")
    if host_pid is not None and ppid == host_pid:
        origin = "clewpath"
    elif "-p" in argv or "--print" in argv:
        origin = "headless"
    elif entrypoint and entrypoint != "cli":
        origin = "sdk"
    elif parent in _CLAUDE_NAMES:
        origin = "child"
    elif parent in _SHELLS:
        origin = "terminal"
    elif parent.startswith("python"):
        origin = "script"
    else:
        origin = "other"
    return {"origin": origin, "parent": info.get("parent_name") or None}


def origin_of(pid, entrypoint: str = "") -> dict:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"origin": "unknown", "parent": None}
    info = _proc_info(pid)
    key = pid
    ct = (info or {}).get("create_time")
    cached = _ORIGIN_CACHE.get(key)
    if cached and cached.get("_ct") == ct and info is not None:
        return {k: v for k, v in cached.items() if not k.startswith("_")}
    import os
    r = classify_origin(info, entrypoint, host_pid=os.getpid())
    if info is not None:
        _ORIGIN_CACHE[key] = {**r, "_ct": ct}
        if len(_ORIGIN_CACHE) > 500:
            for k in list(_ORIGIN_CACHE)[:200]:
                _ORIGIN_CACHE.pop(k, None)
    return r


def peer_map() -> dict[str, dict]:
    """session_id → 피어 정보(세션 목록 API 가 조인)."""
    return {p["session_id"]: p for p in snapshot()}


def _norm_pipe(s: str) -> str:
    return s.replace("\\\\", "\\").replace("/", "\\").lower().strip()


def resolve(ref: str | None) -> str | None:
    """`SendMessage` 의 `to`(이름, "이름 [ref]", "uds:<파이프>")를 세션 UUID 로. 못 찾으면 None."""
    if not ref:
        return None
    ref = str(ref).strip()
    peers = snapshot()
    if ref.lower().startswith("uds:"):
        want = _norm_pipe(ref[4:])
        for p in peers:
            if p["socket"] and _norm_pipe(p["socket"]) == want:
                return p["session_id"]
        return _seen_sock.get(want)          # 지금은 죽었지만 전에 본 파이프
    name = _REF_RE.sub("", ref)
    hits = [p for p in peers if p["name"] == name]
    if len(hits) == 1:
        return hits[0]["session_id"]
    if not hits:
        return _seen_name.get(name)          # 죽은 워커의 이름(실패 감지용)
    return None                              # 동명 2개 이상은 모호 — 해석 안 함
