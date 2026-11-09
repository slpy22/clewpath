"""프롬프트로 세션 다루기 — 판단·안전장치는 전부 여기(docs/designs/session-control.md 3·4·11 절).

| 동작 | 처리 |
|---|---|
| 찾기·미리보기 | 바로(읽기만) |
| 이름 변경 | 실행 중 세션은 거절(사람 확인은 MCP 쪽 Claude 확인창, N3) |
| 새 세션 | 권한을 넓히지 않으면 바로(시간당 상한), 넓히면 Host 승인 `session_create_auto` |
| 이동(기록만)·삭제(휴지통) | 미리보기 → Host 승인 `sessions_move`·`sessions_delete`(일괄 1회) |
| 복구 | 바로 |

내용물까지 옮기기는 프롬프트로 하지 않는다(화면 전용). 파일 작업의 주인은 lifecycle·native_title 이고 여기는 정책 층이다.

승인 인자는 **서버가 다시 만든다**(`prepare`): 호출자가 준 제목·요약을 믿지 않아야 폰 화면이 사실을 보여 준다. 일괄 실행은 승인 행에
항목별 진행을 남겨, 도중에 죽어도 끝난 항목을 다시 하지 않는다(`doing` 은 실제 파일 상태로 판정).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from session_manager import launch_options, lifecycle, native_title, scanner, store, team
from session_manager.team import TeamError

API_VERSION = 1
KINDS = ("sessions_move", "sessions_delete", "session_create_auto", "team_root")
MAX_ITEMS = 100              # 한 승인에 세션 수(폰 화면에 다 보여 줄 수 있는 양)
FIND_MAX = 50
CREATE_PER_HOUR = 20         # 승인 없이 만드는 새 세션 상한(잘못 도는 루프 방지)
_BUCKET = re.compile(r"^[A-Za-z0-9._\-]{1,200}$")

_create_lock = threading.Lock()
_creates: deque[float] = deque()


def _title(m) -> str:
    return m.custom_title or m.agent_name or m.ai_title or m.slug or m.session_id[:8]


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def capabilities() -> dict:
    """MCP 가 시작할 때 읽는다 — 도구 설명·판 확인. 옵션 목록의 정본은 launch_options 하나다."""
    return {"api": API_VERSION, "kinds": list(KINDS), "max_items": MAX_ITEMS, "create_per_hour": CREATE_PER_HOUR,
            "options": list(launch_options.LaunchSpec.__dataclass_fields__), "refused": launch_options.REFUSED,
            "narrow_modes": list(launch_options.NARROW_MODES), "wide_modes": list(launch_options.WIDE_MODES),
            "efforts": list(launch_options.EFFORTS), "prompt_max": launch_options.PROMPT_MAX}


def find(query: str = "", cwd: str = "", limit: int = FIND_MAX) -> dict:
    q = (query or "").strip().lower()
    want = (cwd or "").strip()
    out = []
    metas = scanner.scan_all()
    for m in metas:
        if m.headless or m.continued_in:
            continue
        if want and not _same_path(m.cwd, want):
            continue
        t = _title(m)
        if q and q not in t.lower() and q not in m.session_id.lower():
            continue
        out.append({"session_id": m.session_id, "title": t, "cwd": m.cwd, "last_activity": m.last_activity_at or m.ended_at,
                    "live": lifecycle._live_reason(m.session_id)})
    out.sort(key=lambda x: x["last_activity"] or "", reverse=True)
    n = max(1, min(int(limit or FIND_MAX), FIND_MAX))
    return {"sessions": out[:n], "total": len(out), "truncated": len(out) > n}


def rename(session_id: str, name: str, caller: str | None = None) -> dict:
    if caller and session_id == caller:
        raise TeamError("self_rename_use_slash_rename", 409)        # 자기 이름은 /rename — 떠 있는 세션 기록에 끼어 쓰지 않는다
    if lifecycle._live_reason(session_id):      # 떠 있는 세션(쉬는 중 포함) 기록에 끼어 쓰지 않는다 — set_native_title 은 생성 중만 막는다
        raise TeamError("session_live", 409)
    r = native_title.set_native_title(session_id, name)
    if not r.get("ok"):
        raise TeamError(str(r.get("error") or "rename_failed"), 409 if r.get("error") == "session_live" else 400)
    return r


def restore(bucket: str) -> dict:
    if not _BUCKET.match(bucket or "") or bucket.startswith("."):
        raise TeamError("bad_bucket")
    r = lifecycle.restore_session(bucket)
    if r.get("error"):
        raise TeamError("bucket_not_found", 404)
    return r


# ---------------------------------------------------------------- 승인 인자(서버가 다시 만든다)
def _ids(ids) -> list[str]:
    if not isinstance(ids, (list, tuple)) or not ids:
        raise TeamError("missing:ids")
    out = list(dict.fromkeys(str(x).strip().lower() for x in ids if str(x).strip()))
    if not out:
        raise TeamError("missing:ids")
    if len(out) > MAX_ITEMS:
        raise TeamError("too_many_items")
    return out


def _target(new_cwd) -> str:
    raw = str(new_cwd or "").strip()
    if not raw or not Path(raw).is_absolute():
        raise TeamError("target_not_absolute")
    return os.path.normpath(raw)


def _item(sid: str, caller: str | None) -> dict:
    """대상 하나의 지금 상태. ok=False 면 reason 이 이유(승인 대상에서 빠진다)."""
    m = scanner.scan_one(sid)
    if m is None:
        return {"session_id": sid, "ok": False, "reason": "session_not_found"}
    it = {"session_id": sid, "title": _title(m), "cwd": m.cwd, "ok": True, "agent": team.current_agent_name(sid)}
    if caller and sid == caller:
        return {**it, "ok": False, "reason": "self"}
    live = lifecycle._live_reason(sid)
    if live == "handoff":
        return {**it, "ok": False, "reason": "handoff_in_progress"}
    if live:
        return {**it, "ok": False, "reason": "session_live", "live": live}
    return it


def preview_move(ids, new_cwd, caller: str | None = None) -> dict:
    target = _target(new_cwd)
    items = []
    for sid in _ids(ids):
        it = _item(sid, caller)
        if it["ok"] and _same_path(it.get("cwd"), target):
            it = {**it, "ok": False, "reason": "already_there"}
        if it["ok"]:
            d = lifecycle.move_session(sid, target, dry_run=True)
            if d.get("error"):
                it = {**it, "ok": False, "reason": d["error"]}
        items.append(it)
    return {"new_cwd": target, "items": items, "ok": sum(1 for i in items if i["ok"]), "total": len(items)}


def preview_delete(ids, caller: str | None = None) -> dict:
    items = [_item(sid, caller) for sid in _ids(ids)]
    return {"items": items, "ok": sum(1 for i in items if i["ok"]), "total": len(items)}


def _need_ok(pv: dict) -> list[dict]:
    ok = [{k: i.get(k) for k in ("session_id", "title", "cwd", "agent")} for i in pv["items"] if i["ok"]]
    if not ok:
        raise TeamError("nothing_to_do", 409)
    return ok


def _agent_note(items: list[dict]) -> str:
    who = [i["agent"] for i in items if i.get("agent")]
    return f" · 에이전트 {', '.join(who)} 의 지금 세션 포함" if who else ""


def prepare(kind: str, args: dict, caller: str | None = None) -> tuple[dict, str]:
    """승인에 묶을 인자·요약을 서버가 다시 만든다(호출자가 보낸 제목·요약은 쓰지 않는다)."""
    if kind == "sessions_move":
        pv = preview_move(args.get("ids") or [i.get("session_id") for i in args.get("items") or []], args.get("new_cwd"), caller)
        items = _need_ok(pv)
        names = ", ".join(i["title"] for i in items[:3]) + (f" 외 {len(items) - 3}개" if len(items) > 3 else "")
        return {"items": items, "new_cwd": pv["new_cwd"]}, f"세션 {len(items)}개 이동 → {pv['new_cwd']}: {names}{_agent_note(items)}"
    if kind == "sessions_delete":
        pv = preview_delete(args.get("ids") or [i.get("session_id") for i in args.get("items") or []], caller)
        items = _need_ok(pv)
        names = ", ".join(i["title"] for i in items[:3]) + (f" 외 {len(items) - 3}개" if len(items) > 3 else "")
        return {"items": items}, f"세션 {len(items)}개 휴지통으로(30일 복구): {names}{_agent_note(items)}"
    if kind == "session_create_auto":
        cwd = _target(args.get("cwd"))
        if not os.path.isdir(cwd):
            raise TeamError("no_cwd", 404)
        spec = _spec(args.get("options"), args.get("name"))
        prompt = str(args.get("prompt") or "").strip()
        sid = str(uuid.uuid4())
        _check_argv(sid, spec, prompt)
        wide = launch_options.widenings(spec)
        return ({"cwd": cwd, "prompt": prompt, "options": spec.as_dict(), "session_id": sid, "widens": wide},
                f"새 세션({', '.join(wide) or '권한 넓힘'}): {spec.name or Path(cwd).name} · {cwd}")
    if kind == "team_root":                     # 쓰기 범위의 기준이라 Claude 세션은 승인으로만(바깥문이 원시 라우트를 막는다)
        t = team.get_team(str(args.get("team") or ""))
        root = _target(args.get("root"))
        if not os.path.isdir(root):
            raise TeamError("root_not_found", 404)
        return {"team": t["id"], "root": root, "before": t.get("root")}, f"팀 폴더 변경 [{t['code']}]: {t.get('root') or '(없음)'} → {root}"
    raise TeamError("bad_kind")


def request(kind: str, args: dict, caller: str | None = None) -> dict:
    from session_manager import approvals
    a = approvals.create(kind, args, requested_by=caller)
    return {"status": "pending_approval", "approval": a}


def _spec(options, name=None) -> launch_options.LaunchSpec:
    o = dict(options or {})
    if name:
        o["name"] = name
    try:
        return launch_options.parse(o)
    except launch_options.LaunchError as e:
        raise TeamError(f"{e.code}:{e.message}") from e


def _check_argv(sid: str, spec, prompt: str) -> None:
    from session_manager import config
    try:
        launch_options.new_argv(config.claude_exe(), sid, spec, prompt)
    except launch_options.LaunchError as e:
        raise TeamError(f"{e.code}:{e.message}") from e


def trusted(cwd: str) -> bool | None:
    """Claude 폴더 신뢰 표시(~/.claude.json 읽기만, modelprobe 와 같은 범위). 상위 폴더 신뢰도 인정. 못 읽으면 None."""
    try:
        d = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    ok = {os.path.normcase(os.path.normpath(k)) for k, v in (d.get("projects") or {}).items()
          if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}
    p = Path(os.path.normpath(cwd))
    return any(os.path.normcase(str(x)) in ok for x in (p, *p.parents))


def _rate_ok() -> bool:
    now = time.time()
    with _create_lock:
        while _creates and now - _creates[0] > 3600:
            _creates.popleft()
        if len(_creates) >= CREATE_PER_HOUR:
            return False
        _creates.append(now)
        return True


def _start(cwd: str, prompt: str, spec, session_id: str, source: str) -> dict:
    from session_manager import webterm
    try:
        r = webterm.start_new(cwd, prompt, session_id=session_id, launch=spec, source=source)
    except webterm.TermStartError as e:
        raise TeamError(e.code, {"cap": 409, "exists": 409, "no_cwd": 404}.get(e.code, 400)) from e
    t = trusted(cwd)
    if t is False:                 # 대신 답하지 않는다 — 사람이 탭을 열어 고른다
        r = {**r, "needs_trust": True, "note": "처음 쓰는 폴더라 Claude 신뢰 질문에서 멈춰 있습니다. 터미널을 열어 고르세요."}
    return r


def create(cwd, name=None, prompt: str = "", options=None, caller: str | None = None) -> dict:
    """새 세션. 권한을 넓히지 않으면 바로, 넓히면 승인 요청을 돌려준다(이때는 아직 안 만들어진다)."""
    target = _target(cwd)
    if not os.path.isdir(target):
        raise TeamError("no_cwd", 404)
    spec = _spec(options, name)
    prompt = str(prompt or "").strip()
    if prompt.startswith("-"):
        raise TeamError("bad_prompt")
    if launch_options.widenings(spec):
        return request("session_create_auto", {"cwd": target, "name": name, "prompt": prompt, "options": options}, caller)
    sid = str(uuid.uuid4())
    _check_argv(sid, spec, prompt)
    if not _rate_ok():
        raise TeamError("create_rate_limited", 429)
    return _start(target, prompt, spec, sid, "prompt")


# ---------------------------------------------------------------- 일괄 실행(승인 뒤)
def _progress(aid: str) -> dict:
    c = store.reader()
    try:
        r = c.execute("SELECT result_json FROM approvals WHERE id=?", (aid,)).fetchone()
    finally:
        c.close()
    d = team._uj(r["result_json"], {}) if r else {}
    return (d or {}).get("items") or {}


def _save(aid: str, prog: dict) -> None:
    with team._Tx() as c:
        c.execute("UPDATE approvals SET result_json=? WHERE id=?", (team._j({"items": prog}), aid))


def _applied(kind: str, a: dict, sid: str) -> bool:
    """`doing` 으로 남은 항목(실행 중 Host 가 죽음)이 실제로 끝났는지 파일 상태로 판정."""
    m = scanner.scan_one(sid)
    if kind == "sessions_delete":
        return m is None
    return m is not None and _same_path(m.cwd, a["new_cwd"])


def _one(kind: str, a: dict, sid: str) -> dict:
    from session_manager import webterm
    with webterm.session_lock(sid):            # 실행 직전 검사와 변경 사이에 터미널이 뜨지 않게
        live = lifecycle._live_reason(sid)
        if live:
            return {"state": "failed", "reason": "session_live"}
        if kind == "sessions_move":
            r = lifecycle.move_session(sid, a["new_cwd"], move_content=False, dry_run=False)
        else:
            r = lifecycle.delete_session(sid, dry_run=False)
    if r.get("error"):
        return {"state": "failed", "reason": str(r["error"])[:200]}
    out: dict = {"state": "done"}
    if r.get("trash"):
        out["bucket"] = Path(r["trash"]).name   # 복구에 쓴다
    return out


def run_batch(kind: str, a: dict, aid: str) -> dict:
    """항목별 pending→doing→done/failed. 재실행은 done·failed 를 건너뛰고 doing 은 실제 상태로 판정한다."""
    prog = _progress(aid)
    for it in a["items"]:
        sid = it["session_id"]
        st = (prog.get(sid) or {}).get("state")
        if st in ("done", "failed"):
            continue
        if st == "doing" and _applied(kind, a, sid):
            prog[sid] = {"state": "done", "recovered": True}
            _save(aid, prog)
            continue
        prog[sid] = {"state": "doing"}
        _save(aid, prog)
        try:
            prog[sid] = _one(kind, a, sid)
        except Exception as e:  # noqa: BLE001 — 한 항목의 실패가 나머지를 막지 않는다
            prog[sid] = {"state": "failed", "reason": type(e).__name__}
        _save(aid, prog)
    done = sum(1 for v in prog.values() if v.get("state") == "done")
    return {"items": prog, "done": done, "failed": len(a["items"]) - done, "total": len(a["items"])}


def run_create(a: dict, aid: str) -> dict:
    spec = _spec(a.get("options"))
    from session_manager import webterm
    if webterm._get_live(a["session_id"]) is not None or scanner.scan_one(a["session_id"]) is not None:
        return {"status": "already_started", "session_id": a["session_id"]}     # 같은 승인 재실행 = 한 번만
    return _start(a["cwd"], a.get("prompt") or "", spec, a["session_id"], "approved")


def finish(aid: str) -> dict:
    """승인 상태 확인, 승인됐으면 실행하고 결과를 돌려준다. 폴링용이 아니다(사람이 승인한 뒤 한 번)."""
    from session_manager import approvals
    a = approvals.get(aid)
    if a["kind"] not in KINDS:
        raise TeamError("bad_kind")
    if a["status"] == "approved":
        a = approvals.execute(aid)
    return {"approval": aid, "status": a["status"], "summary": a["summary"], "result": a.get("result")}


def recover() -> int:
    """기동 때: 실행 중에 Host 가 죽어 `executing` 으로 남은 일괄 승인을 다시 실행할 수 있게 되돌린다."""
    marks = ",".join("?" * len(KINDS))
    with team._Tx() as c:
        cur = c.execute(f"UPDATE approvals SET status='approved' WHERE status='executing' AND kind IN ({marks})", KINDS)
        return cur.rowcount or 0
