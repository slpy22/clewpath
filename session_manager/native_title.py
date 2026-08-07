"""네이티브 세션 이름(Ctrl+R = custom-title) 쓰기.

Claude Code 는 rename 시 세션 JSONL 에 다음 레코드를 append 한다(마지막 값이 유효):
    {"type":"custom-title","customTitle":"이름","sessionId":"<uuid>"}
이 모듈은 그와 '동일한 append' 로 네이티브 이름을 설정한다 → --resume 피커/claude.ai 에도 반영.

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

    rec = {"type": "custom-title", "customTitle": title, "sessionId": session_id}
    try:
        with open(meta.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        return {"error": f"write_failed: {e}"}

    return {"ok": True, "session_id": session_id, "title": title,
            "status": status, "native": True}
