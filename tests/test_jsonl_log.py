"""jsonl_log(E-D10): 잠금 append·크기 상한 회전 1세대·tail. policy.audit 과 devices.audit 이 같이 쓴다."""
from __future__ import annotations

import json
import threading

import pytest

from session_manager import jsonl_log, policy, devices


def test_append_and_rotate(tmp_path):
    f = tmp_path / "a.jsonl"
    for i in range(5):
        jsonl_log.append(f, {"i": i}, max_bytes=60)
    assert jsonl_log.rotated_path(f).exists(), "상한을 넘으면 .1 로 회전"
    lines = f.read_text(encoding="utf-8").splitlines()
    assert all("ts" in json.loads(x) for x in lines)
    t = jsonl_log.tail(f, 10)
    assert [r["i"] for r in t] == [0, 1, 2, 3, 4], "tail 은 회전 파일까지 이어 읽는다"
    jsonl_log.append(f, {"ts": "custom", "i": 9}, ts_key="ts")
    assert jsonl_log.tail(f, 1)[0]["ts"] == "custom", "ts 가 있으면 덮어쓰지 않는다"
    jsonl_log.append(f, {"i": 10}, ts_key=None)
    assert "ts" not in jsonl_log.tail(f, 1)[0]


def test_concurrent_appends_keep_every_line(tmp_path):
    f = tmp_path / "c.jsonl"
    def w(k):
        for i in range(50):
            jsonl_log.append(f, {"k": k, "i": i}, max_bytes=0)
    ts = [threading.Thread(target=w, args=(k,)) for k in range(4)]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    rows = jsonl_log.tail(f, 1000)
    assert len(rows) == 200 and all(isinstance(r["i"], int) for r in rows)


def test_policy_audit_uses_shared_log(fake_claude_home):
    policy.audit({"kind": "x"})
    rows = jsonl_log.tail(policy._audit_file(), 5)
    assert rows[-1]["kind"] == "x" and isinstance(rows[-1]["ts"], str) and "T" in rows[-1]["ts"], "ISO ts 유지"


def test_pairing_audit_events(fake_claude_home):
    d = devices.add_device("a")
    devices.audit("delete", d["id"], cpub="cpub_x", pending=False)
    rows = devices.audit_tail(5)
    assert rows[-1]["event"] == "delete" and rows[-1]["device"] == d["id"] and rows[-1]["cpub"] == "cpub_x"
    assert devices._audit_file().name == "pairing-audit.jsonl"


@pytest.mark.asyncio
async def test_first_auth_audits_and_pushes_once(fake_claude_home, monkeypatch):
    from session_manager.connector import Connector
    from session_manager import push
    sent = []
    monkeypatch.setattr(push, "send", lambda kind, sid, title, body, extra=None: sent.append((kind, sid, title)) or 1)
    d = devices.add_device("")
    conn = Connector(relay_url="ws://relay.test/ws", room="rm_x", token="t", local_base="http://127.0.0.1:5100")
    conn._res = lambda *a, **k: _noop()
    await conn._handle_req({"v": 1, "type": "req", "id": "r1", "method": "auth", "params": {"token": d["token"], "name": "iPhone"}, "cid": "c1"})
    await conn._handle_req({"v": 1, "type": "req", "id": "r2", "method": "auth", "params": {"token": d["token"], "name": "iPhone"}, "cid": "c2"})
    ev = [r for r in devices.audit_tail(10) if r["event"] == "first_auth"]
    assert len(ev) == 1 and ev[0]["name"] == "iPhone" and ev[0]["reported"] == "iPhone"
    assert len(sent) == 1 and sent[0][0] == "device" and "iPhone" in sent[0][2], "첫 접속만 웹푸시(E-3 ①)"


async def _noop():
    return None
