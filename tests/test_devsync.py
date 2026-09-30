"""devsync(0.11.0, eng E-D6/E-D13/E-D14/E-D15): CP 상태 일괄 조회 → (device_id,cpub) 재대조 반영,
불명 폐기 재시도, 미확정 새 cpub 정리, 잠금 밖 네트워크, 기동 지연."""
from __future__ import annotations

import threading
import time

import pytest

from session_manager import devices, devsync, cp_client


@pytest.fixture
def cp(monkeypatch):
    monkeypatch.setenv("SM_CP_URL", "https://cp.test")
    st = {"statuses": {}, "revoke_ok": True, "calls": [], "status_none": False, "delay": 0.0}

    def _status(ids):
        st["calls"].append(("status", list(ids)))
        if st["delay"]:
            time.sleep(st["delay"])
        if st["status_none"]:
            return None
        return {k: v for k, v in st["statuses"].items() if k in ids}

    def _revoke(cpub):
        st["calls"].append(("revoke", cpub))
        return st["revoke_ok"]
    monkeypatch.setattr(cp_client, "client_status", _status)
    monkeypatch.setattr(cp_client, "revoke_client_credential", _revoke)
    return st


def test_noop_without_cp(fake_claude_home, monkeypatch):
    monkeypatch.delenv("SM_CP_URL", raising=False)
    devices.add_device("a")
    assert devsync.sync_once() is None


def test_phone_self_revoke_reflected_and_notified(fake_claude_home, cp):
    a = devices.add_device("폰A"); devices.set_client_public_id(a["id"], "cpub_a")
    b = devices.add_device("폰B"); devices.set_client_public_id(b["id"], "cpub_b")
    c = devices.add_device("구형")                                   # cpub 없음 → 조회 대상 아님
    cp["statuses"] = {"cpub_a": {"status": "revoked", "revoked_at": 1700000000}, "cpub_b": {"status": "active"}}
    got = []
    r = devsync.sync_once(notify=lambda did, name: got.append((did, name)))
    assert r["phone_revoked"] == [a["id"]] and r["removed"] == []
    assert cp["calls"] == [("status", ["cpub_a", "cpub_b"])], "cpub 없는 행은 묻지 않는다, 요청 1회"
    rows = {d["id"]: d for d in devices.list_devices()}
    assert rows[a["id"]]["revoked"] and rows[a["id"]]["revoked_by"] == "phone"
    assert not rows[b["id"]]["revoked"] and not rows[c["id"]]["revoked"]
    assert got == [(a["id"], "폰A")]
    assert devices.status()["cp_synced"] and devices.status()["phone_revoked"] == 1
    # 두 번째 동기화: 이미 반영된 행은 다시 알리지 않는다
    got.clear(); r = devsync.sync_once(notify=lambda did, name: got.append(did))
    assert r["phone_revoked"] == [] and got == []


def test_unknown_cpub_untouched_and_status_none_changes_nothing(fake_claude_home, cp):
    a = devices.add_device("a"); devices.set_client_public_id(a["id"], "cpub_a")
    cp["statuses"] = {}                                              # 응답에 없음(남의 것/없음) → 무접촉
    assert devsync.sync_once()["phone_revoked"] == []
    assert not devices.list_devices()[0]["revoked"]
    cp["status_none"] = True                                         # 타임아웃/5xx → 불명 → 아무것도 안 함
    before = devices.status()["cp_synced"]
    assert devsync.sync_once() is None
    assert devices.status()["cp_synced"] == before


def test_cp_pending_revoke_retry_and_clear(fake_claude_home, cp):
    a = devices.add_device("a"); devices.set_client_public_id(a["id"], "cpub_new")
    devices.add_cp_pending(a["id"], "revoke:cpub_old")             # 재발급 때 옛 cpub 폐기가 불명이었다
    devices.add_cp_pending(a["id"], "revoke:cpub_older")
    cp["statuses"] = {"cpub_new": {"status": "active"}, "cpub_old": {"status": "revoked"}, "cpub_older": {"status": "active"}}
    r = devsync.sync_once()
    assert ("status", ["cpub_new", "cpub_old", "cpub_older"]) == cp["calls"][0]
    assert r["pending_cleared"] == ["cpub_old"], "CP 가 이미 revoked 면 그냥 정리"
    assert r["pending_revoked"] == ["cpub_older"], "아직 active 면 재폐기"
    assert ("revoke", "cpub_older") in cp["calls"]
    assert not devices.list_devices()[0]["cp_pending"]
    # 재폐기가 또 실패하면 남겨 두고 다음 주기
    devices.add_cp_pending(a["id"], "revoke:cpub_x"); cp["statuses"]["cpub_x"] = {"status": "active"}; cp["revoke_ok"] = False
    devsync.sync_once()
    assert devices.list_devices()[0]["cp_pending"] is True


def test_pending_cpub_orphan_cleanup(fake_claude_home, cp):
    """재발급 중 확정 전에 Host 가 죽은 흔적(pending_cpub) → 아직 active 면 폐기, 없거나 revoked 면 정리."""
    a = devices.add_device("a"); devices.set_client_public_id(a["id"], "cpub_cur")
    devices.set_pending_cpub(a["id"], "cpub_orphan")
    cp["statuses"] = {"cpub_cur": {"status": "active"}, "cpub_orphan": {"status": "active"}}
    r = devsync.sync_once()
    assert r["pending_revoked"] == ["cpub_orphan"] and ("revoke", "cpub_orphan") in cp["calls"]
    assert devices.sync_snapshot()[0]["pending_cpub"] is None
    assert devices.get_client_public_id(a["id"]) == "cpub_cur", "현재 cpub 은 무접촉"


def test_delete_pending_row_removed_after_confirmation(fake_claude_home, cp):
    a = devices.add_device("a"); devices.set_client_public_id(a["id"], "cpub_a")
    devices.mark_revoked(a["id"], by="pc"); devices.set_delete_pending(a["id"]); devices.add_cp_pending(a["id"], "revoke:cpub_a")
    cp["statuses"] = {"cpub_a": {"status": "active"}}; cp["revoke_ok"] = False
    r = devsync.sync_once()
    assert r["removed"] == [] and devices.list_devices()[0]["delete_pending"] is True, "확인 전엔 행 유지(유령 방지)"
    cp["revoke_ok"] = True
    r = devsync.sync_once()
    assert r["removed"] == [a["id"]] and devices.list_devices() == []
    # CP 쪽에서 이미 revoked 로 보이면 바로 제거
    b = devices.add_device("b"); devices.set_client_public_id(b["id"], "cpub_b")
    devices.mark_revoked(b["id"]); devices.set_delete_pending(b["id"]); devices.add_cp_pending(b["id"], "revoke:cpub_b")
    cp["statuses"] = {"cpub_b": {"status": "revoked"}}
    assert devsync.sync_once()["removed"] == [b["id"]] and devices.list_devices() == []


def test_network_runs_outside_lock(fake_claude_home, cp):
    """회귀(E-D14): CP 조회가 느려도 등록부 쓰기(touch)는 막히지 않는다."""
    a = devices.add_device("a"); devices.set_client_public_id(a["id"], "cpub_a")
    cp["statuses"] = {"cpub_a": {"status": "active"}}; cp["delay"] = 0.8
    t = threading.Thread(target=devsync.sync_once, daemon=True); t.start()
    time.sleep(0.15)
    t0 = time.time(); devices.touch(a["id"]); dt = time.time() - t0
    t.join(3)
    assert dt < 0.3, f"touch 가 CP 조회 동안 잠금에 막혔다({dt:.2f}s)"


def test_thread_first_delay_and_kick(fake_claude_home, cp, monkeypatch):
    calls = []
    monkeypatch.setattr(devsync, "sync_once", lambda notify=None: calls.append(time.time()))
    monkeypatch.setattr(devsync, "MIN_GAP_S", 0.0)
    devsync._last_run = 0.0
    th = devsync.start(first_delay=0.3, interval=10.0)
    time.sleep(0.1); assert calls == [], "기동 직후엔 돌지 않는다(헬스체크 창)"
    time.sleep(0.4); assert len(calls) == 1
    devsync.kick(); time.sleep(1.3); assert len(calls) == 2, "kick 이면 주기를 기다리지 않는다"
    devsync.stop(); th.join(3); assert not th.is_alive()
