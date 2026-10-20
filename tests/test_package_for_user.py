"""ops/package-for-user.ps1 실행 시험 — 화이트리스트(맥·리눅스 스크립트 포함)·생성물 제외·.sh 형식 게이트·.sh 비밀 스캔.

가짜 저장소(tmp)에 스크립트 사본을 두고 진짜로 돌린다($Root = ops 의 부모라 그 저장소를 담는다). 스테이지(%TEMP%)도
tmp 로 돌려 이 PC 의 임시 폴더를 쓰지 않는다. pwsh 가 없으면(리눅스 컨테이너) 건너뛴다 — 정적 계약은
tests/test_release_key_safety.py, 실제 작업 트리의 드라이런은 단계 기록에 있다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh 없음(Windows 릴리스 도구)")

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "ops" / "package-for-user.ps1"
SH = b"#!/bin/sh\nprintf '%s\\n' ok\n"

FILES = {
    "session_manager/__init__.py": b"",
    "session_manager/hostctl.py": b"",
    "session_manager/update_runner.sh": SH,
    "session_manager/start-connector.template.sh": b"#!/bin/sh\n# clewpath-launcher v2 (posix)\n",
    "session_manager/skills/clewpath-workers/SKILL.md": b"x\n",
    "pwa/index.html": b"<!doctype html>\n",
    "pyproject.toml": b'[project]\nname = "clewpath-host"\nversion = "1.2.3"\n',
    "uv.lock": b"version = 1\n",
    "install.ps1": b"# install\r\n",
    "install.sh": SH,
    "uninstall.sh": SH,
    "README.md": b"# ClewPath\n",
    "LICENSE": b"MIT\n",
    # 담기면 안 되는 것 — 설치본의 생성물(렌더한 런처·설치 기록)과 운영자 비밀 기동 스크립트
    "session_manager/start-connector.sh": SH,
    "session_manager/start-connector.ps1": b"$env:SM_PASSWORD = 'x'\r\n",
    "start-connector.sh": SH,
    ".clewpath-install": b"home=/x\n",
}


def _repo(tmp_path: Path, **over: bytes) -> Path:
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    shutil.copyfile(PKG, repo / "ops" / "package-for-user.ps1")
    for rel, data in {**FILES, **over}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return repo


def _pack(tmp_path: Path, repo: Path) -> tuple[subprocess.CompletedProcess, list[Path]]:
    out, temp = tmp_path / "out", tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    env = {**os.environ, "TEMP": str(temp), "TMP": str(temp)}
    r = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "package-for-user.ps1"),
                        "-OutDir", str(out), "-Force"], env=env, capture_output=True, encoding="utf-8",
                       errors="replace", timeout=180)
    assert not list(temp.iterdir()), "스테이지는 성공·실패 모두 치운다"
    return r, sorted(out.glob("clewpath-host-*.zip")) if out.is_dir() else []


def test_packs_posix_scripts_and_leaves_generated_files_out(tmp_path):
    r, zips = _pack(tmp_path, _repo(tmp_path))
    assert r.returncode == 0 and "셸 스크립트 형식 통과" in r.stdout, r.stdout + r.stderr
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as z:
        names = [i.orig_filename for i in z.infolist()]
        files = {n for n in names if not n.endswith("/")}
        assert files == {n for n in FILES if n not in ("session_manager/start-connector.sh",
                                                         "session_manager/start-connector.ps1",
                                                         "start-connector.sh", ".clewpath-install")}, sorted(files)
        assert not [n for n in names if "\\" in n], "항목 이름의 구분자는 '/'(맥·리눅스가 그대로 푼다)"
        for n in ("install.sh", "uninstall.sh", "session_manager/update_runner.sh"):
            assert z.read(n) == FILES[n], f"{n} 는 바이트 그대로(LF)"


@pytest.mark.parametrize("name,data,why", [
    ("install.sh", SH.replace(b"\n", b"\r\n"), "(CR - 줄끝이 CRLF)"),
    ("uninstall.sh", b"\xef\xbb\xbf" + SH, "(BOM)"),
    ("session_manager/update_runner.sh", b"#!/bin/bash\necho x\n", "(1행이 #!/bin/sh 가 아님)"),
    ("session_manager/start-connector.template.sh", b"# no shebang\n", "(1행이 #!/bin/sh 가 아님)"),
])
def test_shell_format_gate_stops_packaging(tmp_path, name, data, why):
    r, zips = _pack(tmp_path, _repo(tmp_path, **{name: data}))
    assert r.returncode == 1 and "셸 스크립트 형식이 어긋났습니다" in r.stdout, r.stdout + r.stderr
    assert f"{name.replace('/', chr(92))} {why}" in r.stdout, r.stdout     # 파일마다 이유 한 줄
    assert zips == [], "zip 을 만들지 않는다"


def test_secret_scan_covers_shell_scripts(tmp_path):
    leaked = SH + b"TOKEN=rly-agt-ABCDEFGHIJKLMNOPQRSTUV\n"
    r, zips = _pack(tmp_path, _repo(tmp_path, **{"install.sh": leaked}))
    assert r.returncode == 1 and "비밀로 보이는 문자열" in r.stdout and "install.sh" in r.stdout, r.stdout + r.stderr
    assert zips == []
