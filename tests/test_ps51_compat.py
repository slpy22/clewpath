"""PowerShell 5.1 호환 회귀 — 사용자 PC 의 기본 셸에서 깨지지 않는지.

install.ps1 과 update_runner.ps1 은 pwsh(7)가 없는 PC 에서 powershell.exe(5.1)로
돈다. 5.1 에서 깨지는 지점은 세 종류이고, 각각을 못박는다:

  1) PS7 전용 문법 (?.  ??  && ||) — 5.1 에선 파스 에러라 스크립트가 아예 안 뜬다.
  2) BOM 없는 UTF-8 — 5.1 은 ANSI(cp949)로 읽어 한글 주석·문자열이 전부 깨진다.
  3) 반대 방향: 5.1 의 Set-Content -Encoding UTF8 은 BOM 을 붙이는데, 파이썬이
     순수 utf-8 로 읽으면 tomllib/json 이 거부한다 → 설정·자격증명이 조용히 무시됨.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
USER_SIDE_PS1 = [
    ROOT / "install.ps1",
    ROOT / "session_manager" / "update_runner.ps1",
]

# 5.1 이 파스 에러를 내는 PS7 전용 토큰. 주석은 제외하고 코드 라인만 본다.
_FORBIDDEN = [
    (re.compile(r"\)\s*\?\."), "?. (null-조건 연산자)"),
    (re.compile(r"\?\?"), "?? (null-병합 연산자)"),
    (re.compile(r"&&"), "&& (파이프라인 체이닝)"),
    (re.compile(r"(?<!\|)\|\|(?!\|)"), "|| (파이프라인 체이닝)"),
]


def _code_lines(path: Path):
    """주석을 벗겨낸 코드 부분만 돌려준다. '#' 이후는 버린다 —
    금지 토큰이 # 뒤에 오는 경우는 코드가 아니므로 오탐만 줄어든다."""
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        yield i, line.split("#", 1)[0]


@pytest.mark.parametrize("ps1", USER_SIDE_PS1, ids=lambda p: p.name)
def test_no_ps7_only_syntax(ps1):
    hits = []
    for lineno, code in _code_lines(ps1):
        for rx, label in _FORBIDDEN:
            if rx.search(code):
                hits.append(f"{ps1.name}:{lineno} {label}: {code.strip()[:60]}")
    assert not hits, "PS 5.1 파스 에러 유발:\n  " + "\n  ".join(hits)


@pytest.mark.parametrize("ps1", USER_SIDE_PS1, ids=lambda p: p.name)
def test_utf8_bom_present(ps1):
    """BOM 이 없으면 5.1 이 이 파일을 cp949 로 읽어 한글이 전부 깨진다."""
    assert ps1.read_bytes().startswith(b"\xef\xbb\xbf"), f"{ps1.name} 에 UTF-8 BOM 없음"


@pytest.mark.parametrize("ps1", USER_SIDE_PS1, ids=lambda p: p.name)
@pytest.mark.skipif(not shutil.which("powershell"), reason="powershell.exe 없음")
def test_parses_under_real_ps51(ps1):
    """정적 스캔이 놓치는 문법(삼항 등)은 진짜 5.1 파서에게 물어본다."""
    cmd = (
        "$e = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1}', [ref]$null, [ref]$e); "
        "if ($e.Count) { $e | ForEach-Object { Write-Output ('L' + $_.Extent.StartLineNumber + ': ' + $_.Message) }; exit 1 }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"PS 5.1 파스 실패:\n{r.stdout}\n{r.stderr}"


# ---- 방향 반대: 5.1 이 쓴 파일(BOM 포함)을 파이썬이 읽는 쪽 ----

def test_appconfig_reads_bom_toml(tmp_path, monkeypatch):
    """5.1 의 Set-Content -Encoding UTF8 이 만든 설정 파일(BOM)이 무시되면 안 된다."""
    from session_manager import appconfig
    p = tmp_path / "clewpath.toml"
    p.write_bytes(b"\xef\xbb\xbf" + b'[server]\nport = 6200\n')
    monkeypatch.setenv("CLEWPATH_CONFIG", str(p))
    monkeypatch.delenv("SM_PORT", raising=False)
    appconfig.load(refresh=True)
    try:
        assert appconfig.get_int("server", "port", 5100) == 6200
    finally:
        appconfig.load(refresh=True)


def test_cp_client_reads_bom_credentials(tmp_path, monkeypatch):
    """install.ps1(5.1) 이 발급받아 저장한 자격증명(BOM)이 무시되면
    커넥터가 조용히 레거시 모드로 떨어진다."""
    from session_manager import cp_client
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    f = tmp_path / "session_manager" / "cp_credentials.json"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"\xef\xbb\xbf" + b'{"connector_id": "c_1", "room_key": "rm_x"}')
    creds = cp_client.load_creds()
    assert creds and creds["connector_id"] == "c_1"
