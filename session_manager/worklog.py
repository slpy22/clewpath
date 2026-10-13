"""모든 세션의 작업 장부(2026-10-13 사장님: "비서가 일반 세션에 요청한 일도 다 파악해야 개인 비서").

팀 수집기(teamlog)는 팀에 묶인 세션만 읽는다. 이 모듈은 **모든 일반 세션**(자동 실행 기록·모델 읽기용 빈 기록 제외)의
jsonl 을 **읽기만** 해서(불가침 원칙 무관) 턴 단위로 짧게 남긴다:

    사람이 친 요청 1줄 = 턴 하나  ──▶ turns(request)
    그 뒤 claude 의 마지막 글 답   ──▶ turns(result)  — 턴이 이어지면 계속 덮어써 '마지막 답' 이 된다

원문을 통째로 보관하지 않고 요청 전문(최대 4천 자) + 답 요약(최대 1500 자)만 둔다(사장님 결정 — 디스크 부담 작게).
claude 가 보관 기간(기본 30일)이 지나 jsonl 을 지워도 장부는 남는다(gone_at 표시). 비밀값은 teamlog.redact 로 가린다.

정기 작업 'worklog'(5분)가 한 번에 세션당 16MB·전체 64MB 만 읽어 이어 간다(처음 채우기는 여러 번에 나눠서).
읽기 위치는 완성된 줄 끝에만 두고, 파일 앞부분이 바뀌거나(첫 4KB 서명) 줄어들면 그 세션을 처음부터 다시 접는다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

PER_SESSION_BYTES = 16 * 1024 * 1024
PER_RUN_BYTES = 64 * 1024 * 1024
REQ_MAX = 4000
RES_MAX = 1500
_HEAD = 4096
_SIG = 64
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws(
  session_id TEXT PRIMARY KEY, path TEXT, cwd TEXT, title TEXT,
  off INTEGER NOT NULL DEFAULT 0, head TEXT, sig TEXT, n INTEGER NOT NULL DEFAULT 0,
  last_ts INTEGER, gone_at INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS turns(
  session_id TEXT NOT NULL, n INTEGER NOT NULL, ts INTEGER, request TEXT, result TEXT, result_ts INTEGER,
  PRIMARY KEY(session_id, n));
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts);
"""


def _db_file() -> Path:
    from session_manager import config
    return config.data_dir() / "worklog.db"


def _connect() -> sqlite3.Connection:
    f = _db_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(f), timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _h(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _epoch(ts) -> int | None:
    from session_manager.scanner import _epoch as e
    v = e(ts) if ts else 0
    return int(v) if v else None


def _clip(text: str, n: int) -> str:
    from session_manager import teamlog
    t = teamlog.redact(text or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _answer_text(rec: dict) -> str | None:
    """assistant 레코드의 글 답(도구 호출·생각 제외). 사이드체인·시스템 생성 답은 뺀다."""
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    m = rec.get("message")
    if not isinstance(m, dict) or m.get("model") == "<synthetic>":
        return None
    c = m.get("content")
    if isinstance(c, str):
        return c.strip() or None
    if isinstance(c, list):
        t = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text").strip()
        return t or None
    return None


def _read(path: Path, start: int, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(n)


def _fold(c, sid: str, path: Path, budget: int) -> int:
    """한 세션을 이어 접는다 → 읽은 바이트 수."""
    from session_manager import teamlog
    row = c.execute("SELECT * FROM ws WHERE session_id=?", (sid,)).fetchone()
    size = path.stat().st_size
    head = _h(_read(path, 0, _HEAD))
    off, n = (row["off"], row["n"]) if row else (0, 0)
    if row and (size < off or row["head"] != head
                or (off >= _SIG and row["sig"] != _h(_read(path, off - _SIG, _SIG)))):
        c.execute("DELETE FROM turns WHERE session_id=?", (sid,))      # 다시 쓰인 파일 → 처음부터
        off, n = 0, 0
    if size <= off:
        return 0
    chunk = _read(path, off, min(budget, size - off))
    nl = chunk.rfind(b"\n")
    if nl == -1:
        return 0
    done = chunk[:nl + 1]
    last_ts = row["last_ts"] if row and off else None
    for bline in done.split(b"\n"):
        if not bline.strip():
            continue
        try:
            rec = json.loads(bline)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        ts = _epoch(rec.get("timestamp"))
        human = teamlog._human_text(rec)
        if human:
            n += 1
            c.execute("INSERT OR REPLACE INTO turns(session_id,n,ts,request,result,result_ts) VALUES(?,?,?,?,NULL,NULL)",
                      (sid, n, ts, _clip(human, REQ_MAX)))
            last_ts = ts or last_ts
            continue
        if n:
            ans = _answer_text(rec)
            if ans:
                c.execute("UPDATE turns SET result=?, result_ts=? WHERE session_id=? AND n=?",
                          (_clip(ans, RES_MAX), ts, sid, n))
                last_ts = ts or last_ts
    off += len(done)
    sig = _h(_read(path, off - _SIG, _SIG)) if off >= _SIG else None
    c.execute("INSERT INTO ws(session_id,path,off,head,sig,n,last_ts,updated) VALUES(?,?,?,?,?,?,?,?)"
              " ON CONFLICT(session_id) DO UPDATE SET path=excluded.path, off=excluded.off, head=excluded.head,"
              " sig=excluded.sig, n=excluded.n, last_ts=excluded.last_ts, updated=excluded.updated, gone_at=NULL",
              (sid, str(path), off, head, sig, n, last_ts, int(time.time())))
    return len(done)


def _title(m, recs) -> str:
    rec = recs.get(m.session_id) or {}
    return rec.get("name") or m.custom_title or m.ai_title or m.slug or m.session_id[:8]


def collect(metas=None, run_budget: int = PER_RUN_BYTES, per_session: int = PER_SESSION_BYTES) -> dict:
    """정기 작업 본체. 최근에 바뀐 세션부터 이어 접고, 사라진 파일은 gone_at 으로 표시(장부는 남김)."""
    from session_manager import housekeeping, labels, scanner
    metas = metas if metas is not None else scanner.scan_all()
    hidden = housekeeping.hidden_session_ids()
    recs = labels.all_records()
    live = [m for m in metas if not getattr(m, "headless", False) and m.session_id not in hidden]
    live.sort(key=lambda m: m.mtime or 0, reverse=True)
    read = touched = 0
    behind = 0
    with _lock:
        c = _connect()
        try:
            known = {r["session_id"]: r for r in c.execute("SELECT session_id, off FROM ws")}
            for m in live:
                p = Path(m.jsonl_path)
                if not p.exists():
                    continue
                c.execute("UPDATE ws SET cwd=?, title=? WHERE session_id=?", (m.cwd, _title(m, recs), m.session_id))
                k = known.get(m.session_id)
                if k and k["off"] == p.stat().st_size:
                    continue                                   # 그대로(줄어들었으면 _fold 가 처음부터 다시 접는다)
                if read >= run_budget:
                    behind += 1
                    continue
                got = _fold(c, m.session_id, p, min(per_session, run_budget - read))
                c.execute("UPDATE ws SET cwd=?, title=? WHERE session_id=?", (m.cwd, _title(m, recs), m.session_id))
                c.commit()
                read += got
                touched += 1 if got else 0
                k2 = c.execute("SELECT off FROM ws WHERE session_id=?", (m.session_id,)).fetchone()
                if k2 and k2["off"] < p.stat().st_size - 1:
                    behind += 1
            now = int(time.time())
            present = {m.session_id for m in metas}
            gone = [sid for sid in known if sid not in present]
            for sid in gone:
                c.execute("UPDATE ws SET gone_at=COALESCE(gone_at, ?) WHERE session_id=?", (now, sid))
            c.commit()
            total = c.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            nses = c.execute("SELECT COUNT(*) FROM ws").fetchone()[0]
        finally:
            c.close()
    summary = f"세션 {nses}개 · 요청 {total}건 기록" + (f" · {touched}개 새로 읽음" if touched else "") \
        + (f" · {behind}개 아직 읽는 중" if behind else "")
    return {"read_bytes": read, "touched": touched, "behind": behind, "sessions": nses, "turns": total, "summary": summary}


# ---------------------------------------------------------------- 비서용 조회
def _live_state(sid: str, rt_map, peer_map, now: float) -> dict:
    from session_manager import hooks, webterm
    rt = hooks.effective(rt_map.get(sid), alive=bool(peer_map.get(sid) or webterm.has_terminal(sid)), quiet_s=None, now=now) or {}
    p = peer_map.get(sid)
    return {"phase": rt.get("phase"), "live": bool(p or webterm.has_terminal(sid)),
            "address": ("uds:" + p["socket"]) if p and p.get("socket") else None}


def brief(days: float = 2, per_session: int = 3, limit: int = 40) -> dict:
    """최근 days 일 동안 요청이 있었던 세션 + 지금 기다리는 세션 — 세션마다 최근 요청·답 몇 개."""
    from session_manager import hooks, peers, retention, scanner
    now = time.time()
    since = int(now - days * 86400)
    rt_map, pmap = hooks.status_map(), peers.peer_map()
    metas = {m.session_id: m for m in scanner.scan_all()}
    with _lock:
        c = _connect()
        try:
            rows = c.execute("SELECT * FROM ws WHERE last_ts>=? ORDER BY last_ts DESC LIMIT ?", (since, int(limit))).fetchall()
            out = []
            seen = set()
            for r in rows:
                seen.add(r["session_id"])
                out.append(_sess_view(c, r, per_session, rt_map, pmap, metas, now))
            # 오래 전 요청이어도 지금 사람을 기다리는 세션은 넣는다
            for sid, st in rt_map.items():
                if sid in seen or (st or {}).get("phase") not in ("permission", "waiting"):
                    continue
                r = c.execute("SELECT * FROM ws WHERE session_id=?", (sid,)).fetchone()
                if r:
                    v = _sess_view(c, r, per_session, rt_map, pmap, metas, now)
                    if v["state"]["phase"] in ("permission", "waiting"):
                        out.append(v)
        finally:
            c.close()
    soon = retention.soon([m for m in metas.values() if not getattr(m, "headless", False)], retention.SOON_DAYS, now)
    return {"since": since, "sessions": out, "cleanup_soon": soon, "now": int(now)}


def _sess_view(c, r, per_session, rt_map, pmap, metas, now) -> dict:
    from session_manager import retention
    sid = r["session_id"]
    turns = c.execute("SELECT n,ts,request,result,result_ts FROM turns WHERE session_id=? ORDER BY n DESC LIMIT ?",
                      (sid, int(per_session))).fetchall()
    m = metas.get(sid)
    return {"session_id": sid, "title": r["title"], "cwd": r["cwd"], "last_ts": r["last_ts"], "turns_total": r["n"],
            "gone_at": r["gone_at"], "cleanup_at": retention.delete_at(m.mtime) if m else None,
            "state": _live_state(sid, rt_map, pmap, now),
            "recent": [{"n": t["n"], "ts": t["ts"], "request": _short(t["request"], 400), "result": _short(t["result"], 500)}
                       for t in turns]}


def _short(t, n):
    if not t:
        return t
    return t if len(t) <= n else t[:n - 1] + "…"


def session_turns(session_id: str, offset: int = 0, limit: int = 50) -> dict:
    with _lock:
        c = _connect()
        try:
            r = c.execute("SELECT * FROM ws WHERE session_id=?", (session_id,)).fetchone()
            if not r:
                return {"session_id": session_id, "found": False, "turns": []}
            rows = c.execute("SELECT n,ts,request,result,result_ts FROM turns WHERE session_id=? ORDER BY n LIMIT ? OFFSET ?",
                             (session_id, max(1, min(int(limit), 200)), max(0, int(offset)))).fetchall()
            return {"session_id": session_id, "found": True, "title": r["title"], "cwd": r["cwd"], "turns_total": r["n"],
                    "gone_at": r["gone_at"], "turns": [dict(x) for x in rows]}
        finally:
            c.close()


def search(q: str, limit: int = 30) -> dict:
    q = (q or "").strip()
    if not q:
        return {"items": []}
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with _lock:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT t.session_id, t.n, t.ts, t.request, t.result, w.title, w.cwd, w.gone_at FROM turns t"
                " JOIN ws w ON w.session_id=t.session_id"
                " WHERE t.request LIKE ? ESCAPE '\\' OR t.result LIKE ? ESCAPE '\\' ORDER BY t.ts DESC LIMIT ?",
                (like, like, max(1, min(int(limit), 200)))).fetchall()
        finally:
            c.close()
    return {"items": [{**dict(r), "request": _short(r["request"], 400), "result": _short(r["result"], 500)} for r in rows]}
