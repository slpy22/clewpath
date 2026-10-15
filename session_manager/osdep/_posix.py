"""macOS·리눅스 구현.

2단계(2026-10-15): pty(ptyprocess)·자식 동반 종료(pty 끊김 SIGHUP)·프로세스 목록/종료 — 리눅스 컨테이너에서 검증.
비밀 보관·자동 기동·재개 창·업데이트 러너는 3단계(맥 실기) — docs/designs/multi-os.md.
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
from pathlib import Path

NAME = "mac" if sys.platform == "darwin" else "linux"
NO_WINDOW = 0
ADOPTER_PIDS = frozenset({1})           # 부모가 죽은 프로세스를 입양하는 init/launchd — 고아 판정에선 '부모 없음'
START_SCRIPT = "start-connector.sh"
UPDATE_RUNNER = "update_runner.sh"


def detached_popen_kwargs() -> dict:
    """새 세션(setsid) — 부모의 터미널·시그널과 떨어져 오래 산다."""
    return {"start_new_session": True}


_PTY_CLS = None


def _pty_cls():
    """pywinpty.PtyProcess 와 같은 모양(read→str·write·setwinsize·isalive·terminate(force)·pid) — 둘 다 ptyprocess 계열."""
    global _PTY_CLS
    if _PTY_CLS is None:
        from ptyprocess import PtyProcessUnicode

        class _Pty(PtyProcessUnicode):
            def __init__(self, pid, fd, encoding="utf-8", codec_errors="replace"):
                super().__init__(pid, fd, encoding=encoding, codec_errors=codec_errors)   # 잘린 UTF-8 에도 안 죽게

        _PTY_CLS = _Pty
    return _PTY_CLS


def spawn_pty(argv: list[str], cwd: str, rows: int = 24, cols: int = 80, env: dict | None = None):
    """자식은 새 세션의 리더(pty 가 제어 터미널). 주인이 죽어 master 가 닫히면 커널이 SIGHUP 을 보낸다."""
    return _pty_cls().spawn(list(argv), cwd=cwd, env=env, dimensions=(rows, cols))


def guard_child(pid) -> bool:
    """주인과 함께 끝나게. pty 자식은 이미 그렇다(master 닫힘 → SIGHUP). 파이프 자식(웹 재개 -p)은 stdin EOF 로 끝나고,
    남은 것은 기동 청소(ADOPTER_PIDS)가 줍는다. 맥엔 Job·PDEATHSIG 같은 커널 장치가 없다."""
    return bool(pid)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True                     # 있지만 남의 프로세스
    except (OSError, OverflowError):    # 없음 · pid 범위 밖(다른 OS 가 남긴 표식)
        return False
    try:                                # 끝났지만 아직 거둬지지 않은 좀비는 죽은 것
        import psutil
        return psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return True


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

