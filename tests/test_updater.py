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
from pathlib import Path

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


_ROOT = Path(updater.__file__).resolve().parent.parent


def test_payload_lists_agree():
    """교체 대상은 네 곳이 같아야 한다 — updater.PAYLOAD · update_runner.ps1 $Payload · update_runner.sh PAYLOAD ·
    package-for-user.ps1 $include. 어긋나면 교체되는 것과 백업되는 것(또는 zip 에 담기는 것)이 달라져 롤백이 반쪽이 된다."""
    import re
    want = set(updater.PAYLOAD)
    txt = (Path(updater.__file__).parent / "update_runner.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r'\$Payload = @\((.*?)\)', txt, re.S)
    assert m, "update_runner.ps1 에서 $Payload 를 못 찾음"
    listed = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert listed == want, f"ps1 불일치: {listed ^ want}"

    sh = Path(updater.__file__).parent / "update_runner.sh"
    if sh.is_file():                    # 맥·리눅스 러너(posix-install 3단계) — 있는지는 test_posix_scripts 가 묶는다
        m = re.search(r'^PAYLOAD="([^"]*)"', sh.read_text(encoding="utf-8"), re.M)
        assert m, "update_runner.sh 에서 PAYLOAD=\"…\" 를 못 찾음"
        assert m.group(1).split() == list(updater.PAYLOAD), "sh PAYLOAD 는 같은 순서·같은 이름"

    pkg = (_ROOT / "ops" / "package-for-user.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r'\$include = @\((.*?)\n\)', pkg, re.S)
    assert m, "package-for-user.ps1 에서 $include 를 못 찾음"
    included = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert included == want, f"패키지 불일치: {included ^ want}"


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


# ---- 어느 OS 가 받는가(platforms 클레임, posix-install.md 4.2·4.6) ----

@pytest.fixture
def served(signer, tmp_path, monkeypatch):
    """check() 가 받아 갈 매니페스트를 심는다(네트워크 없이). 반환: 클레임을 받아 토큰을 심는 함수."""
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.0")
    pem, kid = signer
    box = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return box["tok"].encode()

    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda req, timeout=None: _Resp())

    def serve(**claims):
        box["tok"] = _sign(pem, kid, ver="2.0.0", **claims)
        return box["tok"]
    return serve


@pytest.mark.parametrize("os_name,claims,ok", [
    ("win", {}, True),                                          # 0.28 까지의 릴리스(클레임 없음) = win 전용
    ("linux", {}, False),
    ("mac", {}, False),
    ("win", {"platforms": ["win"]}, True),
    ("linux", {"platforms": ["win"]}, False),                   # 이번 출시(posix-install) 모양
    ("linux", {"platforms": ["win", "mac", "linux"]}, True),
    ("mac", {"platforms": ["win", "linux"]}, False),
    ("win", {"platforms": "win"}, False),                       # 모양이 이상하면 아무도 받지 않는다(fail-closed)
    ("win", {"platforms": ["win", 1]}, False),
    ("linux", {"platforms": []}, False),
])
def test_check_respects_platforms(served, monkeypatch, os_name, claims, ok):
    from session_manager import osdep
    monkeypatch.setattr(osdep, "NAME", os_name)
    tok = served(**claims)
    st = updater.check()
    assert st["available"] is ok and st["latest"] == "2.0.0" and st["error"] is None
    if ok:
        assert st["manifest"] == tok and not st.get("unsupported")
    else:
        assert st["manifest"] is None and os_name in st["unsupported"] and "2.0.0" in st["unsupported"]


def test_supported_check_clears_old_unsupported_note(served, monkeypatch):
    from session_manager import osdep
    monkeypatch.setattr(osdep, "NAME", "linux")
    served()
    assert updater.check()["unsupported"]
    served(platforms=["win", "linux"])
    assert updater.read_state().get("unsupported") and updater.check().get("unsupported") is None


def test_apply_refuses_other_os_release(fake_claude_home, monkeypatch):
    """check 를 거치지 않은 경로가 생겨도 마지막 관문(apply)에서 막는다 — 러너를 띄우지 않고 잠금도 남기지 않는다."""
    from session_manager import osdep
    monkeypatch.setattr(osdep, "NAME", "linux")
    monkeypatch.setattr(updater, "_popen", lambda *a, **k: pytest.fail("러너를 띄우면 안 된다"))
    with pytest.raises(updater.UpdateError, match="지원하지 않습니다"):
        updater.apply({"ver": "9.9.9", "platforms": ["win"]}, fake_claude_home, restart_cmd="")
    assert not (updater.work_dir() / "apply.lock").exists()


def test_apply_refuses_while_maintenance_flag(fake_claude_home, monkeypatch):
    """유지보수 표식이 있으면 런처가 아무것도 띄우지 않는다 → 적용하면 재기동이 막혀 롤백되고 Host 가 꺼진 채 남는다."""
    from session_manager import config
    monkeypatch.setattr(updater, "_popen", lambda *a, **k: pytest.fail("러너를 띄우면 안 된다"))
    d = config.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "maintenance.flag").write_text("by=stop\n", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="maintenance.flag"):
        updater.apply({"ver": "9.9.9", "platforms": ["win", "mac", "linux"]}, fake_claude_home, restart_cmd="")
    assert not (updater.work_dir() / "apply.lock").exists(), "거절은 잠금 전에 — 다음 시도를 5분 막지 않는다"


@pytest.mark.parametrize("case,refused", [
    ("install", True), ("runner", True), ("stale", False), ("dead", False), ("other-program", False), ("windows", False)])
def test_apply_refuses_while_an_install_holds_the_marker(fake_claude_home, monkeypatch, case, refused):
    """맥·리눅스: sh install.sh 가 도는 동안(update.inprogress by=install) 폰에서 적용하면 Host 는 내려가고 러너는 표식을
    보고 3 으로 끝나 다시 띄우지 않는다 — 설치가 중간에 실패하면 Host 가 꺼진 채 남는다. 러너의 marker_live 와 같은 규칙
    (pid 가 그 프로그램으로 살아 있고 30분 안)으로 잠금 전에 거부한다. Windows 는 이 표식을 쓰지 않는다(그대로)."""
    import subprocess
    import sys
    from session_manager import config, osdep
    prog = {"runner": "update_runner.sh", "other-program": "something-else.sh"}.get(case, "install.sh")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", prog])
    try:
        pid = 4_194_999 if case == "dead" else holder.pid
        at = int(time.time()) - (1801 if case == "stale" else 0)
        d = config.data_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "update.inprogress").write_text(f"pid={pid}\nat={at}\nby={'runner' if case == 'runner' else 'install'}\n",
                                             encoding="utf-8")
        monkeypatch.setattr(osdep, "NAME", "win" if case == "windows" else "linux")
        calls = []
        monkeypatch.setattr(updater, "_popen", lambda *a, **k: calls.append(a))
        staged = fake_claude_home / "stage"
        staged.mkdir()
        m = {"ver": "9.9.9", "platforms": ["win", "mac", "linux"]}
        if refused:
            with pytest.raises(updater.UpdateError, match="진행 중"):
                updater.apply(m, staged, restart_cmd="")
            assert calls == [] and not (updater.work_dir() / "apply.lock").exists(), "거절은 잠금 전에"
        else:
            updater.apply(m, staged, restart_cmd="")
            assert len(calls) == 1
    finally:
        holder.kill()
        holder.wait()


def test_manual_apply_reports_unsupported_reason(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(updater, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(updater, "check_and_stage", lambda: {"available": False, "error": None,
                                                             "unsupported": "이 릴리스(2.0.0)는 linux 를 지원하지 않습니다"})
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")         # 이 PC(로컬)에서 누른 적용
    with TestClient(S.create_app()) as c:
        r = c.post("/api/owner/update/apply")
    assert r.status_code == 200 and r.json()["applied"] is False
    assert "지원하지 않습니다" in r.json()["reason"]


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


# ---------- 작업 폴더 정리(TODOS 134행 Q16·R13) ----------

def _fill_work_dir(wd: Path) -> None:
    for v in ("0.9.0", "0.30.0", "0.31.0", "0.32.1", "0.32.2", "0.32.3"):
        (wd / f"clewpath-host-{v}.zip").write_bytes(b"z")
        (wd / f"stage-{v}" / "session_manager").mkdir(parents=True)
    for v in ("0.9.0", "0.30.0", "0.32.1", "0.33.0", "0.33.1"):
        (wd / f"backup-{v}" / "session_manager").mkdir(parents=True)
    for v, f in (("0.30.0", "0.31.0"), ("0.31.0", "0.32.0"), ("0.32.1", "0.32.2"), ("0.9.0", "0.30.0")):
        (wd / f"backup-{v}-failed-{f}" / "session_manager").mkdir(parents=True)
    # 정리 대상이 아닌 것들 — 이름 규칙에 안 맞으면 버전이 있어 보여도 건드리지 않는다
    for n in ("last_apply.json", "update_runner.ps1", "apply-0.9.0.log", "manifest-0.9.0.jwt",
              "clewpath-host-0.9.0.part", "notes.zip"):
        (wd / n).write_text("x", encoding="utf-8")
    for n in ("stage-notes", "backup-0.32.1.tmp.77", "runner-trash"):
        (wd / n).mkdir()


def test_prune_work_dir_keeps_two_newest_per_kind(fake_claude_home, monkeypatch):
    """updates/ 아래 zip·stage·backup(·실패본)이 업데이트마다 쌓였다 → 종류별로 버전이 높은 2개만 남긴다(문자열이 아니라 버전 순:
    0.9.0 < 0.30.0). 아직 적용 안 한 스테이지(staged_version)·적용한 버전(applying)·지금 설치본의 롤백 원본(지금 버전보다 낮은
    백업 중 가장 새것)은 몇 번째든 지우지 않는다."""
    monkeypatch.setattr(updater, "current_version", lambda: "0.32.2")
    monkeypatch.setattr(updater, "_marker_holder", lambda: None)
    wd = updater.work_dir()
    _fill_work_dir(wd)
    updater.write_state(staged_version="0.30.0", applying="0.31.0")
    removed = updater.prune_work_dir()
    names = {p.name for p in wd.iterdir()}
    assert {n for n in names if n.endswith(".zip") and n.startswith("clewpath-host-")} == {
        "clewpath-host-0.32.3.zip", "clewpath-host-0.32.2.zip", "clewpath-host-0.30.0.zip", "clewpath-host-0.31.0.zip"}
    assert {n for n in names if n.startswith("stage-")} == {
        "stage-0.32.3", "stage-0.32.2", "stage-0.30.0", "stage-0.31.0", "stage-notes"}
    assert {n for n in names if n.startswith("backup-") and "-failed-" not in n} == {
        "backup-0.33.1", "backup-0.33.0", "backup-0.32.1", "backup-0.32.1.tmp.77"}      # 0.32.1 = 지금(0.32.2)의 롤백 원본
    assert {n for n in names if "-failed-" in n} == {"backup-0.32.1-failed-0.32.2", "backup-0.31.0-failed-0.32.0"}
    for n in ("state.json", "last_apply.json", "update_runner.ps1", "apply-0.9.0.log", "manifest-0.9.0.jwt",
              "clewpath-host-0.9.0.part", "notes.zip", "runner-trash"):
        assert n in names, n
    assert sorted(removed) == sorted(["clewpath-host-0.9.0.zip", "clewpath-host-0.32.1.zip", "stage-0.9.0", "stage-0.32.1",
                                      "backup-0.9.0", "backup-0.30.0",
                                      "backup-0.30.0-failed-0.31.0", "backup-0.9.0-failed-0.30.0"])


@pytest.mark.parametrize("busy", ["apply.lock", "runner.lock.d", "marker"])
def test_prune_work_dir_does_nothing_while_applying(fake_claude_home, monkeypatch, busy):
    """적용이 도는 중(apply.lock — 나이 무관, 맥·리눅스 러너 잠금·update.inprogress 표식)에는 아무것도 지우지 않는다 —
    러너가 그 백업으로 롤백하고 그 스테이지로 교체한다."""
    monkeypatch.setattr(updater, "current_version", lambda: "0.32.2")
    monkeypatch.setattr(updater, "_marker_holder", lambda: "runner" if busy == "marker" else None)
    wd = updater.work_dir()
    _fill_work_dir(wd)
    if busy == "apply.lock":
        import os
        (wd / "apply.lock").write_text("1", encoding="utf-8")
        os.utime(wd / "apply.lock", (1, 1))                 # 시체 잠금이어도 — 러너가 죽었으면 그 백업이 복구에 필요하다
    elif busy == "runner.lock.d":
        (wd / "runner.lock.d").mkdir()
    before = {p.name for p in wd.iterdir()}
    assert updater.prune_work_dir() == []
    assert {p.name for p in wd.iterdir()} == before


def test_check_and_stage_does_not_prune_inside_the_apply_request(fake_claude_home, monkeypatch):
    """정리는 적용 요청 밖에서 — check_and_stage 안에서 돌던 첫 정리(이 PC 130개 버전분)가 1분 남짓 걸려 release.ps1 의 적용 요청이
    30초에 끊겼다(0.32.4 출시, Host 는 혼자 적용 완료). 이제 Host 가 뜬 뒤 배경에서 정리한다(prune_in_background)."""
    wd = updater.work_dir()
    monkeypatch.setattr(updater, "check", lambda: {"available": True, "manifest": "tok"})
    monkeypatch.setattr(updater, "verify_manifest", lambda t: {"ver": "0.32.3"})
    monkeypatch.setattr(updater, "download", lambda m, t: wd / "clewpath-host-0.32.3.zip")
    monkeypatch.setattr(updater, "stage", lambda z, m: wd / "stage-0.32.3")
    calls = []
    monkeypatch.setattr(updater, "prune_work_dir", lambda: calls.append(1) or [])
    st = updater.check_and_stage()
    assert calls == [] and st["staged_version"] == "0.32.3"


def test_prune_in_background_runs_later_and_swallows_errors(monkeypatch):
    import threading
    done = threading.Event()
    calls = []

    def fake():
        calls.append(1)
        done.set()
        raise OSError("정리 실패")                      # 배경 정리의 실패는 Host 를 건드리지 않는다
    monkeypatch.setattr(updater, "prune_work_dir", fake)
    t = updater.prune_in_background(delay_s=0)
    assert t.daemon and done.wait(5) and calls == [1]
    t.join(5)
    assert not t.is_alive()


def test_host_startup_schedules_background_prune():
    import inspect
    from session_manager import server
    src = inspect.getsource(server._lifespan)
    assert "prune_in_background(" in src and "_prune.stop.set()" in src


def test_background_prune_skips_when_stopped_or_data_dir_changed(fake_claude_home, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(updater, "prune_work_dir", lambda: calls.append(1) or [])
    t = updater.prune_in_background(delay_s=30)
    t.stop.set()                                         # 앱을 내렸다 — 기다리던 정리는 돌지 않는다
    t.join(5)
    assert not t.is_alive() and calls == []
    real = updater.work_dir()
    monkeypatch.setattr(updater, "work_dir", lambda: real)
    t = updater.prune_in_background(delay_s=0.2)
    monkeypatch.setattr(updater, "work_dir", lambda: tmp_path / "other")   # 시험이 끝나 진짜 홈으로 돌아간 것과 같은 꼴
    t.join(5)
    assert calls == []


def test_runner_env_drops_values_the_host_filled_from_config(fake_claude_home, monkeypatch):
    """TODOS 133행 Q15·R10: 러너(→ 새 Host)는 이 Host 의 env 를 물려받는다. Host 가 clewpath.toml 에서 채운 SM_* 까지 넘기면
    env 가 파일보다 우선이라 새 Host 가 옛 설정값으로 떠 업데이트 사이에 고친 clewpath.toml 이 가려졌다. 채운 것만 빼고
    사용자(런처)가 원래 준 env 는 그대로 넘긴다."""
    from session_manager import appconfig
    monkeypatch.setattr(appconfig, "_FILLED", {"SM_PORT", "SM_CP_URL"})
    monkeypatch.setenv("SM_PORT", "5333")                  # 이 Host 가 파일에서 채운 값
    monkeypatch.setenv("SM_CP_URL", "https://old-cp.test")
    monkeypatch.setenv("SM_HOST", "127.0.0.1")             # 런처가 준 값
    captured = {}
    monkeypatch.setattr(updater, "_popen", lambda args, *a, **k: captured.update(k))
    staged = fake_claude_home / "stage"
    staged.mkdir()
    updater.apply({"ver": "9.9.9", "platforms": ["win", "mac", "linux"]}, staged, restart_cmd="")
    env = captured["env"]
    assert "SM_PORT" not in env and "SM_CP_URL" not in env
    assert env["SM_HOST"] == "127.0.0.1"
    assert {k.upper() for k in env} >= {"PATH"}                # 나머지 env 도 그대로


def test_runner_env_is_inherited_as_is_when_nothing_was_filled(fake_claude_home, monkeypatch):
    from session_manager import appconfig
    monkeypatch.setattr(appconfig, "_FILLED", set())
    captured = {}
    monkeypatch.setattr(updater, "_popen", lambda args, *a, **k: captured.update(k))
    staged = fake_claude_home / "stage"
    staged.mkdir()
    updater.apply({"ver": "9.9.9", "platforms": ["win", "mac", "linux"]}, staged, restart_cmd="")
    assert captured.get("env") is None                     # 그대로 물려받는다(예전과 같음)
