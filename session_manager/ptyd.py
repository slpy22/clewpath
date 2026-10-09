"""터미널 관리 프로세스(PTY 브로커) — Host 재기동·업데이트에도 ClewPath 터미널이 살아 있게.

설계 정본: docs/designs/pty-broker.md (2026-10-08 사장님 승인 D1~D4, S1 스파이크 2026-10-09).

    python -m session_manager.ptyd          ← Host 가 필요할 때 분리 기동(DETACHED, Host 잡에 넣지 않음)

- 이 프로세스가 ConPTY 와 그 안의 `claude --resume` 을 **소유**한다. 자식은 자기 Job(KILL_ON_JOB_CLOSE)에 넣어
  브로커가 어떻게 죽든 커널이 함께 끝낸다 → 주인 없는 claude(8/21 형 고아) 0.
- Host 와는 사용자 로컬 named pipe(`multiprocessing.connection`, authkey = 데이터 폴더의 비밀 파일, HMAC 챌린지)로만
  말한다. TCP 없음. 원격 접근은 지금처럼 Host 인증·2FA 를 거친다.
- 세션마다 출력 링 버퍼를 두고, Host 의 스트림 연결(세션당 1개)이 붙어 있으면 그대로 흘려보낸다(백프레셔: 전송이
  막히면 PTY 읽기도 늦춘다 — 지금 Host 내장 PTY 와 같은 동작). Host 가 떨어지면 버퍼에만 쌓고, 다시 붙으면 꼬리부터.
- 일회성(fork, persist=False) 세션은 스트림이 끊기는 순간 종료(D4) — Host 가 죽어도 화면 없는 fork 가 남지 않는다.

이 파일은 **Host 업데이트와 독립적으로 오래 산다**(코드는 기동 때 한 번 읽힘). 바꾸면 해시가 달라져 Host 가
'브로커 업데이트 대기' 로 표시하고, 세션이 0개일 때 자동 교체(D2). 그래서 표준 라이브러리 + pywinpty + jobguard 만 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from pathlib import Path

PROTO = 1
BUF_CAP = 200_000          # 세션당 보관 출력(문자) — webterm._BUF_CAP 과 같게
REPLAY_TAIL = 60_000       # Host 가 다시 붙을 때 먼저 보내는 꼬리
EXIT_SENTINEL = b"\x00\x00clewpath-ptyd:exit"   # 세션 종료로 스트림을 닫을 때 마지막 프레임(브로커 사망과 구분)

# claude 세션 안에서 띄운 프로세스가 물려받는 표식들 — 그대로 넘기면 하위 claude 가 "child session" 으로 떠
# 기록 저장·피어 등록이 꺼진다(S1 실측: "Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION").
_SCRUB_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"}


def clean_env(env: dict | None = None) -> dict:
    src = dict(os.environ if env is None else env)
    return {k: v for k, v in src.items()
            if k not in _SCRUB_EXACT and not k.upper().startswith("CLAUDE_CODE_")}


def _data_dir() -> Path:
    from session_manager import config
    return config.data_dir()


def pipe_address(data_dir: Path | None = None) -> str:
    d = str(data_dir or _data_dir()).lower()
    return r"\\.\pipe\clewpath-ptyd-" + hashlib.sha1(d.encode("utf-8")).hexdigest()[:12]


def key_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "ptyd.key"


def state_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "ptyd.json"


def authkey(data_dir: Path | None = None, create: bool = True) -> bytes | None:
    p = key_path(data_dir)
    try:
        return bytes.fromhex(p.read_text(encoding="ascii").strip())
    except Exception:  # noqa: BLE001
        if not create:
            return None
    p.parent.mkdir(parents=True, exist_ok=True)
    k = secrets.token_bytes(32)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(k.hex(), encoding="ascii")
    os.replace(tmp, p)
    return k


def code_hash() -> str:
    """이 파일 내용의 해시 — Host 의 번들 ptyd.py 와 다르면 '브로커 업데이트 대기'."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return "?"


def _pty_spawn(argv, cwd, cols, rows, env):
    """ConPTY 스폰(테스트에서 가짜로 바꾼다)."""
    from winpty import PtyProcess
    return PtyProcess.spawn(argv, cwd=cwd, dimensions=(rows, cols), env=env)


def _guard(pid) -> None:
    from session_manager import jobguard
    jobguard.guard(pid)


class _Sess:
    def __init__(self, key: str, sid: str, proc, persist: bool):
        self.key, self.sid, self.proc, self.persist = key, sid, proc, persist
        self.pid = getattr(proc, "pid", None)
        self.chunks: deque[str] = deque()
        self.buf_len = 0
        self.stream = None            # Host 스트림 연결(세션당 1개)
        self.lock = threading.Lock()
        self.dead = False
        self.started = time.time()

    def feed(self, data: str) -> None:
        self.chunks.append(data)
        self.buf_len += len(data)
        while self.buf_len > BUF_CAP and self.chunks:
            self.buf_len -= len(self.chunks.popleft())

    def tail(self) -> str:
        return "".join(self.chunks)[-REPLAY_TAIL:]

    def info(self) -> dict:
        return {"key": self.key, "sid": self.sid, "pid": self.pid, "persist": self.persist,
                "attached": self.stream is not None, "started": self.started}


class Broker:
    def __init__(self, address: str, key: bytes):
        self.address = address
        self.key = key
        self.sessions: dict[str, _Sess] = {}
        self.lock = threading.Lock()
        self.listener = None
        self.stopping = False
        self.exit_process = True       # shutdown 요청 때 프로세스까지 끝낼지(테스트는 스레드로 돌려 False)

    # ---------------------------------------------------------------- 수명
    def serve_forever(self) -> None:
        from multiprocessing.connection import Listener
        self.listener = Listener(self.address, family="AF_PIPE", authkey=self.key)
        while not self.stopping:
            try:
                conn = self.listener.accept()
            except Exception:  # noqa: BLE001  인증 실패·끊긴 접속은 그 연결만 버린다
                if self.stopping:
                    break
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def shutdown(self) -> None:
        self.stopping = True
        for s in list(self.sessions.values()):
            self._kill(s)
        try:
            if self.listener is not None:
                self.listener.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 세션
    def _alive(self, s: _Sess) -> bool:
        try:
            return (not s.dead) and s.proc.isalive()
        except Exception:  # noqa: BLE001
            return False

    def _live(self) -> list[_Sess]:
        out = []
        for s in list(self.sessions.values()):
            if self._alive(s):
                out.append(s)
            else:
                self._finish(s)
        return out

    def _kill(self, s: _Sess) -> None:
        try:
            s.proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        self._finish(s)

    def _finish(self, s: _Sess) -> None:
        """세션 종료 처리(한 번만): 목록에서 빼고, 붙은 스트림에 종료 표식 후 닫는다."""
        with s.lock:
            if s.dead and s.key not in self.sessions:
                return
            s.dead = True
            st, s.stream = s.stream, None
        with self.lock:
            if self.sessions.get(s.key) is s:
                self.sessions.pop(s.key, None)
        if st is not None:
            try:
                st.send_bytes(EXIT_SENTINEL)
            except Exception:  # noqa: BLE001
                pass
            try:
                st.close()
            except Exception:  # noqa: BLE001
                pass

    def _reader(self, s: _Sess) -> None:
        try:
            while not s.dead:
                try:
                    data = s.proc.read()
                except Exception:  # noqa: BLE001  EOFError 포함
                    break
                if not data:
                    continue
                with s.lock:
                    s.feed(data)
                    st = s.stream
                if st is not None:
                    try:
                        st.send_bytes(data.encode("utf-8", "replace"))   # 막히면 기다린다(백프레셔)
                    except Exception:  # noqa: BLE001  Host 가 떨어졌다 — 프로세스는 유지
                        self._detach(s, st)
        finally:
            self._finish(s)

    def _detach(self, s: _Sess, st) -> None:
        with s.lock:
            if s.stream is not st:
                return
            s.stream = None
        try:
            st.close()
        except Exception:  # noqa: BLE001
            pass
        if not s.persist:            # D4: 일회성(fork)은 화면 주인이 떠나면 끝
            self._kill(s)

    def _detach_unbound(self, s: _Sess, conn) -> None:
        """스트림으로 묶이기 전에 끊긴 연결 — 닫고, 일회성이면 종료(D4)."""
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        if not s.persist and s.stream is None:
            self._kill(s)

    def spawn(self, key, sid, argv, cwd, cols=80, rows=24, persist=True, cap=12) -> dict:
        with self.lock:
            cur = self.sessions.get(key)
        if cur is not None and self._alive(cur):
            return {"ok": False, "error": "dup", "pid": cur.pid}
        if len(self._live()) >= int(cap or 12):
            return {"ok": False, "error": "cap"}
        try:
            proc = _pty_spawn(list(argv), cwd, int(cols), int(rows), clean_env())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "spawn_failed", "message": str(e)}
        _guard(getattr(proc, "pid", None))
        s = _Sess(key, sid, proc, bool(persist))
        with self.lock:
            self.sessions[key] = s
        threading.Thread(target=self._reader, args=(s,), name=f"ptyd-{key[:8]}", daemon=True).start()
        return {"ok": True, "pid": s.pid}

    # ---------------------------------------------------------------- 연결 처리
    def _handle(self, conn) -> None:
        try:
            while True:
                try:
                    req = json.loads(conn.recv_bytes().decode("utf-8"))
                except Exception:  # noqa: BLE001  EOF·깨진 요청 → 연결 종료
                    break
                op = req.get("op")
                if op == "attach":
                    self._attach(conn, req)
                    return                      # 이 연결은 스트림 전용이 됐다
                try:
                    resp = self._op(op, req)
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": "internal", "message": f"{type(e).__name__}: {e}"}
                conn.send_bytes(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
                if op == "shutdown" and resp.get("ok"):
                    threading.Thread(target=self._exit_soon, daemon=True).start()
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _exit_soon(self) -> None:
        time.sleep(0.2)
        self.shutdown()
        if self.exit_process:
            os._exit(0)

    def _op(self, op, req) -> dict:
        if op == "hello":
            return {"ok": True, "proto": PROTO, "hash": code_hash(), "pid": os.getpid(),
                    "sessions": len(self._live())}
        if op == "list":
            return {"ok": True, "sessions": [s.info() for s in self._live()]}
        if op == "spawn":
            return self.spawn(req["key"], req.get("sid") or req["key"], req["argv"], req.get("cwd"),
                              req.get("cols", 80), req.get("rows", 24), req.get("persist", True), req.get("cap", 12))
        s = self.sessions.get(str(req.get("key") or ""))
        if op == "shutdown":
            if self._live() and not req.get("force"):
                return {"ok": False, "error": "has_sessions", "sessions": len(self._live())}
            return {"ok": True}
        if s is None or not self._alive(s):
            return {"ok": False, "error": "no_session"}
        if op == "write":
            s.proc.write(req.get("data", ""))
            return {"ok": True}
        if op == "resize":
            s.proc.setwinsize(int(req.get("rows", 24)), int(req.get("cols", 80)))
            return {"ok": True}
        if op == "stop":
            self._kill(s)
            return {"ok": True}
        return {"ok": False, "error": "bad_op"}

    def _attach(self, conn, req) -> None:
        s = self.sessions.get(str(req.get("key") or ""))
        if s is None or not self._alive(s):
            try:
                conn.send_bytes(json.dumps({"ok": False, "error": "no_session"}).encode("utf-8"))
            finally:
                conn.close()
            return
        with s.lock:
            old, s.stream = s.stream, None
            tail = s.tail()
        if old is not None:                      # 스트림은 세션당 1개 — 새 Host 연결이 이긴다
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.send_bytes(json.dumps({"ok": True, "pid": s.pid, "persist": s.persist}).encode("utf-8"))
            with s.lock:                         # 꼬리 먼저, 그다음부터 실시간(reader 가 s.stream 으로 보냄)
                if tail:
                    conn.send_bytes(tail.encode("utf-8", "replace"))
                s.stream = conn
        except Exception:  # noqa: BLE001  붙는 도중 Host 가 떠났다
            self._detach_unbound(s, conn)
            return
        # 연결이 끊길 때까지 기다린다(Host 는 이 연결로 보내지 않는다 — EOF 감지용)
        try:
            while True:
                conn.recv_bytes()
        except Exception:  # noqa: BLE001
            pass
        self._detach(s, conn)


def _write_state(address: str) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "address": address, "proto": PROTO, "hash": code_hash(),
                               "started": int(time.time())}), encoding="utf-8")
    os.replace(tmp, p)


def main() -> int:
    address = pipe_address()
    key = authkey()
    b = Broker(address, key)
    try:
        from multiprocessing.connection import Client
        c = Client(address, family="AF_PIPE", authkey=key)   # 이미 떠 있으면 두 번째는 조용히 끝
        c.close()
        return 0
    except Exception:  # noqa: BLE001
        pass
    _write_state(address)
    print(f"[ptyd] 시작 pid={os.getpid()} proto={PROTO} hash={code_hash()}", flush=True)
    b.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
