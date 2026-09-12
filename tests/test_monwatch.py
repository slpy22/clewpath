"""관제 호출 실패 감지(monwatch) — 관리 세션 jsonl tail 로 하위 호출의 tool_result is_error 를 잡는다."""
from __future__ import annotations

import json

from session_manager import monwatch, mongroups, push
from tests.conftest import write_session

M = "aaaaaaaa-1111-2222-3333-444444444444"
S1 = "bbbbbbbb-1111-2222-3333-444444444444"
OUT = "eeeeeeee-1111-2222-3333-444444444444"


def _use(tid, cmd):
    return {"type": "assistant", "timestamp": "2026-09-12T00:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}}


def _res(tid, text, err):
    return {"type": "user", "timestamp": "2026-09-12T00:00:01Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": err, "content": text}]}}


def _append(path, *objs):
    with open(path, "a", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


def _watcher():
    got = []
    w = monwatch.Watcher(notify=lambda g, target, text: got.append((g["id"], target, text)) or 1)
    return w, got


def test_error_result_notifies_with_target(fake_claude_home):
    path = write_session(fake_claude_home, "F--proj", M, [])
    g = mongroups.save("포털", M, [S1], labels={S1: "포털개발"})
    w, got = _watcher()
    assert w.poll() == 0
    _append(path, _use("t1", f'claude -p --resume {S1} "고쳐"'),
            _res("t1", "Exit code 1\nError: session not found", True))
    assert w.poll() == 1
    assert got == [(g["id"], S1, "Exit code 1\nError: session not found")]


def test_ok_result_is_silent(fake_claude_home):
    path = write_session(fake_claude_home, "F--proj", M, [])
    mongroups.save("포털", M, [S1])
    w, got = _watcher()
    w.poll()
    _append(path, _use("t1", f"claude -p --resume {S1} 'x'"), _res("t1", "done", False))
    assert w.poll() == 0 and got == []


def test_non_member_and_non_claude_errors_ignored(fake_claude_home):
    path = write_session(fake_claude_home, "F--proj", M, [])
    mongroups.save("포털", M, [S1])
    w, got = _watcher()
    w.poll()
    _append(path, _use("t1", f"claude -p --resume {OUT} 'x'"), _res("t1", "boom", True),
            _use("t2", "pytest -q"), _res("t2", "1 failed", True))
    assert w.poll() == 0 and got == []


def test_history_before_start_not_replayed(fake_claude_home):
    path = write_session(fake_claude_home, "F--proj", M, [
        _use("old", f"claude -p --resume {S1} 'x'"), _res("old", "boom", True)])
    mongroups.save("포털", M, [S1])
    w, got = _watcher()
    assert w.poll() == 0 and got == []
    _append(path, _use("t1", f"claude -p --resume {S1} 'y'"), _res("t1", "boom2", True))
    assert w.poll() == 1 and got[0][2] == "boom2"


def test_no_groups_or_flag_off_creates_no_tail(fake_claude_home):
    write_session(fake_claude_home, "F--proj", M, [])
    w, _ = _watcher()
    assert w.poll() == 0 and w.tails == {}
    g = mongroups.save("포털", M, [S1], notify={"error": False})
    assert w.poll() == 0 and w.tails == {}
    mongroups.update(g["id"], notify={"error": True})
    w.poll()
    assert list(w.tails) == [g["id"]]
    mongroups.delete(g["id"])
    w.poll()
    assert w.tails == {}                                  # 삭제 즉시 tail 해제


def test_missing_file_then_appears(fake_claude_home, monkeypatch):
    monkeypatch.setattr(monwatch, "_PATH_RETRY_POLLS", 2)
    mongroups.save("포털", M, [S1])
    w, got = _watcher()
    w.poll()
    assert w.tails and w.tails[next(iter(w.tails))].path is None   # 파일 없음 → 내성
    path = write_session(fake_claude_home, "F--proj", M, [])
    w.poll(); w.poll()                                    # 재탐색 주기 후 경로 확보
    _append(path, _use("t1", f"claude -p --resume {S1} 'x'"), _res("t1", "boom", True))
    assert w.poll() == 1 and got[0][1] == S1


def test_long_prompt_before_resume_not_truncated(fake_claude_home):
    path = write_session(fake_claude_home, "F--proj", M, [])
    mongroups.save("포털", M, [S1])
    w, got = _watcher()
    w.poll()
    cmd = 'claude -p "' + ("긴 프롬프트 " * 600) + f'" --resume {S1}'   # 뷰어 절삭(3000자) 넘김
    _append(path, _use("t1", cmd), _res("t1", "boom", True))
    assert w.poll() == 1


def test_notify_error_payload_and_master_switch(fake_claude_home, monkeypatch):
    sent = []
    monkeypatch.setattr(push, "send", lambda kind, sid, title, body, extra=None: sent.append(
        (kind, sid, title, body, extra)) or 1)
    monkeypatch.setattr(monwatch.appconfig, "get_bool", lambda s, k, d=False: d)
    g = mongroups.save("포털", M, [S1], labels={S1: "포털개발"})
    n = monwatch.notify_error(g, S1, "Exit code 1\n\n  Error:   timeout " + "x" * 300)
    assert n == 1
    kind, sid, title, body, extra = sent[0]
    assert (kind, sid, title, extra) == ("mon-error", S1, "[포털] 포털개발 호출 실패", {"gid": g["id"]})
    assert body.startswith("Exit code 1 Error: timeout") and len(body) == 120
    monkeypatch.setattr(monwatch.appconfig, "get_bool",
                        lambda s, k, d=False: False if (s, k) == ("push", "monitor") else d)
    assert monwatch.notify_error(g, S1, "boom") == 0


def test_thread_start_stop_idempotent():
    t1 = monwatch.start(interval=0.05)
    assert monwatch.start(interval=0.05) is t1
    monwatch.stop()
    t1.join(timeout=2)
    assert not t1.is_alive()
