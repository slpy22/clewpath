"""monitor.py — 오케스트레이션 관제 핵심 로직 단위 테스트.

Tailer(바이트 오프셋 tail), parse_calllines(호출선 추출), MonitorGroup(병합·
태깅·링버퍼)을 WS/relay 없이 순수 검증한다.
"""
from __future__ import annotations

import json

from session_manager.monitor import (
    Tailer, parse_calllines, MonitorGroup, _Session, build_group, _RING_CAP,
)
from tests.conftest import write_session

SUB = "59f9577b-0d25-47be-994b-29009cf0fba3"
MGR = "aaaaaaaa-1111-2222-3333-444444444444"


# ---- 레코드/파일 헬퍼 ----

def _write(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _append(path, records, newline=True):
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + ("\n" if newline else ""))


def _user(text, ts):
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": ts}


def _asst_text(text, ts):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}, "timestamp": ts}


def _asst_bash(cmd, ts, tid="tu1"):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                         "input": {"command": cmd}}]}, "timestamp": ts}


# ---- Tailer ----

def test_tailer_incremental(tmp_path):
    p = str(tmp_path / "a.jsonl")
    _write(p, [_user("hi", "t1")])
    t = Tailer(p)
    objs = t.read_new()
    assert len(objs) == 1 and objs[0]["message"]["content"] == "hi"
    assert t.read_new() == []                 # 새 줄 없음
    _append(p, [_user("bye", "t2")])
    objs = t.read_new()
    assert len(objs) == 1 and objs[0]["message"]["content"] == "bye"


def test_tailer_holds_partial_line(tmp_path):
    p = str(tmp_path / "b.jsonl")
    _write(p, [_user("done", "t1")])
    t = Tailer(p)
    assert len(t.read_new()) == 1
    # 개행 없는 부분 줄 append → 아직 완성 안 됨 → 보류
    _append(p, [_user("partial", "t2")], newline=False)
    assert t.read_new() == []
    # 개행으로 완성 → 이제 읽힘
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n")
    objs = t.read_new()
    assert len(objs) == 1 and objs[0]["message"]["content"] == "partial"


def test_tailer_truncation_reset(tmp_path):
    p = str(tmp_path / "c.jsonl")
    _write(p, [_user("one", "t1"), _user("two", "t2")])
    t = Tailer(p)
    assert len(t.read_new()) == 2
    # 파일이 줄어듦(회전/트렁케이트) → 오프셋 리셋해 처음부터
    _write(p, [_user("fresh", "t3")])
    objs = t.read_new()
    assert len(objs) == 1 and objs[0]["message"]["content"] == "fresh"


def test_tailer_multibyte_offset(tmp_path):
    # 한글(멀티바이트) 줄 뒤 append 가 바이트 오프셋으로 정확히 읽히는지
    p = str(tmp_path / "d.jsonl")
    _write(p, [_user("로그인 붙여줘 한글 프롬프트", "t1")])
    t = Tailer(p)
    assert len(t.read_new()) == 1
    _append(p, [_user("완료 보고 두번째 한글", "t2")])
    objs = t.read_new()
    assert len(objs) == 1 and objs[0]["message"]["content"] == "완료 보고 두번째 한글"


def test_tailer_seek_tail_prime(tmp_path):
    p = str(tmp_path / "e.jsonl")
    _write(p, [_user("x" * 100, f"t{i}") for i in range(200)])
    t = Tailer(p)
    t.seek_tail(max_bytes=500)                # 최근 500바이트 부근부터
    objs = t.read_new()
    assert 0 < len(objs) < 200                # 전체가 아니라 최근 일부만
    # 잘린 첫 줄을 버렸으므로 파싱 실패(깨진 줄) 없이 온전한 레코드만
    assert all("timestamp" in o for o in objs)


def test_tailer_missing_file(tmp_path):
    t = Tailer(str(tmp_path / "nope.jsonl"))
    assert t.read_new() == []
    t2 = Tailer(None)
    assert t2.read_new() == []


# ---- parse_calllines ----

def test_callline_group_member():
    group = {SUB}
    cmd = f'claude -p --resume {SUB} "로그인 붙여줘"'
    edges = parse_calllines(cmd, group)
    assert edges == [{"target": SUB, "prompt": "로그인 붙여줘"}]


def test_callline_variants_and_prefix():
    group = {SUB}
    for cmd in (
        f'claude --resume {SUB} -p "x"',                  # 플래그 순서 뒤바뀜
        f'cd "F:/portal" && claude -p --resume {SUB} "x"',  # prefix
        f"claude -p -r {SUB} 'single quote 프롬프트'",       # -r 축약 + 홑따옴표
    ):
        edges = parse_calllines(cmd, group)
        assert len(edges) == 1 and edges[0]["target"] == SUB


def test_callline_out_of_group_degrades():
    # 그룹 밖 UUID → 화살표 생략(빈 리스트). 크래시 없음.
    other = "12345678-1111-2222-3333-444444444444"
    edges = parse_calllines(f'claude -p --resume {other} "x"', {SUB})
    assert edges == []


def test_callline_no_prompt():
    edges = parse_calllines(f"claude -p --resume {SUB}", {SUB})
    assert edges == [{"target": SUB, "prompt": ""}]


def test_callline_ignores_non_claude():
    # UUID 를 언급해도 claude 명령이 아니면 호출선 아님
    edges = parse_calllines(f'echo --resume {SUB}', {SUB})
    assert edges == []


# ---- MonitorGroup ----

def _group(tmp_path):
    mgr_p = str(tmp_path / f"{MGR}.jsonl")
    sub_p = str(tmp_path / f"{SUB}.jsonl")
    open(mgr_p, "w").close()
    open(sub_p, "w").close()
    g = MonitorGroup([
        _Session(MGR, mgr_p, "관리", "#111", "manager"),
        _Session(SUB, sub_p, "포털", "#222", "sub"),
    ])
    return g, mgr_p, sub_p


def test_group_merge_orders_by_timestamp(tmp_path):
    g, mgr_p, sub_p = _group(tmp_path)
    _write(mgr_p, [_user("관리 t01", "2026-09-03T00:00:01Z"),
                   _asst_text("관리 t03", "2026-09-03T00:00:03Z")])
    _write(sub_p, [_asst_text("포털 t02", "2026-09-03T00:00:02Z")])
    out = g.poll()
    texts = [e["text"] for e in out]
    assert texts == ["관리 t01", "포털 t02", "관리 t03"]  # 시간축 병합
    # 세션 태깅 확인
    assert out[0]["session_label"] == "관리" and out[0]["session_id"] == MGR
    assert out[1]["session_label"] == "포털" and out[1]["color"] == "#222"


def test_group_call_edge(tmp_path):
    g, mgr_p, sub_p = _group(tmp_path)
    _write(mgr_p, [_asst_bash(f'claude -p --resume {SUB} "로그인 붙여줘"',
                              "2026-09-03T00:00:01Z", tid="tu9")])
    out = g.poll()
    ev = out[0]
    assert ev["calls_out"] == [{"target_session_id": SUB,
                                "prompt": "로그인 붙여줘", "tool_use_id": "tu9"}]


def test_group_call_edge_out_of_group_no_arrow(tmp_path):
    # 그룹 밖 세션을 부르면 이벤트는 나오되 화살표(calls_out) 없음(저하)
    g, mgr_p, sub_p = _group(tmp_path)
    other = "99999999-1111-2222-3333-444444444444"
    _write(mgr_p, [_asst_bash(f'claude -p --resume {other} "x"',
                              "2026-09-03T00:00:01Z")])
    out = g.poll()
    assert len(out) == 1
    assert out[0]["calls_out"] == []          # 화살표 생략, 이벤트는 유지


def test_group_snapshot_replay(tmp_path):
    g, mgr_p, sub_p = _group(tmp_path)
    _write(mgr_p, [_user("a", "t1"), _user("b", "t2")])
    g.poll()
    snap = g.snapshot()
    assert [e["text"] for e in snap] == ["a", "b"]


def test_group_ring_buffer_cap():
    g = MonitorGroup([])
    for i in range(_RING_CAP + 50):
        g._push({"seq": i})
    assert len(g.buffer) == _RING_CAP
    assert g.buffer[-1]["seq"] == _RING_CAP + 49   # 최신 유지
    assert g.buffer[0]["seq"] == 50                # 오래된 50개 버려짐


def test_group_seq_monotonic(tmp_path):
    g, mgr_p, sub_p = _group(tmp_path)
    _write(mgr_p, [_user("a", "t1")])
    _write(sub_p, [_user("b", "t2")])
    out = g.poll()
    seqs = [e["seq"] for e in out]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# ---- build_group (스캐너 경유) ----

def test_build_group_resolves_path_and_label(fake_claude_home):
    write_session(fake_claude_home, "F--portal", SUB, [
        {"type": "user", "cwd": "F:/portal", "slug": "brave-portal-fox",
         "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-09-03T00:00:00Z"},
    ])
    g = build_group([{"session_id": SUB, "role": "sub"}])
    assert len(g.sessions) == 1
    s = g.sessions[0]
    assert s.session_id == SUB
    assert s.path and s.path.endswith(f"{SUB}.jsonl")
    assert s.label == "brave-portal-fox"      # slug 폴백
    # 실제 tail 도 동작
    out = g.poll()
    assert out and out[0]["text"] == "hi"
