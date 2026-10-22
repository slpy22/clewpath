"""받는 쪽(B) 커넥터 진입점 — 문서 4·5·7절.

커넥터는 `pclink`·`pclink.pair` 요청을 기기 인증 관문보다 먼저 여기로 넘긴다(PC 짝은 접속 토큰이 없고, 글마다 짝 키·초대
비밀로 스스로 인증한다). handle 은 곧바로 돌아와야 한다 — 프레임 루프를 막지 않게 긴 일은 asyncio 작업으로 돌린다.

reply(ok, data=None, error=None) 는 그 요청을 보낸 기기(cid)에게만 res 프레임을 보낸다. 한 요청에 여러 번 부를 수 있다
(접수, 그다음 답). 그 기기가 떠났으면 커넥터가 버린다(방 전체에 뿌리지 않는다).

묻기 하나의 길:
    개봉·만료·짝 확인 → 중복 id 장부(처음이 아니면 보관된 상태를 돌려줌) → 정책(끄기·허용·첫 연락 승인)
    → 한도 예약(분·일) → Host 동시 2·대기 4 → 접수(accepted) → 작업: 도구·MCP·훅·기록 없는 복제본(180초·0.5달러)
    → 답을 짝 키로 봉인해 그 cid 로 + 결과 10분 보관(poll)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import Awaitable, Callable

from session_manager.pclink import policy as POL
from session_manager.pclink import protocol as P

METHODS = ("pclink", "pclink.pair")
FEATURES = ["ask", "poll", "list"]

Reply = Callable[..., Awaitable[None]]

_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=POL.HOST_CONCURRENCY + 1, thread_name_prefix="pclink")
_running = 0
_waiting = 0
_TASKS: set = set()


def _log(msg: str) -> None:
    print(f"[pclink] {msg}", flush=True)


def _host_ver() -> str:
    try:
        from session_manager import updater
        return str(updater.current_version())
    except Exception:  # noqa: BLE001
        return ""


async def _off(fn, *a, **kw):
    """블로킹 일(CP 호출·claude 실행·파일)은 이벤트 루프 밖에서."""
    return await asyncio.get_running_loop().run_in_executor(_EXEC, lambda: fn(*a, **kw))


async def handle(method: str, params: dict, cid, reply: Reply, pol: POL.Policy | None = None) -> None:
    pol = pol or POL.get()
    if not isinstance(params, dict):
        await reply(False, error="pclink:bad_body")
        return
    try:
        if method == "pclink.pair":
            await _on_pair_init(pol, params, reply)
            return
        await _on_envelope(pol, (params or {}).get("env"), reply)
    except POL.PolicyCorrupt:
        await reply(False, error="pclink:not_paired")
    except P.ProtocolError as e:
        await reply(False, error=f"pclink:{e.code}")


async def _on_pair_init(pol: POL.Policy, params: dict, reply: Reply) -> None:
    try:
        resp = await _off(pol.pair_init, params or {})
    except P.ProtocolError as e:
        _log(f"짝짓기 거부: {e.code}")
        await reply(False, error=f"pclink:{e.code}")
        return
    await reply(True, data=resp)


async def _on_envelope(pol: POL.Policy, env, reply: Reply) -> None:
    peer = P.peer_of(env)
    kind = (env.get("h") or {}).get("kind") if isinstance(env, dict) and isinstance(env.get("h"), dict) else None
    key = pol.key_for(peer, kind) if peer else None
    if key is None:
        await reply(False, error="pclink:not_paired")
        return
    me = pol.host_id()
    h, body = P.open_env(key, env, my_host=me, peer_host=peer)
    try:
        first = await _off(pol.ledger.seen_or_record, peer, h["id"], h["kind"], h["expires_at"])
    except P.LedgerError as e:
        _log(f"[!] 중복 장부를 쓰지 못해 거부: {e}")
        await reply(False, error="pclink:ledger_unavailable")
        return

    def seal(kind: str, b: dict, *, session: str | None = None, to_session: str | None = None) -> dict:
        hh = P.header(kind, P.addr(me, session), P.addr(peer, to_session), reply_to=h["id"])
        return {"env": P.seal(key, hh, b)}

    k = h["kind"]
    if not first:
        if k == "ask":                       # 같은 묻기의 재전송 — 다시 실행하지 않고 지금 상태를 돌려준다
            await reply(True, data=_state_reply(pol, peer, h["id"], h, seal))
        else:
            await reply(False, error="pclink:duplicate")
        return

    if k == "hello":
        pol.mark_ok(peer, body.get("host_ver"))
        await reply(True, data=seal("hello", {"pv": P.PV, "min_pv": P.PV, "features": FEATURES,
                                              "host_ver": _host_ver()}))
    elif k == "list":
        if pol.paused():
            await reply(True, data=seal("refused", {"code": "paused"}))
            return
        await reply(True, data=seal("list_result", {"sessions": await _off(_allowed_sessions, pol)}))
    elif k == "ask":
        await _on_ask(pol, peer, h, body, reply, seal)
    elif k == "poll":
        ask_id = body.get("ask_id")
        if not isinstance(ask_id, str):
            raise P.ProtocolError("bad_body")
        await reply(True, data=_state_reply(pol, peer, ask_id, h, seal))
    elif k == "pair_cred":
        done = await _off(pol.pair_complete, peer, body)
        _notify(f"🖥 PC '{pol.pair_name(peer) or peer[:8]}' 와 짝을 맺었습니다",
                "이 PC 의 세션에 원격으로 물을 수 있게 됐습니다. 모르는 PC 면 📱 기기 목록에서 짝을 해제하세요.")
        await reply(True, data={"env": done})
    elif k == "unpair":
        name = pol.pair_name(peer)
        await _off(pol.unpair, peer, notified=True)
        _notify(f"🖥 PC '{name or peer[:8]}' 가 짝을 해제했습니다", "이 PC 쪽 짝 정보도 지웠습니다.")
        await reply(True, data={})
    else:
        raise P.ProtocolError("bad_kind", k)


def _state_reply(pol: POL.Policy, peer: str, ask_id: str, h: dict, seal) -> dict:
    st = pol.results.get(ask_id, peer)
    to_s = h["from"]["session"]
    if st is None:
        return seal("refused", {"ask_id": ask_id, "code": "unknown_ask"}, to_session=to_s)
    if st["state"] != "done":
        return seal("pending", {"ask_id": ask_id, "state": st["state"]}, to_session=to_s)
    return seal(st["kind"], st["body"], session=st["body"].get("session"), to_session=to_s)


def _allowed_sessions(pol: POL.Policy) -> list[dict]:
    from session_manager.scanner import scan_one
    out = []
    for sid in pol.allowed_sessions():
        try:
            m = scan_one(sid)
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            continue
        out.append({"id": sid, "title": getattr(m, "title", None) or (m.get("title") if isinstance(m, dict) else None),
                    "updated_at": getattr(m, "mtime", None) or (m.get("mtime") if isinstance(m, dict) else None)})
    return out


async def _on_ask(pol: POL.Policy, peer: str, h: dict, body: dict, reply: Reply, seal) -> None:
    global _running, _waiting
    sid, frm_s, ask_id = h["to"]["session"], h["from"]["session"], h["id"]
    prompt = body.get("prompt")

    def refuse(code: str, **extra):
        pol.audit("ask", peer=peer, session=sid, from_session=frm_s, ask_id=ask_id, code=code)
        return reply(True, data=seal("refused", {"ask_id": ask_id, "code": code, **extra}, to_session=frm_s))

    if not isinstance(prompt, str) or not prompt.strip():
        raise P.ProtocolError("bad_body")
    if len(prompt) > POL.PROMPT_MAX:
        await refuse("too_long")
        return
    code = pol.check_ask(peer, sid)
    if code == "approval_needed":
        try:
            from session_manager.pclink import service
            await _off(service.request_contact, pol, peer, sid)
            code = "pending_approval"
        except Exception as e:  # noqa: BLE001
            _log(f"첫 연락 승인 요청 실패: {type(e).__name__}")
            code = "not_allowed"
    if code:
        await refuse(code)
        return
    retry = pol.reserve(peer)
    if retry is not None:
        await refuse("limit", retry_after=retry)
        return
    if _running + _waiting >= POL.HOST_CONCURRENCY + POL.HOST_QUEUE:
        await refuse("busy")
        return
    if not pol.results.start(ask_id, peer):
        await reply(True, data=_state_reply(pol, peer, ask_id, h, seal))
        return
    _waiting += 1
    await reply(True, data=seal("accepted", {"ask_id": ask_id}, session=sid, to_session=frm_s))
    t = asyncio.create_task(_run_ask(pol, peer, h, prompt, reply, seal))
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)


_SEM: dict = {}


def _sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    s = _SEM.get(id(loop))
    if s is None:
        s = _SEM[id(loop)] = asyncio.Semaphore(POL.HOST_CONCURRENCY)
    return s


async def _run_ask(pol: POL.Policy, peer: str, h: dict, prompt: str, reply: Reply, seal) -> None:
    global _running, _waiting
    sid, frm_s, ask_id = h["to"]["session"], h["from"]["session"], h["id"]
    t0 = time.monotonic()
    kind, out = "error", {"ask_id": ask_id, "code": "ask_failed", "reason": "internal"}
    try:
        async with _sem():
            _waiting -= 1
            _running += 1
            try:
                pol.results.running(ask_id)
                kind, out = await _off(_ask_clone, sid, prompt, ask_id)
            finally:
                _running -= 1
    except Exception as e:  # noqa: BLE001
        _log(f"원격 묻기 실행 실패: {type(e).__name__}")
    out["session"] = sid
    pol.results.finish(ask_id, kind, out)
    pol.audit("ask", peer=peer, session=sid, from_session=frm_s, ask_id=ask_id,
              code=("answered" if kind == "answer" else out.get("code")), cost=out.get("cost_usd"),
              dur=round(time.monotonic() - t0, 2))
    try:
        await reply(True, data=seal(kind, out, session=sid, to_session=frm_s))
    except Exception:  # noqa: BLE001  묻는 쪽이 떠났으면 결과는 보관만(poll)
        pass


def _ask_clone(sid: str, prompt: str, ask_id: str) -> tuple[str, dict]:
    from session_manager import webapi
    r = webapi.ephemeral_ask(sid, prompt, timeout=POL.ASK_TIMEOUT_S, isolated=True,
                             guardrails={"max_budget_usd": POL.ASK_BUDGET_USD})
    if r.get("error"):
        return "error", {"ask_id": ask_id, "code": "ask_failed", "reason": str(r["error"])[:200]}
    if r.get("isolation_breach"):
        _log("[!] 복제본이 도구를 썼다고 보고했습니다 — 답을 버립니다(실행 안전성 계약 위반)")
        return "error", {"ask_id": ask_id, "code": "ask_failed", "reason": "isolation_breach"}
    if r.get("is_error"):
        return "error", {"ask_id": ask_id, "code": "ask_failed", "reason": str(r.get("result") or "")[:200]}
    ans = str(r.get("answer") or "")
    return "answer", {"ask_id": ask_id, "answer": ans[:POL.ANSWER_MAX], "truncated": len(ans) > POL.ANSWER_MAX,
                      "cost_usd": r.get("cost_usd"), "duration_s": r.get("duration_s")}


def _notify(title: str, body: str) -> None:
    try:
        from session_manager import push
        push.send("pclink", "", title, body)
    except Exception:  # noqa: BLE001
        pass
