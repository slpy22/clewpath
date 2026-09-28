"""v0.9.4 화면 소유권 인계 — 밀려나는 화면에 통지 후 종료, screen_info 노출."""
from __future__ import annotations

import asyncio

from session_manager import webterm


class _Ws:
    def __init__(self, fail_send=False):
        self.sent: list[str] = []; self.closed = False; self.fail_send = fail_send
    async def send_text(self, s):
        if self.fail_send:
            raise RuntimeError("gone")
        self.sent.append(s)
    async def close(self):
        self.closed = True


class _Proc:
    pid = 1
    def isalive(self): return True
    def terminate(self, force=False): pass


def test_push_out_sends_notice_then_closes():
    ws = _Ws()
    asyncio.run(webterm._push_out((ws, None)))
    assert ws.closed and len(ws.sent) == 1
    assert webterm.TAKEOVER_NOTE in ws.sent[0] and "입력할 수 없습니다" in ws.sent[0]


def test_push_out_closes_even_if_notice_fails():
    ws = _Ws(fail_send=True)
    asyncio.run(webterm._push_out((ws, None)))
    assert ws.closed and ws.sent == []
    asyncio.run(webterm._push_out(None))                    # 이전 화면 없음 → 무동작


def test_screen_info_reflects_attached_client():
    webterm._ACTIVE.clear()
    sid = "cccccccc-1111-2222-3333-444444444444"
    assert webterm.screen_info(sid) is None                 # PTY 없음
    sess = webterm._TermSession(sid, _Proc(), persist=True)
    webterm._ACTIVE[sid] = sess
    assert webterm.screen_info(sid) == {"attached": False, "screen_id": None}
    sess.client = (_Ws(), None); sess.client_screen = "scr-1"
    assert webterm.screen_info(sid) == {"attached": True, "screen_id": "scr-1"}
    webterm._ACTIVE.clear()
