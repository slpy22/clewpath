"""관제 그룹 저장소(mongroups) — 사이드카 CRUD 와 훅 라우팅 조회 계약."""
from __future__ import annotations

import json

from session_manager import mongroups

M = "aaaaaaaa-1111-2222-3333-444444444444"
S1 = "bbbbbbbb-1111-2222-3333-444444444444"
S2 = "cccccccc-1111-2222-3333-444444444444"


def test_save_get_list_and_roles(fake_claude_home):
    g = mongroups.save("포털 프로젝트", M, [S1, S2, S1, M, ""], labels={M: "관리", S1: "포털"})
    assert g["id"] and g["name"] == "포털 프로젝트"
    assert g["subs"] == [S1, S2]                       # 중복·빈값·관리 제외
    assert g["notify"] == mongroups.DEFAULT_NOTIFY     # 기본 알림 플래그
    assert mongroups.get(g["id"])["manager"] == M
    assert [x["id"] for x in mongroups.list_groups()] == [g["id"]]
    assert mongroups.groups_for_session(M)[0][1] == "manager"
    assert mongroups.groups_for_session(S2)[0][1] == "sub"
    assert mongroups.groups_for_session("nope") == []
    assert mongroups.label_of(g, S1) == "포털" and mongroups.label_of(g, S2) == S2[:8]


def test_name_defaults_to_manager_label(fake_claude_home):
    g = mongroups.save("", M, [S1], labels={M: "관리 에이전트"})
    assert g["name"] == "관리 에이전트"
    g2 = mongroups.save("  ", S1, [])
    assert g2["name"] == S1[:8]


def test_manager_required(fake_claude_home):
    import pytest
    with pytest.raises(ValueError):
        mongroups.save("x", "", [S1])


def test_update_notify_and_name_partial(fake_claude_home):
    g = mongroups.save("g", M, [S1])
    u = mongroups.update(g["id"], notify={"sub_stop": False, "bogus": True})
    assert u["notify"] == {"manager_stop": True, "sub_stop": False, "sub_start": False, "error": True}
    u = mongroups.update(g["id"], name="새 이름")
    assert u["name"] == "새 이름" and u["notify"]["sub_stop"] is False   # 이전 플래그 유지
    assert mongroups.update("missing", name="x") is None


def test_replace_with_gid_keeps_id(fake_claude_home):
    g = mongroups.save("g", M, [S1])
    g2 = mongroups.save("g2", M, [S2], gid=g["id"])
    assert g2["id"] == g["id"] and g2["subs"] == [S2]
    assert len(mongroups.list_groups()) == 1


def test_delete(fake_claude_home):
    g = mongroups.save("g", M, [S1])
    assert mongroups.delete(g["id"]) is True
    assert mongroups.delete(g["id"]) is False
    assert mongroups.groups_for_session(M) == []


def test_corrupt_file_is_tolerated(fake_claude_home):
    p = mongroups.groups_file(); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert mongroups.list_groups() == []
    g = mongroups.save("g", M, [S1])                    # 덮어쓰고 정상 동작
    assert json.loads(p.read_text(encoding="utf-8"))["groups"][g["id"]]["name"] == "g"


def test_group_cap(fake_claude_home, monkeypatch):
    import pytest
    monkeypatch.setattr(mongroups, "_MAX_GROUPS", 2)
    mongroups.save("a", M, []); mongroups.save("b", S1, [])
    with pytest.raises(ValueError):
        mongroups.save("c", S2, [])
