"""이어받기(resume·fork·묻기) 비용 — 2026-10-23 실측 회귀.

claude 는 이어받은 세션의 과거 누적 비용을 되살려 total_cost_usd·modelUsage 에 섞고 --max-budget-usd 도 그 값과 비교한다:
- 오래 쓴 세션은 API 호출도 없이 error_max_budget_usd 로 실패(감사 기록 9월 6건),
- 사용량 집계는 누적값을 더해 서비스 하루 한도(5달러)를 한 번에 넘길 수 있었고, 여러 턴 재개는 턴마다 누적을 또 더했다.
고친 것: 이어받기엔 --max-budget-usd 를 붙이지 않고(새 세션만), 호출당 상한은 실행 전 분량 어림, 비용은 이번 실행분.
"""
from __future__ import annotations

import pytest

from session_manager import webapi

HAIKU = "claude-haiku-5-5"


def _assistant(mid, model, inp, out, read, cw, h1=True):
    return {"type": "assistant", "message": {"id": mid, "model": model, "content": [], "usage": {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read, "cache_creation_input_tokens": cw,
        "cache_creation": {"ephemeral_1h_input_tokens": cw if h1 else 0, "ephemeral_5m_input_tokens": 0 if h1 else cw}}}}


def _result(total, model_usage, usage=None):
    ev = {"type": "result", "total_cost_usd": total, "modelUsage": model_usage}
    if usage is not None:
        ev["usage"] = usage
    return ev


def test_run_cost_excludes_restored_history():
    """실측 표본(9401f917, haiku): 이번 실행분 2·5·649·4158(1시간) / 누적 4·9·1298·8132 = 0.00164428 → 이번 실행분은 절반쯤."""
    rc = webapi.RunCost()
    rc.feed(_assistant("m1", HAIKU, 2, 5, 649, 4158))
    rc.feed(_result(0.00164428, {HAIKU: {"inputTokens": 4, "outputTokens": 9, "cacheReadInputTokens": 1298,
                                         "cacheCreationInputTokens": 8132, "costUSD": 0.00164428}}))
    assert rc.usd() == pytest.approx(0.00084079, rel=1e-3)


def test_same_message_counted_once_last_usage_wins():
    rc = webapi.RunCost()
    rc.feed(_assistant("m1", HAIKU, 2, 1, 0, 1000))
    rc.feed(_assistant("m1", HAIKU, 2, 5, 0, 1000))          # 같은 메시지의 다음 블록 — 출력이 늘어난 마지막 값
    rc.feed(_result(1.0, {HAIKU: {"inputTokens": 2, "outputTokens": 5, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 1000, "costUSD": 0.2027}}))
    assert rc.usd() == pytest.approx(0.2027, rel=1e-3)       # 이번 실행분이 누적 전부(과거 없음)


def test_multi_turn_is_not_summed_per_result():
    """여러 턴 재개: result 마다 total 이 누적이라 예전처럼 더하면 몇 배로 셌다 — 마지막 계산 하나가 이 실행 전체."""
    rc = webapi.RunCost()
    mu1 = {HAIKU: {"inputTokens": 1, "outputTokens": 1, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 1000, "costUSD": 0.2006}}
    rc.feed(_assistant("m1", HAIKU, 1, 1, 0, 1000))
    rc.feed(_result(0.2006, mu1))
    first = rc.usd()
    mu2 = {HAIKU: {"inputTokens": 2, "outputTokens": 2, "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 1000, "costUSD": 0.2112}}
    rc.feed(_assistant("m2", HAIKU, 1, 1, 1000, 0))
    rc.feed(_result(0.2112, mu2))
    assert first == pytest.approx(0.2006, rel=1e-3) and rc.usd() == pytest.approx(0.2112, rel=1e-3)


def test_multi_model_uses_each_models_rate():
    rc = webapi.RunCost()
    rc.feed(_assistant("a", HAIKU, 100, 0, 0, 0))
    rc.feed(_assistant("b", "claude-opus-5-5", 100, 0, 0, 0))
    rc.feed(_result(99.0, {HAIKU: {"inputTokens": 1000, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.001},
                           "claude-opus-5-5": {"inputTokens": 1000, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.05}}))
    assert rc.usd() == pytest.approx(0.0001 + 0.005, rel=1e-6)


def test_no_api_call_is_zero_not_restored_total():
    """d56d9dca 실측: 과거 누적 114달러가 되살아났지만 이번 실행 usage 는 전부 0(호출 없이 끝남) → 0달러."""
    rc = webapi.RunCost()
    rc.feed(_result(114.44, {"claude-opus-5": {"inputTokens": 3814, "outputTokens": 76491, "cacheReadInputTokens": 54058906,
                                                "cacheCreationInputTokens": 2805391, "costUSD": 57.0}},
                    usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}))
    assert rc.usd() == 0.0


def test_old_claude_without_usage_falls_back_to_total():
    rc = webapi.RunCost()
    rc.feed({"type": "result", "total_cost_usd": 0.003})
    assert rc.usd() == 0.003
    assert webapi.RunCost().usd() is None


def test_budget_flag_only_for_new_sessions():
    g = {"max_budget_usd": 1.0, "model": "m"}
    assert "--max-budget-usd" not in webapi._guardrail_args(g, True)                   # 이어받기(기본)
    assert webapi._guardrail_args(g, True, resume=False)[-3:-1] == ["--max-budget-usd", "1.0"] or \
        "--max-budget-usd" in webapi._guardrail_args(g, True, resume=False)          # 새 세션(서비스 원본 만들기)
    argv = webapi._claude_argv("s1", True, "f1", g)
    assert "--max-budget-usd" not in argv


def test_servicereg_new_base_keeps_budget():
    import inspect
    from session_manager import servicereg
    assert "resume=False" in inspect.getsource(servicereg.make_base)


def _session(home, sid, lines_before, boundary, lines_after):
    from tests.conftest import write_session
    big = {"type": "user", "message": {"role": "user", "content": "가" * 3000}}
    rows = [big] * lines_before + ([{"type": "system", "subtype": "compact_boundary"}] if boundary else []) + [big] * lines_after
    return write_session(home, "P--x", sid, rows)


def test_estimate_resume_usd(fake_claude_home):
    _session(fake_claude_home, "small", 300, True, 2)          # 압축 뒤 몇 KB
    _session(fake_claude_home, "big", 0, False, 400)           # 압축 없음 약 3.6MB
    assert 0 < webapi.estimate_resume_usd("small", 1.0) < 0.1
    assert webapi.estimate_resume_usd("big", 1.0) is None       # 1달러(≈ 526KB) 넘음
    assert webapi.estimate_resume_usd("big", 10.0) is not None
    assert webapi.estimate_resume_usd("nope", 1.0) == pytest.approx(0.0, abs=1e-5)


@pytest.fixture
def client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL", "SM_APP_URL"):
        monkeypatch.delenv(k, raising=False)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    with TestClient(S.create_app()) as c:
        yield c


def test_ask_api_refuses_too_big_before_running(client, fake_claude_home, monkeypatch):
    _session(fake_claude_home, "big", 0, False, 400)
    _session(fake_claude_home, "small", 300, True, 2)
    ran = []
    monkeypatch.setattr(webapi, "ephemeral_ask", lambda sid, *a, **k: ran.append(sid) or
                        {"answer": "ok", "result": "ok", "cost_usd": 0.01, "is_error": False, "tool_uses": []})
    tok = {"Authorization": "Bearer api-token-for-test"}            # 토큰 호출(외부 API) — 호출당 상한 적용
    r = client.post("/api/v1/sessions/big/ask", json={"prompt": "hi"}, headers=tok)
    assert r.status_code == 413 and r.json()["code"] == "too_big" and ran == []
    r = client.post("/api/v1/sessions/small/ask", json={"prompt": "hi"}, headers=tok)
    assert r.status_code == 200 and ran == ["small"]
    r = client.post("/api/v1/sessions/big/ask", json={"prompt": "hi"})   # 사장님 본인(로컬·토큰 없음)은 막지 않는다
    assert r.status_code == 200 and ran == ["small", "big"]
