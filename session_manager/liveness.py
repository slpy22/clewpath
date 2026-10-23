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


_MARK_KEEP = 8


def _read_marks(sd: dict | None) -> dict:
    """shutdown.json → {pid(str): {reason, at}}. 구형(단일 {"pid":…}) 도 읽는다."""
    if not sd:
        return {}
    if isinstance(sd.get("pids"), dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in sd["pids"].items()}
    if sd.get("pid"):
        return {str(sd["pid"]): {"reason": sd.get("reason"), "at": sd.get("at")}}
    return {}


def mark_shutdown(reason: str, pid: int | None = None) -> None:
    """정상 종료 표식. 다음 기동이 '의심' 판정에서 제외한다.

    pid 별로 쌓는다(0.10.2): 업데이트 적용 중 옛 Host 가 남긴 표식을 그 사이 뜬 다른(짧은) 프로세스의
    정상 종료 표식이 **덮어써** 새 Host 가 옛 Host 를 '비정상 종료' 로 오판하고 복구 알림을 보내던 사고
    (2026-09-30 0.10.1 apply, pid 143148 표식이 pid 77156 로 교체됨). 최근 _MARK_KEEP 개만 보관.
    pid: 남의 종료를 대신 적을 때(맥·리눅스 런처의 강제 stop 이 죽인 Host). 없으면 이 프로세스.
    """
    try:
        p = shutdown_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        marks = _read_marks(_read_json(p))
        marks[str(pid or os.getpid())] = {"reason": reason, "at": int(time.time())}
        if len(marks) > _MARK_KEEP:
            for k in sorted(marks, key=lambda k: int((marks[k] or {}).get("at") or 0))[: len(marks) - _MARK_KEEP]:
                marks.pop(k, None)
        p.write_text(json.dumps({"pids": marks}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int) -> bool:
    from session_manager import osdep
    return osdep.pid_alive(pid)


LAUNCHER_MARK = b"=== launcher start"     # 맥·리눅스 런처가 기동 직전 host.log 에 넣는 구분줄(이어쓰기라 옛 꼬리가 남는다)
_PUSHED_FRESH = 900                       # Windows 런처가 민 host.log.1 을 이번 기동의 것으로 보는 시간(초)


def log_tail(n: int = 10) -> list[str]:
    """host.log 마지막 n 줄(ANSI 제거, 줄당 200자). 새 프로세스의 출력이 섞이기 전에 읽는다.

    마지막 런처 구분줄이 있으면 그 앞까지만 — 새 Host 가 이미 찍은 첫 줄들이 죽은 Host 의 꼬리에 섞이지 않게.
    그 앞이 비었으면 런처가 기동 직전(구분줄 바로 앞)에 5MB 회전을 한 것 — 죽은 Host 의 꼬리는 host.log.1 에 있다.
    구분줄이 없는 로그(Windows — Start-Process 가 새로 쓴다): 런처 v3 은 띄우기 직전 옛 로그 끝에 구분줄을 붙여 host.log.1 로
    민다. .1 이 구분줄로 끝나고(뒤에 아무것도 없음) 방금 밀린 것이면 그 앞이 죽은 Host 의 꼬리, 아니면(옛 런처) host.log 그대로.
    """
    def tail_of(p: Path) -> bytes | None:
        try:
            with p.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 20_000))
                return f.read()
        except OSError:
            return None

    def lines_of(raw: bytes) -> list[str]:
        return [_ANSI.sub("", ln).strip()[:200] for ln in raw.decode("utf-8", "replace").splitlines() if ln.strip()]

    p = config.data_dir() / "host.log"
    dot1 = p.with_name(p.name + ".1")
    raw = tail_of(p)
    cut = raw.rfind(LAUNCHER_MARK) if raw is not None else -1
    if cut >= 0:
        out = lines_of(raw[:cut])
        if not out:
            out = lines_of(tail_of(dot1) or b"")
        return out[-n:]
    r1 = tail_of(dot1)
    if r1:
        c1 = r1.rfind(LAUNCHER_MARK)
        try:
            fresh = time.time() - dot1.stat().st_mtime < _PUSHED_FRESH
        except OSError:
            fresh = False
        if c1 >= 0 and fresh and not r1[c1:].partition(b"\n")[2].strip():
            return lines_of(r1[:c1])[-n:]
    return lines_of(raw)[-n:] if raw is not None else []


def check_previous_exit() -> dict | None:
    """기동 직후(runtime.json 을 덮어쓰기 전) 호출. 의심 사고면 기록하고 dict 반환."""
    global LAST_INCIDENT
    rt = _read_json(config.data_dir() / "runtime.json")
    if not rt or not rt.get("pid") or int(rt["pid"]) == os.getpid():
        return None
    prev_pid = int(rt["pid"])
    marks = _read_marks(_read_json(shutdown_file()))
    if str(prev_pid) in marks:
        marks.pop(str(prev_pid), None)                          # 표식 소비(다른 pid 의 표식은 남긴다)
        try:
            if marks:
                shutdown_file().write_text(json.dumps({"pids": marks}), encoding="utf-8")
            else:
                shutdown_file().unlink()
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
    """설치본이면 런처·자동 시작을 맞춘다(Windows: ensure_task.ps1 → 런처 v2·예약 작업, 맥·리눅스: start-connector.sh 를
    지금 템플릿으로 다시 렌더). 버전당 1회(성공한 버전만 표식), 개발 트리는 건너뜀.

    표식은 성공했을 때만 쓴다. osdep 이 예외로 실패를 알리면(Windows: ensure_task.ps1 종료 코드 ≠ 0·빈 출력·시간 초과)
    표식 없이 'ensure failed: …' 를 돌려줘 host.log 에 남기고 다음 기동에 다시 한다 — 2026-10-22 노트북은 실패(빈 출력)도
    표식에 써서 16개 버전 동안 다시 시도하지 않았다. 업데이트로 버전이 바뀌면 표식 이름이 달라 새 코드로 한 번 더 돈다."""
    from session_manager import appconfig, osdep
    root = _install_root()
    if not (root / osdep.START_SCRIPT).is_file():
        return None                                             # 설치본 레이아웃이 아님(개발 실행)
    marker = config.data_dir() / f"launcher-ensured-{_version()}"
    if marker.exists():
        return None
    try:
        out = osdep.ensure_launcher(root, config.data_dir(), appconfig.config_path(), port or 5100)
        if out is None:
            return None
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
            if out and out.startswith("ensure failed"):
                print(f"[liveness] 런처 보장 실패(표식 안 씀, 다음 기동에 다시): {out.replace(chr(10), ' | ')}", flush=True)
            elif out:
                print(f"[liveness] 런처 보장: {out.replace(chr(10), ' | ')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[liveness] 런처 보장 실패: {e}", flush=True)
    threading.Thread(target=run, name="liveness", daemon=True).start()
