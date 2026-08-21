"""claude 에이전트 점유 스냅샷 - bg 세션 잠금 가시화의 근거 데이터.

실사용자 사고(2026-08-21): 잊힌 bg 에이전트(--bg)가 세션을 점유해
--resume 이 거부됐고, 원인이 어디에도 표시되지 않았다. occupancy_map 이
세션 목록에 조인되어 배지·재개 게이트가 이를 예고한다.
"""
from session_manager import agents

FIXTURE = [
    {"pid": "111", "kind": "interactive", "sessionId": "s-int", "name": "터미널A", "status": "busy"},
    {"pid": "222", "kind": "background", "sessionId": "s-bg", "name": "dbcommon",
     "status": "waiting", "waitingFor": "input needed"},
    {"pid": "333", "kind": "interactive", "name": "세션ID 없음", "status": "idle"},
]


def test_occupancy_map_joins_by_session(monkeypatch):
    monkeypatch.setattr(agents, "_fetch", lambda: FIXTURE)
    agents._cache["at"] = 0.0                      # 캐시 무효화
    m = agents.occupancy_map()
    assert set(m) == {"s-int", "s-bg"}             # sessionId 없는 항목은 제외
    assert m["s-bg"]["kind"] == "background"
    assert m["s-bg"]["waiting_for"] == "input needed"
    assert agents.holder_of("s-bg")["name"] == "dbcommon"
    assert agents.holder_of("없는세션") is None


def test_fetch_failure_degrades_to_empty(monkeypatch):
    """CLI 미지원/실패 환경에서도 기능 축소만(빈 목록) - 크래시 금지."""
    monkeypatch.setattr(agents, "_fetch", lambda: (_ for _ in ()).throw(RuntimeError))
    agents._cache["at"] = 0.0
    try:
        agents.snapshot()
    except RuntimeError:
        import pytest
        pytest.fail("snapshot 이 예외를 밖으로 던지면 안 됨")
