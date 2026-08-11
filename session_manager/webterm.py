"""웹 터미널 - claude --resume 세션을 ConPTY로 띄워 WebSocket으로 중계한다.

브라우저(xterm.js) ↔ WebSocket ↔ 이 모듈 ↔ ConPTY(claude --resume).

핵심 설계: **프로세스와 화면을 분리한다.**
화면(WebSocket)이 닫혀도 claude 는 계속 돈다 - 사용자가 알림을 눌러 다른
화면으로 갔다가, 창을 닫았다가, 폰으로 옮겨 타도 세션은 살아 있다.
같은 세션을 다시 열면 새로 스폰하지 않고 **기존 프로세스에 재접속**한다.
(화면 없이 도는 동안 출력은 버퍼에 모아뒀다가 재접속 시 되감아 준다)

- 점유 규칙: 한 세션 = 화면 최대 1개. 새 화면이 붙으면 이전 화면을 밀어낸다.
- 원격 승인(write_to)은 화면이 없어도 동작한다 - 폰에서 알림만 보고
  허용을 눌러도 PTY 에 키가 들어간다.
- 예외: fork(일회용 복제) 세션은 화면이 닫히면 종료한다 - 삭제 흐름이
  프로세스 종료(파일 잠금 해제)를 전제하기 때문.

프로토콜(브라우저 ↔ 서버)은 기존과 동일:
    {"type": "input",  "data": ...} / {"type": "resize", "cols":, "rows":}
    서버 → 브라우저: 터미널 raw 텍스트
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import deque

from session_manager.scanner import scan_one, resolve_launch_cwd

# 화면 없이 도는 동안 모아두는 출력 상한(문자). 재접속 시 마지막 부분을 되감는다.
_BUF_CAP = 200_000
_REPLAY_TAIL = 60_000


class _TermSession:
    """살아있는 PTY 하나. 화면(ws)은 붙었다 떨어졌다 한다."""

    def __init__(self, key: str, proc, persist: bool):
        self.key = key
        self.proc = proc
        self.persist = persist          # False(fork)면 화면이 떨어질 때 종료
        self.chunks: deque[str] = deque()
        self.buf_len = 0
        self.client = None              # (ws, loop) | None
        self.lock = threading.Lock()
        self.dead = False

    def feed(self, data: str) -> None:
        self.chunks.append(data)
        self.buf_len += len(data)
        while self.buf_len > _BUF_CAP and self.chunks:
            self.buf_len -= len(self.chunks.popleft())

    def tail(self) -> str:
        out = "".join(self.chunks)
        return out[-_REPLAY_TAIL:]


_ACTIVE: dict[str, _TermSession] = {}


# ---------------------------------------------------------------- 외부 API

def write_to(session_id: str, data: str) -> bool:
    """그 세션 PTY 에 키 입력을 넣는다(화면 유무 무관 - 원격 승인용)."""
    sess = _ACTIVE.get(session_id)
    if sess is None or sess.dead:
        return False
    try:
        sess.proc.write(data)
        return True
    except Exception:  # noqa: BLE001
        _cleanup(sess)
        return False


def has_terminal(session_id: str) -> bool:
    sess = _ACTIVE.get(session_id)
    return sess is not None and not sess.dead


def _cleanup(sess: _TermSession) -> None:
    sess.dead = True
    if _ACTIVE.get(sess.key) is sess:
        _ACTIVE.pop(sess.key, None)
    try:
        sess.proc.terminate(force=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 내부

def _claude_argv(session_id: str, skip_permissions: bool = True,
                 fork_id: str | None = None) -> list[str]:
    """claude --resume 실행 argv(리스트). PATH에서 claude 실행파일을 찾는다.

    winpty.spawn 은 따옴표 친 문자열 경로를 인식하지 못하므로 반드시 리스트로 넘긴다.
    fork_id 가 있으면 --fork-session --session-id <fork_id> 로 원본을 건드리지 않는
    새 세션(fork)을 만든다. skip_permissions=True 이면 권한확인을 생략한다.
    """
    from session_manager import config as _cfg
    exe = _cfg.claude_exe()
    argv = [exe, "--resume", session_id]
    if fork_id:
        argv += ["--fork-session", "--session-id", fork_id]
    if skip_permissions:
        argv.append("--dangerously-skip-permissions")
    return argv


def _start_reader(sess: _TermSession) -> None:
    """PTY 출력을 읽는 단일 스레드(세션 수명 동안 1개).

    화면이 붙어 있으면 전달하고, 없으면 버퍼에만 쌓는다. 읽기를 멈추면
    ConPTY 파이프가 차서 claude 가 블로킹되므로 화면 유무와 무관하게 계속 읽는다.
    """

    def reader() -> None:
        try:
            while not sess.dead:
                try:
                    data = sess.proc.read()   # blocking, str
                except EOFError:
                    break
                except Exception:  # noqa: BLE001
                    break
                if not data:
                    continue
                with sess.lock:
                    sess.feed(data)
                    client = sess.client
                if client is not None:
                    ws, loop = client
                    fut = asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
                    try:
                        fut.result()          # 백프레셔: 전송 완료까지 대기
                    except Exception:  # noqa: BLE001
                        with sess.lock:       # 화면이 죽었어도 프로세스는 유지
                            if sess.client is client:
                                sess.client = None
        finally:
            # claude 자체가 끝났다(사용자 exit 등) → 진짜 정리
            client = sess.client
            _cleanup(sess)
            if client is not None:
                ws, loop = client
                asyncio.run_coroutine_threadsafe(_safe_close(ws), loop)

    threading.Thread(target=reader, name=f"pty-{sess.key[:8]}", daemon=True).start()


async def run_terminal(ws, session_id: str, skip_permissions: bool = True,
                       fork_id: str | None = None) -> None:
    """WebSocket 한 개를 세션 PTY 에 붙인다(없으면 스폰, 있으면 재접속).

    호출 측에서 ws.accept()는 이미 끝난 상태로 가정한다.
    """
    from winpty import PtyProcess

    loop = asyncio.get_event_loop()
    key = fork_id or session_id
    sess = _ACTIVE.get(key)

    if sess is not None and not sess.dead:
        # ---- 재접속: 스폰하지 않고 기존 프로세스에 붙는다 ----
        with sess.lock:
            old = sess.client
            sess.client = (ws, loop)
        if old is not None:              # 점유 규칙: 이전 화면은 밀어낸다
            await _safe_close(old[0])
        replay = sess.tail()
        if replay:
            try:
                await ws.send_text(replay)
                await ws.send_text("\r\n\x1b[90m[ClewPath] 실행 중인 세션에 다시 연결했습니다\x1b[0m\r\n")
            except Exception:  # noqa: BLE001
                pass
        rejoined = True
    else:
        # ---- 새 스폰 ----
        meta = scan_one(session_id)
        cwd = resolve_launch_cwd(meta) if meta else None
        if not cwd or not os.path.isdir(cwd):
            await ws.send_text(
                f"\r\n\x1b[31m[오류] 세션 작업 폴더를 찾을 수 없습니다: {cwd or '(미상)'}\x1b[0m\r\n"
                f"이 세션은 다른 PC에서 만들어졌거나 폴더가 이동/삭제된 것 같습니다.\r\n"
            )
            await _safe_close(ws)
            return
        argv = _claude_argv(session_id, skip_permissions, fork_id)
        try:
            proc = PtyProcess.spawn(argv, cwd=cwd, dimensions=(24, 80))
        except Exception as e:  # noqa: BLE001
            await ws.send_text(f"\r\n\x1b[31m[오류] 터미널 시작 실패: {e}\x1b[0m\r\n")
            await _safe_close(ws)
            return
        sess = _TermSession(key, proc, persist=not fork_id)
        sess.client = (ws, loop)
        _ACTIVE[key] = sess
        _start_reader(sess)
        rejoined = False

    me = (ws, loop)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            mtype = msg.get("type")
            if mtype == "input":
                try:
                    sess.proc.write(msg.get("data", ""))
                except Exception:  # noqa: BLE001
                    break
            elif mtype == "resize":
                try:
                    cols = int(msg.get("cols", 80))
                    rows = int(msg.get("rows", 24))
                    if rejoined:
                        # 재접속 직후엔 크기가 같으면 TUI 가 다시 안 그린다 -
                        # 한 칸 틀었다 되돌려 강제로 전체 리페인트를 유발한다.
                        rejoined = False
                        sess.proc.setwinsize(rows, max(2, cols - 1))
                    sess.proc.setwinsize(rows, cols)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001  (WebSocketDisconnect 포함)
        pass
    finally:
        # 화면만 떨어졌다. persist 세션은 프로세스를 살려둔다(재접속 가능).
        with sess.lock:
            if sess.client is not None and sess.client[0] is me[0]:
                sess.client = None
        if not sess.persist:
            _cleanup(sess)               # fork 는 일회용 - 종료(삭제 흐름 전제)


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
