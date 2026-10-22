"""보낸 세션 확인 — 로컬 API 를 부른 연결이 어느 Claude 세션의 것인가(문서 5절 bad_sender, 기획 4.5 T4).

    연결(클라이언트 포트) → 그 소켓을 가진 프로세스 → 조상 중 claude 레지스트리(~/.claude/sessions/<pid>.json, 읽기만)에 있는 것
    → 그 항목의 sessionId. 같은 세션 id 를 두 항목이 주장하면 거부. 확인하지 못하면 거부(fail-closed).

막지 못하는 것: 같은 사용자 권한의 악성 프로그램, 레지스트리 파일을 고치는 세션 — 그 권한이면 짝 키도 읽는다.
세션별 허용은 인증이 아니라 노출 줄이기다.
"""
from __future__ import annotations

_LOOP = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
MAX_DEPTH = 12


class BadSender(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _owner_pid(client_port: int, server_port: int) -> int | None:
    import psutil
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception as e:  # noqa: BLE001  (맥은 관리자 권한이 필요 — 맥 경로는 출시 뒤, D15)
        raise BadSender(f"net_connections:{type(e).__name__}") from None
    for c in conns:
        la, ra = c.laddr, c.raddr
        if not la or not ra or not c.pid:
            continue
        if la.port == client_port and ra.port == server_port and la.ip in _LOOP and ra.ip in _LOOP:
            return int(c.pid)
    return None


def _chain(pid: int) -> list[tuple[int, dict]]:
    """그 프로세스와 조상들 [(pid, {name, create_time})]."""
    import psutil
    out = []
    try:
        p = psutil.Process(pid)
        procs = [p] + p.parents()
    except Exception:  # noqa: BLE001
        return out
    for q in procs[:MAX_DEPTH]:
        try:
            out.append((q.pid, {"name": q.name(), "create_time": q.create_time()}))
        except Exception:  # noqa: BLE001
            continue
    return out


def session_of_pid(pid: int, *, registry=None, chain=None) -> str:
    """pid(와 조상) → 세션 id. registry·chain 은 시험용 주입."""
    from session_manager import peers
    reg = registry if registry is not None else peers.snapshot(max_age=0)
    procs = chain(pid) if chain else _chain(pid)
    if not procs:
        raise BadSender("no_process")
    by_pid: dict[int, list[dict]] = {}
    for e in reg:
        try:
            by_pid.setdefault(int(e.get("pid")), []).append(e)
        except (TypeError, ValueError):
            continue
    for ppid, info in procs:
        ents = by_pid.get(ppid)
        if not ents:
            continue
        if len(ents) > 1:
            raise BadSender("ambiguous_pid")
        e = ents[0]
        if peers.same_process(e, info) is False:     # pid 재사용 — 그 항목은 다른(죽은) 프로세스의 것
            continue
        sid = e["session_id"]
        if sum(1 for x in reg if x.get("session_id") == sid) > 1:
            raise BadSender("duplicate_claim")
        return sid
    raise BadSender("no_claude_ancestor")


def caller_session(client_host: str | None, client_port: int | None, server_port: int | None) -> str:
    if client_host not in _LOOP or not client_port or not server_port:
        raise BadSender("not_local")
    pid = _owner_pid(int(client_port), int(server_port))
    if not pid:
        raise BadSender("no_socket_owner")
    return session_of_pid(pid)
