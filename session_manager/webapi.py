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
import time
import uuid

from session_manager.scanner import scan_one, resolve_launch_cwd, latest_session_id


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
    from session_manager import osdep
    alive = {p["pid"] for p in procs} - osdep.ADOPTER_PIDS     # POSIX 고아는 init(1)이 입양 — 부모 사망과 같다
    return [p["pid"] for p in procs
            if _our_stream_signature(p.get("cmdline", ""))
            and p.get("ppid") not in alive]


def sweep_orphan_streams() -> int:
    """기동 시 고아 스트림 정리. 실패해도 서버 기동에 영향 없음(best-effort).

    전체 프로세스(pid/ppid)를 한 번에 받아 '부모 사망'을 존재 여부로 정확히
    판정한다(이름 휴리스틱 금지). 명령줄은 claude 류에만 실려 오면 충분.
    """
    from session_manager import osdep
    try:
        procs = osdep.list_processes()
        killed = 0
        for pid in find_orphan_stream_pids(procs):
            cmd = next((p["cmdline"] for p in procs if p["pid"] == pid), "")
            print(f"[sweep] 고아 웹재개 스트림 정리 pid={pid}: {cmd[:100]}", flush=True)
            osdep.kill(pid)
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


def _guardrail_args(g: dict, skip_permissions: bool, resume: bool = True) -> list[str]:
    """실효 가드레일(dict)을 claude CLI 인자로 변환한다.

    resume=True(기존 세션을 이어받는 실행)에는 --max-budget-usd 를 붙이지 않는다 — claude 는 이어받은 세션의 과거 누적 비용을
    되살려 그 값과 비교하므로, 오래 쓴 세션은 API 호출도 하기 전에 error_max_budget_usd 로 엉뚱하게 실패했다(2026-10-23 실측,
    감사 기록에도 9월 실패 6건). 이어받는 실행의 호출당 상한은 실행 전에 분량으로 어림해 막는다(estimate_resume_usd).
    """
    args: list[str] = []
    if g.get("allowed_tools"):
        args += ["--allowed-tools", " ".join(g["allowed_tools"])]
    if g.get("denied_tools"):
        args += ["--disallowed-tools", " ".join(g["denied_tools"])]
    if g.get("model"):
        args += ["--model", str(g["model"])]
    if g.get("system_prompt"):
        args += ["--append-system-prompt", str(g["system_prompt"])]
    if not resume and g.get("max_budget_usd") and g["max_budget_usd"] > 0:
        args += ["--max-budget-usd", str(g["max_budget_usd"])]
    # 권한 모드가 지정되면 그걸 쓰고, 아니면 비인터랙티브용 skip
    if g.get("permission_mode"):
        args += ["--permission-mode", str(g["permission_mode"])]
    elif skip_permissions:
        args.append("--dangerously-skip-permissions")
    return args


# ---- 이어받기 비용(2026-10-23) ----
# claude 의 total_cost_usd·modelUsage 는 이어받은 세션의 과거 누적이 섞인다. 이번 실행분은 assistant 메시지마다의 usage(같은
# 메시지가 여러 줄로 오면 마지막 것)이고, 단가는 누적 modelUsage 에서 거꾸로 구한다 — 가격표를 코드에 두지 않는다.
# 가중치는 가격 비율: 입력 1 · 출력 5 · 캐시 읽기 0.1 · 캐시 쓰기 5분 1.25 · 1시간 2(실측 결과에 정확히 맞음).
RESUME_USD_PER_MB = 1.9        # 실행 전 어림: 마지막 압축 이후 분량 1MB ≈ 1.9달러(T12 실측 250KB ≈ 0.47달러)


def _cw_factor(u: dict) -> float:
    cc = u.get("cache_creation") or {}
    w5, w1 = int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)
    return 1.25 if (w5 + w1) == 0 else (1.25 * w5 + 2.0 * w1) / (w5 + w1)


def _weighted_run(u: dict) -> tuple[float, float, int]:
    """(캐시 쓰기를 뺀 가중 토큰, 캐시 쓰기 가중, 캐시 쓰기 토큰)."""
    cw = int(u.get("cache_creation_input_tokens") or 0)
    base = (int(u.get("input_tokens") or 0) + 5.0 * int(u.get("output_tokens") or 0)
            + 0.1 * int(u.get("cache_read_input_tokens") or 0))
    return base, _cw_factor(u) * cw, cw


class RunCost:
    """한 claude 실행(프로세스)의 이번 실행분 비용. feed(이벤트) 를 차례로 넣고 usd() 로 읽는다."""

    def __init__(self):
        self.msgs: dict = {}
        self.model_usage: dict | None = None
        self.total: float | None = None
        self.result_usage: dict | None = None

    def feed(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "assistant":
            m = ev.get("message") or {}
            if m.get("usage"):
                self.msgs[m.get("id") or f"_{len(self.msgs)}"] = (str(m.get("model") or ""), m["usage"])
        elif t == "result":
            if isinstance(ev.get("usage"), dict):
                self.result_usage = ev["usage"]
            if ev.get("modelUsage"):
                self.model_usage = ev["modelUsage"]
            if ev.get("total_cost_usd") is not None:
                self.total = float(ev["total_cost_usd"])

    def usd(self) -> float | None:
        if not self.msgs:
            # assistant 사용량이 하나도 없다: 결과 줄의 이번 실행분 usage 가 0 이면 API 호출 없이 끝난 것(0달러 — 보고된 total 은
            # 이어받은 과거 누적뿐), usage 칸 자체가 없으면(옛 claude) 보고값 그대로
            if self.result_usage is None:
                return self.total
            b, w, _ = _weighted_run(self.result_usage)
            return 0.0 if (b + w) == 0 else self.total
        if not self.model_usage:
            return self.total
        cost = 0.0
        by: dict = {}
        for model, u in self.msgs.values():
            b, w, cw = _weighted_run(u)
            x = by.setdefault(model, [0.0, 0.0, 0])
            x[0] += b
            x[1] += w
            x[2] += cw
        for model, (b, w, cw) in by.items():
            mu = self.model_usage.get(model)
            if not mu or not mu.get("costUSD"):
                return self.total
            f = (w / cw) if cw else 1.25                  # 누적의 캐시 쓰기 5분·1시간 비율은 이번 실행분을 따른다
            w_cum = (float(mu.get("inputTokens") or 0) + 5.0 * float(mu.get("outputTokens") or 0)
                     + 0.1 * float(mu.get("cacheReadInputTokens") or 0) + f * float(mu.get("cacheCreationInputTokens") or 0))
            if w_cum <= 0:
                return self.total
            cost += float(mu["costUSD"]) / w_cum * (b + w)
        if self.total is not None:
            cost = min(cost, self.total)
        return round(cost, 6)


def resume_tail_bytes(sid: str, limit: int, chunk: int = 256 * 1024) -> int | None:
    """그 세션 기록에서 마지막 압축 지점(compact_boundary) 이후 바이트 — 이어받을 때 실리는 분량의 어림(읽기만).
    limit 을 넘으면 None(너무 큼). 끝에서부터 limit 만큼만 거꾸로 읽는다(수백 MB 기록도 전부 읽지 않게)."""
    m = scan_one(sid)
    if not m:
        return 0
    mark = b'"compact_boundary"'
    with open(m.jsonl_path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        pos, buf = size, b""
        while pos > 0 and size - pos <= limit + chunk:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            i = buf.rfind(mark)
            if i >= 0:
                start = buf.rfind(b"\n", 0, i) + 1
                tail = size - (pos + start)
                return tail if tail <= limit else None
        return size if size <= limit else None


def estimate_resume_usd(sid: str, cap: float) -> float | None:
    """이어받기 한 번의 어림 비용(달러). cap 을 넘으면 None(실행하지 말 것)."""
    tail = resume_tail_bytes(sid, int(cap / RESUME_USD_PER_MB * 1_000_000))
    if tail is None:
        return None
    return round(max(tail, 1) / 1_000_000 * RESUME_USD_PER_MB, 3)


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
    # 세션별 모델 오버라이드(ClewPath 사이드카): 원본 재개이고 가드레일이 모델을
    # 지정하지 않았을 때만 적용(가드레일 모델이 우선). claude 파일 무수정.
    if not fork_id and "--model" not in argv:
        try:
            from session_manager import labels as _labels
            _mdl = _labels.model_of(session_id)
        except Exception:  # noqa: BLE001
            _mdl = None
        if _mdl:
            argv += ["--model", _mdl]
    return argv


def parse_input(msg: dict) -> tuple[str | None, str]:
    """웹 재개 입력 한 개 → (claude stdin 에 쓸 stream-json 줄, 사람이 친 글). 모르는 형식이면 (None, "")."""
    mtype = msg.get("type")
    if mtype == "prompt":
        text = str(msg.get("text", ""))
        return _user_message(text), text
    if mtype == "user" and isinstance(msg.get("message"), dict):
        c = msg["message"].get("content")
        text = c if isinstance(c, str) else "\n".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        return json.dumps(msg, ensure_ascii=False) + "\n", text   # 원시 stream-json user 메시지 그대로
    return None, ""


def _user_message(text: str) -> str:
    """텍스트 프롬프트를 claude stream-json user 메시지 1줄로 만든다."""
    obj = {"type": "user",
           "message": {"role": "user",
                       "content": [{"type": "text", "text": text}]}}
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def run_resume_api(ws, session_id: str, skip_permissions: bool = True,
                         fork_id: str | None = None,
                         guardrails: dict | None = None,
                         on_finish=None, human: bool = False, allow=None) -> None:
    """WebSocket 한 개에 대해 claude 재개(stream-json) 프로세스를 띄우고 중계한다.

    human=True(API 토큰 없이 들어온 resume-inplace = PWA 의 사람): 보낸 프롬프트를 팀 장부에 알려
    claude 가 turnOrigin=sdk 로 적은 그 입력을 '사장님 입력' 으로 분류하게 한다.

    fork_id 가 있으면 원본을 건드리지 않는 fork 로 재개하고, 연결 종료 시 fork 를 삭제한다.
    guardrails 는 실효 가드레일(도구·권한·모델 등).
    on_finish(usd) 는 종료 시 이 실행의 비용(RunCost — 이어받은 과거 누적 제외, 여러 턴 합)을 콜백으로 넘긴다(사용량 집계용).
    allow() 가 있으면 턴마다 물어 False 면 끊는다(서비스 정지·폐기, 서비스 대장 S2).
    호출 측에서 ws.accept()/인증은 끝난 상태로 가정.
    """
    loop = asyncio.get_event_loop()
    cost_state = {"usd": 0.0, "turns": 0}
    run_cost = RunCost()

    meta = scan_one(session_id)
    cwd = resolve_launch_cwd(meta) if meta else None
    if not cwd or not os.path.isdir(cwd):
        await _send(ws, {"type": "error",
                         "message": f"세션 작업 폴더를 찾을 수 없습니다: {cwd or '(미상)'}"})
        await _safe_close(ws)
        return

    if not fork_id:
        latest = latest_session_id(session_id)
        if latest != session_id:      # 옛 줄(continued-in) 재개 금지 — 새 줄과 갈라진다(0.9.9)
            await _send(ws, {"type": "error", "error": "continued", "continued_in": latest,
                             "message": f"이 세션은 {latest[:8]}… 로 이어졌습니다. 이어받은 세션을 여세요."})
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
    # 고아 방지: Windows 는 커널 동반 종료(Job Object) - Host 가 어떻게 죽든 스트림도 정리.
    # POSIX 는 Host 가 죽으면 stdin 파이프가 닫혀(EOF) stream-json 입력의 claude -p 가 끝난다 + 기동 청소.
    from session_manager import osdep
    osdep.guard_child(proc.pid)

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
                # 비용: 턴 종료(result)마다 이 실행분 전체를 다시 계산한다(total_cost_usd 는 프로세스·이어받은 과거 누적이라
                # 예전처럼 턴마다 더하면 몇 배로 셌다)
                try:
                    ev = json.loads(line)
                    run_cost.feed(ev)
                    if ev.get("type") == "result":
                        cost_state["usd"] = run_cost.usd() or 0.0
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
            line, text_in = parse_input(msg)
            if allow is not None and not allow():
                await _send(ws, {"type": "error", "error": "service_inactive",
                                 "message": "서비스가 정지되었거나 폐기되었습니다."})
                break
            if line is None:
                await _send(ws, {"type": "error",
                                 "message": "알 수 없는 메시지 타입(prompt 또는 user 필요)"})
                continue
            if human and text_in:                      # 사람이 친 말 — 팀 장부가 '사장님 입력' 으로 알아보게
                try:
                    from session_manager import teamlog
                    teamlog.note_web_prompt(session_id, text_in)
                except Exception:  # noqa: BLE001
                    pass
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
        # fork 로 열었으면 종료 시 fork 세션 삭제(원본은 원래 안 건드림). 프로세스가 진짜 끝난 뒤에만 —
        # 살아 있으면(0.9.9 삭제 보호) 삭제가 보류되고 목록에 남는다(백그라운드 에이전트로 승격된 포크 등).
        if fork_id:
            try:
                from session_manager import lifecycle
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill(); proc.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        pass
                lifecycle.delete_fork(fork_id, origin=session_id)  # 못 지우면 대기 목록 → 정기 작업이 다시 지움
            except Exception as e:  # noqa: BLE001
                print(f"[resume] fork {fork_id[:8]} 정리 실패: {e}")
        # 누적 비용을 사용량 집계에 반영(멀티턴 실제 비용을 일일 한도에 잡히게)
        if on_finish:
            try:
                on_finish(cost_state["usd"])
            except Exception:  # noqa: BLE001
                pass


# PC 사이 묻기(pclink) 격리 인자 — 도구 0개(--tools "")·MCP 0개(--strict-mcp-config: --tools 만으로는 MCP 도구가
# 남는다)·훅 끔·기록 안 남김. Claude Code 2.1.308·2.1.309(Windows claude.exe) 실측: init tools=[]·mcp_servers=[]·
# 훅 이벤트 없음·fork jsonl 안 생김.
_ISOLATED_ARGS = ["--no-session-persistence", "--tools", "", "--strict-mcp-config",
                  "--settings", '{"disableAllHooks":true}']


def _fork_jsonl_exists(fork_id: str) -> bool:
    """격리 묻기가 끝난 뒤 fork 기록(jsonl)이 실제로 생겼나 — 미래 claude 가 --no-session-persistence 를 무시할 때 대비."""
    from session_manager import lifecycle
    try:
        return any(p.suffix == ".jsonl" for p in lifecycle._related_paths(fork_id))
    except Exception as e:  # noqa: BLE001
        print(f"[ask] fork {fork_id[:8]} 기록 확인 실패: {e}")
        return False


def ephemeral_ask(session_id: str, prompt: str, skip_permissions: bool = True,
                  timeout: int = 300, guardrails: dict | None = None, *,
                  isolated: bool = False) -> dict:
    """원본 세션을 fork 해서 프롬프트 1턴을 처리하고, fork 를 삭제한 뒤 결과를 반환한다.

    원본은 절대 바뀌지 않는다(--fork-session). 일회성 질의에 적합.
    guardrails 는 policy.effective_guardrails() 결과(도구·권한·모델·시스템프롬프트·비용 등).
    동기 함수(FastAPI sync 엔드포인트 → 스레드풀에서 실행됨).

    isolated=True(PC 사이 묻기): 도구·MCP·훅 없는, 기록도 남기지 않는 복제본에 묻는다(_ISOLATED_ARGS).
    가드레일의 도구 허용/거부·권한 모드와 skip_permissions 는 무시하고 --dangerously-skip-permissions 를 넣지 않는다 —
    미래 claude 가 --tools "" 를 무시해도 -p 의 권한 거절이 2차 방어. 모델·시스템 프롬프트·비용 한도는 그대로.
    기록이 없으니 delete_fork 하지 않는다(그래도 jsonl 이 생겼으면 지우고 fork_persisted=True).
    tool_use 가 하나라도 나오면 isolation_breach=True — 계약 위반이라 호출자가 답을 버린다.
    """
    from session_manager import lifecycle, osdep
    g = guardrails or {}
    meta = scan_one(session_id)
    cwd = resolve_launch_cwd(meta) if meta else None
    if not cwd or not os.path.isdir(cwd):
        return {"error": f"세션 작업 폴더를 찾을 수 없습니다: {cwd or '(미상)'}",
                "session_id": session_id}

    fork_id = str(uuid.uuid4())
    exe = shutil.which("claude") or "claude"
    if isolated and prompt.startswith("-"):
        prompt = " " + prompt          # 원격 질문이 claude 옵션으로 읽히지 않게(예: "--tools=Bash")
    argv = [exe, "-p", prompt, "--resume", session_id,
            "--fork-session", "--session-id", fork_id,
            "--output-format", "stream-json", "--verbose"]
    if isolated:
        argv += _ISOLATED_ARGS
        argv += _guardrail_args({k: g.get(k) for k in ("model", "system_prompt", "max_budget_usd")},
                                skip_permissions=False)
    else:
        argv += _guardrail_args(g, skip_permissions)

    answer: list[str] = []
    tool_uses: list[str] = []
    result_text = None
    run_cost = RunCost()
    is_error = False
    proc = None
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=osdep.NO_WINDOW)
        # 고아 방지: Windows 는 Job Object 편입 — Host 가 어떻게 죽든 이 claude 도 함께 끝난다.
        osdep.guard_child(proc.pid)
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            is_error = True
            result_text = f"시간초과({timeout}s)"
            out = b""
            try:
                proc.kill()
                proc.communicate(timeout=5)          # 파이프를 비우며 거둔다
            except Exception:  # noqa: BLE001
                try:
                    proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        for line in out.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            t = o.get("type")
            run_cost.feed(o)
            if t == "assistant":
                for b in o.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        answer.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        tool_uses.append(b.get("name", "?"))
            elif t == "result":
                result_text = o.get("result")
                is_error = bool(o.get("is_error"))
    finally:
        duration_s = round(time.monotonic() - t0, 2)
        # 프로세스가 끝난 뒤에만 정리(예상 밖 예외로 살아 있으면 kill 뒤 대기). 그래도 살아 있으면 보류(0.9.9 보호).
        try:
            if proc is not None and proc.poll() is None:
                proc.kill(); proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        fork_deleted = False
        fork_persisted = False
        if not isolated:
            d = lifecycle.delete_fork(fork_id, origin=session_id)
            fork_deleted = bool(d.get("deleted"))
        elif _fork_jsonl_exists(fork_id):             # 기록 안 남김 계약 위반 — 남은 것만 치운다
            fork_persisted = True
            d = lifecycle.delete_fork(fork_id, origin=session_id)
            fork_deleted = bool(d.get("deleted"))

    r = {
        "session_id": session_id,
        "fork_id": fork_id,
        "answer": "".join(answer) or result_text,
        "result": result_text,
        "cost_usd": run_cost.usd(),           # 이번 실행분(이어받은 과거 누적 제외)
        "is_error": is_error,
        "tool_uses": tool_uses,
        "fork_deleted": fork_deleted,
        "duration_s": duration_s,
    }
    if fork_persisted:
        r["fork_persisted"] = True
    if isolated and tool_uses:
        r["isolation_breach"] = True
    return r


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
