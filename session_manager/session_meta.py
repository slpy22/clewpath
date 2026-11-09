"""세션에 ClewPath 가 덧붙이는 값 — 태그·재개 모델 고정·항상 켜 두기·재개 전 알림 끄기, 폴더별 별도 프로젝트 표시.

원본 jsonl 은 절대 수정하지 않는다. session_id(UUID, 전역 고유) 기준으로 따로 저장하고 목록 조회 때 합쳐서 보여준다.

저장 형식(이 파일 밖에서는 tags·skip_resume_notice 로만 본다):
{
  "version": 1,
  "sessions": {
    "<session_id>": {
      "labels": ["work", "북한법"],    # 코드의 tags
      "picker": "expose",              # 코드의 skip_resume_notice=True
      "model": "...", "keep": "1",
      "updated_at": "2026-06-30T..."
    }
  }
}

디스크 키가 labels·picker 인 까닭: 정본은 상태 DB(store — session_meta·folder_meta 표), labels.json 은 옛 판이 되돌림 뒤 읽는 거울이라
모양을 바꾸면 되돌림이 깨진다(2026-11-05 데이터 저장 정리 R1). 저장 키는 R2 때 함께 바꾼다.
2026-11-09 사장님: 이름이 너무 많다 → ClewPath 표시 이름(name)·색(color)·메모(note) 제거. 읽지도 받지도 않되 저장된 값은 지우지 않는다
(옛 판으로 되돌리면 그대로 보인다).

동시 쓰기 안전: 모듈 락 + 원자적 쓰기(임시파일 → os.replace).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from session_manager import config, store

_LOCK = threading.Lock()
# 저장 레코드의 키. 코드 밖으로 나가는 이름은 _view 가 정한다.
_TAGS = "labels"
_NOTICE = "picker"
_LEGACY = ("name", "color", "note")      # 제거한 값 — 옛 판 되돌림용으로 디스크에만 남는다


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


def _clean_tags(tags) -> list[str]:
    """중복 제거 + 공백 정리(순서 보존)."""
    out: list[str] = []
    seen = set()
    for item in (tags or []):
        s = str(item).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _view(rec: dict | None) -> dict:
    """저장 레코드 → 코드가 보는 모양. 없으면 빈 dict."""
    if not rec:
        return {}
    out = {k: rec[k] for k in ("model", "keep", "updated_at") if rec.get(k)}
    out["tags"] = list(rec.get(_TAGS) or [])
    if rec.get(_NOTICE) == "expose":
        out["skip_resume_notice"] = True
    return out


def get(session_id: str) -> dict:
    """세션의 레코드(없으면 빈 dict)."""
    with _LOCK:
        return _view(_load()["sessions"].get(session_id))


def raw(session_id: str) -> dict:
    """저장된 그대로(옛 값 포함). 휴지통 사본처럼 되살릴 때 원래 모양이 필요한 곳만 쓴다."""
    with _LOCK:
        return dict(_load()["sessions"].get(session_id) or {})


def get_many(session_ids) -> dict:
    """여러 세션의 레코드를 한 번에. {session_id: record}."""
    ids = set(session_ids)
    with _LOCK:
        sess = _load()["sessions"]
        return {sid: _view(sess[sid]) for sid in ids if sid in sess}


def model_of(session_id: str) -> str | None:
    """세션별 모델 오버라이드(없으면 None). ClewPath 재개 시 --model 로 쓰인다."""
    try:
        m = get(session_id).get("model")
        return m or None
    except Exception:  # noqa: BLE001
        return None


def set_record(session_id: str, **fields) -> dict:
    """레코드를 부분 업데이트(upsert). 제공한 필드만 갱신.

    tags=[...], skip_resume_notice=bool, model=str|None, keep=str|None.
    전부 비면 레코드 삭제.
    """
    unknown = set(fields) - {"tags", "skip_resume_notice", "model", "keep"}
    if unknown:
        raise TypeError(f"모르는 세션 속성: {sorted(unknown)}")
    with _LOCK:
        data = _load()
        rec = dict(data["sessions"].get(session_id, {}))
        if "tags" in fields:
            rec[_TAGS] = _clean_tags(fields["tags"])
        if "skip_resume_notice" in fields:
            rec[_NOTICE] = "expose" if fields["skip_resume_notice"] else None
        for key in ("model", "keep"):
            if key in fields:
                val = fields[key]
                rec[key] = (str(val).strip() if val not in (None, "") else None)
        # None/빈 값 정리
        rec = {k: v for k, v in rec.items()
               if k == _TAGS or v not in (None, "")}
        # 옛 값(_LEGACY)만 남아도 레코드를 지우지 않는다 — 옛 판으로 되돌리면 그대로 보이게
        if not rec.get(_TAGS) and not any(rec.get(k) for k in (_NOTICE, "model", "keep", *_LEGACY)):
            data["sessions"].pop(session_id, None)
            _save(data)
            return {}
        rec["updated_at"] = _now()
        data["sessions"][session_id] = rec
        _save(data)
        return _view(rec)


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
        return {sid: _view(rec) for sid, rec in _load()["sessions"].items()}


def tag_counts() -> list[dict]:
    """전체에서 쓰인 태그와 사용 횟수(많은 순)."""
    counts: dict[str, int] = {}
    with _LOCK:
        for rec in _load()["sessions"].values():
            for t in rec.get(_TAGS, []):
                counts[t] = counts.get(t, 0) + 1
    return [{"tag": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def session_ids_with_tag(tag: str) -> set[str]:
    with _LOCK:
        return {sid for sid, rec in _load()["sessions"].items()
                if tag in rec.get(_TAGS, [])}


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
