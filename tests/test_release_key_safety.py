"""릴리스 서명 개인키가 새어나가지 않는지 — 장치가 살아있는지 검사한다.

개인키를 저장소 안(ops/release-keys/)에 두기로 했다. 프로젝트와 같이 다녀 찾기 쉽지만
그만큼 새어나갈 경로가 둘 생긴다:

    1) 사용자 배포 zip   -> package-for-user.ps1
    2) 소스 저장소       -> .gitignore

이 키가 새면 릴레이 토큰이 새는 것과는 급이 다르다. 사용자 PC 는 이 키로 서명된
매니페스트만 믿고 자기 코드를 갈아끼우므로, 키를 가진 쪽은 모든 설치본에서 임의
코드를 실행할 수 있다. 그래서 두 장치가 조용히 사라지지 않게 여기서 못박는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEY_DIR = ROOT / "ops" / "release-keys"
PKG = ROOT / "ops" / "package-for-user.ps1"
GITIGNORE = ROOT / ".gitignore"


def test_key_dir_is_not_in_package_whitelist():
    """화이트리스트 방식이라 애초에 안 담긴다 — 그 전제가 유지되는지 확인."""
    txt = PKG.read_text(encoding="utf-8")
    m = re.search(r'\$include = @\((.*?)\n\)', txt, re.S)
    assert m, "package-for-user.ps1 에서 $include 를 못 찾음"
    included = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert "release-keys" not in included
    assert not any(i.endswith(".pem") for i in included)


def test_key_dir_is_explicitly_excluded():
    """화이트리스트에 더해 명시적 제외도 있어야 한다(이중 방어)."""
    txt = PKG.read_text(encoding="utf-8")
    m = re.search(r'\$excludePatterns = @\((.*?)\n\)', txt, re.S)
    assert m, "excludePatterns 를 못 찾음"
    excluded = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert "release-keys" in excluded
    assert "*.pem" in excluded


def test_packager_aborts_on_key_file():
    """스테이지에 키가 섞이면 zip 을 만들지 말고 멈춰야 한다.

    문자열 스캔은 텍스트 확장자만 훑어서 .pem 을 지나치므로, 파일 자체를 보는
    독립된 검사가 반드시 있어야 한다.
    """
    txt = PKG.read_text(encoding="utf-8")
    assert "keyFiles" in txt, "키 파일 검사 블록이 사라졌다"
    block = txt[txt.index("$keyFiles"):]
    for ext in (".pem", ".key", ".pfx", ".p12"):
        assert f'"{ext}"' in block, f"{ext} 를 안 본다"
    assert "release-keys" in block
    # 발견 시 반드시 중단해야 한다 — 경고만 하고 넘어가면 의미가 없다
    assert re.search(r'키 파일이 패키지에 섞였습니다[\s\S]{0,600}?exit 1', block)


def test_pem_pattern_in_secret_scan():
    txt = PKG.read_text(encoding="utf-8")
    assert "PRIVATE KEY" in txt, "비밀 스캔에 PEM 개인키 패턴이 없다"


def test_secret_scan_covers_shell_scripts():
    """맥·리눅스 스크립트(.sh)도 패키지에 들어간다 — 비밀 스캔 확장자에 .sh 가 있어야 한다(S20)."""
    txt = PKG.read_text(encoding="utf-8")
    block = txt[txt.index("$leaks = @()"):]
    m = re.search(r'\$_\.Extension -in @\(([^)]*)\)', block)
    assert m, "비밀 스캔의 확장자 목록을 못 찾음"
    exts = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert {".sh", ".py", ".ps1", ".toml"} <= exts, exts


def test_generated_posix_files_are_excluded_but_the_template_is_not():
    """설치본이 만드는 런처(start-connector.sh)·설치 기록(.clewpath-install)은 빠지고, 원본 템플릿은 들어간다.
    제외는 부분일치(-like "*패턴*")라 템플릿 이름이 걸리지 않는지까지 본다."""
    import fnmatch
    txt = PKG.read_text(encoding="utf-8")
    m = re.search(r'\$excludePatterns = @\((.*?)\n\)', txt, re.S)
    excluded = re.findall(r'"([^"]+)"', m.group(1))
    assert "start-connector.sh" in excluded and ".clewpath-install" in excluded

    def dropped(rel: str) -> bool:                    # PowerShell -like 는 대소문자를 가리지 않는다
        return any(fnmatch.fnmatch(rel.lower(), f"*{p.lower()}*") for p in excluded)
    assert dropped("session_manager\\start-connector.sh") and dropped(".clewpath-install")
    for keep in ("session_manager\\start-connector.template.sh", "session_manager\\update_runner.sh",
                 "session_manager\\hostctl.py", "install.sh", "uninstall.sh"):
        assert not dropped(keep), keep


def test_shell_script_format_gate_aborts():
    """패키지의 .sh 는 LF·BOM 없음·1행 #!/bin/sh 여야 한다 — 편집기가 CRLF 로 저장해도 eol=lf 정규화 때문에 git 은
    깨끗해 보인다. 어긋나면 zip 을 만들지 않고 멈춘다(실행 시험은 tests/test_package_for_user.py)."""
    txt = PKG.read_text(encoding="utf-8")
    block = txt[txt.index("$badSh = @()"):]
    for needle in ("0xEF", "0xBB", "0xBF", "[byte]13", '"#!/bin/sh"', '-eq ".sh"'):
        assert needle in block, needle
    assert re.search(r'셸 스크립트 형식이 어긋났습니다[\s\S]{0,400}?exit 1', block)
    assert txt.index("$badSh = @()") < txt.index("Compress-Archive"), "zip 을 만들기 전에"


def test_gitignore_covers_key():
    assert GITIGNORE.is_file(), ".gitignore 가 없다"
    lines = {ln.strip() for ln in GITIGNORE.read_text(encoding="utf-8").splitlines()}
    assert "release-keys/" in lines
    assert "*.pem" in lines


def test_no_private_key_outside_key_dir():
    """저장소 어딘가에 개인키가 굴러다니면 잡는다(복사해두고 잊는 사고)."""
    strays = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".pem", ".key", ".pfx", ".p12"):
            continue
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("ops/release-keys/", ".venv/", "build/", "ops/dist/")):
            continue
        strays.append(rel)
    assert not strays, f"키 파일이 엉뚱한 곳에 있다: {strays}"


@pytest.mark.skipif(not KEY_DIR.is_dir(), reason="이 PC 에 릴리스 키가 없음(운영자 PC 전용)")
def test_shipped_public_key_matches_private_key():
    """updater.RELEASE_KEYS 에 박힌 공개키가 실제 개인키와 짝인지.

    어긋나면 서명은 성공하는데 배포본이 전부 거부한다 — 릴리스한 뒤에야 안다.
    """
    priv_file = KEY_DIR / "private.pem"
    if not priv_file.is_file():
        pytest.skip("private.pem 없음")
    pytest.importorskip("cryptography")
    import base64
    import hashlib

    from cryptography.hazmat.primitives import serialization

    from session_manager import updater

    priv = serialization.load_pem_private_key(priv_file.read_bytes(), password=None)
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)
    kid = hashlib.sha256(raw).hexdigest()[:16]
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert kid in updater.RELEASE_KEYS, (
        f"개인키의 kid({kid}) 가 RELEASE_KEYS 에 없다 - 서명해도 아무도 못 받는다")
    assert updater.RELEASE_KEYS[kid] == b64, "같은 kid 인데 공개키 값이 다르다"
