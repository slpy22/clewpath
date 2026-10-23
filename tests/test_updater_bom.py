"""pwsh 가 없는 PC(노트북 — powershell.exe 5.1)에서 업데이트 러너가 쓴 last_apply.json 에는 BOM 이 붙는다.
읽는 쪽이 BOM 을 견뎌야 '방금 실패한 버전'을 알아 재기동 자동 적용의 롤백 반복을 끊는다(updater.recently_failed)."""
from __future__ import annotations

import json

from session_manager import updater


def _write(fake_claude_home, rec: dict, bom: bool) -> None:
    w = updater.work_dir()
    w.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(rec).encode("utf-8")
    (w / "last_apply.json").write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


def test_recently_failed_reads_bom_written_by_ps51(fake_claude_home):
    for bom in (True, False):
        _write(fake_claude_home, {"version": "0.32.9", "ok": False, "finished_at": 1}, bom)
        assert updater.recently_failed("0.32.9") is True, bom
        assert updater.recently_failed("0.32.8") is False
        _write(fake_claude_home, {"version": "0.32.9", "ok": True, "finished_at": 1}, bom)
        assert updater.recently_failed("0.32.9") is False


def test_update_status_shows_last_apply_with_bom(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL", "SM_APP_URL"):
        monkeypatch.delenv(k, raising=False)
    from session_manager import server as S
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    _write(fake_claude_home, {"version": "0.32.9", "ok": False, "finished_at": 1}, True)
    with TestClient(S.create_app()) as c:
        st = c.get("/api/owner/update/status").json()
    assert st["last_apply"]["version"] == "0.32.9" and st["last_apply"]["ok"] is False
