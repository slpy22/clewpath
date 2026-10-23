"""맥·리눅스 런처 보장도 실패면 표식을 쓰지 않는다 — Windows 노트북 사고(2026-10-22)와 같은 결함의 POSIX 쪽.

예전 _posix.ensure_launcher 는 렌더 실패를 'launcher: regenerate failed: …' 문자열로 돌려줘, liveness 가 성공으로 보고
launcher-ensured-<버전> 표식을 썼다(그 버전 동안 다시 시도하지 않음). 이제 _win 과 같이 예외로 알린다.
"""
from __future__ import annotations

import pytest

from session_manager import config, liveness
from session_manager.osdep import _posix


def _fake_root(tmp_path):
    root = tmp_path / "app"
    (root / "session_manager").mkdir(parents=True)
    (root / "session_manager" / _posix.LAUNCHER_TEMPLATE).write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def test_posix_ensure_failure_raises(tmp_path, monkeypatch):
    root = _fake_root(tmp_path)
    monkeypatch.setattr(_posix, "write_launcher", lambda *a, **k: (_ for _ in ()).throw(OSError("읽기 전용")))
    with pytest.raises(RuntimeError, match="regenerate failed"):
        _posix.ensure_launcher(root, tmp_path / "data", tmp_path / "c.toml", 5100)


def test_posix_ensure_success_and_skip_return_text(tmp_path, monkeypatch):
    root = _fake_root(tmp_path)
    monkeypatch.setattr(_posix, "write_launcher", lambda *a, **k: "unchanged")
    assert _posix.ensure_launcher(root, tmp_path / "data", tmp_path / "c.toml", 5100) == "launcher: ok (posix v2)"
    assert _posix.ensure_launcher(tmp_path / "없음", tmp_path / "data", tmp_path / "c.toml", 5100).endswith("skip")


def test_liveness_writes_no_marker_when_posix_ensure_fails(fake_claude_home, tmp_path, monkeypatch):
    from session_manager import osdep
    root = _fake_root(tmp_path)
    (root / osdep.START_SCRIPT).write_text("x", encoding="utf-8")
    monkeypatch.setattr(liveness, "_install_root", lambda: root)
    monkeypatch.setattr(osdep, "ensure_launcher", _posix.ensure_launcher)
    monkeypatch.setattr(_posix, "write_launcher", lambda *a, **k: (_ for _ in ()).throw(OSError("디스크 가득")))
    out = liveness.ensure_launcher(5100)
    assert out.startswith("ensure failed") and "regenerate failed" in out
    assert not list(config.data_dir().glob("launcher-ensured-*"))
