"""PTY 자기 등록부 - 고아 판정의 정확성(오인 정리 원리적 차단)이 핵심.

실사고: Host 재기동 후 살아남은 persist PTY 가 claude bg 에이전트로 승격돼
세션을 잠금. 등록부(우리가 낳은 pid+sid 기록) 기반이라 서명 추측이 없고,
PID 재사용은 명령줄의 세션 ID 대조로 걸러낸다.
"""
from session_manager import webterm


def test_registry_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("session_manager.config.data_dir", lambda: tmp_path)
    webterm._registry_add(111, "sid-aaa")
    webterm._registry_add(222, "sid-bbb")
    assert {e["pid"] for e in webterm._registry_load()} == {111, 222}
    webterm._registry_remove(111)
    assert [e["pid"] for e in webterm._registry_load()] == [222]
    # 같은 pid 재등록은 교체(중복 없음)
    webterm._registry_add(222, "sid-ccc")
    assert [e["sid"] for e in webterm._registry_load()] == ["sid-ccc"]


def test_pick_orphans_requires_alive_and_sid_match():
    entries = [{"pid": 10, "sid": "sid-aaa"},   # 살아있고 sid 일치 → 고아
               {"pid": 20, "sid": "sid-bbb"},   # 죽어 있음 → 무시
               {"pid": 30, "sid": "sid-ccc"}]   # PID 재사용(다른 프로세스) → 무시
    procs = [{"pid": 10, "cmdline": "claude.exe --resume sid-aaa --dangerously-skip-permissions"},
             {"pid": 30, "cmdline": "notepad.exe something-else"}]
    assert webterm.pick_orphans(entries, procs) == [10]


def test_sweep_resets_registry_even_when_clean(tmp_path, monkeypatch):
    monkeypatch.setattr("session_manager.config.data_dir", lambda: tmp_path)
    webterm._registry_save([])
    assert webterm.sweep_orphan_ptys() == 0     # 빈 등록부 - 프로세스 조회도 안 함
