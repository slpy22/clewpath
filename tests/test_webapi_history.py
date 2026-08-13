"""웹 재개 프롬프트 → claude 입력 히스토리(~/.claude/history.jsonl) 기록.

터미널 ↑/↓ 가 읽는 파일이라 포맷(실측 키 5개)이 깨지면 조용히 무용지물이 된다.
"""
import json


def test_web_prompt_appends_claude_history(fake_claude_home):
    from session_manager import webapi
    webapi._append_claude_history("웹에서 보낸 질문", "sid-123", r"F:\proj\demo")
    line = (fake_claude_home / "history.jsonl").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["display"] == "웹에서 보낸 질문"
    assert rec["sessionId"] == "sid-123"
    assert rec["project"] == r"F:\proj\demo"
    assert rec["pastedContents"] == {}
    assert isinstance(rec["timestamp"], int) and rec["timestamp"] > 10**12  # ms 단위


def test_empty_prompt_not_recorded(fake_claude_home):
    from session_manager import webapi
    webapi._append_claude_history("", "sid-123", None)
    assert not (fake_claude_home / "history.jsonl").exists()
