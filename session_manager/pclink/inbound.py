"""받는 쪽(B) 커넥터 진입점 — 문서 7절.

커넥터는 `pclink`·`pclink.pair` 요청을 기기 인증 관문보다 먼저 여기로 넘긴다(PC 짝은 접속 토큰이 없고, 글마다 짝 키·초대
비밀로 스스로 인증한다). handle 은 곧바로 돌아와야 한다 — 프레임 루프를 막지 않게 긴 일은 asyncio 작업으로 돌린다.

reply(ok, data=None, error=None) 는 그 요청을 보낸 기기(cid)에게만 res 프레임을 보낸다. 한 요청에 여러 번 부를 수 있다
(접수, 그다음 답). 그 기기가 떠났으면 커넥터가 버린다(방 전체에 뿌리지 않는다).
"""
from __future__ import annotations

from typing import Awaitable, Callable

METHODS = ("pclink", "pclink.pair")

Reply = Callable[..., Awaitable[None]]


async def handle(method: str, params: dict, cid, reply: Reply) -> None:
    await reply(False, error="pclink:not_ready")
