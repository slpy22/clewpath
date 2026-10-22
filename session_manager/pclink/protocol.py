"""pclink 전송·암호 층 — 봉투 v0, 봉인·개봉, 만료, 중복 id 장부, 결과 보관, 짝짓기 암호.

정본: docs/designs/pclink-protocol-v0.md. 이 모듈은 봉투 본문의 뜻을 모른다(정책은 policy, 실행은 routes).

봉투(wire):
    {"h": {v, id, reply_to, kind, from:{host,session}, to:{host,session}, expires_at},
     "n": b64url(nonce 12B), "c": b64url(암호문+태그)}
머리 h 는 평문(받는 쪽이 from.host 로 짝을 찾는다)이지만 AEAD 인증 데이터로 묶는다 — 릴레이가 주소·종류·만료를 바꾸면
개봉이 실패한다. 한 짝 키를 두 방향이 같이 쓰고, 방향은 from·to 가 묶는다(open_env 가 from=상대·to=나 를 확인).

받는 쪽 묻기 상태(ResultStore):
    받음 → (거절 | 접수) → queued → running → (answer | error) → KEEP_S 보관 → 지움
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from session_manager.e2ee import b64u, unb64u

PV = 0
AAD_PREFIX = b"clewpath-pclink/0\n"
KINDS = frozenset({"hello", "list", "list_result", "ask", "accepted", "poll", "answer", "pending",
                   "refused", "error", "pair_cred", "pair_done", "unpair"})
HOST_KINDS = frozenset({"hello", "list", "list_result", "pair_cred", "pair_done", "unpair"})  # session 이 null 일 수 있는 종류
SKEW_S = 60            # 두 PC 시계 차 허용
MAX_TTL_S = 600        # 이보다 먼 만료는 거부 — 중복 장부가 무한히 자라지 않게
DEFAULT_TTL_S = 120
NONCE_LEN = 12
KEY_LEN = 32

_HOST_RE = re.compile(r"^[0-9a-f]{16}$")
_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


class ProtocolError(Exception):
    """봉투·짝짓기 규약 위반. code 는 문서 4·5절의 코드(bad_version·bad_kind·not_paired·expired·bad_seal …)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class LedgerError(Exception):
    """중복 id 장부를 읽거나 쓰지 못했다 — 받는 쪽은 거부한다(fail-closed)."""


# ---------------------------------------------------------------- 공용

def canonical(x) -> bytes:
    """봉투 머리·본문의 바이트 표현(문서 3절). 다른 언어 구현도 같은 바이트를 만들어야 한다."""
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def new_host_id() -> str:
    return secrets.token_hex(8)


def is_host_id(s) -> bool:
    return isinstance(s, str) and bool(_HOST_RE.match(s))


def addr(host: str, session: str | None = None) -> dict:
    return {"host": host, "session": session}


# ---------------------------------------------------------------- 머리

def header(kind: str, frm: dict, to: dict, *, reply_to: str | None = None, ttl: int = DEFAULT_TTL_S,
           now: float | None = None, msg_id: str | None = None) -> dict:
    if kind not in KINDS:
        raise ProtocolError("bad_kind", kind)
    t = int(now if now is not None else time.time())
    return {"v": PV, "id": msg_id or str(uuid.uuid4()), "reply_to": reply_to, "kind": kind,
            "from": {"host": frm.get("host"), "session": frm.get("session")},
            "to": {"host": to.get("host"), "session": to.get("session")},
            "expires_at": t + int(ttl)}


def _check_addr(a, kind: str, side: str) -> None:
    if not isinstance(a, dict) or set(a) != {"host", "session"} or not is_host_id(a.get("host")):
        raise ProtocolError("bad_header", f"{side}")
    s = a.get("session")
    if s is None:
        if kind not in HOST_KINDS and side == "to":
            raise ProtocolError("bad_header", f"{side}.session")
    elif not (isinstance(s, str) and _ID_RE.match(s)):
        raise ProtocolError("bad_header", f"{side}.session")


def validate_header(h, *, now: float | None = None) -> None:
    """버전·종류·모양·만료(문서 4절 1·2·4). 짝 확인(3)과 개봉(5)은 open_env 가 한다."""
    if not isinstance(h, dict):
        raise ProtocolError("bad_header")
    if h.get("v") != PV:
        raise ProtocolError("bad_version", str(h.get("v")))
    kind = h.get("kind")
    if kind not in KINDS:
        raise ProtocolError("bad_kind", str(kind))
    if set(h) != {"v", "id", "reply_to", "kind", "from", "to", "expires_at"}:
        raise ProtocolError("bad_header", "fields")
    if not (isinstance(h["id"], str) and _ID_RE.match(h["id"])):
        raise ProtocolError("bad_header", "id")
    if h["reply_to"] is not None and not (isinstance(h["reply_to"], str) and _ID_RE.match(h["reply_to"])):
        raise ProtocolError("bad_header", "reply_to")
    _check_addr(h["from"], kind, "from")
    _check_addr(h["to"], kind, "to")
    exp = h["expires_at"]
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise ProtocolError("bad_header", "expires_at")
    t = int(now if now is not None else time.time())
    if exp < t - SKEW_S or exp > t + MAX_TTL_S + SKEW_S:
        raise ProtocolError("expired")


def peer_of(env) -> str | None:
    """짝을 찾을 보낸 Host(아직 믿지 않는 값). 모양이 틀리면 None."""
    try:
        host = env["h"]["from"]["host"]
    except (KeyError, TypeError):
        return None
    return host if is_host_id(host) else None


# ---------------------------------------------------------------- 봉인·개봉

def _aad(h: dict) -> bytes:
    return AAD_PREFIX + canonical(h)


def seal(key: bytes, h: dict, body: dict, *, _nonce: bytes | None = None) -> dict:
    """머리 h 와 본문 body 를 봉투로. _nonce 는 고정 시험 벡터 전용."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise ProtocolError("bad_key")
    if not isinstance(body, dict):
        raise ProtocolError("bad_body")
    validate_header(h, now=h["expires_at"] - 1)   # 모양만 — 만료 판정은 받는 쪽 몫
    nonce = _nonce if _nonce is not None else os.urandom(NONCE_LEN)
    ct = AESGCM(bytes(key)).encrypt(nonce, canonical(body), _aad(h))
    return {"h": h, "n": b64u(nonce), "c": b64u(ct)}


def open_env(key: bytes, env, *, my_host: str, peer_host: str, now: float | None = None) -> tuple[dict, dict]:
    """봉투 → (머리, 본문). 문서 4절 1~5 순서로 검사하고 실패는 ProtocolError.

    peer_host 는 호출자가 peer_of(env) 로 찾은 짝 상대 — 머리의 from 이 그 상대이고 to 가 나인지 여기서 다시 묶는다.
    중복 id(6)는 호출자가 Ledger 로 확인한다(개봉 뒤에만 — 남의 id 로 장부를 채우지 못하게).
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(env, dict) or set(env) != {"h", "n", "c"}:
        raise ProtocolError("bad_header", "envelope")
    h = env["h"]
    validate_header(h, now=now)
    if h["from"]["host"] != peer_host or h["to"]["host"] != my_host or peer_host == my_host:
        raise ProtocolError("not_paired")
    try:
        nonce = unb64u(env["n"])
        ct = unb64u(env["c"])
    except Exception as e:  # noqa: BLE001
        raise ProtocolError("bad_seal") from e
    if len(nonce) != NONCE_LEN or len(ct) < 16:
        raise ProtocolError("bad_seal")
    try:
        pt = AESGCM(bytes(key)).decrypt(nonce, ct, _aad(h))
        body = json.loads(pt.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ProtocolError("bad_seal") from e
    if not isinstance(body, dict):
        raise ProtocolError("bad_seal")
    return h, body


# ---------------------------------------------------------------- 중복 id 장부(재시작 뒤에도 남음)

class Ledger:
    """본 봉투 id 를 만료(+시계 차)까지 기억한다. sqlite 한 파일 — 쓰기는 원자적이고 프로세스 재시작 뒤에도 남는다.

    seen_or_record 는 처음이면 기록하고 True, 본 적 있으면 False. 열거나 쓰지 못하면 LedgerError(받는 쪽은 거부).
    """

    PRUNE_EVERY_S = 300

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._pruned_at = 0.0

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.path), timeout=5)
        c.execute("CREATE TABLE IF NOT EXISTS seen (peer TEXT NOT NULL, id TEXT NOT NULL, kind TEXT, "
                  "until INTEGER NOT NULL, PRIMARY KEY (peer, id))")
        return c

    def seen_or_record(self, peer: str, msg_id: str, kind: str, until: int, now: float | None = None) -> bool:
        t = now if now is not None else time.time()
        try:
            with self._lock:
                c = self._conn()
                try:
                    with c:
                        if t - self._pruned_at > self.PRUNE_EVERY_S:
                            c.execute("DELETE FROM seen WHERE until < ?", (int(t),))
                            self._pruned_at = t
                        cur = c.execute("INSERT OR IGNORE INTO seen (peer, id, kind, until) VALUES (?, ?, ?, ?)",
                                        (peer, msg_id, kind, int(until) + SKEW_S))
                        return cur.rowcount == 1
                finally:
                    c.close()
        except sqlite3.Error as e:
            raise LedgerError(f"{type(e).__name__}: {e}") from e

    def forget_peer(self, peer: str) -> None:
        """짝 해제 때 그 상대의 기록을 지운다(실패는 무시 — 만료로 어차피 사라진다)."""
        try:
            with self._lock:
                c = self._conn()
                try:
                    with c:
                        c.execute("DELETE FROM seen WHERE peer = ?", (peer,))
                finally:
                    c.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- 결과 보관(받는 쪽, 메모리)

class ResultStore:
    """받는 쪽 묻기의 상태와 결과. 끝난 뒤 KEEP_S 동안 보관해 같은 묻기의 재전송·poll 에 다시 실행하지 않고 돌려준다.

    결과는 묻은 짝(peer)에게만 보인다. 재시작하면 사라진다(poll → unknown_ask) — 중복 실행 방지는 Ledger 가 맡는다.
    """

    KEEP_S = 600
    MAX = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._d: dict[str, dict] = {}

    def start(self, ask_id: str, peer: str, now: float | None = None) -> bool:
        """새 묻기를 queued 로 등록. 이미 있으면 False(다시 실행하지 않는다)."""
        t = now if now is not None else time.time()
        with self._lock:
            self._prune(t)
            if ask_id in self._d:
                return False
            if len(self._d) >= self.MAX:
                # 끝난 것부터 오래된 순으로 비운다(진행 중은 지우지 않는다)
                done = sorted((v["done_at"], k) for k, v in self._d.items() if v["done_at"] is not None)
                for _, k in done[:max(1, len(self._d) - self.MAX + 1)]:
                    self._d.pop(k, None)
            self._d[ask_id] = {"peer": peer, "state": "queued", "kind": None, "body": None,
                               "created": t, "done_at": None}
            return True

    def running(self, ask_id: str) -> None:
        with self._lock:
            r = self._d.get(ask_id)
            if r and r["state"] == "queued":
                r["state"] = "running"

    def finish(self, ask_id: str, kind: str, body: dict, now: float | None = None) -> None:
        """kind: answer | error | refused."""
        t = now if now is not None else time.time()
        with self._lock:
            r = self._d.get(ask_id)
            if r is None:
                return
            r.update(state="done", kind=kind, body=body, done_at=t)

    def get(self, ask_id: str, peer: str, now: float | None = None) -> dict | None:
        """{"state": queued|running|done, "kind", "body"} — 그 짝의 묻기가 아니면 None."""
        t = now if now is not None else time.time()
        with self._lock:
            self._prune(t)
            r = self._d.get(ask_id)
            if r is None or r["peer"] != peer:
                return None
            return {"state": r["state"], "kind": r["kind"], "body": r["body"]}

    def forget_peer(self, peer: str) -> None:
        with self._lock:
            for k in [k for k, v in self._d.items() if v["peer"] == peer]:
                self._d.pop(k, None)

    def _prune(self, t: float) -> None:
        for k in [k for k, v in self._d.items() if v["done_at"] is not None and t - v["done_at"] > self.KEEP_S]:
            self._d.pop(k, None)


# ---------------------------------------------------------------- 짝짓기 암호(문서 6절)

INVITE_PREFIX = "clewpath-pair:0:"
INVITE_TTL_S = 24 * 3600
PAIR_TS_SKEW_S = 300
_INVITE_FIELDS = {"v", "inv", "k", "host", "name", "relay", "cp", "room", "cred", "exp"}


def encode_invite(d: dict) -> str:
    return INVITE_PREFIX + b64u(canonical(d))


def decode_invite(s: str, *, now: float | None = None) -> dict:
    """초대 문자열 → dict. 모양·만료가 틀리면 ProtocolError(bad_invite·invite_expired)."""
    s = (s or "").strip()
    if not s.startswith(INVITE_PREFIX):
        raise ProtocolError("bad_invite", "prefix")
    try:
        d = json.loads(unb64u(s[len(INVITE_PREFIX):]).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ProtocolError("bad_invite", "decode") from e
    if not isinstance(d, dict) or set(d) != _INVITE_FIELDS or d.get("v") != PV:
        raise ProtocolError("bad_invite", "fields")
    if not is_host_id(d["host"]) or not (isinstance(d["inv"], str) and re.fullmatch(r"[0-9a-f]{16}", d["inv"])):
        raise ProtocolError("bad_invite", "ids")
    try:
        k = unb64u(d["k"])
    except Exception as e:  # noqa: BLE001
        raise ProtocolError("bad_invite", "k") from e
    if len(k) != KEY_LEN:
        raise ProtocolError("bad_invite", "k")
    cred = d["cred"]
    if not (isinstance(cred, dict) and isinstance(cred.get("id"), str) and isinstance(cred.get("secret"), str)):
        raise ProtocolError("bad_invite", "cred")
    for f in ("relay", "cp", "room", "name"):
        if not isinstance(d[f], str):
            raise ProtocolError("bad_invite", f)
    if not str(d["relay"]).startswith(("wss://", "ws://")) or not str(d["cp"]).startswith(("https://", "http://")):
        raise ProtocolError("bad_invite", "url")
    t = int(now if now is not None else time.time())
    if not isinstance(d["exp"], int) or d["exp"] < t:
        raise ProtocolError("invite_expired")
    return d


def _hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=salt or None, info=info).derive(ikm)


def invite_mac_key(k: bytes) -> bytes:
    return _hkdf(k, b"", b"clewpath-pclink/0 invite-mac")


def pair_mac(kmac: bytes, *parts: str) -> str:
    return b64u(hmac.new(kmac, "|".join(parts).encode("utf-8"), hashlib.sha256).digest())


def pair_mac_ok(kmac: bytes, mac: str, *parts: str) -> bool:
    return isinstance(mac, str) and hmac.compare_digest(pair_mac(kmac, *parts), mac)


def x25519_keypair(_private: bytes | None = None):
    """(비밀키 객체, 공개키 b64url). _private 는 고정 시험 벡터 전용."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sk = X25519PrivateKey.from_private_bytes(_private) if _private is not None else X25519PrivateKey.generate()
    return sk, b64u(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def derive_pair_key(sk, peer_epk: str, k: bytes, host_b: str, host_a: str, epk_a: str, epk_b: str) -> bytes:
    """운영 짝 키(문서 6절). 초대 비밀 k 를 salt 로, 두 host_id 와 두 공개키를 info 로 묶는다."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    try:
        pub = X25519PublicKey.from_public_bytes(unb64u(peer_epk))
        shared = sk.exchange(pub)
    except Exception as e:  # noqa: BLE001
        raise ProtocolError("bad_pair", "epk") from e
    if shared == b"\x00" * 32:          # 작은 위수 점 — 공유 비밀이 0 이면 거부
        raise ProtocolError("bad_pair", "epk")
    info = "\n".join(["clewpath-pclink/0 pair-key", host_b, host_a, epk_a, epk_b]).encode("utf-8")
    return _hkdf(shared, k, info)
