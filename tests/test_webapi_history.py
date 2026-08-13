"""[원칙 회귀 방지] 웹 재개 프롬프트의 claude 히스토리 기록 기능은 제거됐다.

~/.claude/history.jsonl 은 claude 소유 파일(터미널 ↑/↓ 입력 히스토리)이고,
고지 없는 자동 append 는 'claude 파일 무단 수정 금지' 원칙 위반이다(CLAUDE.md).
같은 이름의 함수가 부활하면 이 테스트가 막는다 - 되살리려면 옵트인 설정
(위험 고지+승인) 게이트와 함께 원칙 협의를 먼저 할 것.
"""


def test_no_silent_claude_history_append():
    from session_manager import webapi
    assert not hasattr(webapi, "_append_claude_history")
