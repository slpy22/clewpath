"""맥·리눅스 설치 e2e 재료 — docs/designs/posix-install.md 6.3·6.4. 호스트(개발 PC)에서 돈다.

    python tests/posix_e2e/build_fixtures.py --out <저장소 밖 폴더> [--get-sh PATH] [--uv-archive PATH] [--no-pwsh]

--out 이 곧 docker build 컨텍스트다(저장소를 컨텍스트로 쓰지 않는다):
  Dockerfile
  base/  잘 안 바뀌는 층 — uv tarball(install.sh 에 고정된 해시와 대조), warm_cache.sh, warm/(의존성 캐시를 데울 최소 트리)
  ctx/   매번 — 변형 zip·시험 키로 서명한 매니페스트·get.sh·fake_cp.py·e2e_driver.py·index.json

기본 zip 은 pwsh 가 있으면 진짜 ops/package-for-user.ps1(-OutDir 임시)로 만든다 — .sh 형식 게이트·화이트리스트까지 같이
시험된다. 없으면 같은 목록(ps1 의 $include·$excludePatterns 를 그대로 읽는다)의 파이썬 판으로 만든다. 변형은 zipfile 로
항목만 바꿔 쓴다(구분자 '/', Compress-Archive 와 같은 '권한 없음' 모양).

지키는 것(S19·S44·S46):
  - 서명 키는 이 프로세스가 만든 시험 키뿐이다. 저장소 밖 임시 폴더에만 두고 서명이 끝나면 지운다 — 컨텍스트에는 공개키
    (zip 안 updater.RELEASE_KEYS 를 시험 키 하나로 바꾼 것)만 간다. 운영 서명 키는 읽지도 만들지도 않는다
  - 컨텍스트에 *.pem·키 폴더·개인키 글이 보이면 중단한다. --out 이 저장소 안이거나 우리가 만든 폴더가 아니면 거부한다
  - 네트워크는 uv 공식 릴리스(GitHub)의 tarball·.sha256 뿐이다 — --uv-archive 를 주거나 base/ 에 맞는 것이 있으면 안 쓴다
  - 시험 빌드는 버전 90.x·91.x 와 시험 kid 라 게시할 수 없다(release.ps1 의 zip 게이트가 RELEASE_KEYS 를 대조한다)
"""
from __future__ import annotations

import argparse
import ast
import base64
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PS1 = ROOT / "ops" / "package-for-user.ps1"
SIGN = ROOT / "ops" / "tools" / "sign_release.py"
INSTALL_SH = ROOT / "install.sh"

CP = "http://127.0.0.1:8099/cp"                 # 컨테이너 안 가짜 CP(fake_cp.py)
MARK = ".clewpath-e2e-build"                     # 이 폴더는 build_fixtures 가 만들었다(지워도 되는 폴더인지 알아보는 표)
NOTES = "e2e 시험 빌드 — 한글 노트 \"따옴표\""

TEMPLATE = "session_manager/start-connector.template.sh"
# 변형(이름 → 버전·받는 OS·바꿀 것). platforms None = 클레임 없음(0.28.1 모양). 명세 6.3 표 + 드라이버가 쓰는 몇 개
VARIANTS = {
    "A": {"ver": "90.0.0", "platforms": "win,mac,linux", "use": "첫 설치(S2)"},
    "B": {"ver": "90.0.1", "platforms": "win,mac,linux", "template": True, "use": "수동 적용 성공(S5)·재렌더"},
    "C": {"ver": "90.0.2", "platforms": "win,mac,linux", "template": True, "broken": True, "use": "실패와 롤백(S6)"},
    "D": {"ver": "90.0.3", "platforms": "win,mac,linux", "template": True, "use": "재기동 자동 적용(S7)"},
    "E": {"ver": "90.0.4", "platforms": "win,mac,linux", "template": True,
          "use": "bash --posix 러너(S9) — CP 로 내지 않고 드라이버가 stage 한다"},
    "L": {"ver": "91.0.0", "platforms": None, "drop": ("install.sh", "uninstall.sh"), "use": "0.28.1 모양 거부(S1)"},
    "P": {"ver": "91.0.1", "platforms": "win", "use": "win 전용 거부(S1)"},
    "Z-dotdot": {"ver": "91.0.2", "platforms": "win,mac,linux", "add": "dotdot", "use": "../ 항목 거부(S1)"},
    "T": {"ver": "91.0.3", "platforms": "win,mac,linux", "use": "sha256·size 변조 거부(S1)"},
    "L-noinstall": {"ver": "91.0.4", "platforms": "win,mac,linux", "drop": ("install.sh",),
                    "use": "linux 클레임은 있으나 install.sh 없음(S1, 푼 뒤 거부)"},
    "Z-abs": {"ver": "91.0.5", "platforms": "win,mac,linux", "add": "abs", "use": "절대 경로 항목 거부(S1)"},
    "Z-link": {"ver": "91.0.6", "platforms": "win,mac,linux", "add": "link", "use": "심볼릭 링크 항목 거부(S1)"},
}
# 서명은 그대로 두고 내용만 바꾼 매니페스트(서명은 get.sh 가 보지 않는다 — 신뢰 모델). 이름 → (원본 변형, 바꿀 것)
TAMPERED = {"T-sha": ("T", "sha256"), "T-size": ("T", "size+1"), "T-small": ("T", "size-1"), "L": ("L", "no-platforms")}
EVIL_DOTDOT = "../../clewpath-e2e-evil"          # stage 에서 두 단계 위 = CLEWPATH_HOME 바로 아래
EVIL_ABS = "/tmp/clewpath-e2e-evil-abs"
EVIL_LINK = "session_manager/clewpath-e2e-link"


class FixtureError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[fixtures] {msg}", flush=True)


# ---------------------------------------------------------------- 패키지(기본 zip)

def ps1_lists(text: str) -> tuple[list[str], list[str]]:
    """package-for-user.ps1 의 $include·$excludePatterns(주석 뺀 따옴표 글자들). 파이썬 판이 같은 목록을 쓰게."""
    out = []
    for var in ("include", "excludePatterns"):
        m = re.search(r"^\$" + var + r"\s*=\s*@\((.*?)^\)", text, re.S | re.M)
        if not m:
            raise FixtureError(f"package-for-user.ps1 에서 ${var} 를 찾지 못했습니다")
        body = "\n".join(line.split("#", 1)[0] for line in m.group(1).splitlines())
        out.append(re.findall(r'"([^"]+)"', body))
    return out[0], out[1]


class _NoAttr(int):
    """값은 0 인데 참인 정수. zipfile 은 속성이 0 이면 쓸 때 0o600 을 넣는다(ZipFile._open_to_write) — 그러면 unzip 이
    FAT 항목의 그 값을 유닉스 권한으로 받아 0600 으로 푼다. Compress-Archive 처럼 속성 0 을 그대로 쓰게 한다(시험이 다시 읽어 확인)."""

    def __bool__(self) -> bool:
        return True


def _fat(name: str) -> zipfile.ZipInfo:
    """Compress-Archive 가 쓰는 항목 모양 — 만든 OS 0(FAT), 속성 0(유닉스 권한 없음), 이름은 날것 그대로."""
    zi = zipfile.ZipInfo("placeholder", date_time=(2026, 10, 21, 0, 0, 0))
    zi.filename = name
    zi.create_system = 0
    zi.external_attr = _NoAttr(0)
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def python_package(root: Path = ROOT) -> bytes:
    """package-for-user.ps1 의 파이썬 판 — 같은 화이트리스트·제외 규칙(PowerShell -like '*패턴*', 대소문자 무시),
    파일만 넣고 하위 폴더만 든 폴더는 폴더 항목으로(Compress-Archive 와 같은 모양). .sh 형식 게이트도 같은 규칙."""
    include, exclude = ps1_lists(PS1.read_text(encoding="utf-8"))

    def excluded(rel: str) -> bool:
        low = rel.lower()
        return any(fnmatch.fnmatchcase(low, f"*{p.lower()}*") for p in exclude)

    files: dict[str, bytes] = {}
    for item in include:
        src = root / item
        if src.is_dir():
            for p in sorted(src.rglob("*")):
                if p.is_file() and not excluded(p.relative_to(root).as_posix()):
                    files[p.relative_to(root).as_posix()] = p.read_bytes()
        elif src.is_file() and not excluded(item):
            files[item] = src.read_bytes()
    bad = [n for n, b in files.items() if n.endswith(".sh") and (b.startswith(b"\xef\xbb\xbf") or b"\r" in b
                                                                 or b.split(b"\n", 1)[0] != b"#!/bin/sh")]
    if bad:
        raise FixtureError(f"맥·리눅스 셸 스크립트 형식이 어긋났습니다(LF·BOM 없음·1행 #!/bin/sh): {bad}")
    dirs = {str(PurePosixPath(n).parent) for n in files} - {"."}
    parents = {str(p) for d in dirs for p in PurePosixPath(d).parents} - {"."}
    empty = sorted(d for d in dirs | parents if not any(str(PurePosixPath(n).parent) == d for n in files))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in empty:
            z.writestr(_fat(d + "/"), b"")
        for n, b in files.items():
            z.writestr(_fat(n), b)
    return buf.getvalue()


def pwsh_package(work: Path) -> bytes:
    """진짜 package-for-user.ps1 로 만든다(스테이지는 work 안 — 사용자의 %TEMP% 를 쓰지 않게 TEMP 를 바꾼다)."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise FixtureError("pwsh 가 없습니다")
    out, tmp = work / "pkg-out", work / "pkg-tmp"
    out.mkdir(parents=True)
    tmp.mkdir(parents=True)
    env = {**os.environ, "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp)}
    r = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(PS1), "-OutDir", str(out)],
                       capture_output=True, timeout=300, env=env)
    zips = sorted(out.glob("clewpath-host-*.zip"))
    if r.returncode != 0 or len(zips) != 1:
        raise FixtureError("package-for-user.ps1 실패(rc=%s):\n%s" % (
            r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")[-3000:]))
    return zips[0].read_bytes()


# ---------------------------------------------------------------- 변형

def _one(pattern: str, repl, text: str, what: str, flags: int = 0) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise FixtureError(f"{what}: 바꿀 자리를 찾지 못했습니다")
    return new


def set_pyproject_version(b: bytes, ver: str) -> bytes:
    return _one(r'^version = "[^"]+"', f'version = "{ver}"', b.decode("utf-8"), "pyproject version", re.M).encode()


def set_lock_version(b: bytes, ver: str) -> bytes:
    """uv.lock 의 루트 패키지(clewpath-host, editable) 버전 — pyproject 와 같게(uv sync --frozen 이 이 줄을 쓴다)."""
    return _one(r'(\[\[package\]\]\nname = "clewpath-host"\nversion = ")[^"]+(")', rf"\g<1>{ver}\g<2>",
                b.decode("utf-8"), "uv.lock 루트 패키지 version").encode()


def set_release_keys(b: bytes, kid: str, pub: str) -> bytes:
    """updater.RELEASE_KEYS 를 시험 공개키 하나로 — 운영 코드에 키 주입 길을 만들지 않고 시험 zip 의 사본만 바꾼다."""
    src = _one(r"^RELEASE_KEYS: dict\[str, str\] = \{\n.*?^\}\n",
               f'RELEASE_KEYS: dict[str, str] = {{\n    "{kid}": "{pub}",   # e2e 시험 키 — 이 시험 빌드에만\n}}\n',
               b.decode("utf-8"), "updater.RELEASE_KEYS", re.S | re.M)
    if release_keys_of(src) != {kid: pub}:
        raise FixtureError("updater.RELEASE_KEYS 를 시험 키 하나로 바꾸지 못했습니다")
    return src.encode("utf-8")


def release_keys_of(src: str) -> dict:
    for node in ast.parse(src).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "RELEASE_KEYS":
            return ast.literal_eval(node.value)
    raise FixtureError("RELEASE_KEYS 가 없습니다")


def readme_marker(name: str, ver: str) -> str:
    return f"<!-- e2e-build: {name} {ver} -->"


def template_marker(name: str) -> str:
    return f"# e2e-build: {name}"


def mark_template(b: bytes, name: str) -> bytes:
    """런처 템플릿 3행 뒤에 주석 한 줄 — 렌더 결과가 달라져 Host 의 ensure_launcher 가 다시 만드는지 본다(1·2행은 그대로)."""
    lines = b.decode("utf-8").split("\n")
    if lines[0] != "#!/bin/sh" or "clewpath-launcher v2" not in lines[1]:
        raise FixtureError("런처 템플릿 머리가 예상과 다릅니다")
    return "\n".join(lines[:3] + [template_marker(name)] + lines[3:]).encode("utf-8")


def break_server(b: bytes) -> bytes:
    """server.main 첫 줄에서 끝난다 — 런처가 '기동 직후 종료'를 보고 러너가 곧바로 롤백한다."""
    return _one(r"^def main\(\):\n", 'def main():\n    raise SystemExit("e2e: 깨진 빌드")\n',
                b.decode("utf-8"), "server.main", re.M).encode("utf-8")


def rewrite_zip(base: bytes, edits: dict, drop=(), add=()) -> bytes:
    """기본 zip 의 항목을 하나씩 옮겨 쓰며 edits(이름 → bytes 를 받아 bytes 를 돌려주는 함수)를 적용하고, drop 은 빼고,
    add([(ZipInfo, bytes)])를 덧붙인다. 모양(이름 그대로·권한 없음)은 기본 zip 을 따른다."""
    seen = set()
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(base)) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in zin.infolist():
            if info.filename in drop:
                seen.add(info.filename)
                continue
            data = zin.read(info)
            if info.filename in edits:
                data = edits[info.filename](data)
                seen.add(info.filename)
            zi = _fat(info.filename)
            zi.date_time = info.date_time
            zi.create_system, zi.external_attr = info.create_system, info.external_attr or _NoAttr(0)
            z.writestr(zi, data)
        for zi, data in add:
            z.writestr(zi, data)
    missing = (set(edits) | set(drop)) - seen
    if missing:
        raise FixtureError(f"기본 zip 에 없는 항목: {sorted(missing)}")
    return buf.getvalue()


def _evil_entries(kind: str) -> list[tuple[zipfile.ZipInfo, bytes]]:
    if kind == "dotdot":
        return [(_fat(EVIL_DOTDOT), b"evil\n")]
    if kind == "abs":
        return [(_fat(EVIL_ABS), b"evil\n")]
    zi = _fat(EVIL_LINK)                       # 유닉스 심볼릭 링크 항목(만든 OS 3, 종류 비트 S_IFLNK)
    zi.create_system = 3
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    return [(zi, b"/etc/passwd")]


def make_variant(base: bytes, name: str, key: "TestKey") -> bytes:
    v = VARIANTS[name]
    ver = v["ver"]
    edits = {
        "pyproject.toml": lambda b: set_pyproject_version(b, ver),
        "uv.lock": lambda b: set_lock_version(b, ver),
        "session_manager/updater.py": lambda b: set_release_keys(b, key.kid, key.pub),
        "README.md": lambda b: (readme_marker(name, ver) + "\n").encode("utf-8") + b,
    }
    if v.get("template"):
        edits[TEMPLATE] = lambda b: mark_template(b, name)
    if v.get("broken"):
        edits["session_manager/server.py"] = break_server
    add = _evil_entries(v["add"]) if v.get("add") else ()
    return rewrite_zip(base, edits, drop=v.get("drop", ()), add=add)


# ---------------------------------------------------------------- 서명(시험 키)

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class TestKey:
    """프로세스 안에서 만든 Ed25519 키. 개인키는 저장소 밖 임시 폴더의 파일 하나(sign_release 가 파일을 받으므로)뿐이고
    close() 가 지운다. kid 규칙은 sign_release 와 같다(공개키 raw 의 SHA-256 앞 16자)."""

    __test__ = False                                   # pytest 가 시험 클래스로 모으지 않게

    def __init__(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self._dir = tempfile.TemporaryDirectory(prefix="clewpath-e2e-key-")
        priv = Ed25519PrivateKey.generate()
        self.pem = Path(self._dir.name) / "test-key.pem"
        self.pem.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
        raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).hexdigest()[:16]
        self.pub = _b64u(raw)

    def close(self) -> None:
        self._dir.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _sign_release():
    spec = importlib.util.spec_from_file_location("sign_release_for_e2e", SIGN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sign(sr, key: TestKey, artifact: Path, ver: str, out: Path, platforms: str) -> str:
    """sign_release.cmd_sign 그대로(운영과 같은 클레임 형식) — 키만 시험 키."""
    a = argparse.Namespace(key=str(key.pem), version=ver, artifact=str(artifact), url=f"{CP}/updates/download/{ver}",
                           out=str(out), notes=NOTES, ttl_days=1, platforms=sr.parse_platforms(platforms))
    if sr.cmd_sign(a) != 0:
        raise FixtureError(f"서명 실패: {artifact.name}")
    return out.read_text(encoding="utf-8")


def resign(token: str, key: TestKey, how: str) -> str:
    """같은 키로 클레임 하나만 바꿔 다시 서명한다(T: sha256·size 변조, L: platforms 클레임 없는 0.28.1 모양)."""
    import jwt as pyjwt
    claims = pyjwt.decode(token, options={"verify_signature": False})
    if how == "sha256":
        claims["sha256"] = claims["sha256"][:-1] + ("0" if claims["sha256"][-1] != "0" else "1")
    elif how == "size+1":
        claims["size"] += 1
    elif how == "size-1":
        claims["size"] -= 1
    elif how == "no-platforms":
        claims.pop("platforms", None)
    else:
        raise FixtureError(how)
    return pyjwt.encode(claims, key.pem.read_bytes(), algorithm="EdDSA", headers={"kid": key.kid})


def broken_jwt() -> str:
    """M: 세 조각 모양은 맞지만 내용(가운데 조각)이 JSON 이 아니다."""
    return ".".join((_b64u(b'{"alg":"EdDSA","typ":"JWT"}'), _b64u(b'{"ver":"91.0.9'), _b64u(b"sig")))


# ---------------------------------------------------------------- uv

def uv_pins(text: str) -> tuple[str, dict[str, str]]:
    """install.sh 의 UV_DEFAULT 와 uv_pinned_sha256 표(그 버전의 대상 → 해시)."""
    ver = re.search(r'^UV_DEFAULT="([0-9.]+)"$', text, re.M)
    if not ver:
        raise FixtureError("install.sh 에서 UV_DEFAULT 를 찾지 못했습니다")
    pins = {t: h for v, t, h in re.findall(r'"([0-9.]+)\|([a-z0-9_-]+)"\)\s+echo "([0-9a-f]{64})"', text)
            if v == ver.group(1)}
    if not pins:
        raise FixtureError("install.sh 에서 uv 고정 해시를 찾지 못했습니다")
    return ver.group(1), pins


def default_uv_target() -> str:
    m = platform.machine().lower()
    return "aarch64-unknown-linux-gnu" if m in ("arm64", "aarch64") else "x86_64-unknown-linux-gnu"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def acquire_uv(dest: Path, target: str, archive: Path | None) -> Path:
    """dest/uv-<target>.tar.gz 를 고정 해시와 맞춘다. 주어진 파일 → 이미 있는 파일 → GitHub 공식 릴리스(.sha256 과도 대조)."""
    ver, pins = uv_pins(INSTALL_SH.read_text(encoding="utf-8"))
    want = pins.get(target)
    if not want:
        raise FixtureError(f"install.sh 에 uv {ver} ({target}) 의 고정 해시가 없습니다")
    out = dest / f"uv-{target}.tar.gz"
    if archive:
        if _sha256_file(archive) != want:
            raise FixtureError(f"--uv-archive 해시가 install.sh 의 고정값과 다릅니다: {archive}")
        if not out.is_file() or _sha256_file(out) != want:
            shutil.copyfile(archive, out)
        return out
    if out.is_file() and _sha256_file(out) == want:
        return out
    url = f"https://github.com/astral-sh/uv/releases/download/{ver}/uv-{target}.tar.gz"
    log(f"uv 내려받기: {url}")
    with tempfile.TemporaryDirectory(prefix="clewpath-e2e-uv-") as td:
        tmp = Path(td) / out.name
        with urllib.request.urlopen(url, timeout=180) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f)
        with urllib.request.urlopen(url + ".sha256", timeout=60) as r:
            published = r.read().decode("ascii", "replace").split()[0].lower()
        got = _sha256_file(tmp)
        if not (got == want == published):
            raise FixtureError(f"uv 해시 불일치: 받은 {got} / 배포처 {published} / install.sh {want}")
        shutil.copyfile(tmp, out)
    return out


# ---------------------------------------------------------------- 컨텍스트 지키기

_PRIVATE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_SUFFIX = (".pem", ".key", ".pfx", ".p12")


def _key_like_name(name: str) -> bool:
    low = name.lower()
    return "release-keys" in low or low.endswith(_KEY_SUFFIX)   # 거부 규칙 # e2e-guard


def guard_context(out: Path) -> None:
    """컨텍스트에 키가 섞였으면 중단한다(S19) — 이름(*.pem·*.key·키 폴더)과 내용(개인키 머리), zip 안까지."""
    bad = []
    for p in out.rglob("*"):
        rel = p.relative_to(out).as_posix()
        if _key_like_name(rel):
            bad.append(rel)
            continue
        if not p.is_file():
            continue
        data = p.read_bytes()
        if _PRIVATE.search(data):
            bad.append(rel)
        if p.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for i in z.infolist():
                    if _key_like_name(i.filename) or _PRIVATE.search(z.read(i)):
                        bad.append(f"{rel}!{i.filename}")
    if bad:
        raise FixtureError("빌드 컨텍스트에 키처럼 보이는 것이 있어 중단합니다: " + ", ".join(sorted(bad)[:10]))


def prepare_out(out: Path) -> Path:
    out = out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise FixtureError(f"--out 은 저장소 밖이어야 합니다: {out}")
    if out.exists() and any(out.iterdir()) and not (out / MARK).is_file():
        raise FixtureError(f"--out 이 비어 있지 않고 이 도구가 만든 폴더도 아닙니다(지우지 않습니다): {out}")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARK).write_text("build_fixtures.py 산출물 — 다시 만들 때 base/ 를 뺀 나머지를 지운다\n", encoding="utf-8")
    for p in out.iterdir():
        if p.name not in (MARK, "base"):
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    return out


# ---------------------------------------------------------------- 흐름

def read_get_sh(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.startswith(b"#!/bin/sh\n"):
        raise FixtureError(f"get.sh 형식이 어긋났습니다(LF·BOM 없음·1행 #!/bin/sh): {path}")
    return raw


def base_zip(work: Path, *, base: Path | None, use_pwsh: bool) -> tuple[bytes, str]:
    if base:
        return base.read_bytes(), f"주어진 zip({base})"
    if use_pwsh and shutil.which("pwsh"):
        return pwsh_package(work), "ops/package-for-user.ps1(pwsh)"
    return python_package(), "파이썬 판(package-for-user.ps1 의 목록)"


def check_base(zb: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        names = z.namelist()
    need = {"install.sh", "uninstall.sh", "pyproject.toml", "uv.lock", "README.md", "session_manager/__init__.py",
            "session_manager/updater.py", "session_manager/server.py", "session_manager/hostctl.py",
            "session_manager/update_runner.sh", TEMPLATE}
    missing = need - set(names)
    if missing:
        raise FixtureError(f"기본 zip 에 필요한 항목이 없습니다: {sorted(missing)}")
    if any("\\" in n for n in names):
        raise FixtureError("기본 zip 항목 이름에 역슬래시가 있습니다(Compress-Archive 5.1 모양) — '/' 여야 합니다")
    return names


def build(out: Path, *, get_sh: Path, uv_archive: Path | None = None, uv_target: str | None = None,
          base: Path | None = None, use_pwsh: bool = True, with_uv: bool = True) -> dict:
    out = prepare_out(out)
    ctx, base_dir = out / "ctx", out / "base"
    fix = ctx / "fixtures"
    (fix / "zips").mkdir(parents=True)
    (fix / "manifests").mkdir(parents=True)
    base_dir.mkdir(exist_ok=True)
    include, _ = ps1_lists(PS1.read_text(encoding="utf-8"))
    sr = _sign_release()
    target = uv_target or default_uv_target()
    with tempfile.TemporaryDirectory(prefix="clewpath-e2e-pkg-") as td:
        zb, how = base_zip(Path(td), base=base, use_pwsh=use_pwsh)
    check_base(zb)
    log(f"기본 zip: {how}, {len(zb)} 바이트")
    index = {"cp": CP, "payload": include, "base_zip": how, "uv_target": target,
             "uv_archive": f"/opt/e2e-base/uv-{target}.tar.gz", "variants": {}, "manifests": {},
             "evil": {"dotdot": EVIL_DOTDOT, "abs": EVIL_ABS, "link": EVIL_LINK}}
    with TestKey() as key:
        index["test_kid"] = key.kid
        tokens = {}
        for name, v in VARIANTS.items():
            zbv = make_variant(zb, name, key)
            zp = fix / "zips" / f"{v['ver']}.zip"
            zp.write_bytes(zbv)
            tokens[name] = sign(sr, key, zp, v["ver"], fix / "manifests" / f"{name}.jwt", v["platforms"] or "win")
            index["variants"][name] = {"ver": v["ver"], "zip": f"zips/{zp.name}", "sha256": hashlib.sha256(zbv).hexdigest(),
                                       "size": len(zbv), "platforms": v["platforms"].split(",") if v["platforms"] else None,
                                       "readme": readme_marker(name, v["ver"]),
                                       "template": template_marker(name) if v.get("template") else None,
                                       "use": v["use"]}
            if name == "A":
                warm = base_dir / "warm"
                shutil.rmtree(warm, ignore_errors=True)
                (warm / "session_manager").mkdir(parents=True)
                with zipfile.ZipFile(io.BytesIO(zbv)) as z:
                    for n in ("pyproject.toml", "uv.lock", "LICENSE", "session_manager/__init__.py"):
                        (warm / n).write_bytes(z.read(n))
        for name, (src, how_) in TAMPERED.items():
            (fix / "manifests" / f"{name}.jwt").write_text(resign(tokens[src], key, how_), encoding="utf-8")
        (fix / "manifests" / "M.jwt").write_text(broken_jwt(), encoding="utf-8")
    for p in sorted((fix / "manifests").glob("*.jwt")):
        index["manifests"][p.stem] = f"manifests/{p.name}"
    (fix / "get.sh").write_bytes(read_get_sh(get_sh))
    index["get_sh_sha256"] = hashlib.sha256((fix / "get.sh").read_bytes()).hexdigest()
    for n in ("e2e_driver.py", "fake_cp.py"):
        shutil.copyfile(HERE / n, ctx / n)
    shutil.copyfile(HERE / "warm_cache.sh", base_dir / "warm_cache.sh")
    shutil.copyfile(HERE / "Dockerfile", out / "Dockerfile")
    if with_uv:
        acquire_uv(base_dir, target, uv_archive)
        for stale in base_dir.glob("uv-*.tar.gz"):
            if stale.name != f"uv-{target}.tar.gz":
                stale.unlink()
    (fix / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    guard_context(out)
    log(f"완료: {out} (변형 {len(VARIANTS)}, 매니페스트 {len(index['manifests'])}, 시험 kid {index['test_kid']})")
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_fixtures", description="맥·리눅스 설치 e2e 재료(docker 빌드 컨텍스트)")
    ap.add_argument("--out", required=True, help="저장소 밖 폴더 — docker build 컨텍스트가 된다")
    ap.add_argument("--get-sh", default=os.environ.get("CLEWPATH_GET_SH") or str(ROOT / "ops" / "get.sh"))
    ap.add_argument("--uv-archive", default=os.environ.get("CLEWPATH_E2E_UV_ARCHIVE") or None,
                    help="install.sh 고정 해시와 같은 uv tarball(주면 내려받지 않는다)")
    ap.add_argument("--uv-target", default=None, help="기본: 이 PC 의 CPU(x86_64·aarch64 리눅스 gnu)")
    ap.add_argument("--base-zip", default=None, help="기본 zip 을 직접 준다(패키징 건너뜀)")
    ap.add_argument("--no-pwsh", action="store_true", help="pwsh 가 있어도 파이썬 판으로 패키징")
    a = ap.parse_args(argv)
    try:
        build(Path(a.out), get_sh=Path(a.get_sh), uv_archive=Path(a.uv_archive) if a.uv_archive else None,
              uv_target=a.uv_target, base=Path(a.base_zip) if a.base_zip else None, use_pwsh=not a.no_pwsh)
    except FixtureError as e:
        print(f"[fixtures] [FAIL] {e}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
