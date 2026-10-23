"""멀티 OS 1단계(docs/designs/multi-os.md): OS 의존 코드는 session_manager/osdep 뒤에만 있다."""
from __future__ import annotations

import inspect
import os
import re
import sys
from pathlib import Path, PurePosixPath

import pytest

from session_manager import osdep
from session_manager.osdep import _posix, _win

PKG = Path(__file__).resolve().parents[1] / "session_manager"

# 아직 경계 밖에 남은 Windows 전용 모듈(이유)
_ALLOWED = {
    "osdep/_win.py",          # Windows 구현 자체
    "jobguard.py",            # Job 객체 — _win.guard_child 가 부른다
    "peers.py",               # psutil 이 빠진 설치본용 CIM 폴백(os.name 으로 막혀 있음)
}
_WIN_ONLY = re.compile(r"from winpty|import winpty|windll|\"powershell\"|'powershell'|\"taskkill\"|Get-CimInstance")


def test_common_code_has_no_windows_calls():
    bad = []
    for p in PKG.rglob("*.py"):
        rel = p.relative_to(PKG).as_posix()
        if rel in _ALLOWED:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            if _WIN_ONLY.search(line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, "OS 의존 호출은 osdep 으로:\n" + "\n".join(bad)


def test_both_implementations_expose_the_same_api():
    for name in osdep.__all__:
        if name in ("IS_WIN", "IS_MAC"):
            continue
        a, b = getattr(_win, name), getattr(_posix, name)
        assert callable(a) == callable(b), name
        if callable(a):
            sa, sb = inspect.signature(a).parameters, inspect.signature(b).parameters
            assert list(sa) == list(sb), name
            assert [p.kind for p in sa.values()] == [p.kind for p in sb.values()], name   # 키워드 전용까지 같게


def test_curl_and_port_free_are_public():
    assert "CURL" in osdep.__all__ and "port_free" in osdep.__all__
    assert osdep.CURL == ("curl.exe" if sys.platform == "win32" else "curl")
    assert (_win.CURL, _posix.CURL) == ("curl.exe", "curl")
    assert osdep.port_free is (_win.port_free if sys.platform == "win32" else _posix.port_free)


def test_picks_the_running_os():
    assert osdep.NAME == ("win" if sys.platform == "win32" else "mac" if sys.platform == "darwin" else "linux")
    assert osdep.pid_alive(os.getpid())
    assert not osdep.pid_alive(0)


def test_posix_process_listing_uses_psutil_shape():
    me = [p for p in _posix.list_processes("python") if p["pid"] == os.getpid()]
    assert me and set(me[0]) == {"pid", "ppid", "cmdline"} and isinstance(me[0]["cmdline"], str)


def test_win_update_runner_args_unchanged():
    a = _win.update_runner_argv(Path("r.ps1"), install_root=Path("I"), stage_dir=Path("S"), backup_dir=Path("B"),
                                wait_pid=7, port=5100, version="1.2.3", log_file=Path("L"), restart_script="X.ps1")
    assert a[1:6] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "r.ps1"]
    assert a[a.index("-WaitPid") + 1] == "7" and a[-2:] == ["-RestartScript", "X.ps1"]
    assert "-RestartScript" not in _win.update_runner_argv(
        Path("r.ps1"), install_root=Path("I"), stage_dir=Path("S"), backup_dir=Path("B"),
        wait_pid=7, port=5100, version="1", log_file=Path("L"))


def test_posix_update_runner_argv_shape():
    """실행 비트에 기대지 않고 /bin/sh 로 부른다. 플래그 이름·순서는 ps1 판과 같다(러너가 같은 인자 해석)."""
    kw = dict(install_root=Path("I"), stage_dir=Path("S"), backup_dir=Path("B"),
              wait_pid=7, port=5100, version="1.2.3", log_file=Path("L"))
    a = _posix.update_runner_argv(Path("r.sh"), restart_script="R/start-connector.sh", **kw)
    assert a[:2] == ["/bin/sh", "r.sh"]
    assert a[a.index("-Port") + 1] == "5100" and a[a.index("-WaitPid") + 1] == "7"
    assert a[a.index("-Version") + 1] == "1.2.3" and a[-2:] == ["-RestartScript", "R/start-connector.sh"]
    assert "-RestartScript" not in _posix.update_runner_argv(Path("r.sh"), **kw)
    w = _win.update_runner_argv(Path("r.sh"), restart_script="R/start-connector.sh", **kw)
    assert a[2:] == w[w.index("r.sh") + 1:]          # 러너가 받는 인자(pwsh 옵션 뒤)는 글자까지 같다


# ---- 맥·리눅스 런처 렌더·보장(docs/designs/posix-install.md 4.1) — 파일 작업이라 모든 OS 에서 돈다 ----

_TPL = ("#!/bin/sh\n# clewpath-launcher v2 (posix) — 시험용\nDATA_DIR={{DATA_DIR}}\nCONF_FILE={{CONF_FILE}}\n"
        "DEFAULT_PORT={{PORT}}\n")
_D, _C = PurePosixPath("/home/u x/.claude/session_manager"), PurePosixPath("/home/u x/.claude/session_manager/clewpath.toml")


def test_render_launcher_quotes_and_validates():
    out = _posix.render_launcher(_TPL, data_dir=_D, conf_file=_C, port=5123)
    assert "DATA_DIR='/home/u x/.claude/session_manager'\n" in out and "DEFAULT_PORT=5123\n" in out
    assert "{{" not in out
    # 한 번에 치환 — 경로 속 '{{PORT}}' 는 글자 그대로(두 번 바뀌지 않음)
    odd = _posix.render_launcher(_TPL, data_dir=PurePosixPath("/x/{{PORT}}"), conf_file=_C, port=5100)
    assert "DATA_DIR='/x/{{PORT}}'\n" in odd
    # 편집기가 남긴 BOM·CRLF 는 걷어낸다(sh 가 \r 을 명령의 일부로 읽는다)
    crlf = _posix.render_launcher("\ufeff" + _TPL.replace("\n", "\r\n"), data_dir=_D, conf_file=_C, port=5100)
    assert "\r" not in crlf and crlf.startswith("#!/bin/sh\n")
    bad = [dict(data_dir=PurePosixPath("/a\nb")), dict(data_dir="/a\x00b"), dict(data_dir="rel/dir"),
           dict(conf_file="/a\tb"), dict(port=0), dict(port=65536), dict(port="5100"), dict(port=True)]
    for over in bad:
        with pytest.raises(ValueError):
            _posix.render_launcher(_TPL, **{**dict(data_dir=_D, conf_file=_C, port=5100), **over})
    with pytest.raises(ValueError):                                  # 자리표시가 빠진 템플릿
        _posix.render_launcher(_TPL.replace("{{PORT}}", "5100"), data_dir=_D, conf_file=_C, port=5100)
    with pytest.raises(ValueError):                                  # 모르는 자리표시
        _posix.render_launcher(_TPL + "X={{ROOT}}\n", data_dir=_D, conf_file=_C, port=5100)


def _install_root(tmp_path, tpl=_TPL):
    root = tmp_path / "app"
    (root / "session_manager").mkdir(parents=True)
    if tpl is not None:
        (root / "session_manager" / _posix.LAUNCHER_TEMPLATE).write_bytes(tpl.encode("utf-8"))
    return root


def test_ensure_launcher_renders_compares_and_replaces(tmp_path):
    root = _install_root(tmp_path)
    target = root / _posix.START_SCRIPT
    assert _posix.ensure_launcher(root, _D, _C, 5100) == "launcher: regenerated (posix v2)"
    first = target.read_bytes()
    assert b"DEFAULT_PORT=5100\n" in first and b"\r" not in first and not first.startswith(b"\xef\xbb\xbf")
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o755
    ino = target.stat().st_ino
    # 같으면 손대지 않는다
    assert _posix.ensure_launcher(root, _D, _C, 5100) == "launcher: ok (posix v2)"
    assert target.stat().st_ino == ino
    # 폴백 포트(5101)로 뜬 날에도 기존 런처의 DEFAULT_PORT 를 지킨다
    assert _posix.ensure_launcher(root, _D, _C, 5101) == "launcher: ok (posix v2)"
    # 템플릿이 바뀐 업데이트는 전파된다(원자 교체 — 임시 파일이 남지 않음)
    (root / "session_manager" / _posix.LAUNCHER_TEMPLATE).write_text(_TPL + "# v2.1\n", encoding="utf-8")
    assert _posix.ensure_launcher(root, _D, _C, 5101) == "launcher: regenerated (posix v2)"
    now = target.read_bytes()
    assert now.endswith(b"# v2.1\n") and b"DEFAULT_PORT=5100\n" in now
    assert sorted(p.name for p in root.iterdir()) == ["session_manager", _posix.START_SCRIPT]


def test_ensure_launcher_failure_keeps_old_launcher(tmp_path):
    assert _posix.ensure_launcher(_install_root(tmp_path, tpl=None), _D, _C, 5100) == "launcher: template missing — skip"
    root = _install_root(tmp_path / "b", tpl=_TPL + "X={{ROOT}}\n")
    (root / _posix.START_SCRIPT).write_bytes(b"#!/bin/sh\n# old\n")
    with pytest.raises(RuntimeError, match="^launcher: regenerate failed:"):   # 실패는 예외로(liveness 가 표식을 안 쓰게)
        _posix.ensure_launcher(root, _D, _C, 5100)
    assert (root / _posix.START_SCRIPT).read_bytes() == b"#!/bin/sh\n# old\n"


def test_write_launcher_reports_what_it_did(tmp_path):
    """install.sh(hostctl render-launcher)가 쓸 원자 쓰기 — created|updated|unchanged. 지정한 포트를 그대로 쓴다
    (기존 DEFAULT_PORT 를 지키는 것은 Host 기동 때의 ensure_launcher 몫)."""
    root = _install_root(tmp_path)
    target = root / _posix.START_SCRIPT
    assert _posix.write_launcher(root, data_dir=_D, conf_file=_C, port=5100) == "created"
    ino = target.stat().st_ino
    assert _posix.write_launcher(root, data_dir=_D, conf_file=_C, port=5100) == "unchanged"
    assert target.stat().st_ino == ino
    assert _posix.write_launcher(root, data_dir=_D, conf_file=_C, port=5123) == "updated"
    assert b"DEFAULT_PORT=5123\n" in target.read_bytes()
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in root.iterdir()) == ["session_manager", _posix.START_SCRIPT]
    with pytest.raises(OSError):                                     # 템플릿 없음 — 런처를 만들지 않는다
        _posix.write_launcher(_install_root(tmp_path / "b", tpl=None), data_dir=_D, conf_file=_C, port=5100)
    assert not (tmp_path / "b" / "app" / _posix.START_SCRIPT).exists()


def test_real_launcher_template_renders_and_keeps_its_port(tmp_path):
    """진짜 템플릿(session_manager/start-connector.template.sh): 자리표시가 다 채워지고, ensure_launcher 가 기존
    DEFAULT_PORT 를 다시 읽을 수 있어야 한다 — 그 줄 모양이 바뀌면 폴백 포트(5101)가 설정 포트를 덮어쓴다."""
    raw = (PKG / _posix.LAUNCHER_TEMPLATE).read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    out = _posix.render_launcher(raw.decode("utf-8"), data_dir=_D, conf_file=_C, port=5123)
    assert "{{" not in out and out.split("\n")[1].startswith("# clewpath-launcher v2 (posix)")
    assert _posix._launcher_port(out) == 5123
    assert "DATA_DIR='/home/u x/.claude/session_manager'\n" in out
    root = _install_root(tmp_path, tpl=raw.decode("utf-8"))
    assert _posix.ensure_launcher(root, _D, _C, 5100) == "launcher: regenerated (posix v2)"
    assert _posix.ensure_launcher(root, _D, _C, 5101) == "launcher: ok (posix v2)"   # 폴백 포트로 뜬 날
    assert _posix._launcher_port((root / _posix.START_SCRIPT).read_text(encoding="utf-8")) == 5100
