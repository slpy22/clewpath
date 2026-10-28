"""외부 서비스 대장(S2) — 서비스별 토큰·원본·한도. docs/designs/service-registry.md

토큰 하나 = 서비스 하나. 서비스 토큰으로는 자기 원본(bases)의 사본(fork) 질의와 자기 정보 조회만 된다.
oneshot=True 인 서비스는 세션 없는 단발 실행(POST /api/v1/oneshot)도 된다 — 이때는 원본이 없어도 된다(2026-10-28).
토큰 원문은 저장하지 않는다(sha256 해시 + 끝 4자리 힌트). 원문은 발급 응답에서 한 번만 보인다.

저장: data_dir/services.json (Host 자체 파일, claude 파일 아님)
    {"services": {"<id>": {name, bases:{용도: 세션id}, token_hash, token_hint, limits, status, created, updated}}}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from session_manager import config

_LOCK = threading.Lock()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")        # 토큰 접두사에 들어가므로 '_' 금지
PURPOSE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
STATUSES = ("active", "suspended", "revoked")
TOKEN_PREFIX = "cps_"
LIMIT_KEYS = ("daily_usd", "daily_calls", "max_call_usd")
DEFAULT_LIMITS = {"daily_usd": 5.0, "daily_calls": 200, "max_call_usd": 1.0}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _file():
    return config.data_dir() / "services.json"


def _load() -> dict:
    f = _file()
    if not f.exists():
        return {"services": {}, "requests": {}}
    try:
        with f.open(encoding="utf-8") as fh:
            d = json.load(fh)
        d.setdefault("services", {})
        d.setdefault("requests", {})
        return d
    except Exception:  # noqa: BLE001
        return {"services": {}, "requests": {}}


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, f)


def _hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _new_token(sid: str) -> str:
    return f"{TOKEN_PREFIX}{sid}_{secrets.token_hex(32)}"


def looks_like(token: str | None) -> bool:
    return bool(token) and token.startswith(TOKEN_PREFIX)


def public(rec: dict) -> dict:
    """화면·API 용(해시 제외)."""
    return {k: v for k, v in rec.items() if k != "token_hash"}


def _clean_limits(limits: dict | None, base: dict | None = None) -> dict:
    out = dict(base or DEFAULT_LIMITS)
    for k, v in (limits or {}).items():
        if k not in LIMIT_KEYS:
            raise ServiceError("bad_limits", f"알 수 없는 한도: {k}")
        try:
            v = int(v) if k == "daily_calls" else float(v)
        except Exception:  # noqa: BLE001
            raise ServiceError("bad_limits", f"한도 값이 숫자가 아닙니다: {k}")
        if v < 0:
            raise ServiceError("bad_limits", f"한도는 0 이상: {k}")
        out[k] = v
    return out


def _check_bases(bases: dict, d: dict, sid: str, allow_empty: bool = False) -> dict:
    """원본 검사: 형식·존재·가드레일 프로필·다른 서비스와 겹침. allow_empty: 단발 실행 전용 서비스는 원본 없이도 된다."""
    from session_manager import profiles
    from session_manager.scanner import scan_one
    if allow_empty and isinstance(bases, dict) and not bases:
        return {}
    if not isinstance(bases, dict) or not bases:
        raise ServiceError("bad_bases", "원본(bases)이 하나 이상 필요합니다. 예: {\"chat\": \"<세션 id>\"}")
    taken = {b: other for other, r in d["services"].items() if other != sid
             and r.get("status") != "revoked" for b in (r.get("bases") or {}).values()}
    out = {}
    for purpose, base in bases.items():
        if not isinstance(purpose, str) or not PURPOSE_RE.match(purpose):
            raise ServiceError("bad_bases", f"용도 이름은 영소문자·숫자·'-': {purpose}")
        base = str(base or "").strip()
        if not scan_one(base):
            raise ServiceError("base_not_found", f"원본 세션을 찾을 수 없습니다: {base[:8] or '(빈 값)'}", 404)
        # 가드레일 없는 원본은 묶지 않는다 — 개발 세션을 통째로 서비스에 내주는 사고 방지
        if not profiles.get(base):
            raise ServiceError("base_no_profile", f"가드레일 프로필이 없는 원본은 서비스에 묶을 수 없습니다: {base[:8]}")
        if base in taken:
            raise ServiceError("base_taken", f"원본 {base[:8]} 은 서비스 '{taken[base]}' 가 이미 씁니다.", 409)
        out[purpose] = base
    return out


def create(sid: str, name: str, bases: dict, limits: dict | None = None,
           extra: dict | None = None, oneshot: bool = False) -> tuple[dict, str]:
    """서비스를 등록하고 토큰 원문을 돌려준다(한 번만). extra: folder·purpose·approval 등 기록용."""
    sid = (sid or "").strip()
    if not ID_RE.match(sid):
        raise ServiceError("bad_id", "서비스 id 는 영소문자·숫자·'-' 2~31자입니다.")
    with _LOCK:
        d = _load()
        if sid in d["services"] and d["services"][sid].get("status") != "revoked":
            raise ServiceError("exists", f"이미 있는 서비스입니다: {sid}", 409)
        clean = _check_bases(bases, d, sid, allow_empty=bool(oneshot))
        token = _new_token(sid)
        now = int(time.time())
        rec = {"id": sid, "name": (name or sid).strip()[:60], "bases": clean,
               "token_hash": _hash(token), "token_hint": f"{TOKEN_PREFIX}{sid}_…{token[-4:]}",
               "limits": _clean_limits(limits), "status": "active", "oneshot": bool(oneshot),
               "created": now, "updated": now, **(extra or {})}
        d["services"][sid] = rec
        _save(d)
    return public(rec), token


def update(sid: str, name: str | None = None, bases: dict | None = None,
           limits: dict | None = None, status: str | None = None, oneshot: bool | None = None) -> dict:
    with _LOCK:
        d = _load()
        rec = d["services"].get(sid)
        if not rec:
            raise ServiceError("not_found", f"서비스가 없습니다: {sid}", 404)
        if rec.get("status") == "revoked" and status != "revoked":
            raise ServiceError("revoked", "폐기된 서비스는 바꿀 수 없습니다. 새로 등록하세요.", 409)
        if name is not None:
            rec["name"] = name.strip()[:60] or sid
        if oneshot is not None:
            rec["oneshot"] = bool(oneshot)
        if bases is not None:
            rec["bases"] = _check_bases(bases, d, sid, allow_empty=bool(rec.get("oneshot")))
        elif not rec.get("bases") and not rec.get("oneshot"):
            raise ServiceError("bad_bases", "원본이 없는 서비스는 단발 실행(oneshot)을 끌 수 없습니다. 원본을 먼저 묶으세요.")
        if limits is not None:
            rec["limits"] = _clean_limits(limits, rec.get("limits"))
        if status is not None:
            if status not in STATUSES:
                raise ServiceError("bad_status", f"상태는 {'/'.join(STATUSES)}")
            rec["status"] = status
        rec["updated"] = int(time.time())
        _save(d)
    return public(rec)


def rotate(sid: str) -> tuple[dict, str]:
    """새 토큰 발급 — 옛 토큰은 즉시 무효."""
    with _LOCK:
        d = _load()
        rec = d["services"].get(sid)
        if not rec:
            raise ServiceError("not_found", f"서비스가 없습니다: {sid}", 404)
        if rec.get("status") == "revoked":
            raise ServiceError("revoked", "폐기된 서비스입니다.", 409)
        token = _new_token(sid)
        rec["token_hash"] = _hash(token)
        rec["token_hint"] = f"{TOKEN_PREFIX}{sid}_…{token[-4:]}"
        rec["updated"] = rec["rotated"] = int(time.time())
        _save(d)
    return public(rec), token


def get(sid: str) -> dict | None:
    rec = _load()["services"].get(sid)
    return public(rec) if rec else None


def list_all() -> list[dict]:
    return [public(r) for r in sorted(_load()["services"].values(), key=lambda r: r.get("created", 0))]


def lookup(token: str | None) -> dict | None:
    """서비스 토큰이면 그 서비스(상태 무관), 아니면 None. 해시는 상수 시간 비교."""
    if not looks_like(token):
        return None
    sid = token[len(TOKEN_PREFIX):].rsplit("_", 1)[0]
    rec = _load()["services"].get(sid)
    if not rec or not hmac.compare_digest(rec.get("token_hash", ""), _hash(token)):
        return None
    return public(rec)


def identify(token: str | None) -> dict | None:
    """쓸 수 있는(active) 서비스 토큰이면 그 서비스."""
    rec = lookup(token)
    return rec if rec and rec.get("status") == "active" else None


def is_active(sid: str) -> bool:
    rec = _load()["services"].get(sid)
    return bool(rec) and rec.get("status") == "active"


def owns(rec: dict, session_id: str) -> bool:
    return session_id in (rec.get("bases") or {}).values()


def base_owner(session_id: str) -> str | None:
    """이 세션을 원본으로 쓰는 (폐기 안 된) 서비스 id."""
    for sid, r in _load()["services"].items():
        if r.get("status") != "revoked" and session_id in (r.get("bases") or {}).values():
            return sid
    return None


# 서비스 토큰이 부를 수 있는 HTTP 경로(그 밖은 403). WS 는 핸들러가 직접 검사한다.
_ASK = re.compile(r"^/api/v1/sessions/([^/]+)/ask$")
_PROFILE = re.compile(r"^/api/v1/sessions/([^/]+)/profile$")


def http_allowed(rec: dict, method: str, path: str) -> bool:
    if path == "/api/v1/service/me" and method == "GET":
        return True
    if path == "/api/v1/oneshot" and method == "POST":
        return bool(rec.get("oneshot"))
    m = _ASK.match(path)
    if m and method == "POST":
        return owns(rec, m.group(1))
    m = _PROFILE.match(path)
    if m and method == "GET":
        return owns(rec, m.group(1))
    return False
