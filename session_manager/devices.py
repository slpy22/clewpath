"""기기 레지스트리 — 릴레이(외부) 클라이언트의 '기기별' 인증/폐기.

설계:
- 각 기기는 1회 발급 토큰(dev-...)으로 식별. 저장은 SHA-256 해시만(원문 보관 안 함).
- 기기 인증은 **항상 강제**(끌 수 없음): 커넥터가 인증되지 않은 클라이언트의 모든 메서드(ping 제외)를 거부.
  등록 기기가 0개면 아무도 통과 못 하므로 사실상 '외부 전면 차단'(별도 킬 스위치 없음).
- 파일: ~/.claude/session_manager/devices.json  (로컬 006만 기록, 커넥터는 같은 프로세스에서 읽음)

보안 경계:
- 이 레지스트리는 '누가(기기)'를 다루는 애플리케이션 계층 인증이다.
- 릴레이 접속용 공유 client 토큰은 '릴레이에 닿을 수 있는가'만 담당(전송 계층).
- 특권 동작의 2차 확인(OTP/grace)은 owner2fa 가 별도로 담당(계층 분리).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

from session_manager import config


def _file():
    return config.data_dir() / "devices.json"


def _blank() -> dict:
    return {"enforced": False, "devices": []}


def _data() -> dict:
    f = _file()
    if not f.exists():
        return _blank()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return _blank()
    d.setdefault("enforced", False)
    d.setdefault("devices", [])
    return d


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


# ---- 기기 인증 ----
def enforced() -> bool:
    """외부(릴레이) 접속의 기기 인증 — **항상 켜져 있다. 끌 수 없다.**

    끌 수 있게 두면 '기기 삭제'가 실효를 잃는다(실측: 꺼진 상태에서 삭제해도 기존 연결이
    유지되고, 폰이 캐시한 JWT 로 TTL 동안 재접속까지 됐다). 릴레이는 무상태라 회수를
    모르므로, 회수를 강제할 수 있는 유일한 지점이 커넥터다. 그래서 상수로 고정한다.

    로컬(루프백) 접근은 이 값과 무관하게 항상 열려 있으므로 스스로 잠길 위험은 없다.
    """
    return True


# ---- 기기 CRUD ----
def list_devices() -> list[dict]:
    """토큰 해시를 제외한 안전 뷰."""
    out = []
    for x in _data()["devices"]:
        out.append({
            "id": x["id"],
            "name": x.get("name") or x["id"][:8],
            "created": x.get("created"),
            "last_seen": x.get("last_seen"),
            "revoked": bool(x.get("revoked")),
        })
    return out


def add_device(name: str | None = None) -> dict:
    """새 기기 등록. 반환에 token(1회성 원문) 포함 — 저장은 해시만."""
    d = _data()
    did = secrets.token_hex(8)
    tok = "dev-" + secrets.token_urlsafe(24)
    nm = (name or "").strip() or did[:8]
    d["devices"].append({
        "id": did, "name": nm, "token_hash": _hash(tok),
        "created": _now(), "last_seen": None, "revoked": False,
    })
    _save(d)
    return {"id": did, "name": nm, "token": tok}


def verify(token: str | None) -> dict | None:
    """토큰 → 활성 기기(dict) 또는 None. revoked 는 실패."""
    if not token:
        return None
    d = _data()
    h = _hash(token)
    for x in d["devices"]:
        if x.get("token_hash") == h and not x.get("revoked"):
            return {"id": x["id"], "name": x.get("name")}
    return None


def is_active(did: str) -> bool:
    """기기 id 가 아직 유효한가(세션 중 폐기/삭제 반영용)."""
    d = _data()
    for x in d["devices"]:
        if x["id"] == did:
            return not x.get("revoked")
    return False


def touch(did: str) -> None:
    d = _data()
    for x in d["devices"]:
        if x["id"] == did:
            x["last_seen"] = _now()
            _save(d)
            return


def revoke(did: str) -> bool:
    """접속만 차단하고 기록은 남김(비활성화). 목록에 '폐기됨'으로 표시."""
    d = _data()
    changed = False
    for x in d["devices"]:
        if x["id"] == did and not x.get("revoked"):
            x["revoked"] = True
            changed = True
    if changed:
        _save(d)
    return changed


def remove(did: str) -> bool:
    """레지스트리에서 기기를 완전히 삭제(토큰 해시 제거 → 재페어링 필요)."""
    d = _data()
    before = len(d["devices"])
    d["devices"] = [x for x in d["devices"] if x.get("id") != did]
    if len(d["devices"]) != before:
        _save(d)
        return True
    return False


def rotate_token(did: str) -> str | None:
    """기기의 접속 토큰을 새로 발급(이전 토큰 즉시 무효). 새 원문을 1회 반환.

    원문은 저장하지 않으므로 기존 QR 을 '다시 볼' 수는 없다 → 다시 만들어 준다.
    """
    d = _data()
    for x in d["devices"]:
        if x["id"] == did and not x.get("revoked"):
            tok = "dev-" + secrets.token_urlsafe(24)
            x["token_hash"] = _hash(tok)
            x["rotated_at"] = _now()
            _save(d)
            return tok
    return None


def token_version(did: str) -> int:
    """기기 토큰의 회전 세대. rotate_token 이 바뀔 때마다 값이 달라진다.

    커넥터가 인증 시점의 값을 들고 있다가 매 요청 대조한다 → 재발급하면
    '이미 인증된 기존 연결'도 즉시 무효가 된다(삭제와 동일한 실효성).
    """
    for x in _data()["devices"]:
        if x["id"] == did:
            return int(x.get("rotated_at") or 0)
    return -1


def set_client_public_id(did: str, client_public_id: str) -> bool:
    """기기에 CP 발급 client 자격증명 id 를 연결(삭제 시 회수하기 위해)."""
    d = _data()
    for x in d["devices"]:
        if x["id"] == did:
            x["client_public_id"] = client_public_id
            _save(d)
            return True
    return False


def get_client_public_id(did: str) -> str | None:
    for x in _data()["devices"]:
        if x["id"] == did:
            return x.get("client_public_id")
    return None


def rename(did: str, name: str) -> bool:
    d = _data()
    changed = False
    for x in d["devices"]:
        if x["id"] == did:
            x["name"] = (name or "").strip() or x["id"][:8]
            changed = True
    if changed:
        _save(d)
    return changed


def status() -> dict:
    """UI 상태 요약."""
    d = _data()
    active = sum(1 for x in d["devices"] if not x.get("revoked"))
    return {"enforced": True,
            "active": active, "total": len(d["devices"])}
