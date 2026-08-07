"""export → import 라운드트립 테스트. fixture 기반."""
import json
from pathlib import Path

from session_manager import exporter, importer, config, pathenc
from tests.conftest import write_session


def _make_session(home, sid="exp11111", folder="P--src", cwd="F:\\src\\proj"):
    write_session(home, folder, sid, [
        {"type": "user", "cwd": cwd, "slug": "s", "message": {"role": "user", "content": "hi"},
         "timestamp": "2026-06-01T00:00:00.000Z"},
        {"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": "yo"},
         "timestamp": "2026-06-01T00:01:00.000Z"},
    ])
    return home / "projects" / folder / f"{sid}.jsonl"


def test_export_creates_zip_with_manifest(fake_claude_home, tmp_path):
    _make_session(fake_claude_home)
    out = tmp_path / "export.zip"
    res = exporter.create_export("exp11111", str(out), include_workdir=False)
    assert out.exists()
    assert res["manifest"]["session_id"] == "exp11111"
    assert res["manifest"]["original_cwd"] == "F:\\src\\proj"


def test_pathenc_known_case():
    assert pathenc.path_to_folder("F:\\021_3_AX 기술스택") == "F--021-3-AX-----"


def test_import_into_fresh_home(fake_claude_home, tmp_path, monkeypatch):
    # 1) 원본 홈에서 export
    _make_session(fake_claude_home)
    zip_out = tmp_path / "export.zip"
    exporter.create_export("exp11111", str(zip_out), include_workdir=False)

    # 2) 새 홈으로 전환 후 import (다른 PC 시뮬레이션)
    new_home = tmp_path / "new_claude"
    (new_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(new_home))

    res = importer.import_session(str(zip_out), new_cwd="D:\\new\\loc")
    assert "error" not in res
    assert res["cwd_lines_rewritten"] == 2
    # 대상 폴더가 새 cwd로 인코딩되었는지
    assert res["target_folder"] == pathenc.path_to_folder("D:\\new\\loc")
    # 실제 jsonl의 cwd가 치환되었는지
    dst = Path(res["target_jsonl"])
    assert dst.exists()
    for line in dst.read_text(encoding="utf-8").splitlines():
        if line.strip():
            assert json.loads(line)["cwd"] == "D:\\new\\loc"


def test_import_no_overwrite_guard(fake_claude_home, tmp_path):
    _make_session(fake_claude_home)
    zip_out = tmp_path / "export.zip"
    exporter.create_export("exp11111", str(zip_out), include_workdir=False)
    # 같은 홈에 다시 import → 이미 존재(원본 폴더명 유지) → 막혀야 함
    res = importer.import_session(str(zip_out), new_cwd=None)
    assert "error" in res


def test_export_import_with_workdir(fake_claude_home, tmp_path, monkeypatch):
    # 작업폴더 생성
    workdir = tmp_path / "src" / "proj"
    workdir.mkdir(parents=True)
    (workdir / "main.py").write_text("print('hi')", encoding="utf-8")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "junk.js").write_text("x", encoding="utf-8")

    _make_session(fake_claude_home, cwd=str(workdir))
    zip_out = tmp_path / "export.zip"
    res = exporter.create_export("exp11111", str(zip_out), include_workdir=True)
    # node_modules는 제외되어야 함
    assert res["manifest"]["workdir_file_count"] == 1

    new_home = tmp_path / "new_claude"
    (new_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("SESSION_MANAGER_CLAUDE_HOME", str(new_home))
    dest = tmp_path / "restored"
    r2 = importer.import_session(str(zip_out), new_cwd=str(dest), restore_workdir=True)
    assert r2["workdir_files_restored"] == 1
    assert (dest / "main.py").exists()
    assert not (dest / "node_modules").exists()
