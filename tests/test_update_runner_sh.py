"""맥·리눅스 업데이트 러너 실행 시험 — docs/designs/posix-install.md 6.2 R①~R⑮ (+ S24·S26·S27·S29~S34).

진짜 러너(session_manager/update_runner.sh)를 updater.apply 처럼 데이터 폴더 updates/ 에 복사해 /bin/sh 로 돌린다.
- 가짜 설치본(옛 트리 1.0.0): venv(--without-pip, .pth 로 지금 파이썬의 site-packages) + 진짜 런처 템플릿을
  ensure_launcher 로 렌더한 start-connector.sh + 설치 기록(.clewpath-install, uv= 가짜 uv). 경로에 공백·한글·
  작은따옴표(R⑧). 재기동은 진짜 런처가 한다(러너 + 런처 통합).
- 스테이지(새 트리 1.1.0). 트리마다 session_manager/fake.json 이 가짜 Host 의 동작을 정한다(tag·report_version·
  exit_now·ignore_term) — 교체되면 동작도 함께 바뀐다.
- 가짜 Host(session_manager/server.py): toml 의 포트로 /api/health·/api/owner/diagnostics 를 서빙한다. version 은
  .venv/clewpath-version(dist-info 흉내 — 가짜 uv 가 sync 때 그 트리의 pyproject 버전으로 맞춘다). runtime.json 을 쓰고,
  받은 env·SigIgn 을 fake-<pid>.json 에 남긴다.
- 가짜 uv: 부른 모양(인자·cwd·env)을 calls.jsonl 에 적는다. uv.json 으로 실패·지연을 넣는다.
- 가짜 브로커(session_manager/ptyd.py): 잔다 — 롤백이 건드리지 않는지 본다.
- 수동 적용 흉내: 러너가 표식을 쓴 뒤 시험이 옛 Host 에 SIGTERM(server._shutdown_soon — 1.5초 뒤 스스로 내려감).
  재기동 자동 적용(Host 가 스스로 안 내려감) 모양은 SIGTERM 을 보내지 않는다.
Windows 에선 건너뛴다(리눅스 검증 컨테이너 `docker run --init`·맥에서 돈다).
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 러너(update_runner.sh) — POSIX 전용")

if sys.platform != "win32":
    import psutil

import session_manager
from session_manager import updater
from session_manager.osdep import _posix

REAL_PKG = Path(session_manager.__file__).resolve().parent
RUNNER = REAL_PKG / _posix.UPDATE_RUNNER
TEMPLATE = REAL_PKG / _posix.LAUNCHER_TEMPLATE
OLD, NEW = "1.0.0", "1.1.0"
DEAD_PID = 4_194_999          # 리눅스 pid_max 상한(2^22)·맥(99998)보다 크다 — 있을 수 없는 pid
SHELLS = [("/bin/sh",)] + ([("bash", "--posix")] if shutil.which("bash") else [])
with_shells = pytest.mark.parametrize("shell", SHELLS, ids=[" ".join(s) for s in SHELLS])
procfs_variants = pytest.mark.parametrize("procfs", [True, False], ids=["procfs", "no-procfs"])
HUP_INT = (1 << 0) | (1 << 1)  # /proc/<pid>/status SigIgn 의 비트 — SIGHUP=1·SIGINT=2(POSIX 공통, Windows 엔 SIGHUP 이 없다)

FAKE_SERVER = r'''"""가짜 Host — tests/test_update_runner_sh.py. 동작은 이 트리의 session_manager/fake.json 이 정한다."""
import json, os, signal, sys, time, tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONF = Path(os.environ["CLEWPATH_CONFIG"])
DATA = CONF.parent
FAKE = json.loads((HERE / "fake.json").read_text(encoding="utf-8"))
PORT = int(tomllib.loads(CONF.read_text(encoding="utf-8"))["server"]["port"])
try:
    VERSION = (ROOT / ".venv" / "clewpath-version").read_text(encoding="utf-8").strip()
except OSError:
    VERSION = None
REPORT = FAKE.get("report_version", VERSION)
SIGIGN = "0"
try:
    with open("/proc/self/status", encoding="utf-8") as f:
        SIGIGN = next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("SigIgn:")), "0")
except OSError:
    pass
(DATA / f"fake-{os.getpid()}.json").write_text(json.dumps({
    "tag": FAKE.get("tag"), "version": VERSION, "report": REPORT, "env": dict(os.environ), "sigign": SIGIGN,
    "root": str(ROOT)}), encoding="utf-8")
print(f"fake host {os.getpid()} {FAKE.get('tag')} up", flush=True)
if FAKE.get("exit_now"):
    sys.exit(3)


def _term(signum, frame):
    (DATA / f"term-{os.getpid()}").write_text("TERM", encoding="utf-8")
    os._exit(0)


signal.signal(signal.SIGTERM, signal.SIG_IGN if FAKE.get("ignore_term") else _term)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/health":
            body, code = {"status": "ok"}, 200
        elif self.path == "/api/owner/diagnostics":
            body, code = {"claude_home": str(DATA.parent), "version": REPORT}, 200
        else:
            body, code = {"error": "nope"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


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

FAKE_UV = r'''#!@PYTHON@
"""가짜 uv — tests/test_update_runner_sh.py. 부른 모양을 적고, sync·pip install 이면 venv 의 버전 표식(dist-info 흉내)을
그 트리의 pyproject 버전으로 맞춘다. 제어: $FAKE_UV_CTL/uv.json {"fail_sync": [버전], "fail_pip": [버전], "sleep_sync": 초}."""
import json, os, sys, time, tomllib
from pathlib import Path

ctl = Path(os.environ["FAKE_UV_CTL"])
conf = json.loads((ctl / "uv.json").read_text(encoding="utf-8")) if (ctl / "uv.json").is_file() else {}
args = sys.argv[1:]
keep = ("UV_PROJECT_ENVIRONMENT", "UV_SYSTEM_CERTS", "UV_PYTHON_PREFERENCE", "UV_PYTHON_INSTALL_DIR", "CLEWPATH_UPDATER")
call = {"exe": sys.argv[0], "args": args, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keep}}
if args[:1] == ["sync"]:
    kind, tree, venv = "sync", Path.cwd(), Path(os.environ["UV_PROJECT_ENVIRONMENT"])
elif args[:2] == ["pip", "install"]:
    kind, tree = "pip", Path(args[args.index("-e") + 1])
    venv = Path(args[args.index("--python") + 1]).parent.parent
else:
    sys.exit(2)
ver = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
call["version"] = ver
with open(ctl / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(call) + "\n")
if kind == "sync":
    (ctl / "syncing").write_text(ver, encoding="utf-8")
time.sleep(float(conf.get("sleep_" + kind, 0)))
if ver in conf.get("fail_" + kind, []):
    print(f"error: 가짜 {kind} 실패({ver})", file=sys.stderr)
    sys.exit(1)
(venv / "clewpath-version").write_text(ver, encoding="utf-8")
print(f"Resolved 9 packages - {kind} {ver}")
'''

# 파이썬이 없을 때의 헬스(러너의 curl 길) — curl --noproxy '*' -s -m 3 URL 만 흉내 낸다
FAKE_CURL = r'''#!@PYTHON@
import os, sys, urllib.request
with open(os.path.join(os.environ["FAKE_UV_CTL"], "curl-calls"), "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(sys.argv[-1], timeout=3) as r:
        sys.stdout.write(r.read().decode())
except Exception:
    sys.exit(7)
'''

# 러너가 부르는 rm 을 적는다(R⑫ — 인자가 나쁘면 아무것도 지우지 않는다)
FAKE_RM = """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_RM_LOG"
exec /bin/rm "$@"
"""

# 러너의 rename 을 세다가 CRASH_AT 번째(교체 저널이 걸린 것만)가 끝나면 러너를 SIGKILL — 바로 그 자리에서 죽음(S29)
FAKE_MV = """#!/bin/sh
/bin/mv "$@" || exit $?
case "$*" in *.clewpath-swap.*) ;; *) exit 0 ;; esac
n=$(( $(cat "$CRASH_COUNT" 2>/dev/null || echo 0) + 1 ))
printf '%s\\n' "$n" >"$CRASH_COUNT"
[ "$n" = "$CRASH_AT" ] && kill -9 "$PPID"
exit 0
"""
SWAP_RENAMES = 13             # 스테이지 7 항목 — 옛 것이 있는 6 개는 old·new 두 번, install.sh 는 new 한 번

PYPROJECT = """# 가짜 — 러너는 [project] 의 version 을 읽는다
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "clewpath-host"
version = "{version}"
requires-python = ">=3.11"
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


def make_tree(dest: Path, version: str, tag: str, extra: dict | None = None) -> Path:
    sm = dest / "session_manager"
    sm.mkdir(parents=True)
    (sm / "__init__.py").write_text("", encoding="utf-8")
    (sm / "server.py").write_text(FAKE_SERVER, encoding="utf-8")
    (sm / "ptyd.py").write_text(FAKE_BROKER, encoding="utf-8")
    (sm / "fake.json").write_text(json.dumps({"tag": tag}), encoding="utf-8")
    shutil.copyfile(TEMPLATE, sm / _posix.LAUNCHER_TEMPLATE)
    (dest / "pwa").mkdir()
    (dest / "pwa" / "index.html").write_text(f"{tag} pwa", encoding="utf-8")
    (dest / "pyproject.toml").write_text(PYPROJECT.format(version=version), encoding="utf-8")
    for name, text in {"uv.lock": f"{tag} lock", "README.md": f"{tag} readme", "LICENSE": "MIT", **(extra or {})}.items():
        (dest / name).write_text(text, encoding="utf-8")
    return dest


class Upd:
    """가짜 설치본 하나와 그 업데이트 — 설치 루트·venv·데이터 폴더·스테이지·러너 사본·가짜 uv."""

    def __init__(self, tmp: Path):
        self.base = base = tmp.resolve()          # 런처의 ROOT 는 pwd -P(물리 경로) — 맥 /var → /private/var
        self.root = base / "My Apps" / "클루패스 it's app"
        self.home = base / "home x"
        self.data = self.home / ".claude" / "session_manager"
        self.work = self.data / "updates"
        self.conf = self.data / "clewpath.toml"
        self.stage = self.work / f"stage-{NEW}"
        self.backup = self.work / f"backup-{OLD}"
        self.log = self.work / f"apply-{NEW}.log"
        self.runner = self.work / _posix.UPDATE_RUNNER
        self.launcher = self.root / _posix.START_SCRIPT
        self.py = self.root / ".venv" / "bin" / "python"
        self.chome = base / "clew home"           # CLEWPATH_HOME — 공통 경로 블록이 uv 를 찾는 두 번째 자리
        self.tools = base / "도구 bin"
        self.uv = self.tools / "uv"
        self.ctl = base / "uv ctl"
        self.port = _free_port()
        self.procs: list[subprocess.Popen] = []
        make_tree(self.root, OLD, "old", {"install.ps1": "old ps1"})
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")],
                       check=True, capture_output=True, timeout=120)
        site = next((self.root / ".venv" / "lib").glob("python3*/site-packages"))
        deps = sorted({sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]})
        (site / "_clewpath_test_deps.pth").write_text("\n".join(deps) + "\n", encoding="utf-8")
        (self.root / ".venv" / "clewpath-version").write_text(OLD, encoding="utf-8")
        self.work.mkdir(parents=True)
        self.conf.write_text(f"[server]\nport = {self.port}\n", encoding="utf-8")
        assert _posix.ensure_launcher(self.root, self.data, self.conf, self.port) == "launcher: regenerated (posix v2)"
        shutil.copyfile(RUNNER, self.runner)       # updater.apply 처럼 설치 폴더 밖 사본(0644 — /bin/sh 로 부른다)
        self.tools.mkdir()
        self.uv.write_text(FAKE_UV.replace("@PYTHON@", sys.executable), encoding="utf-8")
        os.chmod(self.uv, 0o755)
        self.ctl.mkdir()
        self.record()
        make_tree(self.stage, NEW, "new", {"install.sh": "#!/bin/sh\n# 새 버전에만 있는 항목\n"})

    # ---- 준비
    def record(self, **kv) -> None:
        """설치 기록 — install.sh 가 쓸 모양(명세 2.3). 값이 None 이면 그 줄을 뺀다."""
        vals = {"home": self.chome, "uv": self.uv, "python": sys.executable, "python_pref": "only-system",
                "python_install_dir": self.chome / "python", "data_dir": self.data, "conf_file": self.conf,
                "installed_version": OLD, "installed_at": 1760900000}
        vals.update(kv)
        (self.root / ".clewpath-install").write_text(
            "# clewpath-install v1 — install.sh 가 쓴다. 손대지 마세요\n"
            + "".join(f"{k}={v}\n" for k, v in vals.items() if v is not None), encoding="utf-8")

    def set_fake(self, tree: Path, **kv) -> None:
        p = tree / "session_manager" / "fake.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d.update(kv)
        p.write_text(json.dumps(d), encoding="utf-8")

    def uv_conf(self, **conf) -> None:
        (self.ctl / "uv.json").write_text(json.dumps(conf), encoding="utf-8")

    def no_procfs(self) -> None:
        """맥 흉내 — 러너 사본의 /proc 경로를 없는 곳으로 바꿔 ps 길(ps -A -ww·ps -o stat=·command=)로 가게 한다."""
        t = self.runner.read_text(encoding="utf-8")
        assert t.count("/proc/") >= 4 and "/proc/self" in t
        self.runner.write_bytes(t.replace("/proc/", "/nonexistent-proc/").encode("utf-8"))

    def env(self, **extra) -> dict:
        e = {k: v for k, v in os.environ.items()
             if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLEWPATH_UPDATER", "XDG_DATA_HOME")
             and not k.upper().startswith(("CLAUDE_CODE_", "UV_"))}
        e.update(HOME=str(self.home), FAKE_UV_CTL=str(self.ctl), CLEWPATH_HOME=str(self.chome))
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
        """명령줄에 name 이 든 살아 있는 프로세스(잠금·표식의 주인 흉내)."""
        return self.popen(["sh", "-c", "sleep 120; exit 0", name])

    def start_broker(self) -> int:
        self.popen([str(self.py), "-m", "session_manager.ptyd"], cwd=str(self.root), start_new_session=True,
                   env=self.env(FAKE_DATA_DIR=self.data))
        assert _wait(lambda: (self.data / "ptyd.json").is_file(), 10)
        return json.loads((self.data / "ptyd.json").read_text(encoding="utf-8"))["pid"]

    def start_old(self) -> int:
        r = subprocess.run(["/bin/sh", str(self.launcher), "start"], env=self.env(), capture_output=True, timeout=60,
                           cwd=str(self.home))
        assert r.returncode == 0, self.diag(r.stderr.decode("utf-8", "replace"))
        pid = self.runtime()["pid"]
        assert self.hosts() == [pid] and self.fake(pid)["tag"] == "old"
        return pid

    # ---- 러너
    def argv(self, *, wait_pid: int, version: str = NEW, port: int | None = None, restart: str | None = None,
             wait_seconds: int = 5, health_seconds: int | None = None, extra=()) -> list[str]:
        a = _posix.update_runner_argv(self.runner, install_root=self.root, stage_dir=self.stage,
                                      backup_dir=self.backup, wait_pid=wait_pid, port=port or self.port,
                                      version=version, log_file=self.log,
                                      restart_script=str(self.launcher) if restart is None else restart)
        a += ["-WaitSeconds", str(wait_seconds)]
        if health_seconds is not None:
            a += ["-HealthSeconds", str(health_seconds)]
        return a + list(extra)

    def spawn(self, argv, *, shell=("/bin/sh",), env=None, stop_host: int | None = None, cwd=None) -> subprocess.Popen:
        """러너를 띄운다 — updater.apply 처럼 apply.lock 을 먼저 쓰고(하트비트가 보이게 옛 시각으로) 새 세션으로.
        stop_host 면 러너가 표식을 쓴(또는 실패를 적은) 뒤 그 Host 에 SIGTERM — 수동 적용에서 Host 가 스스로 내려감."""
        lock = self.work / "apply.lock"
        lock.write_text(str(time.time()), encoding="utf-8")
        past = time.time() - 200
        os.utime(lock, (past, past))
        out = open(self.base / f"runner-{len(self.procs)}.out", "wb")
        p = self.popen([*shell, *argv[1:]], env=env or self.env(), cwd=str(cwd or self.root), stdout=out,
                       stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        if stop_host:
            _wait(lambda: (self.data / "update.inprogress").exists() or "[FAIL]" in self.logtext()
                  or p.poll() is not None, 10)
            try:
                os.kill(stop_host, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return p

    def run(self, argv, *, timeout: float = 150, **kw) -> int:
        p = self.spawn(argv, **kw)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            pytest.fail("러너가 끝나지 않았다\n" + self.diag())

    def recover(self, *, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", str(self.runner), "-RecoverOnly", "-InstallRoot", str(self.root),
                               "-LogFile", str(self.work / "recover.log")], env=env or self.env(),
                              capture_output=True, timeout=60, cwd=str(self.home))

    # ---- 관찰
    def runtime(self) -> dict:
        try:
            return json.loads((self.data / "runtime.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def fake(self, pid: int) -> dict:
        return json.loads((self.data / f"fake-{pid}.json").read_text(encoding="utf-8"))

    def fakes(self) -> list[tuple[int, str]]:
        return sorted((int(p.stem.split("-", 1)[1]), json.loads(p.read_text(encoding="utf-8"))["tag"])
                      for p in self.data.glob("fake-*.json"))

    def hosts(self) -> list[int]:
        out = []
        for pr in psutil.process_iter(["pid", "cmdline", "status"]):
            c = pr.info["cmdline"] or []
            if c[:3] == [str(self.py), "-m", "session_manager.server"] and pr.info["status"] != psutil.STATUS_ZOMBIE:
                out.append(pr.info["pid"])
        return sorted(out)

    def running_tag(self) -> str:
        """지금 떠 있는 Host(runtime.json 의 pid, 살아 있고 응답함)의 트리."""
        pid = self.runtime().get("pid")
        assert pid and not _gone(pid) and self.health(), self.diag()
        return self.fake(pid)["tag"]

    def health(self) -> bool:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as r:
                return json.loads(r.read()) == {"status": "ok"}
        except Exception:  # noqa: BLE001
            return False

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def text(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def logtext(self) -> str:
        return self.text(self.log)

    def last(self) -> dict:
        raw = (self.work / "last_apply.json").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 금지(updater.recently_failed 가 json.loads 로 읽는다)"
        return json.loads(raw.decode("utf-8"))

    def uv_calls(self) -> list[dict]:
        p = self.ctl / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []

    def swaps(self) -> list[str]:
        return sorted(p.name for p in self.root.glob(".clewpath-swap.*"))

    def tree(self, root: Path | None = None) -> dict:
        root = root or self.root
        out = {}
        for p in sorted(root.rglob("*")):
            parts = p.relative_to(root).parts
            if parts[0] == ".venv" or "__pycache__" in parts:
                continue
            out["/".join(parts)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
        return out

    def clean_after(self) -> None:
        """끝난 뒤 남으면 안 되는 것 — 표식·러너 잠금·apply.lock·교체 저널·헬퍼·임시 파일."""
        left = [p.name for p in (self.data / "update.inprogress", self.work / "runner.lock.d",
                                 self.work / "apply.lock") if p.exists()]
        left += self.swaps() + [p.name for p in self.work.glob(".*")] + [p.name for p in self.data.glob(".update*")]
        left += [p.name for p in self.work.glob("backup-*.tmp.*")]
        assert not left, f"남은 것: {left}\n" + self.diag()

    def diag(self, extra: str = "") -> str:
        outs = "".join(f"--- {p.name}\n{self.text(p)[-1500:]}\n" for p in sorted(self.base.glob("runner-*.out")))
        return (extra + "\n--- apply log\n" + self.logtext()[-3000:] + "\n--- recover.log\n"
                + self.text(self.work / "recover.log")[-1500:] + "\n--- launcher.log\n"
                + self.text(self.data / "launcher.log")[-1500:] + "\n--- host.err.log\n"
                + self.text(self.data / "host.err.log")[-1000:] + "\n" + outs)

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        me, key = os.getpid(), str(self.base)
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
        for d, dirs, files in os.walk(self.base):                  # 시험이 좁힌 권한을 풀어야 tmp 를 지운다
            for n in dirs:
                try:
                    os.chmod(os.path.join(d, n), 0o755)
                except OSError:
                    pass
            for n in files:
                try:
                    os.chmod(os.path.join(d, n), 0o644)
                except OSError:
                    pass


@pytest.fixture
def upd(tmp_path):
    u = Upd(tmp_path)
    try:
        yield u
    finally:
        u.cleanup()


@pytest.fixture
def as_data_dir(upd, monkeypatch):
    """이 시험의 데이터 폴더를 진짜 updater 가 보게(recently_failed 가 last_apply.json 을 읽는다)."""
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(upd.home / ".claude"))
    return upd


def _forward(upd: Upd, sd: Path, steps: int, dangling: bool = False) -> list[tuple[str, str]]:
    """러너의 4b 를 steps 번째 rename 까지 그대로 흉내(저널 한 줄 → rename). dangling 이면 다음 줄만 쓰고 '죽는다'."""
    ops = []
    for n in updater.PAYLOAD:
        if (sd / "new" / n).exists():
            if (upd.root / n).exists():
                ops.append(("old", n))
            ops.append(("new", n))
    with open(sd / "journal", "a", encoding="utf-8") as j:
        for i, (op, n) in enumerate(ops):
            if i == steps:
                if dangling:
                    j.write(f"{op} {n}\n")
                break
            j.write(f"{op} {n}\n")
            j.flush()
            if op == "old":
                (upd.root / n).rename(sd / "old" / n)
            else:
                (sd / "new" / n).rename(upd.root / n)
    return ops


def _leftover(upd: Upd, steps: int, *, pid: int = 4242, dangling: bool = False, committed: bool = False,
              fmt: str = "1") -> Path:
    """죽은 러너가 남긴 교체 저널 — 스테이지를 SWAP/new 로 복사하고 steps 만큼 옮긴 상태."""
    sd = upd.root / f".clewpath-swap.{pid}"
    (sd / "new").mkdir(parents=True)
    (sd / "old").mkdir()
    (sd / "info").write_text(f"format={fmt}\nrunner_pid={pid}\nversion={NEW}\nfrom={OLD}\nstarted=1760900000\n",
                             encoding="utf-8")
    for n in updater.PAYLOAD:
        src = upd.stage / n
        if src.is_dir():
            shutil.copytree(src, sd / "new" / n)
        elif src.is_file():
            shutil.copy2(src, sd / "new" / n)
    assert _forward(upd, sd, steps, dangling)
    if committed:
        (sd / "committed").write_text("", encoding="utf-8")
    return sd


# ---------------------------------------------------------------- R① 성공 (R⑧ 어색한 경로·R⑨·R⑮)

@with_shells
def test_update_succeeds_and_leaves_nothing_behind(upd, shell, as_data_dir):
    old = upd.start_old()
    dead_proxy = "http://127.0.0.1:9"                              # 루프백 헬스가 프록시로 새면 실패한다
    rc = upd.run(upd.argv(wait_pid=old), shell=shell, stop_host=old,
                 env=upd.env(http_proxy=dead_proxy, HTTP_PROXY=dead_proxy, no_proxy="", NO_PROXY=""))
    assert rc == 0, upd.diag()
    assert upd.read("pwa/index.html") == "new pwa" and upd.read("README.md") == "new readme"
    assert upd.read("install.sh").startswith("#!/bin/sh"), "스테이지에만 있던 항목이 들어왔다"
    assert upd.read("install.ps1") == "old ps1", "스테이지에 없는 기존 항목은 남긴다(Windows 와 같음)"
    pid = upd.runtime()["pid"]
    assert upd.hosts() == [pid] and pid != old and upd.running_tag() == "new"
    got = upd.fake(pid)
    assert got["version"] == NEW and got["report"] == NEW
    assert "CLEWPATH_UPDATER" not in got["env"], "러너 표식은 재기동한 Host 로 넘어가지 않는다(런처가 뺀다)"
    assert int(got["sigign"], 16) & HUP_INT == 0, "HUP·INT 무시가 재기동 Host(→ 브로커 → claude)로 물려가지 않는다"
    assert (upd.data / f"term-{old}").exists(), "옛 Host 는 스스로 내려갔다(러너가 죽이지 않음)"
    upd.clean_after()
    assert (upd.backup / "pwa" / "index.html").read_text(encoding="utf-8") == "old pwa"
    assert (upd.backup / "install.ps1").exists() and not list(upd.work.glob("*-failed-*"))
    last = upd.last()
    assert last["version"] == NEW and last["ok"] is True and isinstance(last["finished_at"], int)
    assert last["log"] == str(upd.log), "공백·한글·작은따옴표 경로도 JSON 으로 그대로"
    assert updater.recently_failed(NEW) is False
    calls = upd.uv_calls()
    assert [c["args"] for c in calls] == [["sync", "--frozen", "--no-dev"]]
    assert calls[0]["cwd"] == str(upd.root) and calls[0]["version"] == NEW and calls[0]["exe"] == str(upd.uv)
    assert calls[0]["env"] == {"UV_PROJECT_ENVIRONMENT": str(upd.root / ".venv"), "UV_SYSTEM_CERTS": "1",
                               "UV_PYTHON_PREFERENCE": "only-system", "UV_PYTHON_INSTALL_DIR": str(upd.chome / "python"),
                               "CLEWPATH_UPDATER": None}, "설치 기록의 파이썬 선택을 uv 에 그대로"
    log = upd.logtext()
    for needle in (f"=== 업데이트 적용 시작 -> {NEW} ===", f"  설치폴더 : {upd.root}", f"본체(PID {old}) 종료 대기...",
                   f"백업 완료: {upd.backup}", "파일 교체 완료", "  uv: Resolved 9 packages", f"의존성 동기화 완료 (새 버전 {NEW})",
                   f"헬스체크 통과 - 업데이트 성공 ({NEW})"):
        assert needle in log, needle
    assert "강제 종료" not in log and "롤백" not in log
    assert log.rstrip().endswith("=== 종료 (성공: true) ===")


# ---------------------------------------------------------------- R② 버전 불일치 → 롤백 (R⑨·R⑮)

@with_shells
def test_version_mismatch_rolls_back_to_the_old_version(upd, shell, as_data_dir):
    upd.set_fake(upd.stage, report_version="9.9.9")         # 새 코드가 엉뚱한 버전을 말한다
    old = upd.start_old()
    rc = upd.run(upd.argv(wait_pid=old, health_seconds=3), shell=shell, stop_host=old)
    assert rc == 1, upd.diag()
    assert upd.read("pwa/index.html") == "old pwa" and upd.read("README.md") == "old readme"
    assert json.loads(upd.read("session_manager/fake.json"))["tag"] == "old"
    assert not (upd.root / "install.sh").exists(), "새로 생겼던 항목은 없어진다"
    assert upd.read("install.ps1") == "old ps1"
    failed = upd.work / f"backup-{OLD}-failed-{NEW}"
    assert (failed / "pwa" / "index.html").read_text(encoding="utf-8") == "new pwa" and (failed / "install.sh").exists()
    assert upd.running_tag() == "old"
    assert upd.fake(upd.runtime()["pid"])["version"] == OLD, "롤백 uv sync 로 dist-info 도 옛 버전"
    assert [pid for pid, tag in upd.fakes() if tag == "new" and not _gone(pid)] == [], "실패한 새 Host 는 멈췄다"
    upd.clean_after()
    last = upd.last()
    assert last["version"] == NEW and last["ok"] is False
    assert updater.recently_failed(NEW) is True, "같은 버전을 재기동 자동 적용이 또 받지 않는다(롤백 반복 차단)"
    assert [(c["args"][0], c["version"]) for c in upd.uv_calls()] == [("sync", NEW), ("sync", OLD)]
    log = upd.logtext()
    for needle in ("헬스체크 실패 (port=", "응답한 버전이 다릅니다: 9.9.9", "롤백합니다.", "실패한 Host 를 멈춥니다(SIGTERM)",
                   "이전 버전 복원 완료", "의존성 동기화 완료 (롤백)", "롤백 후 정상 동작 확인"):
        assert needle in log, needle
    assert log.rstrip().endswith("=== 종료 (성공: false) ===")


def test_new_version_that_dies_at_start_rolls_back_without_waiting(upd):
    """새 버전이 뜨자마자 죽으면(런처 종료코드 1) 헬스 45초를 기다리지 않고 롤백한다."""
    upd.set_fake(upd.stage, exit_now=True)
    old = upd.start_old()
    t0 = time.monotonic()
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    assert rc == 1 and time.monotonic() - t0 < 40, upd.diag()
    log = upd.logtext()
    assert "런처가 실패했습니다(종료코드 1)" in log and "재기동이 안 돼 헬스체크를 건너뜁니다" in log
    assert "롤백 후 정상 동작 확인" in log and upd.running_tag() == "old"
    upd.clean_after()


# ---------------------------------------------------------------- R③ 의존성

def test_deps_failing_twice_rolls_back_without_starting_the_new_version(upd):
    upd.uv_conf(fail_sync=[NEW], fail_pip=[NEW])
    old = upd.start_old()
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    assert rc == 1, upd.diag()
    assert "new" not in [tag for _, tag in upd.fakes()], "새 버전은 띄우지도 않는다(헬스에 맡기지 않음)"
    assert upd.read("pwa/index.html") == "old pwa" and upd.running_tag() == "old"
    calls = upd.uv_calls()
    assert [(c["args"][0], c["version"]) for c in calls] == [("sync", NEW), ("pip", NEW), ("sync", OLD)]
    assert calls[1]["args"] == ["pip", "install", "--python", str(upd.py), "-e", str(upd.root)]
    log = upd.logtext()
    assert f"[FAIL] 의존성 동기화 실패(새 버전 {NEW})" in log and "  pip: error:" in log and "  uv: error:" in log
    assert "헬스체크 실패" not in log and "롤백합니다." in log and "롤백 후 정상 동작 확인" in log
    assert upd.last()["ok"] is False
    upd.clean_after()


def test_uv_sync_failure_falls_back_to_pip(upd):
    upd.uv_conf(fail_sync=[NEW])
    old = upd.start_old()
    assert upd.run(upd.argv(wait_pid=old), stop_host=old) == 0, upd.diag()
    assert [c["args"][0] for c in upd.uv_calls()] == ["sync", "pip"]
    assert upd.running_tag() == "new" and upd.fake(upd.runtime()["pid"])["version"] == NEW
    assert "uv sync 실패(종료코드 1)" in upd.logtext()


# ---------------------------------------------------------------- R④ 본체 대기·강제 종료·pid 재사용

@procfs_variants
def test_host_that_ignores_term_is_killed_after_the_wait(upd, procfs):
    if not procfs:
        upd.no_procfs()
    upd.set_fake(upd.root, ignore_term=True)
    old = upd.start_old()
    t0 = time.monotonic()
    rc = upd.run(upd.argv(wait_pid=old, wait_seconds=1))           # 재기동 자동 적용 모양 — Host 가 스스로 안 내려간다
    took = time.monotonic() - t0
    assert rc == 0, upd.diag()
    assert _gone(old) and not (upd.data / f"term-{old}").exists(), "TERM 은 무시됐고 10초 뒤 KILL"
    assert 10 <= took < 60, took
    log = upd.logtext()
    assert "시간 내에 안 죽어서 강제 종료합니다." in log and "SIGKILL" in log
    assert upd.running_tag() == "new"
    upd.clean_after()


@procfs_variants
def test_reused_wait_pid_is_never_signalled(upd, procfs):
    """WaitPid 를 다른 프로그램이 받았다 — 같은 모듈 이름이라도 다른 설치본이면 신호를 보내지 않는다(S27)."""
    if not procfs:
        upd.no_procfs()
    old = upd.start_old()
    os.kill(old, signal.SIGTERM)
    assert _wait(lambda: _gone(old), 10)
    decoy = upd.popen([sys.executable, "-c", "import time; time.sleep(120)", "-m", "session_manager.server"])
    rc = upd.run(upd.argv(wait_pid=decoy.pid, wait_seconds=1))
    assert rc == 0, upd.diag()
    assert decoy.poll() is None, "남의 프로세스는 살아 있어야 한다"
    log = upd.logtext()
    assert f"PID {decoy.pid} 는 이 설치본의 Host 가 아닙니다" in log and "강제 종료" not in log
    assert upd.running_tag() == "new"


# ---------------------------------------------------------------- R⑤ 롤백은 브로커를 건드리지 않는다

@procfs_variants
def test_rollback_stops_failed_hosts_but_spares_the_broker(upd, procfs):
    if not procfs:
        upd.no_procfs()
    upd.set_fake(upd.stage, report_version="9.9.9")
    broker = upd.start_broker()
    stray = upd.popen([str(upd.py), "-c", "import time; time.sleep(120)", "-m", "session_manager.server"])
    decoy = upd.popen([sys.executable, "-c", "import time; time.sleep(120)", "-m", "session_manager.server"])
    old = upd.start_old()
    rc = upd.run(upd.argv(wait_pid=old, health_seconds=3), stop_host=old)
    assert rc == 1, upd.diag()
    assert _wait(lambda: stray.poll() is not None, 10), "이 설치본의 Host 모양 프로세스는 멈춘다"
    assert decoy.poll() is None, "다른 설치본의 프로세스는 그대로"
    assert not _gone(broker), "브로커(session_manager.ptyd)는 롤백에서 빠진다(S26)"
    news = [pid for pid, tag in upd.fakes() if tag == "new"]
    assert news and all(_gone(p) for p in news), "실패한 새 Host 는 멈췄다"
    assert upd.running_tag() == "old"
    upd.clean_after()


# ---------------------------------------------------------------- R⑥ 러너는 하나

def test_second_runner_exits_3_and_touches_nothing(upd):
    lock = upd.work / "runner.lock.d"
    lock.mkdir()
    holder = upd.holder("update_runner.sh")
    (lock / "pid").write_text(f"{holder.pid}\n", encoding="utf-8")
    before = upd.tree()
    rc = upd.run(upd.argv(wait_pid=DEAD_PID))
    assert rc == 3, upd.diag()
    assert (upd.work / "apply.lock").exists(), "apply.lock 은 돌고 있는 러너의 것 — 건드리지 않는다"
    assert (lock / "pid").read_text(encoding="utf-8").strip() == str(holder.pid)
    assert upd.tree() == before and not upd.backup.exists() and not (upd.work / "last_apply.json").exists()
    assert not (upd.data / "update.inprogress").exists() and upd.uv_calls() == []
    assert "다른 러너가 실행 중입니다" in upd.logtext()


def test_live_install_marker_also_exits_3(upd):
    holder = upd.holder("install.sh")
    marker = upd.data / "update.inprogress"
    marker.write_text(f"pid={holder.pid}\nat={int(time.time())}\nby=install\n", encoding="utf-8")
    rc = upd.run(upd.argv(wait_pid=DEAD_PID))
    assert rc == 3, upd.diag()
    assert marker.read_text(encoding="utf-8").startswith(f"pid={holder.pid}\n"), "설치의 표식은 그대로"
    assert not (upd.work / "runner.lock.d").exists() and (upd.work / "apply.lock").exists()
    assert "업데이트·설치가 진행 중입니다" in upd.logtext()


def test_dead_runner_lock_and_marker_are_taken_over(upd):
    lock = upd.work / "runner.lock.d"
    lock.mkdir()
    (lock / "pid").write_text(f"{DEAD_PID}\n", encoding="utf-8")
    (upd.data / "update.inprogress").write_text(f"pid={DEAD_PID}\nat={int(time.time())}\nby=runner\n",
                                                encoding="utf-8")
    rc = upd.run(upd.argv(wait_pid=DEAD_PID))                    # (옛 Host 없이 — 죽은 러너가 내려 둔 상태)
    assert rc == 0, upd.diag()
    assert "버려진 러너 잠금을 치웁니다" in upd.logtext() and upd.running_tag() == "new"
    upd.clean_after()


# ---------------------------------------------------------------- R⑦ 교체 도중 실패

def test_swap_failure_mid_way_puts_moved_items_back(upd):
    old = upd.start_old()
    before = upd.tree()
    os.chmod(upd.root / "pwa", 0o500)          # 다른 폴더로 rename 하려면 쓰기 권한이 필요(.. 를 고쳐 쓴다) — 두 번째 차례
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    os.chmod(upd.root / "pwa", 0o755)
    assert rc == 1, upd.diag()
    assert upd.tree() == before, "앞서 옮긴 session_manager 가 제자리로 — 설치 폴더는 이전 그대로"
    assert upd.running_tag() == "old", "옛 버전으로 다시 띄운다"
    assert upd.uv_calls() == [], "새 버전 의존성은 건드리지 않았다"
    assert upd.last()["ok"] is False
    log = upd.logtext()
    assert "[FAIL] 교체 중 오류: pwa" in log and "옮긴 항목을 되돌렸습니다" in log
    upd.clean_after()


def test_backup_failure_stops_before_touching_the_install(upd):
    old = upd.start_old()
    before = upd.tree()
    os.chmod(upd.root / "uv.lock", 0o000)      # 읽을 수 없다 → 백업 복사 실패
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    os.chmod(upd.root / "uv.lock", 0o644)
    assert rc == 1, upd.diag()
    assert upd.tree() == before and not upd.backup.exists()
    assert "[FAIL] 백업 실패 - 되돌릴 수 없으므로 여기서 멈춥니다" in upd.logtext()
    assert upd.running_tag() == "old" and upd.uv_calls() == [] and upd.last()["ok"] is False
    upd.clean_after()


# ---------------------------------------------------------------- R⑩ runtime.json 포트

def test_health_follows_the_runtime_json_port(upd):
    """-Port 가 옛 값이어도(새 Host 가 폴백 포트로 떴을 때처럼) runtime.json 의 포트로 헬스를 본다."""
    old = upd.start_old()
    rc = upd.run(upd.argv(wait_pid=old, port=_free_port()), stop_host=old)
    assert rc == 0, upd.diag()
    assert f"헬스체크 통과 - 업데이트 성공 ({NEW})" in upd.logtext()


# ---------------------------------------------------------------- R⑪ 개발 체크아웃

@pytest.mark.parametrize("marker", [".git", "ops"])
def test_dev_checkout_is_refused_and_left_as_it_is(upd, marker):
    (upd.root / marker).mkdir()
    old = upd.start_old()
    before = upd.tree()
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    assert rc == 2, upd.diag()
    assert upd.tree() == before and not upd.backup.exists() and upd.uv_calls() == []
    assert "[FAIL] 적용하지 않습니다: 개발 체크아웃" in upd.logtext()
    last = upd.last()
    assert last["version"] == NEW and last["ok"] is False
    upd.clean_after()
    pid = upd.runtime()["pid"]
    assert pid != old and upd.running_tag() == "old", "스스로 내려간 Host(수동 적용)는 옛 버전으로 다시 띄운다"


# ---------------------------------------------------------------- R⑫ 나쁜 인자

def _bad_argv(upd: Upd, case: str, old: int) -> tuple[list[str], Path | None]:
    a = upd.argv(wait_pid=old, wait_seconds=1)
    i = {k: a.index(k) for k in ("-InstallRoot", "-StageDir", "-BackupDir", "-WaitPid", "-Port", "-Version", "-LogFile")}

    def put(flag, value):
        a[i[flag] + 1] = value
    cwd = None
    if case == "relative":
        cwd = upd.base / "rel"
        for n in ("app/session_manager", "stage/session_manager", "backup"):
            (cwd / n).mkdir(parents=True)
            (cwd / n / "keep").write_text("x", encoding="utf-8")
        put("-InstallRoot", "app")
        put("-StageDir", "stage")
        put("-BackupDir", "backup")
        put("-LogFile", "apply.log")
    elif case == "empty-root":
        put("-InstallRoot", "")
    elif case == "root-is-slash":
        put("-InstallRoot", "/")
    elif case == "root-is-home":
        put("-InstallRoot", str(upd.home))
    elif case == "backup-outside-work":
        put("-BackupDir", str(upd.base / "elsewhere"))
    elif case == "backup-relative":
        put("-BackupDir", "backup-x")
    elif case == "dotdot":
        put("-StageDir", str(upd.work / "stage-x" / ".." / f"stage-{NEW}"))
    elif case == "version-path":
        put("-Version", "1.1.0/../../x")
    elif case == "version-mismatch":
        put("-Version", "1.2.0")
    elif case == "log-control-char":
        put("-LogFile", str(upd.work / "apply\t.log"))
    elif case == "wait-pid-zero":
        put("-WaitPid", "0")
    elif case == "port-out-of-range":
        put("-Port", "70000")
    elif case == "missing-stage":
        del a[i["-StageDir"]:i["-StageDir"] + 2]
    elif case == "unknown-flag":
        a += ["-Bogus", "x"]
    return a, cwd


@pytest.mark.parametrize("case", ["relative", "empty-root", "root-is-slash", "root-is-home", "backup-outside-work",
                                  "backup-relative", "dotdot", "version-path", "version-mismatch", "log-control-char",
                                  "wait-pid-zero", "port-out-of-range", "missing-stage", "unknown-flag"])
def test_bad_arguments_change_nothing_and_remove_nothing(upd, case):
    old = upd.start_old()
    a, cwd = _bad_argv(upd, case, old)
    fakebin = upd.base / "fakebin"
    fakebin.mkdir()
    (fakebin / "rm").write_text(FAKE_RM, encoding="utf-8")
    os.chmod(fakebin / "rm", 0o755)
    rm_log = upd.base / "rm.log"
    before, stage_before = upd.tree(), upd.tree(upd.stage)
    rel_before = upd.tree(cwd) if cwd else None
    rc = upd.run(a, cwd=cwd, env=upd.env(PATH=f"{fakebin}:{os.environ['PATH']}", FAKE_RM_LOG=rm_log))
    assert rc == 2, upd.diag()
    assert upd.tree() == before and upd.tree(upd.stage) == stage_before
    if cwd:
        assert upd.tree(cwd) == rel_before and not (cwd / "apply.log").exists()
    calls = upd.text(rm_log).splitlines()
    assert set(calls) <= {f"-f {upd.work / 'apply.lock'}"}, f"정해 둔 것 말고는 지우지 않는다: {calls}"
    if case in ("relative", "log-control-char"):
        assert calls == [], "로그 자리도 모르면 아무것도 지우지 않는다"
    assert not upd.backup.exists() and upd.swaps() == [] and upd.uv_calls() == []
    assert not (upd.data / "update.inprogress").exists() and not (upd.work / "runner.lock.d").exists()
    assert upd.hosts() == [old], "떠 있는 Host 는 건드리지 않는다(스스로 안 내려가면 그대로 둔다)"


# ---------------------------------------------------------------- R⑬ 저널 잔재 복구(-RecoverOnly)

@pytest.mark.parametrize("steps,dangling", [(1, False), (1, True), (2, False), (3, True), (99, False)],
                         ids=["old-moved", "old-moved+next-line", "new-moved", "next-old-moved+line", "all-moved"])
def test_recover_only_puts_the_old_tree_back(upd, steps, dangling, as_data_dir):
    before = upd.tree()
    sd = _leftover(upd, steps, dangling=dangling)
    assert upd.tree() != before
    r = upd.recover()
    assert r.returncode == 0, upd.diag(r.stderr.decode("utf-8", "replace"))
    assert upd.tree() == before and not sd.exists()
    assert upd.last()["version"] == NEW and upd.last()["ok"] is False and updater.recently_failed(NEW)
    assert [(c["args"][0], c["version"]) for c in upd.uv_calls()] == [("sync", OLD)], "dist-info 도 옛 버전으로"
    log = upd.text(upd.work / "recover.log")
    assert "중단된 업데이트를 되돌립니다: .clewpath-swap.4242" in log and "=== 종료 (성공: true) ===" in log
    assert not (upd.data / "update.inprogress").exists() and not (upd.work / "runner.lock.d").exists()


def test_recover_only_is_idempotent_after_a_crash_mid_undo(upd):
    """되돌리다 죽은 상태(저널 뒤쪽 몇 줄만 되돌림)에서 다시 돌려도 옛 트리 — 되돌림이 rm 이 아니라 rename 이라서."""
    before = upd.tree()
    sd = _leftover(upd, 99)
    lines = (sd / "journal").read_text(encoding="utf-8").splitlines()
    for line in reversed(lines[-3:]):                         # 러너의 되돌림을 끝에서 세 줄만 흉내
        op, n = line.split(" ", 1)
        if op == "new":
            (upd.root / n).rename(sd / "new" / n)
        else:
            (sd / "old" / n).rename(upd.root / n)
    r = upd.recover()
    assert r.returncode == 0, upd.diag()
    assert upd.tree() == before and not sd.exists()


def test_recover_only_keeps_a_committed_update(upd):
    sd = _leftover(upd, 99, committed=True)
    after = {k: v for k, v in upd.tree().items() if not k.startswith(".clewpath-swap.")}
    r = upd.recover()
    assert r.returncode == 0, upd.diag()
    assert upd.tree() == after and not sd.exists(), "헬스를 통과한 교체는 되돌리지 않고 저널만 치운다"
    assert upd.uv_calls() == [] and not (upd.work / "last_apply.json").exists()


def test_recover_only_with_nothing_to_do(upd):
    r = upd.recover()
    assert r.returncode == 0 and "교체 잔재가 없습니다" in upd.text(upd.work / "recover.log")


@pytest.mark.parametrize("broken", ["format", "name", "torn-last-line", "no-root-module"])
def test_recover_only_refuses_what_it_does_not_understand(upd, broken):
    if broken == "format":
        sd = _leftover(upd, 2, fmt="2")
    elif broken in ("name", "torn-last-line"):                  # 줄바꿈 없이 끊긴 끝줄도 검사한다
        sd = _leftover(upd, 2)
        with open(sd / "journal", "a", encoding="utf-8") as j:
            j.write("old ../../evil" + ("\n" if broken == "name" else ""))
    else:                                                      # session_manager 도 교체 저널도 없는 폴더
        shutil.rmtree(upd.root / "session_manager")
        sd = None
    snap = upd.tree()
    r = upd.recover()
    if broken == "no-root-module":
        assert r.returncode == 2, upd.diag()
    else:
        assert r.returncode == 1, upd.diag()
        assert upd.tree() == snap and sd.exists(), "모르면 아무것도 옮기지 않는다"
    assert upd.uv_calls() == []


def test_leftover_is_put_back_before_the_next_update(upd):
    """지난 러너가 재기동 뒤(헬스 전) 죽어 잔재가 남은 채 Host 가 떠 있다 → 다음 업데이트가 먼저 되돌리고 진행한다."""
    old = upd.start_old()
    _leftover(upd, 99)
    rc = upd.run(upd.argv(wait_pid=old), stop_host=old)
    assert rc == 0, upd.diag()
    log = upd.logtext()
    assert log.index("중단된 업데이트를 되돌립니다") < log.index("백업 완료")
    assert (upd.backup / "pwa" / "index.html").read_text(encoding="utf-8") == "old pwa", "백업은 되돌린 옛 트리"
    assert [(c["args"][0], c["version"]) for c in upd.uv_calls()] == [("sync", OLD), ("sync", NEW)]
    assert upd.running_tag() == "new"
    upd.clean_after()


@pytest.mark.parametrize("crash_at", [*range(1, SWAP_RENAMES + 1), SWAP_RENAMES + 1, SWAP_RENAMES + 2,
                                      SWAP_RENAMES + 7, 2 * SWAP_RENAMES])
def test_killed_at_any_rename_is_put_back_exactly(upd, crash_at, as_data_dir):
    """교체(1~13번째 rename)든 롤백의 되돌림(14~26번째)이든 어디서 죽어도(SIGKILL) 다음 -RecoverOnly 가 옛 트리를 정확히
    되돌린다 — 저널을 rename 직전에 쓰고, 되돌림도 지우지 않고 반대로 옮기기만 해서(멱등)."""
    if crash_at > SWAP_RENAMES:
        upd.set_fake(upd.stage, report_version="9.9.9")           # 롤백까지 가게
    before = upd.tree()
    fakebin = upd.base / "fakebin"
    fakebin.mkdir()
    (fakebin / "mv").write_text(FAKE_MV, encoding="utf-8")
    os.chmod(fakebin / "mv", 0o755)
    count = upd.base / "mv-count"
    rc = upd.run(upd.argv(wait_pid=DEAD_PID, health_seconds=1),
                 env=upd.env(PATH=f"{fakebin}:{os.environ['PATH']}", CRASH_AT=crash_at, CRASH_COUNT=count))
    assert rc == -signal.SIGKILL and int(count.read_text(encoding="utf-8")) == crash_at, upd.diag()
    assert upd.swaps(), "죽은 자리에 교체 저널이 남았다"
    r = upd.recover()                                              # 진짜 mv 로(런처 start 가 부르는 것과 같다)
    assert r.returncode == 0, upd.diag(r.stderr.decode("utf-8", "replace"))
    assert upd.tree() == before and upd.swaps() == []
    assert upd.last()["version"] == NEW and updater.recently_failed(NEW)


def test_launcher_recovers_leftovers_with_the_real_runner(upd):
    """런처 start 가 교체 잔재를 보면 데이터 폴더의 러너 사본으로 -RecoverOnly 한 뒤 띄운다(3.3 L⑩ 의 진짜 러너판)."""
    before = upd.tree()
    _leftover(upd, 2)
    r = subprocess.run(["/bin/sh", str(upd.launcher), "start"], env=upd.env(), capture_output=True, timeout=60,
                       cwd=str(upd.home))
    assert r.returncode == 0, upd.diag(r.stderr.decode("utf-8", "replace"))
    assert upd.tree() == before and upd.running_tag() == "old"
    assert "recover: 완료" in upd.text(upd.data / "launcher.log")
    assert "중단된 업데이트 되돌림 완료" in upd.text(upd.work / "recover.log")


def test_recover_only_waits_while_a_host_runs_on_the_leftover(upd):
    """러너가 재기동 뒤(헬스 전) 죽어 잔재가 남은 채 Host 가 떠 있다 — 그 Host 가 이 파일로 돌고 있으니 지금은 되돌리지
    않는다(종료코드 3 = 런처는 보류하고 0). Host 가 내려간 뒤의 start 가 되돌리고 옛 버전을 띄운다."""
    before = upd.tree()
    old = upd.start_old()
    sd = _leftover(upd, 99)
    snap = upd.tree()
    r = upd.recover()
    assert r.returncode == 3, upd.diag()
    assert upd.tree() == snap and sd.exists() and upd.hosts() == [old]
    assert f"이 설치본의 Host 가 떠 있어(pid {old})" in upd.text(upd.work / "recover.log")

    def start():
        return subprocess.run(["/bin/sh", str(upd.launcher), "start"], env=upd.env(), capture_output=True, timeout=60,
                              cwd=str(upd.home))
    r = start()
    assert r.returncode == 0 and upd.hosts() == [old] and sd.exists(), upd.diag()
    assert "skip: 복구 보류(rc=3" in upd.text(upd.data / "launcher.log")
    os.kill(old, signal.SIGTERM)
    assert _wait(lambda: _gone(old), 10)
    r = start()
    assert r.returncode == 0, upd.diag(r.stderr.decode("utf-8", "replace"))
    assert upd.tree() == before and not sd.exists() and upd.running_tag() == "old"


def test_uv_is_found_from_the_record_then_clewpath_home_then_path(upd):
    """launchd·분리 기동엔 XDG 변수가 없을 수 있어 설치 기록의 uv= 가 먼저, 다음 공통 경로의 bin/uv, 그다음 PATH."""
    def leftover_and_recover(env=None):
        _leftover(upd, 1)
        r = upd.recover(env=env)
        assert r.returncode == 0, upd.diag()
        return upd.uv_calls()[-1]

    c = leftover_and_recover()
    assert c["exe"] == str(upd.uv)
    assert c["env"]["UV_PYTHON_PREFERENCE"] == "only-system" and c["env"]["UV_PYTHON_INSTALL_DIR"] == str(upd.chome / "python")
    upd.record(uv=None, python_pref=None, python_install_dir=None)
    (upd.chome / "bin").mkdir(parents=True)
    shutil.copy2(upd.uv, upd.chome / "bin" / "uv")
    c = leftover_and_recover()
    assert c["exe"] == str(upd.chome / "bin" / "uv")
    assert c["env"]["UV_PYTHON_PREFERENCE"] is None and c["env"]["UV_PYTHON_INSTALL_DIR"] is None
    (upd.chome / "bin" / "uv").unlink()
    c = leftover_and_recover(env=upd.env(PATH=f"{upd.tools}:/usr/bin:/bin"))
    assert c["exe"] == str(upd.uv)
    n = len(upd.uv_calls())
    _leftover(upd, 1)
    r = upd.recover(env=upd.env(PATH="/usr/bin:/bin"))         # (이 PC 의 진짜 uv 가 PATH 에 있어도 부르지 않게)
    assert r.returncode == 0 and len(upd.uv_calls()) == n
    assert "uv 나 venv 를 못 찾아 의존성 동기화를 건너뜁니다 (복구)" in upd.text(upd.work / "recover.log")


# ---------------------------------------------------------------- R⑭ TERM

def test_term_before_the_swap_aborts_and_keeps_the_old_version(upd):
    old = upd.start_old()
    before = upd.tree()
    p = upd.spawn(upd.argv(wait_pid=old, wait_seconds=20))       # Host 는 아직 안 내려갔다
    assert _wait(lambda: (upd.data / "update.inprogress").exists(), 10), upd.diag()
    p.send_signal(signal.SIGTERM)
    assert _wait(lambda: "TERM 을 받아 업데이트를 중단합니다" in upd.logtext(), 10), upd.diag()
    os.kill(old, signal.SIGTERM)                                 # 그 뒤 Host 가 스스로 내려가면(수동 적용) 다시 띄운다
    assert p.wait(timeout=60) == 1, upd.diag()
    assert upd.tree() == before and not upd.backup.exists() and upd.uv_calls() == []
    pid = upd.runtime()["pid"]
    assert pid != old and upd.running_tag() == "old"
    assert upd.last()["ok"] is False
    upd.clean_after()


def test_term_after_the_swap_started_runs_to_the_end(upd):
    upd.uv_conf(sleep_sync=3)
    old = upd.start_old()
    p = upd.spawn(upd.argv(wait_pid=old), stop_host=old)
    assert _wait(lambda: (upd.ctl / "syncing").exists(), 30), upd.diag()
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=90) == 0, upd.diag()
    assert upd.running_tag() == "new" and upd.last()["ok"] is True
    assert "교체가 시작된 뒤 TERM 을 받아 끝까지 진행했습니다" in upd.logtext()
    upd.clean_after()


# ---------------------------------------------------------------- S24·S31 도는 동안: 표식·잠금·하트비트, 감독자는 기다린다

def test_supervisor_waits_while_the_runner_works(upd):
    upd.uv_conf(sleep_sync=4)
    old = upd.start_old()
    t0 = time.time()
    p = upd.spawn(upd.argv(wait_pid=old), stop_host=old)
    assert _wait(lambda: (upd.ctl / "syncing").exists(), 30), upd.diag()
    mk = _kv((upd.data / "update.inprogress").read_text(encoding="utf-8"))
    assert mk["pid"] == str(p.pid) and mk["by"] == "runner" and abs(int(mk["at"]) - t0) < 60
    assert (upd.work / "runner.lock.d" / "pid").read_text(encoding="utf-8").strip() == str(p.pid)
    assert (upd.work / "apply.lock").stat().st_mtime >= t0 - 1, "단계마다 apply.lock 하트비트"
    assert upd.swaps() == [f".clewpath-swap.{p.pid}"]
    r = subprocess.run(["/bin/sh", str(upd.launcher), "start"], env=upd.env(), capture_output=True, timeout=60,
                       cwd=str(upd.home))
    assert r.returncode == 0 and upd.hosts() == [], "감독자(CLEWPATH_UPDATER 없는 런처)는 업데이트 중에 띄우지 않는다"
    st = subprocess.run(["/bin/sh", str(upd.launcher), "status"], env=upd.env(), capture_output=True, timeout=60)
    assert _kv(st.stdout.decode())["state"] == "updating"
    assert p.wait(timeout=90) == 0, upd.diag()
    assert upd.running_tag() == "new"
    assert "skip: 업데이트 진행 중" in upd.text(upd.data / "launcher.log")
    upd.clean_after()


# ---------------------------------------------------------------- 파이썬이 없을 때(헬스는 curl)

@pytest.mark.parametrize("report", [NEW, "9.9.9"])
def test_health_without_python_uses_curl(upd, report):
    """venv 파이썬도 python3 도 없다 — 의존성 동기화는 건너뛰고(로그), 헬스는 curl 문자열 검사로 본다(버전까지).
    재기동은 시험용 스크립트가 진짜 파이썬으로 가짜 Host 를 띄운다 — 이 Host 는 설치본 venv 로 뜨지 않아 롤백이
    멈추지 못하므로, 버전이 어긋나면 롤백 뒤에도 그 Host 가 답해 '버전 표기가 어긋납니다' 로 끝난다."""
    shutil.rmtree(upd.root / ".venv")
    upd.set_fake(upd.stage, report_version=report)
    fakebin = upd.base / "fakebin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(FAKE_CURL.replace("@PYTHON@", sys.executable), encoding="utf-8")
    os.chmod(fakebin / "curl", 0o755)
    restart = upd.base / "restart.sh"
    restart.write_text("#!/bin/sh\nCLEWPATH_CONFIG=%s %s %s </dev/null >/dev/null 2>&1 &\n" % (
        shlex.quote(str(upd.conf)), shlex.quote(sys.executable), shlex.quote(str(upd.root / "session_manager" / "server.py"))),
        encoding="utf-8")
    path = f"{fakebin}:/usr/bin:/bin"
    assert subprocess.run(["sh", "-c", "command -v python3"], env={"PATH": path}).returncode != 0, "PATH 에 python3 가 없어야"
    rc = upd.run(upd.argv(wait_pid=DEAD_PID, restart=str(restart), health_seconds=5), env=upd.env(PATH=path))
    log = upd.logtext()
    assert f"uv 나 venv 를 못 찾아 의존성 동기화를 건너뜁니다 (새 버전 {NEW})" in log
    calls = upd.text(upd.ctl / "curl-calls")
    assert "--noproxy * -s -m 3" in calls and "/api/owner/diagnostics" in calls
    if report == NEW:
        assert rc == 0, upd.diag()
        assert f"헬스체크 통과 - 업데이트 성공 ({NEW})" in log and upd.read("pwa/index.html") == "new pwa"
    else:
        assert rc == 1, upd.diag()
        assert f"응답한 버전이 다릅니다: 9.9.9 (기대 {NEW})" in log and upd.read("pwa/index.html") == "old pwa"
        assert ("[FAIL] 롤백 후에도 응답이 없습니다 - 수동 확인이 필요합니다. 버전 표기가 어긋납니다(응답 9.9.9, "
                f"기대 {OLD})") in log
    upd.clean_after()
