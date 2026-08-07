"""외부 API 가드레일 정책 — 역량 제한 / 토큰별 일일 한도 / 감사 로그.

핵심 원칙: 서버가 정책을 강제한다. 호출자는 역량을 올릴 수 없다.

환경변수:
    SM_API_DENIED_TOOLS   기본 "Bash Write Edit NotebookEdit KillShell"
                          (fork 세션에서 하드 차단할 도구 — 파일쓰기/명령실행 봉쇄)
    SM_API_DAILY_USD      토큰별 하루 비용 한도(달러). 기본 5
    SM_API_DAILY_CALLS    토큰별 하루 호출 수 한도. 기본 200
    SM_API_MAX_CALL_USD   ask 1회당 --max-budget-usd. 기본 1.0 (0 이면 미적용)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

from session_manager import config, profiles

_LOCK = threading.Lock()


def denied_tools() -> list[str]:
    raw = os.environ.get("SM_API_DENIED_TOOLS",
                         "Bash Write Edit NotebookEdit KillShell")
    return [t for t in raw.replace(",", " ").split() if t]


def daily_usd_cap() -> float:
    try:
        return float(os.environ.get("SM_API_DAILY_USD", "5"))
    except Exception:  # noqa: BLE001
        return 5.0


def daily_calls_cap() -> int:
    try:
        return int(os.environ.get("SM_API_DAILY_CALLS", "200"))
    except Exception:  # noqa: BLE001
        return 200


def max_call_usd() -> float:
    try:
        return float(os.environ.get("SM_API_MAX_CALL_USD", "1"))
    except Exception:  # noqa: BLE001
        return 1.0


def token_id(token: str | None) -> str:
    """토큰을 로그/집계용 짧은 해시로. 원문은 저장하지 않는다."""
    return "local" if not token else hashlib.sha256(token.encode()).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_file():
    return config.data_dir() / "api_usage.json"


def _audit_file():
    return config.data_dir() / "api_audit.jsonl"


def _load_usage() -> dict:
    f = _usage_file()
    if not f.exists():
        return {}
    try:
        with f.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _save_usage(d: dict) -> None:
    f = _usage_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, f)


def check_quota(token: str | None) -> tuple[bool, str, dict]:
    """호출 전 일일 한도 검사. 로컬(token None)은 신뢰하여 무제한.

    Returns: (allowed, reason, today_usage)
    """
    if not token:  # 로컬 루프백 등 — 신뢰
        return True, "", {"calls": 0, "usd": 0.0}
    key = f"{token_id(token)}:{_today()}"
    with _LOCK:
        u = _load_usage().get(key, {"calls": 0, "usd": 0.0})
    if u["calls"] >= daily_calls_cap():
        return False, f"일일 호출 한도 초과 ({daily_calls_cap()}회)", u
    if u["usd"] >= daily_usd_cap():
        return False, f"일일 비용 한도 초과 (${daily_usd_cap()})", u
    return True, "", u


def record_usage(token: str | None, cost: float | None,
                 count_call: bool = True) -> dict:
    """일일 사용량 누적. count_call=False 면 비용만 더하고 호출 수는 안 올린다."""
    if not token:
        return {"calls": 0, "usd": 0.0}
    key = f"{token_id(token)}:{_today()}"
    with _LOCK:
        d = _load_usage()
        u = d.get(key, {"calls": 0, "usd": 0.0})
        if count_call:
            u["calls"] += 1
        u["usd"] = round(u["usd"] + float(cost or 0), 6)
        d[key] = u
        _save_usage(d)
    return u


def effective_guardrails(session_id: str) -> dict:
    """전역 정책 + 세션 프로파일을 '더 빡센 쪽'으로 병합해 실효 가드레일을 만든다."""
    prof = profiles.get(session_id)
    denied = sorted(set(denied_tools()) | set(prof.get("denied_tools") or []))
    budgets = [b for b in (max_call_usd(), prof.get("max_budget_usd")) if b]
    return {
        "denied_tools": denied,                       # 합집합(차단 늘어남)
        "allowed_tools": prof.get("allowed_tools") or None,  # 화이트리스트(더 빡셈)
        "permission_mode": prof.get("permission_mode") or None,
        "model": prof.get("model") or None,
        "max_budget_usd": (min(budgets) if budgets else None),
        "system_prompt": prof.get("system_prompt") or None,
        "deny_patterns": prof.get("deny_patterns") or [],
        "max_prompt_chars": prof.get("max_prompt_chars"),
        "max_output_chars": prof.get("max_output_chars"),
    }


def match_denied(text: str, patterns: list[str]) -> str | None:
    """text 가 금지 정규식 중 하나에 걸리면 그 패턴을 반환(아니면 None)."""
    for pat in (patterns or []):
        try:
            if re.search(pat, text or ""):
                return pat
        except re.error:  # 잘못된 정규식 → 리터럴 포함검사로 폴백
            if pat and pat in (text or ""):
                return pat
    return None


def audit(entry: dict) -> None:
    f = _audit_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), **entry}
    with _LOCK:
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def usage_summary(recent_n: int = 30) -> dict:
    """감사 로그 + 오늘 사용량을 세션별·토큰별·일자별로 롤업한다(API 비용 모니터링)."""
    today = _today()
    caps = {"daily_usd": daily_usd_cap(), "daily_calls": daily_calls_cap()}

    # 오늘 토큰별 현황(한도 대비)
    with _LOCK:
        usage = _load_usage()
    today_tokens = []
    for key, u in usage.items():
        tid, _, day = key.partition(":")
        if day == today:
            today_tokens.append({"token": tid, "calls": u.get("calls", 0),
                                 "usd": round(u.get("usd", 0.0), 4)})
    today_tokens.sort(key=lambda x: x["usd"], reverse=True)

    # 감사 로그 롤업
    by_session: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    recent: list[dict] = []
    total_cost = 0.0
    total_calls = 0
    af = _audit_file()
    if af.exists():
        with af.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                recent.append(e)
                ev = e.get("event", "")
                cost = e.get("cost_usd") or 0
                if ev in ("ask", "resume_end"):
                    total_calls += 1
                    total_cost += cost
                    sid = e.get("session") or "?"
                    s = by_session.setdefault(sid, {"session": sid, "calls": 0, "usd": 0.0})
                    s["calls"] += 1
                    s["usd"] = round(s["usd"] + cost, 4)
                    day = (e.get("ts") or "")[:10]
                    d = by_day.setdefault(day, {"day": day, "calls": 0, "usd": 0.0})
                    d["calls"] += 1
                    d["usd"] = round(d["usd"] + cost, 4)

    return {
        "today": today,
        "caps": caps,
        "today_by_token": today_tokens,
        "by_session": sorted(by_session.values(), key=lambda x: x["usd"], reverse=True),
        "by_day": sorted(by_day.values(), key=lambda x: x["day"], reverse=True),
        "recent": list(reversed(recent))[:recent_n],
        "total_cost_usd": round(total_cost, 4),
        "total_calls": total_calls,
    }
