"""Host 쪽 브로커 클라이언트 — webterm 이 '브로커 안의 claude' 를 자기 PTY 처럼 다루게 한다.

webterm 의 _TermSession 은 그대로 두고 `proc` 자리에 BrokerProc 를 끼운다(read/write/setwinsize/isalive/terminate).
그래서 링 버퍼·델타 재생·화면 인계·상한 같은 Host 로직은 변하지 않는다. Host 가 재기동되면 adopt() 가
브로커의 세션 목록으로 _TermSession 을 다시 만든다(첫 데이터 = 브로커가 보낸 꼬리).

설정: 환경변수 SM_PTY_BROKER(on|off) > clewpath.toml [terminal] broker > 기본 on(Windows). off = 지금까지의 Host 내장 PTY.
D3(2026-10-08): on 인데 브로커가 응답하지 않으면 **터미널 열기를 거부**한다(조용히 내장 PTY 로 폴백하지 않는다).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

from session_manager import ptyd

_lock = threading.Lock()
_ctl = None                 # 제어 연결(요청-응답, _lock 으로 직렬화)
_last_hello: dict = {}
_LAUNCH_WAIT_S = 10.0


class BrokerDown(Exception):
    pass


def enabled() -> bool:
    if sys.platform != "win32" and not os.environ.get("SM_PTY_BROKER"):
        return False
    v = os.environ.get("SM_PTY_BROKER")
    if v is None:
        try:
            from session_manager import appconfig
            v = appconfig.get("terminal", "broker", "on")
        except Exception:  # noqa: BLE001
            v = "on"
    return str(v).strip().lower() not in ("off", "0", "false", "no")


def _connect():
    from multiprocessing.connection import Client
    key = ptyd.authkey(create=False)
    if key is None:
        raise BrokerDown("no_key")
    return Client(ptyd.pipe_address(), family="AF_PIPE", authkey=key)


def _rpc(conn, req: dict) -> dict:
    conn.send_bytes(json.dumps(req, ensure_ascii=False).encode("utf-8"))
    return json.loads(conn.recv_bytes().decode("utf-8"))


def request(op: str, **kw) -> dict:
    """제어 요청. 연결이 끊겼으면 한 번 다시 붙는다. 브로커가 없으면 BrokerDown."""
    global _ctl
    with _lock:
        for attempt in (0, 1):
            try:
                if _ctl is None:
                    _ctl = _connect()
                return _rpc(_ctl, {"op": op, **kw})
            except BrokerDown:
                raise
            except Exception as e:  # noqa: BLE001
                try:
                    if _ctl is not None:
                        _ctl.close()
                except Exception:  # noqa: BLE001
                    pass
                _ctl = None
                if attempt:
                    raise BrokerDown(f"{type(e).__name__}: {e}") from e
    raise BrokerDown("unreachable")


def hello() -> dict | None:
    global _last_hello
    try:
        r = request("hello")
        _last_hello = r
        return r
    except BrokerDown:
        return None


def broker_pid() -> int | None:
    return (_last_hello or {}).get("pid")


def launch() -> None:
    """브로커를 분리 기동한다 — Host 잡에 넣지 않으므로 Host 가 죽어도 산다(S1 실측)."""
    ptyd.authkey(create=True)
    log = ptyd.state_path().with_name("ptyd.log")
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200 | 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    with open(log, "ab") as fh:
        subprocess.Popen([sys.executable, "-m", "session_manager.ptyd"], stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
                         env=ptyd.clean_env(), cwd=str(ptyd.state_path().parent))


def ensure(start: bool = True) -> dict:
    """브로커 응답을 보장한다(필요하면 기동하고 기다림). 실패하면 BrokerDown."""
    h = hello()
    if h and h.get("ok"):
        return h
    if not start:
        raise BrokerDown("not_running")
    launch()
    deadline = time.time() + _LAUNCH_WAIT_S
    while time.time() < deadline:
        time.sleep(0.25)
        h = hello()
        if h and h.get("ok"):
            return h
    raise BrokerDown("launch_timeout")


class BrokerProc:
    """브로커 안의 claude 하나 — winpty.PtyProcess 와 같은 모양(webterm 이 그대로 쓴다)."""

    def __init__(self, key: str, pid: int | None, stream):
        self.key = key
        self.pid = pid
        self._stream = stream
        self._eof = False
        self.exited = False          # 세션 자체가 끝났다(브로커가 종료 표식을 보냄) ↔ 브로커 사망·분리

    def read(self) -> str:
        if self._eof:
            raise EOFError
        try:
            b = self._stream.recv_bytes()
        except Exception as e:  # noqa: BLE001
            self._eof = True
            raise EOFError from e
        if b == ptyd.EXIT_SENTINEL:
            self._eof = True
            self.exited = True
            raise EOFError
        return b.decode("utf-8", "replace")

    def write(self, data: str) -> None:
        r = request("write", key=self.key, data=data)
        if not r.get("ok"):
            raise OSError(r.get("error") or "write_failed")

    def setwinsize(self, rows: int, cols: int) -> None:
        request("resize", key=self.key, rows=int(rows), cols=int(cols))

    def isalive(self) -> bool:
        return not self._eof

    def terminate(self, force: bool = True) -> None:
        if not self._eof:
            try:
                request("stop", key=self.key)
            except BrokerDown:
                pass
        self.detach()

    def detach(self) -> None:
        """스트림만 닫는다(Host 종료 시) — persist 세션은 브로커에서 계속 돈다."""
        self._eof = True
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass


def attach(key: str) -> BrokerProc:
    conn = _connect()
    r = _rpc(conn, {"op": "attach", "key": key})
    if not r.get("ok"):
        conn.close()
        raise BrokerDown(r.get("error") or "attach_failed")
    return BrokerProc(key, r.get("pid"), conn)


def spawn(key: str, sid: str, argv: list[str], cwd: str, cols: int, rows: int, persist: bool, cap: int) -> BrokerProc:
    """브로커에 스폰을 맡기고 스트림을 붙인다. 오류는 (code, message) 를 가진 SpawnRefused/BrokerDown."""
    ensure(start=True)
    r = request("spawn", key=key, sid=sid, argv=list(argv), cwd=cwd, cols=cols, rows=rows,
                persist=persist, cap=cap)
    if not r.get("ok"):
        raise SpawnRefused(r.get("error") or "spawn_failed", r.get("message") or "")
    return attach(key)


class SpawnRefused(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def sessions() -> list[dict]:
    try:
        r = request("list")
        return r.get("sessions") or []
    except BrokerDown:
        return []


def status() -> dict:
    """설정 화면용: 모드·실행 여부·세션 수·업데이트 대기(번들 코드와 해시 다름)."""
    on = enabled()
    h = hello() if on else None
    st = {"enabled": on, "running": bool(h and h.get("ok")), "bundled_hash": ptyd.code_hash()}
    if h and h.get("ok"):
        st.update(pid=h.get("pid"), proto=h.get("proto"), hash=h.get("hash"), sessions=h.get("sessions", 0),
                  update_pending=h.get("hash") != ptyd.code_hash())
    return st


def restart(force: bool = False) -> dict:
    """브로커 교체(D2). 세션이 있으면 force 일 때만 — 그 세션들은 종료된다."""
    try:
        r = request("shutdown", force=bool(force))
    except BrokerDown:
        return {"ok": True, "was_running": False}
    if not r.get("ok"):
        return r
    global _ctl
    with _lock:
        try:
            if _ctl is not None:
                _ctl.close()
        except Exception:  # noqa: BLE001
            pass
        _ctl = None
    deadline = time.time() + 5
    while time.time() < deadline and hello():
        time.sleep(0.2)
    return {"ok": True, "was_running": True}
