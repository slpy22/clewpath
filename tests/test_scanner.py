"""scanner 테스트 — fixture 기반."""
from session_manager import scanner


def test_scan_all_finds_session(sample_session):
    sessions = scanner.scan_all()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "11111111-2222-3333-4444-555555555555"
    assert s.project_folder == "F--021-test"


def test_meta_extracted_from_later_lines(sample_session):
    """cwd/slug가 첫 줄(사이드체인)이 아닌 이후 줄에서 추출되어야 한다."""
    sessions = scanner.scan_all()
    s = sessions[0]
    assert s.cwd == "F:\\021_test\\proj"
    assert s.slug == "happy-test-bee"
    assert s.git_branch == "main"
    assert s.version == "1.0.0"


def test_counts_and_timestamps(sample_session):
    s = scanner.scan_all()[0]
    assert s.message_count == 3
    assert s.line_count == 3
    assert s.started_at == "2026-06-01T00:00:00.000Z"
    assert s.ended_at == "2026-06-01T00:05:00.000Z"


def test_scan_one(sample_session):
    s = scanner.scan_one("11111111-2222-3333-4444-555555555555")
    assert s is not None
    assert s.slug == "happy-test-bee"
    assert scanner.scan_one("nonexistent") is None


def test_empty_when_no_projects(fake_claude_home):
    assert scanner.scan_all() == []


def test_malformed_lines_are_counted_but_skipped(fake_claude_home):
    from tests.conftest import write_session
    write_session(fake_claude_home, "P--x", "abc", [])
    jsonl = fake_claude_home / "projects" / "P--x" / "abc.jsonl"
    jsonl.write_text('not json\n{"type":"user","cwd":"X","timestamp":"t1"}\nbad\n', encoding="utf-8")
    s = scanner.scan_one("abc")
    assert s.line_count == 3       # 빈 줄 제외 전체
    assert s.message_count == 1    # 파싱 성공한 것만
    assert s.cwd == "X"
