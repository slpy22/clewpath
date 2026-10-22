"""Windows 구현 — 0.22.19 까지 각 모듈에 흩어져 있던 코드를 동작 그대로 옮긴 것."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

NAME = "win"
NO_WINDOW = 0x08000000                  # CREATE_NO_WINDOW
_NEW_PROCESS_GROUP = 0x00000200         # CREATE_NEW_PROCESS_GROUP
ADOPTER_PIDS: frozenset = frozenset()   # Windows 는 고아를 입양하지 않는다(부모 pid 가 그냥 사라짐)
START_SCRIPT = "start-connector.ps1"
UPDATE_RUNNER = "update_runner.ps1"
CURL = "curl.exe"                       # 훅 명령 — PowerShell 의 curl 별칭(Invoke-WebRequest)을 피한다


def detached_popen_kwargs() -> dict:
    """부모와 독립으로 오래 사는 자식(브로커·업데이트 러너)의 Popen 인자.

    NEW_PROCESS_GROUP | CREATE_NO_WINDOW — '보이지 않는 콘솔' 을 준다. DETACHED_PROCESS 는 쓰지 않는다:
    - venv 런처(.venv\\Scripts\\python.exe)가 그 아래 진짜 python 을 띄울 때 콘솔이 없으면 Windows 가 **새 콘솔 창**을
      만들어 화면에 뜨고, 사용자가 그 창을 닫으면 브로커와 모든 터미널이 함께 죽는다(2026-10-10 실사고).
    - pwsh 는 콘솔이 아예 없으면 아무것도 안 하고 exit 0 으로 끝난다 — 업데이트가 '성공' 응답만 남기고 안 일어난다(실측).
    프로세스 수명은 원래 부모와 독립이라 부모가 죽어도 자식은 계속 돈다.
    """
    return {"creationflags": _NEW_PROCESS_GROUP | NO_WINDOW}


def spawn_pty(argv: list[str], cwd: str, rows: int = 24, cols: int = 80, env: dict | None = None):
    """ConPTY(pywinpty) 로 argv 를 띄운다. 따옴표 친 문자열 경로를 인식하지 못하므로 argv 는 반드시 리스트."""
    from winpty import PtyProcess
    if env is None:
        return PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols))
    return PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))


def guard_child(pid) -> bool:
    """Host 전역 Job(KILL_ON_JOB_CLOSE)에 자식을 편입 — Host 가 어떻게 죽든 커널이 함께 끝낸다."""
    from session_manager import jobguard
    return jobguard.guard(pid)


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259                           # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)


def kill_tree(pid: int) -> None:
    """pid 와 그 자식들(MCP·도구 프로세스)까지."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)


def list_processes(name_like: str | None = None) -> list[dict]:
    """[{pid, ppid, cmdline(str)}] 전체(또는 이름에 name_like 가 든 것). 실패는 예외 — 호출자가 건너뛴다."""
    flt = f" -Filter \"Name like '%{name_like}%'\"" if name_like else ""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_Process{flt} | "
         "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],
        capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
    rows = json.loads(out or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return [{"pid": int(r["ProcessId"]), "ppid": int(r.get("ParentProcessId") or 0),
             "cmdline": r.get("CommandLine") or ""} for r in rows]


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    """Windows DPAPI(사용자 스코프). 실패면 None."""
    try:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        inb = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                          ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:  # noqa: BLE001
        return None


def protect(data: bytes) -> bytes | None:
    """이 사용자만 풀 수 있게 감싼다. 못 하면 None(호출자가 평문 파일 폴백)."""
    return _dpapi(data, True)


def unprotect(data: bytes) -> bytes | None:
    return _dpapi(data, False)


def _ps_text(b: bytes) -> str:
    """PowerShell 출력 → 문자열(빈 줄·줄끝 정리). ensure_task.ps1 의 줄은 ASCII 지만 파스 에러·예외 문구는 OS 언어 +
    OEM 코드페이지(한국어 cp949, 영어 cp437)로 올 수 있다 — UTF-8 로 안 읽히면 OEM 으로."""
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        try:
            s = b.decode("oem", "replace")
        except LookupError:                     # 'oem' 코덱은 Windows 파이썬에만 있다(맥·리눅스에서 도는 시험)
            s = b.decode("utf-8", "replace")
    return "\n".join(line.rstrip() for line in s.splitlines() if line.strip())


def ensure_launcher(root: Path, data_dir: Path, conf_file: Path, port: int) -> str | None:
    """ensure_task.ps1 로 런처 v2·예약 작업 반복 트리거를 맞춘다. pwsh·powershell 이 둘 다 없으면 None.

    성공(종료 코드 0 + 출력 있음)이면 출력을 돌려준다(stderr 가 있으면 덧붙임) → liveness 가 버전 표식에 적는다.
    실패면 RuntimeError(종료 코드·출력·stderr) → liveness 가 표식을 쓰지 않고 host.log 에 남긴다 → 다음 기동에 다시.
    판정은 출력 글자가 아니라 종료 코드로 — 2026-10-22 노트북(영어 Windows·5.1): ensure_task.ps1 을 파스 못 해 빈 stdout·exit 1
    이었는데 예전엔 stdout 만 돌려줘서 빈 문자열이 표식에 써졌고(stderr 는 버려 원인도 안 남음) 0.10.5~0.31.1 내내 다시 안 했다.
    """
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return None
    args = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(root / "session_manager" / "ensure_task.ps1"),
            "-Root", str(root), "-DataDir", str(data_dir),
            "-ConfFile", str(conf_file), "-Port", str(port)]
    # 120초: 5.1 은 기동만 몇 초 — 7 에서 돌면 5.1 구문 확인·conhost 시험 실행으로 셸을 두어 번 더 띄운다
    r = subprocess.run(args, capture_output=True, timeout=120, creationflags=NO_WINDOW)
    out, err = _ps_text(r.stdout), _ps_text(r.stderr)
    text = out + (f"\nstderr: {err}" if err else "")
    if r.returncode != 0 or not out:
        raise RuntimeError(f"ensure_task exit {r.returncode}: {text[:4000] or '(출력 없음)'}")
    return text


def port_free(host: str, port: int) -> bool:
    """그 포트에 실제로 bind 가 되는지 본다(0.28 까지 server._port_free 본문 그대로).

    "연결이 되나" 로 확인하면 안 된다 — 방화벽·다른 인터페이스 때문에 오판한다.
    우리가 뜰 때와 같은 조건(같은 host)으로 bind 를 시도해 보는 게 유일하게 정확하다.
    SO_REUSEADDR 은 켜지 않는다. Windows 에선 켜면 남이 듣고 있는 포트에도 bind 돼 비어 보인다.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def open_resume_terminal(command: str, cwd: str) -> bool:
    """새 콘솔 창에서 command 를 실행한다. 띄웠으면 True.

    작업 폴더는 cd 명령(명령줄)이 아니라 subprocess cwd= 로 지정한다.
    한글 등 비ASCII 경로를 명령줄로 넘기면 새 콘솔 코드페이지에서 깨져
    "filename ... syntax is incorrect" 오류가 나기 때문. cwd= 는 유니코드 안전.
    """
    subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", command], cwd=cwd, shell=False)
    return True


def hidden_dirs() -> list[Path]:
    """파일 보기에서 늘 숨기는 OS 사용자 데이터 폴더."""
    out = [Path.home() / "AppData"]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append(Path(la))
    return out


def temp_claude_dir() -> str:
    """claude 가 세션 스크래치패드를 두는 곳."""
    home = os.path.expanduser("~")
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")), "Temp", "claude")


def update_runner_argv(runner: Path, *, install_root: Path, stage_dir: Path, backup_dir: Path, wait_pid: int,
                       port: int, version: str, log_file: Path, restart_script: str = "") -> list[str]:
    pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    args = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
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
