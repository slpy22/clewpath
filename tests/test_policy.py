"""외부 API 가드레일 정책 테스트."""
import importlib

from session_manager import policy


def test_denied_tools_default(monkeypatch):
    monkeypatch.delenv("SM_API_DENIED_TOOLS", raising=False)
    tools = policy.denied_tools()
    assert "Bash" in tools and "Write" in tools and "Edit" in tools


def test_denied_tools_override(monkeypatch):
    monkeypatch.setenv("SM_API_DENIED_TOOLS", "Bash,Foo Bar")
    assert policy.denied_tools() == ["Bash", "Foo", "Bar"]


def test_local_token_unlimited(fake_claude_home):
    ok, reason, _ = policy.check_quota(None)
    assert ok and reason == ""


def test_daily_call_cap(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_API_DAILY_CALLS", "2")
    tok = "t1"
    assert policy.check_quota(tok)[0] is True
    policy.record_usage(tok, 0.1)
    policy.record_usage(tok, 0.1)
    ok, reason, u = policy.check_quota(tok)
    assert ok is False
    assert "호출 한도" in reason
    assert u["calls"] == 2


def test_daily_usd_cap(fake_claude_home, monkeypatch):
    monkeypatch.setenv("SM_API_DAILY_USD", "1")
    tok = "t2"
    policy.record_usage(tok, 1.5)  # 이미 초과
    ok, reason, _ = policy.check_quota(tok)
    assert ok is False and "비용 한도" in reason


def test_audit_appends(fake_claude_home):
    policy.audit({"event": "ask", "session": "S1"})
    policy.audit({"event": "ask", "session": "S2"})
    p = policy._audit_file()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert '"S2"' in lines[1]


def test_token_id_stable_and_hashed():
    assert policy.token_id(None) == "local"
    a = policy.token_id("secret")
    assert a == policy.token_id("secret") and a != "secret" and len(a) == 12
