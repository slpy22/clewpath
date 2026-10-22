"""설치 도우미(session_manager.hostctl) — docs/designs/posix-install.md 4.3: 경로·권한·설정·런처·발급·기동 확인.

install.sh 가 `.venv/bin/python -I -m session_manager.hostctl …` 로 부른다. 표준출력은 key=value(셸이 sed 로 읽는다),
사람에게 하는 말은 표준오류. 대부분 같은 프로세스에서 main() 을 부른다(빠르고 Windows 에서도 돈다) — 그래서 env 를
더럽히지 않는지(SM_* 가 남지 않는지)도 본다. 권한(0700·0600·0755)은 맥·리눅스에서만. 발급·기동 확인은 루프백의 가짜
CP·가짜 Host 를 상대로 한다(진짜 CP 에는 닿지 않는다 — conftest 가 SM_CP_URL 을 지운다).
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

import pytest

from session_manager import appconfig, cp_client, hostctl, systls
from session_manager.osdep import _posix

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "session_manager" / _posix.LAUNCHER_TEMPLATE
posix_only = pytest.mark.skipif(os.name != "posix", reason="권한 비트(0700·0600·0755)는 맥·리눅스에서만 뜻이 있다")
_ENV_KEYS = ("CLEWPATH_CONFIG", "SESSION_MANAGER_CLAUDE_HOME", "CLAUDE_CONFIG_DIR", "CLEWPATH_CLIENT_TOKEN",
             "CLEWPATH_AGENT_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """가짜 홈 — Path.home() 이 보는 HOME(맥·리눅스)·USERPROFILE(Windows) 을 바꾸고, 경로·토큰·프록시 env 를 지운다."""
    h = tmp_path / "홈 x"
    (h / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for k in _ENV_KEYS + tuple(k for k in os.environ if k.startswith("SM_")):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(cp_client, "_state", dict(cp_client._state))      # 발급이 바꾸는 배지 상태 — 다른 시험에 새지 않게
    monkeypatch.setattr(systls, "use_system_trust", lambda: None)          # ssl 전역 주입은 시험 프로세스에서 하지 않는다
    appconfig.load(refresh=True)
    yield h
    appconfig.load(refresh=True)


def run(capsys, *argv) -> tuple[int, dict, str]:
    before = {k: v for k, v in os.environ.items()}
    rc = hostctl.main(list(argv))
    out, err = capsys.readouterr()
    assert dict(os.environ) == before, "hostctl 이 env 를 바꾼 채로 끝났다"
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert all("=" in line for line in out.splitlines()), f"표준출력은 key=value 만: {out!r}"
    return rc, kv, err


# ---------------------------------------------------------------- paths — 파이썬 규칙(Host 가 볼 경로)

def test_paths_follow_python_rules(home, capsys, monkeypatch, tmp_path):
    rc, kv, _ = run(capsys, "paths")
    d = home / ".claude" / "session_manager"
    assert rc == 0 and kv == {"data_dir": str(d), "conf_file": str(d / "clewpath.toml"),
                              "claude_home": str(home / ".claude"), "projects_dir": str(home / ".claude" / "projects")}
    other = tmp_path / "옮긴 claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert run(capsys, "paths")[1]["data_dir"] == str(other / "session_manager")
    iso = tmp_path / "격리"
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(iso))                # 시험 격리 변수가 이긴다(config.claude_home)
    assert run(capsys, "paths")[1]["data_dir"] == str(iso / "session_manager")
    pinned = tmp_path / "conf" / "my.toml"
    monkeypatch.setenv("CLEWPATH_CONFIG", str(pinned))
    rc, kv, _ = run(capsys, "paths")
    assert kv["conf_file"] == str(pinned) and kv["data_dir"] == str(iso / "session_manager")


def test_paths_follow_config_dir_in_the_toml(home, capsys, tmp_path):
    """런처가 띄운 Host 는 설정 파일의 [claude] config_dir 를 env 로 펼친 뒤 데이터 폴더를 정한다(server.main →
    appconfig.apply_env) — paths 도 그 순서를 따른다. 설정 파일 자리는 그대로(런처가 CLEWPATH_CONFIG 로 고정한다)."""
    moved = tmp_path / "moved claude"
    conf = home / ".claude" / "session_manager" / "clewpath.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text(appconfig.render_template(cp_url="http://127.0.0.1:9/cp", claude_config_dir=str(moved)),
                    encoding="utf-8")
    rc, kv, _ = run(capsys, "paths")                                           # run() 이 env 가 그대로인지도 본다
    assert rc == 0 and kv["data_dir"] == str(moved / "session_manager") and kv["conf_file"] == str(conf)
    assert kv["claude_home"] == str(moved)


def test_paths_refuse_relative_and_control_chars(home, capsys, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", "상대/경로")
    rc, kv, err = run(capsys, "paths")
    assert rc == 1 and kv == {} and "[FAIL]" in err and "절대 경로" in err
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(home / "줄\n바꿈"))   # key=value 한 줄이 둘이 되면 안 된다
    rc, kv, err = run(capsys, "paths")
    assert rc == 1 and kv == {} and "제어문자" in err


def test_relative_config_dir_in_the_toml_is_refused_by_paths_only(home, capsys):
    """설정 파일의 config_dir 가 상대 경로면 Host 의 데이터 폴더가 cwd(설치 루트)에 따라 달라진다 — paths 는 거부하고
    (install.sh 가 멈춘다), write-config 는 설정 파일 자리만 보므로 그대로 '유지'라고 답한다."""
    conf = home / ".claude" / "session_manager" / "clewpath.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text('[server]\nport = 5100\n\n[claude]\nconfig_dir = "rel/claude"\n', encoding="utf-8")
    rc, kv, err = run(capsys, "paths")
    assert rc == 1 and kv == {} and "절대 경로" in err
    assert run(capsys, "write-config", "--port", "5100")[:2] == (0, {"config": "kept", "path": str(conf), "port": "5100"})


# ---------------------------------------------------------------- write-config — 없을 때만, 0600, 토큰은 env 로만

AWKWARD_DIR = '/data/c\\laude "x" 클루 it\'s'           # 역슬래시·따옴표·한글·공백 — TOML 이스케이프가 왕복해야 한다


def test_write_config_creates_once_and_never_overwrites(home, capsys, monkeypatch):
    conf = home / ".claude" / "session_manager" / "clewpath.toml"
    monkeypatch.setenv("CLEWPATH_CLIENT_TOKEN", "client-SECRET-1")
    monkeypatch.setenv("CLEWPATH_AGENT_TOKEN", "agent-SECRET-2")
    rc, kv, err = run(capsys, "write-config", "--port", "5123", "--cp-url", "https://cp.example/cp//",
                      "--relay-url", "wss://r.example/ws", "--app-url", "https://a.example/app",
                      "--claude-config-dir", AWKWARD_DIR)
    assert rc == 0 and kv == {"config": "created", "path": str(conf), "port": "5123"}
    raw = conf.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    d = tomllib.loads(raw.decode("utf-8"))
    assert d["server"] == {"host": "127.0.0.1", "port": 5123, "port_fallback": 10}
    assert d["cloud"] == {"cp_url": "https://cp.example/cp", "relay_url": "wss://r.example/ws",
                          "app_url": "https://a.example/app", "client_token": "client-SECRET-1",
                          "agent_token": "agent-SECRET-2"}
    assert d["claude"] == {"config_dir": AWKWARD_DIR}
    assert "SECRET" not in err, "토큰은 화면에 나오지 않는다"
    if os.name == "posix":
        assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    # 다시 — 다른 값·다른 토큰이어도 파일은 그대로(사용자가 고친 값을 지킨다), 실제 포트(설정 파일의 것)를 알려 준다
    monkeypatch.setenv("CLEWPATH_CLIENT_TOKEN", "other")
    rc, kv, err = run(capsys, "write-config", "--port", "5100", "--cp-url", "https://x.example/cp")
    assert rc == 0 and kv == {"config": "kept", "path": str(conf), "port": "5123"}
    assert conf.read_bytes() == raw and "5123" in err and "[WARN]" in err
    assert not list(conf.parent.glob(".clewpath.toml.*.tmp")), "임시 파일이 남지 않는다"


def test_write_config_without_tokens_comments_them_out(home, capsys):
    rc, kv, err = run(capsys, "write-config", "--port", "5100", "--cp-url", "https://cp.example/cp")
    d = tomllib.loads(Path(kv["path"]).read_text(encoding="utf-8"))
    assert rc == 0 and "client_token" not in d["cloud"] and "agent_token" not in d["cloud"]
    assert "config_dir" not in d.get("claude", {})
    assert "CLEWPATH_CLIENT_TOKEN" not in err, \
        "컨트롤플레인이 있으면 폰 QR 은 기기 전용 자격증명을 싣는다 — 공유 토큰 경고로 헷갈리게 하지 않는다(맥 실기 3b)"


def test_write_config_warns_about_the_phone_token_only_without_a_control_plane(home, capsys):
    rc, kv, err = run(capsys, "write-config", "--port", "5100", "--cp-url", "")
    assert rc == 0 and kv["config"] == "created" and "CLEWPATH_CLIENT_TOKEN" in err, "그때는 어디에 넣는지 알려 준다"


@pytest.mark.parametrize("body,want", [("[server]\nhost = \"127.0.0.1\"\n", "5100"),   # port 가 없으면 Host 기본값
                                       ("[server\nport = 7000\n", "5100"),            # 깨진 파일 — Host 는 기본값으로 뜬다
                                       ("[server]\nport = 6001\n", "6001")])
def test_write_config_reports_the_port_the_host_will_use(home, capsys, body, want):
    conf = home / ".claude" / "session_manager" / "clewpath.toml"
    conf.parent.mkdir(parents=True)
    conf.write_text(body, encoding="utf-8")
    rc, kv, _ = run(capsys, "write-config", "--port", "5100")
    assert rc == 0 and kv["config"] == "kept" and kv["port"] == want and conf.read_text(encoding="utf-8") == body


def test_write_config_kept_warns_when_given_values_differ(home, capsys, monkeypatch):
    """유지(kept)할 때 인자의 주소·env 토큰을 말없이 버렸다 — 다르면 설정 파일 값을 쓴다고 알린다(토큰 값은 찍지 않는다)."""
    monkeypatch.setenv("CLEWPATH_CLIENT_TOKEN", "tok-OLD-1")
    urls = ["--relay-url", "wss://r.example/ws", "--app-url", "https://a.example/app"]
    assert run(capsys, "write-config", "--cp-url", "https://cp.example/cp", *urls)[1]["config"] == "created"
    rc, kv, err = run(capsys, "write-config", "--cp-url", "https://cp.example/cp/", *urls)   # 끝 / 만 다른 것은 같은 값
    assert rc == 0 and kv["config"] == "kept" and "[WARN]" not in err, err
    monkeypatch.setenv("CLEWPATH_CLIENT_TOKEN", "tok-NEW-2")
    monkeypatch.setenv("CLEWPATH_AGENT_TOKEN", "agt-NEW-3")
    rc, kv, err = run(capsys, "write-config", "--cp-url", "http://127.0.0.1:9/cp", *urls)
    assert rc == 0 and kv["config"] == "kept"
    assert "cp_url" in err and "https://cp.example/cp" in err and "http://127.0.0.1:9/cp" in err
    assert "client_token" in err and "agent_token" in err and "tok-" not in err and "agt-" not in err


@pytest.mark.parametrize("argv", [["write-config", "--port", "0"], ["write-config", "--port", "70000"],
                                  ["write-config", "--port", "05100"], ["write-config", "--client-token", "x"],
                                  ["write-config", "--agent-token", "x"], ["render-launcher", "--root", "/x"],
                                  ["wait-ready", "--port", "5100", "--tries", "0"], ["nope"], []])
def test_usage_errors_exit_2(home, capsys, argv):
    """토큰 인자는 아예 없다(ps 로 보인다 — S17). 포트는 1-65535, 앞자리 0 없이."""
    assert hostctl.main(argv) == 2
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------- render-launcher — osdep._posix.write_launcher

def _tpl_root(tmp_path) -> Path:
    r = tmp_path / "설치 루트"
    (r / "session_manager").mkdir(parents=True)
    shutil.copyfile(TEMPLATE, r / "session_manager" / _posix.LAUNCHER_TEMPLATE)
    return r


def test_render_launcher_created_unchanged_updated(home, capsys, tmp_path):
    r = _tpl_root(tmp_path)
    data = str(PurePosixPath("/home/u x/.claude/session_manager"))
    conf = data + "/clewpath.toml"
    args = ["render-launcher", "--root", str(r), "--data-dir", data, "--conf-file", conf]
    assert run(capsys, *args, "--port", "5123")[:2] == (0, {"launcher": "created"})
    out = (r / _posix.START_SCRIPT).read_bytes()
    assert out.startswith(b"#!/bin/sh\n") and b"\nDEFAULT_PORT=5123\n" in out and b"\r" not in out
    assert out.decode("utf-8") == _posix.render_launcher(TEMPLATE.read_text(encoding="utf-8"), data_dir=data,
                                                         conf_file=conf, port=5123)
    if os.name == "posix":
        assert stat.S_IMODE((r / _posix.START_SCRIPT).stat().st_mode) == 0o755
    assert run(capsys, *args, "--port", "5123")[1] == {"launcher": "unchanged"}
    assert run(capsys, *args, "--port", "5200")[1] == {"launcher": "updated"}
    assert b"DEFAULT_PORT=5200\n" in (r / _posix.START_SCRIPT).read_bytes()


def test_render_launcher_failures(home, capsys, tmp_path):
    r = _tpl_root(tmp_path)
    rc, kv, err = run(capsys, "render-launcher", "--root", str(r), "--data-dir", "상대/data", "--conf-file", "/c.toml",
                      "--port", "5100")
    assert rc == 1 and kv == {} and "절대 경로" in err and not (r / _posix.START_SCRIPT).exists()
    (r / "session_manager" / _posix.LAUNCHER_TEMPLATE).unlink()
    rc, kv, err = run(capsys, "render-launcher", "--root", str(r), "--data-dir", "/d", "--conf-file", "/c.toml",
                      "--port", "5100")
    assert rc == 1 and "템플릿이 없습니다" in err


# ---------------------------------------------------------------- secure-data-dir — 0700, group·other 제거, 링크는 건너뜀

@posix_only
def test_secure_data_dir_narrows_modes_and_skips_links(home, capsys, tmp_path):
    data = home / ".claude" / "session_manager"
    (data / "updates" / "stage").mkdir(parents=True)
    files = {data / "devices.json": 0o644, data / "updates" / "apply.log": 0o666,
             data / "updates" / "stage" / "x.py": 0o664, data / "own.json": 0o600}
    for f, m in files.items():
        f.write_text("{}", encoding="utf-8")
        os.chmod(f, m)
    os.chmod(data, 0o755)
    os.chmod(data / "updates", 0o775)
    os.chmod(data / "updates" / "stage", 0o755)                 # umask 와 무관하게
    outside = tmp_path / "남의 파일"
    outside.write_text("x", encoding="utf-8")
    os.chmod(outside, 0o644)
    (data / "link").symlink_to(outside)
    rc, kv, _ = run(capsys, "secure-data-dir", "--data-dir", str(data))
    assert rc == 0 and kv == {"changed": "6"}                     # data·updates·stage(0755)·파일 셋(0600 은 그대로)
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    for p in (data / "updates", data / "updates" / "stage"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644, "링크 너머는 건드리지 않는다"
    assert run(capsys, "secure-data-dir", "--data-dir", str(data))[1] == {"changed": "0"}
    fresh = home / "없던 폴더" / "session_manager"
    assert run(capsys, "secure-data-dir", "--data-dir", str(fresh))[0] == 0
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o700


# ---------------------------------------------------------------- provision — 가짜 CP, fail-open

class _FakeCP:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.status = 200
        box = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                box.calls.append((self.path, body))
                out = {"connector_id": "c-1", "room_key": "ROOM-KEY-9", "credential_public_id": "pub-1",
                       "secret": "SECRET-S", "claim_secret": "SECRET-C"} if box.status == 200 else {"error": "x"}
                raw = json.dumps(out).encode()
                self.send_response(box.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/cp"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def cp():
    c = _FakeCP()
    yield c
    c.close()


def test_provision_creates_then_keeps(home, capsys, cp):
    rc, kv, err = run(capsys, "provision", "--cp-url", cp.url + "/")
    assert rc == 0 and kv == {"creds": "created", "connector": "c-1", "reason": "ok"}
    assert [p for p, _ in cp.calls] == ["/cp/provision/anonymous"]
    assert cp.calls[0][1]["display_name"] == appconfig.machine_name()
    f = home / ".claude" / "session_manager" / "cp_credentials.json"
    creds = json.loads(f.read_text(encoding="utf-8"))
    assert creds["cp_url"] == cp.url and creds["connector_id"] == "c-1" and creds["secret"] == "SECRET-S"
    assert {"room_key", "credential_public_id", "claim_secret", "created_at"} <= set(creds)
    if os.name == "posix":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert "SECRET" not in err and "ROOM-KEY" not in err, "비밀·room 은 화면에 나오지 않는다"
    raw = f.read_bytes()
    rc, kv, err = run(capsys, "provision", "--cp-url", cp.url)
    assert rc == 0 and kv["creds"] == "kept" and kv["connector"] == "c-1" and len(cp.calls) == 1
    assert f.read_bytes() == raw, "자격증명은 덮어쓰지 않는다(S43)"


@pytest.mark.parametrize("status,reason,said", [(429, "provision_rate_limited", "429"),
                                                (503, "provision_http_503", "fail-open")])
def test_provision_failures_are_warnings(home, capsys, cp, status, reason, said):
    cp.status = status
    rc, kv, err = run(capsys, "provision", "--cp-url", cp.url)
    assert rc == 0 and kv == {"creds": "failed", "connector": "", "reason": reason}
    assert "[WARN]" in err and said in err
    assert not (home / ".claude" / "session_manager" / "cp_credentials.json").exists()


def test_provision_unreachable_cp(home, capsys):
    with socket.socket() as s:                                  # 닫힌 포트 — 연결 거절
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    rc, kv, err = run(capsys, "provision", "--cp-url", f"http://127.0.0.1:{port}/cp")
    assert rc == 0 and kv["creds"] == "failed" and kv["reason"].startswith("provision_error:")


def test_provision_uses_the_cp_in_the_config_file(home, capsys, cp):
    """설정 파일이 있으면 Host 는 그 파일의 cp_url 로 붙는다 — 발급도 같은 규칙(env > 설정 파일)이어야 한다. 전엔 인자
    (install.sh 의 기본값 = 운영 CP)로 발급해, 시험·자체 CP 설정을 재설치하면 운영 CP 에 익명 커넥터가 등록되고 '다른
    컨트롤플레인 것' 거짓 경고가 떴다. 인자는 설정 파일이 없을 때만 쓴다."""
    assert run(capsys, "write-config", "--cp-url", cp.url)[1]["config"] == "created"
    wrong = f"http://127.0.0.1:{_closed_port()}/cp"
    rc, kv, err = run(capsys, "provision", "--cp-url", wrong)
    assert rc == 0 and kv["creds"] == "created" and [p for p, _ in cp.calls] == ["/cp/provision/anonymous"], err
    f = home / ".claude" / "session_manager" / "cp_credentials.json"
    assert json.loads(f.read_text(encoding="utf-8"))["cp_url"] == cp.url
    rc, kv, err = run(capsys, "provision", "--cp-url", wrong)
    assert kv["creds"] == "kept" and "다른 컨트롤플레인" not in err and len(cp.calls) == 1


# ---------------------------------------------------------------- wait-ready — 가짜 Host

class _FakeHost:
    def __init__(self, conn: dict, diag: dict):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"/api/owner/connection": conn, "/api/owner/diagnostics": diag}.get(self.path)
                raw = json.dumps(body or {"error": "nope"}).encode()
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_wait_ready_follows_runtime_port(home, capsys, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    h = _FakeHost({"mode": "fallback"}, {"ok": True, "session_count": 3, "claude_home": str(home / ".claude"),
                                         "claude_exe_found": False})
    try:
        (data / "runtime.json").write_text(json.dumps({"port": h.port, "pid": 1}), encoding="utf-8")
        asked = _closed_port()
        rc, kv, err = run(capsys, "wait-ready", "--port", str(asked), "--tries", "3", "--data-dir", str(data))
    finally:
        h.close()
    assert rc == 0 and kv == {"state": "up", "port": str(h.port), "mode": "fallback", "session_count": "3",
                              "claude_exe_found": "0"}
    assert f"{asked} 가 사용 중이라 {h.port} 번 포트로 열렸습니다" in err
    assert "세션 인식됨: 3 개" in err and "claude 실행파일을 못 찾았습니다" in err and "공유토큰" in err


def test_wait_ready_does_not_take_another_users_host(home, capsys, tmp_path):
    """맥 두 계정 — 그 포트에서 답하는 다른 사용자의 ClewPath 를 '이 설치본이 떴다'로 받아들였다. 진단의 클로드 폴더가 이
    설치본의 것과 다르면 경고하고 down 으로 둔다."""
    data = tmp_path / "data"
    data.mkdir()
    other = str(PurePosixPath("/Users/someone-else/.claude"))
    h = _FakeHost({"mode": "cp"}, {"ok": True, "session_count": 9, "claude_home": other, "claude_exe_found": True})
    try:
        rc, kv, err = run(capsys, "wait-ready", "--port", str(h.port), "--tries", "2", "--data-dir", str(data))
    finally:
        h.close()
    assert rc == 0 and kv["state"] == "down" and kv["port"] == str(h.port), err
    assert "다른 사용자의 ClewPath" in err and other in err and "서비스 기동 확인" not in err


def test_wait_ready_down_shows_the_right_log(home, capsys, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    port = str(_closed_port())
    rc, kv, err = run(capsys, "wait-ready", "--port", port, "--tries", "1", "--data-dir", str(data))
    assert rc == 0 and kv["state"] == "down" and kv["port"] == port and "런처가 Host 를 띄우지 못했습니다" in err
    (data / "host.log").write_text("".join(f"log {i}\n" for i in range(20)), encoding="utf-8")
    (data / "launcher.log").write_text("[t] fail: 기동 직후 종료(pid 7, rc=1)\n", encoding="utf-8")
    err = run(capsys, "wait-ready", "--port", port, "--tries", "1", "--data-dir", str(data))[2]
    assert "    | log 19" in err and "    | log 12" in err and "log 11" not in err, "host.log 마지막 8줄"
    assert "    > [t] fail: 기동 직후 종료" in err
    (data / "host.err.log").write_text("Traceback\nImportError: x\n", encoding="utf-8")
    err = run(capsys, "wait-ready", "--port", port, "--tries", "1", "--data-dir", str(data))[2]
    assert "    ! ImportError: x" in err and "| log 19" not in err, "오류 로그가 비어 있지 않으면 그쪽"


# ---------------------------------------------------------------- CLI 모양(자식 프로세스)

def test_cli_keeps_stdout_machine_readable(tmp_path):
    """install.sh 가 읽는 모양 그대로 — 자식 프로세스, 표준출력은 key=value 만, 사람 말은 표준오류, 토큰은 env 로."""
    h = tmp_path / "home"
    (h / ".claude" / "projects").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS and not k.startswith("SM_")}
    env.update(HOME=str(h), USERPROFILE=str(h), CLEWPATH_CLIENT_TOKEN="tok-SECRET", PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-m", "session_manager.hostctl"]       # cwd=저장소 — 이 작업 트리의 패키지를 쓴다
    r = subprocess.run([*cmd, "write-config", "--port", "5100", "--cp-url", "https://cp.example/cp"], cwd=ROOT,
                       env=env, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert r.stdout.decode("utf-8").splitlines()[0] == "config=created"
    assert b"[OK]" in r.stderr and b"tok-SECRET" not in r.stdout + r.stderr
    conf = h / ".claude" / "session_manager" / "clewpath.toml"
    assert tomllib.loads(conf.read_text(encoding="utf-8"))["cloud"]["client_token"] == "tok-SECRET"
    r = subprocess.run([*cmd, "paths"], cwd=ROOT, env=env, capture_output=True, timeout=60)
    assert r.returncode == 0 and r.stderr == b""
    assert [ln.split("=", 1)[0] for ln in r.stdout.decode("utf-8").splitlines()] == [
        "data_dir", "conf_file", "claude_home", "projects_dir"]
    r = subprocess.run([*cmd, "nope"], cwd=ROOT, env=env, capture_output=True, timeout=60)
    assert r.returncode == 2 and r.stdout == b""


# ---------------------------------------------------------------- render-launchd — [mac] 자동 시작 등록 파일(plistlib)

MAC_ROOT = "/Users/u/Library/Application Support/ClewPath/app"
MAC_DATA = "/Users/u/.claude/session_manager"


def test_launchd_plist_contents():
    """com.clewpath.host 의 내용 — 로그인할 때(RunAtLoad)·5분마다(StartInterval) foreground, 0 이 아닌 종료·신호로 죽었을 때만
    다시(KeepAlive{SuccessfulExit:false}, 30초 간격), Host 가 끝나도 브로커·러너를 죽이지 않게(AbandonProcessGroup), QoS
    제한 없이(Interactive). env 는 PATH·LANG 만. plistlib 로 쓰고 다시 읽어도 같다(launchd 가 읽는 키·형식 그대로)."""
    d = hostctl.launchd_plist(MAC_ROOT, MAC_DATA, path_env="/opt/homebrew/bin:/Users/u/.local/bin:/usr/bin",
                              lang="ko_KR.UTF-8", umask=0o022)
    assert d == {
        "Label": "com.clewpath.host",
        "ProgramArguments": ["/bin/sh", "-c", '[ -f "$1" ] || exit 0; exec /bin/sh "$1" foreground', "clewpath-launchd",
                             f"{MAC_ROOT}/start-connector.sh"],
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "StartInterval": 300, "ThrottleInterval": 30,
        "AbandonProcessGroup": True, "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/Users/u/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "LANG": "ko_KR.UTF-8"},
        "StandardOutPath": f"{MAC_DATA}/launchd.log", "StandardErrorPath": f"{MAC_DATA}/launchd.log", "Umask": 0o022}
    assert plistlib.loads(plistlib.dumps(d, fmt=plistlib.FMT_XML)) == d
    assert hostctl.LAUNCHD_PLIST == "Library/LaunchAgents/com.clewpath.host.plist"
    trimmed = hostctl.launchd_plist(MAC_ROOT + "/", MAC_DATA + "/", path_env=None, lang=None, umask=None)
    assert "Umask" not in trimmed
    assert trimmed["ProgramArguments"][-1] == f"{MAC_ROOT}/start-connector.sh", "끝의 / 는 걷어 낸다"
    assert trimmed["StandardOutPath"] == f"{MAC_DATA}/launchd.log"


@pytest.mark.parametrize("given,want", [
    (None, "/usr/bin:/bin:/usr/sbin:/sbin"),
    ("", "/usr/bin:/bin:/usr/sbin:/sbin"),
    ("rel/bin:.::~/bin:/opt/homebrew/bin/:/opt/homebrew/bin:/usr/bin:/a\tb:/Users/u/.local/bin",
     "/opt/homebrew/bin:/usr/bin:/Users/u/.local/bin:/bin:/usr/sbin:/sbin"),
    ("/sbin:/usr/local/bin:/bin", "/sbin:/usr/local/bin:/bin:/usr/bin:/usr/sbin"),
    ("C:\\Windows;C:\\bin", "/usr/bin:/bin:/usr/sbin:/sbin"),
])
def test_launchd_path_keeps_absolute_unique_pieces(given, want):
    """설치 때 PATH 에서 절대 경로 조각만(상대·빈 조각·~·제어문자는 버린다), 겹치지 않게(끝의 / 는 같은 것으로), 그 뒤에
    launchd 기본 PATH 의 빠진 것만 — launchd 의 기본 PATH 로는 ~/.local/bin·/opt/homebrew/bin 의 claude 를 못 찾는다."""
    assert hostctl.launchd_path(given) == want


@pytest.mark.parametrize("given,want", [("ko_KR.UTF-8", "ko_KR.UTF-8"), ("en_US.utf8", "en_US.utf8"),
                                        ("C.UTF-8", "C.UTF-8"), (None, "en_US.UTF-8"), ("", "en_US.UTF-8"),
                                        ("C", "en_US.UTF-8"), ("ko_KR.eucKR", "en_US.UTF-8"), ("UTF-8", "en_US.UTF-8"),
                                        ("ko_KR.UTF-8\nX=1", "en_US.UTF-8"), ("ko KR.UTF-8", "en_US.UTF-8")])
def test_launchd_lang_is_utf8(given, want):
    """launchd 는 LANG 을 주지 않는다 — 설치 때 값이 UTF-8 로캘이면 그대로, 아니면 en_US.UTF-8(claude·셸의 한글)."""
    assert hostctl.launchd_lang(given) == want


def test_launchd_plist_escapes_awkward_paths():
    """경로의 & < > 는 plistlib 이 XML 로 이스케이프한다(셸로 XML 을 만들면 여기서 깨진다) — 다시 읽으면 원래 글자."""
    root = "/Users/u/A & B <x> \"q\" it's/app"
    data = "/Users/u/d & e/session_manager"
    d = hostctl.launchd_plist(root, data, path_env="/opt/a&b/bin:/usr/bin", lang=None, umask=None)
    raw = plistlib.dumps(d, fmt=plistlib.FMT_XML)
    assert b"A &amp; B &lt;x&gt;" in raw and b"A & B" not in raw and b"<x>" not in raw
    back = plistlib.loads(raw)
    assert back == d and back["ProgramArguments"][-1] == root + "/start-connector.sh"
    assert back["StandardOutPath"] == data + "/launchd.log"
    assert back["EnvironmentVariables"]["PATH"].startswith("/opt/a&b/bin:")


def _mac_root(tmp_path) -> Path:
    """멱등 런처(v2)가 있는 설치 루트 — 공백·& 가 든 경로."""
    root = tmp_path / "Application Support" / "Clew & Path" / "app"
    root.mkdir(parents=True)
    (root / "start-connector.sh").write_bytes(b"#!/bin/sh\n# clewpath-launcher v2 (posix) \xe2\x80\x94 test\n")
    return root


def _launchd_args(root: Path, data: Path, plist: Path) -> list[str]:
    return ["render-launchd", "--root", str(root), "--data-dir", str(data), "--plist", str(plist)]


@posix_only
def test_render_launchd_writes_0644_atomically_once(home, capsys, monkeypatch, tmp_path):
    root, data = _mac_root(tmp_path), home / ".claude" / "session_manager"
    plist = home / "Library" / "LaunchAgents" / "com.clewpath.host.plist"
    monkeypatch.setenv("PATH", f"rel:/opt/homebrew/bin:{home}/.local/bin:/usr/bin")
    monkeypatch.setenv("LANG", "ko_KR.UTF-8")
    old_umask = os.umask(0o027)                         # 설치 때 셸의 umask 를 그대로(S38)
    try:
        rc, kv, err = run(capsys, *_launchd_args(root, data, plist))
        assert rc == 0 and kv == {"launchd": "created", "plist": str(plist), "label": "com.clewpath.host"}, err
        assert stat.S_IMODE(plist.stat().st_mode) == 0o644, "launchd 는 남이 쓸 수 있는 plist 를 거부한다"
        assert stat.S_IMODE(plist.parent.stat().st_mode) == 0o755
        d = plistlib.loads(plist.read_bytes())
        assert d["ProgramArguments"][-1] == f"{root}/start-connector.sh" and "WorkingDirectory" not in d
        assert d["EnvironmentVariables"] == {
            "PATH": f"/opt/homebrew/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "ko_KR.UTF-8"}
        assert d["StandardOutPath"] == d["StandardErrorPath"] == str(data / "launchd.log") and d["Umask"] == 0o027
        assert sorted(p.name for p in plist.parent.iterdir()) == [plist.name], "임시 파일이 남지 않는다"
        raw = plist.read_bytes()
        rc, kv, _ = run(capsys, *_launchd_args(root, data, plist))
        assert rc == 0 and kv["launchd"] == "unchanged" and plist.read_bytes() == raw
        # 바꿔 쓰다 실패하면(교체 직전) 옛 파일 그대로, 임시 파일도 없다(원자적)
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", boom)
        try:
            rc, kv, err = run(capsys, *_launchd_args(root, home / "elsewhere", plist))
        finally:
            monkeypatch.setattr(os, "replace", real_replace)
        assert rc == 1 and kv == {} and "[FAIL]" in err and plist.read_bytes() == raw
        assert sorted(p.name for p in plist.parent.iterdir()) == [plist.name]
        rc, kv, _ = run(capsys, *_launchd_args(root, home / "elsewhere", plist))
        assert rc == 0 and kv["launchd"] == "updated"
        assert plistlib.loads(plist.read_bytes())["StandardOutPath"] == str(home / "elsewhere" / "launchd.log")
    finally:
        os.umask(old_umask)


@posix_only
def test_render_launchd_replaces_another_installs_plist_with_a_warning(home, capsys, tmp_path):
    """같은 사용자에 다른 설치본의 등록이 있으면 덮어쓴다(설치본은 사용자당 하나) — 말없이 덮지 않고 알린다."""
    root, data = _mac_root(tmp_path), home / ".claude" / "session_manager"
    plist = home / "Library" / "LaunchAgents" / "com.clewpath.host.plist"
    plist.parent.mkdir(parents=True)
    other = hostctl.launchd_plist("/Users/u/other/app", MAC_DATA, path_env=None, lang=None, umask=None)
    plist.write_bytes(plistlib.dumps(other))
    rc, kv, err = run(capsys, *_launchd_args(root, data, plist))
    assert rc == 0 and kv["launchd"] == "updated" and "다른 설치본" in err and "/Users/u/other/app/start-connector.sh" in err
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][-1] == f"{root}/start-connector.sh"
    plist.write_text("not a plist", encoding="utf-8")
    rc, kv, err = run(capsys, *_launchd_args(root, data, plist))
    assert rc == 0 and kv["launchd"] == "updated" and "읽을 수 없는" in err
    rc, kv, err = run(capsys, *_launchd_args(root, data, plist))                 # 이제 제 것 — 말없이 그대로
    assert rc == 0 and kv["launchd"] == "unchanged" and "[WARN]" not in err
    assert hostctl.plist_launcher(plist.read_bytes()) == f"{root}/start-connector.sh"
    assert hostctl.plist_launcher(b"junk") is None and hostctl.plist_launcher(plistlib.dumps({"Label": "x"})) == ""


@posix_only
@pytest.mark.parametrize("launcher", [None, b"#!/bin/sh\n# clewpath-launcher v1\n", b"#!/bin/sh\n"])
def test_render_launchd_needs_the_idempotent_launcher(home, capsys, tmp_path, launcher):
    """반복 실행 주체(launchd)는 멱등 런처(v2)를 확인한 뒤에만 둔다(S22) — 런처가 없거나 v2 가 아니면 등록 파일을 쓰지 않는다."""
    root = tmp_path / "app"
    root.mkdir()
    if launcher is not None:
        (root / "start-connector.sh").write_bytes(launcher)
    plist = home / "Library" / "LaunchAgents" / "com.clewpath.host.plist"
    rc, kv, err = run(capsys, *_launchd_args(root, home / ".claude" / "session_manager", plist))
    assert rc == 1 and kv == {} and "[FAIL]" in err and "런처" in err
    assert not plist.exists() and not plist.parent.exists()


@pytest.mark.parametrize("bad", ["root", "data", "plist", "ctrl"])
def test_render_launchd_refuses_odd_paths(home, capsys, tmp_path, bad):
    """plist 에 박는 값은 맥 절대 경로(/…)만, 제어문자 없이 — 상대 경로는 launchd 가 어디서 부를지 모른다."""
    args = {"root": "/Users/u/app", "data": "/Users/u/.claude/session_manager", "plist": str(tmp_path / "x.plist")}
    key = "root" if bad == "ctrl" else bad
    args[key] = {"root": "rel/app", "data": "rel/data", "plist": "rel.plist", "ctrl": "/Users/u/a\npp"}[bad]
    rc, kv, err = run(capsys, "render-launchd", "--root", args["root"], "--data-dir", args["data"],
                      "--plist", args["plist"])
    assert rc == 1 and kv == {} and "[FAIL]" in err
    assert not (tmp_path / "x.plist").exists()


@posix_only
@pytest.mark.parametrize("present,rc", [(True, 7), (False, 0)])
def test_launchd_wrapper_runs_the_launcher_or_quietly_ends(tmp_path, present, rc):
    """launchd 가 부르는 명령 — 런처가 있으면 foreground 로(exec), 설치 폴더를 손으로 지워 런처가 없으면 0 으로 끝난다(0 이
    아니면 KeepAlive 가 30초마다 다시 부른다 — 그대로 /bin/sh <런처> 였으면 sh 의 127 로 쉼 없이 돈다)."""
    launcher = tmp_path / "app x" / "start-connector.sh"
    if present:
        launcher.parent.mkdir()
        launcher.write_text('printf "%s\\n" "$0" "$@" >"$(dirname "$0")/argv"\nexit 7\n', encoding="utf-8")
    argv = hostctl.launchd_plist(str(launcher.parent), "/d", path_env=None, lang=None, umask=None)["ProgramArguments"]
    r = subprocess.run(argv, capture_output=True, timeout=30)
    assert r.returncode == rc, r.stderr.decode("utf-8", "replace")
    if present:
        assert (launcher.parent / "argv").read_text(encoding="utf-8").splitlines() == [str(launcher), "foreground"]
