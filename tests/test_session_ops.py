"""프롬프트로 세션 다루기 — Host 정책 층(docs/designs/session-control.md 3·4·11 절): 찾기·미리보기·승인 묶기·일괄 실행·재시도·새 세션."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from session_manager import approvals, launch_options as lo, lifecycle, native_title, scanner, session_ops as ops, team, webterm
from session_manager.pathenc import path_to_folder
from tests.conftest import write_session

A, B, C = (f"{c * 8}-1111-2222-3333-444444444444" for c in "abc")


@pytest.fixture
def home(fake_claude_home, monkeypatch, tmp_path):
    monkeypatch.setattr(approvals, "_now", approvals._now)
    from session_manager import push
    monkeypatch.setattr(push, "send", lambda *a, **k: 0)
    ops._creates.clear()
    cwd = str(tmp_path / "old")
    os.makedirs(cwd)
    for sid, title in ((A, "남북비교"), (B, "법령 수집"), (C, "화면 작업")):
        write_session(fake_claude_home, path_to_folder(cwd), sid,
                      [{"type": "user", "cwd": cwd, "message": {"role": "user", "content": "x"}, "timestamp": "2026-11-01T00:00:00Z"},
                       {"type": "custom-title", "customTitle": title, "sessionId": sid}])
    scanner._CACHE.clear()
    return fake_claude_home


def _live(monkeypatch, *ids):
    monkeypatch.setattr(lifecycle, "_live_reason", lambda sid: "idle" if sid in ids else None)


def _approve(a):
    return approvals.decide(a["id"], True, "otp")


# ---------------------------------------------------------------- 찾기·이름
def test_find_by_name_and_folder(home, tmp_path):
    r = ops.find("남북")
    assert [s["session_id"] for s in r["sessions"]] == [A] and r["sessions"][0]["title"] == "남북비교"
    assert ops.find(cwd=str(tmp_path / "old"))["total"] == 3
    assert ops.find(cwd=str(tmp_path / "없음"))["total"] == 0


def test_find_caps_results(home):
    r = ops.find(limit=2)
    assert len(r["sessions"]) == 2 and r["total"] == 3 and r["truncated"] is True


def test_rename_refuses_self_and_live(home, monkeypatch):
    with pytest.raises(team.TeamError) as e:
        ops.rename(A, "새 이름", caller=A)
    assert e.value.code == "self_rename_use_slash_rename"
    monkeypatch.setattr(native_title, "session_status", lambda sid: "idle")
    with pytest.raises(team.TeamError) as e:
        ops.rename(A, "새 이름", caller=B)
    assert e.value.code == "session_live" and e.value.status == 409


def test_rename_writes_title(home):
    assert ops.rename(A, "새 이름")["ok"] is True
    scanner._CACHE.clear()
    assert scanner.scan_one(A).custom_title == "새 이름"


@pytest.mark.parametrize("bucket", ["../x", "..", ".hidden", "a/b", "a\\b", ""])
def test_restore_refuses_path_tricks(home, bucket):
    with pytest.raises(team.TeamError) as e:
        ops.restore(bucket)
    assert e.value.code == "bad_bucket"


# ---------------------------------------------------------------- 미리보기
def test_move_preview_reports_each_item(home, monkeypatch, tmp_path):
    _live(monkeypatch, B)
    r = ops.preview_move([A, B, C, "없는세션", A], str(tmp_path / "new"), caller=C)
    by = {i["session_id"]: i for i in r["items"]}
    assert r["total"] == 4, "중복은 하나로"
    assert by[A]["ok"] and by[B]["reason"] == "session_live" and by[C]["reason"] == "self"
    assert by["없는세션"]["reason"] == "session_not_found" and r["ok"] == 1


@pytest.mark.parametrize("bad", ["200_nk_law_center", "", None, "new/folder"])
def test_move_refuses_relative_target(home, bad):
    """2026-11-06 사고: 드라이브 없는 경로."""
    with pytest.raises(team.TeamError) as e:
        ops.preview_move([A], bad)
    assert e.value.code == "target_not_absolute"


def test_move_preview_skips_sessions_already_there(home, tmp_path):
    assert ops.preview_move([A], str(tmp_path / "old"))["items"][0]["reason"] == "already_there"


def test_too_many_items_refused(home):
    with pytest.raises(team.TeamError) as e:
        ops.preview_delete([f"s{i}" for i in range(ops.MAX_ITEMS + 1)])
    assert e.value.code == "too_many_items"


# ---------------------------------------------------------------- 승인 묶기
def test_request_binds_server_side_facts_not_caller_text(home, tmp_path):
    """호출자가 보낸 제목·요약은 쓰지 않는다 — 폰 화면이 사실을 보여야 한다."""
    a = approvals.create("sessions_delete", {"items": [{"session_id": A, "title": "지워도 되는 시험"}]},
                         summary="안전한 정리입니다", requested_by="x")
    assert "남북비교" in a["summary"] and "안전한" not in a["summary"]
    assert a["args"]["items"][0]["title"] == "남북비교"


def test_request_drops_refused_items_and_needs_something(home, monkeypatch, tmp_path):
    _live(monkeypatch, A)
    r = ops.request("sessions_move", {"ids": [A, B], "new_cwd": str(tmp_path / "new")})
    assert [i["session_id"] for i in r["approval"]["args"]["items"]] == [B], "거절된 항목은 승인에 들어가지 않는다"
    assert r["status"] == "pending_approval"
    with pytest.raises(team.TeamError) as e:
        ops.request("sessions_delete", {"ids": [A]})
    assert e.value.code == "nothing_to_do"


def test_summary_counts_and_names(home, tmp_path):
    a = ops.request("sessions_move", {"ids": [A, B, C], "new_cwd": str(tmp_path / "new")})["approval"]
    assert a["summary"].startswith("세션 3개 이동") and str(tmp_path / "new") in a["summary"]


def test_not_executed_before_human_approval(home, tmp_path):
    a = ops.request("sessions_delete", {"ids": [A]})["approval"]
    r = ops.finish(a["id"])
    assert r["status"] == "pending" and scanner.scan_one(A) is not None
    with pytest.raises(team.TeamError):
        approvals.execute(a["id"])
    assert scanner.scan_one(A) is not None


def test_rejected_request_does_nothing(home):
    a = ops.request("sessions_delete", {"ids": [A]})["approval"]
    approvals.decide(a["id"], False, "otp")
    assert ops.finish(a["id"])["status"] == "rejected" and scanner.scan_one(A) is not None


def test_finish_only_for_session_kinds(home):
    a = approvals.create("team_archive", {"team": "T"})
    with pytest.raises(team.TeamError) as e:
        ops.finish(a["id"])
    assert e.value.code == "bad_kind"


# ---------------------------------------------------------------- 일괄 실행
def test_batch_move_end_to_end(home, tmp_path):
    new = str(tmp_path / "new")
    a = ops.request("sessions_move", {"ids": [A, B], "new_cwd": new})["approval"]
    _approve(a)
    r = ops.finish(a["id"])
    assert r["status"] == "executed" and r["result"]["done"] == 2 and r["result"]["failed"] == 0
    scanner._CACHE.clear()
    assert scanner.scan_one(A).cwd == os.path.normpath(new) and scanner.scan_one(B).cwd == os.path.normpath(new)
    assert scanner.scan_one(C).cwd == str(tmp_path / "old"), "승인에 없는 세션은 그대로"


def test_batch_delete_gives_bucket_and_restores(home):
    a = ops.request("sessions_delete", {"ids": [A]})["approval"]
    _approve(a)
    r = ops.finish(a["id"])
    scanner._CACHE.clear()
    assert scanner.scan_one(A) is None
    ops.restore(r["result"]["items"][A]["bucket"])
    scanner._CACHE.clear()
    assert scanner.scan_one(A) is not None


def test_session_turning_live_after_approval_fails_only_that_item(home, monkeypatch, tmp_path):
    a = ops.request("sessions_delete", {"ids": [A, B]})["approval"]
    _approve(a)
    _live(monkeypatch, A)                     # 승인 뒤에 터미널이 열렸다
    r = ops.finish(a["id"])
    assert r["result"]["items"][A] == {"state": "failed", "reason": "session_live"}
    assert r["result"]["items"][B]["state"] == "done" and r["result"]["done"] == 1 and r["result"]["failed"] == 1
    scanner._CACHE.clear()
    assert scanner.scan_one(A) is not None and scanner.scan_one(B) is None


def test_item_crash_does_not_stop_the_rest(home, monkeypatch):
    a = ops.request("sessions_delete", {"ids": [A, B]})["approval"]
    _approve(a)
    real = lifecycle.delete_session

    def flaky(sid, **k):
        if sid == A:
            raise OSError("파일 잠김")
        return real(sid, **k)
    monkeypatch.setattr(lifecycle, "delete_session", flaky)
    r = ops.finish(a["id"])
    assert r["result"]["items"][A] == {"state": "failed", "reason": "OSError"} and r["result"]["items"][B]["state"] == "done"


def test_same_approval_twice_applies_once(home, monkeypatch):
    a = ops.request("sessions_delete", {"ids": [A]})["approval"]
    _approve(a)
    calls = []
    real = lifecycle.delete_session
    monkeypatch.setattr(lifecycle, "delete_session", lambda sid, **k: calls.append(sid) or real(sid, **k))
    first = ops.finish(a["id"])
    second = ops.finish(a["id"])
    assert calls == [A] and first["result"] == second["result"]


def test_retry_after_host_death_skips_done_and_judges_doing(home, monkeypatch, tmp_path):
    """Host 가 일괄 도중에 죽음: 끝난 항목은 건너뛰고, doing 은 실제 파일 상태로 판정한다."""
    a = ops.request("sessions_delete", {"ids": [A, B, C]})["approval"]
    _approve(a)
    aid = a["id"]
    assert lifecycle.delete_session(B, dry_run=False)["recoverable"]      # B 는 지운 뒤 기록 전에 죽음
    scanner._CACHE.clear()
    with team._Tx() as c:
        c.execute("UPDATE approvals SET status='executing' WHERE id=?", (aid,))
    ops._save(aid, {A: {"state": "done"}, B: {"state": "doing"}, C: {"state": "doing"}})
    with pytest.raises(team.TeamError) as e:
        approvals.execute(aid)
    assert e.value.code == "executing", "기동 복구 전에는 동시 실행 차단"
    assert ops.recover() == 1
    calls = []
    real = lifecycle.delete_session
    monkeypatch.setattr(lifecycle, "delete_session", lambda sid, **k: calls.append(sid) or real(sid, **k))
    r = ops.finish(aid)
    assert calls == [C], "A(끝남)·B(이미 지워짐)는 다시 하지 않는다"
    assert r["result"]["items"][B] == {"state": "done", "recovered": True} and r["result"]["items"][C]["state"] == "done"


def test_recover_leaves_other_kinds_alone(home):
    a = approvals.create("team_archive", {"team": "T"})
    with team._Tx() as c:
        c.execute("UPDATE approvals SET status='executing' WHERE id=?", (a["id"],))
    assert ops.recover() == 0 and approvals.get(a["id"])["status"] == "executing"


def test_move_batch_never_moves_content(home, monkeypatch, tmp_path):
    seen = {}
    real = lifecycle.move_session

    def spy(sid, new_cwd, move_content=False, dry_run=True):
        if not dry_run:
            seen["move_content"] = move_content
        return real(sid, new_cwd, move_content=move_content, dry_run=dry_run)
    monkeypatch.setattr(lifecycle, "move_session", spy)
    a = ops.request("sessions_move", {"ids": [A], "new_cwd": str(tmp_path / "new"), "move_content": True})["approval"]
    assert "move_content" not in a["args"]
    _approve(a)
    ops.finish(a["id"])
    assert seen["move_content"] is False, "내용물 이동은 화면 전용"


# ---------------------------------------------------------------- 새 세션
@pytest.fixture
def launched(monkeypatch):
    out = []

    class P:
        pid = 2001

    def fake(key, sid, argv, cwd, persist, cap):
        out.append((argv, cwd))
        s = webterm._TermSession(key, P(), persist=persist)
        webterm._ACTIVE[key] = s
        return s
    webterm._ACTIVE.clear()
    monkeypatch.setattr(webterm, "_launch", fake)
    monkeypatch.setattr(ops, "trusted", lambda cwd: True)
    yield out
    webterm._ACTIVE.clear()


def test_create_narrow_starts_now_in_ask_mode(home, launched, tmp_path):
    r = ops.create(str(tmp_path), "일꾼", "시작", {"effort": "low"})
    assert r["status"] == "started" and r["permission_mode"] == "manual"
    argv = launched[0][0]
    assert lo.SKIP_FLAG not in argv and argv[-1] == "시작" and argv[argv.index("-n") + 1] == "일꾼"
    assert lo.load(r["session_id"])["source"] == "prompt"


@pytest.mark.parametrize("options", [{"permission_mode": "bypassPermissions"}, {"permission_mode": "acceptEdits"},
                                     {"allowed_tools": ["Bash"]}, {"agent": "rv"}])
def test_create_widening_needs_approval_and_starts_nothing(home, launched, tmp_path, options):
    r = ops.create(str(tmp_path), "일꾼", "시작", options, caller=A)
    assert r["status"] == "pending_approval" and not launched
    a = r["approval"]
    assert a["kind"] == "session_create_auto" and a["args"]["widens"] and a["args"]["prompt"] == "시작"


def test_approved_create_starts_once_with_bound_options(home, launched, tmp_path):
    a = ops.create(str(tmp_path), "일꾼", "시작", {"permission_mode": "bypassPermissions"})["approval"]
    _approve(a)
    r = ops.finish(a["id"])
    assert r["status"] == "executed" and r["result"]["session_id"] == a["args"]["session_id"]
    assert lo.SKIP_FLAG in launched[0][0] and lo.load(a["args"]["session_id"])["source"] == "approved"
    assert ops.finish(a["id"])["status"] == "executed" and len(launched) == 1


def test_create_refuses_bad_input(home, launched, tmp_path):
    for call, code in ((lambda: ops.create("상대경로"), "target_not_absolute"),
                       (lambda: ops.create(str(tmp_path / "없음")), "no_cwd"),
                       (lambda: ops.create(str(tmp_path), prompt="--settings x"), "bad_prompt")):
        with pytest.raises(team.TeamError) as e:
            call()
        assert e.value.code == code
    with pytest.raises(team.TeamError) as e:
        ops.create(str(tmp_path), options={"settings": "{}"})
    assert e.value.code.startswith("refused_option")
    assert not launched


def test_create_rate_limit(home, launched, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "CREATE_PER_HOUR", 2)
    ops.create(str(tmp_path))
    ops.create(str(tmp_path))
    with pytest.raises(team.TeamError) as e:
        ops.create(str(tmp_path))
    assert e.value.code == "create_rate_limited" and e.value.status == 429 and len(launched) == 2


def test_create_reports_untrusted_folder(home, launched, tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "trusted", lambda cwd: False)
    r = ops.create(str(tmp_path))
    assert r["needs_trust"] is True and "신뢰" in r["note"]


def test_trusted_reads_flag_and_parents(home, tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert ops.trusted(str(tmp_path / "p")) is None, "못 읽으면 모름"
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {
        str(tmp_path / "ok").replace("\\", "/"): {"hasTrustDialogAccepted": True},
        str(tmp_path / "no"): {"hasTrustDialogAccepted": False}}}), encoding="utf-8")
    assert ops.trusted(str(tmp_path / "ok")) is True and ops.trusted(str(tmp_path / "ok" / "sub")) is True
    assert ops.trusted(str(tmp_path / "no")) is False


# ---------------------------------------------------------------- 라우트
def _client(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setattr(team, "start", lambda *a, **k: None)
    monkeypatch.setattr("session_manager.housekeeping.start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app(), base_url="http://127.0.0.1")


def test_routes_work_for_claude_caller_and_refuse_phone(home, monkeypatch, tmp_path):
    from session_manager.pclink import sender
    monkeypatch.setattr(sender, "caller_session", lambda *a: C)        # 바깥문이 막는 Claude 세션도 이 길은 쓴다
    with _client(monkeypatch) as c:
        assert c.get("/api/v1/session-ops/capabilities").json()["api"] == ops.API_VERSION
        assert c.get("/api/v1/session-ops/find", params={"q": "남북"}).json()["total"] == 1
        r = c.post("/api/v1/session-ops/delete/preview", json={"ids": [A, C]}).json()
        assert {i["session_id"]: i.get("reason") for i in r["items"]} == {A: None, C: "self"}, "부른 세션 판별이 이어진다"
        r = c.post("/api/v1/session-ops/delete/request", json={"ids": [A]})
        assert r.status_code == 200 and r.json()["approval"]["requested_by"] == C
        aid = r.json()["approval"]["id"]
        assert c.post(f"/api/v1/session-ops/finish/{aid}").json()["status"] == "pending"
        assert c.post("/api/sessions/" + A + "/delete", json={"dry_run": False}).status_code == 403, "원시 길은 막힌다"
        assert c.get("/api/v1/session-ops/find", headers={"X-ClewPath-Via": "relay"}).status_code == 403, "폰에서는 안 된다"
        assert c.post("/api/v1/session-ops/move/request", json={"ids": [A], "new_cwd": "상대"}).json()["error"] == "target_not_absolute"
