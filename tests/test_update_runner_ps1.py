"""Windows 업데이트 러너(update_runner.ps1) 실행 시험 — ② 복사 오류를 삼키고, 백업 실패 종료는 잠금·결과를 남기지 않던 것.

진짜 러너를 updater.apply 처럼 데이터 폴더 updates/ 에 복사해 5.1(그리고 있으면 7)로 끝까지 돌린다.
- 가짜 설치본(옛 트리 1.0.0)·스테이지(새 트리 1.1.0): PAYLOAD 의 일부. 경로에 공백·한글. .venv 가 없어 의존성 동기화는 건너뛴다
  (진짜 uv 를 부르지 않는다 — LOCALAPPDATA 도 임시 폴더로).
- 가짜 Host: 이 시험 프로세스 안의 HTTP 스레드. /api/owner/diagnostics 에 claude_home 과 '지금 설치 폴더 pyproject.toml 의 버전' 을
  답한다 — 교체되면 버전도 바뀐다. 재기동 스크립트(-RestartScript)는 restarts.txt 에 한 줄 적는 ps1(러너가 Host 를 다시 띄웠는지만).
- WaitPid 는 있을 수 없는 pid(4 의 배수가 아님). 롤백의 python 일괄 종료는 설치 폴더 아래 python 이 없어 아무것도 안 건드린다.
- 복사 실패 흉내: 시험 프로세스가 파일을 공유 없이(못 읽음) 또는 읽기만 공유로(못 지우고 못 덮음) 열어 둔다(CreateFileW).
진짜 설치본·데이터 폴더·예약 작업은 건드리지 않는다.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from session_manager import updater

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                                reason="Windows 러너(update_runner.ps1) — powershell.exe 5.1 필요")

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "session_manager" / "update_runner.ps1"
OLD, NEW = "1.0.0", "1.1.0"
ENGINES = ["powershell"] + (["pwsh"] if shutil.which("pwsh") else [])
_NO_WINDOW = 0x08000000
SHARE_NONE, SHARE_READ = 0, 1


def make_tree(root: Path, ver: str, extra: dict[str, str]) -> None:
    files = {"session_manager/__init__.py": f"# {ver}\n",
             "session_manager/sub/mod.py": f"V = '{ver}'\n",
             "pwa/index.html": f"<p>{ver}</p>\n",
             "pyproject.toml": f'[project]\nname = "clewpath-host"\nversion = "{ver}"\n',
             "README.md": f"readme {ver}\n",
             "LICENSE": "license\n", **extra}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


@contextlib.contextmanager
def held(path: Path, share: int):
    """다른 프로세스가 그 파일을 쥐고 있는 것 — share 0 이면 못 읽음, 1(읽기만)이면 읽히지만 못 지우고 못 덮는다."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, share, None, 3, 0x80, None)      # GENERIC_READ, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"못 열었다: {path}")
    try:
        yield
    finally:
        k32.CloseHandle(h)


class Upd:
    def __init__(self, tmp: Path, engine: str):
        self.engine = engine
        self.base = tmp / "업데이트 시험"
        self.root = self.base / "app"
        self.data = self.base / "data"
        self.work = self.data / "updates"
        self.stage = self.work / f"stage-{NEW}"
        self.backup = self.work / f"backup-{OLD}"
        self.log = self.work / f"apply-{NEW}.log"
        make_tree(self.root, OLD, {"install.ps1": "old ps1\n", "session_manager/held.py": "held old\n"})
        make_tree(self.stage, NEW, {"session_manager/held.py": "held new\n", "session_manager/new_only.py": "new\n"})
        self.old, self.new = snapshot(self.root), snapshot(self.stage)
        self.runner = self.work / RUNNER.name
        shutil.copy2(RUNNER, self.runner)                         # updater.apply 처럼 설치 폴더 밖에서 돈다
        (self.work / "apply.lock").write_text(str(time.time()), encoding="utf-8")
        self.restarts = self.base / "restarts.txt"
        self.restart_ps1 = self.base / "restart.ps1"
        self.restart_ps1.write_bytes(b"\xef\xbb\xbf" + (
            f"Add-Content -LiteralPath '{str(self.restarts).replace(chr(39), chr(39) * 2)}' -Value 'x'\r\n").encode("utf-8"))
        root = self.root

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                m = re.search(r'version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8")) \
                    if (root / "pyproject.toml").is_file() else None
                body = json.dumps({"claude_home": "x", "version": m.group(1) if m else None}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def run(self) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SM_")}
        env["LOCALAPPDATA"] = str(self.base / "localappdata")    # Sync-Deps 가 진짜 uv 를 찾지 않게(어차피 .venv 가 없어 건너뜀)
        args = [self.engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.runner),
                "-InstallRoot", str(self.root), "-StageDir", str(self.stage), "-BackupDir", str(self.backup),
                "-WaitPid", "99999999", "-Port", str(self.port), "-Version", NEW, "-LogFile", str(self.log),
                "-RestartScript", str(self.restart_ps1)]
        return subprocess.run(args, capture_output=True, timeout=300, creationflags=_NO_WINDOW, env=env)

    def result(self) -> dict | None:
        p = self.work / "last_apply.json"
        return json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else None   # 5.1 은 BOM 을 붙인다

    def locked(self) -> bool:
        return (self.work / "apply.lock").exists()

    def text(self) -> str:
        return self.log.read_text(encoding="utf-8-sig") if self.log.is_file() else ""

    def restarted(self, n: int = 1) -> int:
        """러너가 띄운 재기동 스크립트(분리 실행)가 적은 줄 수 — n 줄이 될 때까지 잠깐 기다린다."""
        for _ in range(150):
            k = len(self.restarts.read_text(encoding="utf-8-sig").splitlines()) if self.restarts.is_file() else 0
            if k >= n:
                return k
            time.sleep(0.1)
        return k

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(params=ENGINES)
def upd(tmp_path, request):
    u = Upd(tmp_path, request.param)
    yield u
    u.close()


def _no_nested(root: Path):
    for name in ("session_manager", "pwa"):
        assert not (root / name / name).exists(), f"{name} 안에 {name} 가 한 겹 더 생겼다(지우지 못한 폴더 위에 폴더째 복사)"


def test_payload_names_used_here_are_in_the_runner():
    assert {"session_manager", "pwa", "pyproject.toml", "README.md", "LICENSE", "install.ps1"} <= set(updater.PAYLOAD)


def test_update_succeeds_records_and_unlocks(upd):
    r = upd.run()
    assert r.returncode == 0, upd.text()
    assert snapshot(upd.root) == {**upd.new, "install.ps1": "old ps1\n"}, "스테이지에 없는 기존 항목은 남긴다"
    assert snapshot(upd.backup) == upd.old
    assert upd.result()["ok"] is True and upd.result()["version"] == NEW
    assert not upd.locked()
    assert upd.restarted() == 1
    assert "업데이트 성공" in upd.text()


def test_unreadable_stage_file_fails_and_rolls_back(upd):
    """스테이지의 파일 하나를 못 읽는다(교체가 반쪽). 고치기 전: 오류가 안 보여 반쪽 트리로 '성공' 을 기록했다."""
    with held(upd.stage / "session_manager" / "sub" / "mod.py", SHARE_NONE):
        r = upd.run()
    log = upd.text()
    assert r.returncode == 0, log
    assert upd.result()["ok"] is False and upd.result()["version"] == NEW, log
    assert snapshot(upd.root) == upd.old, "롤백은 옛 트리 그대로"
    assert not upd.locked()
    assert "[FAIL] 교체" in log and "mod.py" in log, log
    assert "이전 버전 복원 완료" in log and "롤백 후 정상 동작 확인" in log, log
    assert upd.restarted() == 1                                  # 새 버전은 안 띄우고, 되돌린 옛 버전만


def test_backup_failure_stops_records_and_unlocks(upd):
    """설치본의 파일 하나를 못 읽는다(백업이 반쪽). 고치기 전: 반쪽 백업 위에 교체를 진행했다(그 위에서 롤백하면 롤백도 반쪽).
    멈추는 길(exit 1)도 결과 기록·잠금 해제를 거친다 — 아니면 다음 적용이 5분 거부되고 재기동 자동 적용이 같은 버전을 또 받는다."""
    with held(upd.root / "session_manager" / "held.py", SHARE_NONE):
        r = upd.run()
    log = upd.text()
    assert r.returncode == 1, log
    assert snapshot(upd.root) == upd.old, "설치 폴더는 그대로"
    assert upd.result()["ok"] is False and upd.result()["version"] == NEW, log
    assert not upd.locked()
    assert "[FAIL] 백업 실패" in log and "held.py" in log and "파일 교체 완료" not in log, log
    assert upd.restarted() == 1                                  # 옛 Host 를 다시 띄운다


def test_undeletable_install_file_restores_what_it_can_and_restarts(upd):
    """설치본의 파일 하나를 못 지우고 못 덮는다(읽기는 됨 → 백업은 된다). 교체가 실패해 롤백하는데 복원도 그 파일에서 막힌다 —
    그래도 폴더 안에 폴더를 만들지 않고 나머지는 되돌리고(그 파일은 처음부터 안 바뀌었다), 옛 Host 를 다시 띄우고, 실패로 기록한다.
    5.1 의 Copy-Item 은 잠긴 대상을 덮다 종료 오류를 던져 그 뒤 파일을 건너뛰었다 — 하나씩 복사해 나머지는 되돌린다.
    고치기 전: 못 지운 session_manager 위에 폴더째 복사해 session_manager\\session_manager 가 생기고 '성공' 을 기록했다."""
    with held(upd.root / "session_manager" / "held.py", SHARE_READ):
        r = upd.run()
    log = upd.text()
    assert r.returncode == 0, log
    _no_nested(upd.root)
    assert snapshot(upd.root) == upd.old, "옛 트리 그대로(교체 때 들어온 새 파일도 되돌림 때 지워진다)"
    assert upd.result()["ok"] is False, log
    assert not upd.locked()
    assert "[FAIL] 교체" in log and "[FAIL] 이전 버전 복원" in log, log
    assert upd.restarted() == 1
