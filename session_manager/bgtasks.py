"""세션의 백그라운드 쉘·모니터·서브에이전트 수(2026-10-12 사장님: 목록에 몇 개인지, 남아 있으면 '완료' 가 아니다.
2026-10-28: 서브에이전트(Agent·Workflow 백그라운드)도 센다).

근거는 claude 가 jsonl 에 남기는 시작/끝 표식(scanner._Fold._bg_feed, 읽기만)과 피어 레지스트리의
프로세스 시작 시각 — claude 가 다시 뜨면 이전 작업은 함께 죽으므로 그 이후 것만 센다.
"""
from __future__ import annotations

from session_manager import scanner


def counts(meta, peer: dict | None) -> dict | None:
    """살아 있는 세션이면 {"shells": n, "monitors": m, "agents": k}, 아니면 None."""
    if not meta or not peer:
        return None
    st = peer.get("started_at")
    try:
        since = float(st) / 1000.0 if st else None
    except (TypeError, ValueError):
        since = None
    return scanner.bg_counts(meta, since)


def counts_by_id(session_id: str) -> dict | None:
    from session_manager import peers
    peer = peers.peer_map().get(session_id)
    if not peer:
        return None
    return counts(scanner.scan_one(session_id), peer)


def busy(c: dict | None) -> bool:
    return bool(c and (c.get("shells") or c.get("monitors") or c.get("agents")))


def hold_ready(rt: dict | None, c: dict | None) -> dict | None:
    """턴은 끝났어도(ready) 쉘·모니터·서브에이전트가 남아 있으면 '완료' 가 아니라 'background' 로 내보낸다(사본)."""
    if not rt or rt.get("phase") != "ready" or not busy(c):
        return rt
    out = dict(rt)
    out["phase"] = "background"
    out["background_at"] = rt.get("ready_at")
    return out
