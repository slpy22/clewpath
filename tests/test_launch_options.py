"""새 세션 생성 옵션(docs/designs/session-control.md 4-1, 11 절): 허용 목록·거절·넓히기 판정·인자 순서,
생성 설정 저장과 다시 열 때 재적용(저장값이 skip 보다 우선 — 사장님 결정 D1 C), 라벨·삭제·복구·포크와의 수명."""
from __future__ import annotations

import pytest

from session_manager import session_meta, launch_options as lo, lifecycle, resume, store, webapi, webterm

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def home(fake_claude_home):
    return fake_claude_home


# ---------------------------------------------------------------- 허용 목록
@pytest.mark.parametrize("key", sorted(lo.REFUSED))
def test_refused_options_say_why(key):
    with pytest.raises(lo.LaunchError) as e:
        lo.parse({key: "x"})
    assert e.value.code == "refused_option" and lo.REFUSED[key] in e.value.message


def test_unknown_option_refused():
    with pytest.raises(lo.LaunchError) as e:
        lo.parse({"permissionMode": "plan"})
    assert e.value.code == "unknown_option"


@pytest.mark.parametrize("opts", [
    {"name": "--settings"}, {"model": "-x"}, {"append_system_prompt": "--mcp-config a"}, {"worktree": "-w"}, {"agent": "--x"},
    {"model": "a b"}, {"worktree": "a/b"}, {"agent": "a b"}, {"effort": "ultra"}, {"permission_mode": "yolo"},
    {"tools": ["--settings"]}, {"disallowed_tools": ["Bash Edit"]}, {"allowed_tools": ["Bash(a,b)"]},
    {"name": "a\x07b"},
])
def test_bad_values_refused(opts):
    with pytest.raises(lo.LaunchError):
        lo.parse(opts)


def test_add_dirs_absolute_and_existing(tmp_path):
    with pytest.raises(lo.LaunchError):
        lo.parse({"add_dirs": ["200_nk_law_center"]})
    with pytest.raises(lo.LaunchError) as e:
        lo.parse({"add_dirs": [str(tmp_path / "없음")]})
    assert e.value.code == "no_dir"
    assert lo.parse({"add_dirs": [str(tmp_path)]}).add_dirs == [str(tmp_path)]


def test_default_mode_is_manual_and_can_be_none():
    assert lo.parse({}).permission_mode == "manual"
    assert lo.parse({}, default_mode=None).permission_mode is None


@pytest.mark.parametrize("opts,wide", [
    ({}, []), ({"permission_mode": "plan"}, []), ({"permission_mode": "dontAsk"}, []),
    ({"disallowed_tools": ["Bash"], "tools": ["Read"], "effort": "high", "model": "opus"}, []),
    ({"permission_mode": "acceptEdits"}, ["permission_mode=acceptEdits"]),
    ({"permission_mode": "auto"}, ["permission_mode=auto"]),
    ({"permission_mode": "bypassPermissions"}, ["permission_mode=bypassPermissions"]),
    ({"allowed_tools": ["Bash(git *)"]}, ["allowed_tools"]),
    ({"agent": "reviewer"}, ["agent"]),
])
def test_widenings(opts, wide):
    assert lo.widenings(lo.parse(opts)) == wide


def test_widenings_add_dirs(tmp_path):
    assert lo.widenings(lo.parse({"add_dirs": [str(tmp_path)]})) == ["add_dirs"]


# ---------------------------------------------------------------- 인자
def test_prompt_is_last_and_list_options_come_first():
    """여러 값 옵션은 뒤의 값을 계속 먹는다 — 첫 지시가 도구 이름으로 먹히면 안 된다."""
    spec = lo.parse({"name": "일꾼", "tools": ["Read", "Edit"], "disallowed_tools": ["Bash(rm *)"], "effort": "high",
                     "model": "opus", "append_system_prompt": "짧게", "worktree": "w1"})
    argv = lo.new_argv("claude", SID, spec, "일 시작")
    assert argv[0] == "claude" and argv[-3:] == ["--session-id", SID, "일 시작"]
    assert argv[1:5] == ["--tools", "Read,Edit", "--disallowed-tools", "Bash(rm *)"]
    assert argv[argv.index("-n") + 1] == "일꾼" and argv[argv.index("--worktree") + 1] == "w1"
    assert argv[argv.index("--permission-mode") + 1] == "manual"


def test_empty_tools_means_no_tools_and_bypass_uses_skip_flag():
    argv = lo.new_argv("claude", SID, lo.parse({"tools": [], "permission_mode": "bypassPermissions"}))
    assert argv[1:3] == ["--tools", ""] and lo.SKIP_FLAG in argv and "--permission-mode" not in argv


def test_limits():
    with pytest.raises(lo.LaunchError) as e:
        lo.new_argv("claude", SID, lo.parse({}), "가" * (lo.PROMPT_MAX + 1))
    assert e.value.code == "too_long"
    spec = lo.parse({"append_system_prompt": "가" * lo.APPEND_MAX})
    with pytest.raises(lo.LaunchError) as e:
        lo.new_argv("claude", SID, spec, "나" * lo.PROMPT_MAX)
    assert e.value.code == "too_long", "하나씩은 괜찮아도 합계 상한에서 막힌다"


def test_cmd_wrapper_refuses_shell_chars():
    spec = lo.parse({"name": "a&b"})
    assert lo.new_argv("claude.exe", SID, spec)
    with pytest.raises(lo.LaunchError) as e:
        lo.new_argv("C:/npm/claude.cmd", SID, spec)
    assert e.value.code == "bad_char"


# ---------------------------------------------------------------- 저장·다시 열기
def test_no_record_keeps_todays_behavior(home):
    """회귀 계약: 저장값이 없는 세션의 재개 인자는 지금과 같다."""
    assert lo.resume_flags(SID, True) == [lo.SKIP_FLAG]
    assert lo.resume_flags(SID, False) == []


@pytest.mark.parametrize("mode", ["manual", "plan", "dontAsk"])
def test_stored_narrow_mode_beats_skip_true(home, mode):
    """권한 확인으로 만든 세션은 skip:true 로 띄워도 자동 허용이 되지 않는다(D1 C)."""
    lo.save(SID, lo.parse({"permission_mode": mode}), "mcp")
    flags = lo.resume_flags(SID, True)
    assert lo.SKIP_FLAG not in flags and flags == ["--permission-mode", mode]


def test_human_safe_choice_narrows_wide_stored_mode(home):
    lo.save(SID, lo.parse({"permission_mode": "bypassPermissions"}), "api")
    assert lo.resume_flags(SID, True) == [lo.SKIP_FLAG]
    assert lo.resume_flags(SID, False) == [], "화면의 '권한 확인'은 넓은 저장값을 좁힌다"


def test_resume_reapplies_tools_effort(home, tmp_path):
    lo.save(SID, lo.parse({"permission_mode": "plan", "effort": "max", "tools": ["Read"], "disallowed_tools": ["Bash"],
                           "add_dirs": [str(tmp_path)], "allowed_tools": ["Edit"], "agent": "rv"}), "mcp")
    f = lo.resume_flags(SID, True)
    for pair in (["--tools", "Read"], ["--disallowed-tools", "Bash"], ["--allowed-tools", "Edit"],
                 ["--add-dir", str(tmp_path)], ["--effort", "max"], ["--agent", "rv"], ["--permission-mode", "plan"]):
        i = f.index(pair[0])
        assert f[i:i + 2] == pair


def test_store_failure_does_not_widen(home, monkeypatch):
    lo.save(SID, lo.parse({"permission_mode": "manual"}), "mcp")

    def boom():
        raise store.StoreUnavailable("잠김")
    monkeypatch.setattr(store, "reader", boom)
    assert lo.resume_flags(SID, True) == [], "못 읽으면 권한 확인으로"
    assert lo.stored_model(SID) is None


def test_all_three_resume_paths_follow_stored_mode(home, monkeypatch):
    """재개 인자를 만드는 세 곳(터미널·바깥 터미널 명령·웹 재개)이 같은 규칙을 쓴다."""
    from tests.conftest import write_session
    write_session(home, "F--w", SID, [{"type": "user", "cwd": "F:\\w"}])
    lo.save(SID, lo.parse({"permission_mode": "plan", "model": "opus"}), "mcp")
    t = webterm._claude_argv(SID, skip_permissions=True)
    w = webapi._claude_argv(SID, skip_permissions=True)
    c = resume.resume_command(SID, skip_permissions=True)["command"]
    for argv in (t, w):
        assert lo.SKIP_FLAG not in argv and argv[argv.index("--permission-mode") + 1] == "plan"
        assert argv[argv.index("--model") + 1] == "opus"
    assert lo.SKIP_FLAG not in c and "--permission-mode plan" in c


def test_guardrail_permission_mode_still_wins_on_web_resume(home):
    lo.save(SID, lo.parse({"permission_mode": "plan"}), "mcp")
    argv = webapi._claude_argv(SID, skip_permissions=True, guardrails={"permission_mode": "dontAsk"})
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and argv.count("--permission-mode") == 1


def test_label_model_beats_stored_model(home):
    lo.save(SID, lo.parse({"model": "sonnet"}), "mcp")
    session_meta.set_record(SID, model="opus")
    argv = webterm._claude_argv(SID, skip_permissions=True)
    assert argv[argv.index("--model") + 1] == "opus", "사람이 고른 모델이 먼저"


def test_clearing_labels_keeps_launch_settings(home):
    """라벨을 다 지워도 권한 모드는 남는다 — 같은 레코드에 뒀다면 재개가 자동 허용으로 떴다(설계 검토 F4)."""
    session_meta.set_record(SID, tags=["가"], model="opus")
    lo.save(SID, lo.parse({"permission_mode": "manual"}), "mcp")
    session_meta.set_record(SID, tags=[], model=None)
    assert session_meta.get(SID) == {}
    assert lo.resume_flags(SID, True) == ["--permission-mode", "manual"]


def test_delete_and_restore_keep_launch_settings(home):
    """휴지통에 넣었다가 복구해도 권한 모드가 돌아온다 — 없으면 복구한 세션이 자동 허용으로 뜬다."""
    from tests.conftest import write_session
    write_session(home, "F--w", SID, [{"type": "user", "cwd": "F:\\w"}])
    lo.save(SID, lo.parse({"permission_mode": "plan"}), "mcp")
    r = lifecycle.delete_session(SID, dry_run=False)
    assert r.get("recoverable") and lo.load(SID) is None, "삭제하면 표에 유령 행이 안 남는다"
    from pathlib import Path
    assert lifecycle.restore_session(Path(r["trash"]).name).get("errors") in (None, [])
    assert lo.resume_flags(SID, True) == ["--permission-mode", "plan"]


def test_fork_inherits_parent_settings(home):
    lo.save(SID, lo.parse({"permission_mode": "manual", "disallowed_tools": ["Bash"]}), "mcp")
    lo.inherit(SID, "fork-1")
    assert lo.resume_flags("fork-1", True) == ["--disallowed-tools", "Bash", "--permission-mode", "manual"]
    lo.inherit("없는-세션", "fork-2")
    assert lo.load("fork-2") is None, "원본에 설정이 없으면 옛 동작"


def test_saved_record_has_no_prompt_or_name(home):
    lo.save(SID, lo.parse({"name": "일꾼", "append_system_prompt": "비밀 지시"}), "mcp")
    rec = lo.load(SID)
    assert "name" not in rec and "append_system_prompt" not in rec


# ---------------------------------------------------------------- start_new
@pytest.fixture
def fake_launch(monkeypatch):
    launched = []

    class P:
        pid = 2001

    def launch(key, sid, argv, cwd, persist, cap):
        launched.append(argv)
        s = webterm._TermSession(key, P(), persist=persist)
        webterm._ACTIVE[key] = s
        return s
    webterm._ACTIVE.clear()
    monkeypatch.setattr(webterm, "_launch", launch)
    yield launched
    webterm._ACTIVE.clear()


def test_start_new_with_spec_saves_and_reports(home, tmp_path, fake_launch):
    spec = lo.parse({"name": "일꾼", "effort": "low"})
    r = webterm.start_new(str(tmp_path), "시작", session_id=SID, launch=spec, source="mcp")
    assert r["permission_mode"] == "manual"
    argv = fake_launch[0]
    assert lo.SKIP_FLAG not in argv and argv[-1] == "시작"
    assert lo.load(SID)["source"] == "mcp" and lo.load(SID)["effort"] == "low"


@pytest.mark.parametrize("skip,mode", [(True, "bypassPermissions"), (False, "manual")])
def test_raw_start_new_records_mode_too(home, tmp_path, fake_launch, skip, mode):
    """옛 길로 만든 세션도 기록한다 — 권한 확인으로 만든 워커가 나중에 skip:true 로 넓어지지 않게."""
    webterm.start_new(str(tmp_path), "x", skip_permissions=skip, session_id=SID)
    assert lo.load(SID)["permission_mode"] == mode


def test_start_new_turns_bad_spec_into_term_error(home, tmp_path, fake_launch):
    with pytest.raises(webterm.TermStartError) as e:
        webterm.start_new(str(tmp_path), "가" * (lo.PROMPT_MAX + 1), session_id=SID, launch=lo.parse({}))
    assert e.value.code == "too_long" and not fake_launch


def test_raw_start_new_keeps_long_prompt(home, tmp_path, fake_launch):
    """상한은 새 길(허용 목록)에만 — 옛 워커 지시는 지금처럼 길어도 된다."""
    webterm.start_new(str(tmp_path), "가" * (lo.PROMPT_MAX + 1), session_id=SID)
    assert len(fake_launch) == 1
