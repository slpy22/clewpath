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

2026-11-05 데이터 저장 정리(R1): 정본은 상태 DB(store — session_meta·folder_meta 표). labels.json 은 옛 판이 되돌림 뒤
읽을 수 있게 같은 모양으로 계속 쓰는 거울이다(바깥에서 바뀌면 store 가 가져온다). 함수 이름·반환 모양은 그대로.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from session_manager import config, store

_LOCK = threading.Lock()
# picker: 피커 미표시 세션의 노출 정책 - "expose"(claude 재개 목록에 노출 허용) 또는 없음(은닉 유지)
# keep: "1" 이면 쉬는 세션 정리(housekeeping)에서 제외 — 항상 켜 두는 세션(2026-10-12)
_FIELDS = ("labels", "name", "color", "note", "picker", "model", "keep")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _meta_cols(rec) -> dict:
    rec = rec if isinstance(rec, dict) else {}
    return {"name": rec.get("name"), "keep": 1 if rec.get("keep") else None, "model": rec.get("model")}


store.register(store.Doc(
    name="labels", file=lambda: config.labels_file(),
    colls=[store.Coll("session_meta", "sessions", ("session_id",), pc=True, derive=_meta_cols),
           store.Coll("folder_meta", "folders", ("folder_key",), pc=True)],
    empty=lambda: {"version": 1, "sessions": {}},
    dump=lambda d: json.dumps(d, ensure_ascii=False, indent=2),
    valid=lambda d: isinstance(d, dict) and isinstance(d.get("sessions"), dict)))


def _load() -> dict:
    try:
        data = store.read_doc("labels")
    except store.StoreUnavailable:  # 상태 DB 를 못 열어도 죽지 않음(원본 세션과 무관)
        return {"version": 1, "sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"version": 1, "sessions": {}}
    return data


def _save(data: dict) -> None:
    store.save_doc("labels", data)


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


def model_of(session_id: str) -> str | None:
    """세션별 모델 오버라이드(없으면 None). ClewPath 재개 시 --model 로 쓰인다."""
    try:
        m = get(session_id).get("model")
        return m or None
    except Exception:  # noqa: BLE001
        return None


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
        if not rec.get("labels") and not any(rec.get(k) for k in ("name", "color", "note", "picker", "model", "keep")):
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


# ---- 별도 프로젝트 폴더(2026-11-03 사장님) ----
# 목록은 세션이 있는 폴더의 하위 폴더 세션을 그 폴더 묶음에 함께 보인다(PWA). 여기 적힌 폴더는 하위여도 묶지 않고
# 따로(같은 높이로) 보인다. 세션이 아니라 폴더 단위 — 같은 폴더의 세션은 늘 함께 움직인다. 열쇠 = folder_key(cwd).
def folder_key(cwd: str) -> str:
    """경로 비교 열쇠 — PWA pathKey 와 같은 규칙(구분자 /, 끝 / 없음, 드라이브 경로는 소문자)."""
    k = str(cwd or "").replace("\\", "/").rstrip("/")
    if len(k) >= 2 and k[1] == ":" and k[0].isalpha():
        k = k.lower()
    return k


def standalone_folders() -> set[str]:
    with _LOCK:
        return set((_load().get("folders") or {}).keys())


def set_standalone(cwd: str, on: bool) -> bool:
    """폴더를 별도 프로젝트로 두거나(on) 되돌린다. 바뀌었으면 True."""
    key = folder_key(cwd)
    if not key:
        return False
    with _LOCK:
        data = _load()
        folders = dict(data.get("folders") or {})
        if on:
            if key in folders:
                return False
            folders[key] = {"standalone": True, "cwd": str(cwd), "updated_at": _now()}
        else:
            if key not in folders:
                return False
            folders.pop(key)
        data["folders"] = folders
        _save(data)
        return True
