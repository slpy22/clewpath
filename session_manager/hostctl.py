"""설치 도우미 — 맥·리눅스 install.sh 가 부르는 작은 명령들(docs/designs/posix-install.md 4.3).

    .venv/bin/python -I -m session_manager.hostctl <명령> [인자]

표준출력은 key=value 줄(install.sh 가 sed 로 읽는다), 사람에게 하는 말은 표준오류([OK]·[WARN]·[FAIL] — install.ps1 과
같은 말). 종료코드 0 성공 / 1 오류 / 2 사용법.

  paths            Host 가 볼 경로 → data_dir= conf_file= claude_home= projects_dir=
  secure-data-dir  데이터 폴더 0700, 그 아래 파일·폴더에서 group·other 권한을 뺀다(심볼릭 링크는 건너뜀) → changed=
  write-config     clewpath.toml 이 없을 때만 만든다(0600·BOM 없음·tomllib 검증·원자적) → config=created|kept path= port=
  render-launcher  start-connector.sh 를 템플릿으로(0755·원자적) → launcher=created|updated|unchanged
  provision        CP 자격증명(있으면 그대로, 실패는 경고만 — fail-open. CP 는 Host 와 같은 규칙 — 설정 파일이 먼저)
                   → creds=kept|created|failed connector= reason=
  wait-ready       기동 확인(runtime.json 의 실제 포트를 따라간다. 클로드 폴더가 다른 남의 Host 는 down)
                   → state=up|down port= mode= session_count= claude_exe_found=
  render-launchd   [mac] 자동 시작 등록 파일(~/Library/LaunchAgents/com.clewpath.host.plist, plistlib·0644·원자적).
                   멱등 런처(v2)가 있어야 쓴다(S22) — 올리기(launchctl)는 install.sh 가 한다 → launchd=created|updated|
                   unchanged plist= label=

왜 파이썬인가: 경로 규칙(config·appconfig)·설정 템플릿·자격증명 저장(cp_client)은 Host 코드가 원본이다 — 셸에 셋째 사본을
만들지 않는다. 토큰은 인자로 받지 않는다(같은 PC 의 다른 사용자에게 ps 로 보인다) — env CLEWPATH_CLIENT_TOKEN·
CLEWPATH_AGENT_TOKEN 으로만. 맨 위 import 는 표준 라이브러리만: 명령마다 필요한 것만 늦게 불러, 의존성 하나가 깨져도
paths 같은 기본 명령은 돈다. 런처·러너는 이것을 쓰지 않는다(롤백 직후엔 코드 버전이 렌더된 런처와 다를 수 있다).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import stat
import sys
import time
import urllib.request
from pathlib import Path

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HOST_DEFAULT_PORT = 5100            # server.main 의 SM_PORT 기본값 — 설정 파일에 port 가 없을 때 Host 가 여는 포트

# [mac] launchd — 이름·자리는 install.sh·uninstall.sh 의 LAUNCHD_LABEL·"$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist" 와
# 같아야 한다(test_posix_scripts.test_launchd_names_agree)
LAUNCHD_LABEL = "com.clewpath.host"
LAUNCHD_PLIST = f"Library/LaunchAgents/{LAUNCHD_LABEL}.plist"           # 홈 아래
LAUNCHD_INTERVAL = 300               # 5분마다 foreground 한 번 — Windows 예약 작업의 5분 반복과 같은 주기
LAUNCHD_THROTTLE = 30                # 다시 띄우기 사이 최소 간격(초)
LAUNCHD_BASE_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")        # launchd 기본 PATH — 빠져 있으면 뒤에 붙인다
LAUNCHD_LANG = "en_US.UTF-8"         # 설치 때 LANG 이 UTF-8 이 아니면
# launchd 가 부르는 명령 — 런처가 있으면 foreground 로 이어 가고(exec, pid 그대로), 없으면(설치 폴더를 손으로 지움) 0 으로 끝낸다.
# 0 이 아닌 종료는 KeepAlive 가 30초마다 다시 부르는 신호라, 그대로 `/bin/sh <런처>` 를 부르면 런처가 없어진 뒤로 sh 가 127 로
# 끝나며 plist 가 남아 있는 한(로그인할 때마다) 쉼 없이 돈다
LAUNCHD_WRAPPER = '[ -f "$1" ] || exit 0; exec /bin/sh "$1" foreground'
LAUNCHD_ARGV0 = "clewpath-launchd"   # sh -c 의 $0 — ps 에서 알아보게


class HostctlError(Exception):
    """사람에게 보일 실패 — [FAIL] 한 줄, 종료코드 1."""


class _NoCP(Exception):
    """provision — Host 가 쓸 컨트롤플레인 주소가 없다(발급할 곳이 없다)."""


# ---------------------------------------------------------------- 출력

def _say(tag: str, msg: str) -> None:
    print(f"  [{tag}]{' ' * (5 - len(tag))}{msg}", file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    _say("OK", msg)


def warn(msg: str) -> None:
    _say("WARN", msg)


def fail(msg: str) -> None:
    _say("FAIL", msg)


def _out(**kv) -> None:
    """key=value 줄. 값은 파일 시스템 바이트 그대로(os.fsencode) — 로캘이 UTF-8 이 아니어도 셸이 같은 경로를 읽는다.
    줄바꿈·제어문자가 든 값은 내보내지 않는다(한 줄이 둘이 되면 install.sh 가 엉뚱한 값을 읽는다)."""
    lines = []
    for k, v in kv.items():
        s = "" if v is None else str(v)
        if _CTRL.search(s):
            raise HostctlError(f"{k} 에 줄바꿈·제어문자가 있습니다: {s!r}")
        lines.append(k.encode("ascii") + b"=" + os.fsencode(s) + b"\n")
    sys.stdout.flush()
    buf = getattr(sys.stdout, "buffer", None)
    if buf is None:
        sys.stdout.write(b"".join(lines).decode("utf-8", "replace"))
    else:
        buf.write(b"".join(lines))
        buf.flush()


# ---------------------------------------------------------------- Host 가 보는 경로

@contextlib.contextmanager
def _host_env(**extra: str):
    """Host 가 기동할 때와 같은 env 를 잠깐 만든다 — 런처는 CLEWPATH_CONFIG 로 설정 파일을 고정하고, Host(server.main)는
    맨 먼저 appconfig.apply_env() 로 그 파일의 값([claude] config_dir 등)을 env 로 펼친 뒤 데이터 폴더를 정한다.
    끝나면 env 를 통째로 되돌린다(같은 프로세스에서 부르는 시험이 SM_* 를 물려받지 않게)."""
    from session_manager import appconfig
    saved = dict(os.environ)
    conf = appconfig.config_path()
    try:
        os.environ["CLEWPATH_CONFIG"] = str(conf)
        os.environ.update(extra)
        appconfig.load(refresh=True)
        appconfig.apply_env()
        yield conf
    finally:
        os.environ.clear()
        os.environ.update(saved)
        appconfig.load(refresh=True)


def _host_paths() -> tuple[Path, Path, Path]:
    """(설정 파일, 데이터 폴더, 클로드 폴더) — 런처가 박을 값. 상대 경로면 Host(cwd = 설치 루트)와 갈리므로 거부한다."""
    from session_manager import config
    with _host_env() as conf:
        data, home = config.data_dir(), config.claude_home()
    for name, p in (("설정 파일", conf), ("데이터 폴더", data), ("클로드 폴더", home)):
        if not p.is_absolute():
            raise HostctlError(f"{name} 경로가 절대 경로가 아닙니다(SESSION_MANAGER_CLAUDE_HOME·CLAUDE_CONFIG_DIR·"
                               f"CLEWPATH_CONFIG 를 확인하세요): {p}")
    return conf, data, home


def _conf_path() -> Path:
    """설정 파일 자리만 — 런처가 CLEWPATH_CONFIG 로 박을 값(설정 파일 내용과 무관하다)."""
    from session_manager import appconfig
    conf = appconfig.config_path()
    if not conf.is_absolute():
        raise HostctlError(f"설정 파일 경로가 절대 경로가 아닙니다(CLEWPATH_CONFIG·SESSION_MANAGER_CLAUDE_HOME·"
                           f"CLAUDE_CONFIG_DIR 를 확인하세요): {conf}")
    return conf


def cmd_paths(a) -> int:
    conf, data, home = _host_paths()
    _out(data_dir=data, conf_file=conf, claude_home=home, projects_dir=home / "projects")
    return 0


# ---------------------------------------------------------------- 권한

def cmd_secure_data_dir(a) -> int:
    root = Path(a.data_dir) if a.data_dir else _host_paths()[1]
    if os.name != "posix":
        warn("이 OS 에서는 권한을 바꾸지 않습니다(맥·리눅스 전용)")
        _out(changed=0)
        return 0
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise HostctlError(f"데이터 폴더가 폴더가 아닙니다: {root}")
        changed = 0
        if stat.S_IMODE(os.stat(root).st_mode) != 0o700:
            os.chmod(root, 0o700)
            changed += 1
    except OSError as e:
        raise HostctlError(f"데이터 폴더 권한을 0700 으로 만들 수 없습니다: {root} ({e})") from e
    failed = 0
    for dirpath, dirnames, filenames in os.walk(root):            # followlinks=False — 링크 너머는 우리 것이 아닐 수 있다
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    continue                                       # 링크·소켓·파이프는 그대로
                mode = stat.S_IMODE(st.st_mode)
                if mode & 0o077:
                    os.chmod(p, mode & ~0o077)
                    changed += 1
            except OSError:
                failed += 1
    if failed:
        warn(f"권한을 바꾸지 못한 항목이 {failed}개 있습니다(데이터 폴더: {root})")
    ok(f"데이터 폴더 권한 0700 (group·other 권한을 뺀 항목 {changed}개): {root}")
    _out(changed=changed)
    return 0


# ---------------------------------------------------------------- 설정 파일

def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """임시 파일(같은 폴더)에 다 쓴 뒤 os.replace — 반쯤 쓴 파일을 Host 가 읽지 않게. 권한은 처음부터 mode(umask 무관)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _toml_port(conf: Path) -> int:
    """기존 설정 파일로 Host 가 실제로 열 포트 — [server] port, 없거나 읽을 수 없으면 Host 기본값."""
    import tomllib
    try:
        port = tomllib.loads(conf.read_text(encoding="utf-8-sig")).get("server", {}).get("port")
    except Exception as e:  # noqa: BLE001
        warn(f"기존 설정 파일을 읽지 못했습니다({type(e).__name__}) — Host 는 기본값으로 뜹니다: {conf}")
        return _HOST_DEFAULT_PORT
    if type(port) is int and 1 <= port <= 65535:
        return port
    return _HOST_DEFAULT_PORT


def _warn_kept_differences(conf: Path, a, client: str | None, agent: str | None) -> None:
    """유지(kept)할 때 버리는 인자·토큰이 설정 파일과 다르면 알린다 — 말없이 버리면 사용자는 준 값이 쓰인 줄 안다(토큰 값은 찍지 않는다)."""
    import tomllib
    try:
        cloud = tomllib.loads(conf.read_text(encoding="utf-8-sig")).get("cloud", {})
    except Exception:  # noqa: BLE001 — 읽지 못하는 파일은 _toml_port 가 이미 알렸다
        return
    if not isinstance(cloud, dict):
        return
    for key, opt, given in (("cp_url", "--cp-url", a.cp_url), ("relay_url", "--relay-url", a.relay_url),
                            ("app_url", "--app-url", a.app_url)):
        have = cloud.get(key)
        if given and (not isinstance(have, str) or have.rstrip("/") != given.rstrip("/")):
            shown = have if isinstance(have, str) and have else "(없음)"
            warn(f"설정 파일의 {key}({shown})를 씁니다 — {opt} {given} 는 설정 파일을 새로 만들 때만 씁니다"
                 f"(바꾸려면 설정 파일을 고친 뒤 재시작)")
    for key, env, given in (("client_token", "CLEWPATH_CLIENT_TOKEN", client),
                            ("agent_token", "CLEWPATH_AGENT_TOKEN", agent)):
        if given and cloud.get(key) != given:
            warn(f"설정 파일의 {key} 를 씁니다 — {env} 는 설정 파일을 새로 만들 때만 씁니다"
                 f"(바꾸려면 설정 파일의 [cloud] {key} 를 고친 뒤 재시작)")


def cmd_write_config(a) -> int:
    from session_manager import appconfig
    conf = Path(a.conf_file) if a.conf_file else _conf_path()
    client = os.environ.get("CLEWPATH_CLIENT_TOKEN") or None
    agent = os.environ.get("CLEWPATH_AGENT_TOKEN") or None
    if conf.exists() or conf.is_symlink():                         # 사용자가 고친 값을 지킨다 — 재설치도 덮지 않는다
        port = _toml_port(conf)
        ok(f"기존 설정 파일 유지: {conf}")
        if port != a.port:
            warn(f"설정 파일의 포트({port})를 씁니다 — --port {a.port} 는 설정 파일을 새로 만들 때만 씁니다"
                 f"(바꾸려면 [server] port 를 고친 뒤 재시작)")
        _warn_kept_differences(conf, a, client, agent)
        _out(config="kept", path=conf, port=port)
        return 0
    for name, v in (("--cp-url", a.cp_url), ("--relay-url", a.relay_url), ("--app-url", a.app_url),
                    ("--claude-config-dir", a.claude_config_dir or ""), ("CLEWPATH_CLIENT_TOKEN", client or ""),
                    ("CLEWPATH_AGENT_TOKEN", agent or "")):
        if _CTRL.search(v):
            raise HostctlError(f"{name} 에 줄바꿈·제어문자가 있습니다")
    text = appconfig.render_template(port=a.port, cp_url=a.cp_url.rstrip("/"), relay_url=a.relay_url,
                                     app_url=a.app_url, client_token=client, agent_token=agent,
                                     claude_config_dir=a.claude_config_dir or None)
    import tomllib
    try:
        tomllib.loads(text)                                         # 깨진 파일을 쓰면 Host 는 기본값으로 뜬다(무음)
    except tomllib.TOMLDecodeError as e:
        raise HostctlError(f"설정 파일 본문이 TOML 로 읽히지 않습니다: {e}") from e
    _atomic_write(conf, text.encode("utf-8"), 0o600)
    ok(f"설정 파일 생성: {conf}")
    if not client and not a.cp_url:      # 컨트롤플레인이 있으면 QR 은 기기 전용 자격증명을 싣는다(공유 토큰 불필요 — 맥 실기 3b)
        warn("폰 접속 토큰(CLEWPATH_CLIENT_TOKEN)이 없습니다 — 컨트롤플레인 없이 폰 페어링 QR 을 쓰려면 설정 파일의 "
             "client_token 을 채우세요.")
    _out(config="created", path=conf, port=a.port)
    return 0


# ---------------------------------------------------------------- 런처

def cmd_render_launcher(a) -> int:
    from session_manager.osdep import _posix
    root = Path(a.root)
    tpl = root / "session_manager" / _posix.LAUNCHER_TEMPLATE
    if not tpl.is_file():
        raise HostctlError(f"런처 템플릿이 없습니다: {tpl}")
    try:
        res = _posix.write_launcher(root, data_dir=a.data_dir, conf_file=a.conf_file, port=a.port)
    except ValueError as e:
        raise HostctlError(f"런처를 만들 수 없습니다: {e}") from e
    what = {"created": "생성", "updated": "갱신", "unchanged": "그대로(내용이 같음)"}[res]
    ok(f"{_posix.START_SCRIPT} {what}: {root / _posix.START_SCRIPT}")
    _out(launcher=res)
    return 0


# ---------------------------------------------------------------- [mac] 자동 시작(launchd)

def launchd_path(path_env: str | None) -> str:
    """launchd 아래 Host 의 PATH — 설치 때 PATH 에서 절대 경로 조각만(상대·빈 조각·~·제어문자는 버린다), 겹치지 않게, 끝에
    launchd 기본 PATH 를 빠진 것만 붙인다. launchd 의 기본 PATH(/usr/bin:/bin:/usr/sbin:/sbin)로는 ~/.local/bin·
    /opt/homebrew/bin 의 claude 를 못 찾는다(재개·터미널이 claude 를 못 띄운다)."""
    out: list[str] = []
    for p in (path_env or "").split(":") + list(LAUNCHD_BASE_PATH):
        p = (p.rstrip("/") or "/") if p.startswith("/") else ""
        if p and not _CTRL.search(p) and p not in out:
            out.append(p)
    return ":".join(out)


def launchd_lang(lang: str | None) -> str:
    """설치 때 LANG 이 UTF-8 로캘이면 그대로, 아니면(없음·C·다른 인코딩·이상한 글자) en_US.UTF-8 — launchd 는 LANG 을 주지
    않아, 그대로 두면 Host 안의 claude·셸이 C 로캘(한글이 깨진다)로 돈다."""
    if lang and re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", lang) and re.search(r"\.utf-?8(?:@|$)", lang, re.I):
        return lang
    return LAUNCHD_LANG


def launchd_plist(root: str, data_dir: str, *, path_env: str | None, lang: str | None, umask: int | None) -> dict:
    """com.clewpath.host 의 내용(plistlib 가 쓸 dict) — 순수 함수(시험이 쉽게).

    - 로그인할 때(RunAtLoad) 켜고, 5분마다(StartInterval) foreground 를 한 번 돌려 살핀다 — 러너가 업데이트 뒤 launchd 밖에서
      띄운 Host 가 죽어도, 사람이 stop(--hold 없이)한 Host 도 5분 안에 다시 뜬다(Windows 예약 작업과 같다). 계속 끄려면 stop --hold.
      Host 가 떠 있으면 foreground 는 곧 0 으로 끝난다(launchd 는 같은 작업을 둘 띄우지 않는다).
    - KeepAlive{SuccessfulExit: false}: 0 이 아닌 종료·신호로 죽었을 때만 다시(ThrottleInterval 30초). foreground 는 띄우지 않을
      때 늘 0 이고(유지보수·업데이트 중·이미 떠 있음·막힘), 업데이트로 내려가는 Host 는 0 으로 끝난다. (SIGTERM 정상 종료는 지금
      uvicorn 이 그 신호를 다시 일으켜 신호 종료로 보여 30초 뒤 다시 불린다 — stop --hold 면 foreground 가 곧 0 이라 그대로 꺼져 있다)
    - AbandonProcessGroup: Host 가 끝날 때 launchd 가 그 프로세스 그룹을 죽이지 않게 — 터미널 관리 프로세스(브로커)·업데이트
      러너는 Host 재기동·업데이트에도 살아야 한다. ProcessType Interactive: Background 의 QoS 제한이 claude 자식에 물려간다.
    - env 는 PATH·LANG 만(HOME·TMPDIR·USER 는 launchd 가 준다 — 사용자 환경을 바꾸지 않는다, S38). umask 도 설치 때 셸의 것을
      그대로(launchd 기본값과 다르면 Host 안의 claude 가 만드는 파일 권한이 달라진다).
    - WorkingDirectory 는 두지 않는다: 런처는 제 경로($0)로 설치 루트를 찾고 Host 를 띄우기 직전에 그리로 옮겨 간다. 두면 설치
      폴더를 손으로 지웠을 때 launchd 가 작업을 띄우지조차 못할 수 있어(chdir 실패 — 맥 확인 거리) 런처 없음 → 0 으로 끝내는
      감싸개가 소용없어진다.
    - 출력: foreground 셸 단계의 말만 launchd.log 로 — exec 한 Host 는 런처가 host.log·host.err.log 로 돌린다(start 와 같다).
    """
    root = root.rstrip("/") or "/"
    data_dir = data_dir.rstrip("/") or "/"
    doc = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/bin/sh", "-c", LAUNCHD_WRAPPER, LAUNCHD_ARGV0, f"{root}/start-connector.sh"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StartInterval": LAUNCHD_INTERVAL,
        "ThrottleInterval": LAUNCHD_THROTTLE,
        "AbandonProcessGroup": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": launchd_path(path_env), "LANG": launchd_lang(lang)},
        "StandardOutPath": f"{data_dir}/launchd.log",
        "StandardErrorPath": f"{data_dir}/launchd.log",
    }
    if isinstance(umask, int) and not isinstance(umask, bool) and 0 <= umask <= 0o777:
        doc["Umask"] = umask
    return doc


def plist_launcher(raw: bytes) -> str | None:
    """기존 plist 가 부르는 런처(…/start-connector.sh) — 없으면 "", 읽을 수 없는 파일이면 None."""
    import plistlib
    try:
        d = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 — 깨진 XML·다른 형식
        return None
    args = d.get("ProgramArguments") if isinstance(d, dict) else None
    if not isinstance(args, list):
        return ""
    return next((a for a in args if isinstance(a, str) and a.endswith("/start-connector.sh")), "")


def _current_umask() -> int:
    m = os.umask(0o022)
    os.umask(m)
    return m


def cmd_render_launchd(a) -> int:
    import plistlib
    from session_manager.osdep import _posix
    for name, v in (("--root", a.root), ("--data-dir", a.data_dir)):
        if _CTRL.search(v) or not v.startswith("/"):                # plist 에 박는 값 — 맥 절대 경로만
            raise HostctlError(f"{name} 는 절대 경로여야 하고 제어문자가 없어야 합니다: {v!r}")
    plist = Path(a.plist)
    if _CTRL.search(a.plist) or not plist.is_absolute():
        raise HostctlError(f"--plist 는 절대 경로여야 합니다: {a.plist!r}")
    root = a.root.rstrip("/") or "/"
    launcher = f"{root}/{_posix.START_SCRIPT}"
    try:                                                            # 반복 실행 주체는 멱등 런처를 확인한 뒤에만 둔다(S22)
        head = Path(launcher).read_bytes()[:1024].decode("utf-8", "replace").split("\n")
    except OSError as e:
        raise HostctlError(f"런처가 없어 자동 시작을 등록하지 않습니다(먼저 render-launcher): {launcher}") from e
    if len(head) < 2 or "clewpath-launcher v2 (posix)" not in head[1]:
        raise HostctlError(f"멱등 런처(clewpath-launcher v2)가 아니라 자동 시작을 등록하지 않습니다: {launcher}")
    doc = launchd_plist(root, a.data_dir, path_env=os.environ.get("PATH"), lang=os.environ.get("LANG"),
                        umask=_current_umask() if os.name == "posix" else None)
    new = plistlib.dumps(doc, fmt=plistlib.FMT_XML, sort_keys=True)
    old = None
    if plist.is_file() or plist.is_symlink():
        try:
            old = plist.read_bytes()
        except OSError:
            old = b""
        prev = plist_launcher(old)
        if prev is None:
            warn(f"읽을 수 없는 기존 자동 시작 등록 파일을 새로 씁니다: {plist}")
        elif prev and prev != launcher:
            warn(f"다른 설치본의 자동 시작({prev})을 이 설치본으로 바꿉니다 — 설치본은 사용자당 하나입니다")
    if old == new:
        res = "unchanged"
    else:
        d = plist.parent
        if not d.is_dir():                                          # ~/Library/LaunchAgents — 없으면 0755 로
            d.mkdir(mode=0o755, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o755)
        try:                                                        # 0644 — launchd 는 남이 쓸 수 있는 plist 를 거부한다
            _atomic_write(plist, new, 0o644)
        except OSError as e:
            raise HostctlError(f"자동 시작 등록 파일을 쓸 수 없습니다: {plist} ({e})") from e
        res = "created" if old is None else "updated"
    what = {"created": "생성", "updated": "갱신", "unchanged": "그대로(내용이 같음)"}[res]
    ok(f"자동 시작 등록 파일 {what}: {plist}")
    _out(launchd=res, plist=plist, label=LAUNCHD_LABEL)
    return 0


# ---------------------------------------------------------------- 자격증명

def cmd_provision(a) -> int:
    """install.ps1 의 미리 발급과 같다 — 실패해도 설치는 계속(Host 가 뜬 뒤 다시 시도하고, 그동안 공유토큰으로 동작).
    컨트롤플레인은 Host 와 같은 규칙(env > 설정 파일)으로 정한다. --cp-url 은 설정 파일이 없을 때만 — 유지한 설정 파일을 두고
    인자(install.sh 의 기본값 = 운영 CP)로 발급하면 Host 가 쓰지 않는 CP 에 익명 커넥터가 등록된다."""
    connector, reason, state = "", "", "failed"
    try:
        from session_manager import appconfig, cp_client, systls
        with _host_env() as conf:
            if not cp_client.cp_url() and not (conf.exists() or conf.is_symlink()):
                os.environ["SM_CP_URL"] = a.cp_url.rstrip("/")
            cp = cp_client.cp_url()
            if not cp:
                raise _NoCP
            systls.use_system_trust()
            creds = cp_client.load_creds()
            if creds:
                state, connector, reason = "kept", str(creds.get("connector_id") or ""), "exists"
                ok("이미 발급된 자격증명이 있습니다(재사용)")
                if creds.get("cp_url") and str(creds.get("cp_url")).rstrip("/") != cp:
                    warn(f"기존 자격증명은 다른 컨트롤플레인 것입니다({creds.get('cp_url')}) — Host 가 뜰 때 새로 발급받습니다.")
            else:
                import httpx
                with httpx.Client() as client, contextlib.redirect_stdout(io.StringIO()):   # 발급 로그(room 포함)는 버린다
                    got = cp_client.provision(client, appconfig.machine_name())
                if got:
                    state, connector, reason = "created", str(got.get("connector_id") or ""), "ok"
                else:
                    reason = str(cp_client._state.get("reason") or "unknown")
    except _NoCP:
        state, reason = "failed", "no_cp_url"
    except Exception as e:  # noqa: BLE001 — 응답 모양이 이상해도 설치를 멈추지 않는다
        state, reason = "failed", f"provision_error:{type(e).__name__}"
    if state == "created":
        ok(f"자격증명 발급 완료 (connector {connector})")
    elif state == "failed":
        if reason == "no_cp_url":
            warn("설정 파일에 컨트롤플레인 주소([cloud] cp_url)가 없어 발급하지 않습니다 — Host 도 컨트롤플레인 없이 동작합니다.")
        elif reason == "provision_rate_limited":
            warn("발급 한도 초과(429). 잠시 후 다시 시도하세요 — 서비스는 공유토큰으로도 동작합니다.")
        else:
            warn(f"발급 실패: {reason}")
            warn("괜찮습니다 — 서버가 뜬 뒤 자동 재시도하며, 그동안 공유토큰으로 동작합니다(fail-open).")
    _out(creds=state, connector=connector, reason=reason)
    return 0


# ---------------------------------------------------------------- 기동 확인

def _get_json(opener, port: int, path: str, timeout: float) -> dict | None:
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            body = json.loads(r.read(1 << 20))
        return body if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _runtime_port(rt: Path) -> int | None:
    try:
        port = json.loads(rt.read_text(encoding="utf-8-sig")).get("port")
    except Exception:  # noqa: BLE001
        return None
    return port if type(port) is int and 0 < port < 65536 else None


def _tail(p: Path, n: int) -> list[str]:
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def _show_logs(data: Path) -> None:
    """기동이 안 됐을 때 원인을 그 자리에서 — 사용자가 '안 돼요'만 보낼 수밖에 없던 문제(install.ps1 과 같은 순서)."""
    err, log, launcher = data / "host.err.log", data / "host.log", data / "launcher.log"
    if err.is_file() and err.stat().st_size > 0:
        warn(f"오류 로그 끝부분 ({err}):")
        for line in _tail(err, 8):
            print(f"    ! {line}", file=sys.stderr)
    elif log.is_file():
        warn(f"로그 끝부분 ({log}):")
        for line in _tail(log, 8):
            print(f"    | {line}", file=sys.stderr)
    else:
        warn("로그 파일이 아예 없습니다 = 런처가 Host 를 띄우지 못했습니다.")
    if launcher.is_file():
        warn(f"런처 기록 끝부분 ({launcher}):")
        for line in _tail(launcher, 3):
            print(f"    > {line}", file=sys.stderr)


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def cmd_wait_ready(a) -> int:
    """install.ps1 의 기동 확인과 같다 — 1초 간격으로 runtime.json 의 실제 포트를 따라가며 /api/owner/connection 을 본다.
    답한 Host 의 진단(claude_home)이 이 설치본이 볼 클로드 폴더와 다르면 남의 Host(맥 두 계정 — 같은 포트의 다른 사용자)다 —
    이 설치본의 것이 뜨기를 더 기다리고, 끝내 그러면 down. 늘 종료코드 0(경고만): 설치 자체는 끝났고, Host 가 늦게 뜨거나
    실패해도 설치를 되돌릴 일은 아니다."""
    data = Path(a.data_dir) if a.data_dir else _host_paths()[1]
    try:
        expect = str(_host_paths()[2])
    except HostctlError:
        expect = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 루프백은 프록시로 보내지 않는다
    port, conn, diag, foreign = a.port, None, {}, None
    for i in range(a.tries):
        port = _runtime_port(data / "runtime.json") or port
        conn = _get_json(opener, port, "/api/owner/connection", 3)
        if conn is not None:
            diag = _get_json(opener, port, "/api/owner/diagnostics", 5) or {}
            home = diag.get("claude_home")
            if not (expect and isinstance(home, str) and home and not _same_dir(home, expect)):
                break
            foreign, conn, diag = (port, home), None, {}
        if i + 1 < a.tries:
            time.sleep(1)
    if conn is None:
        if foreign:
            warn(f"다른 사용자의 ClewPath 가 {foreign[0]} 번 포트를 쓰고 있습니다(그쪽 클로드 폴더: {foreign[1]}) — "
                 f"이 설치본의 Host 가 아닙니다.")
            warn("설정 파일의 [server] port 를 다른 값으로 바꾼 뒤 다시 시작하세요.")
        else:
            warn(f"기동 확인 실패 — http://127.0.0.1:{port}/ 가 열리지 않습니다.")
        _show_logs(data)
        _out(state="down", port=port, mode="", session_count="", claude_exe_found="")
        return 0
    if port != a.port:
        warn(f"{a.port} 가 사용 중이라 {port} 번 포트로 열렸습니다.")
    count, found = diag.get("session_count"), bool(diag.get("claude_exe_found"))
    if diag:
        if diag.get("ok"):
            ok(f"세션 인식됨: {count} 개 (폴더: {diag.get('claude_home')})")
        else:
            warn(f"세션을 못 찾았습니다 (폴더: {diag.get('claude_home')}, 개수: {count})")
        if not found:
            warn("claude 실행파일을 못 찾았습니다 — 세션 재개가 안 됩니다.")
    mode = str(conn.get("mode") or "")
    ok(f"서비스 기동 확인 (연결 모드: {mode or '?'})")
    if mode == "fallback":
        warn("컨트롤플레인에 닿지 못해 공유토큰으로 접속 중입니다(기능은 정상).")
    _out(state="up", port=port, mode=mode, session_count="" if count is None else count,
         claude_exe_found=1 if found else 0)
    return 0


# ---------------------------------------------------------------- 진입점

def _port(s: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,4}", s or "") or int(s) > 65535:
        raise argparse.ArgumentTypeError(f"포트는 1-65535: {s!r}")
    return int(s)


def _tries(s: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,2}", s or ""):
        raise argparse.ArgumentTypeError(f"횟수는 1-999: {s!r}")
    return int(s)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -I -m session_manager.hostctl",
                                description="ClewPath 설치 도우미(맥·리눅스 install.sh 전용). 토큰은 env 로만 받는다.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="명령")
    sub.add_parser("paths", help="Host 가 볼 경로")
    s = sub.add_parser("secure-data-dir", help="데이터 폴더 권한(0700, group·other 제거)")
    s.add_argument("--data-dir")
    s = sub.add_parser("write-config", help="clewpath.toml(없을 때만). 토큰은 env CLEWPATH_CLIENT_TOKEN·CLEWPATH_AGENT_TOKEN")
    s.add_argument("--port", type=_port, default=_HOST_DEFAULT_PORT)
    s.add_argument("--cp-url", default="https://clewpath.pyongso.com/cp")
    s.add_argument("--relay-url", default="wss://clewpath.pyongso.com/relay/ws")
    s.add_argument("--app-url", default="https://clewpath.pyongso.com/relay/app")
    s.add_argument("--claude-config-dir", default="")
    s.add_argument("--conf-file")
    s = sub.add_parser("render-launcher", help="start-connector.sh 렌더")
    s.add_argument("--root", required=True)
    s.add_argument("--data-dir", required=True)
    s.add_argument("--conf-file", required=True)
    s.add_argument("--port", type=_port, required=True)
    s = sub.add_parser("provision", help="CP 자격증명 발급(fail-open)")
    s.add_argument("--cp-url", required=True)
    s = sub.add_parser("wait-ready", help="기동 확인")
    s.add_argument("--port", type=_port, required=True)
    s.add_argument("--tries", type=_tries, default=20)
    s.add_argument("--data-dir")
    s = sub.add_parser("render-launchd", help="[mac] 자동 시작 등록 파일(plist). PATH·LANG 은 지금 env 에서")
    s.add_argument("--root", required=True)
    s.add_argument("--data-dir", required=True)
    s.add_argument("--plist", required=True)
    return p


_COMMANDS = {"paths": cmd_paths, "secure-data-dir": cmd_secure_data_dir, "write-config": cmd_write_config,
             "render-launcher": cmd_render_launcher, "provision": cmd_provision, "wait-ready": cmd_wait_ready,
             "render-launchd": cmd_render_launchd}


def main(argv: list[str] | None = None) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:                                         # 사용법 오류는 2, -h 는 0
        return e.code if isinstance(e.code, int) else 2
    try:
        return _COMMANDS[a.cmd](a)
    except HostctlError as e:
        fail(str(e))
        return 1
    except Exception as e:  # noqa: BLE001 — 설치 스크립트가 원인을 보여 줄 수 있게 한 줄로
        fail(f"{a.cmd} 실패: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
