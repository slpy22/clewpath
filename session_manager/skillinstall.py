"""동봉 스킬 설치 — `~/.claude/skills/clewpath-workers/SKILL.md` 새 파일 생성.

불가침 원칙 예외(2026-09-23 사장님 승인): claude 가 읽는 폴더에 **파일을 새로 만드는** 행위라
반드시 사용자의 명시 동작(설정 화면 버튼 + 확인창) 뒤에만 호출된다. 기존 파일은 건드리지 않고,
이미 있으면 overwrite 를 명시해야 덮어쓴다. 어떤 기동·백그라운드 경로에서도 호출하지 않는다
(회귀 가드: tests/test_skillinstall.py).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from session_manager import config

SKILL_NAME = "clewpath-workers"


def bundled_path() -> Path:
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"


def target_path() -> Path:
    return config.claude_home() / "skills" / SKILL_NAME / "SKILL.md"


def _digest(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def status() -> dict:
    """설치 여부·동봉본과 같은지(설정 화면 표시용). 파일을 쓰지 않는다."""
    src, dst = bundled_path(), target_path()
    return {"skill": SKILL_NAME, "bundled": src.is_file(), "installed": dst.is_file(),
            "up_to_date": dst.is_file() and _digest(src) == _digest(dst),
            "path": str(dst)}


def install(overwrite: bool = False) -> dict:
    """사용자 확인 뒤에만 호출. 이미 있으면 overwrite 없이는 exists 로 거절."""
    src, dst = bundled_path(), target_path()
    if not src.is_file():
        raise FileNotFoundError("bundled skill missing")
    if dst.exists() and not overwrite:
        return {"installed": False, "exists": True, "path": str(dst)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return {"installed": True, "exists": False, "path": str(dst)}
