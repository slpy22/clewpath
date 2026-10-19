"""세션별 가드레일 프로파일 — 사이드카 JSON 저장.

소유자가 세션에 추가 정책을 걸어두면 fork 시 전역 정책과 '교집합(더 빡센 쪽)'으로 적용된다.
원본 jsonl 은 절대 건드리지 않는다(라벨과 동일 방식).

프로파일 필드(모두 선택):
    permission_mode   "plan" 등 — 읽기전용 등 권한 모드
    allowed_tools     화이트리스트(예: ["Read","Grep"]) — 있으면 이것만 허용
    denied_tools      추가 차단 도구(전역 차단과 합집합)
    model             모델 고정(예: "fable")
    max_budget_usd    호출당 비용 상한(전역과 min)
    system_prompt     append-system-prompt(행동/주제 제약)
    deny_patterns     프롬프트/응답에서 차단할 정규식 목록
    max_prompt_chars  프롬프트 최대 길이
    max_output_chars  응답 최대 길이
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from session_manager import config

_LOCK = threading.Lock()
_FIELDS = ("permission_mode", "allowed_tools", "denied_tools", "model",
           "max_budget_usd", "system_prompt", "deny_patterns",
           "max_prompt_chars", "max_output_chars")
_LIST_FIELDS = ("allowed_tools", "denied_tools", "deny_patterns")
_NUM_FIELDS = ("max_budget_usd", "max_prompt_chars", "max_output_chars")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file():
    return config.data_dir() / "profiles.json"


def _load() -> dict:
    f = _file()
    if not f.exists():
        return {"version": 1, "sessions": {}}
    try:
        with f.open(encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and "sessions" in d else {"version": 1, "sessions": {}}
    except Exception:  # noqa: BLE001
        return {"version": 1, "sessions": {}}


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, f)


def get(session_id: str) -> dict:
    with _LOCK:
        return _load()["sessions"].get(session_id, {})


def all_records() -> dict:
    with _LOCK:
        return dict(_load()["sessions"])


def _clean_list(v) -> list[str]:
    out, seen = [], set()
    for x in (v or []):
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out


def set_record(session_id: str, **fields) -> dict:
    """프로파일 부분 업데이트(upsert). 제공한 필드만 갱신. 전부 비면 삭제."""
    with _LOCK:
        d = _load()
        rec = dict(d["sessions"].get(session_id, {}))
        rec.pop("updated_at", None)
        for key in _FIELDS:
            if key not in fields:
                continue
            val = fields[key]
            if key in _LIST_FIELDS:
                rec[key] = _clean_list(val)
            elif key in _NUM_FIELDS:
                rec[key] = (float(val) if key == "max_budget_usd" else int(val)) \
                    if val not in (None, "") else None
            else:
                rec[key] = (str(val).strip() if val not in (None, "") else None)
        # 빈 값 제거 → 실제 필드가 하나도 없으면 레코드 삭제
        rec = {k: v for k, v in rec.items() if v not in (None, "", [])}
        if not rec:
            d["sessions"].pop(session_id, None)
            _save(d)
            return {}
        rec["updated_at"] = _now()
        d["sessions"][session_id] = rec
        _save(d)
        return rec


def delete(session_id: str) -> bool:
    with _LOCK:
        d = _load()
        if session_id in d["sessions"]:
            d["sessions"].pop(session_id)
            _save(d)
            return True
        return False
