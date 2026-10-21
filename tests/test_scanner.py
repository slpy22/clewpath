"""scanner 테스트 — fixture 기반."""
from session_manager import scanner


def test_scan_all_finds_session(sample_session):
    sessions = scanner.scan_all()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "11111111-2222-3333-4444-555555555555"
    assert s.project_folder == "F--021-test"


def test_meta_extracted_from_later_lines(sample_session):
    """cwd/slug가 첫 줄(사이드체인)이 아닌 이후 줄에서 추출되어야 한다."""
    sessions = scanner.scan_all()
    s = sessions[0]
    assert s.cwd == "F:\\021_test\\proj"
    assert s.slug == "happy-test-bee"
    assert s.git_branch == "main"
    assert s.version == "1.0.0"


def test_counts_and_timestamps(sample_session):
    s = scanner.scan_all()[0]
    assert s.message_count == 3
    assert s.line_count == 3
    assert s.started_at == "2026-06-01T00:00:00.000Z"
    assert s.ended_at == "2026-06-01T00:05:00.000Z"


def test_scan_one(sample_session):
    s = scanner.scan_one("11111111-2222-3333-4444-555555555555")
    assert s is not None
    assert s.slug == "happy-test-bee"
    assert scanner.scan_one("nonexistent") is None


def test_empty_when_no_projects(fake_claude_home):
    assert scanner.scan_all() == []


def test_malformed_lines_are_counted_but_skipped(fake_claude_home):
    from tests.conftest import write_session
    write_session(fake_claude_home, "P--x", "abc", [])
    jsonl = fake_claude_home / "projects" / "P--x" / "abc.jsonl"
    jsonl.write_text('not json\n{"type":"user","cwd":"X","timestamp":"t1"}\nbad\n', encoding="utf-8")
    s = scanner.scan_one("abc")
    assert s.line_count == 3       # 빈 줄 제외 전체
    assert s.message_count == 1    # 파싱 성공한 것만
    assert s.cwd == "X"


def test_picker_hidden_detection(fake_claude_home):
    """picker_hidden = 대화형 마커(mode/permission-mode/system) 유무.

    claude --resume 픽커는 '대화형으로 열린 적 있는 세션'만 나열한다(실측
    2026-08-26: gitBranch 동일 세 세션에서 이 마커 유무만이 픽커 표시를 갈랐다).
    ai-title 유무 기준은 반례(ai-title 있는 에이전트 세션)로 폐기됨.
    """
    from session_manager import scanner
    from tests.conftest import write_session
    U = lambda i: {"type": "user", "cwd": "F:\p",
                   "message": {"role": "user", "content": f"m{i}"},
                   "timestamp": f"2026-08-13T00:0{i}:00.000Z"}
    # ① 헤드리스/에이전트 산물: 대화형 마커 0, ai-title 있어도 hidden
    write_session(fake_claude_home, "P--x", "dddd4444",
                  [U(0), {"type": "ai-title", "aiTitle": "제목"}])
    # ② 대화형 세션: mode/system 마커 있으면 표시(제목 유무 무관)
    write_session(fake_claude_home, "P--x", "eeee5555",
                  [{"type": "system", "content": "init"}, U(0),
                   {"type": "mode", "mode": "default"}])
    # ③ 대화형이지만 제목 없음 → 그래도 표시(마커 기준)
    write_session(fake_claude_home, "P--x", "ffff6666",
                  [{"type": "permission-mode"}, U(0)])
    # ④ 빈 파일 → hidden 아님(목록에도 안 뜸, 오탐 방지)
    write_session(fake_claude_home, "P--x", "00007777", [])
    # ⑤ agent-name 만 있는 스텁(fork/named 산물) → 마커 0이어도 픽커에 뜸
    write_session(fake_claude_home, "P--x", "55558888",
                  [{"type": "ai-title", "aiTitle": "x"},
                   {"type": "agent-name", "agentName": "터널 스텁"}])
    metas = {m.session_id: m for m in scanner.scan_all()}
    assert metas["dddd4444"].picker_hidden is True    # 에이전트 산물(ai-title 무관)
    assert metas["eeee5555"].picker_hidden is False   # 대화형
    assert metas["ffff6666"].picker_hidden is False   # 대화형(제목 없어도)
    assert metas["00007777"].picker_hidden is False   # 빈 세션
    assert metas["55558888"].picker_hidden is False   # agent-name 스텁(픽커 뜸)


def test_headless_detection_and_stop_hook_summary(fake_claude_home):
    """claude -p 단발 기록(entrypoint sdk-cli)은 headless. Stop 훅의 stop_hook_summary system 줄은
    -p 에도 남으므로 대화형 마커로 세지 않는다(2026-10-12 번역 스크립트 56개가 일반 세션처럼 보인 사례)."""
    from tests.conftest import write_session
    Q = {"type": "queue-operation", "operation": "enqueue", "content": "translate"}
    U = {"type": "user", "cwd": "E:\c", "entrypoint": "sdk-cli", "message": {"role": "user", "content": "x"},
         "timestamp": "2026-10-12T04:09:24.579Z"}
    A = {"type": "assistant", "entrypoint": "sdk-cli", "message": {"model": "claude-opus-5"},
         "timestamp": "2026-10-12T04:09:30.000Z"}
    H = {"type": "system", "subtype": "stop_hook_summary", "entrypoint": "sdk-cli"}
    write_session(fake_claude_home, "P--x", "aaaa1111", [Q, U, A, H])
    ci = dict(U, entrypoint="cli")
    write_session(fake_claude_home, "P--x", "bbbb2222",
                  [{"type": "permission-mode"}, ci, dict(H, entrypoint="cli")])
    # 대화형 세션을 SDK 로 이어 쓴 경우(cli+sdk 혼재) → headless 아님
    write_session(fake_claude_home, "P--x", "cccc3333", [{"type": "mode"}, ci, U])
    # entrypoint 가 없는 옛 기록 → headless 아님
    write_session(fake_claude_home, "P--x", "dddd4444", [{"type": "user", "cwd": "E:\c"}])
    m = {x.session_id: x for x in scanner.scan_all()}
    assert m["aaaa1111"].headless is True and m["aaaa1111"].picker_hidden is True
    assert m["bbbb2222"].headless is False and m["bbbb2222"].picker_hidden is False
    assert m["cccc3333"].headless is False
    assert m["dddd4444"].headless is False
    # 다른 system 줄(turn_duration 등)은 계속 대화형 마커
    write_session(fake_claude_home, "P--x", "eeee5555", [{"type": "system", "subtype": "turn_duration"}, U])
    assert scanner.scan_one("eeee5555").picker_hidden is False


def test_v1_list_hides_finished_headless(fake_claude_home, monkeypatch):
    """claude -p 단발 기록은 목록에서 뺀다(2026-10-12 사장님) — 지금 살아 있는 것만 보이고,
    include_headless=1 로는 진단용으로 볼 수 있다. 일반 세션은 그대로."""
    from fastapi.testclient import TestClient
    from tests.conftest import write_session
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    monkeypatch.setenv("SM_API_TOKEN", "t-123")
    H = {"Authorization": "Bearer t-123"}
    U = lambda ep: {"type": "user", "cwd": "E:\c", "entrypoint": ep, "timestamp": "2026-10-12T04:00:00.000Z"}
    write_session(fake_claude_home, "E--c", "aaaa1111", [U("sdk-cli")])
    write_session(fake_claude_home, "E--c", "aaaa2222", [U("sdk-cli")])
    write_session(fake_claude_home, "E--c", "bbbb1111", [{"type": "permission-mode"}, U("cli")])
    monkeypatch.setattr("session_manager.peers.peer_map",
                        lambda: {"aaaa2222": {"name": "p", "status": "busy", "pid": 1}})
    from session_manager.server import create_app
    with TestClient(create_app()) as c:
        rows = c.get("/api/v1/sessions", headers=H).json()["sessions"]
        ids = {s["session_id"] for s in rows}
        assert ids == {"aaaa2222", "bbbb1111"}, "끝난 -p 기록은 빠지고, 지금 도는 것·일반 세션은 보인다"
        b = next(s for s in rows if s["session_id"] == "bbbb1111")
        assert b["cleanup_at"] == int(b["mtime"] + 30 * 86400), "claude 가 지울 즈음 = 마지막 수정 + 기본 30일"
        ids2 = {s["session_id"] for s in c.get("/api/v1/sessions?include_headless=true", headers=H).json()["sessions"]}
        assert "aaaa1111" in ids2
        # 라벨로 찾으면 끝난 -p 기록도 보인다(0.24.1 — 서빙 원본은 -p 로 만든다)
        from session_manager import labels
        labels.set_record("aaaa1111", name="서빙", labels=["서빙 원본"])
        hit = c.get("/api/v1/sessions?label=서빙 원본&limit=1", headers=H).json()["sessions"]
        assert [s["session_id"] for s in hit] == ["aaaa1111"]
        assert "aaaa1111" not in {s["session_id"] for s in c.get("/api/v1/sessions", headers=H).json()["sessions"]}
        local = [s["session_id"] for p in c.get("/api/sessions", headers=H).json()["projects"] for s in p["sessions"]]
        assert "aaaa1111" not in local and "bbbb1111" in local


def test_bg_shell_monitor_tracking(fake_claude_home, monkeypatch):
    """백그라운드 쉘·모니터: 도구 결과 앞머리로 시작, <task-notification> status·TaskStop 으로 끝.
    인용된 글귀(다른 도구 출력 속)는 세지 않고, 프로세스 시작 전 작업은 죽은 것으로 본다."""
    from tests.conftest import write_session
    from session_manager import bgtasks

    def res(text, ts):
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": text}]}}
    lines = [
        {"type": "permission-mode"},
        res("Command running in background with ID: bsh1. Output is being written to: x", "2026-10-12T05:00:00Z"),
        res("Command did not complete within its 120s timeout and was moved to the background (ID: bsh2). Output", "2026-10-12T05:00:01Z"),
        res("Monitor started (task bmo1, timeout 90000ms). You will be notified", "2026-10-12T05:00:02Z"),
        res("Monitor started (task bmo2, timeout 90000ms).", "2026-10-12T05:00:03Z"),
        res("grep output: Command running in background with ID: bquoted", "2026-10-12T05:00:04Z"),
        {"type": "attachment", "timestamp": "2026-10-12T05:01:00Z", "attachment": {"type": "queued_command",
         "prompt": "<task-notification>\n<task-id>bsh1</task-id>\n<status>completed</status>\n</task-notification>"}},
        {"type": "user", "timestamp": "2026-10-12T05:01:01Z", "message": {"role": "user",
         "content": "<task-notification>\n<task-id>bmo2</task-id>\n<summary>Monitor event</summary>\n<event>READY</event>\n</task-notification>"}},
        res('{"message":"Successfully stopped task: bmo2 (until …)","task_id":"bmo2"}', "2026-10-12T05:02:00Z"),
        res("Command running in background with ID: bold. Output", "2026-10-12T04:00:00Z"),
    ]
    write_session(fake_claude_home, "P--x", "ffff0001", lines)
    m = scanner.scan_one("ffff0001")
    assert sorted(x[0] for x in m.bg_open) == ["bmo1", "bold", "bsh2"], "모니터 이벤트(상태 없음)로는 안 끝난다"
    since = scanner._epoch("2026-10-12T04:30:00Z") * 1000
    c = bgtasks.counts(m, {"started_at": since})
    assert c == {"shells": 1, "monitors": 1}, "프로세스 시작 전(bold) 작업은 함께 죽었다"
    assert bgtasks.counts(m, None) is None
    rt = {"phase": "ready", "ready_at": 1}
    assert bgtasks.hold_ready(rt, c)["phase"] == "background" and rt["phase"] == "ready"
    assert bgtasks.hold_ready(rt, {"shells": 0, "monitors": 0}) is rt
    assert bgtasks.hold_ready({"phase": "thinking"}, c)["phase"] == "thinking"


def test_bg_monitor_expiry_ends(fake_claude_home):
    """모니터 만료 알림(<status> 없이 사건 모양)도 끝이다 — 2026-10-21 삼흥_데이터추출: 만료된 모니터 9개가 남은 것으로
    세여 '백그라운드'·완료 푸시 보류·4시간 정리 건너뜀. 만료 문구가 사건 맨 앞일 때만, 모니터에만 — 감시 출력 속 글귀·인용
    (다른 알림의 <status>·<task-id> 포함)은 끝이 아니다."""
    from tests.conftest import write_session

    def res(text, ts):
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": text}]}}

    def note(tid, event, ts, queued=False):
        body = (f'<task-notification>\n<task-id>{tid}</task-id>\n<summary>Monitor event: "끝 또는 오류"</summary>\n'
                f"<event>{event}</event>\n</task-notification>")
        if queued:
            return {"type": "attachment", "timestamp": ts, "attachment": {"type": "queued_command", "prompt": body}}
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": body}}
    expires = "expires in 30m unless the source ends first; you get one notice at expiry — re-arm if you still need the watch"
    lines = [
        {"type": "permission-mode"},
        res(f"Monitor started (task bexp1, {expires}). You will be notified on each event.", "2026-10-21T01:00:00Z"),
        res(f"Monitor started (task bexp2, {expires}).", "2026-10-21T01:00:01Z"),
        res("Monitor started (task bold1, timeout 90000ms). You will be notified", "2026-10-21T01:00:02Z"),
        res("Monitor started (task blive, persistent — runs until TaskStop or session end).", "2026-10-21T01:00:03Z"),
        res(f"Monitor started (task bwatch, {expires}).", "2026-10-21T01:00:04Z"),
        res(f"Monitor started (task bwatch2, {expires}).", "2026-10-21T01:00:05Z"),
        res("Monitor started (task bexp3, expires in 20m unless the source ends first; …).", "2026-10-21T01:00:06Z"),
        res("Command running in background with ID: bsh9. Output is being written to: x", "2026-10-21T01:00:07Z"),
        note("bexp1", "26/52  2.9s/항목", "2026-10-21T01:10:00Z"),
        note("bexp1", "[Monitor expired after 30m with 1 events delivered. Re-arm it if you still need the watch.]",
             "2026-10-21T01:30:00Z"),
        note("bexp2", "[Monitor expired after 30m with no events delivered. Re-arm it if you still need the watch"
             " — and widen the filter if silence was unexpected.]", "2026-10-21T01:30:01Z", queued=True),
        note("bold1", "[Monitor timed out — re-arm if needed.]", "2026-10-21T01:30:02Z"),
        # 감시 출력 줄에 같은 글귀가 섞였을 뿐(맨 앞 아님)
        note("blive", "[01:31:00] [대백과 서비스] [Monitor expired after 30m with 2 events delivered.]", "2026-10-21T01:31:00Z"),
        # 다른 기록(jsonl)을 지켜보다 그 안의 만료 알림을 옮긴 사건 — 지켜보는 모니터도, 인용된 작업도 끝이 아니다
        note("bwatch", '{"content":"<task-notification>\\n<task-id>blive</task-id>\\n'
             '<event>[Monitor expired after 30m with no events delivered.]</event>"}', "2026-10-21T01:32:00Z"),
        # 인용된 완료 알림의 <status> — 알림 머리(첫 <event> 앞)가 아니므로 bwatch2 도, 인용된 blive 도 끝이 아니다
        note("bwatch2", '{"content":"<task-notification>\\n<task-id>blive</task-id>\\n<status>completed</status>"}',
             "2026-10-21T01:33:00Z"),
        note("bexp3", "[Monitor expired after 20m with no events delivered. Re-arm it if you still need the watch.]",
             "2026-10-21T01:33:01Z"),
        # 만료는 모니터에만 — 쉘 작업 알림에 같은 글귀가 와도 끝이 아니다(쉘의 끝은 <status>)
        note("bsh9", "[Monitor expired after 30m with no events delivered.]", "2026-10-21T01:33:02Z"),
    ]
    write_session(fake_claude_home, "P--x", "ffff0002", lines)
    m = scanner.scan_one("ffff0002")
    assert sorted(x[0] for x in m.bg_open) == ["blive", "bsh9", "bwatch", "bwatch2"], \
        "만료(두 문구, 20m·30m, 본문·첨부)는 끝, 사건 중간·인용·쉘은 끝 아님"
    assert scanner.bg_counts(m, scanner._epoch("2026-10-21T00:59:00Z")) == {"shells": 1, "monitors": 3}


def test_stop_push_held_while_background(monkeypatch):
    from session_manager import push, bgtasks
    sent = []
    monkeypatch.setattr(push, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(push, "_notify_groups", lambda sid, ev: False)
    monkeypatch.setattr(bgtasks, "counts_by_id", lambda sid: {"shells": 1, "monitors": 0})
    push.notify_from_event({"session_id": "s1", "hook_event_name": "Stop", "cwd": "E:/p"})
    assert sent == [], "쉘이 남아 있으면 '작업 완료' 를 보내지 않는다"
    monkeypatch.setattr(bgtasks, "counts_by_id", lambda sid: {"shells": 0, "monitors": 0})
    push.notify_from_event({"session_id": "s1", "hook_event_name": "Stop", "cwd": "E:/p"})
    assert sent and sent[0][0] == "ready"
