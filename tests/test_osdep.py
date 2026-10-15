"""멀티 OS 1단계(docs/designs/multi-os.md): OS 의존 코드는 session_manager/osdep 뒤에만 있다."""
from __future__ import annotations

import inspect
import os
import re
import sys
from pathlib import Path

from session_manager import osdep
from session_manager.osdep import _posix, _win

PKG = Path(__file__).resolve().parents[1] / "session_manager"

# 아직 경계 밖에 남은 Windows 전용 모듈(이유)
_ALLOWED = {
    "osdep/_win.py",          # Windows 구현 자체
    "jobguard.py",            # Job 객체 — _win.guard_child 가 부른다
    "peers.py",               # psutil 이 빠진 설치본용 CIM 폴백(os.name 으로 막혀 있음)
    "ptyd.py",                # 바꾸면 모든 PC 에 브로커 교체 대기가 뜬다 → 2단계에서 POSIX pty 와 함께
}
_WIN_ONLY = re.compile(r"from winpty|import winpty|windll|\"powershell\"|'powershell'|\"taskkill\"|Get-CimInstance")


def test_common_code_has_no_windows_calls():
    bad = []
    for p in PKG.rglob("*.py"):
        rel = p.relative_to(PKG).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            if _WIN_ONLY.search(line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, "OS 의존 호출은 osdep 으로:\n" + "\n".join(bad)


def test_both_implementations_expose_the_same_api():
    for name in osdep.__all__:
        if name in ("IS_WIN", "IS_MAC"):
            continue
        a, b = getattr(_win, name), getattr(_posix, name)
        assert callable(a) == callable(b), name
        if callable(a) and name != "update_runner_argv":
            assert list(inspect.signature(a).parameters) == list(inspect.signature(b).parameters), name


def test_picks_the_running_os():
    assert osdep.NAME == ("win" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux")
    assert osdep.pid_alive(os.getpid())
    assert not osdep.pid_alive(0)


def test_posix_process_listing_uses_psutil_shape():
    me = [p for p in _posix.list_processes("python") if p["pid"] == os.getpid()]
    assert me and set(me[0]) == {"pid", "ppid", "cmdline"} and isinstance(me[0]["cmdline"], str)


def test_win_update_runner_args_unchanged():
    a = _win.update_runner_argv(Path("r.ps1"), install_root=Path("I"), stage_dir=Path("S"), backup_dir=Path("B"),
                                wait_pid=7, port=5100, version="1.2.3", log_file=Path("L"), restart_script="X.ps1")
    assert a[1:6] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "r.ps1"]
    assert a[a.index("-WaitPid") + 1] == "7" and a[-2:] == ["-RestartScript", "X.ps1"]
    assert "-RestartScript" not in _win.update_runner_argv(
        Path("r.ps1"), install_root=Path("I"), stage_dir=Path("S"), backup_dir=Path("B"),
        wait_pid=7, port=5100, version="1", log_file=Path("L"))
