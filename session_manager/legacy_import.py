"""옛 저장 파일 → 상태 DB (설계 data-store.md 확정 변경 1·5 · O1). **영구 유지** — 지우지 않는다.

업데이터는 중간 판을 거치지 않고 최신판으로 바로 간다(updater.py is_newer). 그래서 어느 판이든 옛 파일을 읽을 줄 알아야 하고,
되돌림 대상(러너가 남긴 updates/backup-<ver> 중 지금보다 낮은 가장 새 판)이 R1 미만이면 옛 파일 거울을 계속 써야 한다.

- JSON 문서(labels·profiles·…)는 store 의 거울 대조가 읽을 때마다 처리한다(처음 보면 가져오기, 거울 뒤 바뀌면 다시 가져오기).
- worklog.db(옛 작업 장부)는 거울을 쓰지 않는다. 되돌아간 옛 판이 거기에 더 쓴 것은 다시 올라올 때 합친다
  (세션별로 옛 쪽이 더 최근에 갱신됐으면 그 세션 행·턴을 옛 쪽으로, 아니면 없는 턴만 더함 — 턴은 append 전용이라 안전).
- 처음 상태 DB 로 옮길 때 옛 파일 사본을 backups/pre-store/ 에 남긴다(team.db 는 store 가 형식 단계 전에 backup API 로).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

from session_manager import config, store

R1_VERSION = "0.35.0"
LEGACY_FILES = ("labels.json", "profiles.json", "hooks-status.json", "monitor_groups.json", "housekeeping.json",
                "models.json", "pty-registry.json", "fork_pending.json", "services.json", "api_usage.json", "worklog.db")


def _kv_get(c, key):
    r = c.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and r[0] is not None else None


def _kv_set(c, key, val) -> None:
    c.execute("INSERT INTO kv(key,value_json,updated) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET"
              " value_json=excluded.value_json, updated=excluded.updated", (key, json.dumps(val), int(time.time())))


def on_open(c: sqlite3.Connection) -> None:
    """쓰기 연결을 연 직후(store 잠금 아래, 트랜잭션 밖). 멱등."""
    _pre_store_copy(c)
    try:
        merge_worklog(c)
    except Exception as e:  # noqa: BLE001  옛 장부를 못 읽어도 새 장부는 돈다
        store.log(f"[legacy] worklog.db 합치기 실패: {e}")


def _pre_store_copy(c: sqlite3.Connection) -> None:
    if _kv_get(c, "legacy.pre_store_copied"):
        return
    d = config.data_dir()
    dest = d / "backups" / "pre-store"
    copied = []
    for n in LEGACY_FILES:
        f = d / n
        if f.is_file():
            try:
                dest.mkdir(parents=True, exist_ok=True)
                if not (dest / n).exists():
                    shutil.copy2(f, dest / n)
                copied.append(n)
            except OSError as e:
                store.log(f"[legacy] 사본 실패 {n}: {e}")
    c.execute("BEGIN IMMEDIATE")
    try:
        _kv_set(c, "legacy.pre_store_copied", {"at": int(time.time()), "files": copied})
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def worklog_db() -> Path:
    return config.data_dir() / "worklog.db"


def _wsig(f: Path) -> str | None:
    parts = []
    for suf in ("", "-wal"):
        p = Path(str(f) + suf)
        try:
            st = p.stat()
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append("-")
    return None if parts[0] == "-" else "|".join(parts)


def merge_worklog(c: sqlite3.Connection) -> dict:
    """옛 worklog.db → worklog_ws·worklog_turns. 옛 파일이 지난번 합친 뒤 바뀌지 않았으면 아무것도 안 함."""
    f = worklog_db()
    sig = _wsig(f)
    if sig is None or _kv_get(c, "legacy.worklog_sig") == sig:
        return {"merged": 0}
    src = sqlite3.connect(f.as_uri() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    taken = added = 0
    try:
        try:
            ws = src.execute("SELECT * FROM ws").fetchall()
        except sqlite3.OperationalError:
            ws = []
        c.execute("BEGIN IMMEDIATE")
        try:
            for r in ws:
                sid = r["session_id"]
                ours = c.execute("SELECT updated FROM worklog_ws WHERE session_id=?", (sid,)).fetchone()
                turns = src.execute("SELECT n,ts,request,result,result_ts FROM turns WHERE session_id=?", (sid,)).fetchall()
                if ours is None or (r["updated"] or 0) > (ours["updated"] or 0):
                    c.execute("INSERT OR REPLACE INTO worklog_ws(pc_id,session_id,path,cwd,title,off,head,sig,n,last_ts,gone_at,"
                              "updated) VALUES('local',?,?,?,?,?,?,?,?,?,?,?)",
                              (sid, r["path"], r["cwd"], r["title"], r["off"], r["head"], r["sig"], r["n"], r["last_ts"],
                               r["gone_at"], r["updated"]))
                    for t in turns:
                        c.execute("INSERT OR REPLACE INTO worklog_turns(pc_id,session_id,n,ts,request,result,result_ts)"
                                  " VALUES('local',?,?,?,?,?,?)", (sid, t["n"], t["ts"], t["request"], t["result"], t["result_ts"]))
                    taken += 1
                else:
                    for t in turns:
                        cur = c.execute("INSERT OR IGNORE INTO worklog_turns(pc_id,session_id,n,ts,request,result,result_ts)"
                                        " VALUES('local',?,?,?,?,?,?)",
                                        (sid, t["n"], t["ts"], t["request"], t["result"], t["result_ts"]))
                        added += cur.rowcount
            _kv_set(c, "legacy.worklog_sig", sig)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    finally:
        src.close()
    if taken or added:
        store.log(f"[legacy] worklog.db 합침 - 세션 {taken}개 옛 쪽으로, 턴 {added}건 더함")
    return {"merged": taken, "turns_added": added}


# ---------------------------------------------------------------- 되돌림 대상(O1)
def rollback_target() -> str | None:
    """러너가 남긴 updates/backup-<ver> 중 지금 판보다 낮은 가장 새 판(updater.prune_work_dir 의 rollback 규칙과 같음)."""
    from session_manager import updater
    wd = config.data_dir() / "updates"
    if not wd.is_dir():
        return None
    cur = updater._ver_tuple(updater.current_version())
    best = None
    for p in wd.iterdir():
        name = p.name
        if not name.startswith("backup-") or "-failed-" in name or ".tmp" in name:
            continue
        v = name[len("backup-"):]
        t = updater._ver_tuple(v)
        if t < cur and (best is None or t > best[0]):
            best = (t, v)
    return best[1] if best else None


def mirror_needed() -> bool:
    """옛 JSON 거울을 써야 하나. R1(이 판)은 늘 쓴다. R2(TODOS 데이터 저장 정리 T10)가 생기면:
    되돌림 대상이 없거나 R1 미만이면 계속 쓴다(옛 판이 옛 파일만 읽으므로)."""
    return True


def register_all() -> bool:
    """거울 문서를 쓰는 모듈을 모두 불러 문서를 등록한다(기동 때 대조 전에). 몇몇 모듈은 처음 쓸 때 등록하므로 여기서 깨운다."""
    from session_manager import hooks, housekeeping, session_meta, lifecycle, mongroups, policy, profiles, services, webterm  # noqa: F401
    hooks._doc()
    housekeeping._docs()
    lifecycle._fork_doc()
    policy._usage_doc()
    webterm._registry_doc()
    return True
