"""get.sh(맥·리눅스 원라인 설치 부트스트랩) 시험 — docs/designs/posix-install.md 3.1·4.6 (+ 6.3 S1, S6·S7·S8·S12·S17·S18).

두 층이다.
- 함수 시험(sh 가 있는 모든 OS — Windows 는 Git 의 sh·dash·bash): 마지막 줄 `main "$@"` 를 뺀 사본을 source 해서 매니페스트
  읽기(JWT 가운데 조각 → 한 줄 ASCII JSON → 키 1회·값 모양·platforms)와 zip 이름 검사를 본다. 매니페스트는 ops/tools/
  sign_release.py 가 실제로 만든 것이다(시험 키는 프로세스 안에서 만들어 tmp 에만). 제품 코드에 시험용 훅은 없다.
- 실행 시험(맥·리눅스, curl 이 있을 때): 가짜 CP(127.0.0.1)가 매니페스트·zip·get.sh 를 준다. zip 안의 install.sh 는 받은
  인자·env·pid·표준 입력을 적는 대역이다 — 진짜 install.sh 로 이어 가는 왕복은 tests/test_install_sh.py 가 한다.
  거부 시나리오(S1)는 매번 설치 폴더가 생기지 않았고 임시 폴더(.clewpath-get.*·TMPDIR 의 clewpath-get.*)가 남지 않았는지 본다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("cryptography")

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GET_SH = Path(os.environ.get("CLEWPATH_GET_SH") or ROOT / "ops" / "get.sh")
SIGN = ROOT / "ops" / "tools" / "sign_release.py"
# 한글·따옴표·역슬래시, 그리고 키처럼 생긴 글자(,"sha256":"…") — JSON 안에서는 \" 로 이스케이프되므로 키로 세면 안 된다
NOTES = '한글 노트 "따옴표" \\역슬래시 ,"sha256":"' + "f" * 64 + '" {끝}'

# get.sh 가 넘기는 install.sh 의 대역 — 받은 것을 적고 GET_TEST_RC 로 끝난다
RECORDER = r'''#!/bin/sh
{
  printf 'pid=%s\n' "$$"
  printf 'self=%s\n' "$0"
  for a in "$@"; do printf 'arg=%s\n' "$a"; done
  printf 'token=%s\n' "${CLEWPATH_CLIENT_TOKEN:-}"
  printf 'agent=%s\n' "${CLEWPATH_AGENT_TOKEN:-}"
  printf 'umask=%s\n' "$(umask)"
  printf 'stdin=%s\n' "$(readlink "/proc/$$/fd/0" 2>/dev/null)"
} >"$GET_TEST_RECORD"
exit "${GET_TEST_RC:-0}"
'''


def _sign_release():
    spec = importlib.util.spec_from_file_location("sign_release_for_get_sh", SIGN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Signer:
    """시험 키(프로세스 안에서 만들어 tmp 에만) — sign_release 의 sign 그대로, 또는 클레임을 손으로 바꾼 토큰."""

    def __init__(self, d: Path):
        self.sr = _sign_release()
        self.d = d
        priv = Ed25519PrivateKey.generate()
        self.pem = d / "test-key.pem"
        self.pem.write_bytes(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
        raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.kid = hashlib.sha256(raw).hexdigest()[:16]

    def sign(self, zip_bytes: bytes, ver: str, url: str, platforms: str = "win,mac,linux") -> str:
        art = self.d / f"clewpath-host-{uuid.uuid4().hex[:8]}.zip"
        art.write_bytes(zip_bytes)
        out = art.with_suffix(".jwt")
        a = argparse.Namespace(key=str(self.pem), version=ver, artifact=str(art), url=url, out=str(out), notes=NOTES,
                               ttl_days=1, platforms=self.sr.parse_platforms(platforms))
        assert self.sr.cmd_sign(a) == 0
        return out.read_text(encoding="utf-8")

    def token(self, claims: dict) -> str:
        return pyjwt.encode(claims, self.pem.read_bytes(), algorithm="EdDSA", headers={"kid": self.kid})


@pytest.fixture(scope="module")
def signer(tmp_path_factory) -> Signer:
    return Signer(tmp_path_factory.mktemp("get-sh-key"))


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_JWT_HEAD = _b64u(b'{"alg":"EdDSA","typ":"JWT"}')


def _raw_token(payload: bytes) -> str:
    """서명은 보지 않으므로(신뢰 모델) 가운데 조각만 마음대로 — 형식 거부 시험용."""
    return f"{_JWT_HEAD}.{_b64u(payload)}.{_b64u(b'sig')}"


def _claims(**over) -> dict:
    c = {"iss": "clewpath-release", "aud": "clewpath-host", "ver": "90.0.0", "url": "https://cp.example/d/90.0.0",
         "sha256": "a" * 64, "size": 1234, "filename": "clewpath-host.zip", "notes": NOTES,
         "platforms": ["win", "mac", "linux"], "iat": 1, "exp": 4_000_000_000}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}


def _fat(name: str) -> zipfile.ZipInfo:
    """Compress-Archive 가 쓰는 항목 모양 — 만든 OS 0(FAT), 속성 0(유닉스 권한 없음). 이름은 날것 그대로
    (ZipInfo() 는 Windows 에서 '\\' 를 '/' 로 바꾼다)."""
    zi = zipfile.ZipInfo("placeholder")
    zi.filename = name
    zi.create_system = 0
    zi.external_attr = 0
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def _zip_bytes(files: dict[str, str | bytes], *, links: dict[str, str] | None = None, dirs=("session_manager/skills/",),
               dup: str | None = None, unix_modes: dict[str, int] | None = None, stored=()) -> bytes:
    """배포 zip 모양(Compress-Archive: 구분자 '/', 하위 폴더만 든 폴더의 항목, 유닉스 권한 없음). unix_modes 는 그 이름을
    유닉스 항목(만든 OS 3)으로 적고 st_mode(종류 비트 포함)를 그대로 싣는다 — 권한이 이상한 zip 시험용."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                          # 같은 이름(dup) 경고
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in dirs:
                zi = _fat(d)
                if d in (unix_modes or {}):
                    zi.create_system, zi.external_attr = 3, unix_modes[d] << 16
                z.writestr(zi, b"")
            for name, data in files.items():
                zi = _fat(name)
                if name in stored:
                    zi.compress_type = zipfile.ZIP_STORED                # 내용이 zip 바이트에 그대로 — 깨뜨리기 시험용
                if name in (unix_modes or {}):
                    zi.create_system, zi.external_attr = 3, unix_modes[name] << 16
                z.writestr(zi, data)
            for name, target in (links or {}).items():
                zi = zipfile.ZipInfo(name)
                zi.create_system = 3
                zi.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(zi, target)
            if dup:
                z.writestr(_fat(dup), "second copy")
    return buf.getvalue()


def _payload(ver: str = "90.0.0") -> dict[str, str]:
    return {
        "install.sh": RECORDER,
        "uninstall.sh": "#!/bin/sh\nexit 0\n",
        "session_manager/__init__.py": "",
        "session_manager/hostctl.py": "",
        "session_manager/skills/clewpath-workers/SKILL.md": "x\n",
        "pyproject.toml": f'[project]\nname = "clewpath-host"\nversion = "{ver}"\n',
        "README.md": "# ClewPath\n",
        "pwa/index.html": "<!doctype html>\n",
    }


# ---------------------------------------------------------------- 함수 시험(source — 모든 OS)

def _find_shells() -> dict[str, list[str]]:
    out = {}
    for sid, argv in (("sh", ["sh"]), ("dash", ["dash"]), ("bash-posix", ["bash", "--posix"])):
        exe = shutil.which(argv[0])
        if exe and "system32" not in exe.lower():         # Windows 의 WSL bash.exe 는 Windows 경로를 못 읽는다
            out[sid] = [exe, *argv[1:]]
    return out


_SHELLS = _find_shells()
with_shells = pytest.mark.parametrize("shell", list(_SHELLS.values()), ids=list(_SHELLS))
# 사례가 많은 시험은 Windows(Git 의 MSYS — 프로세스 하나에 수십 ms, 사례 하나에 스무 개 남짓)에서는 셸 하나로만.
# 세 셸(sh·dash·bash --posix) 모두는 리눅스 컨테이너가 돈다
_MATRIX = _SHELLS if sys.platform != "win32" else dict(list(_SHELLS.items())[:1])
with_matrix_shells = pytest.mark.parametrize("shell", list(_MATRIX.values()), ids=list(_MATRIX))


@pytest.fixture(scope="module")
def lib(tmp_path_factory) -> str:
    """get.sh 에서 마지막 줄(main "$@")만 뺀 사본 — source 하면 함수만 정의된다(맨 위에 부작용이 없다는 계약)."""
    lines = GET_SH.read_bytes().decode("utf-8").rstrip("\n").split("\n")
    assert lines[-1] == 'main "$@"', "get.sh 의 마지막 줄은 main \"$@\""
    p = tmp_path_factory.mktemp("get-sh-lib") / "get-lib.sh"
    p.write_bytes(("\n".join(lines[:-1]) + "\n").encode("utf-8"))
    return p.as_posix()


_READ = r'''pick_b64 || exit 9
ALLOW_HTTP=${ALLOW:-0}
if read_manifest "$1" "$2"; then
  printf 'ok\nver=%s\nsha=%s\nsize=%s\nurl=%s\nplats=%s\n' "$M_VER" "$M_SHA" "$M_SIZE" "$M_URL" "$M_PLATS"
else
  printf 'bad\nwhy=%s\n' "$RM_WHY"
fi
'''


def _read(shell, lib, tmp_path, token: str | bytes, allow_http: bool = False) -> dict:
    m = tmp_path / f"m-{uuid.uuid4().hex[:6]}"
    m.write_bytes(token.encode() if isinstance(token, str) else token)
    env = {**os.environ, "ALLOW": "1" if allow_http else "0"}
    r = subprocess.run([*shell, "-c", '. "$1" || exit 99; shift; ' + _READ, "sh", lib, m.as_posix(),
                        (tmp_path / "claims.json").as_posix()], capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    lines = r.stdout.decode("utf-8").splitlines()
    return {"state": lines[0], **dict(x.split("=", 1) for x in lines[1:])}


@with_shells
def test_reader_reads_what_sign_release_writes(shell, lib, tmp_path, signer):
    """sign_release 가 쓴 진짜 매니페스트(한글·따옴표·역슬래시·키처럼 생긴 글자가 든 notes)를 sed·awk 만으로 읽는다."""
    zb = _zip_bytes(_payload("1.2.3"))
    tok = signer.sign(zb, "1.2.3", "https://cp.example/cp/updates/download/1.2.3", platforms="linux,win")
    got = _read(shell, lib, tmp_path, tok)
    assert got == {"state": "ok", "ver": "1.2.3", "sha": hashlib.sha256(zb).hexdigest(), "size": str(len(zb)),
                   "url": "https://cp.example/cp/updates/download/1.2.3", "plats": "win linux"}


@with_matrix_shells
@pytest.mark.parametrize("plats,want", [
    (None, "win"),                                   # 0.28 까지의 릴리스 — 클레임 없음 = win 전용
    (["win"], "win"),                                # 이번 출시 모양
    (["win", "mac", "linux"], "win mac linux"),
    (["win", "linux-arm"], "win linux-arm"),         # 앞으로의 이름 — 모르는 이름은 그냥 고르지 않을 뿐
    ([], ""),
    ("linux", ""),                                   # 목록이 아니면 아무 OS 도 받지 않는다(updater 와 같다)
    (["win", 1], ""),
    ({"os": "linux"}, ""),
])
def test_reader_platforms(shell, lib, tmp_path, signer, plats, want):
    got = _read(shell, lib, tmp_path, signer.token(_claims(platforms=plats)))
    assert got["state"] == "ok" and got["plats"] == want


def _dup_sha() -> bytes:
    return (b'{"ver":"90.0.0","sha256":"' + b"a" * 64 + b'","url":"https://x/d","size":5,"sha256":"' + b"b" * 64 + b'"}')


def _tok(**over) -> str:
    """운영 모양(압축된 ASCII JSON)에서 클레임 하나만 바꾼 토큰."""
    return _raw_token(json.dumps(_claims(**over), separators=(",", ":")).encode())


@with_matrix_shells
@pytest.mark.parametrize("token,why", [
    pytest.param("hello", "JWT 모양이 아닙니다", id="one-part"),
    pytest.param("<html>a.b.c</html>", "JWT 모양이 아닙니다", id="html"),
    pytest.param("a.b", "세 조각", id="two-parts"),
    pytest.param("a.b.c.d", "세 조각", id="four-parts"),
    pytest.param("a..c", "세 조각", id="empty-payload"),
    pytest.param("a.AAAAA.c", "풀지 못했습니다", id="base64-len-mod-4-is-1"),
    pytest.param(_raw_token(b"not json"), "JSON 객체가 아닙니다", id="not-json"),
    pytest.param(_raw_token(json.dumps(_claims(), indent=1).encode()), "한 줄 ASCII", id="pretty-json"),
    pytest.param(_raw_token(json.dumps(_claims(), ensure_ascii=False, separators=(",", ":")).encode()), "한 줄 ASCII",
                 id="raw-utf8"),
    pytest.param(_raw_token(_dup_sha()), '"sha256" 가 정확히 한 번', id="duplicate-key"),
    pytest.param(_tok(url=None), '"url" 가 정확히 한 번', id="missing-url"),
    pytest.param(_tok(ver="1.2"), "ver", id="ver-two-parts"),
    pytest.param(_tok(ver="1.2.3.4"), "ver", id="ver-four-parts"),
    pytest.param(_tok(ver="v1.2.3"), "ver", id="ver-prefix"),
    pytest.param(_tok(sha256="A" * 64), "sha256", id="sha-upper"),
    pytest.param(_tok(sha256="a" * 63), "sha256", id="sha-short"),
    pytest.param(_tok(size=0), "size", id="size-zero"),
    pytest.param(_tok(size="12"), "size", id="size-string"),
    pytest.param(_tok(size=10**10), "size 가 너무 큽니다", id="size-huge"),
    pytest.param(_tok(url="http://cp.example/d"), "https", id="url-http"),
    pytest.param(_tok(url="ftp://cp.example/d"), "https", id="url-ftp"),
    pytest.param(_tok(url="https://cp.example/a b"), "쓸 수 없는 글자", id="url-space"),
    pytest.param(_tok(url='https://cp.example/a"b'), "https", id="url-quote"),
    pytest.param("a." + "A" * 70_000 + ".c", "너무 큽니다", id="too-big"),
])
def test_reader_rejects(shell, lib, tmp_path, token, why):
    got = _read(shell, lib, tmp_path, token)
    assert got["state"] == "bad" and why in got["why"], got


@with_shells
def test_reader_takes_http_url_only_with_allow_http(shell, lib, tmp_path, signer):
    tok = signer.token(_claims(url="http://127.0.0.1:8099/cp/updates/download/90.0.0"))
    assert _read(shell, lib, tmp_path, tok)["state"] == "bad"
    assert _read(shell, lib, tmp_path, tok, allow_http=True)["url"] == "http://127.0.0.1:8099/cp/updates/download/90.0.0"


@with_matrix_shells
@pytest.mark.parametrize("names,why", [
    (["install.sh", "session_manager/__init__.py", "session_manager/skills/", "pwa/index.html",
      "session_manager/a..b.py"], None),
    (["../evil"], "'..'"),
    (["a/../../evil"], "'..'"),
    (["session_manager/.."], "'..'"),
    (["/etc/x"], "절대 경로"),
    (["session_manager\\evil.py"], "역슬래시"),
    ([".venv/bin/python"], "점으로 시작하는 최상위 이름: .venv"),
    (["./x"], "점으로 시작하는 최상위 이름"),
    (["start-connector.sh"], "런처"),
    (["README.md", "readme.md"], "같은 이름이 두 번"),
    (["pwa/", "pwa"], "같은 이름이 두 번"),
    (["a\x01b"], "제어문자"),
    (["^A../evil"], "제어문자"),                     # unzip 목록이 '\001../evil' 을 이렇게 보여 준다(풀 때는 '../evil')
    (["a\tb"], "제어문자"),
    (["ok", ""], "빈 이름"),
    ([], "빈 압축 파일"),
])
def test_name_checks(shell, lib, tmp_path, names, why):
    f = tmp_path / "names"
    f.write_bytes(("".join(n + "\n" for n in names)).encode("utf-8"))
    r = subprocess.run([*shell, "-c", '. "$1" || exit 99; if check_names "$2"; then echo ok; else echo "bad:$CN_WHY"; fi',
                        "sh", lib, f.as_posix()], capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8").strip()
    assert out == "ok" if why is None else (out.startswith("bad:") and why in out), out


@with_shells
def test_name_checks_allow_non_ascii(shell, lib, tmp_path):
    """한글 이름은 제어문자가 아니다(맥·리눅스 sh). Git for Windows 의 dash 만 UTF-8 의 이어지는 바이트(0x80-0x9F)를
    로캘과 상관없이 제어문자로 본다 — 시험 하네스의 사정이라 그 셸에서만 건너뛴다."""
    if sys.platform == "win32" and Path(shell[0]).stem == "dash":
        pytest.skip("Git for Windows 의 dash 는 0x80-0x9F 바이트를 [[:cntrl:]] 로 본다(리눅스·맥 sh 는 아님)")
    f = tmp_path / "names"
    f.write_bytes("install.sh\npwa/클루패스 파일.txt\n".encode("utf-8"))
    r = subprocess.run([*shell, "-c", '. "$1" || exit 99; if check_names "$2"; then echo ok; else echo "bad:$CN_WHY"; fi',
                        "sh", lib, f.as_posix()], capture_output=True, timeout=60)
    assert r.stdout.decode("utf-8").strip() == "ok"


# ---------------------------------------------------------------- 실행 시험(맥·리눅스)

posix = pytest.mark.skipif(sys.platform == "win32", reason="get.sh 실행 — 맥·리눅스 전용")
need_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl 없음(리눅스 컨테이너는 apt 로 넣는다)")
need_unzip = pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip 없음")


class FakeCP:
    """가짜 컨트롤플레인 — 길(path)마다 (상태, 본문, 형식). 받은 요청을 적는다."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, str]] = {"/cp/get.sh": (200, GET_SH.read_bytes(), "text/plain")}
        self.requests: list[str] = []
        box = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                box.requests.append(self.path)
                code, body, ctype = box.routes.get(self.path, (404, b'{"error":"not_found"}', "application/json"))
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/cp"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def manifest(self, body: str | bytes | None, code: int = 200) -> None:
        if body is None:
            self.routes["/cp/updates/manifest"] = (404, b'{"error":"no_release"}', "application/json")
        else:
            self.routes["/cp/updates/manifest"] = (code, body.encode() if isinstance(body, str) else body,
                                                   "application/jwt")

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@dataclass
class Run:
    rc: int
    out: str
    err: str
    pid: int

    def __str__(self) -> str:                    # 실패 메시지에 출력 전체를(pytest 의 repr 줄임 없이)
        return f"rc={self.rc}\n--- stdout\n{self.out}\n--- stderr\n{self.err}"


ON_MAC = sys.platform == "darwin"
_LINUX_UNAME = '#!/bin/sh\ncase "${1:-}" in -m) m=$(/usr/bin/uname -m); [ "$m" = arm64 ] && m=aarch64; echo "$m" ;; *) echo Linux ;; esac\n'
TOOLS = ("uname", "id", "curl", "mktemp", "tr", "sed", "awk", "wc", "find", "dirname", "sort", "uniq", "cat", "rm",
         "mkdir", "chmod", "ls", "grep", "sha256sum", "base64", "unzip", "python3", "readlink", "openssl")


class GBox:
    """가짜 홈 하나(공백·한글 경로) — 설치 위치는 공통 경로 블록의 리눅스 기본값($HOME/.local/share/clewpath/app).
    진짜 맥에서는 리눅스 길을 가짜 uname(Linux)으로 흉내 내고 홈은 ASCII 다(맥은 ASCII 밖 설치 경로를 거부한다 — C5).
    PATH 를 직접 주는 시험은 tools() 로 만든다(같은 가짜 uname 이 든다. shims 의 uname 이 있으면 그것)."""

    def __init__(self, tmp: Path, cp: FakeCP, signer: Signer):
        self.base = tmp.resolve()
        self.home = self.base / "home x" / ("hom" if ON_MAC else "홈")
        self.tmpdir = self.base / "tmp"
        self.chome = self.home / ".local" / "share" / "clewpath"
        self.dir = self.chome / "app"
        self.record = self.base / "record.txt"
        self.cp, self.signer = cp, signer
        self.base_args = ["--cp-url", cp.url, "--allow-http", "--no-provision"]
        for d in (self.home, self.tmpdir):
            d.mkdir(parents=True)
        self.os_tools = self.base / "linux uname"
        if ON_MAC:
            self.os_tools.mkdir()
            (self.os_tools / "uname").write_text(_LINUX_UNAME, encoding="utf-8")
            os.chmod(self.os_tools / "uname", 0o755)

    def env(self, **extra) -> dict:
        e = {k: v for k, v in os.environ.items()
             if not k.startswith(("CLEWPATH_", "SM_", "GET_TEST_")) and k.lower() not in
             ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "xdg_data_home", "session_manager_claude_home")}
        e.update(HOME=str(self.home), TMPDIR=str(self.tmpdir), LANG="C.UTF-8", GET_TEST_RECORD=str(self.record))
        if ON_MAC and "PATH" not in extra:
            e["PATH"] = f"{self.os_tools}:{e.get('PATH', '/usr/bin:/bin')}"
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def publish(self, ver: str = "90.0.0", *, files: dict | None = None, drop=(), links=None, dup=None,
                platforms: str | None = "win,mac,linux", claims: dict | None = None, size_delta: int = 0,
                zip_bytes: bytes | None = None, serve_zip: bool = True) -> bytes:
        payload = {k: v for k, v in _payload(ver).items() if k not in drop}
        payload.update(files or {})
        zb = zip_bytes if zip_bytes is not None else _zip_bytes(payload, links=links, dup=dup)
        url = f"{self.cp.url}/updates/download/{ver}"
        if claims is None and platforms is not None and not size_delta and zip_bytes is None:
            tok = self.signer.sign(zb, ver, url, platforms=platforms)          # 운영과 같은 도구로
        else:
            plats = None if platforms is None else self.signer.sr.parse_platforms(platforms)
            c = _claims(ver=ver, url=url, sha256=hashlib.sha256(zb).hexdigest(), size=len(zb) + size_delta,
                        platforms=plats)
            c.update(claims or {})
            tok = self.signer.token(c)
        self.cp.manifest(tok)
        if serve_zip:
            self.cp.routes[f"/cp/updates/download/{ver}"] = (200, zb, "application/zip")
        return zb

    def run(self, *args, env=None, pipe=False, umask: str | None = None, timeout=120) -> Run:
        if pipe:
            argv = ["/bin/sh", "-c", 'u=$1; shift; curl -fsS "$u" | /bin/sh -s -- "$@"', "sh", f"{self.cp.url}/get.sh",
                    *args]
        else:
            argv = ["/bin/sh", str(GET_SH), *args]
        if umask:
            argv = ["/bin/sh", "-c", f'umask {umask}; exec "$@"', "sh", *argv]
        p = subprocess.Popen(argv, env=env or self.env(), cwd=str(self.home), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=timeout)
        return Run(p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), p.pid)

    def rec(self) -> dict:
        d: dict = {"arg": []}
        for line in self.record.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k == "arg":
                d["arg"].append(v)
            else:
                d[k] = v
        return d

    def leftovers(self) -> list[str]:
        """임시 폴더가 남았나 — 설치 폴더 옆 .clewpath-get.*(stage), TMPDIR 의 clewpath-get.*(받은 zip)."""
        left = [p.name for p in self.tmpdir.iterdir()]
        if self.chome.exists():
            left += [p.name for p in self.chome.iterdir() if p.name.startswith(".clewpath-get.")]
        return left

    def tools(self, drop=(), shims: dict[str, str] | None = None) -> str:
        """PATH 를 좁힌다 — 고른 도구만(심볼릭 링크), shims 는 가짜(스크립트)로 바꿔 끼운다."""
        d = self.base / f"tools-{uuid.uuid4().hex[:6]}"
        d.mkdir()
        for t in TOOLS:
            src = shutil.which(t)
            if t == "uname" and ON_MAC and t not in drop:
                (d / t).write_text(_LINUX_UNAME, encoding="utf-8")       # 진짜 맥에서도 리눅스 길(shims 가 있으면 아래서 바뀐다)
                os.chmod(d / t, 0o755)
            elif t not in drop and src:
                (d / t).symlink_to(src)
        for name, body in (shims or {}).items():
            p = d / name
            if p.is_symlink() or p.exists():
                p.unlink()
            p.write_text(body, encoding="utf-8")
            os.chmod(p, 0o755)
        return str(d)


@pytest.fixture
def gbox(tmp_path, signer):
    cp = FakeCP()
    try:
        yield GBox(tmp_path, cp, signer)
    finally:
        cp.close()


def _stage_files(stage: Path) -> list[str]:
    return sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())


def _zip_files(zb: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        return sorted(n for n in z.namelist() if not n.endswith("/"))


@posix
@need_curl
@need_unzip
def test_hands_over_to_the_stage_installer(gbox):
    """검사를 통과하면 설치 폴더 옆 임시 stage 에 풀고 stage 의 install.sh 로 exec 한다(pid·umask 그대로, 표준 입력은
    /dev/null, 인자는 --from-stage·--dir·--cp-url·--port 와 넘길 옵션). 설치 폴더는 건드리지 않는다(옮기기는 install.sh)."""
    zb = gbox.publish()
    r = gbox.run(*gbox.base_args, "--port", "5123", "--relay-url", "ws://127.0.0.1:9/relay/ws",
                 "--app-url", "http://127.0.0.1:9/app", "--no-autostart", umask="027",
                 env=gbox.env(CLEWPATH_AGENT_TOKEN="agent-SECRET-2"))
    assert r.rc == 0, str(r)
    rec = gbox.rec()
    stage = Path(rec["self"]).parent
    assert int(rec["pid"]) == r.pid, "exec — install.sh 는 get.sh 의 pid 로 표식을 쥔다"
    assert stage.name == "stage" and stage.parent.name.startswith(".clewpath-get.") and stage.parent.parent == gbox.chome
    assert rec["arg"] == ["--from-stage", str(stage), "--dir", str(gbox.dir), "--cp-url", gbox.cp.url, "--port", "5123",
                          "--relay-url", "ws://127.0.0.1:9/relay/ws", "--app-url", "http://127.0.0.1:9/app",
                          "--no-autostart", "--no-provision", "--allow-http"]
    assert rec["agent"] == "agent-SECRET-2" and rec["token"] == "" and rec["umask"] == "0027"
    if Path("/proc/self").is_dir():
        assert rec["stdin"] == "/dev/null", "curl | sh 의 표준 입력(스크립트)을 install.sh 가 읽지 않게"
    assert _stage_files(stage) == _zip_files(zb) and (stage / "install.sh").read_bytes() == RECORDER.encode()
    assert not gbox.dir.exists(), "설치 폴더는 install.sh 가 만든다"
    assert gbox.leftovers() == [stage.parent.name], "받은 zip 의 임시 폴더는 치우고 stage 만 install.sh 에 넘긴다"
    for s in ("=== ClewPath Host 받기 ===", f"설치 위치 : {gbox.dir}", "최신 버전 : 90.0.0",
              "[OK]   무결성 확인 (SHA-256)", "[OK]   압축 해제"):
        assert s in r.out, (s, r.out)
    if sys.platform.startswith("linux"):
        assert "리눅스는 시험용입니다" in r.out
    assert "SECRET" not in r.out + r.err and "--allow-http" in r.out
    assert gbox.cp.requests == ["/cp/updates/manifest", "/cp/updates/download/90.0.0"]


@posix
@need_curl
@need_unzip
def test_runs_from_a_pipe_and_passes_the_exit_code(gbox):
    """curl … | sh -s -- … 그대로. 성공하면 exec 하므로 install.sh 의 종료코드가 그대로 사용자에게 간다."""
    gbox.publish()
    r = gbox.run(*gbox.base_args, pipe=True, env=gbox.env(GET_TEST_RC="7"))
    assert r.rc == 7, str(r)
    rec = gbox.rec()
    assert rec["arg"][0] == "--from-stage" and gbox.cp.requests[0] == "/cp/get.sh"
    if Path("/proc/self").is_dir():
        assert rec["stdin"] == "/dev/null", "표준 입력(스크립트를 읽던 파이프)을 install.sh 에 물려주지 않는다"


@posix
@need_curl
def test_truncated_download_runs_nothing(gbox):
    """본문이 main() 안에 있다 — curl | sh 가 중간에 끊겨도 아무것도 하지 않는다(요청·폴더 없음). 맨 끝 줄 앞에서 끊겨도."""
    gbox.publish()
    full = GET_SH.read_bytes()
    last = full.rstrip(b"\n").rfind(b"\n") + 1                    # 마지막 줄(main "$@")이 시작하는 자리
    cuts = sorted({len(full) * k // 10 for k in range(1, 10)} | {last, last + 3})
    for cut in cuts:
        gbox.cp.routes["/cp/get.sh"] = (200, full[:cut], "text/plain")
        gbox.cp.requests.clear()
        gbox.run(*gbox.base_args, pipe=True)
        assert gbox.cp.requests == ["/cp/get.sh"], (cut, gbox.cp.requests)
        assert not gbox.chome.exists() and gbox.leftovers() == [] and not gbox.record.exists(), cut


def _refusal_cases():
    def slip(b):
        b.publish(files={"../evil.txt": "x"})

    return {
        "no-release": ("아직 게시된 릴리스가 없습니다", lambda b: b.cp.manifest(None)),
        "server-error": ("릴리스 서버에 닿지 못했습니다: HTTP 500", lambda b: b.cp.manifest(b"boom", code=500)),
        "not-a-jwt": ("JWT 모양이 아닙니다", lambda b: b.cp.manifest(b"<html>hello</html>")),
        "duplicate-key": ('"sha256" 가 정확히 한 번', lambda b: b.cp.manifest(_raw_token(_dup_sha()))),
        "legacy-no-platforms": ("이 릴리스(90.0.0)는 맥·리눅스 설치를 지원하지 않습니다", lambda b: b.publish(platforms=None)),
        "windows-only": ("이 릴리스(90.0.0)는 맥·리눅스 설치를 지원하지 않습니다", lambda b: b.publish(platforms="win")),
        "platforms-not-a-list": ("지원하지 않습니다", lambda b: b.publish(claims={"platforms": "linux"})),
        "no-install-sh": ("이 릴리스(90.0.0)는 맥·리눅스 설치를 지원하지 않습니다",
                          lambda b: b.publish(drop=("install.sh",))),
        "sha-mismatch": ("SHA-256 불일치 - 받은 파일을 버립니다", lambda b: b.publish(claims={"sha256": "0" * 64})),
        "size-smaller": ("크기 불일치 - 다시 시도하세요", lambda b: b.publish(size_delta=-1)),
        "size-larger": ("크기 불일치 - 다시 시도하세요", lambda b: b.publish(size_delta=1)),
        "zip-missing": ("내려받지 못했습니다", lambda b: b.publish(serve_zip=False)),
        "corrupt-zip": ("받은 파일을 풀 수 없습니다", lambda b: b.publish(zip_bytes=b"PK\x03\x04" + b"garbage" * 40)),
        "zip-slip": ("'..' 가 든 이름", slip),
        "absolute-path": ("절대 경로", lambda b: b.publish(files={str(b.base / "evil-abs.txt"): "x"})),
        "backslash": ("역슬래시", lambda b: b.publish(files={"session_manager\\evil.py": "x"})),
        "dot-top-level": ("점으로 시작하는 최상위 이름: .venv", lambda b: b.publish(files={".venv/bin/python": "x"})),
        "launcher-name": ("start-connector.sh", lambda b: b.publish(files={"start-connector.sh": "#!/bin/sh\n"})),
        "symlink": ("심볼릭 링크", lambda b: b.publish(links={"session_manager/evil-link": "/etc/passwd"})),
        "duplicate-name": ("같은 이름이 두 번", lambda b: b.publish(dup="README.md")),
        "case-duplicate": ("같은 이름이 두 번", lambda b: b.publish(files={"readme.md": "x"})),
        "control-char": ("제어문자", lambda b: b.publish(files={"\x01../evil-ctl.txt": "x"})),
    }


@posix
@need_curl
@need_unzip
@pytest.mark.parametrize("case", list(_refusal_cases()))
def test_refuses_and_leaves_nothing_behind(gbox, case):
    """S1 — 게시 없음·서버 오류·JWT 모양·키 겹침·지원하지 않는 OS(클레임 없음 = win 전용)·해시·크기·zip 이름(zip-slip·
    절대 경로·역슬래시·점 이름·런처 이름·심볼릭 링크·같은 이름·제어문자). 종료코드 1, 설치 폴더·임시 폴더·바깥 파일 없음."""
    said, setup = _refusal_cases()[case]
    setup(gbox)
    r = gbox.run(*gbox.base_args)
    assert r.rc == 1 and said in r.out, str(r)
    assert not gbox.dir.exists() and gbox.leftovers() == [] and not gbox.record.exists(), gbox.leftovers()
    assert not [p for p in gbox.base.rglob("evil*")], "zip 밖으로 아무것도 쓰지 않았다"


@posix
@need_curl
def test_unreachable_control_plane(gbox):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    r = gbox.run("--cp-url", f"http://127.0.0.1:{dead}/cp", "--allow-http")
    assert r.rc == 1 and "릴리스 서버에 닿지 못했습니다" in r.out and "(curl 7)" in r.out, str(r)
    assert gbox.leftovers() == [] and not gbox.chome.exists()


@posix
@need_curl
@need_unzip
def test_loopback_control_plane_skips_the_proxy(gbox):
    """사용자의 프록시 설정은 따르되 이 PC(시험용 CP)로 가는 요청만 건너뛴다 — 죽은 프록시가 있어도 받는다."""
    gbox.publish()
    dead = "http://127.0.0.1:9"
    r = gbox.run(*gbox.base_args, env=gbox.env(http_proxy=dead, https_proxy=dead, HTTP_PROXY=dead, HTTPS_PROXY=dead,
                                                ALL_PROXY=dead))
    assert r.rc == 0, str(r)


@posix
@pytest.mark.parametrize("argv,said", [
    (["--bogus"], "모르는 인자: --bogus"),
    (["leaked-SECRET-value"], "값은 보이지 않게"),
    (["--port", "0"], "--port"),
    (["--port", "70000"], "--port"),
    (["--port", "05100"], "--port"),
    (["--port"], "값이 없습니다"),
    (["--no-provision=yes"], "값을 받지 않습니다"),
    (["--cp-url", "http://cp.example/cp"], "https://"),          # 시험용 http 는 --allow-http 일 때만
    (["--cp-url", "https://cp.example/c p"], "쓸 수 없는 글자"),
    (["--relay-url", "ws://r.example/ws"], "wss://"),
    (["--app-url", "http://a.example/app"], "https://"),
    (["--dir", "relative/app"], "절대 경로"),
    (["--dir", "/opt/../etc/app"], "절대 경로"),
    (["--client-token", "a\nb"], "제어문자"),
])
def test_usage_errors_touch_nothing(gbox, argv, said):
    r = gbox.run(*argv)
    assert r.rc == 2 and said in r.out, str(r)
    assert "SECRET" not in r.out + r.err and gbox.cp.requests == [] and not gbox.chome.exists()


@posix
def test_help_and_home_dir(gbox):
    r = gbox.run("--help")
    assert r.rc == 0 and "쓰는 법" in r.out and gbox.cp.requests == []
    r = gbox.run(*gbox.base_args, "--dir", str(gbox.home) + "/")
    assert r.rc == 2 and "/ 나 홈" in r.out


@posix
@need_curl
@pytest.mark.parametrize("make,said", [
    ("file", "폴더가 아닌 것"),
    ("foreign", "ClewPath 설치본도 아닙니다"),
    ("git", "개발 체크아웃"),
    ("ops", "개발 체크아웃"),
])
def test_refuses_unusable_dirs_before_downloading(gbox, make, said):
    """install.sh 서막과 같은 거절을 내려받기 전에(헛걸음 없이) — 남의 폴더는 그대로 둔다."""
    gbox.publish()
    gbox.chome.mkdir(parents=True)
    if make == "file":
        gbox.dir.write_text("not a dir", encoding="utf-8")
    else:
        gbox.dir.mkdir()
        (gbox.dir / "mine.txt").write_text("mine", encoding="utf-8")
        if make in ("git", "ops"):
            (gbox.dir / ("." + make if make == "git" else make)).mkdir()
    before = sorted(p.name for p in gbox.dir.iterdir()) if gbox.dir.is_dir() else None
    r = gbox.run(*gbox.base_args)
    assert r.rc == 1 and said in r.out, str(r)
    assert gbox.cp.requests == [] and gbox.leftovers() == []
    assert (sorted(p.name for p in gbox.dir.iterdir()) if gbox.dir.is_dir() else None) == before


@posix
@need_curl
@need_unzip
def test_reinstall_target_and_symlinked_dir(gbox):
    """이미 설치된 폴더(설치 기록)는 받는다. 설치 폴더가 심볼릭 링크면 물리 경로로 넘긴다 — install.sh 가 pwd -P 로 맞춰
    보는 자리와 같아야 stage(임시 폴더)를 그 옆에 만들고 치울 수 있다."""
    gbox.publish()
    real = gbox.base / "real place" / "app"
    real.mkdir(parents=True)
    (real / ".clewpath-install").write_text("home=x\n", encoding="utf-8")
    (real / "my-notes.txt").write_text("keep", encoding="utf-8")
    gbox.chome.mkdir(parents=True)
    gbox.dir.symlink_to(real)
    r = gbox.run(*gbox.base_args)
    assert r.rc == 0, str(r)
    rec = gbox.rec()
    assert rec["arg"][rec["arg"].index("--dir") + 1] == str(real)
    assert Path(rec["self"]).parent.parent.parent == real.parent, "stage 는 물리 설치 폴더의 옆(같은 파일 시스템)"


@posix
@need_curl
@need_unzip
def test_token_argument_moves_to_env_with_a_warning(gbox):
    gbox.publish()
    r = gbox.run(*gbox.base_args, "--client-token", "tok-SECRET-1")
    assert r.rc == 0 and r.out.count("같은 PC 의 다른 사용자에게 보입니다(ps)") == 1, str(r)
    rec = gbox.rec()
    assert rec["token"] == "tok-SECRET-1" and not any("SECRET" in a for a in rec["arg"])
    assert "tok-SECRET" not in r.out + r.err
    r = gbox.run(*gbox.base_args, env=gbox.env(CLEWPATH_CLIENT_TOKEN="tok-ENV-2"))
    assert r.rc == 0 and "다른 사용자에게 보입니다" not in r.out and gbox.rec()["token"] == "tok-ENV-2"


@posix
@need_curl
@need_unzip
def test_root_needs_allow_root(gbox):
    gbox.publish()
    real_id = shutil.which("id")
    path = gbox.tools(shims={"id": f'#!/bin/sh\n[ "$1" = -u ] && {{ echo 0; exit 0; }}\nexec "{real_id}" "$@"\n'})
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=path))
    assert r.rc == 1 and "root" in r.out and gbox.cp.requests == [], str(r)
    r = gbox.run(*gbox.base_args, "--allow-root", env=gbox.env(PATH=path))
    assert r.rc == 0 and "--allow-root" in gbox.rec()["arg"] and "root 로 설치합니다(시험 전용)" in r.out, str(r)


@posix
def test_unsupported_os(gbox):
    path = gbox.tools(shims={"uname": '#!/bin/sh\necho FreeBSD\n'})
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=path))
    assert r.rc == 1 and "지원하지 않는 OS 입니다: FreeBSD" in r.out and gbox.cp.requests == [], str(r)


@posix
@need_curl
def test_python_zipfile_when_unzip_is_missing(gbox):
    """[linux] unzip 이 없으면 python3 -I 의 zipfile 로 — 이름 검사(심볼릭 링크·제어문자 포함)는 같다."""
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 길")
    zb = gbox.publish()
    path = gbox.tools(drop=("unzip",))
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=path))
    assert r.rc == 0, str(r)
    assert _stage_files(Path(gbox.rec()["self"]).parent) == _zip_files(zb)
    for kw, said in (({"links": {"session_manager/l": "/etc/passwd"}}, "심볼릭 링크"),
                     ({"files": {"pwa/a\x01b.txt": "x"}}, "제어문자"),
                     ({"files": {"../evil.txt": "x"}}, "'..'")):
        for p in gbox.chome.glob(".clewpath-get.*"):
            shutil.rmtree(p)
        gbox.record.unlink(missing_ok=True)
        gbox.publish(**kw)
        r = gbox.run(*gbox.base_args, env=gbox.env(PATH=path))
        assert r.rc == 1 and said in r.out and gbox.leftovers() == [], (kw, str(r))


@posix
@need_curl
def test_missing_tools_are_named(gbox):
    gbox.publish()
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("curl",))))
    assert r.rc == 1 and "필요한 도구가 없습니다" in r.out and "curl" in r.out and gbox.leftovers() == [], str(r)
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("unzip", "python3"))))
    assert r.rc == 1 and "unzip" in r.out, str(r)
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("sha256sum", "openssl"))))
    assert r.rc == 1 and "sha256sum" in r.out, str(r)
    assert gbox.cp.requests == [], "도구를 먼저 본다"


_DARWIN = '#!/bin/sh\ncase "$1" in -m) echo arm64 ;; *) echo Darwin ;; esac\n'


@posix
@need_curl
@need_unzip
def test_mac_path_installs_under_application_support(gbox):
    """[mac] 흉내(uname): 기본 위치 ~/Library/Application Support/ClewPath/app(공백 경로), 리눅스 경고 없음.
    맥은 ASCII 경로만 받으므로(C5) 홈도 ASCII 로(공백은 그대로)."""
    gbox.publish()
    home = gbox.base / "mac home" / "user"
    home.mkdir(parents=True)
    r = gbox.run(*gbox.base_args, env=gbox.env(HOME=home, PATH=gbox.tools(shims={"uname": _DARWIN})))
    assert r.rc == 0, str(r)
    want = home / "Library" / "Application Support" / "ClewPath" / "app"
    rec = gbox.rec()
    assert rec["arg"][rec["arg"].index("--dir") + 1] == str(want) and "리눅스는 시험용" not in r.out
    assert Path(rec["self"]).parent.parent.parent == want.parent


@posix
@need_curl
def test_mac_never_runs_python(gbox):
    """[mac] 은 unzip 이 없어도 python3 를 부르지 않는다(/usr/bin/python3 는 CLT 설치 대화상자일 수 있다)."""
    gbox.publish()
    log = gbox.base / "python3-called"
    path = gbox.tools(drop=("unzip",), shims={"uname": _DARWIN,
                                               "python3": f'#!/bin/sh\necho called >>"{log}"\nexit 1\n'})
    r = gbox.run(*gbox.base_args, "--dir", str(gbox.base / "mac-app"), env=gbox.env(PATH=path))   # 맥은 ASCII 경로(C5)
    assert r.rc == 1 and "unzip" in r.out and not log.exists(), str(r)


@posix
@need_curl
@need_unzip
def test_odd_unix_modes_in_the_zip(gbox):
    """zip 에 유닉스 권한이 적혀 있으면 unzip 이 그대로 쓴다(배포 zip 은 Windows 에서 만들어 없다). 권한 0 파일·폴더는 내
    권한만 채워 읽고 옮길 수 있게 넘긴다. 종류 비트도 없는 항목처럼 풀다 막히는 zip 은 실패로 끝내되 임시 폴더를 남기지
    않는다(지울 수 있게 권한부터 채운다)."""
    payload = _payload()
    for modes in ({"pwa/index.html": stat.S_IFREG}, {"session_manager/skills/": stat.S_IFDIR}):
        gbox.publish(zip_bytes=_zip_bytes(payload, unix_modes=modes))
        r = gbox.run(*gbox.base_args)
        assert r.rc == 0, (modes, str(r))
        stage = Path(gbox.rec()["self"]).parent
        assert (stage / "pwa" / "index.html").read_text(encoding="utf-8") == payload["pwa/index.html"]
        assert (stage / "session_manager" / "skills" / "clewpath-workers" / "SKILL.md").is_file()
        assert all(stat.S_IMODE(p.stat().st_mode) & 0o700 == (0o700 if p.is_dir() else 0o600) | (stat.S_IMODE(p.stat().st_mode) & 0o100)
                   for p in stage.rglob("*")), "내 권한(읽기·쓰기, 폴더는 들어가기)이 다 있다"
        for p in gbox.chome.glob(".clewpath-get.*"):
            shutil.rmtree(p)
        gbox.record.unlink()
    # 앞 항목들(권한 0 폴더 포함)을 푼 뒤 마지막 항목의 CRC 가 틀려 unzip 이 실패한다 — unzip 은 실패해도 폴더 권한(0)을
    # 끝에 적용하므로, 권한을 채우지 않고 지우면 임시 폴더가 남는다
    tail = b"CRC-CHECK-" * 64
    zb = _zip_bytes({**payload, "zzz-last.bin": tail}, unix_modes={"session_manager/skills/": stat.S_IFDIR},
                    stored=("zzz-last.bin",))
    assert zb.count(tail) == 1
    gbox.publish(zip_bytes=zb.replace(tail, b"X" * len(tail)))
    r = gbox.run(*gbox.base_args)
    assert r.rc == 1 and "압축을 풀지 못했습니다" in r.out, str(r)
    assert gbox.leftovers() == [] and not gbox.record.exists(), gbox.leftovers()


@posix
@need_curl
@need_unzip
def test_hash_and_base64_fallbacks(gbox):
    """옛 맥(base64 -D 만, shasum)과 openssl 만 있는 PC 에서도 같은 결과."""
    zb = gbox.publish()
    real_b64, real_sha = shutil.which("base64"), shutil.which("sha256sum")
    sha_cmd = f'"{real_sha}"' if real_sha else f'"{shutil.which("shasum")}" -a 256'    # 맥에는 sha256sum 이 없을 수 있다
    old_mac = {"base64": f'#!/bin/sh\n[ "$1" = -D ] || {{ echo "base64: invalid option" >&2; exit 64; }}\nexec "{real_b64}" -d\n',
               "shasum": f'#!/bin/sh\n[ "$1 $2" = "-a 256" ] || exit 2\nexec {sha_cmd}\n'}
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("sha256sum", "openssl"), shims=old_mac)))
    assert r.rc == 0, str(r)
    assert _stage_files(Path(gbox.rec()["self"]).parent) == _zip_files(zb)
    if shutil.which("openssl"):
        for p in gbox.chome.glob(".clewpath-get.*"):
            shutil.rmtree(p)
        r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("sha256sum", "base64"))))
        assert r.rc == 0, str(r)


# ---------------------------------------------------------------- 2회차 검토 반영(G1·G2·C1·C2·C5)

# busybox unzip(Alpine 기본) 흉내 — 목록 모드(-Z·-Z1)를 모른다. 그 밖의 호출은 오지 않아야 한다
_BUSYBOX_UNZIP = ('#!/bin/sh\ncase "$1" in -Z*) echo "unzip: unrecognized option: Z" >&2; exit 1 ;; esac\n'
                  'echo "unexpected unzip call" >&2\nexit 99\n')


def _no_get_tmp(gbox) -> bool:
    """설치 폴더 옆 stage(.clewpath-get.*)가 가짜 홈 밖(설치 폴더를 바꾼 시험)에도 남지 않았나."""
    return gbox.leftovers() == [] and not list(gbox.base.rglob(".clewpath-get.*"))


@posix
@need_curl
def test_unzip_without_zipinfo_mode_falls_back_to_python(gbox):
    """G1 — [linux] PATH 의 unzip 이 목록 모드(-Z)를 모르면(busybox) '손상'으로 끝내지 않고 python3 대역으로 간다.
    python3 도 없으면 받기 전에 원인(Info-ZIP unzip 을 설치하라)을 알린다."""
    if not sys.platform.startswith("linux"):
        pytest.skip("리눅스 길")
    zb = gbox.publish()
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(shims={"unzip": _BUSYBOX_UNZIP})))
    assert r.rc == 0, str(r)
    assert _stage_files(Path(gbox.rec()["self"]).parent) == _zip_files(zb)
    for p in gbox.chome.glob(".clewpath-get.*"):
        shutil.rmtree(p)
    gbox.cp.requests.clear()
    r = gbox.run(*gbox.base_args, env=gbox.env(PATH=gbox.tools(drop=("python3",), shims={"unzip": _BUSYBOX_UNZIP})))
    assert r.rc == 1 and "unzip(Info-ZIP)" in r.out and "apk add unzip" in r.out and "손상" not in r.out, str(r)
    assert gbox.cp.requests == [] and _no_get_tmp(gbox)


@posix
@need_curl
def test_mac_refuses_unzip_without_zipinfo_mode(gbox):
    """G1 — [mac] 은 python3 대역이 없으므로 목록 모드가 없는 unzip 이면 받기 전에 원인을 알리고 끝낸다(python3 는 부르지 않는다)."""
    gbox.publish()
    log = gbox.base / "python3-called"
    path = gbox.tools(shims={"uname": _DARWIN, "unzip": _BUSYBOX_UNZIP,
                             "python3": f'#!/bin/sh\necho called >>"{log}"\nexit 1\n'})
    r = gbox.run(*gbox.base_args, "--dir", str(gbox.base / "mac-app"), env=gbox.env(PATH=path))
    assert r.rc == 1 and "unzip(Info-ZIP)" in r.out and "손상" not in r.out and not log.exists(), str(r)
    assert gbox.cp.requests == []


@posix
@need_curl
@pytest.mark.parametrize("extra,rc,said", [
    pytest.param({"CLAUDE_CONFIG_DIR": "claude-alt"}, 1, "클로드 폴더가 절대 경로가 아닙니다", id="config-dir-relative"),
    pytest.param({"SESSION_MANAGER_CLAUDE_HOME": "/x/../y"}, 1, "클로드 폴더가 절대 경로가 아닙니다", id="sm-home-dotdot"),
    # 빈 값은 없는 것과 같다(install.sh 의 ${…:-…}) — 다음 후보인 CLAUDE_CONFIG_DIR 을 본다
    pytest.param({"SESSION_MANAGER_CLAUDE_HOME": "", "CLAUDE_CONFIG_DIR": "/c/./d"}, 1, "클로드 폴더가 절대 경로가 아닙니다",
                 id="empty-sm-home-then-config-dot"),
    pytest.param({"CLEWPATH_AGENT_TOKEN": "agent-SECRET\r"}, 2, "제어문자", id="agent-token-cr"),
    pytest.param({"CLEWPATH_AGENT_TOKEN": "agent-SECRET\nx"}, 2, "제어문자", id="agent-token-newline"),
])
def test_refuses_what_install_sh_refuses_before_download(gbox, extra, rc, said):
    """G2 — install.sh 가 정리(on_exit)를 걸기 전에 거절하는 것(클로드 폴더 env — claude_home_guess 순서·path_ok, agent 토큰의
    제어문자 — CR 포함)을 받기 전에 같은 규칙으로 거절한다. 받은 뒤 거절되면 .clewpath-get.*/stage 가 시도마다 남는다."""
    gbox.publish()
    r = gbox.run(*gbox.base_args, env=gbox.env(**extra))
    assert r.rc == rc and said in r.out, str(r)
    assert "SECRET" not in r.out + r.err and gbox.cp.requests == [] and _no_get_tmp(gbox) and not gbox.chome.exists()


@posix
@need_curl
@need_unzip
def test_claude_home_follows_install_sh_order(gbox):
    """G2 대조군 — SESSION_MANAGER_CLAUDE_HOME 이 있으면 그것만 본다(install.sh 의 claude_home_guess). 뒤 후보는 보지 않는다."""
    gbox.publish()
    r = gbox.run(*gbox.base_args, env=gbox.env(SESSION_MANAGER_CLAUDE_HOME=gbox.base / "ch", CLAUDE_CONFIG_DIR="rel"))
    assert r.rc == 0, str(r)


_PHYS_PWD = "phys_pwd() { if [ -x /bin/pwd ]; then /bin/pwd -P; else pwd -P; fi; }"


def test_physical_paths_come_from_phys_pwd():
    """C1 — 물리 경로는 phys_pwd 한 곳에서(/bin/pwd -P 는 맥 APFS 에서 디스크의 대소문자를 준다 — 셸 내장 pwd -P 는 친
    글자 그대로). 다섯 스크립트가 같은 이름·같은 본문을 쓴다."""
    lines = GET_SH.read_text(encoding="utf-8").split("\n")
    assert _PHYS_PWD in lines
    stray = [ln for ln in lines if ln != _PHYS_PWD and "pwd -P" in ln.split(" #")[0] and not ln.lstrip().startswith("#")]
    assert not stray, stray


@posix
@need_curl
@pytest.mark.parametrize("case", ["home-parent", "grandparent", "physical-home", "home-missing"])
def test_refuses_home_and_its_ancestors(gbox, case):
    """C2 — 설치 폴더가 / 이거나 홈(물리 경로) 자신·그 조상이면 받기 전에 거부한다. 홈의 물리 경로를 못 얻으면 $HOME 글자로."""
    gbox.publish()
    env = gbox.env()
    if case == "home-parent":
        d = gbox.home.parent
    elif case == "grandparent":
        d = gbox.base
    elif case == "physical-home":                 # HOME 이 링크 — 글자로는 홈이 아니지만 물리 경로가 홈(빈 폴더)
        link = gbox.base / "home-link"
        link.symlink_to(gbox.home)
        env = gbox.env(HOME=link)
        d = gbox.home
    else:                                         # 홈이 없다 — $HOME 글자의 조상
        env = gbox.env(HOME=gbox.base / "gone" / "user")
        d = gbox.base / "gone"
    r = gbox.run(*gbox.base_args, "--dir", str(d), env=env)
    assert r.rc == 2 and "/ 나 홈" in r.out, str(r)
    assert gbox.cp.requests == [] and _no_get_tmp(gbox) and not gbox.record.exists()


@posix
@need_curl
@need_unzip
def test_refuses_a_link_to_home_after_resolving(gbox):
    """C2 — --dir 가 홈을 가리키는 링크면(글자로는 홈이 아니다) 물리 경로로 바꾼 뒤, stage 를 만들기 전에 거부한다."""
    gbox.publish()
    link = gbox.base / "app-link"
    link.symlink_to(gbox.home)
    r = gbox.run(*gbox.base_args, "--dir", str(link))
    assert r.rc == 2 and "/ 나 홈" in r.out, str(r)
    assert not gbox.record.exists() and _no_get_tmp(gbox) and sorted(gbox.home.iterdir()) == []


@posix
@need_curl
@need_unzip
def test_mac_refuses_non_ascii_install_paths(gbox):
    """C5 — [mac] ps 는 ASCII 밖 바이트를 이스케이프해 보여 런처·러너가 Host 를 명령줄로 알아보지 못한다. 그래서 맥에서는 설치
    폴더 경로(글자, 그리고 물리 경로)에 ASCII 밖 바이트가 있으면 거부한다. 리눅스는 그대로다(한글 홈으로 도는 위 시험들)."""
    gbox.publish()
    path = gbox.tools(shims={"uname": _DARWIN})
    mac = gbox.env(PATH=path)
    k_home = gbox.base / "한글 홈"                                        # 상자의 홈은 진짜 맥에서 ASCII 다 — 한글 홈은 따로
    k_home.mkdir()
    for home, args in ((k_home, []), (gbox.home, ["--dir", str(gbox.base / "앱" / "app")])):   # 기본 위치(한글 홈 아래)·--dir
        r = gbox.run(*gbox.base_args, *args, env=gbox.env(HOME=home, PATH=path))
        assert r.rc == 2 and "ASCII" in r.out, (args, str(r))
        assert gbox.cp.requests == [] and _no_get_tmp(gbox) and list(k_home.iterdir()) == [], args
    real = gbox.base / "앱 폴더"                                          # 글자는 ASCII, 물리 경로는 한글(링크)
    real.mkdir()
    link = gbox.base / "ascii-link"
    link.symlink_to(real)
    r = gbox.run(*gbox.base_args, "--dir", str(link), env=mac)
    assert r.rc == 2 and "ASCII" in r.out, str(r)
    assert not gbox.record.exists() and _no_get_tmp(gbox) and list(real.iterdir()) == []


@posix
@with_matrix_shells
def test_dir_guard_rules(shell, lib, tmp_path):
    """C2·C5 규칙을 셸마다(맥 /bin/sh 에 가까운 bash --posix 포함, UTF-8 로캘) — / ·홈·홈의 조상(물리 경로, 못 얻으면 글자)은
    2, [mac] 은 ASCII 밖 바이트가 있으면 2, 리눅스는 한글을 받는다. 이름이 홈과 앞부분만 같은 폴더는 조상이 아니다."""
    real = tmp_path / "real" / "u"
    real.mkdir(parents=True)
    link = tmp_path / "link-home"
    link.symlink_to(real)
    gone = "/nonexistent-get-sh/home/u"
    cases = [
        (gone, "linux", "/", 2), (gone, "linux", gone, 2), (gone, "linux", "/nonexistent-get-sh/home", 2),
        (gone, "linux", "/nonexistent-get-sh/hom", 0), (gone, "linux", gone + "/app", 0),
        (gone, "linux", "/x/한글/app", 0), (gone, "mac", "/x/한글/app", 2), (gone, "mac", "/x/caf\u00e9", 2),
        (gone, "mac", "/x/a b/app", 0),
        (str(link), "linux", str(real), 2), (str(link), "linux", str(tmp_path / "real"), 2),
        (str(link), "linux", str(real) + "/app", 0), (str(link), "linux", str(link) + "/app", 0),
    ]
    got = []
    for home, os_, d, _ in cases:
        r = subprocess.run([*shell, "-c", '. "$1" || exit 99; OS=$2; HOME=$3; ( dir_guard "$4" x >/dev/null ); echo $?',
                            "sh", lib, os_, home, d], capture_output=True, timeout=60,
                           env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        got.append(int(r.stdout.decode().strip() or -1))
    assert got == [c[3] for c in cases], list(zip([c[:3] for c in cases], got))
