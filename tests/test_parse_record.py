"""parse_record 계약 — 배치 뷰어와 라이브 tail(monitor) 이 공유하는 단일 파서.

monitor 오케스트레이션 관제의 토대이므로, 대화 레코드는 이벤트로 변환하고
메타 레코드(mode/queue-operation/attachment 등)는 걸러내며, 관리 세션의
`claude -p --resume <UUID>` Bash 호출 command 를 손실 없이 보존하는지 잠근다.
"""
from __future__ import annotations

from session_manager.viewer import parse_record


def test_user_text_record():
    ev = parse_record({"type": "user",
                       "message": {"role": "user", "content": "고쳐줘"},
                       "timestamp": "2026-09-03T00:00:00Z"})
    assert ev is not None
    assert ev["role"] == "user"
    assert ev["text"] == "고쳐줘"
    assert ev["timestamp"] == "2026-09-03T00:00:00Z"


def test_assistant_tool_use_record():
    ev = parse_record({"type": "assistant", "message": {"role": "assistant",
        "content": [
            {"type": "text", "text": "실행합니다"},
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "ls"}},
        ]}})
    assert ev["role"] == "assistant"
    assert ev["text"] == "실행합니다"
    assert ev["tools"] == ["Bash"]
    assert ev["tool_calls"][0]["id"] == "tu1"


def test_meta_records_are_filtered():
    # monitor 타임라인이 무시해야 하는 메타 레코드 타입들
    for t in ("mode", "queue-operation", "attachment", "last-prompt",
              "atis-latch", "system", "permission-mode", "summary",
              "custom-title", "ai-title", "agent-name"):
        assert parse_record({"type": t, "foo": "bar"}) is None, t


def test_non_dict_and_missing_message():
    assert parse_record(None) is None
    assert parse_record("not a dict") is None
    assert parse_record({"type": "user"}) is None            # message 없음
    assert parse_record({"type": "user", "message": "str"}) is None  # message 비-dict


def test_empty_conversation_record_filtered():
    # text/tools/results 모두 비면 대화 이벤트가 아님(빈 메타)
    assert parse_record({"type": "assistant",
                         "message": {"role": "assistant", "content": []}}) is None
    assert parse_record({"type": "user",
                         "message": {"role": "user", "content": ""}}) is None


def test_callline_command_preserved_for_monitor():
    # 관리 세션의 하위 호출: Bash tool_use command 에 claude -p --resume <UUID>.
    # monitor 가 여기서 호출선을 뽑으므로 command 가 손실 없이 보존돼야 한다.
    uuid = "59f9577b-0d25-47be-994b-29009cf0fba3"
    cmd = f'cd "F:/portal" && claude -p --resume {uuid} "로그인 붙여줘"'
    ev = parse_record({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "id": "tu9", "name": "Bash",
                     "input": {"command": cmd}}]}})
    got = ev["tool_calls"][0]["input"]["command"]
    assert uuid in got
    assert "--resume" in got
    # UUID 는 command 선두라 절삭(_cap_val 3000자) 영향권 밖 — 항상 온전
    assert got.index(uuid) < 100


def test_tool_result_pairing():
    ev = parse_record({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu9", "is_error": False,
         "content": [{"type": "text", "text": "STEP2 DONE"}]},
    ]}})
    assert ev["tool_results"][0]["tool_use_id"] == "tu9"
    assert ev["tool_results"][0]["text"] == "STEP2 DONE"
    assert ev["tool_results"][0]["is_error"] is False
