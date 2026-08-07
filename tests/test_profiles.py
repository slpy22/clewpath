"""세션 가드레일 프로파일 + 전역 병합 테스트."""
from session_manager import profiles, policy


def test_set_get_profile(fake_claude_home):
    rec = profiles.set_record("S1", permission_mode="plan",
                              allowed_tools=["Read", "Grep", "Read"],
                              deny_patterns=["(?i)secret"], max_output_chars=500)
    assert rec["permission_mode"] == "plan"
    assert rec["allowed_tools"] == ["Read", "Grep"]      # 중복 제거
    assert rec["max_output_chars"] == 500
    assert profiles.get("S1")["deny_patterns"] == ["(?i)secret"]


def test_empty_profile_deleted(fake_claude_home):
    profiles.set_record("S1", model="fable")
    profiles.set_record("S1", model="")                   # 비우면 레코드 삭제
    assert profiles.get("S1") == {}


def test_effective_merge_tightens(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_API_DENIED_TOOLS", "Bash Write")
    profiles.set_record("S1", denied_tools=["WebFetch"], allowed_tools=["Read"],
                        permission_mode="plan", max_budget_usd=0.2)
    g = policy.effective_guardrails("S1")
    # denied = 전역 ∪ 프로파일 (합집합)
    assert set(g["denied_tools"]) == {"Bash", "Write", "WebFetch"}
    assert g["allowed_tools"] == ["Read"]
    assert g["permission_mode"] == "plan"
    # 예산은 전역과 프로파일 중 작은 값
    assert g["max_budget_usd"] == 0.2


def test_effective_no_profile_uses_global(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_API_DENIED_TOOLS", "Bash")
    g = policy.effective_guardrails("NONE")
    assert g["denied_tools"] == ["Bash"]
    assert g["allowed_tools"] is None
    assert g["permission_mode"] is None


def test_match_denied(fake_claude_home):
    assert policy.match_denied("my PASSWORD here", ["(?i)password"]) == "(?i)password"
    assert policy.match_denied("clean text", ["(?i)password"]) is None
    # 잘못된 정규식 → 리터럴 포함검사 폴백
    assert policy.match_denied("has [weird", ["[weird"]) == "[weird"
