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
            })
    except OSError:
        return out
    return out


def snapshot(max_age: float = _CACHE_TTL) -> list[dict]:
    with _lock:
        now = time.time()
        if now - _cache["at"] > max_age:
            _cache["list"] = _read_all()
            _cache["at"] = now
        return list(_cache["list"])


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
        return None
    name = _REF_RE.sub("", ref)
    hits = [p for p in peers if p["name"] == name]
    if len(hits) == 1:
        return hits[0]["session_id"]
    return None
