"""세션 라벨 — JSON 사이드카 저장소.

원본 jsonl 은 절대 수정하지 않는다. 라벨은 session_id(UUID, 전역 고유) 기준으로
별도 JSON 파일(config.labels_file)에 저장하고, 목록 조회 시 합쳐서 보여준다.

저장 형식:
{
  "version": 1,
  "sessions": {
    "<session_id>": {
      "labels": ["work", "북한법"],
      "name": "사이트 SEO 작업",       # 선택: 사용자 지정 표시명
      "color": "#58a6ff",              # 선택
      "note": "...",                   # 선택
      "updated_at": "2026-06-30T..."
    }
  }
}

동시 쓰기 안전: 모듈 락 + 원자적 쓰기(임시파일 → os.replace).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from session_manager import config

_LOCK = threading.Lock()
# picker: 피커 미표시 세션의 노출 정책 - "expose"(claude 재개 목록에 노출 허용) 또는 없음(은닉 유지)
_FIELDS = ("labels", "name", "color", "note", "picker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    path = config.labels_file()
    if not path.exists():
        return {"version": 1, "sessions": {}}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "sessions" not in data:
            return {"version": 1, "sessions": {}}
        return data
    except Exception:  # noqa: BLE001  손상 시에도 죽지 않음(원본 세션과 무관)
        return {"version": 1, "sessions": {}}


def _save(data: dict) -> None:
    path = config.labels_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 원자적 교체


def _clean_labels(labels) -> list[str]:
    """중복 제거 + 공백 정리(순서 보존)."""
    out: list[str] = []
    seen = set()
    for item in (labels or []):
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get(session_id: str) -> dict:
    """세션의 라벨 레코드(없으면 빈 dict)."""
    with _LOCK:
        return _load()["sessions"].get(session_id, {})


def get_many(session_ids) -> dict:
    """여러 세션의 라벨을 한 번에. {session_id: record}."""
    ids = set(session_ids)
    with _LOCK:
        sess = _load()["sessions"]
        return {sid: sess[sid] for sid in ids if sid in sess}


def set_record(session_id: str, **fields) -> dict:
    """라벨 레코드를 부분 업데이트(upsert). 제공한 필드만 갱신.

    labels=[...], name=str|None, color=str|None, note=str|None.
    빈 값으로 전부 비우면 레코드 삭제.
    """
    with _LOCK:
        data = _load()
        rec = dict(data["sessions"].get(session_id, {}))
        for key in _FIELDS:
            if key in fields:
                val = fields[key]
                if key == "labels":
                    rec[key] = _clean_labels(val)
                else:
                    rec[key] = (str(val).strip() if val not in (None, "") else None)
        # None/빈 값 정리
        rec = {k: v for k, v in rec.items()
               if k == "labels" or v not in (None, "")}
        if not rec.get("labels") and not any(rec.get(k) for k in ("name", "color", "note", "picker")):
            data["sessions"].pop(session_id, None)
            _save(data)
            return {}
        rec["updated_at"] = _now()
        data["sessions"][session_id] = rec
        _save(data)
        return rec


def delete(session_id: str) -> bool:
    with _LOCK:
        data = _load()
        if session_id in data["sessions"]:
            data["sessions"].pop(session_id)
            _save(data)
            return True
        return False


def all_records() -> dict:
    with _LOCK:
        return dict(_load()["sessions"])


def label_counts() -> list[dict]:
    """전체에서 쓰인 라벨과 사용 횟수(많은 순)."""
    counts: dict[str, int] = {}
    with _LOCK:
        for rec in _load()["sessions"].values():
            for lb in rec.get("labels", []):
                counts[lb] = counts.get(lb, 0) + 1
    return [{"label": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def session_ids_with_label(label: str) -> set[str]:
    with _LOCK:
        return {sid for sid, rec in _load()["sessions"].items()
                if label in rec.get("labels", [])}
