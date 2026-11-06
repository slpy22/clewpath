"""lifecycle 테스트 — 삭제/cwd변경. fixture 기반, 실제 데이터 안 건드림."""
import json
import os
from pathlib import Path

import pytest

from session_manager import lifecycle, config
from tests.conftest import write_session


def _make(home, sid="aaaa1111", folder="P--x"):
    write_session(home, folder, sid, [
        {"type": "user", "cwd": "F:\\old\\path", "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
        {"type": "assistant", "cwd": "F:\\old\\path", "message": {"role": "assistant", "content": "yo"},
         "timestamp": "2026-06-01T00:01:00.000Z"},
    ])
    return home / "projects" / folder / f"{sid}.jsonl"


def test_delete_dry_run_lists_targets(sample_session):
    res = lifecycle.delete_session("11111111-2222-3333-4444-555555555555", dry_run=True)
    assert res["dry_run"] is True
    paths = [t["path"] for t in res["would_delete"]]
    assert any(p.endswith(".jsonl") for p in paths)


def test_delete_actually_removes(fake_claude_home):
    jsonl = _make(fake_claude_home)
    assert jsonl.exists()
    res = lifecycle.delete_session("aaaa1111", dry_run=False)
    assert not jsonl.exists()
    assert any("aaaa1111.jsonl" in p for p in res["deleted"])


def test_delete_is_recoverable_via_trash(fake_claude_home):
    """삭제는 하드 삭제가 아니라 휴지통 이동 — restore 로 되살릴 수 있어야 한다(오삭제 방지)."""
    jsonl = _make(fake_claude_home)
    res = lifecycle.delete_session("aaaa1111", dry_run=False)
    assert res["recoverable"] is True
    assert not jsonl.exists()               # 원본 위치는 비었지만
    trash = lifecycle.list_trash()
    assert len(trash) == 1 and trash[0]["session_id"] == "aaaa1111"
    # 복구하면 원본이 되살아난다
    rr = lifecycle.restore_session(trash[0]["bucket"])
    assert jsonl.exists()
    assert any("aaaa1111.jsonl" in p for p in rr["restored"])
    assert lifecycle.list_trash() == []     # 복구 후 버킷 정리


def test_restore_does_not_overwrite_existing(fake_claude_home):
    """같은 경로가 이미 있으면 덮어쓰지 않고 건너뛴다(복구가 현재 것을 지우면 안 됨)."""
    jsonl = _make(fake_claude_home)
    trash = (lifecycle.delete_session("aaaa1111", dry_run=False), lifecycle.list_trash())[1]
    _make(fake_claude_home)                 # 같은 id 로 새 세션이 다시 생김
    rr = lifecycle.restore_session(trash[0]["bucket"])
    assert rr["restored"] == []
    assert any("이미 존재" in s for s in rr["skipped"])


def test_delete_includes_side_dir_and_session_env(fake_claude_home):
    jsonl = _make(fake_claude_home)
    # 사이드 폴더
    side = jsonl.parent / "aaaa1111"
    side.mkdir()
    (side / "x.txt").write_text("x")
    # session-env
    senv = config.session_env_dir()
    senv.mkdir(parents=True, exist_ok=True)
    (senv / "aaaa1111").write_text("env")

    res = lifecycle.delete_session("aaaa1111", dry_run=False)
    assert not side.exists()
    assert not (senv / "aaaa1111").exists()


def test_change_cwd_dry_run(fake_claude_home):
    _make(fake_claude_home)
    res = lifecycle.change_cwd("aaaa1111", "F:\\new\\path", dry_run=True)
    assert res["dry_run"] is True
    assert res["lines_with_cwd"] == 2
    assert res["old_cwd"] == "F:\\old\\path"


def test_change_cwd_applies_and_backs_up(fake_claude_home):
    jsonl = _make(fake_claude_home)
    res = lifecycle.change_cwd("aaaa1111", "F:\\new\\path", dry_run=False)
    assert res["lines_changed"] == 2
    # 백업 존재
    assert Path(res["backup"]).exists()
    # 실제 치환 확인
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert json.loads(line)["cwd"] == "F:\\new\\path"


def test_change_cwd_preserves_unparseable_lines(fake_claude_home):
    jsonl = _make(fake_claude_home)
    # 깨진 줄 추가
    with jsonl.open("a", encoding="utf-8") as f:
        f.write("not json line\n")
    lifecycle.change_cwd("aaaa1111", "F:\\new", dry_run=False)
    content = jsonl.read_text(encoding="utf-8")
    assert "not json line" in content  # 보존됨


def test_no_silent_modification_of_claude_files():
    """[원칙 회귀 방지] claude 세션 파일을 무단 수정하는 기능이 되살아나면 안 된다.

    은닉 유지(scrub_auto_titles: 재개 후 ai-title 자동 제거)는 claude 자체 동작에
    영향을 주는 무단 파일 수정이라 원칙 위반으로 제거됐다(2026-08-13 확정).
    같은 이름의 함수가 다시 생기면 이 테스트가 막는다 - 부활시키려면
    '사용자 사전 고지+승인' 게이트와 함께 원칙 협의를 먼저 할 것.
    """
    assert not hasattr(lifecycle, "scrub_auto_titles")


def test_trash_api_is_local_only(fake_claude_home):
    """[원칙] 휴지통 API 는 로컬 전용 - 원격(비루프백)에서는 403."""
    from fastapi.testclient import TestClient
    from session_manager.server import create_app
    c = TestClient(create_app())
    # TestClient 기본 호스트는 'testclient'(비루프백) → 원격 취급
    assert c.get("/api/owner/trash").status_code == 403
    assert c.post("/api/owner/trash/x/restore").status_code == 403


def test_delete_removes_sidecar_labels(fake_claude_home, monkeypatch, tmp_path):
    """삭제 시 ClewPath 라벨도 함께 제거(B안) - 유령 라벨 누적 방지."""
    from session_manager import lifecycle, labels
    monkeypatch.setattr("session_manager.config.data_dir", lambda: tmp_path)
    jsonl = _make(fake_claude_home, sid="lbl99999")
    labels.set_record("lbl99999", name="지울이름", labels=["태그"])
    assert labels.get("lbl99999")                       # 라벨 존재
    lifecycle.delete_session("lbl99999", dry_run=False)
    assert not jsonl.exists()
    assert labels.get("lbl99999") == {}                 # 라벨도 제거됨


def test_move_session_relocates_file_and_cwd(fake_claude_home, monkeypatch, tmp_path):
    """세션 이사: jsonl 을 새 cwd 인코딩 폴더로 이동 + cwd 치환.

    claude 는 '파일이 어느 인코딩 폴더에 있는지'로 세션을 찾으므로, 폴더 이동은
    파일 물리 이동이 필수(실증 2026-08-31). cwd 만 바꾸던 옛 방식의 갭 해소.
    """
    from session_manager import lifecycle
    from session_manager.pathenc import path_to_folder
    # cwd=F:\old\path 인 세션 생성(_make 는 그 cwd 로 만든다)
    jsonl = _make(fake_claude_home, sid="mv111111", folder=path_to_folder(r"F:\old\path"))
    assert jsonl.exists()
    target = str(tmp_path / "new" / "home")           # 이 OS 의 전체 경로(맥에선 F:\... 가 전체 경로가 아니다)
    r = lifecycle.move_session("mv111111", target, move_content=False, dry_run=False)
    assert "error" not in r, r
    # 원본 위치 비고, 새 인코딩 폴더에 존재
    assert not jsonl.exists()
    new_jsonl = (fake_claude_home / "projects" / path_to_folder(target)
                 / "mv111111.jsonl")
    assert new_jsonl.exists()
    # cwd 치환 확인
    import json as _j
    kinds = [_j.loads(l) for l in new_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(o.get("cwd") == target for o in kinds if "cwd" in o)


@pytest.mark.parametrize("bad", ["200_nk_law_center", r"sub\dir", "./x", "..", "", "   "]
                         + ([r"E:foo", r"\foo", "/foo"] if os.name == "nt" else []))
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("move_content", [True, False])
def test_move_session_rejects_non_absolute(fake_claude_home, monkeypatch, tmp_path, bad, dry_run, move_content):
    """전체 경로가 아니면 미리보기든 실행이든 거절하고 아무것도 옮기지 않는다(2026-11-06 '남북비교' 사고)."""
    from session_manager import lifecycle
    work = tmp_path / "host-cwd"                      # 상대 경로가 풀릴 곳 — 여기에 아무것도 생기면 안 된다
    work.mkdir()
    monkeypatch.chdir(work)
    jsonl = _make(fake_claude_home, sid="mv333333")
    before = jsonl.read_bytes()
    projects = sorted(p.name for p in (fake_claude_home / "projects").iterdir())
    r = lifecycle.move_session("mv333333", bad, move_content=move_content, dry_run=dry_run)
    assert "전체 경로" in r.get("error", ""), r
    assert jsonl.read_bytes() == before
    assert sorted(p.name for p in (fake_claude_home / "projects").iterdir()) == projects
    assert list(work.iterdir()) == []


def test_move_session_normalizes_target(fake_claude_home, tmp_path):
    """끝의 구분자·'..'·앞뒤 공백은 정리해서 적는다 — 표기가 다르면 기록 폴더 이름이 달라져 claude 가 못 찾는다."""
    import json as _j

    from session_manager import lifecycle
    from session_manager.pathenc import path_to_folder
    clean = str(tmp_path / "new" / "home")
    messy = f"  {tmp_path / 'new' / 'x' / '..' / 'home'}{os.sep}  "
    _make(fake_claude_home, sid="mv444444")
    plan = lifecycle.move_session("mv444444", messy, dry_run=True)
    assert plan["new_cwd"] == clean, plan
    r = lifecycle.move_session("mv444444", messy, dry_run=False)
    assert r["new_cwd"] == clean, r
    moved = fake_claude_home / "projects" / path_to_folder(clean) / "mv444444.jsonl"
    rows = [_j.loads(l) for l in moved.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(o["cwd"] == clean for o in rows if "cwd" in o)


@pytest.mark.parametrize("status", ["busy", "idle"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_move_session_blocks_session_live_outside_clewpath(fake_claude_home, tmp_path, status, dry_run):
    """바깥 터미널에 떠 있는 세션(claude 레지스트리)도 이동 차단 — 기록이 둘로 갈라진다(2026-11-06)."""
    from session_manager import lifecycle
    jsonl = _make(fake_claude_home, sid="mv555555")
    before = jsonl.read_bytes()
    reg = fake_claude_home / "sessions"
    reg.mkdir()
    (reg / "4242.json").write_text(json.dumps({"sessionId": "mv555555", "status": status, "updatedAt": 1}),
                                   encoding="utf-8")
    r = lifecycle.move_session("mv555555", str(tmp_path / "new"), dry_run=dry_run)
    assert "실행 중" in r.get("error", ""), r
    assert jsonl.read_bytes() == before


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"])
def test_move_session_writes_lf(fake_claude_home, tmp_path, eol):
    """이동한 기록은 LF 로 쓴다 — Windows 텍스트 모드가 CRLF 로 바꾸던 것, 이미 CRLF 인 기록도 LF 로 돌아온다."""
    from session_manager import lifecycle
    from session_manager.pathenc import path_to_folder
    jsonl = _make(fake_claude_home, sid="mv666666")
    jsonl.write_bytes(jsonl.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", eol))
    target = str(tmp_path / "new")
    r = lifecycle.move_session("mv666666", target, dry_run=False)
    assert "error" not in r, r
    data = (fake_claude_home / "projects" / path_to_folder(target) / "mv666666.jsonl").read_bytes()
    assert b"\r" not in data and data.endswith(b"\n")


def test_move_session_blocks_live(fake_claude_home, monkeypatch):
    """실행 중 세션은 이동 차단(파일 이동 중 손상 방지)."""
    from session_manager import lifecycle, webterm
    _make(fake_claude_home, sid="mv222222")
    monkeypatch.setattr(webterm, "has_terminal", lambda sid: True)
    r = lifecycle.move_session("mv222222", r"F:\x", dry_run=False)
    assert "error" in r and "실행 중" in r["error"]


def test_delete_fork_pending_then_retry(fake_claude_home, monkeypatch):
    """API fork 가 끝날 때 아직 '살아 있음'으로 보이면 대기 목록에 적고, 정기 작업이 나중에 지운다(0.23.9 — 10-19 fork 6건 누적)."""
    jsonl = _make(fake_claude_home, sid="fork0001")
    live = {"fork0001": "idle"}
    monkeypatch.setattr(lifecycle, "_live_reason", lambda sid: live.get(sid))
    d = lifecycle.delete_fork("fork0001", origin="base", wait_live_s=0)
    assert d["pending"] is True and jsonl.exists()
    pend = lifecycle._fork_pending_load()
    assert pend["fork0001"]["reason"] == "idle" and pend["fork0001"]["origin"] == "base"
    assert lifecycle._fork_pending_file().parent == config.data_dir()

    r = lifecycle.retry_forks()                      # 아직 살아 있으면 남겨 둔다
    assert r["kept"] == ["fork0001"] and jsonl.exists()

    live.clear()                                      # 프로세스가 끝나 레지스트리가 사라짐
    r = lifecycle.retry_forks()
    assert r["deleted"] == ["fork0001"] and not jsonl.exists()
    assert lifecycle._fork_pending_load() == {}
    assert lifecycle.list_trash()[0]["session_id"] == "fork0001"   # 휴지통 이동(복구 가능)


def test_delete_fork_immediate_and_gone(fake_claude_home, monkeypatch):
    monkeypatch.setattr(lifecycle, "_live_reason", lambda sid: None)
    jsonl = _make(fake_claude_home, sid="fork0002")
    d = lifecycle.delete_fork("fork0002")
    assert d["deleted"] and not d.get("pending") and not jsonl.exists()
    assert lifecycle._fork_pending_load() == {}
    # 대기 목록에 있었는데 다른 경로로 이미 사라진 fork 는 목록에서만 뺀다
    lifecycle._fork_pending_save({"gone0003": {"since": 0, "tries": 1}})
    r = lifecycle.retry_forks()
    assert r["gone"] == ["gone0003"] and lifecycle._fork_pending_load() == {}


def test_change_cwd_writes_lf(fake_claude_home):
    """옛 cwd 치환도 LF 로 쓴다(2026-11-07, 세션 이사와 같은 원인)."""
    jsonl = _make(fake_claude_home)
    lifecycle.change_cwd("aaaa1111", "F:\new", dry_run=False)
    assert b"\r" not in jsonl.read_bytes()
