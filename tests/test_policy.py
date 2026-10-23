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


def test_usage_summary_recent_reads_into_rotated_file(fake_claude_home):
    """TODOS 299행: api_audit.jsonl 이 8MB 에서 .1 로 회전하면 직후 '최근 n건'이 짧아졌다 — .1 의 끝에서 이어 채운다.
    롤업 합계(by_session·total)는 지금처럼 현재 파일만 본다."""
    import json as _json
    af = policy._audit_file()
    af.parent.mkdir(parents=True, exist_ok=True)
    rot = af.with_name(af.name + ".1")
    rot.write_text("".join(_json.dumps({"ts": f"2026-10-01T00:00:{i:02d}", "event": "ask", "session": f"OLD{i}",
                                        "cost_usd": 0.1}) + "\n" for i in range(20)) + "깨진 줄\n", encoding="utf-8")
    af.write_text("".join(_json.dumps({"ts": f"2026-10-02T00:00:{i:02d}", "event": "ask", "session": f"NEW{i}",
                                       "cost_usd": 0.2}) + "\n" for i in range(3)), encoding="utf-8")
    s = policy.usage_summary(recent_n=10)
    assert [e["session"] for e in s["recent"]] == ["NEW2", "NEW1", "NEW0"] + [f"OLD{i}" for i in range(19, 12, -1)]
    assert s["total_calls"] == 3 and {r["session"] for r in s["by_session"]} == {"NEW0", "NEW1", "NEW2"}
    # 현재 파일만으로 충분하면 .1 은 보지 않는다
    assert [e["session"] for e in policy.usage_summary(recent_n=2)["recent"]] == ["NEW2", "NEW1"]
    # 현재 파일이 없고 .1 만 있어도(회전 직후 첫 기록 전) 최근 목록은 채운다
    af.unlink()
    assert [e["session"] for e in policy.usage_summary(recent_n=3)["recent"]] == ["OLD19", "OLD18", "OLD17"]


def test_tail_records_across_blocks(fake_claude_home):
    """.1 은 8MB 까지 — 끝에서 64KB 블록으로 거꾸로 읽을 때 블록 경계에 걸친 줄도 온전히 읽는다."""
    import json as _json
    p = policy._audit_file().with_name("big.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(_json.dumps({"i": i, "pad": "가" * (i % 97)}) + "\n" for i in range(5000)), encoding="utf-8")
    assert p.stat().st_size > 3 * 65536
    assert [e["i"] for e in policy._tail_records(p, 2000)] == list(range(3000, 5000))
    assert [e["i"] for e in policy._tail_records(p, 9999)] == list(range(5000))
    assert policy._tail_records(p.with_name("없음.jsonl"), 5) == [] and policy._tail_records(p, 0) == []
