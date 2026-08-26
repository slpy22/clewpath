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


def test_picker_hidden_detection(fake_claude_home):
    """picker_hidden = 대화형 마커(mode/permission-mode/system) 유무.

    claude --resume 픽커는 '대화형으로 열린 적 있는 세션'만 나열한다(실측
    2026-08-26: gitBranch 동일 세 세션에서 이 마커 유무만이 픽커 표시를 갈랐다).
    ai-title 유무 기준은 반례(ai-title 있는 에이전트 세션)로 폐기됨.
    """
    from session_manager import scanner
    from tests.conftest import write_session
    U = lambda i: {"type": "user", "cwd": "F:\p",
                   "message": {"role": "user", "content": f"m{i}"},
                   "timestamp": f"2026-08-13T00:0{i}:00.000Z"}
    # ① 헤드리스/에이전트 산물: 대화형 마커 0, ai-title 있어도 hidden
    write_session(fake_claude_home, "P--x", "dddd4444",
                  [U(0), {"type": "ai-title", "aiTitle": "제목"}])
    # ② 대화형 세션: mode/system 마커 있으면 표시(제목 유무 무관)
    write_session(fake_claude_home, "P--x", "eeee5555",
                  [{"type": "system", "content": "init"}, U(0),
                   {"type": "mode", "mode": "default"}])
    # ③ 대화형이지만 제목 없음 → 그래도 표시(마커 기준)
    write_session(fake_claude_home, "P--x", "ffff6666",
                  [{"type": "permission-mode"}, U(0)])
    # ④ 빈 파일 → hidden 아님(목록에도 안 뜸, 오탐 방지)
    write_session(fake_claude_home, "P--x", "00007777", [])
    # ⑤ agent-name 만 있는 스텁(fork/named 산물) → 마커 0이어도 픽커에 뜸
    write_session(fake_claude_home, "P--x", "55558888",
                  [{"type": "ai-title", "aiTitle": "x"},
                   {"type": "agent-name", "agentName": "터널 스텁"}])
    metas = {m.session_id: m for m in scanner.scan_all()}
    assert metas["dddd4444"].picker_hidden is True    # 에이전트 산물(ai-title 무관)
    assert metas["eeee5555"].picker_hidden is False   # 대화형
    assert metas["ffff6666"].picker_hidden is False   # 대화형(제목 없어도)
    assert metas["00007777"].picker_hidden is False   # 빈 세션
    assert metas["55558888"].picker_hidden is False   # agent-name 스텁(픽커 뜸)

