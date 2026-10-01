"""동봉 스킬 설치 — `~/.claude/skills/<스킬>/SKILL.md` 새 파일 생성(workers·assistant).

불가침 원칙 예외(workers 2026-09-23, assistant 2026-10-01 사장님 승인): claude 가 읽는 폴더에 **파일을 새로 만드는** 행위라
반드시 사용자의 명시 동작(설정 화면 버튼 + 확인창) 뒤에만 호출된다. 기존 파일은 건드리지 않고,
이미 있으면 overwrite 를 명시해야 덮어쓴다. 어떤 기동·백그라운드 경로에서도 호출하지 않는다
(회귀 가드: tests/test_skillinstall.py).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from session_manager import config

SKILL_NAME = "clewpath-workers"
# 설치 가능한 동봉 스킬(짧은 이름 → 폴더). 이 목록 밖은 설치할 수 없다(승인된 예외만).
SKILLS = {"workers": "clewpath-workers", "assistant": "clewpath-assistant"}


def _name(key: str | None) -> str:
    if key is None:
        return SKILL_NAME
    if key not in SKILLS:
        raise KeyError(key)
    return SKILLS[key]


def bundled_path(key: str | None = None) -> Path:
    return Path(__file__).resolve().parent / "skills" / _name(key) / "SKILL.md"


def target_path(key: str | None = None) -> Path:
    return config.claude_home() / "skills" / _name(key) / "SKILL.md"


def _digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def status(key: str | None = None) -> dict:
    """설치 여부·동봉본과 같은지(설정 화면 표시용). 파일을 쓰지 않는다."""
    src, dst = bundled_path(key), target_path(key)
    return {"skill": _name(key), "bundled": src.is_file(), "installed": dst.is_file(),
            "up_to_date": dst.is_file() and _digest(src) == _digest(dst),
            "path": str(dst)}


def install(overwrite: bool = False, key: str | None = None) -> dict:
    """사용자 확인 뒤에만 호출. 이미 있으면 overwrite 없이는 exists 로 거절."""
    src, dst = bundled_path(key), target_path(key)
    if not src.is_file():
        raise FileNotFoundError("bundled skill missing")
    if dst.exists() and not overwrite:
        return {"installed": False, "exists": True, "path": str(dst)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return {"installed": True, "exists": False, "path": str(dst)}
