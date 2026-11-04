"""터미널에 뜬 선택 창 읽기·원격 선택·대기 상태 바로잡기(2026-11-04 사장님).

① 🔔·✋ 원격 승인 창이 고정 버튼(1·2·Esc)을 보내 터미널 선택지와 달랐다 → 지금 화면의 선택지 그대로, 고를 때 다시 확인.
② 터미널에서 고른 뒤에도 ✋ 권한 대기·🔔 숫자가 남았다(훅엔 '골랐다' 사건이 없다) → 화면·대화 기록으로 바로잡는다.
"""
from __future__ import annotations

import json
import time

import pytest

from session_manager import hooks, termprompt, webterm

SID = "11111111-2222-3333-4444-555555555555"     # conftest.sample_session


class FakeProc:
    def __init__(self):
        self.written = []

    def write(self, d):
        self.written.append(d)

    def isalive(self):
        return True

    def terminate(self, force=False):
        pass


@pytest.fixture(autouse=True)
def _clean():
    webterm._ACTIVE.clear()
    hooks._STATUS.clear()
    termprompt._CACHE.clear()
    yield
    webterm._ACTIVE.clear()
    hooks._STATUS.clear()
    termprompt._CACHE.clear()


def _term(text: str, size=(30, 110)):
    sess = webterm._TermSession(SID, FakeProc(), persist=True)
    sess.size = size
    sess.feed(text)
    webterm._ACTIVE[SID] = sess
    return sess


def at(r, c, s):
    return f"\x1b[{r};{c}H{s}"


# 2026-11-04 실측(claude 2.1.318, 파일 쓰기) 모양을 커서 이동으로 그린 것 — 색·이동 신호 섞임
DIALOG = ("\x1b[?1049h\x1b[2J"
          + at(2, 2, "● Write(probe.txt)") + at(3, 3, "1 hi")
          + at(20, 2, "Do you want to create \x1b[1mprobe.txt\x1b[22m?")
          + at(21, 2, "\x1b[38;2;177;185;249m❯ 1. Yes\x1b[39m")
          + at(22, 4, "2. Yes, and switch to accept edits (auto-approve file edits) for this session")
          + at(23, 7, "(shift+tab)")
          + at(24, 4, "3. No")
          + at(26, 2, "Esc to cancel · Tab to amend"))
# 고른 뒤: 선택 창이 지워지고 맨 아래 입력칸 ❯ — 그 위엔 산문 속 번호 목록이 있어도 선택 창이 아니다
AFTER = ("\x1b[20;1H\x1b[J"
         + at(18, 2, "1. 첫째 할 일") + at(19, 2, "2. 둘째 할 일")
         + at(27, 1, "─" * 40) + at(28, 1, "❯ ") + at(29, 3, "⏸ manual mode on"))
WORKING = at(26, 1, "✻ Churning… (3s · esc to interrupt)")


def test_parse_reads_choices_as_shown():
    p = termprompt.parse(termprompt.render(DIALOG, (30, 110)))
    assert p["question"] == "Do you want to create probe.txt?"
    assert [(c["n"], c["label"], c["selected"]) for c in p["choices"]] == [
        (1, "Yes", True),
        (2, "Yes, and switch to accept edits (auto-approve file edits) for this session", False),
        (3, "No", False)]
    assert p["choices"][1]["more"] == "(shift+tab)", "번호 줄 아래 더 들여 쓴 줄은 그 선택지의 덧말"
    assert p["footer"] == "Esc to cancel · Tab to amend" and p["multi"] is False


def test_parse_none_when_bottom_marker_is_input_box():
    txt = termprompt.render(DIALOG + AFTER, (30, 110))
    assert termprompt.parse(txt) is None, "맨 아래 ❯ 가 입력칸이면 선택 창 없음(위쪽 산문 번호 목록 무시)"
    assert not termprompt.working(txt)
    assert termprompt.working(termprompt.render(DIALOG + AFTER + WORKING, (30, 110)))


def test_parse_rejects_broken_numbering_and_multi_select():
    assert termprompt.parse("❯ 2. Yes\n  3. No") is None, "1 부터 이어지지 않으면 선택 창으로 보지 않는다"
    m = termprompt.parse("Pick\n❯ 1. [ ] 사과\n  2. [ ] 배")
    assert m and m["multi"] is True, "여러 개 고르기는 원격에서 고르지 않는다(터미널에서)"


def test_read_caches_until_output_changes():
    sess = _term(DIALOG)
    a = termprompt.read(SID)
    assert a["terminal"] and a["prompt"]["choices"][0]["label"] == "Yes"
    calls = []
    orig = termprompt.render
    termprompt.render = lambda *x: calls.append(1) or orig(*x)
    try:
        termprompt.read(SID)
        assert calls == [], "출력이 그대로면 다시 그리지 않는다"
        sess.feed(AFTER)
        assert termprompt.read(SID)["prompt"] is None and calls == [1]
    finally:
        termprompt.render = orig
    assert termprompt.read("nope") is None


# ---------------------------------------------------------------- 대기 상태 바로잡기
def _perm(at_s: float):
    hooks.update_from_event({"session_id": SID, "hook_event_name": "Notification",
                             "message": "Claude needs your permission to use Write"})
    hooks._STATUS[SID]["permission_at"] = int(at_s)
    return hooks._STATUS[SID]


def test_reconcile_terminal_answered_clears_permission():
    st = _perm(time.time() - 5)
    sess = _term(DIALOG)
    assert termprompt.reconcile(SID, st, None)["prompt"], "선택 창이 그대로면 대기 유지"
    assert hooks._STATUS[SID]["phase"] == "permission"
    sess.feed(AFTER + WORKING)
    sess.last_input = time.time()
    termprompt.reconcile(SID, hooks._STATUS[SID], None)
    assert hooks._STATUS[SID]["phase"] == "thinking", "터미널에서 고르고 claude 가 이어서 일한다"


def test_reconcile_terminal_no_input_keeps_waiting():
    """화면 읽기가 선택 창을 못 찾았어도 키 입력이 없었으면 그대로 둔다(읽기 실패로 대기를 지우지 않는다)."""
    st = _perm(time.time() - 5)
    _term("garbage")
    termprompt.reconcile(SID, st, None)
    assert hooks._STATUS[SID]["phase"] == "permission"


def _jsonl(tmp_path, recs):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return str(p)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def test_reconcile_by_jsonl_when_not_clewpath_terminal(tmp_path):
    t0 = time.time() - 60
    use = {"type": "assistant", "timestamp": _iso(t0 - 2), "message": {"content": [
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "git push"}}]}}
    path = _jsonl(tmp_path, [use])
    assert termprompt.pending_tool(path) == {"name": "Bash", "detail": "git push"}
    _perm(t0)
    termprompt.reconcile(SID, hooks._STATUS[SID], path)
    assert hooks._STATUS[SID]["phase"] == "permission", "그 뒤 대화 줄이 없으면 아직 고르는 중"
    res = {"type": "user", "timestamp": _iso(t0 + 10), "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}}
    path = _jsonl(tmp_path, [use, res])
    assert termprompt.pending_tool(path) is None
    termprompt.reconcile(SID, hooks._STATUS[SID], path)
    assert hooks._STATUS[SID]["phase"] == "thinking"
    _perm(t0)
    intr = {"type": "user", "timestamp": _iso(t0 + 11), "message": {"content": [
        {"type": "text", "text": "[Request interrupted by user for tool use]"}]}}
    termprompt.reconcile(SID, hooks._STATUS[SID], _jsonl(tmp_path, [use, res, intr]))
    assert hooks._STATUS[SID]["phase"] == "idle", "거부·중단이면 작업 중이 아니라 쉼"


def test_settle_only_touches_waiting_phases():
    hooks.update_from_event({"session_id": SID, "hook_event_name": "Stop"})
    assert hooks.settle(SID, "thinking") is False and hooks._STATUS[SID]["phase"] == "ready"


# ---------------------------------------------------------------- API
def _app(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import housekeeping
    monkeypatch.setattr(housekeeping, "start", lambda *a, **k: None)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app())


def test_prompt_and_choose_api(sample_session, monkeypatch):
    sess = _term(DIALOG)
    _perm(time.time() - 5)
    with _app(monkeypatch) as c:
        r = c.get(f"/api/owner/sessions/{SID}/prompt").json()
        assert r["terminal"] and [x["label"] for x in r["prompt"]["choices"]][0] == "Yes"
        v1 = {s["session_id"]: s for s in c.get("/api/v1/sessions").json()["sessions"]}
        assert v1[SID]["runtime"]["choices"] is True, "목록이 '여기서 고를 수 있음' 을 알려 준다"
        # 그 사이 선택지가 바뀌었으면(글자 다름) 보내지 않는다
        bad = c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "choose", "n": 2, "label": "Yes, allow all"})
        assert bad.status_code == 409 and bad.json()["error"] == "changed" and sess.proc.written == []
        ok = c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "choose", "n": 3, "label": "No"})
        assert ok.json() == {"ok": True, "sent": "choose", "n": 3} and sess.proc.written == ["3"]
        assert hooks._STATUS[SID]["phase"] == "idle", "고르면 🔔·✋ 가 바로 풀린다"


def test_old_buttons_match_by_label(sample_session, monkeypatch):
    """앱에 묶인 옛 화면의 허용·계속 허용·거부 — 화면 글자로 맞는 번호를 찾고, 없으면 보내지 않는다."""
    sess = _term(DIALOG)
    with _app(monkeypatch) as c:
        assert c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "allow_session"}).json()["n"] == 2
        assert c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "allow"}).json()["n"] == 1
        assert c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "deny"}).json()["sent"] == "deny"
        assert sess.proc.written == ["2", "1", "\x1b"]
        termprompt._CACHE.clear()
        sess.feed(at(21, 2, "❯ 1. Option A") + at(22, 4, "2. Option B") + at(24, 4, "3. Option C"))
        r = c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "allow_session"})
        assert r.status_code == 409 and sess.proc.written == ["2", "1", "\x1b"], "질문 선택지에 '계속 허용' 2번을 보내지 않는다"
        sess.feed(AFTER)
        assert c.post(f"/api/owner/sessions/{SID}/respond", json={"action": "deny"}).json()["error"] == "no_prompt"
        assert c.post("/api/owner/sessions/nope/respond", json={"action": "allow"}).json()["error"] == "no_terminal"
