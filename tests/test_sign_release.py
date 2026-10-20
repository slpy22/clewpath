"""ops/tools/sign_release.py 의 받는 OS(platforms) — docs/designs/posix-install.md 4.5·4.6.

매니페스트의 platforms 클레임이 '어느 OS 의 Host 가 설치·업데이트를 받나'를 정한다(updater.supports_this_os·get.sh 가 읽는다).
오타 하나가 그 OS 사용자 전체의 업데이트를 끊으므로 sign 은 모르는 이름을 거부하고, verify 는 모양을 다시 보고 한 줄로 알린다.

시험 키는 프로세스 안에서 만들어 tmp 에만 둔다 — 운영 키 폴더(ops/release-keys)는 읽지 않고 keygen 도 부르지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from session_manager import updater  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIGN = ROOT / "ops" / "tools" / "sign_release.py"


def _module():
    spec = importlib.util.spec_from_file_location("sign_release_under_test", SIGN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def key(tmp_path) -> tuple[Path, str, str]:
    """시험 키(tmp) — (pem 경로, kid, 공개키 b64url)."""
    priv = Ed25519PrivateKey.generate()
    pem = tmp_path / "test-key.pem"
    pem.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, hashlib.sha256(raw).hexdigest()[:16], base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _zip(tmp_path: Path, ver: str = "1.2.3") -> Path:
    z = tmp_path / f"clewpath-host-{ver}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as f:
        f.writestr("pyproject.toml", f'[project]\nname = "clewpath-host"\nversion = "{ver}"\n')
        f.writestr("session_manager/__init__.py", "")
    z.write_bytes(buf.getvalue())
    return z


def _sign(tmp_path: Path, pem: Path, *extra: str) -> subprocess.CompletedProcess:
    out = tmp_path / "m.jwt"
    return subprocess.run([sys.executable, str(SIGN), "sign", "--key", str(pem), "--version", "1.2.3",
                           "--artifact", str(_zip(tmp_path)), "--url", "https://example.invalid/d/1.2.3",
                           "--out", str(out), "--notes", "한글 노트", *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def _claims(tmp_path: Path) -> dict:
    return pyjwt.decode((tmp_path / "m.jwt").read_text(encoding="utf-8"), options={"verify_signature": False})


def test_sign_defaults_to_windows_only(tmp_path, key):
    """이번 출시(posix-install)는 win 만 — 맥·리눅스는 시험장에서만(결정 Q4)."""
    r = _sign(tmp_path, key[0])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _claims(tmp_path)["platforms"] == ["win"]
    assert "platforms=win" in r.stdout and "ver=1.2.3" in r.stdout      # release.ps1 이 'ver=' 줄을 로그에 남긴다


@pytest.mark.parametrize("arg,want", [
    ("win,mac,linux", ["win", "mac", "linux"]),
    ("linux,win", ["win", "linux"]),                    # 순서는 정해진 대로(매니페스트 모양이 늘 같게)
    ("mac linux,WIN,mac", ["win", "mac", "linux"]),     # 공백·대문자·겹침
])
def test_sign_writes_the_platforms_claim(tmp_path, key, arg, want):
    r = _sign(tmp_path, key[0], "--platforms", arg)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _claims(tmp_path)["platforms"] == want and f"platforms={','.join(want)}" in r.stdout


@pytest.mark.parametrize("arg", ["bsd", "win,macos", "", " , "])
def test_sign_refuses_unknown_or_empty_platforms(tmp_path, key, arg):
    r = _sign(tmp_path, key[0], "--platforms", arg)
    assert r.returncode == 2, r.stdout + r.stderr                       # argparse 사용법 오류 — 서명 전에
    assert "플랫폼" in r.stderr and not (tmp_path / "m.jwt").exists()


def test_parse_platforms_is_the_osdep_vocabulary():
    """값은 osdep.NAME 과 같은 낱말(win·mac·linux) — Host 가 자기 이름을 클레임에서 찾는다."""
    from session_manager import osdep
    sr = _module()
    assert set(sr.PLATFORMS) == {"win", "mac", "linux"} and osdep.NAME in sr.PLATFORMS
    assert sr.DEFAULT_PLATFORMS == "win"
    with pytest.raises(argparse.ArgumentTypeError):
        sr.parse_platforms("windows")


def _token(pem: Path, kid: str, **claims) -> str:
    base = {"iss": updater.ISSUER, "aud": updater.AUDIENCE, "ver": "1.2.3", "url": "https://example.invalid/d",
            "sha256": "a" * 64, "size": 10, "iat": 1, "exp": 4_000_000_000}
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    return pyjwt.encode(base, pem.read_bytes(), algorithm="EdDSA", headers={"kid": kid})


@pytest.mark.parametrize("plats,rc,line", [
    (["win"], 0, "platforms=win"),
    (["win", "mac", "linux"], 0, "platforms=win,mac,linux"),
    (None, 0, "platforms=(클레임 없음 — win 전용으로 읽힌다)"),           # 0.28 까지의 매니페스트
    (["win", 1], 1, "[FAIL] platforms 클레임 모양이 이상합니다"),
    ("win", 1, "[FAIL] platforms 클레임 모양이 이상합니다"),
    (["win", "bsd"], 1, "[FAIL] platforms 클레임 모양이 이상합니다"),
    ([], 1, "[FAIL] platforms 클레임 모양이 이상합니다"),
])
def test_verify_reports_platforms(tmp_path, key, monkeypatch, capsys, plats, rc, line):
    """verify 는 배포본(updater)이 받아들이는지 본 뒤 받는 OS 를 한 줄로 — release.ps1 이 그 줄을 -Platforms 와 대조한다."""
    pem, kid, b64 = key
    monkeypatch.setattr(updater, "RELEASE_KEYS", {kid: b64})
    m = tmp_path / "m.jwt"
    m.write_text(_token(pem, kid, platforms=plats), encoding="utf-8")
    sr = _module()
    assert sr.cmd_verify(argparse.Namespace(manifest=str(m))) == rc
    out = capsys.readouterr().out
    assert line in out.splitlines() if rc == 0 else line in out
    assert ("[OK] 검증 통과" in out) is (rc == 0), "모양이 이상하면 OK 를 찍지 않는다(release.ps1 은 OK 를 본다)"


def test_signed_manifest_is_accepted_by_this_os_rules(tmp_path, key, monkeypatch):
    """sign 이 쓴 클레임을 updater 가 같은 뜻으로 읽는다 — 기본(win)은 맥·리눅스 Host 가 받지 않는다."""
    from session_manager import osdep
    pem, kid, b64 = key
    monkeypatch.setattr(updater, "RELEASE_KEYS", {kid: b64})
    assert _sign(tmp_path, pem).returncode == 0
    m = updater.verify_manifest((tmp_path / "m.jwt").read_text(encoding="utf-8"))
    for name, ok in (("win", True), ("mac", False), ("linux", False)):
        monkeypatch.setattr(osdep, "NAME", name)
        assert updater.supports_this_os(m) is ok, name
    assert _sign(tmp_path, pem, "--platforms", "win,linux").returncode == 0
    m = updater.verify_manifest((tmp_path / "m.jwt").read_text(encoding="utf-8"))
    monkeypatch.setattr(osdep, "NAME", "linux")
    assert updater.supports_this_os(m) is True


def test_keygen_default_is_the_operator_key_dir():
    """keygen 의 기본 출력은 운영 키 폴더다(명세 0절 4) — 그래서 시험·하네스는 keygen 을 부르지 않고 키를 프로세스 안에서
    만들어 sign --key 로만 넘긴다. 이 기본값이 바뀌면 그 전제가 바뀐 것이다."""
    sr = _module()
    assert sr.DEFAULT_KEY_DIR == ROOT / "ops" / "release-keys" and sr.DEFAULT_KEY == sr.DEFAULT_KEY_DIR / "private.pem"
