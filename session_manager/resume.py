"""세션 재개 — claude --resume 명령을 제공하거나 새 터미널에서 실행한다.

웹 서버가 사용자의 터미널 세션을 직접 점유할 수 없으므로:
- 기본: 실행할 명령어와 작업 경로를 반환(사용자가 복사해서 실행)
- launch=True: Windows에서 새 터미널 창을 열어 claude --resume 실행
"""
from __future__ import annotations

import shlex
from pathlib import Path

from session_manager import config, osdep
from session_manager.scanner import scan_one, resolve_launch_cwd


_SKIP_FLAG = "--dangerously-skip-permissions"


def _quote(s: str) -> str:
    """사람이 붙여 넣을 셸의 따옴표 — Windows cmd 는 큰따옴표(공백 있을 때), 맥·리눅스 sh 는 작은따옴표."""
    if osdep.IS_WIN:
        return f'"{s}"' if " " in s else s
    return shlex.quote(s)


def resume_command(session_id: str, skip_permissions: bool = True) -> dict:
    """재개 명령어와 작업 경로를 반환한다."""
    meta = scan_one(session_id)
    if meta is None:
        return {"error": "세션을 찾을 수 없습니다.", "session_id": session_id}
    # fork 세션 등은 실제 저장 폴더와 맞는 실행 디렉토리에서 재개해야 claude 가 찾는다.
    cwd = resolve_launch_cwd(meta) or meta.cwd
    flag = f" {_SKIP_FLAG}" if skip_permissions else ""
    cmd = f'{_quote(config.claude_exe())} --resume {session_id}{flag}'
    if not cwd:
        full = cmd
    elif osdep.IS_WIN:
        full = f'cd /d "{cwd}" && {cmd}'
    else:                                   # 맥·리눅스 터미널(sh·zsh) — cmd 의 cd /d 는 맥 실기(3b)에서 틀린 안내였다
        full = f"cd {shlex.quote(cwd)} && {cmd}"
    return {
        "session_id": session_id,
        "cwd": cwd,
        "skip_permissions": skip_permissions,
        "command": cmd,
        "full_command": full,
        "shell": "cmd" if osdep.IS_WIN else "sh",
        "can_launch": osdep.IS_WIN,         # 새 터미널 창 열기(launch_terminal) — 맥 Terminal.app 은 아직 없다
        "note": "터미널에서 위 명령을 실행하면 이 세션을 이어서 시작합니다.",
    }


def launch_terminal(session_id: str, skip_permissions: bool = True) -> dict:
    """새 터미널 창을 열어 claude --resume을 실행한다 (Windows 우선)."""
    info = resume_command(session_id, skip_permissions)
    if "error" in info:
        return info
    cwd = info["cwd"]
    if not cwd or not Path(cwd).is_dir():
        return {**info, "launched": False,
                "error": "작업 경로가 존재하지 않아 새 터미널을 열 수 없습니다. 명령어를 직접 실행하세요."}

    flag = f" {_SKIP_FLAG}" if skip_permissions else ""
    try:
        if not osdep.open_resume_terminal(f"claude --resume {session_id}{flag}", cwd):
            # 아직 새 터미널 창을 못 여는 OS 면 명령어 반환에 의존
            return {**info, "launched": False,
                    "note": "이 플랫폼에서는 자동 실행을 지원하지 않습니다. 명령어를 직접 실행하세요."}
        return {**info, "launched": True}
    except Exception as e:
        return {**info, "launched": False, "error": str(e)}
