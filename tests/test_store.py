"""상태 DB(store) — 데이터 저장 정리 R1(docs/designs/data-store.md 확정 설계 변경).

지키는 것: 쓰기 연결 하나(D3) · pc_id='local' + pcs 표(D4) · 거울 쓰기 순서와 기동 대조(D5) · team.db 를 키움(D6) ·
바깥 응답 그대로(D7) · worklog 영구(O3) · 옛 worklog.db 합치기(O1) · 손상 복구(O5·O6) · 세션 표 전수(T8) · 훅 쓰기(D2).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from session_manager import config, store


@pytest.fixture
def home(fake_claude_home):
    store.close()
    yield fake_claude_home
    store.close()


def _dd():
    return config.data_dir()


# ---------------------------------------------------------------- 연결·형식
def test_one_writer_connection_and_schema_v7(home):
    w1 = store.writer()
    w2 = store.writer()
    assert w1 is w2                                              # 쓰기 연결은 하나
    r = store.reader()
    try:
        assert r is not w1
        assert r.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 8
        assert r.execute("SELECT name FROM sqlite_master WHERE name='launch_settings'").fetchone(), "v7 생성 설정 표"
        assert r.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        r.close()
    assert store.db_path().name == "team.db"                     # R1 은 team.db 를 키운다(D6)


def test_nested_tx_uses_savepoint(home):
    with store.tx() as c:
        c.execute("INSERT INTO kv(key,value_json) VALUES('a','1')")
        with pytest.raises(RuntimeError):
            with store.tx() as c2:
                c2.execute("INSERT INTO kv(key,value_json) VALUES('b','2')")
                raise RuntimeError("안쪽만 되돌림")
    assert store.kv_get("a") == 1 and store.kv_get("b") is None


def test_clewpath_db_name_wins_when_present(home):
    store.writer()
    store.close()
    (_dd() / "team.db").rename(_dd() / "clewpath.db")
    assert store.db_path().name == "clewpath.db"
    assert store.writer() is not None


# ---------------------------------------------------------------- PC 키(D4)
def test_local_pc_row_and_host_id_change(home):
    (_dd() / "pclink").mkdir(parents=True)
    (_dd() / "pclink" / "identity.json").write_text(json.dumps({"host_id": "h-111"}), encoding="utf-8")
    from session_manager import session_meta
    session_meta.set_record("s-pc", model="이름")
    assert store.local_host_id() == "h-111"
    (_dd() / "pclink" / "identity.json").write_text(json.dumps({"host_id": "h-222"}), encoding="utf-8")
    store.close()
    assert session_meta.get("s-pc")["model"] == "이름"                  # 세션 행은 그대로
    assert store.local_host_id() == "h-222"                     # pcs 한 칸만 바뀜
    r = store.reader()
    try:
        assert r.execute("SELECT pc_id FROM session_meta WHERE session_id='s-pc'").fetchone()[0] == "local"
    finally:
        r.close()


def test_no_identity_file_is_created(home):
    store.writer()
    assert not (_dd() / "pclink" / "identity.json").exists()    # pclink 신원은 읽기만


def test_team_tables_get_pc_id_but_responses_do_not(home):
    from session_manager import team
    t = team.create_team("팀", manager_session="s-mgr")
    r = store.reader()
    try:
        cols = {x[1] for x in r.execute("PRAGMA table_info('agent_sessions')")}
        assert "pc_id" in cols
        assert r.execute("SELECT pc_id FROM agent_sessions WHERE session_id='s-mgr'").fetchone()[0] == "local"
    finally:
        r.close()
    blob = json.dumps(team.get_team(t["id"]), ensure_ascii=False) + json.dumps(team.list_teams(), ensure_ascii=False)
    assert "pc_id" not in blob                                   # D7: 바깥 응답 그대로


def test_old_host_style_sql_still_works_on_v6(home):
    """옛 판(형식 5)이 되돌림 뒤 같은 파일을 쓴다 — 열 이름 있는 INSERT·SELECT 가 그대로 돼야 한다."""
    store.writer()
    c = sqlite3.connect(str(store.db_path()))
    c.execute("INSERT INTO agent_sessions(agent_id,session_id,started,reason) VALUES('ag','s-old',1,'created')")
    c.execute("INSERT INTO usage(session_id,message_id,output) VALUES('s-old','m1',3)")
    c.commit()
    assert c.execute("SELECT pc_id FROM agent_sessions WHERE session_id='s-old'").fetchone()[0] == "local"
    c.close()


# ---------------------------------------------------------------- 거울(D5)
def test_mirror_written_in_old_format(home):
    from session_manager import session_meta
    session_meta.set_record("s1", model="가", tags=["x"])
    raw = json.loads(config.labels_file().read_text(encoding="utf-8"))
    assert raw["version"] == 1 and raw["sessions"]["s1"]["model"] == "가"
    assert [p.name for p in _dd().iterdir() if p.name.endswith(".tmp")] == []


def test_old_reader_sees_mirror_after_every_write(home):
    """되돌림 흉내: 옛 판은 labels.json 을 직접 읽는다 — 쓸 때마다 같은 값이어야 한다."""
    from session_manager import session_meta, profiles
    session_meta.set_record("s1", model="하나")
    profiles.set_record("s1", model="fable")
    session_meta.set_record("s1", tags=["a", "b"])
    old = json.loads(config.labels_file().read_text(encoding="utf-8"))["sessions"]["s1"]
    assert old == session_meta.raw("s1")
    oldp = json.loads((_dd() / "profiles.json").read_text(encoding="utf-8"))["sessions"]["s1"]
    assert oldp == profiles.get("s1")


def _meta(name):
    r = store.reader()
    try:
        return dict(r.execute("SELECT hash, pending_hash, prev_hash FROM doc_meta WHERE name=?", (name,)).fetchone())
    finally:
        r.close()


def test_old_version_wrote_file_is_imported(home):
    """거울 뒤 파일이 바뀌었다 = 되돌아간 옛 판이 쓴 최신 값 → 그 파일 기준으로 다시 채우고 사고 기록."""
    from session_manager import session_meta
    session_meta.set_record("s1", model="새판")
    store.close()                                                # 새 판 종료 → 옛 판이 씀
    p = config.labels_file()
    d = json.loads(p.read_text(encoding="utf-8"))
    d["sessions"]["s1"]["model"] = "옛판에서 바꿈"
    d["sessions"]["s2"] = {"labels": ["옛"], "updated_at": "x"}
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert session_meta.get("s1")["model"] == "옛판에서 바꿈"
    assert session_meta.get("s2")["tags"] == ["옛"]
    inc = (_dd() / "incidents.jsonl").read_text(encoding="utf-8")
    assert "mirror_imported" in inc


def test_crash_after_json_replace_before_clearing_marker(home):
    """③ 전에 죽음: 표식이 남았지만 파일 = 새 해시 → 정상(표식만 지움), 값 그대로."""
    from session_manager import session_meta
    session_meta.set_record("s1", model="가")
    m = _meta("labels")
    with store.tx() as c:                                        # 표식만 되살린다
        c.execute("UPDATE doc_meta SET pending_hash=hash, prev_hash='old' WHERE name='labels'")
    store.close()
    assert session_meta.get("s1")["model"] == "가"
    assert _meta("labels")["pending_hash"] is None and _meta("labels")["hash"] == m["hash"]


def test_crash_after_db_commit_before_json_is_ambiguous(home):
    """② 전에 죽음: 표식 있음 + 파일 = 직전 해시 → 애매함. 자동으로 덮지 않고 양쪽 보존(파일 사본) + DB 로 거울 다시 + 알림."""
    from session_manager import session_meta
    session_meta.set_record("s1", model="가")
    before = config.labels_file().read_bytes()
    with store.tx() as c:                                        # ① 만 된 상태를 만든다(DB 는 '나', 파일은 '가')
        d = json.loads(before)
        d["sessions"]["s1"]["model"] = "나"
        store._db_write_doc(c, store.docs()["labels"], d)
        c.execute("UPDATE doc_meta SET prev_hash=hash, pending_hash='new-hash-never-written' WHERE name='labels'")
    store.close()
    assert session_meta.get("s1")["model"] == "나"                      # DB 값 유지
    assert json.loads(config.labels_file().read_text(encoding="utf-8"))["sessions"]["s1"]["model"] == "나"   # 거울 다시
    kept = list((_dd() / "backups" / "mirror").glob("labels.json.ambiguous.*"))
    assert kept and kept[0].read_bytes() == before                # 파일 값도 보존
    assert any(n["event"] == "mirror_ambiguous" for n in store.notices())


def test_corrupt_mirror_keeps_db_and_rewrites(home):
    from session_manager import session_meta
    session_meta.set_record("s1", model="가")
    store.close()
    config.labels_file().write_text("{깨짐", encoding="utf-8")
    assert session_meta.get("s1")["model"] == "가"
    assert json.loads(config.labels_file().read_text(encoding="utf-8"))["sessions"]["s1"]["model"] == "가"
    assert list((_dd() / "backups" / "mirror").glob("labels.json.corrupt.*"))


def test_missing_mirror_is_rewritten_from_db(home):
    from session_manager import session_meta
    session_meta.set_record("s1", model="가")
    store.close()
    config.labels_file().unlink()
    assert session_meta.get("s1")["model"] == "가"
    assert config.labels_file().exists()


# ---------------------------------------------------------------- 옛 파일 가져오기
def _legacy_fixture(dd):
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "labels.json").write_text(json.dumps({"version": 1, "sessions": {
        "s1": {"labels": ["일"], "model": "첫째", "keep": "1", "updated_at": "t"},
        "s2": {"labels": [], "model": "fable", "updated_at": "t"}},
        "folders": {"f:/a": {"standalone": True, "cwd": "F:/a", "updated_at": "t"}}}, ensure_ascii=False), encoding="utf-8")
    (dd / "profiles.json").write_text(json.dumps({"version": 1, "sessions": {"s1": {"model": "opus", "denied_tools": ["Bash"]}}}),
                                      encoding="utf-8")
    (dd / "services.json").write_text(json.dumps({"services": {"nk": {"id": "nk", "status": "active", "token_hash": "sha256:x"}},
                                                  "requests": {}}), encoding="utf-8")
    (dd / "api_usage.json").write_text(json.dumps({"svc:nk:2026-11-05": {"calls": 3, "usd": 0.5}, "abc:2026-11-04": {"calls": 1, "usd": 0.0}}),
                                       encoding="utf-8")
    (dd / "housekeeping.json").write_text(json.dumps({"jobs": {"reap": {"last_run": 5}}, "probe_sessions": ["p1"], "idle_hours": 4}),
                                          encoding="utf-8")
    (dd / "fork_pending.json").write_text(json.dumps({"f1": {"since": 1, "tries": 2}}), encoding="utf-8")
    (dd / "hooks-status.json").write_text(json.dumps({"s1": {"phase": "ready", "active_at": 9}}), encoding="utf-8")
    (dd / "monitor_groups.json").write_text(json.dumps({"version": 1, "groups": {"g1": {"id": "g1", "name": "그룹"}}}, ensure_ascii=False),
                                            encoding="utf-8")
    w = sqlite3.connect(str(dd / "worklog.db"))
    w.executescript("""CREATE TABLE ws(session_id TEXT PRIMARY KEY, path TEXT, cwd TEXT, title TEXT, off INTEGER NOT NULL DEFAULT 0,
      head TEXT, sig TEXT, n INTEGER NOT NULL DEFAULT 0, last_ts INTEGER, gone_at INTEGER, updated INTEGER);
      CREATE TABLE turns(session_id TEXT NOT NULL, n INTEGER NOT NULL, ts INTEGER, request TEXT, result TEXT, result_ts INTEGER,
      PRIMARY KEY(session_id, n));""")
    w.execute("INSERT INTO ws VALUES('gone1','p','C:/x','지워진 세션',10,'h','s',2,100,50,100)")
    w.execute("INSERT INTO turns VALUES('gone1',1,90,'요청1','답1',91)")
    w.execute("INSERT INTO turns VALUES('gone1',2,100,'요청2','답2',101)")
    w.commit()
    w.close()
    from session_manager import team
    t = sqlite3.connect(str(dd / "team.db"))
    t.executescript(team._SCHEMA + team._SCHEMA_V2 + team._SCHEMA_V3 + team._SCHEMA_V4 + team._SCHEMA_V5)
    t.execute("PRAGMA user_version=5")
    t.execute("INSERT INTO teams(id,code,name,created,kind) VALUES('tm_1','OLD','옛 팀',1,'team')")
    t.commit()
    t.close()


def _snapshot():
    from session_manager import housekeeping, session_meta, lifecycle, mongroups, policy, profiles, services, worklog
    return {"labels": session_meta.all_records(), "folders": sorted(session_meta.standalone_folders()), "profiles": profiles.all_records(),
            "services": services._load(), "usage": policy._load_usage(), "hk": housekeeping._load(),
            "hidden": sorted(housekeeping.hidden_session_ids()), "fork": lifecycle._fork_pending_load(),
            "groups": mongroups._load(), "turns": worklog.session_turns("gone1")}


def test_legacy_import_same_values_and_idempotent(home):
    _legacy_fixture(_dd())
    from session_manager import hooks, team
    hooks._STATUS.clear()
    hooks._persist_loaded = False
    a = _snapshot()
    assert a["labels"]["s1"]["model"] == "첫째" and a["folders"] == ["f:/a"]
    assert a["profiles"]["s1"]["denied_tools"] == ["Bash"]
    assert a["usage"]["svc:nk:2026-11-05"]["calls"] == 3 and a["usage"]["abc:2026-11-04"]["calls"] == 1
    assert a["hk"]["idle_hours"] == 4 and a["hidden"] == ["p1"] and a["fork"] == {"f1": {"since": 1, "tries": 2}}
    assert a["groups"]["groups"]["g1"]["name"] == "그룹"
    assert a["turns"]["found"] and a["turns"]["gone_at"] == 50 and [t["request"] for t in a["turns"]["turns"]] == ["요청1", "요청2"]
    assert hooks.status_of("s1")["phase"] == "ready"
    assert team.list_teams()[0]["code"] == "OLD"
    assert (_dd() / "backups" / "pre-store" / "team-v5.db").exists()
    assert (_dd() / "backups" / "pre-store" / "labels.json").exists()
    store.close()
    assert _snapshot() == a                                      # 두 번째 기동 — 같은 값(중복 없음)
    r = store.reader()
    try:
        assert r.execute("SELECT COUNT(*) FROM worklog_turns WHERE session_id='gone1'").fetchone()[0] == 2
    finally:
        r.close()


def test_worklog_db_written_by_old_version_is_merged(home):
    """되돌림 동안 옛 판이 worklog.db 에 더 쓴 턴 → 다시 올라오면 합친다(O1)."""
    _legacy_fixture(_dd())
    from session_manager import worklog
    assert worklog.session_turns("gone1")["turns_total"] == 2
    store.close()
    time.sleep(0.02)
    w = sqlite3.connect(str(_dd() / "worklog.db"))
    w.execute("UPDATE ws SET n=3, updated=200 WHERE session_id='gone1'")
    w.execute("INSERT INTO turns VALUES('gone1',3,110,'옛판 요청','옛판 답',111)")
    w.commit()
    w.close()
    t = worklog.session_turns("gone1")
    assert t["turns_total"] == 3 and t["turns"][-1]["request"] == "옛판 요청"


def test_worklog_survives_source_deletion(home, monkeypatch):
    """원본 jsonl 이 지워진 세션의 장부도 그대로 조회된다(O3 — 캐시로 내리지 않음)."""
    _legacy_fixture(_dd())
    from session_manager import worklog
    worklog.collect(metas=[])                                    # 원본 없음 → gone_at 만
    t = worklog.session_turns("gone1")
    assert t["found"] and len(t["turns"]) == 2


# ---------------------------------------------------------------- 손상 복구(O5·O6)
def test_corrupt_db_without_backup_is_unavailable(home):
    from session_manager import team
    p = store.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not a sqlite file at all" * 100)
    with pytest.raises(team.TeamError) as e:
        team.list_teams()
    assert e.value.status == 503
    assert p.read_bytes().startswith(b"not a sqlite")            # 자동으로 덮지 않는다


def test_corrupt_db_restored_from_backup_with_offsets_reset_and_search_rebuilt(home):
    from session_manager import team
    team.create_team("팀", manager_session="s-m")
    with store.tx() as c:
        c.execute("INSERT INTO events(ts,team_id,kind,payload_json) VALUES(1,'tm','human_input',?)",
                  (json.dumps({"text": "비밀 계획 검색어"}, ensure_ascii=False),))
        c.execute("INSERT INTO ingest(session_id,offset) VALUES('s-m', 500)")
        c.execute("INSERT INTO worklog_ws(session_id,off) VALUES('w1', 700)")
        try:
            c.execute("DELETE FROM texts")
        except sqlite3.OperationalError:
            pytest.skip("FTS5 없음")
    store.backup_now()
    store.close()
    p = store.db_path()
    for suf in ("-wal", "-shm"):
        try:
            (p.parent / (p.name + suf)).unlink()
        except OSError:
            pass
    p.write_bytes(b"garbage" * 1000)
    assert team.list_teams()[0]["name"] == "팀"
    r = store.reader()
    try:
        assert r.execute("SELECT offset FROM ingest WHERE session_id='s-m'").fetchone()[0] == 0
        assert r.execute("SELECT off FROM worklog_ws WHERE session_id='w1'").fetchone()[0] == 0
        assert r.execute("SELECT COUNT(*) FROM texts WHERE texts MATCH '검색어'").fetchone()[0] == 1
    finally:
        r.close()
    assert any(n["event"] == "restored_from_backup" for n in store.notices())
    assert list((_dd() / "backups").glob("corrupt-*"))


def test_backup_keeps_n(home):
    store.writer()
    d = store.backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(9):
        (d / f"state-2026010{i}.db").write_bytes(b"x")
    store.backup_now(keep=7)
    assert len(list(d.glob("state-*.db"))) == 7


# ---------------------------------------------------------------- 세션 표(T8)
def test_every_session_table_is_registered(home):
    """session_id 열이 있는 표는 '세션 삭제 때 지움' 또는 '남김' 중 하나에 반드시 등록돼야 한다(손으로 맞추는 목록 방지)."""
    store.writer()
    have = store.tables_with_session_id()
    known = set(store.SESSION_TABLES_DELETE) | set(store.SESSION_TABLES_KEEP)
    assert have - known == set(), f"등록 안 된 세션 표: {sorted(have - known)}"
    assert set(store.SESSION_TABLES_DELETE) & set(store.SESSION_TABLES_KEEP) == set()


def test_delete_and_restore_session_rows(home):
    from session_manager import hooks, session_meta, lifecycle, profiles
    from tests.conftest import write_session
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    write_session(home, "F--x", sid, [{"type": "user", "cwd": "F:\\x", "message": {"role": "user", "content": "hi"},
                                        "timestamp": "2026-06-01T00:00:00.000Z"}])
    hooks._persist_loaded = True
    session_meta.set_record(sid, model="지울 세션", tags=["태그"])
    profiles.set_record(sid, model="fable")
    hooks.update_from_event({"session_id": sid, "hook_event_name": "Stop"})
    r = lifecycle.delete_session(sid, dry_run=False, force=True)
    assert r["deleted"]
    assert session_meta.get(sid) == {} and profiles.get(sid) == {} and hooks.status_of(sid) is None
    bucket = r["trash"].replace("\\", "/").rsplit("/", 1)[-1]
    lifecycle.restore_session(bucket)
    assert session_meta.get(sid)["model"] == "지울 세션" and profiles.get(sid)["model"] == "fable"


# ---------------------------------------------------------------- 훅 쓰기(D2)
def test_hook_does_not_wait_for_db_lock(home):
    """다른 쓰기가 잠금을 오래 쥐고 있어도 훅 처리는 바로 끝나고, 풀린 뒤 쓰기 스레드가 저장한다."""
    from session_manager import hooks
    hooks._persist_loaded = True
    hooks._STATUS.clear()
    hooks._last_persist = 0.0
    store.writer()
    held, release = threading.Event(), threading.Event()

    def hold():
        with store.tx():
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    t0 = time.time()
    for i in range(30):
        hooks.update_from_event({"session_id": f"burst{i}", "hook_event_name": "UserPromptSubmit"})
    assert time.time() - t0 < 1.0                                # 잠금을 기다리지 않았다
    release.set()
    t.join()
    deadline = time.time() + 10
    while time.time() < deadline:
        r = store.reader()
        try:
            n = r.execute("SELECT COUNT(*) FROM session_runtime WHERE session_id LIKE 'burst%'").fetchone()[0]
        finally:
            r.close()
        if n == 30:
            break
        time.sleep(0.2)
    assert n == 30
    hooks._STATUS.clear()


def test_flush_thread_restarts_with_incident(home, monkeypatch):
    from session_manager import hooks
    hooks._persist_loaded = True
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    monkeypatch.setattr(hooks, "_flusher", dead)
    monkeypatch.setattr(hooks, "_last_persist", time.time())    # 바로 쓰지 않고 스레드에 맡기게
    hooks.update_from_event({"session_id": "rs1", "hook_event_name": "Stop"})
    assert hooks._flusher is not dead and hooks._flusher.is_alive()
    assert "writer_thread_restarted" in (_dd() / "incidents.jsonl").read_text(encoding="utf-8")
    hooks._STATUS.pop("rs1", None)


# ---------------------------------------------------------------- 되돌림 대상(O1)
def test_rollback_target(home, monkeypatch):
    from session_manager import legacy_import, updater
    monkeypatch.setattr(updater, "current_version", lambda: "0.36.0")
    wd = _dd() / "updates"
    for n in ("backup-0.34.2", "backup-0.35.0", "backup-0.37.0", "backup-0.35.0-failed-0.36.0", "backup-0.35.5.tmp.123"):
        (wd / n).mkdir(parents=True)
    assert legacy_import.rollback_target() == "0.35.0"
    assert legacy_import.mirror_needed() is True                # R1 은 늘 거울


def test_restore_kept_value_via_api(home, monkeypatch):
    """애매한 거울에서 보존한 파일 값으로 사장님이 되돌린다(D5 — 설정 화면)."""
    test_crash_after_db_commit_before_json_is_ambiguous(home)
    from fastapi.testclient import TestClient
    from session_manager import session_meta, server
    st = store.status()
    assert st["kept"] and st["db"] == "team.db" and any(n["event"] == "mirror_ambiguous" for n in st["notices"])
    monkeypatch.setattr(server, "_is_local_request", lambda *a, **k: True, raising=False)
    with TestClient(server.create_app()) as client:
        r = client.post("/api/owner/store/restore-kept", json={"kept": st["kept"][0]})
        assert r.status_code == 200, r.text
        assert client.post("/api/owner/store/restore-kept", json={"kept": "../labels.json"}).status_code == 400
        assert client.get("/api/owner/store").json()["schema"] == store.SCHEMA_VERSION
    assert session_meta.get("s1")["model"] == "가"                      # 보존본(옛 파일) 값
