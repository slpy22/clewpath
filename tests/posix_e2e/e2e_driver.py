"""맥·리눅스 설치 e2e 드라이버 — docs/designs/posix-install.md 6.3. e2e 컨테이너 안(tester, --network none)에서만 돈다.

가짜 CP(fake_cp.py, 127.0.0.1:8099)가 시험 키로 서명한 매니페스트와 변형 zip 을 준다. 드라이버는 실제 get.sh → install.sh →
런처 → Host → 업데이트 러너 → 롤백 → uninstall.sh 를 돌리고, 단계마다 성공·실패와 근거를 모은다. --rm 이라 컨테이너가 남지
않으므로 결과는 표준출력으로만 낸다: 사람이 읽는 줄, 실패하면 로그 꼬리, 마지막 줄 `E2E-RESULT {json}`.

  S0 정적(dash -n·bash --posix -n)   S1 get.sh 거부   S2 설치(A)   S3 재실행 멱등   S4 런처 ①~⑪(실제 Host)
  S5 수동 적용(B)   S6 실패와 롤백(C)   S7 재기동 자동 적용(D)   S9 bash --posix 런처·러너(E)   S8 제거·다시 설치·--purge
  (S9 는 설치본이 있어야 해서 S8 앞에 돈다)

실제 ~/.local/share·~/.claude·5100 에 설치하므로 CLEWPATH_E2E_CONTAINER=1 인 컨테이너가 아니면 아무것도 하지 않는다.
표준 라이브러리만 쓴다(설치본의 venv 파이썬은 Host 쪽 판정을 그대로 확인할 때만 부른다).
"""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

E2E = Path(__file__).resolve().parent              # /opt/e2e
FIX = E2E / "fixtures"
HOME = Path.home()
CH = HOME / ".local" / "share" / "clewpath"        # CLEWPATH_HOME 기본(리눅스)
APP = CH / "app"
VPY = APP / ".venv" / "bin" / "python"
VENV_BIN = str(APP / ".venv" / "bin") + "/"
LAUNCHER = APP / "start-connector.sh"
CLAUDE = HOME / ".claude"
PROJECTS = CLAUDE / "projects"
SETTINGS = CLAUDE / "settings.json"
DATA = CLAUDE / "session_manager"
CONF = DATA / "clewpath.toml"
CREDS = DATA / "cp_credentials.json"
WORK = DATA / "updates"
OUT = HOME / "e2e-out"                             # 명령 출력(실패하면 꼬리를 표준출력으로)
STATE = HOME / "fake-cp-state"
UMASK = 0o027                                      # 기본값(022·077)과 다른 값 — Host 가 물려받는지 본다(S38)
RELAY = "ws://127.0.0.1:9/relay/ws"                # 닫힌 포트 — 커넥터는 붙지 못하고 fail-open 으로 계속 돈다
APP_URL = "http://127.0.0.1:9/app"
DEFAULT_PORT = 5100
RESULT_TAG = "E2E-RESULT "

IX: dict = {}                                      # fixtures/index.json
CP = ""
RESULTS: list[dict] = []
TIMES: dict[str, float] = {}
CUR = {"step": "-"}
KEEP: dict = {}                                    # 단계 사이에 넘기는 값(브로커 pid 등)


class Abort(Exception):
    """이어 갈 수 없는 실패 — 뒤 단계는 앞 단계의 설치본에 기대므로 멈춘다."""


# ---------------------------------------------------------------- 결과

def _short(detail) -> str:
    s = detail if isinstance(detail, str) else repr(detail)
    return s[-4000:]


def check(name: str, cond, detail="", *, fatal: bool = False) -> bool:
    ok = bool(cond)
    RESULTS.append({"step": CUR["step"], "name": name, "ok": ok, "detail": "" if ok else _short(detail)})
    print(("  OK   " if ok else "  FAIL ") + name, flush=True)
    if not ok:
        for line in _short(detail).splitlines()[-40:]:
            print("       | " + line, flush=True)
        if fatal:
            raise Abort(name)
    return ok


def guard() -> None:
    why = []
    if os.environ.get("CLEWPATH_E2E_CONTAINER") != "1":
        why.append("CLEWPATH_E2E_CONTAINER=1 이 없습니다")
    if not sys.platform.startswith("linux"):
        why.append(f"리눅스가 아닙니다({sys.platform})")
    elif os.getuid() == 0:
        why.append("root 로는 돌지 않습니다(일반 사용자 tester)")
    if not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        why.append("컨테이너 표식(/.dockerenv)이 없습니다")
    if why:
        print("[e2e] 거부 — 이 드라이버는 e2e 컨테이너 안에서만 돈다(실제 ~/.local/share·~/.claude·5100 에 설치한다): "
              + "; ".join(why), flush=True)
        sys.exit(2)


# ---------------------------------------------------------------- 명령·파일

def run(argv, *, env=None, timeout=600, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=str(cwd or HOME), stdin=subprocess.DEVNULL)


def out_of(r: subprocess.CompletedProcess) -> str:
    return f"rc={r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"


def save(name: str, r: subprocess.CompletedProcess) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{len(list(OUT.iterdir())):02d}-{name}.log").write_text(out_of(r), encoding="utf-8")


def install_env(**extra) -> dict:
    return {**os.environ, "CLEWPATH_UV_ARCHIVE": IX["uv_archive"], **extra}


INSTALL_ARGS: list[str] = []                      # main 에서 채운다(--cp-url 은 index 의 cp)


def get_sh(*args: str, env=None) -> subprocess.CompletedProcess:
    """명세 6.3 의 공통 설치 명령: curl -fsS …/get.sh | sh -s -- …"""
    return run(["/bin/sh", "-c", 'u=$1; shift; curl -fsS "$u" | sh -s -- "$@"', "sh", f"{CP}/get.sh", *args],
               env=env or install_env(), timeout=900)


def lc(*args: str, shell=("/bin/sh",), env=None, timeout=90) -> subprocess.CompletedProcess:
    return run([*shell, LAUNCHER, *args], env=env, timeout=timeout)


def kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def status() -> dict:
    r = lc("status")
    d = kv(r.stdout)
    d["rc"] = r.returncode
    return d


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def raw(p: Path) -> bytes:
    try:
        return p.read_bytes()
    except OSError:
        return b""


def mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def tree_hash(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        h.update(f.relative_to(p).as_posix().encode())
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


def tail(p: Path, n: int = 60) -> str:
    try:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return "(없음)"


def wait_for(pred, timeout: float, interval: float = 0.3):
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() >= end:
            return v
        time.sleep(interval)


def marks() -> dict:
    """shutdown.json 의 pid 별 종료 표식."""
    return (read_json(DATA / "shutdown.json") or {}).get("pids") or {}


def incidents() -> bytes:
    return raw(DATA / "incidents.jsonl")


def since_last_start(p: Path) -> str:
    """host.log 의 마지막 런처 구분줄 뒤(이번 기동의 Host 가 쓴 것)."""
    t = raw(p).decode("utf-8", "replace")
    i = t.rfind("=== launcher start")
    return t[i:] if i >= 0 else t


# ---------------------------------------------------------------- 프로세스(/proc — 이미지에 ps 가 없다)

def proc_args(pid: int) -> list[str] | None:
    try:
        b = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return b.decode("utf-8", "replace").split("\0")[:-1] if b else []


def proc_state(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def pids_where(pred) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        a = proc_args(int(d.name))
        if a and proc_state(int(d.name)) not in ("", "Z", "X") and pred(a):
            out.append(int(d.name))
    return sorted(out)


def host_pids() -> list[int]:
    return pids_where(lambda a: a[0].startswith(VENV_BIN) and "session_manager.server" in a)


def broker_pids() -> list[int]:
    return pids_where(lambda a: a[0].startswith(VENV_BIN) and "session_manager.ptyd" in a)


def watch_pids() -> list[int]:
    return pids_where(lambda a: any(x.endswith("/start-connector.sh") for x in a) and "watch" in a)


def runner_pids() -> list[int]:
    return pids_where(lambda a: any(x.endswith("/update_runner.sh") for x in a))


def app_pids() -> list[int]:
    return pids_where(lambda a: any(str(APP) in x for x in a))


def alive(pid) -> bool:
    return bool(pid) and proc_state(int(pid)) not in ("", "Z", "X")


def proc_env(pid: int) -> dict:
    b = raw(Path(f"/proc/{pid}/environ"))
    return dict(x.split("=", 1) for x in b.decode("utf-8", "replace").split("\0") if "=" in x)


def proc_umask(pid: int) -> str:
    m = re.search(r"^Umask:\s*(\d+)$", raw(Path(f"/proc/{pid}/status")).decode(), re.M)
    return m.group(1) if m else ""


def tcp_states(port: int) -> list[str]:
    """/proc/net/tcp 에서 이 로컬 포트 소켓들의 상태(06 = TIME_WAIT, 0A = LISTEN)."""
    out = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in raw(Path(f)).decode().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[1].rsplit(":", 1)[-1] == f"{port:04X}":
                out.append(parts[3])
    return out


def plain_bind_errno(port: int) -> int:
    """SO_REUSEADDR 없이 bind — 옛 판정(server._port_free 의 1단계)이 보는 것."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return 0
    except OSError as e:
        return e.errno or -1
    finally:
        s.close()


def sleeper(*tag: str) -> subprocess.Popen:
    """명령줄에 tag 가 든 살아 있는 프로세스(잠금·표식의 '산 pid' 흉내). 끝나면 kill+wait 해 좀비를 남기지 않는다."""
    return subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)", *tag],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reap(p: subprocess.Popen) -> None:
    try:
        p.kill()
    except OSError:
        pass
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------- HTTP(루프백, 프록시 없이)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(port: int, path: str, method: str = "GET", body=None, timeout: float = 5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            code, b = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, b = e.code, e.read()
    except Exception:  # noqa: BLE001
        return None, None
    try:
        return code, json.loads(b)
    except Exception:  # noqa: BLE001
        return code, b.decode("utf-8", "replace")


def runtime() -> dict:
    return read_json(DATA / "runtime.json") or {}


def port_now() -> int:
    p = runtime().get("port")
    return p if isinstance(p, int) else DEFAULT_PORT


def healthy(port: int | None = None) -> bool:
    return api(port or port_now(), "/api/health", timeout=3)[1] == {"status": "ok"}


def diag() -> dict:
    d = api(port_now(), "/api/owner/diagnostics", timeout=10)[1]
    return d if isinstance(d, dict) else {}


def version_now() -> str | None:
    return diag().get("version")


def host_up(timeout: float = 40) -> int | None:
    """runtime.json 의 pid 가 살아 있는 Host 이고 health 가 ok 일 때 그 pid."""
    def ready():
        pid = runtime().get("pid")
        return pid if pid in host_pids() and healthy() else None
    return wait_for(ready, timeout, 0.4)


def stop_host() -> subprocess.CompletedProcess:
    r = lc("stop", timeout=60)
    wait_for(lambda: not host_pids(), 10)
    return r


def start_host() -> int | None:
    lc("start")
    return host_up()


# ---------------------------------------------------------------- 가짜 CP

def reqs() -> list[dict]:
    out = []
    for line in raw(STATE / "requests.log").decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def req_mark() -> int:
    return len(reqs())


def paths_since(mark: int) -> list[str]:
    return [f"{r['m']} {r['p']}" for r in reqs()[mark:]]


def set_current(name: str | None) -> None:
    cur = STATE / "current.jwt"
    if name is None:
        cur.unlink(missing_ok=True)
        return
    tmp = STATE / "current.jwt.tmp"
    tmp.write_bytes((FIX / IX["manifests"][name]).read_bytes())
    os.replace(tmp, cur)


def start_fake_cp() -> subprocess.Popen:
    STATE.mkdir(parents=True, exist_ok=True)
    log = (STATE / "fake_cp.out").open("ab")
    p = subprocess.Popen([sys.executable, "-I", str(E2E / "fake_cp.py"), "--state", str(STATE),
                          "--fixtures", str(FIX), "--port", "8099"], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    ok = wait_for(lambda: api(8099, "/cp/healthz", timeout=2)[1] == {"ok": True}, 20)
    check("가짜 CP 기동(127.0.0.1:8099)", ok, tail(STATE / "fake_cp.out"), fatal=True)
    return p


# ---------------------------------------------------------------- 공통 확인

def ver(name: str) -> str:
    return IX["variants"][name]["ver"]


def readme_first_line() -> str:
    return raw(APP / "README.md").decode("utf-8", "replace").split("\n", 1)[0]


def leftovers() -> list[str]:
    """get.sh·install.sh 의 임시 폴더(설치 루트 옆 .clewpath-get.*, TMPDIR 의 clewpath-get.*·clewpath-install.*)."""
    out = [str(p) for p in CH.glob(".clewpath-get.*")] if CH.is_dir() else []
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    out += [str(p) for pat in ("clewpath-get.*", "clewpath-install.*") for p in tmp.glob(pat)]
    return out


def last_apply() -> dict:
    return read_json(WORK / "last_apply.json") or {}


def runner_done() -> bool:
    return not runner_pids() and not (WORK / "runner.lock.d").exists() and not (DATA / "update.inprogress").exists()


def swap_left() -> list[str]:
    return [p.name for p in APP.glob(".clewpath-swap.*")] if APP.is_dir() else []


def wait_applied(v: str, timeout: float) -> dict:
    """last_apply.json 이 v 로 바뀌고 러너가 다 끝날 때까지(롤백은 시작할 때 ok=false 를 먼저 쓴다)."""
    def done():
        la = last_apply()
        return la if la.get("version") == v and runner_done() else None
    return wait_for(done, timeout, 1.0) or {}


def recently_failed(v: str) -> str:
    r = run([VPY, "-I", "-c", "import sys; from session_manager import updater; print(updater.recently_failed(sys.argv[1]))",
             v], timeout=60)
    return r.stdout.strip() or out_of(r)


def logs_hint() -> str:
    return "\n".join(f"--- {p.name}\n{tail(p, 25)}" for p in (DATA / "launcher.log", DATA / "host.err.log", DATA / "host.log"))


STEPS: list = []


def step(sid: str, title: str):
    def deco(fn):
        STEPS.append((sid, title, fn))
        return fn
    return deco


# ---------------------------------------------------------------- S0

@step("S0", "정적 — 다섯 스크립트를 dash -n · bash --posix -n")
def s0():
    tmp = Path(tempfile.mkdtemp(prefix="e2e-s0-"))
    names = {"install.sh": "install.sh", "uninstall.sh": "uninstall.sh",
             "update_runner.sh": "session_manager/update_runner.sh",
             "start-connector.template.sh": "session_manager/start-connector.template.sh"}
    with zipfile.ZipFile(FIX / IX["variants"]["A"]["zip"]) as z:
        for short, member in names.items():
            (tmp / short).write_bytes(z.read(member))
    targets = {"get.sh": FIX / "get.sh", **{n: tmp / n for n in names}}
    for shell in (["dash", "-n"], ["bash", "--posix", "-n"]):
        for n, p in targets.items():
            r = run([*shell, p], timeout=60)
            check(f"{' '.join(shell)} {n}", r.returncode == 0, out_of(r))
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- S1

@step("S1", "get.sh 거부 — 설치 전, 매번 아무것도 남지 않음")
def s1():
    no_http = ["--cp-url", CP, "--relay-url", RELAY, "--app-url", APP_URL]
    cases = [
        ("게시된 릴리스 없음(404)", None, None, 1, "아직 게시된 릴리스가 없습니다", ()),
        ("T: sha256 변조", "T-sha", None, 1, "SHA-256 불일치", ("T",)),
        ("T: size 변조(실제보다 큼)", "T-size", None, 1, "크기 불일치", ("T",)),
        ("T: size 변조(실제보다 작음 — curl 63)", "T-small", None, 1, "크기 불일치", ("T",)),
        ("--allow-http 없이 http CP", "A", no_http, 2, "--cp-url 는 https:// 주소여야 합니다", ()),
        ("Z: ../ 항목", "Z-dotdot", None, 1, "'..' 가 든 이름", ("Z-dotdot",)),
        ("Z: 절대 경로 항목", "Z-abs", None, 1, "절대 경로", ("Z-abs",)),
        ("Z: 심볼릭 링크 항목", "Z-link", None, 1, "심볼릭 링크", ("Z-link",)),
        ("L: 0.28.1 모양(platforms 클레임·install.sh 없음)", "L", None, 1,
         "맥·리눅스 설치를 지원하지 않습니다", ()),
        ("L': linux 클레임은 있으나 zip 에 install.sh 없음(푼 뒤 거부)", "L-noinstall", None, 1,
         "맥·리눅스 설치를 지원하지 않습니다", ("L-noinstall",)),
        ("P: platforms=[win]", "P", None, 1, "맥·리눅스 설치를 지원하지 않습니다", ()),
        ("M: 내용이 깨진 JWT", "M", None, 1, "릴리스 정보(매니페스트)가 올바르지 않습니다", ()),
    ]
    evil = [CH / "clewpath-e2e-evil", CH.parent / "clewpath-e2e-evil", HOME / "clewpath-e2e-evil",
            Path("/tmp/clewpath-e2e-evil"), Path(IX["evil"]["abs"])]
    for title, manifest, args, rc, said, dl in cases:
        set_current(manifest)
        mark = req_mark()
        r = get_sh(*(args or INSTALL_ARGS))
        save(f"s1-{manifest or 'none'}", r)
        text = r.stdout + r.stderr
        check(f"{title}: 종료코드 {rc}·'{said}'", r.returncode == rc and said in text, out_of(r))
        check(f"{title}: 설치 폴더·임시 폴더가 생기지 않음", not APP.exists() and not leftovers(),
              (APP.exists(), leftovers()))
        if manifest and manifest.startswith("Z"):
            check(f"{title}: 바깥에 파일이 없음", not any(p.exists() or p.is_symlink() for p in evil),
                  [str(p) for p in evil if p.exists()])
        want = ["GET /cp/get.sh"] + (["GET /cp/updates/manifest"] if args is None else [])
        want += [f"GET /cp/updates/download/{ver(n)}" for n in dl]
        what = "·".join(w.rsplit("/", 1)[-1] for w in want)
        check(f"{title}: 요청은 {what} 뿐", paths_since(mark) == want, (paths_since(mark), want))
    check("S1 동안 Host·설치 폴더 없음", not host_pids() and not APP.exists() and not DATA.exists())


# ---------------------------------------------------------------- S2

@step("S2", "설치(A) — curl …/get.sh | sh")
def s2():
    set_current("A")
    mark = req_mark()
    r = get_sh(*INSTALL_ARGS)
    save("s2-install-A", r)
    out = r.stdout + r.stderr
    KEEP["install_A"] = out
    check("종료코드 0·설치 완료", r.returncode == 0 and "=== 설치 완료 ===" in r.stdout, out_of(r) + logs_hint(), fatal=True)
    for said in ("[OK]   무결성 확인 (SHA-256)", "[OK]   압축 해제", f"최신 버전 : {ver('A')}",
                 "uv 해시 검증 통과 (SHA-256, 스크립트에 고정된 값)", "시스템 파이썬 사용: /usr/local/bin/python3",
                 "의존성 설치 완료 (uv.lock 고정 버전)", "자격증명 발급 완료 (connector c-e2e-1)", "서비스 기동 확인"):
        check(f"출력: {said}", said in out, out)
    payload = set(IX["payload"])
    # clewpath_host.egg-info: uv sync 의 편집 설치(setuptools)가 설치 루트에 만든다 — PAYLOAD 밖이라 러너는 건드리지 않고
    # sync 가 버전마다 다시 쓴다(실측)
    check("레이아웃(2.3): 설치 루트 = PAYLOAD + .venv·start-connector.sh·.clewpath-install(+ 편집 설치의 egg-info)",
          set(os.listdir(APP)) == payload | {".venv", "start-connector.sh", ".clewpath-install", "clewpath_host.egg-info"},
          sorted(os.listdir(APP)))
    check("레이아웃(2.3): CLEWPATH_HOME = app·bin(시스템 파이썬이라 python/ 없음)", set(os.listdir(CH)) == {"app", "bin"},
          sorted(os.listdir(CH)))
    check("bin/uv·uvx 0755", (CH / "bin" / "uv").is_file() and mode(CH / "bin" / "uv") == 0o755
          and mode(CH / "bin" / "uvx") == 0o755, oct(mode(CH / "bin" / "uv")))
    check("start-connector.sh 0755", mode(LAUNCHER) == 0o755, oct(mode(LAUNCHER)))
    rec = kv(raw(APP / ".clewpath-install").decode())
    check(".clewpath-install: 경로·파이썬 선호·버전",
          rec.get("home") == str(CH) and rec.get("uv") == str(CH / "bin" / "uv") and rec.get("python_pref") == "only-system"
          and rec.get("python", "").startswith("/usr/local/bin/python3") and rec.get("data_dir") == str(DATA)
          and rec.get("conf_file") == str(CONF) and rec.get("installed_version") == ver("A"), rec)
    check("데이터 폴더 0700", mode(DATA) == 0o700, oct(mode(DATA)))
    tb, cb = raw(CONF), raw(CREDS)
    check("clewpath.toml 0600·BOM 없음", mode(CONF) == 0o600 and not tb.startswith(b"\xef\xbb\xbf"), oct(mode(CONF)))
    try:
        conf = tomllib.loads(tb.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        conf = {"error": repr(e)}
    cloud = conf.get("cloud") or {}
    check("clewpath.toml: tomllib 통과·cp_url 은 가짜 CP·포트 5100",
          cloud.get("cp_url") == CP and cloud.get("relay_url") == RELAY and cloud.get("app_url") == APP_URL
          and (conf.get("server") or {}).get("port") == DEFAULT_PORT, conf)
    creds = read_json(CREDS) or {}
    check("cp_credentials.json 0600·BOM 없음·가짜 CP 발급분", mode(CREDS) == 0o600 and not cb.startswith(b"\xef\xbb\xbf")
          and creds.get("cp_url") == CP and creds.get("connector_id") == "c-e2e-1", creds)
    pid = host_up(30)
    check("runtime.json·health·Host 하나", pid and host_pids() == [pid] and runtime().get("port") == DEFAULT_PORT,
          (runtime(), host_pids(), logs_hint()), fatal=True)
    check("진단 version = A", version_now() == ver("A"), diag())
    hooks = (read_json(SETTINGS) or {}).get("hooks") or {}
    cmds = [h.get("command", "") for g in hooks.values() for x in g for h in (x.get("hooks") or [])]
    check("settings.json 훅: curl -s -m 3 …(curl.exe 아님) 5개",
          len(cmds) == 5 and all(c.startswith("curl -s -m 3 ") and "curl.exe" not in c
                                 and c.endswith(f"http://127.0.0.1:{DEFAULT_PORT}/api/hooks/event") for c in cmds), cmds)
    check(f"Host umask = 드라이버 umask({UMASK:04o})(S38)", proc_umask(pid) == f"{UMASK:04o}", proc_umask(pid))
    env = proc_env(pid)
    check("Host env: uv 설정·토큰이 새지 않고 CLEWPATH_CONFIG 고정",
          not [k for k in env if k.startswith("UV_") and k not in ("UV_OFFLINE", "UV_PYTHON_DOWNLOADS")]
          and "CLEWPATH_CLIENT_TOKEN" not in env and env.get("CLEWPATH_CONFIG") == str(CONF), sorted(env))
    check("임시 폴더·설치 표식이 남지 않음", not leftovers() and not (DATA / "update.inprogress").exists(), leftovers())
    for shell in (["dash", "-n"], ["bash", "--posix", "-n"]):
        r = run([*shell, LAUNCHER], timeout=60)
        check(f"S0 렌더 결과: {' '.join(shell)} start-connector.sh", r.returncode == 0, out_of(r))
    p = paths_since(mark)
    head = ["GET /cp/get.sh", "GET /cp/updates/manifest", f"GET /cp/updates/download/{ver('A')}"]
    rest = p[3:]
    check("요청: get.sh·매니페스트·zip(A) → 발급 1회, 그 뒤로는 Host 의 token·매니페스트뿐",
          p[:3] == head and rest.count("POST /cp/provision/anonymous") == 1
          and all(x in ("POST /cp/provision/anonymous", "POST /cp/connector/token", "GET /cp/updates/manifest")
                  for x in rest), p)
    KEEP["projects"] = tree_hash(PROJECTS)


# ---------------------------------------------------------------- S3

@step("S3", "재실행 멱등 — sh $APP/install.sh")
def s3():
    toml0, creds0, launcher0 = raw(CONF), raw(CREDS), raw(LAUNCHER)
    ino0 = os.lstat(VPY).st_ino
    inc0 = incidents()
    old = runtime().get("pid")
    r = run(["/bin/sh", APP / "install.sh", *INSTALL_ARGS], env=install_env(), timeout=900)
    save("s3-reinstall", r)
    out = r.stdout + r.stderr
    check("종료코드 0·설치 완료", r.returncode == 0 and "=== 설치 완료 ===" in r.stdout, out_of(r) + logs_hint(), fatal=True)
    for said in ("설치된 uv 재사용", "기존 venv 재사용", "기존 설정 파일 유지", "이미 발급된 자격증명이 있습니다(재사용)",
                 f"실행 중이던 Host 를 멈췄습니다(pid {old})"):
        check(f"출력: {said}", said in out, out)
    check("설정·자격증명 바이트 그대로", raw(CONF) == toml0 and raw(CREDS) == creds0)
    check(".venv/bin/python inode 그대로(재사용)", os.lstat(VPY).st_ino == ino0)
    check("런처 내용 그대로", raw(LAUNCHER) == launcher0)
    new = host_up(30)
    check("Host 는 새 pid(stop 후 start)", new and new != old and old not in host_pids(), (old, new, host_pids()))
    check("옛 pid 의 정상 종료 표식은 새 Host 가 소비했고 사고 기록 없음",
          str(old) not in marks() and incidents() == inc0
          and f"이전 Host(pid {old}) 가 정상 종료 기록 없이 사라짐" not in raw(DATA / "host.log").decode("utf-8", "replace"),
          (marks(), incidents()[-400:]))


# ---------------------------------------------------------------- S4

@step("S4", "런처 ①~⑪ — 실제 Host 로")
def s4():
    sh_ = "/bin/sh"
    # ① 두 번 start → 하나. 멈춘 뒤 동시에 셋 → 하나
    pid0 = host_up(20)
    r1, r2 = lc("start"), lc("start")
    check("① 떠 있을 때 start 두 번 — 그대로 하나", r1.returncode == 0 and r2.returncode == 0 and host_pids() == [pid0],
          (out_of(r1), out_of(r2), host_pids()))
    stop_host()
    ps = [subprocess.Popen([sh_, str(LAUNCHER), "start"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) for _ in range(3)]
    rcs = [p.wait(60) for p in ps]
    pid1 = host_up(30)
    time.sleep(1)
    check("① 멈춘 뒤 동시에 start 셋 — Host 하나", rcs == [0, 0, 0] and pid1 and host_pids() == [pid1],
          (rcs, host_pids(), tail(DATA / "launcher.log", 15)))

    # ② kill -9 → start: 새 pid, host.log 이어 쓰기, 사고 기록(진짜 비정상 종료)
    before = raw(DATA / "host.log")
    inc0 = incidents()
    os.kill(pid1, signal.SIGKILL)
    wait_for(lambda: pid1 not in host_pids(), 10)
    r = lc("start")
    pid2 = host_up(30)
    after = raw(DATA / "host.log")
    check("② kill -9 뒤 start — 새 pid", r.returncode == 0 and pid2 and pid2 != pid1, (out_of(r), pid1, pid2))
    check("② host.log 는 이어 쓰기(옛 줄이 남음)", after.startswith(before) and len(after) > len(before),
          (len(before), len(after)))
    got = wait_for(lambda: len(incidents()) > len(inc0), 10)
    last = json.loads(incidents().splitlines()[-1]) if got else {}
    check("② 정상 종료 표식 없이 사라진 Host 는 사고로 기록", last.get("prev_pid") == pid1, last)

    # ③ maintenance.flag → 띄우지 않음, resume → 뜸
    r = lc("stop", "--hold")
    flag = DATA / "maintenance.flag"
    check("③ stop --hold — 멈추고 maintenance.flag", r.stdout.startswith(f"stopped (pid {pid2})") and flag.is_file(),
          out_of(r))
    r = lc("start")
    time.sleep(2)
    st = status()
    check("③ maintenance.flag 면 start 가 띄우지 않음(status maintenance, rc 3)",
          r.returncode == 0 and not host_pids() and st.get("state") == "maintenance" and st["rc"] == 3, (out_of(r), st))
    r = lc("resume")
    pid3 = host_up(30)
    check("③ resume — 표식을 지우고 뜸", r.returncode == 0 and pid3 and not flag.exists(), out_of(r))

    # ④ 잠금: 산 잠금이면 보류, 시체(pid 죽음·pid 없이 2분 넘음)면 넘어간다
    stop_host()
    lock = DATA / "launcher.lock"
    holder = sleeper("start-connector.sh")
    try:
        lock.mkdir()
        (lock / "pid").write_text(f"{holder.pid}\n", encoding="ascii")
        r = lc("start")
        time.sleep(2)
        check("④ 산 잠금(launcher.lock) — 보류", r.returncode == 0 and not host_pids() and lock.is_dir()
              and "skip: 다른 런처가 기동 중(launcher.lock)" in tail(DATA / "launcher.log", 5),
              (out_of(r), tail(DATA / "launcher.log", 5)))
    finally:
        reap(holder)
    r = lc("start")
    pid4 = host_up(30)
    check("④ 시체 잠금(pid 죽음) — 지우고 뜸", r.returncode == 0 and pid4 and not lock.exists(), out_of(r))
    stop_host()
    lock.mkdir()
    old_t = time.time() - 180
    os.utime(lock, (old_t, old_t))
    r = lc("start")
    pid4 = host_up(30)
    check("④ 시체 잠금(pid 없이 2분 넘음) — 지우고 뜸", r.returncode == 0 and pid4 and not lock.exists(), out_of(r))

    # ⑤ 브로커는 ptyclient 와 같은 명령(Host 의 /api/owner/ptyd/restart → ptyclient.ensure). stop 은 SIGTERM·브로커 보존,
    #    TERM 을 못 받는 Host(SIGSTOP) 는 15초 뒤 KILL 과 forced-stop 표식(사고 아님 — Q21)
    code, body = api(port_now(), "/api/owner/ptyd/restart", "POST", {"force": False}, timeout=30)
    bp = (read_json(DATA / "ptyd.json") or {}).get("pid")
    check("⑤ 브로커 기동(ptyclient 와 같은 명령: venv python -m session_manager.ptyd)",
          code == 200 and isinstance(body, dict) and (body.get("status") or {}).get("running") and alive(bp)
          and proc_args(bp) == [str(VPY), "-m", "session_manager.ptyd"], (code, body, bp and proc_args(bp)))
    KEEP["broker"] = bp
    st = status()
    check("⑤ status: broker_pid·broker_alive", st.get("broker_pid") == str(bp) and st.get("broker_alive") == "1", st)
    hp = runtime().get("pid")
    inc0 = incidents()
    r = lc("stop", timeout=60)
    check("⑤ stop — SIGTERM 으로 끝남·정상 종료 표식", r.stdout.strip() == f"stopped (pid {hp})"
          and (marks().get(str(hp)) or {}).get("reason") == "normal", (out_of(r), marks()))
    check("⑤ stop 뒤에도 브로커는 산다", alive(bp) and broker_pids() == [bp], broker_pids())
    hp = start_host()
    os.kill(hp, signal.SIGSTOP)                    # TERM 을 처리하지 못하는 Host
    t0 = time.monotonic()
    r = lc("stop", timeout=90)
    took = time.monotonic() - t0
    check("⑤ TERM 을 못 받는 Host — 15초 뒤 KILL(stopped …, forced)",
          r.stdout.strip() == f"stopped (pid {hp}, forced)" and 14 <= took <= 40 and not alive(hp),
          (out_of(r), round(took, 1)))
    check("⑤ 강제 종료도 forced-stop 표식·launcher.log 기록", (marks().get(str(hp)) or {}).get("reason") == "forced-stop"
          and "stop: forced" in tail(DATA / "launcher.log", 5), (marks(), tail(DATA / "launcher.log", 5)))
    hp2 = start_host()
    check("⑤ 다시 기동 — forced-stop 은 사고로 보지 않음·브로커 그대로", hp2 and incidents() == inc0 and alive(bp),
          (incidents()[-300:], alive(bp)))

    # ⑥ claude 표식 env 는 Host 로 가지 않는다
    stop_host()
    r = lc("start", env={**os.environ, "CLAUDECODE": "1", "CLAUDE_CODE_X": "1", "CLAUDE_PID": "7", "CLAUDE_EFFORT": "max"})
    hp = host_up(30)
    env = proc_env(hp) if hp else {}
    check("⑥ CLAUDECODE·CLAUDE_CODE_*·CLAUDE_PID·CLAUDE_EFFORT 없음, CLEWPATH_CONFIG·PYTHONUNBUFFERED 있음",
          hp and not ({"CLAUDECODE", "CLAUDE_CODE_X", "CLAUDE_PID", "CLAUDE_EFFORT"} & set(env))
          and env.get("CLEWPATH_CONFIG") == str(CONF) and env.get("PYTHONUNBUFFERED") == "1", sorted(env))

    # ⑦ 권한 — 데이터 폴더는 기동마다 0700, 비밀 파일 0600, Host umask 는 사용자 것
    stop_host()
    os.chmod(DATA, 0o755)
    hp = start_host()
    check("⑦ 기동하면 데이터 폴더 0700 으로(넓혀 놨어도)", mode(DATA) == 0o700, oct(mode(DATA)))
    check("⑦ 설정·자격증명 0600·런처 0755", mode(CONF) == 0o600 and mode(CREDS) == 0o600 and mode(LAUNCHER) == 0o755,
          (oct(mode(CONF)), oct(mode(CREDS)), oct(mode(LAUNCHER))))
    check(f"⑦ Host umask {UMASK:04o}(사용자 것 그대로)", proc_umask(hp) == f"{UMASK:04o}", proc_umask(hp))

    # ⑧ 5100 을 남이 쥐고 있으면 5101 — 런처는 runtime.json 으로 찾고, 커넥터는 실제 포트로 프록시(4.4)
    stop_host()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR: 방금 내린 Host 의 연결이 5100 에 TIME_WAIT 로 남아 있다(Connection: close 응답마다 서버 쪽이 먼저
        # 닫는다) — 그 자리에 듣는 소켓을 세우려면 uvicorn 과 같은 옵션이 필요하다(⑨ 가 보는 바로 그 상황)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", DEFAULT_PORT))
        blocker.listen(16)
        r = lc("start")
        hp = host_up(40)
        st = status()
        log_now = since_last_start(DATA / "host.log")
        check("⑧ 5100 이 막히면 5101 로 뜨고 런처 status 가 따라감", hp and runtime().get("port") == DEFAULT_PORT + 1
              and st.get("state") == "running" and st.get("port") == str(DEFAULT_PORT + 1), (out_of(r), st, runtime()))
        check("⑧ host.log: 폴백 안내와 [relay] … (local http://127.0.0.1:5101)",
              f"{DEFAULT_PORT} 가 사용 중이라 {DEFAULT_PORT + 1} 로 대신 엽니다" in log_now
              and f"(local http://127.0.0.1:{DEFAULT_PORT + 1})" in log_now, log_now[-1500:])
        stop_host()
    finally:
        blocker.close()
    hp = start_host()
    check("⑧ 막은 것을 치우면 다시 5100", hp and runtime().get("port") == DEFAULT_PORT, runtime())

    # ⑨ TIME_WAIT — keep-alive 를 서버가 닫게 둔 뒤 stop → 곧바로 start 해도 5100(4.1 port_free)
    c = http.client.HTTPConnection("127.0.0.1", DEFAULT_PORT, timeout=5)
    c.request("GET", "/api/health")
    c.getresponse().read()
    time.sleep(6.5)                                # uvicorn timeout_keep_alive=5 — 서버 쪽이 먼저 닫는다
    c.close()
    tw = wait_for(lambda: tcp_states(DEFAULT_PORT).count("06"), 5)
    r = lc("stop", timeout=60)
    wait_for(lambda: not host_pids(), 10)
    e = plain_bind_errno(DEFAULT_PORT)
    r2 = lc("start")
    hp = host_up(30)
    log_now = since_last_start(DATA / "host.log")
    check("⑨ TIME_WAIT 가 있어(옛 판정이면 사용 중) — 곧바로 start 해도 5100",
          tw and e == errno.EADDRINUSE and hp and runtime().get("port") == DEFAULT_PORT and "사용 중이라" not in log_now,
          (tw, tcp_states(DEFAULT_PORT), e, runtime(), out_of(r2), log_now[-800:]))

    # ⑩ watch 10초 — 하나만, kill 뒤 간격 안에 되살림, TERM 이면 잠금을 풀고 끝나며 Host 는 산다
    w = subprocess.Popen([sh_, str(LAUNCHER), "watch", "10"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        got = wait_for(lambda: status().get("watch_pid") == str(w.pid), 10, 0.5)
        r = run([sh_, LAUNCHER, "watch", "10"], timeout=20)
        check("⑩ watch 는 하나만(두 번째는 곧바로 0)", got and r.returncode == 0 and watch_pids() == [w.pid],
              (status(), out_of(r), watch_pids()))
        old = runtime().get("pid")
        os.kill(old, signal.SIGKILL)
        t0 = time.monotonic()
        new = wait_for(lambda: (runtime().get("pid") != old and host_up(1)) or None, 40, 0.5)
        took = time.monotonic() - t0
        check("⑩ kill -9 뒤 watch 가 간격(10초) 안에 되살림", new and new != old and took <= 25,
              (old, new, round(took, 1), tail(DATA / "launcher.log", 8)))
        w.send_signal(signal.SIGTERM)
        rc = w.wait(15)
        check("⑩ TERM — watch 는 잠금을 풀고 끝나고 Host 는 산다", rc == 0 and not (DATA / "watch.lock.d").exists()
              and new in host_pids() and healthy(), (rc, (DATA / "watch.lock.d").exists(), host_pids()))
    finally:
        reap(w)

    # ⑪ 업데이트 표식 — 산 pid 면 건너뜀, CLEWPATH_UPDATER=1 은 통과, 시체(죽은 pid·다른 프로그램)는 지우고 기동
    stop_host()
    marker = DATA / "update.inprogress"
    busy = sleeper("update-e2e")
    try:
        marker.write_text(f"pid={busy.pid}\nat={int(time.time())}\n", encoding="ascii")
        r = lc("start")
        time.sleep(2)
        st = status()
        check("⑪ 산 pid 의 표식 — start 가 건너뜀(status updating)", r.returncode == 0 and not host_pids()
              and marker.is_file() and st.get("state") == "updating"
              and "skip: 업데이트 진행 중" in tail(DATA / "launcher.log", 5), (out_of(r), st))
        r = lc("start", env={**os.environ, "CLEWPATH_UPDATER": "1"})
        hp = host_up(30)
        check("⑪ CLEWPATH_UPDATER=1(러너 자신의 재기동)은 통과·Host env 에는 남기지 않음",
              hp and marker.is_file() and "CLEWPATH_UPDATER" not in proc_env(hp), (out_of(r), hp))
        stop_host()
        marker.write_text(f"pid={busy.pid}\nat={int(time.time())}\nby=runner\n", encoding="ascii")
        r = lc("start")
        hp = host_up(30)
        check("⑪ by=runner 인데 그 pid 가 러너가 아님(pid 재사용) — 시체로 지우고 기동", hp and not marker.exists()
              and "stale update marker 제거" in tail(DATA / "launcher.log", 5), (out_of(r), tail(DATA / "launcher.log", 5)))
        stop_host()
    finally:
        reap(busy)
    marker.write_text(f"pid={busy.pid}\nat={int(time.time())}\n", encoding="ascii")
    r = lc("start")
    hp = host_up(30)
    check("⑪ 죽은 pid 의 표식 — 지우고 기동", hp and not marker.exists(), out_of(r))
    check("S4 끝: Host 하나(5100)·브로커 그대로", host_pids() == [hp] and port_now() == DEFAULT_PORT
          and alive(KEEP["broker"]), (host_pids(), runtime(), broker_pids()))


# ---------------------------------------------------------------- S5·S6·S7·S9 공통

def apply_now(name: str) -> tuple:
    set_current(name)
    return api(port_now(), "/api/owner/update/apply", "POST", timeout=120)


def common_after_update(name: str, inc0: bytes, *, launcher_name: str, launcher_said: str = "") -> None:
    v = ver(name)
    check(f"{v}: 잠금·표식·교체 저널이 남지 않음", runner_done() and not (WORK / "apply.lock").exists() and not swap_left(),
          (runner_pids(), list(WORK.glob("*.lock*")), (DATA / "update.inprogress").exists(), swap_left()))
    check("사고 기록 그대로(update·normal 표식)", incidents() == inc0, incidents()[-400:])
    check("브로커 pid 그대로 살아 있음", alive(KEEP["broker"]) and broker_pids() == [KEEP["broker"]], broker_pids())
    tpl = IX["variants"][launcher_name]["template"]
    check(launcher_said or f"start-connector.sh 가 {launcher_name} 템플릿으로 다시 렌더됨",
          tpl is not None and tpl in raw(LAUNCHER).decode("utf-8", "replace"), raw(LAUNCHER)[:400])


@step("S5", "수동 적용 성공(B)")
def s5():
    inc0 = incidents()
    a_ver, b_ver = ver("A"), ver("B")
    old = runtime().get("pid")
    # 유지보수 표식이 있으면 적용하지 않는다(S23) — 런처가 재기동을 막아 헬스 실패 → 롤백 → 그 재기동도 막히는 길을 끊는다
    flag = DATA / "maintenance.flag"
    flag.write_text("by=e2e\n", encoding="ascii")
    code, body = apply_now("B")
    flag.unlink()
    check("maintenance.flag 가 있으면 적용 거부(400)·Host·러너 그대로(S23)",
          code == 400 and "maintenance.flag" in json.dumps(body, ensure_ascii=False) and runtime().get("pid") == old
          and healthy() and not runner_pids() and not (WORK / "apply.lock").exists(), (code, body))
    mark = req_mark()
    code, body = apply_now("B")
    check("POST /api/owner/update/apply → applied:true", code == 200 and isinstance(body, dict) and body.get("applied")
          and body.get("version") == b_ver, (code, body), fatal=True)
    la = wait_applied(b_ver, 150)
    up = wait_for(lambda: version_now() == b_ver, 30, 1.0)
    check(f"150초 안에 진단 version {b_ver}", up and la.get("ok") is True, (la, diag(), tail(WORK / f"apply-{b_ver}.log", 40)),
          fatal=True)
    check("옛 Host 는 내려가고 새 Host 하나", old not in host_pids() and len(host_pids()) == 1, host_pids())
    lab = raw(WORK / "last_apply.json")
    check("last_apply.json: ok=true·BOM 없음", not lab.startswith(b"\xef\xbb\xbf") and la == read_json(WORK / "last_apply.json")
          and la.get("ok") is True, lab)
    bak = WORK / f"backup-{a_ver}"
    check(f"backup-{a_ver} 이 있고 옛 버전", (bak / "session_manager" / "__init__.py").is_file()
          and f'version = "{a_ver}"' in raw(bak / "pyproject.toml").decode(), list(WORK.iterdir()))
    check("README 표식이 B", readme_first_line() == IX["variants"]["B"]["readme"], readme_first_line())
    ens = wait_for(lambda: (DATA / f"launcher-ensured-{b_ver}").is_file(), 30)
    check(f"launcher-ensured-{b_ver}: regenerated", ens and raw(DATA / f"launcher-ensured-{b_ver}").decode().strip()
          == "launcher: regenerated (posix v2)", raw(DATA / f"launcher-ensured-{b_ver}"))
    common_after_update("B", inc0, launcher_name="B")
    log = raw(WORK / f"apply-{b_ver}.log").decode("utf-8", "replace")
    check(f"apply-{b_ver}.log: 헬스 통과·=== 종료 (성공: true) ===",
          f"헬스체크 통과 - 업데이트 성공 ({b_ver})" in log and "=== 종료 (성공: true) ===" in log, log[-2500:])
    p = paths_since(mark)
    check(f"요청: Host 가 매니페스트·zip({b_ver}) 을 한 번 받음", p.count(f"GET /cp/updates/download/{b_ver}") == 1
          and "GET /cp/updates/manifest" in p, p)


@step("S6", "실패와 롤백(C — 첫 줄에서 끝나는 빌드)")
def s6():
    inc0 = incidents()
    b_ver, c_ver = ver("B"), ver("C")
    code, body = apply_now("C")
    check("apply → applied:true", code == 200 and isinstance(body, dict) and body.get("applied"), (code, body), fatal=True)
    la = wait_applied(c_ver, 150)
    back = wait_for(lambda: version_now() == b_ver, 30, 1.0)
    log = raw(WORK / f"apply-{c_ver}.log").decode("utf-8", "replace")
    check(f"150초 안에 {b_ver} 로 돌아옴·last_apply = {{{c_ver}, ok:false}}", back and la.get("ok") is False,
          (la, diag(), log[-2500:]), fatal=True)
    failed = WORK / f"backup-{b_ver}-failed-{c_ver}"
    check(f"backup-{b_ver}-failed-{c_ver} 에 실패본(깨진 server.py)", 'raise SystemExit("e2e: 깨진 빌드")'
          in raw(failed / "session_manager" / "server.py").decode("utf-8", "replace"), sorted(p.name for p in WORK.iterdir()))
    check("README 표식이 B 로 돌아옴", readme_first_line() == IX["variants"]["B"]["readme"], readme_first_line())
    check(f"venv 파이썬으로 recently_failed('{c_ver}') = True", recently_failed(c_ver) == "True", recently_failed(c_ver))
    check("실패 버전의 server 프로세스 없음(Host 는 롤백본 하나)", host_pids() == [runtime().get("pid")], host_pids())
    check("apply 로그: 런처 실패 → 곧바로 롤백 → 롤백 후 정상", all(s in log for s in (
        "런처가 실패했습니다", "롤백합니다.", "이전 버전 복원 완료", "롤백 후 정상 동작 확인", "=== 종료 (성공: false) ===")), log[-2500:])
    check("host.err.log 에 깨진 빌드의 끝 말", "e2e: 깨진 빌드" in raw(DATA / "host.err.log").decode("utf-8", "replace"))
    common_after_update("C", inc0, launcher_name="B",
                        launcher_said="start-connector.sh 는 B 렌더 그대로(C 는 뜨지 못해 다시 렌더하지 않았다)")
    # 이어서 재기동하면 20초 뒤 재적용을 건너뛴다(롤백 무한 반복 방지)
    mark = req_mark()
    stop_host()
    start_host()
    said = f"{c_ver} 는 방금 실패해 롤백됨 - 재적용 건너뜀"
    seen = wait_for(lambda: said in since_last_start(DATA / "host.log"), 60, 1.0)
    check(f"재기동 20초 뒤 host.log: '{said}'", seen and version_now() == b_ver, since_last_start(DATA / "host.log")[-1500:])
    check("롤백된 버전은 다시 받지 않음", f"GET /cp/updates/download/{c_ver}" not in paths_since(mark), paths_since(mark))


@step("S7", "재기동 자동 적용(D) — 러너가 60초 기다린 뒤 TERM")
def s7():
    inc0 = incidents()
    b_ver, d_ver = ver("B"), ver("D")
    set_current("D")
    stop_host()
    t0 = time.monotonic()
    start_host()
    la = wait_applied(d_ver, 260)
    up = wait_for(lambda: version_now() == d_ver, 30, 1.0)
    took = time.monotonic() - t0
    log = raw(WORK / f"apply-{d_ver}.log").decode("utf-8", "replace")
    check(f"재기동 → 약 20초 뒤 자동 적용 → version {d_ver}·ok=true", up and la.get("ok") is True,
          (la, diag(), log[-2500:]), fatal=True)
    check("host.log: 재기동 자동적용", f"[update] 재기동 자동적용: {b_ver} -> {d_ver}" in raw(DATA / "host.log").decode(
        "utf-8", "replace"))
    check("러너가 60초 기다린 뒤 TERM: '시간 내에 안 죽어서 강제 종료합니다.'",
          "시간 내에 안 죽어서 강제 종료합니다." in log and "=== 종료 (성공: true) ===" in log and took >= 75,
          (round(took, 1), log[-2500:]))
    check("README 표식이 D", readme_first_line() == IX["variants"]["D"]["readme"], readme_first_line())
    common_after_update("D", inc0, launcher_name="D")


@step("S9", "sh 근사 — bash --posix 로 런처 stop·start 와 러너 R①(E)")
def s9():
    inc0 = incidents()
    d_ver, e_ver = ver("D"), ver("E")
    hp = runtime().get("pid")
    r = lc("stop", shell=("bash", "--posix"), timeout=60)
    st = status()
    check("bash --posix 런처 stop", r.stdout.strip() == f"stopped (pid {hp})" and st.get("state") == "stopped",
          (out_of(r), st))
    r = lc("start", shell=("bash", "--posix"))
    hp = host_up(30)
    check("bash --posix 런처 start", r.returncode == 0 and hp and status().get("state") == "running", out_of(r))
    # updater.apply 와 같은 자리·인자로(stage·러너 사본·argv) — 쉘만 bash --posix. 본체는 3초 뒤 TERM 받는다
    stage_py = (
        "import json, shutil, sys\n"
        "from pathlib import Path\n"
        "from session_manager import osdep, updater\n"
        "zp, ver, pid, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])\n"
        "staged = updater.stage(zp, {'ver': ver})\n"
        "runner = updater.work_dir() / updater.runner_script().name\n"
        "shutil.copy2(updater.runner_script(), runner)\n"
        "root = updater.install_root()\n"
        "print(json.dumps(osdep.update_runner_argv(runner, install_root=root, stage_dir=staged,\n"
        "    backup_dir=updater.work_dir() / ('backup-' + updater.current_version()), wait_pid=pid, port=port,\n"
        "    version=ver, log_file=updater.work_dir() / ('apply-' + ver + '.log'),\n"
        "    restart_script=str(root / osdep.START_SCRIPT))))\n")
    r = run([VPY, "-I", "-c", stage_py, FIX / IX["variants"]["E"]["zip"], e_ver, hp, port_now()], timeout=120)
    try:
        argv = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        argv = []
    check("E stage·러너 사본·argv(updater 와 같은 함수)", argv[:1] == ["/bin/sh"] and "-RestartScript" in argv, out_of(r),
          fatal=True)
    r = run(["bash", "--posix", *argv[1:], "-WaitSeconds", "3"], timeout=400)
    save("s9-runner-bash-posix", r)
    la = last_apply()
    log = raw(WORK / f"apply-{e_ver}.log").decode("utf-8", "replace")
    check(f"bash --posix 러너 R①: 종료코드 0·version {e_ver}·ok=true", r.returncode == 0 and la.get("version") == e_ver
          and la.get("ok") is True and wait_for(lambda: version_now() == e_ver, 30, 1.0), (out_of(r), la, log[-2500:]))
    check("R①: 본체를 3초 기다린 뒤 TERM·=== 종료 (성공: true) ===", "시간 내에 안 죽어서 강제 종료합니다." in log
          and "=== 종료 (성공: true) ===" in log, log[-2000:])
    check(f"R①: backup-{d_ver}·README 표식 E", (WORK / f"backup-{d_ver}" / "pyproject.toml").is_file()
          and readme_first_line() == IX["variants"]["E"]["readme"], readme_first_line())
    wait_for(lambda: (DATA / f"launcher-ensured-{e_ver}").is_file(), 30)
    common_after_update("E", inc0, launcher_name="E")


# ---------------------------------------------------------------- S8

@step("S8", "제거 — --yes(데이터 남김) → 다시 설치 → --purge")
def s8():
    w = subprocess.Popen(["/bin/sh", str(LAUNCHER), "watch", "10"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        check("제거 전: watch·Host·브로커가 돌고 있음", wait_for(lambda: watch_pids() == [w.pid], 10) and host_pids()
              and broker_pids(), (watch_pids(), host_pids(), broker_pids()))
        settings0 = raw(SETTINGS)
        hp, bp = runtime().get("pid"), KEEP["broker"]
        r = run(["/bin/sh", APP / "uninstall.sh", "--yes"], timeout=240)
        save("s8-uninstall", r)
        out = r.stdout + r.stderr
        check("uninstall --yes: 종료코드 0·제거 완료", r.returncode == 0 and "=== 제거 완료 ===" in r.stdout, out_of(r))
        # 첫 길(watch.lock.d·런처 stop·ptyd.json)로 멈췄는지 — 남은 것 쓸기(sweep)는 둘째 방어선이라 여기서 쓰이면 안 된다
        said = [f"감시(watch) 를 멈췄습니다(pid {w.pid})", f"Host 를 멈췄습니다(pid {hp})",
                f"터미널 관리 프로세스(브로커) 를 멈췄습니다(pid {bp})"]
        check("제거: watch·Host·브로커를 제 길(표식·런처·ptyd.json)로 멈춤, 남은 것 쓸기는 빈손",
              all(s in out for s in said) and "남은 Host" not in out and "남은 브로커" not in out, out)
        gone = wait_for(lambda: not (host_pids() or broker_pids() or watch_pids() or app_pids()), 15)
        check("server·ptyd·watch 프로세스 0개(/proc)", gone, (host_pids(), broker_pids(), watch_pids(), app_pids()))
        check("설치 폴더 없음·데이터 폴더 남음·maintenance.flag 없음", not APP.exists() and DATA.is_dir()
              and not (DATA / "maintenance.flag").exists(), sorted(os.listdir(CH)))
        check("projects 해시 그대로", tree_hash(PROJECTS) == KEEP["projects"])
        check("settings.json 제거 전후 바이트 같음·남은 훅 안내", raw(SETTINGS) == settings0
              and "Claude 설정에 ClewPath 훅이 5개 남아 있습니다(지우지 않았습니다)" in out, out)
        check("uv 는 남김(--purge-uv 아님)", (CH / "bin" / "uv").is_file())
    finally:
        reap(w)
    r = get_sh(*INSTALL_ARGS)
    save("s8-reinstall", r)
    out = r.stdout + r.stderr
    check("다시 설치(S2 명령): 데이터·자격증명 재사용", r.returncode == 0 and "=== 설치 완료 ===" in r.stdout
          and "기존 설정 파일 유지" in out and "이미 발급된 자격증명이 있습니다(재사용)" in out, out_of(r), fatal=True)
    check("다시 설치한 Host 가 뜸", host_up(30), logs_hint())
    settings1 = raw(SETTINGS)
    r = run(["/bin/sh", APP / "uninstall.sh", "--purge", "--yes"], timeout=240)
    save("s8-purge", r)
    check("uninstall --purge --yes: 종료코드 0", r.returncode == 0 and "=== 제거 완료 ===" in r.stdout, out_of(r))
    check("--purge: 데이터 폴더·bin·python·ClewPath 폴더까지 없음", not DATA.exists() and not (CH / "bin").exists()
          and not (CH / "python").exists() and not CH.exists(), (DATA.exists(), CH.exists() and sorted(os.listdir(CH))))
    check("--purge: projects·settings.json 그대로", tree_hash(PROJECTS) == KEEP["projects"] and raw(SETTINGS) == settings1)
    check("--purge: 프로세스 0개", wait_for(lambda: not (host_pids() or broker_pids() or app_pids()), 15),
          (host_pids(), broker_pids(), app_pids()))


# ---------------------------------------------------------------- 마무리

def final_requests() -> None:
    CUR["step"] = "CP"
    dl = {ver(n) for n in ("T", "Z-dotdot", "Z-abs", "Z-link", "L-noinstall", "A", "B", "C", "D")}
    allowed = {"GET /cp/healthz", "GET /cp/get.sh", "GET /cp/updates/manifest", "POST /cp/provision/anonymous",
               "POST /cp/connector/token"} | {f"GET /cp/updates/download/{v}" for v in dl}
    rs = reqs()
    odd = [f"{r['m']} {r['p']} → {r['s']} ({r.get('ua', '')})" for r in rs if f"{r['m']} {r['p']}" not in allowed]
    check("요청 로그: 예상 밖 경로 없음", not odd, odd[:20])
    prov = [r for r in rs if r["p"] == "/cp/provision/anonymous"]
    check("요청 로그: 익명 발급은 한 번(설치 때)", len(prov) == 1, prov)
    n_c = sum(r["p"] == f"/cp/updates/download/{ver('C')}" for r in rs)
    check("요청 로그: 롤백된 버전(C)은 다시 받지 않음(많아야 한 번)", n_c <= 1 and (n_c == 1 or "S6" not in TIMES), n_c)


def dump_logs() -> None:
    print("========== 로그 꼬리(실패 진단용) ==========", flush=True)
    files = [DATA / "launcher.log", DATA / "host.err.log", DATA / "host.log", WORK / "recover.log",
             *sorted(WORK.glob("apply-*.log")), STATE / "fake_cp.out", *sorted(OUT.glob("*.log"))[-3:]]
    for p in files:
        if p.is_file():
            print(f"----- {p} (마지막 60줄)\n{tail(p, 60)}", flush=True)


def main() -> int:
    for s in (sys.stdout, sys.stderr):              # 거부 안내도 깨지지 않게(Windows 콘솔 cp949 에서 불려도)
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    guard()
    global CP
    os.umask(UMASK)
    IX.update(json.loads((FIX / "index.json").read_text(encoding="utf-8")))
    CP = IX["cp"]
    INSTALL_ARGS[:] = ["--cp-url", CP, "--allow-http", "--relay-url", RELAY, "--app-url", APP_URL]
    print(f"[e2e] 기본 zip: {IX['base_zip']} / 시험 kid {IX['test_kid']} / uv {IX['uv_target']}", flush=True)
    (PROJECTS / "x").mkdir(parents=True)
    (PROJECTS / "x" / "11111111-2222-3333-4444-555555555555.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": str(HOME / "proj"), "message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8")
    cp = None
    t_all = time.monotonic()
    try:
        CUR["step"] = "준비"
        cp = start_fake_cp()
        for sid, title, fn in STEPS:
            CUR["step"] = sid
            print(f"=== {sid} {title}", flush=True)
            t0 = time.monotonic()
            try:
                fn()
            except Abort as e:
                print(f"  중단: {e}", flush=True)
                break
            except Exception:  # noqa: BLE001
                check(f"{sid} 예외", False, traceback.format_exc())
                break
            finally:
                TIMES[sid] = round(time.monotonic() - t0, 1)
                print(f"  ({TIMES[sid]}초)", flush=True)
        final_requests()
    except Abort:
        pass
    finally:
        if cp is not None:
            cp.terminate()
            try:
                cp.wait(5)
            except subprocess.TimeoutExpired:
                cp.kill()
    bad = [r for r in RESULTS if not r["ok"]]
    ran = {sid for sid, _, _ in STEPS if sid in TIMES}
    missing = [sid for sid, _, _ in STEPS if sid not in ran]
    if bad or missing:
        dump_logs()
    summary = {"ok": not bad and not missing, "passed": len(RESULTS) - len(bad), "failed": len(bad),
               "not_run": missing, "seconds": round(time.monotonic() - t_all, 1), "times": TIMES,
               "base_zip": IX.get("base_zip"), "test_kid": IX.get("test_kid"), "results": RESULTS}
    print(f"=== {summary['passed']}/{len(RESULTS)} 통과, 실패 {len(bad)}"
          + (f", 돌지 않은 단계 {missing}" if missing else "") + f" ({summary['seconds']}초)", flush=True)
    print(RESULT_TAG + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
