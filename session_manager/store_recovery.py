"""상태 DB 손상 복구(설계 data-store.md 확정 변경 6 · O5 · O6).

열 때 깨져 있으면(열기 실패 또는 quick_check ≠ ok):
  최근 백업(backups/state-*.db)이 있으면 → 깨진 파일을 backups/corrupt-<시각>/ 로 옮기고 백업을 제자리에 복사
    → 수집 위치를 0 으로(팀 수집 ingest·작업 장부 worklog_ws) — 백업 뒤 이벤트를 다시 읽는다(idem_key·(session_id,n) 로 중복 없음)
    → 검색 색인(texts)을 events 에서 다시 만든다(수집기는 이미 있는 이벤트의 색인을 넣지 않으므로 재수집으로는 안 생긴다)
    → 사고 기록 + 알림(최대 하루치 손실 — 백업은 하루 1회).
  백업이 없으면 → 그대로 오류(자동으로 지우거나 새로 만들지 않는다 — 사장님 데이터를 덮지 않는다). 호출자는 503.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time

from session_manager import store
from pathlib import Path
from typing import Callable

# 검색 색인에 들어가는 이벤트 종류와 본문 자리(teamlog._ev 의 body_for_search 와 같은 값)
SEARCH_KINDS = {"msg": "body", "msg_in": "body", "human_input": "text", "prompt_in": "text"}


def _check(c: sqlite3.Connection) -> str:
    try:
        return str(c.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.DatabaseError as e:
        return f"error: {e}"


def open_checked(p: Path, opener: Callable[[Path], sqlite3.Connection]) -> sqlite3.Connection:
    """opener(p) 로 열고 quick_check. 깨졌으면 백업으로 되살려 다시 연다. 되살릴 수 없으면 sqlite3.DatabaseError."""
    try:
        c = opener(p)
        res = _check(c)
        if res == "ok":
            return c
        c.close()
        why = res
    except sqlite3.DatabaseError as e:
        why = str(e)
        if not p.exists():
            raise
    from session_manager import store
    bk = store.latest_backup()
    if bk is None:
        raise sqlite3.DatabaseError(f"상태 DB 손상, 백업 없음: {why}")
    moved = _move_aside(p)
    shutil.copy2(bk, p)
    c = opener(p)
    if _check(c) != "ok":
        c.close()
        raise sqlite3.DatabaseError(f"백업도 깨짐: {bk.name}")
    reset_offsets(c)
    n = rebuild_search(c)
    store.incident("restored_from_backup", reason=why[:200], backup=bk.name, moved=str(moved), search_rows=n)
    return c


def _move_aside(p: Path) -> Path:
    d = p.parent / "backups" / f"corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        f = Path(str(p) + suf)
        if f.exists():
            try:
                shutil.move(str(f), str(d / f.name))
            except OSError:
                pass
    return d


def reset_offsets(c: sqlite3.Connection) -> None:
    """O6: 백업 시점 상태 + 지금 시점 읽기 위치가 섞이지 않게 — 처음부터 다시 읽는다."""
    c.execute("BEGIN IMMEDIATE")
    try:
        for sql in ("UPDATE ingest SET offset=0, gen=gen+1, head_sig=NULL, tail_sig=NULL",
                    "UPDATE worklog_ws SET off=0, head=NULL, sig=NULL"):
            try:
                c.execute(sql)
            except sqlite3.OperationalError:
                pass                                    # 표가 없는 옛 형식
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def rebuild_search(c: sqlite3.Connection) -> int:
    """O5: 검색 색인(texts FTS)을 events 에서 다시 만든다. 색인이 없는 SQLite(FTS5 없음)면 0."""
    try:
        c.execute("SELECT 1 FROM texts LIMIT 1")
    except sqlite3.OperationalError:
        return 0
    n = 0
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("DELETE FROM texts")
        q = ",".join("?" * len(SEARCH_KINDS))
        for r in c.execute(f"SELECT id, team_id, kind, payload_json FROM events WHERE kind IN ({q}) ORDER BY id",
                           tuple(SEARCH_KINDS)).fetchall():
            try:
                body = (json.loads(r[3] or "{}") or {}).get(SEARCH_KINDS[r[2]])
            except Exception:  # noqa: BLE001
                body = None
            if body:
                c.execute("INSERT INTO texts(body,kind,team_id,event_id) VALUES(?,?,?,?)", (body, r[2], r[1], r[0]))
                n += 1
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return n


# ---------------------------------------------------------------- Host 기동·종료(server._lifespan)
import threading as _threading

_bg_stop: "_threading.Event | None" = None
BACKUP_FIRST_DELAY_S = 120.0
BACKUP_CHECK_S = 3600.0


def start_background() -> None:
    """상태 DB 열기·문서 대조(스레드) + 하루 1회 백업 감시."""
    global _bg_stop
    ev = _threading.Event()
    _bg_stop = ev

    def run():
        from session_manager import legacy_import, store
        try:
            store.writer()
            res = store.reconcile_all() if legacy_import.register_all() else {}
            bad = {k: v for k, v in res.items() if v != "ok"}
            if bad:
                store.log(f"[store] 문서 대조 실패: {bad}")
        except Exception as e:  # noqa: BLE001  못 열어도 Host 는 산다(각 요청이 503)
            store.log(f"[store] 상태 DB 열기 실패: {e}")
        if ev.wait(BACKUP_FIRST_DELAY_S):
            return
        while True:
            try:
                b = store.latest_backup()
                if b is None or not b.name.startswith("state-" + time.strftime("%Y%m%d")):
                    p = store.backup_now()
                    store.log(f"[store] 하루 백업 {p.name if p else '-'}")
            except Exception as e:  # noqa: BLE001
                store.log(f"[store] 백업 실패: {e}")
                try:
                    store.incident("backup_failed", error=str(e)[:200])
                except Exception:  # noqa: BLE001
                    pass
            if ev.wait(BACKUP_CHECK_S):
                return

    _threading.Thread(target=run, name="store-open", daemon=True).start()


def stop_background() -> None:
    from session_manager import hooks, store
    if _bg_stop is not None:
        _bg_stop.set()
    try:
        hooks.flush(blocking=True)
    except Exception as e:  # noqa: BLE001
        store.log(f"[store] 종료 때 훅 상태 쓰기 실패: {e}")
    try:
        store.close()
    except Exception:  # noqa: BLE001
        pass
