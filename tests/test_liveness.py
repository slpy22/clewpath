"""v0.9.2 자기 회복 — 비정상 종료 의심 감지·정상 종료 표식·런처 템플릿 계약."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from session_manager import config, liveness

DEAD_PID = 4_000_000_000   # Windows/Linux 모두 존재할 수 없는 pid


def _runtime(pid, started=1700000000):
    d = config.data_dir(); d.mkdir(parents=True, exist_ok=True)
    (d / "runtime.json").write_text(json.dumps({"host": "127.0.0.1", "port": 5100, "pid": pid, "started_at": started}), encoding="utf-8")


def _log(lines):
    (config.data_dir() / "host.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_no_runtime_or_self_is_not_incident(fake_claude_home):
    liveness.LAST_INCIDENT = None
    assert liveness.check_previous_exit() is None
    _runtime(os.getpid())
    assert liveness.check_previous_exit() is None


def test_marked_shutdown_is_normal_and_marker_consumed(fake_claude_home):
    _runtime(DEAD_PID)
    liveness.shutdown_file().parent.mkdir(parents=True, exist_ok=True)
    liveness.shutdown_file().write_text(json.dumps({"pid": DEAD_PID, "reason": "update"}), encoding="utf-8")
    assert liveness.check_previous_exit() is None
    assert not liveness.shutdown_file().exists()
    assert not liveness.incidents_file().exists()


def test_vanished_without_marker_is_incident_with_tail(fake_claude_home):
    liveness.LAST_INCIDENT = None
    _runtime(DEAD_PID, started=1790000000)
    _log([f"line {i}" for i in range(30)] + ["\x1b[31mINFO: last line\x1b[0m"])
    inc = liveness.check_previous_exit()
    assert inc and inc["prev_pid"] == DEAD_PID and inc["prev_started_at"] == 1790000000
    assert inc["tail"][-1] == "INFO: last line" and len(inc["tail"]) == 10     # ANSI 제거·10줄
    rec = json.loads(liveness.incidents_file().read_text(encoding="utf-8").splitlines()[0])
    assert rec["prev_pid"] == DEAD_PID and liveness.LAST_INCIDENT is inc


def test_other_live_instance_is_not_incident(fake_claude_home):
    _runtime(os.getppid())            # 살아 있는 다른 프로세스(예: 개발 서버) — 사고 아님
    assert liveness.check_previous_exit() is None


def test_stale_marker_for_other_pid_does_not_mask(fake_claude_home):
    _runtime(DEAD_PID)
    liveness.shutdown_file().write_text(json.dumps({"pid": DEAD_PID - 1, "reason": "normal"}), encoding="utf-8")
    assert liveness.check_previous_exit() is not None            # 다른 pid 의 표식은 무관


def test_mark_shutdown_writes_own_pid(fake_claude_home):
    liveness.mark_shutdown("normal")
    d = json.loads(liveness.shutdown_file().read_text(encoding="utf-8"))
    assert d["pids"][str(os.getpid())]["reason"] == "normal"      # 0.10.2: pid 별 표식


def test_mark_shutdown_for_another_pid_masks_that_exit(fake_claude_home):
    """맥·리눅스 런처의 강제 stop(15초 뒤 KILL)은 죽인 Host 의 pid 로 표식을 남긴다 — 사람이 시킨 종료는 사고가 아니다."""
    liveness.mark_shutdown("forced-stop", pid=DEAD_PID)
    marks = json.loads(liveness.shutdown_file().read_text(encoding="utf-8"))["pids"]
    assert marks[str(DEAD_PID)]["reason"] == "forced-stop" and str(os.getpid()) not in marks
    _runtime(DEAD_PID)
    assert liveness.check_previous_exit() is None and not liveness.incidents_file().exists()


def test_log_tail_stops_at_last_launcher_mark(fake_claude_home):
    """맥·리눅스 런처는 host.log 를 이어 쓰고 기동 직전 구분줄을 넣는다. 새 Host 가 이미 찍은 첫 줄은 죽은 Host 의
    꼬리가 아니다 — 마지막 구분줄 앞까지만."""
    config.data_dir().mkdir(parents=True, exist_ok=True)
    _log(["=== launcher start 2026-10-19 09:00:00 ===", "old 1", "old 2: Traceback 마지막",
          "=== launcher start 2026-10-20 10:00:00 ===", "[config] 새 프로세스 첫 줄", "[serve] http://127.0.0.1:5100"])
    assert liveness.log_tail(10) == ["=== launcher start 2026-10-19 09:00:00 ===",   # 죽은 Host 가 뜬 때(그 구간의 머리)
                                     "old 1", "old 2: Traceback 마지막"]
    assert liveness.log_tail(1) == ["old 2: Traceback 마지막"]
    _log(["no mark a", "no mark b"])                              # Windows(덮어쓰기)·구분줄 없는 로그는 그대로
    assert liveness.log_tail(10) == ["no mark a", "no mark b"]


def test_notify_incident_payload(fake_claude_home, monkeypatch):
    from session_manager import push
    sent = []
    monkeypatch.setattr(push, "send", lambda kind, sid, title, body, extra=None: sent.append((kind, sid, title, body)) or 1)
    n = liveness.notify_incident({"prev_pid": 123, "tail": ["a", "b", "c"]})
    assert n == 1 and sent[0][0] == "host-recovered" and "복구" in sent[0][2]
    assert "b\nc" in sent[0][3] and len(sent[0][3]) <= 300


def test_launcher_template_and_ensure_script_contract():
    tpl = Path(liveness.__file__).parent / "start-connector.template.ps1"
    ens = Path(liveness.__file__).parent / "ensure_task.ps1"
    t = tpl.read_text(encoding="utf-8")
    assert t.splitlines()[0].startswith("# clewpath-launcher v2")
    for needle in ("/api/health", "maintenance.flag", "launcher.lock", "runtime.json", "{{DATA_DIR}}", "{{CONF_FILE}}", "{{PORT}}", "launcher.log"):
        assert needle in t
    e = ens.read_text(encoding="utf-8")
    # 런처가 멱등이 아니면 반복 트리거를 달지 않는다(겹쳐 뜨는 사고 방지) — 순서 계약
    assert e.index("launcher not idempotent") < e.index("RepetitionInterval")
    assert "MultipleInstances IgnoreNew" in e


def test_ensure_launcher_skips_dev_tree(fake_claude_home):
    assert liveness.ensure_launcher(5100) is None               # 리포 실행 = start-connector.ps1 없음
    assert not list(config.data_dir().glob("launcher-ensured-*"))


@pytest.mark.skipif(sys.platform == "win32", reason="맥·리눅스 런처(start-connector.sh) 경로")
def test_ensure_launcher_posix_regenerates_once_per_version(fake_claude_home, tmp_path, monkeypatch):
    """업데이트 뒤 첫 기동이 새 템플릿으로 런처를 다시 렌더한다(버전당 1회). 폴백 포트로 떴어도 DEFAULT_PORT 는 그대로."""
    from session_manager.osdep import _posix
    root = tmp_path / "app"
    (root / "session_manager").mkdir(parents=True)
    (root / "session_manager" / _posix.LAUNCHER_TEMPLATE).write_text(
        "#!/bin/sh\n# clewpath-launcher v2 (posix)\nDATA_DIR={{DATA_DIR}}\nCONF_FILE={{CONF_FILE}}\nDEFAULT_PORT={{PORT}}\n",
        encoding="utf-8")
    (root / _posix.START_SCRIPT).write_text("#!/bin/sh\n# 옛 런처\nDEFAULT_PORT=5100\n", encoding="utf-8")
    monkeypatch.setattr(liveness, "_install_root", lambda: root)
    monkeypatch.setattr(liveness, "_version", lambda: "9.9.9")
    config.data_dir().mkdir(parents=True, exist_ok=True)          # 설치본에선 Host 가 뜨기 전에 이미 있다
    out = liveness.ensure_launcher(5101)
    assert out == "launcher: regenerated (posix v2)"
    assert (config.data_dir() / "launcher-ensured-9.9.9").read_text(encoding="utf-8") == out
    text = (root / _posix.START_SCRIPT).read_text(encoding="utf-8")
    assert "DEFAULT_PORT=5100\n" in text and f"DATA_DIR={config.data_dir()}" in text.replace("'", "")
    assert liveness.ensure_launcher(5101) is None                 # 버전당 1회


def test_marks_are_per_pid_and_survive_overwrite_by_other_process(fake_claude_home, monkeypatch):
    """회귀(2026-09-30 0.10.1 apply): 옛 Host 의 update 표식을 그 사이 뜬 다른 프로세스의 normal 표식이 덮어써
    새 Host 가 옛 Host 를 비정상 종료로 오판·복구 알림. pid 별로 쌓으면 둘 다 남는다."""
    OLD, OTHER = DEAD_PID, DEAD_PID - 1
    # undo() 는 fake_claude_home 의 env 까지 되돌려 실제 ~/.claude 를 읽게 되므로 context() 로 좁힌다
    with monkeypatch.context() as m:
        m.setattr(os, "getpid", lambda: OLD); liveness.mark_shutdown("update")
        m.setattr(os, "getpid", lambda: OTHER); liveness.mark_shutdown("normal")
    marks = json.loads(liveness.shutdown_file().read_text(encoding="utf-8"))["pids"]
    assert set(marks) == {str(OLD), str(OTHER)} and marks[str(OLD)]["reason"] == "update"
    _runtime(OLD)
    assert liveness.check_previous_exit() is None, "옛 Host 의 표식이 살아 있다 → 사고 아님"
    left = json.loads(liveness.shutdown_file().read_text(encoding="utf-8"))["pids"]
    assert set(left) == {str(OTHER)}, "소비한 표식만 지우고 다른 pid 것은 남긴다"
    _runtime(OTHER)
    assert liveness.check_previous_exit() is None
    assert not liveness.shutdown_file().exists(), "마지막 표식까지 소비되면 파일 제거"


def test_marks_keep_recent_only(fake_claude_home, monkeypatch):
    with monkeypatch.context() as m:
        for i in range(12):
            m.setattr(os, "getpid", lambda i=i: 1000 + i)
            m.setattr(liveness.time, "time", lambda i=i: 1_700_000_000 + i)
            liveness.mark_shutdown("normal")
    marks = json.loads(liveness.shutdown_file().read_text(encoding="utf-8"))["pids"]
    assert len(marks) == liveness._MARK_KEEP and "1011" in marks and "1000" not in marks
