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
    교체:  PAYLOAD — session_manager/ pwa/ pyproject.toml uv.lock install.ps1 install.sh uninstall.sh README.md LICENSE
    유지:  .venv (uv sync 로 맞춤), 런처 start-connector.ps1·.sh (설치본마다 렌더 — Host 가 템플릿으로 다시 맞춘다),
           ~/.claude/session_manager/ 전체 (설정·기기·자격증명) ← 여기가 안 건드려지는 게 핵심

어느 OS 가 받는가
-----------------
매니페스트의 platforms 클레임(osdep.NAME: win·mac·linux)에 자기 OS 가 있어야 받는다. 클레임이 없으면
win 전용(0.28 까지의 릴리스), 모양이 이상하면 아무도 받지 않는다(fail-closed).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
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
# 네 곳이 같아야 한다(test_updater.test_payload_lists_agree): 여기·update_runner.ps1 $Payload·
# update_runner.sh PAYLOAD·ops/package-for-user.ps1 $include. install.sh·uninstall.sh 는 맥·리눅스용 —
# 옛 설치본의 stage() 는 모르는 최상위 항목을 버리므로 Windows 설치본에는 다음다음 업데이트부터 들어간다(쓰지 않음).
PAYLOAD = ("session_manager", "pwa",
           "pyproject.toml", "uv.lock", "install.ps1", "install.sh", "uninstall.sh", "README.md", "LICENSE")


class UpdateError(Exception):
    pass


# 러너 기동에 쓰는 Popen 참조. 시험은 이것만 바꿔 끼운다 — subprocess.Popen(표준 모듈 그 자체)을 바꾸면 같은 프로세스의 다른
# 스레드(앞 시험 앱의 orphan-sweep 등)가 부르는 subprocess 까지 가짜로 가서 호출 수 단언이 가끔 틀어졌다(TODOS 172행).
_popen = subprocess.Popen


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


def supports_this_os(claims: dict[str, Any]) -> bool:
    """서명된 매니페스트가 이 OS 용인가. platforms 가 없으면 win 전용(0.28 까지), 문자열 목록이 아니면 아무도 아님."""
    from session_manager import osdep
    plats = claims.get("platforms", ["win"])
    if not (isinstance(plats, list) and all(isinstance(p, str) for p in plats)):
        plats = []                                          # 모양이 이상하면 fail-closed
    return osdep.NAME in plats


def _unsupported_msg(claims: dict[str, Any]) -> str:
    from session_manager import osdep
    return f"이 릴리스({claims.get('ver')})는 {osdep.NAME} 를 지원하지 않습니다"


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
    if not supports_this_os(m):
        # 오류가 아니다 — 최신 릴리스가 이 OS 를 아직 안 낸 것. 받지도 적용하지도 않는다.
        return write_state(last_check=int(time.time()), available=False, error=None,
                           current=cur, latest=m["ver"], notes=m.get("notes", ""),
                           manifest=None, unsupported=_unsupported_msg(m))
    newer = is_newer(m["ver"], cur)
    return write_state(last_check=int(time.time()), available=newer, error=None,
                       current=cur, latest=m["ver"], notes=m.get("notes", ""),
                       manifest=token if newer else None, unsupported=None)


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
    from session_manager import osdep
    return Path(__file__).resolve().parent / osdep.UPDATE_RUNNER


def _marker_holder() -> str | None:
    """맥·리눅스: 데이터 폴더의 update.inprogress(install.sh·러너가 쥔다)가 살아 있으면 그 by(install·runner·…), 아니면 None.

    러너의 marker_live 와 같은 규칙 — pid 가 그 프로그램(명령줄에 install.sh·update_runner)으로 살아 있고 at 이 30분 안.
    pid·at 이 없으면 막 쓰는 중일 수 있어 1분은 산 것으로, 명령줄을 못 읽으면 산 것으로 본다. Windows 는 이 표식을 쓰지 않는다.
    """
    import re
    from session_manager import osdep
    if osdep.NAME == "win":
        return None
    p = config.data_dir() / "update.inprogress"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        age = time.time() - p.stat().st_mtime
    except OSError:
        return None

    def first(rx: str) -> str | None:                   # sed -n 's/^…$/\1/p' | head -n 1 와 같게
        return next((m.group(1) for m in (re.fullmatch(rx, ln) for ln in lines) if m), None)
    pid, at, by = first(r"pid=([0-9]{1,10})\s*"), first(r"at=([0-9]{1,12})\s*"), first(r"by=([a-z]*)\s*")
    if not pid or not at:
        return (by or "?") if age < 60 else None
    if not osdep.pid_alive(int(pid)) or time.time() - int(at) >= 1800:
        return None
    want = {"runner": "update_runner", "install": "install.sh"}.get(by or "")
    if want:
        try:
            import psutil
            cmd = " ".join(psutil.Process(int(pid)).cmdline())
        except Exception:  # noqa: BLE001
            cmd = ""
        if cmd and want not in cmd:
            return None                                 # pid 를 다른 프로그램이 받았다(재사용)
    return by or "?"


APPLY_LOCK_TTL_S = 300     # apply.lock 만료(초) — 러너가 못 지운 시체 잠금은 이만큼 지나면 치운다(자기치유)


def _lock_age(lock: Path) -> float:
    """잠금 파일의 나이(초). 없으면 FileNotFoundError."""
    return time.time() - lock.stat().st_mtime


def _put_back(grave: Path, lock: Path) -> None:
    """옮겨 온 새 잠금을 제자리로 — 그새 또 다른 잠금이 섰으면 덮지 않는다(Windows rename·POSIX link 는 대상이 있으면 실패)."""
    try:
        if os.name == "nt":
            os.rename(grave, lock)
        else:
            os.link(grave, lock)
            os.unlink(grave)
        return
    except FileExistsError:
        pass                                     # 그새 다른 적용이 잡았다 — 그쪽 잠금을 둔다
    except OSError:
        try:
            os.replace(grave, lock)              # 하드 링크를 못 쓰는 파일 시스템
            return
        except OSError:
            pass
    try:
        grave.unlink()
    except OSError:
        pass


def _take_apply_lock(lock: Path) -> None:
    """apply.lock 을 O_EXCL 로 잡는다. APPLY_LOCK_TTL_S 안의 잠금이 있으면 UpdateError.

    '있나 보고 → 쓰기' 는 둘이 동시에 '없음'을 보면 둘 다 쓴다(러너 둘 = 2026-09-04 사고) — 만들기 자체가 확인이어야 한다.
    시체 잠금(러너가 죽어 못 지움)은 치우되 치우기도 경합 안전하게: 바로 지우지 않고 고유 이름으로 옮긴 뒤 옮겨 온 것의 나이를
    다시 본다 — 둘이 같은 시체를 보고 그냥 지우면, 늦게 지운 쪽이 그 사이 먼저 잡은 쪽의 새 잠금을 지운다. 옮겨 온 것이 새
    잠금이면 제자리에 돌려놓고 거부. 잠금을 다룰 수 없는 폴더(권한 등)면 예전처럼 잠금 없이 진행한다.
    """
    busy = UpdateError("이미 업데이트 적용이 진행 중입니다 — 완료를 기다려 주세요(재기동까지 최대 1~2분).")
    for _ in range(3):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            pass
        except OSError:
            return
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            return
        try:
            if _lock_age(lock) < APPLY_LOCK_TTL_S:
                raise busy
        except FileNotFoundError:
            continue                                 # 그새 풀렸다 — 다시 잡아 본다
        except OSError:
            return
        grave = lock.with_name(f"{lock.name}.stale-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            os.replace(lock, grave)
        except OSError:
            continue                                 # 다른 쪽이 먼저 치웠거나 잡고 있다 — 다시 본다
        try:
            fresh = _lock_age(grave) < APPLY_LOCK_TTL_S
        except OSError:
            fresh = False
        if fresh:                                    # 그새 다른 적용이 잡은 새 잠금을 옮겨 왔다
            _put_back(grave, lock)
            raise busy
        try:
            grave.unlink()
        except OSError:
            pass
    raise busy


def apply(manifest: dict[str, Any], staged: Path, restart_cmd: str | None = None) -> dict:
    """분리된 updater 를 띄우고 우리가 죽을 준비를 한다.

    updater 는 설치 폴더 밖(데이터 폴더)으로 복사해서 실행한다 — 설치 폴더를
    통째로 갈아끼우는 동안 자기 자신이 사라지면 안 된다.
    """
    # 유지보수 표식(사람이 만들었거나 맥·리눅스 런처의 stop --hold)이 있으면 런처가 아무것도 띄우지 않는다 —
    # 여기서 적용하면 러너의 재기동이 막혀 헬스 실패 → 롤백 → 그 재기동도 막혀 Host 가 꺼진 채로 남는다.
    if (config.data_dir() / "maintenance.flag").exists():
        raise UpdateError("유지보수 표식(maintenance.flag)이 있어 적용하지 않습니다 — 재기동이 막혀 롤백됩니다. "
                          "표식을 지운 뒤 다시 하세요.")
    if not supports_this_os(manifest):                  # check() 가 걸렀어도 마지막 관문에서 한 번 더
        raise UpdateError(_unsupported_msg(manifest))
    # 맥·리눅스: sh install.sh 재실행 중(옛 Host 가 [1/6]~[5/6] 동안 떠 있다)에 적용하면 Host 는 내려가고 러너는 설치 표식을
    # 보고 종료코드 3 으로 끝난다(재기동 없음) — 설치가 중간에 실패하면 Host 가 꺼진 채 남는다. 잠금 전에 거부한다.
    holder = _marker_holder()
    if holder == "install":
        raise UpdateError("설치(install.sh)가 진행 중이라 적용하지 않습니다 — 설치가 끝난 뒤 다시 하세요.")
    if holder:
        raise UpdateError("다른 업데이트가 진행 중이라(update.inprogress) 적용하지 않습니다 — 끝난 뒤 다시 하세요.")
    # ★ 동시 실행 방지 잠금(실사고 2026-09-04): 사용자가 업데이트를 연달아 누르거나
    #   재기동 자동적용이 겹치면 runner 가 2개 이상 동시에 돌아 포트 경합으로 서비스가
    #   완전히 죽는다. 5분 내 진행 중인 apply 가 있으면 거부한다(만료 시 자기치유). 잡기는 O_EXCL(_take_apply_lock).
    _take_apply_lock(work_dir() / "apply.lock")

    root = install_root()
    runner_src = runner_script()
    if not runner_src.is_file():
        raise UpdateError(f"updater 스크립트가 없습니다: {runner_src}")
    from session_manager import osdep
    runner = work_dir() / runner_src.name
    shutil.copy2(runner_src, runner)

    if restart_cmd is None:
        start_ps = root / osdep.START_SCRIPT
        restart_cmd = str(start_ps) if start_ps.is_file() else ""

    # 헬스체크 포트. apply() 는 살아있는 서버 안에서 불리므로 서버가 실제로 바인드한
    # 포트(BOUND_PORT — server.main 이 세팅)가 권위다. runtime.json 은 claude_home 당
    # 하나뿐인 공유 파일이라 두 번째 인스턴스(개발/테스트 서버, 포트 폴백)가 덮어쓰면
    # stale 해진다 — 그러면 runner 가 엉뚱한 포트를 헬스체크해 멀쩡한 새 버전을
    # 롤백한다(실사고 2026-09-10 운영자 PC: 5199 테스트 서버가 남긴 runtime.json 으로
    # 0.7.0 이 헛롤백). runtime.json 은 BOUND_PORT 가 없을 때(서버 밖 호출)만 폴백.
    port = int(os.environ.get("SM_PORT", "5100"))
    if BOUND_PORT:
        port = int(BOUND_PORT)
    else:
        try:
            rt = json.loads((config.data_dir() / "runtime.json").read_text(encoding="utf-8"))
            port = int(rt.get("port", port))
        except Exception:  # noqa: BLE001
            pass

    log = work_dir() / f"apply-{manifest['ver']}.log"
    args = osdep.update_runner_argv(runner, install_root=root, stage_dir=staged,
                                    backup_dir=work_dir() / f"backup-{current_version()}", wait_pid=os.getpid(),
                                    port=port, version=str(manifest["ver"]), log_file=log,
                                    restart_script=restart_cmd or "")

    # 분리 기동 — Windows 에서 DETACHED_PROCESS 를 쓰면 안 되는 이유는 osdep._win.detached_popen_kwargs
    # runner 가 우리를 강제 종료하므로 lifespan 의 표식이 안 남을 수 있다 → 미리 '업데이트' 표식
    # + 릴레이에 bye(의도된 단절 — '연결 끊김' 알림 억제)
    try:
        from session_manager import liveness
        liveness.mark_shutdown("update")
    except Exception:  # noqa: BLE001
        pass
    try:
        from session_manager import connector
        connector.request_bye("update")
    except Exception:  # noqa: BLE001
        pass
    # 러너 → 새 Host 는 이 env 를 물려받는다. 이 Host 가 clewpath.toml 에서 채운 값(appconfig.filled_env)은 뺀다 — 그대로 넘기면
    # env 가 파일보다 우선이라 새 Host 가 옛 설정값으로 떠 그사이 고친 clewpath.toml 이 가려진다(TODOS Q15·R10).
    # 사용자(런처)가 원래 준 env 는 그대로. 채운 게 없으면 예전처럼 통째로 물려받는다(env=None).
    drop = appconfig.filled_env()
    env = {k: v for k, v in os.environ.items() if k not in drop} if drop else None
    _popen(args, close_fds=True, env=env, **osdep.detached_popen_kwargs(),
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
           stdin=subprocess.DEVNULL)
    return write_state(applying=manifest["ver"], apply_log=str(log),
                       apply_started_at=int(time.time()))


# 이 프로세스가 실제로 연 포트. server.main() 이 바인드 직후 세팅한다. apply() 의
# 헬스체크 포트 권위(위 apply 주석). 서버 밖(테스트·CLI)에서는 None 이라 폴백 경로.
BOUND_PORT: int | None = None


def recently_failed(version: str) -> bool:
    """이 버전의 적용이 방금 실패(롤백)했는지. 재기동 자동적용의 무한루프를 막는다.

    실패 → 롤백 → 재기동 → (게시된 최신은 여전히 그 버전) → 또 적용 시도 → 또 롤백…
    을 끊는다. last_apply.json 은 update_runner 가 롤백 후에도 ok=false 로 남긴다.
    """
    try:
        # utf-8-sig: pwsh 가 없는 PC 는 러너가 powershell.exe(5.1)로 돌고, 5.1 의 Set-Content -Encoding UTF8 은 BOM 을 붙인다 —
        # utf-8 로 읽으면 json 이 BOM 에서 실패해 '방금 실패한 버전'을 몰라 같은 버전을 다시 받아 롤백을 되풀이했다
        last = json.loads((work_dir() / "last_apply.json").read_text(encoding="utf-8-sig"))
        return last.get("version") == version and last.get("ok") is False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 작업 폴더 정리

KEEP_PER_KIND = 2          # 종류별로 남기는 개수(TODOS Q16·R13)

# 작업 폴더(updates/)에 업데이트마다 쌓이는 것 — 받은 zip·펼친 stage·러너의 백업·롤백 때 남긴 실패본.
# 이름이 이 규칙에 딱 맞는 것만 다룬다(러너가 만드는 중인 backup-<v>.tmp.<pid>·잠금·로그·매니페스트·.part 는 건드리지 않는다).
_V = r"(\d+(?:\.\d+)*)"
_PRUNE_KINDS = (("zip", re.compile(rf"clewpath-host-{_V}\.zip")), ("stage", re.compile(rf"stage-{_V}")),
                ("backup", re.compile(rf"backup-{_V}")), ("failed", re.compile(rf"backup-{_V}-failed-{_V}")))


def _applying_now() -> bool:
    """적용이 도는 중인가 — apply.lock(나이 무관: 시체여도 러너가 죽은 것이라 그 백업이 복구에 필요하다), 맥·리눅스 러너 잠금,
    update.inprogress 표식."""
    wd = work_dir()
    return (wd / "apply.lock").exists() or (wd / "runner.lock.d").exists() or _marker_holder() is not None


def prune_work_dir(keep: int = KEEP_PER_KIND) -> list[str]:
    """작업 폴더의 zip·stage·backup·실패본을 종류별로 버전이 높은 keep 개만 남기고 지운다. 지운 이름들을 돌려준다.

    절대 지우지 않는 것: 적용이 도는 중이면 아무것도(_applying_now), 아직 적용 안 한 스테이지(staged_version)와 마지막으로 적용한
    버전(applying)의 zip·stage, 지금 설치본의 롤백 원본(지금 버전보다 낮은 백업 중 가장 새것 — 수동 되돌리기에 쓴다).
    """
    if _applying_now():
        return []
    wd = work_dir()
    st = read_state()
    pinned = {str(st.get(k)) for k in ("staged_version", "applying") if st.get(k)}
    cur = _ver_tuple(current_version())
    found: dict[str, list[tuple[tuple, Path]]] = {k: [] for k, _ in _PRUNE_KINDS}
    try:
        entries = list(wd.iterdir())
    except OSError:
        return []
    for p in entries:
        for kind, rx in _PRUNE_KINDS:
            m = rx.fullmatch(p.name)
            if not m:
                continue
            if kind in ("zip", "stage") and m.group(1) in pinned:
                break
            key = (_ver_tuple(m.group(2)), _ver_tuple(m.group(1))) if kind == "failed" else (_ver_tuple(m.group(1)),)
            found[kind].append((key, p))
            break
    older = [(k, p) for k, p in found["backup"] if k[0] < cur]
    rollback = max(older, key=lambda kp: kp[0])[1] if older else None
    removed: list[str] = []
    for kind, items in found.items():
        items.sort(key=lambda kp: kp[0], reverse=True)
        for _, p in items[keep:]:
            if p == rollback or p.is_symlink():
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(p.name)
            except OSError as e:
                print(f"[update] 작업 폴더 정리 실패(무시): {p.name}: {type(e).__name__}", flush=True)
    if removed:
        print(f"[update] 작업 폴더 정리: {len(removed)}개 지움({', '.join(sorted(removed))})", flush=True)
    return removed


def prune_in_background(delay_s: float = 90.0):
    """Host 가 뜬 뒤 배경에서 작업 폴더를 정리한다(데몬 스레드, 실패는 로그만). 적용 요청 안(check_and_stage)에서 하던 첫 정리가
    쌓인 버전이 많으면 1분 넘게 걸려 release.ps1 의 적용 요청이 끊겼다(0.32.4 출시). 업데이트는 늘 Host 재기동으로 끝나므로
    기동마다 한 번이면 충분하다. 적용이 도는 중이면 prune_work_dir 이 스스로 아무것도 하지 않는다.
    잡은 때의 작업 폴더가 아니면 하지 않는다 — 시험 앱이 잡아 둔 정리가 나중에 진짜 데이터 폴더에서 돌지 않게(맥 1회차의 정기 작업
    사고와 같은 꼴). 앱을 내리면(.stop.set(), lifespan) 기다리던 정리는 돌지 않는다."""
    import threading
    stop = threading.Event()
    wd = work_dir()

    def run():
        if stop.wait(delay_s):               # 기동 직후(헬스체크·자기 회복)와 겹치지 않게 기다린다. 그새 앱이 내려가면 끝
            return
        try:
            if work_dir() != wd:
                return
            prune_work_dir()
        except Exception as e:  # noqa: BLE001 — 정리는 덤이다
            print(f"[update] 작업 폴더 정리 건너뜀: {type(e).__name__}", flush=True)
    t = threading.Thread(target=run, name="update-prune", daemon=True)
    t.stop = stop
    t.start()
    return t


def check_and_stage() -> dict[str, Any]:
    """확인 -> (있으면) 다운로드 -> 검증 -> 스테이징. 적용 직전까지. 작업 폴더 정리는 여기서 하지 않는다(prune_in_background)."""
    st = check()
    if not st.get("available") or not st.get("manifest"):
        return st
    token = st["manifest"]
    m = verify_manifest(token)          # 다시 검증 — 저장/전달 중 바뀌었을 수 있다
    zip_path = download(m, token)
    staged = stage(zip_path, m)
    return write_state(staged=str(staged), staged_version=m["ver"])
