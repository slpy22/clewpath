"""v0.9.3 스캐너 증분 파싱 — 결과 동치·폴백·무효화·성능."""
from __future__ import annotations

import json
import time
from dataclasses import asdict

from session_manager import scanner
from tests.conftest import write_session

SID = "11111111-2222-3333-4444-555555555555"


def _rec(i, **kw):
    o = {"type": "assistant" if i % 2 else "user", "timestamp": f"2026-09-27T00:00:{i%60:02d}Z",
         "cwd": "F:/proj", "message": {"role": "assistant", "model": f"claude-m{i}", "content": "x" * 50}}
    o.update(kw)
    return o


def _append(path, objs, partial: str | None = None):
    with path.open("a", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
        if partial is not None:
            f.write(partial)                                   # 개행 없는 부분 줄(쓰는 중)


def _full(path):
    return asdict(scanner._parse_meta(path, path.stat()))


def _same(a, b):
    a = dict(a); b = dict(b); a.pop("mtime"); b.pop("mtime")
    return a == b


def test_incremental_equals_full_across_appends(fake_claude_home):
    scanner._CACHE.clear(); scanner.STATS.update(incremental=0, full=0)
    path = write_session(fake_claude_home, "F--proj", SID, [_rec(0, slug="first"), {"type": "mode", "mode": "x"}])
    m0 = asdict(scanner.scan_one(SID))
    assert _same(m0, _full(path)) and scanner.STATS["full"] == 1
    # 1) 여러 줄 append + 부분 줄
    time.sleep(0.01)
    _append(path, [_rec(1), {"type": "custom-title", "customTitle": "이름1"}], partial='{"type":"assistant","timestamp":"2026-09-27T00:01:00Z"')
    m1 = asdict(scanner.scan_one(SID))
    assert scanner.STATS["incremental"] == 1 and m1["custom_title"] == "이름1" and m1["last_model"] == "claude-m1"
    assert m1["line_count"] == 4 and m1["ended_at"] == "2026-09-27T00:00:01Z"     # 부분 줄은 아직 안 셈
    assert _same(m1, _full(path))
    # 2) 부분 줄 완성 + 더 append
    time.sleep(0.01)
    _append(path, [], partial=',"message":{"role":"assistant","model":"claude-late"}}\n')
    _append(path, [_rec(3, aiTitle="ai", type="ai-title"), _rec(5)])
    m2 = asdict(scanner.scan_one(SID))
    assert scanner.STATS["incremental"] == 2 and m2["line_count"] == 7 and m2["last_model"] == "claude-m5"
    assert m2["ai_title"] == "ai" and m2["slug"] == "first" and m2["started_at"] == "2026-09-27T00:00:00Z"
    assert _same(m2, _full(path))
    assert scanner.STATS["full"] == 1                                            # 전체 재파싱은 최초 1회뿐


def test_shrink_or_rewrite_falls_back_to_full(fake_claude_home):
    scanner._CACHE.clear(); scanner.STATS.update(incremental=0, full=0)
    path = write_session(fake_claude_home, "F--proj", SID, [_rec(i) for i in range(20)])
    scanner.scan_one(SID)
    # 축소(트렁케이트)
    time.sleep(0.01)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:5]) + "\n", encoding="utf-8")
    m = asdict(scanner.scan_one(SID))
    assert m["line_count"] == 5 and scanner.STATS["full"] == 2 and scanner.STATS["incremental"] == 0
    # 같은 크기 이상으로 앞부분 재작성(오프셋 직전 서명이 달라짐) → 전체
    time.sleep(0.01)
    rewritten = [json.dumps(_rec(i, cwd="G:/moved")) for i in range(5)]
    path.write_text("\n".join(rewritten) + "\n" + json.dumps(_rec(9)) + "\n", encoding="utf-8")
    m = asdict(scanner.scan_one(SID))
    assert m["cwd"] == "G:/moved" and m["line_count"] == 6 and scanner.STATS["full"] == 3
    assert _same(m, _full(path))


def test_invalidate_forces_full(fake_claude_home):
    scanner._CACHE.clear(); scanner.STATS.update(incremental=0, full=0)
    path = write_session(fake_claude_home, "F--proj", SID, [_rec(0)])
    scanner.scan_one(SID)
    scanner.invalidate(path)
    time.sleep(0.01); _append(path, [_rec(1)])
    scanner.scan_one(SID)
    assert scanner.STATS == {"incremental": 0, "full": 2}


def test_unchanged_file_uses_cache_without_reading(fake_claude_home, monkeypatch):
    scanner._CACHE.clear()
    path = write_session(fake_claude_home, "F--proj", SID, [_rec(0)])
    scanner.scan_one(SID)
    monkeypatch.setattr(scanner, "_fold_from", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read!")))
    assert scanner.scan_one(SID).line_count == 1


def test_incremental_is_fast_on_big_file(fake_claude_home):
    scanner._CACHE.clear()
    path = write_session(fake_claude_home, "F--proj", SID, [])
    big = [_rec(i, message={"role": "assistant", "model": "claude-x", "content": "y" * 900}) for i in range(6000)]
    _append(path, big)                                     # ≈ 6MB
    t0 = time.perf_counter(); scanner.scan_one(SID); full_s = time.perf_counter() - t0
    time.sleep(0.01); _append(path, [_rec(1)])
    t0 = time.perf_counter(); m = scanner.scan_one(SID); inc_s = time.perf_counter() - t0
    assert m.line_count == 6001 and inc_s < 0.2 and inc_s < full_s / 5
