"""관제 그룹 저장소 — 관리 에이전트 1 + 하위 N 세션 묶음과 알림 설정.

왜 저장하는가: 관제 오버레이의 그룹은 WS 연결 단위(서버 메모리)라 앱을 닫으면 사라진다.
앱이 닫혀 있어도 "관리 턴 종료·하위 응답 완료" 푸시를 받으려면 Host 가 그룹을 알아야 한다.
labels 사이드카와 같은 방식: 데이터 폴더의 JSON 하나, 원본 jsonl 무접촉, 손상돼도 기동 유지.

알림 소스는 훅이다(실증 2026-09-12: `claude -p` 헤드리스 하위 세션도 Stop/UserPromptSubmit
훅이 Host 에 도달) — push.notify_from_event 가 소속 그룹의 notify 플래그로 라우팅한다.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone

from session_manager import config

# 그룹 알림 기본값. sub_start 는 호출이 잦으면 시끄러워 기본 off.
# error = 관리 세션의 하위 호출(claude -p --resume)이 오류로 끝남 — 훅이 아니라
# monwatch(관리 jsonl tail)가 감지한다. 기존 저장 파일에 키가 없으면 기본 on.
DEFAULT_NOTIFY = {"manager_stop": True, "sub_stop": True, "sub_start": False, "error": True}
_MAX_GROUPS = 50


def groups_file():
    return config.data_dir() / "monitor_groups.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    path = groups_file()
    if not path.exists():
        return {"version": 1, "groups": {}}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
            return {"version": 1, "groups": {}}
        return data
    except Exception:  # noqa: BLE001  손상 시에도 죽지 않음(원본 세션과 무관)
        return {"version": 1, "groups": {}}


def _save(data: dict) -> None:
    path = groups_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 원자적 교체


def norm_notify(n) -> dict:
    """알 수 없는 키는 버리고 기본값으로 채운다(입력이 뭐든 안전한 dict)."""
    out = dict(DEFAULT_NOTIFY)
    if isinstance(n, dict):
        for k in DEFAULT_NOTIFY:
            if k in n:
                out[k] = bool(n[k])
    return out


def _clean_sids(subs, manager: str) -> list[str]:
    out: list[str] = []
    for s in subs or []:
        s = str(s or "").strip()
        if s and s != manager and s not in out:
            out.append(s)
    return out


def _norm_group(g: dict) -> dict:
    """읽기용 사본: notify 를 기본값으로 채운다 — 새 플래그(error 등)가 생겨도 옛 저장 파일을
    읽는 API·UI·워처가 같은 값을 본다(저장 파일 자체는 다음 save/update 때 갱신)."""
    out = dict(g)
    out["notify"] = norm_notify(g.get("notify"))
    return out


# ---------------------------------------------------------------- 공개 API

def list_groups() -> list[dict]:
    """최근 갱신 순."""
    gs = [_norm_group(g) for g in _load()["groups"].values()]
    gs.sort(key=lambda g: g.get("updated_at") or "", reverse=True)
    return gs


def get(gid: str) -> dict | None:
    g = _load()["groups"].get(str(gid or ""))
    return _norm_group(g) if g else None


def save(name: str, manager: str, subs, labels=None, notify=None, gid: str | None = None) -> dict:
    """생성(gid 없음) 또는 교체(gid 있음). manager 필수, name 비면 manager 라벨로."""
    manager = str(manager or "").strip()
    if not manager:
        raise ValueError("manager_required")
    labels = {str(k): str(v) for k, v in (labels or {}).items() if isinstance(labels, dict)} \
        if isinstance(labels, dict) else {}
    subs_c = _clean_sids(subs, manager)
    name = str(name or "").strip() or labels.get(manager) or manager[:8]
    data = _load()
    groups = data["groups"]
    if gid and gid in groups:
        g = groups[gid]
        g.update({"name": name, "manager": manager, "subs": subs_c, "labels": labels,
                  "notify": norm_notify(notify if notify is not None else g.get("notify")),
                  "updated_at": _now()})
    else:
        if len(groups) >= _MAX_GROUPS:
            raise ValueError("too_many_groups")
        gid = secrets.token_hex(4)
        while gid in groups:
            gid = secrets.token_hex(4)
        g = {"id": gid, "name": name, "manager": manager, "subs": subs_c, "labels": labels,
             "notify": norm_notify(notify), "created_at": _now(), "updated_at": _now()}
        groups[gid] = g
    _save(data)
    return g


def update(gid: str, name: str | None = None, notify=None) -> dict | None:
    """이름·알림 플래그만 바꾼다(부분 갱신). 없으면 None."""
    data = _load()
    g = data["groups"].get(str(gid or ""))
    if not g:
        return None
    if name is not None and str(name).strip():
        g["name"] = str(name).strip()
    if isinstance(notify, dict):
        cur = norm_notify(g.get("notify"))
        for k in DEFAULT_NOTIFY:
            if k in notify:
                cur[k] = bool(notify[k])
        g["notify"] = cur
    g["updated_at"] = _now()
    _save(data)
    return g


def delete(gid: str) -> bool:
    data = _load()
    if str(gid or "") not in data["groups"]:
        return False
    data["groups"].pop(str(gid))
    _save(data)
    return True


def groups_for_session(session_id: str) -> list[tuple[dict, str]]:
    """이 세션이 속한 그룹들과 역할('manager'|'sub'). 훅 이벤트 라우팅용."""
    sid = str(session_id or "")
    out: list[tuple[dict, str]] = []
    if not sid:
        return out
    for g in _load()["groups"].values():
        if g.get("manager") == sid:
            out.append((_norm_group(g), "manager"))
        elif sid in (g.get("subs") or []):
            out.append((_norm_group(g), "sub"))
    return out


def label_of(g: dict, session_id: str) -> str:
    labels = g.get("labels") or {}
    return str(labels.get(session_id) or session_id[:8])
