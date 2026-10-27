"""Connector v0 (prototype) — PC 상주 데몬. 릴레이로 아웃바운드 접속해 로컬 006을 프록시한다.

동작:
- 릴레이(RELAY_URL)로 role=agent 아웃바운드 WSS 접속(인바운드 포트 없음).
- 릴레이가 전달한 client 요청(req)을 로컬 006 API(127.0.0.1)로 프록시하고 res 로 회신.
- resume 는 로컬 /api/v1/resume/{id} WS를 대신 열어 양방향 스트리밍.
- 연결 끊기면 지수 백오프로 재접속.

로컬 006은 127.0.0.1 접속을 trust-local 로 인증 생략하므로, Connector는 별도 토큰 없이 로컬 API를 부른다.

스펙: docs/relay-protocol.md

실행:
    RELAY_URL=wss://relay.example.com/ws RELAY_ROOM=myroom RELAY_AGENT_TOKEN=... \
        python -m session_manager.connector
환경변수:
    RELAY_URL          릴레이 WS 베이스 (예: ws://127.0.0.1:8787/ws)  (필수)
    RELAY_ROOM         room 코드 (필수)
    RELAY_AGENT_TOKEN  agent 접속 토큰 (필수)
    SM_LOCAL_BASE      로컬 006 베이스 URL. 기본 http://127.0.0.1:5100
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import re
import json
import time
import os
import sys
from urllib.parse import urlencode

import httpx
import websockets
from websockets.asyncio.client import connect

from session_manager import logredact

# 릴레이 주소 쿼리에 jwt·공유 토큰이 실린다 — websockets DEBUG 로그(요청 줄)에 남지 않게(멱등)
logredact.install()

DEFAULT_LOCAL_BASE = os.environ.get(
    "SM_LOCAL_BASE", "http://127.0.0.1:5100").rstrip("/")

# 로컬 Host(local_base)로 가는 길은 프록시를 쓰지 않는다 — httpx·websockets(15+)는 http_proxy·ALL_PROXY(맥·Windows 는
# 시스템 프록시 설정까지)를 루프백에도 써서, 폰·웹의 요청이 프록시로 나가 실패하고 사람 증명 헤더·세션 내용이 프록시로 샌다.
# Host 는 사용자 env 를 그대로 물려받는다(런처 S38). 릴레이(원격)로 나가는 연결은 사용자의 프록시를 그대로 따른다.
# websockets 15 전에는 프록시 기능도 인자도 없다.
_LOCAL_WS_KW = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}


def _connect_local(uri: str):
    return connect(uri, max_size=None, **_LOCAL_WS_KW)


# 로컬 Host(uvicorn) 호출의 연결 재사용 기한(초). Host 의 uvicorn keep-alive(기본 5초 — server.main 은 따로 정하지 않는다)보다 넉넉히
# 짧아야 한다. httpx 기본값이 같은 5초라, 직전 호출 뒤 5초 언저리에 온 요청이 서버가 막 닫은 연결을 다시 써 ReadError 로 실패했다
# (검토 3회차 실측: 4.98초 3/10, 5.00초 1/10). 2초면 서버가 닫기 전에 이쪽이 먼저 버린다.
LOCAL_KEEPALIVE_S = 2.0

# 릴레이 프레임 프로토콜 버전. 서버(릴레이)가 최소 지원 버전을 강제할 때 쓴다.
# 프레임 형식이 호환 불가하게 바뀌면 올린다. (정본은 아래 두 번째 정의 — 같은 값으로 유지)
PROTOCOL_VERSION = 2


def _client_version() -> str:
    """설치된 패키지 버전(업데이트 필요 판단용). 못 읽으면 'unknown'."""
    try:
        from importlib.metadata import version
        return version("clewpath-host")
    except Exception:  # noqa: BLE001
        return "unknown"


CLIENT_VERSION = _client_version()

# 프로토콜 버전(기획서 §12-8): 앱은 스토어 심사 때문에 웹·Host 보다 늦게 갱신된다.
# auth 에서 서로 pv 를 교환해, 규약이 어긋나면 클라이언트가 업데이트 안내를 띄운다.
# 규약을 깨는 변경(프레임 형식·봉투 등)을 할 때만 올린다.
# 2 (0.11.0): 커넥터→폰 `notice`(device_removed/device_reissued), 폰→커넥터 `bye_device`, CP 상태 동기화.
#   폰은 pv≥2 인 Host 에서만 self-revoke 를 '해제 완료' 로 표시한다(설계 E-D17). 구 폰은 notice 를 무시(무해).
PROTOCOL_VERSION = 2


def _log(msg: str) -> None:
    """콘솔 인코딩에 없는 글자로 로그가 프로세스를 죽이지 않게(T7 사고 대응).

    Windows cp949 콘솔에서 print('⚠')가 UnicodeEncodeError → 접속 루프가 매번 크래시하며
    릴레이에 영구 미접속이 됐던 사고가 있었다. 로그는 기능을 깨뜨려선 안 된다.
    """
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        try:
            enc = sys.stdout.encoding or "ascii"
            print(msg.encode(enc, "replace").decode(enc, "replace"))
        except Exception:  # noqa: BLE001
            pass


def _priv_ok(params: dict) -> bool:
    """특권 동작(in-place/터미널) 자격 검증: grace 토큰 또는 OTP. 2FA 미설정이면 차단."""
    try:
        from session_manager import owner2fa
    except Exception:  # noqa: BLE001
        return False
    if not owner2fa.required():
        return True   # 2FA 강제 off → 코드 없이 허용(사용자 선택)
    return owner2fa.verify_grace(params.get("grace")) or owner2fa.verify(params.get("otp"))


# 원격(릴레이) 경유 프록시 요청임을 서버에 알리는 헤더. 서버 _is_local() 은 이 헤더가 있으면
# 루프백이라도 '로컬 아님' 으로 본다 — 커넥터가 127.0.0.1 로 호출하는 바람에 페어링된 폰이
# 로컬 전용 API(기기 등록·삭제·재발급, 2FA 설정, 스킬 설치, 업데이트 적용…)를 부를 수 있던
# 구멍(2026-09-29 발견, Host 0.9.8 핫픽스). 아래 덴리스트는 2중 방어(서버까지 안 보냄).
VIA_HEADER = "X-ClewPath-Via"
# 사람 증명(개인 비서 단계 3, A-2): 커넥터는 Host 와 같은 프로세스라 이 값은 메모리에만 있다(기동마다 새로).
# 폰(인증된 기기)이 보낸 요청에만 붙여 서버가 '사람이 폰에서 눌렀다' 를 확인한다 — 로컬 프로세스는 위조할 수 없다.
import secrets as _secrets
RELAY_PROOF = _secrets.token_hex(24)
PROOF_HEADER = "X-ClewPath-Relay-Proof"
DEVICE_HEADER = "X-ClewPath-Device"
# 그 요청이 E2EE 봉투로 왔다 — 사람 증명 헤더와 함께만 붙인다. pclink 관리의 '켜는' 동작은 폰에서 이것 + 2차 인증이 필요(D11)
E2EE_HEADER = "X-ClewPath-E2EE"
_LOCAL_ONLY_API = re.compile(
    r"^/api/(owner/(devices(/|$|\?)|services(/|$|\?)|2fa/(provision|toggle)|skills/[^/]+/install|trash/)"
    r"|sessions/[^/]+/terminal/start|v1/team(/|$|\?)|v1/pclink(/|$|\?)|services/)")
# update/apply 는 0.10.5 부터 로컬 전용이 아니라 특권(2FA) — 원격에서 Host 업데이트(사장님 2026-09-30).
# 실행되는 코드는 CP 가 서명한 패키지뿐(Host 가 서명·해시 검증)이라 원격 트리거의 위험은 '재기동' 에 국한된다.


def _is_local_only_api(path: str, verb: str) -> bool:
    """릴레이 경유로는 절대 호출될 수 없는 경로. 기기 목록(GET)도 포함 — 이름·접속 시각 누출 방지."""
    if not _LOCAL_ONLY_API.match(path or ""):
        return False
    if path.startswith(("/api/owner/devices", "/api/owner/services", "/api/services/")) or path.startswith("/api/v1/team"):
        return True                      # 모든 동사(팀 명부: eng E-2 — 폰은 세션에 요청)
    if path.startswith("/api/v1/pclink"):
        return True                      # 다른 PC 에 묻기는 이 PC 의 세션만(폰이 시키지 못하게)
    return verb == "POST"


def _otp_error() -> str:
    try:
        from session_manager import owner2fa
        return "2fa_required" if not owner2fa.enabled() else "2fa_invalid"
    except Exception:  # noqa: BLE001
        return "2fa_required"


# 제네릭 /api/* 프록시로 나가되 특권/파괴적이라 2FA 를 요구해야 하는 경로들.
# 터미널 start 는 _start_terminal 에서 2FA 를 걸지만, 종료(stop)는 제네릭 프록시로
# 새는 비대칭 갭이 있었다 — 원격에서 OTP 없이 남의 claude 프로세스를 죽일 수 있었다.
# start 와 대칭이 되도록 stop 도 특권 경로로 취급한다(릴레이 경유 한정; 소유자
# 로컬 직접 호출은 이 프록시를 안 타므로 무영향 — start 2FA 와 같은 원리).
_PRIV_API_SUFFIXES = ("/terminal/stop", "/kill", "/update/apply", "/revert-continuation", "/ptyd/restart",
                      "/file/download", "/owner/housekeeping", "/housekeeping/idle_sessions/run")
# owner/housekeeping(설정 변경, POST)·idle_sessions/run(지금 정리): 원격에서 세션을 끄게 되는 조작이라 2FA(2026-10-12)
# file/download: 📁 파일 내려받기(보기는 허용, 내려받기는 2FA — file-browser §10), kill: 세션 강제 종료, update/apply: 원격 Host 업데이트, revert-continuation: 이어하기 되돌리기 — 모두 2FA 게이트


def _is_privileged_api(path: str) -> bool:
    p = (path or "").split("?", 1)[0].rstrip("/")
    return any(p.endswith(s) for s in _PRIV_API_SUFFIXES)


# 오래 걸릴 수 있는 요청(로컬 API 프록시·목록·통계·ask·내보내기/가져오기·2FA 확인)은 릴레이 프레임을 받는 길을 막지 않게 따로 돈다.
# 프레임을 하나씩 처리하면 Host 쪽 느린 요청 하나(업데이트 확인·큰 목록·내보내기)가 그 PC 의 모든 기기 프레임 — 다른 기기의 기기 인증,
# client_offline, 터미널 입력 — 을 그 요청이 끝날 때까지 막았고(재연결 3.6초), 늦은 응답이 뒤에 줄 선 client_offline 보다 먼저 나가
# 떠난 기기 앞 응답이 방 전체로 broadcast 됐다(검토 3회차). 기기 인증·스트림 시작·ping 은 그 자리에서 — 순서가 뜻을 가진다
# (인증 뒤의 요청은 인증된 상태를 보고, 스트림 시작 뒤의 입력은 그 스트림의 입력 큐로 간다). bye_device(폰의 자기 해제)는 그 기기의 줄 맨 뒤에
# 선다 — 그 앞에 보낸 요청이 먼저 실행된다(검토 4회차: 그 자리에서 해제하면 같은 읽기로 온 앞 요청이 인증을 잃어 실행되지 않았다).
_SPAWN_METHODS = frozenset({"list_sessions", "get_stats", "ask", "api", "export", "import", "grant", "bye_device"})
# 따로 도는 요청도 기기마다 순서를 지킨다(_spawn_req) — 순서·상한 없이 돌리자 한 기기가 연달아 보낸 쓰기 둘(팀 알림 체크 두 개)이
# Host 에서 겹쳐, 잠금 없이 같은 .tmp 이름으로 쓰는 저장소가 깨지고(관제 그룹 사라짐) 몰린 요청이 Host 의 스레드(anyio 40)를 다 써 PC 자신의
# /api/health 가 5초를 넘겼다(검토 4회차). 그래서 전체 동시 실행에도 상한을 두고, Host 상태를 바꾸는 요청(_is_write)은 기기가 달라도 한 번에
# 하나 — main 처럼 릴레이로 온 쓰기는 서로 겹치지 않는다. 다른 기기의 읽기·기기 인증·client_offline 은 여전히 기다리지 않는다.
# 같은 기기의 읽기(GET)끼리는 함께 돈다(2026-10-27) — 폰 화면은 목록·상세·통계를 한꺼번에 묻는데 하나씩이면 느린 것 하나가 나머지를 묶었다.
# Host 는 PC 로컬 화면의 동시 GET 을 원래 받는다. 쓰기는 그 기기의 앞 요청이 다 끝난 뒤, 쓰기 뒤의 읽기는 그 쓰기가 끝난 뒤.
_REQ_CONCURRENCY = 4
# 줄 서 있을 수 있는 요청 수 — 넘으면 받지 않고 바로 busy 로 답한다(받는 길은 막지 않는다). 하나씩 처리하던 때는 소켓이 역압을 걸었지만
# 따로 돌리면 큰 요청을 쏟아붓는 기기의 태스크·프레임이 끝없이 쌓인다(독립 검토 2026-10-27).
_LANE_BACKLOG = 64        # 기기 하나
_REQ_BACKLOG = 256        # 전체
# 내보내기(export)는 읽기 — Host 가 요청마다 다른 임시 파일에 zip 을 쓴다(server export_session). 쓰기로 두면 작업 폴더까지 담는 큰
# 내보내기가 다른 기기의 쓰기를 붙잡는다(쓰기는 기기가 달라도 한 번에 하나)
_WRITE_METHODS = frozenset({"import", "grant", "ask", "bye_device"})


def _is_write(method, params) -> bool:
    """Host 상태를 바꿀 수 있는 요청인가 — GET 이 아닌 api, 가져오기·2FA 유예 발급·ask·bye_device."""
    if method == "api":
        return str((params if isinstance(params, dict) else {}).get("verb") or "GET").upper() != "GET"
    return method in _WRITE_METHODS


class _Presence:
    """기기 연결 하나(cid)가 '이 커넥터 연결에서 아직 있다'는 표. 떠남(client_offline)·기기 해제(drop_device)·릴레이 재접속이 끝낸다.

    요청은 받을 때 이것을 쥔다(_Req.live) — 끝난 뒤 나오는 그 요청의 답·그 요청이 연 스트림의 출력은 보내지 않는다. 그 사이 같은 cid 로
    프레임이 다시 와서 떠남 표시(gone_cids)가 풀려도 마찬가지다: 풀린 뒤 받은 요청만 새 표를 쥔다(검토 4회차)."""
    __slots__ = ("over",)

    def __init__(self) -> None:
        self.over = False


class _Lane:
    """기기(cid) 하나의 따로 도는 요청 순서(_spawn_req): 아직 안 끝난 읽기들과 마지막 쓰기, 줄 선 요청 수(n)."""
    __slots__ = ("reads", "write", "n")

    def __init__(self) -> None:
        self.reads: set = set()
        self.write = None
        self.n = 0


class _Req:
    """받은 요청 하나에 대해 받을 때 정한 것 — 답과 그 요청이 연 스트림의 출력은 이것을 따른다(send).

    sealed: 봉투로 받았거나 그 기기가 봉투로 말하던 중이었다 → 답은 봉투로만(키가 없으면 버린다), 평문으로는 절대 나가지 않는다.
    보낼 때 정하면 그 사이 릴레이가 위조한 client_offline(enc_cids 에서 뺌)과 평문 ping(떠남 표시를 풂)만으로 봉투로 받은 요청의 답
    (대화·목록·2차 인증 유예 토큰)이 평문으로 나갔다 — E2EE 가 막아야 할 상대가 바로 릴레이다(검토 4회차).
    live: 받을 때 그 cid 의 _Presence.
    dev·admitted: 따로 도는 요청은 받을 때 기기 인증을 보고(_admit) 그 기기를 쥔다 — 줄을 서는 사이 그 기기가 떠나도(client_offline·
    bye_device) 떠나기 전에 보낸 요청은 그대로 실행되고, 사람 증명 헤더·자기 해제도 그 기기로 한다."""
    __slots__ = ("cid", "sealed", "live", "dev", "admitted")

    def __init__(self, cid, sealed: bool, live: _Presence) -> None:
        self.cid, self.sealed, self.live = cid, sealed, live
        self.dev: dict | None = None
        self.admitted = False


# 지금 처리 중인 요청(_Req). 따로 도는 요청은 자기 태스크에서, 그 자리에서 처리하는 요청은 처리하는 동안만 건다. 스트림 파이프 태스크는
# 시작한 요청의 값을 물려받는다(create_task 가 컨텍스트를 복사) — 그래서 _res·_stream_send 를 부르는 곳을 고치지 않고 send() 가 안다.
_REQ: contextvars.ContextVar = contextvars.ContextVar("connector_req", default=None)


def _to_ws(base: str) -> str:
    """http(s)://host → ws(s)://host 로 변환."""
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    return "ws://" + base[len("http://"):]


def relay_config_from_env(bound_port: int | None = None) -> dict | None:
    """통합(006 내장) 릴레이 모드 설정을 환경변수에서 읽는다.

    필수: SM_RELAY_URL + SM_RELAY_AGENT_TOKEN.
    room 은 다음 순서로 결정한다 —
      1) SM_RELAY_ROOM (수동 지정. 기존 배포·기존 페어링 유지용)
      2) 컨트롤플레인이 발급한 room (자격증명 파일 / 최초 발급)  ← 신규 설치 경로
    둘 다 없으면 None(=릴레이 OFF). 즉 SM_CP_URL 만 있으면 room 없이도 켜진다
    (신규 사용자는 room 을 알 수 없으므로 CP 가 발급해 준다).

    로컬 베이스는 SM_LOCAL_BASE 또는 http://127.0.0.1:{포트} (자기 자신). 포트는 서버가 실제로 연 포트(bound_port,
    lifespan 이 넘김)가 먼저다 — 설정 포트가 사용 중이라 옆 포트로 떴으면 SM_PORT 는 틀린 주소다.
    """
    url = os.environ.get("SM_RELAY_URL")
    token = os.environ.get("SM_RELAY_AGENT_TOKEN") or ""
    if not url:
        return None
    # 공유 agent 토큰은 이제 선택 — CP(JWT) 가 설정돼 있으면 없어도 된다.
    # (릴레이가 JWT 로 커넥터를 검증하므로. 레거시 배포는 토큰만으로도 동작)
    if not token and not os.environ.get("SM_CP_URL"):
        return None

    room = os.environ.get("SM_RELAY_ROOM") or ""
    if not room:
        # CP 가 설정돼 있으면 room 은 CP 에서 온다. 이미 발급받은 게 있으면 그것으로 시작
        # (없으면 첫 접속 시 _build_uri 가 발급받아 채택한다).
        if not os.environ.get("SM_CP_URL"):
            return None
        try:
            from session_manager import cp_client
            creds = cp_client.load_creds() or {}
            room = creds.get("room_key") or ""
        except Exception:  # noqa: BLE001
            room = ""

    port = bound_port or os.environ.get("SM_PORT", "5100")
    local_base = os.environ.get("SM_LOCAL_BASE") or f"http://127.0.0.1:{port}"
    return {"relay_url": url, "room": room, "token": token,
            "local_base": local_base}


# 실행 중인 커넥터(서버당 1개). 업데이트 적용(다른 스레드)이 bye 를 예약할 때 쓴다.
CURRENT = None


def request_drop_device(device_id: str, wait_s: float = 2.5, notice: str | None = None) -> int:
    """서버 스레드(기기 삭제/재발급 API)에서 그 기기의 릴레이 연결을 즉시 끊는다. 커넥터 없으면 0.

    notice: 끊기 **전에** 그 기기의 cid 들에 보낼 통지 종류(device_removed/device_reissued, 설계 B-5·E-D5).
    예외는 삼키되 로그로 남긴다(E-D15: 해체 실패가 조용히 0 으로 숨지 않게).
    """
    c = CURRENT
    loop = getattr(c, "loop", None) if c is not None else None
    if c is None or loop is None:
        return 0
    try:
        fut = asyncio.run_coroutine_threadsafe(c.drop_device(device_id, notice=notice), loop)
        return int(fut.result(timeout=wait_s))
    except Exception as e:  # noqa: BLE001
        _log(f"[devices] drop_device({device_id[:8]}) 실패: {type(e).__name__}: {e}")
        return 0


def request_bye(reason: str, wait_s: float = 1.5) -> bool:
    """서버 밖 스레드(업데이트 apply 등)에서 bye 를 보내고 잠깐 기다린다. 커넥터 없으면 False."""
    c = CURRENT
    loop = getattr(c, "loop", None) if c is not None else None
    if c is None or loop is None or getattr(c, "ws", None) is None:
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(c.bye(reason), loop)
        return bool(fut.result(timeout=wait_s))
    except Exception:  # noqa: BLE001
        return False


class Connector:
    def __init__(self, relay_url: str, room: str, token: str,
                 local_base: str | None = None):
        self.relay_url = relay_url
        self.room = room
        self.token = token
        self.local_base = (local_base or DEFAULT_LOCAL_BASE).rstrip("/")
        self.ws = None                       # 릴레이 연결
        # local_base 전용 — 프록시 env 를 따르지 않고(trust_env=False), 연결 재사용 기한은 Host keep-alive 보다 짧게
        self.http = httpx.AsyncClient(timeout=310, trust_env=False, limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=LOCAL_KEEPALIVE_S))
        # 키(rid): 릴레이를 거친 요청은 (cid, 기기가 붙인 id) 튜플 — 기기마다 id 를 1 부터 세어 겹치므로(0.29.0, _on_frame).
        self.streams: dict = {}              # rid -> resume·터미널·관제 파이프 태스크
        self.stream_in: dict = {}            # rid -> client 입력 큐
        self.req_cid: dict = {}              # rid -> cid (응답을 요청 기기로 라우팅)
        self.authed: dict[str, dict] = {}    # cid -> 인증된 기기{id,name}
        self.e2ee_key: bytes | None = None   # room E2EE 키(접속 시 로드)
        self.enc_cids: set = set()           # 암호화 봉투로 말한 기기들(cid) — 이 cid 의 평문 요청은 받지 않는다(_on_frame)
        self.plain_refused_logged: set = set()   # 평문 거부를 로그에 적은 cid(기기마다 한 줄)
        # 떠난 기기(cid) — 그 앞 프레임은 보내지 않는다(send). 값 = 떠날 때 봉투로 말하던 기기였나(평문 프레임으로는 풀지 않는다, _on_frame).
        # 삽입순 집합, 상한 있음
        self.gone_cids: dict = {}
        self.pc_denied_logged: dict = {}     # PC 기기 id -> 마지막 거부 로그 시각(1분에 한 줄)
        self.conn_gen = 0                    # 릴레이 연결 세대(_on_connected 마다 +1) — 이전 연결에서 받은 pclink 요청의 늦은 답은 버린다
        self._live: dict = {}                # cid -> _Presence(지금 이 연결에서 있는 기기) — 요청이 받을 때 쥔다(_Req.live)
        self._req_tasks: set = set()         # 따로 도는 느린 요청(_SPAWN_METHODS) — 끝날 때까지 참조를 쥔다(GC 방지)
        self._lanes: dict = {}               # cid -> _Lane(그 기기의 아직 안 끝난 읽기들·마지막 쓰기 — _spawn_req 의 순서)
        self._req_sem = asyncio.Semaphore(_REQ_CONCURRENCY)   # 따로 도는 요청 전체의 동시 실행 상한
        self._write_lock = asyncio.Lock()    # Host 상태를 바꾸는 요청은 기기가 달라도 한 번에 하나(_is_write)

    # ---- 릴레이 송신 ----
    async def send(self, obj: dict) -> None:
        if self.ws is None:
            return
        cid = obj.get("cid")
        # 지금 처리 중인 요청의 답이거나 그 요청이 연 스트림의 출력이면 받을 때 정한 것을 따른다(_Req): 받은 뒤 그 기기가 끝났으면(떠남·해제·
        # 릴레이 재접속 — 위조된 client_offline 이라도) 보내지 않고, 봉투로 받은 요청이면 봉투로만 보낸다.
        rq = _REQ.get()
        mine = rq is not None and cid == rq.cid
        if mine and rq.live.over:
            return
        # 떠난 기기(client_offline·기기 해제·이전 릴레이 연결의 cid) 앞 프레임은 버린다 — 릴레이는 모르는 cid 앞 프레임을 방 전체에
        # broadcast 하고, E2EE 기기라도 떠날 때 enc_cids 에서 빠져 긴 요청의 늦은 응답·남은 파이프 출력이 평문으로 나갔다(검토 2회차).
        # 그 cid 로 다시 말을 걸면 _on_frame 이 풀어 준다(릴레이는 살아 있고 커넥터만 다시 붙은 경우).
        if cid is not None and cid in self.gone_cids:
            return
        # E2EE: 암호화로 말을 건 기기에는 암호화로 답한다. 평문 기기는 평문(호환). 봉투로 받은 요청의 답은 키가 없으면 버린다.
        if (mine and rq.sealed) or (cid is not None and cid in self.enc_cids):
            if self.e2ee_key is None:
                return
            from session_manager import e2ee
            obj = e2ee.wrap_frame(self.e2ee_key, obj)
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def _plain_send(self, obj: dict) -> None:
        """봉투 없이 그대로 전송(복호 실패 통지 등 - 내용 없는 제어 프레임 전용)."""
        if self.ws is not None:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    def _track_cid(self, rid, cid) -> None:
        """rid→cid 매핑 저장(응답/스트림 라우팅용). 무한 증식 방지 상한."""
        self.req_cid[rid] = cid
        if len(self.req_cid) > 4000:
            # 가장 오래된 항목부터 정리(dict 삽입순)
            for k in list(self.req_cid)[:1000]:
                self.req_cid.pop(k, None)

    def _wire(self, rid):
        """내부 키 → (프레임에 실을 id, cid). (cid, id) 튜플이면 기기가 보낸 원래 id 로 돌려준다."""
        if isinstance(rid, tuple):
            return rid[1], rid[0]
        return rid, self.req_cid.get(rid)

    def _owner(self, rid):
        """그 요청·스트림을 연 기기(cid). 튜플 키는 키에서 읽는다 — 응답 길 표(req_cid)는 상한을 넘으면 오래된 것부터 지우고
        재접속 때 비우므로, 표에 기대면 오래 열어 둔 터미널의 입력이 '인증 없는 기기'로 몰려 끊기고 client_offline·기기 해제가
        그 파이프를 찾지 못했다(검토 2회차)."""
        return self._wire(rid)[1]

    def _mark_gone(self, cid) -> None:
        """이 cid 앞 프레임은 더 보내지 않는다(send) — 이미 받아 둔 요청의 답·스트림 출력도(_Presence 를 끝낸다). 무한 증식 방지 상한.
        enc_cids 에서 빼기 전에 부른다: 봉투로 말하던 기기였는지 적어 두고, 그 cid 의 평문 프레임으로는 다시 풀지 않는다(_on_frame)."""
        p = self._live.pop(cid, None)
        if p is not None:
            p.over = True
        if cid is None:
            return
        self.gone_cids[cid] = bool(self.gone_cids.get(cid)) or cid in self.enc_cids
        if len(self.gone_cids) > 4000:
            for k in list(self.gone_cids)[:1000]:
                self.gone_cids.pop(k, None)

    def _depart(self, cid) -> None:
        """그 기기 연결(cid)이 끝났다(client_offline·기기 해제) — 인증·암호화 상태를 지우고 그 앞 프레임은 보내지 않는다."""
        self._mark_gone(cid)
        self.authed.pop(cid, None)
        self.enc_cids.discard(cid)
        self.plain_refused_logged.discard(cid)

    def _req_dev(self, cid):
        """이 요청을 보낸 기기 — 따로 도는 요청은 받을 때 인증한 기기(그 사이 떠났어도, _admit), 아니면 지금 인증된 기기."""
        rq = _REQ.get()
        if rq is not None and rq.cid == cid and rq.dev:
            return rq.dev
        return self.authed.get(cid)

    async def _admit(self, frame: dict, rq: _Req) -> bool:
        """따로 도는 요청의 기기 인증은 받을 때 본다 — 줄을 서는 사이 그 기기가 떠나도(같은 읽기로 온 client_offline·bye_device) 떠나기
        전에 보낸 요청은 실행된다(main·f1dcf7b 처럼). 실행할 때 보면 그 요청은 auth_required 로 끝나고 그 답도 떠난 기기 앞이라 버려져,
        PC 에 닿은 요청이 아무 흔적 없이 사라졌다(검토 4회차). 인증 없는 기기의 요청은 여기서 바로 거부한다(줄을 세우지 않는다)."""
        from session_manager import devices
        rid, cid = frame.get("id"), frame.get("cid")
        if not devices.enforced():
            rq.dev = self.authed.get(cid)
            return True
        dev = self.authed.get(cid)
        stale = bool(dev) and devices.token_version(dev["id"]) != dev.get("tver")
        if not dev or not devices.is_active(dev["id"]) or stale:
            self.authed.pop(cid, None)
            if cid is not None and not isinstance(rid, tuple):
                self._track_cid(rid, cid)
            await self._res(rid, False, error="auth_required")
            return False
        rq.dev, rq.admitted = dev, True
        return True

    def _presence(self, cid) -> _Presence:
        """그 cid 의 지금 _Presence(없으면 새로). 무한 증식 방지 상한 — 넘으면 오래된 것부터 끝낸다(그 답은 버려진다)."""
        p = self._live.get(cid)
        if p is None:
            p = self._live[cid] = _Presence()
            if len(self._live) > 4000:
                for k in list(self._live)[:1000]:
                    self._live.pop(k).over = True
        return p

    async def _res(self, rid, ok, data=None, error=None) -> None:
        wid, cid = self._wire(rid)
        msg = {"v": 1, "type": "res", "id": wid, "ok": ok}
        if cid is not None:
            msg["cid"] = cid   # 릴레이가 이 응답을 요청 기기에게만 전달
        if data is not None:
            msg["data"] = data
        if error is not None:
            msg["error"] = error
        await self.send(msg)

    async def _stream_send(self, rid, **kw) -> None:
        """stream 프레임 전송(요청 기기로 라우팅되도록 cid 자동 첨부)."""
        wid, cid = self._wire(rid)
        msg = {"v": 1, "type": "stream", "id": wid}
        if cid is not None:
            msg["cid"] = cid
        msg.update(kw)
        await self.send(msg)

    def _pclink_reply(self, rid, cid):
        """pclink 요청 하나의 답 통로 — 여러 번 불린다(접수 → 답). 그 cid 로만 보낸다: 키에 cid 를 묶어 응답 길 표(상한·재접속 때
        비움)에 기대지 않고, 보낼 곳을 모르거나(cid 없음) 릴레이에 다시 붙은 뒤의 늦은 답은 버린다 — 릴레이는 cid 없는 프레임·모르는
        cid 앞 프레임을 방 전체에 뿌린다(문서 pclink-protocol-v0 7절). 떠난 기기(client_offline) 앞 답은 send 가 버린다."""
        key = rid if isinstance(rid, tuple) else (cid, rid)
        gen = self.conn_gen

        async def reply(ok, data=None, error=None):
            if cid is None or self.conn_gen != gen:
                return
            await self._res(key, ok, data=data, error=error)
        return reply

    # ---- 기기 인증(auth 프레임) ----
    async def _auth(self, rid, cid, params: dict) -> None:
        """params: token(필수) · pv(클라이언트 규약 버전) · name(폰이 보고하는 자기 이름, B-1).

        name 은 등록부의 임시 이름(name_auto)만 채운다 — 사용자가 PC 에서 붙인 이름은 보존.
        정규화는 devices.clean_name 한 곳(제어문자·40자). 구 폰은 name 을 안 보내고, 구 Host 는
        name 을 무시한다(양쪽 다 무해).
        """
        from session_manager import devices

        dev = devices.verify(params.get("token"))
        if not dev:
            await self._res(rid, False, error="auth_invalid")
            return
        if cid is not None:
            # 인증 시점의 토큰 세대를 함께 보관(재발급 감지용)
            dev = {**dev, "tver": devices.token_version(dev["id"])}
            self.authed[cid] = dev
        first = devices.first_seen(dev["id"])
        devices.touch(dev["id"])
        filled = devices.set_name_if_empty(dev["id"], params.get("name"))
        if filled:
            dev["name"] = filled
            if cid is not None:
                self.authed[cid]["name"] = filled
        if first:
            # 첫 접속만: 감사 1줄 + PC 웹푸시(E-3 ①). 재접속마다 기록하면 하루 수백 줄이 된다(E-D10).
            devices.audit("first_auth", dev["id"], name=dev.get("name"), reported=params.get("name") or None)
            try:
                from session_manager import push
                push.send("device", dev["id"], f"📱 {dev.get('name') or dev['id'][:8]} 이 PC 에 연결됨",
                          "새 기기가 페어링을 마치고 처음 접속했습니다. 모르는 기기면 📱 목록에서 페어링을 해제하세요.")
            except Exception as e:  # noqa: BLE001
                _log(f"[devices] first-auth push 실패: {type(e).__name__}")
        # ver/hostname: 원격 화면이 "지금 붙은 PC 가 어떤 버전·어느 컴퓨터인지"
        # 보여줄 유일한 통로. (auth 응답만 커넥터가 직접 채워 보낸다)
        from session_manager import appconfig
        # pv: 클라이언트(앱/웹)가 Host 와 규약 일치를 확인하는 유일한 통로.
        # 클라이언트가 보낸 pv 가 더 높으면(=Host 가 구버전) 로그로 남긴다.
        try:
            client_pv = int(params.get("pv") or 0)
        except (TypeError, ValueError):
            client_pv = 0
        if client_pv > PROTOCOL_VERSION:
            _log(f"[pv] client pv={client_pv} > host pv={PROTOCOL_VERSION} - Host 업데이트 권장")
        await self._res(rid, True, data={"id": dev["id"], "name": dev["name"],
                                         "ver": CLIENT_VERSION,
                                         "pv": PROTOCOL_VERSION,
                                         "hostname": devices.clean_name(appconfig.machine_name(), "PC")})

    # ---- 메서드 디스패치 ----
    async def _handle_req(self, frame: dict) -> None:
        rid = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        cid = frame.get("cid")
        if cid is not None and not isinstance(rid, tuple):   # 튜플 키는 cid 를 품는다(_wire·_owner) — 표에 적지 않는다
            self._track_cid(rid, cid)
        try:
            from session_manager import devices

            # 기기 인증: 토큰 → 이 cid 를 인증 처리
            if method == "auth":
                await self._auth(rid, cid, params)
                return

            # pclink(짝지은 PC, 문서 pclink-protocol-v0 6·7절): 접속 토큰 없이 글마다 짝 키·초대 비밀로 스스로 인증하므로 기기 인증
            # 관문 앞에서 본체로 넘긴다. handle 은 곧바로 돌아온다(긴 일은 본체의 작업). 본체가 깨져도 폰 요청은 살게 여기서만 불러온다.
            if isinstance(method, str) and method.startswith("pclink"):
                from session_manager.pclink import inbound
                if method in inbound.METHODS:
                    await inbound.handle(method, params, cid, self._pclink_reply(rid, cid))
                    return

            # PC 등급 기기(kind=pc)는 pclink·ping 만 — 접속 토큰이 없어 원래 auth 될 수 없지만 이중 방어(문서 6절 끝)
            pc = self._req_dev(cid)           # 따로 도는 요청은 받을 때 인증한 기기(그 사이 떠났어도)
            if pc and pc.get("kind") == "pc" and method != "ping":
                did = str(pc.get("id"))
                if time.monotonic() - self.pc_denied_logged.get(did, -1e9) > 60:      # 같은 기기는 1분에 한 줄(로그 폭주 방지)
                    self.pc_denied_logged[did] = time.monotonic()
                    _log(f"[pclink] PC 기기 {did[:8]} 의 {method} 요청 거부(pc_not_allowed)")
                await self._res(rid, False, error="pc_not_allowed")
                return

            # 인증 강제(enforced) 시: 미인증/폐기 기기의 모든 메서드(ping 제외) 거부.
            # 등록 기기 0개면 아무도 통과 못 함 = 외부 전면 차단.
            if devices.enforced() and method != "ping":
                rq = _REQ.get()
                if rq is not None and rq.admitted and rq.cid == cid:
                    # 받을 때 인증했다(_admit) — 그 사이 그 기기가 떠났어도 실행한다(떠나기 전에 보낸 요청). 그 사이 PC 에서 해제·QR 재발급했으면
                    # 아직 시작하지 않은 요청은 실행하지 않는다
                    dev = rq.dev
                    if not devices.is_active(dev["id"]) or devices.token_version(dev["id"]) != dev.get("tver"):
                        await self._res(rid, False, error="auth_required")
                        return
                else:
                    dev = self.authed.get(cid)
                    stale = bool(dev) and devices.token_version(dev["id"]) != dev.get("tver")
                    if not dev or not devices.is_active(dev["id"]) or stale:
                        self.authed.pop(cid, None)
                        await self._res(rid, False, error="auth_required")
                        return

            if method == "ping":
                await self.send({"v": 1, "type": "pong", "id": self._wire(rid)[0],
                                 **({"cid": cid} if cid is not None else {})})

            elif method == "bye_device":
                # 폰이 자기 자격을 CP 에서 self-revoke 한 뒤 알린다(B-4) → 등록부 revoked(by=phone) 📵 + 연결 해체.
                # 인증된 자기 기기에만 적용(위 enforced 검사 통과 = authed). CP 동기화(devsync)가 없어도 즉시 반영.
                # 그 기기의 앞 요청 뒤에 줄 서 있는 사이 폰이 떠났어도 받을 때 인증한 기기로 해제한다(_req_dev)
                dev = self._req_dev(cid) or {}
                did = dev.get("id")
                if did:
                    changed = devices.mark_revoked(did, by="phone")
                    if changed:
                        devices.audit("self_revoke", did, via="bye_device")
                    await self._res(rid, True, data={"revoked": bool(changed)})
                    await self.drop_device(did)
                else:
                    await self._res(rid, False, error="auth_required")

            elif method == "list_sessions":
                q = {k: params[k] for k in ("q", "label", "limit")
                     if params.get(k) not in (None, "")}
                url = f"{self.local_base}/api/v1/sessions"
                if q:
                    url += "?" + urlencode(q)
                r = await self.http.get(url)
                await self._res(rid, True, data=r.json())

            elif method == "get_stats":
                sid = params["session_id"]
                r = await self.http.get(f"{self.local_base}/api/sessions/{sid}/stats")
                await self._res(rid, r.status_code == 200,
                                data=(r.json() if r.status_code == 200 else None),
                                error=(None if r.status_code == 200 else "not_found"))

            elif method == "ask":
                sid = params["session_id"]
                body = {"prompt": params.get("prompt", ""),
                        "skip_permissions": params.get("skip", True),
                        "timeout": params.get("timeout", 300)}
                r = await self.http.post(
                    f"{self.local_base}/api/v1/sessions/{sid}/ask", json=body)
                await self._res(rid, r.status_code == 200, data=r.json())

            elif method == "api":
                # 제네릭 프록시: 로컬 006 의 /api/* 엔드포인트를 그대로 호출한다.
                # PWA 가 목록/통계/뷰어/라벨/프로파일/검색/삭제 등을 이 한 메서드로 쓴다.
                await self._handle_api(rid, params, cid)

            elif method == "resume":
                await self._start_resume(rid, params)

            elif method == "terminal":
                # 웹터미널(/ws/terminal) 원시 스트림 터널링 (외부에서 진짜 xterm)
                await self._start_terminal(rid, params)

            elif method == "monitor":
                # 오케스트레이션 관제(/ws/monitor) 이벤트 스트림 터널링(읽기전용).
                await self._start_monitor(rid, params)

            elif method == "grant":
                # OTP 1회 검증 → grace 토큰 발급(이후 유효기간 동안 코드 없이 특권 동작)
                from session_manager import owner2fa
                if owner2fa.enabled() and owner2fa.verify(params.get("otp")):
                    await self._res(rid, True, data=owner2fa.issue_grace())
                else:
                    await self._res(rid, False, error=_otp_error())

            elif method == "export":
                await self._handle_export(rid, params)

            elif method == "import":
                await self._handle_import(rid, params)

            else:
                await self._res(rid, False, error=f"unknown_method:{method}")
        except KeyError as e:
            await self._res(rid, False, error=f"missing_param:{e.args[0]}")
        except Exception as e:  # noqa: BLE001
            await self._res(rid, False, error=f"{type(e).__name__}: {e}")

    # ---- 제네릭 API 프록시 (로컬 006 /api/* 전부) ----
    async def _handle_api(self, rid: str, params: dict, cid=None) -> None:
        verb = (params.get("verb") or "GET").upper()
        path = params.get("path") or ""
        # 안전장치: /api/ 로 시작하는 경로만 허용(임의 경로 프록시 금지)
        if not path.startswith("/api/") or ".." in path:
            await self._res(rid, False, error="path_not_allowed")
            return
        if _is_local_only_api(path, verb):
            await self._res(rid, False, error="local_only")
            return
        # 특권/파괴적 경로(터미널 종료 등)는 start 와 대칭으로 2FA 를 요구한다.
        if verb == "POST" and _is_privileged_api(path):
            if not _priv_ok(params):
                await self._res(rid, False, error=_otp_error())
                return
        url = f"{self.local_base}{path}"
        query = params.get("query") or {}
        body = params.get("body")
        via = {VIA_HEADER: "relay"}          # 서버가 로컬 전용 경로를 스스로 거부하게(2중 방어)
        dev = self._req_dev(cid) if cid is not None else None
        if dev and dev.get("id"):            # 인증된 기기의 요청에만 사람 증명(A-2) — 받을 때 인증한 기기로(그 사이 떠났어도)
            via.update({PROOF_HEADER: RELAY_PROOF, DEVICE_HEADER: str(dev["id"])})
            # 봉투로 받았나는 받을 때 정한 것(_Req.sealed)으로 — 실행할 때의 enc_cids 로 보면, 평문으로 받아 줄 선 요청이 그 사이 릴레이가
            # 다른 기기의 봉투 겉 cid 를 바꿔 넣은 뒤 E2EE 표시를 달고 나갔다(pclink 켜기의 D11 우회, 독립 검토 2026-10-27). 거꾸로 위조된
            # client_offline 뒤에 실행되는 봉투 요청이 표시를 잃지도 않는다.
            rq = _REQ.get()
            if (rq.sealed if rq is not None and rq.cid == cid else cid in self.enc_cids):
                via[E2EE_HEADER] = "1"
        if verb == "GET":
            r = await self.http.get(url, params=query, headers=via)
        elif verb == "POST":
            r = await self.http.post(url, params=query, json=body, headers=via)
        else:
            await self._res(rid, False, error=f"verb_not_allowed:{verb}")
            return
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            payload = {"text": r.text}
        await self._res(rid, r.status_code < 400,
                        data={"status": r.status_code, "json": payload})

    # ---- resume 스트리밍 파이프 ----
    async def _start_resume(self, rid: str, params: dict) -> None:
        if rid in self.streams:
            await self._res(rid, False, error="stream_exists")
            return
        sid = params["session_id"]
        skip = "1" if params.get("skip", True) else "0"
        # inplace=True 면 소유자용 실제세션 재개(fork 아님) — 외부 경유는 2FA 필수
        if params.get("inplace"):
            if not _priv_ok(params):
                await self._stream_send(rid, eof=True, error=_otp_error())
                return
            ep = "resume-inplace"
        else:
            ep = "resume"
        # src=relay + 메모리 비밀: 인증된 폰(사람)의 입력임을 Host 에 증명(팀 장부의 사장님 입력 판정)
        local_ws = (f"{_to_ws(self.local_base)}/api/v1/{ep}/{sid}?skip={skip}"
                    f"&src=relay&proof={RELAY_PROOF}")
        self.stream_in[rid] = asyncio.Queue()
        self.streams[rid] = asyncio.create_task(self._pipe_resume(rid, local_ws))

    async def _pipe_resume(self, rid: str, local_ws: str) -> None:
        try:
            async with _connect_local(local_ws) as lws:
                async def up():  # client → local
                    q = self.stream_in[rid]
                    while True:
                        data = await q.get()
                        if data is None:
                            return
                        await lws.send(json.dumps(data, ensure_ascii=False))

                up_task = asyncio.create_task(up())
                try:
                    async for raw in lws:  # local → client
                        try:
                            data = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            data = {"raw": raw}
                        await self._stream_send(rid, data=data)
                finally:
                    up_task.cancel()
            await self._stream_send(rid, eof=True)
        except Exception as e:  # noqa: BLE001
            await self._stream_send(rid, eof=True, error=f"{type(e).__name__}: {e}")
        finally:
            self.streams.pop(rid, None)
            self.stream_in.pop(rid, None)

    # ---- export/import (바이너리 zip 을 base64 로 릴레이 터널링) ----
    async def _handle_export(self, rid: str, params: dict) -> None:
        import base64
        import re as _re
        sid = params["session_id"]
        wd = "true" if params.get("include_workdir") else "false"
        r = await self.http.get(f"{self.local_base}/api/sessions/{sid}/export",
                                params={"include_workdir": wd})
        if r.status_code != 200:
            await self._res(rid, False, error=f"export_failed:{r.status_code}")
            return
        cd = r.headers.get("content-disposition", "")
        m = _re.search(r'filename="?([^"]+)"?', cd)
        fn = m.group(1) if m else f"session_{sid[:8]}.zip"
        await self._res(rid, True,
                        data={"b64": base64.b64encode(r.content).decode(), "filename": fn})

    async def _handle_import(self, rid: str, params: dict) -> None:
        import base64
        try:
            blob = base64.b64decode(params.get("b64", ""))
        except Exception:  # noqa: BLE001
            await self._res(rid, False, error="bad_b64")
            return
        files = {"file": (params.get("filename", "import.zip"), blob, "application/zip")}
        form = {"overwrite": "true" if params.get("overwrite") else "false",
                "restore_workdir": "true" if params.get("restore_workdir") else "false"}
        if params.get("new_cwd"):
            form["new_cwd"] = params["new_cwd"]
        r = await self.http.post(f"{self.local_base}/api/import", files=files, data=form)
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            j = {"error": "bad_response"}
        await self._res(rid, r.status_code == 200, data=j,
                        error=(None if r.status_code == 200 else j.get("error")))

    # ---- 웹터미널 스트리밍 파이프 (원시 텍스트 in/out) ----
    async def _start_terminal(self, rid: str, params: dict) -> None:
        if rid in self.streams:
            await self._res(rid, False, error="stream_exists")
            return
        # 웹터미널도 실제 세션 in-place(full shell) → 외부 경유는 2FA 필수
        if not _priv_ok(params):
            await self._stream_send(rid, eof=True, error=_otp_error())
            return
        sid = params["session_id"]
        skip = "1" if params.get("skip", True) else "0"
        fork = params.get("fork")
        local_ws = f"{_to_ws(self.local_base)}/ws/terminal/{sid}?skip={skip}"
        if fork:
            local_ws += f"&fork={fork}"
        # 화면 커서(탭 즉시 전환): 클라가 탭마다 준 screen_id. Host 가 이 화면에 보낸
        # 오프셋을 기억해 재접속 때 못 본 델타만 리플레이한다. 없으면 기존 동작.
        screen = params.get("screen")
        if screen:
            local_ws += "&" + urlencode({"screen": str(screen)})
        self.stream_in[rid] = asyncio.Queue()
        self.streams[rid] = asyncio.create_task(self._pipe_terminal(rid, local_ws))

    async def _pipe_terminal(self, rid: str, local_ws: str) -> None:
        try:
            async with _connect_local(local_ws) as lws:
                async def up():  # client → local (input/resize 제어객체)
                    q = self.stream_in[rid]
                    while True:
                        data = await q.get()
                        if data is None:
                            return
                        await lws.send(json.dumps(data, ensure_ascii=False))

                up_task = asyncio.create_task(up())
                try:
                    async for raw in lws:  # local → client (터미널 출력 텍스트 그대로)
                        await self._stream_send(rid, data=raw)
                finally:
                    up_task.cancel()
            await self._stream_send(rid, eof=True)
        except Exception as e:  # noqa: BLE001
            await self._stream_send(rid, eof=True, error=f"{type(e).__name__}: {e}")
        finally:
            self.streams.pop(rid, None)
            self.stream_in.pop(rid, None)

    # ---- 관제 스트리밍 파이프 (읽기전용 단방향) ----
    async def _start_monitor(self, rid: str, params: dict) -> None:
        if rid in self.streams:
            await self._res(rid, False, error="stream_exists")
            return
        # ids: 콤마 문자열 또는 리스트. manager: 선택.
        ids = params.get("ids") or params.get("session_ids") or ""
        if isinstance(ids, list):
            ids = ",".join(str(x) for x in ids)
        ids = str(ids).strip()
        if not ids:
            await self._stream_send(rid, eof=True, error="empty_group")
            return
        q = {"ids": ids}
        manager = params.get("manager")
        if manager:
            q["manager"] = str(manager)
        if params.get("gid"):
            q["gid"] = str(params["gid"])
        local_ws = f"{_to_ws(self.local_base)}/ws/monitor?{urlencode(q)}"
        # 관전은 기본 단방향이나, 동적 그룹 변경(add/remove) 제어를 위해 up 채널을 둔다.
        self.stream_in[rid] = asyncio.Queue()
        self.streams[rid] = asyncio.create_task(self._pipe_monitor(rid, local_ws))

    async def _pipe_monitor(self, rid: str, local_ws: str) -> None:
        try:
            async with _connect_local(local_ws) as lws:
                async def up():  # client → local (그룹 add/remove 제어 객체)
                    q = self.stream_in[rid]
                    while True:
                        data = await q.get()
                        if data is None:
                            return
                        await lws.send(json.dumps(data, ensure_ascii=False))

                up_task = asyncio.create_task(up())
                try:
                    async for raw in lws:  # local → client (관제 이벤트 JSON 그대로)
                        await self._stream_send(rid, data=raw)
                finally:
                    up_task.cancel()
            await self._stream_send(rid, eof=True)
        except Exception as e:  # noqa: BLE001
            await self._stream_send(rid, eof=True, error=f"{type(e).__name__}: {e}")
        finally:
            self.streams.pop(rid, None)
            self.stream_in.pop(rid, None)

    async def _handle_stream_in(self, frame: dict) -> None:
        rid = frame.get("id")
        q = self.stream_in.get(rid)
        if q is None:
            return
        # 기기 인증 강제 중이면 스트림 입력도 살아 있는 인증에 묶는다 — 기기를 삭제/재발급해도
        # 이미 열린 터미널 입력이 계속 들어가던 구멍(0.9.8). 폐기된 기기의 프레임은 버리고 파이프를 끊는다.
        from session_manager import devices
        if devices.enforced():
            cid = self._owner(rid)
            dev = self.authed.get(cid) if cid is not None else None
            if not dev or not devices.is_active(dev["id"]):
                task = self.streams.get(rid)
                if task is not None:
                    _log(f"[stream] 인증 없는 기기 입력 거부 rid={rid} → 파이프 해체")
                    task.cancel()
                return
        await q.put(frame.get("data"))

    async def drop_device(self, device_id: str, notice: str | None = None) -> int:
        """기기 삭제/재발급/폐기 직후: 그 기기의 모든 연결(cid)을 인증 해제하고 스트림을 끊는다.

        CP 의 자격 폐기는 '다음 JWT 발급' 부터 막을 뿐, 이미 열린 릴레이 연결·터미널 스트림은
        별개다(0.9.8). 돌려주는 값은 끊은 연결 수.

        notice(0.11.0, B-5/E-D5/E-D15): 해체 **앞에** 같은 WS 로 `notice` 프레임을 보낸다(순서 보장) — 폰이
        '해제됨/재발급됨' 을 구별해 ⛔/🔄 를 보인다. 전송은 최선(1초 제한, 실패는 로그), 해체는 finally 로 무조건.
        """
        cids = [cid for cid, dev in list(self.authed.items()) if dev.get("id") == device_id]
        try:
            if notice:
                for cid in cids:
                    try:
                        await asyncio.wait_for(self.send({"v": 1, "type": "notice", "cid": cid,
                                                          "kind": notice, "device": device_id}), 1.0)
                    except Exception as e:  # noqa: BLE001
                        _log(f"[devices] notice {notice} → {cid} 전송 실패: {type(e).__name__}")
        finally:
            n = 0
            for cid in cids:
                for rid, task in list(self.streams.items()):
                    if self._owner(rid) == cid:
                        task.cancel()
                # 해제한 기기 앞으로 늦게 나가는 응답(평문이 됐다)도 보내지 않는다 — 그 기기가 프레임을 하나 더 보내 떠남 표시가 풀려도(검토 4회차)
                self._depart(cid)
                n += 1
            if n:
                _log(f"[devices] 기기 {device_id} 연결 {n}개 해제(스트림 종료{', ' + notice if notice else ''})")
        return n

    # ---- 프레임 라우팅 ----
    async def _on_frame(self, raw: str) -> None:
        """받은 글 하나. 처리 중 예외는 이 글만 버린다 — 밖으로 나가면 run() 이 릴레이에 다시 붙으며(_on_connected) 모든 기기의 인증·
        파이프를 지웠다. 방 참가자 누구나(초대 문자열·짝 PC 포함) method 에 목록 같은 값을 넣어 그렇게 할 수 있었다(독립 검토 2026-10-27).
        릴레이 소켓이 닫혀 보내기가 실패한 것도 여기서 버린다 — 끊김은 run() 의 수신이 안다."""
        try:
            await self._route(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log(f"[connector] 받은 글 처리 실패(그 글만 버림): {type(e).__name__}: {e}")

    async def _route(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(frame, dict):     # '[1]'·'null' 같은 글이 frame.get 예외로 접속 루프를 끊어(재접속·모든 파이프 해체) 방
            return                          # client 자격만 있으면(초대 문자열·짝 PC 포함) 폰 세션을 거듭 끊을 수 있었다
        # E2EE 봉투면 먼저 연다(릴레이가 겉에 주입한 cid 는 unwrap 이 반영).
        from session_manager import e2ee
        sealed = False
        if e2ee.is_envelope(frame):
            if self.e2ee_key is None:
                return                      # 키 없는데 봉투 - 응답 불가, 무시
            try:
                inner = e2ee.unwrap_frame(self.e2ee_key, frame)
            except ValueError:
                # 키 불일치(기기 재페어링 필요). 내용 없는 평문 오류로만 알린다.
                await self._plain_send({"v": 1, "type": "res", "id": None, "ok": False,
                                        "error": "e2ee_key_mismatch",
                                        **({"cid": frame["cid"]} if frame.get("cid") is not None else {})})
                return
            frame = inner
            sealed = True
            if frame.get("cid") is not None:
                self.enc_cids.add(frame["cid"])
        elif self.e2ee_key is not None:
            # 평문 프레임: require 모드면, 아니어도 봉투로 말한 적 있는 기기(cid)면 실질 요청(req/stream_in/stream_close)을 거부한다.
            # 봉투로 말한 기기는 모든 프레임을 봉투로 보낸다(PWA _tx — 인증 auth 도) — 그 cid 의 평문은 릴레이가 넣은 것이다.
            # 예전엔 require 가 꺼져 있으면(기본) 받아, 릴레이가 인증된 폰 cid 로 평문 요청을 넣어 실행시킬 수 있었고(바로가기 검토
            # 4회차 inject 재현) 그 요청은 E2EE 표시(enc_cids)까지 달고 Host 로 갔다. 봉투를 쓴 적 없는 기기(옛 앱·키 없는 기기)는 그대로.
            from session_manager import appconfig
            # pclink 은 예외 — 짝 PC 는 방 키가 없고, 안쪽 봉투가 짝 키로 봉인돼 있어야만 받는다(문서 pclink-protocol-v0 6절 끝)
            pclink_req = frame.get("type") == "req" and frame.get("method") in ("pclink", "pclink.pair")
            sealed_cid = frame.get("cid") is not None and frame["cid"] in self.enc_cids
            if ((sealed_cid or appconfig.get_bool("e2ee", "require", False)) and not pclink_req
                    and frame.get("type") in ("req", "stream_in", "stream_close")):
                if sealed_cid and frame["cid"] not in self.plain_refused_logged:   # 기기마다 한 줄(릴레이가 쏟아부어도 로그 폭주 없음)
                    self.plain_refused_logged.add(frame["cid"])
                    _log(f"[e2ee] 봉투로 말하던 기기의 평문 {frame.get('type')} 거부(cid {str(frame['cid'])[:8]}) - 릴레이 주입 의심")
                await self._plain_send({"v": 1, "type": "res", "id": frame.get("id"),
                                        "ok": False, "error": "e2ee_required",
                                        **({"cid": frame["cid"]} if frame.get("cid") is not None else {})})
                return
        t = frame.get("type")
        cid = frame.get("cid")
        # 기기가 말을 걸었다 — 떠난 것으로 적어 둔 cid(이전 릴레이 연결의 cid)라도 다시 받는다(릴레이는 살아 있고 커넥터만 다시 붙은 경우).
        # 다만 봉투로 말하던 기기(E2EE)는 봉투로만 — 평문 프레임은 릴레이가 위조할 수 있다(검토 4회차). 진짜 폰은 봉투로 다시 인증한다.
        if t in ("req", "stream_in", "stream_close", "ping") and cid is not None and cid in self.gone_cids:
            if sealed or not self.gone_cids[cid]:
                self.gone_cids.pop(cid, None)
        # 기기(폰·태블릿 등)는 저마다 요청 id 를 1 부터 센다 — 둘이 동시에 붙으면 'c5'·'t3' 가 겹쳐 응답이 다른 기기로 가고
        # 터미널 출력이 섞이며, 한쪽의 stream_close 가 다른 기기 스트림을 끊었다(0.29.0 발견). 릴레이가 붙인 cid 와 묶어
        # (cid, id) 를 내부 키로 쓴다. 응답·스트림 프레임에는 _wire 가 원래 id 와 cid 를 그대로 싣는다.
        if t in ("req", "stream_in", "stream_close") and cid is not None and frame.get("id") is not None:
            frame = {**frame, "id": (cid, frame["id"])}
        if t == "req":
            # 받을 때 정한다(_Req): 봉투로 받았거나 봉투로 말하던 기기면 답도 봉투로만, 그리고 지금 이 기기 연결의 표(_Presence)
            rq = _Req(cid, sealed or (cid is not None and cid in self.enc_cids), self._presence(cid))
            tok = _REQ.set(rq)
            try:
                m = frame.get("method")
                if isinstance(m, str) and m in _SPAWN_METHODS:
                    if self._backlog_full(cid):
                        await self._res(frame.get("id"), False, error="busy")   # 줄이 넘치면 받지 않는다(위 _LANE_BACKLOG)
                    elif await self._admit(frame, rq):  # 기기 인증은 받을 때(위 _admit)
                        self._spawn_req(frame, rq)      # 받는 길을 막지 않는다(위 _SPAWN_METHODS)
                else:
                    await self._handle_req(frame)
            finally:
                _REQ.reset(tok)
        elif t == "stream_in":
            await self._handle_stream_in(frame)
        elif t == "stream_close":
            # 클라이언트가 특정 스트림(예: 터미널 탭)만 닫는다 — rid 하나만 해체.
            # client_offline 이 기기 전체를 해체하는 것과 달리 rid 단위다. task.cancel()
            # 이 `async with connect` 를 풀어 로컬 WS 가 닫히고, Host 가 화면을 detach
            # 한다(persist PTY 는 설계대로 유지 — 프로세스-화면 분리). 멀티탭 뷰어에서
            # 배경 탭이 라이브 WS 를 놓되 PTY 는 살려두는 것의 서버측 근거.
            rid = frame.get("id")
            task = self.streams.get(rid)
            if task is not None:
                _log(f"[stream] stream_close rid={rid} → 파이프 해체(화면 detach, PTY 유지)")
                task.cancel()
        elif t == "peer":
            # 기기 연결 종료 → 인증/암호화 상태 정리
            if frame.get("event") == "client_offline":
                # 긴 요청의 늦은 응답은 보내지 않는다 — 평문으로 방 전체에 갔다(검토 2회차). 그 cid 로 프레임이 다시 와도(검토 4회차)
                self._depart(cid)
                # ★ 그 기기가 잡고 있던 스트림 파이프 해체. 이게 빠져 있어서 폰이
                # 소리 없이 사라지면(앱 스와이프 종료·탭 닫기·전파 단절) 파이프가
                # Host 로컬 WS 를 계속 잡아 웹재개 claude(-p 스트림)가 고아로
                # 남았다(실사고: 5일 생존 2건). task.cancel() 이 `async with
                # connect` 를 해제해 로컬 WS 가 닫히고, Host 의 finally 가
                # 프로세스를 정리한다. persist 터미널은 화면만 떨어지고 PTY 는
                # 설계대로 유지된다(webterm 의 프로세스-화면 분리).
                for rid, task in list(self.streams.items()):
                    if self._owner(rid) == cid:
                        _log(f"[stream] client_offline cid={cid} → 파이프 해체 rid={rid}")
                        task.cancel()
        elif t == "ping":
            await self.send({"v": 1, "type": "pong", "id": frame.get("id"),
                             **({"cid": frame["cid"]} if frame.get("cid") is not None else {})})
        # 그 외(hello 등)는 무시

    def _backlog_full(self, cid) -> bool:
        lane = self._lanes.get(cid)
        return len(self._req_tasks) >= _REQ_BACKLOG or (lane is not None and lane.n >= _LANE_BACKLOG)

    def _spawn_req(self, frame: dict, rq: _Req | None = None) -> None:
        """따로 돌린다 — 그 기기(cid)의 순서는 지킨다(위 _REQ_CONCURRENCY): 읽기는 앞서 받은 쓰기가 끝난 뒤에(앞 읽기와는 함께),
        쓰기는 앞서 받은 읽기·쓰기가 모두 끝난 뒤에 시작한다. 그래서 쓰기 앞뒤의 읽기는 받은 순서대로 그 쓰기 전·후의 상태를 본다."""
        cid = frame.get("cid")
        lane = self._lanes.get(cid)
        if lane is None:
            lane = self._lanes[cid] = _Lane()
        write = _is_write(frame.get("method"), frame.get("params"))
        prev = [t for t in ((*lane.reads, lane.write) if write else (lane.write,)) if t is not None and not t.done()]
        task = asyncio.create_task(self._run_req(frame, rq, prev, write))
        lane.n += 1
        if write:
            lane.write, lane.reads = task, set()     # 뒤 요청은 이 쓰기를 기다린다(이 쓰기가 앞 읽기를 기다린다)
        else:
            lane.reads.add(task)
        self._req_tasks.add(task)

        def _done(t, cid=cid):
            self._req_tasks.discard(t)
            ln = self._lanes.get(cid)
            if ln is None:
                return
            ln.n -= 1
            ln.reads.discard(t)
            if ln.write is t:
                ln.write = None
            if ln.n <= 0:
                del self._lanes[cid]
        task.add_done_callback(_done)

    async def _run_req(self, frame: dict, rq: _Req | None = None, prev=(), write: bool = False) -> None:
        """따로 도는 요청. _handle_req 는 실패를 응답으로 돌려주지만, 그 응답을 보내다 릴레이가 끊기는 등의 예외는 여기서 거둔다
        (받는 길은 이미 다음 프레임을 처리 중이라 그 예외로 재접속할 이유가 없다 — 끊김은 run() 의 수신이 안다).
        순서: 그 기기의 앞 요청(_spawn_req 가 고른 것)이 끝나기를 기다린 뒤 → (쓰기면) 쓰기 차례 → 전체 상한 자리.
        기다리는 동안은 자리를 쥐지 않는다."""
        if rq is not None:
            _REQ.set(rq)                    # 이 태스크의 컨텍스트에만(받을 때 정한 것 — send)
        try:
            if prev:
                await asyncio.wait(prev)    # 앞 요청이 실패·취소로 끝나도 다음은 돈다
            async with (self._write_lock if write else contextlib.nullcontext()):
                async with self._req_sem:
                    await self._handle_req(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _log(f"[connector] 요청 처리 실패 method={frame.get('method')}: {type(e).__name__}: {e}")

    # ---- 접속 URI 구성 (CP 가 설정돼 있으면 단기 JWT 첨부) ----
    async def _build_uri(self) -> tuple[str, str]:
        """(uri, mode) 반환. CP 불통이면 공유토큰만으로(fail-open) 접속한다.

        JWT 는 매 (재)접속마다 새로 받는다 — 짧은 TTL 이라 캐시 이득이 없고,
        폐기(auth_version 증가)가 다음 접속에 바로 반영되는 이점이 있다.
        """
        params = {"role": "agent", "room": self.room}
        if self.token:               # 공유 토큰은 있을 때만(JWT 배포에선 없음)
            params["token"] = self.token
        params["v"] = PROTOCOL_VERSION
        params["cv"] = CLIENT_VERSION
        mode = "shared"
        try:
            from session_manager import cp_client
            if cp_client.cp_url():
                # 블로킹 httpx 호출 → 이벤트 루프 밖으로
                out = await asyncio.get_running_loop().run_in_executor(
                    None, cp_client.fetch_jwt, "")
                if out and out.get("token"):
                    params["jwt"] = out["token"]
                    cp_room = out.get("room_key")
                    # room 이사는 기존 페어링(폰·QR)을 무음으로 끊는다 → 기본 금지.
                    # 수동 room(SM_RELAY_ROOM)이 없거나 SM_CP_ADOPT_ROOM=1 일 때만 채택.
                    adopt = os.environ.get("SM_CP_ADOPT_ROOM") == "1"
                    manual = os.environ.get("SM_RELAY_ROOM")
                    if cp_room and cp_room != self.room and (adopt or not manual):
                        cp_client.log(f"[cp] room 채택: {self.room} -> {cp_room}")
                        params["room"] = cp_room
                        self.room = cp_room
                    elif cp_room and cp_room != self.room:
                        # 기존 room 유지(무중단). P1b 릴레이 검증 전에 room 정합 필요.
                        cp_client.log(f"[cp] [!] CP room({cp_room[:12]})과 현재 room 이 다름 -> "
                              f"기존 room 유지(기존 페어링 보호). 이사하려면 SM_CP_ADOPT_ROOM=1")
                        cp_client.mark_room_mismatch(cp_room, self.room)
                    mode = "jwt"
                else:
                    mode = "fallback"
        except Exception as e:  # noqa: BLE001
            # CP 경로의 어떤 예외도 릴레이 접속을 막지 않는다(fail-open)
            _log(f"[cp] JWT 조달 실패({type(e).__name__}) -> 공유토큰으로 계속")
            mode = "fallback"
        # 실제 접속에 쓰일 모드를 확정 기록(status 와 실제의 불일치 방지)
        try:
            from session_manager import cp_client as _cpc
            if _cpc.cp_url():
                _cpc.set_mode(mode, "ok" if mode == "jwt" else _cpc.status().get("reason", ""))
        except Exception:  # noqa: BLE001
            pass
        return f"{self.relay_url}?{urlencode(params)}", mode

    # ---- 정상 종료 예고(자기 회복 Phase 2) ----
    async def bye(self, reason: str = "shutdown") -> bool:
        """릴레이에 bye 프레임을 보낸다 — 업데이트·종료·유지보수 같은 의도된 단절에 릴레이가
        '연결 끊김' 알림을 내지 않게. 소켓이 없거나 실패해도 조용히 False."""
        ws = self.ws
        if ws is None:
            return False
        try:
            await asyncio.wait_for(ws.send(json.dumps({"v": 1, "type": "bye", "reason": reason})), timeout=2)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _on_connected(self) -> None:
        """릴레이 (재)접속 직후: cid 는 전부 새로 발급되므로 인증·암호화·라우팅 상태를 비운다.
        (옛 cid 의 authed 가 남아 '접속 중' 으로 잘못 보이거나 스트림 입력이 통과하던 문제, 0.9.8)

        이전 연결에서 연 파이프(웹 재개·터미널 화면·관제)도 끝내고 그 cid 들 앞 프레임은 보내지 않는다(검토 2회차). 릴레이가
        재기동했으면 그 cid 는 사라졌고 client_offline 도 오지 않아, 남은 파이프는 새 cid 의 stream_close 로도 닫히지 않았고
        (웹 재개면 claude -p 가 Host 재시작까지 남음) 출력은 모르는 cid 앞이라 방 전체에 평문으로 broadcast 됐다. 릴레이가 살아
        있었어도 인증·암호화 상태를 비웠으니 그 파이프는 더 쓸 수 없다. persist 터미널은 화면만 떨어지고 PTY 는 그대로다.

        이전 연결에서 받아 아직 끝나지 않은 요청(따로 도는 요청)의 답도 보내지 않는다(검토 4회차) — 같은 cid 의 폰이 릴레이에 그대로 있어
        프레임이 다시 와 떠남 표시가 풀려도. 폰은 PC 가 떠난 것(agent_offline)으로 보고 그 대기를 이미 끝냈다.
        """
        self.conn_gen += 1                    # 이전 연결의 pclink 요청에 늦게 나오는 답은 버린다(_pclink_reply) — 그 cid 는 표에 없다
        old = (set(self.authed) | set(self.enc_cids) | {c for c in self.req_cid.values() if c is not None}
               | {c for c in self._live if c is not None})
        for rid, task in list(self.streams.items()):
            old.add(self._owner(rid))
            task.cancel()
        if self.streams:
            _log(f"[stream] 릴레이 재접속 → 이전 연결의 파이프 {len(self.streams)}개 해체")
        for cid in old:
            self._mark_gone(cid)
        for p in self._live.values():         # cid 없는 요청(옛 릴레이)의 표까지
            p.over = True
        self._live.clear()
        self.enc_cids.clear()
        self.plain_refused_logged.clear()
        self.authed.clear()
        self.req_cid.clear()

    # ---- 접속 + 재연결 루프 ----
    async def run(self) -> None:
        global CURRENT
        CURRENT = self
        self.loop = asyncio.get_running_loop()
        backoff = 1
        while True:
            try:
                uri, mode = await self._build_uri()
                if not self.room:
                    # room 미확정(CP 발급 대기). 빈 room 으로 붙으면 릴레이가 4400 으로 끊는다.
                    _log("[connector] room 미확정(CP 발급 대기) -> 대기 후 재시도")
                    await asyncio.sleep(min(backoff, 30))
                    backoff = min(backoff * 2, 30)
                    continue
                if mode == "fallback":
                    _log("[connector] [!] CP 불통 -> 공유토큰 fallback 으로 접속(기능 유지)")
                # E2EE 키 로드(없으면 생성) - 페어링 QR 에 실려 기기로 전달된다.
                try:
                    from session_manager import e2ee
                    self.e2ee_key = e2ee.room_key(self.room)
                except Exception as e:  # noqa: BLE001
                    self.e2ee_key = None
                    _log(f"[connector] E2EE 키 로드 실패(평문 폴백): {type(e).__name__}")
                async with connect(uri, max_size=None) as ws:
                    self.ws = ws
                    backoff = 1
                    self._on_connected()
                    _log(f"[connector] relay 연결됨 room={self.room} mode={mode} "
                         f"e2ee={'on' if self.e2ee_key else 'off'} -> 로컬 {self.local_base}")
                    async for raw in ws:
                        await self._on_frame(raw)
            except Exception as e:  # noqa: BLE001
                # 예외 문구(InvalidURI 등)에는 주소 전체 — 쿼리의 jwt·토큰 — 가 들어갈 수 있다
                print(f"[connector] 연결 끊김: {type(e).__name__}: {logredact.redact(str(e))} "
                      f"→ {backoff}s 후 재시도")
            finally:
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def _amain():
    relay_url = os.environ.get("RELAY_URL")
    room = os.environ.get("RELAY_ROOM")
    token = os.environ.get("RELAY_AGENT_TOKEN")
    missing = [n for n, v in (("RELAY_URL", relay_url), ("RELAY_ROOM", room),
                              ("RELAY_AGENT_TOKEN", token)) if not v]
    if missing:
        print(f"[connector] 필수 환경변수 누락: {', '.join(missing)}")
        return
    await Connector(relay_url, room, token).run()


def main():
    # 커넥터를 따로 띄우는 경우(clewpath-connector)에도 설정 파일이 먹어야 한다.
    # 006 서버에 내장돼 돌 때는 server.main() 이 이미 적용한 뒤라 여기선 no-op.
    from session_manager import appconfig, systls
    systls.use_system_trust()   # 회사 TLS 인터셉션 대응 — httpx(JWT 조달)가 OS 인증서를 쓰게(멱등)
    appconfig.apply_env()
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n[connector] stopped")


if __name__ == "__main__":
    main()
