"""테스트 픽스처 — 실제 ~/.claude 대신 임시 디렉토리를 사용한다."""
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def fake_claude_home(tmp_path, monkeypatch):
    """가짜 ~/.claude 구조를 만들고 환경변수로 주입한다."""
    home = tmp_path / ".claude"
    projects = home / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(home))
    return home


def write_session(home: Path, project_folder: str, session_id: str, lines: list[dict]):
    """가짜 세션 jsonl을 만든다."""
    pdir = home / "projects" / project_folder
    pdir.mkdir(parents=True, exist_ok=True)
    jsonl = pdir / f"{session_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    return jsonl


@pytest.fixture
def sample_session(fake_claude_home):
    """대표 세션 1개를 만든다 — 첫 줄은 cwd 없는 사이드체인, 이후 본문에 cwd."""
    home = fake_claude_home
    write_session(home, "F--021-test", "11111111-2222-3333-4444-555555555555", [
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "side"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
        {"type": "user", "cwd": "F:\\021_test\\proj", "slug": "happy-test-bee",
         "gitBranch": "main", "version": "1.0.0",
         "message": {"role": "user", "content": "hi"}, "timestamp": "2026-06-01T00:00:01.000Z"},
        {"type": "assistant", "cwd": "F:\\021_test\\proj",
         "message": {"role": "assistant", "content": "hello", "usage": {"input_tokens": 10, "output_tokens": 5}},
         "timestamp": "2026-06-01T00:05:00.000Z"},
    ])
    return home
