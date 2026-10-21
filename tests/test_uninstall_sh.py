"""맥·리눅스 제거(uninstall.sh) 실행 시험 — docs/designs/posix-install.md 3.5 (+ S30). 2회차 검토 반영분.

설치 폴더 모양은 install.sh 없이 손으로 만든다(_lay: PAYLOAD 사본 + 설치 기록 + venv 파이썬 링크 + 렌더한 런처) — 제거 쪽
규칙만 본다. Host·브로커·감시는 같은 명령줄을 가진 가짜(잠자는 파이썬·sh)다. 진짜 설치·Host 를 띄우는 왕복은
tests/test_install_sh.py 의 제거 시험이 맡는다. Windows 에선 건너뛴다.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 제거(uninstall.sh) — POSIX 전용")

if sys.platform != "win32":
    import pty                                   # termios 가 필요해 Windows 에선 import 조차 안 된다
    from session_manager.osdep import _posix
    from tests.test_install_sh import ROOT, _gone, _wait, box, payload  # noqa: F401 — box·payload 는 픽스처

# 가짜 Host·브로커 — 진짜처럼 runtime.json·ptyd.json 에 제 pid 를 적고 잔다. 명령줄 끝에 -m <모듈>(이 설치본 판정의 재료)
FAKE_PROC = """
import json, os, time
with open(os.environ["FAKE_OUT"], "w") as f:
    json.dump({"pid": os.getpid(), "port": 1, "address": ""}, f)
time.sleep(300)
"""
OURS = ("session_manager", "pwa", "pyproject.toml", "uv.lock", "install.ps1", "install.sh", "uninstall.sh", "README.md",
        "LICENSE", ".venv", "clewpath_host.egg-info", "start-connector.sh", ".clewpath-install", ".clewpath-swap.7",
        ".clewpath-old.8", ".clewpath-trash.9", ".DS_Store")


def _lay(box, root: Path | None = None, *, data: Path | None = None, launcher: bool = True) -> Path:
    """설치 폴더 모양(우리 이름 전부) — PAYLOAD 사본, 설치 기록, venv 파이썬(지금 파이썬으로 가는 링크), 렌더한 런처."""
    root = root or box.root
    data = data or box.data
    root.mkdir(parents=True, exist_ok=True)
    for p in box.payload.iterdir():
        shutil.copytree(p, root / p.name) if p.is_dir() else shutil.copyfile(p, root / p.name)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").symlink_to(sys.executable)
    for d in ("clewpath_host.egg-info", ".clewpath-swap.7", ".clewpath-old.8", ".clewpath-trash.9"):
        (root / d).mkdir()
        (root / d / "x").write_text("x", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\0")
    (root / ".clewpath-install").write_text(
        f"# clewpath-install v1 — install.sh 가 쓴다. 손대지 마세요\nhome={box.chome}\nuv={box.uv}\ndata_dir={data}\n",
        encoding="utf-8")
    if launcher:
        tpl = (ROOT / "session_manager" / _posix.LAUNCHER_TEMPLATE).read_text(encoding="utf-8")
        text = _posix.render_launcher(tpl, data_dir=data, conf_file=data / "clewpath.toml", port=box.port)
        (root / _posix.START_SCRIPT).write_bytes(text.encode("utf-8"))
        os.chmod(root / _posix.START_SCRIPT, 0o755)
    data.mkdir(parents=True, exist_ok=True)
    return root


def _fake(box, python: Path, module: str, out: Path):
    """python(설치본 venv 의 것 — 다른 글자로 가리킬 수도)으로 가짜 Host·브로커를 띄우고 그 pid 를 out 에서 읽는다."""
    out.unlink(missing_ok=True)
    p = box.popen([str(python), "-c", FAKE_PROC, "-m", module], env=box.env(FAKE_OUT=out), start_new_session=True)
    assert _wait(out.is_file, 10) and _wait(lambda: json.loads(out.read_text(encoding="utf-8") or "{}").get("pid"), 5)
    return p


def _out(r) -> str:
    return r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")


def _flag(box) -> Path:
    return box.data / "maintenance.flag"


def _on_tty(argv: list[str], env: dict, answer: str, before=None, timeout: float = 60) -> tuple[int, str]:
    """제어 터미널(pty)로 — 확인 질문이 나오면 before() 를 부른 뒤 answer 를 친다(test_install_sh._run_on_tty + 끼어들 자리)."""
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
                    if before:
                        before()
                    os.write(fd, answer.encode("utf-8") + b"\n")
                    sent = True
    finally:
        _, status = os.waitpid(pid, 0)
        os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


# ---------------------------------------------------------------- [U1] 우리 이름만 지운다

def test_keeps_foreign_files_in_the_install_folder(box):
    """README 의 손 설치(zip 을 설치할 폴더에 풀고)대로 ~/Downloads 에 풀었어도 — 우리 이름만 지우고 남의 것·폴더는 남긴다."""
    dl = _lay(box, box.home / "Downloads")
    (dl / "사진").mkdir()
    (dl / "사진" / "가족.jpg").write_bytes(b"img")
    (dl / "2026-세금신고.pdf").write_bytes(b"tax")
    (dl / ".bashrc-backup").write_text("x", encoding="utf-8")
    rc, out = _on_tty(["/bin/sh", str(dl / "uninstall.sh")], box.env(), "n")
    assert rc == 1 and "ClewPath 가 놓는 이름만" in out and "2026-세금신고.pdf" in out, out
    r = box.sh(dl / "uninstall.sh", "--yes")
    out = _out(r)
    assert r.returncode == 0 and "=== 제거 완료 ===" in out, out
    assert (dl / "사진" / "가족.jpg").read_bytes() == b"img" and (dl / "2026-세금신고.pdf").read_bytes() == b"tax"
    assert (dl / ".bashrc-backup").exists()
    assert sorted(x.name for x in dl.iterdir()) == sorted([".bashrc-backup", "2026-세금신고.pdf", "사진"])
    assert "폴더는 남겼습니다" in out and "사진" in out, out


def test_removes_every_name_of_ours_and_the_folder(box):
    _lay(box)
    r = box.uninstall("--yes")
    assert r.returncode == 0 and "=== 제거 완료 ===" in _out(r), _out(r)
    assert not box.root.exists() and box.data.is_dir()


# ---------------------------------------------------------------- [U2] 홈 가드는 물리 경로로

@pytest.mark.parametrize("case", ["home_trailing_slash", "home_is_a_symlink", "parent_of_home", "claude_folder",
                                  "above_data_dir"])
def test_refuses_home_and_claude_folders_however_spelled(box, case):
    home = box.home
    env = {}
    if case == "home_trailing_slash":
        root, env["HOME"] = home, f"{home}/"
    elif case == "home_is_a_symlink":                       # 홈을 다른 디스크로 옮기고 링크로 남긴 모양
        link = box.base / "homelink"
        link.symlink_to(home)
        root, env["HOME"] = home, str(link)
    elif case == "parent_of_home":
        root = home.parent
    root_data = None
    if case == "claude_folder":                             # 클로드 폴더에 풀었다 — 우리 session_manager 가 곧 데이터 폴더
        root = box.base / "claude2"
        root_data = root / "session_manager"
    elif case == "above_data_dir":
        root = box.base / "cfg"
        root_data = root / "x" / ".claude" / "session_manager"
    _lay(box, root, data=root_data, launcher=False)
    keep = home / "my-notes.txt"
    keep.write_text("mine", encoding="utf-8")
    before = sorted(x.name for x in root.iterdir())
    r = box.sh(root / "uninstall.sh", "--yes", env=box.env(**env))
    out = _out(r)
    assert r.returncode == 1 and "지우지 않습니다" in out, out
    assert sorted(x.name for x in root.iterdir()) == before and keep.read_text(encoding="utf-8") == "mine"
    assert not _flag(box).exists()


# ---------------------------------------------------------------- [U3] 끊긴 제거는 다시 실행해 마저 지운다

def _tool(box, name: str, body: str) -> dict:
    """PATH 앞에 끼울 대역 도구(파이썬) — 진짜는 그대로 exec 한다."""
    tools = box.base / f"tools-{name}"
    tools.mkdir()
    real = shutil.which(name)
    (tools / name).write_text(f"#!{sys.executable}\nimport os, signal, sys\nREAL = {real!r}\n{body}\n"
                              "os.execv(REAL, [REAL] + sys.argv[1:])\n", encoding="utf-8")
    os.chmod(tools / name, 0o755)
    return {"PATH": f"{tools}:{os.environ['PATH']}"}


_KILL_UNINSTALL = '''
def _ppid(p):
    with open(f"/proc/{p}/status") as f:
        return int(next(x for x in f if x.startswith("PPid:")).split()[1])
def _cmd(p):
    with open(f"/proc/{p}/cmdline", "rb") as f:
        return f.read()
def kill_uninstall():
    """나를 부른 제거 스크립트의 본 셸(임시 사본, 하위 셸이 아닌 맨 위)에 SIGTERM — 사람이 창을 닫은 것처럼."""
    p = os.getppid()
    while b"clewpath-uninstall." in _cmd(_ppid(p)):
        p = _ppid(p)
    os.kill(p, signal.SIGTERM)
'''


def test_cut_while_deleting_keeps_the_flag_and_a_rerun_finishes(box):
    """지우는 도중 끊기면(창 닫힘) — 판정·다시 실행에 쓰는 것이 남아 같은 명령으로 마저 지운다. 반쪽 설치본으로 Host 가 뜨지
    않게 maintenance.flag(by=uninstall)는 남긴다."""
    _lay(box)
    env = box.env(FAKE_RM_AT=box.root / "session_manager",
                  **_tool(box, "rm", _KILL_UNINSTALL + "if os.environ['FAKE_RM_AT'] in sys.argv[1:]:\n"
                                                      "    kill_uninstall()"))
    r = box.uninstall("--yes", env=env)
    out = _out(r)
    assert r.returncode != 0 and "다시 실행하면 마저 지웁니다" in out, out
    left = sorted(x.name for x in box.root.iterdir())
    assert left == [".clewpath-install", "pyproject.toml", "uninstall.sh"], left
    assert "by=uninstall" in _flag(box).read_text(encoding="utf-8")
    r = box.uninstall("--yes")                               # 설치 기록 첫 줄만으로 설치본으로 받는다(session_manager 없음)
    assert r.returncode == 0 and "=== 제거 완료 ===" in _out(r), _out(r)
    assert not box.root.exists() and not _flag(box).exists()


def test_cut_before_deleting_takes_the_flag_back(box):
    """아직 아무것도 지우지 않았을 때 끊기면 표식을 거둔다 — 설치본은 그대로이고 다시 띄울 수 있어야 한다."""
    _lay(box)
    once = box.base / "once"
    env = box.env(FAKE_ONCE=once,
                  **_tool(box, "grep", _KILL_UNINSTALL + "if any('/proc/' in a for a in sys.argv[1:]) and "
                                                         "not os.path.exists(os.environ['FAKE_ONCE']):\n"
                                                         "    open(os.environ['FAKE_ONCE'], 'w').close()\n"
                                                         "    kill_uninstall()"))
    r = box.uninstall("--yes", env=env)
    assert r.returncode != 0 and once.exists(), _out(r)
    assert sorted(x.name for x in box.root.iterdir()) == sorted(OURS) and not _flag(box).exists(), _out(r)


# ---------------------------------------------------------------- [U4] 물리 경로·살아 남은 Host

def test_root_is_the_physical_path_from_bin_pwd():
    """맥 APFS 는 대소문자를 무시하고 bash 3.2 내장 pwd -P 는 친 글자를 그대로 둔다 — 설치 폴더는 /bin/pwd -P 로(디스크의
    글자). 다섯 스크립트가 같은 phys_pwd 를 쓴다(C1)."""
    t = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }" in t
    code = [x for x in t.splitlines() if not x.lstrip().startswith("#") and not x.startswith("phys_pwd()")]
    assert not [x for x in code if re.search(r"\bpwd -P\b", x)], "pwd -P 는 phys_pwd 로만"
    assert "ROOT=$(CDPATH='' cd \"$ROOT_ARG\" 2>/dev/null && phys_pwd)" in t
    assert "ROOT=$(CDPATH='' cd \"$(dirname \"$0\")\" && phys_pwd)" in t


def test_host_left_running_under_another_spelling_is_reported(box):
    """Host·브로커가 설치 폴더를 다른 글자(맥의 대소문자 — 리눅스에선 링크로 흉내)로 가리키며 돌면 명령줄로 이 설치본을
    알아보지 못해 멈추지 못한다. 그래도 '제거 완료'(0)라고 하면 안 된다 — runtime.json·ptyd.json 의 pid 가 살아 있으면 알린다."""
    real = box.base / "real"
    _lay(box, real / "app")
    alias = box.base / "Alias"
    alias.symlink_to(real)
    vpy = alias / "app" / ".venv" / "bin" / "python"
    _fake(box, vpy, "session_manager.server", box.data / "runtime.json")
    _fake(box, vpy, "session_manager.ptyd", box.data / "ptyd.json")
    host = json.loads((box.data / "runtime.json").read_text(encoding="utf-8"))["pid"]
    broker = json.loads((box.data / "ptyd.json").read_text(encoding="utf-8"))["pid"]
    r = box.sh(alias / "app" / "uninstall.sh", "--yes")
    out = _out(r)
    assert r.returncode == 1 and "제거가 끝나지 않았습니다" in out, out
    assert f"pid {host}" in out and f"pid {broker}" in out, out
    assert not _gone(host) and not _gone(broker), "이 설치본인지 확인하지 못한 것은 죽이지 않는다"


def test_host_of_another_install_is_left_with_a_warning(box):
    """같은 데이터 폴더를 쓰는 다른 설치본(그 venv 가 아직 있다)의 Host 는 건드리지도, 남은 것으로 세지도 않는다 — 경고만."""
    _lay(box)
    other = _lay(box, box.base / "other" / "app")
    _fake(box, other / ".venv" / "bin" / "python", "session_manager.server", box.data / "runtime.json")
    host = json.loads((box.data / "runtime.json").read_text(encoding="utf-8"))["pid"]
    r = box.uninstall("--yes")
    out = _out(r)
    assert r.returncode == 0 and "다른 설치본" in out and f"pid {host}" in out, out
    assert not box.root.exists() and not _gone(host)


# ---------------------------------------------------------------- [U5] 남의 감시(watch)는 둔다

@pytest.mark.parametrize("case,killed", [("root_file_ours", True), ("root_file_other", False),
                                         ("old_launcher_full_path", True), ("old_launcher_relative", False)])
def test_stops_only_the_watch_of_this_install(box, case, killed):
    root = _lay(box)
    lock = box.data / "watch.lock.d"
    lock.mkdir()
    script = "start-connector.sh" if case in ("root_file_ours", "old_launcher_relative") else \
        str((box.base / "other" if case == "root_file_other" else root) / "start-connector.sh")
    w = box.popen(["sh", "-c", "sleep 120; exit 0", script, "watch", "10"])
    (lock / "pid").write_text(f"{w.pid}\n", encoding="utf-8")
    if case == "root_file_ours":
        (lock / "root").write_text(f"{root}\n", encoding="utf-8")
    elif case == "root_file_other":
        (lock / "root").write_text(f"{box.base / 'other'}\n", encoding="utf-8")
    r = box.uninstall("--yes")
    out = _out(r)
    assert r.returncode == 0, out
    if killed:
        assert _wait(lambda: w.poll() is not None, 10) and "감시(watch) 를 멈췄습니다" in out, out
    else:
        time.sleep(0.5)
        assert w.poll() is None and "감시(watch)" in out, out


# ---------------------------------------------------------------- [X2] --purge-uv 와 uv 관리 런타임의 실제 모양

def test_purge_uv_removes_the_real_uv_python_layout(box):
    """uv 가 늘 만드는 .gitignore('*')·.lock·.temp·cpython-…(+ 부 버전 링크) — 이 모양이면 통째로 지운다."""
    _lay(box)
    py = box.chome / "python"
    rt = py / "cpython-3.11.15-macos-aarch64-none"
    (rt / "bin").mkdir(parents=True)
    (rt / "bin" / "python3.11").write_text("x", encoding="utf-8")
    (py / "cpython-3.11-macos-aarch64-none").symlink_to(rt)
    (py / ".temp").mkdir()
    (py / ".gitignore").write_text("*", encoding="utf-8")
    (py / ".lock").write_text("", encoding="utf-8")
    r = box.uninstall("--purge-uv", "--yes")
    assert r.returncode == 0 and "=== 제거 완료 ===" in _out(r), _out(r)
    assert not py.exists() and not box.chome.exists()


def test_purge_uv_keeps_a_uv_the_install_did_not_use(box):
    """--system-uv 로 설치했거나 CLEWPATH_HOME 을 남과 같이 쓰면 그 자리 uv 는 사용자 것 — 기록의 uv= 가 아니면 남긴다."""
    root = _lay(box)
    rec = root / ".clewpath-install"
    rec.write_text(rec.read_text(encoding="utf-8").replace(f"uv={box.uv}", "uv=/usr/local/bin/uv"), encoding="utf-8")
    mine = box.chome / "bin" / "uv"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_text("#!/bin/sh\n", encoding="utf-8")
    r = box.uninstall("--purge-uv", "--yes")
    assert r.returncode == 0 and "=== 제거 완료 ===" in _out(r), _out(r)
    assert mine.is_file() and "이 자리가 아니어서 남깁니다" in _out(r)


# ---------------------------------------------------------------- [X4] 표식을 만든 뒤 진행 중을 다시 본다

@pytest.mark.parametrize("held", [False, True])
def test_update_started_while_asking_is_seen_after_the_flag(box, held):
    """확인을 묻는 사이 업데이트가 시작됐으면 — 표식을 만든 뒤 다시 보고 멈춘다. 만든 표식은 거둔다(사람이 먼저 둔 것이면
    그 내용으로 되돌린다). 아무것도 멈추거나 지우지 않는다."""
    _lay(box)
    host = _fake(box, box.root / ".venv" / "bin" / "python", "session_manager.server", box.data / "runtime.json")
    if held:
        _flag(box).write_text("by=stop\nat=1\n", encoding="utf-8")

    def runner_starts():
        r = box.holder("update_runner.sh")
        (box.data / "update.inprogress").write_text(f"pid={r.pid}\nat={int(time.time())}\nby=runner\n",
                                                    encoding="utf-8")
    rc, out = _on_tty(["/bin/sh", str(box.root / "uninstall.sh")], box.env(), "y", before=runner_starts)
    assert rc == 1 and "진행 중" in out, out
    assert sorted(x.name for x in box.root.iterdir()) == sorted(OURS) and host.poll() is None
    if held:
        assert _flag(box).read_text(encoding="utf-8") == "by=stop\nat=1\n"
    else:
        assert not _flag(box).exists()
