"""외부 서비스 등록 흐름(S3) — docs/designs/service-registry.md §6~8

    서비스 개발 세션(이 PC, 로컬) ─request─▶ 폴더 검사 ─▶ 승인 요청(service_register) ─폰 알림─▶ 사장님 승인(폰/2FA)
    신청자 ─claim(신청 때 받은 비밀)─▶ 원본 준비 + 서비스 등록 + 토큰 1회

원본 준비는 claim 안에서 돈다(approvals.execute 를 claim 문맥으로). 화면의 '실행' 으로는 돌지 않는다 —
토큰 원문이 승인 DB·화면에 남지 않고 신청자에게만 가게.
ClewPath 는 claude 파일을 쓰지 않는다: 새 원본 기록은 claude -p 가 만들고, 이름·표시는 ClewPath 라벨(자체 파일)로,
폴더의 .claude/settings.json 은 검사·경고만 한다.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time

from session_manager import config
from session_manager.services import (ID_RE, PURPOSE_RE, ServiceError, _LOCK, _clean_limits, _hash, _load, _save,
                                      create, get)

_SECRET_PATTERNS = (re.compile(r"^\.env"), re.compile(r"\.(pem|key|pfx|p12)$"), re.compile(r"^id_(rsa|ed25519|ecdsa)"),
                    re.compile(r"^credentials"), re.compile(r"^secrets?([._-]|$)"))
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
_FILE_WARN = 5000
_MAX_DEPTH = 3
BOOT_PROMPT = "'준비 완료' 네 글자만 답하라."
_TL = threading.local()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def _show(p: str) -> str:
    return os.path.abspath(str(p)).replace("\\", "/")


def _under(child: str, parent: str) -> bool:
    """child 가 parent 의 진짜 하위인가(정규화된 경로끼리)."""
    return child != parent and child.startswith(parent + os.sep)


def check_folder(folder: str, adopt: tuple = (), service_id: str | None = None) -> dict:
    """원본 폴더 검사(설계 §7). errors 가 있으면 승인 불가, warnings 는 사장님이 보고 판단."""
    from session_manager import scanner
    if not folder or not os.path.isdir(folder):
        return {"folder": _show(folder or ""), "warnings": [],
                "errors": ["폴더가 없습니다(ClewPath 는 만들지 않습니다 — 서비스가 먼저 만드세요)."]}
    errors: list[str] = []
    warns: list[str] = []
    f = _norm(folder)
    home = _norm(os.path.expanduser("~"))
    ch = _norm(str(config.claude_home()))
    if os.path.dirname(f) == f:
        errors.append("드라이브 루트는 원본 폴더가 될 수 없습니다.")
    if f == home or os.path.dirname(f) == home:
        errors.append("사용자 홈과 그 바로 아래 폴더(바탕화면 등)는 원본 폴더가 될 수 없습니다.")
    if f == ch or _under(f, ch) or _under(ch, f):
        errors.append("claude 데이터 폴더(~/.claude)와 그 위·아래는 원본 폴더가 될 수 없습니다.")
    inside = same = 0
    for s in scanner.scan_all():
        if not s.cwd:
            continue
        c = _norm(s.cwd)
        if _under(c, f):
            inside += 1
        elif c == f and s.session_id not in adopt:
            same += 1
    if inside:
        errors.append(f"이 폴더 아래에서 작업한 다른 세션이 {inside}개 있습니다 — 원본이 그 작업 파일을 읽을 수 있으니 더 안쪽 전용 폴더를 쓰세요.")
    for sid, r in _load()["services"].items():
        if sid != service_id and r.get("status") != "revoked" and r.get("folder") and _norm(r["folder"]) == f:
            errors.append(f"서비스 '{sid}' 가 이미 이 폴더를 씁니다.")
    if same:
        warns.append(f"이 폴더에서 열린 다른 대화가 {same}개 있습니다 — claude 자동 기억(같은 폴더 공용)을 원본도 읽습니다.")
    found, files = [], 0
    for root, dirs, names in os.walk(folder):
        rel = os.path.relpath(root, folder)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS] if depth < _MAX_DEPTH else []
        files += len(names)
        for n in names:
            if any(p.search(n.lower()) for p in _SECRET_PATTERNS):
                found.append(os.path.relpath(os.path.join(root, n), folder).replace("\\", "/"))
        if files > _FILE_WARN:
            break
    if found:
        more = f" 외 {len(found) - 8}개" if len(found) > 8 else ""
        warns.append("비밀값으로 보이는 파일: " + ", ".join(found[:8]) + more)
    for rel in ("CLAUDE.md", ".claude/CLAUDE.md", ".claude/settings.local.json", ".mcp.json"):
        pth = os.path.join(folder, rel)
        if os.path.isfile(pth):
            warns.append(f"{rel} 있음({os.path.getsize(pth)} B) — claude 가 원본에서 자동으로 읽습니다.")
    deny = []
    try:
        with open(os.path.join(folder, ".claude", "settings.json"), encoding="utf-8") as fh:
            deny = (json.load(fh).get("permissions") or {}).get("deny") or []
    except Exception:  # noqa: BLE001
        pass
    if not deny:
        warns.append(".claude/settings.json 에 차단 목록(permissions.deny)이 없습니다 — 홈 .claude·.env 읽기 차단을 권장합니다.")
    if files > _FILE_WARN:
        warns.append(f"파일이 {_FILE_WARN}개를 넘습니다 — 검색 도구로 읽힐 범위가 넓습니다.")
    return {"folder": _show(folder), "errors": errors, "warnings": warns}


def _clean_bases(bases: dict, folder: str) -> tuple[dict, list[str]]:
    """신청의 bases: {용도: {"new": 프로필} | {"adopt": 세션 id}} 검사 → (정리본, 경고들)."""
    from session_manager import profiles
    from session_manager.scanner import scan_one
    if not isinstance(bases, dict) or not bases:
        raise ServiceError("bad_bases", "원본(bases)이 하나 이상 필요합니다. 예: {\"chat\": {\"new\": {\"system_prompt\": \"…\"}}}")
    out, warns = {}, []
    for purpose, spec in bases.items():
        if not isinstance(purpose, str) or not PURPOSE_RE.match(purpose):
            raise ServiceError("bad_bases", f"용도 이름은 영소문자·숫자·'-': {purpose}")
        if not isinstance(spec, dict) or len(spec) != 1 or next(iter(spec)) not in ("new", "adopt"):
            raise ServiceError("bad_bases", f"{purpose}: {{\"new\": 프로필}} 또는 {{\"adopt\": 세션 id}} 중 하나")
        if "adopt" in spec:
            sid = str(spec["adopt"] or "").strip()
            m = scan_one(sid)
            if not m:
                raise ServiceError("base_not_found", f"{purpose}: 세션을 찾을 수 없습니다: {sid[:8]}", 404)
            if not m.cwd or _norm(m.cwd) != _norm(folder):
                raise ServiceError("adopt_folder_mismatch", f"{purpose}: 세션 작업 폴더가 신청 폴더와 다릅니다.")
            if not profiles.get(sid):
                raise ServiceError("base_no_profile", f"{purpose}: 가드레일 프로필이 없는 세션은 입양할 수 없습니다.")
            warns.append(f"{purpose}: 기존 세션 입양 — 대화 기록 {round((m.size_bytes or 0) / 1024)} KB 가 답에 섞일 수 있어 "
                         "새로 만들기를 권장합니다.")
            out[purpose] = {"adopt": sid}
        else:
            prof = {k: v for k, v in (spec["new"] or {}).items()
                    if k in profiles._FIELDS and v not in (None, "", [])}
            if not prof.get("system_prompt"):
                raise ServiceError("bad_profile", f"{purpose}: 새 원본에는 system_prompt 가 필요합니다.")
            if not prof.get("allowed_tools"):
                warns.append(f"{purpose}: 허용 도구 목록이 없습니다 — 막히지 않은 읽기 도구(Read·Grep 등)를 모두 쓸 수 있습니다.")
            prof.setdefault("permission_mode", "dontAsk")     # 목록 밖 도구는 묻지 않고 거부
            out[purpose] = {"new": prof}
    return out, warns


def request(sid: str, name: str, folder: str, bases: dict, purpose: str = "",
            limits: dict | None = None, requested_by: str | None = None) -> dict:
    """등록 신청. 폴더 검사에 걸리면 신청 자체를 거절. 통과하면 승인 요청을 만들고 {request, claim} — claim 은 지금만 보인다."""
    from session_manager import approvals
    sid = (sid or "").strip()
    if not ID_RE.match(sid):
        raise ServiceError("bad_id", "서비스 id 는 영소문자·숫자·'-' 2~31자입니다.")
    cur = _load()["services"].get(sid)
    if cur and cur.get("status") != "revoked":
        raise ServiceError("exists", f"이미 있는 서비스입니다: {sid}", 409)
    chk = check_folder(folder, (), sid)
    if chk["errors"]:
        raise ServiceError("folder_rejected", " / ".join(chk["errors"]))
    clean, bwarns = _clean_bases(bases, folder)
    adopt = tuple(v["adopt"] for v in clean.values() if "adopt" in v)
    if adopt:                                        # 입양 세션 자신은 '같은 폴더의 다른 대화' 에서 뺀다
        chk = check_folder(folder, adopt, sid)
    warnings = bwarns + chk["warnings"]
    args = {"request": "", "service": sid, "name": (name or sid).strip()[:60], "folder": chk["folder"],
            "purpose": (purpose or "").strip()[:200], "limits": _clean_limits(limits), "bases": clean,
            "warnings": warnings, "requested_by": requested_by}
    rid = "sr_" + secrets.token_hex(6)
    args["request"] = rid
    claim_secret = secrets.token_urlsafe(24)
    summary = (f"외부 서비스 등록: {args['name']}({sid}) · 폴더 {chk['folder']} · 원본 {', '.join(clean)}"
               + (f" · 경고 {len(warnings)}개" if warnings else ""))
    ap = approvals.create("service_register", args, summary, requested_by)
    with _LOCK:
        d = _load()
        d["requests"][rid] = {"id": rid, "service": sid, "approval": ap["id"], "claim_hash": _hash(claim_secret),
                              "status": "pending", "created": int(time.time()), "requested_by": requested_by,
                              "provisioned": {}}
        _save(d)
    return {"request": view(rid), "claim": claim_secret,
            "note": "claim 은 지금 한 번만 보입니다. 사장님 승인 뒤 30분 안에 "
                    f"POST /api/services/requests/{rid}/claim 으로 토큰을 받으세요."}


def view(rid: str) -> dict:
    from session_manager import approvals, team
    r = _load()["requests"].get(rid)
    if not r:
        raise ServiceError("request_not_found", "신청이 없습니다.", 404)
    st, args = r.get("status"), {}
    try:
        ap = approvals.get(r["approval"])
        args = ap.get("args") or {}
        if st == "pending":
            st = {"executed": "approved", "executing": "approved"}.get(ap["status"], ap["status"])
    except team.TeamError:
        pass
    return {"id": rid, "service": r["service"], "status": st, "approval": r["approval"], "created": r["created"],
            "folder": args.get("folder"), "warnings": args.get("warnings") or [],
            "bases": r.get("provisioned") or {}, "error": r.get("error")}


def claim(rid: str, claim_secret: str) -> dict:
    """승인된 신청이면 원본을 준비하고 서비스를 만든 뒤 토큰을 한 번 돌려준다."""
    from session_manager import approvals, team
    r = _load()["requests"].get(rid)
    if not r:
        raise ServiceError("request_not_found", "신청이 없습니다.", 404)
    if not hmac.compare_digest(r.get("claim_hash", ""), _hash(str(claim_secret or ""))):
        raise ServiceError("bad_claim", "claim 이 맞지 않습니다.", 403)
    if r.get("status") == "claimed":
        raise ServiceError("claimed", "이미 토큰을 받았습니다. 잃어버렸으면 사장님이 재발급합니다.", 410)
    ap = approvals.get(r["approval"])
    msgs = {"pending": "아직 승인 전입니다.", "rejected": "거절된 신청입니다.",
            "expired": "만료된 신청입니다(30분). 다시 신청하세요.", "executing": "원본을 준비하는 중입니다."}
    if ap["status"] in msgs:
        raise ServiceError(f"not_approved:{ap['status']}", msgs[ap["status"]], 409)
    if ap["status"] == "executed":
        raise ServiceError("token_lost", "서비스는 등록됐지만 토큰 전달이 끊겼습니다. 사장님이 재발급하세요.", 410)
    _TL.ctx = {"approval": r["approval"], "token": None}
    try:
        approvals.execute(r["approval"])
        token = _TL.ctx["token"]
    except team.TeamError as e:
        raise ServiceError(e.code, f"원본 준비 실패: {e.code}", e.status if e.status >= 400 else 409) from e
    finally:
        _TL.ctx = None
    _note(rid, status="claimed", claimed=int(time.time()))
    return {"service": get(r["service"]), "token": token,
            "note": "토큰 원문은 이번 한 번만 보입니다. 서비스 env 에 넣으세요."}


def _note(rid: str, **fields) -> None:
    with _LOCK:
        d = _load()
        if rid in d["requests"]:
            d["requests"][rid].update(fields)
            _save(d)


def make_base(folder: str, prof: dict, timeout: int = 240) -> str:
    """새 원본: 프로필을 먼저 걸고 claude -p --session-id 로 첫 턴부터 가드레일 아래에서 만든다(NKBOT-7).
    세션 기록은 claude 가 만든다 — ClewPath 는 claude 파일을 쓰지 않는다."""
    import subprocess
    import uuid
    from session_manager import policy, profiles, ptyd, webapi
    sid = str(uuid.uuid4())
    profiles.set_record(sid, **prof)
    argv = [config.claude_exe(), "-p", "--session-id", sid, "--output-format", "json"] + \
        webapi._guardrail_args(policy.effective_guardrails(sid), True, resume=False)   # 새 세션 — 비용 상한이 제대로 듣는다
    try:
        r = subprocess.run(argv, input=BOOT_PROMPT, cwd=folder, env=ptyd.clean_env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.returncode == 0 and json.loads(r.stdout or "{}").get("session_id") == sid
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        profiles.delete(sid)
        raise RuntimeError("base_create_failed")
    return sid


def provision(args: dict, aid: str) -> dict:
    """service_register 실행기 — claim 문맥에서만(토큰을 그 응답으로만 건넨다)."""
    from session_manager import labels, team
    ctx = getattr(_TL, "ctx", None)
    if not ctx or ctx.get("approval") != aid:
        raise team.TeamError("claim_required", 409)      # 화면의 '실행' 으로는 토큰이 사라지므로 막는다
    rid, sid, folder = args["request"], args["service"], args["folder"]
    adopt = tuple(v["adopt"] for v in args["bases"].values() if "adopt" in v)
    chk = check_folder(folder, adopt, sid)
    if chk["errors"]:                                      # 승인 사이 폴더가 바뀌었으면 중단
        _note(rid, error="; ".join(chk["errors"]))
        raise team.TeamError("folder_rejected", 409)
    done = dict(_load()["requests"].get(rid, {}).get("provisioned") or {})
    for purpose, spec in args["bases"].items():
        if purpose in done:
            continue                                       # 다시 시도 — 이미 만든 원본은 다시 만들지 않는다
        if "adopt" in spec:
            done[purpose] = spec["adopt"]
        else:
            try:
                done[purpose] = make_base(folder, spec["new"])
            except RuntimeError:
                _note(rid, provisioned=done, error=f"{purpose} 원본 만들기 실패")
                raise team.TeamError("base_create_failed", 502)
        _note(rid, provisioned=done)
        labels.set_record(done[purpose], labels=[f"서비스: {args['name']}"], name=f"{args['name']} 원본({purpose})")
    try:
        _, token = create(sid, args["name"], done, args.get("limits"),
                          extra={"folder": folder, "purpose": args.get("purpose", ""), "approval": aid,
                                 "requested_by": args.get("requested_by")})
    except ServiceError as e:
        _note(rid, error=e.message)
        raise team.TeamError(e.code, e.status)
    ctx["token"] = token
    _note(rid, error=None)
    return {"service": sid, "bases": done}
