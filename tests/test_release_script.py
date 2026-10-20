"""ops/release.ps1 계약 — 파싱 가능, 단계 순서, 토큰이 출력 경로에 안 실림, 안전 게이트.

받는 OS(-Platforms)·zip 내용 게이트(docs/designs/posix-install.md 4.5·4.6)는 스크립트에서 함수(Resolve-Platforms·
Test-ReleaseZip)만 꺼내(AST) pwsh 로 시험한다 — 스크립트 본문(사전점검·서명·게시)은 돌리지 않는다.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ops" / "release.ps1"
need_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh 없음")


def _src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_step_order_and_gates():
    s = _src()
    order = ["Resolve-Platforms $Platforms", "pyproject.toml 버전", "git status --porcelain", "posix-e2e-$Version.json",
             "pytest -q", "package-for-user.ps1", "Test-ReleaseZip $zip $Root",
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


# ---- 받는 OS(-Platforms) · zip 내용 게이트 (posix-install.md 4.5·4.6) ----

def test_platforms_default_to_windows_and_reach_the_manifest():
    """기본은 win(이번 출시는 맥·리눅스를 시험장에서만). 서명에 넘기고, verify 가 찍는 platforms= 줄과 대조하고, 로그에 남긴다."""
    s = _src()
    assert re.search(r'\[string\[\]\]\$Platforms = @\("win"\)', s)
    sign = s[s.index('sign_release.py") sign'):]
    assert "--platforms $plats" in sign[:sign.index("2>&1")], "서명에 받는 OS 를 넘긴다"
    assert '"(?m)^platforms=$([regex]::Escape($plats))\\s*$"' in s, "verify 출력의 platforms= 와 대조"
    assert 'Log "받는 OS(platforms): $plats"' in s
    # 맥·리눅스가 받는 릴리스는 같은 HEAD 에서 통과한 컨테이너 왕복 기록이 있어야 — 기록은 .gitignore(트리 깨끗 게이트)
    gate = s[s.index('if ($plats -ne "win")'):s.index("uv.lock 이 pyproject.toml 과 어긋납니다")]
    for needle in ("posix-e2e-$Version.json", "git rev-parse HEAD", "$rec.ok -ne $true", "$rec.head", "$rec.version", "Fail "):
        assert needle in gate, needle
    lines = {ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "ops/logs/posix-e2e-*.json" in lines


def test_zip_gate_checks_posix_files_separator_and_keys():
    s = _src()
    fn = s[s.index("function Test-ReleaseZip"):s.index('Log "=== release $Version 시작')]
    for need in _REQUIRED:
        assert f'"{need}"' in fn, need
    assert "Contains('\\')" in fn and "-cnotcontains" in fn
    assert "Get-ReleaseKids" in fn and "session_manager/updater.py" in fn and "시험 빌드는 게시하지 않습니다" in fn
    assert re.search(r'\$zipProblems\.Count\) \{ Fail', s), "문제가 하나라도 있으면 중단"


def _pwsh_fn(body: str, **env: str) -> subprocess.CompletedProcess:
    """release.ps1 에서 함수 정의만 꺼내(AST) 정의한 뒤 body 를 돌린다."""
    head = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:REL_PS1, [ref]$null, [ref]$null)\n"
            "$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |"
            " Where-Object { $_.Name -in @('Resolve-Platforms', 'Get-ReleaseKids', 'Test-ReleaseZip') } |"
            " ForEach-Object { Invoke-Expression $_.Extent.Text }\n")
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", head + body],
                          env={**os.environ, "REL_PS1": str(SCRIPT), **env}, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=120)


@need_pwsh
def test_resolve_platforms():
    cases = {"win": "ok:win", "win,mac": "ok:win,mac", "linux win": "ok:win,linux", "WIN, Mac,mac": "ok:win,mac",
             "bsd": "err:모르는 플랫폼", "win,macos": "err:모르는 플랫폼", "mac": "err:-Platforms 에 win 이 없습니다",
             "": "err:-Platforms 에 win 이 없습니다"}
    body = ("$cases = $env:CASES | ConvertFrom-Json\n"
            "foreach ($c in $cases) { try { 'ok:' + (Resolve-Platforms @($c)) } catch { 'err:' + $_.Exception.Message } }\n")
    r = _pwsh_fn(body, CASES=json.dumps(list(cases)))
    assert r.returncode == 0, r.stdout + r.stderr
    got = r.stdout.strip().splitlines()
    assert len(got) == len(cases), got
    for (arg, want), line in zip(cases.items(), got):
        assert line.startswith(want), (arg, line)
    r = _pwsh_fn("'ok:' + (Resolve-Platforms @('linux', 'win'))\n")    # PowerShell 안에서 배열로 넘긴 경우(-Platforms linux,win)
    assert r.stdout.strip() == "ok:win,linux", r.stdout + r.stderr


_REQUIRED = ("install.sh", "uninstall.sh", "session_manager/update_runner.sh", "session_manager/start-connector.template.sh",
             "session_manager/hostctl.py")


def _release_zip(path: Path, *, drop=(), extra: dict | None = None, updater_text: str | None = None) -> Path:
    upd = (ROOT / "session_manager" / "updater.py").read_text(encoding="utf-8") if updater_text is None else updater_text
    files = {n: "#!/bin/sh\n" if n.endswith(".sh") else "" for n in _REQUIRED}
    files.update({"session_manager/updater.py": upd, "pyproject.toml": '[project]\nversion = "1.2.3"\n'})
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if name in drop:
                continue
            zi = zipfile.ZipInfo("x")
            zi.filename = name                   # ZipInfo() 는 Windows 에서 '\' 를 '/' 로 바꾼다 — 날것 그대로 적는다
            z.writestr(zi, data)
    return path


def _swap_kid(text: str, new_kid: str) -> str:
    m = re.search(r"(?ms)^RELEASE_KEYS\b.*?^\}", text)
    block = re.sub(r'"[0-9a-f]{16}"(\s*:)', f'"{new_kid}"\\1', m.group(0), count=1)
    return text[:m.start()] + block + text[m.end():]


@need_pwsh
def test_zip_gate_on_crafted_zips(tmp_path):
    """맥·리눅스 스크립트 다섯이 있고, 이름 구분자는 '/', 배포본이 믿는 서명 키(kid 집합)는 저장소와 같아야 게시한다 —
    시험 키를 심은 시험 빌드(S46)·Windows PowerShell 5.1 의 Compress-Archive(역슬래시 이름)를 막는다."""
    upd = (ROOT / "session_manager" / "updater.py").read_text(encoding="utf-8")
    kid = re.search(r'(?ms)^RELEASE_KEYS\b.*?"([0-9a-f]{16})"', upd).group(1)
    commented = upd.replace(f'    "{kid}":', f'    # "0123456789abcdef": "옛 키 — 주석은 세지 않는다",\n    "{kid}":', 1)
    extra_key = upd.replace(f'    "{kid}":', '    "' + "e" * 16 + '": "AAAA",\n    "' + kid + '":', 1)
    zips = {
        "good": _release_zip(tmp_path / "good.zip"),
        "good-with-commented-old-key": _release_zip(tmp_path / "c.zip", updater_text=commented),
        "no-install-sh": _release_zip(tmp_path / "a.zip", drop=("install.sh",)),
        "no-hostctl": _release_zip(tmp_path / "b.zip", drop=("session_manager/hostctl.py",)),
        "backslash": _release_zip(tmp_path / "d.zip", extra={"session_manager\\x.py": ""}),
        "test-key": _release_zip(tmp_path / "e.zip", updater_text=_swap_kid(upd, "f" * 16)),
        "extra-key": _release_zip(tmp_path / "f.zip", updater_text=extra_key),
        "no-keys": _release_zip(tmp_path / "g.zip", updater_text="RELEASE_KEYS: dict[str, str] = {\n}\n"),
        "no-updater": _release_zip(tmp_path / "h.zip", drop=("session_manager/updater.py",)),
    }
    body = ("$zips = $env:ZIPS | ConvertFrom-Json\n"
            "foreach ($p in $zips.PSObject.Properties) { $pr = @(Test-ReleaseZip $p.Value $env:REPO);"
            " $p.Name + '|' + $pr.Count + '|' + ($pr -join ' // ') }\n")
    r = _pwsh_fn(body, ZIPS=json.dumps({k: str(v) for k, v in zips.items()}), REPO=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
    got = {ln.split("|", 2)[0]: ln.split("|", 2)[1:] for ln in r.stdout.strip().splitlines()}
    assert set(got) == set(zips), r.stdout
    assert got["good"] == ["0", ""] and got["good-with-commented-old-key"] == ["0", ""], got
    for name, said in (("no-install-sh", "install.sh"), ("no-hostctl", "session_manager/hostctl.py"),
                       ("backslash", "역슬래시"), ("test-key", "시험 빌드는 게시하지 않습니다"),
                       ("extra-key", "시험 빌드는 게시하지 않습니다"), ("no-keys", "RELEASE_KEYS 를 읽지 못했습니다"),
                       ("no-updater", "session_manager/updater.py 가 없습니다")):
        assert got[name][0] == "1" and said in got[name][1], (name, got[name])


@need_pwsh
@pytest.mark.parametrize("plats,said", [("bsd", "모르는 플랫폼"), ("mac,linux", "win 이 없습니다")])
def test_refuses_bad_platforms_before_anything_else(plats, said):
    """받는 OS 는 버전 형식 바로 다음에 본다 — CHANGELOG·트리·키·테스트보다 먼저(아무것도 만들지 않고 끝난다)."""
    r = subprocess.run(["pwsh", "-NoProfile", "-File", str(SCRIPT), "-Version", "0.0.1", "-Platforms", plats, "-SkipTests"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, cwd=ROOT)
    assert r.returncode == 1 and said in r.stdout, r.stdout + r.stderr
    assert "CHANGELOG" not in r.stdout
