"""맥·리눅스 설치 e2e — docs/designs/posix-install.md 6.3·6.4.

두 층이다.
- 하네스 시험(모든 OS, 기본 실행): build_fixtures 의 패키지(ps1 과 같은 목록)·변형 zip·시험 키 서명·컨텍스트 키 가드,
  fake_cp 의 라우트, 드라이버의 '컨테이너 밖에서는 거부'. 빠르고 docker 가 필요 없다.
- 컨테이너 왕복(`pytest -m posix_e2e tests/test_posix_e2e.py`, docker 필요 — 기본 실행에서는 pyproject addopts 로 빠진다):
  build_fixtures → docker build(이름 clewpath-posix-e2e-test, 빌드 때만 apt·PyPI) → docker run --rm --init --network none
  --name clewpath-posix-e2e-test → 드라이버의 마지막 줄 `E2E-RESULT {json}` 을 단정한다(S0~S9, 요청 로그).
  결과: CLEWPATH_E2E_OUT(없으면 pytest tmp) 아래 e2e-run.log·e2e-result.json·docker-build.log.
  통과했고 작업 트리가 깨끗하면 ops/logs/posix-e2e-<ver>.json = {version, head, ok} 를 남긴다 — release.ps1 이
  -Platforms 에 mac·linux 가 들어간 릴리스에서 같은 HEAD 의 이 기록을 요구한다(4.5·Q17).
  env: CLEWPATH_E2E_UV_ARCHIVE(고정 해시와 같은 uv tarball — 없으면 GitHub 에서 받는다), CLEWPATH_GET_SH.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from session_manager import updater
from session_manager.osdep import _posix

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "posix_e2e"
IMAGE = "clewpath-posix-e2e-test"
CONTAINER = "clewpath-posix-e2e-test"
FILES = ("build_fixtures.py", "fake_cp.py", "e2e_driver.py", "Dockerfile", "warm_cache.sh", "README.md")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"posix_e2e_{name}", HARNESS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load("build_fixtures")


@pytest.fixture(scope="module")
def base_zip() -> bytes:
    return bf.python_package()


@pytest.fixture(scope="module")
def key():
    pytest.importorskip("cryptography")
    k = bf.TestKey()
    yield k
    k.close()


def _names(zb: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        return z.namelist()


def _read(zb: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        return z.read(name)


# ---------------------------------------------------------------- 하네스 파일

def test_harness_files_are_present_and_unix_shaped():
    for n in FILES:
        assert (HARNESS / n).is_file(), n
    for n in ("Dockerfile", "warm_cache.sh", "e2e_driver.py", "fake_cp.py", "build_fixtures.py"):
        b = (HARNESS / n).read_bytes()
        assert b"\r" not in b and not b.startswith(b"\xef\xbb\xbf"), f"{n}: 컨테이너에서 도는 파일은 LF·BOM 없음"
    assert (HARNESS / "warm_cache.sh").read_bytes().startswith(b"#!/bin/sh\n")
    df = (HARNESS / "Dockerfile").read_text(encoding="utf-8")
    assert "procps" not in re.sub(r"#.*", "", df), "ps·pgrep 없이 도는지 보려고 procps 는 넣지 않는다"
    assert re.search(r"^USER tester$", df, re.M) and "CLEWPATH_E2E_CONTAINER=1" in df


def test_names_in_harness_end_with_test():
    """이 PC 의 배포 가드: 일회용 컨테이너·이미지는 이름이 -test 로 끝나야 한다."""
    for f in ("Dockerfile", "README.md"):
        for name in re.findall(r"(?:-t|--name)\s+([A-Za-z0-9_.:-]+)", (HARNESS / f).read_text(encoding="utf-8")):
            assert name.endswith("-test"), (f, name)
    assert IMAGE.endswith("-test") and CONTAINER.endswith("-test")


# ---------------------------------------------------------------- 패키지(ps1 과 같은 목록)

def test_package_lists_come_from_the_ps1():
    include, exclude = bf.ps1_lists(bf.PS1.read_text(encoding="utf-8"))
    assert include == list(updater.PAYLOAD), "패키지 화이트리스트 = PAYLOAD(test_updater 와 같은 약속)"
    for p in ("__pycache__", "*.pyc", ".venv", "start-connector.sh", ".clewpath-install", "*.pem", "release-keys"):
        assert p in exclude, p


def test_python_package_has_the_release_zip_shape(base_zip):
    with zipfile.ZipFile(io.BytesIO(base_zip)) as z:
        infos = z.infolist()
    names = [i.filename for i in infos]
    assert all("\\" not in n for n in names) and len(set(n.lower() for n in names)) == len(names)
    assert all(i.create_system == 0 and i.external_attr == 0 for i in infos), "Compress-Archive 모양(유닉스 권한 없음)"
    files = {n for n in names if not n.endswith("/")}
    for need in ("install.sh", "uninstall.sh", "session_manager/update_runner.sh", "session_manager/hostctl.py",
                 "session_manager/start-connector.template.sh", "pyproject.toml", "uv.lock", "LICENSE"):
        assert need in files, need
    assert {n.split("/", 1)[0] for n in files} <= set(updater.PAYLOAD)
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pem"))
                or PurePosixPath(n).name in ("start-connector.sh", ".clewpath-install")]
    for d in (n for n in names if n.endswith("/")):                   # 하위 폴더만 든 폴더의 항목(Compress-Archive)
        assert not [f for f in files if str(PurePosixPath(f).parent) + "/" == d], d


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh 없음 — 진짜 package-for-user.ps1 과의 대조는 pwsh 가 있을 때만")
def test_python_package_matches_package_for_user(base_zip, tmp_path):
    zb = bf.pwsh_package(tmp_path)
    real, ours = set(_names(zb)), set(_names(base_zip))
    assert {n for n in real if not n.endswith("/")} == {n for n in ours if not n.endswith("/")}
    assert {n for n in real if n.endswith("/")} == {n for n in ours if n.endswith("/")}
    for n in ("install.sh", "session_manager/update_runner.sh", "session_manager/updater.py"):
        assert _read(zb, n) == _read(base_zip, n), n


# ---------------------------------------------------------------- 변형·서명

def test_variants_change_only_what_the_table_says(base_zip, key):
    a = bf.make_variant(base_zip, "A", key)
    assert set(_names(a)) == set(_names(base_zip))
    assert re.search(r'^version = "90\.0\.0"$', _read(a, "pyproject.toml").decode(), re.M)
    assert '[[package]]\nname = "clewpath-host"\nversion = "90.0.0"' in _read(a, "uv.lock").decode()
    assert bf.release_keys_of(_read(a, "session_manager/updater.py").decode()) == {key.kid: key.pub}, "시험 키 하나만(S46)"
    assert _read(a, "README.md").decode().split("\n", 1)[0] == bf.readme_marker("A", "90.0.0")
    assert _read(a, bf.TEMPLATE) == _read(base_zip, bf.TEMPLATE), "A 는 템플릿 그대로"
    for n in _names(base_zip):
        if n not in ("pyproject.toml", "uv.lock", "session_manager/updater.py", "README.md") and not n.endswith("/"):
            assert _read(a, n) == _read(base_zip, n), n

    b = bf.make_variant(base_zip, "B", key)
    tpl = _read(b, bf.TEMPLATE).decode()
    assert tpl.split("\n")[3] == bf.template_marker("B") and tpl.startswith("#!/bin/sh\n")
    out = _posix.render_launcher(tpl, data_dir=PurePosixPath("/d"), conf_file=PurePosixPath("/d/c.toml"), port=5100)
    assert bf.template_marker("B") in out and "{{" not in out

    c = bf.make_variant(base_zip, "C", key)
    tree = ast.parse(_read(c, "session_manager/server.py").decode())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert isinstance(main.body[0], ast.Raise), "C 는 server.main 첫 줄에서 끝난다"

    names = {n: set(_names(bf.make_variant(base_zip, n, key))) for n in ("L", "L-noinstall", "Z-dotdot", "Z-abs", "Z-link")}
    assert "install.sh" not in names["L"] and "uninstall.sh" not in names["L"]
    assert "install.sh" not in names["L-noinstall"] and "uninstall.sh" in names["L-noinstall"]
    assert bf.EVIL_DOTDOT in names["Z-dotdot"] and bf.EVIL_ABS in names["Z-abs"] and bf.EVIL_LINK in names["Z-link"]
    with zipfile.ZipFile(io.BytesIO(bf.make_variant(base_zip, "Z-link", key))) as z:
        assert (z.getinfo(bf.EVIL_LINK).external_attr >> 16) & 0o170000 == 0o120000, "심볼릭 링크 항목"


def test_variant_edits_refuse_when_the_spot_is_missing():
    with pytest.raises(bf.FixtureError):
        bf.set_lock_version(b'[[package]]\nname = "other"\nversion = "1"\n', "9.9.9")
    with pytest.raises(bf.FixtureError):
        bf.break_server(b"def main(x):\n    pass\n")
    with pytest.raises(bf.FixtureError):
        bf.set_release_keys(b"RELEASE_KEYS = {}\n", "k", "v")


def test_manifests_are_signed_like_production_with_the_test_key(base_zip, key, tmp_path, monkeypatch):
    sr = bf._sign_release()
    a = bf.make_variant(base_zip, "A", key)
    zp = tmp_path / "90.0.0.zip"
    zp.write_bytes(a)
    tok = bf.sign(sr, key, zp, "90.0.0", tmp_path / "A.jwt", "win,mac,linux")
    monkeypatch.setattr(updater, "RELEASE_KEYS", {key.kid: key.pub})
    m = updater.verify_manifest(tok)
    assert m["url"] == f"{bf.CP}/updates/download/90.0.0" and m["ver"] == "90.0.0" and m["size"] == len(a)
    assert m["platforms"] == ["win", "mac", "linux"] and m["exp"] - m["iat"] == 86400, "TTL 1일"
    import hashlib
    assert m["sha256"] == hashlib.sha256(a).hexdigest()
    sha = updater.verify_manifest(bf.resign(tok, key, "sha256"))
    assert sha["sha256"] != m["sha256"] and len(sha["sha256"]) == 64
    assert updater.verify_manifest(bf.resign(tok, key, "size+1"))["size"] == len(a) + 1
    assert updater.verify_manifest(bf.resign(tok, key, "size-1"))["size"] == len(a) - 1
    legacy = updater.verify_manifest(bf.resign(tok, key, "no-platforms"))
    assert "platforms" not in legacy, "L: 0.28.1 모양(클레임 없음 = win 전용)"
    with pytest.raises(updater.UpdateError):
        updater.verify_manifest(bf.broken_jwt())
    prod = bf.release_keys_of((ROOT / "session_manager" / "updater.py").read_text(encoding="utf-8"))
    monkeypatch.setattr(updater, "RELEASE_KEYS", prod)
    with pytest.raises(updater.UpdateError, match="모르는 서명키"):    # 운영 빌드의 Host 는 시험 서명을 믿지 않는다
        updater.verify_manifest(tok)


# ---------------------------------------------------------------- 산출 폴더·키 가드

def test_build_makes_a_clean_context(tmp_path):
    pytest.importorskip("cryptography")
    out = tmp_path / "ctx-out"
    ix = bf.build(out, get_sh=ROOT / "ops" / "get.sh", use_pwsh=False, with_uv=False)
    assert set(ix["variants"]) == set(bf.VARIANTS)
    assert set(ix["manifests"]) == set(bf.VARIANTS) | set(bf.TAMPERED) | {"M"}
    assert ix["payload"] == list(updater.PAYLOAD) and ix["cp"] == bf.CP
    for rel in ("Dockerfile", "base/warm_cache.sh", "base/warm/pyproject.toml", "base/warm/uv.lock", "base/warm/LICENSE",
                "base/warm/session_manager/__init__.py", "ctx/e2e_driver.py", "ctx/fake_cp.py", "ctx/fixtures/get.sh",
                "ctx/fixtures/index.json"):
        assert (out / rel).is_file(), rel
    assert 'version = "90.0.0"' in (out / "base/warm/pyproject.toml").read_text(encoding="utf-8")
    assert (out / "ctx/fixtures/get.sh").read_bytes() == (ROOT / "ops" / "get.sh").read_bytes()
    assert not [p for p in out.rglob("*") if p.suffix in (".pem", ".key")], "개인키는 컨텍스트에 없다"
    prod = set(bf.release_keys_of((ROOT / "session_manager" / "updater.py").read_text(encoding="utf-8")))
    for v in ix["variants"].values():
        zb = (out / "ctx" / "fixtures" / v["zip"]).read_bytes()
        keys = bf.release_keys_of(_read(zb, "session_manager/updater.py").decode())
        assert set(keys) == {ix["test_kid"]} and not (set(keys) & prod), "시험 빌드는 운영 kid 를 싣지 않는다(S46)"
    ix2 = bf.build(out, get_sh=ROOT / "ops" / "get.sh", use_pwsh=False, with_uv=False)    # 같은 폴더에 다시
    assert ix2["test_kid"] != ix["test_kid"], "키는 실행마다 새로"


def test_build_refuses_odd_output_folders(tmp_path):
    with pytest.raises(bf.FixtureError):
        bf.prepare_out(ROOT / "build-e2e-should-not-exist")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "mine.txt").write_text("x", encoding="utf-8")
    with pytest.raises(bf.FixtureError):
        bf.prepare_out(foreign)
    assert (foreign / "mine.txt").is_file(), "남의 폴더는 지우지 않는다"
    assert not (ROOT / "build-e2e-should-not-exist").exists()


@pytest.mark.parametrize("plant", ["pem", "keydir", "private", "zipped"])
def test_guard_refuses_anything_key_like(tmp_path, plant):
    out = tmp_path / "o"
    (out / "ctx").mkdir(parents=True)
    (out / "ctx" / "ok.txt").write_text("fine", encoding="utf-8")
    bf.guard_context(out)
    if plant == "pem":
        (out / "ctx" / "test-key.pem").write_text("x", encoding="utf-8")
    elif plant == "keydir":
        (out / "ctx" / "release-keys").mkdir()
    elif plant == "private":
        (out / "ctx" / "notes.txt").write_text("-----BEGIN PRIVATE KEY-----\nabc\n", encoding="utf-8")
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ops/x/private.pem", "x")
        (out / "ctx" / "a.zip").write_bytes(buf.getvalue())
    with pytest.raises(bf.FixtureError):
        bf.guard_context(out)


def test_uv_pins_and_archive_check(tmp_path):
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    ver, pins = bf.uv_pins(text)
    assert re.search(rf'^UV_DEFAULT="{re.escape(ver)}"$', text, re.M)
    assert set(pins) == {"x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-apple-darwin",
                         "aarch64-apple-darwin"} and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in pins.values())
    fake = tmp_path / "uv.tar.gz"
    fake.write_bytes(b"not uv")
    with pytest.raises(bf.FixtureError):
        bf.acquire_uv(tmp_path / "dest", "x86_64-unknown-linux-gnu", fake)
    with pytest.raises(bf.FixtureError):
        bf.acquire_uv(tmp_path / "dest", "riscv64-unknown-linux-gnu", fake)


# ---------------------------------------------------------------- 가짜 CP

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _req(url: str, method: str = "GET", body: bytes | None = None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type"), e.read()


def test_fake_cp_routes_and_request_log(tmp_path):
    state, fix = tmp_path / "state", tmp_path / "fix"
    (fix / "zips").mkdir(parents=True)
    (fix / "zips" / "90.0.0.zip").write_bytes(b"PK-zip-bytes")
    (fix / "get.sh").write_bytes(b"#!/bin/sh\nmain() { :; }\nmain \"$@\"\n")
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-I", str(HARNESS / "fake_cp.py"), "--state", str(state), "--fixtures", str(fix),
                          "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}/cp"
    try:
        for _ in range(100):
            try:
                if _req(f"{base}/healthz")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        assert _req(f"{base}/healthz")[2] == b'{"ok": true}'
        assert _req(f"{base}/updates/manifest")[:2] == (404, "application/json")
        (state / "current.jwt").write_bytes(b"aa.bb.cc\n")
        assert _req(f"{base}/updates/manifest") == (200, "application/jwt", b"aa.bb.cc")
        assert _req(f"{base}/updates/download/90.0.0") == (200, "application/zip", b"PK-zip-bytes")
        assert _req(f"{base}/updates/download/9.9.9")[0] == 404
        assert _req(f"{base}/updates/download/..%2Fget.sh")[0] == 404
        code, ctype, got = _req(f"{base}/get.sh")
        assert code == 200 and ctype == "text/plain; charset=utf-8" and got == (fix / "get.sh").read_bytes()
        c1 = json.loads(_req(f"{base}/provision/anonymous", "POST", b"{}")[2])
        c2 = json.loads(_req(f"{base}/provision/anonymous", "POST", b"{}")[2])
        assert c1["connector_id"] == "c-e2e-1" and c2["connector_id"] == "c-e2e-2"
        assert {"room_key", "credential_public_id", "secret", "claim_secret"} <= set(c1)
        (state / "provision_status").write_text("429", encoding="ascii")
        assert _req(f"{base}/provision/anonymous", "POST", b"{}")[0] == 429
        assert _req(f"{base}/connector/token", "POST", b"{}")[0] == 503
        assert _req(f"{base}/nope")[0] == 404 and _req(f"http://127.0.0.1:{port}/get.sh")[0] == 404
        log = [json.loads(x) for x in (state / "requests.log").read_text(encoding="utf-8").splitlines()]
        assert [f"{r['m']} {r['p']}" for r in log][-4:] == ["POST /cp/provision/anonymous", "POST /cp/connector/token",
                                                          "GET /cp/nope", "GET /get.sh"]
        assert (state / "provision_count").read_text(encoding="ascii") == "3"
    finally:
        p.terminate()
        p.wait(10)


def test_fake_cp_env_injects_provision_status(tmp_path):
    (tmp_path / "fix").mkdir()
    port = _free_port()
    env = {**os.environ, "FAKE_CP_PROVISION_STATUS": "429"}
    p = subprocess.Popen([sys.executable, "-I", str(HARNESS / "fake_cp.py"), "--state", str(tmp_path / "st"), "--fixtures",
                          str(tmp_path / "fix"), "--port", str(port)], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                _req(f"http://127.0.0.1:{port}/cp/healthz")
                break
            except OSError:
                time.sleep(0.1)
        assert _req(f"http://127.0.0.1:{port}/cp/provision/anonymous", "POST", b"{}")[0] == 429
    finally:
        p.terminate()
        p.wait(10)


def test_fake_cp_listens_on_loopback_only():
    src = (HARNESS / "fake_cp.py").read_text(encoding="utf-8")
    assert re.findall(r'ThreadingHTTPServer\(\("([^"]*)"', src) == ["127.0.0.1"]


# ---------------------------------------------------------------- 드라이버 가드

def test_driver_refuses_outside_the_container(tmp_path):
    """실제 ~/.local/share·~/.claude·5100 에 설치하는 드라이버 — 컨테이너 표식(env)이 없으면 아무것도 하지 않는다."""
    env = {k: v for k, v in os.environ.items() if k != "CLEWPATH_E2E_CONTAINER"}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    r = subprocess.run([sys.executable, "-I", str(HARNESS / "e2e_driver.py")], env=env, capture_output=True, timeout=60,
                       cwd=str(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "거부" in r.stdout.decode("utf-8", "replace") and not list(tmp_path.iterdir())


def test_driver_runs_every_step_of_the_spec():
    drv = _load("e2e_driver")
    assert [sid for sid, _, _ in drv.STEPS] == ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S9", "S8"]
    assert drv.UMASK not in (0o022, 0o077) and drv.RELAY.startswith("ws://127.0.0.1:")


# ---------------------------------------------------------------- 컨테이너 왕복(-m posix_e2e)

def _docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        pytest.skip("docker 가 없습니다")
    if subprocess.run([exe, "version"], capture_output=True, timeout=120).returncode != 0:
        pytest.skip("docker 데몬에 닿지 못했습니다")
    return exe


def _release_record(summary: dict, root: Path = ROOT) -> str:
    """통과했고 작업 트리가 깨끗하면 ops/logs/posix-e2e-<ver>.json = {version, head, ok} — release.ps1 이 -Platforms 에
    mac·linux 가 든 릴리스에서 같은 HEAD 의 이 기록을 요구한다. '깨끗함'은 release.ps1 과 같은 기준(…003_crosscheck·
    .context 무시)이다. 깨끗하지 않으면 남기지 않는다 — 시험한 트리가 HEAD 와 다를 수 있다."""
    if not summary.get("ok"):
        return "기록 안 남김 — 통과하지 못했다"
    st = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=60)
    dirty = [x for x in st.stdout.splitlines() if not re.match(r"^\?\? (003_crosscheck|\.context)", x)]
    if st.returncode != 0 or dirty:
        return f"기록 안 남김 — 작업 트리가 깨끗하지 않다({len(dirty)}줄)"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=60).stdout.strip()
    ver = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    p = root / "ops" / "logs" / f"posix-e2e-{ver}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": ver, "head": head, "ok": True}), encoding="utf-8")
    return f"기록: {p}"


@pytest.mark.skipif(not shutil.which("git"), reason="git 없음")
def test_release_record_only_for_a_clean_passing_tree(tmp_path):
    """기록은 release.ps1 이 읽는 모양 그대로({version, head, ok}), 통과했고 트리가 깨끗할 때만."""
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=e2e", "-c", "user.email=e2e@example.invalid", *a], cwd=tmp_path,
                              capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    git("init", "-q")
    (tmp_path / ".gitignore").write_text("ops/logs/posix-e2e-*.json\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "clewpath-host"\nversion = "9.8.7"\n', encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    rec = tmp_path / "ops" / "logs" / "posix-e2e-9.8.7.json"
    assert "통과하지" in _release_record({"ok": False}, tmp_path) and not rec.exists()
    (tmp_path / "x.txt").write_text("dirty", encoding="utf-8")
    assert "깨끗하지" in _release_record({"ok": True}, tmp_path) and not rec.exists()
    (tmp_path / "x.txt").unlink()
    assert _release_record({"ok": True}, tmp_path) == f"기록: {rec}"
    assert json.loads(rec.read_text(encoding="utf-8")) == {"version": "9.8.7", "head": git("rev-parse", "HEAD"), "ok": True}
    assert "깨끗하지" not in _release_record({"ok": True}, tmp_path), "기록 자신(.gitignore)은 트리를 더럽히지 않는다"


@pytest.mark.posix_e2e
def test_posix_install_roundtrip_in_container(tmp_path_factory):
    docker = _docker()
    out = Path(os.environ.get("CLEWPATH_E2E_OUT") or tmp_path_factory.mktemp("posix-e2e")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ctx = out / "build"
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(HARNESS / "build_fixtures.py"), "--out", str(ctx)], capture_output=True,
                       timeout=1200)
    (out / "fixtures.log").write_bytes(r.stdout + r.stderr)
    assert r.returncode == 0, (r.stdout + r.stderr).decode("utf-8", "replace")
    b = subprocess.run([docker, "build", "-t", IMAGE, str(ctx)], capture_output=True, timeout=3600)
    (out / "docker-build.log").write_bytes(b.stdout + b.stderr)
    assert b.returncode == 0, f"docker build 실패 — {out / 'docker-build.log'}"
    subprocess.run([docker, "rm", "-f", CONTAINER], capture_output=True, timeout=120)    # 중단된 지난 실행이 남긴 같은 이름
    t = subprocess.run([docker, "run", "--rm", "--init", "--network", "none", "--name", CONTAINER, IMAGE],
                       capture_output=True, timeout=3600)
    text = (t.stdout + b"\n--- stderr\n" + t.stderr).decode("utf-8", "replace")
    (out / "e2e-run.log").write_text(text, encoding="utf-8")
    lines = [x for x in t.stdout.decode("utf-8", "replace").splitlines() if x.startswith("E2E-RESULT ")]
    assert lines, f"드라이버가 결과를 내지 못했습니다(rc={t.returncode}) — {out / 'e2e-run.log'}"
    summary = json.loads(lines[-1][len("E2E-RESULT "):])
    summary["wall_seconds"] = round(time.monotonic() - t0, 1)
    (out / "e2e-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [f"{x['step']} {x['name']}: {x['detail'][-600:]}" for x in summary["results"] if not x["ok"]]
    print(f"\n[posix-e2e] {summary['passed']} 통과·{summary['failed']} 실패 ({summary['seconds']}초) — {out}")
    assert summary["ok"] and t.returncode == 0 and not summary["not_run"], \
        f"{out / 'e2e-run.log'}\n돌지 않은 단계: {summary['not_run']}\n" + "\n".join(bad)
    print("[posix-e2e] " + _release_record(summary))
