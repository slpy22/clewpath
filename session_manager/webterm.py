"""웹 터미널 — claude --resume 세션을 ConPTY로 띄워 WebSocket으로 브라우저에 중계한다.

브라우저(xterm.js) ↔ WebSocket ↔ 이 모듈 ↔ ConPTY(claude --resume).

프로토콜:
- 브라우저 → 서버: JSON 텍스트
    {"type": "input",  "data": "<키 입력>"}
    {"type": "resize", "cols": <int>, "rows": <int>}
- 서버 → 브라우저: 터미널 출력 raw 텍스트 (그대로 term.write)

한 프로세스(=한 터미널)를 한 브라우저 탭이 점유한다(멀티플렉싱 안 함).
보안: 로컬(127.0.0.1) 전용 가정, 인증 없음.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading

from session_manager.scanner import scan_one, resolve_launch_cwd

# 실행 중인 PTY 레지스트리 - 원격 승인(권한 프롬프트에 키 주입)용.
# 한 세션 = 한 터미널 점유 규칙 그대로, 키는 세션 id(fork 면 fork id).
_ACTIVE: dict[str, object] = {}


def write_to(session_id: str, data: str) -> bool:
    """실행 중인 그 세션의 PTY 에 키 입력을 넣는다. 없으면 False.

    원격(폰)에서 권한 프롬프트에 응답하는 용도 - Host 가 PTY 를 소유하고
    있어서 가능한 우리 구조의 강점. 죽은 프로세스면 레지스트리에서 걷어낸다.
    """
    proc = _ACTIVE.get(session_id)
    if proc is None:
        return False
    try:
        proc.write(data)
        return True
    except Exception:  # noqa: BLE001
        _ACTIVE.pop(session_id, None)
        return False


def has_terminal(session_id: str) -> bool:
    return session_id in _ACTIVE


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


async def run_terminal(ws, session_id: str, skip_permissions: bool = True,
                       fork_id: str | None = None) -> None:
    """WebSocket 한 개에 대해 PTY 세션을 시작하고 양방향 중계한다.

    호출 측에서 ws.accept()는 이미 끝난 상태로 가정한다.
    """
    # 지연 임포트: pywinpty 미설치 환경에서도 모듈 자체는 임포트 가능하도록
    from winpty import PtyProcess

    loop = asyncio.get_event_loop()

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
    _ACTIVE[fork_id or session_id] = proc   # 원격 승인용 등록

    stop = threading.Event()

    def reader() -> None:
        """PTY 출력을 읽어 WebSocket으로 보낸다(블로킹, 별도 스레드)."""
        try:
            while not stop.is_set():
                try:
                    data = proc.read()  # blocking, str 반환
                except EOFError:
                    break
                except Exception:  # noqa: BLE001
                    break
                if not data:
                    continue
                fut = asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
                try:
                    fut.result()  # 백프레셔: 전송 완료까지 대기
                except Exception:  # noqa: BLE001
                    break
        finally:
            # 프로세스 종료(claude 종료) → 브라우저 연결도 닫는다.
            asyncio.run_coroutine_threadsafe(_safe_close(ws), loop)

    t = threading.Thread(target=reader, name=f"pty-{session_id[:8]}", daemon=True)
    t.start()

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
                    proc.write(msg.get("data", ""))
                except Exception:  # noqa: BLE001
                    break
            elif mtype == "resize":
                try:
                    cols = int(msg.get("cols", 80))
                    rows = int(msg.get("rows", 24))
                    proc.setwinsize(rows, cols)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001  (WebSocketDisconnect 포함)
        pass
    finally:
        stop.set()
        # 내가 등록한 그 프로세스일 때만 걷어낸다(새 터미널이 대체했을 수 있음)
        key = fork_id or session_id
        if _ACTIVE.get(key) is proc:
            _ACTIVE.pop(key, None)
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
