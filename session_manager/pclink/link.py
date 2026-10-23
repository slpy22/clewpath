"""짝 채널 — 상대 Host 의 릴레이 방에 client 로 붙는 연결(전송 층, 문서 1절·7절).

A 는 **물을 때만** B 의 방에 client 로 붙는다(D7). 첫 call 때 붙고(게으르게), 마지막 사용 뒤 idle_s(기본 60초) 동안 다시 쓰다가
LinkPool.sweep 이 닫는다. 붙는 길은 폰(PWA)과 같다 — CP `POST {cp}/client/token` 으로 단기 JWT 를 받고
`{relay}?role=client&room=<방>&v=1&jwt=<토큰>` 로 웹소켓을 연다. 릴레이(원격)로 나가는 길은 커넥터처럼 사용자의 프록시를 따른다.

이 층은 봉투의 뜻도 짝 정책도 모른다(policy·protocol 을 import 하지 않는다). 한 req 에 오는 res 를 차례로 on_res 에 넘길
뿐이고, 봉투 해석·끝 판단은 호출자가 한다. 비밀값(cred_secret·jwt)은 로그·예외 문구에 싣지 않는다 — 웹소켓·httpx 예외 문구에는
주소(쿼리스트링의 jwt)가 들어갈 수 있어, 예외는 종류 이름만 적고 원인 사슬을 끊는다(`from None`).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from session_manager import logredact

CODES = ("peer_offline", "timeout", "auth", "peer_outdated", "link_error")

# CP 토큰 캐시 — (cp, 자격 id) → (토큰, 만료 epoch 초). 만료 5분 전까지 쓴다(폰과 같은 여유). 프로세스 메모리.
# 연결(PeerLink)이 아니라 모듈에 두어, 쉬다 닫힌 연결을 pool 이 새로 만들어도 CP 를 다시 부르지 않는다.
TOKEN_MARGIN_S = 300.0
_TOKENS: dict[tuple[str, str], tuple[str, float]] = {}

# 릴레이가 자격을 거절할 때의 닫기 코드(relay.py): 4401 공유 토큰 없음·틀림, 4402 JWT 거절, 4403 방 모름·회수됨
_RELAY_AUTH_CODES = (4401, 4402, 4403)
_RELAY_BAD_REQUEST = 4400

# 상대 방 주소 쿼리에 jwt 가 실린다 — websockets DEBUG 로그(요청 줄)에 남지 않게(멱등)
logredact.install()


def _log(msg: str) -> None:
    """콘솔 인코딩에 없는 글자로 로그가 기능을 깨뜨리지 않게(커넥터 _log 와 같은 이유 — T7 사고)."""
    try:
        print(msg)
    except Exception:  # noqa: BLE001
        try:
            enc = sys.stdout.encoding or "ascii"
            print(msg.encode(enc, "replace").decode(enc, "replace"))
        except Exception:  # noqa: BLE001
            pass


class LinkError(Exception):
    """짝 채널 실패. code 는 CODES 중 하나 — 문구에는 비밀값을 싣지 않는다."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code if code in CODES else "link_error"
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class _RelayAuth(Exception):
    """릴레이가 JWT·방을 거절했다(내부용 — 캐시 토큰이면 한 번 새로 받아 다시 붙는다)."""


def _short(room: str) -> str:
    return (room[:8] + "…") if len(room) > 8 else room


class PeerLink:
    """상대 Host 방 하나에 대한 client 연결. 한 연결에 call 여러 개가 동시에 돈다(읽기 작업 하나가 바깥 id 로 나눠 준다).

    연결이 끊기거나 close() 되면 closed 가 되고 다시 쓰지 않는다 — LinkPool.get 이 다음에 새로 만든다.
    읽기용: last_used(time.monotonic), inflight(진행 중 call 수), closed, cid·peer_present(릴레이 hello 값).
    """

    def __init__(self, *, relay: str, cp: str, room: str, cred_id: str, cred_secret: str,
                 connect_timeout: float = 10.0, http: "httpx.AsyncClient | None" = None):
        self.relay = relay
        self.cp = cp.rstrip("/")
        self.room = room
        self.cred_id = cred_id
        self._secret = cred_secret
        self.connect_timeout = connect_timeout
        self._http = http
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._connecting: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Queue] = {}
        self._seq = 0
        self._inflight = 0
        self.last_used = time.monotonic()
        self.closed = False
        self.cid: str | None = None
        # 릴레이 hello 의 peer_present(상대 Host 가 방에 있나). 옛 릴레이처럼 없으면 None — 그땐 보내 보고 릴레이 답을 따른다
        self.peer_present: bool | None = None

    def __repr__(self) -> str:   # 비밀값을 싣지 않는다
        return f"<PeerLink room={_short(self.room)} closed={self.closed} inflight={self._inflight}>"

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self.closed

    # ---- 묻기 ----
    async def call(self, method: str, params: dict, *, on_res: Callable[[dict], Any], timeout: float) -> None:
        """{"v":1,"type":"req","id":<새 id>,"method":method,"params":params} 를 보내고, 그 id 로 오는 res 마다 on_res(res) 를 부른다.

        on_res 가 참을 돌려주면 끝(코루틴 함수여도 된다). ok:false 중 'unknown_method:…' 는 LinkError('peer_outdated'),
        'agent_offline' 은 LinkError('peer_offline') 로 바꾸고, 그 밖의 ok:false 는 on_res 에 그대로 넘긴다.
        시간은 둘로 나뉜다 — 붙기(CP 토큰 + 릴레이 + hello)는 connect_timeout 안(넘으면 peer_offline), 보낸 뒤 끝까지는
        timeout 안(넘으면 LinkError('timeout') — 상대에 req 가 닿았을 수 있으니 호출자가 봉투 id 로 poll 한다).
        연결이 끊기거나 상대 Host 가 방을 떠나면(agent_offline 통지) 진행 중인 call 은 모두 LinkError('peer_offline').
        """
        if self.closed:
            raise LinkError("peer_offline", "연결이 이미 닫혔습니다")
        if not isinstance(params, dict):
            raise LinkError("link_error", "params 는 dict 여야 합니다")
        self._inflight += 1
        self.last_used = time.monotonic()
        try:
            await self._ensure()
            if self.peer_present is False:
                raise LinkError("peer_offline", "상대 Host 가 방에 없습니다")
            self._seq += 1
            rid = f"pl{self._seq}"
            q: asyncio.Queue = asyncio.Queue()
            self._pending[rid] = q
            try:
                await self._send({"v": 1, "type": "req", "id": rid, "method": method, "params": params})
                try:
                    await asyncio.wait_for(self._drain(q, on_res), timeout)
                except asyncio.TimeoutError:
                    raise LinkError("timeout", f"{timeout:g}초 안에 끝나지 않았습니다") from None
            finally:
                self._pending.pop(rid, None)
        finally:
            self._inflight -= 1
            self.last_used = time.monotonic()

    async def _drain(self, q: asyncio.Queue, on_res) -> None:
        while True:
            item = await q.get()
            if isinstance(item, LinkError):
                raise LinkError(item.code, str(item).partition(": ")[2])
            if item.get("ok") is False:
                err = str(item.get("error") or "")
                if err.startswith("unknown_method:"):
                    raise LinkError("peer_outdated", "상대 Host 가 이 요청을 모릅니다")
                if err == "agent_offline":
                    raise LinkError("peer_offline", "상대 Host 가 방에 없습니다")
            done = on_res(item)
            if inspect.isawaitable(done):
                done = await done
            if done:
                return

    async def _send(self, frame: dict) -> None:
        ws = self._ws
        if ws is None or self.closed:
            raise LinkError("peer_offline", "연결이 끊겼습니다")
        try:
            async with self._send_lock:
                await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            raise LinkError("peer_offline", f"보내기 실패({type(e).__name__})") from None

    # ---- 붙기 ----
    async def _ensure(self) -> None:
        """붙어 있지 않으면 붙는다. 동시에 부른 call 들은 붙기 한 번을 같이 기다린다(한 호출이 취소돼도 붙기는 계속)."""
        if self._ws is not None:
            return
        t = self._connecting
        if t is None or t.done():
            t = asyncio.ensure_future(self._connect())
            t.add_done_callback(lambda f: f.cancelled() or f.exception())   # 기다리던 호출이 모두 취소돼도 경고 없게
            self._connecting = t
        await asyncio.wait([t])
        if self._connecting is t:
            self._connecting = None
        if t.cancelled() or self.closed:
            raise LinkError("peer_offline", "연결이 닫혔습니다")
        exc = t.exception()
        if exc is not None:
            if isinstance(exc, LinkError):
                raise LinkError(exc.code, str(exc).partition(": ")[2]) from None
            raise LinkError("link_error", f"붙기 실패({type(exc).__name__})") from None

    async def _connect(self) -> None:
        token, cached = await self._token()
        try:
            ws, hello = await self._open(token)
        except _RelayAuth:
            _TOKENS.pop((self.cp, self.cred_id), None)
            if not cached:
                _log(f"[pclink] 상대 방이 자격을 거절 room={_short(self.room)}")
                raise LinkError("auth", "릴레이가 자격을 거절했습니다") from None
            token, _ = await self._token(force=True)      # 캐시한 토큰이 거절됐다 — 새로 받아 한 번만 더
            try:
                ws, hello = await self._open(token)
            except _RelayAuth:
                _TOKENS.pop((self.cp, self.cred_id), None)
                _log(f"[pclink] 상대 방이 자격을 거절 room={_short(self.room)}")
                raise LinkError("auth", "릴레이가 자격을 거절했습니다") from None
        if self.closed:                                    # 붙는 사이 close() 됐다
            await _quiet_close(ws)
            raise LinkError("peer_offline", "연결이 닫혔습니다")
        self._ws = ws
        self.cid = hello.get("cid")
        pp = hello.get("peer_present")
        self.peer_present = pp if isinstance(pp, bool) else None
        self._reader = asyncio.create_task(self._read(ws))
        _log(f"[pclink] 상대 방 연결됨 room={_short(self.room)} 상대 Host="
             f"{'있음' if self.peer_present else ('없음' if self.peer_present is False else '모름')}")

    async def _token(self, force: bool = False) -> tuple[str, bool]:
        """CP 에서 client JWT 를 받는다(만료 5분 전까지 캐시). 돌려주는 값: (토큰, 캐시에서 왔나)."""
        key = (self.cp, self.cred_id)
        if not force:
            c = _TOKENS.get(key)
            if c and c[1] - TOKEN_MARGIN_S > time.time():
                return c[0], True
        body = {"public_id": self.cred_id, "secret": self._secret}
        url = f"{self.cp}/client/token"
        try:
            if self._http is not None:
                r = await self._http.post(url, json=body, timeout=self.connect_timeout)
            else:
                async with httpx.AsyncClient(timeout=self.connect_timeout) as c:
                    r = await c.post(url, json=body)
        except Exception as e:  # noqa: BLE001
            _log(f"[pclink] CP 토큰 받기 실패({type(e).__name__})")
            raise LinkError("peer_offline", f"CP 에 닿지 못했습니다({type(e).__name__})") from None
        if r.status_code in (401, 403):
            _TOKENS.pop(key, None)
            _log(f"[pclink] CP 가 짝 자격을 거절({r.status_code}) — 폐기됐을 수 있음")
            raise LinkError("auth", f"CP 가 자격을 거절했습니다({r.status_code})")
        if r.status_code >= 400:
            raise LinkError("peer_offline", f"CP 응답 {r.status_code}")
        try:
            j = r.json()
            token = j["token"]
            if not isinstance(token, str) or not token:
                raise ValueError
            exp = float(j.get("expires_at") or 0)
        except Exception:  # noqa: BLE001
            raise LinkError("link_error", "CP 토큰 응답 형식이 다릅니다") from None
        if exp - TOKEN_MARGIN_S > time.time():
            _TOKENS[key] = (token, exp)
        return token, False

    async def _open(self, token: str):
        """릴레이에 client 로 붙고 hello 를 받는다(connect_timeout 안). 돌려주는 값: (웹소켓, hello)."""
        q = urlencode({"role": "client", "room": self.room, "v": 1, "jwt": token})
        url = f"{self.relay}{'&' if '?' in self.relay else '?'}{q}"
        ws = None
        ok = False
        try:
            async with asyncio.timeout(self.connect_timeout):
                ws = await connect(url, max_size=None, open_timeout=None)
                while True:
                    m = _parse(await ws.recv())
                    if m is not None and m.get("type") == "hello":
                        ok = True
                        return ws, m
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd is not None else None
            if code in _RELAY_AUTH_CODES:
                raise _RelayAuth() from None
            _log(f"[pclink] 상대 방 연결 실패(닫힘 {code}) room={_short(self.room)}")
            if code == _RELAY_BAD_REQUEST:
                raise LinkError("link_error", "릴레이가 요청을 거절했습니다(4400)") from None
            raise LinkError("peer_offline", f"릴레이가 연결을 닫았습니다({code})") from None
        except InvalidStatus as e:
            st = e.response.status_code
            if st in (401, 403):
                raise _RelayAuth() from None
            _log(f"[pclink] 상대 방 연결 실패(HTTP {st}) room={_short(self.room)}")
            raise LinkError("peer_offline", f"릴레이 응답 {st}") from None
        except (TimeoutError, asyncio.TimeoutError):
            _log(f"[pclink] 상대 방 연결 시간 초과 room={_short(self.room)}")
            raise LinkError("peer_offline", "릴레이 연결 시간 초과") from None
        except Exception as e:  # noqa: BLE001 — OSError·InvalidURI·InvalidHandshake 등. 문구에 주소(jwt)가 들어 있을 수 있다
            _log(f"[pclink] 상대 방 연결 실패({type(e).__name__}) room={_short(self.room)}")
            raise LinkError("peer_offline", f"릴레이에 닿지 못했습니다({type(e).__name__})") from None
        finally:
            if not ok:          # 실패·취소(붙는 중 close) 모두 — 열린 소켓을 남기지 않는다
                await _quiet_close(ws)

    # ---- 받기 ----
    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                self._on_frame(raw)
        except Exception:  # noqa: BLE001 — 끊김은 아래에서 한 가지로 처리
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            if not self.closed:
                self.closed = True
                _log(f"[pclink] 상대 방 연결 끊김 room={_short(self.room)}")
            self._fail_all("연결이 끊겼습니다")

    def _on_frame(self, raw) -> None:
        m = _parse(raw)
        if m is None:
            return
        t = m.get("type")
        if t == "res":
            cid = m.get("cid")
            if cid is None:
                # cid 없는 res: 릴레이가 직접 보낸 실패(상대 Host 없음·전달 실패)만 본다. 나머지는 상대 Host 의 방 전체 broadcast — 무시
                err = m.get("error")
                if m.get("ok") is not False or err not in ("agent_offline", "agent_send_failed"):
                    return
                self.peer_present = False
                q = self._pending.get(m.get("id")) if isinstance(m.get("id"), str) else None
                if q is not None and err == "agent_offline":
                    q.put_nowait(m)
                else:                                         # 어느 call 인지 모른다 — 상대가 떠난 것과 같게
                    self._fail_all("상대 Host 에 전하지 못했습니다")
                return
            if self.cid is not None and cid != self.cid:      # 다른 기기 앞(릴레이가 모르는 cid 를 방 전체에 뿌린 것)
                return
            rid = m.get("id")
            q = self._pending.get(rid) if isinstance(rid, str) else None
            if q is not None:
                q.put_nowait(m)
            return
        if t == "peer" and m.get("cid") is None:
            ev = m.get("event")
            if ev == "agent_offline":
                self.peer_present = False
                self._fail_all("상대 Host 가 방을 떠났습니다")
            elif ev == "agent_online":
                self.peer_present = True
        # hello 재전송·stream·notice·모르는 type 은 무시

    def _fail_all(self, why: str) -> None:
        for q in list(self._pending.values()):
            q.put_nowait(LinkError("peer_offline", why))

    # ---- 닫기 ----
    async def close(self) -> None:
        """즉시 닫는다. 진행 중인 call 은 LinkError('peer_offline'), 붙는 중이면 그만둔다. 여러 번 불러도 된다."""
        self.closed = True
        t = self._connecting
        if t is not None and not t.done():
            t.cancel()
        self._fail_all("연결을 닫았습니다")
        ws, self._ws = self._ws, None
        await _quiet_close(ws)
        r = self._reader
        if r is not None and r is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(r), 2)
            except Exception:  # noqa: BLE001
                r.cancel()


async def _quiet_close(ws) -> None:
    if ws is None:
        return
    try:
        await asyncio.wait_for(ws.close(), 2)
    except Exception:  # noqa: BLE001
        pass


def _parse(raw) -> dict | None:
    try:
        m = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return m if isinstance(m, dict) else None


class LinkPool:
    """짝(key)마다 PeerLink 하나. 살아 있으면 다시 쓰고, idle_s 넘게 안 쓰고 진행 중인 call 이 없으면 sweep 이 닫는다."""

    sweep_every = 30.0   # start() 의 백그라운드 sweep 간격(초)

    def __init__(self, idle_s: float = 60.0):
        self.idle_s = idle_s
        self._links: dict[str, PeerLink] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None

    def __len__(self) -> int:
        return len(self._links)

    async def get(self, key: str, factory: Callable[[], "PeerLink | Awaitable[PeerLink]"]) -> PeerLink:
        """살아 있는(closed 아닌) 연결이면 그것을, 아니면 factory() 로 새로 만든다. 동시에 불려도 하나만 만든다."""
        link = self._links.get(key)
        if link is not None and not link.closed:
            return link
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            link = self._links.get(key)
            if link is not None and not link.closed:
                return link
            link = factory()
            if inspect.isawaitable(link):
                link = await link
            self._links[key] = link
            return link

    async def drop(self, key: str) -> None:
        """짝 해제 — 그 연결을 즉시 닫는다(진행 중인 call 은 peer_offline)."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            link = self._links.pop(key, None)
        if link is not None:
            await link.close()

    async def sweep(self) -> int:
        """idle_s 넘게 안 쓰고 inflight 0 인 연결을 닫는다. 이미 끊긴 것은 목록에서만 뺀다. 돌려주는 값: 닫은 수."""
        now = time.monotonic()
        n = 0
        for key, link in list(self._links.items()):
            if self._links.get(key) is not link:
                continue
            if link.closed:
                del self._links[key]
                continue
            if link.inflight == 0 and now - link.last_used > self.idle_s:
                del self._links[key]
                await link.close()      # close 는 첫 await 전에 closed 를 세운다 — 그 사이 call 이 끼어들 틈이 없다
                n += 1
        if n:
            _log(f"[pclink] 쉬는 짝 연결 {n}개 닫음")
        return n

    def start(self) -> None:
        """sweep_every 초마다 sweep 하는 백그라운드 작업을 띄운다(이미 돌면 그대로)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_every)
            try:
                await self.sweep()
            except Exception as e:  # noqa: BLE001
                _log(f"[pclink] sweep 실패({type(e).__name__})")

    async def stop(self) -> None:
        """백그라운드 작업을 멈추고 남은 연결을 모두 닫는다(Host 종료용)."""
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            await asyncio.wait([t])
        links, self._links = list(self._links.values()), {}
        for link in links:
            await link.close()
