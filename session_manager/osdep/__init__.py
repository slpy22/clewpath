"""OS 의존 부분의 경계 — 공통 코드는 ConPTY·Windows API·PowerShell·sys.platform 분기를 직접 쓰지 않고 여기만 부른다.

설계 정본: docs/designs/multi-os.md (2026-10-15).

    from session_manager import osdep
    osdep.kill_tree(pid)

Windows 는 _win(지금까지의 코드를 그대로 옮긴 것), 맥·리눅스는 _posix.
_posix 에서 아직 없는 기능(키체인·launchd·재개 창·업데이트 러너)은 3단계(맥 실기)에서 채운다 — 그 전에는 None 이나 NotImplementedError.
"""
from __future__ import annotations

import sys

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    from session_manager.osdep import _win as _impl
else:
    from session_manager.osdep import _posix as _impl

NAME = _impl.NAME                       # "win" | "mac" | "linux"
NO_WINDOW = _impl.NO_WINDOW             # subprocess creationflags: 콘솔 창을 띄우지 않음(POSIX 는 0)
START_SCRIPT = _impl.START_SCRIPT       # 설치본 기동 스크립트 이름(설치 폴더 바로 아래)
UPDATE_RUNNER = _impl.UPDATE_RUNNER     # 업데이트 적용 스크립트 이름(session_manager/ 아래)
ADOPTER_PIDS = _impl.ADOPTER_PIDS       # 고아를 입양하는 pid(POSIX init=1). 고아 판정에서 '살아 있는 부모'로 치지 않는다

detached_popen_kwargs = _impl.detached_popen_kwargs
spawn_pty = _impl.spawn_pty
guard_child = _impl.guard_child
pid_alive = _impl.pid_alive
kill = _impl.kill
kill_tree = _impl.kill_tree
list_processes = _impl.list_processes
protect = _impl.protect
unprotect = _impl.unprotect
ensure_launcher = _impl.ensure_launcher
open_resume_terminal = _impl.open_resume_terminal
hidden_dirs = _impl.hidden_dirs
temp_claude_dir = _impl.temp_claude_dir
update_runner_argv = _impl.update_runner_argv

__all__ = ["IS_WIN", "IS_MAC", "NAME", "NO_WINDOW", "START_SCRIPT", "UPDATE_RUNNER", "ADOPTER_PIDS",
           "detached_popen_kwargs", "spawn_pty", "guard_child", "pid_alive", "kill", "kill_tree",
           "list_processes", "protect", "unprotect", "ensure_launcher", "open_resume_terminal",
           "hidden_dirs", "temp_claude_dir", "update_runner_argv"]
