"""pclink 두 Host 실 e2e 시험 장치 — 로컬 CP·릴레이·Host 두 개를 각각 별도 프로세스로 띄우고 거둔다.

운영과의 격리(2026-10-23 사고 뒤 규칙):
- 자식 프로세스(CP·릴레이·Host)의 env 는 **새로 짠다**: 부모 env 에서 SM_·RELAY_·CLAUDE*(CLAUDE_CODE_·CLAUDECODE 포함)·CLEWPATH_·
  SESSION_MANAGER_ 로 시작하는 것을 모두 지운 뒤 필요한 것만 넣는다. 띄우기 전에 운영 주소(pyongso, 운영 CP·릴레이의 로컬 포트
  5200·8787)가 env 어디에도 없는지 확인한다(있으면 띄우지 않고 실패).
- CP·릴레이는 007 서버 코드를 **그 폴더에서 읽기만** 하며 실행한다(cwd=007, 바이트코드 안 씀). DB·내부 토큰·관리 토큰·모델 파일은
  모두 tmp 다. 운영 컨테이너·네트워크·볼륨은 쓰지 않는다(로컬 파이썬 프로세스).
- Host 는 가짜 홈(SESSION_MANAGER_CLAUDE_HOME=tmp)·자기 포트·로컬 릴레이/CP 로 뜨고, 설정 파일로 자동 업데이트 확인과 Claude 훅
  등록을 끈다. 기계 전체를 훑는 기동 청소 두 개(고아 웹재개 스트림·구버전 PTY 잔재)는 이 시험과 무관하고 사용자의 다른
  프로세스에 닿을 수 있어 감싸는 진입점에서 끈다 — pclink·커넥터·승인·2차 인증 경로는 그대로다.
- 끝나면(실패해도) 자식 프로세스 트리를 모두 죽인다(atexit 안전망 포함).

로그에는 비밀값(토큰·비밀·초대 문자열)을 싣지 않는다. 실패 때 보여주는 로그 꼬리도 가린다(redact).
"""
from __future__ import annotations

import atexit
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT_006 = Path(__file__).resolve().parents[2]

# 지우는 env 접두(대문자 비교). CLAUDE 는 CLAUDE_CODE_*·CLAUDECODE·CLAUDE_PID·CLAUDE_CONFIG_DIR 를 모두 덮는다 —
# 묻기 복제본(claude)이 이 시험을 돌린 Claude 세션의 자식으로 보이거나 그 메시지 소켓에 붙지 않게.
STRIP_PREFIXES = ("SM_", "RELAY_", "CLAUDE", "CLEWPATH_", "SESSION_MANAGER_", "_SM_")
_PROD = re.compile(r"pyongso|(?:127\.0\.0\.1|localhost|\[::1\]):(?:5200|8787)(?!\d)", re.I)
_AVOID_PORTS = {5100, 5101, 5102, 5103, 5104, 5105, 5200, 8787}

_ALL: list["Proc"] = []
_HTTP: httpx.Client | None = None


def http() -> httpx.Client:
    """시험 쪽 HTTP 클라이언트 하나(keep-alive). 호출마다 새로 만들면 Windows 에서 인증서 읽기로 0.3초씩 든다."""
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(timeout=30)
    return _HTTP


def find_007() -> Path | None:
    """007 서버 코드 위치: SM_E2E_007_DIR → 006 옆의 007_clewpath_server. control_plane·relay 가 있어야 한다."""
    cands = []
    if os.environ.get("SM_E2E_007_DIR"):
        cands.append(Path(os.environ["SM_E2E_007_DIR"]))
    cands.append(ROOT_006.parent / "007_clewpath_server")
    for c in cands:
        if (c / "control_plane" / "api.py").is_file() and (c / "relay" / "relay.py").is_file():
            return c
    return None


def free_port() -> int:
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p not in _AVOID_PORTS:
            return p
    raise RuntimeError("빈 포트를 찾지 못했습니다")


def assert_no_prod(env: dict) -> None:
    bad = sorted(k for k, v in env.items() if isinstance(v, str) and _PROD.search(v))
    if bad:
        raise AssertionError(f"자식 env 에 운영 주소가 남아 있습니다(띄우지 않음): {', '.join(bad)}")


def child_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES)}
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    env.update({k: str(v) for k, v in extra.items()})
    assert_no_prod(env)
    return env


_REDACT = [
    (re.compile(r"(jwt|token|secret|otp|grace)=[^&\s\"']{12,}", re.I), r"\1=<가림>"),
    (re.compile(r"(\"(?:secret|token|client_secret|invite|k|otp)\"\s*:\s*\")[^\"]+"), r"\1<가림>"),
    (re.compile(r"clewpath-pair:0:[A-Za-z0-9_\-]+"), "clewpath-pair:0:<가림>"),
]


def redact(s: str) -> str:
    for rx, rep in _REDACT:
        s = rx.sub(rep, s)
    return s


class Proc:
    """자식 프로세스 하나. 표준출력·오류는 로그 파일로. stop() 은 자식 트리까지 죽인다."""

    def __init__(self, name: str, argv: list[str], *, cwd: Path, env: dict, log: Path):
        self.name, self.log_path = name, log
        assert_no_prod(env)
        self._log = open(log, "wb")
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        self.p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=self._log,
                                  stderr=subprocess.STDOUT, creationflags=flags)
        _ALL.append(self)

    @property
    def pid(self) -> int:
        return self.p.pid

    def text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def tail(self, n: int = 60) -> str:
        return redact("\n".join(self.text().splitlines()[-n:]))

    def wait_log(self, needle: str, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.text():
                return
            if self.p.poll() is not None:
                raise AssertionError(f"[{self.name}] '{needle}' 전에 종료됨(코드 {self.p.returncode})\n{self.tail()}")
            time.sleep(0.25)
        raise AssertionError(f"[{self.name}] {timeout:g}초 안에 '{needle}' 이 안 보임\n{self.tail()}")

    def stop(self) -> None:
        try:
            import psutil
            try:
                kids = psutil.Process(self.p.pid).children(recursive=True)
            except Exception:  # noqa: BLE001
                kids = []
            for k in kids:
                try:
                    k.kill()
                except Exception:  # noqa: BLE001
                    pass
        except ImportError:
            pass
        try:
            self.p.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._log.close()
        except Exception:  # noqa: BLE001
            pass
        if self in _ALL:
            _ALL.remove(self)


def stop_all() -> None:
    global _HTTP
    for p in list(reversed(_ALL)):
        p.stop()
    if _HTTP is not None:
        try:
            _HTTP.close()
        except Exception:  # noqa: BLE001
            pass
        _HTTP = None


atexit.register(stop_all)


def wait_http(url: str, timeout: float, ok=lambda r: r.status_code == 200, proc: Proc | None = None) -> httpx.Response:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        if proc is not None and proc.p.poll() is not None:
            raise AssertionError(f"[{proc.name}] 기동 중 종료됨(코드 {proc.p.returncode})\n{proc.tail()}")
        try:
            r = http().get(url, timeout=3)
            if ok(r):
                return r
            last = r.status_code
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        time.sleep(0.3)
    raise AssertionError(f"{timeout:g}초 안에 준비되지 않음({last})" + (f"\n{proc.tail()}" if proc else ""))


# ---------------------------------------------------------------- CP·릴레이(007)

class Cloud:
    """로컬 CP + 릴레이. 운영과 같은 검증(JWT·room DB·내부 토큰)을 켠다."""

    def __init__(self, base: Path, root_007: Path):
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.cp_port, self.relay_port = free_port(), free_port()
        self.cp_url = f"http://127.0.0.1:{self.cp_port}"
        self.relay_url = f"ws://127.0.0.1:{self.relay_port}/ws"
        internal = secrets.token_hex(24)            # 48자 — 32자 하한 위
        self.admin = secrets.token_hex(16)
        nofile = base / "no-such-file"               # 일부러 없는 파일: 기본 경로(007/relay/.internal-token 등) 대신 env 만 쓰게
        py = sys.executable
        self.cp = Proc("cp", [py, "-m", "control_plane.api"], cwd=root_007, log=base / "cp.log", env=child_env({
            "SM_CP_HOST": "127.0.0.1", "SM_CP_PORT": self.cp_port,
            "SM_CP_DB_URL": "sqlite:///" + str(base / "cp.db").replace("\\", "/"),
            "SM_CP_DATA_DIR": base / "cp-data", "SM_CP_INTERNAL_TOKEN": internal,
            "SM_CP_INTERNAL_TOKEN_FILE": nofile, "SM_CP_ADMIN_TOKEN": self.admin,
            "SM_CP_ANON_DAILY": "50", "SM_CP_JANITOR": "0", "SM_CP_JWT_TTL": "600",
        }))
        wait_http(f"{self.cp_url}/healthz", 30, proc=self.cp)
        self.relay = Proc("relay", [py, "-m", "relay.relay"], cwd=root_007, log=base / "relay.log", env=child_env({
            "RELAY_HOST": "127.0.0.1", "RELAY_PORT": self.relay_port,
            "RELAY_AGENT_TOKEN": secrets.token_hex(16), "RELAY_CLIENT_TOKEN": secrets.token_hex(16),
            "RELAY_CP_URL": self.cp_url, "RELAY_JWT_VERIFY": "1", "RELAY_DB_VERIFY": "1",
            "RELAY_INTERNAL_TOKEN": internal, "RELAY_INTERNAL_TOKEN_FILE": nofile,
            "RELAY_VERIFY_FAIL_OPEN_FILE": nofile, "RELAY_DOWN_GRACE_S": "600",
        }))
        self.relay.wait_log("verify jwt=True db=True internal_token=on fail_open=off", 30)

    def client_token_status(self, cred_id: str, secret: str) -> int:
        """기기 자격(cpub) → client JWT 교환의 HTTP 상태(200 = 살아 있음, 401 = 폐기·없음)."""
        return http().post(f"{self.cp_url}/client/token", json={"public_id": cred_id, "secret": secret}, timeout=10).status_code


# ---------------------------------------------------------------- Host

# 기계 전체 기동 청소 두 개만 끄고 진짜 server.main() 을 돈다(모듈 docstring).
_HOST_ENTRY = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from session_manager import webapi, webterm\n"
    "webapi.sweep_orphan_streams = lambda: 0\n"
    "webterm.sweep_legacy_orphans = lambda: 0\n"
    "from session_manager import server\n"
    "server.main()\n"
)

_HOST_TOML = """# pclink e2e 시험용 — 외부로 나가는 자동 동작을 끈다
[update]
auto_check = false
auto_apply = false
apply_on_restart = false

[hooks]
auto_register = false
"""


class Host:
    def __init__(self, name: str, base: Path, cloud: Cloud):
        self.name, self.base = name, base
        self.home = base / ".claude"
        (self.home / "projects").mkdir(parents=True, exist_ok=True)
        (self.home / "session_manager").mkdir(parents=True, exist_ok=True)
        (self.home / "session_manager" / "clewpath.toml").write_text(_HOST_TOML, encoding="utf-8")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.cloud = cloud
        self.proc: Proc | None = None
        self.totp_secret: str | None = None
        self._used_steps: set[int] = set()

    def start(self) -> None:
        self.proc = Proc(f"host-{self.name}", [sys.executable, "-c", _HOST_ENTRY, str(ROOT_006)], cwd=ROOT_006,
                         log=self.base / "host.log", env=child_env({
                             "SESSION_MANAGER_CLAUDE_HOME": self.home, "SM_HOST": "127.0.0.1", "SM_PORT": self.port,
                             "SM_RELAY_URL": self.cloud.relay_url, "SM_CP_URL": self.cloud.cp_url,
                             "SM_PTY_BROKER": "off", "COMPUTERNAME": f"E2E-{self.name}",
                         }))

    def wait_ready(self, timeout: float = 90) -> dict:
        assert self.proc is not None
        self.proc.wait_log("relay 연결됨", timeout)
        r = wait_http(f"{self.url}/api/owner/pclink", 30, ok=lambda r: r.status_code == 200 and r.json().get("room_ready"),
                      proc=self.proc)
        return r.json()

    # ---- HTTP ----
    def get(self, path: str, **kw) -> httpx.Response:
        return http().get(self.url + path, timeout=kw.pop("timeout", 30), **kw)

    def post(self, path: str, body: dict | None = None, **kw) -> httpx.Response:
        return http().post(self.url + path, json=body if body is not None else {}, timeout=kw.pop("timeout", 30), **kw)

    # ---- 2차 인증 ----
    def enable_2fa(self) -> None:
        r = self.post("/api/owner/2fa/provision")
        assert r.status_code == 200, f"[{self.name}] 2FA provision {r.status_code}"
        self.totp_secret = r.json()["secret"]                # 메모리에만 — 출력하지 않는다
        st = self.get("/api/owner/2fa/status").json()
        assert st == {"required": True, "has_secret": True}, st

    def otp(self) -> str:
        """한 번 쓴 코드는 다시 쓰지 않는다(지금 Host 는 재사용을 받아 주지만 RFC 6238 5.2 대로 막게 바뀌어도 이 시험이 돌게).
        지금 창 → 다음 창(검증이 ±1 창을 받는다) 순으로 고르고, 둘 다 썼으면 다음 창까지 기다린다."""
        from session_manager import owner2fa
        assert self.totp_secret
        while True:
            now = int(time.time()) // 30
            for step in (now, now + 1):
                if step not in self._used_steps:
                    self._used_steps.add(step)
                    return owner2fa._hotp(self.totp_secret, step)
            time.sleep(30 - time.time() % 30 + 0.5)

    def decide(self, aid: str, timeout: float = 30) -> dict:
        r = self.post(f"/api/owner/approvals/{aid}/decide", {"approve": True, "otp": self.otp()}, timeout=timeout)
        assert r.status_code == 200, f"[{self.name}] 승인 결정 {r.status_code}: {redact(r.text)[:300]}"
        return r.json()

    def tail(self, n: int = 60) -> str:
        return self.proc.tail(n) if self.proc else ""
