"""E2EE 회귀 - 키 영속·봉투 형식·변조 거부·골든 벡터.

골든 벡터가 곧 JS(WebCrypto)와의 계약이다: 여기 sealed 바이트가 바뀌면
폰의 복호가 전부 깨진다. 벡터 검증 실패 = 프로토콜 호환성 파손 신호.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from session_manager import e2ee  # noqa: E402

VEC = json.loads((Path(__file__).parent / "vectors" / "e2ee_vectors.json")
                 .read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))


# ---- 골든 벡터 (JS 와의 바이트 계약) ----

def test_golden_vectors_seal():
    key = e2ee.unb64u(VEC["key_b64u"])
    for c in VEC["cases"]:
        sealed = e2ee.seal_payload(key, c["frame"], _nonce=e2ee.unb64u(c["nonce_b64u"]))
        assert sealed == c["sealed"], f"{c['name']}: 바이트 계약 파손 - JS 복호가 깨진다"


def test_golden_vectors_open():
    key = e2ee.unb64u(VEC["key_b64u"])
    for c in VEC["cases"]:
        assert e2ee.open_payload(key, c["sealed"]) == c["frame"]


# ---- seal/open ----

def test_roundtrip_random_nonce():
    key = bytes(32)
    f = {"v": 1, "type": "req", "method": "x", "params": {"한글": "값"}}
    s1, s2 = e2ee.seal_payload(key, f), e2ee.seal_payload(key, f)
    assert s1 != s2                       # nonce 가 매번 달라야 한다
    assert e2ee.open_payload(key, s1) == f == e2ee.open_payload(key, s2)


def test_tamper_rejected():
    key = bytes(32)
    sealed = e2ee.seal_payload(key, {"a": 1})
    raw = bytearray(e2ee.unb64u(sealed)); raw[-1] ^= 0xFF
    with pytest.raises(ValueError):
        e2ee.open_payload(key, e2ee.b64u(bytes(raw)))


def test_wrong_key_rejected():
    sealed = e2ee.seal_payload(bytes(32), {"a": 1})
    with pytest.raises(ValueError):
        e2ee.open_payload(b"\x01" * 32, sealed)


def test_bad_version_rejected():
    key = bytes(32)
    raw = bytearray(e2ee.unb64u(e2ee.seal_payload(key, {}))); raw[0] = 0x02
    with pytest.raises(ValueError):
        e2ee.open_payload(key, e2ee.b64u(bytes(raw)))


# ---- 봉투 (cid 평문 유지) ----

def test_wrap_unwrap_keeps_cid_plaintext():
    key = bytes(32)
    env = e2ee.wrap_frame(key, {"v": 1, "type": "res", "id": "r1", "cid": "c9", "data": {"x": 1}})
    assert env["cid"] == "c9" and env["e"] == "a1"           # 릴레이가 라우팅 가능
    assert "data" not in env and "id" not in env              # 내용은 전부 암호문
    assert e2ee.is_envelope(env)
    out = e2ee.unwrap_frame(key, env)
    assert out["data"] == {"x": 1} and out["cid"] == "c9"


def test_unwrap_applies_relay_injected_cid():
    """클라 봉투엔 cid 가 없고 릴레이가 겉에 주입한다 - 프레임에 반영돼야."""
    key = bytes(32)
    env = e2ee.wrap_frame(key, {"v": 1, "type": "req", "id": "q1", "method": "ping"})
    assert "cid" not in env
    env["cid"] = "c7"                     # 릴레이 주입 시뮬레이션
    assert e2ee.unwrap_frame(key, env)["cid"] == "c7"


# ---- 룸 키 영속 ----

def test_room_key_stable_and_room_scoped(monkeypatch):
    monkeypatch.setattr(e2ee, "_dpapi", lambda d, protect: None)   # 결정적(폴백 경로)
    k1 = e2ee.room_key("rm_a")
    assert k1 is not None and len(k1) == 32
    assert e2ee.room_key("rm_a") == k1     # 재호출 = 같은 키 (재생성 금지)
    k2 = e2ee.room_key("rm_b")             # 다른 room = 새 키
    assert k2 != k1
    assert e2ee.room_key("rm_b", create=False) == k2
    assert e2ee.room_key("", create=False) is None


def test_room_key_dpapi_roundtrip():
    """이 PC 의 실제 DPAPI 경로(Windows). 비Windows 폴백이면 skip."""
    k = e2ee.room_key("rm_dp")
    rec = json.loads(e2ee._key_file().read_text(encoding="utf-8"))
    if not rec.get("dpapi"):
        pytest.skip("DPAPI 미사용 환경")
    assert e2ee.unb64u(rec["key"]) != k    # 디스크엔 평문 키가 없다
    assert e2ee.room_key("rm_dp", create=False) == k
