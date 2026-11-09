"""상태 DB — ClewPath 가 만든 정보(세션 이름·라벨·정책·훅 상태·등록부·팀 장부·작업 장부)를 한 SQLite 에 둔다.

설계: docs/designs/data-store.md('확정 설계 변경'이 본문보다 우선). 요점:

- 파일 = 데이터 폴더의 team.db 를 키운다(형식 번호 6~). R2 에서 clewpath.db 로 이름만 바꾼다 — 그래서 이 모듈은 두 이름을
  다 연다(clewpath.db 가 있으면 그것이 정본, 둘 다 있으면 사고 기록 + clewpath.db).
- **쓰기 연결은 하나**(D3): 쓰기·체크포인트는 모두 이 프로세스가 Host 수명 동안 열어 두는 연결 하나로 한다. SQLite 3.51.3 전의
  WAL 재설정 결함은 "두 연결이 같은 순간 쓰기·체크포인트" 일 때만 생긴다(sqlite.org wal.html §11, 2026-11-05 확인). 읽기
  연결은 따로 열어 쓰기 중에도 막히지 않는다. 쓰기 연결이 늘 열려 있어 읽기 연결이 '마지막으로 닫히며 체크포인트' 하는 일도 없다.
  다른 프로세스(MCP 서버·브로커·커넥터)는 이 DB 를 열지 않고 Host API 만 부른다.
- 이 PC 의 행은 pc_id='local'(D4). 진짜 PC id(pclink host_id)는 pcs 표 한 칸에만 둔다.
- **거울 문서**(D5): 옛 JSON 파일(labels.json 등)은 옛 판이 되돌림 뒤 읽을 수 있게 같은 모양으로 계속 쓴다(R1).
  쓰기 순서 = ① 표 + '거울 미완' 표식 커밋 → ② JSON 원자적 교체 → ③ 표식 지움. 읽을 때마다 파일 상태(크기·수정 시각)를 보고,
  바뀌었으면 내용 해시로 대조한다(옛 판·사람이 고친 파일은 가져오고, 애매하면 양쪽을 보존하고 알린다).
- 표 모양은 그 데이터를 쓰는 모듈이 안다. 여기는 연결·트랜잭션·형식 단계·거울·백업만 안다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from session_manager import config

LOCAL_PC = "local"
SCHEMA_VERSION = 7
_lock = threading.RLock()          # 쓰기 직렬화(프로세스 안). team._lock 은 이것의 별명
_w: sqlite3.Connection | None = None
_w_path: str | None = None
_depth = 0                          # 지금 열린 트랜잭션 깊이(잠금을 쥔 스레드만 바꾼다)
_after: list[Callable[[], None]] = []   # 가장 바깥 트랜잭션이 커밋된 뒤 할 일(거울 쓰기)
_logged_version = False


def log(msg: str) -> None:
    """host.log 한 줄. 콘솔 인코딩(cp949 등)이 못 찍는 글자가 있어도 예외를 내지 않는다(로그 실패가 저장을 막지 않게)."""
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        try:
            import sys
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(msg.encode(enc, "replace").decode(enc), flush=True)
        except Exception:  # noqa: BLE001
            pass


class StoreUnavailable(Exception):
    """상태 DB 를 열 수 없음(손상·잠김). 호출자는 503 으로 내보내고 Host 는 계속 산다."""


# ---------------------------------------------------------------- 형식 단계
# v6(데이터 저장 정리 R1): PC 표 · 세션 표에 pc_id · 거울 문서 · 세션 덧붙임 정보 · 운영 상태 · 작업 장부.
# 옛 Host(형식 5)는 형식 번호가 높으면 아무것도 안 하고 모르는 표는 무시한다(열 이름 없는 INSERT 없음 — 2026-11-05 확인).
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS pcs(pc_id TEXT PRIMARY KEY, host_id TEXT, name TEXT, is_local INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER, last_seen INTEGER);
ALTER TABLE agent_sessions ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE ingest ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE usage ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE addresses ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE pending_sends ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
ALTER TABLE web_prompts ADD COLUMN pc_id TEXT NOT NULL DEFAULT 'local';
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value_json TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS doc_meta(name TEXT PRIMARY KEY, head_json TEXT, hash TEXT, pending_hash TEXT, prev_hash TEXT,
  updated INTEGER);
CREATE TABLE IF NOT EXISTS session_meta(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, ord INTEGER NOT NULL,
  name TEXT, keep INTEGER, model TEXT, body_json TEXT NOT NULL, PRIMARY KEY(pc_id, session_id));
CREATE TABLE IF NOT EXISTS folder_meta(pc_id TEXT NOT NULL DEFAULT 'local', folder_key TEXT NOT NULL, ord INTEGER NOT NULL,
  body_json TEXT NOT NULL, PRIMARY KEY(pc_id, folder_key));
CREATE TABLE IF NOT EXISTS session_policy(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, ord INTEGER NOT NULL,
  body_json TEXT NOT NULL, PRIMARY KEY(pc_id, session_id));
CREATE TABLE IF NOT EXISTS session_runtime(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, ord INTEGER NOT NULL,
  phase TEXT, active_at INTEGER, gone_at INTEGER, body_json TEXT NOT NULL, PRIMARY KEY(pc_id, session_id));
CREATE TABLE IF NOT EXISTS pty_registry(pid TEXT PRIMARY KEY, ord INTEGER NOT NULL, pc_id TEXT NOT NULL DEFAULT 'local',
  session_id TEXT, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fork_pending(fork_id TEXT PRIMARY KEY, ord INTEGER NOT NULL, pc_id TEXT NOT NULL DEFAULT 'local',
  body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(name TEXT PRIMARY KEY, ord INTEGER NOT NULL, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hidden_sessions(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, ord INTEGER NOT NULL,
  body_json TEXT NOT NULL, PRIMARY KEY(pc_id, session_id));
CREATE TABLE IF NOT EXISTS services(id TEXT PRIMARY KEY, ord INTEGER NOT NULL, status TEXT, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS service_requests(id TEXT PRIMARY KEY, ord INTEGER NOT NULL, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_usage(caller TEXT NOT NULL, day TEXT NOT NULL, ord INTEGER NOT NULL, calls INTEGER, usd REAL,
  body_json TEXT NOT NULL, PRIMARY KEY(caller, day));
CREATE TABLE IF NOT EXISTS legacy_monitor_groups(id TEXT PRIMARY KEY, ord INTEGER NOT NULL, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worklog_ws(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, path TEXT, cwd TEXT,
  title TEXT, off INTEGER NOT NULL DEFAULT 0, head TEXT, sig TEXT, n INTEGER NOT NULL DEFAULT 0, last_ts INTEGER,
  gone_at INTEGER, updated INTEGER, PRIMARY KEY(session_id));
CREATE TABLE IF NOT EXISTS worklog_turns(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL, n INTEGER NOT NULL,
  ts INTEGER, request TEXT, result TEXT, result_ts INTEGER, PRIMARY KEY(session_id, n));
CREATE INDEX IF NOT EXISTS ix_worklog_turns_ts ON worklog_turns(ts);
"""
# v7(프롬프트로 세션 다루기, 2026-11-07): 새 세션의 생성 설정 — 다시 열 때 같은 권한·도구로 띄운다(docs/designs/session-control.md 4-1).
# 라벨(사람이 지우면 레코드가 사라짐)·API 프로필(Claude 세션이 MCP 로 바꿀 수 있음)과 수명을 나눠 Host 만 쓴다.
_SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS launch_settings(pc_id TEXT NOT NULL DEFAULT 'local', session_id TEXT NOT NULL,
  permission_mode TEXT, effort TEXT, model TEXT, body_json TEXT NOT NULL, source TEXT, created INTEGER,
  PRIMARY KEY(pc_id, session_id));
"""
# 세션 표 기본키는 지금 session_id 만으로도 유일하다(이 PC 행뿐). 허브가 다른 PC 행을 들여올 때 (pc_id, session_id) 로
# 바꾼다(TODOS 데이터 저장 정리 T3). session_meta 등 새 표는 처음부터 (pc_id, session_id).


def _migrate(c: sqlite3.Connection) -> None:
    """형식 단계. team.db v1~v5 단계는 team 모듈의 SQL 을 그대로 쓴다(옛 Host 와 같은 결과)."""
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v >= SCHEMA_VERSION:
        return
    from session_manager import team
    if 1 <= v < 6:
        _pre_store_backup(c, v)
    steps: list[tuple[int, str]] = [
        (1, team._SCHEMA), (2, team._SCHEMA_V2 + team._fts_sql()), (3, team._SCHEMA_V3),
        (4, team._SCHEMA_V4), (5, team._SCHEMA_V5), (6, _SCHEMA_V6), (7, _SCHEMA_V7),
    ]
    for ver, sql in steps:
        if v < ver:
            # 한 단계 = 한 트랜잭션(도중에 죽으면 그 단계는 없던 일 → 다음 기동에 다시). executescript 는 앞서 열린
            # 트랜잭션을 커밋하므로 BEGIN 을 SQL 안에 넣는다.
            c.executescript("BEGIN IMMEDIATE;\n" + sql + f"\nPRAGMA user_version={ver};\nCOMMIT;")
            v = ver


def _pre_store_backup(c: sqlite3.Connection, v: int) -> None:
    """형식 6(R1)으로 올리기 직전 team.db 사본(backup API — WAL 내용까지 일관되게). 한 번만."""
    try:
        d = config.data_dir() / "backups" / "pre-store"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"team-v{v}.db"
        if dest.exists():
            return
        tmp = d / f"team-v{v}.db.tmp"
        out = sqlite3.connect(str(tmp))
        try:
            c.backup(out)
        finally:
            out.close()
        os.replace(tmp, dest)
    except Exception as e:  # noqa: BLE001  사본 실패가 기동을 막지는 않는다(host.log)
        log(f"[store] 옮기기 전 사본 실패: {e}")


# ---------------------------------------------------------------- 파일 이름(D6)
def legacy_name() -> Path:
    return config.data_dir() / "team.db"


def new_name() -> Path:
    return config.data_dir() / "clewpath.db"


def db_path() -> Path:
    """정본 상태 DB 파일. R2 가 이름을 바꾸기 전에는 team.db."""
    n = new_name()
    return n if n.exists() else legacy_name()


# ---------------------------------------------------------------- 연결
def _open(p: Path) -> sqlite3.Connection:
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=5, isolation_level=None, check_same_thread=False)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _migrate(c)
    except BaseException:
        c.close()
        raise
    return c


def _log_version_once() -> None:
    global _logged_version
    if _logged_version:
        return
    _logged_version = True
    log(f"[store] SQLite {sqlite3.sqlite_version} · 상태 DB {db_path().name} · 쓰기 연결 1개(WAL)")


def writer() -> sqlite3.Connection:
    """이 프로세스의 쓰기 연결(지금 데이터 폴더 기준). 잠금 없이 부르면 읽기 전용으로만 쓴다."""
    global _w, _w_path
    p = db_path()
    sp = str(p)
    w = _w
    if w is not None and _w_path == sp:
        return w
    with _lock:
        if _w is not None and _w_path == sp:
            return _w
        if _depth:
            raise StoreUnavailable("데이터 폴더가 트랜잭션 도중에 바뀜")
        _close_writer()
        try:
            from session_manager import store_recovery
            c = store_recovery.open_checked(p, _open)
        except sqlite3.DatabaseError as e:
            raise StoreUnavailable(str(e)) from e
        _w, _w_path = c, sp
        _log_version_once()
        try:
            _after_open(c)
        except Exception as e:  # noqa: BLE001  PC 표·가져오기 실패가 DB 사용을 막지 않는다
            log(f"[store] 기동 후 정리 실패: {e}")
        return c


def _after_open(c: sqlite3.Connection) -> None:
    ensure_local_pc(c)
    if new_name().exists() and legacy_name().exists() and legacy_name().stat().st_size > 0:
        incident("both_db_names", using=new_name().name)       # D6: 둘 다 있으면 clewpath.db 가 정본
    from session_manager import legacy_import
    legacy_import.on_open(c)


def _close_writer() -> None:
    global _w, _w_path
    if _w is not None:
        try:
            _w.close()
        except Exception:  # noqa: BLE001
            pass
    _w, _w_path = None, None


def close() -> None:
    """쓰기 연결을 닫는다(Host 종료·시험 정리). 닫을 때 SQLite 가 마지막 체크포인트를 한다 — 쓰기 연결이 한다."""
    with _lock:
        _close_writer()
        _sig.clear()


def reader() -> sqlite3.Connection:
    """읽기 연결(부른 쪽이 닫는다). 쓰기 연결을 먼저 열어 형식 단계를 끝내고, 읽기 연결이 마지막 연결이 되지 않게 한다."""
    writer()
    try:
        c = sqlite3.connect(str(db_path()), timeout=5, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.DatabaseError as e:
        raise StoreUnavailable(str(e)) from e


class tx:
    """쓰기 트랜잭션(프로세스 안 잠금 + BEGIN IMMEDIATE, 쓰기 연결 하나). 안에서 다시 열면 SAVEPOINT 로 겹친다."""

    def __init__(self, blocking: bool = True):
        self.blocking = blocking
        self.ok = False

    def __enter__(self) -> sqlite3.Connection:
        global _depth
        if not _lock.acquire(blocking=self.blocking):
            raise Busy()
        try:
            c = writer()
            if _depth == 0:
                c.execute("BEGIN IMMEDIATE")
            else:
                c.execute(f"SAVEPOINT sp{_depth}")
            _depth += 1
        except BaseException:
            _lock.release()
            raise
        self.c = c
        return c

    def __exit__(self, et, ev, tb):
        global _depth
        todo: list[Callable[[], None]] = []
        try:
            _depth -= 1
            if _depth == 0:
                try:
                    self.c.execute("ROLLBACK" if et else "COMMIT")
                except BaseException:
                    try:
                        self.c.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001
                        pass
                    _after.clear()
                    raise
                todo = list(_after) if et is None else []
                _after.clear()
            else:
                n = _depth
                if et:
                    self.c.execute(f"ROLLBACK TO sp{n}")
                self.c.execute(f"RELEASE sp{n}")
            for fn in todo:                 # 잠금을 쥔 채 — 거울 쓰기 순서가 커밋 순서와 같다
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    log(f"[store] 커밋 뒤 작업 실패: {e}")
        finally:
            _lock.release()
        return False


class Busy(Exception):
    """blocking=False 트랜잭션이 잠금을 못 얻음(다른 쓰기가 진행 중)."""


def after_commit(fn: Callable[[], None]) -> None:
    """지금 트랜잭션이 커밋된 뒤 fn 을 부른다(트랜잭션 밖이면 바로)."""
    with _lock:
        if _depth:
            _after.append(fn)
            return
    fn()


# ---------------------------------------------------------------- PC 표(D4)
def _host_id() -> str | None:
    """pclink 의 host_id(읽기만 — 없으면 만들지 않는다. pclink 가 처음 쓸 때 만든다)."""
    try:
        d = json.loads((config.data_dir() / "pclink" / "identity.json").read_text(encoding="utf-8"))
        hid = d.get("host_id")
        return hid if isinstance(hid, str) and hid else None
    except Exception:  # noqa: BLE001
        return None


def ensure_local_pc(c: sqlite3.Connection | None = None) -> None:
    """pcs 표의 이 PC 행('local')에 지금 host_id 를 적는다. host_id 가 바뀌어도 이 칸만 바뀐다(세션 행은 그대로)."""
    import socket
    hid = _host_id()
    now = int(time.time())

    def _do(cc):
        cc.execute("INSERT INTO pcs(pc_id,host_id,name,is_local,first_seen,last_seen) VALUES(?,?,?,1,?,?)"
                   " ON CONFLICT(pc_id) DO UPDATE SET host_id=COALESCE(excluded.host_id, pcs.host_id),"
                   " name=excluded.name, last_seen=excluded.last_seen", (LOCAL_PC, hid, socket.gethostname(), now, now))

    if c is not None:
        with _lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                _do(c)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return
    with tx() as cc:
        _do(cc)


def local_host_id() -> str | None:
    c = reader()
    try:
        r = c.execute("SELECT host_id FROM pcs WHERE pc_id=?", (LOCAL_PC,)).fetchone()
        return r["host_id"] if r else None
    finally:
        c.close()


# ---------------------------------------------------------------- kv(작은 단일 값만)
def kv_get(key: str, default=None):
    c = reader()
    try:
        r = c.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value_json"]) if r and r["value_json"] is not None else default
    finally:
        c.close()


def kv_set(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT INTO kv(key,value_json,updated) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET"
                  " value_json=excluded.value_json, updated=excluded.updated",
                  (key, json.dumps(value, ensure_ascii=False), int(time.time())))


# ---------------------------------------------------------------- 사고 기록·알림
def incident(event: str, **detail) -> None:
    """상태 DB 사고(되돌림 뒤 가져오기·애매한 거울·복구)를 incidents.jsonl·host.log 에 남기고 알림 목록에 올린다."""
    rec = {"at": int(time.time()), "kind": "store", "event": event, **detail}
    log(f"[store] {event} {json.dumps(detail, ensure_ascii=False)[:300]}")
    try:
        from session_manager import jsonl_log
        jsonl_log.append(config.data_dir() / "incidents.jsonl", rec, ts_key=None)
    except Exception:  # noqa: BLE001
        pass
    if event in NOTICE_EVENTS:
        try:
            with tx() as c:
                r = c.execute("SELECT value_json FROM kv WHERE key='store.notices'").fetchone()
                lst = json.loads(r["value_json"]) if r and r["value_json"] else []
                lst = (lst + [rec])[-20:]
                c.execute("INSERT INTO kv(key,value_json,updated) VALUES('store.notices',?,?) ON CONFLICT(key) DO UPDATE"
                          " SET value_json=excluded.value_json, updated=excluded.updated",
                          (json.dumps(lst, ensure_ascii=False), int(time.time())))
        except Exception:  # noqa: BLE001
            pass
        _push_notice(event)


NOTICE_EVENTS = ("mirror_ambiguous", "restored_from_backup", "both_db_names")
_NOTICE_TEXT = {
    "mirror_ambiguous": "업데이트 되돌림 중 저장이 끊겼던 흔적이 있어 두 값을 모두 보관했습니다. 설정에서 옛 파일 값으로 되돌릴 수 있습니다.",
    "restored_from_backup": "상태 DB 가 손상돼 최근 백업으로 되살렸습니다(최대 하루치 변경이 빠졌을 수 있음).",
    "both_db_names": "상태 DB 파일이 두 개(team.db·clewpath.db) 있어 clewpath.db 를 썼습니다.",
}


def _push_notice(event: str) -> None:
    try:
        from session_manager import push
        push.send("store-notice", f"store-{event}", "ClewPath 저장소 알림", _NOTICE_TEXT.get(event, event))
    except Exception:  # noqa: BLE001
        pass


def notices() -> list[dict]:
    return kv_get("store.notices", []) or []


def status() -> dict:
    """설정 화면용: SQLite 판·파일·백업·알림."""
    b = latest_backup()
    kept = sorted(p.name for p in (backup_dir() / "mirror").glob("*.ambiguous.*")) if (backup_dir() / "mirror").is_dir() else []
    return {"sqlite": sqlite3.sqlite_version, "db": db_path().name, "schema": SCHEMA_VERSION,
            "last_backup": b.name if b else None, "notices": notices(), "kept": kept}


def restore_kept(kept: str) -> dict:
    """애매한 거울(D5)에서 보존한 옛 파일 값으로 되돌린다(사장님 선택). kept = backups/mirror 안 파일 이름."""
    d = backup_dir() / "mirror"
    p = d / kept
    if not kept or "/" in kept or "\\" in kept or not p.is_file():
        raise ValueError("kept_not_found")
    doc = next((x for x in _docs.values() if kept.startswith(x.file().name + ".")), None)
    if doc is None:
        raise ValueError("unknown_doc")
    b = p.read_bytes()
    v = _parse_file(doc, b)
    if v is None:
        raise ValueError("kept_unreadable")
    save_doc(doc.name, v)
    incident("mirror_kept_restored", doc=doc.name, kept=kept)
    return {"ok": True, "doc": doc.name}


# ---------------------------------------------------------------- 원자적 파일 쓰기
def write_atomic(path: Path, data: bytes) -> None:
    """같은 폴더의 고유 임시 파일 → os.replace. Windows 에서 다른 스레드가 잠깐 열어 둔 파일이면 몇 번 다시 한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        for i in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.02)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sha(b: bytes | None) -> str | None:
    return None if b is None else hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- 거울 문서(D5)
@dataclass
class Coll:
    """문서 안 모음 하나 ↔ 표 하나. 행 = (열쇠 열…, ord, body_json[, 파생 열…]). body_json 이 원본 그대로라 문서가 정확히 되돌아온다."""
    table: str
    path: str | None                     # 문서 최상위 키(None = 문서 전체가 이 모음)
    keys: tuple[str, ...]                # 열쇠 열 이름(보통 1개)
    kind: str = "dict"                   # dict(열쇠→값) · list(값 목록, 열쇠 = 값의 문자열)
    keyof: Callable[[Any], str] | None = None       # list 모음: 값 → 열쇠(기본 str(값))
    split: Callable[[str], tuple] | None = None     # 열쇠 문자열 → 열쇠 열 값들(열쇠 열이 2개일 때)
    join: Callable[[tuple], str] | None = None
    derive: Callable[[Any], dict] | None = None     # 값 → 파생 열(검색용, 읽을 때는 안 씀)
    pc: bool = False                     # pc_id 열이 있는 표(이 PC = 'local')


@dataclass
class Doc:
    name: str
    file: Callable[[], Path]
    colls: list[Coll]
    empty: Callable[[], Any]
    dump: Callable[[Any], str]
    valid: Callable[[Any], bool] = lambda d: isinstance(d, dict)
    norm: Callable[[Any], Any] = lambda d: d          # 가져온 파일 → 문서(빠진 키 채우기 등)
    mirror: bool = True
    extra: dict = field(default_factory=dict)


_docs: dict[str, Doc] = {}
_sig: dict[tuple[str, str], tuple] = {}    # (DB 경로, 문서) → 마지막으로 본 거울 파일 상태


def register(doc: Doc) -> Doc:
    _docs[doc.name] = doc
    return doc


def docs() -> dict[str, Doc]:
    return dict(_docs)


def _fsig(p: Path):
    try:
        st = p.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _key_cols(cl: Coll, k: str) -> tuple:
    if len(cl.keys) == 1:
        return (k,)
    return tuple(cl.split(k)) if cl.split else tuple(k.split(":", len(cl.keys) - 1))


def _key_str(cl: Coll, vals: tuple) -> str:
    if len(cl.keys) == 1:
        return vals[0]
    return cl.join(vals) if cl.join else ":".join(vals)


def _derived_cols(cl: Coll, v) -> dict:
    if not cl.derive:
        return {}
    try:
        return cl.derive(v) or {}
    except Exception:  # noqa: BLE001
        return {}


def _put_rows(c, cl: Coll, items: list[tuple[str, Any]], replace_all: bool) -> None:
    where = (" WHERE pc_id='local'" if cl.pc else "")
    if replace_all:
        c.execute(f"DELETE FROM {cl.table}{where}")
    base = list(cl.keys) + ["ord", "body_json"] + (["pc_id"] if cl.pc and "pc_id" not in cl.keys else [])
    for i, (k, v) in enumerate(items):
        der = _derived_cols(cl, v)
        cols = base + list(der)
        vals = list(_key_cols(cl, k)) + [i, json.dumps(v, ensure_ascii=False)] \
            + ([LOCAL_PC] if cl.pc and "pc_id" not in cl.keys else []) + list(der.values())
        c.execute(f"INSERT OR REPLACE INTO {cl.table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)


def _get_rows(c, cl: Coll) -> list[tuple[str, Any]]:
    where = (" WHERE pc_id='local'" if cl.pc else "")
    rows = c.execute(f"SELECT {','.join(cl.keys)}, body_json FROM {cl.table}{where} ORDER BY ord, rowid").fetchall()
    return [(_key_str(cl, tuple(r[k] for k in cl.keys)), json.loads(r["body_json"])) for r in rows]


def _rootcoll(doc: Doc) -> bool:
    return len(doc.colls) == 1 and doc.colls[0].path is None


def _items(cl: Coll, src) -> list:
    if cl.kind == "list":
        return [((cl.keyof or str)(v), v) for v in (src or [])] if isinstance(src, list) else []
    return list(src.items()) if isinstance(src, dict) else []


def _split_doc(doc: Doc, d) -> tuple[dict, dict[str, list]]:
    """문서 → (머리, 모음별 [(열쇠, 값)]). 머리 = {"v": 모음이 아닌 최상위 값들, "order": 원래 키 순서, "present": 모음 키가 있었나}."""
    if _rootcoll(doc):
        return {}, {doc.colls[0].table: _items(doc.colls[0], d)}
    plain = dict(d)
    parts: dict[str, list] = {}
    present = {}
    for cl in doc.colls:
        present[cl.path] = cl.path in plain
        parts[cl.table] = _items(cl, plain.pop(cl.path, None))
    return {"v": plain, "order": list(d.keys()), "present": present}, parts


def _join_doc(doc: Doc, head: dict, parts: dict[str, list]):
    if _rootcoll(doc):
        cl = doc.colls[0]
        items = parts.get(cl.table, [])
        return [v for _k, v in items] if cl.kind == "list" else {k: v for k, v in items}
    order = head.get("order") or []
    present = head.get("present") or {}
    plain = head.get("v") or {}
    colls = {cl.path: cl for cl in doc.colls}
    out = {}
    for k in list(order) + [k for k in plain if k not in order] + [k for k in colls if k not in order]:
        if k in out:
            continue
        if k in colls:
            if not present.get(k, False):
                continue
            items = parts.get(colls[k].table, [])
            out[k] = [v for _k, v in items] if colls[k].kind == "list" else {kk: v for kk, v in items}
        elif k in plain:
            out[k] = plain[k]
    return out


def _db_write_doc(c, doc: Doc, d, changed: set[str] | None = None, removed: set[str] | None = None) -> None:
    head, parts = _split_doc(doc, d)
    for cl in doc.colls:
        items = parts.get(cl.table, [])
        if changed is None or cl.kind == "list":
            _put_rows(c, cl, items, replace_all=True)
            continue
        idx = {k: i for i, (k, _v) in enumerate(items)}
        for k in (removed or ()):
            if k not in idx:
                kc = _key_cols(cl, k)
                c.execute(f"DELETE FROM {cl.table} WHERE " + " AND ".join(f"{n}=?" for n in cl.keys)
                          + (" AND pc_id='local'" if cl.pc and "pc_id" not in cl.keys else ""), kc)
        sel = [(k, v) for k, v in items if k in changed]
        for k, v in sel:
            der = _derived_cols(cl, v)
            base = list(cl.keys) + ["ord", "body_json"] + (["pc_id"] if cl.pc and "pc_id" not in cl.keys else [])
            cols = base + list(der)
            vals = list(_key_cols(cl, k)) + [idx[k], json.dumps(v, ensure_ascii=False)] \
                + ([LOCAL_PC] if cl.pc and "pc_id" not in cl.keys else []) + list(der.values())
            c.execute(f"INSERT OR REPLACE INTO {cl.table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
    c.execute("INSERT INTO doc_meta(name,head_json,updated) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET"
              " head_json=excluded.head_json, updated=excluded.updated",
              (doc.name, json.dumps(head, ensure_ascii=False), int(time.time())))


def _db_read_doc(c, doc: Doc):
    r = c.execute("SELECT head_json FROM doc_meta WHERE name=?", (doc.name,)).fetchone()
    if r is None or r["head_json"] is None:
        return None
    head = json.loads(r["head_json"])
    parts = {cl.table: _get_rows(c, cl) for cl in doc.colls}
    return _join_doc(doc, head, parts)


def _dump_bytes(doc: Doc, d) -> bytes:
    return doc.dump(d).encode("utf-8")


def _mirror_now(doc: Doc, d) -> None:
    """②③: JSON 교체 → 표식 지움. 잠금을 쥔 채, 트랜잭션 밖에서 부른다(안이면 커밋 뒤로 미룬다)."""
    if _depth:
        after_commit(lambda: _mirror_now(doc, d))
        return
    p = doc.file()
    data = _dump_bytes(doc, d)
    h = _sha(data)
    try:
        write_atomic(p, data)
    except OSError as e:
        log(f"[store] 거울 쓰기 실패 {p.name}: {e}")
        return                                  # 표식이 남는다 → 다음 대조가 거울을 다시 쓴다
    w = writer()
    w.execute("BEGIN IMMEDIATE")
    try:
        w.execute("UPDATE doc_meta SET hash=?, pending_hash=NULL, prev_hash=NULL WHERE name=?", (h, doc.name))
        w.execute("COMMIT")
    except BaseException:
        w.execute("ROLLBACK")
        raise
    _sig[(str(db_path()), doc.name)] = _fsig(p)


def save_doc(name: str, d, changed: set[str] | None = None, removed: set[str] | None = None,
             blocking: bool = True) -> bool:
    """문서를 저장한다: ① 표 + 거울 미완 표식 커밋 → ② JSON 교체 → ③ 표식 지움. changed 를 주면 그 열쇠의 행만 쓴다.
    blocking=False 면 다른 쓰기가 진행 중일 때 기다리지 않고 False(부른 쪽이 나중에 다시)."""
    doc = _docs[name]
    try:
        with tx(blocking=blocking) as c:
            _reconcile_locked(doc, will_write=True)
            _db_write_doc(c, doc, d, changed, removed)
            if doc.mirror and mirror_enabled():
                new = _sha(_dump_bytes(doc, d))
                c.execute("UPDATE doc_meta SET prev_hash=hash, pending_hash=? WHERE name=?", (new, name))
                after_commit(lambda: _mirror_now(doc, d))
    except Busy:
        return False
    return True


def read_doc(name: str):
    """문서를 상태 DB 에서 읽는다(없으면 빈 문서). 거울 파일이 바깥에서 바뀌었으면 먼저 가져온다."""
    doc = _docs[name]
    p = doc.file()
    key = (str(db_path()), name)
    if _sig.get(key, "?") != _fsig(p) or key not in _sig:
        with _lock:
            _reconcile_locked(doc)
    c = reader()
    try:
        d = _db_read_doc(c, doc)
    finally:
        c.close()
    return doc.empty() if d is None else d


def _parse_file(doc: Doc, b: bytes):
    try:
        d = json.loads(b.decode("utf-8-sig"))
    except Exception:  # noqa: BLE001
        return None
    if not doc.valid(d):
        return None
    try:
        return doc.norm(d)
    except Exception:  # noqa: BLE001
        return None


def _backup_copy(doc: Doc, b: bytes, why: str) -> Path | None:
    try:
        d = config.data_dir() / "backups" / "mirror"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{doc.file().name}.{why}.{time.strftime('%Y%m%d-%H%M%S')}"
        p.write_bytes(b)
        return p
    except OSError:
        return None


def _import_locked(c, doc: Doc, d, fh: str | None) -> None:
    c.execute("BEGIN IMMEDIATE")
    try:
        _db_write_doc(c, doc, d)
        c.execute("UPDATE doc_meta SET hash=?, pending_hash=NULL, prev_hash=NULL WHERE name=?", (fh, doc.name))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _meta_set(c, name: str, **cols) -> None:
    c.execute("INSERT INTO doc_meta(name, updated) VALUES(?,?) ON CONFLICT(name) DO NOTHING", (name, int(time.time())))
    for k, v in cols.items():
        c.execute(f"UPDATE doc_meta SET {k}=? WHERE name=?", (v, name))


def _reconcile_locked(doc: Doc, will_write: bool = False) -> None:
    """거울 파일 ↔ 상태 DB 대조(D5). 잠금 아래, 트랜잭션 밖이든 안이든 부를 수 있다(안이면 같은 트랜잭션에 쓴다)."""
    c = writer()
    p = doc.file()
    key = (str(db_path()), doc.name)
    sig = _fsig(p)
    if key in _sig and _sig[key] == sig:
        return
    in_tx = _depth > 0
    try:
        b = p.read_bytes() if sig is not None else None
    except OSError:
        return
    fh = _sha(b)
    row = c.execute("SELECT hash, pending_hash, prev_hash, head_json FROM doc_meta WHERE name=?", (doc.name,)).fetchone()

    def _write(fn):
        if in_tx:
            fn(c)
        else:
            c.execute("BEGIN IMMEDIATE")
            try:
                fn(c)
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def _import(d):
        def fn(cc):
            _db_write_doc(cc, doc, d)
            _meta_set(cc, doc.name, hash=fh, pending_hash=None, prev_hash=None)
        _write(fn)

    def _remirror():
        if will_write:
            return                              # 곧 저장이 거울을 새로 쓴다
        cur = _db_read_doc(c, doc)
        if cur is None:
            _write(lambda cc: _meta_set(cc, doc.name, hash=fh, pending_hash=None, prev_hash=None))
            return
        _mirror_now(doc, cur)

    if row is None or row["head_json"] is None:
        # 처음 보는 문서 → 옛 파일 가져오기(legacy). 파일이 깨졌으면 사본을 남기고 빈 문서로 시작(옛 동작: 깨지면 빈 저장소).
        if b is not None:
            d = _parse_file(doc, b)
            if d is None:
                _backup_copy(doc, b, "corrupt")
                _write(lambda cc: _meta_set(cc, doc.name, hash=fh, pending_hash=None, prev_hash=None))
            else:
                _import(d)
        _sig[key] = _fsig(p)
        return
    h, pend, prev = row["hash"], row["pending_hash"], row["prev_hash"]
    if pend is not None and fh == pend:
        _write(lambda cc: _meta_set(cc, doc.name, hash=pend, pending_hash=None, prev_hash=None))   # 표식만 남았음
    elif pend is not None and fh is not None and fh == prev:
        # 애매함: 거울을 못 쓰고 죽었거나, 되돌아간 옛 판이 같은 내용으로 되돌렸거나 — 자동으로 덮지 않는다.
        bk = _backup_copy(doc, b, "ambiguous")
        _remirror()
        incident("mirror_ambiguous", doc=doc.name, file=p.name, kept=str(bk) if bk else None)
    elif fh == h and pend is None:
        pass
    elif b is None:
        if h is not None or pend is not None:
            _remirror()                         # 거울 파일이 없어짐 → DB 가 정본
    else:
        d = _parse_file(doc, b)
        if d is None:
            bk = _backup_copy(doc, b, "corrupt")
            _remirror()
            incident("mirror_corrupt", doc=doc.name, file=p.name, kept=str(bk) if bk else None)
        else:
            _import(d)                          # 거울 뒤에 바뀐 파일 = 옛 판(또는 사람)이 쓴 최신 값
            incident("mirror_imported", doc=doc.name, file=p.name)
    _sig[key] = _fsig(p)


def mirror_enabled() -> bool:
    """R1: 늘 거울을 쓴다. R2(TODOS T10)는 되돌림 대상이 R1 이상일 때만 끈다(legacy_import.rollback_floor)."""
    from session_manager import legacy_import
    return legacy_import.mirror_needed()


def reconcile_all() -> dict:
    """등록된 문서 전부를 대조(기동 때). 문서 모듈을 다 불러온 뒤 부른다."""
    out = {}
    for name, doc in docs().items():
        try:
            with _lock:
                _reconcile_locked(doc)
            out[name] = "ok"
        except Exception as e:  # noqa: BLE001
            out[name] = f"err: {e}"
    return out


# ---------------------------------------------------------------- 세션 표 등록(T8)
# 세션 삭제(휴지통) 때 같이 지울 표 / 남길 표(장부·이력). session_id 열이 있는 표는 모두 둘 중 하나에 있어야 한다(시험이 검사).
# 지울 표는 lifecycle.delete_session 이 주인 모듈(session_meta·profiles·hooks)로 지우고 사본을 휴지통 버킷에 둔다(복구 때 되살림).
# 남길 표: 팀 장부·작업 장부(경력·기록 — 세션이 지워져도 남는 것이 목적), 등록부·숨김 목록(주인 모듈이 스스로 정리).
SESSION_TABLES_DELETE = ("session_meta", "session_policy", "session_runtime", "launch_settings")
SESSION_TABLES_KEEP = ("agent_sessions", "ingest", "usage", "addresses", "pending_sends", "web_prompts",
                       "worklog_ws", "worklog_turns", "pty_registry", "hidden_sessions")


def tables_with_session_id(c: sqlite3.Connection | None = None) -> set[str]:
    own = c is None
    c = c or reader()
    try:
        out = set()
        for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            cols = {r[1] for r in c.execute(f"PRAGMA table_info('{t}')").fetchall()}
            if "session_id" in cols:
                out.add(t)
        return out
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- 백업(T7)
def backup_dir() -> Path:
    return config.data_dir() / "backups"


def backup_now(keep: int = 7) -> Path | None:
    """sqlite backup API 로 backups/state-YYYYMMDD.db(하루 1개, keep 개 보관). 쓰기 연결이 원본이다."""
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"state-{time.strftime('%Y%m%d')}.db"
    tmp = dest.with_suffix(".db.tmp")
    with _lock:
        src = writer()
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out)
        finally:
            out.close()
    os.replace(tmp, dest)
    olds = sorted(d.glob("state-*.db"))
    for o in olds[:-keep] if len(olds) > keep else []:
        try:
            o.unlink()
        except OSError:
            pass
    return dest


def latest_backup() -> Path | None:
    olds = sorted(backup_dir().glob("state-*.db")) if backup_dir().exists() else []
    return olds[-1] if olds else None


def quick_check() -> str:
    c = reader()
    try:
        return str(c.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        c.close()


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
