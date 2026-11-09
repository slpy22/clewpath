"""에이전트 명부 라우트(사장님 화면·폰). 판단은 team 모듈, 여기는 요청 모양만(docs/designs/agent-unit.md 6-2)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from session_manager import team


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except team.TeamError as e:
        return JSONResponse({"error": e.code}, status_code=e.status)


async def _body(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001 — 빈 본문·깨진 JSON 은 빈 값으로(필수 값 검사는 team 이 한다)
        return {}
    return d if isinstance(d, dict) else {}


def register(app) -> None:
    @app.get("/api/owner/agents")
    def agents_list():
        return _call(team.agents_overview)

    @app.get("/api/owner/agents/{agent_id}")
    def agent_get(agent_id: str):
        return _call(team.agent_detail, agent_id)

    @app.post("/api/owner/agents")
    async def agent_create(request: Request):
        b = await _body(request)
        return _call(team.create_agent, str(b.get("session_id") or ""), str(b.get("name") or ""), str(b.get("role") or ""), b.get("tags"))

    @app.post("/api/owner/agents/{agent_id}/rename")
    async def agent_rename(agent_id: str, request: Request):
        b = await _body(request)
        return _call(team.rename_agent, agent_id, str(b.get("name") or ""), b.get("agent_rev"))

    @app.post("/api/owner/agents/{agent_id}/attach")
    async def agent_attach(agent_id: str, request: Request):
        b = await _body(request)
        return _call(team.attach_session, agent_id, str(b.get("session_id") or ""), bool(b.get("replace")))

    @app.post("/api/owner/agents/{agent_id}/detach")
    async def agent_detach(agent_id: str, request: Request):
        b = await _body(request)
        return _call(team.detach_session, agent_id, b.get("session_id"))

    @app.post("/api/owner/agents/{agent_id}/retire")
    async def agent_retire(agent_id: str, request: Request):
        b = await _body(request)
        return _call(team.retire_agent, agent_id, bool(b.get("on", True)))
