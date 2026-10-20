"""설치 도우미 — 맥·리눅스 install.sh 가 부르는 작은 명령들(docs/designs/posix-install.md 4.3).

    .venv/bin/python -I -m session_manager.hostctl <명령> [인자]

표준출력은 key=value 줄(install.sh 가 sed 로 읽는다), 사람에게 하는 말은 표준오류([OK]·[WARN]·[FAIL] — install.ps1 과
같은 말). 종료코드 0 성공 / 1 오류 / 2 사용법.

  paths            Host 가 볼 경로 → data_dir= conf_file= claude_home= projects_dir=
  secure-data-dir  데이터 폴더 0700, 그 아래 파일·폴더에서 group·other 권한을 뺀다(심볼릭 링크는 건너뜀) → changed=
  write-config     clewpath.toml 이 없을 때만 만든다(0600·BOM 없음·tomllib 검증·원자적) → config=created|kept path= port=
  render-launcher  start-connector.sh 를 템플릿으로(0755·원자적) → launcher=created|updated|unchanged
  provision        CP 자격증명(있으면 그대로, 실패는 경고만 — fail-open) → creds=kept|created|failed connector= reason=
  wait-ready       기동 확인(runtime.json 의 실제 포트를 따라간다) → state=up|down port= mode= session_count= claude_exe_found=

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


class HostctlError(Exception):
    """사람에게 보일 실패 — [FAIL] 한 줄, 종료코드 1."""


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


def cmd_write_config(a) -> int:
    from session_manager import appconfig
    conf = Path(a.conf_file) if a.conf_file else _conf_path()
    if conf.exists() or conf.is_symlink():                         # 사용자가 고친 값을 지킨다 — 재설치도 덮지 않는다
        port = _toml_port(conf)
        ok(f"기존 설정 파일 유지: {conf}")
        if port != a.port:
            warn(f"설정 파일의 포트({port})를 씁니다 — --port {a.port} 는 설정 파일을 새로 만들 때만 씁니다"
                 f"(바꾸려면 [server] port 를 고친 뒤 재시작)")
        _out(config="kept", path=conf, port=port)
        return 0
    client = os.environ.get("CLEWPATH_CLIENT_TOKEN") or None
    agent = os.environ.get("CLEWPATH_AGENT_TOKEN") or None
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
    if not client:
        warn("폰 접속 토큰(CLEWPATH_CLIENT_TOKEN)이 없습니다 — 폰 페어링 QR 은 설정 파일의 client_token 을 채우면 됩니다.")
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


# ---------------------------------------------------------------- 자격증명

def cmd_provision(a) -> int:
    """install.ps1 의 미리 발급과 같다 — 실패해도 설치는 계속(Host 가 뜬 뒤 다시 시도하고, 그동안 공유토큰으로 동작)."""
    cp = a.cp_url.rstrip("/")
    connector, reason, state = "", "", "failed"
    try:
        from session_manager import appconfig, cp_client, systls
        with _host_env(SM_CP_URL=cp):
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
    except Exception as e:  # noqa: BLE001 — 응답 모양이 이상해도 설치를 멈추지 않는다
        state, reason = "failed", f"provision_error:{type(e).__name__}"
    if state == "created":
        ok(f"자격증명 발급 완료 (connector {connector})")
    elif state == "failed":
        if reason == "provision_rate_limited":
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


def cmd_wait_ready(a) -> int:
    """install.ps1 의 기동 확인과 같다 — 1초 간격으로 runtime.json 의 실제 포트를 따라가며 /api/owner/connection 을 본다.
    늘 종료코드 0(경고만): 설치 자체는 끝났고, Host 가 늦게 뜨거나 실패해도 설치를 되돌릴 일은 아니다."""
    data = Path(a.data_dir) if a.data_dir else _host_paths()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 루프백은 프록시로 보내지 않는다
    port, conn = a.port, None
    for i in range(a.tries):
        port = _runtime_port(data / "runtime.json") or port
        conn = _get_json(opener, port, "/api/owner/connection", 3)
        if conn is not None:
            break
        if i + 1 < a.tries:
            time.sleep(1)
    if conn is None:
        warn(f"기동 확인 실패 — http://127.0.0.1:{port}/ 가 열리지 않습니다.")
        _show_logs(data)
        _out(state="down", port=port, mode="", session_count="", claude_exe_found="")
        return 0
    if port != a.port:
        warn(f"{a.port} 가 사용 중이라 {port} 번 포트로 열렸습니다.")
    diag = _get_json(opener, port, "/api/owner/diagnostics", 5) or {}
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
    return p


_COMMANDS = {"paths": cmd_paths, "secure-data-dir": cmd_secure_data_dir, "write-config": cmd_write_config,
             "render-launcher": cmd_render_launcher, "provision": cmd_provision, "wait-ready": cmd_wait_ready}


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
