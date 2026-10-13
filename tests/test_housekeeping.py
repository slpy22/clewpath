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
    terms("viewing", client=("ws", None))              # 화면만 붙어 있고 입력·대화 없음 → 대상(밤새 열어 둔 탭)
    terms("watching", client=("ws", None), last_attach=now - 900)   # 15분 전에 연 화면 → 최근
    terms("perm")
    _RT["perm"] = {"phase": "permission", "permission_at": now - 5 * 3600}
    terms("kept")
    labels.set_record("kept", keep="1")
    terms("fork", persist=False)                       # 일회용 포크는 대상 아님
    terms("hookrecent")
    _RT["hookrecent"] = {"phase": "ready", "ready_at": now - 1800}
    terms("justadopted", started=now - 60)             # 이 Host 가 방금 다시 붙임(업데이트 직후)
    assert sorted(c["session_id"] for c in hk.idle_candidates(now)) == ["idle1", "viewing"]
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
    assert hk._due(time.time()) == ["models", "worklog", "cleanup_watch"], "꺼진 작업은 때가 돼도 줄에 안 선다"
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
    r = hk.refresh_models(metas, probe=False)
    assert r["added"] == ["claude-opus-5-5", "claude-haiku-4-5-20251001"]
    ids = [m["id"] for m in hk.saved_models()["models"]]
    assert ids == ["claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-opus-5"]
    assert hk.model_label("claude-opus-5-5") == "Opus 5.5"
    assert hk.model_label("claude-haiku-4-5-20251001") == "Haiku 4.5"
    assert hk.model_label("claude-fable-5-1") == "Fable 5.1" and hk.model_label("claude-opus-5") == "Opus 5"
    monkeypatch.setattr("session_manager.cp_client.fetch_models", lambda ttl=3600: None)   # CP 불통 → 내장 + 실사용
    r2 = hk.refresh_models([NS(last_model="claude-zeta-9", mtime=1)], probe=False)
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
        assert {j["id"] for j in st["jobs"]} == {"idle_sessions", "models", "worklog", "cleanup_watch"} and st["idle_hours"] == 4
        assert st["builtin"] and st["idle_candidates"] == []
        assert c.post("/api/owner/housekeeping", json={"hours": 8}).json()["idle_hours"] == 8
        assert c.post("/api/owner/housekeeping", json={"hours": 5}).status_code == 400
        assert c.post("/api/owner/housekeeping/models/run").json() == {"queued": "models"}
        assert c.post("/api/owner/housekeeping/zzz/run").status_code == 404
        assert c.post("/api/sessions/abc/keep", json={"keep": True}).json()["keep"] is True
        assert labels.get("abc").get("keep") == "1"
        c.post("/api/sessions/abc/keep", json={"keep": False})
        assert not labels.get("abc").get("keep")
        hk.refresh_models([], probe=False)
        assert c.get("/api/models").json()["source"] == "merged"
    while not hk._q.empty():
        hk._q.get_nowait()
    assert connector._is_privileged_api("/api/owner/housekeeping")
    assert connector._is_privileged_api("/api/owner/housekeeping/idle_sessions/run")
    assert not connector._is_privileged_api("/api/owner/housekeeping/models/run")


def test_models_merge_with_picker(fake_claude_home, monkeypatch):
    """claude /model 화면 목록이 맨 앞(화면 순서), 이름이 같으면 알려진 id(Haiku 4.5 → 날짜 붙은 id), 그다음 실사용 · 운영측."""
    monkeypatch.setattr("session_manager.cp_client.fetch_models",
                        lambda ttl=3600: [{"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
                                          {"id": "claude-opus-4-8", "label": "Opus 4.8"}])
    pick = [{"label": "Opus 5.5", "family": "Opus", "ver": "5.5", "one_m": False},
            {"label": "Haiku 4.5", "family": "Haiku", "ver": "4.5", "one_m": False},
            {"label": "Fable 5.1 (1M)", "family": "Fable", "ver": "5.1", "one_m": True}]
    monkeypatch.setattr(hk, "_maybe_probe", lambda d, force=False: ({"ok_models": pick}, "화면 목록 3개"))
    r = hk.refresh_models([NS(last_model="claude-zeta-9", mtime=1)])
    ids = [m["id"] for m in hk.saved_models()["models"]]
    assert ids == ["claude-opus-5-5", "claude-haiku-4-5-20251001", "claude-fable-5-1[1m]", "claude-zeta-9", "claude-opus-4-8"]
    assert "화면 목록 3개" in r["summary"]


def test_maybe_probe_runs_on_version_change_only(fake_claude_home, monkeypatch):
    from session_manager import modelprobe
    calls = []
    monkeypatch.setattr("session_manager.config.claude_exe", lambda: "claude.exe")
    monkeypatch.setattr(modelprobe, "pick_cwd", lambda: "E:/t")
    ver = {"v": "2.1.296"}
    monkeypatch.setattr(modelprobe, "claude_version", lambda exe: ver["v"])

    def fake_probe(exe, cwd):
        calls.append(cwd)
        return {"ok": True, "models": [{"label": "Opus 5.5", "family": "Opus", "ver": "5.5", "one_m": False}],
                "session_id": f"sid{len(calls)}"}
    monkeypatch.setattr(modelprobe, "probe", fake_probe)
    d = {}
    pr, note = hk._maybe_probe(d)
    assert len(calls) == 1 and "새로 읽음" in note and d["probe_sessions"] == ["sid1"]
    pr, note = hk._maybe_probe(d)
    assert len(calls) == 1 and "다시 읽을 때 아님" in note, "같은 버전·7일 안이면 안 띄운다"
    ver["v"] = "2.1.300"
    hk._maybe_probe(d)
    assert len(calls) == 2, "claude 가 업데이트되면 다시 읽는다"
    monkeypatch.setattr(modelprobe, "probe", lambda exe, cwd: {"ok": False, "models": [], "session_id": "sid9", "error": "no_picker"})
    ver["v"] = "2.1.301"
    pr, note = hk._maybe_probe(d)
    assert "실패" in note and pr["ok_models"][0]["label"] == "Opus 5.5", "실패하면 이전 목록 유지"
    hk._save(d)
    assert hk.hidden_session_ids() == {"sid1", "sid2", "sid9"}


def test_idle_uses_conversation_time_not_file_mtime(terms, fake_claude_home):
    """claude 는 쉬는 동안에도 jsonl 끝에 시각 없는 메타 줄을 다시 써서 mtime 을 새로 만든다(2026-10-13 실측) —
    판정은 마지막 대화 줄(user·assistant·작업 알림) 시각으로. 자동 압축 요약·system 줄은 활동이 아니다."""
    import json, os
    from datetime import datetime, timezone
    from session_manager import scanner
    now = time.time()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")
    sid = "11111111-2222-3333-4444-555555555555"
    d = fake_claude_home / "projects" / "E--x"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "timestamp": iso(now - 6 * 3600), "cwd": "E:/x", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": iso(now - 6 * 3600 + 5), "message": {"role": "assistant", "model": "m", "content": []}},
            {"type": "user", "timestamp": iso(now - 3600), "isCompactSummary": True, "message": {"role": "user", "content": "요약"}},
            {"type": "system", "subtype": "informational", "timestamp": iso(now - 3600), "content": "Compacted while idle"},
            {"type": "last-prompt"}, {"type": "mode"}, {"type": "atis-latch"}]
    f = d / f"{sid}.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    os.utime(f, (now - 60, now - 60))                  # 파일은 1분 전에 수정됨
    scanner.invalidate(str(f))
    assert scanner._epoch(scanner.scan_one(sid).last_activity_at) == pytest.approx(now - 6 * 3600 + 5, abs=1)
    terms(sid)
    got = [c for c in hk.idle_candidates(now) if c["session_id"] == sid]
    assert got and got[0]["idle_s"] >= 6 * 3600 - 10


def test_cleanup_watch_warns_once_before_claude_deletes(fake_claude_home, monkeypatch):
    """claude 는 마지막 수정 뒤 cleanupPeriodDays(기본 30일)가 지난 기록을 지운다(2026-10-13 E:\001_KIS 3개 실종).
    3일 안에 지워질 세션을 세션마다 한 번만 폰으로 알린다. 설정 파일은 읽기만."""
    import json
    from session_manager import retention
    sent = []
    monkeypatch.setattr("session_manager.push.send", lambda *a, **k: sent.append(a))
    now = time.time()
    day = 86400
    m = lambda sid, age, **kw: NS(session_id=sid, mtime=now - age * day, cwd="E:/k", custom_title=sid, ai_title=None,
                                  slug=None, headless=kw.get("headless", False))
    metas = [m("old28", 28), m("old20", 20), m("fresh", 1), m("auto", 29, headless=True)]
    assert retention.period_days() == 30, "설정이 없으면 기본 30일"
    r = hk.watch_cleanup(metas, now)
    assert [c["session_id"] for c in r["soon"]] == ["old28"] and r["notified"] == ["old28"]
    assert len(sent) == 1 and "1개" in sent[0][2] and "old28" in sent[0][3]
    r = hk.watch_cleanup(metas, now)
    assert r["notified"] == [] and len(sent) == 1, "같은 세션은 한 번만 알린다"
    settings = fake_claude_home / "settings.json"
    before = None
    settings.write_text(json.dumps({"cleanupPeriodDays": 22}), encoding="utf-8")
    before = settings.read_text(encoding="utf-8")
    r = hk.watch_cleanup(metas, now)
    assert [c["session_id"] for c in r["soon"]] == ["old28", "old20"] and r["notified"] == ["old20"]
    assert "보관 22일" in r["summary"]
    settings.write_text(json.dumps({"cleanupPeriodDays": 0}), encoding="utf-8")
    assert retention.period_days() is None and hk.watch_cleanup(metas, now)["soon"] == []
    settings.write_text(before, encoding="utf-8")
    assert settings.read_text(encoding="utf-8") == before, "claude 설정은 읽기만"
    assert retention.delete_at(1000.0, 22) == 1000 + 22 * day
