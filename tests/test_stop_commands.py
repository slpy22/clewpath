"""사람에게 안내하는 「지금 끄기」 명령이 터미널 관리 프로세스(브로커)를 빼는지 — ④ (2026-10-20 조사).

브로커(`python -m session_manager.ptyd`)는 열린 터미널의 claude 를 들고 있다. 업데이트 러너·롤백은 명령줄에 session_manager.ptyd 가
있는 python 을 빼는데(update_runner.ps1), install.ps1 완료 안내의 중지 명령과 README 5절 '지금 끄기' 는 설치 폴더 아래 python 을
모두 껐다 — 터미널 안의 claude 도 같이 끝난다. 게다가 install.ps1 안내는 폴더 이름('app')만 봐서 AppData 아래 다른 파이썬까지 걸렸다.

명령을 그대로 PowerShell 로 돌리되 Get-CimInstance·Get-Process·Stop-Process 를 같은 이름의 함수로 가린다(함수가 cmdlet 보다
먼저 불린다) — 가짜 프로세스 목록에서 무엇을 끄려 했는지만 받는다. 설치 폴더도 없는 경로라 가리개가 빠져도 진짜는 안 걸린다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_ps51_compat import ps_encoded

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("powershell"), reason="Windows powershell.exe 5.1 필요(맥·리눅스는 건너뜀)")

APP = r"C:\Users\홍 길동\AppData\Local\ClewPath-stop-test\app"       # 없는 경로
PROCS = [   # (pid, 실행 파일, 명령줄) — venv 의 python.exe 는 설치 폴더 아래(런처), 명령줄은 -m 모듈
    (101, APP + r"\.venv\Scripts\python.exe", f'"{APP}\\.venv\\Scripts\\python.exe" -m session_manager.server'),
    (102, APP + r"\.venv\Scripts\python.exe", f'"{APP}\\.venv\\Scripts\\python.exe" -m session_manager.ptyd'),
    (103, r"C:\Users\홍 길동\AppData\Local\Programs\Python\Python311\python.exe", "python -m http.server"),
    (104, r"C:\Python311\python.exe", "python x.py"),
]

_MOCKS = r"""
$global:stopped = @()
$procs = @(foreach ($p in $PROCS) { [pscustomobject]@{ Name = 'python.exe'; ProcessId = $p[0]; ExecutablePath = $p[1]; CommandLine = $p[2] } })
function Get-CimInstance($ClassName, $Filter) { $procs }
function Get-Process($Name) { $procs | ForEach-Object { [pscustomobject]@{ Id = $_.ProcessId; Path = $_.ExecutablePath; ProcessName = 'python' } } }
function Stop-Process([int]$Id) { if ($Id) { $global:stopped += $Id }; foreach ($o in $input) { $global:stopped += $o.Id } }
"""


def _stopped_by(command: str, tmp_path: Path) -> list[int]:
    """command 를 가린 cmdlet 아래에서 돌려 '끄려 한' pid 목록."""
    out = tmp_path / "stopped.json"
    procs = "@(" + ", ".join(f"@({pid}, '{exe.replace(chr(39), chr(39) * 2)}', '{cmd.replace(chr(39), chr(39) * 2)}')"
                             for pid, exe, cmd in PROCS) + ")"
    script = (f"$PROCS = {procs}\n{_MOCKS}\n{command}\n"
              f"[IO.File]::WriteAllText('{out}', (ConvertTo-Json -InputObject @($global:stopped) -Compress))")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(script)],
                       capture_output=True, timeout=120)
    assert r.returncode == 0 and out.is_file(), r.stderr.decode("utf-8", "replace")
    return sorted(json.loads(out.read_text(encoding="utf-8-sig")))


def _install_stop_command(tmp_path: Path) -> str:
    """install.ps1 완료 안내의 「중지 :」 줄을 그 스크립트처럼 펼친다($Root = 가짜 설치 폴더)."""
    src = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    line = next(ln for ln in src.splitlines() if re.match(r'\s*Say "\s+중지 : ', ln))
    i = src.find("\n$rootLit = ")                                       # 안내 줄이 쓰는 준비 변수(없으면 옛 모양 — 그 줄만)
    pre = src[i:src.index('\nSay ""', i)] if i >= 0 else ""
    out = tmp_path / "said.txt"
    script = (f"$Root = '{APP}'\n$global:said = @()\nfunction Say($m) {{ $global:said += $m }}\n{pre}\n{line.strip()}\n"
              f"[IO.File]::WriteAllText('{out}', ($global:said -join \"`n\"), (New-Object System.Text.UTF8Encoding($false)))")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", ps_encoded(script)],
                       capture_output=True, timeout=60)
    assert r.returncode == 0 and out.is_file(), r.stderr.decode("utf-8", "replace")
    return out.read_text(encoding="utf-8").split("중지 :", 1)[1].strip()


def test_install_completion_stop_command_spares_the_broker(tmp_path):
    cmd = _install_stop_command(tmp_path)
    assert _stopped_by(cmd, tmp_path) == [101], cmd     # Host 만 — 브로커(102)·다른 파이썬(103 AppData 아래, 104)은 남긴다


def test_readme_stop_command_spares_the_broker(tmp_path):
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next(ln for ln in text.splitlines() if ln.startswith("| 지금 끄기 |"))
    cmd = re.search(r"`([^`]+)`", row).group(1).replace(r"\|", "|").replace("<설치폴더>", APP)
    assert _stopped_by(cmd, tmp_path) == [101], cmd


def test_stop_rule_is_the_update_runners():
    """같은 규칙 — 러너가 브로커를 고르는 글자(명령줄의 session_manager.ptyd)와 같다."""
    runner = (ROOT / "session_manager" / "update_runner.ps1").read_text(encoding="utf-8-sig")
    assert "'*session_manager.ptyd*'" in runner
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "session_manager.ptyd" in next(ln for ln in readme.splitlines() if ln.startswith("| 지금 끄기 |"))
    assert "'*session_manager.ptyd*'" in install[install.index("$stopCmd = "):install.index('Say "       중지 : $stopCmd"')]
