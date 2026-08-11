"""E2EE - 릴레이가 프레임 내용을 읽을 수 없게 하는 종단 암호화.

설계(경쟁 제품 분석에서 확정):
- 키: 룸당 대칭키 32B (AES-256-GCM). 페어링 QR 의 fragment(#) 로만 전달되어
  릴레이를 포함한 어떤 서버에도 닿지 않는다(fragment 는 브라우저 밖으로 안 나감).
- 평문으로 남는 것: 릴레이 라우팅에 필요한 최소(cid)와 봉투 표식뿐.
  method/params/세션제목/경로/터미널 텍스트 전부 암호문 안.
- 릴레이는 암호 코드 0줄 - cid 만 읽는 기존 동작 그대로(블라인드 유지).
- 규약 고정: "32B 랜덤 = 키 그대로"(파생 단계 없음), 버전 바이트 0x01,
  nonce 는 매번 os.urandom(12). 릴레이가 저장을 안 하므로 재전송/멱등 이슈 없음.

봉투(wire):
    {"v":1, "e":"a1", "cid":..., "p":"<b64url( 0x01 | nonce12 | ct+tag16 )>"}
    cid: 릴레이 라우팅용(평문 필수). p 안에 실제 프레임 JSON 전체.

키 보관(Host): 데이터 폴더 e2ee_room.json - Windows DPAPI(사용자 스코프)로
감싼다. DPAPI 불가 환경(테스트/비Windows)은 평문 폴백(파일 권한에 의존).
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

from session_manager import config

VERSION_BYTE = b"\x01"
ENVELOPE_MARK = "a1"          # AES-256-GCM v1
NONCE_LEN = 12
KEY_LEN = 32


# ---------------------------------------------------------------- base64url

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- DPAPI

def _dpapi(data: bytes, protect: bool) -> bytes | None:
    """Windows DPAPI(사용자 스코프). 실패/비Windows 면 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        inb = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                          ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 룸 키

def _key_file():
    return config.data_dir() / "e2ee_room.json"


def room_key(room: str, create: bool = True) -> bytes | None:
    """이 room 의 E2EE 키. 없으면 생성(create). room 이 바뀌면 새 키.

    키는 한 번 만들면 유지된다 - 바꾸면 페어링된 모든 기기가 복호 불능이
    되므로(재페어링 필요) 절대 임의 재생성하지 않는다.
    """
    if not room:
        return None
    f = _key_file()
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("room") == room:
            raw = unb64u(rec["key"])
            if rec.get("dpapi"):
                raw = _dpapi(raw, protect=False) or b""
            if len(raw) == KEY_LEN:
                return raw
    except Exception:  # noqa: BLE001
        pass
    if not create:
        return None
    key = os.urandom(KEY_LEN)
    blob = _dpapi(key, protect=True)
    rec = {"room": room,
           "key": b64u(blob if blob else key),
           "dpapi": bool(blob)}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec), encoding="utf-8")
    return key


# ---------------------------------------------------------------- seal/open

def seal_payload(key: bytes, obj: Any, _nonce: bytes | None = None) -> str:
    """프레임(dict)을 암호문 문자열로. _nonce 는 골든벡터 테스트 전용."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = _nonce if _nonce is not None else os.urandom(NONCE_LEN)
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    ct = AESGCM(key).encrypt(nonce, pt, None)
    return b64u(VERSION_BYTE + nonce + ct)


def open_payload(key: bytes, p: str) -> Any:
    """암호문 문자열 -> 프레임(dict). 실패는 ValueError."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = unb64u(p)
    if len(raw) < 1 + NONCE_LEN + 16 or raw[0:1] != VERSION_BYTE:
        raise ValueError("bad envelope version")
    nonce, ct = raw[1:1 + NONCE_LEN], raw[1 + NONCE_LEN:]
    try:
        pt = AESGCM(key).decrypt(nonce, ct, None)
    except Exception as e:  # noqa: BLE001
        raise ValueError("decrypt failed") from e
    return json.loads(pt.decode())


# ---------------------------------------------------------------- 봉투

def is_envelope(frame: dict) -> bool:
    return isinstance(frame, dict) and frame.get("e") == ENVELOPE_MARK and "p" in frame


def wrap_frame(key: bytes, frame: dict) -> dict:
    """프레임 전체를 봉투로. cid 는 릴레이 라우팅용으로 평문에 남긴다."""
    env: dict = {"v": 1, "e": ENVELOPE_MARK}
    if frame.get("cid") is not None:
        env["cid"] = frame["cid"]
    env["p"] = seal_payload(key, frame)
    return env


def unwrap_frame(key: bytes, env: dict) -> dict:
    """봉투 -> 프레임. 릴레이가 봉투 겉에 주입한 cid 를 프레임에 반영한다."""
    frame = open_payload(key, env["p"])
    if not isinstance(frame, dict):
        raise ValueError("payload is not a frame")
    if env.get("cid") is not None:
        frame["cid"] = env["cid"]
    return frame
