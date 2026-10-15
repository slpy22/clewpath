"""macOS·리눅스 구현.

1단계(2026-10-15): psutil·os.kill 로 바로 되는 것만. pty·자식 동반 종료·비밀 보관·자동 기동·업데이트 러너는
2단계(리눅스 컨테이너 검증)·3단계(맥 실기) 에서 채운다 — docs/designs/multi-os.md.
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
from pathlib import Path

NAME = "mac" if sys.platform == "darwin" else "linux"
NO_WINDOW = 0
START_SCRIPT = "start-connector.sh"
UPDATE_RUNNER = "update_runner.sh"


def detached_popen_kwargs() -> dict:
    """새 세션(setsid) — 부모의 터미널·시그널과 떨어져 오래 산다."""
    return {"start_new_session": True}


def spawn_pty(argv: list[str], cwd: str, rows: int = 24, cols: int = 80, env: dict | None = None):
    raise NotImplementedError("POSIX 터미널은 아직 없습니다(멀티 OS 2단계)")


def guard_child(pid) -> bool:
    return False                        # 2단계: 프로세스 그룹 + 브로커 감시


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True                     # 있지만 남의 프로세스
    except OSError:
        return False


def kill(pid: int) -> None:
    try:
        os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_tree(pid: int) -> None:
    import psutil
    pr = psutil.Process(pid)
    for c in pr.children(recursive=True):
        c.kill()
    pr.kill()


def list_processes(name_like: str | None = None) -> list[dict]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        i = p.info
        if name_like and name_like.lower() not in (i.get("name") or "").lower():
            continue
        out.append({"pid": int(i["pid"]), "ppid": int(i.get("ppid") or 0),
                    "cmdline": " ".join(i.get("cmdline") or [])})
    return out


def protect(data: bytes) -> bytes | None:
    return None                         # 3단계: 키체인. 그 전엔 호출자의 0600 파일 폴백


def unprotect(data: bytes) -> bytes | None:
    return None


def ensure_launcher(root: Path, data_dir: Path, conf_file: Path, port: int) -> str | None:
    return None                         # 3단계: launchd plist 점검


def open_resume_terminal(command: str, cwd: str) -> bool:
    return False                        # 3단계: Terminal.app. 그 전엔 명령어 안내


def hidden_dirs() -> list[Path]:
    return [Path.home() / "Library"] if sys.platform == "darwin" else []


def temp_claude_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "claude")


def update_runner_argv(runner: Path, **_kw) -> list[str]:
    raise NotImplementedError("POSIX 업데이트 러너는 아직 없습니다(멀티 OS 3단계)")

