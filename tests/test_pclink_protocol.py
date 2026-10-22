"""pclink 전송·암호 층(protocol) — 봉투 v0·봉인·검사 순서·중복 장부·결과 보관·짝짓기 암호.

정본 docs/designs/pclink-protocol-v0.md. 고정 시험 벡터는 다른 언어 구현이 같은 바이트를 내는지 맞춰 보는 기준이다.
"""
from __future__ import annotations

import base64
import json

import pytest

from session_manager.pclink import protocol as P

A = "a1b2c3d4e5f60718"
B = "0f1e2d3c4b5a6978"
SA = "5d1e0000-0000-4000-8000-00000000000a"
SB = "94010000-0000-4000-8000-00000000000b"
KEY = bytes(range(32))
NOW = 1792650000


def _ask(now=NOW, **kw):
    return P.header("ask", P.addr(A, SA), P.addr(B, SB), now=now, **kw)


def _err(fn, *a, **kw):
    with pytest.raises(P.ProtocolError) as ei:
        fn(*a, **kw)
    return ei.value.code


# ---------------------------------------------------------------- 고정 시험 벡터

GOLD_H = {"v": 0, "id": "3b1f0b0e-0000-4000-8000-000000000001", "reply_to": None, "kind": "ask",
          "from": {"host": A, "session": SA}, "to": {"host": B, "session": SB}, "expires_at": NOW + 120}
GOLD_BODY = {"prompt": "이 세션에서 정한 배포 순서를 한 줄로"}


def test_canonical_bytes_are_fixed():
    assert P.canonical({"b": 1, "a": "가"}) == '{"a":"가","b":1}'.encode("utf-8")
    assert P.canonical(GOLD_H).startswith(b'{"expires_at":1792650120,"from":{"host":"a1b2c3d4e5f60718"')


def test_golden_vector_seal():
    env = P.seal(KEY, GOLD_H, GOLD_BODY, _nonce=bytes(12))
    assert env["n"] == "AAAAAAAAAAAAAAAA"
    # 같은 키·nonce·머리·본문이면 어느 구현이든 이 암호문이 나와야 한다
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aad = b"clewpath-pclink/0\n" + json.dumps(GOLD_H, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8")
    pt = json.dumps(GOLD_BODY, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    want = base64.urlsafe_b64encode(AESGCM(KEY).encrypt(bytes(12), pt, aad)).rstrip(b"=").decode()
    assert env["c"] == want
    assert env["c"] == GOLD_C


GOLD_C = 'dZ7FrNpB88kqkovZhZixdVb7utawbfe_bBovoylfyLvbw8ZXmkJViZ_jnX1dIu3SQqL9xpTk2oW8ErvN6_xsmbg3McvM4Xb22G_RzE26-ys'


# ---------------------------------------------------------------- 봉인·개봉

def test_roundtrip():
    h = _ask()
    env = P.seal(KEY, h, {"prompt": "안녕"})
    h2, body = P.open_env(KEY, env, my_host=B, peer_host=A, now=NOW)
    assert h2 == h and body == {"prompt": "안녕"}


def test_nonce_is_fresh_each_time():
    h = _ask()
    assert P.seal(KEY, h, {"x": 1})["n"] != P.seal(KEY, h, {"x": 1})["n"]


@pytest.mark.parametrize("field,value", [
    ("kind", "poll"), ("expires_at", NOW + 119), ("reply_to", "3b1f0b0e-0000-4000-8000-000000000009"),
    ("id", "3b1f0b0e-0000-4000-8000-000000000002"),
])
def test_header_is_authenticated(field, value):
    """릴레이가 머리를 바꾸면 개봉 실패 — 머리는 AEAD 인증 데이터다."""
    env = P.seal(KEY, _ask(), {"prompt": "x"})
    env["h"] = {**env["h"], field: value}
    assert _err(P.open_env, KEY, env, my_host=B, peer_host=A, now=NOW) == "bad_seal"


def test_session_rewrite_is_detected():
    env = P.seal(KEY, _ask(), {"prompt": "x"})
    env["h"] = {**env["h"], "to": {"host": B, "session": "94010000-0000-4000-8000-0000000000ff"}}
    assert _err(P.open_env, KEY, env, my_host=B, peer_host=A, now=NOW) == "bad_seal"


def test_ciphertext_or_key_tamper():
    env = P.seal(KEY, _ask(), {"prompt": "x"})
    bad = dict(env, c=env["c"][:-2] + ("AA" if env["c"][-2:] != "AA" else "BB"))
    assert _err(P.open_env, KEY, bad, my_host=B, peer_host=A, now=NOW) == "bad_seal"
    assert _err(P.open_env, bytes(32), env, my_host=B, peer_host=A, now=NOW) == "bad_seal"
    assert _err(P.open_env, KEY, dict(env, n="!!"), my_host=B, peer_host=A, now=NOW) == "bad_seal"


def test_direction_is_bound():
    """한 짝 키를 두 방향이 쓰므로, 내가 보낸 글이 되돌아와도(반사) 받지 않는다."""
    env = P.seal(KEY, _ask(), {"prompt": "x"})                   # A → B
    assert _err(P.open_env, KEY, env, my_host=A, peer_host=B, now=NOW) == "not_paired"
    assert _err(P.open_env, KEY, env, my_host=B, peer_host="1111111111111111", now=NOW) == "not_paired"


def test_checks_order_version_kind_shape():
    env = P.seal(KEY, _ask(), {"prompt": "x"})
    assert _err(P.open_env, KEY, dict(env, h={**env["h"], "v": 1}), my_host=B, peer_host=A, now=NOW) == "bad_version"
    assert _err(P.open_env, KEY, dict(env, h={**env["h"], "kind": "exec"}), my_host=B, peer_host=A, now=NOW) == "bad_kind"
    assert _err(P.open_env, KEY, dict(env, h={**env["h"], "x": 1}), my_host=B, peer_host=A, now=NOW) == "bad_header"
    assert _err(P.open_env, KEY, dict(env, extra=1), my_host=B, peer_host=A, now=NOW) == "bad_header"
    assert _err(P.open_env, KEY, "nope", my_host=B, peer_host=A, now=NOW) == "bad_header"


def test_expiry_window_with_skew():
    env = P.seal(KEY, _ask(), {"prompt": "x"})                   # expires_at = NOW+120
    P.open_env(KEY, env, my_host=B, peer_host=A, now=NOW + 120 + 59)          # 시계 차 60초 안
    assert _err(P.open_env, KEY, env, my_host=B, peer_host=A, now=NOW + 120 + 61) == "expired"
    far = P.seal(KEY, _ask(ttl=P.MAX_TTL_S + P.SKEW_S + 5), {"prompt": "x"})
    assert _err(P.open_env, KEY, far, my_host=B, peer_host=A, now=NOW) == "expired"


def test_session_null_only_for_host_kinds():
    h = P.header("hello", P.addr(A), P.addr(B), now=NOW)
    P.open_env(KEY, P.seal(KEY, h, {"pv": 0}), my_host=B, peer_host=A, now=NOW)
    with pytest.raises(P.ProtocolError):
        P.seal(KEY, P.header("ask", P.addr(A, SA), P.addr(B), now=NOW), {"prompt": "x"})


def test_bad_host_id_and_unknown_kind_on_build():
    assert _err(P.header, "exec", P.addr(A), P.addr(B)) == "bad_kind"
    assert _err(P.seal, KEY, P.header("hello", P.addr("A-B"), P.addr(B), now=NOW), {}) == "bad_header"
    assert _err(P.seal, b"short", _ask(), {}) == "bad_key"


def test_peer_of():
    env = P.seal(KEY, _ask(), {"prompt": "x"})
    assert P.peer_of(env) == A
    assert P.peer_of({"h": {"from": {"host": "../x"}}}) is None
    assert P.peer_of(None) is None


# ---------------------------------------------------------------- 중복 id 장부

def test_ledger_first_then_duplicate_and_survives_restart(tmp_path):
    p = tmp_path / "pclink" / "seen.sqlite"
    L = P.Ledger(p)
    assert L.seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW) is True
    assert L.seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW) is False
    assert L.seen_or_record(B, "id-0000001", "ask", NOW + 120, now=NOW) is True    # 짝별로 따로
    assert P.Ledger(p).seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW + 5) is False   # 재시작 뒤에도


def test_ledger_prunes_after_expiry(tmp_path):
    L = P.Ledger(tmp_path / "seen.sqlite")
    L.seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW)
    # 만료+시계 차가 지난 뒤에는 지워진다(그때는 만료 검사가 먼저 막는다)
    assert L.seen_or_record(A, "id-0000002", "ask", NOW + 9000, now=NOW + 1000) is True
    assert L.seen_or_record(A, "id-0000001", "ask", NOW + 9000, now=NOW + 1000) is True


def test_ledger_forget_peer(tmp_path):
    L = P.Ledger(tmp_path / "seen.sqlite")
    L.seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW)
    L.forget_peer(A)
    assert L.seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW) is True


def test_ledger_failure_raises(tmp_path):
    d = tmp_path / "is_a_dir"
    d.mkdir()
    with pytest.raises(P.LedgerError):
        P.Ledger(d).seen_or_record(A, "id-0000001", "ask", NOW + 120, now=NOW)


# ---------------------------------------------------------------- 결과 보관

def test_result_store_lifecycle():
    R = P.ResultStore()
    assert R.start("ask-0000001", A, now=NOW) is True
    assert R.start("ask-0000001", A, now=NOW) is False               # 다시 실행하지 않는다
    assert R.get("ask-0000001", A, now=NOW)["state"] == "queued"
    R.running("ask-0000001")
    assert R.get("ask-0000001", A, now=NOW)["state"] == "running"
    R.finish("ask-0000001", "answer", {"answer": "청포도"}, now=NOW + 10)
    got = R.get("ask-0000001", A, now=NOW + 10)
    assert got == {"state": "done", "kind": "answer", "body": {"answer": "청포도"}}
    assert R.get("ask-0000001", B, now=NOW + 10) is None             # 다른 짝에는 안 보인다
    assert R.get("ask-0000001", A, now=NOW + 10 + R.KEEP_S + 1) is None  # 10분 뒤 지움


def test_result_store_cap_keeps_running(monkeypatch):
    R = P.ResultStore()
    monkeypatch.setattr(R, "MAX", 3)
    R.start("run-0000001", A, now=NOW)                                # 진행 중
    for i in range(2):
        R.start(f"done-000000{i}", A, now=NOW)
        R.finish(f"done-000000{i}", "answer", {}, now=NOW + i)
    R.start("new-0000001", A, now=NOW + 5)
    assert R.get("run-0000001", A, now=NOW + 5) is not None
    assert R.get("done-0000000", A, now=NOW + 5) is None              # 가장 오래 끝난 것부터


def test_result_store_forget_peer():
    R = P.ResultStore()
    R.start("ask-0000001", A, now=NOW)
    R.forget_peer(A)
    assert R.get("ask-0000001", A, now=NOW) is None


# ---------------------------------------------------------------- 짝짓기 암호

def _invite(**kw):
    d = {"v": 0, "inv": "00112233aabbccdd", "k": P.b64u(bytes(32)), "host": B, "name": "노트북",
         "relay": "wss://clewpath.example/relay", "cp": "https://clewpath.example", "room": "room1",
         "cred": {"id": "cpub-1", "secret": "s"}, "exp": NOW + P.INVITE_TTL_S}
    d.update(kw)
    return d


def test_invite_roundtrip_and_rejects():
    s = P.encode_invite(_invite())
    assert s.startswith("clewpath-pair:0:")
    assert P.decode_invite(s, now=NOW)["name"] == "노트북"
    assert _err(P.decode_invite, "xx" + s, now=NOW) == "bad_invite"
    assert _err(P.decode_invite, s, now=NOW + P.INVITE_TTL_S + 1) == "invite_expired"
    assert _err(P.decode_invite, P.encode_invite({**_invite(), "extra": 1}), now=NOW) == "bad_invite"
    assert _err(P.decode_invite, P.encode_invite(_invite(k=P.b64u(b"short"))), now=NOW) == "bad_invite"
    assert _err(P.decode_invite, P.encode_invite(_invite(relay="http://x")), now=NOW) == "bad_invite"
    assert _err(P.decode_invite, P.encode_invite(_invite(v=1)), now=NOW) == "bad_invite"


def test_pairing_both_sides_derive_same_key():
    k = bytes(range(32, 64))
    skA, epkA = P.x25519_keypair()
    skB, epkB = P.x25519_keypair()
    kA = P.derive_pair_key(skA, epkB, k, B, A, epkA, epkB)
    kB = P.derive_pair_key(skB, epkA, k, B, A, epkA, epkB)
    assert kA == kB and len(kA) == 32
    # 다른 초대 비밀·다른 host 묶음이면 다른 키
    assert P.derive_pair_key(skA, epkB, bytes(32), B, A, epkA, epkB) != kA
    assert P.derive_pair_key(skA, epkB, k, A, B, epkA, epkB) != kA


def test_pairing_golden_vector():
    k = bytes(range(32, 64))
    skA, epkA = P.x25519_keypair(_private=bytes([1] * 32))
    skB, epkB = P.x25519_keypair(_private=bytes([2] * 32))
    assert P.b64u(P.derive_pair_key(skA, epkB, k, B, A, epkA, epkB)) == GOLD_PAIR_KEY


GOLD_PAIR_KEY = 'aG_82AluiqHmhWt99xX9wykb_d40wmFLhM7X0RgGp-Q'


def test_pair_mac():
    kmac = P.invite_mac_key(bytes(32))
    m = P.pair_mac(kmac, "init", "inv", B, A, "epk", "1")
    assert P.pair_mac_ok(kmac, m, "init", "inv", B, A, "epk", "1")
    assert not P.pair_mac_ok(kmac, m, "init", "inv", B, A, "epk", "2")
    assert not P.pair_mac_ok(P.invite_mac_key(bytes([1] * 32)), m, "init", "inv", B, A, "epk", "1")
    assert not P.pair_mac_ok(kmac, None, "init")


def test_low_order_point_rejected():
    sk, _ = P.x25519_keypair()
    assert _err(P.derive_pair_key, sk, P.b64u(bytes(32)), bytes(32), B, A, "a", "b") == "bad_pair"
    assert _err(P.derive_pair_key, sk, "!!", bytes(32), B, A, "a", "b") == "bad_pair"
