"""기기 레지스트리 — 릴레이(외부) 클라이언트의 '기기별' 인증/폐기.

설계:
- 각 기기는 1회 발급 토큰(dev-...)으로 식별. 저장은 SHA-256 해시만(원문 보관 안 함).
- 기기 인증은 **항상 강제**(끌 수 없음): 커넥터가 인증되지 않은 클라이언트의 모든 메서드(ping 제외)를 거부.
  등록 기기가 0개면 아무도 통과 못 하므로 사실상 '외부 전면 차단'(별도 킬 스위치 없음).
- 파일: ~/.claude/session_manager/devices.json  (로컬 006만 기록, 커넥터는 같은 프로세스에서 읽음)

동시성(0.10.0, eng E-D3):
- 쓰기는 전부 `_mutate(fn)` 을 통한다 — 모듈 RLock 아래서 읽기→수정→쓰기를 원자화한다.
  커넥터(asyncio 스레드)의 touch 와 로컬 화면(FastAPI 스레드풀)의 삭제/이름 변경, 0.11.0 의
  CP 동기화 스레드(revoked 기록)가 겹쳐도 앞의 변경을 뒤의 쓰기가 지우지 않는다.
- 네트워크 호출은 잠금 밖에서 한다(E-D14). 잠금 안에서는 파일 I/O 만.

보안 경계:
- 이 레지스트리는 '누가(기기)'를 다루는 애플리케이션 계층 인증이다.
- 릴레이 접속용 공유 client 토큰은 '릴레이에 닿을 수 있는가'만 담당(전송 계층).
- 특권 동작의 2차 확인(OTP/grace)은 owner2fa 가 별도로 담당(계층 분리).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Callable

from session_manager import config

_LOCK = threading.RLock()
NAME_MAX = 40
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _file():
    return config.data_dir() / "devices.json"


def _blank() -> dict:
    return {"enforced": False, "devices": [], "meta": {}}


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
    d.setdefault("meta", {})
    return d


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def _mutate(fn: Callable[[dict], object]):
    """잠금 아래서 읽기→fn(d)→쓰기. fn 이 반환하는 값을 그대로 돌려준다.

    fn 이 `None` 을 반환하면 '변경 없음' 으로 보고 저장하지 않는다(불필요한 쓰기 방지).
    저장이 필요하면 무엇이든 None 이 아닌 값을(예: True, 새 토큰) 반환할 것.
    """
    with _LOCK:
        d = _data()
        r = fn(d)
        if r is not None:
            _save(d)
        return r


def _read(fn: Callable[[dict], object]):
    with _LOCK:
        return fn(_data())


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def clean_name(raw: str | None, fallback: str = "") -> str:
    """이름 정규화 한 곳(eng E-D9): 제어문자 제거·연속 공백 1칸·양끝 공백 제거·NAME_MAX 자.

    add/rename(사용자 입력)·폰이 auth 때 보고하는 이름·appconfig.machine_name() 이 모두 이걸 탄다 —
    PC 목록과 폰 목록에 같은 기기가 항상 같은 이름으로 보인다. 빈 결과는 fallback.
    """
    s = re.sub(r"[\t\r\n\f\v 　]", " ", str(raw or ""))   # 탭·줄바꿈·전각 공백 → 한 칸(제어문자 제거보다 먼저)
    s = _CTRL.sub("", s)                                            # 나머지 제어문자(\x1f 등)는 흔적 없이 제거
    s = re.sub(r" {2,}", " ", s).strip()
    if len(s) > NAME_MAX:
        s = s[:NAME_MAX].rstrip()
    return s or fallback


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
    """토큰 해시를 제외한 안전 뷰. client_scoped=False 는 공유토큰(구형) 페어링 — 화면에 🔗 배지."""
    def _f(d):
        out = []
        for x in d["devices"]:
            out.append({
                "id": x["id"],
                "name": x.get("name") or x["id"][:8],
                "created": x.get("created"),
                "last_seen": x.get("last_seen"),
                "revoked": bool(x.get("revoked")),
                "revoked_by": x.get("revoked_by"),                     # 'phone'(self-revoke 📵) | 'pc' | None
                "client_scoped": bool(x.get("client_public_id")),
                "cp_pending": bool(x.get("cp_pending")),               # 서버 폐기 확인 대기(⏳)
                "delete_pending": bool(x.get("delete_pending")),       # 폐기 확인되면 행 제거
            })
        return out
    return _read(_f)


def add_device(name: str | None = None) -> dict:
    """새 기기 등록. 반환에 token(1회성 원문) 포함 — 저장은 해시만.

    이름이 비어 있으면 id 앞 8자를 임시 이름으로 두고, 폰이 auth 때 보고한 이름으로 채워진다
    (set_name_if_empty). 임시 이름 여부는 name_auto 로 기억한다.
    """
    did = secrets.token_hex(8)
    tok = "dev-" + secrets.token_urlsafe(24)
    nm = clean_name(name)

    def _f(d):
        d["devices"].append({
            "id": did, "name": nm or did[:8], "name_auto": not nm, "token_hash": _hash(tok),
            "created": _now(), "last_seen": None, "revoked": False,
        })
        return True
    _mutate(_f)
    return {"id": did, "name": nm or did[:8], "token": tok}


def verify(token: str | None) -> dict | None:
    """토큰 → 활성 기기(dict) 또는 None. revoked 는 실패."""
    if not token:
        return None
    h = _hash(token)

    def _f(d):
        for x in d["devices"]:
            if x.get("token_hash") == h and not x.get("revoked"):
                return {"id": x["id"], "name": x.get("name")}
        return None
    return _read(_f)


def is_active(did: str) -> bool:
    """기기 id 가 아직 유효한가(세션 중 폐기/삭제 반영용)."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                return not x.get("revoked")
        return False
    return _read(_f)


def touch(did: str) -> None:
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                x["last_seen"] = _now()
                return True
        return None
    _mutate(_f)


def set_name_if_empty(did: str, name: str | None) -> str | None:
    """폰이 auth 때 보고한 이름으로 **임시 이름만** 채운다(B-1). 사용자가 붙인 이름은 건드리지 않는다.

    반환: 채워진 이름, 변경 없으면 None.
    """
    nm = clean_name(name)
    if not nm:
        return None

    def _f(d):
        for x in d["devices"]:
            if x["id"] == did and x.get("name_auto", not x.get("name")):
                x["name"] = nm
                x["name_auto"] = False
                return nm
        return None
    return _mutate(_f)


def revoke(did: str) -> bool:
    """접속만 차단하고 기록은 남김(비활성화). 목록에 '폐기됨'으로 표시."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did and not x.get("revoked"):
                x["revoked"] = True
                return True
        return None
    return bool(_mutate(_f))


def remove(did: str) -> bool:
    """레지스트리에서 기기를 완전히 삭제(토큰 해시 제거 → 재페어링 필요)."""
    def _f(d):
        before = len(d["devices"])
        d["devices"] = [x for x in d["devices"] if x.get("id") != did]
        return True if len(d["devices"]) != before else None
    return bool(_mutate(_f))


def rotate_token(did: str) -> str | None:
    """기기의 접속 토큰을 새로 발급(이전 토큰 즉시 무효). 새 원문을 1회 반환.

    원문은 저장하지 않으므로 기존 QR 을 '다시 볼' 수는 없다 → 다시 만들어 준다.
    """
    tok = "dev-" + secrets.token_urlsafe(24)

    def _f(d):
        for x in d["devices"]:
            if x["id"] == did and not x.get("revoked"):
                x["token_hash"] = _hash(tok)
                x["rotated_at"] = _now()
                return tok
        return None
    return _mutate(_f)


def token_version(did: str) -> int:
    """기기 토큰의 회전 세대. rotate_token 이 바뀔 때마다 값이 달라진다.

    커넥터가 인증 시점의 값을 들고 있다가 매 요청 대조한다 → 재발급하면
    '이미 인증된 기존 연결'도 즉시 무효가 된다(삭제와 동일한 실효성).
    """
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                return int(x.get("rotated_at") or 0)
        return -1
    return _read(_f)


def set_client_public_id(did: str, client_public_id: str | None) -> bool:
    """기기에 CP 발급 client 자격증명 id 를 연결(삭제 시 회수하기 위해). None 이면 연결 해제."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                if client_public_id:
                    x["client_public_id"] = client_public_id
                else:
                    x.pop("client_public_id", None)
                return True
        return None
    return bool(_mutate(_f))


def get_client_public_id(did: str) -> str | None:
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                return x.get("client_public_id")
        return None
    return _read(_f)


def rename(did: str, name: str) -> bool:
    nm = clean_name(name)

    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                x["name"] = nm or x["id"][:8]
                x["name_auto"] = not nm
                return True
        return None
    return bool(_mutate(_f))


def status() -> dict:
    """UI 상태 요약(+ 🩺 진단: 마지막 CP 동기화 시각)."""
    def _f(d):
        active = sum(1 for x in d["devices"] if not x.get("revoked"))
        phone = sum(1 for x in d["devices"] if x.get("revoked") and x.get("revoked_by") == "phone")
        return {"enforced": True, "active": active, "total": len(d["devices"]),
                "phone_revoked": phone, "cp_synced": (d.get("meta") or {}).get("cp_synced")}
    return _read(_f)


# ---- 페어링 감사 로그(B-10, E-D10): 첫 auth·삭제·재발급·self-revoke 수신·revoke 만. 재접속 auth 는 기록하지 않는다 ----
def _audit_file():
    return config.data_dir() / "pairing-audit.jsonl"


def audit(event: str, did: str | None = None, **fields) -> None:
    from session_manager import jsonl_log
    jsonl_log.append(_audit_file(), {"event": event, "device": did, **fields})


def audit_tail(n: int = 50) -> list[dict]:
    from session_manager import jsonl_log
    return jsonl_log.tail(_audit_file(), n)


def first_seen(did: str) -> bool:
    """이 기기가 아직 한 번도 접속한 적 없는가(첫 auth 판정 — E-3 ① 웹푸시·감사 1회)."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                return not x.get("last_seen")
        return False
    return _read(_f)


# ---- 0.11.0 CP 동기화(devsync)·self-revoke 지원 (eng E-D6/E-D7/E-D15) ----
def mark_revoked(did: str, by: str = "pc") -> bool:
    """폐기 표시 + 누가 했는지(by='phone' 이면 📵 폰에서 해제함). 이미 폐기면 False."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did and not x.get("revoked"):
                x["revoked"] = True
                x["revoked_by"] = by
                x["revoked_at"] = _now()
                return True
        return None
    return bool(_mutate(_f))


def add_cp_pending(did: str, item: str) -> bool:
    """CP 폐기 결과가 '불명'(실패/타임아웃)인 항목을 행에 남긴다 — devsync 가 status 로 확인 후 재폐기·정리."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                lst = list(x.get("cp_pending") or [])
                if item in lst:
                    return None
                lst.append(item)
                x["cp_pending"] = lst
                return True
        return None
    return bool(_mutate(_f))


def pop_cp_pending(did: str, item: str) -> bool:
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did and item in (x.get("cp_pending") or []):
                x["cp_pending"] = [i for i in x["cp_pending"] if i != item]
                if not x["cp_pending"]:
                    x.pop("cp_pending", None)
                return True
        return None
    return bool(_mutate(_f))


def set_pending_cpub(did: str, cpub: str | None) -> bool:
    """재발급 중 '발급됐지만 아직 확정 안 된' 새 cpub. 확정(set_client_public_id) 전에 Host 가 죽으면
    다음 devsync 가 이 값을 폐기 대상으로 삼는다(미반영 신규 cpub 정리)."""
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                if cpub:
                    x["pending_cpub"] = cpub
                else:
                    x.pop("pending_cpub", None)
                return True
        return None
    return bool(_mutate(_f))


def set_delete_pending(did: str, flag: bool = True) -> bool:
    def _f(d):
        for x in d["devices"]:
            if x["id"] == did:
                if flag:
                    x["delete_pending"] = True
                else:
                    x.pop("delete_pending", None)
                return True
        return None
    return bool(_mutate(_f))


def set_cp_synced(ts: int | None = None) -> None:
    def _f(d):
        d.setdefault("meta", {})["cp_synced"] = int(ts or _now())
        return True
    _mutate(_f)


def sync_snapshot() -> list[dict]:
    """devsync 가 잠금 밖에서 CP 에 물을 자료(행의 사본): id·cpub·pending_cpub·cp_pending·revoked·delete_pending."""
    def _f(d):
        return [{"id": x["id"], "cpub": x.get("client_public_id"), "pending_cpub": x.get("pending_cpub"),
                 "cp_pending": list(x.get("cp_pending") or []), "revoked": bool(x.get("revoked")),
                 "delete_pending": bool(x.get("delete_pending")), "name": x.get("name")}
                for x in d["devices"]]
    return _read(_f)


def apply_cp_status(statuses: dict) -> dict:
    """CP 응답을 (device_id, cpub) 재대조로 반영(잠금 안, 파일 I/O 만). 응답에 없는 cpub 은 건드리지 않는다.

    - 활성 행의 cpub 이 CP 에서 revoked → 행 revoked(by=phone) + 📵 (폰 self-revoke 수신)
    - delete_pending 행의 cpub 이 revoked → 행 제거(폐기 확인)
    반환: {"phone_revoked": [id...], "removed": [id...]}
    """
    out = {"phone_revoked": [], "removed": []}

    def _f(d):
        keep = []
        changed = False
        for x in d["devices"]:
            st = statuses.get(x.get("client_public_id") or "")
            if st and st.get("status") == "revoked":
                if x.get("delete_pending"):
                    out["removed"].append(x["id"]); changed = True
                    continue
                if not x.get("revoked"):
                    x["revoked"] = True; x["revoked_by"] = "phone"; x["revoked_at"] = int(st.get("revoked_at") or _now())
                    out["phone_revoked"].append(x["id"]); changed = True
            keep.append(x)
        d["devices"] = keep
        return True if changed else None
    _mutate(_f)
    return out
