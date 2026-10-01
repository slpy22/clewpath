"""팀 세션 수집기 — 원본 보존 + 장부 자동 채움 + 위반 감지(docs/designs/workers-p2p.md 단계 2).

팀에 바인딩된 모든 세션의 jsonl 을 **읽기만** 해서(불가침 원칙 무관):

    jsonl[offset:] ──완결된 줄만, 세션당 주기 4MB──┬─▶ 보존본  team_archive/<session>/g<세대>-<시작오프셋>.jsonl.gz
                                                    │     (임시→교체 후 DB 커밋. 중간에 죽으면 같은 이름을 다시 쓴다 — C2-2)
                                                    └─▶ 해석(한 트랜잭션)
                                                         user 텍스트(사람)          → events human_input
                                                         user cross-session 수신    → 팀 밖 발신이면 events msg_in, 팀 안이면 문맥만
                                                         user tool_result(전송 결과) → 실패면 events msg_failed
                                                         assistant SendMessage      → events msg (+[cw] 해석, 받는 에이전트)
                                                         assistant usage            → usage (세션·message.id 별 최댓값)

보존·색인 모두 비밀값을 가린다(S2-5). 세대: 첫 4KB 서명이 바뀌거나 크기가 줄거나 오프셋 직전 서명이
달라지면 파일이 바뀐 것 → 세대+1 로 처음부터(C2-1, 중복은 idem 키·usage PK 가 막는다).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from session_manager import config, team

MAX_BYTES_PER_PASS = 4 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
UNANSWERED_S = 30 * 60
DEADLOCK_S = 10 * 60
MAX_TURNS = 4
_NOTIFY_TRIES = 3

# ---------------------------------------------------------------- 비밀값 가리기(S2-5)

_SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk|pk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)"
                        r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',;&]{4,})")


def redact(text: str) -> str:
    if not text:
        return text
    for rx in _SECRET_RES:
        text = rx.sub("[REDACTED]", text)
    return _KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)


# ---------------------------------------------------------------- 경로·서명

def archive_root() -> Path:
    return config.data_dir() / "team_archive"


def _session_path(session_id: str) -> Path | None:
    try:
        from session_manager import monwatch
        p = monwatch._path_of(session_id)
        return Path(p) if p else None
    except Exception:  # noqa: BLE001
        return None


def _sig(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()[:16]


def _read_at(path: Path, start: int, n: int) -> bytes:
    with path.open("rb") as f:
        f.seek(max(0, start))
        return f.read(n)


# ---------------------------------------------------------------- [cw] 헤더

_CW_RE = re.compile(r"^\s*\[cw\]\s*(.+)$", re.M)


def parse_cw(text: str) -> dict:
    """`[cw] task=P-T3 thread=T3-a type=ASK turn=1/4 reply=yes from=백엔드` → dict. 없으면 {}."""
    m = _CW_RE.search(text or "")
    if not m:
        return {}
    out = {}
    for kv in re.split(r"\s*\|\s*|\s+", m.group(1).strip()):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.strip().lower()] = v.strip()
    if "turn" in out:
        t = re.match(r"(\d+)", out["turn"])
        out["turn_n"] = int(t.group(1)) if t else None
    if "type" in out:
        out["type"] = out["type"].upper()
    return out


_XSESS_RE = re.compile(r'<cross-session-message\s+from="([^"]*)"\s+from-name="([^"]*)"[^>]*>\s*(.*?)\s*</cross-session-message>', re.S)
_NOT_HUMAN_PREFIX = ("<command-", "<local-command", "<bash-input", "<bash-stdout", "<bash-stderr", "[Request interrupted",
                     "Caveat: The messages below", "<system-reminder>", "<task-notification>", "Another Claude session sent a message")


def _ts(rec: dict) -> int | None:
    t = rec.get("timestamp")
    if not t:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def _human_text(rec: dict) -> str | None:
    """user 레코드에서 '사람이 친' 텍스트만(C2-4). 도구 결과·세션 간 수신·명령/메타/요약은 제외."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return None
    if rec.get("toolUseResult") is not None:
        return None
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        parts = [c]
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return None
    text = "\n".join(p for p in parts if p).strip()
    if not text or "<cross-session-message" in text or text.startswith(_NOT_HUMAN_PREFIX):
        return None
    return text


# ---------------------------------------------------------------- 주소 이력(C2-6)

def refresh_addresses() -> int:
    """지금 살아 있는 세션의 uds 주소를 이력 테이블에 적는다(과거 메시지의 받는 쪽 해석용)."""
    try:
        from session_manager import peers
        snap = peers.snapshot()
    except Exception:  # noqa: BLE001
        return 0
    now = team._now()
    n = 0
    with team._Tx() as c:
        for p in snap:
            sock = p.get("socket")
            if not sock:
                continue
            addr = "uds:" + sock
            c.execute("INSERT INTO addresses(address,session_id,first_seen,last_seen) VALUES(?,?,?,?)"
                      " ON CONFLICT(address) DO UPDATE SET session_id=excluded.session_id, last_seen=excluded.last_seen",
                      (addr, p["session_id"], now, now))
            n += 1
    return n


def _norm_addr(a: str) -> str:
    a = str(a or "").strip()
    if not a.lower().startswith("uds:"):
        return a
    return "uds:" + a[4:].replace("\\\\", "\\")


def _resolve_to(c, to: str) -> tuple[str | None, str | None]:
    """SendMessage 의 to → (세션, 에이전트). uds 는 주소 이력, 이름은 피어 레지스트리."""
    to = str(to or "").strip()
    sid = None
    if to.lower().startswith("uds:"):
        r = c.execute("SELECT session_id FROM addresses WHERE address=? OR address=?", (to, _norm_addr(to))).fetchone()
        sid = r["session_id"] if r else None
    if not sid:
        try:
            from session_manager import peers
            sid = peers.resolve(to)
        except Exception:  # noqa: BLE001
            sid = None
    if not sid:
        return None, None
    r = c.execute("SELECT agent_id FROM agent_sessions WHERE session_id=?", (sid,)).fetchone()
    return sid, (r["agent_id"] if r else None)


# ---------------------------------------------------------------- 수집

def _offset_since(path: Path | None, since: int | None) -> int:
    """팀에 들어온 시각 이전 줄은 팀 일이 아니다 — 기존 세션(보통 관리 세션)을 팀에 붙이면 그 이전 대화를
    보존·색인하지 않도록, 첫 '그 시각 이후' 줄의 바이트 오프셋부터 시작한다(시각 없는 줄은 앞 줄을 따른다)."""
    if not path or not since or not path.exists():
        return 0
    pos = 0
    first = True
    try:
        with path.open("rb") as f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    return pos                                # 미완성 꼬리 앞에서 멈춤
                m = re.search(rb'"timestamp"\s*:\s*"([^"]+)"', raw[:4096])
                if m:
                    t = _ts({"timestamp": m.group(1).decode("ascii", "ignore")})
                    if t is not None and first and t >= since - 600:
                        return 0                              # 바인딩 직전에 막 만든 세션(claude -p 로 만든 워커) — 전부 팀 일
                    if t is not None:
                        first = False
                    if t is not None and t >= since - 60:
                        return pos
                pos += len(raw)
    except OSError:
        return 0
    return pos


def _row(c, session_id: str, path: Path | None = None):
    r = c.execute("SELECT * FROM ingest WHERE session_id=?", (session_id,)).fetchone()
    if r:
        return r
    b = c.execute("SELECT agent_id, team_id, started, reason FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not b:
        return None
    # 이어진 세션(continued)은 처음부터가 그 에이전트의 일, 그 밖(created·replaced·imported)은 바인딩 시각부터
    start = 0 if b["reason"] == "continued" else _offset_since(path, b["started"])
    c.execute("INSERT INTO ingest(session_id,agent_id,team_id,offset) VALUES(?,?,?,?)",
              (session_id, b["agent_id"], b["team_id"], start))
    return c.execute("SELECT * FROM ingest WHERE session_id=?", (session_id,)).fetchone()


def _disk_ok() -> bool:
    try:
        root = archive_root()
        root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(str(root)).free >= MIN_FREE_BYTES
    except OSError:
        return False


def _write_chunk(session_id: str, gen: int, start: int, data: bytes) -> Path:
    d = archive_root() / session_id
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"g{gen}-{start:012d}.jsonl.gz"
    tmp = final.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, final)
    return final


def ingest_session(session_id: str, max_bytes: int = MAX_BYTES_PER_PASS) -> dict:
    """세션 하나를 오프셋부터 한 번 수집. 반환 {read, events, gone?, rewritten?}."""
    path = _session_path(session_id)
    c = team._read()
    try:
        cur = c.execute("SELECT * FROM ingest WHERE session_id=?", (session_id,)).fetchone()
    finally:
        c.close()
    if path is None or not path.exists():
        if cur is not None and not cur["gone"]:
            with team._Tx() as w:
                w.execute("UPDATE ingest SET gone=? WHERE session_id=?", (team._now(), session_id))
        return {"read": 0, "events": 0, "gone": True}
    size = path.stat().st_size
    with team._Tx() as w:
        row = _row(w, session_id, path)
    if row is None:
        return {"read": 0, "events": 0, "unbound": True}
    gen, off = row["gen"], row["offset"]
    rewritten = False
    if off > 0 and row["head_sig"]:                      # 서명이 아직 없으면(시작 오프셋만 정한 첫 수집) 비교하지 않는다
        tail_now = _sig(_read_at(path, max(0, off - 256), min(256, off)))
        head_now = _sig(_read_at(path, 0, min(4096, off)))   # 저장할 때와 같은 길이(처리한 범위 안)로 비교
        if size < off or head_now != row["head_sig"] or (row["tail_sig"] and tail_now != row["tail_sig"]):
            gen, off, rewritten = gen + 1, 0, True
    if size <= off:
        if rewritten:
            with team._Tx() as w:
                w.execute("UPDATE ingest SET gen=?, offset=0, head_sig=NULL, tail_sig=NULL, rewritten=rewritten+1"
                          " WHERE session_id=?", (gen, session_id))
        return {"read": 0, "events": 0, "rewritten": rewritten}
    data = _read_at(path, off, max_bytes)
    cut = data.rfind(b"\n")
    if cut < 0:
        return {"read": 0, "events": 0}                       # 아직 완결된 줄이 없다
    data = data[:cut + 1]
    end = off + len(data)
    text = data.decode("utf-8", errors="replace")
    if _disk_ok():
        _write_chunk(session_id, gen, off, redact(text).encode("utf-8"))
    else:
        _disk_alert()
    tail_sig = _sig(_read_at(path, max(0, end - 256), min(256, end)))
    head = _sig(_read_at(path, 0, min(4096, end)))
    n = 0
    with team._Tx() as w:
        row = _row(w, session_id)
        ctx = row["ctx_task"]
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001  손상 줄은 보존본에만 남는다
                continue
            if not isinstance(rec, dict):
                continue
            n_, ctx = _apply(w, row, rec, ctx)
            n += n_
        w.execute("UPDATE ingest SET gen=?, offset=?, head_sig=?, tail_sig=?, ctx_task=?, gone=NULL, updated=?,"
                  " rewritten=rewritten+? WHERE session_id=?",
                  (gen, end, head, tail_sig, ctx, team._now(), 1 if rewritten else 0, session_id))
    return {"read": len(data), "events": n, "rewritten": rewritten}


def _ev(w, row, kind, task_id, payload, idem, src_ts, body_for_search: str | None = None) -> int:
    """idem 이 이미 있으면 0. 있으면 전문 검색 색인도."""
    if idem and w.execute("SELECT 1 FROM events WHERE idem_key=?", (idem,)).fetchone():
        return 0
    eid = team._event(w, row["team_id"], kind, task_id, row["agent_id"], row["session_id"], idem, payload)
    w.execute("UPDATE events SET src_ts=? WHERE id=?", (src_ts, eid))
    if body_for_search:
        try:
            w.execute("INSERT INTO texts(body,kind,team_id,event_id) VALUES(?,?,?,?)",
                      (body_for_search, kind, row["team_id"], eid))
        except Exception:  # noqa: BLE001  FTS 없는 SQLite
            pass
    return 1


def _apply(w, row, rec: dict, ctx: str | None) -> tuple[int, str | None]:
    """레코드 하나 → 장부. (만든 이벤트 수, 갱신된 문맥 일감)."""
    n = 0
    ts = _ts(rec)
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    if rec.get("type") == "assistant":
        u = msg.get("usage")
        mid = msg.get("id")
        if isinstance(u, dict) and mid:
            vals = (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))
            w.execute("INSERT INTO usage(session_id,message_id,agent_id,team_id,task_id,model,input,output,cache_read,"
                      "cache_create,src_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,message_id) DO UPDATE SET"
                      " input=MAX(input,excluded.input), output=MAX(output,excluded.output),"
                      " cache_read=MAX(cache_read,excluded.cache_read), cache_create=MAX(cache_create,excluded.cache_create)",
                      (row["session_id"], mid, row["agent_id"], row["team_id"], ctx, msg.get("model"), *vals, ts))
        if isinstance(content, list):
            for b in content:
                if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SendMessage"):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                body = redact(str(inp.get("message") or inp.get("content") or ""))
                cw = parse_cw(body)
                to_sid, to_agent = _resolve_to(w, inp.get("to") or inp.get("recipient"))
                if cw.get("task"):
                    ctx = cw["task"]
                payload = {"to": str(inp.get("to") or ""), "to_session": to_sid, "to_agent": to_agent,
                           "summary": redact(str(inp.get("summary") or "")), "body": body, "cw": cw,
                           "tool_use_id": b.get("id")}
                idem = f"msg:{b.get('id')}" if b.get("id") else None
                if _ev(w, row, "msg", cw.get("task") or ctx, payload, idem, ts, body):
                    n += 1
                    eid = w.execute("SELECT id FROM events WHERE idem_key=?", (idem,)).fetchone()
                    if b.get("id") and eid:
                        w.execute("INSERT OR IGNORE INTO pending_sends(tool_use_id,session_id,event_id,created)"
                                  " VALUES(?,?,?,?)", (b["id"], row["session_id"], eid["id"], team._now()))
        return n, ctx
    if rec.get("type") != "user":
        return 0, ctx
    # 전송 결과(tool_result) — 이 세션의 SendMessage 와 id 로 잇는다(C2-4)
    if isinstance(content, list):
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            tid = b.get("tool_use_id")
            p = w.execute("SELECT event_id FROM pending_sends WHERE tool_use_id=?", (tid,)).fetchone() if tid else None
            if not p:
                continue
            t = b.get("content")
            if isinstance(t, list):
                t = "\n".join(x.get("text", "") for x in t if isinstance(x, dict))
            t = str(t or "")
            from session_manager import monwatch
            failed = bool(b.get("is_error")) or monwatch._sendmsg_failed(t)
            if failed:
                n += _ev(w, row, "msg_failed", None, {"tool_use_id": tid, "msg_event": p["event_id"],
                                                      "error": redact(t[:300])}, f"msgfail:{tid}", ts)
            w.execute("DELETE FROM pending_sends WHERE tool_use_id=?", (tid,))
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    m = _XSESS_RE.search(text or "")
    if m:
        body = redact(m.group(3))
        cw = parse_cw(body)
        if cw.get("task"):
            ctx = cw["task"]                                   # 팀 내부 수신도 문맥 갱신(C2-5)
        from_sid, from_agent = _resolve_to(w, m.group(1))
        same_team = False
        if from_agent:
            r = w.execute("SELECT 1 FROM agent_sessions WHERE session_id=? AND team_id=?", (from_sid, row["team_id"])).fetchone()
            same_team = bool(r)
        if not same_team:                                      # 팀 안 발신은 보낸 쪽에서 이미 기록
            uid = rec.get("uuid")
            n += _ev(w, row, "msg_in", cw.get("task") or ctx,
                     {"from": m.group(1), "from_name": m.group(2), "from_session": from_sid, "body": body, "cw": cw},
                     f"msgin:{uid}" if uid else None, ts, body)
        return n, ctx
    human = _human_text(rec)
    if human:
        h = redact(human)
        uid = rec.get("uuid")
        # claude 가 기록하는 turnOrigin: 터미널에서 사람이 친 것 = 'human', claude -p(프로그램·다른 세션이 만든 프롬프트) = 'sdk'.
        # 사장님 입력(비서가 습관을 배우는 근거)에는 사람 것만 — 프로그램 입력은 prompt_in 으로 따로 남긴다.
        origin = rec.get("turnOrigin")
        kind = "human_input" if origin in (None, "human") else "prompt_in"
        n += _ev(w, row, kind, ctx, {"text": h, "uuid": uid, "origin": origin}, f"human:{uid}" if uid else None, ts, h)
    return n, ctx


_disk_alerted: list = []


def _disk_alert() -> None:
    if _disk_alerted:
        return
    _disk_alerted.append(1)
    print("[teamlog] 디스크 여유 1GB 미만 - 팀 세션 보존본 쓰기 중단(색인은 계속)", flush=True)
    try:
        from session_manager import push
        push.send("team-disk", "", "ClewPath 팀 보존 중단", "디스크 여유 공간이 1GB 미만이라 팀 세션 원본 보존을 멈췄습니다.", {})
    except Exception:  # noqa: BLE001
        pass


def sessions_to_ingest() -> list[str]:
    """보관되지 않은 팀의 모든 바인딩 세션(교체 전 세션 포함). 다 읽고 사라진 세션은 뺀다."""
    c = team._read()
    try:
        rows = c.execute("SELECT s.session_id FROM agent_sessions s JOIN teams t ON t.id=s.team_id"
                         " LEFT JOIN ingest i ON i.session_id=s.session_id"
                         " WHERE t.archived IS NULL AND (i.gone IS NULL)").fetchall()
        return [r["session_id"] for r in rows]
    finally:
        c.close()


def ingest_all() -> dict:
    if not team.db_path().exists():
        return {"sessions": 0, "events": 0}
    refresh_addresses()
    tot = {"sessions": 0, "events": 0, "read": 0}
    for sid in sessions_to_ingest():
        try:
            r = ingest_session(sid)
            tot["sessions"] += 1
            tot["events"] += r.get("events", 0)
            tot["read"] += r.get("read", 0)
        except Exception as e:  # noqa: BLE001  한 세션 실패가 다른 세션을 막지 않는다
            print(f"[teamlog] 수집 실패 {sid[:8]}: {type(e).__name__}: {e}", flush=True)
    return tot


def final_ingest(session_id: str) -> None:
    """세션 삭제 직전 마지막 꼬리까지(C2-3). 팀 세션이 아니면 아무것도 안 한다. 실패해도 삭제를 막지 않는다."""
    try:
        if not team.db_path().exists():
            return
        c = team._read()
        try:
            bound = c.execute("SELECT 1 FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
        finally:
            c.close()
        if not bound:
            return
        for _ in range(64):                                    # 4MB × 64 = 256MB 까지
            r = ingest_session(session_id)
            if not r.get("read"):
                break
    except Exception as e:  # noqa: BLE001
        print(f"[teamlog] 삭제 전 수집 실패 {session_id[:8]}: {e}", flush=True)


# ---------------------------------------------------------------- 위반 판정(C2-7) + 알림(C2-8)

def _msgs(c, team_id: str) -> list[dict]:
    failed = {r["tid"] for r in c.execute(
        "SELECT json_extract(payload_json,'$.tool_use_id') AS tid FROM events WHERE team_id=? AND kind='msg_failed'", (team_id,))}
    out = []
    for e in c.execute("SELECT id, agent_id, payload_json, COALESCE(src_ts, ts) AS t FROM events"
                       " WHERE team_id=? AND kind='msg' ORDER BY COALESCE(src_ts, ts), id", (team_id,)):
        p = team._uj(e["payload_json"], {})
        if p.get("tool_use_id") in failed:
            continue                                           # 전달 성공만 센다
        cw = p.get("cw") or {}
        out.append({"id": e["id"], "from": e["agent_id"], "to": p.get("to_agent"), "t": e["t"],
                    "task": cw.get("task"), "thread": cw.get("thread"), "type": cw.get("type"),
                    "reply": (cw.get("reply") or "").lower() == "yes"})
    return out


def judge(team_id: str, now: int | None = None) -> list[str]:
    """위반 이벤트를 만든다(이미 있으면 건너뜀). 반환: 새로 만든 위반 키."""
    now = now or team._now()
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        mgr = t["manager_agent"]
        msgs = _msgs(c, t["id"])
    finally:
        c.close()
    found: list[tuple[str, str, dict]] = []
    threads: dict[tuple, list[dict]] = {}
    for m in msgs:
        if m["task"] and m["thread"]:
            threads.setdefault((m["task"], m["thread"]), []).append(m)
    for (task, th), ms in threads.items():
        if len(ms) > MAX_TURNS:
            found.append((f"v:turn:{t['id']}:{task}:{th}", "turn_overrun",
                          {"task": task, "thread": th, "count": len(ms)}))
        asked: dict[str, str] = {}                             # 받은 쪽 → 처음 물어본 쪽
        for m in ms:
            if m["type"] == "ASK" and m["to"] and m["to"] not in asked:
                asked[m["to"]] = m["from"]
            src = asked.get(m["from"])
            if src and m["to"] and m["to"] not in (src, mgr) and m["from"] != src:
                found.append((f"v:fwd:{t['id']}:{task}:{th}", "forward_chain",
                              {"task": task, "thread": th, "from": m["from"], "to": m["to"], "origin": src}))
                break
    open_asks = []
    for i, m in enumerate(msgs):
        if m["type"] != "ASK" or not m["reply"] or not m["to"]:
            continue
        answered = any(x["from"] == m["to"] and x["to"] == m["from"] and x["thread"] == m["thread"] and x["t"] >= m["t"]
                       for x in msgs[i + 1:])
        if answered:
            continue
        open_asks.append(m)
        if now - m["t"] >= UNANSWERED_S:
            found.append((f"v:noans:{m['id']}", "unanswered_ask",
                          {"task": m["task"], "thread": m["thread"], "from": m["from"], "to": m["to"], "since": m["t"]}))
    for a in open_asks:
        for b in open_asks:
            if a["from"] == b["to"] and a["to"] == b["from"] and a["id"] < b["id"] \
                    and now - a["t"] >= DEADLOCK_S and now - b["t"] >= DEADLOCK_S:
                found.append((f"v:dead:{a['id']}:{b['id']}", "deadlock",
                              {"a": a["from"], "b": a["to"], "asks": [a["id"], b["id"]]}))
    if t["kind"] == "portfolio":                               # 비서의 위임 전달 실패는 바로 알린다(A-6)
        c = team._read()
        try:
            for e in c.execute("SELECT id, payload_json FROM events WHERE team_id=? AND kind='msg_failed'", (t["id"],)):
                p = team._uj(e["payload_json"], {})
                found.append((f"v:dfail:{e['id']}", "delivery_failed", {"msg_event": p.get("msg_event")}))
        finally:
            c.close()
    new = []
    with team._Tx() as w:
        for key, kind, payload in found:
            if w.execute("SELECT 1 FROM events WHERE idem_key=?", (key,)).fetchone():
                continue
            team._event(w, t["id"], "violation", payload.get("task"), None, None, key, {"rule": kind, **payload})
            new.append(key)
    return new


_RULE_TEXT = {"turn_overrun": "스레드 턴 초과", "forward_chain": "제3자에게 넘김(1-hop 위반)",
              "unanswered_ask": "답 없는 질문 30분", "deadlock": "서로 답을 기다리는 순환",
              "delivery_failed": "비서 위임 전달 실패"}


def notify_violations(team_id: str) -> int:
    """아직 알리지 않은 위반을 폰 푸시(위반마다 고유 kind — 억제 충돌 없음). 실패면 다음 주기 재시도, 최대 3회."""
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        rows = c.execute("SELECT id, task_id, payload_json, idem_key FROM events WHERE team_id=? AND kind='violation'",
                         (t["id"],)).fetchall()
        done = {r["k"] for r in c.execute("SELECT idem_key AS k FROM events WHERE team_id=? AND kind='violation_notified'",
                                          (t["id"],))}
        tries = {}
        for r in c.execute("SELECT payload_json FROM events WHERE team_id=? AND kind='violation_notify_failed'", (t["id"],)):
            k = team._uj(r["payload_json"], {}).get("violation")
            tries[k] = tries.get(k, 0) + 1
        alias = {r["agent_id"]: r["alias"] for r in c.execute(
            "SELECT agent_id, alias FROM memberships WHERE team_id=? ORDER BY joined", (t["id"],))}
    finally:
        c.close()
    sent = 0
    mgr_sid = team.current_session(t["manager_agent"]) if t["manager_agent"] else ""
    for r in rows:
        key = r["idem_key"]
        if f"vn:{key}" in done or tries.get(key, 0) >= _NOTIFY_TRIES:
            continue
        p = team._uj(r["payload_json"], {})
        who = " → ".join(alias.get(x, "?") for x in (p.get("from") or p.get("a"), p.get("to") or p.get("b")) if x)
        body = " · ".join(x for x in (p.get("task"), p.get("thread"), who) if x)
        try:
            from session_manager import push
            push.send(f"team-violation:{key}", mgr_sid or "", f"[{t['name']}] {_RULE_TEXT.get(p.get('rule'), p.get('rule'))}",
                      body, {"team": t["code"]})
            with team._Tx() as w:
                team._event(w, t["id"], "violation_notified", r["task_id"], None, None, f"vn:{key}", {"violation": key})
            sent += 1
        except Exception as e:  # noqa: BLE001
            with team._Tx() as w:
                team._event(w, t["id"], "violation_notify_failed", r["task_id"], None, None, None,
                            {"violation": key, "error": str(e)[:200]})
    return sent


# ---------------------------------------------------------------- 보존본 읽기·지우기

def archive_lines(session_id: str, offset: int = 0, limit: int = 200) -> dict:
    """보존 원본 줄(가린 뒤). offset 은 '원본 바이트 오프셋' — 청크 이름으로 바로 찾는다(C2-10)."""
    d = archive_root() / re.sub(r"[^0-9A-Za-z\-]", "", session_id)
    if not d.is_dir():
        raise team.TeamError("archive_not_found", 404)
    chunks = []
    for f in d.glob("g*-*.jsonl.gz"):
        m = re.match(r"g(\d+)-(\d+)\.jsonl\.gz$", f.name)
        if m:
            chunks.append((int(m.group(1)), int(m.group(2)), f))
    if not chunks:
        raise team.TeamError("archive_not_found", 404)
    gen = max(g for g, _, _ in chunks)
    chunks = sorted((s, f) for g, s, f in chunks if g == gen)
    lines, next_off = [], None
    limit = max(1, min(int(limit), 2000))
    for start, f in chunks:
        if next_off is None and start + _gz_len(f) <= offset:
            continue
        pos = start
        with gzip.open(f, "rb") as z:
            for raw in z:
                if pos >= offset:
                    if len(lines) >= limit:
                        next_off = pos
                        break
                    lines.append({"offset": pos, "line": raw.decode("utf-8", "replace").rstrip("\n")})
                pos += len(raw)
        if next_off is not None:
            break
    return {"session_id": session_id, "gen": gen, "lines": lines, "next_offset": next_off}


def _gz_len(f: Path) -> int:
    """gzip 원본 길이(끝 4바이트 ISIZE — 4GB 미만 청크라 정확)."""
    try:
        with f.open("rb") as h:
            h.seek(-4, 2)
            return int.from_bytes(h.read(4), "little")
    except OSError:
        return 0


def purge_team(team_id: str) -> dict:
    """'이 팀 보존본 지우기'(S2-5, 사용자 확인 뒤에만): 보존 원본 + 전문(사람 입력·메시지).
    일감·배정·제출·승인 같은 구조 기록·토큰 수(내용 아님)·세션 바인딩은 남는다. 원본 세션 jsonl 은 건드리지 않는다."""
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        sids = [r["session_id"] for r in c.execute("SELECT session_id FROM agent_sessions WHERE team_id=?", (t["id"],))]
    finally:
        c.close()
    removed = 0
    for sid in sids:
        d = archive_root() / sid
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    with team._Tx() as w:
        ids = [r["id"] for r in w.execute("SELECT id FROM events WHERE team_id=? AND kind IN"
                                          " ('human_input','prompt_in','msg','msg_in','msg_failed')", (t["id"],))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = ",".join("?" * len(part))
            try:
                w.execute(f"DELETE FROM texts WHERE event_id IN ({q})", part)
            except Exception:  # noqa: BLE001
                pass
            w.execute(f"DELETE FROM events WHERE id IN ({q})", part)
        team._event(w, t["id"], "archive_purged", payload={"sessions": removed, "events": len(ids)})
    try:                                                       # 그 근거는 사장님 프로필에서도 뺀다(A-8)
        from session_manager import approvals
        approvals.profile_drop_evidence(ids)
    except Exception:  # noqa: BLE001
        pass
    return {"purged_sessions": removed, "purged_events": len(ids)}


# ---------------------------------------------------------------- 경력·장부 읽기(단계 2 읽기 API·화면, 비서의 바닥)

_TOK = "SUM(input) AS i, SUM(output) AS o, SUM(cache_read) AS cr, SUM(cache_create) AS cc"


def _tok_row(r) -> dict:
    i, o, cr, cc = (r["i"] or 0), (r["o"] or 0), (r["cr"] or 0), (r["cc"] or 0)
    return {"input": i, "output": o, "cache_read": cr, "cache_create": cc, "total": i + o + cr + cc}


def _aliases(c, team_id: str) -> dict:
    return {m["agent_id"]: m["alias"] for m in c.execute(
        "SELECT agent_id, alias FROM memberships WHERE team_id=? ORDER BY joined", (team_id,))}


def _agent_ref(c, team_id: str, ref: str) -> str:
    """탈퇴자 포함(경력 조회는 과거 구성원도 — C2-6)."""
    r = c.execute("SELECT agent_id FROM memberships WHERE team_id=? AND (agent_id=? OR alias=?)"
                  " ORDER BY (left_at IS NULL) DESC, joined DESC LIMIT 1", (team_id, ref, ref)).fetchone()
    if not r:
        raise team.TeamError("member_not_found", 404)
    return r["agent_id"]


def member_history(team_id: str, agent_ref: str) -> dict:
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        aid = _agent_ref(c, t["id"], agent_ref)
        a = c.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone()
        sess = [dict(r) for r in c.execute(
            "SELECT session_id, started, ended, reason, team_id FROM agent_sessions"
            " WHERE agent_id=? AND (team_id=? OR team_id IS NULL) ORDER BY started, rowid", (aid, t["id"]))]
        owned = [team._task_view(r) for r in c.execute(
            "SELECT * FROM tasks WHERE team_id=? AND owner_agent=? ORDER BY created", (t["id"], aid))]
        collab = [team._task_view(r) for r in c.execute(
            "SELECT * FROM tasks WHERE team_id=? AND owner_agent<>? AND collaborators_json LIKE ? ORDER BY created",
            (t["id"], aid, f'%"{aid}"%'))]
        kinds = {r["kind"]: r["n"] for r in c.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE team_id=? AND agent_id=? GROUP BY kind", (t["id"], aid))}
        tot = c.execute(f"SELECT {_TOK} FROM usage WHERE team_id=? AND agent_id=?", (t["id"], aid)).fetchone()
        by_task = [{"task": r["task_id"], **_tok_row(r)} for r in c.execute(
            f"SELECT task_id, {_TOK} FROM usage WHERE team_id=? AND agent_id=? GROUP BY task_id", (t["id"], aid))]
        mem = [dict(m) for m in c.execute(
            "SELECT alias, member_role, joined, left_at FROM memberships WHERE team_id=? AND agent_id=? ORDER BY joined",
            (t["id"], aid))]
        return {"agent_id": aid, "alias": a["alias"], "role": a["role"], "tags": team._uj(a["tags_json"], []),
                "memberships": mem, "sessions": sess, "tasks_owned": owned, "tasks_collab": collab,
                "counts": {"submitted": kinds.get("submitted", 0),
                           "accepted": sum(1 for x in owned if x["status"] == "accepted"),
                           "rejected": kinds.get("rejected", 0), "messages_sent": kinds.get("msg", 0),
                           "human_inputs": kinds.get("human_input", 0)},
                "tokens": _tok_row(tot), "tokens_by_task_estimate": by_task}
    finally:
        c.close()


def agent_career(agent_id: str) -> dict:
    """팀을 가로지르는 경력: 소속 이력·일감 상태별 수·세션 수·토큰 합."""
    c = team._read()
    try:
        a = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not a:
            raise team.TeamError("agent_not_found", 404)
        mem = [dict(r) for r in c.execute(
            "SELECT m.team_id, t.code, t.name, m.alias, m.member_role, m.joined, m.left_at"
            " FROM memberships m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? ORDER BY m.joined", (agent_id,))]
        tasks = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE owner_agent=? GROUP BY status", (agent_id,))}
        tot = c.execute(f"SELECT {_TOK} FROM usage WHERE agent_id=?", (agent_id,)).fetchone()
        nsess = c.execute("SELECT COUNT(*) FROM agent_sessions WHERE agent_id=?", (agent_id,)).fetchone()[0]
        return {"agent_id": agent_id, "alias": a["alias"], "role": a["role"], "tags": team._uj(a["tags_json"], []),
                "created": a["created"], "memberships": mem, "tasks_by_status": tasks, "sessions": nsess,
                "tokens": _tok_row(tot)}
    finally:
        c.close()


def timeline(team_id: str, task_id: str) -> dict:
    """일감의 모든 이벤트(배정·제출·메시지·위반·사람 입력)를 원본 시각 순으로 + 토큰 추정."""
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        r = c.execute("SELECT * FROM tasks WHERE id=? AND team_id=?", (task_id, t["id"])).fetchone()
        if not r:
            raise team.TeamError("task_not_found", 404)
        alias = _aliases(c, t["id"])
        evs = []
        for e in c.execute("SELECT id, ts, src_ts, kind, agent_id, payload_json FROM events"
                           " WHERE team_id=? AND task_id=? ORDER BY COALESCE(src_ts, ts), id", (t["id"], task_id)):
            p = team._uj(e["payload_json"], {})
            evs.append({"id": e["id"], "t": e["src_ts"] or e["ts"], "kind": e["kind"], "agent": alias.get(e["agent_id"]),
                        "to": alias.get(p.get("to_agent")) if p.get("to_agent") else None, "payload": p})
        tok = c.execute(f"SELECT {_TOK} FROM usage WHERE team_id=? AND task_id=?", (t["id"], task_id)).fetchone()
        return {**team._task_view(r), "events": evs, "tokens_estimate": _tok_row(tok)}
    finally:
        c.close()


def usage_summary(team_id: str) -> dict:
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        alias = _aliases(c, t["id"])
        by_agent = [{"agent_id": r["agent_id"], "alias": alias.get(r["agent_id"]), **_tok_row(r)} for r in c.execute(
            f"SELECT agent_id, {_TOK} FROM usage WHERE team_id=? GROUP BY agent_id", (t["id"],))]
        by_task = [{"task": r["task_id"] or None, **_tok_row(r)} for r in c.execute(
            f"SELECT task_id, {_TOK} FROM usage WHERE team_id=? GROUP BY task_id", (t["id"],))]
        tot = c.execute(f"SELECT {_TOK} FROM usage WHERE team_id=?", (t["id"],)).fetchone()
        return {"total": _tok_row(tot), "by_agent": by_agent, "by_task_estimate": by_task}
    finally:
        c.close()


def inputs(team_id: str, q: str = "", limit: int = 50, before: int | None = None, kind: str = "human_input") -> dict:
    """사람 입력(또는 메시지) 목록·검색. q 가 3자 이상이면 전문 색인(trigram), 짧으면 LIKE."""
    if kind not in ("human_input", "prompt_in", "msg", "msg_in"):
        raise team.TeamError("bad_kind")
    limit = max(1, min(int(limit), 200))
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        alias = _aliases(c, t["id"])
        args: list = [t["id"], kind]
        sql = "SELECT id, ts, src_ts, agent_id, actor_session, task_id, payload_json FROM events WHERE team_id=? AND kind=?"
        q = (q or "").strip()
        if q:
            ids = None
            if len(q) >= 3:
                try:
                    ids = [r[0] for r in c.execute("SELECT event_id FROM texts WHERE texts MATCH ? AND team_id=?",
                                                   ('"' + q.replace('"', '""') + '"', t["id"]))]
                except Exception:  # noqa: BLE001  FTS 없는 SQLite
                    ids = None
            if ids is None:
                sql += " AND payload_json LIKE ?"
                args.append(f"%{q}%")
            elif not ids:
                return {"items": [], "next_before": None}
            else:
                sql += f" AND id IN ({','.join('?' * len(ids))})"
                args += ids
        if before:
            sql += " AND id < ?"
            args.append(int(before))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        items = []
        for e in c.execute(sql, args):
            p = team._uj(e["payload_json"], {})
            items.append({"id": e["id"], "t": e["src_ts"] or e["ts"], "agent": alias.get(e["agent_id"]),
                          "session_id": e["actor_session"], "task": e["task_id"],
                          "text": p.get("text") or p.get("body") or ""})
        return {"items": items, "next_before": items[-1]["id"] if len(items) == limit else None}
    finally:
        c.close()


def violations(team_id: str) -> list[dict]:
    c = team._read()
    try:
        t = team._team_row(c, team_id)
        alias = _aliases(c, t["id"])
        notified = {r["k"][3:] for r in c.execute(
            "SELECT idem_key AS k FROM events WHERE team_id=? AND kind='violation_notified'", (t["id"],))}
        out = []
        for e in c.execute("SELECT id, ts, task_id, idem_key, payload_json FROM events WHERE team_id=? AND kind='violation'"
                           " ORDER BY id DESC", (t["id"],)):
            p = team._uj(e["payload_json"], {})
            out.append({"id": e["id"], "t": e["ts"], "task": e["task_id"], "rule": p.get("rule"),
                        "text": _RULE_TEXT.get(p.get("rule"), p.get("rule")),
                        "who": [alias.get(x) for x in (p.get("from") or p.get("a"), p.get("to") or p.get("b")) if x],
                        "thread": p.get("thread"), "notified": e["idem_key"] in notified})
        return out
    finally:
        c.close()


def watch_teams() -> dict:
    """감시 스레드 1회분(단계 2): 수집 → 팀마다 위반 판정 → 알림."""
    r = ingest_all()
    v = 0
    for t in team.list_teams():
        try:
            v += len(judge(t["id"]))
            notify_violations(t["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[teamlog] 위반 판정 실패 {t['code']}: {e}", flush=True)
    return {**r, "violations": v}
