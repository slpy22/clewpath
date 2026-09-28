"""자기 회복 Phase 2(Host 쪽) — 웹푸시 구독 CP 미러, 커넥터 bye 프레임, 업데이트 시 bye 예약."""
from __future__ import annotations

import asyncio
import json

from session_manager import connector, push


class _Resp:
    def __init__(self, code): self.status_code = code


class _Client:
    calls = []
    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, json=None, headers=None):
        _Client.calls.append((url, json, headers)); return _Resp(200)


def test_mirror_payload_and_post(fake_claude_home, monkeypatch):
    push.ensure_vapid()
    monkeypatch.setattr(push, "mirror_to_cp_async", lambda: None)      # 저장 경로의 비동기 미러는 끈다
    push.add_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}}, name="폰")
    p = push.mirror_payload()
    assert "BEGIN" in p["vapid_private_pem"] and p["subscriptions"] == [
        {"endpoint": "https://push.example/1", "keys": {"p256dh": "a", "auth": "b"}, "name": "폰"}]
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "cp_url", lambda: "http://cp.test")
    monkeypatch.setattr(cp_client, "fetch_jwt", lambda name="": {"token": "JWT1", "room_key": "rm_x"})
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client); _Client.calls.clear()
    r = push.mirror_to_cp()
    assert r == {"ok": True, "count": 1}
    url, body, headers = _Client.calls[0]
    assert url == "http://cp.test/push/webpush-mirror" and headers["Authorization"] == "Bearer JWT1"
    assert body["subscriptions"][0]["endpoint"] == "https://push.example/1"


def test_mirror_skips_without_cp_or_jwt(fake_claude_home, monkeypatch):
    from session_manager import cp_client
    monkeypatch.setattr(cp_client, "cp_url", lambda: None)
    assert push.mirror_to_cp() == {"ok": False, "skipped": "no_cp"}
    monkeypatch.setattr(cp_client, "cp_url", lambda: "http://cp.test")
    assert push.mirror_to_cp()["skipped"] == "no_vapid"                 # 키 파일 없음
    push.ensure_vapid()
    monkeypatch.setattr(cp_client, "fetch_jwt", lambda name="": None)
    assert push.mirror_to_cp()["skipped"] == "no_jwt"


def test_subscription_changes_trigger_mirror(fake_claude_home, monkeypatch):
    push.ensure_vapid()
    n = []
    monkeypatch.setattr(push, "mirror_to_cp_async", lambda: n.append(1))
    push.add_subscription({"endpoint": "https://push.example/2", "keys": {"p256dh": "a", "auth": "b"}})
    push.remove_subscription("https://push.example/2")
    assert len(n) == 2


class _Ws:
    def __init__(self, fail=False): self.sent = []; self.fail = fail
    async def send(self, s):
        if self.fail:
            raise RuntimeError("closed")
        self.sent.append(s)


def test_connector_bye_frame_and_request_bye():
    c = connector.Connector.__new__(connector.Connector)
    c.ws = None
    assert asyncio.run(c.bye("shutdown")) is False                        # 소켓 없음 → 조용히 False
    c.ws = _Ws()
    assert asyncio.run(c.bye("update")) is True
    assert json.loads(c.ws.sent[0]) == {"v": 1, "type": "bye", "reason": "update"}
    c.ws = _Ws(fail=True)
    assert asyncio.run(c.bye("x")) is False
    # request_bye: 커넥터/루프 없으면 False, 있으면 루프에 예약해 결과를 기다린다
    connector.CURRENT = None
    assert connector.request_bye("update") is False
    loop = asyncio.new_event_loop()
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True); t.start()
    try:
        c.ws = _Ws(); c.loop = loop; connector.CURRENT = c
        assert connector.request_bye("update") is True
        assert json.loads(c.ws.sent[-1])["reason"] == "update"
    finally:
        loop.call_soon_threadsafe(loop.stop); t.join(timeout=2); connector.CURRENT = None
