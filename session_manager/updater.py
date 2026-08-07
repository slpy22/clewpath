"""자동 업데이트 — 서명된 매니페스트를 받아 검증하고, 분리된 프로세스가 교체한다.

신뢰 모델
---------
업데이트는 사용자 PC 에서 임의 코드를 실행시키는 통로다. 그래서 두 겹으로 막는다:

  1) 매니페스트는 **오프라인 릴리스 키**로 서명된 EdDSA JWT 다. 공개키는 아래
     RELEASE_KEYS 에 박혀 있다(네트워크로 안 받는다 — 받으면 그 경로가 또 공격면).
     CP 의 JWKS 키가 아니다: CP 개인키는 CP DB 에 살기 때문에, 그 키로 서명하면
     "CP 가 뚫려도 코드는 못 민다"가 성립하지 않는다. 릴리스 키는 서버에 없다.
  2) 내려받은 zip 은 매니페스트에 적힌 SHA-256 과 대조한다. 다르면 버린다.

교체는 왜 별도 프로세스인가
---------------------------
돌고 있는 파이썬이 자기 코드를 덮어쓸 수 없다(윈도우는 실행 중 파일이 잠긴다).
그래서 설치 폴더 **밖**에 updater 를 풀어놓고, 그 프로세스가 우리가 죽기를 기다렸다가
파일을 갈아끼우고 다시 띄운다. 실패하면 백업으로 되돌린다.

무엇이 교체되고 무엇이 남는가
-----------------------------
    교체:  session_manager/ control_plane/ relay/ pwa/ pyproject.toml uv.lock install.ps1
    유지:  .venv (uv sync 로 맞춤), start-connector.ps1(재생성됨),
           ~/.claude/session_manager/ 전체 (설정·기기·자격증명) ← 여기가 안 건드려지는 게 핵심
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from session_manager import appconfig, config

ISSUER = "clewpath-release"
AUDIENCE = "clewpath-host"

# 릴리스 서명 공개키(Ed25519 raw, base64url). kid -> 공개키.
# 여러 개를 두는 이유는 키 교체 때문이다: 신키를 먼저 배포해 모두가 신뢰하게 만든 뒤
# 그 키로 서명하기 시작하고, 한참 뒤 구키를 지운다.
# ops/tools/sign_release.py keygen 이 여기에 넣을 줄을 출력해준다.
RELEASE_KEYS: dict[str, str] = {
    # 2026-08 발급. 개인키는 ops/release-keys/private.pem (저장소에만, 서버에 없음).
    "cc454b27b79cc0d0": "z0JBbrgie_Yg-4V0i2RZFWXdcptbUrn2FkHXK3YS1xg",
}

# 업데이트로 갈아끼우는 대상. 여기 없는 것은 건드리지 않는다.
# control_plane/relay 는 서버 전용이라 배포·교체 대상이 아니다 (0.3.6부터 zip 에서 제외.
# 그 전 설치본에 남은 사본은 install.ps1 이 재설치 때 걷어낸다).
PAYLOAD = ("session_manager", "pwa",
           "pyproject.toml", "uv.lock", "install.ps1", "README.md", "LICENSE")


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- 경로/상태

def install_root() -> Path:
    """설치 폴더(= 이 패키지의 부모)."""
    return Path(__file__).resolve().parent.parent


def is_dev_checkout() -> bool:
    """여기가 운영자의 소스 저장소인가(사용자 배포본이 아니라).

    사용자 배포본에는 .git 도 ops/ 도 없다(배포 zip 에서 제외됨). 둘 중 하나가
    있으면 = 개발 체크아웃 → 자동 업데이트가 소스 트리를 덮어쓰면 안 된다.
    (install.ps1 의 레거시 정리 가드와 같은 신호를 쓴다)
    """
    root = install_root()
    return (root / ".git").exists() or (root / "ops").is_dir()


def work_dir() -> Path:
    d = config.data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file() -> Path:
    return work_dir() / "state.json"


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_state(**kw: Any) -> dict[str, Any]:
    st = read_state()
    st.update(kw)
    st["updated_at"] = int(time.time())
    try:
        state_file().write_text(json.dumps(st, indent=2, ensure_ascii=False),
                                encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[update] 상태 기록 실패(무시): {e}", flush=True)
    return st


def current_version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("clewpath-host")
    except PackageNotFoundError:
        # 편집 설치가 아닌 경우(zip 을 그냥 푼 상태) pyproject 에서 직접 읽는다
        try:
            import tomllib
            p = install_root() / "pyproject.toml"
            return tomllib.loads(p.read_text(encoding="utf-8"))["project"]["version"]
        except Exception:  # noqa: BLE001
            return "0"


def _ver_tuple(v: str) -> tuple:
    out = []
    for part in str(v).split("."):
        num = "".join(c for c in part if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def is_newer(candidate: str, current: str) -> bool:
    return _ver_tuple(candidate) > _ver_tuple(current)


# ---------------------------------------------------------------- 검증

def verify_manifest(token: str) -> dict[str, Any]:
    """서명된 매니페스트를 검증해 클레임을 돌려준다. 실패하면 UpdateError.

    알고리즘을 EdDSA 로 못박는다 — 토큰의 alg 를 그대로 믿으면 alg=none 이나
    HMAC 혼동 공격이 열린다(공개키를 HMAC 비밀로 쓰게 만드는 고전 취약점).
    """
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not RELEASE_KEYS:
        raise UpdateError("릴리스 공개키가 없습니다(이 빌드는 업데이트를 받을 수 없음)")
    try:
        kid = pyjwt.get_unverified_header(token).get("kid")
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"매니페스트 형식이 아닙니다: {e}") from e
    raw_b64 = RELEASE_KEYS.get(kid or "")
    if not raw_b64:
        raise UpdateError(f"모르는 서명키입니다(kid={kid})")
    pad = "=" * (-len(raw_b64) % 4)
    pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(raw_b64 + pad))
    try:
        claims = pyjwt.decode(token, pub, algorithms=["EdDSA"],
                              audience=AUDIENCE, issuer=ISSUER, leeway=60)
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"서명 검증 실패: {type(e).__name__}: {e}") from e
    for k in ("ver", "url", "sha256", "size"):
        if not claims.get(k):
            raise UpdateError(f"매니페스트에 {k} 가 없습니다")
    if len(claims["sha256"]) != 64:
        raise UpdateError("sha256 형식이 잘못됐습니다")
    return claims


# ---------------------------------------------------------------- 확인/다운로드

def manifest_url() -> str:
    explicit = appconfig.get("update", "manifest_url", None)
    if explicit:
        return str(explicit)
    cp = (appconfig.get("cloud", "cp_url", None)
          or os.environ.get("SM_CP_URL") or "https://clewpath.pyongso.com/cp")
    return f"{str(cp).rstrip('/')}/updates/manifest"


def check(timeout: int = 20) -> dict[str, Any]:
    """업데이트가 있는지 확인한다. 네트워크/서명 실패는 예외 대신 상태로 돌려준다.

    조용히 실패하면 안 되지만, 여기서 예외를 던지면 백그라운드 체크가 서버 로그를
    더럽히거나 UI 를 깨뜨린다. 그래서 결과 dict 에 error 를 담아 UI 가 보여주게 한다.
    """
    cur = current_version()
    url = manifest_url()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"clewpath-host/{cur}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            token = r.read().decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        return write_state(last_check=int(time.time()), available=False,
                           error=f"확인 실패: {type(e).__name__}: {e}",
                           current=cur)
    try:
        m = verify_manifest(token)
    except UpdateError as e:
        return write_state(last_check=int(time.time()), available=False,
                           error=str(e), current=cur)
    newer = is_newer(m["ver"], cur)
    return write_state(last_check=int(time.time()), available=newer, error=None,
                       current=cur, latest=m["ver"], notes=m.get("notes", ""),
                       manifest=token if newer else None)


def download(manifest: dict[str, Any], token: str, timeout: int = 600) -> Path:
    """아티팩트를 받아 SHA-256 을 대조하고 경로를 돌려준다."""
    dest = work_dir() / f"clewpath-host-{manifest['ver']}.zip"
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(manifest["url"],
                                 headers={"User-Agent": "clewpath-host"})
    h = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, tmp.open("wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > manifest["size"] * 2 + (1 << 20):
                tmp.unlink(missing_ok=True)
                raise UpdateError("아티팩트가 매니페스트 크기보다 지나치게 큽니다")
            h.update(chunk)
            f.write(chunk)
    if total != manifest["size"]:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"크기 불일치: {total} != {manifest['size']}")
    if h.hexdigest() != manifest["sha256"]:
        tmp.unlink(missing_ok=True)
        raise UpdateError("SHA-256 불일치 - 받은 파일을 버립니다")
    tmp.replace(dest)
    (work_dir() / f"manifest-{manifest['ver']}.jwt").write_text(token, encoding="utf-8")
    return dest


def stage(zip_path: Path, manifest: dict[str, Any]) -> Path:
    """zip 을 펼쳐서 교체 대상만 남긴다. zip-slip 을 여기서 막는다."""
    out = work_dir() / f"stage-{manifest['ver']}"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            # zip 안의 경로가 ../ 로 밖을 가리키면 설치 폴더 바깥을 덮어쓸 수 있다
            target = (out / info.filename).resolve()
            if not str(target).startswith(str(out.resolve())):
                raise UpdateError(f"zip 경로가 스테이지 밖을 가리킵니다: {info.filename}")
            z.extract(info, out)
    top = {p.name for p in out.iterdir()}
    if "session_manager" not in top:
        raise UpdateError("아티팩트에 session_manager 가 없습니다 - 잘못된 zip")
    for name in list(top):
        if name not in PAYLOAD:
            p = out / name
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------- 적용

def runner_script() -> Path:
    return Path(__file__).resolve().parent / "update_runner.ps1"


def apply(manifest: dict[str, Any], staged: Path, restart_cmd: str | None = None) -> dict:
    """분리된 updater 를 띄우고 우리가 죽을 준비를 한다.

    updater 는 설치 폴더 밖(데이터 폴더)으로 복사해서 실행한다 — 설치 폴더를
    통째로 갈아끼우는 동안 자기 자신이 사라지면 안 된다.
    """
    root = install_root()
    runner_src = runner_script()
    if not runner_src.is_file():
        raise UpdateError(f"updater 스크립트가 없습니다: {runner_src}")
    runner = work_dir() / "update_runner.ps1"
    shutil.copy2(runner_src, runner)

    if restart_cmd is None:
        start_ps = root / "start-connector.ps1"
        restart_cmd = str(start_ps) if start_ps.is_file() else ""

    port = int(os.environ.get("SM_PORT", "5100"))
    try:
        rt = json.loads((config.data_dir() / "runtime.json").read_text(encoding="utf-8"))
        port = int(rt.get("port", port))
    except Exception:  # noqa: BLE001
        pass

    log = work_dir() / f"apply-{manifest['ver']}.log"
    pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    args = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
            "-InstallRoot", str(root),
            "-StageDir", str(staged),
            "-BackupDir", str(work_dir() / f"backup-{current_version()}"),
            "-WaitPid", str(os.getpid()),
            "-Port", str(port),
            "-Version", str(manifest["ver"]),
            "-LogFile", str(log)]
    if restart_cmd:
        args += ["-RestartScript", restart_cmd]

    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP.
    # ⚠ DETACHED_PROCESS(0x8) 를 쓰면 안 된다 — 실측으로 확인했듯 pwsh 는 콘솔이
    #   아예 없으면 아무것도 안 하고 exit 0 으로 즉시 끝난다(에러도 안 남긴다).
    #   그러면 업데이트가 "성공했다"고 응답해놓고 실제로는 아무 일도 안 일어난다.
    #   CREATE_NO_WINDOW 는 콘솔을 주되 창을 안 띄운다. 프로세스 수명은 원래
    #   부모와 독립이라 우리가 죽어도 자식은 계속 돈다.
    flags = 0
    if sys.platform == "win32":
        flags = 0x08000000 | 0x00000200
    subprocess.Popen(args, close_fds=True, creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    return write_state(applying=manifest["ver"], apply_log=str(log),
                       apply_started_at=int(time.time()))


def recently_failed(version: str) -> bool:
    """이 버전의 적용이 방금 실패(롤백)했는지. 재기동 자동적용의 무한루프를 막는다.

    실패 → 롤백 → 재기동 → (게시된 최신은 여전히 그 버전) → 또 적용 시도 → 또 롤백…
    을 끊는다. last_apply.json 은 update_runner 가 롤백 후에도 ok=false 로 남긴다.
    """
    try:
        last = json.loads((work_dir() / "last_apply.json").read_text(encoding="utf-8"))
        return last.get("version") == version and last.get("ok") is False
    except Exception:  # noqa: BLE001
        return False


def check_and_stage() -> dict[str, Any]:
    """확인 -> (있으면) 다운로드 -> 검증 -> 스테이징. 적용 직전까지."""
    st = check()
    if not st.get("available") or not st.get("manifest"):
        return st
    token = st["manifest"]
    m = verify_manifest(token)          # 다시 검증 — 저장/전달 중 바뀌었을 수 있다
    zip_path = download(m, token)
    staged = stage(zip_path, m)
    return write_state(staged=str(staged), staged_version=m["ver"])
