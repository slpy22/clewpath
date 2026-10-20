"""맥·리눅스 설치·제거 실행 시험 — docs/designs/posix-install.md 3.2·3.5 (+ S12·S16·S17·S24·S26·S36~S38·S43).

진짜 install.sh·uninstall.sh·런처·hostctl·Host(session_manager.server)·브로커를 가짜 홈에서 돌린다. 가짜는 uv 하나다:
- 가짜 uv: --version 은 고정 버전, `python find` 는 시스템(= 지금 파이썬)·관리 런타임(가짜 폴더), `python install` 은 관리
  런타임 폴더를 만든다, `venv` 는 `python -m venv --without-pip` + 지금 파이썬의 site-packages 를 가리키는 .pth(네트워크 없이
  의존성), `sync`·`pip install -e` 는 편집 설치 흉내(.pth 로 설치 루트 + dist-info 버전). 부른 모양(인자·cwd·env)을
  calls.jsonl 에 적고 uv.json 으로 실패를 넣는다.
- 설치 재료는 이 작업 트리의 PAYLOAD 사본(pwa 는 작은 대역) — get.sh 처럼 설치 루트 옆 .clewpath-get.XXXXXX/stage 에 둔다.
- 경로: 홈에 공백·한글. 설치 위치는 공통 경로 블록의 리눅스 기본값($HOME/.local/share/clewpath/app).
진짜 uv(고정 해시 tarball)·오프라인 uv sync 로 도는 왕복은 컨테이너 e2e(6.3)의 몫이다. Windows 에선 건너뛴다.
"""
from __future__ import annotations

import hashlib
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 설치(install.sh·uninstall.sh) — POSIX 전용")

if sys.platform != "win32":
    import psutil
    import pty

import session_manager
from session_manager import updater
from session_manager.osdep import _posix

REAL_PKG = Path(session_manager.__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
DEAD_PID = 4_194_999
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
with open(ctl / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keep},
                        "marker": marker}) + "\n")
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
    """가짜 컨트롤플레인 — 발급(횟수 기록), 나머지(토큰·매니페스트)는 Host 가 fail-open 하는 응답."""

    def __init__(self):
        self.calls: list[str] = []
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
    """가짜 홈 하나 — 설치 위치·클로드 폴더·가짜 uv·시험 TMPDIR."""

    def __init__(self, tmp: Path, payload: Path):
        self.base = base = tmp.resolve()
        self.home = base / "home x" / "홈"
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

    # ---- 준비
    def env(self, **extra) -> dict:
        drop = ("SESSION_MANAGER_CLAUDE_HOME", "XDG_DATA_HOME", "_SM_TEST_HOME_SET", "VIRTUAL_ENV", "PYTHONPATH",
                "CONDA_PREFIX")
        e = {k: v for k, v in os.environ.items()
             if k not in drop and not k.startswith(("SM_", "UV_", "CLEWPATH_", "CLAUDE"))}
        e.update(HOME=str(self.home), TMPDIR=str(self.tmpdir), FAKE_UV_CTL=str(self.ctl), LANG="C.UTF-8",
                 FAKE_MARKER=str(self.data / "update.inprogress"))
        e.update({k: str(v) for k, v in extra.items()})
        return e

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
    status_txt = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    assert "\nUmask:\t0027\n" in status_txt
    env = psutil.Process(pid).environ()
    assert not [k for k in env if k.startswith("UV_") or k.startswith("CLEWPATH_CLIENT")], sorted(env)
    assert env["CLEWPATH_CONFIG"] == str(box.conf) and env["HOME"] == str(box.home)
    # 훅: Host 가 첫 기동에 등록 — 맥·리눅스는 curl(curl.exe 아님)
    hooks = json.loads((box.claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    cmds = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert cmds and all(c.startswith("curl -s -m 3 ") and f":{box.port}/api/hooks/event" in c for c in cmds)
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


def test_token_argument_is_moved_to_env_with_one_warning(box):
    """--client-token 은 받되(Q19) 경고하고 env 로 옮긴다 — 서막 뒤 exec 한 install.sh 의 인자에는 싣지 않는다."""
    r = box.fresh("--client-token", "tok-ARGV-9")
    out = r.stdout.decode("utf-8")
    assert out.count("같은 PC 의 다른 사용자에게 보입니다(ps)") == 1
    assert "tok-ARGV-9" not in out + r.stderr.decode("utf-8")
    assert tomllib.loads(box.conf.read_text(encoding="utf-8"))["cloud"]["client_token"] == "tok-ARGV-9"
    assert "tok-ARGV-9" not in json.dumps(psutil.Process(box.host_pid()).environ())


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
        assert not box.root.exists() and (st / "session_manager").is_dir(), "하나도 옮기지 않았다"
        shutil.rmtree(st.parent)
    foreign = box.base / "남의 폴더"
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
    for r in (box.install(), box.install_from(box.stage(readme="NOPE\n"))):
        assert r.returncode == 1 and "진행 중" in r.stdout.decode("utf-8"), box.diag(r)
        assert box.host_pids() == [pid] and box.text(box.root / "README.md") == readme, "아무것도 바꾸지 않는다"
        assert f"pid={runner.pid}" in box.text(marker), "남의 표식은 그대로"
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
    assert r.returncode == 0, box.diag(r)
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
