"""claude 가 오래된 대화 기록을 스스로 지우는 시점 예고(2026-10-13 사장님 — E:\\001_KIS 세션 3개 실종 사고).

claude 는 시작할 때 마지막 수정 뒤 `cleanupPeriodDays`(~/.claude/settings.json, 기본 30일)가 지난 세션 기록을
지운다. ClewPath 는 그 설정을 **읽기만** 하고(불가침 원칙 1 — 값을 바꾸지 않는다), 세션 파일 수정 시각 + 보관 기간으로
'지워질 즈음' 을 계산해 목록·🔔 할 일·폰 알림으로 미리 알린다. 실제 삭제는 그 뒤 claude 가 처음 뜰 때라 '쯤' 이다.
살리는 방법: 그 세션을 열어 한 번 주고받으면(수정 시각 갱신) 기한이 다시 늘어난다 · 내보내기로 따로 보관.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_DAYS = 30
SOON_DAYS = 7            # 목록·할 일에 보이기 시작
ALERT_DAYS = 3           # 🔔 숫자·폰 알림
_cache: tuple[tuple, int | None] | None = None   # ((경로, mtime, 크기), 보관 일수)


def _settings_file() -> Path:
    from session_manager import scanner
    return scanner.projects_dir().parent / "settings.json"


def period_days() -> int | None:
    """claude 보관 기간(일). 설정이 없으면 기본 30, 0 이하·이상한 값이면 None(예고 안 함)."""
    global _cache
    f = _settings_file()
    try:
        st = f.stat()
        mt = (str(f), st.st_mtime, st.st_size)
    except OSError:
        return DEFAULT_DAYS
    if _cache and _cache[0] == mt:
        return _cache[1]
    days: int | None = DEFAULT_DAYS
    try:
        v = json.loads(f.read_text(encoding="utf-8")).get("cleanupPeriodDays")
        if v is not None:
            days = int(v) if isinstance(v, (int, float)) and v > 0 else None
    except Exception:  # noqa: BLE001  설정을 못 읽으면 기본값으로 예고(보수적으로 알리는 쪽)
        days = DEFAULT_DAYS
    _cache = (mt, days)
    return days


def delete_at(mtime: float | None, days: int | None = None) -> int | None:
    """이 기록을 claude 가 지울 즈음(epoch 초). 알 수 없으면 None."""
    d = period_days() if days is None else days
    if not d or not mtime:
        return None
    return int(mtime + d * 86400)


def soon(metas, within_days: int = SOON_DAYS, now: float | None = None) -> list[dict]:
    """within_days 안에 지워질 세션(가까운 순)."""
    now = now or time.time()
    d = period_days()
    out = []
    for m in metas:
        t = delete_at(getattr(m, "mtime", None), d)
        if t and t - now <= within_days * 86400:
            out.append({"session_id": m.session_id, "cwd": m.cwd, "delete_at": t,
                        "title": m.custom_title or m.ai_title or m.slug or m.session_id[:8]})
    out.sort(key=lambda x: x["delete_at"])
    return out
