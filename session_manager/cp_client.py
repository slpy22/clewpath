"""컨트롤플레인(CP) 클라이언트 — 자격증명 자동 발급 + 단기 agent JWT 조달. (T7)

설계 원칙(eng-review D7 / 부록 C.2):
- **fail-open**: CP 가 불통이면 예외를 던지지 않고 None 을 돌려준다. 커넥터는 기존 공유토큰으로
  릴레이에 붙어 계속 동작한다(장애가 기존 사용자 접속을 끊지 않게). 단 무음 실패 금지 —
  상태를 기록해 로컬 웹이 배지로 보여준다.
- **비밀은 로컬 파일에만**: ~/.claude/session_manager/cp_credentials.json (0600 의도).
  secret/claim_secret 원문은 이 PC 밖으로 나가지 않는다(claim 시에만 로컬 웹이 읽음).
- CP 미설정(SM_CP_URL 없음)이면 전 기능 no-op → 기존 배포 동작 100% 유지.

상태 머신:
    [미설정]  SM_CP_URL 없음        → mode=shared  (CP 안 씀. 현행 유지)
    [발급전]  자격증명 파일 없음     → provision 시도 → 성공: 저장 / 실패: mode=fallback
    [정상]    JWT 유효              → mode=jwt
    [장애]    CP 불통·거부          → mode=fallback (공유토큰으로 접속 유지)
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

from session_manager import config

_FILE_NAME = "cp_credentials.json"

# 마지막 상태(로컬 웹 배지용). 프로세스 메모리.
_state: dict = {"mode": "shared", "reason": "cp_not_configured",
                "cp_url": None, "connector_id": None, "checked_at": None,
                "jwt_exp": None}


def log(msg: str) -> None:
    """콘솔 인코딩(cp949 등)에 없는 글자 때문에 로그가 프로세스를 죽이지 않게 한다.

    T7 에서 실제로 겪은 사고: print 안의 '⚠'/'…' 가 Windows 콘솔에서 UnicodeEncodeError
    → 커넥터 접속 루프가 매 시도마다 크래시 → 릴레이에 영구 미접속.
    로그는 절대 기능을 깨뜨려선 안 된다.
    """
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        try:
            enc = (sys.stdout.encoding or "ascii")
            print(msg.encode(enc, "replace").decode(enc, "replace"))
        except Exception:  # noqa: BLE001
            pass


def set_mode(mode: str, reason: str) -> None:
    """실제 접속에 쓰인 모드를 확정 기록(호출자=커넥터). status 와 실제의 불일치 방지."""
    _set_state(mode, reason)


def cp_url() -> str | None:
    u = os.environ.get("SM_CP_URL")
    return u.rstrip("/") if u else None


def _file():
    return config.data_dir() / _FILE_NAME


def load_creds() -> dict | None:
    f = _file()
    if not f.exists():
        return None
    try:
        # utf-8-sig: install.ps1 이 PowerShell 5.1 에서 돌면 이 파일에 BOM 이 붙는데,
        # 순수 utf-8 로는 json.loads 가 거부해 발급받은 자격증명이 조용히 무시된다.
        return json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None


def save_creds(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)   # POSIX 에서만 의미. Windows 는 ACL 기본값 사용.
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    """로컬 웹 배지용 현재 연결 모드 스냅샷."""
    creds = load_creds()
    return {**_state,
            "has_credentials": bool(creds),
            "connector_id": (creds or {}).get("connector_id") or _state.get("connector_id"),
            "room_key": (creds or {}).get("room_key")}


def _set_state(mode: str, reason: str, **kw) -> None:
    _state.update({"mode": mode, "reason": reason, "cp_url": cp_url(),
                   "checked_at": int(time.time()), **kw})


def mark_room_mismatch(cp_room: str, used_room: str) -> None:
    """CP 발급 room 과 실제 사용 room 이 다름을 기록(무음 실패 금지 — 배지로 노출).

    기존 페어링(폰·QR)을 지키려고 이사하지 않은 상태. P1b(릴레이 JWT 검증) 전에
    room 정합을 맞춰야 하므로 사용자에게 보인다.
    """
    _state["room_mismatch"] = {"cp_room": cp_room, "used_room": used_room}


def clear_room_mismatch() -> None:
    _state.pop("room_mismatch", None)


def provision(client: httpx.Client, display_name: str = "") -> dict | None:
    """자격증명이 없으면 CP 에 익명 발급 요청. 성공 시 저장하고 반환, 실패 시 None."""
    base = cp_url()
    if not base:
        return None
    try:
        r = client.post(f"{base}/provision/anonymous",
                        json={"display_name": display_name or os.environ.get("COMPUTERNAME", "")},
                        timeout=10)
    except Exception as e:  # noqa: BLE001
        _set_state("fallback", f"provision_error:{type(e).__name__}")
        return None
    if r.status_code == 429:
        _set_state("fallback", "provision_rate_limited")
        return None
    if r.status_code != 200:
        _set_state("fallback", f"provision_http_{r.status_code}")
        return None
    d = r.json()
    creds = {
        "cp_url": base,
        "connector_id": d["connector_id"],
        "room_key": d["room_key"],
        "credential_public_id": d["credential_public_id"],
        "secret": d["secret"],
        "claim_secret": d.get("claim_secret"),
        "created_at": int(time.time()),
    }
    save_creds(creds)
    log(f"[cp] 자격증명 발급됨 connector={creds['connector_id']} room={creds['room_key']}")
    return creds


def issue_client_credential(device_id: str = "", name: str = "") -> dict | None:
    """이 커넥터의 room 에 붙을 기기별 client 자격증명을 CP 에서 발급받는다.

    공유 client 토큰을 대체한다. 실패하면 None → 호출자는 기존 공유 토큰 방식으로 폴백.
    """
    base = cp_url()
    creds = load_creds()
    if not base or not creds:
        return None
    try:
        with httpx.Client() as client:
            r = client.post(f"{base}/client/credential",
                            json={"public_id": creds["credential_public_id"],
                                  "secret": creds["secret"],
                                  "device_id": device_id, "name": name},
                            timeout=10)
        if r.status_code != 200:
            log(f"[cp] client 자격증명 발급 실패 http={r.status_code}")
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log(f"[cp] client 자격증명 발급 오류: {type(e).__name__}")
        return None


def revoke_client_credential(client_public_id: str) -> bool:
    """기기 삭제와 연동해 client 자격증명을 회수."""
    base = cp_url()
    creds = load_creds()
    if not base or not creds or not client_public_id:
        return False
    try:
        with httpx.Client() as client:
            r = client.post(f"{base}/client/credential/revoke",
                            json={"public_id": creds["credential_public_id"],
                                  "secret": creds["secret"],
                                  "client_public_id": client_public_id},
                            timeout=10)
        return r.status_code == 200 and bool(r.json().get("revoked"))
    except Exception:  # noqa: BLE001
        return False


def fetch_jwt(display_name: str = "") -> dict | None:
    """agent JWT 를 조달한다. 실패하면 None(=fail-open, 호출자는 공유토큰으로 계속).

    반환: {"token", "expires_at", "room_key"} 또는 None
    """
    base = cp_url()
    if not base:
        _set_state("shared", "cp_not_configured")
        return None

    with httpx.Client() as client:
        creds = load_creds()
        if not creds or creds.get("cp_url") != base:
            creds = provision(client, display_name)
            if not creds:
                return None   # 상태는 provision 에서 기록됨
        try:
            r = client.post(f"{base}/connector/token",
                            json={"public_id": creds["credential_public_id"],
                                  "secret": creds["secret"]},
                            timeout=10)
        except Exception as e:  # noqa: BLE001
            _set_state("fallback", f"token_error:{type(e).__name__}",
                       connector_id=creds.get("connector_id"))
            return None
        if r.status_code == 401:
            # 자격증명이 폐기됨 → 재발급 시도(사용자가 CP 에서 삭제한 경우 등)
            _set_state("fallback", "credential_revoked",
                       connector_id=creds.get("connector_id"))
            log("[cp] 자격증명이 거부됨(폐기?) -> 재발급 시도")
            creds = provision(client, display_name)
            if not creds:
                return None
            try:
                r = client.post(f"{base}/connector/token",
                                json={"public_id": creds["credential_public_id"],
                                      "secret": creds["secret"]}, timeout=10)
            except Exception as e:  # noqa: BLE001
                _set_state("fallback", f"token_error:{type(e).__name__}")
                return None
        if r.status_code != 200:
            _set_state("fallback", f"token_http_{r.status_code}",
                       connector_id=creds.get("connector_id"))
            return None
        out = r.json()
        _set_state("jwt", "ok", connector_id=creds.get("connector_id"),
                   jwt_exp=out.get("expires_at"))
        return out
