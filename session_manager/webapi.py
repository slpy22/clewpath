"""외부 API용 세션 재개 브리지 — claude headless(stream-json) ↔ WebSocket.

웹 터미널(webterm)이 사람용 인터랙티브 PTY 라면, 이 모듈은 프로그램용 구조화 JSON API다.
claude 를 다음으로 띄운다:
    claude -p --resume <id>
        --input-format stream-json --output-format stream-json --verbose
        [--dangerously-skip-permissions]

이 모드에서 claude 는:
- stdin: 한 줄에 user 메시지 1개(JSON)를 받으면 그 턴을 처리(스트리밍 입력 = 멀티턴)
- stdout: 이벤트별 JSON 1줄 (system/init, assistant, result, rate_limit_event ...)

WebSocket 프로토콜(우리 API):
- 클라이언트 → 서버:
    {"type":"prompt","text":"<프롬프트>"}      # 한 턴 보내기(권장)
    {"type":"user","message":{...}}            # 원시 stream-json user 메시지 그대로 전달(고급)
- 서버 → 클라이언트:
    {"type":"ready","session_id":...,"cwd":...} # 연결됨(우리 제어 이벤트)
    <claude stdout JSON 라인 그대로 전달>         # system/assistant/result/... (type 으로 구분)
    {"type":"closed","code":<exit>}             # claude 종료
    {"type":"error","message":"..."}            # 오류

보안: 라우트에서 API 토큰(auth.valid_api_token) 검증 후 호출된다고 가정.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import uuid

from session_manager.scanner import scan_one, resolve_launch_cwd


# ---- 고아 스트림 청소 (Host 기동 시 1회) ----
# Host 가 강제 종료되면 웹재개 자식(-p 스트림)이 부모 없이 남는다. 이 프로세스는
# ClewPath 만 만드는 고유 인자 조합(-p + --resume + stream-json)이라 식별 가능하고,
# '부모 사망' 조건까지 겹치면 우리가 낳았다 잃어버린 자식이 확실하다.
# [원칙 협의됨 2026-08-19] claude 프로세스 종료지만 ClewPath 자신이 만든 것 한정 -
# 사장님 승인으로 기동 시 자동 정리한다. 임의의 claude 는 절대 건드리지 않는다.

def _our_stream_signature(cmdline: str) -> bool:
    """이 명령줄이 ClewPath 가 띄운 웹재개/일회성 스트림인가(고유 서명)."""
    c = cmdline or ""
    return ("claude" in c.lower() and " -p" in c
            and "--resume" in c and "stream-json" in c)


def find_orphan_stream_pids(procs: list[dict]) -> list[int]:
    """프로세스 목록에서 '우리 서명 + 부모 사망' 인 고아 PID 를 고른다(순수 판정).

    procs: [{"pid":int, "ppid":int, "cmdline":str}, ...] 전체 프로세스 목록.
    """
    alive = {p["pid"] for p in procs}
    return [p["pid"] for p in procs
            if _our_stream_signature(p.get("cmdline", ""))
            and p.get("ppid") not in alive]


def sweep_orphan_streams() -> int:
    """기동 시 고아 스트림 정리. 실패해도 서버 기동에 영향 없음(best-effort).

    전체 프로세스(pid/ppid)를 한 번에 받아 '부모 사망'을 존재 여부로 정확히
    판정한다(이름 휴리스틱 금지). 명령줄은 claude 류에만 실려 오면 충분.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
        rows = json.loads(out or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        procs = [{"pid": int(r["ProcessId"]), "ppid": int(r.get("ParentProcessId") or 0),
                  "cmdline": r.get("CommandLine") or ""} for r in rows]
        killed = 0
        for pid in find_orphan_stream_pids(procs):
            cmd = next((p["cmdline"] for p in procs if p["pid"] == pid), "")
            print(f"[sweep] 고아 웹재개 스트림 정리 pid={pid}: {cmd[:100]}", flush=True)
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            killed += 1
        if killed:
            print(f"[sweep] 고아 스트림 {killed}개 정리 완료", flush=True)
        return killed
    except Exception as e:  # noqa: BLE001
        print(f"[sweep] 고아 정리 건너뜀: {e}", flush=True)
        return 0


# [원칙] 한때 웹 재개 프롬프트를 claude 입력 히스토리(~/.claude/history.jsonl)에
# append 하는 기능이 있었으나(웹→터미널 ↑/↓ 이력 연속성), claude 소유 파일을
# 고지 없이 자동 수정하는 것이라 원칙 위반으로 제거했다(2026-08-13, CLAUDE.md).
# 되살리려면 사용자 사전 고지+승인(옵트인 설정) 게이트와 함께 협의부터 할 것.


def _guardrail_args(g: dict, skip_permissions: bool) -> list[str]:
    """실효 가드레일(dict)을 claude CLI 인자로 변환한다."""
    args: list[str] = []
    if g.get("allowed_tools"):
        args += ["--allowed-tools", " ".join(g["allowed_tools"])]
    if g.get("denied_tools"):
        args += ["--disallowed-tools", " ".join(g["denied_tools"])]
    if g.get("model"):
        args += ["--model", str(g["model"])]
    if g.get("system_prompt"):
        args += ["--append-system-prompt", str(g["system_prompt"])]
    if g.get("max_budget_usd") and g["max_budget_usd"] > 0:
        args += ["--max-budget-usd", str(g["max_budget_usd"])]
    # 권한 모드가 지정되면 그걸 쓰고, 아니면 비인터랙티브용 skip
    if g.get("permission_mode"):
        args += ["--permission-mode", str(g["permission_mode"])]
    elif skip_permissions:
        args.append("--dangerously-skip-permissions")
    return args


def _claude_argv(session_id: str, skip_permissions: bool = True,
                 fork_id: str | None = None,
                 guardrails: dict | None = None) -> list[str]:
    exe = shutil.which("claude") or "claude"
    argv = [exe, "-p", "--resume", session_id,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose"]
    if fork_id:
        argv += ["--fork-session", "--session-id", fork_id]
    argv += _guardrail_args(guardrails or {}, skip_permissions)
    return argv


def _user_message(text: str) -> str:
    """텍스트 프롬프트를 claude stream-json user 메시지 1줄로 만든다."""
    obj = {"type": "user",
           "message": {"role": "user",
                       "content": [{"type": "text", "text": text}]}}
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def run_resume_api(ws, session_id: str, skip_permissions: bool = True,
                         fork_id: str | None = None,
                         guardrails: dict | None = None,
                         on_finish=None) -> None:
    """WebSocket 한 개에 대해 claude 재개(stream-json) 프로세스를 띄우고 중계한다.

    fork_id 가 있으면 원본을 건드리지 않는 fork 로 재개하고, 연결 종료 시 fork 를 삭제한다.
    guardrails 는 실효 가드레일(도구·권한·모델 등).
    on_finish(total_cost_usd) 는 종료 시 누적 비용을 콜백으로 넘긴다(사용량 집계용).
    호출 측에서 ws.accept()/인증은 끝난 상태로 가정.
    """
    loop = asyncio.get_event_loop()
    cost_state = {"usd": 0.0, "turns": 0}

    meta = scan_one(session_id)
    cwd = resolve_launch_cwd(meta) if meta else None
    if not cwd or not os.path.isdir(cwd):
        await _send(ws, {"type": "error",
                         "message": f"세션 작업 폴더를 찾을 수 없습니다: {cwd or '(미상)'}"})
        await _safe_close(ws)
        return

    argv = _claude_argv(session_id, skip_permissions, fork_id, guardrails)
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
        )
    except Exception as e:  # noqa: BLE001
        await _send(ws, {"type": "error", "message": f"claude 시작 실패: {e}"})
        await _safe_close(ws)
        return

    await _send(ws, {"type": "ready", "session_id": session_id, "cwd": cwd,
                     "fork_id": fork_id, "forked": bool(fork_id),
                     "denied_tools": (guardrails or {}).get("denied_tools", [])})

    stop = threading.Event()

    def reader() -> None:
        """claude stdout(JSON 라인)을 읽어 WebSocket 으로 그대로 보낸다."""
        try:
            for raw in iter(proc.stdout.readline, b""):
                if stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.strip():
                    continue
                # 턴 종료(result) 이벤트에서 비용 누적
                try:
                    ev = json.loads(line)
                    if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
                        cost_state["usd"] += float(ev["total_cost_usd"])
                        cost_state["turns"] += 1
                except Exception:  # noqa: BLE001
                    pass
                fut = asyncio.run_coroutine_threadsafe(ws.send_text(line), loop)
                try:
                    fut.result()
                except Exception:  # noqa: BLE001
                    break
        finally:
            code = proc.poll()
            asyncio.run_coroutine_threadsafe(
                _finish(ws, code if code is not None else -1), loop)

    t = threading.Thread(target=reader, name=f"api-{session_id[:8]}", daemon=True)
    t.start()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                await _send(ws, {"type": "error", "message": "JSON 파싱 실패"})
                continue
            mtype = msg.get("type")
            line = None
            if mtype == "prompt":
                line = _user_message(str(msg.get("text", "")))
            elif mtype == "user" and isinstance(msg.get("message"), dict):
                # 원시 stream-json user 메시지 그대로 전달
                line = json.dumps(msg, ensure_ascii=False) + "\n"
            else:
                await _send(ws, {"type": "error",
                                 "message": "알 수 없는 메시지 타입(prompt 또는 user 필요)"})
                continue
            try:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                await _send(ws, {"type": "error", "message": f"입력 전송 실패: {e}"})
                break
    except Exception:  # noqa: BLE001  (WebSocketDisconnect 포함)
        pass
    finally:
        stop.set()
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        # fork 로 열었으면 종료 시 fork 세션 삭제(원본은 원래 안 건드림)
        if fork_id:
            try:
                import time
                from session_manager import lifecycle
                time.sleep(0.5)  # 프로세스 종료/파일잠금 해제 대기
                lifecycle.delete_session(fork_id, dry_run=False)
            except Exception:  # noqa: BLE001
                pass
        # 누적 비용을 사용량 집계에 반영(멀티턴 실제 비용을 일일 한도에 잡히게)
        if on_finish:
            try:
                on_finish(cost_state["usd"])
            except Exception:  # noqa: BLE001
                pass


def ephemeral_ask(session_id: str, prompt: str, skip_permissions: bool = True,
                  timeout: int = 300, guardrails: dict | None = None) -> dict:
    """원본 세션을 fork 해서 프롬프트 1턴을 처리하고, fork 를 삭제한 뒤 결과를 반환한다.

    원본은 절대 바뀌지 않는다(--fork-session). 일회성 질의에 적합.
    guardrails 는 policy.effective_guardrails() 결과(도구·권한·모델·시스템프롬프트·비용 등).
    동기 함수(FastAPI sync 엔드포인트 → 스레드풀에서 실행됨).
    """
    g = guardrails or {}
    meta = scan_one(session_id)
    cwd = resolve_launch_cwd(meta) if meta else None
    if not cwd or not os.path.isdir(cwd):
        return {"error": f"세션 작업 폴더를 찾을 수 없습니다: {cwd or '(미상)'}",
                "session_id": session_id}

    fork_id = str(uuid.uuid4())
    exe = shutil.which("claude") or "claude"
    argv = [exe, "-p", prompt, "--resume", session_id,
            "--fork-session", "--session-id", fork_id,
            "--output-format", "stream-json", "--verbose"]
    argv += _guardrail_args(g, skip_permissions)

    answer: list[str] = []
    tool_uses: list[str] = []
    result_text = None
    cost = None
    is_error = False
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            t = o.get("type")
            if t == "assistant":
                for b in o.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        answer.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        tool_uses.append(b.get("name", "?"))
            elif t == "result":
                result_text = o.get("result")
                cost = o.get("total_cost_usd")
                is_error = bool(o.get("is_error"))
    except subprocess.TimeoutExpired:
        is_error = True
        result_text = f"시간초과({timeout}s)"
    finally:
        # fork 프로세스는 종료됐으므로 파일 잠금 없음 → 안전 삭제
        from session_manager import lifecycle
        d = lifecycle.delete_session(fork_id, dry_run=False)
        fork_deleted = bool(d.get("deleted"))

    return {
        "session_id": session_id,
        "fork_id": fork_id,
        "answer": "".join(answer) or result_text,
        "result": result_text,
        "cost_usd": cost,
        "is_error": is_error,
        "tool_uses": tool_uses,
        "fork_deleted": fork_deleted,
    }


async def _send(ws, obj: dict) -> None:
    try:
        await ws.send_text(json.dumps(obj, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


async def _finish(ws, code: int) -> None:
    await _send(ws, {"type": "closed", "code": code})
    await _safe_close(ws)


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
