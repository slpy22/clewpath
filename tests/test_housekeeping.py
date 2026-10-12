"""정기 작업(2026-10-12): 쉬는 세션 정리 조건·세션 잠금 재확인, 모델 목록 합본, 작업 줄, API·원격 2FA."""
import threading
import time
from types import SimpleNamespace as NS

import pytest

from session_manager import connector, housekeeping as hk, webterm

_RT: dict = {}
_PUSH: list = []


class _Proc:
    def __init__(self):
        self.alive = True
        self.pid = 1

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False


@pytest.fixture
def terms(fake_claude_home, monkeypatch):
    webterm._ACTIVE.clear()
    from session_manager import bgtasks, hooks, peers
    monkeypatch.setattr(hooks, "status_of", lambda sid: _RT.get(sid))
    monkeypatch.setattr(peers, "peer_map", lambda: {})
    monkeypatch.setattr(bgtasks, "busy", lambda c: False)
    monkeypatch.setattr("session_manager.push.send", lambda *a, **k: _PUSH.append(a))
    _RT.clear()
    _PUSH.clear()
    old = time.time() - 6 * 3600

    def add(sid, **kw):
        s = webterm._TermSession(sid, _Proc(), persist=kw.pop("persist", True))
        s.started_at = kw.pop("started", old)
        for k, v in kw.items():
            setattr(s, k, v)
        webterm._ACTIVE[sid] = s
        return s
    yield add
    webterm._ACTIVE.clear()
    while not hk._q.empty():
        hk._q.get_nowait()


def test_idle_rules(terms, monkeypatch):
    from session_manager import bgtasks, labels
    now = time.time()
    terms("idle1")                                     # 6시간 무활동 → 대상
    terms("recent", last_input=now - 600)              # 10분 전 입력
    terms("viewing", client=("ws", None))              # 화면이 붙어 있음
    terms("perm")
    _RT["perm"] = {"phase": "permission", "permission_at": now - 5 * 3600}
    terms("kept")
    labels.set_record("kept", keep="1")
    terms("fork", persist=False)                       # 일회용 포크는 대상 아님
    terms("hookrecent")
    _RT["hookrecent"] = {"phase": "ready", "ready_at": now - 1800}
    terms("justadopted", started=now - 60)             # 이 Host 가 방금 다시 붙임(업데이트 직후)
    assert [c["session_id"] for c in hk.idle_candidates(now)] == ["idle1"]
    assert hk.idle_candidates(now)[0]["idle_s"] >= 6 * 3600 - 5
    monkeypatch.setattr(bgtasks, "busy", lambda c: True)   # 백그라운드 쉘·모니터가 남아 있으면 제외
    assert hk.idle_candidates(now) == []


def test_reap_stops_and_rechecks_under_lock(terms, monkeypatch):
    a = terms("idle1")
    b = terms("idle2")
    real_stop_if = webterm.stop_if

    def racing(sid, check):
        if sid == "idle2":
            b.last_attach = time.time()               # 후보로 뽑힌 뒤 누가 화면을 열었다
        return real_stop_if(sid, check)
    monkeypatch.setattr(webterm, "stop_if", racing)
    r = hk.reap_idle()
    assert [c["session_id"] for c in r["stopped"]] == ["idle1"]
    assert r["kept_at_last_check"] == 1
    assert a.proc.alive is False and b.proc.alive is True
    assert _PUSH and "1개" in _PUSH[0][2]


def test_hours_option_and_job_state(terms):
    now = time.time()
    terms("s", last_input=now - 2.5 * 3600)
    assert hk.idle_candidates(now) == []                        # 기본 4시간
    hk.set_options(hours=2)
    assert [c["session_id"] for c in hk.idle_candidates(now)] == ["s"]
    with pytest.raises(ValueError):
        hk.set_options(hours=3)
    hk.set_options(job="idle_sessions", on=False)
    assert hk._due(time.time()) == ["models"], "꺼진 작업은 때가 돼도 줄에 안 선다"
    res = hk.run_job("idle_sessions")
    st = {j["id"]: j for j in hk.status()["jobs"]}
    assert st["idle_sessions"]["last_ok"] is True and st["idle_sessions"]["last_summary"] == res["summary"]
    assert hk.kick("models") and hk.kick("models") and list(hk._q.queue).count("models") == 1
    assert not hk.kick("nope")


def test_job_failure_is_recorded(terms, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setitem(hk._RUNNERS, "models", boom)
    hk.run_job("models")
    st = {j["id"]: j for j in hk.status()["jobs"]}
    assert st["models"]["last_ok"] is False and "RuntimeError" in st["models"]["last_summary"]


def test_start_and_reaper_share_session_lock(terms):
    """누가 세션 잠금을 잡고 있으면(띄우는 중) 정리의 stop_if 는 기다렸다가 다시 확인한다."""
    terms("w")
    order = []
    with webterm.session_lock("w"):
        t = threading.Thread(target=lambda: order.append(webterm.stop_if("w", lambda a: True)))
        t.start()
        time.sleep(0.2)
        order.append("held")
    t.join(2)
    assert order == ["held", "stopped"]
    assert webterm.stop_if("w", lambda a: True) == "gone"


def test_models_merge(fake_claude_home, monkeypatch):
    monkeypatch.setattr("session_manager.cp_client.fetch_models",
                        lambda ttl=3600: [{"id": "claude-opus-5", "label": "Opus 5"}])
    metas = [NS(last_model="claude-opus-5-5", mtime=200), NS(last_model="claude-opus-5", mtime=300),
             NS(last_model="claude-haiku-4-5-20251001", mtime=100), NS(last_model="<synthetic>", mtime=400),
             NS(last_model=None, mtime=1)]
    r = hk.refresh_models(metas)
    assert r["added"] == ["claude-opus-5-5", "claude-haiku-4-5-20251001"]
    ids = [m["id"] for m in hk.saved_models()["models"]]
    assert ids == ["claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-opus-5"]
    assert hk.model_label("claude-opus-5-5") == "Opus 5.5"
    assert hk.model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert hk.model_label("claude-fable-5-1") == "Fable 5.1" and hk.model_label("claude-opus-5") == "Opus 5"
    monkeypatch.setattr("session_manager.cp_client.fetch_models", lambda ttl=3600: None)   # CP 불통 → 내장 + 실사용
    r2 = hk.refresh_models([NS(last_model="claude-zeta-9", mtime=1)])
    assert r2["source"] == "builtin" and hk.saved_models()["models"][0]["id"] == "claude-zeta-9"


def test_api_and_remote_privilege(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(hk, "start", lambda *a, **k: None)
    from session_manager.server import create_app
    from session_manager import labels
    with TestClient(create_app()) as c:
        st = c.get("/api/owner/housekeeping").json()
        assert {j["id"] for j in st["jobs"]} == {"idle_sessions", "models"} and st["idle_hours"] == 4
        assert st["builtin"] and st["idle_candidates"] == []
        assert c.post("/api/owner/housekeeping", json={"hours": 8}).json()["idle_hours"] == 8
        assert c.post("/api/owner/housekeeping", json={"hours": 5}).status_code == 400
        assert c.post("/api/owner/housekeeping/models/run").json() == {"queued": "models"}
        assert c.post("/api/owner/housekeeping/zzz/run").status_code == 404
        assert c.post("/api/sessions/abc/keep", json={"keep": True}).json()["keep"] is True
        assert labels.get("abc").get("keep") == "1"
        c.post("/api/sessions/abc/keep", json={"keep": False})
        assert not labels.get("abc").get("keep")
        hk.refresh_models([])
        assert c.get("/api/models").json()["source"] == "merged"
    while not hk._q.empty():
        hk._q.get_nowait()
    assert connector._is_privileged_api("/api/owner/housekeeping")
    assert connector._is_privileged_api("/api/owner/housekeeping/idle_sessions/run")
    assert not connector._is_privileged_api("/api/owner/housekeeping/models/run")
