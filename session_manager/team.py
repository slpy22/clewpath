"""팀·에이전트 명부 + 일감 장부 — 워커 직접 통신(P2P)과 개인 비서 인프라의 바닥(설계 docs/designs/workers-p2p.md).

왜: 세션은 교체(대화 비대·continued-in)돼도 '그 일을 한 주체' 는 이어져야 사장님의 작업 이력·경력이 된다.
그래서 에이전트(영속 페르소나) ≠ 세션 으로 두고, 일감과 그 결과를 지워지지 않는 장부에 쌓는다.

    teams ──< memberships >── agents ──< agent_sessions (세션 UUID 이력: created/continued/replaced/imported)
      │                                   (session_id 는 전역 유일 — 한 세션은 한 에이전트)
      └──< tasks ──< events (append 전용: assigned/submitted/accepted/rejected/reopened/blocked/note/overdue …)

일감 상태기계(eng E-9) — 상태와 이벤트는 같은 트랜잭션, 재시도는 idem_key 로 1회만 반영:

    assigned ──submit(ver 일치)──▶ submitted ──accept──▶ accepted ──reopen(ver+1)──▶ assigned
       │  ▲                          └──reject──▶ rejected ──reopen/reassign(ver+1)──┘
       │  └──reassign(ver+1)── blocked ◀──block── assigned

저장 = SQLite(eng E-1: jsonl_log 는 8MB 에서 한 세대만 남겨 경력이 사라진다). 데이터 폴더의 team.db.
쓰기는 Host API 하나로만 들어온다(eng E-2/E-5: 로컬 전용). '관리 세션만 쓴다' 는 프롬프트 약속이고
actor_session 은 자기 신고다(eng E-11, 결정 로그 e2457b66 — 업그레이드 조건 TODOS).
claude 파일은 읽기만(scanner·peers) — 불가침 원칙 무관.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from session_manager import config

SCHEMA_VERSION = 3
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams(
  id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, root TEXT,
  manager_agent TEXT, parent TEXT, monitor_group_id TEXT, monitor_sync TEXT,
  next_task INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL, archived INTEGER);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, alias TEXT NOT NULL, role TEXT, tags_json TEXT,
  status TEXT NOT NULL DEFAULT 'active', created INTEGER NOT NULL, retired INTEGER);
CREATE TABLE IF NOT EXISTS memberships(
  team_id TEXT NOT NULL, agent_id TEXT NOT NULL, alias TEXT NOT NULL, member_role TEXT NOT NULL,
  write_scope_json TEXT, joined INTEGER NOT NULL, left_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS ux_member_active ON memberships(team_id, alias) WHERE left_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_member_agent ON memberships(team_id, agent_id) WHERE left_at IS NULL;
CREATE TABLE IF NOT EXISTS agent_sessions(
  agent_id TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE, started INTEGER NOT NULL,
  ended INTEGER, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, team_id TEXT NOT NULL, goal TEXT NOT NULL, owner_agent TEXT NOT NULL,
  collaborators_json TEXT, write_scope_json TEXT, done_when TEXT, due INTEGER,
  status TEXT NOT NULL, assignment_ver INTEGER NOT NULL DEFAULT 1, parent TEXT,
  created INTEGER NOT NULL, closed INTEGER);
CREATE INDEX IF NOT EXISTS ix_tasks_team ON tasks(team_id, status);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, team_id TEXT NOT NULL, task_id TEXT,
  agent_id TEXT, kind TEXT NOT NULL, actor_session TEXT, idem_key TEXT UNIQUE, payload_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS ix_events_team ON events(team_id, id);
"""

# v2(단계 2): 당시 소속(agent_sessions.team_id)·원본 시각(events.src_ts) + 수집기(teamlog) 테이블.
_SCHEMA_V2 = """
ALTER TABLE agent_sessions ADD COLUMN team_id TEXT;
ALTER TABLE events ADD COLUMN src_ts INTEGER;
UPDATE agent_sessions SET team_id=(SELECT m.team_id FROM memberships m WHERE m.agent_id=agent_sessions.agent_id
  ORDER BY (m.left_at IS NULL) DESC, m.joined DESC LIMIT 1) WHERE team_id IS NULL;
CREATE TABLE IF NOT EXISTS ingest(
  session_id TEXT PRIMARY KEY, agent_id TEXT, team_id TEXT, gen INTEGER NOT NULL DEFAULT 1,
  offset INTEGER NOT NULL DEFAULT 0, head_sig TEXT, tail_sig TEXT, ctx_task TEXT,
  rewritten INTEGER NOT NULL DEFAULT 0, gone INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS usage(
  session_id TEXT NOT NULL, message_id TEXT NOT NULL, agent_id TEXT, team_id TEXT, task_id TEXT, model TEXT,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
  cache_create INTEGER NOT NULL DEFAULT 0, src_ts INTEGER, PRIMARY KEY(session_id, message_id));
CREATE INDEX IF NOT EXISTS ix_usage_team ON usage(team_id, agent_id);
CREATE TABLE IF NOT EXISTS addresses(address TEXT PRIMARY KEY, session_id TEXT NOT NULL, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS pending_sends(tool_use_id TEXT PRIMARY KEY, session_id TEXT, event_id INTEGER, created INTEGER);
"""


# v3(단계 3, 개인 비서): 포트폴리오 팀 종류 · 비서 1명 · 승인 요청 · 사장님 프로필.
_SCHEMA_V3 = """
ALTER TABLE teams ADD COLUMN kind TEXT NOT NULL DEFAULT 'team';
CREATE TABLE IF NOT EXISTS assistant(id INTEGER PRIMARY KEY CHECK(id=1), agent_id TEXT NOT NULL, team_id TEXT NOT NULL,
  set_at INTEGER);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, kind TEXT NOT NULL, args_json TEXT, summary TEXT,
  requested_by TEXT, created INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL,
  decided_at INTEGER, decided_via TEXT, executed_at INTEGER, result_json TEXT);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status, created);
CREATE TABLE IF NOT EXISTS profile(id TEXT PRIMARY KEY, topic TEXT, statement TEXT, evidence_json TEXT, confidence REAL,
  status TEXT NOT NULL, source TEXT NOT NULL, stmt_hash TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER);
CREATE INDEX IF NOT EXISTS ix_profile_hash ON profile(stmt_hash);
"""


def _fts_sql() -> str:
    """사람 입력·메시지 전문 검색(한글 부분 일치 = trigram, 없는 SQLite 면 unicode61)."""
    for tok in ("trigram", "unicode61"):
        try:
            sqlite3.connect(":memory:").execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{tok}')")
            return (f"CREATE VIRTUAL TABLE IF NOT EXISTS texts USING fts5(body, kind UNINDEXED, team_id UNINDEXED,"
                    f" event_id UNINDEXED, tokenize='{tok}');")
        except sqlite3.OperationalError:
            continue
    return ""


MEMBER_ROLES = ("manager", "worker")
OPEN_STATUSES = ("assigned", "blocked", "submitted", "rejected")


class TeamError(Exception):
    """API 로 그대로 내보낼 오류(code, http status)."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def db_path() -> Path:
    return config.data_dir() / "team.db"


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        c = sqlite3.connect(str(p), timeout=5, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        v = c.execute("PRAGMA user_version").fetchone()[0]
        if v < SCHEMA_VERSION:
            with _lock:
                v = c.execute("PRAGMA user_version").fetchone()[0]
                if v < 1:
                    c.executescript(_SCHEMA)
                    c.execute("PRAGMA user_version=1")
                    v = 1
                if v < 2:
                    c.executescript(_SCHEMA_V2 + _fts_sql())
                    c.execute("PRAGMA user_version=2")
                    v = 2
                if v < 3:
                    c.executescript(_SCHEMA_V3)
                    c.execute("PRAGMA user_version=3")
        return c
    except sqlite3.DatabaseError as e:            # 손상·잠김 → 호출자는 503, Host 는 계속 산다
        raise TeamError("team_db_unavailable", 503) from e


class _Tx:
    """쓰기 트랜잭션(프로세스 내 직렬화 + BEGIN IMMEDIATE). 상태와 이벤트를 한 번에 커밋한다."""

    def __enter__(self):
        _lock.acquire()
        try:
            self.c = _connect()
            self.c.execute("BEGIN IMMEDIATE")
        except Exception:
            _lock.release()
            raise
        return self.c

    def __exit__(self, et, ev, tb):
        try:
            self.c.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.c.close()
            _lock.release()
        if et is not None and issubclass(et, sqlite3.IntegrityError):
            raise TeamError("conflict", 409) from ev            # 동시 요청이 유일 제약에 걸림(같은 별칭·세션·idem_key)
        if et is not None and issubclass(et, sqlite3.DatabaseError):
            raise TeamError("team_db_unavailable", 503) from ev
        return False


def _read() -> sqlite3.Connection:
    return _connect()


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _uj(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return default


def _clean(s, n: int = 200) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s or "")).strip()[:n]


def _event(c, team_id, kind, task_id=None, agent_id=None, actor=None, idem=None, payload=None) -> int:
    cur = c.execute(
        "INSERT INTO events(ts,team_id,task_id,agent_id,kind,actor_session,idem_key,payload_json)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (_now(), team_id, task_id, agent_id, kind, actor or None, idem or None, _j(payload)))
    return int(cur.lastrowid)


# ---------------------------------------------------------------- 팀

def _code_from(name: str, c) -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:6] or "TM"
    code, n = base, 2
    while c.execute("SELECT 1 FROM teams WHERE code=?", (code,)).fetchone():
        code = f"{base}{n}"
        n += 1
    return code


def create_team(name: str, root: str | None = None, code: str | None = None,
                manager_session: str | None = None, manager_alias: str = "관리",
                parent: str | None = None, kind: str = "team") -> dict:
    name = _clean(name, 80)
    if not name:
        raise TeamError("name_required")
    with _Tx() as c:
        if code:
            code = re.sub(r"[^A-Za-z0-9]", "", code).upper()[:12]
            if not code or c.execute("SELECT 1 FROM teams WHERE code=?", (code,)).fetchone():
                raise TeamError("code_taken", 409)
        else:
            code = _code_from(name, c)
        tid = "tm_" + secrets.token_hex(4)
        if parent is None and kind == "team":                # 비서가 있으면 새 팀은 비서 포트폴리오 아래(단계 3)
            a = c.execute("SELECT agent_id FROM assistant WHERE id=1").fetchone()
            parent = a["agent_id"] if a else None
        c.execute("INSERT INTO teams(id,code,name,root,parent,created,kind) VALUES(?,?,?,?,?,?,?)",
                  (tid, code, name, _clean(root, 400) or None, parent or None, _now(), kind))
        _event(c, tid, "team_created", payload={"name": name, "code": code, "root": root})
    if manager_session:
        try:
            add_member(tid, manager_alias or "관리", role="관리", session_id=manager_session, member_role="manager")
        except TeamError:
            with _Tx() as c:                     # 관리자 등록이 실패하면 팀도 만들지 않은 것으로(반쪽 팀 방지)
                c.execute("DELETE FROM events WHERE team_id=?", (tid,))
                c.execute("DELETE FROM teams WHERE id=?", (tid,))
            raise
    return get_team(tid)


def _team_row(c, team_id: str):
    r = c.execute("SELECT * FROM teams WHERE id=? OR code=?", (team_id, str(team_id or "").upper())).fetchone()
    if not r:
        raise TeamError("team_not_found", 404)
    return r


def list_teams(include_archived: bool = False) -> list[dict]:
    c = _read()
    try:
        q = "SELECT * FROM teams" + ("" if include_archived else " WHERE archived IS NULL") + " ORDER BY created"
        out = []
        for t in c.execute(q).fetchall():
            n = c.execute("SELECT COUNT(*) FROM memberships WHERE team_id=? AND left_at IS NULL", (t["id"],)).fetchone()[0]
            o = c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status IN (?,?,?,?)",
                          (t["id"], *OPEN_STATUSES)).fetchone()[0]
            out.append({"id": t["id"], "code": t["code"], "name": t["name"], "root": t["root"],
                        "kind": t["kind"], "parent": t["parent"],
                        "members": n, "open_tasks": o, "monitor_group_id": t["monitor_group_id"],
                        "monitor_sync": t["monitor_sync"], "archived": bool(t["archived"])})
        return out
    finally:
        c.close()


def archive(team_id: str, on: bool = True) -> dict:
    """팀 보관(끝난 프로젝트). 장부·이력은 그대로, 목록·감시(그룹 맞추기·기한 알림)에서 빠진다.
    관제 그룹은 지우지 않는다 — 사용자가 관제 화면에서 지운다(감시가 다시 만들지 않음)."""
    with _Tx() as c:
        t = _team_row(c, team_id)
        c.execute("UPDATE teams SET archived=? WHERE id=?", (_now() if on else None, t["id"]))
        _event(c, t["id"], "team_archived" if on else "team_unarchived")
    return {"id": t["id"], "archived": bool(on)}


# ---------------------------------------------------------------- 에이전트·소속·세션

def _bind(c, agent_id: str, session_id: str, reason: str, team_id: str | None = None) -> bool:
    """세션을 에이전트에 붙인다. 같은 에이전트면 멱등(False), 다른 에이전트 소유면 409.
    team_id = 그 세션이 일한 당시 소속(경력 귀속, Codex C2-6). 없으면 지금 소속."""
    row = c.execute("SELECT agent_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
    if row:
        if row["agent_id"] != agent_id:
            raise TeamError("session_owned_by_other_agent", 409)
        return False
    now = _now()
    if not team_id:
        r = c.execute("SELECT team_id FROM memberships WHERE agent_id=? AND left_at IS NULL ORDER BY joined DESC LIMIT 1",
                      (agent_id,)).fetchone()
        team_id = r["team_id"] if r else None
    c.execute("UPDATE agent_sessions SET ended=? WHERE agent_id=? AND ended IS NULL", (now, agent_id))
    c.execute("INSERT INTO agent_sessions(agent_id,session_id,started,reason,team_id) VALUES(?,?,?,?,?)",
              (agent_id, session_id, now, reason, team_id))
    return True


def add_member(team_id: str, alias: str, role: str = "", tags=None, session_id: str | None = None,
               write_scope=None, member_role: str = "worker", agent_id: str | None = None) -> dict:
    """팀에 구성원 추가. agent_id 를 주면 기존 에이전트(다른 팀 이력 포함)를 데려온다, 없으면 새 에이전트."""
    alias = _clean(alias, 40)
    if not alias:
        raise TeamError("alias_required")
    if member_role not in MEMBER_ROLES:
        raise TeamError("bad_member_role")
    with _Tx() as c:
        t = _team_row(c, team_id)
        if member_role == "manager" and t["manager_agent"]:
            raise TeamError("manager_exists", 409)
        if c.execute("SELECT 1 FROM memberships WHERE team_id=? AND alias=? AND left_at IS NULL",
                     (t["id"], alias)).fetchone():
            raise TeamError("alias_taken", 409)
        if not agent_id and session_id:
            # 이미 다른 팀에서 일하는 세션 = 같은 에이전트(페르소나)가 이 팀에도 소속된다(한 관리자가 여러 팀)
            own = c.execute("SELECT agent_id FROM agent_sessions WHERE session_id=?", (str(session_id),)).fetchone()
            if own:
                agent_id = own["agent_id"]
        if agent_id:
            if not c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
                raise TeamError("agent_not_found", 404)
            if c.execute("SELECT 1 FROM memberships WHERE team_id=? AND agent_id=? AND left_at IS NULL",
                         (t["id"], agent_id)).fetchone():
                raise TeamError("already_member", 409)
            aid = agent_id
        else:
            aid = "ag_" + secrets.token_hex(4)
            c.execute("INSERT INTO agents(id,alias,role,tags_json,created) VALUES(?,?,?,?,?)",
                      (aid, alias, _clean(role, 120), _j(list(tags or [])), _now()))
        c.execute("INSERT INTO memberships(team_id,agent_id,alias,member_role,write_scope_json,joined)"
                  " VALUES(?,?,?,?,?,?)", (t["id"], aid, alias, member_role, _j(list(write_scope or [])), _now()))
        if member_role == "manager":
            c.execute("UPDATE teams SET manager_agent=? WHERE id=?", (aid, t["id"]))
        if session_id:
            _bind(c, aid, str(session_id), "created")
        _event(c, t["id"], "member_added", agent_id=aid,
               payload={"alias": alias, "member_role": member_role, "session_id": session_id})
    return member(team_id, aid)


def bind_session(team_id: str, agent_id: str, session_id: str, reason: str = "replaced") -> dict:
    if reason not in ("created", "continued", "replaced", "imported"):
        raise TeamError("bad_reason")
    with _Tx() as c:
        t = _team_row(c, team_id)
        aid = _member_row(c, t["id"], agent_id)["agent_id"]       # 별칭으로 불러도 실제 에이전트 id 로
        if _bind(c, aid, str(session_id), reason):
            _event(c, t["id"], "session_bound", agent_id=aid, payload={"session_id": session_id, "reason": reason})
    return member(team_id, aid)


def _member_row(c, team_id: str, agent_ref: str):
    r = c.execute("SELECT m.*, a.role, a.tags_json, a.status FROM memberships m JOIN agents a ON a.id=m.agent_id"
                  " WHERE m.team_id=? AND m.left_at IS NULL AND (m.agent_id=? OR m.alias=?)",
                  (team_id, agent_ref, agent_ref)).fetchone()
    if not r:
        raise TeamError("member_not_found", 404)
    return r


def leave(team_id: str, agent_ref: str) -> dict:
    with _Tx() as c:
        t = _team_row(c, team_id)
        m = _member_row(c, t["id"], agent_ref)
        c.execute("UPDATE memberships SET left_at=? WHERE team_id=? AND agent_id=? AND left_at IS NULL",
                  (_now(), t["id"], m["agent_id"]))
        if t["manager_agent"] == m["agent_id"]:
            c.execute("UPDATE teams SET manager_agent=NULL WHERE id=?", (t["id"],))
        _event(c, t["id"], "member_left", agent_id=m["agent_id"], payload={"alias": m["alias"]})
    return {"left": True, "agent_id": m["agent_id"]}


def move(team_id: str, agent_ref: str, to_team: str, alias: str | None = None,
         member_role: str = "worker") -> dict:
    """팀 이동(eng E-10): 옛 소속은 left 로 닫고 새 소속을 연다. 에이전트·세션 이력은 그대로."""
    c = _read()
    try:
        t = _team_row(c, team_id)
        m = _member_row(c, t["id"], agent_ref)
        _team_row(c, to_team)
    finally:
        c.close()
    leave(team_id, m["agent_id"])
    return add_member(to_team, alias or m["alias"], member_role=member_role,
                      write_scope=_uj(m["write_scope_json"], []), agent_id=m["agent_id"])


def _continuation_hops(session_id: str, max_hops: int = 8) -> list[str]:
    """continued-in 사슬의 다음 세션들(자기 제외, 순서대로). 끝 세션만 주던 latest_session_id 와 달리 중간도."""
    out: list[str] = []
    seen = {session_id}
    cur = session_id
    for _ in range(max_hops):
        nxt = _latest_session_step(cur)
        if not nxt or nxt in seen:
            break
        out.append(nxt)
        seen.add(nxt)
        cur = nxt
    return out


def _latest_session_step(session_id: str) -> str | None:
    """한 홉: 이 세션의 continued-in 대상(파일이 있을 때만)."""
    nxt = _latest_session(session_id, one_hop=True)
    return nxt if nxt and nxt != session_id else None


def _latest_session(session_id: str, one_hop: bool = False) -> str:
    try:
        from session_manager import scanner
        return scanner.latest_session_id(session_id, max_hops=1 if one_hop else 8)
    except Exception:  # noqa: BLE001  스캐너 실패는 '그대로' 로 저하
        return session_id


def _peer_by_session() -> dict:
    try:
        from session_manager import peers
        return peers.peer_map()
    except Exception:  # noqa: BLE001
        return {}


def current_session(agent_id: str) -> str | None:
    """에이전트의 지금 세션. 마지막으로 붙인 세션에서 continued-in 사슬을 따라가고,
    새 세션이 나오면 agent_sessions 에 'continued' 로 적는다(eng E-3: 워커 교체도 추적)."""
    c = _read()
    try:
        r = c.execute("SELECT session_id FROM agent_sessions WHERE agent_id=? ORDER BY started DESC, rowid DESC LIMIT 1",
                      (agent_id,)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    sid = r["session_id"]
    hops = _continuation_hops(sid)
    if not hops:
        return sid
    cur = sid
    for nxt in hops:                                 # 중간 세션도 이력에 남긴다(Codex C2-3)
        try:
            with _Tx() as w:
                if _bind(w, agent_id, nxt, "continued"):
                    tm = w.execute("SELECT team_id FROM memberships WHERE agent_id=? AND left_at IS NULL", (agent_id,)).fetchone()
                    if tm:
                        _event(w, tm["team_id"], "session_bound", agent_id=agent_id,
                               payload={"session_id": nxt, "reason": "continued", "from": cur})
        except TeamError:
            return cur                               # 다른 에이전트가 이미 가진 세션 — 거기서 멈춤
        cur = nxt
    return cur


def _member_view(c, m, peers: dict) -> dict:
    sid = current_session(m["agent_id"])
    p = peers.get(sid or "") or {}
    sock = p.get("socket") or ""
    return {"agent_id": m["agent_id"], "alias": m["alias"], "member_role": m["member_role"],
            "role": m["role"] or "", "tags": _uj(m["tags_json"], []),
            "write_scope": _uj(m["write_scope_json"], []), "session_id": sid,
            "live": bool(p), "name": p.get("name") or None, "status": p.get("status") or None,
            # 전송 주소(eng E-6): 이름은 동명이면 모호 → 파이프 주소. 살아 있지 않으면 null.
            "address": ("uds:" + sock) if sock else None}


def member(team_id: str, agent_ref: str) -> dict:
    c = _read()
    try:
        t = _team_row(c, team_id)
        return _member_view(c, _member_row(c, t["id"], agent_ref), _peer_by_session())
    finally:
        c.close()


def get_team(team_id: str) -> dict:
    c = _read()
    try:
        t = _team_row(c, team_id)
        peers = _peer_by_session()
        ms = c.execute("SELECT m.*, a.role, a.tags_json, a.status FROM memberships m JOIN agents a ON a.id=m.agent_id"
                       " WHERE m.team_id=? AND m.left_at IS NULL ORDER BY m.member_role, m.joined", (t["id"],)).fetchall()
        tasks = c.execute("SELECT * FROM tasks WHERE team_id=? AND status IN (?,?,?,?) ORDER BY created",
                          (t["id"], *OPEN_STATUSES)).fetchall()
        return {"id": t["id"], "code": t["code"], "name": t["name"], "root": t["root"], "kind": t["kind"],
                "parent": t["parent"], "manager_agent": t["manager_agent"],
                "monitor_group_id": t["monitor_group_id"], "monitor_sync": t["monitor_sync"],
                "members": [_member_view(c, m, peers) for m in ms],
                "open_tasks": [_task_view(x) for x in tasks]}
    finally:
        c.close()


# ---------------------------------------------------------------- 일감

def _task_view(r) -> dict:
    return {"id": r["id"], "goal": r["goal"], "owner": r["owner_agent"],
            "collaborators": _uj(r["collaborators_json"], []), "write_scope": _uj(r["write_scope_json"], []),
            "done_when": r["done_when"], "due": r["due"], "status": r["status"],
            "assignment_ver": r["assignment_ver"], "parent": r["parent"],
            "created": r["created"], "closed": r["closed"]}


def _agent_in_team(c, team_id: str, ref: str) -> str:
    return _member_row(c, team_id, ref)["agent_id"]


def _idem_hit(c, idem: str | None):
    if not idem:
        return None
    r = c.execute("SELECT task_id FROM events WHERE idem_key=?", (idem,)).fetchone()
    return r["task_id"] if r else None


def create_task(team_id: str, goal: str, owner: str, collaborators=None, write_scope=None,
                done_when: str = "", due: int | None = None, parent: str | None = None,
                actor_session: str | None = None, idem_key: str | None = None) -> dict:
    goal = _clean(goal, 500)
    if not goal:
        raise TeamError("goal_required")
    with _Tx() as c:
        t = _team_row(c, team_id)
        hit = _idem_hit(c, idem_key)
        if hit:
            return _task_get(c, hit)
        oid = _agent_in_team(c, t["id"], owner)
        cols = [_agent_in_team(c, t["id"], x) for x in (collaborators or [])]
        n = t["next_task"]
        task_id = f"{t['code']}-T{n}"
        c.execute("UPDATE teams SET next_task=? WHERE id=?", (n + 1, t["id"]))
        c.execute("INSERT INTO tasks(id,team_id,goal,owner_agent,collaborators_json,write_scope_json,done_when,"
                  "due,status,assignment_ver,parent,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (task_id, t["id"], goal, oid, _j(cols), _j(write_scope or {}), _clean(done_when, 500) or None,
                   int(due) if due else None, "assigned", 1, parent or None, _now()))
        _event(c, t["id"], "assigned", task_id, oid, actor_session, idem_key,
               {"goal": goal, "owner": oid, "collaborators": cols, "assignment_ver": 1})
        return _task_get(c, task_id)


def _task_get(c, task_id: str) -> dict:
    r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not r:
        raise TeamError("task_not_found", 404)
    return _task_view(r)


# action → (허용 출발 상태, 도착 상태(None=그대로), 배정 버전 +1 여부)
_TRANSITIONS = {
    "submit":   (("assigned", "blocked"), "submitted", False),
    "accept":   (("submitted",), "accepted", False),
    "reject":   (("submitted",), "rejected", False),
    "reopen":   (("rejected", "accepted"), "assigned", True),
    "reassign": (("assigned", "blocked", "rejected"), "assigned", True),
    "block":    (("assigned",), "blocked", False),
    "note":     (None, None, False),
}


def transition(team_id: str, task_id: str, action: str, actor_session: str | None = None,
               idem_key: str | None = None, assignment_ver: int | None = None, note: str = "",
               artifacts=None, owner: str | None = None) -> dict:
    if action not in _TRANSITIONS:
        raise TeamError("bad_action")
    allowed, to, bump = _TRANSITIONS[action]
    evidence = None
    if action == "submit":
        c0 = _read()
        try:
            root = _team_row(c0, team_id)["root"]
        finally:
            c0.close()
        evidence = collect_evidence(artifacts or [], root)      # 잠금 밖(파일 해시·git 은 느릴 수 있다)
    with _Tx() as c:
        t = _team_row(c, team_id)
        hit = _idem_hit(c, idem_key)
        if hit:
            return _task_get(c, hit)                            # 재시도 — 이미 반영된 결과 그대로
        r = c.execute("SELECT * FROM tasks WHERE id=? AND team_id=?", (task_id, t["id"])).fetchone()
        if not r:
            raise TeamError("task_not_found", 404)
        if allowed is not None and r["status"] not in allowed:
            raise TeamError(f"bad_transition:{r['status']}->{action}", 409)
        if action == "submit" and (assignment_ver is None or int(assignment_ver) != r["assignment_ver"]):
            raise TeamError("stale_assignment", 409)            # 재배정 전 옛 담당자의 제출 거절
        if action in ("accept", "reject") and assignment_ver is not None and int(assignment_ver) != r["assignment_ver"]:
            raise TeamError("stale_assignment", 409)            # 오래된 승인으로 재배정 뒤 다른 결과를 승인하지 않게
        ver = r["assignment_ver"] + (1 if bump else 0)
        new_owner = r["owner_agent"]
        if action == "reassign" and owner:
            new_owner = _agent_in_team(c, t["id"], owner)
        status = to or r["status"]
        closed = _now() if status == "accepted" else None
        c.execute("UPDATE tasks SET status=?, assignment_ver=?, owner_agent=?, closed=? WHERE id=?",
                  (status, ver, new_owner, closed, task_id))
        payload = {"from": r["status"], "to": status, "assignment_ver": ver, "note": _clean(note, 2000) or None}
        if action == "reassign":
            payload["owner"] = new_owner
        if evidence is not None:
            payload["evidence"] = evidence
        kind = {"submit": "submitted", "accept": "accepted", "reject": "rejected", "reopen": "reopened",
                "reassign": "reassigned", "block": "blocked", "note": "note"}[action]
        _event(c, t["id"], kind, task_id, new_owner, actor_session, idem_key, payload)
        return _task_get(c, task_id)


def task_detail(team_id: str, task_id: str) -> dict:
    c = _read()
    try:
        t = _team_row(c, team_id)
        r = c.execute("SELECT * FROM tasks WHERE id=? AND team_id=?", (task_id, t["id"])).fetchone()
        if not r:
            raise TeamError("task_not_found", 404)
        evs = [{"id": e["id"], "ts": e["ts"], "kind": e["kind"], "agent_id": e["agent_id"],
                "actor_session": e["actor_session"], "payload": _uj(e["payload_json"], {})}
               for e in c.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))]
        return {**_task_view(r), "events": evs}
    finally:
        c.close()


def list_tasks(team_id: str, status: str | None = None, limit: int = 100) -> list[dict]:
    c = _read()
    try:
        t = _team_row(c, team_id)
        if status:
            rows = c.execute("SELECT * FROM tasks WHERE team_id=? AND status=? ORDER BY created DESC LIMIT ?",
                             (t["id"], status, int(limit))).fetchall()
        else:
            rows = c.execute("SELECT * FROM tasks WHERE team_id=? ORDER BY created DESC LIMIT ?",
                             (t["id"], int(limit))).fetchall()
        return [_task_view(r) for r in rows]
    finally:
        c.close()


# ---------------------------------------------------------------- 제출 증거(eng E-13)

_MAX_ARTIFACTS = 20
_MAX_HASH_BYTES = 64 * 1024 * 1024


def _git_head(path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        h = (r.stdout or "").strip()
        return h if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", h) else None
    except Exception:  # noqa: BLE001  git 없음·타임아웃
        return None


def collect_evidence(artifacts, root: str | None) -> dict:
    """산출물 파일의 sha256·크기·mtime + 그 폴더의 git HEAD. 나중에 파일이 덮어써져도 '그때 무엇을 냈는지' 확인."""
    out = []
    heads: dict[str, str | None] = {}
    for a in list(artifacts or [])[:_MAX_ARTIFACTS]:
        raw = str(a or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute() and root:
            p = Path(root) / p
        item = {"path": raw}
        try:
            st = p.stat()
            if p.is_file():
                item.update(size=st.st_size, mtime=int(st.st_mtime))
                if st.st_size <= _MAX_HASH_BYTES:
                    h = hashlib.sha256()
                    with p.open("rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                    item["sha256"] = h.hexdigest()
                else:
                    item["sha256"] = None
                    item["too_large"] = True
            else:
                item["dir"] = True
        except OSError:
            item["missing"] = True
        folder = str((p if item.get("dir") else p.parent))
        if folder not in heads:
            heads[folder] = _git_head(Path(folder)) if Path(folder).exists() else None
        item["git_head"] = heads[folder]
        out.append(item)
    return {"artifacts": out, "root_git_head": _git_head(Path(root)) if root and Path(root).exists() else None}


# ---------------------------------------------------------------- 관제 그룹 맞추기(eng E-3/E-7)

def desired_group(team: dict) -> tuple[str | None, list[str], dict]:
    mgr, subs, labels = None, [], {}
    for m in team["members"]:
        sid = m.get("session_id")
        if not sid:
            continue
        labels[sid] = m["alias"] + (f" · {m['role']}" if m.get("role") else "")
        if m["member_role"] == "manager":
            mgr = sid
        else:
            subs.append(sid)
    return mgr, subs, labels


def sync_group(team_id: str) -> dict:
    """팀 구성원 → 관제 그룹. 구성원·라벨만 팀이 정본, 이름·알림 설정은 사용자 값 보존.
    실패하면 monitor_sync='pending' 으로 두고 다음 변경·감시 스레드가 다시 맞춘다. 그룹은 팀당 1개."""
    team = get_team(team_id)
    if team.get("kind") == "portfolio":
        return {"monitor_group_id": team["monitor_group_id"], "monitor_sync": "portfolio"}
    mgr, subs, labels = desired_group(team)
    if not mgr:
        state, gid = "no_manager", team["monitor_group_id"]
    else:
        try:
            from session_manager import mongroups
            gid = team["monitor_group_id"]
            g = mongroups.get(gid) if gid else None
            if g:
                merged = {**(g.get("labels") or {}), **labels}
                if g.get("manager") == mgr and list(g.get("subs") or []) == subs and merged == (g.get("labels") or {}):
                    state = "ok"
                else:
                    mongroups.save(g.get("name") or team["name"], mgr, subs, labels=merged,
                                   notify=g.get("notify"), gid=gid)
                    state = "ok"
            else:
                gid = mongroups.save(team["name"], mgr, subs, labels=labels)["id"]
                state = "ok"
        except Exception as e:  # noqa: BLE001
            print(f"[team] 관제 그룹 동기화 실패 {team['code']}: {type(e).__name__}: {e}", flush=True)
            state, gid = "pending", team["monitor_group_id"]
    with _Tx() as c:
        c.execute("UPDATE teams SET monitor_group_id=?, monitor_sync=? WHERE id=?", (gid, state, team["id"]))
    return {"monitor_group_id": gid, "monitor_sync": state}


# ---------------------------------------------------------------- v1 등록부 가져오기

def import_v1(path: str, name: str | None = None) -> dict:
    """`.clewpath/workers.json`(v1: {workers:{이름:{session_id,role,cwd,…}}, monitor_group_id}) → 새 팀."""
    p = Path(path)
    if p.is_dir():
        p = p / ".clewpath" / "workers.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        workers = data.get("workers")
        if not isinstance(workers, dict):
            raise ValueError("no workers")
    except FileNotFoundError:
        raise TeamError("v1_not_found", 404)
    except Exception:  # noqa: BLE001
        raise TeamError("v1_invalid", 422)
    root = str(p.parent.parent) if p.parent.name == ".clewpath" else str(p.parent)
    team = create_team(name or Path(root).name or "imported", root=root)
    added, skipped = [], []
    for alias, w in workers.items():
        if not isinstance(w, dict) or not w.get("session_id"):
            skipped.append({"alias": alias, "reason": "no_session"})
            continue
        try:
            add_member(team["id"], alias, role=w.get("role") or "", session_id=str(w["session_id"]))
            added.append(alias)
        except TeamError as e:
            skipped.append({"alias": alias, "reason": e.code})
    gid = data.get("monitor_group_id")
    if gid:
        with _Tx() as c:
            c.execute("UPDATE teams SET monitor_group_id=? WHERE id=?", (str(gid), team["id"]))
    return {"team": get_team(team["id"]), "added": added, "skipped": skipped}


# ---------------------------------------------------------------- 감시 스레드: 그룹 맞추기 + 일감 기한(eng E-12)

def overdue(now: int | None = None) -> list[dict]:
    """기한을 넘긴 열린 일감 중 이 배정 버전에 대해 아직 알리지 않은 것."""
    now = now or _now()
    c = _read()
    try:
        rows = c.execute("SELECT t.*, tm.name AS team_name, tm.manager_agent FROM tasks t JOIN teams tm ON tm.id=t.team_id"
                         " WHERE t.due IS NOT NULL AND t.due < ? AND t.status IN ('assigned','blocked')"
                         " AND tm.archived IS NULL", (now,)).fetchall()
        out = []
        for r in rows:
            seen = c.execute("SELECT 1 FROM events WHERE task_id=? AND kind='overdue' AND idem_key=?",
                             (r["id"], f"overdue:{r['id']}:{r['assignment_ver']}")).fetchone()
            if not seen:
                out.append({"task": _task_view(r), "team_id": r["team_id"], "team_name": r["team_name"],
                            "manager_agent": r["manager_agent"]})
        return out
    finally:
        c.close()


def _notify_overdue(item: dict) -> None:
    t = item["task"]
    with _Tx() as c:                    # idem_key 로 '이 배정 버전에 1회' 보장(재기동 뒤에도)
        if _idem_hit(c, f"overdue:{t['id']}:{t['assignment_ver']}"):
            return
        _event(c, item["team_id"], "overdue", t["id"], t["owner"], None,
               f"overdue:{t['id']}:{t['assignment_ver']}", {"due": t["due"]})
    try:
        from session_manager import push
        sid = current_session(item["manager_agent"]) if item.get("manager_agent") else ""
        push.send("team-overdue", sid or "", f"[{item['team_name']}] {t['id']} 기한 초과",
                  _clean(t["goal"], 120), {"task": t["id"]})
    except Exception as e:  # noqa: BLE001
        print(f"[team] 기한 알림 실패 {t['id']}: {e}", flush=True)


def watch_once() -> dict:
    """주기 작업 1회: 구성원 세션 교체 반영 + 관제 그룹 맞추기 + 기한 초과 알림. DB 가 없으면 아무것도 안 한다."""
    if not db_path().exists():
        return {"teams": 0, "overdue": 0}
    n = 0
    for t in list_teams():
        try:
            sync_group(t["id"])                # get_team 이 current_session 으로 교체도 반영한다
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"[team] 감시 실패 {t['code']}: {e}", flush=True)
    od = overdue()
    for item in od:
        _notify_overdue(item)
    try:                                       # 단계 2: 세션 수집·원본 보존·위반 판정(teamlog)
        from session_manager import teamlog
        lg = teamlog.watch_teams()
    except Exception as e:  # noqa: BLE001
        print(f"[team] 수집 실패: {type(e).__name__}: {e}", flush=True)
        lg = {}
    return {"teams": n, "overdue": len(od), **({"ingest": lg} if lg else {})}


_thread: threading.Thread | None = None
_stop: threading.Event | None = None
WATCH_INTERVAL_S = 30.0       # 단계 2 수집 주기(세션당 4MB 상한)


def start(interval: float = WATCH_INTERVAL_S) -> threading.Thread:
    global _thread, _stop
    if _thread is not None and _thread.is_alive():
        return _thread
    ev = threading.Event()

    def run():
        while not ev.wait(interval):
            try:
                watch_once()
            except Exception as e:  # noqa: BLE001  감시 실패가 Host 를 죽이면 안 된다
                print(f"[team] {type(e).__name__}: {e}", flush=True)

    _stop = ev
    _thread = threading.Thread(target=run, name="teamwatch", daemon=True)
    _thread.start()
    return _thread


def stop() -> None:
    if _stop is not None:
        _stop.set()
