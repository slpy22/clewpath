"""승인 요청·비서·사장님 프로필 — 개인 비서 단계 3(docs/designs/workers-p2p.md §단계 3, A-1~A-10).

왜 서버 강제인가(사장님 결정 S3-4): 대화 속 '응' 은 서버가 확인할 수 없고, PC 안의 어떤 세션(워커 포함)도
API 를 부를 수 있으며, 장부 속 문구가 비서를 속일 수 있다. 그래서 영향이 큰 실행은:

    비서/관리 세션 ──create(종류·인자·요약)──▶ approvals[pending] ──폰 푸시──▶ 사장님
                                                      │  decide: 사람 증명 필수
                                                      │   · 폰 = 커넥터 메모리 비밀 + 인증 기기(로컬 위조 불가)
                                                      │   · PC = 2차 인증 코드
                                                      ▼
                                                approved ──execute(1회, 저장된 인자로 서버가 실행)──▶ executed
                                       (30분 지나면 expired, 거절이면 rejected — 둘 다 실행 불가)

일상 흐름(관리 세션의 배정·구성원 추가, 워커의 제출)은 승인 없이 그대로(6B, A-4).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from pathlib import Path

from session_manager import config, team

TTL_S = 30 * 60
KINDS = ("team_create", "team_import", "team_archive", "team_purge", "assistant_set", "delegate", "task_decide", "profile_decide")
_ALIAS_ASSISTANT = "비서"
_PORTFOLIO_CODE = "ASSIST"


def _now() -> int:
    return team._now()


# ---------------------------------------------------------------- 승인 요청

def _summary(kind: str, a: dict) -> str:
    if kind == "team_create":
        ms = ", ".join(m.get("alias", "?") for m in (a.get("members") or []))
        return f"팀 만들기: {a.get('name')}" + (f" (구성원 {ms})" if ms else "")
    if kind == "team_import":
        return f"v1 등록부로 팀 만들기: {a.get('path')}"
    if kind == "team_archive":
        return f"팀 {'보관' if a.get('on', True) else '보관 해제'}: {a.get('team')}"
    if kind == "team_purge":
        return f"팀 보존본 지우기: {a.get('team')}"
    if kind == "assistant_set":
        return "새 비서 세션 만들기" if a.get("create") else f"비서 세션 지정: {str(a.get('session_id'))[:8]}"
    if kind == "delegate":
        return f"[{a.get('team')}] 일감 위임: {a.get('goal')}" + (f" → {a.get('owner')}" if a.get("owner") else "")
    if kind == "task_decide":
        return f"[{a.get('team')}] {a.get('task')} " + {"accept": "승인", "reject": "반려", "cancel": "취소"}.get(a.get("action"), "?")
    if kind == "profile_decide":
        return f"사장님 프로필 {a.get('decision')}: {str(a.get('statement') or a.get('id'))[:60]}"
    return kind


def _validate(kind: str, a: dict) -> None:
    need = {"team_create": ("name",), "team_import": ("path",), "team_archive": ("team",), "team_purge": ("team",), "assistant_set": (),
            "delegate": ("team", "goal"), "task_decide": ("team", "task", "action"), "profile_decide": ("id", "decision")}
    if kind not in KINDS:
        raise team.TeamError("bad_kind")
    for k in need[kind]:
        if not a.get(k):
            raise team.TeamError(f"missing:{k}")
    if kind == "assistant_set" and not (a.get("create") or a.get("session_id")):
        raise team.TeamError("missing:session_id")
    if kind == "task_decide" and a.get("action") not in ("accept", "reject", "cancel"):
        raise team.TeamError("bad_action")
    if kind == "profile_decide" and a.get("decision") not in ("confirm", "reject", "edit", "delete"):
        raise team.TeamError("bad_decision")


def _view(r) -> dict:
    exp = r["status"] == "pending" and r["expires"] < _now()
    return {"id": r["id"], "kind": r["kind"], "args": team._uj(r["args_json"], {}), "summary": r["summary"],
            "requested_by": r["requested_by"], "created": r["created"], "expires": r["expires"],
            "status": "expired" if exp else r["status"], "decided_at": r["decided_at"], "decided_via": r["decided_via"],
            "executed_at": r["executed_at"], "result": team._uj(r["result_json"], None)}


def create(kind: str, args: dict, summary: str = "", requested_by: str | None = None) -> dict:
    args = dict(args or {})
    _validate(kind, args)
    aid = "ap_" + secrets.token_hex(6)
    s = team._clean(summary or _summary(kind, args), 300)
    with team._Tx() as c:
        c.execute("INSERT INTO approvals(id,kind,args_json,summary,requested_by,created,expires,status)"
                  " VALUES(?,?,?,?,?,?,?,?)", (aid, kind, team._j(args), s, requested_by, _now(), _now() + TTL_S, "pending"))
    try:                                      # 사장님 폰으로 — 알림을 누르면 #approval=<id> 승인 화면
        from session_manager import push
        push.send(f"approval:{aid}", requested_by or "", "승인 요청", s, {"approval": aid})
    except Exception:  # noqa: BLE001
        pass
    return get(aid)


def get(aid: str) -> dict:
    c = team._read()
    try:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            raise team.TeamError("approval_not_found", 404)
        return _view(r)
    finally:
        c.close()


def list_(status: str | None = "pending", limit: int = 50) -> list[dict]:
    c = team._read()
    try:
        if status == "pending":
            rows = c.execute("SELECT * FROM approvals WHERE status='pending' AND expires>=? ORDER BY created DESC LIMIT ?",
                             (_now(), int(limit))).fetchall()
        elif status:
            rows = c.execute("SELECT * FROM approvals WHERE status=? ORDER BY created DESC LIMIT ?", (status, int(limit))).fetchall()
        else:
            rows = c.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT ?", (int(limit),)).fetchall()
        return [_view(r) for r in rows]
    finally:
        c.close()


def decide(aid: str, approve: bool, proof: str | None) -> dict:
    """사람 증명(proof)이 있어야만. proof 는 서버가 만든 값('phone:<기기>'·'otp') — 요청 본문에서 받지 않는다."""
    if not proof:
        raise team.TeamError("human_proof_required", 403)
    with team._Tx() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            raise team.TeamError("approval_not_found", 404)
        if r["status"] != "pending":
            raise team.TeamError(f"not_pending:{r['status']}", 409)
        if r["expires"] < _now():
            c.execute("UPDATE approvals SET status='expired' WHERE id=?", (aid,))
            raise team.TeamError("expired", 409)
        c.execute("UPDATE approvals SET status=?, decided_at=?, decided_via=? WHERE id=?",
                  ("approved" if approve else "rejected", _now(), proof, aid))
    return get(aid)


def execute(aid: str) -> dict:
    """승인된 요청을 저장된 인자 그대로 1회 실행. 실행 중 실패하면 상태는 approved 로 남아(인자 고정) 다시 시도할 수 있다."""
    with team._Tx() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            raise team.TeamError("approval_not_found", 404)
        if r["status"] == "executed":
            return _view(r)                                    # 재시도 = 같은 결과(멱등)
        if r["status"] == "executing":
            raise team.TeamError("executing", 409)
        if r["status"] != "approved":
            raise team.TeamError(f"not_approved:{r['status']}", 409)
        if r["expires"] < _now():
            raise team.TeamError("expired", 409)
        kind, args = r["kind"], team._uj(r["args_json"], {})
        c.execute("UPDATE approvals SET status='executing' WHERE id=?", (aid,))   # 동시 실행 차단(먼저 잡은 쪽만)
    try:
        result = _EXEC[kind](args, aid, r["requested_by"])
    except Exception:
        with team._Tx() as c:                                  # 실패 → 다시 시도 가능(인자는 그대로 고정)
            c.execute("UPDATE approvals SET status='approved' WHERE id=? AND status='executing'", (aid,))
        raise
    with team._Tx() as c:
        c.execute("UPDATE approvals SET status='executed', executed_at=?, result_json=? WHERE id=?",
                  (_now(), team._j(result), aid))
    return get(aid)


# ---------------------------------------------------------------- 실행기

def _x_team_create(a: dict, aid: str, by: str | None) -> dict:
    t = team.create_team(a["name"], root=a.get("root"), code=a.get("code"), manager_session=a.get("manager_session"),
                         manager_alias=a.get("manager_alias") or "관리")
    added = []
    for m in a.get("members") or []:
        team.add_member(t["id"], m.get("alias", ""), role=m.get("role", ""), tags=m.get("tags"),
                        session_id=m.get("session_id"), write_scope=m.get("write_scope"))
        added.append(m.get("alias"))
    sync = team.sync_group(t["id"])
    return {"team": t["id"], "code": t["code"], "members": added, **sync}


def _x_team_import(a: dict, aid: str, by: str | None) -> dict:
    r = team.import_v1(a["path"], a.get("name"))
    sync = team.sync_group(r["team"]["id"])
    return {"team": r["team"]["id"], "code": r["team"]["code"], "added": r["added"], "skipped": r["skipped"], **sync}


def _x_team_archive(a: dict, aid: str, by: str | None) -> dict:
    return team.archive(a["team"], bool(a.get("on", True)))


def _x_team_purge(a: dict, aid: str, by: str | None) -> dict:
    from session_manager import teamlog
    return teamlog.purge_team(a["team"])


def _x_assistant_set(a: dict, aid: str, by: str | None) -> dict:
    sid = a.get("session_id")
    if a.get("create"):
        sid = create_assistant_session()
    return set_assistant(sid)


def _x_delegate(a: dict, aid: str, by: str | None) -> dict:
    t = team.get_team(a["team"])
    owner = a.get("owner")
    if not owner:
        mgr = next((m for m in t["members"] if m["member_role"] == "manager"), None)
        if not mgr:
            raise team.TeamError("no_manager", 409)
        owner = mgr["agent_id"]
    k = team.create_task(t["id"], a["goal"], owner, collaborators=a.get("collaborators"), write_scope=a.get("write_scope"),
                         done_when=a.get("done_when", ""), due=a.get("due"), actor_session=by, idem_key=f"ap:{aid}")
    m = team.member(t["id"], k["owner"])
    return {"task": k, "send_to": {"alias": m["alias"], "address": m["address"], "live": m["live"],
                                   "session_id": m["session_id"]}}


def _x_task_decide(a: dict, aid: str, by: str | None) -> dict:
    return team.transition(a["team"], a["task"], a["action"], actor_session=by, idem_key=f"ap:{aid}",
                           assignment_ver=a.get("assignment_ver"), note=a.get("note", ""))


def _x_profile_decide(a: dict, aid: str, by: str | None) -> dict:
    return profile_decide(a["id"], a["decision"], a.get("statement"))


_EXEC = {"team_create": _x_team_create, "team_import": _x_team_import, "team_archive": _x_team_archive, "team_purge": _x_team_purge,
         "assistant_set": _x_assistant_set, "delegate": _x_delegate, "task_decide": _x_task_decide,
         "profile_decide": _x_profile_decide}


# ---------------------------------------------------------------- 비서(A-7)

def assistant_dir() -> Path:
    return config.data_dir() / "assistant"


_BOOT = ("너는 사장님의 개인 비서다. ClewPath 팀 명부(http://127.0.0.1:5100/api/v1/team)로 모든 팀과 에이전트의 이력을 "
         "보고, clewpath-assistant 스킬의 규칙을 따른다. 분석·제안은 자유, 실행은 승인 요청으로만. 지금은 'ready' 라고만 답하라.")


def create_assistant_session(timeout: int = 180) -> str:
    """새 비서 세션: 데이터 폴더 assistant/ 에서 claude -p 1회(claude 가 새 세션을 만든다 — ClewPath 는 파일을 쓰지 않음).
    처음 터미널로 열 때 '폴더 신뢰' 질문은 사장님이 답한다."""
    d = assistant_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([config.claude_exe(), "-p", "--output-format", "json", _BOOT], cwd=str(d),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sid = json.loads(r.stdout or "{}").get("session_id")
    except Exception as e:  # noqa: BLE001
        raise team.TeamError(f"assistant_create_failed:{type(e).__name__}", 502) from e
    if not sid:
        raise team.TeamError("assistant_create_failed", 502)
    return str(sid)


def set_assistant(session_id: str) -> dict:
    """비서 지정. 처음이면 비서 에이전트 + 포트폴리오 팀(수집·보존 대상, A-6), 이미 있으면 같은 비서에 새 세션(A-7)."""
    session_id = str(session_id or "").strip()
    if not session_id:
        raise team.TeamError("missing:session_id")
    c = team._read()
    try:
        row = c.execute("SELECT * FROM assistant WHERE id=1").fetchone()
        own = c.execute("SELECT agent_id FROM agent_sessions WHERE session_id=?", (session_id,)).fetchone()
    finally:
        c.close()
    if row:
        if own and own["agent_id"] != row["agent_id"]:
            raise team.TeamError("session_owned_by_other_agent", 409)
        team.bind_session(row["team_id"], row["agent_id"], session_id, reason="replaced")
        return assistant_info()
    if own:
        raise team.TeamError("session_owned_by_other_agent", 409)
    t = team.create_team("비서", code=_PORTFOLIO_CODE if not _code_taken(_PORTFOLIO_CODE) else None, kind="portfolio")
    m = team.add_member(t["id"], _ALIAS_ASSISTANT, role="개인 비서", session_id=session_id, member_role="manager")
    with team._Tx() as w:
        w.execute("INSERT INTO assistant(id,agent_id,team_id,set_at) VALUES(1,?,?,?)", (m["agent_id"], t["id"], _now()))
        w.execute("UPDATE agents SET role='assistant' WHERE id=?", (m["agent_id"],))
        w.execute("UPDATE teams SET parent=? WHERE parent IS NULL AND kind='team'", (m["agent_id"],))   # 기존 팀도 포트폴리오로
    return assistant_info()


def _code_taken(code: str) -> bool:
    c = team._read()
    try:
        return bool(c.execute("SELECT 1 FROM teams WHERE code=?", (code,)).fetchone())
    finally:
        c.close()


def assistant_info() -> dict | None:
    """쓰기 없는 순수 읽기(원격 헤더 버튼용, A-7): 비서의 지금 세션·살아 있음. 없으면 None."""
    if not team.db_path().exists():
        return None
    c = team._read()
    try:
        row = c.execute("SELECT a.agent_id, a.team_id, t.code FROM assistant a JOIN teams t ON t.id=a.team_id WHERE a.id=1").fetchone()
        if not row:
            return None
        s = c.execute("SELECT session_id FROM agent_sessions WHERE agent_id=? ORDER BY started DESC, rowid DESC LIMIT 1",
                      (row["agent_id"],)).fetchone()
    finally:
        c.close()
    sid = s["session_id"] if s else None
    if sid:
        sid = team._latest_session(sid)                      # continued-in 끝(읽기만 — 바인딩은 감시 스레드가)
    live = bool(team._peer_by_session().get(sid or ""))
    return {"agent_id": row["agent_id"], "team": row["code"], "session_id": sid, "live": live}


# ---------------------------------------------------------------- 사장님 프로필(A-8)

def _hash(statement: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", statement.strip().lower()).encode("utf-8")).hexdigest()[:20]


def _pview(r) -> dict:
    return {"id": r["id"], "topic": r["topic"], "statement": r["statement"], "evidence": team._uj(r["evidence_json"], []),
            "confidence": r["confidence"], "status": r["status"], "source": r["source"],
            "created": r["created"], "updated": r["updated"]}


def profile_list(status: str | None = None) -> list[dict]:
    c = team._read()
    try:
        if status:
            rows = c.execute("SELECT * FROM profile WHERE status=? ORDER BY updated DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM profile WHERE status IN ('proposed','confirmed') ORDER BY status, updated DESC").fetchall()
        return [_pview(r) for r in rows]
    finally:
        c.close()


def profile_propose(statement: str, topic: str = "", evidence=None, confidence: float = 0.5,
                    source: str = "assistant") -> dict:
    """비서의 제안(승인 불필요 — 확정은 사장님 승인으로). 거절·삭제된 문장은 다시 제안 불가."""
    from session_manager import teamlog
    st = teamlog.redact(team._clean(statement, 500))
    if not st:
        raise team.TeamError("statement_required")
    h = _hash(st)
    ev = [int(x) for x in (evidence or []) if str(x).isdigit()][:20]
    with team._Tx() as c:
        old = c.execute("SELECT * FROM profile WHERE stmt_hash=?", (h,)).fetchone()
        if old:
            if old["status"] in ("rejected", "deleted"):
                raise team.TeamError("previously_rejected", 409)
            return _pview(old)
        pid = "pf_" + secrets.token_hex(5)
        c.execute("INSERT INTO profile(id,topic,statement,evidence_json,confidence,status,source,stmt_hash,created,updated)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, team._clean(topic, 60), st, team._j(ev),
                                                  max(0.0, min(1.0, float(confidence or 0))), "proposed", source, h, _now(), _now()))
        return _pview(c.execute("SELECT * FROM profile WHERE id=?", (pid,)).fetchone())


def profile_decide(pid: str, decision: str, statement: str | None = None) -> dict:
    """사장님 결정(승인 요청을 거쳐서만 호출된다): 확정·거절·수정(수정본은 사장님 것)·삭제(문장은 지우고 해시만 남겨 재제안 차단)."""
    from session_manager import teamlog
    with team._Tx() as c:
        r = c.execute("SELECT * FROM profile WHERE id=?", (pid,)).fetchone()
        if not r:
            raise team.TeamError("profile_not_found", 404)
        if decision == "confirm":
            c.execute("UPDATE profile SET status='confirmed', updated=? WHERE id=?", (_now(), pid))
        elif decision == "reject":
            c.execute("UPDATE profile SET status='rejected', updated=? WHERE id=?", (_now(), pid))
        elif decision == "delete":
            c.execute("UPDATE profile SET status='deleted', statement='', evidence_json='[]', updated=? WHERE id=?", (_now(), pid))
        elif decision == "edit":
            st = teamlog.redact(team._clean(statement, 500))
            if not st:
                raise team.TeamError("statement_required")
            c.execute("UPDATE profile SET statement=?, stmt_hash=?, status='confirmed', source='owner', updated=? WHERE id=?",
                      (st, _hash(st), _now(), pid))
        else:
            raise team.TeamError("bad_decision")
        return _pview(c.execute("SELECT * FROM profile WHERE id=?", (pid,)).fetchone())


def profile_drop_evidence(event_ids) -> int:
    """팀 보존본 지우기 시 그 근거를 프로필에서 뺀다(A-8)."""
    ids = {int(x) for x in event_ids}
    if not ids:
        return 0
    n = 0
    with team._Tx() as c:
        for r in c.execute("SELECT id, evidence_json FROM profile").fetchall():
            ev = team._uj(r["evidence_json"], [])
            kept = [x for x in ev if x not in ids]
            if len(kept) != len(ev):
                c.execute("UPDATE profile SET evidence_json=? WHERE id=?", (team._j(kept), r["id"]))
                n += 1
    return n


def search_all(q: str, kind: str = "human_input", limit: int = 50) -> dict:
    """팀을 가로질러 사람 입력·메시지 검색(비서용). 보관 팀 포함."""
    out = []
    for t in team.list_teams(include_archived=True):
        try:
            from session_manager import teamlog
            for it in teamlog.inputs(t["id"], q=q, limit=limit, kind=kind)["items"]:
                out.append({**it, "team": t["code"]})
        except team.TeamError:
            continue
    out.sort(key=lambda x: x.get("t") or 0, reverse=True)
    return {"items": out[:max(1, min(int(limit), 200))]}
