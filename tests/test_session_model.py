"""세션별 재개 모델 — 표시(scanner last_model) + 설정(labels 오버라이드 → argv --model).

실측 확정: `claude --resume`(--model 없이)은 세션의 마지막 message.model 을 계승한다.
ClewPath 는 사이드카에 세션별 모델을 저장하고 재개 시 --model 로 붙인다(claude 파일 무수정).
"""
from __future__ import annotations

from session_manager import scanner, labels, webterm, webapi
from tests.conftest import write_session

SID = "aaaaaaaa-1111-2222-3333-444444444444"


def _asst(text, model, ts):
    return {"type": "assistant", "cwd": "F:/p",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": text}]},
            "timestamp": ts}


# ---- 표시: scanner.last_model ----

def test_scanner_last_model_is_most_recent(fake_claude_home):
    write_session(fake_claude_home, "F--p", SID, [
        {"type": "user", "cwd": "F:/p",
         "message": {"role": "user", "content": "hi"}, "timestamp": "t0"},
        _asst("a", "claude-opus-4-8", "t1"),
        _asst("b", "claude-fable-5-1", "t2"),   # 마지막 실모델
        {"type": "assistant", "cwd": "F:/p",     # <synthetic> 은 제외돼야 함
         "message": {"role": "assistant", "model": "<synthetic>",
                     "content": [{"type": "text", "text": "sys"}]},
         "timestamp": "t3"},
    ])
    m = scanner.scan_one(SID)
    assert m.last_model == "claude-fable-5-1"   # 마지막 non-synthetic


def test_scanner_last_model_none_when_absent(fake_claude_home):
    write_session(fake_claude_home, "F--p", SID, [
        {"type": "user", "cwd": "F:/p",
         "message": {"role": "user", "content": "hi"}, "timestamp": "t0"},
    ])
    assert scanner.scan_one(SID).last_model is None


# ---- 저장: labels 오버라이드 ----

def test_labels_model_override_set_get_clear(fake_claude_home):
    assert labels.model_of(SID) is None
    labels.set_record(SID, model="claude-opus-5")
    assert labels.model_of(SID) == "claude-opus-5"
    # 라벨 없이 모델만 있어도 레코드가 살아있어야(프루닝 예외)
    assert labels.get(SID).get("model") == "claude-opus-5"
    labels.set_record(SID, model=None)              # 해제
    assert labels.model_of(SID) is None


# ---- 재개 argv 에 --model 주입 ----

def test_webterm_argv_injects_override(fake_claude_home):
    labels.set_record(SID, model="claude-opus-5")
    argv = webterm._claude_argv(SID, skip_permissions=True)
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-opus-5"
    assert "--resume" in argv and SID in argv


def test_webterm_argv_no_override_no_flag(fake_claude_home):
    argv = webterm._claude_argv(SID, skip_permissions=True)
    assert "--model" not in argv


def test_webterm_argv_fork_ignores_override(fake_claude_home):
    # fork 는 새 세션이라 원본의 모델 오버라이드를 적용하지 않는다
    labels.set_record(SID, model="claude-opus-5")
    argv = webterm._claude_argv(SID, skip_permissions=True, fork_id="fork-1")
    assert "--model" not in argv


def test_webapi_argv_injects_override(fake_claude_home):
    labels.set_record(SID, model="claude-opus-5")
    argv = webapi._claude_argv(SID, skip_permissions=True)
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-opus-5"


def test_webapi_argv_guardrail_model_wins(fake_claude_home):
    # 가드레일(정책) 모델이 세션 오버라이드보다 우선
    labels.set_record(SID, model="claude-opus-5")
    argv = webapi._claude_argv(SID, skip_permissions=True,
                               guardrails={"model": "claude-haiku-4-5-20251001"})
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"


# ---- API 엔드포인트 ----

def _client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    return TestClient(create_app())


def test_api_models_list_builtin_fallback(fake_claude_home, monkeypatch):
    # CP 미설정 → 내장 기본 목록 폴백
    monkeypatch.delenv("SM_CP_URL", raising=False)
    from session_manager import cp_client
    cp_client._models_cache.update(models=None, at=0)
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.get("/api/models")
        assert r.status_code == 200 and r.json()["source"] == "builtin"
        ids = [m["id"] for m in r.json()["models"]]
        assert "claude-opus-5" in ids and "claude-fable-5-1" in ids


def test_api_models_from_cp(fake_claude_home, monkeypatch):
    # CP 목록이 있으면 그걸 우선 사용
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "fetch_models",
                        lambda: [{"id": "claude-opus-5", "label": "Opus 5 (CP)"}])
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.get("/api/models")
        assert r.json()["source"] == "cp"
        assert r.json()["models"][0]["label"] == "Opus 5 (CP)"


def test_fetch_models_cache_and_no_cp(monkeypatch):
    from session_manager import cp_client
    # CP 미설정 → None
    cp_client._models_cache.update(models=None, at=0)
    monkeypatch.delenv("SM_CP_URL", raising=False)
    assert cp_client.fetch_models() is None
    # CP 설정 + 응답 → 목록, 2회차는 캐시(호출 1회만)
    monkeypatch.setenv("SM_CP_URL", "http://cp.test")
    cp_client._models_cache.update(models=None, at=0)
    calls = {"n": 0}

    class _R:
        status_code = 200
        def json(self):  # noqa: E301
            return {"models": [{"id": "claude-fable-5-1", "label": "Fable"}]}

    def fake_get(url, **kw):
        calls["n"] += 1
        assert url.endswith("/config/models")
        return _R()
    monkeypatch.setattr(cp_client.httpx, "get", fake_get)
    m1 = cp_client.fetch_models()
    m2 = cp_client.fetch_models()   # 캐시
    assert m1 == m2 and m1[0]["id"] == "claude-fable-5-1"
    assert calls["n"] == 1


def test_api_set_and_clear_model(fake_claude_home, monkeypatch):
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.post(f"/api/sessions/{SID}/model", json={"model": "claude-opus-5"})
        assert r.status_code == 200 and r.json()["model_override"] == "claude-opus-5"
        assert labels.model_of(SID) == "claude-opus-5"
        r = c.post(f"/api/sessions/{SID}/model", json={"model": None})
        assert r.status_code == 200 and r.json()["model_override"] is None
        assert labels.model_of(SID) is None


def test_api_set_model_rejects_bad_token(fake_claude_home, monkeypatch):
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.post(f"/api/sessions/{SID}/model",
                   json={"model": "bad; rm -rf /"})
        assert r.status_code == 400


def test_v1_sessions_exposes_resume_model(fake_claude_home, monkeypatch):
    write_session(fake_claude_home, "F--p", SID, [
        {"type": "user", "cwd": "F:/p",
         "message": {"role": "user", "content": "hi"}, "timestamp": "t0"},
        _asst("a", "claude-opus-4-8", "t1"),
    ])
    labels.set_record(SID, model="claude-opus-5")
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.get("/api/sessions")   # 로컬 PWA 가 쓰는 경로(외부 API 토큰 불필요)
        allsess = [x for p in r.json()["projects"] for x in p["sessions"]]
        s = next(x for x in allsess if x["session_id"] == SID)
        assert s["last_model"] == "claude-opus-4-8"       # 감지(마지막 실사용)
        assert s["model_override"] == "claude-opus-5"     # 설정값
        assert s["resume_model"] == "claude-opus-5"       # 오버라이드 우선
