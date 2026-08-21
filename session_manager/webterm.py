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


def _get_live(key: str) -> _TermSession | None:
    """살아있는 세션만 돌려준다 — 시체(PTY 종료)는 그 자리에서 정리.

    dead 플래그는 reader 가 EOF 를 '읽은 뒤'에야 서므로 믿을 수 없다(winpty 는
    claude 종료 후 EOF 가 늦거나 안 오는 경우가 실측됨). 그 상태의 등록부 항목에
    재접속하면 '연결 종료'만 반복되는 좀비 UX 가 된다(재연결 2회→4회… 증가 사고).
    그래서 조회 시점마다 isalive() 로 실측 검증하고, 죽었으면 즉시 정리해
    호출자가 새 스폰 경로를 타게 한다.
    """
    sess = _ACTIVE.get(key)
    if sess is None:
        return None
    alive = False
    try:
        alive = (not sess.dead) and sess.proc.isalive()
    except Exception:  # noqa: BLE001
        alive = False
    if not alive:
        _cleanup(sess)
        return None
    return sess


def has_terminal(session_id: str) -> bool:
    return _get_live(session_id) is not None


def stop_terminal(session_id: str) -> bool:
    """열려 있는 터미널의 claude 프로세스를 명시 종료한다(화면 유무 무관).

    persist 세션은 화면이 떨어져도 살아 있는 게 설계라, 사용자가 끝낼 방법이
    이것뿐이다(터미널 안에서 claude 를 종료하는 것 외에). 프로세스가 죽으면
    reader 의 EOF 경로가 등록부 정리·화면 닫기를 수행한다.
    """
    sess = _get_live(session_id)
    if sess is None:
        return False            # 없거나 이미 시체(그 자리에서 정리됨)
    try:
        sess.proc.terminate(force=True)
    except Exception:  # noqa: BLE001
        _cleanup(sess)
    return True


def _cleanup(sess: _TermSession) -> None:
    sess.dead = True
    if _ACTIVE.get(sess.key) is sess:
        _ACTIVE.pop(sess.key, None)
    try:
        sess.proc.terminate(force=True)
    except Exception:  # noqa: BLE001
        pass
    _registry_remove(getattr(sess.proc, "pid", None))


# ---------------------------------------------------------------- PTY 등록부
# 우리가 띄운 PTY 의 (pid, 세션ID)를 파일에 기록한다. Host 가 강제 종료돼도
# 파일은 남으므로, 다음 기동 때 '우리가 낳았다 잃은' 프로세스를 서명 추측 없이
# 정확히 식별해 정리할 수 있다(임의의 claude 오인 정리가 원리적으로 불가능).
# 방치하면 claude 데몬이 고아를 bg 에이전트로 승격시켜 세션을 잠근다(실사고:
# "already running as a background agent" - 업데이트 재기동마다 재발 위험).

_REG_LOCK = threading.Lock()


def _registry_path():
    from session_manager import config as _cfg
    return _cfg.data_dir() / "pty-registry.json"


def _registry_load() -> list[dict]:
    try:
        import json as _json
        return _json.loads(_registry_path().read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def _registry_save(entries: list[dict]) -> None:
    try:
        import json as _json
        p = _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _registry_add(pid: int | None, session_id: str) -> None:
    if not pid:
        return
    with _REG_LOCK:
        entries = [e for e in _registry_load() if e.get("pid") != pid]
        entries.append({"pid": pid, "sid": session_id})
        _registry_save(entries)


def _registry_remove(pid: int | None) -> None:
    if not pid:
        return
    with _REG_LOCK:
        entries = [e for e in _registry_load() if e.get("pid") != pid]
        _registry_save(entries)


def shutdown_all() -> int:
    """Host 정상 종료 시 우리가 띄운 PTY 전부 종료(고아 예방의 제1방어선).

    업데이트 = 재기동이므로, persist PTY 를 살려두면 재기동마다 고아→bg 승격→
    세션 잠금이 재발한다. 재기동 후엔 어차피 등록을 잃어 재접속도 불가하므로
    유지할 가치가 없다 - 함께 내리는 게 맞다.
    """
    n = 0
    for sess in list(_ACTIVE.values()):
        try:
            _cleanup(sess)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    with _REG_LOCK:
        _registry_save([])
    return n


def pick_orphans(entries: list[dict], procs: list[dict]) -> list[int]:
    """등록부 항목 중 실제로 살아있고 명령줄에 그 세션ID가 있는 PID(순수 판정).

    PID 재사용 가드: 재부팅/시간 경과로 같은 PID 가 다른 프로세스일 수 있어,
    명령줄에 등록된 세션 ID 가 들어 있을 때만 우리 고아로 인정한다.
    """
    by_pid = {p["pid"]: p.get("cmdline", "") for p in procs}
    return [e["pid"] for e in entries
            if e.get("pid") in by_pid and e.get("sid", "") and e["sid"] in by_pid[e["pid"]]]


_LEGACY_UUID_RE = None


def is_legacy_orphan_cmdline(cmdline: str) -> bool:
    """구버전(등록부 없던 ≤0.3.24) ClewPath 가 띄운 PTY 의 고유 서명인가.

    webterm 은 항상 `--resume <UUID> --dangerously-skip-permissions` 형태로
    띄웠다(YOLO 기본). 사용자가 손으로 UUID 까지 쳐서 같은 형태를 만들고 그
    콘솔만 죽는 경우는 사실상 없고(콘솔 종료는 자식도 죽인다), bg 승격체
    (--bg-pty-host)나 웹재개 스트림(stream-json)은 별도 취급이라 제외한다.
    [원칙 협의 2026-08-21] 실사용자 세션 잠금 사고로 소급 정리 승인 -
    '부모 사망 + 이 서명' 이중 조건에서만.
    """
    global _LEGACY_UUID_RE
    import re
    if _LEGACY_UUID_RE is None:
        _LEGACY_UUID_RE = re.compile(
            r"--resume\s+[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    c = cmdline or ""
    return ("claude" in c.lower()
            and _LEGACY_UUID_RE.search(c) is not None
            and "--dangerously-skip-permissions" in c
            and "stream-json" not in c
            and "bg-pty-host" not in c)


def pick_legacy_orphans(procs: list[dict]) -> list[int]:
    """전체 프로세스 목록에서 '레거시 서명 + 부모 사망' PID(순수 판정)."""
    alive = {p["pid"] for p in procs}
    return [p["pid"] for p in procs
            if is_legacy_orphan_cmdline(p.get("cmdline", ""))
            and p.get("ppid") not in alive]


def sweep_legacy_orphans() -> int:
    """부팅 시: 구버전이 남긴 레거시 PTY 고아 소급 정리(등록부 이전 시대분)."""
    import subprocess
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
    except Exception as e:  # noqa: BLE001
        print(f"[sweep] 레거시 고아 조회 실패, 건너뜀: {e}", flush=True)
        return 0
    killed = 0
    for pid in pick_legacy_orphans(procs):
        cmd = next((p["cmdline"] for p in procs if p["pid"] == pid), "")
        print(f"[sweep] 레거시 PTY 고아 정리 pid={pid} (구버전 잔재): {cmd[:100]}", flush=True)
        import subprocess as _sp
        _sp.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        killed += 1
    if killed:
        print(f"[sweep] 레거시 고아 {killed}개 정리 완료", flush=True)
    return killed


def sweep_orphan_ptys() -> int:
    """기동 시: 이전 Host 가 등록부에 남긴 PTY 고아 정리(강제 종료 대비 제2방어선)."""
    import subprocess
    with _REG_LOCK:
        entries = _registry_load()
        if not entries:
            return 0
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like '%claude%'\" | "
                 "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
            rows = json.loads(out or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            procs = [{"pid": int(r["ProcessId"]), "cmdline": r.get("CommandLine") or ""}
                     for r in rows]
        except Exception as e:  # noqa: BLE001
            print(f"[sweep] PTY 고아 조회 실패, 건너뜀: {e}", flush=True)
            return 0
        killed = 0
        for pid in pick_orphans(entries, procs):
            print(f"[sweep] PTY 고아 정리 pid={pid} (이전 Host 의 등록부 기반)", flush=True)
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            killed += 1
        _registry_save([])          # 등록부는 이번 기동 기준으로 리셋
        if killed:
            print(f"[sweep] PTY 고아 {killed}개 정리 완료", flush=True)
        return killed


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
            # claude 자체가 끝났다(사용자 exit 등) → 진짜 정리.
            # '연결 끊김'과 구분되는 명시 문구를 먼저 보낸다(상태 모델: 세션 종료).
            client = sess.client
            _cleanup(sess)
            if client is not None:
                ws, loop = client
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_text(
                        "\r\n\x1b[33m── 세션이 종료되었습니다(claude 종료). "
                        "연결하면 새로 시작합니다 ──\x1b[0m\r\n"), loop).result(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
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
    # 살아있는 세션만 재접속 대상 - 시체는 _get_live 가 정리해 새 스폰으로 떨어진다.
    # (연결 버튼은 항상 1번이면 되어야 한다는 상태 모델 - 시체 상태를 사용자에게 노출 금지)
    sess = _get_live(key)

    if sess is not None:
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
        # 고아 방지 3중: ① Job Object(커널 동반 종료 - Host 가 어떻게 죽든)
        # ② 자기 등록부(① 실패 대비 부팅 청소) ③ 정상 종료 shutdown_all
        from session_manager import jobguard
        jobguard.guard(getattr(proc, "pid", None))
        _registry_add(getattr(proc, "pid", None), session_id)
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
                    # 쓰기 실패 = 프로세스가 죽었을 가능성 - persist 라도 좀비로
                    # 남기지 않는다(안 지우면 다음 재접속이 또 시체에 붙는다)
                    _get_live(key)
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
