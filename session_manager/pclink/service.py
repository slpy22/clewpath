"""pclink 이음새 — 승인 대기열 실행기(짝짓기·첫 연락), 내 방 정보, 첫 연락 승인 요청, 세션 표시 이름.

승인 대기열(approvals.py)은 인자·결과를 평문으로 저장하므로 초대 문자열·비밀은 싣지 않는다 — 정책 파일에 감싸 둔 것을 id 로
가리킨다(초대 = inv, 붙여 넣은 초대 = accept_id).
"""
from __future__ import annotations

import asyncio

from session_manager.pclink import policy as POL


def session_title(sid: str) -> str:
    try:
        from session_manager import labels
        from session_manager.scanner import scan_one
        m = scan_one(sid)
        name = (labels.get(sid) or {}).get("name")
        if m:
            return str(m.custom_title or name or m.ai_title or sid[:8])
        return str(name or sid[:8])
    except Exception:  # noqa: BLE001
        return sid[:8]


def my_room() -> dict:
    """상대 Host 가 이 Host 방에 붙을 길 — 지금 커넥터의 릴레이 주소·방과 CP 주소. 릴레이가 꺼져 있으면 빈 값."""
    from session_manager import connector, cp_client
    c = connector.CURRENT
    relay = getattr(c, "relay_url", None) or ""
    room = getattr(c, "room", None) or ""
    cp = cp_client.cp_url() or ""
    return {"relay": relay, "cp": cp.rstrip("/"), "room": room}


def summary(kind: str, a: dict) -> str:
    if kind == "pclink_pair":
        if a.get("action") == "invite":
            return "PC 짝짓기 초대 만들기 — 1회용·24시간. 초대 문자열을 받은 PC 가 이 PC 의 「원격 묻기 받기」 세션에 물을 수 있게 됩니다"
        return (f"PC 짝 맺기: '{a.get('peer_name') or '?'}' ({str(a.get('peer_host') or '')[:8]}) — 서로의 「원격 묻기 받기」 세션에 "
                f"물을 수 있게 됩니다")
    if kind == "pclink_contact":
        return (f"원격 묻기 허용: PC '{a.get('peer_name') or '?'}' 가 세션 [{a.get('title') or str(a.get('session_id'))[:8]}] 의 "
                f"지금과 앞으로의 대화 전체를 30일 동안 물어볼 수 있게 합니다(답만 돌려주고 이 PC 에서는 아무것도 실행하지 않음)")
    return kind


def request_contact(pol: POL.Policy, peer: str, sid: str) -> dict:
    """첫 연락 승인 요청(폰 알림). 이미 살아 있는 요청이 있으면 다시 만들지 않는다(정책이 pending 으로 본다)."""
    from session_manager import approvals
    ap = approvals.create("pclink_contact", {"peer": peer, "peer_name": pol.pair_name(peer) or peer[:8],
                                             "session_id": sid, "title": session_title(sid)},
                          requested_by=f"pclink:{peer}")
    pol.mark_contact_pending(peer, sid, ap["id"], ap["expires"])
    pol.audit("contact_requested", peer=peer, session=sid, approval=ap["id"])
    return ap


def execute_pair(a: dict) -> dict:
    pol = POL.get()
    if a.get("action") == "invite":
        from session_manager import appconfig, devices
        r = my_room()
        return pol.create_invite(name=devices.clean_name(appconfig.machine_name(), "PC"), **r)
    from session_manager.pclink import routes
    return asyncio.run(routes.accept_invite(pol, a["accept_id"]))


def execute_contact(a: dict) -> dict:
    return POL.get().grant_contact(a["peer"], a["session_id"])
