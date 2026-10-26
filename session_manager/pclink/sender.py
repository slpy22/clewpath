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


def _my_identity() -> tuple[str, object]:
    """이 Host 를 돌리는 사용자 — POSIX 는 uid, Windows 는 계정 이름."""
    import os
    if hasattr(os, "getuid"):
        return ("uid", os.getuid())
    import psutil
    return ("user", psutil.Process().username())


def _is_mine(info: dict, me: tuple[str, object]) -> bool:
    """process_iter/as_dict 가 준 정보가 같은 사용자 프로세스인가. 모르면 아니다(fail-closed).

    POSIX 는 실제·유효 uid 가 둘 다 같아야 한다 — setuid 로 다른 사용자가 된 프로세스(예: sudo 아래 curl)는 남이다.
    """
    kind, v = me
    if kind == "uid":
        u = info.get("uids")
        return bool(u) and u.real == v and u.effective == v
    return bool(info.get("username")) and info.get("username") == v


def _matches(c, client_port: int, server_port: int) -> bool:
    la, ra = c.laddr, c.raddr
    return (bool(la and ra) and la.port == client_port and ra.port == server_port
            and la.ip in _LOOP and ra.ip in _LOOP)


def _one(pids: set[int]) -> int | None:
    """연결 주인은 하나여야 한다. 둘 이상(fork 로 물려받은 소켓 등)이면 누가 보냈는지 모르니 거부 — duplicate_claim 과 같은 규칙."""
    if len(pids) > 1:
        raise BadSender("ambiguous_socket_owner")
    return next(iter(pids), None)


def _owner_pid(client_port: int, server_port: int) -> int | None:
    """그 루프백 연결(클라이언트 쪽 소켓)을 가진 프로세스 pid. 같은 사용자 프로세스만 인정한다.

    1) 시스템 전체 연결 목록(Windows·리눅스). 2) 그게 권한 없음·지원 안 됨이면(맥은 관리자 권한이 필요 — D15)
    같은 사용자 프로세스마다 연결을 본다(_owner_pid_same_user).
    """
    import psutil
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, PermissionError, NotImplementedError):
        return _owner_pid_same_user(client_port, server_port)
    except Exception as e:  # noqa: BLE001
        raise BadSender(f"net_connections:{type(e).__name__}") from None
    pid = _one({int(c.pid) for c in conns if c.pid and _matches(c, client_port, server_port)})
    if pid is None:
        return None
    me = _my_identity()
    try:     # 시스템 전체 목록은 남의 연결도 보이므로 주인이 같은 사용자인지 따로 확인(이유는 _owner_pid_same_user)
        info = psutil.Process(pid).as_dict(attrs=["uids"] if me[0] == "uid" else ["username"])
    except psutil.Error:
        raise BadSender("no_process") from None
    if not _is_mine(info, me):
        raise BadSender("other_user")
    return pid


OWNER_SCAN_TIMEOUT = 1.0   # 초. 맥 실측 약 450개 프로세스 31~34ms — 넘으면 무언가 이상한 것이니 거부


def _owner_pid_same_user(client_port: int, server_port: int) -> int | None:
    """같은 사용자 프로세스마다 Process.net_connections 를 보고 그 연결의 주인을 찾는다(관리자 권한 없이 됨).

    보안 — 다른 사용자 프로세스는 연결을 읽지도 않는다(같은 uid 만):
      - 보낸 쪽은 이 사용자의 claude 세션(~/.claude/sessions, 이 사용자 레지스트리)이어야 하므로, 남의 프로세스가
        가진 연결은 어차피 정당한 보낸 쪽이 아니다. 걸러 내고 못 찾으면 no_socket_owner 로 거부된다(fail-closed).
      - Host 가 혹시 높은 권한으로 돌더라도 남의 프로세스 정보를 들여다보지 않는다(최소 권한, 맥 개인정보 창도 피함).
      - 남의 프로세스를 '주인'으로 받아들이면 다른 사용자가 이 사용자의 세션 이름으로 묻기를 보내는 길이 된다.
    전부 훑는다 — 중복 주인을 찾아야 하므로 첫 일치에서 멈추지 않는다. 시간 상한을 넘으면 거부.
    """
    import time
    import psutil
    me = _my_identity()
    end = time.monotonic() + OWNER_SCAN_TIMEOUT
    pids: set[int] = set()
    for p in psutil.process_iter(["uids"] if me[0] == "uid" else ["username"]):
        if time.monotonic() > end:
            raise BadSender("socket_owner_timeout")
        if not _is_mine(p.info, me):
            continue
        get = getattr(p, "net_connections", None) or p.connections     # psutil<6 은 connections
        try:
            conns = get(kind="tcp")
        except psutil.Error:          # 그사이 끝난 프로세스·같은 사용자라도 막힌 것(맥 login 등)
            continue
        if any(_matches(c, client_port, server_port) for c in conns):
            pids.add(int(p.pid))
    if time.monotonic() > end:
        raise BadSender("socket_owner_timeout")
    return _one(pids)


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
