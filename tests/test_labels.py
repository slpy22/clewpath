"""라벨 사이드카 저장소 테스트."""
from session_manager import labels
from tests.conftest import write_session


def test_set_get_and_clean(fake_claude_home):
    rec = labels.set_record("S1", labels=["a", "a", " b ", ""], name="이름")
    assert rec["labels"] == ["a", "b"]        # 중복/공백 정리
    assert rec["name"] == "이름"
    assert labels.get("S1")["labels"] == ["a", "b"]


def test_partial_update(fake_claude_home):
    labels.set_record("S1", labels=["x"], name="n")
    labels.set_record("S1", name="n2")        # labels 미제공 → 보존
    rec = labels.get("S1")
    assert rec["labels"] == ["x"]
    assert rec["name"] == "n2"


def test_counts_and_filter(fake_claude_home):
    labels.set_record("S1", labels=["work", "북한법"])
    labels.set_record("S2", labels=["work"])
    counts = {c["label"]: c["count"] for c in labels.label_counts()}
    assert counts["work"] == 2
    assert counts["북한법"] == 1
    assert labels.session_ids_with_label("work") == {"S1", "S2"}
    assert labels.session_ids_with_label("북한법") == {"S1"}


def test_clear_removes_record(fake_claude_home):
    labels.set_record("S1", labels=["x"])
    labels.set_record("S1", labels=[])        # 라벨/이름 모두 비면 레코드 삭제
    assert labels.get("S1") == {}
    assert labels.label_counts() == []


def test_original_jsonl_untouched(fake_claude_home):
    # 라벨은 사이드카에만 저장되고 세션 jsonl 은 건드리지 않는다.
    sid = "aaaa1111-2222-3333-4444-555566667777"
    jsonl = write_session(fake_claude_home, "C--proj", sid, [
        {"type": "user", "cwd": "C:\\proj",
         "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
    ])
    content = jsonl.read_bytes()
    labels.set_record(sid, labels=["tag"])
    assert jsonl.read_bytes() == content      # 원본 불변
    assert labels.get(sid)["labels"] == ["tag"]
