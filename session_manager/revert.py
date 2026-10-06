"""이어하기 되돌리기 — 실수로 생긴 '이어받은 세션' 을 지우고 원래 세션을 그대로 쓰게 한다(2026-10-06 사장님 요청).

언제 생기나:
- 터미널에서 **빈 입력칸에 ←** → Claude Code 가 대화를 백그라운드로 보내며 새 세션 id 로 **복사**하고,
  원래 파일 끝에 `{"type":"continued-in","continuedInSessionId":<새 id>}` 를 남긴다(daemon.log `bg spawned`).
- 대화가 커져 요약을 들고 새 세션으로 이어 가는 경우도 같은 표시를 남긴다.

되돌리기(버튼 하나 + 확인창):
  ① 사본을 살려 둔 프로세스 종료(백그라운드 에이전트 = `claude stop`, ClewPath 터미널 = stop, 그 밖 = 강제 종료)
  ② 사본을 ClewPath 휴지통으로(30일 복구 가능)
  ③ 팀 명부: 사본의 흔적(바인딩·중복 사용량·중복 입력)을 지우고 원래 세션을 다시 현재 세션으로
  ④ 원래 세션 파일에서 그 continued-in **한 줄만** 제거 — 원본은 ②의 휴지통 버킷에 백업
     (claude 파일 수정: 불가침 원칙의 승인 예외, CLAUDE.md 2026-10-06 — 사용자 버튼 + 확인창 경유만)

사본에서 새로 진행된 대화가 있으면(원래 파일에 없는 메시지) 미리보기에 개수·마지막 입력을 보여 준다 — 그것도 휴지통으로 간다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from session_manager import scanner

_CONT = b'continued-in'
_UUID_RE = re.compile(rb'"uuid":\s*"([0-9a-f-]{36})"')
_BG_RE = re.compile(rb'"sessionKind":\s*"bg"')
_MSG_RE = re.compile(rb'"type":\s*"(?:user|assistant)"')


def _pair(session_id: str) -> tuple[str, str] | None:
    """(원래 세션, 이어받은 세션). session_id 는 둘 중 어느 쪽이든 된다."""
    m = scanner.scan_one(session_id)
    if m is None:
        return None
    if m.continued_in and scanner.scan_one(m.continued_in) is not None:
        return session_id, m.continued_in
    for s in scanner.scan_all():
        if s.continued_in == session_id:
            return s.session_id, session_id
    return None


def _uuids(path: Path) -> set[str]:
    out: set[str] = set()
    with open(path, "rb") as fh:
        for line in fh:
            for u in _UUID_RE.findall(line):
                out.add(u.decode("ascii", "replace"))
    return out


def _text(o: dict) -> str:
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text") or "" for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")


def _divergence(old_path: Path, new_path: Path) -> dict:
    """사본에만 있는 대화(원래 파일에 없는 user/assistant 메시지)."""
    base = _uuids(old_path)
    kind = "continued"
    new_msgs = 0
    inputs: list[dict] = []
    with open(new_path, "rb") as fh:
        for line in fh:
            if _BG_RE.search(line):
                kind = "background"
            if not _MSG_RE.search(line):
                continue
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if o.get("type") not in ("user", "assistant") or o.get("uuid") in base:
                continue
            new_msgs += 1
            if o.get("type") == "user" and not o.get("isMeta"):
                t = _text(o).strip()
                if t and not t.startswith("<"):
                    inputs.append({"ts": o.get("timestamp"), "text": t[:120]})
    return {"kind": kind, "new_messages": new_msgs, "new_inputs": len(inputs), "last_inputs": inputs[-3:]}


def _process(session_id: str) -> dict:
    """그 세션을 지금 살려 둔 것: none | background | clewpath | interactive."""
    from session_manager import agents, peers, webterm
    if webterm.has_terminal(session_id):
        return {"kind": "clewpath"}
    occ = agents.occupancy_map().get(session_id)
    if occ and occ.get("kind") == "background":
        return {"kind": "background", "pid": occ.get("pid"), "status": occ.get("status")}
    p = peers.peer_map().get(session_id)
    if p or occ:
        return {"kind": "interactive", "pid": (p or occ or {}).get("pid")}
    return {"kind": "none"}


def _title(m) -> str:
    return (m.custom_title or m.ai_title or m.slug or m.session_id[:8]) if m else ""


def plan(session_id: str) -> dict:
    pr = _pair(session_id)
    if not pr:
        return {"ok": False, "error": "no_continuation",
                "message": "되돌릴 이어받기가 없습니다(이어받은 세션이 없거나 이미 지워졌습니다)."}
    old, new = pr
    mo, mn = scanner.scan_one(old), scanner.scan_one(new)
    if mn.continued_in and scanner.scan_one(mn.continued_in) is not None:
        return {"ok": False, "error": "chain", "old": old, "new": new,
                "message": f"이어받은 세션이 또 {mn.continued_in[:8]}… 로 이어졌습니다. 마지막 이어받기부터 되돌리세요."}
    po, pn = _process(old), _process(new)
    if po["kind"] != "none":
        return {"ok": False, "error": "old_live", "old": old, "new": new,
                "message": "원래 세션이 지금 실행 중입니다. 원래 세션을 먼저 종료한 뒤 되돌리세요."}
    div = _divergence(Path(mo.jsonl_path), Path(mn.jsonl_path))
    from session_manager import team
    try:
        teams = team.continuation_bindings(new)
    except Exception:  # noqa: BLE001
        teams = []
    stop_how = {"none": None, "background": "백그라운드 에이전트 종료(claude stop)",
                "clewpath": "ClewPath 터미널 종료", "interactive": "열려 있는 claude 프로세스 강제 종료"}[pn["kind"]]
    return {
        "ok": True, "old": old, "new": new,
        "old_title": _title(mo), "new_title": _title(mn),
        "kind": div["kind"],                     # background = ← 로 생긴 사본, continued = 요약 이어가기 등
        "new_process": pn, "stop": stop_how,
        "new_messages": div["new_messages"], "new_inputs": div["new_inputs"], "last_inputs": div["last_inputs"],
        "new_bytes": mn.size_bytes, "teams": teams,
        "steps": [s for s in (
            stop_how and f"사본 {new[:8]} 의 실행 중지: {stop_how}",
            f"사본 {new[:8]} 를 휴지통으로 이동(30일 안 복구 가능)",
            teams and "팀 명부: " + ", ".join(f"{t.get('team_name') or t.get('team_id')}/{t.get('alias')}" for t in teams)
            + " 를 원래 세션으로 되돌리고 사본의 중복 기록 정리",
            f"원래 세션 {old[:8]} 파일에서 '이어짐' 표시 한 줄 제거(원본은 휴지통에 백업)",
        ) if s],
    }


def _stop(session_id: str, how: dict) -> dict:
    from session_manager import agents, lifecycle, webterm
    k = how.get("kind")
    if k == "clewpath":
        return {"how": k, "ok": webterm.stop_terminal(session_id)}
    if k == "background":
        exe = shutil.which("claude")
        ok = False
        if exe:
            r = subprocess.run([exe, "stop", session_id[:8]], capture_output=True, timeout=30)
            ok = r.returncode == 0
        deadline = time.time() + 10
        while time.time() < deadline and agents.snapshot(0) and \
                any(str(a.get("sessionId")) == session_id for a in agents.snapshot(0)):
            time.sleep(0.5)
        return {"how": k, "ok": ok}
    if k == "interactive":
        r = lifecycle.kill_session(session_id)
        return {"how": k, "ok": bool(r.get("killed")), "detail": r}
    return {"how": "none", "ok": True}


def _strip_marker(path: Path, new_sid: str, backup_to: Path) -> int:
    """원래 파일에서 continuedInSessionId == new_sid 인 continued-in 줄만 지운다. 원본은 backup_to 로 복사."""
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    keep: list[bytes] = []
    dropped = 0
    for ln in lines:
        if _CONT in ln:
            try:
                o = json.loads(ln)
            except Exception:  # noqa: BLE001
                o = {}
            if o.get("type") == "continued-in" and o.get("continuedInSessionId") == new_sid:
                dropped += 1
                continue
        keep.append(ln)
    if not dropped:
        return 0
    backup_to.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_to)
    tmp = path.with_name(path.name + ".tmp-revert")
    tmp.write_bytes(b"".join(keep))
    os.replace(tmp, path)
    scanner.invalidate(path)
    return dropped


def execute(session_id: str) -> dict:
    p = plan(session_id)
    if not p.get("ok"):
        return p
    old, new = p["old"], p["new"]
    from session_manager import hooks, lifecycle, policy
    res: dict = {"ok": False, "old": old, "new": new}
    res["stop"] = _stop(new, p["new_process"])
    d = lifecycle.delete_session(new, dry_run=False, wait_live_s=5.0)
    if d.get("error") or not d.get("trash"):
        res.update(error=d.get("error") or "delete_failed", delete=d,
                   message="사본을 휴지통으로 옮기지 못했습니다(아직 실행 중일 수 있음). 원래 세션은 건드리지 않았습니다.")
        return res
    bucket = Path(d["trash"])
    res["trash"] = str(bucket)
    try:
        from session_manager import team, teamlog
        res["team"] = team.revert_continuation(old, new)
        arc = teamlog.archive_root() / new
        if arc.is_dir():
            shutil.move(str(arc), str(bucket / "team_archive"))
    except Exception as e:  # noqa: BLE001  명부 정리 실패는 파일 되돌리기를 막지 않는다
        res["team_error"] = f"{type(e).__name__}: {e}"
    mo = scanner.scan_one(old)
    res["marker_removed"] = _strip_marker(Path(mo.jsonl_path), new, bucket / f"backup_{old}.jsonl") if mo else 0
    try:   # 휴지통 목록에서 무엇이었는지 알 수 있게
        mf = bucket / "manifest.json"
        m = json.loads(mf.read_text(encoding="utf-8"))
        m["revert"] = {"old": old, "kind": p["kind"], "old_backup": f"backup_{old}.jsonl",
                       "new_messages": p["new_messages"]}
        mf.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    hooks.mark_gone(new, "이어하기 되돌리기로 삭제")
    try:
        policy.audit({"event": "revert_continuation", "old": old, "new": new, "kind": p["kind"],
                      "new_messages": p["new_messages"], "marker_removed": res["marker_removed"]})
    except Exception:  # noqa: BLE001
        pass
    res["ok"] = True
    return res
