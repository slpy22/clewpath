"""Claude 훅 연동 회귀 - 등록의 비파괴성(사용자 settings.json 보존)이 핵심.

settings.json 은 사용자의 전역 Claude 설정이다. 여기가 병합이 아니라
덮어쓰기가 되는 순간 사용자의 기존 훅/설정을 날리는 사고가 된다.
그래서 '등록된다' 만큼 '남의 것을 안 건드린다'를 못박는다.
"""
from __future__ import annotations

import json

import pytest

from session_manager import hooks


@pytest.fixture(autouse=True)
def _clean_status():
    hooks._STATUS.clear()
    yield
    hooks._STATUS.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(tmp_path))
    return tmp_path


# ---- 등록 ----

def test_register_fresh(home):
    assert hooks.ensure_registered(5100) is True
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    for ev in hooks.EVENTS:
        cmds = [h["command"] for g in st["hooks"][ev] for h in g["hooks"]]
        assert any("/api/hooks/event" in c for c in cmds), ev
    cmd = hooks.hook_command(5100)
    assert "127.0.0.1:5100/api/hooks/event" in cmd
    # Claude 는 Windows 에서도 훅을 bash 로 실행한다(실측) - 셸 불문 조건:
    assert "\\" not in cmd                          # 백슬래시 경로 금지(bash 가 삼킴)
    assert ">" not in cmd                           # 리다이렉션 금지(셸별 문법 다름)
    assert cmd.endswith("|| exit 0")                # 실패해도 Claude 를 방해 안 함


def test_register_is_idempotent(home):
    hooks.ensure_registered(5100)
    assert hooks.ensure_registered(5100) is False   # 두 번째는 no-op
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    for ev in hooks.EVENTS:
        ours = [h for g in st["hooks"][ev] for h in g["hooks"]
                if "/api/hooks/event" in h["command"]]
        assert len(ours) == 1, f"{ev} 중복 등록"


def test_register_migrates_legacy_cmd_file_entry(home):
    """구형(.cmd 파일 경로) 등록은 bash 에서 안 돌았다 - 인라인 명령으로 교체."""
    hooks.settings_path().parent.mkdir(parents=True, exist_ok=True)
    legacy = str(hooks.hook_cmd_path())
    hooks.settings_path().write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": legacy}]}]},
    }), encoding="utf-8")
    assert hooks.ensure_registered(5100) is True
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    cmds = [h["command"] for g in st["hooks"]["Stop"] for h in g["hooks"]]
    assert all(hooks.HOOK_CMD_NAME not in c for c in cmds)   # 구형 제거
    assert any("/api/hooks/event" in c for c in cmds)         # 신형 교체
    ours = [c for c in cmds if "/api/hooks/event" in c]
    assert len(ours) == 1, "교체이지 추가가 아니어야"


def test_register_preserves_existing(home):
    """기존 훅·다른 설정 키를 절대 건드리지 않는다."""
    hooks.settings_path().parent.mkdir(parents=True, exist_ok=True)
    hooks.settings_path().write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-thing.exe"}]}]},
    }), encoding="utf-8")
    hooks.ensure_registered(5100)
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    assert st["model"] == "opus"
    stop_cmds = [h["command"] for g in st["hooks"]["Stop"] for h in g["hooks"]]
    assert "user-thing.exe" in stop_cmds            # 사용자 훅 보존
    assert any("/api/hooks/event" in c for c in stop_cmds)   # 우리 것 추가
    # 백업이 남는다
    assert hooks.settings_path().with_name("settings.json.bak-clewpath").is_file()


def test_register_skips_broken_settings(home):
    """깨진 settings.json 은 덮어쓰지 않는다(데이터 손실 방지)."""
    hooks.settings_path().parent.mkdir(parents=True, exist_ok=True)
    hooks.settings_path().write_text("{ broken", encoding="utf-8")
    assert hooks.ensure_registered(5100) is False
    assert hooks.settings_path().read_text(encoding="utf-8") == "{ broken"


def test_register_port_change_updates_command(home):
    hooks.ensure_registered(5100)
    assert hooks.ensure_registered(5101) is True    # 포트 변경 -> 명령 갱신
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    cmds = [h["command"] for g in st["hooks"]["Stop"] for h in g["hooks"]]
    assert any("127.0.0.1:5101" in c for c in cmds)
    assert all("127.0.0.1:5100" not in c for c in cmds)


def test_register_reads_bom_settings(home):
    """PS 5.1 등이 남긴 BOM 있는 settings.json 도 읽는다."""
    hooks.settings_path().parent.mkdir(parents=True, exist_ok=True)
    hooks.settings_path().write_bytes(b'\xef\xbb\xbf{"model": "opus"}')
    assert hooks.ensure_registered(5100) is True
    st = json.loads(hooks.settings_path().read_text(encoding="utf-8"))
    assert st["model"] == "opus"


# ---- 이벤트 -> 상태 ----

def _ev(name, sid="s1", **extra):
    return {"hook_event_name": name, "session_id": sid, **extra}


def test_prompt_then_stop_cycle():
    hooks.update_from_event(_ev("UserPromptSubmit"))
    assert hooks.status_of("s1")["phase"] == "thinking"
    hooks.update_from_event(_ev("Stop"))
    st = hooks.status_of("s1")
    assert st["phase"] == "ready" and st["ready_at"] > 0


def test_permission_notification():
    hooks.update_from_event(_ev("UserPromptSubmit"))
    hooks.update_from_event(_ev(
        "Notification", message="Claude needs your permission to use Bash"))
    st = hooks.status_of("s1")
    assert st["phase"] == "permission"
    assert "permission" in st["note"].lower()
    # 응답하고 나면 권한대기가 걷힌다
    hooks.update_from_event(_ev("UserPromptSubmit"))
    assert "permission_at" not in hooks.status_of("s1")


def test_waiting_notification_default():
    hooks.update_from_event(_ev("Notification", message="Claude is waiting for your input"))
    assert hooks.status_of("s1")["phase"] == "waiting"


def test_session_end():
    hooks.update_from_event(_ev("SessionStart"))
    hooks.update_from_event(_ev("SessionEnd"))
    assert hooks.status_of("s1")["phase"] == "ended"


def test_missing_session_id_ignored():
    hooks.update_from_event({"hook_event_name": "Stop"})
    assert hooks.status_map() == {}


def test_memory_cap(monkeypatch):
    monkeypatch.setattr(hooks, "_MAX_SESSIONS", 3)
    for i in range(5):
        hooks.update_from_event(_ev("SessionStart", sid=f"s{i}"))
    assert len(hooks.status_map()) <= 3
