"""Job Object 기반 자식 동반 종료 — 고아를 '물리적으로' 불가능하게.

기존 방어(정상 종료 shutdown_all + 등록부 sweep)는 '죽고 나서 다음 기동 때'
치우는 사후 처리라, Host 사망~다음 기동 사이에 claude 데몬이 고아를 bg
에이전트로 승격시키면 세션이 잠긴다(실사용자 사고 2026-08-21). Job Object 는
커널 장치라 이 창 자체를 없앤다: Host 가 **어떤 방식으로 죽든**(taskkill /F,
크래시, 전원) 잡 핸들이 닫히는 순간 OS 가 묶인 자식을 전부 종료한다.

persist 터미널의 의미론과도 정합: persist 는 '화면(WS) 이탈에도 유지'이지
'Host 사망에도 유지'가 아니다 — 재기동 후엔 어차피 재접속 불가라 유지 가치가
없고, 살아남으면 잠금 사고만 낸다.

[원칙] 이 잡에는 ClewPath 가 낳는 프로세스(웹터미널 PTY·웹재개 스트림)만
넣는다 — 임의의 claude 와 무관하다.
"""
from __future__ import annotations

import ctypes
import sys

_JOB_HANDLE = None
_INIT_TRIED = False

# WinAPI 상수
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def make_kill_on_close_job():
    """KILL_ON_JOB_CLOSE 잡 핸들 생성(테스트에서도 독립 잡으로 사용). 실패 시 None."""
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32
    h = k32.CreateJobObjectW(None, None)
    if not h:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(h, _JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        k32.CloseHandle(h)
        return None
    return h


def assign_pid(job_handle, pid: int) -> bool:
    """pid 를 잡에 편입. 자식이 또 낳는 프로세스도 기본 상속돼 함께 정리된다."""
    if not job_handle or not pid or sys.platform != "win32":
        return False
    k32 = ctypes.windll.kernel32
    ph = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
    if not ph:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(job_handle, ph))
    finally:
        k32.CloseHandle(ph)


def guard(pid: int) -> bool:
    """Host 전역 잡에 자식을 편입한다. 실패해도 치명 아님(등록부 sweep 이 2선).

    잡 핸들은 프로세스 수명 동안 절대 닫지 않는다 — 닫히는 순간(=Host 종료)
    이 동반 종료가 발동하는 것이 설계다.
    """
    global _JOB_HANDLE, _INIT_TRIED
    if not _INIT_TRIED:
        _INIT_TRIED = True
        _JOB_HANDLE = make_kill_on_close_job()
        if _JOB_HANDLE is None:
            print("[jobguard] Job Object 생성 실패 - 등록부 sweep 만으로 동작", flush=True)
    return assign_pid(_JOB_HANDLE, pid)
