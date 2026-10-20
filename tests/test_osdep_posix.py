"""멀티 OS 2단계: 맥·리눅스의 실제 pty·주인 사망 시 동반 종료·브로커 소켓 (docs/designs/multi-os.md).

Windows 에선 건너뛴다. 리눅스 검증 컨테이너(`docker run --init`)와 맥에서 돈다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX 전용")


def _read_until(proc, needle: str, timeout: float = 5.0) -> str:
    buf, end = "", time.time() + timeout
    while time.time() < end and needle not in buf:
        try:
            buf += proc.read(1024)
        except EOFError:
            break
    return buf


def test_spawn_pty_echo_input_resize_terminate(tmp_path):
    from session_manager import osdep
    p = osdep.spawn_pty(["sh", "-c", "echo 시작; read x; echo got:$x; stty size; sleep 30"], str(tmp_path), 24, 80)
    try:
        assert p.pid and p.isalive()
        assert "시작" in _read_until(p, "시작"), "한글 UTF-8 출력이 str 로"
        p.setwinsize(40, 120)
        p.write("하나\r")
        out = _read_until(p, "40 120")
        assert "got:하나" in out and "40 120" in out
    finally:
        p.terminate(force=True)
    assert not p.isalive()


def test_pty_child_dies_with_its_owner(tmp_path):
    """Windows Job 과 같은 약속: 주인(브로커)이 kill -9 로 죽어도 pty 안의 프로세스가 남지 않는다(SIGHUP)."""
    from session_manager import osdep
    owner = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
        import sys, time
        from session_manager import osdep
        p = osdep.spawn_pty(["sleep", "300"], ".", 24, 80)
        print(p.pid, flush=True)
        time.sleep(300)
    """)], stdout=subprocess.PIPE, text=True, cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    child = int(owner.stdout.readline())
    assert osdep.pid_alive(child)
    owner.kill()
    owner.wait()
    end = time.time() + 5
    while time.time() < end and osdep.pid_alive(child):
        time.sleep(0.1)
    assert not osdep.pid_alive(child), "주인이 죽으면 pty 자식도 끝나야 한다"


def test_orphans_adopted_by_init_count_as_parentless():
    from session_manager import webapi
    procs = [{"pid": 1, "ppid": 0, "cmdline": "init"},
             {"pid": 50, "ppid": 1, "cmdline": "claude -p --resume x --output-format stream-json"},
             {"pid": 60, "ppid": 7, "cmdline": "bash"},
             {"pid": 61, "ppid": 60, "cmdline": "claude -p --resume y --output-format stream-json"}]
    assert webapi.find_orphan_stream_pids(procs) == [50]


def test_broker_uses_private_unix_socket(tmp_path):
    from session_manager import ptyd
    assert ptyd.FAMILY == "AF_UNIX"
    addr = ptyd.pipe_address(tmp_path)
    assert addr == str(tmp_path / "ptyd.sock")
    long_dir = tmp_path / ("x" * 120)
    assert len(ptyd.pipe_address(long_dir).encode()) < 104, "맥 소켓 경로 상한을 넘으면 임시 폴더로"


def test_kill_tree_and_process_listing(tmp_path):
    from session_manager import osdep
    p = subprocess.Popen(["sh", "-c", "sleep 300 & wait"])
    try:
        time.sleep(0.3)
        mine = [x for x in osdep.list_processes("sleep") if x["ppid"] == p.pid]
        assert mine and "sleep 300" in mine[0]["cmdline"]
        osdep.kill_tree(p.pid)
        p.wait(timeout=5)
        time.sleep(0.2)
        assert not osdep.pid_alive(mine[0]["pid"])
    finally:
        if p.poll() is None:
            p.kill()
