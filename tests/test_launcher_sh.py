"""맥·리눅스 멱등 런처 실행 시험 — docs/designs/posix-install.md 6.2 L①~L⑪ (+ S16·S27·S38).

진짜 템플릿(session_manager/start-connector.template.sh)을 ensure_launcher 로 가짜 설치 루트에 렌더하고 실제로 돌린다.
- 설치 루트·데이터 폴더 경로에 공백·한글·작은따옴표가 들어 있다(맥 `Application Support`·한글 사용자 이름 대비).
- 가짜 venv: `python -m venv --without-pip` + 지금 파이썬의 site-packages 를 가리키는 .pth(런처 판정이 psutil 을 쓴다).
- 가짜 Host(session_manager/server.py): toml 의 [server] port 로 /api/health 를 서빙하고 runtime.json 을 쓴다. 받은
  env·umask·cwd·세션 id 를 fake-<pid>.json 으로 남긴다. [fake] 표로 동작을 바꾼다
  (ignore_term·healthy·exit_now·exit_code·delay).
- 가짜 브로커(session_manager/ptyd.py): ptyd.json 을 쓰고 잔다 — stop 이 건드리지 않는지 본다.
- 가짜 패키지에 없는 모듈(liveness·config)은 진짜 패키지에서 온다(__path__ 확장) — 강제 stop 의 표식이 진짜
  liveness.mark_shutdown 으로 남는지까지 본다.
- 판정의 네 갈래(psutil / venv 에 psutil 없음 / /proc 없음 = 맥의 ps 길 / 둘 다 없음 = psutil 없는 맥)는 variant 픽스처로 같은
  시나리오를 돌린다.
Windows 에선 건너뛴다(리눅스 검증 컨테이너 `docker run --init`·맥에서 돈다). 정리는 설치 루트가 명령줄에 든 프로세스를
모두 죽이는 것으로 한다.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 런처(start-connector.sh) — POSIX 전용")

if sys.platform != "win32":
    import psutil

import session_manager
from session_manager import hostctl
from session_manager.osdep import _posix

REAL_PKG = Path(session_manager.__file__).resolve().parent
TEMPLATE = REAL_PKG / _posix.LAUNCHER_TEMPLATE
DEAD_PID = 4_194_999          # 리눅스 pid_max 상한(2^22)·맥(99998)보다 크다 — 있을 수 없는 pid
SHELLS = [("/bin/sh",)] + ([("bash", "--posix")] if shutil.which("bash") else [])
with_shells = pytest.mark.parametrize("shell", SHELLS, ids=[" ".join(s) for s in SHELLS])

FAKE_SERVER = r'''"""가짜 Host — tests/test_launcher_sh.py 가 쓴다."""
import json, os, signal, sys, time, tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONF = Path(os.environ["CLEWPATH_CONFIG"])
DATA = CONF.parent
CFG = tomllib.loads(CONF.read_text(encoding="utf-8"))
FAKE = CFG.get("fake", {})
PORT = int(CFG["server"]["port"])
UMASK = os.umask(0)
os.umask(UMASK)
IGN = [n for n in ("SIGHUP", "SIGINT", "SIGQUIT") if signal.getsignal(getattr(signal, n)) == signal.SIG_IGN]
(DATA / f"fake-{os.getpid()}.json").write_text(json.dumps({
    "env": dict(os.environ), "umask": UMASK, "cwd": os.getcwd(), "sid": os.getsid(0), "sig_ign": IGN}),
    encoding="utf-8")
print(f"fake host {os.getpid()} up", flush=True)
print(f"fake host {os.getpid()} err", file=sys.stderr, flush=True)
if FAKE.get("exit_now"):
    sys.exit(int(FAKE.get("exit_code", 3)))


def _term(signum, frame):
    (DATA / f"term-{os.getpid()}").write_text("TERM", encoding="utf-8")
    os._exit(0)


signal.signal(signal.SIGTERM, signal.SIG_IGN if FAKE.get("ignore_term") else _term)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health" and FAKE.get("healthy", True):
            body, code = {"status": "ok"}, 200
        elif self.path == "/api/owner/diagnostics":
            body, code = {"claude_home": str(DATA.parent), "version": "0.0.0"}, 200
        else:
            body, code = {"error": "unavailable"}, 503
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


time.sleep(float(FAKE.get("delay", 0)))         # 진짜 Host 처럼 import·준비에 시간이 걸리는 경우
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
(DATA / "runtime.json").write_text(json.dumps({
    "host": "127.0.0.1", "port": PORT, "url": f"http://127.0.0.1:{PORT}/", "pid": os.getpid(),
    "started_at": int(time.time())}, indent=2), encoding="utf-8")
srv.serve_forever()
'''

FAKE_BROKER = r'''"""가짜 터미널 관리 프로세스(브로커)."""
import json, os, time
from pathlib import Path

data = Path(os.environ["FAKE_DATA_DIR"])
(data / "ptyd.json").write_text(json.dumps({"pid": os.getpid(), "address": str(data / "ptyd.sock"), "proto": 1,
                                            "hash": "fake", "started": int(time.time())}), encoding="utf-8")
time.sleep(600)
'''

# 런처가 부르는 러너 사본(-RecoverOnly) 대역 — 받은 인자를 적고 교체 잔재를 치운다. FAKE_RUNNER_RC 로 실패를 흉내 낸다.
FAKE_RUNNER = """#!/bin/sh
out="$(dirname "$0")/recover-args"
: >"$out"
for a in "$@"; do printf '%s\\n' "$a" >>"$out"; done
root=
while [ $# -gt 0 ]; do
  case "$1" in -InstallRoot) shift; root=$1 ;; esac
  shift
done
[ "${FAKE_RUNNER_RC:-0}" = 0 ] || exit "$FAKE_RUNNER_RC"
rm -rf "$root"/.clewpath-swap.*
exit 0
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class Inst:
    """가짜 설치본 하나 — 설치 루트·venv·가짜 패키지·데이터 폴더·렌더된 런처."""

    def __init__(self, tmp: Path, with_psutil: bool = True):
        base = tmp.resolve()                       # 런처의 ROOT 는 pwd -P(물리 경로) — 맥 /var → /private/var
        self.root = base / "My Apps" / "클루패스 it's app"
        self.home = base / "home x"
        self.data = self.home / ".claude" / "session_manager"
        self.conf = self.data / "clewpath.toml"
        self.launcher = self.root / _posix.START_SCRIPT
        self.py = self.root / ".venv" / "bin" / "python"
        self.port = _free_port()
        self.procs: list[subprocess.Popen] = []
        pkg = self.root / "session_manager"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(f"__path__.append({str(REAL_PKG)!r})\n", encoding="utf-8")
        (pkg / "server.py").write_text(FAKE_SERVER, encoding="utf-8")
        (pkg / "ptyd.py").write_text(FAKE_BROKER, encoding="utf-8")
        shutil.copyfile(TEMPLATE, pkg / _posix.LAUNCHER_TEMPLATE)
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")],
                       check=True, capture_output=True, timeout=120)
        if with_psutil:                            # 없으면 런처 판정이 psutil 없는 길(/proc)로 간다
            site = next((self.root / ".venv" / "lib").glob("python3*/site-packages"))
            deps = sorted({sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]})
            (site / "_clewpath_test_deps.pth").write_text("\n".join(deps) + "\n", encoding="utf-8")
        self.data.mkdir(parents=True)
        self.conf_write()
        assert _posix.ensure_launcher(self.root, self.data, self.conf, self.port) == "launcher: regenerated (posix v2)"

    # ---- 준비
    def conf_write(self, **fake) -> None:
        lines = ["[server]", f"port = {self.port}", "", "[fake]"] + [f"{k} = {json.dumps(v)}" for k, v in fake.items()]
        self.conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self, **extra) -> dict:
        # 데이터 폴더를 정하는 env(conftest 가 SESSION_MANAGER_CLAUDE_HOME 을 넣는다)도 뺀다 — 설치 때처럼 HOME 아래 기본값
        e = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLEWPATH_UPDATER",
                          "SESSION_MANAGER_CLAUDE_HOME", "CLAUDE_CONFIG_DIR")
             and not k.upper().startswith("CLAUDE_CODE_")}
        e["HOME"] = str(self.home)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def popen(self, argv, **kw) -> subprocess.Popen:
        kw.setdefault("stdin", subprocess.DEVNULL)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        p = subprocess.Popen(argv, **kw)
        self.procs.append(p)
        return p

    def holder(self, name: str) -> subprocess.Popen:
        """명령줄에 name 이 든 살아 있는 프로세스(잠금·표식의 주인 흉내). 명령이 하나뿐이면 sh 가 exec 로 sleep 이 돼
        명령줄에서 name 이 사라지므로 하나 더 붙인다."""
        return self.popen(["sh", "-c", "sleep 120; exit 0", name])

    def start_broker(self) -> int:
        self.popen([str(self.py), "-m", "session_manager.ptyd"], cwd=str(self.root), start_new_session=True,
                   env=self.env(FAKE_DATA_DIR=self.data))
        assert _wait(lambda: (self.data / "ptyd.json").is_file(), 10)
        return json.loads((self.data / "ptyd.json").read_text(encoding="utf-8"))["pid"]

    def marker(self, pid: int, by: str = "runner", age: int = 0) -> Path:
        p = self.data / "update.inprogress"
        p.write_text(f"pid={pid}\nat={int(time.time()) - age}\nby={by}\n", encoding="utf-8")
        return p

    # ---- 실행
    def run(self, *args, env=None, shell=("/bin/sh",), timeout=90, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run([*shell, str(self.launcher), *args], env=env or self.env(), capture_output=True,
                              timeout=timeout, cwd=str(cwd or self.home))

    # ---- 관찰
    def runtime(self) -> dict:
        try:
            return json.loads((self.data / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def health(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as r:
                return json.loads(r.read()) == {"status": "ok"}
        except Exception:  # noqa: BLE001
            return False

    def host_pids(self) -> list[int]:
        out = []
        for pr in psutil.process_iter(["pid", "cmdline", "status"]):
            c = pr.info["cmdline"] or []
            if c[:3] == [str(self.py), "-m", "session_manager.server"] and pr.info["status"] != psutil.STATUS_ZOMBIE:
                out.append(pr.info["pid"])
        return sorted(out)

    def fake(self, pid: int) -> dict:
        return json.loads((self.data / f"fake-{pid}.json").read_text(encoding="utf-8"))

    def text(self, name: str) -> str:
        try:
            return (self.data / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def llog(self) -> str:
        return self.text("launcher.log")

    def diag(self, r: subprocess.CompletedProcess | None = None) -> str:
        head = "" if r is None else (f"rc={r.returncode}\nstdout={r.stdout.decode('utf-8', 'replace')}\n"
                                     f"stderr={r.stderr.decode('utf-8', 'replace')}\n")
        return (head + "--- launcher.log\n" + self.llog()[-2000:] + "\n--- host.err.log\n"
                + self.text("host.err.log")[-1500:])

    def kill_host(self) -> None:
        for pid in self.host_pids():
            os.kill(pid, signal.SIGKILL)
            assert _wait(lambda pid=pid: _gone(pid), 10)

    def started(self, r) -> int:
        """start 가 Host 를 띄웠다 — 그 pid."""
        assert r.returncode == 0, self.diag(r)
        assert self.health(), self.diag(r)
        pids = self.host_pids()
        assert len(pids) == 1 and self.runtime().get("pid") == pids[0], (pids, self.runtime(), self.diag(r))
        return pids[0]

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        me, key = os.getpid(), str(self.root)
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                if pr.info["pid"] != me and any(key in a for a in (pr.info["cmdline"] or [])):
                    pr.kill()
            except psutil.Error:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@pytest.fixture
def inst(tmp_path):
    i = Inst(tmp_path)
    try:
        yield i
    finally:
        i.cleanup()


@pytest.fixture(params=["psutil", "no-psutil", "no-procfs", "no-psutil-no-procfs"])
def variant(request, tmp_path):
    """판정의 네 갈래: 보통(psutil) / venv 에 psutil 이 없을 때(파이썬이 /proc 를 읽음) / /proc 가 없을 때(맥 —
    셸이 ps 로 명령줄·상태를 읽는다. 리눅스에서는 런처의 /proc 경로를 없는 곳으로 바꿔 흉내 낸다) / 둘 다 없을 때(psutil 없는
    맥 — 파이썬 판정도 ps 를 읽는다. 맥 실기 3b: 재사용된 pid 를 '기동 중'으로 보고 Host 를 띄우지 않았다)."""
    procfs = request.param in ("no-procfs", "no-psutil-no-procfs")
    if procfs and not (Path("/proc/self").is_dir() and shutil.which("ps")):
        pytest.skip("/proc 와 ps 가 둘 다 있는 리눅스에서만 흉내 낸다(맥은 기본 시험이 이미 이 길)")
    i = Inst(tmp_path, with_psutil=request.param not in ("no-psutil", "no-psutil-no-procfs"))
    if procfs:
        t = i.launcher.read_text(encoding="utf-8")
        assert t.count('"/proc/') >= 3 and "/proc/self" in t
        i.launcher.write_bytes(t.replace('"/proc/', '"/nonexistent-proc/').replace("/proc/self", "/nonexistent-proc/self")
                               .encode("utf-8"))
    try:
        yield i
    finally:
        i.cleanup()


# ---------------------------------------------------------------- L① 멱등

@with_shells
def test_start_twice_runs_one_host(inst, shell):
    pid = inst.started(inst.run("start", shell=shell))
    r = inst.run("start", shell=shell)
    assert r.returncode == 0, inst.diag(r)
    assert inst.host_pids() == [pid], "두 번째 start 는 아무것도 띄우지 않는다"
    log = inst.llog()
    assert log.count("spawned pid") == 1 and f"spawned pid {pid}" in log and "runtime.json 없음" in log
    assert inst.text("host.log").count("=== launcher start") == 1
    assert inst.run().returncode == 0 and inst.host_pids() == [pid], "인자 없이 부르면 start"


def test_concurrent_starts_spawn_once(inst):
    """동시에 여럿이 start 해도(감시자·사람·러너) 한 번만 띄운다 — mkdir 잠금 + 잠금 안 재판정."""
    procs = [inst.popen(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), cwd=str(inst.home))
             for _ in range(5)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert len(list(inst.data.glob("fake-*.json"))) == 1, inst.diag()
    assert len(inst.host_pids()) == 1 and inst.health()


def test_start_holds_the_lock_until_the_host_registers(inst):
    """Host 가 runtime.json 을 쓰기 전(import·준비 중)에 온 두 번째 start 가 '죽음'으로 읽고 겹쳐 띄우지 않는다 —
    첫 start 는 새 pid 가 runtime.json 에 적힐 때까지 잠금을 쥔다."""
    inst.conf_write(delay=2)
    first = inst.popen(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), cwd=str(inst.home))
    assert _wait(lambda: list(inst.data.glob("fake-*.json")), 10)          # 띄웠고 아직 준비 중
    r = inst.run("start")
    assert r.returncode == 0 and first.wait(timeout=30) == 0, inst.diag(r)
    assert len(list(inst.data.glob("fake-*.json"))) == 1, inst.diag(r)
    assert len(inst.host_pids()) == 1 and inst.health()


def test_relative_invocation_ignores_cdpath(inst, tmp_path):
    """$0 이 상대 경로여도 ROOT 를 물리 경로로 구한다. 사용자의 CDPATH 에 같은 이름 폴더가 있어도 끌려가지 않는다
    (cd 가 CDPATH 로 찾으면 그 폴더로 가면서 경로까지 찍는다)."""
    decoy = tmp_path / "decoy" / inst.root.name
    decoy.mkdir(parents=True)
    r = subprocess.run(["/bin/sh", f"{inst.root.name}/{_posix.START_SCRIPT}", "paths"], cwd=str(inst.root.parent),
                       env=inst.env(CDPATH=str(decoy.parent)), capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode()
    assert r.stdout.decode("utf-8").splitlines()[0] == f"root={inst.root}"
    r = subprocess.run(["/bin/sh", _posix.START_SCRIPT, "start"], cwd=str(inst.root), env=inst.env(),
                       capture_output=True, timeout=60)
    inst.started(r)


# ---------------------------------------------------------------- L② kill -9 뒤 재기동, 로그 이어 쓰기

def test_restart_after_kill_appends_logs(inst):
    p1 = inst.started(inst.run("start"))
    os.kill(p1, signal.SIGKILL)
    assert _wait(lambda: _gone(p1), 10)
    p2 = inst.started(inst.run("start"))
    assert p2 != p1
    host_log, err_log = inst.text("host.log"), inst.text("host.err.log")
    assert f"fake host {p1} up" in host_log and f"fake host {p2} up" in host_log, "이어 쓰기 — 죽은 Host 의 꼬리가 남는다"
    assert f"fake host {p1} err" in err_log and f"fake host {p2} err" in err_log
    assert host_log.count("=== launcher start") == 2
    assert host_log.index(f"fake host {p1} up") < host_log.rindex("=== launcher start") < host_log.index(f"fake host {p2} up")
    assert f"이전 pid {p1} 없음(health 없음) → 기동" in inst.llog()


# ---------------------------------------------------------------- L③ maintenance.flag

def test_maintenance_flag_blocks_until_resume(inst):
    flag = inst.data / "maintenance.flag"
    flag.write_text("by=test\n", encoding="utf-8")
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [] and not inst.runtime(), inst.diag(r)
    st = inst.run("status")
    assert st.returncode == 3 and _kv(st.stdout.decode())["state"] == "maintenance"
    inst.started(inst.run("resume"))
    assert not flag.exists() and "resume: maintenance.flag 제거" in inst.llog()


def test_stop_hold_then_resume(inst):
    pid = inst.started(inst.run("start"))
    r = inst.run("stop", "--hold")
    assert r.returncode == 0 and f"stopped (pid {pid})" in r.stdout.decode(), inst.diag(r)
    assert "by=stop" in (inst.data / "maintenance.flag").read_text(encoding="utf-8")
    assert inst.run("start").returncode == 0 and inst.host_pids() == [], "보류 중엔 띄우지 않는다"
    assert inst.run("status").returncode == 3
    inst.started(inst.run("resume"))


# ---------------------------------------------------------------- L④ 잠금

def test_live_launcher_lock_defers(inst):
    lock = inst.data / "launcher.lock"
    lock.mkdir()
    holder = inst.holder(_posix.START_SCRIPT)                     # 명령줄에 런처 이름이 든 살아 있는 프로세스
    (lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [], inst.diag(r)
    assert "skip: 다른 런처가 기동 중" in inst.llog()
    assert (lock / "pid").read_text(encoding="utf-8").strip() == str(holder.pid), "남의 잠금은 그대로"
    (lock / "pid").unlink()                                       # 막 잡은(아직 pid 를 못 쓴) 잠금도 기다린다
    assert inst.run("start").returncode == 0 and inst.host_pids() == []


@pytest.mark.parametrize("corpse", ["dead-pid", "foreign-pid", "old-no-pid"])
def test_dead_launcher_lock_is_taken_over(inst, corpse):
    lock = inst.data / "launcher.lock"
    lock.mkdir()
    if corpse == "dead-pid":
        (lock / "pid").write_text(f"{DEAD_PID}\n", encoding="utf-8")
    elif corpse == "foreign-pid":                                 # pid 가 다른 프로그램에 재사용됨(명령줄에 런처 없음)
        (lock / "pid").write_text(f"{inst.holder('something-else').pid}\n", encoding="utf-8")
    else:                                                         # pid 없이 2분 넘게 묵음
        old = time.time() - 300
        os.utime(lock, (old, old))
    inst.started(inst.run("start"))
    assert not lock.exists(), "기동 뒤 잠금을 푼다"


# ---------------------------------------------------------------- L⑤ stop

@with_shells
def test_stop_terms_host_and_spares_broker(inst, shell):
    pid = inst.started(inst.run("start", shell=shell))
    broker = inst.start_broker()
    r = inst.run("stop", shell=shell)
    assert r.returncode == 0 and r.stdout.decode().strip() == f"stopped (pid {pid})", inst.diag(r)
    assert (inst.data / f"term-{pid}").is_file(), "SIGTERM 으로 끝냈다(정상 종료 표식·bye 를 남길 기회)"
    assert _gone(pid) and not _gone(broker), "브로커(터미널 관리 프로세스)는 건드리지 않는다"
    assert f"stop: SIGTERM pid {pid}" in inst.llog()
    r = inst.run("stop", shell=shell)
    assert r.returncode == 0 and r.stdout.decode().strip() == "not running"


def test_stop_never_signals_a_foreign_pid(inst):
    """runtime.json 의 pid 가 이 설치본의 Host 가 아니면(재사용·다른 설치본·같은 모듈 이름) 신호를 보내지 않는다(S27)."""
    sleeper = inst.popen(["sleep", "120"])
    same_module = inst.popen([sys.executable, "-c", "import time; time.sleep(120)", "session_manager.server"])
    for victim in (sleeper, same_module):
        (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": victim.pid}), encoding="utf-8")
        r = inst.run("stop")
        assert r.returncode == 0 and r.stdout.decode().strip() == "not running", inst.diag(r)
        time.sleep(0.2)
        assert victim.poll() is None, "남의 프로세스는 살아 있어야 한다"


def test_stop_kills_after_15s_and_marks_forced_stop(inst):
    inst.conf_write(ignore_term=True)
    pid = inst.started(inst.run("start"))
    t0 = time.monotonic()
    r = inst.run("stop", timeout=60)
    took = time.monotonic() - t0
    assert r.returncode == 0 and r.stdout.decode().strip() == f"stopped (pid {pid}, forced)", inst.diag(r)
    assert 14 <= took < 30, took
    assert _gone(pid) and not (inst.data / f"term-{pid}").exists()
    assert "stop: forced" in inst.llog()
    marks = json.loads((inst.data / "shutdown.json").read_text(encoding="utf-8"))["pids"]
    assert marks[str(pid)]["reason"] == "forced-stop", "다음 기동이 사고(비정상 종료)로 보지 않게"


# ---------------------------------------------------------------- L⑥ 업데이트 표식

def test_update_marker_blocks_supervisor_but_not_the_runner(inst):
    runner = inst.holder("update_runner.sh")
    marker = inst.marker(runner.pid)
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [] and marker.exists(), inst.diag(r)
    assert "skip: 업데이트 진행 중" in inst.llog()
    st = inst.run("status")
    assert st.returncode == 1 and _kv(st.stdout.decode())["state"] == "updating"
    pid = inst.started(inst.run("start", env=inst.env(CLEWPATH_UPDATER=1)))    # 러너 자신의 재기동
    assert "CLEWPATH_UPDATER" not in inst.fake(pid)["env"], "러너 표식은 Host 로 넘기지 않는다"
    assert marker.exists(), "러너의 표식은 러너가 지운다"


@pytest.mark.parametrize("why", ["dead-pid", "too-old", "foreign-pid"])
def test_stale_update_marker_is_removed(inst, why):
    if why == "dead-pid":
        marker = inst.marker(DEAD_PID)
    elif why == "too-old":                                        # 30분 넘음 — 러너가 살아 있어도 버려진 표식
        marker = inst.marker(inst.holder("update_runner.sh").pid, age=3600)
    else:                                                         # pid 가 다른 프로그램(명령줄에 update_runner 없음)
        marker = inst.marker(inst.holder("something-else").pid)
    inst.started(inst.run("start"))
    assert not marker.exists() and "stale update marker 제거" in inst.llog()


# ---------------------------------------------------------------- L⑦ 좀비·pid 재사용

def test_zombie_pid_counts_as_dead(inst):
    z = inst.popen([str(inst.py), "-c", "pass", "-m", "session_manager.server"])    # 끝났지만 아무도 거두지 않는다
    assert _wait(lambda: psutil.Process(z.pid).status() == psutil.STATUS_ZOMBIE, 10)
    (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": z.pid,
                                                        "started_at": int(time.time())}), encoding="utf-8")
    pid = inst.started(inst.run("start"))
    assert pid != z.pid and f"이전 pid {z.pid} 없음" in inst.llog()


def test_reused_pid_counts_as_dead(inst):
    sleeper = inst.popen(["sleep", "120"])
    (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": sleeper.pid,
                                                        "started_at": int(time.time())}), encoding="utf-8")
    inst.started(inst.run("start"))
    assert sleeper.poll() is None


def test_unresponsive_host_is_busy_not_restarted(inst):
    """살아 있지만 health 가 없는 Host — 기동 중이거나 응답 불능. 죽이지도 겹쳐 띄우지도 않는다."""
    inst.conf_write(healthy=False)
    r = inst.run("start")
    assert r.returncode == 0, inst.diag(r)
    pids = inst.host_pids()
    assert len(pids) == 1 and not inst.health()
    st = inst.run("status")
    assert st.returncode == 1 and _kv(st.stdout.decode())["state"] == "starting"
    assert _kv(st.stdout.decode())["pid"] == str(pids[0])
    assert inst.run("start").returncode == 0 and inst.host_pids() == pids
    assert f"skip: pid {pids[0]} 살아 있음" in inst.llog()


def test_clock_step_does_not_turn_a_live_host_into_a_corpse(inst):
    """리눅스 psutil 의 create_time 은 지금의 부팅 시각(/proc/stat btime)으로 계산해 시계가 앞으로 뛰면 같이 밀린다(WSL2·VM
    재개·NTP 첫 동기화). 기동 때 적은 started_at 과 시각을 비교하면 살아 있는 Host 를 'pid 재사용'으로 보고, 응답이 잠깐 없는
    틱에 두 번째 Host 를 띄운다(S21). 시계 이동은 started_at 을 그만큼 앞당겨 흉내 낸다(비교되는 차이가 같다)."""
    inst.conf_write(healthy=False)                                # 살아 있지만 응답 없음(busy)
    r = inst.run("start")
    assert r.returncode == 0, inst.diag(r)
    pids = inst.host_pids()
    assert len(pids) == 1
    rt = inst.runtime()
    rt["started_at"] -= 3600                                      # 기동 뒤 시계가 한 시간 앞으로 뛰었다
    (inst.data / "runtime.json").write_text(json.dumps(rt), encoding="utf-8")
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == pids, inst.diag(r)
    assert len(list(inst.data.glob("fake-*.json"))) == 1, "두 번째 Host 를 띄우지 않는다"
    assert f"skip: pid {pids[0]} 살아 있음" in inst.llog()
    assert _kv(inst.run("status").stdout.decode())["state"] == "starting"


def test_host_module_of_another_tree_is_not_this_install(inst):
    """runtime.json 의 pid 가 Host 모듈 이름을 단 남의 트리 프로세스(개발 체크아웃·다른 설치본 — pid 재사용)면 이 설치본의
    Host 가 아니다: 설치 경로(ROOT/.venv/bin/…)까지 본다(S27). 시각 비교 없이 — 그 프로세스는 기록보다 먼저 떴다."""
    other = inst.popen([sys.executable, "-c", "import time; time.sleep(120)", "-m", "session_manager.server"])
    assert _wait(lambda: "session_manager.server" in psutil.Process(other.pid).cmdline(), 10)
    (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": other.pid,
                                                        "started_at": int(time.time()) + 60}), encoding="utf-8")
    pid = inst.started(inst.run("start"))
    assert pid != other.pid and other.poll() is None, "남의 프로세스는 건드리지 않고 이 설치본의 Host 를 띄운다"
    assert f"이전 pid {other.pid} 없음" in inst.llog()


def _ignore_job_signals():
    """nohup(HUP)과 비대화형 셸의 '&'(INT·QUIT)가 하는 일 — README 의 'nohup sh start-connector.sh watch &' 길."""
    for s in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, signal.SIG_IGN)


def test_spawned_host_does_not_inherit_ignored_job_signals(inst):
    """무시(SIG_IGN)는 exec 로 Host → 브로커 → pty 안의 프로그램까지 물려가, 브로커가 죽어도 pty 자식이 SIGHUP 으로 끝나지
    않는다(osdep._posix '주인과 함께 끝나기'). Host 는 새 세션이라 기본 처리여도 터미널 신호는 오지 않는다."""
    r = subprocess.run(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), capture_output=True, timeout=60,
                       cwd=str(inst.home), preexec_fn=_ignore_job_signals)
    pid = inst.started(r)
    assert inst.fake(pid)["sig_ign"] == []
    w = inst.popen(["/bin/sh", str(inst.launcher), "watch", "10"], env=inst.env(), cwd=str(inst.home),
                   preexec_fn=_ignore_job_signals)
    inst.kill_host()
    assert _wait(lambda: inst.runtime().get("pid") not in (None, pid) and inst.health(), 25), inst.diag()
    assert inst.fake(inst.runtime()["pid"])["sig_ign"] == [], "watch 가 되살린 Host 도"
    w.send_signal(signal.SIGTERM)
    w.wait(timeout=10)


def _shell_funcs(path: Path, names: tuple[str, ...]) -> str:
    """셸 스크립트에서 그 이름의 함수 정의만 뽑는다(맨 앞 'name() {' 부터 맨 앞 '}' 까지)."""
    out, on = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not on and any(line.startswith(f"{n}() {{") for n in names):
            on = True
        if on:
            out.append(line)
            if line == "}":
                on = False
    return "\n".join(out) + "\n"


def lock_race(work: Path, funcs: str, contender: str, lock: str, trials: int, n: int) -> list[int]:
    """시체 잠금(죽은 pid) 하나에 n 이 동시에 덤빈다 — 시도마다 '잡았다고 믿은' 수. 잡은 쪽은 모두가 판정을 마칠 때까지 산다
    (그래야 늦게 본 쪽이 산 주인을 시체로 볼 수 없다). 경쟁자 파일 이름이 잠금의 명령줄 표지다."""
    (work / "funcs.sh").write_text(funcs, encoding="utf-8")
    (work / "bin").mkdir()
    script = work / "bin" / contender
    script.write_text(
        "D=$1\nNL='\n'\n. \"$2\"\n"
        f"if {{ lock_try \"$D/{lock}\"; }}; then\n"
        "  printf 'GOT %s\\n' \"$$\" >>\"$D/got\"\n"
        "  while [ ! -e \"$D/release\" ]; do sleep 0.05; done\n"
        "fi\n", encoding="utf-8")
    counts = []
    for t in range(trials):
        d = work / f"t{t}"
        (d / lock).mkdir(parents=True)
        (d / lock / "pid").write_text(f"{DEAD_PID}\n", encoding="utf-8")
        procs = [subprocess.Popen(["/bin/sh", str(script), str(d), str(work / "funcs.sh")],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for _ in range(n)]

        def held():
            try:
                return len((d / "got").read_text(encoding="utf-8").splitlines())
            except OSError:
                return 0
        try:
            _wait(lambda: sum(p.poll() is not None for p in procs) + held() >= n, 30, 0.02)
        finally:
            (d / "release").write_text("", encoding="utf-8")
            for p in procs:
                p.wait(timeout=30)
        counts.append(held())
    return counts


def test_stale_lock_is_taken_over_by_exactly_one(tmp_path):
    """시체 잠금을 여럿이 동시에 치우면 늦게 지운 쪽이 먼저 잡은 쪽의 새 잠금을 지워 둘 다 잡았다고 믿었다(감독자·사람·러너가
    겹치면 Host 둘). 치우는 권리(옆 폴더 mkdir)를 쥔 하나만 다시 보고 지우고 새로 잡는다."""
    funcs = "DATA_DIR=\"$D\"\n" + _shell_funcs(TEMPLATE, ("pid_alive", "cmdline_of", "cmd_has", "boot_id", "lock_live",
                                                         "lock_drop", "lock_reclaim", "lock_acquire"))
    funcs += "lock_try() { lock_acquire \"$1\" start-connector.sh; }\n"
    counts = lock_race(tmp_path, funcs, "start-connector.sh", "launcher.lock", 30, 8)
    assert counts == [1] * len(counts), f"시도마다 잡은 수: {counts}"


def test_runner_stale_lock_is_taken_over_by_exactly_one(tmp_path):
    """러너 잠금(updates/runner.lock.d)도 같은 경합 — 시체 잠금은 러너가 교체 도중 죽었을 때(곧 복구가 필요할 때) 남으므로,
    watch·사람·install 의 -RecoverOnly 가 겹치면 같은 교체 저널을 두 러너가 동시에 되돌렸다. 잡는 길(take_lock_and_mark)과
    같은 함수(runner_lock_take)를 러너에서 그대로 뽑아 쓴다."""
    runner = REAL_PKG / _posix.UPDATE_RUNNER
    funcs = ('WORK="$D"\nLogFile="$D/runner.log"\nLOG_OK=1\nBACKUP_OK=0\nROOT_OK=0\n'
             + _shell_funcs(runner, ("log", "exists", "path_ok", "pid_alive", "cmdline_of", "cmd_has", "lock_live",
                                     "rm_guarded", "lock_reclaim", "runner_lock_take")))
    funcs += "lock_try() { runner_lock_take \"$1\"; }\n"
    counts = lock_race(tmp_path, funcs, "update_runner.sh", "runner.lock.d", 30, 8)
    assert counts == [1] * len(counts), f"시도마다 잡은 수: {counts}"


def test_reclaim_right_left_by_a_dead_reclaimer_expires(inst):
    """치우는 권리 폴더(<잠금>.reclaim)는 치우는 동안(수 ms)만 있다 — 그 사이 죽어 남은 것이 시체 잠금을 영영 막지 않게,
    2분 넘게 묵었으면 치우다 죽은 것으로 본다. 2분이 안 됐으면 다른 런처가 치우는 중이니 기다린다(이번엔 건너뜀)."""
    lock, right = inst.data / "launcher.lock", inst.data / "launcher.lock.reclaim"
    lock.mkdir()
    (lock / "pid").write_text(f"{DEAD_PID}\n", encoding="utf-8")
    right.mkdir()
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [] and lock.is_dir(), inst.diag(r)
    old = time.time() - 300
    os.utime(right, (old, old))
    inst.started(inst.run("start"))
    assert not lock.exists() and not right.exists()


def test_host_that_exits_at_once_is_reported(inst):
    inst.conf_write(exit_now=True, exit_code=7)
    r = inst.run("start")
    assert r.returncode == 1 and "rc=7" in r.stderr.decode(), inst.diag(r)
    assert "fail: 기동 직후 종료" in inst.llog() and inst.host_pids() == []


# ---------------------------------------------------------------- L⑧ env·umask·세션·권한

def test_spawn_env_umask_session_and_data_dir_mode(inst):
    os.chmod(inst.data, 0o755)
    env = inst.env(CLAUDECODE=1, CLAUDE_CODE_ENTRYPOINT="cli", claude_code_lower="x", CLAUDE_PID=7,
                   CLAUDE_EFFORT="high", CLEWPATH_UPDATER=1, KEEP_ME="그대로 두세요", LANG="C.UTF-8",
                   TMPDIR=str(inst.home))
    r = subprocess.run(["/bin/sh", "-c", 'umask 027; exec /bin/sh "$0" start', str(inst.launcher)],
                       env=env, capture_output=True, timeout=60, cwd=str(inst.home))
    pid = inst.started(r)
    got = inst.fake(pid)
    e = got["env"]
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "claude_code_lower", "CLAUDE_PID", "CLAUDE_EFFORT",
              "CLEWPATH_UPDATER"):
        assert k not in e, k
    for k in ("KEEP_ME", "HOME", "PATH", "TMPDIR", "LANG"):
        assert e.get(k) == env[k], k
    assert e["CLEWPATH_CONFIG"] == str(inst.conf), "toml 위치를 고정 — Host 의 데이터 폴더가 런처와 갈리지 않게"
    assert e["PYTHONUNBUFFERED"] == "1" and e["PYTHONIOENCODING"] == "utf-8"
    assert got["umask"] == 0o027, "사용자의 umask 를 그대로 — 그 안의 claude 가 물려받는다(S38)"
    assert got["cwd"] == str(inst.root)
    assert got["sid"] == pid, "새 세션(setsid) — 런처·터미널과 떨어져 산다"
    assert psutil.Process(pid).cmdline()[:3] == [str(inst.py), "-m", "session_manager.server"]
    assert stat.S_IMODE(inst.data.stat().st_mode) == 0o700, "데이터 폴더 0700(S16)"


# ---------------------------------------------------------------- L⑨ watch

def test_watch_revives_host_and_stops_cleanly(inst):
    w = inst.popen(["/bin/sh", str(inst.launcher), "watch", "10"], env=inst.env(), cwd=str(inst.home))
    assert _wait(inst.health, 20), inst.diag()
    p1 = inst.runtime()["pid"]
    r = inst.run("watch", "10", timeout=20)
    assert r.returncode == 0 and w.poll() is None, "둘째 watch 는 바로 끝난다(하나만 돈다)"
    assert _kv(inst.run("status").stdout.decode())["watch_pid"] == str(w.pid)
    os.kill(p1, signal.SIGKILL)
    assert _wait(lambda: inst.runtime().get("pid") not in (None, p1) and inst.health(), 25), inst.diag()
    p2 = inst.runtime()["pid"]
    w.send_signal(signal.SIGTERM)
    assert w.wait(timeout=10) == 0
    assert not (inst.data / "watch.lock.d").exists(), "TERM 이면 잠금을 풀고 끝난다"
    time.sleep(0.5)
    assert inst.host_pids() == [p2] and inst.health(), "Host 는 분리돼 있어 계속 산다"
    log = inst.llog()
    assert "watch: 시작" in log and "watch: 종료" in log
    inst.started(inst.run("start"))                                       # 남은 잠금이 없다


def test_watch_backs_off_when_host_keeps_dying(inst):
    inst.conf_write(exit_now=True)
    w = inst.popen(["/bin/sh", str(inst.launcher), "watch", "10"], env=inst.env(), cwd=str(inst.home))
    assert _wait(lambda: "watch: backoff" in inst.llog(), 45), inst.diag()
    assert inst.llog().count("fail: 기동 직후 종료") >= 3
    w.send_signal(signal.SIGTERM)
    assert w.wait(timeout=10) == 0


# ---------------------------------------------------------------- L⑩ 교체 잔재

def test_swap_leftovers_are_recovered_or_block_the_start(inst):
    swap = inst.root / ".clewpath-swap.4242"
    swap.mkdir()
    r = inst.run("start")
    assert r.returncode == 1 and inst.host_pids() == [], inst.diag(r)
    assert "blocked: 중단된 업데이트 잔재" in inst.llog() and "install.sh" in r.stderr.decode()
    updates = inst.data / "updates"
    updates.mkdir()
    (updates / "update_runner.sh").write_text(FAKE_RUNNER, encoding="utf-8")
    r = inst.run("start", env=inst.env(FAKE_RUNNER_RC=1))          # 복구가 실패하면 막는다
    assert r.returncode == 1 and swap.exists() and inst.host_pids() == [], inst.diag(r)
    assert "blocked: 잔재 복구 실패(rc=1)" in inst.llog()
    inst.started(inst.run("start"))                                # 러너 사본으로 복구한 뒤 띄운다
    assert not swap.exists()
    assert (updates / "recover-args").read_text(encoding="utf-8").splitlines() == [
        "-RecoverOnly", "-InstallRoot", str(inst.root), "-LogFile", str(updates / "recover.log")]


def test_swap_with_live_runner_waits_for_the_runner(inst):
    (inst.root / ".clewpath-swap.777").mkdir()
    lock = inst.data / "updates" / "runner.lock.d"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{inst.holder('update_runner.sh').pid}\n", encoding="utf-8")
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [], inst.diag(r)
    assert "skip: 러너가 교체 중" in inst.llog()
    assert _kv(inst.run("status").stdout.decode())["state"] == "updating"
    inst.started(inst.run("start", env=inst.env(CLEWPATH_UPDATER=1)))  # 러너 자신의 재기동은 통과
    assert (inst.root / ".clewpath-swap.777").exists(), "잔재 정리는 러너 몫"


# ---------------------------------------------------------------- L⑪ paths·status

def test_paths_and_status_output(inst):
    r = inst.run("paths")
    assert r.returncode == 0
    assert r.stdout.decode("utf-8").splitlines() == [
        f"root={inst.root}", f"data_dir={inst.data}", f"conf_file={inst.conf}", f"default_port={inst.port}",
        f"python={inst.py}"]
    st = inst.run("status")
    assert st.returncode == 1
    assert _kv(st.stdout.decode()) == {"state": "stopped", "pid": "", "port": str(inst.port),
                                       "url": f"http://127.0.0.1:{inst.port}/", "broker_pid": "",
                                       "broker_alive": "0", "watch_pid": "", "maintenance": "0"}
    pid = inst.started(inst.run("start"))
    broker = inst.start_broker()
    st = inst.run("status")
    got = _kv(st.stdout.decode())
    assert st.returncode == 0 and got["state"] == "running" and got["pid"] == str(pid)
    assert got["broker_pid"] == str(broker) and got["broker_alive"] == "1"
    for bad in (["nope"], ["start", "x"], ["stop", "--now"], ["watch", "ten"]):
        assert inst.run(*bad).returncode == 2, bad


def test_venv_missing(inst):
    shutil.rmtree(inst.root / ".venv")
    r = inst.run("start")
    assert r.returncode == 2 and "venv" in r.stderr.decode() and "--reinstall" in r.stderr.decode()
    st = inst.run("status")
    assert st.returncode == 1 and _kv(st.stdout.decode())["venv"] == "missing"
    assert inst.run("stop").stdout.decode().strip() == "not running"


# ---------------------------------------------------------------- 판정의 세 갈래(psutil·/proc·ps)

def test_core_lifecycle_on_every_probe_path(variant):
    """멱등 기동 · pid 재사용/좀비는 죽음 · 응답 없는 Host 는 busy · stop 은 이 설치본의 Host 에만 SIGTERM."""
    i = variant
    p1 = i.started(i.run("start"))
    assert i.run("start").returncode == 0 and i.host_pids() == [p1]
    assert _kv(i.run("status").stdout.decode())["pid"] == str(p1)
    i.kill_host()
    sleeper = i.popen(["sleep", "120"])                                   # pid 재사용
    (i.data / "runtime.json").write_text(json.dumps({"port": i.port, "pid": sleeper.pid,
                                                     "started_at": int(time.time())}), encoding="utf-8")
    assert i.run("stop").stdout.decode().strip() == "not running" and sleeper.poll() is None
    p2 = i.started(i.run("start"))
    assert sleeper.poll() is None
    i.kill_host()
    z = i.popen([str(i.py), "-c", "pass", "-m", "session_manager.server"])    # 좀비
    assert _wait(lambda: psutil.Process(z.pid).status() == psutil.STATUS_ZOMBIE, 10)
    (i.data / "runtime.json").write_text(json.dumps({"port": i.port, "pid": z.pid,
                                                     "started_at": int(time.time())}), encoding="utf-8")
    assert i.run("stop").stdout.decode().strip() == "not running"
    p3 = i.started(i.run("start"))
    assert p3 not in (p1, p2, z.pid)
    r = i.run("stop")
    assert r.stdout.decode().strip() == f"stopped (pid {p3})" and (i.data / f"term-{p3}").is_file(), i.diag(r)
    i.conf_write(healthy=False)                                           # 응답 없는 Host
    assert i.run("start").returncode == 0
    busy = i.host_pids()
    assert len(busy) == 1 and i.run("start").returncode == 0 and i.host_pids() == busy
    assert _kv(i.run("status").stdout.decode())["state"] == "starting"
    assert i.run("stop").stdout.decode().strip() == f"stopped (pid {busy[0]})"


def test_zombie_marker_owner_is_dead_on_every_probe_path(variant):
    """update.inprogress 의 pid 가 좀비(끝났는데 아무도 안 거둠)면 시체 — by= 가 없어 명령줄 확인이 없는 표식도."""
    i = variant
    z = i.popen(["sh", "-c", "exit 0"])
    assert _wait(lambda: psutil.Process(z.pid).status() == psutil.STATUS_ZOMBIE, 10)
    marker = i.marker(z.pid, by="")
    i.started(i.run("start"))
    assert not marker.exists() and "stale update marker 제거" in i.llog()


# ---------------------------------------------------------------- foreground([mac] launchd 가 부른다)

def _foreground(inst, sup: Path | None = None, **kw) -> subprocess.Popen:
    """감독자(launchd)처럼 foreground 를 띄운다 — 표준 출력·오류는 sup(launchd 의 StandardOutPath 흉내), 표준 입력은 파이프."""
    if sup is None:
        return inst.popen(["/bin/sh", str(inst.launcher), "foreground"], env=inst.env(), cwd=str(inst.home), **kw)
    with open(sup, "ab") as f:
        return inst.popen(["/bin/sh", str(inst.launcher), "foreground"], env=inst.env(), cwd=str(inst.home),
                          stdin=subprocess.PIPE, stdout=f, stderr=f, **kw)


def test_foreground_becomes_the_host(inst):
    """foreground 는 exec 로 이 프로세스가 곧 Host(pid 그대로 — launchd 가 지켜본다). 표준 입출력은 start 와 같게 바꿔 단다:
    stdin /dev/null, stdout host.log·stderr host.err.log 이어 쓰기 — launchd.log 로 새지 않고, 사고 꼬리(liveness.log_tail)·
    사람의 로그 확인이 start 로 뜬 Host 와 같다. 런처 구분줄(=== launcher start)도 start 와 같은 규칙으로 남긴다."""
    sup = inst.home / "launchd.log"
    p = _foreground(inst, sup)
    assert _wait(lambda: inst.runtime().get("pid") == p.pid and inst.health(), 20), inst.diag()
    assert psutil.Process(p.pid).cmdline()[:3] == [str(inst.py), "-m", "session_manager.server"], "exec — pid 그대로"
    assert inst.fake(p.pid)["env"]["CLEWPATH_CONFIG"] == str(inst.conf)
    if Path(f"/proc/{p.pid}/fd").is_dir():
        fds = {n: os.readlink(f"/proc/{p.pid}/fd/{n}") for n in (0, 1, 2)}
        assert fds == {0: "/dev/null", 1: str(inst.data / "host.log"), 2: str(inst.data / "host.err.log")}, fds
    log = inst.text("host.log")
    assert log.startswith("=== launcher start ") and log.count("=== launcher start") == 1
    assert f"fake host {p.pid} up" in log and f"fake host {p.pid} err" in inst.text("host.err.log")
    assert b"fake host" not in sup.read_bytes(), "Host 의 출력은 감독자의 로그(launchd.log)로 가지 않는다"
    assert f"foreground: runtime.json 없음 → 기동(exec, pid {p.pid})" in inst.llog()
    assert not (inst.data / "launcher.lock").exists(), "잠금은 exec 직전(제 pid 를 적은 뒤)에 놓는다"
    assert json.loads(inst.text("launcher.spawn"))["pid"] == p.pid
    r = inst.run("foreground", timeout=30)                                 # 5분마다의 확인 — 떠 있으면 조용히 0
    assert r.returncode == 0 and inst.host_pids() == [p.pid] and inst.llog().count("foreground:") == 1, inst.diag(r)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0, "정상 종료는 0 — KeepAlive{SuccessfulExit:false} 가 되살리지 않는다"
    (inst.data / "maintenance.flag").write_text("by=test\n", encoding="utf-8")
    r = inst.run("foreground", timeout=30)
    assert r.returncode == 0 and inst.host_pids() == [], "보류 중이면 띄우지 않고 0(감독자가 다시 부르지 않게)"


def test_foreground_refusals_end_with_zero(inst):
    """foreground 가 띄우지 않을 때는 늘 0 — 0 이 아닌 종료는 launchd KeepAlive 에게 'Host 가 죽었다, 곧 다시'라서 곧 풀리지 않는
    사유(남의 서버·venv 없음·잔재 복구 막힘·다른 런처)로 30초마다 쉼 없이 돈다. 사유는 launcher.log(와 표준오류)에 남고 다시
    보는 것은 5분 주기(StartInterval)다. 사유가 풀리면 띄운다."""
    srv = _foreign_server(inst.port)                                        # 남의 서버가 포트를 차지
    try:
        r = inst.run("foreground", timeout=30)
        assert r.returncode == 0 and "다른 프로그램" in r.stderr.decode(), inst.diag(r)
        assert inst.host_pids() == [] and f"fail: 포트 {inst.port}" in inst.llog()
    finally:
        srv.shutdown()
        srv.server_close()
    lock = inst.data / "launcher.lock"                                      # 다른 런처가 기동 중(산 잠금)
    lock.mkdir()
    holder = inst.holder(_posix.START_SCRIPT)
    (lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
    r = inst.run("foreground", timeout=30)
    assert r.returncode == 0 and inst.host_pids() == [] and "foreground: 다른 런처가 기동 중" in inst.llog(), inst.diag(r)
    assert (lock / "pid").read_text(encoding="utf-8") == f"{holder.pid}\n", "남의 잠금은 그대로"
    shutil.rmtree(lock)
    swap = inst.root / ".clewpath-swap.5"                                   # 잔재가 있는데 되돌릴 러너 사본이 없다
    swap.mkdir()
    r = inst.run("foreground", timeout=30)
    assert r.returncode == 0 and "blocked:" in inst.llog() and inst.host_pids() == [], inst.diag(r)
    swap.rmdir()
    shutil.move(str(inst.root / ".venv"), str(inst.root / ".venv-away"))    # venv 가 없다
    r = inst.run("foreground", timeout=30)
    assert r.returncode == 0 and "fail: venv 없음" in inst.llog() and "--reinstall" in r.stderr.decode(), inst.diag(r)
    shutil.move(str(inst.root / ".venv-away"), str(inst.root / ".venv"))
    p = _foreground(inst)                                                   # 사유가 풀리면 띄운다
    assert _wait(lambda: inst.runtime().get("pid") == p.pid and inst.health(), 20), inst.diag()


def test_foreground_rotates_the_log_like_start(inst, monkeypatch):
    """foreground 도 띄우기 직전에 5MB 회전·구분줄 — launchd 아래 Host 는 start 를 거치지 않으므로, 아니면 host.log 가 끝없이
    자라고 새 Host 가 읽는 사고 꼬리(구분줄 앞)가 비었다. 회전이 구분줄 바로 앞이면 꼬리는 host.log.1 에 있다."""
    from session_manager import config, liveness
    with (inst.data / "host.log").open("ab") as f:
        f.write(b"x" * 5_300_000 + b"\nTraceback ... (launchd Host last words)\n")
    p = _foreground(inst)
    assert _wait(lambda: inst.runtime().get("pid") == p.pid and inst.health(), 20), inst.diag()
    assert (inst.data / "host.log.1").is_file() and inst.text("host.log").startswith("=== launcher start")
    monkeypatch.setattr(config, "data_dir", lambda: inst.data)
    assert liveness.log_tail(10)[-1] == "Traceback ... (launchd Host last words)"


def test_foreground_and_start_together_spawn_once(inst):
    """launchd 의 foreground(로그인·5분·KeepAlive)와 사람·러너의 start 가 겹쳐도 한 번만 띄운다 — foreground 도 launcher.lock 을
    쥐고 잠금 안에서 다시 판정하며, 잠금은 제 pid 를 launcher.spawn 에 적은 뒤에야 놓는다."""
    inst.conf_write(delay=1)
    procs = [_foreground(inst) if i % 2 == 0 else
             inst.popen(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), cwd=str(inst.home))
             for i in range(6)]
    assert _wait(inst.health, 30), inst.diag()
    time.sleep(1.5)
    assert len(list(inst.data.glob("fake-*.json"))) == 1, inst.diag()
    assert len(inst.host_pids()) == 1
    alive = [p for p in procs if p.poll() is None]
    assert len(alive) <= 1, "Host 가 된 foreground 하나(또는 start 가 띄웠으면 없음) 말고는 다 끝났다"


# ---------------------------------------------------------------- 2회차 검토(L1~L6·C1·C4)

def _slow_port() -> socket.socket:
    """연결은 받지만(대기열) 답하지 않는 포트 — 런처의 health 가 3초 timeout 까지 붙잡힌다(첫 판정이 도는 사이를 벌린다)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    return s


def _foreign_server(port: int) -> ThreadingHTTPServer:
    """남의 서버 — /api/health 에 {"status":"ok"} 로 답하지만 이 설치본의 Host 가 아니다."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            raw = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _boot_id() -> str:
    """런처가 잠금에 적는 부팅 식별자(템플릿의 boot_id 를 그대로 돌린다). 못 얻는 환경이면 빈 문자열."""
    r = subprocess.run(["/bin/sh", "-c", _shell_funcs(TEMPLATE, ("boot_id",)) + "boot_id\n"], capture_output=True,
                       timeout=10)
    return r.stdout.decode().strip()


def test_slow_first_boot_is_tracked_by_the_spawn_record(inst):
    """[L1] 첫 기동(.pyc 없음)·느린 PC 에서 Host 가 15초 안에 runtime.json 을 못 쓰면 start 는 잠금을 놓는다. 그 사이 status 는
    stopped, stop 은 'not running', 다음 start(watch·사람)는 두 번째 Host 를 띄웠다 — 런처가 띄운 pid(launcher.spawn)도 본다."""
    inst.conf_write(delay=40)
    r = inst.run("start", timeout=60)
    assert r.returncode == 0 and "runtime.json 아직 없음" in inst.llog(), inst.diag(r)
    pids = inst.host_pids()
    assert len(pids) == 1 and not inst.runtime(), (pids, inst.diag(r))
    st = _kv(inst.run("status").stdout.decode())
    assert st["state"] == "starting" and st["pid"] == str(pids[0]), st
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == pids, inst.diag(r)
    assert len(list(inst.data.glob("fake-*.json"))) == 1 and f"skip: pid {pids[0]} 살아 있음" in inst.llog()
    r = inst.run("stop")
    assert r.stdout.decode().strip() == f"stopped (pid {pids[0]})", inst.diag(r)
    assert (inst.data / f"term-{pids[0]}").is_file() and inst.host_pids() == []


def test_dead_pid_in_runtime_json_does_not_hide_the_live_host(inst):
    """[L1] 겹쳐 뜬 쪽(진 쪽)이 runtime.json 을 나중에 덮어쓰고 죽으면 runtime.json 은 죽은 pid — 그 뒤로 살아 있는 Host 를
    런처로 멈출 수 없었다. 런처가 띄운 pid 가 살아 있으면 그것이 Host 다."""
    pid = inst.started(inst.run("start"))
    (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": DEAD_PID}), encoding="utf-8")
    st = _kv(inst.run("status").stdout.decode())
    assert st["state"] == "running" and st["pid"] == str(pid), st
    assert inst.run("start").returncode == 0 and inst.host_pids() == [pid]
    r = inst.run("stop")
    assert r.stdout.decode().strip() == f"stopped (pid {pid})", inst.diag(r)
    assert inst.host_pids() == []


def test_stop_waits_for_the_start_in_progress(inst):
    """[L2] stop 이 진행 중인 start(launcher.lock)를 보지 않아, stop --hold 직후에도 그 start 가 띄운 Host 가 남았다 — 표식을
    쓴 뒤 그 start 가 끝나기를 기다렸다가 판정한다."""
    inst.conf_write(delay=3)
    first = inst.popen(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), cwd=str(inst.home))
    assert _wait(lambda: list(inst.data.glob("fake-*.json")), 10), inst.diag()
    pids = inst.host_pids()
    assert len(pids) == 1
    r = inst.run("stop", "--hold")
    assert r.returncode == 0 and r.stdout.decode().strip() == f"stopped (pid {pids[0]})", inst.diag(r)
    assert first.wait(timeout=30) == 0
    assert inst.host_pids() == [] and (inst.data / "maintenance.flag").is_file()
    assert inst.run("start").returncode == 0 and inst.host_pids() == []


@pytest.mark.parametrize("marker", ["maintenance", "update", "swap"])
def test_start_rechecks_markers_inside_the_lock(inst, marker):
    """[L2] 잠금 안 재판정이 health·pid 만 다시 봤다 — 첫 판정(여기선 health 3초) 사이에 생긴 보류 표식·업데이트 표식·교체
    잔재를 못 보고 띄웠다(러너가 표식을 쓰고 Host 를 내린 순간 늦은 start 가 Host 를 띄우던 자리)."""
    slow = _slow_port()
    try:
        (inst.data / "runtime.json").write_text(json.dumps({"port": slow.getsockname()[1], "pid": DEAD_PID}),
                                                encoding="utf-8")
        p = inst.popen(["/bin/sh", str(inst.launcher), "start"], env=inst.env(), cwd=str(inst.home))
        time.sleep(1)
        assert p.poll() is None, "아직 첫 판정 중(health 3초)"
        if marker == "maintenance":
            (inst.data / "maintenance.flag").write_text("by=test\n", encoding="utf-8")
        elif marker == "update":
            inst.marker(inst.holder("update_runner.sh").pid)
        else:
            lock = inst.data / "updates" / "runner.lock.d"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{inst.holder('update_runner.sh').pid}\n", encoding="utf-8")
            (inst.root / ".clewpath-swap.99").mkdir()
        assert p.wait(timeout=30) == 0, inst.diag()
    finally:
        slow.close()
    assert inst.host_pids() == [] and not list(inst.data.glob("fake-*.json")), inst.diag()
    assert "잠금 안 재확인" in inst.llog()


@pytest.mark.parametrize("runtime", ["none", "dead-pid"])
def test_foreign_server_on_the_port_is_reported(inst, runtime):
    """[L3] health 가 {"status":"ok"} 이면 그 포트에 누가 있든 '떠 있음'으로 봐서, 남의 서버가 포트를 차지하면 start 는 조용히
    아무것도 안 하고 watch 도 영영 되살리지 않았다 — 이 설치본의 Host 가 아니면 실패(rc≠0)와 안내."""
    if runtime == "dead-pid":
        (inst.data / "runtime.json").write_text(json.dumps({"port": inst.port, "pid": DEAD_PID}), encoding="utf-8")
    srv = _foreign_server(inst.port)
    try:
        r = inst.run("start")
        assert r.returncode == 1 and "다른 프로그램" in r.stderr.decode(), inst.diag(r)
        assert not list(inst.data.glob("fake-*.json")) and f"fail: 포트 {inst.port}" in inst.llog()
        st = inst.run("status")
        got = _kv(st.stdout.decode())
        assert st.returncode == 1 and got["state"] == "stopped" and got["pid"] == "", got
        assert inst.run("stop").stdout.decode().strip() == "not running"
    finally:
        srv.shutdown()
        srv.server_close()
    inst.started(inst.run("start"))                                       # 포트가 비면 띄운다


def test_rotated_host_log_keeps_the_incident_tail(inst, monkeypatch):
    """[L4] 5MB 회전은 기동 직전(구분줄 바로 앞)에 한다 — 새 Host 가 읽는 사고 꼬리(host.log 의 마지막 구분줄 앞)가 비었다.
    그때는 host.log.1 의 꼬리가 죽은 Host 의 마지막 말이다."""
    from session_manager import config, liveness
    p1 = inst.started(inst.run("start"))
    with (inst.data / "host.log").open("ab") as f:
        f.write(b"x" * 5_300_000 + b"\nTraceback ... MemoryError (H1 last words)\n")
    os.kill(p1, signal.SIGKILL)
    assert _wait(lambda: _gone(p1), 10)
    inst.started(inst.run("start"))
    assert (inst.data / "host.log.1").is_file() and inst.text("host.log").startswith("=== launcher start")
    monkeypatch.setattr(config, "data_dir", lambda: inst.data)
    assert liveness.log_tail(10)[-1] == "Traceback ... MemoryError (H1 last words)"


@pytest.mark.parametrize("cmd", ["start", "watch"])
def test_corpse_lock_holding_my_own_pid_is_taken_over(inst, cmd):
    """[L5] 시체 잠금의 pid 를 (재부팅 뒤) 나 자신이 받으면 '살아 있고 명령줄에 런처 이름' 이라 산 잠금으로 보고 영영
    건너뛰었다 — 내 pid 면 시체(나는 아직 안 잡았다)."""
    lock = "launcher.lock" if cmd == "start" else "watch.lock.d"
    args = "start" if cmd == "start" else "watch 10"
    script = f'mkdir "$D/{lock}" && printf "%s\\n" "$$" >"$D/{lock}/pid" && exec /bin/sh "$L" {args}'
    p = inst.popen(["/bin/sh", "-c", script], env=inst.env(D=inst.data, L=inst.launcher), cwd=str(inst.home))
    if cmd == "start":
        assert p.wait(timeout=60) == 0, inst.diag()
        assert len(inst.host_pids()) == 1 and inst.health(), inst.diag()
        assert not (inst.data / lock).exists()
    else:
        assert _wait(inst.health, 20), inst.diag()
        assert _kv(inst.run("status").stdout.decode())["watch_pid"] == str(p.pid)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0


def test_lock_from_another_boot_is_a_corpse(inst):
    """[L5] 전원 차단 뒤 재부팅하면 잠금이 시체로 남고, 그 pid 를 명령줄에 런처 이름이 든 프로세스(로그인 때 뜨는 watch)가 받을 수
    있다 — 잠금에 부팅 식별자를 함께 적어 다른 부팅의 잠금은 시체로 본다. 같은 부팅이면(또는 식별자가 없으면) 지금처럼."""
    boot = _boot_id()
    if not boot:
        pytest.skip("부팅 식별자를 얻을 수 없는 환경")
    lock = inst.data / "launcher.lock"
    lock.mkdir()
    holder = inst.holder(_posix.START_SCRIPT)
    (lock / "pid").write_text(f"{holder.pid}\nboot={boot}\n", encoding="utf-8")
    r = inst.run("start")
    assert r.returncode == 0 and inst.host_pids() == [] and "skip: 다른 런처가 기동 중" in inst.llog(), inst.diag(r)
    (lock / "pid").write_text(f"{holder.pid}\nboot=0000-another-boot\n", encoding="utf-8")
    inst.started(inst.run("start"))
    assert not lock.exists()


def test_watch_lock_records_its_root_and_boot(inst):
    """[C4] watch 는 잠금 안에 자기 설치 루트(물리 경로 한 줄, tmp 뒤 mv)를 남긴다 — 제거(uninstall)가 자기 설치본의 watch 만
    멈추게. [L5] 잠금 pid 파일의 둘째 줄은 부팅 식별자."""
    w = inst.popen(["/bin/sh", str(inst.launcher), "watch", "10"], env=inst.env(), cwd=str(inst.home))
    lock = inst.data / "watch.lock.d"
    assert _wait(lambda: (lock / "root").is_file(), 10), inst.diag()
    assert (lock / "root").read_text(encoding="utf-8") == f"{inst.root}\n"
    assert sorted(x.name for x in lock.iterdir()) == ["pid", "root"]
    lines = (lock / "pid").read_text(encoding="utf-8").splitlines()
    boot = _boot_id()
    assert lines == [str(w.pid)] + ([f"boot={boot}"] if boot else []), lines
    w.send_signal(signal.SIGTERM)
    assert w.wait(timeout=10) == 0 and not lock.exists()


def test_root_is_the_physical_path_from_bin_pwd(inst, tmp_path):
    """[C1] ROOT 는 /bin/pwd -P 로 — 맥 APFS(대소문자 무시)에서 셸 내장 pwd -P 는 cd 에 준 글자를 그대로 두고, /bin/pwd 는
    디스크의 글자를 준다. 다섯 스크립트가 같은 phys_pwd 를 쓴다. 심볼릭 링크로 불러도 물리 경로."""
    t = TEMPLATE.read_text(encoding="utf-8")
    assert "\nphys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }\n" in t
    assert "\nROOT=$(CDPATH='' cd \"$(dirname \"$0\")\" && phys_pwd) || exit 1\n" in t
    link = tmp_path / "link-root"
    link.symlink_to(inst.root, target_is_directory=True)
    r = subprocess.run(["/bin/sh", str(link / _posix.START_SCRIPT), "paths"], env=inst.env(), capture_output=True,
                       timeout=30, cwd=str(inst.home))
    assert r.returncode == 0 and r.stdout.decode("utf-8").splitlines()[0] == f"root={inst.root}", r.stderr.decode()


@pytest.mark.parametrize("how", ["CLAUDE_CONFIG_DIR", "SESSION_MANAGER_CLAUDE_HOME", "toml"])
def test_data_dir_mismatch_is_refused(inst, how):
    """[L6] Host 의 데이터 폴더는 SESSION_MANAGER_CLAUDE_HOME > CLAUDE_CONFIG_DIR(env, 없으면 설정 파일 [claude] config_dir) >
    ~/.claude 로 정해진다(config.py·appconfig.apply_env). 설치 때와 다른 값이 든 셸에서 띄우면 runtime.json 이 딴 폴더에 생겨
    stop 은 'not running' 인데 Host 는 계속 돌았다 — 띄우지 않고 알린다."""
    other = inst.home / "other-profile"
    env = inst.env()
    if how == "toml":
        inst.conf.write_text(f"[server]\nport = {inst.port}\n\n[claude]\nconfig_dir = {json.dumps(str(other))}\n",
                             encoding="utf-8")
    else:
        env[how] = str(other)
    r = inst.run("start", env=env)
    assert r.returncode == 1 and "데이터 폴더" in r.stderr.decode(), inst.diag(r)
    assert inst.host_pids() == [] and not list(inst.data.glob("fake-*.json"))
    assert f"{other}/session_manager" in inst.llog()
    r = inst.run("foreground", env=env, timeout=30)          # 감독자용은 0(KeepAlive 가 30초마다 다시 부르지 않게) — 알리기는 같다
    assert r.returncode == 0 and "데이터 폴더" in r.stderr.decode() and inst.host_pids() == [], inst.diag(r)


def test_data_dir_named_differently_but_same_is_fine(inst):
    """같은 폴더를 다른 이름(심볼릭 링크·기본값을 그대로 적은 값)으로 가리키는 env 는 막지 않는다."""
    link = inst.home / "claude-link"
    link.symlink_to(inst.home / ".claude", target_is_directory=True)
    inst.started(inst.run("start", env=inst.env(CLAUDE_CONFIG_DIR=link)))
    assert inst.run("stop").returncode == 0
    inst.started(inst.run("start", env=inst.env(SESSION_MANAGER_CLAUDE_HOME=inst.home / ".claude")))


# ---------------------------------------------------------------- [mac] start 는 launchd 에 맡긴다(kickstart)

# 가짜 launchctl(런처 시험) — 받은 인자를 calls 에 탭으로 적는다. 올림 상태 = FAKE_LCTL_DIR/loaded(등록 파일 사본).
# kickstart 는 올라 있는 작업을 launchd 처럼 한 번 띄운다(새 세션 — 작업 프로세스가 곧 Host, 그 pid 를 jobs 에 적는다).
# FAKE_KICKSTART: fail(실패) | noop(받고도 띄우지 않는다 — 막 내린 작업의 정리·throttle 흉내)
FAKE_LCTL = r'''#!@PYTHON@
import os, plistlib, subprocess, sys
d = os.environ["FAKE_LCTL_DIR"]
with open(os.path.join(d, "calls"), "a", encoding="utf-8") as f:
    f.write("\t".join(sys.argv[1:]) + "\n")
loaded = os.path.join(d, "loaded")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "print":
    sys.exit(0 if os.path.exists(loaded) else 113)
if cmd == "kickstart":
    if not os.path.exists(loaded):
        print("Could not find service (fake)", file=sys.stderr)
        sys.exit(113)
    mode = os.environ.get("FAKE_KICKSTART", "")
    if mode == "fail":
        print("Kickstart failed: 5: Input/output error (fake)", file=sys.stderr)
        sys.exit(5)
    if mode == "noop":
        sys.exit(0)
    with open(loaded, "rb") as f:
        p = plistlib.load(f)
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "TMPDIR") if k in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env.update(p.get("EnvironmentVariables", {}))
    with open(p["StandardOutPath"], "ab") as out:
        job = subprocess.Popen(p["ProgramArguments"], cwd="/", env=env, stdin=subprocess.DEVNULL, stdout=out,
                               stderr=out, start_new_session=True)
    with open(os.path.join(d, "jobs"), "a", encoding="utf-8") as f:
        f.write("%d\n" % job.pid)
    sys.exit(0)
print("fake launchctl: unknown " + cmd, file=sys.stderr)
sys.exit(64)
'''


def _mac(inst, *, plist_root: Path | None = None, loaded: bool = True, darwin: bool = True) -> dict:
    """맥 흉내 env — 가짜 uname(Darwin)·launchctl 을 PATH 앞에 두고, 홈에 자동 시작 등록 파일(plist_root 의 런처를 부른다 —
    기본은 이 설치본)을 쓴다. loaded 면 launchd 에 올라 있다."""
    base = inst.home.parent
    tools = base / "mac tools"
    tools.mkdir(exist_ok=True)
    fakes = {"launchctl": FAKE_LCTL.replace("@PYTHON@", sys.executable)}
    if darwin:
        fakes["uname"] = '#!/bin/sh\ncase "${1:-}" in -m) echo arm64 ;; *) echo Darwin ;; esac\n'
    for name, body in fakes.items():
        (tools / name).write_text(body, encoding="utf-8")
        os.chmod(tools / name, 0o755)
    lctl = base / "lctl"
    lctl.mkdir(exist_ok=True)
    raw = plistlib.dumps(hostctl.launchd_plist(str(plist_root or inst.root), str(inst.data), path_env=os.environ["PATH"],
                                               lang="C.UTF-8", umask=0o022))
    hostctl.write_launchd_program(str(inst.data))          # 등록 파일이 부르는 데이터 폴더의 ClewPath(render-launchd 와 같다)
    plist = inst.home / hostctl.LAUNCHD_PLIST
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(raw)
    if loaded:
        (lctl / "loaded").write_bytes(raw)
    return inst.env(PATH=f"{tools}:{os.environ['PATH']}", FAKE_LCTL_DIR=lctl)


def _calls(inst) -> list[list[str]]:
    try:
        text = (inst.home.parent / "lctl" / "calls").read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.split("\t") for ln in text.splitlines()]


def _jobs(inst) -> list[int]:
    try:
        return [int(x) for x in (inst.home.parent / "lctl" / "jobs").read_text(encoding="utf-8").split()]
    except OSError:
        return []


LABEL = "com.clewpath.host"


def test_mac_start_and_resume_hand_the_host_to_launchd(inst):
    """[mac] 이 설치본의 launchd 작업이 올라 있으면 start·resume 은 직접 띄우지 않고 launchd 에 맡긴다(kickstart) — Host 가 그
    작업의 프로세스라 죽으면 30초 KeepAlive 가 받고 로그아웃 때 같이 끝난다(맥 실기 3b 관찰 3: 설치·resume·start 가 띄운 Host 는
    PPID 1·launchd 밖이라 죽으면 5분 확인이 받았고 로그아웃에도 살아남았다). 떠 있거나 보류(stop --hold) 중이면 맡기지도 않는다."""
    env = _mac(inst)
    target = f"gui/{os.getuid()}/{LABEL}"
    r = inst.run("start", env=env)
    assert r.returncode == 0 and _wait(inst.health, 20), inst.diag(r)
    assert _calls(inst) == [["print", target], ["kickstart", target]], _calls(inst)
    jobs = _jobs(inst)
    assert len(jobs) == 1 and inst.host_pids() == jobs and inst.runtime().get("pid") == jobs[0], (jobs, inst.diag())
    assert f"start: runtime.json 없음 → launchd 에 맡김(kickstart) pid {jobs[0]}" in inst.llog(), inst.diag()
    assert f"foreground: runtime.json 없음 → 기동(exec, pid {jobs[0]})" in inst.llog() and "spawned pid" not in inst.llog()
    assert inst.run("start", env=env).returncode == 0 and len(_calls(inst)) == 2, "떠 있으면 아무것도 안 한다"
    assert inst.run("stop", env=env).stdout.decode().strip() == f"stopped (pid {jobs[0]})"
    assert inst.run("stop", "--hold", env=env).returncode == 0
    r = inst.run("start", env=env)
    assert r.returncode == 0 and len(_calls(inst)) == 2 and inst.host_pids() == [], "보류 중이면 맡기지도 않는다"
    r = inst.run("resume", env=env)
    assert r.returncode == 0 and _wait(inst.health, 20), inst.diag(r)
    jobs = _jobs(inst)
    assert len(jobs) == 2 and inst.host_pids() == [jobs[1]], (jobs, inst.host_pids(), inst.diag())
    assert [c[0] for c in _calls(inst)] == ["print", "kickstart", "print", "kickstart"], _calls(inst)


@pytest.mark.parametrize("case", ["not-loaded", "kickstart-fails", "kickstart-does-nothing"])
def test_mac_start_spawns_itself_when_launchd_does_not(inst, case):
    """맡길 수 없으면 지금처럼 직접 띄운다 — 등록 파일은 있지만 launchd 에 올라 있지 않다(SSH 셸로 설치해 다음 로그인 때 올라갈
    등록)·kickstart 가 실패·받고도 약 20초 안에 뜨지 않음(반초 낮잠 횟수). 사유는 launcher.log 에."""
    env = _mac(inst, loaded=case != "not-loaded")
    if case == "kickstart-fails":
        env["FAKE_KICKSTART"] = "fail"
    elif case == "kickstart-does-nothing":
        env["FAKE_KICKSTART"] = "noop"
    inst.started(inst.run("start", env=env, timeout=120))
    assert _jobs(inst) == [] and "spawned pid" in inst.llog(), inst.diag()
    kinds = [c[0] for c in _calls(inst)]
    if case == "not-loaded":
        assert kinds == ["print"], kinds
        return
    assert kinds == ["print", "kickstart"], kinds
    if case == "kickstart-fails":
        assert "launchd kickstart 실패(Kickstart failed: 5: Input/output error (fake)) → 직접 기동" in inst.llog()
    else:
        assert "launchd 에 맡겼지만 약 20초 안에 뜨지 않음(kickstart) → 직접 기동" in inst.llog()


@pytest.mark.parametrize("case", ["other-install", "linux", "updater"])
def test_mac_start_leaves_launchd_alone(inst, case):
    """같은 이름의 다른 설치본 작업(설치본은 사용자당 하나 — 등록 파일이 다른 런처를 부른다)은 깨우지 않는다. 리눅스(uname
    그대로)는 launchd 를 보지 않는다. 업데이트 러너의 재기동(CLEWPATH_UPDATER=1)은 직접 띄운다 — launchd 의 foreground 는 러너가
    쥔 update.inprogress 를 보고 건너뛴다. 셋 다 launchctl 을 부르지도 않는다."""
    other = inst.home.parent / "Other" / "app"
    env = _mac(inst, plist_root=other if case == "other-install" else None, darwin=case != "linux")
    if case == "updater":
        env["CLEWPATH_UPDATER"] = "1"
    inst.started(inst.run("start", env=env))
    assert _calls(inst) == [] and _jobs(inst) == [], _calls(inst)
