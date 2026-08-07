"""세션 통계 — 토큰 사용량(message.usage) 집계.

usage 필드가 없는 세션/메시지는 0으로 처리(방어적).
"""
from __future__ import annotations

import json
import os

from session_manager.scanner import scan_one, scan_all

_USAGE_KEYS = [
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
]

# 기본 요율 (USD / 1M tokens). 구독 계정은 실제 청구가 아니라 '환산 참고치'다.
# SM_PRICING_JSON 환경변수로 덮어쓸 수 있음(예: {"opus":{"in":15,...}}).
_DEFAULT_PRICES = {
    "opus":   {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"in": 3.0,  "out": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"in": 0.80, "out": 4.0,  "cache_write": 1.00,  "cache_read": 0.08},
    "fable":  {"in": 3.0,  "out": 15.0, "cache_write": 3.75,  "cache_read": 0.30},  # 미공개→sonnet급 가정
}


def _prices() -> dict:
    raw = os.environ.get("SM_PRICING_JSON")
    if raw:
        try:
            return {**_DEFAULT_PRICES, **json.loads(raw)}
        except Exception:  # noqa: BLE001
            pass
    return _DEFAULT_PRICES


def _family(model: str | None) -> str:
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in m:
            return fam
    return "sonnet"  # 알 수 없으면 sonnet급으로 환산


def estimate_cost(totals: dict, model: str | None) -> float:
    p = _prices()[_family(model)]
    return round(
        totals["input_tokens"] / 1e6 * p["in"]
        + totals["output_tokens"] / 1e6 * p["out"]
        + totals["cache_creation_input_tokens"] / 1e6 * p["cache_write"]
        + totals["cache_read_input_tokens"] / 1e6 * p["cache_read"], 4)


def session_stats(session_id: str) -> dict | None:
    meta = scan_one(session_id)
    if meta is None:
        return None

    totals = {k: 0 for k in _USAGE_KEYS}
    by_role = {"user": 0, "assistant": 0, "other": 0}
    assistant_turns = 0
    model_counts: dict[str, int] = {}

    with open(meta.jsonl_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t in by_role:
                by_role[t] += 1
            else:
                by_role["other"] += 1
            msg = obj.get("message")
            if isinstance(msg, dict):
                mdl = msg.get("model")
                if mdl:
                    model_counts[mdl] = model_counts.get(mdl, 0) + 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    assistant_turns += 1
                    for k in _USAGE_KEYS:
                        v = usage.get(k)
                        if isinstance(v, (int, float)):
                            totals[k] += int(v)

    total_tokens = totals["input_tokens"] + totals["output_tokens"]
    model = max(model_counts, key=model_counts.get) if model_counts else None
    return {
        "session_id": session_id,
        "size_bytes": meta.size_bytes,
        "message_count": meta.message_count,
        "by_role": by_role,
        "assistant_turns_with_usage": assistant_turns,
        "tokens": totals,
        "total_tokens": total_tokens,
        "model": model,
        "est_cost_usd": estimate_cost(totals, model),
    }


def global_stats() -> dict:
    """전체 세션의 요약 통계 (토큰은 세션별로 비싸므로 크기/개수 위주)."""
    sessions = scan_all()
    return {
        "total_sessions": len(sessions),
        "total_projects": len({s.project_folder for s in sessions}),
        "total_size_bytes": sum(s.size_bytes for s in sessions),
        "total_messages": sum(s.message_count for s in sessions),
        "largest": [
            {"session_id": s.session_id, "size_bytes": s.size_bytes,
             "cwd": s.cwd, "slug": s.slug}
            for s in sorted(sessions, key=lambda x: x.size_bytes, reverse=True)[:10]
        ],
    }
