"""Host 자기 회복(v0.9.2) — 비정상 종료 의심 감지 + 런처/작업 보장.

사고(2026-09-23): Host 가 흔적 없이 사라졌고(트레이스백·정상 종료 로그·이벤트 없음) 런처가
로그온 시 1회라 8시간 다운. 크로스체크 결론(Codex·Claude): 원인 규명과 복구는 별개 —
① 5분 반복 트리거 + 멱등 런처로 무인 복구, ② 기동 시 "이전 프로세스가 정상 종료 기록 없이
사라졌음(의심)" 을 감지해 마지막 로그를 동봉한 푸시 1건(Gemini: 진단형 알림) → 다음 사고의 실마리.

정상 종료 표식(shutdown.json)은 lifespan 종료와 업데이트 적용(runner 가 우리를 죽이기 전)이 남긴다.
runtime.json 의 pid 가 살아 있으면(개발 서버 등 다른 인스턴스) 사고로 보지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from session_manager import config

LAST_INCIDENT: dict | None = None
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def shutdown_file() -> Path:
    return config.data_dir() / "shutdown.json"


def incidents_file() -> Path:
    return config.data_dir() / "incidents.jsonl"


def mark_shutdown(reason: str) -> None:
    """정상 종료 표식. 다음 기동이 '의심' 판정에서 제외한다."""
    try:
        p = shutdown_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": os.getpid(), "reason": reason, "at": int(time.time())}),
                     encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
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
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def log_tail(n: int = 10) -> list[str]:
    """host.log 마지막 n 줄(ANSI 제거, 줄당 200자). 새 프로세스의 출력이 섞이기 전에 읽는다."""
    p = config.data_dir() / "host.log"
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 20_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = [_ANSI.sub("", ln).strip()[:200] for ln in lines if ln.strip()]
    return out[-n:]


def check_previous_exit() -> dict | None:
    """기동 직후(runtime.json 을 덮어쓰기 전) 호출. 의심 사고면 기록하고 dict 반환."""
    global LAST_INCIDENT
    rt = _read_json(config.data_dir() / "runtime.json")
    if not rt or not rt.get("pid") or int(rt["pid"]) == os.getpid():
        return None
    prev_pid = int(rt["pid"])
    sd = _read_json(shutdown_file())
    if sd and int(sd.get("pid") or 0) == prev_pid:
        try:
            shutdown_file().unlink()                            # 표식 소비
        except OSError:
            pass
        return None
    if pid_alive(prev_pid):
        return None                                             # 다른 인스턴스가 살아 있음(사고 아님)
    inc = {"at": int(time.time()), "prev_pid": prev_pid,
           "prev_started_at": rt.get("started_at"), "tail": log_tail(10)}
    try:
        p = incidents_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(inc, ensure_ascii=False) + "\n")
    except OSError:
        pass
    LAST_INCIDENT = inc
    return inc


def notify_incident(inc: dict) -> int:
    from session_manager import push
    tail = "\n".join(inc.get("tail") or [])[-220:]
    body = (f"이전 프로세스 {inc.get('prev_pid')} 가 정상 종료 기록 없이 사라졌습니다(의심). "
            f"자동 복구됨. 마지막 로그:\n{tail}")[:300]
    return push.send("host-recovered", f"host-{inc.get('prev_pid')}",
                     "ClewPath Host 가 비정상 종료 뒤 복구됨", body)


# ---------------------------------------------------------------- 런처·작업 보장

def _install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("clewpath-host")
    except Exception:  # noqa: BLE001
        return "0"


def ensure_launcher(port: int | None = None) -> str | None:
    """설치본이면 ensure_task.ps1 로 런처 v2·반복 트리거를 맞춘다(버전당 1회). 개발 트리는 건너뜀."""
    if sys.platform != "win32":
        return None
    root = _install_root()
    if not (root / "start-connector.ps1").is_file():
        return None                                             # 설치본 레이아웃이 아님(개발 실행)
    marker = config.data_dir() / f"launcher-ensured-{_version()}"
    if marker.exists():
        return None
    import shutil
    import subprocess
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return None
    from session_manager import appconfig
    args = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(root / "session_manager" / "ensure_task.ps1"),
            "-Root", str(root), "-DataDir", str(config.data_dir()),
            "-ConfFile", str(appconfig.config_path()), "-Port", str(port or 5100)]
    try:
        r = subprocess.run(args, capture_output=True, timeout=60,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        out = r.stdout.decode("utf-8", "replace").strip()
        marker.write_text(out, encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001
        return f"ensure failed: {e}"


def on_startup_async(port: int | None) -> None:
    """lifespan 에서 스레드로: 사고 푸시 + 런처 보장. 기동 경로를 막지 않는다."""
    def run():
        if LAST_INCIDENT:
            try:
                n = notify_incident(LAST_INCIDENT)
                print(f"[liveness] 비정상 종료 의심(pid {LAST_INCIDENT.get('prev_pid')}) → 복구 알림 {n}건", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[liveness] 알림 실패: {e}", flush=True)
        try:
            out = ensure_launcher(port)
            if out:
                print(f"[liveness] 런처 보장: {out.replace(chr(10), ' | ')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[liveness] 런처 보장 실패: {e}", flush=True)
    threading.Thread(target=run, name="liveness", daemon=True).start()
