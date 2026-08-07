"""자동 업데이트 회귀 — 서명 검증과 아티팩트 검사.

업데이트는 사용자 PC 에서 임의 코드를 실행시키는 통로다. 여기 있는 검사가
하나라도 조용히 사라지면 그 통로가 그대로 열린다. 그래서 "통과한다" 뿐 아니라
**변조하면 반드시 거부한다** 를 같이 못박는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import zipfile

import pytest

pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from session_manager import updater  # noqa: E402


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kid = hashlib.sha256(raw).hexdigest()[:16]
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    pem = priv.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    return pem, kid, b64


def _sign(pem, kid, **over):
    now = int(time.time())
    claims = {"iss": updater.ISSUER, "aud": updater.AUDIENCE, "ver": "9.9.9",
              "url": "https://example.invalid/a.zip", "sha256": "a" * 64,
              "size": 123, "iat": now, "exp": now + 3600}
    claims.update(over)
    return pyjwt.encode(claims, pem, algorithm="EdDSA", headers={"kid": kid})


@pytest.fixture
def signer(monkeypatch):
    pem, kid, b64 = _keypair()
    monkeypatch.setattr(updater, "RELEASE_KEYS", {kid: b64})
    return pem, kid


# ---- 정상 ----

def test_valid_manifest_accepted(signer):
    pem, kid = signer
    m = updater.verify_manifest(_sign(pem, kid))
    assert m["ver"] == "9.9.9" and m["size"] == 123


def test_shipped_key_is_configured():
    """배포본에 공개키가 실제로 박혀 있어야 한다 — 없으면 업데이트가 죽은 기능이다."""
    assert updater.RELEASE_KEYS, "RELEASE_KEYS 가 비어 있음"
    for kid, b64 in updater.RELEASE_KEYS.items():
        assert len(kid) == 16
        pad = "=" * (-len(b64) % 4)
        assert len(base64.urlsafe_b64decode(b64 + pad)) == 32   # Ed25519 공개키 길이


# ---- 거부해야 하는 것들 ----

def test_unknown_key_rejected(signer, monkeypatch):
    pem, kid = signer
    tok = _sign(pem, kid)
    monkeypatch.setattr(updater, "RELEASE_KEYS", {"0" * 16: "x" * 43})
    with pytest.raises(updater.UpdateError, match="모르는 서명키"):
        updater.verify_manifest(tok)


def test_signature_from_other_key_rejected(signer):
    """다른 키로 서명한 매니페스트를 우리 kid 로 위장해도 거부돼야 한다."""
    _, kid = signer
    other_pem, _, _ = _keypair()
    with pytest.raises(updater.UpdateError, match="서명 검증 실패"):
        updater.verify_manifest(_sign(other_pem, kid))


def test_tampered_payload_rejected(signer):
    pem, kid = signer
    tok = _sign(pem, kid)
    head, payload, sig = tok.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["url"] = "https://evil.invalid/x.zip"          # 다운로드 주소만 바꿔치기
    new = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(f"{head}.{new}.{sig}")


def test_alg_none_rejected(signer):
    """alg=none 은 고전적인 우회 수법 — algorithms 를 못박았는지 확인한다."""
    _, kid = signer
    body = {"iss": updater.ISSUER, "aud": updater.AUDIENCE, "ver": "9.9.9",
            "url": "u", "sha256": "a" * 64, "size": 1,
            "iat": int(time.time()), "exp": int(time.time()) + 60}
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    tok = f'{b64({"alg": "none", "kid": kid, "typ": "JWT"})}.{b64(body)}.'
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(tok)


def test_expired_manifest_rejected(signer):
    pem, kid = signer
    now = int(time.time())
    with pytest.raises(updater.UpdateError, match="서명 검증 실패"):
        updater.verify_manifest(_sign(pem, kid, iat=now - 7200, exp=now - 3600))


def test_wrong_audience_rejected(signer):
    pem, kid = signer
    with pytest.raises(updater.UpdateError, match="서명 검증 실패"):
        updater.verify_manifest(_sign(pem, kid, aud="someone-else"))


def test_missing_required_claim_rejected(signer):
    pem, kid = signer
    with pytest.raises(updater.UpdateError, match="sha256"):
        updater.verify_manifest(_sign(pem, kid, sha256=""))


def test_no_keys_means_no_update(monkeypatch):
    monkeypatch.setattr(updater, "RELEASE_KEYS", {})
    with pytest.raises(updater.UpdateError, match="공개키가 없습니다"):
        updater.verify_manifest("a.b.c")


# ---- 버전 비교 ----

@pytest.mark.parametrize("cand,cur,expect", [
    ("0.4.0", "0.3.0", True),
    ("0.3.1", "0.3.0", True),
    ("0.3.0", "0.3.0", False),
    ("0.2.9", "0.3.0", False),
    ("0.10.0", "0.9.0", True),      # 문자열 비교였다면 뒤집힌다
    ("1.0.0", "0.99.99", True),
])
def test_version_compare(cand, cur, expect):
    assert updater.is_newer(cand, cur) is expect


# ---- 아티팩트 검사 ----

def test_stage_rejects_zip_slip(tmp_path, monkeypatch):
    """zip 안의 ../ 경로로 설치 폴더 밖을 덮어쓰려는 시도를 막는다."""
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("session_manager/__init__.py", "")
        f.writestr("../../pwned.txt", "x")
    with pytest.raises(updater.UpdateError, match="스테이지 밖"):
        updater.stage(z, {"ver": "9.9.9"})


def test_stage_rejects_zip_without_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    z = tmp_path / "wrong.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("readme.txt", "not our app")
    with pytest.raises(updater.UpdateError, match="session_manager 가 없습니다"):
        updater.stage(z, {"ver": "9.9.9"})


def test_stage_drops_unlisted_entries(tmp_path, monkeypatch):
    """PAYLOAD 에 없는 것은 설치 폴더로 갈 기회조차 없어야 한다."""
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    z = tmp_path / "extra.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("session_manager/__init__.py", "")
        f.writestr("start-connector.ps1", "악성")      # 설정/기동 스크립트 덮어쓰기 시도
        f.writestr("evil/backdoor.py", "x")
    out = updater.stage(z, {"ver": "9.9.9"})
    names = {p.name for p in out.iterdir()}
    assert "session_manager" in names
    assert "start-connector.ps1" not in names and "evil" not in names


def test_payload_matches_runner_script():
    """updater.py 의 PAYLOAD 와 update_runner.ps1 의 $Payload 가 어긋나면
    교체되는 것과 백업되는 것이 달라져 롤백이 반쪽이 된다."""
    import re
    txt = updater.runner_script().read_text(encoding="utf-8")
    m = re.search(r'\$Payload = @\((.*?)\)', txt, re.S)
    assert m, "update_runner.ps1 에서 $Payload 를 못 찾음"
    listed = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert listed == set(updater.PAYLOAD), f"불일치: {listed ^ set(updater.PAYLOAD)}"


# ---- 자동 적용 안전 가드 (재기동 자동적용 · 개발체크아웃 · 롤백루프) ----

def test_is_dev_checkout_detects_ops_or_git(tmp_path, monkeypatch):
    """ops/ 또는 .git 이 있으면 개발 저장소 = 자동적용 금지 대상."""
    monkeypatch.setattr(updater, "install_root", lambda: tmp_path)
    assert updater.is_dev_checkout() is False           # 빈 폴더 = 사용자 배포본
    (tmp_path / "ops").mkdir()
    assert updater.is_dev_checkout() is True             # ops/ = 운영자 소스
    import shutil
    shutil.rmtree(tmp_path / "ops")
    (tmp_path / ".git").mkdir()
    assert updater.is_dev_checkout() is True             # .git = 체크아웃


def test_recently_failed_blocks_rollback_loop(tmp_path, monkeypatch):
    """방금 실패해 롤백된 버전은 재기동 자동적용에서 건너뛴다(무한루프 방지)."""
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    wd = updater.work_dir()
    (wd / "last_apply.json").write_text(
        json.dumps({"version": "9.9.9", "ok": False}), encoding="utf-8")
    assert updater.recently_failed("9.9.9") is True      # 실패 이력 = 건너뜀
    assert updater.recently_failed("9.9.8") is False      # 다른 버전은 무관
    (wd / "last_apply.json").write_text(
        json.dumps({"version": "9.9.9", "ok": True}), encoding="utf-8")
    assert updater.recently_failed("9.9.9") is False      # 성공 이력이면 통과
