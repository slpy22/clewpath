"""웹터미널 좀비 방지 — 시체 PTY 는 조회 시점에 정리되어 재접속이 항상 1회에 성공.

실사고: claude 종료 후 winpty EOF 지연으로 dead=False 시체가 등록부에 남아
재연결 버튼이 2회→4회… 필요해지던 문제(사용자 실기기 보고). 상태 모델:
'연결 버튼은 항상 1번' — 죽은 프로세스는 어떤 경로로든 발견 즉시 정리.
"""
from session_manager import webterm


class _FakeProc:
    def __init__(self, alive=True, raise_on_isalive=False):
        self._alive = alive
        self._raise = raise_on_isalive
        self.terminated = False

    def isalive(self):
        if self._raise:
            raise OSError("pty gone")
        return self._alive

    def terminate(self, force=False):
        self.terminated = True
        self._alive = False


def _make(key, alive=True, raise_on_isalive=False, dead=False):
    sess = webterm._TermSession(key, _FakeProc(alive, raise_on_isalive), persist=True)
    sess.dead = dead
    webterm._ACTIVE[key] = sess
    return sess


def teardown_function(_):
    webterm._ACTIVE.clear()


def test_live_session_is_returned():
    sess = _make("s1", alive=True)
    assert webterm._get_live("s1") is sess
    assert webterm.has_terminal("s1") is True


def test_corpse_is_purged_on_lookup():
    """dead 플래그가 False 여도 isalive() False 면 시체 - 즉시 정리."""
    _make("s2", alive=False, dead=False)
    assert webterm._get_live("s2") is None
    assert "s2" not in webterm._ACTIVE          # 등록부에서 사라짐 -> 다음 접속은 새 스폰


def test_isalive_exception_treated_as_corpse():
    _make("s3", raise_on_isalive=True)
    assert webterm._get_live("s3") is None
    assert "s3" not in webterm._ACTIVE


def test_stop_terminal_on_corpse_returns_false():
    _make("s4", alive=False)
    assert webterm.stop_terminal("s4") is False   # 이미 죽음 = no_terminal
    assert "s4" not in webterm._ACTIVE


def test_stop_terminal_live_terminates():
    sess = _make("s5", alive=True)
    assert webterm.stop_terminal("s5") is True
    assert sess.proc.terminated is True
