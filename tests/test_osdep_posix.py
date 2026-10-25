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


@pytest.mark.parametrize("owner_ignores", [False, True], ids=["plain", "nohup"])
def test_pty_child_dies_with_its_owner(tmp_path, owner_ignores):
    """Windows Job 과 같은 약속: 주인(브로커)이 kill -9 로 죽어도 pty 안의 프로세스가 남지 않는다(SIGHUP).
    nohup: 주인이 nohup·'&' 아래 떠 HUP·INT·QUIT 를 무시해도 — 무시는 exec 로 물려가므로 spawn_pty 가 자식 쪽에서 되돌린다
    (맥 4회차: nohup 아래에서 늘 깨졌다)."""
    from session_manager import osdep
    ignore = "for s in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT): signal.signal(s, signal.SIG_IGN)" if owner_ignores else ""
    owner = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
        import signal, sys, time
        from session_manager import osdep
        %s
        p = osdep.spawn_pty(["sleep", "300"], ".", 24, 80)
        print(p.pid, flush=True)
        time.sleep(300)
    """) % ignore], stdout=subprocess.PIPE, text=True, cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    child = int(owner.stdout.readline())
    try:
        assert osdep.pid_alive(child)
        owner.kill()
        owner.wait()
        end = time.time() + 5
        while time.time() < end and osdep.pid_alive(child):
            time.sleep(0.1)
        assert not osdep.pid_alive(child), "주인이 죽으면 pty 자식도 끝나야 한다"
    finally:
        owner.kill()
        owner.wait()
        if osdep.pid_alive(child):      # 실패했을 때 sleep 300 이 다음 시험까지 남지 않게
            osdep.kill(child)


def test_pid_alive_is_false_when_reaped_between_checks(monkeypatch):
    """os.kill(pid, 0) 은 좀비라 통과했는데 psutil 이 볼 때는 거둬지는 중(리눅스 X = STATUS_DEAD)이거나 이미 거둬졌으면
    (NoSuchProcess) 죽은 것. 예전엔 좀비만 죽음으로 봐 죽은 직후 한 번 True 로 튀었다 — 위 시험이 리눅스에서 가끔 깨진 원인(실측).
    pid 는 이 프로세스(os.kill 0 이 진짜로 통과) — 전역 os.kill·psutil.Process 는 바꾸지 않는다."""
    import psutil

    from session_manager.osdep import _posix
    me = os.getpid()
    for state, alive in ((psutil.STATUS_ZOMBIE, False), (psutil.STATUS_DEAD, False),
                         (psutil.STATUS_SLEEPING, True), (psutil.STATUS_RUNNING, True)):
        monkeypatch.setattr(_posix, "_ps_status", lambda pid, st=state: st)
        assert _posix.pid_alive(me) is alive, state

    for exc, alive in ((psutil.NoSuchProcess(me), False), (psutil.ZombieProcess(me), False),
                       (psutil.AccessDenied(me), True)):
        def raise_(pid, e=exc):
            raise e
        monkeypatch.setattr(_posix, "_ps_status", raise_)
        assert _posix.pid_alive(me) is alive, type(exc).__name__
    monkeypatch.undo()
    assert _posix.pid_alive(me) is True, "진짜 psutil 로 — 살아 있는 자기 자신"


def test_orphans_adopted_by_init_count_as_parentless():
    from session_manager import webapi
    procs = [{"pid": 1, "ppid": 0, "cmdline": "init"},
             {"pid": 50, "ppid": 1, "cmdline": "claude -p --resume x --output-format stream-json"},
             {"pid": 60, "ppid": 7, "cmdline": "bash"},
             {"pid": 61, "ppid": 60, "cmdline": "claude -p --resume y --output-format stream-json"}]
    assert webapi.find_orphan_stream_pids(procs) == [50]


def test_broker_uses_private_unix_socket():
    """소켓은 데이터 폴더의 ptyd.sock — 경로가 길면(맥 소켓 경로 상한 104바이트) 임시 폴더의 사용자·경로별 이름으로.
    짧은 폴더는 /tmp 아래로 잡는다: 맥의 pytest tmp_path(/private/var/folders/…)는 그것만으로 대체 주소로 간다(경로만 계산)."""
    import tempfile
    from pathlib import Path

    from session_manager import ptyd
    assert ptyd.FAMILY == "AF_UNIX"
    short = Path("/tmp") / "clewpath-ptyd-test" / "data"
    assert ptyd.pipe_address(short) == str(short / "ptyd.sock")
    long_dir = short / ("x" * 120)
    addr = ptyd.pipe_address(long_dir)
    assert len(addr.encode()) < 104, "맥 소켓 경로 상한을 넘으면 임시 폴더로"
    assert os.path.dirname(addr) == tempfile.gettempdir() and os.path.basename(addr).startswith(f"clewpath-ptyd-{os.getuid()}-")
    assert ptyd.pipe_address(long_dir) == addr != ptyd.pipe_address(long_dir / "y"), "같은 데이터 폴더면 같은 주소, 다르면 다른 주소"


def _leave_time_wait(listener_reuse: bool = True) -> int:
    """서버 쪽이 먼저 닫은 연결을 하나 남긴다 — uvicorn 이 쉬는 keep-alive 를 5초 뒤 닫는 것과 같은 모양.
    먼저 닫은 쪽(서버 포트)에 TIME_WAIT 가 생긴다. 리스너는 닫는다(듣는 소켓 없음).
    listener_reuse: 옛 Host 의 리스너처럼 SO_REUSEADDR(uvicorn → asyncio create_server 의 POSIX 기본값)."""
    import socket
    srv = socket.socket()
    if listener_reuse:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    srv.close()
    conn.close()                      # 서버가 먼저 FIN
    cli.settimeout(5)
    assert cli.recv(1) == b""         # 클라이언트가 FIN 을 받고
    cli.close()                       # 닫으면 서버 쪽 연결이 TIME_WAIT 로
    time.sleep(0.2)
    return port


def test_port_free_sees_through_time_wait():
    """재기동 직후 5100 이 TIME_WAIT 라 5101 로 밀리던 문제(posix-install.md 0절 3): 일반 bind 는 실패해도 빈 포트다."""
    import socket

    from session_manager import osdep, server
    port = _leave_time_wait()
    with socket.socket() as s:
        with pytest.raises(OSError):
            s.bind(("127.0.0.1", port))   # 재현 확인 — SO_REUSEADDR 없는 bind 는 거절된다
    assert osdep.port_free("127.0.0.1", port) is True
    assert osdep.port_free("0.0.0.0", port) is True
    assert server.resolve_port("127.0.0.1", port, 10) == port, "폴백하지 않고 그 포트로 연다"
    with socket.socket() as s:            # 그리고 uvicorn(asyncio, SO_REUSEADDR)은 실제로 그 포트를 연다
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)


def test_port_free_matches_what_the_server_can_bind():
    """SO_REUSEADDR 없이 연 리스너가 남긴 TIME_WAIT — 서버(uvicorn, SO_REUSEADDR)가 실제로 bind 할 수 있는지와 port_free 의 답이
    같아야 한다. 리눅스는 bind 를 거절한다(TIME_WAIT 를 남긴 리스너도 SO_REUSEADDR 였을 때만 허락) → '사용 중'. 맥(BSD)은
    bind 를 허락한다 → '비어 있음'(2026-10-22 맥 실기에서 리눅스 동작을 단언하던 이 시험만 틀렸다)."""
    import socket

    from session_manager import osdep
    port = _leave_time_wait(listener_reuse=False)
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            can_bind = True
        except OSError:
            can_bind = False
    if sys.platform.startswith("linux"):
        assert can_bind is False, "리눅스: 이 TIME_WAIT 에는 SO_REUSEADDR 로도 bind 할 수 없다(시험의 전제)"
    assert osdep.port_free("127.0.0.1", port) is can_bind


def test_port_free_still_refuses_listeners():
    """듣는 소켓이 있으면 SO_REUSEADDR 로 bind 가 되더라도(맥) 사용 중이다 — connect 확인이 먼저."""
    import socket

    from session_manager import osdep
    for bind_host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind((bind_host, 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            assert osdep.port_free("127.0.0.1", port) is False, bind_host
            assert osdep.port_free("0.0.0.0", port) is False, bind_host
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert osdep.port_free("127.0.0.1", free) is True


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
