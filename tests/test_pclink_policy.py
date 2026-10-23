"""pclink 정책 층 — 두 Host(A·B)를 한 프로세스에 두고 초대→짝짓기→허용→첫 연락 승인→한도→해제를 끝까지.

CP·기기 등록부는 가짜(Host 마다 따로). 선 위 약속은 docs/designs/pclink-protocol-v0.md 6절.
"""
from __future__ import annotations

import json

import pytest

from session_manager.pclink import policy as POL
from session_manager.pclink import protocol as P

SID = "94010000-0000-4000-8000-00000000000b"


class FakeCp:
    def __init__(self, tag):
        self.tag, self.n, self.live, self.fail_revoke, self.fail_issue = tag, 0, set(), False, False

    def issue(self, device_id, name):
        if self.fail_issue:
            return None
        self.n += 1
        cid = f"{self.tag}-cred-{self.n}"
        self.live.add(cid)
        return {"id": cid, "secret": f"secret-{cid}"}

    def revoke(self, cred_id):
        if self.fail_revoke:
            return False
        self.live.discard(cred_id)
        return True


class FakeDevices:
    def __init__(self):
        self.rows = {}

    def add_pc(self, name, peer_host):
        did = f"dev{len(self.rows) + 1}"
        self.rows[did] = {"name": name, "peer": peer_host, "revoked": False, "cpub": None}
        return did

    def link_cred(self, did, cpub):
        self.rows[did]["cpub"] = cpub

    def is_active(self, did):
        return did in self.rows and not self.rows[did]["revoked"]

    def remove(self, did):
        self.rows.pop(did, None)


class Clock:
    def __init__(self, t=1792650000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.setattr(POL.Policy, "_my_name", staticmethod(lambda: "PC-A"))
    clk = Clock()
    a = POL.Policy(tmp_path / "A", devices=FakeDevices(), cp=FakeCp("A"), clock=clk)
    b = POL.Policy(tmp_path / "B", devices=FakeDevices(), cp=FakeCp("B"), clock=clk)
    return a, b, clk


ROOM_A = dict(relay="wss://r.example/relay", cp="https://cp.example", room="roomA")
ROOM_B = dict(relay="wss://r.example/relay", cp="https://cp.example", room="roomB")


def _pair(a, b):
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    ctx, init = a.invitee_begin(s)
    resp = b.pair_init(init)
    env3 = a.invitee_on_resp(ctx, resp, **ROOM_A)
    peer = P.peer_of(env3)
    key = b.key_for(peer, "pair_cred")
    h, body = P.open_env(key, env3, my_host=b.host_id(), peer_host=peer, now=b.now())
    done = b.pair_complete(peer, body)
    a.invitee_on_done(ctx, done)
    return inv, s


# ---------------------------------------------------------------- 짝짓기

def test_full_pairing_is_symmetric(two):
    a, b, _ = two
    inv, s = _pair(a, b)
    pa, pb = a.list_pairs(), b.list_pairs()
    assert [(x["host"], x["state"], x["role"]) for x in pa] == [(b.host_id(), "active", "invitee")]
    assert [(x["host"], x["state"], x["role"]) for x in pb] == [(a.host_id(), "active", "inviter")]
    # 양쪽이 같은 짝 키, 서로의 방으로 가는 길
    oa, ob = a.peer_out(b.host_id()), b.peer_out(a.host_id())
    assert oa["key"] == ob["key"] and len(oa["key"]) == 32
    assert oa["room"] == "roomB" and ob["room"] == "roomA"
    # 각자 상대를 PC 등급 기기로 등록하고 자기가 발급한 자격을 그 기기에 묶음(해제 때 회수)
    rb = next(iter(b.devices.rows.values()))
    assert rb["peer"] == a.host_id() and rb["cpub"] in b.cp.live
    ra = next(iter(a.devices.rows.values()))
    assert ra["peer"] == b.host_id() and ra["cpub"] in a.cp.live
    # 초대는 쓰였고 초대 자격은 폐기됨, 다시 보여 줄 수 없음
    assert b.list_invites()[0]["state"] == "used"
    assert b.invite_string(inv["inv"]) is None
    assert f"B-cred-1" not in b.cp.live            # 초대 자격(첫 발급)
    # 운영 자격은 초대 자격과 다르다
    assert oa["cred_id"] != "B-cred-1"


def test_invite_is_single_use(two):
    a, b, _ = two
    _, s = _pair(a, b)
    ctx, init = a.invitee_begin(s)
    with pytest.raises(P.ProtocolError) as ei:
        b.pair_init(init)
    assert ei.value.code == "bad_invite"


def test_invite_expires_after_24h(two):
    a, b, clk = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    clk.t += P.INVITE_TTL_S + 1
    with pytest.raises(P.ProtocolError) as ei:
        a.invitee_begin(s)
    assert ei.value.code == "invite_expired"
    assert b.invite_string(inv["inv"]) is None
    b.sweep_handshakes()
    assert b.list_invites()[0]["state"] == "expired" and not b.cp.live


def test_wrong_secret_or_tampered_init_rejected(two):
    a, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    ctx, init = a.invitee_begin(s)
    for bad in ({**init, "epk": P.x25519_keypair()[1]}, {**init, "from": "1111111111111111"}, {**init, "mac": "x"}):
        with pytest.raises(P.ProtocolError):
            b.pair_init(bad)
    with pytest.raises(P.ProtocolError) as ei:
        b.pair_init({**init, "ts": init["ts"] - 400})
    assert ei.value.code == "expired"


def test_resp_from_impostor_rejected(two):
    """초대 비밀을 모르는 쪽(장악된 릴레이)이 resp 를 지어내면 A 는 받지 않는다."""
    a, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    ctx, init = a.invitee_begin(b.invite_string(inv["inv"]))
    resp = b.pair_init(init)
    fake = {**resp, "epk": P.x25519_keypair()[1]}
    with pytest.raises(P.ProtocolError):
        a.invitee_on_resp(ctx, fake, **ROOM_A)
    assert not a.devices.rows and not a.cp.live


def test_second_init_while_handshaking_is_busy(two, tmp_path, monkeypatch):
    a, b, clk = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    _, init = a.invitee_begin(s)
    b.pair_init(init)
    c = POL.Policy(tmp_path / "C", devices=FakeDevices(), cp=FakeCp("C"), clock=clk)
    _, init2 = c.invitee_begin(s)
    with pytest.raises(P.ProtocolError) as ei:
        b.pair_init(init2)
    assert ei.value.code == "busy"


def test_handshake_rollback_keeps_invite_usable(two):
    """A 의 pair_cred 가 120초 안에 안 오면 B 는 준 자격을 폐기하고 초대를 다시 연다(초대는 pair_done 을 보내야 소모)."""
    a, b, clk = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    _, init = a.invitee_begin(s)
    b.pair_init(init)
    issued = {c for c in b.cp.live if c != "B-cred-1"}
    clk.t += POL.HANDSHAKE_S + 1
    assert b.sweep_handshakes() == 1
    assert not (issued & b.cp.live)
    assert b.list_invites()[0]["state"] == "open"
    assert b.key_for(a.host_id(), "pair_cred") is None
    # 같은 초대로 다시 성공
    ctx, init = a.invitee_begin(s)
    resp = b.pair_init(init)
    env3 = a.invitee_on_resp(ctx, resp, **ROOM_A)
    h, body = P.open_env(b.key_for(a.host_id(), "pair_cred"), env3, my_host=b.host_id(), peer_host=a.host_id(),
                         now=b.now())
    a.invitee_on_done(ctx, b.pair_complete(a.host_id(), body))
    assert b.list_pairs()[0]["state"] == "active"


def test_invitee_abort_cleans_up(two):
    a, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    ctx, init = a.invitee_begin(b.invite_string(inv["inv"]))
    a.invitee_on_resp(ctx, b.pair_init(init), **ROOM_A)
    assert a.list_pairs()[0]["state"] == "pending"
    a.invitee_abort(ctx)
    assert a.list_pairs() == [] and not a.devices.rows and not a.cp.live


def test_self_invite_and_already_paired(two):
    a, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    with pytest.raises(POL.PolicyError):
        b.invitee_begin(b.invite_string(inv["inv"]))
    _pair(a, b)
    inv2 = b.create_invite(name="노트북", **ROOM_B)
    with pytest.raises(POL.PolicyError) as ei:
        a.stash_accept(b.invite_string(inv2["inv"]))
    assert ei.value.code == "already_paired"


def test_cancel_invite_revokes_cred(two):
    _, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    assert b.cancel_invite(inv["inv"]) is True
    assert not b.cp.live and b.invite_string(inv["inv"]) is None


def test_cp_unavailable(two):
    _, b, _ = two
    b.cp.fail_issue = True
    with pytest.raises(POL.PolicyError) as ei:
        b.create_invite(name="노트북", **ROOM_B)
    assert ei.value.code == "cp_unavailable"


def test_stash_accept_keeps_secret_out_of_args(two):
    a, b, _ = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    s = b.invite_string(inv["inv"])
    st = a.stash_accept(s)
    assert set(st) == {"accept_id", "peer_host", "peer_name", "exp"} and st["peer_name"] == "노트북"
    assert a.take_accept(st["accept_id"]) == s
    with pytest.raises(POL.PolicyError):
        a.take_accept(st["accept_id"])                 # 한 번만


# ---------------------------------------------------------------- 세션 허용·첫 연락 승인

def test_ask_decision_order(two):
    a, b, _ = two
    _pair(a, b)
    peer = a.host_id()
    assert b.check_ask(peer, SID) == "not_allowed"            # 기본 끔
    b.set_allow(SID, True)
    assert b.check_ask(peer, SID) == "approval_needed"
    b.mark_contact_pending(peer, SID, "ap_1", b.now() + 1800)
    assert b.check_ask(peer, SID) == "pending_approval"
    b.grant_contact(peer, SID)
    assert b.check_ask(peer, SID) is None
    b.set_paused(True)
    assert b.check_ask(peer, SID) == "paused"
    b.set_paused(False)
    b.set_allow(SID, False)                                   # 허용 끄면 승인도 폐기
    b.set_allow(SID, True)
    assert b.check_ask(peer, SID) == "approval_needed"


def test_contact_expires_after_30_days(two):
    a, b, clk = two
    _pair(a, b)
    b.set_allow(SID, True)
    b.grant_contact(a.host_id(), SID)
    clk.t += POL.CONTACT_TTL_S + 1
    assert b.check_ask(a.host_id(), SID) == "approval_needed"


def test_pending_contact_lapses(two):
    a, b, clk = two
    _pair(a, b)
    b.set_allow(SID, True)
    b.mark_contact_pending(a.host_id(), SID, "ap_1", b.now() + 1800)
    clk.t += 1801
    assert b.contact_state(a.host_id(), SID) == "none"


def test_grant_requires_allow_and_pair(two):
    a, b, _ = two
    with pytest.raises(POL.PolicyError):
        b.grant_contact(a.host_id(), SID)
    _pair(a, b)
    with pytest.raises(POL.PolicyError) as ei:
        b.grant_contact(a.host_id(), SID)
    assert ei.value.code == "not_allowed"


# ---------------------------------------------------------------- 한도

def test_rate_limit_minute_and_day(two):
    _, b, clk = two
    peer = "a1b2c3d4e5f60718"
    for _ in range(POL.PER_MIN):
        assert b.reserve(peer) is None
    ra = b.reserve(peer)
    assert ra is not None and 0 < ra <= 61
    clk.t += 61
    assert b.reserve(peer) is None
    # 하루 상한
    n = POL.PER_MIN + 1
    while n < POL.PER_DAY:
        clk.t += 11
        assert b.reserve(peer) is None
        n += 1
    clk.t += 61
    assert b.reserve(peer) > 60
    assert b.reserve("0f1e2d3c4b5a6978") is None              # 짝마다 따로


# ---------------------------------------------------------------- 해제

def test_unpair_notified_removes_everything(two):
    a, b, _ = two
    _pair(a, b)
    b.set_allow(SID, True)
    b.grant_contact(a.host_id(), SID)
    issued = b.list_pairs()[0]
    r = b.unpair(a.host_id(), notified=True)
    assert r["removed"] and r["cp_revoked"]
    assert b.list_pairs() == [] and not b.devices.rows
    assert b.key_for(a.host_id(), "ask") is None and b.key_for(a.host_id(), "unpair") is None
    assert b.contact_state(a.host_id(), SID) == "none"
    assert issued["device_id"] is not None


def test_unpair_unnotified_keeps_cleanup_for_notice(two):
    a, b, clk = two
    _pair(a, b)
    r = b.unpair(a.host_id(), notified=False)
    assert r["cleanup"]
    assert b.list_pairs()[0]["state"] == "cleanup"
    assert b.key_for(a.host_id(), "ask") is None               # 묻기는 못 받음
    assert b.key_for(a.host_id(), "unpair") is not None        # 해제 알림만
    assert b.peer_out(a.host_id())["state"] == "cleanup"       # 알림을 보낼 길은 남김
    assert b.cleanup_due() == [a.host_id()]
    clk.t += POL.CLEANUP_KEEP_S + 1
    assert b.cleanup_due() == [] and b.list_pairs() == []


def test_revoke_failure_is_retried(two):
    a, b, _ = two
    _pair(a, b)
    b.cp.fail_revoke = True
    r = b.unpair(a.host_id(), notified=True)
    assert r["cp_revoked"] is False
    b.cp.fail_revoke = False
    assert b.retry_revokes() == 1
    assert b.retry_revokes() == 0


def test_device_revoked_disables_pair(two):
    a, b, _ = two
    _pair(a, b)
    did = b.list_pairs()[0]["device_id"]
    b.devices.rows[did]["revoked"] = True                    # 📱 목록에서 폐기
    assert b.list_pairs()[0]["state"] == "revoked"
    assert b.key_for(a.host_id(), "ask") is None
    assert b.peer_out(a.host_id()) is None


# ---------------------------------------------------------------- 저장·fail-closed

def test_secrets_are_not_plaintext_on_windows(two):
    import os
    a, b, _ = two
    _pair(a, b)
    raw = (b.base / "state.json").read_text(encoding="utf-8")
    key = b.peer_out(a.host_id())["key"]
    assert P.b64u(key) not in raw
    if os.name == "nt":
        assert "secret-A-cred" not in raw                     # 상대 자격 비밀은 DPAPI 로 감쌌다


def test_corrupt_state_fails_closed(two):
    a, b, _ = two
    _pair(a, b)
    b.set_allow(SID, True)
    b.grant_contact(a.host_id(), SID)
    (b.base / "state.json").write_text("{깨짐", encoding="utf-8")
    assert b.list_pairs() == []
    assert b.key_for(a.host_id(), "ask") is None
    assert b.check_ask(a.host_id(), SID) == "paused"          # 깨지면 끄기 스위치가 켜진 것처럼
    with pytest.raises(POL.PolicyCorrupt):
        b.set_allow(SID, True)
    assert (b.base / "state.json").read_text(encoding="utf-8") == "{깨짐"   # 덮어쓰지 않는다


def test_host_id_is_stable(tmp_path):
    p = POL.Policy(tmp_path / "X", devices=FakeDevices(), cp=FakeCp("X"))
    h = p.host_id()
    assert P.is_host_id(h)
    assert POL.Policy(tmp_path / "X", devices=FakeDevices(), cp=FakeCp("X")).host_id() == h
    assert json.loads((tmp_path / "X" / "identity.json").read_text(encoding="utf-8"))["host_id"] == h


def test_audit_has_no_bodies(two):
    a, b, _ = two
    _pair(a, b)
    text = json.dumps(b.audit_tail(50), ensure_ascii=False)
    assert "secret-" not in text and "clewpath-pair:" not in text
    assert any(e["event"] == "paired" for e in b.audit_tail(50))


def test_unapproved_pasted_invite_is_dropped(two):
    a, b, clk = two
    inv = b.create_invite(name="노트북", **ROOM_B)
    st = a.stash_accept(b.invite_string(inv["inv"]))
    mtime = (a.base / "state.json").stat().st_mtime_ns
    a.sweep_handshakes()
    assert (a.base / "state.json").stat().st_mtime_ns == mtime          # 바뀐 게 없으면 쓰지 않는다
    clk.t += POL.ACCEPT_KEEP_S + 1
    a.sweep_handshakes()
    with pytest.raises(POL.PolicyError):
        a.take_accept(st["accept_id"])
