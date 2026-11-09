"""세션 덧붙임 저장소(태그·재개 전 알림 끄기) — 코드는 tags, 디스크는 옛 판 되돌림을 위해 labels·picker 키."""
import json

import pytest

from session_manager import config, session_meta
from tests.conftest import write_session

SID = "11111111-2222-3333-4444-555555555555"   # conftest.sample_session


def _disk(sid):
    return json.loads(config.labels_file().read_text(encoding="utf-8"))["sessions"][sid]


def test_set_get_and_clean(fake_claude_home):
    rec = session_meta.set_record("S1", tags=["a", "a", " b ", ""])
    assert rec["tags"] == ["a", "b"]        # 중복/공백 정리
    assert session_meta.get("S1")["tags"] == ["a", "b"]


def test_partial_update(fake_claude_home):
    session_meta.set_record("S1", tags=["x"])
    session_meta.set_record("S1", keep="1")     # tags 미제공 → 보존
    rec = session_meta.get("S1")
    assert rec["tags"] == ["x"] and rec["keep"] == "1"


def test_counts_and_filter(fake_claude_home):
    session_meta.set_record("S1", tags=["work", "북한법"])
    session_meta.set_record("S2", tags=["work"])
    counts = {c["tag"]: c["count"] for c in session_meta.tag_counts()}
    assert counts == {"work": 2, "북한법": 1}
    assert session_meta.session_ids_with_tag("work") == {"S1", "S2"}
    assert session_meta.session_ids_with_tag("북한법") == {"S1"}


def test_empty_record_is_deleted(fake_claude_home):
    session_meta.set_record("S1", tags=["x"])
    session_meta.set_record("S1", tags=[])      # 전부 비면 레코드 삭제
    assert session_meta.get("S1") == {}
    assert session_meta.tag_counts() == []


def test_original_jsonl_untouched(fake_claude_home):
    sid = "aaaa1111-2222-3333-4444-555566667777"
    jsonl = write_session(fake_claude_home, "C--proj", sid, [
        {"type": "user", "cwd": "C:\\proj",
         "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
    ])
    content = jsonl.read_bytes()
    session_meta.set_record(sid, tags=["tag"])
    assert jsonl.read_bytes() == content      # 원본 불변
    assert session_meta.get(sid)["tags"] == ["tag"]


@pytest.mark.parametrize("field", ["name", "color", "note", "labels", "picker"])
def test_removed_or_old_field_names_are_refused(fake_claude_home, field):
    """옛 이름으로 부르는 코드가 조용히 무시되지 않게."""
    with pytest.raises(TypeError):
        session_meta.set_record("S1", **{field: "x"})


def test_disk_shape_stays_for_rollback(fake_claude_home):
    """옛 판은 labels.json 의 labels·picker 를 읽는다 — 저장 키를 바꾸면 되돌림이 깨진다."""
    session_meta.set_record("S1", tags=["a"], skip_resume_notice=True)
    d = _disk("S1")
    assert d["labels"] == ["a"] and d["picker"] == "expose" and "tags" not in d and "skip_resume_notice" not in d


def test_removed_values_stay_on_disk_but_are_not_read(fake_claude_home):
    """name·color·note 는 읽지 않되 지우지도 않는다(옛 판으로 되돌리면 그대로)."""
    session_meta._save({"version": 1, "sessions": {"S1": {"labels": ["a"], "name": "옛이름", "color": "#000", "note": "메모", "updated_at": "t"}}})
    rec = session_meta.get("S1")
    assert rec["tags"] == ["a"] and not {"name", "color", "note", "labels"} & set(rec)
    assert not {"name", "color", "note"} & set(session_meta.all_records()["S1"])
    session_meta.set_record("S1", tags=[])
    d = _disk("S1")
    assert d["name"] == "옛이름" and d["color"] == "#000" and d["note"] == "메모", "태그를 다 지워도 옛 값은 남는다"
    assert session_meta.raw("S1")["name"] == "옛이름"


def test_skip_resume_notice_on_off(fake_claude_home):
    session_meta.set_record("S1", skip_resume_notice=True)
    assert session_meta.get("S1")["skip_resume_notice"] is True
    session_meta.set_record("S1", skip_resume_notice=False)
    assert session_meta.get("S1") == {}


def test_display_title_has_no_clewpath_name():
    from session_manager.server import _display_title
    md = {"session_id": "abcd1234-x", "custom_title": "클로드이름", "ai_title": "AI", "slug": "s"}
    assert _display_title(md) == "클로드이름"
    assert _display_title({**md, "custom_title": None}) == "AI"
    assert _display_title({**md, "custom_title": None, "ai_title": None}) == "s"
    assert _display_title({"session_id": "abcd1234-x", "custom_title": None, "ai_title": None, "slug": None}) == "abcd1234"


# ---------------------------------------------------------------- 경로: 새 이름 + 옛 이름 별칭
def _app(monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager import housekeeping, server as S
    monkeypatch.setattr(housekeeping, "start", lambda *a, **k: None)
    monkeypatch.setattr(S, "_client_host", lambda r: "127.0.0.1")
    return TestClient(S.create_app())


def _row(c, query=""):
    rows = c.get("/api/v1/sessions" + query).json()["sessions"]
    return next((s for s in rows if s["session_id"] == SID), None)


def _local_row(c):
    return next(s for p in c.get("/api/sessions").json()["projects"] for s in p["sessions"] if s["session_id"] == SID)


def test_tag_routes_new_and_old_names(sample_session, monkeypatch):
    with _app(monkeypatch) as c:
        r = c.post(f"/api/sessions/{SID}/tags", json={"tags": ["a", "b"]}).json()
        assert r["session_id"] == SID and r["record"]["tags"] == ["a", "b"]
        row = _row(c)
        assert row["tags"] == row["labels"] == ["a", "b"], "옛 앱이 읽는 이름도 함께"
        assert "label_name" not in row and "label_color" not in row
        assert _local_row(c)["tags"] == ["a", "b"] and "label_name" not in _local_row(c)
        assert c.get("/api/tags").json() == {"tags": [{"tag": "a", "count": 1}, {"tag": "b", "count": 1}]}
        assert c.get("/api/v1/tags").json() == c.get("/api/tags").json()
        assert c.get("/api/labels").json() == {"labels": [{"label": "a", "count": 1}, {"label": "b", "count": 1}]}
        assert c.get("/api/v1/labels").json() == c.get("/api/labels").json()
        old = c.post(f"/api/v1/sessions/{SID}/labels", json={"labels": ["옛"], "name": "무시", "color": "#000", "note": "x"}).json()
        assert old["record"]["labels"] == ["옛"], "옛 호출은 옛 모양으로 받는다"
        assert not {"name", "color", "note"} & set(_disk(SID))
        assert c.post(f"/api/v1/sessions/{SID}/tags", json={"tags": ["새"]}).json()["record"]["tags"] == ["새"]
        assert _row(c, "?tag=새") and _row(c, "?label=새"), "tag 와 옛 이름 label 둘 다 거른다"
        assert _row(c, "?tag=없음") is None


def test_omitted_tags_keep_existing(sample_session, monkeypatch):
    with _app(monkeypatch) as c:
        c.post(f"/api/sessions/{SID}/tags", json={"tags": ["a"]})
        c.post(f"/api/sessions/{SID}/labels", json={"name": "이름만 보낸 옛 호출"})
        assert _row(c)["tags"] == ["a"]


def test_resume_notice_reaches_the_list_screen_uses(sample_session, monkeypatch):
    """화면이 쓰는 목록(/api/v1/sessions)에 이 값이 빠져 있어, 꺼도 목록이 새로 오면 다시 경고가 떴다(2026-11-09)."""
    with _app(monkeypatch) as c:
        assert _row(c)["skip_resume_notice"] is False
        assert c.post(f"/api/sessions/{SID}/resume-notice", json={"skip": True}).json() == {"session_id": SID, "skip_resume_notice": True}
        row = _row(c)
        assert row["skip_resume_notice"] is True and row["picker_expose"] is True
        assert _local_row(c)["skip_resume_notice"] is True
        assert c.post(f"/api/sessions/{SID}/picker-expose", json={"expose": False}).json() == {"session_id": SID, "picker_expose": False}
        assert _row(c)["skip_resume_notice"] is False
