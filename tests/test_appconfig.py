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
