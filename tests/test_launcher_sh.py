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
- 판정의 세 갈래(psutil / venv 에 psutil 없음 / /proc 없음 = 맥의 ps 길)는 variant 픽스처로 같은 시나리오를 돌린다.
Windows 에선 건너뛴다(리눅스 검증 컨테이너 `docker run --init`·맥에서 돈다). 정리는 설치 루트가 명령줄에 든 프로세스를
모두 죽이는 것으로 한다.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 런처(start-connector.sh) — POSIX 전용")

if sys.platform != "win32":
    import psutil

import session_manager
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
(DATA / f"fake-{os.getpid()}.json").write_text(json.dumps({
    "env": dict(os.environ), "umask": UMASK, "cwd": os.getcwd(), "sid": os.getsid(0)}), encoding="utf-8")
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
        e = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLEWPATH_UPDATER")
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


@pytest.fixture(params=["psutil", "no-psutil", "no-procfs"])
def variant(request, tmp_path):
    """판정의 세 갈래: 보통(psutil) / venv 에 psutil 이 없을 때(파이썬이 /proc 를 읽음) / /proc 가 없을 때(맥 —
    셸이 ps 로 명령줄·상태를 읽는다. 리눅스에서는 런처의 /proc 경로를 없는 곳으로 바꿔 흉내 낸다)."""
    if request.param == "no-procfs" and not (Path("/proc/self").is_dir() and shutil.which("ps")):
        pytest.skip("/proc 와 ps 가 둘 다 있는 리눅스에서만 흉내 낸다(맥은 기본 시험이 이미 이 길)")
    i = Inst(tmp_path, with_psutil=request.param != "no-psutil")
    if request.param == "no-procfs":
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


# ---------------------------------------------------------------- foreground(다음 단계 launchd·systemd)

def test_foreground_becomes_the_host(inst):
    p = inst.popen(["/bin/sh", str(inst.launcher), "foreground"], env=inst.env(), cwd=str(inst.home))
    assert _wait(lambda: inst.runtime().get("pid") == p.pid and inst.health(), 20), inst.diag()
    assert psutil.Process(p.pid).cmdline()[:3] == [str(inst.py), "-m", "session_manager.server"], "exec — pid 그대로"
    assert inst.fake(p.pid)["env"]["CLEWPATH_CONFIG"] == str(inst.conf)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
    (inst.data / "maintenance.flag").write_text("by=test\n", encoding="utf-8")
    r = inst.run("foreground", timeout=30)
    assert r.returncode == 0 and inst.host_pids() == [], "보류 중이면 띄우지 않고 0(감독자가 다시 부르지 않게)"
