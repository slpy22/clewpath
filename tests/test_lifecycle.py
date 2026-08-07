"""lifecycle 테스트 — 삭제/cwd변경. fixture 기반, 실제 데이터 안 건드림."""
import json
from pathlib import Path

from session_manager import lifecycle, config
from tests.conftest import write_session


def _make(home, sid="aaaa1111", folder="P--x"):
    write_session(home, folder, sid, [
        {"type": "user", "cwd": "F:\\old\\path", "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
        {"type": "assistant", "cwd": "F:\\old\\path", "message": {"role": "assistant", "content": "yo"},
         "timestamp": "2026-06-01T00:01:00.000Z"},
    ])
    return home / "projects" / folder / f"{sid}.jsonl"


def test_delete_dry_run_lists_targets(sample_session):
    res = lifecycle.delete_session("11111111-2222-3333-4444-555555555555", dry_run=True)
    assert res["dry_run"] is True
    paths = [t["path"] for t in res["would_delete"]]
    assert any(p.endswith(".jsonl") for p in paths)


def test_delete_actually_removes(fake_claude_home):
    jsonl = _make(fake_claude_home)
    assert jsonl.exists()
    res = lifecycle.delete_session("aaaa1111", dry_run=False)
    assert not jsonl.exists()
    assert any("aaaa1111.jsonl" in p for p in res["deleted"])


def test_delete_includes_side_dir_and_session_env(fake_claude_home):
    jsonl = _make(fake_claude_home)
    # 사이드 폴더
    side = jsonl.parent / "aaaa1111"
    side.mkdir()
    (side / "x.txt").write_text("x")
    # session-env
    senv = config.session_env_dir()
    senv.mkdir(parents=True, exist_ok=True)
    (senv / "aaaa1111").write_text("env")

    res = lifecycle.delete_session("aaaa1111", dry_run=False)
    assert not side.exists()
    assert not (senv / "aaaa1111").exists()


def test_change_cwd_dry_run(fake_claude_home):
    _make(fake_claude_home)
    res = lifecycle.change_cwd("aaaa1111", "F:\\new\\path", dry_run=True)
    assert res["dry_run"] is True
    assert res["lines_with_cwd"] == 2
    assert res["old_cwd"] == "F:\\old\\path"


def test_change_cwd_applies_and_backs_up(fake_claude_home):
    jsonl = _make(fake_claude_home)
    res = lifecycle.change_cwd("aaaa1111", "F:\\new\\path", dry_run=False)
    assert res["lines_changed"] == 2
    # 백업 존재
    assert Path(res["backup"]).exists()
    # 실제 치환 확인
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert json.loads(line)["cwd"] == "F:\\new\\path"


def test_change_cwd_preserves_unparseable_lines(fake_claude_home):
    jsonl = _make(fake_claude_home)
    # 깨진 줄 추가
    with jsonl.open("a", encoding="utf-8") as f:
        f.write("not json line\n")
    lifecycle.change_cwd("aaaa1111", "F:\\new", dry_run=False)
    content = jsonl.read_text(encoding="utf-8")
    assert "not json line" in content  # 보존됨
