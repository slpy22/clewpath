"""에이전트의 지난 세션 대화 찾기·읽기(docs/designs/agent-unit.md '인계 연속성 보강' H5, 판정 K6~K10).

갈아탄 새 세션이 인수인계서에 없는 지난 대화를 스스로 찾게 한다. claude 기록 파일은 **읽기만** 한다.
- 범위: 부른 Claude 세션(바깥문 판별)이 속한 에이전트의 세션만 — 지금 세션 포함(압축 전 앞부분을 되찾는 용도).
  판별 실패·에이전트 밖 세션은 거절한다. session_id 는 그 범위를 좁히는 필터일 뿐이다.
- 대상 글: 사람이 친 말과 Claude 글 답. 도구 출력·생각·압축 요약은 뺀다. 비밀값은 자르기 전에 가린다.
- 기록 파일이 claude 보관 기간으로 지워졌으면 작업 장부(요청 원문 일부·답 요약)에서, 출처를 밝혀서 돌려준다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from session_manager import team
from session_manager.team import TeamError

SEARCH_CHARS = 8000          # 찾기 결과 전체
READ_CHARS = 10000           # 읽기 결과 전체
TARGET_CHARS = 4000          # 읽기에서 대상 글 몫(먼저 배정)
AROUND_CHARS = 1500          # 앞뒤 글 하나의 최대
SNIP = 150                   # 찾기 결과의 일치 앞뒤 글자
LIMIT_MAX = 50
AROUND_MAX = 10
BUDGET_S = 20.0              # 한 번 찾기의 시간 예산 — 넘으면 찾은 데까지(partial)
CACHE_N = 6
LEDGER = "ledger"
RECORD = "record"
ARCHIVE = "archive"
HINT_NONE = "찾은 글이 없습니다. 낱말을 줄이거나 핵심 낱말 하나로 다시 찾아 보세요(모든 낱말이 들어간 글만 찾습니다)."

_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- 글 뽑기
def _extract(path: Path) -> list[dict]:
    from session_manager import teamlog, worklog
    out: list[dict] = []
    with open(path, "rb") as f:
        for bline in f:
            if b'"user"' not in bline and b'"assistant"' not in bline:
                continue
            try:
                rec = json.loads(bline)
            except Exception:  # noqa: BLE001 — 쓰다 만 줄
                continue
            if not isinstance(rec, dict):
                continue
            who, text = "사장님", teamlog._human_text(rec)
            if not text:
                who, text = "Claude", worklog._answer_text(rec)
            if not text:
                continue
            out.append({"n": len(out) + 1, "ts": worklog._epoch(rec.get("timestamp")), "who": who, "text": teamlog.redact(text)})
    return out


def messages(path) -> list[dict]:
    """그 기록 파일의 대화 글(번호·시각·누구·글). (경로·크기·수정 시각)이 같으면 기억해 둔 것을 쓴다."""
    p = Path(path)
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    msgs = _extract(p)
    with _cache_lock:
        for k in [k for k in _cache if k[0] == key[0]]:
            del _cache[k]
        _cache[key] = msgs
        while len(_cache) > CACHE_N:
            _cache.popitem(last=False)
    return msgs


def _ledger(sid: str) -> list[dict]:
    from session_manager import worklog
    out, off = [], 0
    while True:
        r = worklog.session_turns(sid, off, 200)
        rows = r.get("turns") or []
        for t in rows:
            parts = [f"사장님 요청(원문 일부): {t['request']}"] if t.get("request") else []
            if t.get("result"):
                parts.append(f"Claude 마지막 답(요약): {t['result']}")
            out.append({"n": f"t{t['n']}", "ts": t.get("ts"), "who": "장부", "text": "\n".join(parts)})
        if len(rows) < 200:
            return out
        off += 200


def _source(sid: str) -> tuple[str, list[dict]]:
    """원본 → 보관본(K15: 원본의 앞부분 사본이라 글 번호가 같다) → 장부."""
    from session_manager import agent_archive, scanner
    m = scanner.scan_one(sid)
    path = getattr(m, "jsonl_path", None) if m is not None else None
    if path and os.path.isfile(path):
        return RECORD, messages(path)
    arc = agent_archive.path_for(sid)
    if arc is not None:
        return ARCHIVE, messages(arc)
    return LEDGER, _ledger(sid)


# ---------------------------------------------------------------- 범위
def _sessions_of(c, a) -> tuple[dict, list[dict]]:
    rows = c.execute("SELECT session_id, started, ended FROM agent_sessions WHERE agent_id=? AND COALESCE(detached,0)=0 ORDER BY started, rowid",
                     (a["id"],)).fetchall()
    return {"id": a["id"], "name": a["alias"]}, [dict(x) for x in rows]


def scope(caller: str | None) -> tuple[dict, list[dict]]:
    """부른 세션의 에이전트와 그 세션들(시작 순). 판별 실패·에이전트 밖이면 거절."""
    if not caller:
        raise TeamError("caller_unknown", 403)
    c = team._read()
    try:
        r = c.execute("SELECT agent_id, detached FROM agent_sessions WHERE session_id=?", (caller,)).fetchone()
        if not r or r["detached"]:
            raise TeamError("not_an_agent", 403)
        return _sessions_of(c, c.execute("SELECT id, alias FROM agents WHERE id=?", (r["agent_id"],)).fetchone())
    finally:
        c.close()


def scope_agent(agent_ref: str) -> tuple[dict, list[dict]]:
    """사장님 화면용(H8): 그 에이전트의 세션들. 범위는 서버가 에이전트로 정한다(K16)."""
    c = team._read()
    try:
        return _sessions_of(c, team._agent(c, agent_ref))
    finally:
        c.close()


def _ids(caller: str | None, session_id: str = "", agent: str | None = None) -> tuple[dict, list[dict], list[str]]:
    a, sess = scope_agent(agent) if agent else scope(caller)
    ids = [s["session_id"] for s in sess]
    if session_id:
        if session_id not in ids:
            raise TeamError("not_this_agent", 403)
        ids = [session_id]
    return a, sess, ids


# ---------------------------------------------------------------- 찾기
def _snippet(text: str, at: int, width: int) -> str:
    s, e = max(0, at - SNIP), min(len(text), at + width + SNIP)
    return ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")


def search(caller: str | None, query: str, session_id: str = "", limit: int = 20, agent: str | None = None) -> dict:
    a, sess, ids = _ids(caller, session_id, agent)
    terms = [t.lower() for t in str(query or "").split() if t]
    if not terms:
        raise TeamError("empty_query", 400)
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    items, used, partial, truncated, sources = [], 0, False, False, {}
    t0 = time.monotonic()
    for sid in reversed(ids):                                    # 최근 세션부터
        if time.monotonic() - t0 > BUDGET_S:
            partial = True
            break
        src, msgs = _source(sid)
        sources[sid] = src
        for m in reversed(msgs):                                 # 그 안에서도 최근 글부터
            low = m["text"].lower()
            if not all(t in low for t in terms):
                continue
            at = low.find(terms[0])
            snip = _snippet(m["text"], at, len(terms[0]))
            if len(items) >= limit or used + len(snip) > SEARCH_CHARS:
                truncated = True
                break
            used += len(snip)
            items.append({"ref": f"{sid}:{m['n']}", "session_id": sid, "n": m["n"], "ts": m["ts"], "who": m["who"], "source": src,
                          "at": at, "snippet": snip})
        if truncated:
            break
    out = {"agent": a["name"], "query": query, "items": items, "partial": partial, "truncated": truncated,
           "sessions": [{"session_id": s["session_id"], "started": s["started"], "ended": s["ended"],
                         "current": s["session_id"] == caller if caller else s["ended"] is None,
                         "source": sources.get(s["session_id"])} for s in sess],
           "read_with": "agent_history_read(ref, at=결과의 at) 로 앞뒤 글을 읽는다"}
    if any(i["source"] == LEDGER for i in items):
        out["ledger_note"] = "source=ledger 는 기록 파일이 지워져 작업 장부에서 찾은 것 — 요청은 원문 일부, 답은 마지막 답 요약이다."
    if not items:
        out["hint"] = HINT_NONE
    return out


# ---------------------------------------------------------------- 읽기
def _window(text: str, at, size: int) -> tuple[str, int, int]:
    if len(text) <= size:
        return text, 0, len(text)
    at = max(0, min(int(at or 0), len(text)))
    s = max(0, min(at - size // 2, len(text) - size))
    return text[s:s + size], s, s + size


def read(caller: str | None, ref: str, before: int = 2, after: int = 4, at=None, agent: str | None = None) -> dict:
    a, _sess, ids = _ids(caller, agent=agent)
    sid, _, n = str(ref or "").rpartition(":")
    if not sid or sid not in ids:
        raise TeamError("not_this_agent", 403)
    src, msgs = _source(sid)
    idx = next((i for i, m in enumerate(msgs) if str(m["n"]) == n), None)
    if idx is None:
        raise TeamError("ref_not_found", 404)
    before = max(0, min(int(before or 0), AROUND_MAX))
    after = max(0, min(int(after or 0), AROUND_MAX))
    tm = msgs[idx]
    body, s, e = _window(tm["text"], at, TARGET_CHARS)
    picked = {idx: {"text": body, "shown": [s, e], "len": len(tm["text"])}}
    left = READ_CHARS - len(body)
    truncated = e - s < len(tm["text"])
    order = []
    for k in range(1, max(before, after) + 1):                  # 가까운 글부터 앞뒤로 번갈아
        if k <= before and idx - k >= 0:
            order.append(idx - k)
        if k <= after and idx + k < len(msgs):
            order.append(idx + k)
    for i in order:
        if left <= 0:
            truncated = True
            break
        t = msgs[i]["text"]
        cut = t[:min(AROUND_CHARS, left)]
        left -= len(cut)
        picked[i] = {"text": cut, "shown": [0, len(cut)], "len": len(t)}
        truncated = truncated or len(cut) < len(t)
    items = []
    for i in sorted(picked):
        m, p = msgs[i], picked[i]
        items.append({"ref": f"{sid}:{m['n']}", "n": m["n"], "ts": m["ts"], "who": m["who"], "text": p["text"],
                      "shown": p["shown"], "len": p["len"], "target": i == idx})
    last = max(picked)
    out = {"agent": a["name"], "session_id": sid, "source": src, "items": items, "truncated": truncated,
           "next": f"{sid}:{msgs[last + 1]['n']}" if last + 1 < len(msgs) else None,
           "prev": f"{sid}:{msgs[min(picked) - 1]['n']}" if min(picked) > 0 else None}
    if src == LEDGER:
        out["ledger_note"] = "기록 파일이 지워져 작업 장부에서 읽었다 — 요청은 원문 일부, 답은 마지막 답 요약이다."
    return out
