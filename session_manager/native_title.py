"""네이티브 세션 이름(Ctrl+R = custom-title) 쓰기.

Claude Code 는 rename 시 세션 JSONL 에 다음 레코드를 append 한다(마지막 값이 유효):
    {"type":"custom-title","customTitle":"이름","sessionId":"<uuid>"}
이 모듈은 그와 '동일한 append' 로 네이티브 이름을 설정한다 → --resume 피커/claude.ai 에도 반영.

에이전트 이름(2026-10-15 사장님 승인): claude 의 /rename 은 custom-title 과 함께
    {"type":"agent-name","agentName":"이름","sessionId":"<uuid>"}
를 남기고, 이 값이 피어 레지스트리의 name(= 다른 세션이 SendMessage·ListAgents 로 부르는 이름)이 된다.
제목만 바꾸면 목록 이름과 다른 세션이 부르는 이름이 갈린다(북한백과PM ↔ 개발관리에이전트 사례).
그래서 이름을 바꿀 때 **claude 의 /rename 과 똑같이** custom-title + agent-name 두 줄을 함께 남긴다.
처음엔 agent-name 이 이미 있는 세션만 했으나, 이름 없는 세션에 이름 붙이기가 이 기능의 목적이라 같은 날 모든 세션으로 넓혔다
(사장님 결정). 결과는 claude 안에서 /rename 한 것과 같다 — 헤드리스(-p) 세션에 이름을 붙이면 claude 재개 피커에도 나타난다.
살아 있는 프로세스의 피어 이름은 claude 를 다시 띄울 때 바뀐다(claude 는 재개할 때 마지막 기록을 읽어 다시 적는다).

안전 원칙:
- append 전용. 기존 줄은 절대 수정하지 않는다(원본 손상 위험 최소화).
- 실행 중(busy) 세션에는 기본 차단(동시 쓰기로 인한 torn write 방지). force 로만 강제.
"""
from __future__ import annotations

import json

from session_manager import config, scanner


def session_status(session_id: str) -> str | None:
    """~/.claude/sessions/<pid>.json 에서 이 세션의 상태를 찾는다(최신 updatedAt 우선).

    반환: 'busy'(생성 중) | 'idle'(열려있으나 대기) | None(정보 없음 = 중지 추정).
    """
    d = config.claude_home() / "sessions"
    if not d.is_dir():
        return None
    best_status: str | None = None
    best_t = -1.0
    for f in d.glob("*.json"):
        try:
            o = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if o.get("sessionId") == session_id:
            t = float(o.get("updatedAt") or 0)
            if t >= best_t:
                best_t = t
                best_status = o.get("status")
    return best_status


def set_native_title(session_id: str, title: str,
                     allow_live: bool = False) -> dict:
    """세션 JSONL 에 custom-title 을 append 해 네이티브 이름을 설정한다."""
    title = (title or "").strip()
    if not title:
        return {"error": "empty_title"}
    if len(title) > 200:
        title = title[:200]
    meta = scanner.scan_one(session_id)
    if meta is None:
        return {"error": "not_found"}

    status = session_status(session_id)
    if status == "busy" and not allow_live:
        return {"error": "session_live", "status": status,
                "hint": "실행 중(busy) 세션입니다. force=true 로만 강제 가능."}

    recs = [{"type": "custom-title", "customTitle": title, "sessionId": session_id}]
    agent = meta.agent_name != title
    if agent:
        recs.append({"type": "agent-name", "agentName": title, "sessionId": session_id})
    try:
        with open(meta.jsonl_path, "a", encoding="utf-8") as f:   # 한 번에(두 줄 사이에 다른 쓰기가 끼지 않게)
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    except Exception as e:  # noqa: BLE001
        return {"error": f"write_failed: {e}"}
    scanner.invalidate(str(meta.jsonl_path))

    out = {"ok": True, "session_id": session_id, "title": title,
           "status": status, "native": True, "agent_name": title if agent else meta.agent_name}
    if agent and status:
        out["peer_name_after_restart"] = True   # 지금 돌고 있는 프로세스는 옛 이름으로 불린다 — 다시 띄우면 새 이름
    # 다른 세션이 같은 이름으로 불리고 있으면 SendMessage 이름 호출이 모호해진다(주소 uds: 호출은 무관)
    if agent:
        try:
            from session_manager import peers
            if any(p["name"] == title and p["session_id"] != session_id for p in peers.snapshot()):
                out["peer_name_conflict"] = True
        except Exception:  # noqa: BLE001
            pass
    return out
