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
import json
import os
import sys
from urllib.parse import urlencode

import httpx
import websockets
from websockets.asyncio.client import connect

DEFAULT_LOCAL_BASE = os.environ.get(
    "SM_LOCAL_BASE", "http://127.0.0.1:5100").rstrip("/")

# 릴레이 프레임 프로토콜 버전. 서버(릴레이)가 최소 지원 버전을 강제할 때 쓴다.
# 프레임 형식이 호환 불가하게 바뀌면 올린다.
PROTOCOL_VERSION = 1


def _client_version() -> str:
    """설치된 패키지 버전(업데이트 필요 판단용). 못 읽으면 'unknown'."""
    try:
        from importlib.metadata import version
        return version("clewpath-host")
    except Exception:  # noqa: BLE001
        return "unknown"


CLIENT_VERSION = _client_version()


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


def _otp_error() -> str:
    try:
        from session_manager import owner2fa
        return "2fa_required" if not owner2fa.enabled() else "2fa_invalid"
    except Exception:  # noqa: BLE001
        return "2fa_required"


def _to_ws(base: str) -> str:
    """http(s)://host → ws(s)://host 로 변환."""
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    return "ws://" + base[len("http://"):]


def relay_config_from_env() -> dict | None:
    """통합(006 내장) 릴레이 모드 설정을 환경변수에서 읽는다.

    필수: SM_RELAY_URL + SM_RELAY_AGENT_TOKEN.
    room 은 다음 순서로 결정한다 —
      1) SM_RELAY_ROOM (수동 지정. 기존 배포·기존 페어링 유지용)
      2) 컨트롤플레인이 발급한 room (자격증명 파일 / 최초 발급)  ← 신규 설치 경로
    둘 다 없으면 None(=릴레이 OFF). 즉 SM_CP_URL 만 있으면 room 없이도 켜진다
    (신규 사용자는 room 을 알 수 없으므로 CP 가 발급해 준다).

    로컬 베이스는 SM_LOCAL_BASE 또는 http://127.0.0.1:{SM_PORT} (자기 자신).
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

    port = os.environ.get("SM_PORT", "5100")
    local_base = os.environ.get("SM_LOCAL_BASE") or f"http://127.0.0.1:{port}"
    return {"relay_url": url, "room": room, "token": token,
            "local_base": local_base}


class Connector:
    def __init__(self, relay_url: str, room: str, token: str,
                 local_base: str | None = None):
        self.relay_url = relay_url
        self.room = room
        self.token = token
        self.local_base = (local_base or DEFAULT_LOCAL_BASE).rstrip("/")
        self.ws = None                       # 릴레이 연결
        self.http = httpx.AsyncClient(timeout=310)
        self.streams: dict[str, asyncio.Task] = {}   # id -> resume 파이프 태스크
        self.stream_in: dict[str, asyncio.Queue] = {}  # id -> client 입력 큐
        self.req_cid: dict[str, str] = {}    # rid -> cid (응답을 요청 기기로 라우팅)
        self.authed: dict[str, dict] = {}    # cid -> 인증된 기기{id,name}

    # ---- 릴레이 송신 ----
    async def send(self, obj: dict) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    def _track_cid(self, rid, cid) -> None:
        """rid→cid 매핑 저장(응답/스트림 라우팅용). 무한 증식 방지 상한."""
        self.req_cid[rid] = cid
        if len(self.req_cid) > 4000:
            # 가장 오래된 항목부터 정리(dict 삽입순)
            for k in list(self.req_cid)[:1000]:
                self.req_cid.pop(k, None)

    async def _res(self, rid, ok, data=None, error=None) -> None:
        msg = {"v": 1, "type": "res", "id": rid, "ok": ok}
        cid = self.req_cid.get(rid)
        if cid is not None:
            msg["cid"] = cid   # 릴레이가 이 응답을 요청 기기에게만 전달
        if data is not None:
            msg["data"] = data
        if error is not None:
            msg["error"] = error
        await self.send(msg)

    async def _stream_send(self, rid, **kw) -> None:
        """stream 프레임 전송(요청 기기로 라우팅되도록 cid 자동 첨부)."""
        msg = {"v": 1, "type": "stream", "id": rid}
        cid = self.req_cid.get(rid)
        if cid is not None:
            msg["cid"] = cid
        msg.update(kw)
        await self.send(msg)

    # ---- 메서드 디스패치 ----
    async def _handle_req(self, frame: dict) -> None:
        rid = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        cid = frame.get("cid")
        if cid is not None:
            self._track_cid(rid, cid)
        try:
            from session_manager import devices

            # 기기 인증: 토큰 → 이 cid 를 인증 처리
            if method == "auth":
                dev = devices.verify(params.get("token"))
                if dev:
                    if cid is not None:
                        # 인증 시점의 토큰 세대를 함께 보관(재발급 감지용)
                        dev = {**dev, "tver": devices.token_version(dev["id"])}
                        self.authed[cid] = dev
                    devices.touch(dev["id"])
                    # ver/hostname: 원격 화면이 "지금 붙은 PC 가 어떤 버전·어느 컴퓨터인지"
                    # 보여줄 유일한 통로. (auth 응답만 커넥터가 직접 채워 보낸다)
                    from session_manager import appconfig
                    await self._res(rid, True, data={"id": dev["id"], "name": dev["name"],
                                                     "ver": CLIENT_VERSION,
                                                     "hostname": appconfig.machine_name()})
                else:
                    await self._res(rid, False, error="auth_invalid")
                return

            # 인증 강제(enforced) 시: 미인증/폐기 기기의 모든 메서드(ping 제외) 거부.
            # 등록 기기 0개면 아무도 통과 못 함 = 외부 전면 차단.
            if devices.enforced() and method != "ping":
                dev = self.authed.get(cid)
                stale = bool(dev) and devices.token_version(dev["id"]) != dev.get("tver")
                if not dev or not devices.is_active(dev["id"]) or stale:
                    self.authed.pop(cid, None)
                    await self._res(rid, False, error="auth_required")
                    return

            if method == "ping":
                await self.send({"v": 1, "type": "pong", "id": rid,
                                 **({"cid": cid} if cid is not None else {})})

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
                await self._handle_api(rid, params)

            elif method == "resume":
                await self._start_resume(rid, params)

            elif method == "terminal":
                # 웹터미널(/ws/terminal) 원시 스트림 터널링 (외부에서 진짜 xterm)
                await self._start_terminal(rid, params)

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
    async def _handle_api(self, rid: str, params: dict) -> None:
        verb = (params.get("verb") or "GET").upper()
        path = params.get("path") or ""
        # 안전장치: /api/ 로 시작하는 경로만 허용(임의 경로 프록시 금지)
        if not path.startswith("/api/") or ".." in path:
            await self._res(rid, False, error="path_not_allowed")
            return
        url = f"{self.local_base}{path}"
        query = params.get("query") or {}
        body = params.get("body")
        if verb == "GET":
            r = await self.http.get(url, params=query)
        elif verb == "POST":
            r = await self.http.post(url, params=query, json=body)
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
        local_ws = f"{_to_ws(self.local_base)}/api/v1/{ep}/{sid}?skip={skip}"
        self.stream_in[rid] = asyncio.Queue()
        self.streams[rid] = asyncio.create_task(self._pipe_resume(rid, local_ws))

    async def _pipe_resume(self, rid: str, local_ws: str) -> None:
        try:
            async with connect(local_ws, max_size=None) as lws:
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
        self.stream_in[rid] = asyncio.Queue()
        self.streams[rid] = asyncio.create_task(self._pipe_terminal(rid, local_ws))

    async def _pipe_terminal(self, rid: str, local_ws: str) -> None:
        try:
            async with connect(local_ws, max_size=None) as lws:
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

    async def _handle_stream_in(self, frame: dict) -> None:
        rid = frame.get("id")
        q = self.stream_in.get(rid)
        if q is not None:
            await q.put(frame.get("data"))

    # ---- 프레임 라우팅 ----
    async def _on_frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        t = frame.get("type")
        if t == "req":
            await self._handle_req(frame)
        elif t == "stream_in":
            await self._handle_stream_in(frame)
        elif t == "peer":
            # 기기 연결 종료 → 인증 상태 정리
            if frame.get("event") == "client_offline":
                self.authed.pop(frame.get("cid"), None)
        elif t == "ping":
            await self.send({"v": 1, "type": "pong", "id": frame.get("id")})
        # 그 외(hello 등)는 무시

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

    # ---- 접속 + 재연결 루프 ----
    async def run(self) -> None:
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
                async with connect(uri, max_size=None) as ws:
                    self.ws = ws
                    backoff = 1
                    _log(f"[connector] relay 연결됨 room={self.room} mode={mode} "
                         f"-> 로컬 {self.local_base}")
                    async for raw in ws:
                        await self._on_frame(raw)
            except Exception as e:  # noqa: BLE001
                print(f"[connector] 연결 끊김: {type(e).__name__}: {e} "
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
