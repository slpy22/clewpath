"""관제 그룹 알림 라우팅 — 훅 이벤트가 그룹 설정대로 푸시되는지.

소속 세션은 그룹 설정이 결정(일반 '작업 완료' 대신), 비소속은 기존 일반 알림 그대로.
"""
from __future__ import annotations

import pytest

from session_manager import mongroups, push

M = "aaaaaaaa-1111-2222-3333-444444444444"
S1 = "bbbbbbbb-1111-2222-3333-444444444444"
OTHER = "dddddddd-1111-2222-3333-444444444444"


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(push, "send", lambda kind, sid, title, body, extra=None: out.append(
        {"kind": kind, "sid": sid, "title": title, "extra": extra}) or 1)
    # 설정은 전부 기본값처럼 동작하게(permission/ready/monitor on, waiting off)
    monkeypatch.setattr(push.appconfig, "get_bool", lambda s, k, d=False: d)
    return out


def _ev(sid, name, **kw):
    return {"session_id": sid, "hook_event_name": name, "cwd": "C:/proj", **kw}


def test_manager_stop_routes_to_group(fake_claude_home, sent):
    g = mongroups.save("포털", M, [S1], labels={M: "관리", S1: "포털개발"})
    push.notify_from_event(_ev(M, "Stop"))
    assert [s["kind"] for s in sent] == ["mon-stop"]
    assert sent[0]["title"] == "[포털] 관리 에이전트 턴 종료"
    assert sent[0]["extra"] == {"gid": g["id"]}


def test_sub_stop_routes_to_group_with_label(fake_claude_home, sent):
    mongroups.save("포털", M, [S1], labels={S1: "포털개발"})
    push.notify_from_event(_ev(S1, "Stop"))
    assert [s["kind"] for s in sent] == ["mon-done"]
    assert sent[0]["title"] == "[포털] 포털개발 응답 완료"


def test_sub_stop_off_silences_even_generic(fake_claude_home, sent):
    mongroups.save("포털", M, [S1], notify={"sub_stop": False})
    push.notify_from_event(_ev(S1, "Stop"))
    assert sent == []                                   # 그룹 설정이 결정 — 일반 '작업 완료'도 없음


def test_sub_start_default_off_then_on(fake_claude_home, sent):
    g = mongroups.save("포털", M, [S1])
    push.notify_from_event(_ev(S1, "UserPromptSubmit"))
    assert sent == []
    mongroups.update(g["id"], notify={"sub_start": True})
    push.notify_from_event(_ev(S1, "UserPromptSubmit"))
    assert [s["kind"] for s in sent] == ["mon-start"]


def test_non_member_keeps_generic_ready(fake_claude_home, sent):
    mongroups.save("포털", M, [S1])
    push.notify_from_event(_ev(OTHER, "Stop"))
    assert [s["kind"] for s in sent] == ["ready"]


def test_permission_stays_generic_for_members(fake_claude_home, sent, monkeypatch):
    mongroups.save("포털", M, [S1])
    monkeypatch.setattr("session_manager.hooks._classify_notification", lambda m: "permission")
    push.notify_from_event(_ev(S1, "Notification", message="도구 승인 필요"))
    assert [s["kind"] for s in sent] == ["permission"]


def test_monitor_master_switch_off_falls_back(fake_claude_home, sent, monkeypatch):
    mongroups.save("포털", M, [S1])
    monkeypatch.setattr(push.appconfig, "get_bool",
                        lambda s, k, d=False: False if (s, k) == ("push", "monitor") else d)
    push.notify_from_event(_ev(M, "Stop"))
    assert [s["kind"] for s in sent] == ["ready"]        # 일반 알림으로 폴백


def test_send_payload_carries_extra(fake_claude_home, monkeypatch):
    # send() 실제 페이로드에 gid 가 실리는지(웹푸시 전송은 가짜 구독으로 가로챔)
    captured = {}
    monkeypatch.setattr(push, "_load_subs", lambda: [{"endpoint": "e", "keys": {}}])
    monkeypatch.setattr(push, "_webpush_send", lambda sub, body: captured.setdefault("body", body))
    monkeypatch.setattr(push.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    push._dedupe.clear()
    n = push.send("mon-stop", M, "t", "b", {"gid": "abcd1234", "ignored": {"x": 1}})
    assert n == 1
    import json
    p = json.loads(captured["body"])
    assert p["gid"] == "abcd1234" and "ignored" not in p and p["tag"] == f"{M}:mon-stop"
