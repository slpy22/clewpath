"""소유자 2차 인증(TOTP, RFC 6238). 특권 동작(in-place 재개)에 요구.

- 비밀은 로컬 파일(~/.claude/session_manager/owner_2fa.json)에만 저장.
- 외부(릴레이) 경유 in-place 재개 시 인증앱 6자리 코드 검증.
- 외부 라이브러리 없이 HMAC-SHA1 로 구현(pyotp 불필요).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time

from session_manager import config


def _file():
    return config.data_dir() / "owner_2fa.json"


def _data() -> dict:
    f = _file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, f)


def secret() -> str | None:
    return _data().get("secret")


def has_secret() -> bool:
    return bool(secret())


def enabled() -> bool:
    # 하위호환 별칭: '비밀이 있는가'(OTP 검증/grant 발급 가능 여부)
    return has_secret()


def required() -> bool:
    """외부 특권 동작에 2FA 를 '강제'하는가. (비밀 존재 + enforced 플래그)"""
    d = _data()
    return bool(d.get("secret")) and d.get("enforced", True)


def set_enforced(on: bool) -> bool:
    """2FA 강제 on/off. on 인데 비밀이 없으면 False(먼저 provision 필요)."""
    d = _data()
    if on and not d.get("secret"):
        return False
    d["enforced"] = bool(on)
    _save(d)
    return True


def provision(label: str = "owner") -> dict:
    """새 TOTP 비밀을 생성·저장하고(강제 ON) otpauth URI 를 반환한다(기존 비밀 대체)."""
    s = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    _save({"secret": s, "created": int(time.time()), "enforced": True})
    uri = (f"otpauth://totp/SessionManager:{label}"
           f"?secret={s}&issuer=SessionManager&algorithm=SHA1&digits=6&period=30")
    return {"secret": s, "otpauth": uri}


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad, casefold=True)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify(code: str | None, window: int = 1, period: int = 30) -> bool:
    """현재 시각 기준 ±window 스텝 내에서 6자리 코드를 검증한다."""
    s = secret()
    if not s or not code:
        return False
    code = str(code).strip()
    if not (code.isdigit() and len(code) == 6):
        return False
    now = int(time.time()) // period
    for w in range(-window, window + 1):
        if _hotp(s, now + w) == code:
            return True
    return False


# ---- grace 토큰: OTP 1회 성공 후 일정 시간 코드 없이 통과(기기 신뢰) ----
def grace_ttl() -> int:
    """grace 유효기간(초). 기본 8시간. SM_2FA_GRACE_SEC 로 조정."""
    try:
        return max(60, int(os.environ.get("SM_2FA_GRACE_SEC", "28800")))
    except Exception:  # noqa: BLE001
        return 28800


def _grace_key() -> bytes:
    # TOTP 비밀로 서명 → 재설정(provision) 시 기존 grace 전부 무효화됨.
    return (secret() or "").encode()


def issue_grace(ttl: int | None = None) -> dict | None:
    if not secret():
        return None
    exp = int(time.time()) + (ttl or grace_ttl())
    nonce = secrets.token_hex(8)
    sig = hmac.new(_grace_key(), f"{exp}.{nonce}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    tok = base64.urlsafe_b64encode(
        f"{exp}.{nonce}.{sig}".encode()).decode().rstrip("=")
    return {"grace": tok, "exp": exp}


def verify_grace(tok: str | None) -> bool:
    if not secret() or not tok:
        return False
    try:
        pad = "=" * ((4 - len(tok) % 4) % 4)
        raw = base64.urlsafe_b64decode(tok + pad).decode()
        exp_s, nonce, sig = raw.split(".")
        exp = int(exp_s)
        if exp < int(time.time()):
            return False
        good = hmac.new(_grace_key(), f"{exp}.{nonce}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(good, sig)
    except Exception:  # noqa: BLE001
        return False


# ---- RFC 6238 표준 벡터 자체검증(모듈 임포트 시 1회) ----
def _selftest() -> bool:
    # secret "12345678901234567890" (ASCII), T=59, step=30 → counter=1 → 6digit 287082
    b32 = base64.b32encode(b"12345678901234567890").decode()
    return _hotp(b32, 1) == "287082" and _hotp(b32, 2) == "359152"


assert _selftest(), "TOTP self-test failed (RFC 6238 vector mismatch)"
