"""경로 ↔ 폴더명 인코딩.

Claude Code는 작업 경로를 projects/ 하위 폴더명으로 인코딩한다.
규칙: 알파벳/숫자가 아닌 모든 문자를 '-'로 치환한다. (손실 인코딩 — 역변환 불가)
예: 'F:\\021_3_AX 기술스택' -> 'F--021-3-AX-----'
"""
from __future__ import annotations

import re


def path_to_folder(path: str) -> str:
    """작업 경로를 projects/ 하위 폴더명으로 인코딩한다."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)
