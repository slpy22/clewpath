"""Job Object 동반 종료 - 고아 방지의 근본 장치(커널 실동작 검증).

실사용자 사고(2026-08-21): 구버전 고아 PTY 가 claude bg 에이전트로 승격돼
세션 잠금. 사후 청소는 승격 레이스를 못 이기므로, 잡 핸들이 닫히는 순간
자식이 죽는 커널 보장으로 창구 자체를 없앤다.
"""
import subprocess
import sys
import time

import pytest

from session_manager import jobguard


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_kill_on_close_terminates_child():
    job = jobguard.make_kill_on_close_job()
    assert job
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert jobguard.assign_pid(job, child.pid) is True
        assert child.poll() is None            # 편입 직후엔 살아있음
        import ctypes
        ctypes.windll.kernel32.CloseHandle(job)  # 잡 소멸 = Host 사망 시뮬
        deadline = time.time() + 5
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        assert child.poll() is not None        # 커널이 자식을 동반 종료
    finally:
        if child.poll() is None:
            child.kill()


def test_assign_invalid_pid_returns_false():
    job = jobguard.make_kill_on_close_job()
    if job is None:                            # 비윈도우 등
        assert jobguard.assign_pid(job, 1234) is False
        return
    assert jobguard.assign_pid(job, 0) is False
    assert jobguard.assign_pid(None, 1234) is False
