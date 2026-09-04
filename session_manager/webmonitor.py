"""오케스트레이션 관제 WS 러너 — monitor.MonitorGroup 을 주기 폴링해 push.

브라우저(타임라인) ↔ WebSocket ↔ 이 모듈 ↔ monitor(그룹 jsonl tail).

읽기전용·단방향: 클라이언트는 입력을 보내지 않는다(관전). jsonl 무수정(불가침
원칙). 클라이언트가 끊기면 폴링을 멈춘다.

프로토콜(서버 → 브라우저):
    {"type":"snapshot", "events":[...]}   최초 진입 리플레이(링버퍼 스냅샷)
    {"type":"events",   "events":[...]}   증분 이벤트(폴링마다, 있을 때만)
    {"type":"heartbeat"}                  유휴 시 생존 신호(끊김 조기 감지)
    {"type":"error", "error":"..."}       그룹 비정상 등
각 event 는 monitor.MonitorGroup 이 만든 dict(session_id/label/color/role/
text/tool_calls/tool_results/calls_out/seq/ts).
"""
from __future__ import annotations

import asyncio
import json

from session_manager import monitor

# 폴링 주기(초). append-only jsonl 을 오프셋 증분으로 읽는 비용은 작다.
_POLL_INTERVAL = 0.4
# 유휴 하트비트 주기(초) — 이벤트가 없어도 이만큼마다 생존 신호를 보내 끊김 감지.
_HEARTBEAT_EVERY = 5.0
# 그룹 최대 세션 수(자원 상한).
_MAX_SESSIONS = 8


async def run_monitor(ws, specs: list[dict]) -> None:
    """WS 하나를 그룹 관제에 붙인다. 호출 측에서 ws.accept()는 끝난 상태로 가정.

    specs: [{"session_id", "role"?}, ...]
    """
    specs = specs[:_MAX_SESSIONS]
    if not specs:
        try:
            await ws.send_json({"type": "error", "error": "empty_group"})
        except Exception:  # noqa: BLE001
            pass
        return

    group = monitor.build_group(specs)

    # 최초 스냅샷(최근 tail 리플레이)
    try:
        await ws.send_json({"type": "snapshot", "events": group.prime()})
    except Exception:  # noqa: BLE001  클라이언트가 이미 끊김
        return

    closed = asyncio.Event()
    cmds: asyncio.Queue = asyncio.Queue()   # 클라이언트 제어(add/remove) → 폴 루프에서 처리

    async def _watch() -> None:
        # 클라이언트 제어 메시지 수신 + 끊김 감지. 모든 ws 전송은 폴 루프에만 두어
        # 동시 전송으로 프레임이 깨지는 것을 막는다(여기선 큐에 넣기만).
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(msg, dict) and msg.get("type") in ("add", "remove"):
                    await cmds.put(msg)
        except Exception:  # noqa: BLE001 (WebSocketDisconnect 포함)
            closed.set()

    watcher = asyncio.create_task(_watch())
    try:
        idle = 0.0
        while not closed.is_set():
            await asyncio.sleep(_POLL_INTERVAL)
            # 1) 클라이언트 제어(그룹 동적 변경) 먼저 반영
            while not cmds.empty():
                c = cmds.get_nowait()
                if c["type"] == "add" and c.get("session_id") \
                        and len(group.sessions) < _MAX_SESSIONS:
                    evs = group.add_session({"session_id": c["session_id"],
                                             "role": c.get("role", "sub")})
                    await ws.send_json({"type": "group", "action": "add",
                                        "session_id": c["session_id"]})
                    if evs:
                        await ws.send_json({"type": "events", "events": evs})
                elif c["type"] == "remove" and c.get("session_id"):
                    group.remove_session(c["session_id"])
                    await ws.send_json({"type": "group", "action": "remove",
                                        "session_id": c["session_id"]})
                idle = 0.0
            # 2) 증분 이벤트
            evs = group.poll()
            if evs:
                await ws.send_json({"type": "events", "events": evs})
                idle = 0.0
            else:
                idle += _POLL_INTERVAL
                if idle >= _HEARTBEAT_EVERY:
                    await ws.send_json({"type": "heartbeat"})
                    idle = 0.0
    except Exception:  # noqa: BLE001  전송 실패 = 끊김
        pass
    finally:
        watcher.cancel()


def specs_from_query(ids: str | None, manager: str | None) -> list[dict]:
    """쿼리스트링(ids=콤마목록, manager=UUID) → build_group specs.

    manager 로 지정된 세션은 role=manager, 나머지는 sub. manager 미지정 시 첫 세션.
    """
    id_list = [x.strip() for x in (ids or "").split(",") if x.strip()]
    mgr = manager or (id_list[0] if id_list else None)
    return [{"session_id": sid, "role": "manager" if sid == mgr else "sub"}
            for sid in id_list]
