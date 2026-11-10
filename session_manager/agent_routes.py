"""에이전트 명부 라우트(사장님 화면·폰). 판단은 team 모듈, 여기는 요청 모양만(docs/designs/agent-unit.md 6-2)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from session_manager import agent_archive, agent_history, handoff, team


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except handoff.Changed as e:
        return JSONResponse({"error": e.code, **e.extra}, status_code=e.status)
    except team.TeamError as e:
        return JSONResponse({"error": e.code}, status_code=e.status)


def _origin(request: Request) -> str:
    """비용 기준 판정용 — 폰(릴레이 경유) 여부. 위조해 봐야 '폰'으로 더 엄격해질 뿐이다."""
    from session_manager.connector import VIA_HEADER
    return "phone" if request.headers.get(VIA_HEADER) else "local"


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
        def detail():
            d = team.agent_detail(agent_id)
            return {**d, "archive": agent_archive.info(d["id"])}       # 세션별 보관·원본 여부(H7)
        return _call(detail)

    # ---- 지난 대화 찾기(사장님 화면·폰, H8) — 범위는 그 에이전트. 읽기지만 POST(본문, K16)
    @app.post("/api/owner/agents/{agent_id}/history/search")
    async def agent_hist_search(agent_id: str, request: Request):
        b = await _body(request)
        return _call(agent_history.search, None, str(b.get("q") or ""), str(b.get("session_id") or ""), int(b.get("limit") or 20), agent=agent_id)

    @app.post("/api/owner/agents/{agent_id}/history/read")
    async def agent_hist_read(agent_id: str, request: Request):
        b = await _body(request)
        return _call(agent_history.read, None, str(b.get("ref") or ""), int(b.get("before", 2) or 0), int(b.get("after", 4) or 0), b.get("at"),
                     agent=agent_id)

    @app.post("/api/owner/agents/{agent_id}/archive-now")      # 지우기 전에 보관(K13) — ClewPath 데이터만 쓴다
    async def agent_archive_now(agent_id: str, request: Request):
        b = await _body(request)
        ids = b.get("session_ids") if isinstance(b.get("session_ids"), list) else []
        return _call(agent_archive.archive_now, agent_id, [str(x) for x in ids][:200])

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

    # ---- 갈아타기(handoff.py). 쓰기는 모두 바깥문 GUARDED — Claude 세션이 스스로 부르지 못한다(G2)
    @app.get("/api/owner/agents/{agent_id}/handoff")
    def handoff_precheck(agent_id: str):
        return _call(handoff.precheck, agent_id)

    @app.post("/api/owner/agents/{agent_id}/handoff")
    async def handoff_start(agent_id: str, request: Request):
        b = await _body(request)
        return _call(handoff.start, agent_id, str(b.get("mode") or "clone"), _origin(request), b.get("confirm_usd"))

    @app.post("/api/owner/agents/{agent_id}/handoff-2fa")      # 폰 하루 기준을 넘었을 때(커넥터가 2FA 를 요구하는 경로)
    async def handoff_start_2fa(agent_id: str, request: Request):
        b = await _body(request)
        return _call(handoff.start, agent_id, str(b.get("mode") or "clone"), _origin(request), b.get("confirm_usd"), True)

    @app.get("/api/owner/handoffs/{hid}")
    def handoff_status(hid: str):
        return _call(handoff.status_of, hid)

    @app.post("/api/owner/handoffs/{hid}/draft")
    async def handoff_draft(hid: str, request: Request):
        b = await _body(request)
        return _call(handoff.save_draft, hid, b.get("text"), b.get("draft_rev"))

    @app.post("/api/owner/handoffs/{hid}/pending")
    async def handoff_pending(hid: str, request: Request):
        b = await _body(request)
        return _call(handoff.take_pending, hid, bool(b.get("accept")), b.get("draft_rev"))

    @app.post("/api/owner/handoffs/{hid}/resummary")
    async def handoff_resummary(hid: str, request: Request):
        b = await _body(request)
        return _call(handoff.resummary, hid, _origin(request), b.get("confirm_usd"))

    @app.post("/api/owner/handoffs/{hid}/resummary-2fa")
    async def handoff_resummary_2fa(hid: str, request: Request):
        b = await _body(request)
        return _call(handoff.resummary, hid, _origin(request), b.get("confirm_usd"), True)

    @app.post("/api/owner/handoffs/{hid}/cancel")
    async def handoff_cancel(hid: str):
        return _call(handoff.cancel, hid)

    @app.post("/api/owner/handoffs/{hid}/confirm-handoff")
    async def handoff_confirm(hid: str, request: Request):
        b = await _body(request)
        return _call(handoff.confirm, hid, b.get("draft_rev"), b.get("snapshot_token"), b.get("options"), b.get("cwd"))
