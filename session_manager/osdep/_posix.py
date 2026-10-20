"""macOS·리눅스 구현.

2단계(2026-10-15): pty(ptyprocess)·자식 동반 종료(pty 끊김 SIGHUP)·프로세스 목록/종료 — 리눅스 컨테이너에서 검증.
공통 설치·업데이트 단계(docs/designs/posix-install.md): 업데이트 러너 argv·런처 렌더(start-connector.sh)·포트 판정.
비밀 보관(키체인)·자동 기동(launchd·systemd)·재개 창은 맥 실기 단계 — docs/designs/multi-os.md.
"""
from __future__ import annotations

import errno
import os
import re
import shlex
import signal
import socket
import sys
import tempfile
from pathlib import Path

NAME = "mac" if sys.platform == "darwin" else "linux"
NO_WINDOW = 0
ADOPTER_PIDS = frozenset({1})           # 부모가 죽은 프로세스를 입양하는 init/launchd — 고아 판정에선 '부모 없음'
START_SCRIPT = "start-connector.sh"
UPDATE_RUNNER = "update_runner.sh"
CURL = "curl"                           # 훅 명령(hooks.hook_command) — 맥·리눅스엔 curl.exe 가 없다
LAUNCHER_TEMPLATE = "start-connector.template.sh"   # session_manager/ 아래. 설치본 런처(START_SCRIPT)의 원본


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


_PLACEHOLDER = re.compile(r"\{\{(DATA_DIR|CONF_FILE|PORT)\}\}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def render_launcher(template: str, *, data_dir: os.PathLike | str, conf_file: os.PathLike | str, port: int) -> str:
    """런처 템플릿의 자리표시를 채운다. 값은 shlex.quote — 공백·한글·작은따옴표·$ 가 든 경로도 그대로 한 낱말.

    sed 치환(install.ps1 의 -replace)처럼 문자열을 셸에 그냥 꽂으면 경로 하나로 런처가 깨지거나 명령이 끼어든다.
    한 번에 치환한다 — 경로에 '{{PORT}}' 같은 글자가 있어도 두 번 바뀌지 않게.
    """
    vals = {}
    for key, p in (("DATA_DIR", data_dir), ("CONF_FILE", conf_file)):
        s = os.fspath(p)
        if not isinstance(s, str) or _CTRL.search(s):          # 줄바꿈·NUL 이면 런처 한 줄이 둘이 된다
            raise ValueError(f"{key} 경로에 쓸 수 없는 글자가 있습니다: {s!r}")
        if not s.startswith("/"):                               # 런처는 아무 폴더에서나 불린다
            raise ValueError(f"{key} 는 절대 경로여야 합니다: {s!r}")
        vals[key] = shlex.quote(s)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"포트가 잘못됐습니다: {port!r}")
    vals["PORT"] = str(port)
    text = template.lstrip("\ufeff").replace("\r\n", "\n")      # 편집기가 남긴 BOM·CRLF 는 sh 를 깨뜨린다
    for key in vals:
        if "{{" + key + "}}" not in text:
            raise ValueError(f"런처 템플릿에 {{{{{key}}}}} 가 없습니다")
    if "{{" in _PLACEHOLDER.sub("", text):                      # 모르는 자리표시가 남으면 반쯤 렌더된 런처가 된다
        raise ValueError("런처 템플릿에 모르는 자리표시가 있습니다")
    return _PLACEHOLDER.sub(lambda m: vals[m.group(1)], text)


def _launcher_port(text: str) -> int | None:
    m = re.search(r"^DEFAULT_PORT=['\"]?(\d{1,5})['\"]?[ \t]*$", text, re.M)
    if m and 1 <= int(m.group(1)) <= 65535:
        return int(m.group(1))
    return None


def write_launcher(root: Path, *, data_dir: os.PathLike | str, conf_file: os.PathLike | str, port: int) -> str:
    """설치 루트의 템플릿으로 start-connector.sh 를 쓴다 — 렌더 → 비교 → 원자 교체(0755, BOM 없는 UTF-8·LF).

    반환 "created" | "updated" | "unchanged". 템플릿이 없거나 렌더가 안 되면 예외(그때 기존 런처는 그대로다).
    바꿔치기는 os.replace — 돌고 있는 watch 셸은 옛 inode 를 끝까지 읽는다. install.sh(hostctl)·ensure_launcher 가 쓴다.
    """
    tpl = Path(root) / "session_manager" / LAUNCHER_TEMPLATE
    target = Path(root) / START_SCRIPT
    new = render_launcher(tpl.read_text(encoding="utf-8"), data_dir=data_dir, conf_file=conf_file,
                          port=port).encode("utf-8")
    old = target.read_bytes() if target.is_file() else None
    if old == new:
        return "unchanged"
    tmp = target.with_name(f".{START_SCRIPT}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(new)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return "created" if old is None else "updated"


def ensure_launcher(root: Path, data_dir: Path, conf_file: Path, port: int) -> str | None:
    """설치본의 start-connector.sh 를 지금 코드의 템플릿으로 맞춘다(write_launcher — 렌더 → 비교 → 원자 교체).

    Windows 의 ensure_task.ps1 이 'v2 표식'만 보는 것과 달리 내용 전체를 비교한다 — 템플릿을 고친 업데이트도 전파된다.
    포트는 기존 런처의 DEFAULT_PORT 를 우선한다: 호출자(liveness)가 넘기는 것은 이번에 실제로 연 포트라,
    폴백 포트(5101)로 뜬 날 설정 포트(5100)를 덮어쓰면 안 된다.
    자동 시작(launchd·systemd) 점검은 다음 단계(그때도 렌더 성공 뒤에만).
    """
    try:
        if not (Path(root) / "session_manager" / LAUNCHER_TEMPLATE).is_file():
            return "launcher: template missing — skip"
        target = Path(root) / START_SCRIPT
        keep = _launcher_port(target.read_bytes().decode("utf-8", "replace")) if target.is_file() else None
        res = write_launcher(root, data_dir=data_dir, conf_file=conf_file, port=keep or int(port))
        return "launcher: ok (posix v2)" if res == "unchanged" else "launcher: regenerated (posix v2)"
    except Exception as e:  # noqa: BLE001
        return f"launcher: regenerate failed: {type(e).__name__}: {e}"


def port_free(host: str, port: int) -> bool:
    """그 포트로 서버를 열 수 있는가.

    ① SO_REUSEADDR 없이 bind 가 되면 비어 있다(Windows 와 같은 판정).
    ② 안 되면 TIME_WAIT 때문일 수 있다 — uvicorn 은 쉬는 keep-alive 연결을 서버 쪽에서 먼저 닫아, 재기동 직후 5100 에
       TIME_WAIT 가 남고 ① 은 '사용 중'으로 본다(그래서 5101 로 밀렸다). 루프백 connect 가 거절될 때만(= 듣는 소켓 없음)
       SO_REUSEADDR 로 다시 bind 해 본다. uvicorn 자신도 POSIX 에선 SO_REUSEADDR 로 bind 한다(asyncio create_server 기본).
       리눅스는 TIME_WAIT 를 남긴 리스너도 SO_REUSEADDR 였을 때만 허락한다 — 옛 Host(uvicorn)는 그렇고, 아닌 프로그램이
       남긴 것이면 uvicorn 도 못 여니 False 가 맞다. 답의 뜻 = '서버가 실제로 bind 할 수 있나'.
    connect 확인이 먼저인 이유: 맥(BSD)의 SO_REUSEADDR 는 와일드카드 리스너 옆 bind 도 허용해, 쓰는 포트도 비어 보인다.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                return False
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((probe, port), timeout=0.5):
            return False                                        # 누가 듣고 있다
    except ConnectionRefusedError:
        pass                                                    # 듣는 소켓 없음 — TIME_WAIT 뿐
    except OSError:
        return False                                            # 시간 초과 등 — 모르면 '사용 중'
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def open_resume_terminal(command: str, cwd: str) -> bool:
    return False                        # 3단계: Terminal.app. 그 전엔 명령어 안내


def hidden_dirs() -> list[Path]:
    return [Path.home() / "Library"] if sys.platform == "darwin" else []


def temp_claude_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "claude")


def update_runner_argv(runner: Path, *, install_root: Path, stage_dir: Path, backup_dir: Path, wait_pid: int,
                       port: int, version: str, log_file: Path, restart_script: str = "") -> list[str]:
    """update_runner.sh 는 실행 비트에 기대지 않고 /bin/sh 로 부른다(zip·copy2 는 0644 그대로). 플래그 이름은 ps1 과 같다."""
    args = ["/bin/sh", str(runner),
            "-InstallRoot", str(install_root),
            "-StageDir", str(stage_dir),
            "-BackupDir", str(backup_dir),
            "-WaitPid", str(wait_pid),
            "-Port", str(port),
            "-Version", str(version),
            "-LogFile", str(log_file)]
    if restart_script:
        args += ["-RestartScript", restart_script]
    return args

