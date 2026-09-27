"""ops/release.ps1 계약 — 파싱 가능, 단계 순서, 토큰이 출력 경로에 안 실림, 안전 게이트."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "release.ps1"


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_step_order_and_gates():
    s = _src()
    order = ["pyproject.toml 버전", "git status --porcelain", "pytest -q", "package-for-user.ps1",
             "sign_release.py\") sign", "sign_release.py\") verify", "sign_release.py\") publish",
             "update/status?refresh=1", "update/apply", "/api/owner/diagnostics", "incidents $incBefore 유지"]
    pos = [s.index(k) for k in order]
    assert pos == sorted(pos), "단계 순서가 바뀌었습니다"
    assert "-Apply 는 -Publish 와 함께" in s                      # Apply 는 게시 없이는 불가
    assert "이미 존재: $zip" in s                                  # 같은 날 같은 버전 재패키징 금지
    assert "게시된 sha256 이 로컬 zip 과 다릅니다" in s            # 업로드 무결성


def test_token_never_printed():
    s = _src()
    # 토큰은 python 인자(--admin-token $tok)로만 쓰이고, Log/Write-Host 줄에는 등장하지 않는다
    for line in s.splitlines():
        if "$tok" in line and ("Log " in line or "Write-Host" in line):
            raise AssertionError(f"토큰이 출력 경로에 실립니다: {line.strip()}")
    assert re.search(r'-replace \[regex\]::Escape\(\$tok\), "<token>"', s)   # 게시 출력에서 마스킹
    assert "$tok = $null" in s


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh 없음")
def test_parses_and_refuses_bad_version():
    check = ("$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile("
             f"'{SCRIPT}', [ref]$t, [ref]$e) | Out-Null; if ($e.Count) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", check], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(SCRIPT), "-Version", "abc", "-SkipTests"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=ROOT)
    assert r.returncode == 1 and "버전 형식" in r.stdout
