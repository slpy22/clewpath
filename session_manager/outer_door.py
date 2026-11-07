"""바깥문 B — Claude 세션이 claude 파일을 쓰는 원시 API 를 직접 부르지 못하게 막는다(사장님 결정 2026-11-07).

docs/designs/session-control.md 6·11 절. 승인(폰/2FA)을 만들어도 원시 삭제·이동·이름 API 가 열려 있으면 `curl` 로 건너뛸 수 있다.
그래서 루프백 요청의 소켓 소유 프로세스 → 조상 → `~/.claude/sessions` 레지스트리로 부른 쪽을 추적해(pclink/sender.py):

- Claude 세션이 불렀다  → 403
- 판별 실패            → 403(다시 시도 안내). 넓히지 않는 쪽
- 확인된 비-Claude(no_claude_ancestor) · 이 PC 밖(not_local, 인증 미들웨어가 이미 본 요청) → 통과

보호 범위: 실수와 일반적인 프롬프트 주입. 같은 사용자 권한의 프로그램을 막는 격리가 아니고, 통과가 사람 증명도 아니다.
`X-ClewPath-Via` 헤더는 믿지 않는다(curl 로 붙일 수 있다) — 릴레이 요청도 소유자 추적으로 판정한다(커넥터는 Claude 자손이 아니다).

새 변경 라우트는 GUARDED 또는 OPEN 에 반드시 올린다. 표에 없으면 시험이 실패한다(tests/test_outer_door.py, 손으로 맞추는 목록 방지).
비상 스위치: 환경변수 SM_OUTER_DOOR(on|off) > clewpath.toml [security] outer_door > 기본 on. 켜고 끄는 건 사람 몫.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# claude 파일을 쓰거나(세션 기록·스킬) 쓰기 범위의 기준을 바꾸는 라우트. Claude 세션은 승인 경유 도구(session_ops)로만.
GUARDED = frozenset({
    "/api/sessions/{session_id}/title", "/api/v1/sessions/{session_id}/title", "/api/owner/names/agent-sync",
    "/api/sessions/{session_id}/delete", "/api/owner/trash/{bucket}/restore",
    "/api/sessions/{session_id}/change-cwd", "/api/sessions/{session_id}/revert-continuation",
    "/api/import", "/api/owner/skills/{key}/install", "/api/owner/teams/{team_id}/root",
})

# 막지 않는 변경 라우트. ClewPath 자체 데이터, 자체 인증·승인이 있는 것, 스킬·훅이 쓰는 것.
OPEN = frozenset({
    "/login", "/api/hooks/event",
    # 승인: decide 는 사람 증명이 필요하고, execute 는 승인된 것만 돈다
    "/api/owner/approvals", "/api/owner/approvals/{aid}/decide", "/api/owner/approvals/{aid}/execute",
    "/api/v1/team/approvals", "/api/v1/team/approvals/{aid}/execute",
    # 터미널: 워커·서비스 모니터 스킬이 쓴다. 권한은 생성 설정이 정하고 새 자동 허용 세션은 감사 알림(D1 C)
    "/api/terminal/new", "/api/sessions/{session_id}/terminal/start", "/api/sessions/{session_id}/terminal/stop",
    "/api/sessions/{session_id}/resume-launch", "/api/sessions/{session_id}/kill", "/api/owner/sessions/{session_id}/respond",
    # 세션 덧붙임 정보(ClewPath 저장소)
    "/api/sessions/{session_id}/labels", "/api/v1/sessions/{session_id}/labels", "/api/sessions/{session_id}/model",
    "/api/sessions/{session_id}/keep", "/api/sessions/{session_id}/standalone", "/api/sessions/{session_id}/picker-expose",
    "/api/sessions/{session_id}/profile", "/api/v1/sessions/{session_id}/profile", "/api/sessions/{session_id}/file/download",
    # 프롬프트 세션 제어: 판단·승인은 session_ops 가 한다(이 문을 막으면 도구가 못 돈다)
    "/api/v1/session-ops/rename", "/api/v1/session-ops/create", "/api/v1/session-ops/restore",
    "/api/v1/session-ops/move/preview", "/api/v1/session-ops/move/request",
    "/api/v1/session-ops/delete/preview", "/api/v1/session-ops/delete/request", "/api/v1/session-ops/finish/{aid}",
    # 묻기·단발 실행(토큰·한도가 따로 있음)
    "/api/v1/sessions/{session_id}/ask", "/api/v1/oneshot", "/api/v1/pclink/ask", "/api/v1/pclink/poll",
    # 팀 명부(에이전트 경로·사장님 화면)
    "/api/v1/team", "/api/v1/team/import", "/api/v1/team/profile", "/api/v1/team/{team_id}/archive",
    "/api/v1/team/{team_id}/members", "/api/v1/team/{team_id}/members/{agent}/leave",
    "/api/v1/team/{team_id}/members/{agent}/move", "/api/v1/team/{team_id}/members/{agent}/session",
    "/api/v1/team/{team_id}/purge", "/api/v1/team/{team_id}/sync", "/api/v1/team/{team_id}/tasks",
    "/api/v1/team/{team_id}/tasks/{task_id}/{action}",
    "/api/owner/teams/{team_id}/manager", "/api/owner/teams/{team_id}/members",
    "/api/owner/teams/{team_id}/members/{agent}/leave", "/api/owner/teams/{team_id}/notify", "/api/owner/teams/{team_id}/rename",
    # 관제·정기 작업·저장소
    "/api/owner/monitor/groups", "/api/owner/monitor/groups/{gid}/delete", "/api/owner/monitor/groups/{gid}/notify",
    "/api/owner/housekeeping", "/api/owner/housekeeping/{job}/run", "/api/owner/store/notices/clear",
    "/api/owner/store/restore-kept",
    # 기기·2FA·알림·업데이트·서비스·PC 짝(각자 인증·2FA)
    "/api/owner/2fa/provision", "/api/owner/2fa/toggle", "/api/owner/devices", "/api/owner/devices/{device_id}/delete",
    "/api/owner/devices/{device_id}/reissue", "/api/owner/devices/{device_id}/rename", "/api/owner/devices/{device_id}/revoke",
    "/api/owner/push/subscribe", "/api/owner/push/test", "/api/owner/push/unsubscribe",
    "/api/owner/ptyd/restart", "/api/owner/update/apply", "/api/owner/update/check",
    "/api/owner/services", "/api/owner/services/{sid}", "/api/owner/services/{sid}/rotate",
    "/api/services/requests", "/api/services/requests/{rid}/claim",
    "/api/owner/pclink/accept", "/api/owner/pclink/invite", "/api/owner/pclink/invites/{inv}/cancel",
    "/api/owner/pclink/pause", "/api/owner/pclink/peers/{peer}/unpair", "/api/owner/pclink/sessions/{sid}/allow",
})


def _regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "/?$")


_GUARD_RX = [_regex(t) for t in sorted(GUARDED)]
_PASS = ("no_claude_ancestor", "not_local")


def enabled() -> bool:
    v = os.environ.get("SM_OUTER_DOOR")
    if v is None:
        try:
            from session_manager import appconfig
            v = appconfig.get("security", "outer_door", "on")
        except Exception:  # noqa: BLE001
            v = "on"
    return str(v).strip().lower() not in ("off", "0", "false", "no")


def guarded(path: str) -> bool:
    return any(r.match(path) for r in _GUARD_RX)


def identify(client_host: str | None, client_port: int | None, server_port: int | None) -> tuple[str, str | None]:
    """부른 쪽 판정 → ('claude', 세션 id) | ('other', 이유) | ('unknown', 이유)."""
    from session_manager.pclink import sender
    try:
        return "claude", sender.caller_session(client_host, client_port, server_port)
    except sender.BadSender as e:
        reason = str(e)
        return ("other" if reason in _PASS else "unknown"), reason
    except Exception as e:  # noqa: BLE001 — 판별 도구 오류도 판별 실패
        return "unknown", type(e).__name__


def _refusal(kind: str, detail: str | None) -> dict | None:
    if kind == "other":
        return None
    if kind == "claude":
        return {"error": "claude_session_blocked", "caller": detail,
                "message": "Claude 세션에서는 이 동작을 직접 부를 수 없습니다. 승인을 거치는 세션 제어 도구를 쓰거나 화면에서 하세요."}
    return {"error": "caller_unknown", "reason": detail,
            "message": "요청한 프로그램을 확인하지 못했습니다. 잠시 뒤 다시 시도하세요."}


class OuterDoorMiddleware:
    """순수 ASGI(바디를 읽지 않는다). 보호 라우트의 변경 요청에서만 소유자를 추적한다."""

    def __init__(self, app, base_path: str = ""):
        self.app = app
        self.base_path = base_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") in MUTATING:
            path = scope.get("path", "")
            if self.base_path and path.startswith(self.base_path):
                path = path[len(self.base_path):]
            if guarded(path) and enabled():
                client = scope.get("client") or (None, None)
                server = scope.get("server") or (None, None)
                kind, detail = await asyncio.to_thread(identify, client[0], client[1], server[1])
                body = _refusal(kind, detail)
                if body:
                    from session_manager import store
                    store.log(f"[outer_door] 거절 {path} {kind} {detail}")
                    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    await send({"type": "http.response.start", "status": 403,
                                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                            (b"content-length", str(len(raw)).encode())]})
                    await send({"type": "http.response.body", "body": raw})
                    return
        await self.app(scope, receive, send)


def audit_auto_session(request, session_id: str, cwd: str) -> None:
    """Claude 세션이 새 자동 허용 세션을 만들었다 — 통과시키되 기록하고 폰으로 알린다(D1 C). 실패해도 생성은 막지 않는다."""
    try:
        c = request.client
        port = request.scope.get("server", (None, None))[1]
        kind, detail = identify(c.host if c else None, c.port if c else None, port)
        if kind == "other":
            return
        from session_manager import policy, push
        policy.audit({"event": "auto_session_created", "session_id": session_id, "cwd": cwd, "caller_kind": kind, "caller": detail})
        who = detail if kind == "claude" else "판별 실패"
        push.send(f"auto-session:{session_id}", session_id, "자동 허용 세션 생성",
                  f"Claude 세션({str(who)[:8]})이 승인 없이 자동 허용 세션을 만들었습니다: {cwd}")
    except Exception as e:  # noqa: BLE001
        from session_manager import store
        store.log(f"[outer_door] 감사 기록 실패: {e}")
