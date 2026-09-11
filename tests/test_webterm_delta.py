"""화면 커서 델타 리플레이 — 탭 즉시 전환(v0.8.0)의 서버측 계약.

탭마다 screen_id 를 두고 서버가 '그 화면에 실제로 보낸 오프셋'을 기억한다. 재접속 때
그 뒤의 출력(델타)만 보내야 탭별로 보유한 xterm 이 이중 렌더·공백 없이 이어진다.
커서가 없으면(첫 접속·구버전 클라) 기존처럼 tail+배너, 버퍼를 넘겼으면 tail+생략 안내.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from session_manager import webterm


class _Proc:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self.pid = 4242

    def isalive(self):
        return True

    def write(self, d):
        pass

    def terminate(self, force=False):
        pass

    def read(self):
        if not self._chunks:
            raise EOFError
        return self._chunks.pop(0)


class _WS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_text(self, s):
        self.sent.append(s)

    async def close(self):
        self.closed = True

    async def receive_text(self):
        raise RuntimeError("disconnect")   # 펌프 루프 즉시 탈출


# ---------------------------------------------------------------- 순수 회계

def test_feed_tracks_total_and_base_offsets(monkeypatch):
    monkeypatch.setattr(webterm, "_BUF_CAP", 10)
    s = webterm._TermSession("k", _Proc(), persist=True)
    s.feed("aaaa"); s.feed("bbbb"); s.feed("cccc")     # 12 > 10 → 'aaaa' 버림
    assert s.total_len == 12
    assert s.base_off == 4
    assert "".join(s.chunks) == "bbbbcccc"


def test_tail_since_delta_end_and_overflow(monkeypatch):
    monkeypatch.setattr(webterm, "_BUF_CAP", 10)
    s = webterm._TermSession("k", _Proc(), persist=True)
    s.feed("aaaa"); s.feed("bbbb"); s.feed("cccc")     # base_off=4, total=12
    assert s.tail_since(6) == ("bbcccc", 12, False)   # 정확한 델타
    assert s.tail_since(12) == ("", 12, False)        # 이미 다 봤다 → 빈 델타
    text, end, fb = s.tail_since(2)                    # 버퍼 시작(4)보다 오래됨 → 폴백
    assert fb is True and end == 12 and text == "bbbbcccc"


def test_mark_sent_monotonic_and_capped(monkeypatch):
    monkeypatch.setattr(webterm, "_MAX_SCREENS", 3)
    s = webterm._TermSession("k", _Proc(), persist=True)
    s.mark_sent("a", 10); s.mark_sent("a", 5)          # 뒤로 안 간다
    assert s.screens["a"] == 10
    s.mark_sent(None, 99)                              # 구버전(화면 id 없음)은 무시
    assert list(s.screens) == ["a"]
    for k in ("b", "c", "d"):
        s.mark_sent(k, 1)
    assert "a" not in s.screens and list(s.screens) == ["b", "c", "d"]   # 오래된 순 제거


# ---------------------------------------------------------------- 재접속 선택

def _live_session(text_chunks):
    s = webterm._TermSession("sid-1", _Proc(), persist=True)
    for c in text_chunks:
        s.feed(c)
    s.client = None
    return s


async def _reattach(monkeypatch, sess, screen_id):
    monkeypatch.setattr(webterm, "_get_live", lambda key: sess)
    ws = _WS()
    try:
        await webterm.run_terminal(ws, "sid-1", screen_id=screen_id)
    except Exception:  # noqa: BLE001  (펌프 루프 탈출용 disconnect)
        pass
    return ws


@pytest.mark.asyncio
async def test_reattach_with_cursor_sends_only_delta_no_banner(monkeypatch):
    sess = _live_session(["hello ", "world ", "again"])
    sess.mark_sent("S1", 6)                            # 이 화면은 'hello ' 까지 봤다
    ws = await _reattach(monkeypatch, sess, "S1")
    assert ws.sent == ["world again"]                   # 델타만, 배너 없음
    assert sess.screens["S1"] == sess.total_len         # 커서가 끝까지 전진
    assert sess.client_screen == "S1"


@pytest.mark.asyncio
async def test_reattach_with_cursor_at_end_sends_nothing(monkeypatch):
    sess = _live_session(["abc"])
    sess.mark_sent("S1", 3)
    ws = await _reattach(monkeypatch, sess, "S1")
    assert ws.sent == []                                # 빈 델타 → 무전송(깜빡임 0)


@pytest.mark.asyncio
async def test_reattach_without_cursor_keeps_legacy_tail_and_banner(monkeypatch):
    sess = _live_session(["hello ", "world"])
    ws = await _reattach(monkeypatch, sess, None)       # 구버전 클라(screen 없음)
    assert ws.sent[0] == "hello world"
    assert "다시 연결했습니다" in ws.sent[1]
    ws2 = await _reattach(monkeypatch, sess, "NEW")     # 처음 보는 화면도 동일
    assert ws2.sent[0] == "hello world" and "다시 연결했습니다" in ws2.sent[1]
    assert sess.screens["NEW"] == sess.total_len        # 이후부터는 델타 대상


@pytest.mark.asyncio
async def test_reattach_after_buffer_overflow_falls_back_with_note(monkeypatch):
    monkeypatch.setattr(webterm, "_BUF_CAP", 10)
    sess = _live_session(["aaaa"])
    sess.mark_sent("S1", 2)                            # 'aa' 까지 봤는데
    sess.feed("bbbb"); sess.feed("cccc")               # 그 사이 버퍼가 밀려 base_off=4 > 2
    ws = await _reattach(monkeypatch, sess, "S1")
    assert ws.sent[0] == "bbbbcccc"                     # tail 폴백
    assert "생략" in ws.sent[1]                          # 정직하게 알린다
    assert sess.screens["S1"] == sess.total_len


# ---------------------------------------------------------------- reader 커서 전진

@pytest.mark.asyncio
async def test_reader_advances_cursor_of_attached_screen(monkeypatch):
    monkeypatch.setattr(webterm, "_registry_remove", lambda pid: None)
    monkeypatch.setattr(webterm, "_ACTIVE", {}, raising=True)
    sess = webterm._TermSession("sid-r", _Proc(["one ", "two ", "three"]), persist=True)
    ws = _WS()
    sess.client = (ws, asyncio.get_running_loop())
    sess.client_screen = "S1"
    sess.mark_sent("S1", 0)
    webterm._start_reader(sess)
    for _ in range(200):                                # 스레드가 EOF 까지 돌 때까지
        await asyncio.sleep(0.01)
        if sess.dead:
            break
    assert sess.dead is True
    assert "".join(ws.sent[:3]) == "one two three"      # 라이브 전송
    assert sess.screens["S1"] == len("one two three")   # 보낸 만큼 커서 전진
