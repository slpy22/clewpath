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


def test_display_title_falls_back_to_label_name():
    """claude 이름(custom/ai)이 없으면 우리 라벨명을 제목으로 - ID 폴백 전에.

    실사용자 사례: 에이전트 세션(claude 이름 없음)에 ClewPath 에서 이름을
    붙였는데 화면엔 ID 만 떴다. 라벨명 승격으로 해결(claude 파일 무접촉).
    """
    from session_manager.server import _display_title
    # ① claude 이름 없음 + 라벨명 있음 → 라벨명
    md = {"session_id": "abcd1234-x", "custom_title": None, "ai_title": None, "slug": None}
    assert _display_title(md, {"name": "북한법 에이전트"}) == "북한법 에이전트"
    # ② claude custom-title 있으면 그게 우선(라벨명 무시)
    md2 = {"session_id": "abcd1234-x", "custom_title": "클로드이름", "ai_title": None, "slug": None}
    assert _display_title(md2, {"name": "라벨이름"}) == "클로드이름"
    # ③ 둘 다 없으면 ID
    assert _display_title({"session_id": "abcd1234-x", "custom_title": None,
                           "ai_title": None, "slug": None}, None) == "abcd1234"
