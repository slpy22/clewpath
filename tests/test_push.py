"""웹푸시 회귀 - 키 영속·구독 관리·dedupe·만료 정리.

VAPID 키가 재생성되면 모든 기기가 소리 없이 수신 불능이 된다(재구독 필요).
그래서 '한 번 만든 키는 불변'을 가장 먼저 못박는다.
"""
from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("cryptography")

from session_manager import push  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    push._dedupe.clear()
    yield
    push._dedupe.clear()


def _sub(ep="https://fcm.example/ep1"):
    return {"endpoint": ep, "keys": {"p256dh": "pk", "auth": "au"}}


# ---- VAPID ----

def test_vapid_key_is_stable_and_valid():
    k1 = push.ensure_vapid()
    k2 = push.ensure_vapid()
    assert k1 == k2, "호출마다 키가 바뀌면 모든 기기가 수신 불능"
    raw = base64.urlsafe_b64decode(k1 + "=" * (-len(k1) % 4))
    assert len(raw) == 65 and raw[0] == 0x04       # X9.62 비압축 P-256 점


# ---- 구독 관리 ----

def test_subscription_upsert_and_remove():
    assert push.add_subscription(_sub(), name="phone") is True
    assert push.add_subscription(_sub()) is False           # 같은 endpoint = upsert
    assert len(push.list_subscriptions()) == 1
    assert push.remove_subscription("https://fcm.example/ep1") is True
    assert push.remove_subscription("https://fcm.example/ep1") is False
    assert push.list_subscriptions() == []


def test_subscription_requires_keys():
    with pytest.raises(ValueError):
        push.add_subscription({"endpoint": "https://x", "keys": {}})


def test_list_hides_full_endpoint():
    push.add_subscription(_sub("https://fcm.example/" + "x" * 100))
    hint = push.list_subscriptions()[0]["endpoint_hint"]
    assert len(hint) < 70 and hint.endswith("...")


# ---- 발송/dedupe ----

def test_send_dedupes_within_window(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_webpush_send", lambda s, b: sent.append(b))
    monkeypatch.setattr(push.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {
                            "start": lambda self: target(*args)})())
    push.add_subscription(_sub())
    assert push.send("permission", "s1", "t", "b") == 1
    assert push.send("permission", "s1", "t", "b") == 0     # 60초 내 중복 억제
    assert push.send("ready", "s1", "t", "b") == 1           # 다른 종류는 통과
    assert len(sent) == 2


def test_send_without_subscribers():
    assert push.send("ready", "s1", "t", "b") == 0


# ---- 훅 -> 알림 ----

def test_notify_topics(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "send", lambda k, sid, t, b: calls.append(k))
    push.notify_from_event({"hook_event_name": "Notification", "session_id": "s1",
                            "cwd": "F:/proj",
                            "message": "Claude needs your permission to use Bash"})
    push.notify_from_event({"hook_event_name": "Stop", "session_id": "s1"})
    push.notify_from_event({"hook_event_name": "Notification", "session_id": "s1",
                            "message": "Claude is waiting for your input"})
    assert calls == ["permission", "ready"]                  # waiting 은 기본 off


def test_notify_ignores_missing_sid(monkeypatch):
    monkeypatch.setattr(push, "send", lambda *a: pytest.fail("보내면 안 됨"))
    push.notify_from_event({"hook_event_name": "Stop"})


# ---- 만료 구독 자동 정리 ----

def test_expired_subscription_removed(monkeypatch):
    import pywebpush

    class R:
        status_code = 410

    def boom(**kw):
        raise pywebpush.WebPushException("gone", response=R())

    monkeypatch.setattr(pywebpush, "webpush", boom)
    push.add_subscription(_sub())
    push.ensure_vapid()
    push._webpush_send(push._load_subs()[0], "{}")
    assert push.list_subscriptions() == []
