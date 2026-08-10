"""사용자 설정 파일(clewpath.toml) — 환경변수와 한 몸으로 동작한다.

왜 필요한가
-----------
설치본의 포트·주소는 지금까지 install.ps1 이 생성한 start-connector.ps1 안의
`$env:SM_PORT = "5100"` 같은 줄에 박혀 있었다. 문제가 둘이었다:
  1) 사용자가 포트를 바꾸려면 자동 생성 스크립트를 고쳐야 하는데, 재설치하면 날아간다.
  2) 자동 업데이트를 붙이면 그 파일을 덮어쓰게 되므로 설정이 매번 초기화된다.
그래서 설정을 **데이터 폴더(~/.claude/session_manager/)** 로 옮긴다. 여긴 기기 목록·
자격증명이 사는 곳이고, 재설치·업데이트가 건드리지 않는다("연결 주권 모델" 과도 일치).

우선순위
--------
    환경변수  >  clewpath.toml  >  기본값

환경변수를 위에 두는 이유: 개발용 start-public.ps1, 테스트, 도커가 전부 env 로 돌고
있고 그게 계속 동작해야 한다. "일회성으로 덮어쓰기"의 자연스러운 자리이기도 하다.

파일이 없으면 그냥 기본값으로 돈다 — 설정 파일은 항상 선택이다.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

# [섹션] 이름 -> 그 안의 키가 매핑되는 환경변수.
# 여기 없는 env 는 설정 파일로 노출하지 않는다(운영자 전용 값까지 열어줄 이유가 없다).
_MAP: dict[str, dict[str, str]] = {
    "server": {
        "host":       "SM_HOST",
        "port":       "SM_PORT",
        "base_path":  "SM_BASE_PATH",
        "password":   "SM_PASSWORD",
        "api_token":  "SM_API_TOKEN",
    },
    "cloud": {
        "cp_url":       "SM_CP_URL",
        "relay_url":    "SM_RELAY_URL",
        "app_url":      "SM_APP_URL",
        "agent_token":  "SM_RELAY_AGENT_TOKEN",
        "client_token": "SM_RELAY_CLIENT_TOKEN",
        "room":         "SM_RELAY_ROOM",
    },
    "claude": {
        "config_dir": "CLAUDE_CONFIG_DIR",
    },
    "update": {
        "manifest_url": "SM_UPDATE_MANIFEST_URL",
    },
}

_cache: dict[str, Any] | None = None
_cache_path: Path | None = None


def machine_name() -> str:
    """이 PC 의 표시 이름(컴퓨터 이름). 원격/로컬 화면이 '지금 어느 PC 의 세션을
    보고 있는지' 헤더에 보여줄 때 쓴다. Windows 는 COMPUTERNAME, 그 외는 hostname."""
    import socket

    name = os.environ.get("COMPUTERNAME") or ""
    if not name:
        try:
            name = socket.gethostname().split(".")[0]
        except Exception:
            name = ""
    return name or "PC"


def config_path() -> Path:
    """설정 파일 위치. 데이터 폴더와 같은 곳 — 재설치/업데이트가 건드리지 않는다.

    주의: config.claude_home() 을 부르면 순환 import 가 되고, 게다가 claude_home 은
    이 파일이 정하는 CLAUDE_CONFIG_DIR 에 의존한다(닭-달걀). 그래서 여기서는
    env 와 홈 디렉토리만 보고 직접 계산한다.
    """
    override = os.environ.get("CLEWPATH_CONFIG")
    if override:
        return Path(override)
    home = os.environ.get("SESSION_MANAGER_CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(home) if home else Path.home() / ".claude"
    return base / "session_manager" / "clewpath.toml"


def load(refresh: bool = False) -> dict[str, Any]:
    """설정 파일을 읽어 dict 로 돌려준다. 없거나 깨졌으면 빈 dict.

    깨진 파일에 죽지 않는 이유: 설정 오타 하나로 서비스가 안 뜨면 사용자는 원인을
    알 방법이 없다(백그라운드 프로세스라 화면도 없다). 경고만 찍고 기본값으로 뜬다.
    """
    global _cache, _cache_path
    p = config_path()
    if not refresh and _cache is not None and _cache_path == p:
        return _cache
    data: dict[str, Any] = {}
    try:
        if p.is_file():
            # utf-8-sig: PowerShell 5.1 의 Set-Content -Encoding UTF8 은 BOM 을 붙이는데,
            # 순수 utf-8 로 읽으면 tomllib 이 첫 줄부터 거부해 설정이 통째로 무시된다.
            data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        print(f"[config] 설정 파일을 읽지 못했습니다 ({p}): {e} - 기본값으로 계속합니다",
              file=sys.stderr, flush=True)
        data = {}
    _cache, _cache_path = data, p
    return data


def get(section: str, key: str, default: Any = None) -> Any:
    """env > 파일 > default 순으로 값을 찾는다."""
    env_name = _MAP.get(section, {}).get(key)
    if env_name:
        v = os.environ.get(env_name)
        if v not in (None, ""):
            return v
    v = load().get(section, {}).get(key)
    if v not in (None, ""):
        return v
    return default


def get_int(section: str, key: str, default: int) -> int:
    v = get(section, key, None)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        print(f"[config] [{section}] {key} 값이 정수가 아닙니다: {v!r} - 기본값 {default} 사용",
              file=sys.stderr, flush=True)
        return default


def get_bool(section: str, key: str, default: bool) -> bool:
    v = get(section, key, None)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def apply_env() -> list[str]:
    """설정 파일 값을 os.environ 에 밀어넣는다(이미 있는 env 는 건드리지 않음).

    왜 이런 게 필요한가: 코드 곳곳이 이미 os.environ.get("SM_...") 로 흩어져 있다.
    전부 고치는 대신 프로세스 시작 시 한 번 채워주면, 기존 코드는 그대로 두고
    설정 파일만 새로 얹을 수 있다. env 가 이미 있으면 덮지 않으므로 우선순위도 유지된다.

    반환값: 이번에 채워넣은 환경변수 이름들(기동 로그용).
    """
    data = load()
    filled: list[str] = []
    for section, keys in _MAP.items():
        for key, env_name in keys.items():
            if os.environ.get(env_name):
                continue                      # env 가 우선 — 덮지 않는다
            v = data.get(section, {}).get(key)
            if v in (None, ""):
                continue
            os.environ[env_name] = str(v)
            filled.append(env_name)
    return filled


TEMPLATE = """\
# ClewPath Host 설정 파일
#
#   위치: 이 파일은 데이터 폴더에 있습니다. 재설치나 업데이트를 해도 지워지지 않습니다.
#   반영: 값을 고친 뒤 ClewPath 를 재시작하세요.
#   우선순위: 환경변수 > 이 파일 > 기본값
#           (start-connector.ps1 에 $env:... 로 같은 값이 있으면 그쪽이 이깁니다)

[server]
# 로컬 웹 주소. host 는 127.0.0.1 을 권장합니다 —
# 0.0.0.0 으로 열면 같은 공유기의 다른 기기가 인증 없이 들어올 수 있습니다.
host = "127.0.0.1"
port = {port}

# 포트가 이미 사용 중이면 다음 포트를 차례로 시도합니다(0 이면 시도하지 않고 종료).
# 실제로 열린 포트는 기동 로그와 runtime.json 에 적힙니다.
port_fallback = 10

# 비워두면 비밀번호 없이 로컬 전용으로 동작합니다(루프백은 무인증).
# password = "..."

[cloud]
# 외부(폰·웹) 접속을 중계하는 ClewPath Cloud 주소.
cp_url    = "{cp_url}"
relay_url = "{relay_url}"
app_url   = "{app_url}"

# 폰 페어링 QR 생성용 공유 토큰(운영자에게 받은 값). 없으면 QR 대신 수동 입력을 씁니다.
# client_token = "..."

[claude]
# 클로드 데이터 폴더를 옮겨 쓰는 경우에만 지정하세요. 비워두면 자동으로 찾습니다.
# config_dir = "D:\\\\claude"

[update]
# 새 버전이 있는지 주기적으로 확인합니다(설치는 하지 않음).
auto_check = true
check_interval_hours = 24

# 서비스를 재기동할 때 새 버전이 있으면 자동으로 적용합니다.
# 재기동은 사용자의 명시적 행동이라 주기적 자동설치보다 안전해서 기본 ON.
# (서명·해시 검증 통과분만, 실패 시 이전 버전으로 자동 롤백)
apply_on_restart = true

# true 로 두면 재기동을 기다리지 않고 확인 즉시 설치·재시작합니다(주기 백그라운드).
# 기본이 false 인 이유: 작업 도중 예고 없이 재기동되면 놀랄 수 있어서.
auto_apply = false

[hooks]
# Claude Code 훅을 ~/.claude/settings.json 에 등록해 세션 상태(작업중/권한대기/
# 턴종료)를 실시간으로 받습니다. 기존 훅은 보존하며 병합만 합니다.
# false 로 두면 등록하지 않습니다(상태 뱃지·알림 기능이 꺼집니다).
auto_register = true
"""


def render_template(port: int = 5100,
                    cp_url: str = "https://clewpath.pyongso.com/cp",
                    relay_url: str = "wss://clewpath.pyongso.com/relay/ws",
                    app_url: str = "https://clewpath.pyongso.com/relay/app") -> str:
    return TEMPLATE.format(port=port, cp_url=cp_url, relay_url=relay_url, app_url=app_url)
