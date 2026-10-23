"""pclink 정책 층 — 짝 목록·PC 등급 기기·세션별 허용·첫 연락 승인·한도·끄기 스위치·감사 장부·짝짓기 상태.

정본: docs/designs/pclink-protocol-v0.md(6절 짝짓기), 기획 docs/designs/cross-pc-messaging.md(결정 D5~D14).
보내는 법(link)·Claude 를 부르는 법(webapi)은 모른다. 네트워크(CP 자격 발급·폐기)는 잠금 밖에서 한다.

파일(데이터 폴더/pclink/):
    identity.json  이 Host 의 host_id(한 번 만들어 고정)
    state.json     짝·초대·세션 허용·첫 연락 승인·끄기 스위치. 짝 키·상대 자격 비밀은 osdep.protect(Windows DPAPI)로 감싼다
    seen.sqlite    중복 id 장부(protocol.Ledger)
    audit.jsonl    감사 장부(본문 없음)

state.json 이 깨져 있으면 덮어쓰지 않는다 — 읽는 쪽은 '짝 없음'(거부), 쓰는 쪽은 PolicyCorrupt(fail-closed).

짝짓기 상태(초대한 쪽 B):
    초대 open ──init(mac 맞음)──▶ pairing(손잡기 120초) ──A 의 pair_cred──▶ used + 짝 active
                                      └ 120초 지남 ─▶ 준 자격 폐기·open 으로 되돌림(초대는 pair_done 을 보내야 소모)
받는 쪽 A: invitee_begin → (B resp) invitee_on_resp → 짝 pending → (pair_done) invitee_on_done → active, 실패면 invitee_abort.
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import threading
import time
from pathlib import Path

from session_manager.e2ee import b64u, unb64u
from session_manager.pclink import protocol as P

STATE_V = 1
CONTACT_TTL_S = 30 * 86400        # 첫 연락 승인 = 그 세션 지금·앞으로 대화 전체, 30일(D13)
HANDSHAKE_S = 120                 # B: resp 를 보낸 뒤 A 의 pair_cred 를 기다리는 시간
PER_MIN = 6
PER_DAY = 200
PROMPT_MAX = 8000
ANSWER_MAX = 20000
ASK_TIMEOUT_S = 180
# 비용(2026-10-23 사장님 결정: 묻기당 2달러·짝당 하루 20달러, API 기준 어림). 복제본 비용은 그 세션의 '마지막 압축 이후 대화
# 분량'에 비례한다(T12 실측 250KB ≈ 0.47달러). claude 의 --max-budget-usd 는 이어받은 세션의 과거 누적 비용과 비교돼
# 지금 쓰는 세션에는 늘 실패하므로 쓰지 않고, 실행 전에 분량으로 어림해 막는다(inbound._tail_bytes).
ASK_MAX_USD = 2.0
DAY_BUDGET_USD = 20.0
USD_PER_MB = 1.9                  # webapi.RESUME_USD_PER_MB 와 같은 값(서비스 묻기와 같은 어림)
HOST_CONCURRENCY = 2
HOST_QUEUE = 4
CLEANUP_KEEP_S = 7 * 86400
ACCEPT_KEEP_S = 3600             # 붙여 넣은 초대를 승인 전까지 들고 있는 시간(승인 요청 30분 + 여유)        # 해제 알림을 못 보낸 짝을 '상대 쪽 정리 대기'로 두는 기간


class PolicyError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class PolicyCorrupt(PolicyError):
    def __init__(self):
        super().__init__("policy_corrupt", 503)


def _log(msg: str) -> None:
    print(f"[pclink] {msg}", flush=True)


# ---------------------------------------------------------------- 바깥 의존(시험에서 바꿔 끼운다)

class _Devices:
    """devices 모듈 중 pclink 가 쓰는 것."""

    def add_pc(self, name: str, peer_host: str) -> str:
        from session_manager import devices
        return devices.add_pc_device(name, peer_host)

    def link_cred(self, did: str, cpub: str | None) -> None:
        from session_manager import devices
        devices.set_client_public_id(did, cpub)

    def is_active(self, did: str) -> bool:
        from session_manager import devices
        return devices.is_active(did)

    def remove(self, did: str) -> None:
        from session_manager import devices
        devices.remove(did)


class _Cp:
    """CP 자격 발급·폐기(cp_client). 실패면 None/False — 호출자가 '불명'으로 다룬다."""

    def issue(self, device_id: str, name: str) -> dict | None:
        from session_manager import cp_client
        cc = cp_client.issue_client_credential(device_id=device_id, name=name)
        if not cc or not cc.get("client_public_id") or not cc.get("client_secret"):
            return None
        return {"id": cc["client_public_id"], "secret": cc["client_secret"]}

    def revoke(self, cred_id: str) -> bool:
        from session_manager import cp_client
        return bool(cp_client.revoke_client_credential(cred_id))


def _protect(raw: bytes) -> dict:
    from session_manager import osdep
    try:
        blob = osdep.protect(raw)
    except Exception:  # noqa: BLE001
        blob = None
    return {"b": b64u(blob if blob else raw), "dpapi": bool(blob)}


def _unprotect(rec) -> bytes:
    from session_manager import osdep
    if not isinstance(rec, dict) or "b" not in rec:
        raise PolicyCorrupt()
    raw = unb64u(rec["b"])
    if rec.get("dpapi"):
        raw = osdep.unprotect(raw) or b""
        if not raw:
            raise PolicyCorrupt()
    return raw


def _blank() -> dict:
    return {"v": STATE_V, "paused": False, "pairs": {}, "invites": {}, "accepts": {}, "sessions": {}, "contacts": {},
            "revoke_pending": []}


# ---------------------------------------------------------------- 정책

class Policy:
    def __init__(self, base: Path, *, devices=None, cp=None, clock=time.time):
        self.base = Path(base)
        self.devices = devices or _Devices()
        self.cp = cp or _Cp()
        self.clock = clock
        self._lock = threading.RLock()
        self.ledger = P.Ledger(self.base / "seen.sqlite")
        self.results = P.ResultStore()
        self._rate: dict[str, collections.deque] = {}
        self._hs: dict[str, dict] = {}          # 초대 id → 진행 중 손잡기(B, 메모리만 — 재시작이면 되돌림과 같다)
        self._corrupt_logged = False

    # ---- 저장 ----
    def _file(self) -> Path:
        return self.base / "state.json"

    def _load(self) -> dict:
        f = self._file()
        if not f.exists():
            return _blank()
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or d.get("v") != STATE_V:
                raise ValueError("version")
        except Exception:  # noqa: BLE001
            if not self._corrupt_logged:
                self._corrupt_logged = True
                _log(f"[!] 정책 파일이 깨졌습니다 — 짝 없음으로 보고 모든 pclink 를 거부합니다: {f}")
            raise PolicyCorrupt() from None
        for k, v in _blank().items():
            d.setdefault(k, v)
        return d

    def _save(self, d: dict) -> None:
        f = self._file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, f)

    def _mutate(self, fn):
        with self._lock:
            d = self._load()
            r = fn(d)
            if r is not None:
                self._save(d)
            return r

    def _read(self, fn):
        with self._lock:
            return fn(self._load())

    def now(self) -> int:
        return int(self.clock())

    # ---- 신원 ----
    def host_id(self) -> str:
        f = self.base / "identity.json"
        with self._lock:
            try:
                hid = json.loads(f.read_text(encoding="utf-8")).get("host_id")
                if P.is_host_id(hid):
                    return hid
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                raise PolicyCorrupt() from None
            hid = P.new_host_id()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"host_id": hid, "created": self.now()}), encoding="utf-8")
            return hid

    # ---- 끄기 스위치 ----
    def paused(self) -> bool:
        try:
            return bool(self._read(lambda d: d["paused"]))
        except PolicyCorrupt:
            return True

    def set_paused(self, on: bool) -> bool:
        self._mutate(lambda d: d.update(paused=bool(on)) or True)
        self.audit("paused" if on else "resumed")
        return bool(on)

    # ---- 감사 장부(본문 없음) ----
    def audit(self, event: str, **fields) -> None:
        from session_manager import jsonl_log
        jsonl_log.append(self.base / "audit.jsonl", {"event": event, **fields})

    def audit_tail(self, n: int = 50) -> list[dict]:
        from session_manager import jsonl_log
        return jsonl_log.tail(self.base / "audit.jsonl", n)

    # ---- 짝 ----
    @staticmethod
    def _pair_view(p: dict) -> dict:
        return {"host": p["host"], "name": p.get("name"), "state": p.get("state"), "role": p.get("role"),
                "created": p.get("created"), "last_ok": p.get("last_ok"), "peer_ver": p.get("peer_ver"),
                "device_id": p.get("device_id")}

    def list_pairs(self) -> list[dict]:
        try:
            pairs = self._read(lambda d: [dict(p) for p in d["pairs"].values()])
        except PolicyCorrupt:
            return []
        out = []
        for p in pairs:
            v = self._pair_view(p)
            if p.get("state") == "active" and not self.devices.is_active(p.get("device_id") or ""):
                v["state"] = "revoked"            # 기기 목록에서 폐기·삭제됨 — 짝은 더 쓰지 못한다
            out.append(v)
        return out

    def pair_name(self, peer: str) -> str | None:
        try:
            return self._read(lambda d: (d["pairs"].get(peer) or {}).get("name"))
        except PolicyCorrupt:
            return None

    def key_for(self, peer: str, kind: str) -> bytes | None:
        """받은 봉투를 열 짝 키. 살아 있는 짝(기기 활성)이면 그 키, 해제 정리 중이면 unpair 만, 손잡기 중이면 pair_cred 만."""
        try:
            p = self._read(lambda d: dict(d["pairs"].get(peer) or {}))
        except PolicyCorrupt:
            return None
        if p:
            st = p.get("state")
            if st == "active" and self.devices.is_active(p.get("device_id") or ""):
                return _unprotect(p["key"])
            if st == "cleanup" and kind == "unpair":
                return _unprotect(p["key"])
            return None
        if kind == "pair_cred":
            with self._lock:
                for hs in self._hs.values():
                    if hs["peer"] == peer and hs["until"] >= self.now():
                        return hs["key"]
        return None

    def peer_out(self, peer: str) -> dict | None:
        """상대 방에 붙을 길: {relay, cp, room, cred_id, cred_secret, key}. 살아 있는 짝(또는 정리 대기 — 해제 알림용)만."""
        try:
            p = self._read(lambda d: dict(d["pairs"].get(peer) or {}))
        except PolicyCorrupt:
            return None
        if not p or p.get("state") not in ("active", "cleanup") or not p.get("out"):
            return None
        if p["state"] == "active" and not self.devices.is_active(p.get("device_id") or ""):
            return None
        o = p["out"]
        return {"relay": o["relay"], "cp": o["cp"], "room": o["room"], "cred_id": o["cred_id"],
                "cred_secret": _unprotect(o["cred_secret"]).decode("utf-8"), "key": _unprotect(p["key"]),
                "state": p["state"]}

    def mark_ok(self, peer: str, peer_ver: str | None = None) -> None:
        def _f(d):
            p = d["pairs"].get(peer)
            if not p:
                return None
            p["last_ok"] = self.now()
            if peer_ver:
                p["peer_ver"] = str(peer_ver)[:40]
            return True
        try:
            self._mutate(_f)
        except PolicyCorrupt:
            pass

    # ---- 세션별 허용(기본 끔) ----
    def allowed(self, sid: str) -> bool:
        try:
            return bool(self._read(lambda d: (d["sessions"].get(sid) or {}).get("allow")))
        except PolicyCorrupt:
            return False

    def allowed_sessions(self) -> list[str]:
        try:
            return self._read(lambda d: [s for s, v in d["sessions"].items() if v.get("allow")])
        except PolicyCorrupt:
            return []

    def set_allow(self, sid: str, on: bool) -> bool:
        """끄면 그 세션의 첫 연락 승인을 모두 폐기한다(D13)."""
        def _f(d):
            if on:
                d["sessions"][sid] = {"allow": True, "since": self.now()}
            else:
                d["sessions"].pop(sid, None)
                for k in [k for k in d["contacts"] if k.split("|", 1)[1] == sid]:
                    d["contacts"].pop(k)
            return True
        self._mutate(_f)
        self.audit("allow_on" if on else "allow_off", session=sid)
        return bool(on)

    # ---- 첫 연락 승인 ----
    def contact_state(self, peer: str, sid: str) -> str:
        """approved | pending | none. pending 은 승인 요청이 아직 살아 있을 때만(30분 넘으면 none 으로 다시 요청)."""
        try:
            c = self._read(lambda d: dict(d["contacts"].get(f"{peer}|{sid}") or {}))
        except PolicyCorrupt:
            return "none"
        t = self.now()
        if c.get("state") == "approved" and int(c.get("expires_at") or 0) > t:
            return "approved"
        if c.get("state") == "pending" and int(c.get("pending_until") or 0) > t:
            return "pending"
        return "none"

    def mark_contact_pending(self, peer: str, sid: str, approval_id: str, until: int) -> None:
        self._mutate(lambda d: d["contacts"].__setitem__(
            f"{peer}|{sid}", {"state": "pending", "approval_id": approval_id, "pending_until": int(until),
                              "requested_at": self.now()}) or True)

    def grant_contact(self, peer: str, sid: str) -> dict:
        """승인 실행기가 부른다. 짝이 살아 있고 세션 허용이 켜져 있어야 한다."""
        if not self.allowed(sid):
            raise PolicyError("not_allowed", 409)
        if not any(p["host"] == peer and p["state"] == "active" for p in self.list_pairs()):
            raise PolicyError("not_paired", 409)
        exp = self.now() + CONTACT_TTL_S
        self._mutate(lambda d: d["contacts"].__setitem__(
            f"{peer}|{sid}", {"state": "approved", "approved_at": self.now(), "expires_at": exp}) or True)
        self.audit("contact_granted", peer=peer, session=sid, expires_at=exp)
        return {"peer": peer, "session_id": sid, "expires_at": exp}

    def revoke_contacts(self, *, peer: str | None = None, sid: str | None = None) -> int:
        def _f(d):
            ks = [k for k in d["contacts"]
                  if (peer is None or k.split("|", 1)[0] == peer) and (sid is None or k.split("|", 1)[1] == sid)]
            for k in ks:
                d["contacts"].pop(k)
            return len(ks) or None
        return int(self._mutate(_f) or 0)

    def check_ask(self, peer: str, sid: str) -> str | None:
        """정책 판정(실행 전): None = 실행 가능, 아니면 거절 코드(paused·not_allowed·pending_approval·approval_needed)."""
        if self.paused():
            return "paused"
        if not self.allowed(sid):
            return "not_allowed"
        st = self.contact_state(peer, sid)
        if st == "approved":
            return None
        return "pending_approval" if st == "pending" else "approval_needed"

    # ---- 한도(실행 전 예약) ----
    def reserve(self, peer: str, est_usd: float = 0.0) -> int | None:
        """짝당 분 PER_MIN·일 PER_DAY·하루 어림 예산 DAY_BUDGET_USD. 넘으면 다시 해도 되는 초, 아니면 None(예약됨)."""
        t = self.clock()
        with self._lock:
            q = self._rate.setdefault(peer, collections.deque())      # (시각, 어림 달러)
            while q and t - q[0][0] >= 86400:
                q.popleft()
            if len(q) >= PER_DAY:
                return int(86400 - (t - q[0][0])) + 1
            last_min = [x for x, _ in q if t - x < 60]
            if len(last_min) >= PER_MIN:
                return int(60 - (t - last_min[0])) + 1
            spent = sum(u for _, u in q)
            if spent + est_usd > DAY_BUDGET_USD:
                need = spent + est_usd - DAY_BUDGET_USD
                for x, u in q:                                       # 오래된 예약이 빠지면 되는 시각
                    need -= u
                    if need <= 0:
                        return int(86400 - (t - x)) + 1
                return 86400
            q.append((t, float(est_usd)))
            return None

    # ---- 초대(B) ----
    def create_invite(self, *, relay: str, cp: str, room: str, name: str) -> dict:
        """1회용·24시간 초대(D5·D8·D12). 승인 실행기가 부른다. 초대 자격은 짝짓기용 — 끝나면 폐기."""
        if not (relay and cp and room):
            raise PolicyError("relay_unavailable", 503)
        me = self.host_id()
        inv = secrets.token_hex(8)
        k = os.urandom(P.KEY_LEN)
        cc = self.cp.issue(f"pclink-inv-{inv}", "PC 짝짓기 초대")
        if not cc:
            raise PolicyError("cp_unavailable", 503)
        exp = self.now() + P.INVITE_TTL_S
        s = P.encode_invite({"v": P.PV, "inv": inv, "k": b64u(k), "host": me, "name": name, "relay": relay,
                             "cp": cp, "room": room, "cred": {"id": cc["id"], "secret": cc["secret"]}, "exp": exp})

        def _f(d):
            d["invites"][inv] = {"inv": inv, "k": _protect(k), "s": _protect(s.encode("utf-8")), "cred_id": cc["id"],
                                 "relay": relay, "cp": cp, "room": room, "exp": exp, "state": "open",
                                 "created": self.now()}
            return True
        try:
            self._mutate(_f)
        except Exception:
            self._revoke_or_queue(cc["id"])
            raise
        self.audit("invite_created", inv=inv, exp=exp)
        return {"inv": inv, "exp": exp}

    def list_invites(self) -> list[dict]:
        try:
            return self._read(lambda d: [{"inv": v["inv"], "state": v["state"], "exp": v["exp"],
                                          "created": v.get("created")} for v in d["invites"].values()])
        except PolicyCorrupt:
            return []

    def invite_string(self, inv: str) -> str | None:
        """열려 있는 초대의 문자열(로컬 화면·E2EE+2FA 폰만 — 호출자가 막는다)."""
        r = self._read(lambda d: dict(d["invites"].get(inv) or {}))
        if not r or r["state"] != "open" or r["exp"] < self.now():
            return None
        return _unprotect(r["s"]).decode("utf-8")

    def cancel_invite(self, inv: str) -> bool:
        def _f(d):
            r = d["invites"].get(inv)
            if not r or r["state"] in ("used", "revoked"):
                return None
            r["state"] = "revoked"
            return r["cred_id"]
        cred = self._mutate(_f)
        if not cred:
            return False
        with self._lock:
            self._hs.pop(inv, None)
        self._revoke_or_queue(cred)
        self.audit("invite_cancelled", inv=inv)
        return True

    def pair_init(self, p: dict) -> dict:
        """B: A 의 init(문서 6절 1) → resp(2). CP 자격 발급(네트워크)을 하므로 이벤트 루프 밖에서 부를 것."""
        me = self.host_id()
        if not isinstance(p, dict) or p.get("step") != "init":
            raise P.ProtocolError("bad_pair", "step")
        inv, host_a, epk_a, ts = p.get("inv"), p.get("from"), p.get("epk"), p.get("ts")
        name_a = str(p.get("name") or "")[:40] or "PC"
        if not P.is_host_id(host_a) or host_a == me or not isinstance(epk_a, str) or not isinstance(ts, int):
            raise P.ProtocolError("bad_pair", "fields")
        if abs(self.now() - ts) > P.PAIR_TS_SKEW_S:
            raise P.ProtocolError("expired")
        r = self._read(lambda d: dict(d["invites"].get(inv) or {}) if isinstance(inv, str) else {})
        if not r or r["exp"] < self.now() or r["state"] not in ("open", "pairing"):
            raise P.ProtocolError("bad_invite")
        k = _unprotect(r["k"])
        kmac = P.invite_mac_key(k)
        if not P.pair_mac_ok(kmac, p.get("mac"), "init", inv, me, host_a, epk_a, str(ts)):
            raise P.ProtocolError("bad_invite", "mac")
        if self._read(lambda d: (d["pairs"].get(host_a) or {}).get("state") in ("active", "pending")):
            raise P.ProtocolError("already_paired")
        with self._lock:
            hs = self._hs.get(inv)
            if hs and hs["until"] >= self.now():
                raise P.ProtocolError("busy")
            self._hs[inv] = {"peer": host_a, "until": self.now() + HANDSHAKE_S, "key": None, "issued": None,
                             "name": name_a}
        try:
            sk, epk_b = P.x25519_keypair()
            key = P.derive_pair_key(sk, epk_a, k, me, host_a, epk_a, epk_b)
            cc = self.cp.issue(f"pclink-{host_a}", f"PC {name_a}")
            if not cc:
                raise P.ProtocolError("cp_unavailable")
        except Exception:
            with self._lock:
                self._hs.pop(inv, None)
            raise
        with self._lock:
            self._hs[inv].update(key=key, issued=cc["id"])
        self._mutate(lambda d: (d["invites"][inv].update(state="pairing") or True) if inv in d["invites"] else None)
        env = P.seal(key, P.header("pair_cred", P.addr(me), P.addr(host_a), now=self.now()),
                     {"room": r["room"], "relay": r["relay"], "cp": r["cp"],
                      "cred": {"id": cc["id"], "secret": cc["secret"]}})
        self.audit("pair_init", inv=inv, peer=host_a)
        return {"step": "resp", "epk": epk_b, "mac": P.pair_mac(kmac, "resp", inv, me, host_a, epk_a, epk_b), "env": env}

    def pair_complete(self, peer: str, body: dict) -> dict:
        """B: A 의 pair_cred(문서 6절 3) → 짝 active + pair_done 봉투(4). 초대 자격 폐기(네트워크)는 이 안에서 한다."""
        me = self.host_id()
        with self._lock:
            inv, hs = next(((i, h) for i, h in self._hs.items()
                            if h["peer"] == peer and h["key"] and h["until"] >= self.now()), (None, None))
        if not hs:
            raise P.ProtocolError("not_paired")
        out = self._out_from_body(body)
        did = self.devices.add_pc(hs["name"], peer)
        self.devices.link_cred(did, hs["issued"])

        def _f(d):
            d["pairs"][peer] = {"host": peer, "name": hs["name"], "state": "active", "role": "inviter",
                                "created": self.now(), "key": _protect(hs["key"]), "out": out, "device_id": did,
                                "issued_cred": hs["issued"], "last_ok": self.now()}
            r = d["invites"].get(inv)
            if r:
                r["state"] = "used"
                return r["cred_id"]
            return True
        inv_cred = self._mutate(_f)
        with self._lock:
            self._hs.pop(inv, None)
        if isinstance(inv_cred, str):
            self._revoke_or_queue(inv_cred)
        self.audit("paired", peer=peer, role="inviter", inv=inv)
        return P.seal(hs["key"], P.header("pair_done", P.addr(me), P.addr(peer), now=self.now()), {})

    def sweep_handshakes(self) -> int:
        """B: 120초 안에 끝나지 않은 손잡기 되돌림(준 자격 폐기, 초대는 다시 open). 지난 초대는 expired + 자격 폐기."""
        t = self.now()
        with self._lock:
            dead = [(i, h) for i, h in self._hs.items() if h["until"] < t]
            for i, _ in dead:
                self._hs.pop(i, None)
        for inv, h in dead:
            if h.get("issued"):
                self._revoke_or_queue(h["issued"])
            self._mutate(lambda d, inv=inv: (d["invites"][inv].update(state="open") or True)
                         if d["invites"].get(inv, {}).get("state") == "pairing" else None)
            self.audit("pair_rollback", inv=inv, peer=h["peer"])

        def _f(d):
            gone = []
            for r in d["invites"].values():
                if r["state"] in ("open", "pairing") and r["exp"] < t:
                    r["state"] = "expired"
                    gone.append(r["cred_id"])
            # 붙여 넣었지만 승인되지 않은 초대(승인 요청은 30분이면 만료) — 비밀을 오래 들고 있지 않는다
            stale = [k for k, v in d["accepts"].items() if v["exp"] < t or t - int(v.get("created") or 0) > ACCEPT_KEEP_S]
            for k in stale:
                d["accepts"].pop(k)
            return gone if (gone or stale) else None     # 바뀐 게 없으면 쓰지 않는다(30초마다 돈다)
        try:
            for cred in self._mutate(_f) or []:
                self._revoke_or_queue(cred)
        except PolicyCorrupt:
            pass
        self.retry_revokes()
        return len(dead)

    # ---- 초대 받기(A) ----
    def stash_accept(self, invite: str) -> dict:
        """붙여 넣은 초대를 승인 전까지 비밀로 보관한다(승인 대기열 인자에는 비밀을 싣지 않는다)."""
        d = P.decode_invite(invite, now=self.now())
        me = self.host_id()
        if d["host"] == me:
            raise PolicyError("self_invite", 409)
        if self._read(lambda s: (s["pairs"].get(d["host"]) or {}).get("state") in ("active", "pending")):
            raise PolicyError("already_paired", 409)
        aid = secrets.token_hex(6)
        self._mutate(lambda s: s["accepts"].__setitem__(aid, {"s": _protect(invite.strip().encode("utf-8")),
                                                              "exp": d["exp"], "created": self.now()}) or True)
        return {"accept_id": aid, "peer_host": d["host"], "peer_name": d["name"], "exp": d["exp"]}

    def take_accept(self, aid: str) -> str:
        def _f(s):
            r = s["accepts"].pop(aid, None)
            return r if r else None
        r = self._mutate(_f)
        if not r:
            raise PolicyError("accept_not_found", 404)
        return _unprotect(r["s"]).decode("utf-8")

    def invitee_begin(self, invite: str) -> tuple[dict, dict]:
        """A: (ctx, init 인자). ctx 의 비밀키는 메모리에만."""
        d = P.decode_invite(invite, now=self.now())
        me = self.host_id()
        if d["host"] == me:
            raise PolicyError("self_invite", 409)
        k = P.unb64u(d["k"])
        sk, epk_a = P.x25519_keypair()
        ts = self.now()
        kmac = P.invite_mac_key(k)
        init = {"step": "init", "inv": d["inv"], "from": me, "name": self._my_name(), "epk": epk_a, "ts": ts,
                "mac": P.pair_mac(kmac, "init", d["inv"], d["host"], me, epk_a, str(ts))}
        ctx = {"invite": d, "k": k, "sk": sk, "epk_a": epk_a, "me": me, "key": None, "issued": None, "did": None}
        return ctx, init

    def invitee_on_resp(self, ctx: dict, resp: dict, *, relay: str, cp: str, room: str) -> dict:
        """A: B 의 resp(2) 확인 → 짝 키 → B 자격 받기 → 내 방 자격 발급·PC 기기 등록 → pair_cred 봉투(3).
        relay·cp·room 은 내 방(B 가 나에게 붙을 길). CP 발급(네트워크)을 하므로 이벤트 루프 밖에서."""
        d, me = ctx["invite"], ctx["me"]
        host_b = d["host"]
        if not isinstance(resp, dict) or resp.get("step") != "resp" or not isinstance(resp.get("epk"), str):
            raise P.ProtocolError("bad_pair", "resp")
        kmac = P.invite_mac_key(ctx["k"])
        if not P.pair_mac_ok(kmac, resp.get("mac"), "resp", d["inv"], host_b, me, ctx["epk_a"], resp["epk"]):
            raise P.ProtocolError("bad_pair", "mac")
        key = P.derive_pair_key(ctx["sk"], resp["epk"], ctx["k"], host_b, me, ctx["epk_a"], resp["epk"])
        h, body = P.open_env(key, resp.get("env"), my_host=me, peer_host=host_b, now=self.now())
        if h["kind"] != "pair_cred":
            raise P.ProtocolError("bad_pair", "kind")
        out = self._out_from_body(body)
        if not (relay and cp and room):
            raise PolicyError("relay_unavailable", 503)
        cc = self.cp.issue(f"pclink-{host_b}", f"PC {d['name']}")
        if not cc:
            raise PolicyError("cp_unavailable", 503)
        ctx.update(key=key, issued=cc["id"])
        did = self.devices.add_pc(d["name"], host_b)
        ctx["did"] = did
        self.devices.link_cred(did, cc["id"])
        self._mutate(lambda s: s["pairs"].__setitem__(host_b, {
            "host": host_b, "name": d["name"], "state": "pending", "role": "invitee", "created": self.now(),
            "key": _protect(key), "out": out, "device_id": did, "issued_cred": cc["id"]}) or True)
        return P.seal(key, P.header("pair_cred", P.addr(me), P.addr(host_b), now=self.now()),
                      {"room": room, "relay": relay, "cp": cp, "cred": {"id": cc["id"], "secret": cc["secret"]}})

    def invitee_on_done(self, ctx: dict, env: dict) -> dict:
        d, me = ctx["invite"], ctx["me"]
        h, _ = P.open_env(ctx["key"], env, my_host=me, peer_host=d["host"], now=self.now())
        if h["kind"] != "pair_done":
            raise P.ProtocolError("bad_pair", "kind")
        self._mutate(lambda s: (s["pairs"][d["host"]].update(state="active", last_ok=self.now()) or True)
                     if d["host"] in s["pairs"] else None)
        self.audit("paired", peer=d["host"], role="invitee", inv=d["inv"])
        return {"host": d["host"], "name": d["name"]}

    def invitee_abort(self, ctx: dict) -> None:
        host_b = ctx["invite"]["host"]
        if ctx.get("issued"):
            self._revoke_or_queue(ctx["issued"])
        if ctx.get("did"):
            self.devices.remove(ctx["did"])
        try:
            self._mutate(lambda s: (s["pairs"].pop(host_b) and True)
                         if (s["pairs"].get(host_b) or {}).get("state") == "pending" else None)
        except PolicyCorrupt:
            pass
        self.audit("pair_aborted", peer=host_b, role="invitee")

    # ---- 해제 ----
    def unpair(self, peer: str, *, notified: bool) -> dict:
        """내 발급분은 즉시 CP 폐기·기기 삭제, 짝 키는 상대에게 알렸으면 지우고, 못 알렸으면 '정리 대기'(알림용 키만 남김)."""
        p = self._read(lambda d: dict(d["pairs"].get(peer) or {}))
        if not p:
            return {"peer": peer, "removed": False}
        revoked = self._revoke_or_queue(p["issued_cred"]) if p.get("issued_cred") else True
        if p.get("device_id"):
            self.devices.remove(p["device_id"])

        def _f(d):
            q = d["pairs"].get(peer)
            if not q:
                return None
            if notified or q.get("state") == "cleanup" or not q.get("out"):
                d["pairs"].pop(peer)
            else:
                q.update(state="cleanup", cleanup_since=self.now(), device_id=None, issued_cred=None)
            for k in [k for k in d["contacts"] if k.split("|", 1)[0] == peer]:
                d["contacts"].pop(k)
            return True
        self._mutate(_f)
        self.ledger.forget_peer(peer)
        self.results.forget_peer(peer)
        with self._lock:
            self._rate.pop(peer, None)
        self.audit("unpaired", peer=peer, notified=bool(notified), cp_revoked=bool(revoked))
        return {"peer": peer, "removed": True, "cp_revoked": bool(revoked), "cleanup": not notified}

    def cleanup_due(self) -> list[str]:
        """해제 알림을 다시 보낼 짝. CLEANUP_KEEP_S 를 넘긴 것은 지운다."""
        t = self.now()

        def _f(d):
            drop = [h for h, p in d["pairs"].items()
                    if p.get("state") == "cleanup" and t - int(p.get("cleanup_since") or 0) > CLEANUP_KEEP_S]
            for h in drop:
                d["pairs"].pop(h)
            return drop or None
        try:
            self._mutate(_f)
            return self._read(lambda d: [h for h, p in d["pairs"].items() if p.get("state") == "cleanup"])
        except PolicyCorrupt:
            return []

    # ---- CP 폐기 재시도 ----
    def _revoke_or_queue(self, cred_id: str) -> bool:
        try:
            ok = self.cp.revoke(cred_id)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            try:
                self._mutate(lambda d: (d["revoke_pending"].append({"cred": cred_id, "since": self.now()}) or True)
                             if not any(x["cred"] == cred_id for x in d["revoke_pending"]) else None)
            except PolicyCorrupt:
                pass
            _log(f"CP 자격 폐기 실패 → 다시 시도 목록({cred_id[:8]})")
        return ok

    def retry_revokes(self) -> int:
        try:
            pend = self._read(lambda d: list(d["revoke_pending"]))
        except PolicyCorrupt:
            return 0
        done = [x["cred"] for x in pend if self._try_revoke(x["cred"])]
        if done:
            self._mutate(lambda d: d.update(revoke_pending=[x for x in d["revoke_pending"] if x["cred"] not in done])
                         or True)
        return len(done)

    def _try_revoke(self, cred_id: str) -> bool:
        try:
            return bool(self.cp.revoke(cred_id))
        except Exception:  # noqa: BLE001
            return False

    # ---- 도움 ----
    @staticmethod
    def _out_from_body(body: dict) -> dict:
        cred = body.get("cred") if isinstance(body, dict) else None
        if not (isinstance(cred, dict) and isinstance(cred.get("id"), str) and isinstance(cred.get("secret"), str)
                and all(isinstance(body.get(f), str) and body.get(f) for f in ("room", "relay", "cp"))):
            raise P.ProtocolError("bad_pair", "cred")
        if not body["relay"].startswith(("wss://", "ws://")) or not body["cp"].startswith(("https://", "http://")):
            raise P.ProtocolError("bad_pair", "url")
        return {"relay": body["relay"], "cp": body["cp"], "room": body["room"], "cred_id": cred["id"],
                "cred_secret": _protect(cred["secret"].encode("utf-8"))}

    @staticmethod
    def _my_name() -> str:
        from session_manager import appconfig, devices
        return devices.clean_name(appconfig.machine_name(), "PC")


# ---------------------------------------------------------------- 기본 인스턴스

_DEFAULT: Policy | None = None
_DEFAULT_LOCK = threading.Lock()


def get() -> Policy:
    """Host 의 정책(데이터 폴더/pclink). 시험은 Policy(tmp) 를 직접 만든다."""
    global _DEFAULT
    from session_manager import config
    base = config.data_dir() / "pclink"
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT.base != base:
            _DEFAULT = Policy(base)
        return _DEFAULT
