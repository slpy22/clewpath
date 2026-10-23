"""업데이트 안전성 회귀 가드 (2026-09-04 실사고 대응).

사고: 세션 많은 PC 에서 헬스 엔드포인트가 scan_all(전체 파싱) 로 느려 업데이트
헬스체크(45초)가 타임아웃 → 멀쩡한 새 버전이 롤백. 사용자가 재시도하자 runner
2개 동시 실행 → 포트 경합으로 서비스 완전 사망.

가드:
1. /api/owner/diagnostics 는 scan_all 을 부르지 않는다(빠른 파일 카운트).
2. updater.apply 는 5분 내 동시 재실행을 거부한다(잠금).
"""
from __future__ import annotations

import pytest

from tests.conftest import write_session


def _client(fake_claude_home, monkeypatch):
    from fastapi.testclient import TestClient
    for k in ("SM_RELAY_URL", "SM_RELAY_ROOM", "SM_RELAY_AGENT_TOKEN", "SM_CP_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SM_HOST", "127.0.0.1")
    from session_manager.server import create_app
    return TestClient(create_app())


def test_diagnostics_does_not_call_scan_all(fake_claude_home, monkeypatch):
    # scan_all 을 부르면 폭발하도록 심어두고, diagnostics 가 여전히 정상이면
    # = scan_all 을 안 부른다는 증거(콜드 캐시에서 느려지지 않음).
    write_session(fake_claude_home, "F--p", "aaaaaaaa-1111-2222-3333-444444444444", [
        {"type": "user", "cwd": "F:/p", "message": {"role": "user", "content": "hi"},
         "timestamp": "t0"}])
    write_session(fake_claude_home, "F--p", "bbbbbbbb-1111-2222-3333-444444444444", [
        {"type": "user", "cwd": "F:/p", "message": {"role": "user", "content": "hi"},
         "timestamp": "t0"}])
    from session_manager import scanner
    monkeypatch.setattr(scanner, "scan_all",
                        lambda: (_ for _ in ()).throw(AssertionError("scan_all 호출됨!")))
    with _client(fake_claude_home, monkeypatch) as c:
        r = c.get("/api/owner/diagnostics")
        assert r.status_code == 200
        j = r.json()
        assert j["session_count"] == 2      # 파일 카운트로 정확
        assert j["ok"] is True


# 모든 OS 의 릴리스 — 클레임이 없으면 win 전용이라 맥·리눅스에선 apply 가 먼저 거절한다(updater.supports_this_os)
_ALL_OS = ["win", "mac", "linux"]


def test_apply_concurrency_lock(fake_claude_home, monkeypatch):
    from session_manager import updater
    # 실제 runner 스폰은 막는다(부작용 없이 잠금 로직만 검증)
    calls = {"n": 0}

    class _FakePopen:
        def __init__(self, *a, **k):
            calls["n"] += 1
    monkeypatch.setattr(updater, "_popen", _FakePopen)

    m = {"ver": "9.9.9", "platforms": _ALL_OS}
    staged = fake_claude_home / "stage"
    staged.mkdir()
    # 1회차: 정상 실행(잠금 생성)
    updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 1
    # 2회차(5분 내): 거부 → runner 재스폰 안 됨
    with pytest.raises(updater.UpdateError):
        updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 1     # 두 번째는 Popen 안 함

    # 잠금이 만료(5분 경과 시뮬)되면 다시 허용
    import os
    lock = updater.work_dir() / "apply.lock"
    old = os.stat(lock).st_mtime - 400
    os.utime(lock, (old, old))
    updater.apply(m, staged, restart_cmd="")
    assert calls["n"] == 2


def _apply_port(fake_claude_home, monkeypatch, bound_port, runtime_port):
    """apply() 가 runner 에 넘기는 -Port 값. runtime.json 은 runtime_port 로 심어둔다."""
    import json
    from session_manager import config, updater
    captured = {}

    class _Popen:
        def __init__(self, args, *a, **k):
            captured["args"] = list(args)
    monkeypatch.setattr(updater, "_popen", _Popen)
    monkeypatch.setattr(updater, "BOUND_PORT", bound_port)
    d = config.data_dir(); d.mkdir(parents=True, exist_ok=True)
    (d / "runtime.json").write_text(json.dumps({"port": runtime_port}), encoding="utf-8")
    lock = updater.work_dir() / "apply.lock"
    if lock.exists():
        lock.unlink()
    staged = fake_claude_home / f"stage-{bound_port}-{runtime_port}"
    staged.mkdir()
    updater.apply({"ver": "9.9.9", "platforms": _ALL_OS}, staged, restart_cmd="")
    a = captured["args"]
    return a[a.index("-Port") + 1]


def test_apply_prefers_bound_port_over_stale_runtime_json(fake_claude_home, monkeypatch):
    # 실사고(2026-09-10 운영자 PC): 5199 테스트 서버가 남긴 stale runtime.json 때문에
    # runner 가 5199 를 헬스체크해 멀쩡한 0.7.0 을 롤백. 살아있는 서버가 아는 실제
    # 바인드 포트(BOUND_PORT)가 권위여야 한다.
    monkeypatch.delenv("SM_PORT", raising=False)
    assert _apply_port(fake_claude_home, monkeypatch, bound_port=5100, runtime_port=5199) == "5100"


def test_apply_falls_back_to_runtime_json_without_bound_port(fake_claude_home, monkeypatch):
    # 서버 밖(테스트·CLI)에서는 BOUND_PORT 가 없으니 runtime.json 이 그대로 폴백.
    monkeypatch.delenv("SM_PORT", raising=False)
    assert _apply_port(fake_claude_home, monkeypatch, bound_port=None, runtime_port=5199) == "5199"


def test_apply_runs_a_copy_outside_the_install(fake_claude_home, monkeypatch):
    """러너는 설치 폴더 밖(데이터 폴더 updates/)의 사본으로 돈다 — 교체하는 동안 자기 파일이 옮겨지지 않게(S32).
    맥·리눅스는 update_runner.sh(실행 비트 없이 /bin/sh 로), Windows 는 update_runner.ps1."""
    from session_manager import osdep, updater
    captured = {}

    class _Popen:
        def __init__(self, args, *a, **k):
            captured["args"] = list(args)
    monkeypatch.setattr(updater, "_popen", _Popen)
    staged = fake_claude_home / "stage"
    staged.mkdir()
    updater.apply({"ver": "9.9.9", "platforms": _ALL_OS}, staged, restart_cmd="")
    copy = updater.work_dir() / osdep.UPDATE_RUNNER
    assert copy.read_bytes() == updater.runner_script().read_bytes()
    assert str(copy) in captured["args"]
    assert updater.install_root() not in copy.resolve().parents
    if osdep.NAME != "win":
        assert captured["args"][:2] == ["/bin/sh", str(copy)]


def test_runner_spawn_patch_does_not_reach_other_threads(fake_claude_home, monkeypatch):
    """TODOS 172행(간헐 실패의 원인): 시험이 updater.subprocess.Popen(= 표준 subprocess 모듈 그 자체)을 바꿔 끼우면 같은 프로세스의
    다른 스레드(앞 시험 앱의 orphan-sweep 등)가 부르는 subprocess 까지 가짜로 가서 호출 수 단언이 가끔 틀어졌다.
    updater 는 러너를 자기 참조(_popen)로 띄우고 시험은 그것만 바꾼다 — 다른 스레드의 subprocess 는 진짜 그대로 돈다."""
    import subprocess
    import sys
    import threading
    from session_manager import updater
    real = subprocess.Popen
    calls = []
    monkeypatch.setattr(updater, "_popen", lambda args, *a, **k: calls.append(list(args)))
    assert subprocess.Popen is real

    go, out = threading.Event(), {}

    def background():                # 앞 시험 앱의 배경 스레드 흉내 — apply 와 겹쳐 subprocess 를 부른다
        go.wait(5)
        out["rc"] = subprocess.run([sys.executable, "-c", "pass"]).returncode
    t = threading.Thread(target=background)
    t.start()
    go.set()
    staged = fake_claude_home / "stage"
    staged.mkdir()
    updater.apply({"ver": "9.9.9", "platforms": _ALL_OS}, staged, restart_cmd="")
    t.join(60)
    assert out.get("rc") == 0                       # 다른 스레드의 subprocess 는 진짜로 돌았고
    assert len(calls) == 1 and any("9.9.9" == str(x) for x in calls[0])     # 러너 기동만 가짜로 왔다


def _race_apply(n: int = 2) -> list[str]:
    """apply 를 n 스레드로 동시에 — 각자 'ok'(러너 띄움) 또는 'busy'(잠금에 막힘)."""
    import threading
    from session_manager import config, updater
    staged = config.data_dir() / "stage-race"
    staged.mkdir(parents=True, exist_ok=True)
    out: list[str] = []

    def go():
        try:
            updater.apply({"ver": "9.9.9", "platforms": _ALL_OS}, staged, restart_cmd="")
            out.append("ok")
        except updater.UpdateError as e:
            out.append("busy" if "진행 중" in str(e) else f"err:{e}")
    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return sorted(out)


def test_apply_lock_is_taken_atomically(fake_claude_home, monkeypatch):
    """TODOS 136행: '있고 5분 안이면 거부' 를 본 뒤 쓰기라, 두 적용이 동시에 '없음'을 보면 둘 다 잠금을 쓰고 러너를 둘 띄웠다
    (2026-09-04 사고와 같은 모양 — 포트 경합). 잠금은 만들기 자체가 확인이어야 한다(O_EXCL) — 먼저 잡은 쪽만 띄운다."""
    import pathlib
    import threading
    from session_manager import updater
    calls = []
    monkeypatch.setattr(updater, "_popen", lambda args, *a, **k: calls.append(1))
    both_looked = threading.Barrier(2, timeout=2)
    real_is_file = pathlib.Path.is_file

    def is_file(self, *a, **k):                    # 옛 구현의 '있나 보기' 를 둘이 함께 지나가게 — 그 뒤에 각자 쓴다
        r = real_is_file(self, *a, **k)
        if self.name == "apply.lock":
            try:
                both_looked.wait()
            except threading.BrokenBarrierError:
                pass
        return r
    monkeypatch.setattr(pathlib.Path, "is_file", is_file)
    assert _race_apply(2) == ["busy", "ok"]
    assert len(calls) == 1
    assert real_is_file(updater.work_dir() / "apply.lock")


def test_stale_lock_cleared_by_only_one_of_two(fake_claude_home, monkeypatch):
    """5분 넘은 시체 잠금을 둘이 동시에 보면: 하나만 치우고 잡는다. 늦게 온 쪽이 먼저 잡은 쪽의 새 잠금을 치우면 안 된다."""
    import os
    import threading
    from session_manager import updater
    calls = []
    monkeypatch.setattr(updater, "_popen", lambda args, *a, **k: calls.append(1))
    lock = updater.work_dir() / "apply.lock"
    lock.write_text("old", encoding="utf-8")
    old = lock.stat().st_mtime - 400
    os.utime(lock, (old, old))
    both_saw = threading.Barrier(2, timeout=2)
    real_age = updater._lock_age
    seen = {"n": 0}

    def age(p):                                    # 둘 다 '시체' 판정을 본 뒤에 치우러 간다
        a = real_age(p)
        if p.name == "apply.lock" and seen["n"] < 2:
            seen["n"] += 1
            try:
                both_saw.wait()
            except threading.BrokenBarrierError:
                pass
        return a
    monkeypatch.setattr(updater, "_lock_age", age)
    assert _race_apply(2) == ["busy", "ok"]
    assert len(calls) == 1
    assert lock.read_text(encoding="utf-8") != "old"                     # 새 잠금이 섰다
    assert [p.name for p in lock.parent.iterdir() if ".stale-" in p.name] == []


def test_stale_check_then_fresh_lock_is_put_back(fake_claude_home, monkeypatch):
    """'시체'로 본 뒤 옮겨 오니 그새 다른 적용이 잡은 새 잠금이었다 → 제자리에 돌려놓고 거부한다(지우지 않는다)."""
    from session_manager import updater
    monkeypatch.setattr(updater, "_popen", lambda *a, **k: pytest.fail("러너를 띄우면 안 된다"))
    lock = updater.work_dir() / "apply.lock"
    lock.write_text("fresh-from-other-apply", encoding="utf-8")          # 막 잡힌 새 잠금
    real_age = updater._lock_age
    monkeypatch.setattr(updater, "_lock_age", lambda p: 999.0 if p.name == "apply.lock" else real_age(p))
    staged = fake_claude_home / "stage"
    staged.mkdir()
    with pytest.raises(updater.UpdateError, match="진행 중"):
        updater.apply({"ver": "9.9.9", "platforms": _ALL_OS}, staged, restart_cmd="")
    assert lock.read_text(encoding="utf-8") == "fresh-from-other-apply"
    assert [p.name for p in lock.parent.iterdir() if ".stale-" in p.name] == []
