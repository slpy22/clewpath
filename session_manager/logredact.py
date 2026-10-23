"""로그에 실리는 주소의 비밀 쿼리값(jwt·token)을 가린다 — 로깅 필터 한 곳.

커넥터(릴레이 agent)·pclink(상대 방 client)는 `?…&jwt=<토큰>`(공유 토큰 배포면 `token=`), mcp_server 는 로컬 Host 에
`?token=<API 토큰>` 을 실은 주소로 웹소켓을 연다. websockets 는 DEBUG 수준에서 요청 줄(`> GET /ws?…&jwt=… HTTP/1.1`)을
남기고, uvicorn 은 기본(INFO) 수준에서도 받은 웹소켓 주소(`"WebSocket /…?token=…" [accepted]`)와 접근 로그를 남긴다(host.log).
누가 로그 수준을 DEBUG 로 올리거나 host.log 를 남에게 보여 줘도 토큰이 같이 나가지 않게, 기록이 처리기에 닿기 전에 값을 가린다.

logging 규칙상 로거 필터는 그 로거에서 직접 만든 기록에만 걸리고 자식 로거에서 올라온 기록에는 안 걸린다 — 그래서
'websockets' 하나가 아니라 기록을 만드는 로거마다 건다(_LOGGERS). 인자 모양(튜플·사전)은 그대로 두고 그 안의 글자만 바꾼다 —
uvicorn 접근 로그 포매터는 args 를 자리별로 풀어 쓴다. 로그 수준이 꺼져 있으면 필터는 불리지도 않는다(비용 0).
"""
from __future__ import annotations

import logging
import re

# 기록을 만드는 로거들. websockets: client(asyncio·sync·legacy)·server·protocol(legacy). uvicorn: error(웹소켓 수락 줄)·access.
_LOGGERS = ("websockets", "websockets.client", "websockets.server", "websockets.protocol",
            "uvicorn", "uvicorn.error", "uvicorn.access")

# 쿼리스트링의 비밀값 — `?`·`&` 뒤의 jwt=·token=·access_token= 값. 이름은 남겨 무엇이 실렸는지는 보이게 한다.
_SECRET_QS = re.compile(r"(?i)([?&](?:jwt|token|access_token)=)[^&#\s\"'<>]+")
MASK = "***"

_installed = False


def redact(text: str) -> str:
    """글 안의 주소 쿼리 비밀값을 `***` 로. 비밀값이 없으면 같은 글을 돌려준다."""
    return _SECRET_QS.sub(r"\g<1>" + MASK, text)


def _clean(v):
    """인자 하나 — 글자면 가리고, 다른 값은 글자로 바꿨을 때 비밀이 보일 때만 가린 글자로 바꾼다(숫자 인자는 그대로)."""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    try:
        s = str(v)
    except Exception:  # noqa: BLE001 — 로그는 기능을 깨뜨리지 않는다
        return v
    r = redact(s)
    return v if r == s else r


class SecretQueryFilter(logging.Filter):
    """기록의 msg·args 에서 쿼리 비밀값을 가린다. 기록은 늘 통과시킨다(True)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            a = record.args
            if isinstance(a, tuple):
                record.args = tuple(_clean(x) for x in a)
            elif isinstance(a, dict):
                record.args = {k: _clean(x) for k, x in a.items()}
        except Exception:  # noqa: BLE001
            pass
        return True


_FILTER = SecretQueryFilter()


def install() -> None:
    """_LOGGERS 에 필터를 건다. 멱등 — 웹소켓을 여는 모듈(connector·pclink.link·mcp_server)과 Host(server)가 import 때 부른다.
    uvicorn 이 기동하며 dictConfig 로 로거를 다시 꾸며도 필터는 지우지 않는다(처리기·수준만 바꾼다)."""
    global _installed
    if _installed:
        return
    _installed = True
    for name in _LOGGERS:
        lg = logging.getLogger(name)
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
