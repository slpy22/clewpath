"""설정 파일(clewpath.toml) + 포트 폴백 회귀.

핵심 성질 세 가지를 붙잡는다:
  1) 우선순위가 env > 파일 > 기본값 이어야 한다. 뒤집히면 개발용 start-public.ps1 과
     도커(둘 다 env 로 돈다)가 사용자 설정 파일에 덮여 조용히 다른 값으로 뜬다.
  2) 설정 파일이 깨져도 서비스는 떠야 한다. 백그라운드 프로세스라 파싱 에러로 죽으면
     사용자에게 아무 단서도 남지 않는다.
  3) 이미 ClewPath 가 떠 있는 포트에는 폴백하면 안 된다. 두 인스턴스가 같은 세션
     폴더를 동시에 만지게 된다.
"""
from __future__ import annotations

import socket

import pytest

from session_manager import appconfig, server


@pytest.fixture
def conf(tmp_path, monkeypatch):
    """임시 설정 파일을 가리키게 하고 캐시를 비운다."""
    p = tmp_path / "clewpath.toml"
    monkeypatch.setenv("CLEWPATH_CONFIG", str(p))
    appconfig.load(refresh=True)
    yield p
    appconfig.load(refresh=True)


def _write(p, text):
    p.write_text(text, encoding="utf-8")
    appconfig.load(refresh=True)


# ---- 우선순위 ----

def test_file_value_used_when_no_env(conf, monkeypatch):
    monkeypatch.delenv("SM_PORT", raising=False)
    _write(conf, '[server]\nport = 6200\n')
    assert appconfig.get_int("server", "port", 5100) == 6200


def test_env_beats_file(conf, monkeypatch):
    _write(conf, '[server]\nport = 6200\n')
    monkeypatch.setenv("SM_PORT", "7300")
    assert appconfig.get_int("server", "port", 5100) == 7300


def test_default_when_neither(conf, monkeypatch):
    monkeypatch.delenv("SM_PORT", raising=False)
    _write(conf, "")
    assert appconfig.get_int("server", "port", 5100) == 5100


def test_apply_env_does_not_overwrite_existing(conf, monkeypatch):
    _write(conf, '[server]\nhost = "0.0.0.0"\n[cloud]\ncp_url = "http://from-file"\n')
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.delenv("SM_CP_URL", raising=False)
    filled = appconfig.apply_env()
    import os
    assert os.environ["SM_HOST"] == "127.0.0.1"        # env 그대로
    assert os.environ["SM_CP_URL"] == "http://from-file"
    assert "SM_HOST" not in filled and "SM_CP_URL" in filled


def test_broken_toml_does_not_raise(conf, monkeypatch, capsys):
    monkeypatch.delenv("SM_PORT", raising=False)
    _write(conf, '[server\nport = "이건 toml 이 아님\n')
    assert appconfig.get_int("server", "port", 5100) == 5100     # 기본값으로 산다
    assert "설정 파일을 읽지 못했습니다" in capsys.readouterr().err


def test_non_integer_port_falls_back(conf, monkeypatch, capsys):
    monkeypatch.delenv("SM_PORT", raising=False)
    _write(conf, '[server]\nport = "오천백"\n')
    assert appconfig.get_int("server", "port", 5100) == 5100
    assert "정수가 아닙니다" in capsys.readouterr().err


def test_template_is_valid_toml():
    import tomllib
    d = tomllib.loads(appconfig.render_template(port=5100))
    assert d["server"]["port"] == 5100
    assert d["server"]["host"] == "127.0.0.1"
    assert d["cloud"]["cp_url"].startswith("http")
    assert "client_token" not in d["cloud"] and "agent_token" not in d["cloud"]   # 없으면 주석 줄
    assert "config_dir" not in d.get("claude", {})


def test_template_escapes_awkward_values():
    """맥·리눅스 설치(hostctl write-config)는 이 함수로 설정을 만든다 — 따옴표·역슬래시·한글·공백·제어문자가 든
    값에서도 파일이 깨지지 않고 같은 값으로 읽혀야 한다(install.ps1 처럼 문자열을 그냥 꽂으면 TOML 이 깨진다)."""
    import tomllib
    vals = dict(cp_url='https://cp.example/c"p\\x', relay_url="wss://r.example/ws?a=1&b=2",
                app_url="https://앱.example/relay app", client_token='tok"en\\ 끝\x7f\t',
                agent_token="rly-agt-x'y", claude_config_dir="/Users/김 철수/my \"claude\" data")
    d = tomllib.loads(appconfig.render_template(port=5123, **vals))
    assert d["server"]["port"] == 5123
    assert {k: d["cloud"][k] for k in ("cp_url", "relay_url", "app_url", "client_token", "agent_token")} == \
        {k: vals[k] for k in ("cp_url", "relay_url", "app_url", "client_token", "agent_token")}
    assert d["claude"]["config_dir"] == vals["claude_config_dir"]
    win = tomllib.loads(appconfig.render_template(claude_config_dir="D:\\클로드\\data"))
    assert win["claude"]["config_dir"] == "D:\\클로드\\data"
    for bad in (0, 65536):
        with pytest.raises(ValueError):
            appconfig.render_template(port=bad)


def _install_ps1_toml(port, cp, relay, app, client=None, agent=None, cfg=None) -> str:
    """install.ps1 의 인라인 toml(here-string)을 같은 입력으로 펼친다 — $clientLine 등 줄 변수는 install.ps1 이 고르는 키로."""
    import re
    from pathlib import Path
    txt = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r'\$toml = @"\n(.*?)\n"@', txt, re.S)
    assert m, "install.ps1 에서 $toml here-string 을 못 찾음"
    keys = dict(re.findall(r'\$(clientLine|agentLine|claudeLine)\s*=\s*if \([^)]*\)\s*\{\s*"(\w+) = ', txt))
    assert set(keys) == {"clientLine", "agentLine", "claudeLine"}, keys
    given = {"clientLine": client, "agentLine": agent, "claudeLine": cfg}
    vals = {"Port": str(port), "CpUrl": cp, "RelayUrl": relay, "AppUrl": app,
            **{k: (f'{keys[k]} = "{v}"' if v else "") for k, v in given.items()}}
    return re.sub(r"\$(\w+)", lambda x: vals[x.group(1)], m.group(1))


@pytest.mark.parametrize("extra", [{}, {"client": "rly-cli-1", "agent": "rly-agt-2", "cfg": "/data/claude"}])
def test_template_matches_install_ps1(extra):
    """맥·리눅스 설정(render_template)과 Windows 설정(install.ps1 인라인)은 섹션·키·값이 같아야 한다 — 한쪽만 고치면
    OS 마다 다르게 도는 Host 가 생긴다. 템플릿에만 있는 섹션(hooks·push·e2ee)은 코드 기본값과 같은 값이라 허용."""
    import tomllib
    args = dict(port=5123, cp="https://cp.example/cp", relay="wss://r.example/ws", app="https://r.example/app")
    ps = tomllib.loads(_install_ps1_toml(**args, **extra))
    mine = tomllib.loads(appconfig.render_template(
        port=args["port"], cp_url=args["cp"], relay_url=args["relay"], app_url=args["app"],
        client_token=extra.get("client"), agent_token=extra.get("agent"), claude_config_dir=extra.get("cfg")))
    assert set(ps) <= set(mine), f"install.ps1 에만 있는 섹션: {set(ps) - set(mine)}"
    for section, body in ps.items():
        assert mine[section] == body, section


@pytest.mark.skipif(not __import__("shutil").which("powershell"), reason="Windows powershell.exe 5.1 필요(맥·리눅스는 건너뜀)")
def test_install_ps1_data_dir_matches_host(tmp_path, monkeypatch):
    """⑤ install.ps1 이 설정·자격증명을 쓰는 데이터 폴더 = Host 의 config.data_dir() — 같은 순서
    SESSION_MANAGER_CLAUDE_HOME > CLAUDE_CONFIG_DIR > ~\\.claude. 고치기 전엔 CLAUDE_CONFIG_DIR 을 빠뜨려, 클로드 폴더를 옮겨 쓰는 PC 에선
    설치가 만든 clewpath.toml·cp_credentials.json 을 Host 가 못 찾았다. install.ps1 의 [5/6] 계산 줄을 그대로 5.1 로 돌린다."""
    import base64
    import subprocess
    from pathlib import Path

    from session_manager import config
    src = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8-sig")
    i = src.index('Say "[5/6] ')
    i = src.index("\n", i) + 1
    snippet = src[i:src.index("$credFile", i)]
    assert "$dataDir" in snippet, snippet
    sm, cfg = tmp_path / "격리 home", tmp_path / "옮긴 claude"
    cases = [(None, None), (None, cfg), (sm, cfg), (sm, None)]

    def ps_set(name, v):
        return f"$env:{name} = '{str(v).replace(chr(39), chr(39) * 2)}'" if v else f"Remove-Item Env:\\{name} -ErrorAction SilentlyContinue"
    out = tmp_path / "data-dirs.txt"
    body = "\n".join(f"{ps_set('SESSION_MANAGER_CLAUDE_HOME', a)}\n{ps_set('CLAUDE_CONFIG_DIR', b)}\n$r += (& $calc)" for a, b in cases)
    cmd = (f"function Say {{}}\n$calc = {{\n{snippet}\n$dataDir\n}}\n$r = @()\n{body}\n"
           f"[IO.File]::WriteAllLines('{str(out).replace(chr(39), chr(39) * 2)}', [string[]]$r, (New-Object System.Text.UTF8Encoding($false)))")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")], capture_output=True, timeout=120)
    assert r.returncode == 0 and out.is_file(), r.stderr.decode("utf-8", "replace")
    got = out.read_text(encoding="utf-8").splitlines()
    want = []
    for a, b in cases:
        for name, v in (("SESSION_MANAGER_CLAUDE_HOME", a), ("CLAUDE_CONFIG_DIR", b)):
            monkeypatch.setenv(name, str(v)) if v else monkeypatch.delenv(name, raising=False)
        want.append(str(config.data_dir()))
    assert got == want
    assert got[1] == str(cfg / "session_manager")                 # CLAUDE_CONFIG_DIR 만 있을 때(고치기 전엔 ~\.claude\session_manager)


# ---- 포트 폴백 ----

def test_resolve_returns_requested_port_when_free():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    # 위 소켓이 닫힌 뒤라 그 포트는 (거의 항상) 비어 있다
    assert server.resolve_port("127.0.0.1", free, 10) == free


def test_resolve_skips_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        got = server.resolve_port("127.0.0.1", port, 10)
        assert got is not None and got != port
        assert port < got <= port + 10


def test_no_fallback_when_disabled():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert server.resolve_port("127.0.0.1", port, 0) is None


def test_does_not_fallback_past_existing_clewpath(monkeypatch):
    """이미 ClewPath 가 잡은 포트면 옆으로 비키지 말고 멈춰야 한다."""
    monkeypatch.setattr(server, "_clewpath_already_there", lambda h, p: True)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert server.resolve_port("127.0.0.1", port, 10) is None


def test_gives_up_when_whole_range_busy(monkeypatch):
    monkeypatch.setattr(server, "_port_free", lambda h, p: False)
    monkeypatch.setattr(server, "_clewpath_already_there", lambda h, p: False)
    assert server.resolve_port("127.0.0.1", 5100, 5) is None


def test_port_free_is_decided_by_osdep(monkeypatch):
    """판정은 OS 별(맥·리눅스는 TIME_WAIT 를 꿰뚫어 본다, Windows 는 0.28 그대로). server._port_free 는 얇은 이름."""
    from session_manager import osdep
    seen = []
    monkeypatch.setattr(osdep, "port_free", lambda h, p: seen.append((h, p)) or False)
    assert server._port_free("127.0.0.1", 5100) is False and seen == [("127.0.0.1", 5100)]


def test_clewpath_probe_ignores_http_proxy(monkeypatch):
    """S28: 같은 포트에 ClewPath 가 있으면 멈춘다 — 그 확인 요청이 http_proxy 를 타면(맥·리눅스 urllib 기본) '없다'로
    오판해 두 번째 인스턴스가 옆 포트에 뜬다. 루프백 확인은 프록시를 쓰지 않는다."""
    import http.server
    import json
    import threading
    import urllib.request

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"claude_home": "/x"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        for k in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")      # 닫힌 포트 — 프록시를 타면 실패한다
        with pytest.raises(OSError):                                 # 시험 전제: 기본 urlopen 은 정말 프록시를 탄다
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/owner/diagnostics", timeout=3)
        assert server._clewpath_already_there("127.0.0.1", port) is True
        assert server._clewpath_already_there("0.0.0.0", port) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_apply_env_remembers_what_it_filled(conf, monkeypatch):
    """apply_env 가 파일로 채운 이름만 기억한다(filled_env) — 사용자(런처)가 원래 준 env 와 구분해 업데이트 러너에 물려주지 않게."""
    _write(conf, '[server]\nhost = "0.0.0.0"\nport = 5333\n[update]\nmanifest_url = "https://m.test/x"\n')
    monkeypatch.setattr(appconfig, "_FILLED", set())
    for k in ("SM_PORT", "SM_UPDATE_MANIFEST_URL"):      # 시험이 끝나면 지운다(apply_env 가 os.environ 에 직접 쓴다)
        monkeypatch.setenv(k, "x")
        monkeypatch.delenv(k)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    filled = appconfig.apply_env()
    assert sorted(filled) == ["SM_PORT", "SM_UPDATE_MANIFEST_URL"]
    assert appconfig.filled_env() == {"SM_PORT", "SM_UPDATE_MANIFEST_URL"}
    assert appconfig.apply_env() == [] and appconfig.filled_env() == {"SM_PORT", "SM_UPDATE_MANIFEST_URL"}  # 두 번째 호출에도 유지
