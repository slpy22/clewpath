"""수동 갈아타기 상태기계(session_manager/handoff.py, docs/designs/agent-unit.md)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from session_manager import config, handoff, launch_options, lifecycle, native_title, scanner, session_meta, team, webapi, webterm


@pytest.fixture
def H(fake_claude_home, monkeypatch, tmp_path):
    known = {s: SimpleNamespace(session_id=s, headless=False, cwd=str(tmp_path), jsonl_path=str(tmp_path / f"{s}.jsonl"), last_model="claude-haiku-4", custom_title=None, agent_name=None, ai_title=None, slug=None)
             for s in ("s1", "s2", "s3")}
    for m in known.values():
        open(m.jsonl_path, "w", encoding="utf-8").write("x")
    live: dict[str, str] = {}
    env = SimpleNamespace(known=known, live=live, ask=None, started=[], stopped=[], est=0.5, status=None, push=[], tmp=tmp_path, start_err=None)
    monkeypatch.setattr(scanner, "scan_one", lambda sid: known.get(sid))
    monkeypatch.setattr(team, "_latest_session", lambda sid, one_hop=False: sid)
    monkeypatch.setattr(team, "_peer_by_session", lambda: {})
    monkeypatch.setattr(lifecycle, "_live_reason_raw", lambda sid: live.get(sid))
    monkeypatch.setattr(native_title, "session_status", lambda sid: env.status)
    monkeypatch.setattr(config, "claude_exe", lambda: "C:/x/claude.exe")
    monkeypatch.setattr(webapi, "estimate_resume_usd", lambda sid, cap: env.est)
    monkeypatch.setattr(handoff, "_ask", lambda sid, q: env.ask(sid, q))
    monkeypatch.setattr(handoff, "_spawn", lambda hid, gen, sid, q, rev: handoff._run(hid, gen, sid, q, rev))     # 동기로
    monkeypatch.setattr(handoff, "_settle_soon", lambda hid: None)
    monkeypatch.setattr(handoff, "_trusted", lambda cwd: True)
    monkeypatch.setattr(handoff, "_notify", lambda hid, what: env.push.append(what))

    def start_new(cwd, prompt, session_id=None, launch=None, source="api", **k):
        if env.start_err:
            raise webterm.TermStartError(env.start_err, "x", "")
        env.started.append(dict(cwd=cwd, prompt=prompt, sid=session_id, spec=launch, source=source))
        live[session_id] = "terminal"
        return {"status": "started", "session_id": session_id}
    monkeypatch.setattr(webterm, "start_new", start_new)
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: live.get(sid) == "terminal")
    monkeypatch.setattr(webterm, "stop_terminal", lambda sid: env.stopped.append(sid) or True)
    env.ask = lambda sid, q: {"answer": "목표: 시험\n남은 일: 없음", "is_error": False, "cost_usd": 0.42}
    env.agent = team.create_agent("s1", "가", role="역")
    env.id = env.agent["id"]
    return env


def _err(fn, *a, **kw):
    with pytest.raises(team.TeamError) as e:
        fn(*a, **kw)
    return e.value


def _ready(H, **kw):
    s = handoff.start(H.id, **kw)
    assert s["status"] == "ready", s
    return s


def _go(H, s, **kw):
    return handoff.confirm(s["id"], s["draft_rev"], s["snapshot_token"], **kw)


def _rec(H, r):
    """새 세션의 기록 파일이 생긴 상태로 만든다."""
    H.known[r["to_session"]] = SimpleNamespace(session_id=r["to_session"], headless=False, cwd=str(H.tmp), jsonl_path=str(H.tmp / "n.jsonl"), last_model=None)


# ---------------------------------------------------------------- 시작 조건
def test_clone_makes_ready_draft_and_records_cost(H):
    s = _ready(H)
    assert s["draft"].startswith("목표") and s["draft_rev"] == 1 and s["attempts"][0]["actual_usd"] == 0.42 and H.push == ["ready"]


@pytest.mark.parametrize("setup,code", [
    (lambda H: H.live.update(s1="busy"), "live_elsewhere"),
    (lambda H: H.live.update(s1="idle"), "live_elsewhere"),
])
def test_refuses_session_running_elsewhere(H, setup, code):
    setup(H)
    assert _err(handoff.start, H.id).code == code
    assert not H.started


def test_refuses_generating_own_terminal(H):
    H.live["s1"] = "terminal"
    H.status = "busy"
    assert _err(handoff.start, H.id).code == "session_generating"


def test_refuses_cmd_wrapper_before_spending(H, monkeypatch):
    monkeypatch.setattr(config, "claude_exe", lambda: "C:/npm/claude.cmd")
    H.ask = lambda *a: pytest.fail("돈이 들기 전에 막아야 한다")
    assert _err(handoff.start, H.id).code == "cmd_wrapper_unsupported"


def test_one_in_flight_per_agent(H):
    _ready(H)
    e = _err(handoff.start, H.id)
    assert (e.code, e.status) == ("handoff_in_progress", 409)


def test_clone_failure_changes_nothing(H):
    H.ask = lambda *a: {"answer": "", "is_error": True, "cost_usd": None}
    s = handoff.start(H.id)
    assert s["status"] == "failed" and s["error"] == "clone_failed" and H.push == ["failed"]
    assert team.current_session(H.id) == "s1", "실패해도 묶음은 그대로"


def test_manual_goes_ready_without_clone(H):
    H.ask = lambda *a: pytest.fail("직접 쓰기는 복제본을 돌리지 않는다")
    s = _ready(H, mode="manual")
    assert s["draft"] == "" and s["attempts"] == []


def test_secrets_masked_before_saving(H):
    H.ask = lambda *a: {"answer": "키 sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 끝", "is_error": False, "cost_usd": 0.1}
    assert "abcdefghijklmnop" not in _ready(H)["draft"]


# ---------------------------------------------------------------- 비용(D6·E5·E6)
@pytest.mark.parametrize("est,want", [(6.0, 6.0), (None, -1)])
def test_cost_confirm_before_spending(H, est, want):
    H.est = est
    H.ask = lambda *a: pytest.fail("확인 전에 돌면 안 된다")
    e = _err(handoff.start, H.id)
    assert e.code == "cost_confirm_required" and e.extra["cumulative_usd"] == want


def test_cost_confirm_is_bound_to_that_amount(H):
    H.est = 6.0
    assert _err(handoff.start, H.id, confirm_usd=5.5).code == "cost_confirm_required"
    assert _ready(H, confirm_usd=6.0)["status"] == "ready"


def test_resummary_counts_cumulative_cost(H):
    H.est = 3.0
    s = _ready(H)                                            # 실제 0.42
    H.est = 4.8
    e = _err(handoff.resummary, s["id"])
    assert e.code == "cost_confirm_required" and e.extra["cumulative_usd"] == 5.22
    r = handoff.resummary(s["id"], confirm_usd=5.22)
    assert len(r["attempts"]) == 2


def test_phone_daily_threshold_needs_2fa(H, monkeypatch):
    monkeypatch.setattr(handoff, "_phone_limit", lambda: 0.3)
    e = _err(handoff.start, H.id, origin="phone")
    assert (e.code, e.status) == ("phone_daily_limit", 403)
    assert _ready(H, origin="phone", two_factor=True)


def test_unknown_actual_cost_counts_estimate(H):
    H.ask = lambda *a: {"answer": "본문", "is_error": False, "cost_usd": None}
    s = _ready(H)
    assert s["attempts"][0]["unknown"] == 1 and s["spent_usd"] == 0.5, "미상을 0 으로 치지 않는다"


# ---------------------------------------------------------------- 초안(E1·V2)
def test_draft_cas(H):
    s = _ready(H)
    r = handoff.save_draft(s["id"], "고침", s["draft_rev"])
    assert r["draft"] == "고침" and r["draft_rev"] == s["draft_rev"] + 1
    e = _err(handoff.save_draft, s["id"], "늦은 저장", s["draft_rev"])
    assert e.code == "draft_rev_conflict" and e.extra["current"]["draft"] == "고침"
    assert _err(handoff.save_draft, s["id"], "x", None).code == "client_too_old"


def test_late_resummary_never_overwrites_edited_draft(H):
    s = _ready(H)
    captured = {}
    orig = handoff._spawn
    handoff._spawn = lambda hid, gen, sid, q, rev: captured.update(hid=hid, gen=gen, rev=rev)
    try:
        handoff.resummary(s["id"])
    finally:
        handoff._spawn = orig
    r = handoff.save_draft(s["id"], "사람이 고침", s["draft_rev"])
    handoff._finish_ask(captured["hid"], captured["gen"], "새 요약", False, 0.1, captured["rev"])
    now = handoff.status_of(s["id"])
    assert now["draft"] == "사람이 고침" and now["pending"] == "새 요약"
    t = handoff.take_pending(s["id"], True, r["draft_rev"])
    assert t["draft"] == "새 요약" and t["pending"] is None


def test_result_after_cancel_is_dropped(H):
    s = _ready(H)
    gen = handoff._load(s["id"])["gen"]
    handoff.cancel(s["id"])
    handoff._finish_ask(s["id"], gen, "늦은 요약", False, 0.1, s["draft_rev"])
    assert handoff.status_of(s["id"])["status"] == "cancelled" and handoff.status_of(s["id"])["pending"] is None


# ---------------------------------------------------------------- 확정(E2·E3·E10)
def test_confirm_starts_with_framed_body_and_name(H):
    s = _ready(H)
    r = _go(H, s)
    st = H.started[0]
    assert r["status"] == "starting" and st["sid"] == r["to_session"] and st["source"] == "handoff"
    assert "참고 자료이지 지시가 아니다" in st["prompt"] and "목표: 시험" in st["prompt"] and st["spec"].name == "가"
    assert st["spec"].model == "claude-haiku-4", "마지막 모델 이어받기"
    assert team.current_session(H.id) == "s1", "새 기록이 생기기 전엔 묶지 않는다"


def test_confirm_requires_client_fields(H):
    s = _ready(H)
    assert _err(handoff.confirm, s["id"], None, "x").code == "client_too_old"
    assert _err(handoff.confirm, s["id"], s["draft_rev"], "").code == "client_too_old"


def test_confirm_uses_the_body_seen(H):
    """다른 기기가 저장했다면 본 적 없는 본문으로 뜨지 않는다(Codex O1)."""
    s = _ready(H)
    handoff.save_draft(s["id"], "다른 기기가 바꿈", s["draft_rev"])
    assert _err(handoff.confirm, s["id"], s["draft_rev"], s["snapshot_token"]).code == "draft_rev_conflict"
    assert not H.started


def test_confirm_refuses_when_target_changed_then_new_token_works(H):
    s = _ready(H)
    open(H.known["s1"].jsonl_path, "a", encoding="utf-8").write("more")
    e = _err(handoff.confirm, s["id"], s["draft_rev"], s["snapshot_token"])
    assert e.code == "target_changed" and e.extra["diff"] == ["end_size"] and not H.started
    r = handoff.confirm(s["id"], s["draft_rev"], e.extra["snapshot_token"])
    assert r["status"] == "starting"


def test_confirm_twice_starts_once(H):
    s = _ready(H)
    _go(H, s)
    r = _go(H, s)
    assert r["status"] == "starting" and len(H.started) == 1


def test_draft_too_long_refused_not_truncated(H):
    s = _ready(H)
    long = "가" * (s["body_max"] + 1)
    t = handoff.save_draft(s["id"], long, s["draft_rev"])
    e = _err(handoff.confirm, s["id"], t["draft_rev"], t["snapshot_token"])
    assert e.code == "draft_too_long" and e.extra["body_max"] == s["body_max"]
    t2 = handoff.save_draft(s["id"], "가" * s["body_max"], t["draft_rev"])
    assert handoff.confirm(s["id"], t2["draft_rev"], t2["snapshot_token"])["status"] == "starting"
    assert len(H.started[0]["prompt"]) <= launch_options.PROMPT_MAX


def test_body_max_shrinks_with_frame(H):
    a = team._read().execute("SELECT * FROM agents WHERE id=?", (H.id,)).fetchone()
    assert handoff.body_max(a) == launch_options.PROMPT_MAX - len(handoff._frame(a, ""))


def test_new_session_inherits_stored_launch_settings(H, tmp_path):
    spec = launch_options.LaunchSpec(permission_mode="plan", effort="high", tools=["Read"], add_dirs=[str(tmp_path)], model="m-x")
    launch_options.save("s1", spec, "prompt")
    s = _ready(H)
    _go(H, s)
    got = H.started[0]["spec"]
    assert (got.permission_mode, got.effort, got.tools, got.add_dirs, got.model) == ("plan", "high", ["Read"], [str(tmp_path)], "m-x")


def test_user_chosen_options_override(H):
    s = _ready(H)
    _go(H, s, options={"model": "opus", "permission_mode": "bypassPermissions"})
    assert H.started[0]["spec"].model == "opus" and H.started[0]["spec"].permission_mode == "bypassPermissions"


def test_new_folder_is_move_folder(H, tmp_path):
    d = tmp_path / "새폴더"
    d.mkdir()
    s = _ready(H)
    r = _go(H, s, cwd=str(d))
    assert r["kind"] == "move_folder" and H.started[0]["cwd"] == str(d)


def test_start_failure_marks_failed_old_kept(H):
    H.start_err = "cap"
    s = _ready(H)
    r = _go(H, s)
    assert r["status"] == "failed" and r["error"] == "cap" and team.current_session(H.id) == "s1" and not H.stopped


# ---------------------------------------------------------------- 마무리·복구(D8·X6·V1)
def test_settle_binds_when_record_appears_and_stops_old(H):
    H.live["s1"] = None
    s = _ready(H)
    H.live["s1"] = "terminal"
    r = _go(H, s)
    assert handoff.settle(r["id"]) == "starting", "기록 전엔 기다린다"
    _rec(H, r)
    assert handoff.settle(r["id"]) == "done"
    assert team.current_session(H.id) == r["to_session"] and H.stopped == ["s1"]
    h = team.agent_detail(H.id)["history"]
    assert [x["reason"] for x in h] == ["replaced", "created"] and h[1]["ended"]
    assert handoff.settle(r["id"]) == "done", "멱등"
    assert len(team.agent_detail(H.id)["history"]) == 2


def test_declined_trust_fails_and_keeps_old_session(H, monkeypatch):
    s = _ready(H)
    r = _go(H, s)
    H.live.pop(r["to_session"])
    assert handoff.settle(r["id"]) == "starting", "유예 안에는 기다린다"
    monkeypatch.setattr(handoff, "START_GRACE", -1)
    assert handoff.settle(r["id"]) == "failed"
    assert handoff.status_of(r["id"])["error"] == "session_exited" and team.current_session(H.id) == "s1" and not H.stopped


def test_tick_finishes_starting_after_restart(H):
    s = _ready(H)
    r = _go(H, s)
    _rec(H, r)
    handoff.tick()
    assert handoff.status_of(r["id"])["status"] == "done"


def test_tick_fails_orphaned_preparing(H, monkeypatch):
    sync = handoff._spawn
    monkeypatch.setattr(handoff, "_spawn", lambda *a: None)
    s = handoff.start(H.id)
    assert s["status"] == "preparing"
    handoff.tick()
    assert handoff.status_of(s["id"])["status"] == "preparing", "도는 중이면 건드리지 않는다"
    handoff._jobs.clear()                                  # Host 재기동 흉내
    handoff.tick()
    assert handoff.status_of(s["id"])["error"] == "prepare_lost", "주인 잃은 preparing 은 failed(재시도가 막히지 않게)"
    monkeypatch.setattr(handoff, "_spawn", sync)
    assert _ready(H)


def test_resummary_double_click_runs_one_clone(H, monkeypatch):
    s = _ready(H)
    runs = []
    monkeypatch.setattr(handoff, "_spawn", lambda *a: runs.append(a))
    handoff.resummary(s["id"])
    assert _err(handoff.resummary, s["id"]).code == "job_running" and len(runs) == 1
    handoff._jobs.clear()


def test_resummary_refusal_releases_job(H):
    s = _ready(H)
    H.est = 9.0
    _err(handoff.resummary, s["id"])
    assert s["id"] not in handoff._jobs, "거절되면 자리를 돌려준다 — 안 그러면 영영 job_running"


# ---------------------------------------------------------------- 서로 막기·표시
def test_in_progress_blocks_delete_move_rename(H, monkeypatch):
    s = _ready(H)
    monkeypatch.setattr(lifecycle, "_live_reason_raw", lambda sid: None)
    assert handoff.in_progress("s1") and lifecycle._live_reason("s1") == "handoff"
    assert native_title.set_native_title("s1", "새이름")["error"] == "handoff_in_progress"
    handoff.cancel(s["id"])
    assert not handoff.in_progress("s1") and lifecycle._live_reason("s1") is None


def test_summary_and_replaced_map(H):
    s = _ready(H)
    assert [(x["id"], x["status"], x["agent_name"]) for x in handoff.summary()] == [(s["id"], "ready", "가")]
    r = _go(H, s)
    _rec(H, r)
    handoff.settle(r["id"])
    assert handoff.summary() == []
    assert team.agents_overview()["by_session"]["s1"]["replaced_by"] == r["to_session"]


def test_assistant_uses_reassign(H, monkeypatch):
    monkeypatch.setattr(handoff, "_is_assistant", lambda aid: True)
    assert _err(handoff.start, H.id).code == "assistant_uses_reassign"


def test_precheck_reports_blocker_and_defaults(H):
    p = handoff.precheck(H.id)
    assert p["blocked"] is None and p["est_usd"] == 0.5 and p["model"] == "claude-haiku-4" and p["body_max"] > 5000
    H.live["s1"] = "idle"
    assert handoff.precheck(H.id)["blocked"] == "live_elsewhere"


def test_no_claude_file_written(H):
    """복제본·저장은 ClewPath 데이터 폴더에만 쓴다(불가침)."""
    before = open(H.known["s1"].jsonl_path, encoding="utf-8").read()
    s = _ready(H)
    handoff.save_draft(s["id"], "x", s["draft_rev"])
    assert open(H.known["s1"].jsonl_path, encoding="utf-8").read() == before
    assert str(config.data_dir()) in handoff._load(s["id"])["doc_path"]


def test_failed_can_be_dismissed(H):
    H.ask = lambda *a: {"answer": "", "is_error": True, "cost_usd": None}
    s = handoff.start(H.id)
    assert [x["status"] for x in handoff.summary()] == ["failed"]
    assert handoff.cancel(s["id"])["status"] == "cancelled" and handoff.summary() == []


def test_missing_add_dir_is_dropped(H, tmp_path):
    gone = tmp_path / "없음"
    gone.mkdir()
    launch_options.save("s1", launch_options.LaunchSpec(add_dirs=[str(gone), str(tmp_path)]), "p")
    gone.rmdir()
    s = _ready(H)
    _go(H, s)
    assert H.started[0]["spec"].add_dirs == [str(tmp_path)]


def test_delete_and_move_preview_name_the_agent(H):
    from session_manager import session_ops
    H.live["s1"] = None
    _, text = session_ops.prepare("sessions_delete", {"ids": ["s1"]})
    assert "에이전트 가 의 지금 세션" in text


def test_preview_refuses_with_handoff_reason(H):
    from session_manager import session_ops
    _ready(H)
    assert session_ops.preview_delete(["s1"])["items"][0]["reason"] == "handoff_in_progress"


def test_list_response_and_routes_are_guarded():
    from session_manager import outer_door
    for p in ("/api/owner/agents/{agent_id}/handoff", "/api/owner/handoffs/{hid}/confirm-handoff", "/api/owner/handoffs/{hid}/draft", "/api/owner/handoffs/{hid}/resummary"):
        assert p in outer_door.GUARDED, "Claude 세션이 스스로 갈아타면 G2(늘 수동)가 깨진다"
    from session_manager.connector import _PRIV_API_SUFFIXES
    assert "/confirm-handoff" in _PRIV_API_SUFFIXES and "/handoff-2fa" in _PRIV_API_SUFFIXES


def test_no_silent_modification_of_claude_files_on_handoff():
    """[원칙] 갈아타기는 claude 파일을 쓰지 않는다 — 복제본은 isolated, 이름은 claude -n 이 스스로 남긴다(CLAUDE.md 승인된 예외)."""
    import inspect
    src = inspect.getsource(handoff)
    for bad in ("custom-title", "append_title", "set_native_title", "claude_home", "projects_dir", "\"a\", encoding"):
        assert bad not in src, bad
    assert "isolated=True" in inspect.getsource(handoff._ask)


def test_push_payload_carries_room(monkeypatch):
    """폰은 PC 를 릴레이 방으로 안다 — 모든 알림에 방이 실려야 맞는 PC 로 연다(E9)."""
    import threading
    from session_manager import push
    got, done = {}, threading.Event()
    monkeypatch.setattr(push, "_load_subs", lambda: [{"endpoint": "x"}])
    monkeypatch.setattr(push, "_webpush_send", lambda sub, payload: (got.update(json.loads(payload)), done.set()))
    push.set_room("room-1")
    try:
        assert push.send("handoff-ready", "ho_room", "t", "b", {"handoff": "ho_room"}) == 1
        assert done.wait(5)
    finally:
        push.set_room(None)
    assert got["room"] == "room-1" and got["handoff"] == "ho_room" and "draft" not in got
