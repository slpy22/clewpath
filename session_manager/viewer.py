"""세션 뷰어 — jsonl을 읽기 좋은 메시지 목록으로 변환한다.

방어적 원칙: 모르는 구조는 raw로 보존하여 표시한다. 원본은 수정하지 않는다.
"""
from __future__ import annotations

import json

from session_manager.scanner import scan_one


def _render_content(content) -> str:
    """message.content를 텍스트로 변환. 문자열/블록배열/기타 모두 방어적으로 처리."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "thinking":
                    parts.append("[thinking] " + block.get("thinking", ""))
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    parts.append(f"[tool_use: {name}] " + json.dumps(inp, ensure_ascii=False)[:2000])
                elif btype == "tool_result":
                    c = block.get("content", "")
                    parts.append("[tool_result] " + (_render_content(c) if not isinstance(c, str) else c)[:2000])
                else:
                    parts.append(json.dumps(block, ensure_ascii=False)[:1000])
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)[:2000]


def _text_blocks(content) -> str:
    """content 에서 text 블록만 추출(thinking/tool_use/tool_result 제외)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _tool_names(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [b.get("name", "?") for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def read_conversation(session_id: str, limit: int = 200) -> dict | None:
    """대화만 깔끔하게 추출(Claude 표시용). user/assistant 의 text + 사용도구만.

    thinking·tool_use 인자·tool_result·메타 레코드는 전부 제외. 최근 limit 개 반환.
    """
    meta = scan_one(session_id)
    if meta is None:
        return None

    conv: list[dict] = []
    with open(meta.jsonl_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            text = _text_blocks(content).strip()
            tools = _tool_names(content)
            if not text and not tools:
                continue  # 순수 tool_result 등은 제외
            conv.append({"role": role, "text": text, "tools": tools,
                         "timestamp": obj.get("timestamp")})

    total = len(conv)
    if limit and limit > 0:
        conv = conv[-limit:]
    return {"session_id": session_id, "total": total,
            "returned": len(conv), "messages": conv}


def read_messages(session_id: str, offset: int = 0, limit: int = 200) -> dict | None:
    """세션의 메시지를 페이지네이션하여 반환한다."""
    meta = scan_one(session_id)
    if meta is None:
        return None

    messages: list[dict] = []
    total = 0
    idx = 0
    with open(meta.jsonl_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            total += 1
            if total <= offset or len(messages) >= limit:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                messages.append({
                    "index": total - 1, "type": "unparsed",
                    "role": None, "text": line[:500], "raw": True,
                })
                continue
            msg = obj.get("message") or {}
            role = msg.get("role") if isinstance(msg, dict) else None
            text = _render_content(msg.get("content") if isinstance(msg, dict) else None)
            # tool 결과가 별도 필드에 있는 경우도 표시
            if not text and obj.get("toolUseResult") is not None:
                text = "[toolUseResult] " + json.dumps(obj["toolUseResult"], ensure_ascii=False)[:2000]
            messages.append({
                "index": total - 1,
                "type": obj.get("type", "?"),
                "role": role,
                "text": text,
                "timestamp": obj.get("timestamp"),
                "is_sidechain": bool(obj.get("isSidechain")),
                "raw": False,
            })

    return {
        "session_id": session_id,
        "meta": meta.to_dict(),
        "total_messages": total,
        "offset": offset,
        "limit": limit,
        "messages": messages,
    }
