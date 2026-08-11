"""웹터미널 프로세스-화면 분리 회귀.

핵심 계약: 화면(ws)이 떨어져도 persist 세션의 claude 는 죽지 않는다.
이게 깨지면 '알림 눌렀더니 진행 중이던 세션이 사라졌다' 사고가 재발한다.
"""
from __future__ import annotations

import pytest

from session_manager import webterm


class FakeProc:
    def __init__(self):
        self.written = []
        self.terminated = False

    def write(self, d):
        if self.terminated:
            raise OSError("dead")
        self.written.append(d)

    def terminate(self, force=False):
        self.terminated = True


@pytest.fixture(autouse=True)
def _clean():
    webterm._ACTIVE.clear()
    yield
    webterm._ACTIVE.clear()


def _mk(key="s1", persist=True):
    sess = webterm._TermSession(key, FakeProc(), persist=persist)
    webterm._ACTIVE[key] = sess
    return sess


# ---- 버퍼 ----

def test_buffer_trims_to_cap(monkeypatch):
    monkeypatch.setattr(webterm, "_BUF_CAP", 100)
    monkeypatch.setattr(webterm, "_REPLAY_TAIL", 50)
    sess = _mk()
    for _ in range(20):
        sess.feed("x" * 30)
    assert sess.buf_len <= 100 + 30          # 상한 근처 유지(청크 단위 절삭)
    assert len(sess.tail()) <= 50


# ---- 원격 승인(화면 무관) ----

def test_write_to_works_without_client():
    sess = _mk()
    assert sess.client is None               # 화면 없음
    assert webterm.write_to("s1", "1") is True
    assert sess.proc.written == ["1"]


def test_write_to_dead_proc_cleans_up():
    sess = _mk()
    sess.proc.terminated = True
    assert webterm.write_to("s1", "1") is False
    assert not webterm.has_terminal("s1")    # 죽은 세션은 걷어냄


def test_write_to_unknown():
    assert webterm.write_to("nope", "1") is False


# ---- 생존 계약 ----

def test_cleanup_terminates_and_unregisters():
    sess = _mk()
    webterm._cleanup(sess)
    assert sess.proc.terminated and sess.dead
    assert not webterm.has_terminal("s1")


def test_persist_flag_semantics():
    """persist=True(일반) 는 화면이 떨어져도 유지, False(fork) 는 종료 대상."""
    a = _mk("normal", persist=True)
    b = _mk("fork1", persist=False)
    # run_terminal 의 finally 정책을 그대로 재현
    for sess in (a, b):
        sess.client = None
        if not sess.persist:
            webterm._cleanup(sess)
    assert webterm.has_terminal("normal") and not a.proc.terminated
    assert not webterm.has_terminal("fork1") and b.proc.terminated
