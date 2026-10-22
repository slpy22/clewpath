"""재개 명령 안내(맥 실기 3b): Windows 는 cmd(cd /d "…"), 맥·리눅스는 sh(cd '…'), 새 창 열기 단추는 Windows 만."""
from session_manager import osdep, resume
from tests.conftest import write_session

SID = "12345678-aaaa-bbbb-cccc-1234567890ab"


def _sess(home, cwd):
    write_session(home, "x-proj", SID, [
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-10-22T00:00:01.000Z"},
        {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": "ok"},
         "timestamp": "2026-10-22T00:00:02.000Z"}])


def test_posix_command_is_for_sh(fake_claude_home, monkeypatch):
    monkeypatch.setattr(osdep, "IS_WIN", False)
    monkeypatch.setattr("session_manager.config.claude_exe", lambda: "/Users/u/my tools/claude")
    _sess(fake_claude_home, "/Users/u/it's here")
    r = resume.resume_command(SID)
    assert r["full_command"] == ("cd '/Users/u/it'\"'\"'s here' && '/Users/u/my tools/claude' --resume "
                                 f"{SID} --dangerously-skip-permissions"), r
    assert r["shell"] == "sh" and r["can_launch"] is False


def test_windows_command_is_for_cmd(fake_claude_home, monkeypatch):
    monkeypatch.setattr(osdep, "IS_WIN", True)
    monkeypatch.setattr("session_manager.config.claude_exe", lambda: "C:\\Users\\u\\.local\\bin\\claude.exe")
    _sess(fake_claude_home, "F:\\w\\proj")
    r = resume.resume_command(SID, skip_permissions=False)
    assert r["full_command"] == f'cd /d "F:\\w\\proj" && C:\\Users\\u\\.local\\bin\\claude.exe --resume {SID}', r
    assert r["shell"] == "cmd" and r["can_launch"] is True


def test_no_cwd_is_just_the_command(fake_claude_home, monkeypatch):
    monkeypatch.setattr(osdep, "IS_WIN", False)
    monkeypatch.setattr("session_manager.config.claude_exe", lambda: "claude")
    write_session(fake_claude_home, "x-proj", SID, [
        {"type": "user", "message": {"role": "user", "content": "hi"}, "timestamp": "2026-10-22T00:00:01.000Z"}])
    r = resume.resume_command(SID)
    assert r["full_command"] == r["command"] == f"claude --resume {SID} --dangerously-skip-permissions"
