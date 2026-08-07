"""경로 및 설정 — ~/.claude 디렉토리 구조를 가리킨다."""
from __future__ import annotations

import os
from pathlib import Path


def claude_home() -> Path:
    """클로드 데이터 폴더를 찾는다. 우선순위:

      1) SESSION_MANAGER_CLAUDE_HOME  — 이 도구 전용 재정의(테스트/격리)
      2) CLAUDE_CONFIG_DIR            — 클로드 코드 공식 환경변수(사용자가 옮긴 경우)
      3) ~/.claude                    — 기본 위치

    2) 를 빠뜨리면 폴더를 옮겨 쓰는 사용자에게는 '세션 0개'로 보인다.
    """
    override = os.environ.get("SESSION_MANAGER_CLAUDE_HOME")
    if override:
        return Path(override)
    official = os.environ.get("CLAUDE_CONFIG_DIR")
    if official:
        return Path(official)
    return Path.home() / ".claude"


def claude_exe() -> str:
    """claude 실행파일 경로. PATH 에 없으면 흔한 설치 위치를 뒤진다.

    작업 스케줄러로 뜬 프로세스는 PATH 가 로그인 셸과 다를 수 있어,
    문자열 'claude' 만 믿으면 재개가 조용히 실패한다.
    """
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    cands = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / "AppData" / "Roaming" / "npm" / "claude.cmd",
        home / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
        Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude"),
    ]
    for c in cands:
        try:
            if c.exists():
                return str(c)
        except Exception:  # noqa: BLE001
            pass
    return "claude"   # 최후: PATH 에 있길 기대


def projects_dir() -> Path:
    return claude_home() / "projects"


def session_env_dir() -> Path:
    return claude_home() / "session-env"


def todos_dir() -> Path:
    return claude_home() / "todos"


def shell_snapshots_dir() -> Path:
    return claude_home() / "shell-snapshots"


def data_dir() -> Path:
    """ClewPath Host 자체 데이터(라벨 등) 디렉토리. 원본 세션과 분리 보관.

    경로는 호환을 위해 session_manager 그대로 둔다(기존 설치본의 라벨·기기 유지).
    """
    return claude_home() / "session_manager"


def labels_file() -> Path:
    """세션 라벨 사이드카(JSON). 원본 jsonl 은 절대 건드리지 않는다."""
    return data_dir() / "labels.json"
