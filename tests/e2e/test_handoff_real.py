"""수동 갈아타기 실 e2e(A13) — 진짜 claude(haiku)로 복제본 인수인계서 → 새 세션 기동 → 묶음 교체까지 한 번.

돌리는 조건: `SM_E2E_HANDOFF=1` 그리고 `-m e2e`(기본 실행은 e2e 를 빼므로 skip).
작업 폴더: `SM_E2E_HANDOFF_CWD`(빈 폴더 권장, 필수). **이미 신뢰한 폴더(상위 신뢰 포함)만** 쓴다 — 신뢰 질문에 대신 답하면 claude 설정이
바뀌므로 이 시험은 답하지 않고 skip 한다. 새 세션은 plan 권한으로 띄운다.

격리: ClewPath 데이터는 conftest 의 임시 홈(team.db·인수인계서). 세션 기록은 진짜 홈에 생기므로(claude 가 쓴다) 이번 실행이 만든 세션 둘만 지운다.
비용: haiku 3턴(시험 세션 1 + 복제본 1 + 새 세션 1) 수준.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

if os.environ.get("SM_E2E_HANDOFF") != "1":
    pytest.skip("SM_E2E_HANDOFF=1 이 아님 — 진짜 claude 를 쓰는 시험", allow_module_level=True)

CWD = Path(os.environ.get("SM_E2E_HANDOFF_CWD") or "__없음__")
WORD = "청포도"


def _claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "SM_", "RELAY_", "CLEWPATH_"))}


def _drop(sid: str) -> None:
    """이번 실행이 만든 세션만 지운다(폴더는 다른 시험 기록이 있을 수 있어 그대로)."""
    for jl in (Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
        d = jl.parent
        jl.unlink(missing_ok=True)
        for x in (d / "memory", d):                     # 이번 실행이 만든 빈 폴더만 사라진다(내용이 있으면 실패하고 그대로)
            try:
                x.rmdir()
            except OSError:
                pass
    shutil.rmtree(Path.home() / ".claude" / "session-env" / sid, ignore_errors=True)


def _until(fn, timeout: float, what: str):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(1)
    raise AssertionError(f"{timeout:g}초 안에 안 됨: {what}")


def test_real_handoff(monkeypatch):
    from session_manager import handoff, scanner, session_ops, team, webterm

    exe = shutil.which("claude")
    if not exe:
        pytest.skip("claude 가 없음")
    if not CWD.is_dir() or session_ops.trusted(str(CWD)) is not True:
        pytest.skip(f"이미 신뢰한 작업 폴더가 아님: {CWD}")
    for k in [k for k in os.environ if k.upper().startswith("CLAUDE") or k.upper().startswith("RELAY_")]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(scanner, "projects_dir", lambda: Path.home() / ".claude" / "projects")     # 세션 기록만 진짜 홈, ClewPath 데이터는 conftest 의 임시 홈
    monkeypatch.setattr(handoff, "_notify", lambda hid, what: None)
    real_scan = scanner.scan_one
    monkeypatch.setattr(scanner, "scan_one", lambda sid: (m := real_scan(sid)) and dataclasses.replace(m, headless=False))   # 시험 세션을 -p 로 만들어 자동 실행 기록이지만, 에이전트로 쓰는 경로를 본다

    old, new = str(uuid.uuid4()), None
    name = "e2e갈아타기" + old[:6]
    try:
        r = subprocess.run([exe, "-p", f"암호는 {WORD} 입니다. 다음 세션에 꼭 전해야 합니다. '네' 라고만 답하세요.", "--session-id", old, "--model", "haiku",
                            "--tools", "", "--strict-mcp-config", "--settings", json.dumps({"disableAllHooks": True})],
                           cwd=CWD, env=_claude_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        assert r.returncode == 0, "시험 세션 만들기 실패"
        _until(lambda: handoff._meta(old), 30, "시험 세션이 목록에 보임")

        a = team.create_agent(old, name)
        st = handoff.start(a["id"], confirm_usd=-1) if handoff.precheck(a["id"])["est_usd"] is None else handoff.start(a["id"])
        st = _until(lambda: (s := handoff.status_of(st["id"]))["status"] != "preparing" and s, 200, "인수인계서 준비")
        assert st["status"] == "ready", st["error"]
        assert WORD in st["draft"], "인수인계서에 대화 내용이 담겨야 한다"
        assert st["attempts"][0]["actual_usd"] is not None

        done = handoff.confirm(st["id"], st["draft_rev"], st["snapshot_token"], options={"model": "haiku", "permission_mode": "plan"})
        assert done["status"] == "starting", done["error"]
        new = done["to_session"]
        final = _until(lambda: (s := handoff.status_of(st["id"]))["status"] != "starting" and s, 120, "새 세션 기록")
        assert final["status"] == "done", final["error"]
        assert team.current_session(a["id"]) == new
        h = team.agent_detail(a["id"])["history"]
        assert [x["reason"] for x in h] == ["replaced", "created"]

        jl = next((Path.home() / ".claude" / "projects").glob(f"*/{new}.jsonl"))
        text = _until(lambda: (t := jl.read_text(encoding="utf-8", errors="replace")) and "agent-name" in t and t, 30, "새 세션 이름 기록")
        assert WORD in text, "첫 지시로 인수인계서가 넘어가야 한다"
        assert name in text, "claude -n 으로 에이전트 이름이 세션 이름이 된다"
        old_jl = next((Path.home() / ".claude" / "projects").glob(f"*/{old}.jsonl"))
        assert '"custom-title"' not in old_jl.read_text(encoding="utf-8", errors="replace"), "옛 세션 기록은 건드리지 않는다"
    finally:
        if new:
            try:
                webterm.stop_terminal(new)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2)
            _drop(new)
        _drop(old)
