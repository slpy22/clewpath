"""devsync — 기기 자격의 CP 상태를 주기적으로 맞춘다(0.11.0, 설계 B-4 / eng E-D6·E-D13·E-D14·E-D15).

하는 일(한 번의 sync_once):
  1. 등록부 스냅샷(잠금 밖) → CP `POST /client/status` 1회(≤200, 네트워크는 **잠금 밖**).
  2. 응답을 (device_id, cpub) 재대조로 반영: 폰이 self-revoke 한 행 → revoked(by=phone) 📵,
     delete_pending 행이 폐기 확인되면 제거. 응답에 없는 cpub 은 '내 것 아님/없음' 구별 불가 → 무접촉.
  3. cp_pending("revoke:<cpub>") — 폐기 결과가 불명이던 항목: status 가 revoked 면 정리, 아니면 재폐기 시도.
  4. pending_cpub — 재발급 중 확정 못 한 새 cpub: 아직 active 면 폐기(미반영 신규 cpub 정리).
  5. meta.cp_synced 기록. 폰 self-revoke 를 새로 알게 되면 PC 웹푸시(E-3 ②).

스레드 규칙(monwatch 패턴): 기동 +60초 첫 실행(헬스체크 창 무접촉), 5분 주기, kick() 으로 깨우면 즉시(최소 30초 간격),
CP 미설정이면 아무것도 안 한다. 실패가 Host 를 죽이면 안 된다.
"""
from __future__ import annotations

import threading
import time

FIRST_DELAY_S = 60.0
INTERVAL_S = 300.0
MIN_GAP_S = 30.0

_thread: threading.Thread | None = None
_stop: threading.Event | None = None
_wake: threading.Event | None = None
_last_run: float = 0.0


def _log(msg: str) -> None:
    print(f"[devsync] {msg}", flush=True)


def sync_once(notify=None) -> dict | None:
    """한 번 동기화. CP 미설정/실패면 None(무접촉). notify(did, name) 은 폰 self-revoke 를 새로 알게 됐을 때."""
    from session_manager import cp_client, devices

    if not cp_client.cp_url():
        return None
    rows = devices.sync_snapshot()                       # 잠금 밖으로 나온 사본
    ids: list[str] = []
    for r in rows:
        for c in (r["cpub"], r["pending_cpub"]):
            if c and c not in ids:
                ids.append(c)
        for item in r["cp_pending"]:
            c = item.split(":", 1)[1] if item.startswith("revoke:") else ""
            if c and c not in ids:
                ids.append(c)
    statuses = cp_client.client_status(ids) if ids else {}
    if statuses is None:
        return None                                       # 불명 — 아무것도 바꾸지 않는다
    result = {"phone_revoked": [], "removed": [], "pending_cleared": [], "pending_revoked": []}

    # 2) 재대조 반영(잠금 안, 파일 I/O 만)
    ap = devices.apply_cp_status(statuses)
    result["phone_revoked"] = ap["phone_revoked"]
    result["removed"] = ap["removed"]
    for did in ap["phone_revoked"]:
        devices.audit("self_revoke", did, via="cp_sync")

    # 3) 불명이던 폐기 재시도 / 4) 미확정 새 cpub 정리 — 네트워크는 잠금 밖
    for r in rows:
        did = r["id"]
        if did in result["removed"]:
            continue
        for item in r["cp_pending"]:
            if not item.startswith("revoke:"):
                continue
            cpub = item[len("revoke:"):]
            st = statuses.get(cpub)
            if st and st.get("status") == "revoked":
                devices.pop_cp_pending(did, item); result["pending_cleared"].append(cpub)
            elif cp_client.revoke_client_credential(cpub):
                devices.pop_cp_pending(did, item); result["pending_revoked"].append(cpub)
            # 응답에 없거나(남의 것/없음) 재폐기도 실패 → 다음 주기
        pc = r["pending_cpub"]
        if pc and pc != r["cpub"]:
            st = statuses.get(pc)
            if st is None or st.get("status") == "revoked":
                devices.set_pending_cpub(did, None)       # 없거나 이미 폐기 → 정리
            elif cp_client.revoke_client_credential(pc):
                devices.set_pending_cpub(did, None); result["pending_revoked"].append(pc)
    # delete_pending 행 중 cp_pending 이 모두 비면 제거(폐기 확인 완료)
    for r in devices.sync_snapshot():
        if r["delete_pending"] and not r["cp_pending"]:
            if devices.remove(r["id"]):
                result["removed"].append(r["id"])
    devices.set_cp_synced()
    if notify and result["phone_revoked"]:
        names = {r["id"]: r["name"] for r in rows}
        for did in result["phone_revoked"]:
            try:
                notify(did, names.get(did) or did[:8])
            except Exception as e:  # noqa: BLE001
                _log(f"notify 실패: {type(e).__name__}")
    return result


def _push_phone_revoked(did: str, name: str) -> None:
    """E-3 ②: 폰이 스스로 페어링을 해제함 → PC 웹푸시 1회."""
    try:
        from session_manager import push
        push.send("device", did, f"📱 {name} 페어링을 해제함", "폰에서 이 PC 의 페어링을 해제했습니다. 📱 기기 목록에서 정리하세요.")
    except Exception as e:  # noqa: BLE001
        _log(f"push 실패: {type(e).__name__}")


def kick() -> None:
    """📱 목록을 열 때 등: 다음 틱을 앞당긴다(최소 간격 30초는 run 루프가 지킨다)."""
    if _wake is not None:
        _wake.set()


def start(first_delay: float = FIRST_DELAY_S, interval: float = INTERVAL_S) -> threading.Thread:
    global _thread, _stop, _wake
    if _thread is not None and _thread.is_alive():
        return _thread
    stop_ev = threading.Event()
    wake_ev = threading.Event()

    def run():
        global _last_run
        if stop_ev.wait(first_delay):                 # 기동 직후 헬스체크 창은 건드리지 않는다(E-D14)
            return
        while not stop_ev.is_set():
            gap = time.time() - _last_run
            if gap < MIN_GAP_S:
                if stop_ev.wait(MIN_GAP_S - gap):
                    return
            try:
                sync_once(notify=_push_phone_revoked)
            except Exception as e:  # noqa: BLE001  동기화 실패가 Host 를 죽이면 안 된다
                _log(f"{type(e).__name__}: {e}")
            _last_run = time.time()
            wake_ev.clear()
            # 다음 주기까지 자거나, kick() 이면 일찍 깬다
            t0 = time.time()
            while not stop_ev.is_set() and time.time() - t0 < interval:
                if wake_ev.wait(1.0):
                    break

    _stop = stop_ev
    _wake = wake_ev
    _thread = threading.Thread(target=run, name="devsync", daemon=True)
    _thread.start()
    return _thread


def stop() -> None:
    if _stop is not None:
        _stop.set()
    if _wake is not None:
        _wake.set()
