"""맥·리눅스 설치·제거 실행 시험 — docs/designs/posix-install.md 3.2·3.5 (+ S12·S16·S17·S24·S26·S36~S38·S43).

진짜 install.sh·uninstall.sh·런처·hostctl·Host(session_manager.server)·브로커를 가짜 홈에서 돌린다. 가짜는 uv 하나다:
- 가짜 uv: --version 은 고정 버전, `python find` 는 시스템(= 지금 파이썬)·관리 런타임(가짜 폴더), `python install` 은 관리
  런타임 폴더를 만든다, `venv` 는 `python -m venv --without-pip` + 지금 파이썬의 site-packages 를 가리키는 .pth(네트워크 없이
  의존성), `sync`·`pip install -e` 는 편집 설치 흉내(.pth 로 설치 루트 + dist-info 버전). 부른 모양(인자·cwd·env)을
  calls.jsonl 에 적고 uv.json 으로 실패를 넣는다.
- 설치 재료는 이 작업 트리의 PAYLOAD 사본(pwa 는 작은 대역) — get.sh 처럼 설치 루트 옆 .clewpath-get.XXXXXX/stage 에 둔다.
- 경로: 홈에 공백·한글(진짜 맥에서는 ASCII — 아래 HOME_LEAF). 설치 위치는 공통 경로 블록의 리눅스 기본값
  ($HOME/.local/share/clewpath/app). 진짜 맥에서도 mac=False 상자는 가짜 uname(Linux)으로 리눅스 길을 흉내 낸다.
진짜 uv(고정 해시 tarball)·오프라인 uv sync 로 도는 왕복은 컨테이너 e2e(6.3)의 몫이다. Windows 에선 건너뛴다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 설치(install.sh·uninstall.sh) — POSIX 전용")

if sys.platform != "win32":
    import psutil
    import pty

import session_manager
from session_manager import hostctl, updater
from session_manager.osdep import _posix

REAL_PKG = Path(session_manager.__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
DEAD_PID = 4_194_999
# 진짜 맥에서 도는 리눅스 흉내 Box(mac=False): uname 을 Linux 로 바꿔 끼우고(가짜 launchctl 은 늘 PATH 앞) 홈은 ASCII —
# 진짜 맥 ps 는 명령줄의 ASCII 밖 바이트를 바꿔 보여 줘 런처·러너가 Host 를 알아보지 못한다(설치가 맥에서 거부하는 까닭).
# 리눅스 시험장은 한글 홈 그대로(2026-10-22 맥 실기 — 한글 임시 경로가 맥 ASCII 가드에 걸려 80건이 본문을 못 돌았다)
ON_MAC = sys.platform == "darwin"
HOME_LEAF = "hom" if ON_MAC else "홈"
LINUX_UNAME = '#!/bin/sh\ncase "${1:-}" in -m) m=$(/usr/bin/uname -m); [ "$m" = arm64 ] && m=aarch64; echo "$m" ;; *) echo Linux ;; esac\n'
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

FAKE_UV = r'''#!@PYTHON@
"""가짜 uv — tests/test_install_sh.py. 제어: $FAKE_UV_CTL/uv.json {"version", "no_system", "fail": [...]}."""
import json, os, subprocess, sys, sysconfig, tomllib
from pathlib import Path

ctl = Path(os.environ["FAKE_UV_CTL"])
conf = json.loads((ctl / "uv.json").read_text(encoding="utf-8")) if (ctl / "uv.json").is_file() else {}
args = sys.argv[1:]
keep = ("UV_PYTHON_PREFERENCE", "UV_PYTHON_INSTALL_DIR", "UV_PROJECT_ENVIRONMENT", "UV_SYSTEM_CERTS", "UV_PYTHON_BIN_DIR",
        "VIRTUAL_ENV", "UV_PYTHON")
try:                                                  # 이때 설치 표식이 쥐어져 있나(감독자가 그동안 띄우지 않게)
    marker = Path(os.environ["FAKE_MARKER"]).read_text(encoding="utf-8")
except (KeyError, OSError):
    marker = None
parents = []                                          # 부른 쪽(install.sh·하위 셸)의 명령줄 — ps 로 누구나 보는 것
try:
    import psutil
    pr = psutil.Process().parent()
    while pr is not None and len(parents) < 4:
        parents.append(pr.cmdline())
        pr = pr.parent()
except Exception:
    pass
with open(ctl / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keep},
                        "marker": marker, "parents": parents}) + "\n")
fail = set(conf.get("fail", []))
managed = Path(os.environ.get("UV_PYTHON_INSTALL_DIR") or "/nonexistent") / "cpython-3.11.99-fake" / "bin" / "python3"


def editable(venv, tree):
    site = next((venv / "lib").glob("python3*/site-packages"))
    (site / "_clewpath_editable.pth").write_text(str(tree) + "\n", encoding="utf-8")
    ver = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    for d in site.glob("clewpath_host-*.dist-info"):
        for x in d.iterdir():
            x.unlink()
        d.rmdir()
    di = site / f"clewpath_host-{ver}.dist-info"
    di.mkdir()
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: clewpath-host\nVersion: {ver}\n", encoding="utf-8")
    print(f"Installed clewpath-host {ver} (fake)")


if args == ["--version"]:
    print(f"uv {conf.get('version', '0.12.1')} (fake)")
elif args[:2] == ["python", "find"]:
    pref = os.environ.get("UV_PYTHON_PREFERENCE")
    if pref == "only-system" and not conf.get("no_system"):
        print(sys.executable)
    elif pref == "only-managed" and managed.exists():
        print(managed)
    else:
        print("error: No interpreter found (fake)", file=sys.stderr)
        sys.exit(2)
elif args[:2] == ["python", "install"]:
    if "--help" in args:
        print("      --no-bin    Do not install a Python executable into the bin directory")
    elif "python-install" in fail:
        print("error: download failed (fake)", file=sys.stderr)
        sys.exit(1)
    else:
        managed.parent.mkdir(parents=True, exist_ok=True)
        if not managed.exists():
            managed.symlink_to(sys.executable)
        print("Installed Python 3.11.99 (fake)")
elif args[:1] == ["venv"]:
    py, target = Path(args[args.index("--python") + 1]), Path(args[-1])
    if target.exists() or "venv" in fail:
        print(f"error: A directory already exists at: {target}", file=sys.stderr)
        sys.exit(2)
    subprocess.run([str(py), "-m", "venv", "--without-pip", str(target)], check=True)
    site = next((target / "lib").glob("python3*/site-packages"))
    deps = sorted({sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]})
    (site / "_clewpath_test_deps.pth").write_text("\n".join(deps) + "\n", encoding="utf-8")
elif args[:1] == ["sync"]:
    if "sync" in fail:
        print("error: Failed to fetch (fake sync)", file=sys.stderr)
        sys.exit(1)
    editable(Path(os.environ["UV_PROJECT_ENVIRONMENT"]), Path.cwd())
elif args[:2] == ["pip", "install"]:
    if "pip" in fail:
        print("error: Failed to build (fake pip)", file=sys.stderr)
        sys.exit(1)
    editable(Path(args[args.index("--python") + 1]).parent.parent, Path(args[args.index("-e") + 1]))
else:
    print(f"error: unknown fake uv call {args}", file=sys.stderr)
    sys.exit(64)
'''


# 가짜 launchctl — 받은 인자를 탭으로 이어 한 줄씩 적는다(맨 앞 칸: 그때 maintenance.flag 가 있었나 F/-). 올림 상태는 파일 하나.
# FAKE_LAUNCHCTL: ok(기본) | fail(bootstrap 늘 실패 — SSH 셸처럼 GUI 세션이 없다) | fail-once(첫 bootstrap 만 — 막 내린 이름이
# 정리 중) | fail-bootout(올라 있어도 내리지 못한다). FAKE_LAUNCHD_RUN=1 이면 올릴 때 RunAtLoad 처럼 그 작업을 한 번 돌린다
FAKE_LAUNCHCTL = r"""#!/bin/sh
st="$FAKE_LAUNCHCTL_LOG.loaded"
{ if [ -e "${FAKE_FLAG:-/nonexistent}" ]; then printf '%s\t' F; else printf '%s\t' -; fi
  for a in "$@"; do printf '%s\t' "$a"; done; printf '\n'; } >>"$FAKE_LAUNCHCTL_LOG"
case "$1" in
  bootout)
    [ "${FAKE_LAUNCHCTL:-ok}" = fail-bootout ] && { echo "Boot-out failed: 125: Domain does not support specified action" >&2; exit 125; }
    [ -e "$st" ] || { echo "Boot-out failed: 3: No such process" >&2; exit 3; }
    rm -f "$st"; exit 0 ;;
  bootstrap)
    case "${FAKE_LAUNCHCTL:-ok}" in
      fail) echo "Bootstrap failed: 125: Domain does not support specified action" >&2; exit 125 ;;
      fail-once) if [ ! -e "$st.once" ]; then true >"$st.once"; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi ;;
    esac
    [ -f "$3" ] || { echo "Bootstrap failed: 2: No such file or directory" >&2; exit 2; }
    [ -e "$st" ] && { echo "Bootstrap failed: 37: Operation already in progress" >&2; exit 37; }
    cp "$3" "$st" || exit 1
    if [ -n "${FAKE_LAUNCHD_RUN:-}" ]; then fake-launchd "$3"; fi
    exit 0 ;;
  print) [ -e "$st" ] && exit 0; echo "Bad request. Could not find service" >&2; exit 113 ;;
esac
echo "fake launchctl: unknown $1" >&2
exit 64
"""

# launchd 흉내 — plist 대로 작업 하나를 돌린다(ProgramArguments·WorkingDirectory·EnvironmentVariables·StandardOutPath·
# StandardErrorPath·Umask, 새 세션). env 는 launchd 처럼 HOME·USER·LOGNAME·TMPDIR 와 launchd 기본 PATH 위에 EnvironmentVariables.
# 끝나기를(60초까지) 기다려 종료코드를 $FAKE_LAUNCHD_RC 에 이어 적는다. --pid 파일을 주면 띄운 pid 를 거기 먼저 적는다
FAKE_LAUNCHD = r'''#!@PYTHON@
import os, plistlib, subprocess, sys
with open(sys.argv[1], "rb") as f:
    d = plistlib.load(f)
env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "TMPDIR") if k in os.environ}
env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
env.update(d.get("EnvironmentVariables", {}))


def pre():
    if "Umask" in d:
        os.umask(d["Umask"])


with open(d["StandardOutPath"], "ab") as out, open(d["StandardErrorPath"], "ab") as err:
    p = subprocess.Popen(d["ProgramArguments"], cwd=d.get("WorkingDirectory", "/"), env=env, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=err, preexec_fn=pre, start_new_session=True)
if len(sys.argv) > 3 and sys.argv[2] == "--pid":
    with open(sys.argv[3], "w") as f:
        f.write(str(p.pid))
try:
    rc = str(p.wait(timeout=float(os.environ.get("FAKE_LAUNCHD_WAIT", "60"))))
except subprocess.TimeoutExpired:
    rc = "running"
with open(os.environ["FAKE_LAUNCHD_RC"], "a") as f:
    f.write(rc + "\n")
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, step: float = 0.2) -> bool:
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
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _fd_path(pid: int, fd: int) -> str | None:
    """그 프로세스의 fd 가 연 파일(실제 경로) — 리눅스는 /proc, 맥은 psutil(open_files 의 fd)."""
    if Path("/proc/self").is_dir():
        return os.path.realpath(os.readlink(f"/proc/{pid}/fd/{fd}"))
    return next((os.path.realpath(f.path) for f in psutil.Process(pid).open_files() if f.fd == fd), None)


def _tree_hash(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        h.update(str(f.relative_to(p)).encode())
        if f.is_file():
            h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module")
def payload(tmp_path_factory) -> Path:
    """zip 에 들어갈 것(PAYLOAD) — 작업 트리 사본. pwa 는 Host 가 요청 때만 읽으므로 작은 대역."""
    p = tmp_path_factory.mktemp("payload")
    shutil.copytree(REAL_PKG, p / "session_manager", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (p / "pwa").mkdir()
    (p / "pwa" / "index.html").write_text("<!doctype html><title>pwa</title>\n", encoding="utf-8")
    for n in updater.PAYLOAD:
        if n not in ("session_manager", "pwa"):
            shutil.copyfile(ROOT / n, p / n)
    return p


class _FakeCP:
    """가짜 컨트롤플레인 — 발급(횟수 기록), 원라인 설치 길(routes: get.sh·매니페스트·zip), 나머지(토큰·매니페스트 없음)는
    Host 가 fail-open 하는 응답."""

    def __init__(self):
        self.calls: list[str] = []
        self.routes: dict[str, tuple[bytes, str]] = {}
        box = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                box.calls.append(self.path)
                if self.path == "/cp/provision/anonymous":
                    self._send(200, {"connector_id": "c-77", "room_key": "ROOM-77", "credential_public_id": "pub-77",
                                     "secret": "SECRET-77", "claim_secret": "CLAIM-77"})
                else:
                    self._send(503, {"error": "unavailable"})

            def do_GET(self):
                box.calls.append(self.path)
                if self.path in box.routes:
                    body, ctype = box.routes[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._send(200, {"ok": True}) if self.path == "/cp/healthz" else self._send(404, {"error": "no_release"})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/cp"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Box:
    """가짜 홈 하나 — 설치 위치·클로드 폴더·가짜 uv·시험 TMPDIR. mac=True 면 맥 흉내(uname Darwin·가짜 launchctl, 맥 기본 설치
    위치 ~/Library/Application Support/ClewPath, 맥은 비ASCII 설치 경로를 거부하므로 홈은 ASCII — 공백·& 는 든다)."""

    def __init__(self, tmp: Path, payload: Path, mac: bool = False):
        self.base = base = tmp.resolve()
        self.mac = mac
        if mac:
            self.home = base / "mac & home" / "u"
            self.chome = self.home / "Library" / "Application Support" / "ClewPath"
        else:
            self.home = base / "home x" / HOME_LEAF
            self.chome = self.home / ".local" / "share" / "clewpath"
        self.root = self.chome / "app"
        self.claude = self.home / ".claude"
        self.projects = self.claude / "projects"
        self.data = self.claude / "session_manager"
        self.conf = self.data / "clewpath.toml"
        self.tmpdir = base / "tmp"
        self.ctl = base / "uv ctl"
        self.uv = self.chome / "bin" / "uv"
        self.launcher = self.root / _posix.START_SCRIPT
        self.vpy = self.root / ".venv" / "bin" / "python"
        self.port = _free_port()
        self.payload = payload
        self.procs: list[subprocess.Popen] = []
        for d in (self.projects / "-home-u-proj", self.tmpdir, self.ctl, self.uv.parent):
            d.mkdir(parents=True)
        (self.projects / "-home-u-proj" / "11111111-2222-3333-4444-555555555555.jsonl").write_text(
            json.dumps({"type": "user", "cwd": "/home/u/proj", "message": {"role": "user", "content": "hi"}}) + "\n",
            encoding="utf-8")
        self.uv.write_text(FAKE_UV.replace("@PYTHON@", sys.executable), encoding="utf-8")
        os.chmod(self.uv, 0o755)
        # 가짜 launchctl(리눅스 흉내에도 둔다 — 리눅스는 부르지 않아야 한다)·launchd 실행기, 맥 흉내면 uname 도
        self.tools = base / "fake tools"
        self.tools.mkdir()
        self.plist = self.home / "Library" / "LaunchAgents" / "com.clewpath.host.plist"
        self.lctl_log = base / "launchctl.log"
        self.launchd_rc = base / "launchd.rc"
        fakes = {"launchctl": FAKE_LAUNCHCTL, "fake-launchd": FAKE_LAUNCHD.replace("@PYTHON@", sys.executable)}
        if mac:
            fakes["uname"] = '#!/bin/sh\ncase "${1:-}" in -m) echo arm64 ;; *) echo Darwin ;; esac\n'
        elif ON_MAC:
            fakes["uname"] = LINUX_UNAME
        for name, body in fakes.items():
            (self.tools / name).write_text(body, encoding="utf-8")
            os.chmod(self.tools / name, 0o755)

    # ---- 준비
    def env(self, **extra) -> dict:
        drop = ("SESSION_MANAGER_CLAUDE_HOME", "XDG_DATA_HOME", "_SM_TEST_HOME_SET", "VIRTUAL_ENV", "PYTHONPATH",
                "CONDA_PREFIX")
        e = {k: v for k, v in os.environ.items()
             if k not in drop and not k.startswith(("SM_", "UV_", "CLEWPATH_", "CLAUDE", "FAKE_LAUNCH"))}
        e.update(HOME=str(self.home), TMPDIR=str(self.tmpdir), FAKE_UV_CTL=str(self.ctl), LANG="C.UTF-8",
                 FAKE_MARKER=str(self.data / "update.inprogress"), PATH=f"{self.tools}:{os.environ['PATH']}",
                 FAKE_LAUNCHCTL_LOG=str(self.lctl_log), FAKE_LAUNCHD_RC=str(self.launchd_rc),
                 FAKE_FLAG=str(self.data / "maintenance.flag"))
        if "PATH" in extra:                              # 시험이 준 도구 폴더도 앞에 — 가짜 launchctl·uname 은 늘 쓴다
            extra = {**extra, "PATH": f"{self.tools}:{extra['PATH']}"}
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def launchctl(self) -> list[list[str]]:
        """가짜 launchctl 이 받은 호출 — [표식 여부(F=maintenance.flag 있음 / -), 인자…]."""
        try:
            return [ln.rstrip("\t").split("\t") for ln in self.lctl_log.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def uv_conf(self, **conf) -> None:
        (self.ctl / "uv.json").write_text(json.dumps(conf), encoding="utf-8")

    def calls(self) -> list[dict]:
        try:
            return [json.loads(x) for x in (self.ctl / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def stage(self, readme: str | None = None) -> Path:
        """get.sh 가 푼 모양 — 설치 루트 옆(같은 파일 시스템) .clewpath-get.XXXXXX/stage."""
        self.chome.mkdir(parents=True, exist_ok=True)
        g = Path(tempfile.mkdtemp(prefix=".clewpath-get.", dir=self.chome))
        st = g / "stage"
        shutil.copytree(self.payload, st)
        if readme is not None:
            (st / "README.md").write_text(readme, encoding="utf-8")
        return st

    def unpack(self) -> None:
        """zip 을 손으로 푼 모양(get.sh 없이) — 설치 루트에 PAYLOAD 만."""
        shutil.copytree(self.payload, self.root)

    def args(self, *extra, port: int | None = None, cp: str = "http://127.0.0.1:9/cp", provision=False) -> list[str]:
        a = ["--cp-url", cp, "--allow-http", "--relay-url", "ws://127.0.0.1:9/relay/ws",
             "--app-url", "http://127.0.0.1:9/app", "--port", str(port or self.port)]
        return a + ([] if provision else ["--no-provision"]) + list(extra)

    def popen(self, argv, **kw) -> subprocess.Popen:
        kw.setdefault("stdin", subprocess.DEVNULL)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        p = subprocess.Popen(argv, **kw)
        self.procs.append(p)
        return p

    def holder(self, name: str) -> subprocess.Popen:
        return self.popen(["sh", "-c", "sleep 120; exit 0", name])

    # ---- 실행
    def sh(self, script: Path, *args, env=None, umask: str | None = None, timeout=240, shell=("/bin/sh",), **kw):
        argv = [*shell, str(script), *args]
        if umask:
            argv = ["/bin/sh", "-c", f'umask {umask}; exec "$@"', "sh", *argv]
        kw.setdefault("stdin", subprocess.DEVNULL)
        return subprocess.run(argv, env=env or self.env(), capture_output=True, timeout=timeout,
                              cwd=str(kw.pop("cwd", self.home)), **kw)

    def install_from(self, stage: Path, *extra, env=None, umask=None, **akw):
        return self.sh(stage / "install.sh", "--from-stage", str(stage), "--dir", str(self.root),
                       *self.args(*extra, **akw), env=env, umask=umask)

    def install(self, *extra, env=None, **akw):
        return self.sh(self.root / "install.sh", *self.args(*extra, **akw), env=env)

    def fresh(self, *extra, **kw):
        """get.sh 처럼 — 빈 설치 루트에 stage 를 옮기고 설치."""
        r = self.install_from(self.stage(), *extra, **kw)
        assert r.returncode == 0, self.diag(r)
        return r

    def uninstall(self, *args, **kw):
        return self.sh(self.root / "uninstall.sh", *args, **kw)

    # ---- 관찰
    def host_pids(self) -> list[int]:
        out = []
        for pr in psutil.process_iter(["pid", "cmdline", "status"]):
            c = pr.info["cmdline"] or []
            if c[:3] == [str(self.vpy), "-m", "session_manager.server"] and pr.info["status"] != psutil.STATUS_ZOMBIE:
                out.append(pr.info["pid"])
        return sorted(out)

    def host_pid(self) -> int:
        pids = self.host_pids()
        assert len(pids) == 1, (pids, self.diag())
        return pids[0]

    def get(self, path: str, port: int | None = None) -> dict | None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port or self.port}{path}", timeout=5) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            return None

    def text(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def status(self) -> dict:
        r = self.sh(self.launcher, "status")
        return _kv(r.stdout.decode("utf-8"))

    def record(self) -> dict:
        return _kv(self.text(self.root / ".clewpath-install"))

    def diag(self, r: subprocess.CompletedProcess | None = None) -> str:
        head = "" if r is None else (f"rc={r.returncode}\n--- stdout\n{r.stdout.decode('utf-8', 'replace')}\n"
                                     f"--- stderr\n{r.stderr.decode('utf-8', 'replace')}\n")
        return (head + "--- launcher.log\n" + self.text(self.data / "launcher.log")[-1500:] + "\n--- host.err.log\n"
                + self.text(self.data / "host.err.log")[-2500:])

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        me, key = os.getpid(), str(self.base)
        for pr in psutil.process_iter(["pid", "cmdline", "environ"]):
            try:
                hit = any(key in a for a in (pr.info["cmdline"] or []))
                hit = hit or (pr.info["environ"] or {}).get("HOME") == str(self.home)
                if pr.info["pid"] != me and hit:
                    pr.kill()
            except psutil.Error:
                pass
        for p in self.procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@pytest.fixture
def box(tmp_path, payload):
    b = Box(tmp_path, payload)
    try:
        yield b
    finally:
        b.cleanup()


@pytest.fixture
def mac(tmp_path, payload):
    """맥 흉내 — uname 이 Darwin, 가짜 launchctl, 맥 기본 설치 위치(공백·& 가 든 ASCII 홈)."""
    b = Box(tmp_path, payload, mac=True)
    try:
        yield b
    finally:
        b.cleanup()


# ---------------------------------------------------------------- 첫 설치(get.sh 경로)

def test_fresh_install_lays_out_secures_and_starts(box):
    r = box.fresh(umask="027", env=box.env(CLEWPATH_CLIENT_TOKEN="tok-SECRET-1"))
    out = r.stdout.decode("utf-8")
    for step in ("[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]", "=== 설치 완료 ===", "설치된 uv 재사용",
                 "시스템 파이썬 사용", "의존성 설치 완료 (uv.lock 고정 버전)", "서비스 기동 확인"):
        assert step in out, (step, box.diag(r))
    assert "tok-SECRET" not in out + r.stderr.decode("utf-8"), "토큰은 화면에 나오지 않는다(S18)"
    assert out.count("=== ClewPath Host 설치 준비") == 1 and out.count("=== ClewPath Host 설치 ===") == 1
    # 배치: PAYLOAD 가 설치 루트에, get.sh 의 임시 폴더는 치웠다
    assert all((box.root / n).exists() for n in updater.PAYLOAD)
    assert not list(box.chome.glob(".clewpath-get.*")) and not list(box.tmpdir.iterdir())
    # 런처·설치 기록(러너·제거 스크립트가 읽는다)
    assert _mode(box.launcher) == 0o755 and box.launcher.read_bytes().startswith(b"#!/bin/sh\n")
    rec = box.record()
    assert rec == {"home": str(box.chome), "uv": str(box.uv), "python": sys.executable, "python_pref": "only-system",
                   "python_install_dir": str(box.chome / "python"), "data_dir": str(box.data),
                   "conf_file": str(box.conf), "installed_version": VERSION, "installed_at": rec["installed_at"]}
    assert rec["installed_at"].isdigit()
    # 비밀: 데이터 폴더 0700, 설정 0600(BOM 없음), 토큰은 env 로 받아 설정 파일에만(S16·S17)
    assert _mode(box.data) == 0o700 and _mode(box.conf) == 0o600
    cfg = tomllib.loads(box.conf.read_bytes().decode("utf-8"))
    assert cfg["server"]["port"] == box.port and cfg["cloud"]["cp_url"] == "http://127.0.0.1:9/cp"
    assert cfg["cloud"]["client_token"] == "tok-SECRET-1" and cfg["cloud"]["relay_url"] == "ws://127.0.0.1:9/relay/ws"
    # Host: 떠 있고, 이 설치본 버전을 말하고, 런처 status 가 running
    pid = box.host_pid()
    assert box.get("/api/health") == {"status": "ok"}
    assert box.get("/api/owner/diagnostics")["version"] == VERSION
    st = box.status()
    assert st["state"] == "running" and st["pid"] == str(pid)
    # Host 는 사용자의 umask 를 그대로(S38), uv 설정·토큰 env 는 물려받지 않는다
    if Path("/proc/self").is_dir():                                           # 맥은 남의 프로세스 umask 를 읽을 길이 없다
        status_txt = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        assert "\nUmask:\t0027\n" in status_txt
    env = psutil.Process(pid).environ()
    assert not [k for k in env if k.startswith("UV_") or k.startswith("CLEWPATH_CLIENT")], sorted(env)
    assert env["CLEWPATH_CONFIG"] == str(box.conf) and env["HOME"] == str(box.home)
    # 훅: Host 가 첫 기동에 등록 — 맥·리눅스는 curl(curl.exe 아님)
    hooks = json.loads((box.claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    cmds = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert cmds and all(c.startswith("curl -s -m 3 --noproxy 127.0.0.1 ") and f":{box.port}/api/hooks/event" in c
                        for c in cmds), "루프백은 프록시를 쓰지 않는다(훅 내용이 프록시로 새지 않게)"
    # Host 가 본 경로 = hostctl paths(런처를 다시 렌더할 필요가 없었다)
    assert _wait(lambda: list(box.data.glob("launcher-ensured-*")), 15), box.diag()
    assert box.text(next(box.data.glob("launcher-ensured-*"))) == "launcher: ok (posix v2)"
    assert not (box.data / "update.inprogress").exists(), "설치 표식은 끝나면 지운다"
    # uv 는 정해진 모양으로만 불렸다: 가상 환경 밖에서 시스템 파이썬 찾기, 고른 파이썬으로 venv, uv.lock 고정 sync
    calls = box.calls()
    finds = [c for c in calls if c["args"][:2] == ["python", "find"]]
    assert finds and all("--system" in c["args"] and c["cwd"] != str(box.root) for c in finds)
    assert any(c["args"] == ["venv", "--python", sys.executable, str(box.root / ".venv")] for c in calls)
    sync = next(c for c in calls if c["args"][:1] == ["sync"])
    assert sync["args"] == ["sync", "--frozen", "--no-dev"] and sync["cwd"] == str(box.root)
    assert "by=install" in (sync["marker"] or ""), "설치하는 동안 표식을 쥔다(S24 — 감독자·러너가 끼어들지 않게)"
    assert sync["env"] == {"UV_PYTHON_PREFERENCE": "only-system", "UV_PYTHON_INSTALL_DIR": str(box.chome / "python"),
                           "UV_PROJECT_ENVIRONMENT": str(box.root / ".venv"), "UV_SYSTEM_CERTS": "1",
                           "UV_PYTHON_BIN_DIR": None, "VIRTUAL_ENV": None, "UV_PYTHON": None}


def _release_zip(payload: Path) -> bytes:
    """배포 zip 모양(Compress-Archive — 구분자 '/', 만든 OS FAT, 유닉스 권한 없음)으로 PAYLOAD 사본을 담는다."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in payload.rglob("*") if p.is_file()):
            zi = zipfile.ZipInfo(f.relative_to(payload).as_posix(), date_time=(2026, 10, 20, 0, 0, 0))
            zi.create_system, zi.external_attr, zi.compress_type = 0, 0, zipfile.ZIP_DEFLATED
            z.writestr(zi, f.read_bytes())
    return buf.getvalue()


def _manifest(ver: str, url: str, zb: bytes) -> str:
    """운영과 같은 모양의 클레임(버린 시험 키로 서명) — get.sh 는 서명을 보지 않고 내용만 읽는다(신뢰 모델)."""
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption())
    now = int(time.time())
    claims = {"iss": "clewpath-release", "aud": "clewpath-host", "ver": ver, "url": url,
              "sha256": hashlib.sha256(zb).hexdigest(), "size": len(zb), "filename": f"clewpath-host-{ver}.zip",
              "notes": "맥·리눅스 시험", "platforms": ["win", "mac", "linux"], "iat": now, "exp": now + 86400}
    return pyjwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": "0" * 16})


@pytest.mark.skipif(not (shutil.which("curl") and shutil.which("unzip")), reason="curl·unzip 없음(get.sh 길)")
def test_get_sh_one_liner_installs(box, payload):
    """curl …/get.sh | sh -s -- … — get.sh 가 매니페스트·zip(배포 모양)을 받아 검사해 설치 루트 옆 stage 에 풀고
    stage 의 install.sh --from-stage 로 넘겨, 옮기기·설치·기동까지 끝난다(3.1 → 3.2). 임시 폴더는 남지 않는다."""
    cp = _FakeCP()
    try:
        zb = _release_zip(payload)
        url = f"{cp.url}/updates/download/{VERSION}"
        cp.routes = {"/cp/get.sh": ((ROOT / "ops" / "get.sh").read_bytes(), "text/plain; charset=utf-8"),
                     "/cp/updates/manifest": (_manifest(VERSION, url, zb).encode(), "application/jwt"),
                     f"/cp/updates/download/{VERSION}": (zb, "application/zip")}
        r = subprocess.run(["/bin/sh", "-c", 'u=$1; shift; curl -fsS "$u" | sh -s -- "$@"', "sh", f"{cp.url}/get.sh",
                            *box.args(cp=cp.url)], env=box.env(CLEWPATH_CLIENT_TOKEN="tok-GET-SECRET"),
                           capture_output=True, timeout=240, cwd=str(box.home), stdin=subprocess.DEVNULL)
        out = r.stdout.decode("utf-8")
        assert r.returncode == 0 and "=== 설치 완료 ===" in out, box.diag(r)
        for s in ("=== ClewPath Host 받기 ===", f"최신 버전 : {VERSION}", "[OK]   무결성 확인 (SHA-256)", "[OK]   압축 해제",
                  "=== ClewPath Host 설치 준비", "설치 폴더에 옮김"):
            assert s in out, s
        assert "tok-GET-SECRET" not in out + r.stderr.decode("utf-8")
        assert all((box.root / n).exists() for n in updater.PAYLOAD)
        assert not list(box.chome.glob(".clewpath-get.*")) and not list(box.tmpdir.iterdir()), "임시 폴더는 다 치웠다"
        assert box.record()["installed_version"] == VERSION and box.get("/api/health") == {"status": "ok"}
        assert tomllib.loads(box.conf.read_text(encoding="utf-8"))["cloud"]["client_token"] == "tok-GET-SECRET"
        box.host_pid()
        assert cp.calls[:3] == ["/cp/get.sh", "/cp/updates/manifest", f"/cp/updates/download/{VERSION}"]
    finally:
        cp.close()


def test_token_argument_is_moved_to_env_with_one_warning(box):
    """--client-token 은 받되(Q19) 경고하고 env 로 옮긴다 — 서막 뒤 exec 한 install.sh 의 인자에는 싣지 않는다."""
    r = box.fresh("--client-token", "tok-ARGV-9")
    out = r.stdout.decode("utf-8")
    assert out.count("같은 PC 의 다른 사용자에게 보입니다(ps)") == 1
    assert "tok-ARGV-9" not in out + r.stderr.decode("utf-8")
    assert tomllib.loads(box.conf.read_text(encoding="utf-8"))["cloud"]["client_token"] == "tok-ARGV-9"
    assert "tok-ARGV-9" not in json.dumps(psutil.Process(box.host_pid()).environ())


def test_direct_run_moves_token_arguments_off_the_command_line(box):
    """sh install.sh --client-token T 로 바로 부르면(서막 없이) 경고하고 토큰을 env 로 옮겨 자기 자신을 다시 실행한다(exec —
    pid 그대로). 그대로 두면 uv·파이썬·의존성을 까는 내내(수 분) /proc/<pid>/cmdline(누구나 읽음, ps)에 토큰이 보인다(Q19·S17).
    가짜 uv 가 부를 때마다 부른 쪽 명령줄을 적는다 — 설치하는 동안 토큰이 한 번이라도 보이면 안 된다."""
    box.unpack()
    r = box.install("--client-token", "tok-DIRECT-7", "--agent-token=agt-DIRECT-8")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "=== 설치 완료 ===" in out, box.diag(r)
    assert out.count("같은 PC 의 다른 사용자에게 보입니다(ps)") == 1
    assert "DIRECT-" not in out + r.stderr.decode("utf-8")
    calls = box.calls()
    assert calls and all(c["parents"] for c in calls), "가짜 uv 가 부른 쪽 명령줄을 적었다"
    seen = json.dumps([c["parents"] for c in calls], ensure_ascii=False)
    assert "install.sh" in seen and "DIRECT-" not in seen, "설치하는 동안 토큰이 명령줄에 남았다"
    cloud = tomllib.loads(box.conf.read_text(encoding="utf-8"))["cloud"]
    assert cloud["client_token"] == "tok-DIRECT-7" and cloud["agent_token"] == "agt-DIRECT-8", "env 로 옮겨 설정에 들어갔다"
    assert "DIRECT-" not in json.dumps(psutil.Process(box.host_pid()).environ()), "Host 로는 넘기지 않는다"


def test_from_stage_refuses_odd_entries_and_foreign_targets(box):
    """get.sh 가 거른 뒤에도 한 번 더(S8): 점으로 시작하는 이름·심볼릭 링크·런처 이름은 stage 에 있으면 아무것도 옮기지 않는다.
    비어 있지 않은 남의 폴더(ClewPath 설치본이 아님)·개발 체크아웃 위에는 설치하지 않는다."""
    for odd in (".venv", "start-connector.sh", "link"):
        st = box.stage()
        if odd == "link":
            (st / "link").symlink_to(box.home)
        elif odd == ".venv":
            (st / ".venv").mkdir()
        else:
            (st / odd).write_text("#!/bin/sh\n", encoding="utf-8")
        r = box.install_from(st)
        assert r.returncode == 1 and "stage 에" in r.stdout.decode("utf-8"), (odd, box.diag(r))
        assert not box.root.exists() and not st.parent.exists(), "하나도 옮기지 않았고, get.sh 의 임시 stage 는 치웠다"
    foreign = box.base / ("foreign dir" if ON_MAC else "남의 폴더")             # 설치 루트 옆이 아닌 stage — 거절해도 지우지 않는다
    foreign.mkdir()
    (foreign / "README.md").write_text("mine", encoding="utf-8")
    st = box.stage()
    r = box.sh(st / "install.sh", "--from-stage", str(st), "--dir", str(foreign), *box.args())
    assert r.returncode == 1 and "ClewPath 설치본도 아닙니다" in r.stdout.decode("utf-8")
    assert box.text(foreign / "README.md") == "mine" and sorted(p.name for p in foreign.iterdir()) == ["README.md"]
    (foreign / ".git").mkdir()
    r = box.sh(st / "install.sh", "--from-stage", str(st), "--dir", str(foreign), *box.args())
    assert r.returncode == 1 and "개발 체크아웃" in r.stdout.decode("utf-8")


def test_provision_once_then_keep(box):
    cp = _FakeCP()
    try:
        r = box.fresh(cp=cp.url, provision=True)
        creds = box.data / "cp_credentials.json"
        assert "자격증명 발급 완료 (connector c-77)" in r.stdout.decode("utf-8"), box.diag(r)
        assert _mode(creds) == 0o600 and json.loads(creds.read_text(encoding="utf-8"))["cp_url"] == cp.url
        raw = creds.read_bytes()
        r = box.install(cp=cp.url, provision=True)
        assert r.returncode == 0 and "이미 발급된 자격증명이 있습니다" in r.stdout.decode("utf-8"), box.diag(r)
        assert creds.read_bytes() == raw and cp.calls.count("/cp/provision/anonymous") == 1, cp.calls
        assert b"SECRET-77" not in r.stdout + r.stderr and b"ROOM-77" not in r.stdout
    finally:
        cp.close()


# ---------------------------------------------------------------- 재설치·업그레이드

def test_rerun_is_idempotent_and_restarts_the_host(box):
    box.fresh()
    pid1 = box.host_pid()
    conf, launcher = box.conf.read_bytes(), box.launcher.read_bytes()
    ino = os.stat(box.vpy, follow_symlinks=False).st_ino
    loose = box.data / "old-secret.json"                    # 느슨한 권한으로 남은 파일(Host 가 다시 쓰지 않는 것) — 재설치가 좁힌다(S16)
    loose.write_text("{}", encoding="utf-8")
    os.chmod(loose, 0o644)
    r = box.install(port=box.port + 1)                      # --port 가 달라도 설정 파일(사용자 값)이 이긴다
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0, box.diag(r)
    assert "기존 venv 재사용" in out and "기존 설정 파일 유지" in out and f"설정 파일의 포트({box.port})" in out
    assert box.conf.read_bytes() == conf and box.launcher.read_bytes() == launcher
    assert os.stat(box.vpy, follow_symlinks=False).st_ino == ino
    assert _mode(loose) == 0o600
    pid2 = box.host_pid()
    assert pid2 != pid1 and box.get("/api/health") == {"status": "ok"}
    # 옛 Host 는 SIGTERM 으로 정상 종료 — 표식(shutdown.json)은 새 Host 가 기동하며 읽고 지운다. 사고 기록이 없어야 한다
    assert f"실행 중이던 Host 를 멈췄습니다(pid {pid1})" in out
    assert not (box.data / "incidents.jsonl").exists(), box.text(box.data / "incidents.jsonl")
    assert f"http://127.0.0.1:{box.port}/" in out


def test_upgrade_from_stage_replaces_payload_keeps_extras_and_clears_leftovers(box):
    box.fresh()
    pid1 = box.host_pid()
    (box.root / "my-notes.txt").write_text("keep me", encoding="utf-8")       # zip 에 없는 기존 항목은 남긴다
    # 끊긴 업데이트 잔재 — 받은 파일이 모든 교체 대상을 덮었으니 버려야 한다. 이 저널을 되돌리면(재생하면) 방금 놓은
    # README 를 SWAP/new 로 치우고 옛 README 를 제자리로 가져와, 새 설치가 옛 파일로 돌아간다
    swap = box.root / ".clewpath-swap.4242"
    (swap / "old").mkdir(parents=True)
    (swap / "new").mkdir()
    (swap / "old" / "README.md").write_text("STALE README\n", encoding="utf-8")
    (swap / "info").write_text("format=1\nrunner_pid=4242\nversion=9.9.9\nfrom=0.0.1\nstarted=1\n", encoding="utf-8")
    (swap / "journal").write_text("old README.md\nnew README.md\n", encoding="utf-8")
    st = box.stage(readme="NEW README\n")
    r = box.install_from(st)
    assert r.returncode == 0, box.diag(r)
    assert box.text(box.root / "README.md") == "NEW README\n" and box.text(box.root / "my-notes.txt") == "keep me"
    assert not swap.exists() and not st.parent.exists()
    assert "잔재를 치웠습니다" in r.stdout.decode("utf-8")
    pid2 = box.host_pid()
    assert pid2 != pid1 and not (box.data / "update.inprogress").exists()


# 설치 루트의 session_manager 를 치우는(rm) 또는 옆으로 옮기는(mv) 순간 부른 쪽(install.sh)에 SIGTERM — 한 번만.
# Ctrl-C·창 닫기(HUP)·kill 이 바로 그 틈에 온 것과 같다. 신호를 보낸 뒤 진짜 명령을 그대로 한다.
FAKE_INTERRUPT = """#!/bin/sh
case "${0##*/}" in rm) real=/bin/rm ;; *) real=/bin/mv ;; esac
for a in "$@"; do
  if [ "$a" = "$FAKE_TARGET" ] && [ ! -e "$FAKE_FIRED" ]; then
    printf 'fired by %s\\n' "${0##*/}" >"$FAKE_FIRED"
    kill -TERM "$PPID"
    sleep 1
  fi
done
exec "$real" "$@"
"""


def test_signal_while_placing_leaves_a_whole_install_root(box):
    """받은 파일을 설치 루트로 옮기는 도중 끊기면(신호) 설치 폴더에서 session_manager 가 빠진 채 남았다 — 항목마다 rm -rf 뒤
    mv 였고, 끝날 때 on_exit 가 아직 안 옮긴 새 항목이 든 임시 stage 까지 지웠다. 그러면 install.sh·uninstall.sh 둘 다 실행을
    거부하고 안내도 없다. 옮기는 동안은 신호를 미뤘다가 다 옮긴 뒤 끝내고, 항목이 비는 틈은 rename 두 번 사이뿐이다."""
    box.fresh()
    tools = box.base / "끊는 도구"
    tools.mkdir()
    for name in ("rm", "mv"):
        (tools / name).write_text(FAKE_INTERRUPT, encoding="utf-8")
        os.chmod(tools / name, 0o755)
    fired = box.base / "fired"
    st = box.stage(readme="NEW README\n")
    r = box.install_from(st, env=box.env(PATH=f"{tools}:{os.environ['PATH']}", FAKE_FIRED=fired,
                                         FAKE_TARGET=box.root / "session_manager"))
    out = r.stdout.decode("utf-8")
    assert fired.exists() and r.returncode == 1, (fired.exists(), box.diag(r))
    assert (box.root / "session_manager" / "__init__.py").is_file(), "설치 폴더가 통째로 남는다(" + out[-600:] + ")"
    assert all((box.root / n).exists() for n in updater.PAYLOAD) and box.text(box.root / "README.md") == "NEW README\n"
    assert not list(box.root.glob(".clewpath-old.*")) and not list(box.chome.glob(".clewpath-get.*"))
    assert "같은 명령을 다시" in out
    assert not (box.data / "update.inprogress").exists()
    r = box.install()                                         # 안내대로 다시 — 이어서 끝난다
    assert r.returncode == 0 and "=== 설치 완료 ===" in r.stdout.decode("utf-8"), box.diag(r)
    box.host_pid()


def test_placing_clears_old_copies_left_by_an_earlier_crash(box):
    """옛 항목은 설치 루트 안 .clewpath-old.<pid> 로 옮겼다가 다 놓은 뒤 지운다 — 그 사이 전원이 꺼져 남은 것은 다음
    설치가 치운다(받은 파일이 모든 항목을 새로 덮으므로 옛 사본은 필요 없다)."""
    box.fresh()
    stale = box.root / ".clewpath-old.4242"
    (stale / "session_manager").mkdir(parents=True)
    (stale / "session_manager" / "old.py").write_text("old", encoding="utf-8")
    r = box.install_from(box.stage())
    assert r.returncode == 0, box.diag(r)
    assert not list(box.root.glob(".clewpath-old.*"))


def test_reinstall_rebuilds_the_venv_after_stopping_the_host(box):
    box.fresh()
    pid1 = box.host_pid()
    sentinel = box.root / ".venv" / "old-venv-sentinel"     # inode 번호는 지운 뒤 곧바로 다시 쓰일 수 있어 표지로 본다
    sentinel.write_text("x", encoding="utf-8")
    r = box.install("--reinstall")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "기존 .venv 삭제 (--reinstall)" in out, box.diag(r)
    assert not sentinel.exists() and box.vpy.exists()
    stopped = out.index(f"실행 중이던 Host 를 멈췄습니다(pid {pid1})")
    assert stopped < out.index("기존 .venv 삭제"), "돌고 있는 Host 의 파이썬을 지우기 전에 정상 종료"
    assert not (box.data / "incidents.jsonl").exists(), box.text(box.data / "incidents.jsonl")
    assert box.host_pid() != pid1


def test_swap_leftovers_are_rolled_back_by_the_runner(box):
    """`sh install.sh` 로 복구하세요 — 런처가 보내는 안내의 끝. 설치 루트의 러너로 -RecoverOnly 한 뒤 설치를 잇는다."""
    box.fresh()
    swap, orig = _half_applied_swap(box)
    r = box.install()
    assert r.returncode == 0, box.diag(r)
    assert "중단된 업데이트를 되돌렸습니다" in r.stdout.decode("utf-8")
    assert box.text(box.root / "README.md") == orig and not swap.exists()
    assert "-RecoverOnly" in box.text(box.data / "updates" / "recover.log")
    last = json.loads(box.text(box.data / "updates" / "last_apply.json"))
    assert last["version"] == "9.9.9" and last["ok"] is False, "되돌린 버전은 실패로 — 같은 버전을 곧바로 다시 받지 않게"
    box.host_pid()


def _half_applied_swap(box) -> tuple[Path, str]:
    """러너가 old·new 를 다 옮긴 뒤(헬스 전) 죽은 모양 — README 만."""
    orig = box.text(box.root / "README.md")
    swap = box.root / ".clewpath-swap.777"
    (swap / "old").mkdir(parents=True)
    (swap / "new").mkdir()
    shutil.move(str(box.root / "README.md"), str(swap / "old" / "README.md"))
    (box.root / "README.md").write_text("HALF-APPLIED\n", encoding="utf-8")
    (swap / "info").write_text("format=1\nrunner_pid=777\nversion=9.9.9\nfrom=0.0.1\nstarted=1\n", encoding="utf-8")
    (swap / "journal").write_text("old README.md\nnew README.md\n", encoding="utf-8")
    return swap, orig


def test_recovery_works_when_the_marker_is_already_ours(box):
    """서막이 표식을 잡고 exec 로 이어 온 install.sh 와 같은 모양(같은 pid 의 by=install 표식) — 러너가 그 표식을 '설치
    중'으로 보고 비켜서면(종료코드 3) 아무도 되돌리지 못한다. 자기 표식은 내려놓고 부른다."""
    box.fresh()
    swap, orig = _half_applied_swap(box)
    marker = box.data / "update.inprogress"
    wrapper = ('printf "pid=%s\\nat=%s\\nby=install\\n" "$$" "$(date +%s)" >"$MARKER"; '
               'exec /bin/sh "$@"')
    t0 = time.monotonic()
    r = subprocess.run(["/bin/sh", "-c", wrapper, "sh", str(box.root / "install.sh"), *box.args()],
                       env=box.env(MARKER=marker), capture_output=True, timeout=240, cwd=str(box.home),
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and time.monotonic() - t0 < 40, box.diag(r)
    assert "중단된 업데이트를 되돌렸습니다" in r.stdout.decode("utf-8")
    assert box.text(box.root / "README.md") == orig and not swap.exists() and not marker.exists()
    box.host_pid()


def test_prelude_deletes_only_get_sh_temp_next_to_the_root(box):
    """서막이 멈추면 get.sh 의 임시 stage 를 치우지만, 그 모양(설치 루트 옆 .clewpath-get.*)이 아닌 stage 의 부모는
    절대 지우지 않는다 — 손으로 푼 폴더, 이름만 닮은 다른 곳의 폴더."""
    box.fresh()
    runner = box.holder("update_runner.sh")
    (box.data / "update.inprogress").write_text(f"pid={runner.pid}\nat={int(time.time())}\nby=runner\n", encoding="utf-8")
    for parent in (box.base / "내가 푼 곳", box.base / ".clewpath-get.lookalike"):
        shutil.copytree(box.payload, parent / "stage")
        (parent / "keep.txt").write_text("keep", encoding="utf-8")
        r = box.sh(parent / "stage" / "install.sh", "--from-stage", str(parent / "stage"), "--dir", str(box.root),
                   *box.args())
        assert r.returncode == 1 and "진행 중" in r.stdout.decode("utf-8"), box.diag(r)
        assert (parent / "keep.txt").exists() and (parent / "stage" / "session_manager").is_dir(), parent


def test_from_stage_stops_when_leftovers_cannot_be_removed(box):
    box.fresh()
    swap = box.root / ".clewpath-swap.4242"
    (swap / "old" / "locked").mkdir(parents=True)
    (swap / "old" / "locked" / "f").write_text("x", encoding="utf-8")
    os.chmod(swap / "old" / "locked", 0o500)                 # 안의 파일을 지울 수 없다
    try:
        r = box.install_from(box.stage(readme="NEW\n"))
        out = r.stdout.decode("utf-8")
        assert r.returncode == 1 and "잔재를 지우지 못했습니다" in out, box.diag(r)
        assert "되돌리지 못했습니다" not in out, "러너에 넘기지 않고 그 자리에서 멈춘다"
    finally:
        os.chmod(swap / "old" / "locked", 0o700)


# ---------------------------------------------------------------- 거부·실패

def test_refuses_while_an_update_runs(box):
    box.fresh()
    pid = box.host_pid()
    readme = box.text(box.root / "README.md")
    runner = box.holder("update_runner.sh")
    marker = box.data / "update.inprogress"
    marker.write_text(f"pid={runner.pid}\nat={int(time.time())}\nby=runner\n", encoding="utf-8")
    st = box.stage(readme="NOPE\n")
    for r in (box.install(), box.install_from(st)):
        assert r.returncode == 1 and "진행 중" in r.stdout.decode("utf-8"), box.diag(r)
        assert box.host_pids() == [pid] and box.text(box.root / "README.md") == readme, "아무것도 바꾸지 않는다"
        assert f"pid={runner.pid}" in box.text(marker), "남의 표식은 그대로"
    assert not st.parent.exists(), "get.sh 가 넘긴 임시 stage 는 서막이 멈춰도 치운다(같은 명령이 다시 받는다)"
    marker.unlink()
    lock = box.data / "updates" / "runner.lock.d"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{runner.pid}\n", encoding="utf-8")
    r = box.install()
    assert r.returncode == 1 and "러너" in r.stdout.decode("utf-8") and box.host_pids() == [pid]


@pytest.mark.parametrize("argv,rc,said", [
    (["--bogus"], 2, "모르는 인자: --bogus"),
    (["--port", "0"], 2, "--port"),
    (["--port", "05100"], 2, "--port"),
    (["--cp-url", "http://cp.example/cp"], 2, "https://"),
    (["--relay-url", "ws://r.example/ws"], 2, "wss://"),
    (["--dir", "/somewhere"], 2, "--from-stage"),
    (["--python", "3"], 2, "--python"),
    (["leaked-SECRET-value"], 2, "값은 보이지 않게"),
])
def test_usage_errors(box, argv, rc, said):
    box.unpack()
    r = box.sh(box.root / "install.sh", *argv)
    out = r.stdout.decode("utf-8")
    assert r.returncode == rc and said in out, out
    assert "SECRET" not in out and not box.data.exists() and not (box.root / ".venv").exists()


def test_refuses_root_and_dry_run_with_stage(box):
    tools = box.base / "tools"
    tools.mkdir()
    (tools / "id").write_text("#!/bin/sh\necho 0\n", encoding="utf-8")       # root 흉내
    os.chmod(tools / "id", 0o755)
    box.unpack()
    r = box.sh(box.root / "install.sh", *box.args(), env=box.env(PATH=f"{tools}:{os.environ['PATH']}"))
    assert r.returncode == 1 and "root" in r.stdout.decode("utf-8") and not box.data.exists()
    st = box.stage()
    r = box.sh(st / "install.sh", "--from-stage", str(st), "--dir", str(box.base / "elsewhere"), "--dry-run")
    assert r.returncode == 2 and (st / "session_manager").is_dir(), "dry-run 은 옮기지 않는다"


def test_dry_run_changes_nothing(box):
    box.unpack()
    before = _tree_hash(box.home)
    r = box.sh(box.root / "install.sh", *box.args(), "--dry-run")
    assert r.returncode == 0, box.diag(r)
    out = r.stdout.decode("utf-8")
    assert "DryRun — 점검만" in out and "DryRun: 기동 건너뜀" in out
    assert _tree_hash(box.home) == before and not list(box.tmpdir.iterdir())
    assert [c["args"] for c in box.calls()] == [["--version"]], "uv 버전만 본다"


def test_missing_claude_folder_fails_without_creating_it(box):
    shutil.rmtree(box.claude)
    box.unpack()
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "[3/6]" in out and "클로드 데이터 폴더를 찾지 못했습니다" in out, box.diag(r)
    assert not box.claude.exists(), "클로드 폴더를 우리가 만들지 않는다"
    assert not (box.root / ".venv").exists()
    box.claude.mkdir()                                       # 폴더는 있지만 projects 가 없다 — 데이터 폴더도 만들지 않는다
    r = box.install()
    assert r.returncode == 1 and list(box.claude.iterdir()) == []


def test_dependency_failure_reports_and_keeps_the_host_down(box):
    box.uv_conf(fail=["sync", "pip"])
    st = box.stage()
    r = box.install_from(st)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1, box.diag(r)
    assert "잠금 파일로 설치 실패" in out and "의존성 설치 실패" in out and "fake sync" in out and "fake pip" in out
    assert "--reinstall" in out and "PyPI" in out
    assert box.host_pids() == [] and not (box.data / "update.inprogress").exists()
    assert not st.parent.exists() and (box.root / "install.sh").is_file(), "옮기기는 끝났다 — 같은 명령으로 다시"
    box.uv_conf()                                            # 원인을 해결한 뒤 같은 명령 — 우리가 만든 .venv 를 잇는다(기록 전이어도)
    r = box.install_from(box.stage())
    assert r.returncode == 0 and "기존 venv 재사용" in r.stdout.decode("utf-8"), box.diag(r)


def test_managed_python_when_there_is_no_system_python(box):
    box.uv_conf(no_system=True)
    r = box.fresh()
    managed = box.chome / "python" / "cpython-3.11.99-fake" / "bin" / "python3"
    assert "uv 관리 파이썬 사용" in r.stdout.decode("utf-8")
    inst = [c for c in box.calls() if c["args"][:2] == ["python", "install"] and "--help" not in c["args"]]
    assert [c["args"] for c in inst] == [["python", "install", "3.11", "--no-bin"]]
    assert inst[0]["env"]["UV_PYTHON_INSTALL_DIR"] == str(box.chome / "python")
    rec = box.record()
    assert rec["python_pref"] == "only-managed" and rec["python"] == str(managed)
    assert any(c["args"] == ["venv", "--python", str(managed), str(box.root / ".venv")] for c in box.calls())
    box.host_pid()


def test_uv_archive_must_match_the_pinned_hash(box):
    box.uv.unlink()
    arc = box.base / "uv-fake.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        tf.add(str(box.ctl), arcname="uv-x86_64-unknown-linux-gnu")          # 아무 내용 — 해시가 고정값과 다르다
    box.unpack()
    r = box.install(env=box.env(CLEWPATH_UV_ARCHIVE=arc))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "uv 파일 해시가 다릅니다" in out and "uv_checksum_mismatch" in out, box.diag(r)
    assert not box.uv.exists()
    r = box.install("--uv-version", "9.9.9", env=box.env(CLEWPATH_UV_ARCHIVE=arc))
    assert r.returncode == 1 and "해시가 없어 로컬 파일을 믿을 수 없습니다" in r.stdout.decode("utf-8")
    assert not box.uv.exists()


def test_system_uv_is_used_with_a_warning(box):
    tools = box.base / "tools"
    tools.mkdir()
    shutil.move(str(box.uv), str(tools / "uv"))
    box.unpack()
    r = box.install("--system-uv", env=box.env(PATH=f"{tools}:{os.environ['PATH']}"))
    assert r.returncode == 0 and "--system-uv — 버전·해시를 검증하지 않습니다" in r.stdout.decode("utf-8"), box.diag(r)
    assert box.record()["uv"] == str(tools / "uv") and not box.uv.exists()
    box.host_pid()


@pytest.mark.skipif(not shutil.which("bash"), reason="bash 없음")
def test_install_and_uninstall_under_bash_posix(box):
    """맥의 /bin/sh 는 bash 3.2 의 sh 모드다 — 근사로 bash --posix. stage 없는 설치는 처음부터 끝까지 이 셸 하나에서 돌고,
    제거는 --root 로 불러 임시 사본 없이 이 셸에서 돈다."""
    box.unpack()
    r = box.sh(box.root / "install.sh", *box.args(), shell=("bash", "--posix"))
    assert r.returncode == 0 and "=== 설치 완료 ===" in r.stdout.decode("utf-8"), box.diag(r)
    pid = box.host_pid()
    assert box.status()["state"] == "running" and box.record()["python_pref"] == "only-system"
    r = box.sh(box.root / "uninstall.sh", "--root", str(box.root), "--yes", shell=("bash", "--posix"))
    assert r.returncode == 0 and "=== 제거 완료 ===" in r.stdout.decode("utf-8"), box.diag(r)
    assert not box.root.exists() and _wait(lambda: _gone(pid), 10) and box.data.is_dir()


def test_maintenance_flag_skips_the_start(box):
    box.fresh()
    r = box.sh(box.launcher, "stop", "--hold")
    assert r.returncode == 0 and box.host_pids() == []
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "maintenance.flag 가 있어 기동하지 않습니다" in out, box.diag(r)
    assert box.host_pids() == [] and (box.data / "maintenance.flag").exists()


# ---------------------------------------------------------------- 2회차 검토(설치) — 그룹 신호·설치 폴더·데이터 폴더·uv·기동 확인

# 터미널의 Ctrl+C 처럼 프로세스 그룹 전체에 SIGINT — 설치 루트의 session_manager 를 옆으로 옮기려는 순간 한 번. 신호를 무시하지
# 않았으면 이 가짜 mv 자신도 그 자리에서 죽는다(진짜 mv 와 같다)
FAKE_GROUP_INT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "$FAKE_TARGET" ] && [ ! -e "$FAKE_FIRED" ]; then
    printf 'fired\\n' >"$FAKE_FIRED"
    kill -INT 0
    sleep 1
  fi
done
exec /bin/mv "$@"
"""


def test_terminal_interrupt_to_the_whole_group_while_placing(box):
    """터미널 신호(Ctrl+C·HUP)는 프로세스 그룹 전체로 간다 — 셸만 신호를 미루면 옮기던 mv 자식이 죽어 새것·옛것이 섞인 설치와
    .clewpath-old.<pid> 가 남았다(그 상태로 감시가 Host 를 띄울 수 있다). mv 는 신호를 무시한 채 돌고, 다 옮긴 뒤 끝낸다."""
    box.fresh()
    tools = box.base / "그룹 신호"
    tools.mkdir()
    (tools / "mv").write_text(FAKE_GROUP_INT, encoding="utf-8")
    os.chmod(tools / "mv", 0o755)
    fired = box.base / "fired"
    st = box.stage(readme="NEW README\n")
    (st / "session_manager" / "_stage_mark.py").write_text("NEW = 1\n", encoding="utf-8")
    with open(st / "uv.lock", "a", encoding="utf-8") as f:                 # session_manager 뒤에 옮기는 항목
        f.write("# new\n")
    env = box.env(PATH=f"{tools}:{os.environ['PATH']}", FAKE_FIRED=fired, FAKE_TARGET=box.root / "session_manager")
    r = box.sh(st / "install.sh", "--from-stage", str(st), "--dir", str(box.root), *box.args(), env=env,
               start_new_session=True)                                      # 새 프로세스 그룹 — 시험 프로세스는 받지 않는다
    out = r.stdout.decode("utf-8")
    assert fired.exists() and r.returncode == 1, (fired.exists(), box.diag(r))
    assert (box.root / "session_manager" / "_stage_mark.py").is_file(), "session_manager 는 새것(" + out[-600:] + ")"
    assert box.text(box.root / "README.md") == "NEW README\n" and box.text(box.root / "uv.lock").endswith("# new\n")
    assert all((box.root / n).exists() for n in updater.PAYLOAD)
    assert not list(box.root.glob(".clewpath-old.*")) and not list(box.chome.glob(".clewpath-get.*"))
    assert "받은 파일은 다 옮겼습니다" in out and not (box.data / "update.inprogress").exists()
    r = box.install()
    assert r.returncode == 0 and "=== 설치 완료 ===" in r.stdout.decode("utf-8"), box.diag(r)
    box.host_pid()


def test_direct_run_refuses_home_and_folders_above_the_claude_folder(box):
    """배포 zip 을 홈에 그대로 풀고 sh install.sh — 홈을 설치 루트로 받아 사용자의 ~/.venv 를 '기존 venv'로 재사용해
    uv sync(정확 동기화 — 그 안의 패키지를 지운다)를 걸었다. 홈(이나 그 위)·클로드 폴더(나 그 위)는 설치 루트가 아니다."""
    shutil.copytree(box.payload, box.home, dirs_exist_ok=True)
    venv = box.home / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    (venv / "my_personal_pkg.py").write_text("mine", encoding="utf-8")
    r = box.sh(box.home / "install.sh", *box.args())
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "홈 폴더" in out, box.diag(r)
    assert box.text(venv / "my_personal_pkg.py") == "mine" and not box.calls() and not box.data.exists()
    box.unpack()
    inner = box.root / "claude"                                              # 클로드 폴더(CLAUDE_CONFIG_DIR)가 설치 루트 안
    (inner / "projects" / "p").mkdir(parents=True)
    r = box.install(env=box.env(CLAUDE_CONFIG_DIR=inner))
    assert r.returncode == 1 and "클로드 폴더" in r.stdout.decode("utf-8"), box.diag(r)
    assert not (inner / "session_manager").exists() and not (box.root / ".venv").exists() and not box.calls()


def test_first_install_needs_a_folder_of_its_own(box):
    """설치 기록이 없는 첫 설치는 ClewPath 항목만 있는 폴더에서만 — ~/Downloads 처럼 남의 파일이 섞인 곳에 풀어 설치하면 제거가
    그 폴더를 통째로 지웠다. 기록이 있는 재설치에서 모르는 항목은 경고만 하고 그대로 둔다. 맥 Finder 의 .DS_Store 는 셈하지 않는다."""
    box.unpack()
    (box.root / "내 사진.jpg").write_text("photo", encoding="utf-8")
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "내 사진.jpg" in out and "빈 새 폴더에 zip 을 풀어" in out, box.diag(r)
    assert not box.data.exists() and not (box.root / ".venv").exists() and not box.calls()
    (box.root / "내 사진.jpg").unlink()
    (box.root / ".DS_Store").write_text("", encoding="utf-8")
    (box.root / ".clewpath-install.4242.tmp").write_text("home=x\n", encoding="utf-8")   # 기록을 쓰다 끊긴 일시 파일 — 우리 것
    r = box.install()
    assert r.returncode == 0, box.diag(r)
    (box.root / "my-notes.txt").write_text("keep", encoding="utf-8")
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "[WARN]" in out and "my-notes.txt" in out, box.diag(r)
    assert box.text(box.root / "my-notes.txt") == "keep"


def test_from_stage_refuses_a_lookalike_folder_with_foreign_files(box):
    """--dir 에 session_manager·pyproject(clewpath-host)가 있어 설치본처럼 보여도 설치 기록이 없고 남의 파일이 섞였으면 옮기기
    전에 거부한다 — 옮긴 뒤 거부하면 남의 폴더에 받은 파일이 섞인 채 남는다."""
    box.unpack()
    (box.root / "photo.jpg").write_text("photo", encoding="utf-8")
    r = box.install_from(box.stage(readme="NEW\n"))
    assert r.returncode == 1 and "빈 새 폴더" in r.stdout.decode("utf-8"), box.diag(r)
    assert box.text(box.root / "README.md") != "NEW\n" and box.text(box.root / "photo.jpg") == "photo"
    assert not (box.root / ".venv").exists() and not box.data.exists()
    assert not list(box.chome.glob(".clewpath-get.*")), "get.sh 의 임시 stage 는 치운다"


def test_prelude_refusals_clear_the_get_sh_stage(box):
    """서막이 get.sh 의 임시 폴더(설치 루트 옆 .clewpath-get.*)를 알아 두기 전에 거절하면 stage 가 남았다(다시 칠 때마다 쌓인다)
    — stage 가 그 모양이면 인자 해석 바로 뒤에 알아 두어, 서막의 어떤 거절에도 치운다."""
    tools = box.base / "tools"
    tools.mkdir()
    (tools / "id").write_text("#!/bin/sh\necho 0\n", encoding="utf-8")       # root 흉내
    os.chmod(tools / "id", 0o755)
    for env in (box.env(CLAUDE_CONFIG_DIR="rel/claude"), box.env(PATH=f"{tools}:{os.environ['PATH']}")):
        st = box.stage()
        r = box.install_from(st, env=env)
        assert r.returncode == 1 and ("절대 경로" in r.stdout.decode("utf-8") or "root" in r.stdout.decode("utf-8")), \
            box.diag(r)
        assert not st.parent.exists() and not box.root.exists()


def test_existing_venv_is_reused_only_when_it_is_ours(box):
    """설치 기록이 없는 폴더의 .venv 는 남의 것일 수 있다 — 재사용해 uv sync(정확 동기화)를 걸면 그 안의 패키지를 지운다.
    멈춰서 안내하고, --reinstall 이면(지워도 된다는 뜻) 지우고 새로 만든다."""
    box.unpack()
    venv = box.root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    (venv / "someone.txt").write_text("x", encoding="utf-8")
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "--reinstall" in out and str(venv) in out, box.diag(r)
    assert (venv / "someone.txt").exists() and not box.calls()
    r = box.install("--reinstall")
    assert r.returncode == 0 and "기존 .venv 삭제 (--reinstall)" in r.stdout.decode("utf-8"), box.diag(r)
    assert not (venv / "someone.txt").exists()


def test_reinstall_keeps_the_data_folder_the_launcher_uses(box):
    """첫 설치는 CLAUDE_CONFIG_DIR 가 있는 셸, 재설치는 없는 셸 — 설정 파일로 고정된 데이터 폴더를 버리고 홈의 새 폴더로
    갈아타, 페어링·자격증명이 사라진 것처럼 보였다. 기존 런처가 쓰는 설정 파일을 넘기고, 그래도 다르면 갈아타지 않고 멈춘다."""
    moved = box.base / "옮긴 claude"
    (moved / "projects" / "p").mkdir(parents=True)
    box.fresh(env=box.env(CLAUDE_CONFIG_DIR=moved))
    data = moved / "session_manager"
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0, box.diag(r)
    assert "셸 추정" not in out and not box.data.exists(), "홈의 데이터 폴더로 갈아타지 않는다"
    assert _kv(box.sh(box.launcher, "paths").stdout.decode("utf-8"))["data_dir"] == str(data)
    assert box.record()["data_dir"] == str(data) and box.record()["conf_file"] == str(data / "clewpath.toml")
    pid = box.host_pid()
    other = box.base / "다른 claude"                                         # 다른 클로드 폴더를 env 로 — 갈아타지 않고 멈춘다
    (other / "projects" / "p").mkdir(parents=True)
    r = box.install(env=box.env(CLAUDE_CONFIG_DIR=other))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and str(data) in out and "갈아타지 않" in out, box.diag(r)
    assert not (other / "session_manager").exists() and box.record()["data_dir"] == str(data)
    assert box.host_pids() == [pid]


def test_same_data_folder_spelled_differently_is_not_a_switch(box):
    """셸 추정과 hostctl paths 가 겹친 / ·끝 / 만 달라도 '다르다'고 경고하고 표식을 옮겼다 — 정규화해서 비교한다."""
    moved = box.base / "moved claude"
    (moved / "projects" / "p").mkdir(parents=True)
    r = box.fresh(env=box.env(CLAUDE_CONFIG_DIR=str(box.base) + "//moved claude/"))
    assert "셸 추정" not in r.stdout.decode("utf-8"), box.diag(r)
    assert box.record()["data_dir"] == str(moved / "session_manager")


def _sh_function(name: str) -> str:
    src = (ROOT / "install.sh").read_text(encoding="utf-8")
    i = src.index(f"\n{name}() {{") + 1
    return src[i:src.index("\n}\n", i) + 3]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="리눅스의 libc 판정 — 맥에는 getconf GNU_LIBC_VERSION·ldd 가 없어 "
                                                              "glibc 증거가 없다(install.sh 는 맥에서 이 판정을 부르지 않는다)")
def test_glibc_with_a_stray_musl_loader_is_not_musl(tmp_path):
    """glibc 배포판에도 musl 패키지가 /lib/ld-musl-* 를 둘 수 있다 — 그것만 보고 musl 로 보아 설치를 거부했다. glibc 를 먼저 본다."""
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "ld-musl-x86_64.so.1").write_text("", encoding="utf-8")
    fn = _sh_function("is_musl").replace("/lib/ld-musl-", f"{lib}/ld-musl-")
    probe = fn + 'if is_musl; then echo musl; else echo glibc; fi\n'

    def verdict(path: str) -> str:
        r = subprocess.run(["/bin/sh", "-c", probe], env={**os.environ, "PATH": path}, capture_output=True, timeout=30)
        return r.stdout.decode().strip()
    assert verdict(os.environ["PATH"]) == "glibc"                            # 이 컨테이너는 glibc(getconf·ldd 가 말한다)
    musl = tmp_path / "musl tools"                                           # musl: getconf 는 모르고 ldd 는 musl 이라 한다
    musl.mkdir()
    (musl / "getconf").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (musl / "ldd").write_text("#!/bin/sh\necho 'musl libc (x86_64)' >&2\nexit 1\n", encoding="utf-8")
    for f in musl.iterdir():
        os.chmod(f, 0o755)
    assert verdict(f"{musl}:{os.environ['PATH']}") == "musl"
    (musl / "ldd").write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")   # 도구가 아무 말도 없으면 로더로 판단
    assert verdict(f"{musl}:{os.environ['PATH']}") == "musl"


@pytest.mark.parametrize("value,said", [("rel/clewpath", "절대 경로"), ("@HOME@/", "홈")])
def test_clewpath_home_must_be_absolute_and_not_home(box, value, said):
    """상대 경로를 주면 지금 폴더에 uv 를 깔려다 엉뚱하게 실패했고, 홈을 주면 사용자의 ~/bin/uv 를 덮어썼다."""
    box.unpack()
    r = box.install(env=box.env(CLEWPATH_HOME=value.replace("@HOME@", str(box.home))))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "CLEWPATH_HOME" in out and said in out, box.diag(r)
    assert not (box.home / "rel").exists() and not (box.home / "bin").exists() and not box.calls()
    assert not box.data.exists()


def test_uv_in_a_shared_folder_is_not_overwritten(box):
    """CLEWPATH_HOME=~/.local 처럼 남과 같이 쓰는 폴더를 주면 사용자의 ~/.local/bin/uv 를 고정 버전으로 덮어썼다 — 그 자리 uv 는
    우리가 둔 것(설치 기록의 uv=)이거나 ClewPath 전용 기본 위치일 때만 바꾼다."""
    mine = box.home / ".local" / "bin" / "uv"
    mine.parent.mkdir(parents=True, exist_ok=True)
    body = "#!/bin/sh\necho 'uv 0.9.0'\n"
    mine.write_text(body, encoding="utf-8")
    os.chmod(mine, 0o755)
    box.unpack()
    r = box.install(env=box.env(CLEWPATH_HOME=box.home / ".local"))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "덮어쓰지 않습니다" in out and "내려받는 중" not in out, box.diag(r)
    assert box.text(mine) == body
    box.uv_conf(version="0.11.0")                                            # 기본 위치의 옛 버전 — 다시 받는 길로 간다
    arc = box.base / "uv-fake.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        tf.add(str(box.ctl), arcname="uv-x86_64-unknown-linux-gnu")
    r = box.install(env=box.env(CLEWPATH_UV_ARCHIVE=arc))
    assert r.returncode == 1 and "uv 파일 해시가 다릅니다" in r.stdout.decode("utf-8"), box.diag(r)


def test_fallback_install_keeps_the_lock_failure_log(box):
    """uv sync --frozen 이 실패해 잠금 밖(uv pip install -e)으로 설치하면, 성공해도 실패 원인이 임시 폴더째 사라졌다(S11) —
    꼬리를 보여 주고 데이터 폴더에 남긴다."""
    box.uv_conf(fail=["sync"])
    r = box.fresh()
    out = r.stdout.decode("utf-8")
    assert "잠금 파일로 설치 실패" in out and "fake sync" in out
    logs = list(box.data.glob("install-sync-*.log")) + list(box.data.glob("updates/install-sync-*.log"))
    assert len(logs) == 1 and "Failed to fetch (fake sync)" in box.text(logs[0]) and str(logs[0]) in out


def test_system_uv_needs_the_pinned_minor_version(box):
    tools = box.base / "tools"
    tools.mkdir()
    shutil.move(str(box.uv), str(tools / "uv"))
    box.unpack()
    box.uv_conf(version="0.11.9")
    r = box.install("--system-uv", env=box.env(PATH=f"{tools}:{os.environ['PATH']}"))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "0.11.9" in out and "0.12" in out, box.diag(r)
    assert not (box.root / ".venv").exists()
    box.uv_conf(version="0.13.0")                                            # 같은 부 버전 이상이면 쓴다
    r = box.install("--system-uv", env=box.env(PATH=f"{tools}:{os.environ['PATH']}"))
    assert r.returncode == 0, box.diag(r)


def test_mac_refuses_non_ascii_install_paths(box):
    """맥 ps 는 명령줄의 ASCII 밖 바이트를 이스케이프해 보여 준다 — 설치 경로가 그러면 런처·러너·설치가 Host 를 알아보지
    못한다. 맥에서는 그런 경로를 거부하고 이유를 알린다(리눅스는 그대로 — 리눅스 시험장의 홈은 한글이다).
    상자의 uname 을 Darwin 으로 바꾸고(진짜 맥에서는 상자가 리눅스를 흉내 내는 uname 을 덮는다), 홈과 상관없이 설치 폴더
    이름에 한글을 넣는다(진짜 맥에서 도는 상자의 홈은 ASCII)."""
    (box.tools / "uname").write_text('#!/bin/sh\ncase "${1:-}" in -m) echo arm64 ;; *) echo Darwin ;; esac\n',
                                     encoding="utf-8")
    os.chmod(box.tools / "uname", 0o755)
    root = box.chome / "앱"
    shutil.copytree(box.payload, root)
    r = box.sh(root / "install.sh", *box.args())
    out = r.stdout.decode("utf-8")
    assert r.returncode == 1 and "ASCII" in out, box.diag(r)
    assert not box.data.exists() and not box.calls() and not (box.home / "Library").exists()


def test_reinstall_clears_a_flag_left_by_an_interrupted_uninstall(box):
    """제거가 중간에 끊겨 maintenance.flag(by=uninstall)가 남으면 다시 설치해도 Host 가 뜨지 않았다 — install.sh 를 다시
    돌리는 것은 다시 켜겠다는 뜻이므로 그 표식만 지운다(stop --hold 의 by=stop 은 그대로 — 위 시험)."""
    box.fresh()
    r = box.sh(box.launcher, "stop")
    assert r.returncode == 0 and box.host_pids() == []
    (box.data / "maintenance.flag").write_text(f"by=uninstall\nat={int(time.time())}\n", encoding="utf-8")
    r = box.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "by=uninstall" in out, box.diag(r)
    assert not (box.data / "maintenance.flag").exists()
    box.host_pid()


# ---------------------------------------------------------------- [mac] 자동 시작(launchd) — uname·launchctl 흉내

def _umask() -> int:
    m = os.umask(0o022)
    os.umask(m)
    return m


def test_mac_install_registers_launchd_after_the_launcher(mac):
    """[mac] 런처를 렌더한 뒤 ~/Library/LaunchAgents/com.clewpath.host.plist(0644, plistlib)를 쓰고 옛 것을 내린 뒤(bootout)
    올린다(bootstrap gui/<uid>). 올리자마자 도는 foreground(RunAtLoad)는 설치 표식 때문에 아무것도 띄우지 않고 0 — Host 는
    지금처럼 런처 start 가 띄운 하나. 기록에 autostart=launchd. 다시 설치하면 같은 내용은 그대로 두고 다시 내렸다 올린다."""
    r = mac.fresh(env=mac.env(FAKE_LAUNCHD_RUN=1))
    out = r.stdout.decode("utf-8")
    assert "[OK]   자동 시작 등록 (launchd com.clewpath.host" in out, mac.diag(r)
    assert "자동 시작은 아직 없습니다" not in out and "리눅스는 시험용" not in out
    assert _mode(mac.plist) == 0o644 and _mode(mac.plist.parent) == 0o755
    d = plistlib.loads(mac.plist.read_bytes())
    launcher = mac.root / _posix.START_SCRIPT
    assert d["ProgramArguments"] == ["/bin/sh", "-c", hostctl.LAUNCHD_WRAPPER, "clewpath-launchd", str(launcher)]
    assert d["StandardOutPath"] == d["StandardErrorPath"] == str(mac.data / "launchd.log")
    assert d["EnvironmentVariables"] == {"PATH": hostctl.launchd_path(mac.env()["PATH"]), "LANG": "C.UTF-8"}
    assert d["Umask"] == _umask() and d["RunAtLoad"] is True and d["StartInterval"] == 300
    uid = str(os.getuid())
    assert [c[1:] for c in mac.launchctl()] == [["bootout", f"gui/{uid}/com.clewpath.host"],
                                                ["bootstrap", f"gui/{uid}", str(mac.plist)]], mac.launchctl()
    assert mac.text(mac.launchd_rc) == "0\n", "RunAtLoad 의 foreground 는 곧 0 으로 끝났다"
    assert "foreground: 업데이트 진행 중 — 띄우지 않음" in mac.text(mac.data / "launcher.log")
    pid = mac.host_pid()
    assert mac.record()["autostart"] == "launchd"
    assert "자동 시작: launchd(com.clewpath.host)" in out and "watch >/dev/null" not in out
    r = mac.install()
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "자동 시작 등록 파일 그대로(내용이 같음)" in out, mac.diag(r)
    assert [c[1] for c in mac.launchctl()] == ["bootout", "bootstrap", "bootout", "bootstrap"]
    assert mac.record()["autostart"] == "launchd" and mac.host_pid() != pid


@pytest.mark.parametrize("mode", ["fail", "fail-once"])
def test_mac_bootstrap_failure_still_installs(mac, mode):
    """올리지 못해도(SSH 셸 — 로그인한 화면 세션이 없다) 설치는 성공이다 — 등록 파일은 두어 다음 로그인 때 올라가고, 지금 켜는
    법을 알린다. 막 내린 같은 이름이 정리 중이라 첫 bootstrap 만 실패하는 경우는 1초 뒤 한 번 더 해서 올린다."""
    r = mac.fresh(env=mac.env(FAKE_LAUNCHCTL=mode))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "=== 설치 완료 ===" in out, mac.diag(r)
    assert [c[1] for c in mac.launchctl()] == ["bootout", "bootstrap", "bootstrap"], "실패하면 1초 뒤 한 번 더"
    assert mac.plist.is_file()
    mac.host_pid()
    if mode == "fail-once":
        assert "[OK]   자동 시작 등록 (launchd" in out and mac.record()["autostart"] == "launchd"
        return
    assert "[WARN] 자동 시작을 지금 켜지 못했습니다" in out and "125: Domain does not support" in out
    assert f"launchctl bootstrap gui/{os.getuid()} '{mac.plist}'" in out and "SSH" in out
    assert mac.record()["autostart"] == "launchd-pending"


def test_mac_dry_run_and_no_autostart_register_nothing(mac):
    """--dry-run 은 무엇을 할지만 보여 주고(파일·launchctl 없음), --no-autostart 는 등록하지 않는다 — 이미 있던 등록은 그대로
    두고(기록도 이어 적는다) 알린다."""
    mac.unpack()
    before = _tree_hash(mac.home)
    r = mac.install("--dry-run")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "DryRun: 자동 시작(launchd) 등록 건너뜀" in out and str(mac.plist) in out, mac.diag(r)
    assert "launchctl bootstrap gui/" in out and _tree_hash(mac.home) == before and mac.launchctl() == []
    r = mac.install("--no-autostart")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "[WARN] 자동 시작 등록 건너뜀(--no-autostart)" in out, mac.diag(r)
    assert not mac.plist.exists() and mac.launchctl() == [] and "autostart" not in mac.record()
    assert "watch >/dev/null" in out
    r = mac.install()
    assert r.returncode == 0 and mac.plist.is_file() and mac.record()["autostart"] == "launchd", mac.diag(r)
    r = mac.install("--no-autostart")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "이미 있던 자동 시작 등록은 그대로 둡니다" in out, mac.diag(r)
    assert mac.plist.is_file() and mac.record()["autostart"] == "launchd"
    assert [c[1] for c in mac.launchctl()] == ["bootout", "bootstrap"]


def test_mac_replaces_another_installs_autostart_with_a_warning(mac):
    """같은 사용자에 다른 설치본의 등록이 있으면(설치본은 사용자당 하나) 알리고 이 설치본으로 덮어쓴다."""
    other = hostctl.launchd_plist("/Users/x/Other/app", "/Users/x/.claude/session_manager", path_env=None, lang=None,
                                  umask=None)
    mac.plist.parent.mkdir(parents=True)
    mac.plist.write_bytes(plistlib.dumps(other))
    r = mac.fresh()
    out = r.stdout.decode("utf-8")
    assert "다른 설치본의 자동 시작(/Users/x/Other/app/start-connector.sh)을 이 설치본으로 바꿉니다" in out, mac.diag(r)
    assert plistlib.loads(mac.plist.read_bytes())["ProgramArguments"][-1] == str(mac.root / _posix.START_SCRIPT)


def test_mac_launchd_job_becomes_the_host_and_ends_quietly(mac):
    """등록 파일 그대로(launchd 흉내) 작업을 돌린다 — 꺼진 Host 는 foreground 가 띄워 작업의 pid 가 곧 Host 이고(env 는 등록
    파일의 PATH·LANG, 출력은 host.log), 떠 있으면 5분마다의 확인은 곧 0, stop --hold 로 멈추면 KeepAlive 가 다시 불러도
    foreground 가 곧 0, 설치 폴더를 손으로 지웠으면 0 — 어느 쪽도 30초마다 다시 부르는 0 아닌 종료가 아니다.
    SIGTERM 정상 종료도 0 이다(server.main — uvicorn 이 다시 일으키는 SIGTERM 을 0 종료로 받는다. 기본 처리기면 -15 라
    launchd 가 '죽음'으로 보고 stop 한 Host 를 30초 뒤 되살렸다)."""
    mac.fresh()
    r = mac.sh(mac.launcher, "stop")
    assert r.returncode == 0 and mac.host_pids() == [], mac.diag(r)
    pidf = mac.base / "job.pid"
    job = mac.popen([str(mac.tools / "fake-launchd"), str(mac.plist), "--pid", str(pidf)],
                    env=mac.env(FAKE_LAUNCHD_WAIT=600))
    assert _wait(lambda: pidf.is_file() and pidf.read_text() and mac.host_pids() == [int(pidf.read_text())]
                 and mac.get("/api/health") == {"status": "ok"}, 60), mac.diag()
    pid = int(pidf.read_text())
    d = plistlib.loads(mac.plist.read_bytes())
    env = psutil.Process(pid).environ()
    assert env["PATH"] == d["EnvironmentVariables"]["PATH"] and env["LANG"] == d["EnvironmentVariables"]["LANG"]
    assert _fd_path(pid, 1) == os.path.realpath(mac.data / "host.log")
    assert mac.text(mac.data / "host.log").count("=== launcher start") == 2
    assert "Traceback" not in mac.text(mac.data / "launchd.log")
    tick = subprocess.run([str(mac.tools / "fake-launchd"), str(mac.plist)], env=mac.env(), timeout=90)
    assert tick.returncode == 0 and mac.text(mac.launchd_rc).splitlines() == ["0"] and mac.host_pids() == [pid]
    r = mac.sh(mac.launcher, "stop", "--hold")                                # 계속 끄기 — SIGTERM + maintenance.flag
    assert r.returncode == 0 and job.wait(timeout=60) == 0, mac.diag(r)
    rcs = mac.text(mac.launchd_rc).splitlines()
    assert rcs == ["0", "0"], rcs                                           # 정상 종료 = 0 — KeepAlive 가 되살리지 않는다
    n = len(rcs)
    shutil.move(str(mac.root), str(mac.base / "moved away"))                 # 설치 폴더를 손으로 지움
    subprocess.run([str(mac.tools / "fake-launchd"), str(mac.plist)], env=mac.env(), timeout=90)
    assert mac.text(mac.launchd_rc).splitlines()[n:] == ["0"] and mac.host_pids() == []


def test_mac_install_then_uninstall_round_trip(mac):
    r = mac.fresh()
    pid = mac.host_pid()
    r = mac.uninstall("--yes")
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "[OK]   자동 시작(launchd) 해제" in out and "=== 제거 완료 ===" in out, mac.diag(r)
    assert not mac.plist.exists() and not mac.root.exists() and _wait(lambda: _gone(pid), 10)
    assert [c[:2] for c in mac.launchctl()][-2:] == [["F", "bootout"], ["F", "print"]], "maintenance.flag 를 만든 뒤에 내린다"


def test_linux_install_registers_no_launchd(box):
    """[linux] 자동 시작은 안내만(systemd --user 는 결정 전) — 등록 파일·launchctl 이 없다."""
    r = box.fresh()
    out = r.stdout.decode("utf-8")
    assert "자동 시작은 아직 없습니다" in out and box.launchctl() == [] and not (box.home / "Library").exists()
    assert "autostart" not in box.record()
    r = box.uninstall("--yes")
    assert r.returncode == 0 and box.launchctl() == [], box.diag(r)


# ---------------------------------------------------------------- 제거

def _start_watch_and_broker(box) -> tuple[subprocess.Popen, int]:
    w = box.popen(["/bin/sh", str(box.launcher), "watch", "10"], env=box.env(), cwd=str(box.home))
    assert _wait(lambda: (box.data / "watch.lock.d" / "pid").is_file(), 10)
    box.popen([str(box.vpy), "-m", "session_manager.ptyd"], env=box.env(), cwd=str(box.root), start_new_session=True)
    assert _wait(lambda: (box.data / "ptyd.json").is_file(), 20), box.diag()
    return w, json.loads((box.data / "ptyd.json").read_text(encoding="utf-8"))["pid"]


def _slow_grep_env(box) -> dict:
    """grep 이 출력을 다 낸 뒤 1초 더 산다 — 찾는 글자를 명령줄에 가진 채로. 프로세스 훑기가 grep 자신을 '남은 프로세스'로
    보던 경합(파이프로 읽으며 바로 확인)을 늘 재현한다."""
    tools = box.base / "slow-grep"
    tools.mkdir()
    (tools / "grep").write_text(f'#!/bin/sh\n"{shutil.which("grep")}" "$@"\nrc=$?\nsleep 1\nexit $rc\n', encoding="utf-8")
    os.chmod(tools / "grep", 0o755)
    return box.env(PATH=f"{tools}:{os.environ['PATH']}")


def test_uninstall_stops_everything_and_leaves_claude_files(box):
    box.fresh()
    host = box.host_pid()
    watch, broker = _start_watch_and_broker(box)
    sock = json.loads((box.data / "ptyd.json").read_text(encoding="utf-8"))["address"]
    settings = (box.claude / "settings.json").read_bytes()
    projects = _tree_hash(box.projects)
    r = box.uninstall("--yes", env=_slow_grep_env(box))
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "=== 제거 완료 ===" in out, box.diag(r)
    assert _wait(lambda: _gone(host) and _gone(broker) and watch.poll() is not None, 10)
    assert not box.root.exists() and box.data.is_dir() and not (box.data / "maintenance.flag").exists()
    assert not os.path.exists(sock), "남은 소켓은 치운다"
    assert (box.claude / "settings.json").read_bytes() == settings, "settings.json 은 읽기만(결정 Q6)"
    assert _tree_hash(box.projects) == projects, "projects 는 그대로(S36)"
    assert "ClewPath 훅이 5개 남아 있습니다" in out and "/api/hooks/event" in out
    assert box.uv.exists(), "uv 는 --purge-uv 일 때만"
    assert not list(box.tmpdir.glob("clewpath-uninstall.*")), "임시 사본도 치운다"
    time.sleep(1)
    assert box.host_pids() == [], "감시가 다시 띄우지 않는다"


def test_uninstall_purge_removes_data_and_uv_but_never_projects(box):
    box.fresh()
    (box.chome / "python" / "cpython-3.11.99-fake").mkdir(parents=True)
    (box.chome / "python" / ".lock").write_text("", encoding="utf-8")
    settings = (box.claude / "settings.json").read_bytes()
    projects = _tree_hash(box.projects)
    r = box.uninstall("--purge", "--yes")
    assert r.returncode == 0, box.diag(r)
    assert not box.data.exists() and not box.chome.exists(), "데이터·uv·파이썬, 그리고 빈 ClewPath 폴더까지"
    assert _tree_hash(box.projects) == projects and (box.claude / "settings.json").read_bytes() == settings


def test_uninstall_purge_uv_keeps_foreign_files(box):
    box.fresh()
    (box.chome / "bin" / "someone-else").write_text("x", encoding="utf-8")   # CLEWPATH_HOME 을 남과 같이 쓴 경우
    (box.chome / "python" / "notes.txt").parent.mkdir(parents=True)
    (box.chome / "python" / "notes.txt").write_text("x", encoding="utf-8")
    r = box.uninstall("--purge-uv", "--yes")
    assert r.returncode == 1, box.diag(r)                                  # 남긴 파이썬 폴더는 '남은 것'(검토 2회차 X2)
    assert not box.uv.exists() and (box.chome / "bin" / "someone-else").exists()
    assert (box.chome / "python" / "notes.txt").exists() and "파이썬 폴더는 남깁니다" in r.stdout.decode("utf-8")


def _run_on_tty(argv: list[str], env: dict, answer: str, timeout: float = 120) -> tuple[int, str]:
    """제어 터미널(pty)을 가진 자식으로 — 확인 질문이 나오면 answer 를 친다."""
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(env["HOME"])
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    out, sent, end = b"", False, time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if fd in ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
                if not sent and "계속할까요".encode("utf-8") in out:
                    os.write(fd, answer.encode("utf-8") + b"\n")
                    sent = True
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


def test_uninstall_asks_on_the_terminal(box):
    box.fresh()
    host = box.host_pid()
    r = box.uninstall(start_new_session=True)                # 제어 터미널 없음 + --yes 없음
    assert r.returncode == 2 and "--yes" in r.stdout.decode("utf-8") and box.root.exists() and not _gone(host)
    argv = ["/bin/sh", str(box.root / "uninstall.sh")]
    rc, out = _run_on_tty(argv, box.env(), "n")
    assert rc == 1 and "취소했습니다" in out and box.root.exists() and not _gone(host), out
    rc, out = _run_on_tty(argv, box.env(), "y")
    assert rc == 0 and not box.root.exists() and _wait(lambda: _gone(host), 10), out


def test_uninstall_refuses_dev_trees_foreign_dirs_and_running_updates(box):
    box.fresh()
    host = box.host_pid()
    (box.root / ".git").mkdir()
    r = box.uninstall("--yes")
    assert r.returncode == 1 and "개발 체크아웃" in r.stdout.decode("utf-8") and box.root.exists()
    (box.root / ".git").rmdir()
    foreign = box.base / "foreign"
    (foreign / "session_manager").mkdir(parents=True)
    (foreign / "pyproject.toml").write_text('[project]\nname = "other"\n', encoding="utf-8")
    shutil.copyfile(box.root / "uninstall.sh", foreign / "uninstall.sh")
    r = box.sh(foreign / "uninstall.sh", "--yes")
    assert r.returncode == 1 and "ClewPath 설치 폴더가 아닙니다" in r.stdout.decode("utf-8") and foreign.exists()
    r = box.sh(box.root / "uninstall.sh", "--root", "/", "--yes")
    assert r.returncode == 1 and box.root.exists()
    runner = box.holder("update_runner.sh")
    (box.data / "update.inprogress").write_text(f"pid={runner.pid}\nat={int(time.time())}\nby=runner\n",
                                                encoding="utf-8")
    r = box.uninstall("--yes")
    assert r.returncode == 1 and "진행 중" in r.stdout.decode("utf-8") and box.root.exists()
    assert box.host_pids() == [host], "아무것도 멈추지 않았다"
