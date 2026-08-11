"""구조화 카드용 대화 추출 회귀 - 도구 호출/결과가 짝 맞춰 나오는지."""
from __future__ import annotations

from session_manager import viewer
from tests.conftest import write_session


def test_conversation_includes_tool_calls_and_results(fake_claude_home):
    write_session(fake_claude_home, "F--proj", "aaaaaaaa-1111-2222-3333-444444444444", [
        {"type": "user", "cwd": "F:/p",
         "message": {"role": "user", "content": "고쳐줘"},
         "timestamp": "2026-08-11T00:00:00Z"},
        {"type": "assistant", "cwd": "F:/p",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "고치겠습니다"},
             {"type": "tool_use", "id": "tu1", "name": "Edit",
              "input": {"file_path": "F:/p/a.py", "old_string": "x=1", "new_string": "x=2"}},
         ]},
         "timestamp": "2026-08-11T00:00:01Z"},
        {"type": "user", "cwd": "F:/p",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1", "is_error": False,
              "content": [{"type": "text", "text": "ok"}]},
         ]},
         "timestamp": "2026-08-11T00:00:02Z"},
    ])
    r = viewer.read_conversation("aaaaaaaa-1111-2222-3333-444444444444")
    msgs = r["messages"]
    a = next(m for m in msgs if m["role"] == "assistant")
    assert a["tool_calls"][0]["id"] == "tu1"
    assert a["tool_calls"][0]["input"]["new_string"] == "x=2"
    # 결과 레코드(순수 tool_result)도 이제 포함되고 id 로 짝을 맞출 수 있다
    res = next(m for m in msgs if m["tool_results"])
    assert res["tool_results"][0]["tool_use_id"] == "tu1"
    assert res["tool_results"][0]["text"] == "ok"
    assert res["tool_results"][0]["is_error"] is False


def test_huge_tool_input_is_capped(fake_claude_home):
    write_session(fake_claude_home, "F--proj", "bbbbbbbb-1111-2222-3333-444444444444", [
        {"type": "assistant", "cwd": "F:/p",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu2", "name": "Write",
              "input": {"file_path": "F:/p/big.txt", "content": "x" * 50_000}},
         ]},
         "timestamp": "2026-08-11T00:00:00Z"},
    ])
    r = viewer.read_conversation("bbbbbbbb-1111-2222-3333-444444444444")
    content = r["messages"][0]["tool_calls"][0]["input"]["content"]
    assert len(content) < 4000 and content.endswith("…(생략)")
